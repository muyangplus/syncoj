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
from .services.signing import DerError, SigningKey, load_signing_key
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

    @classmethod
    def create(cls, settings: Settings) -> "AppContext":
        settings.ensure_dirs()
        context = cls(
            settings=settings,
            db=Database(settings),
            registry=AgentRegistry(offline_after_seconds=settings.offline_after_seconds),
            blobs=BlobStore(settings.blob_root),
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
