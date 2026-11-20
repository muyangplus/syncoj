"""考号（``player_no``）的形状规则 —— 四个写入口共用这一份。

考号不只是个显示用的字符串：它**直接拼进落盘路径**
（``source/<场次>/<考号>/…``、评测结果目录 ``judge_result/<场次>/<考号>/…``），
也是 Agent 扫描目录模板 ``{player_no}`` 的展开值。曾经四个入口（导入选手、改选手、
导入名单、改名单条目）只查了"非空、≤64"，于是 ``../evil``、``a/b`` 都能入库：
代码会落进别的场次或别人的目录，成绩扫描也对不上号。

规则：strip 之后必须是**一个**合法路径段（与服务端路径校验同一套：不许 ``/`` ``\\``、
``.`` ``..``、控制字符、Windows 保留名等），且不以点开头（隐藏目录会被扫描与评测器
跳过），≤64 字符。
"""

from __future__ import annotations

from typing import Optional

from ..paths import PathValidationError, validate_relpath

__all__ = ["MAX_PLAYER_NO_LENGTH", "player_no_problem"]

MAX_PLAYER_NO_LENGTH = 64


def player_no_problem(value: Optional[str]) -> Optional[str]:
    """不合格就返回一句给教师看的原因，合格返回 ``None``。调用方先 strip。"""
    text = value or ""
    if not text:
        return "选手编号为空"
    if len(text) > MAX_PLAYER_NO_LENGTH:
        return "选手编号超过 %d 字符" % MAX_PLAYER_NO_LENGTH
    if "/" in text or "\\" in text:
        return "选手编号 %r 不能含斜杠（它会被拼进代码与成绩的目录名）" % text
    if text.startswith("."):
        return "选手编号 %r 不能以点开头（会变成隐藏目录）" % text
    try:
        validate_relpath(text, max_length=MAX_PLAYER_NO_LENGTH)
    except PathValidationError as exc:
        return "选手编号 %r 不能用作目录名：%s" % (text, exc)
    return None
