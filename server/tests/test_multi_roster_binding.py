"""一台机器绑多份名单（§4.6）：唯一约束、场次按名单取人、配对 / 改派 / 解绑只动一份名单、
同名建议与名单级「按姓名匹配机器」。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from conftest import bind_by_code, do_tick as tick, enroll_machine, make_roster
from syncoj_server.services.bindings import BoundPerson, pick_binding

ADMIN = "/api/v1/admin"


def bound_machine(client, admin_headers, bootstrap_key, entry_id, **kwargs) -> dict:
    machine = enroll_machine(client, bootstrap_key, **kwargs)
    bind_by_code(client, admin_headers, machine["pair_code"], entry_id)
    return machine


def bind_by_machine(client, admin_headers, agent_id, entry_id):
    return client.post(
        "%s/machines/%d/bind" % (ADMIN, agent_id),
        json={"roster_entry_id": entry_id},
        headers=admin_headers,
    )


def entry_of(roster: dict, player_no: str) -> dict:
    return next(e for e in roster["entries"] if e["player_no"] == player_no)


def new_contest(client, admin_headers, slug, roster_id=None, status="running") -> dict:
    body = {"name": slug, "slug": slug, "status": status}
    if roster_id is not None:
        body["default_roster_id"] = roster_id
    response = client.post("%s/contests" % ADMIN, json=body, headers=admin_headers)
    assert response.status_code == 200, response.text
    contest = response.json()
    if roster_id is not None:
        applied = client.post(
            "%s/contests/%d/players/apply-roster" % (ADMIN, contest["id"]),
            json={"roster_id": roster_id},
            headers=admin_headers,
        )
        assert applied.status_code == 200, applied.text
    return contest


@pytest.fixture()
def two_rosters(client, admin_headers) -> tuple:
    """班级名单 A（1 号张三、2 号李四）与考场名单 B（考号另编：1 号是李四、2 号是张三）。"""
    a = make_roster(
        client, admin_headers, name="名单A",
        entries=[{"player_no": "1", "name": "张三", "seat": "A1"}, {"player_no": "2", "name": "李四"}],
    )
    b = make_roster(
        client, admin_headers, name="名单B",
        entries=[{"player_no": "1", "name": "李四"}, {"player_no": "2", "name": " 张三　"}],
    )
    return a, b


# --------------------------------------------------------------------------- #
# 纯函数：场次取人
# --------------------------------------------------------------------------- #


def person(roster_id, no, agent_id=1) -> BoundPerson:
    return BoundPerson(agent_id=agent_id, roster_id=roster_id, entry_id=roster_id * 100, player_no=no)


def test_场次指定了名单时只认那份名单里的绑定() -> None:
    persons = [person(1, "1"), person(2, "2")]
    assert pick_binding(2, persons, {"1", "2"}, {}).person.player_no == "2"
    assert pick_binding(3, persons, {"1", "2"}, {}).code == "not_in_contest"
    missing = pick_binding(1, persons, {"2"}, {})
    assert missing.code == "player_missing" and not missing.ok


def test_场次没指定名单时按考号对应且不猜() -> None:
    assert pick_binding(None, [person(1, "1")], {"1"}, {"1": 1}).ok
    assert pick_binding(None, [person(1, "1"), person(2, "2")], {"1", "2"}, {}).code == "ambiguous"
    assert pick_binding(None, [person(1, "1")], {"1"}, {"1": 2}).code == "contested"
    assert pick_binding(None, [person(1, "1")], {"9"}, {}).code == "not_in_contest"


# --------------------------------------------------------------------------- #
# 唯一约束
# --------------------------------------------------------------------------- #


def test_一台机器可以同时是名单A的1号和名单B的2号(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])

    response = bind_by_machine(client, admin_headers, machine["agent_id"], entry_of(b, "2")["id"])

    assert response.status_code == 200, response.text
    listed = client.get("%s/machines/bound" % ADMIN, headers=admin_headers).json()
    assert listed["total"] == 1
    assert [(x["roster_name"], x["player_no"]) for x in listed["items"][0]["bindings"]] == [
        ("名单A", "1"),
        ("名单B", "2"),
    ]


def test_一台机器在一份名单里只能是一个人(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, _ = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])

    response = bind_by_machine(client, admin_headers, machine["agent_id"], entry_of(a, "2")["id"])

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "machine_already_bound"


def test_一个人只能有一台机器(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, _ = two_rosters
    bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    second = enroll_machine(client, bootstrap_key, hostname="pc-2")

    response = bind_by_machine(client, admin_headers, second["agent_id"], entry_of(a, "1")["id"])

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "roster_entry_taken"


def test_两条唯一约束由库守着(app, client, admin_headers, bootstrap_key, two_rosters) -> None:
    from syncoj_server.models import AgentBinding

    a, _ = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    other = enroll_machine(client, bootstrap_key, hostname="pc-2")
    roster_id = a["id"]

    for agent_id, entry_id in (
        (machine["agent_id"], entry_of(a, "2")["id"]),
        (other["agent_id"], entry_of(a, "1")["id"]),
    ):
        with pytest.raises(IntegrityError):
            with app.state.ctx.db.session() as session:
                session.add(AgentBinding(agent_id=agent_id, roster_entry_id=entry_id, roster_id=roster_id))
                session.flush()


# --------------------------------------------------------------------------- #
# 场次解析
# --------------------------------------------------------------------------- #


def test_场次按所用名单取考号(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    assert bind_by_machine(client, admin_headers, machine["agent_id"], entry_of(b, "2")["id"]).status_code == 200
    contest_b = new_contest(client, admin_headers, "exam-b", roster_id=b["id"])

    body = tick(client, machine["token"], [], machine_id=machine["machine_id"])

    assert body["bound"] is True, body
    assert body["player_no"] == "2", "名单B里这台机器是 2 号（名单A里它是 1 号）"
    agents = client.get("%s/contests/%d/agents" % (ADMIN, contest_b["id"]), headers=admin_headers).json()
    assert [row["player_no"] for row in agents["items"]] == ["2"]
    assert len(agents["items"][0]["bindings"]) == 2


def test_没绑场次名单的机器不进这一场_哪怕考号撞上(client, admin_headers, bootstrap_key, two_rosters) -> None:
    """名单A的 1 号与名单B的 1 号是两个人：只绑了 A 的机器不能落到 B 场的 1 号上。"""
    a, b = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    contest_b = new_contest(client, admin_headers, "exam-b", roster_id=b["id"])

    body = tick(client, machine["token"], [], machine_id=machine["machine_id"])

    assert body["bound"] is False and body["claimed"] is True, body
    players = client.get("%s/contests/%d/players" % (ADMIN, contest_b["id"]), headers=admin_headers).json()
    assert not any(row["has_agent"] for row in players["items"])


def test_显式指定场次也按场次名单取人(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    bind_by_machine(client, admin_headers, machine["agent_id"], entry_of(b, "2")["id"])
    new_contest(client, admin_headers, "exam-a", roster_id=a["id"])
    contest_b = new_contest(client, admin_headers, "exam-b", roster_id=b["id"])

    ambiguous = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert ambiguous["bound"] is False and "多个" in ambiguous["reason"], ambiguous

    pinned = client.post(
        "%s/agents/%d/contest" % (ADMIN, machine["agent_id"]),
        json={"contest_id": contest_b["id"]},
        headers=admin_headers,
    )
    assert pinned.status_code == 200, pinned.text
    body = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert (body["bound"], body["player_no"], body["contest_slug"]) == (True, "2", "exam-b"), body


def test_未指定名单的场次_考号被两台机器分别绑着时谁也不进(
    client, admin_headers, bootstrap_key, two_rosters, contest
) -> None:
    """``contest`` 夹具没有默认名单：退回按考号对应，但同一个考号不让两台机器共用。"""
    a, b = two_rosters
    client.post(
        "%s/contests/%d/players" % (ADMIN, contest["id"]), json=[{"player_no": "1"}], headers=admin_headers
    )
    one = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    two = bound_machine(client, admin_headers, bootstrap_key, entry_of(b, "1")["id"], hostname="pc-2")

    for machine in (one, two):
        body = tick(client, machine["token"], [], machine_id=machine["machine_id"])
        assert body["bound"] is False, body


# --------------------------------------------------------------------------- #
# 改派 / 解绑只动一份名单
# --------------------------------------------------------------------------- #


def test_改派只换目标名单里的那条(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    bind_by_machine(client, admin_headers, machine["agent_id"], entry_of(b, "2")["id"])

    response = client.post(
        "%s/agents/%d/rebind" % (ADMIN, machine["agent_id"]),
        json={"roster_entry_id": entry_of(b, "1")["id"]},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    bindings = client.get("%s/machines/bound" % ADMIN, headers=admin_headers).json()["items"][0]["bindings"]
    assert [(x["roster_name"], x["player_no"]) for x in bindings] == [("名单A", "1"), ("名单B", "1")]


def test_解绑可以只解一份名单(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    machine = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    bind_by_machine(client, admin_headers, machine["agent_id"], entry_of(b, "2")["id"])

    response = client.delete(
        "%s/agents/%d/bind?roster_id=%d" % (ADMIN, machine["agent_id"], b["id"]), headers=admin_headers
    )

    assert response.status_code == 200, response.text
    bindings = client.get("%s/machines/bound" % ADMIN, headers=admin_headers).json()["items"][0]["bindings"]
    assert [x["roster_name"] for x in bindings] == ["名单A"]
    assert tick(client, machine["token"], [], machine_id=machine["machine_id"])["claimed"] is True

    everything = client.delete("%s/agents/%d/bind" % (ADMIN, machine["agent_id"]), headers=admin_headers)
    assert everything.status_code == 200, everything.text
    pending = client.get("%s/machines/pending" % ADMIN, headers=admin_headers).json()
    assert [row["id"] for row in pending["items"]] == [machine["agent_id"]]


# --------------------------------------------------------------------------- #
# 智能匹配
# --------------------------------------------------------------------------- #


def test_配对回执带上其他名单里的同名建议(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    machine = enroll_machine(client, bootstrap_key)

    result = bind_by_code(client, admin_headers, machine["pair_code"], entry_of(a, "1")["id"])

    assert [(s["roster_name"], s["player_no"], s["source"]) for s in result["suggestions"]] == [
        ("名单B", "2", "名单A · 1 张三")
    ]
    again = client.get(
        "%s/agents/%d/binding-suggestions" % (ADMIN, machine["agent_id"]), headers=admin_headers
    ).json()
    assert [s["roster_entry_id"] for s in again["items"]] == [entry_of(b, "2")["id"]]


def test_名单级按姓名匹配_预览不写库_应用后生效(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    zhang = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"], hostname="pc-z")
    li = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "2")["id"], hostname="pc-l")

    preview = client.post("%s/rosters/%d/match-machines/preview" % (ADMIN, b["id"]), headers=admin_headers)

    assert preview.status_code == 200, preview.text
    pairs = {(p["agent_id"], p["player_no"]) for p in preview.json()["pairs"]}
    assert pairs == {(zhang["agent_id"], "2"), (li["agent_id"], "1")}
    listed = client.get("%s/machines/bound" % ADMIN, headers=admin_headers).json()["items"]
    assert all(len(row["bindings"]) == 1 for row in listed), "预览不能写库"

    applied = client.post(
        "%s/rosters/%d/match-machines/apply" % (ADMIN, b["id"]),
        json={"pairs": [
            {"agent_id": p["agent_id"], "roster_entry_id": p["roster_entry_id"]}
            for p in preview.json()["pairs"]
        ]},
        headers=admin_headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"bound": 2, "failed": []}

    again = client.post("%s/rosters/%d/match-machines/preview" % (ADMIN, b["id"]), headers=admin_headers)
    assert again.json()["pairs"] == []
    assert {s["reason"] for s in again.json()["skipped"]} == {"已绑定"}


def test_名单级应用逐对重新校验(client, admin_headers, bootstrap_key, two_rosters) -> None:
    a, b = two_rosters
    zhang = bound_machine(client, admin_headers, bootstrap_key, entry_of(a, "1")["id"])
    other = bound_machine(client, admin_headers, bootstrap_key, entry_of(b, "2")["id"], hostname="pc-x")

    applied = client.post(
        "%s/rosters/%d/match-machines/apply" % (ADMIN, b["id"]),
        json={"pairs": [
            {"agent_id": zhang["agent_id"], "roster_entry_id": entry_of(b, "2")["id"]},
            {"agent_id": zhang["agent_id"], "roster_entry_id": entry_of(a, "2")["id"]},
        ]},
        headers=admin_headers,
    )

    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["bound"] == 0
    assert [f["player_no"] for f in body["failed"]] == ["2", "?"]
    assert other["agent_id"]
