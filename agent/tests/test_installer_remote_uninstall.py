"""管理端授权 → 机器本地以 root 卸载：**安装侧产物**。

授权模型：服务端用发布私钥签一枚一次性令牌，机器本地用已有的升级信任锚验签，
验过才以 root 删。这里只测安装器该落的那些东西：

* ``<config-dir>/machine_uuid``（0644 root）—— 权威机器身份，令牌绑它；
* ``/usr/local/lib/syncoj/self_uninstall.sh``（0755）—— **固定路径，不在 current
  软链下**（sudo 的 NOPASSWD 白名单按字面路径匹配、不解析符号链接）；
* ``/usr/local/lib/syncoj/verify_uninstall_token.py``（0644）—— 独立零依赖验签；
* ``/etc/sudoers.d/syncoj-uninstall``（0440）—— 一行 NOPASSWD，落盘前必须过
  ``visudo -c``（半个 sudoers 文件会让 sudo 整体不可用）。
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


@pytest.fixture()
def remote(installer_module, tmp_path: Path, monkeypatch):
    """把辅助件路径指到临时目录，并假装我们在 POSIX 上。

    目标机是 Linux；Windows 开发机上 ``install_uninstall_helper`` 会直接跳过
    （没有 sudoers/systemd 那套东西）。这里把 ``os.name`` 与几个常量换掉，
    就能在不碰真实 ``/etc`` 的前提下把产物写出来检查。
    """
    helper = tmp_path / "lib" / "syncoj"
    sudoers = tmp_path / "etc" / "sudoers.d" / installer_module.SUDOERS_PATH.name
    monkeypatch.setattr(installer_module, "HELPER_DIR", helper)
    monkeypatch.setattr(installer_module, "SELF_UNINSTALL_PATH", helper / "self_uninstall.sh")
    monkeypatch.setattr(
        installer_module, "VERIFY_TOKEN_PATH", helper / "verify_uninstall_token.py"
    )
    monkeypatch.setattr(installer_module, "SUDOERS_PATH", sudoers)
    monkeypatch.setattr(installer_module, "_is_posix_platform", lambda: True)
    return helper, sudoers


def make_installer(installer_module, workdir: Path, argv=()):
    options = installer_module.build_parser().parse_args(list(argv))
    options.config_dir = str(workdir / "etc")
    options.state_dir = str(workdir / "state")
    options.prefix = str(workdir / "opt")
    options.unit_dir = str(workdir / "units")
    return installer_module.Installer(
        options, installer_module.Reporter(dry_run=options.dry_run, quiet=True)
    )


# --------------------------------------------------------------------------- #
# 权威机器身份
# --------------------------------------------------------------------------- #


def test_机器身份写进配置目录且幂等(installer_module, workdir: Path) -> None:
    instance = make_installer(installer_module, workdir)

    first = instance.install_machine_uuid()
    target = workdir / "etc" / installer_module.MACHINE_UUID_FILENAME

    assert target.is_file()
    assert target.read_text(encoding="utf-8").strip() == first
    assert len(first) == 32

    # 第二次必须原样留着 —— 重复装机不该换掉一台机器的身份
    assert instance.install_machine_uuid() == first
    assert target.read_text(encoding="utf-8").strip() == first


@pytest.mark.skipif(os.name != "posix", reason="Windows 没有 POSIX 权限位")
def test_机器身份是_0644(installer_module, workdir: Path) -> None:
    instance = make_installer(installer_module, workdir)
    instance.install_machine_uuid()

    target = workdir / "etc" / installer_module.MACHINE_UUID_FILENAME
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


# --------------------------------------------------------------------------- #
# sudoers
# --------------------------------------------------------------------------- #


def test_sudoers_内容只有一行固定路径(installer_module) -> None:
    content = installer_module.render_uninstall_sudoers("noi")

    assert content == (
        "noi ALL=(root) NOPASSWD: /usr/local/lib/syncoj/self_uninstall.sh\n"
    )
    assert "*" not in content, "白名单里不许有通配符 —— 那会变成'任何东西'"


def test_辅助件落盘内容与权限(installer_module, workdir: Path, remote, monkeypatch) -> None:
    helper, sudoers = remote
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    chmods = []
    real_chmod = os.chmod

    def spy(path, mode, *args, **kwargs):
        chmods.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(installer_module.os, "chmod", spy)

    instance.install_uninstall_helper()

    script = helper / "self_uninstall.sh"
    verifier = helper / "verify_uninstall_token.py"
    assert script.is_file() and verifier.is_file() and sudoers.is_file()

    # 固定权限（记调用，Windows 上 POSIX 位不生效）
    assert (str(script), 0o755) in chmods
    assert (str(verifier), 0o644) in chmods
    assert (str(sudoers), 0o440) in chmods

    assert sudoers.read_text(encoding="utf-8") == installer_module.render_uninstall_sudoers("noi")

    # 脚本里的 @@占位@@ 必须全部替换掉，而且换成的是这台机器的实际布局
    text = script.read_text(encoding="utf-8")
    assert "@@" not in text
    assert (workdir / "opt").as_posix() in text
    assert (workdir / "etc").as_posix() in text
    assert (workdir / "state").as_posix() in text


def test_验签脚本是独立实现(installer_module, remote) -> None:
    helper, _sudoers = remote
    instance = make_installer(installer_module, helper.parent)
    instance.install_uninstall_helper()

    verifier = helper / "verify_uninstall_token.py"
    source = verifier.read_text(encoding="utf-8")

    # 看**真实的 import 语句**（注释里提到它是讲解，不算依赖）
    tree = ast.parse(source, filename=str(verifier))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(
        name.split(".")[0] == "syncoj_agent" for name in imported
    ), "验签脚本不许 import 正在被删的包：%s" % sorted(imported)

    # 它必须是个能编译的纯标准库脚本
    compile(source, str(verifier), "exec")


def test_visudo_校验失败时安装失败且不留临时文件(
    installer_module, workdir: Path, remote, monkeypatch
) -> None:
    helper, sudoers = remote
    instance = make_installer(installer_module, workdir, ["--user", "noi"])

    monkeypatch.setattr(installer_module, "_which", lambda name: "/usr/sbin/visudo")
    monkeypatch.setattr(installer_module, "run", lambda cmd, check=True: 1)

    with pytest.raises(installer_module.InstallError) as exc:
        instance.install_uninstall_helper()

    assert "visudo" in str(exc.value)
    assert not sudoers.exists(), "校验没过却把 sudoers 落盘了"
    tmp = sudoers.parent / (sudoers.name + ".tmp")
    assert not tmp.exists(), "校验失败后临时文件没删"


def test_真实_visudo_能过(installer_module, workdir: Path, remote) -> None:
    """有 visudo 就跑真的 ``visudo -c``；没有就 skip —— **不假装验过**。"""
    visudo = shutil.which("visudo")
    if not visudo:
        pytest.skip("这台机器上没有 visudo（代码里会如实警告'没校验'，不是假装验过）")

    _helper, sudoers = remote
    instance = make_installer(installer_module, workdir, ["--user", "noi"])
    instance.install_uninstall_helper()

    result = subprocess.run(
        [visudo, "-c", "-f", str(sudoers)], capture_output=True
    )
    assert result.returncode == 0, result.stderr


def test_没有_visudo_时如实警告而不是假装验过(
    installer_module, workdir: Path, remote, monkeypatch
) -> None:
    helper, sudoers = remote
    instance = make_installer(installer_module, workdir, ["--user", "noi"])
    monkeypatch.setattr(installer_module, "_which", lambda name: None)

    messages = []
    instance.report.warn = lambda message: messages.append(message)
    instance.install_uninstall_helper()

    assert sudoers.is_file(), "没有 visudo 不该阻止安装（但必须说清没校验）"
    assert any("visudo" in message for message in messages), messages


# --------------------------------------------------------------------------- #
# 卸载时一起清掉
# --------------------------------------------------------------------------- #


def test_local_uninstall_也删辅助件(
    installer_module, workdir: Path, remote, monkeypatch
) -> None:
    helper, sudoers = remote
    helper.mkdir(parents=True)
    (helper / "self_uninstall.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (helper / "verify_uninstall_token.py").write_text("x = 1\n", encoding="utf-8")
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text("noi ALL=(root) NOPASSWD: /x\n", encoding="utf-8")

    instance = make_installer(
        installer_module, workdir, ["--uninstall", "--yes", "--keep-user", "--user", "noi"]
    )
    monkeypatch.setattr(installer_module, "_which", lambda name: None)

    assert instance.uninstall() == installer_module.EXIT_OK
    assert not sudoers.exists()
    assert not helper.exists()
