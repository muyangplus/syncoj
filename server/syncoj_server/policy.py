"""文件扫描策略。

**服务端是权威定义**，通过 tick 响应下发给 Agent；Agent 侧另有一份等价的内置
默认（它必须零依赖、且随 Agent 独立分发，不能 import 本模块）。

两侧一致性由 ``server/tests/test_agent_contract.py`` 的契约测试保证：契约测试会
用 Agent 自己的代码生成 payload，再用服务端模型校验。
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = [
    "DEFAULT_EXTENSIONS",
    "DEFAULT_EXCLUDE_DIRS",
    "DEFAULT_EXCLUDE_SUFFIXES",
    "build_policy",
]

#: 允许回收的源码后缀。注意全部小写，比较时对文件名做 lower()。
DEFAULT_EXTENSIONS: List[str] = [
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".pas",
    ".pp",
    ".java",
    ".py",
    ".pyw",
    ".kt",
    ".cs",
    ".go",
    ".rs",
    ".js",
    ".ts",
]

#: 整目录跳过（按目录名精确匹配，大小写不敏感）。
DEFAULT_EXCLUDE_DIRS: List[str] = [
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    "cmake-build-debug",
    "cmake-build-release",
    ".cache",
    "target",
]

#: 文件名后缀跳过（编辑器临时文件等，大小写不敏感）。
DEFAULT_EXCLUDE_SUFFIXES: List[str] = [
    ".swp",
    ".swx",
    ".tmp",
    ".temp",
    ".bak",
    ".orig",
    ".rej",
    ".pyc",
    ".pyo",
    "~",
]


def build_policy(max_file_size: int, scan_interval: int, max_files: int) -> Dict[str, Any]:
    """构造下发给 Agent 的策略片段。"""
    return {
        "extensions": list(DEFAULT_EXTENSIONS),
        "exclude_dirs": list(DEFAULT_EXCLUDE_DIRS),
        "exclude_suffixes": list(DEFAULT_EXCLUDE_SUFFIXES),
        "max_file_size": int(max_file_size),
        "scan_interval": int(scan_interval),
        "max_files": int(max_files),
        "policy_version": 1,
    }
