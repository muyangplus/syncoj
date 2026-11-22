"""失联时的行为：按默认间隔一直试、每 N 轮问一次局域网、日志不刷屏。

曾经是指数退避到 300 秒 —— 服务端恢复后机器最多还要再睡 5 分钟才回来，考场上
这 5 分钟就是"代码交了没收上来"。现在连不上就按默认间隔（``scan.interval``）接着
试，局域网发现也不封顶（只认原来那台服务端，见 ``test_server_uuid.py``）。
"""

from __future__ import annotations

import logging
from typing import List

from test_pairing import FakeServer, agent_config, provision, run_rounds, service  # noqa: F401

from syncoj_agent import discovery
from syncoj_agent.client import NetworkError, RateLimited
from syncoj_agent.config import AgentConfig
from syncoj_agent.main import (
    MAX_BACKOFF,
    NETWORK_RETRY_JITTER,
    REDISCOVER_AFTER_FAILURES,
)


def offline_agent(config: AgentConfig) -> tuple:
    server = FakeServer()
    provision(config, server)
    agent = service(config, server)
    server.tick_error = NetworkError("connection refused")
    calls: List[bool] = []
    agent._try_rediscover = lambda: calls.append(True)  # type: ignore[assignment]
    return agent, server, calls


# --------------------------------------------------------------------------- #
# 节奏：默认间隔 + 小抖动，不翻倍
# --------------------------------------------------------------------------- #


def test_连不上时按默认间隔重试_不翻倍(agent_config: AgentConfig) -> None:
    agent_config.scan_interval = 30
    agent, _server, _calls = offline_agent(agent_config)

    delays = run_rounds(agent, 12)

    jitter = min(NETWORK_RETRY_JITTER, 30 * 0.1)
    assert len(delays) == 12
    for delay in delays:
        assert 30.0 <= delay <= 30.0 + jitter, delays
    assert max(delays) < 60, "不能再指数退避"


def test_间隔跟着配置走(agent_config: AgentConfig) -> None:
    agent_config.scan_interval = 10
    agent, _server, _calls = offline_agent(agent_config)

    delays = run_rounds(agent, 5)

    assert all(10.0 <= d <= 11.0 for d in delays), delays


def test_有抖动_整间机房不在同一秒撞上来(agent_config: AgentConfig) -> None:
    agent_config.scan_interval = 30
    agent, _server, _calls = offline_agent(agent_config)

    delays = run_rounds(agent, 20)

    assert len(set(delays)) > 1


def test_限速仍按服务端给的时间等(agent_config: AgentConfig) -> None:
    """限速不是网络错误：节奏仍由服务端的 Retry-After 决定，本地 MAX_BACKOFF 封顶。"""
    agent, server, _calls = offline_agent(agent_config)
    server.tick_error = RateLimited("429", status=429, code="rate_limited", retry_after=120)

    assert run_rounds(agent, 2) == [120.0, 120.0]

    server.tick_error = RateLimited("429", status=429, code="rate_limited", retry_after=0)
    agent._stop = False
    assert run_rounds(agent, 1) == [MAX_BACKOFF]


def test_长时间失联_日志不每轮都刷_WARNING(agent_config: AgentConfig, caplog) -> None:
    agent, _server, _calls = offline_agent(agent_config)

    with caplog.at_level(logging.DEBUG, logger="syncoj.agent"):
        run_rounds(agent, 25)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "网络不可用" in r.getMessage()
    ]
    assert 1 <= len(warnings) <= 3, [r.getMessage() for r in warnings]


# --------------------------------------------------------------------------- #
# 局域网发现：每 N 轮一次，一直问下去
# --------------------------------------------------------------------------- #


def test_每N轮问一次局域网_不封顶不停(agent_config: AgentConfig) -> None:
    agent, _server, calls = offline_agent(agent_config)

    run_rounds(agent, REDISCOVER_AFTER_FAILURES * 10)

    assert len(calls) == 10


def test_成功一轮后重新数(agent_config: AgentConfig) -> None:
    agent, server, calls = offline_agent(agent_config)
    server.tick_error_times = REDISCOVER_AFTER_FAILURES - 1

    run_rounds(agent, REDISCOVER_AFTER_FAILURES)  # 两轮失败 + 一轮成功
    server.tick_error_times = -1
    server.tick_errors_raised = 0
    agent._stop = False
    delays = run_rounds(agent, REDISCOVER_AFTER_FAILURES - 1)

    assert len(delays) == REDISCOVER_AFTER_FAILURES - 1
    assert calls == [], "中间成功过一轮，之前的失败不该累计"
    agent._stop = False
    run_rounds(agent, 1)
    assert calls == [True], "再失败一轮才凑够 N 轮"


# --------------------------------------------------------------------------- #
# 日志：同样的结论不刷屏
# --------------------------------------------------------------------------- #


class FakeKey:
    key_id = "kid"


def test_同样的没结果只在第一次记_WARNING(agent_config: AgentConfig, monkeypatch, caplog) -> None:
    agent = service(agent_config, FakeServer())
    monkeypatch.setattr(discovery, "discover", lambda public_key, **kwargs: discovery.DiscoveryOutcome(None, []))
    agent._public_key = FakeKey()  # type: ignore[assignment]

    with caplog.at_level(logging.DEBUG, logger="syncoj.agent"):
        for _ in range(5):
            agent._try_rediscover()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "局域网发现" in r.getMessage()]
    assert len(warnings) == 1
