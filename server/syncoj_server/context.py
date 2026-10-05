"""应用级共享对象的容器。

用显式容器而不是一堆模块级全局变量：测试时可以构造独立的 AppContext 指向
临时目录，互不干扰。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import Database
from .registry import AgentRegistry
from .storage import BlobStore

__all__ = ["AppContext"]


@dataclass
class AppContext:
    settings: Settings
    db: Database
    registry: AgentRegistry
    blobs: BlobStore

    @classmethod
    def create(cls, settings: Settings) -> "AppContext":
        settings.ensure_dirs()
        return cls(
            settings=settings,
            db=Database(settings),
            registry=AgentRegistry(offline_after_seconds=settings.offline_after_seconds),
            blobs=BlobStore(settings.blob_root),
        )
