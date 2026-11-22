"""FPS 格式（格式 id ``fps``）。

单个 FPS XML（直接上传或 zip 里只有一个 .xml）；``view.names[0]`` 是那个文件。先拒 ``<!DOCTYPE`` /
``<!ENTITY``（``xml_unsafe``），再 ``ElementTree.fromstring``。测试数据内嵌，用 ``FileRef(data=...)``。只导入，不导出。

**待实现**：现在只是占位，调用即报 ``format_unimplemented``。实现时只改本文件与
``tests/test_problem_io_fps.py``（夹具在 ``tests/problem_io_fixtures.py`` 的 ``make_fps_*``）；
注册表、识别、接口、前端都已接好。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .archive import ArchiveView
from .bundle import ExportProblem, ImportResult, Notice
from .exporting import OpenBlob
from .notices import ProblemIoError

__all__ = ["LABEL", "parse"]

LABEL = "FPS"


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="fps")``。"""
    raise ProblemIoError("format_unimplemented", action="导入", format=LABEL)
