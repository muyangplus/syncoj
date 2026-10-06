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
from sqlalchemy import select

from syncoj_server.models import Agent, Roster, RosterEntry

from conftest import bind_by_code, do_tick as tick, enroll_machine, scan_entry as entry, sha256_of


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
    body = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=headers
    ).json()
    return body["items"]


# --------------------------------------------------------------------------- #
# 基本维护
# --------------------------------------------------------------------------- #


def test_create_and_list_rosters(client: TestClient, admin_headers: dict) -> None:
    make_roster(client, admin_headers, "高一(1)班")
    make_roster(client, admin_headers, "高一(2)班")

    rows = client.get("/api/v1/admin/rosters", headers=admin_headers).json()["items"]
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
        "/api/v1/admin/roster-entries/%d?confirm=S001" % entry_id, headers=admin_headers
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
    # （用另一份名字不同的名单：``enrolled`` 夹具已经建了一份含 S001 的）
    roster = make_roster(client, admin_headers, "临时调整用")
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S999"}])
    report = apply_roster(client, admin_headers, contest, roster["id"], prune=True)

    assert report["protected"] == ["S001"], "有提交的选手必须被保护，并报给教师"
    assert report["pruned"] == 0

    rows = {p["player_no"] for p in players_of(client, admin_headers, contest)}
    assert rows == {"S001", "S999"}, "有提交的选手被删掉了"

    # 代码本身也要还在
    files = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=admin_headers
    ).json()["items"]
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


def test_setting_the_default_roster_does_not_touch_players(
    client: TestClient, admin_headers: dict
) -> None:
    """设/改「默认名单」只是存一个预设，**一个人都不会动**。

    这是刻意选的语义（真正的动作是界面上那个「按这份名单补人」按钮）：场次里的
    选手是从名单复制出去的独立数据，而"改一个设置"顺手创建十几条选手记录，会让
    教师根本分不清到底是哪一步把人弄进来的。

    也正因为如此，界面上必须把动作摆在旁边。此前那里只留了一句"要落到场次请到
    「名单库」点「应用」"—— 教师照着走一遍的结论是"我配了默认名单，选手怎么还是
    空的"，然后来问这是不是 bug。
    """
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001", "name": "张三"}])
    contest = client.post(
        "/api/v1/admin/contests",
        json={"name": "先设名单的场次", "slug": "roster-preset"},
        headers=admin_headers,
    ).json()
    assert contest["player_count"] == 0

    updated = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"default_roster_id": roster["id"]},
        headers=admin_headers,
    ).json()
    # 预设存下来了……
    assert updated["default_roster_id"] == roster["id"]
    # ……但选手一个都没动
    assert updated["player_count"] == 0
    players = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()
    assert players["total"] == 0

    # 显式应用之后才有人 —— 那才是动作
    apply_roster(client, admin_headers, contest, roster["id"])
    after = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()
    assert after["total"] == 1



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

    ack = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )
    assert ack.status_code == 200
    assert "选手" in ack.json()["detail"]

    rows = players_of(client, admin_headers, contest_with_roster)
    assert [p["player_no"] for p in rows] == ["S001"], "删名单把选手也删了"

    # 场次不再指向这份名单
    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()["items"]
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
# 取消名单
# --------------------------------------------------------------------------- #


def test_clear_roster_entries_keeps_the_roster(
    client: TestClient, admin_headers: dict
) -> None:
    """清空条目 ≠ 删名单：名单名下的字段（名称、备注、被哪些场次引用）都要留下。"""
    roster = make_roster(client, admin_headers)
    add_entries(
        client, admin_headers, roster["id"],
        [{"player_no": "S001"}, {"player_no": "S002"}],
    )

    refused = client.post(
        "/api/v1/admin/rosters/%d/entries/clear" % roster["id"],
        json={"confirm": "认错的名字"},
        headers=admin_headers,
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "name_mismatch"

    response = client.post(
        "/api/v1/admin/rosters/%d/entries/clear" % roster["id"],
        json={"confirm": roster["name"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "2 条" in response.json()["detail"]

    detail = client.get(
        "/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers
    ).json()
    assert detail["entries"] == []
    assert detail["name"] == roster["name"], "清空条目不该把名单本身也删掉"


# --------------------------------------------------------------------------- #
# 名单条目会连带机器：删名单 / 清空条目 / 删单条
#
# 机器绑的是名单里的**人**（``Agent.roster_entry_id``），而条目没了那个外键
# 会 ``ON DELETE SET NULL`` —— 于是删名单顺手把一批机器变回"未配对"，它们
# 下次心跳开始显示新的配对码，要教师**一台台重新配**。这条路径此前完全静默，
# 而且违背的正是名单库存在的理由："配对一次、之后 N 场比赛零人工"。
#
# 所以下面这一节盯三件事：
#
# * 会造成**批量**解绑的动作（删名单、清空条目）在真有机器绑着时**拒绝**
# * 被拒绝之后**数据库一行都没动** —— 必须打到库上，因为"先删了再报错"
#   的实现能骗过所有只看响应的断言
# * 只解绑一台的删单条**允许**，但响应该说的副作用（哪几台会掉线）一句都不能少
# --------------------------------------------------------------------------- #


def roster_row(client: TestClient, roster_id: int):
    """直接按主键读库里的名单。

    不用 ``GET /rosters/{id}``（它 404 就抛异常、活着就返回投影）：下面的断言要
    区分"名单还在"和"名单已经没了"，接口层那个包装恰好把两者都变成"没数据"。
    """
    with client.app.state.ctx.db.session() as session:
        return session.get(Roster, roster_id)


def roster_entry_row(client: TestClient, entry_id: int):
    """直接按主键读库里的条目 —— 不看接口投影，只看那一行到底还在不在。"""
    with client.app.state.ctx.db.session() as session:
        return session.get(RosterEntry, entry_id)


def agent_row(client: TestClient, agent_id: int):
    """直接读机器那一行：``roster_entry_id`` 是这条测试唯一有意义的证据。"""
    with client.app.state.ctx.db.session() as session:
        return session.get(Agent, agent_id)


def bound_machine(
    client: TestClient, admin_headers: dict, bootstrap_key: str, entry_id: int, **kwargs
) -> dict:
    """注册一台机器并绑到指定条目上，返回注册响应体（带 ``agent_id``）。"""
    machine = enroll_machine(client, bootstrap_key, **kwargs)
    bind_by_code(client, admin_headers, machine["pair_code"], entry_id)
    return machine


def test_delete_roster_refused_while_a_machine_is_bound(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """有机器绑着时删名单 → 409 ``roster_in_use``，并且把代价说清楚。

    消息里必须有**台数**和**考号**：教师看到"操作失败"只会反复点同一个按钮，
    看到"这台机器（S001）会掉线"才知道该先去「机器配对」里处理谁。
    """
    roster = make_roster(client, admin_headers)
    add_entries(client, admin_headers, roster["id"], [{"player_no": "S001", "name": "张三"}])
    detail = client.get(
        "/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers
    ).json()
    entry_id = detail["entries"][0]["id"]
    bound_machine(client, admin_headers, bootstrap_key, entry_id)

    response = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "roster_in_use"
    assert "1 台机器" in body["detail"], "必须说清有几台机器会掉线"
    assert "S001" in body["detail"], "必须说清是谁被绑着"
    assert body["details"]["count"] == 1
    assert body["details"]["player_nos"] == ["S001"]


def test_refused_delete_touched_nothing_in_the_database(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    """**这一节最重要的一条**：拒绝之后库里一行都没动。

    只断言响应是不够的：一个"先 ``session.delete`` 再检查、发现不行才抛错"的
    实现会返回一模一样的 409，而名单和条目早就没了 —— 机器真被解绑了，
    教师却被提示"什么都没发生"。所以这里的三条断言必须直接打在库上：

    * 名单那一行还在
    * 条目那一行还在（这才是机器 ``roster_entry_id`` 指向的东西）
    * 机器的 ``roster_entry_id`` 还指向原来那个条目

    注意 ``machine`` 夹具只负责"注册上来、还没配对"，绑定要在这里显式做
    （与 ``test_admin_crud.py`` 里那批配对测试一致）—— 否则根本没有任何机器
    绑着，删名单会正常成功，这条测试就测了个空。
    """
    entry = roster["entries"][0]
    machine = bound_machine(client, admin_headers, bootstrap_key, entry["id"])
    assert agent_row(client, machine["agent_id"]).roster_entry_id == entry["id"]

    response = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )
    assert response.status_code == 409, response.text

    assert roster_row(client, roster["id"]) is not None, "拒绝之后名单不该消失"
    assert roster_entry_row(client, entry["id"]) is not None, "拒绝之后条目不该消失"
    agent = agent_row(client, machine["agent_id"])
    assert agent is not None
    assert agent.roster_entry_id == entry["id"], "拒绝之后机器的配对关系不该被拆掉"


def test_roster_without_machines_still_deletes(
    client: TestClient, admin_headers: dict
) -> None:
    """没人绑着时正常路径不能被护栏堵死：名单和条目都该真的没了。

    它和上面那条构成一对：只测"拦住"会漏掉"拦错了"（一个条件写反的实现
    会让所有名单都删不掉，而测试全绿）。
    """
    roster = make_roster(client, admin_headers)
    add_entries(
        client, admin_headers, roster["id"],
        [{"player_no": "S001"}, {"player_no": "S002"}],
    )
    detail = client.get(
        "/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers
    ).json()
    entry_ids = [e["id"] for e in detail["entries"]]
    assert len(entry_ids) == 2

    response = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert roster_row(client, roster["id"]) is None
    for entry_id in entry_ids:
        assert roster_entry_row(client, entry_id) is None, "名单没了，条目也要跟着没"


def test_clear_entries_refused_while_a_machine_is_bound(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """清空条目等于批量解绑，同样拒绝，且库里一行没动。

    和删名单分开测是因为它走的是另一个端点：只测 ``DELETE /rosters`` 会漏掉
    "清空条目"这条更常用的路（教师想换一批人时按的往往是这个按钮）。
    """
    roster = make_roster(client, admin_headers)
    add_entries(
        client, admin_headers, roster["id"],
        [{"player_no": "S001"}, {"player_no": "S002"}],
    )
    detail = client.get(
        "/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers
    ).json()
    entry_ids = [e["id"] for e in detail["entries"]]
    assert len(entry_ids) == 2
    # 只绑第一个人
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_ids[0])

    response = client.post(
        "/api/v1/admin/rosters/%d/entries/clear" % roster["id"],
        json={"confirm": roster["name"]},
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "roster_in_use"
    assert body["details"]["count"] == 1

    # 库里一行都没动 —— 两条条目都还在，机器的绑定也还在
    assert roster_row(client, roster["id"]) is not None
    for entry_id in entry_ids:
        assert roster_entry_row(client, entry_id) is not None, "拒绝之后条目不该消失"
    agent = agent_row(client, machine["agent_id"])
    assert agent is not None
    assert agent.roster_entry_id == entry_ids[0]


def test_delete_single_entry_unbinds_the_machine_and_says_so(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    """删单条**允许**（那本来就是"这个人走了"），但副作用要说出来。

    它和"删名单"是刻意的不同待遇：这里只影响一台机器、是教师看着名字点的，
    拦它反而碍事；但不说的话，教师只会看到"已移除 S001"，然后对着那台机器
    新冒出来的配对码发愣。所以这里同时盯两件事：``detail`` 明说解除了 1 台，
    以及库里那台机器的 ``roster_entry_id`` 真的变成了 ``NULL``（事实不该被藏）。
    """
    entry = roster["entries"][0]
    machine = bound_machine(client, admin_headers, bootstrap_key, entry["id"])
    assert agent_row(client, machine["agent_id"]).roster_entry_id == entry["id"]

    response = client.delete(
        "/api/v1/admin/roster-entries/%d?confirm=%s" % (entry["id"], entry["player_no"]),
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    detail = response.json()["detail"]
    assert "解除了 1 台机器" in detail, detail
    assert "新的配对码" in detail, "还得告诉教师接下来会发生什么"

    # 条目没了，机器还在（没被作废），只是回到了"未配对"
    assert roster_entry_row(client, entry["id"]) is None
    agent = agent_row(client, machine["agent_id"])
    assert agent is not None
    assert agent.roster_entry_id is None, "条目被删，外键该 SET NULL"


def test_unbound_machine_no_longer_blocks_deletion(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    """绑过又解绑之后就能删 —— 护栏拦的是"现在有机器绑着"，不是"曾经被用过"。

    没有这条，一个写成"这份名单历史上出现过机器绑定就一律拒绝"的实现会一直
    绿：现场表现是教师解绑之后仍然删不掉这份名单，而且怎么点都没用。
    """
    entry = roster["entries"][0]
    machine = bound_machine(client, admin_headers, bootstrap_key, entry["id"])

    # 先确认护栏本来是生效的（否则下面的"解绑后能删"可能因为别的原因成立）
    blocked = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )
    assert blocked.status_code == 409, blocked.text

    unbound = client.delete(
        "/api/v1/admin/agents/%d/bind" % machine["agent_id"], headers=admin_headers
    )
    assert unbound.status_code == 200, unbound.text
    assert agent_row(client, machine["agent_id"]).roster_entry_id is None

    entry_ids = [e["id"] for e in roster["entries"]]
    response = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert roster_row(client, roster["id"]) is None
    for entry_id in entry_ids:
        assert roster_entry_row(client, entry_id) is None
    # 解绑不是作废：机器那一行要还在
    assert agent_row(client, machine["agent_id"]) is not None


def test_only_bound_entries_count_as_in_use(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """名单里没人绑的那些人不算数 —— 台数只数**被绑着的人**。

    三个人里只有一个绑了机器，拒删的理由是"会解绑那 1 台"，不是"这份名单有
    三个人"。把没绑的人也算进去（例如漏了 ``Agent.roster_entry_id ==
    RosterEntry.id`` 这个 join 条件）会让台数虚高、把考号列成一串无关的人，
    教师照着提示去"机器配对"里找，一个都找不到。
    """
    roster = make_roster(client, admin_headers)
    add_entries(
        client, admin_headers, roster["id"],
        [{"player_no": "S001"}, {"player_no": "S002"}, {"player_no": "S003"}],
    )
    detail = client.get(
        "/api/v1/admin/rosters/%d" % roster["id"], headers=admin_headers
    ).json()
    by_no = {e["player_no"]: e["id"] for e in detail["entries"]}
    assert set(by_no) == {"S001", "S002", "S003"}
    bound_machine(client, admin_headers, bootstrap_key, by_no["S002"])

    response = client.delete(
        "/api/v1/admin/rosters/%d?confirm=%s" % (roster["id"], roster["name"]),
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "roster_in_use"
    assert body["details"]["count"] == 1, "没绑机器的两个人不该被算进来"
    assert body["details"]["player_nos"] == ["S002"], "只该列出真正被绑的人"
    assert "1 台机器" in body["detail"]


# --------------------------------------------------------------------------- #
# 场次设置
# --------------------------------------------------------------------------- #


def test_contest_can_be_updated_partially(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """只改传了的字段 —— 一次 PATCH 不该把没提到的设置清掉。"""
    roster = make_roster(client, admin_headers)
    client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"default_roster_id": roster["id"]},
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
