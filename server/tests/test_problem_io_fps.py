"""FPS XML 导入：时间与内存单位、测试数据配对、spj 外壳与 tpj、XML 安全、交互器报错。"""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

from syncoj_server.judging.testdata import MAX_CASES, parse_testdata
from syncoj_server.services import problem_io
from syncoj_server.services.problem_io import checkers, fps
from syncoj_server.services.problem_io.build import build_testdata_zip
from syncoj_server.services.problem_io.notices import ProblemIoError

from problem_io_fixtures import fps_item, fps_tests, make_fps_xml, make_fps_zip, make_zip, stage
from test_judge_import_api import accept_all, apply, preview

GXX = shutil.which("g++")

HUSTOJ_SPJ = """#include <stdio.h>
int main(int argc, char *argv[]) {
    FILE *fstd = fopen(argv[2], "r"), *fuser = fopen(argv[3], "r");
    int want, got;
    if (fscanf(fstd, "%d", &want) != 1) return 2;
    if (fscanf(fuser, "%d", &got) != 1) return 1;
    return want == got ? 0 : 1;
}
"""

TPJ = """#include "testlib.h"
int main(int argc, char* argv[]) { registerTestlibCmd(argc, argv); quitf(_ok, "ok"); }
"""


def _parse(tmp_path: Path, data: bytes, name: str = "problems.xml", fmt: str = "auto"):
    with problem_io.parse_archive(stage(tmp_path, data, name), fmt, name) as parsed:
        built = [build_testdata_zip(b, parsed.archive) if not b.errors else None for b in parsed.result.problems]
        return parsed.result, built


def _one(tmp_path: Path, item: str):
    result, (built,) = _parse(tmp_path, make_fps_xml([item]))
    (bundle,) = result.problems
    return bundle, built


def _codes(items) -> List[str]:
    return [item.code for item in items]


def _testdata(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return parse_testdata(zf), {name: zf.read(name) for name in zf.namelist()}


# ---- 基本映射 ---------------------------------------------------------------


def test_plain_xml_upload(tmp_path: Path) -> None:
    result, (built,) = _parse(tmp_path, make_fps_xml([fps_item("A+B Problem", count=3)]))
    assert result.format == "fps"
    (bundle,) = result.problems
    assert (bundle.key, bundle.ident, bundle.title, bundle.source_name) == ("1", "p1", "A+B Problem", "A+B Problem")
    assert (bundle.time_limit_ms, bundle.memory_limit_mb, bundle.io_mode) == (1000, 256, "stdio")
    assert [c.name for c in bundle.cases] == ["test_01", "test_02", "test_03"]
    # FPS 没有分值：按 100 分平分
    assert [c.score for c in bundle.cases] == [33, 33, 34] and "scores_missing" in _codes(bundle.warnings)
    assert bundle.checker is None and bundle.errors == []
    assert _codes(result.warnings) == ["statement_dropped"]
    parsed, files = _testdata(built)
    assert [c.name for c in parsed.cases] == ["test_01", "test_02", "test_03"]
    assert files["test_02.in"] == b"2 2\n" and files["test_02.ans"] == b"4\n"
    assert "sample.in" not in files  # 样例不导入


def test_zip_with_problem_xml_and_several_items(tmp_path: Path) -> None:
    data = make_fps_zip([fps_item("甲"), fps_item("乙")], shell="export")
    result, built = _parse(tmp_path, data, name="fps.zip")
    assert [(b.key, b.ident, b.title) for b in result.problems] == [("1", "p1", "甲"), ("2", "p2", "乙")]
    assert all(item is not None for item in built)


@pytest.mark.parametrize(
    "time, unit, expected",
    [
        ("1", "s", 1000),
        ("1.000", "s", 1000),       # 官方示例的写法
        ("0.5", "s", 500),
        ("0.3", "s", 300),          # 浮点误差不应变成 299
        ("2", None, 2000),          # 缺省单位 = 秒
        ("1500", "ms", 1500),
        ("1500", "MS", 1500),       # 单位不分大小写
        ("3", "S", 3000),
    ],
)
def test_time_units(tmp_path: Path, time: str, unit, expected: int) -> None:
    bundle, _ = _one(tmp_path, fps_item(time=time, time_unit=unit))
    assert bundle.time_limit_ms == expected
    assert "limits_defaulted" not in _codes(bundle.warnings)


@pytest.mark.parametrize(
    "memory, unit, expected",
    [
        ("128", "mb", 128),
        ("128", None, 128),
        ("131072", "kb", 128),
        ("131072", "KB", 128),
        ("100000", "kb", 98),        # 向上取整
        ("1", "gb", 1024),
        ("64.5", "mb", 65),
    ],
)
def test_memory_units(tmp_path: Path, memory: str, unit, expected: int) -> None:
    bundle, _ = _one(tmp_path, fps_item(memory=memory, memory_unit=unit))
    assert bundle.memory_limit_mb == expected


def test_missing_limits_fall_back_to_defaults(tmp_path: Path) -> None:
    bundle, _ = _one(tmp_path, fps_item(time="", memory="abc"))
    assert (bundle.time_limit_ms, bundle.memory_limit_mb) == (1000, 256)
    assert "limits_defaulted" in _codes(bundle.warnings)


# ---- 测试数据配对 -----------------------------------------------------------


def test_pairs_by_name_even_when_out_of_order(tmp_path: Path) -> None:
    tests = (
        '<test_input name="b"><![CDATA[2 2\n]]></test_input>'
        '<test_input name="a"><![CDATA[1 1\n]]></test_input>'
        '<test_output name="a"><![CDATA[2\n]]></test_output>'
        '<test_output name="b"><![CDATA[4\n]]></test_output>'
    )
    bundle, built = _one(tmp_path, fps_item(tests=tests))
    assert [(c.name, c.input.data, c.answer.data) for c in bundle.cases] == [
        ("b", b"2 2\n", b"4\n"),
        ("a", b"1 1\n", b"2\n"),
    ]
    assert built is not None


def test_named_input_without_output_is_an_error(tmp_path: Path) -> None:
    tests = (
        '<test_input name="a"><![CDATA[1 1\n]]></test_input>'
        '<test_output name="a"><![CDATA[2\n]]></test_output>'
        '<test_input name="b"><![CDATA[2 2\n]]></test_input>'
    )
    bundle, built = _one(tmp_path, fps_item(tests=tests))
    assert _codes(bundle.errors) == ["missing_answer"] and "b" in bundle.errors[0].message
    assert built is None


@pytest.mark.parametrize("layout", ["interleaved", "grouped"])
def test_unnamed_tests_pair_by_position(tmp_path: Path, layout: str) -> None:
    bundle, built = _one(tmp_path, fps_item(tests=fps_tests(3, layout=layout, named=False)))
    assert [(c.name, c.input.data, c.answer.data) for c in bundle.cases] == [
        ("1", b"1 1\n", b"2\n"),
        ("2", b"2 2\n", b"4\n"),
        ("3", b"3 3\n", b"6\n"),
    ]
    assert bundle.errors == [] and built is not None


@pytest.mark.parametrize("layout", ["interleaved", "grouped"])
def test_named_inputs_unnamed_outputs_pair_by_position(tmp_path: Path, layout: str) -> None:
    tests = fps_tests(2, layout=layout, named=True).replace(' name="test_01"></test_output', "")
    tests = tests.replace('<test_output name="test_01">', "<test_output>").replace('<test_output name="test_02">', "<test_output>")
    bundle, _ = _one(tmp_path, fps_item(tests=tests))
    assert [(c.name, c.answer.data) for c in bundle.cases] == [("test_01", b"2\n"), ("test_02", b"4\n")]


def test_interleaved_input_without_output_is_an_error(tmp_path: Path) -> None:
    # HUSTOJ 导出时某个 .in 没有 .out：in, in, out —— 第一个输入缺答案，不能错位
    tests = (
        "<test_input><![CDATA[1 1\n]]></test_input>"
        "<test_input><![CDATA[2 2\n]]></test_input>"
        "<test_output><![CDATA[4\n]]></test_output>"
        "<test_input><![CDATA[3 3\n]]></test_input>"
        "<test_output><![CDATA[6\n]]></test_output>"
    )
    bundle, _ = _one(tmp_path, fps_item(tests=tests))
    assert _codes(bundle.errors) == ["missing_answer"]
    assert bundle.errors[0].message == "测试点 1 缺少答案"


def test_grouped_with_fewer_outputs_is_an_error(tmp_path: Path) -> None:
    tests = (
        "<test_input><![CDATA[1 1\n]]></test_input>"
        "<test_input><![CDATA[2 2\n]]></test_input>"
        "<test_output><![CDATA[2\n]]></test_output>"
    )
    bundle, _ = _one(tmp_path, fps_item(tests=tests))
    assert bundle.errors[0].message == "测试点 2 缺少答案"


def test_empty_output_and_text_are_kept_verbatim(tmp_path: Path) -> None:
    tests = (
        '<test_input name="x"><![CDATA[  a  b \n\nc]]></test_input>'
        '<test_output name="x"></test_output>'
        '<test_input name="y"><![CDATA[中文 ]]>]]&gt;<![CDATA[ 尾]]></test_input>'
        '<test_output name="y"><![CDATA[ok]]></test_output>'
    )
    bundle, built = _one(tmp_path, fps_item(tests=tests))
    assert bundle.cases[0].input.data == b"  a  b \n\nc" and bundle.cases[0].answer.data == b""
    # HUSTOJ 的 fixcdata 把 "]]>" 拆成多段，拼回来是原文
    assert bundle.cases[1].input.data == "中文 ]]> 尾".encode("utf-8")
    assert built is not None


def test_no_tests_is_an_error(tmp_path: Path) -> None:
    bundle, _ = _one(tmp_path, fps_item(tests=""))
    assert _codes(bundle.errors) == ["no_cases"]


def test_too_many_cases(tmp_path: Path) -> None:
    bundle, _ = _one(tmp_path, fps_item(tests=fps_tests(MAX_CASES + 1, named=False)))
    assert _codes(bundle.errors) == ["too_many_cases"]


# ---- checker ----------------------------------------------------------------


def test_hustoj_spj_is_wrapped(tmp_path: Path) -> None:
    item = fps_item(extra_xml='<spj language="C"><![CDATA[%s]]></spj>' % HUSTOJ_SPJ)
    bundle, built = _one(tmp_path, item)
    assert bundle.errors == []
    assert "wrapped_hustoj_spj" in _codes(bundle.warnings)
    wrapper, support = checkers.hustoj_wrapper(HUSTOJ_SPJ.encode())
    assert bundle.checker.data == wrapper
    assert set(bundle.checker_support) == {checkers.HUSTOJ_SOURCE_NAME}
    assert bundle.checker_support[checkers.HUSTOJ_SOURCE_NAME].data == HUSTOJ_SPJ.encode()
    assert b"#define main __syncoj_spj_main" in wrapper and b'#include "spj_src.h"' in wrapper
    _parsed, files = _testdata(built)
    assert files["checker.cpp"] == wrapper and files["spj_src.h"] == HUSTOJ_SPJ.encode()
    assert "testlib.h" not in files


@pytest.mark.skipif(GXX is None, reason="没有 g++")
def test_wrapped_spj_judges_with_testlib_argument_order(tmp_path: Path) -> None:
    bundle, built = _one(tmp_path, fps_item(extra_xml='<spj language="C++"><![CDATA[%s]]></spj>' % HUSTOJ_SPJ))
    _parsed, files = _testdata(built)
    work = tmp_path / "chk"
    work.mkdir()
    for name in ("checker.cpp", "spj_src.h"):
        (work / name).write_bytes(files[name])
    subprocess.run([GXX, "-O2", "-o", str(work / "chk"), str(work / "checker.cpp")], check=True, cwd=str(work))
    (work / "in").write_text("1 1\n")
    (work / "ans").write_text("2\n")
    for out, code in (("2\n", 0), ("3\n", 1)):
        (work / "out").write_text(out)
        # 评测机按 testlib 顺序 (输入, 选手输出, 标准答案) 调用
        done = subprocess.run([str(work / "chk"), str(work / "in"), str(work / "out"), str(work / "ans")])
        assert done.returncode == code


def test_spj_using_testlib_gets_testlib_injected(tmp_path: Path) -> None:
    bundle, built = _one(tmp_path, fps_item(extra_xml='<spj language="C++"><![CDATA[%s]]></spj>' % TPJ))
    assert "testlib_injected" in _codes(bundle.warnings)
    _parsed, files = _testdata(built)
    assert "testlib.h" in files


def test_tpj_is_a_testlib_checker(tmp_path: Path) -> None:
    bundle, built = _one(tmp_path, fps_item(extra_xml='<tpj language="C++"><![CDATA[%s]]></tpj>' % TPJ))
    assert bundle.checker.data == TPJ.encode() and bundle.checker_support == {}
    assert "tpj_order_uncertain" in _codes(bundle.warnings)
    assert "checker_not_testlib" not in _codes(bundle.warnings)
    _parsed, files = _testdata(built)
    assert files["checker.cpp"] == TPJ.encode() and "testlib.h" in files


def test_tpj_without_testlib_is_flagged(tmp_path: Path) -> None:
    bundle, _ = _one(tmp_path, fps_item(extra_xml="<tpj><![CDATA[int main(){return 0;}]]></tpj>"))
    assert {"tpj_order_uncertain", "checker_not_testlib"} <= set(_codes(bundle.warnings))


def test_empty_spj_element_means_default_compare(tmp_path: Path) -> None:
    bundle, _ = _one(tmp_path, fps_item(extra_xml='<spj language="C++"><![CDATA[]]></spj>'))
    assert bundle.checker is None and bundle.errors == []


@pytest.mark.parametrize(
    "extra, code",
    [
        ('<interactor language="C++"><![CDATA[int main(){}]]></interactor>', "interactive_unsupported"),
        ('<spj language="Text"><![CDATA[]]></spj>', "text_judge_unsupported"),
        ('<spj language="Java"><![CDATA[class Main{}]]></spj>', "checker_language"),
        ('<tpj language="Python"><![CDATA[print(1)]]></tpj>', "checker_language"),
    ],
)
def test_unsupported_judging(tmp_path: Path, extra: str, code: str) -> None:
    bundle, built = _one(tmp_path, fps_item(extra_xml=extra))
    assert _codes(bundle.errors) == [code] and built is None


def test_code_template_is_dropped_with_warning(tmp_path: Path) -> None:
    extra = '<append language="C++"><![CDATA[int main(){}]]></append><solution language="C++"><![CDATA[x]]></solution>'
    bundle, _ = _one(tmp_path, fps_item(extra_xml=extra))
    assert "template_dropped" in _codes(bundle.warnings) and bundle.errors == []


# ---- XML 安全与整包错误 -----------------------------------------------------


def test_doctype_is_rejected(tmp_path: Path) -> None:
    bomb = '<!DOCTYPE fps [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
    with pytest.raises(ProblemIoError) as info:
        _parse(tmp_path, make_fps_xml(prolog=bomb))
    assert info.value.code == "xml_unsafe"


def test_doctype_is_rejected_by_the_parser_even_when_utf16() -> None:
    # 原始字节里查不到 "<!DOCTYPE"（UTF-16），靠 expat 的声明回调拒
    text = '<?xml version="1.0" encoding="UTF-16"?>\n<!DOCTYPE fps [<!ENTITY a "x">]>\n<fps><item>&a;</item></fps>'
    with pytest.raises(ProblemIoError) as info:
        fps._parse_xml(text.encode("utf-16"))
    assert info.value.code == "xml_unsafe"


def test_doctype_text_inside_cdata_is_fine(tmp_path: Path) -> None:
    item = fps_item().replace("<p>a+b</p>", "<!DOCTYPE html><p>a+b</p>")
    bundle, built = _one(tmp_path, item)
    assert bundle.errors == [] and built is not None


def test_malformed_xml_and_wrong_root(tmp_path: Path) -> None:
    with pytest.raises(ProblemIoError) as info:
        _parse(tmp_path, b"<fps><item></fps>")
    assert info.value.code == "bad_manifest"
    with pytest.raises(ProblemIoError) as info:
        _parse(tmp_path, b"<problems><item/></problems>", fmt="fps")
    assert info.value.code == "unknown_format"


def test_fps_without_items_has_no_problems(tmp_path: Path) -> None:
    with pytest.raises(ProblemIoError) as info:
        _parse(tmp_path, make_fps_xml([]))
    assert info.value.code == "no_problems"


def test_forced_fps_picks_the_only_xml_in_zip(tmp_path: Path) -> None:
    data = make_zip({"readme.txt": "hi", "problems.xml": make_fps_xml()})
    result, _ = _parse(tmp_path, data, name="x.zip", fmt="fps")
    assert len(result.problems) == 1


def test_preview_and_apply_through_api(client: TestClient, contest: dict, admin_headers: dict) -> None:
    item = fps_item("A+B", count=4, time="0.5", extra_xml='<spj language="C"><![CDATA[%s]]></spj>' % HUSTOJ_SPJ)
    response = preview(client, contest, admin_headers, make_fps_xml([item, fps_item("B")]), name="hustoj.xml")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "fps"
    first, second = body["problems"]
    assert (first["ident"], first["cases"], first["total_score"], first["time_limit_ms"]) == ("p1", 4, 100, 500)
    assert second["ident"] == "p2"
    response = apply(client, contest, admin_headers, body["token"], accept_all(body))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["created"] == 2
    assert out["problems"][0]["testdata"]["cases"] == 4
