"""把 :class:`ProblemBundle` 写成现有格式的数据包，并立即用 ``parse_testdata`` 回读自检。

写出的包::

    <name>.in / <name>.ans     # name 规整成 [A-Za-z0-9._-]，冲突加 -2、-3
    config.json                # cases（name + score）与 subtasks
    checker.cpp + 支持文件      # 有 checker 时；用了 testlib 而缺 testlib.h 时补上

native 有 ``raw_testdata`` 时原样用内层 zip（sha256 不变），同样过一遍校验。

内存：数据包一律写临时文件（:func:`try_build`），不进内存；预览只要摘要，走 :func:`try_summarize`
（测试点写成空文件的骨架包回读自检，测试数据按成员各解压一遍查损坏），不产出完整数据包。
一次请求里全部题目的构建量另有上限（:class:`BuildBudget`）。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import IO, Dict, List, Optional, Set, Tuple, Union

from ...judging.testdata import MAX_UNCOMPRESSED_BYTES, TestdataError, parse_testdata
from .archive import CORRUPT_ERRORS, MAX_DESCRIPTOR_BYTES, SafeArchive, SingleFileArchive, human_size
from .bundle import FileRef, ProblemBundle
from .checkers import inject_testlib
from .notices import ProblemIoError, notice

__all__ = [
    "MAX_TESTDATA_BYTES",
    "MAX_BUILD_BYTES",
    "MAX_REQUEST_BUILD_BYTES",
    "flat_case_names",
    "unique_name",
    "read_ref",
    "BuildBudget",
    "BuiltTestdata",
    "write_testdata",
    "build_testdata_zip",
    "summarize",
    "try_build",
    "try_summarize",
]

#: 与 ``POST /problems/{id}/testdata`` 的上传上限一致
MAX_TESTDATA_BYTES = 512 * 1024 * 1024
#: 写出的数据包解压后总量上限（与 ``parse_testdata`` 一致）。同一个成员被多个测试点引用时
#: 每次引用都算一次 —— 写之前先累计，超了就不写
MAX_BUILD_BYTES = MAX_UNCOMPRESSED_BYTES
#: 一次请求（preview / apply）里全部题目按引用累计的构建量上限。导入包本身解压后 ≤ 2 GB，
#: 正常的包到不了；挡的是"小包 + 多道题反复引用同一个大成员"
MAX_REQUEST_BUILD_BYTES = 4 * 1024 * 1024 * 1024
_CHUNK = 256 * 1024

Archive = Union[SafeArchive, SingleFileArchive, None]

_FLAT_BAD = re.compile(r"[^A-Za-z0-9._-]")


def _flat(name: str) -> str:
    text = _FLAT_BAD.sub("_", name.replace("/", "_")).strip(".") or "case"
    return text[:120]


def unique_name(base: str, used: set, sep: str = "-") -> str:
    """``base`` 不撞 ``used``（不分大小写）就用它，否则加 ``<sep>2``、``<sep>3``……；结果记进 ``used``。

    生成的名字也记进去，后面真实存在的同名文件才不会撞上它。"""
    candidate = base
    serial = 2
    while candidate.lower() in used:
        candidate = "%s%s%d" % (base, sep, serial)
        serial += 1
    used.add(candidate.lower())
    return candidate


def flat_case_names(names: List[str]) -> Dict[str, str]:
    """来源测试点名 → 扁平名。冲突（不分大小写）时加 ``-2``、``-3``。"""
    out: Dict[str, str] = {}
    used: set = set()
    for name in names:
        out[name] = unique_name(_flat(name), used)
    return out


def read_ref(ref: FileRef, archive: Archive, limit: int = MAX_DESCRIPTOR_BYTES) -> bytes:
    if ref.data is not None:
        if len(ref.data) > limit:
            raise TestdataError("文件超过上限")
        return ref.data
    if archive is None:
        raise TestdataError("缺少归档")
    try:
        return archive.read(ref.member, limit)  # type: ignore[arg-type]
    except KeyError:
        raise TestdataError("缺少文件 %s" % ref.member)
    except ProblemIoError as exc:
        raise TestdataError(exc.notice.message)


def _open_ref(ref: FileRef, archive: Archive) -> IO[bytes]:
    if ref.data is not None:
        return io.BytesIO(ref.data)
    if archive is None:
        raise TestdataError("缺少归档")
    try:
        return archive.open(ref.member)  # type: ignore[arg-type]
    except KeyError:
        raise TestdataError("缺少文件 %s" % ref.member)


def _copy_ref(ref: FileRef, archive: Archive, zf: zipfile.ZipFile, arcname: str) -> None:
    if ref.data is not None:
        zf.writestr(arcname, ref.data)
        return
    with _open_ref(ref, archive) as source, zf.open(arcname, "w") as target:
        shutil.copyfileobj(source, target, _CHUNK)


def _ref_size(ref: FileRef, archive: Archive) -> int:
    if ref.data is not None:
        return len(ref.data)
    if archive is None:
        raise TestdataError("缺少归档")
    try:
        return archive.size(ref.member)  # type: ignore[arg-type]
    except KeyError:
        raise TestdataError("缺少文件 %s" % ref.member)


def _check_volume(bundle: ProblemBundle, archive: Archive) -> int:
    """按引用（含重复引用）累计测试数据的解压量并返回；超过 :data:`MAX_BUILD_BYTES` 就拒绝。

    写出的数据包里每次引用都是一份真实的副本，评测时也要逐份解出，所以这里**不**去重。"""
    total = 0
    for case in bundle.cases:
        total += _ref_size(case.input, archive) + (_ref_size(case.answer, archive) if case.answer else 0)
        if total > MAX_BUILD_BYTES:
            raise TestdataError("解压后超过 %s" % human_size(MAX_BUILD_BYTES))
    return total


class BuildBudget:
    """一次请求（preview 或 apply）里全部题目共用的构建预算。

    * :meth:`charge`：按引用累计每题要写出的量，超过 :data:`MAX_REQUEST_BUILD_BYTES` 时那道题报错
      —— 每题 2 GB 的上限挡不住 200 道题各自反复引用同一个大成员。
    * :attr:`verified`：预览时已完整解压校验过的成员（归档完整路径）。同一成员无论被几个测试点、
      几道题引用，每次请求只解压一遍。
    """

    def __init__(self, limit: Optional[int] = None) -> None:
        self.limit = MAX_REQUEST_BUILD_BYTES if limit is None else limit
        self.used = 0
        self.verified: Set[str] = set()

    def charge(self, amount: int) -> None:
        if self.used + amount > self.limit:
            raise TestdataError("本次导入的数据总量超过 %s，请分批导入" % human_size(self.limit))
        self.used += amount


def _plan(
    bundle: ProblemBundle, archive: Archive, budget: Optional[BuildBudget]
) -> Tuple[Dict[str, str], Dict[str, object]]:
    """构建前的全部检查：测试点名、解压量、子任务引用、请求预算。返回 ``(扁平名, config.json 内容)``。"""
    if not bundle.cases:
        raise TestdataError("没有测试点")
    flat = flat_case_names([case.name for case in bundle.cases])
    if len(flat) != len(bundle.cases):
        raise TestdataError("测试点名重复")
    volume = _check_volume(bundle, archive)
    config: Dict[str, object] = {
        "cases": [{"name": flat[case.name], "score": int(case.score)} for case in bundle.cases],
    }
    if bundle.problem_type != "traditional":
        config["type"] = bundle.problem_type
    if bundle.checker is not None and bundle.checker_protocol:
        config["checker_protocol"] = bundle.checker_protocol
    if bundle.checker is not None and bundle.checker_points:
        config["checker_points"] = bundle.checker_points
    if bundle.problem_type == "answer_only" and bundle.answer_files:
        config["answer_files"] = {flat[name]: file for name, file in bundle.answer_files.items() if name in flat}
    if bundle.problem_type == "grader" and bundle.grader is not None:
        config["grader"] = bundle.grader_name
        config["grader_files"] = sorted(_base(name) for name in bundle.grader_files)
    if bundle.subtasks:
        subtasks = []
        for sub in bundle.subtasks:
            unknown = [name for name in sub.cases if name not in flat]
            if unknown:
                raise TestdataError("子任务里有不存在的测试点：%s" % ", ".join(unknown[:5]))
            subtasks.append({"score": int(sub.score), "cases": [flat[name] for name in sub.cases]})
        config["subtasks"] = subtasks
    if budget is not None:
        budget.charge(volume)
    return flat, config


def _write_zip(
    bundle: ProblemBundle,
    archive: Archive,
    flat: Dict[str, str],
    config: Dict[str, object],
    out: IO[bytes],
    skeleton: bool = False,
) -> None:
    """把数据包写进 ``out``。``skeleton`` 时测试点写成空文件（只为回读结构、算摘要）。"""
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for case in bundle.cases:
            name = flat[case.name]
            if skeleton:
                zf.writestr(name + ".in", b"")
                if case.answer is not None:
                    zf.writestr(name + ".ans", b"")
            else:
                _copy_ref(case.input, archive, zf, name + ".in")
                if case.answer is not None:
                    _copy_ref(case.answer, archive, zf, name + ".ans")
        zf.writestr("config.json", json.dumps(config, ensure_ascii=False, indent=1))
        if bundle.checker is not None:
            source = read_ref(bundle.checker, archive)
            support = dict(bundle.checker_support)
            extra = inject_testlib(source, support)
            if extra is not None:
                bundle.warn(extra)
            zf.writestr("checker.cpp", source)
            for support_name, ref in sorted(support.items()):
                base = support_name.replace("\\", "/").rsplit("/", 1)[-1]
                if not base.lower().endswith((".h", ".hpp")) or base.lower() == "checker.cpp":
                    continue
                zf.writestr(base, read_ref(ref, archive))
        if bundle.interactor is not None:
            zf.writestr("interactor.cpp", read_ref(bundle.interactor, archive))
        if bundle.grader is not None:
            zf.writestr(bundle.grader_name, read_ref(bundle.grader, archive))
            for name, ref in sorted(bundle.grader_files.items()):
                zf.writestr(_base(name), read_ref(ref, archive))


def _base(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _copy_raw(ref: FileRef, archive: Archive, out: IO[bytes]) -> None:
    """native 的内层 zip 原样流式拷进 ``out``（不整包读进内存），超过 512 MB 拒绝。"""
    size = 0
    with _open_ref(ref, archive) as source:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TESTDATA_BYTES:
                raise TestdataError("超过 %s" % human_size(MAX_TESTDATA_BYTES))
            out.write(chunk)


def write_testdata(
    bundle: ProblemBundle, archive: Archive, out: IO[bytes], budget: Optional[BuildBudget] = None
) -> Dict[str, object]:
    """把数据包写进 ``out``（可 seek 的可写文件，写完不关），回读自检后返回摘要。

    结构不对抛 ``TestdataError``（信息给人看）。会往 ``bundle.warnings`` 里加
    ``testlib_injected`` / ``testlib_missing``。
    """
    if bundle.raw_testdata is not None:
        if budget is not None:
            budget.charge(_ref_size(bundle.raw_testdata, archive))
        _copy_raw(bundle.raw_testdata, archive, out)
        out.seek(0)
        return summarize(out, bundle.problem_type)
    flat, config = _plan(bundle, archive, budget)
    _write_zip(bundle, archive, flat, config, out)
    out.seek(0)
    return _self_check(bundle, flat, out)


def build_testdata_zip(bundle: ProblemBundle, archive: Archive) -> bytes:
    """写出数据包字节（整包在内存里）。只给测试与小数据用；接口走 :func:`try_build`（临时文件）。"""
    buffer = io.BytesIO()
    write_testdata(bundle, archive, buffer)
    return buffer.getvalue()


def summarize(data: Union[bytes, IO[bytes]], problem_type: Optional[str] = None) -> Dict[str, object]:
    """数据包摘要（与上传时写进 ``testdata_info`` 的一样）。不合法抛 ``TestdataError``。"""
    source = io.BytesIO(data) if isinstance(data, bytes) else data
    try:
        with zipfile.ZipFile(source) as zf:
            return parse_testdata(zf, problem_type).summary()
    except zipfile.BadZipFile:
        raise TestdataError("不是合法的 zip")


def _self_check(bundle: ProblemBundle, flat: Dict[str, str], source: IO[bytes]) -> Dict[str, object]:
    """回读写出的数据包与 bundle 对账；一致时返回摘要。"""
    with zipfile.ZipFile(source) as zf:
        parsed = parse_testdata(zf, bundle.problem_type)
    want_scores = {flat[case.name]: int(case.score) for case in bundle.cases}
    got_scores = {case.name: case.score for case in parsed.cases}
    want_subs = sorted((int(s.score), sorted(flat[n] for n in s.cases)) for s in bundle.subtasks)
    got_subs = sorted((s.score, sorted(s.cases)) for s in parsed.subtasks)
    if (
        want_scores != got_scores
        or want_subs != got_subs
        or (bundle.checker is not None) != (parsed.checker is not None)
    ):
        raise TestdataError("生成的数据包回读不一致")
    return parsed.summary()


def _verify_members(bundle: ProblemBundle, archive: Archive, budget: BuildBudget) -> None:
    """测试点引用的归档成员各完整解压一遍（读完即丢），查出损坏的压缩数据。

    同一成员每次请求只读一遍（记在 ``budget.verified``）。"""
    for case in bundle.cases:
        for ref in (case.input, case.answer):
            if ref is None or ref.member is None or ref.member in budget.verified:
                continue
            with _open_ref(ref, archive) as source:
                while source.read(_CHUNK):
                    pass
            budget.verified.add(ref.member)


def _fail(bundle: ProblemBundle, exc: BaseException) -> None:
    if isinstance(exc, TestdataError):
        bundle.fail(notice("testdata_invalid", reason=str(exc)))
    else:
        bundle.fail(notice("testdata_invalid", reason="压缩数据已损坏"))


_BUILD_ERRORS = (TestdataError,) + CORRUPT_ERRORS


class BuiltTestdata:
    """构建好的数据包：``file`` 从头可读（写 blob 用），用完 ``close()``。

    普通题是写好的临时文件；native 的内层包直接是归档成员的流（解析时已核对过 sha256 与结构），
    不再落一份临时文件。"""

    def __init__(
        self,
        file: IO[bytes],
        summary: Dict[str, object],
        max_bytes: int,
        sha256: Optional[str] = None,
    ) -> None:
        self.file = file
        self.summary = summary
        #: 写 blob 时的上限（``BlobStore.put_stream`` 的 ``max_bytes``）
        self.max_bytes = max_bytes
        #: 已知的 sha256（native 内层包）；写 blob 时核对
        self.sha256 = sha256

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> "BuiltTestdata":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _raw_ready(bundle: ProblemBundle, archive: Archive) -> bool:
    """native 内层包在解析时已校验过（有摘要与 sha256），可以直接用归档成员。"""
    ref = bundle.raw_testdata
    return (
        ref is not None and ref.member is not None and archive is not None
        and bundle.raw_summary is not None and bundle.raw_sha256 is not None
    )


def try_build(
    bundle: ProblemBundle,
    archive: Archive,
    budget: Optional[BuildBudget] = None,
    tmp_dir: Optional[Union[str, Path]] = None,
) -> Optional[BuiltTestdata]:
    """构建数据包（写进 ``tmp_dir`` 下的临时文件，不进内存）并取摘要。

    失败时把 ``testdata_invalid`` 记进 ``bundle.errors`` 并返回 None。返回值由调用方 ``close()``。
    """
    if _raw_ready(bundle, archive):
        ref = bundle.raw_testdata
        assert ref is not None and archive is not None
        try:
            if budget is not None:
                budget.charge(_ref_size(ref, archive))
            stream = _open_ref(ref, archive)
        except TestdataError as exc:
            _fail(bundle, exc)
            return None
        return BuiltTestdata(stream, dict(bundle.raw_summary or {}), MAX_TESTDATA_BYTES, bundle.raw_sha256)

    # 用真正的临时文件：SpooledTemporaryFile 在 3.11 之前没有 seekable，zipfile 读它会 AttributeError
    out = tempfile.TemporaryFile(mode="w+b", dir=None if tmp_dir is None else str(tmp_dir))
    try:
        summary = write_testdata(bundle, archive, out, budget)
        size = out.seek(0, io.SEEK_END)
        out.seek(0)
    except _BUILD_ERRORS as exc:
        out.close()
        _fail(bundle, exc)
        return None
    except BaseException:
        out.close()
        raise
    return BuiltTestdata(out, summary, max(int(size), 1))


def try_summarize(
    bundle: ProblemBundle, archive: Archive, budget: Optional[BuildBudget] = None
) -> Optional[Dict[str, object]]:
    """预览用：做与构建相同的检查并返回摘要，**不**写出完整数据包。

    测试点写成空文件的"骨架包"回读自检；测试数据只按成员各解压一遍查损坏。
    失败时记 ``testdata_invalid`` 并返回 None。
    """
    budget = budget if budget is not None else BuildBudget()
    if bundle.raw_testdata is not None:
        if _raw_ready(bundle, archive):
            try:
                budget.charge(_ref_size(bundle.raw_testdata, archive))
            except TestdataError as exc:
                _fail(bundle, exc)
                return None
            return dict(bundle.raw_summary or {})
        built = try_build(bundle, archive, budget)
        if built is None:
            return None
        with built:
            return built.summary
    try:
        flat, config = _plan(bundle, archive, budget)
        _verify_members(bundle, archive, budget)
        skeleton = io.BytesIO()
        _write_zip(bundle, archive, flat, config, skeleton, skeleton=True)
        skeleton.seek(0)
        return _self_check(bundle, flat, skeleton)
    except _BUILD_ERRORS as exc:
        _fail(bundle, exc)
        return None
