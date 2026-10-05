"""FastAPI 依赖：请求上下文与 Agent 身份认证。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from ..context import AppContext
from ..models import Agent, Contest, MachineClaim, Player
from ..security import hash_token
from ..services import enrollment

__all__ = [
    "AgentIdentity",
    "MachineIdentity",
    "Principal",
    "get_ctx",
    "require_agent",
    "require_principal",
]


def get_ctx(request: Request) -> AppContext:
    ctx: Optional[AppContext] = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - 装配错误
        raise RuntimeError("AppContext 未初始化")
    return ctx


@dataclass
class AgentIdentity:
    agent_id: int
    player_id: int
    player_no: str
    player_name: Optional[str]
    contest_id: int
    contest_slug: str
    contest_name: str
    contest_status: str
    machine_id: str
    hostname: Optional[str]


@dataclass
class MachineIdentity:
    """还没认领到人的机器。

    它**没有** player_id / contest_id —— 那不是"暂时为空"，而是"根本还不知道"。
    用可空字段去表示这件事，会让每个消费方都多一层 `if player_id is None`，
    而漏掉任何一处都会变成 AttributeError 或者更糟：把文件挂到一个不存在的
    选手身上。

    所以单独一个类型，`require_agent` 拿不到它 —— 想用它的接口必须显式声明。
    """

    claim_id: int
    machine_id: str
    hostname: Optional[str]
    pair_code: Optional[str]


Principal = Union[AgentIdentity, MachineIdentity]


def _unauthorized(detail: str) -> "HTTPException":
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise _unauthorized("缺少 Bearer 凭据")
    raw = header[7:].strip()
    if not raw:
        raise _unauthorized("凭据为空")
    return raw


def require_agent(request: Request, ctx: AppContext = Depends(get_ctx)) -> AgentIdentity:
    """从 ``Authorization: Bearer <token>`` 解析 Agent 身份。

    服务端只存 token 的 SHA-256，用哈希做等值查询（索引命中），不存在明文比对。

    **未认领机器的临时凭据会被明确拒绝**（403 而不是 401），并且说明原因：
    如果这里返回 401，Agent 会以为凭据坏了，于是反复重新注册 ——
    刷注册限速、刷审计日志，而真正的问题（还没配对）反而看不见。
    """
    token_hash = hash_token(_bearer(request))
    with ctx.db.session() as session:
        row = session.execute(
            select(Agent, Player, Contest)
            .join(Player, Agent.player_id == Player.id)
            .join(Contest, Player.contest_id == Contest.id)
            .where(Agent.token_hash == token_hash)
        ).first()

        if row is None:
            claim = enrollment.find_claim_by_token(session, token_hash)
            if claim is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="这台机器还没配对到选手，请在管理界面完成配对",
                )
            raise _unauthorized("凭据无效")
        agent, player, contest = row
        if agent.revoked_at is not None:
            raise _unauthorized("该机器的凭据已被吊销")

        identity = AgentIdentity(
            agent_id=agent.id,
            player_id=player.id,
            player_no=player.player_no,
            player_name=player.name,
            contest_id=contest.id,
            contest_slug=contest.slug,
            contest_name=contest.name,
            contest_status=contest.status,
            machine_id=agent.machine_id,
            hostname=agent.hostname,
        )

    # 静态身份写入内存注册表，后续 tick 只更新动态部分
    ctx.registry.upsert_identity(
        agent_id=identity.agent_id,
        player_id=identity.player_id,
        contest_id=identity.contest_id,
        player_no=identity.player_no,
        player_name=identity.player_name,
        contest_slug=identity.contest_slug,
        machine_id=identity.machine_id,
        hostname=identity.hostname,
    )
    return identity


def require_principal(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> Principal:
    """接受**任意**有效凭据：已认领的 Agent，或一台还没配对的机器。

    只有 ``/tick`` 用它 —— 未配对的机器唯一要做的事就是定期问一句
    "认领了没有"。别的接口（上传、下载、事件）一律走 ``require_agent``：
    它们都需要选手身份，让未配对的机器进来没有任何意义，只会多出一堆
    需要判空的分支。
    """
    token_hash = hash_token(_bearer(request))
    with ctx.db.session() as session:
        row = session.execute(
            select(Agent, Player, Contest)
            .join(Player, Agent.player_id == Player.id)
            .join(Contest, Player.contest_id == Contest.id)
            .where(Agent.token_hash == token_hash)
        ).first()
        if row is not None:
            agent, player, contest = row
            if agent.revoked_at is not None:
                raise _unauthorized("该机器的凭据已被吊销")
            return AgentIdentity(
                agent_id=agent.id,
                player_id=player.id,
                player_no=player.player_no,
                player_name=player.name,
                contest_id=contest.id,
                contest_slug=contest.slug,
                contest_name=contest.name,
                contest_status=contest.status,
                machine_id=agent.machine_id,
                hostname=agent.hostname,
            )

        claim = enrollment.find_claim_by_token(session, token_hash)
        if claim is not None:
            return MachineIdentity(
                claim_id=claim.id,
                machine_id=claim.machine_id or "unknown",
                hostname=claim.hostname,
                pair_code=None,  # 明文只在注册那一刻存在，之后只有哈希
            )

    raise _unauthorized("凭据无效")
