"""bundle → 数据包（build.py）、公共规整（finalize）与生成的 checker（checkers.py）。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict

import pytest

from syncoj_server.judging import testdata as testdata_mod
from syncoj_server.judging.testdata import parse_testdata
from syncoj_server.services.problem_io import checkers, finalize
from syncoj_server.services.problem_io.archive import SafeArchive
from syncoj_server.services.problem_io.build import (
    build_testdata_zip,
    flat_case_names,
    try_build,
    try_summarize,
)
from syncoj_server.services.problem_io.bundle import (
    BundleCase,
    BundleSubtask,
    FileRef,
    ImportResult,
    ProblemBundle,
)
from syncoj_server.services.problem_io.notices import MESSAGES, ProblemIoError, notice, register

from problem_io_fixtures import make_zip, stage

GXX = shutil.which("g++")
needs_gxx = pytest.mark.skipif(GXX is None, reason="没有 g++")

TESTLIB_CHECKER = """#include "testlib.h"
int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);
    long long want = ans.readLong(), got = ouf.readLong();
    if (want != got) quitf(_wa, "expected %lld, found %lld", want, got);
    quitf(_ok, "ok");
}
"""


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


def test_testlib_is_injected_when_missing() -> None:
    bundle = ProblemBundle(key="k", ident="x", cases=[_case("1", score=100)], checker=FileRef(data=TESTLIB_CHECKER.encode()))
    _parsed, files = _parse(build_testdata_zip(bundle, None))
    assert files["testlib.h"] == checkers.injected_testlib()
    assert [w.code for w in bundle.warnings] == ["testlib_injected"]
    # 包里自带了就不注入
    own = ProblemBundle(
        key="k", ident="x", cases=[_case("1", score=100)], checker=FileRef(data=TESTLIB_CHECKER.encode()),
        checker_support={"testlib.h": FileRef(data=b"// own\n")},
    )
    _parsed, files = _parse(build_testdata_zip(own, None))
    assert files["testlib.h"] == b"// own\n" and own.warnings == []


def test_missing_vendor_testlib_gives_a_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(checkers, "VENDOR_DIR", tmp_path / "nowhere")
    support: Dict[str, FileRef] = {}
    got = checkers.inject_testlib(TESTLIB_CHECKER.encode(), support)
    assert got is not None and got.code == "testlib_missing" and support == {}
    assert checkers.inject_testlib(b"int main(){}", support) is None


def test_vendor_testlib_and_license_ship_with_the_package() -> None:
    data = checkers.vendor_testlib()
    assert data is not None and b"_TESTLIB_H_" in data
    assert (checkers.VENDOR_DIR / "LICENSE-testlib").read_text().startswith("The MIT License")
    # 仓库里的 vendor/testlib.h 与上游逐字节一致；许可声明只加在注入的副本上
    assert hashlib.sha256(data).hexdigest() == "d92ca8a6b3690d34d5c56c9394d99798130e21cb0096db817d91e265f8d958c3"


def test_injected_testlib_carries_the_license_notice() -> None:
    """注入的副本随数据包 / 导出包分发：最前面是注明来源、版本与 MIT 全文的注释，之后是上游原文。"""
    upstream = checkers.vendor_testlib()
    data = checkers.injected_testlib()
    assert upstream is not None and data is not None
    assert data.startswith(b"/*\n * testlib.h, bundled with this problem's test data by SyncOJ.\n")
    head, rest = data.split(b"*/\n", 1)
    assert rest == upstream
    assert b"https://github.com/MikeMirzayanov/testlib" in head
    assert b"Version: 0.9.51" in head
    assert b"Copyright (c) 2015 Mike Mirzayanov" in head
    assert b"Permission is hereby granted" in head and b"THE SOFTWARE IS PROVIDED" in head
    head.decode("ascii")  # 注释纯 ASCII


def test_missing_testlib_license_counts_as_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "testlib.h").write_bytes(checkers.vendor_testlib() or b"")
    monkeypatch.setattr(checkers, "VENDOR_DIR", tmp_path)
    assert checkers.injected_testlib() is None
    got = checkers.inject_testlib(TESTLIB_CHECKER.encode(), {})
    assert got is not None and got.code == "testlib_missing"


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


# --------------------------------------------------------------------------- #
# 生成的 checker：真编译真跑
# --------------------------------------------------------------------------- #


def _compile(tmp_path: Path, files: Dict[str, bytes]) -> Path:
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    exe = tmp_path / "chk"
    subprocess.run(
        [GXX, "-O2", "-std=c++14", "-o", str(exe), "checker.cpp", "-lm"],
        cwd=str(tmp_path), check=True, capture_output=True, timeout=120,
    )
    return exe


def _judge(exe: Path, tmp_path: Path, out: str, ans: str, inp: str = "") -> "tuple":
    (tmp_path / "in.txt").write_text(inp)
    (tmp_path / "out.txt").write_text(out)
    (tmp_path / "ans.txt").write_text(ans)
    proc = subprocess.run(
        [str(exe), "in.txt", "out.txt", "ans.txt"], cwd=str(tmp_path), capture_output=True, timeout=30,
    )
    return proc.returncode, proc.stderr.decode()


@needs_gxx
def test_real_checker_accepts_absolute_or_relative_error(tmp_path: Path) -> None:
    exe = _compile(tmp_path, {"checker.cpp": checkers.real_checker(1e-3)})
    assert _judge(exe, tmp_path, "1.0009\n", "1\n")[0] == 0        # 绝对误差 ≤ eps
    assert _judge(exe, tmp_path, "100.09\n", "100\n")[0] == 0      # 相对误差 ≤ eps
    code, message = _judge(exe, tmp_path, "0.5015\n", "0.5\n")     # 两者都超
    assert code == 1 and message.startswith("wrong answer")
    assert _judge(exe, tmp_path, "1.0011\n", "1\n")[0] == 1
    assert _judge(exe, tmp_path, "YES 3.14159", "YES\n3.1416\n")[0] == 0  # 非数字按字符串比
    assert _judge(exe, tmp_path, "NO 3.14159", "YES 3.1416")[0] == 1
    assert _judge(exe, tmp_path, "1 2", "1 2 3")[0] == 1           # 少输出
    assert _judge(exe, tmp_path, "1 2 3", "1 2")[0] == 1           # 多输出
    code, message = _judge(exe, tmp_path, "", "")
    assert code == 0 and message.startswith("ok")


@needs_gxx
def test_hustoj_wrapper_swaps_answer_and_output(tmp_path: Path) -> None:
    spj = b"""#include <stdio.h>
#include <string.h>
int main(int argc, char *argv[]) {
    FILE *fstd = fopen(argv[2], "r"), *fuser = fopen(argv[3], "r");
    char tag[16]; int want, got;
    if (fscanf(fstd, "%15s %d", tag, &want) != 2 || strcmp(tag, "STD") != 0) return 2;
    if (fscanf(fuser, "%d", &got) != 1) return 1;
    return want == got ? 0 : 1;
}
"""
    source, support = checkers.hustoj_wrapper(spj)
    assert list(support) == [checkers.HUSTOJ_SOURCE_NAME]
    exe = _compile(tmp_path, {"checker.cpp": source, "spj_src.h": support["spj_src.h"].data})
    # 评测机按 (输入, 选手输出, 标准答案) 调用
    assert _judge(exe, tmp_path, "5\n", "STD 5\n")[0] == 0
    code, message = _judge(exe, tmp_path, "6\n", "STD 5\n")
    assert code == 1 and message.startswith("wrong answer")


@needs_gxx
def test_injected_testlib_compiles(tmp_path: Path) -> None:
    bundle = ProblemBundle(key="k", ident="x", cases=[_case("1", score=100)], checker=FileRef(data=TESTLIB_CHECKER.encode()))
    _parsed, files = _parse(build_testdata_zip(bundle, None))
    assert files["testlib.h"].startswith(b"/*\n * testlib.h, bundled") and b"Mike Mirzayanov" in files["testlib.h"]
    exe = _compile(tmp_path, {"checker.cpp": files["checker.cpp"], "testlib.h": files["testlib.h"]})
    assert _judge(exe, tmp_path, "3\n", "3\n")[0] == 0
    assert _judge(exe, tmp_path, "4\n", "3\n")[0] == 1


def test_eps_literal() -> None:
    assert checkers.format_eps(1e-6) == "1e-06"
    assert checkers.format_eps(0.001) == "0.001"
    assert b"static const double EPS = 1e-06;" in checkers.real_checker(1e-6)
    with pytest.raises(ValueError):
        checkers.format_eps(0)
    assert json.dumps(checkers.checker_language_ok("SPJ.CC")) == "true"
    assert not checkers.checker_language_ok("spj.py")


def test_repeated_references_count_towards_volume_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个成员被引用 N 次按 N 份解压量算，超限在写数据包之前就拒绝。"""
    from syncoj_server.services.problem_io import build as build_mod

    path = stage(tmp_path, make_zip({"big.in": b"\0" * 4096, "big.out": "0\n"}))
    with SafeArchive(path) as archive:
        bundle = ProblemBundle(key="k", ident="k", cases=[
            BundleCase(name=str(i), input=FileRef(member="big.in"), answer=FileRef(member="big.out"), score=1)
            for i in range(1, 41)
        ])
        monkeypatch.setattr(build_mod, "MAX_BUILD_BYTES", 40 * 4096)
        copied = []
        monkeypatch.setattr(build_mod, "_copy_ref", lambda *args: copied.append(args))
        assert try_build(bundle, archive) is None
        assert [e.code for e in bundle.errors] == ["testdata_invalid"]
        assert "超过" in bundle.errors[0].message
        assert copied == []


@needs_gxx
def test_real_checker_absolute_only_matches_testlib_rcmp(tmp_path: Path) -> None:
    """testlib rcmp：EPS 1.5e-6，只比绝对误差。"""
    exe = _compile(tmp_path, {"checker.cpp": checkers.real_checker(1.5e-6, relative=False)})
    assert _judge(exe, tmp_path, "1.0000014\n", "1\n")[0] == 0     # (1e-6, 1.5e-6] 内仍通过
    assert _judge(exe, tmp_path, "1.0000016\n", "1\n")[0] == 1
    assert _judge(exe, tmp_path, "1000000.5\n", "1000000\n")[0] == 1  # 相对误差合格也不算


def test_native_import_keeps_io_columns_as_is(tmp_path: Path) -> None:
    """原生格式各列原样写回：stdio 题上存着的文件名、等于默认名的文件名都不规整。"""
    from syncoj_server.services import problem_io

    from problem_io_fixtures import make_native_zip, native_problem

    problems = [
        native_problem("sum", io_mode="stdio", input_file="data.txt"),
        native_problem("q", io_mode="file", input_file="q.in", output_file="q.out", compile_flags="-O2  -std=c++17"),
        native_problem("bad", io_mode="file", input_file="prog"),
    ]
    with problem_io.parse_archive(stage(tmp_path, make_native_zip(problems))) as parsed:
        got = {b.ident: (b.io_mode, b.input_file, b.output_file, b.compile_flags, [e.code for e in b.errors])
               for b in parsed.result.problems}
    assert got == {
        "sum": ("stdio", "data.txt", None, "-std=c++17", []),
        "q": ("file", "q.in", "q.out", "-O2  -std=c++17", []),
        "bad": ("file", "prog", None, "-std=c++17", ["io_name_invalid"]),
    }


# --------------------------------------------------------------------------- #
# 内存：构建写临时文件、预览不产出完整数据包、请求级预算
# --------------------------------------------------------------------------- #


def _shared_member_bundles(count: int, refs: int):
    """``count`` 道题，每道题 ``refs`` 个测试点都引用同一对成员 big.in / big.out。"""
    return [
        ProblemBundle(key="p%d" % p, ident="p%d" % p, cases=[
            BundleCase(name=str(i), input=FileRef(member="big.in"), answer=FileRef(member="big.out"), score=1)
            for i in range(1, refs + 1)
        ])
        for p in range(count)
    ]


def test_try_build_writes_a_temp_file_in_tmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建结果落在给定目录的临时文件里（不是内存里的 BytesIO），从头可读，摘要与回读一致。"""
    from syncoj_server.services.problem_io import build as build_mod

    dirs = []
    real = build_mod.tempfile.TemporaryFile

    def spy(*args, **kwargs):
        dirs.append(kwargs.get("dir"))
        return real(*args, **kwargs)

    monkeypatch.setattr(build_mod.tempfile, "TemporaryFile", spy)
    bundle = ProblemBundle(key="k", ident="k", cases=[_case("1", score=40), _case("2", score=60)])
    built = try_build(bundle, None, tmp_dir=tmp_path)
    assert built is not None
    with built:
        assert dirs == [str(tmp_path)]
        assert not isinstance(built.file, io.BytesIO) and built.file.fileno() >= 0
        data = built.file.read()
    assert built.max_bytes == len(data) and built.sha256 is None
    parsed, _files = _parse(data)
    assert built.summary == parsed.summary() == {"cases": 2, "subtasks": 0, "has_checker": False, "total_score": 100}


def test_summarize_does_not_write_test_data_and_reads_shared_member_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预览：测试数据不拷进任何数据包；多道题、多个测试点引用的同一成员整个请求只解压一遍。"""
    from syncoj_server.services.problem_io import build as build_mod

    path = stage(tmp_path, make_zip({"big.in": b"1 2\n" * 1024, "big.out": "3\n"}))
    with SafeArchive(path) as archive:
        expected = []
        for bundle in _shared_member_bundles(2, 5):
            built = try_build(bundle, archive)
            assert built is not None
            built.close()
            expected.append(built.summary)

        def no_copy(*args):
            raise AssertionError("预览不应拷贝测试数据")

        monkeypatch.setattr(build_mod, "_copy_ref", no_copy)
        opened = []
        real_open = archive.open
        monkeypatch.setattr(archive, "open", lambda name: opened.append(name) or real_open(name))
        budget = build_mod.BuildBudget()
        got = [try_summarize(bundle, archive, budget) for bundle in _shared_member_bundles(2, 5)]
    assert got == expected
    assert sorted(opened) == ["big.in", "big.out"]


def test_summarize_reports_corrupt_test_data(tmp_path: Path) -> None:
    """不写出数据包也要查出压缩数据损坏（与应用时一致），而不是等到应用时才报。"""
    from problem_io_fixtures import corrupt_member

    data = corrupt_member(make_zip({"1.in": b"1 2\n" * 4096, "1.out": "3\n"}), "1.in")
    with SafeArchive(stage(tmp_path, data)) as archive:
        bundle = ProblemBundle(key="k", ident="k", cases=[
            BundleCase(name="1", input=FileRef(member="1.in"), answer=FileRef(member="1.out"), score=100),
        ])
        assert try_summarize(bundle, archive) is None
    assert [(e.code, e.message) for e in bundle.errors] == [("testdata_invalid", "数据包不对：压缩数据已损坏")]


def test_request_budget_rejects_problems_beyond_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每题都在 2 GB 以内，但一次请求按引用累计超过总预算：超出的那道题报错，前面的照常。"""
    from syncoj_server.services.problem_io import build as build_mod

    path = stage(tmp_path, make_zip({"big.in": b"\0" * 4096, "big.out": "0\n"}))
    per_problem = 10 * (4096 + 2)
    monkeypatch.setattr(build_mod, "MAX_REQUEST_BUILD_BYTES", 2 * per_problem)
    with SafeArchive(path) as archive:
        for runner in ("summarize", "build"):
            budget = build_mod.BuildBudget()
            bundles = _shared_member_bundles(3, 10)
            for bundle in bundles:
                if runner == "summarize":
                    try_summarize(bundle, archive, budget)
                else:
                    built = try_build(bundle, archive, budget, tmp_dir=tmp_path)
                    if built is not None:
                        built.close()
            assert [b.errors for b in bundles[:2]] == [[], []]
            assert [e.code for e in bundles[2].errors] == ["testdata_invalid"]
            assert "本次导入的数据总量超过" in bundles[2].errors[0].message


def test_native_inner_zip_shared_by_problems_is_checked_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多道题指向同一个内层 testdata.zip：只读、哈希、解析一遍；sha 不符的那道题照样报 sha_mismatch。"""
    from syncoj_server.services import problem_io
    from syncoj_server.services.problem_io import native as native_mod

    from problem_io_fixtures import make_testdata_zip, sha256_hex

    inner = make_testdata_zip(4)
    meta = {"path": "shared/testdata.zip", "name": "t.zip", "sha256": sha256_hex(inner), "size": len(inner)}
    entries = [dict(ident="p%d" % i, io_mode="stdio", judge_mode="builtin", testdata=dict(meta)) for i in range(3)]
    entries.append(dict(ident="bad", io_mode="stdio", judge_mode="builtin", testdata=dict(meta, sha256="0" * 64)))
    data = make_zip({
        "shared/testdata.zip": inner,
        "syncoj-problems.json": json.dumps({"format": "syncoj-judge", "version": 1, "problems": entries}),
    })
    calls = []
    real = native_mod._inspect
    monkeypatch.setattr(native_mod, "_inspect", lambda view, p: calls.append(p) or real(view, p))
    with problem_io.parse_archive(stage(tmp_path, data)) as parsed:
        bundles = parsed.result.problems
        assert calls == ["shared/testdata.zip"]
        assert [[e.code for e in b.errors] for b in bundles] == [[], [], [], ["sha_mismatch"]]
        assert bundles[0].raw_sha256 == sha256_hex(inner)
        assert bundles[0].raw_summary == {"cases": 4, "subtasks": 0, "has_checker": False, "total_score": 100}


def test_native_inner_zip_streams_without_reading_into_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建原生题时直接流式读归档成员（不 read_ref 成 bytes，也不另落临时文件），字节与 sha256 不变。"""
    from syncoj_server.services import problem_io
    from syncoj_server.services.problem_io import build as build_mod

    from problem_io_fixtures import make_native_zip, make_testdata_zip, native_problem, sha256_hex

    inner = make_testdata_zip(5)
    with problem_io.parse_archive(stage(tmp_path, make_native_zip([native_problem("sum", testdata=inner)]))) as parsed:
        (bundle,) = parsed.result.problems

        def forbidden(*args, **kwargs):
            raise AssertionError("不应整包读进内存或另落临时文件")

        monkeypatch.setattr(build_mod, "read_ref", forbidden)
        monkeypatch.setattr(build_mod.tempfile, "TemporaryFile", forbidden)
        summary = try_summarize(bundle, parsed.archive)
        assert summary is not None and summary["cases"] == 5
        built = try_build(bundle, parsed.archive)
        assert built is not None
        with built:
            assert built.file.read() == inner
        assert built.sha256 == sha256_hex(inner) and built.summary["cases"] == 5
