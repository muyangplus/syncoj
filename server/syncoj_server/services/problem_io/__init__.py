"""题目评测配置的导入 / 导出。

入口：

* :func:`parse_archive` —— 打开上传的包、识别格式、交给格式模块解析，再跑一遍公共规整
  （:func:`finalize`：ident 建议与去重、时限内存夹取、IO 文件名、编译参数白名单、分值补足）。
* :func:`export_problems` —— 按格式把题目写成 zip（或预览时试运行，只收警告）。

格式注册表 :data:`FORMATS`：每种格式一个模块，模块只需提供

* ``parse(view: ArchiveView) -> ImportResult``（整包级失败抛 ``ProblemIoError``，单题问题记进
  ``bundle.errors`` / ``bundle.warnings``）
* ``export(problems, open_blob, zf) -> Dict[int, List[Notice]]``（只有能导出的格式）

识别规则在 :mod:`.detect`，数据包写出在 :mod:`.build`。
"""

from __future__ import annotations

import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Optional, Union

from . import fps, hoj, hydro, lemon, native, qduoj
from .archive import ArchiveView, SafeArchive, SingleFileArchive, open_upload
from .bundle import ExportProblem, ImportResult, Notice, ProblemBundle
from .detect import detect
from .exporting import NullZip, OpenBlob
from .idents import io_name_error, suggest_ident, valid_ident
from .notices import ProblemIoError, notice

__all__ = [
    "FormatSpec",
    "FORMATS",
    "IMPORT_FORMATS",
    "EXPORT_FORMATS",
    "MAX_PROBLEMS",
    "ParsedImport",
    "parse_archive",
    "finalize",
    "ExportOutcome",
    "export_problems",
    "ProblemIoError",
]

#: 一次导入最多多少道题（与 ``POST /contests/{id}/problems`` 一致）
MAX_PROBLEMS = 200
TIME_RANGE = (100, 60000)
MEMORY_RANGE = (16, 4096)


@dataclass(frozen=True)
class FormatSpec:
    id: str
    label: str
    parse: Callable[[ArchiveView], ImportResult]
    export: Optional[Callable[[List[ExportProblem], OpenBlob, Any], Dict[int, List[Notice]]]] = None


#: 格式 id → 实现。id 与接口 ``format`` 参数、``detect`` 的返回值一致。
FORMATS: Dict[str, FormatSpec] = {
    "syncoj": FormatSpec("syncoj", "SyncOJ", native.parse, native.export),
    "hoj": FormatSpec("hoj", hoj.LABEL, hoj.parse, hoj.export),
    "lemonlime": FormatSpec("lemonlime", lemon.LABEL, lemon.parse, lemon.export),
    "hydro": FormatSpec("hydro", hydro.LABEL, hydro.parse, hydro.export),
    "qduoj": FormatSpec("qduoj", qduoj.LABEL, qduoj.parse),
    "fps": FormatSpec("fps", fps.LABEL, fps.parse),
}
IMPORT_FORMATS = tuple(FORMATS)
EXPORT_FORMATS = tuple(key for key, spec in FORMATS.items() if spec.export is not None)


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #


@dataclass
class ParsedImport:
    """解析结果 + 仍打开着的归档（``build_testdata_zip`` 要从里面读）。用完 ``close()``。"""

    result: ImportResult
    archive: Union[SafeArchive, SingleFileArchive]

    def close(self) -> None:
        self.archive.close()

    def __enter__(self) -> "ParsedImport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def parse_archive(path: Union[str, Path], fmt: str = "auto", filename: str = "") -> ParsedImport:
    """打开 ``path``（暂存的上传文件），识别并解析。整包级失败抛 :class:`ProblemIoError`。"""
    if fmt != "auto" and fmt not in FORMATS:
        raise ProblemIoError("unknown_format")
    archive = open_upload(path, filename)
    try:
        format_id, view = detect(archive, fmt)
        result = FORMATS[format_id].parse(view)
        result.format = format_id
        finalize(result)
    except Exception:
        archive.close()
        raise
    return ParsedImport(result=result, archive=archive)


def _clamp(value: Optional[int], bounds: "tuple") -> Optional[int]:
    if value is None:
        return None
    return min(max(int(value), bounds[0]), bounds[1])


def _flags(bundle: ProblemBundle) -> None:
    from ...api.admin_judge import _FLAG_RE  # 接口层是白名单的唯一定义处

    if not bundle.compile_flags or not bundle.compile_flags.strip():
        bundle.compile_flags = None
        return
    tokens = bundle.compile_flags.split()
    good = [token for token in tokens if _FLAG_RE.match(token)]
    bad = [token for token in tokens if not _FLAG_RE.match(token)]
    if bad:
        bundle.warn(notice("flags_dropped", flags=" ".join(bad)))
    bundle.compile_flags = " ".join(good)[:200] or None


def _io_names(bundle: ProblemBundle) -> None:
    if bundle.io_mode != "file":
        bundle.io_mode = "stdio"
        bundle.input_file = bundle.output_file = None
        return
    for attr in ("input_file", "output_file"):
        value = getattr(bundle, attr)
        value = value.strip() if isinstance(value, str) else None
        if value and io_name_error(value):
            bundle.fail(notice("io_name_invalid", name=value))
        setattr(bundle, attr, value or None)
    if bundle.ident:
        if bundle.input_file == "%s.in" % bundle.ident:
            bundle.input_file = None
        if bundle.output_file == "%s.out" % bundle.ident:
            bundle.output_file = None
    effective_in = bundle.input_file or "%s.in" % bundle.ident
    effective_out = bundle.output_file or "%s.out" % bundle.ident
    if effective_in == effective_out:
        bundle.fail(notice("io_name_invalid", name=effective_in))


def _fill_scores(bundle: ProblemBundle) -> None:
    """全部分值为 0 / 缺失：按 100 分平分到"计分单元"（子任务 + 不在子任务里的点）。"""
    if not bundle.cases:
        return
    in_subtask = {name for sub in bundle.subtasks for name in sub.cases}
    free = [case for case in bundle.cases if case.name not in in_subtask]
    if any(sub.score for sub in bundle.subtasks) or any(case.score for case in free):
        return
    units: List[Any] = list(bundle.subtasks) + free
    base, remainder = divmod(100, len(units))
    for index, unit in enumerate(units):
        unit.score = base + (1 if index >= len(units) - remainder else 0)
    for case in bundle.cases:
        if case.name in in_subtask:
            case.score = 0
    bundle.warn(notice("scores_missing"))


def _native_columns(bundle: ProblemBundle) -> None:
    from .. import matching

    if bundle.file_patterns:
        try:
            bundle.file_patterns = matching.validate_patterns(bundle.file_patterns) or None
        except matching.PatternError:
            bundle.file_patterns = None
            bundle.warn(notice("file_patterns_dropped"))
    if bundle.title:
        bundle.title = bundle.title[:200]
    if bundle.note:
        bundle.note = bundle.note[:2000]
    if bundle.order_index is not None:
        bundle.order_index = min(max(bundle.order_index, 0), 9999)


def finalize(result: ImportResult) -> None:
    """公共规整，原地修改。原生格式保留 ident 与各列原值，只校验。"""
    if not result.problems:
        raise ProblemIoError("no_problems")
    if len(result.problems) > MAX_PROBLEMS:
        raise ProblemIoError("too_many_problems", limit=MAX_PROBLEMS)
    is_native = result.format == "syncoj"
    taken: set = set()
    for index, bundle in enumerate(result.problems, 1):
        if is_native:
            if not valid_ident(bundle.ident):
                bundle.fail(notice("ident_invalid", ident=bundle.ident or "（空）"))
            elif bundle.ident in taken:
                bundle.fail(notice("ident_invalid", ident=bundle.ident))
            taken.add(bundle.ident)
            _native_columns(bundle)
        else:
            raw = (bundle.ident or bundle.source_name or "").strip()
            bundle.ident, renamed = suggest_ident(raw, taken, index)
            if renamed and raw:
                bundle.warn(notice("ident_renamed", source=raw, ident=bundle.ident))
            bundle.judge_mode = "builtin"
            _fill_scores(bundle)
        time_limit = _clamp(bundle.time_limit_ms, TIME_RANGE)
        memory_limit = _clamp(bundle.memory_limit_mb, MEMORY_RANGE)
        if time_limit != bundle.time_limit_ms or memory_limit != bundle.memory_limit_mb:
            bundle.warn(notice("limit_clamped", time=time_limit or 1000, memory=memory_limit or 256))
        bundle.time_limit_ms, bundle.memory_limit_mb = time_limit, memory_limit
        _io_names(bundle)
        _flags(bundle)
        if not is_native and not bundle.cases and not bundle.errors:
            bundle.fail(notice("no_cases"))


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


@dataclass
class ExportOutcome:
    #: 写好的 zip（已 seek 到开头）；试运行时为 None
    spool: Optional[IO[bytes]]
    #: 每道传入题目的警告
    notices: Dict[int, List[Notice]] = field(default_factory=dict)
    #: 实际写进包里的题目 id
    included: List[int] = field(default_factory=list)


def export_problems(
    problems: List[ExportProblem], fmt: str, open_blob: OpenBlob, dry_run: bool = False
) -> ExportOutcome:
    """把题目导出成 ``fmt`` 格式。非原生格式跳过没有数据包的题（``no_testdata``）。"""
    spec = FORMATS.get(fmt)
    if spec is None or spec.export is None:
        raise ProblemIoError("format_unimplemented", action="导出", format=spec.label if spec else fmt)
    notices: Dict[int, List[Notice]] = {problem.id: [] for problem in problems}
    if fmt == "syncoj":
        included = list(problems)
    else:
        included = []
        for problem in problems:
            if problem.testdata_sha256:
                included.append(problem)
            else:
                notices[problem.id].append(notice("no_testdata"))
    if not included:
        raise ProblemIoError("no_exportable")

    spool: Optional[IO[bytes]] = None
    try:
        if dry_run:
            extra = spec.export(included, open_blob, NullZip())
        else:
            spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
            with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                extra = spec.export(included, open_blob, zf)
            spool.seek(0)
    except Exception:
        if spool is not None:
            spool.close()
        raise
    for problem_id, items in extra.items():
        notices.setdefault(problem_id, []).extend(items)
    return ExportOutcome(spool=spool, notices=notices, included=[p.id for p in included])
