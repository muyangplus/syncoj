"""把 :class:`ProblemBundle` 写成现有格式的数据包，并立即用 ``parse_testdata`` 回读自检。

写出的包::

    <name>.in / <name>.ans     # name 规整成 [A-Za-z0-9._-]，冲突加 -2、-3
    config.json                # cases（name + score）与 subtasks
    checker.cpp + 支持文件      # 有 checker 时；用了 testlib 而缺 testlib.h 时补上

native 有 ``raw_testdata`` 时原样返回内层 zip 的字节（sha256 不变），同样过一遍校验。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from typing import Dict, List, Optional, Tuple, Union

from ...judging.testdata import MAX_UNCOMPRESSED_BYTES, TestdataError, parse_testdata
from .archive import CORRUPT_ERRORS, MAX_DESCRIPTOR_BYTES, SafeArchive, SingleFileArchive, human_size
from .bundle import FileRef, ProblemBundle
from .checkers import inject_testlib
from .notices import ProblemIoError, notice

__all__ = [
    "MAX_TESTDATA_BYTES",
    "flat_case_names",
    "read_ref",
    "build_testdata_zip",
    "summarize",
    "try_build",
]

#: 与 ``POST /problems/{id}/testdata`` 的上传上限一致
MAX_TESTDATA_BYTES = 512 * 1024 * 1024
#: 写出的数据包解压后总量上限（与 ``parse_testdata`` 一致）。同一个成员被多个测试点引用时
#: 每次引用都算一次 —— 写之前先累计，超了就不写
MAX_BUILD_BYTES = MAX_UNCOMPRESSED_BYTES

Archive = Union[SafeArchive, SingleFileArchive, None]

_FLAT_BAD = re.compile(r"[^A-Za-z0-9._-]")


def _flat(name: str) -> str:
    text = _FLAT_BAD.sub("_", name.replace("/", "_")).strip(".") or "case"
    return text[:120]


def flat_case_names(names: List[str]) -> Dict[str, str]:
    """来源测试点名 → 扁平名。冲突（不分大小写）时加 ``-2``、``-3``。"""
    out: Dict[str, str] = {}
    used = set()
    for name in names:
        base = _flat(name)
        candidate = base
        serial = 2
        while candidate.lower() in used:
            candidate = "%s-%d" % (base, serial)
            serial += 1
        used.add(candidate.lower())
        out[name] = candidate
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


def _copy_ref(ref: FileRef, archive: Archive, zf: zipfile.ZipFile, arcname: str) -> None:
    if ref.data is not None:
        zf.writestr(arcname, ref.data)
        return
    if archive is None:
        raise TestdataError("缺少归档")
    try:
        source = archive.open(ref.member)  # type: ignore[arg-type]
    except KeyError:
        raise TestdataError("缺少文件 %s" % ref.member)
    with source, zf.open(arcname, "w") as target:
        shutil.copyfileobj(source, target, 256 * 1024)


def _ref_size(ref: FileRef, archive: Archive) -> int:
    if ref.data is not None:
        return len(ref.data)
    if archive is None:
        raise TestdataError("缺少归档")
    try:
        return archive.size(ref.member)  # type: ignore[arg-type]
    except KeyError:
        raise TestdataError("缺少文件 %s" % ref.member)


def _check_volume(bundle: ProblemBundle, archive: Archive) -> None:
    """按引用（含重复引用）累计测试数据的解压量，超过 :data:`MAX_BUILD_BYTES` 就拒绝。"""
    total = 0
    for case in bundle.cases:
        total += _ref_size(case.input, archive) + _ref_size(case.answer, archive)
        if total > MAX_BUILD_BYTES:
            raise TestdataError("解压后超过 %s" % human_size(MAX_BUILD_BYTES))


def build_testdata_zip(bundle: ProblemBundle, archive: Archive) -> bytes:
    """写出数据包字节。结构不对抛 ``TestdataError``（信息给人看）。

    会往 ``bundle.warnings`` 里加 ``testlib_injected`` / ``testlib_missing``。
    """
    if bundle.raw_testdata is not None:
        data = read_ref(bundle.raw_testdata, archive, MAX_TESTDATA_BYTES)
        summarize(data)
        return data
    if not bundle.cases:
        raise TestdataError("没有测试点")

    flat = flat_case_names([case.name for case in bundle.cases])
    if len(flat) != len(bundle.cases):
        raise TestdataError("测试点名重复")
    _check_volume(bundle, archive)
    config: Dict[str, object] = {
        "cases": [{"name": flat[case.name], "score": int(case.score)} for case in bundle.cases],
    }
    if bundle.subtasks:
        subtasks = []
        for sub in bundle.subtasks:
            unknown = [name for name in sub.cases if name not in flat]
            if unknown:
                raise TestdataError("子任务里有不存在的测试点：%s" % ", ".join(unknown[:5]))
            subtasks.append({"score": int(sub.score), "cases": [flat[name] for name in sub.cases]})
        config["subtasks"] = subtasks

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for case in bundle.cases:
            name = flat[case.name]
            _copy_ref(case.input, archive, zf, name + ".in")
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
    data = buffer.getvalue()
    _self_check(bundle, flat, data)
    return data


def summarize(data: bytes) -> Dict[str, object]:
    """数据包摘要（与上传时写进 ``testdata_info`` 的一样）。不合法抛 ``TestdataError``。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return parse_testdata(zf).summary()
    except zipfile.BadZipFile:
        raise TestdataError("不是合法的 zip")


def _self_check(bundle: ProblemBundle, flat: Dict[str, str], data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parsed = parse_testdata(zf)
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


def try_build(bundle: ProblemBundle, archive: Archive) -> Optional[Tuple[bytes, Dict[str, object]]]:
    """构建并取摘要；失败时把 ``testdata_invalid`` 记进 ``bundle.errors`` 并返回 None。"""
    try:
        data = build_testdata_zip(bundle, archive)
        return data, summarize(data)
    except TestdataError as exc:
        bundle.fail(notice("testdata_invalid", reason=str(exc)))
        return None
    except CORRUPT_ERRORS:
        bundle.fail(notice("testdata_invalid", reason="压缩数据已损坏"))
        return None
