"""服务端主动通告（§11）：服务端在局域网里喊"我在这儿"，机器听到后自己来核实。

与 :mod:`.discovery` 方向相反：那边是**机器问、服务端答**（机器给随机数，应答签名）；
这边是**服务端单方面喊**。单方面发出的东西没有机会带上对方的随机数，签名再强也挡不住
**重放** —— 有人录下一条真通告，等那个 IP 被 DHCP 分给自己的机器后再放出来。所以：

* 通告**不签名**，只是一条线索：``{"syncoj", "v", "kind": "announce", "url", "key_id", "boot_id"}``；
* 机器听到后到通告里的地址上做**挑战应答**：``GET <url>/api/v1/agent/whoami?nonce=<机器现生成>``，
  服务端用发布私钥签 ``syncoj-announce-v1\\n<nonce>\\n<url>``，验过才切换（见 ``api/agent.py``）。

通告本身被伪造、被重放，都只会让机器多发一次 HTTP 请求。

**什么时候喊**：启动后前 10 分钟每 30 秒一次（考场里服务端通常先开、考试机陆续开机），
之后每 5 分钟一次；管理端「召集机器」随时立即喊一次。报文 < 200 字节、不需要应答，
没有放大面（机器的回应是一次 HTTP 请求，不是 UDP 回包）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import threading
import time
from typing import List, Optional

from typing import TYPE_CHECKING

from .discovery import MAGIC, PROTOCOL_VERSION, describe_advertised_url

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext

log = logging.getLogger(__name__)

#: 挑战应答签名对象的版本前缀：与发现（``syncoj-discovery-v1``）、发布签名都不同，
#: 一个场景的签名搬不到另一个场景里用。
ANNOUNCE_SIGNATURE_PREFIX = "syncoj-announce-v1"

#: 通告端口。与发现端口分开：发现端口是服务端在听，这个端口是**机器**在听。
DEFAULT_ANNOUNCE_PORT = 45872

#: 前 10 分钟每 30 秒一次，之后每 5 分钟一次
FAST_INTERVAL_SECONDS = 30.0
FAST_PERIOD_SECONDS = 600.0
SLOW_INTERVAL_SECONDS = 300.0

#: 机器那边收的上限（与 Agent 一致）；超过它的通告不发
MAX_ANNOUNCE_BYTES = 512


def challenge_text(nonce: str, url: str) -> str:
    """挑战应答要签的那段文本。**两边必须逐字节一致**。"""
    return "%s\n%s\n%s" % (ANNOUNCE_SIGNATURE_PREFIX, nonce, url)


def build_announcement(url: str, key_id: str, boot_id: str) -> Optional[bytes]:
    payload = {
        "syncoj": MAGIC,
        "v": PROTOCOL_VERSION,
        "kind": "announce",
        "url": url,
        "key_id": key_id,
        "boot_id": boot_id,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return raw if len(raw) <= MAX_ANNOUNCE_BYTES else None


def broadcast_targets(route_path: str = "/proc/net/route") -> List[str]:
    """受限广播 + Linux 上各接口的子网广播（读 ``/proc/net/route``）。

    与 Agent 的 ``discovery.broadcast_targets`` 同一个判据，但**各写一份**：服务端不 import
    Agent 的代码（见 ``services/packaging.py`` 的说明）。
    """
    targets = ["255.255.255.255"]
    try:
        with open(route_path, "r", encoding="ascii") as handle:
            lines = handle.read().splitlines()[1:]
    except OSError:
        return targets
    for line in lines:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            destination = int(fields[1], 16)
            mask = int(fields[7], 16)
        except ValueError:
            continue
        if mask == 0:
            continue
        broadcast = (destination & mask) | (~mask & 0xFFFFFFFF)
        # /proc/net/route 是小端
        target = socket.inet_ntoa(broadcast.to_bytes(4, "little"))
        if target not in targets and not target.startswith("127."):
            targets.append(target)
    return targets


class Announcer:
    """通告的状态：本次启动的 ``boot_id``、"立即喊一次"的请求、上一次喊的结果。

    管理端接口跑在线程池里，后台任务跑在事件循环里，所以"立即喊一次"用
    :class:`threading.Event` 传过去。
    """

    def __init__(self) -> None:
        self.boot_id = secrets.token_hex(8)
        self.started_at = time.monotonic()
        self._now = threading.Event()
        self.last_sent_at: Optional[float] = None
        self.last_url: Optional[str] = None
        self.last_error: Optional[str] = None

    def request_now(self) -> None:
        self._now.set()

    def take_request(self) -> bool:
        if self._now.is_set():
            self._now.clear()
            return True
        return False

    def interval(self, now: Optional[float] = None) -> float:
        elapsed = (now if now is not None else time.monotonic()) - self.started_at
        return FAST_INTERVAL_SECONDS if elapsed < FAST_PERIOD_SECONDS else SLOW_INTERVAL_SECONDS


def announce_once(ctx: "AppContext", announcer: Announcer, sock: socket.socket) -> bool:
    """喊一次。说不出自己的地址（§7.3 的三条来源都没有）就不喊，返回 False。"""
    url = describe_advertised_url(ctx)
    if url is None:
        announcer.last_error = "不知道自己的对外地址（设 SYNCOJ_PUBLIC_URL，或先用浏览器打开一次管理界面）"
        return False
    key_id = getattr(ctx.signing_key, "key_id", "") or ""
    raw = build_announcement(url, key_id, announcer.boot_id)
    if raw is None:
        announcer.last_error = "通告超长（地址太长）"
        return False
    sent = 0
    for target in broadcast_targets():
        try:
            sock.sendto(raw, (target, ctx.settings.announce_port))
            sent += 1
        except OSError as exc:
            log.debug("通告发往 %s 失败：%s", target, exc)
    if not sent:
        announcer.last_error = "所有广播地址都发送失败"
        return False
    announcer.last_sent_at = time.time()
    announcer.last_url = url
    announcer.last_error = None
    return True


async def run_announcer(ctx: "AppContext") -> None:
    """后台任务：按节奏喊，或者被「召集机器」叫醒立即喊。"""
    settings = ctx.settings
    announcer = ctx.announcer
    if not settings.announce_enabled:
        announcer.last_error = "已关闭（SYNCOJ_ANNOUNCE=0）"
        log.info("服务端主动通告已关闭（SYNCOJ_ANNOUNCE=0）")
        return
    if ctx.signing_key is None:
        # 机器那边要用发布公钥验挑战应答；没有私钥就签不出来，喊了也没人会信
        announcer.last_error = "没有发布签名私钥"
        log.info("服务端主动通告未启用：没有发布签名私钥")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    log.info("服务端主动通告已启用：UDP %d（boot_id=%s）", settings.announce_port, announcer.boot_id)
    try:
        next_at = time.monotonic()
        while True:
            now = time.monotonic()
            if announcer.take_request() or now >= next_at:
                if announce_once(ctx, announcer, sock):
                    log.debug("已通告 %s", announcer.last_url)
                else:
                    log.debug("这一轮没有通告：%s", announcer.last_error)
                next_at = time.monotonic() + announcer.interval()
            await asyncio.sleep(1.0)
    finally:
        sock.close()
