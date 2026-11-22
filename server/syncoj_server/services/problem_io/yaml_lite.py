"""受限的 YAML 子集：只覆盖 Hydro / SYZOJ 实际会写出的写法，不引 PyYAML。

支持：块映射、块序列（含 ``- key: v`` 形式的映射项）、行内 ``[..]`` / ``{..}``（可跨行）、
单/双引号与无引号标量、``#`` 注释、开头的 ``---``。

不支持（报 :class:`YamlError`，``code = "yaml_unsupported"``）：锚点 ``&``、别名 ``*``、
标签 ``!``、块标量 ``|`` / ``>``、复杂键 ``?``、指令 ``%``、多文档、跨行的无引号标量。

无引号标量的类型：``null`` / ``~`` / 空 → None；``true`` / ``false`` → bool；十进制整数 → int；
浮点数 → float；其余 → str（所以 ``time: 1s`` 是字符串 ``"1s"``）。

:func:`dump` 的输出能被 :func:`load` 原样读回；字符串一律用 JSON 风格的双引号。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple, Union

__all__ = ["YamlError", "load", "dump", "MAX_INPUT_BYTES", "MAX_DEPTH"]

MAX_INPUT_BYTES = 1024 * 1024
MAX_DEPTH = 32

_UNSUPPORTED_START = "&*!|>?%@`"
_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class YamlError(ValueError):
    """``code``：``yaml_unsupported``（不支持的写法）或 ``yaml_invalid``（写错了）。"""

    def __init__(self, line: int, reason: str, code: str = "yaml_invalid") -> None:
        super().__init__("第 %d 行：%s" % (line, reason))
        self.line = line
        self.reason = reason
        self.code = code


def _unsupported(line: int, what: str) -> YamlError:
    return YamlError(line, "不支持的 YAML 写法（%s）" % what, "yaml_unsupported")


# --------------------------------------------------------------------------- #
# 预处理：去注释、记缩进
# --------------------------------------------------------------------------- #


class _Line:
    __slots__ = ("no", "indent", "text")

    def __init__(self, no: int, indent: int, text: str) -> None:
        self.no = no
        self.indent = indent
        self.text = text


def _strip_comment(text: str, no: int) -> str:
    """去掉行尾注释（引号外、前面是空白或行首的 ``#``）。"""
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            # 只有出现在值的开头（或行内集合里）时才算引号；"it's" 里的不算
            prev = text[:i].rstrip()
            if not prev or prev[-1] in ":-[{,":
                quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _lines(text: str) -> List[_Line]:
    out: List[_Line] = []
    seen_content = False
    for no, raw in enumerate(text.split("\n"), 1):
        raw = raw.rstrip("\r")
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise YamlError(no, "缩进不能用 Tab")
        body = _strip_comment(raw, no)
        if not body.strip():
            continue
        stripped = body.strip()
        if stripped == "---":
            if seen_content:
                raise _unsupported(no, "多文档")
            continue
        if stripped == "...":
            continue
        if stripped.startswith("%"):
            raise _unsupported(no, "指令")
        seen_content = True
        indent = len(body) - len(body.lstrip(" "))
        out.append(_Line(no, indent, body.strip()))
    return out


# --------------------------------------------------------------------------- #
# 标量与行内集合
# --------------------------------------------------------------------------- #


def _plain_scalar(text: str) -> Any:
    lowered = text.lower()
    if text in ("", "~") or lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


class _Flow:
    """行内集合 / 引号标量的小解析器。"""

    def __init__(self, text: str, line: int, depth: int) -> None:
        self.text = text
        self.pos = 0
        self.line = line
        self.depth = depth

    def error(self, reason: str) -> YamlError:
        return YamlError(self.line, reason)

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\n":
            self.pos += 1

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def value(self, depth: int, terminators: str) -> Any:
        if depth > MAX_DEPTH:
            raise self.error("嵌套超过 %d 层" % MAX_DEPTH)
        self.skip_ws()
        ch = self.peek()
        if ch == "[":
            return self.seq(depth + 1)
        if ch == "{":
            return self.mapping(depth + 1)
        if ch in "\"'":
            return self.quoted()
        if ch and ch in _UNSUPPORTED_START:
            raise _unsupported(self.line, ch)
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in terminators:
            if self.text[self.pos] == ":" and terminators and (
                self.pos + 1 >= len(self.text) or self.text[self.pos + 1] in " \t\n"
            ):
                break
            self.pos += 1
        return _plain_scalar(self.text[start:self.pos].strip())

    def quoted(self) -> str:
        quote = self.text[self.pos]
        if quote == '"':
            end = self.pos + 1
            while end < len(self.text):
                if self.text[end] == "\\":
                    end += 2
                    continue
                if self.text[end] == '"':
                    break
                end += 1
            if end >= len(self.text):
                raise self.error("双引号没有闭合")
            raw = self.text[self.pos:end + 1]
            self.pos = end + 1
            try:
                return json.loads(raw)
            except ValueError:
                raise self.error("双引号字符串里有无法识别的转义")
        out = []
        i = self.pos + 1
        while i < len(self.text):
            if self.text[i] == "'":
                if i + 1 < len(self.text) and self.text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                self.pos = i + 1
                return "".join(out)
            out.append(self.text[i])
            i += 1
        raise self.error("单引号没有闭合")

    def seq(self, depth: int) -> List[Any]:
        self.pos += 1
        items: List[Any] = []
        while True:
            self.skip_ws()
            if self.peek() == "]":
                self.pos += 1
                return items
            if not self.peek():
                raise self.error("[ 没有闭合")
            items.append(self.value(depth, ",]"))
            self.skip_ws()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "]":
                raise self.error("行内序列里缺逗号")

    def mapping(self, depth: int) -> dict:
        self.pos += 1
        out: dict = {}
        while True:
            self.skip_ws()
            if self.peek() == "}":
                self.pos += 1
                return out
            if not self.peek():
                raise self.error("{ 没有闭合")
            key = self.value(depth, ":,}")
            self.skip_ws()
            if self.peek() != ":":
                raise self.error("行内映射里缺冒号")
            self.pos += 1
            value = self.value(depth, ",}")
            if not isinstance(key, (str, int, float, bool)) and key is not None:
                raise self.error("键必须是标量")
            out[key] = value
            self.skip_ws()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "}":
                raise self.error("行内映射里缺逗号")


def _inline_value(text: str, line: int, depth: int) -> Any:
    """冒号或 ``- `` 后面那一段（不是块）。"""
    if not text:
        return None
    if text[0] in _UNSUPPORTED_START:
        raise _unsupported(line, text[0])
    if text[0] in "[{\"'":
        parser = _Flow(text, line, depth)
        value = parser.value(depth, "")
        parser.skip_ws()
        if parser.pos != len(text):
            raise YamlError(line, "值后面有多余的内容")
        return value
    return _plain_scalar(text)


def _split_key(text: str, line: int) -> Optional[Tuple[Any, str]]:
    """``key: value`` → (key, value 文本)；不是映射项返回 None。"""
    if text[0] in "\"'":
        parser = _Flow(text, line, 0)
        key = parser.quoted()
        rest = text[parser.pos:].lstrip(" ")
        if not rest.startswith(":"):
            return None
        return key, rest[1:].strip()
    if text[0] in "[{":
        return None
    match = re.search(r":(\s|$)", text)
    if match is None:
        return None
    key_text = text[: match.start()].strip()
    if key_text and key_text[0] in _UNSUPPORTED_START:
        raise _unsupported(line, key_text[0])
    return _plain_scalar(key_text), text[match.end():].strip()


# --------------------------------------------------------------------------- #
# 块结构
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, lines: List[_Line]) -> None:
        self.lines = lines
        self.i = 0

    def block(self, indent: int, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise YamlError(self.lines[self.i].no, "嵌套超过 %d 层" % MAX_DEPTH)
        line = self.lines[self.i]
        if line.text[0] in _UNSUPPORTED_START:
            raise _unsupported(line.no, line.text[0])
        if line.text == "-" or line.text.startswith("- "):
            return self.sequence(line.indent, depth)
        if _split_key(line.text, line.no) is not None:
            return self.mapping(line.indent, depth)
        # 单独一个标量 / 行内集合
        value_text, end = self.gather_flow(self.i, line.text)
        self.i = end
        if self.i < len(self.lines) and self.lines[self.i].indent >= indent:
            raise _unsupported(self.lines[self.i].no, "跨行的标量")
        return _inline_value(value_text, line.no, depth)

    def gather_flow(self, start: int, text: str) -> Tuple[str, int]:
        """行内集合可以跨行：一直拼到括号配平。返回 (文本, 下一行下标)。"""
        if not text or text[0] not in "[{":
            return text, start + 1
        parts = [text]
        i = start + 1
        while _unbalanced("\n".join(parts)):
            if i >= len(self.lines):
                raise YamlError(self.lines[start].no, "行内集合没有闭合")
            parts.append(self.lines[i].text)
            i += 1
        return "\n".join(parts), i

    def value_after(self, text: str, line: _Line, parent_indent: int, depth: int) -> Any:
        """``key:`` / ``-`` 后面的值：同行就地解析，空则读下面更深的块。"""
        if text:
            if text[0] in _UNSUPPORTED_START:
                raise _unsupported(line.no, text[0])
            joined, end = self.gather_flow(self.i, text)
            self.i = end
            if (
                self.i < len(self.lines)
                and self.lines[self.i].indent > parent_indent
                and not joined.startswith(("[", "{"))
            ):
                raise _unsupported(self.lines[self.i].no, "跨行的标量")
            return _inline_value(joined, line.no, depth)
        self.i += 1
        if self.i >= len(self.lines):
            return None
        nxt = self.lines[self.i]
        if nxt.indent > parent_indent:
            return self.block(nxt.indent, depth + 1)
        # 允许 "key:\n- a" 这种序列与键同缩进
        if nxt.indent == parent_indent and (nxt.text == "-" or nxt.text.startswith("- ")):
            return self.sequence(nxt.indent, depth + 1, stop_at_key=True)
        return None

    def mapping(self, indent: int, depth: int) -> dict:
        out: dict = {}
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError(line.no, "缩进不对")
            if line.text == "-" or line.text.startswith("- "):
                raise YamlError(line.no, "映射里出现了序列项")
            if line.text[0] in _UNSUPPORTED_START:
                raise _unsupported(line.no, line.text[0])
            split = _split_key(line.text, line.no)
            if split is None:
                raise YamlError(line.no, "应为 key: value")
            key, rest = split
            if isinstance(key, (list, dict)):
                raise YamlError(line.no, "键必须是标量")
            if key in out:
                raise YamlError(line.no, "重复的键 %s" % key)
            out[key] = self.value_after(rest, line, indent, depth)
        return out

    def sequence(self, indent: int, depth: int, stop_at_key: bool = False) -> List[Any]:
        items: List[Any] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError(line.no, "缩进不对")
            if not (line.text == "-" or line.text.startswith("- ")):
                if stop_at_key:
                    break
                raise YamlError(line.no, "序列里出现了非序列项")
            rest = line.text[1:].lstrip(" ")
            if rest and rest[0] not in "[{\"'" and _split_key(rest, line.no) is not None:
                # "- key: v"：把这一行改写成更深缩进的映射首行，再按映射读
                offset = len(line.text) - len(rest)
                self.lines[self.i] = _Line(line.no, indent + offset, rest)
                items.append(self.mapping(indent + offset, depth + 1))
            elif rest.startswith("- "):
                offset = len(line.text) - len(rest)
                self.lines[self.i] = _Line(line.no, indent + offset, rest)
                items.append(self.sequence(indent + offset, depth + 1))
            else:
                items.append(self.value_after(rest, line, indent, depth + 1))
        return items


def _unbalanced(text: str) -> bool:
    depth = 0
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth > 0


def load(text: Union[str, bytes]) -> Any:
    """解析；不合法 / 不支持抛 :class:`YamlError`。空文档返回 None。"""
    if isinstance(text, bytes):
        if len(text) > MAX_INPUT_BYTES:
            raise YamlError(1, "超过 1 MB")
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise YamlError(1, "不是 UTF-8")
    if len(text) > MAX_INPUT_BYTES:
        raise YamlError(1, "超过 1 MB")
    lines = _lines(text)
    if not lines:
        return None
    parser = _Parser(lines)
    value = parser.block(lines[0].indent, 1)
    if parser.i < len(lines):
        raise YamlError(lines[parser.i].no, "缩进不对")
    return value


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if text in ("inf", "-inf", "nan"):
            raise ValueError("不能输出 %s" % text)
        return text
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError("不能输出 %r" % type(value))


def _key(key: Any) -> str:
    if isinstance(key, str) and _PLAIN_KEY.match(key) and _plain_scalar(key) == key:
        return key
    return _scalar(key)


def _dump(value: Any, indent: int, out: List[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                out.append("%s%s:" % (pad, _key(key)))
                _dump(item, indent + 2, out)
            else:
                out.append("%s%s: %s" % (pad, _key(key), _inline(item)))
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                lines: List[str] = []
                _dump(item, indent + 2, lines)
                lines[0] = pad + "- " + lines[0][indent + 2:]
                out.extend(lines)
            elif isinstance(item, list) and item:
                out.append(pad + "- " + _inline(item))
            else:
                out.append(pad + "- " + _inline(item))
        return
    out.append(pad + _scalar(value))


def _inline(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join("%s: %s" % (_key(k), _inline(v)) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_inline(item) for item in value) + "]"
    return _scalar(value)


def dump(value: Any) -> str:
    """输出成 YAML 文本（以换行结尾）。键按插入顺序。"""
    out: List[str] = []
    if isinstance(value, (dict, list)) and value:
        _dump(value, 0, out)
    else:
        out.append(_inline(value))
    return "\n".join(out) + "\n"
