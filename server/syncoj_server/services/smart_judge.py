"""自动评测的「智能」模式（§12.12）：判断某人某题"可能已经写完"才排评测，减轻评测机压力。

分两层：

* :func:`decide` 是**纯函数**：输入版本时间线、机器在线、场次时间、已有任务、队列负载，
  输出要排的 ``(选手, 题目, 版本, 信号, 优先级)``。所有判定规则都在这里，单测不必造库。
* :func:`evaluate_contest` / :func:`run_round` 从库与内存注册表里取出这些输入，调用
  :func:`decide`，把结果排成 ``trigger = smart`` 的任务。后台循环见 ``tasks/smart_judge.py``。

任务照旧"领任务时才选代码"（评的总是最新版）、照旧按 ``judge_key`` 复用结果；智能模式只决定
**什么时候排**。``target_version_id`` 记下排任务时看到的最新版本，"同一版本不重复排"按它判。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, select

from ..models import Contest, ContestStatus, EventLog, JudgeTask, JudgeWorker, Player, Problem, SourceVersion, utcnow
from . import builtin_judge, judge_workers

log = logging.getLogger(__name__)

#: 信号：写完换题 / 停很久 / 快结束 / 机器掉线 / 考试结束
MOVED_ON, LONG_STABLE, NEAR_END, OFFLINE, CONTEST_END = (
    "moved_on", "long_stable", "near_end", "offline", "contest_end",
)
REASONS = (MOVED_ON, LONG_STABLE, NEAR_END, OFFLINE, CONTEST_END)

#: 优先级：手动（100）> 考试结束 > 掉线 > 快结束 > 换题 > 停很久 > 收到新版本即评（10）
PRIORITY = {CONTEST_END: 60, OFFLINE: 50, NEAR_END: 40, MOVED_ON: 30, LONG_STABLE: 20}

#: 不受冷却、也不因队列过载跳过的信号（同一版本仍不重复排）
URGENT = frozenset((CONTEST_END, OFFLINE, NEAR_END))


@dataclass(frozen=True)
class Thresholds:
    """场次的阈值（秒；``queue_factor`` 是倍数）。"""

    stable: int = 180
    idle: int = 600
    near_end: int = 900
    cooldown: int = 300
    queue_factor: int = 20


DEFAULTS = Thresholds()

#: 接口接受的范围（含两端）。``near_end = 0`` = 不用「快结束」这个信号
RANGES = {
    "stable": (30, 3600),
    "idle": (60, 7200),
    "near_end": (0, 7200),
    "cooldown": (0, 3600),
    "queue_factor": (1, 1000),
}


def thresholds_of(contest: Contest) -> Thresholds:
    """场次的阈值；NULL（老场次、没设过）读作默认值。"""

    def pick(value: Optional[int], name: str) -> int:
        if value is None:
            return getattr(DEFAULTS, name)
        low, high = RANGES[name]
        return max(low, min(int(value), high))

    return Thresholds(
        stable=pick(contest.smart_stable_seconds, "stable"),
        idle=pick(contest.smart_idle_seconds, "idle"),
        near_end=pick(contest.smart_near_end_seconds, "near_end"),
        cooldown=pick(contest.smart_cooldown_seconds, "cooldown"),
        queue_factor=pick(contest.smart_queue_factor, "queue_factor"),
    )


#: 接口字段名 → ``Thresholds`` 字段名
FIELDS = {
    "smart_stable_seconds": "stable",
    "smart_idle_seconds": "idle",
    "smart_near_end_seconds": "near_end",
    "smart_cooldown_seconds": "cooldown",
    "smart_queue_factor": "queue_factor",
}


def threshold_fields(contest: Contest) -> Dict[str, int]:
    """回显给接口的阈值（没设过的给默认值）。"""
    current = thresholds_of(contest)
    return {column: getattr(current, name) for column, name in FIELDS.items()}


@dataclass(frozen=True)
class PairState:
    """一个 (选手, 题目) 此刻的样子。"""

    player_id: int
    problem: str
    #: 归到这道题的最新一个版本
    version_id: int
    received_at: datetime
    #: 最新版本已评过 / 已排上：有任务评的（或智能模式排时看到的）就是它，任意状态；
    #: 或者有还在排队 / 在评的"评最新版"任务（领到时选的就是它）
    covered: bool = False
    #: 这一对最近一次智能模式排任务的时刻（冷却从这里算）
    last_smart_at: Optional[datetime] = None
    #: 这一对已经因为「快结束」排过
    near_end_done: bool = False


@dataclass(frozen=True)
class Pick:
    player_id: int
    problem: str
    version_id: int
    reason: str
    priority: int


@dataclass(frozen=True)
class ContestClock:
    """场次在判定时刻的状态。"""

    ends_at: Optional[datetime] = None
    #: 已结束：状态为已结束，或到了结束时间
    ended: bool = False
    #: 结束后的「全员评一遍」已做过
    end_done: bool = False
    archived: bool = False


def decide(
    *,
    now: datetime,
    pairs: Iterable[PairState],
    activity: Dict[int, Dict[str, datetime]],
    offline: Set[int],
    clock: ContestClock,
    thresholds: Thresholds,
    queued: int,
    capacity: int,
) -> List[Pick]:
    """决定这一轮要排哪些任务。纯函数。

    * ``activity``：选手 → 题目 → 这道题最新版本的收到时刻（含不评测的题，判「换题」用）；
    * ``offline``：机器掉线的选手（有机器、且全都不在线）；
    * ``queued`` / ``capacity``：全局排队任务数与在线评测机并发总数（至少按 1 算）。

    每对最多一个，取适用信号里优先级最高的；结果按优先级降序、再按 (选手, 题目)。
    """
    if clock.archived:
        return []
    picks = []  # type: List[Pick]
    if clock.ended:
        if clock.end_done:
            return []
        for pair in pairs:
            if not pair.covered:
                picks.append(_pick(pair, CONTEST_END))
        return _ordered(picks)

    overloaded = queued > max(1, int(capacity)) * thresholds.queue_factor
    near_end = (
        thresholds.near_end > 0
        and clock.ends_at is not None
        and timedelta(0) < clock.ends_at - now <= timedelta(seconds=thresholds.near_end)
    )
    for pair in pairs:
        if pair.covered:
            continue
        cooled = pair.last_smart_at is None or now - pair.last_smart_at >= timedelta(seconds=thresholds.cooldown)
        stable_for = now - pair.received_at
        reason = None  # type: Optional[str]
        if pair.player_id in offline:
            reason = OFFLINE
        elif near_end and (not pair.near_end_done or cooled):
            reason = NEAR_END
        elif overloaded or not cooled:
            reason = None
        elif stable_for >= timedelta(seconds=thresholds.stable) and _moved_on(pair, activity):
            reason = MOVED_ON
        elif stable_for >= timedelta(seconds=thresholds.idle):
            reason = LONG_STABLE
        if reason is not None:
            picks.append(_pick(pair, reason))
    return _ordered(picks)


def _moved_on(pair: PairState, activity: Dict[int, Dict[str, datetime]]) -> bool:
    """这道题的最新版本之后，这位选手别的题收到过新版本。"""
    return any(
        problem != pair.problem and at > pair.received_at
        for problem, at in activity.get(pair.player_id, {}).items()
    )


def _pick(pair: PairState, reason: str) -> Pick:
    return Pick(pair.player_id, pair.problem, pair.version_id, reason, PRIORITY[reason])


def _ordered(picks: List[Pick]) -> List[Pick]:
    return sorted(picks, key=lambda p: (-p.priority, p.player_id, p.problem))


# --------------------------------------------------------------------------- #
# 取输入、排任务
# --------------------------------------------------------------------------- #


def contest_ended(contest: Contest, now: datetime) -> bool:
    if contest.status == ContestStatus.CLOSED:
        return True
    return contest.ends_at is not None and now >= contest.ends_at


def queue_load(session, now: datetime) -> Tuple[int, int]:
    """(全局排队任务数, 在线且没暂停的评测机并发总数)。"""
    queued = session.execute(
        select(func.count(JudgeTask.id)).where(JudgeTask.status == builtin_judge.QUEUED)
    ).scalar_one()
    capacity = 0
    for row in session.execute(select(JudgeWorker)).scalars():
        if row.enabled is not False and judge_workers.is_online(row, now):
            capacity += max(1, int(row.slots or 1))
    return int(queued), capacity


def offline_players(registry, contest_id: int) -> Set[int]:
    """机器掉线的选手：内存注册表里有这一场的机器、且一台都不在线。

    服务端刚重启时注册表是空的 —— 没有记录不算掉线，免得重启一次把全场都当成掉线。
    """
    online = {}  # type: Dict[int, bool]
    for runtime in registry.all(contest_id):
        online[runtime.player_id] = online.get(runtime.player_id, False) or bool(runtime.online)
    return {player_id for player_id, flag in online.items() if not flag}


def contest_inputs(session, contest: Contest, settings) -> Tuple[List[PairState], Dict[int, Dict[str, datetime]]]:
    """从库里取出一场的 (选手, 题目) 状态与各人各题的最新活动时刻。"""
    judgeable = {
        row.ident: row
        for row in session.execute(select(Problem).where(Problem.contest_id == contest.id)).scalars()
        if builtin_judge.is_builtin(row) and row.testdata_sha256
    }
    if not judgeable:
        return [], {}
    player_ids = select(Player.id).where(Player.contest_id == contest.id)
    versions = session.execute(
        select(SourceVersion.id, SourceVersion.player_id, SourceVersion.rel_path, SourceVersion.received_at)
        .where(SourceVersion.player_id.in_(player_ids))
        .order_by(SourceVersion.received_at, SourceVersion.id)
    ).all()
    idents = builtin_judge.classify(session, contest.id, [row.rel_path for row in versions], settings)
    activity = {}  # type: Dict[int, Dict[str, datetime]]
    latest = {}  # type: Dict[Tuple[int, str], Tuple[int, datetime]]
    for row in versions:
        ident = idents.get(row.rel_path)
        if not ident:
            continue
        activity.setdefault(row.player_id, {})[ident] = row.received_at
        if ident in judgeable and builtin_judge.judgeable_path(judgeable[ident], row.rel_path):
            latest[(row.player_id, ident)] = (row.id, row.received_at)
    if not latest:
        return [], activity

    covered_versions = set()  # type: Set[int]
    pending = set()  # type: Set[Tuple[int, str]]
    last_smart = {}  # type: Dict[Tuple[int, str], datetime]
    near_end_done = set()  # type: Set[Tuple[int, str]]
    for task in session.execute(
        select(JudgeTask.player_id, JudgeTask.problem, JudgeTask.status, JudgeTask.trigger, JudgeTask.reason,
               JudgeTask.source_version_id, JudgeTask.target_version_id, JudgeTask.created_at)
        # 被取消的任务不算"评过"：取消之后同一版本照样可以再排
        .where(JudgeTask.contest_id == contest.id, JudgeTask.status != builtin_judge.CANCELLED)
    ):
        key = (task.player_id, task.problem)
        for version_id in (task.source_version_id, task.target_version_id):
            if version_id is not None:
                covered_versions.add(version_id)
        # 排队中的"评最新版"任务领到时选的就是最新版；在评的已选定版本，按版本判
        if task.status == builtin_judge.QUEUED and task.trigger != builtin_judge.REJUDGE:
            pending.add(key)
        if task.trigger == builtin_judge.SMART:
            if key not in last_smart or task.created_at > last_smart[key]:
                last_smart[key] = task.created_at
            if task.reason == NEAR_END:
                near_end_done.add(key)

    pairs = [
        PairState(
            player_id=player_id, problem=ident, version_id=version_id, received_at=received_at,
            covered=version_id in covered_versions or (player_id, ident) in pending,
            last_smart_at=last_smart.get((player_id, ident)),
            near_end_done=(player_id, ident) in near_end_done,
        )
        for (player_id, ident), (version_id, received_at) in sorted(latest.items())
    ]
    return pairs, activity


def evaluate_contest(ctx, contest_id: int, now: Optional[datetime] = None) -> List[Pick]:
    """评估一场并排任务（一个短事务）；返回排了的。不是智能模式、已归档的场次什么也不做。"""
    now = now or utcnow()
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None or builtin_judge.auto_mode(contest) != builtin_judge.AUTO_SMART:
            return []
        if contest.status == ContestStatus.ARCHIVED:
            return []
        ended = contest_ended(contest, now)
        if not ended:
            # 场次重新开放（结束时间改到以后 / 状态改回进行中）：下次结束再评一遍
            contest.smart_end_done_at = None
        elif contest.smart_end_done_at is not None:
            return []
        pairs, activity = contest_inputs(session, contest, ctx.settings)
        queued, capacity = queue_load(session, now)
        picks = decide(
            now=now,
            pairs=pairs,
            activity=activity,
            offline=set() if ended else offline_players(ctx.registry, contest.id),
            clock=ContestClock(ends_at=contest.ends_at, ended=ended, end_done=False, archived=False),
            thresholds=thresholds_of(contest),
            queued=queued,
            capacity=capacity,
        )
        for pick in picks:
            session.add(JudgeTask(
                contest_id=contest.id, player_id=pick.player_id, problem=pick.problem,
                status=builtin_judge.QUEUED, trigger=builtin_judge.SMART, reason=pick.reason,
                priority=pick.priority, target_version_id=pick.version_id, created_at=now,
            ))
        if ended:
            contest.smart_end_done_at = now
            session.add(EventLog(
                level="info", category="judge_auto", contest_id=contest.id,
                message="场次已结束：智能评测为 %d 个未评的最新版本排了评测" % len(picks),
            ))
    if picks:
        counts = {}  # type: Dict[str, int]
        for pick in picks:
            counts[pick.reason] = counts.get(pick.reason, 0) + 1
        log.info("智能评测：场次 %d 排 %d 个任务 %s", contest_id, len(picks), counts)
    return picks


def run_round(ctx, now: Optional[datetime] = None) -> int:
    """一轮：所有智能模式、未归档的场次各评估一次（每场一个事务）。返回排了的任务数。"""
    now = now or utcnow()
    with ctx.db.session() as session:
        ids = list(session.execute(
            select(Contest.id).where(
                Contest.auto_judge == builtin_judge.AUTO_SMART,
                Contest.status != ContestStatus.ARCHIVED,
            ).order_by(Contest.id)
        ).scalars())
    total = 0
    for contest_id in ids:
        try:
            total += len(evaluate_contest(ctx, contest_id, now))
        except Exception:  # pragma: no cover - 一场出错不影响其他场次
            log.exception("智能评测：评估场次 %d 失败", contest_id)
    return total
