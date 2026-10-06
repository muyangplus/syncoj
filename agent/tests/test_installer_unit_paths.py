"""systemd 单元里的**路径指令**：一条不存在的路径就能让服务永远起不来（226）。

现场复现过两次：``ReadWritePaths=`` 里写着模板假定的选手代码目录（
``/home/student/code``），而那台机器上根本没有这个目录。systemd 设沙箱失败：

.. code-block:: text

    Failed to set up mount namespacing: /run/systemd/unit-root/home/student/code: No such file or directory
    ... code=exited, status=226/NAMESPACE

会触发 226 的**只有路径指令**（``ReadWritePaths=`` / ``ReadOnlyPaths=`` /
``BindPaths=`` / ``WorkingDirectory=`` / ``RootDirectory=`` …）；
``ProtectSystem=`` / ``ProtectHome=`` / ``PrivateTmp=`` 这些本身不会，别误改。

所以口径是：**只写安装器确认存在、且确实有理由写的目录，每一条都带 ``-``**
（systemd 对不存在的路径会跳过，而不是让整个服务起不来）；选手的代码/桌面目录
来自可选配置、可能不存在 —— 一律不写进单元，``%h`` 已经覆盖了家目录下的一切。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

#: 所有"路径类"指令 —— 任意一条指向不存在的路径都可能 226。
PATH_DIRECTIVES = (
    "ReadWritePaths",
    "ReadOnlyPaths",
    "BindPaths",
    "BindReadOnlyPaths",
    "InaccessiblePaths",
    "ExecPaths",
    "NoExecPaths",
    "WorkingDirectory",
    "RootDirectory",
    "TemporaryFileSystem",
)


def unit_of(installer_module, **overrides) -> str:
    args = dict(
        prefix=Path("/opt/syncoj"),
        config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"),
        # 故意给"选手的代码/桌面目录"，而且它们在真机上不存在
        deploy_root="/home/student/exam",
        scan_roots="/home/student/code,/srv/other",
        run_user="noi",
        python="/usr/bin/python3",
    )
    args.update(overrides)
    return installer_module.render_unit(**args)


def enroll_unit_of(installer_module) -> str:
    return installer_module.render_enroll_unit(
        prefix=Path("/opt/syncoj"),
        config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"),
        run_user="noi",
        python="/usr/bin/python3",
    )


def test_只看路径指令_每条要么带减号要么是存在的目录(installer_module) -> None:
    """226 的守卫：把单元渲染出来逐条审。

    这条测试同时挡住"悄悄加一条路径指令"—— 出现了没审查过的路径指令就直接红。
    去掉任意一条的 ``-`` 前缀也会让它红。
    """
    text = unit_of(installer_module)

    seen = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() not in PATH_DIRECTIVES:
            continue
        seen.append(key.strip())
        assert key.strip() == "ReadWritePaths", (
            "单元里出现了没审查过的路径指令（它可能 226）：%s" % line
        )
        for token in value.split():
            assert token.startswith("-"), (
                "路径 %s 没有 `-` 前缀 —— 真机上不存在就会 226：%s" % (token, line)
            )

    assert seen == ["ReadWritePaths"], seen


def test_白名单只有带减号的_家目录与安装器自建目录(installer_module) -> None:
    text = unit_of(installer_module)
    lines = [line for line in text.splitlines() if line.startswith("ReadWritePaths=")]
    assert lines == ["ReadWritePaths=-%h -/opt/syncoj -/var/lib/syncoj"], lines
    # 单元里不能出现字面量 `~`（systemd 不展开它）
    assert "~" not in text


def test_不写死选手的代码目录与桌面目录(installer_module) -> None:
    """现场就是 ``/home/student/code`` 这条字面量 226 的 —— 一个都不许留。"""
    text = unit_of(installer_module)

    assert "/home/student" not in text
    for literal in ("/home/student/code", "/home/student/exam", "/srv/other"):
        assert literal not in text, "%s 被写进了单元" % literal
    # 模板占位符也不能漏进单元（systemd 不认识它们）
    assert "{desktop}" not in text
    assert "{player_no}" not in text


def test_单元里的运行账号就是解析出来的那个(installer_module) -> None:
    text = unit_of(installer_module, run_user="noi")
    assert "User=noi" in text
    assert "Group=noi" in text


def test_重启上限在_Unit_段(installer_module) -> None:
    """写在 ``[Service]`` 里的 ``StartLimit*Sec`` 会被 systemd 当未知键忽略 ——
    真机上就那样一路重启到第 28 次。必须在 ``[Unit]`` 段才生效。"""
    text = unit_of(installer_module)
    before, _, after = text.partition("[Service]")

    assert "StartLimitIntervalSec=300" in before
    assert "StartLimitBurst=5" in before
    assert "StartLimitIntervalSec" not in after
    assert "StartLimitBurst" not in after


def test_注册单元的路径白名单也带减号(installer_module) -> None:
    text = enroll_unit_of(installer_module)
    assert "ReadWritePaths=-/var/lib/syncoj" in text
    assert "ReadWritePaths=/var/lib/syncoj" not in text


def test_自愈策略是_on_failure_而不是_always(installer_module) -> None:
    """自卸载成功时 Agent 以 **exit 0** 退出，``Restart=always`` 会立刻把它拉起来
    —— 而那时 /opt/syncoj、/etc/syncoj 已经删了，只会刷"找不到文件"，看起来像
    卸载失败。崩溃（非 0）仍要自愈，所以是 ``on-failure`` 不是 ``no``。"""
    text = unit_of(installer_module)
    assert "Restart=on-failure" in text
    assert "Restart=always" not in text
    assert "Restart=no" not in text


def test_provision_那个_oneshot_单元不带_Restart(installer_module) -> None:
    """注册单元（``--provision`` 那条路）的规则与 Agent 本体无关，别一起改。"""
    text = enroll_unit_of(installer_module)
    assert "Type=oneshot" in text
    assert "Restart" not in text
