"""Agent 面 API：注册、tick、上传、下载、事件上报。

这是系统唯一的常态化流量入口。设计要点：

* ``/tick`` 是**幂等**的。重复调用、补发、乱序都不会破坏状态 —— 它比对的是
  "目录里现在有什么" 这个状态，而不是 "刚刚发生了什么" 这个事件序列。
* 除 ``/enroll`` 外全部要求 Bearer 凭据，且凭据只能访问**本选手本人**的资源。
* 所有来自客户端的路径都经 ``validate_relpath`` 校验，落盘前再经 ``safe_join``
  做符号链接检查。服务端不信任 Agent 的任何输入。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..context import AppContext
from ..models import (
    Agent,
    Asset,
    Contest,
    DeployStatus,
    DeployTarget,
    DeployTask,
    EnrollCode,
    EventLog,
    Player,
    SourceFile,
    utcnow,
)
from ..paths import PathValidationError, safe_join, validate_relpath
from ..policy import build_policy
from ..schemas import (
    AgentEvent,
    EnrollRequest,
    EnrollResponse,
    SimpleAck,
    TickRequest,
    TickResponse,
    UploadResult,
)
from ..security import (
    hash_enroll_code,
    hash_token,
    new_token,
)
from ..storage import BlobTooLarge, HashMismatch, materialize
from ..services.collect import reconcile_scan, record_events
from ..services.deploy import collect_deploy_jobs
from .deps import AgentIdentity, get_ctx, require_agent

__all__ = ["router"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_RANGE_CHUNK = 256 * 1024

#: 响应中最多回传多少条待上传路径，避免超大目录一次性撑爆响应体
MAX_NEED_UPLOAD = 500


def _agent_config(ctx: AppContext) -> Dict[str, Any]:
    """下发给 Agent 的运行时策略。服务端是权威，客户端不得自行决定。"""
    cfg = build_policy(
        max_file_size=ctx.settings.max_file_size,
        scan_interval=ctx.settings.tick_idle_seconds,
        max_files=ctx.settings.max_files_per_scan,
    )
    cfg["tick_idle_seconds"] = ctx.settings.tick_idle_seconds
    cfg["tick_active_seconds"] = ctx.settings.tick_active_seconds
    return cfg


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


@router.post("/enroll", response_model=EnrollResponse)
def enroll(
    payload: EnrollRequest,
    ctx: AppContext = Depends(get_ctx),
) -> EnrollResponse:
    """用注册码换长期凭据。

    刻意允许**重复注册**：NOI Linux 考试机常做整机快照还原，机器上的
    ``credential.json`` 会消失。此时 Agent 用镜像内置的注册码重新 enroll，
    服务端按 ``machine_id`` 认出这是老机器，换发新凭据并作废旧凭据。
    因此注册码不是一次性的，而是 "机器凭据种子"。
    """
    code_hash = hash_enroll_code(payload.enroll_code)
    now = utcnow()
    raw_token = new_token(ctx.settings.token_bytes)

    for attempt in (0, 1):
        try:
            return _enroll_once(ctx, payload, code_hash, raw_token, now)
        except IntegrityError:
            # 并发注册（同一台机器同时起了两个实例，或重试）触发唯一约束。
            # 回滚后重试一次即可命中已存在的行。
            if attempt == 1:
                raise HTTPException(status_code=409, detail="注册冲突，请重试")
            log.warning("注册发生唯一约束冲突，重试一次 machine_id=%s", payload.machine_id)
    raise HTTPException(status_code=409, detail="注册冲突，请重试")  # pragma: no cover


def _enroll_once(
    ctx: AppContext,
    payload: EnrollRequest,
    code_hash: str,
    raw_token: str,
    now,
) -> EnrollResponse:
    with ctx.db.session() as session:
        code = session.execute(
            select(EnrollCode).where(EnrollCode.code_hash == code_hash)
        ).scalar_one_or_none()
        if code is None:
            raise HTTPException(status_code=404, detail="注册码无效")
        if code.revoked_at is not None:
            raise HTTPException(status_code=403, detail="注册码已被吊销")
        if code.expires_at is not None and code.expires_at < now:
            raise HTTPException(status_code=403, detail="注册码已过期")

        player = session.get(Player, code.player_id)
        if player is None:  # pragma: no cover - 外键保证
            raise HTTPException(status_code=404, detail="选手不存在")
        contest = session.get(Contest, player.contest_id)
        if contest is None:  # pragma: no cover
            raise HTTPException(status_code=404, detail="场次不存在")

        # 首次使用则该注册码与这台机器绑定；之后只接受同一台机器
        if code.machine_id is None:
            code.machine_id = payload.machine_id
            log.info("注册码绑定到机器 machine_id=%s player=%s",
                     payload.machine_id, player.player_no)
        elif code.machine_id != payload.machine_id:
            raise HTTPException(
                status_code=403,
                detail="该注册码已绑定到其他机器（%s）" % code.machine_id[:12],
            )

        agent = session.execute(
            select(Agent).where(
                Agent.player_id == player.id,
                Agent.machine_id == payload.machine_id,
            )
        ).scalar_one_or_none()

        first_time = agent is None
        if agent is None:
            agent = Agent(
                player_id=player.id,
                token_hash=hash_token(raw_token),
                machine_id=payload.machine_id,
                hostname=payload.hostname,
                os_info=payload.os_info,
                agent_version=payload.agent_version,
                enrolled_at=now,
                last_enrolled_at=now,
                last_seen_at=now,
            )
            session.add(agent)
        else:
            if agent.revoked_at is not None:
                raise HTTPException(status_code=403, detail="该机器凭据已被吊销，请联系教师")
            # 换发凭据：旧 token 立刻失效
            agent.token_hash = hash_token(raw_token)
            agent.last_enrolled_at = now
            agent.last_seen_at = now
            agent.hostname = payload.hostname or agent.hostname
            agent.os_info = payload.os_info or agent.os_info
            agent.agent_version = payload.agent_version or agent.agent_version

        session.add(
            EventLog(
                level="info",
                category="enroll",
                contest_id=contest.id,
                player_id=player.id,
                message=("首次注册" if first_time else "重新注册（凭据换发）")
                + "：%s @ %s" % (player.player_no, payload.machine_id[:16]),
                meta_json=None,
            )
        )
        session.flush()

        agent_id = agent.id
        player_id = player.id
        response = EnrollResponse(
            token=raw_token,
            agent_id=agent_id,
            player_no=player.player_no,
            player_name=player.name,
            contest_id=contest.id,
            contest_slug=contest.slug,
            contest_name=contest.name,
            config=_agent_config(ctx),
        )

    ctx.registry.upsert_identity(
        agent_id=agent_id,
        player_id=player_id,
        contest_id=response.contest_id,
        player_no=response.player_no,
        player_name=response.player_name,
        contest_slug=response.contest_slug,
        machine_id=payload.machine_id,
        hostname=payload.hostname,
    )
    return response


# --------------------------------------------------------------------------- #
# tick
# --------------------------------------------------------------------------- #


@router.post("/tick", response_model=TickResponse)
def tick(
    payload: TickRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> TickResponse:
    if payload.machine_id and payload.machine_id != identity.machine_id:
        # 凭据与声明的机器不符：可能是凭据被复制到了别的机器
        log.warning("machine_id 不匹配 agent=%s 声明=%s 实际=%s",
                    identity.agent_id, payload.machine_id, identity.machine_id)
        raise HTTPException(status_code=409, detail="machine_id 与凭据不匹配")

    client_ip = request.client.host if request.client else None
    now = utcnow()

    with ctx.db.session() as session:
        collect = reconcile_scan(
            session,
            identity.player_id,
            payload.scan,
            ctx.settings,
            scan_complete=payload.scan_complete,
        )
        record_events(
            session,
            player_id=identity.player_id,
            contest_id=identity.contest_id,
            agent_id=identity.agent_id,
            events=collect.events,
        )

        partials = {p.asset_id: int(p.bytes_done) for p in payload.partials}
        jobs = collect_deploy_jobs(session, identity.player_id, partials)

        if payload.completed_assets:
            _mark_deployments_done(session, identity.player_id, payload.completed_assets)

        agent = session.get(Agent, identity.agent_id)
        if agent is not None:
            agent.last_seen_at = now
            if payload.agent_version:
                agent.agent_version = payload.agent_version

    ctx.registry.note_tick(
        identity.agent_id,
        ip=client_ip,
        agent_version=payload.agent_version,
        scan_root=payload.scan_root,
        file_count=collect.seen_files + collect.new_files,
        disk_free=payload.stats.disk_free,
        last_error=payload.stats.last_error,
    )

    # 自适应周期：有活干就收紧，没活干就放宽
    active = bool(collect.need_upload or jobs or collect.deleted)
    next_tick = (
        ctx.settings.tick_active_seconds if active else ctx.settings.tick_idle_seconds
    )

    return TickResponse(
        server_time=int(now.replace(tzinfo=None).timestamp()),
        next_tick_seconds=next_tick,
        need_upload=collect.need_upload[:MAX_NEED_UPLOAD],
        deploy_jobs=jobs,
        cancel_assets=[],
        upgrade=None,
        config=_agent_config(ctx),
    )


def _mark_deployments_done(
    session, player_id: int, asset_ids: List[int]
) -> None:
    """把该选手的这些资源对应的下发目标标记为完成，并同步任务整体状态。"""
    if not asset_ids:
        return
    targets = list(
        session.execute(
            select(DeployTarget)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(
                DeployTarget.player_id == player_id,
                DeployTask.asset_id.in_(asset_ids[:200]),
                DeployTarget.status.in_((DeployStatus.PENDING, DeployStatus.READY)),
            )
        ).scalars()
    )
    if not targets:
        return

    now = utcnow()
    affected_tasks = set()
    for target in targets:
        target.status = DeployStatus.DONE
        target.last_error = None
        target.updated_at = now
        affected_tasks.add(target.task_id)

    session.flush()
    for task_id in affected_tasks:
        _refresh_task_status(session, task_id)


def _refresh_task_status(session, task_id: int) -> None:
    """按目标的实际状态重算任务状态。

    不维护"任务状态"这个独立字段，而是每次从目标聚合 —— 两处独立维护的状态
    迟早会不一致，典型症状是 Web 上进度条卡在 90% 永远不动。
    """
    task = session.get(DeployTask, task_id)
    if task is None or task.status == DeployStatus.CANCELLED:
        return

    targets = list(
        session.execute(
            select(DeployTarget).where(DeployTarget.task_id == task_id)
        ).scalars()
    )
    if not targets:
        return

    done = sum(1 for t in targets if t.status == DeployStatus.DONE)
    failed = sum(1 for t in targets if t.status == DeployStatus.FAILED)
    if done == len(targets):
        task.status = DeployStatus.DONE
    elif done + failed == len(targets):
        task.status = DeployStatus.FAILED
    else:
        task.status = DeployStatus.PENDING


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

@router.post("/files", response_model=UploadResult)
def upload_file(
    request: Request,
    path: str = Form(...),
    sha256: str = Form(...),
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> UploadResult:
    """接收一个文件内容。

    **旧版本保护**：若客户端送来的 ``sha256`` 与服务端台账当前记录的版本不一致，
    说明这是迟到的旧内容（断网补传、乱序重传），直接以 409 拒绝，让客户端在下一
    轮 tick 里拿到真正需要的版本。绝不覆盖更新的版本。
    """
    try:
        rel_path = validate_relpath(path, max_length=ctx.settings.max_relpath_length)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail="路径不合法: %s" % exc)

    if not SHA256_RE.match(sha256 or ""):
        raise HTTPException(status_code=400, detail="sha256 格式不合法")

    try:
        actual_sha, size = ctx.blobs.put_stream(
            file.file, max_bytes=ctx.settings.max_file_size, expected_sha256=sha256
        )
    except BlobTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except HashMismatch as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = utcnow()
    with ctx.db.session() as session:
        row = session.execute(
            select(SourceFile).where(
                SourceFile.player_id == identity.player_id,
                SourceFile.rel_path == rel_path,
            )
        ).scalar_one_or_none()

        if row is None:
            # 台账里还没有：Agent 比服务端先看到了这个文件。
            # 这不危险（路径与内容都已校验），建一条即可。
            row = SourceFile(
                player_id=identity.player_id,
                rel_path=rel_path,
                sha256=actual_sha,
                size=size,
                mtime=0,
                revision=0,
                content_stored=False,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(row)
        elif row.content_stored and row.sha256 != actual_sha:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stale",
                    "message": "该内容已不是当前版本，已在下一轮 tick 中请求最新版本",
                    "expected": row.sha256,
                },
            )

        row.sha256 = actual_sha
        row.size = size
        row.revision += 1
        row.content_stored = True
        row.deleted_at = None
        row.last_seen_at = now
        session.flush()
        revision = row.revision

    # 物化到评测器可读的目录：source/<场次>/<选手>/<相对路径>
    contest_dir = Path(ctx.settings.source_root) / identity.contest_slug / identity.player_no
    try:
        dest = safe_join(contest_dir, rel_path, max_length=ctx.settings.max_relpath_length)
        materialize(ctx.blobs.path_for(actual_sha), dest)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail="落盘路径不合法: %s" % exc)
    except OSError as exc:
        log.exception("物化到 source/ 失败: %s", rel_path)
        raise HTTPException(status_code=500, detail="落盘失败: %s" % exc)

    return UploadResult(path=rel_path, sha256=actual_sha, revision=revision, stored=True)


# --------------------------------------------------------------------------- #
# 下载（支持 Range 断点续传）
# --------------------------------------------------------------------------- #


@router.get("/assets/{asset_id}")
def download_asset(
    asset_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> Response:
    with ctx.db.session() as session:
        asset = session.get(Asset, asset_id)
        if asset is None or asset.contest_id != identity.contest_id:
            raise HTTPException(status_code=404, detail="资源不存在")

        # 授权：必须确实是"下发给这个选手"的资源。
        # 仅凭 token 有效是不够的 —— 否则任一选手可以拖走全场测试点。
        authorized = session.execute(
            select(DeployTarget.id)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(
                DeployTask.asset_id == asset_id,
                DeployTarget.player_id == identity.player_id,
            )
        ).first()
        if authorized is None:
            raise HTTPException(status_code=403, detail="该资源未下发给本选手")

        blob_sha = asset.sha256
        expected_size = int(asset.size)

    blob_path = ctx.blobs.path_for(blob_sha)
    if not blob_path.is_file():
        raise HTTPException(status_code=410, detail="资源内容缺失")
    if expected_size and blob_path.stat().st_size != expected_size:
        raise HTTPException(status_code=500, detail="资源内容损坏，大小与台账不符")

    return _range_response(blob_path, request)


def _range_response(file_path: Path, request: Request) -> Response:
    """实现 HTTP Range，用于断点续传。

    不依赖框架的 Range 支持：这是断点续传的核心路径，行为必须明确可测。
    """
    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            file_path,
            media_type="application/octet-stream",
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    match = _RANGE_RE.match(range_header.strip())
    if not match:
        return Response(
            status_code=416,
            headers={"Content-Range": "bytes */%d" % file_size},
        )

    start_raw, end_raw = match.groups()
    if start_raw == "":
        # suffix range：最后 N 字节
        if end_raw == "":
            return Response(status_code=416, headers={"Content-Range": "bytes */%d" % file_size})
        start = max(0, file_size - int(end_raw))
        end = file_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1

    if start >= file_size or start > end:
        return Response(status_code=416, headers={"Content-Range": "bytes */%d" % file_size})
    end = min(end, file_size - 1)
    length = end - start + 1

    def iter_range() -> Iterator[bytes]:
        with file_path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_RANGE_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            "Content-Range": "bytes %d-%d/%d" % (start, end, file_size),
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


# --------------------------------------------------------------------------- #
# 事件上报
# --------------------------------------------------------------------------- #


@router.post("/events", response_model=SimpleAck)
def report_events(
    payload: List[AgentEvent],
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> SimpleAck:
    """Agent 主动上报的审计事件（权限异常、磁盘满、批量重写等）。

    刻意用列表批量提交：断网期间事件会堆积，逐条上报会在重连瞬间产生请求风暴。
    """
    if not payload:
        return SimpleAck(ok=True, detail="无事件")
    if len(payload) > 200:
        payload = payload[:200]

    with ctx.db.session() as session:
        for item in payload:
            session.add(
                EventLog(
                    level=item.level,
                    category=item.category[:32],
                    contest_id=identity.contest_id,
                    player_id=identity.player_id,
                    agent_id=identity.agent_id,
                    message=item.message[:2000],
                    meta_json=(
                        _dumps(item.meta) if item.meta else None
                    ),
                )
            )
    return SimpleAck(ok=True, detail="已记录 %d 条" % len(payload))


def _dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)[:8000]
    except (TypeError, ValueError):
        return "{}"


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


@router.get("/me")
def whoami(
    identity: AgentIdentity = Depends(require_agent),
    ctx: AppContext = Depends(get_ctx),
) -> Dict[str, Any]:
    runtime = ctx.registry.get(identity.agent_id)
    return {
        "agent_id": identity.agent_id,
        "player_no": identity.player_no,
        "player_name": identity.player_name,
        "contest_id": identity.contest_id,
        "contest_slug": identity.contest_slug,
        "contest_name": identity.contest_name,
        "contest_status": identity.contest_status,
        "machine_id": identity.machine_id,
        "online": bool(runtime and runtime.online),
        "config": _agent_config(ctx),
    }
