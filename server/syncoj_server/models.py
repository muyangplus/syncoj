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
    "Problem",
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


class EnrollmentMode:
    """场次的注册方式。

    ``PER_PLAYER_CODE`` 是原有行为：一个选手一个注册码，绑定
    ``player_no + machine_id``，长期有效。

    ``BOOTSTRAP`` 是镜像统一密钥：整间机房一份密钥（root 只读）换回每机凭据，
    注册出来的机器**没有归属**，靠短码配对认领到人。适合"镜像预装 + 批量克隆"
    的部署方式 —— 逐台发码在那种场景下根本不现实。

    两种并存，按场次切换：外校选手、补位、重装的机器仍然可以走单人码。
    """

    PER_PLAYER_CODE = "per_player_code"
    BOOTSTRAP = "bootstrap"

    ALL = (PER_PLAYER_CODE, BOOTSTRAP)


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

    #: 默认名单。建场次时从名单库挑一份，之后可以「一键应用」把名单落到选手表。
    #: 只是**模板**，不参与鉴权也不参与成绩 —— 改了名单不会动已有选手，
    #: 得显式应用一次。这样"名单调整"和"比赛数据"永远是两件分开的事。
    default_roster_id = Column(Integer, ForeignKey("roster.id", ondelete="SET NULL"), nullable=True)
    #: 见 ``EnrollmentMode``。老场次迁移后一律是 ``per_player_code``
    enrollment_mode = Column(String(24), nullable=True, default=EnrollmentMode.PER_PLAYER_CODE)

    players = relationship("Player", back_populates="contest", cascade="all, delete-orphan")
    default_roster = relationship("Roster")

    __table_args__ = (Index("ix_contest_status", "status"),)

    @property
    def effective_enrollment_mode(self) -> str:
        """没设过（老数据、或迁移还没回填完）时按原有行为理解。"""
        return self.enrollment_mode or EnrollmentMode.PER_PLAYER_CODE


# --------------------------------------------------------------------------- #
# 名单库
# --------------------------------------------------------------------------- #


class Roster(Base, TimestampMixin):
    """可复用的学生名单。

    和 ``Player`` 的区别是**用途**，不是字段：

    * ``Roster`` 是"这个班/这个考点有哪些人"的事实，跨场次复用
    * ``Player`` 是"这场比赛的参赛者"，成绩、代码、下发都挂在它身上

    两者的关系是**复制**而不是引用：场次从名单库"应用"一次，之后各走各的。
    引用看起来更优雅，但会让"改一下名单"顺带改掉历史场次的参赛者 ——
    那时成绩矩阵的列、代码目录、下发目标全都会跟着动，这不是教师想要的效果。
    """

    __tablename__ = "roster"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    note = Column(Text, nullable=True)

    entries = relationship(
        "RosterEntry",
        back_populates="roster",
        cascade="all, delete-orphan",
        order_by="RosterEntry.player_no",
    )


class RosterEntry(Base, TimestampMixin):
    __tablename__ = "roster_entry"

    id = Column(Integer, primary_key=True)
    roster_id = Column(Integer, ForeignKey("roster.id", ondelete="CASCADE"), nullable=False)
    player_no = Column(String(64), nullable=False)
    name = Column(String(64), nullable=True)
    seat = Column(String(32), nullable=True)
    group_name = Column(String(64), nullable=True)

    roster = relationship("Roster", back_populates="entries")

    __table_args__ = (
        UniqueConstraint("roster_id", "player_no", name="uq_roster_entry_no"),
        Index("ix_roster_entry_roster", "roster_id"),
    )


class BootstrapKey(Base, TimestampMixin):
    """镜像内置的统一注册密钥。

    和 ``EnrollCode`` 的区别：

    * ``EnrollCode`` 绑定到**某个选手**，泄漏一把只影响一个人
    * ``BootstrapKey`` 是**服务端级**的，泄漏一把等于交出"注册任意多台机器"的
      能力 —— 所以它不该出现在选手读得到的地方（``/etc/syncoj/agent.ini``
      对选手账号可读，密钥必须放到 root 只读的单独文件里）

    只存哈希，明文只在签发时打印一次；带 ``use_count`` 便于事后看"这把钥匙
    到底被用过多少次"，异常用量能直接看出来。
    """

    __tablename__ = "bootstrap_key"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(64), nullable=False, unique=True)
    label = Column(String(200), nullable=True, default="")
    note = Column(String(500), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    use_count = Column(Integer, nullable=False, default=0)


class MachineClaim(Base):
    """已注册但**还没认领到人**的机器。

    刻意**不**塞进 ``agent`` 表：那需要把 ``agent.player_id`` 改成可空，
    而 SQLite 改不了列的可空性，只能重建表 —— 重建要关外键强制，而
    ``PRAGMA foreign_keys`` 在事务里是空操作，关不掉时 ``DROP TABLE`` 会顺着
    ``ON DELETE CASCADE`` 把 ``agent_status`` 一起删掉。为了一个"还没归属"的
    中间状态去冒删数据的风险，不值得。

    另立一张表还有个附带好处：未认领的机器**根本没有 player_no**，
    它也就算不出扫描目录、扫不了代码、传不了文件，它的全部工作就是"等认领"。
    状态少，能出的错就少。

    认领成功后这一行被**删除**，同一个 ``token_hash`` 原样转成 ``Agent`` 行 ——
    客户端不用换 token，也没机会在换 token 的过程中掉线。
    """

    __tablename__ = "machine_claim"

    id = Column(Integer, primary_key=True)
    #: 临时凭据。认领后原样变成 agent.token_hash
    token_hash = Column(String(64), nullable=False, unique=True)
    #: Agent 首次运行时自己生成的 UUID，用来在服务端识别"同一台机器的重复注册"
    machine_uuid = Column(String(64), nullable=True)
    #: 硬件指纹。快照还原后 UUID 会没，指纹不会 —— 靠它认回原来的绑定
    machine_fingerprint = Column(String(128), nullable=True)
    machine_id = Column(String(128), nullable=True)
    hostname = Column(String(128), nullable=True)
    os_info = Column(String(200), nullable=True)
    agent_version = Column(String(32), nullable=True)
    #: 配对短码。只存哈希 —— 知道短码就等于能把这台机器认领走，
    #: 而认领走一台机器就是在改"谁的成绩算谁的"
    pair_code_hash = Column(String(64), nullable=True)
    pair_code_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_claim_fingerprint", "machine_fingerprint"),
        Index("ix_claim_uuid", "machine_uuid"),
    )


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


class Problem(Base, TimestampMixin):
    """场次内的题目清单。

    题目在这里是**显式声明**的，而不是从评测结果反向推导。三个理由：

    1. 成绩矩阵的列需要稳定顺序 —— 应当是教师排的顺序，不是字典序，
       更不该随着"哪个选手先被评测"而变
    2. 成绩矩阵要能区分"交了没评测"与"根本没交"，得先知道有哪些题目
    3. 代码回收的路径规范要能按题目自定义

    ``ident`` 同时充当三个角色：目录名、代码文件名、成绩矩阵的列标识。
    所以它必须能安全用作目录名 —— 服务端会对它跑一遍路径校验。

    评测结果里出现的、但没登记在这里的题目**仍会出现在矩阵里**（标注未登记），
    避免因为漏登记而丢掉真实成绩。

    **代码路径规范是可配的**：``file_patterns`` 是一组 glob 模式，用于把回收
    上来的文件归到这道题。留空表示用服务端配置的默认模式
    （``default_file_pattern``，出厂值是 ``{ident}/**``）。改标识时模式里的
    ``{ident}`` 会跟着走，不用手工同步。
    """

    __tablename__ = "problem"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    #: 题目标识：目录名 + 代码文件名 + 成绩列名
    ident = Column(String(64), nullable=False)
    #: 显示标题，留空则显示 ident
    title = Column(String(200), nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    note = Column(Text, nullable=True)
    #: 归属这道题的 glob 模式，JSON 数组；NULL / 空数组表示用服务端默认模式。
    #: 支持 {ident} 与 {title} 占位符（每次匹配时展开，所以改名会自动跟着走）。
    file_patterns = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("contest_id", "ident", name="uq_problem_contest_ident"),
        Index("ix_problem_contest", "contest_id"),
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

    #: Agent 首次运行时自己生成的 UUID。比 ``/etc/machine-id`` 更适合当身份：
    #: 克隆镜像没做通用化时 ``/etc/machine-id`` 是**整批相同**的，而它在首次
    #: 开机后才生成。代价是快照还原会连它一起丢 —— 所以还需要下面的指纹。
    machine_uuid = Column(String(64), nullable=True)
    #: 硬件指纹（SMBIOS UUID / 主机名等）。**快照还原后仍然不变**，
    #: 用来在 Agent 重新注册时认回原来那台机器与它已认领的选手。
    machine_fingerprint = Column(String(128), nullable=True)
    #: 认领（配对）到选手的时间。空表示这台机器是走单人码直接注册的
    claimed_at = Column(DateTime, nullable=True)

    player = relationship("Player", back_populates="agents")

    __table_args__ = (
        UniqueConstraint("player_id", "machine_id", name="uq_agent_player_machine"),
        Index("ix_agent_machine", "machine_id"),
        Index("ix_agent_fingerprint", "machine_fingerprint"),
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
    """Agent 升级包。

    刻意**不自动下发**：``published_at`` 为空表示"已上传但未铺开"。教师必须显式
    调 rollout 接口才会开始向考试机提供 —— 上传一个包和把它推给 50 台机器是
    两件风险等级完全不同的事，不该合成一个动作。
    """

    __tablename__ = "agent_release"

    id = Column(Integer, primary_key=True)
    version = Column(String(32), nullable=False, unique=True)
    channel = Column(String(16), nullable=False, default="stable")
    sha256 = Column(String(64), nullable=False)
    signature = Column(Text, nullable=False)
    size = Column(BigInteger, nullable=False, default=0)
    notes = Column(Text, nullable=True)
    #: 非空 = 已铺开，会随 tick 下发给 Agent
    published_at = Column(DateTime, nullable=True)
    #: 非空 = 已撤回，不再下发
    yanked_at = Column(DateTime, nullable=True)
