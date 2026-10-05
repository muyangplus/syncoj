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

from conftest import do_tick as tick, scan_entry as entry, sha256_of


def add_problems(client: TestClient, contest: dict, headers: dict, items: List[dict]):
    return client.post(
        "/api/v1/admin/contests/%d/problems" % contest["id"], json=items, headers=headers
    )


def list_problems(client: TestClient, contest: dict, headers: dict) -> List[dict]:
    body = client.get(
        "/api/v1/admin/contests/%d/problems" % contest["id"], headers=headers
    ).json()
    return body["items"]


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

    missing = client.delete(
        "/api/v1/admin/problems/%d" % problem["id"], headers=admin_headers
    )
    assert missing.status_code == 422, "删题目要打题目标识"

    response = client.delete(
        "/api/v1/admin/problems/%d?confirm=p1" % problem["id"], headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert list_problems(client, contest, admin_headers) == []


def test_delete_problem_rejects_a_wrong_confirm(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]

    response = client.delete(
        "/api/v1/admin/problems/%d?confirm=p2" % problem["id"], headers=admin_headers
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "name_mismatch"
    assert [p["ident"] for p in list_problems(client, contest, admin_headers)] == ["p1"]


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

    client.delete(
        "/api/v1/admin/problems/%d?confirm=p1" % problem["id"], headers=admin_headers
    )

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    assert [c["ident"] for c in matrix["columns"]] == ["p1"]
    assert matrix["columns"][0]["declared"] is False, "题目没了但成绩还在，应标为未登记"
    assert matrix["rows"][0]["cells"][0]["score"] == 100


# --------------------------------------------------------------------------- #
# 代码路径模式
# --------------------------------------------------------------------------- #


def test_default_pattern_is_reported_back(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """没配模式时，接口要把**实际生效的模式模板**回给前端。

    前端推不出这个值 —— 它是可配置的（``SYNCOJ_DEFAULT_FILE_PATTERN``）。
    让前端自己猜默认值，改配置那天界面就会显示错。

    返回的是**模板**（占位符原样保留）而不是展开后的 ``p1/**``：
    前端「打开编辑 → 保存」会把看到的东西原样传回来，展开过的值一存
    就把 ``{ident}`` 冻死了，之后改标识再也认不出文件，而界面上毫无异常。
    """
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]
    assert problem["file_patterns"] == ["{ident}/**"]


def test_default_pattern_template_survives_edit_round_trip(
    client: TestClient, contest: dict, admin_headers: dict, enrolled: dict
) -> None:
    """界面"打开编辑 → 原样保存"之后，默认模式必须还能跟着改名走。

    只断言接口返回 ``{ident}/**`` 不够 —— 得证明原样存回去之后，
    改名后仍然认得新目录。
    """
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]
    as_shown = problem["file_patterns"]

    for ident in ("p1", "签到题"):
        response = client.patch(
            "/api/v1/admin/problems/%d" % problem["id"],
            json={"ident": ident, "file_patterns": as_shown},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

    report_files(client, enrolled, {"签到题/签到题.cpp": b"// one\n"})
    assert match_path(client, contest, admin_headers, "签到题/签到题.cpp")["problem"] == "签到题"
    assert list_files(client, contest, admin_headers)[0]["problem"] == "签到题"


def test_explicit_patterns_round_trip(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    body = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1", "file_patterns": ["{ident}/**", "**/p1.cpp"]}],
    ).json()
    assert body["problems"][0]["file_patterns"] == ["{ident}/**", "**/p1.cpp"]
    assert list_problems(client, contest, admin_headers)[0]["file_patterns"] == [
        "{ident}/**",
        "**/p1.cpp",
    ]


def test_invalid_pattern_is_reported_not_silently_ignored(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    body = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1", "file_patterns": ["/etc/**"]}],
    ).json()
    assert body["created"] == 0
    assert body["errors"], "模式不合法必须说出来"
    assert list_problems(client, contest, admin_headers) == []


def test_invalid_pattern_leaves_no_half_written_problem(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """坏模式不能留下半条记录。

    "先建行、再校验模式、失败就 continue" 是很容易写出来的顺序，
    但 session 退出时照样提交 —— 于是库里多出一道连模式都没有的题。
    单条看起来无害，教师的清单从此就多了一道自己没登记的题。
    """
    add_problems(client, contest, admin_headers, [{"ident": "p1", "title": "旧标题"}])
    body = add_problems(
        client, contest, admin_headers,
        [{"ident": "p2", "title": "新题", "file_patterns": ["a\\b"]}],
    ).json()

    assert body["errors"]
    assert [p["ident"] for p in list_problems(client, contest, admin_headers)] == ["p1"]


def test_update_replaces_patterns_rather_than_appending(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """编辑时"删掉一个模式"必须能生效 —— 追加语义做不到这一点。"""
    problem = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1", "file_patterns": ["a/**", "b/**"]}],
    ).json()["problems"][0]

    response = client.patch(
        "/api/v1/admin/problems/%d" % problem["id"],
        json={"ident": "p1", "file_patterns": ["c/**"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["file_patterns"] == ["c/**"]


def test_update_with_empty_patterns_falls_back_to_default(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    problem = add_problems(
        client, contest, admin_headers, [{"ident": "p1", "file_patterns": ["a/**"]}]
    ).json()["problems"][0]

    response = client.patch(
        "/api/v1/admin/problems/%d" % problem["id"],
        json={"ident": "p1", "file_patterns": []},
        headers=admin_headers,
    )
    assert response.json()["file_patterns"] == ["{ident}/**"], "清空应当回到默认模板"


def test_update_rejects_invalid_pattern(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]
    response = client.patch(
        "/api/v1/admin/problems/%d" % problem["id"],
        json={"ident": "p1", "file_patterns": ["../x/**"]},
        headers=admin_headers,
    )
    assert response.status_code == 400


def test_too_many_patterns_is_rejected(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """逐条校验挡不住"一次粘一百条进来"。

    数量上限要单独卡：模式越多每次归题越慢，而且"到底哪条命中"会变得没法
    跟教师解释清楚。上限值本身在这里硬编码成 17，是有意的 —— 常量改了
    就该有人来看一眼这条断言。
    """
    from syncoj_server.services.matching import MAX_PATTERNS

    assert MAX_PATTERNS == 16, "上限变了，请同步检查界面上的提示文案"

    too_many = ["p%d/**" % i for i in range(MAX_PATTERNS + 1)]
    body = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1", "file_patterns": too_many}],
    ).json()
    assert body["created"] == 0
    assert body["errors"]
    assert list_problems(client, contest, admin_headers) == []


def test_patterns_at_the_limit_are_accepted(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """刚好到上限要能过 —— 边界写错一位就会变成"少配一个也不行"。"""
    from syncoj_server.services.matching import MAX_PATTERNS

    at_limit = ["p%d/**" % i for i in range(MAX_PATTERNS)]
    body = add_problems(
        client, contest, admin_headers,
        [{"ident": "p1", "file_patterns": at_limit}],
    ).json()
    assert body["created"] == 1
    assert len(list_problems(client, contest, admin_headers)[0]["file_patterns"]) == MAX_PATTERNS


def test_rename_keeps_default_pattern_working(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """题目改名后，默认模式跟着走 —— 教师不该因为改个名字就得重配模式。"""
    problem = add_problems(client, contest, admin_headers, [{"ident": "p1"}]).json()["problems"][0]
    client.patch(
        "/api/v1/admin/problems/%d" % problem["id"],
        json={"ident": "签到题"},
        headers=admin_headers,
    )
    assert list_problems(client, contest, admin_headers)[0]["file_patterns"] == ["{ident}/**"]


# --------------------------------------------------------------------------- #
# 文件归属
# --------------------------------------------------------------------------- #


def upload(client: TestClient, token: str, path: str, data: bytes):
    return client.post(
        "/api/v1/agent/files",
        data={"path": path, "sha256": sha256_of(data)},
        files={"file": ("content.bin", data, "application/octet-stream")},
        headers={"Authorization": "Bearer " + token},
    )


def report_files(client: TestClient, enrolled: dict, paths: dict) -> None:
    """让 Agent 报告一批文件（内容已上传）。"""
    machine = enrolled["machine_id"]
    scan = [entry(path, data) for path, data in paths.items()]
    tick(client, enrolled["token"], scan, machine_id=machine)
    for path, data in paths.items():
        assert upload(client, enrolled["token"], path, data).status_code == 200


def list_files(client: TestClient, contest: dict, headers: dict) -> List[dict]:
    body = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=headers
    ).json()
    return body["items"]


def test_files_are_annotated_with_their_problem(
    client: TestClient, contest: dict, admin_headers: dict, enrolled: dict
) -> None:
    add_problems(client, contest, admin_headers, [{"ident": "p1"}, {"ident": "p2"}])
    report_files(
        client, enrolled,
        {
            "p1/p1.cpp": b"// one\n",
            "p2/p2.cpp": b"// two\n",
            "misc/notes.txt": b"junk\n",
        },
    )

    by_path = {row["rel_path"]: row["problem"] for row in list_files(client, contest, admin_headers)}
    assert by_path["p1/p1.cpp"] == "p1"
    assert by_path["p2/p2.cpp"] == "p2"
    assert by_path["misc/notes.txt"] is None, "认不出来就说认不出来，别硬塞给某道题"


def test_file_problem_follows_pattern_changes_without_reupload(
    client: TestClient, contest: dict, admin_headers: dict, enrolled: dict
) -> None:
    """归属是**算出来的**，所以改模式立刻生效，不需要选手重交。"""
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    report_files(client, enrolled, {"p1/p1.cpp": b"// one\n"})
    assert list_files(client, contest, admin_headers)[0]["problem"] == "p1"

    problems = list_problems(client, contest, admin_headers)
    client.patch(
        "/api/v1/admin/problems/%d" % problems[0]["id"],
        json={"ident": "p1", "file_patterns": ["other/**"]},
        headers=admin_headers,
    )
    assert list_files(client, contest, admin_headers)[0]["problem"] is None


def test_matrix_marks_submitted_but_unjudged(
    client: TestClient, contest: dict, admin_headers: dict, enrolled: dict
) -> None:
    """「交了但还没评测」与「压根没交」必须看得出区别。

    两者 ``parse_status`` 都是 ``missing``，但教师要做的事完全相反：
    前者等一会儿就好，后者得去座位上看一眼。
    """
    add_problems(client, contest, admin_headers, [{"ident": "p1"}, {"ident": "p2"}])
    report_files(client, enrolled, {"p1/p1.cpp": b"// one\n"})

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()

    cells = {c["problem"]: c for c in matrix["rows"][0]["cells"]}
    assert cells["p1"]["submitted"] is True
    assert cells["p1"]["parse_status"] == "missing", "交了但没成绩，仍然是 missing"
    assert cells["p2"]["submitted"] is False


def test_matrix_marks_submitted_when_result_arrives(
    client: TestClient, contest: dict, admin_headers: dict, app, enrolled: dict
) -> None:
    """出了成绩也照样标着"交过" —— 界面靠它显示复核入口。"""
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    report_files(client, enrolled, {"p1/p1.cpp": b"// one\n"})
    write_result(app, contest["slug"], "S001", "p1", {"score": 100})
    with app.state.ctx.db.session() as session:
        scan_contest_results(
            session, app.state.ctx.settings, session.get(Contest, contest["id"]), force=True
        )

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    cell = matrix["rows"][0]["cells"][0]
    assert cell["score"] == 100
    assert cell["submitted"] is True


def test_deleted_file_does_not_count_as_submitted(
    client: TestClient, contest: dict, admin_headers: dict, enrolled: dict
) -> None:
    """选手把文件删了就是没交。

    如果按"曾经交过"来算，矩阵会把空座位显示成"已交未评测"，
    教师就永远等不到那个其实没人写的程序。
    """
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    report_files(client, enrolled, {"p1/p1.cpp": b"// one\n"})

    # 第二次 tick 不带这个文件 = Agent 明确上报"它没了"
    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    assert matrix["rows"][0]["cells"][0]["submitted"] is False


def test_score_matrix_works_with_no_problems_declared(
    client: TestClient, contest: dict, admin_headers: dict, player: dict
) -> None:
    """一道题都没登记时不该崩 —— 教师往往先建场次、再慢慢填题。"""
    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers
    ).json()
    assert matrix["columns"] == []
    assert matrix["rows"] and matrix["rows"][0]["cells"] == []


# --------------------------------------------------------------------------- #
# 路径试算（排错用）
# --------------------------------------------------------------------------- #


def match_path(client: TestClient, contest: dict, headers: dict, path: str) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/problems/match" % contest["id"],
        json={"path": path},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_match_reports_the_winning_problem(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """要同时回「哪条模式赢了」和「它展开成了什么」。

    教师写的是模板（``{ident}/**``），真正拿去匹配的是展开后的 ``p2/**``。
    只回其中一个，排错时都要靠脑补。
    """
    add_problems(client, contest, admin_headers, [{"ident": "p1"}, {"ident": "p2"}])
    body = match_path(client, contest, admin_headers, "p2/p2.cpp")
    assert body["problem"] == "p2"
    assert body["patterns"] == ["{ident}/**"]
    assert body["expanded"] == ["p2/**"]


def test_match_reports_title_expansion(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """``{title}`` 的展开结果看得见 —— 标题留空时应当退回标识。"""
    add_problems(
        client, contest, admin_headers,
        [{"ident": "A", "title": "签到题", "file_patterns": ["{title}/**"]}],
    )
    body = match_path(client, contest, admin_headers, "签到题/A.cpp")
    assert body["problem"] == "A"
    assert body["expanded"] == ["签到题/**"]


def test_match_reports_no_hit_honestly(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """认不出来就要说认不出来。

    这个接口的全部意义在于排错 —— 一旦它学会"猜一个差不多的"，
    教师就没法用它区分「模式写对了」和「只是碰巧没炸」。
    """
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    body = match_path(client, contest, admin_headers, "somewhere/else.cpp")
    assert body["problem"] is None
    assert body["patterns"] == []
    assert body["expanded"] == []


def test_match_uses_custom_patterns(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    add_problems(
        client, contest, admin_headers,
        [{"ident": "签到题", "file_patterns": ["{ident}/**", "**/签到题.cpp"]}],
    )
    assert match_path(client, contest, admin_headers, "签到题/a.cpp")["problem"] == "签到题"
    assert match_path(client, contest, admin_headers, "deep/签到题.cpp")["problem"] == "签到题"
    assert match_path(client, contest, admin_headers, "deep/other.cpp")["problem"] is None


def test_match_does_not_touch_the_filesystem(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """试算的是字符串，不是真实文件 —— 教师可以直接粘贴一条"计划中"的路径。"""
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    body = match_path(client, contest, admin_headers, "p1/尚未创建的文件.cpp")
    assert body["problem"] == "p1"


def test_match_on_other_contest_does_not_see_foreign_problems(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """归题只看本场次的清单，别串场。"""
    add_problems(client, contest, admin_headers, [{"ident": "p1"}])
    other = client.post(
        "/api/v1/admin/contests",
        json={"name": "另一场", "slug": "other", "status": "draft"},
        headers=admin_headers,
    ).json()

    response = client.post(
        "/api/v1/admin/contests/%d/problems/match" % other["id"],
        json={"path": "p1/p1.cpp"},
        headers=admin_headers,
    )
    assert response.json()["problem"] is None


def test_match_requires_admin(client: TestClient, contest: dict) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/problems/match" % contest["id"], json={"path": "p1/a.cpp"}
    )
    assert response.status_code == 401
