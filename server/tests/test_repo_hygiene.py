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

import re
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
    ".ps1",
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

#: 行尾必须是 LF 的文件类型。
#:
#: ``.ps1`` 不在其中：PowerShell 两种行尾都认，而它的主要读者是 Windows ——
#: 为了一个不会出错的约束去要求 LF，只会让编辑器反复改回来。
#:
#: 前端那几种（``.ts``/``.vue``/``.json``/``.mjs``）在名单里是有原因的，不是
#: 顺手加的：``web/scripts/check-routes.mjs`` 靠**逐行比较**认方法
#: （``line === '}'``），带 CR 的行尾会让它一条都认不出来；而它报出来的是
#: "入口对账自身失效，多半是文件被重新格式化" —— 指的方向没错，只是没人会
#: 想到罪魁祸首是行尾符。生成物（``web/openapi.json``）同理，它是被逐字节比对的。
LF_ONLY_SUFFIXES = (".sh", ".py", ".ini", ".service", ".tmpl", ".ts", ".vue", ".json", ".mjs")


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


def test_generated_openapi_json_uses_lf() -> None:
    """生成物不能带 CR。

    `web/openapi.json` 由 `server/tools/dump_openapi.py` 写出来。Windows 上
    ``Path.write_text()`` 默认把 ``\\n`` 翻成 ``\\r\\n``，于是开发机上的生成物
    和 CI 上的差了一整个文件的字节数 —— diff 里"全变了"，真正改动的两行淹在
    里面。而 ``read_text()`` 又会把 CRLF 翻译回 LF，所以"是否已最新"的检查
    完全看不见这件事。已在脚本里显式写 ``newline="\\n"``，这里守住它。

    `.json` 刻意不在 :data:`LF_ONLY_SUFFIXES` 里（JSON 规范允许 CRLF），所以
    这条针对具体生成物的检查是必需的，不是重复。
    """
    path = REPO_ROOT / "web" / "openapi.json"
    if not path.is_file():  # pragma: no cover
        pytest.skip("web/openapi.json 不存在")

    assert b"\r" not in path.read_bytes(), (
        "web/openapi.json 里有 CR；请重新运行 "
        "python server/tools/dump_openapi.py（它会写 LF）"
    )


#: 会被**逐字节**或**逐行**处理的子树 → 它们在 .gitattributes 里必须钉死 LF。
PINNED_LF_PREFIX = "web/"


def _attr_pattern_to_regex(pattern: str):
    """把 .gitattributes 里的一条模式翻成正则。

    只实现这个仓库真的用到的通配符（``**`` / ``*`` / ``?``）—— 写一个通用的
    gitattributes 实现是另一个项目，而这里需要回答的只是一个很窄的问题：
    "某条 ``eol=lf`` 的规则**管不管**这个文件。
    """
    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^%s$" % "".join(out))


def _attr_matches(pattern: str, path: str) -> bool:
    """gitattributes 的匹配规矩：**不含斜杠**的模式按文件名匹配（任意层级）。

    这一条必须实现对，否则 ``*.py`` 会被当成"只匹配仓库根下的 .py"，而 git 的
    真实语义是"任意层级" —— 在这个仓库里两者的差别到处都是（``.gitattributes``
    里除 ``web/**`` 之外全是这种模式）。
    """
    if "/" in pattern:
        return _attr_pattern_to_regex(pattern).match(path) is not None
    return _attr_pattern_to_regex(pattern).match(path.rsplit("/", 1)[-1]) is not None


def _attr_rules() -> "list[tuple[str, list]]":
    """按文件里的**顺序**读出 (模式, 属性列表)。

    顺序要紧：gitattributes 的规矩是后面的规则覆盖前面的，所以不能塞进 dict。
    """
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        rules.append((parts[0], parts[1:]))
    return rules


def effective_eol(path: str, rules=None) -> "str | None":
    """这个路径最终生效的 ``eol``（``lf`` / ``crlf`` / ``None``）。"""
    if rules is None:  # pragma: no cover - 便利默认值
        rules = _attr_rules()
    eol = None
    for pattern, values in rules:
        if not _attr_matches(pattern, path):
            continue
        for value in values:
            if value.startswith("eol="):
                eol = value[4:]
            elif value in ("binary", "-text"):
                eol = None
    return eol


def test_frontend_tree_is_pinned_to_lf(files: List[Path]) -> None:
    """整棵 ``web/`` 必须在 .gitattributes 里钉死 LF。

    上面那条检查看的是**当前工作区**的内容，所以开发机上它是绿的；但在
    ``core.autocrlf=true`` 的 Windows 上做一次全新 checkout、或者只是
    ``git stash`` 一次往返，git 就会把这些文件写成 CRLF。这一次真的发生过：
    stash 之后 ``check-routes.mjs`` 从 index.ts 里认出 **0** 个方法，报的是
    "多半是文件被重新格式化"。

    而"照着提示去格式化一遍"是修不好的 —— 声明不在，下次 checkout 又会变回去。
    所以声明本身也要守住。这条只读 .gitattributes，任何平台上结论一致。
    """
    web_files = [
        str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        for path in files
        if str(path.relative_to(REPO_ROOT)).replace("\\", "/").startswith(PINNED_LF_PREFIX)
    ]
    assert web_files, "一个 web/ 下的受跟踪文件都没找到，说明这条检查自己失效了"

    rules = _attr_rules()
    wrong = [name for name in web_files if effective_eol(name, rules) != "lf"]

    assert not wrong, (
        "这些前端文件没有在 .gitattributes 里钉死 LF，新 checkout 的机器上会变成 CRLF，"
        "于是按行解析它们的工具（先是 check-routes.mjs）会静默失效：%r" % wrong[:10]
    )


def test_pin_check_actually_catches_a_missing_pin() -> None:
    """自证：规则读歪了、或者声明被删了，都必须能被认出来。

    没有这一条的话，``_attr_pattern_to_regex`` 写错（比如 ``**`` 没翻成 ``.*``）
    会让上面那条检查悄悄变成"空列表是空列别的子集"，永远绿。
    """
    rules = [("*.py", ["text", "eol=lf"]), ("web/**", ["text", "eol=crlf"])]

    assert effective_eol("web/src/api/index.ts", rules) == "crlf"
    assert effective_eol("server/syncoj_server/main.py", rules) == "lf"
    # 没有规则管它 → 没有生效的 eol，也算"没钉死"
    assert effective_eol("docs/protocol.md", rules) is None
    # 后面的规则覆盖前面的
    assert effective_eol("web/x.py", [("*.py", ["eol=lf"]), ("web/**", ["eol=crlf"])]) == "crlf"
