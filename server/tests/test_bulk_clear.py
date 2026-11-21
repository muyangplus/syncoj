"""批量清理走一条 ``DELETE``：范围与计数要和逐行删时完全一样。

审计日志与成绩的清理此前没有接口级测试；改成批量删除之后，"删多了 / 删少了 /
计数报错"只会在这里被抓到。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from syncoj_server.models import EventLog, JudgeRun, utcnow


def _events(app, rows) -> None:
    with app.state.ctx.db.session() as session:
        for contest_id, level, age_days in rows:
            session.add(
                EventLog(
                    level=level,
                    category="test",
                    contest_id=contest_id,
                    message="m",
                    ts=utcnow() - timedelta(days=age_days),
                )
            )


def _remaining_events(app):
    with app.state.ctx.db.session() as session:
        return sorted(
            (row.contest_id or 0, row.level)
            for row in session.execute(
                select(EventLog).where(EventLog.category == "test")
            ).scalars()
        )


def test_clear_events_respects_contest_level_and_age(
    app, client: TestClient, admin_headers: dict, contest: dict
) -> None:
    cid = contest["id"]
    _events(app, [(cid, "info", 10), (cid, "warning", 10), (cid, "info", 0), (None, "info", 10)])

    response = client.post(
        "/api/v1/admin/events/clear",
        params={"contest_id": cid, "level": "info", "older_than_days": 5},
        json={"confirm": contest["slug"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "1 条" in response.json()["detail"]
    # 全局事件（不属于任何场次）记作 0
    assert _remaining_events(app) == sorted([(cid, "warning"), (cid, "info"), (0, "info")])

    # 清理本身留一条痕迹
    with app.state.ctx.db.session() as session:
        trail = session.execute(
            select(func.count(EventLog.id)).where(EventLog.category == "events_clear")
        ).scalar_one()
    assert trail == 1

    response = client.post(
        "/api/v1/admin/events/clear", json={"confirm": "all"}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert _remaining_events(app) == []


def test_clear_judge_runs_keeps_manual_scores_by_default(
    app, client: TestClient, admin_headers: dict, contest: dict
) -> None:
    cid = contest["id"]
    imported = client.post(
        "/api/v1/admin/contests/%d/players" % cid,
        json=[{"player_no": "S001"}, {"player_no": "S002"}],
        headers=admin_headers,
    )
    assert imported.status_code == 200, imported.text
    p1, p2 = [row["id"] for row in imported.json()["players"]]
    with app.state.ctx.db.session() as session:
        for player_id, problem, status in (
            (p1, "a", "ok"), (p1, "b", "manual"), (p2, "a", "ok"), (p2, "b", "unparsed"),
        ):
            session.add(
                JudgeRun(contest_id=cid, player_id=player_id, problem=problem,
                         score=1, parse_status=status)
            )

    url = "/api/v1/admin/contests/%d/judge/runs/clear" % cid
    response = client.post(
        url, json={"confirm": contest["slug"], "player_id": p1}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert "1 条成绩" in response.json()["detail"] and "保留 1 条" in response.json()["detail"]

    response = client.post(url, json={"confirm": contest["slug"]}, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert "2 条成绩" in response.json()["detail"]

    with app.state.ctx.db.session() as session:
        left = sorted(
            (row.player_id, row.problem)
            for row in session.execute(select(JudgeRun)).scalars()
        )
    assert left == [(p1, "b")]

    response = client.post(
        url, json={"confirm": contest["slug"], "keep_manual": False}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    with app.state.ctx.db.session() as session:
        assert session.execute(select(func.count(JudgeRun.id))).scalar_one() == 0
