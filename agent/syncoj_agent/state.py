"""本地状态：凭据、机器标识、哈希缓存。

这些是 Agent **唯一的本地持久状态**，而且都是可再生的：

* 凭据丢了 —— 拿 `machine_uuid` / 硬件指纹重新注册（快照还原后的标准路径）
* 机器身份丢了 —— 重新生成一个 UUID，靠硬件指纹让服务端认回老机器
* 哈希缓存丢了 —— 重新算一遍，只是慢一点
* 下载分片丢了 —— 从头发，服务端不知道也不关心

所以本进程可以被随意 kill、状态目录可以被随意删除，都不会不可恢复。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import socket
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "Credential",
    "HashCache",
    "STATE_READY",
    "STATE_UNCLAIMED",
    "STATE_WAITING",
    "atomic_write_text",
    "resolve_machine_id",
    "describe_os",
    "detect_desktop",
    "DESKTOP_CANDIDATES",
]

#: 三态的名字。``claimed`` 与 ``bound`` 两个布尔量的组合在很多地方都要判断，
#: 所以字符串只在一处产生（``Credential.state``），其余地方一律用这些常量 ——
#: 手写字符串拼错时不会报错，只会走进一个"永远不成立"的分支。
STATE_UNCLAIMED = "unclaimed"
STATE_WAITING = "waiting"
STATE_READY = "ready"

log = logging.getLogger(__name__)

#: 哈希缓存落盘上限。超过就整体丢弃重建，避免无限增长
MAX_CACHE_ENTRIES = 20000


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """原子写文本文件。

    先写同目录的临时文件再 ``os.replace`` —— 保证任何时刻磁盘上要么是旧内容
    要么是新内容，不会出现被截断的半截文件。断电场景下这点很重要：凭据文件
    写了一半会导致 Agent 认为"未注册"而反复重新注册。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass  # Windows 上 chmod 语义有限
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # 损坏的 JSON 直接当没有，调用方会走重新注册/重算路径
        log.warning("状态文件损坏，将重建: %s (%s)", path, exc)
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# 机器标识
# --------------------------------------------------------------------------- #


def resolve_machine_id(configured: str, state_dir: Path) -> str:
    """确定本机标识。

    优先用 ``/etc/machine-id`` —— 它是机器级稳定标识，且 NOI Linux 整机快照
    还原时**不会变化**（它在镜像里就固定了），这正是"还原后能认出是老机器"
    所依赖的前提。

    退路：配置项 -> dbus machine-id -> 状态目录里持久化的随机 UUID。
    最后这条不理想（状态目录被删就变了），但好过没有。
    """
    if configured:
        return _sanitize(configured)

    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
            if value:
                return _sanitize(value)
        except (OSError, UnicodeDecodeError):
            continue

    # Windows 开发机 / 特殊环境：用注册表 MachineGuid 或主机名
    try:
        import winreg  # type: ignore  # noqa: F401  (仅 Windows 存在)

        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")  # type: ignore[attr-defined]
            if value:
                return _sanitize(str(value))
    except Exception:  # pragma: no cover - 非 Windows 或权限不足
        pass

    fallback_file = state_dir / "machine_id"
    data = _read_json(fallback_file)
    if data and isinstance(data.get("machine_id"), str) and data["machine_id"]:
        return _sanitize(data["machine_id"])

    generated = _sanitize("%s-%s" % (socket.gethostname(), uuid.uuid4().hex))
    try:
        atomic_write_text(
            fallback_file, json.dumps({"machine_id": generated}, indent=2), mode=0o600
        )
    except OSError:
        log.warning("无法持久化 machine_id，重启后可能导致重复注册")
    return generated


def _sanitize(raw: str) -> str:
    """machine-id 会出现在 URL、日志、数据库里，收紧字符集最省心。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip())
    return cleaned[:128] or "unknown"


# --------------------------------------------------------------------------- #
# 机器身份：UUID 与硬件指纹
# --------------------------------------------------------------------------- #

#: 硬件指纹的读取位置。SMBIOS UUID 是**物理机器**的标识，整机快照还原后
#: 依然是同一个值 —— 这正是"还原之后能认出原来那台机器"所依赖的东西。
#:
#: 它和 ``/etc/machine-id`` 的区别很关键：machine-id 是**文件**，会随镜像克隆
#: 被整批复制成同一个值；而 SMBIOS UUID 来自主板，克隆镜像不会克隆硬件。
_FINGERPRINT_SOURCES = (
    "/sys/class/dmi/id/product_uuid",
    "/sys/devices/virtual/dmi/id/product_uuid",
)


def resolve_machine_uuid(
    state_dir: Path, authoritative_path: Optional[Path] = None
) -> str:
    """本机身份 UUID，**优先读权威那份**（``<config-dir>/machine_uuid``）。

    权威那份由安装器以 root 写（0644），选手账号改不动；远程卸载的授权令牌就是
    拿它当判据的（服务端签令牌时绑这个值，卸载脚本比对本地这个文件）。用状态
    目录那份当判据是不行的 —— 那份归选手账号，等于可以把别的机器泄漏的令牌
    搬来删本机。

    读不到权威那份（老版本装的机器）才退回状态目录里那份：**并记一条警告** ——
    那条路仍然能用，但"令牌绑的是这台机器"这件事就不再是硬保证了。

    生成时机也有讲究：**必须在首次开机之后**。如果在建镜像时跑过一次 Agent
    （哪怕只是 ``--check``，它已经会建状态目录），UUID 就被烙进镜像了，
    等于没生成 —— 而且是"看起来做了防护"的那种没做。installer 会在建镜像时
    检查状态目录里有没有残留凭据并告警。
    """
    if authoritative_path is not None:
        existing = _read_text(authoritative_path)
        if existing:
            return _sanitize(existing)
        log.warning(
            "没有权威机器身份 %s，退回状态目录里那份 —— "
            "它的判据可被选手账号改写，远程卸载的绑定强度会打折",
            authoritative_path,
        )

    path = state_dir / "machine_uuid"
    existing = _read_text(path)
    if existing:
        return _sanitize(existing)

    generated = uuid.uuid4().hex
    try:
        atomic_write_text(path, generated + "\n", mode=0o600)
    except OSError as exc:
        log.warning("无法持久化 machine_uuid（%s），重启后可能被当成新机器", exc)
    return generated


def resolve_machine_fingerprint() -> str:
    """硬件指纹。读不到就返回空串 —— **不要**退回主机名或 machine-id。

    退回主机名会让一批同名机器互相认成同一台；退回 machine-id 则会在克隆镜像
    时整批相同。两种"退而求其次"都会把"认回原机器"变成"认错机器"，
    而认错的代价是一个学生的代码落进另一个人的目录，且完全静默。

    空串是安全的：服务端见到空指纹就当新机器处理，走人工配对。
    """
    for candidate in _FINGERPRINT_SOURCES:
        value = _read_text(candidate)
        if value:
            cleaned = _sanitize(value)
            # 虚拟机与某些 OEM 主板会给出全 0 或全 F 的 UUID，那不是身份
            compact = cleaned.replace("-", "").lower()
            if set(compact) <= {"0"} or set(compact) <= {"f"}:
                continue
            return cleaned
    return ""


def _read_text(path: Path) -> str:
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _read_text_or_none(path: Path) -> Optional[str]:
    """读文件；**不存在返回 None，读不到返回空串**。

    两者的区别很重要：前者是"这里没有这个文件"，后者是"有这个文件但我没权限"。
    统一密钥文件必须区分这两种情况 —— 权限不足是部署问题（Agent 以选手身份
    运行，本来就读不到 root 只读的文件），得说出来；文件不存在则说明装机时
    那一步没做，要提示去补。
    """
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("无法读取 %s：%s", path, exc)
        return ""


def read_bootstrap_key(path: Optional[Path]) -> Optional[str]:
    """读统一密钥文件。

    返回 ``None`` 表示"没有配置或文件不存在"；返回空串表示"文件在但读不到"。
    调用方要能区分这两者，见 ``_read_text_or_none``。
    """
    if path is None:
        return None
    raw = _read_text_or_none(Path(path))
    if raw is None:
        return None
    return raw


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #


@dataclass
class Credential:
    token: str
    player_no: str = ""
    contest_id: int = 0
    contest_slug: str = ""
    contest_name: str = ""
    #: 服务端分配的机器编号。未配对时可能是 0 —— 凭据照样是可用的，
    #: 见 ``is_usable()``
    agent_id: int = 0
    machine_id: str = ""
    server_url: str = ""
    #: 是否已经配对到**人**（名单条目）。
    #: 老版本写的凭据没有这个字段，反序列化时默认为 True —— 那时只有"已配对"
    #: 这一种，默认成 False 会让所有老机器升级后突然开始等配对。
    claimed: bool = True
    #: 是否已经能干活了（绑定好了，而且当前有包含本人的场次）。
    #:
    #: ``claimed`` 与 ``bound`` 是**两个正交的布尔量**，组合出三种状态：
    #:
    #: ===========  ==========  ============================
    #: claimed      bound       含义
    #: ===========  ==========  ============================
    #: False        False       还没配对到人
    #: True         False       配对好了，但没有含本人的场次
    #: True         True        正常
    #: ===========  ==========  ============================
    #:
    #: 为什么需要中间那一档：机器绑的是**人**，而"这场比赛有没有这个人"取决于
    #: 那份名单有没有被应用到场次里。所以配对成功不等于马上能干活 ——
    #: 分不清的话，Agent 会去扫一个用准考证号展开不出来的目录。
    bound: bool = True
    #: 未配对时的六位短码。它要被人从这台机器上读出来，所以明文留在这里 ——
    #: 服务端那边只有哈希，明文只在注册/tick 响应里下发过
    pair_code: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Credential":
        claimed = bool(data.get("claimed", True))
        return cls(
            token=str(data.get("token", "")),
            player_no=str(data.get("player_no", "")),
            contest_id=int(data.get("contest_id", 0) or 0),
            contest_slug=str(data.get("contest_slug", "")),
            contest_name=str(data.get("contest_name", "")),
            agent_id=int(data.get("agent_id", 0) or 0),
            machine_id=str(data.get("machine_id", "")),
            server_url=str(data.get("server_url", "")),
            claimed=claimed,
            # 没配对到人就不可能"已经能干活"：把这种自相矛盾的文件当成未配对，
            # 而不是相信那个 bound=True
            bound=bool(data.get("bound", True)) and claimed,
            pair_code=str(data.get("pair_code", "")),
        )

    def is_usable(self) -> bool:
        """有 token 就能用 —— 未配对的机器也有 token。

        **不要**把 ``agent_id > 0`` 当成可用的条件：未配对的机器可能压根没有
        agent_id，按那种判据会被当成"没有凭据"而反复重新注册，刷限速、刷审计，
        而真正的原因（还没配对）永远显示不出来。
        """
        return bool(self.token)

    @property
    def state(self) -> str:
        """三态之一：``STATE_UNCLAIMED`` / ``STATE_WAITING`` / ``STATE_READY``。

        三个状态看起来都像"注册成功了"，但该做的事完全不同 —— 所以这里给出
        一个**唯一**的判据，而不是让每个调用点各自去拼 ``claimed`` 和 ``bound``
        （少拼一个条件就会出现"未配对的机器跑去扫描"这类错误）。

        刻意**不**再提供 ``needs_pairing`` / ``waiting_for_contest`` 这类布尔
        快捷方式：它们与本方法表达的是同一件事，多一份就会漂移 —— 判决改了一处、
        忘了另一处，而错的那一处恰好是"要不要扫代码"。
        """
        if not self.claimed:
            return STATE_UNCLAIMED
        return STATE_READY if self.bound else STATE_WAITING


def load_credential(path: Path) -> Optional[Credential]:
    data = _read_json(path)
    if data is None:
        return None
    credential = Credential.from_dict(data)
    return credential if credential.is_usable() else None


def save_credential(path: Path, credential: Credential) -> None:
    atomic_write_text(path, credential.to_json(), mode=0o600)


# --------------------------------------------------------------------------- #
# 哈希缓存
# --------------------------------------------------------------------------- #


@dataclass
class HashCache:
    """``(路径, mtime, size) -> sha256`` 的缓存。

    代码目录里绝大多数文件在绝大多数轮次里都没变。没有缓存的话，每轮 tick 都
    要把全部文件重新读一遍算 SHA-256 —— 50 个文件或许还能忍，但如果选手目录
    里堆了几百 MB 的测试数据，纯属浪费 IO。有了缓存，稳态下几乎不读盘。
    """

    entries: Dict[str, List[object]] = field(default_factory=dict)
    _hits: int = 0
    _misses: int = 0

    @classmethod
    def load(cls, path: Path) -> "HashCache":
        data = _read_json(path)
        if not data:
            return cls()
        raw = data.get("entries")
        if not isinstance(raw, dict):
            return cls()
        entries: Dict[str, List[object]] = {}
        for key, value in raw.items():
            if (
                isinstance(value, list)
                and len(value) == 3
                and isinstance(value[2], str)
                and len(value[2]) == 64
            ):
                entries[key] = value
        return cls(entries=entries)

    def save(self, path: Path) -> None:
        payload = {"version": 1, "entries": self.entries}
        atomic_write_text(path, json.dumps(payload), mode=0o600)

    def lookup(self, rel_path: str, mtime: int, size: int) -> Optional[str]:
        value = self.entries.get(rel_path)
        if value is None:
            self._misses += 1
            return None
        if value[0] == mtime and value[1] == size and isinstance(value[2], str):
            self._hits += 1
            return value[2]
        self._misses += 1
        return None

    def store(self, rel_path: str, mtime: int, size: int, digest: str) -> None:
        self.entries[rel_path] = [mtime, size, digest]

    def prune_to(self, keep: Optional[set]) -> None:
        """丢弃本轮未出现的条目，防止改名/删除的残留无限堆积。"""
        if keep is None:
            return
        self.entries = {k: v for k, v in self.entries.items() if k in keep}

    @property
    def stats(self) -> Dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self.entries)}

    def __len__(self) -> int:
        return len(self.entries)


# --------------------------------------------------------------------------- #
# 环境描述
# --------------------------------------------------------------------------- #


def describe_os() -> str:
    """给教师端看的系统描述，用于排查"这台机器到底是什么环境"。"""
    parts = [platform.system(), platform.release()]
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    parts.append(line.split("=", 1)[1].strip().strip('"'))
                    break
    except (OSError, UnicodeDecodeError):
        pass
    return " ".join(p for p in parts if p)[:200]


# --------------------------------------------------------------------------- #
# 桌面目录探测
# --------------------------------------------------------------------------- #

#: 桌面目录的候选名字。中文 locale 是「桌面」，英文是 Desktop，
#: 而 NOI Linux 各版本的中文环境不一定都装了语言包，所以两种都得认。
DESKTOP_CANDIDATES = ("Desktop", "桌面", "desktop")


def detect_desktop(home: Optional[Path] = None) -> Path:
    """找出当前用户的桌面目录。

    顺序：
    1. ``~/.config/user-dirs.dirs`` 里的 ``XDG_DESKTOP_DIR``（最权威 ——
       用户可能把桌面挪到别处，或者设成英文名）
    2. ``~/桌面`` / ``~/Desktop`` / ``~/desktop`` 里**实际存在**的那个
    3. 都不存在时返回 ``~/桌面``

    第 3 条是刻意的：探测不出就按中文环境猜，而不是退回家目录 ——
    退回家目录会让 Agent 把整个家目录当成工作区扫，收到一堆无关文件。
    """
    home_dir = Path(home) if home else Path.home()

    from_xdg = _desktop_from_xdg_config(home_dir)
    if from_xdg is not None:
        return from_xdg

    for name in DESKTOP_CANDIDATES:
        candidate = home_dir / name
        if candidate.is_dir():
            return candidate

    return home_dir / "桌面"


def declared_xdg_desktop(home_dir: Optional[Path] = None) -> Optional[Path]:
    """``~/.config/user-dirs.dirs`` 里**声明**的桌面目录；没声明返回 ``None``。

    与 :func:`detect_desktop` 的区别只在"要求目录存在吗"：探测要能真的写文件，
    所以声明的路径不在就跳过；而"配置里的下发落点与 GNOME 显示的桌面是不是同一个"
    这件事要的是**声明值**本身 —— 声明的目录不存在（家目录不对、桌面被删了）
    同样值得警告。
    """
    home = Path(home_dir) if home_dir else Path.home()
    config_file = home / ".config" / "user-dirs.dirs"
    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError):
        return None

    for line in content.splitlines():
        text = line.strip()
        if not text.startswith("XDG_DESKTOP_DIR"):
            continue
        _, _, value = text.partition("=")
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        # 文件里写的是 $HOME/... 这种 shell 展开形式
        value = value.replace("$HOME", str(home)).replace("${HOME}", str(home))
        return Path(value).expanduser()
    return None


def _desktop_from_xdg_config(home_dir: Path) -> Optional[Path]:
    """读 ``~/.config/user-dirs.dirs``，**只在声明的目录真的存在时**认它。"""
    candidate = declared_xdg_desktop(home_dir)
    if candidate is not None and candidate.is_dir():
        return candidate
    return None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
