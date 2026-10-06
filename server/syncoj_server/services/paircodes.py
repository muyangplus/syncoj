"""配对码的明文缓存（**只在内存里**）。

为什么服务端必须留着明文
------------------------
协议规定未配对的机器**每一轮 tick 的响应里都要带上当前有效的配对码**
（见 ``docs/reference/protocol.md`` §6：心跳是未配对机器唯一稳定的上行通道 ——
它读不到 root 只读的统一密钥，也没有任何别的渠道能拿到码）。

而库里只存哈希，服务端自己也没法把一个哈希"还原"成六位数字。于是只有两种
做法：要么每轮重新发一个新码，要么把明文留着。前者看似更安全，实际上更糟 ——
教师刚在屏幕上读到的数字，六十秒后就被下一轮心跳换掉了，界面上只表现为
"配对码一直在跳"，没人会往心跳上想。**一个读不到、抄不对的码不是安全，是坏掉。**

为什么放内存而不是加一列
------------------------
配对码的有效期只有 30 分钟、一次性，而且**只有管理员**能用它（绑定接口
要 ``require_admin``）。把它落到库里意味着这个秘密在磁盘上一直留到进程清理
为止，换来的收益只是"服务端重启之后教师不用重读一次屏幕"。重启的代价是
重新生成一个码，成本几乎为零，所以不值得为它改动数据模型。

进程重启让所有在途的码作废是**可以接受**的：机器下一轮心跳会拿到一个新码，
而教师本来就要重新看一眼屏幕。真正不能接受的是服务端一重启，"配对"
这件事就永远办不成了 —— 那才是把安全做成了不可用。
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

__all__ = ["PairCodeCache"]


class PairCodeCache:
    """``agent_id`` → ``(明文配对码, 过期时刻)``。

    线程安全：uvicorn 的同步端点跑在线程池里，多个 tick 可能并发进来。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._codes: Dict[int, Tuple[str, object]] = {}

    def put(self, agent_id: int, code: str, expires_at) -> None:
        with self._lock:
            self._codes[int(agent_id)] = (code, expires_at)

    def get(self, agent_id: int, now) -> Optional[str]:
        """取还没过期的码；过期或没记录都返回 ``None``（顺手清掉）。"""
        key = int(agent_id)
        with self._lock:
            entry = self._codes.get(key)
            if entry is None:
                return None
            code, expires_at = entry
            if expires_at is not None and expires_at < now:
                # 过期的条目留着只会让人以为"还有码"，而它已经对不上了
                self._codes.pop(key, None)
                return None
            return code

    def forget(self, agent_id: int) -> None:
        """配对成功、机器被作废、或者被解绑时调用。

        解绑之后**必须**忘掉：那台机器马上要重新配一次，把旧码留在缓存里
        会让服务端继续回一个已经不该再用的数字。
        """
        with self._lock:
            self._codes.pop(int(agent_id), None)

    def clear(self) -> None:
        with self._lock:
            self._codes.clear()

    def __len__(self) -> int:  # pragma: no cover - 排错与测试用
        with self._lock:
            return len(self._codes)
