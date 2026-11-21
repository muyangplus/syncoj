"""Agent 协议版本。

Agent 在 ``enroll`` / ``tick`` 里带 ``protocol_version``；低于
:data:`MIN_AGENT_PROTOCOL_VERSION` 的回 **426 ``agent_outdated``**，而不是一个
含糊的 422 —— 现场看到的应该是"这台机器的 Agent 太旧，要重装"，而不是
"请求内容不合法"。

版本号只在**破坏性**协议变更时加一，并同步 ``agent/syncoj_agent/client.py`` 的
``PROTOCOL_VERSION``（``test_agent_contract`` 对账）：

* 1 —— 没有这个字段的那一代（发 ``partials`` / ``completed_assets``）；
* 2 —— 2026-11：``scanned_for``、下发按目标回执（``deploy_done`` 带 sha256）、
  ``scan_complete`` 必填。
"""

from __future__ import annotations

__all__ = [
    "AGENT_PROTOCOL_VERSION",
    "MIN_AGENT_PROTOCOL_VERSION",
    "VERSIONED_AGENT_PATHS",
    "outdated_detail",
]

AGENT_PROTOCOL_VERSION = 2
MIN_AGENT_PROTOCOL_VERSION = 2

#: 带协议版本的两个端点（请求体校验失败时也要先看版本，见 errors 的 422 处理）
VERSIONED_AGENT_PATHS = ("/api/v1/agent/enroll", "/api/v1/agent/tick")


def outdated_detail(reported: int) -> str:
    return (
        "这台机器上的 Agent 太旧（协议版本 %d，服务端至少要 %d），请按「全部重装」重新装机"
        % (int(reported), MIN_AGENT_PROTOCOL_VERSION)
    )
