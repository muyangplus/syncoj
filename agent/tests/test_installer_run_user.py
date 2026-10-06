"""默认运行账号 = **跑安装的那个人**；以及卸载绝不误删人自己的账号。

为什么不能默认新建一个专用账号（``syncoj``）：Agent 要读的是**选手桌面上的
文件**，而桌面是那个人家目录下的东西。专用账号的家目录其实是状态目录，读不到
选手的桌面 —— 表现是"服务起来了但扫描/下发全是空的"。

为什么卸载要特别小心：默认运行账号就是**跑安装的那个人自己的账号**。如果卸载
还按老逻辑"删运行账号"，就变成删用户本人的账号 —— 灾难级。所以只删"有据可查
是本安装器建的"那一个（安装时写在状态目录里的标记）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


def make_installer(installer_module, workdir: Path, argv=()):
    options = installer_module.build_parser().parse_args(list(argv))
    options.config_dir = str(workdir / "etc")
    options.state_dir = str(workdir / "state")
    options.prefix = str(workdir / "opt")
    options.unit_dir = str(workdir / "units")
    return installer_module.Installer(
        options, installer_module.Reporter(dry_run=options.dry_run, quiet=True)
    )


def stub_run(monkeypatch, installer_module, calls):
    monkeypatch.setattr(
        installer_module, "run", lambda cmd, check=True: calls.append(list(cmd)) or 0
    )


def collect(instance):
    collected = []
    for kind in ("warn", "note", "action", "plan", "skip"):
        setattr(
            instance.report,
            kind,
            lambda message, k=kind: collected.append((k, message)),
        )
    return collected


def all_text(collected) -> str:
    return "\n".join(message for _tag, message in collected)


def read_ini_values(path: Path) -> dict:
    """把 agent.ini 的 `键 = 值` 读成字典（注释与分节行跳过）。"""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# --------------------------------------------------------------------------- #
# 默认账号怎么定
# --------------------------------------------------------------------------- #


def test_SUDO_USER_最优先(installer_module, monkeypatch) -> None:
    monkeypatch.setenv("SUDO_USER", "noi")
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: "root")
    assert installer_module.default_run_user() == "noi"


def test_没有_SUDO_USER_就取当前用户(installer_module, monkeypatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: "alice")
    assert installer_module.default_run_user() == "alice"


def test_都取不到才兜底(installer_module, monkeypatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: None)
    assert installer_module.default_run_user() == installer_module.DEFAULT_RUN_USER


def test_不显式指定时由_Installer_解析默认账号(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """parser 留 ``None``：要能区分"默认来的"与"人明确指定的"（root 判定用它）。"""
    monkeypatch.setenv("SUDO_USER", "noi")
    options = installer_module.build_parser().parse_args([])
    assert options.user is None

    instance = make_installer(installer_module, workdir)
    assert instance.run_user == "noi"


def test_以_root_跑安装且没指定账号时拒绝(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """默认会算成 root —— 不静默接受：Agent 以 root 跑会往选手桌面写 root 的文件、
    `%h` 也不再是选手桌面，而且是个没必要的提权面。"""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: "root")
    instance = make_installer(installer_module, workdir)
    assert instance.run_user == "root"

    with pytest.raises(installer_module.InstallError) as exc:
        instance.preflight()

    assert "--user" in str(exc.value)
    assert "root" in str(exc.value)


def test_以_root_跑预览只警告不报错(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """dry-run 什么都没做，不该直接失败 —— 但要说清真装会被拒。"""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: "root")
    source = workdir / "agentdir"
    (source / "syncoj_agent").mkdir(parents=True)
    instance = make_installer(
        installer_module,
        workdir,
        ["--dry-run", "--skip-python-check", "--from-dir", str(source)],
    )
    messages = collect(instance)

    instance.preflight()

    assert "以 root 身份跑安装" in all_text(messages)


def test_显式_user_root_允许但要警告(
    installer_module, workdir: Path, monkeypatch
) -> None:
    source = workdir / "agentdir"
    (source / "syncoj_agent").mkdir(parents=True)
    instance = make_installer(
        installer_module,
        workdir,
        ["--dry-run", "--skip-python-check", "--user", "root", "--from-dir", str(source)],
    )
    messages = collect(instance)

    instance.preflight()

    assert instance.run_user == "root"
    assert "定成了 root" in all_text(messages)


# --------------------------------------------------------------------------- #
# 默认不新建账号；只有显式指定不存在的账号才创建 + 留证据
# --------------------------------------------------------------------------- #


def test_默认账号已存在就不去_useradd(
    installer_module, workdir: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: "alice")
    instance = make_installer(installer_module, workdir)
    assert instance.run_user == "alice"

    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: name == "alice")

    instance.ensure_user()

    assert calls == [], "默认账号本来就存在，不该去创建"
    assert instance._user_created_by_installer() is None, "没建账号就不该留标记"


def test_显式指定不存在的账号才创建并留下证据(
    installer_module, workdir: Path, monkeypatch
) -> None:
    instance = make_installer(installer_module, workdir, ["--user", "syncoj-new"])

    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: False)

    instance.ensure_user()

    assert any(cmd[:1] == ["useradd"] for cmd in calls), calls
    assert instance._user_created_by_installer() == "syncoj-new"


def test_跑安装的账号无论如何不会被动(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """默认解析出来的那个账号（人自己的）必须原样保留，不新建、不改动。"""
    monkeypatch.setenv("SUDO_USER", "noi")
    instance = make_installer(installer_module, workdir)
    assert instance.run_user == "noi"

    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: name == "noi")

    instance.ensure_user()

    assert calls == []
    assert instance._user_created_by_installer() is None


# --------------------------------------------------------------------------- #
# 状态目录属主跟着运行账号，且不是 root
# --------------------------------------------------------------------------- #


def test_状态目录属主是运行账号(installer_module, workdir: Path, monkeypatch) -> None:
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    chowns = []
    monkeypatch.setattr(
        installer_module.shutil,
        "chown",
        lambda path, user=None, group=None: chowns.append((str(path), user)),
    )
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: name == "noi")

    instance.ensure_dirs()

    state = str(workdir / "state")
    assert (state, "noi") in chowns, chowns
    assert all(user != "root" for _path, user in chowns), chowns


def test_运行账号不存在时不硬改属主(installer_module, workdir: Path, monkeypatch) -> None:
    """账号不存在时 chown 会抛 LookupError —— 别制造一条没必要的警告。"""
    instance = make_installer(installer_module, workdir, ["--user", "ghost"])

    chowns = []
    monkeypatch.setattr(
        installer_module.shutil,
        "chown",
        lambda path, user=None, group=None: chowns.append((str(path), user)),
    )
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: False)

    instance.ensure_dirs()

    assert chowns == []


# --------------------------------------------------------------------------- #
# 配置里的路径模板：安装时只换占位符，绝不固化
# --------------------------------------------------------------------------- #


def test_波浪号换成_home_占位符(installer_module, workdir: Path) -> None:
    """``~`` 在安装器里会被解释成 **root 的家** —— 换成 ``{home}`` 从根上消掉它。"""
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    assert instance._template_user_paths("~/Desktop") == "{home}/Desktop"
    assert instance._template_user_paths("~") == "{home}"
    assert instance._template_user_paths("~/code, {home}/x") == "{home}/code, {home}/x"


def test_desktop_模板保持原样(installer_module, workdir: Path) -> None:
    """**不在安装时展开 ``{desktop}``**：镜像预装时用户还没登录过、桌面可能还不
    存在，装的时候探测会固化成错的 ``~/桌面``；运行时展开永远是对的。"""
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    assert instance._template_user_paths("{desktop}/{player_no}") == "{desktop}/{player_no}"
    assert instance._template_user_paths("/srv/code") == "/srv/code"


def test_写进_agent_ini_的是模板而不是绝对路径(
    installer_module, workdir: Path
) -> None:
    instance = make_installer(
        installer_module,
        workdir,
        [
            "--user", "noi",
            "--server", "https://10.0.0.1:8443",
            "--deploy-root", "{desktop}",
            "--scan-root", "~/Desktop/{player_no}",
        ],
    )

    instance.write_config("1.0.0")

    values = read_ini_values(workdir / "etc" / "agent.ini")
    assert values["deploy_root"] == "{desktop}"
    assert values["roots"] == "{home}/Desktop/{player_no}"
    assert "~" not in values["deploy_root"]
    assert "~" not in values["roots"]
    # 运行账号必须进配置：{home}/{desktop} 的展开只认它 —— 注册单元以 root 跑，
    # 没有这一项就只能按"当前是谁在跑"展开，root 会把它算成 /root/桌面。
    assert values["run_user"] == "noi"


# --------------------------------------------------------------------------- #
# 换运行账号之后：属主/权限要跟着新账号（真机 PermissionError 的根因）
# --------------------------------------------------------------------------- #


def test_状态目录已有内容的属主也交给运行账号(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """只改目录属主不够 —— 里面已有的凭据/缓存/日志还是**上一个账号**的名字。"""
    state = workdir / "state"
    state.mkdir()
    credential = state / "credential.json"
    credential.write_text("{}", encoding="utf-8")

    instance = make_installer(installer_module, workdir, ["--user", "noi"])
    chowns = []
    monkeypatch.setattr(
        installer_module.shutil,
        "chown",
        lambda path, user=None, group=None: chowns.append((str(path), user)),
    )
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: name == "noi")

    instance.ensure_dirs()

    assert (str(state), "noi") in chowns, chowns
    assert (str(credential), "noi") in chowns, chowns


def test_已有配置的属主也会跟运行账号对齐(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """现场：旧安装（专用 syncoj）留下的 ``agent.ini`` 是 0600 属 syncoj 的；
    单元已改成以 ``noi`` 运行 → 服务一直
    ``PermissionError: '/etc/syncoj/agent.ini'`` 然后无限重启。

    重跑安装器**不动内容**（教师的手工修改要保住），但属主必须交给新账号。
    """
    etc = workdir / "etc"
    etc.mkdir()
    config_path = etc / "agent.ini"
    original = "[server]\nurl = http://old.invalid\n"
    config_path.write_text(original, encoding="utf-8")

    instance = make_installer(
        installer_module,
        workdir,
        ["--user", "noi", "--server", "https://10.0.0.1:8443"],
    )
    chowns = []
    monkeypatch.setattr(
        installer_module.shutil,
        "chown",
        lambda path, user=None, group=None: chowns.append((str(path), user)),
    )
    monkeypatch.setattr(installer_module.os, "chmod", lambda path, mode: None)

    instance.write_config("1.0.0")

    assert (str(config_path), "noi") in chowns, chowns
    assert config_path.read_text(encoding="utf-8") == original, "内容被改了"


# --------------------------------------------------------------------------- #
# 卸载：只删"安装器建的"账号
# --------------------------------------------------------------------------- #


def test_卸载不删人自己的账号(installer_module, workdir: Path, monkeypatch) -> None:
    """没有"是我建的"证据 → 绝不 userdel。这是灾难级的误删。"""
    instance = make_installer(
        installer_module, workdir, ["--uninstall", "--yes", "--user", "noi"]
    )
    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_which", lambda name: None)
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: name == "noi")
    messages = collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    assert not any(cmd[:1] == ["userdel"] for cmd in calls), calls
    assert "未删除运行账号" in all_text(messages)


def test_卸载会删安装器建过的账号(
    installer_module, workdir: Path, monkeypatch
) -> None:
    instance = make_installer(
        installer_module, workdir, ["--uninstall", "--yes", "--user", "syncoj-new"]
    )
    state = workdir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / installer_module.CREATED_USER_MARKER_FILENAME).write_text(
        "syncoj-new\n", encoding="utf-8"
    )

    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_which", lambda name: None)
    monkeypatch.setattr(
        installer_module, "_user_exists", lambda name: name == "syncoj-new"
    )

    assert instance.uninstall() == installer_module.EXIT_OK

    assert ["userdel", "syncoj-new"] in calls, calls


def test_证据只认名字对得上的那个(installer_module, workdir: Path, monkeypatch) -> None:
    """标记里写的是别的账号（或人换了账号）→ 当前运行账号照样不动。"""
    instance = make_installer(
        installer_module, workdir, ["--uninstall", "--yes", "--user", "noi"]
    )
    state = workdir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / installer_module.CREATED_USER_MARKER_FILENAME).write_text(
        "syncoj\n", encoding="utf-8"
    )

    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_which", lambda name: None)
    monkeypatch.setattr(installer_module, "_user_exists", lambda name: name == "noi")
    messages = collect(instance)

    instance.uninstall()

    assert not any(cmd[:1] == ["userdel"] for cmd in calls), calls
    assert "未删除运行账号" in all_text(messages)


def test_keep_user_仍然跳过(installer_module, workdir: Path, monkeypatch) -> None:
    instance = make_installer(
        installer_module, workdir, ["--uninstall", "--yes", "--keep-user"]
    )
    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_which", lambda name: None)

    did, bad = instance._remove_user("syncoj")

    assert (did, bad) == (False, False)
    assert calls == []


# --------------------------------------------------------------------------- #
# 装完注册单元必须**就地跑一次**
# --------------------------------------------------------------------------- #
#
# 真机事故：`enable` 只写了"开机自启"的软链，本轮装机根本不会执行它 ——
# `systemctl status syncoj-agent-enroll.service` 显示 `inactive (dead)`，看起来
# "没事"，而机器一直没注册上。装完顺手跑一次，凭据当场就有。


def test_装完注册单元就地_start_一次(installer_module, workdir: Path, monkeypatch) -> None:
    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_which", lambda name: "/bin/systemctl")
    instance = make_installer(installer_module, workdir, ["--user", "noi"])
    messages = collect(instance)

    instance.install_enroll_unit(True)

    unit = installer_module.ENROLL_UNIT_FILENAME
    flat = [list(cmd) for cmd in calls]
    assert ["systemctl", "daemon-reload"] in flat
    assert ["systemctl", "enable", unit] in flat
    assert ["systemctl", "reset-failed", unit] in flat, "要先清掉上次的 failed 状态"
    assert ["systemctl", "start", unit] in flat, "装完必须就地跑一次注册单元"
    # start 必须在 enable 之后：反过来的话这次装机仍然没注册
    assert flat.index(["systemctl", "enable", unit]) < flat.index(["systemctl", "start", unit])
    # 成功时要如实说"注册完成"，不能只说"已启用"
    assert "已启用并执行" in all_text(messages)


def test_重复跑安装器也会再_start_一次(installer_module, workdir: Path, monkeypatch) -> None:
    """单元内容没变 ≠ 注册成功过。重跑安装器是"把注册再试一次"的正常手段。

    现场就是这么恢复的：单元文件早就在，缺的只是那一次 start。
    """
    calls = []
    stub_run(monkeypatch, installer_module, calls)
    monkeypatch.setattr(installer_module, "_which", lambda name: "/bin/systemctl")
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    instance.install_enroll_unit(True)
    calls.clear()

    instance.install_enroll_unit(True)  # 内容一致 → 跳过写文件，但**不能**跳过 start

    unit = installer_module.ENROLL_UNIT_FILENAME
    assert ["systemctl", "start", unit] in [list(cmd) for cmd in calls], calls


def test_注册失败时如实报告并给手工步骤(installer_module, workdir: Path, monkeypatch) -> None:
    """``systemctl start`` 失败就**不许**说"已注册"。

    只说"已启用"的话，现场看到的是"服务起来了但没数据"，而真正的原因（注册
    单元执行失败）藏在 journal 里，没人会想到去看。
    """
    calls = []

    def fake_run(cmd, check=True):
        calls.append(list(cmd))
        return 1 if list(cmd)[:2] == ["systemctl", "start"] else 0

    monkeypatch.setattr(installer_module, "run", fake_run)
    monkeypatch.setattr(installer_module, "_which", lambda name: "/bin/systemctl")
    instance = make_installer(installer_module, workdir, ["--user", "noi"])
    messages = collect(instance)

    instance.install_enroll_unit(True)

    text = all_text(messages)
    assert "还没注册上" in text, text
    assert "journalctl" in text, "要告诉人去看哪儿"
    assert "reset-failed" in text and "systemctl start" in text, (
        "要给出能直接抄的手工步骤：%s" % text
    )
    assert "已启用并执行" not in text, "失败了就不能说注册完成"
