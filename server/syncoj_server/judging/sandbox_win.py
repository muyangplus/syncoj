"""Windows 沙箱后端 ``winjob``（§12）：Job Object 限资源，不隔离文件系统与网络。

做法：

* 每次运行建一个 Job：进程内存上限（编译用整个 Job 的内存上限）、活动进程数 1（选手程序与
  checker 起不了子进程；编译器要起 cc1plus / as / ld，不限）、用户态 CPU 时间上限（略高于
  时限，判 TLE 仍看实际 CPU 时间）、未处理异常直接结束（不弹 WER 对话框）、关 Job 即杀光、
  UI 限制（剪贴板、桌面、全局原子、关机等）。
* **自己调 CreateProcessW**（``CREATE_SUSPENDED``）拿到主线程句柄 → ``AssignProcessToJobObject``
  → ``ResumeThread``。程序在进 Job 之前一条指令都没执行，没有逃逸窗口。没用
  ``subprocess.Popen``：它不暴露主线程句柄，只能靠未公开的 ``NtResumeProcess`` 恢复。
  继承的句柄用 ``PROC_THREAD_ATTRIBUTE_HANDLE_LIST`` 限定为 stdin / stdout / stderr 三个。
* 工作目录的根必须是纯 ASCII 路径：MinGW 的 as / ld 用 ANSI API 打开文件，中文用户名的
  ``%TEMP%`` 会让编译失败。不是 ASCII 时依次退到 ``SYNCOJ_JUDGE_BOXES``、
  ``%ProgramData%\\SyncOJ\\judge\\boxes``、``%SystemDrive%\\syncoj-judge-boxes``（:func:`box_root_candidates`）。
* 计时：Job 记账的用户态 + 内核态 CPU 时间判 TLE；墙钟到 ``wall_seconds`` 就 ``TerminateJobObject``。
  内存：Job 记的峰值提交量（``PeakProcessMemoryUsed`` / 编译时 ``PeakJobMemoryUsed``）。
  输出：运行中每 25 ms 看一次工作目录增长，超过 ``output_bytes`` 就杀掉判 OLE。
* 不切换账号（Windows 上要密码），``isolated`` 恒为 False；评测机应以低权限专用账号运行。

本模块在 Linux 上也能导入（ctypes 只在用到时才装载 Win32 函数），纯逻辑在 Linux 上测试。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .sandbox import MLE, OK, OLE, RE, SE, TLE, Limits, RunResult, Sandbox, SandboxError

__all__ = [
    "WinJobSandbox",
    "NTSTATUS_NAMES",
    "describe_exit_code",
    "classify",
    "job_settings",
    "resolve_program",
    "build_environment",
    "environment_block",
    "ascii_safe",
    "box_root_candidates",
]

#: 常见的 NTSTATUS 退出码 → 可读名称（未处理异常时进程以异常码退出）
NTSTATUS_NAMES: Dict[int, str] = {
    0xC0000005: "访问违例",
    0xC00000FD: "栈溢出",
    0xC0000094: "整数除以零",
    0xC0000095: "整数溢出",
    0xC000008C: "数组越界",
    0xC000008E: "浮点除以零",
    0xC000008F: "浮点结果不精确",
    0xC0000090: "浮点无效运算",
    0xC0000091: "浮点上溢",
    0xC0000092: "浮点栈检查",
    0xC0000093: "浮点下溢",
    0xC000001D: "非法指令",
    0xC0000096: "特权指令",
    0xC0000409: "栈缓冲区溢出或快速失败（abort）",
    0xC0000374: "堆损坏",
    0xC0000017: "内存不足",
    0xC000012D: "提交内存超过上限",
    0xC0000135: "找不到 DLL",
    0xC0000142: "DLL 初始化失败",
    0xC000013A: "被 Ctrl+C 终止",
    0x80000003: "断点",
    0x80000002: "数据未对齐",
}
#: 评测机自己的问题（程序没真正跑起来）：记 SE，不算选手的分
SYSTEM_FAILURES = frozenset({0xC0000135, 0xC0000142})
#: 本身就意味着内存不够的退出码
OUT_OF_MEMORY = frozenset({0xC0000017, 0xC000012D})
#: C 运行库 ``abort()`` 的退出码（``std::bad_alloc`` 没接住也走这里）
ABORT_EXIT_CODE = 3

#: Job 内存上限比题目限制多给的余量（判 MLE 看峰值，不看这个上限）
MEMORY_MARGIN = 16 * 1024 * 1024
#: Job 的 CPU 时间上限比时限多给的余量
CPU_MARGIN_SECONDS = 1.0
#: 崩溃时峰值内存达到限制的这个比例，按"内存分配失败"判 MLE
NEAR_MEMORY_LIMIT = 0.9
POLL_SECONDS = 0.025

# Job 限制标志
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
#: 全部 UI 限制（句柄、剪贴板读写、系统参数、显示设置、全局原子、桌面、关机）
JOB_OBJECT_UILIMIT_ALL = 0x000000FF

COMPILERS = frozenset({"g++", "gcc", "c++", "cc"})

#: 覆盖工作目录根的环境变量（必须是纯 ASCII 路径）
BOXES_ENV = "SYNCOJ_JUDGE_BOXES"


def ascii_safe(path: str) -> bool:
    """MinGW 的 as / ld 用 ANSI API 开文件：只有纯 ASCII 路径在任何代码页下都安全。"""
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def box_root_candidates(base: Optional[str], environ: Mapping[str, str], tempdir: str) -> List[str]:
    """工作目录的根，按优先级：``base``（没给则系统临时目录）→ ``SYNCOJ_JUDGE_BOXES`` →
    ``%ProgramData%\\SyncOJ\\judge\\boxes`` → ``%SystemDrive%\\syncoj-judge-boxes``。只留纯 ASCII 的。"""
    upper = {key.upper(): value for key, value in environ.items()}
    items = [str(base) if base else tempdir]
    if upper.get(BOXES_ENV):
        items.append(upper[BOXES_ENV])
    if upper.get("PROGRAMDATA"):
        items.append(upper["PROGRAMDATA"].rstrip("\\/") + "\\SyncOJ\\judge\\boxes")
    items.append((upper.get("SYSTEMDRIVE") or "C:").rstrip("\\/") + "\\syncoj-judge-boxes")
    result: List[str] = []
    for item in items:
        if item and ascii_safe(item) and item not in result:
            result.append(item)
    return result


def _is_crash(code: Optional[int]) -> bool:
    """未处理异常（NTSTATUS 错误码）或 ``abort()``。"""
    if code is None:
        return False
    code &= 0xFFFFFFFF
    return code >= 0x80000000 or code == ABORT_EXIT_CODE


def describe_exit_code(code: Optional[int]) -> str:
    """退出码的可读形式。普通退出码照旧是"退出码 N"，NTSTATUS 写成十六进制并带名称。"""
    if code is None:
        return "没有退出码"
    unsigned = code & 0xFFFFFFFF
    if code < 0 or unsigned >= 0x80000000:
        name = NTSTATUS_NAMES.get(unsigned)
        return "退出码 0x%08X（%s）" % (unsigned, name) if name else "退出码 0x%08X" % unsigned
    return "退出码 %d" % code


def classify(
    *, limits: Limits, cpu: float, peak: int, exit_code: Optional[int],
    killed_by_wall: bool, output_exceeded: bool,
) -> Tuple[str, str]:
    """判定结果状态与说明。顺序与 Linux 后端一致（TLE > MLE > RE），输出超限是被我们杀的，最先判。

    Job 内存上限挡住的分配不会计入峰值，所以"崩溃 + 峰值已接近限制"按 MLE 算：
    ``new`` 失败抛 ``bad_alloc`` 没接住 → ``abort()``；``malloc`` 返回空指针再解引用 → 访问违例。
    """
    if output_exceeded:
        return OLE, "输出超过 %d 字节" % limits.output_bytes
    if killed_by_wall or cpu > limits.cpu_seconds:
        return TLE, ""
    if peak > limits.memory_bytes:
        return MLE, ""
    if exit_code == 0:
        return OK, ""
    unsigned = None if exit_code is None else exit_code & 0xFFFFFFFF
    if unsigned in SYSTEM_FAILURES:
        return SE, "程序没能启动：%s" % describe_exit_code(exit_code)
    if unsigned in OUT_OF_MEMORY or (_is_crash(exit_code) and peak >= limits.memory_bytes * NEAR_MEMORY_LIMIT):
        return MLE, "内存分配失败（%s）" % describe_exit_code(exit_code)
    return RE, describe_exit_code(exit_code)


def job_settings(limits: Limits) -> Dict[str, int]:
    """Job 的扩展限制。只限一个进程时按进程限 CPU 与内存；编译（不限进程数）按整个 Job 限。

    时间单位是 100 ns（Win32 的 LARGE_INTEGER 时长），只限用户态时间（Job 的时间上限只看用户态）。
    """
    cpu_ticks = int((limits.cpu_seconds + CPU_MARGIN_SECONDS) * 10000000)
    memory = limits.memory_bytes + MEMORY_MARGIN
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    settings = {"process_time": 0, "job_time": 0, "process_memory": 0, "job_memory": 0, "active_processes": 0}
    if limits.processes is None:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME | JOB_OBJECT_LIMIT_JOB_MEMORY
        settings["job_time"] = cpu_ticks
        settings["job_memory"] = memory
    else:
        flags |= (JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                  | JOB_OBJECT_LIMIT_ACTIVE_PROCESS)
        settings["process_time"] = cpu_ticks
        settings["process_memory"] = memory
        settings["active_processes"] = limits.processes
    settings["flags"] = flags
    return settings


def resolve_program(argv0: str, workdir: Path, path: Optional[str]) -> Optional[str]:
    """带路径分隔符的相对工作目录找（``./prog.exe``），否则在 ``path`` 里找。找不到返回 None。

    CreateProcessW 自己找程序时会先看**父进程**的当前目录，所以这里先解析成绝对路径。
    """
    if "/" in argv0 or "\\" in argv0:
        candidate = Path(argv0) if os.path.isabs(argv0) else workdir / argv0
        for item in (candidate, candidate.with_name(candidate.name + ".exe")):
            if item.is_file():
                return str(item.resolve())
        return None
    return shutil.which(argv0, path=path)


def build_environment(workdir: Path, base: Mapping[str, str]) -> Dict[str, str]:
    """子进程的环境：只留系统必需的几项与 PATH（g++ 找 as / ld），临时目录指向工作目录。"""
    upper = {key.upper(): value for key, value in base.items()}
    env = {}
    for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT"):
        if key in upper:
            env[key] = upper[key]
    env["TEMP"] = env["TMP"] = str(workdir)
    return env


def environment_block(env: Mapping[str, str]) -> str:
    """CreateProcessW 的 Unicode 环境块：按名称（不分大小写）排序，``名=值\\0`` 相接，再以 ``\\0`` 结尾。"""
    items = sorted(env.items(), key=lambda item: item[0].upper())
    return "".join("%s=%s\0" % (key, value) for key, value in items) + "\0"


def _dir_bytes(workdir: Path) -> Dict[str, int]:
    sizes = {}
    try:
        with os.scandir(str(workdir)) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        sizes[entry.name] = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return sizes


def _growth(before: Dict[str, int], workdir: Path) -> int:
    return sum(max(0, size - before.get(name, 0)) for name, size in _dir_bytes(workdir).items())


# --------------------------------------------------------------------------- #
# Win32（只在 Windows 上装载）
# --------------------------------------------------------------------------- #

_API = None


def _win32():  # pragma: no cover - 只在 Windows 上跑
    global _API
    if _API is not None:
        return _API
    import ctypes
    from ctypes import wintypes

    HANDLE = wintypes.HANDLE

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    BOOL, DWORD, LPVOID = wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p
    signatures = {
        "CreateJobObjectW": (HANDLE, [LPVOID, wintypes.LPCWSTR]),
        "SetInformationJobObject": (BOOL, [HANDLE, ctypes.c_int, LPVOID, DWORD]),
        "QueryInformationJobObject": (BOOL, [HANDLE, ctypes.c_int, LPVOID, DWORD, ctypes.POINTER(DWORD)]),
        "AssignProcessToJobObject": (BOOL, [HANDLE, HANDLE]),
        "TerminateJobObject": (BOOL, [HANDLE, ctypes.c_uint]),
        "TerminateProcess": (BOOL, [HANDLE, ctypes.c_uint]),
        "CreateProcessW": (BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID,
                                  wintypes.LPCWSTR, LPVOID, ctypes.POINTER(PROCESS_INFORMATION)]),
        "ResumeThread": (DWORD, [HANDLE]),
        "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
        "GetExitCodeProcess": (BOOL, [HANDLE, ctypes.POINTER(DWORD)]),
        "CloseHandle": (BOOL, [HANDLE]),
        "SetErrorMode": (ctypes.c_uint, [ctypes.c_uint]),
        "InitializeProcThreadAttributeList": (BOOL, [LPVOID, DWORD, DWORD, ctypes.POINTER(ctypes.c_size_t)]),
        "UpdateProcThreadAttribute": (BOOL, [LPVOID, DWORD, ctypes.c_size_t, LPVOID, ctypes.c_size_t,
                                             LPVOID, LPVOID]),
        "DeleteProcThreadAttributeList": (None, [LPVOID]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(k32, name)
        function.restype = restype
        function.argtypes = argtypes

    class Api:
        pass

    api = Api()
    api.ctypes = ctypes
    api.k32 = k32
    api.HANDLE = HANDLE
    api.DWORD = DWORD
    for cls in (JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
                JOBOBJECT_BASIC_UI_RESTRICTIONS, STARTUPINFOW, STARTUPINFOEXW, PROCESS_INFORMATION):
        setattr(api, cls.__name__, cls)
    _API = api
    return api


# JOBOBJECTINFOCLASS
_JobObjectBasicAccountingInformation = 1
_JobObjectBasicUIRestrictions = 4
_JobObjectExtendedLimitInformation = 9

_DETACHED_PROCESS = 0x00000008
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_WAIT_OBJECT_0 = 0
# SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX：子进程继承，不弹对话框
_ERROR_MODE = 0x0001 | 0x0002 | 0x8000


def _fail(what: str) -> SandboxError:  # pragma: no cover - 只在 Windows 上跑
    import ctypes

    code = ctypes.get_last_error()
    return SandboxError("%s 失败：%s" % (what, ctypes.FormatError(code).strip() if code else "未知错误"))


class WinJobSandbox(Sandbox):
    """Job Object 限资源。不隔离文件系统与网络（见模块说明）。"""

    isolated = False

    def __init__(self, base: Optional[Path] = None, run_as: Optional[str] = None) -> None:
        if sys.platform != "win32":
            raise SandboxError("winjob 后端只能在 Windows 上用")
        if run_as:
            raise SandboxError("winjob 后端不支持 --run-as：请直接以低权限专用账号运行评测机")
        api = _win32()
        api.k32.SetErrorMode(_ERROR_MODE)
        self.workdir = self._make_workdir(base)

    @staticmethod
    def _make_workdir(base: Optional[Path]) -> Path:
        problems = []
        candidates = box_root_candidates(str(base) if base else None, os.environ, tempfile.gettempdir())
        for root in candidates:
            try:
                os.makedirs(root, exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="syncoj-judge-", dir=root))
            except OSError as exc:
                problems.append("%s（%s）" % (root, exc))
        raise SandboxError("建不了纯 ASCII 路径的工作目录：%s" % ("；".join(problems) or "没有可用的候选目录"))

    # ------------------------------------------------------------------ #

    def _make_job(self, api, limits: Limits):  # pragma: no cover - 只在 Windows 上跑
        ctypes = api.ctypes
        job = api.k32.CreateJobObjectW(None, None)
        if not job:
            raise _fail("CreateJobObjectW")
        settings = job_settings(limits)
        info = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        basic = info.BasicLimitInformation
        basic.LimitFlags = settings["flags"]
        basic.PerProcessUserTimeLimit = settings["process_time"]
        basic.PerJobUserTimeLimit = settings["job_time"]
        basic.ActiveProcessLimit = settings["active_processes"]
        info.ProcessMemoryLimit = settings["process_memory"]
        info.JobMemoryLimit = settings["job_memory"]
        ui = api.JOBOBJECT_BASIC_UI_RESTRICTIONS(JOB_OBJECT_UILIMIT_ALL)
        ok = api.k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                             ctypes.byref(info), ctypes.sizeof(info))
        ok = ok and api.k32.SetInformationJobObject(job, _JobObjectBasicUIRestrictions,
                                                    ctypes.byref(ui), ctypes.sizeof(ui))
        if not ok:
            error = _fail("SetInformationJobObject")
            api.k32.CloseHandle(job)
            raise error
        return job

    def _spawn(self, api, job, argv: Sequence[str], files, limits: Limits) -> object:  # pragma: no cover
        """挂起创建 → 进 Job → 恢复主线程。返回进程句柄。"""
        import msvcrt

        ctypes = api.ctypes
        env = build_environment(self.workdir, os.environ)
        program = resolve_program(argv[0], self.workdir, env.get("PATH"))
        if program is None:
            stem = os.path.basename(argv[0]).lower()
            if stem.endswith(".exe"):
                stem = stem[:-4]
            if stem in COMPILERS:
                raise SandboxError("评测机上找不到 %s（MinGW-w64）" % argv[0])
            raise SandboxError("找不到要运行的程序：%s" % argv[0])

        handles: List[int] = []
        for handle in files:
            raw = msvcrt.get_osfhandle(handle.fileno())
            os.set_handle_inheritable(raw, True)
            handles.append(raw)

        size = ctypes.c_size_t(0)
        api.k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not api.k32.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
            raise _fail("InitializeProcThreadAttributeList")
        try:
            unique = list(dict.fromkeys(handles))
            inherit = (api.HANDLE * len(unique))(*unique)
            if not api.k32.UpdateProcThreadAttribute(
                attributes, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherit, ctypes.sizeof(inherit), None, None,
            ):
                raise _fail("UpdateProcThreadAttribute")
            startup = api.STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = handles
            startup.lpAttributeList = ctypes.addressof(attributes)

            block = environment_block(env)
            env_buffer = (ctypes.c_wchar * len(block))(*block)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([program] + list(argv[1:])))
            info = api.PROCESS_INFORMATION()
            flags = _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
            # 限进程数时不给控制台：有控制台就会多一个 conhost.exe 算进 Job 的活动进程数。
            # 编译器要给子进程（cc1plus / as / ld）共用一个隐藏控制台，否则每个子进程各开一个。
            flags |= _CREATE_NO_WINDOW if limits.processes is None else _DETACHED_PROCESS
            if not api.k32.CreateProcessW(program, command, None, None, True, flags, env_buffer,
                                          str(self.workdir), ctypes.byref(startup), ctypes.byref(info)):
                raise _fail("CreateProcessW（%s）" % argv[0])
        finally:
            api.k32.DeleteProcThreadAttributeList(attributes)
            for raw in handles:
                try:
                    os.set_handle_inheritable(raw, False)
                except OSError:
                    pass

        if not api.k32.AssignProcessToJobObject(job, info.hProcess):
            error = _fail("AssignProcessToJobObject")
            api.k32.TerminateProcess(info.hProcess, 1)
            api.k32.CloseHandle(info.hThread)
            api.k32.CloseHandle(info.hProcess)
            raise error
        if api.k32.ResumeThread(info.hThread) == 0xFFFFFFFF:
            error = _fail("ResumeThread")
            api.k32.TerminateJobObject(job, 1)
            api.k32.CloseHandle(info.hThread)
            api.k32.CloseHandle(info.hProcess)
            raise error
        api.k32.CloseHandle(info.hThread)
        return info.hProcess

    def run(self, argv, limits, *, stdin=None, stdout=None, stderr=None) -> RunResult:  # pragma: no cover
        api = _win32()
        ctypes = api.ctypes

        def open_or_null(name: Optional[str], mode: str):
            return open(os.devnull if name is None else str(self.workdir / name), mode)

        files = [open_or_null(stdin, "rb"), open_or_null(stdout, "wb"), open_or_null(stderr, "wb")]
        before = _dir_bytes(self.workdir)
        job = None
        process = None
        try:
            job = self._make_job(api, limits)
            started = time.monotonic()
            process = self._spawn(api, job, argv, files, limits)
            for handle in files:
                handle.close()
            killed_by_wall = output_exceeded = False
            while api.k32.WaitForSingleObject(process, int(POLL_SECONDS * 1000)) != _WAIT_OBJECT_0:
                if time.monotonic() - started > limits.wall_seconds:
                    killed_by_wall = True
                elif _growth(before, self.workdir) > limits.output_bytes:
                    output_exceeded = True
                else:
                    continue
                api.k32.TerminateJobObject(job, 1)
                api.k32.WaitForSingleObject(process, 5000)
                break
            wall = time.monotonic() - started
            if not output_exceeded and _growth(before, self.workdir) > limits.output_bytes:
                output_exceeded = True
            code = api.DWORD(0)
            exit_code = int(code.value) if api.k32.GetExitCodeProcess(process, ctypes.byref(code)) else None

            accounting = api.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            api.k32.QueryInformationJobObject(job, _JobObjectBasicAccountingInformation,
                                              ctypes.byref(accounting), ctypes.sizeof(accounting), None)
            cpu = (accounting.TotalUserTime + accounting.TotalKernelTime) / 10000000.0
            extended = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            api.k32.QueryInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                              ctypes.byref(extended), ctypes.sizeof(extended), None)
            peak = int(extended.PeakJobMemoryUsed if limits.processes is None else extended.PeakProcessMemoryUsed)
            api.k32.TerminateJobObject(job, 1)  # 编译器可能留下子进程
        finally:
            for handle in files:
                if not handle.closed:
                    handle.close()
            if process:
                api.k32.CloseHandle(process)
            if job:
                api.k32.CloseHandle(job)
        status, message = classify(
            limits=limits, cpu=cpu, peak=peak, exit_code=exit_code,
            killed_by_wall=killed_by_wall, output_exceeded=output_exceeded,
        )
        return RunResult(status, exit_code, None, cpu, wall, peak, message)

    def close(self) -> None:
        # 进程刚结束时文件句柄可能还没释放，删不掉就等一会儿再试
        for attempt in range(10):
            try:
                shutil.rmtree(str(self.workdir))
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        shutil.rmtree(str(self.workdir), ignore_errors=True)
