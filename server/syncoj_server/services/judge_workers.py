"""评测机的状态与管理动作（§12.10）。

* 在线判定：最近联系（任何一次带令牌的请求）不超过轮询间隔的 3 倍。轮询间隔是评测机
  自报的；旧评测机不报，按默认 5 秒算。评测中的新评测机照样每个轮询间隔 poll 一次
  （``want`` = 0），所以评一个长任务不会被误判离线。
* 暂停（``enabled`` = False）：领不到新任务，心跳照常，手上的评完照常交。
* 停止 / 重启：记进 ``desired``，在下一次 poll 的响应里带走并复位成 ``run``。
* 正在评什么：不靠评测机自报，直接看任务表里 ``running`` 且租约在它名下的 ——
  服务端持有全部状态（与 Agent 一侧同一条原则）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select

from ..models import JudgeTask, JudgeWorker, Player, iso_utc
from ..schemas import JudgePollIn, JudgeWorkerOut, JudgeWorkerTaskOut

DEFAULT_POLL_SECONDS = 5
#: 在线判定的下限：轮询间隔很短时也给网络抖动留余地
MIN_ONLINE_SECONDS = 15

COMMANDS = ("run", "stop", "restart")


def online_window(row: JudgeWorker) -> timedelta:
    poll = int(row.poll_seconds or DEFAULT_POLL_SECONDS)
    return timedelta(seconds=max(MIN_ONLINE_SECONDS, 3 * poll))


def is_online(row: JudgeWorker, now: datetime) -> bool:
    return row.last_seen_at is not None and now - row.last_seen_at <= online_window(row)


def remote_control(row: JudgeWorker) -> bool:
    """报过状态的评测机才认 poll 响应里的命令；旧评测机只调 ``/lease``。"""
    return bool(row.version)


def record_poll(row: JudgeWorker, payload: JudgePollIn) -> None:
    row.version = payload.version
    row.backend = payload.backend
    row.isolated = payload.isolated
    row.hostname = payload.hostname
    row.platform = payload.platform
    row.slots = payload.slots
    row.poll_seconds = max(1, int(round(payload.poll_seconds)))
    if payload.boot_id:
        row.boot_id = payload.boot_id


def issue_command(row: JudgeWorker, command: str) -> None:
    row.desired = command
    row.desired_boot = row.boot_id


def take_command(row: JudgeWorker, boot_id: Optional[str] = None) -> str:
    """这次 poll 要送达的命令。

    带 ``boot_id`` 的评测机：命令一直重发给下命令时的那个进程（停止 / 重启都是幂等的，
    响应丢了下一次 poll 还会收到）；换了进程说明命令已经生效，复位成 ``run``。
    不带的（或下命令时还没报过）按一次性送达。
    """
    command = row.desired if row.desired in COMMANDS else "run"
    if command == "run":
        return command
    if boot_id and row.desired_boot:
        if boot_id == row.desired_boot:
            return command
        row.desired, row.desired_boot = "run", None
        return "run"
    row.desired, row.desired_boot = "run", None
    return command


def running_tasks(session, worker_ids: List[int]) -> Dict[int, List[JudgeWorkerTaskOut]]:
    if not worker_ids:
        return {}
    rows = session.execute(
        select(JudgeTask.id, JudgeTask.worker_id, JudgeTask.contest_id, JudgeTask.problem, Player.player_no)
        .join(Player, Player.id == JudgeTask.player_id)
        .where(JudgeTask.status == "running", JudgeTask.worker_id.in_(worker_ids))
        .order_by(JudgeTask.id)
    ).all()
    out: Dict[int, List[JudgeWorkerTaskOut]] = {}
    for task_id, worker_id, contest_id, problem, player_no in rows:
        out.setdefault(worker_id, []).append(
            JudgeWorkerTaskOut(id=task_id, contest_id=contest_id, player_no=player_no, problem=problem)
        )
    return out


def worker_out(row: JudgeWorker, now: datetime, running: Optional[List[JudgeWorkerTaskOut]] = None) -> JudgeWorkerOut:
    return JudgeWorkerOut(
        id=row.id,
        name=row.name,
        created_at=iso_utc(row.created_at) or "",
        last_seen_at=iso_utc(row.last_seen_at),
        last_seen_ip=row.last_seen_ip,
        online=is_online(row, now),
        enabled=row.enabled is not False,
        desired=row.desired if row.desired in COMMANDS else "run",
        local=bool(row.is_local),
        version=row.version,
        backend=row.backend,
        isolated=row.isolated,
        hostname=row.hostname,
        platform=row.platform,
        slots=row.slots,
        remote_control=remote_control(row),
        running=list(running or []),
    )


def release_tasks(session, worker_id: int) -> int:
    """把这台评测机名下还在评的任务立刻退回队列（吊销时用），返回条数。

    不等 15 分钟租约到期：令牌已经失效，它交不回结果了。``attempt`` 不退，
    这一次算作一次尝试（反复吊销同一批任务不会无限重来）。
    """
    tasks = session.execute(
        select(JudgeTask).where(JudgeTask.status == "running", JudgeTask.worker_id == worker_id)
    ).scalars().all()
    for task in tasks:
        task.status = "queued"
        task.worker_id = None
        task.lease_until = None
    return len(tasks)
