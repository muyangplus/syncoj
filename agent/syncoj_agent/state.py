"""本地状态：凭据、机器标识、哈希缓存。

这些是 Agent **唯一的本地持久状态**，而且都是可再生的：

* 凭据丢了 —— 用注册码重新注册（快照还原后的标准路径）
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
    "atomic_write_text",
    "resolve_machine_id",
    "describe_os",
    "detect_desktop",
    "DESKTOP_CANDIDATES",
]

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
# 凭据
# --------------------------------------------------------------------------- #


@dataclass
class Credential:
    token: str
    agent_id: int
    player_no: str
    contest_id: int
    contest_slug: str
    contest_name: str = ""
    machine_id: str = ""
    server_url: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Credential":
        return cls(
            token=str(data.get("token", "")),
            agent_id=int(data.get("agent_id", 0) or 0),
            player_no=str(data.get("player_no", "")),
            contest_id=int(data.get("contest_id", 0) or 0),
            contest_slug=str(data.get("contest_slug", "")),
            contest_name=str(data.get("contest_name", "")),
            machine_id=str(data.get("machine_id", "")),
            server_url=str(data.get("server_url", "")),
        )

    def is_usable(self) -> bool:
        return bool(self.token) and self.agent_id > 0


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


def _desktop_from_xdg_config(home_dir: Path) -> Optional[Path]:
    """读 ``~/.config/user-dirs.dirs``。

    文件里是 shell 变量形式：``XDG_DESKTOP_DIR="$HOME/桌面"``。
    这里只做最小的解析，不引入任何依赖。
    """
    config_file = home_dir / ".config" / "user-dirs.dirs"
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
        value = value.replace("$HOME", str(home_dir)).replace("${HOME}", str(home_dir))
        candidate = Path(value).expanduser()
        if candidate.is_dir():
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
