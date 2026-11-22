"""智能匹配的纯函数（services/name_match.py）：同名、重名、空名、已占用、全半角空格。"""

from __future__ import annotations

from syncoj_server.services.name_match import (
    Person,
    match_roster,
    normalize_name,
    suggest_for_machine,
)


def p(entry_id, roster_id, player_no, name=None, seat=None, holder=None) -> Person:
    return Person(entry_id, roster_id, player_no, name, seat, holder)


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #


def test_姓名去首尾空白并统一全半角() -> None:
    assert normalize_name("  张三 ") == "张三"
    assert normalize_name("　张三　") == "张三", "全角空格也是空白"
    assert normalize_name("ＡＢＣ１") == "ABC1"
    assert normalize_name("张 三") == "张 三", "中间的空白不动：只比首尾"


def test_空名永远不算名字() -> None:
    assert normalize_name(None) is None
    assert normalize_name("") is None
    assert normalize_name(" 　 ") is None


# --------------------------------------------------------------------------- #
# 单台机器
# --------------------------------------------------------------------------- #


def test_同名唯一时建议() -> None:
    bound = [p(1, 1, "S001", "张三", holder=9)]
    rosters = {1: [bound[0]], 2: [p(21, 2, "K07", "张三"), p(22, 2, "K08", "李四")]}

    result = suggest_for_machine(9, bound, rosters)

    assert [(c.target.entry_id, c.source.entry_id) for c in result] == [(21, 1)]


def test_全角空格与全角字母也算同名() -> None:
    bound = [p(1, 1, "S001", "Ｔｏｍ　", holder=9)]
    rosters = {2: [p(21, 2, "K07", " Tom")]}

    assert [c.target.entry_id for c in suggest_for_machine(9, bound, rosters)] == [21]


def test_重名时用座位号区分() -> None:
    bound = [p(1, 1, "S001", "张三", seat="A3", holder=9)]
    rosters = {2: [p(21, 2, "K07", "张三", seat="A1"), p(22, 2, "K08", "张三", seat="Ａ3")]}

    assert [c.target.entry_id for c in suggest_for_machine(9, bound, rosters)] == [22]


def test_重名时座位号不够再用考号() -> None:
    bound = [p(1, 1, "S001", "张三", holder=9)]
    rosters = {2: [p(21, 2, "S001", "张三"), p(22, 2, "S002", "张三")]}

    assert [c.target.entry_id for c in suggest_for_machine(9, bound, rosters)] == [21]


def test_重名且区分不了时不建议() -> None:
    bound = [p(1, 1, "S001", "张三", holder=9)]
    rosters = {2: [p(21, 2, "K07", "张三"), p(22, 2, "K08", "张三")]}

    assert suggest_for_machine(9, bound, rosters) == []


def test_空名不建议() -> None:
    bound = [p(1, 1, "S001", "  ", holder=9)]
    rosters = {2: [p(21, 2, "K07", "  ")]}

    assert suggest_for_machine(9, bound, rosters) == []


def test_对方已有别的机器时不建议() -> None:
    bound = [p(1, 1, "S001", "张三", holder=9)]
    rosters = {2: [p(21, 2, "K07", "张三", holder=5)]}

    assert suggest_for_machine(9, bound, rosters) == []


def test_本机已绑的名单不再建议() -> None:
    bound = [p(1, 1, "S001", "张三", holder=9), p(21, 2, "K01", "别人", holder=9)]
    rosters = {2: [bound[1], p(22, 2, "K07", "张三")]}

    assert suggest_for_machine(9, bound, rosters) == []


def test_本机绑着的两个人指向目标名单里的不同人时不建议() -> None:
    bound = [p(1, 1, "S001", "张三", holder=9), p(11, 3, "T01", "李四", holder=9)]
    rosters = {2: [p(21, 2, "K07", "张三"), p(22, 2, "K08", "李四")]}

    assert suggest_for_machine(9, bound, rosters) == []


# --------------------------------------------------------------------------- #
# 名单级
# --------------------------------------------------------------------------- #


def test_名单级匹配_成对_冲突_跳过() -> None:
    target = [
        p(21, 2, "K01", "张三"),
        p(22, 2, "K02", "李四"),
        p(23, 2, "K03", "王五", holder=7),
        p(24, 2, "K04", ""),
        p(25, 2, "K05", "赵六"),
        p(26, 2, "K06", "钱七"),
        p(27, 2, "K07", "钱七"),
    ]
    machines = {
        1: [p(1, 1, "S001", "张三", holder=1)],
        2: [p(2, 1, "S002", "李四", holder=2)],
        3: [p(3, 3, "T001", "李四", holder=3)],
        7: [p(23, 2, "K03", "王五", holder=7)],
        8: [p(8, 1, "S008", "钱七", holder=8)],
    }

    report = match_roster(target, machines)

    assert [(c.agent_id, c.target.entry_id) for c in report.pairs] == [(1, 21)]
    assert [(person.entry_id, reason) for person, reason in report.conflicts] == [
        (22, "2 台机器都绑着同名的人")
    ]
    skipped = {person.entry_id: reason for person, reason in report.skipped}
    assert skipped[23] == "已绑定"
    assert skipped[24] == "姓名为空"
    assert skipped[25] == "没有绑着同名者的机器"
    assert skipped[26].startswith("同名") and skipped[27].startswith("同名")


def test_名单级匹配_已在目标名单里有绑定的机器不参与() -> None:
    target = [p(21, 2, "K01", "张三"), p(22, 2, "K02", "李四", holder=1)]
    machines = {1: [p(1, 1, "S001", "张三", holder=1), p(22, 2, "K02", "李四", holder=1)]}

    report = match_roster(target, machines)

    assert report.pairs == []


def test_名单级匹配_空名单() -> None:
    report = match_roster([], {1: [p(1, 1, "S001", "张三", holder=1)]})
    assert (report.pairs, report.conflicts, report.skipped) == ([], [], [])
