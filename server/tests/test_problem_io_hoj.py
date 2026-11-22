"""HOJ 格式：导入映射、导出，以及「导出 → 再导入」的语义往返。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

from syncoj_server.judging.testdata import parse_testdata
from syncoj_server.services import problem_io
from syncoj_server.services.problem_io import hoj
from syncoj_server.services.problem_io.build import build_testdata_zip
from syncoj_server.services.problem_io.bundle import ExportProblem, ProblemBundle
from syncoj_server.services.problem_io.exporting import NullZip
from syncoj_server.services.problem_io.notices import ProblemIoError

from problem_io_fixtures import (
    hoj_problem_json,
    make_hoj_zip,
    make_testdata_zip,
    make_zip,
    sha256_hex,
    stage,
)

TESTLIB_CHECKER = '#include "testlib.h"\nint main(int c, char** v) { registerTestlibCmd(c, v); quitf(_ok, "ok"); }\n'
PLAIN_SPJ = "#include <cstdio>\nint main() { return 0; }\n"


def _parse(tmp_path: Path, data: bytes, fmt: str = "auto") -> Tuple[problem_io.ImportResult, List[dict]]:
    """解析并把每道题建成数据包，返回 (result, [每题的回读摘要])。"""
    with problem_io.parse_archive(stage(tmp_path, data), fmt) as parsed:
        built = []
        for bundle in parsed.result.problems:
            built.append(_built(bundle, parsed.archive) if not bundle.errors else {})
        return parsed.result, built


def _built(bundle: ProblemBundle, archive) -> dict:
    data = build_testdata_zip(bundle, archive)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        td = parse_testdata(zf)
        return {
            "cases": [(c.name, c.score) for c in td.cases],
            "subtasks": [(s.score, s.cases) for s in td.subtasks],
            "total": td.total_score,
            "checker": zf.read(td.checker) if td.checker else None,
            "support": sorted(n for n in td.checker_support),
            "files": {c.name: (zf.read(c.input), zf.read(c.answer)) for c in td.cases},
        }


def _codes(bundle: ProblemBundle) -> List[str]:
    return [n.code for n in bundle.warnings]


def _errors(bundle: ProblemBundle) -> List[str]:
    return [n.code for n in bundle.errors]


def _one(tmp_path: Path, doc: dict, **kwargs) -> Tuple[ProblemBundle, dict, problem_io.ImportResult]:
    result, built = _parse(tmp_path, make_hoj_zip([doc], **kwargs))
    assert result.format == "hoj"
    (bundle,) = result.problems
    return bundle, built[0], result


def _samples(scores: List[int], groups: List[int] = None) -> List[dict]:
    out = []
    for i, score in enumerate(scores, 1):
        item = {"input": "%d.in" % i, "output": "%d.out" % i, "score": score}
        if groups is not None:
            item["groupNum"] = groups[i - 1]
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 导入：基本字段
# --------------------------------------------------------------------------- #


def test_basic_problem(tmp_path: Path) -> None:
    doc = hoj_problem_json("P1000", count=3, timeLimit=2000, memoryLimit=512, stackLimit=512, title="加法")
    bundle, built, result = _one(tmp_path, doc)
    assert (bundle.key, bundle.source_name, bundle.ident, bundle.title) == ("problem_1", "P1000", "p1000", "加法")
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (2000, 512)
    assert (bundle.io_mode, bundle.input_file, bundle.output_file) == ("stdio", None, None)
    assert bundle.judge_mode == "builtin" and bundle.checker is None
    assert built["cases"] == [("1", 33), ("2", 33), ("3", 34)] and built["subtasks"] == []
    assert built["files"]["2"] == (b"2 20\n", b"22\n")
    # languages 里有 C++ 17 → -std=c++17
    assert bundle.compile_flags == "-std=c++17"
    assert "languages_mapped" in _codes(bundle)
    assert "stack_limit_dropped" not in _codes(bundle)
    assert not result.warnings  # 题面为空，不提示 statement_dropped


def test_file_io_names_and_ident(tmp_path: Path) -> None:
    doc = hoj_problem_json("P1001", isFileIO=True, ioReadFileName="aplusb.in", ioWriteFileName="aplusb.ans")
    bundle, _, _ = _one(tmp_path, doc)
    # 文件 IO 题的 ident 取输入文件主名；等于默认名的那一项存 NULL
    assert (bundle.ident, bundle.io_mode) == ("aplusb", "file")
    assert (bundle.input_file, bundle.output_file) == (None, "aplusb.ans")
    assert "ident_renamed" not in _codes(bundle)


def test_file_io_flag_off_ignores_names(tmp_path: Path) -> None:
    doc = hoj_problem_json("P1", isFileIO=False, ioReadFileName="a.in", ioWriteFileName="a.out")
    bundle, _, _ = _one(tmp_path, doc)
    assert (bundle.ident, bundle.io_mode, bundle.input_file) == ("p1", "stdio", None)


def test_languages_mapping(tmp_path: Path) -> None:
    doc = hoj_problem_json()
    doc["languages"] = ["C", "C++ 20 With O2", "C++ 17", "Java"]
    bundle, _, _ = _one(tmp_path, doc)
    assert bundle.compile_flags == "-std=c++20"
    doc["languages"] = ["C", "C++", "C++ With O2", "Python3"]
    (tmp_path / "b").mkdir()
    bundle, _, _ = _one(tmp_path / "b", doc)
    assert bundle.compile_flags is None and "languages_mapped" not in _codes(bundle)


def test_stack_limit_and_statement(tmp_path: Path) -> None:
    doc = hoj_problem_json(stackLimit=128, description="<p>题面</p>")
    doc["tags"] = ["入门"]
    bundle, _, result = _one(tmp_path, doc)
    assert "stack_limit_dropped" in _codes(bundle)
    assert "128" in next(n.message for n in bundle.warnings if n.code == "stack_limit_dropped")
    assert [n.code for n in result.warnings] == ["statement_dropped"]


def test_missing_limits_default(tmp_path: Path) -> None:
    doc = hoj_problem_json()
    del doc["problem"]["timeLimit"]
    bundle, _, _ = _one(tmp_path, doc)
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (1000, 256)
    assert "limits_defaulted" in _codes(bundle)


def test_wrapper_folder_and_multiple_problems(tmp_path: Path) -> None:
    docs = [hoj_problem_json("A1", count=2), hoj_problem_json("B2", count=4)]
    result, built = _parse(tmp_path, make_hoj_zip(docs, shell="export"))
    assert [(b.key, b.ident, len(b.cases)) for b in result.problems] == [("problem_1", "a1", 2), ("problem_2", "b2", 4)]
    assert built[1]["total"] == 100


def test_duplicate_problem_ids_are_deduped(tmp_path: Path) -> None:
    result, _ = _parse(tmp_path, make_hoj_zip([hoj_problem_json("P1"), hoj_problem_json("p1")]))
    assert [b.ident for b in result.problems] == ["p1", "p1-2"]


# --------------------------------------------------------------------------- #
# 导入：测试点文件
# --------------------------------------------------------------------------- #


def test_output_defaults_to_input_stem(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=2)
    doc["samples"] = [{"input": "a.1.in", "score": 50}, {"input": "b.in", "output": "", "score": 50}]
    # input.split('.')[0] + '.out'：a.1.in → a.out
    data = make_hoj_zip([doc])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["problem_1.json", "problem_1/a.1.in", "problem_1/a.out",
                                         "problem_1/b.in", "problem_1/b.out"]
    bundle, built, _ = _one(tmp_path, doc)
    assert not bundle.errors
    assert [c.answer.member for c in bundle.cases] == ["problem_1/a.out", "problem_1/b.out"]
    assert [s for _, s in built["cases"]] == [50, 50]


def test_absolute_paths_use_basename(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=2)
    doc["samples"] = [
        {"input": "/judge/test_case/problem_7/1.in", "output": "/judge/test_case/problem_7/1.out", "score": 40},
        {"input": "C:\\hoj\\testcase\\2.in", "output": "C:\\hoj\\testcase\\2.out", "score": 60},
    ]
    bundle, built, _ = _one(tmp_path, doc)
    assert not bundle.errors
    assert [c.input.member for c in bundle.cases] == ["problem_1/1.in", "problem_1/2.in"]
    assert built["cases"] == [("1", 40), ("2", 60)]


def test_relative_subdirectory_is_kept(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=1)
    doc["samples"] = [{"input": "sub/1.in", "output": "sub/1.out", "score": 100}]
    data = make_zip({"problem_1.json": json.dumps(doc), "problem_1/sub/1.in": "1 2\n", "problem_1/sub/1.out": "3\n",
                     "problem_1/1.in": "wrong\n", "problem_1/1.out": "wrong\n"})
    result, built = _parse(tmp_path, data)
    assert result.problems[0].cases[0].input.member == "problem_1/sub/1.in"
    assert built[0]["files"]["1"] == (b"1 2\n", b"3\n")


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    bundle, _, _ = _one(tmp_path, hoj_problem_json(count=3), drop=["problem_1/2.out"])
    assert _errors(bundle) == ["missing_file"]
    assert "problem_1/2.out" in bundle.errors[0].message


def test_info_file_and_unreferenced_files_are_ignored(tmp_path: Path) -> None:
    info = json.dumps({"mode": "default", "testCasesSize": 3})
    bundle, built, _ = _one(tmp_path, hoj_problem_json(count=3), extra={"problem_1/info": info, "problem_1/x.txt": "x"})
    assert not bundle.errors and len(built["cases"]) == 3


def test_fractional_scores_truncated(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=2)
    doc["samples"] = [{"input": "1.in", "output": "1.out", "score": 33.5},
                      {"input": "2.in", "output": "2.out", "score": 66.9}]
    bundle, built, _ = _one(tmp_path, doc)
    assert built["cases"] == [("1", 33), ("2", 66)]
    assert "score_truncated" in _codes(bundle)


def test_all_zero_scores_split_100(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=4)
    for sample in doc["samples"]:
        sample.pop("score")
    bundle, built, _ = _one(tmp_path, doc)
    assert built["total"] == 100 and "scores_missing" in _codes(bundle)


# --------------------------------------------------------------------------- #
# 导入：计分模式
# --------------------------------------------------------------------------- #


def test_judge_case_mode_default(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=3, judgeCaseMode="default")
    doc["samples"] = _samples([10, 30, 60], groups=[1, 1, 2])  # default 模式下 groupNum 无效
    bundle, built, _ = _one(tmp_path, doc)
    assert built["cases"] == [("1", 10), ("2", 30), ("3", 60)] and built["subtasks"] == []
    assert not [c for c in _codes(bundle) if c.startswith("subtask")]


def test_subtask_lowest(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=5, judgeCaseMode="subtask_lowest")
    doc["samples"] = _samples([20, 30, 30, 50, 50], groups=[1, 2, 2, 3, 3])
    bundle, built, _ = _one(tmp_path, doc)
    # 只有一个点的组 = 普通测试点；多点组 = 捆绑子任务，组分 = 组内最小值
    assert built["cases"] == [("1", 20), ("2", 0), ("3", 0), ("4", 0), ("5", 0)]
    assert built["subtasks"] == [(30, ["2", "3"]), (50, ["4", "5"])]
    assert built["total"] == 100
    assert "subtask_scores_uneven" not in _codes(bundle)


def test_subtask_lowest_uneven_scores(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=3, judgeCaseMode="subtask_lowest")
    doc["samples"] = _samples([40, 60, 50], groups=[1, 2, 2])
    bundle, built, _ = _one(tmp_path, doc)
    assert built["subtasks"] == [(50, ["2", "3"])]
    assert built["total"] == 90
    uneven = next(n for n in bundle.warnings if n.code == "subtask_scores_uneven")
    assert "2" in uneven.message and "50" in uneven.message


def test_subtask_lowest_missing_group_defaults_to_1(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=3, judgeCaseMode="subtask_lowest")
    doc["samples"] = _samples([50, 50, 50])
    _, built, _ = _one(tmp_path, doc)
    assert built["subtasks"] == [(50, ["1", "2", "3"])]


def test_subtask_average(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=5, judgeCaseMode="subtask_average")
    # 组 1：round((10+20+15)/3) = 15 → 5/5/5；组 2：round((50+35)/2) = round(42.5) = 43 → 21/22
    doc["samples"] = _samples([10, 20, 15, 50, 35], groups=[1, 1, 1, 2, 2])
    bundle, built, _ = _one(tmp_path, doc)
    assert built["cases"] == [("1", 5), ("2", 5), ("3", 5), ("4", 21), ("5", 22)]
    assert built["subtasks"] == []
    assert "subtask_average_split" in _codes(bundle)


def test_ergodic_without_error(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=2, judgeCaseMode="ergodic_without_error")
    doc["samples"] = _samples([30, 70])
    bundle, built, _ = _one(tmp_path, doc)
    assert built["cases"] == [("1", 30), ("2", 70)] and built["subtasks"] == []
    assert "ergodic_as_default" in _codes(bundle)


def test_acm_problem_is_one_bundle(tmp_path: Path) -> None:
    doc = hoj_problem_json(count=3, type=0, judgeCaseMode="subtask_lowest")
    doc["samples"] = _samples([0, 0, 0], groups=[1, 2, 2])
    bundle, built, _ = _one(tmp_path, doc)
    assert built["subtasks"] == [(100, ["1", "2", "3"])]
    assert built["total"] == 100
    assert "acm_as_bundle" in _codes(bundle) and "scores_missing" not in _codes(bundle)


# --------------------------------------------------------------------------- #
# 导入：评测方式与 checker
# --------------------------------------------------------------------------- #


def test_spj_testlib_checker(tmp_path: Path) -> None:
    doc = hoj_problem_json(judgeMode="spj", spjCode=TESTLIB_CHECKER, spjLanguage="C++")
    bundle, built, _ = _one(tmp_path, doc)
    assert built["checker"] == TESTLIB_CHECKER.encode()
    assert built["support"] == ["testlib.h"]  # 自带的那份
    assert "checker_not_testlib" not in _codes(bundle)
    assert "testlib_injected" in _codes(bundle)


def test_spj_without_testlib_warns(tmp_path: Path) -> None:
    doc = hoj_problem_json(judgeMode="spj", spjCode=PLAIN_SPJ, spjLanguage="C")
    bundle, built, _ = _one(tmp_path, doc)
    assert built["checker"] == PLAIN_SPJ.encode()
    assert "checker_not_testlib" in _codes(bundle)


def test_spj_language_and_source_checks(tmp_path: Path) -> None:
    doc = hoj_problem_json(judgeMode="spj", spjCode=TESTLIB_CHECKER, spjLanguage="Java")
    bundle, _, _ = _one(tmp_path, doc)
    assert _errors(bundle) == ["checker_language"]
    (tmp_path / "b").mkdir()
    doc = hoj_problem_json(judgeMode="spj", spjCode="", spjLanguage="C++")
    bundle, _, _ = _one(tmp_path / "b", doc)
    assert _errors(bundle) == ["checker_missing_source"]


def test_interactive_is_rejected(tmp_path: Path) -> None:
    doc = hoj_problem_json(judgeMode="interactive", spjCode=TESTLIB_CHECKER, spjLanguage="C++")
    bundle, _, _ = _one(tmp_path, doc)
    assert _errors(bundle) == ["interactive_unsupported"]


def test_judge_mode_mismatch_follows_problem(tmp_path: Path) -> None:
    doc = hoj_problem_json(judgeMode="spj", spjCode=TESTLIB_CHECKER, spjLanguage="C++")
    doc["judgeMode"] = "default"
    bundle, built, _ = _one(tmp_path, doc)
    assert built["checker"] is not None
    mismatch = next(n for n in bundle.warnings if n.code == "judge_mode_mismatch")
    assert "spj" in mismatch.message
    # 只有顶层：按顶层
    (tmp_path / "b").mkdir()
    doc = hoj_problem_json(spjCode=TESTLIB_CHECKER, spjLanguage="C++")
    del doc["problem"]["judgeMode"]
    doc["judgeMode"] = "spj"
    bundle, built, _ = _one(tmp_path / "b", doc)
    assert built["checker"] is not None and "judge_mode_mismatch" not in _codes(bundle)


def test_remove_end_blank_off_warns(tmp_path: Path) -> None:
    bundle, _, _ = _one(tmp_path, hoj_problem_json(isRemoveEndBlank=False))
    assert "strict_compare_relaxed" in _codes(bundle)


def test_extra_files(tmp_path: Path) -> None:
    checker = '#include "testlib.h"\n#include "geo.h"\nint main(int c, char** v) { registerTestlibCmd(c, v); }\n'
    doc = hoj_problem_json(judgeMode="spj", spjCode=checker, spjLanguage="C++",
                           judgeExtraFile=json.dumps({"testlib.h": "// 包里的\n", "geo.h": "// geo\n"}))
    doc["userExtraFile"] = {"util.h": "// 选手用\n"}
    bundle, built, _ = _one(tmp_path, doc)
    assert built["support"] == ["geo.h", "testlib.h"]
    # 包里的 testlib.h 丢弃，建包时换成自带的那份
    assert sorted(bundle.checker_support) == ["geo.h"]
    assert "testlib_injected" in _codes(bundle)
    dropped = next(n for n in bundle.warnings if n.code == "extra_files_dropped")
    assert "util.h" in dropped.message


def test_user_extra_file_rescued_when_checker_includes_it(tmp_path: Path) -> None:
    """HOJ 导出 bug：判题附加文件被写进顶层 userExtraFile。被 checker 引用的 .h 仍收下。"""
    checker = '#include "testlib.h"\n#include "geo.h"\nint main(int c, char** v) { registerTestlibCmd(c, v); }\n'
    doc = hoj_problem_json(judgeMode="spj", spjCode=checker, spjLanguage="C++")
    doc["userExtraFile"] = {"geo.h": "// geo\n"}
    bundle, built, _ = _one(tmp_path, doc)
    assert sorted(bundle.checker_support) == ["geo.h"] and built["support"] == ["geo.h", "testlib.h"]
    assert "extra_files_dropped" not in _codes(bundle)


def test_bad_json_is_a_per_problem_error(tmp_path: Path) -> None:
    good = hoj_problem_json("OK")
    data = make_hoj_zip([good], extra={"problem_2.json": "{not json", "problem_2/1.in": "1\n"})
    result, _ = _parse(tmp_path, data, fmt="hoj")
    assert [(b.key, _errors(b)) for b in result.problems] == [("problem_1", []), ("problem_2", ["bad_manifest"])]


def test_forced_hoj_on_wrong_package(tmp_path: Path) -> None:
    with pytest.raises(ProblemIoError) as info:
        problem_io.parse_archive(stage(tmp_path, make_zip({"a/1.in": "1", "a/1.out": "1"})), "hoj")
    assert info.value.code == "bad_manifest"


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


class _Blobs:
    def __init__(self) -> None:
        self.data: Dict[str, bytes] = {}

    def add(self, data: bytes) -> str:
        digest = sha256_hex(data)
        self.data[digest] = data
        return digest

    def open(self, digest: str):
        if digest not in self.data:
            raise FileNotFoundError(digest)
        return io.BytesIO(self.data[digest])


def _problem(blobs: _Blobs, pid: int, ident: str, testdata: bytes, **columns) -> ExportProblem:
    return ExportProblem(id=pid, ident=ident, judge_mode="builtin", testdata_sha256=blobs.add(testdata),
                         testdata_name="t.zip", testdata_size=len(testdata), **columns)


def _export(problems: List[ExportProblem], blobs: _Blobs) -> Tuple[bytes, Dict[int, list]]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        notices = hoj.export(problems, blobs.open, zf)
    return buffer.getvalue(), notices


def _fixtures(blobs: _Blobs) -> List[ExportProblem]:
    bundled = make_testdata_zip(
        count=5, scores=[10, 10, 0, 0, 0],
        subtasks=[{"score": 30, "cases": ["3", "4"]}, {"score": 50, "cases": ["5"]}],
        checker=TESTLIB_CHECKER, support={"testlib.h": "// t\n", "geo.h": "// geo\n"},
    )
    return [
        _problem(blobs, 1, "sum", make_testdata_zip(count=3), title="求和", time_limit_ms=2000, memory_limit_mb=512,
                 io_mode="file", input_file="data.in", compile_flags="-std=c++17 -O2 -DLOCAL"),
        _problem(blobs, 2, "geo", bundled, title=None),
        _problem(blobs, 3, "plain", make_testdata_zip(count=2, checker=PLAIN_SPJ)),
    ]


def test_export_layout_and_fields() -> None:
    blobs = _Blobs()
    data, notices = _export(_fixtures(blobs), blobs)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        # 根目录只有 *.json 与数据目录
        assert {n.split("/")[0] for n in names} == {"problem_1.json", "problem_1", "problem_2.json", "problem_2",
                                                    "problem_3.json", "problem_3"}
        assert sorted(n for n in names if n.startswith("problem_2/")) == [
            "problem_2/%d.%s" % (i, ext) for i in range(1, 6) for ext in ("in", "out")]
        assert zf.read("problem_1/2.in") == b"2 20\n" and zf.read("problem_1/2.out") == b"22\n"
        first = json.loads(zf.read("problem_1.json"))
        second = json.loads(zf.read("problem_2.json"))
        third = json.loads(zf.read("problem_3.json"))

    p = first["problem"]
    assert (p["problemId"], p["title"], p["type"]) == ("SUM", "求和", 1)
    assert (p["timeLimit"], p["memoryLimit"], p["stackLimit"]) == (2000, 512, 512)
    assert (p["isFileIO"], p["ioReadFileName"], p["ioWriteFileName"]) == (True, "data.in", "sum.out")
    assert (p["judgeMode"], first["judgeMode"], p["judgeCaseMode"]) == ("default", "default", "default")
    assert "spjCode" not in p and p["isRemoveEndBlank"] is True
    assert p["ioScore"] == 100 and p["description"] == ""
    assert first["samples"] == [{"input": "%d.in" % i, "output": "%d.out" % i, "score": s}
                                for i, s in enumerate([33, 33, 34], 1)]
    assert first["languages"] == hoj.EXPORT_LANGUAGES and first["tags"] == [] and first["codeTemplates"] == []
    assert [n.code for n in notices[1]] == ["flags_dropped"] and "-DLOCAL" in notices[1][0].message
    assert "-O2" not in notices[1][0].message

    p = second["problem"]
    assert (p["problemId"], p["title"], p["isFileIO"]) == ("GEO", "geo", False)
    assert "ioReadFileName" not in p
    assert (p["judgeMode"], second["judgeMode"], p["spjLanguage"]) == ("spj", "spj", "C++")
    assert p["spjCode"] == TESTLIB_CHECKER
    assert p["judgeCaseMode"] == "subtask_lowest" and p["ioScore"] == 100
    # 子任务组内每点分值 = 子任务分；不在子任务里的点各自成组
    assert [(s["score"], s["groupNum"]) for s in second["samples"]] == [(10, 3), (10, 4), (30, 1), (30, 1), (50, 2)]
    assert second["judgeExtraFile"] == {"geo.h": "// geo\n"}
    assert json.loads(p["judgeExtraFile"]) == {"geo.h": "// geo\n"}
    assert [n.code for n in notices[2]] == ["hoj_extra_headers"] and "geo.h" in notices[2][0].message

    assert third["problem"]["spjCode"] == PLAIN_SPJ
    assert [n.code for n in notices[3]] == ["checker_not_testlib"]


def test_export_dry_run_writes_nothing_real() -> None:
    blobs = _Blobs()
    zf = NullZip()
    notices = hoj.export(_fixtures(blobs), blobs.open, zf)
    assert sorted(notices) == [1, 2, 3]
    assert "problem_2.json" in zf.names and "problem_2/5.out" in zf.names


def test_export_missing_blob_is_reported() -> None:
    blobs = _Blobs()
    problem = ExportProblem(id=7, ident="lost", judge_mode="builtin", testdata_sha256="0" * 64)
    _, notices = _export([problem], blobs)
    assert [n.code for n in notices[7]] == ["missing_file"]


def test_export_cxx20_languages() -> None:
    blobs = _Blobs()
    problem = _problem(blobs, 1, "a", make_testdata_zip(count=1), compile_flags="-std=c++20")
    data, notices = _export([problem], blobs)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "C++ 20" in json.loads(zf.read("problem_1.json"))["languages"]
    assert notices[1] == []


def _semantics(td_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(td_bytes)) as zf:
        td = parse_testdata(zf)
        in_sub = {name for sub in td.subtasks for name in sub.cases}
        cases = [(zf.read(c.input), zf.read(c.answer)) for c in td.cases]
        index = {c.name: i for i, c in enumerate(td.cases)}
        # 单点子任务与同分值普通测试点等价：都折算成"计分单元"
        units = sorted(
            [(s.score, tuple(sorted(index[n] for n in s.cases))) for s in td.subtasks if len(s.cases) > 1]
            + [(s.score, (index[s.cases[0]],)) for s in td.subtasks if len(s.cases) == 1]
            + [(c.score, (index[c.name],)) for c in td.cases if c.name not in in_sub]
        )
        checker = zf.read(td.checker) if td.checker else None
        headers = sorted(n.rsplit("/", 1)[-1] for n in td.checker_support if not n.endswith("testlib.h"))
    return {"cases": cases, "units": units, "total": td.total_score, "checker": checker, "headers": headers}


def test_export_then_import_round_trip(tmp_path: Path) -> None:
    blobs = _Blobs()
    problems = _fixtures(blobs)
    data, _ = _export(problems, blobs)
    with problem_io.parse_archive(stage(tmp_path, data)) as parsed:
        result = parsed.result
        assert result.format == "hoj"
        assert [b.errors for b in result.problems] == [[], [], []]
        by_key = {b.key: b for b in result.problems}
        for k, problem in enumerate(problems, 1):
            bundle = by_key["problem_%d" % k]
            assert bundle.title == (problem.title or problem.ident)
            assert (bundle.time_limit_ms or 1000, bundle.memory_limit_mb or 256) == (
                problem.effective_time_limit_ms, problem.effective_memory_limit_mb)
            assert bundle.io_mode == problem.io_mode
            if problem.io_mode == "file":
                assert (bundle.input_file or "%s.in" % bundle.ident) == problem.effective_input_file
                assert (bundle.output_file or "%s.out" % bundle.ident) == problem.effective_output_file
            else:
                assert bundle.ident == problem.ident
            assert _semantics(build_testdata_zip(bundle, parsed.archive)) == _semantics(blobs.data[problem.testdata_sha256])
            assert "stack_limit_dropped" not in _codes(bundle)
            assert "subtask_scores_uneven" not in _codes(bundle)


# --------------------------------------------------------------------------- #
# 接口：导出 → 导入另一场次
# --------------------------------------------------------------------------- #


def test_api_round_trip(client: TestClient, contest: dict, admin_headers: dict) -> None:
    from test_judge_export_api import export_zip, setup_problems
    from test_judge_import_api import ADMIN, accept_all, apply, make_contest, preview

    source = setup_problems(client, contest, admin_headers)
    body = client.get("%s/contests/%d/judge-export/preview?format=hoj" % (ADMIN, contest["id"]),
                      headers=admin_headers).json()
    assert [(p["ident"], p["included"]) for p in body["problems"]] == [("sum", True)]
    assert [w["code"] for w in body["problems"][0]["warnings"]] == []

    archive = export_zip(client, "%s/contests/%d/judge-export?format=hoj" % (ADMIN, contest["id"]), admin_headers)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name in archive.namelist():
            out.writestr(name, archive.read(name))

    target = make_contest(client, admin_headers, "hoj-target")
    body = preview(client, target, admin_headers, buffer.getvalue()).json()
    assert body["format"] == "hoj"
    (item,) = body["problems"]
    assert not item["errors"]
    result = apply(client, target, admin_headers, body["token"], accept_all(body)).json()
    assert result["created"] == 1
    (row,) = result["problems"]
    original = source["sum"]
    # 文件 IO 题的 ident 取输入文件主名（data.in → data），有效文件名不变
    assert row["ident"] == "data"
    assert (row["input_file"] or "data.in", row["output_file"]) == ("data.in", "sum.out")
    assert (row["time_limit_ms"], row["memory_limit_mb"], row["io_mode"]) == (2000, 512, "file")
    assert row["judge_mode"] == "builtin"
    assert row["testdata"]["cases"] == original["testdata"]["cases"]
    assert row["testdata"]["total_score"] == original["testdata"]["total_score"]
    assert row["testdata"]["has_checker"] is True


def test_infinite_numbers_do_not_crash(tmp_path: Path) -> None:
    doc = hoj_problem_json(timeLimit="1e999", memoryLimit=float("inf"))
    doc["samples"][0]["score"] = float("inf")  # json 写成 Infinity，Python 的 json 照收
    bundle, built, _ = _one(tmp_path, doc)
    assert _errors(bundle) == [] and built
    assert "limits_defaulted" in _codes(bundle)
    assert built["cases"][0][1] == 0
