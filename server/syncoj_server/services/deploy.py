"""下发任务 → 下发作业的转换。

服务端在这里决定"这台机器现在应该下载什么、从哪个字节开始"。客户端只负责执行。
"""

from __future__ import annotations

import logging
import re
from typing import List, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, DeployStatus, DeployTarget, DeployTask
from ..paths import PathValidationError, validate_relpath
from ..schemas import DeployJob

__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "apply_deploy_reports",
    "collect_deploy_jobs",
    "expand_dest_template",
    "validate_dest_template",
    "DEST_PLAYER_TOKEN",
]

log = logging.getLogger(__name__)

#: 这些状态的作业需要下发给 Agent
ACTIVE_STATUSES = (DeployStatus.PENDING, DeployStatus.READY)

AGENT_ASSET_URL = "/api/v1/agent/assets/%d"

#: 下发目标目录模板里**唯一**支持的占位符。
#:
#: ``{player_no}`` 由**服务端**按目标选手逐个展开 —— 不能交给 Agent，
#: 因为"全员下发"时每台机器的目标目录都不同，而 Agent 只知道自己的编号，
#: 不知道这次下发是给谁的。
DEST_PLAYER_TOKEN = "{player_no}"

#: 模板里任何 ``{...}`` 形状的东西（不要求合理，因为要拦的正是"教师以为它会被展开"）。
_ANY_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
#: 唯一允许和必须拦下的两个形状。
_PLAYER_PLACEHOLDER = re.compile(r"\{player_no\}")
_UNKNOWN_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


def _find_unknown_placeholder(template: str) -> str:
    """模板里第一个除 ``{player_no}`` 以外的占位符；没有就返回空串。

    只看花括号里不是空白的那些：``{}`` 是空名（多半是笔误），留给下面的路径
    规则报；``{  }`` 同理。有名字的才值得单独报一句"它不会被展开"。
    """
    for match in _UNKNOWN_PLACEHOLDER.finditer(template):
        token = match.group(0)
        if token == DEST_PLAYER_TOKEN:
            continue
        return token
    return ""


def expand_dest_template(template: str, player_no: str) -> str:
    """展开下发目标目录模板。"""
    return (template or "").replace(DEST_PLAYER_TOKEN, player_no)


def validate_dest_template(raw: str) -> str:
    """校验并归一化下发目录模板，返回可入库的形式。

    只有 ``{player_no}`` 一个占位符。**其他任何 ``{...}`` 一律拒绝** ——
    从前的 ``{problem}`` 就是反例：它被校验放行，而 :func:`expand_dest_template`
    并不认识它，于是机器上真的会多出一个名叫 ``{problem}`` 的目录，全程没有任何
    报错。这种"安静地做错事"比报错难查得多，所以宁可在这里拒得干脆一点。

    校验结构时先把 ``{player_no}`` 换成一个合法样例再走路径规则 —— 否则
    ``{player_no}/../x`` 这种"看起来有占位符"的写法会被当成普通字符放过去。

    返回的是**原始模板**（只归一化末尾斜杠），不是替换后的样例：
    模板要入库，展开是下发那一刻才做的事。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""

    # 不认识的占位符先单独报 —— 它比"路径不合法"具体得多，而且不拦的话
    # 它会原样变成一个目录名（见上面那段）。
    unknown = _find_unknown_placeholder(text)
    if unknown:
        raise ValueError(
            "目标目录只认 {player_no} 这一个占位符，不会展开 %s；"
            "它到机器上会原样变成一个目录名。"
            "要按题分发请自己填 {player_no}/<题目名> 这类目录" % unknown
        )

    probe = _ANY_PLACEHOLDER.sub("player0", text)
    try:
        validate_relpath(probe, max_length=512)
    except PathValidationError as exc:
        raise ValueError("目标目录不合法: %s" % exc)
    return text


#: 连续这么多次下载失败就判 FAILED、不再派发（管理端可以手工重试）。
MAX_CONSECUTIVE_FAILURES = 5


def collect_deploy_jobs(
    session: Session,
    player_id: int,
    player_no: str,
) -> List[DeployJob]:
    """算出该选手当前待完成的下发作业。

    **不再回填续传偏移**：续传位置以 Agent 本地分片为准（分片按内容 sha256 命名，
    内容换了自然从头来）。曾经服务端按上报的偏移在"分片已满长"时**跳过派发**，
    而 Agent 只在收到作业时才做最后一步落盘 —— 落盘前崩溃 / 改名失败过一次，
    这个目标就永远卡在进行中，管理端也看不到任何错误。

    已取消任务下的目标不派发（目标自身不在 pending/ready 也不派发）。

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
                DeployTask.cancelled_at.is_(None),
            )
            .order_by(DeployTarget.id)
        )
    )

    jobs: List[DeployJob] = []
    for target, task, asset in rows:
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
                target_id=target.id,
                asset_id=asset.id,
                url=AGENT_ASSET_URL % asset.id,
                sha256=asset.sha256,
                size=int(asset.size),
                dest=dest,
                mode=task.mode,
            )
        )
        # 标记为 READY 表示"已经交给客户端了"，便于 Web 区分"待调度"与"进行中"
        if target.status == DeployStatus.PENDING:
            target.status = DeployStatus.READY

    return jobs


def apply_deploy_reports(
    session: Session,
    player_id: int,
    progress: Sequence[object],
    done: Sequence[object],
    failed: Sequence[object],
    now,
) -> List[int]:
    """把机器回报的进度 / 完成 / 失败写回下发目标，返回状态有变化的任务 id。

    只碰**这个选手自己的、还在派发中的**目标（pending/ready），别的一律忽略 ——
    回执里的 target_id 是机器给的，不可信。

    * 完成：只认 ``sha256`` 与资产**当前**内容一致的回执。教师在线改了正文之后，
      迟到的旧版回执不能把新内容标成已完成（曾经按 asset_id、不看内容就标）。
    * 失败：记原因、连续失败计数 +1，到 :data:`MAX_CONSECUTIVE_FAILURES` 判 FAILED。
    * 进度：更新 ``bytes_done``（管理端的进度条）；分片比上次记的长 = 有进展，
      连续失败计数清零（先于失败处理，所以"断在更远处"的那次失败只记 1）。
    """
    ids = {int(getattr(r, "target_id")) for r in list(progress) + list(done) + list(failed)}
    if not ids:
        return []
    rows = {
        target.id: (target, asset)
        for target, asset in session.execute(
            select(DeployTarget, Asset)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .join(Asset, DeployTask.asset_id == Asset.id)
            .where(
                DeployTarget.id.in_(sorted(ids)[:1000]),
                DeployTarget.player_id == player_id,
                DeployTarget.status.in_(ACTIVE_STATUSES),
            )
        ).all()
    }
    changed = set()

    for item in progress:
        hit = rows.get(int(item.target_id))
        if hit is not None:
            target = hit[0]
            reported = min(int(item.bytes_done), int(hit[1].size))
            if reported > int(target.bytes_done or 0):
                # 分片在长：断网续传是常态，不是"连续失败"。不清零的话，弱网下
                # 大文件每轮断一次、5 轮后被判 FAILED，再也不派发。
                target.failure_count = 0
            target.bytes_done = reported

    for item in done:
        hit = rows.get(int(item.target_id))
        if hit is None:
            continue
        target, asset = hit
        if str(item.sha256).lower() != asset.sha256:
            # 落地的是旧内容（或别的东西）：不算完成，下一轮照常派发新内容
            continue
        target.status = DeployStatus.DONE
        target.bytes_done = int(asset.size)
        target.last_error = None
        target.failure_count = 0
        target.updated_at = now
        changed.add(target.task_id)

    for item in failed:
        hit = rows.get(int(item.target_id))
        if hit is None or hit[0].status == DeployStatus.DONE:
            continue
        target = hit[0]
        target.failure_count = int(target.failure_count or 0) + 1
        target.last_error = (str(item.reason) or "下载失败")[:300]
        target.updated_at = now
        if target.failure_count >= MAX_CONSECUTIVE_FAILURES:
            target.status = DeployStatus.FAILED
            changed.add(target.task_id)

    return sorted(changed)


def _join_dest(dest_dir: str, filename: str) -> str:
    """拼出下发目标相对路径。两边都已由调用方保证是合法相对路径。"""
    base = (dest_dir or "").strip().strip("/")
    name = (filename or "").strip().lstrip("/")
    if not base:
        return name
    return "%s/%s" % (base, name)
