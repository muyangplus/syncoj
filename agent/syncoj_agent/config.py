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

from .state import detect_desktop

__all__ = [
    "AgentConfig",
    "ConfigError",
    "DEFAULT_INI",
    "WAITING_FILE_NAME",
    "expand_placeholders",
]

#: "已配对，但还没有含本人的场次"时写到桌面上的文件名。
#:
#: 和 ``pairing_file_name`` 不同，这个不做成配置项：它只是一句状态说明，
#: 没有谁会在配置里折腾它；而每多一个配置键，就多一处"配错了没人发现"的地方。
WAITING_FILE_NAME = "等待场次.txt"

#: 路径模板里支持的占位符。
#:
#: 分两类，区别很重要：**载入配置时**能展开的（``{desktop}`` / ``{home}``）
#: 和**注册之后**才能展开的（``{player_no}`` / ``{contest_slug}``）。
#: 注册之前后者原样保留，等拿到凭据再展开一次。
PLACEHOLDER_DESKTOP = "{desktop}"
PLACEHOLDER_HOME = "{home}"
PLACEHOLDER_PLAYER_NO = "{player_no}"
PLACEHOLDER_CONTEST_SLUG = "{contest_slug}"

#: 需要凭据才能确定的占位符
CREDENTIAL_PLACEHOLDERS = (PLACEHOLDER_PLAYER_NO, PLACEHOLDER_CONTEST_SLUG)

#: 路径前缀策略：auto = 取根目录名；none = 不加前缀；其余按字面量
PREFIX_AUTO = "auto"
PREFIX_NONE = "none"


def expand_placeholders(
    text: str,
    desktop: Optional[Path] = None,
    player_no: Optional[str] = None,
    contest_slug: Optional[str] = None,
    home: Optional[Path] = None,
) -> str:
    """展开路径模板。

    ``{desktop}`` / ``{home}`` 现在就能展开；``{player_no}`` / ``{contest_slug}``
    只有注册之后才知道 —— 展开不了的时候**原样保留**，由调用方在合适的时机
    再展开一次。保留而不是报错，是因为配置校验发生在注册之前。
    """
    result = text
    if PLACEHOLDER_DESKTOP in result:
        result = result.replace(PLACEHOLDER_DESKTOP, str(desktop or detect_desktop()))
    if PLACEHOLDER_HOME in result:
        result = result.replace(PLACEHOLDER_HOME, str(home or Path.home()))
    if player_no and PLACEHOLDER_PLAYER_NO in result:
        result = result.replace(PLACEHOLDER_PLAYER_NO, player_no)
    if contest_slug and PLACEHOLDER_CONTEST_SLUG in result:
        result = result.replace(PLACEHOLDER_CONTEST_SLUG, contest_slug)
    return result


def credential_placeholders_in(text: str) -> List[str]:
    """挑出这段文本里"要等凭据"的占位符。"""
    return [token for token in CREDENTIAL_PLACEHOLDERS if token in text]

DEFAULT_INI = """\
[server]
# 服务端地址。生产环境务必用 https
url = https://127.0.0.1:8000
# 校验服务端证书。内网自签 CA 时保持 true 并配置 ca_file
verify_tls = true
# 自签 CA 证书路径（verify_tls=true 且用自签证书时必填）
ca_file =

[agent]
# 镜像内置的统一注册密钥文件（整间机房一份）。
# **只有 root 读得到**，所以这条路径通常用不上 —— 真正干活的是装机时装的
# syncoj-enroll.service（root 身份跑一次，把凭据写进 state_dir）。
# 留在这里是为了：root 直接跑 Agent 时能自己注册，以及排查时能手工触发。
#
# 绝对不要把密钥**内容**写进这个文件：agent.ini 的属主是选手账号，
# 学生读得到里面的每一个字节，而那把钥匙能注册整间机房。
bootstrap_key_file = /etc/syncoj/bootstrap.key
# 状态目录：存放凭据、哈希缓存、日志、未完成的下载
state_dir = /var/lib/syncoj
# 本机标识。留空则读取 /etc/machine-id（推荐）
machine_id =
# 下发文件的落地根目录。
# {desktop} 会自动探测当前用户的桌面（兼容「桌面」与 Desktop 两种命名）
deploy_root = {desktop}

[pairing]
# 把"本机当前状态"写到桌面上，方便教师走过来看一眼：
#   * 还没配对到人  -> <桌面>/配对码.txt，里面是六位配对码
#   * 已配对但没场次 -> <桌面>/等待场次.txt，里面是服务端给的原因
# 配对成功、且拿到场次之后这两个文件都会被自动删掉。
#
# 关掉它的场景只有一种：桌面目录不可写、或者考点规定桌面必须干净。
# 关掉不等于不工作 —— 日志里还有一份，只是要 journalctl 才看得到。
show_on_desktop = true
# 配对码文件名
file_name = 配对码.txt

[scan]
# 要回收的代码目录，绝对路径。多个目录用换行或逗号分隔。
#
# 默认约定：桌面/<准考证号>/<题目名>/<题目名>.cpp
#
# 可用的占位符：
#   {desktop}      当前用户的桌面（自动探测，兼容「桌面」与 Desktop 两种命名）
#   {home}         当前用户的家目录
#   {player_no}    准考证号（注册成功后由 Agent 展开）
#   {contest_slug} 场次标识（注册成功后由 Agent 展开）
#
# 想改成别的位置直接改这一行，例如：
#   roots = /home/student/code
#   roots = {home}/我的代码
#   roots = {desktop}/比赛/{contest_slug}/{player_no}
roots = {desktop}/{player_no}
# 上报路径的前缀：
#   none = 不加前缀（本机只有一个选手时推荐 —— 否则 source/ 里准考证号会出现两次）
#   auto = 取根目录名。配置了多个扫描目录时用它可以区分同名文件
#   其他字面量 = 就用这个字符串
prefix = none
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

[upgrade]
# 自更新模式：
#   off   = 只上报"有新版本"，什么都不做（默认，考场推荐）
#   stage = 下载、验签、解包到独立目录，但不激活
#   apply = 完整执行：解包后原子切换软链并重启
#
# 默认关闭是刻意的 —— 静默地在考试机上升级 Agent 是高风险动作。
# 更关键的是：Agent 必须装在版本化目录 + current 软链布局下（见 installer），
# 否则 apply 无处可切，会直接失败。
mode = off
# 安装根目录（installer 创建；手工部署时可留空）
install_root = /opt/syncoj
# 发布签名公钥。留空则任何升级都会被拒绝 —— 没有信任锚的签名毫无意义
public_key =
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

    #: 统一注册密钥文件（root 只读）。Agent 以选手身份跑时读不到它 ——
    #: 那种情况下由 syncoj-enroll.service 以 root 身份先换好凭据。
    #: **没有 enroll_code**：每选手注册码那条链路已被"机器永久绑定名单条目"取代。
    bootstrap_key_file: Optional[Path] = field(
        default_factory=lambda: Path("/etc/syncoj/bootstrap.key")
    )
    state_dir: Path = field(default_factory=lambda: Path("/var/lib/syncoj"))
    machine_id: str = ""

    #: 未配对时是否把配对码写到桌面（教师走过来读一眼）
    pairing_show_on_desktop: bool = True
    #: 桌面上那个文件叫什么
    pairing_file_name: str = "配对码.txt"

    scan_roots: List[Path] = field(default_factory=list)
    #: 上报路径的前缀策略：``none`` 不加、``auto`` 取根目录名、其他按字面量
    scan_prefix: str = PREFIX_NONE
    scan_interval: int = 60
    max_file_size: int = 2 * 1024 * 1024

    #: 下发文件的落地根目录。服务端给的 dest 是相对路径，只会落在这下面
    deploy_root: Path = field(default_factory=lambda: Path("/home/student/exam"))

    log_level: str = "INFO"
    log_file: Optional[Path] = None
    log_to_stderr: bool = False

    # ---- 自更新 ----
    #: off = 只报告不下载（默认）；stage = 下载验签解包但不激活；apply = 完整执行
    upgrade_mode: str = "off"
    #: 安装根目录，内含 releases/<版本>/ 与 current 软链
    install_root: Path = field(default_factory=lambda: Path("/opt/syncoj"))
    #: 发布签名公钥（JSON）。缺失时任何升级都会被拒绝 —— 没有信任锚就没有签名
    release_public_key: Optional[Path] = None

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

    @property
    def needs_player_no(self) -> bool:
        """是否有路径要等注册拿到准考证号之后才能确定。"""
        return any(PLACEHOLDER_PLAYER_NO in str(root) for root in self.scan_roots)

    @property
    def needs_credential(self) -> bool:
        """是否有路径要等注册拿到凭据（准考证号 / 场次标识）之后才能确定。"""
        return any(
            credential_placeholders_in(str(root)) for root in self.scan_roots
        )

    def resolved_roots(self, player_no: str, contest_slug: str = "") -> List[Path]:
        """展开 ``{player_no}`` / ``{contest_slug}`` 之后的扫描目录。

        必须在拿到凭据之后调用 —— 准考证号与场次标识都是注册的产物。
        """
        return [
            Path(
                expand_placeholders(
                    str(root), player_no=player_no, contest_slug=contest_slug
                )
            )
            for root in self.scan_roots
        ]

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
            text = str(root)
            if not root.is_absolute():
                problems.append("扫描目录必须是绝对路径: %s" % root)
                continue
            pending = credential_placeholders_in(text)
            if pending:
                # 还没注册，准考证号/场次标识未知，没法检查最终目录是否存在。
                # 但可以检查占位符**左边那一段**——它现在已经能确定了。
                #
                # 注意是直接取 split 的前半段（末尾带分隔符，Path 会归一化掉），
                # 不要再取 .parent：那会跳到祖父目录，检查的是已经存在的工作区，
                # 于是永远不报错。
                # 有多个待展开占位符时取**最靠左**的那个，剩下的都还在它右边。
                first = min(text.index(token) for token in pending)
                prefix_dir = Path(text[:first])
                if str(prefix_dir) not in ("", ".") and not prefix_dir.is_dir():
                    problems.append(
                        "扫描目录的父目录不存在: %s（%s 会在注册后展开）"
                        % (prefix_dir, "、".join(pending))
                    )
                continue
            if not root.is_dir():
                problems.append("扫描目录不存在或不是目录: %s" % root)

        if not self.deploy_root.is_absolute():
            problems.append("deploy_root 必须是绝对路径: %s" % self.deploy_root)

        if self.upgrade_mode not in ("off", "stage", "apply"):
            problems.append("upgrade.mode 只能是 off / stage / apply，实际是 %r" % self.upgrade_mode)

        if self.upgrade_mode != "off":
            if not self.install_root.is_absolute():
                problems.append("upgrade.install_root 必须是绝对路径: %s" % self.install_root)
            if self.release_public_key is None:
                # 没有公钥就没有信任锚 —— 与其"升级前才发现"，不如启动时就报出来
                problems.append(
                    "upgrade.mode=%s 但未配置 upgrade.public_key；"
                    "没有公钥就无法验证发布包签名，升级会全部失败" % self.upgrade_mode
                )
            elif not self.release_public_key.is_file():
                problems.append("upgrade.public_key 不存在: %s" % self.release_public_key)

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
            bootstrap_key_file=_templated_path(
                parser.get("agent", "bootstrap_key_file", fallback="/etc/syncoj/bootstrap.key")
                or "/etc/syncoj/bootstrap.key"
            ),
            state_dir=Path(
                parser.get("agent", "state_dir", fallback="/var/lib/syncoj").strip()
                or "/var/lib/syncoj"
            ),
            machine_id=(parser.get("agent", "machine_id", fallback="") or "").strip(),
            # {desktop} 在这里就展开（靠探测）；{player_no} 要等注册后才展开
            deploy_root=_templated_path(
                parser.get("agent", "deploy_root", fallback="{desktop}") or "{desktop}"
            ),
            scan_roots=[
                Path(expand_placeholders(item))
                for item in _split_paths(parser.get("scan", "roots", fallback="") or "{desktop}/{player_no}")
            ],
            scan_prefix=(parser.get("scan", "prefix", fallback=PREFIX_NONE) or PREFIX_NONE).strip(),
            scan_interval=int(parser.get("scan", "interval", fallback="60")),
            max_file_size=int(parser.get("scan", "max_file_size", fallback=str(2 * 1024 * 1024))),
            log_level=(parser.get("log", "level", fallback="INFO") or "INFO").strip().upper(),
            log_file=_opt_path(parser.get("log", "file", fallback="")),
            log_to_stderr=_as_bool(parser.get("log", "to_stderr", fallback="false")),
            pairing_show_on_desktop=_as_bool(
                parser.get("pairing", "show_on_desktop", fallback="true"), True
            ),
            pairing_file_name=(
                parser.get("pairing", "file_name", fallback="配对码.txt") or "配对码.txt"
            ).strip(),
            upgrade_mode=(parser.get("upgrade", "mode", fallback="off") or "off").strip().lower(),
            install_root=Path(
                parser.get("upgrade", "install_root", fallback="/opt/syncoj").strip()
                or "/opt/syncoj"
            ),
            release_public_key=_opt_path(parser.get("upgrade", "public_key", fallback="")),
        )

        _apply_env_overrides(config)
        config.state_dir = config.state_dir.expanduser()
        return config


def _opt_path(raw: Optional[str]) -> Optional[Path]:
    text = (raw or "").strip()
    return Path(text).expanduser() if text else None


def _templated_path(raw: str) -> Path:
    """展开 ``{desktop}`` 与 ``~``，但**保留** ``{player_no}``。"""
    return Path(expand_placeholders(raw.strip() or "{desktop}")).expanduser()


def _split_paths(raw: Optional[str]) -> List[str]:
    """支持换行、逗号、分号分隔 —— 教师在配置文件里怎么写都行。"""
    if not raw:
        return []
    normalized = raw.replace(",", "\n").replace(";", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


#: 环境变量覆盖表：env 名 -> 属性名
_ENV_OVERRIDES = {
    "SYNCOJ_SERVER_URL": "server_url",
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
        # 走和配置文件**同一条**模板展开路径。早先这里直接塞字符串进来，
        # 于是 validate() 会在 str 上调 .is_absolute() 直接崩掉 ——
        # 镜像预装时用环境变量注入恰恰是最常见的方式。
        config.scan_roots = [Path(expand_placeholders(item)) for item in _split_paths(roots)]

    for env_name, attr in (("SYNCOJ_VERIFY_TLS", "verify_tls"), ("SYNCOJ_LOG_STDERR", "log_to_stderr")):
        if env_name in os.environ:
            setattr(config, attr, _as_bool(os.environ[env_name]))
