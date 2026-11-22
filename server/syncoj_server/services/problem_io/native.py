"""SyncOJ 原生格式 ``syncoj-judge/1``：无损导出与导入。

::

    syncoj-problems.json
    <ident>/testdata.zip          # 原样的 blob 字节（sha256 不变）

清单里每道题的列按原值写出（``file_patterns`` 是库里的原值，None = 默认模式）；
导入时原样写回，内层 zip 的 sha256 必须与清单一致。
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
VERSION = 1


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
        "testdata": None,
    }


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
    manifest = {"format": FORMAT_NAME, "version": VERSION, "problems": entries}
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


def _verify_inner(
    view: ArchiveView, bundle: ProblemBundle, path: str, meta: Dict[str, Any], spool: IO[bytes]
) -> bool:
    """把内层 zip 读进 ``spool``、核对 sha256、过 ``parse_testdata``。不通过时记错误并返回 False。"""
    digest = hashlib.sha256()
    size = 0
    with view.open(path) as source:
        while True:
            chunk = source.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TESTDATA_BYTES:
                bundle.fail(notice("testdata_invalid", reason="超过 512 MB"))
                return False
            digest.update(chunk)
            spool.write(chunk)
    if digest.hexdigest() != str(meta.get("sha256") or "").lower():
        bundle.fail(notice("sha_mismatch", path=path))
        return False
    spool.seek(0)
    try:
        with zipfile.ZipFile(spool) as inner:
            parse_testdata(inner)
    except zipfile.BadZipFile:
        bundle.fail(notice("testdata_invalid", reason="不是合法的 zip"))
        return False
    except TestdataError as exc:
        bundle.fail(notice("testdata_invalid", reason=str(exc)))
        return False
    return True


def _check_testdata(view: ArchiveView, bundle: ProblemBundle, meta: Any) -> None:
    if not isinstance(meta, dict) or not isinstance(meta.get("path"), str):
        bundle.fail(notice("bad_manifest", path=MANIFEST, reason="testdata 缺少 path"))
        return
    path = meta["path"]
    if not view.exists(path):
        bundle.fail(notice("missing_file", path=path))
        return
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    try:
        ok = _verify_inner(view, bundle, path, meta, spool)
    except CORRUPT_ERRORS:
        bundle.fail(notice("testdata_invalid", reason="压缩数据已损坏"))
        ok = False
    finally:
        spool.close()
    if not ok:
        return
    bundle.raw_testdata = FileRef(member=view.full(path))
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
            io_mode="file" if item.get("io_mode") == "file" else "stdio",
            input_file=_opt_str(item.get("input_file")),
            output_file=_opt_str(item.get("output_file")),
            compile_flags=_opt_str(item.get("compile_flags")),
            order_index=_opt_int(item.get("order_index")),
            note=_opt_str(item.get("note")),
            file_patterns=(
                [p for p in patterns if isinstance(p, str)] if isinstance(patterns, list) else None
            ),
            judge_mode="builtin" if item.get("judge_mode") == "builtin" else "external",
        )
        if item.get("testdata") is not None:
            _check_testdata(view, bundle, item["testdata"])
        result.problems.append(bundle)
    return result
