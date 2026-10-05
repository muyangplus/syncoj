"""协议契约测试：保证零依赖的 Agent 与 pydantic 服务端说的是同一种语言。

为什么需要它
------------
Agent 必须零第三方依赖，所以**不能** import 服务端的 pydantic 模型。两边于是
有了两份各自独立的协议描述，天然存在漂移风险：Agent 把 ``scan_complete`` 改名
成 ``complete``，服务端会当作缺省值 True 静默接受 —— 直到某天选手目录里出现
一个读不了的子目录，删除判定才误伤，而那时已经很难查。

对策：让 Agent 用自己的代码生成真实报文样本，服务端用 pydantic 模型校验它。
任何一侧改了字段名/类型/枚举值，这里立刻变红。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from syncoj_server.policy import build_policy
from syncoj_server.schemas import (
    AgentEvent,
    EnrollRequest,
    PartialDownload,
    ScanEntry,
    TickRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
FIXTURE_TOOL = AGENT_DIR / "tools" / "build_fixture.py"


@pytest.fixture(scope="module")
def fixture() -> dict:
    """调用 Agent 自己的构造函数生成报文样本。"""
    if not FIXTURE_TOOL.is_file():
        pytest.skip("找不到 agent/tools/build_fixture.py")
    completed = subprocess.run(
        [sys.executable, str(FIXTURE_TOOL), "--compact"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
    )
    if completed.returncode != 0:
        pytest.fail("生成 Agent 报文样本失败：\n%s\n%s" % (completed.stdout, completed.stderr))
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------- #
# 请求体契约
# --------------------------------------------------------------------------- #


def test_enroll_payload_matches_server_schema(fixture: dict) -> None:
    model = EnrollRequest.model_validate(fixture["enroll"])
    assert model.machine_id
    assert model.enroll_code


def test_tick_payload_matches_server_schema(fixture: dict) -> None:
    model = TickRequest.model_validate(fixture["tick"])
    assert model.machine_id
    assert isinstance(model.scan, list) and model.scan


def test_tick_scan_entries_are_valid(fixture: dict) -> None:
    for raw in fixture["tick"]["scan"]:
        entry = ScanEntry.model_validate(raw)
        assert len(entry.sha256) == 64


def test_tick_partials_are_valid(fixture: dict) -> None:
    for raw in fixture["tick"]["partials"]:
        partial = PartialDownload.model_validate(raw)
        assert partial.asset_id > 0
        assert partial.bytes_done >= 0


def test_tick_stats_are_valid(fixture: dict) -> None:
    stats = fixture["tick"]["stats"]
    assert set(stats) == {"disk_free", "last_error", "queue"}, (
        "stats 字段集变化了；服务端 TickStats 需同步"
    )


def test_scan_complete_flag_is_present(fixture: dict) -> None:
    """这个字段缺失会让服务端默认成 True，进而把漏扫误判为删除 —— 必须显式存在。"""
    assert "scan_complete" in fixture["tick"], (
        "tick 请求体缺少 scan_complete；服务端会默认 True 并错误地判定删除"
    )
    assert fixture["tick"]["scan_complete"] is False, "样本中有扫描不完整的根目录"


def test_event_payload_shape(fixture: dict) -> None:
    sample = {
        "level": "warning",
        "category": "disk_full",
        "message": "磁盘空间不足",
        "meta": {"free": 1024},
    }
    event = AgentEvent.model_validate(sample)
    assert event.category == "disk_full"


# --------------------------------------------------------------------------- #
# 策略一致性
# --------------------------------------------------------------------------- #


def test_agent_policy_defaults_match_server(fixture: dict) -> None:
    """两端的默认扫描策略必须逐项相等。

    不一致的后果很隐蔽：Agent 首轮用内置默认值扫描，第二轮的策略才来自服务端。
    若两边默认值不同，同一个文件可能在两轮之间"凭空出现或消失"。
    """
    agent_policy = fixture["policy_defaults"]
    server_policy = build_policy(max_file_size=2 * 1024 * 1024, scan_interval=60, max_files=5000)

    list_keys = ("extensions", "exclude_dirs", "exclude_suffixes")
    scalar_keys = ("max_file_size", "scan_interval", "max_files")
    keys = list_keys + scalar_keys

    missing = [key for key in keys if key not in agent_policy]
    assert not missing, "Agent 默认策略缺少字段: %s" % missing

    mismatches = []
    for key in keys:
        agent_value = agent_policy[key]
        server_value = server_policy[key]
        if key in list_keys:
            if sorted(agent_value) != sorted(server_value):
                mismatches.append((key, agent_value, server_value))
        elif agent_value != server_value:
            mismatches.append((key, agent_value, server_value))

    assert not mismatches, "两端默认策略不一致：\n" + "\n".join(
        "  %s:\n    agent  = %r\n    server = %r" % item for item in mismatches
    )


# --------------------------------------------------------------------------- #
# 路径校验实现的一致性
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def agent_paths():
    """导入 Agent 侧的路径模块（它刻意与服务端独立实现）。"""
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))
    try:
        from syncoj_agent import safepath  # type: ignore
    except ImportError as exc:  # pragma: no cover
        pytest.skip("无法导入 syncoj_agent.safepath: %s" % exc)
    return safepath


#: 敌意路径语料。Agent 与服务端必须对每一条给出**相同**的裁决。
HOSTILE_PATHS = [
    "main.cpp",
    "src/main.cpp",
    "a/b/c/d.pas",
    "题目一/solution.cpp",
    "../etc/passwd",
    "a/../../etc/passwd",
    "..",
    "a/..",
    "/etc/passwd",
    "/",
    "//server/share/x",
    "C:/Windows/system32",
    "c:foo.txt",
    "\\\\server\\share\\x",
    "a\\b\\c.cpp",
    "~/secret.cpp",
    "",
    " ",
    " main.cpp",
    "main.cpp ",
    "a//b.cpp",
    "a/\x00b.cpp",
    "a/\x01b.cpp",
    "a/.",
    "a/b./c.cpp",
    "a/con/b.cpp",
    "a/NUL.txt",
    "a/com1",
    "a/lpt9",
    "a" * 300 + ".cpp",
]


def test_path_validation_parity(agent_paths) -> None:
    """同一批敌意输入，两份独立实现必须给出相同裁决。

    Agent 与服务端**刻意**各写一份路径校验（纵深防御：不能一个漏洞同时洞穿
    两层）。代价是有漂移风险，这条测试就是防漂移的锚。
    """
    from syncoj_server.paths import PathValidationError as ServerError
    from syncoj_server.paths import validate_relpath as server_validate

    mismatches = []
    for raw in HOSTILE_PATHS:
        try:
            server_result = ("ok", server_validate(raw))
        except ServerError:
            server_result = ("reject", None)

        try:
            agent_result = ("ok", agent_paths.validate_relpath(raw))
        except agent_paths.PathError:
            agent_result = ("reject", None)

        if server_result != agent_result:
            mismatches.append((raw, server_result, agent_result))

    assert not mismatches, "两端路径校验结果不一致：\n" + "\n".join(
        "  %r: server=%r agent=%r" % item for item in mismatches
    )
