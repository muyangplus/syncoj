"""管理端：题目评测配置的导入 / 导出，以及数据包原样下载。

导入分两步：``preview`` 把上传的包暂存到 ``<data_root>/tmp/judge-import/<token>.bin``、解析并试构建
数据包（不写 blob），返回每题的建议 ident、动作与警告；``apply`` 凭 token **重新解析**暂存包
（不信任客户端回传的配置，只采用 ident 与动作），写 blob、建题或覆盖。暂存 2 小时过期，
每个场次最多留 5 份。

内存：preview 只算摘要（``try_summarize``，不写出完整数据包）；apply 把数据包写到暂存目录下的
临时文件再流式入库（``try_build`` + ``put_stream``）。两者各有一份整请求共用的构建预算。

格式解析与写出在 ``services/problem_io``；这里只管暂存、鉴权、落库。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select

from ..context import AppContext
from ..errors import ApiError
from ..models import Contest, EventLog, Problem
from ..schemas import (
    JudgeExportFormat,
    JudgeExportItemOut,
    JudgeExportPreviewOut,
    JudgeImportApplyIn,
    JudgeImportErrorOut,
    JudgeImportFormat,
    JudgeImportItemOut,
    JudgeImportOut,
    JudgeImportPreviewOut,
    NoticeOut,
)
from ..services import problem_io
from ..services.problem_io.build import BuildBudget, try_build, try_summarize
from ..services.problem_io.bundle import ExportProblem, Notice, ProblemBundle
from ..services.problem_io.notices import ProblemIoError
from ..storage import BlobTooLarge, HashMismatch
from .admin import (
    AdminIdentity,
    _attachment_header,
    _blob_response,
    _problem_out,
    _reclaim_blobs,
    _spool_chunks,
    _validate_problem_ident,
    require_admin,
)
from .deps import get_ctx

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

#: 导入包上限（与数据包上传一致）
MAX_IMPORT_BYTES = 512 * 1024 * 1024
#: 暂存有效期
STAGING_TTL_SECONDS = 2 * 3600
#: 每个场次同时保留的暂存份数
MAX_STAGED_PER_CONTEST = 5
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------- #
# 暂存
# --------------------------------------------------------------------------- #


def _staging_dir(ctx: AppContext) -> Path:
    path = ctx.settings.data_root / "tmp" / "judge-import"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _drop(folder: Path, token: str) -> None:
    for suffix in (".bin", ".json"):
        try:
            (folder / (token + suffix)).unlink()
        except FileNotFoundError:
            pass


def _staged(folder: Path) -> List[Tuple[str, Dict[str, object]]]:
    out = []
    for meta_path in folder.glob("*.json"):
        token = meta_path.stem
        if not _TOKEN_RE.match(token):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        out.append((token, meta if isinstance(meta, dict) else {}))
    return out


def _sweep(folder: Path, contest_id: Optional[int] = None) -> None:
    """删掉过期的暂存；给了场次时再把该场次超出 5 份的最旧几份删掉。"""
    now = time.time()
    entries = _staged(folder)
    alive = []
    for token, meta in entries:
        created = float(meta.get("created_at") or 0)  # type: ignore[arg-type]
        if now - created > STAGING_TTL_SECONDS:
            _drop(folder, token)
        else:
            alive.append((token, meta, created))
    # 没有 .json 的孤儿 .bin（写到一半失败）按修改时间清
    for bin_path in folder.glob("*.bin"):
        if not (folder / (bin_path.stem + ".json")).exists():
            try:
                if now - bin_path.stat().st_mtime > STAGING_TTL_SECONDS:
                    bin_path.unlink()
            except OSError:
                pass
    if contest_id is not None:
        mine = sorted(
            (item for item in alive if item[1].get("contest_id") == contest_id),
            key=lambda item: item[2],
        )
        for token, _meta, _created in mine[: max(0, len(mine) - MAX_STAGED_PER_CONTEST)]:
            _drop(folder, token)


def _save_upload(folder: Path, token: str, upload: UploadFile) -> None:
    target = folder / (token + ".bin")
    size = 0
    with target.open("wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_IMPORT_BYTES:
                out.close()
                _drop(folder, token)
                raise ApiError(413, "payload_too_large", "导入包超过 512 MB")
            out.write(chunk)


def _io_error(exc: ProblemIoError) -> ApiError:
    if exc.code == "too_many_problems":
        return ApiError(413, "too_many_problems", exc.notice.message, details={"code_detail": exc.code})
    return ApiError(400, "validation_error", exc.notice.message, details={"code_detail": exc.code})


def _notices(items: List[Notice]) -> List[NoticeOut]:
    return [NoticeOut(code=item.code, message=item.message) for item in items]


def _load_contest(session, contest_id: int) -> Contest:
    contest = session.get(Contest, contest_id)
    if contest is None:
        raise ApiError(404, "not_found", "场次不存在")
    return contest


def _existing(session, contest_id: int) -> Dict[str, Problem]:
    rows = session.execute(select(Problem).where(Problem.contest_id == contest_id)).scalars()
    return {row.ident: row for row in rows}


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #


def _item_out(bundle: ProblemBundle, existing: Dict[str, Problem], summary: Optional[dict]) -> JudgeImportItemOut:
    row = existing.get(bundle.ident)
    if bundle.errors:
        action = "skip"
    elif row is not None:
        action = "update"
    else:
        action = "create"
    summary = summary or {}
    return JudgeImportItemOut(
        key=bundle.key,
        source_name=bundle.source_name or bundle.key,
        ident=bundle.ident,
        title=bundle.title,
        exists=row is not None,
        existing_problem_id=row.id if row is not None else None,
        action=action,
        judge_mode=bundle.judge_mode,
        time_limit_ms=bundle.time_limit_ms,
        memory_limit_mb=bundle.memory_limit_mb,
        io_mode=bundle.io_mode,
        input_file=bundle.input_file,
        output_file=bundle.output_file,
        compile_flags=bundle.compile_flags,
        cases=int(summary.get("cases") or 0),
        subtasks=int(summary.get("subtasks") or 0),
        total_score=int(summary.get("total_score") or 0),
        has_checker=bool(summary.get("has_checker")),
        warnings=_notices(bundle.warnings),
        errors=_notices(bundle.errors),
    )


@router.post("/contests/{contest_id}/judge-import/preview", response_model=JudgeImportPreviewOut)
def judge_import_preview(
    contest_id: int,
    file: UploadFile = File(...),
    format: JudgeImportFormat = Query("auto"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeImportPreviewOut:
    """上传题目配置包（zip / FPS xml）：识别格式、解析、试构建数据包，返回预览与暂存 token。"""
    with ctx.db.session() as session:
        _load_contest(session, contest_id)
    folder = _staging_dir(ctx)
    _sweep(folder)
    token = secrets.token_hex(16)
    _save_upload(folder, token, file)
    try:
        parsed = problem_io.parse_archive(folder / (token + ".bin"), format, file.filename or "")
    except ProblemIoError as exc:
        _drop(folder, token)
        raise _io_error(exc)
    except Exception:
        _drop(folder, token)
        raise
    with parsed:
        # 预览只要摘要：不写出完整数据包；整个请求共用一份构建预算
        budget = BuildBudget()
        summaries: Dict[str, Optional[dict]] = {}
        for bundle in parsed.result.problems:
            summaries[bundle.key] = None
            if bundle.errors or not bundle.has_testdata:
                continue
            summaries[bundle.key] = try_summarize(bundle, parsed.archive, budget)
    (folder / (token + ".json")).write_text(
        json.dumps({
            "contest_id": contest_id,
            "created_at": time.time(),
            "format": parsed.result.format,
            "filename": (file.filename or "")[:255],
        }),
        encoding="utf-8",
    )
    _sweep(folder, contest_id)
    with ctx.db.session() as session:
        existing = _existing(session, contest_id)
        problems = [_item_out(b, existing, summaries[b.key]) for b in parsed.result.problems]
    return JudgeImportPreviewOut(
        token=token,
        format=parsed.result.format,
        problems=problems,
        warnings=_notices(parsed.result.warnings),
    )


def _load_staged(ctx: AppContext, token: str, contest_id: int) -> Tuple[Path, Dict[str, object]]:
    folder = _staging_dir(ctx)
    if not _TOKEN_RE.match(token):
        raise ApiError(404, "import_token_expired")
    meta_path = folder / (token + ".json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApiError(404, "import_token_expired")
    if not isinstance(meta, dict):
        raise ApiError(404, "import_token_expired")
    expired = time.time() - float(meta.get("created_at") or 0) > STAGING_TTL_SECONDS
    if meta.get("contest_id") != contest_id or expired:
        if expired:
            _drop(folder, token)
        raise ApiError(404, "import_token_expired")
    if not (folder / (token + ".bin")).is_file():
        raise ApiError(404, "import_token_expired")
    return folder, meta


def _title_is_just_a_name(bundle: ProblemBundle, ident: str) -> bool:
    """包里的标题只是来源名 / ident（不分大小写）—— 如 LemonLime 的 problemTitle 就是文件主名。
    覆盖已有题时这种标题不算"有值"，不拿它换掉教师起的标题。"""
    title = (bundle.title or "").strip().casefold()
    return title in {(bundle.source_name or "").strip().casefold(), ident.casefold()}


def _effective_io(bundle: ProblemBundle, ident: str) -> Tuple[Optional[str], Optional[str]]:
    """改了 ident 之后，IO 文件名仍指向包里原来的实际文件名。"""
    if bundle.io_mode != "file":
        return None, None
    actual_in = bundle.input_file or "%s.in" % bundle.ident
    actual_out = bundle.output_file or "%s.out" % bundle.ident
    return (
        None if actual_in == "%s.in" % ident else actual_in,
        None if actual_out == "%s.out" % ident else actual_out,
    )


@router.post("/contests/{contest_id}/judge-import/apply", response_model=JudgeImportOut)
def judge_import_apply(
    contest_id: int,
    payload: JudgeImportApplyIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeImportOut:
    """按预览时的 token 应用导入。每题只采用客户端给的 ident 与动作（新建 / 覆盖 / 跳过）。"""
    folder, meta = _load_staged(ctx, payload.token, contest_id)
    fmt = str(meta.get("format") or "auto")
    try:
        parsed = problem_io.parse_archive(folder / (payload.token + ".bin"), fmt, str(meta.get("filename") or ""))
    except ProblemIoError as exc:
        raise _io_error(exc)

    result = JudgeImportOut()
    new_blobs: List[str] = []
    with parsed:
        bundles = {bundle.key: bundle for bundle in parsed.result.problems}
        chosen = {}
        for item in payload.items:
            if item.key not in bundles:
                raise ApiError(400, "validation_error", "包里没有 %s" % item.key)
            if item.key in chosen:
                raise ApiError(400, "validation_error", "%s 重复" % item.key)
            chosen[item.key] = item

        with ctx.db.session() as session:
            _load_contest(session, contest_id)
            existing = _existing(session, contest_id)

        plan: List[Tuple[ProblemBundle, str, str]] = []  # (bundle, ident, action)
        seen_idents: Dict[str, str] = {}
        for key, bundle in bundles.items():
            item = chosen.get(key)
            if item is None or item.action == "skip":
                result.skipped += 1
                continue
            try:
                ident = _validate_problem_ident(item.ident)
            except HTTPException as exc:
                raise ApiError(400, "validation_error", str(exc.detail), details={"key": key})
            if bundle.errors:
                raise ApiError(
                    400, "validation_error", "%s 有错误，只能跳过" % (bundle.source_name or key),
                    details={"key": key},
                )
            if ident in seen_idents:
                raise ApiError(409, "ident_conflict", "题目标识 %s 重复" % ident, details={"key": key, "ident": ident})
            seen_idents[ident] = key
            if item.action == "create" and ident in existing:
                raise ApiError(409, "ident_conflict", "题目 %s 已存在" % ident, details={"key": key, "ident": ident})
            if item.action == "update" and ident not in existing:
                raise ApiError(400, "validation_error", "题目 %s 不存在，无法覆盖" % ident, details={"key": key})
            plan.append((bundle, ident, item.action))

        # 先写全部 blob，再在一个会话里建题 / 覆盖。数据包一题一题写临时文件（放在暂存目录，
        # 与数据同盘，不占 /tmp 的内存盘）再流式入库，同一时刻只有一份在盘上
        built: Dict[str, Tuple[str, int, dict]] = {}
        budget = BuildBudget()
        try:
            for bundle, ident, _action in plan:
                if not bundle.has_testdata:
                    continue
                output = try_build(bundle, parsed.archive, budget, tmp_dir=folder)
                if output is None:
                    raise ApiError(
                        400, "validation_error",
                        "%s：%s" % (ident, bundle.errors[-1].message), details={"key": bundle.key},
                    )
                with output:
                    try:
                        sha256, size = ctx.blobs.put_stream(
                            output.file, max_bytes=output.max_bytes, expected_sha256=output.sha256,
                        )
                    except (BlobTooLarge, HashMismatch) as exc:
                        raise ApiError(400, "validation_error", "%s：%s" % (ident, exc), details={"key": bundle.key})
                new_blobs.append(sha256)
                built[bundle.key] = (sha256, size, output.summary)
        except Exception:
            _reclaim_blobs(ctx, new_blobs)
            raise

    is_native = parsed.result.format == "syncoj"
    replaced: List[str] = []
    try:
        with ctx.db.session() as session:
            existing = _existing(session, contest_id)
            order = session.execute(
                select(func.max(Problem.order_index)).where(Problem.contest_id == contest_id)
            ).scalar() or 0
            for bundle, ident, action in plan:
                row = existing.get(ident)
                if action == "create":
                    if row is not None:
                        raise ApiError(409, "ident_conflict", "题目 %s 已存在" % ident, details={"key": bundle.key})
                    order += 1
                    row = Problem(contest_id=contest_id, ident=ident, order_index=order)
                    if is_native:
                        if bundle.order_index is not None:
                            row.order_index = bundle.order_index
                        row.note = bundle.note
                        if bundle.file_patterns is not None:
                            row.file_patterns = json.dumps(bundle.file_patterns, ensure_ascii=False)
                    session.add(row)
                    row.judge_mode = bundle.judge_mode if is_native else "builtin"
                    result.created += 1
                else:
                    if row is None:
                        raise ApiError(400, "validation_error", "题目 %s 不存在，无法覆盖" % ident)
                    if is_native:
                        row.judge_mode = bundle.judge_mode
                    elif payload.set_builtin:
                        row.judge_mode = "builtin"
                    result.updated += 1
                if bundle.title and (action == "create" or not _title_is_just_a_name(bundle, ident)):
                    row.title = bundle.title
                row.time_limit_ms = bundle.time_limit_ms
                row.memory_limit_mb = bundle.memory_limit_mb
                row.io_mode = bundle.io_mode
                row.input_file, row.output_file = _effective_io(bundle, ident)
                row.compile_flags = bundle.compile_flags
                old = row.testdata_sha256
                if bundle.key in built:
                    sha256, size, summary = built[bundle.key]
                    row.testdata_sha256 = sha256
                    row.testdata_size = size
                    row.testdata_name = (bundle.testdata_name or "%s.zip" % ident)[:255]
                    row.testdata_info = json.dumps(summary)
                else:
                    row.testdata_sha256 = row.testdata_name = row.testdata_info = None
                    row.testdata_size = None
                if old and old != row.testdata_sha256:
                    replaced.append(old)
                session.flush()
                result.problems.append(_problem_out(row, ctx.settings))
            session.add(EventLog(
                level="info", category="judge_import", contest_id=contest_id,
                message="导入题目配置（%s）：新建 %d 道，覆盖 %d 道"
                % (problem_io.FORMATS[parsed.result.format].label, result.created, result.updated),
            ))
    except Exception:
        _reclaim_blobs(ctx, new_blobs)
        raise
    _reclaim_blobs(ctx, replaced)
    _drop(folder, payload.token)
    return result


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _snapshot(row: Problem) -> ExportProblem:
    try:
        patterns = json.loads(row.file_patterns) if row.file_patterns else None
    except ValueError:
        patterns = None
    return ExportProblem(
        id=row.id,
        ident=row.ident,
        title=row.title,
        order_index=int(row.order_index or 0),
        note=row.note,
        file_patterns=patterns if isinstance(patterns, list) else None,
        judge_mode=row.judge_mode or "external",
        time_limit_ms=row.time_limit_ms,
        memory_limit_mb=row.memory_limit_mb,
        io_mode=row.io_mode or "stdio",
        input_file=row.input_file or None,
        output_file=row.output_file or None,
        compile_flags=row.compile_flags,
        testdata_sha256=row.testdata_sha256,
        testdata_name=row.testdata_name,
        testdata_size=int(row.testdata_size) if row.testdata_size is not None else None,
    )


def _select(ctx: AppContext, contest_id: int, fmt: str, problem_ids: Optional[List[int]]) -> Tuple[str, List[ExportProblem]]:
    with ctx.db.session() as session:
        contest = _load_contest(session, contest_id)
        rows = list(session.execute(
            select(Problem).where(Problem.contest_id == contest_id).order_by(Problem.order_index, Problem.ident)
        ).scalars())
        if problem_ids:
            wanted = set(problem_ids)
            rows = [row for row in rows if row.id in wanted]
            if not rows:
                raise ApiError(404, "not_found", "题目不在本场次")
        elif fmt != "syncoj":
            rows = [row for row in rows if row.judge_mode == "builtin" and row.testdata_sha256]
        return contest.slug, [_snapshot(row) for row in rows]


def _export(ctx: AppContext, problems: List[ExportProblem], fmt: str, dry_run: bool) -> problem_io.ExportOutcome:
    try:
        return problem_io.export_problems(problems, fmt, ctx.blobs.open, dry_run=dry_run)
    except ProblemIoError as exc:
        raise _io_error(exc)


def _zip_response(outcome: problem_io.ExportOutcome, filename: str) -> StreamingResponse:
    return StreamingResponse(
        _spool_chunks(outcome.spool),
        media_type="application/zip",
        headers={"Content-Disposition": _attachment_header(filename)},
    )


@router.get("/contests/{contest_id}/judge-export/preview", response_model=JudgeExportPreviewOut)
def judge_export_preview(
    contest_id: int,
    format: JudgeExportFormat = Query("syncoj"),
    problem_id: Optional[List[int]] = Query(None),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeExportPreviewOut:
    """导出前看一眼：哪些题会写进包里、每题会丢掉什么。不传 problem_id 时按默认范围选题。"""
    _slug, problems = _select(ctx, contest_id, format, problem_id)
    if not problems:
        return JudgeExportPreviewOut(format=format)
    outcome = _export(ctx, problems, format, dry_run=True)
    included = set(outcome.included)
    return JudgeExportPreviewOut(
        format=format,
        problems=[
            JudgeExportItemOut(
                problem_id=p.id, ident=p.ident, included=p.id in included,
                warnings=_notices(outcome.notices.get(p.id, [])),
            )
            for p in problems
        ],
    )


@router.get("/contests/{contest_id}/judge-export")
def judge_export(
    contest_id: int,
    format: JudgeExportFormat = Query("syncoj"),
    problem_id: Optional[List[int]] = Query(None),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    """导出题目评测配置（zip）。"""
    slug, problems = _select(ctx, contest_id, format, problem_id)
    if not problems:
        raise ApiError(400, "validation_error", "没有可导出的题目")
    outcome = _export(ctx, problems, format, dry_run=False)
    return _zip_response(outcome, "题目配置-%s-%s.zip" % (slug, format))


@router.get("/problems/{problem_id}/judge-export")
def judge_export_problem(
    problem_id: int,
    format: JudgeExportFormat = Query("syncoj"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    """单题导出：等价于场次导出只选这一题。"""
    with ctx.db.session() as session:
        row = session.get(Problem, problem_id)
        if row is None:
            raise ApiError(404, "not_found", "题目不存在")
        contest_id = row.contest_id
    slug, problems = _select(ctx, contest_id, format, [problem_id])
    outcome = _export(ctx, problems, format, dry_run=False)
    return _zip_response(outcome, "题目配置-%s-%s-%s.zip" % (slug, problems[0].ident, format))


@router.get("/problems/{problem_id}/testdata/file")
def download_testdata(
    problem_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Response:
    """原样下载题目的数据包（上传时的 zip，字节不变）。"""
    with ctx.db.session() as session:
        row = session.get(Problem, problem_id)
        if row is None:
            raise ApiError(404, "not_found", "题目不存在")
        sha256, name = row.testdata_sha256, row.testdata_name
    if not sha256 or not ctx.blobs.has(sha256):
        raise ApiError(404, "not_found", "这道题没有数据包")
    return _blob_response(ctx, sha256, download_name=name or "testdata.zip")
