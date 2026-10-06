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
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
PACKAGE_NAME = "syncoj_agent"

#: 升级签名公钥在包里的名字（放在包目录**之外**、与启动器同级）。
#:
#: 装完之后它会落到机器的 ``<config-dir>/release-key.pub.json``（见 install.py），
#: 也就是 Agent 的信任锚。做成"随包带走"是因为手工把公钥拷到每台机器上这一步
#: 在 50 台的规模下必然有人漏掉几台，而漏掉的表现是"那台机器永远不升级"，
#: 且没有任何报错。
PUBLIC_KEY_NAME = "release-key.pub.json"
DEFAULT_PUBLIC_KEY = REPO_ROOT / ".key" / PUBLIC_KEY_NAME

#: 服务端地址在包里的名字（同样放在包目录之外、与启动器同级）。
#:
#: 装 50 台机器时，"服务端地址"是唯一还要人手填的一项，而它是**打包这台服务端
#: 自己就知道**的 —— 所以让它随包走，装机时一个字都不用输。
SERVER_URL_NAME = "server.json"

#: 这一项的格式版本，与发现协议（``syncoj_server.services.discovery``）同号。
SERVER_URL_FORMAT = 1

#: 放在发布根目录（包目录之外）的启动器。
#:
#: ``syncoj_agent/main.py`` 用包内相对导入，**不能当脚本直接跑**。而 systemd 必须
#: 用 ``-E -s`` 启动以隔离选手的 Python 环境，可 ``-E`` 又会忽略 PYTHONPATH ——
#: 所以只能靠一个显式设置 sys.path 的启动器。systemd 的 ExecStart 指向它。
LAUNCHER_NAME = "run_agent.py"

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


def launcher_path(agent_root: Path) -> Path:
    launcher = agent_root / LAUNCHER_NAME
    if not launcher.is_file():
        raise SystemExit(
            "缺少启动器 %s。没有它 Agent 在目标机上根本起不来 —— "
            "syncoj_agent/main.py 使用包内相对导入，无法当脚本直接执行。" % launcher
        )
    return launcher


def read_version(agent_root: Path) -> str:
    init_file = agent_root / PACKAGE_NAME / "__init__.py"
    for line in init_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__"):
            return stripped.partition("=")[2].strip().strip("'\"")
    raise SystemExit("找不到 __version__")


def resolve_public_key(explicit: Optional[str]) -> Optional[Path]:
    """定出要打进包里的升级公钥；没有就返回 ``None``。

    显式给了就用显式的（路径错了由调用方负责，它会当场报"文件不存在"）；
    否则看仓库 ``.key/`` 里有没有 —— 那是 ``syncoj-server init`` 生成的位置。
    都没有 → 照旧打一个不含公钥的包，机器上自更新保持关闭。

    **只在文件真的存在时才用默认路径。** 这个脚本也会被拿去在别处跑，
    硬凑一个路径的结果是打出一个里面躺着陌生公钥的包 —— 而那会让机器信任
    一把没人知道来源的签名。
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise SystemExit("找不到升级公钥: %s" % candidate)
        return candidate
    return DEFAULT_PUBLIC_KEY if DEFAULT_PUBLIC_KEY.is_file() else None


def server_url_bytes(url: str) -> bytes:
    """包内 ``server.json`` 的内容。

    **LF 结尾、显式 UTF-8、可复现**：这个文件参与 sha256，而 sha256 又要被签名，
    所以它的字节必须稳定 —— 同一份输入在哪台机器上打都是同一串。
    """
    payload = {"v": SERVER_URL_FORMAT, "url": url.strip().rstrip("/")}
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def build(
    bundle_path: Path,
    agent_root: Path,
    reproducible: bool = True,
    public_key: Optional[Path] = None,
    server_url: Optional[str] = None,
) -> Tuple[str, int, str]:
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
                _write_tar(
                    gz,
                    files,
                    agent_root,
                    reproducible=True,
                    public_key=public_key,
                    server_url=server_url,
                )
    else:
        with tarfile.open(str(bundle_path), "w:gz", compresslevel=9) as archive:
            _add_files(
                archive,
                files,
                agent_root,
                reproducible=False,
                public_key=public_key,
                server_url=server_url,
            )

    raw_bytes = bundle_path.read_bytes()
    return version, len(raw_bytes), hashlib.sha256(raw_bytes).hexdigest()


def _write_tar(
    stream,
    files: List[Path],
    agent_root: Path,
    reproducible: bool,
    public_key: Optional[Path] = None,
    server_url: Optional[str] = None,
) -> None:
    with tarfile.open(fileobj=stream, mode="w") as archive:
        _add_files(archive, files, agent_root, reproducible, public_key, server_url)


def _add_files(
    archive: tarfile.TarFile,
    files: List[Path],
    agent_root: Path,
    reproducible: bool,
    public_key: Optional[Path] = None,
    server_url: Optional[str] = None,
) -> None:
    package = agent_root / PACKAGE_NAME
    #: ``(包内路径, 来源)``。来源是 ``Path`` 就读文件，是 ``bytes`` 就当场造一份 ——
    #: ``server.json`` 没有对应的磁盘文件，它是打包时才算出来的。
    entries: List[Tuple[str, object]] = [(LAUNCHER_NAME, launcher_path(agent_root))]
    for path in files:
        entries.append(
            ("%s/%s" % (PACKAGE_NAME, path.relative_to(package).as_posix()), path)
        )
    # 公钥与地址都放在包目录之外（与启动器同级）：它们是给**安装器**看的，
    # 不是包的一部分，混进 syncoj_agent/ 里只会被当成模块或数据文件
    if public_key is not None:
        entries.append((PUBLIC_KEY_NAME, public_key))
    if server_url:
        entries.append((SERVER_URL_NAME, server_url_bytes(server_url)))

    for arcname, source in entries:
        payload = source if isinstance(source, bytes) else None
        if payload is None:
            info = archive.gettarinfo(str(source), arcname=arcname)
        else:
            info = tarfile.TarInfo(arcname)
            info.size = len(payload)
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        if reproducible:
            info.mtime = 0
            info.mode = 0o644
        if payload is None:
            with open(str(source), "rb") as handle:
                archive.addfile(info, handle)
        else:
            archive.addfile(info, io.BytesIO(payload))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="打包 SyncOJ Agent 离线安装包")
    parser.add_argument("--out", default=None, help="输出路径（默认 dist/syncoj-agent-<版本>.tar.gz）")
    parser.add_argument("--agent-root", default=str(AGENT_ROOT))
    parser.add_argument("--verify-signature", default=None,
                        help="可选：用服务端私钥签名，产出 .sig 文件（供手工核对）")
    parser.add_argument("--public-key", default=None,
                        help="打进包里的升级公钥。不指定时用仓库 .key/release-key.pub.json"
                             "（存在才用）；都没有就不打，机器上自更新保持关闭。")
    parser.add_argument("--server-url", default=None,
                        help="服务端地址，如 http://10.0.0.5:8000。会写进包内 %s，"
                             "装机时不必再手填 —— 装 50 台时这是唯一还要人输的一项。"
                             % SERVER_URL_NAME)
    parser.add_argument("--print-sha256-only", action="store_true")
    args = parser.parse_args(argv)

    agent_root = Path(args.agent_root)
    version = read_version(agent_root)
    out = Path(args.out) if args.out else REPO_ROOT / "dist" / ("syncoj-agent-%s.tar.gz" % version)
    public_key = resolve_public_key(args.public_key)
    if args.server_url is not None and not args.server_url.strip():
        raise SystemExit("--server-url 不能是空字符串（不要它就别传这个参数）")

    version, size, digest = build(
        out, agent_root, public_key=public_key, server_url=args.server_url
    )

    if args.print_sha256_only:
        print(digest)
        return 0

    print("已生成 %s" % out)
    print("  版本:   %s" % version)
    print("  大小:   %d 字节" % size)
    print("  sha256: %s" % digest)
    if public_key is not None:
        print("  升级公钥: %s → 包内 %s" % (public_key, PUBLIC_KEY_NAME))
        print("    装完之后它在机器的 <config-dir>/release-key.pub.json，")
        print("    agent.ini 的 [upgrade] public_key 会指向它 —— 不用手工拷。")
    else:
        print("  升级公钥: 没有（%s 不存在，也没给 --public-key）" % DEFAULT_PUBLIC_KEY)
        print("    包照样能装能跑，只是机器上自更新保持关闭 —— 签不出包就没法升。")

    if args.server_url:
        print("  服务端地址: %s → 包内 %s" % (args.server_url, SERVER_URL_NAME))
        print("    装机时 install.py 自动用它，不用再传 --server；")
        print("    机器上找不到服务端时还会用局域网发现再问一次。")
    else:
        print("  服务端地址: 没打进去（没给 --server-url）")
        print("    装机时得传 --server，或者靠局域网发现自己问出来。")

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
