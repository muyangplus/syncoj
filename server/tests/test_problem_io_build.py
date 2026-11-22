"""bundle → 数据包（build.py）与公共规整（finalize）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from syncoj_server.judging import testdata as testdata_mod
from syncoj_server.judging.testdata import parse_testdata
from syncoj_server.services.problem_io import finalize
from syncoj_server.services.problem_io.archive import SafeArchive
from syncoj_server.services.problem_io.build import build_testdata_zip, flat_case_names, try_build
from syncoj_server.services.problem_io.bundle import (
    BundleCase,
    BundleSubtask,
    FileRef,
    ImportResult,
    ProblemBundle,
)
from syncoj_server.services.problem_io.notices import MESSAGES, ProblemIoError, notice, register

from problem_io_fixtures import make_zip, stage

def _case(name: str, inp: str = "1 2\n", ans: str = "3\n", score: int = 0) -> BundleCase:
    return BundleCase(name=name, input=FileRef(data=inp.encode()), answer=FileRef(data=ans.encode()), score=score)


def _parse(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return parse_testdata(zf), {n: zf.read(n) for n in zf.namelist()}


# --------------------------------------------------------------------------- #
# build_testdata_zip
# --------------------------------------------------------------------------- #


def test_cases_scores_subtasks_and_checker_survive_a_round_trip() -> None:
    bundle = ProblemBundle(
        key="k",
        ident="sum",
        cases=[_case("1", score=10), _case("2", score=0), _case("3", score=0), _case("4", score=20)],
        subtasks=[BundleSubtask(score=70, cases=["2", "3"])],
        checker=FileRef(data=b"int main(){return 0;}\n"),
        checker_support={"helper.h": FileRef(data=b"// helper\n")},
    )
    parsed, files = _parse(build_testdata_zip(bundle, None))
    assert [(c.name, c.score) for c in parsed.cases] == [("1", 10), ("2", 0), ("3", 0), ("4", 20)]
    assert [(s.score, s.cases) for s in parsed.subtasks] == [(70, ["2", "3"])]
    assert parsed.checker == "checker.cpp"
    assert parsed.checker_support == ["helper.h"]
    assert parsed.total_score == 100
    assert files["1.in"] == b"1 2\n" and files["1.ans"] == b"3\n"


def test_case_names_are_flattened_and_deduplicated() -> None:
    names = flat_case_names(["a/1", "a_1", "b c", "A_1", "..x", "中文"])
    assert names == {"a/1": "a_1", "a_1": "a_1-2", "b c": "b_c", "A_1": "A_1-3", "..x": "x", "中文": "__"}
    bundle = ProblemBundle(
        key="k", ident="x",
        cases=[_case("a/1", score=50), _case("a_1", score=50)],
    )
    parsed, _ = _parse(build_testdata_zip(bundle, None))
    assert sorted(c.name for c in parsed.cases) == ["a_1", "a_1-2"]


def test_member_refs_are_read_from_the_archive(tmp_path: Path) -> None:
    path = stage(tmp_path, make_zip({"pkg/data/1.txt": "5 6\n", "pkg/data/1.res": "11\n"}))
    bundle = ProblemBundle(
        key="k", ident="x",
        cases=[BundleCase("1", FileRef(member="pkg/data/1.txt"), FileRef(member="pkg/data/1.res"), 100)],
    )
    with SafeArchive(path) as archive:
        _parsed, files = _parse(build_testdata_zip(bundle, archive))
    assert files["1.in"] == b"5 6\n" and files["1.ans"] == b"11\n"


def test_missing_member_becomes_a_testdata_error(tmp_path: Path) -> None:
    path = stage(tmp_path, make_zip({"1.in": "1"}))
    bundle = ProblemBundle(
        key="k", ident="x", cases=[BundleCase("1", FileRef(member="1.in"), FileRef(member="1.out"), 100)],
    )
    with SafeArchive(path) as archive:
        with pytest.raises(testdata_mod.TestdataError):
            build_testdata_zip(bundle, archive)
        assert try_build(bundle, archive) is None
    assert [e.code for e in bundle.errors] == ["testdata_invalid"]


def test_unknown_subtask_member_is_an_error() -> None:
    bundle = ProblemBundle(key="k", ident="x", cases=[_case("1")], subtasks=[BundleSubtask(100, ["9"])])
    with pytest.raises(testdata_mod.TestdataError):
        build_testdata_zip(bundle, None)


def test_raw_testdata_is_returned_byte_for_byte() -> None:
    raw = make_zip({"1.in": "1", "1.ans": "1"})
    bundle = ProblemBundle(key="k", ident="x", raw_testdata=FileRef(data=raw))
    assert build_testdata_zip(bundle, None) == raw


# --------------------------------------------------------------------------- #
# finalize：公共规整
# --------------------------------------------------------------------------- #


def _result(*bundles: ProblemBundle, fmt: str = "hoj") -> ImportResult:
    return ImportResult(format=fmt, problems=list(bundles))


def test_idents_are_normalised_and_deduplicated() -> None:
    a = ProblemBundle(key="1", ident="P1000", cases=[_case("1")])
    b = ProblemBundle(key="2", ident="p1000", cases=[_case("1")])
    c = ProblemBundle(key="3", ident="Ａ＋Ｂ 问题", cases=[_case("1")])
    d = ProblemBundle(key="4", ident="", source_name="", cases=[_case("1")])
    finalize(_result(a, b, c, d))
    assert [x.ident for x in (a, b, c, d)] == ["P1000", "p1000-2", "A_B", "p4"]
    assert a.warnings == [] or all(w.code != "ident_renamed" for w in a.warnings)
    assert any(w.code == "ident_renamed" for w in b.warnings)
    assert any(w.code == "ident_renamed" for w in c.warnings)


def test_limits_are_clamped() -> None:
    bundle = ProblemBundle(key="1", ident="x", time_limit_ms=0, memory_limit_mb=99999, cases=[_case("1")])
    finalize(_result(bundle))
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (100, 4096)
    assert [w.code for w in bundle.warnings if w.code == "limit_clamped"] == ["limit_clamped"]


def test_missing_scores_split_100_over_scoring_units() -> None:
    bundle = ProblemBundle(
        key="1", ident="x",
        cases=[_case("1"), _case("2"), _case("3"), _case("4")],
        subtasks=[BundleSubtask(0, ["3", "4"])],
    )
    finalize(_result(bundle))
    # 计分单元按「子任务在前、散点在后」排，余数给最后几个单元（与数据包默认分配一致）
    assert [c.score for c in bundle.cases] == [33, 34, 0, 0]
    assert bundle.subtasks[0].score == 33
    assert any(w.code == "scores_missing" for w in bundle.warnings)
    parsed, _ = _parse(build_testdata_zip(bundle, None))
    assert parsed.total_score == 100


def test_io_names() -> None:
    same = ProblemBundle(key="1", ident="aplusb", io_mode="file", input_file="aplusb.in", output_file="x.out", cases=[_case("1")])
    bad = ProblemBundle(key="2", ident="b", io_mode="file", input_file="in.txt", cases=[_case("1")])
    clash = ProblemBundle(key="3", ident="c", io_mode="file", input_file="c.io", output_file="c.io", cases=[_case("1")])
    stdio = ProblemBundle(key="4", ident="d", io_mode="stdio", input_file="d.in", cases=[_case("1")])
    finalize(_result(same, bad, clash, stdio))
    assert (same.input_file, same.output_file) == (None, "x.out")  # 等于默认名 → NULL
    assert [e.code for e in bad.errors] == ["io_name_invalid"]
    assert [e.code for e in clash.errors] == ["io_name_invalid"]
    assert (stdio.input_file, stdio.output_file) == (None, None)


def test_compile_flags_whitelist() -> None:
    bundle = ProblemBundle(key="1", ident="x", compile_flags="-std=c++17 -O2 -fplugin=evil.so -I/etc", cases=[_case("1")])
    finalize(_result(bundle))
    assert bundle.compile_flags == "-std=c++17 -O2"
    assert any(w.code == "flags_dropped" for w in bundle.warnings)


def test_non_native_import_is_always_builtin_and_needs_cases() -> None:
    bundle = ProblemBundle(key="1", ident="x", judge_mode="external")
    finalize(_result(bundle))
    assert bundle.judge_mode == "builtin"
    assert [e.code for e in bundle.errors] == ["no_cases"]


def test_empty_import_is_rejected() -> None:
    with pytest.raises(ProblemIoError) as info:
        finalize(_result())
    assert info.value.code == "no_problems"


# --------------------------------------------------------------------------- #
# notices
# --------------------------------------------------------------------------- #


def test_notice_formats_its_template() -> None:
    item = notice("limits_collapsed", time=2000, memory=512)
    assert item.message == "各测试点时限/内存不同，已统一为 2000 ms / 512 MB"
    with pytest.raises(KeyError):
        notice("no_such_code")


def test_register_rejects_a_conflicting_message() -> None:
    register({"dependency_dropped": MESSAGES["dependency_dropped"]})  # 同文案：无事
    with pytest.raises(ValueError):
        register({"dependency_dropped": "别的文案"})


def test_messages_do_not_explain() -> None:
    """界面文案只写后果与数据：不出现"因为 / 为了 / 建议 / 注意"这类解释句。"""
    for code, text in MESSAGES.items():
        for word in ("因为", "为了", "建议", "注意", "可以"):
            assert word not in text, (code, text)


