"""统一密钥注册 + 短码配对。

这是本轮最需要小心的一条链路：它把"服务端怎么知道这台机器是谁"从
"注册码里写着"换成了"教师在机器前确认一次"。测试要守住的是这条链路
**不能悄悄认错人** —— 认错的代价是一个学生的代码落进另一个人的目录，
而界面上看不出任何异常。

所以这里重点是四件事：

* 配对必须把机器认到**正确**的名单条目，且一人一机、一机一人
* 配对码是一次性的、会过期的、要管理员才能用
* 快照还原之后能凭硬件指纹认回同一台机器，**不需要重新配对**；
  但母机还开着的时候绝不允许"认回"（否则克隆机会拿走别人的身份）
* 还没配对的机器**什么都干不了**，而且服务端要如实告诉它为什么
"""

from __future__ import annotations

from datetime import timedelta
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.schemas import GLOBAL_CONFIRM

from conftest import (
    agent_headers,
    bind_by_code,
    enroll_machine,
    issue_bootstrap_key,
    make_roster,
)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def issue_key(client: TestClient, headers: dict, **payload) -> Dict:
    response = client.post("/api/v1/admin/bootstrap-keys", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def pending(client: TestClient, headers: dict) -> List[Dict]:
    """待配对列表的**数据行**（信封里的 items）。

    这里刻意只回列表：绝大多数断言关心的是"有哪些机器"，
    信封的形状有专门的用例去盯（见 ``test_pending_envelope_*``）。
    """
    response = client.get("/api/v1/admin/machines/pending", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def global_events(
    client: TestClient, headers: dict, category: Optional[str] = None
) -> List[Dict]:
    """不分场次的审计事件。

    注册与配对冲突发生在"还没有场次归属"的机器上，所以那些事件查不到任何
    场次里 —— 它们只能从这个接口看到。这也正是它必须存在的原因。
    """
    query = "?category=%s" % category if category else ""
    response = client.get("/api/v1/admin/events" + query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def age_agent(app, machine_uuid: str, seconds: int = 3600) -> None:
    """把某台机器的"最后心跳"推到过去，模拟它关机了一段时间。

    快照还原的真实时序是：机器关机 → 打快照 / 还原 → 再开机。
    期间它一定超过了离线判定阈值，所以这个 helper 不是"绕过保护"，
    而是把测试放到真实的时序里。

    直接改库而不是 sleep：测试不该为了等一个超时慢下来，而且
    ``offline_after_seconds`` 是可配的，sleep 的写法会在改配置时静默失效。
    """
    from syncoj_server.models import Agent, utcnow

    with app.state.ctx.db.session() as session:
        agent = session.execute(
            select(Agent).where(Agent.machine_uuid == machine_uuid)
        ).scalar_one()
        agent.last_seen_at = utcnow() - timedelta(seconds=seconds)


def agent_row(client: TestClient, machine_uuid: str):
    from syncoj_server.models import Agent

    with client.app.state.ctx.db.session() as session:
        return session.execute(
            select(Agent).where(Agent.machine_uuid == machine_uuid)
        ).scalar_one()


def tick_payload(**kwargs) -> Dict:
    """一次最小可用的 tick 请求体。"""
    payload = {
        "agent_version": "0.1.0",
        "machine_id": "machine-x",
        "ts": 1767225600,
        "scan_root": "/home/student/code",
        "scan": [],
        "partials": [],
        "stats": {"disk_free": 10 ** 10, "last_error": None, "queue": 0},
    }
    payload.update(kwargs)
    return payload


def do_tick(client: TestClient, token: str, **kwargs) -> Dict:
    """发一次 tick 并断言成功（返回响应体）。"""
    response = client.post(
        "/api/v1/agent/tick",
        json=tick_payload(**kwargs),
        headers=agent_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 密钥管理
# --------------------------------------------------------------------------- #


def test_issue_and_list_keys(client: TestClient, admin_headers: dict) -> None:
    issued = issue_key(client, admin_headers, label="2025 机房镜像")
    assert issued["key"]
    assert issued["use_count"] == 0
    assert issued["revoked_at"] is None

    body = client.get("/api/v1/admin/bootstrap-keys", headers=admin_headers).json()
    assert [r["label"] for r in body["items"]] == ["2025 机房镜像"]
    # 列表接口**不能**回显明文
    assert "key" not in body["items"][0]
    assert issued["key"] not in str(body)


def test_keys_require_admin(client: TestClient) -> None:
    assert client.get("/api/v1/admin/bootstrap-keys").status_code == 401


def test_revoked_key_is_rejected(client: TestClient, admin_headers: dict) -> None:
    issued = issue_key(client, admin_headers)
    client.post(
        "/api/v1/admin/bootstrap-keys/%d/revoke" % issued["id"], headers=admin_headers
    )

    response = client.post(
        "/api/v1/agent/enroll",
        json={"bootstrap_key": issued["key"], "machine_id": "m-1"},
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "bootstrap_key_revoked"
    assert "吊销" in body["detail"]


def test_expired_key_is_rejected(client: TestClient, admin_headers: dict) -> None:
    """过期与吊销是不同的 code —— Agent 对两者的处置不同（前者等人换镜像，
    后者要查是谁把密钥贴出去了）。"""
    from syncoj_server.models import BootstrapKey, utcnow

    issued = issue_key(client, admin_headers)
    with client.app.state.ctx.db.session() as session:
        key = session.get(BootstrapKey, issued["id"])
        key.expires_at = utcnow() - timedelta(days=1)

    response = client.post(
        "/api/v1/agent/enroll",
        json={"bootstrap_key": issued["key"], "machine_id": "m-1"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "bootstrap_key_expired"


def test_unknown_key_is_rejected(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/api/v1/agent/enroll",
        json={"bootstrap_key": "AAAA-BBBB-CCCC-DDDD", "machine_id": "m-1"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "bootstrap_key_invalid"


def test_enroll_needs_the_key(client: TestClient) -> None:
    """没给密钥是 422（请求不合法），不是 404（密钥不存在）—— 排错方向不同。"""
    response = client.post("/api/v1/agent/enroll", json={"machine_id": "m-1"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_revoking_a_key_does_not_affect_registered_machines(
    client: TestClient, admin_headers: dict, bootstrap_key: str, enrolled: dict
) -> None:
    """已经注册好的机器手里是各自的 token，不是这把密钥。

    这条决定了教师**敢不敢**在比赛期间吊销一把泄漏出去的密钥。
    """
    body = client.get("/api/v1/admin/bootstrap-keys", headers=admin_headers).json()
    key_id = body["items"][0]["id"]
    client.post(
        "/api/v1/admin/bootstrap-keys/%d/revoke" % key_id, headers=admin_headers
    )

    tick = do_tick(client, enrolled["token"], machine_id=enrolled["machine_id"])
    assert tick["claimed"] is True and tick["bound"] is True


# --------------------------------------------------------------------------- #
# 注册出来是"没有归属"的
# --------------------------------------------------------------------------- #


def test_enroll_is_unclaimed(client: TestClient, bootstrap_key: str) -> None:
    body = enroll_machine(client, bootstrap_key)
    assert body["token"]
    assert body["claimed"] is False
    assert body["pair_code"]
    assert body["player_no"] == ""
    assert body["contest_id"] is None


def test_unclaimed_machine_shows_up_in_pending(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    enroll_machine(client, bootstrap_key, machine_id="m-1", hostname="exam-pc-07")

    rows = pending(client, admin_headers)
    assert len(rows) == 1
    assert rows[0]["machine_id"] == "m-1"
    assert rows[0]["hostname"] == "exam-pc-07"
    # 配对码的**明文不能**回给界面：库里只有哈希，服务端自己也不该留
    assert "pair_code" not in rows[0]
    assert rows[0]["pair_code_expires_in"] > 0


def test_unclaimed_tick_says_not_claimed(
    client: TestClient, bootstrap_key: str
) -> None:
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")
    body = do_tick(client, enrolled["token"], machine_id="m-1")
    assert body["claimed"] is False
    assert body["bound"] is False
    assert body["next_tick_seconds"] == 30, "它本来就没事可做，用空闲周期"


def test_unclaimed_machine_cannot_upload(
    client: TestClient, bootstrap_key: str
) -> None:
    """没归属的机器连准考证号都不知道，上传只能是污染台账。

    返回 403 而不是 401 也很重要：401 会让 Agent 以为凭据坏了，于是反复重新
    注册，把真正的"还没配对"盖掉。
    """
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")

    response = client.post(
        "/api/v1/agent/files",
        data={"path": "p1/p1.cpp", "sha256": "0" * 64},
        files={"file": ("p1.cpp", b"int main(){}", "application/octet-stream")},
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "pairing_required"
    assert "配对" in body["detail"]


def test_unclaimed_scan_is_discarded(
    client: TestClient, admin_headers: dict, bootstrap_key: str, contest: dict
) -> None:
    """未配对机器上报的扫描结果一律丢弃 —— 没有准考证号，路径没法归属。"""
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")
    do_tick(
        client,
        enrolled["token"],
        machine_id="m-1",
        scan=[{"path": "p1/p1.cpp", "sha256": "a" * 64, "size": 3, "mtime": 1}],
    )

    files = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=admin_headers
    ).json()
    assert files["items"] == []
    assert files["total"] == 0


# --------------------------------------------------------------------------- #
# 配对
# --------------------------------------------------------------------------- #


def test_bind_by_code_pairs_the_machine_to_the_person(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")

    result = bind_by_code(client, admin_headers, enrolled["pair_code"], entry["id"])
    assert result["player_no"] == "S001"
    assert result["roster_entry_id"] == entry["id"]

    assert pending(client, admin_headers) == []

    tick = do_tick(client, enrolled["token"], machine_id="m-1")
    assert tick["claimed"] is True


def test_bind_by_id_needs_the_pair_code_when_given(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    """主机名可能是重复的（克隆镜像、默认 hostname），所以给了配对码就必须对得上。

    这是"教师确实站在那台机器前面"的唯一证明。
    """
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")

    wrong = client.post(
        "/api/v1/admin/machines/%d/bind" % enrolled["agent_id"],
        json={"roster_entry_id": entry["id"], "pair_code": "000000"},
        headers=admin_headers,
    )
    assert wrong.status_code == 400, wrong.text
    assert wrong.json()["code"] == "pair_code_invalid"

    ok = client.post(
        "/api/v1/admin/machines/%d/bind" % enrolled["agent_id"],
        json={"roster_entry_id": entry["id"], "pair_code": enrolled["pair_code"]},
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text


def test_bind_requires_admin(client: TestClient, bootstrap_key: str) -> None:
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")
    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": enrolled["pair_code"], "roster_entry_id": 1},
    )
    assert response.status_code == 401


def test_expired_pair_code_is_rejected(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1")

    # 把码的有效期推到过去（教师走下讲台花了太久，或者机器被晾了一晚上）
    from datetime import timedelta

    from syncoj_server.models import Agent, utcnow

    with client.app.state.ctx.db.session() as session:
        agent = session.get(Agent, enrolled["agent_id"])
        agent.pair_code_expires_at = utcnow() - timedelta(seconds=1)

    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": enrolled["pair_code"], "roster_entry_id": entry["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "pair_code_invalid"

    # 但下一轮心跳会让机器拿到一个新码 —— 现场不用重启、不用重新装机
    tick = do_tick(client, enrolled["token"], machine_id="m-1")
    assert tick["pair_code"] and tick["pair_code"] != enrolled["pair_code"]

    assert bind_by_code(
        client, admin_headers, tick["pair_code"], entry["id"]
    )["player_no"] == "S001"


def test_one_person_cannot_have_two_machines(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    first = enroll_machine(client, bootstrap_key, machine_id="m-1", hostname="pc-1")
    second = enroll_machine(client, bootstrap_key, machine_id="m-2", hostname="pc-2")

    bind_by_code(client, admin_headers, first["pair_code"], entry["id"])
    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": second["pair_code"], "roster_entry_id": entry["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "roster_entry_taken"


def test_revoke_pending_machine_removes_it_from_the_queue(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1", hostname="pc-1")

    missing = client.delete(
        "/api/v1/admin/machines/pending/%d" % enrolled["agent_id"], headers=admin_headers
    )
    assert missing.status_code == 422, "删机器要打机器名"

    ok = client.delete(
        "/api/v1/admin/machines/pending/%d?confirm=pc-1" % enrolled["agent_id"],
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text
    assert pending(client, admin_headers) == []


def test_revoked_machine_credential_stops_working(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """401（凭据坏了）而不是 403（还没配对）：机器该清掉本地凭据、重新注册。

    这是 401/403 分工里最容易写反的一处 —— 写反了那台被移除的机器会一直
    以"还没配对"的姿态安静地等下去，而现场以为已经把它踢掉了。
    """
    enrolled = enroll_machine(client, bootstrap_key, machine_id="m-1", hostname="pc-1")
    client.delete(
        "/api/v1/admin/machines/pending/%d?confirm=pc-1" % enrolled["agent_id"],
        headers=admin_headers,
    )

    response = client.post(
        "/api/v1/agent/tick",
        json=tick_payload(machine_id="m-1"),
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "machine_revoked"


def test_clear_pending_machines_requires_the_whole_word(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """清空待配对列表是范围删除 —— 确认内容是固定字面量 all。"""
    for i in range(2):
        enroll_machine(client, bootstrap_key, hostname="pc-%d" % i)

    refused = client.post(
        "/api/v1/admin/machines/pending/clear",
        json={"confirm": "pc-0"},
        headers=admin_headers,
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "name_mismatch"

    ok = client.post(
        "/api/v1/admin/machines/pending/clear",
        json={"confirm": GLOBAL_CONFIRM},
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text
    assert pending(client, admin_headers) == []


# --------------------------------------------------------------------------- #
# 快照还原：凭指纹认回同一台机器
# --------------------------------------------------------------------------- #


def test_snapshot_restore_keeps_the_pairing(
    client: TestClient, admin_headers: dict, app, bootstrap_key: str, roster: dict,
    player: dict,
) -> None:
    """整机快照还原会把 ``machine_uuid`` 一起抹掉，但**配对结果不该丢**。

    还原之后机器重新注册：UUID 认不出来，指纹认得出 —— 于是它还是原来那个人，
    教师不用走到每台机器跟前重新配对 50 次。
    """
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    fingerprint = "smbios-" + "c" * 24
    original = enroll_machine(
        client, bootstrap_key, machine_id="m-1", fingerprint=fingerprint
    )
    bind_by_code(client, admin_headers, original["pair_code"], entry["id"])

    # 关机 → 还原 → 开机
    age_agent(app, original["machine_uuid"])
    restored = enroll_machine(
        client, bootstrap_key, machine_id="m-1", fingerprint=fingerprint
    )

    assert restored["agent_id"] == original["agent_id"], "应当认回同一台机器"
    assert restored["claimed"] is True, "配对关系必须保留"
    assert restored["bound"] is True, "而且它下一轮就知道自己该扫哪"
    assert restored["player_no"] == "S001"


def test_restore_revokes_the_old_token(
    client: TestClient, admin_headers: dict, app, bootstrap_key: str, roster: dict,
    player: dict,
) -> None:
    """认回之后旧凭据必须立刻失效。

    否则被打快照的那台"母机"（或者它的副本）还能拿着旧 token 继续上传，
    而服务端会把它当成同一个学生 —— 这正是克隆镜像最隐蔽的后果。
    """
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    fingerprint = "smbios-" + "d" * 24
    original = enroll_machine(client, bootstrap_key, fingerprint=fingerprint)
    bind_by_code(client, admin_headers, original["pair_code"], entry["id"])

    age_agent(app, original["machine_uuid"])
    restored = enroll_machine(client, bootstrap_key, fingerprint=fingerprint)
    assert restored["token"] != original["token"]

    stale = client.post(
        "/api/v1/agent/tick",
        json=tick_payload(machine_id=original["machine_id"]),
        headers=agent_headers(original["token"]),
    )
    assert stale.status_code == 401, stale.text


def test_online_machine_cannot_be_taken_over_by_a_lookalike(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster: dict
) -> None:
    """**克隆镜像保护。**

    指纹只能证明硬件相同。母机**还开着**的时候又冒出一台报同样指纹的机器，
    这几乎不可能是"同一台机器回来了" —— 更可能是镜像被克隆了。
    让它拿走原机器的身份，那个学生的成绩就会被另一台机器的代码污染，
    而且完全静默（成绩矩阵只是看起来"他交了两遍"）。
    """
    entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    fingerprint = "smbios-" + "e" * 24
    original = enroll_machine(client, bootstrap_key, fingerprint=fingerprint)
    bind_by_code(client, admin_headers, original["pair_code"], entry["id"])

    # 母机没关机，克隆机就上线了
    clone = enroll_machine(
        client, bootstrap_key, fingerprint=fingerprint, hostname="克隆机"
    )

    assert clone["agent_id"] != original["agent_id"], "克隆机绝不能拿到母机的身份"
    assert clone["claimed"] is False, "该走人工配对"
    assert clone["pair_code"]

    events = global_events(client, admin_headers, category="enroll_conflict")
    assert events, "拒绝自动认回必须留下痕迹，否则事后无从解释"
    assert fingerprint[:16] in events[0]["message"]


def test_unknown_fingerprint_means_a_new_machine(
    client: TestClient, bootstrap_key: str
) -> None:
    first = enroll_machine(client, bootstrap_key, fingerprint="smbios-" + "f" * 24)
    second = enroll_machine(client, bootstrap_key, fingerprint="smbios-" + "1" * 24)
    assert second["agent_id"] != first["agent_id"]
    assert second["claimed"] is False


def test_ambiguous_fingerprint_does_not_guess(
    client: TestClient, admin_headers: dict, app, bootstrap_key: str
) -> None:
    """两台机器共用同一个指纹时**不许自动认回任何一台**。

    猜错的代价是身份串到别人身上，而现场完全看不出来。宁可让它进待配对列表，
    让教师花十秒确认一下。
    """
    fingerprint = "smbios-" + "2" * 24
    first = enroll_machine(client, bootstrap_key, fingerprint=fingerprint)
    second = enroll_machine(client, bootstrap_key, fingerprint=fingerprint)
    assert first["agent_id"] != second["agent_id"]

    # 两台都离线（模拟镜像是在两台机器都跑过之后才做的）
    age_agent(app, first["machine_uuid"])
    age_agent(app, second["machine_uuid"])

    third = enroll_machine(client, bootstrap_key, fingerprint=fingerprint)
    assert third["agent_id"] not in (first["agent_id"], second["agent_id"])

    events = global_events(client, admin_headers, category="enroll_ambiguous")
    assert events, "无法自动认回时要说清原因，而不是静默新建"


def test_empty_fingerprint_is_treated_as_unknown(
    client: TestClient, bootstrap_key: str
) -> None:
    """读不到指纹（虚拟机、SMBIOS 不可读）的机器**不能互相认回**。

    否则所有读不到指纹的机器会彼此"认回"，等于把一台机器的身份白送给另一台。
    """
    first = enroll_machine(client, bootstrap_key, fingerprint=None)
    second = enroll_machine(client, bootstrap_key, fingerprint=None)
    assert second["agent_id"] != first["agent_id"]

    # 太短的指纹同样不认（半截字符串可能来自读取失败，不是真的相同）
    short = enroll_machine(client, bootstrap_key, fingerprint="abc")
    assert short["agent_id"] not in (first["agent_id"], second["agent_id"])


# --------------------------------------------------------------------------- #
# 克隆告警
# --------------------------------------------------------------------------- #


def test_clone_alert_reports_shared_fingerprints(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    shared = "smbios-" + "3" * 24
    enroll_machine(client, bootstrap_key, hostname="pc-1", fingerprint=shared)
    enroll_machine(client, bootstrap_key, hostname="pc-2", fingerprint=shared)
    enroll_machine(client, bootstrap_key, hostname="pc-3", fingerprint="smbios-" + "4" * 24)

    body = client.get("/api/v1/admin/machines/clone-alerts", headers=admin_headers).json()
    assert body["total"] == 1
    assert body["items"][0]["machine_count"] == 2
    # 待配对的机器会带上这个后缀，让人一眼看出它们还没归属
    assert body["items"][0]["hostnames"] == ["pc-1（待配对）", "pc-2（待配对）"]
    # 一台都还没配对 —— 界面上就不该说"可能混进了别人的凭据"
    assert body["items"][0]["bound_count"] == 0


def test_clone_alert_says_how_many_are_already_bound(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """有人的身份挂在这个指纹上，才是真正要报警的那一刻。

    ``bound_count`` 就是"严重程度"的判据：0 时快照还原认回谁都无所谓（没有身份
    可冒领），≥1 时那条"按指纹认回原机器"的路会认错机器，而错的那一头是某个
    学生的成绩。
    """
    shared = "smbios-" + "7" * 24
    enroll_machine(client, bootstrap_key, hostname="pc-1", fingerprint=shared)
    second = enroll_machine(client, bootstrap_key, hostname="pc-2", fingerprint=shared)
    roster = make_roster(client, admin_headers, entries=[{"player_no": "S001", "name": "张三"}])
    bind_by_code(client, admin_headers, second["pair_code"], roster["entries"][0]["id"])

    body = client.get("/api/v1/admin/machines/clone-alerts", headers=admin_headers).json()
    assert body["items"][0]["machine_count"] == 2
    assert body["items"][0]["bound_count"] == 1
    # 已经配对的那台不再带"待配对"后缀，教师能一眼看出是哪一台挂在上面
    assert body["items"][0]["hostnames"] == ["pc-1（待配对）", "pc-2"]


def test_no_clone_alert_for_distinct_fingerprints(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    enroll_machine(client, bootstrap_key, fingerprint="smbios-" + "5" * 24)
    enroll_machine(client, bootstrap_key, fingerprint="smbios-" + "6" * 24)
    body = client.get("/api/v1/admin/machines/clone-alerts", headers=admin_headers).json()
    assert body["items"] == []


def test_pending_list_flags_fingerprint_peers(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """待配对列表里要能直接看出"这个指纹上还挂着别的机器"。"""
    shared = "smbios-" + "7" * 24
    enroll_machine(client, bootstrap_key, hostname="pc-1", fingerprint=shared)
    enroll_machine(client, bootstrap_key, hostname="pc-2", fingerprint=shared)

    rows = pending(client, admin_headers)
    assert [row["fingerprint_peers"] for row in rows] == [2, 2]
