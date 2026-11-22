"""服务端生成的 checker 与 testlib.h 注入。

* :func:`real_checker` —— 实数比较（Lemon mode 3、Hydro ``rcmpN``）：绝对误差**或**相对误差
  不超过 eps 即通过；不依赖 testlib，退出码 0/1，stderr 写 ``ok`` / ``wrong answer``。
* :func:`hustoj_wrapper` —— HUSTOJ spj（参数 in std user）套外壳，对调成 testlib 顺序。
* :func:`inject_testlib` —— checker ``#include "testlib.h"`` 而包里没有时补上随仓库带的那份。

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
    "inject_testlib",
    "checker_language_ok",
]

_JUDGING = Path(__file__).resolve().parents[2] / "judging"
TEMPLATE_DIR = _JUDGING / "checkers"
VENDOR_DIR = _JUDGING / "vendor"
TESTLIB_NAME = "testlib.h"
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


def real_checker(eps: float) -> bytes:
    """实数比较 checker 的源码（写成 checker.cpp）。"""
    return _template("realcmp.cpp.tmpl").replace("{{EPS}}", format_eps(eps)).encode("utf-8")


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


def inject_testlib(checker_source: bytes, support: Dict[str, FileRef]) -> Optional[Notice]:
    """checker 用了 testlib 而 ``support`` 里没有 testlib.h：补上并返回 ``testlib_injected``；
    随仓库的那份也找不到时返回 ``testlib_missing``（不崩）。不需要补时返回 None。"""
    if not uses_testlib(checker_source):
        return None
    if any(name.lower() == TESTLIB_NAME for name in support):
        return None
    data = vendor_testlib()
    if data is None:
        return notice("testlib_missing")
    support[TESTLIB_NAME] = FileRef(data=data)
    return notice("testlib_injected")


def checker_language_ok(filename: str) -> bool:
    """checker 只收 C/C++ 源码（``.c`` 按 C++ 编译）。"""
    return filename.lower().endswith(_CHECKER_SUFFIXES)
