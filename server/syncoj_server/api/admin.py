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

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, select

from ..config import Settings
from ..context import AppContext
from ..models import (
    Admin,
    AdminSession,
    Agent,
    AgentRelease,
    Asset,
    BootstrapKey,
    Contest,
    ContestStatus,
    DeployStatus,
    DeployTarget,
    DeployTask,
    EnrollmentMode,
    EnrollCode,
    EventLog,
    JudgeRun,
    MachineClaim,
    Player,
    Problem,
    Roster,
    RosterEntry,
    SourceFile,
    utcnow,
)
from ..paths import PathValidationError, safe_join, slugify, validate_relpath
from ..schemas import (
    AdminInfo,
    AgentRuntimeOut,
    ApplyRosterIn,
    ApplyRosterOut,
    AssetOut,
    ContestCreate,
    ContestOut,
    ContestUpdate,
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
    ProblemColumnOut,
    ProblemImportOut,
    ProblemMatchIn,
    ProblemMatchOut,
    ProblemOut,
    ProblemUpsert,
    ReleaseOut,
    ReleaseUpdate,
    RosterCreate,
    RosterDetailOut,
    RosterEntryIn,
    RosterEntryOut,
    RosterImportOut,
    RosterOut,
    ScoreCellOut,
    ScoreMatrixOut,
    ScoreRowOut,
    SimpleAck,
    SourceFileOut,
    UpgradeStatusOut,
)
from ..security import (
    hash_enroll_code,
    hash_password,
    hash_token,
    new_enroll_code,
    new_token,
    verify_password,
)
from ..services import matching, rosters
from ..services.deploy import validate_dest_template
from ..services.signing import parse_version
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
    mode = _validate_enrollment_mode(payload.enrollment_mode)

    with ctx.db.session() as session:
        if session.execute(select(Contest).where(Contest.slug == slug)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="场次标识已存在: %s" % slug)
        roster = _resolve_roster(session, payload.default_roster_id)
        contest = Contest(
            slug=slug,
            name=payload.name,
            status=status_value,
            note=payload.note,
            default_roster_id=roster.id if roster else None,
            enrollment_mode=mode,
        )
        session.add(contest)
        session.flush()
        return _contest_out(contest, player_count=0, online_count=0, roster=roster)


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
        rosters = {row.id: row for row in session.execute(select(Roster)).scalars()}
        contests = list(session.execute(select(Contest).order_by(Contest.id)).scalars())
        result = []
        for contest in contests:
            online = sum(1 for a in ctx.registry.all(contest.id) if a.online)
            result.append(
                _contest_out(
                    contest,
                    counts.get(contest.id, 0),
                    online,
                    roster=rosters.get(contest.default_roster_id),
                )
            )
        return result


@router.patch("/contests/{contest_id}", response_model=ContestOut)
def update_contest(
    contest_id: int,
    payload: ContestUpdate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ContestOut:
    """改场次设置。只改传了的字段。

    ``default_roster_id`` 有个特殊之处：``None`` 没法区分"没传"和"要清空"。
    所以清空要靠 ``clear_default_roster`` 这个显式开关 —— 否则教师一次选错
    名单就再也改不回来了。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

        if payload.name is not None:
            contest.name = payload.name
        if payload.note is not None:
            contest.note = payload.note
        if payload.status is not None:
            if payload.status not in ContestStatus.ALL:
                raise HTTPException(status_code=400, detail="未知的场次状态: %s" % payload.status)
            contest.status = payload.status
        if payload.enrollment_mode is not None:
            contest.enrollment_mode = _validate_enrollment_mode(payload.enrollment_mode)

        roster = None
        if payload.clear_default_roster:
            contest.default_roster_id = None
        elif payload.default_roster_id is not None:
            roster = _resolve_roster(session, payload.default_roster_id)
            contest.default_roster_id = roster.id
        elif contest.default_roster_id is not None:
            roster = session.get(Roster, contest.default_roster_id)

        session.flush()
        player_count = session.execute(
            select(func.count(Player.id)).where(Player.contest_id == contest_id)
        ).scalar_one()
        online = sum(1 for a in ctx.registry.all(contest_id) if a.online)
        return _contest_out(contest, player_count, online, roster=roster)


def _validate_enrollment_mode(raw: Optional[str]) -> str:
    """校验注册方式。空值按原有行为理解 —— 老场次没这个字段。"""
    if raw is None or raw == "":
        return EnrollmentMode.PER_PLAYER_CODE
    if raw not in EnrollmentMode.ALL:
        raise HTTPException(
            status_code=400,
            detail="未知的注册方式: %s（可选 %s）" % (raw, " / ".join(EnrollmentMode.ALL)),
        )
    return raw


def _resolve_roster(session, roster_id: Optional[int]) -> Optional[Roster]:
    if roster_id is None:
        return None
    roster = session.get(Roster, roster_id)
    if roster is None:
        raise HTTPException(status_code=404, detail="名单不存在: %s" % roster_id)
    return roster


def _contest_out(
    contest: Contest,
    player_count: int,
    online_count: int,
    roster: Optional[Roster] = None,
) -> ContestOut:
    return ContestOut(
        id=contest.id,
        slug=contest.slug,
        name=contest.name,
        status=contest.status,
        player_count=player_count,
        online_count=online_count,
        created_at=_iso(contest.created_at) or "",
        default_roster_id=contest.default_roster_id,
        default_roster_name=(roster.name if roster is not None else None),
        enrollment_mode=contest.effective_enrollment_mode,
    )


# --------------------------------------------------------------------------- #
# 名单库
# --------------------------------------------------------------------------- #


def _roster_out(roster: Roster, entry_count: int) -> RosterOut:
    return RosterOut(
        id=roster.id,
        name=roster.name,
        note=roster.note,
        created_at=_iso(roster.created_at) or "",
        entry_count=entry_count,
    )


def _first_overlong(*candidates) -> Optional[str]:
    """返回第一个超长的字段说明，都合规就返回 None。

    ``candidates`` 是 ``(值, 上限, 字段名)`` 三元组。抽成一个小函数是因为
    校验逻辑一旦散在三处，早晚会漏掉一处，而漏掉的表现是"数据静默超长"。
    """
    for value, limit, label in candidates:
        if value is not None and len(value) > limit:
            return "%s 超过 %d 字符" % (label, limit)
    return None


def _roster_entry_out(entry: RosterEntry) -> RosterEntryOut:
    return RosterEntryOut(
        id=entry.id,
        roster_id=entry.roster_id,
        player_no=entry.player_no,
        name=entry.name,
        seat=entry.seat,
        group_name=entry.group_name,
    )


@router.get("/rosters", response_model=List[RosterOut])
def list_rosters(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[RosterOut]:
    with ctx.db.session() as session:
        counts = dict(
            session.execute(
                select(RosterEntry.roster_id, func.count(RosterEntry.id)).group_by(
                    RosterEntry.roster_id
                )
            ).all()
        )
        rows = session.execute(select(Roster).order_by(Roster.name)).scalars()
        return [_roster_out(row, counts.get(row.id, 0)) for row in rows]


@router.post("/rosters", response_model=RosterOut)
def create_roster(
    payload: RosterCreate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名单名称不能为空")
    with ctx.db.session() as session:
        if session.execute(select(Roster).where(Roster.name == name)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="名单名称已存在: %s" % name)
        roster = Roster(name=name, note=payload.note)
        session.add(roster)
        session.flush()
        return _roster_out(roster, 0)


@router.get("/rosters/{roster_id}", response_model=RosterDetailOut)
def get_roster(
    roster_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterDetailOut:
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise HTTPException(status_code=404, detail="名单不存在")
        entries = list(
            session.execute(
                select(RosterEntry)
                .where(RosterEntry.roster_id == roster_id)
                .order_by(RosterEntry.player_no)
            ).scalars()
        )
        return RosterDetailOut(
            **_roster_out(roster, len(entries)).model_dump(),
            entries=[_roster_entry_out(entry) for entry in entries],
        )


@router.patch("/rosters/{roster_id}", response_model=RosterOut)
def update_roster(
    roster_id: int,
    payload: RosterCreate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名单名称不能为空")
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise HTTPException(status_code=404, detail="名单不存在")
        clash = session.execute(
            select(Roster).where(Roster.name == name, Roster.id != roster_id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail="名单名称已存在: %s" % name)
        roster.name = name
        roster.note = payload.note
        session.flush()
        count = session.execute(
            select(func.count(RosterEntry.id)).where(RosterEntry.roster_id == roster_id)
        ).scalar_one()
        return _roster_out(roster, count)


@router.delete("/rosters/{roster_id}", response_model=SimpleAck)
def delete_roster(
    roster_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删除名单。

    **只删名单本身，不动任何场次的选手。** 名单是模板，场次的参赛者是从它
    复制出去的一份独立数据 —— 删模板不该牵动已发生的比赛。引用了这份名单的
    场次会被置空（``ON DELETE SET NULL``），只是"没预设名单了"而已。
    """
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            return SimpleAck(ok=True, detail="名单不存在")
        name = roster.name
        referenced = session.execute(
            select(func.count(Contest.id)).where(Contest.default_roster_id == roster_id)
        ).scalar_one()
        session.delete(roster)
    detail = "已删除名单 %s" % name
    if referenced:
        detail += "（%d 个场次仍保留各自的选手，只是不再指向这份名单）" % referenced
    return SimpleAck(ok=True, detail=detail)


@router.post("/rosters/{roster_id}/entries", response_model=RosterImportOut)
def import_roster_entries(
    roster_id: int,
    payload: List[RosterEntryIn],
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterImportOut:
    """批量登记/更新名单条目。按 ``player_no`` 幂等 upsert。

    和题目导入一样逐条容错：某一行不合法只跳过那一行并在 ``errors`` 里说明。
    一份名单几十上百行，因为一个空格全部白填是没人能接受的。
    """
    if not payload:
        return RosterImportOut()
    if len(payload) > 5000:
        raise HTTPException(status_code=413, detail="单次最多导入 5000 条")

    result = RosterImportOut()
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise HTTPException(status_code=404, detail="名单不存在")

        existing = {
            row.player_no: row
            for row in session.execute(
                select(RosterEntry).where(RosterEntry.roster_id == roster_id)
            ).scalars()
        }
        seen = set()
        for offset, item in enumerate(payload):
            line_no = offset + 1
            player_no = item.player_no.strip()
            if not player_no:
                result.skipped += 1
                result.errors.append("第 %d 行：选手编号为空" % line_no)
                continue
            if len(player_no) > 64:
                result.skipped += 1
                result.errors.append("第 %d 行：选手编号超过 64 字符" % line_no)
                continue
            # 姓名字段在库里是 VARCHAR(64/32)，超了 SQLite 也不会报错，只会静默
            # 存进去。宁可在这里逐条拒绝 —— 同一份名单重新导一次就能修好
            too_long = _first_overlong(
                (item.name, 64, "姓名"), (item.seat, 32, "座位"), (item.group_name, 64, "分组")
            )
            if too_long:
                result.skipped += 1
                result.errors.append("第 %d 行：%s" % (line_no, too_long))
                continue
            if player_no in seen:
                result.skipped += 1
                result.errors.append("第 %d 行：%s 在本次导入中重复" % (line_no, player_no))
                continue
            seen.add(player_no)

            row = existing.get(player_no)
            if row is None:
                row = RosterEntry(roster_id=roster_id, player_no=player_no)
                session.add(row)
                existing[player_no] = row
                result.created += 1
            else:
                result.updated += 1
            row.name = item.name
            row.seat = item.seat
            row.group_name = item.group_name
            session.flush()
            result.entries.append(_roster_entry_out(row))
    return result


@router.delete("/roster-entries/{entry_id}", response_model=SimpleAck)
def delete_roster_entry(
    entry_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    with ctx.db.session() as session:
        entry = session.get(RosterEntry, entry_id)
        if entry is None:
            return SimpleAck(ok=True, detail="条目不存在")
        player_no = entry.player_no
        session.delete(entry)
    return SimpleAck(ok=True, detail="已从名单中移除 %s" % player_no)


@router.post("/contests/{contest_id}/players/apply-roster", response_model=ApplyRosterOut)
def apply_roster_to_contest(
    contest_id: int,
    payload: ApplyRosterIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ApplyRosterOut:
    """把名单应用到场次：名单里有而场次里没有的补上，已有的更新，默认不删人。

    ``prune=True`` 时会删掉"名单里没有"的选手，但**有代码或成绩的一个都不动**，
    并在 ``protected`` 里列出来告诉教师"这些人删不掉、也不该删"。

    名单是模板，场次是从它复制出去的独立数据 —— 这个动作是**显式**的，
    改了名单不会自动影响任何场次。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

        roster_id = payload.roster_id or contest.default_roster_id
        if roster_id is None:
            raise HTTPException(
                status_code=400,
                detail="这个场次还没指定名单，也没有传 roster_id —— 没什么可应用的",
            )
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise HTTPException(status_code=404, detail="名单不存在: %s" % roster_id)

        entries = list(
            session.execute(
                select(RosterEntry)
                .where(RosterEntry.roster_id == roster_id)
                .order_by(RosterEntry.player_no)
            ).scalars()
        )
        report = rosters.apply_roster(session, contest_id, roster, entries, payload.prune)

        session.add(
            EventLog(
                level="info",
                category="roster_apply",
                contest_id=contest_id,
                message="应用名单「%s」：新增 %d，更新 %d，保留 %d%s"
                % (
                    roster.name,
                    report.created,
                    report.updated,
                    report.kept,
                    "，清理 %d" % report.pruned if report.pruned else "",
                ),
            )
        )

        return ApplyRosterOut(
            roster_id=roster.id,
            roster_name=roster.name,
            created=report.created,
            updated=report.updated,
            kept=report.kept,
            pruned=report.pruned,
            protected=list(report.protected),
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
        # 归属是**算出来的**，不是存下来的：教师改一个模式，界面上立刻跟着变，
        # 不需要重收文件，也不用担心存量数据里的旧归属变成脏数据
        rules = contest_problem_rules(session, contest_id, ctx.settings)
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
                problem=matching.match_problem(row.rel_path, rules),
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
    kind: str = Form("testdata"),
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
    #
    # dest_dir 支持 {player_no} 占位符（全员下发时每台机器的目标目录不同），
    # 校验时先把它换成一个合法样例再走路径规则，但**入库的是模板本身**。
    raw_dest = (payload.dest_dir or "").strip()
    if raw_dest:
        try:
            dest_dir = validate_dest_template(raw_dest)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
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
# 题目
# --------------------------------------------------------------------------- #


def _validate_problem_ident(raw: str) -> str:
    """校验题目标识。

    它会同时成为目录名、代码文件名与成绩矩阵列名，所以必须能安全用作**单个**
    路径段。这里先按路径规则校验，再额外禁止斜杠（题目名不该带层级）。
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="题目标识不能为空")
    if "/" in text:
        raise HTTPException(
            status_code=400, detail="题目标识不能包含斜杠：%s（它应当是单个目录名）" % text
        )
    try:
        validate_relpath(text, max_length=64)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail="题目标识不合法: %s" % exc)
    return text


@router.get("/contests/{contest_id}/problems", response_model=List[ProblemOut])
def list_problems(
    contest_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> List[ProblemOut]:
    with ctx.db.session() as session:
        rows = session.execute(
            select(Problem)
            .where(Problem.contest_id == contest_id)
            .order_by(Problem.order_index, Problem.ident)
        ).scalars()
        return [_problem_out(row, ctx.settings) for row in rows]


@router.post("/contests/{contest_id}/problems", response_model=ProblemImportOut)
def import_problems(
    contest_id: int,
    payload: List[ProblemUpsert],
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ProblemImportOut:
    """批量登记/更新题目。按 ``ident`` 幂等 upsert —— 名单可以反复导。

    单条不合法只跳过那一条并在 ``errors`` 里说明，不让整批失败 ——
    教师一次粘贴十道题，不该因为其中一个名字打错就全部白填。
    """
    if not payload:
        return ProblemImportOut()
    if len(payload) > 200:
        raise HTTPException(status_code=413, detail="单次最多登记 200 道题")

    result = ProblemImportOut()
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

        existing = {
            row.ident: row
            for row in session.execute(
                select(Problem).where(Problem.contest_id == contest_id)
            ).scalars()
        }
        # 未指定顺序时按提交顺序自动编号，避免全部堆在 0
        auto_order = max([row.order_index for row in existing.values()] + [0])

        for item in payload:
            try:
                ident = _validate_problem_ident(item.ident)
            except HTTPException as exc:
                result.errors.append("%s：%s" % (item.ident, exc.detail))
                continue

            # 先校验模式再落库。顺序反过来的话，"新建一行 → 模式不合法 → continue"
            # 会留下一条只写了一半的题目（session 退出时照样提交）
            try:
                patterns = matching.validate_patterns(item.file_patterns)
            except matching.PatternError as exc:
                result.errors.append("%s：模式不合法（%s）" % (ident, exc))
                continue

            row = existing.get(ident)
            if row is None:
                auto_order += 1
                row = Problem(
                    contest_id=contest_id,
                    ident=ident,
                    order_index=item.order_index or auto_order,
                )
                session.add(row)
                existing[ident] = row
                result.created += 1
            else:
                result.updated += 1
                if item.order_index:
                    row.order_index = item.order_index

            row.title = item.title or row.title
            row.note = item.note or row.note
            # 传了非空模式列表就整体替换（不是追加）—— 编辑时"删掉一个模式"
            # 必须能生效，追加语义做不到这一点
            if patterns:
                row.file_patterns = json.dumps(patterns, ensure_ascii=False)
            session.flush()
            result.problems.append(_problem_out(row, ctx.settings))

        if result.created or result.updated:
            session.add(
                EventLog(
                    level="info",
                    category="problem_import",
                    contest_id=contest_id,
                    message="题目清单更新：新增 %d 道，更新 %d 道"
                    % (result.created, result.updated),
                )
            )

    return result


@router.post("/contests/{contest_id}/problems/match", response_model=ProblemMatchOut)
def match_problem_path(
    contest_id: int,
    payload: ProblemMatchIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ProblemMatchOut:
    """试算「这条相对路径会被算成哪道题」。

    排错专用：教师配完模式想知道对不对，总得有个地方能问。放在服务端是因为
    **匹配只有一份实现** —— 前端自己算一遍，迟早会出现"界面说归 p1、
    实际归了 p2"这种谁也说不清的场面。

    只做匹配，不碰文件系统；``path`` 按普通字符串处理，不需要是已存在的文件。
    """
    with ctx.db.session() as session:
        rules = contest_problem_rules(session, contest_id, ctx.settings)

    ident = matching.match_problem(payload.path, rules)
    hit = next((rule for rule in rules if rule.ident == ident), None)
    if hit is None:
        return ProblemMatchOut(path=payload.path, problem=None)

    return ProblemMatchOut(
        path=payload.path,
        problem=hit.ident,
        patterns=list(hit.patterns),
        # 模板和展开结果都回 —— 教师写了 {title}/**，得能看见它变成了 签到题/**
        expanded=[
            matching.expand_pattern(p, hit.ident, hit.title) for p in hit.patterns
        ],
    )


@router.patch("/problems/{problem_id}", response_model=ProblemOut)
def update_problem(
    problem_id: int,
    payload: ProblemUpsert,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ProblemOut:
    with ctx.db.session() as session:
        row = session.get(Problem, problem_id)
        if row is None:
            raise HTTPException(status_code=404, detail="题目不存在")

        ident = _validate_problem_ident(payload.ident)
        if ident != row.ident:
            clash = session.execute(
                select(Problem).where(
                    Problem.contest_id == row.contest_id,
                    Problem.ident == ident,
                    Problem.id != problem_id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=409, detail="题目标识 %s 已被占用" % ident)
            row.ident = ident

        row.title = payload.title
        row.note = payload.note
        if payload.order_index:
            row.order_index = payload.order_index

        # 同 import：传了就整体替换。传空列表 = 回到默认模式
        try:
            patterns = matching.validate_patterns(payload.file_patterns)
        except matching.PatternError as exc:
            raise HTTPException(status_code=400, detail="模式不合法: %s" % exc)
        row.file_patterns = json.dumps(patterns, ensure_ascii=False) if patterns else None

        session.flush()
        return _problem_out(row, ctx.settings)


@router.delete("/problems/{problem_id}", response_model=SimpleAck)
def delete_problem(
    problem_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删除题目登记。

    只删"清单里的一条"，**不动已有的成绩记录** —— 已经收到的评测结果不该因为
    清单调整而消失。那些成绩会以"未登记"的形式继续显示在矩阵里。
    """
    with ctx.db.session() as session:
        row = session.get(Problem, problem_id)
        if row is None:
            return SimpleAck(ok=True, detail="没有该题目")
        ident = row.ident
        session.delete(row)
    return SimpleAck(ok=True, detail="已删除题目 %s（已有成绩记录保留）" % ident)


def _problem_out(row: Problem, settings: Settings) -> ProblemOut:
    return ProblemOut(
        id=row.id,
        contest_id=row.contest_id,
        ident=row.ident,
        title=row.title,
        order_index=int(row.order_index),
        note=row.note,
        # 把实际生效的模式一并给前端 —— 否则界面得自己推默认值，
        # 而默认值是可配置的（settings.default_file_pattern），前端推不出来
        file_patterns=problem_patterns(row, settings),
    )


def problem_patterns(row: Problem, settings: Settings) -> List[str]:
    """题目实际生效的 glob 模式模板。

    配了就用配的，没配就用服务端的默认模板。注意返回的是**模板本身**：
    ``{ident}`` / ``{title}`` 占位符原样保留，由 ``matching`` 在每次匹配时展开。

    这很关键：如果这里把占位符换成具体名字再返回，前端"打开编辑再保存"
    就会把 ``{ident}/**`` 冻成一个写死的 ``p1/**`` —— 之后改个题目标识，
    代码就再也认不出来了，而界面上看不出任何异常。
    """
    if not row.file_patterns:
        return [settings.default_file_pattern]

    try:
        stored = json.loads(row.file_patterns)
    except (TypeError, ValueError):
        log.warning("题目 %s 的 file_patterns 不是合法 JSON，回退到默认模式", row.ident)
        return [settings.default_file_pattern]

    if not isinstance(stored, list):
        return [settings.default_file_pattern]
    return [str(p) for p in stored if isinstance(p, str) and p.strip()]


def contest_problem_rules(
    session, contest_id: int, settings: Settings
) -> List[matching.ProblemRule]:
    """把某场次的题目整理成匹配规则，供归题使用。

    **顺序就是题目的顺序** —— 匹配时第一个命中的胜出。让顺序显式可预期，
    比"最具体者优先"这类隐式规则好排查。
    """
    rows = session.execute(
        select(Problem)
        .where(Problem.contest_id == contest_id)
        .order_by(Problem.order_index, Problem.ident)
    ).scalars()
    return [
        matching.ProblemRule(
            ident=row.ident, title=row.title, patterns=problem_patterns(row, settings)
        )
        for row in rows
    ]


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

    再往下还分一层：``missing`` 里要看清是**交了但还没评测**（``submitted``
    为真）还是**压根没交**。前者等着就行，后者得去问人 —— 教师看矩阵主要
    就是想知道哪几个座位该去催。
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
        # 列顺序：**先按教师的题目清单**（有稳定的人工顺序），再把只在评测结果里
        # 出现过的题目补在后面。补的那些要标出来 —— 它们可能是漏登记，
        # 也可能是历史遗留；无论哪种都不该被静默隐藏。
        declared = list(
            session.execute(
                select(Problem)
                .where(Problem.contest_id == contest_id)
                .order_by(Problem.order_index, Problem.ident)
            ).scalars()
        )
        rules = [
            matching.ProblemRule(
                ident=row.ident,
                title=row.title,
                patterns=problem_patterns(row, ctx.settings),
            )
            for row in declared
        ]

        # "交没交"只看当前活着的文件（deleted_at 为空）。已删除的不算交 ——
        # 删除是 Agent 明确上报的事实，不该被当成"曾经交过"永久保留
        submitted_pairs = set()
        for player_id, rel_path in session.execute(
            select(SourceFile.player_id, SourceFile.rel_path)
            .join(Player, SourceFile.player_id == Player.id)
            .where(Player.contest_id == contest_id, SourceFile.deleted_at.is_(None))
        ):
            ident = matching.match_problem(rel_path, rules)
            if ident is not None:
                submitted_pairs.add((player_id, ident))

        problems = sorted({run.problem for run in runs})
        declared_idents = [row.ident for row in declared]
        declared_set = set(declared_idents)
        undeclared = [name for name in problems if name not in declared_set]

        # 在会话内就把列构造好 —— 不要把 ORM 对象带出 with 块，
        # 那属于"现在能跑，改了 expire_on_commit 就炸"的写法
        columns = [
            ProblemColumnOut(ident=row.ident, title=row.title, declared=True) for row in declared
        ] + [ProblemColumnOut(ident=name, title=None, declared=False) for name in undeclared]

        by_key = {(run.player_id, run.problem): run for run in runs}

        rows: List[ScoreRowOut] = []
        unparsed = 0
        for player in players:
            cells: List[ScoreCellOut] = []
            total = 0
            for ident in declared_idents + undeclared:
                submitted = (player.id, ident) in submitted_pairs
                run = by_key.get((player.id, ident))
                if run is None:
                    cells.append(
                        ScoreCellOut(problem=ident, parse_status="missing", submitted=submitted)
                    )
                    continue
                if run.parse_status == "unparsed":
                    unparsed += 1
                if run.score:
                    total += run.score
                cells.append(
                    ScoreCellOut(
                        problem=ident,
                        score=run.score,
                        max_score=run.max_score,
                        status=run.status,
                        parse_status=run.parse_status,
                        submitted=submitted,
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
        columns=columns,
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


# --------------------------------------------------------------------------- #
# Agent 发布与自更新
# --------------------------------------------------------------------------- #


@router.get("/releases", response_model=UpgradeStatusOut)
def list_releases(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> UpgradeStatusOut:
    with ctx.db.session() as session:
        rows = list(
            session.execute(
                select(AgentRelease).order_by(AgentRelease.id.desc())
            ).scalars()
        )
        releases = [_release_out(row) for row in rows]
        active = next((r for r in releases if r.rolled_out and not r.yanked), None)

    return UpgradeStatusOut(
        signing_available=ctx.signing_key is not None,
        key_id=ctx.signing_key.key_id if ctx.signing_key else None,
        error=ctx.signing_key_error,
        active_release=active,
        releases=releases,
    )


@router.post("/releases", response_model=ReleaseOut)
def upload_release(
    file: UploadFile = File(...),
    # 注意：必须显式声明 Form(...)。带 File 的端点里，裸的 `version: str = ""`
    # 会被 FastAPI 当成**查询参数**而不是表单字段 —— 客户端在 multipart 里发的
    # version 会被静默忽略，然后参数取默认空值。这类 bug 不会报错，只会"没生效"。
    version: str = Form(""),
    channel: str = Form("stable"),
    notes: str = Form(""),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ReleaseOut:
    """上传 Agent 升级包。

    **上传不等于铺开**：``published_at`` 保持为空，Agent 不会收到任何东西，
    直到教师显式调 rollout。上传一个包和把它推给 50 台机器是风险等级完全
    不同的两件事，不该合成一个动作。
    """
    if ctx.signing_key is None:
        raise HTTPException(
            status_code=503,
            detail="未配置发布签名私钥，无法签发升级包：%s"
            % (ctx.signing_key_error or "请设置 SYNCOJ_RELEASE_KEY"),
        )

    version = (version or "").strip()
    if not version:
        raise HTTPException(status_code=400, detail="必须指定版本号")

    try:
        parse_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="版本号不合法: %s" % exc)

    filename = Path(file.filename or "agent-bundle.tar.gz").name

    try:
        sha256, size = ctx.blobs.put_stream(file.file, max_bytes=ctx.settings.max_release_size)
    except BlobTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    # 签名对象是 sha256 的十六进制字符串，而不是包本身的字节 ——
    # 见 agent/syncoj_agent/upgrade.py 的说明。两边必须选同一种，否则所有
    # 真实升级都会失败，且只在生产环境暴露。
    signature = _b64(ctx.signing_key.sign(sha256.encode("ascii")))

    with ctx.db.session() as session:
        existing = session.execute(
            select(AgentRelease).where(AgentRelease.version == version)
        ).scalar_one_or_none()
        if existing is not None:
            if existing.published_at is not None and existing.yanked_at is None:
                raise HTTPException(
                    status_code=409, detail="版本 %s 正在铺开中，先撤回再覆盖" % version
                )
            existing.sha256 = sha256
            existing.signature = signature
            existing.size = size
            existing.notes = notes or existing.notes
            existing.channel = channel or existing.channel
            existing.yanked_at = None
            session.flush()
            row = existing
        else:
            row = AgentRelease(
                version=version,
                channel=channel or "stable",
                sha256=sha256,
                signature=signature,
                size=size,
                notes=notes or None,
            )
            session.add(row)
            session.flush()

        session.add(
            EventLog(
                level="info",
                category="release_uploaded",
                message="上传 Agent 版本 %s（%s，%d 字节，%s）"
                % (version, filename, size, sha256[:12]),
            )
        )
        return _release_out(row)


@router.post("/releases/{release_id}/rollout", response_model=ReleaseOut)
def rollout_release(
    release_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ReleaseOut:
    """开始向 Agent 提供这个版本。

    同一时刻只允许一个版本处于铺开状态 —— 否则不同机器可能拿到不同版本，
    排查问题时无法判断"这台机器到底跑的是哪个"。
    """
    now = utcnow()
    with ctx.db.session() as session:
        target = session.get(AgentRelease, release_id)
        if target is None:
            raise HTTPException(status_code=404, detail="版本不存在")

        previous = []
        for row in session.execute(
            select(AgentRelease).where(
                AgentRelease.published_at.isnot(None),
                AgentRelease.yanked_at.is_(None),
                AgentRelease.id != release_id,
            )
        ).scalars():
            # 旧版本自动撤回，不需要教师手动点两次
            row.yanked_at = now
            previous.append(row.version)

        target.published_at = now
        target.yanked_at = None
        session.flush()

        session.add(
            EventLog(
                level="warning",
                category="release_rollout",
                message="开始铺开 Agent 版本 %s%s"
                % (target.version, ("（自动撤回 %s）" % ", ".join(previous)) if previous else ""),
            )
        )
        return _release_out(target)


@router.post("/releases/{release_id}/yank", response_model=ReleaseOut)
def yank_release(
    release_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ReleaseOut:
    """撤回：不再向 Agent 提供该版本。

    注意**撤回不能把已经升级的机器降回去** —— 那需要 Agent 侧的回滚机制
    （升级后连续启动失败会自动回滚）。撤回只是止血，防止影响面继续扩大。
    """
    with ctx.db.session() as session:
        row = session.get(AgentRelease, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        row.yanked_at = utcnow()
        session.flush()
        session.add(
            EventLog(
                level="warning",
                category="release_yank",
                message="撤回 Agent 版本 %s" % row.version,
            )
        )
        return _release_out(row)


@router.patch("/releases/{release_id}", response_model=ReleaseOut)
def update_release(
    release_id: int,
    payload: ReleaseUpdate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ReleaseOut:
    with ctx.db.session() as session:
        row = session.get(AgentRelease, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        if payload.notes is not None:
            row.notes = payload.notes
        if payload.channel is not None:
            row.channel = payload.channel[:16]
        session.flush()
        return _release_out(row)


def _release_out(row: AgentRelease) -> ReleaseOut:
    return ReleaseOut(
        id=row.id,
        version=row.version,
        channel=row.channel,
        sha256=row.sha256,
        size=int(row.size),
        notes=row.notes,
        rolled_out=row.published_at is not None and row.yanked_at is None,
        yanked=row.yanked_at is not None,
        created_at=_iso(row.created_at) or "",
        published_at=_iso(row.published_at),
    )


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
