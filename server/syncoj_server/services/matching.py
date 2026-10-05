"""把回收到的代码文件归到题目上。

题目里配的是一组 glob 模式（``{ident}/**``、``{ident}/src/*.cpp`` 之类）。这里
负责按顺序逐个尝试，**第一个命中的题目胜出** —— 顺序由教师控制，比"最具体者
优先"这类隐式规则更容易预测，出问题时也好解释。

通配符语义
----------
刻意实现成**标准 glob** 而不是直接套 ``fnmatch``：

- ``*``  匹配任意字符，但**不跨** ``/``
- ``**`` 跨 ``/``（``**/*.cpp`` 能匹配 ``p1/src/a.cpp``）
- ``?``  单个字符（不跨 ``/``）
- ``[abc]`` / ``[!abc]`` 字符类

``fnmatch`` 的 ``*`` 是"匹配一切（含 ``/``）"，那会让 ``p1/*`` 意外命中
``p1x/y`` 之外的东西，也让教师没法表达"只在根目录下"。glob 语义更符合直觉。

占位符是**活的**
----------------
``{ident}`` 与 ``{title}`` 在**每次匹配时**才展开，不是入库那一刻就换掉。
所以改标识、改标题之后模式立刻跟着走，不需要教师回去手工改一遍模式 ——
"改了个名字导致代码收不上来"是最难排查的一类故障，因为它看起来像客户端坏了。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

__all__ = [
    "DEFAULT_PATTERN",
    "IDENT_TOKEN",
    "TITLE_TOKEN",
    "MAX_PATTERNS",
    "MAX_PATTERN_LENGTH",
    "PatternError",
    "ProblemRule",
    "compile_pattern",
    "expand_pattern",
    "match_problem",
    "validate_pattern",
    "validate_patterns",
]

log = logging.getLogger(__name__)

#: 没配模式时用的默认值：该题目录下的一切都算这道题。
#: 对应约定 ``桌面/<准考证号>/<题目名>/<题目名>.cpp``。
DEFAULT_PATTERN = "{ident}/**"

#: 展开成题目标识。标识是目录名/文件名/成绩矩阵列名，是稳定的锚点。
IDENT_TOKEN = "{ident}"

#: 展开成题目标题。标题可以留空，此时退回标识 —— 让 ``{title}/**`` 这种写法
#: 在没填标题时仍然能用，而不是静静地匹配不到任何文件。
TITLE_TOKEN = "{title}"

#: 单道题最多配多少个模式 —— 防止有人粘一坨进去
MAX_PATTERNS = 16

#: 模式长度上限
MAX_PATTERN_LENGTH = 256


@dataclass(frozen=True)
class ProblemRule:
    """一道题的认领规则。

    把 ``title`` 一起带上，是因为 ``{title}`` 要能展开；而 ``ident`` 既是展开
    素材，也是匹配结果的返回值。
    """

    ident: str
    patterns: Sequence[str]
    title: Optional[str] = None


class PatternError(ValueError):
    """模式不合法。

    刻意**不是** ``re.error``：调用方（API 层）只该认识这一种失败，
    而且"模式写错了"是用户能自己改的问题，不该冒成 500。
    """


def expand_pattern(pattern: str, ident: str, title: Optional[str] = None) -> str:
    """展开模式里的 ``{ident}`` 与 ``{title}``。

    在**匹配那一刻**展开，所以题目改名之后模式自动跟着走，不会留下一个
    写死的旧名字。
    """
    text = pattern.replace(IDENT_TOKEN, ident)
    return text.replace(TITLE_TOKEN, title or ident)


def validate_pattern(raw: str) -> str:
    """校验一个模式，返回归一化后的形式。"""
    if not isinstance(raw, str):
        raise PatternError("模式必须是字符串")
    text = raw.strip()
    if not text:
        raise PatternError("模式不能为空")
    if len(text) > MAX_PATTERN_LENGTH:
        raise PatternError("模式过长（上限 %d 字符）" % MAX_PATTERN_LENGTH)

    # 模式只用于匹配回收上来的相对路径，不碰文件系统，
    # 所以这里不是安全校验，而是"这明显是写错了"的兜底
    if text.startswith("/"):
        raise PatternError("模式不能以 / 开头（它匹配的是扫描根的相对路径）")
    if "\\" in text:
        raise PatternError("模式里请用正斜杠 /")
    for segment in text.split("/"):
        if segment == "..":
            raise PatternError("模式里不能出现 .. 段")

    # 提前编译一次，把语法错误在这里暴露，而不是等到回收文件时才发现。
    # 把 re.error 包成 PatternError —— 否则 API 层接不住会变成 500，
    # 而教师看到的应该是"这条模式不合法"。
    try:
        compile_pattern(text)
    except re.error as exc:
        raise PatternError("模式语法有误：%s" % exc)
    return text


def validate_patterns(raw: Optional[Iterable[str]]) -> List[str]:
    """校验一整份模式列表，返回归一化后的形式。

    空列表是合法的 —— 它的含义是"用服务端的默认模式"。数量上限在这里卡住：
    逐条校验挡不住"粘了一百条进来"，而那会让每次归题都变慢、
    还会让"到底哪条命中"变得没法解释。
    """
    patterns: List[str] = []
    for item in raw or []:
        patterns.append(validate_pattern(item))
        if len(patterns) > MAX_PATTERNS:
            raise PatternError("最多只能配 %d 个模式" % MAX_PATTERNS)
    return patterns


# --------------------------------------------------------------------------- #
# 模式编译
# --------------------------------------------------------------------------- #

_PATTERN_CACHE: Dict[str, "re.Pattern[str]"] = {}


def compile_pattern(pattern: str) -> "re.Pattern[str]":
    """把 glob 模式编译成正则。带缓存 —— 匹配是逐文件逐题目调用的。

    ``pattern`` 必须是**已经展开过占位符**的最终模式。
    """
    cached = _PATTERN_CACHE.get(pattern)
    if cached is not None:
        return cached

    result = re.compile(_translate(pattern))
    _PATTERN_CACHE[pattern] = result
    return result


def _translate(pattern: str) -> str:
    out: List[str] = []
    index = 0
    length = len(pattern)

    while index < length:
        char = pattern[index]

        if char == "*":
            if pattern.startswith("**", index):
                index += 2
                # ``**/`` 也要匹配"零层目录"，否则 ``**/*.cpp`` 命不中根目录下的
                # a.cpp —— 那是反直觉的
                if index < length and pattern[index] == "/":
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
            index += 1
            continue

        if char == "?":
            out.append("[^/]")
            index += 1
            continue

        if char == "[":
            end = _find_class_end(pattern, index)
            if end < 0:
                # 没有闭合的 `]`，按字面量处理而不是报错 ——
                # 教师写了 `[` 当普通字符是完全可能的
                out.append(re.escape(char))
                index += 1
                continue
            body = pattern[index + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[%s]" % body.replace("\\", "\\\\"))
            index = end + 1
            continue

        out.append(re.escape(char))
        index += 1

    return "(?s:%s)\\Z" % "".join(out)


def _find_class_end(pattern: str, start: int) -> int:
    index = start + 1
    if index < len(pattern) and pattern[index] in ("!", "^"):
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1  # `[]]` 里的第一个 ] 是字面量
    while index < len(pattern):
        if pattern[index] == "]":
            return index
        index += 1
    return -1


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #


def match_problem(
    rel_path: str,
    rules: Sequence[ProblemRule],
) -> Optional[str]:
    """判断一个文件属于哪道题。

    按 ``rules`` 给定的顺序尝试，**第一个命中的胜出**。
    都不命中返回 ``None``（未归类）—— 不猜、不兜底。
    """
    if not rel_path:
        return None

    for rule in rules:
        for pattern in rule.patterns:
            # 展开失败或编译失败都不该让整轮归题崩掉：一条坏模式最多是
            # "这道题认领不到"，不该连累其它题目
            try:
                expanded = expand_pattern(pattern, rule.ident, rule.title)
                compiled = compile_pattern(expanded)
            except (re.error, TypeError, AttributeError) as exc:  # pragma: no cover
                log.warning(
                    "题目 %s 的模式 %r 无法编译，已跳过: %s", rule.ident, pattern, exc
                )
                continue
            if compiled.match(rel_path):
                return rule.ident
    return None

