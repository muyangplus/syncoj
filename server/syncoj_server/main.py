"""FastAPI 应用装配。

刻意**不在模块级创建 app**：``create_app()`` 会创建数据目录，若在 import 时执行，
测试与 CLI 子命令都会在磁盘上留下意料之外的目录。uvicorn 用工厂模式启动：

    uvicorn syncoj_server.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, errors
from .api import admin as admin_api
from .api import agent as agent_api
from .config import Settings, default_settings
from .context import AppContext
from .paths import PathValidationError
from .services import discovery
from .services.discovery import run_discovery_responder
from .tasks.flush import run_maintenance_loop
from .tasks.judge import run_judge_scan_loop

__all__ = ["create_app"]

log = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or default_settings()
    ctx = AppContext.create(settings)
    ctx.db.create_all()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("SyncOJ 启动，数据目录 %s", settings.data_root)
        background = [
            asyncio.ensure_future(run_maintenance_loop(ctx)),
            asyncio.ensure_future(run_judge_scan_loop(ctx)),
            asyncio.ensure_future(run_discovery_responder(ctx)),
        ]
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            ctx.db.dispose()
            log.info("SyncOJ 已停止")

    app = FastAPI(
        title="SyncOJ",
        description="通用编程考试与竞赛模拟的在线同步评测系统",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    # 必须在注册路由**之前**装：异常处理器是查表用的，晚装不影响已注册的路由，
    # 但早装能让 /docs 与 OpenAPI 也带上统一的错误响应文档
    errors.install_error_handlers(app)

    app.include_router(agent_api.router)
    app.include_router(admin_api.router)

    _remember_public_url(app, ctx)

    @app.exception_handler(PathValidationError)
    async def _path_error(_request: Request, exc: PathValidationError) -> JSONResponse:
        return errors.error_response(400, "path_invalid", "路径不合法：%s" % exc)

    @app.get("/healthz", tags=["meta"])
    async def healthz(request: Request) -> dict:
        live_ctx: AppContext = request.app.state.ctx
        agents = live_ctx.registry.all()
        return {
            "ok": live_ctx.db.healthcheck(),
            "version": __version__,
            "agents_total": len(agents),
            "agents_online": sum(1 for a in agents if a.online),
            "web_ui": _frontend_enabled(settings),
            # 这台服务端认为自己对考试机是什么地址。装机现场第一件要确认的事，
            # 而它取决于"有没有人从局域网打开过界面"，只有服务端自己知道。
            "public_url": discovery.describe_advertised_url(ctx),
        }

    _mount_frontend(app, settings)
    return app


def _remember_public_url(app: FastAPI, ctx: AppContext) -> None:
    """把"教师是从哪个地址打开界面的"记下来，当作这台服务端对外地址的**次选**。

    为什么从请求里学：服务端**没有办法可靠地知道自己的对外地址**。枚举网卡会
    在多网卡机器上挑错那一块；而反过来看，教师既然能用一个地址打开界面，那个
    地址就是**从局域网真的可达**的。

    但它只是次选，因为它来自 ``Host`` 头 —— **客户端可以随便写**。所以：

    * **回环**（``127.0.0.1`` / ``localhost``）永远不记。把它当成对外地址是这里
      最坏的一种错：50 台机器会各自连自己，现场只有"注册不上"，没有任何线索。
    * **只记成功的请求**（``< 400``）。少了这一条，局域网里任何人都能发一个
      ``Host: 1.2.3.4:9`` 的匿名请求把这里污染掉 —— 之后所有新打的包都会内嵌
      那个地址、发现应答也会报它，等于让外人替我们挑服务器地址。
      （更可信的那一路是"从来源 IP 反推本机地址"，它由内核给出，伪造不了，
      在 :func:`discovery.describe_advertised_url` 里排在前面。）
    * 只认管理端与探活路径：Agent 的 Host 也可能到达，但那不能当权威 ——
      手工填错的地址只会被拒，不会成为真相。

    只记最后一个，不做多数投票：教师在考场上换一次网线（比如从 Wi-Fi 换到
    有线）之后，**新的那个**才是对的。
    """
    observed_prefixes = ("/api/v1/admin", "/healthz")

    @app.middleware("http")
    async def _capture_host(request: Request, call_next):
        response = await call_next(request)
        if response.status_code < 400 and request.url.path.startswith(observed_prefixes):
            host = (request.headers.get("host") or "").strip()
            if host and not _is_loopback_host(host):
                scheme = (request.headers.get("x-forwarded-proto") or "http").split(",")[0].strip()
                ctx.public_url_hint = "%s://%s" % (scheme or "http", host)
        return response


def _is_loopback_host(host: str) -> bool:
    """``host`` 是不是本机自环（含端口）。"""
    name = host.rsplit(":", 1)[0].strip("[]").lower()
    return name in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or name.startswith("127.")


# --------------------------------------------------------------------------- #
# 管理界面
# --------------------------------------------------------------------------- #

#: 这些前缀下的路径由 API 自己处理，绝不能被 SPA 兜底吞掉。
#: 不加这道排除的话，打错一个接口路径会返回一段 HTML，排查起来很费劲。
_API_PREFIXES = ("api/", "docs", "redoc", "openapi.json", "healthz")


def _frontend_enabled(settings: Settings) -> bool:
    dist = settings.web_dist
    return bool(dist and (dist / "index.html").is_file())


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """托管前端产物并做 SPA 兜底。

    顺序很重要：**必须先注册 API 路由再挂兜底路由**。FastAPI 按注册顺序匹配，
    所以 /api/* 永远轮不到兜底处理。

    产物不存在时什么都不做 —— 前端没构建不该让服务端起不来，此时 /docs
    仍然可以交互。
    """
    dist = settings.web_dist
    if not _frontend_enabled(settings) or dist is None:
        log.info("未找到前端产物，仅提供 API（/docs 可交互）")
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index_file = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith(_API_PREFIXES):
            # 是 API 路径却没被任何路由处理 = 真的不存在，别拿 HTML 糊弄调用方
            raise HTTPException(status_code=404, detail="Not Found")
        # 其余一律交给前端路由（history 模式需要这样兜底）
        return FileResponse(index_file)

    log.info("管理界面已挂载: %s", dist)
