"""LemonLime .cdf 的导入与导出。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient

from syncoj_server.judging.testdata import parse_testdata
from syncoj_server.services import problem_io
from syncoj_server.services.problem_io.build import build_testdata_zip, try_build
from syncoj_server.services.problem_io.bundle import ExportProblem, ProblemBundle
from syncoj_server.services.problem_io.detect import CDF_MAGIC

from problem_io_fixtures import (
    LEMON_SAMPLE_TASKS,
    lemon_task,
    make_lemon_sample_zip,
    make_lemon_zip,
    make_testdata_zip,
    make_zip,
    sha256_hex,
    stage,
)

from test_judge_export_api import export_zip, setup_problems
from test_judge_import_api import ADMIN, accept_all, apply, make_contest, preview

DEP = "_lemon_SUbtaskDEPENDENCE_fLAg"


def parse(tmp_path: Path, data: bytes) -> List[ProblemBundle]:
    with problem_io.parse_archive(stage(tmp_path, data), "auto") as parsed:
        assert parsed.result.format == "lemonlime"
        return parsed.result.problems


def parse_one(tmp_path: Path, task: dict, **kwargs: object) -> ProblemBundle:
    (bundle,) = parse(tmp_path, make_lemon_zip([task], **kwargs))  # type: ignore[arg-type]
    return bundle


def codes(items) -> List[str]:
    return [item.code for item in items]


def structure(tmp_path: Path, data: bytes) -> Dict[str, dict]:
    """解析 + 构建数据包，返回每题（ident → 语义摘要）。"""
    out = {}
    with problem_io.parse_archive(stage(tmp_path, data, "rt.zip"), "auto") as parsed:
        for bundle in parsed.result.problems:
            assert not bundle.errors, bundle.errors
            blob = build_testdata_zip(bundle, parsed.archive)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                td = parse_testdata(zf)
                inputs = [zf.read(case.input) for case in td.cases]
            out[bundle.ident] = {
                "time": bundle.time_limit_ms,
                "memory": bundle.memory_limit_mb,
                "io": (bundle.io_mode, bundle.input_file, bundle.output_file),
                "scores": [case.score for case in td.cases],
                "subtasks": sorted((s.score, len(s.cases)) for s in td.subtasks),
                "checker": td.checker is not None,
                "inputs": inputs,
                "total": td.total_score,
            }
    return out


# --------------------------------------------------------------------------- #
# 真实包 的复刻
# --------------------------------------------------------------------------- #


def test_real_sample_package(tmp_path: Path) -> None:
    with problem_io.parse_archive(stage(tmp_path, make_lemon_sample_zip()), "auto") as parsed:
        assert parsed.result.format == "lemonlime"
        problems = parsed.result.problems
        assert [(p.ident, p.title, p.time_limit_ms, p.memory_limit_mb) for p in problems] == [
            (pid, pid, limit, 512) for pid, limit in LEMON_SAMPLE_TASKS
        ]
        for bundle in problems:
            assert not bundle.errors and not bundle.warnings, (bundle.errors, bundle.warnings)
            # 文件 IO <pid>.in/.out：等于默认名，规整成 None
            assert (bundle.io_mode, bundle.input_file, bundle.output_file) == ("file", None, None)
            assert bundle.checker is None and not bundle.subtasks
            assert [c.name for c in bundle.cases] == ["%s%d" % (bundle.ident, i) for i in range(1, 21)]
            assert bundle.cases[0].input.member == "sample/data/%s/%s1.in" % (bundle.ident, bundle.ident)
            built = try_build(bundle, parsed.archive)
            assert built is not None, bundle.errors
            assert built[1] == {"cases": 20, "subtasks": 0, "has_checker": False, "total_score": 100}
        assert not parsed.result.warnings


def test_real_sample_package_through_api(client: TestClient, contest: dict, admin_headers: dict) -> None:
    response = preview(client, contest, admin_headers, make_lemon_sample_zip(), name="sample.zip")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "lemonlime"
    assert [
        (p["ident"], p["action"], p["time_limit_ms"], p["memory_limit_mb"], p["io_mode"], p["cases"], p["total_score"])
        for p in body["problems"]
    ] == [(pid, "create", limit, 512, "file", 20, 100) for pid, limit in LEMON_SAMPLE_TASKS]
    result = apply(client, contest, admin_headers, body["token"], accept_all(body)).json()
    assert result["created"] == 6
    assert all(p["testdata"]["total_score"] == 100 for p in result["problems"])


# --------------------------------------------------------------------------- #
# 测试点与子任务
# --------------------------------------------------------------------------- #


def test_single_and_multi_file_groups(tmp_path: Path) -> None:
    task = lemon_task("sum", 1)
    task["testCases"] = [
        {"fullScore": 30, "timeLimit": 1000, "memoryLimit": 256,
         "inputFiles": ["sum/sum1.in"], "outputFiles": ["sum/sum1.out"]},
        {"fullScore": 70, "timeLimit": 1000, "memoryLimit": 256,
         "inputFiles": ["sum/sum2.in", "sum/sum3.in"], "outputFiles": ["sum/sum2.out", "sum/sum3.out"]},
    ]
    bundle = parse_one(tmp_path, task)
    assert not bundle.errors and not bundle.warnings
    assert [(c.name, c.score) for c in bundle.cases] == [("sum1", 30), ("sum2", 0), ("sum3", 0)]
    assert [(s.score, s.cases) for s in bundle.subtasks] == [(70, ["sum2", "sum3"])]
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (1000, 256)


def test_dependency_flags_are_stripped(tmp_path: Path) -> None:
    task = lemon_task("sum", 1)
    task["testCases"] = [
        {"fullScore": 40, "timeLimit": 1000, "memoryLimit": 256,
         "inputFiles": ["sum/a.in", "sum/b.in"], "outputFiles": ["sum/a.out", "sum/b.out"]},
        {"fullScore": 60, "timeLimit": 1000, "memoryLimit": 256,
         "inputFiles": ["sum/c.in", "1" + DEP, "sum/d.in"], "outputFiles": ["sum/c.out", "sum/d.out"]},
    ]
    bundle = parse_one(tmp_path, task)
    assert codes(bundle.warnings) == ["dependency_dropped"]
    assert [(s.score, s.cases) for s in bundle.subtasks] == [(40, ["a", "b"]), (60, ["c", "d"])]
    assert bundle.cases[3].answer.member == "data/sum/d.out"


def test_limits_collapse_to_maximum(tmp_path: Path) -> None:
    task = lemon_task("sum", 3)
    task["testCases"][1]["timeLimit"] = 2500  # type: ignore[index]
    task["testCases"][2]["memoryLimit"] = 1024  # type: ignore[index]
    bundle = parse_one(tmp_path, task)
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (2500, 1024)
    (warning,) = bundle.warnings
    assert warning.code == "limits_collapsed" and "2500 ms / 1024 MB" in warning.message


def test_fractional_and_missing_scores(tmp_path: Path) -> None:
    task = lemon_task("sum", 2)
    task["testCases"][0]["fullScore"] = 33.5  # type: ignore[index]
    bundle = parse_one(tmp_path, task)
    assert "score_truncated" in codes(bundle.warnings)
    assert bundle.cases[0].score == 33

    task = lemon_task("sum", 4)
    for group in task["testCases"]:  # type: ignore[union-attr]
        group["fullScore"] = 0
    bundle = parse_one(tmp_path, task)
    assert "scores_missing" in codes(bundle.warnings)
    assert sum(c.score for c in bundle.cases) == 100


def test_missing_data_file(tmp_path: Path) -> None:
    data = make_lemon_zip([lemon_task("sum", 2)], drop=["data/sum/sum2.out"])
    (bundle,) = parse(tmp_path, data)
    assert codes(bundle.errors) == ["missing_file"]
    assert "data/sum/sum2.out" in bundle.errors[0].message


def test_input_without_output(tmp_path: Path) -> None:
    task = lemon_task("sum", 1)
    task["testCases"][0]["inputFiles"].append("sum/extra.in")  # type: ignore[index]
    bundle = parse_one(tmp_path, task, extra={"data/sum/extra.in": "1\n"})
    assert codes(bundle.errors) == ["missing_answer"]


# --------------------------------------------------------------------------- #
# 比较方式
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode, code", [(1, None), (0, "strict_compare_relaxed"), (2, "diff_compare_relaxed")])
def test_text_compare_modes(tmp_path: Path, mode: int, code: str) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=mode))
    assert not bundle.errors and bundle.checker is None
    assert codes(bundle.warnings) == ([code] if code else [])


def test_real_number_mode_generates_checker(tmp_path: Path) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=3, realPrecision=2))
    assert not bundle.errors
    assert codes(bundle.warnings) == ["real_checker_generated"]
    assert "0.01" in bundle.warnings[0].message
    assert bundle.checker is not None and bundle.checker.data is not None
    assert b"0.01" in bundle.checker.data and b"testlib.h" not in bundle.checker.data


def test_lemon_spj_mode_is_rejected(tmp_path: Path) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=4, specialJudge="sum/spj"))
    assert codes(bundle.errors) == ["lemon_spj_unsupported"]


@pytest.mark.parametrize("spj, source", [
    ("sum/chk", "sum/chk.cpp"),
    ("sum/chk", "sum/chk.cc"),
    ("sum/chk.exe", "sum/chk.cpp"),
    ("sum/chk", "sum/checker.cpp"),
    ("sum/judge", "sum/spj.cpp"),
    ("sum\\chk", "sum/chk.cpp"),
])
def test_testlib_mode_finds_checker_source(tmp_path: Path, spj: str, source: str) -> None:
    checker = '#include "testlib.h"\nint main(int c,char**v){registerTestlibCmd(c,v);quitf(_ok,"ok");}\n'
    bundle = parse_one(
        tmp_path, lemon_task("sum", comparisonMode=5, specialJudge=spj),
        extra={"data/sum/chk": b"\x7fELF", "data/" + source: checker, "data/sum/testlib.h": "// vendored\n",
               "data/sum/helper.hpp": "// h\n"},
    )
    assert not bundle.errors, bundle.errors
    assert bundle.checker is not None and bundle.checker.member == "data/" + source
    assert sorted(bundle.checker_support) == ["helper.hpp", "testlib.h"]


def test_testlib_mode_without_source(tmp_path: Path) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=5, specialJudge="sum/chk"),
                       extra={"data/sum/chk": b"\x7fELF"})
    assert codes(bundle.errors) == ["checker_missing_source"]
    assert "data/sum/chk" in bundle.errors[0].message
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=5, specialJudge=""))
    assert codes(bundle.errors) == ["checker_missing_source"]
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=5, specialJudge="sum/chk.exe"),
                       extra={"data/sum/chk.exe": b"MZ"})
    assert codes(bundle.errors) == ["checker_missing_source"]
    bundle = parse_one(tmp_path, lemon_task("sum", comparisonMode=5, specialJudge="sum/chk.py"),
                       extra={"data/sum/chk.py": "print('ok')\n"})
    assert codes(bundle.errors) == ["checker_language"]


def test_testlib_checker_injects_testlib(tmp_path: Path) -> None:
    checker = '#include "testlib.h"\nint main(){}\n'
    data = make_lemon_zip([lemon_task("sum", comparisonMode=5, specialJudge="sum/chk")],
                          extra={"data/sum/chk.cpp": checker})
    with problem_io.parse_archive(stage(tmp_path, data), "auto") as parsed:
        (bundle,) = parsed.result.problems
        built = try_build(bundle, parsed.archive)
        assert built is not None and built[1]["has_checker"]
        assert "testlib_injected" in codes(bundle.warnings)


# --------------------------------------------------------------------------- #
# IO 方式与题型
# --------------------------------------------------------------------------- #


def test_stdio(tmp_path: Path) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", standardInputCheck=True, standardOutputCheck=True))
    assert (bundle.io_mode, bundle.input_file, bundle.output_file) == ("stdio", None, None)
    assert not bundle.warnings


def test_custom_io_names(tmp_path: Path) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", inputFileName="data.txt", outputFileName="ans.txt"))
    assert (bundle.ident, bundle.io_mode, bundle.input_file, bundle.output_file) == ("sum", "file", "data.txt", "ans.txt")


@pytest.mark.parametrize("stdin, stdout", [(True, False), (False, True)])
def test_mixed_io_becomes_file(tmp_path: Path, stdin: bool, stdout: bool) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", standardInputCheck=stdin, standardOutputCheck=stdout,
                                            inputFileName="in.txt"))
    assert bundle.io_mode == "file" and bundle.input_file == "in.txt"
    assert codes(bundle.warnings) == ["mixed_io"]


@pytest.mark.parametrize("task_type, code", [
    (1, "answers_only_unsupported"),
    (2, "interactive_unsupported"),
    (3, "communication_unsupported"),
    (4, "communication_unsupported"),
    (9, "problem_type_unsupported"),
])
def test_task_types_are_rejected(tmp_path: Path, task_type: int, code: str) -> None:
    bundle = parse_one(tmp_path, lemon_task("sum", taskType=task_type))
    assert codes(bundle.errors) == [code]


def test_other_tasks_survive_a_rejected_one(tmp_path: Path) -> None:
    problems = parse(tmp_path, make_lemon_zip([lemon_task("a", taskType=2), lemon_task("b")]))
    assert [(p.ident, codes(p.errors)) for p in problems] == [("a", ["interactive_unsupported"]), ("b", [])]


# --------------------------------------------------------------------------- #
# 路径与整包
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["../secret.in", "sum/../../x.in", "/etc/passwd", "C:/x.in", "sum\\..\\..\\x.in"])
def test_unsafe_paths_are_rejected(tmp_path: Path, path: str) -> None:
    task = lemon_task("sum", 1)
    task["testCases"][0]["inputFiles"] = [path]  # type: ignore[index]
    doc = {"contestTitle": "c", "contestants": [], "tasks": [task], "version": "1.0"}
    data = make_zip({"c.cdf": json.dumps(doc), "data/sum/sum1.out": "1\n"})
    (bundle,) = parse(tmp_path, data)
    assert codes(bundle.errors) == ["unsafe_path"]


def test_backslash_paths_are_normalized(tmp_path: Path) -> None:
    task = lemon_task("sum", 2)
    for i, group in enumerate(task["testCases"], 1):  # type: ignore[union-attr]
        group["inputFiles"] = ["sum\\sum%d.in" % i]
        group["outputFiles"] = [".\\sum\\sum%d.out" % i]
    bundle = parse_one(tmp_path, task, extra={"data/sum/sum1.out": "2\n", "data/sum/sum2.out": "4\n"})
    assert not bundle.errors
    assert [(c.name, c.input.member, c.answer.member) for c in bundle.cases] == [
        ("sum1", "data/sum/sum1.in", "data/sum/sum1.out"),
        ("sum2", "data/sum/sum2.in", "data/sum/sum2.out"),
    ]


def test_data_is_resolved_next_to_the_cdf(tmp_path: Path) -> None:
    # 外壳目录 a/：.cdf 与 data/ 都在 a/ 下，按 .cdf 所在目录找 data/
    problems = parse(tmp_path, make_lemon_zip([lemon_task("sum")], shell="a"))
    assert problems[0].cases[0].input.member == "a/data/sum/sum1.in"


def test_version(tmp_path: Path) -> None:
    (bundle,) = parse(tmp_path, make_lemon_zip([lemon_task("sum")], version=None))
    assert not bundle.errors
    with pytest.raises(problem_io.ProblemIoError) as exc:
        problem_io.parse_archive(stage(tmp_path, make_lemon_zip(version="2.0")), "auto")
    assert exc.value.code == "version_unsupported"


@pytest.mark.parametrize("cdf, code", [
    (CDF_MAGIC + b"\x00\x01binary", "cdf_binary"),
    (b"{not json", "bad_manifest"),
    (b'{"tasks": 3}', "bad_manifest"),
])
def test_bad_cdf(tmp_path: Path, cdf: bytes, code: str) -> None:
    with pytest.raises(problem_io.ProblemIoError) as exc:
        problem_io.parse_archive(stage(tmp_path, make_lemon_zip(cdf=cdf)), "auto")
    assert exc.value.code == code


def test_cdf_without_data(tmp_path: Path) -> None:
    with pytest.raises(problem_io.ProblemIoError) as exc:
        problem_io.parse_archive(stage(tmp_path, make_lemon_zip(drop=[
            "data/game/game%d.%s" % (i, ext) for i in (1, 2, 3) for ext in ("in", "out")
        ])), "auto")
    assert exc.value.code == "cdf_without_data"


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def _blobs(*packages: bytes) -> Dict[str, bytes]:
    return {sha256_hex(data): data for data in packages}


def _problem(pid: int, ident: str, data: bytes, **kw: object) -> ExportProblem:
    return ExportProblem(id=pid, ident=ident, judge_mode="builtin", testdata_sha256=sha256_hex(data),
                         testdata_name="t.zip", testdata_size=len(data), **kw)  # type: ignore[arg-type]


def _export(problems: List[ExportProblem], blobs: Dict[str, bytes], dry_run: bool = False):
    return problem_io.export_problems(problems, "lemonlime", lambda sha: io.BytesIO(blobs[sha]), dry_run=dry_run)


SUM = make_testdata_zip(count=4, scores=[20, 20, 0, 0], subtasks=[{"score": 60, "cases": ["3", "4"]}],
                        checker='#include "testlib.h"\nint main(){}\n', support={"testlib.h": "// t\n"})
PLAIN = make_testdata_zip(count=3)
REAL = make_testdata_zip(count=2, checker="int main(){return 0;}\n")


def test_export_layout(tmp_path: Path) -> None:
    problems = [
        _problem(1, "sum", SUM, title="求和", time_limit_ms=2000, memory_limit_mb=512, io_mode="file",
                 input_file="data.in", compile_flags="-std=c++17 -O2"),
        _problem(2, "echo", PLAIN),
    ]
    outcome = _export(problems, _blobs(SUM, PLAIN))
    assert outcome.included == [1, 2]
    with zipfile.ZipFile(outcome.spool) as zf:  # type: ignore[arg-type]
        names = set(zf.namelist())
        doc = json.loads(zf.read("contest/contest.cdf").decode("utf-8"))
        assert zf.read("contest/data/sum/3.in") == b"3 30\n"
        assert zf.read("contest/data/sum/3.out") == b"33\n"
    assert {"contest/data/sum/checker.cpp", "contest/data/sum/testlib.h", "contest/data/echo/1.in"} <= names
    assert (doc["version"], doc["contestants"]) == ("1.0", [])
    sum_task, echo_task = doc["tasks"]
    assert sum_task["sourceFileName"] == "sum" and sum_task["problemTitle"] == "求和"
    assert (sum_task["taskType"], sum_task["subFolderCheck"], sum_task["compilerConfiguration"]) == (0, True, {})
    assert (sum_task["comparisonMode"], sum_task["specialJudge"]) == (5, "sum/checker")
    assert (sum_task["standardInputCheck"], sum_task["inputFileName"], sum_task["outputFileName"]) == (
        False, "data.in", "sum.out")
    assert [(g["fullScore"], g["inputFiles"], g["timeLimit"], g["memoryLimit"]) for g in sum_task["testCases"]] == [
        (20, ["sum/1.in"], 2000, 512), (20, ["sum/2.in"], 2000, 512), (60, ["sum/3.in", "sum/4.in"], 2000, 512),
    ]
    assert sum_task["testCases"][2]["outputFiles"] == ["sum/3.out", "sum/4.out"]
    assert (echo_task["comparisonMode"], echo_task["specialJudge"], echo_task["standardInputCheck"]) == (1, "", True)
    assert [g["timeLimit"] for g in echo_task["testCases"]] == [1000] * 3
    assert {k: [n.code for n in v] for k, v in outcome.notices.items()} == {
        1: ["checker_needs_build", "flags_dropped"], 2: [],
    }


def test_export_preview_warnings() -> None:
    problems = [_problem(1, "real", REAL), _problem(2, "sum", SUM, compile_flags="-O2")]
    outcome = _export(problems, _blobs(REAL, SUM), dry_run=True)
    assert outcome.spool is None
    assert {k: [n.code for n in v] for k, v in outcome.notices.items()} == {
        1: ["checker_needs_build", "checker_not_testlib"], 2: ["checker_needs_build", "flags_dropped"],
    }
    assert "data/real/checker" in outcome.notices[1][0].message


def test_export_then_import_round_trip(tmp_path: Path) -> None:
    problems = [
        _problem(1, "sum", SUM, title="求和", time_limit_ms=2000, memory_limit_mb=512, io_mode="file",
                 input_file="data.in", output_file="data.out"),
        _problem(2, "echo", PLAIN, time_limit_ms=1500),
        _problem(3, "real", REAL, io_mode="file"),
    ]
    blobs = _blobs(SUM, PLAIN, REAL)
    outcome = _export(problems, blobs)
    exported = structure(tmp_path, outcome.spool.read())  # type: ignore[union-attr]
    original = {}
    for problem in problems:
        with zipfile.ZipFile(io.BytesIO(blobs[problem.testdata_sha256 or ""])) as zf:
            td = parse_testdata(zf)
            original[problem.ident] = {
                "time": problem.effective_time_limit_ms,
                "memory": problem.effective_memory_limit_mb,
                "io": (problem.io_mode, problem.input_file, problem.output_file),
                "scores": [case.score for case in td.cases],
                "subtasks": sorted((s.score, len(s.cases)) for s in td.subtasks),
                "checker": td.checker is not None,
                "inputs": [zf.read(case.input) for case in td.cases],
                "total": td.total_score,
            }
    assert exported == original


def test_export_then_import_through_api(client: TestClient, contest: dict, admin_headers: dict) -> None:
    setup_problems(client, contest, admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=lemonlime" % (ADMIN, contest["id"]),
                         admin_headers)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name in archive.namelist():
            out.writestr(name, archive.read(name))

    target = make_contest(client, admin_headers, "lemon-round-trip")
    body = preview(client, target, admin_headers, buffer.getvalue()).json()
    assert body["format"] == "lemonlime"
    (item,) = body["problems"]
    assert not item["errors"]
    assert (item["ident"], item["title"], item["time_limit_ms"], item["memory_limit_mb"]) == ("sum", "求和", 2000, 512)
    assert (item["io_mode"], item["input_file"], item["output_file"]) == ("file", "data.in", None)
    assert (item["cases"], item["subtasks"], item["total_score"], item["has_checker"]) == (4, 1, 100, True)
    result = apply(client, target, admin_headers, body["token"], accept_all(body)).json()
    assert result["created"] == 1
    assert result["problems"][0]["testdata"]["total_score"] == 100
