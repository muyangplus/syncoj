"""选手页（免登录）API。

这一页是给**考场里的选手**看的：他坐在机器前，要确认"这场发给我的文件到了没、
老师有什么话要说"。它必须免登录 —— 选手没有管理后台账号，现场也不会有人发一个给他。

于是身份只有两条来源，优先级明确：

1. **自动匹配本机**（主路径）：桌面上放一个指向这一页的快捷方式，选手点开就该直接
   看到自己的清单，一个字都不用填。服务端按请求的**来源 IP** 找那台机器，再用与
   ``/tick`` **完全相同**的 ``resolve_machine`` 解析出"哪场比赛的哪个选手"。
2. **手填 ``contest`` + ``player_no``**（兜底）：教师的笔记本、Agent 没起来的机器、
   或者换了 IP 的机器，靠这两个值精确查找。

两条路都回同一个模型，并如实告诉调用方是哪一种（``matched_by``）。

免登录带来的边界，全部在下面这几行里：

* 只有两个自由输入，命中不了就 404/403，**不做模糊匹配、不做"最近一个"这类兜底** ——
  输错一个字符就该看到"没有这个选手"，而不是看到别人的清单。
* 返回的只有这个选手自己的东西：下发给他的资产台账（按 ``DeployTarget.player_id``
  过滤）、**从他自己的下发目标里认出来的公告**（文件名 ``NOTICE.md``，见
  ``_notice_for``）、以及他自己的考号/姓名/座位/分组。代码（``SourceFile``）、
  成绩（``JudgeRun``）、别的选手一律**不进模型**。前端不显示不是边界，
  服务端不返回才是。
* 来源 IP 只用来缩小"打开这一页的是哪台机器"的范围，它**不是身份依据**
  （可共享、可变、局域网里也可伪造）。所以它解析出来的仍然只是"这台机器自己"
  这个身份，解析不出场次时如实报卡在哪一档，绝不猜测。
* 它也不比别的接口多给一个字节：资产清单里的文件名、目录、状态本来就是发给选手的。

为什么单独一个路由文件而不是塞进 ``agent.py``：两者的调用方、鉴权方式和能看到的数据
都不同（Agent 有 Bearer 凭据和自己的 identity，这里什么都没有）。混在一个 900 行的
文件里，"这个端点是免登录的"这件事会被淹没 —— 而"下一个加接口的人顺手把
``require_agent`` 也挂上去"正是最可能发生的事故。
"""

from __future__ import annotations

import logging
from datetime import timezone
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..errors import ApiError
from ..models import (
    Agent,
    Asset,
    Contest,
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
    PlayerNoticeOut,
    PlayerProfileOut,
)
from ..services.deploy import expand_dest_template
from .deps import get_ctx, resolve_machine

__all__ = ["router"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/player", tags=["player"])

#: 一次最多回多少条下发目标。契约里定的上限：清单是给人看的，超过 200 行时
#: 真正的问题不是"还有多少条没显示"，而是这个场次的下发配置出了别的事。
MAX_ASSETS = 200

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
    contest: Optional[str] = Query(
        default=None, max_length=64, description="场次标识（slug），自动匹配不上时手填"
    ),
    player_no: Optional[str] = Query(
        default=None, max_length=64, description="考号，自动匹配不上时手填"
    ),
    ctx: AppContext = Depends(get_ctx),
) -> PlayerContextOut:
    """取选手页要显示的全部内容。**不鉴权。**

    不传参数 = 按来源 IP 自动匹配本机；两个参数都传 = 显式查找。
    只传一个直接 400：单独一个 ``contest`` 或单独一个 ``player_no`` 都定位不到人，
    而"猜另一半"正是这一页最不该做的事。
    """
    slug = (contest or "").strip()
    number = (player_no or "").strip()
    if bool(slug) != bool(number):
        raise ApiError(
            400,
            "bad_request",
            "场次和考号要么都填、要么都不填 —— 只填一个没法确定是谁",
        )

    now = utcnow()
    with ctx.db.session() as session:
        if slug and number:
            contest_row, player, matched_by = _resolve_explicit(session, slug, number)
        else:
            contest_row, player, matched_by = _resolve_by_machine(session, request)

        return _build_context(session, contest_row, player, ctx, matched_by, now)


# --------------------------------------------------------------------------- #
# 两种定位方式
# --------------------------------------------------------------------------- #


def _resolve_explicit(
    session: Session, slug: str, number: str
) -> Tuple[Contest, Player, str]:
    """按"场次 slug + 考号"精确查找（兜底路径）。

    现场最常见的是考号输错，所以那句 404 要把**两个值都报出来** —— "没有这个选手"
    五个字分不清是场次错了还是考号错了。按精确相等命中，不做任何模糊匹配。
    """
    contest_row = session.execute(
        select(Contest).where(Contest.slug == slug)
    ).scalar_one_or_none()
    if contest_row is None:
        raise ApiError(404, "not_found", "没有标识为 %s 的场次，请核对地址" % slug)

    # 库里的 player_no 在写入时都 strip 过（见 admin.py 的三处导入），查询前也 strip：
    # 从屏幕上抄一个 " S001 " 过来是现场的高频动作，为一个空格 404 没有道理。
    player = session.execute(
        select(Player).where(
            Player.contest_id == contest_row.id,
            Player.player_no == number,
        )
    ).scalar_one_or_none()
    if player is None:
        raise ApiError(
            404,
            "not_found",
            "场次 %s 里没有编号 %s 的选手，请核对考号是否输错" % (contest_row.slug, number),
        )
    return contest_row, player, "explicit"


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
        # 取 epoch 之前**补回 tzinfo**：``utcnow()`` 是 naive UTC，直接对它
        # ``.timestamp()`` 会按本机时区去解释，在东八区的机器上整整差 8 小时。
        # 页面上"清单是服务端几点取的"就是靠这个数显示的，差 8 小时就是直接显示错。
        server_time=int(now.replace(tzinfo=timezone.utc).timestamp()),
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
