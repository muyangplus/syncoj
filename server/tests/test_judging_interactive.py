"""交互题（§12.15）：testlib interactor 与选手程序双向管道、判定表、管道与 fd 的回归。

真编译、真运行的用例用 rlimit 后端（需要 g++；root 下选手程序换成 nobody），与
``test_judging_runner.py`` 同一套做法。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import pytest

from syncoj_server.judging import interact, runner, sandbox, testdata, verdict

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


# ------------------------------------------------------------ 交互题 -------- #

INTERACTOR = r'''#include "testlib.h"
#include <iostream>
using namespace std;
int main(int argc,char**argv){registerInteraction(argc,argv);int n=inf.readInt();int q=0;
while(true){int x=ouf.readInt();q++;if(q>40)quitf(_wa,"too many queries");
if(x==n){cout<<"="<<endl;break;}cout<<(x<n?"<":">")<<endl;}
tout<<q<<endl;if(q>12)quitp(0.5,"%d queries",q);quitf(_ok,"%d queries",q);}
'''
GUESS = r'''#include <cstdio>
int main(){int lo=1,hi=1000;while(true){int m=(lo+hi)/2;printf("%d\n",m);fflush(stdout);char r[4];
if(scanf("%3s",r)!=1)return 0;if(r[0]=='=')return 0;if(r[0]=='<')lo=m+1;else hi=m-1;}}
'''
LINEAR = r'''#include <cstdio>
int main(){for(int m=1;;m++){printf("%d\n",m);fflush(stdout);char r[4];if(scanf("%3s",r)!=1)return 0;
if(r[0]=='=')return 0;}}
'''


def guess_files(**extra):
    files = {"1.in": "500\n", "2.in": "17\n", "interactor.cpp": INTERACTOR}
    files.update(extra)
    return files


@needs_gcc
def test_交互_猜数_AC与部分分与WA(tmp_path) -> None:
    good = judge(tmp_path, GUESS, guess_files(), problem_type="interactive")
    assert good.status == "AC" and good.score == 100, good
    assert "interactor_time_ms" in good.to_result_json()["cases"][0]
    slow = judge(tmp_path, LINEAR, {"1.in": "17\n", "interactor.cpp": INTERACTOR}, problem_type="interactive")
    assert slow.status == "PA" and slow.score == 50
    lost = judge(tmp_path, LINEAR, {"1.in": "900\n", "interactor.cpp": INTERACTOR}, problem_type="interactive")
    assert lost.status == "WA" and "too many" in lost.cases[0].message


@needs_gcc
def test_交互_选手超时与空等(tmp_path) -> None:
    busy = "int main(){volatile long x=0;for(;;)x++;}"
    outcome = judge(tmp_path, busy, guess_files(), problem_type="interactive", time_limit_ms=300)
    assert outcome.status == "TLE"
    # 不 fflush：双方都在等对方 —— 墙钟打破僵局，判 TLE（空等）
    no_flush = GUESS.replace("fflush(stdout);", "")
    started = time.monotonic()
    outcome = judge(tmp_path, no_flush, {"1.in": "500\n", "interactor.cpp": INTERACTOR},
                    problem_type="interactive", time_limit_ms=500)
    assert outcome.status == "TLE" and "fflush" in outcome.cases[0].message
    assert time.monotonic() - started < 2 * 0.5 + 1 + 3 + 15  # 含编译时间


@needs_gcc
def test_交互_交互器判错与交互器超时记SE(tmp_path) -> None:
    fail = r'''#include "testlib.h"
int main(int argc,char**argv){registerInteraction(argc,argv);quitf(_fail,"bad data");}'''
    with pytest.raises(sandbox.SandboxError, match="bad data"):
        judge(tmp_path, GUESS, {"1.in": "1\n", "interactor.cpp": fail}, problem_type="interactive")
    stuck = r'''#include "testlib.h"
int main(int argc,char**argv){registerInteraction(argc,argv);volatile long x=0;for(;;)x++;}'''
    quick = "#include <cstdio>\nint main(){puts(\"1\");return 0;}"
    with pytest.raises(sandbox.SandboxError, match="interactor"):
        judge(tmp_path, quick, {"1.in": "1\n", "interactor.cpp": stuck}, problem_type="interactive",
              time_limit_ms=300)


@needs_gcc
def test_交互_选手不读输出也不死锁(tmp_path) -> None:
    """interactor 先写一大段给选手，选手一个字也不读就退出：interactor 写断管道，判 WA 而不卡住。"""
    flood = r'''#include "testlib.h"
#include <cstdio>
int main(int argc,char**argv){registerInteraction(argc,argv);
for(int i=0;i<200000;i++)fputs("0123456789\n",stdout);fflush(stdout);int x=ouf.readInt();quitf(_ok,"%d",x);}'''
    quiet = "#include <cstdio>\nint main(){puts(\"1\");return 0;}"
    started = time.monotonic()
    outcome = judge(tmp_path, quiet, {"1.in": "1\n", "interactor.cpp": flood}, problem_type="interactive")
    assert outcome.status == "WA" and "提前结束" in outcome.cases[0].message
    assert time.monotonic() - started < 30


@needs_gcc
def test_交互_交互结束后选手仍在输出判RE(tmp_path) -> None:
    chatty = GUESS.replace("if(r[0]=='=')return 0;", "if(r[0]=='='){for(;;){printf(\"x\\n\");fflush(stdout);}}")
    outcome = judge(tmp_path, chatty, {"1.in": "500\n", "interactor.cpp": INTERACTOR}, problem_type="interactive")
    assert outcome.status == "RE"


@needs_gcc
def test_交互加checker(tmp_path) -> None:
    check = r'''#include "testlib.h"
int main(int argc,char**argv){registerTestlibCmd(argc,argv);int q=ouf.readInt();
if(q<=10)quitf(_ok,"%d",q);quitp(0.2,"%d queries",q);}'''
    files = guess_files(**{"checker.cpp": check})
    outcome = judge(tmp_path, GUESS, files, problem_type="interactive")
    assert outcome.status == "AC"
    slow = judge(tmp_path, LINEAR, {"1.in": "11\n", "interactor.cpp": INTERACTOR, "checker.cpp": check},
                 problem_type="interactive")
    assert slow.status == "PA" and slow.score == 20


def _rr(status, sig=None, code=0, cpu=0.0):
    return sandbox.RunResult(status, code, sig, cpu)


@pytest.mark.parametrize("user, inter, first, grace, kind, expected", [
    (_rr("OK"), _rr("TLE"), "user", False, "ok", "SE"),
    (_rr("OK"), _rr("OK"), "user", True, "ok", "SE"),
    (_rr("TLE", 9, None, 1.0), _rr("RE", None, 1), "interactor", False, "wa", "WA"),
    (_rr("TLE", 9, None, 1.0), _rr("OK"), "user", False, "ok", "TLE"),
    (_rr("MLE"), _rr("OK"), "user", False, "ok", "MLE"),
    (_rr("RE", 11, None), _rr("OK"), "user", False, "ok", "RE"),
    (_rr("RE", 13, None), _rr("OK"), "interactor", False, "ok", "RE"),
    (_rr("RE", 13, None), _rr("RE", None, 1), "interactor", False, "wa", "WA"),
    (_rr("RE", 13, None), _rr("RE", None, 3), "interactor", False, "fail", "SE"),
    (_rr("OK"), _rr("RE", 13, None), "user", False, None, "WA"),
    (_rr("OK"), _rr("RE", None, 3), "user", False, "fail", "SE"),
    (_rr("OK"), _rr("OK"), "interactor", False, "ok", "AC"),
    (_rr("OK"), _rr("RE", None, 7), "interactor", False, "partial", "PA"),
])
def test_交互判定表(user, inter, first, grace, kind, expected) -> None:
    pair = interact.PairResult(user, inter, first, grace, 0, 1.0)
    result = None if kind is None else verdict.Verdict(kind, {"ok": 1.0, "partial": 0.5}.get(kind, 0.0), "m")
    if expected == "SE":
        with pytest.raises(sandbox.SandboxError):
            interact.decide(pair, result, False)
    else:
        assert interact.decide(pair, result, False)[0] == expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 管道")
def test_run_pair_回显不泄漏fd(tmp_path) -> None:
    """cat 回显 + 50 次连跑：不死锁，本进程打开的 fd 数不增长。"""
    make = factory(tmp_path)
    user, peer = make(), None
    try:
        peer = user.peer()
        limits = sandbox.Limits(cpu_seconds=2, wall_seconds=3, memory_bytes=256 << 20)
        fd_dir = "/proc/self/fd"
        before = len(os.listdir(fd_dir)) if os.path.isdir(fd_dir) else 0
        for _ in range(50):
            pair = interact.run_pair(user, ["/bin/true"], limits, peer, ["/bin/cat"], limits, inter_stderr="e")
            assert pair.user.status == "OK" and pair.interactor.status == "OK"
        if before:
            assert len(os.listdir(fd_dir)) <= before + 2
    finally:
        if peer is not None:
            peer.close()
        user.close()
