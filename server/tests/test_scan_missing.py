"""「扫描根不存在」的后台提示。

Agent 每轮心跳上报"本机上不存在的那几个扫描根"。服务端拿它做两件事：

1. 机器列表上写一句「选手目录还没建：<路径>」—— 那种情况下扫出来是零个文件，
   与"选手还没开始写"长得一模一样，没有这条提示就只能等考完才发现；
2. **状态变化时**记一条审计（缺失 → warning，恢复 → info）。

第 2 条容易写成"每轮都记"：心跳 20 秒一次，一台机器一个上午能刷出上千条一模一样
的事件，那条审计就再也读不得了。所以这里专门测"同样的值再报一次不重复记"。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from conftest import do_tick
from syncoj_server.models import Agent, EventLog

from syncoj_server.services import scan_missing as sm


def scan_missing_events(app) -> list:
    with app.state.ctx.db.session() as session:
        rows = list(
            session.execute(
                select(EventLog).where(EventLog.category == "scan_missing")
            ).scalars()
        )
    return rows


def stored_paths(app, agent_id: int) -> list:
    with app.state.ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        assert agent is not None
        return sm.load_scan_missing(agent.scan_missing_json)


def machine_row(client, admin_headers, contest, agent_id: int) -> dict:
    response = client.get(
        "/api/v1/admin/contests/%d/agents" % contest["id"],
        params={"limit": 500},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    rows = [item for item in response.json()["items"] if item["agent_id"] == agent_id]
    assert rows, "机器不在这一场的列表里：%s" % response.json()
    return rows[0]


def tick_with(client, token: str, paths, *, machine_id: str):
    return do_tick(
        client, token, [], machine_id=machine_id, stats={"scan_missing": list(paths)}
    )


# --------------------------------------------------------------------------- #
# 存取形态
# --------------------------------------------------------------------------- #


def test_归一化_去重排序并去掉空值() -> None:
    assert sm.normalize([" /b ", "", "/a", "/b", None, "/a"]) == ["/a", "/b"]
    # None 不能变成字面量 "None" 之后被当成一个目录显示出来
    assert sm.normalize([None]) == []


def test_空列表存成_NULL() -> None:
    """空与"没有已知缺失"是同一件事，而 ``NULL`` 让"从来没报过"在库里一眼可辨。"""
    assert sm.dump_scan_missing([]) is None
    assert sm.dump_scan_missing(None) is None
    assert sm.dump_scan_missing(["/a"]) == '["/a"]'
    assert sm.load_scan_missing(None) == []


def test_坏内容当成没有缺失而不是崩() -> None:
    """手工改库或半截写入不该让「机器列表」整个 500。"""
    assert sm.load_scan_missing("不是 JSON") == []
    assert sm.load_scan_missing('{"a": 1}') == []
    # 列表里混进非法类型：能转成字符串的留下，转不了的（None）丢掉
    assert sm.load_scan_missing('["/a", 3]') == ["/a", "3"]
    assert sm.load_scan_missing('["/a", null]') == ["/a"]


# --------------------------------------------------------------------------- #
# 上报 → 行上能读到、事件记一条
# --------------------------------------------------------------------------- #


def test_上报之后行上能读到(app, client, admin_headers, contest, enrolled) -> None:
    body = tick_with(
        client,
        enrolled["token"],
        ["/home/student/桌面/S001"],
        machine_id=enrolled["machine_id"],
    )
    assert body["server_time"] > 0  # 心跳成功（TickResponse 没有 ok 字段）

    assert stored_paths(app, enrolled["agent_id"]) == ["/home/student/桌面/S001"]
    row = machine_row(client, admin_headers, contest, enrolled["agent_id"])
    assert row["scan_missing"] == ["/home/student/桌面/S001"]


def test_缺失出现记一条_warning(app, client, admin_headers, contest, enrolled) -> None:
    tick_with(client, enrolled["token"], ["/a"], machine_id=enrolled["machine_id"])

    rows = scan_missing_events(app)
    assert len(rows) == 1, [r.message for r in rows]
    assert rows[0].level == "warning"
    assert "/a" in rows[0].message
    assert rows[0].agent_id == enrolled["agent_id"]


def test_再报同样的值不重复记事件(app, client, enrolled) -> None:
    """心跳 20 秒一次：按轮记的话一个上午就是上千条一模一样的事件。"""
    tick_with(client, enrolled["token"], ["/a", "/b"], machine_id=enrolled["machine_id"])
    tick_with(client, enrolled["token"], ["/a", "/b"], machine_id=enrolled["machine_id"])
    tick_with(client, enrolled["token"], ["/b", "/a"], machine_id=enrolled["machine_id"])

    rows = scan_missing_events(app)
    assert len(rows) == 1, [r.message for r in rows]
    # 顺带钉住：顺序变了不算状态变化（顺序是 Agent 那边的实现细节）
    assert rows[0].message.count("/a") == 1


def test_集合变了要记新的一条(app, client, enrolled) -> None:
    tick_with(client, enrolled["token"], ["/a"], machine_id=enrolled["machine_id"])
    tick_with(client, enrolled["token"], ["/a", "/b"], machine_id=enrolled["machine_id"])

    rows = scan_missing_events(app)
    assert len(rows) == 2
    assert "/b" in rows[-1].message
    assert rows[-1].level == "warning"


def test_恢复时记一条_info_且列表里清空(app, client, admin_headers, contest, enrolled) -> None:
    tick_with(client, enrolled["token"], ["/a"], machine_id=enrolled["machine_id"])
    tick_with(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    rows = scan_missing_events(app)
    assert len(rows) == 2, [r.message for r in rows]
    assert rows[-1].level == "info"
    assert "/a" in rows[-1].message, "恢复那条要说清是哪几个目录回来了"

    assert stored_paths(app, enrolled["agent_id"]) == []
    row = machine_row(client, admin_headers, contest, enrolled["agent_id"])
    assert row["scan_missing"] == []


def test_恢复之后再缺失会重新记(app, client, enrolled) -> None:
    """恢复不是"以后不再报"：目录被删掉照样要再提示一次。"""
    tick_with(client, enrolled["token"], ["/a"], machine_id=enrolled["machine_id"])
    tick_with(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    tick_with(client, enrolled["token"], ["/a"], machine_id=enrolled["machine_id"])

    rows = scan_missing_events(app)
    assert [r.level for r in rows] == ["warning", "info", "warning"]


def test_没报过这个字段的机器视为没有缺失(app, client, admin_headers, contest, enrolled) -> None:
    """老版本 Agent 不报 ``scan_missing``：缺省空列表 = 没有已知缺失，而不是报错。"""
    body = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    assert body["server_time"] > 0
    assert stored_paths(app, enrolled["agent_id"]) == []
    assert scan_missing_events(app) == []


def test_未配对的机器也记(app, client, admin_headers, bootstrap_key) -> None:
    """目录没建这件事在还没配对的机器上同样值得教师看见。"""
    from conftest import enroll_machine

    machine = enroll_machine(client, bootstrap_key, machine_id="m-scan-missing")

    tick_with(client, machine["token"], ["/a"], machine_id="m-scan-missing")

    assert stored_paths(app, machine["agent_id"]) == ["/a"]


def test_库里的那一列是_json_数组(app, client, enrolled) -> None:
    """存的是 JSON 文本，读回来是列表 —— 两边用同一个模块，不各写一套。"""
    tick_with(client, enrolled["token"], ["/a"], machine_id=enrolled["machine_id"])

    with app.state.ctx.db.session() as session:
        agent = session.get(Agent, enrolled["agent_id"])
        raw = agent.scan_missing_json

    assert json.loads(raw) == ["/a"]
