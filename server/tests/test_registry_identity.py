"""在线注册表：身份随解析结果更新；待配对 / 无场次的心跳也算在线。"""

from __future__ import annotations

from sqlalchemy import select

from conftest import do_tick
from syncoj_server.models import Contest
from syncoj_server.registry import AgentRegistry


def test_改派之后注册表里的场次与选手跟着变() -> None:
    registry = AgentRegistry()
    registry.upsert_identity(7, 1, 10, "S001", None, "mock-1", "m")
    registry.upsert_identity(7, 2, 20, "S002", None, "mock-2", "m")

    runtime = registry.get(7)
    assert (runtime.player_id, runtime.contest_id) == (2, 20)
    assert [a.agent_id for a in registry.all(contest_id=20)] == [7]
    assert registry.all(contest_id=10) == []


def test_场次结束后不再挂在注册表里_也不报离线(client, enrolled, app) -> None:
    """场次结束后机器走待配对心跳：从在线注册表里拿掉 —— 既不挂着旧选手显示在线，
    也不会在 offline_after 之后集体报一遍"离线"。"""
    from syncoj_server.models import EventLog
    from syncoj_server.tasks import flush

    do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert app.state.ctx.registry.get(enrolled["agent_id"]) is not None

    with app.state.ctx.db.session() as session:
        contest = session.execute(select(Contest)).scalars().first()
        contest.status = "ended"

    body = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert body["bound"] is False, body
    assert app.state.ctx.registry.get(enrolled["agent_id"]) is None

    app.state.ctx.registry.sweep_offline(0)
    flush.flush_once(app.state.ctx)
    with app.state.ctx.db.session() as session:
        offline = session.execute(
            select(EventLog).where(EventLog.category == "offline")
        ).scalars().all()
    assert offline == []


def test_扫描不完整只在状态变化时记一条(client, enrolled, app) -> None:
    from syncoj_server.models import EventLog

    def count() -> int:
        with app.state.ctx.db.session() as session:
            return len(session.execute(
                select(EventLog).where(EventLog.category == "scan_incomplete")
            ).scalars().all())

    for _ in range(3):
        do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"],
                scan_complete=False)
    assert count() == 1, "同一状态每轮都记会把审计刷成同一句话"

    do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"],
            scan_complete=False)
    assert count() == 2, "状态消失后再出现要再记"
