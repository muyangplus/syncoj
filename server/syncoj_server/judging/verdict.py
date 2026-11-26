"""checker / interactor 的判定解析（§12.14）：把出题人程序的输出翻译成 (结果, 得分比例, 说明)。

三种协议（数据包 ``config.json`` 的 ``checker_protocol``）：

``testlib``（默认）
    看 stderr **首行前缀**（testlib 的约定）：``ok`` / ``wrong answer`` / ``wrong output format`` /
    ``FAIL`` / ``points X`` / ``partially correct (k)`` / ``unexpected eof``。没有可认的前缀时
    才按退出码兜底：0 → 通过，1 → 答案错，2 → 格式错，其余 → checker 自己出错。
``lemon``
    LemonLime 的老式 spj：``chk <输入> <选手输出> <答案> <满分> <分数文件> <信息文件>``，
    分数文件里是 0..满分的整数；不在范围内或读不出来 → checker 自己出错。
``exitcode``
    只看退出码：0 → 通过，1 → 答案错，其余 → checker 自己出错；stderr 只当说明。

``points X`` 的口径（``checker_points``）：``ratio``（默认，X 是 0..1 的比例）/ ``percent``
（X 是 0..100）/ ``auto``（X ≤ 1 当比例，否则当百分数）。越界判 checker 自己出错 —— 宁可报错
也不悄悄给错分。``partially correct (k)`` 一律按 k/100；k > 100 同样算出错。

本模块只做解析，按协议的文字约定独立实现（不含任何外部评测系统的代码）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Verdict", "OK", "WA", "PE", "PARTIAL", "FAIL", "POINTS_MODES", "PROTOCOLS",
    "parse_testlib", "parse_lemon", "parse_exitcode",
]

OK, WA, PE, PARTIAL, FAIL = "ok", "wa", "pe", "partial", "fail"
POINTS_MODES = ("ratio", "percent", "auto")
PROTOCOLS = ("testlib", "lemon", "exitcode")
#: 比例的容差：0.9999999 当作满分、1e-10 当作 0
EPSILON = 1e-9

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_POINTS = re.compile(r"^points\s+(%s)\s*(.*)$" % _NUMBER, re.S)
_PARTIAL = re.compile(r"^partially correct\s*\(\s*(-?\d+)\s*\)\s*(.*)$", re.S)


@dataclass
class Verdict:
    #: ok / wa / pe / partial / fail
    kind: str
    #: 得分比例 0..1（ok 为 1，wa / pe 为 0；fail 无意义）
    ratio: float
    message: str = ""


def _clean(text: str) -> str:
    return _ANSI.sub("", text or "").strip()


def _from_ratio(ratio: float, message: str) -> Verdict:
    if ratio >= 1 - EPSILON:
        return Verdict(OK, 1.0, message)
    if ratio <= EPSILON:
        return Verdict(WA, 0.0, message)
    return Verdict(PARTIAL, ratio, message)


def _points_ratio(value: float, mode: str) -> Optional[float]:
    """``points X`` 换成比例；越界返回 None。"""
    if value < -EPSILON:
        return None
    if mode == "percent":
        ratio = value / 100.0
    elif mode == "auto":
        ratio = value if value <= 1 + EPSILON else value / 100.0
    else:
        ratio = value
    if ratio > 1 + EPSILON:
        return None
    return max(0.0, min(1.0, ratio))


def parse_testlib(stderr: str, exit_code: Optional[int], points_mode: str = "ratio") -> Verdict:
    """按 testlib 的输出约定解析。``stderr`` 是 checker / interactor 的标准错误全文。"""
    text = _clean(stderr)
    first = text.split("\n", 1)[0].strip()
    lower = first.lower()
    rest = text
    if lower.startswith("ok"):
        return Verdict(OK, 1.0, rest)
    if lower.startswith("wrong answer"):
        return Verdict(WA, 0.0, rest)
    if lower.startswith("wrong output format"):
        return Verdict(PE, 0.0, rest)
    if lower.startswith("unexpected eof"):
        return Verdict(WA, 0.0, rest)
    if first.startswith("FAIL"):
        return Verdict(FAIL, 0.0, rest)
    if lower.startswith("points"):
        if lower.startswith("points_info") or "points_info=" in lower:
            return Verdict(FAIL, 0.0, "checker 用了 quitpi（points_info），本系统不支持：" + rest)
        match = _POINTS.match(first)
        if not match:
            return Verdict(FAIL, 0.0, "checker 的 points 输出读不懂：" + rest)
        ratio = _points_ratio(float(match.group(1)), points_mode)
        if ratio is None:
            return Verdict(FAIL, 0.0, "checker 给的分 %s 超出 %s 口径的范围：%s" % (match.group(1), points_mode, rest))
        return _from_ratio(ratio, rest)
    match = _PARTIAL.match(first)
    if match:
        value = int(match.group(1))
        if value < 0 or value > 100:
            return Verdict(FAIL, 0.0, "partially correct (%d) 超出 0..100：%s" % (value, rest))
        return _from_ratio(value / 100.0, rest)
    # 没有可认的前缀：按退出码兜底
    if exit_code == 0:
        return Verdict(OK, 1.0, rest)
    if exit_code == 1:
        return Verdict(WA, 0.0, rest)
    if exit_code == 2:
        return Verdict(PE, 0.0, rest)
    return Verdict(FAIL, 0.0, "checker 报告自身失败（退出码 %s）：%s" % (exit_code, rest))


def parse_lemon(score_text: Optional[str], full_score: int, message: str = "") -> Verdict:
    """LemonLime spj：分数文件里一个 0..满分的整数（容许小数，按比例算）。"""
    message = _clean(message)
    if score_text is None:
        return Verdict(FAIL, 0.0, "spj 没有写分数文件：%s" % message)
    token = (score_text.strip().split() or [""])[0]
    try:
        value = float(token)
    except ValueError:
        return Verdict(FAIL, 0.0, "spj 的分数文件读不懂（%r）：%s" % (token[:32], message))
    if full_score <= 0:
        return Verdict(FAIL, 0.0, "spj 的满分必须大于 0")
    if value < -EPSILON or value > full_score + EPSILON:
        return Verdict(FAIL, 0.0, "spj 给的分 %s 不在 0..%d：%s" % (token, full_score, message))
    return _from_ratio(max(0.0, value) / full_score, message)


def parse_exitcode(exit_code: Optional[int], message: str = "") -> Verdict:
    """只看退出码：0 通过、1 答案错，其余算 checker 自己出错。"""
    message = _clean(message)
    if exit_code == 0:
        return Verdict(OK, 1.0, message)
    if exit_code == 1:
        return Verdict(WA, 0.0, message)
    return Verdict(FAIL, 0.0, "checker 报告自身失败（退出码 %s）：%s" % (exit_code, message))
