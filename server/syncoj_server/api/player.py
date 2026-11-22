"""考生页（免登录）API。

这一页是给**考场里的选手**看的：他坐在机器前，要确认"这场发给我的文件到了没、
老师有什么话要说"。它必须免登录 —— 选手没有管理后台账号，现场也不会有人发一个给他。

页面分三层，从谁都能看的到只有本人能看的：

0. **降级预览**（``GET /preview``，默认）：当前在考的场次（``running`` / ``frozen``
   且还没到结束时间；多场可切换），每场只有名称、时间窗和**面向全场**的公告 ——
   ``NOTICE.md`` 的下发任务目标是"全场"（``target_kind == "all"``）才算。
   这一层谁都能打开，所以**绝不**含任何选手的信息或个人下发清单：只发给部分人的
   公告也不出现（那是给那部分人的话）。
1. **自动匹配本机**（``GET /context`` 不带令牌）：桌面上放一个指向这一页的快捷方式，
   选手点开就该直接看到自己的清单，一个字都不用填。服务端按请求的**来源 IP** 找那台
   机器，再用与 ``/tick`` **完全相同**的 ``resolve_machine`` 解析出"哪场比赛的哪个
   选手"。
2. **准考证号 + 考生页密码**（``POST /login`` 换令牌，``GET /context`` 带
   ``X-Player-Token``）：教师的笔记本、Agent 没起来的机器、换了 IP 的机器靠它。
   密码是**场次级**的（场次设置里设，库里只存哈希）；场次没设密码时这条路关着。
   令牌是 HMAC 短期令牌（见 ``services/player_tokens.py``），绑定场次 + 选手，
   并签进了密码指纹 —— 教师换掉或清除密码，签出去的令牌全部作废。

曾经的第三条路"只填场次 + 考号、不要密码"已经删掉：它让任何人都能看任何人的清单。

后两条路回同一个模型，并如实告诉调用方是哪一种（``matched_by``）。

免登录带来的边界，全部在下面这几行里：

* 考号**精确匹配**，不做模糊、不做"最近一个"这类兜底；登录失败**统一 403**，
  不区分"考号不存在"和"密码错"（否则一个循环就能试出哪些考号存在），并按 IP 限速。
* 返回的只有这个选手自己的东西：下发给他的资产台账（按 ``DeployTarget.player_id``
  过滤）、**从他自己的下发目标里认出来的公告**（文件名 ``NOTICE.md``，见
  ``_notice_for``）、以及他自己的考号/姓名/座位/分组。代码（``SourceFile``）、
  成绩（``JudgeRun``）、别的选手一律**不进模型**。前端不显示不是边界，
  服务端不返回才是。
* 网页下载（``GET /assets/{id}``）只给**确实下发给这个人、且没取消**的资产。
* 来源 IP 只用来缩小"打开这一页的是哪台机器"的范围，它**不是身份依据**
  （可共享、可变、局域网里也可伪造）。所以它解析出来的仍然只是"这台机器自己"
  这个身份，解析不出场次时如实报卡在哪一档，绝不猜测。
* 失效的令牌回 **403** ``player_token_invalid`` 而不是 401：管理端前端把 401
  当成"管理员会话失效"，教师在自己的浏览器里打开考生页时会被踢回登录页。

为什么单独一个路由文件而不是塞进 ``agent.py``：两者的调用方、鉴权方式和能看到的数据
都不同（Agent 有 Bearer 凭据和自己的 identity，这里什么都没有）。混在一个 900 行的
文件里，"这个端点是免登录的"这件事会被淹没 —— 而"下一个加接口的人顺手把
``require_agent`` 也挂上去"正是最可能发生的事故。
"""

from __future__ import annotations

import logging
import time
from datetime import timezone
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..services.ratelimit import RateLimitExceeded
from ..context import AppContext
from ..errors import ApiError
from ..models import (
    Agent,
    Asset,
    Contest,
    ContestStatus,
    DeployStatus,
    DeployTarget,
    DeployTask,
    Player,
    utcnow,
)
from ..schemas import (
    PlayerAssetOut,
    PlayerContextOut,
    PlayerContestOut,
    PlayerLoginIn,
    PlayerLoginOut,
    PlayerNoticeOut,
    PlayerPreviewContestOut,
    PlayerPreviewOut,
    PlayerProfileOut,
)
from ..security import verify_password
from ..services import player_tokens
from ..services.deploy import expand_dest_template
from .attachments import blob_response
from .deps import get_ctx, resolve_machine

__all__ = ["router", "TOKEN_HEADER"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/player", tags=["player"])

#: 登录令牌放在这个头里。不用 ``Authorization``：教师在管理端的浏览器里打开考生页时，
#: 前端会顺手带上管理员的 Bearer —— 两种凭据挤在一个头里只会互相顶掉。
TOKEN_HEADER = "X-Player-Token"

#: 一次最多回多少条下发目标。契约里定的上限：清单是给人看的，超过 200 行时
#: 真正的问题不是"还有多少条没显示"，而是这个场次的下发配置出了别的事。
MAX_ASSETS = 200

#: 降级预览最多列几场。同时在考的场次本来就是个位数。
MAX_PREVIEW_CONTESTS = 20

#: 考场公告的**文件名约定**：基名等于它（不分大小写）就算公告。
#:
#: 为什么不是"在资产上加一个 is_notice 字段"：那等于让教师再配置一次，而公告
#: 本来就该走文件下发那条路（教师用「新建文本文件」写一份 NOTICE.md 下发）。
#: 换一个字段就是把"文件下发"重新变回"场次上的配置"，正是这一轮要去掉的形态。
NOTICE_FILENAME = "notice.md"

#: 公告正文的大小上限。公告是"老师写在页首的几句话"，几十 KB 已经远超正常；
#: 再大的东西读进来只会变成一坨塞进 JSON、把选手的浏览器拖住 ——
#: 而它根本不是公告，当成"不是公告"跳过比塞进去更对。
MAX_NOTICE_BYTES = 64 * 1024

#: 与 ``api/admin.py`` 的 ``_ISO`` 一致：全站时间字符串只有这一种形状。
#: 这里宁可重复一个常量，也不从 ``admin`` 里 import 一个私有名 ——
#: 那会让"改 admin 的格式"变成"顺手改掉选手页的格式"。
_ISO = "%Y-%m-%dT%H:%M:%SZ"

#: 下发根目录在选手眼里的名字。
#:
#: README 的默认值是 ``agent.deploy_root = {desktop}``，于是"目标目录留空"的意思
#: 就是**直接放在桌面上**。服务端拿不到那台机器上的真实配置（deploy_root 可以改），
#: 所以这里用的是文档承诺的默认值：页面上写"桌面"对这个考场的默认装机是对的，
#: 而写一个空串或者 ``{player_no}`` 一定是错的。
DEPLOY_ROOT_LABEL = "桌面"

#: ``resolve_machine`` 给出的 ``code`` → HTTP 状态码。
#:
#: 这套分工与 ``/tick`` 那侧一致，而且必须一致：
#:
#: * 403 = "机器和凭据都没问题，只是还没有你的位置" —— 安静等人处理，不是错误
#: * 409 = "有东西，但不唯一或者指定的那个已经不对了" —— 需要老师改一个具体配置
#:
#: 混成一个 404 的话，现场就分不清"这台机器没注册"和"这台上没有你的场次"。
_BLOCKED_STATUS = {
    "pairing_required": 403,
    "no_active_contest": 403,
    "ambiguous_contest": 409,
    "contest_missing": 409,
    "contest_player_missing": 409,
}


@router.get("/context", response_model=PlayerContextOut)
def player_context(
    request: Request,
    x_player_token: Optional[str] = Header(
        default=None,
        max_length=256,
        description="考生页登录令牌（POST /player/login 签出）。不带 = 按来源 IP 自动匹配本机",
    ),
    ctx: AppContext = Depends(get_ctx),
) -> PlayerContextOut:
    """取考生页上"这个选手"的全部内容。**不需要管理端凭据。**

    带 ``X-Player-Token`` = 已登录的考生；不带 = 按来源 IP 自动匹配本机。
    自动匹配失败时回 403/404/409 人话，页面据此退到降级预览（``GET /preview``）。
    """
    now = utcnow()
    with ctx.db.session() as session:
        if x_player_token:
            contest_row, player = _resolve_by_token(session, ctx, x_player_token)
            matched_by = "token"
        else:
            contest_row, player, matched_by = _resolve_by_machine(session, request)

        return _build_context(session, contest_row, player, ctx, matched_by, now)


@router.get("/preview", response_model=PlayerPreviewOut)
def player_preview(ctx: AppContext = Depends(get_ctx)) -> PlayerPreviewOut:
    """降级预览：当前在考的场次与面向全场的公告。**谁都能看，所以不含任何个人信息。**

    "在考" = 状态是 ``running`` / ``frozen``（不是这两个就不算，以后加的状态也一样），
    并且还没到结束时间。**还没开考的也列出来**：考生提前进场时要看到"距开考"
    倒计时和全场公告，而那份公告本来就已经下发到了每台机器上。
    """
    now = utcnow()
    with ctx.db.session() as session:
        contests = session.execute(
            select(Contest)
            .where(Contest.status.in_((ContestStatus.RUNNING, ContestStatus.FROZEN)))
            .order_by(Contest.id)
        ).scalars().all()
        live = [row for row in contests if row.ends_at is None or now < row.ends_at]
        # 先开考的排前面；没配开考时间的按建场顺序排在最前（它们"一直在考"）
        live.sort(key=lambda row: (row.starts_at is not None, row.starts_at or now, row.id))

        items = [
            PlayerPreviewContestOut(
                slug=row.slug,
                name=row.name,
                status=row.status,
                starts_at=_iso(row.starts_at),
                ends_at=_iso(row.ends_at),
                notice=_contest_wide_notice(session, ctx, row.id),
                login_enabled=bool(row.player_password_hash),
            )
            for row in live[:MAX_PREVIEW_CONTESTS]
        ]
    return PlayerPreviewOut(contests=items, server_time=_unix(now))


@router.post("/login", response_model=PlayerLoginOut)
def player_login(
    payload: PlayerLoginIn,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> PlayerLoginOut:
    """准考证号 + 场次考生页密码 → 短期令牌。

    * 考号**精确匹配**（只去掉首尾空白：从准考证上抄过来多一个空格是常事）
    * 考号不存在与密码错**统一 403** ``player_login_failed``；两种情况都完整地算一遍
      scrypt，响应时间上也分不出来
    * 场次没设密码 → 409 ``player_login_disabled``：这条路关着，页面只剩降级预览
    * 按来源 IP 限速（``player_lookup_per_ip_per_minute``），先限速再算 scrypt
    """
    client_ip = request.client.host if request.client else "unknown"
    try:
        ctx.player_lookup_limiter.check(client_ip, time.monotonic())
    except RateLimitExceeded as exc:
        raise ApiError(
            429, "rate_limited", "尝试太频繁了，请稍后再试",
            {"retry_after": exc.retry_after},
        )

    slug = payload.contest.strip()
    number = payload.player_no.strip()
    with ctx.db.session() as session:
        contest_row = session.execute(
            select(Contest).where(Contest.slug == slug)
        ).scalar_one_or_none()
        if contest_row is None:
            raise ApiError(404, "not_found", "没有标识为 %s 的场次" % slug)
        password_hash = contest_row.player_password_hash
        if not password_hash:
            raise ApiError(409, "player_login_disabled", "本场没有开放考生登录")

        # 先验密码、再查人：两种失败走同样多的 scrypt，时间上分不出"考号不存在"
        password_ok = verify_password(payload.password, password_hash)
        player = session.execute(
            select(Player).where(
                Player.contest_id == contest_row.id,
                Player.player_no == number,
            )
        ).scalar_one_or_none()
        if player is None or not password_ok:
            raise ApiError(403, "player_login_failed", "准考证号或密码不对")

        token, expires_at = player_tokens.issue_token(
            _token_secret(ctx), contest_row.id, player.id, password_hash, int(time.time())
        )
        log.info("考生页登录：场次 %s 考号 %s（来源 %s）", contest_row.slug, player.player_no, client_ip)
    return PlayerLoginOut(token=token, expires_at=expires_at)


@router.get("/assets/{asset_id}")
def player_download_asset(
    asset_id: int,
    request: Request,
    x_player_token: Optional[str] = Header(default=None, max_length=256),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    """网页下载一个下发给本人的文件。

    身份二选一：登录令牌，或来源 IP 自动匹配到本机。之后**必须**确实有一条
    "这个资产 → 这个选手"的下发目标，而且任务没取消、目标也没被取消 ——
    与 Agent 下载（``api/agent.py::download_asset``）同一个判据。

    * 认不出你是谁 → 403
    * 资产不存在 / 不在你这一场 / 这一场里根本没下发过 → 404
    * 下发过、但不是给你（或已取消）→ 403
    * 内容已被清理 → 410
    """
    with ctx.db.session() as session:
        if x_player_token:
            contest_row, player = _resolve_by_token(session, ctx, x_player_token)
        else:
            try:
                contest_row, player, _ = _resolve_by_machine(session, request)
            except ApiError:
                raise ApiError(403, "forbidden", "认不出你是哪位考生 —— 请先登录")

        asset = session.get(Asset, asset_id)
        if asset is None or asset.contest_id != contest_row.id:
            raise ApiError(404, "not_found", "没有这个文件")

        live_deploy = (
            DeployTask.asset_id == asset.id,
            DeployTask.contest_id == contest_row.id,
            DeployTask.cancelled_at.is_(None),
            DeployTarget.status != DeployStatus.CANCELLED,
        )
        mine = session.execute(
            select(DeployTarget.id)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(DeployTarget.player_id == player.id, *live_deploy)
            .limit(1)
        ).first()
        if mine is None:
            anyone = session.execute(
                select(DeployTarget.id)
                .join(DeployTask, DeployTarget.task_id == DeployTask.id)
                .where(DeployTask.asset_id == asset.id, DeployTask.contest_id == contest_row.id)
                .limit(1)
            ).first()
            if anyone is None:
                raise ApiError(404, "not_found", "没有这个文件")
            raise ApiError(403, "forbidden", "这个文件没有下发给你")

        sha256, filename = asset.sha256, asset.filename

    if not ctx.blobs.path_for(sha256).is_file():
        raise ApiError(410, "gone", "这个文件的内容已经不在服务端了 —— 请找监考老师")
    return blob_response(ctx, sha256, Path(filename).name or filename)


# --------------------------------------------------------------------------- #
# 两种定位方式
# --------------------------------------------------------------------------- #


def _token_secret(ctx: AppContext) -> bytes:
    """签令牌的密钥：第一次用到时从数据目录读出（没有就生成），之后记在 ctx 上。"""
    secret = ctx.player_token_secret
    if secret is None:
        secret = player_tokens.load_or_create_secret(ctx.settings.data_root)
        ctx.player_token_secret = secret
    return secret


def _resolve_by_token(session: Session, ctx: AppContext, token: str) -> Tuple[Contest, Player]:
    """登录令牌 → (场次, 选手)。任何一步不对都是同一个 403 ``player_token_invalid``。

    验签用的是场次**当前**的密码哈希：密码换了 / 清了，旧令牌当场作废。
    """
    invalid = ApiError(403, "player_token_invalid", "考生登录已失效，请重新登录")
    claims = player_tokens.parse_token(token)
    if claims is None:
        raise invalid
    contest_row = session.get(Contest, claims.contest_id)
    player = session.get(Player, claims.player_id)
    if contest_row is None or player is None or player.contest_id != contest_row.id:
        raise invalid
    verified = player_tokens.verify_token(
        _token_secret(ctx), token, contest_row.player_password_hash, int(time.time())
    )
    if verified is None:
        raise invalid
    return contest_row, player


def _resolve_by_machine(
    session: Session, request: Request
) -> Tuple[Contest, Player, str]:
    """按来源 IP 认机器，再交给 ``resolve_machine`` 解析场次与选手（主路径）。

    **解析场次这一段刻意与 ``/tick`` 共用同一个函数**：同一台机器在这一页说
    "还没有含你的场次"、在 tick 里却"能干活"的话，两边都会有人去查，而查不出东西 ——
    因为差异来自两套实现，不是来自状态。
    """
    ip = request.client.host if request.client else None
    agent = _agent_by_ip(session, ip)
    if agent is None:
        raise ApiError(
            404,
            "not_found",
            "这台机器还没有注册上来（或者 Agent 没在运行）—— 请找监考老师",
            {"matched_by": "machine"},
        )

    resolved = resolve_machine(session, agent)
    if not resolved.ok:
        code = resolved.code or "pairing_required"
        raise ApiError(
            _BLOCKED_STATUS.get(code, 403),
            code,
            _machine_blocked_detail(resolved),
            {
                "matched_by": "machine",
                "agent_id": agent.id,
                "paired": resolved.entry is not None,
            },
        )

    # resolve_machine 保证了这两个不为空（``ok`` 就是它们的合取）
    assert resolved.contest is not None and resolved.player is not None
    return resolved.contest, resolved.player, "machine"


def _agent_by_ip(session: Session, ip: Optional[str]) -> Optional[Agent]:
    """来源 IP → 那台机器。

    ``agent.last_seen_ip`` 在**每一次**带凭据的请求上更新（见 ``deps._note_ip``），
    所以这里读到的是"刚刚才说过话的那台机器"，而不是上一次心跳时的快照。

    同一个 IP 上有多台机器（NAT、或者一台机器装了两次 Agent）时按"最近说话"取一台：
    取巧，但不会串人 —— 这条查询的结果只用来定位**这台机器自己**，
    绝不会被当成"谁是某某号"的依据（那要靠配对关系）。
    """
    if not ip:
        return None
    return session.execute(
        select(Agent)
        .where(Agent.last_seen_ip == ip)
        .order_by(Agent.last_seen_at.desc().nullslast(), Agent.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _machine_blocked_detail(resolved) -> str:
    """机器在，但这一页看不了：把 ``resolve_machine`` 给的原因说成人话。

    原因用它的原话（``resolved.reason``），不在这里另编一套说法 —— 那句话是
    给教师看的同一句，两边措辞不一致时现场会以为是两回事。
    """
    if resolved.code == "pairing_required":
        return "这台机器还没有配对到人 —— 请找监考老师读桌上的配对码"
    return "这台机器还不能用：%s —— 请找监考老师" % (
        resolved.reason or "找不到可用的场次"
    )


# --------------------------------------------------------------------------- #
# 组装响应
# --------------------------------------------------------------------------- #


def _build_context(
    session: Session,
    contest_row: Contest,
    player: Player,
    ctx: AppContext,
    matched_by: str,
    now,
) -> PlayerContextOut:
    """两条定位路径共用的取数逻辑。"""
    assets = _assets_for(session, contest_row.id, player.id, player.player_no)
    # 公告与清单走**同一套过滤**（这个人 + 这一场），区别只在"挑出 NOTICE.md"：
    # 少了同一个过滤条件，页面就会把别人的公告、甚至别的场次的公告显示出来。
    notice = _notice_for(session, ctx, contest_row.id, player.id)

    return PlayerContextOut(
        contest=PlayerContestOut(
            slug=contest_row.slug,
            name=contest_row.name,
            status=contest_row.status,
            # 选手要能自己看到"几点开考、几点结束"：到点之后服务端会拒收文件，
            # 而他是唯一一个需要提前知道这件事的人。
            starts_at=_iso(contest_row.starts_at),
            ends_at=_iso(contest_row.ends_at),
        ),
        # 两条路都证明了"就是这个人"（本机 / 准考证号 + 密码），所以回全部。
        # 曾经那条不要密码的手填查找只回考号 —— 它已经删掉了。
        player=PlayerProfileOut(
            player_no=player.player_no,
            name=player.name,
            seat=player.seat,
            group_name=player.group_name,
        ),
        # 没下发公告就是 None（不是空对象）：前端只判一次"有没有"，
        # 不需要分辨"{} 和 null 哪个是没公告"。
        notice=notice,
        assets=assets,
        matched_by=matched_by,
        server_time=_unix(now),
    )


def _assets_for(
    session: Session, contest_id: int, player_id: int, player_no: str
) -> List[PlayerAssetOut]:
    """清单 = 这场里**下发给这个人**的资产，按下发时间倒序。

    两个 where 条件各有分工，缺一不可：

    * ``DeployTarget.player_id == player_id`` —— 这一行是整页的边界。少了它，
      任何选手（或者任何一台机器）都能看到全场的下发清单，以及谁该收到什么
    * ``DeployTask.contest_id == contest_id`` —— 机器绑的是人、不绑场次，
      同一个人可能在多场里都在；他看的是**这一场**的清单

    顺序按 ``DeployTask.created_at`` 倒序、再按 id 倒序：刚发的排在最上面，
    而同一秒建的两条任务（批量下发时很常见）也有确定顺序 —— 顺序不确定的列表
    在两个请求之间会跳来跳去，选手会以为清单在变。
    """
    rows = session.execute(
        select(DeployTarget, DeployTask, Asset)
        .join(DeployTask, DeployTarget.task_id == DeployTask.id)
        .join(Asset, DeployTask.asset_id == Asset.id)
        .where(
            DeployTarget.player_id == player_id,
            DeployTask.contest_id == contest_id,
        )
        .order_by(DeployTask.created_at.desc(), DeployTask.id.desc())
        .limit(MAX_ASSETS)
    ).all()

    return [
        PlayerAssetOut(
            id=asset.id,
            filename=asset.filename,
            dest_dir=_student_dest_dir(task.dest_dir, player_no),
            status=target.status,
            size=int(asset.size),
            sha256=asset.sha256,
            # 只有 done 才有"完成时间"。其余状态给 null，而不是把
            # "最后一次状态变化"当成完成时间 —— 那会让没到手的文件看起来已经到手了
            finished_at=_iso(target.updated_at)
            if target.status == DeployStatus.DONE
            else None,
        )
        for target, task, asset in rows
    ]


def _student_dest_dir(dest_dir: str, player_no: str) -> str:
    """把 ``DeployTask.dest_dir`` 说成选手看得懂的一个目录。

    教师填的是一个**模板**（``{player_no}/<题目名>``），而 Agent 拿它去和下发的
    根目录 ``deploy_root`` 拼 —— 页面上原样显示 ``{player_no}`` 等于没说。
    所以这里做两件事：

    * 展开 ``{player_no}``（复用下发那侧的同一份实现）
    * 空的模板补上"桌面"：留空的意思就是"直接放在桌面上"，
      前端拿到空串只能显示一个"—"，而那个文件其实就在桌面上

    非空的模板前面加上"桌面/"（除非教师自己已经写了），因为 ``deploy_root`` 的
    默认值就是桌面 —— 页面上给出的必须是**选手一眼能找到的那个位置**。
    服务端拿不到那台机器上的真实 ``deploy_root``，所以这里用的是文档承诺的默认值。
    """
    text = expand_dest_template(dest_dir or "", player_no).strip().strip("/")
    if not text:
        return DEPLOY_ROOT_LABEL
    if text == DEPLOY_ROOT_LABEL or text.startswith(DEPLOY_ROOT_LABEL + "/"):
        return text
    return "%s/%s" % (DEPLOY_ROOT_LABEL, text)


# --------------------------------------------------------------------------- #
# 考场公告：从下发给他的资产里认出一个 NOTICE.md
# --------------------------------------------------------------------------- #


def _notice_for(
    session: Session,
    ctx: AppContext,
    contest_id: int,
    player_id: int,
) -> Optional[PlayerNoticeOut]:
    """在这一场发给**这个选手**的资产里找考场公告，找不到返回 ``None``。

    **过滤条件与清单完全一致**（``DeployTarget.player_id`` + ``DeployTask.contest_id``），
    这一点是整段的边界：少了任何一半，选手页上就会出现**别人的公告**或者**别的场次的
    公告** —— 页面照样正常渲染，现场没人会发现，而公告是老师要全场看的话，
    发错人比看不到更糟。

    多个候选（教师误发了两次 NOTICE.md）时取**最后下发**的那一个（下发任务 id 最大），
    并记一条 warning：那是配置失误，但不该因此让页面空着 —— 选手更需要看到公告。
    排序在这里顺带完成，下面第一次循环就是"从新到旧"。

    读不出来的候选（超大 / 非 UTF-8）**跳过、继续看下一个**：把"这个文件不是公告"
    当作它的状态，而不是让整页 500。
    """
    rows = session.execute(
        select(Asset, DeployTask.id)
        .join(DeployTask, DeployTask.asset_id == Asset.id)
        .join(DeployTarget, DeployTarget.task_id == DeployTask.id)
        .where(
            DeployTarget.player_id == player_id,
            DeployTask.contest_id == contest_id,
        )
        .order_by(DeployTask.id.desc())
    ).all()

    # 基名比较、大小写不敏感：教师按约定命名为 NOTICE.md，但他可能写成 notice.md
    # 或者 Notice.Md；服务端不该为一个大小写让整场人看不到公告。
    #
    # 去重按 (下发任务, 资产)：一条下发任务理论上不该给同一个选手建两个目标，
    # 但 player_ids 里传了重复值时就会 —— 那不是"下发了两个公告"，
    # 不能让它触发下面那条 warning。
    candidates: List[Asset] = []
    seen = set()
    for asset, task_id in rows:
        key = (task_id, asset.id)
        if key in seen:
            continue
        seen.add(key)
        if Path(asset.filename or "").name.lower() == NOTICE_FILENAME:
            candidates.append(asset)
    if not candidates:
        return None

    if len(candidates) > 1:
        log.warning(
            "场次 %d 给选手 %d 下发了 %d 个公告文件（%s），页面只显示最后下发的 %s "
            "—— 请删掉多余的那几个",
            contest_id,
            player_id,
            len(candidates),
            "、".join(asset.filename for asset in candidates),
            candidates[0].filename,
        )

    for asset in candidates:
        text = _read_notice_text(ctx, asset)
        if text is not None:
            return PlayerNoticeOut(filename=asset.filename, content=text)
    return None


def _contest_wide_notice(
    session: Session, ctx: AppContext, contest_id: int
) -> Optional[PlayerNoticeOut]:
    """降级预览用的**全场公告**：目标是全场（``target_kind == "all"``）、没取消的
    ``NOTICE.md`` 下发里最后下发的那一个。

    只认 ``all``：发给分组 / 指定选手的公告是给那几个人的话，降级预览谁都能看，
    放出来就是把它发给了全场。宁可空着，也不猜"这几个人差不多就是全场"。
    """
    rows = session.execute(
        select(Asset)
        .join(DeployTask, DeployTask.asset_id == Asset.id)
        .where(
            DeployTask.contest_id == contest_id,
            DeployTask.target_kind == "all",
            DeployTask.cancelled_at.is_(None),
        )
        .order_by(DeployTask.id.desc())
    ).scalars().all()
    for asset in rows:
        if Path(asset.filename or "").name.lower() != NOTICE_FILENAME:
            continue
        text = _read_notice_text(ctx, asset)
        if text is not None:
            return PlayerNoticeOut(filename=asset.filename, content=text)
    return None


def _read_notice_text(ctx: AppContext, asset: Asset) -> Optional[str]:
    """读出一个候选资产的正文；它不像公告就返回 ``None``。

    三种"不像公告"一律跳过，**绝不抛出去**：这一页是免登录给考场用的，
    一个坏文件名换来一整页 500 是拿全场的时间在赌。

    * 记在库里的 ``size`` 已经超过上限 —— 连盘都不开
    * 真读出来超过上限 —— ``size`` 是上传那一刻记的，可能有别的东西改过它，
      所以按**实际读到的字节数**再判一次（多读一个字节用来发现"超了"）
    * 不是合法 UTF-8 —— 解码不出来就没有正文可显示

    正文按与「新建文本文件」那条入口一致的方式归一：去掉开头的 BOM、换行统一成
    ``\\n``。只清"字节层面的噪声"，不 trim 首尾空白 —— 页面上按原文显示，
    替教师改一个字都是错的。
    """
    size = int(asset.size or 0)
    if size > MAX_NOTICE_BYTES:
        log.info(
            "选手页：公告候选 %s 有 %d 字节，超过上限 %d，当作不是公告跳过",
            asset.filename,
            size,
            MAX_NOTICE_BYTES,
        )
        return None

    try:
        with ctx.blobs.open(asset.sha256) as handle:
            raw = handle.read(MAX_NOTICE_BYTES + 1)
    except OSError as exc:
        # blob 丢了（回收过、磁盘坏了）也不该让整页挂掉
        log.warning("选手页：公告候选 %s 读不出来（%s），跳过", asset.filename, exc)
        return None

    if len(raw) > MAX_NOTICE_BYTES:
        log.info(
            "选手页：公告候选 %s 实际超过 %d 字节，当作不是公告跳过",
            asset.filename,
            MAX_NOTICE_BYTES,
        )
        return None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        log.info("选手页：公告候选 %s 不是合法 UTF-8，当作不是公告跳过", asset.filename)
        return None

    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _iso(value) -> Optional[str]:
    return value.strftime(_ISO) if value else None


def _unix(now) -> int:
    """naive UTC → Unix 秒。

    取 epoch 之前**补回 tzinfo**：``utcnow()`` 是 naive UTC，直接对它
    ``.timestamp()`` 会按本机时区去解释，在东八区的机器上整整差 8 小时。
    页面上的倒计时就是靠这个数算的，差 8 小时就是直接显示错。
    """
    return int(now.replace(tzinfo=timezone.utc).timestamp())
