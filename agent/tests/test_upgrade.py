"""自更新测试。

重点在**安全解包**。``tarfile.extractall`` 在 Python 3.12 之前对路径穿越与
符号链接逃逸没有任何防护，而 Python 3.8 上连 ``filter="data"`` 参数都不存在。
一个恶意升级包如果能写到 ``/etc/cron.d/``，就完全绕过了签名机制的全部意义。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Iterator, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
SERVER_DIR = REPO_ROOT / "server"
for candidate in (AGENT_DIR, SERVER_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from syncoj_agent.rsa import RSAPublicKey  # noqa: E402
from syncoj_agent.upgrade import (  # noqa: E402
    MAX_EXTRACTED_BYTES,
    UpgradeError,
    ReleaseManifest,
    activate_release,
    current_release,
    list_releases,
    note_boot,
    parse_version,
    prune_releases,
    rollback_release,
    safe_extract_tar,
    stage_release,
    symlinks_supported,
    verify_bundle,
)

try:
    from syncoj_server.services.signing import generate_keypair, openssl_available

    HAS_OPENSSL = openssl_available() is not None
except ImportError:  # pragma: no cover
    HAS_OPENSSL = False

requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

#: 目标平台是 Linux，符号链接必然可用。Windows 上需要开发者模式或管理员权限，
#: 否则 os.symlink 抛 WinError 1314 —— 那是平台限制，不是实现问题。
requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(), reason="当前环境不允许创建符号链接（Windows 需开发者模式）"
)
#: Windows 没有 POSIX 权限位
requires_posix_modes = pytest.mark.skipif(
    os.name != "posix", reason="Windows 没有 POSIX 权限位"
)


@pytest.fixture(scope="module")
def work_root() -> Iterator[Path]:
    root = REPO_ROOT / ".pytest-tmp" / ("upgrade-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def signing_key(work_root: Path):
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    return generate_keypair(work_root / "release.pem", bits=2048)


@pytest.fixture()
def workdir() -> Iterator[Path]:
    root = REPO_ROOT / ".pytest-tmp" / ("upgrade-case-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# tar 构造辅助
# --------------------------------------------------------------------------- #


def make_tar(path: Path, entries) -> Path:
    """``entries`` 是 ``[(name, content, kind)]``，kind 可为 file/dir/sym/hard/dev。"""
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                payload = content if isinstance(content, bytes) else content.encode("utf-8")
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = content
                archive.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = content
                archive.addfile(info)
            elif kind == "dev":
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                archive.addfile(info)
            else:  # pragma: no cover
                raise AssertionError("未知 kind: %s" % kind)
    return path


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("0.1.0", (0, 1, 0)),
        ("2", (2,)),
        ("1.2.3-rc1", (1, 2, 3)),
        (" 1.2.3 ", (1, 2, 3)),
        ("10.20.30", (10, 20, 30)),
    ],
)
def test_parse_version(text: str, expected) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "v", None, 123])
def test_parse_version_rejects_garbage(text) -> None:
    with pytest.raises(UpgradeError):
        parse_version(text)


def test_version_ordering_is_numeric_not_lexical() -> None:
    """1.10.0 必须大于 1.9.0 —— 字符串比较会得出相反结论。"""
    assert parse_version("1.10.0") > parse_version("1.9.0")


# --------------------------------------------------------------------------- #
# 清单校验
# --------------------------------------------------------------------------- #


def test_manifest_from_dict() -> None:
    manifest = ReleaseManifest.from_dict(
        {
            "version": "1.2.3",
            "url": "/api/v1/agent/releases/1",
            "sha256": "a" * 64,
            "size": 1024,
            "signature": "sig",
        }
    )
    assert manifest.version == "1.2.3"
    assert manifest.sha256 == "a" * 64


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": "1", "url": "/x"},                                  # 缺 sha256
        {"version": "1", "url": "/x", "sha256": "zz"},                  # sha256 非法
        {"version": "1", "url": "/x", "sha256": "a" * 64, "size": -5},  # 负数大小
        {"version": "1", "url": "/x", "sha256": "a" * 64, "size": 10 ** 9 * 300},
        "not a dict",
        None,
    ],
)
def test_manifest_rejects_bad_input(payload) -> None:
    with pytest.raises(UpgradeError):
        ReleaseManifest.from_dict(payload)


# --------------------------------------------------------------------------- #
# 包校验
# --------------------------------------------------------------------------- #


def _make_signed_bundle(signing_key, workdir: Path, content: bytes = b"agent bundle"):
    """构造一个签名合法的发布包。

    签名对象是 sha256 的**十六进制字符串**（不是原始文件字节）—— 见
    ``verify_bundle`` 的说明。
    """
    bundle = workdir / "bundle.tar.gz"
    make_tar(bundle, [("syncoj_agent/main.py", content, "file")])
    raw = bundle.read_bytes()
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()
    manifest = ReleaseManifest(
        version="9.9.9",
        url="/x",
        sha256=digest,
        size=len(raw),
        signature=_b64(signing_key.sign(digest.encode("ascii"))),
    )
    return bundle, manifest


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@requires_openssl
def test_verify_bundle_accepts_valid(signing_key, workdir: Path) -> None:
    bundle, manifest = _make_signed_bundle(signing_key, workdir)
    public = RSAPublicKey.from_dict(signing_key.public_key_dict())
    verify_bundle(public, bundle, manifest)


@requires_openssl
def test_verify_bundle_rejects_tampered_content(signing_key, workdir: Path) -> None:
    bundle, manifest = _make_signed_bundle(signing_key, workdir)
    public = RSAPublicKey.from_dict(signing_key.public_key_dict())

    bundle.write_bytes(bundle.read_bytes() + b"malicious")
    with pytest.raises(UpgradeError, match="大小不符|sha256"):
        verify_bundle(public, bundle, manifest)


@requires_openssl
def test_verify_bundle_rejects_wrong_key(signing_key, workdir: Path) -> None:
    bundle, manifest = _make_signed_bundle(signing_key, workdir)
    other = generate_keypair(workdir / "other.pem", bits=2048)
    wrong_public = RSAPublicKey.from_dict(other.public_key_dict())

    with pytest.raises(UpgradeError, match="签名"):
        verify_bundle(wrong_public, bundle, manifest)


@requires_openssl
def test_verify_bundle_requires_signature(signing_key, workdir: Path) -> None:
    bundle, manifest = _make_signed_bundle(signing_key, workdir)
    manifest.signature = ""
    public = RSAPublicKey.from_dict(signing_key.public_key_dict())
    with pytest.raises(UpgradeError, match="签名"):
        verify_bundle(public, bundle, manifest)


@requires_openssl
def test_signature_must_be_over_digest_not_raw_bytes(signing_key, workdir: Path) -> None:
    """钉死签名对象：签的是 sha256 的十六进制串，不是文件原始字节。

    两种方案都安全，但服务端与 Agent 必须**选同一个**。若这里通过了，说明有人
    把 Agent 改成了对原始字节验签，而服务端仍在对摘要签名 —— 那会导致所有
    真实升级都失败，且只在生产环境暴露。
    """
    bundle = workdir / "bundle.tar.gz"
    make_tar(bundle, [("main.py", "x", "file")])
    raw = bundle.read_bytes()
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()
    public = RSAPublicKey.from_dict(signing_key.public_key_dict())

    manifest = ReleaseManifest(
        version="9.9.9",
        url="/x",
        sha256=digest,
        size=len(raw),
        signature=_b64(signing_key.sign(raw)),  # 对原始字节签名 —— 错误的方案
    )
    with pytest.raises(UpgradeError, match="签名"):
        verify_bundle(public, bundle, manifest)

    # 换成对摘要签名就应当通过
    manifest.signature = _b64(signing_key.sign(digest.encode("ascii")))
    verify_bundle(public, bundle, manifest)


# --------------------------------------------------------------------------- #
# 安全解包 —— 本文件的重点
# --------------------------------------------------------------------------- #


def test_extracts_normal_bundle(workdir: Path) -> None:
    bundle = make_tar(
        workdir / "ok.tar.gz",
        [
            ("syncoj_agent", "", "dir"),
            ("syncoj_agent/main.py", "print(1)", "file"),
            ("syncoj_agent/README.md", "文档", "file"),
        ],
    )
    dest = workdir / "out"
    count = safe_extract_tar(bundle, dest)

    assert count == 2
    assert (dest / "syncoj_agent" / "main.py").read_text(encoding="utf-8") == "print(1)"
    assert (dest / "syncoj_agent" / "README.md").read_text(encoding="utf-8") == "文档"


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "../../escape.py",
        "a/../../escape.py",
        "a/b/../../../escape.py",
        "/etc/cron.d/pwn",
        "//etc/passwd",
        "C:/Windows/pwn",
        "a\\..\\..\\pwn.py",
    ],
)
def test_rejects_path_traversal(workdir: Path, name: str) -> None:
    """路径穿越是升级包最直接的攻击方式：写到 /etc/cron.d 就等于拿到 root。"""
    bundle = make_tar(workdir / "evil.tar.gz", [(name, "pwned", "file")])
    with pytest.raises(UpgradeError):
        safe_extract_tar(bundle, workdir / "out")


def test_rejects_symlink_member(workdir: Path) -> None:
    """符号链接成员可以让后续写入落到解压目录之外。一律拒绝，不去校验链接目标。"""
    bundle = make_tar(
        workdir / "sym.tar.gz",
        [("link", "/etc", "sym"), ("link/pwn", "pwned", "file")],
    )
    with pytest.raises(UpgradeError, match="链接"):
        safe_extract_tar(bundle, workdir / "out")


def test_rejects_hardlink_member(workdir: Path) -> None:
    bundle = make_tar(
        workdir / "hard.tar.gz",
        [("a.py", "x", "file"), ("b.py", "a.py", "hard")],
    )
    with pytest.raises(UpgradeError, match="链接"):
        safe_extract_tar(bundle, workdir / "out")


def test_rejects_device_node(workdir: Path) -> None:
    bundle = make_tar(workdir / "dev.tar.gz", [("null", "", "dev")])
    with pytest.raises(UpgradeError, match="设备|节点"):
        safe_extract_tar(bundle, workdir / "out")


def test_rejects_oversized_extraction(workdir: Path, monkeypatch) -> None:
    """zip bomb 防护：解压后总大小必须有上限。"""
    import syncoj_agent.upgrade as upgrade_module

    monkeypatch.setattr(upgrade_module, "MAX_EXTRACTED_BYTES", 100)
    bundle = make_tar(workdir / "bomb.tar.gz", [("big.bin", b"x" * 5000, "file")])

    with pytest.raises(UpgradeError, match="总大小"):
        safe_extract_tar(bundle, workdir / "out")


def test_rejects_too_many_members(workdir: Path, monkeypatch) -> None:
    import syncoj_agent.upgrade as upgrade_module

    monkeypatch.setattr(upgrade_module, "MAX_MEMBERS", 5)
    bundle = make_tar(
        workdir / "many.tar.gz", [("f%d.py" % i, "x", "file") for i in range(20)]
    )
    with pytest.raises(UpgradeError, match="成员数"):
        safe_extract_tar(bundle, workdir / "out")


def test_rejects_non_tar(workdir: Path) -> None:
    bogus = workdir / "not.tar.gz"
    bogus.write_bytes(b"this is definitely not a tar archive")
    with pytest.raises(UpgradeError, match="tar"):
        safe_extract_tar(bogus, workdir / "out")


def test_failed_extraction_leaves_no_partial_state(workdir: Path) -> None:
    """穿越检测失败时不能留下半解压的内容。"""
    bundle = make_tar(
        workdir / "mixed.tar.gz",
        [("good.py", "ok", "file"), ("../evil.py", "pwn", "file")],
    )
    dest = workdir / "out"
    with pytest.raises(UpgradeError):
        safe_extract_tar(bundle, dest)

    outside = workdir / "evil.py"
    assert not outside.exists(), "绝不能在解压目录外创建文件"


@requires_posix_modes
def test_strips_privileged_bits(workdir: Path) -> None:
    """升级包没有理由携带 SUID 程序。"""
    bundle = workdir / "suid.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        info = tarfile.TarInfo("tool.sh")
        payload = b"#!/bin/sh\n"
        info.size = len(payload)
        info.mode = 0o4755  # SUID
        archive.addfile(info, io.BytesIO(payload))

    dest = workdir / "out"
    safe_extract_tar(bundle, dest)

    mode = (dest / "tool.sh").stat().st_mode & 0o7777
    assert not (mode & 0o4000), "SUID 位必须被剥掉，实际 mode=%o" % mode


@requires_posix_modes
def test_preserves_executable_bit(workdir: Path) -> None:
    bundle = workdir / "exec.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        info = tarfile.TarInfo("run.sh")
        payload = b"#!/bin/sh\n"
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))

    dest = workdir / "out"
    safe_extract_tar(bundle, dest)
    assert (dest / "run.sh").stat().st_mode & 0o100


# --------------------------------------------------------------------------- #
# 暂存 / 激活 / 回滚
# --------------------------------------------------------------------------- #


@requires_symlinks
def test_stage_and_activate_and_rollback(workdir: Path) -> None:
    install = workdir / "install"
    bundle_v1 = make_tar(workdir / "v1.tar.gz", [("main.py", "v1", "file")])
    bundle_v2 = make_tar(workdir / "v2.tar.gz", [("main.py", "v2", "file")])

    stage_release(install, "1.0.0", bundle_v1)
    stage_release(install, "2.0.0", bundle_v2)
    assert list_releases(install) == ["1.0.0", "2.0.0"]

    assert activate_release(install, "1.0.0") is None
    assert current_release(install) == "1.0.0"
    assert (install / "current" / "main.py").read_text(encoding="utf-8") == "v1"

    previous = activate_release(install, "2.0.0")
    assert previous == "1.0.0"
    assert (install / "current" / "main.py").read_text(encoding="utf-8") == "v2"

    rollback_release(install, previous)
    assert current_release(install) == "1.0.0"
    assert (install / "current" / "main.py").read_text(encoding="utf-8") == "v1"


def test_activate_requires_existing_version(workdir: Path) -> None:
    with pytest.raises(UpgradeError, match="不存在"):
        activate_release(workdir / "install", "9.9.9")


def test_stage_is_idempotent(workdir: Path) -> None:
    install = workdir / "install"
    bundle = make_tar(workdir / "v.tar.gz", [("main.py", "x", "file")])
    first = stage_release(install, "1.0.0", bundle)
    second = stage_release(install, "1.0.0", bundle)
    assert first == second
    assert len(list_releases(install)) == 1


def test_stage_rejects_invalid_version(workdir: Path) -> None:
    install = workdir / "install"
    bundle = make_tar(workdir / "v.tar.gz", [("main.py", "x", "file")])
    with pytest.raises(UpgradeError, match="版本号"):
        stage_release(install, "../escape", bundle)


def test_failed_stage_leaves_no_partial_directory(workdir: Path) -> None:
    install = workdir / "install"
    bundle = make_tar(workdir / "evil.tar.gz", [("../pwn.py", "x", "file")])
    with pytest.raises(UpgradeError):
        stage_release(install, "1.0.0", bundle)
    assert list_releases(install) == []


@requires_symlinks
def test_prune_keeps_recent_versions(workdir: Path) -> None:
    install = workdir / "install"
    for version in ("1.0.0", "1.1.0", "1.2.0", "2.0.0"):
        bundle = make_tar(workdir / ("%s.tar.gz" % version), [("f", version, "file")])
        stage_release(install, version, bundle)

    activate_release(install, "2.0.0")
    removed = prune_releases(install, keep=2)

    assert removed == ["1.0.0", "1.1.0"]
    assert list_releases(install) == ["1.2.0", "2.0.0"]


@requires_symlinks
def test_prune_never_removes_active_version(workdir: Path) -> None:
    """删掉正在运行的版本会让下次重启直接起不来。"""
    install = workdir / "install"
    for version in ("1.0.0", "2.0.0"):
        bundle = make_tar(workdir / ("%s.tar.gz" % version), [("f", version, "file")])
        stage_release(install, version, bundle)

    activate_release(install, "1.0.0")
    prune_releases(install, keep=1)

    assert current_release(install) == "1.0.0"
    assert "1.0.0" in list_releases(install)


def test_activate_reports_unsupported_environment(workdir: Path) -> None:
    """在不支持符号链接的环境里必须给出**可操作**的报错，而不是一个裸 OSError。"""
    if symlinks_supported():
        pytest.skip("当前环境支持符号链接，无从触发该分支")

    install = workdir / "install"
    bundle = make_tar(workdir / "v.tar.gz", [("main.py", "x", "file")])
    stage_release(install, "1.0.0", bundle)

    with pytest.raises(UpgradeError, match="符号链接"):
        activate_release(install, "1.0.0")


@requires_symlinks
def test_current_release_on_missing_install(workdir: Path) -> None:
    assert current_release(workdir / "nope") is None
    assert list_releases(workdir / "nope") == []


# --------------------------------------------------------------------------- #
# 启动守卫与自动回滚
# --------------------------------------------------------------------------- #


def test_note_boot_without_state_is_noop(workdir: Path) -> None:
    assert note_boot(workdir / "install", "1.0.0") is None


def test_boot_counter_triggers_rollback(workdir: Path) -> None:
    """新版本反复起不来时必须自动切回上一版本 —— 否则 50 台机器一起变砖。"""
    from syncoj_agent.upgrade import (
        MAX_BOOT_ATTEMPTS,
        UpgradeState,
        load_state,
        note_boot,
        save_state,
    )

    install = workdir / "install"
    save_state(install, UpgradeState(version="2.0.0", previous="1.0.0"))

    # 前几次启动只累加计数，不回滚
    for _ in range(MAX_BOOT_ATTEMPTS):
        assert note_boot(install, "2.0.0") is None
    assert load_state(install).boots == MAX_BOOT_ATTEMPTS

    # 再启动一次说明它确实起不来 -> 回滚
    assert note_boot(install, "2.0.0") == "1.0.0"


def test_boot_counter_without_previous_version(workdir: Path) -> None:
    """没有可回滚的上一版本时不能假装能回滚，只能放弃守卫。"""
    from syncoj_agent.upgrade import (
        MAX_BOOT_ATTEMPTS,
        UpgradeState,
        load_state,
        note_boot,
        save_state,
    )

    install = workdir / "install"
    save_state(install, UpgradeState(version="2.0.0", previous=None))
    for _ in range(MAX_BOOT_ATTEMPTS + 1):
        result = note_boot(install, "2.0.0")
    assert result is None
    assert load_state(install) is None, "无路可退时应当清掉状态，避免反复误判"


def test_stale_state_is_discarded(workdir: Path) -> None:
    """状态文件记的版本与当前运行版本不符 = 状态已失效（可能有人手工换过版本）。"""
    from syncoj_agent.upgrade import UpgradeState, load_state, note_boot, save_state

    install = workdir / "install"
    save_state(install, UpgradeState(version="2.0.0", previous="1.0.0"))

    assert note_boot(install, "1.5.0") is None
    assert load_state(install) is None


def test_mark_healthy_clears_state(workdir: Path) -> None:
    from syncoj_agent.upgrade import UpgradeState, load_state, mark_healthy, save_state

    install = workdir / "install"
    save_state(install, UpgradeState(version="2.0.0", previous="1.0.0", boots=1))

    mark_healthy(install)
    assert load_state(install) is None


def test_mark_healthy_without_state_is_safe(workdir: Path) -> None:
    from syncoj_agent.upgrade import mark_healthy

    mark_healthy(workdir / "install")  # 不应抛异常


def test_corrupt_state_file_is_ignored(workdir: Path) -> None:
    from syncoj_agent.upgrade import load_state, note_boot

    install = workdir / "install"
    install.mkdir(parents=True, exist_ok=True)
    (install / "upgrade-state.json").write_text("{ 这不是 JSON", encoding="utf-8")

    assert load_state(install) is None
    assert note_boot(install, "1.0.0") is None
