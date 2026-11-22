"""QDUOJ 导入：OI / ACM、spj 报错、测试点编号缺号或多出、规整后的数据包。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

from syncoj_server.judging.testdata import parse_testdata
from syncoj_server.services import problem_io
from syncoj_server.services.problem_io.build import build_testdata_zip
from syncoj_server.services.problem_io.notices import ProblemIoError

from problem_io_fixtures import make_qduoj_zip, qduoj_problem_json, stage
from test_judge_import_api import accept_all, apply, preview


def _parse(tmp_path: Path, data: bytes, fmt: str = "auto"):
    with problem_io.parse_archive(stage(tmp_path, data), fmt) as parsed:
        built = []
        for bundle in parsed.result.problems:
            built.append(build_testdata_zip(bundle, parsed.archive) if not bundle.errors else None)
        return parsed.result, built


def _codes(items) -> List[str]:
    return [item.code for item in items]


def _testdata(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return parse_testdata(zf), {name: zf.read(name) for name in zf.namelist()}


def test_oi_problem_keeps_case_scores_and_limits(tmp_path: Path) -> None:
    doc = qduoj_problem_json("A-1001", count=3, time_limit=2500, memory_limit=512)
    doc["test_case_score"] = [
        {"score": 10, "input_name": "1.in", "output_name": "1.out"},
        {"score": 30, "input_name": "2.in", "output_name": "2.out"},
        {"score": 60, "input_name": "3.in", "output_name": "3.out"},
    ]
    result, (data,) = _parse(tmp_path, make_qduoj_zip([doc]))
    assert result.format == "qduoj"
    (bundle,) = result.problems
    assert (bundle.key, bundle.source_name, bundle.ident, bundle.title) == ("1", "A-1001", "A-1001", "A+B")
    assert (bundle.time_limit_ms, bundle.memory_limit_mb, bundle.io_mode) == (2500, 512, "stdio")
    assert [(c.name, c.score) for c in bundle.cases] == [("1", 10), ("2", 30), ("3", 60)]
    assert bundle.subtasks == [] and bundle.checker is None and bundle.errors == []
    assert "acm_as_bundle" not in _codes(bundle.warnings)
    assert _codes(result.warnings) == ["statement_dropped"]

    parsed, files = _testdata(data)
    assert [(c.name, c.score) for c in parsed.cases] == [("1", 10), ("2", 30), ("3", 60)]
    assert files["2.in"] == b"2 2\n" and files["2.ans"] == b"4\n"


def test_acm_problem_becomes_one_bundled_subtask(tmp_path: Path) -> None:
    doc = qduoj_problem_json(count=3, rule_type="ACM")
    for item in doc["test_case_score"]:  # QDUOJ 导出 ACM 题时每点写 100，只是占位
        item["score"] = 100
    result, (data,) = _parse(tmp_path, make_qduoj_zip([doc]))
    (bundle,) = result.problems
    assert [c.score for c in bundle.cases] == [0, 0, 0]
    assert [(s.score, s.cases) for s in bundle.subtasks] == [(100, ["1", "2", "3"])]
    assert "acm_as_bundle" in _codes(bundle.warnings)
    parsed, _files = _testdata(data)
    assert [(s.score, list(s.cases)) for s in parsed.subtasks] == [(100, ["1", "2", "3"])]


def test_acm_with_one_case_is_a_plain_case(tmp_path: Path) -> None:
    result, _ = _parse(tmp_path, make_qduoj_zip([qduoj_problem_json(count=1, rule_type="ACM")]))
    (bundle,) = result.problems
    assert [c.score for c in bundle.cases] == [100] and bundle.subtasks == []
    assert "acm_as_bundle" not in _codes(bundle.warnings)


def test_spj_is_rejected(tmp_path: Path) -> None:
    doc = qduoj_problem_json(spj={"code": "int main(){return 0;}", "language": "C++"})
    result, (data,) = _parse(tmp_path, make_qduoj_zip([doc]))
    (bundle,) = result.problems
    assert _codes(bundle.errors) == ["qduoj_spj_unsupported"] and data is None
    assert bundle.errors[0].message == "两参数 spj（无标准答案）无法导入"


def test_missing_case_number_is_an_error(tmp_path: Path) -> None:
    data = make_qduoj_zip([qduoj_problem_json(count=3)], drop=["1/testcase/2.in", "1/testcase/2.out"])
    result, _ = _parse(tmp_path, data)
    (bundle,) = result.problems
    assert _codes(bundle.errors) == ["missing_file"]
    assert "1/testcase/2.in" in bundle.errors[0].message


def test_missing_answer_is_an_error(tmp_path: Path) -> None:
    result, _ = _parse(tmp_path, make_qduoj_zip([qduoj_problem_json(count=2)], drop=["1/testcase/2.out"]))
    assert _codes(result.problems[0].errors) == ["missing_answer"]


def test_unlisted_case_files_are_an_error(tmp_path: Path) -> None:
    data = make_qduoj_zip([qduoj_problem_json(count=2)], extra={"1/testcase/4.in": "9 9\n", "1/testcase/4.out": "18\n"})
    result, _ = _parse(tmp_path, data)
    (bundle,) = result.problems
    assert _codes(bundle.errors) == ["cases_unlisted"] and "4.in" in bundle.errors[0].message


def test_case_files_follow_index_not_input_name(tmp_path: Path) -> None:
    # QDUOJ 只按 k.in 取文件；input_name 写成别的也按下标对应
    doc = qduoj_problem_json(count=2)
    doc["test_case_score"][0]["input_name"] = "weird.in"
    data = make_qduoj_zip([doc], extra={"1/testcase/1.in": "1 1\n"}, drop=["1/testcase/weird.in"])
    result, (built,) = _parse(tmp_path, data)
    assert result.problems[0].errors == []
    _parsed, files = _testdata(built)
    assert files["1.in"] == b"1 1\n"


def test_null_score_table_counts_consecutive_files(tmp_path: Path) -> None:
    doc = qduoj_problem_json(count=3)
    data = make_qduoj_zip([doc], extra={"1/problem.json": json.dumps(dict(doc, test_case_score=None))})
    result, _ = _parse(tmp_path, data)
    (bundle,) = result.problems
    assert [c.name for c in bundle.cases] == ["1", "2", "3"]
    # 全为 0 → 公共规整按 100 分平分
    assert sum(c.score for c in bundle.cases) == 100 and "scores_missing" in _codes(bundle.warnings)


def test_multiple_problems_shell_and_numeric_order(tmp_path: Path) -> None:
    problems = [qduoj_problem_json("p%d" % i) for i in range(1, 12)]
    result, built = _parse(tmp_path, make_qduoj_zip(problems, shell="problem-export"))
    assert [b.key for b in result.problems] == [str(i) for i in range(1, 12)]
    assert [b.ident for b in result.problems] == ["p%d" % i for i in range(1, 12)]
    assert all(data is not None for data in built)
    assert result.problems[0].cases[0].input.member == "problem-export/1/testcase/1.in"


def test_fractional_scores_bad_limits_and_template(tmp_path: Path) -> None:
    doc = qduoj_problem_json(count=2, time_limit="x", memory_limit=None, template={"C++": {"prepend": "#include <cstdio>", "template": "", "append": ""}})
    doc["test_case_score"][0]["score"] = 49.5
    result, _ = _parse(tmp_path, make_qduoj_zip([doc]))
    (bundle,) = result.problems
    codes = _codes(bundle.warnings)
    assert {"score_truncated", "limits_defaulted", "template_dropped"} <= set(codes)
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (1000, 256)
    assert bundle.cases[0].score == 49


def test_unknown_rule_type_and_bad_json(tmp_path: Path) -> None:
    data = make_qduoj_zip(
        [qduoj_problem_json(rule_type="IOI"), qduoj_problem_json("x")],
        extra={"2/problem.json": "{not json"},
    )
    result, _ = _parse(tmp_path, data, fmt="qduoj")
    first, second = result.problems
    assert _codes(first.errors) == ["problem_type_unsupported"]
    assert _codes(second.errors) == ["bad_manifest"]


def test_forced_qduoj_on_other_package_has_no_problems(tmp_path: Path) -> None:
    with pytest.raises(ProblemIoError) as info:
        _parse(tmp_path, make_qduoj_zip(extra={"x.txt": "1"}, drop=["1/problem.json"]), fmt="qduoj")
    assert info.value.code == "no_problems"


def test_preview_and_apply_through_api(client: TestClient, contest: dict, admin_headers: dict) -> None:
    data = make_qduoj_zip([qduoj_problem_json("1001"), qduoj_problem_json("1002", rule_type="ACM", count=3)])
    response = preview(client, contest, admin_headers, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "qduoj"
    assert [(p["ident"], p["cases"], p["total_score"]) for p in body["problems"]] == [("1001", 2, 100), ("1002", 3, 100)]
    response = apply(client, contest, admin_headers, body["token"], accept_all(body))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["created"] == 2
    assert [p["testdata"]["cases"] for p in out["problems"]] == [2, 3]
    assert all(p["judge_mode"] == "builtin" for p in out["problems"])


def test_too_many_problem_folders_fail_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from syncoj_server.services.problem_io import qduoj

    from problem_io_fixtures import make_zip

    parsed = []
    monkeypatch.setattr(qduoj, "_parse_problem", lambda *args: parsed.append(1))
    data = make_zip({"%d/problem.json" % i: json.dumps(qduoj_problem_json()) for i in range(1, 202)})
    with pytest.raises(ProblemIoError) as info:
        problem_io.parse_archive(stage(tmp_path, data), "qduoj")
    assert info.value.code == "too_many_problems" and parsed == []


def test_huge_integers_do_not_crash(tmp_path: Path) -> None:
    doc = qduoj_problem_json(time_limit=10 ** 400, memory_limit=10 ** 400)
    doc["test_case_score"][0]["score"] = 10 ** 400  # type: ignore[index]
    result, built = _parse(tmp_path, make_qduoj_zip([doc]))
    (bundle,) = result.problems
    assert bundle.errors == [] and built[0] is not None
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (60000, 4096)
