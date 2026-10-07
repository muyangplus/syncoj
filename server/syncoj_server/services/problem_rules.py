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
from typing import List

from sqlalchemy import select

from ..config import Settings
from ..models import Problem
from . import matching

__all__ = ["patterns_for", "rules_for"]

log = logging.getLogger(__name__)


def patterns_for(row: Problem, settings: Settings) -> List[str]:
    """题目实际生效的 glob 模式模板。

    配了就用配的，没配就用服务端的默认模板。坏内容（手工改库、半截写入）回退到
    默认模式而不是抛异常：一道题的模式读不出来不该让整个题目列表 500。
    """
    if not row.file_patterns:
        return [settings.default_file_pattern]

    try:
        stored = json.loads(row.file_patterns)
    except (TypeError, ValueError):
        log.warning("题目 %s 的 file_patterns 不是合法 JSON，回退到默认模式", row.ident)
        return [settings.default_file_pattern]

    if not isinstance(stored, list):
        return [settings.default_file_pattern]
    return [str(p) for p in stored if isinstance(p, str) and p.strip()]


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
