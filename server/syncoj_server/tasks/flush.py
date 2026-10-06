"""后台任务。

只做三件事，都是"必须有，但不该挂在请求路径上"的：

1. **离线判定**。""不再 tick"" 这件事本身不产生任何事件，所以只能靠定时扫描。
2. **批量落库**。把内存注册表里的脏数据合并成**一个事务**刷入 ``agent_status``。
   这是让 SQLite 在 50 台机器持续心跳下不出现写锁争抢的关键 —— 写频率从
   "每请求一次" 降到 "每 5 秒一次"。
3. **到点自动结束场次**。配了 ``ends_at`` 的场次时间一到就置成"已结束"并留一条
   审计事件。**收不收代码不靠它**（那条判据在 ``api/agent.py`` 的上传门禁里，
   因为循环是周期跑的，"到点那一刻"不能靠等它）—— 它管的是**状态**：界面该显示
   "已结束"，机器也不该再被解析成一个还在跑的场次。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Iterable, List, Optional

from sqlalchemy import select

from ..context import AppContext
from ..models import AgentStatus, Contest, ContestStatus, EventLog, local_clock, utcnow
from ..registry import AgentRuntime

__all__ = [
    "run_maintenance_loop",
    "flush_statuses",
    "flush_once",
    "close_finished_contests",
]

log = logging.getLogger(__name__)


def close_finished_contests(ctx: AppContext, now: Optional[datetime] = None) -> List[str]:
    """把到了结束时间的场次自动置为**已结束**，返回被关掉的场次标识。

    为什么要有这一步：``ends_at`` 只用来挡上传的话，场次会永远停在"进行中"，
    而"进行中"的含义是"机器可以在这场比赛里干活"（``Contest.is_active``）。
    于是到点之后机器仍会被解析进这个场次去扫代码、去下载 —— 每一次上传都被 409
    挡回来，却始终没有任何东西告诉它们"这场结束了"。置成"已结束"，机器才会安静。

    **只往"已结束"这一个方向走，绝不自动开赛。** ``starts_at`` 到了不把 ``draft``
    改成 ``running``：开考是教师的有意动作（题面、名单、机器都得到位才敢放人进），
    按表自动开赛会在"老师迟到十分钟"时把学生放进一场已经开始的考试里 —— 而那是
    不可撤回的。反方向则相反：提前关掉最多"少收几分钟"，不会"多考"。

    判据是 ``status != closed``：``running`` 要关，``draft`` 与 ``frozen`` 也要关。
    ``draft`` 一并关掉是因为"配了结束时间就该按它结束"—— 一个忘了点开考的场次
    留在 ``draft`` 里，界面上看起来像"还没开始"，其实它的时间早就过了，教师会
    一直等一个永远不会到的开考；``frozen``（封榜）本来就还在收卷，到点也该停。
    幂等就靠这一个判据：已经 ``closed`` 的场次不会再进结果集，也就不会再记事件。
    """
    moment = now or utcnow()
    closed: List[str] = []
    with ctx.db.session() as session:
        found = list(
            session.execute(
                select(Contest).where(
                    Contest.status != ContestStatus.CLOSED,
                    Contest.ends_at.isnot(None),
                    Contest.ends_at <= moment,
                )
            ).scalars()
        )
        for contest in found:
            contest.status = ContestStatus.CLOSED
            # 这是**状态变更的审计**，不是对谁的判定：说清"哪一场、按哪个时间、
            # 被谁关的（这里没有谁）"。教师事后查"这场怎么自己结束了"时，答案
            # 必须能在这里找到 —— 只在服务端日志里留一句是不够的。
            session.add(
                EventLog(
                    level="info",
                    category="contest_auto_closed",
                    contest_id=contest.id,
                    message="场次「%s」已到配置的结束时间（%s），自动置为已结束"
                    % (contest.name, local_clock(contest.ends_at)),
                    meta_json=None,
                )
            )
            closed.append(contest.slug)

    if closed:
        log.info("到点自动结束的场次：%s", "、".join(closed))
    return closed


async def run_maintenance_loop(ctx: AppContext) -> None:
    """常驻维护循环。由 lifespan 启动，随应用关闭而取消。"""
    interval = ctx.settings.flush_interval_seconds
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, flush_once, ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 后台任务绝不能把主进程带崩
            log.exception("维护任务执行失败，将在下一周期重试")
        await asyncio.sleep(interval)


def flush_once(ctx: AppContext) -> int:
    """执行一轮：判定离线 -> 自动结束到点的场次 -> 落库。返回本轮落库条数。"""
    flipped: List[AgentRuntime] = ctx.registry.sweep_offline()
    for runtime in flipped:
        _record_offline_event(ctx, runtime)

    close_finished_contests(ctx)

    dirty = ctx.registry.take_dirty()
    if dirty:
        flush_statuses(ctx, dirty)
    return len(dirty)


def flush_statuses(ctx: AppContext, runtimes: Iterable[AgentRuntime]) -> None:
    """把若干运行时状态 upsert 进 ``agent_status``，合并为一个事务。"""
    now = utcnow()
    with ctx.db.session() as session:
        ids = [r.agent_id for r in runtimes]
        existing = {
            row.agent_id: row
            for row in session.execute(
                select(AgentStatus).where(AgentStatus.agent_id.in_(ids))
            ).scalars()
        }
        for runtime in runtimes:
            row = existing.get(runtime.agent_id)
            if row is None:
                row = AgentStatus(agent_id=runtime.agent_id)
                session.add(row)
            row.online = runtime.online
            row.last_tick_at = runtime.last_tick_at
            row.last_seen_ip = runtime.last_seen_ip
            row.agent_version = runtime.agent_version
            row.scan_root = runtime.scan_root
            row.file_count = runtime.file_count
            row.disk_free = runtime.disk_free
            row.last_error = runtime.last_error
            row.updated_at = now


def _record_offline_event(ctx: AppContext, runtime: AgentRuntime) -> None:
    with ctx.db.session() as session:
        session.add(
            EventLog(
                level="warning",
                category="offline",
                contest_id=runtime.contest_id,
                player_id=runtime.player_id,
                agent_id=runtime.agent_id,
                message="选手 %s 离线（超过 %d 秒无 tick）"
                % (runtime.player_no, ctx.settings.offline_after_seconds),
                meta_json=None,
            )
        )
