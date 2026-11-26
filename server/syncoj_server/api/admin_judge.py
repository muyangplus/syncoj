"""管理端：自带评测（§12）—— 题目评测配置、数据包、排任务与进度、评测机令牌。

``api/admin.py`` 已经很长，评测相关的接口单独放在这里，路径仍在 ``/api/v1/admin`` 下。
"""

from __future__ import annotations

import json
import re
import zipfile
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy import func, select

from ..context import AppContext
from ..errors import ApiError
from ..judging.testdata import TestdataError, parse_testdata
from ..models import (
    Contest,
    ContestStatus,
    EventLog,
    JudgeRun,
    JudgeTask,
    JudgeWorker,
    Player,
    Problem,
    SourceFile,
    SourceVersion,
    utcnow,
)
from ..schemas import (
    BuiltinJudgeIn,
    BuiltinJudgeProgressOut,
    BuiltinJudgeQueuedOut,
    JudgeAutoIn,
    JudgeAutoOut,
    JudgeServiceIn,
    JudgeServiceOut,
    JudgeSourceOut,
    JudgeTaskDetailOut,
    JudgeTaskOut,
    JudgeWorkerCreatedOut,
    JudgeWorkerIn,
    JudgeWorkerOut,
    JudgeWorkerPatchIn,
    LocalJudgeOut,
    Page,
    ProblemJudgeIn,
    ProblemOut,
    SimpleAck,
    SourceVersionOut,
    VersionJudgeOut,
    page_of,
    slice_page,
)
from ..security import hash_token, new_token
from ..storage import BlobTooLarge
from ..services import builtin_judge, judge_dist, judge_workers, smart_judge
from ..services.judge import SCAN_LOCK, scan_contest_results
from .admin import (
    AdminIdentity,
    FlagFilter,
    flag_condition,
    _iso,
    _problem_out,
    _reclaim_blobs,
    _result_details,
    require_admin,
    require_confirm,
)
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


#: 文件 IO 文件名：字母数字开头，只含字母数字 . _ -（不能带目录）
_IO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 沙箱工作目录里 runner 自己要用的文件名（见 judging/runner.py），选手的输入/输出不能占用。
#: input.txt / output.txt 只在标准 IO 时由 runner 使用，文件 IO 时可以当文件名
_RESERVED_IO_NAMES = frozenset({
    "prog", "main.c", "main.cpp", "compile.out", "compile.err",
    "chk", "checker.err", "chk.out", "chk.err",
    "in.txt", "out.txt", "ans.txt",
})


def _validate_io_name(raw: Optional[str], label: str) -> Optional[str]:
    """校验一个文件 IO 文件名；空 = 用默认名（返回 None）。"""
    if raw is None or not raw.strip():
        return None
    name = raw.strip()
    if not _IO_NAME_RE.match(name) or name in (".", ".."):
        raise ApiError(400, "validation_error", "%s不合法：只能用字母、数字、. _ -，且以字母或数字开头" % label)
    if name in _RESERVED_IO_NAMES:
        raise ApiError(400, "validation_error", "%s不能叫 %s（评测机自己要用）" % (label, name))
    return name


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
    """题目的评测方式：外部评测器 / 自带评测，以及时限、内存、IO 方式与文件名、编译参数。"""
    flags = _validate_flags(payload.compile_flags)
    input_file = _validate_io_name(payload.input_file, "输入文件名")
    output_file = _validate_io_name(payload.output_file, "输出文件名")
    with ctx.db.session() as session:
        row = _load_problem(session, problem_id)
        if (input_file or "%s.in" % row.ident) == (output_file or "%s.out" % row.ident):
            raise ApiError(400, "validation_error", "输入文件名和输出文件名不能相同")
        row.judge_mode = payload.judge_mode
        row.time_limit_ms = payload.time_limit_ms
        row.memory_limit_mb = payload.memory_limit_mb
        row.io_mode = payload.io_mode
        row.input_file = input_file
        row.output_file = output_file
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
    """排自带评测：指定的题（或全部用自带评测的题）× 指定的选手（或全部选手），评最新版。

    场次还在进行时拒绝（``contest_in_progress``）：那时评的是考到一半的代码；教师确认后带
    ``force`` 再来。同一对还在排队的最新版任务会被替换；跑完的留作历史（§12.9）。

    带 ``version_id`` = 重测那一个历史版本：结果只进历史与 timeline，不受场次进行中的限制。
    """
    now = utcnow()
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise ApiError(404, "not_found", "场次不存在")
        if contest.status == ContestStatus.ARCHIVED:
            raise ApiError(409, "contest_archived", "场次已归档，取消归档后再评测")
        if payload.version_id is not None:
            return _queue_version(ctx, session, contest, payload, admin)
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
        if payload.player_id is not None and not player_ids:
            raise ApiError(404, "not_found", "选手不在本场次")
        idents = [row.ident for row in problems]
        queued = builtin_judge.enqueue(
            session, contest_id, idents, player_ids,
            trigger=builtin_judge.MANUAL, requested_by=admin.username, rerun=payload.rerun,
        )
        session.add(EventLog(
            level="info", category="judge_queue", contest_id=contest_id,
            message="排自带评测 %d 个任务（%s）" % (queued, "、".join(idents)),
        ))
    return BuiltinJudgeQueuedOut(queued=queued, problems=idents)


def _queue_version(ctx: AppContext, session, contest: Contest, payload: BuiltinJudgeIn,
                   admin: AdminIdentity) -> BuiltinJudgeQueuedOut:
    version = session.get(SourceVersion, payload.version_id)
    player = session.get(Player, version.player_id) if version is not None else None
    if version is None or player is None or player.contest_id != contest.id:
        raise ApiError(404, "not_found", "这个版本不在本场次")
    if payload.player_id is not None and payload.player_id != version.player_id:
        raise ApiError(400, "validation_error", "版本不属于这位选手")
    if not ctx.blobs.has(version.sha256):
        raise ApiError(410, "gone", "这一版代码的内容已不在服务端")
    ident = payload.problem or builtin_judge.classify(
        session, contest.id, [version.rel_path], ctx.settings
    )[version.rel_path]
    problem = session.execute(
        select(Problem).where(Problem.contest_id == contest.id, Problem.ident == ident)
    ).scalar_one_or_none() if ident else None
    if problem is None or not builtin_judge.is_builtin(problem):
        raise ApiError(400, "validation_error", "这一版代码归不到自带评测的题目")
    if not problem.testdata_sha256:
        raise ApiError(400, "validation_error", "这些题还没有数据包：%s" % problem.ident)
    builtin_judge.enqueue_version(
        session, contest.id, version, problem.ident, requested_by=admin.username, rerun=payload.rerun,
    )
    session.add(EventLog(
        level="info", category="judge_queue", contest_id=contest.id,
        message="重测 %s「%s」的历史版本 %s（%s）"
        % (player.player_no, problem.ident, version.sha256[:8], version.rel_path),
    ))
    return BuiltinJudgeQueuedOut(queued=1, problems=[problem.ident])


@router.put("/contests/{contest_id}/judge/auto", response_model=JudgeAutoOut)
def set_auto_judge(
    contest_id: int,
    payload: JudgeAutoIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeAutoOut:
    """自动入队（§12.9）：``on_change`` = 收到某题的新版本就排评测，同一 (选手, 题目)
    在 ``auto_judge_delay`` 秒内连续改动只评最后一版。场次进行中也排。

    ``smart``（§12.12）= 判断"可能已经写完"才排（换题 / 空闲 / 将结束 / 掉线 / 已结束），
    阈值随请求改，不传的不改。"""
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise ApiError(404, "not_found", "场次不存在")
        contest.auto_judge = payload.auto_judge
        contest.auto_judge_delay = payload.auto_judge_delay
        for column in smart_judge.FIELDS:
            value = getattr(payload, column)
            if value is not None:
                setattr(contest, column, value)
        if payload.auto_judge == builtin_judge.AUTO_SMART:
            current = smart_judge.thresholds_of(contest)
            detail = "智能（稳定 %d 秒、空闲 %d 秒、结束前 %d 秒、冷却 %d 秒、队列倍数 %d）" % (
                current.stable, current.idle, current.near_end, current.cooldown, current.queue_factor)
        elif payload.auto_judge == builtin_judge.AUTO_ON_CHANGE:
            detail = "收到新版本即评（去抖 %d 秒）" % payload.auto_judge_delay
        else:
            detail = "关闭"
        session.add(EventLog(
            level="info", category="judge_auto", contest_id=contest_id, message="自动评测：%s" % detail,
        ))
        return JudgeAutoOut(
            auto_judge=contest.auto_judge, auto_judge_delay=contest.auto_judge_delay,
            **smart_judge.threshold_fields(contest),
        )


def _matrix_task_ids(session, contest_id: int, player_ids=None) -> Dict[Tuple[int, str], int]:
    """成绩矩阵当前格子来自哪个评测任务（自带评测写的 result.json 里带 ``task_id``）。"""
    stmt = select(JudgeRun.player_id, JudgeRun.problem, JudgeRun.raw_json, JudgeRun.parse_status).where(
        JudgeRun.contest_id == contest_id
    )
    if player_ids is not None:
        stmt = stmt.where(JudgeRun.player_id.in_(list(player_ids)))
    found = {}
    for player_id, problem, raw, parse_status in session.execute(stmt):
        if parse_status != "ok" or not raw or '"task_id"' not in raw:
            continue
        task_id = _result_details(raw).get("task_id")
        if task_id:
            found[(player_id, problem)] = task_id
    return found


def _task_out(task: JudgeTask, player_no: str, matrix: Dict[Tuple[int, str], int]) -> JudgeTaskOut:
    return JudgeTaskOut(
        id=task.id,
        player_id=task.player_id,
        player_no=player_no,
        problem=task.problem,
        status=task.status,
        trigger=builtin_judge.trigger_of(task),
        reason=task.reason,
        priority=int(task.priority or 0),
        requested_by=task.requested_by,
        source_version_id=task.source_version_id,
        source_path=task.source_path,
        source_sha256=task.source_sha256,
        score=task.score,
        max_score=task.max_score,
        outcome=task.outcome,
        error=task.error,
        reused_from=task.reused_from,
        rerun=bool(task.rerun),
        applied=task.applied_at is not None,
        in_matrix=matrix.get((task.player_id, task.problem)) == task.id,
        has_result=bool(task.result_json),
        created_at=_iso(task.created_at) or "",
        finished_at=_iso(task.finished_at),
        not_before=_iso(task.not_before),
        warning_count=int(task.warning_count or 0),
        risk_count=int(task.risk_count or 0),
    )


@router.get("/contests/{contest_id}/judge/tasks", response_model=Page[JudgeTaskOut])
def list_judge_tasks(
    contest_id: int,
    player_id: Optional[int] = None,
    problem: Optional[str] = Query(default=None, max_length=64),
    status: Optional[str] = Query(default=None, max_length=16),
    trigger: Optional[str] = Query(default=None, max_length=16),
    reason: Optional[str] = Query(default=None, max_length=16, description="智能模式的信号（§12.12）"),
    flag: Optional[FlagFilter] = Query(default=None, description="any / warning（有疑似违规）/ risk（有风险）"),
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[JudgeTaskOut]:
    """评测任务（新的在前）。任务即历史：每一次评测（含复用的）都在这里。

    ``flag``（§12.11）：``risk`` 有风险、``warning`` 有疑似违规（不论有没有风险）、``any`` 两者之一。
    ``reason``（§12.12）：智能模式的信号，配 ``trigger=smart`` 用。
    """
    conditions = [JudgeTask.contest_id == contest_id]
    if player_id is not None:
        conditions.append(JudgeTask.player_id == player_id)
    if problem:
        conditions.append(JudgeTask.problem == problem)
    if status:
        conditions.append(JudgeTask.status == status)
    if trigger:
        if trigger == builtin_judge.MANUAL:
            conditions.append(JudgeTask.trigger.is_(None) | (JudgeTask.trigger == trigger))
        else:
            conditions.append(JudgeTask.trigger == trigger)
    if reason:
        conditions.append(JudgeTask.reason == reason)
    flagged = flag_condition(JudgeTask.warning_count, JudgeTask.risk_count, flag)
    if flagged is not None:
        conditions.append(flagged)
    with ctx.db.session() as session:
        total = session.execute(select(func.count(JudgeTask.id)).where(*conditions)).scalar_one()
        rows = session.execute(
            select(JudgeTask, Player.player_no)
            .join(Player, JudgeTask.player_id == Player.id)
            .where(*conditions)
            .order_by(JudgeTask.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        ).all()
        matrix = _matrix_task_ids(session, contest_id, {task.player_id for task, _ in rows})
        items = [_task_out(task, player_no, matrix) for task, player_no in rows]
    return page_of(items, total, page)


def _load_task(session, contest_id: int, task_id: int) -> Tuple[JudgeTask, str]:
    found = session.execute(
        select(JudgeTask, Player.player_no)
        .join(Player, JudgeTask.player_id == Player.id)
        .where(JudgeTask.id == task_id, JudgeTask.contest_id == contest_id)
    ).first()
    if found is None:
        raise ApiError(404, "not_found", "评测任务不存在")
    return found[0], found[1]


@router.get("/contests/{contest_id}/judge/tasks/{task_id}", response_model=JudgeTaskDetailOut)
def get_judge_task(
    contest_id: int,
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeTaskDetailOut:
    """一个评测任务及其结果的测试点明细（历史版本的结果从这里看）。"""
    with ctx.db.session() as session:
        task, player_no = _load_task(session, contest_id, task_id)
        matrix = _matrix_task_ids(session, contest_id, [task.player_id])
        base = _task_out(task, player_no, matrix).model_dump()
        details = _result_details(task.result_json)
        for key in ("source_sha256", "judged_source", "task_id"):
            details.pop(key, None)
        return JudgeTaskDetailOut(**base, **details)


#: 明细里「源码」区最多显示这么大的代码（选手代码远小于它；再大的多半是误收的数据文件）
MAX_SOURCE_VIEW_BYTES = 1024 * 1024


@router.get("/contests/{contest_id}/judge/tasks/{task_id}/source", response_model=JudgeSourceOut)
def get_judge_task_source(
    contest_id: int,
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeSourceOut:
    """这个评测任务评的那份代码（按任务记下的 sha256 从内容存储取；只读，≤ 1 MB）。

    明细里按行显示、把预检 / 风险标注的行高亮。还没选定代码（排队中）或内容已被清掉时 404，
    超过 1 MB 时 413。
    """
    with ctx.db.session() as session:
        task, _ = _load_task(session, contest_id, task_id)
        sha256, path = task.source_sha256, task.source_path
    if not sha256 or not ctx.blobs.has(sha256):
        raise ApiError(404, "not_found", "这次评测的代码不在了（还没领到，或内容已被清掉）")
    blob = ctx.blobs.path_for(sha256)
    size = blob.stat().st_size
    if size > MAX_SOURCE_VIEW_BYTES:
        raise ApiError(413, "payload_too_large", "代码 %d KB，超过 1 MB 不在页面上显示" % (size // 1024))
    content = blob.read_bytes().decode("utf-8", "replace")
    return JudgeSourceOut(task_id=task_id, path=path, sha256=sha256, size=size, content=content)


@router.post("/contests/{contest_id}/judge/tasks/{task_id}/adopt", response_model=JudgeTaskOut)
def adopt_judge_task(
    contest_id: int,
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeTaskOut:
    """采用此版本成绩：把这个任务的结果写成当前 ``result.json`` 并立即重扫，成绩矩阵这一格
    换成它。手工录入的格子不动（409 ``manual_score``，先清除手工成绩）。"""
    now = utcnow()
    with ctx.db.session() as session:
        task, player_no = _load_task(session, contest_id, task_id)
        if task.status != builtin_judge.DONE or not task.result_json:
            raise ApiError(409, "judge_task_no_result", "这个任务没有可采用的评测结果")
        manual = session.execute(
            select(JudgeRun.id).where(
                JudgeRun.contest_id == contest_id,
                JudgeRun.player_id == task.player_id,
                JudgeRun.problem == task.problem,
                JudgeRun.parse_status == "manual",
            )
        ).first()
        if manual is not None:
            raise ApiError(409, "manual_score", "这一格是手工录入的成绩，先清除再采用")
        builtin_judge.adopt(ctx, session, task, now)
        session.add(EventLog(
            level="info", category="judge_adopt", contest_id=contest_id,
            message="采用 %s「%s」评测任务 #%d 的成绩（%s/%s）"
            % (player_no, task.problem, task.id, task.score, task.max_score),
        ))
    with SCAN_LOCK, ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is not None:
            scan_contest_results(session, ctx.settings, contest)
    with ctx.db.session() as session:
        task, player_no = _load_task(session, contest_id, task_id)
        return _task_out(task, player_no, _matrix_task_ids(session, contest_id, [task.player_id]))


@router.get("/contests/{contest_id}/judge/versions", response_model=Page[SourceVersionOut])
def list_source_versions(
    contest_id: int,
    player_id: int = Query(..., description="选手"),
    problem: Optional[str] = Query(default=None, max_length=64, description="只看归到这道题的版本"),
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[SourceVersionOut]:
    """一位选手的代码版本历史（旧的在前），每版带最近一次评测结果 —— 分数 timeline 的数据。

    归题按**当前**题目模式现算（改了题目标识或模式也对得上）。一个人的版本数有界，在
    Python 里切页。
    """
    with ctx.db.session() as session:
        player = session.get(Player, player_id)
        if player is None or player.contest_id != contest_id:
            raise ApiError(404, "not_found", "选手不在本场次")
        versions = list(session.execute(
            select(SourceVersion)
            .where(SourceVersion.player_id == player_id)
            .order_by(SourceVersion.received_at, SourceVersion.id)
        ).scalars())
        idents = builtin_judge.classify(session, contest_id, [v.rel_path for v in versions], ctx.settings)
        if problem:
            versions = [v for v in versions if idents.get(v.rel_path) == problem]
        total = len(versions)
        chunk = versions[page.offset:page.offset + page.limit]

        live = {
            row.rel_path: row.sha256
            for row in session.execute(
                select(SourceFile).where(
                    SourceFile.player_id == player_id,
                    SourceFile.deleted_at.is_(None),
                    SourceFile.content_stored.is_(True),
                )
            ).scalars()
        }
        newest = {}
        for version in versions:
            newest[version.rel_path] = version.id
        tasks: Dict[int, List[JudgeTask]] = {}
        ids = [v.id for v in chunk]
        for start in range(0, len(ids), 500):
            for task in session.execute(
                select(JudgeTask)
                .where(JudgeTask.source_version_id.in_(ids[start:start + 500]))
                .order_by(JudgeTask.id)
            ).scalars():
                tasks.setdefault(task.source_version_id, []).append(task)
        matrix = _matrix_task_ids(session, contest_id, [player_id])
        matrix_ids = set(matrix.values())

        items = []
        for version in chunk:
            ident = idents.get(version.rel_path)
            mine = [task for task in tasks.get(version.id, []) if ident is None or task.problem == ident]
            judged = [task for task in mine if task.status == builtin_judge.DONE and task.result_json]
            chosen = (judged or mine or [None])[-1]
            items.append(SourceVersionOut(
                id=version.id,
                player_id=version.player_id,
                rel_path=version.rel_path,
                sha256=version.sha256,
                size=int(version.size or 0),
                received_at=_iso(version.received_at) or "",
                problem=ident,
                current=newest.get(version.rel_path) == version.id and live.get(version.rel_path) == version.sha256,
                tasks=len(mine),
                judge=None if chosen is None else VersionJudgeOut(
                    task_id=chosen.id,
                    status=chosen.status,
                    trigger=builtin_judge.trigger_of(chosen),
                    reason=chosen.reason,
                    score=chosen.score,
                    max_score=chosen.max_score,
                    outcome=chosen.outcome,
                    reused=chosen.reused_from is not None,
                    applied=chosen.applied_at is not None,
                    finished_at=_iso(chosen.finished_at),
                ),
                in_matrix=any(task.id in matrix_ids for task in mine),
            ))
    return page_of(items, total, page)


@router.get("/contests/{contest_id}/judge/builtin", response_model=BuiltinJudgeProgressOut)
def builtin_judge_progress(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BuiltinJudgeProgressOut:
    """自带评测的进度：排队 / 在评 / 完成 / 失败，以及失败的原因。"""
    with ctx.db.session() as session:
        return BuiltinJudgeProgressOut(**builtin_judge.progress(session, contest_id))


def _workers_out(session, rows: List[JudgeWorker]) -> List[JudgeWorkerOut]:
    now = utcnow()
    running = judge_workers.running_tasks(session, [row.id for row in rows])
    return [judge_workers.worker_out(row, now, running.get(row.id)) for row in rows]


def _load_worker(session, worker_id: int) -> JudgeWorker:
    row = session.get(JudgeWorker, worker_id)
    if row is None:
        raise ApiError(404, "not_found", "评测机不存在")
    return row


@router.get("/judge-service", response_model=JudgeServiceOut)
def judge_service_status(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeServiceOut:
    """「评测服务」开关与全部场次的排队 / 在评数（§12.10）。"""
    with ctx.db.session() as session:
        return judge_workers.service_out(session)


@router.put("/judge-service", response_model=JudgeServiceOut)
def set_judge_service(
    payload: JudgeServiceIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeServiceOut:
    """开 / 关评测服务。关 = 所有评测机都领不到新任务：手上的照常评完交回，排队的任务留着
    （自动入队也照常入队），打开后接着发。"""
    with ctx.db.session() as session:
        before = judge_workers.service_enabled(session)
        judge_workers.set_service_enabled(session, payload.enabled)
        if before != payload.enabled:
            session.add(EventLog(
                level="info", category="judge_service",
                message="评测服务：%s（%s）" % ("开启" if payload.enabled else "关闭", admin.username),
            ))
        return judge_workers.service_out(session)


@router.get("/judge-workers", response_model=Page[JudgeWorkerOut])
def list_judge_workers(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[JudgeWorkerOut]:
    """评测机与它们自报的状态、在线与否、手上正在评的任务（§12.10）。"""
    with ctx.db.session() as session:
        rows = list(session.execute(select(JudgeWorker).order_by(JudgeWorker.id)).scalars())
        return slice_page(_workers_out(session, rows), page)


@router.post("/judge-workers", response_model=JudgeWorkerCreatedOut)
def create_judge_worker(
    payload: JudgeWorkerIn,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeWorkerCreatedOut:
    """签发一台评测机的令牌。明文只在这一次返回，库里只存哈希；顺带给出一行安装命令。"""
    token = new_token()
    with ctx.db.session() as session:
        row = JudgeWorker(name=payload.name.strip(), token_hash=hash_token(token))
        session.add(row)
        session.flush()
        out = _workers_out(session, [row])[0]
    server = judge_dist.base_url(request.headers.get("host"), request.url.scheme, ctx.settings.http_port)
    linux, windows, manual = judge_dist.install_commands(server, token)
    return JudgeWorkerCreatedOut(
        **out.model_dump(), token=token, linux_command=linux, windows_command=windows, manual_command=manual,
    )


@router.patch("/judge-workers/{worker_id}", response_model=JudgeWorkerOut)
def update_judge_worker(
    worker_id: int,
    payload: JudgeWorkerPatchIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeWorkerOut:
    """改名；暂停（``enabled`` = false：领不到新任务，手上的照常评完）/ 恢复；改并发数
    （``slots``：随下一次 poll 下发，评测机评完手上的任务后以新的 ``--slots`` 重启）。

    改并发数：旧评测机（不认下发的并发数）409 ``judge_worker_unsupported``；后端不是 isolate
    而 ``slots`` > 1 时 409 ``judge_worker_slots``。
    """
    with ctx.db.session() as session:
        row = _load_worker(session, worker_id)
        if payload.slots is not None:
            problem = judge_workers.slots_problem(row, payload.slots)
            if problem == "unsupported":
                raise ApiError(409, "judge_worker_unsupported",
                               "这台评测机版本太旧，不能远程改并发数；请在那台机器上改 --slots 后重启")
            if problem == "backend":
                raise ApiError(409, "judge_worker_slots",
                               "%s 后端只能单并发：多个任务以同一个账号运行，能互相读写工作目录（仅 isolate 可以大于 1）"
                               % (row.backend or "未知"))
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise ApiError(400, "validation_error", "名称不能为空")
            row.name = name
        if payload.enabled is not None:
            row.enabled = payload.enabled
        if payload.slots is not None:
            row.desired_slots = payload.slots
        session.flush()
        return _workers_out(session, [row])[0]


def _command(ctx: AppContext, worker_id: int, command: str) -> JudgeWorkerOut:
    with ctx.db.session() as session:
        row = _load_worker(session, worker_id)
        if not judge_workers.remote_control(row):
            raise ApiError(409, "judge_worker_unsupported", "这台评测机版本太旧，收不到远程命令；请在那台机器上操作")
        if not judge_workers.is_online(row, utcnow()):
            raise ApiError(409, "judge_worker_offline", "评测机不在线")
        if row.desired == "uninstall":
            # 卸载命令不复位，不能被停止 / 重启盖掉；不想等了就直接吊销
            raise ApiError(409, "judge_worker_uninstalling", "评测机正在卸载")
        judge_workers.issue_command(row, command)
        session.flush()
        return _workers_out(session, [row])[0]


@router.post("/judge-workers/{worker_id}/stop", response_model=JudgeWorkerOut)
def stop_judge_worker(
    worker_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeWorkerOut:
    """停止：评测机下一次 poll 时取走命令，评完手上的任务后退出（systemd / 计划任务不会再拉起它）。"""
    return _command(ctx, worker_id, "stop")


@router.post("/judge-workers/{worker_id}/restart", response_model=JudgeWorkerOut)
def restart_judge_worker(
    worker_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeWorkerOut:
    """重启：评完手上的任务后以同样的参数重新启动自己（换上磁盘上的新程序）。"""
    return _command(ctx, worker_id, "restart")


@router.post("/judge-workers/{worker_id}/uninstall", response_model=SimpleAck)
def uninstall_judge_worker(
    worker_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入评测机名称"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """卸载：评测机下一次 poll 时取走命令，评完手上的任务、告别（服务端删行，令牌失效），
    再按安装方式删掉自己的服务 / 计划任务、程序、令牌与缓存（§12.10）。

    不在线 409 ``judge_worker_offline``；没报 ``uninstall`` 能力（旧评测机）409
    ``judge_worker_unsupported``。本机评测机没在跑时直接卸：删行、删 ``<数据目录>/judge-local``。
    评测机一直不回来时，照样可以直接吊销。
    """
    with ctx.db.session() as session:
        row = _load_worker(session, worker_id)
        require_confirm(row.name, confirm, "评测机名称")
        if row.is_local and not ctx.local_judge.running():
            released = judge_workers.forget_uninstalled(session, row, "local", admin.username)
            session.flush()
            local_now = True
        else:
            local_now = False
            if not row.is_local:
                if not judge_workers.remote_control(row) or not judge_workers.uninstall_control(row):
                    raise ApiError(409, "judge_worker_unsupported",
                                   "这台评测机版本太旧，不能远程卸载；请先升级，或到那台机器上手动卸载")
                if not judge_workers.is_online(row, utcnow()):
                    raise ApiError(409, "judge_worker_offline", "评测机不在线")
            judge_workers.issue_command(row, "uninstall")
            session.flush()
    if local_now:
        ctx.local_judge.remove_files(ctx.settings)
        return SimpleAck(ok=True, detail="已卸载" + ("，%d 个任务已退回队列" % released if released else ""))
    return SimpleAck(ok=True, detail="已下令卸载")


@router.delete("/judge-workers/{worker_id}", response_model=SimpleAck)
def delete_judge_worker(
    worker_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入评测机名称"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """吊销一台评测机（删行）。它手上没交的任务立刻退回队列；本机评测机一并停掉。"""
    with ctx.db.session() as session:
        row = session.get(JudgeWorker, worker_id)
        if row is None:
            return SimpleAck(ok=True, detail="没有这台评测机")
        require_confirm(row.name, confirm, "评测机名称")
        local = bool(row.is_local)
        released = judge_workers.release_tasks(session, row.id)
        session.delete(row)
    if local:
        ctx.local_judge.stop()
    detail = "已吊销评测机" + ("，%d 个任务已退回队列" % released if released else "")
    return SimpleAck(ok=True, detail=detail)


@router.get("/judge-local", response_model=LocalJudgeOut)
def local_judge_status(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> LocalJudgeOut:
    """本机评测机（服务端子进程）的状态；非 Linux 服务端 ``supported`` = false。"""
    return ctx.local_judge.status()


@router.post("/judge-local/start", response_model=LocalJudgeOut)
def local_judge_start(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> LocalJudgeOut:
    """启动本机评测机：签一枚新令牌（记为本机评测机那一行），以子进程运行 worker。已在运行则不动。"""
    return ctx.local_judge.start(ctx)


@router.post("/judge-local/stop", response_model=LocalJudgeOut)
def local_judge_stop(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> LocalJudgeOut:
    """停止本机评测机：评完手上的任务后退出。"""
    return ctx.local_judge.stop()
