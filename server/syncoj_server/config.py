"""服务端运行时配置。

优先级：命令行参数 > 环境变量 > 默认值。

刻意不引入 ``pydantic-settings``：配置项很少，dataclass 足够，少一个依赖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import keys

__all__ = ["Settings", "default_settings"]


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else Path(default)


def _env_opt_path(name: str) -> Optional[Path]:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def _guess_web_dist() -> Optional[Path]:
    """猜一下前端产物在哪。

    只在开发仓库布局下成立（``<repo>/server/syncoj_server/config.py`` →
    ``<repo>/web/dist``）。猜不到就返回 None —— 服务端照常只提供 API，
    不会因为前端没构建而启动失败。
    """
    candidate = Path(__file__).resolve().parents[2] / "web" / "dist"
    return candidate if candidate.is_dir() else None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """环境变量里的布尔。认 ``1/true/yes/on`` 与 ``0/false/no/off``（不分大小写）。

    不认的值**退回默认值**而不是报错：这是启动路径上的一行配置，为它让服务端起
    不来不值得；而"写错了当没写"在这里的后果只是发现开关保持默认，
    有日志可查。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _env_opt_str(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else None


def _default_file_pattern() -> str:
    """默认代码路径模式。

    诚实的说法：``matching.DEFAULT_PATTERN`` 才是唯一的出厂常量，这里只是
    把它读出来并允许环境变量覆盖。**故意用延迟 import** —— 顶层 import 会
    把 ``syncoj_server.services`` 的 ``__init__`` 拉到 config 被完整定义之前
    执行，哪天有人在 ``services/__init__`` 里 import 领域模块，就会变成一条
    难查的循环导入。
    """
    override = os.environ.get("SYNCOJ_DEFAULT_FILE_PATTERN")
    if override:
        return override
    from .services.matching import DEFAULT_PATTERN

    return DEFAULT_PATTERN


@dataclass
class Settings:
    """服务端全部可调参数。"""

    # ---- 存储 ----
    data_root: Path = field(default_factory=lambda: _env_path("SYNCOJ_DATA_ROOT", "runtime/server"))

    # ---- 密钥 ----
    #: 密钥目录（仓库里的 ``.key/``）。解析规则见 :mod:`syncoj_server.keys` ——
    #: 那里是"私钥在哪"的唯一定义。``None`` = 没有隐式密钥目录。
    key_dir: Optional[Path] = field(default_factory=keys.key_dir)
    #: 发布签名私钥（PEM/DER/JSON）。
    #:
    #: ``SYNCOJ_RELEASE_KEY`` 最优先；否则退回 ``<key_dir>/release-key.pem``
    #: （**只在文件真的存在时**）。两者都没有 → ``None`` → 服务端不提供任何升级，
    #: 而没有签名的包 Agent 一律拒绝 —— 这是刻意的安全默认值。
    release_signing_key: Optional[Path] = field(
        default_factory=keys.release_signing_key_path
    )

    # ---- 轮询节奏（下发给 Agent，Agent 不得自行决定）----
    # 空闲时放宽到 60s：50 台机器 -> 约 0.8 req/s
    tick_idle_seconds: int = 60
    # 有活干时收紧到 2s，把下发/上传的感知延迟压到秒级
    tick_active_seconds: int = 2
    # 距上次 tick 超过该秒数即判定离线。必须大于 tick_idle_seconds，
    # 否则空闲状态的机器会被误判为掉线。
    offline_after_seconds: int = 180

    # ---- 文件策略（作为默认值下发给 Agent，Agent 侧有等价内置默认）----
    max_file_size: int = 2 * 1024 * 1024
    max_relpath_length: int = 1024
    max_files_per_scan: int = 5000

    # ---- 代码路径规范 ----
    #: 题目没配 ``file_patterns`` 时用的默认 glob 模式。
    #:
    #: 出厂值对应约定 ``桌面/<准考证号>/<题目名>/<题目名>.cpp`` —— 但**那只是
    #: 默认值**，换考点、换赛事可以直接改这里，或者按题目单独覆盖。
    #:
    #: 支持 ``{ident}`` / ``{title}`` 占位符，以及 glob 通配符：
    #:   *  不跨 /；  **  跨 /；  ?  单字符；  [abc] 字符类
    #:
    #: 环境变量 ``SYNCOJ_DEFAULT_FILE_PATTERN`` 可覆盖。
    default_file_pattern: str = field(default_factory=_default_file_pattern)

    # ---- 状态落库 ----
    # 内存持有实时状态，按此周期合并成一个事务刷入 SQLite
    flush_interval_seconds: float = 5.0

    # ---- 评测成绩扫描 ----
    # 周期扫描 judge_result/ 目录并回写成绩。对延迟不敏感，所以放得比较宽
    judge_scan_interval: float = 10.0

    # ---- 自更新 ----
    #: 发布包大小上限。签名私钥见上面的 ``release_signing_key`` —— 它**只能有
    #: 一处定义**：这个 dataclass 曾经把同一个字段声明了两遍（一次读环境变量、
    #: 一次写死 None），后者静默覆盖前者，于是 ``SYNCOJ_RELEASE_KEY`` 完全不
    #: 起作用，而"自更新为什么开不起来"在代码里怎么看都像是配好了。
    max_release_size: int = 256 * 1024 * 1024

    # ---- 管理界面 ----
    #: 前端构建产物目录（web/dist）。存在则由本进程托管，不存在就只提供 API。
    #: 单进程托管省掉一个 Caddy/nginx —— 本机部署场景下少一个组件就少一处故障点。
    web_dist: Optional[Path] = field(default_factory=lambda: _env_opt_path("SYNCOJ_WEB_DIST") or _guess_web_dist())

    # ---- 对外地址与局域网发现 ----
    #:
    #: **这台服务端对考试机来说是什么地址。** 它有三处用途，必须是同一个值：
    #: 内嵌进离线包、发给广播探测的应答、以及管理界面里让人照着抄。
    #:
    #: 不设也能跑：打包/应答时会退回"最后一个非回环的 Host 头"（教师既然能从
    #: 那台机器打开界面，那个地址就是通的）。两者都没有时**什么都不做** ——
    #: 退化成 ``127.0.0.1`` 是最坏的一种，因为 50 台机器会各自找自己，
    #: 而现象是"注册不上"，没人会想到去看服务端配置。
    public_url: Optional[str] = field(default_factory=lambda: _env_opt_str("SYNCOJ_PUBLIC_URL"))

    #: HTTP 监听端口。**由 ``serve --port`` 写入**，用来在没配 ``public_url`` 时
    #: 拼出要广播的地址。放在 Settings 里是因为应答器跑在后台任务里，
    #: 拿不到命令行参数。
    http_port: int = 8000

    #: 是否应答局域网里的发现探测。关掉它不影响任何别的功能。
    discovery_enabled: bool = field(
        default_factory=lambda: _env_bool("SYNCOJ_DISCOVERY", True)
    )

    #: 发现用的 UDP 端口。**固定**是最重要的性质：机器上不能预置任何配置，
    #: 所以两边都得知道往哪个端口喊。可以改，但改了就要整间机房一起改。
    discovery_port: int = field(
        default_factory=lambda: _env_int("SYNCOJ_DISCOVERY_PORT", 45871)
    )

    #: 同一个来源 IP 每秒最多应答几次。UDP 应答器是个**无状态反射点**，
    #: 不限速的话它就成了别人手里的放大器。
    discovery_replies_per_second: int = 5

    #: 探测报文的最小长度。**应答不得大于探测** —— 否则这个端口可以被用来放大
    #: 流量（小请求、大应答）。RSA-2048 的签名 base64 就有 344 字符，所以探测
    #: 那边带了填充；这条是那道约束的服务端一侧。
    #:
    #: **必须与 ``services.discovery.MIN_PROBE_BYTES`` 相等**（有测试盯着）：
    #: 探测填到 640、而这里要求 1024 的话，现象是"完全没人应答"。
    discovery_min_probe_bytes: int = field(
        default_factory=lambda: _env_int("SYNCOJ_DISCOVERY_MIN_PROBE", 640)
    )

    # ---- 认证 ----
    admin_session_ttl_seconds: int = 12 * 3600
    #: 统一注册密钥的字节数。一把密钥对应**整间机房**，
    #: 泄漏了等于交出"无限注册"的能力，熵必须够
    bootstrap_key_bytes: int = 32
    #: 配对短码长度。它只在机器与教师之间口头/目视传递，越长越难抄对；
    #: 6 位配合"限时 + 一次性 + 管理员鉴权"已经够用
    pair_code_length: int = 6
    #: 配对短码的有效期。机器装好后教师可能过一阵才走到跟前，
    #: 太短就得回管理界面重新发，太长则墙上贴的码会被别人抄走
    pair_code_ttl_seconds: int = 30 * 60
    token_bytes: int = 32

    # ---- 注册限速 ----
    #: 单 IP 每秒允许的注册次数。0 = 不限速。
    #: 统一密钥把 /agent/enroll 变成"一把钥匙开整间机房"，必须有东西挡住
    #: 无限注册与爆破。正常场景下一次注册就够，所以默认给得很紧。
    enroll_per_ip_per_second: int = 10
    #: 全局每秒允许的注册次数（所有 IP 合计）。挡住"从很多 IP 一起刷"。
    #: 50 台机器开机时同时注册是真实场景，所以这里比单 IP 宽得多。
    enroll_global_per_second: int = 50

    # ---- 上传 ----
    # 单次 multipart 请求体上限，略高于 max_file_size 以容纳 multipart 开销
    max_upload_request_bytes: int = 4 * 1024 * 1024
    # 教师上传的待下发资产（题面、测试点包）上限。远大于选手代码上限 ——
    # 测试点包动辄几百 MB，而源码只有几十 KB
    max_asset_size: int = 2 * 1024 * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_root / "syncoj.db"

    @property
    def blob_root(self) -> Path:
        return self.data_root / "blobs"

    @property
    def source_root(self) -> Path:
        """供 LemonLime / Arbiter 直接读取的目录。"""
        return self.data_root / "source"

    @property
    def judge_result_root(self) -> Path:
        return self.data_root / "judge_result"

    @property
    def log_root(self) -> Path:
        return self.data_root / "logs"

    @property
    def database_url(self) -> str:
        # as_posix() 保证 Windows 开发机上也能生成合法的 SQLite URL
        return "sqlite+pysqlite:///" + self.db_path.resolve().as_posix()

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.blob_root,
            self.source_root,
            self.judge_result_root,
            self.log_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def default_settings() -> Settings:
    return Settings()
