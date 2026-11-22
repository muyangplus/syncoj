"""按姓名在名单之间找"同一个人"（智能匹配，§4.6）。

一台机器绑在名单 A 的「张三」上，名单 B 里也有「张三」—— 多半是同一个人，
教师不该再对着 50 台机器把配对码输一遍。但"多半"不等于"一定"，所以这里只**建议**，
应用由教师确认；规则宁严勿宽，不唯一就不建议：

* 姓名比较：去首尾空白、NFKC 统一全半角（全角空格、全角字母数字都折成半角）后**完全相等**。
  空名永不匹配。
* 同名多人：先用座位号收窄，再用考号收窄；还不唯一就不建议。
* 已被别的机器占着的人不建议；本机在目标名单里已有绑定也不建议。
* 一台机器在多份名单里绑着的人各自指向目标名单里的**不同**人 → 冲突，不建议。

全部是纯函数（只吃数据类），接口层负责从库里取数。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Candidate",
    "MachineSuggestion",
    "Person",
    "RosterMatch",
    "match_roster",
    "normalize_name",
    "suggest_for_machine",
]


def normalize_name(value: Optional[str]) -> Optional[str]:
    """姓名比较用的规范形式；空（含只有空白）返回 ``None``。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    return text or None


def _norm_key(value: Optional[str]) -> Optional[str]:
    """座位号、考号的规范形式（同姓名规则，再忽略大小写）。"""
    text = normalize_name(value)
    return text.casefold() if text else None


@dataclass(frozen=True)
class Person:
    """名单里的一个人。``holder`` 是绑着他的机器 id（没有则 None）。"""

    entry_id: int
    roster_id: int
    player_no: str
    name: Optional[str] = None
    seat: Optional[str] = None
    holder: Optional[int] = None


@dataclass(frozen=True)
class Candidate:
    """一条建议：把 ``agent_id`` 绑到 ``target``，依据是它绑着的 ``source``。"""

    agent_id: int
    target: Person
    source: Person


@dataclass
class MachineSuggestion:
    """单台机器在某份名单里的匹配结论：``candidate`` 与 ``reason`` 二选一。"""

    roster_id: int
    candidate: Optional[Candidate] = None
    reason: Optional[str] = None


def _narrow(source: Person, pool: Sequence[Person]) -> List[Person]:
    """同名多人时用座位号、再用考号收窄。"""
    if len(pool) <= 1:
        return list(pool)
    narrowed = list(pool)
    seat = _norm_key(source.seat)
    if seat is not None:
        by_seat = [p for p in narrowed if _norm_key(p.seat) == seat]
        if by_seat:
            narrowed = by_seat
    if len(narrowed) > 1:
        number = _norm_key(source.player_no)
        by_no = [p for p in narrowed if _norm_key(p.player_no) == number]
        if by_no:
            narrowed = by_no
    return narrowed


def _suggest_in_roster(
    agent_id: int, sources: Sequence[Person], roster_people: Sequence[Person]
) -> MachineSuggestion:
    roster_id = roster_people[0].roster_id if roster_people else -1
    by_name: Dict[str, List[Person]] = {}
    for person in roster_people:
        key = normalize_name(person.name)
        if key is not None:
            by_name.setdefault(key, []).append(person)

    picked: Dict[int, Tuple[Person, Person]] = {}
    reasons: List[str] = []
    for source in sources:
        key = normalize_name(source.name)
        if key is None:
            continue
        pool = by_name.get(key, [])
        if not pool:
            continue
        narrowed = _narrow(source, pool)
        if len(narrowed) != 1:
            reasons.append("同名 %d 人，座位号与考号也区分不了" % len(pool))
            continue
        target = narrowed[0]
        if target.holder is not None and target.holder != agent_id:
            reasons.append("%s 已绑定其他机器" % target.player_no)
            continue
        picked.setdefault(target.entry_id, (target, source))

    if len(picked) > 1:
        return MachineSuggestion(
            roster_id, reason="本机绑着的几个人分别对应 %s" % "、".join(
                sorted(t.player_no for t, _ in picked.values())
            )
        )
    if picked:
        target, source = next(iter(picked.values()))
        return MachineSuggestion(roster_id, candidate=Candidate(agent_id, target, source))
    return MachineSuggestion(roster_id, reason=reasons[0] if reasons else "没有同名的人")


def suggest_for_machine(
    agent_id: int,
    bound: Sequence[Person],
    rosters: Dict[int, Sequence[Person]],
) -> List[Candidate]:
    """一台机器在**其他名单**里的同名建议。

    ``bound`` 是它现在绑着的人（各名单至多一人）；``rosters`` 是"名单 id → 全部条目"，
    已经绑着本机的名单会被跳过。
    """
    bound_rosters = {person.roster_id for person in bound}
    result: List[Candidate] = []
    for roster_id in sorted(rosters):
        if roster_id in bound_rosters:
            continue
        outcome = _suggest_in_roster(agent_id, bound, rosters[roster_id])
        if outcome.candidate is not None:
            result.append(outcome.candidate)
    return result


@dataclass
class RosterMatch:
    """名单级「按姓名匹配机器」的预览。"""

    pairs: List[Candidate] = field(default_factory=list)
    #: (条目, 原因)：多台机器指向同一个人、或机器侧有冲突
    conflicts: List[Tuple[Person, str]] = field(default_factory=list)
    #: (条目, 原因)：已绑定、空名、没有同名机器……
    skipped: List[Tuple[Person, str]] = field(default_factory=list)


def match_roster(
    target: Sequence[Person],
    machines: Dict[int, Sequence[Person]],
) -> RosterMatch:
    """对名单 ``target`` 的每个未绑定的人找"恰好一台"在别的名单里绑着同名者的机器。

    ``machines`` 是"机器 id → 它绑着的人（全部名单）"；在目标名单里已有绑定的机器
    不参与。规则与 :func:`suggest_for_machine` 同一套（每台机器先算出它在目标名单里
    指向谁，再反过来看每个人被几台机器指向）。
    """
    report = RosterMatch()
    if not target:
        return report
    roster_id = target[0].roster_id
    claims: Dict[int, List[Candidate]] = {}
    machine_notes: Dict[int, str] = {}
    for agent_id in sorted(machines):
        bound = list(machines[agent_id])
        if any(person.roster_id == roster_id for person in bound):
            continue
        outcome = _suggest_in_roster(agent_id, bound, target)
        if outcome.candidate is not None:
            claims.setdefault(outcome.candidate.target.entry_id, []).append(outcome.candidate)
        elif outcome.reason and outcome.reason.startswith("同名"):
            for person in target:
                for source in bound:
                    if normalize_name(person.name) and normalize_name(person.name) == normalize_name(source.name):
                        machine_notes.setdefault(person.entry_id, outcome.reason)

    for person in target:
        if person.holder is not None:
            report.skipped.append((person, "已绑定"))
            continue
        if normalize_name(person.name) is None:
            report.skipped.append((person, "姓名为空"))
            continue
        found = claims.get(person.entry_id, [])
        if len(found) == 1:
            report.pairs.append(found[0])
        elif len(found) > 1:
            report.conflicts.append(
                (person, "%d 台机器都绑着同名的人" % len(found))
            )
        else:
            report.skipped.append(
                (person, machine_notes.get(person.entry_id, "没有绑着同名者的机器"))
            )
    return report

