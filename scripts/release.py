#!/usr/bin/env python3
"""发布：版本号对账与发布产物构建。CI 的 release 工作流（打 ``v*`` 标签触发）调用它。

    python scripts/release.py versions                 # 四处版本号是否一致（check.py 也跑）
    python scripts/release.py versions --tag v0.2.0    # 外加：标签是否就是 v<版本号>
    python scripts/release.py build --out dist         # 产物（需要先 npm run build 出 web/dist）

产物（都在 ``--out`` 下，外加一份 ``SHA256SUMS``）：

``syncoj-<版本>.tar.gz``
    服务端部署包：``git archive HEAD`` 的全部受版本管理的文件 + 预构建的 ``web/dist``。
    解开后 ``pip install ./server`` 即可；服务端按仓库布局找到 ``web/dist`` 与 ``agent/``
    （「发布当前版本」从 ``agent/`` 源码打 Agent 包），所以发的是整棵树而不是一个 wheel。
``syncoj-agent-<版本>.tar.gz``
    Agent 离线安装包（``build_bundle.py``，可复现）。**不带发布公钥、不带服务端地址**：
    信任锚是每台服务端自己的发布私钥，CI 没有也不该有。镜像预装时配合
    ``install.py --bundle … --public-key …``；平时走服务端「发布当前版本」更省事。
``install.py`` / ``bootstrap.sh``
    独立安装器与在线自举脚本，与包里的是同一份。

版本号的唯一来源是代码本身：``server/pyproject.toml``、``server/syncoj_server/__init__.py``、
``agent/syncoj_agent/__init__.py``、``web/package.json`` 四处必须相同，标签必须是 ``v<版本号>``。
发布前先改这四处、提交，再打标签 —— 标签对不上时工作流直接失败，不会发出去一个
"自称 0.2.0 实际是 0.1.0"的包。

本文件只用标准库，并且要能在 Python 3.8 上跑。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 版本号写在哪儿、怎么读出来。按行读，不 import（与服务端读 Agent 版本同一个理由）。
VERSION_SOURCES = {
    "server/pyproject.toml": r'^version\s*=\s*"([^"]+)"',
    "server/syncoj_server/__init__.py": r'^__version__\s*=\s*"([^"]+)"',
    "agent/syncoj_agent/__init__.py": r'^__version__\s*=\s*"([^"]+)"',
}

#: 与服务端 ``validate_release_version`` 同一口径的子集：数字段 + 可选的预发布后缀。
VERSION_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+){1,3}([.-]?(a|b|rc|dev)[0-9]*)?$")


class ReleaseError(Exception):
    pass


def read_versions(root: Path = REPO_ROOT) -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for rel, pattern in VERSION_SOURCES.items():
        text = (root / rel).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.MULTILINE)
        found[rel] = match.group(1) if match else None
    package = json.loads((root / "web" / "package.json").read_text(encoding="utf-8"))
    found["web/package.json"] = package.get("version")
    return found


def check_versions(tag: Optional[str] = None, root: Path = REPO_ROOT) -> str:
    """四处版本号一致且合法时返回它；``tag`` 给了还要求它是 ``v<版本号>``。"""
    found = read_versions(root)
    missing = [rel for rel, value in found.items() if not value]
    if missing:
        raise ReleaseError("读不到版本号：%s" % ", ".join(missing))
    distinct = sorted(set(found.values()))  # type: ignore[arg-type]
    if len(distinct) != 1:
        raise ReleaseError(
            "版本号不一致：\n" + "\n".join("  %-36s %s" % (rel, value) for rel, value in found.items())
        )
    version = distinct[0]
    if not VERSION_PATTERN.match(version):
        raise ReleaseError("版本号 %r 不合法（形如 0.2.0、0.2.0rc1）" % version)
    if tag is not None and tag != "v" + version:
        raise ReleaseError("标签 %s 与代码里的版本号 %s 对不上（应为 v%s）" % (tag, version, version))
    return version


def is_prerelease(version: str) -> bool:
    return bool(re.search(r"[a-z]", version))


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git"] + list(args), cwd=str(REPO_ROOT), check=True, stdout=subprocess.PIPE
    ).stdout


def _commit_time() -> int:
    """产物里所有文件的 mtime 都取 HEAD 的提交时间：同一个提交打出来逐字节相同。"""
    return int(_git("log", "-1", "--format=%ct").decode().strip())


def build_server_tarball(version: str, out: Path) -> Path:
    dist = REPO_ROOT / "web" / "dist"
    if not (dist / "index.html").is_file():
        raise ReleaseError("web/dist 不存在：先 (cd web && npm ci && npm run build)")
    prefix = "syncoj-%s/" % version
    mtime = _commit_time()
    archive = tarfile.open(fileobj=io.BytesIO(_git("archive", "--format=tar", "HEAD")), mode="r:")
    target = out / ("syncoj-%s.tar.gz" % version)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for member in archive.getmembers():
            if member.name == "pax_global_header":
                continue
            data = archive.extractfile(member) if member.isfile() else None
            member.name = prefix + member.name
            _normalize(member, mtime)
            tar.addfile(member, data)
        for path in sorted(dist.rglob("*")):
            if not path.is_file():
                continue
            info = tarfile.TarInfo(prefix + "web/dist/" + path.relative_to(dist).as_posix())
            info.size = path.stat().st_size
            info.mode = 0o644
            _normalize(info, mtime)
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    with target.open("wb") as handle:
        # gzip 头里不写文件名、mtime 固定：可复现
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=mtime) as gz:
            gz.write(raw.getvalue())
    return target


def _normalize(member: tarfile.TarInfo, mtime: int) -> None:
    member.mtime = mtime
    member.uid = member.gid = 0
    member.uname = member.gname = ""


def build_agent_bundle(version: str, out: Path) -> Path:
    target = out / ("syncoj-agent-%s.tar.gz" % version)
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "agent/packaging/build_bundle.py"), "--out", str(target),
         "--no-public-key"],
        cwd=str(REPO_ROOT), check=True,
    )
    return target


def build(out: Path, tag: Optional[str] = None) -> List[Path]:
    version = check_versions(tag)
    out.mkdir(parents=True, exist_ok=True)
    artifacts = [build_server_tarball(version, out), build_agent_bundle(version, out)]
    for name in ("install.py", "bootstrap.sh"):
        target = out / name
        shutil.copyfile(str(REPO_ROOT / "agent" / "packaging" / name), str(target))
        artifacts.append(target)
    lines = []
    for path in artifacts:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append("%s  %s\n" % (digest, path.name))
    sums = out / "SHA256SUMS"
    sums.write_text("".join(lines), encoding="utf-8")
    artifacts.append(sums)
    return artifacts


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SyncOJ 发布：版本号对账与产物构建")
    sub = parser.add_subparsers(dest="command", required=True)
    p_versions = sub.add_parser("versions", help="四处版本号是否一致")
    p_versions.add_argument("--tag", default=None, help="要发布的标签（如 v0.2.0）")
    p_versions.add_argument("--github-output", default=None,
                            help="把 version / prerelease 写进这个文件（GITHUB_OUTPUT）")
    p_build = sub.add_parser("build", help="构建发布产物")
    p_build.add_argument("--out", default="dist")
    p_build.add_argument("--tag", default=None)
    args = parser.parse_args(argv)

    try:
        if args.command == "versions":
            version = check_versions(args.tag)
            print("版本号一致：%s" % version)
            if args.github_output:
                with open(args.github_output, "a", encoding="utf-8") as handle:
                    handle.write("version=%s\nprerelease=%s\n"
                                 % (version, "true" if is_prerelease(version) else "false"))
            return 0
        for path in build(Path(args.out).resolve(), args.tag):
            print("  %s（%d 字节）" % (path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path,
                                     path.stat().st_size))
        return 0
    except ReleaseError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
