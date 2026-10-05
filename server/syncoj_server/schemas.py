"""API 请求 / 响应模型（pydantic v2）。

这些模型是 **协议的唯一真相源**：FastAPI 由它们导出 OpenAPI，前端 TS 类型从
OpenAPI 生成；Agent 侧因为必须零依赖（不能 import pydantic），改用
``agent/tools/build_fixture.py`` 生成真实 payload，再由契约测试反向校验。

字段命名与 `docs/protocol.md` 必须逐字对应。
"""

from __future__ import annotations

from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnrollRequest",
    "EnrollResponse",
    "ScanEntry",
    "PartialDownload",
    "TickStats",
    "TickRequest",
    "DeployJob",
    "UpgradeInfo",
    "TickResponse",
    "UploadResult",
    "AgentEvent",
    "SimpleAck",
    "Page",
]

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 列表信封
# --------------------------------------------------------------------------- #

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """**所有** GET 集合接口的统一形状。

    ``total`` 是**忽略分页后的真实总数**，不是 ``len(items)``：前端的分页器与
    "清空 N 条"的确认文案都靠它，拿本页条数充数会让第二页显示"共 50 条"。

    这里刻意**不继承** ``_Base``：它是响应模型，没有"请求体多了个字段"这回事，
    而 ``extra="forbid"`` 在响应用途上毫无意义、只会让开放 API 的时候多点噪音。
    """

    items: List[T] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


def page_of(items: List[T], total: int, params: Any) -> "Page[T]":
    """把已经切好页的一批对象装进信封。

    只做装配，不碰 SQL —— 分页由调用方决定是真的 ``LIMIT/OFFSET`` 还是
    Python 切片（见 ``docs/api-conventions.md`` §2）。
    """
    return Page(
        items=items, total=int(total), limit=int(params.limit), offset=int(params.offset)
    )


def slice_page(rows: List[T], params: Any) -> "Page[T]":
    """对有界集合用 Python 切片分页。

    只在"天然就很小"的集合上用（场次、名单、题目、资产…）。
    ``total`` 仍然是完整长度，所以前端看到的分页信息是真的。
    """
    return page_of(
        rows[params.offset : params.offset + params.limit], len(rows), params
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


class EnrollRequest(_Base):
    """注册请求。

    只有一条路了：**镜像内置的统一密钥**。每选手注册码已经被"机器永久绑定
    名单条目"取代 —— 逐台发码在"一份镜像装遍整间机房"的现实里根本不可行，
    而两种模式并存意味着每种都要维护、测试、并且迟早有人选错。

    机器身份的三个要素，作用各不相同：

    * ``machine_uuid`` —— Agent 首次运行时生成。**配对之后认机器主要靠它**，
      换凭据、重新注册都按它匹配，不再需要配对码
    * ``machine_fingerprint`` —— 硬件指纹（SMBIOS UUID）。UUID 会随整机快照还原
      一起消失，指纹不会，所以它是第二道依据
    * ``machine_id`` —— 仅供人工辨认（列表里显示），不参与身份判定
    """

    machine_id: str = Field(min_length=1, max_length=128)
    hostname: Optional[str] = Field(default=None, max_length=128)
    agent_version: Optional[str] = Field(default=None, max_length=32)
    os_info: Optional[str] = Field(default=None, max_length=200)

    bootstrap_key: str = Field(min_length=1, max_length=256)
    machine_uuid: Optional[str] = Field(default=None, max_length=64)
    machine_fingerprint: Optional[str] = Field(default=None, max_length=128)


class EnrollResponse(_Base):
    """注册结果。

    三种状态，客户端必须分清 —— 它们看起来都像"注册成功了"，但该做的事完全不同：

    ==================  =========  =============  ==============================
    ``claimed``         ``bound``  ``contest``    客户端该做什么
    ==================  =========  =============  ==============================
    ``false``           false      —              把 ``pair_code`` 显示给人看，等配对
    ``true``            false      ``null``       已配对，但还没有含你的场次：等
    ``true``            true       有值           正常干活：扫代码、收下发
    ==================  =========  =============  ==============================

    "已配对但没场次"这一档是新的：机器绑的是**人**，而某个场次有没有这个人，
    取决于那份名单有没有被应用到场次里。所以配对成功不等于马上能干活 ——
    这个中间状态必须说得出来，否则客户端只能猜（猜错的后果是它去扫一个
    展开不出准考证号的目录）。
    """

    token: str
    agent_id: int
    #: 已经绑定了名单条目（人）
    claimed: bool = True
    #: 未绑定时的一次性配对码，**六位数字**
    pair_code: Optional[str] = None
    #: 已解析出可用的场次与选手
    bound: bool = False
    #: 没能解析出场次时的人话原因（界面与日志都用它）
    reason: Optional[str] = None

    player_no: str = ""
    player_name: Optional[str] = None
    contest_id: Optional[int] = None
    contest_slug: str = ""
    contest_name: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# tick
# --------------------------------------------------------------------------- #


class ScanEntry(_Base):
    """一条扫描结果。**不含文件内容**，只有索引。"""

    path: str = Field(max_length=1024)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    mtime: int = Field(ge=0)


class PartialDownload(_Base):
    """客户端本地未完成的下载，服务端据此回填 offset。"""

    asset_id: int
    bytes_done: int = Field(ge=0)


class TickStats(_Base):
    disk_free: Optional[int] = Field(default=None, ge=0)
    last_error: Optional[str] = Field(default=None, max_length=1024)
    queue: int = Field(default=0, ge=0)


class TickRequest(_Base):
    agent_version: Optional[str] = Field(default=None, max_length=32)
    machine_id: str = Field(min_length=1, max_length=128)
    #: 主机名。每次 tick 都报 —— 待配对的机器要靠它在列表里被认出来，
    #: 而"改名"这件事在考场上是会发生的（教师按座位重命名机器）
    hostname: Optional[str] = Field(default=None, max_length=128)
    ts: int = Field(ge=0)
    scan_root: Optional[str] = Field(default=None, max_length=512)
    scan: List[ScanEntry] = Field(default_factory=list)
    #: 本次扫描是否完整覆盖了扫描根目录。
    #: 若 Agent 中途遇到 PermissionError 等错误导致扫描不完整，必须置 False，
    #: 服务端会因此**跳过删除判定** —— 否则会把"没扫到"误判成"被删了"。
    scan_complete: bool = True
    partials: List[PartialDownload] = Field(default_factory=list)
    #: 本地已确认完整的下发资源。服务端据此把对应的 deploy_target 标为完成。
    #: 刻意由 Agent 显式上报而不是服务端从 partials 推断 —— "分片不见了" 既可能
    #: 是下完了，也可能是被清理了，推断会误判。
    completed_assets: List[int] = Field(default_factory=list)
    stats: TickStats = Field(default_factory=TickStats)


class DeployJob(_Base):
    asset_id: int
    url: str
    sha256: str
    size: int
    dest: str
    offset: int = 0
    mode: str = "overwrite"


class UpgradeInfo(_Base):
    version: str
    url: str
    sha256: str
    signature: str
    size: int = 0
    notes: Optional[str] = None


class TickResponse(_Base):
    server_time: int
    next_tick_seconds: int
    need_upload: List[str] = Field(default_factory=list)
    deploy_jobs: List[DeployJob] = Field(default_factory=list)
    cancel_assets: List[int] = Field(default_factory=list)
    upgrade: Optional[UpgradeInfo] = None
    config: Dict[str, Any]

    #: 这台机器是否已经绑定到名单条目（人）。未配对时为 False
    claimed: bool = True
    #: 未配对时的六位配对码。Agent 要把它显示给人看（桌面文件 + 日志）。
    #: **只在绑定时用一次** —— 配对之后按 machine_uuid 认机器，不再需要它
    pair_code: Optional[str] = None
    #: 是否已经解析出可用的场次与选手（见 ``EnrollResponse`` 的三态说明）
    bound: bool = False
    #: 没解析出场次时的人话原因
    reason: Optional[str] = None
    #: 解析出来的身份，Agent 拿到就存进凭据，之后才能扫代码
    player_no: Optional[str] = None
    contest_slug: Optional[str] = None


class UploadResult(_Base):
    path: str
    sha256: str
    revision: int
    stored: bool


class AgentEvent(_Base):
    level: str = Field(default="info", max_length=16)
    category: str = Field(max_length=32)
    message: str = Field(max_length=2000)
    meta: Optional[Dict[str, Any]] = None


class SimpleAck(_Base):
    ok: bool = True
    detail: Optional[str] = None


class ConfirmIn(_Base):
    """批量清空的确认体。

    "清空"删的是一**批**对象，没有单个名字可以打，所以确认内容是**范围的名字**：
    场次范围内的清空用场次 ``slug``、名单范围内用名单 ``name``；
    范围本身是"全部"的（审计日志、待配对机器）用固定字面量 ``all``。

    为什么要走请求体而不是 query：清空是破坏性动作，把确认值放在 JSON 体里，
    浏览器、curl、前端都更不容易在一个 copy-paste 里把它丢掉。
    """

    confirm: str = Field(min_length=1, max_length=200)


class FileClearIn(ConfirmIn):
    """清代码台账。

    ``purge=False``（默认）只打墓碑 —— 行还在、``?include_deleted=true`` 仍能
    查到，导出里也仍然带着（见 ``docs/api-conventions.md`` §5.2）。
    ``purge=True`` 才连行一起删，只在"这些记录本来就是误传"时才该用。
    """

    purge: bool = False


#: 全局范围清空时要求输入的确认字面量。前端把这句话原样显示给教师：
#: "这次删的不是某一个对象，整个范围都要清掉，请输入 all 确认"。
GLOBAL_CONFIRM = "all"


class PlayerClearIn(ConfirmIn):
    """清空一个场次的选手名单。

    ``keep_with_submissions`` 默认 **True**：已经有代码或成绩的选手留着 ——
    误删代码是不可逆的，所以"连提交一起清"必须是显式选择，不能是默认。
    """

    keep_with_submissions: bool = True


class JudgeRunClearIn(ConfirmIn):
    """清空成绩记录。

    ``player_id`` 把范围缩小到一个人；``keep_manual`` 默认 True，
    因为手工录入的分数是全场最贵的数据，不该被"重扫一遍"顺手清掉。
    """

    player_id: Optional[int] = None
    keep_manual: bool = True


# --------------------------------------------------------------------------- #
# 管理后台
# --------------------------------------------------------------------------- #


class LoginRequest(_Base):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(_Base):
    token: str
    username: str
    expires_at: str


class AdminInfo(_Base):
    username: str
    is_active: bool


class ContestCreate(_Base):
    name: str = Field(min_length=1, max_length=200)
    slug: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(default="draft", max_length=16)
    note: Optional[str] = None
    #: 默认名单。只作为"一键应用"的预设，不参与鉴权也不影响已有选手
    default_roster_id: Optional[int] = None


class ContestOut(_Base):
    id: int
    slug: str
    name: str
    status: str
    player_count: int = 0
    online_count: int = 0
    created_at: str
    default_roster_id: Optional[int] = None
    default_roster_name: Optional[str] = None
    #: 备注。**必须回显** —— ``PATCH`` 收下它却读不回来的话，界面上就是
    #: "改完保存再打开又变回空的"，而教师会以为是自己没保存成功
    note: Optional[str] = None


class ContestUpdate(_Base):
    """场次的可改字段。全部可选 —— 只改传了的。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[str] = Field(default=None, max_length=16)
    note: Optional[str] = None
    default_roster_id: Optional[int] = None
    #: 显式置空用的开关。``None`` 字段没法区分"没传"和"传了 null"，
    #: 所以要多一个布尔 —— 否则教师永远清不掉已经选错的名单。
    clear_default_roster: bool = False


# --------------------------------------------------------------------------- #
# 名单库
# --------------------------------------------------------------------------- #


class RosterEntryIn(_Base):
    """名单条目。

    ``player_no`` 的长度上限刻意给得**很宽**（512），真正的 64 字上限由业务层
    逐条判定。原因：pydantic 的校验是**全有或全无** —— 一条超长会让整个请求
    422，教师粘的 100 行名单全部白填，而界面上只会弹一句
    "String should have at most 64 characters"。

    批量接口的逐条错误必须由业务层报，那里才能做到"跳过这一条，其余照常入库"。
    这里留的宽上限只用来挡住明显荒谬的输入（比如 10MB 的字符串）。
    """

    player_no: str = Field(max_length=512)
    name: Optional[str] = Field(default=None, max_length=512)
    seat: Optional[str] = Field(default=None, max_length=512)
    group_name: Optional[str] = Field(default=None, max_length=512)


class RosterEntryOut(_Base):
    id: int
    roster_id: int
    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None
    group_name: Optional[str] = None


class RosterCreate(_Base):
    name: str = Field(min_length=1, max_length=200)
    note: Optional[str] = None


class RosterOut(_Base):
    id: int
    name: str
    note: Optional[str] = None
    created_at: str
    entry_count: int = 0


class RosterDetailOut(RosterOut):
    entries: List["RosterEntryOut"] = Field(default_factory=list)


class RosterImportOut(_Base):
    """名单导入结果。

    和题目导入一样**逐条容错**：某一行不合法只跳过那一行并在 ``errors``
    里说明，不让整批失败。一份名单几十上百行，因为一个空格全部白填
    是没人能接受的。
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = Field(default_factory=list)
    entries: List[RosterEntryOut] = Field(default_factory=list)


class ApplyRosterIn(_Base):
    """把名单应用到场次。

    ``prune`` 默认 **False**，而且**永远不动已经有代码或成绩的选手** ——
    名单调整是常事，把参赛者的提交一起删掉是不可逆的事故。
    """

    roster_id: Optional[int] = None
    prune: bool = False


class ApplyRosterOut(_Base):
    roster_id: Optional[int] = None
    roster_name: Optional[str] = None
    created: int = 0
    updated: int = 0
    kept: int = 0
    pruned: int = 0
    #: 因为"已经有提交/成绩"而被保留下来的选手编号
    protected: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 机器配对（统一密钥注册）
# --------------------------------------------------------------------------- #


class PendingMachineOut(_Base):
    """一台注册上来、还没配对到人的机器。"""

    id: int
    machine_id: str
    hostname: Optional[str] = None
    os_info: Optional[str] = None
    agent_version: Optional[str] = None
    machine_uuid: Optional[str] = None
    machine_fingerprint: Optional[str] = None
    created_at: str
    last_seen_at: Optional[str] = None
    #: 距上次心跳多少秒 —— 教师靠它判断"这台机器还在不在"
    seconds_since_seen: Optional[float] = None
    #: 配对码还剩多少秒过期。**不返回码本身**：库里只有哈希，
    #: 明文只在注册那一刻发给过机器，服务端自己也不该留
    pair_code_expires_in: Optional[int] = None
    #: 同一硬件指纹上还有几台机器。>1 说明疑似克隆镜像，界面要报警
    fingerprint_peers: int = 0


class BindMachineIn(_Base):
    """把一台机器配对到名单里的某个人。

    这是"永久配对"：绑的是**人**（名单条目），不是某场比赛的选手。
    所以同一个学生换一场比赛不用重新配 —— 只要新场次应用了那份名单。

    ``pair_code`` 可以省略（教师从列表里按主机名直接点选时），
    但只要给了就必须对得上：主机名可能是重复的，而配对码是唯一能证明
    "我确实站在那台机器前面"的东西。
    """

    roster_entry_id: int
    pair_code: Optional[str] = Field(default=None, max_length=32)


class BindByCodeIn(_Base):
    """用配对码配对：机器上显示什么，教师就输什么。"""

    pair_code: str = Field(min_length=1, max_length=32)
    roster_entry_id: int


class RebindAgentIn(_Base):
    """把一台机器改派给名单里的另一个人。"""

    roster_entry_id: int


class BindResultOut(_Base):
    """配对成功的结果。

    回显"绑到了谁、在哪份名单里"，是因为配对是**永久**动作：
    教师按下的这一刻决定了这台机器以后是谁的。给一句明确的回执，
    比让他在列表里自己找那台机器现在归属谁要可靠得多。
    """

    agent_id: int
    roster_entry_id: int
    player_no: str
    roster_name: str = ""
    detail: str = ""


class SetAgentContestIn(_Base):
    """给一台机器显式指定场次。

    默认是**动态解析**：找一个"进行中、且名单含此人"的场次。
    只有在一个考点同时跑多场比赛、同一个人两边都在时才需要指定 ——
    那种情况下服务端会明确说"需要指定场次"，而不是自己挑一个。
    """

    contest_id: Optional[int] = None


class CloneAlertOut(_Base):
    """克隆镜像告警：多台机器共用同一个硬件指纹。

    **严重程度取决于有没有人已经被绑上去。** 一份镜像装遍整间机房时撞指纹是必然
    的、也是正常的 —— 那时一台机器都还没配对，没有任何人的身份可以被冒领，界面
    上就不该说"可能混进了别人的凭据"。真正要警告的是"某个指纹上已经挂着一个具体
    的人了"：快照还原那条"按指纹认回原机器"的路径会认错机器，而错的那一头就是
    某个学生的成绩。
    """

    fingerprint: str
    machine_count: int
    #: 这个指纹上已经配对给人的机器数。0 = 还没有任何身份可被冒领
    bound_count: int = 0
    hostnames: List[str] = Field(default_factory=list)


class BootstrapKeyOut(_Base):
    id: int
    label: Optional[str] = None
    note: Optional[str] = None
    created_at: str
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None
    use_count: int = 0


class BootstrapKeyIssueIn(_Base):
    label: Optional[str] = Field(default=None, max_length=200)
    note: Optional[str] = Field(default=None, max_length=500)
    expires_days: Optional[int] = Field(default=None, ge=1, le=3650)


class BootstrapKeyIssuedOut(BootstrapKeyOut):
    """签发结果。``key`` 明文**只在这一刻出现**，之后库里只有哈希。"""

    key: str


class PlayerUpsert(_Base):
    player_no: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=64)
    seat: Optional[str] = Field(default=None, max_length=32)
    group_name: Optional[str] = Field(default=None, max_length=64)


class PlayerImportOut(_Base):
    """批量导入选手的结果。

    这是**动作结果**，不是集合读取 —— 所以它**不用列表信封**（见
    ``docs/api-conventions.md`` §2：信封只属于 GET 集合）。
    返回 ``players`` 是因为调用方（导入界面）要拿到新建行的 id 才能
    接着做"给这几个人应用名单""批量配对"之类的动作。
    """

    created: int = 0
    updated: int = 0
    players: List[PlayerOut] = Field(default_factory=list)


class PlayerOut(_Base):
    id: int
    contest_id: int
    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None
    group_name: Optional[str] = None
    has_agent: bool = False
    online: bool = False
    file_count: int = 0
    last_tick_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# 题目
# --------------------------------------------------------------------------- #


class ProblemUpsert(_Base):
    """登记一道题。

    ``ident`` 会同时成为目录名、代码文件名与成绩矩阵的列名，
    所以要按路径段的规则校验（服务端会做）。

    ``file_patterns`` 是用于把回收的代码归到这道题的 glob 模式。
    留空表示用服务端的默认模式（出厂值 ``{ident}/**``）。
    """

    ident: str = Field(min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=200)
    order_index: int = Field(default=0, ge=0, le=9999)
    note: Optional[str] = Field(default=None, max_length=2000)
    file_patterns: List[str] = Field(default_factory=list)


class ProblemOut(_Base):
    id: int
    contest_id: int
    ident: str
    title: Optional[str] = None
    order_index: int = 0
    note: Optional[str] = None
    #: 实际生效的模式（服务端把默认值补上，前端不用自己推）
    file_patterns: List[str] = Field(default_factory=list)


class ProblemImportOut(_Base):
    created: int = 0
    updated: int = 0
    problems: List[ProblemOut] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class ProblemMatchIn(_Base):
    """拿一条相对路径来试算归题结果。

    界面上的「这条路径会算成哪道题」走的是**服务端同一套匹配代码** ——
    前端再实现一遍 glob 迟早会和服务端说不到一块去，而教师没法判断
    是配置错了还是界面显示错了。
    """

    path: str = Field(min_length=1, max_length=1024)


class ProblemMatchOut(_Base):
    path: str
    #: 命中的题目标识；``None`` 表示按当前模式没有任何题目认领这条路径
    problem: Optional[str] = None
    #: 胜出题目的模式**模板**（``{ident}`` / ``{title}`` 原样保留）
    patterns: List[str] = Field(default_factory=list)
    #: 上面那些模式展开后的样子。排错时要看的正是这一步 ——
    #: 教师写的是 ``{title}/**``，实际匹配的是 ``签到题/**``，两个都得看到。
    expanded: List[str] = Field(default_factory=list)


class AgentRuntimeOut(_Base):
    agent_id: int
    player_id: int
    contest_id: int
    contest_slug: str = ""
    player_no: str
    player_name: Optional[str] = None
    machine_id: str
    hostname: Optional[str] = None
    agent_version: Optional[str] = None
    scan_root: Optional[str] = None
    online: bool
    last_tick_at: Optional[str] = None
    seconds_since_tick: Optional[float] = None
    last_seen_ip: Optional[str] = None
    file_count: int = 0
    disk_free: Optional[int] = None
    last_error: Optional[str] = None
    tick_count: int = 0


class EventOut(_Base):
    id: int
    ts: str
    level: str
    category: str
    player_no: Optional[str] = None
    message: str
    meta: Optional[Dict[str, Any]] = None


class SourceFileOut(_Base):
    id: int
    player_id: int
    player_no: str
    rel_path: str
    sha256: str
    size: int
    revision: int
    content_stored: bool
    first_seen_at: str
    last_seen_at: str
    deleted_at: Optional[str] = None
    #: 归属的题目 ``ident``；``None`` 表示按当前模式没能归类。
    #: 只是**提示**，不影响文件的收发与存储 —— 教师改完模式不需要重收文件。
    problem: Optional[str] = None


# --------------------------------------------------------------------------- #
# 文件下发
# --------------------------------------------------------------------------- #


class AssetOut(_Base):
    id: int
    contest_id: int
    sha256: str
    size: int
    filename: str
    kind: str
    created_at: str
    #: 还有几个选手**没下载完**这个资产（下发目标里处于 pending/ready 的数量）。
    #: 界面靠它说清"改名/删除会对谁生效"：已经落地的文件不受影响，
    #: 没落地的会按新名字落地 —— 不说的话教师只能靠猜。
    pending_targets: int = 0


class AssetRenameIn(_Base):
    """只改显示名。内容是按 sha256 存的，改名不触碰到内容。"""

    filename: str = Field(min_length=1, max_length=255)


class DeployCreate(_Base):
    """创建下发任务。

    ``target_kind`` 取 ``all`` / ``player`` / ``group``：
    - ``all``    全量下发，忽略 ``player_ids``
    - ``player`` 只发给 ``player_ids`` 列出的选手
    - ``group``  发给 ``group_name`` 等于 ``target_group`` 的选手
    """

    asset_id: int
    target_kind: str = Field(default="all", max_length=16)
    player_ids: List[int] = Field(default_factory=list)
    target_group: Optional[str] = Field(default=None, max_length=64)
    dest_dir: str = Field(default="", max_length=512)
    mode: str = Field(default="overwrite", max_length=16)


class DeployTargetOut(_Base):
    player_id: int
    player_no: str
    status: str
    bytes_done: int = 0
    retry_count: int = 0
    last_error: Optional[str] = None


class DeployTaskOut(_Base):
    id: int
    contest_id: int
    asset_id: int
    filename: str
    size: int
    sha256: str
    target_kind: str
    dest_dir: str
    mode: str
    status: str
    created_at: str
    total: int = 0
    done: int = 0
    failed: int = 0
    pending: int = 0
    targets: List[DeployTargetOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 评测成绩
# --------------------------------------------------------------------------- #


class ScoreCellOut(_Base):
    problem: str
    score: Optional[int] = None
    max_score: Optional[int] = None
    status: Optional[str] = None
    #: ok / unparsed / manual / missing（从未收到结果）
    parse_status: str = "missing"
    #: 这一格对应的题目**有没有收到过代码**。
    #:
    #: ``parse_status="missing"`` 时才有区分意义：收了代码但没出成绩 = 评测还没跑完，
    #: 界面该显示"已交未评测"；压根没收 = "未交"。这两件事对教师的含义完全不同。
    submitted: bool = False
    detail: Optional[str] = None
    updated_at: Optional[str] = None


class ScoreRowOut(_Base):
    player_id: int
    player_no: str
    player_name: Optional[str] = None
    total: int = 0
    cells: List[ScoreCellOut] = Field(default_factory=list)


class ProblemColumnOut(_Base):
    """成绩矩阵的一列。

    ``declared=False`` 表示这个题目**只在评测结果里出现过，没在题目清单里登记**。
    仍然显示出来，避免因为漏登记而丢掉真实成绩；界面上应当标注提醒教师去补登记。
    """

    ident: str
    title: Optional[str] = None
    declared: bool = True


class ScoreMatrixOut(_Base):
    contest_id: int
    columns: List[ProblemColumnOut] = Field(default_factory=list)
    rows: List[ScoreRowOut] = Field(default_factory=list)
    #: 尚未解析出成绩的单元格数 —— 界面应当把它显示成待办而不是"0 分"
    unparsed: int = 0
    complete: bool = True


class ManualScoreIn(_Base):
    """教师手工录入 / 修正成绩。

    未解析的文件靠它兜底 —— 我们无法覆盖所有评测器格式，但绝不能因此让教师
    只能干瞪眼。
    """

    player_id: int
    problem: str = Field(min_length=1, max_length=64)
    score: Optional[int] = Field(default=None, ge=0)
    max_score: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = Field(default=None, max_length=32)


class JudgeRunOut(_Base):
    id: int
    player_id: int
    player_no: str
    problem: str
    score: Optional[int] = None
    max_score: Optional[int] = None
    status: Optional[str] = None
    parse_status: str
    detail: Optional[str] = None
    source_path: Optional[str] = None
    updated_at: str


class JudgeScanOut(_Base):
    parsed: int = 0
    unparsed: int = 0
    unchanged: int = 0
    skipped: int = 0
    #: 因教师手工录入而被保留（未覆盖）的条目数
    manual: int = 0
    errors: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agent 发布与自更新
# --------------------------------------------------------------------------- #


class ReleaseOut(_Base):
    id: int
    version: str
    channel: str
    sha256: str
    size: int
    notes: Optional[str] = None
    #: 已铺开（会对 Agent 下发）
    rolled_out: bool = False
    yanked: bool = False
    created_at: str
    published_at: Optional[str] = None
    key_id: Optional[str] = None


class ReleaseUpdate(_Base):
    notes: Optional[str] = Field(default=None, max_length=2000)
    channel: Optional[str] = Field(default=None, max_length=16)


class UpgradeStatusOut(_Base):
    """自更新总览。教师一眼看清"现在会不会有机器被升级"。"""

    signing_available: bool
    key_id: Optional[str] = None
    error: Optional[str] = None
    active_release: Optional[ReleaseOut] = None
    releases: List[ReleaseOut] = Field(default_factory=list)
