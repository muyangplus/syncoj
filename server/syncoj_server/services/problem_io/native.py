"""SyncOJ 原生格式 ``syncoj-judge/1``（有非传统题、比较方式或自动 IO 时写 ``/2``）：无损导出与导入。

::

    syncoj-problems.json
    <ident>/testdata.zip          # 原样的 blob 字节（sha256 不变）

清单里每道题的列按原值写出（``file_patterns`` 是库里的原值，None = 默认模式）；
导入时原样写回，内层 zip 的 sha256 必须与清单一致。多道题指向同一个内层包时按 path 只校验一遍；
构建时直接流式读归档成员写 blob（``build.try_build``），不整包读进内存。

版本 2 多了 ``problem_type`` / ``compare_mode`` 两列与 ``io_mode: auto``（§12.13、§12.16）；只有
用到它们时才写 2，旧服务端读到 2 会 ``version_unsupported`` 拒收，而不是悄悄当传统题导入。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from typing import IO, Any, Dict, List, Optional

from ...judging.testdata import TestdataError, parse_testdata
from .archive import CORRUPT_ERRORS, ArchiveView, load_json
from .build import MAX_TESTDATA_BYTES
from .bundle import ExportProblem, FileRef, ImportResult, Notice, ProblemBundle
from .exporting import OpenBlob
from .notices import ProblemIoError, notice

__all__ = ["MANIFEST", "FORMAT_NAME", "VERSION", "parse", "export"]

MANIFEST = "syncoj-problems.json"
FORMAT_NAME = "syncoj-judge"
VERSION = 2
#: 只有传统题、默认比较方式时仍写 1（旧服务端能读）
BASE_VERSION = 1
PROBLEM_TYPES = ("traditional", "grader", "answer_only", "interactive")
COMPARE_MODES = ("strict", "trailing_spaces", "trailing_newlines", "both")


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _entry(problem: ExportProblem) -> Dict[str, Any]:
    return {
        "ident": problem.ident,
        "title": problem.title,
        "order_index": problem.order_index,
        "note": problem.note,
        "file_patterns": problem.file_patterns,
        "judge_mode": problem.judge_mode,
        "time_limit_ms": problem.time_limit_ms,
        "memory_limit_mb": problem.memory_limit_mb,
        "io_mode": problem.io_mode,
        "input_file": problem.input_file,
        "output_file": problem.output_file,
        "compile_flags": problem.compile_flags,
        "problem_type": problem.problem_type,
        "compare_mode": problem.compare_mode,
        "testdata": None,
    }


def _needs_v2(problem: ExportProblem) -> bool:
    return problem.problem_type != "traditional" or problem.compare_mode != "both" or problem.io_mode == "auto"


def export(problems: List[ExportProblem], open_blob: OpenBlob, zf: Any) -> Dict[int, List[Notice]]:
    notices: Dict[int, List[Notice]] = {}
    entries = []
    dry_run = getattr(zf, "dry_run", False)
    for problem in problems:
        notices[problem.id] = []
        entry = _entry(problem)
        if problem.testdata_sha256:
            path = "%s/testdata.zip" % problem.ident
            try:
                if dry_run:
                    open_blob(problem.testdata_sha256).close()
                else:
                    with open_blob(problem.testdata_sha256) as source, zf.open(path, "w") as target:
                        shutil.copyfileobj(source, target, 256 * 1024)
            except FileNotFoundError:
                notices[problem.id].append(notice("missing_file", path=path))
            else:
                entry["testdata"] = {
                    "path": path,
                    "name": problem.testdata_name or "testdata.zip",
                    "sha256": problem.testdata_sha256,
                    "size": int(problem.testdata_size or 0),
                }
        entries.append(entry)
    version = VERSION if any(_needs_v2(problem) for problem in problems) else BASE_VERSION
    manifest = {"format": FORMAT_NAME, "version": version, "problems": entries}
    zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    return notices


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _opt_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _load_manifest(view: ArchiveView) -> List[Any]:
    try:
        data = load_json(view.read(MANIFEST))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProblemIoError("bad_manifest", path=MANIFEST, reason=str(exc)[:200])
    except KeyError:
        raise ProblemIoError("bad_manifest", path=MANIFEST, reason="文件不存在")
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        raise ProblemIoError("bad_manifest", path=MANIFEST, reason="format 不是 %s" % FORMAT_NAME)
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProblemIoError("bad_manifest", path=MANIFEST, reason="缺少 version")
    if version > VERSION:
        raise ProblemIoError("version_unsupported", path=MANIFEST, version=version)
    problems = data.get("problems")
    if not isinstance(problems, list):
        raise ProblemIoError("bad_manifest", path=MANIFEST, reason="problems 不是数组")
    return problems


class _Inner:
    """一个内层 zip 的校验结果（按 path 缓存：多题指向同一个包时只读、哈希、解析一遍）。

    ``error`` 是读取 / 结构上的错误（与清单里的 sha256 无关）；``sha256`` 读完整包才有。"""

    __slots__ = ("sha256", "summary", "error")

    def __init__(self) -> None:
        self.sha256: Optional[str] = None
        self.summary: Optional[Dict[str, object]] = None
        self.error: Optional[Notice] = None


def _verify_inner(view: ArchiveView, path: str, spool: IO[bytes], problem_type: str = "traditional") -> _Inner:
    """把内层 zip 流式读进 ``spool``、算 sha256、过 ``parse_testdata``。"""
    inner = _Inner()
    digest = hashlib.sha256()
    size = 0
    with view.open(path) as source:
        while True:
            chunk = source.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TESTDATA_BYTES:
                inner.error = notice("testdata_invalid", reason="超过 512 MB")
                return inner
            digest.update(chunk)
            spool.write(chunk)
    inner.sha256 = digest.hexdigest()
    spool.seek(0)
    try:
        with zipfile.ZipFile(spool) as archive:
            inner.summary = parse_testdata(archive, problem_type).summary()
    except zipfile.BadZipFile:
        inner.error = notice("testdata_invalid", reason="不是合法的 zip")
    except TestdataError as exc:
        inner.error = notice("testdata_invalid", reason=str(exc))
    return inner


def _inspect(view: ArchiveView, path: str, problem_type: str = "traditional") -> _Inner:
    # 用真正的临时文件：SpooledTemporaryFile 在 3.11 之前没有 seekable，zipfile 读它会 AttributeError
    spool = tempfile.TemporaryFile(mode="w+b")
    try:
        return _verify_inner(view, path, spool, problem_type)
    except CORRUPT_ERRORS:
        inner = _Inner()
        inner.error = notice("testdata_invalid", reason="压缩数据已损坏")
        return inner
    finally:
        spool.close()


def _check_testdata(view: ArchiveView, bundle: ProblemBundle, meta: Any, cache: Dict[str, _Inner]) -> None:
    if not isinstance(meta, dict) or not isinstance(meta.get("path"), str):
        bundle.fail(notice("bad_manifest", path=MANIFEST, reason="testdata 缺少 path"))
        return
    path = meta["path"]
    if not view.exists(path):
        bundle.fail(notice("missing_file", path=path))
        return
    key = "%s\0%s" % (path, bundle.problem_type)
    inner = cache.get(key)
    if inner is None:
        inner = cache[key] = _inspect(view, path, bundle.problem_type)
    if inner.sha256 is not None and inner.sha256 != str(meta.get("sha256") or "").lower():
        bundle.fail(notice("sha_mismatch", path=path))
        return
    if inner.error is not None:
        bundle.fail(inner.error)
        return
    bundle.raw_testdata = FileRef(member=view.full(path))
    bundle.raw_sha256 = inner.sha256
    bundle.raw_summary = dict(inner.summary or {})
    bundle.testdata_name = clean_testdata_name(meta.get("name"))


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def clean_testdata_name(value: Any) -> Optional[str]:
    """清单里的 ``testdata.name``：只留基本名、去掉控制字符（CR/LF 会让下载接口的
    Content-Disposition 发不出去）、截到 255 字符。剩下为空 → None（apply 时用 ``<ident>.zip``）。"""
    if not isinstance(value, str):
        return None
    name = _CONTROL_CHARS.sub("", value).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255] or None


def parse(view: ArchiveView) -> ImportResult:
    result = ImportResult(format="syncoj")
    checked: Dict[str, _Inner] = {}
    for index, item in enumerate(_load_manifest(view)):
        key = "problems[%d]" % index
        if not isinstance(item, dict):
            bundle = ProblemBundle(key=key, source_name=key, ident="")
            bundle.fail(notice("bad_manifest", path=MANIFEST, reason="%s 不是对象" % key))
            result.problems.append(bundle)
            continue
        ident = _opt_str(item.get("ident")) or ""
        patterns = item.get("file_patterns")
        bundle = ProblemBundle(
            key=key,
            source_name=ident,
            ident=ident,
            title=_opt_str(item.get("title")),
            time_limit_ms=_opt_int(item.get("time_limit_ms")),
            memory_limit_mb=_opt_int(item.get("memory_limit_mb")),
            io_mode=item.get("io_mode") if item.get("io_mode") in ("file", "auto") else "stdio",
            input_file=_opt_str(item.get("input_file")),
            output_file=_opt_str(item.get("output_file")),
            compile_flags=_opt_str(item.get("compile_flags")),
            order_index=_opt_int(item.get("order_index")),
            note=_opt_str(item.get("note")),
            file_patterns=(
                [p for p in patterns if isinstance(p, str)] if isinstance(patterns, list) else None
            ),
            judge_mode="builtin" if item.get("judge_mode") == "builtin" else "external",
            problem_type=item.get("problem_type") if item.get("problem_type") in PROBLEM_TYPES else "traditional",
            compare_mode=item.get("compare_mode") if item.get("compare_mode") in COMPARE_MODES else None,
        )
        if item.get("testdata") is not None:
            _check_testdata(view, bundle, item["testdata"], checked)
        result.problems.append(bundle)
    return result
