"""FastAPI 应用装配。

刻意**不在模块级创建 app**：``create_app()`` 会创建数据目录，若在 import 时执行，
测试与 CLI 子命令都会在磁盘上留下意料之外的目录。uvicorn 用工厂模式启动：

    uvicorn syncoj_server.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, errors
from .api import admin as admin_api
from .api import agent as agent_api
from .api import meta as meta_api
from .api import player as player_api
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
    # 选手页是**免登录**的第三个命名空间（见 api/player.py 的说明）。
    # 它不放进 /agent：Agent 那侧每个端点都要求 Bearer 凭据，混在一起
    # 迟早有人顺手给这一页也挂上 require_agent —— 那就等于把入口关掉了。
    app.include_router(player_api.router)
    # 全站元信息（显示时区…）。同样免登录：选手页与装机页都不登录，
    # 而它们都要按同一个钟点显示时间。
    app.include_router(meta_api.router)
    app.include_router(admin_api.router)

    _install_public_port_gate(app)
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
                new_hint = "%s://%s" % (scheme or "http", host)
                if new_hint != ctx.public_url_hint:
                    ctx.public_url_hint = new_hint
                    # **把算出来的地址直接打出来**：现场第一个问题永远是"这台服务端
                    # 认为自己是哪个地址"，而它只在第一次从局域网打开界面之后才有值。
                    # 顺带印出来源 —— 来源决定了这个地址有多可信，也决定了要不要改用
                    # SYNCOJ_PUBLIC_URL 固定下来。变了才打，避免每翻一页刷一行。
                    url, source = discovery.advertised_url_with_source(
                        ctx, request.client.host if request.client else None
                    )
                    log.info("对外地址: %s（来源：%s）", url or "还没有", source)
        return response


def _is_loopback_host(host: str) -> bool:
    """``host`` 是不是本机自环（含端口）。"""
    name = host.rsplit(":", 1)[0].strip("[]").lower()
    return name in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or name.startswith("127.")


# --------------------------------------------------------------------------- #
# 公开端口（默认 80）
# --------------------------------------------------------------------------- #

#: 公开端口上**不该存在**的命名空间。
#:
#: 这里刻意用"挡住管理端"的黑名单，而不是"只放行考生页与装机页"的白名单：
#: 白名单漏一条的后果是**免登录的那两页悄悄坏掉** —— 现场看到的是一段 HTML、
#: 一个 404 或者一个转不完的圈，而没人会想到是端口门禁干的；黑名单漏一条的
#: 后果则只是多暴露一个接口，而那些接口自己都还有 ``require_admin`` 兜着。
#: 两侧都失败时，宁可失败在"多一道鉴权门"那一侧。
#:
#: ``/docs`` 与 ``/openapi.json`` 一起挡住：它们**不需要登录**就能列出全部管理
#: 接口，放在考生随手就能打开的地址上没有意义，只是白送一张接口地图。
_PUBLIC_PORT_HIDDEN = (
    "/api/v1/admin",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/healthz",
)

def _public_mode_script(app: FastAPI) -> str:
    """注入给前端的那一句：这一份 index.html 只提供考生页与装机页。

    为什么由**服务端**注入、而不是让前端自己数端口：前端只能从 ``location.port``
    猜，而那个数字要跟服务端实际绑上的端口对得上，等于把同一个事实定义两遍。
    端口 80 在浏览器里是省略的（``location.port === ""``），这个坑更是躲不掉。

    ``adminPort`` 是给装机命令用的：命令里那个地址会被写进那台机器、从此长期
    使用，所以它必须指向 **API 端口**，而不是教师恰好打开的这一页所在的端口
    （公开端口是个"方便用的"端口，可以关掉）。见 ``web/src/publicMode.ts``。
    """
    payload: dict = {"publicOnly": True}
    admin_port = getattr(app.state, "admin_port", None)
    if admin_port:
        payload["adminPort"] = admin_port
    # separators 去掉空格：这句话在 HTML 里越短越好，也便于人肉核对
    return "<script>window.__SYNCOJ__=%s</script>" % json.dumps(
        payload, separators=(",", ":")
    )


def _public_port_of(request: Request) -> Optional[int]:
    """这个请求进的是公开端口吗？是的话返回那个端口号，否则 ``None``。

    **判据是"真的在这个端口上听着"，不是配置里写着的数字。** 两件事必须分开：
    :data:`Settings.public_port` 说的是"想开在哪"，而 ``serve`` 在绑不上 80 时会
    降级（见 ``cli._cmd_serve``）。降级之后 80 上没有任何监听，也就不该有任何
    请求被当成公开端口 —— 否则一台绑不上特权端口的服务端会把自己的管理界面
    一起挡掉，而现场只会看到"管理界面 404"。

    于是这个值由 ``serve`` 在启动时写进 ``app.state.public_port``，没写过就是
    ``None``（测试、``--factory`` 直接起、都不是公开端口）。
    """
    active = getattr(request.app.state, "public_port", None)
    if not active:
        return None
    return active if request.scope.get("server", ("", 0))[1] == active else None


def _install_public_port_gate(app: FastAPI) -> None:
    """在公开端口上挡掉管理端。

    用中间件而不是给每条路由加依赖：需要挡的是一个**前缀**（现在和以后所有
    管理接口），逐条挂依赖迟早会漏掉新加的那条 —— 而漏掉的那条正是没人记得
    去挂的那条。
    """

    @app.middleware("http")
    async def _hide_admin_on_public_port(request: Request, call_next):
        if _public_port_of(request) and request.url.path.startswith(_PUBLIC_PORT_HIDDEN):
            # 回 404 而不是 403：这个端口上**没有**这个接口，不是"你不配"。
            # 后者等于告诉你"管理接口在别处，去另一个端口试试"。
            return errors.error_response(404, "not_found", "Not Found")
        return await call_next(request)


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
    async def spa_fallback(full_path: str, request: Request) -> Response:
        if full_path.startswith(_API_PREFIXES):
            # 是 API 路径却没被任何路由处理 = 真的不存在，别拿 HTML 糊弄调用方
            raise HTTPException(status_code=404, detail="Not Found")
        if _public_port_of(request):
            return Response(
                content=_public_index_html(index_file, _public_mode_script(request.app)),
                media_type="text/html",
                # 页面本身可以缓存，但**必须每次回源校验**：这个 HTML 里写着
                # 构建产物的 asset 文件名，前端一旦重新构建，缓存里的旧名字
                # 会指向一个不存在的文件 —— 表现是白屏。
                headers={"Cache-Control": "no-cache"},
            )
        # 其余一律交给前端路由（history 模式需要这样兜底）。
        #
        # 这份 HTML 也要**每次回源校验**，理由与公开端口那份一样：它里面写着构建产物
        # 的 asset 文件名（`index-<hash>.js`），前端重新构建之后，浏览器缓存里的旧
        # HTML 会指向一个**已经不存在的**旧 chunk。真机上的表现不是白屏，而是"界面
        # 看着正常、但新加的功能一个都没有" —— 因为那一对 HTML/JS 是自洽的旧版本，
        # 于是人会以为是功能没做，而不是缓存没刷新。
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})

    log.info("管理界面已挂载: %s", dist)


def _public_index_html(index_file: Path, flag: str) -> str:
    """读 ``index.html``，在 ``</head>`` 前注入 ``flag``（见 :func:`_public_mode_script`）。

    每次请求都读盘：这个文件只有一两 KB，而缓存下来会在"前端重新构建之后
    服务端还端着旧 HTML"这件事上咬人 —— 那时页面会去加载一个已经不存在的
    asset 文件名，表现是白屏，而且重启服务端就好了，最难查的那一类。
    """
    html = index_file.read_text(encoding="utf-8")
    for marker in ("</head>", "</body>"):
        if marker in html:
            return html.replace(marker, flag + marker, 1)
    # 兜底：**追加到末尾，绝不前插**。任何内容出现在 <!doctype html> 之前都会让
    # 浏览器进 quirks 模式（页面布局莫名其妙地变），而插在末尾一样够用 ——
    # 这句是内联脚本、立刻执行，而应用自己的 <script type="module"> 是延迟执行的。
    return html + flag
