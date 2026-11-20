"""文件下发：断点续传 + 校验 + 原子落盘。

几个不显眼但很关键的决定
------------------------
1. **分片按内容命名，集中放在 ``deploy_root/.syncoj-parts/<sha256>.part``**。
   曾经按 ``asset_id`` 命名、散在各个目标文件旁边：教师在线改了正文（同一个
   asset_id、新内容）之后，会拿旧分片去续新内容；找"有哪些分片"还得每轮把整个
   桌面 ``rglob`` 一遍。按 sha256 命名，内容一换自然从头来，同一份内容落到多处也
   只下一次。分片目录在 ``deploy_root`` 里（与落点同一文件系统，``os.replace``
   才是原子的；跨文件系统会 EXDEV）。
2. **续传位置以本地分片实际大小为准**。服务端不再给偏移。
3. **进度、完成、失败都按下发目标（target_id）由下一轮 tick 回报**，完成回执带
   落地内容的 sha256 —— 服务端只认与资产当前内容一致的回执。
4. **出错就丢连接，不把剩下的响应体读完**。曾经 ``finally: _drain(response)``：
   磁盘满或超时之后还要把几百 MB 的剩余内容读完再扔掉。
5. **下载有自己的截止时间**（调用方给），到点保留分片、下一轮接着续，不算失败。
"""

from __future__ import annotations

import errno
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .client import AgentClient
from .safepath import PathError, resolve_within
from .state import sha256_file

__all__ = [
    "DownloadOutcome",
    "download_asset",
    "PARTS_DIRNAME",
    "part_path_for",
    "part_size",
    "sweep_stale_parts",
]

log = logging.getLogger(__name__)

_CHUNK = 256 * 1024
#: 分片目录（在 deploy_root 下；隐藏目录，扫描与选手视线都会跳过它）
PARTS_DIRNAME = ".syncoj-parts"
PART_SUFFIX = ".part"
#: 老版本散落在目标文件旁边的分片后缀；启动清扫时顺手清掉
LEGACY_PART_SUFFIX = ".syncoj-part"

ProgressCallback = Callable[[int, int], None]


class _DeadlineReached(Exception):
    """本轮下载时间用完：分片保留，下一轮接着续。"""


@dataclass
class DownloadOutcome:
    asset_id: int
    dest: str
    #: "ok" | "skipped" | "partial"（时间用完，分片保留）| "failed"
    status: str
    bytes_written: int = 0
    total_bytes: int = 0
    error: Optional[str] = None
    target_id: int = 0
    sha256: str = ""
    #: 磁盘满：调用方应停下本轮剩下的下载（它们也会失败）
    disk_full: bool = False


def part_path_for(deploy_root: Path, sha256: str) -> Path:
    return Path(deploy_root) / PARTS_DIRNAME / (sha256 + PART_SUFFIX)


def part_size(deploy_root: Path, sha256: str) -> int:
    """本机这份内容的分片里已有多少字节（没有分片就是 0）。"""
    try:
        return part_path_for(deploy_root, sha256).stat().st_size
    except OSError:
        return 0


def _failed(job_fields, error: str, **extra) -> DownloadOutcome:
    asset_id, dest_rel, target_id, sha = job_fields
    return DownloadOutcome(
        asset_id=asset_id, dest=dest_rel, status="failed", error=error,
        target_id=target_id, sha256=sha, **extra
    )


def download_asset(
    client: AgentClient,
    job: dict,
    deploy_root: Path,
    on_progress: Optional[ProgressCallback] = None,
    deadline: Optional[float] = None,
) -> DownloadOutcome:
    """执行一个下发作业。不抛异常 —— 所有失败都转成 ``status="failed"``。

    ``deadline`` 是 ``time.monotonic()`` 口径的截止时刻；到点就停（``partial``），
    分片保留到下一轮。
    """
    try:
        asset_id = int(job["asset_id"])
        target_id = int(job.get("target_id") or 0)
        dest_rel = str(job["dest"])
        expected_sha = str(job["sha256"]).lower()
        total_size = int(job.get("size") or 0)
        mode = str(job.get("mode") or "overwrite")
    except (KeyError, TypeError, ValueError) as exc:
        return DownloadOutcome(
            asset_id=int(job.get("asset_id", 0) or 0),
            dest=str(job.get("dest", "")),
            status="failed",
            error="下发作业字段不合法: %s" % exc,
            target_id=int(job.get("target_id", 0) or 0),
        )
    fields = (asset_id, dest_rel, target_id, expected_sha)

    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        # 它会被拼进分片文件名：形状必须先卡死
        return _failed(fields, "sha256 格式不合法")

    # 服务端给的路径同样要过 Agent 的独立校验 —— 服务端也可能被攻破
    try:
        dest_path = resolve_within(deploy_root, dest_rel)
    except PathError as exc:
        log.error("拒绝不安全的下发路径 %r: %s", dest_rel, exc)
        return _failed(fields, "目标路径不合法: %s" % exc)

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failed(fields, "无法创建目标目录: %s" % exc, disk_full=_is_disk_full(exc))

    # 已存在且内容正确 -> 跳过（skip_exist 模式；overwrite 模式下内容相同也没必要重下）
    if dest_path.is_file():
        try:
            if sha256_file(dest_path) == expected_sha:
                return DownloadOutcome(
                    asset_id=asset_id, dest=dest_rel, status="skipped",
                    total_bytes=total_size, target_id=target_id, sha256=expected_sha,
                )
        except OSError:
            pass
        if mode == "skip_exist":
            # 内容不同但策略是"存在就不动"
            log.info("%s 已存在且模式为 skip_exist，保留原文件", dest_rel)
            return DownloadOutcome(
                asset_id=asset_id, dest=dest_rel, status="skipped",
                total_bytes=total_size, target_id=target_id, sha256=expected_sha,
            )

    part_path = part_path_for(deploy_root, expected_sha)
    try:
        part_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failed(fields, "无法创建分片目录: %s" % exc, disk_full=_is_disk_full(exc))
    local_have = 0
    try:
        if part_path.is_file():
            local_have = part_path.stat().st_size
    except OSError:
        local_have = 0

    # 本地分片比服务端声称的总大小还大 —— 已经损坏
    if total_size and local_have > total_size:
        _silent_unlink(part_path)
        local_have = 0

    written = 0
    if not total_size or local_have < total_size:
        try:
            written = _stream_to_part(
                client, asset_id, part_path, local_have, total_size, on_progress, deadline
            )
        except _DeadlineReached:
            return DownloadOutcome(
                asset_id=asset_id, dest=dest_rel, status="partial",
                bytes_written=0, total_bytes=total_size,
                target_id=target_id, sha256=expected_sha,
            )
        except Exception as exc:
            # 网络中断是常态：保留分片，下一轮从断点继续。**丢掉连接**而不是把剩下的
            # 响应体读完 —— 那可能是几百 MB，读完了也只是扔掉。
            client.drop_transfer_connection()
            prefix = "写盘失败: " if isinstance(exc, OSError) and not _is_network(exc) else ""
            return _failed(
                fields, "%s%s" % (prefix, exc),
                bytes_written=local_have, total_bytes=total_size,
                disk_full=isinstance(exc, OSError) and _is_disk_full(exc),
            )

    # 完整性校验：必须校验**整个**文件，不能只校验新下的尾部 ——
    # 断点续传的前提是本地分片本身可信，而它可能来自上一次崩溃。
    try:
        actual_size = part_path.stat().st_size
    except OSError as exc:
        return _failed(fields, "校验时读取失败: %s" % exc, bytes_written=written,
                       total_bytes=total_size)

    if total_size and actual_size != total_size:
        if actual_size < total_size:
            # 传完了却没到预期长度 = 连接中断或服务端提前收尾。
            # **必须保留分片**：删掉的话下一轮又从头下，弱网环境下大文件将永远
            # 传不完 —— 每轮都在同一个位置重新开始，永远走不到终点。
            return _failed(
                fields,
                "传输未完成：已收到 %d/%d 字节，下轮从此处续传" % (actual_size, total_size),
                bytes_written=written, total_bytes=total_size,
            )
        _silent_unlink(part_path)
        return _failed(fields, "分片大小 %d 超出预期 %d，已丢弃" % (actual_size, total_size),
                       bytes_written=written, total_bytes=total_size)

    try:
        actual_sha = sha256_file(part_path)
    except OSError as exc:
        return _failed(fields, "计算校验和失败: %s" % exc, bytes_written=written,
                       total_bytes=total_size)

    if actual_sha != expected_sha:
        _silent_unlink(part_path)
        return _failed(
            fields,
            "sha256 不符：期望 %s，实际 %s（已丢弃分片，下轮重下）"
            % (expected_sha[:12], actual_sha[:12]),
            bytes_written=written, total_bytes=total_size,
        )

    try:
        _place(part_path, dest_path)
    except OSError as exc:
        return _failed(fields, "落盘失败: %s" % exc, bytes_written=written,
                       total_bytes=total_size, disk_full=_is_disk_full(exc))
    try:
        # 分片目录空了就收起来：桌面上不该常驻一个隐藏目录
        part_path.parent.rmdir()
    except OSError:
        pass

    return DownloadOutcome(
        asset_id=asset_id, dest=dest_rel, status="ok",
        bytes_written=written, total_bytes=actual_size,
        target_id=target_id, sha256=expected_sha,
    )


def _place(part_path: Path, dest_path: Path) -> None:
    """把校验过的分片原子地放到目标位置（分片随之消失）。

    同一文件系统里直接 ``os.replace``：一步到位、评测器读不到半个文件。
    目标在另一个文件系统上（下发根下挂了别的盘，EXDEV）时，先复制到目标旁边的
    临时名再 ``os.replace``。同一份内容要落到好几处时，后几处会再下一遍 ——
    罕见，换来的是不必对每个大文件多拷一遍。
    """
    try:
        os.replace(str(part_path), str(dest_path))
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.EXDEV:
            raise
        import shutil

        tmp = dest_path.parent / (".%s.syncoj-tmp" % dest_path.name)
        try:
            shutil.copyfile(str(part_path), str(tmp))
            os.replace(str(tmp), str(dest_path))
        except OSError:
            _silent_unlink(tmp)
            raise
        _silent_unlink(part_path)
    try:
        os.chmod(str(dest_path), 0o644)
    except OSError:
        pass  # 尽力而为：某些文件系统不支持


def _stream_to_part(
    client: AgentClient,
    asset_id: int,
    part_path: Path,
    local_have: int,
    total_size: int,
    on_progress: Optional[ProgressCallback],
    deadline: Optional[float],
) -> int:
    """把内容写到分片文件，返回本次写入字节数。出错直接抛，由调用方丢连接。"""
    offset = max(0, local_have)
    response = None

    if offset > 0:
        status, _headers, response = client.open_download(asset_id, offset)
        if status != 206:
            # 服务端忽略了 Range（或代理剥离了它）—— 只能从头来
            log.info("服务端未响应 Range 请求，asset %d 从头下载", asset_id)
            client.drop_transfer_connection()
            offset = 0
            response = None

    if response is None:
        offset = 0
        _status, _headers, response = client.open_download(asset_id, 0)

    written = 0
    mode = "ab" if offset > 0 else "wb"
    with open(part_path, mode) as handle:
        if offset > 0:
            handle.seek(offset)
        try:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    client.drop_transfer_connection()
                    raise _DeadlineReached()
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress(offset + written, total_size)
        finally:
            # 已经写下的部分一定落盘：它是下一轮续传的起点
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

    return written


def _is_disk_full(exc: OSError) -> bool:
    return getattr(exc, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))


def _is_network(exc: BaseException) -> bool:
    import socket

    return isinstance(exc, (ConnectionError, socket.timeout))


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def sweep_stale_parts(deploy_root: Path, max_age_seconds: int = 7 * 86400) -> int:
    """清理长期未完成的分片。返回清理数量。启动时跑一次。

    只看 :data:`PARTS_DIRNAME` 一个目录；顺带把老版本散在目标文件旁边的
    ``*.syncoj-part`` 一并清掉（那一种要遍历整个下发根，所以只在启动时做）。
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    parts = Path(deploy_root) / PARTS_DIRNAME
    candidates = []
    try:
        candidates.extend(parts.glob("*" + PART_SUFFIX))
    except OSError:
        pass
    try:
        candidates.extend(
            p for p in Path(deploy_root).rglob("*" + LEGACY_PART_SUFFIX)
        )
    except OSError:
        pass
    for path in candidates:
        try:
            legacy = path.name.endswith(LEGACY_PART_SUFFIX)
            if legacy or path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
