"""数据包：一个 zip，里面是测试点与（可选的）配置、checker。

约定（与 LemonLime 的习惯一致，能直接拿现成的数据目录打包）::

    1.in  1.ans          # 答案也可以叫 1.out
    2.in  2.ans
    ...
    config.json          # 可选：每点分值、子任务
    checker.cpp          # 可选：special judge（testlib 风格），连同 testlib.h

测试点可以放在任意一层子目录里，**名字**（去掉扩展名的相对路径）唯一即可。按"自然序"
排（``2`` 在 ``10`` 前面）。

``config.json``（全部可选）::

    {"cases": [{"name": "1", "score": 10}, ...],
     "subtasks": [{"score": 40, "cases": ["1", "2"]}, ...],
     "precheck": {"mode": "block", "disabled": ["file_access"]}}

* 没写分值：总分 100 按测试点平均分（除不尽的余数给最后几个点）。
* 子任务：里面**全部** AC 才得这个子任务的分（捆绑测试）；子任务里的点不再单独计分。
* ``precheck``：编译前源码预检的模式与关闭的规则（:mod:`.precheck`）；缺省按默认。

题型与 checker 相关的键（§12.13、§12.14，全部可选）::

    {"type": "interactive",                  # 必须与题目的题型一致（服务端校验）
     "checker": "checker.cpp", "checker_protocol": "testlib", "checker_points": "ratio",
     "testlib": "builtin",                   # builtin = 评测时注入随服务端带的 testlib.h；bundled = 用包内的
     "interactor": "interactor.cpp", "interactor_time_factor": 2, "interactor_memory_mb": 512,
     "grader": "grader.cpp", "grader_files": ["sum.h"],
     "answer_pattern": "{ident}{input_stem}.out", "answer_files": {"1": "a1.out"},
     "cases": [{"name": "1", "score": 10, "full_score": 10}]}

不认识的键只给 warning（``unknown_config_key``），不报错。

校验在**上传时**做（服务端）：结构不对当场报出来，而不是等到评测时才发现。
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence

from .precheck import PrecheckConfig

__all__ = [
    "PROBLEM_TYPES",
    "read_member",
    "builtin_testlib",
    "expand_answer_name",
    "TestdataError",
    "Case",
    "Subtask",
    "Testdata",
    "parse_testdata",
    "safe_member",
    "MAX_CASES",
]

#: 测试点数上限（防一个错包把评测机拖死）
MAX_CASES = 500
#: 解压后总大小上限
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
ANSWER_SUFFIXES = (".ans", ".out")
CHECKER_NAMES = ("checker.cpp", "spj.cpp", "chk.cpp")
INTERACTOR_NAMES = ("interactor.cpp", "interactor.cc", "interact.cpp")
GRADER_NAMES = {"cpp": ("grader.cpp", "grader.cc"), "c": ("grader.c",)}
CONFIG_NAME = "config.json"
TESTLIB_NAME = "testlib.h"

#: 题型（§12.13）：传统 / 函数交互（grader）/ 提交答案 / 交互（双向管道）
PROBLEM_TYPES = ("traditional", "grader", "answer_only", "interactive")
CHECKER_PROTOCOLS = ("testlib", "lemon", "exitcode")
CHECKER_POINTS = ("ratio", "percent", "auto")
TESTLIB_MODES = ("builtin", "bundled")
KNOWN_CONFIG_KEYS = frozenset({
    "cases", "subtasks", "precheck", "type", "checker", "checker_protocol", "checker_points", "testlib",
    "interactor", "interactor_time_factor", "interactor_memory_mb", "grader", "grader_files",
    "answer_pattern", "answer_files",
})
#: 出题人文件与答案文件的文件名（放进评测目录、选手机上回收）
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 评测机自己在工作目录里用的名字，出题人文件不能叫这些
RESERVED_NAMES = frozenset({
    "prog", "prog.exe", "chk", "chk.exe", "interactor", "interactor.exe", "main.cpp", "main.c",
    "user.o", "grader.o", "compile.out", "compile.err", "checker.err", "chk.out", "chk.err",
    "in.txt", "out.txt", "ans.txt", "tout.txt", "score.txt", "msg.txt", "inter.err", "input.txt", "output.txt",
})
DEFAULT_INTERACTOR_FACTOR = 2
MAX_INTERACTOR_FACTOR = 10
MAX_INTERACTOR_MEMORY_MB = 4096
MAX_ANSWER_NAMES_IN_SUMMARY = 500


class TestdataError(ValueError):
    """数据包结构不对。信息直接给教师看。"""


@dataclass
class Case:
    name: str
    input: str  # zip 内路径
    #: 答案在 zip 内的路径；交互题、有 checker 的提交答案题可以没有（按空文件）
    answer: Optional[str]
    score: int = 0
    #: Lemon 协议传给 spj 的满分（config 的 ``full_score``）；0 = 用 ``score``（为 0 时用 100）
    full_score: int = 0

    @property
    def input_stem(self) -> str:
        return PurePosixPath(self.input).name.rsplit(".", 1)[0]

    def lemon_full(self) -> int:
        return self.full_score or self.score or 100


def read_member(archive: zipfile.ZipFile, member: Optional[str]) -> bytes:
    """zip 里的一个成员；None（没有答案文件）按空内容。"""
    return archive.read(member) if member else b""


@dataclass
class Subtask:
    score: int
    cases: List[str]


@dataclass
class Testdata:
    cases: List[Case]
    subtasks: List[Subtask] = field(default_factory=list)
    checker: Optional[str] = None
    #: checker 同目录下的其它源文件（testlib.h 等），编译时一起解出来
    checker_support: List[str] = field(default_factory=list)
    #: 源码预检配置（config.json 的 ``precheck``）
    precheck: PrecheckConfig = field(default_factory=PrecheckConfig)
    #: 按哪个题型校验与评测；``declared_type`` 是 config.json 写的，``detected_type`` 是按文件名猜的
    problem_type: str = "traditional"
    declared_type: Optional[str] = None
    detected_type: Optional[str] = None
    checker_protocol: str = "testlib"
    checker_points: str = "ratio"
    testlib_mode: str = "builtin"
    #: 包里带的 testlib.h（checker 同目录）；builtin 模式下会被覆盖
    bundled_testlib: Optional[str] = None
    interactor: Optional[str] = None
    interactor_support: List[str] = field(default_factory=list)
    interactor_time_factor: int = DEFAULT_INTERACTOR_FACTOR
    interactor_memory_mb: Optional[int] = None
    #: 语言（cpp / c）→ grader 源文件
    grader: Dict[str, str] = field(default_factory=dict)
    #: grader 的附带文件：头文件放进选手编译目录，不含 main 的源文件一起编进 grader
    grader_files: List[str] = field(default_factory=list)
    #: 提交答案题：测试点名 → 答案文件名模板（``{ident}`` 在服务端按题目标识展开）
    answer_names: Dict[str, str] = field(default_factory=dict)
    warnings: List[Dict[str, str]] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        in_subtask = {name for sub in self.subtasks for name in sub.cases}
        return sum(sub.score for sub in self.subtasks) + sum(
            case.score for case in self.cases if case.name not in in_subtask
        )

    def answer_name(self, case: str, ident: str) -> str:
        return expand_answer_name(self.answer_names[case], ident)

    def summary(self) -> Dict[str, object]:
        document: Dict[str, object] = {
            "cases": len(self.cases),
            "subtasks": len(self.subtasks),
            "has_checker": self.checker is not None,
            "total_score": self.total_score,
        }
        if self.problem_type != "traditional":
            document["type"] = self.problem_type
        if self.declared_type:
            document["declared_type"] = self.declared_type
        if self.detected_type:
            document["detected_type"] = self.detected_type
        if self.checker is not None:
            document["checker_protocol"] = self.checker_protocol
            document["checker_points"] = self.checker_points
            document["testlib"] = self.testlib_mode
        if self.interactor:
            document["has_interactor"] = True
        if self.grader:
            document["grader"] = sorted(self.grader)
            document["grader_names"] = sorted({PurePosixPath(n).name for n in self.grader.values()})
            document["grader_files"] = [PurePosixPath(n).name for n in self.grader_files]
        if self.answer_names:
            names = dict(list(self.answer_names.items())[:MAX_ANSWER_NAMES_IN_SUMMARY])
            document["answer_names"] = names
            document["answer_exts"] = sorted({answer_ext(n) for n in self.answer_names.values()})
        if self.warnings:
            document["warnings"] = self.warnings[:50]
        return document


def expand_answer_name(template: str, ident: str) -> str:
    return template.replace("{ident}", ident)


def answer_ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[1].lower() if "." in name else ""


def detect_type(files) -> Optional[str]:
    """按文件名猜题型（只作提示）：有 interactor 是交互题，有 grader 是函数交互题。"""
    names = {PurePosixPath(n).name.lower() for n in files}
    if names & set(INTERACTOR_NAMES):
        return "interactive"
    if names & {n for group in GRADER_NAMES.values() for n in group}:
        return "grader"
    return None


def _natural_key(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def safe_member(name: str) -> bool:
    """zip 成员名是否安全：相对路径、不含 ``..``、不含反斜杠。导入器（problem_io）也用它。"""
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in name


#: 旧名，保留给已有调用方
_safe_member = safe_member


def parse_testdata(archive: zipfile.ZipFile, problem_type: Optional[str] = None) -> Testdata:
    """读出数据包结构；不对就抛 :class:`TestdataError`（信息给人看）。

    ``problem_type`` 是题目的题型（题目表里那一列，§12.13）；None = 按 config.json 的 ``type``，
    再没有就是传统题。config.json 写的题型与它不一致时不在这里报错（``declared_type`` 留给调用方比对）。
    """
    total = 0
    files: Dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        if not safe_member(info.filename):
            raise TestdataError("数据包里有不安全的路径：%s" % info.filename)
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise TestdataError("数据包解压后超过 2 GB")
        files[info.filename] = info

    config: Dict[str, object] = {}
    config_member = next((n for n in files if PurePosixPath(n).name == CONFIG_NAME), None)
    if config_member is not None:
        try:
            config = json.loads(archive.read(config_member).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TestdataError("config.json 不是合法 JSON：%s" % exc)
        if not isinstance(config, dict):
            raise TestdataError("config.json 必须是一个对象")
    warnings: List[Dict[str, str]] = []
    for key in sorted(config):
        if key not in KNOWN_CONFIG_KEYS:
            warnings.append({"code": "unknown_config_key", "message": "config.json 里不认识的键：%s" % key})

    declared = config.get("type")
    if declared is not None and declared not in PROBLEM_TYPES:
        raise TestdataError("config.json 的 type 只能是 %s" % " / ".join(PROBLEM_TYPES))
    detected = detect_type(files)
    kind = problem_type or declared or "traditional"
    if kind not in PROBLEM_TYPES:
        raise TestdataError("不认识的题型：%s" % kind)
    if declared is None and detected and detected != kind:
        warnings.append({"code": "detected_type", "message": "数据包里的文件像是 %s 题" % detected})

    checker = _named_member(files, config.get("checker"), CHECKER_NAMES, "checker")
    interactor = None
    if kind == "interactive":
        interactor = _named_member(files, config.get("interactor"), INTERACTOR_NAMES, "interactor")
        if interactor is None:
            raise TestdataError("交互题的数据包里要有 interactor.cpp（或在 config.json 的 interactor 里指定）")
    grader: Dict[str, str] = {}
    grader_files: List[str] = []
    if kind == "grader":
        grader = _grader_members(files, config.get("grader"))
        grader_files = _grader_files(files, config.get("grader_files"), grader)

    inputs: Dict[str, str] = {}
    answers: Dict[str, str] = {}
    for name in files:
        lower = name.lower()
        stem = name.rsplit(".", 1)[0]
        if lower.endswith(".in"):
            inputs[stem] = name
        elif lower.endswith(ANSWER_SUFFIXES):
            # .ans 优先于 .out（有的数据目录两个都有，.out 是参考程序跑出来的）
            if stem not in answers or lower.endswith(".ans"):
                answers[stem] = name

    names = sorted(inputs, key=_natural_key)
    # 交互题的答案给 interactor 用、可以没有；有 checker 的提交答案题与 exitcode 协议（QDUOJ 的两参数 spj
    # 不读答案）也可以没有，按空文件
    answers_optional = kind == "interactive" or (checker is not None and (
        kind == "answer_only" or config.get("checker_protocol") == "exitcode"))
    missing = [name for name in names if name not in answers]
    if missing and not answers_optional:
        raise TestdataError("这些输入没有对应的答案（.ans / .out）：%s" % ", ".join(missing[:10]))
    if not names:
        raise TestdataError("数据包里没有测试点（要有成对的 X.in 与 X.ans）")
    if len(names) > MAX_CASES:
        raise TestdataError("测试点太多（%d > %d）" % (len(names), MAX_CASES))

    # 测试点名：去掉扩展名的相对路径；只有一层目录时去掉那层公共前缀，配置里好写
    prefixes = {n.rsplit("/", 1)[0] if "/" in n else "" for n in names}
    strip = prefixes.pop() + "/" if len(prefixes) == 1 and "" not in prefixes else ""
    cases = [
        Case(name=n[len(strip):] if strip else n, input=inputs[n], answer=answers.get(n)) for n in names
    ]

    _apply_scores(cases, config)
    subtasks = _parse_subtasks(cases, config)
    try:
        precheck = PrecheckConfig.from_json(config.get("precheck"))
    except ValueError as exc:
        raise TestdataError("config.json 的 %s" % exc)

    protocol = _choice(config, "checker_protocol", CHECKER_PROTOCOLS)
    points = _choice(config, "checker_points", CHECKER_POINTS)
    testlib_mode = _choice(config, "testlib", TESTLIB_MODES)
    support: List[str] = []
    bundled = None
    if checker is not None:
        support = _headers_beside(files, checker)
        bundled = next((n for n in support if PurePosixPath(n).name == TESTLIB_NAME), None)
    interactor_support = _headers_beside(files, interactor) if interactor else []
    for member in (bundled, next((n for n in interactor_support if PurePosixPath(n).name == TESTLIB_NAME), None)):
        if member and testlib_mode == "builtin" and archive.read(member) != builtin_testlib():
            warnings.append({"code": "testlib_overridden",
                             "message": "包里的 testlib.h 与内置版本不同，评测时用内置版本"})
            break

    factor = config.get("interactor_time_factor", DEFAULT_INTERACTOR_FACTOR)
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not 1 <= factor <= MAX_INTERACTOR_FACTOR:
        raise TestdataError("config.json 的 interactor_time_factor 要是 1..%d 的数" % MAX_INTERACTOR_FACTOR)
    memory = config.get("interactor_memory_mb")
    if memory is not None and (isinstance(memory, bool) or not isinstance(memory, int)
                               or not 16 <= memory <= MAX_INTERACTOR_MEMORY_MB):
        raise TestdataError("config.json 的 interactor_memory_mb 要是 16..%d 的整数" % MAX_INTERACTOR_MEMORY_MB)

    answer_names = _answer_names(cases, config) if kind == "answer_only" else {}
    return Testdata(
        cases=cases, subtasks=subtasks, checker=checker, checker_support=support, precheck=precheck,
        problem_type=kind, declared_type=declared, detected_type=detected,
        checker_protocol=protocol, checker_points=points, testlib_mode=testlib_mode, bundled_testlib=bundled,
        interactor=interactor, interactor_support=interactor_support,
        interactor_time_factor=int(factor), interactor_memory_mb=memory,
        grader=grader, grader_files=grader_files, answer_names=answer_names, warnings=warnings,
    )


def builtin_testlib() -> bytes:
    """随服务端带的 testlib.h（``judging/vendor/``）。"""
    return (Path(__file__).parent / "vendor" / TESTLIB_NAME).read_bytes()


def _choice(config: Dict[str, object], key: str, options: Sequence[str]) -> str:
    value = config.get(key, options[0])
    if value not in options:
        raise TestdataError("config.json 的 %s 只能是 %s" % (key, " / ".join(options)))
    return str(value)


def _check_name(name: str, what: str) -> None:
    if not NAME_RE.match(name) or name.lower() in RESERVED_NAMES:
        raise TestdataError("%s 的文件名 %s 不行：只能用字母、数字、. _ -，且不能用评测机自己要用的名字" % (what, name))


def _find(files, wanted: str, what: str) -> str:
    """config.json 里写的文件：zip 内路径，或者唯一的文件名。"""
    if wanted in files:
        return wanted
    hits = [n for n in files if PurePosixPath(n).name == wanted]
    if len(hits) != 1:
        raise TestdataError("config.json 的 %s 指定的 %s %s" % (what, wanted, "不在数据包里" if not hits else "有多个"))
    return hits[0]


def _named_member(files, configured: object, defaults: Sequence[str], what: str) -> Optional[str]:
    if configured is not None:
        if not isinstance(configured, str) or not configured:
            raise TestdataError("config.json 的 %s 要是文件名" % what)
        member = _find(files, configured, what)
    else:
        member = next((n for n in sorted(files) if PurePosixPath(n).name.lower() in defaults), None)
    if member is not None:
        _check_name(PurePosixPath(member).name, what)
    return member


def _headers_beside(files, member: str) -> List[str]:
    folder = str(PurePosixPath(member).parent)
    return sorted(
        n for n in files
        if n != member and str(PurePosixPath(n).parent) == folder and n.lower().endswith((".h", ".hpp"))
    )


def _grader_members(files, configured: object) -> Dict[str, str]:
    grader: Dict[str, str] = {}
    if configured is None:
        for language, names in GRADER_NAMES.items():
            hit = next((n for n in sorted(files) if PurePosixPath(n).name.lower() in names), None)
            if hit:
                grader[language] = hit
    elif isinstance(configured, str):
        language = "c" if configured.lower().endswith(".c") else "cpp"
        grader[language] = _find(files, configured, "grader")
    elif isinstance(configured, dict):
        for language, name in configured.items():
            if language not in GRADER_NAMES or not isinstance(name, str):
                raise TestdataError("config.json 的 grader 只能写 {\"cpp\": ..., \"c\": ...}")
            grader[language] = _find(files, name, "grader")
    else:
        raise TestdataError("config.json 的 grader 要是文件名或 {\"cpp\": ..., \"c\": ...}")
    if not grader:
        raise TestdataError("函数交互题的数据包里要有 grader.cpp（或在 config.json 的 grader 里指定）")
    for member in grader.values():
        _check_name(PurePosixPath(member).name, "grader")
    return grader


def _grader_files(files, configured: object, grader: Dict[str, str]) -> List[str]:
    if configured is None:
        folders = {str(PurePosixPath(m).parent) for m in grader.values()}
        members = sorted(
            n for n in files
            if str(PurePosixPath(n).parent) in folders and n.lower().endswith((".h", ".hpp"))
            and PurePosixPath(n).name != TESTLIB_NAME
        )
    else:
        if not isinstance(configured, list) or not all(isinstance(n, str) for n in configured):
            raise TestdataError("config.json 的 grader_files 要是文件名数组")
        members = [_find(files, n, "grader_files") for n in configured]
        bad = [n for n in members if not n.lower().endswith((".h", ".hpp", ".cpp", ".cc", ".c"))]
        if bad:
            raise TestdataError("grader_files 只能是头文件或源文件：%s" % ", ".join(bad[:5]))
    seen = set()
    for member in members:
        name = PurePosixPath(member).name
        _check_name(name, "grader_files")
        if name in seen or member in grader.values():
            raise TestdataError("grader_files 里有重名文件：%s" % name)
        seen.add(name)
    return members


def _render(template: str, case: Case, index: int) -> str:
    return (template.replace("{input_stem}", case.input_stem).replace("{case}", case.name)
            .replace("{index}", str(index)))


def _answer_names(cases: Sequence[Case], config: Dict[str, object]) -> Dict[str, str]:
    """提交答案题每个测试点的答案文件名（模板里 ``{ident}`` 保留，服务端展开）。"""
    pattern = config.get("answer_pattern")
    if pattern is None:
        digits = all(case.input_stem.isdigit() for case in cases)
        pattern = "{ident}{input_stem}.out" if digits else "{input_stem}.out"
    if not isinstance(pattern, str) or not pattern:
        raise TestdataError("config.json 的 answer_pattern 要是字符串")
    explicit = config.get("answer_files") or {}
    if not isinstance(explicit, dict) or not all(isinstance(v, str) for v in explicit.values()):
        raise TestdataError("config.json 的 answer_files 要是 {测试点: 文件名}")
    known = {case.name for case in cases}
    unknown = [k for k in explicit if k not in known]
    if unknown:
        raise TestdataError("answer_files 里有不存在的测试点：%s" % ", ".join(unknown[:5]))
    names: Dict[str, str] = {}
    for index, case in enumerate(cases, 1):
        names[case.name] = str(explicit.get(case.name) or _render(pattern, case, index))
    seen = set()
    for template in names.values():
        sample = expand_answer_name(template, "p")
        if "{" in sample or "}" in sample:
            raise TestdataError("答案文件名模板里有不认识的占位符：%s" % template)
        _check_name(sample, "答案文件")
        if not answer_ext(sample) or len(answer_ext(sample)) > 10:
            raise TestdataError("答案文件名要带扩展名（选手机按扩展名回收）：%s" % sample)
        if sample in seen:
            raise TestdataError("两个测试点的答案文件同名：%s" % sample)
        seen.add(sample)
    return names


def _apply_scores(cases: Sequence[Case], config: Dict[str, object]) -> None:
    by_name = {case.name: case for case in cases}
    configured = config.get("cases")
    if configured is not None:
        if not isinstance(configured, list):
            raise TestdataError("config.json 的 cases 必须是数组")
        for item in configured:
            if not isinstance(item, dict) or str(item.get("name")) not in by_name:
                raise TestdataError("config.json 的 cases 里有不存在的测试点：%r" % (item,))
            score = item.get("score")
            if not isinstance(score, int) or score < 0:
                raise TestdataError("测试点 %s 的分值必须是非负整数" % item.get("name"))
            by_name[str(item["name"])].score = score
            full = item.get("full_score")
            if full is not None:
                if isinstance(full, bool) or not isinstance(full, int) or full <= 0:
                    raise TestdataError("测试点 %s 的 full_score 必须是正整数" % item.get("name"))
                by_name[str(item["name"])].full_score = full
        return
    if config.get("subtasks"):
        return  # 只写了子任务：不在子任务里的点得 0 分，由子任务决定总分
    base, remainder = divmod(100, len(cases))
    for index, case in enumerate(cases):
        case.score = base + (1 if index >= len(cases) - remainder else 0)


def _parse_subtasks(cases: Sequence[Case], config: Dict[str, object]) -> List[Subtask]:
    raw = config.get("subtasks")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TestdataError("config.json 的 subtasks 必须是数组")
    names = {case.name for case in cases}
    seen = set()
    subtasks = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise TestdataError("第 %d 个子任务不是对象" % index)
        score = item.get("score")
        members = item.get("cases")
        if not isinstance(score, int) or score < 0:
            raise TestdataError("第 %d 个子任务的分值必须是非负整数" % index)
        if not isinstance(members, list) or not members:
            raise TestdataError("第 %d 个子任务没有测试点" % index)
        members = [str(m) for m in members]
        unknown = [m for m in members if m not in names]
        if unknown:
            raise TestdataError("第 %d 个子任务里有不存在的测试点：%s" % (index, ", ".join(unknown)))
        dup = [m for m in members if m in seen]
        if dup:
            raise TestdataError("测试点 %s 出现在多个子任务里" % ", ".join(dup))
        seen.update(members)
        subtasks.append(Subtask(score=score, cases=members))
    return subtasks
