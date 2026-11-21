"""评测机（§12）：向服务端领任务 → 取代码与数据包 → 在沙箱里评 → 交结果。

    syncoj-judge --server http://10.0.0.5:8000 --token-file /etc/syncoj-judge/token \\
                 --backend isolate

以**专用账号**运行，可以在服务端那台机器上，也可以在另一台（评测时 CPU 跑满，不该和收代码的
服务端抢）。只用标准库：另一台机器上 ``pip install`` 服务端包即可得到这条命令，用不到它的
第三方依赖。

数据包按 sha256 缓存在 ``--cache-dir``（同一道题的几十个选手只下载一次）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import runner
from . import sandbox as sb

log = logging.getLogger("syncoj.judge")

DEFAULT_POLL_SECONDS = 5.0
MAX_BLOB_BYTES = 1024 * 1024 * 1024


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
        sandbox_factory: Callable[[], sb.Sandbox],
        cache_dir: Path,
    ) -> None:
        self.transport = transport
        self.sandbox_factory = sandbox_factory
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stop = False

    def stop(self, *_args) -> None:
        self._stop = True

    def _fetch(self, task_id: int, sha256: str, target: Path) -> Path:
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

    def run_once(self) -> bool:
        """领一个任务并评完。没有任务返回 False。"""
        status, raw = self.transport.request("POST", "/api/v1/judge/lease")
        if status == 204:
            return False
        if status != 200:
            raise RuntimeError("领任务失败：HTTP %d %s" % (status, raw[:200].decode("utf-8", "replace")))
        task = json.loads(raw.decode("utf-8"))
        task_id = int(task["id"])
        log.info("评测 #%d：%s / %s", task_id, task["player_no"], task["problem"]["ident"])
        try:
            outcome = self._judge(task)
        except Exception as exc:  # noqa: BLE001 - 评测系统自己的问题一律 SE、退回队列
            log.exception("评测 #%d 出错", task_id)
            self.transport.request("POST", "/api/v1/judge/tasks/%d/fail" % task_id, {"message": str(exc)[:2000]})
            return True
        document = outcome.to_result_json()
        body = {
            "score": document.pop("score"), "max_score": document.pop("max_score"),
            "status": document.pop("status"), "detail": document,
        }
        status, raw = self.transport.request("POST", "/api/v1/judge/tasks/%d/result" % task_id, body)
        if status != 200:
            log.warning("交结果 #%d 失败：HTTP %d（任务可能已被重测替换）", task_id, status)
        return True

    def _judge(self, task: Dict[str, Any]) -> runner.JudgeOutcome:
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
                compile_flags=problem.get("compile_flags"),
            )
            return runner.judge(source, source_name, testdata, config, self.sandbox_factory)
        finally:
            import shutil

            shutil.rmtree(str(work), ignore_errors=True)

    def run_forever(self, poll_seconds: float = DEFAULT_POLL_SECONDS) -> int:
        failures = 0
        while not self._stop:
            try:
                busy = self.run_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - 服务端暂时连不上之类：退避后重试
                failures += 1
                log.warning("和服务端通信失败（第 %d 次）：%s", failures, exc)
                busy = False
            if not busy:
                deadline = time.monotonic() + min(poll_seconds * (2 ** min(failures, 4)), 120)
                while not self._stop and time.monotonic() < deadline:
                    time.sleep(0.5)
        return 0


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
    parser.add_argument("--backend", choices=["isolate", "rlimit"], default="isolate",
                        help="沙箱：isolate（推荐）/ rlimit（只给开发自测，不隔离网络与文件系统）")
    parser.add_argument("--run-as", default=None, help="rlimit 后端以 root 运行时，选手程序换成这个账号跑")
    parser.add_argument("--box-id", type=int, default=0, help="isolate 的 box 编号（同机多个 worker 要各不相同）")
    parser.add_argument("--isolate", default="isolate", help="isolate 可执行文件路径")
    parser.add_argument("--cgroups", action="store_true", help="isolate 用 cgroup 记内存（需要 isolate 配好 cgroup）")
    parser.add_argument("--cache-dir", default=None, help="数据包缓存目录（默认 ~/.cache/syncoj-judge）")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS, help="没有任务时多久问一次（秒）")
    parser.add_argument("--once", action="store_true", help="只评一个任务就退出（调试）")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    cache = Path(args.cache_dir) if args.cache_dir else Path.home() / ".cache" / "syncoj-judge"

    def factory() -> sb.Sandbox:
        return sb.make_sandbox(
            args.backend, run_as=args.run_as, box_id=args.box_id,
            isolate_binary=args.isolate, cgroups=args.cgroups,
        )

    if args.backend == "rlimit":
        log.warning("rlimit 后端不隔离网络与文件系统：只用于开发自测，正式考试请用 isolate")
        if hasattr(os, "geteuid") and os.geteuid() == 0 and not args.run_as:
            print("rlimit 后端以 root 运行时必须给 --run-as <普通账号>", file=sys.stderr)
            return 2
    worker = Worker(Transport(args.server, token), factory, cache)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    if args.once:
        return 0 if worker.run_once() else 1
    return worker.run_forever(args.poll)


if __name__ == "__main__":
    sys.exit(main())
