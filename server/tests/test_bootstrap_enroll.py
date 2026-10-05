"""统一密钥注册 + 短码配对。

这是本轮最需要小心的一条链路：它把"服务端怎么知道这台机器是谁"从
"注册码里写着"换成了"教师在机器前确认一次"。测试要守住的是这条链路
**不能悄悄认错人** —— 认错的代价是一个学生的代码落进另一个人的目录，
而界面上看不出任何异常。

所以这里重点是三件事：

* 配对必须把机器认到**正确**的选手，且一人一机
* 配对码是一次性的、会过期的、要管理员才能用
* 快照还原之后能凭硬件指纹认回同一台机器，**不需要重新配对**
"""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi.testclient import TestClient

from conftest import agent_headers


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def issue_key(client: TestClient, headers: dict, **payload) -> Dict:
    response = client.post("/api/v1/admin/bootstrap-keys", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def enroll_with_key(
    client: TestClient,
    key: str,
    *,
    machine_id: str,
    machine_uuid: Optional[str] = None,
    machine_fingerprint: Optional[str] = None,
    hostname: str = "exam-pc",
) -> Dict:
    body = {
        "bootstrap_key": key,
        "machine_id": machine_id,
        "hostname": hostname,
        "agent_version": "0.1.0",
        "os_info": "NOI Linux 2.0",
        "machine_uuid": machine_uuid,
        "machine_fingerprint": machine_fingerprint,
    }
    return client.post("/api/v1/agent/enroll", json=body)


def pending(client: TestClient, headers: dict) -> List[Dict]:
    response = client.get("/api/v1/admin/machines/pending", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def add_player(client: TestClient, headers: dict, contest: dict, player_no: str) -> Dict:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": player_no}],
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()[0]


def global_events(client: TestClient, headers: dict, category: Optional[str] = None) -> List[Dict]:
    """不分场次的审计事件。

    注册与配对冲突发生在"还没有场次归属"的机器上，所以那些事件查不到任何
    场次里 —— 它们只能从这个接口看到。这也正是它必须存在的原因。
    """
    query = "?category=%s" % category if category else ""
    response = client.get("/api/v1/admin/events" + query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def age_agent(app, machine_id: str, seconds: int = 3600) -> None:
    """把某台机器的"最后心跳"推到过去，模拟它关机了一段时间。

    快照还原的真实时序是：机器关机 → 打快照 / 还原 → 再开机。
    期间它一定超过了离线判定阈值，所以这个 helper 不是"绕过保护"，
    而是把测试放到真实的时序里。
    """
    from datetime import timedelta

    from sqlalchemy import select

    from syncoj_server.models import Agent, utcnow

    with app.state.ctx.db.session() as session:
        agent = session.execute(
            select(Agent).where(Agent.machine_id == machine_id)
        ).scalar_one()
        agent.last_seen_at = utcnow() - timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# 密钥管理
# --------------------------------------------------------------------------- #


def test_issue_and_list_keys(client: TestClient, admin_headers: dict) -> None:
    issued = issue_key(client, admin_headers, label="2025 机房镜像")
    assert issued["key"]
    assert issued["use_count"] == 0
    assert issued["revoked_at"] is None

    rows = client.get("/api/v1/admin/bootstrap-keys", headers=admin_headers).json()
    assert [r["label"] for r in rows] == ["2025 机房镜像"]
    # 列表接口**不能**回显明文
    assert "key" not in rows[0]


def test_keys_require_admin(client: TestClient) -> None:
    assert client.get("/api/v1/admin/bootstrap-keys").status_code == 401


def test_revoked_key_is_rejected(client: TestClient, admin_headers: dict) -> None:
    issued = issue_key(client, admin_headers)
    client.post(
        "/api/v1/admin/bootstrap-keys/%d/revoke" % issued["id"], headers=admin_headers
    )

    response = enroll_with_key(client, issued["key"], machine_id="m-1")
    assert response.status_code == 403
    assert "吊销" in response.json()["detail"]


def test_unknown_key_is_rejected(client: TestClient, admin_headers: dict) -> None:
    response = enroll_with_key(client, "AAAA-BBBB-CCCC-DDDD", machine_id="m-1")
    assert response.status_code == 404


def test_enroll_needs_some_credential(client: TestClient, admin_headers: dict) -> None:
    response = client.post("/api/v1/agent/enroll", json={"machine_id": "m-1"})
    assert response.status_code == 400


def test_revoking_a_key_does_not_affect_registered_machines(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """已经注册好的机器手里是各自的 token，不是这把密钥。"""
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()

    player = add_player(client, admin_headers, contest, "S001")
    client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": enrolled["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    client.post(
        "/api/v1/admin/bootstrap-keys/%d/revoke" % issued["id"], headers=admin_headers
    )

    tick = client.post(
        "/api/v1/agent/tick",
        json=_tick_payload(),
        headers=agent_headers(enrolled["token"]),
    )
    assert tick.status_code == 200, tick.text


# --------------------------------------------------------------------------- #
# 注册出来是"没有归属"的
# --------------------------------------------------------------------------- #


def test_bootstrap_enroll_is_unclaimed(client: TestClient, admin_headers: dict) -> None:
    issued = issue_key(client, admin_headers)
    response = enroll_with_key(client, issued["key"], machine_id="m-1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"]
    assert body["claimed"] is False
    assert body["pair_code"]
    assert body["player_no"] == ""
    assert body["contest_id"] is None


def test_unclaimed_machine_shows_up_in_pending(
    client: TestClient, admin_headers: dict
) -> None:
    issued = issue_key(client, admin_headers)
    enroll_with_key(client, issued["key"], machine_id="m-1", hostname="exam-pc-07")

    rows = pending(client, admin_headers)
    assert len(rows) == 1
    assert rows[0]["machine_id"] == "m-1"
    assert rows[0]["hostname"] == "exam-pc-07"
    # 配对码的**明文不能**回给界面：库里只有哈希，服务端自己也不该留
    assert "pair_code" not in rows[0]


def test_unclaimed_tick_says_not_claimed(
    client: TestClient, admin_headers: dict
) -> None:
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()

    response = client.post(
        "/api/v1/agent/tick",
        json=_tick_payload(),
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["claimed"] is False


def test_unclaimed_machine_cannot_upload(
    client: TestClient, admin_headers: dict
) -> None:
    """没归属的机器连准考证号都不知道，上传只能是污染台账。

    返回 403 而不是 401 也很重要：401 会让 Agent 以为凭据坏了，于是反复重新
    注册，把真正的"还没配对"盖掉。
    """
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()

    response = client.post(
        "/api/v1/agent/files",
        data={"path": "p1/p1.cpp", "sha256": "0" * 64},
        files={"file": ("p1.cpp", b"int main(){}", "application/octet-stream")},
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 403
    assert "配对" in response.json()["detail"]


def test_unclaimed_scan_is_discarded(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """未认领机器上报的扫描结果一律丢弃 —— 没有准考证号，路径没法归属。"""
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()

    payload = _tick_payload(scan=[{"path": "p1/p1.cpp", "sha256": "a" * 64, "size": 3, "mtime": 1}])
    client.post(
        "/api/v1/agent/tick", json=payload, headers=agent_headers(enrolled["token"])
    )

    files = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=admin_headers
    ).json()
    assert files == []


# --------------------------------------------------------------------------- #
# 配对
# --------------------------------------------------------------------------- #


def test_claim_by_code_binds_the_right_player(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()
    player = add_player(client, admin_headers, contest, "S001")

    response = client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": enrolled["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "S001" in response.json()["detail"]

    assert pending(client, admin_headers) == []

    tick = client.post(
        "/api/v1/agent/tick",
        json=_tick_payload(),
        headers=agent_headers(enrolled["token"]),
    ).json()
    assert tick["claimed"] is True
    assert tick["player_no"] == "S001"


def test_claim_code_is_case_insensitive(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """教师是照着机器抄的，不该因为大小写或连字符被为难。"""
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()
    player = add_player(client, admin_headers, contest, "S001")

    messy = enrolled["pair_code"].lower()
    response = client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": messy, "player_id": player["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


def test_claim_by_id_without_code(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """从列表里按主机名直接认领（教师能看到主机名时最省事）。"""
    issued = issue_key(client, admin_headers)
    enroll_with_key(client, issued["key"], machine_id="m-1", hostname="exam-pc-07")
    player = add_player(client, admin_headers, contest, "S001")
    claim_id = pending(client, admin_headers)[0]["id"]

    response = client.post(
        "/api/v1/admin/machines/%d/claim" % claim_id,
        json={"player_id": player["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


def test_claim_by_id_with_wrong_code_is_rejected(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """给了配对码就必须对得上 —— 主机名可能是重复的，码才是"我站在机器前"的证据。"""
    issued = issue_key(client, admin_headers)
    enroll_with_key(client, issued["key"], machine_id="m-1")
    player = add_player(client, admin_headers, contest, "S001")
    claim_id = pending(client, admin_headers)[0]["id"]

    response = client.post(
        "/api/v1/admin/machines/%d/claim" % claim_id,
        json={"player_id": player["id"], "pair_code": "WRONG1"},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert pending(client, admin_headers), "配对失败的机器不该从列表里消失"


def test_expired_pair_code_is_rejected(
    client: TestClient, admin_headers: dict, contest: dict, app
) -> None:
    from datetime import timedelta

    from syncoj_server.models import MachineClaim, utcnow

    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="m-1").json()
    player = add_player(client, admin_headers, contest, "S001")

    with app.state.ctx.db.session() as session:
        claim = session.query(MachineClaim).one()
        claim.pair_code_expires_at = utcnow() - timedelta(seconds=1)

    response = client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": enrolled["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 404
    assert "过期" in response.json()["detail"]


def test_one_player_cannot_have_two_machines(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """两台机器认领到同一个选手：代码会往同一个目录里写，而且是静默的。

    成绩矩阵只会看起来"这个人交了两遍"，谁也不知道哪一份是真的 ——
    所以必须在入口拒绝。
    """
    key = issue_key(client, admin_headers)["key"]
    first = enroll_with_key(client, key, machine_id="m-1").json()
    second = enroll_with_key(client, key, machine_id="m-2").json()
    player = add_player(client, admin_headers, contest, "S001")

    ok = client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": first["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    assert ok.status_code == 200

    clash = client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": second["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    assert clash.status_code == 409
    assert "已经绑定" in clash.json()["detail"]


def test_claiming_requires_admin(client: TestClient, contest: dict) -> None:
    response = client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": "ABCDEF", "player_id": 1},
    )
    assert response.status_code == 401


def test_revoke_pending_machine(client: TestClient, admin_headers: dict) -> None:
    issued = issue_key(client, admin_headers)
    enroll_with_key(client, issued["key"], machine_id="junk")
    claim_id = pending(client, admin_headers)[0]["id"]

    assert client.delete(
        "/api/v1/admin/machines/pending/%d" % claim_id, headers=admin_headers
    ).status_code == 200
    assert pending(client, admin_headers) == []


def test_revoked_machine_credential_stops_working(
    client: TestClient, admin_headers: dict
) -> None:
    issued = issue_key(client, admin_headers)
    enrolled = enroll_with_key(client, issued["key"], machine_id="junk").json()
    claim_id = pending(client, admin_headers)[0]["id"]
    client.delete("/api/v1/admin/machines/pending/%d" % claim_id, headers=admin_headers)

    response = client.post(
        "/api/v1/agent/tick",
        json=_tick_payload(),
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# 快照还原：凭硬件指纹认回原机器
# --------------------------------------------------------------------------- #


def test_snapshot_restore_keeps_the_pairing(
    client: TestClient, admin_headers: dict, contest: dict, app
) -> None:
    """**这条是统一密钥方案能成立的关键。**

    机器上的 credential.json 会随快照还原消失，连自己生成的 UUID 也一起没了。
    如果服务端不能靠硬件指纹认出"这是原来那台机器"，每还原一次就要教师重新
    配对一次 —— 50 台机器就是 50 次人工，方案直接不可用。
    """
    issued = issue_key(client, admin_headers)
    fingerprint = "4c4c4544-0031-3010-8043-b7c04f4d4432"

    first = enroll_with_key(
        client,
        issued["key"],
        machine_id="m-1",
        machine_uuid="uuid-a",
        machine_fingerprint=fingerprint,
    ).json()
    player = add_player(client, admin_headers, contest, "S001")
    client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": first["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )

    # 关机 → 还原 → 再开机：凭据和 UUID 都没了，指纹还在
    age_agent(app, "m-1")
    restored = enroll_with_key(
        client,
        issued["key"],
        machine_id="m-1",
        machine_uuid="uuid-b",
        machine_fingerprint=fingerprint,
    ).json()

    assert restored["claimed"] is True
    assert restored["player_no"] == "S001"
    assert restored["token"] != first["token"], "凭据应当换发"
    assert pending(client, admin_headers) == [], "不该多出一台待配对机器"


def test_restore_revokes_the_old_token(
    client: TestClient, admin_headers: dict, contest: dict, app
) -> None:
    """换发凭据之后旧 token 必须立刻失效 —— 否则快照里那份还能用。"""
    issued = issue_key(client, admin_headers)
    fingerprint = "fingerprint-abcdefgh"

    first = enroll_with_key(
        client, issued["key"], machine_id="m-1", machine_fingerprint=fingerprint
    ).json()
    player = add_player(client, admin_headers, contest, "S001")
    client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": first["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    age_agent(app, "m-1")
    enroll_with_key(
        client, issued["key"], machine_id="m-1", machine_fingerprint=fingerprint
    )

    stale = client.post(
        "/api/v1/agent/tick",
        json=_tick_payload(),
        headers=agent_headers(first["token"]),
    )
    assert stale.status_code == 401


def test_online_machine_cannot_be_taken_over_by_a_lookalike(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """**指纹认回不能无条件自动执行。**

    指纹只证明硬件相同。原机器还在心跳时，另一台报同样指纹的机器几乎只可能是
    克隆出来的 —— 它要是把身份拿走了，那个学生的成绩就会被别的机器的代码污染，
    而且完全静默。所以在线时一律不认回，改走人工配对。
    """
    issued = issue_key(client, admin_headers)
    fingerprint = "fingerprint-shared-1"

    first = enroll_with_key(
        client, issued["key"], machine_id="m-1", machine_fingerprint=fingerprint
    ).json()
    player = add_player(client, admin_headers, contest, "S001")
    client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": first["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )

    # m-1 刚刚才配对过，是"在线"的
    impostor = enroll_with_key(
        client, issued["key"], machine_id="m-2", machine_fingerprint=fingerprint
    ).json()

    assert impostor["claimed"] is False, "在线机器的身份被另一台机器拿走了"
    assert impostor["player_no"] == ""
    assert len(pending(client, admin_headers)) == 1

    events = global_events(client, admin_headers, category="enroll_conflict")
    assert events, "应当留下审计事件"
    assert "在线" in events[0]["message"]


def test_unknown_fingerprint_means_a_new_machine(
    client: TestClient, admin_headers: dict
) -> None:
    issued = issue_key(client, admin_headers)
    enroll_with_key(
        client, issued["key"], machine_id="m-1", machine_fingerprint="fingerprint-11111111"
    )
    other = enroll_with_key(
        client, issued["key"], machine_id="m-2", machine_fingerprint="fingerprint-22222222"
    ).json()

    assert other["claimed"] is False
    assert len(pending(client, admin_headers)) == 2


def test_ambiguous_fingerprint_does_not_guess(
    client: TestClient, admin_headers: dict, contest: dict, app
) -> None:
    """两台机器报同一个指纹时**不能猜**。

    猜错就是把成绩算到别人头上，而且看不出异常。宁可让教师人工确认一次。
    """
    issued = issue_key(client, admin_headers)
    shared = "shared-fingerprint-xxxx"
    key = issued["key"]

    first = enroll_with_key(
        client, key, machine_id="m-1", machine_fingerprint=shared
    ).json()
    player = add_player(client, admin_headers, contest, "S001")
    client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": first["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )

    # 母机还在线时又来了两台报同样指纹的机器 —— 明显是克隆
    second = enroll_with_key(
        client, key, machine_id="m-2", machine_fingerprint=shared
    ).json()
    assert second["claimed"] is False, "指纹撞了就该进待配对，而不是擅自认回"

    third = enroll_with_key(
        client, key, machine_id="m-3", machine_fingerprint=shared
    ).json()
    assert third["claimed"] is False

    events = global_events(client, admin_headers, category="enroll_conflict")
    assert len(events) == 2, "两次冲突都该留下记录"
    assert "指纹" in events[0]["message"]


def test_empty_fingerprint_is_treated_as_unknown(
    client: TestClient, admin_headers: dict
) -> None:
    """没有指纹（虚拟机、SMBIOS 不可读）时不该拿空值去互相匹配。

    不然所有读不到指纹的机器会彼此"认回"，那就是把一台机器的身份白送给另一台。
    """
    issued = issue_key(client, admin_headers)
    first = enroll_with_key(client, issued["key"], machine_id="m-1").json()
    second = enroll_with_key(client, issued["key"], machine_id="m-2").json()

    assert first["claimed"] is False
    assert second["claimed"] is False
    assert len(pending(client, admin_headers)) == 2


# --------------------------------------------------------------------------- #
# 克隆镜像告警
# --------------------------------------------------------------------------- #


def test_clone_alert_reports_shared_fingerprints(
    client: TestClient, admin_headers: dict, contest: dict, app
) -> None:
    issued = issue_key(client, admin_headers)
    shared = "cloned-image-fingerprint"
    key = issued["key"]

    first = enroll_with_key(
        client, key, machine_id="m-1", hostname="pc-01", machine_fingerprint=shared
    ).json()
    player = add_player(client, admin_headers, contest, "S001")
    client.post(
        "/api/v1/admin/machines/claim-by-code",
        json={"pair_code": first["pair_code"], "player_id": player["id"]},
        headers=admin_headers,
    )
    # 母机（pc-01）还在心跳，pc-02 却报出同样的指纹 —— 克隆镜像的典型形态
    enroll_with_key(
        client, key, machine_id="m-2", hostname="pc-02", machine_fingerprint=shared
    )

    alerts = client.get("/api/v1/admin/machines/clone-alerts", headers=admin_headers).json()
    assert len(alerts) == 1
    assert alerts[0]["machine_count"] == 2
    assert "pc-01" in alerts[0]["hostnames"]
    assert "pc-02（待配对）" in alerts[0]["hostnames"]


def test_no_clone_alert_for_distinct_fingerprints(
    client: TestClient, admin_headers: dict
) -> None:
    issued = issue_key(client, admin_headers)
    enroll_with_key(
        client, issued["key"], machine_id="m-1", machine_fingerprint="fingerprint-aaaaaaaa"
    )
    enroll_with_key(
        client, issued["key"], machine_id="m-2", machine_fingerprint="fingerprint-bbbbbbbb"
    )

    assert client.get("/api/v1/admin/machines/clone-alerts", headers=admin_headers).json() == []


def test_pending_list_flags_fingerprint_peers(
    client: TestClient, admin_headers: dict
) -> None:
    issued = issue_key(client, admin_headers)
    shared = "peer-fingerprint-cccc"
    enroll_with_key(client, issued["key"], machine_id="m-1", machine_fingerprint=shared)
    enroll_with_key(client, issued["key"], machine_id="m-2", machine_fingerprint=shared)

    rows = pending(client, admin_headers)
    assert all(row["fingerprint_peers"] == 2 for row in rows)


# --------------------------------------------------------------------------- #
# 限速
# --------------------------------------------------------------------------- #


def _tick_payload(scan=None, hostname=None) -> Dict:
    return {
        "agent_version": "0.1.0",
        "machine_id": "m-1",
        "hostname": hostname,
        "ts": 1767225600,
        "scan_root": "/home/student/code",
        "scan": scan or [],
        "partials": [],
        "stats": {"disk_free": 10 ** 10, "last_error": None, "queue": 0},
    }
