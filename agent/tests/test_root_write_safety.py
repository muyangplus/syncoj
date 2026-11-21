"""root 往选手可控的目录里写东西时不顺着符号链接；安装器不受调用方 umask 影响。

这几条都是在 systemd 245 容器冒烟里实测出来的：

* 安装器在 umask 0 下（docker exec、某些镜像构建器）装出 0777 的 /opt/syncoj、/etc/syncoj；
* 开机注册单元以 root 在属选手的状态目录里写凭据、写日志、chown —— 一个预先放好的
  符号链接就能让 root 改写 / 交出任意文件；
* root 先建出的 agent.log 选手服务打不开，整台机器没有日志。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from syncoj_agent import main as agent_main
from syncoj_agent.config import AgentConfig
from syncoj_agent.state import atomic_write_text

posix_only = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="只在 Linux 上有意义")


@posix_only
def test_原子写不顺着残留的临时名符号链接(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("原内容", encoding="utf-8")
    target = tmp_path / "credential.json"
    (tmp_path / "credential.json.tmp").symlink_to(victim)

    atomic_write_text(target, "{}")

    assert victim.read_text(encoding="utf-8") == "原内容"
    assert target.read_text(encoding="utf-8") == "{}"
    assert not target.is_symlink()


@posix_only
def test_交出状态文件时跳过符号链接(tmp_path: Path, monkeypatch) -> None:
    import pwd

    state = tmp_path / "state"
    state.mkdir()
    victim = tmp_path / "shadow"
    victim.write_text("x", encoding="utf-8")
    (state / "credential.json").symlink_to(victim)
    (state / "agent.log").write_text("", encoding="utf-8")

    calls = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid, follow_symlinks=True: calls.append(
        (Path(path).name, follow_symlinks)
    ))
    config = AgentConfig(state_dir=state)
    agent_main._chown_state_files(config, pwd.getpwuid(os.getuid()).pw_name)

    names = [name for name, _ in calls]
    assert "credential.json" not in names, "符号链接不该被 chown"
    assert "agent.log" in names, "root 建出来的日志要交给运行账号"
    assert all(follow is False for _, follow in calls)


def test_注册路径日志只进stderr_不建日志文件(tmp_path: Path) -> None:
    import logging

    config = AgentConfig(state_dir=tmp_path)
    agent_main.setup_logging(config, to_file=False)
    try:
        handlers = logging.getLogger("syncoj").handlers
        assert not any(isinstance(h, logging.FileHandler) for h in handlers)
        assert not (tmp_path / "agent.log").exists()
    finally:
        logging.getLogger("syncoj").handlers = []


@posix_only
def test_安装器不受调用方umask影响(installer_module, tmp_path: Path) -> None:
    from test_installer import make_bundle

    bundle = make_bundle(tmp_path / "v1.tar.gz", version="1.0.0")
    prefix, config = tmp_path / "opt", tmp_path / "etc"
    previous = os.umask(0)
    try:
        assert installer_module.main([
            "--bundle", str(bundle), "--prefix", str(prefix), "--config-dir", str(config),
            "--state-dir", str(tmp_path / "state"), "--unit-dir", str(tmp_path / "units"),
            "--server", "http://10.0.0.1:8000",
            "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
        ]) == 0
        assert os.umask(0) == 0, "进程内调用结束后要还原调用方的 umask"
    finally:
        os.umask(previous)

    for path in (prefix, prefix / "releases", config):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & 0o022, "%s 是 %o，组 / 其他人可写" % (path, mode)
