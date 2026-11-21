"""听服务端的主动通告（§11），核实之后再改用它说的地址。

服务端上线后在局域网里喊"我在这儿，地址是 X"。这里：

1. 一个守护线程在 UDP 端口上听，**输入一律不可信**：限长、只认那几个键、``key_id`` 不是
   本机信任锚的丢掉（隔壁考场）、同一个 ``boot_id`` 只处理一次、每分钟最多认真处理一次。
   它只往一个"候选地址"槽里放东西，什么都不连。
2. 主循环每轮开头看一眼槽：有候选就到那个地址上做**挑战应答**
   （``GET /api/v1/agent/whoami?nonce=…``，**不带凭据**），用发布公钥验签，切换到**签名里**
   的地址。

为什么不直接信通告：通告是服务端单方面发的，没有机器给的随机数，签了也挡不住重放
（录下一条真通告，等那个 IP 换了主人再放）。挑战应答让"那里此刻真的握着私钥"成为
切换的前提。

本模块只用标准库（Python 3.8）。签名格式与服务端 ``services/announce.py`` 对账。
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from .discovery import MAGIC, PROTOCOL_VERSION, _decode_signature
from .rsa import RSAPublicKey, verify_pkcs1v15_sha256

log = logging.getLogger(__name__)

DEFAULT_ANNOUNCE_PORT = 45872
ANNOUNCE_SIGNATURE_PREFIX = "syncoj-announce-v1"
MAX_ANNOUNCE_BYTES = 512
MAX_URL_LENGTH = 256
#: 两次"认真处理"之间至少隔多久（挡住有人在本机 / 局域网里刷伪造通告）
MIN_SECONDS_BETWEEN_CANDIDATES = 60.0
CHALLENGE_TIMEOUT_SECONDS = 5.0
MAX_CHALLENGE_REPLY_BYTES = 8192


def challenge_text(nonce: str, url: str) -> str:
    return "%s\n%s\n%s" % (ANNOUNCE_SIGNATURE_PREFIX, nonce, url)


def parse_announcement(raw: bytes) -> Optional[Tuple[str, str, str]]:
    """``(url, key_id, boot_id)``；不是合法通告就 ``None``。"""
    if len(raw) > MAX_ANNOUNCE_BYTES or raw[:1] != b"{":
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("syncoj") != MAGIC or payload.get("v") != PROTOCOL_VERSION:
        return None
    if payload.get("kind") != "announce":
        return None
    url, key_id, boot_id = payload.get("url"), payload.get("key_id"), payload.get("boot_id")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if len(url) > MAX_URL_LENGTH or any(ch in url for ch in " \r\n\t"):
        return None
    if not isinstance(key_id, str) or not isinstance(boot_id, str) or len(boot_id) > 64:
        return None
    return url.rstrip("/"), key_id, boot_id


class CandidateSlot:
    """监听线程与主循环之间唯一的共享状态：最新的一个候选地址。"""

    def __init__(self, expected_key_id: str = "") -> None:
        self.expected_key_id = expected_key_id
        self._lock = threading.Lock()
        self._candidate: Optional[str] = None
        self._seen_boot_ids = set()
        self._last_accepted = float("-inf")

    def offer(self, raw: bytes, now: Optional[float] = None) -> bool:
        """处理一个收到的报文；被采纳为候选就返回 True。"""
        parsed = parse_announcement(raw)
        if parsed is None:
            return False
        url, key_id, boot_id = parsed
        if self.expected_key_id and key_id != self.expected_key_id:
            return False  # 别的服务端（隔壁考场）
        now = time.monotonic() if now is None else now
        with self._lock:
            if boot_id in self._seen_boot_ids:
                return False
            if now - self._last_accepted < MIN_SECONDS_BETWEEN_CANDIDATES:
                return False
            if len(self._seen_boot_ids) > 64:
                self._seen_boot_ids.clear()
            self._seen_boot_ids.add(boot_id)
            self._last_accepted = now
            self._candidate = url
        return True

    def take(self) -> Optional[str]:
        with self._lock:
            candidate, self._candidate = self._candidate, None
            return candidate


class AnnounceListener(threading.Thread):
    """守护线程：在 UDP 端口上听通告。绑不上（端口被占）就安静退出。"""

    def __init__(self, slot: CandidateSlot, port: int = DEFAULT_ANNOUNCE_PORT) -> None:
        super().__init__(name="syncoj-announce", daemon=True)
        self.slot = slot
        self.port = port
        self._stop = threading.Event()
        self.bound = threading.Event()
        self.sock: Optional[socket.socket] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
        except OSError as exc:
            log.info("不听服务端通告：UDP %d 绑定失败（%s）；连不上时仍会自己去问", self.port, exc)
            sock.close()
            return
        sock.settimeout(1.0)
        self.sock = sock
        self.bound.set()
        try:
            while not self._stop.is_set():
                try:
                    raw, _peer = sock.recvfrom(MAX_ANNOUNCE_BYTES + 1)
                except socket.timeout:
                    continue
                except OSError:  # pragma: no cover
                    time.sleep(1.0)
                    continue
                if self.slot.offer(raw):
                    log.info("听到服务端通告，等主循环核实")
        finally:
            sock.close()


def verify_announced(
    url: str,
    public_key: RSAPublicKey,
    *,
    ssl_context: Optional[ssl.SSLContext] = None,
    timeout: float = CHALLENGE_TIMEOUT_SECONDS,
) -> Optional[str]:
    """到 ``url`` 上做挑战应答。验过签就返回**签名里**的地址，否则 ``None``。

    请求**不带凭据**：还没证明对面是谁之前，不把令牌交出去。
    """
    nonce = secrets.token_hex(16)
    target = "%s/api/v1/agent/whoami?%s" % (url.rstrip("/"), urllib.parse.urlencode({"nonce": nonce}))
    request = urllib.request.Request(target, headers={"Accept": "application/json"})
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl_context),
        )
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_CHALLENGE_REPLY_BYTES + 1)
    except (OSError, ValueError) as exc:
        log.info("核实通告失败（%s 连不上或拒绝）：%s", url, exc)
        return None
    if len(raw) > MAX_CHALLENGE_REPLY_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("nonce") != nonce:
        return None
    signed_url = payload.get("url")
    if not isinstance(signed_url, str) or not signed_url.startswith(("http://", "https://")):
        return None
    if len(signed_url) > MAX_URL_LENGTH:
        return None
    signature = _decode_signature(str(payload.get("sig") or ""))
    if signature is None:
        return None
    if not verify_pkcs1v15_sha256(public_key, challenge_text(nonce, signed_url).encode("utf-8"), signature):
        log.warning("通告里的地址 %s 签不出本机信任的签名，不切换", url)
        return None
    return signed_url.rstrip("/")
