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
    "AgentBinding",
    "AgentDiagnostic",
    "SourceFile",
    "SourceVersion",
    "Asset",
    "DeployTask",
    "DeployTarget",
    "JudgeRun",
    "JudgeTask",
    "JudgeWorker",
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
    #: 已归档：数据全部保留、可查看可导出，但**不再参与任何自动流程** —— 机器动态解析
    #: 不到它（与 ``closed`` 一样不收代码、不下发）、成绩扫描跳过它、不排自带评测，
    #: 场次下拉默认不显示。取消归档回到 ``closed``（§4.9）。
    ARCHIVED = "archived"

    ALL = (DRAFT, RUNNING, FROZEN, CLOSED, ARCHIVED)


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
    #: 考生页密码（scrypt 哈希，同 ``Admin.password_hash``）。**可空 = 不开放**
    #: 「准考证号 + 密码」登录考生页 —— 那一页只剩降级预览与本机自动匹配。
    #: 只写不读：接口只回 ``has_player_password``（迁移 022）。
    player_password_hash = Column(String(255), nullable=True)

    #: 默认名单。建场次时从名单库挑一份，之后可以「一键应用」把名单落到选手表。
    #: 对**选手表**它只是模板 —— 改了名单不会动已有选手，得显式应用一次。
    #: 但它决定**机器进这一场时用哪条绑定**：一台机器可以同时绑多份名单，
    #: 场次用的是这份名单里的那条（§4.6）。没指定时退回按考号对应。
    default_roster_id = Column(Integer, ForeignKey("roster.id", ondelete="SET NULL"), nullable=True)

    #: 自带评测的自动入队（§12.9）：``off`` = 不自动评；``on_change`` = 收到某题的新版本就排一个任务。
    #: 可空只为老库（迁移 020 回填 ``off``），读作 ``off``。
    auto_judge = Column(String(16), nullable=True, default="off")
    #: 自动入队的去抖秒数：同一 (选手, 题目) 在这段时间内连续改动只评最后一版。NULL 读作 60。
    auto_judge_delay = Column(Integer, nullable=True, default=60)
    #: 智能模式（``auto_judge = smart``，§12.12）的阈值，迁移 025。全部可空，NULL 读作默认值
    #: （见 ``services/smart_judge.py`` 的 ``DEFAULTS``）：稳定秒数、空闲秒数、结束前秒数、
    #: 冷却秒数、队列倍数
    smart_stable_seconds = Column(Integer, nullable=True)
    smart_idle_seconds = Column(Integer, nullable=True)
    smart_near_end_seconds = Column(Integer, nullable=True)
    smart_cooldown_seconds = Column(Integer, nullable=True)
    smart_queue_factor = Column(Integer, nullable=True)
    #: 智能模式在场次结束后「全员最新版本评一遍」已做过的时刻；场次重新开放（时间改回未来、
    #: 状态改回进行中）时清空，下次结束再做一次
    smart_end_done_at = Column(DateTime, nullable=True)

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

    @property
    def is_archived(self) -> bool:
        return self.status == ContestStatus.ARCHIVED


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
    机器绑的是名单里的人（``AgentBinding``），所以同一个人换一场比赛
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

    # ---- 自带评测（§12）----
    #: external = 外部评测器（LemonLime / Arbiter 写 judge_result/）；builtin = 自带评测
    judge_mode = Column(String(16), nullable=True, default="external")
    time_limit_ms = Column(Integer, nullable=True)
    memory_limit_mb = Column(Integer, nullable=True)
    #: stdio | file（默认读 <ident>.in、写 <ident>.out，NOI 惯例）
    io_mode = Column(String(16), nullable=True, default="stdio")
    #: 文件 IO 的输入/输出文件名；NULL = 沿用 <ident>.in / <ident>.out
    input_file = Column(String(64), nullable=True)
    output_file = Column(String(64), nullable=True)
    #: 追加到默认编译参数之后（如 -std=c++17）
    compile_flags = Column(String(200), nullable=True)
    #: 数据包（zip）的内容指纹，内容在 blobs/ 里
    testdata_sha256 = Column(String(64), nullable=True)
    testdata_name = Column(String(255), nullable=True)
    testdata_size = Column(BigInteger, nullable=True)
    #: 上传时校验出的摘要（测试点数、子任务数、有没有 checker、总分），JSON
    testdata_info = Column(Text, nullable=True)
    #: 题型（迁移 026，§12.13）：traditional | grader | answer_only | interactive；NULL = traditional。
    #: 题型决定 Agent 收哪些文件（提交答案题收答案文件），所以以这一列为准，数据包只能与它一致
    problem_type = Column(String(16), nullable=True)
    #: 默认比较器的空白处理（迁移 026，§12.5）：strict | trailing_spaces | trailing_newlines | both；NULL = both
    compare_mode = Column(String(16), nullable=True)

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

    现在绑的是**名单库条目**，而且**可以同时绑多份名单**（``agent_binding``，
    每份名单里至多一人）：同一台机器可以是「班级名单」的 1 号，同时是「考场名单」
    的 2 号。进入某个场次时用的是**该场次所用名单**里的那条绑定（见
    ``services/bindings.py``），所以今天绑的机器，明天换一场比赛照样能用。

    一条绑定都没有表示**还没配对**：机器已经注册上来、有凭据，但服务端还不知道
    它是谁的。曾经是单值列 ``roster_entry_id``（迁移 021 搬进 ``agent_binding``
    并删掉）；留着它当"主绑定"会出现两份真相，哪一份说了算只能靠约定。
    """

    __tablename__ = "agent"

    id = Column(Integer, primary_key=True)

    # ---- 身份 ----
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
    #: 最近一次心跳里**被题目预设挡掉**的文件数（见 ``services/collect.py``）。
    #:
    #: 与 ``scan_missing_json`` 同一个理由：判"状态变化"必须有上一次的值，而只放内存
    #: 的话服务端一重启就会把同一批文件重报一遍。它是"数量"而不是"清单"——
    #: 清单在审计事件的 meta 里（截到 20 条），这里只需要回答"现在有没有"。
    scan_skipped = Column(Integer, nullable=True)
    #: 教师点过「要一份诊断」，等**下一次心跳**取走的一次性标记。
    #:
    #: 只存"待取"这一个事实，取走即置空（见 ``api/agent.py`` 的
    #: ``_take_diagnostics_request``）—— 机器没收到就让教师再点一次。
    #: 落库而不是只放内存：服务端重启不该把教师刚点的请求吞掉。
    diagnostics_requested_at = Column(DateTime, nullable=True)
    #: 最近一次请求的**来源 IP**。
    #:
    #: 它存在的唯一理由是选手页的"自动匹配本机"：选手不输任何东西，服务端就靠
    #: "哪个 IP 刚才来过"认出他坐的是哪台机器。所以它必须在**每一次**带凭据的
    #: 请求上更新（见 ``api/deps.py`` 的 ``_note_ip``），而不是只在心跳时更新 ——
    #: 选手页自己不会触发心跳，落在它后面就会永远匹配不上。
    #:
    #: 它**不是**身份依据（IP 可以变、可以共享、局域网里也可以伪造）：真正的身份
    #: 是凭据 + 配对关系，这里只回答"打开这一页的浏览器大概在哪台机器上"。
    #: 匹配不上时页面会退回到降级预览与「准考证号 + 考生页密码」登录。
    last_seen_ip = Column(String(64), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    #: 完成配对的时间
    claimed_at = Column(DateTime, nullable=True)

    contest = relationship("Contest")
    #: 这台机器在各份名单里的绑定（每份名单至多一条）。空 = 未配对
    bindings = relationship(
        "AgentBinding",
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentBinding.id",
    )

    __table_args__ = (
        UniqueConstraint("machine_uuid", name="uq_agent_uuid"),
        Index("ix_agent_machine", "machine_id"),
        Index("ix_agent_fingerprint", "machine_fingerprint"),
    )

    @property
    def is_paired(self) -> bool:
        """至少绑了一份名单里的一个人。"""
        return bool(self.bindings)


class AgentBinding(Base):
    """机器 ↔ 某份名单里的某个人（§4.6）。

    两条唯一约束就是"一人一机、一机一人"在多名单下的样子：

    * ``(agent_id, roster_id)``：一台机器在**一份名单里**只对应一个人
    * ``(roster_entry_id)``：一个人只对应一台机器

    ``roster_id`` 是冗余（条目自己知道在哪份名单），留着它只为了让第一条约束能由
    库来守 —— 靠应用层先查再写，并发配对时守不住。条目、名单、机器任一被删，
    绑定随之级联删除（条目没了绑定就没了，与以前 ``SET NULL`` 的效果相同）。
    """

    __tablename__ = "agent_binding"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agent.id", ondelete="CASCADE"), nullable=False)
    roster_entry_id = Column(
        Integer, ForeignKey("roster_entry.id", ondelete="CASCADE"), nullable=False
    )
    roster_id = Column(Integer, ForeignKey("roster.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    agent = relationship("Agent", back_populates="bindings")
    entry = relationship("RosterEntry")
    roster = relationship("Roster")

    __table_args__ = (
        UniqueConstraint("agent_id", "roster_id", name="uq_agent_binding_roster"),
        UniqueConstraint("roster_entry_id", name="uq_agent_binding_entry"),
        Index("ix_agent_binding_roster", "roster_id"),
    )


class JudgeWorker(Base):
    """一台评测机（§12）。凭据只存哈希；吊销 = 删行（硬删，ADR-0003）。"""

    __tablename__ = "judge_worker"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(64), nullable=True)
    #: 评测机自报的状态（迁移 023，§12.10）：每次 ``/judge/poll`` 覆盖写。旧评测机只调
    #: ``/judge/lease``、不报这些，于是全是 NULL —— 界面按"旧版评测机"显示，也收不到远程命令。
    version = Column(String(32), nullable=True)
    backend = Column(String(16), nullable=True)
    isolated = Column(Boolean, nullable=True)
    hostname = Column(String(128), nullable=True)
    platform = Column(String(32), nullable=True)
    slots = Column(Integer, nullable=True)
    #: 评测机空闲时多久问一次（秒）。在线判定 = 最近联系不超过它的 3 倍
    poll_seconds = Column(Integer, nullable=True)
    #: 暂停 = False：领不到新任务，但照常心跳、评完手上的
    enabled = Column(Boolean, nullable=False, default=True)
    #: 教师要它做的事：``run`` / ``stop`` / ``restart``。后两者在下一次 poll 里随响应带走，
    #: 带走即复位成 ``run`` —— 命令是一次性的，评测机日后重新启动不会再被停一次
    desired = Column(String(16), nullable=False, default="run")
    #: 评测机进程的随机标识（每次启动换新）；``desired_boot`` = 下命令时它是哪个进程。
    #: 命令一直重发给那个进程，等换了进程（重启 / 停止后再起）才复位：响应丢了命令不丢
    boot_id = Column(String(32), nullable=True)
    desired_boot = Column(String(32), nullable=True)
    #: 服务端以子进程拉起的「本机评测机」（§12.10）。同时最多一行
    is_local = Column(Boolean, nullable=False, default=False)
    #: 管理端设定的并发数（迁移 024）：随 poll 响应下发，与评测机自报的 ``slots`` 不同时它评完
    #: 手上的任务、换成这个 ``--slots`` 重启自己。NULL = 没设过，按评测机自己的启动参数
    desired_slots = Column(Integer, nullable=True)
    #: 评测机报的能力（逗号分隔，迁移 024）：``slots`` = 认下发的并发数。旧评测机为 NULL
    features = Column(String(128), nullable=True)


class JudgeTask(Base):
    """一次自带评测（选手 × 题目 × 某一版代码）。任务即历史：完成的任务连同结果留着，
    分数 timeline 从这里读（§12.9）。

    状态：``queued`` → ``running``（带租约，worker 崩了租约过期自动回到队列）→ ``done`` / ``failed``。
    ``done`` 不等于有成绩：没交代码的选手直接 ``done``、不写 ``result.json``（矩阵照旧显示未交）。

    ``trigger``：``auto``（收到新版本自动排）/ ``smart``（智能模式判断"可能写完了"才排，§12.12）/
    ``manual``（教师排，评最新版）/ ``rejudge``（教师指定某个历史版本重测）。前三种领任务时才选代码，
    结果写进 ``result.json``；
    ``rejudge`` 钉死 ``source_version_id``，结果只进历史，教师「采用」后才写 ``result.json``。
    """

    __tablename__ = "judge_task"

    id = Column(Integer, primary_key=True)
    contest_id = Column(Integer, ForeignKey("contest.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    problem = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    attempt = Column(Integer, nullable=False, default=0)
    worker_id = Column(Integer, ForeignKey("judge_worker.id", ondelete="SET NULL"), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    #: 领任务时选定的那份代码（台账里的相对路径与内容指纹）
    source_path = Column(String(1024), nullable=True)
    source_sha256 = Column(String(64), nullable=True)
    #: 结果摘要（不是成绩本身 —— 成绩走 judge_result/ → 扫描器）
    outcome = Column(String(16), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    # ---- 按版本记（§12.9，迁移 020）----
    #: auto / manual / rejudge（见类说明）；NULL 读作 manual（迁移前的老任务）
    trigger = Column(String(16), nullable=True)
    #: 排任务的管理员用户名；自动入队为 NULL
    requested_by = Column(String(64), nullable=True)
    #: 评的是哪一版代码。rejudge 排任务时就定下；auto / manual 领任务时选定。
    #: 版本被删（清代码、删选手）时置空，任务与结果仍留在历史里
    source_version_id = Column(Integer, ForeignKey("source_version.id", ondelete="SET NULL"), nullable=True)
    #: 去抖：这个时刻之前不发给评测机（自动入队用；手动排的为 NULL）
    not_before = Column(DateTime, nullable=True)
    #: 教师要求真跑一遍，不复用相同 (数据包, 评测配置, 代码) 的已有结果
    rerun = Column(Boolean, nullable=True, default=False)
    #: (数据包 sha, 评测配置, 代码 sha) 的指纹，复用结果按它找
    judge_key = Column(String(64), nullable=True)
    #: 提交答案题这次评的答案文件清单（迁移 026）：JSON ``[{case, name, path, sha256}]``；
    #: ``source_sha256`` 存这份清单（规范化后）的 sha256
    sources_json = Column(Text, nullable=True)
    #: 在评任务的阶段（迁移 026）：compiling / judging；评测机上报，旧评测机不报（NULL）
    phase = Column(String(16), nullable=True)
    #: 结果直接抄自哪个任务（没有占评测机）
    reused_from = Column(Integer, nullable=True)
    #: 交回的结果：三项成绩与完整文档（测试点明细，超长时截断）
    score = Column(Integer, nullable=True)
    max_score = Column(Integer, nullable=True)
    result_json = Column(Text, nullable=True)
    #: 这份结果写进 result.json 的时刻（自动写入或教师「采用」）
    applied_at = Column(DateTime, nullable=True)
    #: 结果里疑似违规 / 风险的条数（迁移 024，§12.11）：入库时按 ``precheck.count_flags`` 算好，
    #: 列表按它筛选，不必每次解析 ``result_json``
    warning_count = Column(Integer, nullable=True, default=0)
    risk_count = Column(Integer, nullable=True, default=0)
    #: 领任务的先后（迁移 025，§12.12）：大的先领，同级按 id。手动 / 重测 100，智能模式按
    #: 信号 20–60，收到新版本即评 10。老任务迁移时按触发方式回填
    priority = Column(Integer, nullable=True, default=0)
    #: 智能模式排的任务（``trigger = smart``）是哪个信号触发的：``moved_on`` / ``long_stable`` /
    #: ``near_end`` / ``offline`` / ``contest_end``；其余触发方式为 NULL
    reason = Column(String(16), nullable=True)
    #: 智能模式排任务时看到的那个最新版本（不钉死代码，领任务时照常选最新版）。"同一版本不重复排"
    #: 按它与 ``source_version_id`` 判 —— 领任务失败、没选上这一版时也不会反复排
    target_version_id = Column(Integer, ForeignKey("source_version.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("ix_judge_task_status", "status", "id"),
        Index("ix_judge_task_pair", "contest_id", "player_id", "problem"),
        Index("ix_judge_task_version", "source_version_id"),
        Index("ix_judge_task_key", "judge_key"),
    )


class AgentDiagnostic(Base):
    """一台机器**最新一份**诊断包（Agent 定期/出错/被点名时回传的现场）。

    为什么只留最新一份而不是历史：诊断包的用途是"机器离线之后还看得到它最后
    的样子"，教师要看的是**当前**那份现场；存历史会让一张表按机器数 × 每 10
    分钟一条无限长下去，而旧的现场对排查几乎没用（Agent 的日志本身也在轮转，
    更早的包只会是更早的噪音）。旧包想留，就在管理端读到之后自己存出去。

    ``agent_id`` 直接当主键：一台机器一行，"覆盖写入"因此是天然语义，不需要
    额外的唯一约束。``content`` 存**解压后的 JSON 文本**，所以读的时候不必再
    解压一次 —— 而"解压炸弹"在上传那一刻就已经被上限挡住（见
    ``services/diagnostics.py``）。

    ``bytes`` 是**解压后** JSON 文本的字节数（也就是 ``content`` 的大小）：
    它比压缩后的大小更能说明"这份现场里到底有多少东西"。
    ``agent`` 行被删时一起删（``CASCADE``）—— 机器都不在台账里了，留着它的
    诊断包没有意义，反而会在"作废后重新注册"时指向一个已经不存在的身份。
    """

    __tablename__ = "agent_diagnostic"

    agent_id = Column(
        Integer, ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True
    )
    received_at = Column(DateTime, nullable=False, default=utcnow)
    #: Agent 自报的 ``reason``（``periodic`` / ``error`` / ``manual``）。没给或
    #: 给了个空白值就是 ``NULL`` —— 服务端只原样存下，不做取值校验。
    reason = Column(String(32), nullable=True)
    #: 解压后 JSON 文本的字节数（见类 docstring）。
    bytes = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)


# --------------------------------------------------------------------------- #
# 代码回收台账
# --------------------------------------------------------------------------- #


class SourceFile(Base):
    """回收到的文件的最新版本。

    ``revision`` 在每次存下新内容时递增。"迟到的旧版本"（Agent 断网重传时可能把
    旧内容送到）靠比对 ``scanned_sha256`` 丢弃：送来的不是最近一次扫描到的那一版，
    就以 409 拒绝，避免覆盖更新的版本。
    """

    __tablename__ = "source_file"

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    rel_path = Column(String(1024), nullable=False)
    #: **已存下的**那一版的内容指纹（``content_stored`` 为真时，blobs/ 与 source/ 里
    #: 就是它）。下载、导出、blob 引用计数读的都是它。尚未存过任何一版时，它暂记
    #: 最近一次扫描到的值（``content_stored`` 为假，没有读者会把它当成"已存"）。
    sha256 = Column(String(64), nullable=False)
    #: **最近一次扫描**到的内容指纹。与 ``sha256`` 不同 = 选手改过、新内容还没传上来，
    #: tick 会一直把它放进 ``need_upload`` 直到传上来为止。
    #:
    #: 为什么要单独一列：曾经 tick 在**上传之前**就把 ``sha256`` 改成新扫描值、却不
    #: 复位 ``content_stored`` —— 那一轮只要没传上（断网、重启、截断、409），下一轮
    #: 就判成"没变、已存"，再也不要求上传：``source/`` 停在旧版本，台账却显示新指纹。
    #: 可空只为老库：迁移 013 用 ``sha256`` 回填；读的一侧把 NULL 当成 ``sha256``。
    scanned_sha256 = Column(String(64), nullable=True)
    size = Column(BigInteger, nullable=False, default=0)
    mtime = Column(BigInteger, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=0)
    first_seen_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=utcnow, nullable=False)
    # 服务端已收到内容（blobs 中有 sha256 那一版）才置 True
    content_stored = Column(Boolean, nullable=False, default=False)
    # 非 NULL 表示该文件已从扫描结果中消失（选手删除/改名）
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("player_id", "rel_path", name="uq_source_player_path"),
        Index("ix_source_sha", "player_id", "sha256"),
    )


class SourceVersion(Base):
    """代码的版本历史（§12.9）：Agent 每传上来一份**新内容**就记一条。

    ``SourceFile`` 只记当前版本；这里留下每一版的内容指纹（内容在 ``blobs/`` 里，列入
    blob 引用清单不被回收），分数 timeline 与"重测过去的版本"都从这里取。选手删除、
    清空代码台账（含已消失的）时一并删掉并回收内容。

    ``problem`` 是收到时按题目模式归到的题（审计用；读的一侧按**当前**题目模式重新归题，
    改题目标识或模式不会让历史对不上）。迁移 020 补的初始版本为空。
    """

    __tablename__ = "source_version"

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    rel_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size = Column(BigInteger, nullable=False, default=0)
    received_at = Column(DateTime, default=utcnow, nullable=False)
    problem = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_source_version_player", "player_id", "rel_path", "id"),
        Index("ix_source_version_sha", "sha256"),
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
    """一次下发。

    **任务没有存储的状态列**：界面上的任务状态每次从目标聚合（见管理端
    ``_derive_task_status``）。曾经有一列 ``status``，由回执路径重算、取消 / 重试 /
    重排各自去改，取消接口又按它判断 —— 几处写法不一致时它就和目标对不上了
    （迁移 015 删掉了它）。唯一需要单独记的是「教师取消过」这件事本身，
    它不是目标能推导出来的，所以是 ``cancelled_at``。
    """

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
    #: 教师点「取消」的时刻；非空 = 已取消：不再派发、也不再给下载。
    #: 「重试」把失败目标重新排队时清空它（重试就是重新开始下发）。
    cancelled_at = Column(DateTime, nullable=True)

    asset = relationship("Asset")
    targets = relationship("DeployTarget", back_populates="task", cascade="all, delete-orphan")


class DeployTarget(Base):
    __tablename__ = "deploy_target"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("deploy_task.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("player.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(16), nullable=False, default=DeployStatus.PENDING)
    #: 机器上报的本地进度（分片里已有多少字节），只供管理端显示。
    bytes_done = Column(BigInteger, nullable=False, default=0)
    #: 教师在管理端手工"重试"过几次。
    retry_count = Column(Integer, nullable=False, default=0)
    #: **连续**下载失败的次数（机器回报的）。到 ``deploy.MAX_CONSECUTIVE_FAILURES``
    #: 判 FAILED，不再派发 —— 曾经下载失败从不回写，失败的目标永远显示"进行中"。
    #: 成功、内容变更、手工重试都清零。可空只为老库（迁移 014），读作 0。
    failure_count = Column(Integer, nullable=True, default=0)
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
    #: ``raw_json`` 里疑似违规 / 风险的条数（迁移 024，§12.11）：扫描入库时算好，列表按它筛选
    warning_count = Column(Integer, nullable=True, default=0)
    risk_count = Column(Integer, nullable=True, default=0)
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
    #: 级别与类别都建索引：审计页按它们筛、清理按级别删（迁移 016）
    level = Column(String(16), nullable=False, default="info", index=True)
    category = Column(String(32), nullable=False, index=True)
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
