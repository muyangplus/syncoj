"""扫描根缺失的**存取形态**：库里那一列 ↔ 一串绝对路径。

为什么单独抽一个模块：这个值有两个读者（Agent 心跳写入、管理端列表读出），
而它又必须跨进程存活（"上一次报的是什么"是判断状态变化的唯一依据 —— 只放内存的话，
服务端一重启就会把同一批缺失重报一遍，审计里立刻出现一串假事件）。
两处各写一遍 JSON 读写，迟早会出现"写进去的是一个对象、读出来当成数组"这种
只在某一侧看得见的错。

放在 ``services/`` 而不是 ``api/`` 里，是为了让 ``api/admin.py`` 能读它而不去
import 另一个 api 模块（那会形成两个路由模块互相依赖）。
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

__all__ = ["MAX_PATHS", "dump_scan_missing", "load_scan_missing", "normalize"]

#: 服务端这边最多认多少条。Agent 上报时已经截到 5 条，这里再拦一次是因为
#: 报文来自网络：多出来的路径既没用（页面上显示不下）又会把一列无限撑大。
MAX_PATHS = 20


def normalize(values: Optional[List[Any]]) -> List[str]:
    """去空、去重、排序，并截到 :data:`MAX_PATHS`。

    **排序**是为了让"同一个集合、不同顺序"不被当成状态变化：顺序是 Agent 那边的
    实现细节，而这里判的是"缺失的路径集合有没有变"。
    """
    if not values:
        return []
    cleaned = []
    for item in values:
        # None 不是路径：``str(None)`` 会变成字面量 "None"，然后被当成一个目录显示出来
        if item is None:
            continue
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return sorted(set(cleaned))[:MAX_PATHS]


def dump_scan_missing(values: Optional[List[Any]]) -> Optional[str]:
    """把路径列表存进库。空列表存成 ``NULL``：它与"没有已知的缺失"是同一件事，
    而 ``NULL`` 让"这台机器从来没报过"在库里一眼可辨。"""
    items = normalize(values)
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def load_scan_missing(raw: Optional[str]) -> List[str]:
    """把库里那一列读回一个排好序的列表。坏内容当成"没有缺失"，不抛异常。

    这一列是我们自己写进去的，坏内容只可能来自手工改库或半截写入；为它让
    「机器列表」整个 500，代价是教师在最需要看现场的时候看不到任何机器。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return normalize(parsed)
