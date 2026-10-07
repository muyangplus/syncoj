"""管理后台 API。

单管理员模型：登录换取不透明会话 token（服务端只存哈希，可按会话吊销）。
M1 阶段只覆盖"建场次 → 导选手 → 发注册码 → 看在线上报"这条闭环。
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
# Response / StreamingResponse 必须**在模块顶层可见**：本文件开了
# ``from __future__ import annotations``，返回标注只是个字符串，
# FastAPI 到了生成 OpenAPI 时才去解析它。名字没导入的话，
# 它会试图把 ``ForwardRef('Response')`` 当成响应模型来建模，然后炸在
# /openapi.json 上 —— 而 /docs 和前端类型生成都依赖那个端点。
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import keys
from ..config import Settings
from ..context import AppContext
from ..errors import ERROR_CODES, ApiError
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
    EventLog,
    JudgeRun,
    Player,
    Problem,
    Roster,
    RosterEntry,
    SourceFile,
    iso_utc,
    utcnow,
)
from ..paths import PathValidationError, safe_join, slugify, validate_relpath
from ..schemas import (
    GLOBAL_CONFIRM,
    AdminInfo,
    AgentRuntimeOut,
    ApplyRosterIn,
    ApplyRosterOut,
    AssetOut,
    AssetRenameIn,
    AssetTextEditIn,
    AssetTextIn,
    AssetTextOut,
    AssetTextSavedOut,
    AssetZipPasswordIn,
    AssetZipPasswordOut,
    AssetZipPasswordSavedOut,
    BootstrapKeyIssueIn,
    BootstrapKeyIssuedOut,
    BootstrapKeyOut,
    BindByCodeIn,
    BindMachineIn,
    BindResultOut,
    CloneAlertOut,
    ConfirmIn,
    ContestCreate,
    ContestOut,
    ContestUpdate,
    DeployCreate,
    DeployTargetOut,
    DeployTaskOut,
    EventOut,
    FileClearIn,
    JudgeRunClearIn,
    JudgeRunOut,
    JudgeScanOut,
    LoginRequest,
    LoginResponse,
    ManualScoreIn,
    Page,
    PendingMachineOut,
    PlayerClearIn,
    PlayerImportOut,
    PlayerOut,
    PlayerUpsert,
    ProblemColumnOut,
    ProblemImportOut,
    ProblemMatchIn,
    ProblemMatchOut,
    ProblemOut,
    ProblemUpsert,
    ReleaseBuildIn,
    ReleaseOut,
    ReleaseSourceOut,
    ReleaseUpdate,
    RuntimeSettingsOut,
    RuntimeSettingsUpdate,
    RebindAgentIn,
    RosterCreate,
    RosterDetailOut,
    RosterEntryIn,
    RosterEntryOut,
    RosterImportOut,
    RosterOut,
    ScoreCellOut,
    ScoreMatrixOut,
    ScoreRowOut,
    SetAgentContestIn,
    SimpleAck,
    SourceFileOut,
    UpgradeStatusOut,
    page_of,
    slice_page,
)
from ..security import (
    hash_bootstrap_key,
    hash_password,
    hash_token,
    new_bootstrap_key,
    new_token,
    verify_password,
)
from ..services import (
    discovery,
    enrollment,
    install_policy,
    matching,
    packaging,
    rosters,
    runtime_settings,
    scan_missing,
    uninstall,
    zipcrypto,
)
from ..services.deploy import validate_dest_template
from ..services.signing import parse_version
from ..storage import BlobTooLarge, HashMismatch
from .deps import PageParams, get_ctx

__all__ = ["router", "require_admin", "AdminIdentity"]

#: 「还没落地」的下发状态。改名/删除资产时要看有没有这些 ——
#: 一旦目标已经完成，改名就只是改标签；还没完成的话，新名字会决定它落在哪
ACTIVE_DEPLOY_STATUSES = (DeployStatus.PENDING, DeployStatus.READY)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _iso(value) -> Optional[str]:
    """库里的 naive UTC → API 的 ISO-8601。

    实现只有一处：``models.iso_utc``（时间格式与存储约定都写在那个模块的
    docstring 里）。这里留一层薄封装只是因为本文件有几十处调用点。
    """
    return iso_utc(value)


def _parse_iso(value: Optional[str], label: str) -> Optional[datetime]:
    """请求里的 ISO-8601 字符串 → 库里的 naive UTC。

    三种写法都要认：

    * 带 ``Z``（``2026-06-01T01:00:00Z``）—— 我们自己回显用的就是这个形状，
      "把服务端给的值再喂回来"必须成立
    * 带偏移（``2026-06-01T09:00:00+08:00``）—— 浏览器 ``toISOString`` 之外的
      工具会这么发；按它自己声明的时区折算，绝不当地时间硬存
    * 不带时区（``2026-06-01T09:00:00``）—— **当成 UTC**。这是本系统对外的
      时间约定（见 ``docs/reference/api-conventions.md`` 的命名表），不是猜。

    空串与 ``None`` 都是"这一端不限制"。
    """
    raw = (value or "").strip()
    if not raw:
        return None
    text_value = raw[:-1] + "+00:00" if raw[-1] in ("Z", "z") else raw
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="%s 不是合法的时间（例：2026-06-01T09:00:00），请检查格式" % label,
        )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _require_valid_window(starts_at: Optional[datetime], ends_at: Optional[datetime]) -> None:
    """开考时间必须早于结束时间。

    反过来的窗口**永远不会成立**：整场考试从头到尾都"还没开考"，而到点判定又
    说"已结束"。这种配置一旦存进去，现场只能用"把两个时间都删掉"来救，
    所以宁可在写入时挡下来，并说清楚该怎么改。
    """
    if starts_at is not None and ends_at is not None and starts_at > ends_at:
        raise ApiError(
            400,
            "bad_request",
            "开考时间不能晚于结束时间，否则这场考试永远不会开始。请把结束时间改到开考之后",
            {"starts_at": iso_utc(starts_at), "ends_at": iso_utc(ends_at)},
        )


def require_confirm(actual: Optional[str], given: Optional[str], label: str) -> None:
    """结构性数据（名单/场次/选手/机器/题目）删除前的**服务端**确认。

    为什么校验放在这里而不只是前端弹窗：前端弹窗是给人看的提示，服务端校验
    才是"没经过界面的调用也一样安全"的保证。删除是不可逆的（成绩矩阵连同代码
    一起消失），而"是否确定？"这种弹窗在连续操作里会被手指肌肉记忆点掉 ——
    所以确认内容不是"是否确定"，而是**把名字打一遍**。

    逐字比较（只容忍首尾空格）：差一个字就是差一个字，不做大小写/别名宽容，
    因为宽容的规则在紧张的操作现场只会让人搞不清到底输入什么才算对。
    """
    expected = (actual or "").strip()
    supplied = (given or "").strip()
    if expected and supplied == expected:
        return
    raise ApiError(
        400,
        "name_mismatch",
        "确认不通过：要删的是%s「%s」，请把它原样输一遍再提交。" % (label, actual or ""),
        {"expected": actual, "given": given, "label": label},
    )


@dataclass
class AdminIdentity:
    id: int
    username: str


def _unauthorized(detail: str, code: str = "unauthorized") -> ApiError:
    return ApiError(
        status.HTTP_401_UNAUTHORIZED,
        code,
        detail,
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
            # 这一条单独给 ``token_expired`` 而不是笼统的 ``unauthorized``：
            # "过期了" 和 "这个凭据我根本不认识" 对人的含义不同 —— 前者是
            # 该重新登录，后者是有人的 token 被换了/拼错了。
            raise _unauthorized("登录已过期，请重新登录", code="token_expired")
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
            raise _unauthorized("用户名或口令错误", code="bad_credentials")

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

    # 时间窗在建场时就可以配（大多数场次是"先建好、顺手把考试时间定下来"）。
    # 两个都留空 = 不限制，这是默认；只配一个也合法。
    starts_at = _parse_iso(payload.starts_at, "开考时间")
    ends_at = _parse_iso(payload.ends_at, "结束时间")
    _require_valid_window(starts_at, ends_at)

    with ctx.db.session() as session:
        if session.execute(select(Contest).where(Contest.slug == slug)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="场次标识已存在: %s" % slug)
        roster = _resolve_roster(session, payload.default_roster_id)
        contest = Contest(
            slug=slug,
            name=payload.name,
            status=status_value,
            note=payload.note,
            starts_at=starts_at,
            ends_at=ends_at,
            default_roster_id=roster.id if roster else None,
        )
        session.add(contest)
        session.flush()
        return _contest_out(contest, player_count=0, online_count=0, roster=roster)


@router.get("/contests", response_model=Page[ContestOut])
def list_contests(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[ContestOut]:
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
        return slice_page(result, page)


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

    时间窗（``starts_at`` / ``ends_at``）走另一条路：直接看
    ``model_fields_set``。"传了 null" = 清掉这一端的时间限制，"压根没传" =
    这次别动它。两者必须分得开 —— 用 ``is not None`` 判断的话，教师永远删不掉
    一个填错的时间点，而界面上看起来是保存成功了（那种"改完又变回去"的故障
    最难查，因为它不报错）。
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

        # 时间窗要拿**改完之后**的两个值一起校验：只 POST 了新结束时间的请求，
        # 同样可能把窗口配反（老的开考时间 > 新的结束时间）。
        provided = payload.model_fields_set
        starts_at = _parse_iso(payload.starts_at, "开考时间") if "starts_at" in provided else contest.starts_at
        ends_at = _parse_iso(payload.ends_at, "结束时间") if "ends_at" in provided else contest.ends_at
        _require_valid_window(starts_at, ends_at)
        contest.starts_at = starts_at
        contest.ends_at = ends_at

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
        note=contest.note,
        # 时间窗要能回显：教师填完再打开对话框，看到的是刚才那两个值。
        # 不回显的话他会重新填一遍，而且永远不知道自己有没有填上。
        starts_at=_iso(contest.starts_at),
        ends_at=_iso(contest.ends_at),
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


@router.get("/rosters", response_model=Page[RosterOut])
def list_rosters(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[RosterOut]:
    with ctx.db.session() as session:
        counts = dict(
            session.execute(
                select(RosterEntry.roster_id, func.count(RosterEntry.id)).group_by(
                    RosterEntry.roster_id
                )
            ).all()
        )
        rows = list(session.execute(select(Roster).order_by(Roster.name)).scalars())
        return slice_page(
            [_roster_out(row, counts.get(row.id, 0)) for row in rows], page
        )


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


def _bound_player_nos(session, roster_id: int) -> List[str]:
    """这份名单里**被机器绑着**的人的考号（去重、按考号排序）。

    机器绑的是名单条目（``agent.roster_entry_id``），而条目被删时那个外键是
    ``ON DELETE SET NULL`` —— 于是那台机器回到"未配对"，下一次心跳显示新的配对码，
    要教师**重新配一遍**。这件事必须先说出来：它是不可逆的**批量**解绑，而且完全静默
    （端点的说明原来只写"不动任何场次的选手" —— 对场次确实没动，机器被解绑了）。
    """
    rows = session.execute(
        select(RosterEntry.player_no)
        .join(Agent, Agent.roster_entry_id == RosterEntry.id)
        .where(RosterEntry.roster_id == roster_id)
        .order_by(RosterEntry.player_no)
    ).scalars()
    return list(dict.fromkeys(rows))


def _roster_in_use_error(bound: List[str], what: str) -> ApiError:
    """被机器绑着时**拒绝**，并给出台数、前几个人、以及出路。

    选拒绝而不是"允许但警告"，与结构性删除要打名字是同一条理由（§5.4）：这是不可逆的
    批量解绑，而一句"确定吗"在连续操作里会被手指记忆点掉。
    """
    shown = "、".join(bound[:3]) + ("…" if len(bound) > 3 else "")
    return ApiError(
        409,
        "roster_in_use",
        "%s里有 %d 台机器绑在上面的人（%s）。这么做会把这 %d 台机器全部解除配对 ——"
        "它们下次心跳会显示新的配对码，要一台台重新配。请先到「机器配对」里把这几台"
        "改派或解绑，再回来。"
        % (what, len(bound), shown, len(bound)),
        {"count": len(bound), "player_nos": bound[:20]},
    )


@router.delete("/rosters/{roster_id}", response_model=SimpleAck)
def delete_roster(
    roster_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入名单名称"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删除名单。

    **只删名单本身，不动任何场次的选手。** 名单是模板，场次的参赛者是从它
    复制出去的一份独立数据 —— 删模板不该牵动已发生的比赛。引用了这份名单的
    场次会被置空（``ON DELETE SET NULL``），只是"没预设名单了"而已。

    **但机器不适用这句话**：机器绑的是名单里的**人**（条目），条目会随名单一起被删，
    于是那批机器被解除配对、要重新配一遍。所以有机器绑着时**拒绝删除**，并说清代价
    （见 :func:`_roster_in_use_error`）。这条直接关系到"配对一次、之后 N 场比赛零人工"
    这个性质 —— 不能让人在以为"只是个模板"的时候把它悄悄拆掉。
    """
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            return SimpleAck(ok=True, detail="名单不存在")
        require_confirm(roster.name, confirm, "名单名称")
        bound = _bound_player_nos(session, roster_id)
        if bound:
            raise _roster_in_use_error(bound, "这份名单")
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


@router.patch("/roster-entries/{entry_id}", response_model=RosterEntryOut)
def update_roster_entry(
    entry_id: int,
    payload: RosterEntryIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterEntryOut:
    """改名单里的一条。改错了编号要能修，不必删了重加。"""
    with ctx.db.session() as session:
        entry = session.get(RosterEntry, entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="名单条目不存在")

        player_no = payload.player_no.strip()
        if not player_no:
            raise HTTPException(status_code=400, detail="选手编号不能为空")
        if len(player_no) > 64:
            raise HTTPException(status_code=400, detail="选手编号超过 64 字符")
        too_long = _first_overlong(
            (payload.name, 64, "姓名"), (payload.seat, 32, "座位"), (payload.group_name, 64, "分组")
        )
        if too_long:
            raise HTTPException(status_code=400, detail=too_long)

        if player_no != entry.player_no:
            clash = session.execute(
                select(RosterEntry).where(
                    RosterEntry.roster_id == entry.roster_id,
                    RosterEntry.player_no == player_no,
                    RosterEntry.id != entry_id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=409, detail="名单里已经有 %s 了" % player_no)
            entry.player_no = player_no

        entry.name = payload.name
        entry.seat = payload.seat
        entry.group_name = payload.group_name
        session.flush()
        return _roster_entry_out(entry)


@router.post("/rosters/{roster_id}/entries/clear", response_model=SimpleAck)
def clear_roster_entries(
    roster_id: int,
    payload: ConfirmIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """清空名单里的全部条目（保留名单本身）。

    与删整份名单同一个理由要拦：机器绑的是条目，清空等于把这份名单上**所有**机器的
    配对照样解除掉。所以有机器绑着时拒绝，并说清是哪几个人。
    """
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise HTTPException(status_code=404, detail="名单不存在")
        require_confirm(roster.name, payload.confirm, "名单名称")
        bound = _bound_player_nos(session, roster_id)
        if bound:
            raise _roster_in_use_error(bound, "这份名单")
        removed = 0
        for entry in session.execute(
            select(RosterEntry).where(RosterEntry.roster_id == roster_id)
        ).scalars():
            session.delete(entry)
            removed += 1
        name = roster.name
    return SimpleAck(ok=True, detail="已清空名单「%s」的 %d 条记录" % (name, removed))


@router.delete("/roster-entries/{entry_id}", response_model=SimpleAck)
def delete_roster_entry(
    entry_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入考号"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """从名单里删掉一个人。

    **允许**（这本来就是"这个人走了"），但要把副作用如实说出来：绑在这条条目上的
    机器会被解除配对（外键 ``ON DELETE SET NULL``），它下次心跳会显示新的配对码。
    不说的话，教师只会看到"已移除 S001"，然后对着那台机器的配对码发愣 ——
    这条路径不像"删整份名单"那样带批量风险，所以拦它反而碍事。
    """
    with ctx.db.session() as session:
        entry = session.get(RosterEntry, entry_id)
        if entry is None:
            return SimpleAck(ok=True, detail="条目不存在")
        require_confirm(entry.player_no, confirm, "考号")
        player_no = entry.player_no
        bound = session.execute(
            select(func.count(Agent.id)).where(Agent.roster_entry_id == entry_id)
        ).scalar_one()
        session.delete(entry)
    detail = "已从名单中移除 %s" % player_no
    if bound:
        detail += "（同时解除了 %d 台机器的配对，它们下次心跳会显示新的配对码）" % bound
    return SimpleAck(ok=True, detail=detail)


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
# 机器配对
# --------------------------------------------------------------------------- #
#
# 机器绑的是**名单里的某个人**，不是某场比赛的选手 —— 所以配对是永久的，
# 同一个学生换一场比赛不用重新配。教师在这里做的事只有一件：
# 把"机器上显示的那串 6 位数字"对应到名单里的一个人。


@router.get("/machines/pending", response_model=Page[PendingMachineOut])
def list_pending_machines(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[PendingMachineOut]:
    """待配对的机器。

    按**最后心跳时间倒序**：教师站在机器前读配对码时，那台机器刚刚才心跳过，
    它就应该在最上面。按注册时间排会让人从一堆久未上线的机器里翻找。
    """
    now = utcnow()
    with ctx.db.session() as session:
        # 这个集合会无限增长（每次重装 Agent 都是一条新行），所以真分页
        total = session.execute(
            select(func.count(Agent.id)).where(
                Agent.roster_entry_id.is_(None), Agent.revoked_at.is_(None)
            )
        ).scalar_one()
        agents = list(
            session.execute(
                select(Agent)
                .where(Agent.roster_entry_id.is_(None), Agent.revoked_at.is_(None))
                .order_by(Agent.last_seen_at.desc().nullslast(), Agent.id)
                .limit(page.limit)
                .offset(page.offset)
            ).scalars()
        )
        peers = _fingerprint_peer_counts(session)

    result = []
    for agent in agents:
        expires_in = None
        if agent.pair_code_expires_at is not None:
            expires_in = int((agent.pair_code_expires_at - now).total_seconds())
        result.append(
            PendingMachineOut(
                id=agent.id,
                machine_id=agent.machine_id,
                hostname=agent.hostname,
                os_info=agent.os_info,
                agent_version=agent.agent_version,
                machine_uuid=agent.machine_uuid,
                machine_fingerprint=agent.machine_fingerprint,
                created_at=_iso(agent.enrolled_at) or "",
                last_seen_at=_iso(agent.last_seen_at),
                seconds_since_seen=(
                    (now - agent.last_seen_at).total_seconds()
                    if agent.last_seen_at is not None
                    else None
                ),
                pair_code_expires_in=expires_in,
                fingerprint_peers=peers.get(agent.machine_fingerprint or "", 0),
            )
        )
    return page_of(result, total, page)


def _fingerprint_peer_counts(session) -> Dict[str, int]:
    """每个指纹上总共挂了几台机器。

    含已配对的是有意的：告警要回答的是"这份镜像是不是克隆出来的"，
    而克隆出来的机器里，先配对好的那几台恰恰是证据。
    """
    counts = dict(
        session.execute(
            select(Agent.machine_fingerprint, func.count(Agent.id))
            .where(Agent.machine_fingerprint.isnot(None), Agent.revoked_at.is_(None))
            .group_by(Agent.machine_fingerprint)
        ).all()
    )
    return {str(key): int(value) for key, value in counts.items()}


@router.get("/machines/clone-alerts", response_model=Page[CloneAlertOut])
def list_clone_alerts(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[CloneAlertOut]:
    """疑似克隆镜像：多台机器共用同一个硬件指纹。

    正常情况下每台物理机的 SMBIOS UUID 都不同。撞了指纹说明镜像是在某台机器
    **跑过之后**才克隆的 —— 那批机器里可能已经有人的凭据被一起拷了进去，
    配对与成绩归属都有串的风险，值得教师停下来看一眼。

    **但要分清"还没配对"和"已经配给了人"。** 一份镜像装遍整间机房时撞指纹是必然
    也是正常的：一台都还没配对时，没有任何人的身份可被冒领，界面就不该指控"混进了
    别人的凭据"。所以这里如实给出 ``bound_count``，由界面决定该报警还是该说明。
    """
    with ctx.db.session() as session:
        agents = list(
            session.execute(
                select(Agent)
                .where(Agent.machine_fingerprint.isnot(None), Agent.revoked_at.is_(None))
                .order_by(Agent.machine_fingerprint)
            ).scalars()
        )

    grouped: Dict[str, List[str]] = {}
    bound: Dict[str, int] = {}
    for agent in agents:
        fingerprint = agent.machine_fingerprint or ""
        label = agent.hostname or agent.machine_id or "?"
        if agent.roster_entry_id is None:
            label += "（待配对）"
        else:
            bound[fingerprint] = bound.get(fingerprint, 0) + 1
        grouped.setdefault(fingerprint, []).append(label)

    alerts = [
        CloneAlertOut(
            fingerprint=fingerprint,
            machine_count=len(names),
            bound_count=bound.get(fingerprint, 0),
            hostnames=sorted(names),
        )
        for fingerprint, names in sorted(grouped.items())
        if len(names) > 1
    ]
    return slice_page(alerts, page)


@router.post("/machines/bind-by-code", response_model=BindResultOut)
def bind_machine_by_code(
    payload: BindByCodeIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BindResultOut:
    """按配对码配对：机器上显示什么，教师就输什么。

    这是主路径。配对码六位数字、限时、用一次即作废 —— 它**只在绑定时用**，
    配对之后认机器靠 machine_uuid。
    """
    now = utcnow()
    with ctx.db.session() as session:
        entry = session.get(RosterEntry, payload.roster_entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="名单条目不存在")

        agent = enrollment.find_unbound_by_pair_code(session, payload.pair_code, now)
        if agent is None:
            # 不区分"没这个码"和"码过期了"：对外说法一致，
            # 免得有人拿它当预言机去猜码
            raise ApiError(
                404,
                "pair_code_invalid",
                "配对码不存在或已过期。让机器重新注册一次即可刷新（重启 Agent 服务）。",
            )
        return _do_bind(session, agent, entry, now, ctx)


@router.post("/machines/{agent_id}/bind", response_model=BindResultOut)
def bind_machine_by_id(
    agent_id: int,
    payload: BindMachineIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BindResultOut:
    """按机器配对（教师从列表里按主机名点选）。

    给了 ``pair_code`` 就必须对得上 —— 机器名可能是重复的（克隆镜像、
    默认 hostname），而配对码是唯一能证明"教师确实站在这台机器前面"的东西。
    """
    now = utcnow()
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None or agent.revoked_at is not None:
            raise HTTPException(status_code=404, detail="这台机器不在待配对列表里")

        if payload.pair_code and not enrollment.pair_code_is_valid(
            agent, payload.pair_code, now
        ):
            raise ApiError(
                400,
                "pair_code_invalid",
                "配对码不对（或已过期）。请对着机器上的配对码重新输入。",
            )

        entry = session.get(RosterEntry, payload.roster_entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="名单条目不存在")
        return _do_bind(session, agent, entry, now, ctx)


def _do_bind(
    session, agent: Agent, entry: RosterEntry, now, ctx: AppContext = None
) -> BindResultOut:
    try:
        outcome = enrollment.bind_machine(session, agent, entry, now)
    except enrollment.BootstrapRejected as exc:
        # 把服务层的 StatusCode + Code 原样透出去：前端要拿 code 区分
        # "这台机器已经属于别人"和"这个人已经有机器了"（两者的补救动作不同）
        raise ApiError(exc.status_code, exc.code, exc.detail)

    if ctx is not None:
        # 配对码是一次性的：绑上了就把它从内存里忘掉，
        # 否则哪天解绑之后服务端还会回一个早就作废的数字
        ctx.pair_codes.forget(agent.id)

    name = outcome.entry.roster.name if outcome.entry.roster else ""
    return BindResultOut(
        agent_id=outcome.agent.id,
        roster_entry_id=outcome.entry.id,
        player_no=outcome.entry.player_no,
        roster_name=name,
        detail="已把机器配对给 %s%s。它下一轮心跳就会去找自己该在的场次"
        % (outcome.entry.player_no, "（%s）" % outcome.entry.name if outcome.entry.name else ""),
    )


@router.delete("/machines/pending/{agent_id}", response_model=SimpleAck)
def revoke_pending_machine(
    agent_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入主机名"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """把一台待配对的机器从列表里去掉（认错机器、测试机、刷注册的垃圾）。

    作废它的凭据 —— 那台机器下次心跳会拿到 401，然后等下一次开机由注册单元
    重新注册。真正的垃圾机器应该先吊销统一密钥，否则它会一直回来。
    """
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None or agent.roster_entry_id is not None:
            return SimpleAck(ok=True, detail="这台机器不在待配对列表里")
        # 主机名可能是空的（Agent 读不到 hostname 时），退回 machine_id 作为标识 ——
        # 空字符串会让"原样输入"变成一个谁都不用输入的空操作
        require_confirm(agent.hostname or agent.machine_id, confirm, "机器名")
        agent.revoked_at = utcnow()
    ctx.registry.forget(agent_id)
    ctx.pair_codes.forget(agent_id)
    return SimpleAck(ok=True, detail="已移除")


@router.post("/machines/pending/clear", response_model=SimpleAck)
def clear_pending_machines(
    payload: ConfirmIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """清空待配对列表。

    典型场景：机房做镜像时忘了通用化，50 台克隆机全冒出来了 ——
    教师修好镜像之后要把这一堆清掉重来。

    这是**范围**删除（没有单个名字可打），所以确认内容是全局面量 ``all``。

    **先吊销统一密钥再清**，否则那批机器下次心跳拿到 401、等下次开机又回来。
    """
    require_confirm(GLOBAL_CONFIRM, payload.confirm, "清空确认")
    with ctx.db.session() as session:
        rows = list(
            session.execute(
                select(Agent).where(
                    Agent.roster_entry_id.is_(None), Agent.revoked_at.is_(None)
                )
            ).scalars()
        )
        now = utcnow()
        for agent in rows:
            agent.revoked_at = now
        if rows:
            session.add(
                EventLog(
                    level="warning",
                    category="machines_clear",
                    message="清空待配对机器 %d 台" % len(rows),
                )
            )
        ids = [agent.id for agent in rows]

    for agent_id in ids:
        ctx.registry.forget(agent_id)
        ctx.pair_codes.forget(agent_id)

    detail = "已移除 %d 台待配对机器" % len(rows)
    if rows:
        detail += "。它们下次开机还会回来 —— 要挡住请先吊销统一密钥"
    return SimpleAck(ok=True, detail=detail)


# --------------------------------------------------------------------------- #
# 运行参数（心跳节奏与离线判定）
# --------------------------------------------------------------------------- #


@router.get("/settings/runtime", response_model=RuntimeSettingsOut)
def get_runtime_settings(
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RuntimeSettingsOut:
    """当前生效的运行参数。

    这三项决定"机器多久心跳一次"与"多久没心跳算离线"，都是**整间机房**的节奏，
    所以界面要能一眼看到当前值 —— 否则教师只能靠列表上的刷新间隔去猜。
    """
    with ctx.db.session() as session:
        return RuntimeSettingsOut(**runtime_settings.load(session).as_dict())


@router.put("/settings/runtime", response_model=RuntimeSettingsOut)
def update_runtime_settings(
    payload: RuntimeSettingsUpdate,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RuntimeSettingsOut:
    """改运行参数。**改完立即生效，不用重启。**

    心跳处理与离线判定每次都现读这三项（``services/runtime_settings.py`` 的
    ``load``），所以下一轮心跳就按新节奏走；下发给 Agent 的策略本来每轮都带，
    机器那边也跟着换。

    改一行配置会改变整间机房的节奏，所以**记一条审计**（谁、把哪一项从多少改成
    多少）—— 事后看到"心跳突然变密了"时，这是唯一能回答"谁动的"的东西。
    """
    changes = payload.model_dump(exclude_none=True)
    with ctx.db.session() as session:
        before = runtime_settings.load(session)
        try:
            after = runtime_settings.save(session, changes)
        except runtime_settings.RuntimeSettingError as exc:
            # 400 而不是 422：请求体本身合法（类型、范围都对），不合格的是
            # **改完之后这一组值**之间的关系 —— 那要看库里当前的另外两项
            raise ApiError(400, "bad_request", exc.detail)

        changed = [
            "%s：%d → %d" % (name, before.as_dict()[name], value)
            for name, value in after.as_dict().items()
            if before.as_dict()[name] != value
        ]
        if changed:
            session.add(
                EventLog(
                    level="warning",
                    category="runtime_settings",
                    message="修改运行参数（%s）：%s" % (admin.username, "；".join(changed)),
                    meta_json=json.dumps(
                        {
                            "by": admin.username,
                            "before": before.as_dict(),
                            "after": after.as_dict(),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return RuntimeSettingsOut(**after.as_dict())


# --------------------------------------------------------------------------- #
# 统一注册密钥
# --------------------------------------------------------------------------- #
# 统一注册密钥
# --------------------------------------------------------------------------- #


def _bootstrap_key_out(key: BootstrapKey) -> BootstrapKeyOut:
    return BootstrapKeyOut(
        id=key.id,
        label=key.label,
        note=key.note,
        created_at=_iso(key.created_at) or "",
        expires_at=_iso(key.expires_at),
        revoked_at=_iso(key.revoked_at),
        last_used_at=_iso(key.last_used_at),
        use_count=int(key.use_count or 0),
    )


@router.get("/bootstrap-keys", response_model=Page[BootstrapKeyOut])
def list_bootstrap_keys(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[BootstrapKeyOut]:
    with ctx.db.session() as session:
        rows = list(
            session.execute(select(BootstrapKey).order_by(BootstrapKey.id)).scalars()
        )
        return slice_page([_bootstrap_key_out(row) for row in rows], page)


@router.post("/bootstrap-keys", response_model=BootstrapKeyIssuedOut)
def issue_bootstrap_key(
    payload: BootstrapKeyIssueIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BootstrapKeyIssuedOut:
    """签发一把统一密钥。**明文只返回这一次**，库里只有哈希。"""
    raw = new_bootstrap_key(ctx.settings.bootstrap_key_bytes)
    expires_at = (
        utcnow() + timedelta(days=payload.expires_days) if payload.expires_days else None
    )
    with ctx.db.session() as session:
        key = BootstrapKey(
            key_hash=hash_bootstrap_key(raw),
            label=payload.label or "",
            note=payload.note,
            expires_at=expires_at,
        )
        session.add(key)
        session.flush()
        session.add(
            EventLog(
                level="info",
                category="bootstrap_key",
                message="签发统一注册密钥 #%d（%s）" % (key.id, key.label or "无用途备注"),
            )
        )
        return BootstrapKeyIssuedOut(**_bootstrap_key_out(key).model_dump(), key=raw)


@router.post("/bootstrap-keys/{key_id}/revoke", response_model=BootstrapKeyOut)
def revoke_bootstrap_key(
    key_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> BootstrapKeyOut:
    """吊销一把统一密钥。

    **已经注册好的机器不受影响** —— 它们手里是各自的 token，不是这把密钥。
    吊销只挡住"以后还想拿它注册"的机器。
    """
    with ctx.db.session() as session:
        key = session.get(BootstrapKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="密钥不存在")
        if key.revoked_at is None:
            key.revoked_at = utcnow()
            session.add(
                EventLog(
                    level="warning",
                    category="bootstrap_key",
                    message="吊销统一注册密钥 #%d（%s）" % (key.id, key.label or "无用途备注"),
                )
            )
        session.flush()
        return _bootstrap_key_out(key)


@router.delete("/bootstrap-keys/{key_id}", response_model=SimpleAck)
def delete_bootstrap_key(
    key_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """彻底删掉一把统一密钥（不是吊销）。

    「吊销」和「删除」的区别值得说清楚，否则教师只会看到两个长得很像的按钮：

    * **吊销**：留痕。``use_count`` / ``last_used_at`` 还在，能回答"这把钥匙
      到底被用过多少次"。密钥泄漏时的第一反应应该是吊销。
    * **删除**：抹掉记录。只在"签错了、一次都没用过"时才合适。

    没被用过、也没被吊销的密钥才允许删除 —— 一把用过的钥匙不能因为记录被删
    就当它没存在过。
    """
    with ctx.db.session() as session:
        key = session.get(BootstrapKey, key_id)
        if key is None:
            return SimpleAck(ok=True, detail="密钥不存在")
        if int(key.use_count or 0) > 0:
            raise HTTPException(
                status_code=409,
                detail="这把密钥已经被用过 %d 次，不能删除 —— 请改用「吊销」以保留记录"
                % int(key.use_count or 0),
            )
        label = key.label or "#%d" % key.id
        session.delete(key)
        session.add(
            EventLog(
                level="info",
                category="bootstrap_key",
                message="删除未使用的统一注册密钥 %s" % label,
            )
        )
    return SimpleAck(ok=True, detail="已删除统一密钥 %s" % label)


# --------------------------------------------------------------------------- #
# 选手
# --------------------------------------------------------------------------- #


@router.post("/contests/{contest_id}/players", response_model=PlayerImportOut)
def import_players(
    contest_id: int,
    payload: List[PlayerUpsert],
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> PlayerImportOut:
    """批量导入/更新选手。按 ``player_no`` 幂等 upsert。

    **这不是集合读取，所以不用列表信封**（``docs/reference/api-conventions.md`` §2）：
    它返回的是"这次导入干了什么"，前端要显示的是 ``created``/``updated``。
    顺带回传受影响的行，是因为导入之后常常紧接着"应用名单""批量配对"，
    调用方需要那些 id，不该再查一次。
    """
    if not payload:
        return PlayerImportOut()
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
        created = 0
        updated = 0
        for item in payload:
            row = existing.get(item.player_no)
            if row is None:
                row = Player(contest_id=contest_id, player_no=item.player_no)
                session.add(row)
                existing[item.player_no] = row
                created += 1
            else:
                updated += 1
            row.name = item.name
            row.seat = item.seat
            row.group_name = item.group_name
            touched.append(row)

        # 必须先 flush 才能拿到自增主键 —— 新插入的行在 flush 前 id 为 None
        session.flush()
        return PlayerImportOut(
            created=created,
            updated=updated,
            players=[
                _player_out(row, has_agent=False, online=False, file_count=0, last_tick=None)
                for row in touched
            ],
        )


@router.get("/contests/{contest_id}/players", response_model=Page[PlayerOut])
def list_players(
    contest_id: int,
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[PlayerOut]:
    with ctx.db.session() as session:
        total = session.execute(
            select(func.count(Player.id)).where(Player.contest_id == contest_id)
        ).scalar_one()
        players = list(
            session.execute(
                select(Player)
                .where(Player.contest_id == contest_id)
                .order_by(Player.player_no)
                .limit(page.limit)
                .offset(page.offset)
            ).scalars()
        )
        # 机器数是**跨场次**的：机器绑的是人（名单条目），不是这场比赛的选手记录。
        # 所以按准考证号去数，而不是按 player_id —— 同一个人在别的场次绑的机器
        # 也该算"他有机器"，否则每一场的列表都会显示成"未注册"。
        machine_counts = {
            player_no: count
            for player_no, count in session.execute(
                select(RosterEntry.player_no, func.count(Agent.id))
                .join(Agent, Agent.roster_entry_id == RosterEntry.id)
                .where(Agent.revoked_at.is_(None))
                .group_by(RosterEntry.player_no)
            ).all()
        }
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
                has_agent=bool(machine_counts.get(player.player_no, 0)),
                online=bool(runtime and runtime.online),
                file_count=file_counts.get(player.id, 0),
                last_tick=runtime.last_tick_at if runtime else None,
            )
        )
    return page_of(result, total, page)


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


@router.patch("/players/{player_id}", response_model=PlayerOut)
def update_player(
    player_id: int,
    payload: PlayerUpsert,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> PlayerOut:
    """改一名选手。

    ``player_no`` 也能改，但要连带想清楚：它出现在已回收代码的落盘路径
    （``source/<场次>/<准考证号>/…``）与下发目标模板里。改完之后**已有文件的
    目录名不会跟着变** —— 这是有意的：把磁盘上的目录改名会让评测器配置
    与历史成绩一起失效。真要改名，请连同 `source/` 目录和评测器配置一起改。
    """
    with ctx.db.session() as session:
        player = session.get(Player, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="选手不存在")

        player_no = payload.player_no.strip()
        if not player_no:
            raise HTTPException(status_code=400, detail="选手编号不能为空")
        if len(player_no) > 64:
            raise HTTPException(status_code=400, detail="选手编号超过 64 字符")
        too_long = _first_overlong(
            (payload.name, 64, "姓名"), (payload.seat, 32, "座位"), (payload.group_name, 64, "分组")
        )
        if too_long:
            raise HTTPException(status_code=400, detail=too_long)

        if player_no != player.player_no:
            clash = session.execute(
                select(Player).where(
                    Player.contest_id == player.contest_id,
                    Player.player_no == player_no,
                    Player.id != player_id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=409, detail="选手编号 %s 已被占用" % player_no)
            player.player_no = player_no

        player.name = payload.name
        player.seat = payload.seat
        player.group_name = payload.group_name
        session.flush()

        machine_count = session.execute(
            select(func.count(Agent.id))
            .join(RosterEntry, Agent.roster_entry_id == RosterEntry.id)
            .where(RosterEntry.player_no == player.player_no, Agent.revoked_at.is_(None))
        ).scalar_one()
        file_count = session.execute(
            select(func.count(SourceFile.id)).where(
                SourceFile.player_id == player_id, SourceFile.deleted_at.is_(None)
            )
        ).scalar_one()
        runtime = next(
            (a for a in ctx.registry.all(player.contest_id) if a.player_id == player_id), None
        )
        return _player_out(
            player,
            has_agent=bool(machine_count),
            online=bool(runtime and runtime.online),
            file_count=int(file_count),
            last_tick=runtime.last_tick_at if runtime else None,
        )


@router.delete("/players/{player_id}", response_model=SimpleAck)
def delete_player(
    player_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入考号"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删一名选手，连同他的代码台账与成绩（外键级联）。

    **不动他的机器。** 机器绑的是名单里的那个人，不是这场比赛的这条选手记录 ——
    删掉他在本场的参赛记录，不等于要作废他那台机器：同一个学生明天还有比赛，
    机器明天照样要用。作废机器请用「作废机器」，那是另一件事、也该由人显式做。

    这条在一次误删里是救命的：教师清场时删掉整场选手，如果连机器一起作废，
    第二天的比赛就得重新配对 50 台。
    """
    with ctx.db.session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return SimpleAck(ok=True, detail="选手不存在")
        require_confirm(player.player_no, confirm, "考号")

        contest_id = player.contest_id
        player_no = player.player_no
        files = session.execute(
            select(func.count(SourceFile.id)).where(SourceFile.player_id == player_id)
        ).scalar_one()
        runs = session.execute(
            select(func.count(JudgeRun.id)).where(JudgeRun.player_id == player_id)
        ).scalar_one()
        # 只统计"有多少台机器绑到这个人身上"，用于回执 —— 但一台都不动
        bound_machines = session.execute(
            select(func.count(Agent.id))
            .join(RosterEntry, Agent.roster_entry_id == RosterEntry.id)
            .where(
                RosterEntry.player_no == player.player_no, Agent.revoked_at.is_(None)
            )
        ).scalar_one()

        session.delete(player)
        session.add(
            EventLog(
                level="warning",
                category="player_delete",
                contest_id=contest_id,
                message="删除选手 %s（代码 %d 条，成绩 %d 条）"
                % (player_no, files, runs),
            )
        )

    detail = "已删除选手 %s（代码 %d 条，成绩 %d 条）" % (player_no, files, runs)
    if bound_machines:
        detail += "。他的 %d 台机器没有动 —— 那些机器绑的是人，不是这场比赛的记录" % (
            bound_machines
        )
    return SimpleAck(ok=True, detail=detail)


@router.post("/contests/{contest_id}/players/clear", response_model=SimpleAck)
def clear_players(
    contest_id: int,
    payload: PlayerClearIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """清空场次的选手名单。

    ``keep_with_submissions`` 默认 **True**：已经有代码或成绩的选手留着。
    理由和名单应用里的 ``prune`` 一样 —— 顺手把参赛者的提交一起删掉是不可逆的
    事故，而且它不报错。真要连提交一起清，得显式传 ``false``（界面上的按钮
    会写明这一点）。

    确认内容是**场次 slug**：这次删的不是某一个选手，而是"这场比赛的整份名单"。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")
        require_confirm(contest.slug, payload.confirm, "场次标识")

        keep_with_submissions = payload.keep_with_submissions
        players = list(
            session.execute(select(Player).where(Player.contest_id == contest_id)).scalars()
        )
        protected_ids = rosters._players_with_evidence(session, contest_id)

        removed = 0
        protected = []
        for player in players:
            if keep_with_submissions and player.id in protected_ids:
                protected.append(player.player_no)
                continue
            session.delete(player)
            removed += 1

        session.add(
            EventLog(
                level="warning",
                category="players_clear",
                contest_id=contest_id,
                message="清空选手名单：删除 %d 名，保留 %d 名（有提交）"
                % (removed, len(protected)),
            )
        )

    detail = "已删除 %d 名选手" % removed
    if protected:
        detail += "；保留 %d 名有提交的：%s" % (
            len(protected),
            "、".join(protected[:10]) + ("…" if len(protected) > 10 else ""),
        )
    return SimpleAck(ok=True, detail=detail)


# --------------------------------------------------------------------------- #
# 注册码
# --------------------------------------------------------------------------- #



# --------------------------------------------------------------------------- #
# 在线状态 / 文件台账 / 审计
# --------------------------------------------------------------------------- #


@router.get("/contests/{contest_id}/agents", response_model=Page[AgentRuntimeOut])
def list_agents(
    contest_id: int,
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[AgentRuntimeOut]:
    """这场比赛的机器台账 + 在线状态。

    **台账来自数据库，在线状态来自内存注册表。** 两者都要，缺一不可：

    * 只读注册表的话，机器在"配对完成"到"第一次心跳"之间是**看不见的** ——
      而教师刚配对完，正是最想确认"它到底认到没有"的那一刻
    * 只读库的话，就永远不知道谁在线

    所以先按"绑的人在这场比赛的名单里"从库里捞出全部机器（这就是台账），
    再用注册表里的心跳信息盖上在线状态。还没心跳过的机器显示为离线。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")

        players = {
            row.player_no: row
            for row in session.execute(
                select(Player).where(Player.contest_id == contest_id)
            ).scalars()
        }
        if not players:
            return slice_page([], page)

        # 机器绑的是"人"，而这个人算不算"这场比赛的人"取决于名单有没有被应用。
        # 显式指定了别的场次的机器不在这里出现 —— 它已经明确说了自己归哪一场。
        agents = list(
            session.execute(
                select(Agent)
                .join(RosterEntry, Agent.roster_entry_id == RosterEntry.id)
                .where(
                    RosterEntry.player_no.in_(list(players.keys())),
                    Agent.revoked_at.is_(None),
                    or_(Agent.contest_id.is_(None), Agent.contest_id == contest_id),
                )
                .order_by(Agent.id)
            ).scalars()
        )
        slug = contest.slug
        no_by_entry = {
            entry.id: entry.player_no
            for entry in session.execute(
                select(RosterEntry).where(RosterEntry.id.in_([a.roster_entry_id for a in agents]))
            ).scalars()
        }

    items = []
    for agent in agents:
        player = players.get(no_by_entry.get(agent.roster_entry_id, ""))
        if player is None:  # pragma: no cover - 上面的 join 已经保证了
            continue
        runtime = ctx.registry.get(agent.id)
        if runtime is not None and runtime.contest_id == contest_id:
            # 心跳过：在线状态、扫描目录、磁盘余量、文件数都以注册表为准
            items.append(AgentRuntimeOut(**runtime.to_public_dict()))
            continue
        items.append(
            AgentRuntimeOut(
                agent_id=agent.id,
                player_id=player.id,
                contest_id=contest_id,
                contest_slug=slug,
                player_no=player.player_no,
                player_name=player.name,
                machine_id=agent.machine_id,
                hostname=agent.hostname,
                agent_version=agent.agent_version,
                online=False,
                # 离线机器没有运行时对象，但"上一次报的扫描根缺失"仍要显示：
                # 目录没建这件事不会因为机器掉线就变得不重要
                scan_missing=scan_missing.load_scan_missing(agent.scan_missing_json),
            )
        )
    return slice_page(items, page)


@router.post("/agents/{agent_id}/rebind", response_model=SimpleAck)
def rebind_agent(
    agent_id: int,
    payload: RebindAgentIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """把一台机器改派给名单里的另一个人（换人）。

    这是"换座位"最自然的做法：机器上的凭据不用动、不用重启、不用重新配对 ——
    服务端改一下绑定，Agent 下一轮 tick 就拿到新的准考证号、自己更新扫描目录。

    **为什么不做成"作废旧凭据 + 重新注册"**：那条路要机器重新走一遍注册，
    而走统一密钥的机器读不到 root 只读的密钥，只能等到下次开机由注册单元处理。
    考场上"换个人"要等到重启，这是不能接受的。

    目标条目**必须还没有机器**：两台机器绑同一个人，代码会往同一个目录里写，
    而且完全静默（成绩矩阵只是看起来"这个人交了两遍"）。
    """
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="这台机器不在台账里")
        if agent.revoked_at is not None:
            raise HTTPException(status_code=409, detail="这台机器的凭据已作废，不能改派")

        entry = session.get(RosterEntry, payload.roster_entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="名单条目不存在")

        current = session.get(RosterEntry, agent.roster_entry_id) if agent.roster_entry_id else None
        if current is not None and current.id == entry.id:
            return SimpleAck(ok=True, detail="这台机器本来就属于 %s" % entry.player_no)

        clash = session.execute(
            select(Agent).where(
                Agent.roster_entry_id == entry.id,
                Agent.revoked_at.is_(None),
                Agent.id != agent_id,
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ApiError(
                409,
                "roster_entry_taken",
                "%s 已经有一台机器了（%s）。要换机器请先作废那一台，或者用它改派。"
                % (entry.player_no, clash.machine_id[:16]),
                {"player_no": entry.player_no, "machine_id": clash.machine_id},
            )

        old_no = current.player_no if current else "（未配对）"
        agent.roster_entry_id = entry.id
        agent.claimed_at = utcnow()
        # 改派之后原来那台的配对码/场次指定都该作废，让它按新身份重新解析
        agent.pair_code_hash = None
        agent.pair_code_expires_at = None
        agent.contest_id = None
        session.add(
            EventLog(
                level="warning",
                category="agent_rebind",
                message="机器改派：%s → %s（%s）"
                % (old_no, entry.player_no, agent.machine_id[:16]),
            )
        )
        session.flush()

    # 内存里的在线状态也要跟着清掉：旧的 player_id/contest 已经不作数了，
    # 留着它会让「选手状态」页一直显示错误的归属
    ctx.registry.forget(agent_id)

    return SimpleAck(
        ok=True,
        detail="已把机器从 %s 改派给 %s。它下一轮心跳就会切换到新身份，无需重启"
        % (old_no, entry.player_no),
    )


@router.post("/agents/{agent_id}/contest", response_model=SimpleAck)
def set_agent_contest(
    agent_id: int,
    payload: SetAgentContestIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """给一台机器显式指定（或取消指定）场次。

    默认是**动态解析**：找一个"进行中、且名单含此人"的场次。只有在一个考点
    同时跑多场比赛、同一个人两边都在时才需要指定 —— 那种情况下机器的 tick
    响应里会明确说"需要指定场次"，而不是自己挑一个。

    传 ``contest_id=null`` 就是取消指定、回到自动。
    """
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="这台机器不在台账里")
        if agent.roster_entry_id is None and payload.contest_id is not None:
            raise HTTPException(
                status_code=409, detail="这台机器还没有配对到人，先配对再指定场次"
            )

        if payload.contest_id is None:
            agent.contest_id = None
            detail = "已取消指定场次，改回自动匹配"
        else:
            contest = session.get(Contest, payload.contest_id)
            if contest is None:
                raise HTTPException(status_code=404, detail="场次不存在")
            agent.contest_id = contest.id
            detail = "已指定场次「%s」" % contest.name
        session.flush()

    # 归属变了，内存里的解析结果立刻作废 —— 下一轮 tick 会重新解析
    ctx.registry.forget(agent_id)
    return SimpleAck(ok=True, detail=detail)


@router.delete("/agents/{agent_id}/bind", response_model=SimpleAck)
def unbind_agent(
    agent_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """解除机器与人的绑定（不是作废凭据）。

    解绑之后机器回到"待配对"，并在下一次心跳里重新拿到一个配对码 ——
    这样它能被绑给别人，而不用重新注册（重新注册要读 root 只读的密钥，
    在考场上等于要重启）。

    要彻底让一台机器下线请用「作废」。
    """
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            return SimpleAck(ok=True, detail="这台机器不在台账里")
        if agent.roster_entry_id is None:
            return SimpleAck(ok=True, detail="这台机器本来就没配对")

        entry = session.get(RosterEntry, agent.roster_entry_id)
        player_no = entry.player_no if entry else "?"
        agent.roster_entry_id = None
        agent.contest_id = None
        agent.claimed_at = None
        # 老配对码早就作废了，让下一轮注册/心跳重新生成一个
        agent.pair_code_hash = None
        agent.pair_code_expires_at = None
        session.add(
            EventLog(
                level="warning",
                category="agent_unbind",
                message="解除机器配对：%s（%s）" % (player_no, agent.machine_id[:16]),
            )
        )
        session.flush()

    ctx.registry.forget(agent_id)
    # 解绑之后**必须**把缓存里的旧码忘掉：那台机器马上要重新配一次，
    # 留着旧码会让服务端继续回一个已经不该再用的数字，教师怎么输都不对
    ctx.pair_codes.forget(agent_id)
    return SimpleAck(
        ok=True,
        detail="已解除 %s 与这台机器的绑定。它下一轮心跳会拿到新的配对码" % player_no,
    )


@router.delete("/agents/{agent_id}", response_model=SimpleAck)
def revoke_agent(
    agent_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入机器名"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """作废一台机器的凭据（硬删）。

    删行而不是打 ``revoked_at`` 标记，有两个具体原因：

    * ``agent`` 表上有机器 UUID 唯一约束。留着那一行，同一台机器重新注册时
      会撞唯一约束 —— 而"作废后重新配对"正是最常见的后续动作。
    * 凭据是哈希存的行，删掉它就等于立刻失效；留着标记还要在每个鉴权点记得查。

    代价是这台机器的历史在线记录（``agent_status``）会一起级联删掉。
    要保留历史就别删，用「改派」。
    """
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            return SimpleAck(ok=True, detail="这台机器不在台账里")
        # 标识用机器名：教师是在列表里"指着那一行"删的，而列表上显示的就是它
        require_confirm(agent.hostname or agent.machine_id, confirm, "机器名")
        entry = session.get(RosterEntry, agent.roster_entry_id) if agent.roster_entry_id else None
        player_no = entry.player_no if entry else "（未配对）"
        machine_id = agent.machine_id
        session.delete(agent)
        session.add(
            EventLog(
                level="warning",
                category="agent_revoke",
                message="作废机器凭据：%s @ %s" % (player_no, machine_id[:16]),
            )
        )

    ctx.registry.forget(agent_id)
    return SimpleAck(
        ok=True,
        detail="已作废 %s 那台机器的凭据。它下次心跳会被拒，届时需要重新注册（或改用「改派」）"
        % player_no,
    )


@router.post("/agents/{agent_id}/uninstall", response_model=SimpleAck)
def uninstall_agent(
    agent_id: int,
    request: Request,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入机器名"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """让一台考试机把自己**彻底卸载**。

    服务端这里只做一件事：记下"教师点过卸载"。真正的授权是下一次心跳随
    ``uninstall_token`` 发下去的那枚签名令牌，机器本地用已有的升级信任锚验过
    才执行删除 —— 所以这个端点在没有签名私钥的服务端上**根本没有意义**，
    宁可当场拒绝并给出一条能手工执行的替代命令。

    为什么不做成"服务端直接下发一条卸载命令"：Agent 以选手账号运行，而它要删的
    全是 root 的东西。授权必须是一样**选手伪造不出来、又搬不到别的机器上**的
    东西，签名令牌正好是；一条明文的 shell 命令不是。

    审计里**不写令牌明文**：那枚令牌能在那台机器上换一次 root 删除，写进事件
    表等于把一把一次性钥匙抄进一份到处被导出、被翻看的日志里。
    """
    with ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise ApiError(404, "agent_not_found", "这台机器不在台账里")

        # 不可逆动作：与「作废」同一套"把机器名打一遍"的规矩
        machine_name = agent.hostname or agent.machine_id
        require_confirm(machine_name, confirm, "机器名")

        # 私钥是这套机制的**前提**：没有它，"服务端签发"这件事本身不存在。
        # 放在这里（而不是让它走到下面顺手报个 500）是因为这道门禁与「发布当前
        # 版本」是同一道，失败原因是"服务端没配好"，与机器那边的状态无关。
        if ctx.signing_key is None:
            raise ApiError(
                400,
                "uninstall_unavailable",
                _uninstall_fallback_detail(
                    ctx,
                    request,
                    lead="这台服务端没有配置发布签名私钥，签不出卸载授权（%s）。"
                    % (ctx.signing_key_error or "请设置 SYNCOJ_RELEASE_KEY"),
                ),
            )

        if not agent.release_public_key_at:
            raise ApiError(
                400,
                "uninstall_unavailable",
                _uninstall_fallback_detail(
                    ctx,
                    request,
                    lead="这台机器最近一次心跳没有报告发布公钥，它验不了卸载授权。",
                ),
            )

        machine_uuid = agent.machine_uuid
        if not machine_uuid:
            # 老机器可能在迁移里丢了 UUID。没有 UUID 就没法把授权绑死在这一台上，
            # 而"绑不上"的授权等于允许拿到它的任何机器删自己 —— 不发。
            raise ApiError(
                400,
                "uninstall_unavailable",
                _uninstall_fallback_detail(
                    ctx, request, lead="这台机器没有报告机器 UUID，卸载授权绑不到它身上。"
                ),
            )

        ctx.pending_uninstalls.request(
            machine_uuid,
            agent_id=agent.id,
            admin=admin.username,
            now=utcnow(),
        )
        session.add(
            EventLog(
                level="warning",
                category="agent_uninstall",
                message="请求卸载机器：%s @ %s（由 %s 发起）"
                % (machine_name, agent.machine_id[:16], admin.username),
            )
        )

    return SimpleAck(
        ok=True,
        detail="已记下这台机器的卸载请求：它下次心跳会拿到卸载授权，届时自己清干净；"
        "授权 %d 分钟内有效，机器一直没上线就重新点一次" % (uninstall.TOKEN_TTL_SECONDS // 60),
    )


def _uninstall_fallback_detail(
    ctx: AppContext, request: Request, lead: Optional[str] = None
) -> str:
    """发不出卸载授权时给教师看的那句话。

    **必须带一条能照着做的命令**：这条路走不通时，教师手里唯一剩下的办法就是
    站到机器前面跑装机页那条卸载命令。只说"不可用"等于把人卡在半路。
    """
    base = (discovery.describe_advertised_url(
        ctx, request.client.host if request.client else None
    ) or "").rstrip("/")
    command = uninstall.fallback_uninstall_command(base)
    head = lead or "这台服务端没有配置发布签名私钥，签不出卸载授权。"
    return "%s改用装机页那条卸载命令，到那台机器上执行：%s" % (head, command)


@router.get("/contests/{contest_id}/files", response_model=Page[SourceFileOut])
def list_files(
    contest_id: int,
    page: PageParams = Depends(),
    player_id: Optional[int] = None,
    include_deleted: bool = False,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[SourceFileOut]:
    """这场比赛回收上来的代码台账。

    ``total`` 是**过滤后**的总条数（含 ``player_id`` 筛选），因为前端要拿它
    显示"共 N 份"并且据此分页；拿本页条数充数会让第二页显示"共 50 份"。
    """
    with ctx.db.session() as session:
        # 归属是**算出来的**，不是存下来的：教师改一个模式，界面上立刻跟着变，
        # 不需要重收文件，也不用担心存量数据里的旧归属变成脏数据
        rules = contest_problem_rules(session, contest_id, ctx.settings)

        filters = [Player.contest_id == contest_id]
        if player_id is not None:
            filters.append(SourceFile.player_id == player_id)
        if not include_deleted:
            filters.append(SourceFile.deleted_at.is_(None))

        total = session.execute(
            select(func.count(SourceFile.id))
            .join(Player, SourceFile.player_id == Player.id)
            .where(*filters)
        ).scalar_one()

        stmt = (
            select(SourceFile, Player.player_no)
            .join(Player, SourceFile.player_id == Player.id)
            .where(*filters)
            .order_by(SourceFile.player_id, SourceFile.rel_path)
            .limit(page.limit)
            .offset(page.offset)
        )

        items = [
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
        return page_of(items, total, page)


@router.get("/files/{file_id}/content")
def download_source_file(
    file_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Response:
    """下载一份回收上来的代码。

    "收代码"的系统收完之后教师只能去磁盘上翻 ``source/``，这是最刺眼的一处缺口 ——
    而这个端点让"看一眼某个学生交了什么"变成一次点击。

    内容走内容寻址的 blob 存储，所以这里是**按哈希取文件**，
    不存在路径穿越的可能（``rel_path`` 只用来决定下载时显示的文件名）。
    """
    with ctx.db.session() as session:
        row = session.get(SourceFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="这份文件不在台账里")
        sha256 = row.sha256
        rel_path = row.rel_path
        stored = bool(row.content_stored)

    if not stored or not ctx.blobs.has(sha256):
        raise HTTPException(
            status_code=404,
            detail="这条台账只有索引，内容从未成功上传过（Agent 上报了它，但上传没完成）",
        )

    # 用 basename 做下载名：rel_path 里的目录结构对教师没用，反而会让浏览器
    # 把文件名拼成一长串
    filename = rel_path.rsplit("/", 1)[-1] or "code.txt"
    return _blob_response(ctx, sha256, download_name=filename)


def _blob_response(ctx: AppContext, sha256: str, download_name: str) -> Response:
    """把 blob 作为附件流出去。

    流式而不是整体读进内存：``max_file_size`` 是可配的，默认 2MB 但有人会调大，
    而"下载一个几百 MB 的文件把服务端内存打满"是完全没必要的失败模式。
    """
    from urllib.parse import quote

    handle = ctx.blobs.open(sha256)
    size = ctx.blobs.path_for(sha256).stat().st_size

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    # 用 RFC 5987 的 filename* 传中文名；filename 那个 ASCII 版本是给老浏览器兜底
    ascii_name = download_name.encode("ascii", "replace").decode("ascii") or "code.txt"
    disposition = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name.replace('"', "_"),
        quote(download_name, safe=""),
    )
    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition, "Content-Length": str(size)},
    )


@router.delete("/files/{file_id}", response_model=SimpleAck)
def delete_source_file(
    file_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删掉台账里的一条代码记录（连同不再被引用的内容）。

    三件事必须说清楚，否则这个按钮会让人误判：

    1. **不会删除选手机器上的文件。** 服务端管不到那边。
    2. **文件还在的话，下一轮 tick 会把它重新收上来。** 这个按钮的持久用途是
       清掉"选手已经删掉、服务端还留着墓碑记录"的那些行，以及把误收的文件
       从归档里去掉。
    3. 内容按哈希共享：只有**没有任何其他记录引用**这份内容时才会真删掉它。
    """
    with ctx.db.session() as session:
        row = session.get(SourceFile, file_id)
        if row is None:
            return SimpleAck(ok=True, detail="这条记录已经不在了")
        player = session.get(Player, row.player_id)
        rel_path = row.rel_path
        sha256 = row.sha256
        was_live = row.deleted_at is None
        player_no = player.player_no if player else "?"
        session.delete(row)
        session.flush()
        freed = _release_blob_if_unreferenced(session, ctx, sha256)

    detail = "已删除 %s 的台账记录 %s" % (player_no, rel_path)
    if was_live:
        detail += "。注意：文件若仍在选手机器上，下一轮会被重新收上来"
    if freed:
        detail += "；内容已从存储中清除"
    return SimpleAck(ok=True, detail=detail)


def _release_blob_if_unreferenced(session, ctx: AppContext, sha256: str) -> bool:
    """没有任何记录再引用这份内容时，把它从存储里删掉。

    内容寻址存储是**三处共用**的：回收上来的代码、下发的资产、发布的 Agent 包。
    所以"还有没有人用"必须一次查全 —— 只查一处会在跨用途共享时报错删掉
    别人还在用的内容，而表现是"某个早就归档的文件突然取不到内容"，
    不报错、只是 404，事后极难对上原因。

    **这个函数只能有一个定义。** 第一版按用途各写了一份，
    于是同名的那个把先定义的静默覆盖掉了 —— 而 Python 不会为此发任何警告。
    现在收成一个：以后再加内容消费者，改这一处。
    """
    references = (
        (SourceFile, SourceFile.sha256),
        (Asset, Asset.sha256),
        (AgentRelease, AgentRelease.sha256),
    )
    for model, column in references:
        left = session.execute(
            select(func.count(model.id)).where(column == sha256)
        ).scalar_one() or 0
        if left:
            return False

    try:
        ctx.blobs.path_for(sha256).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover
        log.warning("删除 blob %s 失败：%s", sha256, exc)
        return False


@router.post("/contests/{contest_id}/files/clear", response_model=SimpleAck)
def clear_source_files(
    contest_id: int,
    payload: FileClearIn,
    player_id: Optional[int] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """清理代码台账。

    这是**软删除**（打墓碑）的批量版本：行先删掉，``blobs/`` 里的内容只在
    没有任何记录再引用它时才释放 —— 见 ``docs/reference/api-conventions.md`` §5.2。

    默认 ``purge=false``：只清掉**已经消失**的墓碑记录（选手删了文件之后留下的
    那些），这不会影响任何还在的东西 —— 也是这个操作最常见的用途。

    ``purge=true`` 会连活的一起删，但**机器还在报的文件下一轮就会回来**，
    所以它只适合"比赛结束后归档完毕、准备清场"。界面上的按钮要写明这一点。

    确认内容是**场次 slug**。
    """
    purge = payload.purge
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")
        require_confirm(contest.slug, payload.confirm, "场次标识")

        stmt = (
            select(SourceFile)
            .join(Player, SourceFile.player_id == Player.id)
            .where(Player.contest_id == contest_id)
        )
        if player_id is not None:
            stmt = stmt.where(SourceFile.player_id == player_id)
        if not purge:
            stmt = stmt.where(SourceFile.deleted_at.isnot(None))

        rows = list(session.execute(stmt).scalars())
        hashes = {row.sha256 for row in rows}
        for row in rows:
            session.delete(row)
        session.flush()

        freed = sum(1 for sha in hashes if _release_blob_if_unreferenced(session, ctx, sha))

        session.add(
            EventLog(
                level="warning",
                category="files_clear",
                contest_id=contest_id,
                message="清理代码台账：%d 条%s（释放 %d 份内容）"
                % (len(rows), "（含未消失的）" if purge else "（只清已消失的）", freed),
            )
        )

    detail = "已清理 %d 条台账记录，释放 %d 份内容" % (len(rows), freed)
    if purge:
        detail += "。机器还在报的文件会在下一轮重新出现"
    return SimpleAck(ok=True, detail=detail)


@router.get("/contests/{contest_id}/files/export")
def export_source_files(
    contest_id: int,
    player_id: Optional[int] = None,
    problem: Optional[str] = None,
    include_deleted: bool = False,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    """把回收的代码打包成一个 zip。

    包里有两个东西：

    * ``代码/`` —— 按 ``<准考证号>/<相对路径>`` 放，结构与 ``source/`` 一致，
      解压出来就能直接丢给 LemonLime / Arbiter
    * ``清单.csv`` —— 每份文件的**精确字节数与 SHA256**

    清单是这个功能的一半价值：教师拿到归档之后要能核对"是不是收全了、有没有传坏"，
    而只看文件列表做不到这一点（同名文件、大小相近的代码太常见了）。
    """
    with ctx.db.session() as session:
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
        stmt = stmt.order_by(Player.player_no, SourceFile.rel_path)

        rows = []
        for row, player_no in session.execute(stmt):
            ident = matching.match_problem(row.rel_path, rules)
            if problem and ident != problem:
                continue
            rows.append((row, player_no, ident))

    if not rows:
        raise HTTPException(status_code=404, detail="没有符合条件的代码可以导出")

    # 先落到临时文件再流出去：zip 需要能随机写（中央目录在末尾），
    # 而边生成边流式输出做不到这一点。Spooled 会先放内存，
    # 超过阈值自动落到磁盘 —— 小场次不碰盘，大场次不占内存。
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    try:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("清单.csv", _export_manifest(rows))
            used_paths: Dict[str, int] = {}
            for row, player_no, ident in rows:
                if not row.content_stored or not ctx.blobs.has(row.sha256):
                    # 内容缺失的条目仍然写进清单（教师要知道少了哪个），但不建空文件
                    continue
                arcname = _unique_arcname(used_paths, "代码/%s/%s" % (player_no, row.rel_path))
                with ctx.blobs.open(row.sha256) as handle:
                    archive.writestr(arcname, handle.read())
        spool.seek(0)
    except Exception:
        spool.close()
        raise

    return StreamingResponse(
        _spool_chunks(spool),
        media_type="application/zip",
        headers={
            "Content-Disposition": _attachment_header("代码归档-%d.zip" % contest_id),
        },
    )


def _export_manifest(rows) -> str:
    """导出的清单 CSV。

    带 BOM：Excel 打开中文列名才不会乱码（和成绩导出同一个理由）。
    """
    lines = [
        "准考证号,题目,相对路径,字节数,sha256,修订号,首次回收,最近出现,内容完整"
    ]
    for row, player_no, ident in rows:
        lines.append(
            ",".join(
                [
                    _csv_cell(player_no),
                    _csv_cell(ident or ""),
                    _csv_cell(row.rel_path),
                    str(int(row.size)),
                    row.sha256,
                    str(int(row.revision)),
                    _iso(row.first_seen_at) or "",
                    _iso(row.last_seen_at) or "",
                    "是" if row.content_stored else "否（只有索引）",
                ]
            )
        )
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


def _csv_cell(value: str) -> str:
    """CSV 单元格转义。路径里出现逗号是完全可能的。"""
    text = str(value)
    if any(ch in text for ch in ',"\r\n'):
        return '"%s"' % text.replace('"', '""')
    return text


def _unique_arcname(used: Dict[str, int], candidate: str) -> str:
    """zip 里同名条目要避开。

    什么情况下会重名：同一份 ``rel_path`` 在同一个选手名下只会有一条台账记录
    （以玩家+路径为唯一），但**导出全部选手**时，两个选手的 ``rel_path`` 相同
    是完全正常的 —— 所以外层套了准考证号目录。这里再兜一层是为了万一
    （比如台账里出现过大小写不同的路径），宁可多一个后缀，也不要覆盖丢文件。
    """
    if candidate not in used:
        used[candidate] = 1
        return candidate
    used[candidate] += 1
    stem, _, suffix = candidate.rpartition(".")
    if stem:
        return "%s(%d).%s" % (stem, used[candidate], suffix)
    return "%s(%d)" % (candidate, used[candidate])


def _attachment_header(filename: str) -> str:
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name.replace('"', "_"),
        quote(filename, safe=""),
    )


def _spool_chunks(spool) -> Iterator[bytes]:
    """把临时文件分块吐出去，结束后关掉。

    关闭放在生成器的 finally 里：客户端中途断开时生成器会被 GC，
    finally 仍然会跑 —— 否则每个中断的下载都会漏一个临时文件。
    """
    try:
        while True:
            chunk = spool.read(256 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            spool.close()
        except OSError:  # pragma: no cover
            pass


@router.get("/events", response_model=Page[EventOut])
def list_all_events(
    page: PageParams = Depends(),
    level: Optional[str] = None,
    category: Optional[str] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[EventOut]:
    """不按场次过滤的审计事件。

    有些事件**根本不属于任何场次**，而它们恰恰是最需要被看到的：
    "有机器用统一密钥注册上来了"、"某台机器报的硬件指纹与一台**在线**机器相同"。
    注册发生在配对之前，那台机器那时还没有场次归属 —— 如果只能按场次查，
    这些告警会写进库然后永远没人看见。那和没记录没有区别。
    """
    with ctx.db.session() as session:
        filters = []
        if level:
            filters.append(EventLog.level == level)
        if category:
            filters.append(EventLog.category == category)

        total = session.execute(
            select(func.count(EventLog.id)).where(*filters)
        ).scalar_one()
        stmt = (
            select(EventLog, Player.player_no)
            .outerjoin(Player, EventLog.player_id == Player.id)
            .where(*filters)
            .order_by(EventLog.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        items = [_event_out(row, player_no) for row, player_no in session.execute(stmt)]
        return page_of(items, total, page)


@router.get("/contests/{contest_id}/events", response_model=Page[EventOut])
def list_events(
    contest_id: int,
    page: PageParams = Depends(),
    category: Optional[str] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[EventOut]:
    with ctx.db.session() as session:
        filters = [EventLog.contest_id == contest_id]
        if category:
            filters.append(EventLog.category == category)

        total = session.execute(
            select(func.count(EventLog.id)).where(*filters)
        ).scalar_one()
        stmt = (
            select(EventLog, Player.player_no)
            .outerjoin(Player, EventLog.player_id == Player.id)
            .where(*filters)
            .order_by(EventLog.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        items = [_event_out(row, player_no) for row, player_no in session.execute(stmt)]
        return page_of(items, total, page)


@router.post("/events/clear", response_model=SimpleAck)
def clear_events(
    payload: ConfirmIn,
    contest_id: Optional[int] = None,
    level: Optional[str] = None,
    older_than_days: Optional[int] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """清空审计日志。

    ``older_than_days`` 是**推荐用法**：日志的价值在于事后回看，
    一把全清掉很容易把"三天前那台机器为什么掉线"的唯一线索抹掉。
    界面上的默认值就填一个天数，而不是"全部"。

    ``contest_id`` 留空表示所有场次（含不属于任何场次的那批全局事件，
    比如统一密钥注册与克隆告警）。

    确认内容跟着**范围**走：指定了场次就输场次 slug，全局清空则输 ``all`` ——
    因为这次删的不是某一个对象，而是"这一片日志"，输入名字才有意义。
    """
    from datetime import timedelta

    with ctx.db.session() as session:
        if contest_id is not None:
            contest = session.get(Contest, contest_id)
            if contest is None:
                raise HTTPException(status_code=404, detail="场次不存在")
            require_confirm(contest.slug, payload.confirm, "场次标识")
        else:
            require_confirm(GLOBAL_CONFIRM, payload.confirm, "清空确认")

        stmt = select(EventLog)
        if contest_id is not None:
            stmt = stmt.where(EventLog.contest_id == contest_id)
        if level:
            stmt = stmt.where(EventLog.level == level)
        if older_than_days is not None:
            cutoff = utcnow() - timedelta(days=max(0, older_than_days))
            stmt = stmt.where(EventLog.ts < cutoff)

        rows = list(session.execute(stmt).scalars())
        for row in rows:
            session.delete(row)
        if rows:
            # 记一条"谁清的"—— 否则日志被清这件事本身没有痕迹
            session.add(
                EventLog(
                    level="warning",
                    category="events_clear",
                    message="清理审计日志 %d 条（场次=%s，级别=%s，早于 %s 天）"
                    % (
                        len(rows),
                        contest_id if contest_id is not None else "全部",
                        level or "全部",
                        older_than_days if older_than_days is not None else "不限",
                    ),
                )
            )

    return SimpleAck(ok=True, detail="已清理 %d 条审计日志" % len(rows))


def _event_out(row: EventLog, player_no: Optional[str]) -> EventOut:
    meta = None
    if row.meta_json:
        try:
            meta = json.loads(row.meta_json)
        except ValueError:
            meta = {"_raw": row.meta_json[:500]}
    return EventOut(
        id=row.id,
        ts=_iso(row.ts) or "",
        level=row.level,
        category=row.category,
        player_no=player_no,
        message=row.message,
        meta=meta,
    )


@router.delete("/contests/{contest_id}", response_model=SimpleAck)
def delete_contest(
    contest_id: int,
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入场次标识"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删除场次**连同它的一切**。

    外键全是 ``ON DELETE CASCADE``，所以删一个场次会连带删掉它的选手、
    代码台账、下发任务、成绩、资产。这是整个系统里破坏力最大的一个操作，
    所以要求把场次标识**原样再打一遍** ——
    界面上常见的"你确定吗"点一下就过去了，代价却不可逆。

    返回里带上删掉了什么，让教师事后能对上账（也便于日志审阅）。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            return SimpleAck(ok=True, detail="场次不存在")

        require_confirm(contest.slug, confirm, "场次标识")

        counts = {
            "选手": _count(session, Player, Player.contest_id == contest_id),
            "代码文件": _count(
                session,
                SourceFile,
                SourceFile.player_id.in_(
                    select(Player.id).where(Player.contest_id == contest_id)
                ),
            ),
            "评测记录": _count(session, JudgeRun, JudgeRun.contest_id == contest_id),
            "下发任务": _count(session, DeployTask, DeployTask.contest_id == contest_id),
            "资产": _count(session, Asset, Asset.contest_id == contest_id),
        }
        slug = contest.slug
        name = contest.name
        session.delete(contest)
        session.add(
            EventLog(
                level="warning",
                category="contest_delete",
                message="删除场次「%s」（%s）：%s"
                % (
                    name,
                    slug,
                    "、".join("%s %d" % (k, v) for k, v in counts.items() if v),
                ),
            )
        )

    # 内存里的在线状态也要跟着清，否则顶栏会继续显示一台已经不存在的场次的机器
    for runtime in list(ctx.registry.all(contest_id)):
        ctx.registry.forget(runtime.agent_id)

    return SimpleAck(
        ok=True,
        detail="已删除场次「%s」：%s"
        % (name, "、".join("%s %d" % (k, v) for k, v in counts.items() if v) or "没有关联数据"),
    )


def _count(session, model, *conditions) -> int:
    return int(
        session.execute(select(func.count(model.id)).where(*conditions)).scalar_one() or 0
    )


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


def _validate_asset_filename(name: str) -> str:
    """校验并归一化资产文件名，返回能用的那一个。

    文件名会**参与 Agent 侧的落地路径**，所以按路径段的规则校验一遍：不允许斜杠、
    ``..``、控制字符 —— 否则"改个名字"就能把文件写到别的地方去。

    上传、改名、新建文本三条路都走它：同一条规则抄三遍，迟早有一处松掉。
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="文件名超过 255 字符")
    if name in (".", ".."):
        raise HTTPException(status_code=400, detail="文件名不合法")
    # 这一条是"文件名"而不是"相对路径"：`validate_relpath` 会**接受** `a/b.txt`
    # （那是合法相对路径），于是一个填了斜杠的文件名会让文件在选手机器上落进一个
    # 凭空多出来的子目录里 —— 界面说的明明是"文件名"。所以自己再拦一道。
    # 上传那条路不受影响：浏览器可能发来完整路径，那边先取 `Path(...).name` 剥掉。
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="文件名不能带路径分隔符（/ 或 \\）")
    try:
        validate_relpath(name, max_length=255)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail="文件名不合法: %s" % exc)
    return name


def _create_asset(
    ctx: AppContext,
    contest_id: int,
    *,
    sha256: str,
    size: int,
    filename: str,
    kind: str,
    session: Optional[Session] = None,
) -> AssetOut:
    """登记一个资产，内容已经在 blob 库里。

    抽出来是因为现在有两条创建路（上传文件、直接写文本），而"同名同内容就复用"
    这条判据必须一致 —— 各写一遍的话，一条会去重、另一条会造出两份一模一样的资产，
    界面上看起来就是"我明明只发了一次"。

    ``session`` 是给调用方**用自己的事务**用的：zip 打密码那一步要把 zip 资产与
    ``password.txt`` 一起改掉，各开一个会话的话 SQLite 上会互相锁，而且一半成功
    一半失败时没人能回滚。不给 session 就自己开一个短会话，上传与新建文本走的
    都是那条路。
    """
    if session is not None:
        return _create_asset_row(
            session, contest_id, sha256=sha256, size=size, filename=filename, kind=kind
        )
    with ctx.db.session() as own:
        return _create_asset_row(
            own, contest_id, sha256=sha256, size=size, filename=filename, kind=kind
        )


def _create_asset_row(
    session: Session, contest_id: int, *, sha256: str, size: int, filename: str, kind: str
) -> AssetOut:
    contest = session.get(Contest, contest_id)
    if contest is None:
        raise HTTPException(status_code=404, detail="场次不存在")

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


#: 上传转存到临时文件时的块大小。与 blob 存储同一个量级。
_UPLOAD_CHUNK = 1024 * 1024


def _packaged_filename(filename: str) -> str:
    """打包之后资产的新文件名：``题面.pdf`` → ``题面.zip``。

    只换**最后一个**后缀（``样例.tar.gz`` → ``样例.tar.zip``）；名字本来就以
    ``.zip`` 结尾时回到它自己，不叠成 ``x.zip.zip``。zip 里的成员名仍然是教师
    上传时的那个名字（``题面.pdf``），改名只发生在"资产"这一层。

    用 ``Path(...).stem`` 而不是 ``with_suffix``：只有后缀的名字（``.zip``）会让
    ``with_suffix`` 抛 ValueError —— 一个改名动作不该因为文件名怪就变成 500。
    """
    stem = Path(filename).stem
    return (stem + ".zip") if stem else (filename + ".zip")


def _spool_upload(source, dest, *, max_bytes: int) -> int:
    """把上传流原样转存进可 seek 的 ``dest``，返回字节数（超过上限就是 413）。

    打包要先算出 CRC 才能写加密头（见 ``zipcrypto.pack_member``），而那一步要求
    源可 seek —— ``UploadFile.file`` 不保证可以。上限那句话与 ``put_stream`` 的
    一致，这样"太大"的报错不会因为走的是哪条路而换一个说法。
    """
    size = 0
    while True:
        chunk = source.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail="内容超过上限 %d 字节" % max_bytes
            )
        dest.write(chunk)
    dest.flush()
    dest.seek(0)
    return size


def _upload_packaged_asset(
    ctx: AppContext,
    contest_id: int,
    source,
    *,
    filename: str,
    kind: str,
    zip_password: str,
) -> AssetOut:
    """上传时就把字节包成 zip（可选加密），并同步写 ``password.txt``。

    密码留空 = 服务端生成一个：这个入口的另一半职责就是把密码写进
    ``password.txt``，没有密码就没有那份文件 —— 教师选了「打包并加密」却拿到一个
    连自己都不知道口令的包，是比"多生成一个密码"坏得多的结果。

    落库与写 ``password.txt`` 在**同一个会话**里做（``_create_asset`` 的
    ``session`` 参数就是为这件事留的）：一半成功一半失败时能整体回滚，不会出现
    "zip 已经是新的、密码文件还是旧的"。
    """
    password = zip_password or zipcrypto.generate_password()
    zip_filename = _packaged_filename(filename)

    _reject_password_file_name(filename)

    # 与"给已有资产打密码"那条路同一道闸门：``password.txt`` 不许再被打成 zip。
    # 判据在这里（而不是只在界面上）：界面管不到"上传时勾了打包"那条路。

    with tempfile.TemporaryFile() as raw, tempfile.TemporaryFile() as packed:
        _spool_upload(source, raw, max_bytes=ctx.settings.max_asset_size)
        try:
            zipcrypto.pack_member(
                raw, packed, member_name=filename, password=password.encode("utf-8")
            )
        except zipcrypto.ZipCryptoError as exc:
            raise ApiError(
                400, "zip_package_failed", "打包「%s」失败（%s）" % (filename, exc)
            )
        packed.seek(0)
        try:
            sha256, size = ctx.blobs.put_stream(
                packed, max_bytes=ctx.settings.max_asset_size
            )
        except BlobTooLarge as exc:  # pragma: no cover - 2 GB 上限下走不到
            raise HTTPException(status_code=413, detail=str(exc))
        except HashMismatch as exc:  # pragma: no cover - 未声明哈希时不会触发
            raise HTTPException(status_code=400, detail=str(exc))

    with ctx.db.session() as session:
        created = _create_asset(
            ctx,
            contest_id,
            sha256=sha256,
            size=size,
            filename=zip_filename,
            kind=kind,
            session=session,
        )
        asset = session.get(Asset, created.id)
        if asset is None:  # pragma: no cover - _create_asset 刚 flush 过
            raise ApiError(404, "asset_not_found", "资产不存在")
        password_asset, password_requeued = _upsert_password_asset(
            ctx, session, contest_id, zip_asset=asset, password=password
        )
        session.add(
            EventLog(
                level="info",
                category="asset_zip_password",
                contest_id=contest_id,
                message="上传时就把「%s」打包成 zip（成员名 %s，新文件名「%s」，"
                "sha %s），同步写好密码文件 password.txt（资产 #%d，重排 %d 台）"
                % (
                    filename,
                    filename,
                    zip_filename,
                    sha256[:8],
                    password_asset.id,
                    password_requeued,
                ),
            )
        )
        session.flush()
        return _asset_out(asset, _pending_targets(session, asset.id))


@router.post("/contests/{contest_id}/assets", response_model=AssetOut)
def upload_asset(
    contest_id: int,
    file: UploadFile = File(...),
    kind: str = Form("testdata"),
    package_zip: bool = Form(False),
    zip_password: str = Form(""),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetOut:
    """上传一个待下发文件。

    走与服务端回收同一套内容寻址存储：相同内容只占一份磁盘。因此"给全场下发
    同一份 500MB 测试点"实际只消耗 500MB，而不是 50 × 500MB。

    ``package_zip=true`` 时**磁盘上直接只存打包后的 zip**：成员名是原文件名，
    资产文件名是 ``<原基名>.zip``，并同步写/更新一份 ``password.txt``。这里刻意
    不做"先把原文件存下来再改"：那会凭空留下一份谁都不引用的原始 blob，而"这个
    资产是什么"从落盘那一刻起就只能是那个 zip。
    """
    filename = _validate_asset_filename(Path(file.filename or "unnamed").name)

    if package_zip:
        return _upload_packaged_asset(
            ctx,
            contest_id,
            file.file,
            filename=filename,
            kind=kind,
            zip_password=zip_password,
        )

    try:
        sha256, size = ctx.blobs.put_stream(
            file.file,
            max_bytes=ctx.settings.max_asset_size,
        )
    except BlobTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except HashMismatch as exc:  # pragma: no cover - 未声明哈希时不会触发
        raise HTTPException(status_code=400, detail=str(exc))

    return _create_asset(
        ctx, contest_id, sha256=sha256, size=size, filename=filename, kind=kind
    )


def _normalize_text_content(content: str) -> str:
    """把浏览器里来的正文归一成"真正落盘的那一份"。

    两条规则，新建文本资产与在线编辑**共用这一份实现**（各写一遍的话，
    "新建时去掉的 BOM"迟早会在编辑那条路上被存回去）：

    * **去掉开头的 BOM**。从 Windows 记事本粘过来的文本可能带 U+FEFF，
      它到 Linux 上会变成文件开头三个看不见的字节。
    * **换行统一成 LF**。浏览器 textarea 给的是 ``\\n``，但从别处粘进来的内容
      可能带 ``\\r\\n``；目标机是 Linux，留着 ``\\r`` 会在选手的编辑器里
      显示成 ``^M``，看起来像文件坏了。
    """
    if content.startswith("\ufeff"):
        content = content[1:]
    return content.replace("\r\n", "\n").replace("\r", "\n")


@router.post("/contests/{contest_id}/assets/text", response_model=AssetOut)
def create_text_asset(
    contest_id: int,
    payload: AssetTextIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetOut:
    """**直接写一个纯文本资产**（`须知.txt`、`README.md`、`说明.md`…）。

    为什么需要它：教师想给选手发一段"注意事项"，而为此要在自己机器上先造一个文件、
    再上传，是纯粹的仪式 —— 内容本来就是在浏览器里打的。发完之后它和上传的资产
    **完全一样**（同样的内容寻址、同样的下发流程），所以下游一行都不用改。

    两条边界，都是为了让"界面里写的东西"和"落到选手桌面上的文件"逐字节一致：

    * **换行统一成 LF**。浏览器 textarea 给的是 ``\\n``，但从别处粘进来的内容可能带
      ``\\r\\n``，而目标机是 Linux —— 留着那个 ``\\r`` 会在选手的编辑器里显示成
      ``^M``，看起来像文件坏了。
    * **BOM 去掉**。从 Windows 记事本粘过来的文本可能带 U+FEFF，它会在 Linux 上
      变成文件开头三个看不见的字节。
    """
    filename = _validate_asset_filename(payload.filename)
    _reject_binary_filename(filename)

    content = _normalize_text_content(payload.content)

    try:
        sha256, size = ctx.blobs.put_bytes(content.encode("utf-8"))
    except BlobTooLarge as exc:  # pragma: no cover - 上限由 schema 兜着
        raise HTTPException(status_code=413, detail=str(exc))

    log.info("新建文本资产：%s（%s，%d 字节）", filename, payload.kind, size)
    return _create_asset(
        ctx, contest_id, sha256=sha256, size=size, filename=filename, kind=payload.kind
    )


#: 明显是二进制的扩展名。文本入口只服务"随手写一段话"，把 zip/png 之类塞给它
#: 只会造出一个打不开的文件 —— 那时候教师看到的是"文件发下去了但学生打不开"。
_BINARY_SUFFIXES = frozenset(
    ".zip .tar .gz .tgz .bz2 .xz .7z .rar .jar .exe .msi .dll .so .dylib .bin "
    ".img .iso .db .sqlite .png .jpg .jpeg .gif .webp .bmp .ico .pdf .mp3 .mp4 "
    ".avi .mkv .mov .doc .docx .xls .xlsx .ppt .pptx".split()
)


def _reject_binary_filename(name: str) -> None:
    suffix = Path(name).suffix.lower()
    if suffix in _BINARY_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="%s 看起来是二进制文件 —— 请用「上传文件」，"
            "这里只能写纯文本（.txt / .md / .csv / .ini …）" % suffix,
        )


# --------------------------------------------------------------------------- #
# 在线改正文：能在线上改哪些文件、怎么判
# --------------------------------------------------------------------------- #

#: 能在线上直接改正文的扩展名**白名单**。
#:
#: 为什么是白名单而不是复用 `_BINARY_SUFFIXES` 那份黑名单：这个功能会**打开**
#: 一份别人的文件、把内容显示给人、再存回去。判据必须是"我认得它是文本"，而不是
#: "我没认出它是二进制" —— 后者对没登记的格式（`.doc`、`.psd`、`.dat`…）一律放行，
#: 放行的后果是在对话框里显示一份乱码、教师改两笔再保存，等于把一份好好的文件
#: 覆盖成半截乱码。
#:
#: 列表页靠这个集合逐行算 `AssetOut.editable`，所以它是**纯扩展名比较** ——
#: 一个字节的磁盘读都不做（见 `_is_editable_text`）。
_TEXT_SUFFIXES = frozenset(
    ".txt .md .markdown .csv .tsv .ini .cfg .conf .json .yaml .yml .log .sh .py "
    ".c .cpp .h .hpp .java .go .rs .js .ts .html .htm .tex .rst".split()
)

#: 能在线上改的正文体积上限（1 MiB）。
#:
#: **必须比「新建文本文件」那条路的上限宽**：新建走的是 ``AssetTextIn.content`` 的
#: 200000 **字符**上限，而 UTF-8 里一个汉字占 3 字节、一个 emoji 占 4 —— 也就是
#: 新建这条路径最多能造出 800 KB 上下的正文。编辑的上限如果比它小，就会出现
#: "刚在界面上写好的公告，回头想改一个字却说过大"，而教师完全无从理解。
#: 反过来宽一点没有代价：真要挡的是一屏塞不下的巨型 blob，不是这几十 KB。
#:
#: 与「新建文本文件」的 200000 字上限是**两回事**：那个管"能写多少"（schema 上的
#: 422），这个管"能不能打开"（400）。上限存在的理由是"打开"这件事本身 ——
#: 一个 300 KB 的 csv 是正常资产、也让改，但是把它整个塞进浏览器 textarea 再
#: PUT 回来没有任何意义，那种规模该在本地改完重新上传。
_TEXT_MAX_BYTES = 1024 * 1024


def _has_text_suffix(name: str) -> bool:
    return Path(name or "").suffix.lower() in _TEXT_SUFFIXES


def _is_editable_text(asset: Asset) -> bool:
    """这个资产**大概**能不能在线上改正文。

    只读元数据（扩展名 + 库里记的 size），所以列表页可以逐行调它而不碰磁盘。
    真正的"这一份到底行不行"在 :func:`_read_editable_text` 里 —— 那一步必须开文件，
    因为它要验的是字节能不能按 UTF-8 解出来，而这件事只有读了才知道。
    """
    return _has_text_suffix(asset.filename) and int(asset.size or 0) <= _TEXT_MAX_BYTES


def _require_editable_asset(asset: Asset) -> None:
    """按元数据判一次，不合格就 400。"""
    if not _has_text_suffix(asset.filename):
        raise ApiError(
            400,
            "asset_not_editable",
            "「%s」不是可在线编辑的文本类型 —— 只有 .txt / .md / .csv 这类文本文件"
            "能在线上改正文，其他格式请改完再上传" % asset.filename,
        )
    size = int(asset.size or 0)
    if size > _TEXT_MAX_BYTES:
        raise ApiError(
            400,
            "asset_not_editable",
            "「%s」有 %d KB，超过 1 MB 的在线编辑上限 —— 请改完再上传"
            % (asset.filename, size // 1024),
        )


def _read_editable_text(ctx: AppContext, asset: Asset) -> str:
    """读出一个可编辑文本资产的正文；不合格一律 400。

    两道关：

    1. **元数据**（扩展名 + 体积）—— 与列表页那个 ``editable`` 同一套判据
    2. **字节**（必须能按 UTF-8 解出来）—— 只能开文件才知道，所以它不在列表那条路上

    读出来的正文按与「新建文本文件」一致的方式归一（去 BOM、换行转 LF），这样
    **打开再原样保存是幂等的** —— 否则一份 CRLF 文件每被打开保存一次就变一次 sha，
    而那会连带把已经下发的机器全部重排一遍。
    """
    _require_editable_asset(asset)

    try:
        with ctx.blobs.open(asset.sha256) as handle:
            # 多读一个字节：库里的 size 是**记录**，真正要拦的是"实际读到的字节"。
            raw = handle.read(_TEXT_MAX_BYTES + 1)
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail="「%s」的内容已经不在服务端存储里了（%s）" % (asset.filename, exc),
        )

    if len(raw) > _TEXT_MAX_BYTES:
        raise ApiError(
            400,
            "asset_not_editable",
            "「%s」实际超过 1 MB 的在线编辑上限 —— 请改完再上传" % asset.filename,
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(
            400,
            "asset_not_utf8",
            "「%s」的字节不是合法的 UTF-8 文本（第 %d 个字节就解不出来），"
            "没法当成正文打开" % (asset.filename, exc.start + 1),
        )

    return _normalize_text_content(text)


def _contest_asset(session, contest_id: int, asset_id: int) -> Asset:
    """按「场次 + 资产」取一行，取不到就是 404。

    必须把 ``contest_id`` 一起查：只按 asset_id 取的话，拿别场的 id 来也能读到、
    还能改 —— 资产行本身不带权限信息，那等于跨场次越权。两个路由都走它，
    免得"GET 查了场次、PUT 忘了查"。
    """
    asset = session.get(Asset, asset_id)
    if asset is None or int(asset.contest_id) != int(contest_id):
        raise HTTPException(status_code=404, detail="资产不存在或不属于本场次")
    return asset


@router.get("/contests/{contest_id}/assets", response_model=Page[AssetOut])
def list_assets(
    contest_id: int,
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[AssetOut]:
    with ctx.db.session() as session:
        rows = list(
            session.execute(
                select(Asset).where(Asset.contest_id == contest_id).order_by(Asset.id.desc())
            ).scalars()
        )
        pending = _pending_target_counts(session, contest_id)
        return slice_page(
            [_asset_out(row, pending.get(row.id, 0)) for row in rows], page
        )


def _asset_out(asset: Asset, pending_targets: int = 0) -> AssetOut:
    return AssetOut(
        id=asset.id,
        contest_id=asset.contest_id,
        sha256=asset.sha256,
        size=int(asset.size),
        filename=asset.filename,
        kind=asset.kind,
        created_at=_iso(asset.created_at) or "",
        pending_targets=int(pending_targets),
        # 逐行算，但**不读盘**（见 `_is_editable_text`）：列表页一屏几十行，
        # 为了这一个布尔逐个打开 blob 会把一次列表请求变成几十次磁盘读。
        editable=_is_editable_text(asset),
    )


def _pending_target_counts(session, contest_id: int) -> Dict[int, int]:
    """每个资产还有几个选手没下载完。

    一次分组查询，而不是逐行去数 —— 资产列表一屏几十行，
    N+1 会把一次列表请求变成几十次查询。
    """
    rows = session.execute(
        select(DeployTask.asset_id, func.count(DeployTarget.id))
        .join(DeployTarget, DeployTarget.task_id == DeployTask.id)
        .where(
            DeployTask.contest_id == contest_id,
            DeployTarget.status.in_(ACTIVE_DEPLOY_STATUSES),
        )
        .group_by(DeployTask.asset_id)
    ).all()
    return {asset_id: int(count) for asset_id, count in rows}


def _pending_targets(session: Session, asset_id: int) -> int:
    """一个资产还有几个下发目标没落地（``pending`` / ``ready``）。

    与 :func:`_pending_target_counts` 是同一个判据的单资产版本：列表页用前者
    一次算完一屏，改内容/改名这类单资产动作要的就是这一个数。
    """
    pending = session.execute(
        select(func.count(DeployTarget.id))
        .join(DeployTask, DeployTarget.task_id == DeployTask.id)
        .where(
            DeployTask.asset_id == asset_id,
            DeployTarget.status.in_(ACTIVE_DEPLOY_STATUSES),
        )
    ).scalar_one()
    return int(pending or 0)


def _requeue_done_targets(session: Session, asset_id: int) -> int:
    """把已经 ``done`` 的下发目标改回 ``pending``，返回被重排的台数。

    "换了内容"必须重排：``services/deploy.py`` 的 tick 按**当前**的
    ``asset.sha256`` 组装 job、且只取 ``pending/ready`` 的目标，所以不重排的话
    选手页显示的是新内容、机器上躺着的还是旧文件，而界面上一切正常。

    重排时**必须把续传偏移一起清零**：拿旧文件的偏移去续新文件，拼出来的是一份
    永远校验不过的文件。还没落地（``pending``/``ready``）的目标不动 —— 它们本来
    就等着领作业，顺手清零等于让一台下到一半的机器从头发一遍。

    这个函数被"在线改正文"与"zip 打密码/改密码"共用：两处各写一遍的话，
    迟早有一处忘了清零偏移，而那种坏文件要到选手解压时才会被发现。
    """
    requeued = 0
    tasks: Dict[int, DeployTask] = {}
    for target, task in session.execute(
        select(DeployTarget, DeployTask)
        .join(DeployTask, DeployTarget.task_id == DeployTask.id)
        .where(
            DeployTask.asset_id == asset_id,
            DeployTarget.status == DeployStatus.DONE,
        )
    ).all():
        target.status = DeployStatus.PENDING
        target.bytes_done = 0
        target.updated_at = utcnow()
        requeued += 1
        tasks[int(task.id)] = task

    # 任务状态是目标状态的聚合缓存（`_derive_task_status`），读的时候会重算，
    # 但**写**的路径上有两处按它判断：取消接口会看它是不是 done。留在 done
    # 会让"刚被重排的任务"报"任务已完成，无法取消"。
    for task in tasks.values():
        if task.status != DeployStatus.CANCELLED:
            task.status = DeployStatus.PENDING
    return requeued


@router.get(
    "/contests/{contest_id}/assets/{asset_id}/text", response_model=AssetTextOut
)
def get_asset_text(
    contest_id: int,
    asset_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetTextOut:
    """读出一个纯文本资产的正文，供界面在线上编辑。

    列表页那个 ``AssetOut.editable`` 只看元数据（不读盘）。这里是**真判**：
    扩展名、体积、以及最要紧的那一条 —— 字节必须能按 UTF-8 解出来。

    解不出来一律 400 而不是"尽力显示"：一份乱码在对话框里看起来也有内容，
    教师在上面改两笔再保存，就等于用半截正确的字节覆盖掉原来那份文件，
    而原始字节是拿不回来的。
    """
    with ctx.db.session() as session:
        asset = _contest_asset(session, contest_id, asset_id)
        return AssetTextOut(
            filename=asset.filename,
            content=_read_editable_text(ctx, asset),
        )


@router.put(
    "/contests/{contest_id}/assets/{asset_id}/text", response_model=AssetTextSavedOut
)
def save_asset_text(
    contest_id: int,
    asset_id: int,
    payload: AssetTextEditIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetTextSavedOut:
    """用新正文换掉一个纯文本资产的内容，**同一个 asset id**。

    资产是内容寻址的（一行只有 ``sha256`` + ``size``），所以"改内容"就是换掉
    这两列，指向它的下发任务与目标一行都不用动。

    但只换这两列是不够的：``services/deploy.py`` 的 tick 按 ``pending/ready``
    取作业、并按**当前**的 ``asset.sha256`` 组装 job —— 已经 ``done`` 的目标
    不会再被下发。不重排的话，选手页显示的是新正文、机器上躺着的还是旧文件，
    而界面上一切正常，没人会发现。重排时**必须把续传偏移一起清零**：
    拿旧文件的偏移去续新文件，拼出来的是一份永远校验不过的文件。

    **审计**：这条路径改的是已经发出去的东西，事后必须查得到 —— 哪个场次、
    哪个文件、从哪一版改到哪一版、影响了多少台。所以每次都要写一条 info 事件。
    """
    with ctx.db.session() as session:
        asset = _contest_asset(session, contest_id, asset_id)
        # 先确认这一份**本来**就能改（同样过扩展名/体积/UTF-8 三道），再动手。
        # 反过来先写后验的话，一份不该被碰的资产会被改掉一半才发现不对。
        _read_editable_text(ctx, asset)

        old_sha = asset.sha256
        content = _normalize_text_content(payload.content)
        try:
            sha256, size = ctx.blobs.put_bytes(content.encode("utf-8"))
        except BlobTooLarge as exc:  # pragma: no cover - 上限由 schema 兜着
            raise HTTPException(status_code=413, detail=str(exc))

        asset.sha256 = sha256
        asset.size = size

        requeued = _requeue_done_targets(session, asset_id)

        session.add(
            EventLog(
                level="info",
                category="asset_text_edited",
                contest_id=contest_id,
                message="在线修改了场次 %d 的「%s」：正文 sha %s → %s，重新排队 %d 台机器"
                % (contest_id, asset.filename, old_sha[:8], sha256[:8], requeued),
            )
        )
        session.flush()

        pending = _pending_targets(session, asset_id)
        log.info(
            "在线改正文：场次 %d 的「%s」sha %s → %s，重排 %d 台",
            contest_id, asset.filename, old_sha[:8], sha256[:8], requeued,
        )
        return AssetTextSavedOut(asset=_asset_out(asset, pending), requeued=requeued)


# --------------------------------------------------------------------------- #
# zip 打密码 / 改密码 / 生成随机密码（InfoZIP 传统加密）
#
# 为什么是 ZipCrypto 而不是 AES-256：学生机器上是 Archive Manager
# （GNOME file-roller），AES-256 的 zip 它打不开（除非另外装了 p7zip），
# 传统加密的 zip 会弹框要密码。**它是弱加密** —— 已知明文攻击可破，
# 测试数据又几乎全是已知结构。所以这里的定位是"挡得住随手翻看，挡不住有心人"，
# 页面上与错误文案里都不许写成"安全加密"。
# --------------------------------------------------------------------------- #

#: 密码文件的固定名字。**就叫 password.txt**（用户拍板的写法），不追加 zip 名字 ——
#: 一场考试可能有好几个 zip，多出来的 `题面.password.txt` 反而更难念给学生听。
PASSWORD_FILENAME = "password.txt"


def _is_password_file_name(filename: Optional[str]) -> bool:
    """这个名字是不是系统那份密码文件（大小写不敏感，只比基名）。

    只比基名是因为上传时教师可能写成 ``dir/password.txt`` —— 那仍然是同一份
    能自己滚下去的文件。
    """
    base = Path(filename or "").name
    return base.lower() == PASSWORD_FILENAME.lower()


def _reject_password_file(asset: Asset) -> None:
    """给已有资产打 zip 之前的那道闸门。"""
    if _is_password_file_name(asset.filename):
        raise ApiError(400, "asset_is_password_file", ERROR_CODES["asset_is_password_file"])


def _reject_password_file_name(filename: Optional[str]) -> None:
    """上传即打包那条路上的同一道闸门（那时还没有资产行）。"""
    if _is_password_file_name(filename):
        raise ApiError(400, "asset_is_password_file", ERROR_CODES["asset_is_password_file"])

#: 密码文件按哪个"用途"落地。它是一份给选手读的说明，所以走「须知」。
#: 这个字段在服务端只影响列表里显示的中文名，不影响落点（落点由下发任务的
#: `dest_dir` 决定）。
PASSWORD_ASSET_KIND = "须知"


def _password_created_at() -> str:
    """password.txt 里的"生成时间"（服务端本地时间，写给人看）。

    抽成函数是为了测试能钉住它：正文里带时间，而"同名同内容就复用"要求同一时刻
    的两次调用得到**逐字节一致**的正文 —— 测试不该靠"跑得快"去赌没跨过分钟边界。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _password_file_text(zip_filename: str, password: str, created_at: str) -> str:
    """password.txt 的正文，自解释到"捡到这张纸也知道它是哪来的"。

    三行都有用：哪份包的密码（一场考试可能有多个 zip）、密码本身、什么时候生成的
    （事后核对"这份是不是我最后改的那一版"）。
    """
    return (
        "文件：%s\n"
        "密码：%s\n"
        "生成时间：%s（服务端时间）\n"
        % (zip_filename, password, created_at)
    )


def _open_asset_blob(ctx: AppContext, asset: Asset):
    """打开资产内容；读不到就是 404（与在线改正文那条路同一个说法）。"""
    try:
        return ctx.blobs.open(asset.sha256)
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail="「%s」的内容已经不在服务端存储里了（%s）" % (asset.filename, exc),
        )


def _probe_asset_zip_state(ctx: AppContext, asset: Asset) -> Optional[bool]:
    """``True`` 已加密 / ``False`` 明文 zip / ``None`` 不是 zip。

    只读 zip 的目录与标志位（见 ``zipcrypto.probe_encrypted``），**不解压、
    不解密任何成员**：教师点一下按钮，不该把几百 MB 的题面从磁盘上拖一遍。
    """
    with _open_asset_blob(ctx, asset) as handle:
        try:
            return zipcrypto.probe_encrypted(handle)
        except zipcrypto.NotAZipError:
            return None


def _probe_asset_zip(ctx: AppContext, asset: Asset) -> bool:
    """GET 的判据：不是 zip 就 400 ``asset_not_zip``。

    回 400 而不是 ``encrypted=false``：这一条 GET 回答的是"包里有没有密码"，
    对一份 pdf 它没有答案。界面拿到 ``asset_not_zip`` 会把动作切换成「打包成
    zip」（POST 那条路对非 zip 是打包，不是拒绝）—— 它由**内容**判定，
    比行上的扩展名可靠。
    """
    encrypted = _probe_asset_zip_state(ctx, asset)
    if encrypted is None:
        raise ApiError(
            400,
            "asset_not_zip",
            "「%s」不是能识别的 zip 压缩包，没有密码状态可读" % asset.filename,
        )
    return encrypted


def _rewrite_asset_zip(
    ctx: AppContext,
    asset: Asset,
    *,
    new_password: str,
    old_password: Optional[str],
) -> Tuple[str, int]:
    """把资产里的 zip 用新密码重新打包，返回 ``(sha256, size)``。

    重新打包写进一个临时文件，全部成功之后才交给内容寻址存储 —— 任何一步失败
    （旧密码不对、不是 zip、超过体积上限）都不会碰到原来那份 blob。"旧密码错时
    资产一个字节都没动"这条判据靠的就是这个顺序，不是靠调用方自觉。
    """
    new_pwd = new_password.encode("utf-8")
    old_pwd = old_password.encode("utf-8") if old_password else None
    with _open_asset_blob(ctx, asset) as source:
        with tempfile.TemporaryFile() as buffer:
            try:
                zipcrypto.rewrite_zip(
                    source, buffer, new_password=new_pwd, old_password=old_pwd
                )
            except zipcrypto.PasswordRequiredError:
                # 这个分支只有"先探测时以为没加密、真读的时候发现成员是加密的"
                # 才走得到 —— 预先那条检查（见 set_asset_zip_password）会先给出一句
                # 更有用的提示（旧密码在 password.txt 里）。文案刻意与那句不同：
                # 两句一样的话，测试就分不清"预检查有没有生效"。
                raise ApiError(
                    400,
                    "zip_password_required",
                    "「%s」里还有加密成员，但没给能解开它的旧密码" % asset.filename,
                )
            except zipcrypto.BadPasswordError:
                raise ApiError(
                    400,
                    "zip_password_wrong",
                    "旧密码不对，「%s」没有被改动" % asset.filename,
                )
            except zipcrypto.NotAZipError:
                raise ApiError(
                    400,
                    "asset_not_zip",
                    "「%s」不是能识别的 zip 压缩包" % asset.filename,
                )
            except zipcrypto.UnsupportedCompressionError as exc:
                raise ApiError(
                    400,
                    "zip_password_failed",
                    "「%s」里有不能重新打包的成员（%s）" % (asset.filename, exc),
                )
            except zipcrypto.ZipCryptoError as exc:
                raise ApiError(
                    400,
                    "zip_password_failed",
                    "重新打包「%s」失败（%s）" % (asset.filename, exc),
                )
            buffer.seek(0)
            try:
                return ctx.blobs.put_stream(
                    buffer, max_bytes=ctx.settings.max_asset_size
                )
            except BlobTooLarge as exc:  # pragma: no cover - 由 2 GB 上限兜着
                raise HTTPException(status_code=413, detail=str(exc))


def _pack_asset(
    ctx: AppContext,
    asset: Asset,
    *,
    new_password: str,
) -> Tuple[str, int]:
    """把资产里的原始字节包成一个单成员 zip，返回 ``(sha256, size)``。

    成员名就是资产现在的文件名（``题面.pdf``）：打包只改变"这是一个 zip"这件事，
    不改变包里那份文件叫什么。

    与 ``_rewrite_asset_zip`` 同一条纪律：先在临时文件里做完，全部成功才交给内容
    寻址存储。中途失败（超过上限、源读不动）时原来那份 blob 一个字节都没动。
    """
    with _open_asset_blob(ctx, asset) as source:
        with tempfile.TemporaryFile() as buffer:
            try:
                zipcrypto.pack_member(
                    source,
                    buffer,
                    member_name=asset.filename,
                    password=new_password.encode("utf-8"),
                )
            except zipcrypto.ZipCryptoError as exc:
                raise ApiError(
                    400,
                    "zip_package_failed",
                    "打包「%s」失败（%s）" % (asset.filename, exc),
                )
            buffer.seek(0)
            try:
                return ctx.blobs.put_stream(
                    buffer, max_bytes=ctx.settings.max_asset_size
                )
            except BlobTooLarge as exc:  # pragma: no cover - 由 2 GB 上限兜着
                raise HTTPException(status_code=413, detail=str(exc))


def _upsert_password_asset(
    ctx: AppContext,
    session: Session,
    contest_id: int,
    *,
    zip_asset: Asset,
    password: str,
) -> Tuple[AssetOut, int]:
    """写/更新那份 ``password.txt``，返回 ``(资产, 它自己被重排的台数)``。

    同名但内容不同时**选"更新"而不是"新建一条"**，理由是这个文件名是**单件**
    语义：教室里要做的事永远是"把**当前**这份密码发下去"。走 ``_create_asset``
    的"内容不同就新建"那条路的话，每改一次密码就会多出一条 password.txt，
    列表上很快躺着三五条同名文件，而教师没有任何依据判断哪一条是最新的 ——
    发错的那一次，学生打开包时看到的是"密码不对"，现场没人能立刻反应过来。

    "更新"走的是与「在线改正文」**同一套**语义：同一个 asset id 换
    ``sha256``/``size``，并把已经下发完成（``done``）的目标重排回 ``pending``。
    这样"以前收过这份密码的机器"会在下一轮 tick 自动拿到新的那一份，而不是
    守着一份过期密码。同名同内容（sha256 相同）时直接复用，一个字节都不写。

    新造那一条时走 ``_create_asset``（「新建文本资产」的公共入口），
    不另发明一套下发机制。
    """
    content = _normalize_text_content(
        _password_file_text(zip_asset.filename, password, _password_created_at())
    )
    sha256, size = ctx.blobs.put_bytes(content.encode("utf-8"))

    existing = (
        session.execute(
            select(Asset)
            .where(Asset.contest_id == contest_id, Asset.filename == PASSWORD_FILENAME)
            # 去重是按「名字 + 内容」做的，手工上传过同名文件才可能出现不止一条。
            # 取 id 最大的那条当"当前这份"，至少是确定的、可解释的。
            .order_by(Asset.id.desc())
        )
        .scalars()
        .first()
    )

    if existing is None:
        return (
            _create_asset(
                ctx,
                contest_id,
                sha256=sha256,
                size=size,
                filename=PASSWORD_FILENAME,
                kind=PASSWORD_ASSET_KIND,
                session=session,
            ),
            0,
        )

    if existing.sha256 == sha256:
        return _asset_out(existing, _pending_targets(session, existing.id)), 0

    existing.sha256 = sha256
    existing.size = size
    requeued = _requeue_done_targets(session, existing.id)
    session.flush()
    return _asset_out(existing, _pending_targets(session, existing.id)), requeued


@router.get(
    "/contests/{contest_id}/assets/{asset_id}/zip-password",
    response_model=AssetZipPasswordOut,
)
def get_asset_zip_password(
    contest_id: int,
    asset_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetZipPasswordOut:
    """这个 zip 现在有没有密码。

    判据是标志位（读 zip 目录，不读成员正文）。不是 zip 就 400 ``asset_not_zip``：
    这一条 GET 回答的是"包里有没有密码"，对一份 pdf 它没有答案。界面拿到这个
    错误码会把动作切成「打包成 zip」（POST 对非 zip 是打包，不是拒绝）——
    「是不是 zip」由**内容**判定，比列表行上的扩展名可靠。
    """
    with ctx.db.session() as session:
        asset = _contest_asset(session, contest_id, asset_id)
        return AssetZipPasswordOut(
            encrypted=_probe_asset_zip(ctx, asset), filename=asset.filename
        )


@router.post(
    "/contests/{contest_id}/assets/{asset_id}/zip-password",
    response_model=AssetZipPasswordSavedOut,
)
def set_asset_zip_password(
    contest_id: int,
    asset_id: int,
    payload: AssetZipPasswordIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetZipPasswordSavedOut:
    """给 zip 打密码 / 改密码；不是 zip 时先把它打包成 zip，并同步写 ``password.txt``。

    **这个密码是弱加密**：InfoZIP 传统加密（ZipCrypto）只挡得住随手翻看，
    挡不住有心人 —— 选它是因为学生机上的 Archive Manager 只认这一种
    （AES-256 的 zip 它打不开）。界面文案与这里的口径必须一致，不许写"安全加密"。

    **服务端不另外保存密码明文**：密码只写进 ``password.txt`` 那份文本资产，
    没有新增的密码列/表。因此"密码丢了"的答案就是"去「文件下发」页读
    password.txt" —— 它是普通文本资产，教师随时能读回来、能看到、能再发一遍。
    审计事件里也刻意不写密码，否则等于多存了一份明文。

    改内容的语义与「在线改正文」完全一致（同一套 ``_requeue_done_targets``）：
    同一个 asset id 换 ``sha256``/``size``、把已经 ``done`` 的目标重排回
    ``pending`` 并清零续传偏移、写一条审计事件。区别只是"新内容"不是一段文本，
    而是"用新密码重新打包的 zip"。资产本来不是 zip 时，除了重新打包，文件名还会
    换成 ``<原基名>.zip``（zip 里的成员名仍是原文件名）—— 这是**同一个资产**的
    就地替换，不是新建一条；回执里的 ``packaged`` 说明这次走的是哪条路。
    """
    with ctx.db.session() as session:
        asset = _contest_asset(session, contest_id, asset_id)
        # 闸门只装在"要打包"的这条路上：`password.txt` 被打成 zip 之后会再生成一份
        # 新的 `password.txt`，能自己滚下去。**读它的正文不受影响**（教师要靠读正文
        # 把密码抄给学生），所以这道闸门不能放进两条路共用的 helper。
        _reject_password_file(asset)
        state = _probe_asset_zip_state(ctx, asset)

        if payload.generate:
            # 服务端生成：字符集去掉易混字符，用 secrets（见 zipcrypto）。
            password = zipcrypto.generate_password()
        else:
            password = payload.password or ""
        if not password:
            raise ApiError(
                400,
                "zip_password_missing",
                "请给一个新密码，或者让服务端生成一个",
            )

        old_password = payload.old_password
        if state is True and not old_password:
            raise ApiError(
                400,
                "zip_password_required",
                "「%s」已经有密码了，改密码要先给旧密码（旧密码在之前那份 password.txt 里）"
                % asset.filename,
            )
        if state is not True:
            # 明文 zip 与"根本不是 zip"都不需要旧密码。教师顺手填了一个也只是
            # 习惯 —— 静默忽略比报错更符合意图，但绝不能让它参与任何判断。
            old_password = None

        old_sha = asset.sha256
        old_filename = asset.filename
        if state is None:
            # 不是 zip：就地打包成 zip。成员名仍是原文件名（不然包里那份 pdf
            # 会改名叫 .zip），资产文件名换成 <原基名>.zip —— 名字变了，教师
            # 在列表里一眼能看出这是一个包。
            sha256, size = _pack_asset(ctx, asset, new_password=password)
            asset.filename = _packaged_filename(old_filename)
            packaged = True
        else:
            sha256, size = _rewrite_asset_zip(
                ctx, asset, new_password=password, old_password=old_password
            )
            packaged = False
        asset.sha256 = sha256
        asset.size = size

        requeued = _requeue_done_targets(session, asset.id)
        password_asset, password_requeued = _upsert_password_asset(
            ctx, session, contest_id, zip_asset=asset, password=password
        )

        if packaged:
            message = (
                "给场次 %d 的「%s」打包成 zip（成员名 %s，文件名改为「%s」，"
                "sha %s → %s），重新排队 %d 台机器；同步写好密码文件 password.txt"
                "（资产 #%d，重排 %d 台）"
                % (
                    contest_id,
                    old_filename,
                    old_filename,
                    asset.filename,
                    old_sha[:8],
                    sha256[:8],
                    requeued,
                    password_asset.id,
                    password_requeued,
                )
            )
        else:
            message = (
                "给场次 %d 的「%s」重新打包了 zip（sha %s → %s），"
                "重新排队 %d 台机器；同步写好密码文件 password.txt"
                "（资产 #%d，重排 %d 台）"
                % (
                    contest_id,
                    asset.filename,
                    old_sha[:8],
                    sha256[:8],
                    requeued,
                    password_asset.id,
                    password_requeued,
                )
            )
        session.add(
            EventLog(
                level="info",
                category="asset_zip_password",
                contest_id=contest_id,
                message=message,
            )
        )
        session.flush()

        pending = _pending_targets(session, asset.id)
        log.info(
            "zip %s：场次 %d 的「%s」sha %s → %s（重排 %d 台），password.txt=#%d",
            "打包" if packaged else "打密码",
            contest_id,
            asset.filename,
            old_sha[:8],
            sha256[:8],
            requeued,
            password_asset.id,
        )
        return AssetZipPasswordSavedOut(
            asset=_asset_out(asset, pending),
            password=password,
            password_asset=password_asset,
            requeued=requeued,
            packaged=packaged,
        )


@router.patch("/assets/{asset_id}", response_model=AssetOut)
def rename_asset(
    asset_id: int,
    payload: AssetRenameIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> AssetOut:
    """给资产改个显示名。

    上传时文件名打错了（`题面(1).pdf`、`样例 最终版.zip`）在考场上是常态，
    而这个名字会变成学生桌面上的文件名 —— 改它比重新上传省事。

    **内容不改**：资产是按 sha256 存的内容寻址对象，改名只是换标签，
    已经下发给选手的文件不受影响（它们早就落地了），但**未完成**的下发任务
    会按新名字落地 —— 这一点要说清楚，否则教师会以为改名能修正已经发出去的文件。
    """
    name = _validate_asset_filename(payload.filename)

    with ctx.db.session() as session:
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="资产不存在")
        old = asset.filename
        asset.filename = name
        # 还没落地的下发目标会按**新名字**落地，已经落地的文件不受影响。
        # 这个数字要跟着响应回去，界面才能说清"这次改名会对谁生效" ——
        # 算了不报出去，等于教师只能靠猜。
        pending = session.execute(
            select(func.count(DeployTarget.id))
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(
                DeployTask.asset_id == asset_id,
                DeployTarget.status.in_(ACTIVE_DEPLOY_STATUSES),
            )
        ).scalar_one()
        session.flush()
        log.info("资产改名：%s -> %s（还有 %d 个目标未完成）", old, name, pending)
        return _asset_out(asset, pending)


@router.delete("/assets/{asset_id}", response_model=SimpleAck)
def delete_asset(
    asset_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删掉一个下发用的资产（题面、样例、测试点包…）。

    会连带删掉引用它的下发任务与逐选手进度（外键级联）。**已经落到选手机器上的
    文件不会被撤回** —— 客户端那边的文件不归服务端管，删了这个动作只影响
    "以后还能不能发"。界面上要把这句说清楚，否则教师会以为删了就能收回题面。

    内容本身（blob）是内容寻址的：如果还有别的资产或代码文件引用同一份内容，
    它不会被删。
    """
    with ctx.db.session() as session:
        asset = session.get(Asset, asset_id)
        if asset is None:
            return SimpleAck(ok=True, detail="资产不存在")
        filename = asset.filename
        tasks = session.execute(
            select(func.count(DeployTask.id)).where(DeployTask.asset_id == asset_id)
        ).scalar_one()
        session.delete(asset)

    return SimpleAck(
        ok=True,
        detail="已删除资源「%s」%s。已经落到选手机器上的文件不会撤回"
        % (filename, "（含 %d 个下发任务）" % tasks if tasks else ""),
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


@router.get("/contests/{contest_id}/deploys", response_model=Page[DeployTaskOut])
def list_deploys(
    contest_id: int,
    page: PageParams = Depends(),
    include_targets: bool = False,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[DeployTaskOut]:
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
        return slice_page(out, page)


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


@router.delete("/deploys/{task_id}", response_model=SimpleAck)
def delete_deploy(
    task_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删掉一条下发记录。

    **不会撤回已经落地的文件** —— 客户端那边的文件不归服务端管。
    它做的是"把这条任务从列表里去掉"，包括还没完成的那些（等于放弃它）：
    删掉之后 Agent 下一轮 tick 不会再领到这个作业。

    还没完成的作业被删时要提醒一句：教师很可能以为"删除 = 取消下发"，
    而实际上那台机器上的半份文件会留在硬盘上（``.part`` 分片）。
    真要停止下发应该用「取消」，它会保留记录、只是不再派发。
    """
    with ctx.db.session() as session:
        task = session.get(DeployTask, task_id)
        if task is None:
            return SimpleAck(ok=True, detail="下发记录不存在")
        asset = session.get(Asset, task.asset_id)
        filename = asset.filename if asset else "?"
        counts: Dict[str, int] = {}
        for target in session.execute(
            select(DeployTarget).where(DeployTarget.task_id == task_id)
        ).scalars():
            counts[target.status] = counts.get(target.status, 0) + 1
        unfinished = counts.get(DeployStatus.PENDING, 0) + counts.get(DeployStatus.READY, 0)
        contest_id = task.contest_id
        session.delete(task)
        session.add(
            EventLog(
                level="info",
                category="deploy_delete",
                contest_id=contest_id,
                message="删除下发记录「%s」（未完成 %d 台）" % (filename, unfinished),
            )
        )

    detail = "已删除下发记录「%s」" % filename
    if unfinished:
        detail += "。有 %d 台还没下完，它们不会再收到这个作业，但已经落地的部分文件会留在机器上" % unfinished
    return SimpleAck(ok=True, detail=detail)


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


@router.get("/contests/{contest_id}/problems", response_model=Page[ProblemOut])
def list_problems(
    contest_id: int,
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[ProblemOut]:
    with ctx.db.session() as session:
        rows = list(
            session.execute(
                select(Problem)
                .where(Problem.contest_id == contest_id)
                .order_by(Problem.order_index, Problem.ident)
            ).scalars()
        )
        return slice_page([_problem_out(row, ctx.settings) for row in rows], page)


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
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入题目标识"),
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
        require_confirm(row.ident, confirm, "题目标识")
        ident = row.ident
        session.delete(row)
    return SimpleAck(ok=True, detail="已删除题目 %s（已有成绩记录保留）" % ident)


@router.post("/contests/{contest_id}/problems/clear", response_model=SimpleAck)
def clear_problems(
    contest_id: int,
    payload: ConfirmIn,
    idents: Optional[str] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """批量删除题目登记。

    ``idents`` 是逗号分隔的题目标识；**留空表示清空整份清单**。

    和单条删除一样，**不动已有成绩记录** —— 它们会以「未登记」继续出现在矩阵里。
    这一点必须说清楚：教师看到"清空清单"很容易以为成绩也一起没了，
    于是不敢动，或者反过来以为清干净了结果成绩还在。

    确认内容是**场次 slug**：即便只删其中几道题，范围也还是"这场比赛的题目清单"。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")
        require_confirm(contest.slug, payload.confirm, "场次标识")

        stmt = select(Problem).where(Problem.contest_id == contest_id)
        wanted: Optional[List[str]] = None
        if idents is not None and idents.strip():
            wanted = [item.strip() for item in idents.split(",") if item.strip()]
            stmt = stmt.where(Problem.ident.in_(wanted))

        rows = list(session.execute(stmt).scalars())
        removed = [row.ident for row in rows]
        for row in rows:
            session.delete(row)

        if removed:
            session.add(
                EventLog(
                    level="warning",
                    category="problem_clear",
                    contest_id=contest_id,
                    message="删除题目登记 %d 条：%s"
                    % (len(removed), "、".join(removed[:20])),
                )
            )

    detail = "已删除 %d 道题的登记" % len(removed)
    if removed:
        detail += "（已有成绩记录保留，会以「未登记」显示在矩阵里）"
    return SimpleAck(ok=True, detail=detail)


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


@router.get("/contests/{contest_id}/judge/runs", response_model=Page[JudgeRunOut])
def list_judge_runs(
    contest_id: int,
    page: PageParams = Depends(),
    parse_status: Optional[str] = None,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[JudgeRunOut]:
    with ctx.db.session() as session:
        filters = [JudgeRun.contest_id == contest_id]
        if parse_status:
            filters.append(JudgeRun.parse_status == parse_status)

        total = session.execute(
            select(func.count(JudgeRun.id)).where(*filters)
        ).scalar_one()
        stmt = (
            select(JudgeRun, Player.player_no)
            .join(Player, JudgeRun.player_id == Player.id)
            .where(*filters)
            .order_by(Player.player_no, JudgeRun.problem)
            .limit(page.limit)
            .offset(page.offset)
        )

        items = [
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
        return page_of(items, total, page)


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
    confirm: str = Query(..., min_length=1, max_length=200, description="原样输入考号"),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """抹掉一条成绩记录（某个选手的某道题）。

    标识用**考号**：教师是在成绩矩阵上指着那一行操作的，矩阵行首显示的就是考号；
    题目标识已经在这个请求的 `problem` 参数里了，再要求拼一遍只会让人打错。
    """
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
        player = session.get(Player, player_id)
        require_confirm(player.player_no if player else None, confirm, "考号")
        session.delete(run)
    return SimpleAck(ok=True, detail="已清除")


@router.post("/contests/{contest_id}/judge/runs/clear", response_model=SimpleAck)
def clear_judge_runs(
    contest_id: int,
    payload: JudgeRunClearIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """清空评测成绩。

    ``keep_manual`` 默认 **True**：**教师手工录入的成绩不会被清掉**。

    这条默认值是有意的：手工录入意味着评测器给不出结果、教师看过代码之后
    亲自判的分。它是全场里最贵的那部分数据，而"重扫一遍"这种常见操作
    恰恰最容易顺手把它清掉。要连手工分一起清，得显式传 ``false``。

    确认内容是**场次 slug**（可选的 ``player_id`` 只是把范围缩小到一个人，
    范围本身仍然属于这场比赛）。
    """
    player_id = payload.player_id
    keep_manual = payload.keep_manual
    with ctx.db.session() as session:
        contest = session.get(Contest, contest_id)
        if contest is None:
            raise HTTPException(status_code=404, detail="场次不存在")
        require_confirm(contest.slug, payload.confirm, "场次标识")

        stmt = select(JudgeRun).where(JudgeRun.contest_id == contest_id)
        if player_id is not None:
            stmt = stmt.where(JudgeRun.player_id == player_id)

        rows = list(session.execute(stmt).scalars())
        removed = 0
        kept = 0
        for run in rows:
            if keep_manual and run.parse_status == "manual":
                kept += 1
                continue
            session.delete(run)
            removed += 1

        session.add(
            EventLog(
                level="warning",
                category="scores_clear",
                contest_id=contest_id,
                message="清空成绩：删除 %d 条，保留手工录入 %d 条" % (removed, kept),
            )
        )

    detail = "已清除 %d 条成绩记录" % removed
    if kept:
        detail += "；保留 %d 条手工录入（要一起清就关掉「保留手工分」）" % kept
    return SimpleAck(ok=True, detail=detail)


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
        # 随包附带的密钥一次查完，而不是每行一次 —— 版本历史可能很长，而这一页
        # 每次刷新都要走这里。缺行（密钥被删）时按 id 回落成 ``#id``。
        bootstrap_keys = _bootstrap_keys_by_id(session, rows)
        releases = [_release_out(row, bootstrap_keys) for row in rows]
        active = next((r for r in releases if r.rolled_out and not r.yanked), None)

    return UpgradeStatusOut(
        signing_available=ctx.signing_key is not None,
        key_id=ctx.signing_key.key_id if ctx.signing_key else None,
        error=ctx.signing_key_error,
        active_release=active,
        releases=releases,
    )


def _bootstrap_keys_by_id(
    session: Session, rows: "list[AgentRelease]"
) -> Dict[int, BootstrapKey]:
    """把版本记录提到的那几把密钥一次取出来。

    刻意不建 ORM 关系：``BootstrapKey`` 那边没有"附带它的版本"这个概念
    （一把密钥可能先于版本存在，也可能在版本删掉之后还留着），
    而这里要的只是显示用的两三个字段。列表里没有一行带密钥时**一次库都不查**。
    """
    key_ids = {row.bootstrap_key_id for row in rows if row.bootstrap_key_id}
    if not key_ids:
        return {}
    found = session.execute(
        select(BootstrapKey).where(BootstrapKey.id.in_(sorted(key_ids)))
    ).scalars()
    return {key.id: key for key in found}


def _release_out(
    row: AgentRelease, bootstrap_keys: Optional[Dict[int, BootstrapKey]] = None
) -> ReleaseOut:
    """把一行发布记录翻译成回执。

    ``bootstrap_keys`` 是 ``{id: BootstrapKey}``。**列表接口必须传**（它一次把
    所有提到过的密钥查出来，避免一行一次查询）；单个版本的动作接口走
    :func:`_release_with_key`，那里只查当前这一行提到的那一把。
    """
    key_id = row.bootstrap_key_id
    label: Optional[str] = None
    revoked = False
    if key_id is not None:
        key = None if bootstrap_keys is None else bootstrap_keys.get(int(key_id))
        if key is not None:
            label = key.label or "#%d" % key.id
            revoked = key.revoked_at is not None
        elif bootstrap_keys is not None:
            # 密钥记录被删了（允许删"签错了、没被用过"的那种），版本记录还在。
            # 报 ``#id`` 而不是空字符串：界面上要能回答"这个包附带过密钥吗"。
            label = "#%d" % key_id
    # 策略按同一份 helper 展开：界面上要显示"这一版发出去的是什么"，而库里
    # 只存教师改过的那些键（``config_policy`` 因此被补成完整映射）。
    #
    # ``has_bundled_key`` 用 ``bootstrap_key_id``：迁移之前建的记录没有策略列，
    # 但"这个包夹带过密钥"记在这一列上 —— 不给它，老记录会显示成"不覆盖"。
    policy = install_policy.build_install_policy(
        bootstrap_key_policy=row.bootstrap_key_policy,
        config_policy=install_policy.load_config_policy(row.config_policy_json),
        upgrade_mode=row.upgrade_mode,
        has_bundled_key=row.bootstrap_key_id is not None,
    )
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
        bootstrap_key_id=key_id,
        bootstrap_key_label=label,
        bootstrap_key_revoked=revoked,
        bootstrap_key_policy=policy["bootstrap_key_policy"],
        config_policy=policy["config_policy"],
        upgrade_mode=policy["upgrade_mode"],
    )


def _require_signing_key(ctx: AppContext) -> None:
    """没有私钥就别往下走 —— 打出一个未签名的包是最坏的失败方式：

    Agent 会拒收它，于是"界面上发布成功、所有机器都升不上去"。宁可在这里
    直接拦住并说清怎么配。
    """
    if ctx.signing_key is None:
        raise ApiError(
            503,
            "release_not_signed",
            "未配置发布签名私钥，无法签发升级包：%s"
            % (ctx.signing_key_error or "请设置 SYNCOJ_RELEASE_KEY"),
        )


def _store_release(
    ctx: AppContext,
    *,
    stream,
    version: str,
    channel: str,
    notes: Optional[str],
    filename: str,
    admin: AdminIdentity,
    source: str,
    bootstrap_key_id: Optional[int] = None,
    policy: Optional[install_policy.ResolvedPolicy] = None,
) -> ReleaseOut:
    """把一份升级包**收进库并签发**，返回发布记录。

    抽出来是因为现在有两个入口（上传一个包、从源码构建一个包），而这两条路
    在"签名对象是什么、同名版本怎么处理、留哪条审计"上**必须完全一致**：
    任何一处不同都会让两条路产出行为不同的发布，而现场只看得出来"有时能升级
    有时不能"。真正不同的只有 ``source``（审计里那份包是打哪儿来的）。

    ``bootstrap_key_id`` 只在"构建时附带密钥"那条路上有值（上传的包是别人打好的，
    服务端不知道里面有没有密钥，所以上传一律不写这一列）。**这里只收 id，
    收不到明文** —— 调用方在构建前一刻现场签发的那把密钥，明文只在它手里待了一次。

    ``policy`` 同理只在构建那条路上有值：服务端不知道上传进来的包裹里写了什么策略，
    所以上传一律只留默认值（不改配置、密钥不动、按默认模式升级）。**不猜** ——
    猜错的表现是机器按一套没人指定过的策略去改 ``agent.ini``。
    """
    _require_signing_key(ctx)
    try:
        sha256, size = ctx.blobs.put_stream(
            stream, max_bytes=ctx.settings.max_release_size
        )
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
            # 不给值时**保持原样**：重打一个不带密钥的同名版本，不该悄悄把
            # "上一版附带的是哪把钥匙"这条记录抹掉 —— 那把钥匙还在外面流通，
            # 界面必须还看得见它、还能吊销它。
            if bootstrap_key_id is not None:
                existing.bootstrap_key_id = bootstrap_key_id
            # 策略同理：上传那条路没有策略（``policy is None``），不该把上一版
            # 构建出来的策略抹成默认值 —— 那个包还在外面，机器会照着它行动。
            if policy is not None:
                existing.bootstrap_key_policy = policy.bootstrap_key_policy
                existing.config_policy_json = install_policy.dump_config_policy(
                    policy.config_policy
                )
                existing.upgrade_mode = policy.upgrade_mode
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
                bootstrap_key_id=bootstrap_key_id,
                bootstrap_key_policy=policy.bootstrap_key_policy if policy else None,
                config_policy_json=(
                    install_policy.dump_config_policy(policy.config_policy) if policy else None
                ),
                upgrade_mode=policy.upgrade_mode if policy else None,
            )
            session.add(row)
            session.flush()

        session.add(
            EventLog(
                level="info",
                category="release_%s" % source,
                message="%s Agent 版本 %s（%s，%d 字节，%s）%s"
                % (
                    _SOURCE_LABELS[source],
                    version,
                    filename,
                    size,
                    sha256[:12],
                    # 审计里只写"附带了哪一把"的 id —— **明文绝不进这里**：
                    # events 表是教师界面能翻的，写进去等于把密钥重新公开一次。
                    ("，附带统一注册密钥 #%d" % bootstrap_key_id)
                    if bootstrap_key_id is not None
                    else "",
                ),
                meta_json=json.dumps(
                    {
                        "by": admin.username,
                        "source": source,
                        "version": version,
                        "bootstrap_key_id": bootstrap_key_id,
                        # 策略要能事后回答："这批机器当初是被按什么规则升级的"。
                        # 它不含任何秘密，所以直接写进审计，与密钥 id 的待遇不同。
                        "install_policy": (
                            policy.install_policy if policy else None
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        return _release_with_key(session, row)


#: 审计里怎么称呼这个包的来路。用固定词表而不是自由文本，日志才 grep 得动。
_SOURCE_LABELS = {"uploaded": "上传", "built": "构建"}


def _expected_public_key_path(ctx: AppContext) -> Path:
    """"本该在那儿的公钥文件"的路径，只用来写错误消息。

    优先从密钥目录推（那才是真实布局）；退而用私钥文件旁边的位置，好在"私钥
    来自 ``SYNCOJ_RELEASE_KEY`` 指向的别处"时仍然给出一个**说得通**的建议路径。
    绝不去读这个文件 —— 它只是在报错时用来告诉人"该把文件放哪"。
    """
    where = keys.key_dir()
    if where is not None:
        return where / keys.RELEASE_PUBLIC_KEY_NAME
    signing = ctx.settings.release_signing_key
    parent = Path(signing).parent if signing else Path("<密钥目录>")
    return parent / keys.RELEASE_PUBLIC_KEY_NAME


@router.get("/releases/source", response_model=ReleaseSourceOut)
def release_source(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ReleaseSourceOut:
    """本机有没有可构建的 Agent 源码。**永远 200。**

    界面上"发布当前版本"要在**点之前**就知道行不行：没有源码时该显示成一句
    解释（"生产上服务端可能没 checkout，请改用上传"），而不是让人点一下再吃
    一个 500。所以这里把"不可用"当成正常状态返回。
    """
    probe = packaging.probe_source(public_url=_advertised_url(request, ctx))
    return ReleaseSourceOut(
        available=probe.available,
        version=probe.version,
        agent_root=probe.agent_root,
        public_key=probe.public_key,
        public_url=probe.public_url,
        reason=probe.reason,
        config_policy_keys=list(install_policy.CONFIG_POLICY_KEYS),
        # 默认值按"没夹带密钥"算 —— 它只是给界面看的起点（全部"不改"）；
        # 真正要不要覆盖由每一版自己的 bootstrap_key_id 决定。
        config_policy_defaults=install_policy.config_policy_payload({}),
    )


def _advertised_url(request: Request, ctx: AppContext) -> Optional[str]:
    """这台服务端该对考试机说自己是哪个地址。

    顺序由 :func:`discovery.describe_advertised_url` 定：显式配置 →
    "教师浏览器用过的那个非回环 Host" → 从**这次请求的来源**反推本机地址。

    最后那条是给"全新服务端、没人配过地址"准备的：教师是从局域网上点进来的，
    内核告诉他这条路走哪个网卡，那个地址就是考试机该用的地址 —— 于是零配置也
    能打出带正确地址的包。
    """
    peer_ip = request.client.host if request.client else None
    return discovery.describe_advertised_url(ctx, peer_ip)


@router.post("/releases/build", response_model=ReleaseOut)
def build_release(
    payload: ReleaseBuildIn,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> ReleaseOut:
    """从本机仓库里的 ``agent/`` 源码构建一个升级包并签发。

    **构建不等于铺开**：``published_at`` 保持为空，Agent 不会收到任何东西，
    直到显式调 rollout。构建和"推给 50 台机器"是风险等级完全不同的两件事。

    检查的**顺序是有意的**：先答"这台服务器到底能不能发布"（配置问题），再答
    "你这次提交的对不对"（输入问题）。反过来的话，一个只是填错版本号的人会先
    撞上版本不一致，改对了再撞上"没有公钥" —— 两轮才走到真正该修的那一步。
    """
    _require_signing_key(ctx)
    # 有私钥、却没有要内嵌的公钥：服务端能签，机器验不了。这是最难查的一种现场
    # —— 界面显示发布成功、铺开也成功，然后所有机器一动不动。所以在这里拦住，
    # 而不是等构建完、铺开完再让人去猜。
    if keys.release_public_key_path() is None:
        raise ApiError(
            503,
            "release_trust_anchor_missing",
            "本机能签名，但找不到要内嵌进包里的发布公钥（%s）—— 这样打出去的包"
            "机器验不了签名，会静默拒绝升级。" % _expected_public_key_path(ctx),
        )

    version = payload.version.strip()
    if not version:
        raise HTTPException(status_code=400, detail="必须指定版本号")
    try:
        parse_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="版本号不合法: %s" % exc)

    probe = packaging.probe_source(public_url=_advertised_url(request, ctx))
    if not probe.available:
        raise ApiError(503, "release_source_missing", probe.reason or "找不到可构建的源码")
    if probe.version != version:
        raise ApiError(
            409,
            "version_mismatch",
            "源码里的版本是 %s，你提交的是 %s。要么改 agent/syncoj_agent/__init__.py "
            "里的 __version__，要么把版本号填对 —— 两者不一致时，包里那份代码和"
            "发布记录上那个号就对不上了。" % (probe.version, version),
            {"source_version": probe.version, "given": version},
        )

    # 三条安装策略在这里**先校验再动手**：非法就当场 400，而不是打出一个包之后
    # 才在半路发现问题（那时临时文件、可能还有一把刚签发的密钥都已经产生了）。
    # 校验放在版本号之后：先说清"你这个版本号不对"，比先说"upgrade_mode 拼错了"
    # 更贴近提交者眼前那件要改的事。
    try:
        policy = install_policy.resolve_policy(
            include_bootstrap_key=payload.include_bootstrap_key,
            bootstrap_key_policy=payload.bootstrap_key_policy,
            config_policy=payload.config_policy,
            upgrade_mode=payload.upgrade_mode,
        )
    except install_policy.UpgradePolicyError as exc:
        # 400 而不是 422：请求体本身完全合法（字段名、类型都对），不合格的是**取值**
        # —— 与"路径不合法""改不了的正文"同一类。
        raise ApiError(400, "bad_request", exc.detail)

    # 打到一个临时文件，再和"上传"走同一条入库路径。临时目录用 blob 库的
    # ``.tmp``：同一个文件系统，最后那次 rename 才是原子的（跨分区会退化成拷贝）。
    # 它只会被 ``sweep_tmp`` 按 ``*.part`` 清理，我们这个目录不在其列。
    tmp_dir = Path(tempfile.mkdtemp(prefix="build-", dir=str(ctx.blobs.tmp_root)))
    tmp_path = tmp_dir / ("syncoj-agent-%s.tar.gz" % version)
    try:
        # 「附带统一注册密钥」：**在构建这一刻现场签一把新的**。
        #
        # 不能改成"从已签发的密钥里挑一把"：``BootstrapKey`` 只存
        # ``hash_bootstrap_key(key)``，服务端拿不到任何明文可塞 —— 明文只在
        # 签发那一刻出现过一次（UI 原话："库里只存哈希。丢了就重新签发一把"）。
        #
        # 现在这样做反而更好：**每发一个带密钥的版本就对应一把独立的密钥**，
        # 于是可以按版本单独吊销。万一某个包流出去了，吊销那一把就行，别的机器
        # （包括共用机房主密钥的那些）不受影响 —— 比"共用一把主密钥"安全得多。
        #
        # 明文的去向只有一条：``build_agent_bundle`` 的入参 → 包内 ``bootstrap.key``。
        # 它不进数据库（这里只把 ``hash_bootstrap_key(raw)`` 交给 BootstrapKey）、
        # 不进日志（下面所有文案都只带 id）、不进响应（回执里只有 id 和标签）。
        raw_key: Optional[str] = None
        bootstrap_key_id: Optional[int] = None
        if payload.include_bootstrap_key:
            raw_key = new_bootstrap_key(ctx.settings.bootstrap_key_bytes)
            with ctx.db.session() as session:
                key = BootstrapKey(
                    key_hash=hash_bootstrap_key(raw_key),
                    label="随版本 %s 附带" % version,
                    note="构建时现场签发、随包下发，装机即注册。用完请吊销这一把。",
                )
                session.add(key)
                session.flush()
                bootstrap_key_id = key.id
                session.add(
                    EventLog(
                        level="warning",
                        category="bootstrap_key",
                        message="为 Agent 版本 %s 附带签发统一注册密钥 #%d"
                        % (version, bootstrap_key_id),
                        meta_json=json.dumps(
                            {
                                "by": admin.username,
                                "version": version,
                                "bootstrap_key_id": bootstrap_key_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )

        # 把服务端地址一起写进包：装 50 台时这是唯一还要人手输的一项。算不出来
        # （没人从局域网打开过界面、也没配 public_url）时不写，机器那边还有
        # 局域网发现兜着 —— 但**绝不退化成 127.0.0.1**，那会让 50 台机器各自找自己。
        #
        # 三条策略也一起写进包内 ``install_policy.json``：离线装机时机器读的是它，
        # 而不是台账（那时它可能连不上服务端）。传的是同一个 helper 的产物 ——
        # 台账与升级清单里那三条因此与包里逐字相同。
        built_version, size, sha256 = packaging.build_agent_bundle(
            tmp_path,
            server_url=probe.public_url,
            bootstrap_key=raw_key,
            install_policy=policy.install_policy,
        )
        if built_version != version:  # pragma: no cover - 上面刚比对过，双保险
            raise ApiError(409, "version_mismatch", "构建出的版本是 %s" % built_version)
        with tmp_path.open("rb") as handle:
            return _store_release(
                ctx,
                stream=handle,
                version=version,
                channel=payload.channel,
                notes=payload.notes,
                filename=tmp_path.name,
                admin=admin,
                source="built",
                bootstrap_key_id=bootstrap_key_id,
                policy=policy,
            )
    except packaging.BuildError as exc:
        # 原文只进日志：它可能带着一大堆路径和栈
        log.warning("构建 Agent 发布包失败：%s", exc.log)
        # 构建失败时那把已经签发的密钥会留在库里（一条没有任何包对应的记录）。
        # 这是**故意**的：它确实存在过、确实可能已经被谁看到，悄悄删掉反而会让人
        # 以为"没签发过"；而且它已经被写进过磁盘上的临时包（失败点在存储那一步时
        # 甚至已经进了 blob）。界面上它带着「随版本 x.y.z 附带」的标签，一眼能认出来，
        # 顺手吊销即可 —— 这也正是"用独立密钥、按版本吊销"这条路的意义。
        raise ApiError(500, "release_build_failed", exc.detail)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    version = (version or "").strip()
    if not version:
        raise HTTPException(status_code=400, detail="必须指定版本号")

    try:
        parse_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="版本号不合法: %s" % exc)

    return _store_release(
        ctx,
        stream=file.file,
        version=version,
        channel=channel,
        notes=notes,
        filename=Path(file.filename or "agent-bundle.tar.gz").name,
        admin=admin,
        source="uploaded",
    )


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
        return _release_with_key(session, target)


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
        return _release_with_key(session, row)


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
        return _release_with_key(session, row)


def _release_with_key(session: Session, row: AgentRelease) -> ReleaseOut:
    """单个版本动作（铺开/撤回/改备注/入库）的回执。

    和列表那条路的区别只有一个：这里连着当前会话查一次密钥，好让界面上的
    "附带过密钥 / 已吊销"在动作之后立刻是新的（而不是等下一次刷新列表）。
    版本记录本来就在手边，多这一次查询换来一条自洽的回执。
    """
    return _release_out(row, _bootstrap_keys_by_id(session, [row]))


@router.delete("/releases/{release_id}", response_model=SimpleAck)
def delete_release(
    release_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> SimpleAck:
    """删掉一条发布记录。

    **正在铺开的版本要先下架**再删：已经升级上去的 Agent 拿不到旧包也没关系
    （它们在本地已经有一份），但服务端这边"哪些机器该升到这个版本"的账
    会随着记录一起消失，出问题时就没有对照物了。

    发布包本身存在 blob 存储里且是内容寻址的，所以这条删除只针对记录；
    没有别的记录引用同一份内容时才会顺手清掉内容。
    """
    with ctx.db.session() as session:
        row = session.get(AgentRelease, release_id)
        if row is None:
            return SimpleAck(ok=True, detail="版本不存在")
        if row.published_at is not None and row.yanked_at is None:
            raise HTTPException(
                status_code=409,
                detail="版本 %s 正在铺开，请先「下架」再删除" % row.version,
            )
        version = row.version
        sha256 = row.sha256
        session.delete(row)
        session.flush()
        freed = _release_blob_if_unreferenced(session, ctx, sha256)

    detail = "已删除版本 %s" % version
    if freed:
        detail += "；发布包内容已从存储中清除"
    return SimpleAck(ok=True, detail=detail)


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
