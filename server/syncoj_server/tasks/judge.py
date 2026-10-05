"""评测成绩的后台扫描。

为什么不做成"文件变化就触发"
------------------------------
inotify 监听评测输出目录看起来更实时，但引入三个问题：跨平台差异、目录被整体
替换时事件丢失、以及递归 watch 的 fd 开销。而成绩回写本身对延迟完全不敏感 ——
教师在 LemonLime 里点完"评测"，几秒后界面上出现成绩，和十几秒后出现，体验上
没有区别。周期性扫描因此是更划算的选择。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from ..context import AppContext
from ..models import Contest, ContestStatus
from ..services.judge import scan_contest_results

__all__ = ["run_judge_scan_loop", "scan_all_contests"]

log = logging.getLogger(__name__)


async def run_judge_scan_loop(ctx: AppContext) -> None:
    interval = max(2.0, ctx.settings.judge_scan_interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.get_event_loop().run_in_executor(None, scan_all_contests, ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 后台任务绝不能把主进程带崩
            log.exception("成绩扫描失败，将在下一周期重试")


def scan_all_contests(ctx: AppContext, force: bool = False) -> int:
    """扫描所有未归档场次的结果目录，返回更新的条数。"""
    updated = 0
    with ctx.db.session() as session:
        contests = list(
            session.execute(
                select(Contest).where(Contest.status != ContestStatus.CLOSED)
            ).scalars()
        )
        for contest in contests:
            try:
                report = scan_contest_results(session, ctx.settings, contest, force=force)
            except Exception as exc:  # pragma: no cover - 单个场次出错不影响其他
                log.exception("扫描场次 %s 的成绩失败: %s", contest.slug, exc)
                continue
            if report.parsed or report.unparsed:
                log.info(
                    "场次 %s 成绩扫描：解析 %d、未解析 %d、未变化 %d",
                    contest.slug, report.parsed, report.unparsed, report.unchanged,
                )
            updated += report.parsed + report.unparsed
    return updated
