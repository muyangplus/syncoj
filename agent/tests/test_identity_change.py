"""机器被改派 / 换了场次之后，扫描根必须跟着换。

曾经 READY→READY 时 Agent 根本不接新身份：``_ensure_roots`` 有缓存，配置为
``桌面/{player_no}`` 时，机器从 S001 改派给 S002 之后它一直扫 ``桌面/S001``、
却以 S002 的身份上报与上传 —— S001 的代码进了 S002 的 ``source/``，S002 自己的
一份一个字节也收不上来；本地凭据里也一直是 S001，重启之后照样扫错。
"""

from __future__ import annotations

from pathlib import Path

from syncoj_agent.config import AgentConfig
from syncoj_agent.main import Agent
from syncoj_agent.state import Credential


class ReassigningServer:
    """第一轮回 S001；从第二轮起，服务端已经把这台机器改派给 S002。"""

    def __init__(self) -> None:
        self.sent = []
        self.events = []

    def tick(self, payload):
        self.sent.append(payload)
        player = "S001" if len(self.sent) == 1 else "S002"
        return {
            "claimed": True, "bound": True, "player_no": player, "contest_slug": "c",
            "next_tick_seconds": 30, "need_upload": [], "deploy_jobs": [],
        }

    def report_events(self, body):
        items = body.get("events", body) if isinstance(body, dict) else body
        self.events.extend(e.get("category") for e in items)

    def set_token(self, token):
        pass

    def close(self):
        pass


def make_agent(workdir: Path) -> Agent:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "desk"
    config.scan_roots = [workdir / "desk" / "{player_no}"]
    config.scan_prefix = "none"
    config.server_url = "http://127.0.0.1:9"
    config.state_dir.mkdir(parents=True)
    for player, content in (("S001", "A"), ("S002", "BB")):
        folder = config.deploy_root / player / "p1"
        folder.mkdir(parents=True)
        (folder / "p1.cpp").write_text(content, encoding="utf-8")
    agent = Agent(config)
    agent._credential = Credential(
        token="t", player_no="S001", contest_slug="c", claimed=True, bound=True,
        server_url=config.base_url,
    )
    return agent


def test_改派之后下一轮就扫新人的目录(workdir: Path) -> None:
    agent = make_agent(workdir)
    server = ReassigningServer()
    agent.client = server

    agent.cycle()  # S001：扫 S001
    agent.cycle()  # 服务端说已是 S002：本轮扫的仍是 S001（按 S001 展开），随即重算
    agent.cycle()  # 按 S002 展开，扫 S002

    first, second, third = server.sent
    assert first["scanned_for"] == {"player_no": "S001", "contest_slug": "c"}
    assert [(e["path"], e["size"]) for e in first["scan"]] == [("p1/p1.cpp", 1)]
    # 这一轮的报文**如实**说"我是按 S001 展开的" —— 服务端据此拒收对账
    assert second["scanned_for"] == {"player_no": "S001", "contest_slug": "c"}
    assert third["scanned_for"] == {"player_no": "S002", "contest_slug": "c"}
    assert [(e["path"], e["size"]) for e in third["scan"]] == [("p1/p1.cpp", 2)], (
        "改派后仍在扫上一个人的目录"
    )
    assert agent._credential.player_no == "S002", "本地凭据没跟上，重启后又会扫错"
    events = server.events + [e["category"] for e in agent._pending_events]
    assert events.count("identity_changed") == 1, events


def test_身份不变时不会重算扫描根(workdir: Path) -> None:
    agent = make_agent(workdir)

    class SameServer(ReassigningServer):
        def tick(self, payload):
            self.sent.append(payload)
            return {
                "claimed": True, "bound": True, "player_no": "S001", "contest_slug": "c",
                "next_tick_seconds": 30, "need_upload": [], "deploy_jobs": [],
            }

    server = SameServer()
    agent.client = server
    agent.cycle()
    roots = list(agent._roots)
    agent.cycle()

    assert agent._roots == roots
    events = server.events + [e["category"] for e in agent._pending_events]
    assert "identity_changed" not in events
