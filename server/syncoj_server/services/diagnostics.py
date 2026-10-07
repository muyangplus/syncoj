"""诊断包（Agent → 服务端）的收件侧工具：**带上限地解压**与**按机器限速**。

为什么单独一个模块
------------------
"解压炸弹"这件事必须只有一个写法。上传端点与管理端读到的那一份内容都出自
这里：上传按同一套上限校验，管理端存的是校验过的文本（不再二次解压）。
两处各写一遍，迟早会出现"上传那道挡住了、另一道没有"这种只在被攻击的那天
才看得出来的差别。

为什么不能 `gzip.decompress` 再判长度
------------------------------------
``gzip.decompress`` 会**先把整个明文展开**再返回，等我们拿到它去量长度时内存
已经吃满了 —— 一个几十 KB 的 gzip 可以展开成几个 GB，进程当场被杀。所以这里
用 ``zlib.decompressobj(16 + zlib.MAX_WBITS)``（``16 + MAX_WBITS`` 就是"按
gzip 头解析"），并显式给 ``decompress(data, max_length)`` 一个上限：
最多产出 ``max_length + 1`` 个字节，剩下的留在 ``unconsumed_tail`` 里 ——
"超限"因此在**产出那一小段之后就知道了**，不需要先展开完。

两层上限都要有，理由不同
------------------------
* **压缩后 ≤ 256 KB**：Agent 那边本来就按这个数截（``diagnostics.encode_bundle``），
  再大说明对端不是我们认识的那个 Agent，直接拒掉，连解压都不做。
* **解压后 ≤ 1 MB**：真正的 zip bomb 防线。压缩比可以很大，只看压缩后大小
  等于没防。

限速为什么按机器（而不是按 IP / 全局）
--------------------------------------
诊断包是"顺便留个现场"的旁路请求，服务端只允 60 秒一份。按机器限最贴近语义：
同一台机器刷不出更多，而**别的机器完全不受影响**（考场机器共用出口 IP，
按 IP 限速会一台发完、全场被挡）。窗口放在内存里（``DiagnosticsRateLimiter``，
挂在 ``AppContext`` 上）：它只是"刚才是不是刚收过一份"的节流状态，重启后重置
的代价是"可能多收一份"，而不是丢东西。
"""

from __future__ import annotations

import json
import math
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .ratelimit import RateLimitExceeded

__all__ = [
    "MAX_COMPRESSED_BYTES",
    "MAX_DECOMPRESSED_BYTES",
    "MIN_INTERVAL_SECONDS",
    "MAX_REASON_LENGTH",
    "BundleRejected",
    "DecodedBundle",
    "DiagnosticsRateLimiter",
    "decode_bundle",
    "extract_reason",
]

#: 压缩后的上限（字节）。与 ``agent/syncoj_agent/diagnostics.MAX_BUNDLE_BYTES`` 同值：
#: Agent 自己发不出来的大小，服务端不该收。
MAX_COMPRESSED_BYTES = 256 * 1024

#: 解压后的上限（字节）。Agent 的完整诊断包（含 64 KB 日志尾部）只有几百 KB，
#: 1 MB 留了足够余量；越过它的一定不是"现场"，而是压缩炸弹或坏数据。
MAX_DECOMPRESSED_BYTES = 1024 * 1024

#: 同一台机器两次上报的最小间隔（秒）。Agent 自己也守着同一个数（周期 600 秒、
#: 补发最小间隔 60 秒），这里再守一遍是因为"报文来自网络"。
MIN_INTERVAL_SECONDS = 60.0

#: ``reason`` 落库前的长度上限。取值只有 ``periodic`` / ``error`` / ``manual``
#: 三个，但不逐字段校验（Agent 将来加字段不该被拒），所以只截断、不判取值。
MAX_REASON_LENGTH = 32


class BundleRejected(Exception):
    """诊断包不合法的**业务**结论。

    ``services/`` 不该 import FastAPI，所以这里只描述"是什么问题、该回什么
    状态码/错误码"，由 ``api/agent.py`` 翻成统一的 ``ApiError`` 错误体。
    """

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DecodedBundle:
    """一份通过全部校验的诊断包。

    ``content`` 是**解压后的 JSON 文本**（原样、未重新序列化），服务端存的
    就是它；``data`` 是解析出来的对象；``size`` 是 ``content`` 的 UTF-8 字节数。
    """

    content: str
    data: Dict[str, Any]
    size: int


def _reject(detail: str, code: str = "bad_request", status_code: int = 400) -> BundleRejected:
    return BundleRejected(status_code=status_code, code=code, detail=detail)


def decode_bundle(
    blob: bytes, max_bytes: int = MAX_DECOMPRESSED_BYTES
) -> DecodedBundle:
    """解压 → 解码 → 解析，任何一步不合规都抛 :class:`BundleRejected`。

    校验顺序是刻意的：**先量压缩后大小**（连 zlib 都不启动），再带上限解压，
    最后才 JSON 解析。上游端点会在调用这里之前先挡掉超大 body；这里再量一次
    是为了让函数本身可独立测试、也防止将来有人在别处直接用。
    """
    if len(blob) > MAX_COMPRESSED_BYTES:
        raise _reject(
            "诊断包压缩后超过 %d 字节" % MAX_COMPRESSED_BYTES,
            code="payload_too_large",
            status_code=413,
        )

    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = decompressor.decompress(blob, max_bytes + 1)
    except zlib.error as exc:
        raise _reject("诊断包不是合法的 gzip 数据（%s）" % exc)
    if decompressor.unconsumed_tail:
        # 解到上限还没解完 —— 解压后必然超限。此时**不** flush、不继续解。
        raise _reject(
            "诊断包解压后超过 %d 字节" % max_bytes,
            code="payload_too_large",
            status_code=413,
        )

    try:
        raw += decompressor.flush()
    except zlib.error as exc:  # pragma: no cover - 正常 gzip 不会在这抛
        raise _reject("诊断包不是合法的 gzip 数据（%s）" % exc)
    if len(raw) > max_bytes:
        raise _reject(
            "诊断包解压后超过 %d 字节" % max_bytes,
            code="payload_too_large",
            status_code=413,
        )
    if not decompressor.eof:
        # 数据在 gzip 成员结束前就没了：截断/半截传输
        raise _reject("诊断包不完整（gzip 数据被截断）")
    if decompressor.unused_data:
        # gzip 成员结束后还有多余字节。多成员 gzip 也走这一支 —— Agent 那边
        # 只会发单成员，认不出来的一律按坏数据处理，免得"后续成员"变成绕过
        # 上限的通道（每个成员都各自不超过 max_length，但加起来没有上限）。
        raise _reject("诊断包带了 gzip 成员之外的多余数据")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _reject("诊断包解压后不是合法的 UTF-8 文本")

    try:
        parsed = json.loads(text)
    except ValueError:
        raise _reject("诊断包解压后不是合法的 JSON")

    if not isinstance(parsed, dict):
        # 只校验"是对象"，**不逐字段校验**：Agent 将来加字段不该被服务端拒绝。
        raise _reject("诊断包必须是一个 JSON 对象")

    return DecodedBundle(content=text, data=parsed, size=len(raw))


def extract_reason(bundle: Dict[str, Any]) -> Optional[str]:
    """从诊断包里取 ``reason``。

    只做"非空字符串就截到 32 字"这一件事，不判取值 —— 服务端只负责存下对端
    自报的原因，前端对认识的那三个给人话，其余原样显示。
    """
    reason = bundle.get("reason")
    if isinstance(reason, str):
        reason = reason.strip()
        if reason:
            return reason[:MAX_REASON_LENGTH]
    return None


class DiagnosticsRateLimiter:
    """``agent_id -> 上一次收到诊断包的时刻``，固定 60 秒窗口，线程安全。

    用单调钟（``time.monotonic``）而不是墙上时间：机器/服务器对表这件事不该
    影响限速窗口。时钟可注入，测试里推进时间不用真等一分钟。
    """

    def __init__(
        self,
        window_seconds: float = MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._last: Dict[int, float] = {}

    def check(self, agent_id: int) -> None:
        """记一次"刚收到这台机器的诊断包"；窗口内再来就抛 ``RateLimitExceeded``。

        **被拒的那次不改写时刻**：固定窗口从"上一次真的收下"开始算，而不是
        "上一次尝试" —— 否则一直撞的机器会把窗口一直顶开，永远等不到。
        """
        if self.window_seconds <= 0:
            return
        now = self._clock()
        with self._lock:
            last = self._last.get(agent_id)
            if last is not None and now - last < self.window_seconds:
                remaining = self.window_seconds - (now - last)
                raise RateLimitExceeded(
                    int(math.ceil(remaining)),
                    "同一台机器的诊断包最多 %d 秒一份，请稍后再传"
                    % int(self.window_seconds),
                )
            self._last[agent_id] = now

    def reset(self) -> None:
        """清空所有窗口。给测试与"服务端重新开始计时"用。"""
        with self._lock:
            self._last.clear()

    def last_received_at(self, agent_id: int) -> Optional[float]:
        """这台机器上一次被收下的时刻（单调秒）；没有就是 ``None``。"""
        with self._lock:
            return self._last.get(agent_id)
