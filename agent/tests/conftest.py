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


@pytest.fixture(scope="session", autouse=True)
def _basetemp_must_not_be_our_workdir_root(pytestconfig) -> None:
    """``--basetemp`` 不能和这个文件里的 ``TMP_ROOT`` 是同一个目录。

    踩过一次，而且现象极具误导性：两者曾经都是 ``.pytest-tmp/agent``，于是
    pytest 在**第一次**用到 ``tmp_path`` 时会清空整个 basetemp —— 而那时里面
    已经躺着本次会话里别的测试的临时目录，其中 ``state/agent.log`` 还被日志
    处理器占着（Windows 上删不掉）。报出来是一串

        PermissionError: [WinError 32] 另一个程序正在使用此文件

    指向一个跟被测代码毫无关系的文件，而且只在某些测试**顺序**下出现。

    读的是命令行给的 ``--basetemp``，不是 ``tmp_path_factory.getbasetemp()``：
    后者会在夹具里**建目录**，而它的父目录不存在时抛的是
    ``FileNotFoundError`` —— 一个关于路径、与被测代码毫无关系的报错。
    """
    given = getattr(pytestconfig.option, "basetemp", None)
    if not given:
        return  # 没给 --basetemp 就落在系统临时目录里，不可能和我们撞
    base = Path(given).resolve()
    ours = TMP_ROOT.resolve()
    # 危险的是**basetemp 把 TMP_ROOT 包住**（或者就等于它）：pytest 清空 basetemp
    # 时会把 TMP_ROOT 连同里面正在用的临时目录一起删掉。
    # 反过来（basetemp 落在 TMP_ROOT 里面）是安全的 —— 清空只动它自己那一棵。
    dangerous = base == ours or base in ours.parents
    assert not dangerous, (
        "--basetemp（%s）把 TMP_ROOT（%s）包住了，或者就等于它。\n"
        "pytest 会清空 basetemp，于是这里正在用的临时目录会被一起删掉；\n"
        "表现是 Windows 上删文件失败引发的 PermissionError。\n"
        "请在 scripts/check.sh 里把 basetemp 指到 .pytest-tmp/basetemp/ 这类独立目录。"
        % (base, ours)
    )


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
