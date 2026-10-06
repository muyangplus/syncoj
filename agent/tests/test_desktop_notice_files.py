"""桌面提示文件：**内容一致就不重写、不记日志**（心跳 30 秒之后日志会被刷满）。

现场：06:15 → 06:21 每分钟一行「等待场次说明 已写到 …/等待场次.txt」。行为本身是
刻意的（教师可能刚开机才看桌面，文件被删掉/改坏要能自愈），但心跳一改成 30 秒，
一天就是几千行，而 ``agent.log`` 4 MB 就轮转 —— 真正要看的"状态变化"会被冲掉。

口径：
* 写之前先比内容，**一致就什么都不做**；
* 文件被删掉 = 读不到 = 不一致 → 自然补写（自愈性质不变）；
* 内容真变了才写 + 记一行 —— 那条日志的价值在"状态（或内容）变了"。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

import syncoj_agent.main as main_module  # noqa: E402
from syncoj_agent.config import AgentConfig  # noqa: E402
from syncoj_agent.main import Agent  # noqa: E402
from syncoj_agent.state import STATE_READY, STATE_UNCLAIMED, STATE_WAITING  # noqa: E402


@pytest.fixture()
def agent(workdir: Path) -> Agent:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code"]
    config.server_url = "https://127.0.0.1:8000"
    for path in (config.state_dir, config.deploy_root, config.scan_roots[0]):
        path.mkdir(parents=True, exist_ok=True)
    return Agent(config)


@pytest.fixture()
def writes(monkeypatch) -> List[str]:
    """数一数**真正写盘**了几次（放行真实写入：第二次比较要读到第一次的内容）。"""
    real = main_module.atomic_write_text
    calls: List[str] = []

    def spy(path, text, mode=0o600):
        calls.append(str(path))
        return real(path, text, mode=mode)

    monkeypatch.setattr(main_module, "atomic_write_text", spy)
    return calls


def log_lines(records) -> List[str]:
    return [record.getMessage() for record in records if "已写到" in record.getMessage()]


def unclaimed(code: str = "482913") -> SimpleNamespace:
    return SimpleNamespace(state=STATE_UNCLAIMED, pair_code=code, player_no="", contest_slug="")


def waiting() -> SimpleNamespace:
    return SimpleNamespace(state=STATE_WAITING, pair_code="", player_no="S001", contest_slug="")


def test_未配对时配对码文件只写一次(
    agent: Agent, writes: List[str], capture_logs
) -> None:
    records = capture_logs("syncoj.agent")
    path = agent._pair_code_path()

    agent._show_state_files(unclaimed(), "")  # 第 1 轮：写
    agent._show_state_files(unclaimed(), "")  # 第 2 轮：内容没变 → 不写
    agent._show_state_files(unclaimed(), "")  # 第 3 轮：同上

    assert writes == [str(path)], "同一个内容被重复写了：%r" % writes
    assert len(log_lines(records)) == 1, log_lines(records)
    assert path.is_file()


def test_配对码文件被删掉之后会自动补写(
    agent: Agent, writes: List[str], capture_logs
) -> None:
    """**自愈不能丢**：读不到 = 不一致 → 下一轮补回来。"""
    records = capture_logs("syncoj.agent")
    path = agent._pair_code_path()

    agent._show_state_files(unclaimed(), "")
    path.unlink()
    agent._show_state_files(unclaimed(), "")

    assert writes == [str(path), str(path)], "删掉之后没有补写：%r" % writes
    assert path.is_file()
    assert len(log_lines(records)) == 2, log_lines(records)


def test_配对码变了要重写(agent: Agent, writes: List[str]) -> None:
    """服务端换了一个新配对码 → 内容变了 → 必须写。"""
    agent._show_state_files(unclaimed("111111"), "")
    agent._show_state_files(unclaimed("222222"), "")

    assert len(writes) == 2, writes
    assert "222222" in agent._pair_code_path().read_text(encoding="utf-8")


def test_等待场次文件只写一次_但_reason_变了要重写(
    agent: Agent, writes: List[str], capture_logs
) -> None:
    records = capture_logs("syncoj.agent")
    path = agent._waiting_file_path()

    agent._show_state_files(waiting(), "这场没有你")
    agent._show_state_files(waiting(), "这场没有你")
    assert writes == [str(path)], "同样的说明被重复写了：%r" % writes
    assert len(log_lines(records)) == 1

    agent._show_state_files(waiting(), "名单还没应用到场次里")
    assert len(writes) == 2, "服务端换了原因却还在用旧文件：%r" % writes
    assert "名单还没应用" in path.read_text(encoding="utf-8")


def test_能干活之后不再重复写也不刷日志(
    agent: Agent, writes: List[str], capture_logs
) -> None:
    """READY 时两个提示文件都该消失，而且**不产生"已写到"日志**。

    注意这里**不**断言 unlink 只调一次：``_remove_desktop_file`` 对已经不存在的
    文件会撞一次 ``FileNotFoundError`` 然后咽掉（那是刻意的幂等写法，也不记日志）。
    这一批要治的是"重复写 + 每轮一行 INFO"，不是那两次系统调用。
    """
    records = capture_logs("syncoj.agent")
    agent._show_state_files(unclaimed(), "")
    agent._show_state_files(unclaimed(), "")
    assert len(writes) == 1

    ready = SimpleNamespace(state=STATE_READY, pair_code="")
    agent._show_state_files(ready, "")
    after_first = len(log_lines(records))
    agent._show_state_files(ready, "")

    assert not agent._pair_code_path().exists()
    assert not agent._waiting_file_path().exists()
    assert len(log_lines(records)) == after_first == 1, log_lines(records)
