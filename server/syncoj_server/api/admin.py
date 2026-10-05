"""管理后台 API。

单管理员模型：登录换取不透明会话 token（服务端只存哈希，可按会话吊销）。
M1 阶段只覆盖"建场次 → 导选手 → 发注册码 → 看在线上报"这条闭环。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import func, select

from ..context import AppContext
from ..models import (
    Admin,
    AdminSession,
    Agent,
    Asset,
    Contest,
    ContestStatus,
    DeployStatus,
    DeployTarget,
    DeployTask,
    EnrollCode,
    EventLog,
    JudgeRun,
    Player,
    SourceFile,
    utcnow,
)
from ..paths import PathValidationError, safe_join, slugify, validate_relpath
from ..schemas import (
    AdminInfo,
    AgentRuntimeOut,
    AssetOut,
    ContestCreate,
    ContestOut,
    DeployCreate,
    DeployTargetOut,
    DeployTaskOut,
    EnrollCodeOut,
    EventOut,
    JudgeRunOut,
    JudgeScanOut,
    LoginRequest,
    LoginResponse,
    ManualScoreIn,
    PlayerOut,
    PlayerUpsert,
    ScoreCellOut,
    ScoreMatrixOut,
    ScoreRowOut,
    SimpleAck,
    SourceFileOut,
)
from ..security import (
    hash_enroll_code,
    hash_password,
    hash_token,
    new_enroll_code,
    new_token,
    verify_password,
)
from ..storage import BlobTooLarge, HashMismatch
from .deps import get_ctx

__all__ = ["router", "require_admin", "AdminIdentity"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _iso(value) -> Optional[str]:
    return value.strftime(_ISO) if value else None


@dataclass
class AdminIdentity:
    id: int
    username: str


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin(request: Request, ctx: AppContext = Depends(get_ctx)) -> AdminIdentity:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise _unauthorized("缺少 Bearer 凭据")
    raw = header[7:].strip()
    if not raw:
        raise _unauthorized("凭据为空")

    now = utcnow()
    with ctx.db.session() as session:
        row = session.execute(
            select(AdminSession, Admin)
            .join(Admin, AdminSession.admin_id == Admin.id)
            .where(AdminSession.token_hash == hash_token(raw))
        ).first()
        if row is None:
            raise _unauthorized("会话无效")
        admin_session, admin = row
        if admin_session.revoked_at is not None:
            raise _unauthorized("会话已注销")
        if admin_session.expires_at < now:
            raise _unauthorized("会话已过期")
        if not admin.is_active:
            raise _unauthorized("账号已停用")
        return AdminIdentity(id=admin.id, username=admin.username)


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, ctx: AppContext = Depends(get_ctx)) -> LoginResponse:
    now = utcnow()
    raw_token = new_token(ctx.settings.token_bytes)
    expires_at = now + timedelta(seconds=ctx.settings.admin_session_ttl_seconds)

    with ctx.db.session() as session:
        admin = session.execute(
            select(Admin).where(Admin.username == payload.username)
        ).scalar_one_or_none()

        # 无论账号是否存在都跑一次口令校验，避免通过响应时间枚举用户名
        stored = admin.password_hash if admin else _DUMMY_HASH
        ok = verify_password(payload.password, stored)
        if admin is None or not ok or not admin.is_active:
            log.warning("管理员登录失败: %s", payload.username)
            raise _unauthorized("用户名或口令错误")

        admin.last_login_at = now
        session.add(
            AdminSession(
                admin_id=admin.id,
                token_hash=hash_token(raw_token),
                created_at=now,
                expires_at=expires_at,
            )
        )
        username = admin.username

    return LoginResponse(token=raw_token, username=username, expires_at=_iso(expires_at))


#: 用户名不存在时用来消耗等量 CPU 的假哈希（口令为 "invalid"）
_DUMMY_HASH = hash_password("invalid")


@router.post("/logout", response_model=SimpleAck)
def logout(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> SimpleAck:
    header = request.headers.get("authorization") or ""
    raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not raw:
        return SimpleAck(ok=True, detail="未提供凭据")
    with ctx.db.session() as session:
        row = session.execute(
            select(AdminSession).where(AdminSession.token_hash == hash_token(raw))
        ).scalar_one_or_none()
        if row is not None:
            row.revoked_at = utcnow()
    return SimpleAck(ok=True)


@router.get("/me", response_model=AdminInfo)
def me(admin: AdminIdentity = Depends(require_admin)) -> AdminInfo:
    return AdminInfo(username=admin.username, is_active=True)


# --------------------------------------------------------------------------- #
# 场次
# --------------------------------------------------------------------------- #


@router.post("/contests", response_model=ContestOut)
def create_contest(
    payload: ContestCreate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ContestOut:
    status_value = payload.status if payload.status in ContestStatus.ALL else ContestStatus.DRAFT
    slug = slugify(payload.slug or payload.name, fallback="contest")

    with ctx.db.session() as session:
        if session.execute(select(Contest).where(Contest.slug == slug)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="场次标识已存在: %s" % slug)
        contest = Contest(slug=slug, name=payload.name, status=status_value, note=payload.note)
        session.add(contest)
        session.flush()
        return _contest_out(contest, player_count=0, online_count=0)


@router.get("/contests", response_model=List[ContestOut])
def list_contests(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[ContestOut]:
    with ctx.db.session() as session:
        counts = dict(
            session.execute(
                select(Player.contest_id, func.count(Player.id)).group_by(Player.contest_id)
            ).all()
        )
        contests = list(session.execute(select(Contest).order_by(Contest.id)).scalars())
        result = []
        for contest in contests:
            online = sum(1 for a in ctx.registry.all(contest.id) if a.online)
            result.append(_contest_out(contest, counts.get(contest.id, 0), online))
        return result


def _contest_out(contest: Contest, player_count: int, online_count: int) -> ContestOut:
    return ContestOut(
        id=contest.id,
        slug=contest.slug,
        name=contest.name,
        status=contest.status,
        player_count=player_count,
        online_count=online_count,
        created_at=_iso(contest.created_at) or "",
    )


# --------------------------------------------------------------------------- #
# 选手
# --------------------------------------------------------------------------- #


@router.post("/contests/{contest_id}/players", response_model=List[PlayerOut])
def import_players(
    contest_id: int,
    payload: List[PlayerUpsert],
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[PlayerOut]:
    """批量导入/更新选手。按 ``player_no`` 幂等 upsert。"""
    if not payload:
        return []
    if len(payload) > 2000:
        raise HTTPException(status_code=413, detail="单次最多导入 2000 名选手")

    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

        existing = {
            row.player_no: row
            for row in session.execute(
                select(Player).where(Player.contest_id == contest_id)
            ).scalars()
        }
        touched: List[Player] = []
        for item in payload:
            row = existing.get(item.player_no)
            if row is None:
                row = Player(contest_id=contest_id, player_no=item.player_no)
                session.add(row)
                existing[item.player_no] = row
            row.name = item.name
            row.seat = item.seat
            row.group_name = item.group_name
            touched.append(row)

        # 必须先 flush 才能拿到自增主键 —— 新插入的行在 flush 前 id 为 None
        session.flush()
        return [
            _player_out(row, has_agent=False, online=False, file_count=0, last_tick=None)
            for row in touched
        ]


@router.get("/contests/{contest_id}/players", response_model=List[PlayerOut])
def list_players(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[PlayerOut]:
    with ctx.db.session() as session:
        players = list(
            session.execute(
                select(Player).where(Player.contest_id == contest_id).order_by(Player.player_no)
            ).scalars()
        )
        agent_counts = dict(
            session.execute(
                select(Agent.player_id, func.count(Agent.id))
                .where(Agent.revoked_at.is_(None))
                .group_by(Agent.player_id)
            ).all()
        )
        file_counts = dict(
            session.execute(
                select(SourceFile.player_id, func.count(SourceFile.id))
                .where(SourceFile.deleted_at.is_(None))
                .group_by(SourceFile.player_id)
            ).all()
        )

    # 在线状态走内存注册表，不查库
    runtime_by_player = {a.player_id: a for a in ctx.registry.all(contest_id)}

    result = []
    for player in players:
        runtime = runtime_by_player.get(player.id)
        result.append(
            _player_out(
                player,
                has_agent=bool(agent_counts.get(player.id, 0)),
                online=bool(runtime and runtime.online),
                file_count=file_counts.get(player.id, 0),
                last_tick=runtime.last_tick_at if runtime else None,
            )
        )
    return result


def _player_out(
    player: Player,
    has_agent: bool,
    online: bool,
    file_count: int,
    last_tick,
) -> PlayerOut:
    return PlayerOut(
        id=player.id,
        contest_id=player.contest_id,
        player_no=player.player_no,
        name=player.name,
        seat=player.seat,
        group_name=player.group_name,
        has_agent=has_agent,
        online=online,
        file_count=file_count,
        last_tick_at=_iso(last_tick),
    )


# --------------------------------------------------------------------------- #
# 注册码
# --------------------------------------------------------------------------- #


@router.post("/players/{player_id}/enroll-code", response_model=EnrollCodeOut)
def issue_enroll_code(
    player_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> EnrollCodeOut:
    """签发（或重新签发）该选手的注册码。

    注册码长期有效、可重复使用 —— 它是"机器凭据种子"，供快照还原后自愈。
    重新签发会吊销该选手此前所有未绑定的注册码。
    """
    now = utcnow()
    raw_code = new_enroll_code(ctx.settings.enroll_code_bytes)

    with ctx.db.session() as session:
        player = session.get(Player, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="选手不存在")

        for old in session.execute(
            select(EnrollCode).where(
                EnrollCode.player_id == player_id,
                EnrollCode.revoked_at.is_(None),
            )
        ).scalars():
            old.revoked_at = now

        session.add(
            EnrollCode(
                code_hash=hash_enroll_code(raw_code),
                player_id=player_id,
                note="为选手 %s 签发" % player.player_no,
            )
        )
        return EnrollCodeOut(
            player_id=player_id,
            player_no=player.player_no,
            code=raw_code,
            expires_at=None,
            note="长期有效；机器还原后可重复使用",
        )


# --------------------------------------------------------------------------- #
# 在线状态 / 文件台账 / 审计
# --------------------------------------------------------------------------- #


@router.get("/contests/{contest_id}/agents", response_model=List[AgentRuntimeOut])
def list_agents(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[AgentRuntimeOut]:
    return [AgentRuntimeOut(**a.to_public_dict()) for a in ctx.registry.all(contest_id)]


@router.get("/contests/{contest_id}/files", response_model=List[SourceFileOut])
def list_files(
    contest_id: int,
    player_id: Optional[int] = None,
    include_deleted: bool = False,
    limit: int = 500,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[SourceFileOut]:
    limit = max(1, min(limit, 5000))
    with ctx.db.session() as session:
        stmt = (
            select(SourceFile, Player.player_no)
            .join(Player, SourceFile.player_id == Player.id)
            .where(Player.contest_id == contest_id)
        )
        if player_id is not None:
            stmt = stmt.where(SourceFile.player_id == player_id)
        if not include_deleted:
            stmt = stmt.where(SourceFile.deleted_at.is_(None))
        stmt = stmt.order_by(SourceFile.player_id, SourceFile.rel_path).limit(limit)

        return [
            SourceFileOut(
                id=row.id,
                player_id=row.player_id,
                player_no=player_no,
                rel_path=row.rel_path,
                sha256=row.sha256,
                size=row.size,
                revision=row.revision,
                content_stored=row.content_stored,
                first_seen_at=_iso(row.first_seen_at) or "",
                last_seen_at=_iso(row.last_seen_at) or "",
                deleted_at=_iso(row.deleted_at),
            )
            for row, player_no in session.execute(stmt)
        ]


@router.get("/contests/{contest_id}/events", response_model=List[EventOut])
def list_events(
    contest_id: int,
    limit: int = 200,
    category: Optional[str] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[EventOut]:
    limit = max(1, min(limit, 2000))
    with ctx.db.session() as session:
        stmt = (
            select(EventLog, Player.player_no)
            .outerjoin(Player, EventLog.player_id == Player.id)
            .where(EventLog.contest_id == contest_id)
        )
        if category:
            stmt = stmt.where(EventLog.category == category)
        stmt = stmt.order_by(EventLog.id.desc()).limit(limit)

        out = []
        for row, player_no in session.execute(stmt):
            meta = None
            if row.meta_json:
                try:
                    meta = json.loads(row.meta_json)
                except ValueError:
                    meta = {"_raw": row.meta_json[:500]}
            out.append(
                EventOut(
                    id=row.id,
                    ts=_iso(row.ts) or "",
                    level=row.level,
                    category=row.category,
                    player_no=player_no,
                    message=row.message,
                    meta=meta,
                )
            )
        return out


@router.get("/health")
def health(ctx: AppContext = Depends(get_ctx)) -> Dict[str, Any]:
    agents = ctx.registry.all()
    return {
        "ok": ctx.db.healthcheck(),
        "agents_total": len(agents),
        "agents_online": sum(1 for a in agents if a.online),
        "data_root": str(ctx.settings.data_root),
    }


# --------------------------------------------------------------------------- #
# 资产（下发文件的源）
# --------------------------------------------------------------------------- #


@router.post("/contests/{contest_id}/assets", response_model=AssetOut)
def upload_asset(
    contest_id: int,
    file: UploadFile = File(...),
    kind: str = "testdata",
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetOut:
    """上传一个待下发文件。

    走与服务端回收同一套内容寻址存储：相同内容只占一份磁盘。因此"给全场下发
    同一份 500MB 测试点"实际只消耗 500MB，而不是 50 × 500MB。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

    filename = Path(file.filename or "unnamed").name
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    try:
        sha256, size = ctx.blobs.put_stream(
            file.file,
            max_bytes=ctx.settings.max_asset_size,
        )
    except BlobTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except HashMismatch as exc:  # pragma: no cover - 未声明哈希时不会触发
        raise HTTPException(status_code=400, detail=str(exc))

    with ctx.db.session() as session:
        existing = session.execute(
            select(Asset).where(
                Asset.contest_id == contest_id,
                Asset.sha256 == sha256,
                Asset.filename == filename,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _asset_out(existing)

        asset = Asset(
            contest_id=contest_id, sha256=sha256, size=size,
            filename=filename, kind=kind[:32],
        )
        session.add(asset)
        session.flush()
        return _asset_out(asset)


@router.get("/contests/{contest_id}/assets", response_model=List[AssetOut])
def list_assets(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[AssetOut]:
    with ctx.db.session() as session:
        rows = session.execute(
            select(Asset).where(Asset.contest_id == contest_id).order_by(Asset.id.desc())
        ).scalars()
        return [_asset_out(row) for row in rows]


def _asset_out(asset: Asset) -> AssetOut:
    return AssetOut(
        id=asset.id,
        contest_id=asset.contest_id,
        sha256=asset.sha256,
        size=int(asset.size),
        filename=asset.filename,
        kind=asset.kind,
        created_at=_iso(asset.created_at) or "",
    )


# --------------------------------------------------------------------------- #
# 下发任务
# --------------------------------------------------------------------------- #


@router.post("/contests/{contest_id}/deploys", response_model=DeployTaskOut)
def create_deploy(
    contest_id: int,
    payload: DeployCreate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> DeployTaskOut:
    """创建下发任务。

    注意这里**只建任务**，不推送。Agent 会在下一次 tick 里自己领走 ——
    轮询架构下服务端没有"推"这个动作，这也意味着离线机器重连后会自动补上。
    """
    # 顺序很重要：先剥掉末尾多余的斜杠（"exam/" 是教师的自然写法），
    # 再把剩下的部分交给校验。
    #
    # 反过来先 strip("/") 再校验是错的 —— 那会把 "/etc" 就地洗成 "etc"
    # 然后顺利通过，等于校验形同虚设。落点虽然仍被限制在 deploy_root 内，
    # 但"绝对路径"这个明确错误意图被静默重新解释了，教师无从察觉。
    raw_dest = (payload.dest_dir or "").strip().rstrip("/")
    if raw_dest:
        try:
            dest_dir = validate_relpath(raw_dest, max_length=512)
        except PathValidationError as exc:
            raise HTTPException(status_code=400, detail="目标目录不合法: %s" % exc)
    else:
        dest_dir = ""

    if payload.mode not in ("overwrite", "skip_exist"):
        raise HTTPException(status_code=400, detail="mode 只能是 overwrite 或 skip_exist")

    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")
        asset = session.get(Asset, payload.asset_id)
        if asset is None or asset.contest_id != contest_id:
            raise HTTPException(status_code=404, detail="资源不存在或不属于本场次")

        players = _resolve_target_players(session, contest_id, payload)
        if not players:
            raise HTTPException(status_code=400, detail="下发目标为空，没有匹配到任何选手")

        task = DeployTask(
            contest_id=contest_id,
            asset_id=asset.id,
            target_kind=payload.target_kind,
            target_desc=payload.target_group or _describe_targets(payload, players),
            dest_dir=dest_dir,
            mode=payload.mode,
            status=DeployStatus.PENDING,
        )
        session.add(task)
        session.flush()

        for player in players:
            session.add(
                DeployTarget(
                    task_id=task.id,
                    player_id=player.id,
                    status=DeployStatus.PENDING,
                )
            )
        session.flush()

        session.add(
            EventLog(
                level="info",
                category="deploy_created",
                contest_id=contest_id,
                message="下发 %s 给 %d 名选手，目标目录 %s"
                % (asset.filename, len(players), dest_dir or "<根目录>"),
            )
        )
        return _deploy_task_out(session, task, asset, include_targets=False)


def _resolve_target_players(session, contest_id: int, payload: DeployCreate):
    if payload.target_kind == "all":
        return list(
            session.execute(
                select(Player).where(Player.contest_id == contest_id).order_by(Player.player_no)
            ).scalars()
        )
    if payload.target_kind == "player":
        if not payload.player_ids:
            raise HTTPException(status_code=400, detail="target_kind=player 时必须给 player_ids")
        return list(
            session.execute(
                select(Player).where(
                    Player.contest_id == contest_id,
                    Player.id.in_(payload.player_ids),
                )
            ).scalars()
        )
    if payload.target_kind == "group":
        if not payload.target_group:
            raise HTTPException(status_code=400, detail="target_kind=group 时必须给 target_group")
        return list(
            session.execute(
                select(Player).where(
                    Player.contest_id == contest_id,
                    Player.group_name == payload.target_group,
                )
            ).scalars()
        )
    raise HTTPException(status_code=400, detail="target_kind 只能是 all / player / group")


def _describe_targets(payload: DeployCreate, players) -> str:
    if payload.target_kind == "all":
        return "全员 %d 人" % len(players)
    if payload.target_kind == "group":
        return "分组「%s」 %d 人" % (payload.target_group, len(players))
    return "指定 %d 人" % len(players)


@router.get("/contests/{contest_id}/deploys", response_model=List[DeployTaskOut])
def list_deploys(
    contest_id: int,
    include_targets: bool = False,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[DeployTaskOut]:
    with ctx.db.session() as session:
        tasks = list(
            session.execute(
                select(DeployTask)
                .where(DeployTask.contest_id == contest_id)
                .order_by(DeployTask.id.desc())
            ).scalars()
        )
        out = []
        for task in tasks:
            asset = session.get(Asset, task.asset_id)
            out.append(_deploy_task_out(session, task, asset, include_targets=include_targets))
        return out


@router.get("/deploys/{task_id}", response_model=DeployTaskOut)
def get_deploy(
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> DeployTaskOut:
    with ctx.db.session() as session:
        task = session.get(DeployTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="下发任务不存在")
        asset = session.get(Asset, task.asset_id)
        return _deploy_task_out(session, task, asset, include_targets=True)


@router.post("/deploys/{task_id}/cancel", response_model=SimpleAck)
def cancel_deploy(
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """取消下发。

    只把**未完成**的目标置为 cancelled —— 已经收下文件的机器不回滚。要求 Agent
    删掉已下发的文件是危险动作（可能误删教师自己放的文件），不做。
    """
    with ctx.db.session() as session:
        task = session.get(DeployTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="下发任务不存在")
        if task.status == DeployStatus.DONE:
            raise HTTPException(status_code=409, detail="任务已完成，无法取消")

        affected = 0
        for target in session.execute(
            select(DeployTarget).where(
                DeployTarget.task_id == task_id,
                DeployTarget.status.in_((DeployStatus.PENDING, DeployStatus.READY)),
            )
        ).scalars():
            target.status = DeployStatus.CANCELLED
            affected += 1
        task.status = DeployStatus.CANCELLED
        session.add(
            EventLog(
                level="info",
                category="deploy_cancelled",
                contest_id=task.contest_id,
                message="下发任务 #%d 已取消，%d 个未完成目标被终止" % (task_id, affected),
            )
        )
    return SimpleAck(ok=True, detail="已取消 %d 个未完成目标" % affected)


@router.post("/deploys/{task_id}/retry", response_model=SimpleAck)
def retry_deploy(
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """把失败的目标重置为待下发，让 Agent 下一轮重新领取。"""
    with ctx.db.session() as session:
        task = session.get(DeployTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="下发任务不存在")

        affected = 0
        for target in session.execute(
            select(DeployTarget).where(
                DeployTarget.task_id == task_id,
                DeployTarget.status == DeployStatus.FAILED,
            )
        ).scalars():
            target.status = DeployStatus.PENDING
            target.retry_count += 1
            target.last_error = None
            affected += 1
        if affected:
            task.status = DeployStatus.PENDING
    return SimpleAck(ok=True, detail="已重置 %d 个失败目标" % affected)


def _deploy_task_out(session, task: DeployTask, asset, include_targets: bool) -> DeployTaskOut:
    rows = list(
        session.execute(
            select(DeployTarget, Player.player_no)
            .join(Player, DeployTarget.player_id == Player.id)
            .where(DeployTarget.task_id == task.id)
            .order_by(Player.player_no)
        )
    )
    counts: Dict[str, int] = {}
    targets: List[DeployTargetOut] = []
    for target, player_no in rows:
        counts[target.status] = counts.get(target.status, 0) + 1
        if include_targets:
            targets.append(
                DeployTargetOut(
                    player_id=target.player_id,
                    player_no=player_no,
                    status=target.status,
                    bytes_done=int(target.bytes_done),
                    retry_count=int(target.retry_count),
                    last_error=target.last_error,
                )
            )

    return DeployTaskOut(
        id=task.id,
        contest_id=task.contest_id,
        asset_id=task.asset_id,
        filename=asset.filename if asset else "<已删除>",
        size=int(asset.size) if asset else 0,
        sha256=asset.sha256 if asset else "",
        target_kind=task.target_kind,
        dest_dir=task.dest_dir,
        mode=task.mode,
        status=_derive_task_status(task, counts),
        created_at=_iso(task.created_at) or "",
        total=len(rows),
        done=counts.get(DeployStatus.DONE, 0),
        failed=counts.get(DeployStatus.FAILED, 0),
        pending=(
            counts.get(DeployStatus.PENDING, 0)
            + counts.get(DeployStatus.READY, 0)
            + counts.get(DeployStatus.CANCELLED, 0)
        ),
        targets=targets,
    )


def _derive_task_status(task: DeployTask, counts: Dict[str, int]) -> str:
    """任务状态由目标聚合得出，而不是单独维护 —— 避免两者不一致。

    单独维护会出现的典型问题：某个目标失败后任务状态仍是 pending，Web 上
    进度条卡在 90% 永远不动。
    """
    if task.status == DeployStatus.CANCELLED:
        return DeployStatus.CANCELLED
    total = sum(counts.values())
    if total == 0:
        return task.status
    if counts.get(DeployStatus.DONE, 0) == total:
        return DeployStatus.DONE
    if counts.get(DeployStatus.FAILED, 0) == total:
        return DeployStatus.FAILED
    if counts.get(DeployStatus.DONE, 0) + counts.get(DeployStatus.FAILED, 0) == total:
        return DeployStatus.FAILED  # 部分失败
    return DeployStatus.PENDING


# --------------------------------------------------------------------------- #
# 评测成绩
# --------------------------------------------------------------------------- #


@router.get("/contests/{contest_id}/scores", response_model=ScoreMatrixOut)
def score_matrix(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ScoreMatrixOut:
    """「选手 × 题目」成绩矩阵。

    **区分"0 分"和"没有成绩"**：``parse_status`` 为 ``missing`` 表示从未收到
    结果，``unparsed`` 表示收到了但看不懂。界面上这两者都不该显示成 0 ——
    那会让教师以为选手考砸了。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

        players = list(
            session.execute(
                select(Player).where(Player.contest_id == contest_id).order_by(Player.player_no)
            ).scalars()
        )
        runs = list(
            session.execute(select(JudgeRun).where(JudgeRun.contest_id == contest_id)).scalars()
        )

    problems = sorted({run.problem for run in runs})
    by_key = {(run.player_id, run.problem): run for run in runs}

    rows: List[ScoreRowOut] = []
    unparsed = 0
    for player in players:
        cells: List[ScoreCellOut] = []
        total = 0
        for problem in problems:
            run = by_key.get((player.id, problem))
            if run is None:
                cells.append(ScoreCellOut(problem=problem, parse_status="missing"))
                continue
            if run.parse_status == "unparsed":
                unparsed += 1
            if run.score:
                total += run.score
            cells.append(
                ScoreCellOut(
                    problem=problem,
                    score=run.score,
                    max_score=run.max_score,
                    status=run.status,
                    parse_status=run.parse_status,
                    detail=run.detail,
                    updated_at=_iso(run.updated_at),
                )
            )
        rows.append(
            ScoreRowOut(
                player_id=player.id,
                player_no=player.player_no,
                player_name=player.name,
                total=total,
                cells=cells,
            )
        )

    return ScoreMatrixOut(
        contest_id=contest_id,
        problems=problems,
        rows=rows,
        unparsed=unparsed,
        complete=unparsed == 0,
    )


@router.get("/contests/{contest_id}/judge/runs", response_model=List[JudgeRunOut])
def list_judge_runs(
    contest_id: int,
    parse_status: Optional[str] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[JudgeRunOut]:
    with ctx.db.session() as session:
        stmt = (
            select(JudgeRun, Player.player_no)
            .join(Player, JudgeRun.player_id == Player.id)
            .where(JudgeRun.contest_id == contest_id)
        )
        if parse_status:
            stmt = stmt.where(JudgeRun.parse_status == parse_status)
        stmt = stmt.order_by(Player.player_no, JudgeRun.problem)

        return [
            JudgeRunOut(
                id=run.id,
                player_id=run.player_id,
                player_no=player_no,
                problem=run.problem,
                score=run.score,
                max_score=run.max_score,
                status=run.status,
                parse_status=run.parse_status,
                detail=run.detail,
                source_path=run.source_path,
                updated_at=_iso(run.updated_at) or "",
            )
            for run, player_no in session.execute(stmt)
        ]


@router.post("/contests/{contest_id}/judge/rescan", response_model=JudgeScanOut)
def rescan_judge_results(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeScanOut:
    """立即重扫成绩目录（忽略 mtime 缓存）。"""
    from ..services.judge import scan_contest_results

    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")
        report = scan_contest_results(session, ctx.settings, contest, force=True)
        return JudgeScanOut(
            parsed=report.parsed,
            unparsed=report.unparsed,
            unchanged=report.unchanged,
            skipped=report.skipped,
            manual=report.manual,
            errors=report.errors[:50],
        )


@router.put("/contests/{contest_id}/judge/score", response_model=JudgeRunOut)
def set_judge_score(
    contest_id: int,
    payload: ManualScoreIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> JudgeRunOut:
    """教师手工录入或修正成绩。

    这是"解析器覆盖不到"的兜底出口。手工录入的记录会标记成 ``manual``，
    后续自动扫描**不会覆盖**它 —— 教师既然亲手改了，就该由教师负责。
    """
    problem = (payload.problem or "").strip()
    if not problem:
        raise HTTPException(status_code=400, detail="题目标识不能为空")

    with ctx.db.session() as session:
        player = session.get(Player, payload.player_id)
        if player is None or player.contest_id != contest_id:
            raise HTTPException(status_code=404, detail="选手不存在或不属于本场次")

        run = session.execute(
            select(JudgeRun).where(
                JudgeRun.contest_id == contest_id,
                JudgeRun.player_id == payload.player_id,
                JudgeRun.problem == problem,
            )
        ).scalar_one_or_none()

        if run is None:
            run = JudgeRun(
                contest_id=contest_id,
                player_id=payload.player_id,
                problem=problem,
                scanned_at=utcnow(),
            )
            session.add(run)

        run.score = payload.score
        run.max_score = payload.max_score
        run.status = payload.status
        run.parse_status = "manual"
        run.detail = "教师手工录入"
        run.updated_at = utcnow()
        session.flush()

        session.add(
            EventLog(
                level="info",
                category="judge_manual",
                contest_id=contest_id,
                player_id=payload.player_id,
                message="手工录入成绩：%s %s = %s"
                % (player.player_no, problem, payload.score),
            )
        )

        return JudgeRunOut(
            id=run.id,
            player_id=run.player_id,
            player_no=player.player_no,
            problem=run.problem,
            score=run.score,
            max_score=run.max_score,
            status=run.status,
            parse_status=run.parse_status,
            detail=run.detail,
            source_path=run.source_path,
            updated_at=_iso(run.updated_at) or "",
        )


@router.delete("/contests/{contest_id}/judge/score", response_model=SimpleAck)
def clear_judge_score(
    contest_id: int,
    player_id: int,
    problem: str,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    with ctx.db.session() as session:
        run = session.execute(
            select(JudgeRun).where(
                JudgeRun.contest_id == contest_id,
                JudgeRun.player_id == player_id,
                JudgeRun.problem == problem,
            )
        ).scalar_one_or_none()
        if run is None:
            return SimpleAck(ok=True, detail="没有该记录")
        session.delete(run)
    return SimpleAck(ok=True, detail="已清除")
