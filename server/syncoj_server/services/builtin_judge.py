"""自带评测的任务队列（§12）：排任务、领任务（租约）、选代码、写 ``result.json``。

worker 经 HTTP 来领（``api/judge.py``），与 Agent 同一种形状：服务端持有全部状态，worker
碰不到数据库、私钥与 ``source/`` 目录本身，只拿得到"这个任务要用的两份内容"。

成绩**不直接写表**：写进 ``judge_result/<场次>/<选手>/<题目>/result.json``，由现有的扫描器
收进矩阵。手工录入的成绩照旧不会被覆盖，外部评测器与自带评测可以在同一场里混用。

按版本记（§12.9）：任务即历史，每个任务记下评的是哪一版代码（``source_version``）与结果；
同一 (数据包, 评测配置, 代码) 评过就直接抄结果、不占评测机；场次开了自动入队时，收到某题的
新版本就排一个带去抖的任务。评历史版本（``rejudge``）的结果只进历史，教师「采用」才写
``result.json``。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete, func, select

from ..judging.runner import LANGUAGES
from ..models import Contest, JudgeTask, Player, Problem, SourceFile, SourceVersion, utcnow
from ..paths import safe_join
from . import matching, problem_rules

LEASE_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
DEFAULT_TIME_LIMIT_MS = 1000
DEFAULT_MEMORY_LIMIT_MB = 256
RESULT_FILE_NAME = "result.json"
MAX_RESULT_BYTES = 2 * 1024 * 1024

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

#: 触发方式：收到新版本自动排 / 教师排（评最新版）/ 教师指定历史版本重测
AUTO, MANUAL, REJUDGE = "auto", "manual", "rejudge"
TRIGGERS = (AUTO, MANUAL, REJUDGE)

#: 场次的自动入队开关
AUTO_OFF, AUTO_ON_CHANGE = "off", "on_change"
DEFAULT_AUTO_DELAY = 60
MAX_AUTO_DELAY = 3600

#: 不复用的结果：评测系统自己的错（SE）不该被抄给下一次
_NOT_REUSABLE = ("SE",)


def problem_config(row: Problem) -> Dict[str, Any]:
    return {
        "ident": row.ident,
        "time_limit_ms": int(row.time_limit_ms or DEFAULT_TIME_LIMIT_MS),
        "memory_limit_mb": int(row.memory_limit_mb or DEFAULT_MEMORY_LIMIT_MB),
        "io_mode": row.io_mode or "stdio",
        "input_file": row.input_file or None,
        "output_file": row.output_file or None,
        "compile_flags": row.compile_flags or None,
    }


def judge_key(problem: Problem, source_sha256: str) -> str:
    """(数据包 sha, 评测配置, 代码 sha) 的指纹：三者都相同的两次评测，结果没有理由不同。"""
    raw = json.dumps(
        {"config": problem_config(problem), "testdata": problem.testdata_sha256, "source": source_sha256},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_builtin(row: Problem) -> bool:
    return (row.judge_mode or "external") == "builtin"


def trigger_of(task: JudgeTask) -> str:
    return task.trigger or MANUAL


def is_pinned(task: JudgeTask) -> bool:
    """钉死了版本的任务（评历史版本）：结果只进历史，不自动写 ``result.json``。"""
    return trigger_of(task) == REJUDGE


def auto_mode(contest: Contest) -> str:
    return contest.auto_judge or AUTO_OFF


def auto_delay(contest: Contest) -> int:
    value = contest.auto_judge_delay
    return DEFAULT_AUTO_DELAY if value is None else max(0, min(int(value), MAX_AUTO_DELAY))


def contest_in_progress(contest: Contest, now: datetime) -> bool:
    """还在考：状态是进行中 / 封榜，或者结束时间还没到。此时评测拿到的是考到一半的代码。"""
    if contest.is_active:
        return True
    return contest.ends_at is not None and now < contest.ends_at


def _latest_target():
    """评最新版的任务（auto / manual，含迁移前 trigger 为空的老任务）。"""
    return JudgeTask.trigger.is_(None) | (JudgeTask.trigger != REJUDGE)


def enqueue(
    session,
    contest_id: int,
    idents: Iterable[str],
    player_ids: Iterable[int],
    *,
    trigger: str = MANUAL,
    requested_by: Optional[str] = None,
    rerun: bool = False,
) -> int:
    """为 (选手 × 题目) 排"评最新版"的任务，代码在领任务时才选定。

    同一对**还在排队**的最新版任务先删掉（它们领到时选的也是同一份最新代码，留着只是重复）；
    正在跑的、已完成的留着 —— 任务即历史。正在跑的那条交回结果时，若已有更新版本的结果写进了
    ``result.json``，它只进历史（见 :func:`_should_apply`）。
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
            JudgeTask.status == QUEUED,
            _latest_target(),
        ).execution_options(synchronize_session=False)
    )
    count = 0
    for player_id in player_ids:
        for ident in idents:
            session.add(JudgeTask(
                contest_id=contest_id, player_id=player_id, problem=ident, status=QUEUED,
                trigger=trigger, requested_by=requested_by, rerun=bool(rerun),
            ))
            count += 1
    return count


def enqueue_version(
    session,
    contest_id: int,
    version: SourceVersion,
    ident: str,
    *,
    requested_by: Optional[str] = None,
    rerun: bool = False,
) -> JudgeTask:
    """重测某个历史版本（``rejudge``）：版本在排任务时钉死，结果只进历史。"""
    session.execute(
        delete(JudgeTask).where(
            JudgeTask.source_version_id == version.id,
            JudgeTask.problem == ident,
            JudgeTask.status == QUEUED,
            JudgeTask.trigger == REJUDGE,
        ).execution_options(synchronize_session=False)
    )
    task = JudgeTask(
        contest_id=contest_id, player_id=version.player_id, problem=ident, status=QUEUED,
        trigger=REJUDGE, requested_by=requested_by, rerun=bool(rerun),
        source_version_id=version.id, source_path=version.rel_path, source_sha256=version.sha256,
    )
    session.add(task)
    session.flush()
    return task


def classify(session, contest_id: int, rel_paths: Iterable[str], settings) -> Dict[str, Optional[str]]:
    """按**当前**题目模式给一批路径归题。"""
    rules = problem_rules.rules_for(session, contest_id, settings)
    return {path: (matching.match_problem(path, rules) or None) for path in set(rel_paths)}


def judgeable_ident(session, contest_id: int, rel_path: str, settings) -> Optional[str]:
    """这个路径归到的、能自带评测的题（自带评测、有数据包、语言后缀认得）；否则 None。"""
    if PurePosixPath(rel_path).suffix.lower() not in LANGUAGES:
        return None
    ident = classify(session, contest_id, [rel_path], settings)[rel_path]
    if not ident:
        return None
    problem = session.execute(
        select(Problem).where(Problem.contest_id == contest_id, Problem.ident == ident)
    ).scalar_one_or_none()
    if problem is None or not is_builtin(problem) or not problem.testdata_sha256:
        return None
    return ident


def record_version(
    session, contest_id: int, player_id: int, rel_path: str, sha256: str, size: int, now: datetime, settings
) -> SourceVersion:
    """Agent 传上来一份新内容：记一条版本。归题出错不影响收代码，题目留空。"""
    try:
        ident = classify(session, contest_id, [rel_path], settings)[rel_path]
    except Exception:  # pragma: no cover - 归题出错不该挡住收代码
        ident = None
    version = SourceVersion(
        player_id=player_id, rel_path=rel_path, sha256=sha256, size=int(size or 0),
        received_at=now, problem=ident,
    )
    session.add(version)
    session.flush()
    return version


def auto_enqueue(session, contest: Contest, player_id: int, rel_path: str, now: datetime, settings) -> Optional[str]:
    """场次开了自动入队时，为收到新版本的 (选手, 题目) 排一个带去抖的任务；返回题目标识。

    去抖：任务带 ``not_before``，到点前不发给评测机。同一对已有排队中的自动任务时只把它往后推，
    不新增 —— 领任务时才选代码，所以评的总是最后一版。已有排队中的手动任务时不动它（它领到时
    同样评最新版）。场次进行中也排：这是自动入队的意义，与手动排任务的 409 不同。
    """
    if auto_mode(contest) != AUTO_ON_CHANGE:
        return None
    ident = judgeable_ident(session, contest.id, rel_path, settings)
    if ident is None:
        return None
    due = now + timedelta(seconds=auto_delay(contest))
    waiting = session.execute(
        select(JudgeTask).where(
            JudgeTask.contest_id == contest.id,
            JudgeTask.player_id == player_id,
            JudgeTask.problem == ident,
            JudgeTask.status == QUEUED,
            _latest_target(),
        ).order_by(JudgeTask.id.desc())
    ).scalars().first()
    if waiting is not None:
        if trigger_of(waiting) == AUTO:
            waiting.not_before = due
        return ident
    session.add(JudgeTask(
        contest_id=contest.id, player_id=player_id, problem=ident, status=QUEUED,
        trigger=AUTO, not_before=due,
    ))
    return ident


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


#: 领任务串行化：几台评测机同时来领时，"先查后改"会在 SQLite 上撞旧快照（database is
#: locked）或把同一个任务发给两台。服务端是单进程，进程内一把锁就够。
_LEASE_LOCK = threading.Lock()


def lease_next(ctx, worker_id: int, now: Optional[datetime] = None) -> Optional[Lease]:
    """领下一个任务。没交代码的、题目配置不对的就地了结，接着找下一个。"""
    with _LEASE_LOCK:
        return _lease_next(ctx, worker_id, now or utcnow())


def version_for(session, source: SourceFile) -> SourceVersion:
    """台账当前版本对应的那条版本记录；没有（极老的数据）就补一条。"""
    version = session.execute(
        select(SourceVersion)
        .where(
            SourceVersion.player_id == source.player_id,
            SourceVersion.rel_path == source.rel_path,
            SourceVersion.sha256 == source.sha256,
        )
        .order_by(SourceVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version is None:
        version = SourceVersion(
            player_id=source.player_id, rel_path=source.rel_path, sha256=source.sha256,
            size=int(source.size or 0), received_at=source.last_seen_at or utcnow(),
        )
        session.add(version)
        session.flush()
    return version


def _reusable(session, task: JudgeTask) -> Optional[JudgeTask]:
    if task.rerun or not task.judge_key:
        return None
    return session.execute(
        select(JudgeTask)
        .where(
            JudgeTask.judge_key == task.judge_key,
            JudgeTask.status == DONE,
            JudgeTask.result_json.isnot(None),
            JudgeTask.id != task.id,
            JudgeTask.outcome.notin_(_NOT_REUSABLE),
        )
        .order_by(JudgeTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _lease_next(ctx, worker_id: int, now: datetime) -> Optional[Lease]:
    with ctx.db.session() as session:
        while True:
            task = session.execute(
                select(JudgeTask)
                .where(
                    ((JudgeTask.status == QUEUED)
                     & (JudgeTask.not_before.is_(None) | (JudgeTask.not_before <= now)))
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
            if is_pinned(task):
                version = session.get(SourceVersion, task.source_version_id) if task.source_version_id else None
                if version is None or not ctx.blobs.has(version.sha256):
                    _finish(task, FAILED, now, error="这一版代码已被删除")
                    session.flush()
                    continue
                task.source_path = version.rel_path
                task.source_sha256 = version.sha256
            else:
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
                task.source_version_id = version_for(session, source).id
                task.source_path = source.rel_path
                task.source_sha256 = source.sha256
            task.judge_key = judge_key(problem, task.source_sha256)
            earlier = _reusable(session, task)
            if earlier is not None:
                # 同一 (数据包, 评测配置, 代码) 评过：抄结果，不占评测机
                root = earlier.reused_from or earlier.id
                document = json.loads(earlier.result_json)
                document["reused_from"] = root
                task.reused_from = root
                _store_result(ctx, session, task, contest, player, document, now,
                              judged_at=document.get("judged_at"))
                session.flush()
                continue
            task.status = RUNNING
            task.worker_id = worker_id
            task.lease_until = now + timedelta(seconds=LEASE_SECONDS)
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


def _encode(document: Dict[str, Any]) -> bytes:
    raw = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
    if len(raw) > MAX_RESULT_BYTES:
        document = dict(document, cases=[], truncated=True)
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    return raw


def write_result(path: Path, document: Dict[str, Any]) -> None:
    """原子写 ``result.json``（临时名 + rename），扫描器不会读到半截。

    扫描器按**整秒** mtime 判"没变"：同一秒里写两次（复用结果是瞬间完成的），第二次会被
    当成没变而漏收。所以新文件的整秒 mtime 不大于旧文件时，把它拨到旧值 + 1 秒。
    """
    raw = _encode(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = int(path.stat().st_mtime)  # type: Optional[int]
    except OSError:
        previous = None
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_bytes(raw)
    os.replace(str(tmp), str(path))
    if previous is not None and int(path.stat().st_mtime) <= previous:
        os.utime(str(path), (previous + 1, previous + 1))


def _should_apply(session, task: JudgeTask) -> bool:
    """评最新版的任务交回结果时，写不写 ``result.json``。

    已写进去的结果来自**更新的**版本时不写（乱序：旧版本的任务比新版本的晚交回），这份结果
    只进历史。同一版本重评照写（数据包改了、教师要求重跑）。
    """
    if is_pinned(task):
        return False
    newest = session.execute(
        select(func.max(JudgeTask.source_version_id)).where(
            JudgeTask.contest_id == task.contest_id,
            JudgeTask.player_id == task.player_id,
            JudgeTask.problem == task.problem,
            JudgeTask.applied_at.isnot(None),
            JudgeTask.id != task.id,
        )
    ).scalar_one_or_none()
    return newest is None or (task.source_version_id or 0) >= newest


def apply_result(ctx, task: JudgeTask, contest: Contest, player: Player, now: datetime) -> None:
    """把任务的结果写成当前 ``result.json``（扫描器随后收进矩阵）。"""
    document = json.loads(task.result_json)
    write_result(result_path(ctx, contest.slug, player.player_no, task.problem), document)
    task.applied_at = now


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _store_result(ctx, session, task: JudgeTask, contest: Contest, player: Player,
                  document: Dict[str, Any], now: datetime, *, judged_at: Optional[str] = None) -> None:
    """记下任务的结果（连同明细）；评最新版且没被更新的版本抢先时写 ``result.json``。"""
    document = dict(document)
    document["source_path"] = task.source_path
    document["source_sha256"] = task.source_sha256
    document["judged_at"] = judged_at or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    document["task_id"] = task.id
    if task.source_version_id:
        document["source_version_id"] = task.source_version_id
    task.result_json = _encode(document).decode("utf-8")
    task.score = _int_or_none(document.get("score"))
    task.max_score = _int_or_none(document.get("max_score"))
    _finish(task, DONE, now, outcome=str(document.get("status") or "")[:16])
    if _should_apply(session, task):
        apply_result(ctx, task, contest, player, now)


def complete(ctx, task_id: int, worker_id: int, document: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    with ctx.db.session() as session:
        task = leased_task(session, task_id, worker_id)
        if task is None:
            return False
        contest = session.get(Contest, task.contest_id)
        player = session.get(Player, task.player_id)
        _store_result(ctx, session, task, contest, player, document, now)
    return True


def adopt(ctx, session, task: JudgeTask, now: Optional[datetime] = None) -> None:
    """教师「采用此版本成绩」：把这个任务的结果写成当前 ``result.json``。"""
    contest = session.get(Contest, task.contest_id)
    player = session.get(Player, task.player_id)
    apply_result(ctx, task, contest, player, now or utcnow())


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


def progress(session, contest_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """排队 / 在评的全数；完成 / 失败只数每 (选手, 题目) **最新的那条**（任务即历史，
    旧的完成任务不该让进度条越涨越长）。``delayed`` 是排队里还在去抖、没到点的。"""
    now = now or utcnow()
    rows = session.execute(
        select(JudgeTask.id, JudgeTask.player_id, JudgeTask.problem, JudgeTask.status,
               JudgeTask.not_before, JudgeTask.error, Player.player_no)
        .join(Player, JudgeTask.player_id == Player.id)
        .where(JudgeTask.contest_id == contest_id)
        .order_by(JudgeTask.id)
    ).all()
    counts = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0}
    delayed = 0
    latest = {}  # type: Dict[Tuple[int, str], Any]
    for row in rows:
        latest[(row.player_id, row.problem)] = row
        if row.status in (QUEUED, RUNNING):
            counts[row.status] += 1
            if row.status == QUEUED and row.not_before is not None and row.not_before > now:
                delayed += 1
    failures = []
    for row in latest.values():
        if row.status in (DONE, FAILED):
            counts[row.status] += 1
        if row.status == FAILED:
            failures.append({"player_no": row.player_no, "problem": row.problem, "error": row.error or ""})
    failures.sort(key=lambda item: (item["player_no"], item["problem"]))
    return {"total": sum(counts.values()), **counts, "delayed": delayed, "failures": failures[:50]}
