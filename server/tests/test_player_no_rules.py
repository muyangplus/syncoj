"""考号会被拼进落盘路径：四个写入口用同一套形状规则。

审计 M1：导入选手、改选手、导入名单、改名单条目只查了"非空、≤64"，于是
``../evil``、``a/b``、`` S009 `` 都能入库 —— 代码落进别的场次或别人的目录，
成绩扫描也对不上号。
"""

from __future__ import annotations

import pytest

from conftest import make_roster
from syncoj_server.services.player_no import player_no_problem

BAD = ["../evil", "a/b", "a\\b", "..", ".hidden", "x\x00y", "x" * 65]
GOOD = ["S001", "2026-001", "高一1班-07", "A 12"]


@pytest.mark.parametrize("value", BAD)
def test_不能当目录名的考号一律拒绝(value: str) -> None:
    assert player_no_problem(value)


@pytest.mark.parametrize("value", GOOD)
def test_正常考号照常接受(value: str) -> None:
    assert player_no_problem(value) is None


def test_导入选手时整批拒绝_一条都不写(client, admin_headers, contest) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S100"}, {"player_no": "../evil"}],
        headers=admin_headers,
    )
    assert response.status_code == 400, response.text
    listed = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()["items"]
    assert "S100" not in [p["player_no"] for p in listed]


def test_导入选手时去掉首尾空白(client, admin_headers, contest) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "  S200  "}],
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["players"][0]["player_no"] == "S200"


def test_导入名单时逐条跳过坏考号_其余照收(client, admin_headers) -> None:
    roster = make_roster(client, admin_headers, name="名单A")
    response = client.post(
        "/api/v1/admin/rosters/%d/entries" % roster["id"],
        json=[{"player_no": "S1"}, {"player_no": "a/b"}, {"player_no": "S2"}],
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped"] == 1
    assert any("斜杠" in error for error in body["errors"])
