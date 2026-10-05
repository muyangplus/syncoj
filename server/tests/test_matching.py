"""代码文件归题测试。

题目靠一组 glob 模式认领回收上来的文件。这里定死的是**语义**：``*`` 不跨
``/``、``**`` 跨、第一个命中的题目胜出。

为什么要谨慎到专门测一遍：默认约定（``桌面/<准考证号>/<题目名>/<题目名>.cpp``）
只是**出厂默认值**。教师一旦改模式，这套匹配就是"哪个文件算哪道题"的唯一
依据 —— 匹配错了，成绩矩阵会把 A 题的分算到 B 题头上，而且看起来毫无异常。
"""

from __future__ import annotations

import pytest

from syncoj_server.services import matching
from syncoj_server.services.matching import (
    PatternError,
    ProblemRule,
    match_problem,
    validate_pattern,
)


def rules(*items):
    """``("p1", ["p1/**"]), ...`` -> matcher 需要的形状。

    允许 ``("p1", ["p1/**"], "签到题")`` 这样带上标题。
    """
    out = []
    for item in items:
        ident, patterns = item[0], item[1]
        title = item[2] if len(item) > 2 else None
        out.append(ProblemRule(ident=ident, patterns=list(patterns), title=title))
    return out


# --------------------------------------------------------------------------- #
# glob 语义
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # `*` 不跨斜杠 —— 这是与 fnmatch 分道扬镳的地方
        ("p1/*.cpp", "p1/a.cpp", True),
        ("p1/*.cpp", "p1/sub/a.cpp", False),
        ("p1/*", "p1/sub/a.cpp", False),
        # `**` 跨
        ("p1/**", "p1/a.cpp", True),
        ("p1/**", "p1/sub/deep/a.cpp", True),
        ("p1/**", "p2/a.cpp", False),
        # `**/` 也得能匹配零层目录，否则 `**/*.cpp` 会漏掉根目录下的文件
        ("**/*.cpp", "a.cpp", True),
        ("**/*.cpp", "x/y/a.cpp", True),
        ("**/*.cpp", "x/y/a.hpp", False),
        # `?` 单字符，且不跨斜杠
        ("p?/a.cpp", "p1/a.cpp", True),
        ("p?/a.cpp", "p12/a.cpp", False),
        ("p?/a.cpp", "p//a.cpp", False),
        # 字符类
        ("p[12]/a.cpp", "p1/a.cpp", True),
        ("p[12]/a.cpp", "p3/a.cpp", False),
        ("p[!12]/a.cpp", "p3/a.cpp", True),
        # 整个路径必须匹配完，不能只匹配前缀
        ("p1", "p1/a.cpp", False),
        ("p1/**", "p1x/a.cpp", False),
        # 中文路径（考点目录名常是中文）
        ("签到题/**", "签到题/签到题.cpp", True),
        # 点号是字面量，不是正则的"任意字符"
        ("a.cpp", "axcpp", False),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert bool(matching.compile_pattern(pattern).match(path)) is expected


def test_star_does_not_cross_slash_is_the_whole_point() -> None:
    """一句话说明为什么不用 ``fnmatch``。

    ``fnmatch`` 的 ``*`` 会吞掉 ``/``，于是 ``p1/*`` 能命中 ``p1/sub/a.cpp``，
    教师就没法表达"只收这个目录下第一层的文件"了。
    """
    import fnmatch

    assert fnmatch.fnmatch("p1/sub/a.cpp", "p1/*") is True, "fnmatch 确实会跨斜杠"
    assert bool(matching.compile_pattern("p1/*").match("p1/sub/a.cpp")) is False


# --------------------------------------------------------------------------- #
# 归题顺序
# --------------------------------------------------------------------------- #


def test_first_match_wins() -> None:
    """顺序由教师控制，第一个命中的胜出 —— 不做"最具体者优先"这种隐式判断。"""
    both = rules(("broad", ["**"]), ("narrow", ["p1/**"]))
    assert match_problem("p1/a.cpp", both) == "broad"

    swapped = rules(("narrow", ["p1/**"]), ("broad", ["**"]))
    assert match_problem("p1/a.cpp", swapped) == "narrow"


def test_unmatched_returns_none() -> None:
    """没命中就是没命中。返回 None 而不是"兜底归给第一题" —— 后者会造出假归属。"""
    assert match_problem("random/a.cpp", rules(("p1", ["p1/**"]))) is None
    assert match_problem("", rules(("p1", ["p1/**"]))) is None
    assert match_problem("a.cpp", []) is None


def test_multiple_patterns_on_one_problem() -> None:
    """一道题可以配多条模式 —— 选手交在 `p1/` 或 `p1/src/` 都算。"""
    rule = rules(("p1", ["p1/*.cpp", "p1/src/**"]))
    assert match_problem("p1/a.cpp", rule) == "p1"
    assert match_problem("p1/src/a.cpp", rule) == "p1"
    assert match_problem("p1/other/a.cpp", rule) is None


def test_ident_placeholder_follows_rename() -> None:
    """``{ident}`` 是"跟着题目标识走"，不是"存了当时那个名字"。

    题目从 ``p1`` 改名成 ``签到题`` 之后，默认模式必须跟着认新目录，
    否则教师会以为改名把回收弄坏了。
    """
    rule = rules(("签到题", ["{ident}/**"]))
    assert match_problem("签到题/a.cpp", rule) == "签到题"
    assert match_problem("p1/a.cpp", rule) is None


def test_title_placeholder_uses_the_title() -> None:
    """``{title}`` 展开成标题 —— 题目目录按题面命名（``签到题/``）时用得上。"""
    rule = rules(("A", ["{title}/**"], "签到题"))
    assert match_problem("签到题/a.cpp", rule) == "A"
    assert match_problem("A/a.cpp", rule) is None


def test_title_placeholder_falls_back_to_ident() -> None:
    """标题留空时 ``{title}`` 退回标识。

    否则 ``{title}/**`` 会展开成 ``/**``（匹配一切）或匹配不到任何东西 ——
    两种都是教师在界面上看不出来的坑。
    """
    rule = rules(("p1", ["{title}/**"], None))
    assert match_problem("p1/a.cpp", rule) == "p1"
    assert match_problem("other/a.cpp", rule) is None

    empty = rules(("p1", ["{title}/**"], ""))
    assert match_problem("p1/a.cpp", empty) == "p1"


def test_title_rename_follows_through() -> None:
    """改标题之后 ``{title}`` 跟着走 —— 和 ``{ident}`` 一样是活占位符。"""
    before = rules(("A", ["{title}/**"], "签到题"))
    assert match_problem("签到题/a.cpp", before) == "A"

    after = rules(("A", ["{title}/**"], "简单题"))
    assert match_problem("简单题/a.cpp", after) == "A"
    assert match_problem("签到题/a.cpp", after) is None


def test_expand_pattern_is_idempotent_for_resolved_names() -> None:
    """展开过的模式再展开一次不该出问题（模式里没有占位符时是恒等映射）。"""
    assert matching.expand_pattern("p1/**", "p1", "签到题") == "p1/**"
    assert matching.expand_pattern("{ident}/{title}", "p1", "签到题") == "p1/签到题"


def test_group_by_problem() -> None:
    grouped = matching.group_by_problem(
        ["p1/a.cpp", "p2/b.cpp", "loose/c.cpp"],
        rules(("p1", ["p1/**"]), ("p2", ["p2/**"])),
    )
    assert grouped["p1"] == ["p1/a.cpp"]
    assert grouped["p2"] == ["p2/b.cpp"]
    assert grouped[""] == ["loose/c.cpp"], "未归类的放空键下，不要悄悄丢掉"


# --------------------------------------------------------------------------- #
# 模式校验
# --------------------------------------------------------------------------- #


def test_default_patterns_use_ident() -> None:
    assert matching.default_patterns("p1") == ["p1/**"]


def test_validate_accepts_normal_patterns() -> None:
    for pattern in ["p1/**", "**/*.cpp", "{ident}/**", "a[bc]/?.cpp", "签到题/*.cpp"]:
        assert validate_pattern(pattern) == pattern


def test_validate_trims_whitespace() -> None:
    """从表格里粘贴过来常带空格。修剪掉，别让教师对着看不见的字符排查。"""
    assert validate_pattern("  p1/**  ") == "p1/**"


@pytest.mark.parametrize(
    "bad,why",
    [
        ("", "空"),
        ("   ", "全空格"),
        ("/p1/**", "以 / 开头"),
        ("p1\\**", "反斜杠"),
        ("a" * 300, "过长"),
        ("../secret/**", "含 .. 段"),
        ("p1/../p2/**", "含 .. 段"),
    ],
)
def test_validate_rejects_bad_patterns(bad: str, why: str) -> None:
    with pytest.raises(PatternError):
        validate_pattern(bad)


def test_unclosed_bracket_is_literal_not_an_error() -> None:
    """``[`` 当成普通字符是完全可能的写法，不该因此报错。"""
    assert validate_pattern("p[1/**") == "p[1/**"
    assert bool(matching.compile_pattern("p[1/**").match("p[1/a.cpp")) is True


def test_normalise_patterns_falls_back_to_default() -> None:
    assert matching.normalise_patterns(None, "p1") == ["p1/**"]
    assert matching.normalise_patterns([], "p1") == ["p1/**"]
    assert matching.normalise_patterns(["", "  "], "p1") == ["p1/**"]


def test_normalise_patterns_enforces_limit() -> None:
    many = ["p1/**"] * (matching.MAX_PATTERNS + 1)
    with pytest.raises(PatternError):
        matching.normalise_patterns(many, "p1")


def test_compile_is_cached() -> None:
    """逐文件逐题目调用，不能每次都重新编译 —— 这是热路径。"""
    first = matching.compile_pattern("cached/**")
    assert matching.compile_pattern("cached/**") is first
