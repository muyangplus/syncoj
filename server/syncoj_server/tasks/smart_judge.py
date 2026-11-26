"""智能自动评测的后台循环（§12.12）：每 ``smart_tick_seconds`` 评估一轮。

判定规则全在 ``services/smart_judge.py`` 的纯函数里；这里只管节奏，并保证一轮出错不会把
主进程带崩。
"""

from __future__ import annotations

import asyncio
import logging

from ..context import AppContext
from ..services.smart_judge import run_round

__all__ = ["run_smart_judge_loop"]

log = logging.getLogger(__name__)


async def run_smart_judge_loop(ctx: AppContext) -> None:
    interval = max(5.0, ctx.settings.smart_tick_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.get_event_loop().run_in_executor(None, run_round, ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 后台任务绝不能把主进程带崩
            log.exception("智能评测评估失败，将在下一周期重试")
