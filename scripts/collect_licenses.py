#!/usr/bin/env python3
"""生成第三方许可证声明。

数据来源（全部实际读取，不手写）：

1. 前端 ``web/package.json`` 的 ``dependencies``：版本、许可证取自 ``web/node_modules`` 里已安装包的
   ``package.json``，全文取自包目录里的 LICENSE / LICENCE / COPYING / NOTICE 文件；
2. 服务端 ``server/pyproject.toml`` 的 ``dependencies``：许可证与全文取自已安装发行包的元数据与随附的
   许可证文件，版本列写依赖声明里的版本范围（服务端不锁版本）；
3. 随仓库分发的源码：``server/syncoj_server/judging/vendor/``（testlib.h）。

运行时调用、不随本项目分发的外部程序（isolate、gcc/g++）单列一表，不收全文。

用法::

    python scripts/collect_licenses.py            写入 THIRD-PARTY-LICENSES.md 与前端清单
    python scripts/collect_licenses.py --check    校验磁盘内容与依赖一致（不一致退出码 1）

``--check`` 对服务端依赖只比较名称、版本范围与许可证标识，不比较全文：服务端依赖不锁版本，
各环境装到的小版本不同，全文里的年份会随之变化。重新生成时全文按当前环境刷新。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_PATH = ROOT / "THIRD-PARTY-LICENSES.md"
CREDITS_PATH = ROOT / "web" / "src" / "legal" / "credits.generated.ts"
WEB = ROOT / "web"
PYPROJECT = ROOT / "server" / "pyproject.toml"
VENDOR = ROOT / "server" / "syncoj_server" / "judging" / "vendor"
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)([-_.].*)?(\.(md|txt|rst|markdown))?$", re.I)

#: 运行时调用、不随本项目分发的外部程序
EXTERNAL_PROGRAMS = [
    ("isolate", "GPL-2.0-or-later", "https://github.com/ioi/isolate", "自带评测 isolate 后端以子进程调用"),
    ("gcc / g++", "GPL-3.0-or-later WITH GCC-exception-3.1", "https://gcc.gnu.org/",
     "自带评测编译选手代码与 checker 时调用"),
]

PY_TEXT_OPEN = "<!-- python-license-text -->"
PY_TEXT_CLOSE = "<!-- /python-license-text -->"


class Row(object):
    def __init__(self, component: str, name: str, version: str, license: str,
                 text: Optional[str], homepage: str = "", python: bool = False) -> None:
        self.component = component
        self.name = name
        self.version = version
        self.license = license
        self.text = text
        self.homepage = homepage
        self.python = python


def _license_text(directory: Path) -> Optional[str]:
    if not directory.is_dir():
        return None
    texts = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_file() and LICENSE_FILE.match(entry.name):
            text = entry.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                texts.append(text)
    return "\n\n".join(texts) if texts else None


# --------------------------------------------------------------------------- #
# 前端
# --------------------------------------------------------------------------- #


def _npm_license(pkg: Dict[str, object]) -> Optional[str]:
    value = pkg.get("license")
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return str(value["type"])
    legacy = pkg.get("licenses")
    if isinstance(legacy, list):
        types = [item if isinstance(item, str) else item.get("type") for item in legacy]
        types = [str(t) for t in types if t]
        if types:
            return " OR ".join(types)
    return None


def collect_web() -> List[Row]:
    manifest = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    rows = []
    for name in sorted(manifest.get("dependencies", {})):
        pkg_json = WEB / "node_modules" / name / "package.json"
        if not pkg_json.is_file():
            raise SystemExit("web: 未安装 %s，先执行 (cd web && npm ci)" % name)
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        license = _npm_license(pkg)
        if license is None:
            raise SystemExit("web: %s 未声明 license" % name)
        repo = pkg.get("repository")
        homepage = pkg.get("homepage") or (repo.get("url") if isinstance(repo, dict) else repo) or ""
        rows.append(Row("前端", name, str(pkg.get("version", "")), license,
                        _license_text(pkg_json.parent), str(homepage)))
    return rows


# --------------------------------------------------------------------------- #
# 服务端
# --------------------------------------------------------------------------- #


def _pyproject_dependencies() -> List[str]:
    """读 ``[project] dependencies = [...]``（py38 没有 tomllib，按固定格式解析）。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    if not match:
        raise SystemExit("server/pyproject.toml 里找不到 dependencies")
    return re.findall(r'"([^"]+)"', match.group(1))


def _split_requirement(requirement: str):
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)(\[[^\]]*\])?\s*(.*)$", requirement)
    if not match:
        raise SystemExit("无法解析依赖声明：%s" % requirement)
    return match.group(1), match.group(3).strip() or "任意"


def _python_license(meta) -> Optional[str]:
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    value = (meta.get("License") or "").strip()
    if value and "\n" not in value and len(value) <= 64 and value.upper() != "UNKNOWN":
        return value
    found = []
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            found.append(classifier.split(" :: ")[-1])
    return " OR ".join(found) if found else None


def collect_server() -> List[Row]:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - py38 自带
        raise SystemExit("需要 Python 3.8+")
    rows = []
    for requirement in _pyproject_dependencies():
        name, spec = _split_requirement(requirement)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            raise SystemExit("server: 未安装 %s，先执行 pip install -e server" % name)
        meta = dist.metadata
        license = _python_license(meta)
        if license is None:
            raise SystemExit("server: %s 未声明许可证" % name)
        texts = []
        for item in dist.files or []:
            if LICENSE_FILE.match(Path(str(item)).name):
                path = Path(str(dist.locate_file(item)))
                if path.is_file():
                    texts.append(path.read_text(encoding="utf-8", errors="replace").strip())
        homepage = meta.get("Home-page") or ""
        for url in meta.get_all("Project-URL") or []:
            label, _, link = url.partition(",")
            if not homepage and label.strip().lower() in ("homepage", "source", "repository"):
                homepage = link.strip()
        rows.append(Row("服务端", meta.get("Name") or name, spec, license,
                        "\n\n".join(t for t in texts if t) or None, homepage, python=True))
    return rows


# --------------------------------------------------------------------------- #
# 随仓库分发的源码
# --------------------------------------------------------------------------- #


def collect_vendored() -> List[Row]:
    header = (VENDOR / "testlib.h").read_text(encoding="utf-8", errors="replace")
    version = re.search(r'#define\s+VERSION\s+"([^"]+)"', header)
    text = (VENDOR / "LICENSE-testlib").read_text(encoding="utf-8").strip()
    return [Row("评测（随仓库分发）", "testlib.h", version.group(1) if version else "",
                "MIT", text, "https://github.com/MikeMirzayanov/testlib")]


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown(rows: List[Row]) -> str:
    out = [
        "# 第三方许可证声明",
        "",
        "SyncOJ 版权所有 © 2026 muyangplus，依据 Apache License 2.0 授权（见 `LICENSE`、`NOTICE`）。以下为本项目使用的第三方软件。",
        "",
        "本文件由 `python scripts/collect_licenses.py` 生成：版本与许可证取自依赖清单与实际安装的依赖元数据，",
        "许可证全文取自各包自带的许可证文件。收录范围：",
        "",
        "- 前端 `web/package.json` 的 `dependencies`；",
        "- 服务端 `server/pyproject.toml` 的 `dependencies`（版本列为声明的版本范围）；",
        "- 随仓库分发的源码 `server/syncoj_server/judging/vendor/`。",
        "",
        "前端构建产物另附 `web/dist/assets/third-party-licenses.txt`：列出实际打包进产物的全部第三方包"
        "（含间接依赖）及许可证全文，由 `web/scripts/license-plugin.mjs` 在构建时生成。",
        "",
        "校验一致性：`python scripts/collect_licenses.py --check`（不一致时退出码 1）。",
        "",
        "## 清单",
        "",
        "| 组件 | 依赖 | 版本 | 许可证 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        out.append("| %s | %s | %s | %s |" % (row.component, _cell(row.name), _cell(row.version), _cell(row.license)))
    out += [
        "",
        "## 运行时调用的外部程序（不随本项目分发）",
        "",
        "| 程序 | 许可证 | 来源 | 用途 |",
        "|---|---|---|---|",
    ]
    for name, license, url, usage in EXTERNAL_PROGRAMS:
        out.append("| %s | %s | %s | %s |" % (name, _cell(license), url, usage))
    out += ["", "## 许可证全文", ""]
    by_license: Dict[str, List[Row]] = {}
    for row in rows:
        by_license.setdefault(row.license, []).append(row)
    for license in sorted(by_license):
        out += ["### %s" % license, ""]
        for row in by_license[license]:
            out += ["#### %s %s（%s）" % (row.name, row.version, row.component), ""]
            if row.homepage:
                out += ["来源：%s" % row.homepage, ""]
            body = ["```text", row.text, "```"] if row.text else ["该包未随附许可证全文，许可证类型以上表声明为准。"]
            if row.python:
                body = [PY_TEXT_OPEN] + body + [PY_TEXT_CLOSE]
            out += body + [""]
    return "\n".join(out).rstrip() + "\n"


def _quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_credits(rows: List[Row]) -> str:
    out = [
        "// 由 scripts/collect_licenses.py 生成，请勿手改。",
        "export interface RuntimeCredit {",
        "  component: string",
        "  name: string",
        "  version: string",
        "  license: string",
        "}",
        "",
        "export const RUNTIME_CREDITS: readonly RuntimeCredit[] = [",
    ]
    for row in rows:
        out.append("  { component: %s, name: %s, version: %s, license: %s }," % (
            _quote(row.component), _quote(row.name), _quote(row.version), _quote(row.license)))
    out += ["]", ""]
    return "\n".join(out)


_PY_BLOCK = re.compile(re.escape(PY_TEXT_OPEN) + r".*?" + re.escape(PY_TEXT_CLOSE), re.S)


def _comparable(markdown: str) -> str:
    return _PY_BLOCK.sub(PY_TEXT_OPEN, markdown).strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验第三方许可证声明")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    rows = collect_web() + collect_server() + collect_vendored()
    markdown = render_markdown(rows)
    credits = render_credits(rows)

    if args.check:
        problems = []
        current = MARKDOWN_PATH.read_text(encoding="utf-8") if MARKDOWN_PATH.is_file() else ""
        if _comparable(current) != _comparable(markdown):
            problems.append("THIRD-PARTY-LICENSES.md")
        current_credits = CREDITS_PATH.read_text(encoding="utf-8") if CREDITS_PATH.is_file() else ""
        if current_credits.strip() != credits.strip():
            problems.append("web/src/legal/credits.generated.ts")
        if problems:
            print("许可证声明与依赖不一致：%s" % "、".join(problems), file=sys.stderr)
            print("执行 python scripts/collect_licenses.py 重新生成", file=sys.stderr)
            return 1
        print("许可证声明一致：%d 项依赖" % len(rows))
        return 0

    CREDITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    for path, content in ((MARKDOWN_PATH, markdown), (CREDITS_PATH, credits)):
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    print("已写入许可证声明：%d 项依赖" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
