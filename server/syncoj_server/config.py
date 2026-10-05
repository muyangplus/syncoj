"""服务端运行时配置。

优先级：命令行参数 > 环境变量 > 默认值。

刻意不引入 ``pydantic-settings``：配置项很少，dataclass 足够，少一个依赖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
    #: 发布签名私钥路径（环境变量 SYNCOJ_RELEASE_KEY 可覆盖）
    release_signing_key: Optional[Path] = field(
        default_factory=lambda: _env_opt_path("SYNCOJ_RELEASE_KEY")
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
    #: 发布签名私钥（PEM/DER/JSON）。**留空则服务端不提供任何升级** ——
    #: 没有私钥就签不出包，而没有签名的包 Agent 一律拒绝，所以留空是安全的默认值。
    release_signing_key: Optional[Path] = None
    #: 发布包大小上限
    max_release_size: int = 256 * 1024 * 1024

    # ---- 管理界面 ----
    #: 前端构建产物目录（web/dist）。存在则由本进程托管，不存在就只提供 API。
    #: 单进程托管省掉一个 Caddy/nginx —— 本机部署场景下少一个组件就少一处故障点。
    web_dist: Optional[Path] = field(default_factory=lambda: _env_opt_path("SYNCOJ_WEB_DIST") or _guess_web_dist())

    # ---- 认证 ----
    admin_session_ttl_seconds: int = 12 * 3600
    enroll_code_bytes: int = 16
    token_bytes: int = 32

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
