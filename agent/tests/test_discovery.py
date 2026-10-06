"""Agent 侧的局域网发现：喊一声、验签、只在**恰好一个**答案时才认。

这里最要紧的两件事都不是"能不能找到"：

* **不该信的应答一个字都不信**。找到服务端之后做的第一件事是把统一注册密钥发
  过去，所以一个不验签/验不过的应答比"没找到"危险得多。下面每一类伪造都有一条：
  没签名、签名对但地址被改、另一个随机数的签名（重放）、格式非法、地址不像样。
* **有两个答案时不许猜**。两个考场共用一层楼、或者有人插了一台假服务端时，
  猜错的表现是"代码交上去了但成绩是空的"。所以多个**都验得过**的地址 = 歧义，
  如实报出来。

假服务端是**手写报文**的，不复用任何一端的构造代码 —— 复用会让"两边一起改错"
也能通过（真正的互通性由 `server/tests/test_discovery.py` 从另一侧再证一遍）。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncoj_agent import discovery
from syncoj_agent.rsa import RSAPublicKey

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    # 只用来生成密钥对（agent 自己是零依赖、不含私钥运算）。
    # 与 test_upgrade.py 用的是同一条路子。
    sys.path.insert(0, str(SERVER_DIR))

from syncoj_server.services.signing import generate_keypair, openssl_available  # noqa: E402

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

#: 手写的签名前缀。**故意不 import 任何一端的常量** —— 这里要能发现"某一边把
#: 前缀改了"，而不是跟着它一起改。
SIGNATURE_PREFIX = "syncoj-discovery-v1"


@pytest.fixture()
def signing_key(tmp_path: Path):
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    return generate_keypair(tmp_path / "release.pem", bits=2048)


def canonical(nonce: str, url: str) -> str:
    return "%s\n%s\n%s" % (SIGNATURE_PREFIX, nonce, url)


def signed_reply(signing_key, nonce: str, url: str, **overrides) -> bytes:
    """手写一条应答。``overrides`` 用来造各种坏报文。"""
    signature = base64.urlsafe_b64encode(
        signing_key.sign(canonical(nonce, url).encode("utf-8"))
    ).decode("ascii").rstrip("=")
    payload = {
        "syncoj": "syncoj",
        "v": 1,
        "nonce": nonce,
        "url": url,
        "key_id": "test",
        "sig": signature,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def probe_nonce(raw: bytes) -> str:
    """从探测报文里取出随机数（假服务端要照原样签回去）。"""
    return json.loads(raw.decode("utf-8"))["nonce"]


@contextlib.contextmanager
def fake_server(build):
    """一个只会应答的假服务端，绑在回环的一个随机端口上。

    ``build(raw, peer)`` 返回一个字节串列表 —— 一个探测可以回多条，用来测歧义。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    stop = threading.Event()
    seen = []

    def loop() -> None:
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                raw, peer = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover - 套接字被关掉了
                return
            seen.append(raw)
            for payload in build(raw, peer):
                try:
                    sock.sendto(payload, peer)
                except OSError:  # pragma: no cover
                    return

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, seen=seen)
    finally:
        stop.set()
        thread.join(timeout=3)
        sock.close()


def public_of(signing_key) -> RSAPublicKey:
    return RSAPublicKey.from_dict(signing_key.public_key_dict())


def run_discovery(signing_key, port: int, **kwargs):
    """对着假服务端跑一次发现。广播到不了回环，所以显式指到 127.0.0.1。"""
    return discovery.discover(
        public_of(signing_key),
        port=port,
        timeout=0.6,
        targets=["127.0.0.1"],
        machine_id="m-agent-test",
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


@requires_openssl
def test_找到唯一的服务端(signing_key) -> None:
    url = "http://10.0.0.5:8000"

    def build(raw, peer):
        return [signed_reply(signing_key, probe_nonce(raw), url)]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url == url
    assert outcome.candidates == [url]
    assert not outcome.ambiguous
    assert url in outcome.explain()


@requires_openssl
def test_探测里带了随机数和主机名(signing_key) -> None:
    """随机数是防重放的根：每次都必须新生成。"""
    with fake_server(lambda raw, peer: []) as server:
        run_discovery(signing_key, server.port)

    assert server.seen, "假服务端应当收到探测"
    payload = json.loads(server.seen[0].decode("utf-8"))
    assert payload["syncoj"] == "syncoj"
    assert payload["v"] == discovery.PROTOCOL_VERSION
    assert len(payload["nonce"]) == 32
    assert payload["machine_id"] == "m-agent-test"


@requires_openssl
def test_探测够长_服务端才会理它(signing_key) -> None:
    """长度是"应答不得大于探测"那条约束的另一半 —— 太短服务端直接不理。"""
    with fake_server(lambda raw, peer: []) as server:
        run_discovery(signing_key, server.port)

    assert len(server.seen[0]) >= discovery.MIN_PROBE_BYTES


@requires_openssl
def test_两次探测的随机数不一样(signing_key) -> None:
    with fake_server(lambda raw, peer: []) as server:
        run_discovery(signing_key, server.port)
        run_discovery(signing_key, server.port)

    nonces = {json.loads(raw.decode("utf-8"))["nonce"] for raw in server.seen}
    assert len(nonces) == 2


# --------------------------------------------------------------------------- #
# 没人应答 / 没有公钥
# --------------------------------------------------------------------------- #


def test_没人应答就空手而归() -> None:
    """一个没人监听的端口。不抛异常，也不编一个地址出来。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[0]

    outcome = discovery.discover(
        None, port=port, timeout=0.2, targets=["127.0.0.1"]
    )
    assert outcome.url is None
    assert outcome.candidates == []
    assert "没有服务端" in outcome.explain()


@requires_openssl
def test_没有公钥就完全不问(signing_key) -> None:
    """没有公钥就分不出"服务端"和"随便一个应答者"，那就不该往局域网上喊。

    与"没有密钥就不升级"同一个默认：能力缺失时**关掉**，而不是降级成不安全。
    """
    with fake_server(
        lambda raw, peer: [signed_reply(signing_key, probe_nonce(raw), "http://10.0.0.5:8000")]
    ) as server:
        outcome = discovery.discover(None, port=server.port, timeout=0.3, targets=["127.0.0.1"])

    assert outcome.url is None
    assert server.seen == [], "没有公钥时不该发出任何探测"


# --------------------------------------------------------------------------- #
# 伪造：每一类都要被拒
# --------------------------------------------------------------------------- #


@requires_openssl
def test_没有签名的应答被拒(signing_key) -> None:
    """局域网里任何人都能回一个"我是服务端，地址是这个"。"""
    def build(raw, peer):
        payload = json.loads(signed_reply(signing_key, probe_nonce(raw), "http://10.0.0.66:8000"))
        payload.pop("sig")
        return [json.dumps(payload, separators=(",", ":")).encode("utf-8")]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url is None
    assert outcome.candidates == []


@requires_openssl
def test_篡改地址的应答被拒(signing_key) -> None:
    """**这条是整套东西存在的理由**：签名对得上，但地址被人换成了另一个。

    没有它，一台机器就能把别的机器引到假服务端上、顺手收走统一注册密钥。
    """
    def build(raw, peer):
        nonce = probe_nonce(raw)
        payload = json.loads(signed_reply(signing_key, nonce, "http://10.0.0.5:8000"))
        payload["url"] = "http://10.0.0.66:8000"      # 签的是 .5，发的是 .66
        return [json.dumps(payload, separators=(",", ":")).encode("utf-8")]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url is None, "改了地址还能通过，等于没有鉴权"


@requires_openssl
def test_重放上一次的应答被拒(signing_key) -> None:
    """录下一条真应答原样发回来 —— 随机数对不上，不算数。"""
    canned = signed_reply(signing_key, "0" * 32, "http://10.0.0.5:8000")

    with fake_server(lambda raw, peer: [canned]) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url is None


@requires_openssl
def test_别的私钥签的应答被拒(signing_key, tmp_path: Path) -> None:
    """攻击者自己生成一把密钥 —— 机器手里那把公钥不认它。"""
    other = generate_keypair(tmp_path / "attacker.pem", bits=2048)

    def build(raw, peer):
        return [signed_reply(other, probe_nonce(raw), "http://10.0.0.66:8000")]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"syncoj": "someone-else"},          # 不是我们的协议
        {"v": 99},                           # 版本对不上
        {"url": "not-a-url"},                # 地址不像样
        {"url": "ftp://10.0.0.5:8000"},      # 只认 http/https
        {"sig": "!!!!"},                     # 签名不是 base64
        {"sig": ""},                         # 空签名
    ],
)
@requires_openssl
def test_各种坏应答一律不用(signing_key, overrides) -> None:
    def build(raw, peer):
        # 基础地址可以被 overrides 覆盖 —— 用关键字传，别和位置参数撞上
        fields = {"url": "http://10.0.0.5:8000"}
        fields.update(overrides)
        return [signed_reply(signing_key, probe_nonce(raw), **fields)]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url is None, overrides


@requires_openssl
def test_不是_json的应答不影响别的(signing_key) -> None:
    """一个乱发的包不该让整个发现过程崩掉 —— 先回垃圾、再回真答案。"""
    def build(raw, peer):
        return [b"not json at all", signed_reply(signing_key, probe_nonce(raw), "http://10.0.0.5:8000")]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url == "http://10.0.0.5:8000"


# --------------------------------------------------------------------------- #
# 歧义：两个都验得过时不许猜
# --------------------------------------------------------------------------- #


@requires_openssl
def test_两个服务端算歧义(signing_key) -> None:
    """两个考场共用一层楼时很常见。猜错的表现是"交上去了但成绩是空的"。"""
    def build(raw, peer):
        nonce = probe_nonce(raw)
        return [
            signed_reply(signing_key, nonce, "http://10.0.0.5:8000"),
            signed_reply(signing_key, nonce, "http://10.0.0.6:8000"),
        ]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url is None, "有两个答案时不能替人挑一个"
    assert outcome.candidates == ["http://10.0.0.5:8000", "http://10.0.0.6:8000"]
    assert outcome.ambiguous
    assert "多个服务端" in outcome.explain()


@requires_openssl
def test_同一个地址回两遍不算歧义(signing_key) -> None:
    """服务端在多个地址上收到探测时会回多条（多网卡），那是同一个答案。"""
    def build(raw, peer):
        nonce = probe_nonce(raw)
        return [
            signed_reply(signing_key, nonce, "http://10.0.0.5:8000"),
            signed_reply(signing_key, nonce, "http://10.0.0.5:8000"),
        ]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url == "http://10.0.0.5:8000"
    assert not outcome.ambiguous


@requires_openssl
def test_歧义时混着一个假答案不影响判断(signing_key) -> None:
    """歧义的判据是"验得过的答案有几个"，不是"回了几个"。"""
    def build(raw, peer):
        nonce = probe_nonce(raw)
        return [
            signed_reply(signing_key, nonce, "http://10.0.0.5:8000"),
            signed_reply(signing_key, nonce, "http://10.0.0.66:8000", sig="AAAA"),
        ]

    with fake_server(build) as server:
        outcome = run_discovery(signing_key, server.port)

    assert outcome.url == "http://10.0.0.5:8000"


# --------------------------------------------------------------------------- #
# 两端的常量与文本必须一致（对不上就是"完全没人应答"）
# --------------------------------------------------------------------------- #


def test_协议常量与服务端一致() -> None:
    """端口、版本、魔法字、最小长度任一处不一致，现象都是"局域网里没有服务端"。

    那种现象没有任何线索指向常量 —— 所以在这里对齐一次。
    """
    from syncoj_server.services import discovery as server_side

    assert discovery.MAGIC == server_side.MAGIC
    assert discovery.PROTOCOL_VERSION == server_side.PROTOCOL_VERSION
    assert discovery.MIN_PROBE_BYTES == server_side.MIN_PROBE_BYTES
    assert discovery.DEFAULT_DISCOVERY_PORT == server_side.DEFAULT_DISCOVERY_PORT


def test_签名文本两边逐字节一致() -> None:
    """签名对象是**字节**，两边差一个空格就永远验不过。"""
    from syncoj_server.services import discovery as server_side

    nonce = "a" * 32
    url = "http://10.0.0.5:8000"
    assert discovery.canonical_text(nonce, url) == server_side.canonical_text(nonce, url)


def test_服务端能解析我们造的探测() -> None:
    """报文格式是两边的接口，从**对方**的解析器过一遍才算真的对上了。"""
    from syncoj_server.services import discovery as server_side

    raw = discovery.build_probe("b" * 32, machine_id="m1")
    parsed = server_side.parse_probe(raw)

    assert parsed is not None
    assert parsed["nonce"] == "b" * 32
    assert parsed["machine_id"] == "m1"
