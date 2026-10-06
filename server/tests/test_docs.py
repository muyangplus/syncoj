"""文档守卫。

文档体系是按受众重建过的（见 `docs/README.md`）：`README.md` 只留入口、
`docs/design/` 按主题装着原 `DESIGN.md` 的正文、`docs/decisions/` 装着 ADR。
重建之后最容易坏掉的不是某一句话，而是**结构性**的东西：一份没人能索引到的
文档、一处指向已经搬走的文件的引用、一个只写在文档里而代码里没有的配置键。
这些东西靠人记住是不行的 —— 所以由这个文件守着，纯标准库、快，与
`test_repo_hygiene.py` 同一个路子。

五条检查，每一条都对应一次真实的坏法：

1. **索引双向完整**：`docs/**` 下每个 md 都在 `docs/README.md` 的清单里，
   清单里每个文件都存在。一份没人能找到的文档等于不存在。
2. **`§X.Y` 引用能落到地方**：全仓库（代码注释 + 文档）里的 `§` 引用，
   必须能在 `docs/design/` 的标题里找到对应编号，或落到它自己那份文档
   （协议 / API 约定）。这条是重建时"编号一律不许动"那个约束的守卫。
3. **相对链接存在**：`[文本](路径)` 指到不存在的东西。
4. **`README.md` ≤ 40 行、`DESIGN.md` ≤ 60 行**：这两份是入口与映射表，
   一旦重新长回来，就等于重建没做。
5. **`config-keys.md` 与代码对账**：文档是"逐键策略"的第三方，
   必须与 `CONFIG_POLICY_KEYS` 和 `config.example.ini` 三方一致。
"""

from __future__ import annotations

import configparser
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCS_INDEX = Path("docs") / "README.md"

MAX_README_LINES = 40
MAX_DESIGN_LINES = 60

#: 文档里出现 `§X` 时，`§` **前面紧邻**的文档路径。用它把"跨文件编号"
#: 与"本文件自己的编号"分开。旧体系里写成 `docs/protocol.md §1`，
#: 搬家之后可以是 `reference/protocol.md §1` 或 `../reference/protocol.md §1`。
DOC_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)[`\s]*§\s*(\d+(?:\.\d+)*)")

#: 代码里"没有显式文档名"的 `§X` 一律理解成 DESIGN 的编号 ——
#: 这正是当初决定保留 `DESIGN.md` 映射表的原因（老文档里的编号引用有几十处，逐个改成新路径的收益是零、改错的风险很大）。
BARE_SECTION_RE = re.compile(r"§(\d+(?:\.\d+)*)")

#: `§` 如果紧跟在路径字符后面，它属于那个路径表达式的一部分，
#: 不是一条编号引用。这个仓库里真实出现过的是 shell 变量：
#: `sha[${sha:0:2}]` 被展开时的 `${sha:0:2}` —— 同类写法里出现 `§` 时一样要跳过。
PATH_CHAR_BEFORE = re.compile(r"[A-Za-z0-9_.$/\\-]")

#: 本文件内用文字提到文档名时，`§` 归到那一份。只认这两份，因为只有它们
#: **用自己的编号引自己**（协议与 API 约定各是一套独立编号）。
SELF_REF_DOCS = ("protocol.md", "api-conventions.md")

#: 文档名后面 **跟着** 的 `§` —— 这种已经由 `DOC_REF_RE` 认过了。
FOLLOWED_BY_SECTION_RE = re.compile(r"\.md`?\s*§")

#: 还没被删除的旧文档（搬家之后由 `docs/reference/` 那份取代）。
#: 它们自己还在磁盘上，但**不参与引用检查** —— 否则"`docs/protocol.md §7` 在
#: `docs/protocol.md` 里找得到"会让检查白绿一遍。
STALE_PATHS = (
    "docs/protocol.md",
    "docs/api-conventions.md",
    "docs/judge-result.md",
)

#: 编号 -> 该编号所在的设计文档。**从 `DESIGN.md` 的映射表里读**，
#: 不在这里再抄一份 —— 抄一份就等于有两个真相。
#:
#: 解析方式刻意用"切单元格"而不是一条大正则：这一行的形状是
#: `| §4 服务端（4.1–4.8） | [`docs/design/04-server.md`](docs/design/04-server.md) |`，
#: 里面有嵌套的反引号和方括号，一条正则要同时容忍表格竖线、括号、反引号三层，
#: 写歪了就会静默读不出来（第一版就是这样：映射表读出来是空的，
#: 报错却长得像"编号不存在"）。切开之后每个字段只看一眼。
#: 第一列里的编号：**必须带 `§`**。不带 `§` 就会把标签文字里的数字也算进来 ——
#: "§3.6 Python 3.8 兼容清单" 里的那个 `3.8` 会被当成一个编号，
#: 然后报"03-agent.md 里没有编号 3.8 的标题"。这个误报第一版真的出现过。
MAPPING_CELL_NUM_RE = re.compile(r"§\s*(\d+(?:\.\d+)*)")
MAPPING_CELL_LINK_RE = re.compile(r"\[`([^`]+)`\]\(([^)]+)\)")

#: 设计文档里的编号标题：`## 4. 服务端` / `### 4.6 注册与配对：一条路`
DESIGN_HEADING_RE = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+)*)[.\s]")

#: 协议那类编号引用在仓库里还可能是 **RFC 8017 §8.2.2**（密码学标准的章节号）。
#: 那是另一套编号，不该拿去和设计文档的编号对。判据取"同一行里提到 RFC"。
RFC_LINE_RE = re.compile(r"\bRFC\s*\d+")

MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: 文档里逐键表格的一行。**只看第一个单元格** —— 说明列里也有反引号包着的名字
#: （`credential.json`、`machine_uuid`…），顺手扫整行会把它们也当成配置键。
KEY_TABLE_ROW_RE = re.compile(r"^\|\s*`([a-z_]+\.[a-z_]+)`\s*\|")


#: 遍历要跳过的目录。**必须排掉这几个**：`web/node_modules` 与 `.venv` 加起来有
#: 几万个文件，一次全盘扫描要几十秒 —— 而测试超时看起来像"守卫写坏了"。
#: `.uv-cache` 也是同类东西（uv 的包缓存，里面有第三方包的源码，
#: 它们的注释里自然带着 RFC 章节号）。生成物与缓存里不该有我们的文档。
SKIP_DIRS = {
    ".git", ".venv", "node_modules", "dist", "__pycache__",
    ".pytest-tmp", ".mypy_cache", ".ruff_cache", ".uv-cache",
    "build", "releases", ".idea", ".vscode",
}


#: 会被扫 `§` 引用与相对链接的文件类型。
SCANNED_SUFFIXES = (".md", ".py", ".ts", ".vue", ".json", ".sh", ".ps1", ".ini")


def all_doc_bearing_files() -> List[str]:
    """仓库里所有可能写着文档引用的文件（仓库相对路径，正斜杠）。

    **不走 `git ls-files`**：这次重建新增的文件在提交之前都是 untracked，
    而 `git ls-files` 看不见它们 —— 第一版的索引检查就是这么变成"一个 docs/
    下的 md 都没找到"的（`test_...` 里那句"这条检查自己失效了"把它抓出来了，
    那正是它存在的理由）。走磁盘就不会有这个问题，新写的文档第一次跑测试
    就能被检查到。
    """
    found = []
    # 逐层走、遇到 SKIP_DIRS 就剪枝：`rglob("*")` 会先把 `.venv` 与
    # `node_modules` 里的每一个文件都枚举出来再过滤，那部分开销白花。
    stack = [REPO_ROOT]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if child.is_dir():
                if child.name in SKIP_DIRS:
                    continue
                stack.append(child)
                continue
            if child.suffix in SCANNED_SUFFIXES:
                found.append(child.relative_to(REPO_ROOT).as_posix())
    return sorted(found)


@pytest.fixture(scope="module")
def scanned() -> List[str]:
    return all_doc_bearing_files()


def read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def markdown_files(scanned: Sequence[str]) -> List[str]:
    return [name for name in scanned if name.endswith(".md")]


def doc_name_index(scanned: Sequence[str]) -> Dict[str, List[str]]:
    """`文档文件名 -> 它可能出现的位置`（仓库相对路径，按深度浅的在前）。

    用它替代 `rglob`：`Path.rglob` 会一路走进 `web/node_modules` 与 `.venv`
    （几万个文件），一次扫描要几十秒 —— 测试超时看起来像"检查写坏了"，
    而真正的原因只是找文件的方式太笨。`git ls-files` 一次就够，天然排除生成物。

    重名的 md **不算错**（仓库里就有三个 `README.md`）。引用只写文件名，
    所以查找时按"离引用者最近"来定：先看同目录，再逐级向上找到仓库根。
    这样 `docs/decisions/README.md` 里写 `README.md` 指的是它自己那一层，
    而 `docs/reference/` 里也不会被根目录那份抢走。
    """
    index: Dict[str, List[str]] = {}
    for name in scanned:
        if not name.endswith(".md"):
            continue
        index.setdefault(Path(name).name, []).append(name)
    for paths in index.values():
        paths.sort(key=lambda p: (p.count("/"), p))
    return index


# --------------------------------------------------------------------------- #
# 1. 索引双向完整
# --------------------------------------------------------------------------- #

def index_links() -> Set[str]:
    """`docs/README.md` 清单里指向的文件（仓库相对路径）。

    链接是相对 `docs/README.md` 写的（`quickstart.md`、`reference/protocol.md`），
    所以解析基准是 `docs/`，而**不是**进程的当前目录 —— 后者会让这条检查
    在从仓库根跑和从 `server/` 跑时给出不同答案。
    """
    text = read(DOCS_INDEX.as_posix())
    found = set()
    for target in MD_LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        plain = target.split("#", 1)[0]
        if not plain or plain.endswith("/"):
            continue
        resolved = (REPO_ROOT / "docs" / plain).resolve()
        try:
            rel = resolved.relative_to(REPO_ROOT)
        except ValueError:
            continue
        found.add(rel.as_posix())
    return found


def test_docs_index_lists_every_doc_and_nothing_more(scanned: List[str]) -> None:
    """`docs/**` 下的每个 md 都要在 `docs/README.md` 里出现，反之亦然。

    反向那半（清单里写的文件必须存在）同样要紧：清单里留着一个已经搬走的文件名，
    读的人会照着去找，然后得到一个 404 —— 而他会以为是自己的问题。
    列表里有目录（`reference/`、`design/`…），那些只查"存在"。
    """
    on_disk = {name for name in markdown_files(scanned) if name.startswith("docs/")}
    listed = {name for name in index_links() if name.startswith("docs/")}

    # 清单里可以出现目录（`docs/reference/` 这种），它们不是文件。
    # 逐个按"目录存在"检查，然后从"文件"集合里去掉。
    #
    # 基准是**仓库根**，不是 `Path("docs/README.md").parent` —— 后者会跟着
    # 进程的当前目录走（从 `server/` 跑 pytest 时就变成 `server/docs/`），
    # 于是"这个目录不存在"这种假红会随调用方式出现和消失。
    text = read(DOCS_INDEX.as_posix())
    for target in MD_LINK_RE.findall(text):
        plain = target.split("#", 1)[0]
        if plain.endswith("/"):
            resolved = (REPO_ROOT / "docs" / plain).resolve()
            assert resolved.is_dir(), "docs/README.md 里链了一个不存在的目录：%s" % plain

    missing_from_index = sorted(on_disk - listed)
    dangling = sorted(name for name in listed if not (REPO_ROOT / name).is_file())

    assert not missing_from_index, (
        "这些文档在磁盘上但 `docs/README.md` 的清单里没有 —— "
        "一份没人能从索引找到的文档等于不存在：\n"
        + "\n".join("  %s" % name for name in missing_from_index)
    )
    assert not dangling, (
        "`docs/README.md` 的清单里列了这些文件，但它们不存在：\n"
        + "\n".join("  %s" % name for name in dangling)
    )
    assert on_disk, "一个 docs/ 下的 md 都没找到，说明这条检查自己失效了"


# --------------------------------------------------------------------------- #
# 2. `§X.Y` 引用必须落得到地方
# --------------------------------------------------------------------------- #

def design_section_map() -> Dict[str, str]:
    """从 `DESIGN.md` 的映射表读 `编号 -> 设计文档路径`（仓库相对路径）。

    一个单元格里可以写多个编号（同一节在不止一个文件里时用得上），
    所以对第一列取**所有**数字。只认有两列的表格行，并且第二列里得有一个
    `` [`路径`](路径) `` —— 表头行与说明段落都不会被算进来。
    """
    mapping: Dict[str, str] = {}
    for line in read("DESIGN.md").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        link_match = MAPPING_CELL_LINK_RE.search(cells[1])
        if not link_match:
            continue
        numbers = MAPPING_CELL_NUM_RE.findall(cells[0])
        if not numbers:
            continue
        for number in numbers:
            mapping.setdefault(number, link_match.group(1))
    return mapping


def design_headings() -> Dict[str, Set[str]]:
    """每份设计文档里出现了哪些编号标题。"""
    found: Dict[str, Set[str]] = {}
    for path in sorted((REPO_ROOT / "docs" / "design").glob("*.md")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        numbers = {
            match.group(1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if (match := DESIGN_HEADING_RE.match(line))
        }
        found[rel] = numbers
    return found


def doc_headings(rel: str) -> Set[str]:
    numbers = set()
    for line in read(rel).splitlines():
        match = DESIGN_HEADING_RE.match(line)
        if match:
            numbers.add(match.group(1))
    return numbers


def test_design_map_and_design_files_agree() -> None:
    """映射表里的每个编号都要真的能落到一份设计文档的标题上。

    映射表是给**代码里那些老式 `DESIGN.md` 编号引用**用的落点，所以它错一个编号，
    引用就静默失效 —— 而失败的样子是"文档里找不到那一节"，没人会为此开一张单。

    查的是**单向**的：映射表 -> 标题。反过来（每个标题都要在表里有落点）是错的：
    映射表只需要盖住**代码里真的引用过**的编号，而 `design/00-decisions.md` 本身就
    只是一个标题（`# 0. 已锁定决策`）—— 要求它也进表，就得为它编一行没有引用的记录。
    """
    mapping = design_section_map()
    headings = design_headings()

    assert mapping, "从 DESIGN.md 里一个 § 编号都没读出来，说明映射表格式变了"

    missing_section = []
    for number, target in sorted(mapping.items()):
        numbers = headings.get(target)
        if numbers is None:
            missing_section.append("%s -> %s（这份设计文档不存在）" % (number, target))
        elif number not in numbers:
            missing_section.append(
                "%s -> %s（该文件里没有编号 %s 的标题）" % (number, target, number)
            )

    assert not missing_section, (
        "DESIGN.md 的映射表里有这些编号落不到地方：\n"
        + "\n".join("  %s" % item for item in missing_section)
    )


def resolve_doc_ref(referrer: str, name: str, index: Dict[str, List[str]]) -> "str | None":
    """把一条 `§` 引用里的文档名解析成仓库相对路径。

    顺序是"离引用者最近的那一份"：先看引用者所在目录，再一级一级往上找。
    这样 `docs/decisions/README.md` 里的 `README.md` 指它自己那一层，
    而不会莫名指到仓库根下那份。
    """
    candidates = index.get(name)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    current = Path(referrer).parent
    while True:
        prefix = (current / name).as_posix()
        if prefix in candidates:
            return prefix
        if current.as_posix() in (".", ""):
            break
        current = current.parent
    # 指到了别的目录里的一份同名文档（例如 `docs/decisions/README.md` 说
    # `../design/README.md`）—— 那种写法本来就该带路径，这里退回最浅的那份。
    return candidates[0]


def scan_section_refs(rel: str) -> Tuple[List[Tuple[str, str, int]], List[str]]:
    """扫一个文件里的 `§` 引用。

    返回 `(显式文档引用 [(文档名, 编号, 行号)], 无文档名的编号 [编号])`。

    判据的边界都在这里，写下来是因为每一条都对应一种真实的误报：

    * **`§` 紧跟在路径字符后面** -> 不是编号引用（`${sha:0:2}` 那类表达式）。
    * **同一行里出现 `RFC`** -> 那是密码学标准的章节号（`RFC 8017 §9.2`），
      与设计文档的编号体系无关。
    * **同一行里出现了 `protocol.md` / `api-conventions.md`** -> 这个 `§` 是
      **那一份文档**的编号（协议与 API 约定各是一套独立编号）。只对**文档**这样判；
      代码里出现别的文档名时，`§` 仍然是 DESIGN 的编号
      （`migrations.py` 里那句老式编号引用就是这种）。

    一个真实踩到的坑：`agent/syncoj_agent/client.py` 里有一行

        # 这一个形状（见协议文档里那一节）

    —— `§` 前面带的是**仓库相对路径** `docs/protocol.md`，而 `DOC_REF_RE` 从紧邻的
    那串路径字符开始匹配，拿到的是 `docs/protocol.md`（对的）。但同一行如果写成
    但同一行如果写成 `` `protocol.md` §0.1 ``（先关反引号再空格），本行里就只剩
    文件名了 —— 两种写法都要归到同一份文档上，所以判据取的是**文件名**。
    """
    explicit: List[Tuple[str, str, int]] = []
    bare: List[str] = []
    for lineno, line in enumerate(read(rel).splitlines(), 1):
        if not line.strip():
            continue
        rfc = bool(RFC_LINE_RE.search(line))
        doc_hits = list(DOC_REF_RE.finditer(line))
        covered = set()
        for match in doc_hits:
            explicit.append((match.group(1), match.group(2), lineno))
            covered.add(match.span())
        if rfc:
            continue

        # 这一行提到的那份文档（`§` 前或后的写法都算）
        owner = None
        if rel.endswith(".md"):
            for name in SELF_REF_DOCS:
                if name in line:
                    owner = name
                    break

        # 文档名后面跟着的 `§` 已经由 doc_hits 处理过，这里把那些 `§` 也盖住
        for match in FOLLOWED_BY_SECTION_RE.finditer(line):
            covered.add((match.end() - 1, match.end()))

        for match in BARE_SECTION_RE.finditer(line):
            if any(start <= match.start() < end for start, end in covered):
                continue
            if match.start() > 0 and PATH_CHAR_BEFORE.match(line[match.start() - 1]):
                continue
            if owner is not None:
                explicit.append((owner, match.group(1), lineno))
            else:
                bare.append(match.group(1))
    return explicit, bare


def test_section_references_resolve(scanned: List[str]) -> None:
    """全仓库的 `§X` 引用都要落得到地方。

    三类来源，三种期待：

    * **代码**（`.py` / `.ts` / `.vue` / `.json`）里的裸 `§X` —— 那是 DESIGN 的编号，
      必须能在 `docs/design/` 里找到（靠映射表转一手）。
    * **文档**（`docs/*.md`）里裸 `§X` —— 指本文件自己的编号
      （协议与 API 约定都是自成一体的编号体系）。
    * **任何地方**写成 `<某文档>.md §X` —— 那就在那份文档里找编号。

    这条检查是"编号一律保持不变"那个约束的**唯一**守卫。没有它，搬家时顺手把
    老编号被改短的错就永远不会被发现，而代码里几十处引用会静默指错地方。
    """
    mapping = design_section_map()
    headings = design_headings()
    doc_index = doc_name_index(scanned)

    problems: List[str] = []

    for rel in sorted(scanned):
        if rel in STALE_PATHS:
            continue
        # DESIGN.md 自己就是映射表，"§X" 在那里是**被解释的对象**，不是引用
        if rel == "DESIGN.md":
            continue
        if not (REPO_ROOT / rel).is_file():
            continue
        if not rel.endswith((".md", ".py", ".ts", ".vue", ".json", ".sh", ".ps1", ".ini")):
            continue

        explicit, bare = scan_section_refs(rel)

        for doc, number, lineno in explicit:
            name = Path(doc).name
            target_rel = resolve_doc_ref(rel, name, doc_index)
            if target_rel is None:
                problems.append("%s:%d 引用了不存在的文档 %s" % (rel, lineno, doc))
                continue
            if number not in doc_headings(target_rel):
                problems.append(
                    "%s:%d 引用 %s §%s，但那份文档里没有这个编号"
                    % (rel, lineno, name, number)
                )

        for number in bare:
            target = mapping.get(number)
            if target is None:
                problems.append(
                    "%s 里有裸 §%s，而 DESIGN.md 的映射表里没有这个编号" % (rel, number)
                )
                continue
            if number not in headings.get(target.lstrip("./"), set()):
                problems.append(
                    "%s 里的 §%s 指向 %s，但那个文件里没有这个编号的标题"
                    % (rel, number, target)
                )

    assert not problems, (
        "这些 § 引用落不到地方（搬文档时编号被改动、或者映射表没跟上）：\n"
        + "\n".join("  %s" % item for item in problems)
    )


def test_section_scan_actually_catches_a_bad_reference() -> None:
    """自证：真引用的编号能对上，假编号对不上。

    没有这一条的话，`DOC_REF_RE`/`BARE_SECTION_RE` 写歪（比如多要求一个空格）
    会让上面那条检查悄悄变成"空列表是空列别的子集"，永远绿 ——
    这正是文档守卫最容易犯的错。
    """
    mapping = design_section_map()
    headings = design_headings()

    # 真存在的一节
    assert mapping.get("4.6") == "docs/design/04-server.md", mapping.get("4.6")
    assert "4.6" in headings["docs/design/04-server.md"]
    assert "5.1" in headings["docs/design/05-api-and-frontend.md"]

    # 一个不存在的编号必须落不到地方
    assert mapping.get("4.99") is None

    # 标签里的数字不许被当成编号（"§3.6 Python 3.8 兼容清单" 里有个 3.8）
    assert mapping.get("3.8") is None
    assert mapping.get("25519") is None
    # 而 §3.6 本身要在
    assert mapping.get("3.6") == "docs/design/03-agent.md"


# --------------------------------------------------------------------------- #
# 3. 相对链接必须存在
# --------------------------------------------------------------------------- #

def test_relative_links_exist(scanned: List[str]) -> None:
    """文档里的相对链接都要指向真实存在的文件。

    重建搬动了大量文件（`docs/protocol.md` -> `docs/reference/protocol.md`…），
    而**行内代码里的路径不会被这条检查覆盖** —— 那是它管不到的边界，
    见下面的 `test_doc_paths_are_not_stale`。
    """
    problems: List[str] = []
    checked = 0

    for rel in markdown_files(scanned):
        path = REPO_ROOT / rel
        for target in MD_LINK_RE.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            plain = target.split("#", 1)[0]
            if not plain:
                continue
            checked += 1
            resolved = (path.parent / plain).resolve()
            if not resolved.exists():
                problems.append("%s -> %s" % (rel, target))

    assert checked, "一条相对链接都没检查到，说明这条检查自己失效了"
    assert not problems, (
        "这些相对链接指到了不存在的东西：\n"
        + "\n".join("  %s" % item for item in problems)
    )


#: 行内代码里写着的、已经被搬走的路径 -> 新路径。
STALE_DOC_PATHS = {
    "docs/protocol.md": "docs/reference/protocol.md",
    "docs/api-conventions.md": "docs/reference/api-conventions.md",
    "docs/judge-result.md": "docs/reference/judge-result.md",
}


def test_doc_paths_are_not_stale() -> None:
    """文档里不该再用行内代码指着旧的 `docs/*.md` 路径。

    这条补的是上一条的盲区：`[文本](../reference/protocol.md)` 是链接、上一条查得到；
    但 `server/syncoj_server/schemas.py` 那类注释里写的是
    `` `docs/api-conventions.md` ``（行内代码），它不会被链接检查抓到。
    那三份文档搬进了 `docs/reference/`，老写法就成了指向空处的指路。
    """
    stale_lines: List[str] = []
    for rel in ("docs", "README.md", "DESIGN.md", "agent", "server", "web"):
        base = REPO_ROOT / rel
        if base.is_file():
            paths = [base]
        elif base.is_dir():
            paths = [p for p in base.rglob("*") if p.is_file()]
        else:
            continue
        for path in paths:
            if path.suffix not in (".md", ".py", ".ts", ".vue"):
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # 例外：文档里刻意留档说明"旧路径是哪个"的地方
                if "重建之前" in line or "STALE_DOC_PATHS" in line:
                    continue
                for old in STALE_DOC_PATHS:
                    if old in line:
                        stale_lines.append("%s:%d  %s" % (path.relative_to(REPO_ROOT), lineno, line.strip()[:100]))

    # 仓库里确实还留着"旧路径"的**记录**（README/DESIGN 里说明搬家前后），
    # 所以这里只要求它不再出现在代码注释里。
    code_stale = [item for item in stale_lines if item.startswith(("server/", "agent/", "web/"))]
    assert not code_stale, (
        "代码注释里还在指这些已经搬走的文档路径：\n"
        + "\n".join("  %s" % item for item in code_stale)
    )


# --------------------------------------------------------------------------- #
# 4. 入口文件不许重新长回来
# --------------------------------------------------------------------------- #

def count_lines(rel: str) -> int:
    return len(read(rel).splitlines())


def test_readme_stays_an_entry_point() -> None:
    """`README.md` 只留"是什么 + 三条命令 + 文档地图"。

    上限 40 行不是审美：它一旦重新长回几百行，`docs/` 那套按受众分流的体系就形同
    虚设 —— 而"一本大文件装着所有东西"正是这次重建要解决的那个问题。
    """
    lines = count_lines("README.md")
    assert lines <= MAX_README_LINES, (
        "README.md 有 %d 行，超过 %d 行的上限。"
        "内容该进 docs/ 下对应受众的那一份（见 docs/README.md）。"
        % (lines, MAX_README_LINES)
    )


def test_design_stays_a_mapping_table() -> None:
    """`DESIGN.md` 只剩"§ 编号 → 新文件"的映射表。

    留着它是因为代码与注释里有几十处老式编号引用 ——
    改那些引用的收益是零，所以让老引用有一个落点。它不该再长出正文。
    """
    lines = count_lines("DESIGN.md")
    assert lines <= MAX_DESIGN_LINES, (
        "DESIGN.md 有 %d 行，超过 %d 行的上限。"
        "正文应该进 docs/design/ 下对应主题的那一份，这里只留映射表。"
        % (lines, MAX_DESIGN_LINES)
    )


def test_line_budget_check_actually_bites() -> None:
    """自证：行数比较用的是真行数，不是恒真的空集合。"""
    assert count_lines("README.md") > 0
    assert count_lines("README.md") <= MAX_README_LINES
    assert count_lines("DESIGN.md") > 0
    assert count_lines("DESIGN.md") <= MAX_DESIGN_LINES
    # 一份明显超限的"文件"必须被判红
    assert count_lines("docs/reference/protocol.md") > MAX_DESIGN_LINES


# --------------------------------------------------------------------------- #
# 5. config-keys.md 与代码对账
# --------------------------------------------------------------------------- #

def documented_config_keys() -> List[str]:
    """`docs/reference/config-keys.md` 逐键表格里列出的键。"""
    keys = []
    for line in read("docs/reference/config-keys.md").splitlines():
        match = KEY_TABLE_ROW_RE.match(line)
        if match:
            keys.append(match.group(1))
    return sorted(keys)


def example_ini_keys() -> List[str]:
    """`agent/config.example.ini` 里真正被赋值的键（形如 `<段>.<键>`）。"""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "agent" / "config.example.ini", encoding="utf-8")
    return sorted(
        "%s.%s" % (section, key)
        for section in parser.sections()
        for key in parser.options(section)
    )


def policy_keys() -> List[str]:
    """`CONFIG_POLICY_KEYS` —— 逐键策略认得的所有键。"""
    import sys

    server_root = str(REPO_ROOT / "server")
    if server_root not in sys.path:
        sys.path.insert(0, server_root)
    from syncoj_server.services import install_policy

    return sorted(install_policy.CONFIG_POLICY_KEYS)


def test_config_keys_doc_matches_the_example_ini() -> None:
    """文档的键清单必须与 `agent/config.example.ini` 一模一样。

    多一个：文档里有一条**安装器不会写**的键，教师照着配了、机器上什么都没发生。
    少一个：某个键在文档里根本不存在，谁也不知道它能不能被逐键策略覆盖。
    """
    documented = documented_config_keys()
    actual = example_ini_keys()

    assert documented, "从 config-keys.md 里一个键都没读出来，说明表格格式变了"
    assert documented == actual, (
        "config-keys.md 与 agent/config.example.ini 对不上：\n"
        "  文档里多出来的：%s\n"
        "  示例里有、文档里没有的：%s"
        % (sorted(set(documented) - set(actual)), sorted(set(actual) - set(documented)))
    )


def test_config_keys_doc_matches_the_policy_keys() -> None:
    """文档的键清单必须与 `CONFIG_POLICY_KEYS` 一模一样。

    这一条与上一条是**两个方向**：上一条管"示例文件里有的键都要写到文档里"，
    这一条管"**逐键策略认得**的键都要写到文档里"。两者现在一致，
    但将来 `install_policy.py` 里加一个键而示例文件没跟上时，
    这个测试先红，指出的位置比契约测试更靠前。
    """
    documented = documented_config_keys()
    declared = policy_keys()

    assert declared, "CONFIG_POLICY_KEYS 是空的，说明导入的方式变了"
    assert documented == declared, (
        "config-keys.md 与 CONFIG_POLICY_KEYS 对不上：\n"
        "  文档里多出来的：%s\n"
        "  策略清单里有、文档里没有的：%s"
        % (sorted(set(documented) - set(declared)), sorted(set(declared) - set(documented)))
    )


def test_key_table_parser_actually_reads_the_table() -> None:
    """自证：键名表格确实被解析出 20 条，而且说明列里的反引号没被误当成键。

    `credential.json` / `machine_uuid` / `agent.ini.syncoj-default` 这些都出现在
    说明列里并且带着反引号 —— 解析器若扫整行而不是只看第一个单元格，
    它们会被算成"多出来的键"，这条检查就会以另一种方式恒红/恒绿。
    """
    keys = documented_config_keys()
    assert len(keys) == 20, keys
    assert "server.url" in keys
    assert "upgrade.public_key" in keys
    # 说明列里出现的那些东西不该被当成键
    assert "credential.json" not in keys
    assert "machine_uuid" not in keys
    assert all(key.count(".") == 1 for key in keys), keys


# --------------------------------------------------------------------------- #
# 文档守卫自己的先决条件
# --------------------------------------------------------------------------- #

def test_guarded_files_all_exist() -> None:
    """本文件检查的那些东西真的在仓库里。

    没有这一条的话，"读不到文件"会以 `FileNotFoundError` 的形式冒出来，
    而那种失败看起来像是环境问题，不像"文档被删了"。
    """
    for rel in (
        "README.md",
        "DESIGN.md",
        "docs/README.md",
        "docs/quickstart.md",
        "docs/install-agent.md",
        "docs/operate.md",
        "docs/reference/protocol.md",
        "docs/reference/api-conventions.md",
        "docs/reference/judge-result.md",
        "docs/reference/config-keys.md",
        "docs/decisions/README.md",
    ):
        assert (REPO_ROOT / rel).is_file(), "少了 %s" % rel
