"""服务端生成的 checker 与 testlib.h 注入。

* :func:`real_checker` —— 实数比较（Lemon mode 3、Hydro ``rcmpN``）：绝对误差**或**相对误差
  不超过 eps 即通过（Hydro ``rcmp`` 只比绝对误差）；不依赖 testlib，退出码 0/1，stderr 写 ``ok`` / ``wrong answer``。
* :func:`hustoj_wrapper` —— HUSTOJ spj（参数 in std user）套外壳，对调成 testlib 顺序。
* :func:`inject_testlib` —— checker ``#include "testlib.h"`` 而包里没有时补上随仓库带的那份
  （:func:`injected_testlib`：上游原文前加一段注明来源、版本与 MIT 许可全文的注释）。

模板在 ``judging/checkers/``，testlib.h 在 ``judging/vendor/``（pyproject 的 package-data 声明）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from .bundle import FileRef, Notice
from .notices import notice

__all__ = [
    "TESTLIB_NAME",
    "real_checker",
    "format_eps",
    "hustoj_wrapper",
    "HUSTOJ_SOURCE_NAME",
    "uses_testlib",
    "vendor_testlib",
    "injected_testlib",
    "inject_testlib",
    "checker_language_ok",
]

_JUDGING = Path(__file__).resolve().parents[2] / "judging"
TEMPLATE_DIR = _JUDGING / "checkers"
VENDOR_DIR = _JUDGING / "vendor"
TESTLIB_NAME = "testlib.h"
TESTLIB_LICENSE_NAME = "LICENSE-testlib"
TESTLIB_UPSTREAM = "https://github.com/MikeMirzayanov/testlib"
HUSTOJ_SOURCE_NAME = "spj_src.h"

_TESTLIB_INCLUDE = re.compile(rb'#[ \t]*include[ \t]*[<"][ \t]*testlib\.h[ \t]*[>"]')
_CHECKER_SUFFIXES = (".cpp", ".cc", ".cxx", ".c")


def _template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def format_eps(eps: float) -> str:
    """C++ 字面量：``1e-06``、``0.001``。"""
    if not eps > 0:
        raise ValueError("eps 必须为正数")
    return repr(float(eps))


def real_checker(eps: float, relative: bool = True) -> bytes:
    """实数比较 checker 的源码（写成 checker.cpp）。``relative=False`` 只比绝对误差（testlib ``rcmp``）。"""
    text = _template("realcmp.cpp.tmpl").replace("{{EPS}}", format_eps(eps))
    return text.replace("{{RELATIVE}}", "true" if relative else "false").encode("utf-8")


def hustoj_wrapper(spj_source: bytes) -> Tuple[bytes, Dict[str, FileRef]]:
    """HUSTOJ spj → (checker.cpp 源码, 支持文件 {``spj_src.h``: 原 spj})。"""
    wrapper = _template("hustoj_wrap.cpp.tmpl").encode("utf-8")
    return wrapper, {HUSTOJ_SOURCE_NAME: FileRef(data=spj_source)}


def uses_testlib(source: bytes) -> bool:
    return bool(_TESTLIB_INCLUDE.search(source))


def vendor_testlib() -> Optional[bytes]:
    """随仓库带的 testlib.h；文件缺失（打包时漏了）返回 None。"""
    try:
        return (VENDOR_DIR / TESTLIB_NAME).read_bytes()
    except OSError:
        return None


_TESTLIB_VERSION = re.compile(rb'^[ \t]*#[ \t]*define[ \t]+VERSION[ \t]+"([^"\r\n]*)"', re.M)


def injected_testlib() -> Optional[bytes]:
    """补进题目数据包的 testlib.h：上游原文前加一段 C 注释，写明来源、版本与 MIT 许可全文。

    这份副本会随数据包、导出包分发给第三方；MIT 要求副本附带版权与许可声明，而上游
    testlib.h 头部只有一行 ``Copyright (c) 2005-2026``。仓库里的 vendor/testlib.h 本身
    保持与上游逐字节一致。testlib.h 或许可证文件缺失时返回 None（按缺 testlib 处理）。
    注释用纯 ASCII，免得非 UTF-8 区域设置下的编译器告警。
    """
    data = vendor_testlib()
    try:
        license_text = (VENDOR_DIR / TESTLIB_LICENSE_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    if data is None:
        return None
    match = _TESTLIB_VERSION.search(data)
    version = match.group(1).decode("ascii", "replace") if match else "unknown"
    lines = [
        "/*",
        " * testlib.h, bundled with this problem's test data by SyncOJ.",
        " * Source:  %s" % TESTLIB_UPSTREAM,
        " * Version: %s" % version,
        " * The upstream file follows this comment unmodified.",
        " * It is distributed under the MIT License; full text below.",
        " *",
    ]
    for line in license_text.replace("*/", "* /").strip().splitlines():
        lines.append((" * " + line).rstrip())
    lines.append(" */")
    return ("\n".join(lines) + "\n").encode("utf-8") + data


def inject_testlib(checker_source: bytes, support: Dict[str, FileRef]) -> Optional[Notice]:
    """checker 用了 testlib 而 ``support`` 里没有 testlib.h：补上并返回 ``testlib_injected``；
    随仓库的那份也找不到时返回 ``testlib_missing``（不崩）。不需要补时返回 None。"""
    if not uses_testlib(checker_source):
        return None
    if any(name.lower() == TESTLIB_NAME for name in support):
        return None
    data = injected_testlib()
    if data is None:
        return notice("testlib_missing")
    support[TESTLIB_NAME] = FileRef(data=data)
    return notice("testlib_injected")


def checker_language_ok(filename: str) -> bool:
    """checker 只收 C/C++ 源码（``.c`` 按 C++ 编译）。"""
    return filename.lower().endswith(_CHECKER_SUFFIXES)
