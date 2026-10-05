"""名单库：把"名单"复制成"参赛者"。

为什么是复制而不是引用
----------------------
``Roster`` 是"这个班/这个考点有哪些人"的事实，跨场次复用；
``Player`` 是"这场比赛谁在参赛"，成绩、代码、下发全挂在它身上。

两者的关系刻意做成**一次性复制**。让场次直接引用名单看起来更优雅，但会让
"改一下名单"顺带改掉历史场次的参赛者 —— 那时成绩矩阵的列、代码目录、
下发目标、既有成绩的外键全都会跟着动。教师想要的从来不是这个效果。

所以这里只做一件事：``apply_roster`` 把名单条目合并进选手表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import JudgeRun, Player, Roster, RosterEntry, SourceFile

__all__ = ["ApplyReport", "apply_roster"]


@dataclass
class ApplyReport:
    """应用名单的结果。

    刻意把 ``kept`` 与 ``protected`` 分开报：

    * ``kept`` 是"名单里没有，但场次里本来就有的选手" —— 正常情况，比如
      临时加的选手。报出来让教师知道"我没动他们"。
    * ``protected`` 是**本该被删掉、但因为有提交/成绩而被拦住**的选手。
      这是需要教师看一眼的东西 —— 有人已经把代码交上来了，
      自动清掉是不可逆的事故。
    """

    created: int = 0
    updated: int = 0
    kept: int = 0
    pruned: int = 0
    protected: List[str] = field(default_factory=list)


def _players_with_evidence(session: Session, contest_id: int) -> Set[int]:
    """挑出"已经有东西"的选手。

    有代码或有成绩都算。判据取的是**存在性**而不是数量：哪怕只剩一条已删除的
    文件记录，也说明这台机器真的连上过、真的有人干过活，不该被名单整理顺手抹掉。
    """
    ids: Set[int] = set()

    for (player_id,) in session.execute(
        select(SourceFile.player_id)
        .join(Player, SourceFile.player_id == Player.id)
        .where(Player.contest_id == contest_id)
        .distinct()
    ):
        ids.add(player_id)

    for (player_id,) in session.execute(
        select(JudgeRun.player_id).where(JudgeRun.contest_id == contest_id).distinct()
    ):
        ids.add(player_id)

    return ids


def apply_roster(
    session: Session,
    contest_id: int,
    roster: Optional[Roster],
    entries: Sequence[RosterEntry],
    prune: bool = False,
) -> ApplyReport:
    """把名单合并进场次的选手表。

    合并规则：
      * 名单里有、场次里没有 → 新建
      * 两边都有 → 用名单里的姓名/座位/分组覆盖
      * 场次里有、名单里没有 → 默认保留；``prune=True`` 时才删，
        而且**有代码或成绩的一律不删**

    ``prune`` 的默认值是 False 而且保护规则不可关闭：名单是用来"补人"的，
    不是用来"清场"的。真要清场，教师应该看到"这些人有提交，删不掉"，
    而不是第二天发现某个学生的代码不见了。
    """
    report = ApplyReport()

    existing: Dict[str, Player] = {
        row.player_no: row
        for row in session.execute(
            select(Player).where(Player.contest_id == contest_id)
        ).scalars()
    }

    wanted: Set[str] = set()
    for entry in entries:
        wanted.add(entry.player_no)
        player = existing.get(entry.player_no)
        if player is None:
            player = Player(contest_id=contest_id, player_no=entry.player_no)
            session.add(player)
            existing[entry.player_no] = player
            report.created += 1
        else:
            report.updated += 1
        player.name = entry.name
        player.seat = entry.seat
        player.group_name = entry.group_name

    # 必须先 flush：新增的选手还没有主键，下面按主键做的保护判断会漏掉它们
    session.flush()

    leftovers = [row for player_no, row in existing.items() if player_no not in wanted]
    if not prune:
        report.kept = len(leftovers)
        return report

    protected_ids = _players_with_evidence(session, contest_id)
    for player in leftovers:
        if player.id in protected_ids:
            report.protected.append(player.player_no)
            continue
        session.delete(player)
        report.pruned += 1

    report.kept = len(leftovers) - report.pruned
    return report
