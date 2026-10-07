"""「回收只认题目预设」。

用户的说法是"注意同步代码严格按照服务端预设读取。不同步不匹配的代码。如名称不对，
位置不对等等"。落地成一句话：**这一场配了题目之后，扫描上来的文件必须匹配某个题目
的模式才进台账、才要求上传。**

三个刻意的决定（都写在这里，因为它们的"为什么"从代码上看不出来）：

1. **一场都没配题目时一律照收**。教师还没建题目就什么都收不上来，是最糟的默认值；
   严格只在"有预设可依"时才严格。
2. **被挡掉的路径仍然算进"本轮见过"**。否则教师改完模式的那一刻，之前按老模式收进来
   的条目会被当成"选手删除了文件" —— 审计里会出现一条与事实相反的记录。
3. **被挡掉的记一条 warning，但只在"有没有"发生变化时记**。心跳 30 秒一次，
   按轮记一个上午就是上千条一模一样的事件。

另外钉住"教师能自己放宽"：默认模式是最窄的那个，要收整个题目目录就显式写
``{ident}/**``。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from conftest import do_tick, scan_entry

from syncoj_server.models import Agent, EventLog, SourceFile


def tick(client, enrolled, entries, **kwargs):
    return do_tick(
        client, enrolled["token"], entries, machine_id=enrolled["machine_id"], **kwargs
    )


def import_problems(client, admin_headers, contest, items) -> list:
    response = client.post(
        "/api/v1/admin/contests/%d/problems" % contest["id"],
        json=items,
        headers=admin_headers,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def ledger(app, player_id: int) -> list:
    with app.state.ctx.db.session() as session:
        return list(
            session.execute(
                select(SourceFile).where(SourceFile.player_id == player_id)
            ).scalars()
        )


def paths(app, player_id: int) -> list:
    return sorted(row.rel_path for row in ledger(app, player_id))


def unmatched_events(app) -> list:
    return session_events(app, "scan_no_match")


def session_events(app, category: str) -> list:
    with app.state.ctx.db.session() as session:
        return list(
            session.execute(
                select(EventLog).where(EventLog.category == category)
            ).scalars()
        )


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


def stored_skipped(app, agent_id: int):
    with app.state.ctx.db.session() as session:
        agent = session.get(Agent, agent_id)
        assert agent is not None
        return agent.scan_skipped


# --------------------------------------------------------------------------- #
# 过滤本身
# --------------------------------------------------------------------------- #


def test_配了题目就只收匹配的文件(
    app, client, admin_headers, contest, player, enrolled
) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    body = tick(
        client,
        enrolled,
        [scan_entry("p1/p1.cpp", b"int main(){}"), scan_entry("notes.txt", b"hh")],
    )

    assert body["need_upload"] == ["p1/p1.cpp"]
    assert paths(app, player["id"]) == ["p1/p1.cpp"]


def test_没配题目时一律照收(
    app, client, admin_headers, contest, player, enrolled
) -> None:
    """教师还没建题目就什么都收不上来，是最糟的默认值。"""
    body = tick(
        client,
        enrolled,
        [scan_entry("p1/p1.cpp", b"a"), scan_entry("随便放/随便写.txt", b"b")],
    )

    assert sorted(body["need_upload"]) == ["p1/p1.cpp", "随便放/随便写.txt"]
    assert paths(app, player["id"]) == ["p1/p1.cpp", "随便放/随便写.txt"]


def test_默认模式是题目目录下的同名_cpp(
    app, client, admin_headers, contest, player, enrolled
) -> None:
    """默认 ``{ident}/{ident}.cpp``：同一题目录下别的东西不算这次提交。"""
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    body = tick(
        client,
        enrolled,
        [
            scan_entry("p1/p1.cpp", b"a"),
            scan_entry("p1/p1.cpp.bak", b"b"),
            scan_entry("p1/样例说明.txt", b"c"),
            scan_entry("p2/p2.cpp", b"d"),
        ],
    )

    assert body["need_upload"] == ["p1/p1.cpp"]


def test_自定义模式能收整个题目目录(
    app, client, admin_headers, contest, player, enrolled
) -> None:
    """要收整个目录就显式写 ``{ident}/**`` —— "严格"不等于"没救"。"""
    import_problems(
        client,
        admin_headers,
        contest,
        [{"ident": "p1", "file_patterns": ["{ident}/**"]}],
    )

    body = tick(client, enrolled, [scan_entry("p1/随便写.txt", b"a")])

    assert body["need_upload"] == ["p1/随便写.txt"]


def test_被挡掉的文件不参与_sha256_校验(
    app, client, admin_headers, contest, enrolled
) -> None:
    """本来就不该收的文件，它的 sha256 合不合法都不该变成"待处理的拒绝条目"。"""
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    bad = {"path": "notes.txt", "sha256": "0" * 64, "size": 1, "mtime": 1}
    body = tick(client, enrolled, [bad])

    assert body["need_upload"] == []
    assert session_events(app, "scan_rejected") == []


# --------------------------------------------------------------------------- #
# 老条目：不动，不误判成删除
# --------------------------------------------------------------------------- #


def test_改模式之后老条目不会被当成选手删除(
    app, client, admin_headers, contest, player, enrolled
) -> None:
    """先按"没配题目"收了 ``notes.txt``，再配一个只认 ``p1/p1.cpp`` 的题目。

    那条老记录必须**原样留着**：它既不该被删掉，也不该因为内容变了就被重新上传。
    否则教师改一次模式，审计里就会出现一批"选手删除了文件"。
    """
    tick(client, enrolled, [scan_entry("notes.txt", b"a")])

    import_problems(client, admin_headers, contest, [{"ident": "p1"}])
    body = tick(
        client,
        enrolled,
        [scan_entry("notes.txt", b"changed"), scan_entry("p1/p1.cpp", b"b")],
    )

    assert "notes.txt" not in body["need_upload"], "不该再收的文件不许重新上传"
    rows = {row.rel_path: row for row in ledger(app, player["id"])}
    assert rows["notes.txt"].deleted_at is None
    assert rows["p1/p1.cpp"].deleted_at is None


def test_文件真从磁盘上消失时仍然算删除(
    app, client, admin_headers, contest, player, enrolled
) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])
    tick(client, enrolled, [scan_entry("p1/p1.cpp", b"a")])

    tick(client, enrolled, [])

    rows = {row.rel_path: row for row in ledger(app, player["id"])}
    assert rows["p1/p1.cpp"].deleted_at is not None
    assert len(session_events(app, "file_deleted")) == 1


# --------------------------------------------------------------------------- #
# 上报与事件
# --------------------------------------------------------------------------- #


def test_被挡掉的文件数上报到机器行(
    app, client, admin_headers, contest, enrolled
) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    tick(client, enrolled, [scan_entry("a.txt", b"a"), scan_entry("b.txt", b"b")])

    assert stored_skipped(app, enrolled["agent_id"]) == 2
    assert machine_row(client, admin_headers, contest, enrolled["agent_id"])["scan_skipped"] == 2


def test_不再被挡时数字清零(
    app, client, admin_headers, contest, enrolled
) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])
    tick(client, enrolled, [scan_entry("a.txt", b"a")])
    tick(client, enrolled, [scan_entry("p1/p1.cpp", b"a")])

    assert stored_skipped(app, enrolled["agent_id"]) is None
    assert machine_row(client, admin_headers, contest, enrolled["agent_id"])["scan_skipped"] == 0


def test_被挡掉时记一条_warning(app, client, admin_headers, contest, enrolled) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    tick(client, enrolled, [scan_entry("a.txt", b"a")])

    rows = unmatched_events(app)
    assert len(rows) == 1, [row.message for row in rows]
    assert rows[0].level == "warning"
    assert "a.txt" in rows[0].message
    assert rows[0].agent_id == enrolled["agent_id"]


def test_同样的数量再报一次不重复记(app, client, admin_headers, contest, enrolled) -> None:
    """心跳 30 秒一次：按轮记的话一个上午就是上千条一模一样的事件。"""
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    tick(client, enrolled, [scan_entry("a.txt", b"a")])
    tick(client, enrolled, [scan_entry("a.txt", b"a")])
    # 数量变了也不是状态变化 —— 9 个变 10 个不值得再记一条
    tick(client, enrolled, [scan_entry("a.txt", b"a"), scan_entry("b.txt", b"b")])

    assert len(unmatched_events(app)) == 1


def test_恢复正常时记一条_info(app, client, admin_headers, contest, enrolled) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    tick(client, enrolled, [scan_entry("a.txt", b"a")])
    tick(client, enrolled, [scan_entry("p1/p1.cpp", b"a")])

    rows = unmatched_events(app)
    assert [row.level for row in rows] == ["warning", "info"]
    assert rows[-1].agent_id == enrolled["agent_id"]


def test_恢复之后再被挡会重新记(app, client, admin_headers, contest, enrolled) -> None:
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    tick(client, enrolled, [scan_entry("a.txt", b"a")])
    tick(client, enrolled, [scan_entry("p1/p1.cpp", b"a")])
    tick(client, enrolled, [scan_entry("a.txt", b"a")])

    assert [row.level for row in unmatched_events(app)] == ["warning", "info", "warning"]


def test_样本截断但计数完整(app, client, admin_headers, contest, enrolled) -> None:
    """挡掉几百个文件是可能的：事件里的清单截到 20 条，但数字要是完整的。"""
    import_problems(client, admin_headers, contest, [{"ident": "p1"}])

    entries = [scan_entry("f%02d.txt" % index, b"a") for index in range(25)]
    tick(client, enrolled, entries)

    rows = unmatched_events(app)
    assert len(rows) == 1
    meta = json.loads(rows[0].meta_json)
    assert meta["count"] == 25
    assert len(meta["sample"]) == 20
    assert meta["truncated"] is True
    assert stored_skipped(app, enrolled["agent_id"]) == 25


def test_没配题目时不记这类事件(app, client, admin_headers, contest, enrolled) -> None:
    tick(client, enrolled, [scan_entry("a.txt", b"a")])

    assert unmatched_events(app) == []
    assert stored_skipped(app, enrolled["agent_id"]) is None
