"""HOJ 格式（格式 id ``hoj``）。

根目录 ``X.json`` + 同名数据目录 ``X/``（每题一对）。

**待实现**：现在只是占位，调用即报 ``format_unimplemented``。实现时只改本文件与
``tests/test_problem_io_hoj.py``（夹具在 ``tests/problem_io_fixtures.py`` 的 ``make_hoj_*``）；
注册表、识别、接口、前端都已接好。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .archive import ArchiveView
from .bundle import ExportProblem, ImportResult, Notice
from .exporting import OpenBlob
from .notices import ProblemIoError

__all__ = ["LABEL", "parse", "export"]

LABEL = "HOJ"


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="hoj")``。"""
    raise ProblemIoError("format_unimplemented", action="导入", format=LABEL)


def export(problems: List[ExportProblem], open_blob: OpenBlob, zf: Any) -> Dict[int, List[Notice]]:
    """把 ``problems``（都有数据包）写进 ``zf``，返回每题的警告。"""
    raise ProblemIoError("format_unimplemented", action="导出", format=LABEL)
