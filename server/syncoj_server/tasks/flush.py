"""后台任务。

只做两件事，都是"必须有，但不该挂在请求路径上"的：

1. **离线判定**。""不再 tick"" 这件事本身不产生任何事件，所以只能靠定时扫描。
2. **批量落库**。把内存注册表里的脏数据合并成**一个事务**刷入 ``agent_status``。
   这是让 SQLite 在 50 台机器持续心跳下不出现写锁争抢的关键 —— 写频率从
   "每请求一次" 降到 "每 5 秒一次"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, List

from sqlalchemy import select

from ..context import AppContext
from ..models import AgentStatus, EventLog, utcnow
from ..registry import AgentRuntime

__all__ = ["run_maintenance_loop", "flush_statuses", "flush_once"]

log = logging.getLogger(__name__)


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
    """执行一轮：判定离线 -> 落库。返回本轮落库条数。"""
    flipped: List[AgentRuntime] = ctx.registry.sweep_offline()
    for runtime in flipped:
        _record_offline_event(ctx, runtime)

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
