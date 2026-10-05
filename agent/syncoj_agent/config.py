"""Agent 配置解析。

**为什么是 INI 而不是 TOML**：``tomllib`` 是 Python 3.11 才进标准库的，
目标机是 3.8.10。``configparser`` 从 Python 2 时代就在标准库里，零风险。

配置来源优先级：配置文件 < 环境变量 < 命令行参数。
环境变量用 ``SYNCOJ_`` 前缀，方便镜像预装时由部署脚本注入而不用改配置文件。
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

__all__ = ["AgentConfig", "ConfigError", "DEFAULT_INI"]

DEFAULT_INI = """\
[server]
# 服务端地址。生产环境务必用 https
url = https://127.0.0.1:8000
# 校验服务端证书。内网自签 CA 时保持 true 并配置 ca_file
verify_tls = true
# 自签 CA 证书路径（verify_tls=true 且用自签证书时必填）
ca_file =

[agent]
# 注册码。教师端签发，绑定到本机。机器快照还原后靠它自动重新注册
enroll_code =
# 状态目录：存放凭据、哈希缓存、日志、未完成的下载
state_dir = /var/lib/syncoj
# 本机标识。留空则读取 /etc/machine-id（推荐）
machine_id =
# 下发文件的落地根目录。服务端只能在这下面写相对路径
deploy_root = /home/student/exam

[scan]
# 要监控的代码目录，绝对路径。多个目录用换行或逗号分隔
roots = /home/student/code
# 两轮扫描之间的最小间隔（秒）。实际节奏由服务端 tick 响应控制
interval = 60
# 单文件大小上限（字节）。超过则跳过并上报
max_file_size = 2097152

[log]
level = INFO
# 日志文件路径。留空则默认写到 <state_dir>/agent.log（自动轮转）
file =
# 是否同时输出到 stderr（systemd 会收进 journal）
to_stderr = false
"""


class ConfigError(ValueError):
    """配置文件不合法。"""


def _as_bool(raw: str, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class AgentConfig:
    server_url: str = "https://127.0.0.1:8000"
    verify_tls: bool = True
    ca_file: Optional[Path] = None

    enroll_code: str = ""
    state_dir: Path = field(default_factory=lambda: Path("/var/lib/syncoj"))
    machine_id: str = ""

    scan_roots: List[Path] = field(default_factory=list)
    scan_interval: int = 60
    max_file_size: int = 2 * 1024 * 1024

    #: 下发文件的落地根目录。服务端给的 dest 是相对路径，只会落在这下面
    deploy_root: Path = field(default_factory=lambda: Path("/home/student/exam"))

    log_level: str = "INFO"
    log_file: Optional[Path] = None
    log_to_stderr: bool = False

    #: 请求超时（秒）
    request_timeout: int = 30
    #: 下载超时（秒），大文件很慢，需要单独放宽
    download_timeout: int = 600

    # ---- 派生路径 ----
    @property
    def credential_path(self) -> Path:
        return self.state_dir / "credential.json"

    @property
    def hash_cache_path(self) -> Path:
        return self.state_dir / "hash_cache.json"

    @property
    def download_dir(self) -> Path:
        return self.state_dir / "download"

    @property
    def resolved_log_file(self) -> Path:
        return self.log_file or (self.state_dir / "agent.log")

    @property
    def base_url(self) -> str:
        return self.server_url.rstrip("/")

    def validate(self) -> None:
        """检查配置自洽性。**不做网络请求**，便于离线自检。"""
        problems: List[str] = []

        if not self.server_url:
            problems.append("server.url 不能为空")
        elif not self.server_url.startswith(("http://", "https://")):
            problems.append("server.url 必须以 http:// 或 https:// 开头")

        if self.server_url.startswith("http://") and self.verify_tls:
            # 不是错误，但必须让运维看见 —— 明文 HTTP 下 token 会被中间人拿走
            pass

        if self.verify_tls and self.ca_file is not None and not self.ca_file.is_file():
            problems.append("ca_file 不存在: %s" % self.ca_file)

        if not self.scan_roots:
            problems.append("scan.roots 至少要配置一个目录")

        for root in self.scan_roots:
            if not root.is_absolute():
                problems.append("扫描目录必须是绝对路径: %s" % root)
            elif not root.is_dir():
                problems.append("扫描目录不存在或不是目录: %s" % root)

        if not self.deploy_root.is_absolute():
            problems.append("deploy_root 必须是绝对路径: %s" % self.deploy_root)

        if self.scan_interval < 5:
            problems.append("scan.interval 不得小于 5 秒")

        if self.max_file_size <= 0:
            problems.append("scan.max_file_size 必须为正数")

        # 状态目录不能落在被扫描的代码目录里面，否则 Agent 会追着自己的日志传
        for root in self.scan_roots:
            try:
                if self.state_dir == root or root in self.state_dir.parents:
                    problems.append(
                        "state_dir (%s) 不能位于扫描目录 (%s) 内" % (self.state_dir, root)
                    )
            except (OSError, ValueError):
                continue

        if problems:
            raise ConfigError("配置有误:\n  - " + "\n  - ".join(problems))

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AgentConfig":
        parser = configparser.ConfigParser()
        # 保留键名大小写不是必须的，但选项名一律小写更省心
        parser.optionxform = str.lower

        if path is not None:
            if not Path(path).is_file():
                raise ConfigError("配置文件不存在: %s" % path)
            with open(path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
        else:
            parser.read_string(DEFAULT_INI)

        config = cls(
            server_url=(parser.get("server", "url", fallback="") or "").strip(),
            verify_tls=_as_bool(parser.get("server", "verify_tls", fallback="true"), True),
            ca_file=_opt_path(parser.get("server", "ca_file", fallback="")),
            enroll_code=(parser.get("agent", "enroll_code", fallback="") or "").strip(),
            state_dir=Path(
                parser.get("agent", "state_dir", fallback="/var/lib/syncoj").strip()
                or "/var/lib/syncoj"
            ),
            machine_id=(parser.get("agent", "machine_id", fallback="") or "").strip(),
            scan_roots=_split_paths(parser.get("scan", "roots", fallback="")),
            scan_interval=int(parser.get("scan", "interval", fallback="60")),
            max_file_size=int(parser.get("scan", "max_file_size", fallback=str(2 * 1024 * 1024))),
            deploy_root=Path(
                parser.get("agent", "deploy_root", fallback="/home/student/exam").strip()
                or "/home/student/exam"
            ),
            log_level=(parser.get("log", "level", fallback="INFO") or "INFO").strip().upper(),
            log_file=_opt_path(parser.get("log", "file", fallback="")),
            log_to_stderr=_as_bool(parser.get("log", "to_stderr", fallback="false")),
        )

        _apply_env_overrides(config)
        config.state_dir = config.state_dir.expanduser()
        return config


def _opt_path(raw: Optional[str]) -> Optional[Path]:
    text = (raw or "").strip()
    return Path(text).expanduser() if text else None


def _split_paths(raw: Optional[str]) -> List[Path]:
    """支持换行、逗号、分号分隔 —— 教师在配置文件里怎么写都行。"""
    if not raw:
        return []
    normalized = raw.replace(",", "\n").replace(";", "\n")
    result: List[Path] = []
    for line in normalized.splitlines():
        text = line.strip()
        if text:
            result.append(Path(text).expanduser())
    return result


#: 环境变量覆盖表：env 名 -> 属性名
_ENV_OVERRIDES = {
    "SYNCOJ_SERVER_URL": "server_url",
    "SYNCOJ_ENROLL_CODE": "enroll_code",
    "SYNCOJ_STATE_DIR": "state_dir",
    "SYNCOJ_MACHINE_ID": "machine_id",
    "SYNCOJ_CA_FILE": "ca_file",
}


def _apply_env_overrides(config: AgentConfig) -> None:
    for env_name, attr in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        if attr in ("state_dir", "ca_file"):
            setattr(config, attr, Path(value).expanduser())
        else:
            setattr(config, attr, value)

    roots = os.environ.get("SYNCOJ_SCAN_ROOTS")
    if roots:
        config.scan_roots = _split_paths(roots)

    for env_name, attr in (("SYNCOJ_VERIFY_TLS", "verify_tls"), ("SYNCOJ_LOG_STDERR", "log_to_stderr")):
        if env_name in os.environ:
            setattr(config, attr, _as_bool(os.environ[env_name]))
