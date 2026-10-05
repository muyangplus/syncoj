"""FastAPI 依赖：请求上下文与 Agent 身份认证。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from ..context import AppContext
from ..models import Agent, Contest, Player
from ..security import hash_token

__all__ = ["get_ctx", "AgentIdentity", "require_agent"]


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


def _unauthorized(detail: str) -> "HTTPException":
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_agent(request: Request, ctx: AppContext = Depends(get_ctx)) -> AgentIdentity:
    """从 ``Authorization: Bearer <token>`` 解析 Agent 身份。

    服务端只存 token 的 SHA-256，用哈希做等值查询（索引命中），不存在明文比对。
    """
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise _unauthorized("缺少 Bearer 凭据")
    raw = header[7:].strip()
    if not raw:
        raise _unauthorized("凭据为空")

    token_hash = hash_token(raw)
    with ctx.db.session() as session:
        row = session.execute(
            select(Agent, Player, Contest)
            .join(Player, Agent.player_id == Player.id)
            .join(Contest, Player.contest_id == Contest.id)
            .where(Agent.token_hash == token_hash)
        ).first()

        if row is None:
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
