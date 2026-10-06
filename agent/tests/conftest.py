"""Agent 测试夹具。

Agent 本身零依赖，测试需要 pytest —— 但 pytest 只在开发机上用，
不会出现在考试机。``agent/pyproject.toml`` 也只声明 pytest 配置，不声明依赖。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import uuid
from pathlib import Path
from typing import Iterator, List

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


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.items: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 平凡
        self.items.append(record)


@pytest.fixture()
def capture_logs():
    """按 logger 名收集日志：``records = capture_logs("syncoj.agent.client")``。

    为什么不直接用 ``caplog``：有别的用例会调 ``setup_logging()``，而那一句是
    ``root.handlers = []`` —— pytest 挂在 root 上的捕获 handler 会被一起换掉，
    于是 caplog 之后什么都看不到（单独跑绿、全量跑红，而且报的是"日志条数是 0"）。
    挂在具体 logger 上的 handler 不受影响。
    """

    def factory(name: str) -> List[logging.LogRecord]:
        logger = logging.getLogger(name)
        handler = _LogCollector()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        installed.append((logger, handler, old_level))
        return handler.items

    installed = []
    try:
        yield factory
    finally:
        for logger, handler, old_level in installed:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


@pytest.fixture(scope="session")
def installer_module():
    """``packaging/install.py`` —— **整个测试会话只加载一次**。

    它不是一个包（``packaging/`` 不在 Agent 的 import 路径上），只能按路径加载，
    而"按路径加载"带来过一个真实的坑：三个测试文件各自
    ``spec_from_file_location("syncoj_installer", ...)`` + ``sys.modules.setdefault``。

    于是**先加载的那个赢**：后面那个文件拿到的其实是最先那个模块对象，而
    ``test_server_address.py`` 里有一处 monkeypatch 是打在
    ``sys.modules["syncoj_installer"].render_config`` 上的 —— 当它拿到的是**另一个**
    模块对象时，补丁打在了空气上，被测代码照旧调用真的 ``render_config``，
    结果是一个 `KeyError: 'server_url'`。

    那种失败只在**全量跑**时出现（单独跑那个文件是绿的），而且报错和被测代码毫无
    关系。所以加载器收到这里，只此一份，谁也别再 ``setdefault``。
    """
    path = AGENT_ROOT / "packaging" / "install.py"
    spec = importlib.util.spec_from_file_location("syncoj_installer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["syncoj_installer"] = module
    spec.loader.exec_module(module)
    return module
