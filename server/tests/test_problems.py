"""场次题目清单测试。

题目在 SyncOJ 里是**显式声明**的，不是从评测结果推导的。它同时充当三样东西：
目录名、代码文件名、成绩矩阵的列名。所以这里的重点是标识校验与列顺序。
"""

from __future__ import annotations

import json
from typing import List

import pytest
from fastapi.testclient import TestClient

from syncoj_server.services.judge import scan_contest_results
from syncoj_server.models import Contest


def add_problems(client: TestClient, contest: dict, headers: dict, items: List[dict]):
    return client.post(
        "/api/v1/admin/contests/%d/problems" % contest["id"], json=items, headers=headers
    )


def list_problems(client: TestClient, contest: dict, headers: dict) -> List[dict]:
    return client.get(
        "/api/v1/admin/contests/%d/problems" % contest["id"], headers=headers
    ).json()


# --------------------------------------------------------------------------- #
# 基本增删改查
# --------------------------------------------------------------------------- #


def test_import_problems(client: TestClient, contest: dict, admin_headers: dict) -> None:
    response = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1", "title": "签到题"}, {"ident": "p2", "title": "图论"}],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert body["errors"] == []

    problems = list_problems(client, contest, admin_headers)
    assert [p["ident"] for p in problems] == ["p1", "p2"]
    assert problems[0]["title"] == "签到题"


def test_import_is_idempotent(client: TestClient, contest: dict, admin_headers: dict) -> None:
    """同一份题目清单反复导入不该产生重复 —— 教师会反复调整。"""
    for _ in range(3):
        add_problems(client, contest, admin_headers, [{"ident": "p1", "title": "签到题"}])

    assert len(list_problems(client, contest, admin_headers)) == 1


def test_import_updates_existing(client: TestClient, contest: dict, admin_headers: dict) -> None:
    add_problems(client, contest, admin_headers, [{"ident": "p1", "title": "旧标题"}])
    body = add_problems(client, contest, admin_headers, [{"ident": "p1", "title": "新标题"}]).json()

    assert body["created"] == 0
    assert body["updated"] == 1
    assert list_problems(client, contest, admin_headers)[0]["title"] == "新标题"


def test_auto_order_follows_submission_order(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """不指定顺序时按提交顺序编号 —— 否则全部堆在 0，排列就不稳定了。"""
    add_problems(
        client, contest, admin_headers,
        [{"ident": "c"}, {"ident": "a"}, {"ident": "b"}],
    )
    idents = [p["ident"] for p in list_problems(client, contest, admin_headers)]
    assert idents == ["c", "a", "b"], "应当按提交顺序，而不是字典序"


def test_explicit_order_is_respected(client: TestClient, contest: dict, admin_headers: dict) -> None:
    add_problems(
        client, contest, admin_headers,
        [
            {"ident": "hard", "order_index": 30},
            {"ident": "easy", "order_index": 10},
            {"ident": "mid", "order_index": 20},
        ],
    )
    idents = [p["ident"] for p in list_problems(client, contest, admin_headers)]
    assert idents == ["easy", "mid", "hard"]


def test_update_problem(client: TestClient, contest: dict, admin_headers: dict) -> None:
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]

    response = client.patch(
        "/api/v1/admin/problems/%d" % problem["id"],
        json={"ident": "p1", "title": "改名后的标题", "order_index": 5},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["title"] == "改名后的标题"


def test_rename_to_existing_ident_is_rejected(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    add_problems(client, contest, admin_headers, [{"ident": "p1"}, {"ident": "p2"}])
    problems = list_problems(client, contest, admin_headers)

    response = client.patch(
        "/api/v1/admin/problems/%d" % problems[0]["id"],
        json={"ident": "p2"},
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_delete_problem(client: TestClient, contest: dict, admin_headers: dict) -> None:
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]
    assert client.delete(
        "/api/v1/admin/problems/%d" % problem["id"], headers=admin_headers
    ).status_code == 200
    assert list_problems(client, contest, admin_headers) == []


def test_problems_require_admin(client: TestClient, contest: dict) -> None:
    assert client.get("/api/v1/admin/contests/%d/problems" % contest["id"]).status_code == 401


# --------------------------------------------------------------------------- #
# 标识校验
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "a/b",         # 带斜杠 —— 它必须是单个目录名
        "a/../b",
        "..",
        "../etc",
        "/etc",
        "a\\b",
        "con",
        "nul.txt",
    ],
)
def test_invalid_ident_is_rejected(
    client: TestClient, contest: dict, admin_headers: dict, bad: str
) -> None:
    """题目标识会变成目录名与文件名，必须能安全用作单个路径段。"""
    body = add_problems(client, contest, admin_headers, [{"ident": bad, "title": "x"}]).json()
    assert body["created"] == 0
    assert body["errors"], "非法标识必须给出说明，而不是静默忽略"
    assert list_problems(client, contest, admin_headers) == []


@pytest.mark.parametrize("bad", ["", "a" * 100])
def test_ident_rejected_at_schema_level(
    client: TestClient, contest: dict, admin_headers: dict, bad: str
) -> None:
    """长度类问题在 pydantic 层就被挡下（422），不该走到业务逻辑。

    这条与上面那条的区别很重要：**校验分两层**，界面对 422 与 200+errors
    的处理方式不同。混在一起断言会掩盖"其实根本没进业务逻辑"。
    """
    response = add_problems(client, contest, admin_headers, [{"ident": bad}])
    assert response.status_code == 422
    assert list_problems(client, contest, admin_headers) == []


def test_chinese_ident_is_accepted(client: TestClient, contest: dict, admin_headers: dict) -> None:
    """中文题目名是常态（"签到题"），不该被拒绝。"""
    body = add_problems(
        client, contest, admin_headers, [{"ident": "签到题", "title": "A+B"}]
    ).json()
    assert body["created"] == 1
    assert list_problems(client, contest, admin_headers)[0]["ident"] == "签到题"


def test_one_bad_item_does_not_fail_the_batch(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """教师一次粘贴十道题，不该因为其中一个名字打错就全部白填。"""
    body = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1"}, {"ident": "a/b"}, {"ident": "p3"}],
    ).json()

    assert body["created"] == 2
    assert len(body["errors"]) == 1
    assert [p["ident"] for p in list_problems(client, contest, admin_headers)] == ["p1", "p3"]


def test_import_into_missing_contest(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/api/v1/admin/contests/9999/problems", json=[{"ident": "p1"}], headers=admin_headers
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 与成绩矩阵的关系
# --------------------------------------------------------------------------- #


def write_result(app, contest_slug: str, player_no: str, problem: str, payload: dict) -> None:
    target = app.state.ctx.settings.judge_result_root / contest_slug / player_no / problem
    target.mkdir(parents=True, exist_ok=True)
    (target / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def test_matrix_columns_follow_declared_order(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """列的先后由教师决定，不该随"哪个选手先被评测"而变。"""
    add_problems(
        client, contest, admin_headers,
        [
            {"ident": "hard", "order_index": 30, "title": "压轴"},
            {"ident": "easy", "order_index": 10, "title": "签到"},
        ],
    )
    # 先给 hard 写结果，故意让"字典序/出现顺序"与声明顺序不一致
    write_result(app, contest["slug"], "S001", "hard", {"score": 10})
    with app.state.ctx.db.session() as session:
        scan_contest_results(session, app.state.ctx.settings, session.get(Contest, contest["id"]), force=True)

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    assert [c["ident"] for c in matrix["columns"]] == ["easy", "hard"]
    assert matrix["columns"][0]["title"] == "签到"
    assert all(c["declared"] for c in matrix["columns"])


def test_undeclared_problem_appears_and_is_flagged(
    client: TestClient, contest: dict, admin_headers: dict, app, player: dict
) -> None:
    """结果里有、清单里没有的题目仍然显示，但标注"未登记"。

    静默隐藏它会丢掉真实成绩；静默当成正式题目又会掩盖漏登记。
    """
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    write_result(app, contest["slug"], "S001", "legacy", {"score": 88})
    with app.state.ctx.db.session() as session:
        scan_contest_results(session, app.state.ctx.settings, session.get(Contest, contest["id"]), force=True)

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()

    columns = {c["ident"]: c for c in matrix["columns"]}
    assert set(columns) == {"p1", "legacy"}
    assert columns["p1"]["declared"] is True
    assert columns["legacy"]["declared"] is False
    assert matrix["rows"][0]["cells"][1]["score"] == 88


def test_declared_problems_show_even_without_results(
    client: TestClient, contest: dict, admin_headers: dict, player: dict
) -> None:
    """刚登记完还没评测时，矩阵也要有列 —— 否则教师会以为登记没生效。"""
    add_problems(client, contest, admin_headers, [{"ident": "p1"}, {"ident": "p2"}])
    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    assert [c["ident"] for c in matrix["columns"]] == ["p1", "p2"]
    assert matrix["rows"], "有选手就该有行"
    assert all(cell["parse_status"] == "missing" for cell in matrix["rows"][0]["cells"])


def test_deleting_problem_keeps_scores(
    client: TestClient, contest: dict, admin_headers: dict, app, player: dict
) -> None:
    """删题目只是从清单里去掉，**不能连带删掉已收到的成绩**。"""
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]
    write_result(app, contest["slug"], "S001", "p1", {"score": 100})
    with app.state.ctx.db.session() as session:
        scan_contest_results(session, app.state.ctx.settings, session.get(Contest, contest["id"]), force=True)

    client.delete("/api/v1/admin/problems/%d" % problem["id"], headers=admin_headers)

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    assert [c["ident"] for c in matrix["columns"]] == ["p1"]
    assert matrix["columns"][0]["declared"] is False, "题目没了但成绩还在，应标为未登记"
    assert matrix["rows"][0]["cells"][0]["score"] == 100
