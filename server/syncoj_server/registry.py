"""在线状态的内存真身。

为什么状态放内存而不是每次 tick 写库
------------------------------------
50 台机器 × 每 20s 一次 tick = 2.5 次写/秒。听起来不多，但若每个 tick 都开事务，
SQLite 的单写锁会在 Web 查询与 Agent 上报之间来回争抢，且每次都要
``UPDATE agent_status SET ...`` 做一次 B 树定位。改成"内存持有 + 定时批量落库"
之后，写频率降到 **每 5 秒一个事务**，彻底退出争抢区。

内存 dict 是**权威读源**，``agent_status`` 表只是它的持久化快照（用于服务端重启
后 Web 不空白，以及历史回溯）。因此 Web 查询走内存，不查库。

线程安全
--------
FastAPI 的同步端点跑在线程池里，多个请求可能并发修改注册表。全部变更走
``threading.Lock``。临界区里只有 dict 操作，没有 IO，锁竞争可忽略。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .models import utcnow

__all__ = ["AgentRuntime", "AgentRegistry"]

log = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    """一台考试机的实时状态。"""

    agent_id: int
    player_id: int
    contest_id: int
    player_no: str
    player_name: Optional[str] = None
    contest_slug: str = ""

    machine_id: str = ""
    hostname: Optional[str] = None
    agent_version: Optional[str] = None
    scan_root: Optional[str] = None

    online: bool = False
    last_tick_at: Optional[datetime] = None
    last_seen_ip: Optional[str] = None
    file_count: int = 0
    disk_free: Optional[int] = None
    last_error: Optional[str] = None

    #: 需要被批量落库
    dirty: bool = True
    #: 供 Web 展示的累计 tick 次数
    tick_count: int = 0

    def seconds_since_tick(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.last_tick_at is None:
            return None
        return ((now or utcnow()) - self.last_tick_at).total_seconds()

    def to_public_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "player_id": self.player_id,
            "contest_id": self.contest_id,
            "contest_slug": self.contest_slug,
            "player_no": self.player_no,
            "player_name": self.player_name,
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "agent_version": self.agent_version,
            "scan_root": self.scan_root,
            "online": self.online,
            "last_tick_at": self.last_tick_at.isoformat() + "Z" if self.last_tick_at else None,
            "seconds_since_tick": self.seconds_since_tick(),
            "last_seen_ip": self.last_seen_ip,
            "file_count": self.file_count,
            "disk_free": self.disk_free,
            "last_error": self.last_error,
            "tick_count": self.tick_count,
        }


class AgentRegistry:
    def __init__(self, offline_after_seconds: int = 180) -> None:
        self._lock = threading.Lock()
        self._agents: Dict[int, AgentRuntime] = {}
        self._offline_after = offline_after_seconds

    # ---------------------------------------------------------------- #
    # 写入
    # ---------------------------------------------------------------- #

    def upsert_identity(
        self,
        agent_id: int,
        player_id: int,
        contest_id: int,
        player_no: str,
        player_name: Optional[str],
        contest_slug: str,
        machine_id: str,
        hostname: Optional[str] = None,
    ) -> AgentRuntime:
        """注册或刷新一台机器的静态身份信息（登录时调用一次即可）。"""
        with self._lock:
            runtime = self._agents.get(agent_id)
            if runtime is None:
                runtime = AgentRuntime(
                    agent_id=agent_id,
                    player_id=player_id,
                    contest_id=contest_id,
                    player_no=player_no,
                    contest_slug=contest_slug,
                )
                self._agents[agent_id] = runtime
            runtime.player_no = player_no
            runtime.player_name = player_name
            runtime.contest_slug = contest_slug
            runtime.machine_id = machine_id
            runtime.hostname = hostname
            return runtime

    def note_tick(
        self,
        agent_id: int,
        *,
        ip: Optional[str] = None,
        agent_version: Optional[str] = None,
        scan_root: Optional[str] = None,
        file_count: int = 0,
        disk_free: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> Optional[AgentRuntime]:
        """记录一次心跳。返回运行时对象；未知 agent_id 返回 None。"""
        now = utcnow()
        with self._lock:
            runtime = self._agents.get(agent_id)
            if runtime is None:
                return None
            was_offline = not runtime.online
            runtime.online = True
            runtime.last_tick_at = now
            runtime.tick_count += 1
            if ip:
                runtime.last_seen_ip = ip
            if agent_version:
                runtime.agent_version = agent_version
            if scan_root:
                runtime.scan_root = scan_root
            runtime.file_count = file_count
            runtime.disk_free = disk_free
            runtime.last_error = last_error
            runtime.dirty = True
            if was_offline:
                log.info("agent %s (%s) 恢复在线", agent_id, runtime.player_no)
            return runtime

    def mark_offline(self, agent_id: int, reason: str = "tick 超时") -> Optional[AgentRuntime]:
        with self._lock:
            runtime = self._agents.get(agent_id)
            if runtime is None or not runtime.online:
                return None
            runtime.online = False
            runtime.dirty = True
            log.info("agent %s (%s) 判定离线：%s", agent_id, runtime.player_no, reason)
            return runtime

    def forget(self, agent_id: int) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    # ---------------------------------------------------------------- #
    # 读取
    # ---------------------------------------------------------------- #

    def get(self, agent_id: int) -> Optional[AgentRuntime]:
        with self._lock:
            return self._agents.get(agent_id)

    def all(self, contest_id: Optional[int] = None) -> List[AgentRuntime]:
        with self._lock:
            items = list(self._agents.values())
        if contest_id is not None:
            items = [a for a in items if a.contest_id == contest_id]
        items.sort(key=lambda a: (not a.online, a.player_no))
        return items

    # ---------------------------------------------------------------- #
    # 后台任务接口
    # ---------------------------------------------------------------- #

    def sweep_offline(self) -> List[AgentRuntime]:
        """把超时未 tick 的机器标记为离线。返回本次刚转为离线的集合。

        由后台任务周期调用 —— 不能只在 tick 时判断，因为"不再 tick"这件事
        本身没有任何事件可以触发。
        """
        now = utcnow()
        deadline = now - timedelta(seconds=self._offline_after)
        flipped: List[AgentRuntime] = []
        with self._lock:
            for runtime in self._agents.values():
                if not runtime.online:
                    continue
                last = runtime.last_tick_at
                if last is None or last < deadline:
                    runtime.online = False
                    runtime.dirty = True
                    flipped.append(runtime)
        for runtime in flipped:
            log.info("agent %s (%s) 判定离线（超过 %ds 无 tick）",
                     runtime.agent_id, runtime.player_no, self._offline_after)
        return flipped

    def take_dirty(self) -> List[AgentRuntime]:
        """取走所有待落库的运行时对象并清除 dirty 标记。"""
        with self._lock:
            dirty = [a for a in self._agents.values() if a.dirty]
            for runtime in dirty:
                runtime.dirty = False
            return dirty
