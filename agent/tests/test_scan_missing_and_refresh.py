"""扫描根缺失的上报与桌面刷新（这一批的两个现场问题）。

**问题一**：以前扫描根不存在时由 Agent **代建**（`mkdir(parents=True)`），现场
表现是"老师打开桌面就多出一堆空文件夹"，还掩盖了"这台机器根本没在收文件"。
现在的口径：**不建、不报错、当成"这个根现在没有文件"**，缺失的根每轮通过
``stats.scan_missing`` 上报（服务端拿它做后台提示），本地日志只在**状态变化**时
说一次（缺失一次、出现一次），缺失时也不进 ``last_error``。

**问题二**：文件下发落盘是对的（``.syncoj-part`` + 同目录 ``os.replace``），但桌面
不刷新，用户要进文件夹才看得到。补的是"通知文件管理器"：对刚放进去文件的那个目录
碰一下 mtime（``os.utime``），**只在确实写成功之后**、且失败绝不影响下发结果。
另外启动时核一遍"配置里的落点"与 XDG 声明的桌面是不是同一个目录。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from syncoj_agent.config import AgentConfig  # noqa: E402
from syncoj_agent.main import SCAN_MISSING_MAX, Agent, build_tick_payload  # noqa: E402
from syncoj_agent.state import declared_xdg_desktop, detect_desktop  # noqa: E402


class _Collector(logging.Handler):
    """把日志收在手里，**不依赖 pytest 的 caplog**。

    为什么不用 caplog：有别的用例会调 ``setup_logging()``，那会 ``root.handlers = []``
    把 pytest 挂在 root 上的捕获 handler 一起换掉；之后 caplog 就什么都看不到。
    挂在 ``syncoj_agent.main`` 这一个 logger 上的 handler 不受影响。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.items: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 平凡
        self.items.append(record)


@pytest.fixture()
def logs():
    # 注意不是 "syncoj_agent.main"：main.py 里的 logger 名字是 "syncoj.agent"
    logger = logging.getLogger("syncoj.agent")
    handler = _Collector()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.items
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def messages(records) -> List[str]:
    return [record.getMessage() for record in records]


@pytest.fixture()
def config(workdir: Path) -> AgentConfig:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code"]
    config.scan_prefix = "none"
    config.bootstrap_key_file = workdir / "bootstrap.key"
    config.server_url = "https://127.0.0.1:8000"
    for path in (config.state_dir, config.deploy_root):
        path.mkdir(parents=True, exist_ok=True)
    return config


def make_agent(config: AgentConfig) -> Agent:
    return Agent(config)


def payload_of(agent: Agent, roots: "list" = None):
    """跑一遍真正的 ``_ensure_roots`` → ``_scan`` → ``_build_tick_payload``。"""
    agent._roots = roots if roots is not None else agent._resolve_roots("S001", "mock-1")
    results, local, skipped = agent._scan()
    payload, _oversize, _errors = agent._build_tick_payload(
        results, local, scan_complete=(skipped == 0)
    )
    return payload


# --------------------------------------------------------------------------- #
# 一、缺失的扫描根：不建、不报错、每轮上报
# --------------------------------------------------------------------------- #


def test_缺失的扫描根不被创建也不报错(config: AgentConfig, workdir: Path) -> None:
    missing = workdir / "还没建的目录"
    config.scan_roots = [missing]
    agent = make_agent(config)

    payload = payload_of(agent)

    assert not missing.exists(), "扫描根被代建了（现场会看到一堆空文件夹）"
    assert payload["stats"]["scan_missing"] == [str(missing)]
    assert payload["stats"]["last_error"] is None, "缺失不是错误，别进 last_error"
    assert payload["scan"] == []
    # 缺失的根**没有**被扫过，所以不能说"扫完了" —— 服务端拿它判删除，
    # 而"根不在"与"文件都被删了"在报文里长得一样
    assert payload["scan_complete"] is False


def test_根出现之后_scan_missing_清空并且开始收文件(
    config: AgentConfig, workdir: Path
) -> None:
    root = workdir / "code"
    config.scan_roots = [root]
    agent = make_agent(config)

    assert payload_of(agent)["stats"]["scan_missing"] == [str(root)]

    # 选手开始保存文件 —— 目录是**他**建出来的
    (root / "p1").mkdir(parents=True)
    (root / "p1" / "p1.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    payload = payload_of(agent)

    assert payload["stats"]["scan_missing"] == []
    assert payload["scan_complete"] is True
    assert [entry["path"] for entry in payload["scan"]] == ["p1/p1.cpp"]


def test_根被同名文件占着也算缺失并说明原因(config: AgentConfig, workdir: Path) -> None:
    """"存在但不是目录"与"不存在"都要报，而且要说得出是哪种 —— 配置看着没错、<br>
    一个文件都收不上来，是最难查的那种。"""
    occupied = workdir / "code"
    occupied.write_text("我不是目录\n", encoding="utf-8")
    config.scan_roots = [occupied]
    agent = make_agent(config)
    agent._roots = agent._resolve_roots("S001", "mock-1")

    reasons = agent._missing_scan_roots()

    assert list(reasons) == [str(occupied)]
    assert "目录" in reasons[str(occupied)]
    assert payload_of(agent)["stats"]["scan_missing"] == [str(occupied)]


def test_缺失提示只在状态变化时打一次(
    config: AgentConfig, workdir: Path, logs
) -> None:
    """每轮打一条会把真正的问题埋掉 —— 开考前那个目录本来就**不该存在**。"""
    missing = workdir / "code"
    config.scan_roots = [missing]
    agent = make_agent(config)
    agent._roots = agent._resolve_roots("S001", "mock-1")

    for _ in range(3):
        agent._missing_scan_roots()
    warnings = [text for text in messages(logs) if "扫描根不可用" in text]
    assert len(warnings) == 1, "缺失提示打了 %d 次" % len(warnings)
    assert str(missing) in warnings[0]

    logs.clear()
    missing.mkdir()
    agent._missing_scan_roots()
    agent._missing_scan_roots()
    appeared = [text for text in messages(logs) if "已出现" in text]
    assert len(appeared) == 1, "出现提示打了 %d 次" % len(appeared)


def test_scan_missing_最多五条且排序稳定(workdir: Path) -> None:
    roots = [workdir / ("根-%02d" % index) for index in range(7)]
    raw = [str(p) for p in reversed(roots)] + [str(roots[0])]  # 乱序 + 重复

    payload, _o, _e = build_tick_payload(
        machine_id="m",
        agent_version="1.0.0",
        scan_root="x",
        results=[],
        partials=[],
        stats={"scan_missing": raw},
    )

    reported = payload["stats"]["scan_missing"]
    assert len(reported) == SCAN_MISSING_MAX == 5
    assert reported == sorted(reported), "排序不稳定，服务端与日志没法对账"
    assert reported == sorted({str(p) for p in roots})[:5]


def test_超过五条时日志里说明(workdir: Path, logs) -> None:
    raw = [str(workdir / ("根-%02d" % index)) for index in range(6)]

    payload, _o, _e = build_tick_payload(
        machine_id="m",
        agent_version="1.0.0",
        scan_root="x",
        results=[],
        partials=[],
        stats={"scan_missing": raw},
    )

    assert len(payload["stats"]["scan_missing"]) == 5
    assert any("只报前 5 个" in text for text in messages(logs)), messages(logs)


def test_都齐了就说扫完了(workdir: Path, config: AgentConfig) -> None:
    root = workdir / "code"
    root.mkdir(parents=True, exist_ok=True)
    config.scan_roots = [root]
    agent = make_agent(config)

    payload = payload_of(agent)

    assert payload["stats"]["scan_missing"] == []
    assert payload["scan_complete"] is True


# --------------------------------------------------------------------------- #
# 二、下发落盘之后催一下桌面刷新
# --------------------------------------------------------------------------- #


def test_写成功之后会碰该刷新的目录(
    config: AgentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = make_agent(config)
    calls = []
    monkeypatch.setattr(os, "utime", lambda path, times=None: calls.append(str(path)))

    # 落在桌面根上 → 只碰桌面自己
    agent._nudge_after_write("题面.pdf")
    assert calls == [str(config.deploy_root)]

    # 落在子目录里 → 子目录要碰（新文件在那儿），桌面也要碰（可能刚多出一个文件夹）
    calls.clear()
    agent._nudge_after_write("p1/p1.cpp")
    assert calls == [str(config.deploy_root / "p1"), str(config.deploy_root)]


def test_碰_mtime_失败不影响下发(
    config: AgentConfig, monkeypatch: pytest.MonkeyPatch, logs
) -> None:
    agent = make_agent(config)

    def boom(path, times=None):
        raise OSError("不支持这个文件系统")

    monkeypatch.setattr(os, "utime", boom)

    agent._nudge_after_write("题面.pdf")  # 不抛
    agent._nudge_after_write("题面.pdf")

    records = [r for r in logs if "碰目录 mtime 失败" in r.getMessage()]
    assert len(records) == 1, "同一个目录的失败提示只该记一次：%s" % messages(logs)
    assert records[0].levelno == logging.DEBUG, "它不该惊动 journal 的 WARNING"


def test_只有写成功才碰_mtime(
    config: AgentConfig, monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    """跳过（内容已一致）与失败都不碰 —— 没有新文件要显示。"""
    import syncoj_agent.main as main_module

    agent = make_agent(config)
    nudged = []
    monkeypatch.setattr(agent, "_nudge_after_write", lambda dest: nudged.append(dest))

    def fake_download(client, job, deploy_root, on_progress=None):
        return SimpleNamespace(
            status=job["status"],
            asset_id=1,
            dest=job["dest"],
            bytes_written=10,
            error=None,
        )

    monkeypatch.setattr(main_module, "download_asset", fake_download)

    agent._download([{"status": "skipped", "dest": "a.txt"}])
    assert nudged == [], "内容已一致、没有新文件，不该碰 mtime"

    agent._download([{"status": "failed", "dest": "b.txt"}])
    assert nudged == [], "失败更不该碰 mtime"

    agent._download([{"status": "ok", "dest": "c.txt"}])
    assert nudged == ["c.txt"]


# --------------------------------------------------------------------------- #
# 三、落点与 XDG 桌面一致性
# --------------------------------------------------------------------------- #


def test_declared_xdg_desktop_读的是声明值(workdir: Path) -> None:
    """``detect_desktop`` 优先读 ``XDG_DESKTOP_DIR``；核对用的是**声明值**本身。

    声明了但目录不在时，``detect_desktop`` 会跳过它（探测要能真的写文件），
    而"配置与声明不一致"这件事仍然要能看出来。
    """
    home = workdir / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/我的桌面"\n', encoding="utf-8"
    )

    assert declared_xdg_desktop(home) == home / "我的桌面"
    assert detect_desktop(home) == home / "桌面", "目录不在时探测会跳过它"


def test_落点与_XDG_不一致时警告一次(
    config: AgentConfig, monkeypatch: pytest.MonkeyPatch, logs
) -> None:
    import syncoj_agent.main as main_module

    agent = make_agent(config)
    monkeypatch.setattr(
        main_module, "declared_xdg_desktop", lambda *a, **k: config.deploy_root / "别处"
    )

    agent._check_desktop_consistency()
    agent._check_desktop_consistency()

    warnings = [text for text in messages(logs) if "不一致" in text]
    assert len(warnings) == 1, "只该说一次"
    assert str(config.deploy_root) in warnings[0], "要说清去哪儿看"
    assert "user-dirs.dirs" in warnings[0], "要说清依据是什么"


def test_落点与_XDG_一致时不说话(
    config: AgentConfig, monkeypatch: pytest.MonkeyPatch, logs
) -> None:
    import syncoj_agent.main as main_module

    agent = make_agent(config)
    monkeypatch.setattr(
        main_module, "declared_xdg_desktop", lambda *a, **k: config.deploy_root
    )

    agent._check_desktop_consistency()

    assert [text for text in messages(logs) if "不一致" in text] == []
