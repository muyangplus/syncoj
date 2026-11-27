"""LemonLime 格式（格式 id ``lemonlime``）。

比赛目录::

    <contest>.cdf               # JSON，version "1.0"（数字 1.0 与缺省也认）；一个文件含全部题目
    data/<pid>/<pid><i>.in/.out # testCases 里的路径都相对 .cdf 所在目录的 data/
    source/<选手>/...            # 忽略

导入：

* ``taskType``：0 传统；1 提交答案（答案文件名 = 各测试点输出文件的文件名）；2 交互（LemonLime 的
  交互是函数交互：``grader`` 是带 main 的源码、``interactor`` 是给选手 include 的头文件，名字取
  ``interactorName``）→ ``grader`` 题型；3 / 4 通信题报错（§12.13）。
* ``standardInputCheck`` 与 ``standardOutputCheck`` 都为真 → stdio；都为假 → 文件 IO，
  文件名取 ``inputFileName`` / ``outputFileName``；两者不同 → ``auto``（标准 IO 与文件都能评）。
* 每个 testCase 是一个计分单元：只有 1 对文件 → 普通测试点；多对 → 捆绑子任务。分值取 ``fullScore``。
  ``<k>_lemon_SUbtaskDEPENDENCE_fLAg`` 先剔除再按下标配对（``dependency_dropped``）。
* 各组 ``timeLimit``（ms）/ ``memoryLimit``（MB）取最大值，不同时给 ``limits_collapsed``。
* ``comparisonMode``：1 默认比较（两种空白都忽略）；0 严格比较（``strict``）；2 默认比较并提示放宽；
  3 生成实数 checker（eps = 10^-realPrecision，绝对或相对误差满足其一）；4 Lemon 协议的 spj
  （``checker_protocol: lemon``，分数文件）；5 在 ``specialJudge`` 同目录找源码（testlib）。
* 路径里的 ``\\`` 换成 ``/``；``..``、绝对路径、盘符 → ``unsafe_path``。

导出：``contest/contest.cdf`` + ``contest/data/<ident>/``，题型反向映射（交互题 LemonLime 没有对应，
跳过并提示），每个普通测试点一个 testCase，
每个子任务一个 testCase（装下它的全部文件对），每组都带整题的时限与内存。checker 只能给源码，
需在 LemonLime 机器上编译（``checker_needs_build``）。
"""

from __future__ import annotations

import json
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

from ...judging.testdata import MAX_CASES
from .archive import ArchiveView, load_json
from .build import flat_case_names
from .bundle import (
    BundleCase,
    BundleSubtask,
    ExportProblem,
    FileRef,
    ImportResult,
    Notice,
    ProblemBundle,
)
from .checkers import checker_language_ok, real_checker, uses_testlib
from .detect import CDF_MAGIC
from .exporting import OpenBlob, open_testdata
from .idents import ident_from_io
from .notices import ProblemIoError, notice, register

__all__ = ["LABEL", "parse", "export", "DEPENDENCE_FLAG", "EXPORT_CONTEST"]

LABEL = "LemonLime"

register({
    "lemon_layout_mismatch": "代码路径 {patterns} 不是 <题目>.cpp 或 <题目>/<题目>.cpp，LemonLime 可能找不到选手代码",
})

DEPENDENCE_FLAG = "_lemon_SUbtaskDEPENDENCE_fLAg"
#: 导出包的比赛目录名 / .cdf 主名（导出接口拿不到场次名，固定一个）
EXPORT_CONTEST = "contest"
#: LemonLime 的默认值（testCase 没写限制时）
DEFAULT_TIME_MS = 1000
DEFAULT_MEMORY_MB = 512
DEFAULT_PRECISION = 3
#: mode 5：在 specialJudge 同目录依次找这些源码
_CHECKER_FALLBACKS = ("checker.cpp", "chk.cpp", "spj.cpp")
#: specialJudge 指向这些扩展名时是编译产物，找不到源码报 checker_missing_source
_BINARY_EXTS = ("exe", "out", "bin")
_TASK_TYPE_ERRORS = {
    3: "communication_unsupported",
    4: "communication_unsupported",
}
#: taskType → 题型
_TASK_TYPES = {0: "traditional", 1: "answer_only", 2: "grader"}
_EXPORT_TASK_TYPES = {"traditional": 0, "answer_only": 1, "grader": 2}


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


class _UnsafePath(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #


def _norm(raw: str) -> str:
    """.cdf 里的路径（相对 data/）规整成 ``a/b``；不安全抛 :class:`_UnsafePath`。"""
    text = raw.replace("\\", "/").strip()
    if (
        not text
        or "\x00" in text
        or text.startswith("/")
        or (len(text) >= 2 and text[1] == ":")
        or ".." in text.split("/")
    ):
        raise _UnsafePath(raw)
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise _UnsafePath(raw)
    return "/".join(parts)


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) < 1e12:
        return int(value)
    return None


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _find_cdf(view: ArchiveView) -> Tuple[str, str]:
    """返回 ``(cdf 视图相对路径, 所在目录前缀 "" 或 "x/")``。优先浅层且旁边有 data/ 的。"""
    candidates = sorted(
        (n for n in view.names if n.lower().endswith(".cdf") and n.count("/") <= 1),
        key=lambda n: (n.count("/"), n),
    )
    if not candidates:
        raise ProblemIoError("unknown_format")
    for name in candidates:
        base = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
        if view.walk(base + "data"):
            return name, base
    name = candidates[0]
    with view.open(name) as handle:
        if handle.read(len(CDF_MAGIC)) == CDF_MAGIC:
            raise ProblemIoError("cdf_binary")
    raise ProblemIoError("cdf_without_data")


def _supported_version(value: Any) -> bool:
    """版本 1.0：LemonLime 写字符串 "1.0"，第三方脚本生成的包常写数字 1.0 或 1。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return float(value) == 1.0
    if isinstance(value, str):
        try:
            return float(value.strip()) == 1.0
        except ValueError:
            return False
    return False


def _load_cdf(view: ArchiveView, name: str) -> Dict[str, Any]:
    raw = view.read(name)
    if raw.startswith(CDF_MAGIC):
        raise ProblemIoError("cdf_binary")
    try:
        doc = load_json(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProblemIoError("bad_manifest", path=name, reason=str(exc)[:200])
    if not isinstance(doc, dict):
        raise ProblemIoError("bad_manifest", path=name, reason="顶层不是对象")
    if "version" in doc and not _supported_version(doc["version"]):
        raise ProblemIoError("version_unsupported", path=name, version=doc["version"])
    if not isinstance(doc.get("tasks"), list):
        raise ProblemIoError("bad_manifest", path=name, reason="tasks 不是数组")
    return doc


def _strip_common_dir(rels: List[str]) -> Dict[str, str]:
    """数据文件相对路径 → 测试点名：去扩展名；全在同一目录时去掉这层目录。"""
    stems = {rel: (rel.rsplit(".", 1)[0] if "." in rel.rsplit("/", 1)[-1] else rel) for rel in rels}
    folders = {_dirname(rel) for rel in rels}
    if len(folders) == 1:
        folder = folders.pop()
        if folder:
            return {rel: stem[len(folder) + 1:] for rel, stem in stems.items()}
    return stems


def _checker(view: ArchiveView, data_dir: str, bundle: ProblemBundle, task: Dict[str, Any]) -> None:
    raw = _str(task.get("specialJudge"))
    if not raw:
        bundle.fail(notice("checker_missing_source", path="data/"))
        return
    spj = _norm(raw)
    folder = _dirname(spj)
    prefix = folder + "/" if folder else ""
    name = _basename(spj)
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    candidates: List[str] = []
    if checker_language_ok(name):
        candidates.append(spj)
    candidates += [spj + ".cpp", spj + ".cc"]
    if ext:  # chk.exe → chk.cpp
        stem = prefix + name.rsplit(".", 1)[0]
        candidates += [stem + ".cpp", stem + ".cc"]
    candidates += [prefix + fallback for fallback in _CHECKER_FALLBACKS]
    found = next((c for c in candidates if view.exists(data_dir + c)), None)
    if found is None:
        if ext and ext not in _BINARY_EXTS and view.exists(data_dir + spj):
            bundle.fail(notice("checker_language", language=ext))
        else:
            bundle.fail(notice("checker_missing_source", path="data/" + spj))
        return
    bundle.checker = FileRef(member=view.full(data_dir + found))
    for name in view.listdir(data_dir + folder):
        if name.lower().endswith((".h", ".hpp")):
            bundle.checker_support[name] = FileRef(member=view.full(data_dir + prefix + name))


def _compare(view: ArchiveView, data_dir: str, bundle: ProblemBundle, task: Dict[str, Any]) -> None:
    mode = _int(task.get("comparisonMode"))
    if mode is None:
        mode = 1
    if mode == 1:
        return
    if mode == 0:
        bundle.compare_mode = "strict"
    elif mode == 2:
        bundle.warn(notice("diff_compare_relaxed"))
    elif mode == 3:
        precision = _int(task.get("realPrecision"))
        if precision is None or precision < 0:
            precision = DEFAULT_PRECISION
        precision = min(precision, 15)
        eps = 10.0 ** -precision
        bundle.checker = FileRef(data=real_checker(eps))
        bundle.warn(notice("real_checker_generated", eps="%g" % eps))
    elif mode == 4:
        _checker(view, data_dir, bundle, task)
        bundle.checker_protocol = "lemon"
    elif mode == 5:
        _checker(view, data_dir, bundle, task)
    else:
        bundle.fail(notice("checker_unknown", name="comparisonMode %d" % mode))


def _grader(view: ArchiveView, data_dir: str, bundle: ProblemBundle, task: Dict[str, Any]) -> None:
    """LemonLime 的交互题（函数交互）：``grader`` 源码 + ``interactor`` 头文件（复制成 ``interactorName``）。"""
    raw = _str(task.get("grader"))
    grader = _norm(raw) if raw else ""
    if not grader or not view.exists(data_dir + grader):
        bundle.fail(notice("checker_missing_source", path="data/" + (grader or "grader")))
        return
    bundle.grader = FileRef(member=view.full(data_dir + grader))
    bundle.grader_name = "grader.c" if grader.lower().endswith(".c") else "grader.cpp"
    raw_header = _str(task.get("interactor"))
    header = _norm(raw_header) if raw_header else ""
    if header:
        if not view.exists(data_dir + header):
            bundle.fail(notice("missing_file", path="data/" + header))
            return
        name = _basename(_str(task.get("interactorName")) or header)
        bundle.grader_files[name] = FileRef(member=view.full(data_dir + header))


def _io(bundle: ProblemBundle, task: Dict[str, Any]) -> None:
    stdin = task.get("standardInputCheck") is True
    stdout = task.get("standardOutputCheck") is True
    if stdin and stdout:
        bundle.io_mode = "stdio"
        return
    # 混合 IO（一边标准、一边文件）用 auto：输入同时是 stdin 与输入文件，输出优先取输出文件
    bundle.io_mode = "file" if stdin == stdout else "auto"
    bundle.input_file = _str(task.get("inputFileName")) or None
    bundle.output_file = _str(task.get("outputFileName")) or None


def _total_pairs(groups: List[Any]) -> int:
    """全部 testCase 的输入文件数（去掉依赖标记），只用于报 too_many_cases。"""
    total = 0
    for group in groups:
        inputs = group.get("inputFiles") if isinstance(group, dict) else None
        if isinstance(inputs, list):
            total += sum(1 for item in inputs if isinstance(item, str) and not item.endswith(DEPENDENCE_FLAG))
    return total


def _cases(view: ArchiveView, data_dir: str, bundle: ProblemBundle, task: Dict[str, Any]) -> None:
    groups = task.get("testCases")
    if groups is None:
        return
    if not isinstance(groups, list):
        bundle.fail(notice("bad_manifest", path=bundle.key, reason="testCases 不是数组"))
        return
    units: List[Tuple[int, List[Tuple[str, str]]]] = []
    count = 0
    times: List[int] = []
    memories: List[int] = []
    for index, group in enumerate(groups, 1):
        if not isinstance(group, dict):
            bundle.fail(notice("bad_manifest", path=bundle.key, reason="testCases[%d] 不是对象" % index))
            return
        inputs = group.get("inputFiles") or []
        outputs = group.get("outputFiles") or []
        if not isinstance(inputs, list) or not isinstance(outputs, list) or not all(
            isinstance(item, str) for item in list(inputs) + list(outputs)
        ):
            bundle.fail(notice("bad_manifest", path=bundle.key, reason="testCases[%d] 的文件列表不对" % index))
            return
        real_inputs = [item for item in inputs if not item.endswith(DEPENDENCE_FLAG)]
        if len(real_inputs) != len(inputs):
            bundle.warn(notice("dependency_dropped"))
        pairs: List[Tuple[str, str]] = []
        for position, raw_in in enumerate(real_inputs):
            if position >= len(outputs):
                bundle.fail(notice("missing_answer", name=raw_in))
                return
            pairs.append((_norm(raw_in), _norm(outputs[position])))
        if not pairs:
            continue
        count += len(pairs)
        if count > MAX_CASES:
            bundle.fail(notice("too_many_cases", count=_total_pairs(groups), limit=MAX_CASES))
            return
        score = group.get("fullScore", 0)
        whole = _int(score)
        if whole is None:
            whole = 0
        elif isinstance(score, float) and score != whole:
            bundle.warn(notice("score_truncated"))
        units.append((max(whole, 0), pairs))
        time_limit = _int(group.get("timeLimit"))
        memory_limit = _int(group.get("memoryLimit"))
        times.append(time_limit if time_limit is not None and time_limit > 0 else DEFAULT_TIME_MS)
        memories.append(memory_limit if memory_limit is not None and memory_limit > 0 else DEFAULT_MEMORY_MB)

    bundle.time_limit_ms = max(times) if times else DEFAULT_TIME_MS
    bundle.memory_limit_mb = max(memories) if memories else DEFAULT_MEMORY_MB
    if len(set(times)) > 1 or len(set(memories)) > 1:
        bundle.warn(notice("limits_collapsed", time=bundle.time_limit_ms, memory=bundle.memory_limit_mb))

    for _, pairs in units:
        for rel_in, rel_out in pairs:
            for rel in (rel_in, rel_out):
                if not view.exists(data_dir + rel):
                    bundle.fail(notice("missing_file", path="data/" + rel))
                    return

    names = _strip_common_dir([rel_in for _, pairs in units for rel_in, _ in pairs])
    used = set()  # type: set
    for score, pairs in units:
        members: List[str] = []
        for rel_in, rel_out in pairs:
            name = base = names[rel_in]
            serial = 2
            while name in used:
                name = "%s-%d" % (base, serial)
                serial += 1
            used.add(name)
            members.append(name)
            bundle.cases.append(BundleCase(
                name=name,
                input=FileRef(member=view.full(data_dir + rel_in)),
                answer=FileRef(member=view.full(data_dir + rel_out)),
                score=score if len(pairs) == 1 else 0,
            ))
        if len(pairs) > 1:
            bundle.subtasks.append(BundleSubtask(score=score, cases=members))


def _task(view: ArchiveView, data_dir: str, index: int, task: Any) -> ProblemBundle:
    key = "tasks[%d]" % index
    if not isinstance(task, dict):
        bundle = ProblemBundle(key=key, source_name=key)
        bundle.fail(notice("bad_manifest", path=key, reason="不是对象"))
        return bundle
    source = _str(task.get("sourceFileName"))
    title = _str(task.get("problemTitle")) or None
    bundle = ProblemBundle(
        key=key,
        source_name=source or title or key,
        ident=source or ident_from_io(_str(task.get("inputFileName"))) or title or "",
        title=title,
    )
    task_type = _int(task.get("taskType"))
    if task_type not in _TASK_TYPES and task_type is not None:
        code = _TASK_TYPE_ERRORS.get(task_type)  # type: ignore[arg-type]
        bundle.fail(notice(code) if code else notice("problem_type_unsupported", type=task.get("taskType")))
        return bundle
    bundle.problem_type = _TASK_TYPES[task_type or 0]
    _io(bundle, task)
    try:
        _compare(view, data_dir, bundle, task)
        _cases(view, data_dir, bundle, task)
        if bundle.problem_type == "grader":
            _grader(view, data_dir, bundle, task)
        elif bundle.problem_type == "answer_only":
            bundle.answer_files = {
                case.name: _basename(case.answer.member or "")
                for case in bundle.cases if case.answer is not None and case.answer.member
            }
            bundle.io_mode = "stdio"
    except _UnsafePath as exc:
        bundle.fail(notice("unsafe_path", path=exc.path))
    if bundle.time_limit_ms is None:
        bundle.time_limit_ms, bundle.memory_limit_mb = DEFAULT_TIME_MS, DEFAULT_MEMORY_MB
    return bundle


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view`` 里的比赛目录解析成 ``ImportResult(format="lemonlime")``。"""
    cdf, base = _find_cdf(view)
    doc = _load_cdf(view, cdf)
    data_dir = base + "data/"
    result = ImportResult(format="lemonlime")
    for index, task in enumerate(doc["tasks"]):
        result.problems.append(_task(view, data_dir, index, task))
    return result


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _copy(zf_in: Any, member: str, zf: Any, target: str, dry_run: bool) -> None:
    if dry_run:
        zf.writestr(target, b"")
        return
    with zf_in.open(member) as source, zf.open(target, "w") as sink:
        shutil.copyfileobj(source, sink, 256 * 1024)


def _export_task(problem: ExportProblem, zf_in: Any, testdata: Any, zf: Any, root: str,
                 notices: List[Notice]) -> Dict[str, Any]:
    dry_run = bool(getattr(zf, "dry_run", False))
    folder = problem.ident
    flat = flat_case_names([case.name for case in testdata.cases])
    time_limit = problem.effective_time_limit_ms
    memory_limit = problem.effective_memory_limit_mb
    files: Dict[str, Tuple[str, str]] = {}
    for case in testdata.cases:
        rel_in = "%s/%s.in" % (folder, flat[case.name])
        rel_out = "%s/%s.out" % (folder, flat[case.name])
        if problem.problem_type == "answer_only":
            rel_out = "%s/%s" % (folder, testdata.answer_name(case.name, problem.ident))
        _copy(zf_in, case.input, zf, root + "data/" + rel_in, dry_run)
        if case.answer:
            _copy(zf_in, case.answer, zf, root + "data/" + rel_out, dry_run)
        else:
            zf.writestr(root + "data/" + rel_out, b"")
        files[case.name] = (rel_in, rel_out)

    def group(score: int, names: List[str]) -> Dict[str, Any]:
        return {
            "fullScore": int(score),
            "inputFiles": [files[name][0] for name in names],
            "memoryLimit": memory_limit,
            "outputFiles": [files[name][1] for name in names],
            "timeLimit": time_limit,
        }

    owner = {name: sub for sub in testdata.subtasks for name in sub.cases}
    groups: List[Dict[str, Any]] = []
    emitted = set()  # type: set
    for case in testdata.cases:
        sub = owner.get(case.name)
        if sub is None:
            groups.append(group(case.score, [case.name]))
        elif id(sub) not in emitted:
            emitted.add(id(sub))
            groups.append(group(sub.score, list(sub.cases)))

    comparison, special = (0 if problem.compare_mode == "strict" else 1), ""
    if problem.compare_mode in ("trailing_spaces", "trailing_newlines") and testdata.checker is None:
        notices.append(notice("compare_mode_approx", mode=problem.compare_mode))
    if testdata.checker is not None:
        comparison = 4 if testdata.checker_protocol == "lemon" else 5
        special = "%s/checker" % folder
        if testdata.checker_protocol == "exitcode":
            notices.append(notice("checker_protocol_approx", protocol="exitcode"))
        source = zf_in.read(testdata.checker)
        _copy(zf_in, testdata.checker, zf, root + "data/%s/checker.cpp" % folder, dry_run)
        for member in testdata.checker_support:
            base = member.rsplit("/", 1)[-1]
            _copy(zf_in, member, zf, root + "data/%s/%s" % (folder, base), dry_run)
        notices.append(notice("checker_needs_build", path="data/%s/checker" % folder))
        if not uses_testlib(source):
            notices.append(notice("checker_not_testlib"))
    if problem.compile_flags:
        notices.append(notice("flags_dropped", flags=problem.compile_flags))

    extra: Dict[str, Any] = {}
    if problem.problem_type == "grader" and testdata.grader:
        member = testdata.grader.get("cpp") or next(iter(testdata.grader.values()))
        base = member.rsplit("/", 1)[-1]
        _copy(zf_in, member, zf, root + "data/%s/%s" % (folder, base), dry_run)
        extra["grader"] = "%s/%s" % (folder, base)
        headers = [m for m in testdata.grader_files if m.lower().endswith((".h", ".hpp"))]
        if headers:
            head = headers[0].rsplit("/", 1)[-1]
            _copy(zf_in, headers[0], zf, root + "data/%s/%s" % (folder, head), dry_run)
            extra["interactor"] = "%s/%s" % (folder, head)
            extra["interactorName"] = head
        if len(testdata.grader_files) > 1:
            notices.append(notice("extra_files_dropped", files=", ".join(
                m.rsplit("/", 1)[-1] for m in testdata.grader_files[1:])))

    sub_folder = _sub_folder_check(problem, notices)
    stdio = problem.io_mode != "file"
    document = {
        "answerFileExtension": "out",
        "comparisonMode": comparison,
        "compilerConfiguration": {},
        "diffArguments": "--ignore-space-change --text --brief",
        "inputFileName": problem.effective_input_file,
        "outputFileName": problem.effective_output_file,
        "problemTitle": problem.title or problem.ident,
        "realPrecision": DEFAULT_PRECISION,
        "sourceFileName": problem.ident,
        "specialJudge": special,
        "standardInputCheck": stdio,
        "standardOutputCheck": stdio,
        "subFolderCheck": sub_folder,
        "taskType": _EXPORT_TASK_TYPES.get(problem.problem_type, 0),
        "testCases": groups,
    }
    if problem.io_mode == "auto":
        # LemonLime 只能二选一：按文件 IO 写出（auto 下 freopen 的程序照样能评）
        document["standardInputCheck"] = document["standardOutputCheck"] = False
        notices.append(notice("io_auto_as_file"))
    document.update(extra)
    return document


def _sub_folder_check(problem: ExportProblem, notices: List[Notice]) -> bool:
    """按题目的代码路径模式决定 ``subFolderCheck``：LemonLime 为真时找 ``<选手>/<pid>/<pid>.cpp``，
    为假时找 ``<选手>/<pid>.cpp``。SyncOJ 按模式的相对路径把代码落到 ``source/<场次>/<选手>/``。

    用收代码时实际生效的模式（没配时是服务端配置的默认模式，不是出厂值）。"""
    from .. import matching

    patterns = problem.effective_patterns or problem.file_patterns or [matching.DEFAULT_PATTERN]
    expanded = [matching.expand_pattern(p, problem.ident, problem.title) for p in patterns]
    ident = re.escape(problem.ident)
    flat = [bool(re.match(r"^%s\.[^/*?\[]+$" % ident, p)) for p in expanded]
    nested = [bool(re.match(r"^%s/%s\.[^/*?\[]+$" % (ident, ident), p)) for p in expanded]
    if all(flat):
        return False
    if all(nested):
        return True
    notices.append(notice("lemon_layout_mismatch", patterns="、".join(patterns)))
    return not all("/" not in p for p in expanded)


def export(problems: List[ExportProblem], open_blob: OpenBlob, zf: Any) -> Dict[int, List[Notice]]:
    """写成 ``contest/contest.cdf`` + ``contest/data/<ident>/``，返回每题的警告。"""
    root = EXPORT_CONTEST + "/"
    notices: Dict[int, List[Notice]] = {}
    tasks: List[Dict[str, Any]] = []
    for problem in problems:
        notices[problem.id] = []
        if problem.problem_type == "interactive":
            notices[problem.id].append(notice("type_not_exportable", type="交互题", format=LABEL))
            continue
        try:
            with open_testdata(problem, open_blob) as (zf_in, testdata):
                tasks.append(_export_task(problem, zf_in, testdata, zf, root, notices[problem.id]))
        except FileNotFoundError:
            notices[problem.id].append(notice("missing_file", path="%s/testdata.zip" % problem.ident))
    doc = {"contestTitle": EXPORT_CONTEST, "contestants": [], "tasks": tasks, "version": "1.0"}
    zf.writestr(
        root + EXPORT_CONTEST + ".cdf",
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    return notices
