"""Hydro 格式（含旧 config.ini 与 SYZOJ data.yml）的导入，以及 Hydro 导出与"导出 → 再导入"往返。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from syncoj_server.judging.testdata import parse_testdata
from syncoj_server.services import problem_io
from syncoj_server.services.problem_io import checkers, hydro, yaml_lite
from syncoj_server.services.problem_io.build import try_build
from syncoj_server.services.problem_io.bundle import ExportProblem, ImportResult, Notice, ProblemBundle

from problem_io_fixtures import make_hydro_zip, make_syzoj_zip, make_testdata_zip, sha256_hex, stage

from test_judge_export_api import export_zip, setup_problems
from test_judge_import_api import ADMIN, accept_all, apply, make_contest, preview

TESTLIB_CHECKER = '#include "testlib.h"\nint main(int argc, char* argv[]) { registerTestlibCmd(argc, argv); }\n'


def load(tmp_path: Path, data: bytes, fmt: str = "auto") -> Tuple[ImportResult, Dict[str, Optional[bytes]]]:
    """解析并为每道没有错误的题构建数据包；返回 (结果, {key: 数据包字节})。"""
    with problem_io.parse_archive(stage(tmp_path, data), fmt) as parsed:
        built: Dict[str, Optional[bytes]] = {}
        for bundle in parsed.result.problems:
            if not bundle.errors:
                out = try_build(bundle, parsed.archive)
                if out is None:
                    built[bundle.key] = None
                else:
                    with out:
                        built[bundle.key] = out.file.read()
        return parsed.result, built


def one(tmp_path: Path, data: bytes) -> Tuple[ProblemBundle, Optional[bytes]]:
    result, built = load(tmp_path, data)
    assert result.format == "hydro"
    (bundle,) = result.problems
    return bundle, built.get(bundle.key)


def codes(items: List[Notice]) -> List[str]:
    return [item.code for item in items]


def scores(bundle: ProblemBundle) -> Dict[str, int]:
    return {case.name: case.score for case in bundle.cases}


def subtasks(bundle: ProblemBundle) -> List[Tuple[int, List[str]]]:
    return [(sub.score, sub.cases) for sub in bundle.subtasks]


def data_for(*stems: str, out: str = ".out") -> Dict[str, str]:
    files: Dict[str, str] = {}
    for index, stem in enumerate(stems, 1):
        files[stem + ".in"] = "%d %d\n" % (index, index)
        files[stem + out] = "%d\n" % (2 * index)
    return files


def hydro_pkg(config: Optional[str], *stems: str, **kwargs: object) -> bytes:
    data = kwargs.pop("data", None) or data_for(*(stems or ("1", "2", "3")))
    return make_hydro_zip(config=config, data=data, **kwargs)  # type: ignore[arg-type]


def semantics(data: bytes):
    """数据包的语义：计分单元（分值 + 测试点内容）、checker 源码、支持文件名。与测试点命名无关。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parsed = parse_testdata(zf)
        content = {c.name: (zf.read(c.input), zf.read(c.answer)) for c in parsed.cases}
        inside = {name for sub in parsed.subtasks for name in sub.cases}
        units = [(sub.score, tuple(sorted(content[n] for n in sub.cases))) for sub in parsed.subtasks]
        units += [(c.score, (content[c.name],)) for c in parsed.cases if c.name not in inside]
        checker = zf.read(parsed.checker) if parsed.checker else None
        support = sorted(n.rsplit("/", 1)[-1] for n in parsed.checker_support)
    return sorted(units), checker, support


# --------------------------------------------------------------------------- #
# 包结构
# --------------------------------------------------------------------------- #


def test_default_package(tmp_path: Path) -> None:
    result, built = load(tmp_path, make_hydro_zip())
    (bundle,) = result.problems
    assert (bundle.key, bundle.ident, bundle.source_name, bundle.title) == ("1001", "P1001", "P1001", "A+B Problem")
    assert (bundle.time_limit_ms, bundle.memory_limit_mb, bundle.io_mode) == (1000, 256, "stdio")
    assert subtasks(bundle) == [(30, ["1-1", "1-2"])]
    assert scores(bundle) == {"1-1": 0, "1-2": 0, "2-1": 70}
    assert bundle.checker is None and not bundle.errors and not bundle.warnings
    assert codes(result.warnings) == ["statement_dropped"]
    assert built["1001"] is not None


@pytest.mark.parametrize("kwargs", [{"folder": ""}, {"shell": "wrap"}, {"folder": "", "shell": "wrap"}])
def test_single_problem_and_wrapper_folder(tmp_path: Path, kwargs: dict) -> None:
    bundle, data = one(tmp_path, make_hydro_zip(**kwargs))
    assert bundle.ident == "P1001" and len(bundle.cases) == 3 and data is not None


def test_several_problems_in_one_package(tmp_path: Path) -> None:
    first = make_hydro_zip(folder="1001")
    second = make_hydro_zip(folder="1002", problem_yaml="pid: P1002\ntitle: B\n")
    with zipfile.ZipFile(io.BytesIO(first)) as a, zipfile.ZipFile(io.BytesIO(second)) as b:
        files = {n: a.read(n) for n in a.namelist()}
        files.update({n: b.read(n) for n in b.namelist()})
    files["misc/readme.txt"] = b"not a problem"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    result, built = load(tmp_path, buffer.getvalue())
    assert [(b.key, b.ident, b.title) for b in result.problems] == [
        ("1001", "P1001", "A+B Problem"), ("1002", "P1002", "B")]
    assert all(built.values())


def test_broken_problem_yaml_is_only_a_warning(tmp_path: Path) -> None:
    bundle, data = one(tmp_path, make_hydro_zip(problem_yaml="title: [unclosed\n"))
    assert codes(bundle.warnings) == ["problem_yaml_ignored"]
    assert bundle.ident == "1001" and bundle.title is None and data is not None


def test_problem_yaml_limits_override_config(tmp_path: Path) -> None:
    yaml_text = "pid: P1\ntitle: t\nlimits:\n  time_limit: 2\n  memory: 512\n"
    bundle, _ = one(tmp_path, make_hydro_zip(problem_yaml=yaml_text))
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (2000, 512)


def test_testdata_subfolders_are_flattened_one_level(tmp_path: Path) -> None:
    data = {"sub/" + name: content for name, content in data_for("1-1", "1-2", "2-1").items()}
    bundle, built = one(tmp_path, make_hydro_zip(data=data))
    assert bundle.cases[0].input.member == "1001/testdata/sub/1-1.in"
    assert built is not None


def test_missing_data_file_is_an_error(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, make_hydro_zip(drop=["1001/testdata/2-1.out"]))
    assert codes(bundle.errors) == ["missing_file"]
    assert "1001/testdata/2-1.out" in bundle.errors[0].message


def test_yaml_error_names_file_and_line(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("time: 1s\nmemory: &m 256m\n"))
    assert codes(bundle.errors) == ["yaml_unsupported"]
    assert "1001/testdata/config.yaml" in bundle.errors[0].message and "第 2 行" in bundle.errors[0].message


# --------------------------------------------------------------------------- #
# 时限与内存
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value, expected", [
    ("1s", 1000), ("1000ms", 1000), ("1.5s", 1500), ("500us", 0), ("2500US", 2), (1500, 1500),
    ("1500", 1500), (1.5, 1), ("0.5s", 500),
])
def test_parse_time_ms(value: object, expected: int) -> None:
    assert hydro.parse_time_ms(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("256m", 256), ("1g", 1024), ("0.5g", 512), ("65536k", 64), ("1000k", 1), ("512mb", 512),
    (128, 128), ("128", 128), (1.5, 2),
])
def test_parse_memory_mb(value: object, expected: int) -> None:
    assert hydro.parse_memory_mb(value) == expected


@pytest.mark.parametrize("bad", ["fast", "1h", "", "1.5x"])
def test_unparseable_units(bad: str) -> None:
    with pytest.raises(ValueError):
        hydro.parse_time_ms(bad)
    with pytest.raises(ValueError):
        hydro.parse_memory_mb(bad)


@pytest.mark.parametrize("time, memory, want, clamped", [
    ("2s", "512m", (2000, 512), False),
    ("1500", "1g", (1500, 1024), False),
    ("500us", "256m", (100, 256), True),   # 500us → 0 ms，再夹到 100
    ("1s", "1000k", (1000, 16), True),     # 1000k → 1 MB，再夹到 16
])
def test_limits_in_config(tmp_path: Path, time: str, memory: str, want: tuple, clamped: bool) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("time: %s\nmemory: %s\n" % (time, memory)))
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == want
    assert ("limit_clamped" in codes(bundle.warnings)) is clamped


def test_unparseable_time_is_an_error(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("time: soon\n"))
    assert codes(bundle.errors) == ["bad_manifest"]


def test_subtask_and_case_limits_collapse_to_maximum(tmp_path: Path) -> None:
    config = """time: 1s
memory: 256m
subtasks:
  - score: 50
    time: 2s
    cases: [{input: 1.in, output: 1.out}]
  - score: 50
    cases: [{input: 2.in, output: 2.out, memory: 512m}, {input: 3.in, output: 3.out}]
"""
    bundle, _ = one(tmp_path, hydro_pkg(config))
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (2000, 512)
    assert "limits_collapsed" in codes(bundle.warnings)
    assert "2000 ms / 512 MB" in next(w.message for w in bundle.warnings if w.code == "limits_collapsed")


def test_uniform_subtask_limit_is_not_a_collapse(tmp_path: Path) -> None:
    config = "time: 1s\nsubtasks:\n  - time: 3s\n    cases: [{input: 1.in, output: 1.out}]\n"
    bundle, _ = one(tmp_path, hydro_pkg(config))
    assert bundle.time_limit_ms == 3000 and "limits_collapsed" not in codes(bundle.warnings)


# --------------------------------------------------------------------------- #
# 子任务与分值
# --------------------------------------------------------------------------- #


def test_sum_and_min_subtasks_with_missing_scores(tmp_path: Path) -> None:
    config = """subtasks:
  - score: 40
    type: sum
    cases:
      - {input: a1.in, output: a1.out, score: 10}
      - {input: a2.in, output: a2.out}
      - {input: a3.in, output: a3.out}
  - type: min
    cases:
      - {input: b1.in, output: b1.out}
      - {input: b2.in, output: b2.out}
"""
    bundle, built = one(tmp_path, hydro_pkg(config, "a1", "a2", "a3", "b1", "b2"))
    assert scores(bundle) == {"a1": 10, "a2": 15, "a3": 15, "b1": 0, "b2": 0}
    assert subtasks(bundle) == [(60, ["b1", "b2"])]
    assert not bundle.warnings
    units, _, _ = semantics(built)  # type: ignore[arg-type]
    assert sum(score for score, _ in units) == 100


def test_subtask_scores_fill_remainder_to_the_last(tmp_path: Path) -> None:
    config = "subtasks:\n" + "".join(
        "  - id: %d\n    cases: [{input: %d.in, output: %d.out}]\n" % (3 - i, i, i) for i in range(3))
    bundle, _ = one(tmp_path, hydro_pkg(config, "0", "1", "2"))
    # 按 id 排序：id1=2.in、id2=1.in、id3=0.in；100/3 → 33、33、34
    assert scores(bundle) == {"2": 33, "1": 33, "0": 34}
    assert [case.name for case in bundle.cases] == ["2", "1", "0"]


def test_max_subtask_is_bundled(tmp_path: Path) -> None:
    config = "subtasks:\n  - score: 100\n    type: max\n    cases: [{input: 1.in, output: 1.out}, {input: 2.in, output: 2.out}]\n"
    bundle, _ = one(tmp_path, hydro_pkg(config, "1", "2"))
    assert subtasks(bundle) == [(100, ["1", "2"])]
    assert codes(bundle.warnings) == ["max_as_bundle"]


def test_dependencies_are_dropped(tmp_path: Path) -> None:
    config = """subtasks:
  - {id: 1, score: 30, cases: [{input: 1.in, output: 1.out}]}
  - {id: 2, score: 70, if: [1], cases: [{input: 2.in, output: 2.out}, {input: 3.in, output: 3.out}]}
"""
    bundle, _ = one(tmp_path, hydro_pkg(config))
    assert scores(bundle)["1"] == 30 and subtasks(bundle) == [(70, ["2", "3"])]
    assert codes(bundle.warnings) == ["dependency_dropped"]


def test_uneven_case_scores_in_min_subtask(tmp_path: Path) -> None:
    config = "subtasks:\n  - score: 30\n    cases: [{input: 1.in, output: 1.out, score: 20}, {input: 2.in, output: 2.out}]\n"
    bundle, _ = one(tmp_path, hydro_pkg(config, "1", "2"))
    assert subtasks(bundle) == [(20, ["1", "2"])]
    assert codes(bundle.warnings) == ["subtask_scores_uneven"]


def test_flat_cases_are_one_sum_subtask(tmp_path: Path) -> None:
    config = """time: 1000ms
memory: 512m
cases:
  - {input: 1.in, output: 1.ans, score: 40}
  - {input: 2.in, output: 2.ans}
subtasks:
  - {score: 100, cases: [{input: 1.in, output: 1.ans}]}
"""
    bundle, _ = one(tmp_path, hydro_pkg(config, data=data_for("1", "2", out=".ans")))
    assert scores(bundle) == {"1": 40, "2": 60} and not bundle.subtasks  # cases 覆盖 subtasks
    assert bundle.memory_limit_mb == 512


def test_fractional_scores_are_truncated(tmp_path: Path) -> None:
    config = "cases:\n  - {input: 1.in, output: 1.out, score: 40.5}\n  - {input: 2.in, output: 2.out, score: 59.5}\n"
    bundle, _ = one(tmp_path, hydro_pkg(config, "1", "2"))
    assert scores(bundle) == {"1": 40, "2": 59}
    assert codes(bundle.warnings) == ["score_truncated"]


def test_cases_from_file_names_without_config(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, make_hydro_zip(config=None))
    # 1-1/1-2 → min 子任务 1；2-1 → 子任务 2（单点）；没写分 → 50/50
    assert subtasks(bundle) == [(50, ["1-1", "1-2"])]
    assert scores(bundle) == {"1-1": 0, "1-2": 0, "2-1": 50}

    bundle, _ = one(tmp_path, hydro_pkg(None, data=data_for("a1", "a2", "a10", out=".ans")))
    assert [case.name for case in bundle.cases] == ["a1", "a2", "a10"]
    assert scores(bundle) == {"a1": 33, "a2": 33, "a10": 34} and not bundle.subtasks


def test_config_without_cases_falls_back_to_file_names(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("time: 2s\nmemory: 128m\n", "1", "2"))
    assert scores(bundle) == {"1": 50, "2": 50}
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (2000, 128)


def test_config_ini(tmp_path: Path) -> None:
    ini = "3\n1.in|1.out|1|30|262144\n2.in|2.out|2|30|524288\n3.in|3.out|1|40|\n"
    data = {}
    for name, content in data_for("1", "2", "3").items():
        data[("input/" if name.endswith(".in") else "output/") + name] = content
    bundle, built = one(tmp_path, make_hydro_zip(config=ini, config_name="config.ini", data=data))
    assert scores(bundle) == {"1": 30, "2": 30, "3": 40}
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (2000, 512)
    assert codes(bundle.warnings) == ["limits_collapsed"]
    assert built is not None


def test_config_ini_count_mismatch(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, make_hydro_zip(config="3\n1.in|1.out|1|50|\n", config_name="config.ini",
                                             data=data_for("1")))
    assert codes(bundle.errors) == ["bad_manifest"]


def test_too_many_cases(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg(None, data=data_for(*("t%d" % i for i in range(501)))))
    assert codes(bundle.errors) == ["too_many_cases"]


# --------------------------------------------------------------------------- #
# 文件 IO、编译参数、题型
# --------------------------------------------------------------------------- #


def test_filename_means_file_io_and_ident(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("filename: aplusb\n"))
    assert (bundle.ident, bundle.source_name, bundle.io_mode) == ("aplusb", "P1001", "file")
    assert (bundle.input_file, bundle.output_file) == (None, None)  # 等于 <ident>.in/.out → 存 NULL


def test_langs_map_to_std_flag(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("langs: [c, cc, cc.cc14o2, cc.cc17o2, py.py3]\n"))
    assert bundle.compile_flags == "-std=c++17"
    assert codes(bundle.warnings) == ["languages_mapped"]
    bundle, _ = one(tmp_path, hydro_pkg("langs: [cc, py.py3]\n"))
    assert bundle.compile_flags is None and not bundle.warnings


@pytest.mark.parametrize("kind, code", [
    ("communication", "communication_unsupported"),
    ("objective", "problem_type_unsupported"),
    ("remote_judge", "problem_type_unsupported"),
])
def test_problem_types_are_rejected(tmp_path: Path, kind: str, code: str) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("type: %s\n" % kind))
    assert codes(bundle.errors) == [code]


def test_user_extra_files_without_main_are_rejected(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("user_extra_files: [grader.h]\n", extra={"1001/testdata/grader.h": "x"}))
    assert codes(bundle.errors) == ["grader_unsupported"]


def test_user_extra_files_with_main_map_to_grader(tmp_path: Path) -> None:
    extra = {"1001/testdata/grader.cpp": "#include \"add.h\"\nint main(){}", "1001/testdata/add.h": "int add();"}
    bundle, built = one(tmp_path, hydro_pkg("user_extra_files: [grader.cpp, add.h]\n", extra=extra))
    assert not bundle.errors and bundle.problem_type == "grader" and list(bundle.grader_files) == ["add.h"]
    with zipfile.ZipFile(io.BytesIO(built)) as zf:
        data = parse_testdata(zf)
    assert data.problem_type == "grader" and data.grader_files == ["add.h"]


def test_interactive_maps_to_interactor(tmp_path: Path) -> None:
    extra = {"1001/testdata/interactor.cc": "int main(){}"}
    bundle, built = one(tmp_path, hydro_pkg("type: interactive\ninteractor: interactor.cc\n", extra=extra))
    assert not bundle.errors and bundle.problem_type == "interactive" and bundle.interactor is not None
    with zipfile.ZipFile(io.BytesIO(built)) as zf:
        data = parse_testdata(zf)
    assert data.problem_type == "interactive" and data.interactor == "interactor.cpp"


def test_submit_answer_maps_to_answer_only(tmp_path: Path) -> None:
    bundle, built = one(tmp_path, hydro_pkg("type: submit_answer\n"))
    assert not bundle.errors and bundle.problem_type == "answer_only" and bundle.answer_files
    with zipfile.ZipFile(io.BytesIO(built)) as zf:
        data = parse_testdata(zf)
    assert data.problem_type == "answer_only"
    assert sorted(data.answer_names.values()) == sorted(bundle.answer_files.values())


# --------------------------------------------------------------------------- #
# checker
# --------------------------------------------------------------------------- #


def test_rcmp6_generates_real_checker(tmp_path: Path) -> None:
    bundle, built = one(tmp_path, hydro_pkg("checker_type: testlib\nchecker: rcmp6\n"))
    assert bundle.checker is not None and bundle.checker.data == checkers.real_checker(1e-6)
    assert codes(bundle.warnings) == ["real_checker_generated"]
    _, checker, support = semantics(built)  # type: ignore[arg-type]
    assert checker == checkers.real_checker(1e-6) and support == []


@pytest.mark.parametrize("name, eps, relative", [("rcmp4", 1e-4, True), ("rcmp9", 1e-9, True), ("rcmp", 1.5e-6, False)])
def test_other_real_checkers(tmp_path: Path, name: str, eps: float, relative: bool) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: testlib\nchecker: %s\n" % name))
    assert bundle.checker.data == checkers.real_checker(eps, relative)  # type: ignore[union-attr]
    assert ("只比绝对误差" in bundle.warnings[0].message) is (not relative)


@pytest.mark.parametrize("name", ["wcmp", "ncmp", "lcmp"])
def test_simple_builtin_checkers_fall_back_to_default(tmp_path: Path, name: str) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: testlib\nchecker: %s\n" % name))
    assert bundle.checker is None and codes(bundle.warnings) == ["builtin_checker_approx"]
    assert name in bundle.warnings[0].message


def test_unknown_builtin_checker(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: testlib\nchecker: yesno\n"))
    assert codes(bundle.errors) == ["checker_unknown"]


def test_strict_compare_maps_to_strict(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: strict\n"))
    assert bundle.checker is None and bundle.warnings == [] and bundle.compare_mode == "strict"


def test_lemon_checker_uses_lemon_protocol(tmp_path: Path) -> None:
    bundle, built = one(tmp_path, hydro_pkg("checker_type: lemon\nchecker: chk.cpp\n",
                                            extra={"1001/testdata/chk.cpp": "int main(){}"}))
    assert not bundle.errors and bundle.checker_protocol == "lemon"
    with zipfile.ZipFile(io.BytesIO(built)) as zf:
        assert parse_testdata(zf).checker_protocol == "lemon"


@pytest.mark.parametrize("kind", ["hustoj", "qduoj", "syzoj", "kattis"])
def test_foreign_checker_protocols_are_rejected(tmp_path: Path, kind: str) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: %s\nchecker: chk.cpp\n" % kind,
                                        extra={"1001/testdata/chk.cpp": "int main(){}"}))
    assert codes(bundle.errors) == ["checker_protocol_unsupported"]
    assert kind in bundle.errors[0].message


def test_testlib_checker_with_extra_headers(tmp_path: Path) -> None:
    config = "checker_type: testlib\nchecker: {file: chk.cc, lang: cc}\njudge_extra_files: [testlib.h, util.h, notes.txt]\n"
    extra = {
        "1001/testdata/chk.cc": TESTLIB_CHECKER,
        "1001/testdata/util.h": "// util\n",
        "1001/testdata/testlib.h": "// 包里的旧版 testlib\n",
        "1001/testdata/notes.txt": "x",
    }
    bundle, built = one(tmp_path, hydro_pkg(config, extra=extra))
    assert bundle.checker is not None and bundle.checker.member == "1001/testdata/chk.cc"
    assert sorted(bundle.checker_support) == ["util.h"]
    assert codes(bundle.warnings) == ["extra_files_dropped", "testlib_injected"]
    _, checker, support = semantics(built)  # type: ignore[arg-type]
    assert checker == TESTLIB_CHECKER.encode() and support == ["testlib.h", "util.h"]
    with zipfile.ZipFile(io.BytesIO(built)) as zf:  # type: ignore[arg-type]
        assert zf.read("testlib.h") == checkers.injected_testlib()


def test_checker_must_exist_and_be_cpp(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: testlib\nchecker: chk.cpp\n"))
    assert codes(bundle.errors) == ["checker_missing_source"]
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: testlib\nchecker: chk.py\n",
                                        extra={"1001/testdata/chk.py": "print()"}))
    assert codes(bundle.errors) == ["checker_language"]
    bundle, _ = one(tmp_path, hydro_pkg("checker_type: testlib\n"))
    assert codes(bundle.errors) == ["bad_manifest"]


def test_checker_ignored_without_testlib_type(tmp_path: Path) -> None:
    bundle, _ = one(tmp_path, hydro_pkg("checker: chk.cpp\n"))
    assert bundle.checker is None and not bundle.errors


# --------------------------------------------------------------------------- #
# SYZOJ data.yml
# --------------------------------------------------------------------------- #


def test_syzoj_data_yml(tmp_path: Path) -> None:
    bundle, built = one(tmp_path, make_syzoj_zip())
    assert subtasks(bundle) == [(30, ["data1", "data2"])]
    assert scores(bundle) == {"data1": 0, "data2": 0, "data3": 35, "data4": 35}
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (1000, 256)
    assert codes(bundle.warnings) == ["limits_defaulted"]
    assert built is not None


def test_syzoj_replaces_only_the_first_hash(tmp_path: Path) -> None:
    yml = "subtasks:\n  - score: 100\n    type: sum\n    cases: [1, 2]\ninputFile: 't#-#.in'\noutputFile: 't#-#.out'\n"
    data = {"t1-#.in": "1\n", "t1-#.out": "1\n", "t2-#.in": "2\n", "t2-#.out": "2\n"}
    bundle, built = one(tmp_path, make_syzoj_zip(data_yml=yml, data=data, folder="p1"))
    assert [case.input.member for case in bundle.cases] == ["p1/t1-#.in", "p1/t2-#.in"]
    assert scores(bundle) == {"t1-#": 50, "t2-#": 50} and built is not None


def test_syzoj_mul_and_unsupported(tmp_path: Path) -> None:
    yml = "subtasks:\n  - score: 100\n    type: mul\n    cases: [1, 2]\ninputFile: 'data#.in'\noutputFile: 'data#.out'\n"
    bundle, _ = one(tmp_path, make_syzoj_zip(data_yml=yml))
    assert subtasks(bundle) == [(100, ["data1", "data2"])]
    assert codes(bundle.warnings) == ["mul_as_bundle", "limits_defaulted"]

    spj = "specialJudge:\n  language: cpp11\n  fileName: spj_cpp11.cpp\ninputFile: 'data#.in'\n"
    bundle, _ = one(tmp_path, make_syzoj_zip(data_yml=spj))
    assert codes(bundle.errors) == ["checker_protocol_unsupported"]

    yml = "subtasks:\n  - score: 100\n    type: sum\n    cases: [1]\ninputFile: 'data#.in'\noutputFile: '-'\n"
    bundle, _ = one(tmp_path, make_syzoj_zip(data_yml=yml))
    assert codes(bundle.errors) == ["missing_answer"]


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #

SUPPORTED_TESTDATA = make_testdata_zip(
    count=4, scores=[20, 20, 0, 0], subtasks=[{"score": 60, "cases": ["3", "4"]}],
    checker=TESTLIB_CHECKER, support={"testlib.h": "// t\n", "util.h": "// util\n"},
)


def problem(ident: str, data: bytes, pid: int = 1, **columns: object) -> ExportProblem:
    values: Dict[str, object] = {"title": "题目 " + ident, "judge_mode": "builtin", "time_limit_ms": 2000,
                                 "memory_limit_mb": 512}
    values.update(columns)
    return ExportProblem(id=pid, ident=ident, testdata_sha256=sha256_hex(data), testdata_name="t.zip",
                         testdata_size=len(data), **values)  # type: ignore[arg-type]


def export(problems: List[ExportProblem], blobs: List[bytes], dry_run: bool = False):
    store = {sha256_hex(data): data for data in blobs}
    outcome = problem_io.export_problems(problems, "hydro", lambda sha: io.BytesIO(store[sha]), dry_run=dry_run)
    body = None if dry_run else outcome.spool.read()  # type: ignore[union-attr]
    return body, {pid: codes(items) for pid, items in outcome.notices.items()}


def test_export_layout(tmp_path: Path) -> None:
    body, notices = export([problem("sum", SUPPORTED_TESTDATA)], [SUPPORTED_TESTDATA])
    assert notices == {1: []}
    with zipfile.ZipFile(io.BytesIO(body)) as zf:  # type: ignore[arg-type]
        names = sorted(zf.namelist())
        meta = yaml_lite.load(zf.read("sum/problem.yaml"))
        config = yaml_lite.load(zf.read("sum/testdata/config.yaml"))
        checker = zf.read("sum/testdata/checker.cpp")
    assert names == sorted(
        ["sum/problem.yaml", "sum/testdata/config.yaml", "sum/testdata/checker.cpp", "sum/testdata/util.h"]
        + ["sum/testdata/%d.%s" % (i, ext) for i in range(1, 5) for ext in ("in", "out")]
    )
    assert meta == {"pid": "sum", "title": "题目 sum", "tag": []}
    assert config == {
        "type": "default", "time": "2000ms", "memory": "512m",
        "checker_type": "testlib", "checker": "checker.cpp", "judge_extra_files": ["util.h"],
        "subtasks": [
            {"id": 1, "score": 20, "type": "min", "cases": [{"input": "1.in", "output": "1.out"}]},
            {"id": 2, "score": 20, "type": "min", "cases": [{"input": "2.in", "output": "2.out"}]},
            {"id": 3, "score": 60, "type": "min",
             "cases": [{"input": "3.in", "output": "3.out"}, {"input": "4.in", "output": "4.out"}]},
        ],
    }
    assert checker == TESTLIB_CHECKER.encode()


def test_export_file_io_and_warnings() -> None:
    plain = make_testdata_zip(count=3, scores=[30, 30, 30])
    many = make_testdata_zip(count=101)
    problems = [
        problem("aplusb", plain, 1, io_mode="file"),
        problem("a_b", plain, 2, io_mode="file", input_file="data.in", output_file="data.out"),
        problem("2sum", plain, 3, io_mode="file", input_file="in.txt", compile_flags="-std=c++17 -O2"),
        problem("many", many, 4, time_limit_ms=None, memory_limit_mb=None),
    ]
    body, notices = export(problems, [plain, many])
    assert notices == {
        1: ["hydro_total_not_100"],
        2: ["pid_omitted", "hydro_total_not_100"],
        3: ["pid_omitted", "io_names_lost", "flags_dropped", "hydro_total_not_100"],
        4: ["hydro_case_limit", "hydro_total_time"],
    }
    with zipfile.ZipFile(io.BytesIO(body)) as zf:  # type: ignore[arg-type]
        configs = {p.ident: yaml_lite.load(zf.read("%s/testdata/config.yaml" % p.ident)) for p in problems}
        assert "pid" not in yaml_lite.load(zf.read("a_b/problem.yaml"))
    assert configs["aplusb"]["filename"] == "aplusb"
    assert configs["a_b"]["filename"] == "data"
    assert configs["2sum"]["filename"] == "2sum"
    assert "filename" not in configs["many"] and "checker" not in configs["many"]
    assert (configs["many"]["time"], configs["many"]["memory"]) == ("1000ms", "256m")


def test_export_dry_run_gives_the_same_warnings() -> None:
    problems = [problem("a_b", SUPPORTED_TESTDATA, compile_flags="-O2")]
    _, real = export(problems, [SUPPORTED_TESTDATA])
    _, dry = export(problems, [SUPPORTED_TESTDATA], dry_run=True)
    assert real == dry == {1: ["pid_omitted", "flags_dropped"]}


def test_export_flattens_nested_case_names(tmp_path: Path) -> None:
    files = {"g1/1.in": "1\n", "g1/1.ans": "1\n", "g2/1.in": "2\n", "g2/1.ans": "2\n"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    data = buffer.getvalue()
    body, _ = export([problem("nest", data)], [data])
    with zipfile.ZipFile(io.BytesIO(body)) as zf:  # type: ignore[arg-type]
        assert sorted(n for n in zf.namelist() if n.endswith(".in")) == ["nest/testdata/g1_1.in", "nest/testdata/g2_1.in"]
    result, built = load(tmp_path, body)  # type: ignore[arg-type]
    assert semantics(built["nest"]) == semantics(data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 导出 → 再导入
# --------------------------------------------------------------------------- #

ROUND_TRIP_SHAPES = {
    "plain": make_testdata_zip(count=3),
    "scored": make_testdata_zip(count=4, scores=[10, 20, 30, 40]),
    "bundled": make_testdata_zip(count=5, scores=[10, 10, 0, 0, 0], subtasks=[
        {"score": 50, "cases": ["3", "4"]}, {"score": 30, "cases": ["5"]}]),
    "checker": SUPPORTED_TESTDATA,
}


@pytest.mark.parametrize("shape", sorted(ROUND_TRIP_SHAPES))
@pytest.mark.parametrize("io_columns", [
    {"io_mode": "stdio"},
    {"io_mode": "file"},
    {"io_mode": "file", "input_file": "game.in", "output_file": "game.out"},
])
def test_round_trip_keeps_semantics(tmp_path: Path, shape: str, io_columns: dict) -> None:
    data = ROUND_TRIP_SHAPES[shape]
    original = problem("prob" + shape, data, time_limit_ms=1500, memory_limit_mb=128, **io_columns)
    body, _ = export([original], [data])
    result, built = load(tmp_path, body)  # type: ignore[arg-type]
    assert result.format == "hydro"
    (bundle,) = result.problems
    assert not bundle.errors, bundle.errors
    assert bundle.title == original.title
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (1500, 128)
    assert bundle.io_mode == original.io_mode
    if original.io_mode == "file":
        # 文件名保持；ident 按 Hydro 规则取 filename
        assert (bundle.input_file or bundle.ident + ".in") == original.effective_input_file
        assert (bundle.output_file or bundle.ident + ".out") == original.effective_output_file
    else:
        assert bundle.ident == original.ident
    got_units, got_checker, got_support = semantics(built[bundle.key])  # type: ignore[arg-type]
    want_units, want_checker, want_support = semantics(data)
    assert got_units == want_units and got_checker == want_checker
    assert got_support == want_support  # testlib.h 没导出，再导入时补上随仓库的那份
    assert [w for w in codes(bundle.warnings) if w != "testlib_injected"] == []


def test_round_trip_through_the_api(client: TestClient, contest: dict, admin_headers: dict) -> None:
    problems = setup_problems(client, contest, admin_headers)
    url = "%s/contests/%d/judge-export/preview?format=hydro" % (ADMIN, contest["id"])
    body = client.get(url, headers=admin_headers).json()
    assert [(p["ident"], [w["code"] for w in p["warnings"]]) for p in body["problems"]] == [
        ("sum", ["io_names_lost", "flags_dropped"])]
    url = "%s/problems/%d/judge-export?format=hydro" % (ADMIN, problems["sum"]["id"])
    archive = export_zip(client, url, admin_headers)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name in archive.namelist():
            out.writestr(name, archive.read(name))

    target = make_contest(client, admin_headers, "hydro-trip")
    body = preview(client, target, admin_headers, buffer.getvalue()).json()
    assert body["format"] == "hydro"
    (row,) = body["problems"]
    assert (row["ident"], row["action"], row["errors"]) == ("sum", "create", [])
    result = apply(client, target, admin_headers, body["token"], accept_all(body)).json()
    (made,) = result["problems"]
    assert (made["time_limit_ms"], made["memory_limit_mb"], made["io_mode"]) == (2000, 512, "file")
    assert made["judge_mode"] == "builtin" and made["compile_flags"] is None
    response = client.get("%s/problems/%d/testdata/file" % (ADMIN, made["id"]), headers=admin_headers)
    assert response.status_code == 200
    assert semantics(response.content) == semantics(problems["sum"]["_zip"])


_HYDRO_RULE1 = r"^(([\w.-]*?)(?:(\d*)[-_])?(\d+))\.(in|IN|txt|TXT|in\.txt|IN\.TXT)$"


@pytest.mark.parametrize("name", [
    "1.in", "a10.in", "1-2.in", "a1_23.in", "ab12.txt", "x1.in.txt", "X1.IN.TXT", "_5.in", "-5.in",
    "1.2.in", "a-b1.in", "1-.in", "a.in", "1.in.TXT", "1.inx", "a b1.in", "1__2.in", "07-003.IN",
])
def test_auto_rule_matches_hydro_regex(name: str) -> None:
    import re

    want = re.match(_HYDRO_RULE1, name, re.A)
    got = hydro._rule1_match(name)
    assert (want is None) == (got is None)
    if want is not None:
        assert [got.group(i) for i in range(1, 6)] == list(want.groups())


def test_auto_rule_is_linear() -> None:
    """Hydro 原正则对长数字串加不匹配的结尾会二次方回溯；这里必须是线性的。"""
    import time

    start = time.monotonic()
    for name in ("1" * 200000 + ".inx", "1" * 200000 + "a1.in", "1_" * 100000 + ".inx"):
        hydro._rule1_match(name)
    assert time.monotonic() - start < 1.0


def test_config_ini_count_over_limit_fails_before_building(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config.ini 首行的测试点数超限：直接报 too_many_cases，不逐行建测试点。"""
    made = []
    original = hydro._RawSubtask
    monkeypatch.setattr(hydro, "_RawSubtask", lambda *a, **k: made.append(1) or original(*a, **k))
    ini = "600000\n" + "a|b|1|1|1\n" * 600000
    bundle, _ = one(tmp_path, make_hydro_zip(config=ini, config_name="config.ini", data=data_for("1")))
    assert codes(bundle.errors) == ["too_many_cases"]
    assert "600000" in bundle.errors[0].message
    assert made == [] and bundle.cases == []


def test_yaml_cases_over_limit_fail_before_normalizing(tmp_path: Path) -> None:
    cases = "".join("  - {input: a.in, output: a.out}\n" for _ in range(501))
    bundle, _ = one(tmp_path, hydro_pkg("cases:\n" + cases, data=data_for("a")))
    assert codes(bundle.errors) == ["too_many_cases"]
    assert bundle.cases == [] and bundle.subtasks == []


def test_too_many_problem_dirs_fail_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from problem_io_fixtures import make_zip

    parsed = []
    monkeypatch.setattr(hydro, "_parse_problem", lambda *args: parsed.append(1))
    data = make_zip({"%d/testdata/config.yaml" % i: "" for i in range(201)})
    with pytest.raises(problem_io.ProblemIoError) as info:
        problem_io.parse_archive(stage(tmp_path, data))
    assert info.value.code == "too_many_problems" and parsed == []


@pytest.mark.parametrize("config, code", [
    ("time: 1e999\n", "bad_manifest"),
    ("time: '%ss'\n" % ("9" * 400), "bad_manifest"),
    ("memory: '%sg'\n" % ("9" * 400), "bad_manifest"),
    ("time: 1e999\nmemory: 1e999\n", "bad_manifest"),
])
def test_infinite_limits_are_bad_manifest(tmp_path: Path, config: str, code: str) -> None:
    bundle, _ = one(tmp_path, hydro_pkg(config))
    assert codes(bundle.errors) == [code]


def test_infinite_subtask_score_counts_as_unset(tmp_path: Path) -> None:
    config = ("subtasks:\n  - score: 1e400\n    cases: [{input: 1.in, output: 1.out}]\n"
              "  - score: 40\n    cases: [{input: 2.in, output: 2.out}]\n")
    bundle, built = one(tmp_path, hydro_pkg(config, "1", "2"))
    assert bundle.errors == [] and built is not None
    assert scores(bundle) == {"1": 60, "2": 40}


def test_infinite_limits_through_api(client: TestClient, contest: dict, admin_headers: dict) -> None:
    data = hydro_pkg("time: 1e999\n")
    response = client.post(
        "/api/v1/admin/contests/%d/judge-import/preview" % contest["id"],
        headers=admin_headers, files={"file": ("h.zip", data, "application/zip")},
    )
    assert response.status_code == 200, response.text
    (item,) = response.json()["problems"]
    assert item["action"] == "skip" and [e["code"] for e in item["errors"]] == ["bad_manifest"]


def test_reused_case_name_does_not_clash_with_real_file(tmp_path: Path) -> None:
    """子任务之间复用 a：第二次出现生成的 a-2 不能和真实的 a-2.in 撞名。"""
    config = ("subtasks:\n"
              "  - score: 40\n    cases: [{input: a.in, output: a.out}]\n"
              "  - score: 60\n    cases: [{input: a.in, output: a.out}, {input: a-2.in, output: a-2.out}]\n")
    bundle, built = one(tmp_path, hydro_pkg(config, data=data_for("a", "a-2")))
    assert bundle.errors == [] and built is not None
    names = [case.name for case in bundle.cases]
    assert len(set(n.lower() for n in names)) == 3 and names[:2] == ["a", "a-2"]


def test_export_warns_when_zero_score_units_get_filled(tmp_path: Path) -> None:
    """满分不到 100 时，Hydro 会把 0 分的单元当成没写分、补上剩余分：导出要说清这个后果。"""
    data = make_testdata_zip(count=3, scores=[0, 30, 30])
    body, notices = export([problem("zero", data)], [data])
    assert notices == {1: ["hydro_zero_score_filled"]}
    result, built = load(tmp_path, body)  # type: ignore[arg-type]
    (bundle,) = result.problems
    assert {c.name: c.score for c in bundle.cases}["1"] == 40  # 正是警告里说的后果
    full = make_testdata_zip(count=3, scores=[0, 50, 50])
    assert export([problem("full", full)], [full])[1] == {1: []}  # 满分 100 时 0 分点仍是 0 分


def test_export_warns_about_total_time_limit() -> None:
    data = make_testdata_zip(count=25)
    _, notices = export([problem("slow", data, time_limit_ms=3000)], [data])
    assert notices == {1: ["hydro_total_time"]}
    _, notices = export([problem("ok", data, time_limit_ms=2400)], [data])
    assert notices == {1: []}


def test_every_uneven_subtask_is_named(tmp_path: Path) -> None:
    config = ("subtasks:\n"
              "  - {score: 30, type: max, cases: [{input: 1.in, output: 1.out, score: 10},"
              " {input: 2.in, output: 2.out, score: 20}]}\n"
              "  - {score: 70, type: max, cases: [{input: 3.in, output: 3.out, score: 30},"
              " {input: 4.in, output: 4.out, score: 40}]}\n")
    bundle, _ = one(tmp_path, hydro_pkg(config, "1", "2", "3", "4"))
    (item,) = [n for n in bundle.warnings if n.code == "subtask_scores_uneven"]
    assert item.message == "子任务 1、2 内分值不一致，取最小值 10、30"


def test_root_level_package_is_named_after_the_upload(tmp_path: Path) -> None:
    with problem_io.parse_archive(stage(tmp_path, make_syzoj_zip()), "auto", "syzoj-pkg.zip") as parsed:
        (bundle,) = parsed.result.problems
    assert (bundle.key, bundle.source_name, bundle.ident) == (".", "syzoj-pkg", "syzoj-pkg")
    item = [n for n in bundle.warnings if n.code == "limits_defaulted"][0]
    assert item.message == "时限/内存未知，已设为 1000 ms / 256 MB"
