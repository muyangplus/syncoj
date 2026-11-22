"""管理端：归档 / 取消归档场次（§4.9）。

归档只改状态，数据一行不动：归档的场次仍可查看、导出，只是不再参与任何自动流程
（机器动态解析、成绩扫描、自带评测排队），场次下拉默认不显示它。可逆，所以不要求
输名称确认；但**进行中 / 封榜**的场次要先结束 —— 那时机器还在往里交代码，直接归档
等于在考试中途把整间机房切断。
"""

from __future__ import annotations

from fastapi import Depends
from fastapi import APIRouter
from sqlalchemy import func, select, update

from ..context import AppContext
from ..errors import ApiError
from ..models import Contest, ContestStatus, EventLog, JudgeTask, Player, Roster, utcnow
from ..schemas import ContestOut
from .admin import AdminIdentity, _contest_out, require_admin
from .deps import get_ctx

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def require_archivable(contest: Contest) -> None:
    """进行中 / 封榜的场次不能直接归档（409 ``contest_running``）。PATCH 改状态也走这里。"""
    if contest.status in (ContestStatus.RUNNING, ContestStatus.FROZEN):
        raise ApiError(
            409,
            "contest_running",
            "场次「%s」还在进行，先结束再归档" % contest.name,
            {"status": contest.status},
        )


def drop_queued_judge_tasks(session, contest_id: int) -> int:
    """归档时把还在排队的自带评测任务了结（标成失败、写明原因），评测机不再领它们。"""
    return int(
        session.execute(
            update(JudgeTask)
            .where(JudgeTask.contest_id == contest_id, JudgeTask.status == "queued")
            .values(status="failed", error="场次已归档", finished_at=utcnow())
        ).rowcount
        or 0
    )


def _out(session, ctx: AppContext, contest: Contest) -> ContestOut:
    players = session.execute(
        select(func.count(Player.id)).where(Player.contest_id == contest.id)
    ).scalar_one()
    roster = session.get(Roster, contest.default_roster_id) if contest.default_roster_id else None
    online = sum(1 for a in ctx.registry.all(contest.id) if a.online)
    return _contest_out(contest, players, online, roster=roster)


@router.post("/contests/{contest_id}/archive", response_model=ContestOut)
def archive_contest(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ContestOut:
    """归档场次。已归档时是空操作。"""
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise ApiError(404, "not_found", "场次不存在")
        if contest.status != ContestStatus.ARCHIVED:
            require_archivable(contest)
            contest.status = ContestStatus.ARCHIVED
            dropped = drop_queued_judge_tasks(session, contest.id)
            session.add(
                EventLog(
                    level="info",
                    category="contest_archive",
                    contest_id=contest.id,
                    message="归档场次「%s」%s"
                    % (contest.name, "，撤下排队中的评测 %d 个" % dropped if dropped else ""),
                )
            )
            session.flush()
        return _out(session, ctx, contest)


@router.post("/contests/{contest_id}/unarchive", response_model=ContestOut)
def unarchive_contest(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ContestOut:
    """取消归档：回到「已结束」。不是已归档时是空操作。"""
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise ApiError(404, "not_found", "场次不存在")
        if contest.status == ContestStatus.ARCHIVED:
            contest.status = ContestStatus.CLOSED
            session.add(
                EventLog(
                    level="info",
                    category="contest_unarchive",
                    contest_id=contest.id,
                    message="取消归档场次「%s」，回到已结束" % contest.name,
                )
            )
            session.flush()
        return _out(session, ctx, contest)
