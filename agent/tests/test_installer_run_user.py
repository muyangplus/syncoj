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


def test_命令行默认值就是跑安装的人(installer_module, monkeypatch) -> None:
    monkeypatch.setenv("SUDO_USER", "noi")
    options = installer_module.build_parser().parse_args([])
    assert options.user == "noi"


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
# 家目录 / 桌面的展开
# --------------------------------------------------------------------------- #


def test_SUDO_USER_的家目录用来展开桌面(
    installer_module, workdir: Path, monkeypatch
) -> None:
    home = workdir / "home" / "noi"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("SUDO_USER", "noi")
    monkeypatch.setattr(installer_module, "_current_user_name", lambda: "root")
    monkeypatch.setattr(installer_module, "_home_of", lambda user: home)

    assert installer_module.default_run_user() == "noi"
    instance = make_installer(installer_module, workdir)
    assert instance.run_user == "noi"

    expanded = instance._expand_user_paths("~/Desktop,{desktop}/{player_no}")
    assert expanded == "%s, %s/{player_no}" % (home / "Desktop", home / "Desktop")


def test_展开同时兼容中文桌面与_xdg(
    installer_module, workdir: Path, monkeypatch
) -> None:
    home = workdir / "home" / "noi"
    (home / "桌面").mkdir(parents=True)
    monkeypatch.setattr(installer_module, "_home_of", lambda user: home)
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    assert instance._expand_user_paths("{desktop}") == str(home / "桌面")


def test_探测不出桌面时按家目录下的桌面猜(
    installer_module, workdir: Path, monkeypatch
) -> None:
    """家目录里没有任何桌面目录时：猜 ``~/桌面``，**绝不能**写死别的路径。

    退回家目录（或写死一个 ``/home/student/...``）都会让 Agent 扫错地方 ——
    前者把整个家目录当工作区，后者在真机上根本不存在。
    """
    home = workdir / "home" / "noi"
    home.mkdir(parents=True)
    monkeypatch.setattr(installer_module, "_home_of", lambda user: home)
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    assert instance._expand_user_paths("{desktop}") == str(home / "桌面")


def test_展开不出来时保留模板而不是写死构建机路径(
    installer_module, workdir: Path, monkeypatch
) -> None:
    monkeypatch.setattr(installer_module, "_home_of", lambda user: None)
    instance = make_installer(installer_module, workdir, ["--user", "ghost"])

    assert instance._expand_user_paths("{desktop}/{player_no}") == "{desktop}/{player_no}"
    assert instance._expand_user_paths("~/x") == "~/x"


def test_写进_agent_ini_的是展开后的绝对路径(
    installer_module, workdir: Path, monkeypatch
) -> None:
    home = workdir / "home" / "noi"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(installer_module, "_home_of", lambda user: home)
    instance = make_installer(
        installer_module,
        workdir,
        ["--user", "noi", "--server", "https://10.0.0.1:8443"],
    )

    instance.write_config("1.0.0")

    text = (workdir / "etc" / "agent.ini").read_text(encoding="utf-8")
    assert str(home / "Desktop") in text

    # 只看**配置项的值**：注释里解释 {desktop} 的用法是应该留的
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    assert values["deploy_root"] == str(home / "Desktop")
    assert values["roots"].startswith(str(home / "Desktop"))
    assert "{desktop}" not in values["deploy_root"]
    assert "{desktop}" not in values["roots"]
    assert "~" not in values["deploy_root"]
    assert "~" not in values["roots"]


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
