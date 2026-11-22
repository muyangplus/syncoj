"""评测机分发（§12.10）：评测机程序与一键安装脚本。

**不鉴权**，但**只在管理端口上**（公开端口的闸门挡掉 ``/judge/`` 前缀）：脚本与程序里
没有秘密，令牌由教师在命令行里给；而公开端口是考生网段随手能打开的地址，没理由把
评测机程序摆在那里。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response

from ..context import AppContext
from ..services import judge_dist
from .deps import get_ctx

router = APIRouter(prefix="/judge", tags=["judge"])


def _cache_root(ctx: AppContext):
    return ctx.settings.data_root / "cache" / "judge-worker"


def _server(request: Request, ctx: AppContext) -> str:
    return judge_dist.base_url(request.headers.get("host"), request.url.scheme, ctx.settings.http_port)


@router.get("/worker.pyz", response_class=FileResponse)
def worker_pyz(ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    """评测机程序（zipapp）：``python3 syncoj-judge.pyz --server … --token-file …``。"""
    path = judge_dist.worker_pyz(_cache_root(ctx))
    return FileResponse(str(path), media_type="application/zip", filename="syncoj-judge.pyz")


def _script(request: Request, ctx: AppContext, name: str, media_type: str) -> Response:
    pyz = judge_dist.worker_pyz(_cache_root(ctx))
    body = judge_dist.render_script(name, _server(request, ctx), judge_dist.sha256_of(pyz))
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": 'attachment; filename="%s"' % name},
    )


@router.get("/install.sh", response_class=Response)
def install_sh(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    """Linux 一键安装：``curl -fsSL <地址>/judge/install.sh | sudo bash -s -- <令牌>``。"""
    return _script(request, ctx, "install.sh", "text/x-sh; charset=utf-8")


@router.get("/install.ps1", response_class=Response)
def install_ps1(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    """Windows 一键安装（PowerShell，UTF-8 BOM）：下载到文件后 ``& $p -Token '<令牌>'``。"""
    return _script(request, ctx, "install.ps1", "text/plain; charset=utf-8")
