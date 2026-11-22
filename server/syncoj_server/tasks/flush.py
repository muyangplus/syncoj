"""后台任务。

只做两件事，都是"必须有，但不该挂在请求路径上"的：

1. **离线判定**。""不再 tick"" 这件事本身不产生任何事件，所以只能靠定时扫描。
   （曾经还有第三件"把内存注册表批量刷进 ``agent_status``"——那张表从来没人读，
   2026-11 连表一起删了，见迁移 017。）
2. **到点自动结束场次**。配了 ``ends_at`` 的场次时间一到就置成"已结束"并留一条
   审计事件。**收不收代码不靠它**（那条判据在 ``api/agent.py`` 的上传门禁里，
   因为循环是周期跑的，"到点那一刻"不能靠等它）—— 它管的是**状态**：界面该显示
   "已结束"，机器也不该再被解析成一个还在跑的场次。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from ..context import AppContext
from ..models import Contest, ContestStatus, EventLog, local_clock, utcnow
from ..registry import AgentRuntime
from ..services import runtime_settings

__all__ = [
    "run_maintenance_loop",
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
                    Contest.status.notin_((ContestStatus.CLOSED, ContestStatus.ARCHIVED)),
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
    """执行一轮：判定离线 -> 自动结束到点的场次。返回本轮刚转为离线的机器数。

    离线阈值**每轮现读**运行参数：它是可以在管理端改的，缓存一份就等于
    "改完得重启服务端"。
    """
    with ctx.db.session() as session:
        offline_after = runtime_settings.load(session).offline_after_seconds

    flipped: List[AgentRuntime] = ctx.registry.sweep_offline(offline_after)
    for runtime in flipped:
        _record_offline_event(ctx, runtime, offline_after)

    close_finished_contests(ctx)

    return len(flipped)


def _record_offline_event(
    ctx: AppContext, runtime: AgentRuntime, offline_after_seconds: int
) -> None:
    with ctx.db.session() as session:
        session.add(
            EventLog(
                level="warning",
                category="offline",
                contest_id=runtime.contest_id,
                player_id=runtime.player_id,
                agent_id=runtime.agent_id,
                message="选手 %s 离线（超过 %d 秒无 tick）"
                % (runtime.player_no, offline_after_seconds),
                meta_json=None,
            )
        )
