"""运行参数：心跳节奏与离线判定。

这三项的关键性质不是"能改"，而是**改完立刻生效**：现场教师看着机器列表觉得
30 秒太钝，希望当场改成 15 秒 —— 而不是去改配置文件再重启服务端（那会打断正在
跑的考试）。所以这一组测试几乎都在证明"同一个进程内，改完之后下一次用到它时
拿到的就是新值"：

* 下一次心跳下发的策略里是新节奏；
* 离线扫描按新阈值判（这是**另一处**用到它的地方 —— 只把心跳改了、离线扫描还
  缓存着旧值，现场表现是"心跳变快了，但机器还是 3 分钟才显示离线"）；
* 重启之后仍然是改过的值（设置要落库，不是内存态）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.models import EventLog, utcnow
from syncoj_server.services import runtime_settings

SETTINGS_URL = "/api/v1/admin/settings/runtime"


# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #


def test_出厂默认就是_30_2_90() -> None:
    """三条默认值写死在代码里，且互相满足约束。"""
    assert runtime_settings.DEFAULTS == {
        "tick_idle_seconds": 30,
        "tick_active_seconds": 2,
        "offline_after_seconds": 90,
    }
    # 离线必须大于空闲心跳，否则空闲机器会在下一轮心跳回来之前就被判离线
    assert (
        runtime_settings.DEFAULTS["offline_after_seconds"]
        > runtime_settings.DEFAULTS["tick_idle_seconds"]
    )


def test_读接口给出默认值(client: TestClient, admin_headers: dict) -> None:
    response = client.get(SETTINGS_URL, headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "tick_idle_seconds": 30,
        "tick_active_seconds": 2,
        "offline_after_seconds": 90,
    }


def test_读接口要管理员(client: TestClient) -> None:
    assert client.get(SETTINGS_URL).status_code == 401


def test_改接口也要管理员(client: TestClient) -> None:
    assert client.put(SETTINGS_URL, json={"tick_idle_seconds": 15}).status_code == 401


# --------------------------------------------------------------------------- #
# 改完立即生效
# --------------------------------------------------------------------------- #


def test_改完之后同一个进程里心跳就用新节奏(
    client: TestClient, admin_headers: dict, enrolled: dict
) -> None:
    """**这条是"立即生效"的判据。**

    先按默认值心跳一次（30/2/90），改完再心跳一次 —— 两次之间没有重启、没有
    重建 app，而下发的策略与 ``next_tick_seconds`` 必须都是新值。
    """
    from conftest import do_tick

    machine_id = enrolled["machine_id"]
    token = enrolled["token"]

    before = do_tick(client, token, [], machine_id=machine_id)
    assert before["next_tick_seconds"] == 30
    assert before["config"]["tick_idle_seconds"] == 30
    assert before["config"]["scan_interval"] == 30

    response = client.put(
        SETTINGS_URL,
        json={
            "tick_idle_seconds": 15,
            "tick_active_seconds": 1,
            "offline_after_seconds": 45,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    after = do_tick(client, token, [], machine_id=machine_id)
    assert after["next_tick_seconds"] == 15
    assert after["config"]["tick_idle_seconds"] == 15
    # 扫描与心跳同周期（用户明确要求绑在一起）
    assert after["config"]["scan_interval"] == 15
    assert after["config"]["tick_active_seconds"] == 1


def test_改完之后离线判定也用新阈值(app, client: TestClient, admin_headers: dict, enrolled: dict) -> None:
    """离线扫描是**另一处**用到这个值的地方，而且它最容易漏。

    只改心跳、离线扫描还按缓存里的 90 秒判，现场表现是"心跳明明变快了，机器
    却还是过一分半才显示离线" —— 这条测试就是钉住那种半吊子的实现。
    """
    from conftest import do_tick

    # 让这台机器"刚刚心跳过"
    do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    runtime = app.state.ctx.registry.get(enrolled["agent_id"])
    assert runtime is not None

    # 把它摆成"60 秒前心跳过"：默认阈值 90 秒下它在线，改成 45 秒后离线
    runtime.last_tick_at = utcnow() - timedelta(seconds=60)
    runtime.online = True

    from syncoj_server.tasks import flush as flush_task

    flush_task.flush_once(app.state.ctx)
    assert app.state.ctx.registry.get(enrolled["agent_id"]).online is True, (
        "60 秒没心跳，默认阈值 90 秒下它该还在线"
    )

    # 改成 15/1/45：同一个 60 秒的间隔现在应当判为离线
    response = client.put(
        SETTINGS_URL,
        json={"tick_idle_seconds": 15, "offline_after_seconds": 45},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    flush_task.flush_once(app.state.ctx)
    assert app.state.ctx.registry.get(enrolled["agent_id"]).online is False, (
        "阈值已经改成 45 秒了，60 秒没心跳该判离线 —— 多半是离线扫描还在用旧值"
    )


def test_设置真的落库了_重启后还在(workdir, isolated_key_dir) -> None:
    """设置存的是库，不是内存 —— 重启服务端不该把它忘掉。"""
    from fastapi.testclient import TestClient as _TestClient

    from conftest import ADMIN_PASSWORD, ADMIN_USER
    from syncoj_server.config import Settings
    from syncoj_server.main import create_app
    from syncoj_server.models import Admin
    from syncoj_server.security import hash_password

    settings = Settings()
    settings.data_root = workdir / "data"

    def make_client():
        app = create_app(settings)
        client = _TestClient(app)
        client.__enter__()
        return app, client

    app, client = make_client()
    try:
        with app.state.ctx.db.session() as session:
            session.add(
                Admin(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD))
            )
        login = client.post(
            "/api/v1/admin/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        )
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        response = client.put(
            SETTINGS_URL,
            json={"tick_idle_seconds": 17, "offline_after_seconds": 51},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    finally:
        client.__exit__(None, None, None)

    # 第二个 app、同一个 data_root：读出来的必须是改过的值
    app2, client2 = make_client()
    try:
        with app2.state.ctx.db.session() as session:
            live = runtime_settings.load(session)
        assert live.tick_idle_seconds == 17
        assert live.offline_after_seconds == 51
    finally:
        client2.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_离线必须大于空闲心跳(client: TestClient, admin_headers: dict) -> None:
    response = client.put(
        SETTINGS_URL,
        json={"tick_idle_seconds": 60, "offline_after_seconds": 60},
        headers=admin_headers,
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "bad_request"
    assert "离线判定" in body["detail"] and "空闲心跳" in body["detail"]


def test_离线必须大于空闲心跳_拿库里的当前值一起算(
    client: TestClient, admin_headers: dict
) -> None:
    """只改一项时，另一项取**库里当前的值**再比 —— 这是跨字段校验的真正难点。"""
    # 当前空闲 30：把离线设成 20（小于 30）必须被拒，哪怕这次请求里没提空闲
    response = client.put(
        SETTINGS_URL, json={"offline_after_seconds": 20}, headers=admin_headers
    )

    assert response.status_code == 400, response.text
    assert "必须大于空闲心跳" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload,label",
    [
        ({"tick_idle_seconds": 4}, "空闲心跳"),
        ({"tick_idle_seconds": 3601}, "空闲心跳"),
        ({"tick_active_seconds": 0}, "有活心跳"),
        ({"tick_active_seconds": 61}, "有活心跳"),
        ({"offline_after_seconds": 9}, "离线判定"),
        ({"offline_after_seconds": 86401}, "离线判定"),
    ],
)
def test_越界的值被拒(client: TestClient, admin_headers: dict, payload: dict, label: str) -> None:
    """越界一律拒绝，**不静默夹紧** —— 夹紧会让教师以为改成了他要的值。"""
    response = client.put(SETTINGS_URL, json=payload, headers=admin_headers)

    assert response.status_code == 422, response.text
    assert label in response.json()["detail"]


def test_非整数被拒(client: TestClient, admin_headers: dict) -> None:
    response = client.put(
        SETTINGS_URL, json={"tick_idle_seconds": "十五"}, headers=admin_headers
    )

    assert response.status_code == 422, response.text


def test_不认识的参数被拒(client: TestClient, admin_headers: dict) -> None:
    """多一个没人认的字段要拒（``extra="forbid"``）：静默忽略等于"改了没生效"。"""
    response = client.put(
        SETTINGS_URL, json={"tick_idle_seconds": 20, "scan_interval_seconds": 60},
        headers=admin_headers,
    )

    assert response.status_code == 422, response.text


def test_被拒之后库里的值没变(client: TestClient, admin_headers: dict) -> None:
    client.put(
        SETTINGS_URL, json={"tick_idle_seconds": 60, "offline_after_seconds": 30},
        headers=admin_headers,
    )

    assert client.get(SETTINGS_URL, headers=admin_headers).json()["tick_idle_seconds"] == 30


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #


def test_改动记审计_含前后值(client: TestClient, admin_headers: dict, app) -> None:
    response = client.put(
        SETTINGS_URL,
        json={"tick_idle_seconds": 20, "offline_after_seconds": 50},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    with app.state.ctx.db.session() as session:
        rows = list(
            session.execute(
                select(EventLog).where(EventLog.category == "runtime_settings")
            ).scalars()
        )

    assert len(rows) == 1, [r.message for r in rows]
    row = rows[0]
    assert row.level == "warning"
    # 谁改的、哪一项、从多少到多少
    assert "admin" in row.message
    assert "tick_idle_seconds：30 → 20" in row.message
    assert "offline_after_seconds：90 → 50" in row.message

    import json

    meta = json.loads(row.meta_json)
    assert meta["before"]["tick_idle_seconds"] == 30
    assert meta["after"]["tick_idle_seconds"] == 20


def test_同样的值不算改动(client: TestClient, admin_headers: dict, app) -> None:
    """提交一份与当前完全一样的值：不该留下"改了"的审计。"""
    response = client.put(
        SETTINGS_URL,
        json={"tick_idle_seconds": 30, "offline_after_seconds": 90},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    with app.state.ctx.db.session() as session:
        rows = list(
            session.execute(
                select(EventLog).where(EventLog.category == "runtime_settings")
            ).scalars()
        )
    assert rows == []


# --------------------------------------------------------------------------- #
# 存取形态
# --------------------------------------------------------------------------- #


def test_只改一项时另外两项不动(app, client: TestClient, admin_headers: dict) -> None:
    client.put(SETTINGS_URL, json={"tick_active_seconds": 5}, headers=admin_headers)

    body = client.get(SETTINGS_URL, headers=admin_headers).json()
    assert body == {
        "tick_idle_seconds": 30,
        "tick_active_seconds": 5,
        "offline_after_seconds": 90,
    }


def test_库里那一格坏了就退回出厂值(app, client: TestClient, admin_headers: dict) -> None:
    """手工改库把值写坏时，宁可退回出厂值继续跑，也不要让整个心跳 500。

    心跳 500 的后果是**全机房的机器一起失联** —— 比"节奏退回默认"严重得多。
    """
    from syncoj_server.models import RuntimeSetting

    with app.state.ctx.db.session() as session:
        session.add(RuntimeSetting(key="tick_idle_seconds", value="不是数字"))

    with app.state.ctx.db.session() as session:
        live = runtime_settings.load(session)

    assert live.tick_idle_seconds == runtime_settings.DEFAULTS["tick_idle_seconds"]


def test_库里几项互相矛盾时整组退回出厂值(app) -> None:
    """有人手工把离线改得比空闲还小：整组退回出厂值，而不是带着矛盾的组合跑。"""
    from syncoj_server.models import RuntimeSetting

    with app.state.ctx.db.session() as session:
        session.add(RuntimeSetting(key="tick_idle_seconds", value="600"))
        session.add(RuntimeSetting(key="offline_after_seconds", value="30"))

    with app.state.ctx.db.session() as session:
        live = runtime_settings.load(session)

    assert live.as_dict() == runtime_settings.DEFAULTS
