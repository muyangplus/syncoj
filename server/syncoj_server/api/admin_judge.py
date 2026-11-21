"""管理端：自带评测（§12）—— 题目评测配置、数据包、排任务与进度、评测机令牌。

``api/admin.py`` 已经很长，评测相关的接口单独放在这里，路径仍在 ``/api/v1/admin`` 下。
"""

from __future__ import annotations

import json
import re
import zipfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import select

from ..context import AppContext
from ..errors import ApiError
from ..judging.testdata import TestdataError, parse_testdata
from ..models import Contest, EventLog, JudgeWorker, Player, Problem, utcnow
from ..schemas import (
    BuiltinJudgeIn,
    BuiltinJudgeProgressOut,
    BuiltinJudgeQueuedOut,
    JudgeWorkerCreatedOut,
    JudgeWorkerIn,
    JudgeWorkerOut,
    Page,
    ProblemJudgeIn,
    ProblemOut,
    SimpleAck,
    slice_page,
)
from ..security import hash_token, new_token
from ..storage import BlobTooLarge
from ..services import builtin_judge
from .admin import AdminIdentity, _iso, _problem_out, _reclaim_blobs, require_admin, require_confirm
from .deps import PageParams, get_ctx

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

#: 编译参数白名单：语言标准、优化、宏、警告、-f 开关（插件除外）、-g、-static、-m。
#: 编译虽然也在沙箱里跑，但 -o / -I / -B / -specs / -wrapper / -fplugin 这类能改变"编译谁、
#: 用什么编译、输出到哪"的参数没有理由出现在一道题的配置里。
_FLAG_RE = re.compile(r"^-(std=[a-z0-9+]+|O[0-3s]?|D[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_.]*)?|W[a-z0-9-]*|f(?!plugin)[a-z0-9-]+(=[a-z0-9-]+)?|g|static|m[a-z0-9-]+(=[a-z0-9-]+)?)$")
MAX_TESTDATA_BYTES = 512 * 1024 * 1024


def _validate_flags(raw: Optional[str]) -> Optional[str]:
    if not raw or not raw.strip():
        return None
    tokens = raw.split()
    bad = [token for token in tokens if not _FLAG_RE.match(token)]
    if bad:
        raise ApiError(400, "validation_error", "不允许的编译参数：%s" % " ".join(bad[:5]))
    return " ".join(tokens)


def _load_problem(session, problem_id: int) -> Problem:
    row = session.get(Problem, problem_id)
    if row is None:
        raise ApiError(404, "not_found", "题目不存在")
    return row


@router.put("/problems/{problem_id}/judge", response_model=ProblemOut)
def set_problem_judge(
    problem_id: int,
    payload: ProblemJudgeIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ProblemOut:
    """题目的评测方式：外部评测器 / 自带评测，以及时限、内存、IO 方式、编译参数。"""
    flags = _validate_flags(payload.compile_flags)
    with ctx.db.session() as session:
        row = _load_problem(session, problem_id)
        row.judge_mode = payload.judge_mode
        row.time_limit_ms = payload.time_limit_ms
        row.memory_limit_mb = payload.memory_limit_mb
        row.io_mode = payload.io_mode
        row.compile_flags = flags
        session.flush()
        return _problem_out(row, ctx.settings)


@router.post("/problems/{problem_id}/testdata", response_model=ProblemOut)
def upload_testdata(
    problem_id: int,
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ProblemOut:
    """上传数据包（zip）。**上传时就校验结构**：缺答案、路径不安全、配置写错当场报出来。"""
    try:
        sha256, size = ctx.blobs.put_stream(file.file, max_bytes=MAX_TESTDATA_BYTES)
    except BlobTooLarge as exc:
        raise ApiError(413, "payload_too_large", str(exc))
    try:
        with zipfile.ZipFile(str(ctx.blobs.path_for(sha256))) as archive:
            summary = parse_testdata(archive).summary()
    except (zipfile.BadZipFile, TestdataError) as exc:
        _reclaim_blobs(ctx, [sha256])
        message = "不是合法的 zip" if isinstance(exc, zipfile.BadZipFile) else str(exc)
        raise ApiError(400, "validation_error", "数据包不对：%s" % message)
    with ctx.db.session() as session:
        row = _load_problem(session, problem_id)
        old = row.testdata_sha256
        row.testdata_sha256 = sha256
        row.testdata_name = (file.filename or "testdata.zip")[:255]
        row.testdata_size = size
        row.testdata_info = json.dumps(summary)
        session.flush()
        out = _problem_out(row, ctx.settings)
    if old and old != sha256:
        _reclaim_blobs(ctx, [old])
    return out


@router.delete("/problems/{problem_id}/testdata", response_model=ProblemOut)
def delete_testdata(
    problem_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ProblemOut:
    with ctx.db.session() as session:
        row = _load_problem(session, problem_id)
        old = row.testdata_sha256
        row.testdata_sha256 = row.testdata_name = row.testdata_info = None
        row.testdata_size = None
        session.flush()
        out = _problem_out(row, ctx.settings)
    _reclaim_blobs(ctx, [old])
    return out


@router.post("/contests/{contest_id}/judge/builtin", response_model=BuiltinJudgeQueuedOut)
def queue_builtin_judge(
    contest_id: int,
    payload: BuiltinJudgeIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BuiltinJudgeQueuedOut:
    """排自带评测：指定的题（或全部用自带评测的题）× 指定的选手（或全部选手）。

    场次还在进行时拒绝（``contest_in_progress``）：那时评的是考到一半的代码；教师确认后带
    ``force`` 再来。已有的任务（不管什么状态）会被替换 —— 重测就是重新排。
    """
    now = utcnow()
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise ApiError(404, "not_found", "场次不存在")
        if builtin_judge.contest_in_progress(contest, now) and not payload.force:
            raise ApiError(409, "contest_in_progress", "场次还在进行：现在评测的是考到一半的代码")
        stmt = select(Problem).where(Problem.contest_id == contest_id)
        if payload.problem:
            stmt = stmt.where(Problem.ident == payload.problem)
        problems = [row for row in session.execute(stmt).scalars() if builtin_judge.is_builtin(row)]
        missing = [row.ident for row in problems if not row.testdata_sha256]
        if missing:
            raise ApiError(400, "validation_error", "这些题还没有数据包：%s" % "、".join(missing))
        if not problems:
            raise ApiError(400, "validation_error", "没有设成自带评测的题目")
        players = select(Player.id).where(Player.contest_id == contest_id)
        if payload.player_id is not None:
            players = players.where(Player.id == payload.player_id)
        player_ids = list(session.execute(players).scalars())
        idents = [row.ident for row in problems]
        queued = builtin_judge.enqueue(session, contest_id, idents, player_ids)
        session.add(EventLog(
            level="info", category="judge_queue", contest_id=contest_id,
            message="排自带评测 %d 个任务（%s）" % (queued, "、".join(idents)),
        ))
    return BuiltinJudgeQueuedOut(queued=queued, problems=idents)


@router.get("/contests/{contest_id}/judge/builtin", response_model=BuiltinJudgeProgressOut)
def builtin_judge_progress(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BuiltinJudgeProgressOut:
    """自带评测的进度：排队 / 在评 / 完成 / 失败，以及失败的原因。"""
    with ctx.db.session() as session:
        return BuiltinJudgeProgressOut(**builtin_judge.progress(session, contest_id))


def _worker_out(row: JudgeWorker) -> JudgeWorkerOut:
    return JudgeWorkerOut(
        id=row.id, name=row.name, created_at=_iso(row.created_at),
        last_seen_at=_iso(row.last_seen_at), last_seen_ip=row.last_seen_ip,
    )


@router.get("/judge-workers", response_model=Page[JudgeWorkerOut])
def list_judge_workers(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[JudgeWorkerOut]:
    with ctx.db.session() as session:
        rows = session.execute(select(JudgeWorker).order_by(JudgeWorker.id)).scalars()
        return slice_page([_worker_out(row) for row in rows], page)


@router.post("/judge-workers", response_model=JudgeWorkerCreatedOut)
def create_judge_worker(
    payload: JudgeWorkerIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeWorkerCreatedOut:
    """签发一台评测机的令牌。明文只在这一次返回，库里只存哈希。"""
    token = new_token()
    with ctx.db.session() as session:
        row = JudgeWorker(name=payload.name.strip(), token_hash=hash_token(token))
        session.add(row)
        session.flush()
        out = _worker_out(row)
    return JudgeWorkerCreatedOut(**out.model_dump(), token=token)


@router.delete("/judge-workers/{worker_id}", response_model=SimpleAck)
def delete_judge_worker(
    worker_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入评测机名称"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """吊销一台评测机（删行）。它手上没交的任务租约到期后回到队列。"""
    with ctx.db.session() as session:
        row = session.get(JudgeWorker, worker_id)
        if row is None:
            return SimpleAck(ok=True, detail="没有这台评测机")
        require_confirm(row.name, confirm, "评测机名称")
        session.delete(row)
    return SimpleAck(ok=True, detail="已吊销评测机")
