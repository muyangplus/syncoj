"""运行不可信程序的沙箱后端（§12）。

三个后端，接口相同（:class:`Sandbox`）；``winjob``（Windows Job Object）在 :mod:`.sandbox_win`：

``isolate``（推荐，正式考试用）
    调用 IOI 的 isolate（命名空间 + 可选 cgroup）。它是竞赛圈事实标准的沙箱，自己写一份
    同等强度的东西（unshare + seccomp + cgroup）维护成本和出错代价都不值得。这里只是
    **调用**它（它是 GPL-2），不链接。
``rlimit``（只给开发与自测）
    ``fork`` + ``setrlimit``（CPU / 内存 / 输出大小 / 进程数）+ 以另一个账号运行 + 每个
    测试点前清空工作目录。**没有网络与文件系统隔离**，界面上标红"不隔离"。worker 以 root
    运行时必须给 ``run_as``，否则拒绝 —— 以 root 跑选手代码是绝对不行的。

计时判 TLE 用 **CPU 时间**；墙钟只作硬上限（防 sleep 卡死）。内存用峰值常驻集（isolate 开
cgroup 时用 cgroup 的峰值）。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

__all__ = [
    "Limits",
    "RunResult",
    "Sandbox",
    "SandboxError",
    "RlimitSandbox",
    "IsolateSandbox",
    "make_sandbox",
    "default_backend",
    "backend_problem",
    "BACKENDS",
    "parse_isolate_meta",
]

OK, TLE, MLE, RE, OLE, SE = "OK", "TLE", "MLE", "RE", "OLE", "SE"

#: rlimit 后端每个运行账号的进程数上限（见 RlimitSandbox.run 里的说明）
RLIMIT_NPROC_CAP = 64


class SandboxError(RuntimeError):
    """评测系统自己的问题（沙箱起不来、配置不对）—— 结果记 SE，不算选手的分。"""


@dataclass
class Limits:
    cpu_seconds: float
    wall_seconds: float
    memory_bytes: int
    output_bytes: int = 64 * 1024 * 1024
    #: None = 不限（编译器要起子进程）
    processes: Optional[int] = 1


@dataclass
class RunResult:
    status: str
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    memory_bytes: int = 0
    message: str = ""


class Sandbox:
    """一个工作目录 + 在里面运行程序的能力。路径都相对于 :attr:`workdir`。"""

    workdir: Path
    isolated = False

    def run(
        self,
        argv: Sequence[str],
        limits: Limits,
        *,
        stdin: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> RunResult:  # pragma: no cover - 接口
        raise NotImplementedError

    def clean(self, keep: Sequence[str] = ()) -> None:
        """清空工作目录，只留 ``keep`` 里的文件（程序不能跨测试点留东西）。"""
        for entry in self.workdir.iterdir():
            if entry.name in keep:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(str(entry), ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass

    def put(self, name: str, data: bytes, mode: int = 0o644) -> Path:
        path = self.workdir / name
        path.write_bytes(data)
        os.chmod(str(path), mode)
        return path

    def close(self) -> None:  # pragma: no cover - 接口
        raise NotImplementedError


def _classify(
    *, killed_by_wall: bool, cpu: float, limits: Limits, memory: int,
    exit_code: Optional[int], sig: Optional[int],
) -> str:
    if killed_by_wall or cpu > limits.cpu_seconds or sig == getattr(signal, "SIGXCPU", -1):
        return TLE
    if sig == getattr(signal, "SIGXFSZ", -1):
        return OLE
    if memory > limits.memory_bytes:
        return MLE
    if sig is not None or (exit_code or 0) != 0:
        return RE
    return OK


# --------------------------------------------------------------------------- #
# rlimit
# --------------------------------------------------------------------------- #


#: 中间那一层：再 fork 一次、exec 选手程序、把**它自己的** rusage 从管道报回来。
#:
#: 为什么要这一层：Linux 的 ``ru_maxrss`` 跨 ``execve`` 保留旧地址空间的峰值。直接从 worker
#: （一个几十上百 MB 的 Python 进程）fork 出来再 exec，选手程序的"峰值内存"就是 worker 的
#: 大小 —— A+B 都会被判超内存。从这个小进程再 fork 一次，孙进程的峰值只会被抬高到 shim
#: 自己的几 MB。管道在 exec 之前关掉，选手程序写不到它。
_SHIM = r"""
import json, os, sys
argv = json.loads(sys.argv[1])
report = int(sys.argv[2])
pid = os.fork()
if pid == 0:
    os.close(report)
    try:
        os.execvp(argv[0], argv)
    except OSError:
        os._exit(127)
_, status, usage = os.wait4(pid, 0)
os.write(report, json.dumps([status, usage.ru_utime + usage.ru_stime, usage.ru_maxrss]).encode())
"""


class RlimitSandbox(Sandbox):
    """``fork`` + ``setrlimit``。只给开发与自测（见模块说明）。"""

    isolated = False

    def __init__(self, base: Optional[Path] = None, run_as: Optional[str] = None) -> None:
        import resource  # noqa: F401  （Windows 上没有：在这里就失败，而不是跑到一半）

        self._uid = self._gid = None
        if run_as:
            import pwd

            entry = pwd.getpwnam(run_as)
            self._uid, self._gid = entry.pw_uid, entry.pw_gid
        elif hasattr(os, "geteuid") and os.geteuid() == 0:
            raise SandboxError("rlimit 后端以 root 运行时必须用 --run-as 指定一个普通账号")
        self.workdir = Path(tempfile.mkdtemp(prefix="syncoj-judge-", dir=str(base) if base else None))
        os.chmod(str(self.workdir), 0o700)
        if self._uid is not None:
            os.chown(str(self.workdir), self._uid, self._gid)

    def put(self, name: str, data: bytes, mode: int = 0o644) -> Path:
        path = super().put(name, data, mode)
        if self._uid is not None:
            os.chown(str(path), self._uid, self._gid)
        return path

    def run(self, argv, limits, *, stdin=None, stdout=None, stderr=None) -> RunResult:
        import resource

        uid, gid = self._uid, self._gid

        def apply_limits() -> None:  # 在子进程里、exec 之前
            cpu = int(limits.cpu_seconds) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
            memory = limits.memory_bytes + 64 * 1024 * 1024  # 动态库、栈的余量；判 MLE 看峰值
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_STACK, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits.output_bytes, limits.output_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if uid is not None:
                if limits.processes is not None:
                    # NPROC 按**账号**计、不按这一次运行计：同一个账号下并行的几次评测（多个
                    # worker、并行测试）会互相挤占，设成 2 就会让别人的 fork 失败。所以这里只
                    # 挡 fork 炸弹（每账号 64），"只许一个进程"做不到 —— 这是 rlimit 后端只给
                    # 开发自测的又一个原因；isolate 每个 box 一个账号，没有这个问题。
                    resource.setrlimit(resource.RLIMIT_NPROC, (RLIMIT_NPROC_CAP, RLIMIT_NPROC_CAP))
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)

        def open_or_null(name: Optional[str], mode: str):
            if name is None:
                return open(os.devnull, mode)
            return open(str(self.workdir / name), mode)

        files = [open_or_null(stdin, "rb"), open_or_null(stdout, "wb"), open_or_null(stderr, "wb")]
        if uid is not None:
            for name in (stdout, stderr):
                if name:
                    os.chown(str(self.workdir / name), uid, gid)
        import json
        import sys

        report_read, report_write = os.pipe()

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _SHIM, json.dumps(list(argv)), str(report_write)],
                cwd=str(self.workdir), stdin=files[0], stdout=files[1], stderr=files[2],
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C"},
                preexec_fn=apply_limits, start_new_session=True, pass_fds=(report_write,),
            )
        except OSError as exc:
            for handle in files:
                handle.close()
            os.close(report_read)
            os.close(report_write)
            raise SandboxError("起不来：%s" % exc)
        os.close(report_write)

        killed = threading.Event()

        def kill() -> None:
            killed.set()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

        timer = threading.Timer(limits.wall_seconds, kill)
        timer.start()
        try:
            report = b""
            while True:
                chunk = os.read(report_read, 4096)
                if not chunk:
                    break
                report += chunk
            proc.wait()
        finally:
            timer.cancel()
            os.close(report_read)
            for handle in files:
                handle.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # 收拾留下的子进程
        except OSError:
            pass
        wall = time.monotonic() - started
        try:
            status, cpu, maxrss = json.loads(report.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            if killed.is_set():
                return RunResult(TLE, None, signal.SIGKILL, limits.cpu_seconds, wall, 0)
            raise SandboxError("沙箱中间层没有报回结果（退出码 %s）" % proc.returncode)
        memory = int(maxrss) * 1024
        sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        verdict = _classify(
            killed_by_wall=killed.is_set(), cpu=cpu, limits=limits, memory=memory,
            exit_code=code, sig=sig,
        )
        return RunResult(verdict, code, sig, cpu, wall, memory)

    def close(self) -> None:
        shutil.rmtree(str(self.workdir), ignore_errors=True)


# --------------------------------------------------------------------------- #
# isolate
# --------------------------------------------------------------------------- #


def parse_isolate_meta(text: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta


class IsolateSandbox(Sandbox):
    """IOI isolate。``--cg`` 打开时用 cgroup 记内存（更准，需要 isolate 配好 cgroup）。"""

    isolated = True

    def __init__(self, box_id: int = 0, binary: str = "isolate", cgroups: bool = False) -> None:
        self.binary = binary
        self.box_id = box_id
        self.cgroups = cgroups
        self._base = [binary, "--box-id=%d" % box_id] + (["--cg"] if cgroups else [])
        subprocess.run(self._base + ["--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = subprocess.run(
            self._base + ["--init"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if result.returncode != 0:
            raise SandboxError("isolate --init 失败：%s" % (result.stderr or result.stdout).strip()[:500])
        self.box = Path(result.stdout.strip().splitlines()[-1])
        self.workdir = self.box / "box"
        self._meta_dir = Path(tempfile.mkdtemp(prefix="syncoj-isolate-meta-"))

    def _command(self, argv, limits, stdin, stdout, stderr, meta: Path) -> List[str]:
        command = self._base + [
            "--run",
            "--meta=%s" % meta,
            "--time=%.3f" % limits.cpu_seconds,
            "--wall-time=%.3f" % limits.wall_seconds,
            "--extra-time=0.5",
            "--fsize=%d" % max(1, limits.output_bytes // 1024),
            "--env=PATH=/usr/local/bin:/usr/bin:/bin",
        ]
        command.append("--processes" if limits.processes is None else "--processes=%d" % limits.processes)
        memory_kb = limits.memory_bytes // 1024
        if self.cgroups:
            command.append("--cg-mem=%d" % memory_kb)
        else:
            # 没有 cgroup 时只能限地址空间（给动态库留余量），判 MLE 看峰值常驻集
            command.append("--mem=%d" % (memory_kb + 64 * 1024))
        for flag, name in (("--stdin", stdin), ("--stdout", stdout), ("--stderr", stderr)):
            if name:
                command.append("%s=%s" % (flag, name))
        return command + ["--"] + list(argv)

    def run(self, argv, limits, *, stdin=None, stdout=None, stderr=None) -> RunResult:
        meta_path = self._meta_dir / "meta.txt"
        try:
            meta_path.unlink()
        except FileNotFoundError:
            pass
        command = self._command(argv, limits, stdin, stdout, stderr, meta_path)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        try:
            meta = parse_isolate_meta(meta_path.read_text(encoding="utf-8"))
        except OSError:
            raise SandboxError("isolate 没有写 meta（退出码 %d）：%s" % (result.returncode, result.stderr.strip()[:500]))
        return self._result_from_meta(meta, limits)

    def _result_from_meta(self, meta: Dict[str, str], limits: Limits) -> RunResult:
        cpu = float(meta.get("time", "0") or 0)
        wall = float(meta.get("time-wall", "0") or 0)
        memory_kb = int(meta.get("cg-mem") or meta.get("max-rss") or 0)
        exit_code = int(meta["exitcode"]) if "exitcode" in meta else None
        sig = int(meta["exitsig"]) if "exitsig" in meta else None
        status = meta.get("status", "")
        message = meta.get("message", "")
        if status == "XX":
            return RunResult(SE, exit_code, sig, cpu, wall, memory_kb * 1024, message)
        if meta.get("cg-oom-killed"):
            return RunResult(MLE, exit_code, sig, cpu, wall, memory_kb * 1024, message)
        verdict = _classify(
            killed_by_wall=(status == "TO"), cpu=cpu, limits=limits, memory=memory_kb * 1024,
            exit_code=exit_code, sig=sig,
        )
        return RunResult(verdict, exit_code, sig, cpu, wall, memory_kb * 1024, message)

    def close(self) -> None:
        subprocess.run(self._base + ["--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(str(self._meta_dir), ignore_errors=True)


BACKENDS = ("isolate", "rlimit", "winjob")
LINUX_BACKENDS = ("isolate", "rlimit")


def default_backend(platform: str) -> str:
    return "winjob" if platform == "win32" else "isolate"


def backend_problem(backend: str, platform: str, run_as: Optional[str] = None) -> Optional[str]:
    """这个平台能不能用这个后端；不能时返回给人看的原因。"""
    if platform.startswith("linux"):
        if backend in LINUX_BACKENDS:
            return None
        return "%s 后端只能在 Windows 上用；Linux 上用 isolate（推荐）或 rlimit" % backend
    if platform == "win32":
        if backend != "winjob":
            return "%s 后端只能在 Linux 上用；Windows 上用 winjob" % backend
        if run_as:
            return "winjob 后端不支持 --run-as：请直接以低权限专用账号运行评测机"
        return None
    return "syncoj-judge 只支持 Linux（isolate / rlimit）与 Windows（winjob），当前平台 %s" % platform


def make_sandbox(
    backend: str, *, base: Optional[Path] = None, run_as: Optional[str] = None,
    box_id: int = 0, isolate_binary: str = "isolate", cgroups: bool = False,
) -> Sandbox:
    if backend == "isolate":
        return IsolateSandbox(box_id=box_id, binary=isolate_binary, cgroups=cgroups)
    if backend == "rlimit":
        return RlimitSandbox(base=base, run_as=run_as)
    if backend == "winjob":
        from .sandbox_win import WinJobSandbox

        return WinJobSandbox(base=base, run_as=run_as)
    raise SandboxError("不认识的沙箱后端：%s（%s）" % (backend, " / ".join(BACKENDS)))
