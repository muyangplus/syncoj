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
from typing import Optional

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
    "unix_seconds",
    "iso_utc",
    "local_clock",
    "Admin",
    "AdminSession",
    "Contest",
    "Player",
    "Problem",
    "RosterEntry",
    "Roster",
    "BootstrapKey",
    "Agent",
    "AgentStatus",
    "SourceFile",
    "Asset",
    "DeployTask",
    "DeployTarget",
    "JudgeRun",
    "EventLog",
    "RuntimeSetting",
    "AgentRelease",
    "ContestStatus",
    "DeployStatus",
]

Base = declarative_base()


def utcnow() -> datetime:
    """当前 UTC 时间，刨掉 tzinfo（见模块 docstring）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def unix_seconds(moment: datetime) -> int:
    """把 :func:`utcnow` 给的 naive-UTC 时刻转成真正的 Unix 秒。

    **必须显式补 UTC 时区**：``datetime.timestamp()`` 对 naive 时间**按本机时区
    解释**，于是同一个时刻在东八区机器上算出来的秒数差 28800。这个函数以前在
    ``api/agent.py`` 里叫 ``_unix_seconds``，那里踩过一次；卸载令牌的
    ``issued_at`` / ``expires_at`` 也是同一类值，所以把它提到这里只留一处定义 ——
    这个量在心跳、选手页、令牌三处都要用。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """把库里的 naive UTC 渲染成 API 用的 ISO-8601（带 ``Z``）。

    ``None`` 原样返回 ``None``：**"没配时间"和"配了某个时刻"必须能分开**，
    塞一个空串或者当天的零点进去，前端就只能猜这是哪种意思。
    """
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def local_clock(value: datetime) -> str:
    """把库里的 naive UTC 折算成**显示时区**的 ``HH:MM``。

    真正的实现在 :mod:`syncoj_server.timeutil`（那里解释了为什么不能再用进程
    本地时区、以及 ``SYNCOJ_TZ`` 怎么配）。这里留一层转出，是为了让 ``models``
    的读者仍然一眼看得到"这个模块提供哪些时间工具"，同时避免两处各写一份格式化。
    """
    from .timeutil import local_clock as _render

    return _render(value)


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
    #: 考试时间窗。**两个都可空，留空 = 不限制** —— 没配时间的场次绝不能被当成
    #: "已结束"，那是出厂默认（新场次、练习场都不该被时间门禁挡住）。
    #: 只配一个也合法：只填 ``starts_at`` 就只管开考，只填 ``ends_at`` 就只管结束。
    #:
    #: 语义是 ``[starts_at, ends_at)``：**到点那一刻就算结束**。宁可早一秒截止，
    #: 也不要让"最后一秒"的提交落进一个没人打算再收的场次里。
    #: 到点后 ``tasks/flush.py`` 会把 ``running`` 自动置成 ``closed``（那是给人看的
    #: 状态）；而"收不收代码"只看这两个时间（见 ``api/agent.py`` 的上传门禁）——
    #: 后台循环是周期跑的，"到点那一刻"不能靠等它。
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    #: 默认名单。建场次时从名单库挑一份，之后可以「一键应用」把名单落到选手表。
    #: 只是**模板**，不参与鉴权也不参与成绩 —— 改了名单不会动已有选手，
    #: 得显式应用一次。这样"名单调整"和"比赛数据"永远是两件分开的事。
    default_roster_id = Column(Integer, ForeignKey("roster.id", ondelete="SET NULL"), nullable=True)

    players = relationship("Player", back_populates="contest", cascade="all, delete-orphan")
    default_roster = relationship("Roster")

    __table_args__ = (Index("ix_contest_status", "status"),)

    @property
    def is_active(self) -> bool:
        """还在进行的场次。机器动态解析场次时只看这些。

        ``frozen``（封榜）也算 —— 它只是停止下发，仍然在收卷；
        把封榜的场次排除掉，会让封榜瞬间所有机器都"找不到场次"而停摆。
        """
        return self.status in (ContestStatus.RUNNING, ContestStatus.FROZEN)


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

    它是**服务端级**的：泄漏一把等于交出"注册任意多台机器"的能力 ——
    所以它不该出现在选手读得到的地方（``/etc/syncoj/agent.ini``
    对选手账号可读，密钥必须放到 root 只读的单独文件里）。

    只存哈希，明文只在签发时打印一次；带 ``use_count`` 便于事后看"这把钥匙
    到底被用过多少次"，异常用量能直接看出来。

    注意它和**配对码**的分工：统一密钥回答"这台机器是不是我们机房的"，
    配对码回答"这台机器属于哪个人"。前者装在镜像里、长期有效，后者贴在
    屏幕上一小会儿、只用一次。
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


class Player(Base, TimestampMixin):
    """某个场次的参赛者。

    它是**名单库条目在那个场次的物化**：``player_no`` 与名单条目一致。
    机器绑的是名单里的人（``Agent.roster_entry_id``），所以同一个人换一场比赛
    不需要重新配对 —— 只要新场次应用了那份名单，他的机器自然就认得路。
    """

    __tablename__ = "player"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    player_no = Column(String(64), nullable=False)
    name = Column(String(64), nullable=True)
    seat = Column(String(32), nullable=True)
    group_name = Column(String(64), nullable=True)

    contest = relationship("Contest", back_populates="players")

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
    （``default_file_pattern``，出厂值是 ``{ident}/{ident}.cpp`` —— 只认"题目
    目录下与题目同名的那个源文件"。默认要窄：``{ident}/**`` 会把题面、数据、
    说明一起当成选手代码收上来，而那是**教师显式写它**才该发生的事）。
    改标识时模式里的 ``{ident}`` 会跟着走，不用手工同步。
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


# --------------------------------------------------------------------------- #
# Agent 与在线状态
# --------------------------------------------------------------------------- #


class Agent(Base, TimestampMixin):
    """一台考试机。**绑定的是人，不是场次。**

    老模型里 ``agent.player_id`` 指向某个场次的 ``player``，于是"这台机器是谁的"
    会随场次变化，题库里每换一场就得重新发码、重新认领一次。

    现在绑的是**名单库条目**（``roster_entry_id``）。名单条目代表一个学生，
    与场次无关；某个场次有没有他，取决于那份名单有没有被应用到场次里。
    所以今天绑的机器，明天换一场比赛照样能用 —— 这才是"永久配对"该有的样子。

    ``roster_entry_id`` 为空表示**还没配对**：机器已经注册上来、有凭据，
    但服务端还不知道它是谁的。这个状态以前放在单独一张 ``machine_claim`` 表里，
    目的是绕开"改 ``player_id`` 可空性要重建表"这件事；既然这次无论如何都要
    重建 ``agent``（``player_id`` 整个不要了），就该把两种状态收进同一张表 ——
    一张表、一次查询、一套状态机，比两张表少一半的分支。

    ``registration_mode`` 一类的场次级开关也随之消失了：只剩一条注册路径，
    没有"两种模式"可选，也就没有"选错了模式"这种事。
    """

    __tablename__ = "agent"

    id = Column(Integer, primary_key=True)

    # ---- 身份 ----
    #: 绑定的名单条目（人）。为空 = 已注册但未配对
    roster_entry_id = Column(
        Integer, ForeignKey("roster_entry.id", ondelete="SET NULL"), nullable=True
    )
    #: 显式指定的场次。为空 = **动态解析**：找一个"进行中且名单含此人"的场次。
    #: 一个考点同时跑多场比赛、同一个人两边都在时才需要显式指定
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="SET NULL"), nullable=True)

    token_hash = Column(String(64), nullable=False, unique=True)
    machine_id = Column(String(128), nullable=False)
    #: Agent 首次运行时生成并持久化的 UUID。**配对之后认机器主要靠它** ——
    #: 换凭据、重新注册都按它匹配，不再需要配对码。
    machine_uuid = Column(String(64), nullable=True)
    #: 硬件指纹。UUID 会随整机快照还原消失，指纹不会 —— 它是第二道认回依据
    machine_fingerprint = Column(String(128), nullable=True)

    #: 未配对时的 6 位数字配对码。只存哈希：知道它就能把一台机器绑到自己名下
    pair_code_hash = Column(String(64), nullable=True)
    pair_code_expires_at = Column(DateTime, nullable=True)

    hostname = Column(String(128), nullable=True)
    os_info = Column(String(200), nullable=True)
    agent_version = Column(String(32), nullable=True)
    enrolled_at = Column(DateTime, default=utcnow, nullable=False)
    last_enrolled_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    #: 最近一次心跳**报告**"本机有发布公钥"（升级信任锚）的时刻。
    #:
    #: 卸载授权必须由 root 执行，而机器只能用那把公钥验签 —— 所以在没有公钥的
    #: 机器上签授权是白费：它一定拒收，教师看到的却是"操作成功"。这一列就是那句
    #: 拒绝的依据。
    #:
    #: 存时刻而不是布尔：它同时回答了"这台机器上一次说自己有锚是什么时候"，
    #: 排查时那个时间比自己记一个状态位有用。心跳报告为"没有"时会被清空 ——
    #: 信任锚是会被卸载脚本删掉的东西，不能只增不减。
    release_public_key_at = Column(DateTime, nullable=True)
    #: 最近一次心跳报告的"本机上不存在的扫描根"（JSON 数组，绝对路径，最多 5 条）。
    #:
    #: 存下来而不是只放内存，是因为服务端要用它做**状态变化**的判据：缺失出现时
    #: 记一条 warning、恢复时记一条 info，而"这一次和上一次比有没有变"必须有上一次
    #: 的值。列表为空与列是 NULL 在这里是同一件事（没有已知的缺失）。
    scan_missing_json = Column(Text, nullable=True)
    #: 最近一次请求的**来源 IP**。
    #:
    #: 它存在的唯一理由是选手页的"自动匹配本机"：选手不输任何东西，服务端就靠
    #: "哪个 IP 刚才来过"认出他坐的是哪台机器。所以它必须在**每一次**带凭据的
    #: 请求上更新（见 ``api/deps.py`` 的 ``_note_ip``），而不是只在心跳时更新 ——
    #: 选手页自己不会触发心跳，落在它后面就会永远匹配不上。
    #:
    #: 它**不是**身份依据（IP 可以变、可以共享、局域网里也可以伪造）：真正的身份
    #: 是凭据 + 配对关系，这里只回答"打开这一页的浏览器大概在哪台机器上"。
    #: 匹配不上时页面会退回到手填场次与考号，那条路才是兜底。
    last_seen_ip = Column(String(64), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    #: 完成配对的时间
    claimed_at = Column(DateTime, nullable=True)

    roster_entry = relationship("RosterEntry")
    contest = relationship("Contest")

    __table_args__ = (
        UniqueConstraint("machine_uuid", name="uq_agent_uuid"),
        Index("ix_agent_machine", "machine_id"),
        Index("ix_agent_fingerprint", "machine_fingerprint"),
        Index("ix_agent_roster_entry", "roster_entry_id"),
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


class RuntimeSetting(Base):
    """一项**运行参数**（心跳节奏、离线判定）。

    为什么是 key-value 而不是"每个参数一列"：这三项是同一类东西（都是秒数、都由
    同一个对话框改、都在同一个地方校验），而它们以后还会长（教师可能还要调
    扫描上限之类）。一列一个参数的写法每加一项都要改表结构，而 key-value 只要
    在 ``services/runtime_settings.py`` 的 ``DEFAULTS`` 里加一行。

    代价是这一列没有类型约束 —— 所以读取侧一律走 ``load()``，它在那边做范围校验，
    坏值退回出厂值，不让整个心跳 500。

    **改完立即生效**：使用处每次现读，不缓存到启动时（见 ``load`` 的说明）。
    """

    __tablename__ = "runtime_setting"

    key = Column(String(64), primary_key=True)
    value = Column(String(64), nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


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
    #: 构建这个包时**现场签发**的那把统一注册密钥（``bootstrap.key`` 就来自它）。
    #:
    #: 服务端只存哈希、拿不到明文，所以"挑一把已签发的密钥塞进包"这件事
    #: 从一开始就做不到 —— 只能在构建那一刻签一把新的，然后把 id 记在这里。
    #:
    #: ``ON DELETE SET NULL`` 而不是 CASCADE：密钥被删/吊销之后**版本记录必须还在**
    #: （"这个包是哪天发出去的、当时带的是哪把钥匙"是排查现场的第一手材料），
    #: 只是不再指着某一行密钥。吊销本身只改 ``revoked_at``，不动这一列。
    bootstrap_key_id = Column(
        Integer, ForeignKey("bootstrap_key.id", ondelete="SET NULL"), nullable=True
    )
    #: 非空 = 已铺开，会随 tick 下发给 Agent
    published_at = Column(DateTime, nullable=True)
    #: 非空 = 已撤回，不再下发
    yanked_at = Column(DateTime, nullable=True)

    # ---- 随包带下去的安装策略（三条，见 services/install_policy.py）----
    #
    # 存在发布记录上而不是只留在包内：装机台账与升级清单都要**在不读包**的情况下
    # 报出策略（台账要鉴权之外的免登录访问、升级清单只是一次 tick 的响应）。
    # 三处共用同一个 helper 生成，所以它们不会各自漂。
    #
    # 三列都可空，但读的时候一律按默认值补（``build_install_policy``）：迁移之前
    # 建的记录没有这些值，而它们的实际行为就是"不改配置、密钥不动、按默认模式升级"。
    #: 机器上已有的注册密钥要不要被包内那把覆盖（``keep`` / ``replace``）
    bootstrap_key_policy = Column(String(16), nullable=True)
    #: ``agent.ini`` 逐键三态，形如 ``{"scan.roots": "force"}``。空 = 全不改。
    #: 存 JSON 文本而不是单列拆开：键集合以后还会长，一列一个键的写法每加一个键
    #: 都要来改一次表结构。
    config_policy_json = Column(Text, nullable=True)
    #: 自更新模式（``apply`` / ``stage`` / ``off``）
    upgrade_mode = Column(String(16), nullable=True)
