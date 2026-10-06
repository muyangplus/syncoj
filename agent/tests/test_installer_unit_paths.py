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
        unit_dir=Path("/etc/systemd/system"),
        helper_dir=Path("/usr/local/lib/syncoj"),
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
    assert lines == [
        "ReadWritePaths=-%h -/opt/syncoj -/var/lib/syncoj "
        "-/etc/syncoj -/etc/systemd/system -/etc/sudoers.d -/usr/local/lib/syncoj"
    ], lines
    # 单元里不能出现字面量 `~`（systemd 不展开它）
    assert "~" not in text


def test_远程卸载那四条都是带减号的可写目录(installer_module) -> None:
    """**(a) 变体的核心**：sudo 出来的 root 子进程在**同一个 mount namespace** 里，
    不放开这四条，删 ``/etc/syncoj`` / 单元文件 / ``sudoers`` / 辅助脚本全是 ``EROFS``。

    每一条都必须带 `-`：这些目录在真机上不一定存在（比如没装远程辅助件的机器），
    而 ``ReadWritePaths=`` 里一条不存在的路径就会 226，整个服务起不来。
    """
    text = unit_of(installer_module)
    line = [x for x in text.splitlines() if x.startswith("ReadWritePaths=")][0]

    for path in (
        "/etc/syncoj",  # 配置目录：卸载要删 agent.ini / bootstrap.key / machine_uuid
        "/etc/systemd/system",  # 单元目录：卸载要删两个 .service 与 *.wants 软链
        "/etc/sudoers.d",  # sudoers 规则
        "/usr/local/lib/syncoj",  # 自卸载脚本 + 验签脚本
    ):
        assert "-%s" % path in line, "%s 没有被放开写权限：%s" % (path, line)
        assert " %s" % path not in line, "%-不带的 %s 会在目录不存在时 226" % (path, path)

    # 去重逻辑保持：一个路径只能出现一次
    tokens = line.split("=", 1)[1].split()
    assert len(tokens) == len(set(tokens)), tokens


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
    # **刻意不写 Group=**：systemd 按 NSS 解析主组；写死 Group=<账号名> 会在
    # "主组与账号不同名"的账号上撞 217/GROUP。
    assert "Group=" not in text


def test_没有_NoNewPrivileges_但其它加固都还在(installer_module) -> None:
    """``NoNewPrivileges=yes`` 会让 setuid 的 sudo 无法提权 —— 管理端授权的远程
    卸载（``sudo -n self_uninstall.sh``）整条链就是死的。服务以普通账号运行，
    那个账号本人在本机同样能跑任何 setuid 程序，所以这一条对"防选手"没有增量
    价值。其余加固必须保留。"""
    text = unit_of(installer_module)

    assert "NoNewPrivileges" not in text
    for hardening in (
        "ProtectSystem=strict",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6",
        "LockPersonality=yes",
    ):
        assert hardening in text, hardening


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


def test_不放开_AF_UNIX(installer_module) -> None:
    """**刻意不放开 AF_UNIX** —— 那正是 D-Bus / systemctl 的传输层。

    自卸载脚本里的 ``systemctl disable`` 只是 best-effort（连不上 D-Bus 时
    静默失败）；真正保证"重启后不回来"的是删 ``*.wants/`` 软链那一步。
    谁要是为了"让 systemctl 能跑"顺手把 AF_UNIX 加回来，这条就红。
    """
    text = unit_of(installer_module)

    assert "RestrictAddressFamilies=AF_INET AF_INET6" in text
    assert "AF_UNIX" not in text


def test_sudoers_白名单是无参数的固定路径(installer_module) -> None:
    """sudoers 那一行一旦带上参数或通配符，"白名单"就变成"任何东西"。"""
    line = installer_module.render_uninstall_sudoers("noi")

    assert line == "noi ALL=(root) NOPASSWD: /usr/local/lib/syncoj/self_uninstall.sh\n"
    for forbidden in ("*", "?", "ALL", "/bin/sh", "sudoers.d"):
        assert forbidden not in line.replace("noi ALL=(root)", "")


def test_自卸载脚本用_glob_删掉两个单元的开机自启软链(installer_module) -> None:
    """**这才是"卸载之后重启不会回来"的保证。**

    ``systemctl disable`` 在沙箱里可能连不上 D-Bus（AF_UNIX 是关着的），失败还是
    静默的；所以脚本必须**自己**把 ``*.wants/`` 下的软链删掉，而且两个单元
    （Agent 本体 + 注册单元）都要删、要覆盖任何一个 target 的 wants 目录。

    但只能按单元名匹配 —— 整目录 rm 会连带删掉别人的服务。
    """
    text = installer_module._SELF_UNINSTALL_SCRIPT

    assert '"$UNIT_DIR"/*.wants/"$SERVICE"' in text, "没有 glob 掉本体的自启软链"
    assert '"$UNIT_DIR"/*.wants/"$ENROLL_UNIT"' in text, "没有 glob 掉注册单元的自启软链"
    assert "multi-user.target.wants/$SERVICE" not in text, "写死 target 会漏掉别的 wants 目录"
    # 绝不允许整目录删除（那会删掉同机别的服务）
    assert 'rm -rf "$UNIT_DIR"' not in text
    assert '"$UNIT_DIR"/*.wants/*' not in text


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
