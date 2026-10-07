"""诊断回传（Agent → 服务端）的服务端侧。

Agent 那边早就上线了：每 10 分钟一份、连续失败 2 次补一份、tick 里带
``diagnostics_request`` 时补一份（见 ``agent/syncoj_agent/diagnostics.py``）。这里
守的是服务端那一半：

* ``POST /api/v1/agent/diagnostics``：**两道上限**（压缩后 256 KB、解压后 1 MB）、
  只收 JSON 对象、按机器 60 秒限速（429，对 Agent 是"这次没传成"）、
  **每台机器只留最新一份**。
* ``diagnostics_request`` 一次性字段：两条 tick 路径（正常 / 未配对）都要能回一次
  ``true``，点一次只回一次。
* 管理端读取与请求端点、错误码、审计。

"只校验是对象、不逐字段校验"是刻意的：Agent 将来加字段不该被服务端拒绝。
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from conftest import agent_headers, do_tick, enroll_machine
from syncoj_server.models import AgentDiagnostic, EventLog
from syncoj_server.services import diagnostics as diag


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def make_bundle(reason: str = "periodic", **extra) -> dict:
    """Agent 真正会发的那种形状（字段名见 diagnostics.build_bundle）。"""
    bundle = {
        "version": "0.1.0",
        "generated_at": 1767225600,
        "reason": reason,
        "state": "ready",
        "machine_id": "m-diag",
        "machine_uuid": "uuid-diag",
        "hostname": "exam-pc-01",
        "run_user": "student",
        "os_info": "NOI Linux 2.0",
        "tick": {"count": 3, "last_error": None, "consecutive_failures": 0},
        "process": {"pid": 1, "uptime_seconds": 1.0, "rss_bytes": 1, "threads": 1},
        "disk": {"free_bytes": 1, "total_bytes": 2},
        "policy": {"scan_interval": 30, "max_file_size": 2, "max_files": 100},
        "config_summary": {"server.url": "http://10.0.0.1:8000"},
        "log_tail": "最后一行日志",
    }
    bundle.update(extra)
    return bundle


def raw_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def gzip_json(value: object) -> bytes:
    return gzip.compress(raw_json(value), mtime=0)


def upload(client, token: str, blob: bytes):
    return client.post(
        "/api/v1/agent/diagnostics",
        content=blob,
        headers={**agent_headers(token), "Content-Type": "application/gzip"},
    )


def admin_get(client, admin_headers: dict, agent_id: int):
    return client.get(
        "/api/v1/admin/agents/%d/diagnostics" % agent_id, headers=admin_headers
    )


def admin_request(client, admin_headers: dict, agent_id: int):
    return client.post(
        "/api/v1/admin/agents/%d/diagnostics/request" % agent_id,
        headers=admin_headers,
    )


def stored_rows(app) -> list:
    with app.state.ctx.db.session() as session:
        return list(session.execute(select(AgentDiagnostic)).scalars())


def event_rows(app, category: str) -> list:
    with app.state.ctx.db.session() as session:
        return list(
            session.execute(
                select(EventLog).where(EventLog.category == category)
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# 上报：成功、覆盖、限速
# --------------------------------------------------------------------------- #


def test_正常上报_200_且库里只有一份(app, client, admin_headers, enrolled) -> None:
    first = make_bundle(reason="periodic", log_tail="第一份")
    response = upload(client, enrolled["token"], gzip_json(first))

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["received_at"]
    # bytes 是**解压后** JSON 文本的字节数，与库里那一列、管理端读到的同一个数
    assert receipt["bytes"] == len(raw_json(first))

    got = admin_get(client, admin_headers, enrolled["agent_id"])
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["content"] == first
    assert body["reason"] == "periodic"
    assert body["bytes"] == receipt["bytes"]
    assert body["received_at"] == receipt["received_at"]

    assert len(stored_rows(app)) == 1


def test_再发一份是覆盖_时间与内容都换新(app, client, admin_headers, enrolled) -> None:
    first = make_bundle(reason="periodic", log_tail="第一份")
    assert upload(client, enrolled["token"], gzip_json(first)).status_code == 200

    # 把第一次的收到时刻改旧：收到时间精确到秒，同一秒内的两次上传在 API 上
    # 看不出差别，直接回填一个明确的旧值才能把"换新了"钉死。
    with app.state.ctx.db.session() as session:
        row = session.get(AgentDiagnostic, enrolled["agent_id"])
        row.received_at = datetime(2020, 1, 1, 0, 0, 0)

    app.state.ctx.diagnostics_limiter.reset()

    second = make_bundle(reason="manual", log_tail="第二份")
    assert upload(client, enrolled["token"], gzip_json(second)).status_code == 200

    rows = stored_rows(app)
    assert len(rows) == 1, "同一台机器只该有一行（覆盖写）"
    assert rows[0].content == raw_json(second).decode("utf-8")
    assert rows[0].reason == "manual"

    body = admin_get(client, admin_headers, enrolled["agent_id"]).json()
    assert body["content"] == second
    assert body["received_at"] != "2020-01-01T00:00:00Z", "收到时刻应当被换新"


def test_同一台机器_60_秒内第二次_429_且带_retry_after(client, enrolled) -> None:
    blob = gzip_json(make_bundle())

    assert upload(client, enrolled["token"], blob).status_code == 200

    second = upload(client, enrolled["token"], blob)
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) >= 1


def test_限速按机器_另一台不受影响(client, bootstrap_key, enrolled) -> None:
    blob = gzip_json(make_bundle())
    assert upload(client, enrolled["token"], blob).status_code == 200

    other = enroll_machine(client, bootstrap_key, machine_id="m-diag-other")
    response = upload(client, other["token"], blob)
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 上限与坏数据
# --------------------------------------------------------------------------- #


def test_压缩后超限被拒(client, enrolled) -> None:
    blob = b"\x00" * (diag.MAX_COMPRESSED_BYTES + 1)

    response = upload(client, enrolled["token"], blob)

    assert response.status_code == 413, response.text
    assert response.json()["code"] == "payload_too_large"


def test_解压后超限被拒_高压缩比(client, enrolled) -> None:
    # 几 KB 的 gzip 展开成 1 MB+：只看压缩后大小等于没防 zip bomb
    blob = gzip.compress(b"a" * (diag.MAX_DECOMPRESSED_BYTES + 1))

    response = upload(client, enrolled["token"], blob)

    assert response.status_code == 413, response.text
    assert response.json()["code"] == "payload_too_large"


def test_坏_gzip_被拒(client, enrolled) -> None:
    response = upload(client, enrolled["token"], "这不是 gzip，是一段纯文本".encode("utf-8"))

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "bad_request"


def test_截断的_gzip_被拒(client, enrolled) -> None:
    blob = gzip.compress(raw_json(make_bundle()))
    response = upload(client, enrolled["token"], blob[: len(blob) // 2])

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "bad_request"


@pytest.mark.parametrize("value", [[1, 2, 3], "纯文本", 42, None])
def test_解压出来不是_JSON_对象被拒(client, enrolled, value) -> None:
    """只校验"是对象"：数组、标量、null 一律拒；对象里的字段一概不管。"""
    response = upload(client, enrolled["token"], gzip_json(value))

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "bad_request"


def test_对象里的额外字段被照单全收(client, admin_headers, enrolled) -> None:
    """Agent 将来加字段不该被服务端拒绝 —— 服务端只校验"是个对象"。"""
    payload = make_bundle(reason="manual", 未来字段={"a": 1})
    assert upload(client, enrolled["token"], gzip_json(payload)).status_code == 200

    body = admin_get(client, admin_headers, enrolled["agent_id"]).json()
    assert body["content"]["未来字段"] == {"a": 1}


# --------------------------------------------------------------------------- #
# tick 的一次性 diagnostics_request
# --------------------------------------------------------------------------- #


def test_没点过是_false(client, enrolled) -> None:
    body = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    assert body.get("diagnostics_request") is False


def test_点一次只回一次_true_正常_tick_路径(
    client, admin_headers, enrolled
) -> None:
    do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    assert admin_request(client, admin_headers, enrolled["agent_id"]).status_code == 200

    first = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert first["diagnostics_request"] is True

    second = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert second["diagnostics_request"] is False, "一次性标记取走之后不该再回 true"


def test_点一次只回一次_true_未配对的_tick_路径(
    client, admin_headers, bootstrap_key
) -> None:
    """``_tick_pending`` 那条路同样要处理：待配对的机器也要被点到。"""
    machine = enroll_machine(client, bootstrap_key, machine_id="m-diag-pending")
    token = machine["token"]

    assert (
        do_tick(client, token, [], machine_id="m-diag-pending")["diagnostics_request"]
        is False
    )
    assert admin_request(client, admin_headers, machine["agent_id"]).status_code == 200

    first = do_tick(client, token, [], machine_id="m-diag-pending")
    assert first["diagnostics_request"] is True

    second = do_tick(client, token, [], machine_id="m-diag-pending")
    assert second["diagnostics_request"] is False


def test_未配对的机器也能上报(client, bootstrap_key) -> None:
    """卡在配对阶段的机器，它的现场恰恰最有用。"""
    machine = enroll_machine(client, bootstrap_key, machine_id="m-diag-unpaired")

    response = upload(client, machine["token"], gzip_json(make_bundle()))

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 管理端
# --------------------------------------------------------------------------- #


def test_没有收到过是_404_diagnostics_not_found(client, admin_headers, enrolled) -> None:
    response = admin_get(client, admin_headers, enrolled["agent_id"])

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "diagnostics_not_found"


def test_两个管理端点都要管理员(client, enrolled) -> None:
    agent_id = enrolled["agent_id"]

    get = client.get("/api/v1/admin/agents/%d/diagnostics" % agent_id)
    post = client.post("/api/v1/admin/agents/%d/diagnostics/request" % agent_id)

    assert get.status_code == 401
    assert post.status_code == 401


def test_请求诊断记一条审计(app, client, admin_headers, enrolled) -> None:
    response = admin_request(client, admin_headers, enrolled["agent_id"])
    assert response.status_code == 200, response.text

    rows = event_rows(app, "diagnostics_request")
    assert len(rows) == 1
    assert rows[0].agent_id == enrolled["agent_id"]


def test_只给不知道的机器要诊断是_agent_not_found(client, admin_headers) -> None:
    response = admin_request(client, admin_headers, 999999)

    assert response.status_code == 404
    assert response.json()["code"] == "agent_not_found"


def test_首次上报记一条审计_覆盖不再记(app, client, enrolled) -> None:
    """10 分钟一份的例行上报不该把审计刷满，所以只在第一次收到时记一条。"""
    first = gzip_json(make_bundle(reason="periodic"))
    assert upload(client, enrolled["token"], first).status_code == 200

    app.state.ctx.diagnostics_limiter.reset()
    assert upload(client, enrolled["token"], gzip_json(make_bundle(reason="error"))).status_code == 200

    rows = event_rows(app, "diagnostics_received")
    assert len(rows) == 1
    assert rows[0].level == "info"
    assert rows[0].agent_id == enrolled["agent_id"]


def test_库里的_bytes_与内容长度一致(app, client, enrolled) -> None:
    payload = make_bundle()
    assert upload(client, enrolled["token"], gzip_json(payload)).status_code == 200

    with app.state.ctx.db.session() as session:
        row = session.get(AgentDiagnostic, enrolled["agent_id"])
        assert row is not None
        assert row.bytes == len(row.content.encode("utf-8"))
        assert row.bytes == len(raw_json(payload))
