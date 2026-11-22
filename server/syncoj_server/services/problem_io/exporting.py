"""导出侧的公共件：试运行用的空 zip、按题目读出数据包结构。

导出函数统一签名::

    export(problems: List[ExportProblem], open_blob: OpenBlob, zf) -> Dict[int, List[Notice]]

* ``zf`` 是 ``zipfile.ZipFile``（写模式），或预览时的 :class:`NullZip`（``zf.dry_run`` 为真，
  写进去的东西直接丢掉 —— 可以据此跳过读大文件）。只用 ``writestr`` 与 ``open(name, "w")``。
* 返回 ``{problem.id: [警告…]}``，每道传进来的题都要有一项（没有警告就是空列表）。
* 题目已由框架筛过：非 native 格式收到的题都有数据包。
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager
from typing import IO, Callable, Iterator, Tuple

from ...judging.testdata import Testdata, parse_testdata
from .bundle import ExportProblem

__all__ = ["OpenBlob", "NullZip", "open_testdata"]

#: sha256 -> 只读二进制文件对象（调用方负责关闭）
OpenBlob = Callable[[str], IO[bytes]]


class _Discard(io.RawIOBase):
    def writable(self) -> bool:
        return True

    def write(self, data) -> int:  # type: ignore[override]
        return len(data)


class NullZip:
    """预览用：接口同写模式的 ``ZipFile``，内容全部丢弃，只记下写了哪些名字。"""

    dry_run = True

    def __init__(self) -> None:
        self.names = []  # type: list

    def writestr(self, name, data, *args, **kwargs) -> None:
        self.names.append(name if isinstance(name, str) else name.filename)

    def open(self, name, mode: str = "r", *args, **kwargs) -> IO[bytes]:
        if mode != "w":
            raise ValueError("NullZip 只能写")
        self.names.append(name if isinstance(name, str) else name.filename)
        return _Discard()  # type: ignore[return-value]


@contextmanager
def open_testdata(problem: ExportProblem, open_blob: OpenBlob) -> Iterator[Tuple[zipfile.ZipFile, Testdata]]:
    """打开一道题的数据包：``with open_testdata(p, open_blob) as (zf, testdata): ...``。

    ``testdata.cases[i].input`` / ``.answer`` 是 ``zf`` 里的成员名；``testdata.checker`` 与
    ``checker_support`` 同理。结构与评测机看到的完全一致（同一个 ``parse_testdata``）。
    """
    if not problem.testdata_sha256:
        raise ValueError("题目 %s 没有数据包" % problem.ident)
    handle = open_blob(problem.testdata_sha256)
    try:
        with zipfile.ZipFile(handle) as zf:
            yield zf, parse_testdata(zf)
    finally:
        handle.close()
