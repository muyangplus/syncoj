"""端到端接口测试：注册 → tick → 上传 → 落盘 source/ → 删除检测。

这些测试覆盖的是"两个进程之间到底能不能跑通"，而不是单个函数的返回值。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from conftest import agent_headers, do_tick as tick, scan_entry as entry, sha256_of
from syncoj_server.config import Settings


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_admin_login_rejects_bad_password(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/api/v1/admin/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert response.status_code == 401


def test_admin_login_rejects_unknown_user(client: TestClient, app) -> None:
    response = client.post(
        "/api/v1/admin/login", json={"username": "nobody", "password": "whatever"}
    )
    assert response.status_code == 401


def test_admin_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/admin/contests").status_code == 401
    assert client.get("/api/v1/admin/contests/1/players").status_code == 401
    assert client.get("/api/v1/admin/contests/1/agents").status_code == 401


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


def test_enroll_full_flow(client: TestClient, enrolled: dict, contest: dict, player: dict) -> None:
    assert enrolled["player_no"] == "S001"
    assert enrolled["contest_slug"] == contest["slug"]
    assert enrolled["token"]
    assert enrolled["config"]["tick_idle_seconds"] > enrolled["config"]["tick_active_seconds"]
    assert ".cpp" in enrolled["config"]["extensions"]


def test_enroll_rejects_bad_code(client: TestClient, contest: dict, player: dict) -> None:
    response = client.post(
        "/api/v1/agent/enroll",
        json={"enroll_code": "AAAA-BBBB-CCCC-DDDD", "machine_id": "m1"},
    )
    assert response.status_code == 404


def test_enroll_code_is_bound_to_first_machine(
    client: TestClient, admin_headers: dict, player: dict
) -> None:
    code = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()["code"]

    first = client.post(
        "/api/v1/agent/enroll",
        json={"enroll_code": code, "machine_id": "machine-A"},
    )
    assert first.status_code == 200

    # 同一注册码换一台机器 —— 必须拒绝，否则一台机器的配置可以被整场复制
    second = client.post(
        "/api/v1/agent/enroll",
        json={"enroll_code": code, "machine_id": "machine-B"},
    )
    assert second.status_code == 403


def test_reenroll_rotates_token_and_invalidates_old(
    client: TestClient, admin_headers: dict, player: dict
) -> None:
    """快照还原自愈：同一台机器重复 enroll 应换发新凭据，旧凭据立即失效。"""
    code = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()["code"]

    body = {"enroll_code": code, "machine_id": "machine-X", "hostname": "pc-1"}
    first = client.post("/api/v1/agent/enroll", json=body)
    assert first.status_code == 200
    old_token = first.json()["token"]

    second = client.post("/api/v1/agent/enroll", json=body)
    assert second.status_code == 200
    new_token = second.json()["token"]
    assert new_token != old_token

    # 旧凭据必须已经不能用了
    stale = client.get("/api/v1/agent/me", headers=agent_headers(old_token))
    assert stale.status_code == 401
    assert client.get("/api/v1/agent/me", headers=agent_headers(new_token)).status_code == 200


def test_enroll_code_issuance_revokes_previous(
    client: TestClient, admin_headers: dict, player: dict
) -> None:
    first = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()["code"]
    second = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()["code"]
    assert first != second

    # 旧注册码应已被吊销
    response = client.post(
        "/api/v1/agent/enroll", json={"enroll_code": first, "machine_id": "m-1"}
    )
    assert response.status_code == 403
    assert (
        client.post(
            "/api/v1/agent/enroll", json={"enroll_code": second, "machine_id": "m-1"}
        ).status_code
        == 200
    )


def test_enroll_code_format_pasted_with_separators(
    client: TestClient, admin_headers: dict, player: dict
) -> None:
    """教师手抄注册码时大小写和连字符都不该成为障碍。"""
    code = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()["code"]
    messy = code.replace("-", " ").lower()

    response = client.post(
        "/api/v1/agent/enroll", json={"enroll_code": messy, "machine_id": "m-1"}
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# tick 与回收
# --------------------------------------------------------------------------- #


def test_tick_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/agent/tick", json={})
    assert response.status_code == 401


def test_tick_empty_scan_then_needs_upload(client: TestClient, enrolled: dict) -> None:
    token, machine = enrolled["token"], enrolled["machine_id"]

    body = tick(client, token, [], machine_id=machine)
    assert body["need_upload"] == []
    # 空闲时应放宽轮询周期
    assert body["next_tick_seconds"] == 60

    content = b"int main() { return 0; }\n"
    body = tick(client, token, [entry("main.cpp", content)], machine_id=machine)
    assert body["need_upload"] == ["main.cpp"]
    # 有活干时应收紧轮询周期
    assert body["next_tick_seconds"] == 2


def test_tick_does_not_re_request_unchanged_content(
    client: TestClient, enrolled: dict
) -> None:
    """内容没变就不该重复索要 —— 否则每轮 tick 都会白传一遍全部代码。"""
    token, machine = enrolled["token"], enrolled["machine_id"]
    content = b"int main() { return 0; }\n"
    scan = [entry("main.cpp", content)]

    tick(client, token, scan, machine_id=machine)
    assert upload(client, token, "main.cpp", content).status_code == 200

    body = tick(client, token, scan, machine_id=machine)
    assert body["need_upload"] == []


def upload(client: TestClient, token: str, path: str, content: bytes):
    return client.post(
        "/api/v1/agent/files",
        data={"path": path, "sha256": sha256_of(content)},
        files={"file": (Path(path).name, content, "application/octet-stream")},
        headers=agent_headers(token),
    )


def scan_and_upload(
    client: TestClient,
    token: str,
    machine_id: str,
    path: str,
    content: bytes,
    scan=None,
):
    """完整走一遍协议：先 tick 声明内容，再上传。

    ``scan`` 用于需要同时声明多个文件的场景 —— tick 上报的是**全量**扫描结果，
    漏报的文件会被服务端判定为已删除。
    """
    entries = scan if scan is not None else [entry(path, content)]
    body = tick(client, token, entries, machine_id=machine_id)
    assert path in body["need_upload"], "tick 应先声明该文件需要上传"
    return upload(client, token, path, content)


def test_upload_lands_in_source_tree(
    client: TestClient, enrolled: dict, contest: dict, settings: Settings
) -> None:
    token = enrolled["token"]
    content = b"#include <cstdio>\nint main(){puts(\"hi\");}\n"

    response = upload(client, token, "src/main.cpp", content)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"] is True
    assert body["revision"] == 1

    landed = (
        settings.source_root
        / contest["slug"]
        / enrolled["player_no"]
        / "src"
        / "main.cpp"
    )
    assert landed.is_file(), "文件必须落到评测器可读的 source/ 目录"
    assert landed.read_bytes() == content

    # blob 存储按 sha256 分片
    assert (settings.blob_root / sha256_of(content)[:2] / sha256_of(content)).is_file()


def test_upload_revision_increments_on_content_change(
    client: TestClient, enrolled: dict
) -> None:
    """每次内容变化，版本号必须递增 —— 它是"丢弃迟到旧版本"的依据。"""
    token, machine = enrolled["token"], enrolled["machine_id"]
    assert scan_and_upload(client, token, machine, "main.cpp", b"v1").json()["revision"] == 1
    assert scan_and_upload(client, token, machine, "main.cpp", b"v2").json()["revision"] == 2
    assert scan_and_upload(client, token, machine, "main.cpp", b"v3").json()["revision"] == 3


def test_upload_rejects_hash_mismatch(client: TestClient, enrolled: dict) -> None:
    """客户端声明的 sha256 与实际内容不符 —— 必须拒绝，不能存进去。"""
    token = enrolled["token"]
    response = client.post(
        "/api/v1/agent/files",
        data={"path": "main.cpp", "sha256": "0" * 64},
        files={"file": ("main.cpp", b"actual content", "application/octet-stream")},
        headers=agent_headers(token),
    )
    assert response.status_code == 400


def test_upload_rejects_traversal_path(client: TestClient, enrolled: dict) -> None:
    token = enrolled["token"]
    content = b"pwned"
    response = upload(client, token, "../../../etc/pwned.conf", content)
    assert response.status_code == 400


def test_upload_rejects_oversized_file(client: TestClient, app, enrolled: dict) -> None:
    app.state.ctx.settings.max_file_size = 1024
    response = upload(client, enrolled["token"], "big.cpp", b"x" * 4096)
    assert response.status_code == 413


def test_stale_upload_is_rejected(client: TestClient, enrolled: dict) -> None:
    """断网补传场景：客户端送来的是已被取代的旧版本，必须拒绝而不是覆盖。"""
    token = enrolled["token"]
    assert upload(client, token, "main.cpp", b"newer").status_code == 200

    response = upload(client, token, "main.cpp", b"older")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "stale"
    assert detail["expected"] == sha256_of(b"newer")


def test_scan_rejects_traversal_and_reports_event(
    client: TestClient, admin_headers: dict, enrolled: dict, contest: dict
) -> None:
    body = tick(
        client,
        enrolled["token"],
        [entry("../../etc/passwd", b"x")],
        machine_id=enrolled["machine_id"],
    )
    assert body["need_upload"] == [], "非法路径不得进入待上传列表"

    events = client.get(
        "/api/v1/admin/contests/%d/events" % contest["id"], headers=admin_headers
    ).json()
    categories = [e["category"] for e in events]
    assert "scan_rejected" in categories


def test_tick_detects_deleted_file(
    client: TestClient, admin_headers: dict, enrolled: dict, contest: dict
) -> None:
    token, machine = enrolled["token"], enrolled["machine_id"]
    content = b"int main(){}"

    scan_and_upload(client, token, machine, "main.cpp", content)

    # 文件从扫描结果里消失 = 选手删了
    body = tick(client, token, [], machine_id=machine)
    assert body["next_tick_seconds"] == 2, "删除属于需要关注的变化"

    events = client.get(
        "/api/v1/admin/contests/%d/events" % contest["id"], headers=admin_headers
    ).json()
    deleted = [e for e in events if e["category"] == "file_deleted"]
    assert deleted, "删除必须留下审计记录"
    assert "main.cpp" in deleted[0]["meta"]["paths"]


def test_file_listing_reflects_ledger(
    client: TestClient, admin_headers: dict, enrolled: dict, contest: dict
) -> None:
    token, machine = enrolled["token"], enrolled["machine_id"]
    one, two = b"one", b"two"
    full_scan = [entry("a/one.cpp", one), entry("b/two.cpp", two)]

    scan_and_upload(client, token, machine, "a/one.cpp", one, scan=full_scan)
    scan_and_upload(client, token, machine, "b/two.cpp", two, scan=full_scan)

    rows = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=admin_headers
    ).json()
    paths = sorted(r["rel_path"] for r in rows)
    assert paths == ["a/one.cpp", "b/two.cpp"]
    assert all(r["content_stored"] for r in rows)


def test_incomplete_scan_does_not_mark_files_deleted(
    client: TestClient, admin_headers: dict, enrolled: dict, contest: dict
) -> None:
    """扫描中途失败（如子目录权限不足）时，绝不能把漏扫当成删除。

    这是真实会发生的场景：选手在自己目录里建了个 0700 的子目录，Agent 递归
    进去会 PermissionError。若不设防，服务端会把这批文件全部标记成"已删除"，
    审计日志被污染，教师会收到一堆假告警。
    """
    token, machine = enrolled["token"], enrolled["machine_id"]
    content = b"int main(){}"
    scan_and_upload(client, token, machine, "main.cpp", content)

    body = tick(client, token, [], machine_id=machine, scan_complete=False)
    assert body["need_upload"] == []

    rows = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=admin_headers
    ).json()
    assert [r["rel_path"] for r in rows] == ["main.cpp"]
    assert rows[0]["deleted_at"] is None, "扫描不完整时不得标记删除"

    events = client.get(
        "/api/v1/admin/contests/%d/events" % contest["id"], headers=admin_headers
    ).json()
    assert "scan_incomplete" in [e["category"] for e in events]
    assert "file_deleted" not in [e["category"] for e in events]


# --------------------------------------------------------------------------- #
# 在线状态
# --------------------------------------------------------------------------- #


def test_online_status_tracks_ticks(
    client: TestClient, admin_headers: dict, enrolled: dict, contest: dict
) -> None:
    agents = client.get(
        "/api/v1/admin/contests/%d/agents" % contest["id"], headers=admin_headers
    ).json()
    assert len(agents) == 1
    assert agents[0]["online"] is False, "还没 tick 过，不应是在线"

    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    agents = client.get(
        "/api/v1/admin/contests/%d/agents" % contest["id"], headers=admin_headers
    ).json()
    assert agents[0]["online"] is True
    assert agents[0]["last_tick_at"] is not None
    assert agents[0]["seconds_since_tick"] is not None

    players = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()
    assert players[0]["online"] is True
    assert players[0]["has_agent"] is True


def test_machine_id_mismatch_is_rejected(client: TestClient, enrolled: dict) -> None:
    """凭据被复制到另一台机器时，声明的 machine_id 会与凭据不符。"""
    response = client.post(
        "/api/v1/agent/tick",
        json={
            "machine_id": "some-other-machine",
            "ts": 1,
            "scan": [],
            "partials": [],
            "stats": {},
        },
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 409


def test_events_endpoint_records_agent_reports(client: TestClient, enrolled: dict) -> None:
    response = client.post(
        "/api/v1/agent/events",
        json=[
            {
                "level": "warning",
                "category": "disk_full",
                "message": "磁盘空间不足",
                "meta": {"free": 1024},
            }
        ],
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
