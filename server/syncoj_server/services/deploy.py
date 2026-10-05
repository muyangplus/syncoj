"""下发任务 → 下发作业的转换。

服务端在这里决定"这台机器现在应该下载什么、从哪个字节开始"。客户端只负责执行。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, DeployStatus, DeployTarget, DeployTask
from ..paths import PathValidationError, validate_relpath
from ..schemas import DeployJob

__all__ = [
    "collect_deploy_jobs",
    "expand_dest_template",
    "validate_dest_template",
    "DEST_PLAYER_TOKEN",
    "DEST_PROBLEM_TOKEN",
]

log = logging.getLogger(__name__)

#: 这些状态的作业需要下发给 Agent
ACTIVE_STATUSES = (DeployStatus.PENDING, DeployStatus.READY)

AGENT_ASSET_URL = "/api/v1/agent/assets/%d"

#: 下发目标目录模板里支持的占位符。
#:
#: ``{player_no}`` 由**服务端**按目标选手逐个展开 —— 不能交给 Agent，
#: 因为"全员下发"时每台机器的目标目录都不同，而 Agent 只知道自己的编号，
#: 不知道这次下发是给谁的。
DEST_PLAYER_TOKEN = "{player_no}"
#: ``{problem}`` 保留给"按题目下发"的场景，由界面在建任务时替换成具体题目名，
#: 服务端不展开它（服务端不知道教师想发哪道题）。
DEST_PROBLEM_TOKEN = "{problem}"


def expand_dest_template(template: str, player_no: str) -> str:
    """展开下发目标目录模板。"""
    return (template or "").replace(DEST_PLAYER_TOKEN, player_no)


def validate_dest_template(raw: str) -> str:
    """校验并归一化下发目录模板，返回可入库的形式。

    校验时先把占位符换成一个合法样例再走路径规则 —— 否则 ``{player_no}/../x``
    这种"看起来有占位符"的写法会被当成普通字符放过去。

    返回的是**原始模板**（只归一化末尾斜杠），不是替换后的样例：
    模板要入库，展开是下发那一刻才做的事。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""

    probe = text.replace(DEST_PLAYER_TOKEN, "player0").replace(DEST_PROBLEM_TOKEN, "problem0")
    try:
        validate_relpath(probe, max_length=512)
    except PathValidationError as exc:
        raise ValueError("目标目录不合法: %s" % exc)
    return text


def collect_deploy_jobs(
    session: Session,
    player_id: int,
    player_no: str,
    partials: Dict[int, int],
) -> List[DeployJob]:
    """算出该选手当前待完成的下发作业。

    ``partials`` 是客户端上报的 ``{asset_id: 已下载字节数}``，用来回填 ``offset``。
    服务端不保存下载进度 —— 进度是客户端的本地事实，服务端只做记录与汇总，
    这样 Agent 重装/快照还原后也不会因为"服务端以为已经传了一半"而错位。

    ``player_no`` 用于展开 ``dest_dir`` 里的 ``{player_no}``：全员下发时每台机器的
    目标目录都不同（``桌面/<各自的准考证号>/…``），这个替换只能由服务端做。
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

        dest_dir = expand_dest_template(task.dest_dir, player_no)
        dest = _join_dest(dest_dir, asset.filename)

        # 展开之后再校验一次：模板本身在建任务时验过，但展开后的具体路径
        # 才是真正会落到客户端磁盘上的东西。多一道检查很便宜。
        try:
            dest = validate_relpath(dest, max_length=512)
        except Exception as exc:
            log.error(
                "下发目标路径不合法，跳过该作业 task=%s asset=%s dest=%r: %s",
                task.id, asset.id, dest, exc,
            )
            target.status = DeployStatus.FAILED
            target.last_error = "目标路径不合法: %s" % exc
            continue

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
