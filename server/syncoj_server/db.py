"""数据库引擎与连接管理。

SQLite 调优要点
---------------
* ``journal_mode=WAL``：读写互不阻塞 —— 这是"Web 查询的同时 Agent 在写"能成立的
  前提。默认的 ``delete`` 模式会让读操作直接撞上写锁。
* ``synchronous=NORMAL``：WAL 下的安全折中（掉电最多丢最后一个事务，不会损坏库）。
* ``busy_timeout=5000``：写锁竞争时自动重试 5 秒，而不是立刻抛
  ``database is locked``。50 台机器的写入量远低于这个阈值。
* ``foreign_keys=ON``：SQLite 默认 **关闭** 外键约束，必须显式打开，否则
  ``ondelete="CASCADE"`` 形同虚设。

写入策略
--------
本模块**不做**连接池级别的写串行化。理由：真正的高频写路径（心跳状态）已经被
``registry`` + 定时批量落库收敛成"每 5 秒一个事务"，剩余的写（上传、注册、
管理操作）是低频的。WAL + busy_timeout 足够。若将来单场次超过 500 台，再引入
单一写线程 + 队列。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .migrations import migrate

__all__ = ["Database"]

log = logging.getLogger(__name__)


class Database:
    """引擎 + 会话工厂的轻量封装。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()

        self.engine: Engine = create_engine(
            settings.database_url,
            future=True,
            echo=False,
            # Agent 的请求由线程池处理，连接会跨线程复用
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        _install_pragmas(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def create_all(self) -> None:
        """把库升到最新结构。

        **不要**直接叫 ``Base.metadata.create_all`` —— 它只建缺失的表，
        看不见"已有表少了一列"。加字段必须走 ``migrations``，
        否则老部署升级后会以一个很难定位的 500 收场。
        """
        applied = migrate(self.engine)
        for line in applied:
            log.info("数据库迁移已应用：%s", line)

    def dispose(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """短生命周期会话。退出时自动 commit，异常时 rollback。

        每次调用都新建会话 —— 会话不是线程安全的，跨请求复用会串数据。
        """
        sess = self.session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    def healthcheck(self) -> bool:
        from sqlalchemy import text

        try:
            with self.session() as sess:
                sess.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # pragma: no cover
            log.warning("数据库健康检查失败: %s", exc)
            return False


def _install_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()


def resolve_settings(settings: Optional[Settings] = None) -> Settings:
    if settings is None:
        from .config import default_settings

        return default_settings()
    return settings
