"""协议版本：太旧的 Agent 回 426 agent_outdated，而不是一个含糊的 422。"""

from __future__ import annotations

from conftest import agent_headers


def test_旧协议的_tick_回_426(client, enrolled) -> None:
    """旧 Agent 带着新服务端不认的字段（partials）——校验失败也要先认出"太旧"。"""
    response = client.post(
        "/api/v1/agent/tick",
        json={
            "machine_id": enrolled["machine_id"], "ts": 1, "scan": [],
            "partials": [], "completed_assets": [],
        },
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 426
    body = response.json()
    assert body["code"] == "agent_outdated"
    assert "重装" in body["detail"]


def test_字段都对但版本太低也回_426(client, enrolled) -> None:
    response = client.post(
        "/api/v1/agent/tick",
        json={"protocol_version": 1, "machine_id": enrolled["machine_id"], "ts": 1,
              "scan": [], "scan_complete": True},
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 426


def test_旧协议的注册也回_426(client) -> None:
    response = client.post(
        "/api/v1/agent/enroll",
        json={"machine_id": "m-old", "bootstrap_key": "k" * 43},
    )
    assert response.status_code == 426
    assert response.json()["code"] == "agent_outdated"


def test_新协议的坏请求仍是_422(client, enrolled) -> None:
    response = client.post(
        "/api/v1/agent/tick",
        json={"protocol_version": 2, "machine_id": enrolled["machine_id"], "ts": -1,
              "scan": [], "scan_complete": True},
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 422
