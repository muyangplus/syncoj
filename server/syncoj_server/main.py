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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .api import admin as admin_api
from .api import agent as agent_api
from .config import Settings, default_settings
from .context import AppContext
from .paths import PathValidationError
from .tasks.flush import run_maintenance_loop

__all__ = ["create_app"]

log = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or default_settings()
    ctx = AppContext.create(settings)
    ctx.db.create_all()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("SyncOJ 启动，数据目录 %s", settings.data_root)
        maintenance = asyncio.ensure_future(run_maintenance_loop(ctx))
        try:
            yield
        finally:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
            ctx.db.dispose()
            log.info("SyncOJ 已停止")

    app = FastAPI(
        title="SyncOJ",
        description="通用编程考试与竞赛模拟的在线同步评测系统",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    app.include_router(agent_api.router)
    app.include_router(admin_api.router)

    @app.exception_handler(PathValidationError)
    async def _path_error(_request: Request, exc: PathValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": "路径不合法: %s" % exc})

    @app.get("/healthz", tags=["meta"])
    async def healthz(request: Request) -> dict:
        live_ctx: AppContext = request.app.state.ctx
        agents = live_ctx.registry.all()
        return {
            "ok": live_ctx.db.healthcheck(),
            "version": __version__,
            "agents_total": len(agents),
            "agents_online": sum(1 for a in agents if a.online),
        }

    return app
