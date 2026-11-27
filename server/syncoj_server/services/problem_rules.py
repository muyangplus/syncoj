"""题目的归题规则：把库里那一行整理成 ``matching`` 能用的东西。

放在 ``services/`` 而不是 ``api/admin.py`` 里，是因为它有两个读者：**管理端**
（题目列表、文件台账的归题视图）与**心跳**（回收时按题目预设过滤）。写成 api 模块
之间互相 import 会让两个路由模块互相依赖 —— 同一个理由已经让 ``scan_missing``
单独成模块。

``patterns_for`` 返回的是**模板本身**：``{ident}`` / ``{title}`` 占位符原样保留，
由 ``matching`` 在每次匹配时展开。这一点很关键：如果这里把占位符换成具体名字再返回，
前端"打开编辑再保存"就会把 ``{ident}/**`` 冻成一个写死的 ``p1/**`` —— 之后改个题目
标识，代码就再也认不出来了，而界面上看不出任何异常。
"""

from __future__ import annotations

import json
import logging
import os
from typing import List

from sqlalchemy import select

from ..config import Settings
from ..models import Problem
from . import matching

__all__ = ["patterns_for", "rules_for", "answer_patterns", "answer_extensions"]

log = logging.getLogger(__name__)


def patterns_for(row: Problem, settings: Settings) -> List[str]:
    """题目实际生效的 glob 模式模板。

    配了就用配的，没配就用服务端的默认模板。坏内容（手工改库、半截写入）回退到
    默认模式而不是抛异常：一道题的模式读不出来不该让整个题目列表 500。
    """
    if not row.file_patterns:
        if (row.problem_type or "") == "answer_only":
            return answer_patterns(row)
        return [settings.default_file_pattern]

    try:
        stored = json.loads(row.file_patterns)
    except (TypeError, ValueError):
        log.warning("题目 %s 的 file_patterns 不是合法 JSON，回退到默认模式", row.ident)
        return [settings.default_file_pattern]

    if not isinstance(stored, list):
        return [settings.default_file_pattern]
    return [str(p) for p in stored if isinstance(p, str) and p.strip()]


def answer_patterns(row: Problem) -> List[str]:
    """提交答案题没配模式时的默认模式（§12.13）：``{ident}/<答案名公共前缀>*<扩展名>``，每种扩展名
    一条。用公共前缀而不是 ``*.out``：选手目录里编译出来的 ``a.out`` 之类不该被当成答案收上来。
    还没有数据包时用 ``{ident}/{ident}*.out``。"""
    try:
        info = json.loads(row.testdata_info or "{}")
    except ValueError:
        info = {}
    names = info.get("answer_names") if isinstance(info, dict) else None
    if not isinstance(names, dict) or not names:
        return ["{ident}/{ident}*.out"]
    resolved = [str(n).replace("{ident}", row.ident) for n in names.values()]
    patterns = []
    for ext in sorted({n[n.rfind("."):] for n in resolved if "." in n}):
        group = [n[: -len(ext)] for n in resolved if n.endswith(ext)]
        prefix = os.path.commonprefix(group)
        if prefix.startswith(row.ident):
            prefix = "{ident}" + prefix[len(row.ident):]
        patterns.append("{ident}/%s*%s" % (prefix, ext))
    return patterns


def answer_extensions(rows) -> List[str]:
    """一批题目里提交答案题的答案扩展名（Agent 要多收这些扩展名的文件）。"""
    exts = set()
    for row in rows:
        if (row.problem_type or "") != "answer_only":
            continue
        try:
            info = json.loads(row.testdata_info or "{}")
        except ValueError:
            continue
        exts.update(str(e).lower() for e in (info.get("answer_exts") or []) if str(e).startswith("."))
        if not info.get("answer_exts"):
            exts.add(".out")
    return sorted(exts)


def rules_for(session, contest_id: int, settings: Settings) -> List[matching.ProblemRule]:
    """把某场次的题目整理成匹配规则，供归题与回收过滤使用。

    **顺序就是题目的顺序** —— 匹配时第一个命中的胜出。让顺序显式可预期，
    比"最具体者优先"这类隐式规则好排查。
    """
    rows = session.execute(
        select(Problem)
        .where(Problem.contest_id == contest_id)
        .order_by(Problem.order_index, Problem.ident)
    ).scalars()
    return [
        matching.ProblemRule(
            ident=row.ident, title=row.title, patterns=patterns_for(row, settings)
        )
        for row in rows
    ]
