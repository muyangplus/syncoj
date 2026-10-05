"""Agent 侧的未配对状态与配对码展示。

统一密钥注册出来的是"一台还没有归属的机器"。它在客户端该怎么表现，
是这条链路上最容易做错的一段 —— 因为它**看起来像注册失败了**：

* 服务端回了 200，但 `player_no` 是空的、`agent_id` 是 0
* 它不能扫代码（不知道准考证号，``scan.roots`` 里的 ``{player_no}`` 展开不出来）
* 它不知道要等多久

做错的表现是：Agent 把"还没有归属"当成"凭据坏了"，于是反复重新注册 ——
每注册一次服务端就换一个新配对码，教师刚在机器上读到的那个立刻失效，
而且日志里刷满注册记录，真正的状态反而看不见。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from syncoj_agent.config import AgentConfig
from syncoj_agent.main import Agent
from syncoj_agent.state import Credential, load_credential, save_credential


@pytest.fixture()
def agent_config(workdir: Path) -> AgentConfig:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code"]
    config.enroll_code = ""
    config.bootstrap_key_file = workdir / "bootstrap.key"
    config.server_url = "https://127.0.0.1:8000"
    return config


def make_agent(config: AgentConfig) -> Agent:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.deploy_root.mkdir(parents=True, exist_ok=True)
    (config.scan_roots[0]).mkdir(parents=True, exist_ok=True)
    return Agent(config)


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #


def test_unclaimed_credential_is_still_usable() -> None:
    """**这是整件事的关键。**

    老判据是 ``token and agent_id > 0``。未配对的机器 agent_id 就是 0，
    按老判据会被当成"没有凭据"，于是反复重新注册，把配对码刷成新的 ——
    教师刚读到的那个立刻失效，而且日志里只看得到注册记录。
    """
    credential = Credential(token="tok", claimed=False, pair_code="ABC123")
    assert credential.is_usable() is True
    assert credential.needs_pairing is True


def test_claimed_credential_does_not_need_pairing() -> None:
    credential = Credential(token="tok", agent_id=7, player_no="S001")
    assert credential.is_usable() is True
    assert credential.needs_pairing is False


def test_old_credential_files_are_treated_as_claimed(workdir: Path) -> None:
    """升级前的凭据文件里没有 claimed 字段，默认必须是"已配对"。

    默认成 False 的话，所有老机器升级后都会突然开始等配对 ——
    而它们其实早就配好了。
    """
    path = workdir / "credential.json"
    path.write_text(
        json.dumps({"token": "tok", "agent_id": 3, "player_no": "S001"}),
        encoding="utf-8",
    )
    credential = load_credential(path)
    assert credential is not None
    assert credential.claimed is True
    assert credential.needs_pairing is False


def test_pair_code_round_trips(workdir: Path) -> None:
    path = workdir / "credential.json"
    save_credential(path, Credential(token="tok", claimed=False, pair_code="ABC123"))
    assert load_credential(path).pair_code == "ABC123"


# --------------------------------------------------------------------------- #
# 配对码展示
# --------------------------------------------------------------------------- #


def test_pair_code_is_written_to_the_desktop(agent_config: AgentConfig) -> None:
    """配对码的价值在于**被人在机器前读到**。

    日志要 journalctl 才看得到，而教师是走到机器前看屏幕的 ——
    桌面上一个「配对码.txt」是他最可能看见的东西。
    """
    agent = make_agent(agent_config)
    agent._show_pair_code("XYZ789")

    path = agent_config.deploy_root / agent_config.pairing_file_name
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "XYZ789" in text
    assert "配对" in text


def test_pair_code_file_is_removed_after_pairing(agent_config: AgentConfig) -> None:
    """留着它会让人以为还没配对 —— 下一个人看见就会去问老师，白折腾一轮。"""
    agent = make_agent(agent_config)
    agent._show_pair_code("XYZ789")
    path = agent_config.deploy_root / agent_config.pairing_file_name
    assert path.is_file()

    agent.clear_pair_code_file()
    assert not path.exists()


def test_missing_pair_code_file_is_not_an_error(agent_config: AgentConfig) -> None:
    """没配对过的机器本来就没有这个文件，不该报错。"""
    agent = make_agent(agent_config)
    agent.clear_pair_code_file()  # 不抛


def test_desktop_showing_can_be_turned_off(agent_config: AgentConfig) -> None:
    agent_config.pairing_show_on_desktop = False
    agent = make_agent(agent_config)
    agent._show_pair_code("XYZ789")

    path = agent_config.deploy_root / agent_config.pairing_file_name
    assert not path.exists()


def test_unwritable_desktop_does_not_crash(agent_config: AgentConfig, workdir: Path) -> None:
    """桌面写不进去不算致命：日志里还有一份，教师可以查日志。"""
    agent = make_agent(agent_config)
    # 把 deploy_root 指向一个"文件"，mkdir 一定失败
    blocker = workdir / "blocker"
    blocker.write_text("x", encoding="utf-8")
    agent_config.deploy_root = blocker

    agent._show_pair_code("XYZ789")  # 不抛


# --------------------------------------------------------------------------- #
# 认领之后
# --------------------------------------------------------------------------- #


def test_adopting_identity_updates_the_saved_credential(agent_config: AgentConfig) -> None:
    """服务端把身份放在 tick 响应里回来，Agent 接住并存下来。

    不让 Agent 再 enroll 一次：那是多一次可能失败的网络往返，还会多刷一条
    注册审计 —— 而这个信息服务端本来每轮都在发。
    """
    agent = make_agent(agent_config)
    agent._credential = Credential(token="tok", claimed=False, pair_code="ABC123")
    save_credential(agent_config.credential_path, agent._credential)
    agent._show_pair_code("ABC123")

    agent._adopt_identity({"claimed": True, "player_no": "S001", "contest_slug": "mock-1"})

    saved = load_credential(agent_config.credential_path)
    assert saved is not None
    assert saved.claimed is True
    assert saved.player_no == "S001"
    assert saved.contest_slug == "mock-1"
    assert saved.pair_code == ""
    assert not (agent_config.deploy_root / agent_config.pairing_file_name).exists()


# --------------------------------------------------------------------------- #
# 注册时的凭据选择
# --------------------------------------------------------------------------- #


def test_missing_everything_gives_an_actionable_error(agent_config: AgentConfig) -> None:
    """三种可能都要说出来，而不是笼统地"注册失败"。

    现场排错时，教师需要知道的是"去启动注册单元"还是"把注册码写进配置"。
    """
    from syncoj_agent.config import ConfigError

    agent = make_agent(agent_config)
    with pytest.raises(ConfigError) as excinfo:
        agent.ensure_credential()

    message = str(excinfo.value)
    assert "enroll_code" in message
    assert "bootstrap" in message or "统一密钥" in message


def test_unreadable_bootstrap_key_explains_root_only(
    agent_config: AgentConfig, workdir: Path
) -> None:
    """密钥文件在但读不出来时，要说清"这是设计如此"。

    否则运维会去改权限 —— 而把那把钥匙开放给选手读，等于把"无限注册"
    的能力交出去。
    """
    agent = make_agent(agent_config)
    key_path = workdir / "bootstrap.key"
    key_path.write_text("SECRET\n", encoding="utf-8")

    # 模拟"文件在但读不到"：read_bootstrap_key 返回空串
    import syncoj_agent.main as main_module

    original = main_module.read_bootstrap_key
    main_module.read_bootstrap_key = lambda path: ""  # type: ignore[assignment]
    try:
        from syncoj_agent.config import ConfigError

        with pytest.raises(ConfigError) as excinfo:
            agent.ensure_credential()
        assert "root" in str(excinfo.value)
    finally:
        main_module.read_bootstrap_key = original  # type: ignore[assignment]
