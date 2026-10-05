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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .state import HashCache, sha256_file

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
    path: str
    sha256: str
    size: int
    mtime: int

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

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)


def scan_directory(
    root: Path,
    policy: ScanPolicy,
    cache: Optional[HashCache] = None,
    max_files: int = 5000,
) -> ScanOutcome:
    """扫描一个根目录，返回全部符合条件的文件索引。"""
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
    seen_this_round = set()

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
                        seen_this_round=seen_this_round,
                        exclude_dirs=exclude_dirs,
                        exclude_suffixes=exclude_suffixes,
                        extensions=extensions,
                    )
                except OSError as exc:
                    # 单个文件读不了（权限、被独占）不该让整轮扫描失败，
                    # 但必须标记不完整，否则该文件会被误判为"已删除"
                    outcome.complete = False
                    outcome.errors.append("处理 %s 失败: %s" % (entry.name, exc))
                if len(outcome.entries) >= max_files:
                    outcome.truncated = True
                    outcome.complete = False
                    outcome.errors.append("文件数超过上限 %d，已截断" % max_files)
                    stack = []
                    break

    if cache is not None:
        cache.prune_to(seen_this_round)
    return outcome


def _handle_entry(
    entry: "os.DirEntry[str]",
    base: Path,
    policy: ScanPolicy,
    cache: Optional[HashCache],
    outcome: ScanOutcome,
    stack: List[Path],
    seen_this_round: set,
    exclude_dirs: set,
    exclude_suffixes: tuple,
    extensions: set,
) -> None:
    name = entry.name
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

    rel_path = _relative_posix(Path(entry.path), base)
    if rel_path is None:
        outcome.complete = False
        outcome.errors.append("无法计算相对路径: %s" % entry.path)
        return

    seen_this_round.add(rel_path)
    mtime = int(stat.st_mtime)
    size = int(stat.st_size)

    digest: Optional[str] = None
    if cache is not None:
        digest = cache.lookup(rel_path, mtime, size)
    if digest is None:
        digest = sha256_file(Path(entry.path))
        if cache is not None:
            cache.store(rel_path, mtime, size, digest)

    outcome.entries.append(
        FileEntry(path=rel_path, sha256=digest, size=size, mtime=mtime)
    )


def _relative_posix(path: Path, base: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return None
