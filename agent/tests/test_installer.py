"""安装器测试。

把安装器写成 Python（而不是 install.sh）的最大理由就在这个文件：shell 脚本在
这个环境里根本跑不起来（没有可用 bash），我只能交付一个"看起来对"的东西。
Python 版本则每条分支都能被真实执行。
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
PACKAGING = AGENT_ROOT / "packaging"

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from syncoj_agent.upgrade import parse_version, symlinks_supported  # noqa: E402

requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(), reason="当前环境不允许创建符号链接（Windows 需开发者模式）"
)


@pytest.fixture(scope="module")
def installer():
    """把 install.py 当模块加载。它不是包的一部分，只能走 importlib。"""
    spec = importlib.util.spec_from_file_location("syncoj_installer", PACKAGING / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def workdir() -> Iterator[Path]:
    root = REPO_ROOT / ".pytest-tmp" / ("installer-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 构造测试用的安装包
# --------------------------------------------------------------------------- #


def make_bundle(path: Path, version: str = "1.2.3", extra: List[Tuple[str, str, str]] = None) -> Path:
    entries = [
        ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version, "file"),
        ("syncoj_agent/main.py", "print('hi')\n", "file"),
    ]
    entries.extend(extra or [])
    with tarfile.open(str(path), "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                payload = content.encode("utf-8")
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = content
                archive.addfile(info)
            elif kind == "dev":
                info.type = tarfile.CHRTYPE
                archive.addfile(info)
    return path


# --------------------------------------------------------------------------- #
# 安全解包
# --------------------------------------------------------------------------- #


def test_safe_extract_normal(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "ok.tar.gz")
    dest = workdir / "out"
    tops = installer.safe_extract(bundle, dest)

    assert tops == ["syncoj_agent"]
    assert (dest / "syncoj_agent" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "a/../../escape.py", "/etc/cron.d/pwn", "//etc/passwd",
     "C:/Windows/pwn", "a\\..\\..\\pwn.py"],
)
def test_safe_extract_rejects_traversal(installer, workdir: Path, name: str) -> None:
    bundle = make_bundle(workdir / "evil.tar.gz", extra=[(name, "pwned", "file")])
    with pytest.raises(installer.InstallError):
        installer.safe_extract(bundle, workdir / "out")


def test_safe_extract_rejects_symlink(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "sym.tar.gz", extra=[("syncoj_agent/link", "/etc", "sym")])
    with pytest.raises(installer.InstallError, match="链接"):
        installer.safe_extract(bundle, workdir / "out")


def test_safe_extract_rejects_device(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "dev.tar.gz", extra=[("syncoj_agent/null", "", "dev")])
    with pytest.raises(installer.InstallError, match="设备|节点"):
        installer.safe_extract(bundle, workdir / "out")


def test_safe_extract_rejects_non_tar(installer, workdir: Path) -> None:
    bogus = workdir / "not.tar.gz"
    bogus.write_bytes(b"definitely not a tar file")
    with pytest.raises(installer.InstallError, match="tar"):
        installer.safe_extract(bogus, workdir / "out")


def test_safe_extract_rejects_oversized(installer, workdir: Path, monkeypatch) -> None:
    monkeypatch.setattr(installer, "MAX_EXTRACTED_BYTES", 10)
    bundle = make_bundle(workdir / "bomb.tar.gz", extra=[("syncoj_agent/big.bin", "x" * 5000, "file")])
    with pytest.raises(installer.InstallError, match="总大小"):
        installer.safe_extract(bundle, workdir / "out")


# --------------------------------------------------------------------------- #
# 版本识别
# --------------------------------------------------------------------------- #


def test_read_bundle_version(installer, workdir: Path) -> None:
    dest = workdir / "extracted"
    installer.safe_extract(make_bundle(workdir / "v.tar.gz", version="4.5.6"), dest)
    assert installer.read_bundle_version(dest) == "4.5.6"


def test_read_bundle_version_rejects_wrong_layout(installer, workdir: Path) -> None:
    with pytest.raises(installer.InstallError, match="不是 SyncOJ Agent 包"):
        installer.read_bundle_version(workdir)


def test_read_bundle_version_rejects_missing_version(installer, workdir: Path) -> None:
    pkg = workdir / "syncoj_agent"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# 没有 __version__\n", encoding="utf-8")
    with pytest.raises(installer.InstallError, match="__version__"):
        installer.read_bundle_version(workdir)


# --------------------------------------------------------------------------- #
# 配置与单元模板
# --------------------------------------------------------------------------- #


def test_render_config_contains_all_settings(installer) -> None:
    text = installer.render_config(
        server_url="https://10.0.0.1:8443",
        verify_tls=True,
        ca_file="/etc/syncoj/ca.pem",
        enroll_code="AAAA-BBBB",
        state_dir=Path("/var/lib/syncoj"),
        deploy_root=Path("/home/student/exam"),
        scan_roots="/home/student/code",
        upgrade_mode="off",
        install_root=Path("/opt/syncoj"),
        public_key="",
    )
    assert "url = https://10.0.0.1:8443" in text
    assert "verify_tls = true" in text
    assert "ca_file = /etc/syncoj/ca.pem" in text
    assert "enroll_code = AAAA-BBBB" in text
    assert "roots = /home/student/code" in text
    assert "mode = off" in text


def test_rendered_config_is_parseable(installer, workdir: Path) -> None:
    """生成出来的配置必须能被 Agent 的解析器读进去 —— 否则安装成功却起不来。"""
    from syncoj_agent.config import AgentConfig

    text = installer.render_config(
        server_url="https://10.0.0.1:8443",
        verify_tls=True,
        ca_file="",
        enroll_code="AAAA-BBBB",
        state_dir=Path("/var/lib/syncoj"),
        deploy_root=Path("/home/student/exam"),
        scan_roots="/home/student/code",
        upgrade_mode="off",
        install_root=Path("/opt/syncoj"),
        public_key="",
    )
    config_file = workdir / "agent.ini"
    config_file.write_text(text, encoding="utf-8")

    config = AgentConfig.load(config_file)
    assert config.server_url == "https://10.0.0.1:8443"
    assert config.enroll_code == "AAAA-BBBB"
    assert config.upgrade_mode == "off"
    assert config.deploy_root == Path("/home/student/exam")


def test_rendered_paths_are_always_posix(installer) -> None:
    """生成的配置与单元只被 Linux 读取，里面的路径必须恒为正斜杠。

    安装器可能在 Windows 上被运行（开发机/镜像构建机），若直接 ``str(Path)``
    会写出 ``\\opt\\syncoj`` —— 拿到目标机上就是废的，而问题只在现场暴露。
    """
    config_text = installer.render_config(
        server_url="https://x", verify_tls=True, ca_file="",
        enroll_code="", state_dir=Path("/var/lib/syncoj"),
        deploy_root=Path("/home/student/exam"), scan_roots="/home/student/code",
        upgrade_mode="off", install_root=Path("/opt/syncoj"), public_key="",
    )
    unit_text = installer.render_unit(
        prefix=Path("/opt/syncoj"), config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"), deploy_root=Path("/home/student/exam"),
        scan_roots="/home/student/code", run_user="syncoj", python="/usr/bin/python3",
    )

    for text, label in ((config_text, "config"), (unit_text, "unit")):
        assert "\\" not in text, "%s 中出现了反斜杠路径" % label
        assert "/var/lib/syncoj" in text
        assert "/home/student/exam" in text


def test_render_config_with_insecure_tls(installer) -> None:
    text = installer.render_config(
        server_url="http://127.0.0.1:8000", verify_tls=False, ca_file="",
        enroll_code="", state_dir=Path("/s"), deploy_root=Path("/d"),
        scan_roots="/c", upgrade_mode="off", install_root=Path("/o"), public_key="",
    )
    assert "verify_tls = false" in text


def test_render_unit_uses_configured_paths(installer) -> None:
    """systemd 的读写白名单必须跟着实际配置走。

    写死成 /home/student/code 的话，换个扫描目录就会被 ProtectSystem=strict
    挡住，表现为"服务起来了但什么都不传" —— 最难排查的那类故障。
    """
    text = installer.render_unit(
        prefix=Path("/opt/syncoj"),
        config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"),
        deploy_root=Path("/srv/exam"),
        scan_roots="/srv/contest/code, /srv/contest/backup",
        run_user="syncoj",
        python="/usr/bin/python3",
    )
    assert "ReadWritePaths=/var/lib/syncoj /srv/exam" in text
    assert "ReadOnlyPaths=/srv/contest/code /srv/contest/backup" in text
    assert "/home/student" not in text


def test_render_unit_uses_current_symlink(installer) -> None:
    """ExecStart 必须走 current 软链 —— 自更新的原子切换靠它生效。"""
    text = installer.render_unit(
        prefix=Path("/opt/syncoj"), config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"), deploy_root=Path("/d"),
        scan_roots="/c", run_user="u", python="/usr/bin/python3",
    )
    assert "ExecStart=/usr/bin/python3 -E -s /opt/syncoj/current/syncoj_agent/main.py" in text
    # -E 忽略 PYTHON* 环境变量，-s 忽略 user site-packages：
    # 选手怎么 pip install 都污染不到 Agent
    assert " -E -s " in text
    assert "StandardOutput=null" in text
    assert "NoNewPrivileges=yes" in text


def test_render_unit_without_scan_roots(installer) -> None:
    text = installer.render_unit(
        prefix=Path("/o"), config_path=Path("/c"), state_dir=Path("/s"),
        deploy_root=Path("/d"), scan_roots="", run_user="u", python="python3",
    )
    assert "ReadOnlyPaths" not in text


# --------------------------------------------------------------------------- #
# 命令行与流程（dry-run 不需要 root，可在任何平台跑）
# --------------------------------------------------------------------------- #


def run_installer(installer, argv: List[str]) -> int:
    return installer.main(argv)


def test_dry_run_makes_no_changes(installer, workdir: Path) -> None:
    prefix = workdir / "opt" / "syncoj"
    code = workdir / "code"
    code.mkdir()

    rc = run_installer(installer, [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--server", "https://127.0.0.1:8443",
        "--scan-root", str(code),
        "--prefix", str(prefix),
        "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--dry-run", "--skip-user", "--skip-service", "--quiet",
    ])

    assert rc == 0
    assert not prefix.exists(), "dry-run 不得创建任何东西"
    assert not (workdir / "etc").exists()


def test_requires_exactly_one_source(installer, workdir: Path) -> None:
    rc = run_installer(installer, ["--dry-run", "--skip-user", "--quiet"])
    assert rc == installer.EXIT_ERROR

    rc = run_installer(installer, [
        "--dry-run", "--skip-user", "--quiet",
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--download-url", "https://example.invalid/x.tar.gz",
    ])
    assert rc == installer.EXIT_ERROR


def test_missing_bundle_is_reported(installer, workdir: Path) -> None:
    rc = run_installer(installer, [
        "--bundle", str(workdir / "nope.tar.gz"),
        "--dry-run", "--skip-user", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR


@requires_symlinks
def test_full_install_and_idempotency(installer, workdir: Path) -> None:
    """完整安装 + 重复执行 —— 幂等性的核心断言是"第二次不产生任何改动"。"""
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    state_dir = workdir / "state"
    code = workdir / "code"
    code.mkdir()
    bundle = make_bundle(workdir / "b.tar.gz", version="1.0.0")

    base = [
        "--bundle", str(bundle),
        "--server", "https://10.0.0.1:8443",
        "--enroll-code", "AAAA-BBBB",
        "--scan-root", str(code),
        "--deploy-root", str(workdir / "exam"),
        "--prefix", str(prefix),
        "--config-dir", str(config_dir),
        "--state-dir", str(state_dir),
        "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, base) == 0

    release = prefix / "releases" / "1.0.0"
    assert (release / "syncoj_agent" / "main.py").is_file()
    assert installer.current_version(prefix) == "1.0.0"
    assert (config_dir / "agent.ini").is_file()
    assert (workdir / "units" / "syncoj-agent.service").is_file()

    # 第二次执行：结果必须一致
    assert run_installer(installer, base) == 0
    assert installer.current_version(prefix) == "1.0.0"


@requires_symlinks
def test_existing_config_is_preserved(installer, workdir: Path) -> None:
    """**教师的手工修改绝不能被冲掉。**

    现场很可能已经改过扫描目录或注册码。安装器把它们覆盖是灾难性的 ——
    而且现场往往是在"重装一下试试"的时候才踩到。
    """
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "agent.ini"
    config_file.write_text("[server]\nurl = https://edited-by-teacher\n", encoding="utf-8")
    code = workdir / "code"
    code.mkdir()

    base = [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--server", "https://new-server",
        "--scan-root", str(code),
        "--prefix", str(prefix),
        "--config-dir", str(config_dir),
        "--state-dir", str(workdir / "state"),
        "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, base) == 0
    assert "edited-by-teacher" in config_file.read_text(encoding="utf-8")


@requires_symlinks
def test_force_config_overwrites(installer, workdir: Path) -> None:
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "agent.ini"
    config_file.write_text("[server]\nurl = https://old\n", encoding="utf-8")
    code = workdir / "code"
    code.mkdir()

    rc = run_installer(installer, [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--server", "https://new", "--scan-root", str(code),
        "--prefix", str(prefix), "--config-dir", str(config_dir),
        "--state-dir", str(workdir / "state"), "--unit-dir", str(workdir / "units"),
        "--force-config", "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == 0
    assert "https://new" in config_file.read_text(encoding="utf-8")


@requires_symlinks
def test_upgrade_to_new_version_switches_symlink(installer, workdir: Path) -> None:
    prefix = workdir / "opt" / "syncoj"
    code = workdir / "code"
    code.mkdir()

    common = [
        "--server", "https://x", "--scan-root", str(code),
        "--prefix", str(prefix), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"), "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, common + ["--bundle", str(make_bundle(workdir / "v1.tar.gz", "1.0.0"))]) == 0
    assert installer.current_version(prefix) == "1.0.0"

    assert run_installer(installer, common + ["--bundle", str(make_bundle(workdir / "v2.tar.gz", "2.0.0"))]) == 0
    assert installer.current_version(prefix) == "2.0.0"

    # 旧版本保留，供回滚
    assert (prefix / "releases" / "1.0.0").is_dir()
    assert (prefix / "releases" / "2.0.0").is_dir()


@requires_symlinks
def test_install_from_directory(installer, workdir: Path) -> None:
    """镜像预装路径：直接从已解开的目录复制，不需要打包。"""
    source = workdir / "source" / "syncoj_agent"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('__version__ = "7.7.7"\n', encoding="utf-8")
    (source / "main.py").write_text("print('x')\n", encoding="utf-8")
    code = workdir / "code"
    code.mkdir()

    rc = run_installer(installer, [
        "--from-dir", str(workdir / "source"),
        "--server", "https://x", "--scan-root", str(code),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"), "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == 0
    assert installer.current_version(workdir / "opt") == "7.7.7"


def test_sha256_mismatch_is_rejected(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz")
    rc = run_installer(installer, [
        "--bundle", str(bundle),
        "--sha256", "0" * 64,
        "--server", "https://x", "--scan-root", str(workdir),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR
    assert not (workdir / "opt" / "releases").exists() or not list(
        (workdir / "opt" / "releases").iterdir()
    )


def test_sha256_malformed_is_rejected(installer, workdir: Path) -> None:
    rc = run_installer(installer, [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--sha256", "zzz",
        "--server", "https://x", "--scan-root", str(workdir),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR


def test_rejects_bundle_with_wrong_top_level(installer, workdir: Path) -> None:
    """名字像压缩包的普通文件不该被当成 Agent 装进去。"""
    bundle = workdir / "wrong.tar.gz"
    with tarfile.open(str(bundle), "w:gz") as archive:
        payload = b"hello"
        info = tarfile.TarInfo("somethingelse/readme.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    rc = run_installer(installer, [
        "--bundle", str(bundle),
        "--server", "https://x", "--scan-root", str(workdir),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR


# --------------------------------------------------------------------------- #
# 打包器
# --------------------------------------------------------------------------- #


def test_build_bundle_produces_installable_archive(workdir: Path) -> None:
    """打包器产出的包必须与安装器的期望结构一致 —— 这是三处约定的交汇点
    （build_bundle / install.py / upgrade.safe_extract_tar）。"""
    spec = importlib.util.spec_from_file_location(
        "syncoj_build_bundle", PACKAGING / "build_bundle.py"
    )
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    out = workdir / "bundle.tar.gz"
    version, size, digest = builder.build(out, AGENT_ROOT)

    assert out.is_file() and size > 0 and len(digest) == 64
    assert parse_version(version), "产出的版本号必须可解析"

    with tarfile.open(str(out)) as archive:
        names = [m.name for m in archive.getmembers()]
    tops = {n.split("/")[0] for n in names}
    assert tops == {"syncoj_agent"}
    assert "syncoj_agent/main.py" in names
    assert not any("__pycache__" in n for n in names)
    assert not any(n.startswith("syncoj_agent/tests/") for n in names)


def test_build_bundle_is_reproducible(workdir: Path) -> None:
    """同样的内容必须产出同样的字节，**哪怕输出文件名不同**。

    否则"同一版本两次打包校验和不同"，签名的确定性与"这个包是不是那个包"的
    判断都会失效。注意这里刻意用了两个不同的文件名 —— gzip 头里的 FNAME 字段
    默认会带上输出文件名，那是这个坑最隐蔽的地方。
    """
    spec = importlib.util.spec_from_file_location(
        "syncoj_build_bundle2", PACKAGING / "build_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first = workdir / "version-a.tar.gz"
    second = workdir / "version-b-with-longer-name.tar.gz"
    _v1, _s1, d1 = module.build(first, AGENT_ROOT)
    _v2, _s2, d2 = module.build(second, AGENT_ROOT)

    assert d1 == d2, "同样内容两次打包的 sha256 不同"
    assert first.read_bytes() == second.read_bytes()
