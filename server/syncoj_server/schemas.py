"""API 请求 / 响应模型（pydantic v2）。

这些模型是 **协议的唯一真相源**：FastAPI 由它们导出 OpenAPI，前端 TS 类型从
OpenAPI 生成；Agent 侧因为必须零依赖（不能 import pydantic），改用
``agent/tools/build_fixture.py`` 生成真实 payload，再由契约测试反向校验。

字段命名与 `docs/reference/protocol.md` 必须逐字对应。
"""

from __future__ import annotations

from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnrollRequest",
    "EnrollResponse",
    "ScanEntry",
    "TickStats",
    "TickRequest",
    "DeployJob",
    "UpgradeInfo",
    "TickResponse",
    "DiagnosticsUploadOut",
    "UploadResult",
    "AgentEvent",
    "SimpleAck",
    "WhoAmIOut",
    "AnnounceStatusOut",
    "DiagnosticsOut",
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
    Python 切片（见 ``docs/reference/api-conventions.md`` §2）。
    """
    return Page(
        items=items, total=int(total), limit=int(params.limit), offset=int(params.offset)
    )


def slice_page(rows: List[T], params: Any) -> "Page[T]":
    """对有界集合用 Python 切片分页。

    只在"天然就很小"、且要在 Python 里拼字段的集合上用（场次要算在线人数、题目…）。
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

    #: 协议版本（见 ``syncoj_server.protocol``）。缺省 0 = 没有这个字段的旧 Agent
    protocol_version: int = Field(default=0, ge=0, le=10000)
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


class DeployProgress(_Base):
    """一个下发目标在本机的进度（分片里已经有多少字节）。"""

    target_id: int
    bytes_done: int = Field(ge=0)


class DeployDone(_Base):
    """一个下发目标已经在本机落地，落地的是**哪一份内容**。

    带 ``sha256`` 是为了让服务端只认"当前这一版"的回执：教师在线改了正文 /
    改了 zip 密码之后，迟到的旧版回执不能把新内容标成已完成。
    """

    target_id: int
    sha256: str = Field(min_length=64, max_length=64)


class DeployFailed(_Base):
    """一个下发目标本轮没下成，以及原因（给教师看的）。"""

    target_id: int
    reason: str = Field(default="", max_length=300)


class TickStats(_Base):
    disk_free: Optional[int] = Field(default=None, ge=0)
    last_error: Optional[str] = Field(default=None, max_length=1024)
    queue: int = Field(default=0, ge=0)
    #: 本机上**当前不存在**的扫描根（绝对路径，最多 5 条，由 Agent 每轮上报）。
    #:
    #: 存在的理由是"选手目录还没建"这类问题在服务端完全看不见：扫描根不存在时
    #: Agent 扫出来是零个文件，而那与"选手还没开始写"长得一模一样。教师需要的是
    #: "这台机器的目录还没建"，而不是等考完才发现一份代码都没收到。
    #:
    #: 缺省空列表：老版本 Agent 不报这个字段，而"没报"与"没有缺失"在这里正好是
    #: 同一个结论（没有已知的缺失）。
    scan_missing: List[str] = Field(default_factory=list)


class ScanIdentity(_Base):
    """这一轮的扫描根是按哪个身份展开的（Agent 的 ``{player_no}`` / ``{contest_slug}``）。"""

    player_no: str = Field(default="", max_length=64)
    contest_slug: str = Field(default="", max_length=128)


class TickRequest(_Base):
    #: 协议版本（见 ``syncoj_server.protocol``）。缺省 0 = 没有这个字段的旧 Agent
    protocol_version: int = Field(default=0, ge=0, le=10000)
    agent_version: Optional[str] = Field(default=None, max_length=32)
    machine_id: str = Field(min_length=1, max_length=128)
    #: 主机名。每次 tick 都报 —— 待配对的机器要靠它在列表里被认出来，
    #: 而"改名"这件事在考场上是会发生的（教师按座位重命名机器）
    hostname: Optional[str] = Field(default=None, max_length=128)
    ts: int = Field(ge=0)
    scan_root: Optional[str] = Field(default=None, max_length=512)
    #: 上限是兜底的形状约束（请求体大小另有闸门）：服务端的 max_files_per_scan
    #: 默认 5000，超出部分本来就会被截断；这里挡的是"几十万条先被逐条校验一遍"。
    scan: List[ScanEntry] = Field(default_factory=list, max_length=50000)
    #: 本次扫描是否完整覆盖了扫描根目录。
    #: 若 Agent 中途遇到 PermissionError 等错误导致扫描不完整，必须置 False，
    #: 服务端会因此**跳过删除判定** —— 否则会把"没扫到"误判成"被删了"。
    #:
    #: **必填**：它曾经缺省为 True —— 漏写这个字段的报文被当成"完整扫描"，
    #: 恰好是会误删的那一边。报文必须自己说清楚。
    scan_complete: bool
    #: 下发作业**按目标**（``target_id``）回报。曾经按 ``asset_id``：同一资产发到
    #: 同一个人的两个目录时，一条回执把两个目标一起标完成（另一个其实没落地）。
    #:
    #: * ``deploy_progress``：本机分片里已有多少字节（管理端的进度条）；
    #: * ``deploy_done``：已落地，连同落地内容的 sha256 —— 服务端只认与资产当前
    #:   内容一致的回执；
    #: * ``deploy_failed``：本轮没下成及原因 —— 连续失败若干次服务端判 FAILED，
    #:   教师在管理端看得见、点得了重试。
    #:
    #: 刻意由 Agent 显式上报"完成"，而不是服务端从"没有进度了"去推断 ——
    #: "分片不见了"既可能是下完了，也可能是被清理了。
    deploy_progress: List[DeployProgress] = Field(default_factory=list, max_length=1000)
    deploy_done: List[DeployDone] = Field(default_factory=list, max_length=1000)
    deploy_failed: List[DeployFailed] = Field(default_factory=list, max_length=1000)
    #: 这台机器上**有没有可用的发布公钥**（``/etc/syncoj/release-key.pub.json``，
    #: 也就是升级信任锚）。
    #:
    #: 它只上报"在不在"，不含任何密钥内容：服务端要在下发卸载授权之前知道
    #: "那台机器到底验不验得了签名"，而在没有公钥的机器上发授权是白费 ——
    #: 它一定会拒收，教师看到的却是"操作成功"。
    #:
    #: 缺省 ``False`` 是有意的：老版本 Agent 不报这个字段，而"没报"与"没有"
    #: 在这里该得到同一个结论（当作验不了），不能乐观地假定它有。
    release_public_key: bool = False
    #: 这一轮的扫描根按哪个身份展开。与服务端此刻解析出的身份不同 = 机器刚被
    #: 改派 / 换了场次，这份扫描是**上一个人**的目录：服务端拒收本轮对账（不把
    #: 它记进新身份的台账、不要求上传），响应里带上新身份，Agent 重算扫描根。
    #: 缺省（老 Agent、测试夹具）按"一致"处理。
    scanned_for: Optional[ScanIdentity] = None
    stats: TickStats = Field(default_factory=TickStats)


class DeployJob(_Base):
    #: 回执与进度都按它报（不是 asset_id —— 同一资产可以有多个目标）
    target_id: int
    asset_id: int
    url: str
    sha256: str
    size: int
    dest: str
    #: 曾经还有一个 ``offset``（服务端回填的续传位置）。续传位置以 Agent 本地分片
    #: 为准，服务端那份没人用；更糟的是"本地分片满长"时服务端据它**跳过派发**，
    #: 而 Agent 只在收到作业时才会完成最后的落盘 —— 任务永远卡在进行中。
    mode: str = "overwrite"


class UpgradeInfo(_Base):
    version: str
    url: str
    sha256: str
    signature: str
    size: int = 0
    notes: Optional[str] = None
    #: 曾经还带三条安装策略（bootstrap_key_policy / config_policy / upgrade_mode）。
    #: Agent 从来不读它们 —— 升级时生效的是包内 ``install_policy.json``（与装机
    #: 台账同出 ``services/install_policy.build_install_policy``）。删掉，免得有人
    #: 以为改了这里就改了机器上的行为。


class TickResponse(_Base):
    server_time: int
    next_tick_seconds: int
    need_upload: List[str] = Field(default_factory=list)
    deploy_jobs: List[DeployJob] = Field(default_factory=list)
    upgrade: Optional[UpgradeInfo] = None
    #: 教师请求过卸载、且这台机器报过有发布公钥时，这里是一次性卸载授权令牌；
    #: 其余时候是 ``null``。Agent 拿它去验签，验过才执行删除。
    #:
    #: 同一枚令牌会在多轮心跳里原样重发（机器可能一次没收到、也可能写盘失败），
    #: 而教师**再点一次**必然换一枚新的。它没有单独的有效状态位：令牌存在本身
    #: 就等于"教师点过"，过期后服务端自然不再下发。
    uninstall_token: Optional[str] = None
    #: 教师点过「要一份诊断」且这台机器这一轮把它取走了：``true`` 只出现**一次**，
    #: 服务端在返回它的同时就把标记清掉（见 ``api/agent.py`` 的
    #: ``_take_diagnostics_request``）。机器没收到就让教师再点一次 —— 不重发。
    diagnostics_request: bool = False
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
    #: 服务端实例 ID（``<data_root>/server-id``，跨重启不变）。Agent 记住它，之后
    #: 局域网发现 / 主动通告只切到同一个 ID 的服务端（§7.3）。老 Agent 忽略
    server_uuid: Optional[str] = None


class UploadResult(_Base):
    path: str
    sha256: str
    revision: int
    stored: bool


class DiagnosticsUploadOut(_Base):
    """Agent 诊断包上报成功的回执（``POST /api/v1/agent/diagnostics``）。

    ``received_at`` 是服务端收到它的时刻（ISO-8601，UTC）；``bytes`` 是
    **解压后 JSON 文本**的字节数 —— 与库里那一列、以及管理端读回来的
    ``bytes`` 是同一个数（不是压缩后的大小）。
    """

    received_at: str
    bytes: int = 0


class AgentEvent(_Base):
    level: str = Field(default="info", max_length=16)
    category: str = Field(max_length=32)
    message: str = Field(max_length=2000)
    meta: Optional[Dict[str, Any]] = None


class WhoAmIOut(_Base):
    """挑战应答（§11）：用发布私钥签 ``syncoj-announce-v1\n<nonce>\n<url>``（v2 见下）。

    ``url`` 是**服务端自己认定的**对外地址（§7.3 的三条来源），不是请求里的 Host：
    机器切换到的是签名里的这个地址。
    """

    url: str
    nonce: str
    key_id: str = ""
    sig: str
    #: 请求带 ``v=2`` 且服务端有实例 ID 时才有：此时签名对象是
    #: ``syncoj-announce-v2\n<nonce>\n<url>\n<server_uuid>``
    server_uuid: Optional[str] = None


class AnnounceStatusOut(_Base):
    """服务端主动通告的现状（管理端「召集机器」用）。"""

    enabled: bool
    port: int
    boot_id: str
    #: 最近一次成功通告的时间（UTC ISO）与地址；还没喊过是 None
    last_sent_at: Optional[str] = None
    last_url: Optional[str] = None
    #: 喊不出去的原因（没有私钥、不知道自己的地址……）；正常时 None
    last_error: Optional[str] = None


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
    """清代码台账（硬删，见 ``docs/reference/api-conventions.md`` §5.3）。

    ``purge=False``（默认）只删扫描对账标成"已消失"的行（``deleted_at`` 非空）。
    ``purge=True`` 连还在的行一起删 —— 机器还在报的文件下一轮会重新收上来，
    所以只在"归档完毕准备清场"或"这些记录本来就是误传"时才该用。
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
    #: 考试时间窗，ISO-8601 字符串。**留空 = 不限制**（这是默认，练习场与
    #: 还没定时间的场次都不该被时间门禁挡住）。只填一个也合法：只填
    #: ``starts_at`` 就只管开考，只填 ``ends_at`` 就只管结束。
    #: 服务端会拒绝 ``starts_at > ends_at`` 那种永远不可能成立的窗口。
    starts_at: Optional[str] = Field(default=None, max_length=32)
    ends_at: Optional[str] = Field(default=None, max_length=32)
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
    #: 考试时间窗（ISO-8601，带 ``Z``）。**必须回显**，理由同上：教师填完两个
    #: 时间点再打开对话框，看到的必须是刚才填的那两个值，而不是空。
    #: ``null`` = 这一端不限制。
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    #: 自带评测的自动入队（§12.9）：``off`` / ``on_change`` / ``smart``（§12.12）；去抖秒数
    auto_judge: str = "off"
    auto_judge_delay: int = 60
    #: 智能模式的阈值（§12.12；没设过的回显默认值）
    smart_stable_seconds: int = 180
    smart_idle_seconds: int = 600
    smart_near_end_seconds: int = 900
    smart_cooldown_seconds: int = 300
    smart_queue_factor: int = 20
    #: 是否设了考生页密码。密码本身**只写不读**（库里也只有哈希）。
    has_player_password: bool = False


class ContestUpdate(_Base):
    """场次的可改字段。全部可选 —— 只改传了的。

    时间窗这里的"传了 ``null``"和"没传"是两件事，靠 pydantic 的
    ``model_fields_set`` 区分（见 ``api/admin.py``）：前者是"清掉这个限制"，
    后者是"这次别动它"。只按 ``is not None`` 判断的话，教师永远清不掉一个
    填错的时间，而界面上看起来是保存成功了。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[str] = Field(default=None, max_length=16)
    note: Optional[str] = None
    starts_at: Optional[str] = Field(default=None, max_length=32)
    ends_at: Optional[str] = Field(default=None, max_length=32)
    default_roster_id: Optional[int] = None
    #: 显式置空用的开关。``None`` 字段没法区分"没传"和"传了 null"，
    #: 所以要多一个布尔 —— 否则教师永远清不掉已经选错的名单。
    clear_default_roster: bool = False
    #: 考生页密码。给了就**替换**（存 scrypt 哈希）；不给 = 不动。
    #: 设了它，考生能在考生页用「准考证号 + 这个密码」看到自己的清单并下载文件。
    player_password: Optional[str] = Field(default=None, min_length=4, max_length=128)
    #: 清除考生页密码（关掉那条登录路径，已签出的令牌随之失效）。
    #: 与 ``player_password`` 同时给时以清除为准。
    clear_player_password: bool = False


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


class AdminHealthOut(_Base):
    """管理端探活：概览卡片直接要用的几个数。"""

    ok: bool
    agents_total: int
    agents_online: int
    data_root: str


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
    #: 其他名单里与这个人**同名**的条目（智能匹配，§4.6）：界面一键「同时绑定」
    suggestions: List["BindingSuggestionOut"] = Field(default_factory=list)


class BindingSuggestionOut(_Base):
    """一条同名建议：把 ``agent_id`` 也绑到 ``roster_name`` 里的这个人。"""

    agent_id: int
    roster_id: int
    roster_name: str = ""
    roster_entry_id: int
    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None
    #: 依据：机器在哪份名单里绑着的谁（"名单 · 考号 姓名"）
    source: str = ""


class BoundMachineOut(_Base):
    """已配对的机器（至少绑了一份名单）。"""

    id: int
    machine_id: str
    hostname: Optional[str] = None
    agent_version: Optional[str] = None
    contest_id: Optional[int] = None
    last_seen_at: Optional[str] = None
    bindings: List[AgentBindingOut] = Field(default_factory=list)


class RosterMatchPairOut(_Base):
    """「按姓名匹配机器」预览里的一对。"""

    agent_id: int
    hostname: Optional[str] = None
    machine_id: str = ""
    roster_entry_id: int
    player_no: str
    name: Optional[str] = None
    #: 依据：机器在哪份名单里绑着的谁
    source: str = ""


class RosterMatchSkipOut(_Base):
    """没有配成对的人及原因。"""

    roster_entry_id: int
    player_no: str
    name: Optional[str] = None
    reason: str


class RosterMatchPreviewOut(_Base):
    roster_id: int
    roster_name: str
    pairs: List[RosterMatchPairOut] = Field(default_factory=list)
    conflicts: List[RosterMatchSkipOut] = Field(default_factory=list)
    skipped: List[RosterMatchSkipOut] = Field(default_factory=list)


class RosterMatchPairIn(_Base):
    agent_id: int
    roster_entry_id: int


class RosterMatchApplyIn(_Base):
    """教师在预览里确认过的那些对。逐对重新校验，失败的列进 ``failed``。"""

    pairs: List[RosterMatchPairIn] = Field(default_factory=list, max_length=5000)


class RosterMatchApplyOut(_Base):
    bound: int = 0
    failed: List[RosterMatchSkipOut] = Field(default_factory=list)


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
    ``docs/reference/api-conventions.md`` §2：信封只属于 GET 集合）。
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
    留空表示用服务端的默认模式（出厂值 ``{ident}/{ident}.cpp``：只认"题目目录下
    与题目同名的那个源文件"。要连整个题目目录一起收，显式写 ``{ident}/**``）。
    """

    ident: str = Field(min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=200)
    order_index: int = Field(default=0, ge=0, le=9999)
    note: Optional[str] = Field(default=None, max_length=2000)
    file_patterns: List[str] = Field(default_factory=list)


class ProblemTestdataOut(_Base):
    """题目的数据包（§12）：上传时校验出的摘要。"""

    name: str
    size: int
    sha256: str
    cases: int
    subtasks: int = 0
    has_checker: bool = False
    total_score: int = 0


class ProblemOut(_Base):
    id: int
    contest_id: int
    ident: str
    title: Optional[str] = None
    order_index: int = 0
    note: Optional[str] = None
    #: 实际生效的模式（服务端把默认值补上，前端不用自己推）
    file_patterns: List[str] = Field(default_factory=list)
    #: external = 外部评测器写 judge_result/；builtin = 自带评测（§12）
    judge_mode: str = "external"
    #: 空 = 用默认值（1000 ms / 256 MB）
    time_limit_ms: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    io_mode: str = "stdio"
    #: 文件 IO 的输入/输出文件名；空 = <ident>.in / <ident>.out
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None
    testdata: Optional[ProblemTestdataOut] = None


class ProblemJudgeIn(_Base):
    """题目的自带评测配置（§12）。``compile_flags`` 只接受白名单里的几类参数（服务端校验）。"""

    judge_mode: Literal["external", "builtin"] = "external"
    time_limit_ms: Optional[int] = Field(default=None, ge=100, le=60000)
    memory_limit_mb: Optional[int] = Field(default=None, ge=16, le=4096)
    io_mode: Literal["stdio", "file"] = "stdio"
    #: 文件 IO 的输入/输出文件名；不传或留空 = <ident>.in / <ident>.out
    input_file: Optional[str] = Field(default=None, max_length=64)
    output_file: Optional[str] = Field(default=None, max_length=64)
    compile_flags: Optional[str] = Field(default=None, max_length=200)


class BuiltinJudgeIn(_Base):
    """排自带评测任务。都不填 = 全场所有用自带评测的题 × 所有选手（评最新版）。

    给了 ``version_id`` = 重测那一个历史版本（§12.9）：选手与题目由版本定，结果只进历史与
    timeline，不覆盖成绩矩阵（教师「采用」才写）；不受"场次还在进行"的限制。
    """

    problem: Optional[str] = Field(default=None, max_length=64)
    player_id: Optional[int] = None
    #: 场次还在进行时也评（评的是考到一半的代码）
    force: bool = False
    #: 指定历史版本（``GET .../judge/versions`` 的 ``id``）
    version_id: Optional[int] = None
    #: 真跑一遍：不复用相同 (数据包, 评测配置, 代码) 的已有结果
    rerun: bool = False


class BuiltinJudgeQueuedOut(_Base):
    queued: int
    problems: List[str] = Field(default_factory=list)


class BuiltinJudgeFailureOut(_Base):
    player_no: str
    problem: str
    error: str


class BuiltinJudgeProgressOut(_Base):
    """排队 / 在评是全数；完成 / 失败只数每 (选手, 题目) 最新的那条任务。"""

    total: int = 0
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    #: 排队中、还在去抖（没到 ``not_before``）的自动任务数
    delayed: int = 0
    failures: List[BuiltinJudgeFailureOut] = Field(default_factory=list)


class JudgeAutoIn(_Base):
    """场次的自动入队设置（§12.9、§12.12）。智能模式的阈值不传 = 不改。"""

    auto_judge: Literal["off", "on_change", "smart"]
    #: 去抖秒数：同一 (选手, 题目) 在这段时间内连续改动只评最后一版（``on_change`` 用）
    auto_judge_delay: int = Field(default=60, ge=0, le=3600)
    #: 智能：最新版本稳定这么久、且之后改了别的题 → 排（「换题」）
    smart_stable_seconds: Optional[int] = Field(default=None, ge=30, le=3600)
    #: 智能：最新版本稳定这么久 → 排（「空闲」）
    smart_idle_seconds: Optional[int] = Field(default=None, ge=60, le=7200)
    #: 智能：距结束时间不超过这么久时，未评的最新版本各排一次（「将结束」）；0 = 不用
    smart_near_end_seconds: Optional[int] = Field(default=None, ge=0, le=7200)
    #: 智能：同一 (选手, 题目) 两次「换题」/「空闲」排任务的最短间隔
    smart_cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    #: 智能：排队任务数超过「在线评测机并发总数 × 它」时只接受将结束 / 掉线 / 已结束
    smart_queue_factor: Optional[int] = Field(default=None, ge=1, le=1000)


class JudgeAutoOut(_Base):
    auto_judge: str
    auto_judge_delay: int
    smart_stable_seconds: int
    smart_idle_seconds: int
    smart_near_end_seconds: int
    smart_cooldown_seconds: int
    smart_queue_factor: int


class JudgeTaskOut(_Base):
    """一个评测任务（任务即历史，§12.9）。"""

    id: int
    player_id: int
    player_no: str
    problem: str
    #: queued / running / done / failed
    status: str
    #: auto / smart / manual / rejudge
    trigger: str
    #: 智能模式的信号（§12.12）：moved_on / long_stable / near_end / offline / contest_end；其余为 null
    reason: Optional[str] = None
    #: 领任务的优先级，大的先领
    priority: int = 0
    requested_by: Optional[str] = None
    source_version_id: Optional[int] = None
    source_path: Optional[str] = None
    source_sha256: Optional[str] = None
    score: Optional[int] = None
    max_score: Optional[int] = None
    #: 评测状态（AC / WA …），没交为 ``missing``
    outcome: Optional[str] = None
    error: Optional[str] = None
    #: 结果抄自哪个任务（没占评测机）
    reused_from: Optional[int] = None
    rerun: bool = False
    #: 结果写进过 result.json（自动写入或被采用）
    applied: bool = False
    #: 成绩矩阵当前这一格就是这个任务的结果
    in_matrix: bool = False
    has_result: bool = False
    created_at: str
    finished_at: Optional[str] = None
    #: 去抖：这个时刻之前不发给评测机
    not_before: Optional[str] = None
    #: 结果里疑似违规 / 风险的条数（§12.11；分级见 ``precheck.level_of``）
    warning_count: int = 0
    risk_count: int = 0


class JudgeTaskDetailOut(JudgeTaskOut):
    """任务连同结果里的测试点明细（形状同 ``JudgeRunDetailOut`` 的明细部分）。"""

    judge: Optional[str] = None
    language: Optional[str] = None
    isolated: Optional[bool] = None
    compile_message: str = ""
    judged_at: Optional[str] = None
    cases: List[JudgeCaseOut] = Field(default_factory=list)
    subtasks: List[JudgeSubtaskOut] = Field(default_factory=list)
    precheck_warnings: List[JudgePrecheckFindingOut] = Field(default_factory=list)
    risk_events: List[JudgeRiskEventOut] = Field(default_factory=list)
    truncated: bool = False


class JudgeSourceOut(_Base):
    """评测任务评的那份代码（按任务记下的 sha256 从内容存储取，§12.11）。"""

    task_id: int
    path: Optional[str] = None
    sha256: str
    size: int
    #: UTF-8 解码（坏字节替换）后的全文
    content: str


class JudgeServiceIn(_Base):
    """「评测服务」开关：关 = 所有评测机都领不到新任务（手上的照常评完，排队的留着）。"""

    enabled: bool


class JudgeServiceOut(_Base):
    enabled: bool = True
    #: 全部场次排队中的任务数（含还在去抖的）
    queued: int = 0
    #: 其中还在去抖、没到点的
    delayed: int = 0
    running: int = 0


class VersionJudgeOut(_Base):
    """一个版本最近一次评测的摘要。"""

    task_id: int
    status: str
    trigger: str
    #: 智能模式的信号（§12.12），其余触发方式为 null
    reason: Optional[str] = None
    score: Optional[int] = None
    max_score: Optional[int] = None
    outcome: Optional[str] = None
    reused: bool = False
    applied: bool = False
    finished_at: Optional[str] = None


class SourceVersionOut(_Base):
    """代码的一个历史版本（§12.9），连同它的评测结果。"""

    id: int
    player_id: int
    rel_path: str
    sha256: str
    size: int = 0
    received_at: str
    #: 按**当前**题目模式归到的题；归不到为 null
    problem: Optional[str] = None
    #: 这一版就是台账里的当前版本
    current: bool = False
    #: 评过这一版的任务数
    tasks: int = 0
    judge: Optional[VersionJudgeOut] = None
    #: 成绩矩阵当前这一格用的是这一版的结果
    in_matrix: bool = False


class JudgeWorkerIn(_Base):
    name: str = Field(min_length=1, max_length=64)


class JudgeWorkerPatchIn(_Base):
    """改名 / 暂停与恢复。不给的字段不动。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    enabled: Optional[bool] = None
    #: 并发数（§12.10）：随 poll 下发，评测机评完手上的任务后以新的 ``--slots`` 重启。
    #: 后端不是 isolate 的只能是 1；旧评测机（不认下发的并发数）409
    slots: Optional[int] = Field(default=None, ge=1, le=64)


class JudgeWorkerTaskOut(_Base):
    """评测机手上正在评的一个任务。"""

    id: int
    contest_id: int
    player_no: str
    problem: str


class JudgeWorkerOut(_Base):
    id: int
    name: str
    created_at: str
    last_seen_at: Optional[str] = None
    last_seen_ip: Optional[str] = None
    #: 最近联系不超过轮询间隔的 3 倍（§12.10）
    online: bool = False
    #: 暂停 = False：领不到新任务，照常心跳
    enabled: bool = True
    #: ``run`` / ``stop`` / ``restart`` / ``uninstall``：后三者 = 已下令、等评测机取走；
    #: ``uninstall`` 一直保持到评测机告别（这一行随之删除）
    desired: Literal["run", "stop", "restart", "uninstall"] = "run"
    #: 服务端拉起的本机评测机
    local: bool = False
    #: 以下为评测机自报；旧评测机不报，全为空
    version: Optional[str] = None
    backend: Optional[str] = None
    isolated: Optional[bool] = None
    hostname: Optional[str] = None
    platform: Optional[str] = None
    slots: Optional[int] = None
    #: 管理端设定的并发数；与 ``slots`` 不同 = 等评测机评完手上的任务后重启生效
    desired_slots: Optional[int] = None
    #: 能收远程停止 / 重启（报过状态的评测机）
    remote_control: bool = False
    #: 能远程改并发数（评测机报了 ``slots`` 能力）
    slots_control: bool = False
    #: 能远程卸载（评测机报了 ``uninstall`` 能力；本机评测机恒为 true）
    uninstall_control: bool = False
    running: List[JudgeWorkerTaskOut] = Field(default_factory=list)


class JudgeWorkerCreatedOut(JudgeWorkerOut):
    #: 明文令牌，**只在这一刻返回一次**；库里只存哈希
    token: str
    #: 一行安装命令：Linux（bash + systemd）与 Windows（PowerShell + 计划任务），已嵌入令牌与服务端地址
    linux_command: str
    windows_command: str
    #: 已装好 syncoj-judge 时直接运行的命令（令牌写进文件后）
    manual_command: str


class JudgePollIn(_Base):
    """评测机的一次 poll：自报状态，``want`` = 1 时顺带领一个任务（§12.10）。"""

    version: Optional[str] = Field(default=None, max_length=32)
    backend: Optional[str] = Field(default=None, max_length=16)
    isolated: Optional[bool] = None
    hostname: Optional[str] = Field(default=None, max_length=128)
    platform: Optional[str] = Field(default=None, max_length=32)
    slots: int = Field(default=1, ge=1, le=64)
    poll_seconds: float = Field(default=5.0, ge=0.5, le=3600)
    want: int = Field(default=1, ge=0, le=1)
    #: 本进程的随机标识（每次启动换新）；不带 = 旧评测机，命令按一次性送达
    boot_id: Optional[str] = Field(default=None, max_length=32)
    #: 评测机的能力：``slots`` = 认响应里下发的并发数
    features: Optional[List[str]] = Field(default=None, max_length=16)


class JudgeGoodbyeIn(_Base):
    """评测机收到 ``uninstall``、评完手上的任务后的告别（§12.10）：服务端删掉这一行。"""

    #: 它接下来怎么清理自己：``systemd`` / ``schtask``（安装脚本装的）、``local``（本机评测机）、
    #: ``manual``（手动运行：只删令牌与缓存，程序文件要到那台机器上手动删）
    kind: Literal["systemd", "schtask", "local", "manual"] = "manual"


class LocalJudgeOut(_Base):
    """本机评测机（服务端以子进程运行的 worker）的状态。"""

    #: 只有 Linux 服务端支持；不支持时 ``reason`` 是一句短标签
    supported: bool
    reason: Optional[str] = None
    running: bool = False
    pid: Optional[int] = None
    backend: Optional[str] = None
    worker_id: Optional[int] = None
    started_at: Optional[str] = None
    #: 上一次退出的退出码（启动失败、被停止）
    exit_code: Optional[int] = None


class JudgeProblemConfigOut(_Base):
    ident: str
    time_limit_ms: int
    memory_limit_mb: int
    io_mode: str
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None


class JudgeBlobRefOut(_Base):
    sha256: str
    path: Optional[str] = None
    name: Optional[str] = None


class JudgeLeaseOut(_Base):
    """评测机领到的任务。两份内容（代码、数据包）用各自的 sha256 去取。"""

    id: int
    contest_slug: str
    player_no: str
    problem: JudgeProblemConfigOut
    source: JudgeBlobRefOut
    testdata: JudgeBlobRefOut
    attempt: int


class JudgePollOut(_Base):
    """``task`` 为空 = 没领到（没有任务、已暂停或 ``want`` = 0）。``command`` 见 §12.10。"""

    task: Optional[JudgeLeaseOut] = None
    command: Literal["run", "stop", "restart", "uninstall"] = "run"
    enabled: bool = True
    #: 管理端设定的并发数；与评测机当前的不同时评测机评完手上的任务、以它重启。null = 没设
    slots: Optional[int] = None
    #: 「评测服务」开关：关时不发任务
    service: bool = True


class JudgeResultIn(_Base):
    """评测机交回的结果：``score`` / ``max_score`` / ``status`` 进成绩矩阵，其余原样进 ``raw_json``。"""

    score: int = Field(ge=0, le=1000000)
    max_score: int = Field(ge=0, le=1000000)
    status: str = Field(min_length=1, max_length=16)
    detail: Dict[str, Any] = Field(default_factory=dict)


class JudgeFailIn(_Base):
    message: str = Field(default="", max_length=2000)


class ProblemImportOut(_Base):
    created: int = 0
    updated: int = 0
    problems: List[ProblemOut] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


# ---- 题目评测配置导入 / 导出 ----

#: 导入接受的格式；auto = 自动识别
JudgeImportFormat = Literal["auto", "syncoj", "hoj", "lemonlime", "hydro", "qduoj", "fps"]
#: 能导出的格式
JudgeExportFormat = Literal["syncoj", "hoj", "lemonlime", "hydro"]
JudgeImportAction = Literal["create", "update", "skip"]


class NoticeOut(_Base):
    """导入 / 导出的一条警告或错误。``message`` 是给人看的中文。"""

    code: str
    message: str


class JudgeImportItemOut(_Base):
    key: str
    source_name: str
    #: 建议的题目标识（应用时可改）
    ident: str
    title: Optional[str] = None
    exists: bool = False
    existing_problem_id: Optional[int] = None
    #: 默认动作：有错误 → skip；同名题已存在 → update；否则 create
    action: JudgeImportAction = "create"
    judge_mode: str = "builtin"
    time_limit_ms: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    io_mode: str = "stdio"
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None
    cases: int = 0
    subtasks: int = 0
    total_score: int = 0
    has_checker: bool = False
    warnings: List[NoticeOut] = Field(default_factory=list)
    errors: List[NoticeOut] = Field(default_factory=list)


class JudgeImportPreviewOut(_Base):
    token: str
    #: 识别出的格式 id
    format: str
    problems: List[JudgeImportItemOut] = Field(default_factory=list)
    #: 整包级提示
    warnings: List[NoticeOut] = Field(default_factory=list)
    #: 本场次已有的题目标识：界面改标识后据此在新建 / 覆盖之间切换
    existing_idents: List[str] = Field(default_factory=list)


class JudgeImportApplyItem(_Base):
    key: str = Field(min_length=1, max_length=200)
    ident: str = Field(min_length=1, max_length=64)
    action: JudgeImportAction


class JudgeImportApplyIn(_Base):
    token: str = Field(min_length=1, max_length=64)
    items: List[JudgeImportApplyItem] = Field(default_factory=list, max_length=200)
    #: 覆盖已有题目时是否把评测方式改为自带评测（新建的题一律自带评测；原生格式按包里的值）
    set_builtin: bool = True


class JudgeImportErrorOut(_Base):
    key: str
    message: str


class JudgeImportOut(_Base):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    problems: List[ProblemOut] = Field(default_factory=list)
    errors: List[JudgeImportErrorOut] = Field(default_factory=list)


class JudgeExportItemOut(_Base):
    problem_id: int
    ident: str
    #: 是否写进包里（非原生格式跳过没有数据包的题）
    included: bool
    warnings: List[NoticeOut] = Field(default_factory=list)


class JudgeExportPreviewOut(_Base):
    format: str
    problems: List[JudgeExportItemOut] = Field(default_factory=list)


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


class AgentBindingOut(_Base):
    """机器的一条绑定：名单 · 考号 · 姓名。"""

    roster_id: int
    roster_name: str = ""
    roster_entry_id: int
    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None


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
    #: 这台机器上**当前不存在**的扫描根（最近一次心跳报的，最多 5 条）。
    #:
    #: 页面上只写一句「选手目录还没建：<路径>」—— 它是"机器在跑但目录不在"这个
    #: 事实的唯一线索：那种情况下扫出来是零个文件，与"选手还没开始写"长得一模一样。
    scan_missing: List[str] = Field(default_factory=list)
    #: 最近一次心跳里**被题目预设挡掉**的文件数（0 = 没有被挡掉的）。
    #:
    #: 页面上写一句「有 N 个文件不符合题目预设，已跳过」：没有它，教师看到的是
    #: "这个学生一份代码都没交"，而那与"他把文件存错了地方"长得一模一样。
    scan_skipped: int = Field(default=0, ge=0)
    #: 这台机器在各份名单里的全部绑定（一机可绑多份名单，§4.6）
    bindings: List[AgentBindingOut] = Field(default_factory=list)


class DiagnosticsOut(_Base):
    """一台机器**最新一份**诊断包（管理端 ``GET .../diagnostics``）。

    ``content`` 直接给**解析好的对象**（服务端存的本来就是解压后的 JSON 文本，
    见 ``models.AgentDiagnostic``）。``bytes`` 是那份文本的字节数，
    ``reason`` 是 Agent 自报的原因（``periodic`` / ``error`` / ``manual``，
    可能为 ``null``）。
    """

    received_at: str
    reason: Optional[str] = None
    bytes: int = 0
    content: Dict[str, Any] = Field(default_factory=dict)


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
    #: 能不能在线上直接改正文。**由服务端算**（扩展名白名单 + 体积上限），
    #: 前端不许按扩展名自己判断：两处规则必然分叉，而分叉的那一天表现是
    #: "界面上给了他编辑按钮，点下去服务端说不行"（或者反过来，能改的没入口）。
    #:
    #: 列表页要逐行算这个字段，所以它**只看元数据、不打开 blob** —— 一屏资产
    #: 逐个读盘去验编码，会把一次列表请求变成几十次磁盘读。真正的编码/大小
    #: 校验在 ``GET|PUT .../text`` 里做（见 ``api/admin.py``）。
    editable: bool = False


class AssetRenameIn(_Base):
    """只改显示名。内容是按 sha256 存的，改名不触碰到内容。"""

    filename: str = Field(min_length=1, max_length=255)


class AssetTextIn(_Base):
    """**直接写一个纯文本资产**（`须知.txt` / `README.md` …）。

    为什么要有这个入口，而不是"先在自己机器上造个文件再上传"：教师想说的那句话
    本来就是在浏览器里打的，为了发它去造一个文件是纯粹的仪式。

    ``content`` 的 200000 字上限（约 200 KB）**刻意远小于** ``max_asset_size``：
    这个入口服务的是"随手写一段话"，真有大文件该走上传（那份能到 2 GB 且流式）。
    上限放在 schema 上，超了就是 422 + 一句人话，不用自己写检查。
    """

    filename: str = Field(min_length=1, max_length=255, description="文件名，如 须知.txt")
    content: str = Field(max_length=200_000, description="文本内容（UTF-8；换行统一成 LF）")
    kind: str = Field(default="testdata", max_length=32)


class AssetTextOut(_Base):
    """一个可在线编辑的纯文本资产的正文。

    回显 ``filename`` 是因为对话框要用它写标题（"编辑 须知.txt"）——
    调用方手上可能只有一个 asset id。
    """

    filename: str
    content: str


class AssetTextEditIn(_Base):
    """在线改正文。**只有内容**，文件名不在这个入口里。

    改名是"换标签"（内容按 sha256 存，改名不碰内容），改正文是"换内容"；
    合成一个入口的话，"到底改的是哪一个"就没法从回执里说清 —— 而这两件事
    对已经落到机器上的文件、对未完成的下发目标，后果完全不同。

    ``content`` 的 200000 字上限与「新建文本文件」那条**逐字一致**：同一个
    对话框写出来的东西，不该因为走新建还是走编辑而撞上两条不同的规则。
    """

    content: str = Field(max_length=200_000, description="文本内容（UTF-8；换行统一成 LF）")


class AssetTextSavedOut(_Base):
    """在线改正文的回执。

    ``asset`` 是**改后**的那一份（sha256 / size 都变了），界面拿它刷新行。
    ``requeued`` 是被重新排队（``done`` → ``pending``）的机器台数 ——
    已经落地的文件不会自己变成新的，只有重排之后机器才会在下一轮 tick
    拿到新字节；这个数字是教师确认"改动已经推进下去"的唯一依据。
    """

    asset: AssetOut
    requeued: int = 0


class AssetZipPasswordOut(_Base):
    """一个 zip 资产的加密状态（只读探测）。

    ``encrypted`` 由服务端读 zip 的标志位算出来（不解压、不解密任何成员）。
    ``filename`` 回显是因为对话框要用它写标题与密码文件的正文。
    """

    encrypted: bool
    filename: str


class AssetZipPasswordIn(_Base):
    """给 zip 打密码 / 改密码。

    * ``password``：新密码。不给就让服务端生成一个（``generate=true``）。
    * ``generate``：让服务端用 ``secrets`` 生成一个不含易混字符的随机密码。
    * ``old_password``：**已经在加密状态时必填**。资产是按内容寻址的（``sha256``
      决定内容），服务端手上只有加密后的字节，没有旧密码就读不出来、也就改不了。
      密码**不会被服务端单独保存**：它只活在 ``password.txt`` 这份文本资产里
      （见 ``api/admin.py`` 的 ``set_asset_zip_password``）。
    """

    password: Optional[str] = Field(default=None, max_length=128)
    generate: bool = False
    old_password: Optional[str] = Field(default=None, max_length=128)


class AssetZipPasswordSavedOut(_Base):
    """打密码 / 改密码的回执。

    * ``asset``：重新打包后的 zip（同一个 asset id，``sha256``/``size`` 变了）。
    * ``password``：这次生效的密码（生成的或教师给的）—— 界面要立刻显示给教师
      抄下来。这是它**唯一**一次被回显：之后想再查只能去读 ``password.txt``。
    * ``password_asset``：同步写好的 ``password.txt`` 文本资产。下发它仍然是
      教师自己的动作（没有"考试开始时自动下发"那套机制），这里只是把入口指出来。
    * ``requeued``：**zip 这一个资产**被重排的机器台数（``done`` → ``pending``）。
      ``password.txt`` 自己如果已经发出去过，也会被重排（同一套"改内容"语义），
      但不计入这个数字 —— 界面那句"包已重新排队给 N 台机器"说的是包。
    * ``packaged``：这次是**把它打包成 zip**（原资产不是 zip）还是**给已有的 zip
      改密码**。只有前者会把 ``asset.filename`` 换成 ``<原基名>.zip``，界面要靠
      这个字段说清回执里的"打包"与"改密码"是两件事。
    """

    asset: AssetOut
    password: str
    password_asset: AssetOut
    requeued: int = 0
    packaged: bool = False


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
    #: pending / done / failed / cancelled。读时从目标聚合，库里不存
    status: str
    created_at: str
    total: int = 0
    done: int = 0
    failed: int = 0
    #: 还在下发的（pending + ready）
    pending: int = 0
    #: 被取消时还没下完的。不计入 ``pending``：它们不会再被派发
    cancelled: int = 0
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
    #: 对应的评测记录；``parse_status="missing"`` 时为 None
    run_id: Optional[int] = None
    #: 结果文件带测试点明细（``GET .../judge/runs/{run_id}``）
    has_cases: bool = False
    #: 这一格结果里疑似违规 / 风险的条数（§12.11）
    warning_count: int = 0
    risk_count: int = 0


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


class PlayerProgressOut(_Base):
    """一位选手的交题进度（概览页巡场用）。

    只数**清单里登记过的题目**，口径与成绩矩阵一致：有评测记录算"已出成绩"，
    没有记录但收到了这道题的代码算"待评测"，两样都没有算"未交"。
    概览页每 5 秒刷一次，只要这几个数，不该为此拉整张成绩矩阵。
    """

    player_id: int
    player_no: str
    #: 有评测记录（含未解析、手工录入）的题数
    submitted: int = 0
    #: 交了代码、还没有评测记录的题数
    pending: int = 0
    #: 既没有代码也没有评测记录的题数
    missing: int = 0
    #: 清单里登记的题目数
    total: int = 0


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
    has_cases: bool = False
    #: 结果里疑似违规 / 风险的条数（§12.11）
    warning_count: int = 0
    risk_count: int = 0


class JudgeCaseOut(_Base):
    name: str
    status: str
    score: int = 0
    time_ms: Optional[int] = None
    memory_kb: Optional[int] = None
    message: str = ""


class JudgeSubtaskOut(_Base):
    index: int
    score: int
    max_score: int
    cases: List[str] = Field(default_factory=list)


class JudgePrecheckFindingOut(_Base):
    """源码预检的一条预警（规则 code 见 ``judging/precheck.py``）。"""

    code: str
    line: int = 0
    message: str = ""
    excerpt: str = ""
    #: ``risk``（拦截类规则）/ ``warning``（疑似违规），服务端按 code 分级
    level: Literal["risk", "warning"] = "warning"


class JudgeRiskEventOut(_Base):
    """一条风险事件（§12.11）：运行选手程序后发现的逃逸 / 篡改痕迹，或源码预检的拦截项。"""

    code: str
    #: 哪个测试点；预检拦截为 null
    case: Optional[str] = None
    #: 源码行号；只有预检拦截才有
    line: int = 0
    message: str = ""
    #: 预检拦截时被拦下的规则 code
    rule: str = ""
    excerpt: str = ""
    level: Literal["risk", "warning"] = "risk"


class JudgeRunDetailOut(JudgeRunOut):
    """一条评测记录连同结果文件里的测试点明细。"""

    judge: Optional[str] = None
    language: Optional[str] = None
    isolated: Optional[bool] = None
    compile_message: str = ""
    judged_at: Optional[str] = None
    source_sha256: Optional[str] = None
    #: 评测时用的选手代码路径（结果文件里的 ``source_path``）
    judged_source: Optional[str] = None
    cases: List[JudgeCaseOut] = Field(default_factory=list)
    subtasks: List[JudgeSubtaskOut] = Field(default_factory=list)
    #: 自带评测的源码预检预警；旧评测机或外部评测器的结果没有
    precheck_warnings: List[JudgePrecheckFindingOut] = Field(default_factory=list)
    #: 风险事件（§12.11）；旧评测机或外部评测器的结果没有
    risk_events: List[JudgeRiskEventOut] = Field(default_factory=list)
    truncated: bool = False
    #: 自带评测写的结果：出自哪个评测任务（§12.9）
    task_id: Optional[int] = None


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
    #: 构建这个包时**现场签发**的那把统一注册密钥的 id。
    #:
    #: 空 = 这个包没附带密钥。非空时界面要显示"哪个版本附带过密钥（哪一把）"
    #: 并给一个就地吊销的入口 —— 所以下面把展示要用的标签与状态一起带出来，
    #: 免得前端为了渲染一行表格再去分页拉一遍密钥列表（那还得自己算 id 对不上）。
    bootstrap_key_id: Optional[int] = None
    #: 那把密钥的用途标签（签发时写的是「随版本 x.y.z 附带」）。密钥记录被删掉后
    #: 回落到 ``#id``，这样版本记录还在时界面不会显示成一个空白的"附带过密钥"。
    bootstrap_key_label: Optional[str] = None
    #: 那把密钥已经吊销了。**吊销不会动版本记录**，所以这一位是界面判断"还要不要
    #: 显示吊销按钮"的唯一依据。
    bootstrap_key_revoked: bool = False
    #: 这一版随包带下去的三条安装策略（见 ``services/install_policy.py``）。
    #: 界面要能显示"这一版发出去的是什么"，否则事后问"这批机器为什么升成 apply 了"
    #: 只能靠翻审计。
    bootstrap_key_policy: str = "keep"
    #: 逐键策略的**完整映射**（每个可配键都有值，没提到的按不改）。
    config_policy: Dict[str, str] = Field(default_factory=dict)
    upgrade_mode: str = "apply"


class ReleaseUpdate(_Base):
    notes: Optional[str] = Field(default=None, max_length=2000)
    channel: Optional[str] = Field(default=None, max_length=16)


class InstallLedgerOut(_Base):
    """装机入口的台账：**当前可以装上去的 Agent 版本**。

    这个接口**不鉴权**（见 ``api/agent.py`` 里那三个端点的说明）：初次安装时机器
    手上什么都没有，没有任何凭据可用。所以它只报"本来就该发给每台机器"的东西 ——
    版本号、校验和、大小、签名。**不含任何考场数据。**

    ``bundle`` / ``installer`` 是相对路径，让客户端自己拼 base：服务端不知道
    客户端是通过哪个地址访问它的（多网卡、反向代理），报绝对地址必然有一半是错的。
    """

    version: str
    sha256: str
    size: int
    #: 包内那一版代码的签名（由发布私钥对 sha256 的十六进制串签）。机器上**有**
    #: 发布公钥时能验；没有时只能靠 sha256，那一步的取舍写在 install.py 的说明里。
    signature: Optional[str] = None
    key_id: Optional[str] = None
    notes: Optional[str] = None
    bundle: str
    installer: str
    #: 那条"一条 curl 就能起头"的自举脚本。空机器上只有 shell 时用它。
    bootstrap: str
    #: 随这个包带下去的安装策略。空机器装机器时就要按它行动，所以它必须在这里 ——
    #: 机器不会去翻发布记录，而且装机入口刻意不鉴权，不能只给发布列表里的那份。
    #: 与包内 ``install_policy.json``、升级清单里那三条**是同一份真相**
    #: （都出自 ``services/install_policy.py`` 的同一个 helper）。
    bootstrap_key_policy: str = "keep"
    config_policy: Dict[str, str] = Field(default_factory=dict)
    upgrade_mode: str = "apply"


class ReleaseSourceOut(_Base):
    """本机有没有可构建的 Agent 源码、会打出哪个版本。

    ``available=False`` 时 ``reason`` 一定是一句能直接显示的中文 ——
    这个接口设计成"永远 200"，因为"没有源码"是一种正常状态而不是错误。

    ``public_url`` 是会被内嵌进包里的服务端地址。装 50 台机器时机器就靠它找
    服务端，所以它要在这里露出来：教师需要知道这个包"能不能自己找到服务器"，
    而不是装完 50 台之后才发现每台都得手填一次。

    ``config_policy_keys`` / ``config_policy_defaults`` 是逐键策略的**规范清单**
    与默认值，由服务端给而不是前端自己抄一份：界面要能列出"可以逐键覆盖哪些键"，
    而抄一份清单的后果是两边漂 —— 漂的那一天教师配了一条**服务端不认识**的键，
    表现是"策略没生效"（甚至报错），而界面上看不出哪里不对。
    """

    available: bool
    version: Optional[str] = None
    agent_root: Optional[str] = None
    public_key: Optional[str] = None
    public_url: Optional[str] = None
    reason: Optional[str] = None
    config_policy_keys: List[str] = Field(default_factory=list)
    config_policy_defaults: Dict[str, str] = Field(default_factory=dict)


class ReleaseBuildIn(_Base):
    """「发布当前版本」的请求体。

    ``version`` **必填**：界面会预填源码里的 ``__version__``，但服务端不接受
    "猜一个默认值"。让发版这件事必须经过一次显式确认，是为了在"改了代码忘了改
    版本号"时撞上"这个版本已经存在"，而不是静默覆盖掉上一版 —— 后者更糟，
    因为已经升级过的机器会因为"版本不高于当前"拒绝升级，而界面上一切正常。

    ``include_bootstrap_key`` **默认关**：打开后这个包等于一张能注册进这台服务端的
    通行证。装机入口（``/api/v1/agent/install/*``）不鉴权，铺开带密钥的版本后，
    能访问公开端口的人都能取到这把密钥；装完机就吊销它。
    """

    version: str = Field(min_length=1, max_length=64)
    channel: str = Field(default="stable", max_length=16)
    notes: Optional[str] = Field(default=None, max_length=2000)
    #: 在构建那一刻现场签发一把统一注册密钥、塞进包里。库里照旧只存哈希。
    include_bootstrap_key: bool = False
    #: 机器上已有的注册密钥要不要被包内那把覆盖（``keep`` / ``replace``）。
    #:
    #: ``None`` = **跟着"附带密钥"走**：勾了就默认 ``replace`` —— 包内那把是权威，
    #: 现场被"旧的已吊销密钥挡住新的"坑过一次，那台机器因此永远注册不上。
    #: 没勾附带时这一项无从谈起，只能是 ``keep``。
    #:
    #: 显式给 ``keep`` 与"没给"是两件事，必须分清：否则教师没法在附带密钥的同时
    #: 保住机器上那把手改过的旧钥。
    bootstrap_key_policy: Optional[str] = Field(default=None, max_length=16)
    #: ``agent.ini`` 逐键三态：``keep`` 不改 / ``default`` 只更新没人动过的 /
    #: ``force`` 强制覆盖。没提到的键一律 ``keep``。
    #:
    #: 键名是 ``<段>.<键>``（如 ``scan.roots``），取值只能是那三态之一；两条都由
    #: 服务端的规范清单校验（``services/install_policy.py``），非法一律 400 ——
    #: 静默丢弃会让教师以为策略生效了。
    config_policy: Optional[Dict[str, str]] = Field(default=None)
    #: 自更新模式：``apply`` / ``stage`` / ``off``。默认 ``apply``。
    upgrade_mode: Optional[str] = Field(default=None, max_length=16)


class UpgradeStatusOut(_Base):
    """自更新总览。教师一眼看清"现在会不会有机器被升级"。"""

    signing_available: bool
    key_id: Optional[str] = None
    error: Optional[str] = None
    active_release: Optional[ReleaseOut] = None
    releases: List[ReleaseOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 选手页（免登录）
# --------------------------------------------------------------------------- #
#
# 这一组模型是**给考场里的选手**看的，所以它们同时守两条边界：
#
# * 字段只够回答"我是谁、要交什么、有哪些注意事项"，一个字节的多余信息都不给 ——
#   代码（SourceFile）、成绩（JudgeRun）、别的选手都不在模型里。前端不显示不是
#   边界，**服务端不返回**才是。
# * 路径与状态沿用现有枚举（``ContestStatus`` / ``DeployStatus`` 的字符串值），
#   不另造一套"选手友好"的状态：前端要按同一套值分支，多一套就多一处要同步。


class RuntimeSettingsOut(_Base):
    """运行参数：心跳节奏与离线判定。

    三项都是**整间机房生效**的（服务端按它们决定下发给每台机器什么节奏、
    多久没心跳算离线），所以界面上要说清影响面。

    单位统一是秒。``tick_idle_seconds`` **同时是本地扫描周期** —— 这两件事在
    Agent 那边绑在一起（扫描发生在心跳里），界面上必须写成一项，不能拆成两个
    各自可调的旋钮（拆开会让"心跳快了扫描也快了"这件事从界面上消失）。
    """

    #: 空闲心跳。范围 5–3600。
    tick_idle_seconds: int = Field(default=30, ge=5, le=3600)
    #: 有活干时的心跳。范围 1–60。
    tick_active_seconds: int = Field(default=2, ge=1, le=60)
    #: 距上次心跳超过它就判离线。必须大于 ``tick_idle_seconds``。
    offline_after_seconds: int = Field(default=90, ge=10, le=86400)


class RuntimeSettingsUpdate(_Base):
    """改运行参数。只放要改的项，没提到的保持当前值。

    这样界面改一项不必把另外两项也带上 —— 带上就得担心"界面显示的旧值覆盖了
    别人刚改的值"。

    **范围校验在这里做（422），跨字段那条在服务端做（400）**：范围是每一项自己的
    事，跨字段（离线必须大于空闲心跳）要看的是**改完之后**那一组值，而它取决于
    库里当前的另外两项 —— 那只能由服务端算。
    """

    tick_idle_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    tick_active_seconds: Optional[int] = Field(default=None, ge=1, le=60)
    offline_after_seconds: Optional[int] = Field(default=None, ge=10, le=86400)


class MetaOut(_Base):
    """全站共用的运行时元信息（免登录）。

    ``display_utc_offset_minutes`` 是**服务端与前端唯一那个要一致的数**：界面上
    所有时间字符串都按它渲染，而不是按浏览器所在时区。教师从不与考区同时区的
    机器上打开管理界面时，后者会让整页时间差几个小时，而页面上完全看不出来。

    回的是**固定的分钟偏移**而不是 IANA 名：前端要能只靠一个数算出钟点；
    IANA 名需要一整个时区数据库才能解释，那是浏览器上的第二个真相源。
    """

    #: 显示时区相对 UTC 的偏移分钟数。默认 480（UTC+8），``SYNCOJ_TZ`` 可改。
    display_utc_offset_minutes: int = 480
    #: 给人看的标识，例如 ``Asia/Shanghai（+08:00）`` 或 ``+08:00``
    display_timezone: str = "+08:00"


class PlayerContestOut(_Base):
    """选手页上的场次信息：标识、名字、状态，以及考试时间窗。

    时间窗（``starts_at`` / ``ends_at``）是给**选手看**的：这一页上"几点开考、几点
    结束"必须一眼看得到 —— 那是他能自己确认"现在到底还收不收卷"的地方（服务端到点
    会把场次自动置为已结束，但选手在此之前就该知道几点结束）。留空 = 不限制，如实回
    ``null`` 让页面显示"不限"，不要编一个时间出来。
    """

    slug: str
    name: str
    status: str
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


class PlayerProfileOut(_Base):
    """选手页上的"我是谁"：考号、姓名、座位、分组。

    只回显他自己的（按查询参数定位到的那一个 ``Player``）。姓名等字段允许为空 ——
    名单里没填就如实是空，不拿考号去顶替。
    """

    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None
    group_name: Optional[str] = None


class PlayerAssetOut(_Base):
    """清单里的一行：下发了什么、该落在哪个目录、到没到。

    ``id`` 是资产 id，考生页的「下载」按它取文件（``GET /player/assets/{id}``）。

    ``dest_dir`` 是**任务的**目标目录模板展开后的样子（``DeployTask.dest_dir``），
    不是某个选手磁盘上的绝对路径 —— 服务端不认识选手机器上的家目录，编一个
    绝对路径只会是错的。空串表示"直接落在下发根目录"（默认就是桌面）。
    """

    id: int
    filename: str
    dest_dir: str = ""
    #: ``DeployStatus`` 的取值：pending/ready/done/failed/cancelled
    status: str
    size: int = 0
    sha256: str
    #: 完成时间。只有 ``status == "done"`` 时才有意义，其余一律 null ——
    #: 把"最后一次状态变化的时间"当成完成时间显示，会让选手以为文件已经到手了。
    finished_at: Optional[str] = None


class PlayerNoticeOut(_Base):
    """下发给选手的**考场公告文件**。

    用户定的形状：公告就是一份走"文件下发"那条路发下去的文件，服务端不去解析
    它的内容、也不把它拆成"说明 + 保存规则"两段 —— 那些拼出来的说法迟早会和
    教师真正发下去的那份文件对不上，而选手会照着页面上那句错的去做。

    ``content`` 是 Markdown **原文**，不转 HTML：渲染是页面的事，服务端一转换，
    "页面上看到的"和"下发给机器的那份文件"就不再是同一份东西了。
    """

    #: 实际下发的公告文件名，例如 ``NOTICE.md``
    filename: str
    #: 公告正文（Markdown 原文，不转 HTML）
    content: str


class PlayerContextOut(_Base):
    """选手页一次要拿到的全部内容。

    刻意做成**一个**接口而不是三个：这一页是给选手看的，"半张页面"（场次加载到了、
    清单还在转圈）比多等一会儿更糟；而且"同一个场次、同一个人"这个一致性本来
    就只能由服务端保证。

    两种查法共用同一个模型：

    * ``matched_by="machine"``：请求没带参数，服务端按来源 IP 认出了这台机器。
      这是主路径 —— 选手在考场机器上打开就是一个带参数的快捷方式，一个字都不用填。
    * ``matched_by="token"``：请求带了考生页登录令牌（``X-Player-Token``），
      那是用「准考证号 + 场次考生页密码」换来的（``POST /player/login``）。
      自动匹配不上时（教师的笔记本、Agent 没起来的机器、换了 IP）靠它兜底。

    回这个字段是让**页面**能说清"我这份清单是怎么定位到你的"：自动匹配到别人的
    机器上时，页面上的考号会当场露馅，而不是安静地显示一份不属于这台机器的清单。
    """

    contest: PlayerContestOut
    player: PlayerProfileOut
    #: 这一场下发给选手的公告文件。**没下发就是 null**（不是空对象）：
    #: 空对象会让页面显示一个"有公告、但内容是空的"的框，而事实是压根没有。
    notice: Optional[PlayerNoticeOut] = None
    assets: List[PlayerAssetOut] = Field(default_factory=list)
    #: ``machine`` / ``token``，见类说明
    matched_by: str
    #: 服务端的 Unix 秒。时钟不一致时页面上还能显示"服务端现在几点"，
    #: 而考试里"还剩多久"是靠它算的。
    server_time: int


class PlayerPreviewContestOut(_Base):
    """降级预览里的一场：只有场次本身与**面向全场**的公告。

    降级预览是谁都能打开的（没认出本机、也没登录），所以这里**绝不**出现任何
    选手的信息或个人下发清单。公告只取"目标是全场"的 ``NOTICE.md`` 下发
    （``DeployTask.target_kind == "all"`` 且没取消）—— 只发给部分人的公告不算。
    """

    slug: str
    name: str
    status: str
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    notice: Optional[PlayerNoticeOut] = None
    #: 本场设了考生页密码 = 可以用「准考证号 + 密码」登录
    login_enabled: bool = False


class PlayerPreviewOut(_Base):
    """考生页的降级预览：当前在考的场次（多场可切换）。"""

    contests: List[PlayerPreviewContestOut] = Field(default_factory=list)
    #: 服务端的 Unix 秒（倒计时的基准），同 ``PlayerContextOut.server_time``
    server_time: int


class PlayerLoginIn(_Base):
    """考生页登录：场次标识 + 准考证号（精确匹配）+ 场次的考生页密码。"""

    contest: str = Field(min_length=1, max_length=64)
    player_no: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PlayerLoginOut(_Base):
    """登录成功签出的短期令牌。之后放进 ``X-Player-Token`` 头取上下文与下载。"""

    token: str
    #: 过期时刻（Unix 秒）
    expires_at: int
