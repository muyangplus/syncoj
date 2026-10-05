"""下发任务 → 下发作业的转换。

服务端在这里决定"这台机器现在应该下载什么、从哪个字节开始"。客户端只负责执行。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, DeployStatus, DeployTarget, DeployTask
from ..schemas import DeployJob

__all__ = ["collect_deploy_jobs", "mark_ready"]

log = logging.getLogger(__name__)

#: 这些状态的作业需要下发给 Agent
ACTIVE_STATUSES = (DeployStatus.PENDING, DeployStatus.READY)

AGENT_ASSET_URL = "/api/v1/agent/assets/%d"


def collect_deploy_jobs(
    session: Session,
    player_id: int,
    partials: Dict[int, int],
) -> List[DeployJob]:
    """算出该选手当前待完成的下发作业。

    ``partials`` 是客户端上报的 ``{asset_id: 已下载字节数}``，用来回填 ``offset``。
    服务端不保存下载进度 —— 进度是客户端的本地事实，服务端只做记录与汇总，
    这样 Agent 重装/快照还原后也不会因为"服务端以为已经传了一半"而错位。
    """
    rows: Sequence[Tuple[DeployTarget, DeployTask, Asset]] = list(
        session.execute(
            select(DeployTarget, DeployTask, Asset)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .join(Asset, DeployTask.asset_id == Asset.id)
            .where(
                DeployTarget.player_id == player_id,
                DeployTarget.status.in_(ACTIVE_STATUSES),
            )
            .order_by(DeployTarget.id)
        )
    )

    jobs: List[DeployJob] = []
    for target, task, asset in rows:
        offset = int(partials.get(asset.id, 0) or 0)
        if offset < 0 or offset > asset.size:
            # 客户端报了个不可能的偏移量，从头发
            offset = 0
        if offset == asset.size:
            # 内容已完整下载但尚未上报结果，不再重复下发
            continue

        dest = _join_dest(task.dest_dir, asset.filename)
        jobs.append(
            DeployJob(
                asset_id=asset.id,
                url=AGENT_ASSET_URL % asset.id,
                sha256=asset.sha256,
                size=int(asset.size),
                dest=dest,
                offset=offset,
                mode=task.mode,
            )
        )
        # 标记为 READY 表示"已经交给客户端了"，便于 Web 区分"待调度"与"进行中"
        if target.status == DeployStatus.PENDING:
            target.status = DeployStatus.READY

    return jobs


def _join_dest(dest_dir: str, filename: str) -> str:
    """拼出下发目标相对路径。两边都已由调用方保证是合法相对路径。"""
    base = (dest_dir or "").strip().strip("/")
    name = (filename or "").strip().lstrip("/")
    if not base:
        return name
    return "%s/%s" % (base, name)
