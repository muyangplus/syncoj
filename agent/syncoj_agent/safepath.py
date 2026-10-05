"""Agent 侧的路径安全校验。

**刻意不复用服务端的 ``syncoj_server.paths``**，哪怕两边逻辑高度相似。理由：

1. Agent 要独立分发，不能 import 服务端包。
2. 这是纵深防御。服务端是不可信输入来源之一（可能被攻破，也可能被中间人
   劫持）。若两边共用同一份实现，一个逻辑漏洞就同时洞穿两层；独立实现意味着
   攻击者必须同时找到两个实现的破绽。

代价是代码可能漂移。为此 ``server/tests/test_agent_contract.py`` 会用同一批
敌意输入同时喂给两份实现，断言判定结果一致 —— 有漂移就会红。
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Union

__all__ = ["PathError", "validate_relpath", "resolve_within"]

PathLike = Union[str, os.PathLike]

_WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)


class PathError(ValueError):
    """服务端下发的路径不可信。"""


def validate_relpath(raw: str, max_length: int = 1024) -> str:
    """校验并归一化一个 POSIX 相对路径。"""
    if not isinstance(raw, str):
        raise PathError("路径必须是字符串")
    if not raw:
        raise PathError("路径为空")
    if raw != raw.strip():
        raise PathError("路径首尾不允许空白字符")
    if len(raw) > max_length:
        raise PathError("路径过长")
    if "\x00" in raw:
        raise PathError("路径含 NUL 字节")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise PathError("路径含控制字符")
    if "\\" in raw:
        raise PathError("路径必须是 POSIX 相对路径，不接受反斜杠")
    if raw.startswith("/"):
        raise PathError("不接受绝对路径")
    if len(raw) >= 2 and raw[1] == ":":
        raise PathError("不接受盘符路径")

    parts = []
    for segment in raw.split("/"):
        if segment == "":
            raise PathError("路径含空段")
        if segment in (".", ".."):
            raise PathError("路径含 . 或 .. 段")
        if segment.startswith("~"):
            raise PathError("路径段不允许以 ~ 开头")
        if segment != segment.strip():
            raise PathError("路径段首尾不允许空白字符")
        if segment.endswith("."):
            raise PathError("路径段不允许以 . 结尾")
        if segment.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise PathError("路径段使用了系统保留名: %s" % segment)
        if len(segment.encode("utf-8")) > 255:
            raise PathError("单个路径段过长")
        parts.append(unicodedata.normalize("NFC", segment))

    return "/".join(parts)


def resolve_within(base: Path, rel: str, max_length: int = 1024) -> Path:
    """把 ``rel`` 解析到 ``base`` 之下，保证结果不逃逸。

    逐段拒绝符号链接：否则服务端（或中间人）可以先让 Agent 下发的合法文件
    覆盖成一个指向 ``/etc`` 的软链，后续写入就落到根目录外了。
    """
    rel = validate_relpath(rel, max_length=max_length)
    base_resolved = Path(base).resolve()
    base_resolved.mkdir(parents=True, exist_ok=True)

    cursor = base_resolved
    for segment in PurePosixPath(rel).parts:
        cursor = cursor / segment
        # 已存在的路径段不能是符号链接
        if cursor.is_symlink():
            raise PathError("路径经过符号链接，已拒绝: %s" % segment)

    target = cursor.resolve()
    try:
        common = os.path.commonpath([str(target), str(base_resolved)])
    except ValueError:
        raise PathError("路径逃逸出目标目录: %s" % rel)
    if common != str(base_resolved):
        raise PathError("路径逃逸出目标目录: %s" % rel)
    return target
