"""HOJ 格式（格式 id ``hoj``）：HimitZH/HOJ 后台「导入 / 导出题目」的 zip。

::

    problem_<k>.json        # ImportProblemVO：{"problem": {...}, "samples": [...], "languages": [...],
    problem_<k>/1.in ...    #                   "tags": [], "codeTemplates": [], "judgeMode": "..."}

导入：根目录每个 ``X.json`` 配同名目录 ``X/`` 是一道题；``samples`` 是全部评测数据（不是样例）。
计分按 ``problem.judgeCaseMode`` 折算成测试点分值 / 捆绑子任务，折算不了的给警告。导出：``problem_<k>.json`` + ``problem_<k>/<i>.in|.out``，有子任务时写
``subtask_lowest``（组内每点分值 = 子任务分），不在子任务里的点各自成组。
"""

from __future__ import annotations

import json
import math
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

from ...judging.testdata import MAX_CASES
from .archive import ArchiveView, load_json
from .build import unique_name
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
from .checkers import TESTLIB_NAME, uses_testlib
from .exporting import OpenBlob, open_testdata
from .idents import ident_from_io
from .notices import ProblemIoError, limits_defaulted, notice, register, scores_uneven

__all__ = ["LABEL", "EXPORT_LANGUAGES", "parse", "export"]

LABEL = "HOJ"

register({
    "hoj_extra_headers": "{files} 写进 judgeExtraFile，HOJ 再次导出时会丢失",
})

#: 导出时写的 languages（HOJ 导入要求是数组；只开 C/C++）。C++ 17 / 20 只在编译参数带了对应
#: 标准时才加 —— 导入时 languages 里有 C++ 17 / 20 就会取 -std=c++17 / c++2a，否则往返会凭空多出标准
EXPORT_LANGUAGES = ["C", "C With O2", "C++", "C++ With O2"]
_CXX17_LANGUAGES = ["C++ 17", "C++ 17 With O2"]
_CXX20_LANGUAGES = ["C++ 20", "C++ 20 With O2"]
#: 能由 languages 表达、再导入时能还原的编译参数（其余导出时丢弃并提示）
#: C++ 20 导入写 -std=c++2a：NOI Linux 2.0 的 g++ 9.3 不认 -std=c++20；导出时两种写法都算 C++ 20
_CXX20_FLAGS = ("-std=c++2a", "-std=c++20")
_REPRESENTED_FLAGS = {"-std=c++17"} | set(_CXX20_FLAGS)
#: 有任何一项非空就算带了题面类内容 → 整包给一次 statement_dropped
_STATEMENT_FIELDS = ("description", "input", "output", "examples", "hint", "source")


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #


def _int(value: Any) -> Optional[int]:
    """JSON 里的整数（也收整数形式的浮点数 / 字符串）；拿不到返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) < 1e12:
        return int(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return int(parsed) if math.isfinite(parsed) and abs(parsed) < 1e12 else None
    return None


def _score(value: Any, bundle: ProblemBundle) -> int:
    """测试点分值：缺省 / 非法按 0，小数向下取整并给 score_truncated。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return 0
    if not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)) or value < 0:
        return 0
    if isinstance(value, float) and value != int(value):
        bundle.warn(notice("score_truncated"))
    return int(value)


def _java_round(value: float) -> int:
    """``Math.round``：.5 向上取整（Python 的 round 是银行家舍入）。"""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def _extra_files(value: Any) -> Dict[str, str]:
    """``judgeExtraFile`` / ``userExtraFile``：顶层是对象，problem 里是 JSON 字符串。"""
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            value = load_json(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _base(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _locate(view: ArchiveView, folder: str, raw: str) -> Optional[str]:
    """在 ``folder/`` 里找 samples 引用的文件：先按相对路径，再按 basename（HOJ 写绝对路径的 bug）。"""
    text = raw.replace("\\", "/").strip()
    candidates = []
    if text and not text.startswith("/") and ":" not in text:
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if parts and ".." not in parts:
            candidates.append("/".join(parts))
    base = _base(text)
    if base and base not in candidates:
        candidates.append(base)
    for rel in candidates:
        path = "%s/%s" % (folder, rel)
        if view.exists(path):
            return path
    return None


def _checker(bundle: ProblemBundle, problem: Dict[str, Any], folder: str) -> None:
    code = problem.get("spjCode")
    if not isinstance(code, str) or not code.strip():
        bundle.fail(notice("checker_missing_source", path="%s.json: spjCode" % folder))
        return
    language = problem.get("spjLanguage")
    if language is not None and str(language).strip().upper() not in ("C", "C++"):
        bundle.fail(notice("checker_language", language=str(language)))
        return
    source = code.encode("utf-8")
    if bundle.problem_type == "interactive":
        # HOJ 的交互题：spjCode 是 testlib interactor
        bundle.interactor = FileRef(data=source)
        bundle.checker_points = "percent"
        return
    bundle.checker = FileRef(data=source)
    # HOJ 的 testlib checker 用 quitp 给 0..100 的分
    bundle.checker_points = "percent"
    if not uses_testlib(source):
        bundle.warn(notice("checker_not_testlib"))


def _extra(bundle: ProblemBundle, problem: Dict[str, Any], doc: Dict[str, Any]) -> None:
    """判题附加文件：.h 作为 checker 支持文件（testlib.h 用自带的），其余丢弃并提示。

    HOJ 导出有 bug：判题附加文件被写进顶层 ``userExtraFile``。所以 ``userExtraFile`` 里被
    checker ``#include`` 的 .h 也当支持文件收下。
    """
    judge = _extra_files(problem.get("judgeExtraFile"))
    judge.update(_extra_files(doc.get("judgeExtraFile")))
    user = _extra_files(problem.get("userExtraFile"))
    user.update(_extra_files(doc.get("userExtraFile")))
    source = b""
    if bundle.checker is not None and bundle.checker.data is not None:
        source = bundle.checker.data
    dropped: List[str] = []
    for name, content in sorted(judge.items()):
        base = _base(name)
        if base.lower() == TESTLIB_NAME:
            continue
        if bundle.checker is not None and base.lower().endswith((".h", ".hpp")):
            bundle.checker_support[base] = FileRef(data=content.encode("utf-8"))
        else:
            dropped.append(name)
    for name, content in sorted(user.items()):
        base = _base(name)
        if base.lower() == TESTLIB_NAME or base in bundle.checker_support:
            continue
        included = ('"%s"' % base).encode("utf-8") in source or ("<%s>" % base).encode("utf-8") in source
        if bundle.checker is not None and base.lower().endswith((".h", ".hpp")) and included:
            bundle.checker_support[base] = FileRef(data=content.encode("utf-8"))
        else:
            dropped.append(name)
    if dropped:
        bundle.warn(notice("extra_files_dropped", files="、".join(dropped[:5])))


def _languages(bundle: ProblemBundle, doc: Dict[str, Any]) -> None:
    names = doc.get("languages")
    if not isinstance(names, list):
        return
    normalized = {str(name).strip().upper().replace(" WITH O2", "") for name in names}
    flags = None
    if "C++ 20" in normalized:
        flags = _CXX20_FLAGS[0]
    elif "C++ 17" in normalized:
        flags = "-std=c++17"
    if flags:
        bundle.compile_flags = flags
        bundle.warn(notice("languages_mapped", flags=flags))


def _scoring(
    bundle: ProblemBundle, groups: List[int], is_acm: bool, case_mode: str
) -> None:
    """按 type / judgeCaseMode 把 samples 的分值折算成测试点分 + 捆绑子任务。"""
    cases = bundle.cases
    if is_acm:
        for case in cases:
            case.score = 0
        bundle.subtasks = [BundleSubtask(score=100, cases=[case.name for case in cases])]
        bundle.warn(notice("acm_as_bundle"))
        return
    if case_mode == "ergodic_without_error":
        bundle.warn(notice("ergodic_as_default"))
        return
    if case_mode not in ("subtask_lowest", "subtask_average"):
        return
    order: List[int] = []
    members: Dict[int, List[BundleCase]] = {}
    for case, group in zip(cases, groups):
        if group not in members:
            order.append(group)
            members[group] = []
        members[group].append(case)
    if case_mode == "subtask_lowest":
        uneven: List[Tuple[int, int]] = []
        for group in order:
            items = members[group]
            if len(items) == 1:
                continue  # 只有一个点的组就是普通测试点
            scores = [case.score for case in items]
            low = min(scores)
            if len(set(scores)) > 1:
                uneven.append((group, low))
            for case in items:
                case.score = 0
            bundle.subtasks.append(BundleSubtask(score=low, cases=[case.name for case in items]))
        if uneven:
            bundle.warn(scores_uneven(uneven))
        return
    split = False
    for group in order:
        items = members[group]
        if len(items) == 1:
            continue
        split = True
        total = _java_round(sum(case.score for case in items) / float(len(items)))
        base, rest = divmod(total, len(items))
        for index, case in enumerate(items):
            case.score = base + (1 if index >= len(items) - rest else 0)
    if split:
        bundle.warn(notice("subtask_average_split"))


def _case_name(raw: str, used: set) -> str:
    base = _base(raw)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return unique_name(stem or "case", used, "#")


def _cases(bundle: ProblemBundle, view: ArchiveView, folder: str, samples: List[Any]) -> List[int]:
    """samples → 测试点；返回每个点的 groupNum（缺省 1）。"""
    groups: List[int] = []
    used: set = set()
    if len(samples) > MAX_CASES:
        bundle.fail(notice("too_many_cases", count=len(samples), limit=MAX_CASES))
        return groups
    for index, sample in enumerate(samples, 1):
        if not isinstance(sample, dict) or not isinstance(sample.get("input"), str) or not sample["input"].strip():
            bundle.fail(notice("bad_manifest", path="%s.json" % folder, reason="samples[%d] 缺少 input" % (index - 1)))
            continue
        raw_in = sample["input"]
        raw_out = sample.get("output")
        if not isinstance(raw_out, str) or not raw_out.strip():
            # HOJ：output 缺省 = input 第一个 '.' 之前 + ".out"
            raw_out = _base(raw_in).split(".")[0] + ".out"
        found_in = _locate(view, folder, raw_in)
        found_out = _locate(view, folder, raw_out)
        if found_in is None:
            bundle.fail(notice("missing_file", path="%s/%s" % (folder, _base(raw_in))))
            continue
        if found_out is None:
            bundle.fail(notice("missing_file", path="%s/%s" % (folder, _base(raw_out))))
            continue
        bundle.cases.append(BundleCase(
            name=_case_name(raw_in, used),
            input=FileRef(member=view.full(found_in)),
            answer=FileRef(member=view.full(found_out)),
            score=_score(sample.get("score"), bundle),
        ))
        group = _int(sample.get("groupNum"))
        groups.append(group if group is not None else 1)
    return groups


def _judge_mode(bundle: ProblemBundle, problem: Dict[str, Any], doc: Dict[str, Any]) -> Optional[str]:
    """评测实际读的是 ``problem.judgeMode``；顶层 ``judgeMode`` 只作校验。"""
    inner = problem.get("judgeMode")
    outer = doc.get("judgeMode")
    inner = str(inner).strip().lower() if isinstance(inner, str) and inner.strip() else None
    outer = str(outer).strip().lower() if isinstance(outer, str) and outer.strip() else None
    mode = inner or outer or "default"
    if inner and outer and inner != outer:
        bundle.warn(notice("judge_mode_mismatch", mode=mode))
    if mode not in ("default", "spj", "interactive"):
        bundle.fail(notice("problem_type_unsupported", type=mode))
        return None
    return mode


def _parse_one(view: ArchiveView, folder: str, doc: Any) -> ProblemBundle:
    bundle = ProblemBundle(key=folder, source_name=folder)
    if not isinstance(doc, dict) or not isinstance(doc.get("problem"), dict):
        bundle.fail(notice("bad_manifest", path="%s.json" % folder, reason="缺少 problem"))
        return bundle
    problem: Dict[str, Any] = doc["problem"]
    problem_id = problem.get("problemId")
    problem_id = str(problem_id).strip() if problem_id is not None and str(problem_id).strip() else ""
    bundle.source_name = problem_id or folder
    title = problem.get("title")
    bundle.title = title.strip() if isinstance(title, str) and title.strip() else None

    time_limit = _int(problem.get("timeLimit"))
    memory_limit = _int(problem.get("memoryLimit"))
    time_missing = time_limit is None or time_limit <= 0
    memory_missing = memory_limit is None or memory_limit <= 0
    if time_missing or memory_missing:
        bundle.warn(limits_defaulted(time_missing, memory_missing))
        time_limit = DEFAULT_TIME_LIMIT_MS if time_missing else time_limit
        memory_limit = DEFAULT_MEMORY_LIMIT_MB if memory_missing else memory_limit
    bundle.time_limit_ms, bundle.memory_limit_mb = time_limit, memory_limit
    stack = _int(problem.get("stackLimit"))
    if stack is not None and stack != memory_limit:
        bundle.warn(notice("stack_limit_dropped", mb=stack))

    io_in = io_out = None
    if problem.get("isFileIO") is True:
        bundle.io_mode = "file"
        io_in = problem.get("ioReadFileName")
        io_out = problem.get("ioWriteFileName")
        bundle.input_file = io_in.strip() if isinstance(io_in, str) and io_in.strip() else None
        bundle.output_file = io_out.strip() if isinstance(io_out, str) and io_out.strip() else None
    bundle.ident = ident_from_io(bundle.input_file) or problem_id.lower() or folder

    mode = _judge_mode(bundle, problem, doc)
    if mode == "interactive":
        bundle.problem_type = "interactive"
        bundle.io_mode = "stdio"
        bundle.input_file = bundle.output_file = None
        _checker(bundle, problem, folder)
    elif mode == "spj":
        _checker(bundle, problem, folder)
    elif mode == "default" and problem.get("isRemoveEndBlank") is False:
        bundle.compare_mode = "strict"
    _extra(bundle, problem, doc)
    _languages(bundle, doc)

    samples = doc.get("samples")
    if not isinstance(samples, list):
        bundle.fail(notice("bad_manifest", path="%s.json" % folder, reason="samples 不是数组"))
        return bundle
    groups = _cases(bundle, view, folder, samples)
    if bundle.errors or not bundle.cases:
        return bundle
    case_mode = problem.get("judgeCaseMode")
    case_mode = str(case_mode).strip().lower() if isinstance(case_mode, str) else "default"
    _scoring(bundle, groups, _int(problem.get("type")) == 0, case_mode)
    return bundle


def _has_statement(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    problem = doc.get("problem")
    if isinstance(problem, dict) and any(
        isinstance(problem.get(key), str) and problem[key].strip() for key in _STATEMENT_FIELDS
    ):
        return True
    return any(isinstance(doc.get(key), list) and doc[key] for key in ("tags", "codeTemplates"))


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="hoj")``。"""
    result = ImportResult(format="hoj")
    dirs = set(view.root_dirs())
    pairs = sorted(
        (name[:-5], name) for name in view.root_files()
        if name.lower().endswith(".json") and name[:-5] in dirs
    )
    if not pairs:
        raise ProblemIoError("bad_manifest", path="X.json", reason="根目录没有 X.json 与同名目录 X/")
    statement = False
    for folder, name in sorted(pairs, key=lambda pair: _natural(pair[0])):
        try:
            doc = load_json(view.read(name))
        except (ValueError, UnicodeDecodeError) as exc:
            bundle = ProblemBundle(key=folder, source_name=folder, ident=folder)
            bundle.fail(notice("bad_manifest", path=name, reason=str(exc)[:200]))
            result.problems.append(bundle)
            continue
        statement = statement or _has_statement(doc)
        result.problems.append(_parse_one(view, folder, doc))
    if statement:
        result.warn(notice("statement_dropped"))
    return result


def _natural(text: str) -> List[Any]:
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", text)]


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _languages_for(flags: Optional[str]) -> Tuple[List[str], List[str]]:
    """编译参数 → (languages, 表达不了而丢弃的参数)。"""
    tokens = (flags or "").split()
    languages = list(EXPORT_LANGUAGES)
    cxx20 = any(flag in tokens for flag in _CXX20_FLAGS)
    if "-std=c++17" in tokens or cxx20:
        languages += _CXX17_LANGUAGES
    if cxx20:
        languages += _CXX20_LANGUAGES
    return languages, [token for token in tokens if token not in _REPRESENTED_FLAGS]


def _copy(zf_in: Any, member: str, zf: Any, arcname: str, dry_run: bool) -> None:
    if dry_run:
        zf.writestr(arcname, b"")
        return
    with zf_in.open(member) as source, zf.open(arcname, "w") as target:
        shutil.copyfileobj(source, target, 256 * 1024)


def _export_one(problem: ExportProblem, k: int, open_blob: OpenBlob, zf: Any) -> List[Notice]:
    warnings: List[Notice] = []
    dry_run = bool(getattr(zf, "dry_run", False))
    folder = "problem_%d" % k
    with open_testdata(problem, open_blob) as (zf_in, testdata):
        in_subtask: Dict[str, int] = {}
        for index, sub in enumerate(testdata.subtasks, 1):
            for name in sub.cases:
                in_subtask[name] = index
        samples: List[Dict[str, Any]] = []
        next_group = len(testdata.subtasks) + 1
        for i, case in enumerate(testdata.cases, 1):
            sample: Dict[str, Any] = {"input": "%d.in" % i, "output": "%d.out" % i}
            if testdata.subtasks:
                group = in_subtask.get(case.name)
                if group is None:
                    group, next_group = next_group, next_group + 1
                    sample["score"] = case.score
                else:
                    sample["score"] = testdata.subtasks[group - 1].score
                sample["groupNum"] = group
            else:
                sample["score"] = case.score
            samples.append(sample)
            _copy(zf_in, case.input, zf, "%s/%d.in" % (folder, i), dry_run)
            if case.answer:
                _copy(zf_in, case.answer, zf, "%s/%d.out" % (folder, i), dry_run)
            else:
                zf.writestr("%s/%d.out" % (folder, i), b"")

        checker_code: Optional[str] = None
        headers: Dict[str, str] = {}
        if testdata.interactor is not None:
            checker_code = zf_in.read(testdata.interactor).decode("utf-8", errors="replace")
            if testdata.checker is not None:
                warnings.append(notice("extra_files_dropped", files=testdata.checker.rsplit("/", 1)[-1]))
        elif testdata.checker is not None:
            source = zf_in.read(testdata.checker)
            checker_code = source.decode("utf-8", errors="replace")
            if not uses_testlib(source):
                warnings.append(notice("checker_not_testlib"))
            for member in sorted(testdata.checker_support):
                base = _base(member)
                if base.lower() == TESTLIB_NAME:
                    continue  # HOJ 评测机自带 testlib.h
                headers[base] = zf_in.read(member).decode("utf-8", errors="replace")
            if headers:
                warnings.append(notice("hoj_extra_headers", files="、".join(sorted(headers))))
        total = testdata.total_score

    mode = "interactive" if problem.problem_type == "interactive" else (
        "spj" if checker_code is not None else "default")
    if testdata.checker is not None and testdata.checker_protocol != "testlib":
        warnings.append(notice("checker_protocol_approx", protocol=testdata.checker_protocol))
    if checker_code is None and problem.compare_mode in ("trailing_spaces", "trailing_newlines"):
        warnings.append(notice("compare_mode_approx", mode=problem.compare_mode))
    if problem.io_mode == "auto":
        warnings.append(notice("io_auto_as_file"))
    languages, dropped = _languages_for(problem.compile_flags)
    if dropped:
        warnings.append(notice("flags_dropped", flags=" ".join(dropped)))
    memory = problem.effective_memory_limit_mb
    body: Dict[str, Any] = {
        "problemId": problem.ident.upper(),
        "title": problem.title or problem.ident,
        "type": 1,
        "judgeMode": mode,
        "judgeCaseMode": "subtask_lowest" if testdata.subtasks else "default",
        "timeLimit": problem.effective_time_limit_ms,
        "memoryLimit": memory,
        "stackLimit": memory,
        "isFileIO": problem.io_mode in ("file", "auto"),
        "isRemoveEndBlank": problem.compare_mode != "strict",
        "ioScore": total,
        "description": "",
        "input": "",
        "output": "",
        "examples": "",
        "hint": "",
        "source": "",
        "difficulty": 0,
        "auth": 1,
        "codeShare": True,
        "isRemote": False,
        "isGroup": False,
        "isUploadCase": True,
        "openCaseResult": True,
    }
    if problem.io_mode in ("file", "auto"):
        body["ioReadFileName"] = problem.effective_input_file
        body["ioWriteFileName"] = problem.effective_output_file
    if checker_code is not None:
        body["spjCode"] = checker_code
        body["spjLanguage"] = "C++"
    if headers:
        body["judgeExtraFile"] = json.dumps(headers, ensure_ascii=False)
    doc: Dict[str, Any] = {
        "problem": body,
        "languages": languages,
        "samples": samples,
        "tags": [],
        "codeTemplates": [],
        "judgeMode": mode,
    }
    if headers:
        doc["judgeExtraFile"] = headers
    zf.writestr("%s.json" % folder, json.dumps(doc, ensure_ascii=False, indent=1))
    return warnings


def export(problems: List[ExportProblem], open_blob: OpenBlob, zf: Any) -> Dict[int, List[Notice]]:
    """把 ``problems``（都有数据包）写进 ``zf``，返回每题的警告。"""
    out: Dict[int, List[Notice]] = {}
    for k, problem in enumerate(problems, 1):
        if problem.problem_type in ("grader", "answer_only"):
            label = "函数交互题" if problem.problem_type == "grader" else "提交答案题"
            out[problem.id] = [notice("type_not_exportable", type=label, format=LABEL)]
            continue
        try:
            out[problem.id] = _export_one(problem, k, open_blob, zf)
        except FileNotFoundError:
            out[problem.id] = [notice("missing_file", path="%s/testdata.zip" % problem.ident)]
    return out
