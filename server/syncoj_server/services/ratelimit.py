"""注册接口的限速。

为什么需要它
------------
``/agent/enroll`` 原来的威胁模型是"一码一人"：注册码有 16 字节熵、绑定机器，
爆破没有意义。换成统一密钥之后，这个接口变成**一把钥匙开整间机房** ——
攻击者可以拿它无限注册，也可以在待认领列表里塞满垃圾机器。

两层限速：

* **单 IP** —— 挡住同一台机器上的脚本刷注册
* **全局** —— 挡住"从很多 IP 一起刷"。考场内网 IP 数量有限，全局上限是兜底

为什么不用令牌桶
----------------
这里要的是"别被打爆"，不是"精确控速"。固定窗口计数足够，而且**每组状态只是一个
整数对** —— 令牌桶要为每个 key 保留一个桶结构，那本身就是内存放大攻击面：
攻击者伪造源 IP 就能让表无限增长。

清理策略同理：表大了就按窗口时间整批丢弃过期项，而不是逐个精确回收。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional, Tuple

__all__ = ["RateLimiter", "RateLimitExceeded"]

log = logging.getLogger(__name__)

#: 表超过这个规模就顺手清理。正常考场不会有这么多来源 IP。
_PRUNE_THRESHOLD = 4096

#: 窗口长度（秒）。固定窗口，不做滑动。
_WINDOW = 1.0


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int, detail: str) -> None:
        super().__init__(detail)
        self.retry_after = max(1, int(retry_after))
        self.detail = detail


@dataclass
class RateLimiter:
    """固定窗口计数器。

    ``limit <= 0`` 表示不限速（大考场巡检、压测时用得上）。
    """

    #: 每个窗口允许的次数
    limit: int
    #: 每个 key 的记录：``(窗口起点, 计数)``
    _buckets: Dict[str, Tuple[float, int]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def check(self, key: str, now: float) -> None:
        """记一次请求，超限就抛 ``RateLimitExceeded``。"""
        if self.limit <= 0:
            return

        with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            if now - start >= _WINDOW:
                start, count = now, 0
            count += 1
            self._buckets[key] = (start, count)
            if len(self._buckets) > _PRUNE_THRESHOLD:
                self._prune(now, keep_key=key)
            exceeded = count > self.limit

        if exceeded:
            raise RateLimitExceeded(retry_after=1, detail="请求过于频繁，请稍后再试")

    def _prune(self, now: float, keep_key: Optional[str] = None) -> None:
        """丢掉窗口已经过期的记录。调用方必须持锁。"""
        self._buckets = {
            key: value
            for key, value in self._buckets.items()
            if now - value[0] < _WINDOW or key == keep_key
        }

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
