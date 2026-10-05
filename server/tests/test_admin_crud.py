"""补齐的增删改查入口。

这一批端点大多是"缺了很久但一直没人提起"的那类：平时用不到，
真要用的那一刻（导错一个选手、发错一个文件、比赛结束要清场）才会发现没有入口。
所以这里逐条盯三件事：

* 危险操作**该拦的拦住**（删场次要打标识、清成绩默认保留手工分）
* 说清楚**边界**（删记录不等于删机器上的文件、改名只影响没落地的）
* 返回的说明是给人看的 —— 它会直接显示在界面上，是教师唯一能拿到的反馈

第二轮（统一密钥 + 永久配对）之后，"逐台发注册码"那一整节被"机器配对"取代：
注册码到不了机器手里（一份镜像装遍整间机房）、绑定又只该发生一次，
所以这里改盯配对的三条硬规则与"改派/解绑/作废"的正确性。
"""

from __future__ import annotations

from typing import Dict, List

from fastapi.testclient import TestClient

from syncoj_server.schemas import GLOBAL_CONFIRM

from conftest import (
    agent_headers,
    bind_by_code,
    enroll_machine,
    issue_bootstrap_key,
    make_roster,
    do_tick as tick,
    scan_entry as entry,
    sha256_of,
)


def upload_code(client: TestClient, enrolled: dict, path: str, data: bytes) -> None:
    tick(client, enrolled["token"], [entry(path, data)], machine_id=enrolled["machine_id"])
    response = client.post(
        "/api/v1/agent/files",
        data={"path": path, "sha256": sha256_of(data)},
        files={"file": ("code.bin", data, "application/octet-stream")},
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 200, response.text


def files_of(client: TestClient, headers: dict, contest: dict) -> List[Dict]:
    body = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=headers
    ).json()
    return body["items"]


def players_of(client: TestClient, headers: dict, contest: dict) -> List[Dict]:
    body = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=headers
    ).json()
    return body["items"]


def contests_of(client: TestClient, headers: dict) -> List[Dict]:
    return client.get("/api/v1/admin/contests", headers=headers).json()["items"]


def upload_asset(client: TestClient, headers: dict, contest: dict, name: str, data: bytes) -> Dict:
    response = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": (name, data, "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 场次
# --------------------------------------------------------------------------- #


def test_delete_contest_requires_the_slug(client: TestClient, admin_headers: dict,
                                         contest: dict) -> None:
    """删场次会连带删掉选手、代码、成绩、下发 —— 破坏力最大的一个操作。

    所以要求把标识原样再打一遍：界面上"你确定吗"点一下就过去的东西，
    代价却是不可逆的。

    漏传 confirm 是 422（参数必填），传错是 400 + ``name_mismatch`` ——
    分开是有意的：前者是"你没调对接口"，后者是"你打错了名字"。
    """
    missing = client.delete(
        "/api/v1/admin/contests/%d" % contest["id"], headers=admin_headers
    )
    assert missing.status_code == 422, missing.text

    wrong = client.delete(
        "/api/v1/admin/contests/%d?confirm=打错的" % contest["id"], headers=admin_headers
    )
    assert wrong.status_code == 400, wrong.text
    body = wrong.json()
    assert body["code"] == "name_mismatch"
    assert body["details"]["expected"] == contest["slug"]
    assert contest["slug"] in body["detail"]

    assert len(contests_of(client, admin_headers)) == 1, "确认失败时不该真的删掉"


def test_delete_contest_with_confirmation(client: TestClient, admin_headers: dict,
                                          contest: dict, player: dict) -> None:
    response = client.delete(
        "/api/v1/admin/contests/%d?confirm=%s" % (contest["id"], contest["slug"]),
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    detail = response.json()["detail"]
    assert "选手 1" in detail, detail
    assert contests_of(client, admin_headers) == []


def test_delete_contest_clears_online_registry(client: TestClient, admin_headers: dict,
                                               contest: dict, enrolled: dict) -> None:
    """内存里的在线状态要跟着清，否则顶栏会继续显示一台已经不存在的场次的机器。"""
    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    listed = client.get(
        "/api/v1/admin/contests/%d/agents" % contest["id"], headers=admin_headers
    ).json()
    assert listed["items"]

    client.delete(
        "/api/v1/admin/contests/%d?confirm=%s" % (contest["id"], contest["slug"]),
        headers=admin_headers,
    )
    health = client.get("/healthz").json()
    assert health["agents_total"] == 0


# --------------------------------------------------------------------------- #
# 选手
# --------------------------------------------------------------------------- #


def test_update_player(client: TestClient, admin_headers: dict, contest: dict,
                       player: dict) -> None:
    response = client.patch(
        "/api/v1/admin/players/%d" % player["id"],
        json={"player_no": "S001", "name": "改过的名字", "seat": "B2", "group_name": "B组"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "改过的名字"
    assert body["seat"] == "B2"


def test_update_player_rejects_duplicate_no(client: TestClient, admin_headers: dict,
                                            contest: dict, player: dict) -> None:
    other = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S002"}],
        headers=admin_headers,
    ).json()["players"][0]

    response = client.patch(
        "/api/v1/admin/players/%d" % other["id"],
        json={"player_no": "S001"},
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_delete_player_removes_their_code(client: TestClient, admin_headers: dict,
                                          contest: dict, player: dict,
                                          enrolled: dict) -> None:
    upload_code(client, enrolled, "p1/p1.cpp", b"int main(){}")
    assert len(files_of(client, admin_headers, contest)) == 1

    missing = client.delete(
        "/api/v1/admin/players/%d" % player["id"], headers=admin_headers
    )
    assert missing.status_code == 422, "删选手必须要求把考号打一遍"

    response = client.delete(
        "/api/v1/admin/players/%d?confirm=S001" % player["id"], headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert "代码 1 条" in response.json()["detail"]
    assert files_of(client, admin_headers, contest) == []


def test_delete_player_keeps_their_machine(client: TestClient, admin_headers: dict,
                                          contest: dict, player: dict,
                                          enrolled: dict) -> None:
    """删参赛记录**不作废机器** —— 机器绑的是人，明天那场还要用。

    这条在一次误删里是救命的：教师清场时删掉整场选手，如果连机器一起作废，
    第二天的比赛就得重新配对 50 台。
    """
    response = client.delete(
        "/api/v1/admin/players/%d?confirm=S001" % player["id"], headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert "机器没有动" in response.json()["detail"]

    rows = client.get("/api/v1/admin/machines/pending", headers=admin_headers).json()["items"]
    assert rows == [], "机器不该被扔回待配对列表"

    # 机器还在：把名单重新应用到场次，它就又能干活了
    roster = make_roster(client, admin_headers, name="回填班", entries=[{"player_no": "S001"}])
    back = client.post(
        "/api/v1/admin/contests/%d/players/apply-roster" % contest["id"],
        json={"roster_id": roster["id"]},
        headers=admin_headers,
    )
    assert back.status_code == 200, back.text
    tick_body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert tick_body["claimed"] is True and tick_body["bound"] is True


def test_clear_players_keeps_those_with_submissions(client: TestClient, admin_headers: dict,
                                                    contest: dict, enrolled: dict) -> None:
    """清空名单默认**保留**有提交的选手 —— 顺手把参赛者的提交删掉是不可逆的事故。"""
    upload_code(client, enrolled, "p1/p1.cpp", b"int main(){}")
    client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S999"}],
        headers=admin_headers,
    )

    response = client.post(
        "/api/v1/admin/contests/%d/players/clear" % contest["id"],
        json={"confirm": contest["slug"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    detail = response.json()["detail"]
    assert "保留 1 名有提交" in detail, detail

    left = players_of(client, admin_headers, contest)
    assert [p["player_no"] for p in left] == ["S001"]


def test_clear_players_can_remove_everything(client: TestClient, admin_headers: dict,
                                             contest: dict, player: dict) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/players/clear" % contest["id"],
        json={"confirm": contest["slug"], "keep_with_submissions": False},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert players_of(client, admin_headers, contest) == []


def test_clear_players_needs_the_contest_slug(client: TestClient, admin_headers: dict,
                                              contest: dict, player: dict) -> None:
    """清空是范围删除，没有单个名字可打 —— 确认内容是**范围**的名字（场次 slug）。"""
    response = client.post(
        "/api/v1/admin/contests/%d/players/clear" % contest["id"],
        json={"confirm": "随便写的"},
        headers=admin_headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "name_mismatch"
    assert players_of(client, admin_headers, contest), "确认失败时一个都不该删"


# --------------------------------------------------------------------------- #
# 机器配对
#
# 配对是**永久**的：绑的是名单里的**人**，不是某场比赛的选手。
# 下面逐条盯住那条规则和它的三个"不许"。
# --------------------------------------------------------------------------- #


def test_pairing_by_code_binds_the_machine(client: TestClient, admin_headers: dict,
                                           roster_entry: dict, machine: dict) -> None:
    result = bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])
    assert result["roster_entry_id"] == roster_entry["id"]
    assert result["player_no"] == "S001"
    assert result["roster_name"] == "高一(1)班"

    # 配对之后它就不再出现在待配对列表里
    pending = client.get("/api/v1/admin/machines/pending", headers=admin_headers).json()
    assert pending["items"] == []
    assert pending["total"] == 0


def test_wrong_pair_code_is_rejected_with_a_branchable_code(
    client: TestClient, admin_headers: dict, roster_entry: dict, machine: dict
) -> None:
    """前端要区分"码打错了"和"这个人已经有机器了" —— 两者的补救动作不同。"""
    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": "000000", "roster_entry_id": roster_entry["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "pair_code_invalid"
    assert isinstance(body["detail"], str) and body["detail"]


def test_pair_code_is_single_use(client: TestClient, admin_headers: dict,
                                 roster_entry: dict, machine: dict) -> None:
    """配对码限时 + 一次性：教师读一遍就该作废，不能被第二个人拿去复用。"""
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    again = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": machine["pair_code"], "roster_entry_id": roster_entry["id"]},
        headers=admin_headers,
    )
    assert again.status_code == 404, again.text
    assert again.json()["code"] == "pair_code_invalid"


def test_one_person_can_only_have_one_machine(client: TestClient, admin_headers: dict,
                                              bootstrap_key: str, roster_entry: dict,
                                              machine: dict) -> None:
    """两台机器绑同一个人，代码会往同一个目录里写，而且完全静默。"""
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])
    second = enroll_machine(client, bootstrap_key, hostname="exam-pc-02")

    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": second["pair_code"], "roster_entry_id": roster_entry["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "roster_entry_taken"


def test_one_machine_cannot_silently_change_owner(client: TestClient, admin_headers: dict,
                                                  roster_entry: dict, machine: dict) -> None:
    """一台机器只能属于一个人。

    覆盖已有绑定必须走显式的「改派」—— 让"再输一次配对码"悄悄换掉一个人的身份，
    可能是教师在另一台机器上输错了数字，而后果是成绩归属悄悄换人。
    """
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    other = make_roster(client, admin_headers, name="隔壁班", entries=[{"player_no": "S002"}])
    other_entry = next(e for e in other["entries"] if e["player_no"] == "S002")

    # 老师又输了一遍同一个码（这正是"在另一台机器上输错数字"的现实版本）
    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": machine["pair_code"], "roster_entry_id": other_entry["id"]},
        headers=admin_headers,
    )
    # 码已经作废了，所以先在"码"这一关被拦住
    assert response.status_code == 404, response.text

    # 就算拿机器 id 直接绑（界面上的另一条路），也必须拒绝并说明原因
    forced = client.post(
        "/api/v1/admin/machines/%d/bind" % machine["agent_id"],
        json={"roster_entry_id": other_entry["id"]},
        headers=admin_headers,
    )
    assert forced.status_code == 409, forced.text
    assert forced.json()["code"] == "machine_already_bound"


def test_rebind_moves_a_machine_to_another_person(client: TestClient, admin_headers: dict,
                                                  machine: dict, roster_entry: dict,
                                                  bootstrap_key: str) -> None:
    """改派是"换座位"最自然的做法：凭据不动、不用重启、下一轮心跳就换人。

    考场上"换个人"要等到重启，这是不能接受的。
    """
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    other = make_roster(client, admin_headers, name="隔壁班", entries=[{"player_no": "S002"}])
    other_entry = next(e for e in other["entries"] if e["player_no"] == "S002")

    response = client.post(
        "/api/v1/admin/agents/%d/rebind" % machine["agent_id"],
        json={"roster_entry_id": other_entry["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "S002" in response.json()["detail"]

    # 凭据没变：同一把 token 还能干活
    tick_body = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert tick_body["claimed"] is True


def test_rebind_refuses_a_person_who_already_has_a_machine(
    client: TestClient, admin_headers: dict, bootstrap_key: str, roster_entry: dict,
    machine: dict
) -> None:
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])
    second = enroll_machine(client, bootstrap_key, hostname="exam-pc-02")
    other = make_roster(client, admin_headers, name="隔壁班", entries=[{"player_no": "S002"}])
    other_entry = next(e for e in other["entries"] if e["player_no"] == "S002")
    bind_by_code(client, admin_headers, second["pair_code"], other_entry["id"])

    response = client.post(
        "/api/v1/admin/agents/%d/rebind" % machine["agent_id"],
        json={"roster_entry_id": other_entry["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "roster_entry_taken"


def test_unbind_returns_the_machine_to_the_pairing_queue(
    client: TestClient, admin_headers: dict, roster_entry: dict, machine: dict
) -> None:
    """解绑后它又变成"待配对"，并且**下一轮心跳就能拿到一个新配对码**。

    这一步是必须的：机器上没有 root 只读的统一密钥，读不到就只能靠心跳 ——
    只在注册那一刻发码的话，解绑之后那台机器就永远拿不到新码了。
    """
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    response = client.delete(
        "/api/v1/admin/agents/%d/bind" % machine["agent_id"], headers=admin_headers
    )
    assert response.status_code == 200, response.text

    pending = client.get("/api/v1/admin/machines/pending", headers=admin_headers).json()
    assert [row["id"] for row in pending["items"]] == [machine["agent_id"]]

    tick_body = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert tick_body["claimed"] is False
    new_code = tick_body["pair_code"]
    assert new_code and new_code != machine["pair_code"], "解绑后必须换一个新码"

    # 新码可以直接用来配对；旧码（已经作废）不行
    stale = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": machine["pair_code"], "roster_entry_id": roster_entry["id"]},
        headers=admin_headers,
    )
    assert stale.status_code == 404, stale.text

    result = bind_by_code(client, admin_headers, new_code, roster_entry["id"])
    assert result["player_no"] == "S001"


def test_repeated_ticks_do_not_churn_the_pair_code(
    client: TestClient, admin_headers: dict, machine: dict, roster_entry: dict
) -> None:
    """未配对机器的每一轮心跳都要带上**当前有效的那个码**，而不是换一个新的。

    协议规定 `claimed=false` 的响应里必须带 `pair_code`（心跳是未配对机器唯一
    稳定的上行通道），但**同一个码在有效期内必须原样回同一串**：
    每轮换新会让教师刚在屏幕上读到的数字当场作废，而现场看起来只是
    "配对码一直在跳"，没人会往心跳上想。
    """
    first = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert first["claimed"] is False
    assert first["pair_code"] == machine["pair_code"], "有效期内必须回同一个码"

    second = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert second["pair_code"] == machine["pair_code"], "再来一轮还是同一个码"

    # 而且它必须真的能用
    result = bind_by_code(client, admin_headers, second["pair_code"], roster_entry["id"])
    assert result["roster_entry_id"] == roster_entry["id"]


def test_bound_machine_no_longer_carries_a_pair_code(
    client: TestClient, admin_headers: dict, machine: dict, roster_entry: dict
) -> None:
    """配对之后不许再带配对码 —— 带了教师会以为配对没生效，跑去重配一遍。"""
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    body = tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert body["claimed"] is True
    assert body["pair_code"] is None


def test_revoke_machine_requires_the_hostname(client: TestClient, admin_headers: dict,
                                              roster_entry: dict, machine: dict) -> None:
    """作废是硬删（凭据立刻失效、在线历史一起没了），所以要打机器名。"""
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    missing = client.delete(
        "/api/v1/admin/agents/%d" % machine["agent_id"], headers=admin_headers
    )
    assert missing.status_code == 422, missing.text

    response = client.delete(
        "/api/v1/admin/agents/%d?confirm=exam-pc-01" % machine["agent_id"],
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    # 凭据失效：401 而不是 403 —— Agent 会清掉本地凭据重新注册，正是我们要的
    ticked = client.post(
        "/api/v1/agent/tick",
        json={"agent_version": "0.1.0", "machine_id": machine["machine_id"], "ts": 1,
              "scan_root": "/x", "scan": [], "partials": [],
              "stats": {"disk_free": 1, "last_error": None, "queue": 0}},
        headers=agent_headers(machine["token"]),
    )
    assert ticked.status_code == 401, ticked.text


def test_clear_pending_machines_needs_global_confirmation(
    client: TestClient, admin_headers: dict, bootstrap_key: str
) -> None:
    """清空待配对列表是**范围**删除 —— 确认内容是固定字面量 all。"""
    for i in range(3):
        enroll_machine(client, bootstrap_key, hostname="exam-pc-%02d" % i)

    refused = client.post(
        "/api/v1/admin/machines/pending/clear",
        json={"confirm": "exam-pc-01"},
        headers=admin_headers,
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "name_mismatch"
    assert client.get(
        "/api/v1/admin/machines/pending", headers=admin_headers
    ).json()["total"] == 3, "确认失败时一台都不该清"

    cleared = client.post(
        "/api/v1/admin/machines/pending/clear",
        json={"confirm": GLOBAL_CONFIRM},
        headers=admin_headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert client.get(
        "/api/v1/admin/machines/pending", headers=admin_headers
    ).json()["total"] == 0


def test_pending_list_is_paginated_and_counted(client: TestClient, admin_headers: dict,
                                               bootstrap_key: str) -> None:
    """``total`` 必须是**真实总数**，不是本页条数 —— 分页器和确认文案都靠它。"""
    for i in range(3):
        enroll_machine(client, bootstrap_key, hostname="exam-pc-%02d" % i)

    page = client.get(
        "/api/v1/admin/machines/pending?limit=2&offset=0", headers=admin_headers
    ).json()
    assert page["total"] == 3, "拿本页条数充数的话，第二页会显示「共 2 条」"
    assert len(page["items"]) == 2
    assert (page["limit"], page["offset"]) == (2, 0)

    second = client.get(
        "/api/v1/admin/machines/pending?limit=2&offset=2", headers=admin_headers
    ).json()
    assert len(second["items"]) == 1
    assert {row["id"] for row in page["items"]} != {row["id"] for row in second["items"]}


def test_bootstrap_keys_are_never_returned_in_list(
    client: TestClient, admin_headers: dict
) -> None:
    """明文只该在签发那一刻出现 —— 列表里只有哈希的元信息。"""
    raw = issue_bootstrap_key(client, admin_headers, label="机房 A")

    body = client.get("/api/v1/admin/bootstrap-keys", headers=admin_headers).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["label"] == "机房 A"
    assert raw not in str(body), "明文只该返回一次"
    assert "key" not in row


def test_contest_note_survives_a_round_trip(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """``PATCH`` 收下备注、界面却读不回来 —— 那是"保存成功了但看起来没保存"。

    这类问题的表现最难查：教师改完点保存、提示条也说成功了，重新打开对话框
    还是空的，于是他会一遍遍再试。响应模型少一个字段就足以造成它，
    而且不会有任何报错。
    """
    note = "考场在东楼 302；备用机在讲台旁"
    saved = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"], json={"note": note}, headers=admin_headers
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["note"] == note, "PATCH 的回执里就该带着它"

    listed = contests_of(client, admin_headers)
    assert listed[0]["note"] == note, "列表里也要能读回来"

    # 只改别的字段时，备注不该被顺手清掉
    renamed = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"name": "改了名字"},
        headers=admin_headers,
    ).json()
    assert renamed["note"] == note
