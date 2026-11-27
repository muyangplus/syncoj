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
* 每次运行选手程序后检查工作目录与沙箱的报告，记风险事件 ``risk_events``（符号链接 / FIFO /
  设备 / 硬链接、checker 同名文件或程序自身被改写、逃出进程组的进程）；源码预检的拦截项也记一条。
  风险事件只是标注，不改变判分。
* Windows（``winjob`` 后端，MinGW-w64）：产物带 ``.exe``、静态链接、主线程栈调大、``-pipe``（:func:`platform_compile_argv`）。
"""

from __future__ import annotations

import hashlib
import math
import sys
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import interact
from . import precheck as pc
from . import sandbox as sb
from . import verdict as vd
from .sandbox_win import describe_exit_code
from .testdata import TESTLIB_NAME, Case, Testdata, builtin_testlib, parse_testdata, read_member

__all__ = [
    "ProblemConfig", "judge", "judge_answers", "io_file_names", "compare_text", "compare_outputs",
    "STATUS_ORDER", "LANGUAGES", "COMPARE_MODES",
    "exe_name", "platform_compile_argv",
]

AC, WA, CE, PA, PE = "AC", "WA", "CE", "PA", "PE"
#: 整题状态取"最严重"的那个（PA = Partial Accepted，部分正确；PE = 格式错误）
STATUS_ORDER = ["SE", "CE", "RE", "MLE", "TLE", "OLE", "WA", "PE", "PA", "AC"]
#: io_mode = auto 时选手程序的标准输出写到这里（输出文件不存在时用它）
AUTO_STDOUT = "stdout.txt"

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
#: 出题人程序（checker / interactor / grader）的编译：testlib.h 很大，-O2 编一次要好几秒
TRUSTED_COMPILE_LIMITS = sb.Limits(
    cpu_seconds=60.0, wall_seconds=120.0, memory_bytes=2048 * 1024 * 1024,
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
    io_mode: str = "stdio"  # stdio | file | auto
    #: 文件 IO 的文件名；None = <ident>.in / <ident>.out
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None  # 追加到默认编译参数之后
    output_limit_mb: int = 64
    #: traditional | grader | answer_only | interactive（§12.13）
    problem_type: str = "traditional"
    #: 默认比较器的空白处理：strict | trailing_spaces | trailing_newlines | both
    compare_mode: str = "both"


@dataclass
class CaseResult:
    name: str
    status: str
    score: int = 0
    time_ms: int = 0
    memory_kb: int = 0
    message: str = ""
    #: 本点分值（子任务里的点也记它自己的分值）
    max_score: int = 0
    #: 得分比例 0..1（只在 PA 时写进结果）
    ratio: Optional[float] = None
    #: 题型相关的附加字段（答案文件名 ``file``、交互器用时 ``interactor_time_ms``）
    extra: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        document: Dict[str, object] = {
            "name": self.name, "status": self.status, "score": self.score,
            "time_ms": self.time_ms, "memory_kb": self.memory_kb, "message": self.message,
        }
        if self.status == PA and self.ratio is not None:
            document["ratio"] = round(self.ratio, 6)
            document["max_score"] = self.max_score
        for key, value in self.extra.items():
            if key not in ("risks", "escaped"):
                document[key] = value
        return document


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
    #: 风险事件（:class:`RiskEvent` 的 ``to_dict()``）；没有就不写进结果
    risk_events: List[Dict[str, object]] = field(default_factory=list)
    problem_type: str = "traditional"

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
        if self.risk_events:
            document["risk_events"] = self.risk_events
        if self.problem_type != "traditional":
            document["problem_type"] = self.problem_type
        return document


#: 风险事件最多记几条（每个测试点每种至多一条，再多就没有信息量了）
MAX_RISK_EVENTS = 50
#: 选手程序不该造出来的文件名：评测机放 checker 时用的名字
CHECKER_NAMES = frozenset(["chk", "chk.exe", "checker", "checker.exe"])
#: 检查工作目录时最多看多少个条目（选手程序可以造很多文件，检查本身不能卡住评测机）
MAX_WORKDIR_ENTRIES = 10000


@dataclass
class RiskEvent:
    """一条风险事件：``code`` 稳定（见 ``precheck.RISK_EVENT_CODES``），``case`` 是测试点名
    （源码预检拦截为 None），``line`` 只有预检拦截才有，``rule`` 是被拦下的预检规则。"""

    code: str
    message: str
    case: Optional[str] = None
    line: int = 0
    rule: str = ""
    excerpt: str = ""

    def to_dict(self) -> Dict[str, object]:
        document: Dict[str, object] = {"code": self.code, "case": self.case, "message": self.message}
        if self.line:
            document["line"] = self.line
        if self.rule:
            document["rule"] = self.rule
        if self.excerpt:
            document["excerpt"] = self.excerpt
        return document


def _sha256_fd_path(path: Path) -> Optional[str]:
    """工作目录里一个普通文件的 sha256（不跟随链接）；不是普通文件或打不开为 None。"""
    try:
        fd = sb.open_existing(path)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
        return digest.hexdigest()


def inspect_workdir(workdir: Path, case: str, prog: str, prog_sha: str) -> List[RiskEvent]:
    """选手程序运行之后看一眼工作目录：逃逸、篡改的痕迹各记一条。"""
    events: List[RiskEvent] = []
    odd: List[str] = []
    checkers: List[str] = []
    seen = 0
    for root, dirs, files in os.walk(str(workdir), followlinks=False):
        for name in list(dirs) + files:
            seen += 1
            if seen > MAX_WORKDIR_ENTRIES:
                break
            path = os.path.join(root, name)
            rel = os.path.relpath(path, str(workdir)).replace(os.sep, "/")
            try:
                info = os.lstat(path)
            except OSError:
                continue
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                odd.append("%s（符号链接）" % rel)
            elif stat.S_ISFIFO(mode):
                odd.append("%s（FIFO）" % rel)
            elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                odd.append("%s（设备文件）" % rel)
            elif stat.S_ISSOCK(mode):
                odd.append("%s（套接字）" % rel)
            elif stat.S_ISREG(mode) and info.st_nlink > 1:
                odd.append("%s（硬链接 %d）" % (rel, info.st_nlink))
            if name.lower() in CHECKER_NAMES:
                checkers.append(rel)
        if seen > MAX_WORKDIR_ENTRIES:
            break
    if odd:
        events.append(RiskEvent("workdir_symlink", "工作目录里出现 " + "、".join(odd[:5])
                                + ("等 %d 项" % len(odd) if len(odd) > 5 else ""), case))
    problems = ["写出了 " + "、".join(checkers[:5])] if checkers else []
    current = _sha256_fd_path(workdir / prog)
    if current != prog_sha:
        problems.append("%s 被%s" % (prog, "删除或替换" if current is None else "改写"))
    if problems:
        events.append(RiskEvent("tamper_checker", "；".join(problems), case))
    return events


def exe_name(stem: str, windows: bool = WINDOWS) -> str:
    return stem + ".exe" if windows else stem


def platform_compile_argv(
    argv: List[str], *, windows: bool = WINDOWS, stack_bytes: int = DEFAULT_STACK_BYTES, link: bool = True,
) -> List[str]:
    """Windows（MinGW-w64）上：``-o`` 的产物加 ``.exe``；``-static``（不依赖 MinGW 的 DLL，
    也不在 PATH 里找）；``-Wl,--stack`` 把主线程栈保留到 ``stack_bytes``（Linux 上栈不限，
    默认 1 MB 的栈会让深递归在 Windows 上栈溢出）；``-pipe``（编译各阶段走管道，少落临时文件）。
    Linux 上原样返回。"""
    if not windows:
        return list(argv)
    if not link:  # 只编译成目标文件（grader）：产物名不改，链接参数不加
        return list(argv) + ["-pipe"]
    result = []
    for index, item in enumerate(argv):
        if index > 0 and argv[index - 1] == "-o" and not item.lower().endswith(".exe"):
            item += ".exe"
        result.append(item)
    return result + ["-pipe", "-static", "-Wl,--stack=%d" % stack_bytes]


def worst(statuses: List[str]) -> str:
    present = set(statuses)
    for status in STATUS_ORDER:
        if status in present:
            return status
    return AC


COMPARE_MODES = ("strict", "trailing_spaces", "trailing_newlines", "both")


def _normalized_lines(data: bytes, mode: str = "both") -> List[bytes]:
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    if mode in ("trailing_spaces", "both"):
        lines = [line.rstrip(b" \t\r") for line in lines]
    if mode in ("trailing_newlines", "both"):
        while lines and not lines[-1]:
            lines.pop()
    return lines


def compare_text(output: bytes, answer: bytes, mode: str = "both") -> bool:
    """按比较方式逐行比较（§12.5）。所有方式都先把 ``\\r\\n`` 当 ``\\n``：

    ``strict`` 逐字节；``trailing_spaces`` 忽略行末空格与制表；``trailing_newlines`` 忽略文末空行；
    ``both``（默认）两者都忽略。"""
    return _normalized_lines(output, mode) == _normalized_lines(answer, mode)


def compare_outputs(output: bytes, answer: bytes, mode: str = "both") -> str:
    """默认比较器的结论：相等 AC；不相等但按空白切词后相同 PE；否则 WA。"""
    if compare_text(output, answer, mode):
        return AC
    if output.split() == answer.split():
        return PE
    return WA


def _truncate(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE] + "\n…（截断）"


def _read(box: sb.Sandbox, name: str, limit: int = MAX_OUTPUT_COMPARE) -> Optional[bytes]:
    # 不跟随符号链接，且只读普通文件：选手程序可能把输出文件换成指向别处的链接或 FIFO
    try:
        fd = sb.open_existing(box.workdir / name)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return handle.read(limit + 1)[:limit]


def _ratio_score(ratio: float, full: int) -> int:
    return int(math.floor(ratio * full + 1e-9))


# --------------------------------------------------------------------------- #
# 评测侧（trusted box）：checker / interactor / grader
# --------------------------------------------------------------------------- #


class _Trusted:
    """评测侧工作目录（§12.15）：用到时才建。出题人的程序在这里编译与运行，编译产物读进内存，
    每次运行前清空、重新放入，运行完再清空 —— 选手程序运行时这里什么都没有。"""

    def __init__(self, owner: sb.Sandbox, archive: zipfile.ZipFile, data: Testdata) -> None:
        self.owner = owner
        self.archive = archive
        self.data = data
        self._box: Optional[sb.Sandbox] = None
        self.checker: Optional[bytes] = None
        self.interactor: Optional[bytes] = None

    @property
    def box(self) -> sb.Sandbox:
        if self._box is None:
            self._box = self.owner.peer()
        return self._box

    def close(self) -> None:
        if self._box is not None:
            self._box.close()
            self._box = None

    def put_testlib(self, bundled: Optional[str]) -> None:
        if self.data.testlib_mode == "builtin" or not bundled:
            self.box.put(TESTLIB_NAME, builtin_testlib())

    def build(self, member: str, support: List[str], stem: str, what: str) -> bytes:
        """编译 checker / interactor（``g++ -O2 -std=gnu++17``），返回可执行文件的字节。"""
        box = self.box
        box.clean()
        for item in support:
            box.put(Path(item).name, self.archive.read(item))
        bundled = next((n for n in support if Path(n).name == TESTLIB_NAME), None)
        self.put_testlib(bundled)
        name = Path(member).name
        box.put(name, self.archive.read(member))
        built = box.run(platform_compile_argv(["g++", "-O2", "-std=gnu++17", "-o", stem, name, "-lm"]),
                        TRUSTED_COMPILE_LIMITS, stderr="checker.err")
        binary = exe_name(stem)
        data = _read(box, binary, 1 << 30) if built.status == sb.OK else None
        if data is None:
            raise sb.SandboxError("%s 编译失败：%s" % (what, _truncate(_read(box, "checker.err", 1 << 20) or b"")))
        box.clean()
        return data

    def prepare_checker(self) -> None:
        if self.data.checker and self.checker is None:
            self.checker = self.build(self.data.checker, self.data.checker_support, "chk", "checker")

    def check(self, case: Case, output: bytes, answer: Optional[bytes] = None) -> Tuple[str, float, str]:
        """跑 checker：返回 (状态, 比例, 说明)；checker 自己出错抛 SandboxError。"""
        data, box = self.data, self.box
        box.clean()
        chk = exe_name("chk")
        box.put(chk, self.checker or b"", 0o755)
        box.put("in.txt", self.archive.read(case.input))
        box.put("out.txt", output)
        box.put("ans.txt", answer if answer is not None else read_member(self.archive, case.answer))
        argv = ["./" + chk, "in.txt", "out.txt", "ans.txt"]
        if data.checker_protocol == "lemon":
            argv += [str(case.lemon_full()), "score.txt", "msg.txt"]
        try:
            run = box.run(argv, CHECKER_LIMITS, stdout="chk.out", stderr="chk.err")
            stderr = (_read(box, "chk.err", 1 << 16) or b"").decode("utf-8", "replace")
            message = _truncate(stderr.strip().encode("utf-8"))[:500]
            if run.status in (sb.TLE, sb.MLE, sb.OLE) or run.signal is not None:
                raise sb.SandboxError("checker 自己出错（%s）：%s" % (run.status, message))
            if data.checker_protocol == "lemon":
                score = _read(box, "score.txt", 1 << 12)
                extra = (_read(box, "msg.txt", 1 << 16) or b"").decode("utf-8", "replace")
                verdict = vd.parse_lemon(None if score is None else score.decode("utf-8", "replace"),
                                         case.lemon_full(), extra or stderr)
            elif data.checker_protocol == "exitcode":
                verdict = vd.parse_exitcode(run.exit_code, stderr)
            else:
                verdict = vd.parse_testlib(stderr, run.exit_code, data.checker_points)
        finally:
            box.clean()
        return _from_verdict(verdict)


def _from_verdict(verdict: vd.Verdict) -> Tuple[str, float, str]:
    message = verdict.message[:500]
    if verdict.kind == vd.FAIL:
        raise sb.SandboxError(message or "checker 报告自身失败")
    if verdict.kind == vd.OK:
        return AC, 1.0, message
    if verdict.kind == vd.PARTIAL:
        return PA, verdict.ratio, message
    if verdict.kind == vd.PE:
        return PE, 0.0, message
    return WA, 0.0, message


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def judge(
    source: Path,
    source_name: str,
    testdata_zip: Path,
    config: ProblemConfig,
    sandbox_factory: Callable[[], sb.Sandbox],
    on_phase: Optional[Callable[[str], None]] = None,
) -> JudgeOutcome:
    """评一份代码（传统 / 函数交互 / 交互题）。沙箱自己的问题抛 :class:`sandbox.SandboxError`
    （调用方记 SE、可重试）。``on_phase`` 在开始编译（``compiling``）与开始跑测试点（``judging``）时调用。"""
    phase = on_phase or (lambda _name: None)
    suffix = Path(source_name).suffix.lower()
    if config.problem_type == "answer_only":
        raise sb.SandboxError("提交答案题要用 judge_answers")
    if suffix not in LANGUAGES:
        return JudgeOutcome(0, 0, CE, compile_message="不支持的语言：%s（支持 C++ / C）" % source_name,
                            problem_type=config.problem_type)
    language, main_name, compile_argv = LANGUAGES[suffix]

    with zipfile.ZipFile(str(testdata_zip)) as archive:
        data = parse_testdata(archive, config.problem_type)
        report = pc.check_source(
            source.read_bytes(), language=language, rules=data.precheck, allowed_files=io_file_names(config),
        )
        warnings = [item.to_dict() for item in report.warnings]
        if report.blocked:
            risks = [
                RiskEvent("precheck_blocked", item.message, None, item.line, item.code, item.excerpt).to_dict()
                for item in report.blocked[:MAX_RISK_EVENTS]
            ]
            return JudgeOutcome(0, data.total_score, CE, language, _truncate(report.blocked_message().encode("utf-8")),
                                precheck_warnings=warnings, risk_events=risks, problem_type=data.problem_type)
        box = sandbox_factory()
        trusted = _Trusted(box, archive, data)
        try:
            phase("compiling")
            if data.problem_type == "grader":
                compiled = _compile_grader(box, trusted, data, source, main_name, compile_argv, language, config)
            else:
                compiled = _compile(box, source, main_name, compile_argv, config)
            if isinstance(compiled, str):
                outcome = JudgeOutcome(0, data.total_score, CE, language, compiled, isolated=box.isolated,
                                       problem_type=data.problem_type)
            else:
                binary, message = compiled
                trusted.prepare_checker()
                if data.problem_type == "interactive":
                    trusted.interactor = trusted.build(data.interactor, data.interactor_support, "interactor",
                                                       "interactor")
                phase("judging")
                outcome = _run_cases(box, trusted, archive, data, binary, config)
                outcome.language = language
                outcome.compile_message = message
        finally:
            trusted.close()
            box.close()
        outcome.precheck_warnings = warnings
        return outcome


def judge_answers(
    answers: Dict[str, Optional[Path]],
    names: Dict[str, str],
    testdata_zip: Path,
    config: ProblemConfig,
    sandbox_factory: Callable[[], sb.Sandbox],
    on_phase: Optional[Callable[[str], None]] = None,
) -> JudgeOutcome:
    """评提交答案题：``answers`` 是 测试点 → 选手的答案文件（没交为 None），``names`` 是答案文件名。

    不编译、不运行选手的东西；有 checker 时在评测侧工作目录里跑 checker，没有就用默认比较器。"""
    phase = on_phase or (lambda _name: None)
    with zipfile.ZipFile(str(testdata_zip)) as archive:
        data = parse_testdata(archive, "answer_only")
        box = sandbox_factory()
        trusted = _Trusted(box, archive, data)
        try:
            phase("judging")
            trusted.prepare_checker()
            isolated = trusted.box.isolated
            cases: List[CaseResult] = []
            for case in data.cases:
                name = names.get(case.name) or ""
                item = CaseResult(case.name, WA, max_score=case.score, extra={"file": name})
                path = answers.get(case.name)
                output = path.read_bytes() if path is not None and path.is_file() else None
                if output is None:
                    item.message = "没有提交答案文件 %s" % name
                elif data.checker:
                    item.status, item.ratio, item.message = trusted.check(case, output)
                else:
                    item.status = compare_outputs(output, read_member(archive, case.answer), config.compare_mode)
                    item.ratio = 1.0 if item.status == AC else 0.0
                cases.append(item)
        finally:
            trusted.close()
            box.close()
    outcome = _score(data, cases, isolated, [])
    outcome.problem_type = "answer_only"
    return outcome


def io_file_names(config: ProblemConfig) -> List[str]:
    """选手代码可以用字面量打开的文件名：``<题目>.in`` / ``<题目>.out`` 与题目配置的输入输出文件名。"""
    names = ["%s.in" % config.ident, "%s.out" % config.ident]
    names += [name for name in (config.input_file, config.output_file) if name]
    return names


def _compile_failed(result: sb.RunResult, message: str) -> str:
    reason = {"TLE": "编译超时", "MLE": "编译超内存", "OLE": "编译输出过大"}.get(result.status, "")
    return (reason + "\n" + message).strip()


def _compile(box, source: Path, main_name: str, compile_argv: List[str], config: ProblemConfig):
    """编译选手代码：成功返回 (可执行文件字节, 编译信息)，失败返回编译信息（CE）。"""
    argv = platform_compile_argv(
        list(compile_argv) + (config.compile_flags.split() if config.compile_flags else []),
        stack_bytes=config.memory_limit_mb * 1024 * 1024,
    )
    box.put(main_name, source.read_bytes())
    result = box.run(argv, COMPILE_LIMITS, stdout="compile.out", stderr="compile.err")
    message = _truncate((_read(box, "compile.err", 1 << 20) or b"") + (_read(box, "compile.out", 1 << 20) or b""))
    prog = exe_name("prog")
    binary = _read(box, prog, 1 << 30) if result.status == sb.OK else None
    if binary is None:
        return _compile_failed(result, message)
    return binary, message


def _compile_grader(box, trusted: _Trusted, data: Testdata, source: Path, main_name: str,
                    compile_argv: List[str], language: str, config: ProblemConfig):
    """函数交互题（§12.13）：grader 在评测侧单独编成目标文件（失败记 SE），选手代码在选手 box
    里只和头文件一起编（看不到 grader 源码，失败记 CE），最后链接（grader 在前，失败记 CE）。"""
    member = data.grader.get(language)
    if member is None:
        return "这道题的 grader 不支持 %s（只有 %s）" % (language, " / ".join(sorted(data.grader)))
    compiler, standard = compile_argv[0], compile_argv[2]
    flags = config.compile_flags.split() if config.compile_flags else []
    headers = [m for m in data.grader_files if m.lower().endswith((".h", ".hpp"))]
    extra_sources = [m for m in data.grader_files if m not in headers]

    tbox = trusted.box
    tbox.clean()
    for item in data.grader_files + [member]:
        tbox.put(Path(item).name, trusted.archive.read(item))
    trusted.put_testlib(None)
    objects: Dict[str, bytes] = {}
    for index, item in enumerate([member] + extra_sources):
        obj = "grader.o" if index == 0 else "grader%d.o" % index
        built = tbox.run(platform_compile_argv([compiler, "-c", "-O2", standard, "-DEVAL"] + flags
                                               + [Path(item).name, "-o", obj], link=False),
                         TRUSTED_COMPILE_LIMITS, stderr="checker.err")
        data_obj = _read(tbox, obj, 1 << 30) if built.status == sb.OK else None
        if data_obj is None:
            raise sb.SandboxError("grader 编译失败：%s" % _truncate(_read(tbox, "checker.err", 1 << 20) or b""))
        objects[obj] = data_obj
    tbox.clean()

    box.put(main_name, source.read_bytes())
    for item in headers:
        box.put(Path(item).name, trusted.archive.read(item))
    result = box.run(platform_compile_argv([compiler, "-c", "-O2", standard] + flags + [main_name, "-o", "user.o"],
                                           link=False),
                     COMPILE_LIMITS, stdout="compile.out", stderr="compile.err")
    message = _truncate((_read(box, "compile.err", 1 << 20) or b"") + (_read(box, "compile.out", 1 << 20) or b""))
    if result.status != sb.OK or _read(box, "user.o", 1 << 30) is None:
        return _compile_failed(result, message)
    for name, blob in objects.items():
        box.put(name, blob)
    link = platform_compile_argv([compiler, "-o", "prog"] + list(objects) + ["user.o", "-lm"],
                                 stack_bytes=config.memory_limit_mb * 1024 * 1024)
    linked = box.run(link, COMPILE_LIMITS, stdout="compile.out", stderr="compile.err")
    link_message = _truncate((_read(box, "compile.err", 1 << 20) or b"") + (_read(box, "compile.out", 1 << 20) or b""))
    binary = _read(box, exe_name("prog"), 1 << 30) if linked.status == sb.OK else None
    if binary is None:
        return _compile_failed(linked, ("链接失败（是不是自己写了 main，或者函数名、参数和题目要求的不一致？）\n"
                                        + link_message).strip())
    return binary, (message + ("\n" + link_message if link_message else "")).strip()


def _user_limits(config: ProblemConfig) -> sb.Limits:
    return sb.Limits(
        cpu_seconds=config.time_limit_ms / 1000.0,
        wall_seconds=config.time_limit_ms / 1000.0 * 2 + 1,
        memory_bytes=config.memory_limit_mb * 1024 * 1024,
        output_bytes=config.output_limit_mb * 1024 * 1024,
        processes=1,
    )


def _run_cases(box, trusted: _Trusted, archive, data: Testdata, binary: bytes, config: ProblemConfig) -> JudgeOutcome:
    # 编译产物在评测机内存里：选手程序能改写工作目录（换掉 checker 就能"永远 AC"），
    # 所以每次运行前清空工作目录、从这里重新放一份干净的，不在测试点之间留任何文件；
    # checker / interactor 在另一个工作目录（评测侧），选手 box 里只有 prog
    prog = exe_name("prog")
    prog_sha = hashlib.sha256(binary).hexdigest()
    limits = _user_limits(config)
    risks: List[RiskEvent] = []
    cases: List[CaseResult] = []
    for case in data.cases:
        box.clean()
        box.put(prog, binary, 0o755)
        if data.problem_type == "interactive":
            item = _interactive_case(box, trusted, archive, data, case, prog, prog_sha, limits)
            run_escaped = item.extra.pop("escaped", 0)
        else:
            item, run_escaped = _traditional_case(box, trusted, archive, data, case, prog, prog_sha, limits, config)
        if run_escaped:
            risks.append(RiskEvent("escaped_process", "清掉 %d 个逃出进程组的进程" % run_escaped, case.name))
        risks.extend(item.extra.pop("risks", []))
        cases.append(item)
    return _score(data, cases, box.isolated, risks)


def _traditional_case(box, trusted: _Trusted, archive, data: Testdata, case: Case, prog: str, prog_sha: str,
                      limits: sb.Limits, config: ProblemConfig):
    io_mode = config.io_mode
    file_in = (config.input_file or "%s.in" % config.ident)
    file_out = (config.output_file or "%s.out" % config.ident)
    if io_mode == "file":
        in_name, out_name, stdin, stdout = file_in, file_out, None, None
    elif io_mode == "auto":
        # 标准 IO 与 freopen 都能评：输入同时是 stdin 与输入文件，输出优先取输出文件
        in_name, out_name, stdin, stdout = file_in, file_out, file_in, AUTO_STDOUT
    else:
        in_name, out_name, stdin, stdout = "input.txt", "output.txt", "input.txt", "output.txt"
    box.put(in_name, archive.read(case.input))
    run = box.run(["./" + prog], limits, stdin=stdin, stdout=stdout)
    item = CaseResult(case.name, run.status, 0, int(run.cpu_seconds * 1000), run.memory_bytes // 1024,
                      max_score=case.score)
    if run.status == sb.SE:
        raise sb.SandboxError("沙箱出错：%s" % run.message)
    item.extra["risks"] = inspect_workdir(box.workdir, case.name, prog, prog_sha)
    if run.status == sb.OK:
        output = _read(box, out_name)
        if io_mode == "auto" and output is None:
            output = _read(box, AUTO_STDOUT)
        if output is None:
            item.status = WA
            item.message = ("没有输出文件 %s（是不是忘了 freopen？）" % out_name) if io_mode == "file" else "没有输出"
        elif data.checker:
            item.status, item.ratio, item.message = trusted.check(case, output)
        else:
            item.status = compare_outputs(output, read_member(archive, case.answer), config.compare_mode)
            item.ratio = 1.0 if item.status == AC else 0.0
            if item.status == PE:
                item.message = "只有空白（空格、换行）与答案不同"
    elif run.status == "RE" and run.signal:
        item.message = "被信号 %d 终止" % run.signal
    elif run.status == "RE":
        item.message = describe_exit_code(run.exit_code)
    return item, run.escaped


def _interactive_case(box, trusted: _Trusted, archive, data: Testdata, case: Case, prog: str, prog_sha: str,
                      limits: sb.Limits) -> CaseResult:
    tbox = trusted.box
    tbox.clean()
    inter = exe_name("interactor")
    tbox.put(inter, trusted.interactor or b"", 0o755)
    tbox.put("in.txt", archive.read(case.input))
    tbox.put("ans.txt", read_member(archive, case.answer))
    memory_mb = data.interactor_memory_mb or max(2 * limits.memory_bytes // (1024 * 1024), 256)
    inter_limits = sb.Limits(
        cpu_seconds=max(1.0, data.interactor_time_factor * limits.cpu_seconds),
        wall_seconds=limits.wall_seconds + 2,
        memory_bytes=int(memory_mb) * 1024 * 1024,
        output_bytes=64 * 1024 * 1024,
        processes=1,
    )
    try:
        pair = interact.run_pair(box, ["./" + prog], limits, tbox, ["./" + inter, "in.txt", "tout.txt", "ans.txt"],
                                 inter_limits, inter_stderr="inter.err")
        stderr = (_read(tbox, "inter.err", 1 << 16) or b"").decode("utf-8", "replace")
        verdict = vd.parse_testlib(stderr, pair.interactor.exit_code, data.checker_points) if (
            stderr.strip() or pair.interactor.exit_code is not None) else None
        tout = _read(tbox, "tout.txt", 64 * 1024 * 1024) or b""
    finally:
        tbox.clean()
    user = pair.user
    item = CaseResult(case.name, user.status, 0, int(user.cpu_seconds * 1000), user.memory_bytes // 1024,
                      max_score=case.score)
    item.extra["interactor_time_ms"] = int(pair.interactor.cpu_seconds * 1000)
    item.extra["escaped"] = pair.escaped
    item.extra["risks"] = inspect_workdir(box.workdir, case.name, prog, prog_sha)
    status, ratio, message, need_check = interact.decide(pair, verdict, bool(data.checker))
    item.status, item.ratio, item.message = status, ratio, _truncate(message.encode("utf-8"))[:500]
    if need_check:
        status, ratio2, message = trusted.check(case, tout)
        item.status, item.message = status, message
        item.ratio = min(ratio, ratio2) if status in (AC, PA) else 0.0
        if item.status == AC and item.ratio < 1:
            item.status = PA
    return item


def _score(data: Testdata, cases: List[CaseResult], isolated: bool, risks: List[RiskEvent]) -> JudgeOutcome:
    """每点得分 = floor(比例 × 分值)；子任务得分 = floor(子任务分 × 子任务内最小比例)，有点
    没过（不是 AC / PA）就是 0。"""
    for item in cases:
        if item.status not in (AC, PA):
            item.ratio = 0.0
        elif item.ratio is None:
            item.ratio = 1.0
        item.score = _ratio_score(item.ratio, item.max_score)
    by_name = {item.name: item for item in cases}
    subtask_results = []
    in_subtask = set()
    for index, sub in enumerate(data.subtasks, 1):
        members = [by_name[name] for name in sub.cases]
        ratio = min(item.ratio or 0.0 for item in members) if members else 0.0
        subtask_results.append({"index": index, "score": _ratio_score(ratio, sub.score), "max_score": sub.score,
                                "cases": sub.cases})
        in_subtask.update(sub.cases)
    for item in cases:
        if item.name in in_subtask:
            item.score = 0  # 捆绑测试：分算在子任务上
    score = sum(item.score for item in cases) + sum(int(s["score"]) for s in subtask_results)
    return JudgeOutcome(
        score, data.total_score, worst([item.status for item in cases]), cases=cases,
        subtasks=subtask_results, isolated=isolated,
        risk_events=[event.to_dict() for event in risks[:MAX_RISK_EVENTS]],
        problem_type=data.problem_type,
    )
