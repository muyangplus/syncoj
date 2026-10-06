"""运行参数：心跳节奏与离线判定。**存在库里、改完立即生效。**

为什么不是普通配置项
--------------------
``config.py`` 里那些是"启动时定下来"的东西（数据目录、签名私钥、监听端口），
改它们要重启 —— 而现场要调的是**心跳多快、多久算掉线**：教师看着机器列表，
觉得 30 秒太钝，希望当场改成 15 秒，而不是去改配置文件再重启服务端（那会打断
正在跑的考试）。

所以这三个值单独存在库里（``runtime_setting`` 表），每次用到时**现读**，
管理端点改完下一轮心跳就按新节奏走。

三个值的约束是**互相耦合**的
----------------------------
``offline_after_seconds`` 必须大于 ``tick_idle_seconds``：否则空闲状态下的机器
会在一轮心跳还没回来时就被判成离线，列表上所有机器开始闪 —— 这正是这个约束要
挡住的现场。校验放在这里一处，读写两边都走它。

**非法值一律拒绝，不静默夹紧**：把 4 悄悄改成 5 会让教师以为改成了他要的值，
而机器那边的节奏跟他想的不一样。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

__all__ = [
    "DEFAULTS",
    "LIMITS",
    "RuntimeSettingError",
    "RuntimeSettings",
    "load",
    "load_offline_after_seconds",
    "save",
]

#: 出厂值。三条都有明确理由：
#:
#: * ``tick_idle_seconds=30``：空闲心跳。它**同时**是本地扫描周期（两者继续绑在
#:   一起，见 ``api/agent.py::_agent_config``）。30 秒是"机器还活着"与"整间机房
#:   的请求量"之间的折中：50 台机器约 1.7 req/s，对单机 SQLite 很轻松。
#: * ``tick_active_seconds=2``：有活干时收紧，把下发的感知延迟压到秒级。
#: * ``offline_after_seconds=90``：能容 3 次心跳丢失（90 / 30），而"机器死了却
#:   还在列表上显示在线"最多也就 90 秒 —— 对监考来说那已经够快了。
DEFAULTS: Dict[str, int] = {
    "tick_idle_seconds": 30,
    "tick_active_seconds": 2,
    "offline_after_seconds": 90,
}

#: ``参数名 -> (下限, 上限)``。上下限是**硬边界**，越界一律 400。
LIMITS: Dict[str, tuple] = {
    "tick_idle_seconds": (5, 3600),
    "tick_active_seconds": (1, 60),
    "offline_after_seconds": (10, 86400),
}

#: 参数的中文名与单位，给错误信息用（教师看到的是这一串，不是字段名）。
LABELS: Dict[str, str] = {
    "tick_idle_seconds": "空闲心跳（秒）",
    "tick_active_seconds": "有活心跳（秒）",
    "offline_after_seconds": "离线判定（秒）",
}


class RuntimeSettingError(ValueError):
    """取值不合法。``detail`` 是一句能直接显示给教师的中文。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class RuntimeSettings:
    """三项运行参数的当前取值。"""

    tick_idle_seconds: int
    tick_active_seconds: int
    offline_after_seconds: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "tick_idle_seconds": self.tick_idle_seconds,
            "tick_active_seconds": self.tick_active_seconds,
            "offline_after_seconds": self.offline_after_seconds,
        }


def _coerce(name: str, raw: Any) -> int:
    """把库里/请求里的一格转成整数并做范围校验。"""
    if isinstance(raw, bool) or raw is None:
        raise RuntimeSettingError("%s 必须是一个整数秒数" % LABELS.get(name, name))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RuntimeSettingError("%s 必须是一个整数秒数，实际是 %r" % (LABELS.get(name, name), raw))
    low, high = LIMITS[name]
    if value < low or value > high:
        raise RuntimeSettingError(
            "%s 必须在 %d 到 %d 之间，实际是 %d" % (LABELS.get(name, name), low, high, value)
        )
    return value


def _validate(settings: RuntimeSettings) -> None:
    """跨字段的约束。现在只有一条，但它是**最容易踩的**那一条。"""
    if settings.offline_after_seconds <= settings.tick_idle_seconds:
        raise RuntimeSettingError(
            "离线判定（%d 秒）必须大于空闲心跳（%d 秒），否则机器一轮心跳还没回来"
            "就会被判成离线"
            % (settings.offline_after_seconds, settings.tick_idle_seconds)
        )


def load(session: Session) -> RuntimeSettings:
    """读当前取值。**每次调用都查库** —— 生效方式是"现读"，不是"启动时缓存"。"""
    from ..models import RuntimeSetting

    stored: Dict[str, str] = {
        row.key: row.value
        for row in session.execute(select(RuntimeSetting)).scalars()
    }

    values: Dict[str, int] = {}
    for name, default in DEFAULTS.items():
        raw = stored.get(name)
        if raw is None:
            values[name] = default
            continue
        try:
            values[name] = _coerce(name, raw)
        except RuntimeSettingError:
            # 库里那一格坏了（手工改库、半截写入）：退回出厂值继续跑，
            # **不让整个心跳 500** —— 那会让全机房的机器一起失联
            values[name] = default

    settings = RuntimeSettings(**values)
    # 库里那几格可能互相矛盾（有人手工改过）。矛盾时按出厂值兜一把：
    # 宁可"心跳按出厂节奏走"，也不要全机房一起闪离线
    try:
        _validate(settings)
    except RuntimeSettingError:
        return RuntimeSettings(**DEFAULTS)
    return settings


def load_offline_after_seconds(session: Session) -> int:
    """只取离线判定那一个值 —— 它被两个地方单独用（离线扫描、注册冲突）。"""
    return load(session).offline_after_seconds


def save(session: Session, changes: Dict[str, Any]) -> RuntimeSettings:
    """按 ``changes`` 更新，返回**改完之后**的完整取值。非法则抛 :class:`RuntimeSettingError`。

    ``changes`` 里只放要改的项；没提到的保持当前值。这样界面上改一项不必把另外
    两项也带上（带上就得担心"界面显示的旧值覆盖了别人刚改的值"）。
    """
    from ..models import RuntimeSetting

    current = load(session).as_dict()

    unknown = sorted(set(changes) - set(DEFAULTS))
    if unknown:
        raise RuntimeSettingError(
            "不认识的运行参数：%s。可配的是：%s"
            % ("、".join(unknown), "、".join(sorted(DEFAULTS)))
        )

    merged = dict(current)
    for name, raw in changes.items():
        merged[name] = _coerce(name, raw)

    resolved = RuntimeSettings(**merged)
    _validate(resolved)

    rows = {
        row.key: row for row in session.execute(select(RuntimeSetting)).scalars()
    }
    for name, value in resolved.as_dict().items():
        row = rows.get(name)
        if row is None:
            session.add(RuntimeSetting(key=name, value=str(value)))
        elif row.value != str(value):
            row.value = str(value)
    session.flush()
    return resolved
