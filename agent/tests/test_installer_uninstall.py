"""``install.py --uninstall``：干净地重来一次。

现场的现实是"装了几遍之后要能重新来"。卸载工具的危险恰恰在**看起来很简单**，
所以这些用例盯着三件事：

* **幂等**：没装过的机器照样成功，并且明说"什么都没做"；
* **--dry-run 真的什么都不改**：现场最需要的正是"先看一眼再决定"；
* **只删自己建的东西**：``--prefix/--config-dir/--state-dir/--unit-dir`` 都是
  可覆盖的，删之前要确认"这看起来是 SyncOJ 的安装吗"，否则宁可不卸。

非交互（``curl … | sh -s -- --uninstall``）**必须**显式 ``--yes``：卸载会删掉
统一注册密钥与本机身份，不可逆。这一条在下面有专门的用例。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


@pytest.fixture()
def no_systemctl(monkeypatch, installer_module):
    """用例不去碰真实的 systemd。

    开发机/CI 上可能真的装着服务或别的单元，这个补丁把"调用 init 系统"这一步
    换成"找不到 systemctl"那条分支 —— 单元文件的删除逻辑照样被完整执行。
    """
    monkeypatch.setattr(installer_module, "_which", lambda name: None)


def make_installed(installer_module, workdir: Path, version: str = "1.0.0"):
    """造一份"装过的样子"：三个目录 + 两个单元文件。"""
    prefix = workdir / "opt" / "syncoj"
    config = workdir / "etc" / "syncoj"
    state = workdir / "var" / "lib" / "syncoj"
    units = workdir / "units"

    release = prefix / "releases" / version
    (release / "syncoj_agent").mkdir(parents=True)
    (release / "syncoj_agent" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    config.mkdir(parents=True)
    (config / installer_module.CONFIG_FILENAME).write_text("[server]\n", encoding="utf-8")
    (config / installer_module.BOOTSTRAP_KEY_FILENAME).write_text("SECRET\n", encoding="utf-8")
    (config / installer_module.PUBLIC_KEY_FILENAME).write_text("{}\n", encoding="utf-8")

    state.mkdir(parents=True)
    (state / "credential.json").write_text("{}\n", encoding="utf-8")
    (state / "machine_uuid").write_text("uuid\n", encoding="utf-8")
    (state / "agent.log").write_text("log\n", encoding="utf-8")

    units.mkdir(parents=True)
    (units / installer_module.UNIT_FILENAME).write_text("[Unit]\n", encoding="utf-8")
    (units / installer_module.ENROLL_UNIT_FILENAME).write_text("[Unit]\n", encoding="utf-8")

    return SimpleNamespace(prefix=prefix, config=config, state=state, units=units)


def uninstall_args(installer_module, layout, extra=()):
    return [
        "--uninstall",
        "--prefix", str(layout.prefix),
        "--config-dir", str(layout.config),
        "--state-dir", str(layout.state),
        "--unit-dir", str(layout.units),
    ] + list(extra)


def make_installer(installer_module, layout, extra=()):
    options = installer_module.build_parser().parse_args(
        uninstall_args(installer_module, layout, extra)
    )
    return installer_module.Installer(
        options, installer_module.Reporter(dry_run=options.dry_run, quiet=True)
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


def artifacts(installer_module, layout):
    """"装过"的全部痕迹，用来逐路径断言。"""
    return [
        layout.prefix / "releases",
        layout.config / installer_module.CONFIG_FILENAME,
        layout.config / installer_module.BOOTSTRAP_KEY_FILENAME,
        layout.config / installer_module.PUBLIC_KEY_FILENAME,
        layout.state / "credential.json",
        layout.state / "machine_uuid",
        layout.state / "agent.log",
        layout.units / installer_module.UNIT_FILENAME,
        layout.units / installer_module.ENROLL_UNIT_FILENAME,
    ]


# --------------------------------------------------------------------------- #
# 1) 能删干净
# --------------------------------------------------------------------------- #


def test_卸载会删掉机器身份_sudoers_与辅助目录(
    installer_module, workdir: Path, monkeypatch, no_systemctl
) -> None:
    """远程卸载那一套辅助件也必须跟着走。

    留下 sudoers 规则 = 留下一条**常驻**的"运行账号可以免密以 root 执行某个
    固定脚本"的授权；留下辅助目录 = 留一份能验令牌、能删系统的脚本；
    留下 ``machine_uuid`` = 本机身份还在（远程卸载令牌绑的就是它）。
    """
    layout = make_installed(installer_module, workdir)

    # 这三样在真机上都在系统目录里（/etc/sudoers.d、/usr/local/lib/syncoj），
    # 测试里把它们指到 workdir —— 常量是可注入的。
    sudoers = workdir / "sudoers.d" / "syncoj-uninstall"
    helper = workdir / "usr" / "local" / "lib" / "syncoj"
    monkeypatch.setattr(installer_module, "SUDOERS_PATH", sudoers)
    monkeypatch.setattr(installer_module, "HELPER_DIR", helper)
    monkeypatch.setattr(
        installer_module, "SELF_UNINSTALL_PATH", helper / "self_uninstall.sh"
    )
    monkeypatch.setattr(
        installer_module, "VERIFY_TOKEN_PATH", helper / "verify_uninstall_token.py"
    )

    sudoers.parent.mkdir(parents=True)
    sudoers.write_text(
        "noi ALL=(root) NOPASSWD: %s\n" % (helper / "self_uninstall.sh"), encoding="utf-8"
    )
    helper.mkdir(parents=True)
    (helper / "self_uninstall.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (helper / "verify_uninstall_token.py").write_text("# verify\n", encoding="utf-8")
    # 权威机器身份：安装器写在配置目录旁边（不是状态目录里那份）
    uuid_file = layout.config / "machine_uuid"
    uuid_file.write_text("uuid\n", encoding="utf-8")

    instance = make_installer(installer_module, layout, extra=["--yes", "--keep-user"])
    messages = collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    assert not uuid_file.exists(), "machine_uuid 没删（远程卸载令牌绑的就是它）"
    assert not sudoers.exists(), "sudoers 规则没删"
    assert not helper.exists(), "辅助脚本目录没删"
    text = all_text(messages)
    assert str(sudoers) in text, text
    assert str(helper) in text, text


def test_辅助目录不像我们的就不动(
    installer_module, workdir: Path, monkeypatch, no_systemctl
) -> None:
    """同名目录里没有那两个脚本 → 不是我们的，宁可不删（别人的东西不可逆）。"""
    layout = make_installed(installer_module, workdir)
    helper = workdir / "usr" / "local" / "lib" / "syncoj"
    monkeypatch.setattr(installer_module, "HELPER_DIR", helper)
    monkeypatch.setattr(
        installer_module, "SELF_UNINSTALL_PATH", helper / "self_uninstall.sh"
    )
    monkeypatch.setattr(
        installer_module, "VERIFY_TOKEN_PATH", helper / "verify_uninstall_token.py"
    )
    monkeypatch.setattr(
        installer_module, "SUDOERS_PATH", workdir / "sudoers.d" / "syncoj-uninstall"
    )
    helper.mkdir(parents=True)
    (helper / "someone-else.txt").write_text("not ours\n", encoding="utf-8")

    instance = make_installer(installer_module, layout, extra=["--yes", "--keep-user"])
    collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    assert helper.is_dir(), "把别人的同名目录删了"


def test_卸载把装过的东西删干净(installer_module, workdir: Path, no_systemctl) -> None:
    layout = make_installed(installer_module, workdir)
    instance = make_installer(installer_module, layout, extra=["--yes", "--keep-user"])
    messages = collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    assert not layout.prefix.exists(), "安装根目录没删"
    assert not layout.config.exists(), "配置目录（含凭据）没删"
    assert not layout.state.exists(), "状态目录（含本机凭据）没删"
    assert not (layout.units / installer_module.UNIT_FILENAME).exists()
    assert not (layout.units / installer_module.ENROLL_UNIT_FILENAME).exists()

    # 回执里每一项都要点到名 —— 现场靠它核对到底删了什么
    text = all_text(messages)
    for needle in (
        str(layout.units / installer_module.UNIT_FILENAME),
        str(layout.units / installer_module.ENROLL_UNIT_FILENAME),
        str(layout.prefix),
        str(layout.config),
        str(layout.state),
    ):
        assert needle in text, "回执里没提到 %s：\n%s" % (needle, text)
    assert "已删除" in text
    # 凭据要单独点名 —— "卸载必须 --yes"就是因为它删这些
    assert str(layout.config / installer_module.BOOTSTRAP_KEY_FILENAME) in text
    assert "统一注册密钥" in text
    # 状态目录里的本机凭据也要点名（路径按 POSIX 渲染，Windows 上只比对文件名）
    assert "credential.json" in text
    assert "machine_uuid" in text


def test_卸载是幂等的_第二次说什么都没做(
    installer_module, workdir: Path, no_systemctl
) -> None:
    layout = make_installed(installer_module, workdir)
    first = make_installer(installer_module, layout, extra=["--yes", "--keep-user"])
    assert first.uninstall() == installer_module.EXIT_OK

    second = make_installer(installer_module, layout, extra=["--yes", "--keep-user"])
    messages = collect(second)

    assert second.uninstall() == installer_module.EXIT_OK, "没装过也必须是成功，不是错误"
    assert "这台机器上没有装 Agent，什么都没做" in all_text(messages)


# --------------------------------------------------------------------------- #
# 2) --dry-run 必须真的什么都不改
# --------------------------------------------------------------------------- #


def test_dry_run_只列计划不动任何东西(
    installer_module, workdir: Path, no_systemctl
) -> None:
    layout = make_installed(installer_module, workdir)
    before = artifacts(installer_module, layout)
    assert all(path.exists() for path in before)

    # 不给 --yes：dry-run 是安全的，不该被确认拦下来
    instance = make_installer(installer_module, layout, extra=["--dry-run"])
    messages = collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    for path in before:
        assert path.exists(), "dry-run 动了 %s" % path
    tags = {tag for tag, _message in messages}
    assert "action" not in tags, "dry-run 不该产生任何实际动作：%r" % (messages,)

    text = all_text(messages)
    # 计划里要列出会停/删哪些单元与路径
    assert "systemctl disable --now %s" % installer_module.UNIT_FILENAME in text
    assert "systemctl disable --now %s" % installer_module.ENROLL_UNIT_FILENAME in text
    assert str(layout.prefix) in text
    assert str(layout.config) in text
    assert str(layout.state) in text
    # 预览也要说清会碰哪些凭据
    assert "统一注册密钥" in text
    assert "未做任何改动" in text


# --------------------------------------------------------------------------- #
# 3) 非交互必须显式 --yes
# --------------------------------------------------------------------------- #


def test_非交互没有yes就拒绝且什么都不删(
    installer_module, workdir: Path, monkeypatch, capsys, no_systemctl
) -> None:
    """``curl … | sh -s -- --uninstall`` 就是这样：stdin 不是终端。"""
    layout = make_installed(installer_module, workdir)
    before = artifacts(installer_module, layout)

    # 管道的等价物：stdin 不是终端，且没有 --yes
    monkeypatch.setattr(installer_module.sys, "stdin", io.StringIO(""))

    rc = installer_module.main(
        uninstall_args(installer_module, layout, extra=["--keep-user"])
    )

    assert rc == installer_module.EXIT_ERROR
    for path in before:
        assert path.exists(), "被拒绝了却还是删了 %s" % path

    captured = capsys.readouterr()
    assert "不可逆" in captured.err
    assert "--yes" in captured.err


def test_交互确认时打对名字才执行(
    installer_module, workdir: Path, monkeypatch, no_systemctl
) -> None:
    """交互时的规矩：不是问"是否确定"，而是把名字原样打一遍。"""
    layout = make_installed(installer_module, workdir)

    monkeypatch.setattr(installer_module.sys, "stdin", _FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "syncoj-agent")

    rc = installer_module.main(uninstall_args(installer_module, layout, extra=["--keep-user"]))

    assert rc == installer_module.EXIT_OK
    assert not layout.prefix.exists()
    assert not layout.config.exists()
    assert not layout.state.exists()


def test_交互确认打错名字就取消(
    installer_module, workdir: Path, monkeypatch, no_systemctl
) -> None:
    layout = make_installed(installer_module, workdir)
    before = artifacts(installer_module, layout)

    monkeypatch.setattr(installer_module.sys, "stdin", _FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    rc = installer_module.main(uninstall_args(installer_module, layout, extra=["--keep-user"]))

    assert rc == installer_module.EXIT_ERROR
    for path in before:
        assert path.exists(), "确认不匹配却删了 %s" % path


class _FakeTty:
    def isatty(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# 4) --keep-state
# --------------------------------------------------------------------------- #


def test_keep_state_时状态目录留下其余照删(
    installer_module, workdir: Path, no_systemctl
) -> None:
    layout = make_installed(installer_module, workdir)
    instance = make_installer(
        installer_module, layout, extra=["--yes", "--keep-user", "--keep-state"]
    )
    messages = collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    assert layout.state.is_dir(), "--keep-state 却把状态目录删了"
    assert (layout.state / "credential.json").is_file()
    assert (layout.state / "agent.log").is_file(), "日志是复盘用的，--keep-state 必须留住"

    assert not layout.prefix.exists()
    assert not layout.config.exists()
    assert not (layout.units / installer_module.UNIT_FILENAME).exists()
    assert not (layout.units / installer_module.ENROLL_UNIT_FILENAME).exists()

    assert "保留状态目录" in all_text(messages)


# --------------------------------------------------------------------------- #
# 5) 只删自己建的东西
# --------------------------------------------------------------------------- #


def test_不像_SyncOJ_的目录不会被误删(
    installer_module, workdir: Path, no_systemctl
) -> None:
    """``--prefix`` 是可覆盖的 —— 指到别人的目录时必须**跳过**而不是清掉。"""
    foreign = workdir / "opt" / "syncoj"
    (foreign / "someones-data").mkdir(parents=True)
    (foreign / "keep.txt").write_text("do not delete\n", encoding="utf-8")

    layout = SimpleNamespace(
        prefix=foreign,
        config=workdir / "etc" / "syncoj",
        state=workdir / "state" / "syncoj",
        units=workdir / "units",
    )
    instance = make_installer(installer_module, layout, extra=["--yes", "--keep-user"])
    messages = collect(instance)

    assert instance.uninstall() == installer_module.EXIT_OK

    assert (foreign / "keep.txt").is_file(), "把别人的目录删了"
    assert (foreign / "someones-data").is_dir()
    assert "看起来不是 SyncOJ" in all_text(messages)
    # 什么都没删 → 明确说"什么都没做"
    assert "这台机器上没有装 Agent，什么都没做" in all_text(messages)
