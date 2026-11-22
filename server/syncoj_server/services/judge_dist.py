"""评测机分发（§12.10）：zipapp 打包、一键安装脚本、安装命令。

* ``worker.pyz``：把 ``syncoj_server/judging/``（只用标准库）连同包根的 ``__init__.py``
  （只有版本号）打成 zipapp，``python3 syncoj-judge.pyz --server …`` 直接跑，另一台机器上
  不必装服务端包与它的第三方依赖。按需生成，以「版本号 + 源码指纹」为键缓存在
  ``<数据目录>/cache/judge-worker/``；源码一改（开发时）自动换一份。
* ``install.sh`` / ``install.ps1``：模板在 ``services/judge_install/``，发出时嵌进服务端
  地址与 pyz 的 sha256。地址取自请求的 Host 头（这几条路由只在管理端口上，所以就是
  管理端口地址），只收形状安全的 —— 它要被嵌进以 root / 管理员执行的脚本。
"""

from __future__ import annotations

import hashlib
import re
import threading
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from .. import __version__

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
JUDGING_ROOT = PACKAGE_ROOT / "judging"
TEMPLATES = Path(__file__).resolve().parent / "judge_install"

#: 能安全嵌进脚本字符串的 Host 头（主机名 / IPv4 / [IPv6] + 可选端口），同 api/agent.py
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(?::[0-9]{1,5})?$|^\[[0-9A-Fa-f:.]+\](?::[0-9]{1,5})?$")

#: zipapp 入口
MAIN = (
    "import sys\n"
    "from syncoj_server.judging.worker import main\n"
    "sys.exit(main())\n"
)

_BUILD_LOCK = threading.Lock()


def _sources() -> List[Tuple[str, Path]]:
    """要打进 pyz 的文件：(包内相对路径, 源文件)。不带 ``__pycache__``。"""
    files = [("syncoj_server/__init__.py", PACKAGE_ROOT / "__init__.py")]
    for path in sorted(JUDGING_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        files.append(("syncoj_server/judging/" + path.relative_to(JUDGING_ROOT).as_posix(), path))
    return files


def fingerprint() -> str:
    digest = hashlib.sha256(__version__.encode("utf-8") + b"\0" + MAIN.encode("utf-8"))
    for name, path in _sources():
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def worker_pyz(cache_root: Path) -> Path:
    """当前源码对应的 pyz（没有就现打一份）。"""
    key = fingerprint()[:16]
    target = Path(cache_root) / ("syncoj-judge-%s-%s.pyz" % (__version__, key))
    if target.is_file():
        return target
    with _BUILD_LOCK:
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        # 与 zipapp.create_archive 同一个格式：shebang 一行 + zip（zip 读取方按目录里的偏移找内容）
        with partial.open("wb") as handle:
            handle.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("__main__.py", MAIN)
                for name, path in _sources():
                    archive.write(str(path), name)
        partial.replace(target)
        # 旧版本的包留着没用：只留当前这一份
        for stale in target.parent.glob("syncoj-judge-*.pyz"):
            if stale != target:
                try:
                    stale.unlink()
                except OSError:
                    pass
    return target


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def base_url(host: Optional[str], scheme: str, fallback_port: int) -> str:
    """装机脚本里用的服务端地址：请求的 Host 头（形状不安全就退回本机地址）。"""
    host = (host or "").strip()
    scheme = scheme if scheme in ("http", "https") else "http"
    if host and SAFE_HOST_RE.match(host):
        return "%s://%s" % (scheme, host)
    return "http://127.0.0.1:%d" % fallback_port


def render_script(name: str, server: str, pyz_sha256: str) -> bytes:
    """``install.sh`` / ``install.ps1``：嵌入地址与 pyz 指纹。ps1 带 UTF-8 BOM（PowerShell 5.1 要它）。"""
    raw = (TEMPLATES / name).read_bytes()
    text = raw.decode("utf-8-sig")
    if name.endswith(".sh"):
        text = text.replace('EMBEDDED_SERVER="__SYNCOJ_SERVER__"', 'EMBEDDED_SERVER="%s"' % server, 1)
        text = text.replace('PYZ_SHA256="__SYNCOJ_PYZ_SHA256__"', 'PYZ_SHA256="%s"' % pyz_sha256, 1)
        return text.replace("\r\n", "\n").encode("utf-8")
    text = text.replace("$EmbeddedServer = '__SYNCOJ_SERVER__'", "$EmbeddedServer = '%s'" % server, 1)
    text = text.replace("$PyzSha256 = '__SYNCOJ_PYZ_SHA256__'", "$PyzSha256 = '%s'" % pyz_sha256, 1)
    return b"\xef\xbb\xbf" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def install_commands(server: str, token: str) -> Tuple[str, str, str]:
    """(Linux 一行命令, Windows 一行命令, 已装好 syncoj-judge 时的手动命令)。"""
    linux = "curl -fsSL %s/judge/install.sh | sudo bash -s -- %s" % (server, token)
    windows = (
        "$p = Join-Path $env:TEMP syncoj-judge-install.ps1; "
        "iwr -UseBasicParsing %s/judge/install.ps1 -OutFile $p; "
        "& $p -Token '%s'" % (server, token)
    )
    # 令牌是 base64url，可能以 - 开头：PowerShell 里加引号（否则被当成参数名），
    # 写文件用 printf（echo 会把 -n / -e 开头的当成选项）
    manual = (
        "printf '%%s\\n' '%s' > judge.token\n"
        "syncoj-judge --server %s --token-file judge.token --backend isolate" % (token, server)
    )
    return linux, windows, manual
