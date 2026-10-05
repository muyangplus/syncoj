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
        }

    _mount_frontend(app, settings)
    return app


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
