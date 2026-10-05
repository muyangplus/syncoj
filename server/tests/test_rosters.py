"""名单库测试。

名单的存在意义是"一次录入、反复使用"，所以测试的重点不是增删改查，
而是**名单和场次之间的那道边界**：

* 改名单**不会**动任何场次（名单是模板，不是引用）
* 应用名单是**显式**动作，且默认不删人
* 即使开了 ``prune``，已经有代码或成绩的选手也一个都不能少

最后一条是这份文件里最重要的断言。名单调整是常事，顺手把参赛者的提交
一起删掉是不可逆的事故 —— 而且它不会报错，只会让人第二天发现"代码不见了"。
"""

from __future__ import annotations

from typing import List

from fastapi.testclient import TestClient

from conftest import do_tick as tick, scan_entry as entry, sha256_of


def make_roster(client: TestClient, headers: dict, name: str = "高一(1)班") -> dict:
    response = client.post("/api/v1/admin/rosters", json={"name": name}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def add_entries(client: TestClient, headers: dict, roster_id: int, items: List[dict]) -> dict:
    response = client.post(
        "/api/v1/admin/rosters/%d/entries" % roster_id, json=items, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def apply_roster(
    client: TestClient, headers: dict, contest: dict, roster_id: int, prune: bool = False
) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/players/apply-roster" % contest["id"],
        json={"roster_id": roster_id, "prune": prune},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def players_of(client: TestClient, headers: dict, contest: dict) -> List[dict]:
    return client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=headers
    ).json()


# --------------------------------------------------------------------------- #
# 基本维护
# --------------------------------------------------------------------------- #


def test_create_and_list_rosters(client: TestClient, admin_headers: dict) -> None:
    make_roster(client, admin_headers, "高一(1)班")
    make_roster(client, admin_headers, "高一(2)班")

    rows = client.get("/api/v1/admin/rosters", headers=admin_headers).json()
    assert [r["name"] for r in rows] == ["高一(1)班", "高一(2)班"]
    assert all(r["entry_count"] == 0 for r in rows)


def test_duplicate_roster_name_is_rejected(client: TestClient, admin_headers: dict) -> None:
    make_roster(client, admin_headers, "同一个名字")
    response = client.post("/api/v1/admin/rosters", json={"name": "同一个名字"}, headers=admin_headers)
    assert response.status_code == 409


def test_import_entries_is_idempotent(client: TestClient, admin_headers: dict) -> None:
    """名单要能反复导 —— 教师拿到更新版名单时会直接再导一次。"""
    roster = make_roster(client, admin_headers)
    items = [{"player_no": "S001", "name": "张三"}, {"player_no": "S002", "name": "李四"}]

    first = add_entries(client, admin_headers, roster["id"], items)
    assert (first["created"], first["updated"]) == (2, 0)

    second = add_entries(client, admin_headers, roster["id"], items)
    assert (second["created"], second["updated"]) == (0, 2)

    detail = client.get("/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers).json()
    assert len(detail["entries"]) == 2


def test_one_bad_row_does_not_fail_the_batch(client: TestClient, admin_headers: dict) -> None:
    """一份名单几十上百行，因为一个空格全部白填是没人能接受的。"""
    roster = make_roster(client, admin_headers)
    result = add_entries(
        client, admin_headers, roster["id"],
        [
            {"player_no": "S001"},
            {"player_no": "   "},
            {"player_no": "S001"},          # 本次导入内重复
            {"player_no": "x" * 100},       # 超长
            {"player_no": "S003"},
        ],
    )

    assert result["created"] == 2
    assert result["skipped"] == 3
    assert len(result["errors"]) == 3

    detail = client.get("/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers).json()
    assert [e["player_no"] for e in detail["entries"]] == ["S001", "S003"]


def test_delete_entry(client: TestClient, admin_headers: dict) -> None:
    roster = make_roster(client, admin_headers)
    result = add_entries(
        client, admin_headers, roster["id"], [{"player_no": "S001"}, {"player_no": "S002"}]
    )
    entry_id = result["entries"][0]["id"]

    assert client.delete(
        "/api/v1/admin/roster-entries/%d" % entry_id, headers=admin_headers
    ).status_code == 200

    detail = client.get("/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers).json()
    assert [e["player_no"] for e in detail["entries"]] == ["S002"]


def test_rosters_require_admin(client: TestClient) -> None:
    assert client.get("/api/v1/admin/rosters").status_code == 401


def test_entries_into_missing_roster(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/api/v1/admin/rosters/9999/entries", json=[{"player_no": "S001"}], headers=admin_headers
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 应用到场次
# --------------------------------------------------------------------------- #


def test_apply_creates_players(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    roster = make_roster(client, admin_headers)
    add_entries(
        client, admin_headers, roster["id"],
        [
            {"player_no": "S001", "name": "张三", "seat": "A1"},
            {"player_no": "S002", "name": "李四"},
        ],
    )

    report = apply_roster(client, admin_headers, contest, roster["id"])
    assert (report["created"], report["updated"]) == (2, 0)
    assert report["roster_name"] == "高一(1)班"

    rows = {p["player_no"]: p for p in players_of(client, admin_headers, contest)}
    assert rows["S001"]["name"] == "张三"
    assert rows["S001"]["seat"] == "A1"


def test_apply_is_idempotent(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001"}])

    apply_roster(client, admin_headers, contest, roster["id"])
    second = apply_roster(client, admin_headers, contest, roster["id"])

    assert (second["created"], second["updated"]) == (0, 1)
    assert len(players_of(client, admin_headers, contest)) == 1


def test_apply_updates_changed_fields(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """名单改了姓名/座位，重新应用要能更新过去。"""
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001", "name": "旧名"}])
    apply_roster(client, admin_headers, contest, roster["id"])

    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001", "name": "新名"}])
    apply_roster(client, admin_headers, contest, roster["id"])

    assert players_of(client, admin_headers, contest)[0]["name"] == "新名"


def test_apply_keeps_extra_players_by_default(
    client: TestClient, admin_headers: dict, contest: dict, player: dict
) -> None:
    """名单里没有的选手默认**保留** —— 名单是用来补人的，不是用来清场的。"""
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S999"}])

    report = apply_roster(client, admin_headers, contest, roster["id"])

    assert report["pruned"] == 0
    assert report["kept"] == 1
    assert {p["player_no"] for p in players_of(client, admin_headers, contest)} == {"S001", "S999"}


def test_prune_removes_players_without_evidence(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001"}])
    client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S001"}, {"player_no": "临时加的"}],
        headers=admin_headers,
    )

    report = apply_roster(client, admin_headers, contest, roster["id"], prune=True)

    assert report["pruned"] == 1
    assert {p["player_no"] for p in players_of(client, admin_headers, contest)} == {"S001"}


def test_prune_never_deletes_a_player_with_submissions(
    client: TestClient, admin_headers: dict, contest: dict, enrolled: dict
) -> None:
    """**这条是整份文件里最重要的断言。**

    名单调整是常事，顺手把参赛者的代码一起删掉是不可逆的事故 ——
    而且它不报错，只会让人第二天发现"某个学生的代码不见了"。
    """
    # S001 是 enrolled 夹具对应的选手，让它交一份代码
    content = b"int main() { return 0; }\n"
    tick(client, enrolled["token"], [entry("p1/p1.cpp", content)], machine_id=enrolled["machine_id"])
    upload = client.post(
        "/api/v1/agent/files",
        data={"path": "p1/p1.cpp", "sha256": sha256_of(content)},
        files={"file": ("p1.cpp", content, "application/octet-stream")},
        headers={"Authorization": "Bearer " + enrolled["token"]},
    )
    assert upload.status_code == 200, upload.text

    # 名单里只有另一个人，S001 不在其中 —— 而且开了 prune
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S999"}])
    report = apply_roster(client, admin_headers, contest, roster["id"], prune=True)

    assert report["protected"] == ["S001"], "有提交的选手必须被保护，并报给教师"
    assert report["pruned"] == 0

    rows = {p["player_no"] for p in players_of(client, admin_headers, contest)}
    assert rows == {"S001", "S999"}, "有提交的选手被删掉了"

    # 代码本身也要还在
    files = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=admin_headers
    ).json()
    assert [f["rel_path"] for f in files] == ["p1/p1.cpp"]


def test_apply_without_roster_is_a_clear_error(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/players/apply-roster" % contest["id"],
        json={},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert "名单" in response.json()["detail"]


def test_apply_uses_the_contests_default_roster(
    client: TestClient, admin_headers: dict
) -> None:
    """场次配了默认名单之后，应用时不用再传 roster_id。"""
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001"}])

    contest = client.post(
        "/api/v1/admin/contests",
        json={"name": "带名单的场次", "slug": "with-roster", "default_roster_id": roster["id"]},
        headers=admin_headers,
    ).json()
    assert contest["default_roster_name"] == "高一(1)班"

    response = client.post(
        "/api/v1/admin/contests/%d/players/apply-roster" % contest["id"],
        json={},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1


# --------------------------------------------------------------------------- #
# 名单与场次之间的边界
# --------------------------------------------------------------------------- #


def test_editing_a_roster_does_not_touch_contests(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """名单是模板，场次是从它**复制**出去的独立数据。

    引用关系会让"改一下名单"顺带改掉历史场次的参赛者 —— 那时成绩矩阵的列、
    代码目录、下发目标全都会跟着动。教师想要的从来不是这个效果。
    """
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001", "name": "张三"}])
    apply_roster(client, admin_headers, contest, roster["id"])

    # 名单里改名 + 加人
    add_entries(
        client, admin_headers, roster["id"],
        [{"player_no": "S001", "name": "改过的名字"}, {"player_no": "S002", "name": "新人"}],
    )

    rows = {p["player_no"]: p for p in players_of(client, admin_headers, contest)}
    assert rows["S001"]["name"] == "张三", "改名单不该自动影响已应用的场次"
    assert "S002" not in rows


def test_deleting_a_roster_keeps_contest_players(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """删模板不该牵动已发生的比赛。"""
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001", "name": "张三"}])

    contest_with_roster = client.post(
        "/api/v1/admin/contests",
        json={"name": "引用名单", "slug": "ref", "default_roster_id": roster["id"]},
        headers=admin_headers,
    ).json()
    apply_roster(client, admin_headers, contest_with_roster, roster["id"])

    ack = client.delete("/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers)
    assert ack.status_code == 200
    assert "选手" in ack.json()["detail"]

    rows = players_of(client, admin_headers, contest_with_roster)
    assert [p["player_no"] for p in rows] == ["S001"], "删名单把选手也删了"

    # 场次不再指向这份名单
    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()
    target = next(c for c in listed if c["id"] == contest_with_roster["id"])
    assert target["default_roster_id"] is None
    assert target["default_roster_name"] is None


def test_one_roster_can_serve_many_contests(
    client: TestClient, admin_headers: dict
) -> None:
    """一次录入反复使用 —— 这是名单库存在的全部理由。"""
    roster = make_roster(client, admin_headers)
    add_entries(
        client, admin_headers, roster["id"],
        [{"player_no": "S001", "name": "张三"}, {"player_no": "S002", "name": "李四"}],
    )

    for i in range(3):
        contest = client.post(
            "/api/v1/admin/contests",
            json={"name": "第 %d 场" % i, "slug": "round-%d" % i, "default_roster_id": roster["id"]},
            headers=admin_headers,
        ).json()
        report = apply_roster(client, admin_headers, contest, roster["id"])
        assert report["created"] == 2
        assert len(players_of(client, admin_headers, contest)) == 2


# --------------------------------------------------------------------------- #
# 场次注册方式
# --------------------------------------------------------------------------- #


def test_contest_defaults_to_per_player_code(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """老场次与新建场次的默认都是每选手注册码 —— 原有行为不能变。"""
    assert contest["enrollment_mode"] == "per_player_code"


def test_enrollment_mode_can_be_switched(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"enrollment_mode": "bootstrap"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["enrollment_mode"] == "bootstrap"


def test_unknown_enrollment_mode_is_rejected(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"enrollment_mode": "随便写的"},
        headers=admin_headers,
    )
    assert response.status_code == 400


def test_contest_can_be_updated_partially(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """只改传了的字段 —— 一次 PATCH 不该把没提到的设置清掉。"""
    roster = make_roster(client, admin_headers)
    client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"default_roster_id": roster["id"], "enrollment_mode": "bootstrap"},
        headers=admin_headers,
    )

    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"name": "改了名字"},
        headers=admin_headers,
    )
    body = response.json()
    assert body["name"] == "改了名字"
    assert body["default_roster_id"] == roster["id"]
    assert body["enrollment_mode"] == "bootstrap"
    assert body["slug"] == contest["slug"], "slug 不该被 PATCH 改掉"


def test_default_roster_can_be_cleared(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """``None`` 分不清"没传"和"要清空"，所以要有显式开关。

    没有它，教师一次选错名单就再也改不回来了。
    """
    roster = make_roster(client, admin_headers)
    client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"default_roster_id": roster["id"]},
        headers=admin_headers,
    )

    cleared = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"clear_default_roster": True},
        headers=admin_headers,
    ).json()
    assert cleared["default_roster_id"] is None


def test_updating_missing_contest_is_404(client: TestClient, admin_headers: dict) -> None:
    response = client.patch(
        "/api/v1/admin/contests/9999", json={"name": "x"}, headers=admin_headers
    )
    assert response.status_code == 404
