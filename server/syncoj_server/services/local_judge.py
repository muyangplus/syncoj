"""本机评测机（§12.10）：服务端以子进程运行 ``syncoj-judge``。

给"只有一台机器"的场景：不必另开终端、另配账号。它和别的评测机走同一套 HTTP 接口
（连 ``127.0.0.1:<管理端口>``），令牌也是一行 ``judge_worker``（``is_local`` = 1，
每次启动换一枚新令牌）。

* 只在 Linux 服务端上提供：两个沙箱后端都依赖 Linux。
* 后端：装了 isolate 用 isolate；否则 rlimit。rlimit 在服务端以 root 运行时选手程序
  换成 ``judge_local_run_as``（默认 ``nobody``）跑；不是 root 时就以服务端账号跑
  （它本来就没有 root 能丢）。
* 停止 = SIGTERM：评测机评完手上的任务再退出。服务端退出时一并停掉（等 10 秒，
  不退就 SIGKILL —— 没交的任务租约到期自动回队列）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from sqlalchemy import select

from ..models import JudgeWorker, iso_utc, utcnow
from ..schemas import LocalJudgeOut
from ..security import hash_token, new_token


def loopback_host(listen_host: str) -> str:
    """本机评测机连服务端用的主机名：通配地址 / 回环 → 127.0.0.1；具体地址原样（IPv6 加方括号）。"""
    host = (listen_host or "").strip()
    if host in ("", "0.0.0.0", "::", "localhost", "127.0.0.1", "::1"):
        return "127.0.0.1"
    return "[%s]" % host if ":" in host else host


log = logging.getLogger(__name__)

LOCAL_NAME = "本机评测机"
UNSUPPORTED_REASON = "仅 Linux"


class LocalJudge:
    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, platform: Optional[str] = None) -> None:
        self._popen = popen
        self._platform = platform
        self._lock = threading.Lock()
        self._proc: Any = None
        self._backend: Optional[str] = None
        self._worker_id: Optional[int] = None
        self._started_at: Optional[datetime] = None
        self._exit_code: Optional[int] = None

    # ---- 查询 ----

    def supported(self) -> bool:
        return (self._platform or sys.platform).startswith("linux")

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> LocalJudgeOut:
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._exit_code = self._proc.returncode
                self._proc = None
            supported = self.supported()
            return LocalJudgeOut(
                supported=supported,
                reason=None if supported else UNSUPPORTED_REASON,
                running=self._proc is not None,
                pid=self._proc.pid if self._proc is not None else None,
                backend=self._backend,
                worker_id=self._worker_id,
                started_at=iso_utc(self._started_at) if self._proc is not None else None,
                exit_code=self._exit_code,
            )

    # ---- 启停 ----

    def start(self, ctx) -> LocalJudgeOut:
        if not self.supported():
            from ..errors import ApiError

            raise ApiError(409, "local_judge_unsupported", "本机评测机只能在 Linux 服务端上运行")
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                pass
            else:
                self._spawn(ctx)
        return self.status()

    def _spawn(self, ctx) -> None:
        settings = ctx.settings
        root = Path(settings.data_root) / "judge-local"
        root.mkdir(parents=True, exist_ok=True)
        token = new_token()
        with ctx.db.session() as session:
            row = session.execute(
                select(JudgeWorker).where(JudgeWorker.is_local.is_(True)).order_by(JudgeWorker.id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = JudgeWorker(name=LOCAL_NAME, token_hash=hash_token(token), is_local=True)
                session.add(row)
            else:
                row.token_hash = hash_token(token)
            row.enabled = True
            row.desired = "run"
            session.flush()
            worker_id = row.id
        token_file = root / "token"
        fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(str(token_file), 0o600)

        backend = "isolate" if shutil.which("isolate") else "rlimit"
        argv: List[str] = [
            sys.executable, "-m", "syncoj_server.judging.worker",
            "--server", "http://%s:%d" % (loopback_host(settings.http_host), settings.http_port),
            "--token-file", str(token_file),
            "--backend", backend,
            "--cache-dir", str(root / "cache"),
        ]
        if backend == "rlimit" and hasattr(os, "geteuid") and os.geteuid() == 0:
            argv += ["--run-as", settings.judge_local_run_as]
        log_path = Path(settings.log_root) / "judge-local.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # 子进程要 import 到同一份 syncoj_server（开发检出、可编辑安装都一样）
        package_parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_parent, env.get("PYTHONPATH")]))
        with open(str(log_path), "ab") as log_file:
            self._proc = self._popen(
                argv, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                env=env, cwd=str(root), start_new_session=True,
            )
        self._backend = backend
        self._worker_id = worker_id
        self._started_at = utcnow()
        self._exit_code = None
        log.info("本机评测机已启动（pid %s，后端 %s），日志 %s", getattr(self._proc, "pid", "?"), backend, log_path)

    def stop(self) -> LocalJudgeOut:
        """发 SIGTERM 就返回：评测机评完手上的任务再退出（状态里 ``running`` 随后变 False）。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
        return self.status()

    def shutdown(self, timeout: float = 10.0) -> None:
        """服务端退出时调用：停掉子进程，等不到就杀掉。"""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - 杀不掉就算了
                    pass
            self._exit_code = proc.returncode
            self._proc = None
            log.info("本机评测机已随服务端停止")
