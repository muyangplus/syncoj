"""QDUOJ 格式（格式 id ``qduoj``）。只导入，不导出。

包结构（QDUOJ ``ExportProblemAPI`` 的产物）：每道题一个从 1 起的数字目录 ``<n>/``，内含
``problem.json`` 与 ``testcase/1.in``、``testcase/1.out``……。QDUOJ 导入时只认连续编号的
``k.in`` / ``k.out``，``test_case_score`` 按下标对应第 k 个测试点（``input_name`` 只是记录）。
这里同样按下标取文件；编号缺号或多出未列入分值表的测试点，这道题报错。

映射：

* ``display_id`` → ident 建议；``title`` → 标题
* ``time_limit``（ms）/ ``memory_limit``（MB）→ 直接对应；缺失或不是数字时按默认值并提示
* ``rule_type=OI`` → 每个测试点用自己的分值；``ACM`` → 全部测试点一个捆绑子任务，100 分
* ``spj`` 不为 null → checker，``checker_protocol: exitcode``（QDUOJ 的 spj 以 ``spj <输入> <选手输出>``
  调用、退出码 0 通过 1 答案错；本系统多传的第三个参数它不读）；只收 C / C++
* ``io_mode`` 不在导出包里，一律 stdio；比较方式 ``trailing_newlines``（QDUOJ 只对整个文件去尾部空白）
* ``template`` 非空 → ``template_dropped``；题面、样例、标签、标程 → 整包一次 ``statement_dropped``
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from ...judging.testdata import MAX_CASES
from .archive import ArchiveView, load_json
from .bundle import (
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_TIME_LIMIT_MS,
    MAX_PROBLEMS,
    BundleCase,
    BundleSubtask,
    FileRef,
    ImportResult,
    ProblemBundle,
)
from .notices import ProblemIoError, limits_defaulted, notice, register

__all__ = ["LABEL", "parse"]

LABEL = "QDUOJ"

register(
    {
        "cases_unlisted": "测试点 {files} 不在分值表中",
        "template_dropped": "代码模板已丢弃，选手须提交完整程序",
    }
)

_PROBLEM_JSON = re.compile(r"^(\d+)/problem\.json$")
_CASE_IN = re.compile(r"^(\d+)\.in$")
#: 只要有内容就算"带了题面等"的字段
_STATEMENT_KEYS = ("description", "input_description", "output_description", "hint", "samples", "tags", "answers")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(max(min(value, 10 ** 12), -(10 ** 12)))  # 超大整数转 float 会溢出
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _has_content(value: Any) -> bool:
    if isinstance(value, dict):
        if "value" in value:  # 题面字段 {format, value}
            return _has_content(value["value"])
        return any(_has_content(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_content(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value is not False


def _limits(doc: Dict[str, Any], bundle: ProblemBundle) -> None:
    time_limit = _number(doc.get("time_limit"))
    memory_limit = _number(doc.get("memory_limit"))
    if time_limit is None or memory_limit is None:
        bundle.warn(limits_defaulted(time_limit is None, memory_limit is None))
    bundle.time_limit_ms = int(time_limit) if time_limit is not None else DEFAULT_TIME_LIMIT_MS
    bundle.memory_limit_mb = int(math.ceil(memory_limit)) if memory_limit is not None else DEFAULT_MEMORY_LIMIT_MB


def _scores(entries: List[Any], bundle: ProblemBundle) -> List[int]:
    scores: List[int] = []
    for entry in entries:
        raw = _number(entry.get("score")) if isinstance(entry, dict) else None
        value = max(raw or 0.0, 0.0)
        if value != int(value):
            bundle.warn(notice("score_truncated"))
        scores.append(int(value))
    return scores


def _case_files(view: ArchiveView, folder: str) -> Dict[int, str]:
    """``testcase/`` 下的 ``k.in`` → 视图相对路径。"""
    found: Dict[int, str] = {}
    base = folder + "/testcase"
    for name in view.listdir(base):
        match = _CASE_IN.match(name)
        if match:
            found.setdefault(int(match.group(1)), base + "/" + name)
    return found


def _parse_problem(view: ArchiveView, folder: str, result: ImportResult) -> ProblemBundle:
    path = folder + "/problem.json"
    bundle = ProblemBundle(key=folder, source_name=folder)
    try:
        doc = load_json(view.read(path))
    except (ValueError, UnicodeDecodeError) as exc:
        bundle.fail(notice("bad_manifest", path=view.full(path), reason=str(exc)[:200]))
        return bundle
    if not isinstance(doc, dict):
        bundle.fail(notice("bad_manifest", path=view.full(path), reason="不是 JSON 对象"))
        return bundle

    display_id = doc.get("display_id")
    if isinstance(display_id, (str, int)) and not isinstance(display_id, bool) and str(display_id).strip():
        bundle.source_name = bundle.ident = str(display_id).strip()
    title = doc.get("title")
    if isinstance(title, str) and title.strip():
        bundle.title = title.strip()
    _limits(doc, bundle)
    if _has_content(doc.get("template")):
        bundle.warn(notice("template_dropped"))
    if any(_has_content(doc.get(key)) for key in _STATEMENT_KEYS):
        result.warn(notice("statement_dropped"))

    rule_type = str(doc.get("rule_type") or "OI").strip().upper()
    if rule_type not in ("OI", "ACM"):
        bundle.fail(notice("problem_type_unsupported", type=rule_type))
    spj = doc.get("spj")
    if spj is not None:
        code = spj.get("code") if isinstance(spj, dict) else None
        language = str(spj.get("language") or "") if isinstance(spj, dict) else ""
        if not isinstance(code, str) or not code.strip():
            bundle.fail(notice("checker_missing_source", path=view.full(path) + ": spj"))
        elif language.strip().upper() not in ("C", "C++", ""):
            bundle.fail(notice("checker_language", language=language))
        else:
            bundle.checker = FileRef(data=code.encode("utf-8"))
            bundle.checker_protocol = "exitcode"
    else:
        bundle.compare_mode = "trailing_newlines"
    if bundle.errors:
        return bundle

    files = _case_files(view, folder)
    entries = doc.get("test_case_score")
    if entries is None:
        # 没有分值表：照 QDUOJ 从 1 起数到第一个缺号
        count = 0
        while count + 1 in files:
            count += 1
        entries = [{} for _ in range(count)]
    if not isinstance(entries, list):
        bundle.fail(notice("bad_manifest", path=view.full(path), reason="test_case_score 不是数组"))
        return bundle
    if len(entries) > MAX_CASES:
        bundle.fail(notice("too_many_cases", count=len(entries), limit=MAX_CASES))
        return bundle

    scores = _scores(entries, bundle)
    for k in range(1, len(entries) + 1):
        input_rel = files.get(k)
        if input_rel is None:
            bundle.fail(notice("missing_file", path=view.full("%s/testcase/%d.in" % (folder, k))))
            return bundle
        answer_rel = "%s/testcase/%d.out" % (folder, k)
        has_answer = view.exists(answer_rel)
        if not has_answer and bundle.checker is None:
            bundle.fail(notice("missing_answer", name=str(k)))
            return bundle
        bundle.cases.append(
            BundleCase(
                name=str(k),
                input=FileRef(member=view.full(input_rel)),
                # spj 题没有标准答案：按空文件
                answer=FileRef(member=view.full(answer_rel)) if has_answer else None,
                score=scores[k - 1],
            )
        )
    extra = sorted(number for number in files if number > len(entries))
    if extra:
        bundle.fail(notice("cases_unlisted", files=", ".join("%d.in" % n for n in extra[:5])))
        return bundle

    if rule_type == "ACM" and bundle.cases:
        for case in bundle.cases:
            case.score = 0
        if len(bundle.cases) == 1:
            bundle.cases[0].score = 100
        else:
            bundle.subtasks.append(BundleSubtask(score=100, cases=[case.name for case in bundle.cases]))
            bundle.warn(notice("acm_as_bundle"))
    return bundle


def _problem_folders(view: ArchiveView) -> List[str]:
    numbered: List[Tuple[int, str]] = []
    for name in view.names:
        match = _PROBLEM_JSON.match(name)
        if match:
            numbered.append((int(match.group(1)), match.group(1)))
    return [folder for _, folder in sorted(numbered)]


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="qduoj")``。"""
    folders = _problem_folders(view)
    if not folders:
        raise ProblemIoError("no_problems")
    if len(folders) > MAX_PROBLEMS:
        raise ProblemIoError("too_many_problems", limit=MAX_PROBLEMS)
    result = ImportResult(format="qduoj")
    for folder in folders:
        result.problems.append(_parse_problem(view, folder, result))
    return result
