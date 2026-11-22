"""评测机（§12）：向服务端领任务 → 取代码与数据包 → 在沙箱里评 → 交结果。

    syncoj-judge --server http://10.0.0.5:8000 --token-file /etc/syncoj-judge/token \\
                 --backend isolate

以**专用账号**运行，可以在服务端那台机器上，也可以在另一台（评测时 CPU 跑满，不该和收代码的
服务端抢）。只用标准库：另一台机器上直接下载服务端打好的 ``/judge/worker.pyz``（或
``pip install`` 服务端包）即可运行，用不到服务端的第三方依赖。

和服务端的往来只有一个循环（§12.10）：每个轮询间隔 ``POST /judge/poll`` 一次 —— 自报状态
（版本、后端、主机、并发数），有空位就顺带领一个任务，响应里带管理端下的命令
（``stop``：评完手上的退出；``restart``：评完手上的、以同样的参数重启自己）。评测中也照常
poll（``want`` = 0），服务端据此判断在线。``--slots N`` 时 N 个任务并发评（各用一个线程，
isolate 的 box 编号依次 +1）。

数据包按 sha256 缓存在 ``--cache-dir``（同一道题的几十个选手只下载一次）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import runner
from . import sandbox as sb

log = logging.getLogger("syncoj.judge")

DEFAULT_POLL_SECONDS = 5.0
MAX_BLOB_BYTES = 1024 * 1024 * 1024

#: 各后端是否隔离（报给服务端，界面据此标"不隔离"）。不在表里的后端先报未知，
#: 评完第一个任务后按结果里的 ``isolated`` 更正。
BACKEND_ISOLATED = {"isolate": True, "rlimit": False, "winjob": False}


def _version() -> str:
    try:
        from .. import __version__
    except Exception:  # noqa: BLE001 - 单独拷出 judging/ 时没有包根
        return "unknown"
    return str(__version__)


class Transport:
    """最小的 HTTP 接口：``request(method, path, body) -> (status, bytes)``。测试里换成 TestClient。"""

    def __init__(self, base_url: str, token: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                return response.status, response.read(MAX_BLOB_BYTES + 1)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(64 * 1024)


class Worker:
    def __init__(
        self,
        transport: Transport,
        sandbox_factory: Callable[..., sb.Sandbox],
        cache_dir: Path,
        *,
        slots: int = 1,
        backend: Optional[str] = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        """``sandbox_factory``：无参调用给第 0 个并发位；``slots`` > 1 时其余位以 ``factory(slot)`` 调用。"""
        self.transport = transport
        self.sandbox_factory = sandbox_factory
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.slots = max(1, int(slots))
        self.poll_seconds = float(poll_seconds)
        self.status: Dict[str, Any] = {
            "version": _version(),
            "backend": backend,
            "isolated": BACKEND_ISOLATED.get(backend or ""),
            "hostname": socket.gethostname()[:128],
            "platform": sys.platform[:32],
        }
        self._stop = False
        #: 服务端下的命令（``stop`` / ``restart``）；``main`` 据此决定退出后是否重启自己
        self.command = "run"
        self._wake = threading.Event()
        self._fetch_lock = threading.Lock()

    def stop(self, *_args) -> None:
        self._stop = True
        self._wake.set()

    @property
    def restart_requested(self) -> bool:
        return self.command == "restart"

    def _fetch(self, task_id: int, sha256: str, target: Path) -> Path:
        with self._fetch_lock:  # 并发位同时取同一份数据包时只下载一次
            if target.is_file() and _sha256_of(target) == sha256:
                return target
            status, data = self.transport.request("GET", "/api/v1/judge/tasks/%d/blobs/%s" % (task_id, sha256))
            if status != 200:
                raise RuntimeError("取内容 %s 失败：HTTP %d" % (sha256[:12], status))
            if hashlib.sha256(data).hexdigest() != sha256:
                raise RuntimeError("取到的内容与 sha256 不符：%s" % sha256[:12])
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(data)
            os.replace(str(tmp), str(target))
            return target

    def poll(self, want: int) -> Optional[Dict[str, Any]]:
        """报状态，``want`` = 1 时顺带领任务。返回领到的任务（没有为 None），命令记进 ``self.command``。

        服务端太旧（没有 ``/poll``，404）时退回旧的 ``/lease``：照样能评，只是收不到命令。
        """
        body = dict(self.status, slots=self.slots, poll_seconds=self.poll_seconds, want=1 if want else 0)
        status, raw = self.transport.request("POST", "/api/v1/judge/poll", body)
        if status == 404:
            if not want:
                return None
            status, raw = self.transport.request("POST", "/api/v1/judge/lease")
            if status == 204:
                return None
            if status != 200:
                raise RuntimeError("领任务失败：HTTP %d %s" % (status, raw[:200].decode("utf-8", "replace")))
            return json.loads(raw.decode("utf-8"))
        if status != 200:
            raise RuntimeError("和服务端通信失败：HTTP %d %s" % (status, raw[:200].decode("utf-8", "replace")))
        reply = json.loads(raw.decode("utf-8"))
        command = reply.get("command") or "run"
        if command in ("stop", "restart") and self.command == "run":
            log.info("服务端要求%s：评完手上的任务后%s", "停止" if command == "stop" else "重启",
                     "退出" if command == "stop" else "重启")
            self.command = command
            self.stop()
        return reply.get("task")

    def run_once(self) -> bool:
        """领一个任务并评完。没有任务返回 False。"""
        task = self.poll(want=1)
        if task is None:
            return False
        self._run_task(task, 0)
        return True

    def _run_task(self, task: Dict[str, Any], slot: int) -> None:
        task_id = int(task["id"])
        log.info("评测 #%d：%s / %s", task_id, task["player_no"], task["problem"]["ident"])
        try:
            outcome = self._judge(task, slot)
        except Exception as exc:  # noqa: BLE001 - 评测系统自己的问题一律 SE、退回队列
            log.exception("评测 #%d 出错", task_id)
            self.transport.request("POST", "/api/v1/judge/tasks/%d/fail" % task_id, {"message": str(exc)[:2000]})
            return
        self.status["isolated"] = bool(outcome.isolated)
        document = outcome.to_result_json()
        body = {
            "score": document.pop("score"), "max_score": document.pop("max_score"),
            "status": document.pop("status"), "detail": document,
        }
        status, raw = self.transport.request("POST", "/api/v1/judge/tasks/%d/result" % task_id, body)
        if status != 200:
            log.warning("交结果 #%d 失败：HTTP %d（任务可能已被重测替换）", task_id, status)

    def _factory_for(self, slot: int) -> Callable[[], sb.Sandbox]:
        if slot == 0:
            return self.sandbox_factory
        return lambda: self.sandbox_factory(slot)

    def _judge(self, task: Dict[str, Any], slot: int = 0) -> runner.JudgeOutcome:
        task_id = int(task["id"])
        testdata_sha = task["testdata"]["sha256"]
        testdata = self._fetch(task_id, testdata_sha, self.cache_dir / ("%s.zip" % testdata_sha))
        work = Path(tempfile.mkdtemp(prefix="syncoj-judge-src-"))
        try:
            source_name = Path(task["source"]["path"] or "main.cpp").name
            source = self._fetch(task_id, task["source"]["sha256"], work / source_name)
            problem = task["problem"]
            config = runner.ProblemConfig(
                ident=problem["ident"],
                time_limit_ms=int(problem["time_limit_ms"]),
                memory_limit_mb=int(problem["memory_limit_mb"]),
                io_mode=problem.get("io_mode") or "stdio",
                input_file=problem.get("input_file") or None,
                output_file=problem.get("output_file") or None,
                compile_flags=problem.get("compile_flags"),
            )
            return runner.judge(source, source_name, testdata, config, self._factory_for(slot))
        finally:
            import shutil

            shutil.rmtree(str(work), ignore_errors=True)

    def _slot_main(self, task: Dict[str, Any], slot: int) -> None:
        try:
            self._run_task(task, slot)
        finally:
            self._wake.set()

    def run_forever(self, poll_seconds: Optional[float] = None) -> int:
        """主循环：有空位就领任务（每个任务一个线程），没空位也每个轮询间隔 poll 一次报平安。

        停止（信号或服务端命令）后不再领新任务，等手上的评完再返回。
        """
        if poll_seconds is not None:
            self.poll_seconds = float(poll_seconds)
        running: Dict[int, threading.Thread] = {}
        failures = 0
        while True:
            for slot, thread in list(running.items()):
                if not thread.is_alive():
                    del running[slot]
            if self._stop and not running:
                break
            free = [slot for slot in range(self.slots) if slot not in running]
            self._wake.clear()
            task = None
            try:
                task = self.poll(want=1 if free and not self._stop else 0)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - 服务端暂时连不上之类：退避后重试
                failures += 1
                log.warning("和服务端通信失败（第 %d 次）：%s", failures, exc)
            if task is not None:
                slot = free[0]
                thread = threading.Thread(target=self._slot_main, args=(task, slot),
                                          name="judge-slot-%d" % slot, daemon=True)
                running[slot] = thread
                thread.start()
                if len(free) > 1:
                    continue  # 还有空位：马上再领
            wait = min(self.poll_seconds * (2 ** min(failures, 4)), 120)
            if self._stop and running:
                wait = min(wait, 1.0)
            self._wake.wait(wait)
        return 0


def restart_argv() -> List[str]:
    """以同样的参数重启自己的命令行。

    从 zipapp（``python syncoj-judge.pyz …``）启动时原样重跑那个 pyz；``-m`` / 安装后的
    ``syncoj-judge`` 入口（Windows 上是 .exe，不能交给 python）一律换成 ``-m``。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and os.path.isfile(argv0) and _is_zip(argv0):
        return [sys.executable, argv0] + sys.argv[1:]
    return [sys.executable, "-m", "syncoj_server.judging.worker"] + sys.argv[1:]


def _is_zip(path: str) -> bool:
    import zipfile

    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def restart_self() -> None:
    """Linux 上原地 exec（同一个进程号，systemd 看来服务没断）；Windows 上 exec 会换进程号、
    父进程（计划任务）以为它已退出，所以改为起一个新进程再退出。"""
    argv = restart_argv()
    log.info("重启：%s", " ".join(argv))
    logging.shutdown()
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(argv, close_fds=True, creationflags=flags)
        return
    os.execv(sys.executable, argv)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syncoj-judge", description="SyncOJ 评测机（自带沙箱评测）")
    parser.add_argument("--server", required=True, help="服务端管理端口地址，如 http://10.0.0.5:8000")
    parser.add_argument("--token-file", required=True, help="管理端「评测机」签发的令牌（文件里只有这一行）")
    parser.add_argument("--backend", choices=list(sb.BACKENDS), default=None,
                        help="沙箱：isolate（Linux 默认、推荐）/ rlimit（只给开发自测，不隔离网络与文件系统）"
                             " / winjob（Windows 默认，Job Object 限资源，不隔离网络与文件系统）")
    parser.add_argument("--run-as", default=None, help="rlimit 后端以 root 运行时，选手程序换成这个账号跑")
    parser.add_argument("--box-id", type=int, default=0, help="isolate 的 box 编号（同机多个 worker 要各不相同）")
    parser.add_argument("--isolate", default="isolate", help="isolate 可执行文件路径")
    parser.add_argument("--cgroups", action="store_true", help="isolate 用 cgroup 记内存（需要 isolate 配好 cgroup）")
    parser.add_argument("--cache-dir", default=None, help="数据包缓存目录（默认 ~/.cache/syncoj-judge）")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS, help="没有任务时多久问一次（秒）")
    parser.add_argument("--slots", type=int, default=1, help="同时评几个任务（isolate 的 box 编号从 --box-id 起依次占用）")
    parser.add_argument("--log-file", default=None, help="日志写到这个文件（默认 stderr）")
    parser.add_argument("--once", action="store_true", help="只评一个任务就退出（调试）")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    backend = args.backend or sb.default_backend(sys.platform)
    problem = sb.backend_problem(backend, sys.platform, run_as=args.run_as)
    if problem:
        print("错误：" + problem, file=sys.stderr)
        return 2
    if args.slots < 1:
        print("--slots 要是正整数", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        filename=args.log_file,
    )
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    cache = Path(args.cache_dir) if args.cache_dir else Path.home() / ".cache" / "syncoj-judge"

    def factory(slot: int = 0) -> sb.Sandbox:
        return sb.make_sandbox(
            backend, run_as=args.run_as, box_id=args.box_id + slot,
            isolate_binary=args.isolate, cgroups=args.cgroups,
        )

    if backend == "winjob":
        log.warning("winjob 后端不隔离网络与文件系统：请以低权限专用账号运行评测机")
    if backend == "rlimit":
        log.warning("rlimit 后端不隔离网络与文件系统：只用于开发自测，正式考试请用 isolate")
        if hasattr(os, "geteuid") and os.geteuid() == 0 and not args.run_as:
            print("rlimit 后端以 root 运行时必须给 --run-as <普通账号>", file=sys.stderr)
            return 2
    worker = Worker(Transport(args.server, token), factory, cache,
                    slots=args.slots, backend=backend, poll_seconds=args.poll)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    if args.once:
        return 0 if worker.run_once() else 1
    code = worker.run_forever()
    if worker.restart_requested:
        restart_self()
    return code


if __name__ == "__main__":
    sys.exit(main())
