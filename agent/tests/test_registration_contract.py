"""新契约：**常驻服务本体永不注册**，注册只发生在 ``--provision``。

真机事故（这条契约就是为它立的）：Agent 以选手身份跑，遇到"没有凭据"就去读
root 只读的统一密钥 → ``PermissionError`` → 抛异常 → ``exit 1`` → 被 systemd
反复重启（5 次撞 ``StartLimitBurst`` 罢手），而 root 的注册单元
``syncoj-agent-enroll.service`` **从来没被跑过** —— 机器永远不出现。

现在：

* 没有凭据 = 一个**正常状态**（在等注册单元）：不退出、不联网、不刷日志，
  每 :data:`REGISTRATION_POLL_SECONDS` 秒看一眼凭据是否出现；
* 注册只由 ``--provision``（装机时 root 跑一次）完成；
* 注册路径**不校验也不触碰**选手目录（``scan.roots`` / ``deploy_root`` /
  ``upgrade.*``），因为注册以 root 跑，那些模板按 root 的家展开必然不存在；
* ``{home}`` / ``{desktop}`` 一律按**运行账号**（跑安装的那个账号）展开，
  与"当前是谁在跑"无关 —— **绝不允许出现 ``/root/...``**（用户口径：
  「桌面应该是安装用户的桌面」）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from syncoj_agent.config import AgentConfig, ConfigError  # noqa: E402
from syncoj_agent.main import (  # noqa: E402
    REGISTRATION_POLL_SECONDS,
    Agent,
    main,
)


# --------------------------------------------------------------------------- #
# 假件：不联网、不碰 systemd
# --------------------------------------------------------------------------- #


class RecordingClient:
    """记录调用的假客户端。**注册调用会被逐条记下来**（这正是被测的点）。"""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.enroll_calls: List[Dict[str, object]] = []
        self.tick_calls: List[Dict[str, object]] = []
        self.token: Optional[str] = None
        self.closed = False
        self.enroll_response: Dict[str, object] = {
            "token": "tok-1",
            "agent_id": 7,
            "claimed": True,
            "bound": True,
            "pair_code": "",
            "player_no": "S001",
            "contest_id": 1,
            "contest_slug": "mock-1",
            "contest_name": "校内模拟赛",
            "reason": "",
        }

    def enroll(self, **kwargs) -> dict:
        self.enroll_calls.append(kwargs)
        return dict(self.enroll_response)

    def tick(self, payload: dict) -> dict:
        self.tick_calls.append(payload)
        return {
            "server_time": 1767225600,
            "next_tick_seconds": 60,
            "need_upload": [],
            "deploy_jobs": [],
            "cancel_assets": [],
            "upgrade": None,
            "config": {},
            "claimed": True,
            "bound": True,
            "pair_code": None,
            "reason": None,
            "player_no": "S001",
            "contest_slug": "mock-1",
        }

    def set_token(self, token: Optional[str]) -> None:
        self.token = token

    def close(self) -> None:
        self.closed = True

    # 新契约下服务"没有归属"时不该碰这两个接口
    def upload_file(self, *args, **kwargs):
        raise AssertionError("还没有归属就上传了代码")

    def open_download(self, *args, **kwargs):
        raise AssertionError("还没有归属就收到下发")


@pytest.fixture()
def agent_config(workdir: Path) -> AgentConfig:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code"]
    config.bootstrap_key_file = workdir / "bootstrap.key"
    config.server_url = "https://127.0.0.1:8000"
    return config


def make_agent(config: AgentConfig, client: RecordingClient) -> Agent:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.deploy_root.mkdir(parents=True, exist_ok=True)
    config.scan_roots[0].mkdir(parents=True, exist_ok=True)
    # 故意**不写**统一密钥：服务本来就不该去读它（真机上它根本读不到）
    agent = Agent(config)
    agent.client.close()
    agent.client = client  # type: ignore[assignment]
    return agent


def run_rounds(agent: Agent, rounds: int = 1) -> List[float]:
    delays: List[float] = []

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= rounds:
            agent.request_stop()

    agent._sleep = fake_sleep  # type: ignore[assignment]
    assert agent.run_forever() == 0, "等凭据不是错误，退出码必须是 0"
    return delays


def write_bootstrap_key(config: AgentConfig) -> None:
    """给**注册那条路**用的密钥。服务那条路永远用不上它（也不许去读）。"""
    config.bootstrap_key_file.write_text("SECRET-BOOTSTRAP-KEY\n", encoding="utf-8")


class ProvisionArgs:
    """``_run_provision`` 要的命令行参数（只有 ``--chown-to``）。"""

    chown_to = None


# --------------------------------------------------------------------------- #
# 一、没有凭据 = 正常状态（不注册、不退出、不刷日志）
# --------------------------------------------------------------------------- #


def test_service_without_credential_waits_instead_of_enrolling(
    agent_config: AgentConfig,
) -> None:
    """**这是真机事故的回归守卫。**

    没有凭据时服务必须：不注册、不上传、不退出，就按慢节奏轮询等着。
    """
    client = RecordingClient()
    agent = make_agent(agent_config, client)

    delays = run_rounds(agent, 3)

    assert client.enroll_calls == [], "服务本体不许注册（密钥是 root 只读的）"
    assert client.tick_calls == [], "没有凭据时不该 tick"
    assert delays == [REGISTRATION_POLL_SECONDS] * 3, (
        "应当按固定的慢节奏轮询，而不是退出或指数退避"
    )


def test_waiting_message_is_logged_once_not_every_round(
    agent_config: AgentConfig, caplog
) -> None:
    """每次轮询都打一段"这是设计如此"会把 journal 刷满，真正的原因反而被埋掉。"""
    client = RecordingClient()
    agent = make_agent(agent_config, client)

    with caplog.at_level("WARNING"):
        run_rounds(agent, 5)

    waiting = [r for r in caplog.records if "还没有注册凭据" in r.getMessage()]
    assert len(waiting) == 1, "等待提示只该说一次，实际说了 %d 次" % len(waiting)
    assert "syncoj-agent-enroll.service" in waiting[0].getMessage()


def test_service_never_touches_the_bootstrap_key(
    agent_config: AgentConfig, monkeypatch
) -> None:
    """密钥"读不到"这件事**不该发生**：服务压根不去读它。

    老代码的现场就是撞在这一步：``read_bootstrap_key`` 抛 ``PermissionError``
    → 异常 → 退出 1 → systemd 反复重启。这里把它变成一个"只要被调用就失败"
    的探针 —— 服务必须一次都不碰。
    """
    import syncoj_agent.main as main_module

    def forbidden(path):
        raise AssertionError("服务本体去读注册密钥了：%s" % path)

    monkeypatch.setattr(main_module, "read_bootstrap_key", forbidden)

    client = RecordingClient()
    agent = make_agent(agent_config, client)

    delays = run_rounds(agent, 2)

    assert delays == [REGISTRATION_POLL_SECONDS] * 2
    assert client.enroll_calls == []


def test_credential_appearing_is_used_from_the_next_round(
    agent_config: AgentConfig,
) -> None:
    """凭据一出现就该转入正常同步 —— 等注册不是"卡住"，只是没到时候。"""
    client = RecordingClient()
    agent = make_agent(agent_config, client)

    # 第一轮：没有凭据，等着
    delays = run_rounds(agent, 1)
    assert delays == [REGISTRATION_POLL_SECONDS]
    assert client.tick_calls == []

    # 注册单元（root 的 --provision）把凭据放进来了
    from syncoj_agent.main import _run_provision

    write_bootstrap_key(agent_config)
    provision_agent = make_agent(agent_config, client)
    assert _run_provision(ProvisionArgs(), agent_config, provision_agent) == 0
    assert len(client.enroll_calls) == 1, "注册只发生在 --provision 这条路上"

    # 第二轮：同一个进程（服务没重启）也应当立刻用上它
    agent2 = make_agent(agent_config, client)
    delays = run_rounds(agent2, 1)
    assert len(client.tick_calls) == 1
    assert delays == [60.0]


def test_existing_credential_does_not_enroll(agent_config: AgentConfig) -> None:
    """重启之后从磁盘读凭据，而不是重新注册（否则配对码每轮都换）。"""
    from syncoj_agent.main import _run_provision

    client = RecordingClient()
    write_bootstrap_key(agent_config)
    provision_agent = make_agent(agent_config, client)
    assert _run_provision(ProvisionArgs(), agent_config, provision_agent) == 0

    agent = make_agent(agent_config, client)
    client.tick_calls.clear()
    run_rounds(agent, 2)

    assert len(client.enroll_calls) == 1, "只有装机那一次注册"
    assert len(client.tick_calls) == 2


# --------------------------------------------------------------------------- #
# 二、注册路径不校验也不触碰选手目录
# --------------------------------------------------------------------------- #


def _write_ini(
    workdir: Path,
    *,
    run_user: str,
    deploy_root: str,
    scan_roots: str,
    upgrade_mode: str = "off",
    public_key: str = "",
) -> Path:
    state = workdir / "state"
    state.mkdir(parents=True, exist_ok=True)
    ini = workdir / "agent.ini"
    body = (
        "[server]\n"
        "url = https://10.0.0.1:8443\n"
        "verify_tls = false\n"
        "ca_file =\n"
        "\n"
        "[agent]\n"
        "run_user = %s\n"
        "bootstrap_key_file = %s\n"
        "state_dir = %s\n"
        "deploy_root = %s\n"
        "\n"
        "[scan]\n"
        "roots = %s\n"
        "prefix = none\n"
        "\n"
        "[upgrade]\n"
        "mode = %s\n"
        "install_root = /opt/syncoj\n"
        "public_key = %s\n"
        % (
            run_user,
            workdir / "bootstrap.key",
            state,
            deploy_root,
            scan_roots,
            upgrade_mode,
            public_key,
        )
    )
    # newline="\n" 只能走 open(...)：write_text(newline=) 是 3.10 才有的，
    # 而目标机是 3.8（check_py38.py 会挡）
    with ini.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    (workdir / "bootstrap.key").write_text("SECRET-KEY\n", encoding="utf-8")
    return ini


def test_provision_ignores_player_dirs_that_do_not_exist(
    workdir: Path, monkeypatch, capsys
) -> None:
    """(a) 注册运行时，选手目录不存在**不该**让它退出。

    现场就是这样死的：``scan.roots`` 里的 ``{desktop}/{player_no}`` 在注册时
    被按 **root 的家**展开成 ``/root/桌面/...``，校验直接失败退出 2，
    而 journal 里一个字都没有。
    """
    import syncoj_agent.main as main_module

    missing_scan = workdir / "nonexistent" / "{player_no}"
    ini = _write_ini(
        workdir,
        run_user="",
        deploy_root=str(workdir / "also-nonexistent"),
        scan_roots=str(missing_scan),
    )

    monkeypatch.setattr(main_module, "AgentClient", RecordingClient)

    code = main(["--provision", "--config", str(ini)])

    out = capsys.readouterr()
    assert code == 0, "注册不该因为选手目录不存在而失败（stderr=%s）" % out.err
    assert not (workdir / "nonexistent").exists(), "注册路径不该创建选手目录"
    assert not (workdir / "also-nonexistent").exists(), "注册路径不该创建下发目录"
    assert (workdir / "state" / "credential.json").is_file(), "凭据应当落盘"


def test_service_guard_still_rejects_a_missing_scan_parent(
    workdir: Path, monkeypatch, capsys
) -> None:
    """(c) 服务路径这道守卫**不许**被削弱：服务以运行账号跑，目录不存在要吵。"""
    import syncoj_agent.main as main_module

    ini = _write_ini(
        workdir,
        run_user="",
        deploy_root=str(workdir / "deploy"),
        scan_roots=str(workdir / "nonexistent" / "{player_no}"),
    )
    monkeypatch.setattr(main_module, "AgentClient", RecordingClient)

    code = main(["--config", str(ini)])

    assert code == 2, "服务路径必须仍然因为扫描目录不存在而拒绝启动"
    err = capsys.readouterr().err
    assert "扫描目录的父目录不存在" in err
    assert "配置有误" in err


def test_provision_skips_upgrade_and_scan_validation(workdir: Path) -> None:
    """注册时只校验：服务端地址、状态目录、统一密钥。"""
    ini = _write_ini(
        workdir,
        run_user="",
        deploy_root=str(workdir / "absent-deploy"),
        scan_roots=str(workdir / "absent-scan" / "{player_no}"),
        upgrade_mode="apply",
        public_key="",
    )
    config = AgentConfig.load(ini)

    config.validate(for_provision=True)  # 不抛

    with pytest.raises(ConfigError) as excinfo:
        config.validate()
    message = str(excinfo.value)
    assert "扫描目录的父目录不存在" in message
    assert "upgrade.public_key" in message


def test_provision_still_requires_a_valid_server_url(workdir: Path) -> None:
    """变异守卫：注册运行也必须校验 ``server.url``（别的都能跳，这个不能）。"""
    ini = _write_ini(
        workdir,
        run_user="",
        deploy_root=str(workdir / "桌面"),
        scan_roots=str(workdir / "code"),
    )
    config = AgentConfig.load(ini)
    config.server_url = "ftp://nonsense"

    with pytest.raises(ConfigError) as excinfo:
        config.validate(for_provision=True)
    assert "server.url" in str(excinfo.value)


def test_运行账号查不到时报可执行的错_不许静默退回当前用户(
    workdir: Path, monkeypatch
) -> None:
    """**这条守的是"绝不出现 ``/root/...``"的最后一道闸。**

    运行账号写错了/还没建时**不许**默默按当前进程的用户展开 —— 注册单元以 root
    跑，那就会得到 ``/root/桌面``，而那台机器上不存在：注册退出 2、journal 里
    一个字都没有（真机事故）。必须报一条照着能修的错误：说清是哪个账号、
    去哪儿改。
    """
    from syncoj_agent import config as config_module

    monkeypatch.setattr(config_module, "_pwd_home", lambda user: None)

    ini = _write_ini(
        workdir,
        run_user="noi",
        deploy_root="{desktop}",
        scan_roots="{home}/{player_no}",
    )

    with pytest.raises(ConfigError) as excinfo:
        AgentConfig.load(ini)

    message = str(excinfo.value)
    assert "noi" in message, "要说清是哪个账号：%s" % message
    assert "run_user" in message, "要指出去哪儿改（agent.ini 的 run_user）：%s" % message
    assert "家目录" in message, message


# --------------------------------------------------------------------------- #
# 三、校验失败必须出现在 journal 里（无条件、且只说一遍）
# --------------------------------------------------------------------------- #


def test_validation_failure_always_reaches_stderr(
    workdir: Path, monkeypatch, capsys
) -> None:
    """(d) 注册单元由 systemd 起 —— 不打到 stderr 就等于 journal 里一个字都没有。

    而且异常里已经带了"配置有误"，外面**不许**再包一层前缀（"配置校验失败：
    配置有误：…"）。
    """
    import syncoj_agent.main as main_module

    ini = _write_ini(
        workdir,
        run_user="",
        deploy_root=str(workdir / "absent"),
        scan_roots=str(workdir / "absent" / "{player_no}"),
    )
    monkeypatch.setattr(main_module, "AgentClient", RecordingClient)

    code = main(["--config", str(ini)])

    assert code == 2
    err = capsys.readouterr().err
    assert err.strip(), "校验失败必须打到 stderr（journal 里才看得到）"
    assert err.count("配置有误") == 1, "前缀只该出现一次：%r" % err
    assert "配置校验失败" not in err


def test_provision_failure_carries_a_next_step(
    workdir: Path, monkeypatch, capsys
) -> None:
    """注册失败必须带"下一步怎么办" —— 这次最贵的成本是"失败了却一个字都没有"。"""
    import syncoj_agent.main as main_module

    ini = _write_ini(
        workdir,
        run_user="",
        deploy_root=str(workdir / "absent"),
        scan_roots=str(workdir / "absent" / "{player_no}"),
    )
    # 把密钥删掉：注册路径会因此报错（服务端地址是合法的）
    (workdir / "bootstrap.key").unlink()
    monkeypatch.setattr(main_module, "AgentClient", RecordingClient)

    code = main(["--provision", "--config", str(ini)])

    assert code == 2, "统一密钥不可读属于配置类失败"
    err = capsys.readouterr().err
    assert "统一密钥" in err
    assert "下一步" in err
    assert "systemctl start syncoj-agent-enroll.service" in err
