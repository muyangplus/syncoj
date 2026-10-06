"""局域网发现：考试机喊一声，服务端回一句"我在这儿，地址是这个"。

## 为什么需要它

装机时每台机器都手填服务端地址，是"一份镜像装 50 台"这件事上唯一没被消掉的
一步。而且它有两种坏法：填错一个数字（现象是"注册不上"），或者服务端换了 IP
（DHCP 很常见）之后**已经装好的 50 台一起失联**。

所以这一层有两个入口，而且主次分明：

1. **离线包里内嵌地址**（主路径）—— 包是服务端自己打的，地址是它自己知道的，
   装机时不用任何人输入。见 :mod:`syncoj_server.services.packaging`。
2. **UDP 广播发现**（兜底）—— 包里没带地址、或者服务端换了 IP 时，机器喊一声。

## 为什么必须签名

机器"找到服务端"之后要做的事是**把统一注册密钥发过去**。如果应答不验签，
局域网里任何一台机器都能抢先应答、把别的机器引到一台假服务端上、顺手收走那把
"能注册整间机房"的密钥 —— 而那个密钥是 **50 台机器共享**的，所以拿它做 HMAC
也挡不住（任何一台机器都持有它）。

因此：应答用**发布签名私钥**签「随机数 + 地址」，机器用包里已经内嵌的那把公钥
验签。这套密钥本来就在（见 :mod:`syncoj_server.keys`），不需要新增任何东西。
没有私钥就不应答，机器那边也就照旧手填地址 —— 与"没有密钥就不升级"同一个默认。

## 报文

探测（机器 → 广播，UDP）::

    {"syncoj": 1, "nonce": "<32 位十六进制>", "machine_id": "...", "pad": "<填充>"}

应答（服务端 → 单播回来源地址）::

    {"syncoj": 1, "nonce": "<原样带回>", "url": "http://10.0.0.5:8000",
     "key_id": "...", "sig": "<base64url，无填充>"}

签名对象是一段**规范化文本**，而不是 JSON —— 两边各自 :func:`json.dumps` 出来的
字节可能不一样（键序、空格、转义），而签名是逐字节比的::

    syncoj-discovery-v1\\n<nonce>\\n<url>

版本前缀的作用是**跨协议隔离**：将来若有另一处也用这把私钥签东西，两边的签名
对象不会长得一样，一个签名就不能被搬到另一个场景里用。随机数是防重放的：
机器每次探测现生成，应答里带不回那个数就不认。

## 应答不得大于探测

这是一个**无状态 UDP 反射点**：任何人把来源地址伪造成受害者的地址、往这个端口
发一个探测，服务端就会替他向受害者发一份应答。所以当应答比探测还大时，它就是
一个放大器。规矩很短：**探测报文有最小长度（默认 320 字节，靠 ``pad`` 凑），
比自己短的探测一律不应答。** 于是 1 字节的请求最多换回 1 字节，放大不了。
另加一条按来源 IP 的限速，挡住"一个来源反复刷"。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import socket
import time
from typing import Dict, Optional, Tuple

from ..context import AppContext

log = logging.getLogger("syncoj")

__all__ = [
    "MAGIC",
    "MIN_PROBE_BYTES",
    "PROTOCOL_VERSION",
    "canonical_text",
    "build_probe",
    "build_reply",
    "describe_advertised_url",
    "run_discovery_responder",
    "local_address_for",
    "parse_probe",
    "ReplySigner",
]

#: 探测与应答里的版本字段。**两边都对不上就不吭声** —— 宁可让人去手填地址，
#: 也不要让新老版本互相说半句话。
MAGIC = "syncoj"
PROTOCOL_VERSION = 1

#: 规范化签名串的前缀（跨协议隔离，见模块说明）。
SIGNATURE_PREFIX = "syncoj-discovery-v1"

#: 探测报文的默认长度，也是服务端接受的最小长度。
#:
#: 它**不是随便定的**：应答里最大的一块是 RSA-2048 签名（344 字符 base64url），
#: 加上 JSON 骨架与地址，一条应答大约 480 字节。探测必须比它更长，"应答不得大于
#: 探测"这条约束才有余量留给稍微长一点的地址（域名 + 端口）。640 留给地址约
#: 130 字节，够长；地址再长就**不应答**，而不是打破约束。
MIN_PROBE_BYTES = 640

#: 一次 ``recvfrom`` 的最大长度。探测报文本身不长（几百字节），给 2 KB 足够，
#: 同时避免有人拿一个超大 UDP 包来占内存。
MAX_DATAGRAM_BYTES = 2048

#: 发现端口的默认值。**和 Settings 里的默认值必须一致** —— 那里是给部署用的
#: 覆盖入口，这里是协议常量。两边不一致的表现是"完全没人应答"，很难查，
#: 所以有一条测试盯着它们相等。
DEFAULT_DISCOVERY_PORT = 45871


def canonical_text(nonce: str, url: str) -> str:
    """要被签名的那段文本。**两边必须逐字节一致**，所以它只有这一处定义。"""
    return "%s\n%s\n%s" % (SIGNATURE_PREFIX, nonce, url)


def _b64(raw: bytes) -> str:
    """base64url、去掉填充。与发布包的签名用同一种编码。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_probe(
    nonce: Optional[str] = None,
    machine_id: str = "",
    min_bytes: int = MIN_PROBE_BYTES,
) -> bytes:
    """造一个探测报文。**带填充**，让它不短于服务端要求的最小长度。

    填充不是装饰：它是"应答不得大于探测"那条约束的客户端一半（见模块说明）。
    """
    if nonce is None:
        nonce = secrets.token_hex(16)
    payload = {
        "syncoj": MAGIC,
        "v": PROTOCOL_VERSION,
        "nonce": nonce,
        "machine_id": machine_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) >= min_bytes:
        return raw
    # 填充放在一个独立字段里，而不是往后面贴垃圾字节：贴垃圾会让 JSON 解析
    # 失败，而"解析失败的探测"正是我们要能识别并丢掉的东西。
    payload["pad"] = "0" * (min_bytes - len(raw) - 12)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # 上面的估算可能差几个字节（JSON 转义、长度位数），补到位为止
    while len(raw) < min_bytes:
        payload["pad"] += "0" * (min_bytes - len(raw))
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return raw


def parse_probe(raw: bytes) -> Optional[Dict[str, object]]:
    """解析探测报文；不是我们的探测就返回 ``None``。

    故意**很严格**：字段不全、版本不对、随机数长度不对，一律当没听见。
    这个端口对对全网开放，宽进严出只会给自己找事。
    """
    if len(raw) < 2 or raw[:1] != b"{":
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("syncoj") != MAGIC or payload.get("v") != PROTOCOL_VERSION:
        return None
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 32:
        return None
    if any(char not in "0123456789abcdef" for char in nonce):
        return None
    return payload


def build_reply(nonce: str, url: str, signer, max_bytes: int) -> Optional[bytes]:
    """造应答。**超长就返回 ``None``**（宁可不答，也不要当放大器）。

    ``signer`` 是 :class:`ReplySigner`（鸭子类型即可：要有一个 ``sign(bytes)``）。
    """
    signature = _b64(signer.sign(canonical_text(nonce, url).encode("utf-8")))
    payload = {
        "syncoj": MAGIC,
        "v": PROTOCOL_VERSION,
        "nonce": nonce,
        "url": url,
        "key_id": getattr(signer, "key_id", None) or "",
        "sig": signature,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > max_bytes:
        # 地址特别长（比如带 IPv6 或一长串域名）时可能超；这种情况下不答，
        # 因为"答了"就意味着违反放大约束 —— 而探测那边还有个 pad 字段，
        # 真正该做的是把最小长度调大，不是绕过约束。
        log.warning(
            "发现应答 %d 字节超过探测 %d 字节，忽略这次探测", len(raw), max_bytes
        )
        return None
    return raw


class ReplySigner:
    """给应答签名的东西。**没有私钥时它不存在** —— 应答器因此直接不启动。"""

    def __init__(self, signing_key) -> None:
        self._key = signing_key

    @property
    def key_id(self) -> str:
        return getattr(self._key, "key_id", "") or ""

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


def local_address_for(peer_ip: str, port: int = 9) -> Optional[str]:
    """从本机到 ``peer_ip`` 会走哪个本机地址。

    用一个连出去的 UDP socket 问内核，而不是去枚举网卡：多网卡（有线 + 无线 +
    几个虚拟网卡）的服务端上，"枚举出来的第一个地址"经常不是能到这台机器的那个。
    UDP 的 ``connect`` 不发任何包，只做一次路由查找。

    这也让**每台机器拿到的是从它那一侧可达的地址** —— 多宿主服务端不用为此做
    任何配置。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer_ip, port))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def describe_advertised_url(ctx: AppContext, peer_ip: Optional[str] = None) -> Optional[str]:
    """这台服务端该对外说自己是哪个地址；说不出来就 ``None``。

    三条来源，**按可信度**排，不按方便程度排：

    1. **显式配置**（``SYNCOJ_PUBLIC_URL`` / ``serve --public-url``）—— 操作员说的
       话最权威，也最不容易出错。
    2. **从请求来源反推的本机地址**（内核告诉我们的路由）—— 这条是**客户端伪造
       不了**的，而且多网卡时会给出"从那一侧真的可达"的那个地址。
    3. **"教师浏览器用过的那个非回环 Host"**—— 那是一条真人证据（有人真的从这个
       地址成功打开过界面），但它是 **HTTP 头，客户端可以随便写**。所以它排在
       推导之后，而且只有**成功**的请求才会被记下来（见 ``main.py``）。

    三条都没有就返回 ``None``：**宁可不吭声，也不要退回 127.0.0.1** ——
    那会让 50 台机器各自连自己，而现场只看到"注册不上"。
    """
    configured = (ctx.settings.public_url or "").strip()
    if configured:
        return configured.rstrip("/")

    if peer_ip:
        local = local_address_for(peer_ip)
        # 推导出来的回环地址是**没用**的：那说明这台服务端和请求方在同一台机器上
        # （或者中间隔着本机的反向代理），把 127.0.0.1 发给考试机等于让它们连自己。
        if local and not local.startswith("127."):
            return "http://%s:%d" % (local, ctx.settings.http_port)

    observed = (getattr(ctx, "public_url_hint", None) or "").strip()
    if observed:
        return observed.rstrip("/")
    return None


def _remember_peer(seen: Dict[str, Tuple[float, int]], peer_ip: str, limit: int, now: float):
    """按来源 IP 限速。返回是否允许应答。

    ``limit <= 0`` 表示不限速（测试里用得到，生产上别这么配）。
    """
    if limit <= 0:
        return True
    window, count = seen.get(peer_ip, (now, 0))
    if now - window >= 1.0:
        seen[peer_ip] = (now, 1)
        return True
    if count >= limit:
        return False
    seen[peer_ip] = (window, count + 1)
    return True


async def run_discovery_responder(ctx: AppContext) -> None:
    """应答局域网里的发现探测。由 lifespan 当成后台任务拉起。

    起不来的原因只有两种，都**不是**错误：端口被占（同一台机器上跑了第二个
    实例）、或者这台服务端根本不知道自己的对外地址。两种情况下都只是没有发现
    能力，注册、收代码、升级全都不受影响 —— 所以日志写清楚、然后安静退出。
    """
    settings = ctx.settings
    if not settings.discovery_enabled:
        log.info("局域网发现已关闭（SYNCOJ_DISCOVERY=0）")
        return
    if ctx.signing_key is None:
        # 没有私钥就没法签，也就没法防"谁都能冒充服务端" —— 那就不要开着这个端口。
        log.info(
            "局域网发现未启用：没有发布签名私钥，无法签发应答（%s）",
            ctx.signing_key_error or "未配置",
        )
        return

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", settings.discovery_port))
    except OSError as exc:
        log.warning(
            "局域网发现未启用：UDP %d 绑定失败（%s）—— 多半是同一台机器上已经跑了一个实例",
            settings.discovery_port,
            exc,
        )
        sock.close()
        return

    sock.setblocking(False)
    signer = ReplySigner(ctx.signing_key)
    seen: Dict[str, Tuple[float, int]] = {}
    log.info("局域网发现已启用：UDP %d 等待探测", settings.discovery_port)

    try:
        while True:
            try:
                raw, peer = sock.recvfrom(MAX_DATAGRAM_BYTES)
            except (BlockingIOError, InterruptedError):
                await asyncio.sleep(0.2)
                continue
            except OSError as exc:  # pragma: no cover - 网卡没了之类
                log.warning("发现端口读取失败：%s", exc)
                await asyncio.sleep(0.5)
                continue

            # 会占用一点事件循环，但每一步都是微秒级：解析几十字节的 JSON、
            # 一次限速查表、最多一次 RSA 签名。放进线程池反而多一层复杂度。
            await loop.run_in_executor(None, _answer_one, ctx, sock, signer, seen, raw, peer)
    finally:
        sock.close()


def _answer_one(ctx, sock, signer, seen, raw: bytes, peer) -> None:
    """处理一个探测。抽成同步函数，好让它在线程池里跑（签名是 CPU 活）。"""
    settings = ctx.settings
    if len(raw) < settings.discovery_min_probe_bytes:
        # 太短的探测一律不理：这一条就是"应答不得大于探测"的执行方式
        log.debug("忽略过短的发现探测：%d 字节", len(raw))
        return
    probe = parse_probe(raw)
    if probe is None:
        return
    peer_ip = peer[0]
    if not _remember_peer(seen, peer_ip, settings.discovery_replies_per_second, time.monotonic()):
        log.debug("发现探测过于频繁，忽略：%s", peer_ip)
        return

    url = describe_advertised_url(ctx, peer_ip)
    if url is None:
        # 连自己对外是什么地址都不知道，就该闭嘴。回一个 127.0.0.1 是最坏的：
        # 机器会去连它自己，而现场只看到"注册不上"。
        log.debug("无法确定对外地址，忽略来自 %s 的探测", peer_ip)
        return

    reply = build_reply(str(probe["nonce"]), url, signer, max_bytes=len(raw))
    if reply is None:
        return
    try:
        sock.sendto(reply, peer)
    except OSError as exc:  # pragma: no cover
        log.debug("发现应答发送失败：%s", exc)
