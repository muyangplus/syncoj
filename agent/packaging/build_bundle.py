#!/usr/bin/env python3
"""打包 Agent 离线安装包。

产出一个 tar.gz，解压后顶层是 ``syncoj_agent/`` —— 与
``agent/syncoj_agent/upgrade.py`` 的 ``safe_extract_tar`` 以及
``agent/packaging/install.py`` 所期望的结构一致。三处必须对齐，否则
"自更新能用但离线装不上"这类问题会在最不方便的时候暴露。

用法::

    python agent/packaging/build_bundle.py                    # 输出到 dist/
    python agent/packaging/build_bundle.py --out /tmp/x.tar.gz
    python agent/packaging/build_bundle.py --verify-signature key.pem
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
PACKAGE_NAME = "syncoj_agent"

#: 打进包里的内容。刻意用白名单而不是排除法 ——
#: 排除法会在新增目录时悄悄漏掉或误带上不该带的东西（比如测试与开发工具）。
INCLUDE_FILES = ("*.py",)
INCLUDE_DIRS: Tuple[str, ...] = ()

#: 明确不打进去的
EXCLUDE_DIRS = frozenset({
    "__pycache__", "tests", "tools", "packaging", ".pytest_cache", ".mypy_cache",
})
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd")


def collect_files(agent_root: Path) -> List[Path]:
    package = agent_root / PACKAGE_NAME
    if not package.is_dir():
        raise SystemExit("找不到 %s 目录: %s" % (PACKAGE_NAME, package))

    files: List[Path] = []
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(package).parts)
        if parts & EXCLUDE_DIRS:
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        files.append(path)
    return files


def read_version(agent_root: Path) -> str:
    init_file = agent_root / PACKAGE_NAME / "__init__.py"
    for line in init_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__"):
            return stripped.partition("=")[2].strip().strip("'\"")
    raise SystemExit("找不到 __version__")


def build(bundle_path: Path, agent_root: Path, reproducible: bool = True) -> Tuple[str, int, str]:
    """打包，返回 ``(版本号, 字节数, sha256)``。"""
    version = read_version(agent_root)
    files = collect_files(agent_root)

    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    # 可复现性是刻意的：gzip 头里默认嵌当前时间戳**和输出文件名**，tar 成员里
    # 也有 mtime，三者都会让"同样内容产出不同字节"。那会让"同一版本两次打包
    # 校验和不同"，于是签名的确定性和"这个包是不是那个包"的判断全部失效。
    #
    #   · mtime=0            -> 去掉时间戳
    #   · filename=""        -> 去掉 FNAME 字段（GzipFile 默认从 fileobj.name
    #                           推断，于是包名会进了压缩流）
    #   · tar 成员 mtime/mode -> 在 _add_files 里统一置零
    if reproducible:
        import gzip

        with open(str(bundle_path), "wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
            ) as gz:
                _write_tar(gz, files, agent_root, reproducible=True)
    else:
        with tarfile.open(str(bundle_path), "w:gz", compresslevel=9) as archive:
            _add_files(archive, files, agent_root, reproducible=False)

    raw_bytes = bundle_path.read_bytes()
    return version, len(raw_bytes), hashlib.sha256(raw_bytes).hexdigest()


def _write_tar(stream, files: List[Path], agent_root: Path, reproducible: bool) -> None:
    with tarfile.open(fileobj=stream, mode="w") as archive:
        _add_files(archive, files, agent_root, reproducible)


def _add_files(archive: tarfile.TarFile, files: List[Path], agent_root: Path,
               reproducible: bool) -> None:
    package = agent_root / PACKAGE_NAME
    for path in files:
        arcname = "%s/%s" % (PACKAGE_NAME, path.relative_to(package).as_posix())
        info = archive.gettarinfo(str(path), arcname=arcname)
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        if reproducible:
            info.mtime = 0
            info.mode = 0o644
        with open(str(path), "rb") as handle:
            archive.addfile(info, handle)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="打包 SyncOJ Agent 离线安装包")
    parser.add_argument("--out", default=None, help="输出路径（默认 dist/syncoj-agent-<版本>.tar.gz）")
    parser.add_argument("--agent-root", default=str(AGENT_ROOT))
    parser.add_argument("--verify-signature", default=None,
                        help="可选：用服务端私钥签名，产出 .sig 文件（供手工核对）")
    parser.add_argument("--print-sha256-only", action="store_true")
    args = parser.parse_args(argv)

    agent_root = Path(args.agent_root)
    version = read_version(agent_root)
    out = Path(args.out) if args.out else REPO_ROOT / "dist" / ("syncoj-agent-%s.tar.gz" % version)

    version, size, digest = build(out, agent_root)

    if args.print_sha256_only:
        print(digest)
        return 0

    print("已生成 %s" % out)
    print("  版本:   %s" % version)
    print("  大小:   %d 字节" % size)
    print("  sha256: %s" % digest)

    if args.verify_signature:
        # 延迟导入：只有需要签名时才依赖服务端模块
        sys.path.insert(0, str(REPO_ROOT / "server"))
        from syncoj_server.services.signing import load_signing_key

        key = load_signing_key(Path(args.verify_signature))
        signature = key.sign(digest.encode("ascii"))
        import base64

        encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        sig_path = out.with_suffix(out.suffix + ".sig")
        sig_path.write_text(encoded + "\n", encoding="ascii")
        print("  签名:   %s" % sig_path)
        print("  key_id: %s" % key.key_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
