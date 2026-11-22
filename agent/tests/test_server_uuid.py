"""记住服务端实例 ID（双向 UUID 配对的 Agent 一半）。

tick 成功时记下 ``server_uuid``；失联后的局域网发现带上本机 ``machine_uuid`` 与它，
只有"恰好一个、ID 相同、认得本机"的应答才切换（验签与 ID 比对在 ``discovery.py``，
见 ``test_discovery.py``；这里测主循环怎么用它）。
"""

from __future__ import annotations

import logging

import pytest

from test_pairing import FakeServer, agent_config, make_ready_server, provision, run_rounds, service  # noqa: F401

from syncoj_agent import discovery
from syncoj_agent.config import AgentConfig
from syncoj_agent.main import Agent
from syncoj_agent.state import load_server_uuid, save_server_uuid

SERVER_A = "a" * 32
SERVER_B = "b" * 32


# --------------------------------------------------------------------------- #
# 记住服务端实例 ID
# --------------------------------------------------------------------------- #


def test_第一次_tick_成功就记住实例ID(agent_config: AgentConfig) -> None:
    server = FakeServer()
    server.tick_response["server_uuid"] = SERVER_A
    provision(agent_config, server)
    agent = service(agent_config, server)
    assert agent.server_uuid == ""

    run_rounds(agent, 1)

    assert agent.server_uuid == SERVER_A
    assert load_server_uuid(agent_config.state_dir) == SERVER_A
    assert Agent(agent_config).server_uuid == SERVER_A, "重启之后还记得"


def test_能干活的_tick_也记(agent_config: AgentConfig) -> None:
    server = make_ready_server()
    server.tick_response["server_uuid"] = SERVER_A
    provision(agent_config, server)
    agent = service(agent_config, server)

    run_rounds(agent, 1)

    assert load_server_uuid(agent_config.state_dir) == SERVER_A


def test_实例ID变了就跟着换(agent_config: AgentConfig) -> None:
    """agent.ini 里的地址换成了新服务端：tick 成功说明教师就是要连它。"""
    save_server_uuid(agent_config.state_dir, SERVER_A)
    server = FakeServer()
    server.tick_response["server_uuid"] = SERVER_B
    provision(agent_config, server)
    agent = service(agent_config, server)
    assert agent.server_uuid == SERVER_A

    run_rounds(agent, 1)

    assert load_server_uuid(agent_config.state_dir) == SERVER_B


@pytest.mark.parametrize("value", [None, "", "short", "../../etc/passwd", 42])
def test_老服务端或乱填的实例ID不记(agent_config: AgentConfig, value) -> None:
    server = FakeServer()
    server.tick_response["server_uuid"] = value
    provision(agent_config, server)
    agent = service(agent_config, server)

    run_rounds(agent, 1)

    assert agent.server_uuid == ""
    assert not (agent_config.state_dir / "server_uuid").exists()


# --------------------------------------------------------------------------- #
# 重新发现：带上双方的 UUID，结论只在"恰好一个、认得本机的原来那台"时切换
# --------------------------------------------------------------------------- #


class FakeKey:
    key_id = "kid"


def rediscover_with(agent: Agent, monkeypatch, outcome) -> dict:
    seen: dict = {}

    def fake_discover(public_key, **kwargs):
        seen.update(kwargs)
        return outcome

    monkeypatch.setattr(discovery, "discover", fake_discover)
    agent._public_key = FakeKey()  # type: ignore[assignment]
    agent._try_rediscover()
    return seen


def test_发现时带上本机UUID与记住的实例ID(agent_config: AgentConfig, monkeypatch) -> None:
    save_server_uuid(agent_config.state_dir, SERVER_A)
    agent = service(agent_config, FakeServer())

    seen = rediscover_with(agent, monkeypatch, discovery.DiscoveryOutcome(None, []))

    assert seen["machine_uuid"] == agent.machine_uuid
    assert seen["server_uuid"] == SERVER_A


def test_服务端说不认得本机就不切(agent_config: AgentConfig, monkeypatch, caplog) -> None:
    agent = service(agent_config, FakeServer())
    before = agent.config.server_url
    outcome = discovery.DiscoveryOutcome(None, [], (), ("http://10.0.0.7:8000",))

    with caplog.at_level(logging.WARNING, logger="syncoj.agent"):
        rediscover_with(agent, monkeypatch, outcome)

    assert agent.config.server_url == before
    assert any("不认得" in r.getMessage() for r in caplog.records)


def test_只有别的服务端应答就不切(agent_config: AgentConfig, monkeypatch, caplog) -> None:
    agent = service(agent_config, FakeServer())
    before = agent.config.server_url
    outcome = discovery.DiscoveryOutcome(None, [], ("http://10.0.0.8:8000",))

    with caplog.at_level(logging.WARNING, logger="syncoj.agent"):
        rediscover_with(agent, monkeypatch, outcome)

    assert agent.config.server_url == before
    assert any("实例 ID" in r.getMessage() for r in caplog.records)


def test_找到原来那台就切过去(agent_config: AgentConfig, monkeypatch) -> None:
    agent = service(agent_config, FakeServer())
    switched = []
    agent.client.repoint = lambda url: switched.append(url)  # type: ignore[attr-defined]
    outcome = discovery.DiscoveryOutcome("http://10.0.0.7:8000", ["http://10.0.0.7:8000"])

    rediscover_with(agent, monkeypatch, outcome)

    assert agent.config.server_url == "http://10.0.0.7:8000"
    assert switched == ["http://10.0.0.7:8000"]
