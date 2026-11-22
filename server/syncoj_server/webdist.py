"""前端构建产物（``web/dist``）是否过期。

构建时 ``web/scripts/build-info-plugin.mjs`` 写出 ``dist/build-info.json``：
``{"version", "source_sha256", "built_at"}``。启动时这里按**同一算法**重算前端源码指纹并比对：

* 产物里的版本号与服务端版本不同 → 过期；
* 源码目录在（仓库布局）且指纹不同 → 过期（改了前端没重新构建，或 ``git pull`` 之后没构建）；
* 产物里没有 ``build-info.json`` → 视为过期（0.2.0 之前的构建都没有它）。

指纹算法（两边必须一致，``tests/test_webdist.py`` 用 node 对拍）：取 :data:`SOURCE_FILES` 与
:data:`SOURCE_DIRS` 下的全部文件（跳过以 ``.`` 开头的名字），按相对路径（``/`` 分隔）排序；
每个文件把内容里的 ``\\r\\n`` 换成 ``\\n`` 后算 sha256，拼成 ``<路径>\\0<十六进制摘要>\\n``；
对整串再算 sha256。换行规整是为了 Windows 上 git 自动转 CRLF 时不误报。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

__all__ = [
    "BUILD_INFO_NAME",
    "SOURCE_FILES",
    "SOURCE_DIRS",
    "WebDistStatus",
    "source_sha256",
    "check",
    "build",
]

BUILD_INFO_NAME = "build-info.json"
#: 影响构建产物的单个文件（相对 ``web/``）
SOURCE_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "env.d.ts",
)
#: 影响构建产物的目录（递归）
SOURCE_DIRS = ("src", "public", "scripts")


class WebDistStatus(object):
    """``stale`` 为真时 ``reason`` 是给人看的原因。"""

    def __init__(self, stale: bool, reason: str = "", dist: Optional[Path] = None,
                 web_root: Optional[Path] = None) -> None:
        self.stale = stale
        self.reason = reason
        self.dist = dist
        self.web_root = web_root


def _files(web_root: Path) -> List[str]:
    out = []
    for name in SOURCE_FILES:
        if (web_root / name).is_file():
            out.append(name)
    for folder in SOURCE_DIRS:
        base = web_root / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            rel = path.relative_to(web_root)
            if any(part.startswith(".") for part in rel.parts) or not path.is_file():
                continue
            out.append(rel.as_posix())
    return sorted(out)


def source_sha256(web_root: Path) -> str:
    """前端源码指纹（算法见模块说明）。"""
    outer = hashlib.sha256()
    for rel in _files(web_root):
        data = (web_root / rel).read_bytes().replace(b"\r\n", b"\n")
        outer.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).hexdigest().encode("ascii") + b"\n")
    return outer.hexdigest()


def check(dist: Optional[Path], version: str) -> WebDistStatus:
    """``dist`` 为 None 或不是目录：不托管前端，谈不上过期。"""
    if dist is None or not (dist / "index.html").is_file():
        return WebDistStatus(False, dist=dist)
    web_root = dist.parent if (dist.parent / "src").is_dir() else None
    info_path = dist / BUILD_INFO_NAME
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = None
    if not isinstance(info, dict):
        return WebDistStatus(True, "构建产物里没有 %s（是旧版本构建的）" % BUILD_INFO_NAME, dist, web_root)
    built_version = str(info.get("version") or "")
    if built_version != version:
        return WebDistStatus(
            True, "前端构建于 %s，服务端是 %s" % (built_version or "未知版本", version), dist, web_root
        )
    if web_root is not None and info.get("source_sha256") != source_sha256(web_root):
        return WebDistStatus(True, "web/ 下的前端源码在构建之后改过", dist, web_root)
    return WebDistStatus(False, dist=dist, web_root=web_root)


def _npm() -> Optional[str]:
    # Windows 上是 npm.cmd，shutil.which 会按 PATHEXT 找到
    return shutil.which("npm")


def build(web_root: Path, install: Optional[bool] = None) -> int:
    """在 ``web_root`` 里构建前端：需要时先 ``npm ci``，再 ``npm run build``。返回退出码。

    ``install`` 为 None 时自动判断：没有 ``node_modules``，或 ``package-lock.json`` 比它新，就先装依赖。
    """
    npm = _npm()
    if npm is None:
        print("错误：找不到 npm，先安装 Node.js（https://nodejs.org/）", flush=True)
        return 2
    modules = web_root / "node_modules"
    lock = web_root / "package-lock.json"
    if install is None:
        install = not modules.is_dir() or (
            lock.is_file() and lock.stat().st_mtime > modules.stat().st_mtime
        )
    steps = ([[npm, "ci"]] if install else []) + [[npm, "run", "build"]]
    for argv in steps:
        print("[i] %s（在 %s）" % (" ".join(["npm"] + argv[1:]), web_root), flush=True)
        code = subprocess.call(argv, cwd=str(web_root))
        if code != 0:
            print("错误：%s 失败（退出码 %d）" % (" ".join(["npm"] + argv[1:]), code), flush=True)
            return code
    return 0
