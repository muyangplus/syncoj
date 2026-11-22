"""配对与机器身份：移除后能回来、配对码查重。多名单绑定见 test_multi_roster_binding.py。"""

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


def test_发码时不与仍有效的码重复(app, monkeypatch) -> None:
    """六位数字的码在 50 台机器里撞上的概率约千分之一；发码时查重。"""
    from datetime import timedelta

    from syncoj_server.models import Agent, utcnow
    from syncoj_server.security import hash_pair_code
    from syncoj_server.services import enrollment

    now = utcnow()
    codes = iter(["111111", "111111", "222222"])
    monkeypatch.setattr(enrollment, "_new_pair_code", lambda length: next(codes))
    with app.state.ctx.db.session() as session:
        session.add(Agent(
            token_hash="t" * 64, machine_id="m-held", pair_code_hash=hash_pair_code("111111"),
            pair_code_expires_at=now + timedelta(minutes=5),
            enrolled_at=now, last_enrolled_at=now, last_seen_at=now,
        ))
        session.flush()
        assert enrollment.unique_pair_code(session, now, 6) == "222222"


def test_同一个码对上两台机器时拒绝按码绑定(client, admin_headers, bootstrap_key, roster_entry, app) -> None:
    """曾经静默绑第一台 —— 把 A 的身份绑给了 B。"""
    from syncoj_server.models import Agent
    from syncoj_server.security import hash_pair_code

    one = enroll_machine(client, bootstrap_key, hostname="pc-a")
    two = enroll_machine(client, bootstrap_key, hostname="pc-b")
    with app.state.ctx.db.session() as session:
        for agent_id in (one["agent_id"], two["agent_id"]):
            session.get(Agent, agent_id).pair_code_hash = hash_pair_code("123456")

    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": "123456", "roster_entry_id": roster_entry["id"]},
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
