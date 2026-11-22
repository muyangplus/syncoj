"""自带评测的核心（§12）：数据包解析、沙箱、编译比对计分。

用 rlimit 后端真编译、真运行（需要 g++）；以 root 跑测试时换成 nobody 账号运行选手程序。
isolate 后端用一个假的 isolate 脚本覆盖命令行与 meta 解析（真 isolate 不在 CI 上）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from syncoj_server.judging import runner, sandbox, testdata

needs_gcc = pytest.mark.skipif(
    shutil.which("g++") is None or sys.platform == "win32", reason="需要 Linux + g++"
)

AB = "#include <cstdio>\nint main(){long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n"


def make_zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(str(path), "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def ab_data(tmp_path: Path, extra: dict = None) -> Path:
    files = {"1.in": "1 2\n", "1.ans": "3\n", "2.in": "5 7\n", "2.ans": "12\n",
             "10.in": "0 0\n", "10.ans": "0\n"}
    files.update(extra or {})
    return make_zip(tmp_path / "data.zip", files)


def factory(tmp_path: Path):
    run_as = "nobody" if hasattr(os, "geteuid") and os.geteuid() == 0 else None

    def make():
        return sandbox.RlimitSandbox(base=tmp_path, run_as=run_as)

    if run_as:
        os.chmod(str(tmp_path), 0o755)
    return make


def judge(tmp_path: Path, code: str, data: Path, name="ab.cpp", **config):
    source = tmp_path / name
    source.write_text(code, encoding="utf-8")
    cfg = runner.ProblemConfig(ident="ab", **config)
    return runner.judge(source, name, data, cfg, factory(tmp_path))


# ---------------------------------------------------------------- 数据包 ---- #


def test_数据包按自然序_平均分配分值(tmp_path) -> None:
    with zipfile.ZipFile(str(ab_data(tmp_path))) as archive:
        data = testdata.parse_testdata(archive)
    assert [c.name for c in data.cases] == ["1", "2", "10"]
    assert [c.score for c in data.cases] == [33, 33, 34] and data.total_score == 100


def test_数据包_单层目录去掉前缀_配置分值与子任务(tmp_path) -> None:
    config = {"cases": [{"name": "1", "score": 10}, {"name": "2", "score": 20}, {"name": "3", "score": 30}],
              "subtasks": [{"score": 40, "cases": ["2", "3"]}]}
    path = make_zip(tmp_path / "d.zip", {"data/1.in": "", "data/1.ans": "", "data/2.in": "", "data/2.out": "",
                                          "data/3.in": "", "data/3.ans": "", "data/config.json": json.dumps(config)})
    with zipfile.ZipFile(str(path)) as archive:
        data = testdata.parse_testdata(archive)
    assert [c.name for c in data.cases] == ["1", "2", "3"]
    assert data.total_score == 10 + 40


@pytest.mark.parametrize("files, message", [
    ({"1.in": ""}, "没有对应的答案"),
    ({"a.txt": ""}, "没有测试点"),
    ({"../x.in": "", "../x.ans": ""}, "不安全"),
    ({"1.in": "", "1.ans": "", "config.json": "{"}, "JSON"),
    ({"1.in": "", "1.ans": "", "config.json": '{"subtasks":[{"score":1,"cases":["9"]}]}'}, "不存在"),
])
def test_数据包结构不对当场报出来(tmp_path, files, message) -> None:
    with zipfile.ZipFile(str(make_zip(tmp_path / "bad.zip", files))) as archive:
        with pytest.raises(testdata.TestdataError, match=message):
            testdata.parse_testdata(archive)


def test_比对忽略行末空白与文末空行() -> None:
    assert runner.compare_text(b"1 2 \r\n3\n\n\n", b"1 2\n3")
    assert not runner.compare_text(b"1 2\n3\n", b"1 2\n4\n")
    assert not runner.compare_text(b"12\n", b"1 2\n")


# ---------------------------------------------------------------- 评测 ------ #


@needs_gcc
def test_正确的程序满分(tmp_path) -> None:
    outcome = judge(tmp_path, AB, ab_data(tmp_path))
    assert (outcome.score, outcome.max_score, outcome.status) == (100, 100, "AC")
    assert [c.status for c in outcome.cases] == ["AC"] * 3


@needs_gcc
def test_错误超时运行错误超内存编译错误(tmp_path) -> None:
    data = ab_data(tmp_path)
    wrong = AB.replace("a+b", "a-b")
    assert judge(tmp_path, wrong, data).status == "WA"
    tle = judge(tmp_path, "int main(){volatile long x=0;for(;;)x++;}", data, time_limit_ms=300)
    assert tle.status == "TLE" and tle.score == 0
    re_ = judge(tmp_path, "int main(){int*p=0;*p=1;}", data)
    assert re_.status == "RE"
    # volatile：否则 GCC 会把"分配了只读一个字节"整段优化掉
    mle = judge(tmp_path, "#include <cstdlib>\nint main(){volatile char*p=(volatile char*)malloc(200<<20);"
                          "for(long i=0;i<(200<<20);i+=4096)p[i]=1;return 0;}", data, memory_limit_mb=64)
    assert mle.status in ("MLE", "RE"), mle.status
    ce = judge(tmp_path, "int main( {", data)
    assert ce.status == "CE" and "error" in ce.compile_message


@needs_gcc
def test_文件IO忘了freopen判错并说明原因(tmp_path) -> None:
    outcome = judge(tmp_path, AB, ab_data(tmp_path), io_mode="file")
    assert outcome.status == "WA" and "freopen" in outcome.cases[0].message
    with_freopen = AB.replace("int main(){", "int main(){freopen(\"ab.in\",\"r\",stdin);freopen(\"ab.out\",\"w\",stdout);")
    assert judge(tmp_path, with_freopen, ab_data(tmp_path), io_mode="file").status == "AC"


@needs_gcc
def test_文件IO用自定义文件名(tmp_path) -> None:
    custom = AB.replace("int main(){", "int main(){freopen(\"sum.in\",\"r\",stdin);freopen(\"sum.txt\",\"w\",stdout);")
    names = {"io_mode": "file", "input_file": "sum.in", "output_file": "sum.txt"}
    assert judge(tmp_path, custom, ab_data(tmp_path), **names).status == "AC"
    # 还按 ident 读写的程序读不到输入、也写不出指定的输出文件
    by_ident = AB.replace("int main(){", "int main(){freopen(\"ab.in\",\"r\",stdin);freopen(\"ab.out\",\"w\",stdout);")
    outcome = judge(tmp_path, by_ident, ab_data(tmp_path), **names)
    assert outcome.status != "AC" and outcome.score == 0


@needs_gcc
def test_文件IO可以用input_txt与output_txt(tmp_path) -> None:
    code = AB.replace("int main(){", "int main(){freopen(\"input.txt\",\"r\",stdin);freopen(\"output.txt\",\"w\",stdout);")
    names = {"io_mode": "file", "input_file": "input.txt", "output_file": "output.txt"}
    assert judge(tmp_path, code, ab_data(tmp_path), **names).status == "AC"
    checker = ("#include <cstdio>\nint main(int c,char**v){FILE*o=fopen(v[2],\"r\"),*a=fopen(v[3],\"r\");"
               "long long x,y;fscanf(o,\"%lld\",&x);fscanf(a,\"%lld\",&y);return x==y?0:1;}")
    assert judge(tmp_path, code, ab_data(tmp_path, {"checker.cpp": checker}), **names).status == "AC"


@needs_gcc
def test_程序不能跨测试点留东西(tmp_path) -> None:
    sneaky = ("#include <cstdio>\nint main(){FILE*f=fopen(\"mark\",\"r\");if(f){puts(\"0\");return 0;}"
              "fopen(\"mark\",\"w\");long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}")
    # 源码预检会拦下打开 "mark"：这里要的是沙箱本身的行为，关掉这条规则
    data = ab_data(tmp_path, {"config.json": json.dumps({"precheck": {"disabled": ["file_access"]}})})
    assert judge(tmp_path, sneaky, data).status == "AC"


@needs_gcc
def test_checker与子任务(tmp_path) -> None:
    checker = ("#include <cstdio>\nint main(int c,char**v){FILE*o=fopen(v[2],\"r\"),*a=fopen(v[3],\"r\");"
               "long long x,y;fscanf(o,\"%lld\",&x);fscanf(a,\"%lld\",&y);if(x==y)return 0;"
               "fprintf(stderr,\"expected %lld\",y);return 1;}")
    config = {"subtasks": [{"score": 60, "cases": ["1", "2"]}, {"score": 40, "cases": ["10"]}]}
    data = ab_data(tmp_path, {"checker.cpp": checker, "config.json": json.dumps(config)})
    good = judge(tmp_path, AB.replace("\\n", "  \\n"), data)
    assert good.score == 100 and good.status == "AC"
    half = judge(tmp_path, AB.replace("printf", "if(a==5)a=0;printf"), data)
    assert half.score == 40 and half.status == "WA"
    assert "expected 12" in half.cases[1].message


def test_rlimit后端以root运行必须换账号(tmp_path) -> None:
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("只在 root 下有意义")
    with pytest.raises(sandbox.SandboxError, match="run-as"):
        sandbox.RlimitSandbox(base=tmp_path)


# ---------------------------------------------------------------- isolate --- #

FAKE_ISOLATE = r'''#!/usr/bin/env python3
import os, subprocess, sys, time, resource
root = os.environ["FAKE_ISOLATE_ROOT"]
args = sys.argv[1:]
box = os.path.join(root, [a for a in args if a.startswith("--box-id=")][0].split("=")[1])
if "--init" in args:
    os.makedirs(os.path.join(box, "box"), exist_ok=True); print(box); sys.exit(0)
if "--cleanup" in args:
    sys.exit(0)
opts = dict(a[2:].split("=", 1) for a in args[:args.index("--")] if a.startswith("--") and "=" in a)
with open(os.path.join(root, "last-command"), "w") as f: f.write(" ".join(args))
cmd = args[args.index("--") + 1:]
cwd = os.path.join(box, "box")
def o(name, mode):
    return open(os.path.join(cwd, opts[name]), mode) if name in opts else subprocess.DEVNULL
start = time.time()
try:
    p = subprocess.run(cmd, cwd=cwd, stdin=o("stdin", "rb"), stdout=o("stdout", "wb"), stderr=o("stderr", "wb"),
                       timeout=float(opts["wall-time"]))
    status = None if p.returncode == 0 else ("SG" if p.returncode < 0 else "RE")
    code = p.returncode
except subprocess.TimeoutExpired:
    status, code = "TO", None
usage = resource.getrusage(resource.RUSAGE_CHILDREN)
with open(opts["meta"], "w") as m:
    m.write("time:%.3f\ntime-wall:%.3f\nmax-rss:%d\n" % (usage.ru_utime, time.time() - start, usage.ru_maxrss))
    if status == "SG": m.write("exitsig:%d\nstatus:SG\n" % -code)
    elif status: m.write("status:%s\n" % status)
    if code is not None and code >= 0: m.write("exitcode:%d\n" % code)
'''


@needs_gcc
def test_isolate后端_命令行与meta(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "isolate"
    fake.write_text(FAKE_ISOLATE, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_ISOLATE_ROOT", str(tmp_path / "boxes"))
    source = tmp_path / "ab.cpp"
    source.write_text(AB, encoding="utf-8")
    outcome = runner.judge(source, "ab.cpp", ab_data(tmp_path), runner.ProblemConfig(ident="ab"),
                           lambda: sandbox.IsolateSandbox(box_id=3, binary=str(fake)))
    assert outcome.status == "AC" and outcome.isolated
    last = (tmp_path / "boxes" / "last-command").read_text(encoding="utf-8")
    assert "--box-id=3" in last and "--time=1.000" in last and "--processes=1" in last


def test_isolate_meta解析() -> None:
    box = sandbox.IsolateSandbox.__new__(sandbox.IsolateSandbox)
    limits = sandbox.Limits(cpu_seconds=1, wall_seconds=3, memory_bytes=64 << 20)
    meta = sandbox.parse_isolate_meta("time:1.2\ntime-wall:1.3\nmax-rss:1000\nstatus:TO\n")
    assert box._result_from_meta(meta, limits).status == "TLE"
    assert box._result_from_meta({"status": "XX", "message": "x"}, limits).status == "SE"
    assert box._result_from_meta({"cg-oom-killed": "1", "status": "SG", "exitsig": "9"}, limits).status == "MLE"
    assert box._result_from_meta({"status": "SG", "exitsig": "25"}, limits).status == "OLE"


@needs_gcc
def test_checker报告自身失败不能当成选手错(tmp_path) -> None:
    """testlib 的退出码 3（FAIL）是 checker / 答案文件坏了：交给评测机记 SE，不判 WA。"""
    data = ab_data(tmp_path, {"checker.cpp": "int main(){return 3;}"})
    with pytest.raises(sandbox.SandboxError, match="退出码 3"):
        judge(tmp_path, AB, data)
