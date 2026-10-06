"""内置扫描策略默认值。

服务端在 tick 响应里下发的 ``config`` 是权威值；这里是**离线兜底** ——
Agent 第一次启动、还没成功 tick 过的时候必须能工作，否则会陷入
"要先 tick 才能拿到策略，但要 tick 又需要策略" 的死循环。

两侧一致性由 ``server/tests/test_agent_contract.py`` 的契约测试保证：
该测试会断言服务端默认策略与这里的默认值逐项相等。
"""

from __future__ import annotations

from typing import Dict, List

__all__ = ["DEFAULT_POLICY", "merge_policy"]

#: 允许回收的源码后缀（全小写）
DEFAULT_EXTENSIONS: List[str] = [
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".pas", ".pp", ".java", ".py", ".pyw",
    ".kt", ".cs", ".go", ".rs", ".js", ".ts",
]

#: 整目录跳过（按目录名精确匹配，大小写不敏感）
DEFAULT_EXCLUDE_DIRS: List[str] = [
    ".git", ".svn", ".hg", ".idea", ".vscode", "__pycache__",
    "node_modules", "build", "dist",
    "cmake-build-debug", "cmake-build-release", ".cache", "target",
]

#: 文件名后缀跳过（编辑器临时文件等）
DEFAULT_EXCLUDE_SUFFIXES: List[str] = [
    ".swp", ".swx", ".tmp", ".temp", ".bak", ".orig", ".rej", ".pyc", ".pyo", "~",
]

DEFAULT_POLICY: Dict[str, object] = {
    "extensions": DEFAULT_EXTENSIONS,
    "exclude_dirs": DEFAULT_EXCLUDE_DIRS,
    "exclude_suffixes": DEFAULT_EXCLUDE_SUFFIXES,
    "max_file_size": 2 * 1024 * 1024,
    #: 心跳兜底间隔（秒）。**扫描是每轮 cycle 都跑的**（没有独立节流），这个值
    #: 只在"服务端没给 next_tick_seconds"时当兜底等待用（见 main.py `_clamp_wait`）。
    #: 与 config.py / config.example.ini / 安装器模板保持一致（都是 30）。
    "scan_interval": 30,
    "max_files": 5000,
    "policy_version": 1,
}


def merge_policy(remote: object) -> Dict[str, object]:
    """合并服务端下发的策略。

    对每个键做独立校验：**某一项不合法只回退该项**，不整体退回默认值 ——
    否则服务端一个新字段的把关疏漏会让整个策略退化，副作用太大。
    """
    merged = dict(DEFAULT_POLICY)
    if not isinstance(remote, dict):
        return merged

    list_keys = ("extensions", "exclude_dirs", "exclude_suffixes")
    for key in list_keys:
        value = remote.get(key)
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            merged[key] = [v.lower() for v in value]

    for key in ("max_file_size", "scan_interval", "max_files"):
        value = remote.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            merged[key] = value

    return merged
