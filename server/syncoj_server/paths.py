"""路径安全：所有来自客户端的路径都必须经过这里。

设计原则
--------
1. **服务端独立校验**。Agent 跑在选手机器上，随时可能被替换成恶意程序，
   "客户端已经校验过了" 永远不是理由。
2. 拒绝一切歧义写法：绝对路径、盘符、UNC、``\\``、``..``、空段、控制字符。
3. 归一化后必须仍落在指定根目录内 —— 防 ``..`` 与符号链接穿越。
4. 逐段检查符号链接，避免 "根目录内的软链指向根目录外"。

已知局限
--------
``resolve()`` 与随后的 ``open()`` 之间存在 TOCTOU 窗口。v1 通过
"服务端以专用低权限用户运行 + 逐段 symlink 检查" 缓解；彻底解决需要
``os.open(O_NOFOLLOW)`` + ``dir_fd`` 逐段打开，留待后续加固。
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Union

__all__ = [
    "PathValidationError",
    "validate_relpath",
    "safe_join",
    "slugify",
    "is_within",
]

PathLike = Union[str, os.PathLike]


class PathValidationError(ValueError):
    """客户端提供的路径不合法。调用方应转成 400 响应。"""


# Windows 保留设备名。目标机是 Linux，但服务端可能跑在 Windows 开发机上，
# 且这类名字落盘后极易造成困惑，一律拒绝。
_WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)

_SLUG_STRIP = re.compile(r"[^A-Za-z0-9._-]+")


def is_within(child: PathLike, parent: PathLike) -> bool:
    """``child`` 是否位于 ``parent`` 之内（含相等）。

    用 ``os.path.commonpath`` 而非 ``Path.is_relative_to``，后者是 Python 3.9+。
    """
    child_p = Path(child).resolve()
    parent_p = Path(parent).resolve()
    try:
        common = os.path.commonpath([str(child_p), str(parent_p)])
    except ValueError:
        # 不同盘符（Windows）
        return False
    return common == str(parent_p)


def validate_relpath(raw: str, max_length: int = 1024) -> str:
    """校验并归一化一个 POSIX 相对路径，返回规范化后的字符串。

    校验通过不代表落盘安全 —— 落盘前还要过 :func:`safe_join` 做符号链接检查。
    """
    if not isinstance(raw, str):
        raise PathValidationError("路径必须是字符串")
    if not raw:
        raise PathValidationError("路径为空")
    if raw != raw.strip():
        raise PathValidationError("路径首尾不允许空白字符")
    if len(raw) > max_length:
        raise PathValidationError("路径过长（上限 %d 字节）" % max_length)
    if "\x00" in raw:
        raise PathValidationError("路径含 NUL 字节")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise PathValidationError("路径含控制字符")
    if "\\" in raw:
        # 反斜杠是 Windows 分隔符。混用会绕过基于 "/" 的分段检查，
        # 直接拒绝，强制客户端只发 POSIX 相对路径。
        raise PathValidationError("路径必须是 POSIX 相对路径，不接受反斜杠")
    if raw.startswith("/"):
        raise PathValidationError("不接受绝对路径")
    # Windows 盘符 / UNC 的残留形态
    if len(raw) >= 2 and raw[1] == ":":
        raise PathValidationError("不接受盘符路径")

    parts = []
    for seg in raw.split("/"):
        if seg == "":
            raise PathValidationError("路径含空段（重复的 /）")
        if seg in (".", ".."):
            raise PathValidationError("路径含 . 或 .. 段")
        if seg.startswith("~"):
            raise PathValidationError("路径段不允许以 ~ 开头")
        if seg != seg.strip():
            # Windows 会静默去掉结尾的点和空格，导致 "a. " 与 "a" 冲突
            raise PathValidationError("路径段首尾不允许空白字符")
        if seg.endswith("."):
            raise PathValidationError("路径段不允许以 . 结尾")
        stem = seg.split(".", 1)[0].lower()
        if stem in _WINDOWS_RESERVED:
            raise PathValidationError("路径段使用了系统保留名: %s" % seg)
        if len(seg.encode("utf-8")) > 255:
            raise PathValidationError("单个路径段过长: %s" % seg)
        # 归一化到 NFC，避免同形不同码点产生两个"相同"文件
        parts.append(unicodedata.normalize("NFC", seg))

    return "/".join(parts)


def safe_join(root: PathLike, rel: str, max_length: int = 1024) -> Path:
    """把经过校验的相对路径拼到 ``root`` 下，返回可安全写入的绝对路径。

    会逐段拒绝符号链接，并在最后再确认一次结果落在根目录内。
    """
    rel = validate_relpath(rel, max_length=max_length)
    root_resolved = Path(root).resolve()
    root_resolved.mkdir(parents=True, exist_ok=True)

    cursor = root_resolved
    for seg in PurePosixPath(rel).parts:
        cursor = cursor / seg
        try:
            if cursor.is_symlink():
                raise PathValidationError("路径经过符号链接，已拒绝: %s" % seg)
        except OSError as exc:  # pragma: no cover - 取决于文件系统
            raise PathValidationError("无法检查路径段 %s: %s" % (seg, exc))

    target = cursor.resolve()
    if not is_within(target, root_resolved):
        raise PathValidationError("路径逃逸出根目录: %s" % rel)
    return target


def slugify(text: str, fallback: str = "x", max_length: int = 64) -> str:
    """把场次名 / 选手编号转成可安全用作目录名的 slug。

    输出只含 ``[A-Za-z0-9._-]``，且不会以 ``.`` 开头。
    """
    if not isinstance(text, str):
        text = str(text)
    normalized = unicodedata.normalize("NFKD", text)
    slug = _SLUG_STRIP.sub("-", normalized)
    # `..` 是目录穿越语义，绝不能出现在结果里。连续点一律压成连字符后再合并
    # 连续连字符，最后的结果只可能含单个 `.`。
    slug = re.sub(r"\.{2,}", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-._")
    if not slug:
        slug = fallback
    if slug.startswith("."):
        slug = "_" + slug.lstrip(".")
    return slug[:max_length].strip("-._") or fallback
