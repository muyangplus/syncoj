#!/usr/bin/env python3
"""Python 3.8 兼容门禁。

背景
----
开发机是 Python 3.14，目标机是 NOI Linux 2.0 自带的 Python 3.8.10。
两者相差 6 个大版本，光靠人眼审查必然漏。这个脚本把"能不能在 3.8 上跑"
拆成三类可机械判定的检查：

1. **语法**：``ast.parse(feature_version=(3, 8))`` —— 直接让 3.8 的语法规则
   来判定，比任何手写规则都准（能挡住 ``match``、括号式上下文管理器等）。
2. **标准库可导入性**：import 的模块名必须在 Python 3.8 的标准库清单里
   （挡 ``tomllib``/``zoneinfo``/``graphlib`` 这类 3.9+ 才有的模块）。
3. **API 存在性**：按黑名单拦截 3.9+ 才有的属性调用
   （挡 ``str.removeprefix``、``Path.is_relative_to``、``functools.cache`` 等
   语法合法但运行期 AttributeError 的坑）。

用法::

    python agent/tools/check_py38.py agent/     # 从仓库根目录
    python tools/check_py38.py                  # 从 agent/ 目录（默认查整个 agent/）

退出码 0 表示通过，1 表示发现问题。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

#: 不给参数时的检查范围：本脚本所在的 ``agent/`` 目录。
#:
#: 用 ``__file__`` 推导而不是写死字符串 ``"agent"`` —— 后者只在**仓库根目录**下
#: 跑得通，而从 ``agent/`` 里跑（最自然的位置）会得到一句"没有找到待检查的 .py
#: 文件"，于是门禁自己成了需要排查的东西。门禁一旦让人费解，就会被绕过去。
DEFAULT_TARGET = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #

#: Python 3.8 有、而 3.9+ 新增的标准库模块。出现在 import 里即失败。
MODULES_ADDED_AFTER_38: Set[str] = {
    "graphlib",       # 3.9
    "zoneinfo",       # 3.9
    "tomllib",        # 3.11
    # 注意：importlib.metadata 是 3.8 就有的，不在此列
}

#: 语法上合法、但 3.8 运行期不存在的属性名。按 (对象提示, 属性名) 匹配，
#: 对象提示为 None 表示"任意对象上出现该属性名都算"。
ATTRS_ADDED_AFTER_38: List[Tuple[str, str]] = [
    (None, "removeprefix"),      # 3.9
    (None, "removesuffix"),      # 3.9
    (None, "is_relative_to"),    # 3.9  pathlib
    (None, "with_stem"),         # 3.9  pathlib
    (None, "pairwise"),          # 3.10 itertools
    (None, "batched"),           # 3.12 itertools
    (None, "to_thread"),         # 3.9  asyncio
    (None, "TaskGroup"),         # 3.11 asyncio
    (None, "aiter"),             # 3.10 内置
    (None, "anext"),             # 3.10 内置
    (None, "except_star"),       # 3.11
    ("functools", "cache"),      # 3.9
    ("typing", "Self"),          # 3.11
    ("typing", "TypeAlias"),     # 3.10
    ("typing", "ParamSpec"),     # 3.10
    ("enum", "StrEnum"),         # 3.11
    ("contextlib", "aclosing"),  # 3.10
    ("datetime", "UTC"),         # 3.11
]

#: 这些名字在 3.8 的对应模块里不存在。必须单独检查 ——
#: ``from itertools import pairwise`` 的模块名 ``itertools`` 完全合法，
#: 只有导入的**名字**是新的，光看模块名会漏掉。
#:
#: 注意：只列真正缺失的名字。误报比漏报更糟 —— 一旦开发者发现门禁会冤枉
#: 正常代码，就会开始绕过它，门禁随即失去意义。
NAMES_ADDED_AFTER_38: Dict[str, Set[str]] = {
    "itertools": {"pairwise", "batched"},          # 3.10 / 3.12
    "functools": {"cache"},                        # 3.9
    "asyncio": {"to_thread", "TaskGroup", "Runner"},  # 3.9 / 3.11
    "contextlib": {"aclosing"},                    # 3.10
    "typing": {                                    # 3.10 / 3.11
        "Self", "TypeAlias", "ParamSpec", "TypeVarTuple",
        "Required", "NotRequired", "LiteralString", "Never", "assert_never",
    },
    "datetime": {"UTC"},                           # 3.11
    "enum": {"StrEnum", "ReprEnum", "EnumCheck", "FlagBoundary"},  # 3.11
    "math": {"nextafter", "ulp", "cbrt", "exp2", "fma"},           # 3.9
}

#: 允许被 import 的顶层模块（Python 3.8 标准库 + 本包自身）。
#: 用白名单而非黑名单：新增一个 3.9+ 模块时不需要改这个脚本。
ALLOWED_TOP_LEVEL: Set[str] = {
    # 本包
    "syncoj_agent",
    # 测试里用作**参照实现**：Agent 的签名验证器需要拿 openssl/服务端签名器
    # 作为 oracle 来交叉验证，自签自验证明不了任何事。测试代码不随 Agent 分发，
    # 因此这里放行不影响"Agent 零依赖"这一约束。
    "syncoj_server",
    # __future__
    "__future__",
    # 纯 Python 核心
    "abc", "argparse", "ast", "asyncio", "base64", "binascii", "bisect",
    "calendar", "codecs", "collections", "concurrent", "configparser",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime",
    "decimal", "difflib", "enum", "errno", "fcntl", "fnmatch", "fractions",
    "functools", "getpass", "glob", "grp", "gzip", "hashlib", "heapq",
    "hmac", "html", "http", "importlib", "inspect", "io", "ipaddress",
    "itertools", "json", "keyword", "linecache", "locale", "logging",
    "lzma", "marshal", "math", "mimetypes", "multiprocessing", "netrc",
    "numbers", "operator", "os", "pathlib", "pdb", "pickle", "pkgutil",
    "platform", "plistlib", "pprint", "pwd", "queue", "random", "re",
    "resource", "secrets", "select", "selectors", "shlex", "shutil",
    "signal", "site", "smtplib", "socket", "socketserver", "sqlite3",
    "ssl", "stat", "statistics", "string", "struct", "subprocess", "sys",
    "syslog", "tarfile", "tempfile", "textwrap", "threading", "time",
    "timeit", "tokenize", "traceback", "types", "typing", "unicodedata",
    "unittest", "urllib", "uuid", "venv", "warnings", "weakref",
    "webbrowser", "winreg", "xml", "zipfile", "zlib",
    # 本脚本与测试工具需要的（开发机侧，不随 Agent 分发）
    "pytest",
}


class Issue:
    def __init__(self, path: Path, lineno: int, kind: str, message: str) -> None:
        self.path = path
        self.lineno = lineno
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return "%s:%d: [%s] %s" % (self.path.as_posix(), self.lineno, self.kind, self.message)


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #


def check_syntax(path: Path, source: str) -> List[Issue]:
    try:
        ast.parse(source, filename=str(path), feature_version=(3, 8))
    except SyntaxError as exc:
        return [
            Issue(path, exc.lineno or 0, "syntax", "%s（该语法 3.8 不支持）" % exc.msg)
        ]
    return []


def check_tree(
    path: Path,
    tree: ast.AST,
    local_modules: Set[str] = frozenset(),
    allowed_top_level: Set[str] = ALLOWED_TOP_LEVEL,
) -> List[Issue]:
    issues: List[Issue] = []

    def _is_known(top: str) -> bool:
        """本地模块（同目录的 conftest.py、本包内的模块）不算第三方依赖。"""
        return top in allowed_top_level or top in local_modules

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in MODULES_ADDED_AFTER_38:
                    issues.append(
                        Issue(path, node.lineno, "import", "模块 %s 在 3.8 中不存在" % top)
                    )
                elif not _is_known(top):
                    issues.append(
                        Issue(path, node.lineno, "import", "非标准库或未登记模块: %s" % top)
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # 相对导入，属本包
            top = (node.module or "").split(".")[0]
            if top in MODULES_ADDED_AFTER_38:
                issues.append(
                    Issue(path, node.lineno, "import", "模块 %s 在 3.8 中不存在" % top)
                )
            elif top and not _is_known(top):
                issues.append(
                    Issue(path, node.lineno, "import", "非标准库或未登记模块: %s" % top)
                )
            else:
                blocked = NAMES_ADDED_AFTER_38.get(top, set())
                for alias in node.names:
                    if alias.name in blocked:
                        issues.append(
                            Issue(
                                path,
                                node.lineno,
                                "api",
                                "%s.%s 在 3.8 中不存在（3.9+ API）" % (top, alias.name),
                            )
                        )
        elif isinstance(node, ast.Attribute):
            for obj_hint, name in ATTRS_ADDED_AFTER_38:
                if node.attr != name:
                    continue
                if obj_hint is None or _receiver_name(node.value) == obj_hint:
                    issues.append(
                        Issue(
                            path,
                            node.lineno,
                            "api",
                            ".%s 在 3.8 中不存在（3.9+ API）" % name,
                        )
                    )

    return issues


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def collect_targets(paths: Iterable[str]) -> List[Path]:
    found: List[Path] = []
    for raw in paths:
        target = Path(raw)
        if target.is_dir():
            found.extend(sorted(p for p in target.rglob("*.py") if _is_source(p)))
        elif target.suffix == ".py":
            found.append(target)
    return found


def _is_source(path: Path) -> bool:
    parts = set(path.parts)
    return not ({"__pycache__", ".venv", "venv", "build", "dist"} & parts)


def collect_local_modules(targets: List[Path]) -> Set[str]:
    """识别出"本地模块"的名字。

    ``tests/test_scan.py`` 里的 ``from conftest import make_tree`` 完全合法，
    但 ``conftest`` 既不是 Python 3.8 标准库也不是已登记模块。若不识别，
    门禁会对正常代码报假警 —— 而假警会让人开始绕过门禁，那它就废了。

    判定规则：待检查文件里存在 ``<name>.py``，或存在包含 ``__init__.py`` 的
    ``<name>/`` 目录，则 ``name`` 视为本地模块。
    """
    local: Set[str] = set()
    for path in targets:
        if path.name == "__init__.py":
            local.add(path.parent.name)
        else:
            local.add(path.stem)
            local.add(path.parent.name)
    return local


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="check_py38",
        description="检查代码是否兼容 Python 3.8",
    )
    parser.add_argument("paths", nargs="*", default=None, help="待检查的文件或目录")
    parser.add_argument(
        "--allow",
        default="",
        help="额外允许的顶层模块（逗号分隔），用于服务端这类本身就有第三方依赖的代码",
    )
    args = parser.parse_args(argv[1:])

    targets = collect_targets(args.paths or [str(DEFAULT_TARGET)])
    if not targets:
        print("没有找到待检查的 .py 文件（参数: %s）" % " ".join(args.paths or [str(DEFAULT_TARGET)]))
        return 1

    extra = {name.strip() for name in args.allow.split(",") if name.strip()}
    allowed = ALLOWED_TOP_LEVEL | extra
    local_modules = collect_local_modules(targets) | extra

    all_issues: List[Issue] = []
    for path in targets:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            all_issues.append(Issue(path, 0, "read", str(exc)))
            continue
        all_issues.extend(check_syntax(path, source))
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue  # 语法错误已在上面报过
        all_issues.extend(check_tree(path, tree, local_modules, allowed))

    if all_issues:
        print("Python 3.8 兼容性检查未通过：\n")
        for issue in all_issues:
            print("  " + str(issue))
        print("\n共 %d 处问题。" % len(all_issues))
        return 1

    print("Python 3.8 兼容性检查通过（%d 个文件）。" % len(targets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
