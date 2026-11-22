"""编译 → 逐点运行 → 比对 → 计分，产出 ``result.json`` 的内容（§12）。

产物写进现有的 ``judge_result/<场次>/<选手>/<题目>/result.json``，由 ``ResultScanner`` 收进成绩
矩阵 —— 扫描器只读 ``score`` / ``max_score`` / ``status``，``cases`` 等细节原样进 ``raw_json``。

NOI 惯例：

* C++ ``g++ -O2 -std=c++14 -lm``（可按题改），C ``gcc -O2 -std=c99 -lm``；编译也在沙箱里跑。
* 文件 IO（读 ``<题目>.in`` 写 ``<题目>.out``）或标准 IO，按题配置；文件 IO 没有输出文件判 WA
  并写清原因（多半是忘了 ``freopen``）。
* 默认比对：逐行比较，忽略行末空白与文末空行。数据包里有 ``checker.cpp`` 时用它
  （testlib 风格：``checker <in> <out> <ans>``，退出码 0 为通过）。
* 每个测试点前清空工作目录：程序不能跨测试点留东西。
* 编译前先做源码预检（:mod:`.precheck`）：有拦截项就不编译、记 CE；预警写进 ``precheck_warnings``。
* Windows（``winjob`` 后端，MinGW-w64）：产物带 ``.exe``、静态链接、主线程栈调大（:func:`platform_compile_argv`）。
"""

from __future__ import annotations

import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import precheck as pc
from . import sandbox as sb
from .sandbox_win import describe_exit_code
from .testdata import Testdata, parse_testdata

__all__ = [
    "ProblemConfig", "judge", "io_file_names", "compare_text", "STATUS_ORDER", "LANGUAGES",
    "exe_name", "platform_compile_argv",
]

AC, WA, CE = "AC", "WA", "CE"
#: 整题状态取"最严重"的那个
STATUS_ORDER = ["SE", "CE", "RE", "MLE", "TLE", "OLE", "WA", "AC"]

LANGUAGES = {
    ".cpp": ("cpp", "main.cpp", ["g++", "-O2", "-std=c++14", "-o", "prog", "main.cpp", "-lm"]),
    ".cc": ("cpp", "main.cpp", ["g++", "-O2", "-std=c++14", "-o", "prog", "main.cpp", "-lm"]),
    ".cxx": ("cpp", "main.cpp", ["g++", "-O2", "-std=c++14", "-o", "prog", "main.cpp", "-lm"]),
    ".c": ("c", "main.c", ["gcc", "-O2", "-std=c99", "-o", "prog", "main.c", "-lm"]),
}

COMPILE_LIMITS = sb.Limits(
    cpu_seconds=10.0, wall_seconds=30.0, memory_bytes=1024 * 1024 * 1024,
    output_bytes=64 * 1024 * 1024, processes=None,
)
CHECKER_LIMITS = sb.Limits(
    cpu_seconds=10.0, wall_seconds=20.0, memory_bytes=1024 * 1024 * 1024,
    output_bytes=16 * 1024 * 1024, processes=1,
)
MAX_MESSAGE = 4000
WINDOWS = sys.platform == "win32"
#: Windows 上没有题目内存限制可参照时（checker）的主线程栈大小
DEFAULT_STACK_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_COMPARE = 256 * 1024 * 1024


@dataclass
class ProblemConfig:
    ident: str
    time_limit_ms: int = 1000
    memory_limit_mb: int = 256
    io_mode: str = "stdio"  # stdio | file
    #: 文件 IO 的文件名；None = <ident>.in / <ident>.out
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None  # 追加到默认编译参数之后
    output_limit_mb: int = 64


@dataclass
class CaseResult:
    name: str
    status: str
    score: int = 0
    time_ms: int = 0
    memory_kb: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name, "status": self.status, "score": self.score,
            "time_ms": self.time_ms, "memory_kb": self.memory_kb, "message": self.message,
        }


@dataclass
class JudgeOutcome:
    score: int
    max_score: int
    status: str
    language: str = ""
    compile_message: str = ""
    cases: List[CaseResult] = field(default_factory=list)
    subtasks: List[Dict[str, object]] = field(default_factory=list)
    isolated: bool = False
    #: 源码预检的预警（``Finding.to_dict()``）；没有就不写进结果
    precheck_warnings: List[Dict[str, object]] = field(default_factory=list)

    def to_result_json(self) -> Dict[str, object]:
        document: Dict[str, object] = {
            "score": self.score,
            "max_score": self.max_score,
            "status": self.status,
            "judge": "syncoj-builtin",
            "isolated": self.isolated,
            "language": self.language,
            "compile_message": self.compile_message,
            "cases": [case.to_dict() for case in self.cases],
            "subtasks": self.subtasks,
        }
        if self.precheck_warnings:
            document["precheck_warnings"] = self.precheck_warnings
        return document


def exe_name(stem: str, windows: bool = WINDOWS) -> str:
    return stem + ".exe" if windows else stem


def platform_compile_argv(
    argv: List[str], *, windows: bool = WINDOWS, stack_bytes: int = DEFAULT_STACK_BYTES,
) -> List[str]:
    """Windows（MinGW-w64）上：``-o`` 的产物加 ``.exe``；``-static``（不依赖 MinGW 的 DLL，
    也不在 PATH 里找）；``-Wl,--stack`` 把主线程栈保留到 ``stack_bytes``（Linux 上栈不限，
    默认 1 MB 的栈会让深递归在 Windows 上栈溢出）。Linux 上原样返回。"""
    if not windows:
        return list(argv)
    result = []
    for index, item in enumerate(argv):
        if index > 0 and argv[index - 1] == "-o" and not item.lower().endswith(".exe"):
            item += ".exe"
        result.append(item)
    return result + ["-static", "-Wl,--stack=%d" % stack_bytes]


def worst(statuses: List[str]) -> str:
    present = set(statuses)
    for status in STATUS_ORDER:
        if status in present:
            return status
    return AC


def _normalized_lines(data: bytes) -> List[bytes]:
    lines = [line.rstrip(b" \t\r") for line in data.split(b"\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def compare_text(output: bytes, answer: bytes) -> bool:
    """逐行比较：忽略行末空白（空格、制表、``\\r``）与文末空行。"""
    return _normalized_lines(output) == _normalized_lines(answer)


def _truncate(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE] + "\n…（截断）"


def _read(box: sb.Sandbox, name: str, limit: int = MAX_OUTPUT_COMPARE) -> Optional[bytes]:
    path = box.workdir / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None


def judge(
    source: Path,
    source_name: str,
    testdata_zip: Path,
    config: ProblemConfig,
    sandbox_factory: Callable[[], sb.Sandbox],
) -> JudgeOutcome:
    """评一份代码。沙箱自己的问题抛 :class:`sandbox.SandboxError`（调用方记 SE、可重试）。"""
    suffix = Path(source_name).suffix.lower()
    if suffix not in LANGUAGES:
        return JudgeOutcome(0, 0, CE, compile_message="不支持的语言：%s（支持 C++ / C）" % source_name)
    language, main_name, compile_argv = LANGUAGES[suffix]
    compile_argv = platform_compile_argv(
        list(compile_argv) + (config.compile_flags.split() if config.compile_flags else []),
        stack_bytes=config.memory_limit_mb * 1024 * 1024,
    )

    with zipfile.ZipFile(str(testdata_zip)) as archive:
        data = parse_testdata(archive)
        report = pc.check_source(
            source.read_bytes(), language=language, rules=data.precheck, allowed_files=io_file_names(config),
        )
        warnings = [item.to_dict() for item in report.warnings]
        if report.blocked:
            return JudgeOutcome(0, data.total_score, CE, language, _truncate(report.blocked_message().encode("utf-8")),
                                precheck_warnings=warnings)
        box = sandbox_factory()
        try:
            outcome = _judge_in(box, archive, data, source, main_name, compile_argv, language, config)
        finally:
            box.close()
        outcome.precheck_warnings = warnings
        return outcome


def io_file_names(config: ProblemConfig) -> List[str]:
    """选手代码可以用字面量打开的文件名：``<题目>.in`` / ``<题目>.out`` 与题目配置的输入输出文件名。"""
    names = ["%s.in" % config.ident, "%s.out" % config.ident]
    names += [name for name in (config.input_file, config.output_file) if name]
    return names


def _judge_in(box, archive, data: Testdata, source, main_name, compile_argv, language, config):
    max_score = data.total_score
    box.put(main_name, source.read_bytes())
    result = box.run(compile_argv, COMPILE_LIMITS, stdout="compile.out", stderr="compile.err")
    compile_message = _truncate((_read(box, "compile.err", 1 << 20) or b"") + (_read(box, "compile.out", 1 << 20) or b""))
    prog, chk = exe_name("prog"), exe_name("chk")
    if result.status != sb.OK or not (box.workdir / prog).is_file():
        reason = {"TLE": "编译超时", "MLE": "编译超内存", "OLE": "编译输出过大"}.get(result.status, "")
        return JudgeOutcome(0, max_score, CE, language, (reason + "\n" + compile_message).strip(),
                            isolated=box.isolated)

    keep = [prog]
    if data.checker:
        for member in [data.checker] + data.checker_support:
            box.put(Path(member).name, archive.read(member))
        checker_name = Path(data.checker).name
        built = box.run(platform_compile_argv(["g++", "-O2", "-std=c++14", "-o", "chk", checker_name, "-lm"]),
                        COMPILE_LIMITS, stderr="checker.err")
        if built.status != sb.OK or not (box.workdir / chk).is_file():
            raise sb.SandboxError("checker 编译失败：%s" % _truncate(_read(box, "checker.err", 1 << 20) or b""))
        keep.append(chk)

    limits = sb.Limits(
        cpu_seconds=config.time_limit_ms / 1000.0,
        wall_seconds=config.time_limit_ms / 1000.0 * 2 + 1,
        memory_bytes=config.memory_limit_mb * 1024 * 1024,
        output_bytes=config.output_limit_mb * 1024 * 1024,
        processes=1,
    )
    file_io = config.io_mode == "file"
    in_name = (config.input_file or "%s.in" % config.ident) if file_io else "input.txt"
    out_name = (config.output_file or "%s.out" % config.ident) if file_io else "output.txt"

    cases: List[CaseResult] = []
    for case in data.cases:
        box.clean(keep)
        box.put(in_name, archive.read(case.input))
        run = box.run(
            ["./" + prog], limits,
            stdin=None if file_io else in_name,
            stdout=None if file_io else out_name,
        )
        item = CaseResult(
            case.name, run.status, 0, int(run.cpu_seconds * 1000), run.memory_bytes // 1024,
        )
        if run.status == sb.SE:
            raise sb.SandboxError("沙箱出错：%s" % run.message)
        if run.status == sb.OK:
            output = _read(box, out_name)
            if output is None:
                item.status = WA
                item.message = ("没有输出文件 %s（是不是忘了 freopen？）" % out_name) if file_io else "没有输出"
            elif data.checker:
                item.status, item.message = _run_checker(box, archive, case, output, keep)
            else:
                item.status = AC if compare_text(output, archive.read(case.answer)) else WA
        elif run.status == "RE" and run.signal:
            item.message = "被信号 %d 终止" % run.signal
        elif run.status == "RE":
            item.message = describe_exit_code(run.exit_code)
        if item.status == AC:
            item.score = case.score
        cases.append(item)

    by_name = {item.name: item for item in cases}
    subtask_results = []
    in_subtask = set()
    for index, sub in enumerate(data.subtasks, 1):
        passed = all(by_name[name].status == AC for name in sub.cases)
        subtask_results.append({"index": index, "score": sub.score if passed else 0, "max_score": sub.score,
                                "cases": sub.cases})
        in_subtask.update(sub.cases)
    for item in cases:
        if item.name in in_subtask:
            item.score = 0  # 捆绑测试：分算在子任务上
    score = sum(item.score for item in cases) + sum(int(s["score"]) for s in subtask_results)
    return JudgeOutcome(
        score, max_score, worst([item.status for item in cases]), language, compile_message,
        cases, subtask_results, isolated=box.isolated,
    )


def _run_checker(box, archive, case, output: bytes, keep: List[str]):
    box.clean(keep)
    box.put("in.txt", archive.read(case.input))
    box.put("out.txt", output)
    box.put("ans.txt", archive.read(case.answer))
    run = box.run(["./" + exe_name("chk"), "in.txt", "out.txt", "ans.txt"], CHECKER_LIMITS,
                  stdout="chk.out", stderr="chk.err")
    message = _truncate((_read(box, "chk.err", 1 << 16) or b"").strip())[:500]
    if run.status in (sb.TLE, sb.MLE, sb.OLE) or (run.signal is not None):
        raise sb.SandboxError("checker 自己出错（%s）：%s" % (run.status, message))
    # testlib 的退出码：0 通过、1 答案错、2 格式错、3 FAIL（checker 或答案文件本身有问题）、
    # 7 部分分（这里不支持，按错算）。3 与不认识的码绝不能当成"选手错了" —— 那会让一份坏数据
    # 把全场判成 WA 而没人察觉；交给评测机记 SE、重试、最后在进度里显示原因。
    if run.exit_code == 0:
        return AC, message
    if run.exit_code in (1, 2, 7):
        return WA, message
    raise sb.SandboxError("checker 报告自身失败（退出码 %s）：%s" % (run.exit_code, message))
