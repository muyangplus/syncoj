"""场次的可配置考试时间窗：``starts_at`` / ``ends_at``。

三条规则，这个文件围着它们转：

1. **都不填 = 不限制**（这是默认）。判反了（把 NULL 当成"已经结束"）会让每一个
   没配时间的场次当场拒收代码，而"没配时间"正是出厂状态 —— 练习场、还没定时间的
   场次全都在这一档。
2. **只填一个也合法**：``starts_at`` 只管开考，``ends_at`` 只管结束。
3. **时间窗之外不收代码**（``POST /api/v1/agent/files``）。**只挡这一条路**：
   tick、下载 assets、事件上报照常 —— 到点之后机器还得活着、还得能拿到题面。

外加一条后台动作：到点把场次自动置为"已结束"（那是**状态**，界面显示与机器解析
靠它）。它与上传门禁是两条独立的判据，因为它们回答的是两个不同的问题：
"界面该怎么显示" 与 "现在这份代码收不收"。维护循环是周期跑的，**"到点那一刻"
不能靠等它**，所以门禁必须自己看时间 —— 这也是下面那些测试要把自动结束先按住
的原因（不按住，测到的就是"谁先跑完"，而不是门禁本身）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from conftest import agent_headers, do_tick, sha256_of

from syncoj_server.api import agent as agent_api
from syncoj_server.models import Contest, ContestStatus, EventLog, utcnow
from syncoj_server.tasks import flush


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def iso(moment) -> str:
    """服务端自己的时间形状（``_iso`` / ``models.iso_utc`` 输出的就是它）。"""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def upload(client: TestClient, token: str, path: str, content: bytes):
    return client.post(
        "/api/v1/agent/files",
        data={"path": path, "sha256": sha256_of(content)},
        files={"file": (path.rsplit("/", 1)[-1], content, "application/octet-stream")},
        headers=agent_headers(token),
    )


def upload_asset(client: TestClient, contest: dict, admin_headers: dict, name: str, data: bytes):
    return client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": (name, data, "application/octet-stream")},
        headers=admin_headers,
    )


def make_deploy(client: TestClient, contest: dict, admin_headers: dict, asset_id: int):
    return client.post(
        "/api/v1/admin/contests/%d/deploys" % contest["id"],
        json={
            "asset_id": asset_id,
            "target_kind": "all",
            "dest_dir": "exam",
            "mode": "overwrite",
        },
        headers=admin_headers,
    )


def set_window(client: TestClient, admin_headers: dict, contest_id: int, **fields: Any) -> dict:
    """走管理端 PATCH 改时间窗：``None`` = 清掉那一端，**不传** = 不动。"""
    response = client.patch(
        "/api/v1/admin/contests/%d" % contest_id, json=fields, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def contest_state(app, contest_id: int) -> Dict[str, Any]:
    """直接查库。断言要落在**库**上：接口回显对了不等于存对了。"""
    with app.state.ctx.db.session() as session:
        row = session.get(Contest, contest_id)
        return {"status": row.status, "starts_at": row.starts_at, "ends_at": row.ends_at}


def put_ends_at(app, contest_id: int, moment) -> None:
    """绕过 API 直接写库。

    用来造"时间已经过了、但状态还没来得及跟上"的那一瞬间（真实世界里它就是
    维护循环下一次醒来之前的那个窗口）。
    """
    with app.state.ctx.db.session() as session:
        session.get(Contest, contest_id).ends_at = moment


def events(app, contest_id: Optional[int] = None) -> List[EventLog]:
    with app.state.ctx.db.session() as session:
        stmt = select(EventLog)
        if contest_id is not None:
            stmt = stmt.where(EventLog.contest_id == contest_id)
        return list(session.execute(stmt).scalars())


def hold_auto_close(monkeypatch) -> None:
    """把后台的"到点自动结束"按住，模拟"维护循环还没跑到"的那一瞬间。

    要测的正是那一刻：门禁必须自己看时间。不按住的话，状态会被后台先一步置成
    "已结束"，于是 ``require_agent`` 先以"没有进行中的场次"把人挡在门外，
    门禁那条分支根本走不到 —— 测试会变成"看谁先跑完"。
    """
    monkeypatch.setattr(flush, "close_finished_contests", lambda ctx, now=None: [])


# --------------------------------------------------------------------------- #
# 默认：不限制
# --------------------------------------------------------------------------- #


def test_fresh_database_has_the_window_columns(client: TestClient, app) -> None:
    """全新库直接建出这两列，而且**可空** —— 可空才表示得出"不限制"。

    顺带确认上一轮的 ``contest.player_notice`` 已经不在表里了（迁移 005 的职责之一）。
    """
    columns = {
        c["name"]: bool(c["nullable"])
        for c in inspect(app.state.ctx.db.engine).get_columns("contest")
    }

    assert columns.get("starts_at") is True
    assert columns.get("ends_at") is True
    assert "player_notice" not in columns


def test_no_window_means_no_restriction(
    client: TestClient, enrolled: dict, contest: dict, app
) -> None:
    """**这份文件里最要紧的一条**：两个时间都不填 = 不限制。

    判反了（比如把 NULL 当"已结束"）会让每一个没配时间的场次立刻拒收代码，
    而那是出厂默认状态 —— 文件里其它测试全都建立在"没配时间能上传"之上。
    """
    assert contest["starts_at"] is None and contest["ends_at"] is None, (
        "建场次默认就不该带时间窗（这是『不限制』，不是『已结束』）"
    )
    state = contest_state(app, contest["id"])
    assert state["starts_at"] is None and state["ends_at"] is None

    response = upload(client, enrolled["token"], "code.cpp", b"int main() { return 0; }")

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 时间窗之外不收代码
# --------------------------------------------------------------------------- #


def test_ends_at_passed_rejects_upload_without_any_audit_event(
    client: TestClient, enrolled: dict, contest: dict, app, monkeypatch
) -> None:
    """结束之后不再收代码；**而且不留违规记录**。

    用户要的是"自动结束、不再接收"，不是"记一笔犯规"：被挡下的上传只是
    "这场不收"，不点名任何人、也不该在审计日志里出现 warning。
    这条断言专门用来防"顺手加一条违规事件"重新长回来。
    """
    # 把后台维护循环整个停掉：只有这样"事件表是空的"才是确定性的断言
    # （离线告警与自动结束事件都由它产生，与本次上传无关）。
    monkeypatch.setattr(flush, "flush_once", lambda ctx: 0)
    put_ends_at(app, contest["id"], utcnow() - timedelta(minutes=1))

    response = upload(client, enrolled["token"], "code.cpp", b"int main() { return 0; }")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "contest_ended"
    # 这句话是给**选手**看的，所以在测试里也按"人能读懂"来断言：
    # 说清"什么时候结束的、代码还收不收"
    assert "结束" in body["detail"] and "代码不再接收" in body["detail"]
    # 机器可读的那一份：Agent 不该去解析中文（中文是给人看的）
    assert body["details"]["reason"] == "ended"

    assert events(app, contest["id"]) == [], (
        "被时间门禁挡下的上传不该留下任何审计事件（尤其不该有 late_upload / warning）"
    )


def test_starts_at_in_the_future_rejects_upload(
    client: TestClient, enrolled: dict, contest: dict, admin_headers: dict, monkeypatch
) -> None:
    """还没到开考时间，同样不收 —— 理由与结束后是同一句：现在不是考试时间。"""
    hold_auto_close(monkeypatch)
    set_window(
        client, admin_headers, contest["id"], starts_at=iso(utcnow() + timedelta(minutes=30))
    )

    response = upload(client, enrolled["token"], "code.cpp", b"int main() { return 0; }")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "contest_not_started"
    assert "还没开始" in body["detail"]
    assert body["details"]["reason"] == "not_started"


def test_ends_at_exactly_now_counts_as_ended(
    client: TestClient, enrolled: dict, contest: dict, app, monkeypatch
) -> None:
    """边界：``ends_at`` **恰好等于**"现在"要按已结束处理（窗口是左闭右开）。

    判据写成 ``now > ends_at`` 的话，最后一刻交上来的东西会落进一个已经结束的
    场次里 —— 而那是没人打算再收的卷子。
    """
    hold_auto_close(monkeypatch)
    moment = utcnow()
    put_ends_at(app, contest["id"], moment)
    # 把"服务端现在几点了"钉死在 ends_at 那一刻，边界才测得住（否则只能测到
    # "结束时间早于上传时刻"这种宽松情形）
    monkeypatch.setattr(agent_api, "utcnow", lambda: moment)

    response = upload(client, enrolled["token"], "code.cpp", b"int main() { return 0; }")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "contest_ended"


# --------------------------------------------------------------------------- #
# 只配一个也生效
# --------------------------------------------------------------------------- #


def test_only_starts_at_configured(
    client: TestClient, enrolled: dict, contest: dict, admin_headers: dict, monkeypatch
) -> None:
    """只配开考时间 = 只管开考，没有"结束"这件事。"""
    hold_auto_close(monkeypatch)
    set_window(
        client, admin_headers, contest["id"], starts_at=iso(utcnow() - timedelta(minutes=1))
    )
    assert upload(client, enrolled["token"], "a.cpp", b"a").status_code == 200

    # 同一个场次把开考时间推到未来 → 立刻变成"还没开始"
    set_window(
        client, admin_headers, contest["id"], starts_at=iso(utcnow() + timedelta(minutes=1))
    )
    response = upload(client, enrolled["token"], "b.cpp", b"b")

    assert response.status_code == 409
    assert response.json()["code"] == "contest_not_started"


def test_only_ends_at_configured(
    client: TestClient, enrolled: dict, contest: dict, admin_headers: dict, app, monkeypatch
) -> None:
    """只配结束时间 = 只管结束，开考与否不作数。"""
    hold_auto_close(monkeypatch)
    set_window(
        client, admin_headers, contest["id"], ends_at=iso(utcnow() + timedelta(hours=1))
    )
    assert upload(client, enrolled["token"], "a.cpp", b"a").status_code == 200

    put_ends_at(app, contest["id"], utcnow() - timedelta(minutes=1))
    response = upload(client, enrolled["token"], "b.cpp", b"b")

    assert response.status_code == 409
    assert response.json()["code"] == "contest_ended"


def test_window_can_be_cleared_with_an_explicit_null(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """传 ``null`` = 清掉那一端，**不传** = 不动。

    两者分不开的话（比如都用 ``is not None`` 判），教师永远删不掉一个填错的
    时间点，而界面上看起来是保存成功了 —— 那种"改完又变回去"的故障最难查。
    """
    set_window(
        client,
        admin_headers,
        contest["id"],
        starts_at="2026-06-01T01:00:00Z",
        ends_at="2026-06-01T03:00:00Z",
    )

    body = set_window(client, admin_headers, contest["id"], ends_at=None)

    assert body["ends_at"] is None, "显式传 null 必须真的把结束时间清掉"
    assert body["starts_at"] == "2026-06-01T01:00:00Z", "没传的那一端不该被动"


# --------------------------------------------------------------------------- #
# 写入时校验窗口
# --------------------------------------------------------------------------- #


def test_inverted_window_is_rejected_when_creating(
    client: TestClient, admin_headers: dict
) -> None:
    """``starts_at > ends_at`` 在写入时就拒绝：这种窗口永远不可能成立。"""
    response = client.post(
        "/api/v1/admin/contests",
        json={
            "name": "配反了的场次",
            "slug": "inverted-1",
            "status": "running",
            "starts_at": "2026-06-01T12:00:00Z",
            "ends_at": "2026-06-01T09:00:00Z",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400, response.text
    assert "开考时间" in response.json()["detail"]
    # 被拒绝的请求不该留下一个半成品场次
    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()
    assert listed["items"] == []


def test_inverted_window_is_rejected_when_updating(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """PATCH 同样要拦，而且看的是**改完之后**的那两个值（不是只看请求里的）。"""
    set_window(client, admin_headers, contest["id"], starts_at="2026-06-01T12:00:00Z")

    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"ends_at": "2026-06-01T11:00:00Z"},
        headers=admin_headers,
    )

    assert response.status_code == 400, response.text
    row = next(
        item
        for item in client.get("/api/v1/admin/contests", headers=admin_headers).json()["items"]
        if item["id"] == contest["id"]
    )
    assert row["ends_at"] is None, "被拒绝的那次不能留下半个改动"
    assert row["starts_at"] == "2026-06-01T12:00:00Z"


def test_window_survives_a_round_trip(client: TestClient, admin_headers: dict) -> None:
    """配的时间要**原样回来**：不是本地时间、不是空、也不是被悄悄改掉。"""
    created = client.post(
        "/api/v1/admin/contests",
        json={
            "name": "有时间窗的场次",
            "slug": "window-1",
            "status": "running",
            "starts_at": "2026-06-01T01:00:00Z",
            "ends_at": "2026-06-01T03:30:00Z",
        },
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    assert created.json()["starts_at"] == "2026-06-01T01:00:00Z"
    assert created.json()["ends_at"] == "2026-06-01T03:30:00Z"

    # 带偏移的量要按它自己声明的时区折算（+08:00 的 09:00 就是 UTC 的 01:00）
    body = set_window(
        client, admin_headers, created.json()["id"], ends_at="2026-06-01T11:30:00+08:00"
    )
    assert body["ends_at"] == "2026-06-01T03:30:00Z"


# --------------------------------------------------------------------------- #
# 到点自动置为"已结束"
# --------------------------------------------------------------------------- #


def test_maintenance_closes_a_finished_contest(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """到点的**进行中**场次由维护循环置为"已结束"，并留一条 info 审计事件。

    这一步管的是**状态**：界面要显示"已结束"，机器也不该再被解析进一场已经结束的
    考试里。事件是给教师事后查的（"这场怎么自己结束了"），所以必须是 info、
    必须落在 event_log 里、也必须能在管理端的事件列表里看到。
    """
    set_window(client, admin_headers, contest["id"], ends_at=iso(utcnow() - timedelta(minutes=1)))

    flush.close_finished_contests(app.state.ctx)

    assert contest_state(app, contest["id"])["status"] == ContestStatus.CLOSED
    auto = [e for e in events(app, contest["id"]) if e.category == "contest_auto_closed"]
    assert len(auto) == 1, "到点结束应当**恰好**记一条事件"
    assert auto[0].level == "info", "这是状态变更的审计，不是对谁的告警"
    assert auto[0].player_id is None, "不该点名任何选手"
    assert "自动置为已结束" in auto[0].message

    listed = client.get(
        "/api/v1/admin/contests/%d/events" % contest["id"], headers=admin_headers
    )
    assert listed.status_code == 200, listed.text
    assert any(item["category"] == "contest_auto_closed" for item in listed.json()["items"]), (
        "教师必须能在场次事件里看到它 —— 只写在服务端日志里等于没记"
    )


def test_maintenance_round_actually_closes_finished_contests(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """这一步必须**真的挂在维护循环上**，而不只是"有一个能关场次的函数"。

    单独测 ``close_finished_contests()`` 是不够的：把 ``flush_once`` 里那一行
    删掉，函数本身还在、上面那些测试全绿，而线上到点之后场次永远不会结束 ——
    这是"只测了函数、没测接线"最典型的翻车方式（本项目已经栽过一次同类问题）。
    """
    set_window(client, admin_headers, contest["id"], ends_at=iso(utcnow() - timedelta(minutes=1)))

    flush.flush_once(app.state.ctx)

    assert contest_state(app, contest["id"])["status"] == ContestStatus.CLOSED


def test_auto_close_is_idempotent(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    """同一场次只该被关一次：第二遍什么都不做、也不再记事件。"""
    set_window(client, admin_headers, contest["id"], ends_at=iso(utcnow() - timedelta(minutes=1)))
    flush.close_finished_contests(app.state.ctx)

    second = flush.close_finished_contests(app.state.ctx)

    assert second == [], "已经结束的场次不该再被选中"
    assert contest_state(app, contest["id"])["status"] == ContestStatus.CLOSED
    auto = [e for e in events(app, contest["id"]) if e.category == "contest_auto_closed"]
    assert len(auto) == 1, "再跑一遍不该多出一条事件"


def test_running_contest_with_a_future_end_is_untouched(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """**没到点的场次一个都不许动。**

    这条防的是"把所有场次一起关掉"那种写法（比如漏了 ``ends_at <= now`` 这一段，
    或者把 ``starts_at`` 当成结束时间用）。真出现的话，全场在考试中途被一次性
    结束掉，而且只留一堆看起来正常的"自动结束"审计。
    """
    set_window(client, admin_headers, contest["id"], ends_at=iso(utcnow() + timedelta(hours=1)))

    flush.close_finished_contests(app.state.ctx)

    assert contest_state(app, contest["id"])["status"] == ContestStatus.RUNNING
    assert [e for e in events(app, contest["id"]) if e.category == "contest_auto_closed"] == []


def test_draft_with_a_past_end_is_also_closed(
    client: TestClient, contest: dict, admin_headers: dict, app
) -> None:
    """``draft``（还没开考）配了结束时间、而且已经过了 → 也要关掉。

    语义是选的：**配了结束时间就该按它结束**。一个忘了点"开考"的场次留在
    ``draft`` 里，界面看起来像"还没开始"，可它的时间早就过去了 —— 教师会一直
    等一个永远不会到的开考。反过来（不关）唯一的"好处"是省掉一次状态变更，
    而那正是需要被人看见的东西。
    """
    set_window(
        client,
        admin_headers,
        contest["id"],
        status="draft",
        ends_at=iso(utcnow() - timedelta(minutes=1)),
    )
    assert contest_state(app, contest["id"])["status"] == ContestStatus.DRAFT

    flush.close_finished_contests(app.state.ctx)

    assert contest_state(app, contest["id"])["status"] == ContestStatus.CLOSED
    assert [e for e in events(app, contest["id"]) if e.category == "contest_auto_closed"]


def test_closed_contest_tells_the_machine_it_ended(
    client: TestClient, enrolled: dict, contest: dict, app
) -> None:
    """自动结束后，机器听到的必须是"你所在的场次已经结束了"。

    给它回"还没有包含你的场次"是不行的：学生明明在考试（刚刚还在交卷），看到
    这句话会以为系统坏了、机器掉线了 —— 然后去重启、去重新注册。
    ``code`` 仍然是 ``no_active_contest``：这两档在 Agent 那边要做的事完全一样
    （安静等着、不要重新注册），而机器不该靠解析中文去区分。
    """
    put_ends_at(app, contest["id"], utcnow() - timedelta(minutes=1))
    flush.close_finished_contests(app.state.ctx)
    assert contest_state(app, contest["id"])["status"] == ContestStatus.CLOSED

    body = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    assert body["bound"] is False
    assert "已经结束" in (body["reason"] or "")


# --------------------------------------------------------------------------- #
# 门禁只加在该加的地方
# --------------------------------------------------------------------------- #


def test_ended_contest_still_allows_tick_assets_and_events(
    client: TestClient, enrolled: dict, contest: dict, admin_headers: dict, app, monkeypatch
) -> None:
    """到点之后，tick / 下载 assets / 事件上报**一律照常**。

    这条防的是"顺手把门禁加到 ``require_agent`` 里"：那样一加，整间机房在到点
    那一刻会连心跳都收不到 —— 现场看起来像全体掉线，而真正该停的只是收卷。
    机器还得活着：拿题面、被教师看见、把现场情况报上来。
    """
    hold_auto_close(monkeypatch)
    data = b"downloadable content" * 10
    asset = upload_asset(client, contest, admin_headers, "exam.zip", data).json()
    make_deploy(client, contest, admin_headers, asset["id"])
    put_ends_at(app, contest["id"], utcnow() - timedelta(minutes=1))

    download = client.get(
        "/api/v1/agent/assets/%d" % asset["id"], headers=agent_headers(enrolled["token"])
    )
    assert download.status_code == 200, download.text
    assert download.content == data

    tick = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert tick["bound"] is True, "到点之后机器仍要能干活（只是不再收代码）"

    reported = client.post(
        "/api/v1/agent/events",
        json=[{"level": "info", "category": "selfcheck", "message": "到点之后的自检上报"}],
        headers=agent_headers(enrolled["token"]),
    )
    assert reported.status_code == 200, reported.text

    # 而代码上传是唯一被挡住的那一个
    assert upload(client, enrolled["token"], "code.cpp", b"x").status_code == 409
