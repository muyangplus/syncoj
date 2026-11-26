"""评测机的状态与管理动作（§12.10）。

* 在线判定：最近联系（任何一次带令牌的请求）不超过轮询间隔的 3 倍。轮询间隔是评测机
  自报的；旧评测机不报，按默认 5 秒算。评测中的新评测机照样每个轮询间隔 poll 一次
  （``want`` = 0），所以评一个长任务不会被误判离线。
* 暂停（``enabled`` = False）：领不到新任务，心跳照常，手上的评完照常交。
* 停止 / 重启：记进 ``desired``，在下一次 poll 的响应里带走并复位成 ``run``。
* 卸载（``uninstall``）：同样记进 ``desired``，但**不复位** —— 一直下发到评测机告别
  （``POST /judge/goodbye``）、服务端删掉这一行为止；中途评测机崩了被 systemd / 计划任务
  拉起，新进程照样收到。只对报了 ``uninstall`` 能力的评测机下。
* 并发数（``desired_slots``）：管理端设定，随 poll 响应下发；评测机发现与自己的 ``--slots``
  不同时评完手上的任务、以新值重启。只有报了 ``slots`` 能力的评测机才能改；后端不是 isolate
  的只能是 1（rlimit / winjob 的并发任务以同一个账号运行，能互相读写工作目录）。
* 「评测服务」开关：全局一个，存在 ``runtime_setting``（键 ``judge_service``）。关 = 所有
  评测机都领不到新任务（``/poll`` 与 ``/lease`` 都遵守），手上的照常评完，排队的留着；
  自动入队照常入队，只是不发出去。
* 正在评什么：不靠评测机自报，直接看任务表里 ``running`` 且租约在它名下的 ——
  服务端持有全部状态（与 Agent 一侧同一条原则）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select

from ..models import EventLog, JudgeTask, JudgeWorker, Player, RuntimeSetting, iso_utc, utcnow
from ..schemas import JudgePollIn, JudgeServiceOut, JudgeWorkerOut, JudgeWorkerTaskOut
from .builtin_judge import worker_types

DEFAULT_POLL_SECONDS = 5
#: 在线判定的下限：轮询间隔很短时也给网络抖动留余地
MIN_ONLINE_SECONDS = 15

COMMANDS = ("run", "stop", "restart", "uninstall")
MAX_SLOTS = 64
#: ``runtime_setting`` 里「评测服务」开关的键；值 ``on`` / ``off``，没有这一行 = 开
SERVICE_KEY = "judge_service"


def online_window(row: JudgeWorker) -> timedelta:
    poll = int(row.poll_seconds or DEFAULT_POLL_SECONDS)
    return timedelta(seconds=max(MIN_ONLINE_SECONDS, 3 * poll))


def is_online(row: JudgeWorker, now: datetime) -> bool:
    return row.last_seen_at is not None and now - row.last_seen_at <= online_window(row)


def remote_control(row: JudgeWorker) -> bool:
    """报过状态的评测机才认 poll 响应里的命令；旧评测机只调 ``/lease``。"""
    return bool(row.version)


def slots_control(row: JudgeWorker) -> bool:
    """报了 ``slots`` 能力的评测机才认下发的并发数。"""
    return "slots" in (row.features or "").split(",")


def uninstall_control(row: JudgeWorker) -> bool:
    """报了 ``uninstall`` 能力的评测机才认卸载命令（会告别、按安装方式清理自己）。"""
    return "uninstall" in (row.features or "").split(",")


def slots_problem(row: JudgeWorker, slots: int) -> Optional[str]:
    """不能把这台评测机的并发数改成 ``slots`` 时返回原因：``unsupported``（旧评测机，不认下发的
    并发数）/ ``backend``（后端不是 isolate 却要大于 1）。"""
    if not remote_control(row) or not slots_control(row):
        return "unsupported"
    if slots > 1 and row.backend != "isolate":
        return "backend"
    return None


def slots_for_poll(row: JudgeWorker) -> Optional[int]:
    """poll 响应里下发的并发数：没设过、或与后端不相容（后端换了）时不下发。"""
    wanted = row.desired_slots
    if not wanted or not slots_control(row):
        return None
    if wanted > 1 and row.backend != "isolate":
        return None
    return int(wanted)


def service_enabled(session) -> bool:
    row = session.get(RuntimeSetting, SERVICE_KEY)
    return row is None or row.value != "off"


def set_service_enabled(session, enabled: bool) -> None:
    value = "on" if enabled else "off"
    row = session.get(RuntimeSetting, SERVICE_KEY)
    if row is None:
        session.add(RuntimeSetting(key=SERVICE_KEY, value=value, updated_at=utcnow()))
    elif row.value != value:
        row.value = value
        row.updated_at = utcnow()
    session.flush()


def service_out(session, now: Optional[datetime] = None) -> JudgeServiceOut:
    """开关与全部场次的排队 / 在评数。"""
    from sqlalchemy import func

    now = now or utcnow()
    counts = dict(session.execute(
        select(JudgeTask.status, func.count(JudgeTask.id))
        .where(JudgeTask.status.in_(("queued", "running")))
        .group_by(JudgeTask.status)
    ).all())
    delayed = session.execute(
        select(func.count(JudgeTask.id)).where(
            JudgeTask.status == "queued", JudgeTask.not_before.isnot(None), JudgeTask.not_before > now,
        )
    ).scalar_one()
    return JudgeServiceOut(
        enabled=service_enabled(session),
        queued=int(counts.get("queued", 0)),
        delayed=int(delayed or 0),
        running=int(counts.get("running", 0)),
    )


def record_poll(row: JudgeWorker, payload: JudgePollIn) -> None:
    row.version = payload.version
    row.backend = payload.backend
    row.isolated = payload.isolated
    row.hostname = payload.hostname
    row.platform = payload.platform
    row.slots = payload.slots
    row.poll_seconds = max(1, int(round(payload.poll_seconds)))
    features = sorted({str(item)[:32] for item in payload.features or [] if item})
    row.features = ",".join(features)[:128] or None
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
    if command in ("run", "uninstall"):
        # 卸载不复位：告别时这一行就删了；在那之前换了进程（崩溃后被拉起）也要接着卸
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
        desired_slots=row.desired_slots,
        remote_control=remote_control(row),
        slots_control=remote_control(row) and slots_control(row),
        uninstall_control=bool(row.is_local) or (remote_control(row) and uninstall_control(row)),
        types=list(worker_types((row.features or "").split(","))),
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


#: 告别时报的清理方式 → 审计日志里的说明
UNINSTALL_KIND_LABEL = {
    "systemd": "systemd 服务、程序、令牌与缓存随后由 root 删除",
    "schtask": "计划任务、程序与令牌随后由清理任务（SYSTEM）删除",
    "local": "本机评测机目录已删除",
    "manual": "手动运行：已删令牌与缓存，程序文件需到那台机器上手动删除",
}


def forget_uninstalled(session, row: JudgeWorker, kind: str, actor: Optional[str] = None) -> int:
    """卸载完成（评测机告别，或本机评测机直接卸）：名下在评的任务退回队列、记审计、删行。
    返回退回队列的任务数。"""
    released = release_tasks(session, row.id)
    message = "评测机已卸载：%s（%s）" % (row.name, UNINSTALL_KIND_LABEL.get(kind, kind))
    if actor:
        message += "，%s 操作" % actor
    session.add(EventLog(level="info", category="judge_worker_uninstall", message=message))
    session.delete(row)
    return released
