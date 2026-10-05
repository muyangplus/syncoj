"""Agent 测试夹具。

Agent 本身零依赖，测试需要 pytest —— 但 pytest 只在开发机上用，
不会出现在考试机。``agent/pyproject.toml`` 也只声明 pytest 配置，不声明依赖。
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

REPO_ROOT = AGENT_ROOT.parent
TMP_ROOT = REPO_ROOT / ".pytest-tmp" / "agent"


@pytest.fixture()
def workdir() -> Iterator[Path]:
    """仓库内的临时目录。

    不用 ``tempfile``：受限环境下它对新建目录做 chmod 会失败，反而留下删不掉的
    残留目录污染 git。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / uuid.uuid4().hex[:12]
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_tree(root: Path, files) -> None:
    """按 ``{"相对路径": b"内容"}`` 建目录树。"""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
