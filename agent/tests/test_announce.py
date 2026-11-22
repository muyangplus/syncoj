"""Agent 侧的服务端主动通告（§11）：过滤、挑战应答、切换。

挑战应答用服务端那份签名实现现签（与生产同一把钥匙的两端），跑在一个本地 HTTP 服务上。
"""

from __future__ import annotations

import base64
import http.server
import json
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "server") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "server"))

from syncoj_agent import announce  # noqa: E402
from syncoj_agent.rsa import RSAPublicKey  # noqa: E402

try:
    from syncoj_server.services import announce as server_announce  # noqa: E402
    from syncoj_server.services.signing import generate_keypair, openssl_available  # noqa: E402

    HAS_OPENSSL = openssl_available() is not None
except ImportError:  # pragma: no cover
    HAS_OPENSSL = False

requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")


def packet(**overrides) -> bytes:
    payload = {"syncoj": "syncoj", "v": 1, "kind": "announce",
               "url": "http://10.0.0.9:8000", "key_id": "kid", "boot_id": "b1"}
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_两端的签名对象逐字节一致() -> None:
    if not HAS_OPENSSL:
        pytest.skip("需要服务端代码")
    assert announce.challenge_text("n", "u") == server_announce.challenge_text("n", "u")
    assert announce.challenge_text_v2("n", "u", "s" * 32) == server_announce.challenge_text_v2("n", "u", "s" * 32)
    assert announce.challenge_text_v2("n", "u", "s").startswith("syncoj-announce-v2\n")
    assert announce.DEFAULT_ANNOUNCE_PORT == server_announce.DEFAULT_ANNOUNCE_PORT


def test_只认合法的通告() -> None:
    assert announce.parse_announcement(packet()) == ("http://10.0.0.9:8000", "kid", "b1")
    assert announce.parse_announcement(packet(kind="probe")) is None
    assert announce.parse_announcement(packet(url="file:///etc/passwd")) is None
    assert announce.parse_announcement(packet(url="http://x/\r\nHost: y")) is None
    assert announce.parse_announcement(b"x" * 600) is None
    assert announce.parse_announcement(b"{not json") is None


def test_候选槽_别的服务端不要_同一次启动只要一次_限速() -> None:
    slot = announce.CandidateSlot("kid")
    assert not slot.offer(packet(key_id="other"), now=0)
    assert slot.offer(packet(), now=0)
    assert slot.take() == "http://10.0.0.9:8000"
    assert slot.take() is None
    assert not slot.offer(packet(), now=1000), "同一个 boot_id 只处理一次"
    assert not slot.offer(packet(boot_id="b2"), now=10), "一分钟内只认一次"
    assert slot.offer(packet(boot_id="b2"), now=100)


class _WhoAmI(http.server.BaseHTTPRequestHandler):
    key = None
    signed_url = "http://10.0.0.9:8000"
    tamper = False
    #: 非空 = 新服务端：请求带 v=2 时按 v2 签、回 server_uuid；空 = 老服务端，只会 v1
    server_uuid = ""
    queries: list = []

    def do_GET(self):  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).queries.append(query)
        nonce = query["nonce"][0]
        assert "Authorization" not in self.headers, "挑战应答不该带凭据"
        signed_nonce = "f" * 32 if self.tamper else nonce
        v2 = bool(self.server_uuid) and query.get("v") == ["2"]
        if v2:
            text = server_announce.challenge_text_v2(signed_nonce, self.signed_url, self.server_uuid)
        else:
            text = server_announce.challenge_text(signed_nonce, self.signed_url)
        sig = self.key.sign(text.encode("utf-8"))
        reply = {"url": self.signed_url, "nonce": nonce,
                 "sig": base64.urlsafe_b64encode(sig).decode().rstrip("=")}
        if v2:
            reply["server_uuid"] = self.server_uuid
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    key = generate_keypair(tmp_path_factory.mktemp("k") / "r.pem", bits=2048)
    public = RSAPublicKey(n=key.n, e=key.e, key_id=key.key_id)
    return key, public


@pytest.fixture()
def whoami_server(keypair):
    handler = type("H", (_WhoAmI,), {"key": keypair[0], "queries": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()


@requires_openssl
def test_挑战应答验过签_切到签名里的地址(keypair, whoami_server) -> None:
    server, _handler = whoami_server
    url = "http://127.0.0.1:%d" % server.server_port
    assert announce.verify_announced(url, keypair[1]) == "http://10.0.0.9:8000"


@requires_openssl
def test_签名对不上随机数就不切(keypair, whoami_server) -> None:
    server, handler = whoami_server
    handler.tamper = True
    url = "http://127.0.0.1:%d" % server.server_port
    assert announce.verify_announced(url, keypair[1]) is None


@requires_openssl
def test_别的钥匙签的不认(keypair, whoami_server, tmp_path) -> None:
    server, _ = whoami_server
    other = generate_keypair(tmp_path / "o.pem", bits=2048)
    url = "http://127.0.0.1:%d" % server.server_port
    assert announce.verify_announced(url, RSAPublicKey(n=other.n, e=other.e)) is None


def test_连不上就不切(keypair) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert announce.verify_announced("http://127.0.0.1:%d" % port, keypair[1], timeout=1) is None


def test_监听线程收到通告就放进槽(tmp_path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    slot = announce.CandidateSlot("kid")
    listener = announce.AnnounceListener(slot, port=port)
    listener.start()
    try:
        assert listener.bound.wait(5)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(packet(), ("127.0.0.1", port))
        for _ in range(50):
            candidate = slot.take()
            if candidate:
                break
            threading.Event().wait(0.1)
        assert candidate == "http://10.0.0.9:8000"
    finally:
        listener.stop()
        listener.join(5)


@requires_openssl
def test_主循环核实后切换地址(keypair, whoami_server, tmp_path) -> None:
    from test_pairing import FakeServer, agent_config, make_agent  # noqa: F401

    from syncoj_agent.config import AgentConfig

    server, handler = whoami_server
    handler.signed_url = "http://127.0.0.1:%d" % server.server_port
    config = AgentConfig.load(None)
    config.state_dir = tmp_path / "state"
    config.deploy_root = tmp_path / "桌面"
    config.scan_roots = [tmp_path / "code"]
    config.bootstrap_key_file = tmp_path / "bootstrap.key"
    config.server_url = "http://10.0.0.1:8000"
    agent = make_agent(config)
    agent._public_key = keypair[1]
    agent._announce_slot = announce.CandidateSlot(keypair[1].key_id)
    agent._announce_slot.offer(packet(url=handler.signed_url, key_id=keypair[1].key_id), now=0)

    agent._consider_announcement()

    assert agent.config.server_url == handler.signed_url
    assert any(e.get("category") == "server_address_changed" for e in agent._pending_events)


SERVER_A = "a" * 32
SERVER_B = "b" * 32


@requires_openssl
def test_v2_挑战应答实例ID相同才切(keypair, whoami_server) -> None:
    server, handler = whoami_server
    handler.server_uuid = SERVER_A
    url = "http://127.0.0.1:%d" % server.server_port

    assert announce.verify_announced(url, keypair[1], server_uuid=SERVER_A) == "http://10.0.0.9:8000"
    assert handler.queries[-1].get("v") == ["2"], "新 Agent 要带 v=2"


@requires_openssl
def test_v2_挑战应答实例ID不同就不切(keypair, whoami_server) -> None:
    server, handler = whoami_server
    handler.server_uuid = SERVER_B
    url = "http://127.0.0.1:%d" % server.server_port
    assert announce.verify_announced(url, keypair[1], server_uuid=SERVER_A) is None


@requires_openssl
def test_v2_记着ID时_老服务端的v1应答不认(keypair, whoami_server) -> None:
    server, handler = whoami_server
    handler.server_uuid = ""
    url = "http://127.0.0.1:%d" % server.server_port
    assert announce.verify_announced(url, keypair[1], server_uuid=SERVER_A) is None


@requires_openssl
def test_v2_没记住ID时_v1_v2_都按签名认(keypair, whoami_server) -> None:
    server, handler = whoami_server
    url = "http://127.0.0.1:%d" % server.server_port
    handler.server_uuid = SERVER_B
    assert announce.verify_announced(url, keypair[1]) == "http://10.0.0.9:8000"
    handler.server_uuid = ""
    assert announce.verify_announced(url, keypair[1]) == "http://10.0.0.9:8000"


@requires_openssl
def test_v2_报文里的实例ID被改就验不过(keypair, whoami_server, monkeypatch) -> None:
    """签的是 B，有人把报文里的 server_uuid 改成 A —— 签名对象变了，验不过。"""
    server, handler = whoami_server
    handler.server_uuid = SERVER_B
    original = json.dumps

    def forged_dumps(obj, *args, **kwargs):
        if isinstance(obj, dict) and obj.get("server_uuid") == SERVER_B:
            obj = dict(obj, server_uuid=SERVER_A)
        return original(obj, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", forged_dumps)
    url = "http://127.0.0.1:%d" % server.server_port
    # 不记 ID 也验不过：拦下它的是签名，不是 ID 比对
    assert announce.verify_announced(url, keypair[1]) is None
    assert announce.verify_announced(url, keypair[1], server_uuid=SERVER_A) is None


@requires_openssl
def test_主循环只切到记住的那台服务端(keypair, whoami_server, tmp_path) -> None:
    from test_pairing import make_agent

    from syncoj_agent.config import AgentConfig

    server, handler = whoami_server
    handler.signed_url = "http://127.0.0.1:%d" % server.server_port
    handler.server_uuid = SERVER_B
    config = AgentConfig.load(None)
    config.state_dir = tmp_path / "state"
    config.deploy_root = tmp_path / "桌面"
    config.scan_roots = [tmp_path / "code"]
    config.bootstrap_key_file = tmp_path / "bootstrap.key"
    config.server_url = "http://10.0.0.1:8000"
    agent = make_agent(config)
    agent.server_uuid = SERVER_A
    agent._public_key = keypair[1]
    agent._announce_slot = announce.CandidateSlot(keypair[1].key_id)
    agent._announce_slot.offer(packet(url=handler.signed_url, key_id=keypair[1].key_id), now=0)

    agent._consider_announcement()

    assert agent.config.server_url == "http://10.0.0.1:8000", "别的服务端（实例 ID 不同）不该切过去"
