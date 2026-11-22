"""机器 ↔ 名单绑定：查询、场次取人规则、增删改（§4.6）。

一台机器可以同时绑多份名单（每份名单里至多一个人）。于是"这台机器在场次 X 里是谁"
不再是一句"按考号找"，而要先回答"场次 X 用的是哪份名单"：

1. 场次指定了名单（``Contest.default_roster_id``）→ **只认这份名单里的那条绑定**。
   机器在这份名单里没绑 = 不属于这一场（哪怕它在别的名单里绑着一个同考号的人：
   各份名单的考号是各自编的，名单 A 的 1 号和名单 B 的 1 号可以是两个人）。
2. 场次没指定名单（选手是手工导入的）→ 退回按考号对应，但要两道护栏：
   * 这台机器有**不止一个**考号都在这一场里 → 不猜，算歧义
   * 这个考号还被**别的机器**（在任何名单里）绑着 → 不猜，算冲突
   两道护栏都是为了同一件事：不让两台机器往同一个 ``source/`` 目录静默写代码。

规则写成纯函数 :func:`pick_binding`，``/tick`` 的身份解析、选手页、「选手状态」
列表都走它 —— 两处各写一套，迟早一处说"能干活"、另一处说"不在这一场"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Agent, AgentBinding, Contest, EventLog, Player, Roster, RosterEntry, utcnow

__all__ = [
    "BindingConflict",
    "BoundPerson",
    "Pick",
    "agents_in_contest",
    "bind",
    "binding_in_roster",
    "contest_player_nos",
    "entry_holder",
    "holder_counts",
    "load_bindings",
    "pick_binding",
    "rebind",
    "unbind",
    "unpaired_clause",
]


@dataclass(frozen=True)
class BoundPerson:
    """一条绑定连同它指向的人。纯数据，便于纯函数与测试使用。"""

    agent_id: int
    roster_id: int
    entry_id: int
    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None
    roster_name: str = ""


@dataclass(frozen=True)
class Pick:
    """:func:`pick_binding` 的结果。``person`` 非空即"这台机器在这一场就是他"。

    ``code``：``ok`` / ``player_missing``（名单里绑着，但场次里没有这个考号的选手）/
    ``not_in_contest`` / ``ambiguous`` / ``contested``。
    """

    person: Optional[BoundPerson]
    code: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.person is not None and self.code == "ok"


def pick_binding(
    default_roster_id: Optional[int],
    bindings: Sequence[BoundPerson],
    contest_player_nos: Set[str],
    holders: Mapping[str, int],
) -> Pick:
    """这台机器（``bindings`` 是它的全部绑定）在某个场次里对应谁。

    ``contest_player_nos`` 至少要包含 ``bindings`` 里出现的那些考号在本场是否存在；
    ``holders`` 是"考号 → 有几台机器（含本机）在任何名单里绑着它"，只在退回按考号
    对应时用到。
    """
    if default_roster_id is not None:
        for person in bindings:
            if person.roster_id == default_roster_id:
                if person.player_no in contest_player_nos:
                    return Pick(person, "ok")
                return Pick(
                    person,
                    "player_missing",
                    "场次里没有编号 %s 的选手，请先把名单应用到场次" % person.player_no,
                )
        return Pick(None, "not_in_contest", "这台机器在场次所用的名单里没有绑定")

    matched = [person for person in bindings if person.player_no in contest_player_nos]
    numbers = sorted({person.player_no for person in matched})
    if not numbers:
        return Pick(None, "not_in_contest", "场次里没有这台机器绑着的考号")
    if len(numbers) > 1:
        return Pick(
            None,
            "ambiguous",
            "这台机器绑着的 %s 都在这一场里；请给场次指定名单" % "、".join(numbers),
        )
    if holders.get(numbers[0], 1) > 1:
        return Pick(
            None,
            "contested",
            "考号 %s 在多份名单里分别绑着不同的机器；请给场次指定名单" % numbers[0],
        )
    return Pick(matched[0], "ok")


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def unpaired_clause():
    """``WHERE`` 条件：一条绑定都没有的机器（= 待配对）。"""
    return ~Agent.bindings.any()


def load_bindings(
    session: Session, agent_ids: Optional[Iterable[int]] = None
) -> Dict[int, List[BoundPerson]]:
    """``agent_id → [BoundPerson]``，按绑定先后排序。``agent_ids`` 为空则取全部。"""
    stmt = (
        select(AgentBinding, RosterEntry, Roster.name)
        .join(RosterEntry, AgentBinding.roster_entry_id == RosterEntry.id)
        .join(Roster, AgentBinding.roster_id == Roster.id)
        .order_by(AgentBinding.agent_id, AgentBinding.id)
    )
    if agent_ids is not None:
        ids = list(agent_ids)
        if not ids:
            return {}
        stmt = stmt.where(AgentBinding.agent_id.in_(ids))
    result: Dict[int, List[BoundPerson]] = {}
    for binding, entry, roster_name in session.execute(stmt).all():
        result.setdefault(binding.agent_id, []).append(
            BoundPerson(
                agent_id=binding.agent_id,
                roster_id=binding.roster_id,
                entry_id=entry.id,
                player_no=entry.player_no,
                name=entry.name,
                seat=entry.seat,
                roster_name=roster_name or "",
            )
        )
    return result


def holder_counts(session: Session, player_nos: Iterable[str]) -> Dict[str, int]:
    """考号 → 有几台（未作废的）机器在任何名单里绑着它。"""
    numbers = sorted(set(player_nos))
    if not numbers:
        return {}
    rows = session.execute(
        select(RosterEntry.player_no, func.count(func.distinct(AgentBinding.agent_id)))
        .join(AgentBinding, AgentBinding.roster_entry_id == RosterEntry.id)
        .join(Agent, Agent.id == AgentBinding.agent_id)
        .where(RosterEntry.player_no.in_(numbers), Agent.revoked_at.is_(None))
        .group_by(RosterEntry.player_no)
    ).all()
    return {str(no): int(count) for no, count in rows}


def contest_player_nos(
    session: Session, contest_ids: Iterable[int], player_nos: Iterable[str]
) -> Dict[int, Dict[str, Player]]:
    """``contest_id → {考号: Player}``，只取给定考号。"""
    ids = list(contest_ids)
    numbers = sorted(set(player_nos))
    if not ids or not numbers:
        return {}
    result: Dict[int, Dict[str, Player]] = {}
    for player in session.execute(
        select(Player).where(Player.contest_id.in_(ids), Player.player_no.in_(numbers))
    ).scalars():
        result.setdefault(player.contest_id, {})[player.player_no] = player
    return result


def agents_in_contest(
    session: Session, contest: Contest
) -> List[Tuple[Agent, BoundPerson, Player]]:
    """这一场里"能落到某个选手上"的机器（未作废、未钉到别的场次），按机器 id 排序。

    与 ``/tick`` 用同一条 :func:`pick_binding`，所以「选手状态」页显示的归属
    就是机器下一轮心跳会用的归属。
    """
    players = {
        row.player_no: row
        for row in session.execute(
            select(Player).where(Player.contest_id == contest.id)
        ).scalars()
    }
    if not players:
        return []
    agents = {
        agent.id: agent
        for agent in session.execute(
            select(Agent)
            .where(
                Agent.revoked_at.is_(None),
                Agent.bindings.any(),
                (Agent.contest_id.is_(None)) | (Agent.contest_id == contest.id),
            )
            .order_by(Agent.id)
        ).scalars()
    }
    bindings = load_bindings(session, agents.keys())
    holders: Dict[str, int] = {}
    if contest.default_roster_id is None:
        for persons in bindings.values():
            for number in {person.player_no for person in persons}:
                holders[number] = holders.get(number, 0) + 1
    numbers = set(players)
    result: List[Tuple[Agent, BoundPerson, Player]] = []
    for agent_id, agent in agents.items():
        pick = pick_binding(
            contest.default_roster_id, bindings.get(agent_id, []), numbers, holders
        )
        if pick.ok and pick.person is not None:
            result.append((agent, pick.person, players[pick.person.player_no]))
    return result


def binding_in_roster(session: Session, agent_id: int, roster_id: int) -> Optional[AgentBinding]:
    return session.execute(
        select(AgentBinding).where(
            AgentBinding.agent_id == agent_id, AgentBinding.roster_id == roster_id
        )
    ).scalar_one_or_none()


def entry_holder(session: Session, entry_id: int) -> Optional[AgentBinding]:
    return session.execute(
        select(AgentBinding).where(AgentBinding.roster_entry_id == entry_id)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


class BindingConflict(Exception):
    """绑定被拒。带 HTTP 状态码与机器可读的 ``code``（调用方原样透出）。"""

    def __init__(self, detail: str, code: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


def _roster_label(entry: RosterEntry) -> str:
    return entry.roster.name if entry.roster is not None else "?"


def _describe(entry: RosterEntry) -> str:
    return "%s%s" % (entry.player_no, "（%s）" % entry.name if entry.name else "")


def bind(session: Session, agent: Agent, entry: RosterEntry, now=None) -> bool:
    """把机器绑到 ``entry``（它所在那份名单里）。返回是否新建了绑定（已绑同一人返回 False）。

    * 这台机器在这份名单里已经绑着**别人** → 409 ``machine_already_bound``（换人走「改派」）
    * 这个人已经有**别的机器** → 409 ``roster_entry_taken``
    * 不影响这台机器在其他名单里的绑定
    """
    now = now or utcnow()
    current = binding_in_roster(session, agent.id, entry.roster_id)
    if current is not None:
        if current.roster_entry_id == entry.id:
            return False
        held = session.get(RosterEntry, current.roster_entry_id)
        raise BindingConflict(
            "这台机器在名单「%s」里已经配对给 %s 了。要换人请用「改派」，或先解除这份名单的绑定。"
            % (_roster_label(entry), held.player_no if held else "?"),
            "machine_already_bound",
        )
    holder = entry_holder(session, entry.id)
    if holder is not None and holder.agent_id != agent.id:
        other = session.get(Agent, holder.agent_id)
        raise BindingConflict(
            "%s 已经有一台机器了（%s）。要换机器请先解除那一台的绑定，或用「改派」。"
            % (entry.player_no, (other.hostname or other.machine_id[:16]) if other else "?"),
            "roster_entry_taken",
        )

    session.add(
        AgentBinding(
            agent_id=agent.id, roster_entry_id=entry.id, roster_id=entry.roster_id, created_at=now
        )
    )
    if agent.claimed_at is None:
        agent.claimed_at = now
    # 配对码是一次性的：用过就作废，且只在绑定时用
    agent.pair_code_hash = None
    agent.pair_code_expires_at = None
    session.add(
        EventLog(
            level="info",
            category="bind",
            message="机器配对：名单「%s」%s ← %s @ %s"
            % (_roster_label(entry), _describe(entry), agent.hostname or "未知主机", agent.machine_id[:16]),
        )
    )
    session.flush()
    session.expire(agent, ["bindings"])
    return True


def rebind(session: Session, agent: Agent, entry: RosterEntry, now=None) -> Optional[RosterEntry]:
    """改派：把机器在 ``entry`` 那份名单里的绑定换成 ``entry``。返回原来绑的人（没有则 None）。

    目标必须还没有别的机器；其他名单里的绑定不动。
    """
    now = now or utcnow()
    holder = entry_holder(session, entry.id)
    if holder is not None and holder.agent_id != agent.id:
        other = session.get(Agent, holder.agent_id)
        raise BindingConflict(
            "%s 已经有一台机器了（%s）。要换机器请先解除那一台的绑定，或者用它改派。"
            % (entry.player_no, (other.hostname or other.machine_id[:16]) if other else "?"),
            "roster_entry_taken",
        )
    current = binding_in_roster(session, agent.id, entry.roster_id)
    previous = session.get(RosterEntry, current.roster_entry_id) if current is not None else None
    if current is not None:
        if current.roster_entry_id == entry.id:
            return previous
        current.roster_entry_id = entry.id
        current.created_at = now
    else:
        session.add(
            AgentBinding(
                agent_id=agent.id, roster_entry_id=entry.id, roster_id=entry.roster_id, created_at=now
            )
        )
    agent.claimed_at = now
    agent.pair_code_hash = None
    agent.pair_code_expires_at = None
    session.add(
        EventLog(
            level="warning",
            category="agent_rebind",
            message="机器改派：名单「%s」%s → %s（%s）"
            % (
                _roster_label(entry),
                previous.player_no if previous else "（未绑定）",
                entry.player_no,
                agent.machine_id[:16],
            ),
        )
    )
    session.flush()
    session.expire(agent, ["bindings"])
    return previous


def unbind(session: Session, agent: Agent, roster_id: Optional[int] = None) -> List[str]:
    """解除绑定：``roster_id`` 为空 = 全部名单。返回被解除的"名单 · 考号"。

    一条都不剩时机器回到待配对：场次指定、配对时间、旧配对码一并清掉。
    """
    stmt = select(AgentBinding).where(AgentBinding.agent_id == agent.id)
    if roster_id is not None:
        stmt = stmt.where(AgentBinding.roster_id == roster_id)
    rows = list(session.execute(stmt).scalars())
    removed: List[str] = []
    for row in rows:
        entry = session.get(RosterEntry, row.roster_entry_id)
        roster = session.get(Roster, row.roster_id)
        removed.append(
            "%s · %s" % (roster.name if roster else "?", entry.player_no if entry else "?")
        )
        session.delete(row)
    session.flush()
    session.expire(agent, ["bindings"])
    if not agent.bindings:
        agent.contest_id = None
        agent.claimed_at = None
        # 老配对码早就作废了，让下一轮注册/心跳重新生成一个
        agent.pair_code_hash = None
        agent.pair_code_expires_at = None
    if removed:
        session.add(
            EventLog(
                level="warning",
                category="agent_unbind",
                message="解除机器配对：%s（%s）" % ("、".join(removed), agent.machine_id[:16]),
            )
        )
    session.flush()
    return removed
