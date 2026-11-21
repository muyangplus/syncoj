"""服务端主动通告（§11）：通告报文、节奏、挑战应答端点、「召集机器」。"""

from __future__ import annotations

import base64
import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncoj_server.services import announce
from syncoj_server.services.signing import generate_keypair, openssl_available

requires_openssl = pytest.mark.skipif(openssl_available() is None, reason="需要 openssl")

NONCE = "0123456789abcdef0123456789abcdef"


def _verify(key, text: str, sig: str) -> bool:
    import hashlib

    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    # 公钥运算：s^e mod n，与 PKCS#1 v1.5 + SHA-256 的期望编码比较
    em = pow(int.from_bytes(raw, "big"), key.e, key.n).to_bytes((key.n.bit_length() + 7) // 8, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420")
    return em.endswith(digest_info + hashlib.sha256(text.encode("utf-8")).digest())


@pytest.fixture()
def keyed(app, workdir: Path):
    if openssl_available() is None:
        pytest.skip("需要 openssl")
    key = generate_keypair(workdir / "release.pem", bits=2048)
    app.state.ctx.signing_key = key
    app.state.ctx.settings.public_url = "http://10.9.8.7:8000"
    return key


def test_通告报文小而且不带签名() -> None:
    raw = announce.build_announcement("http://10.0.0.5:8000", "kid", "b" * 16)
    payload = json.loads(raw)
    assert payload["kind"] == "announce" and payload["url"] == "http://10.0.0.5:8000"
    assert "sig" not in payload, "通告挡不住重放，签了也不能信，所以干脆不签"
    assert len(raw) < 200
    assert announce.build_announcement("http://" + "x" * 600, "kid", "b") is None


def test_前十分钟每30秒一次_之后每5分钟一次() -> None:
    announcer = announce.Announcer()
    start = announcer.started_at
    assert announcer.interval(start + 10) == announce.FAST_INTERVAL_SECONDS
    assert announcer.interval(start + announce.FAST_PERIOD_SECONDS + 1) == announce.SLOW_INTERVAL_SECONDS


@requires_openssl
def test_挑战应答签的是服务端自己认定的地址(client: TestClient, keyed) -> None:
    response = client.get("/api/v1/agent/whoami", params={"nonce": NONCE})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url"] == "http://10.9.8.7:8000"
    assert body["nonce"] == NONCE and body["key_id"] == keyed.key_id
    assert _verify(keyed, announce.challenge_text(NONCE, body["url"]), body["sig"])
    assert not _verify(keyed, announce.challenge_text("f" * 32, body["url"]), body["sig"])


def test_挑战应答_随机数形状不对就拒(client: TestClient) -> None:
    assert client.get("/api/v1/agent/whoami", params={"nonce": "short"}).status_code == 422
    assert client.get("/api/v1/agent/whoami", params={"nonce": "G" * 32}).status_code == 422


def test_挑战应答_没有私钥就说清楚(client: TestClient, app) -> None:
    app.state.ctx.signing_key = None
    response = client.get("/api/v1/agent/whoami", params={"nonce": NONCE})
    assert response.status_code == 503 and response.json()["code"] == "no_signing_key"


@requires_openssl
def test_挑战应答按IP限速(client: TestClient, keyed, app) -> None:
    from syncoj_server.services.ratelimit import RateLimiter

    app.state.ctx.whoami_limiter = RateLimiter(limit=2)
    codes = [client.get("/api/v1/agent/whoami", params={"nonce": NONCE}).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


@requires_openssl
def test_召集机器立即喊一次(client: TestClient, admin_headers: dict, keyed, app, monkeypatch) -> None:
    sent = []

    class FakeSocket:
        def sendto(self, raw, target):
            sent.append((json.loads(raw), target))

    monkeypatch.setattr(announce, "broadcast_targets", lambda: ["255.255.255.255"])
    response = client.post("/api/v1/admin/machines/announce", headers=admin_headers)
    assert response.status_code == 200
    assert app.state.ctx.announcer.take_request(), "按钮只是把请求交给后台任务"

    assert announce.announce_once(app.state.ctx, app.state.ctx.announcer, FakeSocket())
    (payload, target), = sent
    assert payload["url"] == "http://10.9.8.7:8000" and target[1] == app.state.ctx.settings.announce_port
    status = client.get("/api/v1/admin/machines/announce", headers=admin_headers).json()
    assert status["last_url"] == "http://10.9.8.7:8000" and status["last_error"] is None


def test_不知道自己的地址就不喊(app) -> None:
    ctx = app.state.ctx
    ctx.settings.public_url = None
    ctx.public_url_hint = None
    assert not announce.announce_once(ctx, ctx.announcer, socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    assert "地址" in ctx.announcer.last_error


def test_广播地址按小端还原(tmp_path) -> None:
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t000200C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n",
        encoding="ascii",
    )
    assert announce.broadcast_targets(str(route)) == ["255.255.255.255", "192.0.2.255"]


def test_两个环境变量真的被读到(monkeypatch) -> None:
    """ADR-0014：配置项要有测试证明它真的被读到。"""
    from syncoj_server.config import Settings

    monkeypatch.setenv("SYNCOJ_ANNOUNCE", "0")
    monkeypatch.setenv("SYNCOJ_ANNOUNCE_PORT", "45999")
    settings = Settings()
    assert settings.announce_enabled is False and settings.announce_port == 45999
