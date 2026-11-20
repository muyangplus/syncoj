"""配对与机器身份：移除后能回来、一个考号只能有一台机器。"""

from __future__ import annotations

from sqlalchemy import text

from conftest import enroll_machine


def pending_ids(client, admin_headers) -> list:
    body = client.get("/api/v1/admin/machines/pending", headers=admin_headers).json()
    return [row["id"] for row in body["items"]]


def test_移除待配对之后同一台机器还能注册回来(client, admin_headers, bootstrap_key) -> None:
    """审计 S3：曾经只打 revoked_at，machine_uuid 唯一约束 + 认回不看作废行 → 再注册 500。"""
    first = enroll_machine(client, bootstrap_key, hostname="pc-x")
    agent_id = first["agent_id"]
    removed = client.delete(
        "/api/v1/admin/machines/pending/%d?confirm=pc-x" % agent_id, headers=admin_headers
    )
    assert removed.status_code == 200, removed.text

    again = enroll_machine(
        client, bootstrap_key, machine_uuid=first["machine_uuid"], hostname="pc-x"
    )

    assert again["agent_id"] in pending_ids(client, admin_headers)


def test_清空待配对之后整批机器还能注册回来(client, admin_headers, bootstrap_key) -> None:
    machines = [enroll_machine(client, bootstrap_key, hostname="pc-%d" % i) for i in range(3)]
    cleared = client.post(
        "/api/v1/admin/machines/pending/clear", json={"confirm": "all"}, headers=admin_headers
    )
    assert cleared.status_code == 200, cleared.text
    assert pending_ids(client, admin_headers) == []

    for machine in machines:
        enroll_machine(client, bootstrap_key, machine_uuid=machine["machine_uuid"])
    assert len(pending_ids(client, admin_headers)) == 3


def test_老库里已作废的行不再挡住同一台机器注册(client, admin_headers, bootstrap_key, app) -> None:
    """升级上来的库里可能已经有 revoked_at 标记的行（旧版本"移除待配对"留下的）。"""
    first = enroll_machine(client, bootstrap_key, hostname="pc-old")
    with app.state.ctx.db.engine.begin() as conn:
        conn.execute(
            text("UPDATE agent SET revoked_at = '2026-01-01 00:00:00' WHERE id = :id"),
            {"id": first["agent_id"]},
        )

    again = enroll_machine(client, bootstrap_key, machine_uuid=first["machine_uuid"])

    assert again["agent_id"] in pending_ids(client, admin_headers)


def test_两份名单里的同一个考号不能各绑一台机器(client, admin_headers, bootstrap_key, enrolled) -> None:
    """审计 S2：一人一机曾按名单条目判。班级名单与考场名单同时含 S001 是常态，
    两台机器分别绑到两份名单里的 S001，tick 出来都是 S001，往同一个 source/ 写。"""
    from conftest import make_roster

    other = make_roster(
        client, admin_headers, name="考场名单",
        entries=[{"player_no": "S001", "name": "同一个人"}],
    )
    entry_id = other["entries"][0]["id"]
    second = enroll_machine(client, bootstrap_key, hostname="pc-2")

    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": second["pair_code"], "roster_entry_id": entry_id},
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "roster_entry_taken"
