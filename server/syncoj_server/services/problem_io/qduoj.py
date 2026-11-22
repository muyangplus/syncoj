"""QDUOJ 格式（格式 id ``qduoj``）。

``<n>/problem.json`` + ``<n>/testcase/``。只导入，不导出。

**待实现**：现在只是占位，调用即报 ``format_unimplemented``。实现时只改本文件与
``tests/test_problem_io_qduoj.py``（夹具在 ``tests/problem_io_fixtures.py`` 的 ``make_qduoj_*``）；
注册表、识别、接口、前端都已接好。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .archive import ArchiveView
from .bundle import ExportProblem, ImportResult, Notice
from .exporting import OpenBlob
from .notices import ProblemIoError

__all__ = ["LABEL", "parse"]

LABEL = "QDUOJ"


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="qduoj")``。"""
    raise ProblemIoError("format_unimplemented", action="导入", format=LABEL)
