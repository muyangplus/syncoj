"""应用级共享对象的容器。

用显式容器而不是一堆模块级全局变量：测试时可以构造独立的 AppContext 指向
临时目录，互不干扰。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import Settings
from .db import Database
from .registry import AgentRegistry
from .services.paircodes import PairCodeCache
from .services.ratelimit import RateLimiter
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
    #: 未配对机器的配对码明文。**只在内存里** —— 协议要求每一轮 tick 都带上
    #: 当前有效的码，而库里只存哈希（见 ``services/paircodes.py``）
    pair_codes: PairCodeCache = field(default_factory=PairCodeCache)
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
        )
        context.load_signing_key()
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
