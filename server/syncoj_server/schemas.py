"""API 请求 / 响应模型（pydantic v2）。

这些模型是 **协议的唯一真相源**：FastAPI 由它们导出 OpenAPI，前端 TS 类型从
OpenAPI 生成；Agent 侧因为必须零依赖（不能 import pydantic），改用
``agent/tools/build_fixture.py`` 生成真实 payload，再由契约测试反向校验。

字段命名与 `docs/protocol.md` 必须逐字对应。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
]

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


class EnrollRequest(_Base):
    """注册请求。

    两种凭据二选一：

    * ``enroll_code`` —— 每选手一个注册码（原有行为），一码一人
    * ``bootstrap_key`` —— 镜像内置的统一密钥，注册出来的机器**没有归属**，
      要靠短码配对认领到人

    两个都传时以 ``enroll_code`` 为准：单人码是更明确的意图（"这台机器就是
    某个具体选手"），不该被镜像里那份宽泛的密钥盖过去。
    """

    machine_id: str = Field(min_length=1, max_length=128)
    hostname: Optional[str] = Field(default=None, max_length=128)
    agent_version: Optional[str] = Field(default=None, max_length=32)
    os_info: Optional[str] = Field(default=None, max_length=200)

    enroll_code: Optional[str] = Field(default=None, max_length=64)
    bootstrap_key: Optional[str] = Field(default=None, max_length=256)

    #: Agent 首次运行时自己生成的 UUID。比 ``/etc/machine-id`` 更适合当身份 ——
    #: 克隆镜像没做通用化时 machine-id 是整批相同的
    machine_uuid: Optional[str] = Field(default=None, max_length=64)
    #: 硬件指纹。**快照还原后仍然不变**，用来认回原来那台机器与它已认领的选手
    machine_fingerprint: Optional[str] = Field(default=None, max_length=128)


class EnrollResponse(_Base):
    """注册结果。

    ``claimed=False`` 表示这是一台**还没有归属**的机器（走统一密钥注册的）。
    此时 Agent 该做的是把 ``pair_code`` 显示给人看、然后等着被认领，
    而不是去扫代码 —— 它连准考证号都不知道，扫出来的路径没有意义。

    ``player_no`` 等在未认领时为空串，客户端必须按 ``claimed`` 分支处理。
    """

    token: str
    agent_id: int
    claimed: bool = True
    pair_code: Optional[str] = None
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

    #: 这台机器是否已经认领到选手。走统一密钥注册、还没配对时为 False
    claimed: bool = True
    #: 未认领时的配对短码。Agent 要把它显示给人看（桌面文件 + 日志）
    pair_code: Optional[str] = None
    #: 认领完成后回填的身份，Agent 拿到就把它存进凭据里，之后才能扫代码
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
    #: 见 ``EnrollmentMode``。留空 = 每选手注册码（原有行为）
    enrollment_mode: Optional[str] = Field(default=None, max_length=24)


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
    #: 实际生效的注册方式。老数据可能没这个字段，所以由服务端算好再下发
    enrollment_mode: str = "per_player_code"


class ContestUpdate(_Base):
    """场次的可改字段。全部可选 —— 只改传了的。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[str] = Field(default=None, max_length=16)
    note: Optional[str] = None
    default_roster_id: Optional[int] = None
    enrollment_mode: Optional[str] = Field(default=None, max_length=24)
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
    """一台注册上来、还没认领到人的机器。"""

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
    #: 同一硬件指纹上还有几台机器。>0 说明疑似克隆镜像，界面要报警
    fingerprint_peers: int = 0


class ClaimMachineIn(_Base):
    """认领一台机器。

    ``pair_code`` 可以省略（教师从列表里按主机名直接认领时就省略），
    但只要给了就必须对得上 —— 那是"我确认过这台机器就是那台"的凭据。
    """

    player_id: int
    pair_code: Optional[str] = Field(default=None, max_length=32)


class ClaimByCodeIn(_Base):
    """用配对码认领：机器上显示什么，教师就输什么。"""

    pair_code: str = Field(min_length=1, max_length=32)
    player_id: int


class RebindAgentIn(_Base):
    """把一台机器改派给另一位选手。"""

    player_id: int


class CloneAlertOut(_Base):
    """克隆镜像告警：多台机器共用同一个硬件指纹。"""

    fingerprint: str
    machine_count: int
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


class EnrollCodeOut(_Base):
    player_id: int
    player_no: str
    code: str
    expires_at: Optional[str] = None
    note: Optional[str] = None


class EnrollCodeStateOut(_Base):
    """一把注册码的**状态**，不含明文。

    库里只有哈希，明文只在签发那一刻出现过 —— 所以这里回答的是
    "谁手上还有一把能用的钥匙、谁已经用过了"，而不是"那把钥匙长什么样"。
    """

    id: int
    player_id: int
    player_no: str
    #: 已经绑定的机器。为空表示这把码还没被用过
    bound_machine_id: Optional[str] = None
    #: 还能不能用来注册
    usable: bool = True
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None


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
