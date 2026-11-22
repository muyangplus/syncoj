"""Windows 沙箱后端 winjob（§12）。

纯逻辑（退出码、状态判定、Job 限制、编译参数、后端选择）在任何平台都跑；
``windows`` 标记的用例真编译、真运行，只在 Windows + MinGW-w64 g++ 上跑（CI 的 Windows job）。
"""

from __future__ import annotations

import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from syncoj_server.judging import runner, sandbox, sandbox_win as sw

MB = 1024 * 1024
LIMITS = sandbox.Limits(cpu_seconds=1.0, wall_seconds=3.0, memory_bytes=256 * MB, output_bytes=64 * MB)


def classify(**kwargs):
    base = dict(limits=LIMITS, cpu=0.1, peak=8 * MB, exit_code=0, killed_by_wall=False, output_exceeded=False)
    base.update(kwargs)
    return sw.classify(**base)


# ------------------------------------------------------------ 退出码与判定 ---- #


def test_退出码_普通码照旧_NTSTATUS写名称() -> None:
    assert sw.describe_exit_code(1) == "退出码 1"
    assert sw.describe_exit_code(0xC0000005) == "退出码 0xC0000005（访问违例）"
    assert "栈溢出" in sw.describe_exit_code(0xC00000FD)
    assert "整数除以零" in sw.describe_exit_code(0xC0000094)
    assert sw.describe_exit_code(0xC0001234) == "退出码 0xC0001234"
    # 有符号形式（-1073741819 == 0xC0000005）
    assert "访问违例" in sw.describe_exit_code(-1073741819)
    assert sw.describe_exit_code(None) == "没有退出码"


def test_判定_正常与各类错误() -> None:
    assert classify() == ("OK", "")
    assert classify(exit_code=1) == ("RE", "退出码 1")
    status, message = classify(exit_code=0xC00000FD)
    assert status == "RE" and "栈溢出" in message


def test_判定_顺序与Linux后端一致() -> None:
    assert classify(output_exceeded=True, killed_by_wall=True)[0] == "OLE"
    assert classify(killed_by_wall=True, peak=999 * MB)[0] == "TLE"
    assert classify(cpu=1.2, exit_code=1)[0] == "TLE"
    assert classify(peak=257 * MB, exit_code=0)[0] == "MLE"


def test_判定_接近内存上限时崩溃算MLE() -> None:
    # new 失败 → bad_alloc 没接住 → abort()（退出码 3）
    assert classify(peak=250 * MB, exit_code=3)[0] == "MLE"
    # malloc 返回空指针再解引用
    assert classify(peak=240 * MB, exit_code=0xC0000005)[0] == "MLE"
    # 离上限远：就是普通的运行错误
    assert classify(peak=10 * MB, exit_code=0xC0000005)[0] == "RE"
    # 正常的非零退出码不算崩溃
    assert classify(peak=250 * MB, exit_code=1)[0] == "RE"
    assert classify(peak=1 * MB, exit_code=0xC0000017)[0] == "MLE"


def test_判定_程序没能启动记SE() -> None:
    status, message = classify(exit_code=0xC0000135)
    assert status == "SE" and "DLL" in message


def test_Job限制_选手程序与编译() -> None:
    run = sw.job_settings(LIMITS)
    assert run["flags"] & sw.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    assert run["flags"] & sw.JOB_OBJECT_LIMIT_PROCESS_MEMORY
    assert run["flags"] & sw.JOB_OBJECT_LIMIT_PROCESS_TIME
    assert run["flags"] & sw.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert run["flags"] & sw.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    assert run["active_processes"] == 1
    assert run["process_memory"] == 256 * MB + sw.MEMORY_MARGIN
    assert run["process_time"] == int((1.0 + sw.CPU_MARGIN_SECONDS) * 10000000)
    assert run["job_memory"] == run["job_time"] == 0

    build = sw.job_settings(runner.COMPILE_LIMITS)
    assert not build["flags"] & sw.JOB_OBJECT_LIMIT_ACTIVE_PROCESS  # cc1plus / as / ld
    assert build["flags"] & sw.JOB_OBJECT_LIMIT_JOB_MEMORY
    assert build["flags"] & sw.JOB_OBJECT_LIMIT_JOB_TIME
    assert build["process_memory"] == build["active_processes"] == 0


def test_找程序_相对工作目录或PATH(tmp_path: Path) -> None:
    (tmp_path / "prog.exe").write_bytes(b"")
    assert sw.resolve_program("./prog.exe", tmp_path, None) == str((tmp_path / "prog.exe").resolve())
    assert sw.resolve_program("./prog", tmp_path, None) == str((tmp_path / "prog.exe").resolve())
    assert sw.resolve_program("./nope", tmp_path, None) is None
    assert sw.resolve_program("no-such-compiler-xyz", tmp_path, str(tmp_path)) is None


def test_环境_只留必需项_临时目录指向工作目录(tmp_path: Path) -> None:
    env = sw.build_environment(tmp_path, {"SystemRoot": r"C:\Windows", "Path": r"C:\mingw64\bin", "SECRET": "x"})
    assert env == {"SYSTEMROOT": r"C:\Windows", "PATH": r"C:\mingw64\bin",
                   "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    assert sw.environment_block({"b": "2", "A": "1"}) == "A=1\0b=2\0\0"


def test_非Windows上不能建winjob沙箱(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows 上能建")
    with pytest.raises(sandbox.SandboxError, match="只能在 Windows"):
        sandbox.make_sandbox("winjob", base=tmp_path)


# ------------------------------------------------------------ 编译参数 ---- #


def test_编译参数_Linux原样_Windows加exe静态链接与大栈() -> None:
    argv = ["g++", "-O2", "-std=c++14", "-o", "prog", "main.cpp", "-lm"]
    assert runner.platform_compile_argv(argv, windows=False) == argv
    assert runner.platform_compile_argv(argv, windows=True, stack_bytes=512 * MB) == [
        "g++", "-O2", "-std=c++14", "-o", "prog.exe", "main.cpp", "-lm", "-static", "-Wl,--stack=%d" % (512 * MB),
    ]
    assert runner.platform_compile_argv(["g++", "-o", "chk.exe", "c.cpp"], windows=True)[2] == "chk.exe"
    assert runner.exe_name("prog", windows=True) == "prog.exe"
    assert runner.exe_name("prog", windows=False) == "prog"


# ------------------------------------------------------------ 后端选择 ---- #


def test_默认后端随平台() -> None:
    assert sandbox.default_backend("win32") == "winjob"
    assert sandbox.default_backend("linux") == "isolate"


@pytest.mark.parametrize("backend,platform,run_as,ok", [
    ("isolate", "linux", None, True),
    ("rlimit", "linux", "nobody", True),
    ("winjob", "linux", None, False),
    ("winjob", "win32", None, True),
    ("winjob", "win32", "judge", False),
    ("isolate", "win32", None, False),
    ("rlimit", "win32", None, False),
    ("isolate", "darwin", None, False),
    ("winjob", "cygwin", None, False),
])
def test_后端与平台的组合(backend, platform, run_as, ok) -> None:
    assert (sandbox.backend_problem(backend, platform, run_as=run_as) is None) is ok


# ------------------------------------------------------------ Windows 端到端 ---- #

windows = pytest.mark.windows
needs_windows_gxx = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("g++") is None, reason="需要 Windows + MinGW-w64 g++",
)

#: CI 的 Windows job 设它：缺 g++ 时失败而不是悄悄跳过
REQUIRE_E2E = os.environ.get("SYNCOJ_REQUIRE_WINJOB") == "1"


def test_要求真跑时环境齐备() -> None:
    if not REQUIRE_E2E:
        pytest.skip("没设 SYNCOJ_REQUIRE_WINJOB")
    assert sys.platform == "win32"
    assert shutil.which("g++"), "PATH 里没有 MinGW-w64 g++"


AB = "#include <cstdio>\nint main(){long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n"


def _judge(tmp_path: Path, code: str, **config):
    data = tmp_path / "data.zip"
    with zipfile.ZipFile(str(data), "w") as archive:
        archive.writestr("1.in", "1 2\n")
        archive.writestr("1.ans", "3\n")
    source = tmp_path / "ab.cpp"
    source.write_text(code, encoding="utf-8")
    boxes = tmp_path / "boxes"
    boxes.mkdir(exist_ok=True)
    return runner.judge(source, "ab.cpp", data, runner.ProblemConfig(ident="ab", **config),
                        lambda: sw.WinJobSandbox(base=boxes))


@windows
@needs_windows_gxx
def test_win_端到端_AC(tmp_path: Path) -> None:
    outcome = _judge(tmp_path, AB)
    assert outcome.status == "AC", outcome
    assert outcome.score == 100 and outcome.isolated is False
    assert not list((tmp_path / "boxes").iterdir())  # 工作目录已清理


@windows
@needs_windows_gxx
def test_win_端到端_CE(tmp_path: Path) -> None:
    assert _judge(tmp_path, "int main(){ return }").status == "CE"


@windows
@needs_windows_gxx
def test_win_端到端_TLE(tmp_path: Path) -> None:
    outcome = _judge(tmp_path, "int main(){ volatile unsigned x = 0; for(;;) ++x; }", time_limit_ms=500)
    assert outcome.cases[0].status == "TLE", outcome


@windows
@needs_windows_gxx
def test_win_端到端_RE带可读名称(tmp_path: Path) -> None:
    outcome = _judge(tmp_path, "int main(){ volatile int *p = 0; *p = 1; return 0; }")
    assert outcome.cases[0].status == "RE", outcome
    assert "访问违例" in outcome.cases[0].message


@windows
@needs_windows_gxx
def test_win_端到端_MLE(tmp_path: Path) -> None:
    code = ("#include <cstring>\n#include <cstdlib>\nint main(){ for(;;){ char *p = (char*)malloc(16<<20);"
            " if(!p) return 1; memset(p, 1, 16<<20);} }")
    outcome = _judge(tmp_path, code, memory_limit_mb=64)
    assert outcome.cases[0].status == "MLE", outcome


@windows
@needs_windows_gxx
def test_win_端到端_深递归不栈溢出(tmp_path: Path) -> None:
    code = ("#include <cstdio>\nint f(int n){ volatile char pad[64]; pad[0] = (char)n;"
            " return n ? f(n - 1) + pad[0] - (char)n : 0; }\n"
            "int main(){ long long a,b; scanf(\"%lld%lld\",&a,&b); printf(\"%lld\\n\", a + b + f(1000000)); }")
    outcome = _judge(tmp_path, code, memory_limit_mb=512)
    assert outcome.status == "AC", outcome


@windows
@needs_windows_gxx
def test_win_端到端_OLE(tmp_path: Path) -> None:
    code = "#include <cstdio>\nint main(){ for(;;) fputs(\"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n\", stdout); }"
    outcome = _judge(tmp_path, code, output_limit_mb=1, time_limit_ms=3000)
    assert outcome.cases[0].status == "OLE", outcome


@windows
@needs_windows_gxx
def test_win_选手程序起不了子进程(tmp_path: Path) -> None:
    box = sw.WinJobSandbox(base=tmp_path)
    try:
        box.put("main.cpp", b"#include <cstdlib>\nint main(){ return std::system(\"cmd /c exit 0\") == 0 ? 1 : 0; }\n")
        built = box.run(runner.platform_compile_argv(["g++", "-O2", "-o", "prog", "main.cpp"]),
                        runner.COMPILE_LIMITS, stderr="err.txt")
        assert built.status == "OK", (box.workdir / "err.txt").read_text(errors="replace")
        result = box.run(["./prog.exe"], LIMITS)
        assert result.status == "OK", result
    finally:
        box.close()
