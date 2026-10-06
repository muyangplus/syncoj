"""局域网里找服务端：喊一声，验过签的那一句才认。

## 在哪用得上

装机时（``install.py``）和运行期（连不上服务端时）各用一次。它不是主路径 ——
主路径是**离线包里已经内嵌了服务端地址**（那台服务端自己打的包，当然知道自己的
地址）。这里解决的是另外两种情况：包是别处打的、或者服务端换了 IP（DHCP 很常见，
而"已经装好的 50 台一起失联"是最难受的一种故障）。

## 为什么必须验签

找到服务端之后做的第一件事是把**统一注册密钥**发过去。所以一个不验签的应答等于
把"能注册整间机房"的密钥交给局域网里任何一个应答者。统一注册密钥本身也挡不住
（它是这间机房所有机器共享的，任何一台机器都持有它），所以只能用**服务端的发布
私钥**签、用包里内嵌的那把公钥验 —— 这套密钥本来就在，不需要新增任何东西。

没有公钥就**不发现**（与"没有密钥就不升级"同一个默认）：宁可让人去手填一个地址，
也不要让机器去信一个没人签过的应答。

## 一次探测只认一个答案

同一个局域网里可能同时跑着两台服务端（两个考场共用一层楼）。那时**不能猜**：
猜错的表现是"代码交上去了但成绩是空的"，而且现场看不出来。多个不同的、都验过签
的地址 = 歧义，如实报出来让人去手填。
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from .rsa import RSAPublicKey, SignatureError, verify_pkcs1v15_sha256

__all__ = [
    "DEFAULT_DISCOVERY_PORT",
    "DiscoveryOutcome",
    "MAGIC",
    "MIN_PROBE_BYTES",
    "PROTOCOL_VERSION",
    "broadcast_targets",
    "canonical_text",
    "discover",
]

log = logging.getLogger(__name__)

#: 协议常量。**必须与服务端 ``services/discovery.py`` 里的同名常量一致**：
#: 版本对不上服务端就不吭声，而现象是"完全没人应答"，很难查。有一条测试盯着。
MAGIC = "syncoj"
PROTOCOL_VERSION = 1
MIN_PROBE_BYTES = 640
DEFAULT_DISCOVERY_PORT = 45871

#: 探测文本前缀，与服务端共用一个字面量（跨协议隔离）。
SIGNATURE_PREFIX = "syncoj-discovery-v1"

#: 等应答的时间。局域网往返是个位数毫秒，1.5 秒足够；再长就没人愿意等了。
DEFAULT_TIMEOUT_SECONDS = 1.5

#: 一次最多看几个应答。这只是个防"被应答淹没"的上界，不是答案数量。
MAX_REPLIES = 16


class DiscoveryOutcome(NamedTuple):
    """一次探测的结果。

    ``url`` 只有**恰好一个**验过签的答案时才非空。``candidates`` 用来解释为什么
    没定下来（有两个服务端 = 歧义；空 = 没人应答）。
    """

    url: Optional[str]
    candidates: List[str]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    def explain(self) -> str:
        if self.url:
            return "找到服务端：%s" % self.url
        if self.ambiguous:
            return "局域网里有多个服务端都回了应答（%s）—— 需要显式指定地址" % (
                "、".join(self.candidates)
            )
        return "局域网里没有服务端应答"


def canonical_text(nonce: str, url: str) -> str:
    """要被签名的那段文本。**与服务端逐字节一致**，所以两边各写一遍同样的规则。"""
    return "%s\n%s\n%s" % (SIGNATURE_PREFIX, nonce, url)


def build_probe(nonce: str, machine_id: str = "", min_bytes: int = MIN_PROBE_BYTES) -> bytes:
    """造探测报文，**带填充**凑到 ``min_bytes``。

    填充是协议的一部分，不是凑数：服务端立了一条"应答不得大于探测"的规矩来
    避免自己变成放大器，太短的探测它直接不理。
    """
    payload: Dict[str, object] = {
        "syncoj": MAGIC,
        "v": PROTOCOL_VERSION,
        "nonce": nonce,
        "machine_id": machine_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) < min_bytes:
        payload["pad"] = "0" * max(0, min_bytes - len(raw) - 12)
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    while len(raw) < min_bytes:
        payload["pad"] = str(payload.get("pad", "")) + "0" * (min_bytes - len(raw))
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return raw


def _decode_signature(text: str) -> Optional[bytes]:
    """base64url 解码，容忍缺填充与标准字母表。

    服务端用 urlsafe 且去掉 ``=``。这里刻意宽容一点：解不出来只是这一个应答不算，
    不该让整个发现过程崩掉。
    """
    import base64
    import binascii

    cleaned = (text or "").strip().replace("+", "-").replace("/", "_")
    if not cleaned:
        return None
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError):
        return None


def broadcast_targets() -> List[str]:
    """往哪些地址喊。

    永远包含受限广播 ``255.255.255.255``。此外**在 Linux 上**从 ``/proc/net/route``
    读出各接口的子网广播地址 —— 有些考场的内网没有默认路由，受限广播就不一定能
    出得去；而定向广播（``10.0.0.255``）是照着路由表算的，更实在。

    ``/proc/net/route`` 读不到（Windows、或者被限制）时**静默跳过**：这不是错误，
    受限广播在那些平台上本来就够用。
    """
    targets = ["255.255.255.255"]
    try:
        with open("/proc/net/route", "r", encoding="ascii") as handle:
            lines = handle.read().splitlines()[1:]
    except OSError:
        return targets

    for line in lines:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            # 这两列是**小端**十六进制（内核按内存里的字节序直接打印）
            destination = int(fields[1], 16)
            mask = int(fields[7], 16)
        except ValueError:
            continue
        if mask == 0:
            continue  # 默认路由：它的"广播地址"没有意义
        network = destination & mask
        broadcast = network | (~mask & 0xFFFFFFFF)
        target = socket.inet_ntoa(broadcast.to_bytes(4, "big"))
        if target not in targets:
            targets.append(target)
    return targets


def _parse_reply(raw: bytes, nonce: str, public_key: RSAPublicKey) -> Optional[str]:
    """验一个应答；通过就返回它宣称的地址，否则 ``None``。

    四道都要过：能解析、随机数对得上、地址像样、**签名验得过**。少任何一道，
    这个应答都不能用来决定"把注册密钥发给谁"。
    """
    if len(raw) > 4096:  # pragma: no cover - 防御性
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("syncoj") != MAGIC:
        return None
    if payload.get("v") != PROTOCOL_VERSION:
        return None
    if payload.get("nonce") != nonce:
        # 重放：别人录下上一次的应答原样发过来，随机数就对不上了
        log.debug("发现应答的随机数对不上，忽略（可能是重放）")
        return None
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    signature = _decode_signature(str(payload.get("sig") or ""))
    if signature is None:
        return None
    if not verify_pkcs1v15_sha256(public_key, canonical_text(nonce, url).encode("utf-8"), signature):
        # 这是整套东西存在的理由：局域网里任何人想冒充服务端，都签不出这一段
        log.warning("发现应答的签名验不过，忽略：%s", url)
        return None
    return url.rstrip("/")


def discover(
    public_key: Optional[RSAPublicKey],
    *,
    port: int = DEFAULT_DISCOVERY_PORT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    machine_id: str = "",
    targets: Optional[List[str]] = None,
) -> DiscoveryOutcome:
    """在局域网里找服务端。**不抛异常**，找不到就返回空结果。

    ``public_key`` 为 ``None``（机器上没装发布公钥）时直接返回空 —— 没有公钥就
    无法分辨"服务端"和"局域网里随便一个应答者"，那就不猜。

    ``targets`` 默认按 :func:`broadcast_targets` 广播。可以显式指定地址：有些
    交换机禁广播，那时教师至少能填一个地址让它直接问；测试也靠它指到 127.0.0.1
    （广播是发不到本机回环的）。
    """
    if public_key is None:
        log.info("没有发布公钥，跳过局域网发现（无法验证应答来源）")
        return DiscoveryOutcome(None, [])

    nonce = secrets.token_hex(16)
    probe = build_probe(nonce, machine_id=machine_id)
    destinations = list(targets) if targets else broadcast_targets()
    seen: Dict[str, int] = {}
    deadline = time.monotonic() + max(0.05, timeout)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sent = 0
        for target in destinations:
            try:
                sock.sendto(probe, (target, port))
                sent += 1
            except OSError as exc:
                # 某个网段的广播发不出去很正常（没有到那个网段的路由），
                # 继续试下一个；全都发不出去才需要报出来。
                log.debug("向 %s 发探测失败：%s", target, exc)
        if not sent:
            log.warning(
                "局域网发现：没有一个地址能发出去（试过 %s）", ", ".join(destinations)
            )
            return DiscoveryOutcome(None, [])

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(seen) >= MAX_REPLIES:
                break
            sock.settimeout(remaining)
            try:
                raw, peer = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError as exc:  # pragma: no cover - 网卡没了之类
                log.warning("局域网发现：收应答失败：%s", exc)
                break
            url = _parse_reply(raw, nonce, public_key)
            if url:
                seen[url] = seen.get(url, 0) + 1
    finally:
        sock.close()

    candidates = sorted(seen)
    if len(candidates) == 1:
        return DiscoveryOutcome(candidates[0], candidates)
    return DiscoveryOutcome(None, candidates)


def load_public_key(path) -> Optional[RSAPublicKey]:
    """读发布公钥；读不出来返回 ``None``（调用方按"不发现"处理）。

    与自更新用的是**同一把**公钥、同一个文件。刻意不在这里区分"文件不存在"和
    "内容不合法"：两种情况的结果都是"不发现"，而调用方该做的事也一样
    （照旧手填地址，或者去把密钥配好）。
    """
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return RSAPublicKey.from_dict(json.loads(text))
    except (ValueError, SignatureError):
        return None


def machine_id_hint() -> str:
    """探测里带的主机名，纯为服务端日志好读。读不到就空着。"""
    try:
        return socket.gethostname()[:64]
    except OSError:  # pragma: no cover
        return ""
