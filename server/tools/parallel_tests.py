#!/usr/bin/env python3
"""把测试**按文件**分片并行跑 —— 零依赖、离线可用。

为什么不直接上 pytest-xdist
--------------------------
目标机上装不了（PyPI 不通），而仓库的口味本来就是"能不加依赖就不加"。
而这套测试是**按文件隔离**的：每个用例用 ``tmp_path`` 造自己的库与目录，
少数需要真端口的用例（``test_e2e_agent``）也是先 ``bind(0)`` 要一个空闲端口。
所以"一个文件一个进程"就足以把 22 个核用起来，不需要插件，也不需要改写测试。

为什么按**文件**而不是按用例
--------------------------
按用例分片会让同一进程反复重建 app/库，并且 pytest 的收集开销（每个 worker 一次）
被摊薄得没有意义；按文件分片时，进程启动与收集各只付一次，而重活本来就在文件内部。

计数与失败怎么出来
------------------
* 每个文件的输出写进独立文件（**不用管道**：Windows 沙箱里跨进程管道会 EPERM，
  而且管道读写互相阻塞很容易把并行跑成串行）；失败的文件在最后**完整回放**，
  日志里仍然看得到原始断言，不用去猜。
* 退出码 = 有任一文件失败就是 1；所有文件都绿才是 0。CI 只看这一个数。

用法::

    python server/tools/parallel_tests.py                     # 默认 8 个 worker
    python server/tools/parallel_tests.py --jobs 12
    python server/tools/parallel_tests.py --serial            # 顺序跑（排查用）
    python server/tools/parallel_tests.py -- --maxfail=1      # 透传给 pytest
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

#: 默认 worker 数。按核数走，但**封顶 8**：每个 worker 都是一整个 Python + app
#: + 一个 SQLite 库，22 个一起上的收益已经被 IO 摊平，而失败信息会难读得多。
DEFAULT_JOBS_CAP = 8

#: 失败回放时最多打多少行（够看清断言与栈顶，又不至于把整份日志埋掉）。
FAILURE_TAIL_LINES = 60


def _use_utf8_stdout() -> None:
    """把 stdout/stderr 钉成 UTF-8，编不出来的字符用 ``?`` 顶替。

    **不是洁癖**：简体中文 Windows 的控制台默认代码页是 GBK，而这里要打中文与
    ``✓/✗`` —— 直接 ``print`` 会抛 ``UnicodeEncodeError``。第一次实测就是这么挂的：
    worker 线程当场死掉，于是只跑了一部分文件，而"少跑了文件"看起来跟"全部通过"
    一模一样。宁可打几个 ``?``，也不能因为打日志而少跑测试。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 老解释器/被重定向
            pass


def _find_tests(root: Path, pattern: str) -> List[Path]:
    return sorted(path for path in root.glob(pattern) if path.is_file())


def _jobs_default() -> int:
    env = (os.environ.get("SYNCOJ_TEST_JOBS") or "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    cpu = os.cpu_count() or 4
    return max(1, min(cpu, DEFAULT_JOBS_CAP))


def _run_one(
    python: str,
    server_dir: Path,
    test_file: Path,
    basetemp: Path,
    extra: Sequence[str],
) -> Tuple[int, str, float]:
    """跑一个测试文件，返回 ``(退出码, 输出, 耗时秒)``。

    输出落文件而不是走管道：Windows 沙箱禁止跨进程管道（会 EPERM），而且管道
    写满缓冲区时两边会互相等 —— 那是"并行跑出串行速度"的经典原因。
    """
    out_path = basetemp / ("%s.log" % test_file.stem)
    with open(str(out_path), "w", encoding="utf-8", errors="replace") as handle:
        env = dict(os.environ)
        # 让 tests/ 里的 conftest 能被 import；调用方（check.sh）也会设，这里兜底
        existing = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = os.pathsep.join(
            [str(server_dir)] + ([existing] if existing else [])
        )
        env.setdefault("PYTHONIOENCODING", "utf-8")
        argv = [
            python,
            "-m",
            "pytest",
            str(test_file.relative_to(server_dir)),
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp=%s" % (basetemp / test_file.stem),
        ] + list(extra)
        started = time.monotonic()
        try:
            code = subprocess.call(argv, cwd=str(server_dir), env=env, stdout=handle, stderr=subprocess.STDOUT)
        except OSError as exc:  # pragma: no cover - 解释器都起不来就没得跑了
            handle.write("无法启动 pytest: %s\n" % exc)
            code = 127
        elapsed = time.monotonic() - started
    try:
        text = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        text = ""
    return code, text, elapsed


def _basetemp_dir():
    """每个文件的 basetemp 根目录。

    默认用系统临时目录（跑完就删）。设了 ``SYNCOJ_TEST_TMP`` 就用它、**不删** ——
    排查某个失败用例时，`tmp_path` 里留下的现场（建出来的库、包、日志）比回放的
    stdout 有用得多。
    """
    keep = (os.environ.get("SYNCOJ_TEST_TMP") or "").strip()
    if keep:
        path = Path(keep)
        path.mkdir(parents=True, exist_ok=True)
        return contextlib.nullcontext(str(path))
    return tempfile.TemporaryDirectory(prefix="syncoj-tests-")


def main(argv: Optional[Sequence[str]] = None) -> int:
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="按文件并行跑测试（零依赖）",
        epilog="`--` 之后的参数原样透传给每个 pytest 进程",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="服务端根目录（里面有 tests/），默认是脚本的上一级",
    )
    parser.add_argument("--pattern", default="tests/test_*.py", help="测试文件的 glob")
    parser.add_argument("--jobs", type=int, default=_jobs_default(), help="worker 数")
    parser.add_argument("--serial", action="store_true", help="顺序跑（等价于 --jobs 1）")
    parser.add_argument("--list", action="store_true", help="只列出将要跑的文件")
    parser.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    server_dir = Path(args.root).resolve()
    tests = _find_tests(server_dir, args.pattern)
    if not tests:
        print("没有找到测试文件：%s/%s" % (server_dir, args.pattern), file=sys.stderr)
        return 2
    if args.list:
        for path in tests:
            print(path.relative_to(server_dir))
        return 0

    jobs = 1 if args.serial else max(1, int(args.jobs))
    # 大的先跑：动态领任务时，先放长工进池子，尾巴上才不会被一个 200 秒的文件卡住
    queue = sorted(tests, key=lambda p: p.stat().st_size, reverse=True)
    lock = threading.Lock()
    results: List[Tuple[Path, int, float, str]] = []

    with _basetemp_dir() as tmp:
        basetemp = Path(tmp)
        print(
            "并行跑 %d 个测试文件，%d 个 worker（%s）"
            % (len(queue), jobs, server_dir),
            flush=True,
        )
        started = time.monotonic()

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    test_file = queue.pop(0)
                code, text, elapsed = _run_one(
                    sys.executable, server_dir, test_file, basetemp, args.extra
                )
                with lock:
                    results.append((test_file, code, elapsed, text))
                    done = len(results)
                mark = "ok  " if code == 0 else "FAIL"
                print(
                    "[%2d/%2d] %s %6.1fs  %s"
                    % (done, len(tests), mark, elapsed, test_file.name),
                    flush=True,
                )

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.monotonic() - started

    failed = [(path, code, text) for path, code, _elapsed, text in results if code != 0]
    slowest = sorted(results, key=lambda item: item[2], reverse=True)[:5]

    print("\n耗时最长的 5 个文件：")
    for path, _code, elapsed, _text in slowest:
        print("  %6.1fs  %s" % (elapsed, path.name))
    print(
        "合计 %d 个文件 / 墙上时间 %.1fs（串行约 %.1fs）"
        % (
            len(results),
            wall,
            sum(item[2] for item in results),
        )
    )

    if not failed:
        print("全部通过")
        return 0

    print("\n%d 个文件失败：" % len(failed))
    for path, code, text in failed:
        print("  FAIL %s（退出码 %s）" % (path.name, code))
    for path, code, text in failed:
        print("\n" + "=" * 72)
        print("失败回放：%s（退出码 %s）" % (path.name, code))
        print("=" * 72)
        lines = text.strip().splitlines()
        for line in lines[-FAILURE_TAIL_LINES:]:
            print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
