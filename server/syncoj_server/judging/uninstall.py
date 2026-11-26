"""评测机远程卸载（§12.10）：告别服务端，再按安装方式清理自己。

管理端「卸载」→ poll 响应 ``command = "uninstall"`` → 评测机不再领新任务、评完手上的、交完结果
→ ``POST /judge/goodbye``（服务端删掉这一行，令牌失效）→ 按 ``install.json`` 记的安装方式清理：

* ``systemd``（install.sh 装的）：服务可能以非 root 账号运行，删不了单元。评测机只在缓存目录写一个
  标记文件后以退出码 0 退出；单元的 ``ExecStopPost=+…/uninstall.sh --if-requested``（root）看到标记，
  用 ``systemd-run`` 脱离本单元去停服务、删单元、程序、令牌、缓存与账号。
* ``schtask``（install.ps1 装的）：计划任务以 LOCAL SERVICE 运行，删不了任务。同样写标记后退出；
  另一个以 SYSTEM 运行的计划任务「SyncOJ Judge Cleanup」每 2 分钟看一次标记，有就执行 uninstall.ps1。
* ``local``（服务端拉起的本机评测机）：服务端在告别时删目录，这里什么都不做。
* 没有 ``install.json``（手动运行）：删自己的令牌文件（删得掉的话）与缓存里自己写的文件；
  程序文件要到那台机器上手动删。

标记文件只看在不在，内容不被任何一方读取或执行。告别失败（服务端连不上）时**不清理**、以退出码 1
退出：systemd / 计划任务会把它拉起来，新进程照样收到卸载命令（服务端不复位它）再来一遍。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("syncoj.judge")

KINDS = ("systemd", "schtask", "local", "manual")
MARKER_NAME = ".uninstall-requested"
MANIFEST_NAME = "install.json"
GOODBYE_ATTEMPTS = 4


class Manifest:
    """``install.json``：``{"kind", "service", "marker", "paths", "accounts"}``。

    评测机只用 ``kind`` 与 ``marker``；``paths`` / ``accounts`` 给人看（真正删什么由 root 拥有的
    卸载脚本自己定，不信这份文件）。"""

    def __init__(self, kind: str = "manual", marker: Optional[str] = None, source: Optional[Path] = None) -> None:
        self.kind = kind
        self.marker = Path(marker) if marker else None
        self.source = source

    def marker_path(self, cache_dir: Path) -> Path:
        return self.marker if self.marker is not None else Path(cache_dir) / MARKER_NAME

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "Manifest(kind=%r, marker=%r)" % (self.kind, self.marker)


def default_manifest_path(argv0: Optional[str] = None) -> Optional[Path]:
    """默认在 pyz 同目录找 ``install.json``（``python …/syncoj-judge.pyz`` 启动时）。"""
    argv0 = argv0 if argv0 is not None else (sys.argv[0] if sys.argv else "")
    if not argv0 or not os.path.isfile(argv0):
        return None
    try:
        if not zipfile.is_zipfile(argv0):
            return None
    except OSError:
        return None
    return Path(argv0).resolve().parent / MANIFEST_NAME


def load_manifest(path: Optional[Path]) -> Manifest:
    """读 ``install.json``；没有 → 手动运行。读不懂（损坏、kind 不认识）也按手动运行，并告警。"""
    if path is None or not Path(path).is_file():
        return Manifest("manual")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("读不懂安装清单 %s（%s）：卸载时按手动运行处理", path, exc)
        return Manifest("manual", source=Path(path))
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind not in KINDS:
        log.warning("安装清单 %s 的 kind 不认识（%r）：卸载时按手动运行处理", path, kind)
        return Manifest("manual", source=Path(path))
    marker = data.get("marker")
    if marker is not None and (not isinstance(marker, str) or not os.path.isabs(marker)):
        log.warning("安装清单 %s 的 marker 不是绝对路径，改用缓存目录下的标记", path)
        marker = None
    return Manifest(kind, marker, Path(path))


def already_requested(manifest: Manifest, cache_dir: Path) -> bool:
    """服务 / 计划任务装的评测机：标记已在（告别过、等 root / SYSTEM 来清理）时不再启动。"""
    if manifest.kind not in ("systemd", "schtask"):
        return False
    return os.path.lexists(str(manifest.marker_path(cache_dir)))


def goodbye(transport: Any, kind: str, sleep: Callable[[float], None] = time.sleep) -> bool:
    """告诉服务端「我卸了」。服务端删了行（200）或令牌已失效（401，被吊销或告别过）都算成功。"""
    for attempt in range(GOODBYE_ATTEMPTS):
        try:
            status, raw = transport.request("POST", "/api/v1/judge/goodbye", {"kind": kind})
        except Exception as exc:  # noqa: BLE001 - 连不上：退避后重试
            status, raw = 0, str(exc).encode("utf-8", "replace")
        if status == 200:
            log.info("已向服务端告别，这台评测机已从管理端删除")
            return True
        if status == 401:
            log.info("令牌已失效（已被吊销或已告别过）：照样清理")
            return True
        if status == 409:
            log.warning("服务端说没有要求卸载（%s）：不清理", raw[:200].decode("utf-8", "replace"))
            return False
        log.warning("告别失败（第 %d 次）：HTTP %d %s", attempt + 1, status, raw[:200].decode("utf-8", "replace"))
        if attempt + 1 < GOODBYE_ATTEMPTS:
            sleep(min(2.0 ** attempt, 10.0))
    return False


def request_cleanup(marker: Path) -> bool:
    """写标记文件（内容无关紧要，只看在不在）。"""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("uninstall\n")
    except OSError as exc:
        log.error("写不了卸载标记 %s：%s。请到这台机器上手动运行卸载脚本", marker, exc)
        return False
    log.info("已写卸载标记 %s：退出后由安装时注册的清理程序删除服务、程序、令牌与缓存", marker)
    return True


def _remove_cache(cache_dir: Path) -> None:
    """只删评测机自己写进缓存目录的东西（数据包 ``<sha256>.zip``、下载中的 ``.part``、评测时的
    源码目录），删空了再删目录 —— ``--cache-dir`` 是用户给的，不能整个 rmtree。"""
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return
    for entry in cache_dir.iterdir():
        name = entry.name
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir() and name.startswith("syncoj-judge-src-"):
                shutil.rmtree(str(entry), ignore_errors=True)
            elif entry.is_file() and (name.endswith(".zip") or name.endswith(".part")) and len(name.split(".")[0]) == 64:
                entry.unlink()
        except OSError as exc:
            log.warning("删不掉缓存 %s：%s", entry, exc)
    try:
        cache_dir.rmdir()
    except OSError:
        pass


def cleanup_manual(token_file: Path, cache_dir: Path, program: Optional[str] = None) -> None:
    try:
        Path(token_file).unlink()
        log.info("已删除令牌文件 %s", token_file)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("删不掉令牌文件 %s（%s）：它已经失效，可以手动删除", token_file, exc)
    _remove_cache(cache_dir)
    log.warning("手动运行的评测机：程序文件（%s）请到这台机器上手动删除", program or "syncoj-judge")


def finish(
    transport: Any,
    manifest: Manifest,
    token_file: Path,
    cache_dir: Path,
    *,
    program: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """收到卸载命令、手上的任务都交了之后调用：告别，再按安装方式清理。返回进程退出码。"""
    if not goodbye(transport, manifest.kind, sleep=sleep):
        log.error("没能告别服务端：这次不清理，退出后会被重新拉起再试")
        return 1
    if manifest.kind in ("systemd", "schtask"):
        return 0 if request_cleanup(manifest.marker_path(cache_dir)) else 1
    if manifest.kind == "local":
        return 0
    cleanup_manual(token_file, cache_dir, program)
    return 0
