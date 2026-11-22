"""归档场次（§4.9）：进行中先结束、归档后机器解析不到、扫描与评测跳过、取消归档回到已结束。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from conftest import do_tick as tick

ADMIN = "/api/v1/admin"


def set_status(client, admin_headers, contest, status):
    return client.patch(
        "%s/contests/%d" % (ADMIN, contest["id"]), json={"status": status}, headers=admin_headers
    )


def archive(client, admin_headers, contest):
    return client.post("%s/contests/%d/archive" % (ADMIN, contest["id"]), headers=admin_headers)


def test_进行中的场次不能直接归档(client, admin_headers, contest) -> None:
    response = archive(client, admin_headers, contest)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "contest_running"

    patched = set_status(client, admin_headers, contest, "archived")
    assert patched.status_code == 409 and patched.json()["code"] == "contest_running"


def test_结束后归档_再取消归档回到已结束(client, admin_headers, contest, player) -> None:
    assert set_status(client, admin_headers, contest, "closed").status_code == 200

    archived = archive(client, admin_headers, contest)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archive(client, admin_headers, contest).status_code == 200, "重复归档是空操作"

    # 数据保留、可查看
    players = client.get("%s/contests/%d/players" % (ADMIN, contest["id"]), headers=admin_headers)
    assert players.json()["total"] == 1
    listed = client.get("%s/contests" % ADMIN, headers=admin_headers).json()["items"]
    assert [row["status"] for row in listed] == ["archived"]
    only_active = client.get("%s/contests?archived=false" % ADMIN, headers=admin_headers).json()
    only_archived = client.get("%s/contests?archived=true" % ADMIN, headers=admin_headers).json()
    assert (only_active["total"], only_archived["total"]) == (0, 1)

    restored = client.post("%s/contests/%d/unarchive" % (ADMIN, contest["id"]), headers=admin_headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "closed"


def test_归档后机器不再解析到这一场(client, admin_headers, contest, enrolled) -> None:
    assert tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])["bound"] is True
    set_status(client, admin_headers, contest, "closed")
    archive(client, admin_headers, contest)

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    assert (body["bound"], body["claimed"]) == (False, True), body
    assert "结束" not in (body["reason"] or ""), "已归档的场次不算「你的场次已经结束」"


def test_钉在已归档场次上的机器也不干活_也不能再钉上去(client, admin_headers, contest, enrolled) -> None:
    pin = client.post(
        "%s/agents/%d/contest" % (ADMIN, enrolled["agent_id"]),
        json={"contest_id": contest["id"]},
        headers=admin_headers,
    )
    assert pin.status_code == 200, pin.text
    set_status(client, admin_headers, contest, "closed")
    archive(client, admin_headers, contest)

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert body["bound"] is False and "归档" in body["reason"], body

    again = client.post(
        "%s/agents/%d/contest" % (ADMIN, enrolled["agent_id"]),
        json={"contest_id": contest["id"]},
        headers=admin_headers,
    )
    assert again.status_code == 409 and again.json()["code"] == "contest_archived"


def test_成绩扫描跳过已归档场次(app, client, admin_headers, contest, player) -> None:
    from syncoj_server.tasks.judge import scan_all_contests

    ctx = app.state.ctx
    folder = Path(ctx.settings.judge_result_root) / contest["slug"] / player["player_no"] / "p1"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({"score": 10}), encoding="utf-8")
    set_status(client, admin_headers, contest, "closed")
    archive(client, admin_headers, contest)

    scan_all_contests(ctx)
    url = "%s/contests/%d/judge/runs" % (ADMIN, contest["id"])
    assert client.get(url, headers=admin_headers).json()["total"] == 0

    client.post("%s/contests/%d/unarchive" % (ADMIN, contest["id"]), headers=admin_headers)
    set_status(client, admin_headers, contest, "running")
    scan_all_contests(ctx)
    assert client.get(url, headers=admin_headers).json()["total"] == 1


def test_已归档的场次不排评测_排队中的任务被撤下(app, client, admin_headers, contest, player) -> None:
    from syncoj_server.models import JudgeTask

    with app.state.ctx.db.session() as session:
        session.add(JudgeTask(contest_id=contest["id"], player_id=player["id"], problem="p1", status="queued"))
    set_status(client, admin_headers, contest, "closed")
    archive(client, admin_headers, contest)

    with app.state.ctx.db.session() as session:
        task = session.query(JudgeTask).one()
        assert (task.status, task.error) == ("failed", "场次已归档")

    response = client.post(
        "%s/contests/%d/judge/builtin" % (ADMIN, contest["id"]), json={"force": True}, headers=admin_headers
    )
    assert response.status_code == 409 and response.json()["code"] == "contest_archived"


def test_到点自动结束不会把已归档的场次改回已结束(app, client, admin_headers, contest) -> None:
    from syncoj_server.models import Contest, utcnow
    from syncoj_server.tasks.flush import close_finished_contests

    set_status(client, admin_headers, contest, "closed")
    archive(client, admin_headers, contest)
    with app.state.ctx.db.session() as session:
        session.get(Contest, contest["id"]).ends_at = utcnow() - timedelta(minutes=1)

    assert close_finished_contests(app.state.ctx) == []
    listed = client.get("%s/contests" % ADMIN, headers=admin_headers).json()["items"]
    assert listed[0]["status"] == "archived"
