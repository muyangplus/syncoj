"""概览页的交题进度接口（``/contests/{id}/scores/progress``）。

它是成绩矩阵的"只要几个数"版本，口径必须与矩阵一致 —— 否则概览页和成绩页
对同一个人说出两个不同的"没交几道"。这里直接拿矩阵算一遍期望值来比。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from syncoj_server.models import JudgeRun, Problem, SourceFile, utcnow


def _expected_from_matrix(matrix: dict) -> dict:
    declared = {c["ident"] for c in matrix["columns"] if c["declared"]}
    out = {}
    for row in matrix["rows"]:
        done = pending = missing = 0
        for cell in row["cells"]:
            if cell["problem"] not in declared:
                continue
            if cell["parse_status"] != "missing":
                done += 1
            elif cell["submitted"]:
                pending += 1
            else:
                missing += 1
        out[row["player_id"]] = (done, pending, missing, len(declared))
    return out


def test_progress_matches_the_score_matrix(
    app, client: TestClient, admin_headers: dict, contest: dict
) -> None:
    cid = contest["id"]
    imported = client.post(
        "/api/v1/admin/contests/%d/players" % cid,
        json=[{"player_no": "S%03d" % i} for i in range(1, 5)],
        headers=admin_headers,
    )
    assert imported.status_code == 200, imported.text
    ids = {row["player_no"]: row["id"] for row in imported.json()["players"]}

    with app.state.ctx.db.session() as session:
        for index, ident in enumerate(("apple", "banana", "cherry")):
            session.add(Problem(contest_id=cid, ident=ident, order_index=index))
        now = utcnow()
        for no, path, deleted in (
            ("S001", "apple/apple.cpp", False),
            ("S001", "banana/banana.cpp", False),
            ("S002", "cherry/cherry.cpp", False),
            ("S003", "apple/apple.cpp", True),  # 删掉的不算交
        ):
            session.add(
                SourceFile(player_id=ids[no], rel_path=path, sha256="0" * 64,
                           deleted_at=now if deleted else None)
            )
        for no, ident, status in (
            ("S001", "apple", "ok"),
            ("S002", "banana", "unparsed"),
            ("S004", "zzz", "ok"),  # 未登记的题不计
        ):
            session.add(JudgeRun(contest_id=cid, player_id=ids[no], problem=ident,
                                 score=10, parse_status=status))

    matrix = client.get("/api/v1/admin/contests/%d/scores" % cid, headers=admin_headers)
    assert matrix.status_code == 200, matrix.text
    expected = _expected_from_matrix(matrix.json())
    assert expected[ids["S001"]] == (1, 1, 1, 3), "前提：样例要覆盖三种状态"

    url = "/api/v1/admin/contests/%d/scores/progress" % cid
    full = client.get(url, params={"limit": 500}, headers=admin_headers).json()
    assert full["total"] == 4
    got = {
        row["player_id"]: (row["submitted"], row["pending"], row["missing"], row["total"])
        for row in full["items"]
    }
    assert got == expected

    # 与选手列表同序同页
    page = client.get(url, params={"limit": 2, "offset": 1}, headers=admin_headers).json()
    players = client.get(
        "/api/v1/admin/contests/%d/players" % cid,
        params={"limit": 2, "offset": 1},
        headers=admin_headers,
    ).json()
    assert [r["player_id"] for r in page["items"]] == [r["id"] for r in players["items"]]
    assert page["total"] == 4
