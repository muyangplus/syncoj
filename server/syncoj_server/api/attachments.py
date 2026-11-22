"""把内容寻址的 blob 当附件流出去：管理端与考生页共用的两件工具。

单独一个模块而不是让考生页去 import ``admin`` 的私有名：考生页是免登录的，
"它依赖管理端模块"这件事本身就会让下一个读代码的人以为两边共享鉴权。
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import quote

from fastapi.responses import StreamingResponse

from ..context import AppContext

__all__ = ["attachment_header", "blob_response"]


def attachment_header(filename: str, fallback: str = "download") -> str:
    """附件头。用 RFC 5987 的 filename* 传中文名；filename 那个 ASCII 版本是给老浏览器兜底。

    名字可能来自上传的文件名或导入包的清单：控制字符（CR/LF 等）换成 ``_``，
    否则 h11 / httptools 拒发这个头，客户端只看到连接被断。
    """
    filename = "".join("_" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch for ch in filename) or fallback
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name.replace('"', "_").replace("\\", "_"),
        quote(filename, safe=""),
    )


def blob_response(
    ctx: AppContext, sha256: str, download_name: str, fallback: str = "download"
) -> StreamingResponse:
    """把 blob 作为附件流出去。

    流式而不是整体读进内存：``max_file_size`` 是可配的，默认 2MB 但有人会调大，
    而"下载一个几百 MB 的文件把服务端内存打满"是完全没必要的失败模式。
    """
    handle = ctx.blobs.open(sha256)
    size = ctx.blobs.path_for(sha256).stat().st_size

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": attachment_header(download_name, fallback=fallback),
            "Content-Length": str(size),
        },
    )
