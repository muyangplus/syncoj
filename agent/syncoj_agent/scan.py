"""代码目录扫描。

**为什么不用 inotify / watchdog**
--------------------------------
inotify 是"事件流"模型，它有一个致命性质：**进程不在的时候事件就丢了**。
Agent 被杀、机器重启、快照还原期间的任何改动，inotify 都看不见，而代码回收
恰恰最需要"机器刚恢复时把现状完整捞回来"。

周期全量扫描是"状态快照"模型，天然没有这个问题：不关心发生过什么，只关心
现在有什么。代价是要周期性地遍历目录 —— 但选手代码目录只有几十个文件，
配合 ``(mtime, size) -> sha256`` 缓存后，稳态下几乎不读盘。

顺带还省掉了 ``fs.inotify.max_user_watches`` 调优（Ubuntu 20.04 默认只有 8192，
递归 watch 一个稍大的目录树就会耗尽）。
"""

from __future__ import annotations

import logging
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .state import HashCache, file_fingerprint, sha256_file

__all__ = ["FileEntry", "ScanOutcome", "scan_directory", "ScanPolicy"]

log = logging.getLogger(__name__)


@dataclass
class ScanPolicy:
    extensions: Sequence[str]
    exclude_dirs: Sequence[str]
    exclude_suffixes: Sequence[str]
    max_file_size: int

    @classmethod
    def from_mapping(cls, mapping: Dict[str, object]) -> "ScanPolicy":
        return cls(
            extensions=[str(x).lower() for x in mapping.get("extensions", [])],  # type: ignore[union-attr]
            exclude_dirs=[str(x).lower() for x in mapping.get("exclude_dirs", [])],  # type: ignore[union-attr]
            exclude_suffixes=[str(x).lower() for x in mapping.get("exclude_suffixes", [])],  # type: ignore[union-attr]
            max_file_size=int(mapping.get("max_file_size", 2 * 1024 * 1024)),  # type: ignore[arg-type]
        )


@dataclass
class FileEntry:
    #: 上报用的相对路径（**NFC 归一化**之后；服务端也按 NFC 存）
    path: str
    sha256: str
    size: int
    mtime: int
    #: 本地真实路径。上传必须按它打开 —— 磁盘上的名字可能是 NFD，
    #: 与 ``path`` 拼回去的 NFC 名字在 Linux 上是**另一个文件**。
    local: Optional[Path] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mtime": self.mtime,
        }


@dataclass
class ScanOutcome:
    root: str
    entries: List[FileEntry] = field(default_factory=list)
    #: 扫描是否完整。False 时服务端会跳过删除判定 —— 漏扫不等于文件被删
    complete: bool = True
    errors: List[str] = field(default_factory=list)
    oversize: List[str] = field(default_factory=list)
    #: 命中大小上限而被截断（文件数超限）
    truncated: bool = False
    #: 本轮用到的哈希缓存键（调用方汇总所有根之后统一 prune）
    cache_keys: Set[str] = field(default_factory=set)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)


def scan_directory(
    root: Path,
    policy: ScanPolicy,
    cache: Optional[HashCache] = None,
    max_files: int = 5000,
    cache_namespace: str = "",
) -> ScanOutcome:
    """扫描一个根目录，返回全部符合条件的文件索引。

    ``cache_namespace`` 区分不同扫描根在哈希缓存里的键（同一相对路径在两个根里
    是两个文件）。本函数**不** prune 缓存：调用方扫完全部根之后，拿各根的
    ``cache_keys`` 合起来 prune 一次 —— 逐根 prune 会把别的根的条目清掉。

    ``max_files`` 是这一个根还能收多少个；正好收满不算截断。
    """
    outcome = ScanOutcome(root=root.as_posix())
    try:
        base = root.resolve()
    except OSError as exc:
        outcome.complete = False
        outcome.errors.append("无法解析扫描根目录 %s: %s" % (root, exc))
        return outcome

    if not base.is_dir():
        outcome.complete = False
        outcome.errors.append("扫描根目录不存在或不是目录: %s" % base)
        return outcome

    exclude_dirs = set(policy.exclude_dirs)
    exclude_suffixes = tuple(policy.exclude_suffixes)
    extensions = set(policy.extensions)
    seen_paths: Set[str] = set()

    # 显式栈而非递归：选手可能建出很深的目录树，递归会栈溢出
    stack: List[Path] = [base]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError as exc:
            outcome.complete = False
            outcome.errors.append("无法读取目录 %s: %s" % (current, exc))
            continue

        with iterator:
            for entry in iterator:
                try:
                    _handle_entry(
                        entry=entry,
                        base=base,
                        policy=policy,
                        cache=cache,
                        outcome=outcome,
                        stack=stack,
                        seen_paths=seen_paths,
                        cache_namespace=cache_namespace,
                        exclude_dirs=exclude_dirs,
                        exclude_suffixes=exclude_suffixes,
                        extensions=extensions,
                    )
                except OSError as exc:
                    # 单个文件读不了（权限、被独占）不该让整轮扫描失败，
                    # 但必须标记不完整，否则该文件会被误判为"已删除"
                    outcome.complete = False
                    outcome.errors.append("处理 %s 失败: %s" % (entry.name, exc))
                if len(outcome.entries) > max_files:
                    # 多出来的那一个才说明"还有没收的"；正好收满不是截断
                    outcome.entries.pop()
                    outcome.truncated = True
                    outcome.complete = False
                    outcome.errors.append("文件数超过上限 %d，已截断" % max_files)
                    stack = []
                    break

    return outcome


def _handle_entry(
    entry: "os.DirEntry[str]",
    base: Path,
    policy: ScanPolicy,
    cache: Optional[HashCache],
    outcome: ScanOutcome,
    stack: List[Path],
    seen_paths: Set[str],
    cache_namespace: str,
    exclude_dirs: set,
    exclude_suffixes: tuple,
    extensions: set,
) -> None:
    name = entry.name
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        # 文件名不是合法 UTF-8（Python 用代理字符表示那几个字节）。它进了报文，
        # json.dumps(...).encode("utf-8") 就抛异常 —— 曾经因此**每一轮** tick
        # 都失败，这台机器从此一个文件都收不上来。跳过它并说出来；不影响
        # complete（服务端从来没收到过它，谈不上"漏扫被当成删除"）。
        outcome.errors.append(
            "文件名不是合法 UTF-8，已跳过: %s" % name.encode("utf-8", "replace").decode("utf-8")
        )
        return
    lower = name.lower()

    # 隐藏文件与目录整个跳过（.git 等已在 exclude_dirs 里，这里兜住其余）
    if name.startswith("."):
        return

    # follow_symlinks=False：符号链接一律不跟随。跟随的话，选手放一个指向
    # /etc 或整个家目录的软链就能让 Agent 把系统文件当成代码回收上来
    if entry.is_dir(follow_symlinks=False):
        if lower in exclude_dirs:
            return
        stack.append(Path(entry.path))
        return

    if not entry.is_file(follow_symlinks=False):
        return  # 符号链接、设备文件、FIFO 等一律忽略

    if lower.endswith(exclude_suffixes):
        return

    dot = name.rfind(".")
    extension = lower[dot:] if dot > 0 else ""
    if extension not in extensions:
        return

    stat = entry.stat(follow_symlinks=False)
    if stat.st_size > policy.max_file_size:
        outcome.oversize.append(entry.name)
        return

    raw_rel = _relative_posix(Path(entry.path), base)
    if raw_rel is None:
        outcome.complete = False
        outcome.errors.append("无法计算相对路径: %s" % entry.path)
        return

    # 上报 NFC：服务端按 NFC 存、按 NFC 回 need_upload。曾经原样上报 NFD 名字，
    # 服务端回的是 NFC，Agent 用它查不到本地文件 —— 那个文件永远收不上来、
    # tick 一直停在"有活"的 2 秒节奏。本地仍按真实名字打开（FileEntry.local）。
    rel_path = unicodedata.normalize("NFC", raw_rel)
    if rel_path in seen_paths:
        # 两个磁盘名字归一化后相同（一个 NFC、一个 NFD）：只能收一个，说出来
        outcome.errors.append("归一化后与另一个文件同名，已跳过: %s" % rel_path)
        return
    seen_paths.add(rel_path)

    fingerprint = file_fingerprint(stat)
    cache_key = "%s\x00%s" % (cache_namespace, raw_rel)
    outcome.cache_keys.add(cache_key)

    digest: Optional[str] = None
    if cache is not None:
        digest = cache.lookup(cache_key, fingerprint)
    if digest is None:
        digest = sha256_file(Path(entry.path))
        if cache is not None:
            cache.store(cache_key, fingerprint, digest)

    outcome.entries.append(
        FileEntry(
            path=rel_path,
            sha256=digest,
            size=int(stat.st_size),
            mtime=int(stat.st_mtime),
            local=Path(entry.path),
        )
    )


def _relative_posix(path: Path, base: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return None
