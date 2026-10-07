"""显示时区：给教师/选手看的时间字符串按哪个钟点渲染。

这一组测试守的是一条**只影响显示**的约定：

* 库里继续是 naive UTC，排序与范围查询一个字都不动；
* 给人看的那串数字默认按 UTC+8 渲染（考区墙上那只钟），``SYNCOJ_TZ`` 可改；
* 取不到配置里的 IANA 名时**降级**而不是崩 —— Windows 上没装 tzdata、
  Python 3.8 上没有 ``zoneinfo``，这两种情况在真机上都会遇到。

最后一条尤其重要：``local_clock()`` 以前用的是**进程本地时区**，服务端跑在 UTC
容器里时，"本场考试已于 11:30 结束"会晚 8 小时 —— 而照着这句话行动的人会晚到
一整个上午。
"""

from __future__ import annotations

import ast
import importlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from syncoj_server import timeutil
from syncoj_server.models import local_clock as models_local_clock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都从"没配 SYNCOJ_TZ"开始。

    解析结果按配置串缓存（见 ``display_offset_minutes``），所以只要环境变量变了
    就会自然失效；但仍然要清干净，否则上一条用例设的值会漏到下一条里。
    """
    monkeypatch.delenv(timeutil.SYNCOJ_TZ_ENV, raising=False)


def naive_utc(text: str) -> datetime:
    """库里的写法：不带 tzinfo 的 UTC 时刻。"""
    return datetime.fromisoformat(text)


# --------------------------------------------------------------------------- #
# 存储不变：UTC 就是 UTC
# --------------------------------------------------------------------------- #


def test_显示时区不影响库里那个时刻(monkeypatch: pytest.MonkeyPatch) -> None:
    """``to_display`` 只**算出**一个用于渲染的时刻，不改原值。

    这条是整组测试的底线：时区设置一旦能反过来影响存储，排序与范围查询就会
    开始出现"某些时刻前后不一致"这种查不出来的问题。
    """
    stored = naive_utc("2026-03-01T01:00:00")
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+08:00")

    shown = timeutil.to_display(stored)

    assert stored.tzinfo is None and stored.hour == 1, "原值被改动了"
    assert shown is not None
    assert shown.utcoffset() == timedelta(hours=8)
    assert (shown.hour, shown.strftime("%H:%M")) == (9, "09:00")


def test_utc_偏移为零时原样(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+00:00")
    assert timeutil.local_clock(naive_utc("2026-03-01T01:00:00")) == "01:00"


def test_没配时间时给空串而不是崩(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` 是"这件事没有时间"，不能变成一句 "None" 或当天的零点。"""
    assert timeutil.to_display(None) is None
    assert timeutil.local_clock(None) == ""


# --------------------------------------------------------------------------- #
# 默认值：UTC+8
# --------------------------------------------------------------------------- #


def test_默认是_UTC_加_8(monkeypatch: pytest.MonkeyPatch) -> None:
    """不配任何东西时按考区那只钟显示 —— 这正是这一轮要修的东西。"""
    assert timeutil.display_offset_minutes() == 8 * 60
    assert timeutil.offset_label(timeutil.display_offset_minutes()) == "+08:00"
    assert timeutil.local_clock(naive_utc("2026-03-01T01:00:00")) == "09:00"


def test_进程本地时区不参与(monkeypatch: pytest.MonkeyPatch) -> None:
    """把进程时区设成 UTC（服务端跑在容器里的常态）也要显示 +8。

    旧实现用 ``astimezone()``，这条会红 —— 也就是这条测试盯着的那个回归。
    """
    monkeypatch.setenv("TZ", "UTC")
    import time as _time

    if hasattr(_time, "tzset"):
        _time.tzset()

    assert timeutil.local_clock(naive_utc("2026-03-01T01:00:00")) == "09:00"


def test_models_里的_local_clock_与_timeutil_同一份(monkeypatch: pytest.MonkeyPatch) -> None:
    """``models.local_clock`` 只是转出，两处不能各写一套格式化。

    留着一个同名函数是为了 ``from ..models import local_clock`` 的既有调用方
    （``api/agent.py``、``tasks/flush.py``）不用改 import；但它必须与 timeutil
    给出**同一个**答案，否则同一句话在两处会显示成两个钟点。
    """
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+05:30")
    stored = naive_utc("2026-03-01T01:00:00")

    assert models_local_clock(stored) == timeutil.local_clock(stored) == "06:30"


# --------------------------------------------------------------------------- #
# 跨日边界
# --------------------------------------------------------------------------- #


def test_跨日边界_UTC_23_30_是次日_07_30(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC 23:30 + 8 小时 = **次日** 07:30。

    这是最容易写错的一种：只加时区、不处理日期进位，页面上会显示成"23:30 的
    09:30"，而教师在晚上核对时间窗时完全看不出来。
    """
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+08:00")
    shown = timeutil.to_display(naive_utc("2026-03-01T23:30:00"))

    assert shown is not None
    assert (shown.year, shown.month, shown.day) == (2026, 3, 2)
    assert shown.strftime("%Y-%m-%d %H:%M") == "2026-03-02 07:30"


def test_跨日边界往负方向(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC 00:30 在 -05:00 上是**前一天** 19:30（对称的另一半）。"""
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "-05:00")
    shown = timeutil.to_display(naive_utc("2026-03-01T00:30:00"))

    assert shown is not None
    assert shown.strftime("%Y-%m-%d %H:%M") == "2026-02-28 19:30"


# --------------------------------------------------------------------------- #
# SYNCOJ_TZ 覆盖
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+08:00", "09:00"),
        ("+0800", "09:00"),
        ("+8", "09:00"),
        ("-05:30", "19:30"),
        ("+00:00", "01:00"),
        ("+14:00", "15:00"),
    ],
)
def test_固定偏移各种写法(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, raw)
    assert timeutil.local_clock(naive_utc("2026-03-01T01:00:00")) == expected


def test_偏移越界不算合法(monkeypatch: pytest.MonkeyPatch) -> None:
    """``+25:00`` 这种不是"另一个时区"，是打错了 —— 退回默认而不是照算。"""
    assert timeutil.parse_offset_minutes("+25:00") is None
    assert timeutil.parse_offset_minutes("+08:99") is None
    assert timeutil.parse_offset_minutes("随便写点什么") is None

    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+25:00")
    assert timeutil.display_offset_minutes() == timeutil.DEFAULT_OFFSET_MINUTES


# --------------------------------------------------------------------------- #
# 名字：只认硬编码的那几个，不碰 tzdata
# --------------------------------------------------------------------------- #


def test_中国的几个时区名硬编码为加_8(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Asia/Shanghai`` 这类名字**不走 zoneinfo**，而是硬编码成 ``+08:00``。

    理由有两条，都在 ``timeutil`` 的 docstring 里：``zoneinfo`` 是 Python 3.9+
    才进标准库的（目标机是 3.8），而且它还需要一份独立的 tzdata 数据库 ——
    Windows 上通常没有，现场的服务端也就无从验证。中国没有夏令时，这几个名字
    在任何日期都等于 +08:00。
    """
    for name in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "PRC", "CST"):
        monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, name)
        assert timeutil.display_offset_minutes() == 8 * 60, name
        assert timeutil.local_clock(naive_utc("2026-03-01T01:00:00")) == "09:00", name


def test_名字不区分大小写(monkeypatch: pytest.MonkeyPatch) -> None:
    """现场有人在环境变量里写 ``utc`` 或 ``prc`` 是常事。"""
    for name, expected in (("utc", 0), ("PRC", 8 * 60), ("Asia/Shanghai", 8 * 60)):
        monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, name)
        assert timeutil.display_offset_minutes() == expected, name


def test_零偏移的几个写法(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("UTC", "Z", "GMT"):
        monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, name)
        assert timeutil.display_offset_minutes() == 0, name
        assert timeutil.local_clock(naive_utc("2026-03-01T01:00:00")) == "01:00"


def test_不认识的名字退回默认值(monkeypatch: pytest.MonkeyPatch) -> None:
    """别的 IANA 名一律退回 +08:00 并记一条警告。

    **静默按 +08:00 处理是不行的**：教师以为配置生效了，而屏幕上那个钟点其实
    是他刚写错的时区。所以这条同时断言"值退回默认"与"说了一句"。
    """
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "America/New_York")

    records = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(timeutil.log.name)
    collector = _Collector()
    logger.addHandler(collector)
    logger.setLevel(logging.WARNING)
    try:
        # 解析结果带缓存（同一个配置串只警告一次），先让它重新解析一次：
        # 别条用例可能已经拿同一个值填过缓存
        timeutil._CACHE = None
        minutes = timeutil.display_offset_minutes()
    finally:
        logger.removeHandler(collector)

    assert minutes == timeutil.DEFAULT_OFFSET_MINUTES
    assert any("退回默认" in record.getMessage() for record in records), [
        record.getMessage() for record in records
    ]


def test_不认识的写法退回默认值() -> None:
    """``+25:00`` 这种不是"另一个时区"，是打错了 —— 退回默认而不是照算。"""
    assert timeutil.parse_offset_minutes("+25:00") is None
    assert timeutil.parse_offset_minutes("+08:99") is None
    assert timeutil.parse_timezone("随便写点什么") is None


def test_这个模块不依赖_zoneinfo() -> None:
    """``timeutil`` 里不能出现真正的 ``import zoneinfo``。

    ``check_py38.py`` 是静态 AST 检查：它只看 import 语句，不看你是不是包在
    ``try/except ImportError`` 里。而门禁是"服务端也能跑在 3.8 上"的唯一自动
    保证 —— 所以这里把它钉在位，免得哪天有人顺手把 IANA 解析加回来。

    只查 import 那一行：docstring 里**提到了** ``zoneinfo`` 并解释为什么不用它，
    那不是依赖（``ast`` 只认 ``Import`` / ``ImportFrom`` 节点）。
    """
    source = (Path(__file__).resolve().parents[1] / "syncoj_server" / "timeutil.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "zoneinfo" not in imported, "timeutil 不能 import zoneinfo（Python 3.9+）"


def test_显示标签在固定偏移时不编一个时区名(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+05:30")
    assert timeutil.display_label() == "+05:30"


def test_默认显示时区就是_Asia_Shanghai_那个偏移() -> None:
    """默认值写的是 ``+08:00``，等价于 ``Asia/Shanghai``（中国没有夏令时）。

    这条把这个前提写下来，免得以后有人把默认改成别的时区。
    """
    assert timeutil.parse_timezone("Asia/Shanghai") == timeutil.DEFAULT_OFFSET_MINUTES


# --------------------------------------------------------------------------- #
# 下发到前端
# --------------------------------------------------------------------------- #


def test_meta_接口把显示时区下发出去(client) -> None:
    """前端不猜时区：它按这里给的分钟数渲染所有时间。"""
    response = client.get("/api/v1/meta")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_utc_offset_minutes"] == 8 * 60
    assert "08:00" in body["display_timezone"]


def test_meta_接口不鉴权(client) -> None:
    """登录页、选手页、装机页都要读它 —— 要求鉴权就等于这三个页面都读不到。"""
    assert client.get("/api/v1/meta").status_code == 200


def test_meta_接口跟着配置走(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+05:30")

    body = client.get("/api/v1/meta").json()
    assert body["display_utc_offset_minutes"] == 330
    assert body["display_timezone"] == "+05:30"


def test_选手页给的时间窗仍是_UTC(monkeypatch: pytest.MonkeyPatch) -> None:
    """给前端的永远是带 ``Z`` 的 UTC；换成显示时区是**前端**那一步的事。

    若这里就开始偏移，前端会再偏一次 —— 两边各偏一次的结果是差 16 小时，
    而两边单看都"有理"。
    """
    from syncoj_server.api import player as player_api

    stored = naive_utc("2026-03-01T01:00:00")
    assert player_api._iso(stored) == "2026-03-01T01:00:00Z"


def test_选手页的_server_time_不受显示时区影响(monkeypatch: pytest.MonkeyPatch) -> None:
    """``server_time`` 是真正的 Unix 秒，与显示时区无关。

    它同时是倒计时的**基准**：基准一旦被时区偏移污染，倒计时会整体跑偏 8 小时。
    所以这里在两种显示时区下各算一次，两次必须给出同一个数。
    """
    import time as _time

    from syncoj_server.models import unix_seconds, utcnow

    moment = utcnow()
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+08:00")
    east = unix_seconds(moment)
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "-05:00")
    west = unix_seconds(moment)

    assert east == west
    assert abs(east - int(_time.time())) < 60


def test_模块可以被重复导入而不改变行为(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析结果有缓存，重新 import 不该让它算出另一个值。"""
    monkeypatch.setenv(timeutil.SYNCOJ_TZ_ENV, "+09:00")
    first = timeutil.display_offset_minutes()
    importlib.reload(timeutil)
    assert timeutil.display_offset_minutes() == first == 9 * 60
