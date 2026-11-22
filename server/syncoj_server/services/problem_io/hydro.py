"""Hydro 格式（格式 id ``hydro``），兼带 Hydro 旧 ``config.ini`` 与 SYZOJ ``data.yml``。

包结构（Hydro ``ProblemModel.import`` / ``export``）::

    [<dir>/]problem.yaml          # pid、title、tag……；可选 ICPC 兼容的 limits.time_limit(s)/memory(MB)
    [<dir>/]testdata/config.yaml  # 评测配置（也认 config.yml、旧 config.ini）；子目录只展开一层
    [<dir>/]testdata/*.in / *.out / checker.cpp ...

根目录直接有 ``problem.yaml``（或 ``testdata/``、``data.yml``）时整包是一道题，否则根下每个
目录一道题，什么都没有的目录静默跳过。SYZOJ 的数据目录是平铺的，``data.yml`` 与数据文件同级。

导入语义对照 Hydro 源码（``packages/common/subtask.ts``、``cases.ts``、``utils/lib/common.ts``）：

* ``time``：数字与整数字符串按 ms；带单位（``s`` / ``ms`` / ``us``）换算后向下取整
  （``'500us'`` → 0，再由框架夹到 100）。``memory``：带 ``k`` / ``m`` / ``g`` 换算后向上取整，
  数字按 MB。缺省 1000 ms / 256 MB。``problem.yaml`` 的 ``limits`` 与 Hydro 导入一样覆盖 config。
* 子任务按 ``normalizeSubtasks``：没写分的子任务平分 ``100 - 已写分值之和``（余数给后面几个）；
  ``sum`` 子任务里没写分的点平分 ``子任务分 - 已写的点分``；其余类型点分缺省等于子任务分。
  ``min`` → 捆绑（单点就是普通测试点）、``max`` → 捆绑 + ``max_as_bundle``、``sum`` → 拆成独立点。
  平铺的 ``cases`` 等价于一个 ``sum`` 子任务。没有配置或配置里没有测试点时按文件名自动识别
  （``readSubtasksFromFiles`` 的规则：``X.in`` 配 ``X.out`` / ``X.ans``，``N-M.in`` 归入 min 子任务 N）。
* ``config.ini``：首行测试点数，之后每行 ``in|out|time(s)|score|memory(KB)``，每行一个单点子任务。
* SYZOJ ``data.yml``：``inputFile`` / ``outputFile`` 模板只替换第一个 ``#``；``min`` / ``mul`` 捆绑
  （``mul`` 给 ``mul_as_bundle``），``sum`` 拆成独立点；时限内存不在文件里（``limits_defaulted``）。

导出：``<ident>/problem.yaml``（pid 不满足 Hydro 规则时省略）+ ``<ident>/testdata/config.yaml`` 与
扁平化的数据文件。子任务写成 ``type: min``，散点各自写成单点 min 子任务；testlib.h 不写
（Hydro 评测机总是用自带的那份）。
"""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ...judging.testdata import MAX_CASES
from . import yaml_lite
from .archive import ArchiveView
from .build import flat_case_names
from .bundle import (
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_TIME_LIMIT_MS,
    BundleCase,
    BundleSubtask,
    ExportProblem,
    FileRef,
    ImportResult,
    Notice,
    ProblemBundle,
)
from .checkers import TESTLIB_NAME, checker_language_ok, real_checker
from .exporting import OpenBlob, open_testdata
from .notices import ProblemIoError, notice

__all__ = ["LABEL", "parse", "export", "parse_time_ms", "parse_memory_mb", "valid_pid"]

LABEL = "Hydro"

PROBLEM_YAML = "problem.yaml"
TESTDATA_DIR = "testdata"
CONFIG_NAMES = ("config.yaml", "config.yml")
INI_NAME = "config.ini"
SYZOJ_NAME = "data.yml"

#: Hydro 默认评测机的测试点上限（``testcases_max``）
HYDRO_CASE_LIMIT = 100

#: 名字里不带点的 checker 是 Hydro 内置的 testlib checker；这几个与默认比较等价或更严
_APPROX_CHECKERS = frozenset({"wcmp", "ncmp", "lcmp", "fcmp", "hcmp"})
_REAL_CHECKERS = {"rcmp": 1e-6, "rcmp4": 1e-4, "rcmp6": 1e-6, "rcmp9": 1e-9}

_PROTOCOL_CHECKERS = frozenset({"hustoj", "lemon", "qduoj", "syzoj", "kattis"})
_TYPE_ERRORS = {
    "interactive": "interactive_unsupported",
    "communication": "communication_unsupported",
    "submit_answer": "answers_only_unsupported",
}

_PID_RE = re.compile(r"^[a-z][0-9a-z]*$", re.I)
_TIME_RE = re.compile(r"^([0-9]+(?:\.[0-9]*)?)([mu]?)s?$", re.I)
_MEMORY_RE = re.compile(r"^([0-9]+(?:\.[0-9]*)?)([kmg])b?$", re.I)
_TIME_UNITS = {"": 1000.0, "m": 1.0, "u": 0.001}
_MEMORY_UNITS = {"k": 1.0 / 1024, "m": 1.0, "g": 1024.0}
_SAFE_INT = re.compile(r"^\s*[-+]?\d+\s*$")
#: Hydro 语言 id → C++ 标准：``cc17``、``cc.cc17o2``、``cc.cc14``
_LANG_STD = re.compile(r"^cc(?:\.cc)?(98|03|11|14|17|20|23|26)", re.I)
_STD_ORDER = ("98", "03", "11", "14", "17", "20", "23", "26")


# --------------------------------------------------------------------------- #
# 单位
# --------------------------------------------------------------------------- #


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_time_ms(value: Any) -> int:
    """Hydro ``parseTimeMS``：数字 / 整数字符串按 ms；``1s``、``1000ms``、``500us`` 换算后向下取整。

    不能解析抛 ``ValueError``。"""
    if _is_number(value):
        return int(math.floor(value))
    if isinstance(value, str):
        if _SAFE_INT.match(value):
            return int(value)
        match = _TIME_RE.match(value.strip())
        if match:
            return int(math.floor(float(match.group(1)) * _TIME_UNITS[match.group(2).lower()]))
    raise ValueError("时间 %r 无法解析" % (value,))


def parse_memory_mb(value: Any) -> int:
    """Hydro ``parseMemoryMB``：数字 / 整数字符串按 MB；``k`` / ``m`` / ``g`` 换算后向上取整。"""
    if _is_number(value):
        return int(math.ceil(value))
    if isinstance(value, str):
        if _SAFE_INT.match(value):
            return int(value)
        match = _MEMORY_RE.match(value.strip())
        if match:
            return int(math.ceil(float(match.group(1)) * _MEMORY_UNITS[match.group(2).lower()]))
    raise ValueError("内存 %r 无法解析" % (value,))


def valid_pid(text: str) -> bool:
    return bool(_PID_RE.match(text))


def _get_score(total: int, count: int) -> Iterator[int]:
    """Hydro ``getScore``：``total`` 平分成 ``count`` 份，余数给后面几个。"""
    if count <= 0:
        return
    base = total // count
    extra = count - total % count
    for index in range(count):
        yield base + 1 if index >= extra else base


def _natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


# --------------------------------------------------------------------------- #
# 导入：中间表示
# --------------------------------------------------------------------------- #


class _ConfigError(Exception):
    """单题级的配置问题：带一条错误 notice，记进 ``bundle.errors``。"""

    def __init__(self, item: Notice) -> None:
        super().__init__(item.message)
        self.notice = item


@dataclass
class _RawCase:
    input: Optional[str]
    output: Optional[str]
    score: Any = None
    time: Any = None
    memory: Any = None
    #: 自动识别时的排序号（Hydro 按它排）
    order: Optional[int] = None


@dataclass
class _RawSubtask:
    cases: List[_RawCase]
    type: Optional[str] = None
    score: Any = None
    id: Any = None
    time: Any = None
    memory: Any = None
    depends: List[Any] = field(default_factory=list)


class _Problem:
    """一道题的目录：``base``（视图相对，"" 或 "dir/"）与数据目录 ``data``。"""

    def __init__(self, view: ArchiveView, base: str) -> None:
        self.view = view
        self.base = base
        testdata = base + TESTDATA_DIR
        self.data = testdata + "/" if view.is_dir(testdata) else base
        # 平铺文件名 → 视图相对路径：顶层优先，子目录只展开一层（Hydro 导入的做法）
        self.flat: Dict[str, str] = {}
        top = view.listdir(self.data.rstrip("/"))
        for name in top:
            self.flat.setdefault(name, self.data + name)
        for rel in view.walk(self.data.rstrip("/")):
            parts = rel.split("/")
            if len(parts) == 2:
                self.flat.setdefault(parts[1], self.data + rel)
        #: 自动识别用的文件表（Hydro ``collectFiles``：顶层 + input/ + output/）
        self.files = list(top) + [
            rel for rel in view.walk(self.data.rstrip("/"))
            if rel.count("/") == 1 and rel.split("/", 1)[0] in ("input", "output")
        ]

    def path(self, name: str) -> str:
        return self.data + name

    def resolve(self, name: Any) -> Optional[str]:
        """配置里的文件名 → 视图相对路径；找不到返回 None。"""
        if not isinstance(name, (str, int)) or isinstance(name, bool):
            return None
        text = str(name).replace("\\", "/").strip()
        if not text or text.startswith("/") or ".." in text.split("/"):
            return None
        if self.view.exists(self.data + text):
            return self.data + text
        return self.flat.get(text.rsplit("/", 1)[-1])

    def find(self, *names: str) -> Optional[str]:
        """数据目录（或题目目录）里第一个存在的文件名（大小写不敏感）。"""
        for folder in (self.data, self.base):
            listing = {n.lower(): n for n in self.view.listdir(folder.rstrip("/"))}
            for name in names:
                if name in listing:
                    return folder + listing[name]
        return None


def _problem_dirs(view: ArchiveView) -> List[str]:
    def is_problem(base: str) -> bool:
        return (
            view.exists(base + PROBLEM_YAML)
            or view.is_dir(base + TESTDATA_DIR)
            or view.exists(base + SYZOJ_NAME)
            or any(view.exists(base + name) for name in CONFIG_NAMES + (INI_NAME,))
        )

    if is_problem(""):
        return [""]
    return [d + "/" for d in sorted(view.root_dirs(), key=_natural_key) if is_problem(d + "/")]


# --------------------------------------------------------------------------- #
# 导入：读配置
# --------------------------------------------------------------------------- #


def _load_yaml(view: ArchiveView, rel: str) -> Dict[str, Any]:
    """读一个 YAML 配置；空文档当 ``{}``，顶层不是映射 → ``bad_manifest``。"""
    try:
        doc = yaml_lite.load(view.read(rel))
    except yaml_lite.YamlError as exc:
        raise _ConfigError(notice(exc.code, path=view.full(rel), line=exc.line, reason=exc.reason))
    except ProblemIoError as exc:
        raise _ConfigError(exc.notice)
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise _bad(view, rel, "不是映射")
    return doc


def _bad(view: ArchiveView, rel: str, reason: str) -> _ConfigError:
    return _ConfigError(notice("bad_manifest", path=view.full(rel), reason=reason))


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _raw_case(item: Any) -> _RawCase:
    if not isinstance(item, dict):
        return _RawCase(input=None, output=None)
    return _RawCase(
        input=item.get("input"), output=item.get("output"), score=item.get("score"),
        time=item.get("time"), memory=item.get("memory"),
    )


def _yaml_subtasks(config: Dict[str, Any]) -> List[_RawSubtask]:
    """``readYamlCases``：``cases`` 非空时覆盖 ``subtasks``，当成一个 sum 子任务。"""
    cases = config.get("cases")
    if isinstance(cases, list) and cases:
        return [_RawSubtask(cases=[_raw_case(item) for item in cases], type="sum")]
    out = []
    for item in _as_list(config.get("subtasks")):
        if not isinstance(item, dict):
            continue
        out.append(_RawSubtask(
            cases=[_raw_case(c) for c in _as_list(item.get("cases"))],
            type=item.get("type"), score=item.get("score"), id=item.get("id"),
            time=item.get("time"), memory=item.get("memory"),
            depends=_as_list(item.get("if")),
        ))
    return out


def _ini_subtasks(view: ArchiveView, rel: str) -> List[_RawSubtask]:
    """Hydro ``convertIniConfig``。"""
    try:
        text = view.read(rel).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _bad(view, rel, "不是 UTF-8")
    except ProblemIoError as exc:
        raise _ConfigError(exc.notice)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        count = int(lines[0].strip())
    except ValueError:
        raise _bad(view, rel, "第 1 行不是测试点数")
    out = []
    for index in range(1, count + 1):
        line = lines[index] if index < len(lines) else ""
        if not line.strip():
            raise _bad(view, rel, "测试点数与行数不符")
        parts = [part.strip() for part in line.split("|")] + [""] * 5
        try:
            score: Any = int(parts[3])
        except ValueError:
            raise _bad(view, rel, "第 %d 行分值不对" % (index + 1))
        memory = "256m"
        if re.match(r"^\d+", parts[4]):
            memory = "%dm" % (int(re.match(r"^\d+", parts[4]).group(0)) // 1024)  # type: ignore[union-attr]
        out.append(_RawSubtask(
            cases=[_RawCase(input=parts[0], output=parts[1])],
            score=score, time="%ss" % parts[2], memory=memory,
        ))
    return out


# ---- 按文件名自动识别（readSubtasksFromFiles）----

_IO_EXTS = ["out", "ans"]


def _rule1_outputs(match: "re.Match") -> List[str]:
    stem = match.group(1)
    exts = [e for i in _IO_EXTS for e in (i, i.upper(), i + ".txt", i.upper() + ".TXT")]
    out = []
    for ext in exts:
        name = "%s.%s" % (stem, ext)
        out.append(name)
        out.append(name.replace("input", "output").replace("INPUT", "OUTPUT"))
    if "input" in stem:
        out.append((stem + ".txt").replace("input", "output"))
    return out


def _rule2_outputs(match: "re.Match") -> List[str]:
    prefix = "ou" if match.group(2) == "in" else "OU"
    names = ["%s.%s%s" % (match.group(1), prefix, match.group(3)),
             "%s.%s%s" % (match.group(1), prefix + ("t" if prefix == "ou" else "T"), match.group(3))]
    return [x for n in names for x in (n, n.replace("input", "output").replace("INPUT", "OUTPUT"))]


class _Groups:
    """只提供 ``group(i)`` 的匹配结果（:func:`_rule1_match` 用）。"""

    def __init__(self, *groups: Optional[str]) -> None:
        self._groups = (None,) + groups

    def group(self, index: int) -> Optional[str]:
        return self._groups[index]


_RULE1_EXTS = ("in.txt", "IN.TXT", "in", "IN", "txt", "TXT")
_RULE1_STEM = re.compile(r"[\w.-]*[0-9]", re.A)


def _rule1_match(name: str) -> Optional[_Groups]:
    """等价于 Hydro 的 ``^(([\\w.-]*?)(?:(\\d*)[-_])?(\\d+))\\.(in|IN|txt|TXT|in\\.txt|IN\\.TXT)$``，
    但线性时间：那条正则对"长数字串 + 不匹配的结尾"会二次方回溯。

    stem 必须以数字结尾，所以扩展名最多一种取法；最短前缀落在"末尾数字串前若有 ``-``/``_``，
    就从分隔符前那段数字的开头起"。"""
    for ext in _RULE1_EXTS:
        if not name.endswith("." + ext):
            continue
        stem = name[: -len(ext) - 1]
        if not _RULE1_STEM.fullmatch(stem):
            continue
        r = len(stem)
        while r > 0 and stem[r - 1] in "0123456789":
            r -= 1
        if r > 0 and stem[r - 1] in "-_":
            q = r - 1
            while q > 0 and stem[q - 1] in "0123456789":
                q -= 1
            return _Groups(stem, stem[:q], stem[q:r - 1], stem[r:], ext)
        return _Groups(stem, stem[:r], None, stem[r:], ext)
    return None


class _Rule1:
    match = staticmethod(_rule1_match)


_MATCHERS = [
    # (正则, 输出候选, 序号, 子任务号, 计分方式)
    (_Rule1,
     _rule1_outputs, lambda m: int(m.group(4)), lambda m: int(m.group(3) or 1),
     lambda m: "min" if m.group(3) else "sum"),
    (re.compile(r"^(\D*)\.(in|IN)(\d+)$", re.A),
     _rule2_outputs, lambda m: int(m.group(3)), lambda m: 1, lambda m: "sum"),
    (re.compile(r"^(\D*)([0-9]+)([-_])([0-9]+)\.(in|IN)$", re.A),
     lambda m: ["%s%s%s%s.%s" % (m.group(1), m.group(2), m.group(3), m.group(4), e)
                for e in ("out", "ans", "OUT", "ANS")],
     lambda m: int(m.group(4)), lambda m: int(m.group(2)), lambda m: "min"),
    (re.compile(r"^(([0-9]+)[-_].*)\.(in|IN)$", re.A),
     lambda m: ["%s.%s" % (m.group(1), e) for e in ("out", "ans", "OUT", "ANS")],
     lambda m: int(m.group(2)), lambda m: 1, lambda m: "sum"),
]


def _auto_subtasks(files: List[str], time: Any, memory: Any) -> List[_RawSubtask]:
    present = set(files)
    subtasks: Dict[int, _RawSubtask] = {}
    for name in files:
        for regex, outputs, case_id, sub_id, scorer in _MATCHERS:
            match = regex.match(name)
            if not match:
                continue
            found = next((o for o in outputs(match) if o and o != name and o in present), None)
            if found is None:
                continue
            sid = sub_id(match)
            case = _RawCase(input=name, output=found, order=case_id(match))
            sub = subtasks.get(sid)
            if sub is None:
                subtasks[sid] = _RawSubtask(cases=[case], type=scorer(match), id=sid, time=time, memory=memory)
            elif not any(c.input == name and c.output == found for c in sub.cases):
                sub.cases.append(case)
            break
    for sub in subtasks.values():
        sub.cases.sort(key=lambda c: (c.order or 0, _natural_key(str(c.input))))
    return list(subtasks.values())


# --------------------------------------------------------------------------- #
# 导入：规整成 bundle
# --------------------------------------------------------------------------- #


def _int_score(bundle: ProblemBundle, value: Any) -> int:
    """Hydro 里 ``score || 缺省``：0 / 缺失 / 非数字都当没写。小数向下取整。"""
    if not _is_number(value) or value <= 0:
        return 0
    if value != int(value):
        bundle.warn(notice("score_truncated"))
    return int(math.floor(value))


@dataclass
class _Normalized:
    """规整后的结果：测试点、捆绑子任务、所有点的时限内存。"""

    cases: List[BundleCase] = field(default_factory=list)
    subtasks: List[BundleSubtask] = field(default_factory=list)
    limits: List[Tuple[int, int]] = field(default_factory=list)


def _normalize(
    bundle: ProblemBundle,
    problem: _Problem,
    subtasks: List[_RawSubtask],
    time: Any,
    memory: Any,
    bundle_types: Dict[str, Optional[str]],
) -> _Normalized:
    """``normalizeSubtasks`` + 映射到 SyncOJ。``bundle_types``：子任务类型 → 降级为捆绑时的警告代码
    （None = 不警告）；不在表里的类型拆成独立测试点。"""
    view = problem.view
    if all(isinstance(s.id, int) and not isinstance(s.id, bool) for s in subtasks):
        subtasks = sorted(subtasks, key=lambda s: s.id)
    scores = [_int_score(bundle, s.score) for s in subtasks]
    fill = _get_score(max(100 - sum(scores), 0), sum(1 for s in scores if not s))
    out = _Normalized()
    used: Dict[str, int] = {}

    def case_name(raw: _RawCase) -> str:
        base = str(raw.input or "case").replace("\\", "/").rsplit("/", 1)[-1]
        base = base.rsplit(".", 1)[0] if "." in base else base
        base = base or "case"
        serial = used.get(base.lower(), 0) + 1
        used[base.lower()] = serial
        return base if serial == 1 else "%s-%d" % (base, serial)

    for number, (sub, explicit) in enumerate(zip(subtasks, scores), 1):
        if not sub.cases:
            continue
        kind = sub.type if isinstance(sub.type, str) and sub.type else "min"
        score = explicit or next(fill, 0)
        case_scores = [_int_score(bundle, c.score) for c in sub.cases]
        case_fill = _get_score(max(score - sum(case_scores), 0), sum(1 for s in case_scores if not s))
        if any(item for item in sub.depends):
            bundle.warn(notice("dependency_dropped"))
        made: List[BundleCase] = []
        for raw, case_score in zip(sub.cases, case_scores):
            effective = case_score or (next(case_fill, 0) if kind == "sum" else score)
            try:
                limit = (
                    parse_time_ms(raw.time or sub.time or time),
                    parse_memory_mb(raw.memory or sub.memory or memory),
                )
            except ValueError as exc:
                raise _ConfigError(notice("bad_manifest", path=view.full(problem.data), reason=str(exc)))
            out.limits.append(limit)
            name = case_name(raw)
            if raw.input:
                found = problem.resolve(raw.input)
                if found is None:
                    raise _ConfigError(notice("missing_file", path=view.full(problem.path(str(raw.input)))))
                input_ref = FileRef(member=view.full(found))
            else:
                input_ref = FileRef(data=b"")  # Hydro：没写 input 就是 /dev/null
            if not raw.output:
                raise _ConfigError(notice("missing_answer", name=name))
            answer = problem.resolve(raw.output)
            if answer is None:
                raise _ConfigError(notice("missing_file", path=view.full(problem.path(str(raw.output)))))
            made.append(BundleCase(name=name, input=input_ref, answer=FileRef(member=view.full(answer)),
                                   score=effective))
        if kind in bundle_types and len(made) > 1:
            warning = bundle_types[kind]
            if warning:
                bundle.warn(notice(warning))
            lowest = min(case.score for case in made)
            if any(case.score != score for case in made):
                bundle.warn(notice("subtask_scores_uneven", group=sub.id if sub.id is not None else number,
                                   score=lowest))
            for case in made:
                case.score = 0
            out.subtasks.append(BundleSubtask(score=lowest, cases=[case.name for case in made]))
        out.cases.extend(made)
    return out


def _apply_limits(bundle: ProblemBundle, normalized: _Normalized, time: Any, memory: Any, path: str) -> None:
    try:
        default = (parse_time_ms(time), parse_memory_mb(memory))
    except ValueError as exc:
        raise _ConfigError(notice("bad_manifest", path=path, reason=str(exc)))
    limits = normalized.limits or [default]
    bundle.time_limit_ms = max(item[0] for item in limits)
    bundle.memory_limit_mb = max(item[1] for item in limits)
    if len(set(limits)) > 1:
        bundle.warn(notice("limits_collapsed", time=bundle.time_limit_ms, memory=bundle.memory_limit_mb))


def _checker(bundle: ProblemBundle, problem: _Problem, config: Dict[str, Any], path: str) -> None:
    kind = config.get("checker_type") or "default"
    if kind == "default":
        return
    if kind == "strict":
        bundle.warn(notice("strict_compare_relaxed"))
        return
    if kind != "testlib":
        raise _ConfigError(notice("checker_protocol_unsupported", checker=kind))
    spec = config.get("checker")
    name = spec.get("file") if isinstance(spec, dict) else spec
    if not isinstance(name, str) or not name.strip():
        raise _ConfigError(notice("bad_manifest", path=path, reason="缺少 checker"))
    name = name.strip()
    if "." not in name:
        if name in _REAL_CHECKERS:
            eps = _REAL_CHECKERS[name]
            bundle.checker = FileRef(data=real_checker(eps))
            bundle.warn(notice("real_checker_generated", eps="%g" % eps))
        elif name in _APPROX_CHECKERS:
            bundle.warn(notice("builtin_checker_approx", name=name))
        else:
            raise _ConfigError(notice("checker_unknown", name=name))
        return
    if not checker_language_ok(name):
        raise _ConfigError(notice("checker_language", language=name.rsplit(".", 1)[-1]))
    found = problem.resolve(name)
    if found is None:
        raise _ConfigError(notice("checker_missing_source", path=problem.view.full(problem.path(name))))
    bundle.checker = FileRef(member=problem.view.full(found))
    dropped = []
    for extra in _as_list(config.get("judge_extra_files")):
        text = str(extra)
        base = text.replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower() == TESTLIB_NAME:
            continue  # Hydro 评测机总是用自带的 testlib.h；这里由 build 补随仓库的那份
        if not base.lower().endswith((".h", ".hpp")):
            dropped.append(base)
            continue
        found = problem.resolve(text)
        if found is None:
            raise _ConfigError(notice("missing_file", path=problem.view.full(problem.path(text))))
        bundle.checker_support[base] = FileRef(member=problem.view.full(found))
    if dropped:
        bundle.warn(notice("extra_files_dropped", files=", ".join(dropped)))


def _langs(bundle: ProblemBundle, config: Dict[str, Any]) -> None:
    best = None
    for lang in _as_list(config.get("langs")):
        match = _LANG_STD.match(str(lang))
        if match and (best is None or _STD_ORDER.index(match.group(1)) > _STD_ORDER.index(best)):
            best = match.group(1)
    if best is not None:
        bundle.compile_flags = "-std=c++%s" % best
        bundle.warn(notice("languages_mapped", flags=bundle.compile_flags))


def _hydro_config(bundle: ProblemBundle, problem: _Problem, config: Dict[str, Any], path: str,
                  time: Any, memory: Any) -> None:
    kind = config.get("type") or "default"
    if kind != "default":
        code = _TYPE_ERRORS.get(str(kind))
        raise _ConfigError(notice(code) if code else notice("problem_type_unsupported", type=kind))
    if config.get("multi_pass"):
        raise _ConfigError(notice("problem_type_unsupported", type="multi_pass"))
    if _as_list(config.get("user_extra_files")):
        raise _ConfigError(notice("grader_unsupported"))
    filename = config.get("filename")
    if isinstance(filename, str) and filename.strip():
        filename = filename.strip()
        bundle.io_mode = "file"
        bundle.input_file = filename + ".in"
        bundle.output_file = filename + ".out"
        bundle.ident = filename
    _checker(bundle, problem, config, path)
    _langs(bundle, config)
    subtasks = _yaml_subtasks(config)
    if not any(sub.cases for sub in subtasks):
        subtasks = _auto_subtasks(problem.files, time, memory)
    normalized = _normalize(bundle, problem, subtasks, time, memory, {"min": None, "max": "max_as_bundle"})
    _apply_limits(bundle, normalized, time, memory, path)
    bundle.cases, bundle.subtasks = normalized.cases, normalized.subtasks


def _syzoj_config(bundle: ProblemBundle, problem: _Problem, config: Dict[str, Any], path: str) -> None:
    if config.get("interactor"):
        raise _ConfigError(notice("interactive_unsupported"))
    if config.get("specialJudge"):
        raise _ConfigError(notice("checker_protocol_unsupported", checker="syzoj"))
    if config.get("extraSourceFiles"):
        raise _ConfigError(notice("grader_unsupported"))
    input_tpl, output_tpl = config.get("inputFile"), config.get("outputFile")
    if not isinstance(input_tpl, str) or not input_tpl:
        raise _ConfigError(notice("bad_manifest", path=path, reason="缺少 inputFile"))
    subtasks = []
    for item in _as_list(config.get("subtasks")):
        if not isinstance(item, dict):
            continue
        cases = []
        for case in _as_list(item.get("cases")):
            label = str(case)
            output = None
            if isinstance(output_tpl, str) and output_tpl and output_tpl != "-":
                output = output_tpl.replace("#", label, 1)
            cases.append(_RawCase(input=input_tpl.replace("#", label, 1), output=output))
        subtasks.append(_RawSubtask(cases=cases, type=item.get("type") or "sum", score=item.get("score")))
    if not any(sub.cases for sub in subtasks):
        subtasks = _auto_subtasks(problem.files, DEFAULT_TIME_LIMIT_MS, "%dm" % DEFAULT_MEMORY_LIMIT_MB)
    normalized = _normalize(bundle, problem, subtasks, DEFAULT_TIME_LIMIT_MS, "%dm" % DEFAULT_MEMORY_LIMIT_MB,
                            {"min": None, "mul": "mul_as_bundle"})
    bundle.time_limit_ms, bundle.memory_limit_mb = DEFAULT_TIME_LIMIT_MS, DEFAULT_MEMORY_LIMIT_MB
    bundle.warn(notice("limits_defaulted"))
    bundle.cases, bundle.subtasks = normalized.cases, normalized.subtasks


def _meta(bundle: ProblemBundle, result: ImportResult, problem: _Problem) -> Tuple[Any, Any]:
    """读 problem.yaml：pid / title；返回 ICPC limits 的 (time, memory) 覆盖值（没有为 None）。"""
    view, base = problem.view, problem.base
    for name in view.listdir(base.rstrip("/")):
        if name != PROBLEM_YAML and (name.startswith("problem_") or name.endswith(".md")):
            result.warn(notice("statement_dropped"))
    if any(view.is_dir(base + d) for d in ("additional_file", "statement", "problem_statement", "solution", "std")):
        result.warn(notice("statement_dropped"))
    if not view.exists(base + PROBLEM_YAML):
        return None, None
    try:
        doc = yaml_lite.load(view.read(base + PROBLEM_YAML))
    except (yaml_lite.YamlError, ProblemIoError):
        doc = None
    if not isinstance(doc, dict):
        bundle.warn(notice("problem_yaml_ignored"))
        return None, None
    pid = doc.get("pid")
    if isinstance(pid, (str, int)) and not isinstance(pid, bool) and str(pid).strip():
        bundle.source_name = bundle.ident = str(pid).strip()
    title = doc.get("title") or doc.get("name")
    if isinstance(title, (str, int, float)) and not isinstance(title, bool) and str(title).strip():
        bundle.title = str(title).strip()
    if doc.get("tag") or doc.get("content") or doc.get("statements"):
        result.warn(notice("statement_dropped"))
    limits = doc.get("limits")
    if not isinstance(limits, dict):
        return None, None
    time = limits.get("time_limit")
    memory = limits.get("memory")
    return (
        "%sms" % (time * 1000) if _is_number(time) and time > 0 else None,
        "%sm" % memory if _is_number(memory) and memory > 0 else None,
    )


def _parse_problem(view: ArchiveView, base: str, result: ImportResult) -> ProblemBundle:
    folder = base.rstrip("/")
    key = folder or "."
    label = folder or (view.root.rstrip("/").rsplit("/", 1)[-1] if view.root else "")
    bundle = ProblemBundle(key=key, source_name=label, ident=label)
    problem = _Problem(view, base)
    time_override, memory_override = _meta(bundle, result, problem)
    try:
        config_rel = problem.find(*CONFIG_NAMES)
        ini_rel = problem.find(INI_NAME) if config_rel is None else None
        syzoj_rel = problem.find(SYZOJ_NAME) if config_rel is None and ini_rel is None else None
        if syzoj_rel is not None:
            _syzoj_config(bundle, problem, _load_yaml(view, syzoj_rel), view.full(syzoj_rel))
        else:
            config = _load_yaml(view, config_rel) if config_rel is not None else {}
            path = view.full(config_rel or ini_rel or problem.data)
            time = time_override or config.get("time") or "%dms" % DEFAULT_TIME_LIMIT_MS
            memory = memory_override or config.get("memory") or "%dm" % DEFAULT_MEMORY_LIMIT_MB
            if ini_rel is not None:
                _ini_problem(bundle, problem, _ini_subtasks(view, ini_rel), path, time, memory)
            else:
                _hydro_config(bundle, problem, config, path, time, memory)
    except _ConfigError as exc:
        bundle.fail(exc.notice)
    if len(bundle.cases) > MAX_CASES:
        bundle.fail(notice("too_many_cases", count=len(bundle.cases), limit=MAX_CASES))
    return bundle


def _ini_problem(bundle: ProblemBundle, problem: _Problem, subtasks: List[_RawSubtask], path: str,
                 time: Any, memory: Any) -> None:
    if not subtasks:
        subtasks = _auto_subtasks(problem.files, time, memory)
    normalized = _normalize(bundle, problem, subtasks, time, memory, {"min": None})
    _apply_limits(bundle, normalized, time, memory, path)
    bundle.cases, bundle.subtasks = normalized.cases, normalized.subtasks


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="hydro")``。"""
    result = ImportResult(format="hydro")
    for base in _problem_dirs(view):
        result.problems.append(_parse_problem(view, base, result))
    return result


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _filename(problem: ExportProblem, warnings: List[Notice]) -> Optional[str]:
    if problem.io_mode != "file":
        return None
    inp, out = problem.effective_input_file, problem.effective_output_file
    if inp.endswith(".in") and out.endswith(".out") and inp[:-3] == out[:-4] and inp[:-3]:
        return inp[:-3]
    warnings.append(notice("io_names_lost", input=inp, output=out, ident=problem.ident))
    return problem.ident


def _units(testdata: Any) -> List[Tuple[int, List[Any]]]:
    """按测试点顺序排出计分单元：子任务（首次出现时整组写出）与散点（各自单点）。"""
    by_name = {case.name: case for case in testdata.cases}
    owner = {name: index for index, sub in enumerate(testdata.subtasks) for name in sub.cases}
    units: List[Tuple[int, List[Any]]] = []
    emitted = set()
    for case in testdata.cases:
        index = owner.get(case.name)
        if index is None:
            units.append((case.score, [case]))
        elif index not in emitted:
            emitted.add(index)
            sub = testdata.subtasks[index]
            units.append((sub.score, [by_name[name] for name in sub.cases]))
    return units


def _copy(zf_in: Any, member: str, zf: Any, arcname: str) -> None:
    if getattr(zf, "dry_run", False):
        zf.writestr(arcname, b"")
        return
    with zf_in.open(member) as source, zf.open(arcname, "w") as target:
        shutil.copyfileobj(source, target, 256 * 1024)


def _export_one(problem: ExportProblem, open_blob: OpenBlob, zf: Any) -> List[Notice]:
    warnings: List[Notice] = []
    folder = problem.ident
    meta: Dict[str, Any] = {}
    if valid_pid(problem.ident):
        meta["pid"] = problem.ident
    else:
        warnings.append(notice("pid_omitted", ident=problem.ident))
    meta["title"] = problem.title or problem.ident
    meta["tag"] = []
    zf.writestr(folder + "/" + PROBLEM_YAML, yaml_lite.dump(meta))

    config: Dict[str, Any] = {
        "type": "default",
        "time": "%dms" % problem.effective_time_limit_ms,
        "memory": "%dm" % problem.effective_memory_limit_mb,
    }
    filename = _filename(problem, warnings)
    if filename:
        config["filename"] = filename
    if problem.compile_flags:
        warnings.append(notice("flags_dropped", flags=problem.compile_flags))
    prefix = "%s/%s/" % (folder, TESTDATA_DIR)
    with open_testdata(problem, open_blob) as (zf_in, testdata):
        flat = flat_case_names([case.name for case in testdata.cases])
        for case in testdata.cases:
            _copy(zf_in, case.input, zf, prefix + flat[case.name] + ".in")
            _copy(zf_in, case.answer, zf, prefix + flat[case.name] + ".out")
        if testdata.checker is not None:
            config["checker_type"] = "testlib"
            config["checker"] = "checker.cpp"
            _copy(zf_in, testdata.checker, zf, prefix + "checker.cpp")
            extra = []
            for member in sorted(testdata.checker_support):
                base = member.rsplit("/", 1)[-1]
                if base.lower() == TESTLIB_NAME:
                    continue
                _copy(zf_in, member, zf, prefix + base)
                extra.append(base)
            if extra:
                config["judge_extra_files"] = extra
        subtasks = []
        for number, (score, cases) in enumerate(_units(testdata), 1):
            subtasks.append({
                "id": number,
                "score": int(score),
                "type": "min",
                "cases": [{"input": flat[c.name] + ".in", "output": flat[c.name] + ".out"} for c in cases],
            })
        config["subtasks"] = subtasks
        total = testdata.total_score
        count = len(testdata.cases)
    zf.writestr(prefix + "config.yaml", yaml_lite.dump(config))
    if total != 100:
        warnings.append(notice("hydro_total_not_100", total=total))
    if count > HYDRO_CASE_LIMIT:
        warnings.append(notice("hydro_case_limit", count=count))
    return warnings


def export(problems: List[ExportProblem], open_blob: OpenBlob, zf: Any) -> Dict[int, List[Notice]]:
    """把 ``problems``（都有数据包）写进 ``zf``，返回每题的警告。"""
    out: Dict[int, List[Notice]] = {}
    for problem in problems:
        try:
            out[problem.id] = _export_one(problem, open_blob, zf)
        except FileNotFoundError:
            out[problem.id] = [notice("missing_file", path="%s/%s" % (problem.ident, TESTDATA_DIR))]
    return out
