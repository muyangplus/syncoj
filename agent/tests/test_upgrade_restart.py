"""自更新止血：切换版本后能被 systemd 拉起、失败不拖垮主循环、版本号不能穿路径。

这三件事都曾经是真缺陷（重构计划阶段 0 第 7 步）：

* 激活新版本 / 启动守卫回滚之后 ``run_forever`` 以 ``exit 0`` 退出，而单元是
  ``Restart=on-failure`` —— 铺开一个版本，整个机房停摆到重启机器为止；
* 运行账号写不进安装根时 ``stage_release`` 抛 ``PermissionError``，穿出
  ``_handle_upgrade`` 被 ``run_forever`` 的兜底接住：每一轮的事件不落、退避涨到
  300 秒，而且每轮都重下一遍发布包；
* 版本号不在签名覆盖范围内，又被直接拼进路径：``"1/../../x"`` 能把包写出
  ``releases/`` 之外（下载落点那一次甚至在验签之前）。
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from syncoj_agent import main as main_module
from syncoj_agent.upgrade import (
    ReleaseManifest,
    UpgradeError,
    UpgradeMode,
    stage_release,
    validate_release_version,
)


def make_agent(workdir: Path):
    from syncoj_agent.config import AgentConfig

    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "desktop"
    config.scan_roots = [workdir / "code"]
    config.bootstrap_key_file = workdir / "bootstrap.key"
    config.server_url = "https://127.0.0.1:8000"
    config.install_root = workdir / "opt"
    for path in (config.state_dir, config.deploy_root, config.scan_roots[0]):
        path.mkdir(parents=True, exist_ok=True)
    config.bootstrap_key_file.write_text("S\n", encoding="utf-8")
    agent = main_module.Agent(config)
    # 签名在这里不是被测对象：verify_bundle 会被替身掉，只要"有公钥"这一关过得去
    agent._public_key = object()
    return agent


def make_bundle(path: Path) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"print(1)\n"
        info = tarfile.TarInfo("run_agent.py")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture()
def agent(workdir: Path):
    instance = make_agent(workdir)
    try:
        yield instance
    finally:
        instance.client.close()


def counting_download(bundle: Path, counter: dict):
    def download(url, dest, max_bytes):
        counter["n"] = counter.get("n", 0) + 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(bundle), str(dest))
        return bundle.stat().st_size

    return download


def manifest_for(bundle: Path, version: str = "99.0.0") -> dict:
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    return {"version": version, "url": "/x", "sha256": sha, "signature": "x"}


# --------------------------------------------------------------------------- #
# 1) 切换版本的退出码
# --------------------------------------------------------------------------- #


def test_激活新版本后以非零的专用退出码退出(agent, monkeypatch) -> None:
    """``Restart=on-failure`` 不会拉起 exit 0 —— 退出码必须非 0。"""
    monkeypatch.setattr(agent, "_guard_boot", lambda: False)
    agent._restart_requested = True

    code = agent.run_forever()

    assert code == main_module.EXIT_RESTART
    assert code != 0


def test_启动守卫回滚后同样以专用退出码退出(agent, monkeypatch) -> None:
    monkeypatch.setattr(agent, "_guard_boot", lambda: True)

    assert agent.run_forever() == main_module.EXIT_RESTART


def test_正常停止仍然是退出码_0(agent, monkeypatch) -> None:
    """SIGTERM（systemctl stop / 卸载）不能被误报成"要重启"。"""
    monkeypatch.setattr(agent, "_guard_boot", lambda: False)
    agent._stop = True

    assert agent.run_forever() == 0


def test_单元模板认这个退出码(installer_module) -> None:
    """安装器是单文件分发、不能 import Agent 包，退出码在两边各写一份 —— 这里对账。"""
    assert installer_module.AGENT_EXIT_RESTART == main_module.EXIT_RESTART
    text = installer_module.render_unit(
        prefix=Path("/opt/syncoj"),
        config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"),
        deploy_root="{desktop}",
        scan_roots="{desktop}/{player_no}",
        run_user="student",
        python="/usr/bin/python3",
    )
    lines = text.splitlines()
    assert "Restart=always" in lines
    assert "SuccessExitStatus=%d" % main_module.EXIT_RESTART in lines
    assert "RestartForceExitStatus=%d" % main_module.EXIT_RESTART in lines


# --------------------------------------------------------------------------- #
# 2) 升级失败不拖垮主循环，也不每轮重下
# --------------------------------------------------------------------------- #


def test_写不进安装根时不抛出_也不再每轮重下(agent, workdir: Path, monkeypatch) -> None:
    agent.config.upgrade_mode = UpgradeMode.APPLY
    bundle = make_bundle(workdir / "b.tar.gz")
    downloads: dict = {}
    monkeypatch.setattr(agent.client, "download_to_file", counting_download(bundle, downloads))
    monkeypatch.setattr(main_module, "verify_bundle", lambda *args: None)

    def denied(*args):
        raise PermissionError(13, "Permission denied: /opt/syncoj/releases/.staging")

    monkeypatch.setattr(main_module, "stage_release", denied)
    # 这条测的是"自己往安装根里放"那条（开发机）路径的失败：假装安装根可写
    monkeypatch.setattr(main_module, "_writable", lambda root: True)
    info = manifest_for(bundle)

    for _ in range(3):
        agent._handle_upgrade(info)  # 不许抛出

    assert downloads["n"] == 1, "同一份清单失败后不该每轮重下：下了 %d 次" % downloads["n"]
    failures = [e for e in agent._pending_events if e["category"] == "upgrade_failed"]
    assert len(failures) == 1, failures
    assert agent._restart_requested is False


def test_换了内容的清单会再试一次(agent, workdir: Path, monkeypatch) -> None:
    agent.config.upgrade_mode = UpgradeMode.APPLY
    bundle = make_bundle(workdir / "b.tar.gz")
    downloads: dict = {}
    monkeypatch.setattr(agent.client, "download_to_file", counting_download(bundle, downloads))
    monkeypatch.setattr(main_module, "verify_bundle", lambda *args: None)
    monkeypatch.setattr(
        main_module, "stage_release",
        lambda *args: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    monkeypatch.setattr(main_module, "_writable", lambda root: True)

    agent._handle_upgrade(manifest_for(bundle, "99.0.0"))
    agent._handle_upgrade(manifest_for(bundle, "99.0.1"))

    assert downloads["n"] == 2


def test_stage_模式暂存成功后不再每轮重下(agent, workdir: Path, monkeypatch) -> None:
    agent.config.upgrade_mode = UpgradeMode.STAGE
    bundle = make_bundle(workdir / "b.tar.gz")
    downloads: dict = {}
    monkeypatch.setattr(agent.client, "download_to_file", counting_download(bundle, downloads))
    monkeypatch.setattr(main_module, "verify_bundle", lambda *args: None)
    info = manifest_for(bundle)

    for _ in range(3):
        agent._handle_upgrade(info)

    assert downloads["n"] == 1
    staged = [e for e in agent._pending_events if e["category"] == "upgrade_staged"]
    assert len(staged) == 1


# --------------------------------------------------------------------------- #
# 3) 版本号形状
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", ["0.1.0", "1.2.3-rc1", "1.2.3+build.7", "10"])
def test_正常版本号照常接受(version: str) -> None:
    assert validate_release_version(version) == version


@pytest.mark.parametrize(
    "version",
    ["1/../../x", "1..2", "../1", "1.2/3", ".1", "1\\x", "", "1.2.3-", "a1", "1" * 65],
)
def test_能穿路径或不可比较的版本号一律拒绝(version: str) -> None:
    with pytest.raises(UpgradeError):
        validate_release_version(version)


def test_清单解析时就拒绝穿路径的版本号() -> None:
    """下载落点 ``state_dir/upgrade/<v>.tar.gz`` 在**验签之前**就拼好了，
    所以必须在解析清单这一步拦住，不能等到 stage。"""
    with pytest.raises(UpgradeError):
        ReleaseManifest.from_dict(
            {"version": "1/../../evil", "url": "/x", "sha256": "0" * 64}
        )


def test_暂存时也拒绝_且什么都不落盘(workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz")
    root = workdir / "opt"

    with pytest.raises(UpgradeError):
        stage_release(root, "1/../../escaped", bundle)

    assert not root.exists(), "形状不对就不该建任何目录"
    assert not (workdir / "escaped").exists()


def test_安装根写不进时把验过签的包交给_root_管理单元(agent, workdir: Path, monkeypatch) -> None:
    """真机上的常态：/opt/syncoj 属 root。Agent 不再自己切换，而是写一份交接请求。"""
    import json

    agent.config.upgrade_mode = UpgradeMode.APPLY
    bundle = make_bundle(workdir / "b.tar.gz")
    downloads: dict = {}
    monkeypatch.setattr(agent.client, "download_to_file", counting_download(bundle, downloads))
    monkeypatch.setattr(main_module, "verify_bundle", lambda *args: None)
    monkeypatch.setattr(main_module, "_writable", lambda root: False)
    monkeypatch.setattr(
        main_module, "stage_release",
        lambda *args: (_ for _ in ()).throw(AssertionError("不该自己暂存")),
    )
    info = manifest_for(bundle)

    for _ in range(3):
        agent._handle_upgrade(info)

    assert downloads["n"] == 1
    request = json.loads(
        (Path(agent.config.state_dir) / "handoff" / "request.json").read_text(encoding="utf-8")
    )
    assert request["kind"] == "upgrade"
    assert request["version"] == info["version"]
    assert request["sha256"] == info["sha256"]
    assert request["signature"] == info.get("signature", "")
    assert agent._restart_requested is False, "切换与重启由 root 那边做"
    assert [e["category"] for e in agent._pending_events].count("upgrade_handoff") == 1
