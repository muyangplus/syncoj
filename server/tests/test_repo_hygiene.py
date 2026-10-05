"""仓库卫生检查。

这些东西在 Windows 上开发时**完全无害**，到了 NOI Linux 上就是致命的 ——
所以必须由测试来守，不能靠"我记得"。

两类问题都是真实踩过的：

- **UTF-8 BOM**：``#!/bin/sh`` 前面多一个 U+FEFF，Linux 会把它当成解释器
  路径的一部分，报 "cannot execute: required file not found" —— 一个
  完全看不出是编码问题的错误。
- **CRLF**：``sh`` 会因为行尾的 ``\\r`` 把命令参数改掉（``$'\\r'``），
  表现是莫名其妙的参数错误。

开发机是 Windows，编辑器默认行为随时可能引入这两样。``.gitattributes``
已经声明了应当的换行符，但那是"声明"不是"强制" —— 有人手动改一个文件、
或者某个工具直接写盘，声明就绕过去了。这里做实际检查。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 用 UTF-8 BOM 开头会出问题的文件类型。
#: 特意**不**检查 ``.json`` —— JSON 规范允许 BOM，而且 ``web/openapi.json``
#: 是生成物，改它没意义。
BOM_SENSITIVE_SUFFIXES = (
    ".sh",
    ".py",
    ".ini",
    ".md",
    ".tmpl",
    ".service",
    ".cfg",
    ".toml",
    ".ts",
    ".vue",
    ".txt",
)

#: 运行在 Linux 上、行尾必须是 LF 的文件类型。
LF_ONLY_SUFFIXES = (".sh", ".py", ".ini", ".service", ".tmpl")


def tracked_files() -> List[Path]:
    """列出 git 跟踪的文件。用 git 而不是遍历目录，天然排除生成物与缓存。"""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=True,
    )
    names = result.stdout.decode("utf-8").split("\0")
    return [REPO_ROOT / name for name in names if name]


@pytest.fixture(scope="module")
def files() -> List[Path]:
    try:
        return tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip("拿不到 git 文件清单: %s" % exc)


def find_bom_offenders(paths: List[Path]) -> List[str]:
    """挑出以 UTF-8 BOM 开头的文件。抽成函数是为了能被单独验证 —— 一个
    永远不会红的检查等于没有检查。"""
    offenders = []
    for path in paths:
        if path.suffix not in BOM_SENSITIVE_SUFFIXES or not path.is_file():
            continue
        if path.read_bytes()[:3] == b"\xef\xbb\xbf":
            offenders.append(str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path))
    return offenders


def find_cr_offenders(paths: List[Path]) -> List[str]:
    """挑出含 CR 的 shell / Python / INI 文件。"""
    offenders = []
    for path in paths:
        if path.suffix not in LF_ONLY_SUFFIXES or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\r" in data:
            label = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
            offenders.append("%s（%d 处 CR）" % (label, data.count(b"\r")))
    return offenders


@pytest.fixture(scope="module")
def files() -> List[Path]:
    try:
        return tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip("拿不到 git 文件清单: %s" % exc)


def test_no_file_starts_with_a_utf8_bom(files: List[Path]) -> None:
    """任何文本源文件都不能以 UTF-8 BOM 开头。

    BOM 在 Windows 的编辑器里是"体贴"，在 Linux 上是故障：``install.py``
    真的被这么坑过一次 —— 一次 PowerShell 文本替换给它加了 BOM，
    diff 里只看得出第一行"变了"，肉眼看不出多了三个字节。
    """
    offenders = find_bom_offenders(files)

    assert not offenders, (
        "以下文件以 UTF-8 BOM 开头，在 Linux 上会引发难以定位的故障：\n"
        + "\n".join("  %s" % name for name in offenders)
        + "\n\n去掉 BOM：\n"
        "  python -c \"from pathlib import Path;p=Path('文件');p.write_bytes(p.read_bytes().lstrip(b'\\xef\\xbb\\xbf'))\""
    )


def test_shell_and_python_files_use_lf_only(files: List[Path]) -> None:
    """shell 脚本与 Python 源文件里不能出现 CR。

    ``sh`` 会把行尾的 ``\\r`` 当成命令的一部分，报错信息完全对不上真实原因。
    """
    offenders = find_cr_offenders(files)

    assert not offenders, (
        "以下文件里有 CR，在 Linux 上执行会出错：\n"
        + "\n".join("  %s" % name for name in offenders)
        + "\n\n.gitattributes 已经声明了换行符，但声明绕不过直接写盘的工具。"
    )


# --------------------------------------------------------------------------- #
# 上面两条检查自身的可证伪性
# --------------------------------------------------------------------------- #


def test_bom_check_actually_catches_a_bom(workdir: Path) -> None:
    """用一个真的带 BOM 的文件验证检查会红。

    写这条是因为刚吃过一次亏：另一处测试里我写了
    ``assert f() is not False``，而 ``f()`` 永远返回 ``None`` ——
    断言恒真，测试永远是绿的，等于没写。
    """
    bad = workdir / "bad.py"
    bad.write_bytes(b"\xef\xbb\xbfprint('hi')\n")
    good = workdir / "good.py"
    good.write_bytes(b"print('hi')\n")
    other = workdir / "notes.json"      # 不在检查范围内
    other.write_bytes(b"\xef\xbb\xbf{}")

    offenders = find_bom_offenders([bad, good, other])
    # 只比较文件名：路径前缀取决于测试临时目录放在哪，断言整个路径会假红
    assert [Path(name).name for name in offenders] == ["bad.py"], offenders


def test_cr_check_actually_catches_crlf(workdir: Path) -> None:
    bad = workdir / "bad.sh"
    bad.write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    good = workdir / "good.sh"
    good.write_bytes(b"#!/bin/sh\necho hi\n")

    offenders = find_cr_offenders([bad, good])
    assert len(offenders) == 1 and "bad.sh" in offenders[0], offenders
