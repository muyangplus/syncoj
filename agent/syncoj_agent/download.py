"""文件下发：断点续传 + 校验 + 原子落盘。

三个不显眼但很关键的决定
------------------------
1. **分片文件放在目标目录里**，而不是 state_dir。因为 ``os.replace`` 只在同一
   文件系统内是原子的；跨文件系统会直接失败（EXDEV）。把 ``.part`` 放在
   ``dest`` 旁边，才能保证"要么是完整的旧文件，要么是完整的新文件"，评测器
   永远不会读到写了一半的测试数据。
2. **续传位置以本地磁盘实际大小为准**，而不是服务端给的 offset。服务端只知道
   我们上次报告了多少，本地文件才是事实。
3. **不主动推送进度**。下载进度由下一轮 tick 的 ``partials`` 字段自然带到服务端。
   少一条上报通道就少一类竞态。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .client import AgentClient, AgentError
from .safepath import PathError, resolve_within
from .state import sha256_file

__all__ = ["DownloadOutcome", "download_asset", "PART_SUFFIX", "find_partial_sizes"]

log = logging.getLogger(__name__)

_CHUNK = 256 * 1024
PART_SUFFIX = ".syncoj-part"

ProgressCallback = Callable[[int, int], None]


@dataclass
class DownloadOutcome:
    asset_id: int
    dest: str
    #: "ok" | "skipped" | "failed"
    status: str
    bytes_written: int = 0
    total_bytes: int = 0
    error: Optional[str] = None


def part_path_for(dest_path: Path, asset_id: int) -> Path:
    """分片文件路径。

    与目标同目录（保证 ``os.replace`` 同文件系统原子），文件名里编入 asset_id，
    这样无需依赖服务端下发的作业列表就能反查出"哪些 asset 有未完成分片" ——
    Agent 重启后照样能续上。
    """
    return dest_path.parent / (".%d.%s%s" % (asset_id, dest_path.name, PART_SUFFIX))


def find_partial_sizes(deploy_root: Path) -> Dict[int, int]:
    """扫描下发目录，返回 ``{asset_id: 已下载字节数}``。

    这个结果会作为下一轮 tick 的 ``partials`` 上报，服务端据此把 ``offset``
    填进下发作业里。进度是客户端的本地事实，服务端只做记录。
    """
    sizes: Dict[int, int] = {}
    if not deploy_root.is_dir():
        return sizes
    try:
        candidates = list(deploy_root.rglob("*" + PART_SUFFIX))
    except OSError:
        return sizes

    for path in candidates:
        asset_id = _parse_asset_id(path.name)
        if asset_id is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        # 同一 asset 若存在多个分片（改了 dest），取最大的那个 —— 它最有可能是
        # 真正在续传的那份
        if size > sizes.get(asset_id, -1):
            sizes[asset_id] = size
    return sizes


def _parse_asset_id(filename: str) -> Optional[int]:
    """从 ``.123.main.cpp.syncoj-part`` 解出 asset_id。"""
    if not filename.startswith(".") or not filename.endswith(PART_SUFFIX):
        return None
    body = filename[1 : len(filename) - len(PART_SUFFIX)]
    head = body.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def download_asset(
    client: AgentClient,
    job: dict,
    deploy_root: Path,
    on_progress: Optional[ProgressCallback] = None,
) -> DownloadOutcome:
    """执行一个下发作业。不抛异常 —— 所有失败都转成 ``status="failed"``。"""
    try:
        asset_id = int(job["asset_id"])
        dest_rel = str(job["dest"])
        expected_sha = str(job["sha256"])
        total_size = int(job.get("size") or 0)
        mode = str(job.get("mode") or "overwrite")
    except (KeyError, TypeError, ValueError) as exc:
        return DownloadOutcome(
            asset_id=int(job.get("asset_id", 0) or 0),
            dest=str(job.get("dest", "")),
            status="failed",
            error="下发作业字段不合法: %s" % exc,
        )

    # 服务端给的路径同样要过 Agent 的独立校验 —— 服务端也可能被攻破
    try:
        dest_path = resolve_within(deploy_root, dest_rel)
    except PathError as exc:
        log.error("拒绝不安全的下发路径 %r: %s", dest_rel, exc)
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            error="目标路径不合法: %s" % exc,
        )

    if len(expected_sha) != 64:
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed", error="sha256 格式不合法"
        )

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            error="无法创建目标目录: %s" % exc,
        )

    # 已存在且内容正确 -> 跳过（skip_exist 模式；overwrite 模式下内容相同也没必要重下）
    if dest_path.is_file():
        try:
            if sha256_file(dest_path) == expected_sha:
                return DownloadOutcome(
                    asset_id=asset_id, dest=dest_rel, status="skipped",
                    bytes_written=0, total_bytes=total_size,
                )
        except OSError:
            pass
        if mode == "skip_exist":
            # 内容不同但策略是"存在就不动"
            log.info("%s 已存在且模式为 skip_exist，保留原文件", dest_rel)
            return DownloadOutcome(
                asset_id=asset_id, dest=dest_rel, status="skipped",
                bytes_written=0, total_bytes=total_size,
            )

    part_path = part_path_for(dest_path, asset_id)
    local_have = 0
    try:
        if part_path.is_file():
            local_have = part_path.stat().st_size
    except OSError:
        local_have = 0

    # 本地分片比服务端声称的总大小还大 —— 说明它已经损坏或属于另一版本
    if total_size and local_have > total_size:
        _silent_unlink(part_path)
        local_have = 0

    written = 0
    try:
        written = _stream_to_part(client, asset_id, part_path, local_have, total_size, on_progress)
    except AgentError as exc:
        # 网络中断是常态：保留分片，下一轮从断点继续
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=local_have, total_bytes=total_size, error=str(exc),
        )
    except OSError as exc:
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=local_have, total_bytes=total_size,
            error="写盘失败: %s" % exc,
        )

    # 完整性校验：必须校验**整个**文件，不能只校验新下的尾部 ——
    # 断点续传的前提是本地分片本身可信，而它可能来自上一次崩溃。
    try:
        actual_size = part_path.stat().st_size
    except OSError as exc:
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=written, total_bytes=total_size,
            error="校验时读取失败: %s" % exc,
        )

    if total_size and actual_size != total_size:
        if actual_size < total_size:
            # 传完了却没到预期长度 = 连接中断或服务端提前收尾。
            # **必须保留分片**：删掉的话下一轮又从头下，弱网环境下大文件将永远
            # 传不完 —— 每轮都在同一个位置重新开始，永远走不到终点。
            return DownloadOutcome(
                asset_id=asset_id, dest=dest_rel, status="failed",
                bytes_written=written, total_bytes=total_size,
                error="传输未完成：已收到 %d/%d 字节，下轮从此处续传"
                % (actual_size, total_size),
            )
        # 比预期还大 = 分片损坏或属于另一个版本，不可续传
        _silent_unlink(part_path)
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=written, total_bytes=total_size,
            error="分片大小 %d 超出预期 %d，已丢弃" % (actual_size, total_size),
        )

    try:
        actual_sha = sha256_file(part_path)
    except OSError as exc:
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=written, total_bytes=total_size,
            error="计算校验和失败: %s" % exc,
        )

    if actual_sha != expected_sha:
        _silent_unlink(part_path)
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=written, total_bytes=total_size,
            error="sha256 不符：期望 %s，实际 %s（已丢弃分片，下轮重下）"
            % (expected_sha[:12], actual_sha[:12]),
        )

    try:
        os.replace(part_path, dest_path)
    except OSError as exc:
        return DownloadOutcome(
            asset_id=asset_id, dest=dest_rel, status="failed",
            bytes_written=written, total_bytes=total_size,
            error="重命名落盘失败: %s" % exc,
        )

    try:
        os.chmod(dest_path, 0o644)
    except OSError:
        pass  # 尽力而为：某些文件系统不支持

    return DownloadOutcome(
        asset_id=asset_id, dest=dest_rel, status="ok",
        bytes_written=written, total_bytes=actual_size,
    )


def _stream_to_part(
    client: AgentClient,
    asset_id: int,
    part_path: Path,
    local_have: int,
    total_size: int,
    on_progress: Optional[ProgressCallback],
) -> int:
    """把内容写到分片文件，返回本次写入字节数。"""
    offset = max(0, local_have)
    response = None

    if offset > 0:
        status, _headers, response = client.open_download(asset_id, offset)
        if status != 206:
            # 服务端忽略了 Range（或代理剥离了它）—— 只能从头来
            log.info("服务端未响应 Range 请求，asset %d 从头下载", asset_id)
            _drain(response)
            client.drop_transfer_connection()
            offset = 0
            response = None

    if response is None:
        offset = 0
        _status, _headers, response = client.open_download(asset_id, 0)

    written = 0
    mode = "ab" if offset > 0 else "wb"
    try:
        with open(part_path, mode) as handle:
            if offset > 0:
                handle.seek(offset)
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress(offset + written, total_size)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        _drain(response)

    return written


def _drain(response) -> None:
    """把响应体读完，让连接可以回到 keep-alive 池；读失败就丢弃连接。"""
    if response is None:
        return
    try:
        while response.read(_CHUNK):
            pass
    except Exception:
        pass


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def sweep_stale_parts(deploy_root: Path, max_age_seconds: int = 7 * 86400) -> int:
    """清理长期未完成的分片残留。返回清理数量。"""
    import time

    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        candidates = list(deploy_root.rglob("*" + PART_SUFFIX))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
