"""整轮看门狗：**任何一次阻塞调用都不许让心跳停摆超过 N 秒。**

真机现场：一次 keep-alive 竞态之后 Agent 静默挂死在一次 socket 读上（
``wchan=do_sys_poll``）、34 分钟没再心跳。给每个调用单独加超时挡不住这种事 ——
阻塞点太多，总有一个没被穷举到。所以在 ``run_forever`` 外面套一层 SIGALRM：

* 超时抛 :class:`RoundTimeout`，**中止这一轮**（不是退出进程），
  记一条 WARNING 后照常进入下一轮；
* 只有主线程能用 SIGALRM（``run_forever`` 就是主线程）；Windows 上没有
  ``setitimer``，那几条只是"不装"，功能不受影响；
* 与既有的 SIGTERM/SIGINT 是两条线：那两个置 ``_stop``（跑完这轮退出），
  SIGALRM 抛异常（这一轮不算）。
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

import syncoj_agent.main as main_module  # noqa: E402
from syncoj_agent.config import AgentConfig  # noqa: E402
from syncoj_agent.main import (  # noqa: E402
    Agent,
    RoundTimeout,
    arm_watchdog,
    disarm_watchdog,
    watchdog_supported,
)


@pytest.fixture()
def agent(workdir: Path) -> Agent:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code"]
    config.scan_interval = 30
    config.server_url = "https://127.0.0.1:8000"
    for path in (config.state_dir, config.deploy_root, config.scan_roots[0]):
        path.mkdir(parents=True, exist_ok=True)
    return Agent(config)


class FakeSignal:
    """假的 signal 模块：Windows 上没有 SIGALRM/``setitimer``，但被测的**接线**
    与平台无关 —— 装一个假的就能在任何平台上验证"该装的时候装了、该撤的时候撤了"。
    """

    SIGALRM = 14
    ITIMER_REAL = 0

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def signal(self, signum, handler):  # pragma: no cover - 平凡
        self.calls.append(("handler", signum, handler))

    def setitimer(self, which, seconds):  # pragma: no cover - 平凡
        self.calls.append(("timer", which, seconds))


def test_没有_sigalrm_的平台上不装也不报错(monkeypatch) -> None:
    monkeypatch.delattr(main_module.signal, "setitimer", raising=False)

    assert watchdog_supported() is False
    assert arm_watchdog(10) is False
    disarm_watchdog()  # 不抛


def test_装闹钟时显式钉好秒数(monkeypatch) -> None:
    fake = FakeSignal()
    monkeypatch.setattr(main_module, "signal", fake)

    assert arm_watchdog(42.5) is True
    assert ("handler", FakeSignal.SIGALRM, main_module._watchdog_handler) in fake.calls
    assert ("timer", FakeSignal.ITIMER_REAL, 42.5) in fake.calls

    disarm_watchdog()
    assert fake.calls[-1] == ("timer", FakeSignal.ITIMER_REAL, 0)


def test_升级那一段临时放宽看门狗(agent: Agent, monkeypatch) -> None:
    """``activate_release`` + 重启是刻意长时间的，拿"单轮 N 秒"去掐它是误伤。"""
    fake = FakeSignal()
    monkeypatch.setattr(main_module, "signal", fake)
    agent._watchdog_active = True

    with agent._watchdog_paused():
        assert ("timer", FakeSignal.ITIMER_REAL, 0) in fake.calls, "进去时没有撤掉"

    assert fake.calls[-1] == ("timer", FakeSignal.ITIMER_REAL, agent.watchdog_seconds), (
        "出来时没有按完整预算重新装上"
    )


def test_看门狗掐掉一轮之后进程继续下一轮(
    agent: Agent, monkeypatch, capture_logs
) -> None:
    """卡住的那一轮被中止 → 记一条 WARNING → **下一轮照常跑**（进程不退）。"""
    records = capture_logs("syncoj.agent")
    rounds = []
    delays: List[float] = []
    armed: List[float] = []
    disarmed: List[int] = []

    def cycle():
        rounds.append(len(rounds) + 1)
        if len(rounds) == 1:
            raise RoundTimeout("卡住了")
        return 7.0

    monkeypatch.setattr(agent, "cycle", cycle)
    monkeypatch.setattr(main_module, "arm_watchdog", lambda s: armed.append(s) or True)
    monkeypatch.setattr(main_module, "disarm_watchdog", lambda: disarmed.append(1))

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 2:
            agent.request_stop()

    agent._sleep = fake_sleep  # type: ignore[assignment]

    assert agent.run_forever() == 0, "进程不该退出"
    assert rounds == [1, 2], "被中止之后必须继续跑下一轮：%r" % rounds
    warnings = [
        r.getMessage() for r in records if r.levelno == logging.WARNING
    ]
    assert any("已中止；下次心跳继续" in text for text in warnings), warnings
    assert delays[0] <= 30, "掐掉之后下一轮要来得快一点：%r" % delays
    assert delays[1] == 7.0, "正常那一轮仍然按服务端给的节奏：%r" % delays
    # **每一轮都要装上、每一轮都要撤掉**：漂到下一轮里的闹钟会在"下一轮明明
    # 很正常"的时候把它掐掉，报出来的还是一条看不懂的超时
    assert armed == [agent.watchdog_seconds, agent.watchdog_seconds], armed
    assert len(disarmed) == 2, disarmed


@pytest.mark.skipif(
    not watchdog_supported(), reason="这个平台没有 SIGALRM/setitimer（Windows）"
)
def test_真的卡住时看门狗当场中止这一轮(agent: Agent, capfd) -> None:
    """真刀真枪：一轮里睡 5 秒，看门狗 0.3 秒 → 必须立刻被掐掉。

    这条在 Linux（目标机与 CI）上跑；Windows 上 skip（没有 setitimer）。
    """
    agent.watchdog_seconds = 0.3
    rounds = []
    delays: List[float] = []

    def cycle():
        rounds.append(len(rounds) + 1)
        if len(rounds) == 1:
            time.sleep(5)  # 被 SIGALRM 打断
            return 999.0
        return 7.0

    agent.cycle = cycle  # type: ignore[assignment]

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 2:
            agent.request_stop()

    agent._sleep = fake_sleep  # type: ignore[assignment]

    started = time.monotonic()
    assert agent.run_forever() == 0
    elapsed = time.monotonic() - started

    assert rounds == [1, 2], "被掐掉之后要继续跑：%r" % rounds
    assert elapsed < 3.0, "看门狗没有及时中止那一轮：%.1f 秒" % elapsed
    assert delays[0] <= 30
