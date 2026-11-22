"""应用级共享对象的容器。

用显式容器而不是一堆模块级全局变量：测试时可以构造独立的 AppContext 指向
临时目录，互不干扰。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings
from .db import Database
from .registry import AgentRegistry
from .services.diagnostics import DiagnosticsRateLimiter
from .services.paircodes import PairCodeCache
from .services.ratelimit import RateLimiter
from .services.server_id import load_or_create_server_uuid
from .services.signing import DerError, SigningKey, load_signing_key
from .services.uninstall import PendingUninstalls
from .storage import BlobStore

__all__ = ["AppContext"]

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    registry: AgentRegistry
    blobs: BlobStore
    #: 发布签名私钥。为 None 时服务端不提供任何升级（安全默认值）
    signing_key: Optional[SigningKey] = None
    #: 加载私钥时的错误，用于在健康检查里明示而不是静默失败
    signing_key_error: Optional[str] = None
    #: 按 IP 限速。统一密钥把 /agent/enroll 变成"一把钥匙开整间机房"，
    #: 必须有东西挡住无限注册与爆破
    enroll_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(limit=0))
    #: 全局注册限速 —— 挡住"从很多 IP 一起刷"。考场内网 IP 数量有限，
    #: 全局上限才是真正的兜底
    enroll_global_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(limit=0))
    #: 管理端登录、考生页登录：按 IP、每分钟计
    login_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(limit=0))
    player_lookup_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(limit=0))
    #: 未配对机器的配对码明文。**只在内存里** —— 协议要求每一轮 tick 都带上
    #: 当前有效的码，而库里只存哈希（见 ``services/paircodes.py``）
    pair_codes: PairCodeCache = field(default_factory=PairCodeCache)
    #: 诊断包的按机器限速（60 秒一份）。只在内存里：它回答的是"刚才是不是
    #: 刚收过一台机器的诊断包"，重启后重置的代价只是可能多收一份。
    diagnostics_limiter: DiagnosticsRateLimiter = field(
        default_factory=DiagnosticsRateLimiter
    )
    #: "教师点过卸载"的机器。**只在内存里**：落库等于把一枚能在那台机器上换一次
    #: root 删除的凭据留在 ``syncoj.db`` 里（见 ``services/uninstall.py``）
    pending_uninstalls: PendingUninstalls = field(default_factory=PendingUninstalls)
    #: 教师浏览器最近一次打开管理界面用的地址（``http://10.0.0.5:8000``）。
    #:
    #: 服务端**没法可靠地算出**自己的对外地址（枚举网卡会在多网卡机器上挑错那
    #: 一块），但"有人从这个地址成功访问过"是一条真实证据 —— 内嵌进离线包和
    #: 广播应答都用它。``None`` = 还没人从非回环地址访问过，此时什么都别做，
    #: 尤其不要退回 ``127.0.0.1``（那会让 50 台机器各自找自己）。
    public_url_hint: Optional[str] = None
    #: 服务端主动通告的状态（``services.announce.Announcer``）。延迟 import：
    #: announce → discovery → context 会成环。
    announcer: Any = field(default_factory=lambda: _new_announcer())
    #: 挑战应答端点按 IP 限速（不鉴权、每次一个 RSA 签名）
    whoami_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(limit=0))
    #: 服务端实例 ID（``<data_root>/server-id``，见 ``services/server_id.py``）。空串 =
    #: 读写失败，tick / 发现 / 挑战应答都按不带它的旧格式回
    server_uuid: str = ""
    #: 考生页登录令牌的 HMAC 密钥。第一次用到时从 ``<data_root>/player-token.key``
    #: 读出（没有就生成），见 ``services/player_tokens.py``
    player_token_secret: Optional[bytes] = None
    #: 本机评测机子进程（§12.10，``services.local_judge.LocalJudge``）。只在内存里：
    #: 服务端退出时一并停掉，重启后不自动拉起（教师再点一次「启动」）
    local_judge: Any = field(default_factory=lambda: _new_local_judge())

    @classmethod
    def create(cls, settings: Settings) -> "AppContext":
        settings.ensure_dirs()
        context = cls(
            settings=settings,
            db=Database(settings),
            registry=AgentRegistry(offline_after_seconds=settings.offline_after_seconds),
            blobs=BlobStore(settings.blob_root),
            enroll_limiter=RateLimiter(limit=settings.enroll_per_ip_per_second),
            enroll_global_limiter=RateLimiter(limit=settings.enroll_global_per_second),
            login_limiter=RateLimiter(limit=settings.login_per_ip_per_minute, window=60.0),
            player_lookup_limiter=RateLimiter(
                limit=settings.player_lookup_per_ip_per_minute, window=60.0
            ),
            whoami_limiter=RateLimiter(limit=settings.whoami_per_ip_per_second),
        )
        context.load_signing_key()
        context.server_uuid = load_or_create_server_uuid(settings.data_root)
        return context

    def load_signing_key(self) -> None:
        """载入发布签名私钥。

        失败**不阻止启动** —— 签名只影响自更新这一个功能，不该因为运维忘了配
        私钥就让整个考试系统起不来。但错误会被记住并在 /healthz 里暴露出来。
        """
        path = self.settings.release_signing_key
        if path is None:
            self.signing_key = None
            self.signing_key_error = None
            log.info("未配置发布签名私钥，自更新功能关闭")
            return
        try:
            self.signing_key = load_signing_key(path)
            self.signing_key_error = None
            log.info(
                "已载入发布签名私钥（%d 位，key_id=%s）",
                self.signing_key.bits, self.signing_key.key_id,
            )
        except (DerError, OSError) as exc:
            self.signing_key = None
            self.signing_key_error = str(exc)
            log.error("发布签名私钥载入失败，自更新功能不可用: %s", exc)


def _new_announcer():
    from .services.announce import Announcer

    return Announcer()


def _new_local_judge():
    from .services.local_judge import LocalJudge

    return LocalJudge()
