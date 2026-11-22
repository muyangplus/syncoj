"""管理端：一台机器绑多份名单（§4.6）—— 已配对机器列表、同名建议、名单级「按姓名匹配机器」。

配对 / 改派 / 解绑三个老接口仍在 ``api/admin.py``（路径不变）；这里只放新加的几条，
路径同样在 ``/api/v1/admin`` 下。匹配规则是 ``services/name_match.py`` 里的纯函数，
这里只负责从库里取数、把结论翻成接口形状。
"""

from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..context import AppContext
from ..errors import ApiError
from ..models import Agent, EventLog, Roster, RosterEntry
from ..schemas import (
    BindingSuggestionOut,
    BoundMachineOut,
    Page,
    RosterMatchApplyIn,
    RosterMatchApplyOut,
    RosterMatchPairOut,
    RosterMatchPreviewOut,
    RosterMatchSkipOut,
    page_of,
    slice_page,
)
from ..services import bindings as agent_bindings
from ..services import name_match
from .admin import AdminIdentity, _binding_out, _iso, require_admin
from .deps import PageParams, get_ctx

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #


def _people_by_roster(session) -> Dict[int, List[name_match.Person]]:
    """全部名单条目，带上"被哪台机器绑着"。"""
    holders = {
        person.entry_id: agent_id
        for agent_id, persons in agent_bindings.load_bindings(session).items()
        for person in persons
    }
    result: Dict[int, List[name_match.Person]] = {}
    for entry in session.execute(
        select(RosterEntry).order_by(RosterEntry.roster_id, RosterEntry.player_no)
    ).scalars():
        result.setdefault(entry.roster_id, []).append(
            name_match.Person(
                entry_id=entry.id,
                roster_id=entry.roster_id,
                player_no=entry.player_no,
                name=entry.name,
                seat=entry.seat,
                holder=holders.get(entry.id),
            )
        )
    return result


def _as_person(person: agent_bindings.BoundPerson) -> name_match.Person:
    return name_match.Person(
        entry_id=person.entry_id,
        roster_id=person.roster_id,
        player_no=person.player_no,
        name=person.name,
        seat=person.seat,
        holder=person.agent_id,
    )


def _roster_names(session) -> Dict[int, str]:
    return {row.id: row.name for row in session.execute(select(Roster)).scalars()}


def _source_label(source: name_match.Person, roster_names: Dict[int, str]) -> str:
    return "%s · %s%s" % (
        roster_names.get(source.roster_id, "?"),
        source.player_no,
        " %s" % source.name if source.name else "",
    )


def machine_suggestions(session, agent_id: int) -> List[BindingSuggestionOut]:
    """这台机器在**其他名单**里的同名建议（配对回执与建议接口共用）。"""
    bound = [
        _as_person(person)
        for person in agent_bindings.load_bindings(session, [agent_id]).get(agent_id, [])
    ]
    if not bound:
        return []
    roster_names = _roster_names(session)
    result = []
    for candidate in name_match.suggest_for_machine(agent_id, bound, _people_by_roster(session)):
        target = candidate.target
        result.append(
            BindingSuggestionOut(
                agent_id=agent_id,
                roster_id=target.roster_id,
                roster_name=roster_names.get(target.roster_id, ""),
                roster_entry_id=target.entry_id,
                player_no=target.player_no,
                name=target.name,
                seat=target.seat,
                source=_source_label(candidate.source, roster_names),
            )
        )
    return result


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


@router.get("/machines/bound", response_model=Page[BoundMachineOut])
def list_bound_machines(
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[BoundMachineOut]:
    """已配对的机器（至少绑了一份名单），每台带上全部绑定。按主机名排序。"""
    with ctx.db.session() as session:
        condition = (Agent.revoked_at.is_(None), Agent.bindings.any())
        total = session.execute(select(func.count(Agent.id)).where(*condition)).scalar_one()
        agents = list(
            session.execute(
                select(Agent)
                .where(*condition)
                .order_by(Agent.hostname, Agent.id)
                .limit(page.limit)
                .offset(page.offset)
            ).scalars()
        )
        persons = agent_bindings.load_bindings(session, [agent.id for agent in agents])
        items = [
            BoundMachineOut(
                id=agent.id,
                machine_id=agent.machine_id,
                hostname=agent.hostname,
                agent_version=agent.agent_version,
                contest_id=agent.contest_id,
                last_seen_at=_iso(agent.last_seen_at),
                bindings=[_binding_out(person) for person in persons.get(agent.id, [])],
            )
            for agent in agents
        ]
    return page_of(items, total, page)


@router.get("/agents/{agent_id}/binding-suggestions", response_model=Page[BindingSuggestionOut])
def binding_suggestions(
    agent_id: int,
    page: PageParams = Depends(),
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> Page[BindingSuggestionOut]:
    """这台机器在其他名单里的同名建议：姓名（去首尾空白、全半角统一）完全相等，
    同名多人时用座位号 / 考号收窄，仍不唯一、或对方已有机器就不建议。"""
    with ctx.db.session() as session:
        if session.get(Agent, agent_id) is None:
            raise ApiError(404, "not_found", "这台机器不在台账里")
        items = machine_suggestions(session, agent_id)
    return slice_page(items, page)


def _skip_out(person: name_match.Person, reason: str) -> RosterMatchSkipOut:
    return RosterMatchSkipOut(
        roster_entry_id=person.entry_id, player_no=person.player_no, name=person.name, reason=reason
    )


@router.post("/rosters/{roster_id}/match-machines/preview", response_model=RosterMatchPreviewOut)
def preview_roster_match(
    roster_id: int,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterMatchPreviewOut:
    """「按姓名匹配机器」预览：这份名单里每个未绑定的人，若恰好有一台机器在别的名单里
    绑着同名的人、且那台机器在这份名单里还没绑，就列成一对。不写库。"""
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise ApiError(404, "not_found", "名单不存在")
        roster_names = _roster_names(session)
        target = _people_by_roster(session).get(roster_id, [])
        machines = {
            agent_id: [_as_person(person) for person in persons]
            for agent_id, persons in agent_bindings.load_bindings(session).items()
        }
        agents = {
            agent.id: agent
            for agent in session.execute(
                select(Agent).where(Agent.id.in_(list(machines)), Agent.revoked_at.is_(None))
            ).scalars()
        } if machines else {}
        machines = {key: value for key, value in machines.items() if key in agents}
        report = name_match.match_roster(target, machines)
        pairs = [
            RosterMatchPairOut(
                agent_id=pair.agent_id,
                hostname=agents[pair.agent_id].hostname,
                machine_id=agents[pair.agent_id].machine_id,
                roster_entry_id=pair.target.entry_id,
                player_no=pair.target.player_no,
                name=pair.target.name,
                source=_source_label(pair.source, roster_names),
            )
            for pair in report.pairs
        ]
        return RosterMatchPreviewOut(
            roster_id=roster.id,
            roster_name=roster.name,
            pairs=pairs,
            conflicts=[_skip_out(person, reason) for person, reason in report.conflicts],
            skipped=[_skip_out(person, reason) for person, reason in report.skipped],
        )


@router.post("/rosters/{roster_id}/match-machines/apply", response_model=RosterMatchApplyOut)
def apply_roster_match(
    roster_id: int,
    payload: RosterMatchApplyIn,
    ctx: AppContext = Depends(get_ctx),
    admin: AdminIdentity = Depends(require_admin),
) -> RosterMatchApplyOut:
    """应用预览里确认过的对。每一对都按配对规则重新校验（一机在一份名单里一人、
    一人一机），不成的列进 ``failed`` 并说明原因，其余照常绑定。"""
    result = RosterMatchApplyOut()
    touched: List[int] = []
    with ctx.db.session() as session:
        roster = session.get(Roster, roster_id)
        if roster is None:
            raise ApiError(404, "not_found", "名单不存在")
        for pair in payload.pairs:
            entry = session.get(RosterEntry, pair.roster_entry_id)
            agent = session.get(Agent, pair.agent_id)
            if entry is None or entry.roster_id != roster_id:
                result.failed.append(
                    RosterMatchSkipOut(
                        roster_entry_id=pair.roster_entry_id, player_no="?", reason="不是这份名单里的人"
                    )
                )
                continue
            if agent is None or agent.revoked_at is not None:
                result.failed.append(
                    RosterMatchSkipOut(
                        roster_entry_id=entry.id,
                        player_no=entry.player_no,
                        name=entry.name,
                        reason="机器不存在",
                    )
                )
                continue
            try:
                if agent_bindings.bind(session, agent, entry):
                    result.bound += 1
                    touched.append(agent.id)
            except agent_bindings.BindingConflict as exc:
                result.failed.append(
                    RosterMatchSkipOut(
                        roster_entry_id=entry.id,
                        player_no=entry.player_no,
                        name=entry.name,
                        reason=exc.detail,
                    )
                )
        if result.bound:
            session.add(
                EventLog(
                    level="info",
                    category="bind",
                    message="按姓名匹配机器：名单「%s」绑定 %d 台" % (roster.name, result.bound),
                )
            )
    for agent_id in touched:
        ctx.registry.forget(agent_id)
        ctx.pair_codes.forget(agent_id)
    return result
