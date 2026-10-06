"""局域网发现：协议、签名、以及"别把自己变成放大器"。

这个功能的两个真正风险都不在"能不能找到"，而在别处：

* **谁能应答**。机器找到服务端之后第一件事是把统一注册密钥发过去，所以一个不
  验签的应答等于把"能注册整间机房"的密钥交给局域网里任何一个应答者。这一半是
  密码学的 —— 这里用**服务端的发布私钥**签，机器用包里内嵌的公钥验。
* **这个 UDP 端口本身**。它是个无状态反射点：伪造来源地址就能让别人替我们收
  应答。所以定了"应答不得大于探测"这条硬约束，这里盯着它。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncoj_server.config import Settings
from syncoj_server.services import discovery
from syncoj_server.services.signing import generate_keypair, openssl_available

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "agent"))

from syncoj_agent.rsa import RSAPublicKey, verify_pkcs1v15_sha256  # noqa: E402

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")


@pytest.fixture()
def signer(tmp_path: Path):
    """一把真的服务端发布密钥 —— 用它签、再用 Agent 的验签代码验。"""
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    key = generate_keypair(tmp_path / "release-key.pem", bits=2048)
    return discovery.ReplySigner(key)


@pytest.fixture()
def ctx(tmp_path: Path) -> SimpleNamespace:
    """够用的假 AppContext：应答器只用到 settings / signing_key / public_url_hint。"""
    settings = Settings()
    settings.data_root = tmp_path / "data"
    settings.discovery_enabled = True
    settings.discovery_port = 0  # 由具体用例覆盖
    return SimpleNamespace(
        settings=settings, signing_key=None, signing_key_error=None, public_url_hint=None
    )


# --------------------------------------------------------------------------- #
# 两个"必须一致"的常量
# --------------------------------------------------------------------------- #


def test_默认端口在协议与服务端配置里必须一致() -> None:
    """两边不一致的表现是"完全没人应答" —— 没有任何线索能指向端口号。

    所以它是一个协议常量，而不是"某处配置的默认值"。
    """
    assert discovery.DEFAULT_DISCOVERY_PORT == Settings().discovery_port


def test_协议版本与包内_server_json_格式版本同号() -> None:
    """发现协议和包内 ``server.json`` 是同一件事的两种传法。

    有一天其中一边要改格式时，这个断言会提醒人：两边得一起想，而不是各改各的。
    """
    sys.path.insert(0, str(REPO_ROOT / "agent" / "packaging"))
    import build_bundle

    assert build_bundle.SERVER_URL_FORMAT == discovery.PROTOCOL_VERSION


# --------------------------------------------------------------------------- #
# 报文
# --------------------------------------------------------------------------- #


def test_探测够长_能被解析(ctx) -> None:
    """探测带填充，长度够服务端的最低要求 —— 否则服务端按"防放大"的规矩不理它。"""
    raw = discovery.build_probe(machine_id="m1")

    assert len(raw) >= ctx.settings.discovery_min_probe_bytes
    assert len(raw) <= discovery.MAX_DATAGRAM_BYTES
    parsed = discovery.parse_probe(raw)
    assert parsed is not None
    assert parsed["machine_id"] == "m1"
    assert len(parsed["nonce"]) == 32


def test_每次探测的随机数都不一样() -> None:
    """随机数是防重放的唯一依据，重复就等于重放窗口。"""
    nonces = {discovery.parse_probe(discovery.build_probe())["nonce"] for _ in range(20)}
    assert len(nonces) == 20


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x01\x02",
        b"not json at all",
        b"[]",
        b'{"syncoj":"someone-else","v":1,"nonce":"' + b"0" * 32 + b'"}',
        b'{"syncoj":"syncoj","v":99,"nonce":"' + b"0" * 32 + b'"}',
        b'{"syncoj":"syncoj","v":1}',                                   # 没有随机数
        b'{"syncoj":"syncoj","v":1,"nonce":"abc"}',                     # 太短
        b'{"syncoj":"syncoj","v":1,"nonce":"' + b"Z" * 32 + b'"}',      # 不是十六进制
    ],
)
def test_垃圾探测一律不理(raw: bytes) -> None:
    """这个端口对全网开放，宽进严出只会给自己找事。"""
    assert discovery.parse_probe(raw) is None


# --------------------------------------------------------------------------- #
# 防放大：应答不得大于探测
# --------------------------------------------------------------------------- #


def test_应答比探测小(signer) -> None:
    """这是那个 UDP 端口的底线：1 字节的请求最多换回 1 字节。

    伪造来源地址的人只能靠"小请求大应答"来借我们放大流量；只要应答永远不大于
    探测，他什么也借不到。
    """
    probe = discovery.build_probe()
    nonce = discovery.parse_probe(probe)["nonce"]

    reply = discovery.build_reply(nonce, "http://10.0.0.5:8000", signer, max_bytes=len(probe))

    assert reply is not None
    assert len(reply) < len(probe), "应答比探测还大就等于一个放大器"


def test_地址长得离谱时宁可不应答(signer) -> None:
    """超长就闭嘴，而不是答一个超大的包 —— 那正是要用规矩挡住的事。"""
    long_url = "http://" + ("a" * 2000) + ".example:8000"
    probe = discovery.build_probe()
    nonce = discovery.parse_probe(probe)["nonce"]

    assert discovery.build_reply(nonce, long_url, signer, max_bytes=len(probe)) is None


def test_太短的探测不被理(ctx, signer) -> None:
    """服务端一侧的执行点：短于下限的探测直接丢掉。"""
    calls = []
    ctx.signing_key = signer._key
    ctx.settings.discovery_min_probe_bytes = 320

    class FakeSock:
        def sendto(self, data, peer):  # pragma: no cover - 不该走到这里
            calls.append((data, peer))

    discovery._answer_one(
        ctx, FakeSock(), signer, {}, json.dumps({"syncoj": "syncoj", "v": 1, "nonce": "0" * 32}).encode(), ("10.0.0.9", 1)
    )

    assert calls == [], "短探测必须被丢掉"


# --------------------------------------------------------------------------- #
# 签名：谁能应答
# --------------------------------------------------------------------------- #


@requires_openssl
def test_应答的签名能用_Agent_的验签代码验通(ctx, signer) -> None:
    """签的是「随机数 + 地址」那段规范化文本，验的是**内嵌包里的那把公钥**。

    这条串起了整件事：服务端有私钥、机器有公钥、两边对"签对象是什么"的理解一致。
    """
    probe = discovery.build_probe()
    nonce = discovery.parse_probe(probe)["nonce"]
    reply = json.loads(
        discovery.build_reply(nonce, "http://10.0.0.5:8000", signer, max_bytes=4096)
    )

    public = RSAPublicKey.from_dict(signer._key.public_key_dict())
    signature = _b64decode(reply["sig"])

    # 注意这个 API 的契约是**返回 bool、从不抛异常**（见 rsa.py 的说明：
    # 返回 False 可能因为篡改、也可能因为格式非法，调用方不需要区分）
    assert verify_pkcs1v15_sha256(
        public,
        discovery.canonical_text(nonce, reply["url"]).encode("utf-8"),
        signature,
    ) is True


@requires_openssl
def test_改掉地址就验不过(ctx, signer) -> None:
    """**这条是鉴权的全部意义**：串改应答里的地址会立刻暴露。

    没有它，局域网里任何一台机器都能把别的机器引到一台假服务端上。
    """
    probe = discovery.build_probe()
    nonce = discovery.parse_probe(probe)["nonce"]
    reply = json.loads(
        discovery.build_reply(nonce, "http://10.0.0.5:8000", signer, max_bytes=4096)
    )
    forged = "http://10.0.0.66:8000"

    public = RSAPublicKey.from_dict(signer._key.public_key_dict())
    assert verify_pkcs1v15_sha256(
        public,
        discovery.canonical_text(nonce, forged).encode("utf-8"),
        _b64decode(reply["sig"]),
    ) is False


@requires_openssl
def test_另一个随机数上的签名不算数(ctx, signer) -> None:
    """这就是随机数的作用：录下上一次的应答、原样重放，验不过。"""
    first = discovery.build_probe()
    nonce_a = discovery.parse_probe(first)["nonce"]
    reply = json.loads(
        discovery.build_reply(nonce_a, "http://10.0.0.5:8000", signer, max_bytes=4096)
    )

    nonce_b = discovery.parse_probe(discovery.build_probe())["nonce"]
    assert nonce_a != nonce_b

    public = RSAPublicKey.from_dict(signer._key.public_key_dict())
    assert verify_pkcs1v15_sha256(
        public,
        discovery.canonical_text(nonce_b, reply["url"]).encode("utf-8"),
        _b64decode(reply["sig"]),
    ) is False


@requires_openssl
def test_别的私钥签的应答验不过(ctx, signer, tmp_path: Path) -> None:
    """别人自己生成一把密钥、签一个地址 —— 机器手里那把公钥不认它。"""
    other = discovery.ReplySigner(generate_keypair(tmp_path / "other.pem", bits=2048))
    probe = discovery.build_probe()
    nonce = discovery.parse_probe(probe)["nonce"]
    reply = json.loads(
        discovery.build_reply(nonce, "http://10.0.0.66:8000", other, max_bytes=4096)
    )

    public = RSAPublicKey.from_dict(signer._key.public_key_dict())
    assert verify_pkcs1v15_sha256(
        public,
        discovery.canonical_text(nonce, reply["url"]).encode("utf-8"),
        _b64decode(reply["sig"]),
    ) is False


@requires_openssl
def test_默认探测长度容得下真实域名(signer) -> None:
    """把"应答不得大于探测"从一个愿望变成一件**有余量**的事实。

    签名一个人就占 344 字符，所以留给地址的空间是有限的。这条拿一个像真的的地址
    （40 字符主机名 + 端口）算一遍，确保默认探测长度还容得下 —— 不然现场会表现成
    "服务端明明在，就是没人应答"，而完全看不出是长度问题。
    """
    url = "http://exam-server-3f7a19c2.room-201.local:8000"
    nonce = discovery.parse_probe(discovery.build_probe())["nonce"]

    reply = discovery.build_reply(nonce, url, signer, max_bytes=discovery.MIN_PROBE_BYTES)

    assert reply is not None
    assert len(reply) < discovery.MIN_PROBE_BYTES
    # 顺手把"还留了多少"钉住：将来改小 MIN_PROBE_BYTES 时这条会先红
    assert discovery.MIN_PROBE_BYTES - len(reply) > 40


def _b64decode(text: str) -> bytes:
    import base64

    cleaned = text.strip().replace("+", "-").replace("/", "_")
    return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))


# --------------------------------------------------------------------------- #
# 对外地址从哪来
# --------------------------------------------------------------------------- #


def test_显式配置最优先(ctx) -> None:
    ctx.settings.public_url = "http://10.0.0.5:8000/"
    ctx.public_url_hint = "http://1.2.3.4:9999"

    assert discovery.describe_advertised_url(ctx) == "http://10.0.0.5:8000"


def test_从来源反推优先于_host_提示(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """反推那条由内核给出，客户端伪造不了；Host 是 HTTP 头，谁都能写。

    所以一个**没人配过**的服务端上，考场上有人拿一个假 Host 打一下管理接口，
    也不该改变机器看到的地址。
    """
    ctx.public_url_hint = "http://attacker.example:8000"
    monkeypatch.setattr(discovery, "local_address_for", lambda peer, port=9: "10.0.0.5")

    assert discovery.describe_advertised_url(ctx, "10.0.0.9") == "http://10.0.0.5:8000"


def test_反推到回环就不用它(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """反推出 127.0.0.1 说明请求来自本机（或者隔着一层本机反代）——
    把那个地址发给考试机等于让它们连自己。"""
    ctx.settings.http_port = 8000
    ctx.public_url_hint = "http://10.0.0.5:8000"
    monkeypatch.setattr(discovery, "local_address_for", lambda peer, port=9: "127.0.0.1")

    assert discovery.describe_advertised_url(ctx, "127.0.0.1") == "http://10.0.0.5:8000"


def test_什么都没有时_宁可不吭声(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """退回 127.0.0.1 是最坏的失败方式：50 台机器各自找自己，而现象只是"注册不上"。"""
    monkeypatch.setattr(discovery, "local_address_for", lambda peer, port=9: None)

    assert discovery.describe_advertised_url(ctx, "10.0.0.9") is None
    assert discovery.describe_advertised_url(ctx) is None


def test_不知道地址时不应答(ctx, signer, monkeypatch: pytest.MonkeyPatch) -> None:
    """连自己对外是什么地址都不知道，就该闭嘴 —— 而不是回一个 127.0.0.1。"""
    monkeypatch.setattr(discovery, "local_address_for", lambda peer, port=9: None)
    sent = []

    class FakeSock:
        def sendto(self, data, peer):  # pragma: no cover
            sent.append(data)

    probe = discovery.build_probe()
    discovery._answer_one(
        ctx, FakeSock(), signer, {}, probe, ("10.0.0.9", 1)
    )

    assert sent == []


# --------------------------------------------------------------------------- #
# 应答器的启停
# --------------------------------------------------------------------------- #


def test_发现关掉时立即返回(ctx) -> None:
    ctx.settings.discovery_enabled = False

    asyncio.run(asyncio.wait_for(discovery.run_discovery_responder(ctx), timeout=1))


def test_没有私钥时不启动(ctx) -> None:
    """没有私钥就签不出应答，签不出应答就挡不住冒充 —— 那这个端口就不该开。

    与"没有密钥就不升级"是同一个默认：能力缺失时**关掉**，而不是降级成不安全。
    """
    ctx.signing_key = None

    asyncio.run(asyncio.wait_for(discovery.run_discovery_responder(ctx), timeout=1))


def test_端口被占时安静退出(ctx, signer) -> None:
    """同一台机器上跑第二个实例是很正常的事，不该因此起不来。"""
    ctx.signing_key = signer._key
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 必须占在**同一个地址**上（0.0.0.0），和应答器一样 —— 否则在 Windows 上
        # 占着 127.0.0.1:port 并不妨碍别人绑 0.0.0.0:port，这条就测了个寂寞
        # （第一版就是这么写的，实测在 Windows 上不冲突）。
        holder.bind(("0.0.0.0", 0))
        ctx.settings.discovery_port = holder.getsockname()[1]

        asyncio.run(asyncio.wait_for(discovery.run_discovery_responder(ctx), timeout=2))
    finally:
        holder.close()


def test_完整走一遍_探测进来应答出去(signer) -> None:
    """真的起一个应答器、真的发一个探测、真的收一个应答。

    前面那些都是分片验证（造报文、签名、验签），这一段证明它们**接得上**：
    端口绑得上、探测被认出来、签名串两边一致、应答回到了来源地址。
    """
    ctx = SimpleNamespace(
        settings=Settings(),
        signing_key=signer._key,
        signing_key_error=None,
        public_url_hint=None,
    )
    ctx.settings.discovery_enabled = True
    ctx.settings.public_url = "http://10.0.0.5:8000"
    ctx.settings.discovery_replies_per_second = 0  # 测速不限速
    # 端口用 0（内核分配），起完之后从任务里读不出来 —— 所以显式挑一个空闲端口
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_sock:
        probe_sock.bind(("127.0.0.1", 0))
        port = probe_sock.getsockname()[1]
    ctx.settings.discovery_port = port

    async def scenario():
        task = asyncio.ensure_future(discovery.run_discovery_responder(ctx))
        await asyncio.sleep(0.3)  # 让它绑好端口
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(3.0)
        try:
            raw = discovery.build_probe(machine_id="m-real")
            nonce = discovery.parse_probe(raw)["nonce"]
            client.sendto(raw, ("127.0.0.1", port))
            loop = asyncio.get_running_loop()
            reply_raw, _ = await loop.run_in_executor(None, client.recvfrom, 4096)
        finally:
            client.close()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return nonce, reply_raw

    nonce, reply_raw = asyncio.run(scenario())
    reply = json.loads(reply_raw.decode("utf-8"))

    assert reply["nonce"] == nonce
    assert reply["url"] == "http://10.0.0.5:8000"
    assert len(reply_raw) < len(discovery.build_probe())
    public = RSAPublicKey.from_dict(signer._key.public_key_dict())
    verify_pkcs1v15_sha256(
        public,
        discovery.canonical_text(nonce, reply["url"]).encode("utf-8"),
        _b64decode(reply["sig"]),
    )


def test_同一个来源会被限速(ctx, signer, monkeypatch: pytest.MonkeyPatch) -> None:
    """限速是挡"一个来源反复刷"的那一层。"""
    monkeypatch.setattr(discovery, "local_address_for", lambda peer, port=9: "10.0.0.5")
    ctx.settings.discovery_replies_per_second = 2
    sent = []

    class FakeSock:
        def sendto(self, data, peer):
            sent.append(data)

    probe = discovery.build_probe()
    # 限速状态是**跨调用**累积的（就是那个 seen 表），所以它得在循环外面 ——
    # 每次传一个新的空表等于根本没有记忆，测出来永远是"全部放行"。
    seen: dict = {}
    for _ in range(5):
        discovery._answer_one(ctx, FakeSock(), signer, seen, probe, ("10.0.0.9", 1))

    assert len(sent) == 2, "同一个来源一秒内只该应答 2 次"
