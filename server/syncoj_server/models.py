"""SQLAlchemy 数据模型。

时间约定
--------
全部时间戳以 **naive UTC** 存储（``datetime.now(timezone.utc).replace(tzinfo=None)``）。

原因是 SQLite 没有原生时间类型，SQLAlchemy 会把它写成字符串；若混用带时区与不带
时区的值，字符串排序与比较会失真。统一成 naive UTC 后，SQLite 的字典序即时间序，
``ORDER BY`` 与范围查询都正确。API 序列化时再补回 ``timezone.utc``。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

__all__ = [
    "Base",
    "utcnow",
    "Admin",
    "AdminSession",
    "Contest",
    "Player",
    "EnrollCode",
    "Agent",
    "AgentStatus",
    "SourceFile",
    "Asset",
    "DeployTask",
    "DeployTarget",
    "JudgeRun",
    "EventLog",
    "AgentRelease",
    "ContestStatus",
    "DeployStatus",
]

Base = declarative_base()


def utcnow() -> datetime:
    """当前 UTC 时间，刨掉 tzinfo（见模块 docstring）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContestStatus:
    DRAFT = "draft"
    RUNNING = "running"
    FROZEN = "frozen"
    CLOSED = "closed"

    ALL = (DRAFT, RUNNING, FROZEN, CLOSED)


class DeployStatus:
    PENDING = "pending"
    READY = "ready"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = (PENDING, READY, DONE, FAILED, CANCELLED)


class TimestampMixin:
    created_at = Column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# 管理员
# --------------------------------------------------------------------------- #


class Admin(Base, TimestampMixin):
    __tablename__ = "admin"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime, nullable=True)

    sessions = relationship("AdminSession", back_populates="admin", cascade="all, delete-orphan")


class AdminSession(Base):
    __tablename__ = "admin_session"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, ForeignKey("admin.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)

    admin = relationship("Admin", back_populates="sessions")


# --------------------------------------------------------------------------- #
# 场次与选手
# --------------------------------------------------------------------------- #


class Contest(Base, TimestampMixin):
    __tablename__ = "contest"

    id = Column(Integer, primary_key=True)
    slug = Column(String(64), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    status = Column(String(16), nullable=False, default=ContestStatus.DRAFT)
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    players = relationship("Player", back_populates="contest", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_contest_status", "status"),)


class Player(Base, TimestampMixin):
    __tablename__ = "player"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    player_no = Column(String(64), nullable=False)
    name = Column(String(64), nullable=True)
    seat = Column(String(32), nullable=True)
    group_name = Column(String(64), nullable=True)

    contest = relationship("Contest", back_populates="players")
    agents = relationship("Agent", back_populates="player", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("contest_id", "player_no", name="uq_player_contest_no"),
        Index("ix_player_contest", "contest_id"),
    )


class EnrollCode(Base, TimestampMixin):
    """注册码。

    刻意 **不是一次性** 的：NOI Linux 考试机常做整机快照还原，机器上的凭据会消失，
    Agent 需要用镜像内置的注册码重新 enroll 自愈。因此注册码是绑定
    ``player_no``（经 ``player_id``）与 ``machine_id`` 的 "机器凭据种子"，长期有效。
    """

    __tablename__ = "enroll_code"

    id = Column(Integer, primary_key=True)
    code_hash = Column(String(64), nullable=False, unique=True)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    # 为 NULL 表示尚未绑定机器；首次 enroll 时写入，之后只接受同一 machine_id
    machine_id = Column(String(128), nullable=True)
    note = Column(String(200), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    player = relationship("Player")

    __table_args__ = (Index("ix_enroll_player", "player_id"),)


# --------------------------------------------------------------------------- #
# Agent 与在线状态
# --------------------------------------------------------------------------- #


class Agent(Base, TimestampMixin):
    __tablename__ = "agent"

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    machine_id = Column(String(128), nullable=False)
    hostname = Column(String(128), nullable=True)
    os_info = Column(String(200), nullable=True)
    agent_version = Column(String(32), nullable=True)
    #: 首次注册时间
    enrolled_at = Column(DateTime, default=utcnow, nullable=False)
    #: 最近一次注册/换发凭据时间。快照还原后 Agent 会重新 enroll，这里会更新
    last_enrolled_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    player = relationship("Player", back_populates="agents")

    __table_args__ = (
        UniqueConstraint("player_id", "machine_id", name="uq_agent_player_machine"),
        Index("ix_agent_machine", "machine_id"),
    )


class AgentStatus(Base):
    """在线状态的**落库快照**。

    这是个派生表：实时真身在 ``registry.AgentRegistry`` 的内存 dict 里，
    由后台任务每 ``flush_interval_seconds`` 秒合并成一个事务刷进来。
    存在的意义是服务端重启后 Web 不会看到空白，以及支持历史回溯。
    """

    __tablename__ = "agent_status"

    agent_id = Column(Integer, ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True)
    online = Column(Boolean, nullable=False, default=False)
    last_tick_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(64), nullable=True)
    agent_version = Column(String(32), nullable=True)
    scan_root = Column(String(512), nullable=True)
    file_count = Column(Integer, nullable=False, default=0)
    disk_free = Column(BigInteger, nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# 代码回收台账
# --------------------------------------------------------------------------- #


class SourceFile(Base):
    """回收到的文件的最新版本。

    ``revision`` 递增，用于丢弃"迟到的旧版本"：Agent 断网重传时可能把旧内容
    送到，服务端比对 revision 后直接丢弃，避免覆盖更新的版本。
    """

    __tablename__ = "source_file"

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    rel_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size = Column(BigInteger, nullable=False, default=0)
    mtime = Column(BigInteger, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=0)
    first_seen_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=utcnow, nullable=False)
    # 服务端已收到内容（blobs 中有该 sha256）才置 True
    content_stored = Column(Boolean, nullable=False, default=False)
    # 非 NULL 表示该文件已从扫描结果中消失（选手删除/改名）
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("player_id", "rel_path", name="uq_source_player_path"),
        Index("ix_source_sha", "player_id", "sha256"),
    )


# --------------------------------------------------------------------------- #
# 文件下发
# --------------------------------------------------------------------------- #


class Asset(Base, TimestampMixin):
    """内容寻址的下发文件。相同内容只存一份。"""

    __tablename__ = "asset"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size = Column(BigInteger, nullable=False, default=0)
    filename = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False, default="testdata")

    __table_args__ = (
        UniqueConstraint("contest_id", "sha256", "filename", name="uq_asset_contest_sha_name"),
    )


class DeployTask(Base, TimestampMixin):
    __tablename__ = "deploy_task"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    asset_id = Column(Integer, ForeignKey("asset.id", ondelete="CASCADE"), nullable=False)
    # "all" | "player" | "group"
    target_kind = Column(String(16), nullable=False, default="all")
    target_desc = Column(String(255), nullable=True)
    dest_dir = Column(String(512), nullable=False)
    # "overwrite" | "skip_exist"
    mode = Column(String(16), nullable=False, default="overwrite")
    status = Column(String(16), nullable=False, default=DeployStatus.PENDING)

    asset = relationship("Asset")
    targets = relationship("DeployTarget", back_populates="task", cascade="all, delete-orphan")


class DeployTarget(Base):
    __tablename__ = "deploy_target"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("deploy_task.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(16), nullable=False, default=DeployStatus.PENDING)
    # 服务端记录已传字节数，用于重连后下发 offset 提示
    bytes_done = Column(BigInteger, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)

    task = relationship("DeployTask", back_populates="targets")

    __table_args__ = (
        UniqueConstraint("task_id", "player_id", name="uq_deploy_task_player"),
    )


# --------------------------------------------------------------------------- #
# 成绩与审计
# --------------------------------------------------------------------------- #


class JudgeRun(Base):
    """一条评测成绩。

    ``parse_status`` 是刻意存在的：我们无法覆盖所有评测器的输出格式，遇到看不懂
    的文件时**必须留下痕迹**，让教师在界面上看到"这个文件没解析出来"并手动补录。
    静默丢弃会让人以为"还没跑评测"，静默猜一个分数更糟 —— 教师会拿它当真。
    """

    __tablename__ = "judge_run"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    problem = Column(String(64), nullable=False)
    score = Column(Integer, nullable=True)
    max_score = Column(Integer, nullable=True)
    status = Column(String(32), nullable=True)
    #: ok = 解析成功 | unparsed = 有文件但看不懂 | manual = 教师手工录入
    parse_status = Column(String(16), nullable=False, default="ok")
    #: 解析来源文件（相对 judge_result 根目录）
    source_path = Column(String(1024), nullable=True)
    #: 来源文件的 mtime，用于跳过未变化的文件
    source_mtime = Column(BigInteger, nullable=False, default=0)
    #: 解析器名称 / 未解析原因
    detail = Column(String(512), nullable=True)
    raw_json = Column(Text, nullable=True)
    scanned_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("contest_id", "player_id", "problem", name="uq_judge_run"),
        Index("ix_judge_contest", "contest_id"),
    )


class EventLog(Base):
    """审计事件。只增不改，是"仅完整性校验 + 审计日志"这一档防作弊的全部依据。"""

    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=utcnow, nullable=False)
    level = Column(String(16), nullable=False, default="info")
    category = Column(String(32), nullable=False)
    contest_id = Column(Integer, nullable=True)
    player_id = Column(Integer, nullable=True)
    agent_id = Column(Integer, nullable=True)
    message = Column(Text, nullable=False)
    meta_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_event_ts", "ts"),
        Index("ix_event_contest_ts", "contest_id", "ts"),
    )


class AgentRelease(Base, TimestampMixin):
    """Agent 升级包。签名校验通过才允许下发。"""

    __tablename__ = "agent_release"

    id = Column(Integer, primary_key=True)
    version = Column(String(32), nullable=False, unique=True)
    channel = Column(String(16), nullable=False, default="stable")
    sha256 = Column(String(64), nullable=False)
    signature = Column(Text, nullable=False)
    notes = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True)
    yanked_at = Column(DateTime, nullable=True)
