"""FastAPI 依赖：请求上下文与 Agent 身份解析。

这个文件是**新配对模型的汇聚点**，值得说清楚它到底在做什么。

机器绑的是**人**（``agent.roster_entry_id`` → 名单条目），不是某场比赛的选手。
可"收代码、发文件、记成绩"全都要落到**某一场次里的某个选手**上。于是每个请求
都要做一次翻译：

    机器 ──► 名单条目（人，永久） ──► 某场次的 Player ──► 干活

翻译的第三步是动态的：一个学生今天在 A 场、明天在 B 场，机器不用重新配对 ——
只要那场比赛应用了含他的名单。所以这里解析出 ``(contest, player)``，
把它作为 ``AgentIdentity`` 往下传；下游（collect/deploy/judge/upload）
完全不用知道上面那层变了 —— 它们看到的仍然是一个普通的"某场次的某选手"。

解析不出来的情况有三种，各自的含义完全不同，必须分开：

* **未配对**（``roster_entry_id`` 为空）→ 机器刚注册上来，等教师配对
* **没有场次** → 配对好了，但没有一场"进行中、且名单含此人"的比赛
* **场次不唯一** → 一个考点同时跑多场，需要教师显式指定

前两种在 ``/tick`` 里是**正常回应**（机器该安静等着），只有第三种需要人动手。
把它们混成一个 403，现场就只能猜。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from fastapi import Depends, Query, Request
from sqlalchemy import select

from ..context import AppContext
from ..errors import ApiError
from ..models import Agent, Contest, ContestStatus, Player, RosterEntry
from ..security import hash_token

__all__ = [
    "AgentIdentity",
    "MachineIdentity",
    "PageParams",
    "Principal",
    "Resolution",
    "get_ctx",
    "require_agent",
    "require_principal",
    "resolve_machine",
]


def get_ctx(request: Request) -> AppContext:
    ctx: Optional[AppContext] = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - 装配错误
        raise RuntimeError("AppContext 未初始化")
    return ctx


class PageParams:
    """``?limit=&offset=`` —— **每个** GET 集合接口都有的两个参数。

    做成依赖而不是每个接口各写一遍 ``Query(50, ge=1, le=500)``：默认值与上限
    只要有一处写歪，前端的分页组件就会在某一个页面上突然报 422。
    """

    def __init__(
        self,
        limit: int = Query(50, ge=1, le=500, description="每页条数"),
        offset: int = Query(0, ge=0, description="起始位置"),
    ) -> None:
        self.limit = limit
        self.offset = offset

    def __repr__(self) -> str:  # pragma: no cover - 只为日志好看
        return "PageParams(limit=%d, offset=%d)" % (self.limit, self.offset)


@dataclass
class AgentIdentity:
    """**已经解析到具体场次与选手**的机器。

    下游代码只认这个形状 —— 它是"翻译"之后的产物，与机器怎么绑的无关。
    """

    agent_id: int
    player_id: int
    player_no: str
    player_name: Optional[str]
    contest_id: int
    contest_slug: str
    contest_name: str
    contest_status: str
    #: 绑定的名单条目。改派、界面显示"这台机器是谁的"都要用它
    roster_entry_id: Optional[int]
    roster_entry_name: Optional[str]
    machine_id: str
    hostname: Optional[str]


@dataclass
class MachineIdentity:
    """机器还在，但暂时干不了活。

    两种情况都归到这里，用 ``paired`` 区分：

    * ``paired=False`` 还没配对到人 —— 该显示配对码、等人来配
    * ``paired=True``  配好了但解析不出场次 —— 该安静等着，不要显示配对码
      （显示的话教师会以为配对没生效，去重配一遍，把好好的绑定搞乱）

    它**没有** player_id / contest_id —— 那不是"暂时为空"，而是"根本还没有"。
    用可空字段表示这件事，会让每个消费方都多一层 `if ... is None`，
    而漏掉任何一处都会变成把文件挂到不存在的选手身上。
    """

    agent_id: int
    machine_id: str
    hostname: Optional[str]
    paired: bool
    roster_entry_id: Optional[int]
    roster_entry_name: Optional[str]
    #: 解析不出场次时的人话原因
    reason: Optional[str] = None


Principal = Union[AgentIdentity, MachineIdentity]


@dataclass
class Resolution:
    """解析结果。内部用，外面只看得到 Principal 的两个形状。

    ``code`` 是**给客户端分支用的**：同样是"这台机器暂时干不了活"，
    "还没配对"要让人去配对、"还没有含你的场次"只要安静等着、
    "多个场次需要指定"要教师动手 —— 三种该做的事完全不同。
    ``reason`` 是给人看的中文句子，两者都带上，因为调用方既要分支也要显示。
    """

    agent: Agent
    entry: Optional[RosterEntry]
    contest: Optional[Contest]
    player: Optional[Player]
    reason: Optional[str] = None
    code: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.contest is not None and self.player is not None


def _unauthorized(detail: str, code: str = "unauthorized") -> "ApiError":
    return ApiError(
        401, code, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise _unauthorized("缺少 Bearer 凭据")
    raw = header[7:].strip()
    if not raw:
        raise _unauthorized("凭据为空")
    return raw


def resolve_machine(session, agent: Agent) -> Resolution:
    """把一台机器解析到"某场次的某选手"。

    场次的确定顺序：

    1. ``agent.contest_id`` 显式指定 → 就用它（教师为什么指定就是为什么）
    2. 否则找**进行中、且名单含此人**的场次：
       * 恰好一个 → 用它
       * 一个都没有 → 说清"还没有含你的场次"
       * 多于一个 → 说清"需要指定"，**不自己挑一个**（挑错的后果是代码与成绩
         落到另一场比赛里，而两场可能同时在跑）

    ``Player`` 行必须已经存在（教师把名单应用到场次时物化出来的）。
    这里**不自动创建** —— 靠机器在线与否去给一个场次添参赛者，
    会让"谁在这场比赛里"变成一件悄悄发生的事。
    """
    if agent.roster_entry_id is None:
        return Resolution(
            agent=agent, entry=None, contest=None, player=None, code="pairing_required"
        )

    entry = session.get(RosterEntry, agent.roster_entry_id)
    if entry is None:  # pragma: no cover - 外键 SET NULL 兜底
        return Resolution(
            agent=agent,
            entry=None,
            contest=None,
            player=None,
            reason="绑定的名单条目已被删除",
            code="pairing_required",
        )

    if agent.contest_id is not None:
        contest = session.get(Contest, agent.contest_id)
        if contest is None:
            return Resolution(
                agent=agent,
                entry=entry,
                contest=None,
                player=None,
                reason="指定的场次已被删除",
                code="contest_missing",
            )
        player = session.execute(
            select(Player).where(
                Player.contest_id == contest.id, Player.player_no == entry.player_no
            )
        ).scalar_one_or_none()
        if player is None:
            return Resolution(
                agent=agent,
                entry=entry,
                contest=contest,
                player=None,
                reason="指定场次「%s」里没有编号 %s 的选手，请先把这个人的名单应用到场次"
                % (contest.name, entry.player_no),
                code="contest_player_missing",
            )
        return Resolution(agent=agent, entry=entry, contest=contest, player=player)

    candidates: List[Tuple[Contest, Player]] = []
    matches: List[Tuple[Contest, Player]] = list(
        session.execute(
            select(Contest, Player)
            .join(Player, Player.contest_id == Contest.id)
            .where(Player.player_no == entry.player_no)
            .order_by(Contest.id)
        )
    )
    for contest, player in matches:
        if contest.is_active:
            candidates.append((contest, player))

    if not candidates:
        # "没有进行中的场次"里有两种完全不同的情况，说错了会让现场误判：
        #
        # * 这场**已经结束**了 —— 学生明明在考试（刚才还在交卷），看到"还没有包含
        #   你的场次"会以为系统坏了、机器掉线了，跑去重启或者重新注册
        # * 压根没有他的场次 —— 教师还没把名单应用到场次，那才是真的"还没有"
        #
        # 判据只看**已结束**这一个状态：``closed`` 是"这场结束了"，而机器该做的事
        # 两种情况下完全一样（安静等着，不要重新注册），所以 ``code`` 仍然用
        # ``no_active_contest`` —— Agent 那边不该为一句措辞多写一个分支。
        finished = [contest for contest, _ in matches if contest.status == ContestStatus.CLOSED]
        if finished:
            return Resolution(
                agent=agent,
                entry=entry,
                contest=None,
                player=None,
                reason="你所在的场次（%s）已经结束了" % "、".join(c.name for c in finished),
                code="no_active_contest",
            )
        return Resolution(
            agent=agent,
            entry=entry,
            contest=None,
            player=None,
            reason="还没有进行中的、名单里含 %s 的场次" % entry.player_no,
            code="no_active_contest",
        )
    if len(candidates) > 1:
        names = "、".join(contest.name for contest, _ in candidates)
        return Resolution(
            agent=agent,
            entry=entry,
            contest=None,
            player=None,
            reason="有多个进行中的场次都含 %s（%s），需要指定这一个（在界面上给这台机器选场次）"
            % (entry.player_no, names),
            code="ambiguous_contest",
        )
    contest, player = candidates[0]
    return Resolution(agent=agent, entry=entry, contest=contest, player=player)


def _load_agent(session, token_hash: str) -> Optional[Agent]:
    return session.execute(
        select(Agent).where(Agent.token_hash == token_hash)
    ).scalar_one_or_none()


def require_agent(request: Request, ctx: AppContext = Depends(get_ctx)) -> AgentIdentity:
    """要求一台**能干活**的机器。

    解析不出来时给的是 403 + 具体原因 + 具体 ``code``，不是 401。这个区别很重要：
    401 会让 Agent 以为凭据坏了，于是反复重新注册 —— 而它其实好好的，
    只是还没配对、或者还没有它的场次。**用错状态码会把一个安静等待
    变成一场注册风暴**，还会把真正的原因埋进日志噪音里。
    """
    token_hash = hash_token(_bearer(request))
    client_ip = _client_ip(request)
    with ctx.db.session() as session:
        agent = _load_agent(session, token_hash)
        if agent is None:
            raise _unauthorized("凭据无效")
        if agent.revoked_at is not None:
            raise _unauthorized("该机器的凭据已作废", code="machine_revoked")

        _note_ip(agent, client_ip)

        resolved = resolve_machine(session, agent)
        if not resolved.ok:
            raise ApiError(
                403,
                resolved.code or "pairing_required",
                _blocked_detail(agent.id, resolved),
                {"agent_id": agent.id, "paired": resolved.entry is not None},
            )

        identity = _to_identity(resolved)
        machine_id = agent.machine_id
        hostname = agent.hostname

    _remember(ctx, identity, machine_id, hostname, client_ip)
    return identity


def _client_ip(request: Request) -> Optional[str]:
    """这次请求的来源地址。

    ``request.client`` 在极少数情况下是 ``None``（ASGI 服务器没给出对端地址），
    那时返回 None —— 记不下地址只会让选手页退回手填那一条路，不该让请求失败。
    """
    return request.client.host if request.client else None


def _note_ip(agent: Agent, ip: Optional[str]) -> None:
    """把来源 IP 记在这台机器上。

    每个带凭据的请求都要走（心跳、上传、下载、事件），因为选手页的"自动匹配本机"
    是按 IP 找机器的，而**这一页自己不会触发心跳** —— 它进来时还没有任何凭据。
    只在心跳里记的话，选手刚开机看到的那一页会 404，几十秒后才"自己好了"。

    只在变化时赋值，避免每次请求都把这行标脏（SQLAlchemy 会因此生成一条 UPDATE）。
    """
    if ip and agent.last_seen_ip != ip:
        agent.last_seen_ip = ip


def _remember(
    ctx: AppContext,
    identity: AgentIdentity,
    machine_id,
    hostname,
    ip: Optional[str] = None,
) -> None:
    """把"这台机器现在是谁、在哪场比赛、从哪来"写进内存注册表。

    在线状态**只存在于内存里**（心跳时更新），所以每一个能干活请求都必须
    经过这里。漏掉任何一个（特别是 ``/tick`` —— 它才是心跳）的后果是
    管理界面上那一页永远是空的，而且不报错。
    """
    ctx.registry.upsert_identity(
        agent_id=identity.agent_id,
        player_id=identity.player_id,
        contest_id=identity.contest_id,
        player_no=identity.player_no,
        player_name=identity.player_name,
        contest_slug=identity.contest_slug,
        machine_id=machine_id,
        hostname=hostname,
        ip=ip,
    )


def _blocked_detail(agent_id: int, resolved: Resolution) -> str:
    if resolved.entry is None:
        return "这台机器还没配对到人，请在管理界面「机器配对」里认领（机器上有配对码）"
    return "这台机器已经配对给 %s，但暂时不能干活：%s" % (
        resolved.entry.player_no,
        resolved.reason or "找不到可用的场次",
    )


def _to_identity(resolved: Resolution) -> AgentIdentity:
    assert resolved.entry is not None and resolved.contest is not None
    assert resolved.player is not None
    return AgentIdentity(
        agent_id=resolved.agent.id,
        player_id=resolved.player.id,
        player_no=resolved.player.player_no,
        player_name=resolved.player.name,
        contest_id=resolved.contest.id,
        contest_slug=resolved.contest.slug,
        contest_name=resolved.contest.name,
        contest_status=resolved.contest.status,
        roster_entry_id=resolved.entry.id,
        roster_entry_name=resolved.entry.name,
        machine_id=resolved.agent.machine_id,
        hostname=resolved.agent.hostname,
    )


def require_principal(request: Request, ctx: AppContext = Depends(get_ctx)) -> Principal:
    """接受任意有效凭据：能干活就干活，不能干活就如实说明卡在哪。

    只有 ``/tick`` 用它 —— 干不了活的机器唯一要做的事就是定期问一句
    "轮到我了吗"。别的接口（上传、下载、事件）一律走 ``require_agent``：
    它们都需要选手身份，让干不了活的机器进来没有任何意义，
    只会多出一堆需要判空的分支。
    """
    token_hash = hash_token(_bearer(request))
    client_ip = _client_ip(request)
    with ctx.db.session() as session:
        agent = _load_agent(session, token_hash)
        if agent is None:
            raise _unauthorized("凭据无效")
        if agent.revoked_at is not None:
            raise _unauthorized("该机器的凭据已作废", code="machine_revoked")

        # 干不了活的机器**也要**记 IP：它正是"配对好了但还没有场次"的那一档
        # （或者还没配对），而选手页要用同一个地址把这台机器认出来、
        # 再如实告诉选手"卡在哪一档"。
        _note_ip(agent, client_ip)

        resolved = resolve_machine(session, agent)
        if resolved.ok:
            identity = _to_identity(resolved)
            machine_id = agent.machine_id
            hostname = agent.hostname
            # /tick 是**唯一**的心跳入口，而在线状态只活在内存注册表里。
            # 不在这里登记的话，管理界面看到的永远是一页空白 —— 而且不报错。
            _remember(ctx, identity, machine_id, hostname, client_ip)
            return identity

        entry = resolved.entry
        return MachineIdentity(
            agent_id=agent.id,
            machine_id=agent.machine_id,
            hostname=agent.hostname,
            paired=entry is not None,
            roster_entry_id=entry.id if entry else None,
            roster_entry_name=entry.name if entry else None,
            reason=resolved.reason,
        )
