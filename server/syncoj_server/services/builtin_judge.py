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

from ..judging.precheck import count_flags
from ..judging.runner import LANGUAGES
from ..models import Contest, ContestStatus, JudgeTask, Player, Problem, SourceFile, SourceVersion, utcnow
from ..paths import safe_join
from . import matching, problem_rules

LEASE_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
DEFAULT_TIME_LIMIT_MS = 1000
DEFAULT_MEMORY_LIMIT_MB = 256
RESULT_FILE_NAME = "result.json"
MAX_RESULT_BYTES = 2 * 1024 * 1024

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
#: 失败任务的两类（§12.5 状态词）：选代码 / 题目配置失败 = SF（重新评测即可），评测机连续出错 = SE
SUBMIT_FAILED, SYSTEM_ERROR = "SF", "SE"
#: 被教师取消的任务（§12.5）：不写 result.json；正在评的评测机交结果时 404 丢掉
CANCELLED_OUTCOME = "CA"
#: 在评任务的阶段（评测机上报）
PHASES = ("compiling", "judging")

#: 题型（§12.13）。没声明能力的评测机（旧评测机、/lease）只领传统题
TRADITIONAL = "traditional"
PROBLEM_TYPES = ("traditional", "grader", "answer_only", "interactive")
#: 评测机在 features 里用 ``type:<题型>`` 报它能评的题型
TYPE_FEATURE_PREFIX = "type:"

#: 触发方式：收到新版本自动排 / 智能模式判断"可能写完了"才排（§12.12）/ 教师排（评最新版）/
#: 教师指定历史版本重测
AUTO, SMART, MANUAL, REJUDGE = "auto", "smart", "manual", "rejudge"
TRIGGERS = (AUTO, SMART, MANUAL, REJUDGE)

#: 领任务的优先级（大的先领，§12.12）。智能模式各信号的优先级在 ``smart_judge.PRIORITY``，
#: 都落在手动与收到新版本即评之间
PRIORITY_MANUAL = 100
PRIORITY_AUTO = 10

#: 场次的自动入队开关：关闭 / 收到新版本即评 / 智能（§12.12）
AUTO_OFF, AUTO_ON_CHANGE, AUTO_SMART = "off", "on_change", "smart"
AUTO_MODES = (AUTO_OFF, AUTO_ON_CHANGE, AUTO_SMART)
DEFAULT_AUTO_DELAY = 60
MAX_AUTO_DELAY = 3600

#: 不复用的结果：评测系统自己的错（SE）不该被抄给下一次
_NOT_REUSABLE = ("SE",)


def problem_type_of(row: Problem) -> str:
    return row.problem_type or TRADITIONAL


def testdata_info(row: Problem) -> Dict[str, Any]:
    try:
        info = json.loads(row.testdata_info or "{}")
    except ValueError:
        return {}
    return info if isinstance(info, dict) else {}


def answer_names(row: Problem) -> Dict[str, str]:
    """提交答案题：测试点 → 答案文件名（``{ident}`` 已展开）。没有数据包为空。"""
    names = testdata_info(row).get("answer_names") or {}
    if not isinstance(names, dict):
        return {}
    return {str(case): str(name).replace("{ident}", row.ident) for case, name in names.items()}


def problem_config(row: Problem) -> Dict[str, Any]:
    return {
        "ident": row.ident,
        "time_limit_ms": int(row.time_limit_ms or DEFAULT_TIME_LIMIT_MS),
        "memory_limit_mb": int(row.memory_limit_mb or DEFAULT_MEMORY_LIMIT_MB),
        "io_mode": row.io_mode or "stdio",
        "input_file": row.input_file or None,
        "output_file": row.output_file or None,
        "compile_flags": row.compile_flags or None,
        "problem_type": problem_type_of(row),
        "compare_mode": row.compare_mode or "both",
    }


#: checker 判定口径的版本：0.5 起 checker 按 stderr 前缀判（部分分、格式错），之前评过的
#: 有 checker 的结果不能再复用
CHECKER_REVISION = 2


def judge_key(problem: Problem, source_sha256: str) -> str:
    """(数据包 sha, 评测配置, 代码 sha) 的指纹：三者都相同的两次评测，结果没有理由不同。

    题型与比较方式是默认值（传统题、两种空白都忽略）时不进指纹，老题目的指纹与以前相同；
    有 checker 的题带上判定口径的版本。"""
    config = problem_config(problem)
    if config["problem_type"] == TRADITIONAL:
        config.pop("problem_type")
    if config["compare_mode"] == "both":
        config.pop("compare_mode")
    if testdata_info(problem).get("has_checker"):
        config["checker_revision"] = CHECKER_REVISION
    raw = json.dumps(
        {"config": config, "testdata": problem.testdata_sha256, "source": source_sha256},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def judgeable_path(problem: Problem, rel_path: str) -> bool:
    """归到这道题的这个文件能不能当评测对象：提交答案题是答案文件，其余是 C / C++ 源文件。"""
    name = PurePosixPath(rel_path).name
    if problem_type_of(problem) == "answer_only":
        return name in set(answer_names(problem).values())
    if PurePosixPath(rel_path).suffix.lower() not in LANGUAGES:
        return False
    return name not in grader_names(problem)


def grader_names(problem: Problem) -> set:
    """函数交互题数据包里出题人的文件名（grader 与附带文件）：选手目录里同名的文件不当作选手代码。"""
    if problem_type_of(problem) != "grader":
        return set()
    info = testdata_info(problem)
    return {str(n) for n in (info.get("grader_names") or []) + (info.get("grader_files") or [])}


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
                priority=PRIORITY_AUTO if trigger == AUTO else PRIORITY_MANUAL,
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
        trigger=REJUDGE, requested_by=requested_by, rerun=bool(rerun), priority=PRIORITY_MANUAL,
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
    """这个路径归到的、能自带评测的题（自带评测、有数据包、是这道题的评测对象）；否则 None。"""
    ident = classify(session, contest_id, [rel_path], settings)[rel_path]
    if not ident:
        return None
    problem = session.execute(
        select(Problem).where(Problem.contest_id == contest_id, Problem.ident == ident)
    ).scalar_one_or_none()
    if problem is None or not is_builtin(problem) or not problem.testdata_sha256:
        return None
    if not judgeable_path(problem, rel_path):
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
    if auto_mode(contest) != AUTO_ON_CHANGE or contest.status == ContestStatus.ARCHIVED:
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
        trigger=AUTO, not_before=due, priority=PRIORITY_AUTO,
    ))
    return ident


def _live_files(session, player_id: int) -> List[SourceFile]:
    return list(session.execute(
        select(SourceFile).where(
            SourceFile.player_id == player_id,
            SourceFile.deleted_at.is_(None),
            SourceFile.content_stored.is_(True),
        )
    ).scalars().all())


def pick_answers(
    session, player_id: int, ident: str, rules: List[matching.ProblemRule], names: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """提交答案题：按文件名找这位选手每个测试点的答案文件。返回 ``(清单, 错误)``。

    只看归到这道题的、还在机器上且内容已存下的文件；同名的取路径最浅的，深度也相同就如实报错。
    一个都没有 = 没交（清单为空）；缺几个照常评，缺的点判错。"""
    by_name: Dict[str, List[SourceFile]] = {}
    wanted = set(names.values())
    for row in _live_files(session, player_id):
        name = PurePosixPath(row.rel_path).name
        if name in wanted and matching.match_problem(row.rel_path, rules) == ident:
            by_name.setdefault(name, []).append(row)
    picked: Dict[str, SourceFile] = {}
    for name, rows in by_name.items():
        rows.sort(key=lambda r: (r.rel_path.count("/"), r.rel_path))
        if len(rows) > 1 and rows[0].rel_path.count("/") == rows[1].rel_path.count("/"):
            return [], "答案文件 %s 有多份，无法确定评哪一份：%s" % (
                name, ", ".join(r.rel_path for r in rows[:5]))
        picked[name] = rows[0]
    if not picked:
        return [], None
    manifest = []
    for case, name in names.items():
        row = picked.get(name)
        manifest.append({"case": case, "name": name, "path": row.rel_path if row else None,
                         "sha256": row.sha256 if row else None})
    return manifest, None


def manifest_sha(manifest: List[Dict[str, Any]]) -> str:
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def task_sources(task: JudgeTask) -> List[Dict[str, Any]]:
    try:
        items = json.loads(task.sources_json or "[]")
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def pick_source(
    session, player_id: int, ident: str, rules: List[matching.ProblemRule], problem: Optional[Problem] = None,
) -> Tuple[Optional[SourceFile], Optional[str]]:
    """选出这位选手这道题要评的那份代码。返回 ``(行, 错误)``：都为 None = 没交。

    只看**还在机器上**（没被扫描对账标成已消失）且**内容已存下**的文件，按题目的认领模式
    归题，再按语言后缀过滤。多份候选时优先文件名与题目同名的；还分不出来就如实报错，
    不猜 —— 评错一份代码比"请教师看一眼"代价大得多。
    """
    excluded = grader_names(problem) if problem is not None else set()
    candidates = [
        row for row in _live_files(session, player_id)
        if matching.match_problem(row.rel_path, rules) == ident
        and PurePosixPath(row.rel_path).suffix.lower() in LANGUAGES
        and PurePosixPath(row.rel_path).name not in excluded
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
            "sources": task_sources(self.task),
        }


#: 领任务串行化：几台评测机同时来领时，"先查后改"会在 SQLite 上撞旧快照（database is
#: locked）或把同一个任务发给两台。服务端是单进程，进程内一把锁就够。
_LEASE_LOCK = threading.Lock()


def worker_types(features: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """评测机报的能力 → 它能评的题型。传统题人人能评；没报能力（旧评测机）只评传统题。"""
    types = [TRADITIONAL]
    for item in features or ():
        if item.startswith(TYPE_FEATURE_PREFIX) and item[len(TYPE_FEATURE_PREFIX):] in PROBLEM_TYPES:
            name = item[len(TYPE_FEATURE_PREFIX):]
            if name not in types:
                types.append(name)
    return tuple(types)


def lease_next(ctx, worker_id: int, now: Optional[datetime] = None,
               types: Iterable[str] = (TRADITIONAL,)) -> Optional[Lease]:
    """领下一个任务（只领 ``types`` 里的题型；不能评的任务留在队列里、不消耗次数）。
    没交代码的、题目配置不对的就地了结，接着找下一个。"""
    with _LEASE_LOCK:
        return _lease_next(ctx, worker_id, now or utcnow(), tuple(types))


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


def _lease_next(ctx, worker_id: int, now: datetime, types: Tuple[str, ...] = (TRADITIONAL,)) -> Optional[Lease]:
    kind = func.coalesce(Problem.problem_type, TRADITIONAL)
    with ctx.db.session() as session:
        while True:
            task = session.execute(
                select(JudgeTask)
                .outerjoin(Problem, (Problem.contest_id == JudgeTask.contest_id) & (Problem.ident == JudgeTask.problem))
                .where(
                    ((JudgeTask.status == QUEUED)
                     & (JudgeTask.not_before.is_(None) | (JudgeTask.not_before <= now)))
                    | ((JudgeTask.status == RUNNING) & (JudgeTask.lease_until < now))
                )
                # 题目已删的任务照样领出来就地了结；其余只领这台评测机能评的题型
                .where(Problem.id.is_(None) | kind.in_(types))
                # 优先级高的先领（§12.12），同级先排先领
                .order_by(JudgeTask.priority.desc(), JudgeTask.id)
                .limit(1)
            ).scalar_one_or_none()
            if task is None:
                return None
            task.attempt = int(task.attempt or 0) + 1
            if task.attempt > MAX_ATTEMPTS:
                _finish(task, FAILED, now, outcome=SYSTEM_ERROR,
                        error=task.error or "评测机连续 %d 次没有交回结果" % MAX_ATTEMPTS)
                session.flush()
                continue
            contest = session.get(Contest, task.contest_id)
            player = session.get(Player, task.player_id)
            problem = session.execute(
                select(Problem).where(Problem.contest_id == task.contest_id, Problem.ident == task.problem)
            ).scalar_one_or_none()
            if contest is None or player is None or problem is None or not is_builtin(problem):
                _finish(task, FAILED, now, outcome=SUBMIT_FAILED, error="题目已删除或不再使用自带评测")
                session.flush()
                continue
            if not problem.testdata_sha256 or not ctx.blobs.has(problem.testdata_sha256):
                _finish(task, FAILED, now, outcome=SUBMIT_FAILED, error="题目没有数据包")
                session.flush()
                continue
            task.phase = None
            if is_pinned(task) and problem_type_of(problem) == "answer_only":
                _finish(task, FAILED, now, outcome=SUBMIT_FAILED, error="提交答案题不能单独重测一个文件的历史版本")
                session.flush()
                continue
            if problem_type_of(problem) == "answer_only":
                rules = problem_rules.rules_for(session, task.contest_id, ctx.settings)
                manifest, error = pick_answers(session, task.player_id, task.problem, rules, answer_names(problem))
                if error:
                    _finish(task, FAILED, now, outcome=SUBMIT_FAILED, error=error)
                    session.flush()
                    continue
                present = [item for item in manifest if item["sha256"]]
                if not present or not all(ctx.blobs.has(item["sha256"]) for item in present):
                    _finish(task, DONE, now, outcome="missing")
                    session.flush()
                    continue
                newest = None
                for item in present:
                    row = session.execute(select(SourceFile).where(
                        SourceFile.player_id == task.player_id, SourceFile.rel_path == item["path"])).scalar_one()
                    version = version_for(session, row)
                    newest = version.id if newest is None else max(newest, version.id)
                task.source_version_id = newest
                task.source_path = "%s/*" % task.problem
                task.sources_json = json.dumps(manifest, ensure_ascii=False)
                task.source_sha256 = manifest_sha(manifest)
            elif is_pinned(task):
                version = session.get(SourceVersion, task.source_version_id) if task.source_version_id else None
                if version is None or not ctx.blobs.has(version.sha256):
                    _finish(task, FAILED, now, outcome=SUBMIT_FAILED, error="这一版代码已被删除")
                    session.flush()
                    continue
                task.source_path = version.rel_path
                task.source_sha256 = version.sha256
            else:
                rules = problem_rules.rules_for(session, task.contest_id, ctx.settings)
                source, error = pick_source(session, task.player_id, task.problem, rules, problem)
                if error:
                    _finish(task, FAILED, now, outcome=SUBMIT_FAILED, error=error)
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
    task.phase = None
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
    task.warning_count, task.risk_count = count_flags(document)
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
        dirty = getattr(ctx, "judge_dirty_contests", None)
        if dirty is not None:
            dirty.add(task.contest_id)
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
            _finish(task, FAILED, now, outcome=SYSTEM_ERROR, error=task.error)
        else:
            task.status = QUEUED
            task.worker_id = None
            task.lease_until = None
            task.phase = None
    return True


def set_phase(ctx, task_id: int, worker_id: int, phase: str) -> bool:
    """评测机报告在评任务的阶段（编译中 / 评测中）。"""
    with ctx.db.session() as session:
        task = leased_task(session, task_id, worker_id)
        if task is None:
            return False
        task.phase = phase
    return True


def cancel(session, contest_id: int, problem: Optional[str] = None, player_id: Optional[int] = None,
           task_id: Optional[int] = None, now: Optional[datetime] = None) -> int:
    """取消排队中 / 在评的任务（状态 CA）：不写 result.json，在评的评测机交结果时被丢掉（404）。
    返回取消的条数。被取消的任务不算"评过"：智能模式照样会为同一版本再排。"""
    now = now or utcnow()
    query = select(JudgeTask).where(JudgeTask.contest_id == contest_id, JudgeTask.status.in_((QUEUED, RUNNING)))
    if problem:
        query = query.where(JudgeTask.problem == problem)
    if player_id is not None:
        query = query.where(JudgeTask.player_id == player_id)
    if task_id is not None:
        query = query.where(JudgeTask.id == task_id)
    count = 0
    for task in session.execute(query).scalars():
        _finish(task, CANCELLED, now, outcome=CANCELLED_OUTCOME)
        task.worker_id = None
        task.phase = None
        count += 1
    session.flush()
    return count


def progress(session, contest_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """排队 / 在评的全数；完成 / 失败只数每 (选手, 题目) **最新的那条**（任务即历史，
    旧的完成任务不该让进度条越涨越长）。``delayed`` 是排队里还在去抖、没到点的。"""
    now = now or utcnow()
    rows = session.execute(
        select(JudgeTask.id, JudgeTask.player_id, JudgeTask.problem, JudgeTask.status,
               JudgeTask.not_before, JudgeTask.error, JudgeTask.outcome, JudgeTask.phase, Player.player_no)
        .join(Player, JudgeTask.player_id == Player.id)
        .where(JudgeTask.contest_id == contest_id)
        .order_by(JudgeTask.id)
    ).all()
    types = {
        row.ident: problem_type_of(row)
        for row in session.execute(select(Problem).where(Problem.contest_id == contest_id)).scalars()
    }
    servable = online_types(session, now)
    counts = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
    phases = {"compiling": 0, "judging": 0}
    delayed = 0
    unservable: Dict[str, int] = {}
    latest = {}  # type: Dict[Tuple[int, str], Any]
    for row in rows:
        latest[(row.player_id, row.problem)] = row
        if row.status in (QUEUED, RUNNING):
            counts[row.status] += 1
            if row.status == QUEUED and row.not_before is not None and row.not_before > now:
                delayed += 1
            if row.status == RUNNING and row.phase in phases:
                phases[row.phase] += 1
            kind = types.get(row.problem, TRADITIONAL)
            if row.status == QUEUED and kind not in servable:
                unservable[kind] = unservable.get(kind, 0) + 1
    failures = []
    submit_failed = 0
    for row in latest.values():
        if row.status in (DONE, FAILED, CANCELLED):
            counts[row.status] += 1
        if row.status == FAILED:
            code = SYSTEM_ERROR if row.outcome == SYSTEM_ERROR else SUBMIT_FAILED
            submit_failed += code == SUBMIT_FAILED
            failures.append({"player_no": row.player_no, "problem": row.problem, "error": row.error or "",
                             "code": code})
    failures.sort(key=lambda item: (item["player_no"], item["problem"]))
    return {"total": sum(counts.values()), **counts, "delayed": delayed, "failures": failures[:50],
            "compiling": phases["compiling"], "judging": phases["judging"],
            "submit_failed": submit_failed, "system_error": counts[FAILED] - submit_failed,
            "unservable": unservable}


def online_types(session, now: datetime) -> set:
    """在线、没暂停的评测机合起来能评的题型（全局评测服务关着时照算，那是另一回事）。"""
    from ..models import JudgeWorker
    from . import judge_workers

    types = set()
    for worker in session.execute(select(JudgeWorker)).scalars():
        if worker.enabled is False or not judge_workers.is_online(worker, now):
            continue
        types.update(worker_types((worker.features or "").split(",")))
    return types
