"""列表接口的分页与统计列在 SQL 里做（``docs/reference/api-conventions.md`` §2）。

守两件事：

* 翻页的结果与"全量取回再切"完全一致 —— 顺序、``total``、每行的统计列都不变；
* 下发列表的查询条数与任务数无关（曾经逐任务各查一次资产与全部目标，N+1）。
"""

from __future__ import annotations

from typing import List

from fastapi.testclient import TestClient
from sqlalchemy import event

from conftest import issue_bootstrap_key, make_roster


def _get(client: TestClient, headers: dict, url: str, **params) -> dict:
    response = client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _assert_pages_match_full(client: TestClient, headers: dict, url: str, n: int) -> None:
    full = _get(client, headers, url, limit=500)
    assert full["total"] == n
    assert len(full["items"]) == n
    for offset in range(0, n + 1):
        page = _get(client, headers, url, limit=2, offset=offset)
        assert page["total"] == n
        assert page["limit"] == 2 and page["offset"] == offset
        assert page["items"] == full["items"][offset : offset + 2]


def _upload(client: TestClient, headers: dict, contest_id: int, name: str, data: bytes) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/assets" % contest_id,
        files={"file": (name, data, "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _players(client: TestClient, headers: dict, contest_id: int, nos: List[str]) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest_id,
        json=[{"player_no": no, "name": no} for no in nos],
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_roster_pages_match_full_list(client: TestClient, admin_headers: dict) -> None:
    for name, size in (("丙班", 1), ("甲班", 3), ("乙班", 0), ("丁班", 2)):
        make_roster(
            client,
            admin_headers,
            name=name,
            entries=[{"player_no": "%s%02d" % (name, i)} for i in range(size)],
        )
    _assert_pages_match_full(client, admin_headers, "/api/v1/admin/rosters", 4)
    counts = {
        row["name"]: row["entry_count"]
        for row in _get(client, admin_headers, "/api/v1/admin/rosters", limit=500)["items"]
    }
    assert counts == {"丙班": 1, "甲班": 3, "乙班": 0, "丁班": 2}


def test_bootstrap_key_pages_match_full_list(client: TestClient, admin_headers: dict) -> None:
    for i in range(4):
        issue_bootstrap_key(client, admin_headers, label="密钥%d" % i)
    _assert_pages_match_full(client, admin_headers, "/api/v1/admin/bootstrap-keys", 4)


def test_player_pages_match_full_list(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    _players(client, admin_headers, contest["id"], ["S004", "S001", "S003", "S002", "S005"])
    url = "/api/v1/admin/contests/%d/players" % contest["id"]
    _assert_pages_match_full(client, admin_headers, url, 5)


def test_asset_and_deploy_pages_match_full_list(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    cid = contest["id"]
    _players(client, admin_headers, cid, ["S001", "S002", "S003"])
    assets = [_upload(client, admin_headers, cid, "f%d.txt" % i, b"x" * (i + 1)) for i in range(4)]
    for asset in assets:
        response = client.post(
            "/api/v1/admin/contests/%d/deploys" % cid,
            json={"asset_id": asset["id"], "target_kind": "all", "dest_dir": "exam"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

    _assert_pages_match_full(client, admin_headers, "/api/v1/admin/contests/%d/assets" % cid, 4)
    deploys = "/api/v1/admin/contests/%d/deploys" % cid
    _assert_pages_match_full(client, admin_headers, deploys, 4)

    full = _get(client, admin_headers, deploys, limit=500, include_targets="true")
    for row in full["items"]:
        assert row["total"] == 3
        assert [t["player_no"] for t in row["targets"]] == ["S001", "S002", "S003"]
    assets_page = _get(client, admin_headers, "/api/v1/admin/contests/%d/assets" % cid)
    assert {row["pending_targets"] for row in assets_page["items"]} == {3}


def test_deploy_list_query_count_does_not_grow_with_tasks(
    app, client: TestClient, admin_headers: dict, contest: dict
) -> None:
    cid = contest["id"]
    _players(client, admin_headers, cid, ["S001", "S002"])
    url = "/api/v1/admin/contests/%d/deploys" % cid

    statements: List[str] = []

    def _record(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    engine = app.state.ctx.db.engine

    def _count_queries() -> int:
        statements.clear()
        event.listen(engine, "before_cursor_execute", _record)
        try:
            _get(client, admin_headers, url, limit=500, include_targets="true")
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        return len(statements)

    def _add_task(i: int) -> None:
        asset = _upload(client, admin_headers, cid, "q%d.txt" % i, b"q" * (i + 1))
        response = client.post(
            url,
            json={"asset_id": asset["id"], "target_kind": "all", "dest_dir": "exam"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

    _add_task(0)
    one = _count_queries()
    for i in range(1, 6):
        _add_task(i)
    six = _count_queries()
    assert six == one, "任务从 1 条变成 6 条，查询从 %d 条变成了 %d 条" % (one, six)
