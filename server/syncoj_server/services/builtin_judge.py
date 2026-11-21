"""自带评测的任务队列（§12）：排任务、领任务（租约）、选代码、写 ``result.json``。

worker 经 HTTP 来领（``api/judge.py``），与 Agent 同一种形状：服务端持有全部状态，worker
碰不到数据库、私钥与 ``source/`` 目录本身，只拿得到"这个任务要用的两份内容"。

成绩**不直接写表**：写进 ``judge_result/<场次>/<选手>/<题目>/result.json``，由现有的扫描器
收进矩阵。手工录入的成绩照旧不会被覆盖，外部评测器与自带评测可以在同一场里混用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete, select

from ..judging.runner import LANGUAGES
from ..models import Contest, JudgeTask, Player, Problem, SourceFile, utcnow
from ..paths import safe_join
from . import matching, problem_rules

LEASE_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
DEFAULT_TIME_LIMIT_MS = 1000
DEFAULT_MEMORY_LIMIT_MB = 256
RESULT_FILE_NAME = "result.json"
MAX_RESULT_BYTES = 2 * 1024 * 1024

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


def problem_config(row: Problem) -> Dict[str, Any]:
    return {
        "ident": row.ident,
        "time_limit_ms": int(row.time_limit_ms or DEFAULT_TIME_LIMIT_MS),
        "memory_limit_mb": int(row.memory_limit_mb or DEFAULT_MEMORY_LIMIT_MB),
        "io_mode": row.io_mode or "stdio",
        "compile_flags": row.compile_flags or None,
    }


def is_builtin(row: Problem) -> bool:
    return (row.judge_mode or "external") == "builtin"


def contest_in_progress(contest: Contest, now: datetime) -> bool:
    """还在考：状态是进行中 / 封榜，或者结束时间还没到。此时评测拿到的是考到一半的代码。"""
    if contest.is_active:
        return True
    return contest.ends_at is not None and now < contest.ends_at


def enqueue(session, contest_id: int, idents: Iterable[str], player_ids: Iterable[int]) -> int:
    """为 (选手 × 题目) 排任务。同一对已有的任务（不管什么状态）先删掉 —— 重测就是重新排。

    正在跑的那条被删掉后，worker 交结果时会 404、结果被丢弃：代码可能已经变了。
    """
    idents = list(idents)
    player_ids = list(player_ids)
    if not idents or not player_ids:
        return 0
    session.execute(
        delete(JudgeTask).where(
            JudgeTask.contest_id == contest_id,
            JudgeTask.problem.in_(idents),
            JudgeTask.player_id.in_(player_ids),
        )
    )
    count = 0
    for player_id in player_ids:
        for ident in idents:
            session.add(JudgeTask(contest_id=contest_id, player_id=player_id, problem=ident, status=QUEUED))
            count += 1
    return count


def pick_source(
    session, player_id: int, ident: str, rules: List[matching.ProblemRule]
) -> Tuple[Optional[SourceFile], Optional[str]]:
    """选出这位选手这道题要评的那份代码。返回 ``(行, 错误)``：都为 None = 没交。

    只看**还在机器上**（没被扫描对账标成已消失）且**内容已存下**的文件，按题目的认领模式
    归题，再按语言后缀过滤。多份候选时优先文件名与题目同名的；还分不出来就如实报错，
    不猜 —— 评错一份代码比"请教师看一眼"代价大得多。
    """
    rows = session.execute(
        select(SourceFile).where(
            SourceFile.player_id == player_id,
            SourceFile.deleted_at.is_(None),
            SourceFile.content_stored.is_(True),
        )
    ).scalars().all()
    candidates = [
        row for row in rows
        if matching.match_problem(row.rel_path, rules) == ident
        and PurePosixPath(row.rel_path).suffix.lower() in LANGUAGES
    ]
    if len(candidates) > 1:
        named = [row for row in candidates if PurePosixPath(row.rel_path).stem == ident]
        candidates = named or candidates
    if not candidates:
        return None, None
    if len(candidates) > 1:
        return None, "有多份可评测的源文件，无法确定评哪一份：%s" % ", ".join(
            sorted(row.rel_path for row in candidates)[:5]
        )
    return candidates[0], None


@dataclass
class Lease:
    task: JudgeTask
    contest: Contest
    player: Player
    problem: Problem

    def to_payload(self) -> Dict[str, Any]:
        return {
            "id": self.task.id,
            "contest_slug": self.contest.slug,
            "player_no": self.player.player_no,
            "problem": problem_config(self.problem),
            "source": {"path": self.task.source_path, "sha256": self.task.source_sha256},
            "testdata": {"sha256": self.problem.testdata_sha256, "name": self.problem.testdata_name},
            "attempt": self.task.attempt,
        }


def lease_next(ctx, worker_id: int, now: Optional[datetime] = None) -> Optional[Lease]:
    """领下一个任务。没交代码的、题目配置不对的就地了结，接着找下一个。"""
    now = now or utcnow()
    with ctx.db.session() as session:
        while True:
            task = session.execute(
                select(JudgeTask)
                .where(
                    (JudgeTask.status == QUEUED)
                    | ((JudgeTask.status == RUNNING) & (JudgeTask.lease_until < now))
                )
                .order_by(JudgeTask.id)
                .limit(1)
            ).scalar_one_or_none()
            if task is None:
                return None
            task.attempt = int(task.attempt or 0) + 1
            if task.attempt > MAX_ATTEMPTS:
                _finish(task, FAILED, now, error=task.error or "评测机连续 %d 次没有交回结果" % MAX_ATTEMPTS)
                session.flush()
                continue
            contest = session.get(Contest, task.contest_id)
            player = session.get(Player, task.player_id)
            problem = session.execute(
                select(Problem).where(Problem.contest_id == task.contest_id, Problem.ident == task.problem)
            ).scalar_one_or_none()
            if contest is None or player is None or problem is None or not is_builtin(problem):
                _finish(task, FAILED, now, error="题目已删除或不再使用自带评测")
                session.flush()
                continue
            if not problem.testdata_sha256 or not ctx.blobs.has(problem.testdata_sha256):
                _finish(task, FAILED, now, error="题目没有数据包")
                session.flush()
                continue
            rules = problem_rules.rules_for(session, task.contest_id, ctx.settings)
            source, error = pick_source(session, task.player_id, task.problem, rules)
            if error:
                _finish(task, FAILED, now, error=error)
                session.flush()
                continue
            if source is None or not ctx.blobs.has(source.sha256):
                # 没交：不写 result.json，矩阵照旧显示"未交"
                _finish(task, DONE, now, outcome="missing")
                session.flush()
                continue
            task.status = RUNNING
            task.worker_id = worker_id
            task.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            task.source_path = source.rel_path
            task.source_sha256 = source.sha256
            session.flush()
            session.expunge_all()
            return Lease(task, contest, player, problem)


def _finish(task: JudgeTask, status: str, now: datetime, *, outcome: Optional[str] = None,
            error: Optional[str] = None) -> None:
    task.status = status
    task.finished_at = now
    task.lease_until = None
    task.outcome = outcome
    task.error = (error or "")[:2000] or None


def leased_task(session, task_id: int, worker_id: int) -> Optional[JudgeTask]:
    task = session.get(JudgeTask, task_id)
    if task is None or task.status != RUNNING or task.worker_id != worker_id:
        return None
    return task


def result_path(ctx, contest_slug: str, player_no: str, ident: str) -> Path:
    return safe_join(ctx.settings.judge_result_root, "/".join((contest_slug, player_no, ident, RESULT_FILE_NAME)))


def write_result(path: Path, document: Dict[str, Any]) -> None:
    """原子写 ``result.json``（临时名 + rename），扫描器不会读到半截。"""
    raw = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
    if len(raw) > MAX_RESULT_BYTES:
        document = dict(document, cases=[], truncated=True)
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_bytes(raw)
    os.replace(str(tmp), str(path))


def complete(ctx, task_id: int, worker_id: int, document: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    with ctx.db.session() as session:
        task = leased_task(session, task_id, worker_id)
        if task is None:
            return False
        contest = session.get(Contest, task.contest_id)
        player = session.get(Player, task.player_id)
        document = dict(document)
        document["source_path"] = task.source_path
        document["source_sha256"] = task.source_sha256
        document["judged_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        write_result(result_path(ctx, contest.slug, player.player_no, task.problem), document)
        _finish(task, DONE, now, outcome=str(document.get("status") or "")[:16])
    return True


def fail(ctx, task_id: int, worker_id: int, message: str, now: Optional[datetime] = None) -> bool:
    """worker 自己出错（沙箱起不来之类，SE）：退回队列，次数用完就记失败。"""
    now = now or utcnow()
    with ctx.db.session() as session:
        task = leased_task(session, task_id, worker_id)
        if task is None:
            return False
        task.error = (message or "评测机出错")[:2000]
        if int(task.attempt or 0) >= MAX_ATTEMPTS:
            _finish(task, FAILED, now, error=task.error)
        else:
            task.status = QUEUED
            task.worker_id = None
            task.lease_until = None
    return True


def progress(session, contest_id: int) -> Dict[str, Any]:
    rows = session.execute(
        select(JudgeTask, Player.player_no)
        .join(Player, JudgeTask.player_id == Player.id)
        .where(JudgeTask.contest_id == contest_id)
    ).all()
    counts = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0}
    failures = []
    for task, player_no in rows:
        counts[task.status] = counts.get(task.status, 0) + 1
        if task.status == FAILED:
            failures.append({"player_no": player_no, "problem": task.problem, "error": task.error or ""})
    failures.sort(key=lambda item: (item["player_no"], item["problem"]))
    return {"total": len(rows), **counts, "failures": failures[:50]}
