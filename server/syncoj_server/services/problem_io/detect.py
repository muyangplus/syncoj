"""识别导入包的格式。

预处理：``__MACOSX/``、``.DS_Store``、``.`` 开头的文件已由 :mod:`archive` 隐去；根目录只有
一个目录、没有文件时另备一个剥掉外壳的视图。每条规则先在原视图上试，再在剥壳视图上试，
首个命中即采用：

1. 非 zip：``<`` / ``<?xml`` 开头 → fps；``0x20111127`` 开头 → ``cdf_binary``；
   ``{`` 开头 → ``cdf_without_data``；其他 → ``unknown_format``
2. 根目录有 ``syncoj-problems.json`` → syncoj
3. 深度 ≤ 2 处有 ``*.cdf`` → lemonlime
4. 根目录 ``X.json`` + 同名目录 ``X/``，JSON 含 ``problem`` 与 ``samples`` → hoj
5. ``<n>/problem.json`` 含 ``test_case_score`` 或 ``rule_type`` → qduoj
6. ``problem.yaml`` / ``*/problem.yaml`` / ``[*/]testdata/config.{yaml,yml,ini}`` / ``data.yml`` → hydro
7. 只有一个 ``*.xml``，根元素 ``<fps>`` → fps
8. 只有成对的 ``.in`` / ``.ans`` → ``plain_testdata``
9. 其他 → ``unknown_format``
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple, Union

from .archive import ArchiveView, SafeArchive, SingleFileArchive, load_json
from .notices import ProblemIoError

__all__ = ["FORMAT_IDS", "detect", "views_of", "sniff_single"]

#: 识别得出的格式 id（与 ``problem_io.FORMATS`` 的键一致）
FORMAT_IDS = ("syncoj", "lemonlime", "hoj", "qduoj", "hydro", "fps")

NATIVE_MANIFEST = "syncoj-problems.json"
CDF_MAGIC = b"\x20\x11\x11\x27"
_HEAD = 4096


def views_of(archive: Union[SafeArchive, SingleFileArchive]) -> List[ArchiveView]:
    root = archive.view("")
    stripped = root.shell_stripped()
    return [root] + ([stripped] if stripped is not None else [])


def _head(view: ArchiveView, name: str) -> bytes:
    with view.open(name) as handle:
        return handle.read(_HEAD)


def _json_keys(view: ArchiveView, name: str) -> set:
    try:
        data = load_json(view.read(name))
    except (ValueError, UnicodeDecodeError, ProblemIoError):
        return set()
    return set(data) if isinstance(data, dict) else set()


def _strip_bom(data: bytes) -> bytes:
    return data[3:] if data.startswith(b"\xef\xbb\xbf") else data


def sniff_single(head: bytes) -> str:
    """非 zip 上传：返回 ``"fps"``，或抛对应的 :class:`ProblemIoError`。"""
    body = _strip_bom(head).lstrip()
    if head.startswith(CDF_MAGIC):
        raise ProblemIoError("cdf_binary")
    if body.startswith(b"<"):
        return "fps"
    if body.startswith(b"{"):
        raise ProblemIoError("cdf_without_data")
    raise ProblemIoError("unknown_format")


# ---- 各条规则：view -> bool ----


def _is_native(view: ArchiveView) -> bool:
    return view.exists(NATIVE_MANIFEST)


def _is_lemon(view: ArchiveView) -> bool:
    return any(n.lower().endswith(".cdf") and n.count("/") <= 1 for n in view.names)


def _is_hoj(view: ArchiveView) -> bool:
    dirs = set(view.root_dirs())
    for name in view.root_files():
        if name.lower().endswith(".json") and name[:-5] in dirs:
            if {"problem", "samples"} <= _json_keys(view, name):
                return True
    return False


_QDUOJ_JSON = re.compile(r"^\d+/problem\.json$")


def _is_qduoj(view: ArchiveView) -> bool:
    for name in view.names:
        if _QDUOJ_JSON.match(name):
            keys = _json_keys(view, name)
            if "test_case_score" in keys or "rule_type" in keys:
                return True
    return False


_HYDRO = re.compile(
    r"^(?:[^/]+/)?(?:problem\.yaml|testdata/config\.(?:yaml|yml|ini)|data\.yml)$"
)


def _is_hydro(view: ArchiveView) -> bool:
    return any(_HYDRO.match(n) for n in view.names)


def _is_fps(view: ArchiveView) -> bool:
    if len(view.names) != 1 or not view.names[0].lower().endswith(".xml"):
        return False
    head = _strip_bom(_head(view, view.names[0]))
    head = re.sub(rb"<\?.*?\?>|<!--.*?-->", b"", head, flags=re.S).lstrip()
    return head.startswith(b"<fps")


def _is_plain(view: ArchiveView) -> bool:
    lower = {n.lower() for n in view.names}
    for name in lower:
        if name.endswith(".in"):
            stem = name[:-3]
            if stem + ".ans" in lower or stem + ".out" in lower:
                return True
    return False


_RULES: List[Tuple[str, Callable[[ArchiveView], bool]]] = [
    ("syncoj", _is_native),
    ("lemonlime", _is_lemon),
    ("hoj", _is_hoj),
    ("qduoj", _is_qduoj),
    ("hydro", _is_hydro),
    ("fps", _is_fps),
]


def detect(
    archive: Union[SafeArchive, SingleFileArchive], forced: Optional[str] = None
) -> Tuple[str, ArchiveView]:
    """返回 ``(格式 id, 视图)``；识别不了抛 :class:`ProblemIoError`。

    ``forced``（非 ``auto``）时跳过识别：取那条规则命中的视图，都不命中就取最内层视图，
    交给该格式的解析器报具体错误。
    """
    if isinstance(archive, SingleFileArchive):
        found = sniff_single(_head(archive.view(""), archive.names[0]))
        if forced and forced != "auto" and forced != found:
            raise ProblemIoError("unknown_format")
        return found, archive.view("")

    views = views_of(archive)
    if forced and forced != "auto":
        rule = dict(_RULES).get(forced)
        if rule is None:
            raise ProblemIoError("unknown_format")
        for view in views:
            if rule(view):
                return forced, view
        return forced, views[-1]

    for format_id, rule in _RULES:
        for view in views:
            if rule(view):
                return format_id, view
    for view in views:
        if _is_plain(view):
            raise ProblemIoError("plain_testdata")
    if any(n.lower().endswith(".cdf") for n in archive.names):
        raise ProblemIoError("cdf_without_data")
    raise ProblemIoError("unknown_format")
