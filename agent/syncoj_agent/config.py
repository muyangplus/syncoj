"""Agent 配置解析。

**为什么是 INI 而不是 TOML**：``tomllib`` 是 Python 3.11 才进标准库的，
目标机是 3.8.10。``configparser`` 从 Python 2 时代就在标准库里，零风险。

配置来源优先级：配置文件 < 环境变量 < 命令行参数。
环境变量用 ``SYNCOJ_`` 前缀，方便镜像预装时由部署脚本注入而不用改配置文件。
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .state import detect_desktop

log = logging.getLogger(__name__)

__all__ = [
    "AgentConfig",
    "ConfigError",
    "DEFAULT_INI",
    "WAITING_FILE_NAME",
    "expand_for_user",
    "expand_placeholders",
    "lookup_user_home",
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
    user: Optional[str] = None,
) -> str:
    """展开路径模板。

    ``{desktop}`` / ``{home}`` 现在就能展开；``{player_no}`` / ``{contest_slug}``
    只有注册之后才知道 —— 展开不了的时候**原样保留**，由调用方在合适的时机
    再展开一次。保留而不是报错，是因为配置校验发生在注册之前。

    ``user`` 说明"这些路径算在哪个账号头上"：给了它就按**那个账号**的家目录
    （``pwd.getpwnam``）以及该家目录下的桌面来展开，而不是按当前进程的用户。
    这一条是必须的 —— 注册运行（``--provision``）以 **root** 跑，
    ``Path.home()`` 是 ``/root``，``{desktop}/{player_no}`` 于是被展开成
    ``/root/桌面/<准考证号>``：目录不存在 → 校验失败 → 退出 2，而选手的桌面
    根本不在这里。真机上就是这么"机器永远不注册"的。
    账号查不到时**报错而不是退回当前用户**（见 :func:`_home_and_desktop`）。
    """
    result = text
    if PLACEHOLDER_DESKTOP in result or PLACEHOLDER_HOME in result:
        resolved_home, resolved_desktop = _home_and_desktop(user, home, desktop)
        if PLACEHOLDER_DESKTOP in result:
            result = result.replace(PLACEHOLDER_DESKTOP, str(resolved_desktop))
        if PLACEHOLDER_HOME in result:
            result = result.replace(PLACEHOLDER_HOME, str(resolved_home))
    if player_no and PLACEHOLDER_PLAYER_NO in result:
        result = result.replace(PLACEHOLDER_PLAYER_NO, player_no)
    if contest_slug and PLACEHOLDER_CONTEST_SLUG in result:
        result = result.replace(PLACEHOLDER_CONTEST_SLUG, contest_slug)
    return result


def _pwd_home(user: str) -> Optional[Path]:
    """``pwd.getpwnam(user).pw_dir``；查不到（或本平台没有 ``pwd``）返回 ``None``。

    单独抽成函数是为了**可注入**：测试不该依赖真机器的 ``/etc/passwd``
    （Windows 开发机上根本没有 ``pwd`` 模块）。
    """
    try:
        import pwd
    except ImportError:  # pragma: no cover - 开发机（Windows）
        return None
    try:
        entry = pwd.getpwnam(user)
    except (KeyError, OSError, ValueError):
        return None
    return Path(entry.pw_dir) if entry.pw_dir else None


def lookup_user_home(user: Optional[str]) -> Optional[Path]:
    """某个账号的家目录；账号为空或查不到返回 ``None``。"""
    text = (user or "").strip()
    if not text:
        return None
    return _pwd_home(text)


def expand_for_user(text: str, user: Optional[str] = None) -> str:
    """按**指定账号**的家目录/桌面展开 ``{home}`` / ``{desktop}``。

    账号为空就是原来那套"按当前用户"的行为；指定了但查不到账号会抛
    :class:`ConfigError` —— 见 :func:`_home_and_desktop`。
    """
    return expand_placeholders(text, user=user)


def _home_and_desktop(
    user: Optional[str],
    home: Optional[Path],
    desktop: Optional[Path],
) -> "Tuple[Path, Path]":
    """定出 ``{home}`` / ``{desktop}`` 各展开成什么。

    桌面**在家目录下探测**：root 去探测只会得到 ``/root/桌面``，而
    ``user-dirs.dirs`` 是**每个账号**自己的（有人把桌面挪到别处、有人用英文名）。

    指定了账号却查不到它时**直接报错**，不退回当前用户：注册单元以 root 跑，
    退回去就是 ``/root/...``，而那个目录在选手机器上不存在 —— 现场的表现是
    "退出 2、journal 空"，比报一句"账号查不到"难查得多。
    """
    resolved_home = home
    if resolved_home is None and user:
        resolved_home = lookup_user_home(user)
        if resolved_home is None:
            raise ConfigError(
                "找不到运行账号 %r 的家目录，无法解析路径模板里的 {home} / {desktop}。\n"
                "  不能退回当前用户 —— 注册单元以 root 跑，退回去就等于把它们\n"
                "  展开成 /root/...，而选手的家不在那里。\n"
                "  请检查 agent.ini 的 run_user（或 --chown-to / --user 传的账号）"
                "是否写错，或者那个账号是不是还没建。" % user
            )
    if resolved_home is None:
        resolved_home = Path.home()
    if desktop is None:
        desktop = detect_desktop(resolved_home)
    return resolved_home, desktop


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
# **运行账号**（安装器填）。路径模板里的 {home}/{desktop} 按**它**的家展开，
# 而不是按"当前是谁在跑" —— 注册单元以 root 跑一次，按 root 的家展开会变成
# /root/桌面/...，那台机器上不存在，注册直接失败。留空则退回当前用户。
run_user =
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
# 心跳兜底间隔（秒）。**服务端策略优先**（tick 响应里的 next_tick_seconds 与
# 下发的策略都会盖过它）；只有"还没下发策略"时才用这个值（默认 30）
interval = 30
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
#   apply = 下载 → 验签 → 原子切换软链并重启（**默认**）
#   stage = 下载、验签、解包到独立目录，但不激活
#   off   = 只上报"有新版本"，什么都不做
#
# 默认 apply 是**运维口径**决定的：机器铺开之后没人会一台台去点升级，而安全
# 由签名兜底（没有公钥时 apply 会直接失败，不会"静默升级到不知道什么东西"）。
# 考场上要关掉就显式写 off；安装器不会悄悄改机器上已经写死的值
# （见 install.py 的 config_policy：默认 keep）。
#
# Agent 必须装在版本化目录 + current 软链布局下（见 installer），否则 apply
# 无处可切、会直接失败。
mode = apply
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

    #: 运行账号（安装器写进 agent.ini）。记下来是为了让 ``{home}`` / ``{desktop}``
    #: 的展开**只有一个来源**：注册单元以 root 跑、服务以选手账号跑，两边读同一份
    #: 配置，都按这个账号的家去展开 —— 不依赖调用方记得传 ``--chown-to``。
    run_user: str = ""

    #: 统一注册密钥文件（root 只读）。Agent 以选手身份跑时读不到它 ——
    #: 那种情况下由 syncoj-enroll.service 以 root 身份先换好凭据。
    #: **没有 enroll_code**：每选手注册码那条链路已被"机器永久绑定名单条目"取代。
    bootstrap_key_file: Optional[Path] = field(
        default_factory=lambda: Path("/etc/syncoj/bootstrap.key")
    )
    state_dir: Path = field(default_factory=lambda: Path("/var/lib/syncoj"))
    machine_id: str = ""

    #: 权威机器身份文件（安装器以 root 写进配置目录，0644，选手改不动）。
    #: Agent **优先**读它；读不到才退回 `state_dir/machine_uuid`（见
    #: `state.resolve_machine_uuid`）。远程卸载的授权令牌绑的就是这个值，所以
    #: 它的位置跟着配置文件所在的目录走，而不是写死 /etc/syncoj。
    machine_uuid_file: Path = field(
        default_factory=lambda: Path("/etc/syncoj/machine_uuid")
    )

    #: 未配对时是否把配对码写到桌面（教师走过来读一眼）
    pairing_show_on_desktop: bool = True
    #: 桌面上那个文件叫什么
    pairing_file_name: str = "配对码.txt"

    scan_roots: List[Path] = field(default_factory=list)
    #: 上报路径的前缀策略：``none`` 不加、``auto`` 取根目录名、其他按字面量
    scan_prefix: str = PREFIX_NONE
    #: 心跳兜底间隔（秒）。**服务端策略优先**：tick 响应里的 ``next_tick_seconds``
    #: 与配置策略里的 ``scan.interval`` 都会盖过它（见 ``_clamp_wait``）——
    #: 这个值只在"服务端还没下发策略"或"策略读不到"时生效，所以要和服务端的
    #: 空心跳默认值保持一致（30 秒），否则连不上策略时机器会比其他机器慢一半。
    scan_interval: int = 30
    max_file_size: int = 2 * 1024 * 1024

    #: 下发文件的落地根目录。服务端给的 dest 是相对路径，只会落在这下面
    deploy_root: Path = field(default_factory=lambda: Path("/home/student/exam"))

    log_level: str = "INFO"
    log_file: Optional[Path] = None
    log_to_stderr: bool = False

    # ---- 自更新 ----
    #: apply = 下载验签后自动切换并重启（默认）；stage = 只下载验签不激活；
    #: off = 完全不动。**默认 apply**：机器铺开之后没人会一台台点升级，
    #: 安全由签名兜底（没配公钥时 apply 会直接失败，不会静默升到不明版本）。
    upgrade_mode: str = "apply"
    #: ``upgrade.mode`` 是不是**配置文件里真的写了**（内置模板的那份不算）。
    #: 用来区分两种"apply 但没有公钥"：
    #:
    #: * 没人配过（模板默认）→ 警告一句、按 off 跑 —— 否则每个没打包发布公钥的
    #:   镜像都会**起不来**，那比"升不了级"严重得多；
    #: * 有人显式写了 apply → 那是配置错误（想开升级却没给信任锚），启动就报。
    upgrade_mode_explicit: bool = False
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

    def validate(self, for_provision: bool = False) -> None:
        """检查配置自洽性。**不做网络请求**，便于离线自检。

        ``for_provision=True`` 是**注册运行**（``--provision``，装机时由 root 的
        一次性单元执行）专用的、**更窄**的校验集。注册只关心"服务端地址合法、
        状态目录写得到、统一密钥读得到"，``scan.roots`` / ``deploy_root`` /
        ``upgrade.*`` 一律跳过：它们与注册无关，而且模板里的 ``{home}`` /
        ``{desktop}`` 是按运行账号展开的 —— 注册以 root 跑时会展开成
        ``/root/...``，那台机器上当然不存在。真机就是这样退出 2、journal 里
        一个字都没有的（见 main.py 里"校验失败必须打 stderr"）。

        **服务路径那道守卫一点都不放松**（``for_provision`` 默认 ``False``）：
        服务以运行账号跑，扫描目录不存在就该在启动时吵出来。
        """
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

        if for_provision:
            # 注册该看的那几件事（而且**只看**这几件）
            problems.extend(self._provision_problems())
        else:
            problems.extend(self._service_problems())

        if problems:
            raise ConfigError("配置有误:\n  - " + "\n  - ".join(problems))

    def _provision_problems(self) -> List[str]:
        """注册运行要检查的少数几件事（见 :meth:`validate`）。

        **不碰选手目录**：``scan.roots`` / ``deploy_root`` 一个都不校验、也不
        创建 —— 它们要等拿到准考证号之后才有意义，而且那时 Agent 已经以选手
        身份在跑了。
        """
        problems: List[str] = []

        problems.extend(_dir_writable_problems(self.state_dir, "state_dir"))

        if self.credential_path.is_file():
            # 已经注册过了。注册单元每次开机都会重跑，但它第一件事就是读回凭据
            # （见 Agent.ensure_credential）—— 这时**不再需要**统一密钥。所以
            # 不能因为"运维注册完就把密钥删了"（考场里很常见）让单元失败。
            if not os.access(str(self.credential_path), os.R_OK):
                problems.append("已注册但没有权限读回凭据: %s" % self.credential_path)
        else:
            key = self.bootstrap_key_file
            if key is None:
                problems.append("未配置 bootstrap_key_file，注册拿不到统一密钥")
            elif not key.is_file():
                problems.append(
                    "统一密钥文件不存在: %s（注册只有这一条路，见 bootstrap.sh --help）"
                    % key
                )
            elif not os.access(str(key), os.R_OK):
                problems.append("统一密钥文件读不到: %s" % key)

        return problems

    def _service_problems(self) -> List[str]:
        """常驻服务要检查的（扫描目录、下发目录、自更新……）。"""
        problems: List[str] = []

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

        return problems

    @classmethod
    def load(
        cls,
        path: Optional[Path] = None,
        expand_for_user: Optional[str] = None,
    ) -> "AgentConfig":
        """读配置。

        ``expand_for_user`` 指定 ``{home}`` / ``{desktop}`` 按**哪个账号**展开
        （注册运行以 root 跑时必须传"要交付的那个账号"，见
        :func:`expand_placeholders`）。没传就看配置里的 ``run_user``，再没有就
        按当前用户 —— 于是"跟谁的家"只有一个来源（agent.ini），调用方忘了传参
        也不会退回 root。
        """
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

        run_user = (parser.get("agent", "run_user", fallback="") or "").strip()
        # 优先级：命令行给的账号（--chown-to）> 配置里记的运行账号 > 当前用户
        expand_as = (expand_for_user or "").strip() or run_user or None

        config = cls(
            server_url=(parser.get("server", "url", fallback="") or "").strip(),
            verify_tls=_as_bool(parser.get("server", "verify_tls", fallback="true"), True),
            ca_file=_opt_path(parser.get("server", "ca_file", fallback="")),
            bootstrap_key_file=_templated_path(
                parser.get("agent", "bootstrap_key_file", fallback="/etc/syncoj/bootstrap.key")
                or "/etc/syncoj/bootstrap.key",
                user=expand_as,
            ),
            run_user=run_user,
            state_dir=Path(
                parser.get("agent", "state_dir", fallback="/var/lib/syncoj").strip()
                or "/var/lib/syncoj"
            ),
            machine_id=(parser.get("agent", "machine_id", fallback="") or "").strip(),
            # 权威机器身份跟着**配置文件所在目录**走（安装器就写在它旁边）
            machine_uuid_file=(
                Path(path).parent / "machine_uuid"
                if path is not None
                else Path("/etc/syncoj/machine_uuid")
            ),
            # {desktop} 在这里就展开（按运行账号探测家目录与桌面）；
            # {player_no} 要等注册后才展开
            deploy_root=_templated_path(
                parser.get("agent", "deploy_root", fallback="{desktop}") or "{desktop}",
                user=expand_as,
            ),
            scan_roots=[
                Path(expand_placeholders(item, user=expand_as))
                for item in _split_paths(parser.get("scan", "roots", fallback="") or "{desktop}/{player_no}")
            ],
            scan_prefix=(parser.get("scan", "prefix", fallback=PREFIX_NONE) or PREFIX_NONE).strip(),
            scan_interval=int(parser.get("scan", "interval", fallback="30")),
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
            upgrade_mode=(parser.get("upgrade", "mode", fallback="apply") or "apply").strip().lower(),
            upgrade_mode_explicit=(
                path is not None and parser.has_option("upgrade", "mode")
            ),
            install_root=Path(
                parser.get("upgrade", "install_root", fallback="/opt/syncoj").strip()
                or "/opt/syncoj"
            ),
            release_public_key=_opt_path(parser.get("upgrade", "public_key", fallback="")),
        )

        config.run_user = expand_as or ""

        # 默认 apply 只是"想让机器能自动升级"；没有信任锚时它升不了，而**不能**
        # 因为一个默认值让整台机器起不来 —— 那会让所有没打包发布公钥的镜像一起
        # 失联。所以这里降级成 off 并吵一句。显式写了 mode=apply 却没配公钥是
        # 另一回事（配置错误），留给 validate() 报出来。
        if (
            config.upgrade_mode != "off"
            and config.release_public_key is None
            and not config.upgrade_mode_explicit
        ):
            log.warning(
                "upgrade.mode 用的是默认值 %s，但没配置 upgrade.public_key"
                "（没有信任锚，无法验证发布包签名）—— 本次按 off 运行。"
                "要启用自动升级：让安装包带上 release-key.pub.json，或显式指定 --public-key。",
                config.upgrade_mode,
            )
            config.upgrade_mode = "off"

        _apply_env_overrides(config)
        config.state_dir = config.state_dir.expanduser()
        return config


def _opt_path(raw: Optional[str]) -> Optional[Path]:
    text = (raw or "").strip()
    return Path(text).expanduser() if text else None


def _dir_writable_problems(path: Path, label: str) -> List[str]:
    """目录写不了时给出人能看懂的问题（目录还不存在不算错）。

    注册运行必须能建出 ``state_dir`` 并把凭据写进去 —— 写不进去时最好在这里
    就说清是哪个目录，而不是等到写的那一刻抛一句 OSError。
    """
    if not path.is_absolute():
        return ["%s 必须是绝对路径: %s" % (label, path)]
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        return []
    if os.access(str(probe), os.W_OK):
        return []
    if probe == path:
        return ["%s 不可写: %s" % (label, path)]
    return ["%s 无法创建（%s 不可写）: %s" % (label, probe, path)]


def _templated_path(raw: str, user: Optional[str] = None) -> Path:
    """展开 ``{desktop}`` / ``{home}`` 与 ``~``，但**保留** ``{player_no}``。

    ``user`` 指定按谁的家展开（见 :func:`expand_placeholders`）。
    """
    return Path(expand_placeholders(raw.strip() or "{desktop}", user=user)).expanduser()


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
        # 展开账号跟着 config.run_user（镜像预装时的 {home}/{desktop} 也是
        # 那个选手账号的家，不是装镜像那个人）。
        config.scan_roots = [
            Path(expand_placeholders(item, user=config.run_user))
            for item in _split_paths(roots)
        ]

    for env_name, attr in (("SYNCOJ_VERIFY_TLS", "verify_tls"), ("SYNCOJ_LOG_STDERR", "log_to_stderr")):
        if env_name in os.environ:
            setattr(config, attr, _as_bool(os.environ[env_name]))
