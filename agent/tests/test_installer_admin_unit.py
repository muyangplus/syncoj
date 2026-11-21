"""root 管理单元：Agent 把升级 / 卸载请求放进交接目录，root oneshot 处理。

Agent 以选手账号、``NoNewPrivileges=yes`` 运行，写不进 ``/opt/syncoj``，也不能提权。
需要 root 的两件事 —— 切换已验签的升级、按管理端授权卸载 —— 由
``install.py --admin-handoff`` 处理。这里钉住：

* 单元文本（path 单元盯的是哪个文件、service 带了哪些布局参数）；
* 升级：自己再验一遍签名（覆盖版本号与大小），然后走装机同一条路切到新版本；
  签名不对就拒绝、版本不动；
* 交接目录被换成符号链接时 root 不顺着它读写；
* 卸载：令牌验过才卸、验不过不卸。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from test_installer import make_bundle  # noqa: E402

try:
    from syncoj_server.services.signing import (  # noqa: E402
        generate_keypair,
        openssl_available,
        write_public_key_json,
    )

    HAS_OPENSSL = openssl_available() is not None
except ImportError:  # pragma: no cover
    HAS_OPENSSL = False

requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")
posix_only = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="只在 Linux 上有意义")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prefix = root / "opt"
        self.config = root / "etc"
        self.state = root / "state"
        self.units = root / "units"
        self.admin = root / "admin"
        self.pub = root / "release-key.pub.json"

    def args(self):
        return [
            "--prefix", str(self.prefix),
            "--config-dir", str(self.config),
            "--state-dir", str(self.state),
            "--unit-dir", str(self.units),
            "--admin-dir", str(self.admin),
            "--public-key", str(self.pub),
            "--server", "http://10.0.0.1:8000",
            "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
        ]

    @property
    def handoff(self) -> Path:
        return self.state / "handoff"


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    return generate_keypair(tmp_path_factory.mktemp("k") / "release.pem", bits=2048)


@pytest.fixture()
def layout(tmp_path: Path, installer_module, key) -> Layout:
    lay = Layout(tmp_path)
    write_public_key_json(lay.pub, key)
    bundle = make_bundle(tmp_path / "v1.tar.gz", version="1.0.0")
    assert installer_module.main(["--bundle", str(bundle)] + lay.args()) == 0
    assert installer_module.current_version(lay.prefix) == "1.0.0"
    lay.handoff.mkdir(parents=True, exist_ok=True)
    return lay


def stage_upgrade(lay: Layout, key, version: str = "2.0.0", sign_version: str = None) -> None:
    import hashlib

    bundle = make_bundle(lay.root / ("src-%s.tar.gz" % version), version=version)
    raw = bundle.read_bytes()
    (lay.state / "upgrade").mkdir(parents=True, exist_ok=True)
    (lay.state / "upgrade" / ("%s.tar.gz" % version)).write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    message = "syncoj-release-v1\n%s\n%s\n%d\n" % (sign_version or version, sha, len(raw))
    request = {
        "kind": "upgrade", "version": version, "sha256": sha, "size": len(raw),
        "signature": b64url(key.sign(message.encode("ascii"))),
    }
    (lay.handoff / "request.json").write_text(json.dumps(request), encoding="utf-8")


def result_of(lay: Layout) -> dict:
    return json.loads((lay.handoff / "result.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 单元文本
# --------------------------------------------------------------------------- #


def test_管理单元盯交接目录里的请求文件(installer_module) -> None:
    service, path_unit = installer_module.render_admin_units(
        "/usr/bin/python3", Path("/usr/lib/syncoj/install.py"), Path("/var/lib/syncoj"),
        ["--prefix", "/opt/syncoj", "--user", "noi"],
    )
    assert "PathExists=/var/lib/syncoj/handoff/request.json" in path_unit
    assert "Unit=syncoj-agent-admin.service" in path_unit
    assert "Type=oneshot" in service
    assert (
        "ExecStart=/usr/bin/python3 -E -s /usr/lib/syncoj/install.py --admin-handoff "
        "--prefix /opt/syncoj --user noi"
    ) in service
    assert "User=" not in service, "管理单元以 root 运行"


# --------------------------------------------------------------------------- #
# 升级
# --------------------------------------------------------------------------- #


@posix_only
@requires_openssl
def test_验过签名的升级请求切到新版本(installer_module, layout: Layout, key) -> None:
    stage_upgrade(layout, key)

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0

    assert installer_module.current_version(layout.prefix) == "2.0.0"
    assert not (layout.handoff / "request.json").exists(), "请求要被取走"
    assert result_of(layout)["ok"] is True


@posix_only
@requires_openssl
def test_签名覆盖版本号_换了版本号就拒绝(installer_module, layout: Layout, key) -> None:
    stage_upgrade(layout, key, version="2.0.0", sign_version="1.9.9")

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 1

    assert installer_module.current_version(layout.prefix) == "1.0.0"
    result = result_of(layout)
    assert result["ok"] is False and "签名" in result["message"]
    assert not (layout.handoff / "request.json").exists(), "坏请求也要取走，免得反复触发"


@posix_only
@requires_openssl
def test_交接目录被换成符号链接时不顺着它读写(installer_module, layout: Layout, key, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stage_upgrade(layout, key)
    (layout.handoff / "request.json").rename(elsewhere / "request.json")
    layout.handoff.rmdir()
    layout.handoff.symlink_to(elsewhere, target_is_directory=True)

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0

    assert installer_module.current_version(layout.prefix) == "1.0.0"
    assert (elsewhere / "request.json").exists(), "不该顺着符号链接取走请求"
    assert not (elsewhere / "result.json").exists(), "不该顺着符号链接写回执"


@posix_only
@requires_openssl
def test_没有请求时什么都不做(installer_module, layout: Layout) -> None:
    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0
    assert installer_module.current_version(layout.prefix) == "1.0.0"


# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #


def mint_uninstall_token(key, machine_uuid: str) -> str:
    from syncoj_server.services import uninstall

    return uninstall.mint_token(
        key, machine_uuid=machine_uuid, agent_id=1, nonce="n" * 16, issued_at=int(time.time())
    )


@posix_only
@requires_openssl
def test_卸载令牌验过才卸(installer_module, layout: Layout, key) -> None:
    machine_uuid = (layout.config / "machine_uuid").read_text(encoding="utf-8").strip()
    token = mint_uninstall_token(key, machine_uuid)
    (layout.handoff / "request.json").write_text(
        json.dumps({"kind": "uninstall", "token": token}), encoding="utf-8"
    )

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0

    assert not layout.prefix.exists()
    assert not layout.config.exists()


@posix_only
@requires_openssl
def test_别的机器的卸载令牌不卸(installer_module, layout: Layout, key) -> None:
    token = mint_uninstall_token(key, "f" * 32)
    (layout.handoff / "request.json").write_text(
        json.dumps({"kind": "uninstall", "token": token}), encoding="utf-8"
    )

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 1

    assert installer_module.current_version(layout.prefix) == "1.0.0"
    result = result_of(layout)
    assert result["ok"] is False and "令牌" in result["message"]


@posix_only
@requires_openssl
def test_不许降级(installer_module, layout: Layout, key) -> None:
    """请求文件是运行账号写的：真签名的旧版本也不能让 root 装回去。"""
    stage_upgrade(layout, key, version="0.9.0")

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 1

    assert installer_module.current_version(layout.prefix) == "1.0.0"
    assert "降级" in result_of(layout)["message"]


@posix_only
def test_读不了的请求也被挪走_不会反复触发(installer_module, layout: Layout, tmp_path) -> None:
    target = tmp_path / "secret"
    target.write_text("x", encoding="utf-8")
    (layout.handoff / "request.json").symlink_to(target)

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 1

    assert not os.path.lexists(str(layout.handoff / "request.json"))
    assert target.read_text(encoding="utf-8") == "x"


def test_装机时给的公钥路径带进管理单元(installer_module, tmp_path) -> None:
    import argparse

    args = installer_module.build_parser().parse_args([
        "--prefix", str(tmp_path / "opt"), "--public-key", "/etc/custom/pub.json",
        "--user", "noi",
    ])
    instance = installer_module.Installer(args, installer_module.Reporter(dry_run=True, quiet=True))
    layout_args = instance._layout_args()
    assert layout_args[layout_args.index("--public-key") + 1] == "/etc/custom/pub.json"


@posix_only
@requires_openssl
def test_交接目录被换成符号链接时重装不把链接目标交出去(
    installer_module, layout: Layout, tmp_path
) -> None:
    """装机 / 升级以 root 建交接目录：选手把它换成指向 /etc 的链接，root 不能顺着它 chmod / chown。"""
    victim = tmp_path / "etc-like"
    victim.mkdir()
    os.chmod(str(victim), 0o755)
    layout.handoff.rmdir()
    layout.handoff.symlink_to(victim, target_is_directory=True)

    bundle = make_bundle(tmp_path / "v1b.tar.gz", version="1.0.0")
    args = [a for a in layout.args() if a != "--skip-service"]
    import unittest.mock as mock

    with mock.patch.object(installer_module, "_is_posix_platform", return_value=True):
        installer_module.Installer(
            installer_module.build_parser().parse_args(["--bundle", str(bundle)] + args),
            installer_module.Reporter(dry_run=False, quiet=True),
        ).install_admin_unit()

    assert (victim.stat().st_mode & 0o777) == 0o755, "链接目标的权限被改了"
    assert not layout.handoff.is_symlink() and layout.handoff.is_dir()
    assert (layout.handoff.stat().st_mode & 0o777) == 0o700


# --------------------------------------------------------------------------- #
# 启动守卫（root 一侧）
# --------------------------------------------------------------------------- #


def _install_two(installer_module, layout: Layout, tmp_path) -> None:
    """装上 1.0.0 之后再装 2.0.0，并像 root 管理单元那样记下回滚点。"""
    bundle = make_bundle(tmp_path / "v2.tar.gz", version="2.0.0")
    assert installer_module.main(["--bundle", str(bundle)] + layout.args()) == 0
    installer_module._save_upgrade_state(
        layout.prefix, {"version": "2.0.0", "previous": "1.0.0", "boots": 0}
    )


def _guard(installer_module, layout: Layout) -> int:
    return installer_module.main(["--boot-guard"] + layout.args())


def test_启动守卫数够次数就切回上一版(installer_module, layout: Layout, tmp_path) -> None:
    _install_two(installer_module, layout, tmp_path)
    for _ in range(installer_module.MAX_BOOT_ATTEMPTS):
        assert _guard(installer_module, layout) == 0
        assert installer_module.current_version(layout.prefix) == "2.0.0"

    assert _guard(installer_module, layout) == 0
    assert installer_module.current_version(layout.prefix) == "1.0.0"
    assert installer_module._load_upgrade_state(layout.prefix) is None


def test_启动守卫_状态对不上当前版本就清掉(installer_module, layout: Layout) -> None:
    installer_module._save_upgrade_state(
        layout.prefix, {"version": "9.9.9", "previous": "1.0.0", "boots": 5}
    )
    assert _guard(installer_module, layout) == 0
    assert installer_module.current_version(layout.prefix) == "1.0.0"
    assert installer_module._load_upgrade_state(layout.prefix) is None


def test_启动守卫自己出错也返回0(installer_module, layout: Layout, monkeypatch) -> None:
    def boom(self):
        raise RuntimeError("坏了")

    monkeypatch.setattr(installer_module.Installer, "boot_guard", boom)
    assert _guard(installer_module, layout) == 0, "ExecStartPre 失败会挡住服务启动"


@posix_only
def test_平稳运行的报告清掉回滚状态_版本对不上不清(installer_module, layout: Layout, tmp_path) -> None:
    _install_two(installer_module, layout, tmp_path)
    request = layout.handoff / "request.json"

    request.write_text(json.dumps({"kind": "healthy", "version": "1.0.0"}), encoding="utf-8")
    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0
    assert installer_module._load_upgrade_state(layout.prefix) is not None

    request.write_text(json.dumps({"kind": "healthy", "version": "2.0.0"}), encoding="utf-8")
    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0
    assert installer_module._load_upgrade_state(layout.prefix) is None
    assert not (layout.handoff / "result.json").exists(), "平稳报告不写回执"


@posix_only
@requires_openssl
def test_经管理单元升级时记下回滚点(installer_module, layout: Layout, key) -> None:
    stage_upgrade(layout, key)
    assert installer_module.main(["--admin-handoff"] + layout.args()) == 0
    state = installer_module._load_upgrade_state(layout.prefix)
    assert state["version"] == "2.0.0" and state["previous"] == "1.0.0"


def test_单元以root跑启动守卫_失败不挡启动(installer_module, tmp_path) -> None:
    args = installer_module.build_parser().parse_args([
        "--prefix", str(tmp_path / "opt"), "--user", "noi",
    ])
    instance = installer_module.Installer(args, installer_module.Reporter(dry_run=True, quiet=True))
    command = instance._boot_guard_command()
    if command is None:
        pytest.skip("非 POSIX")
    text = installer_module.render_unit(
        prefix=Path("/opt/syncoj"), config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"), deploy_root="{desktop}",
        scan_roots="{desktop}/{player_no}", run_user="noi", python="/usr/bin/python3",
        boot_guard=command,
    )
    line = [l for l in text.splitlines() if l.startswith("ExecStartPre=")]
    assert len(line) == 1 and line[0].startswith("ExecStartPre=-+/usr/bin/python3 -E -s ")
    assert "--boot-guard" in line[0]


def test_启动次数上限与Agent一致(installer_module) -> None:
    from syncoj_agent import upgrade

    assert installer_module.MAX_BOOT_ATTEMPTS == upgrade.MAX_BOOT_ATTEMPTS
    assert installer_module.UPGRADE_STATE_FILE == upgrade.STATE_FILE


def test_回滚过的版本记下来_管理单元不再切过去(installer_module, layout: Layout, tmp_path) -> None:
    _install_two(installer_module, layout, tmp_path)
    for _ in range(installer_module.MAX_BOOT_ATTEMPTS + 1):
        _guard(installer_module, layout)
    assert installer_module.current_version(layout.prefix) == "1.0.0"
    assert installer_module._rolled_back_versions(layout.prefix) == ["2.0.0"]

    from syncoj_agent import upgrade

    assert upgrade.rolled_back_versions(layout.prefix) == ["2.0.0"], "Agent 读得到同一份记录"
    assert installer_module.ROLLED_BACK_FILE == upgrade.ROLLED_BACK_FILE


@posix_only
@requires_openssl
def test_管理单元拒绝切到回滚过的版本(installer_module, layout: Layout, key) -> None:
    installer_module._note_rolled_back(layout.prefix, "2.0.0")
    stage_upgrade(layout, key, version="2.0.0")

    assert installer_module.main(["--admin-handoff"] + layout.args()) == 1
    assert installer_module.current_version(layout.prefix) == "1.0.0"
    assert "回滚" in result_of(layout)["message"]
