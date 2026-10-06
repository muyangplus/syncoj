"""``install_release``：**按内容**决定"复用还是覆盖"，而不是"版本目录在不在"。

现场踩过的痛点：用户重新构建了**同一个版本号**的包并铺开，已经装过的机器却永远
拿不到新内容 —— 老逻辑只要看到 ``releases/<版本>/`` 存在就"已安装，跳过解压"。

新的规则：

* 版本目录里 ``.syncoj-bundle-sha256`` 与这一份来源的指纹相同 → 跳过（幂等）；
* 指纹不同、或者老目录里**没有**记录（历史安装）→ **覆盖安装**；
* 覆盖是"安全替换"：先解到 ``.staging-<pid>``，失败则老版本原封不动；换目录时
  被换的是当前激活版本就先停服务；``os.replace`` 原子换，失败能回滚。

用例里没有真 systemd：需要的那条用 monkeypatch 记录调用顺序。
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

OLD_MAIN = "print('old')\n"
NEW_MAIN = "print('new')\n"
#: 契约：指纹记在版本目录内的这个名字下（安装器常量必须与它一致）。
MARKER_NAME = ".syncoj-bundle-sha256"


def make_bundle(
    path: Path, version: str = "0.1.0", main_text: str = OLD_MAIN
) -> Path:
    entries = [
        ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version),
        ("syncoj_agent/main.py", main_text),
        ("run_agent.py", "print('launcher')\n"),
    ]
    with tarfile.open(str(path), "w:gz") as archive:
        for name, content in entries:
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return path


def make_installer(installer_module, workdir: Path, argv=()):
    options = installer_module.build_parser().parse_args(["--user", "syncoj"] + list(argv))
    options.config_dir = str(workdir / "etc")
    options.state_dir = str(workdir / "state")
    options.prefix = str(workdir / "opt")
    options.unit_dir = str(workdir / "units")
    return installer_module.Installer(
        options, installer_module.Reporter(dry_run=options.dry_run, quiet=True)
    )


def collect_messages(instance):
    collected = []
    for kind in ("warn", "note", "action", "plan", "skip"):
        setattr(
            instance.report,
            kind,
            lambda message, k=kind: collected.append((k, message)),
        )
    return collected


def texts(collected, kind: str) -> str:
    return "\n".join(message for tag, message in collected if tag == kind)


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def releases_dir(instance) -> Path:
    return Path(instance.options.prefix) / "releases"


def release_dir_of(instance, version: str) -> Path:
    return releases_dir(instance) / version


def marker_of(instance, version: str) -> Path:
    return release_dir_of(instance, version) / MARKER_NAME


def main_of(instance, version: str) -> Path:
    return release_dir_of(instance, version) / "syncoj_agent" / "main.py"


def leftovers(instance):
    """``releases/`` 里不许留下的临时目录。"""
    releases = releases_dir(instance)
    return sorted(
        str(path) for path in list(releases.glob(".staging-*")) + list(releases.glob(".old-*"))
    )


# --------------------------------------------------------------------------- #
# 1) 内容一致 → 跳过，且旧目录一点都没被碰
# --------------------------------------------------------------------------- #


def test_内容一致就跳过且旧目录不被重写(installer_module, workdir: Path) -> None:
    # 契约：测试钉的名字必须就是安装器常量
    assert installer_module.BUNDLE_SHA_MARKER_FILENAME == MARKER_NAME

    bundle = make_bundle(workdir / "b.tar.gz", main_text=OLD_MAIN)
    instance = make_installer(installer_module, workdir)

    version = instance.install_release(bundle)
    marker = marker_of(instance, version)
    assert marker.read_text(encoding="utf-8").strip() == sha_of(bundle)

    # 把探针文件的 mtime 钉到一个远古时间：只要被重写，它就一定变
    probe = main_of(instance, version)
    os.utime(str(probe), (946684800, 946684800))

    messages = collect_messages(instance)
    assert instance.install_release(bundle) == version

    assert int(probe.stat().st_mtime) == 946684800, "内容一致却被重写了（幂等性破了）"
    assert marker.read_text(encoding="utf-8").strip() == sha_of(bundle)
    assert "已是最新" in texts(messages, "skip")
    assert leftovers(instance) == []


# --------------------------------------------------------------------------- #
# 2) 内容变了 → 同版本号也要覆盖
# --------------------------------------------------------------------------- #


def test_内容变了同版本也要覆盖(installer_module, workdir: Path) -> None:
    old = make_bundle(workdir / "old.tar.gz", version="0.1.0", main_text=OLD_MAIN)
    new = make_bundle(workdir / "new.tar.gz", version="0.1.0", main_text=NEW_MAIN)
    assert sha_of(old) != sha_of(new)

    instance = make_installer(installer_module, workdir)
    version = instance.install_release(old)
    assert main_of(instance, version).read_text(encoding="utf-8") == OLD_MAIN
    assert marker_of(instance, version).read_text(encoding="utf-8").strip() == sha_of(old)

    messages = collect_messages(instance)
    assert instance.install_release(new) == version

    assert main_of(instance, version).read_text(encoding="utf-8") == NEW_MAIN, (
        "同版本号重新构建的包没有覆盖上去 —— 内容变了却还是老代码"
    )
    assert marker_of(instance, version).read_text(encoding="utf-8").strip() == sha_of(new)
    assert "已覆盖安装版本 0.1.0" in texts(messages, "action")
    assert leftovers(instance) == []


# --------------------------------------------------------------------------- #
# 3) 老目录没有 marker（历史安装）→ 覆盖一次，之后就有记录
# --------------------------------------------------------------------------- #


def test_老目录没有标记时覆盖一次(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", main_text=NEW_MAIN)

    # 手工造一个"历史安装"：目录在、没有 .syncoj-bundle-sha256
    release = workdir / "opt" / "releases" / "0.1.0"
    (release / "syncoj_agent").mkdir(parents=True)
    (release / "syncoj_agent" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (release / "syncoj_agent" / "main.py").write_text("print('very old')\n", encoding="utf-8")
    assert not (release / MARKER_NAME).exists(), "前提：老目录里没有标记"

    instance = make_installer(installer_module, workdir)
    messages = collect_messages(instance)
    assert instance.install_release(bundle) == "0.1.0"

    assert main_of(instance, "0.1.0").read_text(encoding="utf-8") == NEW_MAIN
    assert marker_of(instance, "0.1.0").read_text(encoding="utf-8").strip() == sha_of(bundle)
    assert "已覆盖安装版本 0.1.0" in texts(messages, "action")

    # 第二次：指纹对上了 → 跳过（幂等从这里开始成立）
    again = collect_messages(instance)
    assert instance.install_release(bundle) == "0.1.0"
    assert "已是最新" in texts(again, "skip")


# --------------------------------------------------------------------------- #
# 4) 解包失败 → 老版本完好，且不留 .staging-* / .old-*
# --------------------------------------------------------------------------- #


def test_解包失败时老版本完好且无残留(installer_module, workdir: Path) -> None:
    good = make_bundle(workdir / "good.tar.gz", main_text=OLD_MAIN)
    instance = make_installer(installer_module, workdir)
    version = instance.install_release(good)

    release = release_dir_of(instance, version)
    snapshot = {
        path.relative_to(release).as_posix(): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }

    bad = workdir / "bad.tar.gz"
    bad.write_bytes(b"this is definitely not a tar archive")

    with pytest.raises(installer_module.InstallError):
        instance.install_release(bad)

    after = {
        path.relative_to(release).as_posix(): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }
    assert after == snapshot, "解包失败却动了老版本"
    assert leftovers(instance) == [], "解包失败留下了 .staging-*/.old-*"


# --------------------------------------------------------------------------- #
# 5) 被替换的正是当前激活版本 → 先停服务，再换目录
# --------------------------------------------------------------------------- #


def test_覆盖当前激活版本时先停服务再换目录(
    installer_module, workdir: Path, monkeypatch
) -> None:
    old = make_bundle(workdir / "old.tar.gz", main_text=OLD_MAIN)
    new = make_bundle(workdir / "new.tar.gz", main_text=NEW_MAIN)

    instance = make_installer(installer_module, workdir)
    version = instance.install_release(old)
    probe = main_of(instance, version)

    # 让 current 指向这个版本；并假装机器上有 systemctl
    monkeypatch.setattr(installer_module, "current_version", lambda prefix: version)
    monkeypatch.setattr(installer_module, "_which", lambda name: "/bin/systemctl")

    calls = []
    content_at_stop = {}

    def fake_run(cmd, check=True):
        calls.append(list(cmd))
        if list(cmd)[:2] == ["systemctl", "stop"]:
            # 停服务的那一刻必须还是老内容 —— 证明"停"发生在"换目录"之前
            content_at_stop["main"] = probe.read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(installer_module, "run", fake_run)

    assert instance.install_release(new) == version

    assert ["systemctl", "stop", "syncoj-agent"] in calls, "替换激活版本前没有停服务"
    assert content_at_stop.get("main") == OLD_MAIN, (
        "停服务时目录里已经是新内容了 —— 停晚了，进程可能跑在半新半旧上"
    )
    assert probe.read_text(encoding="utf-8") == NEW_MAIN, "换目录没成功"
    assert leftovers(instance) == []


# --------------------------------------------------------------------------- #
# 6) --from-dir：没有 tar 包，指纹按目录树算
# --------------------------------------------------------------------------- #


def test_from_dir_内容变了也要覆盖(installer_module, workdir: Path) -> None:
    source = workdir / "agentdir"
    (source / "syncoj_agent").mkdir(parents=True)
    (source / "syncoj_agent" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (source / "syncoj_agent" / "main.py").write_text(OLD_MAIN, encoding="utf-8")

    instance = make_installer(installer_module, workdir, argv=["--from-dir", str(source)])
    version = instance.install_release(source)
    assert version == "0.1.0"
    assert main_of(instance, version).read_text(encoding="utf-8") == OLD_MAIN
    assert marker_of(instance, version).is_file()

    # 目录内容变了（同版本号）→ 覆盖
    (source / "syncoj_agent" / "main.py").write_text(NEW_MAIN, encoding="utf-8")
    again = make_installer(installer_module, workdir, argv=["--from-dir", str(source)])
    assert again.install_release(source) == version
    assert main_of(again, version).read_text(encoding="utf-8") == NEW_MAIN

    # 内容不变 → 跳过，且不重写
    probe = main_of(again, version)
    os.utime(str(probe), (946684800, 946684800))
    third = make_installer(installer_module, workdir, argv=["--from-dir", str(source)])
    assert third.install_release(source) == version
    assert int(probe.stat().st_mtime) == 946684800
    assert leftovers(third) == []
