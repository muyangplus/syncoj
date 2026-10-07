"""Agent 扫描结果的对账（reconciliation）。

这是整个系统最核心的一段逻辑：把 Agent 上报的**全量文件索引**与服务端台账比对，
算出「需要上传什么」和「什么不见了」。

为什么是全量比对而不是增量事件
------------------------------
Agent 上报的是"当前这一时刻目录里有什么"，而不是"刚刚发生了什么"。这带来三个
性质，都是白拿的：

1. **删除检测免费**。上一轮台账里有、这一轮扫描里没有 = 被删了。不需要客户端
   主动上报删除事件（那种事件在有崩溃、有快照还原的环境里必然丢）。
2. **去重免费**。同一轮里重复上报的路径直接忽略。
3. **可重放**。Agent 重启、断网补传、重复 tick，都不会产生错误状态 —— 对账是
   幂等的，比对的是状态而非操作序列。

写入量控制
----------
50 个文件 × 每 20s 一次 tick，如果每次都刷新 ``last_seen_at``，就是每秒 2.5 次
无意义的 UPDATE。所以 ``last_seen_at`` 只在距上次刷新超过
``LAST_SEEN_REFRESH_SECONDS`` 时才写。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import EventLog, SourceFile, utcnow
from ..paths import PathValidationError, validate_relpath
from . import matching

__all__ = [
    "CollectResult",
    "reconcile_scan",
    "LAST_SEEN_REFRESH_SECONDS",
    "MAX_SKIPPED_SAMPLE",
]

log = logging.getLogger(__name__)

#: ``last_seen_at`` 的最小刷新间隔，避免每轮 tick 都产生无意义的 UPDATE
LAST_SEEN_REFRESH_SECONDS = 300

#: 事件与页面上最多列几个"被题目预设挡掉"的路径。**计数是完整的，只有样本截断** ——
#: 一台机器上被挡掉几百个文件是可能的（整个目录都不符合预设），而一条审计里塞几百个
#: 路径既没人看，又把那一行撑大。
MAX_SKIPPED_SAMPLE = 20


@dataclass
class CollectResult:
    #: 需要 Agent 上传内容的相对路径
    need_upload: List[str] = field(default_factory=list)
    #: 本轮发现消失的文件
    deleted: List[str] = field(default_factory=list)
    #: 重新出现的文件（曾删除，现已恢复）
    restored: List[str] = field(default_factory=list)
    #: 被拒绝的条目 (path, reason)
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    new_files: int = 0
    changed_files: int = 0
    seen_files: int = 0
    #: 本轮被"题目预设"挡掉的文件数（不匹配任何题目模式）。完整计数。
    skipped_unmatched: int = 0
    #: 上面那些路径里的一小段样本，供审计与页面显示。
    skipped_sample: List[str] = field(default_factory=list)
    #: 待写入的审计事件 (level, category, message, meta)
    events: List[Tuple[str, str, str, dict]] = field(default_factory=list)


def reconcile_scan(
    session: Session,
    player_id: int,
    entries: Sequence[object],
    settings: Settings,
    scan_complete: bool = True,
    rules: Optional[Sequence[matching.ProblemRule]] = None,
) -> CollectResult:
    """对账一次扫描结果。

    ``entries`` 是已经过 pydantic 校验的 ``ScanEntry`` 序列（有 path/sha256/size/mtime
    四个属性）。这里仍然要再校验一次路径 —— pydantic 只保证类型和长度，不保证
    路径语义合法。

    ``scan_complete=False`` 时**跳过删除判定**。这是必须的：Agent 递归扫描时
    完全可能因为某个子目录权限不足而漏掉一批文件，若照常判定，这批文件会被
    错误地标成"选手删除了"，污染审计日志。

    ``rules`` 是这一场次的题目模式（``services.problem_rules``）。给了它就**只收**
    匹配得上某个题目的文件：不匹配的既不进台账、也不要求上传。两个刻意的决定：

    * ``rules`` 为空表示"这一场还没配任何题目"，此时**一律照收** —— 教师还没建题目
      就什么都收不上来，是最糟的默认值；严格只在"有预设可依"时才严格。
    * 被挡掉的路径仍然算进 ``seen``：教师改完模式之后，那些老条目不该被当成
      "选手删除了文件"，只有真的从磁盘上消失才算删除。
    """
    result = CollectResult()
    now = utcnow()

    if len(entries) > settings.max_files_per_scan:
        result.rejected.append(
            ("<scan>", "文件数 %d 超过上限 %d" % (len(entries), settings.max_files_per_scan))
        )
        result.events.append(
            (
                "warning",
                "scan_too_large",
                "扫描结果被截断：%d 个文件超过上限 %d" % (len(entries), settings.max_files_per_scan),
                {"count": len(entries), "limit": settings.max_files_per_scan},
            )
        )
        entries = entries[: settings.max_files_per_scan]

    # 一次性读出该选手的全部台账，避免逐文件查库（N+1）
    rows: List[SourceFile] = list(
        session.execute(select(SourceFile).where(SourceFile.player_id == player_id)).scalars()
    )
    by_path: Dict[str, SourceFile] = {row.rel_path: row for row in rows}

    seen: set = set()
    for entry in entries:
        raw_path = getattr(entry, "path", None)
        sha256 = getattr(entry, "sha256", None)
        try:
            path = validate_relpath(raw_path, max_length=settings.max_relpath_length)
        except PathValidationError as exc:
            result.rejected.append((str(raw_path), str(exc)))
            continue

        if path in seen:
            continue
        seen.add(path)

        # 「严格按题目预设回收」：不属于任何题目的文件不进台账、不要求上传。
        # 放在 sha256 校验**之前** —— 一个本来就不该收的文件，它的 sha256 合不合法
        # 都不该出现在"被拒绝的条目"里（那会让教师以为需要处理它）。
        if rules and matching.match_problem(path, rules) is None:
            result.skipped_unmatched += 1
            if len(result.skipped_sample) < MAX_SKIPPED_SAMPLE:
                result.skipped_sample.append(path)
            continue

        if not isinstance(sha256, str) or len(sha256) != 64:
            result.rejected.append((path, "sha256 不合法"))
            continue

        row = by_path.get(path)
        size = int(getattr(entry, "size", 0) or 0)
        mtime = int(getattr(entry, "mtime", 0) or 0)

        if row is None:
            row = SourceFile(
                player_id=player_id,
                rel_path=path,
                sha256=sha256,
                size=size,
                mtime=mtime,
                revision=0,
                content_stored=False,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(row)
            by_path[path] = row
            result.need_upload.append(path)
            result.new_files += 1
            continue

        # 曾经消失、现在又回来了
        if row.deleted_at is not None:
            row.deleted_at = None
            result.restored.append(path)
            result.events.append(
                (
                    "info",
                    "file_restored",
                    "文件重新出现：%s" % path,
                    {"path": path},
                )
            )

        content_changed = row.sha256 != sha256
        if content_changed or not row.content_stored:
            # 注意：revision 在**上传成功**时才递增，这里不动。
            # 否则"扫描发现变了但上传失败"会白白消耗版本号。
            result.need_upload.append(path)
            if content_changed:
                result.changed_files += 1

        row.sha256 = sha256
        row.size = size
        row.mtime = mtime
        if (now - row.last_seen_at).total_seconds() >= LAST_SEEN_REFRESH_SECONDS:
            row.last_seen_at = now
        result.seen_files += 1

    # 本轮扫描里没有出现的文件 = 被删除或改名。
    # 只在扫描完整时才敢下这个判断，理由见函数 docstring。
    if scan_complete:
        for path, row in by_path.items():
            if path in seen or row.deleted_at is not None:
                continue
            row.deleted_at = now
            result.deleted.append(path)

        if result.deleted:
            result.events.append(
                (
                    "warning",
                    "file_deleted",
                    "选手代码目录中 %d 个文件消失" % len(result.deleted),
                    {"paths": result.deleted[:50], "total": len(result.deleted)},
                )
            )
    else:
        result.events.append(
            (
                "warning",
                "scan_incomplete",
                "本轮扫描不完整，已跳过删除判定（避免把漏扫误判为删除）",
                {"scanned": len(seen)},
            )
        )

    if result.rejected:
        result.events.append(
            (
                "warning",
                "scan_rejected",
                "扫描结果中有 %d 条被拒绝" % len(result.rejected),
                {"items": [{"path": p, "reason": r} for p, r in result.rejected[:20]]},
            )
        )

    # 大批量重写检测：短时间内大量文件内容变化，是"整体替换"的典型特征
    if result.changed_files >= 20:
        result.events.append(
            (
                "warning",
                "bulk_rewrite",
                "本轮有 %d 个文件内容变化，疑似整体重写" % result.changed_files,
                {"changed": result.changed_files, "seen": result.seen_files},
            )
        )

    session.flush()
    return result


def record_events(session: Session, player_id: int, contest_id: int, agent_id: int,
                  events: Sequence[Tuple[str, str, str, dict]]) -> None:
    """把对账产生的事件写入审计日志。"""
    import json

    for level, category, message, meta in events:
        session.add(
            EventLog(
                level=level,
                category=category,
                contest_id=contest_id,
                player_id=player_id,
                agent_id=agent_id,
                message=message,
                meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
            )
        )
