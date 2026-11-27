"""题型与部分分（§12.13–§12.15）：checker 判定解析、Lemon / exitcode 协议、函数交互（grader）、
提交答案、交互题（testlib 双向管道）。

真编译、真运行的用例用 rlimit 后端（需要 g++；root 下选手程序换成 nobody），与
``test_judging_runner.py`` 同一套做法。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from syncoj_server.judging import runner, sandbox, testdata, verdict

needs_gcc = pytest.mark.skipif(
    shutil.which("g++") is None or sys.platform == "win32", reason="需要 Linux + g++"
)

AB = "#include <cstdio>\nint main(){long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n"


def make_zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(str(path), "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def factory(tmp_path: Path):
    run_as = "nobody" if hasattr(os, "geteuid") and os.geteuid() == 0 else None
    if run_as:
        os.chmod(str(tmp_path), 0o755)
    return lambda: sandbox.RlimitSandbox(base=tmp_path, run_as=run_as)


def judge(tmp_path: Path, code: str, files: dict, name="ab.cpp", **config):
    source = tmp_path / name
    source.write_text(code, encoding="utf-8")
    data = make_zip(tmp_path / "data.zip", files)
    return runner.judge(source, name, data, runner.ProblemConfig(ident="ab", **config), factory(tmp_path))


def parse(files: dict, problem_type=None):
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with zipfile.ZipFile(buffer) as archive:
        return testdata.parse_testdata(archive, problem_type)


AB_CASES = {"1.in": "1 2\n", "1.ans": "3\n", "2.in": "5 7\n", "2.ans": "12\n"}


# ------------------------------------------------------------ 判定解析 ------ #


@pytest.mark.parametrize("stderr, code, mode, kind, ratio", [
    ("ok 3 numbers", 0, "ratio", "ok", 1.0),
    ("wrong answer expected 3", 1, "ratio", "wa", 0.0),
    ("wrong output format Extra information", 2, "ratio", "pe", 0.0),
    ("FAIL answer file broken", 3, "ratio", "fail", 0.0),
    ("points 0.5 half", 7, "ratio", "partial", 0.5),
    ("points 1.5", 7, "ratio", "fail", 0.0),
    ("points 50 half", 7, "percent", "partial", 0.5),
    ("points 150", 7, "percent", "fail", 0.0),
    ("points 0.25", 7, "auto", "partial", 0.25),
    ("points 25", 7, "auto", "partial", 0.25),
    ("points 0", 7, "ratio", "wa", 0.0),
    ("points 1", 7, "ratio", "ok", 1.0),
    ("points points_info=abc", 7, "ratio", "fail", 0.0),
    ("partially correct (0) none", 16, "ratio", "wa", 0.0),
    ("partially correct (3) a bit", 19, "ratio", "partial", 0.03),
    ("partially correct (50) half", 66, "ratio", "partial", 0.5),
    ("partially correct (150) over", 166, "ratio", "fail", 0.0),
    ("unexpected eof", 8, "ratio", "wa", 0.0),
    ("", 0, "ratio", "ok", 1.0),
    ("", 1, "ratio", "wa", 0.0),
    ("", 2, "ratio", "pe", 0.0),
    ("", 7, "ratio", "fail", 0.0),
    ("\x1b[0;32mok\x1b[0m fine", 0, "ratio", "ok", 1.0),
])
def test_testlib前缀表(stderr, code, mode, kind, ratio) -> None:
    result = verdict.parse_testlib(stderr, code, mode)
    assert result.kind == kind
    assert abs(result.ratio - ratio) < 1e-9


def test_lemon与exitcode协议() -> None:
    assert verdict.parse_lemon("7\n", 10).ratio == pytest.approx(0.7)
    assert verdict.parse_lemon("10", 10).kind == "ok"
    assert verdict.parse_lemon("0", 10).kind == "wa"
    assert verdict.parse_lemon(None, 10).kind == "fail"
    assert verdict.parse_lemon("-1", 10).kind == "fail"
    assert verdict.parse_lemon("11", 10).kind == "fail"
    assert verdict.parse_lemon("x", 10).kind == "fail"
    assert verdict.parse_exitcode(0).kind == "ok"
    assert verdict.parse_exitcode(1).kind == "wa"
    assert verdict.parse_exitcode(2).kind == "fail"


@pytest.mark.parametrize("output, answer, mode, expected", [
    (b"1 2\n", b"1 2\n", "strict", "AC"),
    (b"1 2 \n", b"1 2\n", "strict", "PE"),
    (b"1 2\n\n", b"1 2\n", "strict", "PE"),
    (b"1 2\r\n", b"1 2\n", "strict", "AC"),
    (b"1  2\n", b"1 2\n", "strict", "PE"),
    (b"1 2 \n", b"1 2\n", "trailing_spaces", "AC"),
    (b"1 2\n\n", b"1 2\n", "trailing_spaces", "PE"),
    (b"1 2\n\n\n", b"1 2\n", "trailing_newlines", "AC"),
    (b"1 2 \n", b"1 2\n", "trailing_newlines", "PE"),
    (b"1 2 \n\n", b"1 2", "both", "AC"),
    (b"1  2\n", b"1 2\n", "both", "PE"),
    (b"12\n", b"1 2\n", "both", "WA"),
    (b"1 3\n", b"1 2\n", "both", "WA"),
])
def test_比较方式真值表与格式错误(output, answer, mode, expected) -> None:
    assert runner.compare_outputs(output, answer, mode) == expected


def test_状态取最严重() -> None:
    assert runner.worst(["AC", "PA", "PE"]) == "PE"
    assert runner.worst(["AC", "PA"]) == "PA"
    assert runner.worst(["PA", "WA"]) == "WA"


# ------------------------------------------------------------ 数据包 -------- #


def test_数据包_题型与字段() -> None:
    with pytest.raises(testdata.TestdataError, match="grader"):
        parse({"1.in": "", "1.ans": "", "config.json": json.dumps({"type": "grader"})})
    data = parse({"1.in": "", "1.ans": "", "grader.cpp": "", "add.h": "", "config.json": json.dumps({"foo": 1})},
                 "grader")
    assert data.problem_type == "grader" and data.grader == {"cpp": "grader.cpp"} and data.grader_files == ["add.h"]
    assert [w["code"] for w in data.warnings] == ["unknown_config_key"]
    inter = parse({"1.in": "", "interactor.cpp": ""}, "interactive")
    assert inter.cases[0].answer is None and inter.interactor == "interactor.cpp"
    with pytest.raises(testdata.TestdataError, match="interactor"):
        parse({"1.in": "", "1.ans": ""}, "interactive")
    hint = parse({"1.in": "", "1.ans": "", "interactor.cpp": ""})
    assert hint.detected_type == "interactive" and hint.problem_type == "traditional"
    with pytest.raises(testdata.TestdataError, match="checker_points"):
        parse({"1.in": "", "1.ans": "", "config.json": json.dumps({"checker_points": "x"})})
    with pytest.raises(testdata.TestdataError, match="文件名"):
        parse({"1.in": "", "1.ans": "", "x/main.cpp": "", "config.json": json.dumps({"grader": "main.cpp"})}, "grader")


def test_数据包_提交答案的答案名() -> None:
    data = parse({"1.in": "", "1.ans": "", "2.in": "", "2.ans": ""}, "answer_only")
    assert data.answer_names == {"1": "{ident}1.out", "2": "{ident}2.out"}
    assert data.summary()["answer_exts"] == [".out"]
    named = parse({"tsp1.in": "", "tsp1.ans": "", "config.json": json.dumps({"answer_files": {"tsp1": "a.txt"}})},
                  "answer_only")
    assert named.answer_names == {"tsp1": "a.txt"}
    with pytest.raises(testdata.TestdataError, match="扩展名"):
        parse({"1.in": "", "1.ans": "", "config.json": json.dumps({"answer_pattern": "{ident}{case}"})}, "answer_only")
    # 有 checker 时答案可以缺
    assert parse({"1.in": "", "checker.cpp": ""}, "answer_only").cases[0].answer is None


# ------------------------------------------------------------ SPJ 部分分 ---- #

TESTLIB_PARTIAL = r'''#include "testlib.h"
int main(int argc,char**argv){registerTestlibCmd(argc,argv);
long long x=ouf.readLong(),y=ans.readLong();
if(x==y)quitf(_ok,"right");
if(x==y+1)quitp(0.5,"off by one");
if(x==y+2)quitf(_pc(30),"off by two");
if(x==y+3)quitf(_pe,"format");
quitf(_wa,"expected %lld",y);}
'''


@needs_gcc
def test_testlib_checker给部分分_子任务取最小比例(tmp_path) -> None:
    config = {"cases": [{"name": "1", "score": 40}, {"name": "2", "score": 60}],
              "subtasks": [{"score": 60, "cases": ["2"]}]}
    files = dict(AB_CASES, **{"checker.cpp": TESTLIB_PARTIAL, "config.json": json.dumps(config)})
    good = judge(tmp_path, AB, files)
    assert (good.status, good.score) == ("AC", 100)
    plus1 = judge(tmp_path, AB.replace("a+b", "a+b+1"), files)
    assert [c.status for c in plus1.cases] == ["PA", "PA"]
    assert plus1.cases[0].score == 20 and plus1.subtasks[0]["score"] == 30 and plus1.score == 50
    assert plus1.status == "PA" and plus1.to_result_json()["cases"][0]["ratio"] == 0.5
    plus2 = judge(tmp_path, AB.replace("a+b", "a+b+2"), files)
    assert plus2.score == 12 + 18 and plus2.status == "PA"
    plus3 = judge(tmp_path, AB.replace("a+b", "a+b+3"), files)
    assert plus3.status == "PE" and plus3.score == 0


@needs_gcc
def test_lemon协议读分数文件(tmp_path) -> None:
    spj = ("#include <cstdio>\nint main(int c,char**v){FILE*o=fopen(v[2],\"r\"),*a=fopen(v[3],\"r\");"
           "long long x,y;fscanf(o,\"%lld\",&x);fscanf(a,\"%lld\",&y);int full;sscanf(v[4],\"%d\",&full);"
           "FILE*s=fopen(v[5],\"w\");fprintf(s,\"%d\\n\",x==y?full:7);fclose(s);"
           "FILE*m=fopen(v[6],\"w\");fputs(x==y?\"good\":\"partly\",m);fclose(m);return 0;}")
    config = {"checker_protocol": "lemon", "cases": [{"name": "1", "score": 10}, {"name": "2", "score": 30,
                                                                                    "full_score": 10}]}
    files = dict(AB_CASES, **{"spj.cpp": spj, "config.json": json.dumps(config)})
    wrong = judge(tmp_path, AB.replace("a+b", "a-b"), files)
    assert [c.status for c in wrong.cases] == ["PA", "PA"] and wrong.score == 7 + 21
    assert wrong.cases[0].message == "partly"
    broken = dict(files, **{"spj.cpp": "int main(){return 0;}"})
    with pytest.raises(sandbox.SandboxError, match="分数文件"):
        judge(tmp_path, AB, broken)


@needs_gcc
def test_exitcode协议(tmp_path) -> None:
    chk = ("#include <cstdio>\nint main(int c,char**v){FILE*o=fopen(v[2],\"r\"),*a=fopen(v[3],\"r\");"
           "long long x,y;fscanf(o,\"%lld\",&x);fscanf(a,\"%lld\",&y);fputs(\"points 1\",stderr);"
           "return x==y?0:(x==0?5:1);}")
    files = dict(AB_CASES, **{"checker.cpp": chk, "config.json": json.dumps({"checker_protocol": "exitcode"})})
    assert judge(tmp_path, AB, files).status == "AC"
    assert judge(tmp_path, AB.replace("a+b", "a+b+1"), files).status == "WA"
    with pytest.raises(sandbox.SandboxError, match="退出码 5"):
        judge(tmp_path, AB.replace("a+b", "0*a"), files)


@needs_gcc
def test_checker在评测侧工作目录_选手看不到(tmp_path) -> None:
    """选手程序在自己的工作目录里找 checker 与答案：一个都看不到。"""
    spy = ("#include <cstdio>\nint main(){const char*n[]={\"chk\",\"checker.cpp\",\"ans.txt\",\"1.ans\","
           "\"testlib.h\",\"interactor\"};int seen=0;for(int i=0;i<6;i++){FILE*f=fopen(n[i],\"r\");if(f)seen++;}"
           "long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",seen?-1:a+b);}")
    files = dict(AB_CASES, **{"checker.cpp": TESTLIB_PARTIAL,
                              "config.json": json.dumps({"precheck": {"disabled": ["file_access"]}})})
    assert judge(tmp_path, spy, files).status == "AC"


# ------------------------------------------------------------ grader -------- #

GRADER = ("#include <cstdio>\n#include \"add.h\"\n#ifndef EVAL\n#error EVAL\n#endif\n"
          "int main(){long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",add(a,b));}\n")
ADD_H = "long long add(long long a, long long b);\n"


def grader_files(**extra):
    files = dict(AB_CASES, **{"grader.cpp": GRADER, "add.h": ADD_H})
    files.update(extra)
    return files


@needs_gcc
def test_grader_选手只写函数(tmp_path) -> None:
    user = "#include \"add.h\"\nlong long add(long long a,long long b){return a+b;}\n"
    outcome = judge(tmp_path, user, grader_files(), problem_type="grader")
    assert outcome.status == "AC" and outcome.score == 100, outcome
    assert outcome.to_result_json()["problem_type"] == "grader"


@needs_gcc
def test_grader_选手写main或包含grader判CE(tmp_path) -> None:
    with_main = AB + "long long add(long long a,long long b){return a+b;}\n"
    outcome = judge(tmp_path, with_main, grader_files(), problem_type="grader")
    assert outcome.status == "CE" and "main" in outcome.compile_message
    sneaky = "#include \"grader.cpp\"\nlong long add(long long a,long long b){return a+b;}\n"
    outcome = judge(tmp_path, sneaky, grader_files(), problem_type="grader")
    assert outcome.status == "CE"
    c_code = "long long add(long long a,long long b){return a+b;}\n"
    outcome = judge(tmp_path, c_code, grader_files(), name="ab.c", problem_type="grader")
    assert outcome.status == "CE" and "grader" in outcome.compile_message


# ------------------------------------------------------------ 提交答案 ------ #


def test_提交答案_多点_缺文件判WA(tmp_path) -> None:
    files = {"1.in": "", "1.ans": "1\n", "2.in": "", "2.ans": "2\n", "3.in": "", "3.ans": "3 4\n"}
    data = make_zip(tmp_path / "d.zip", files)
    (tmp_path / "a1").write_bytes(b"1\n")
    (tmp_path / "a3").write_bytes(b"3  4\n")
    outcome = runner.judge_answers(
        {"1": tmp_path / "a1", "2": None, "3": tmp_path / "a3"},
        {"1": "ab1.out", "2": "ab2.out", "3": "ab3.out"},
        data, runner.ProblemConfig(ident="ab", problem_type="answer_only"),
        factory(tmp_path),
    )
    assert [c.status for c in outcome.cases] == ["AC", "WA", "PE"]
    assert "ab2.out" in outcome.cases[1].message and outcome.cases[1].to_dict()["file"] == "ab2.out"
    assert outcome.score == 33 and outcome.status == "WA"


@needs_gcc
def test_提交答案_checker给部分分(tmp_path) -> None:
    files = {"1.in": "", "1.ans": "5\n", "checker.cpp": TESTLIB_PARTIAL}
    data = make_zip(tmp_path / "d.zip", files)
    (tmp_path / "a1").write_bytes(b"6\n")
    outcome = runner.judge_answers({"1": tmp_path / "a1"}, {"1": "ab1.out"}, data,
                                   runner.ProblemConfig(ident="ab", problem_type="answer_only"), factory(tmp_path))
    assert outcome.status == "PA" and outcome.score == 50


# ------------------------------------------------------------ IO auto ------- #


@needs_gcc
def test_自动IO_标准输入输出与freopen都能评(tmp_path) -> None:
    config = {"precheck": {"disabled": ["file_access"]}}
    files = dict(AB_CASES, **{"config.json": json.dumps(config)})
    assert judge(tmp_path, AB, files, io_mode="auto").status == "AC"
    with_freopen = AB.replace("int main(){", "int main(){freopen(\"ab.in\",\"r\",stdin);freopen(\"ab.out\",\"w\",stdout);")
    assert judge(tmp_path, with_freopen, files, io_mode="auto").status == "AC"
    # 读标准输入、写输出文件的混合写法
    mixed = AB.replace("int main(){", "int main(){freopen(\"ab.out\",\"w\",stdout);")
    assert judge(tmp_path, mixed, files, io_mode="auto").status == "AC"
    silent = "int main(){return 0;}"
    outcome = judge(tmp_path, silent, files, io_mode="auto")
    assert outcome.status == "WA"


@needs_gcc
def test_比较方式与格式错误_端到端(tmp_path) -> None:
    spaced = AB.replace("%lld\\n", "%lld \\n")
    assert judge(tmp_path, spaced, AB_CASES).status == "AC"
    strict = judge(tmp_path, spaced, AB_CASES, compare_mode="strict")
    assert strict.status == "PE" and strict.score == 0
