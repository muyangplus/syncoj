"""ident 建议与 IO 文件名的规整。"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Set, Tuple

from ...paths import PathValidationError, validate_relpath

__all__ = ["suggest_ident", "ident_from_io", "valid_ident", "io_name_error"]

_BAD_CHARS = re.compile(r"[^A-Za-z0-9_-]")
MAX_IDENT = 64


def valid_ident(text: str) -> bool:
    """与 ``api/admin._validate_problem_ident`` 同一条规则：单个合法路径段。"""
    if not text or "/" in text or text != text.strip():
        return False
    try:
        validate_relpath(text, max_length=MAX_IDENT)
    except PathValidationError:
        return False
    return True


def suggest_ident(raw: Optional[str], taken: Set[str], index: int = 1) -> Tuple[str, bool]:
    """把来源里的名字规整成 ident，并在 ``taken`` 内去重（比较不分大小写）。

    返回 ``(ident, renamed)``；``renamed`` = 结果与原名不同。结果会加进 ``taken``。
    """
    original = (raw or "").strip()
    text = unicodedata.normalize("NFKC", original)
    text = _BAD_CHARS.sub("_", text).strip("_")[:MAX_IDENT]
    if not text or not valid_ident(text):
        text = "p%d" % index
    lowered = {item.lower() for item in taken}
    candidate = text
    serial = 2
    while candidate.lower() in lowered:
        suffix = "-%d" % serial
        candidate = text[: MAX_IDENT - len(suffix)] + suffix
        serial += 1
    taken.add(candidate)
    return candidate, candidate != original


def ident_from_io(filename: Optional[str]) -> Optional[str]:
    """文件 IO 题用输入文件名的主名当 ident 候选（``aplusb.in`` → ``aplusb``）。"""
    if not filename:
        return None
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem or None


def io_name_error(name: str) -> Optional[str]:
    """文件 IO 文件名不合法时返回原因；合法返回 None。规则与 ``PUT /problems/{id}/judge`` 相同。"""
    from ...api.admin_judge import _IO_NAME_RE, _RESERVED_IO_NAMES  # 接口层是唯一定义处

    if not _IO_NAME_RE.match(name) or name in (".", ".."):
        return "非法字符"
    if name in _RESERVED_IO_NAMES:
        return "保留名"
    return None
