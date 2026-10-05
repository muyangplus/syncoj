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
    enroll_code: str = Field(min_length=4, max_length=64)
    machine_id: str = Field(min_length=1, max_length=128)
    hostname: Optional[str] = Field(default=None, max_length=128)
    agent_version: Optional[str] = Field(default=None, max_length=32)
    os_info: Optional[str] = Field(default=None, max_length=200)


class EnrollResponse(_Base):
    token: str
    agent_id: int
    player_no: str
    player_name: Optional[str] = None
    contest_id: int
    contest_slug: str
    contest_name: str
    config: Dict[str, Any]


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


class TickResponse(_Base):
    server_time: int
    next_tick_seconds: int
    need_upload: List[str] = Field(default_factory=list)
    deploy_jobs: List[DeployJob] = Field(default_factory=list)
    cancel_assets: List[int] = Field(default_factory=list)
    upgrade: Optional[UpgradeInfo] = None
    config: Dict[str, Any]


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


class ContestOut(_Base):
    id: int
    slug: str
    name: str
    status: str
    player_count: int = 0
    online_count: int = 0
    created_at: str


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
