"""显示时区：给教师看的时间字符串按哪个钟点渲染。

只影响**显示**
--------------
库里一律是 naive UTC，排序与范围查询也一律按 UTC —— 这条底线不因为显示时区而变。
本模块只做一件方向单一的事：把一个"库里的 UTC 时刻"渲染成**人看的那串数字**。

为什么不能直接用服务端进程的本地时区
------------------------------------
原来这里调的是 ``astimezone()``，也就是**进程所在机器的本地时区**。服务端跑在
UTC 的容器里时，教师看到的就是 UTC 时间 —— 而中国考场的墙上那只钟是 UTC+8，
"本场考试已于 11:30 结束"这句话会晚 8 小时，照着它行动的人会早到或晚到一整个
上午。所以显示时区必须是**配置项**，不是"服务端恰好装在哪"。

只支持固定偏移，刻意不碰 tzdata
-------------------------------
``SYNCOJ_TZ`` 只认三种写法：

* 固定偏移：``+08:00`` / ``-05:30`` / ``+0800`` / ``+8``；
* ``UTC`` / ``Z`` / ``GMT``（= ``+00:00``）；
* 几个**硬编码的中国时区别名**：``Asia/Shanghai`` / ``Asia/Chongqing`` /
  ``Asia/Harbin`` / ``PRC`` / ``CST``（= ``+08:00``）。

为什么不用 ``zoneinfo`` 解析 IANA 名：它是 **Python 3.9+ 才进标准库**的模块，
而目标机是 3.8；更要紧的是它还需要一份独立的 tzdata 数据库 —— Windows 上通常
没有，现场的服务端也就无从验证。为"几个小时的显示差"引入一个会静默降级的依赖，
不如把考区那个偏移写死：中国没有夏令时，这几个别名在任何日期都等于 ``+08:00``。

需要别的时区时，**写固定偏移**（``SYNCOJ_TZ=-05:00``）就能立刻用；真要按
"某地夏令时规则自动切换"，那是一件比这个配置项大得多的事，不该藏在一个显示开关里。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

__all__ = [
    "DEFAULT_OFFSET_MINUTES",
    "SYNCOJ_TZ_ENV",
    "NAMED_OFFSETS",
    "display_label",
    "display_offset",
    "display_offset_minutes",
    "display_timezone_name",
    "local_clock",
    "offset_label",
    "parse_offset_minutes",
    "parse_timezone",
    "to_display",
]

log = logging.getLogger(__name__)

#: 覆盖显示时区的环境变量。留空 = 用默认值。
SYNCOJ_TZ_ENV = "SYNCOJ_TZ"

#: 默认显示时区：UTC+8（Asia/Shanghai）。中国考场墙上那只钟就是它。
DEFAULT_OFFSET_MINUTES = 8 * 60

#: ``+08:00`` / ``-0530`` / ``+8`` 这类写法。冒号可有可无，小时允许一位。
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<hours>\d{1,2}):?(?P<minutes>\d{2})?$")

#: 认得的**名字** → 那个名字的固定偏移。
#:
#: 只列中国这几个（历史与现状都等于 +08:00，没有夏令时）与几个零偏移的写法。
#: 不认识的 IANA 名一律退回默认值并记一条警告 —— 静默按 +08:00 处理会让教师
#: 以为配置生效了，而屏幕上那个钟点其实是他刚写错的时区。
NAMED_OFFSETS: Dict[str, int] = {
    "UTC": 0,
    "Z": 0,
    "GMT": 0,
    "GMT+0": 0,
    "GMT+00:00": 0,
    "ASIA/SHANGHAI": 8 * 60,
    "ASIA/CHONGQING": 8 * 60,
    "ASIA/HARBIN": 8 * 60,
    "ASIA/URUMQI": 8 * 60,
    "PRC": 8 * 60,
    "CST": 8 * 60,
}


def parse_offset_minutes(raw: str) -> Optional[int]:
    """把 ``+08:00`` 这类固定偏移解析成分钟数；不是这种写法就返回 ``None``。"""
    text = (raw or "").strip()
    match = _OFFSET_RE.match(text)
    if not match:
        return None
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes") or 0)
    if hours > 23 or minutes > 59:
        return None
    total = hours * 60 + minutes
    return -total if match.group("sign") == "-" else total


def parse_timezone(raw: str) -> Optional[int]:
    """``SYNCOJ_TZ`` 的取值 → 偏移分钟数；不认识就 ``None``。

    两种写法：固定偏移（``+08:00``）与 :data:`NAMED_OFFSETS` 里那几个名字
    （``UTC`` / ``Asia/Shanghai`` …）。名字不区分大小写 —— 现场有人在环境变量里
    写 ``utc`` 是常事，而为一个大小写让服务端退回 +08:00 说不通。
    """
    offset = parse_offset_minutes(raw)
    if offset is not None:
        return offset
    return NAMED_OFFSETS.get((raw or "").strip().upper())


#: 解析结果的缓存：``(配置串, 偏移分钟, 是否已经警告过)``。
#:
#: 键是**原始的配置串**，所以改环境变量之后自动失效 —— 测试里 monkeypatch
#: 环境变量也就能立刻生效，不需要额外的清缓存入口。
#:
#: 记着"已经警告过"是为了让退回默认值那条提示**只出现一次**：它挂在每次渲染的
#: 前面，而"启动时刷一屏一样的警告"会让真正要看的那条淹掉。换一个配置串就会
#: 重新警告，所以现场改配置时仍然看得见。
_CACHE: Optional[Tuple[str, int, bool]] = None


def display_timezone_name() -> str:
    """生效的显示时区名：配置的原始值（没配就是 ``+08:00``）。"""
    return (os.environ.get(SYNCOJ_TZ_ENV) or "").strip() or offset_label(
        DEFAULT_OFFSET_MINUTES
    )


def display_offset_minutes() -> int:
    """显示时区相对 UTC 的偏移分钟数（默认 480 = UTC+8）。"""
    global _CACHE
    raw = display_timezone_name()
    if _CACHE is not None and _CACHE[0] == raw:
        return _CACHE[1]

    minutes = parse_timezone(raw)
    warned = False
    if minutes is None:
        # 不认识的名字：退回默认值，但**要说出来** —— 静默退回的后果是
        # "屏幕上那个钟点莫名其妙差了几个小时"，而这条警告是唯一的线索。
        log.warning(
            "显示时区 %r 不认识（只认固定偏移如 +08:00，以及 %s 这几个名字）；"
            "退回默认 %s",
            raw,
            "、".join(sorted(NAMED_OFFSETS)),
            offset_label(DEFAULT_OFFSET_MINUTES),
        )
        minutes = DEFAULT_OFFSET_MINUTES
        warned = True

    _CACHE = (raw, minutes, warned)
    return minutes


def display_offset() -> timezone:
    """显示时区的 ``tzinfo``（固定偏移）。"""
    return timezone(timedelta(minutes=display_offset_minutes()))


def offset_label(minutes: int) -> str:
    """``480`` → ``+08:00``。"""
    sign = "+" if minutes >= 0 else "-"
    total = abs(int(minutes))
    return "%s%02d:%02d" % (sign, total // 60, total % 60)


def display_label() -> str:
    """给界面看的一句标识，例如 ``Asia/Shanghai（+08:00）``。

    配置成固定偏移时只回 ``+08:00`` —— 硬拼一个 IANA 名反而会误导人。
    """
    raw = display_timezone_name()
    label = offset_label(display_offset_minutes())
    if parse_offset_minutes(raw) is not None:
        return label
    return "%s（%s）" % (raw, label)


def to_display(value: Optional[datetime]) -> Optional[datetime]:
    """把库里的 naive UTC 时刻折算到显示时区；``None`` 原样返回。

    ``None`` 必须原样透出去："没配时间"和"配了某个时刻"是两件事，塞一个当天的
    零点进去，前端就只能猜这是哪种意思。
    """
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(display_offset())


def local_clock(value: Optional[datetime]) -> str:
    """把库里的 naive UTC 折算成**显示时区**的 ``HH:MM``。

    只给日志与提示语用（"本场考试已于 11:30 结束"）。默认 UTC+8 而不是进程本地
    时区，理由见模块 docstring —— 这句话恰恰是照着它行动的人唯一需要的数。
    """
    shown = to_display(value)
    return shown.strftime("%H:%M") if shown else ""
