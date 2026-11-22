"""评测机（worker）接口（§12）：领任务、取内容、交结果。

凭据是管理端签发的评测机令牌（``/admin/judge-workers``），库里只存哈希。它只能做这里的
四件事：领任务、取**这个任务**要用的两份内容（代码、数据包）、交结果、报自己出错。
碰不到数据库、私钥，也读不到别的选手的代码。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response

from ..context import AppContext
from ..errors import ApiError
from ..models import JudgeWorker, utcnow
from ..schemas import JudgeFailIn, JudgeLeaseOut, JudgeResultIn, SimpleAck
from ..security import hash_token
from ..services import builtin_judge
from .deps import _bearer, get_ctx

router = APIRouter(prefix="/api/v1/judge", tags=["judge"])

#: 交回的结果里，除了三项成绩之外原样落进 result.json 的键
DETAIL_KEYS = ("judge", "isolated", "language", "compile_message", "cases", "subtasks", "precheck_warnings")


def require_judge_worker(request: Request, ctx: AppContext = Depends(get_ctx)) -> int:
    token_hash = hash_token(_bearer(request))
    with ctx.db.session() as session:
        worker = session.query(JudgeWorker).filter(JudgeWorker.token_hash == token_hash).one_or_none()
        if worker is None:
            raise ApiError(401, "unauthorized", "评测机令牌无效（被吊销了，或者抄错了）")
        worker.last_seen_at = utcnow()
        worker.last_seen_ip = request.client.host if request.client else None
        return worker.id


@router.post("/lease", response_model=JudgeLeaseOut, responses={204: {"description": "没有待评的任务"}})
def lease(ctx: AppContext = Depends(get_ctx), worker_id: int = Depends(require_judge_worker)):
    """领下一个任务；没有就 204。租约 15 分钟，过期没交结果的任务自动回到队列。"""
    leased = builtin_judge.lease_next(ctx, worker_id)
    if leased is None:
        return Response(status_code=204)
    return JudgeLeaseOut(**leased.to_payload())


@router.get("/tasks/{task_id}/blobs/{sha256}")
def task_blob(
    task_id: int,
    sha256: str,
    ctx: AppContext = Depends(get_ctx),
    worker_id: int = Depends(require_judge_worker),
):
    """取任务要用的内容：只认这个任务的代码与它那道题的数据包。"""
    with ctx.db.session() as session:
        task = builtin_judge.leased_task(session, task_id, worker_id)
        if task is None:
            raise ApiError(404, "not_found", "任务不存在、不是你领的，或者已经结束")
        from ..models import Problem
        from sqlalchemy import select

        testdata = session.execute(
            select(Problem.testdata_sha256).where(
                Problem.contest_id == task.contest_id, Problem.ident == task.problem
            )
        ).scalar_one_or_none()
        allowed = {task.source_sha256, testdata}
    if sha256 not in allowed or not ctx.blobs.has(sha256):
        raise ApiError(404, "not_found", "这份内容不属于这个任务")
    return FileResponse(str(ctx.blobs.path_for(sha256)), media_type="application/octet-stream")


@router.post("/tasks/{task_id}/result", response_model=SimpleAck)
def submit_result(
    task_id: int,
    payload: JudgeResultIn,
    ctx: AppContext = Depends(get_ctx),
    worker_id: int = Depends(require_judge_worker),
) -> SimpleAck:
    """交结果：写进 ``judge_result/<场次>/<选手>/<题目>/result.json``，扫描器随后收进矩阵。

    任务已被重测替换掉（或者不是你领的）时 404：那份结果对应的代码可能已经变了，丢掉。
    """
    document = {"score": payload.score, "max_score": payload.max_score, "status": payload.status}
    for key in DETAIL_KEYS:
        if key in payload.detail:
            document[key] = payload.detail[key]
    if not builtin_judge.complete(ctx, task_id, worker_id, document):
        raise ApiError(404, "not_found", "任务不存在、不是你领的，或者已经被重测替换")
    return SimpleAck(ok=True, detail="已记录")


@router.post("/tasks/{task_id}/fail", response_model=SimpleAck)
def report_failure(
    task_id: int,
    payload: JudgeFailIn,
    ctx: AppContext = Depends(get_ctx),
    worker_id: int = Depends(require_judge_worker),
) -> SimpleAck:
    """评测机自己出错（沙箱起不来、checker 编译不过……）：退回队列，三次都失败就记失败。"""
    if not builtin_judge.fail(ctx, task_id, worker_id, payload.message):
        raise ApiError(404, "not_found", "任务不存在、不是你领的，或者已经结束")
    return SimpleAck(ok=True, detail="已退回队列")
