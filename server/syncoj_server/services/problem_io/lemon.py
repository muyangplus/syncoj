"""LemonLime 格式（格式 id ``lemonlime``）。

``*.cdf``（JSON，version "1.0"）+ ``data/<pid>/``；``testCases`` 里的路径相对 .cdf 所在目录的 ``data/``。
``standardInputCheck=false`` 为文件 IO；时限 ms、内存 MB。二进制 .cdf（``detect.CDF_MAGIC`` 开头）报 ``cdf_binary``，
缺 ``data/`` 报 ``cdf_without_data``。

**待实现**：现在只是占位，调用即报 ``format_unimplemented``。实现时只改本文件与
``tests/test_problem_io_lemon.py``（夹具在 ``tests/problem_io_fixtures.py`` 的 ``make_lemon_*``）；
注册表、识别、接口、前端都已接好。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .archive import ArchiveView
from .bundle import ExportProblem, ImportResult, Notice
from .exporting import OpenBlob
from .notices import ProblemIoError

__all__ = ["LABEL", "parse", "export"]

LABEL = "LemonLime"


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="lemonlime")``。"""
    raise ProblemIoError("format_unimplemented", action="导入", format=LABEL)


def export(problems: List[ExportProblem], open_blob: OpenBlob, zf: Any) -> Dict[int, List[Notice]]:
    """把 ``problems``（都有数据包）写进 ``zf``，返回每题的警告。"""
    raise ProblemIoError("format_unimplemented", action="导出", format=LABEL)
