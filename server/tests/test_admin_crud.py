"""补齐的增删改查入口。

这一批端点大多是"缺了很久但一直没人提起"的那类：平时用不到，
真要用的那一刻（导错一个选手、发错一个文件、比赛结束要清场）才会发现没有入口。
所以这里逐条盯三件事：

* 危险操作**该拦的拦住**（删场次要打标识、清成绩默认保留手工分）
* 说清楚**边界**（删记录不等于删机器上的文件、改名只影响没落地的）
* 返回的说明是给人看的 —— 它会直接显示在界面上，是教师唯一能拿到的反馈
"""

from __future__ import annotations

from typing import Dict, List

from fastapi.testclient import TestClient

from conftest import agent_headers, do_tick as tick, scan_entry as entry, sha256_of


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
    return client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=headers
    ).json()


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
    """
    response = client.delete(
        "/api/v1/admin/contests/%d" % contest["id"], headers=admin_headers
    )
    assert response.status_code == 400
    assert contest["slug"] in response.json()["detail"]

    still_there = client.get("/api/v1/admin/contests", headers=admin_headers).json()
    assert len(still_there) == 1, "确认失败时不该真的删掉"


def test_delete_contest_with_confirmation(client: TestClient, admin_headers: dict,
                                          contest: dict, player: dict) -> None:
    response = client.delete(
        "/api/v1/admin/contests/%d?confirm_slug=%s" % (contest["id"], contest["slug"]),
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    detail = response.json()["detail"]
    assert "选手 1" in detail, detail
    assert client.get("/api/v1/admin/contests", headers=admin_headers).json() == []


def test_delete_contest_clears_online_registry(client: TestClient, admin_headers: dict,
                                               contest: dict, enrolled: dict) -> None:
    """内存里的在线状态要跟着清，否则顶栏会继续显示一台已经不存在的场次的机器。"""
    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert client.get(
        "/api/v1/admin/contests/%d/agents" % contest["id"], headers=admin_headers
    ).json()

    client.delete(
        "/api/v1/admin/contests/%d?confirm_slug=%s" % (contest["id"], contest["slug"]),
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
    ).json()[0]

    response = client.patch(
        "/api/v1/admin/players/%d" % other["id"],
        json={"player_no": "S001"},
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_delete_player_removes_their_code(client: TestClient, admin_headers: dict,
                                          contest: dict, enrolled: dict) -> None:
    upload_code(client, enrolled, "p1/p1.cpp", b"int main(){}")
    assert len(files_of(client, admin_headers, contest)) == 1

    player_id = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()[0]["id"]
    response = client.delete(
        "/api/v1/admin/players/%d" % player_id, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert "代码 1 条" in response.json()["detail"]
    assert files_of(client, admin_headers, contest) == []


def test_clear_players_keeps_those_with_submissions(client: TestClient, admin_headers: dict,
                                                    contest: dict, enrolled: dict) -> None:
    """清空名单默认**保留**有提交的选手 —— 顺手把参赛者的提交删掉是不可逆的事故。"""
    upload_code(client, enrolled, "p1/p1.cpp", b"int main(){}")
    client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S999"}],
        headers=admin_headers,
    )

    response = client.delete(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    )
    assert response.status_code == 200, response.text
    detail = response.json()["detail"]
    assert "保留 1 名有提交" in detail, detail

    left = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()
    assert [p["player_no"] for p in left] == ["S001"]


def test_clear_players_can_remove_everything(client: TestClient, admin_headers: dict,
                                             contest: dict, player: dict) -> None:
    response = client.delete(
        "/api/v1/admin/contests/%d/players?keep_with_submissions=false" % contest["id"],
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json() == []


# --------------------------------------------------------------------------- #
# 注册码
# --------------------------------------------------------------------------- #


def test_list_enroll_codes_never_leaks_the_code(client: TestClient, admin_headers: dict,
                                                contest: dict, player: dict) -> None:
    issued = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()

    rows = client.get(
        "/api/v1/admin/contests/%d/enroll-codes" % contest["id"], headers=admin_headers
    ).json()
    assert len(rows) == 1
    assert rows[0]["usable"] is True
    assert rows[0]["bound_machine_id"] is None
    # 明文只该在签发那一刻出现过
    assert issued["code"] not in str(rows)


def test_revoke_single_enroll_code(client: TestClient, admin_headers: dict,
                                   contest: dict, player: dict) -> None:
    client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    )
    code_id = client.get(
        "/api/v1/admin/contests/%d/enroll-codes" % contest["id"], headers=admin_headers
    ).json()[0]["id"]

    assert client.delete(
        "/api/v1/admin/enroll-codes/%d" % code_id, headers=admin_headers
    ).status_code == 200
    assert client.get(
        "/api/v1/admin/contests/%d/enroll-codes" % contest["id"], headers=admin_headers
    ).json() == []


def test_clear_enroll_codes_keeps_bound_ones(client: TestClient, admin_headers: dict,
                                             contest: dict, player: dict,
                                             enrolled: dict) -> None:
    """已绑定机器的注册码是那台机器的「还原自愈种子」，清场时不能顺手撤掉。

    撤掉之后的表现是"某台机器快照还原后再也连不上"，而现场几乎不可能
    联想到是清理注册码那一步干的。
    """
    client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    )

    response = client.delete(
        "/api/v1/admin/contests/%d/enroll-codes" % contest["id"], headers=admin_headers
    )
    assert response.status_code == 200, response.text
    # 断言结果而不是措辞：说明文字会改，行为不该改
    assert "已绑定的保留" in response.json()["detail"], response.json()["detail"]

    rows = client.get(
        "/api/v1/admin/contests/%d/enroll-codes" % contest["id"], headers=admin_headers
    ).json()
    assert len(rows) == 1 and rows[0]["bound_machine_id"], "已绑定的那把被撤掉了"
