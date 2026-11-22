#!/usr/bin/env python3
"""SyncOJ 全量检查：CI 与本地提交前都跑它。

    python scripts/check.py                 # 全部
    python scripts/check.py --skip-web      # 没装 web/node_modules 时
    python scripts/check.py --fail-fast     # 第一步失败就停
    python scripts/check.py --only agent-tests --only docs   # 只跑某几步（名字见 --list）

``scripts/check.sh`` 与 ``scripts/check.ps1`` 只负责**挑一个能用的 Python** 然后
把参数原样转给这里。

为什么收进一个 Python 驱动脚本
------------------------------
两个 shell 入口曾经各写一遍全部步骤、靠"一一对应"的约定同步，实际已经漂开：
ps1 全局设了 ``SYNCOJ_STATE_DIR``、sh 没设（有两条用例因此只在一边能过）；ps1 有 ``-SkipWeb``、sh 没有；两边都建了
一个从未传给任何命令的 ``basetemp``；两边都先 ``typecheck`` 再 ``build``，而
``build`` 自己就含 ``typecheck``。步骤只写一份，这类漂移就没有地方发生。

为什么默认**跑完全部步骤再汇总**
--------------------------------
旧脚本 ``set -e``：Agent 测试一红，服务端测试、OpenAPI 同步、前端构建全都不跑 ——
于是"红了几处"永远只看得见第一处，修完一处再跑才冒出下一处。现在每一步都跑，
最后列出哪些红了；``--fail-fast`` 保留旧行为。

本文件只用标准库，并且要能在 Python 3.8 上跑（与服务端同一条底线）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "web"

#: 服务端自己有第三方依赖，py38 门禁里显式放行（而不是把它们混进标准库白名单）。
SERVER_THIRD_PARTY = "fastapi,sqlalchemy,pydantic,starlette,uvicorn"

#: parallel_tests 的 worker 数默认值（与它自己的封顶一致）。
DEFAULT_JOBS = "8"

USE_COLOR = sys.stdout.isatty() and os.name != "nt"


def _banner(text: str) -> None:
    if USE_COLOR:
        print("\n\033[1;36m==> %s\033[0m" % text, flush=True)
    else:
        print("\n==> %s" % text, flush=True)


class StepFailed(Exception):
    """某一步没过。``str()`` 是给人看的原因。"""


def run(argv: Sequence[str], cwd: Optional[Path] = None) -> None:
    """跑一条命令，输出直接透传；非 0 就抛 :class:`StepFailed`。"""
    completed = subprocess.run(list(argv), cwd=str(cwd or REPO_ROOT))
    if completed.returncode != 0:
        raise StepFailed("%s 退出码 %d" % (" ".join(str(a) for a in argv), completed.returncode))


# --------------------------------------------------------------------------- #
# 各个步骤
# --------------------------------------------------------------------------- #


def step_py38(ctx: "Context") -> None:
    """Agent 必须能跑在 NOI Linux 2.0 的 Python 3.8 上；服务端也守同一条底线。"""
    run([ctx.python, "agent/tools/check_py38.py", "agent/"])
    run([ctx.python, "agent/tools/check_py38.py", "--allow", SERVER_THIRD_PARTY,
         "server/syncoj_server/"])
    # 本驱动脚本自己也要能在 3.8 上跑
    run([ctx.python, "agent/tools/check_py38.py", "scripts/"])


def step_ps1_bom(ctx: "Context") -> None:
    """``.ps1`` 必须带 UTF-8 BOM。

    Windows PowerShell 5.1 在**没有 BOM** 时按当前 ANSI 代码页（简体中文系统上是
    GBK）解码脚本，于是里面的中文注释会让它**整个文件解析失败**，报的还是"字符串
    缺少终止符"这种指向别处的错。编辑工具保存时通常不保留 BOM —— 改一行注释就
    可能把它弄丢，所以单独一步守着。
    """
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "scripts").glob("*.ps1"))
        if not path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    if missing:
        raise StepFailed(
            "这些 .ps1 缺少 UTF-8 BOM（修法：在文件最前面补上 EF BB BF）：%s" % ", ".join(missing)
        )


def step_versions(ctx: "Context") -> None:
    """四处版本号一致（发布时标签还要对上它，见 ``scripts/release.py``）。"""
    run([ctx.python, "scripts/release.py", "versions"])


def _parallel_tests(ctx: "Context", root: str) -> None:
    run([ctx.python, "server/tools/parallel_tests.py", "--root", root, "--jobs", ctx.jobs])


def step_agent_tests(ctx: "Context") -> None:
    _parallel_tests(ctx, "agent")


def step_server_tests(ctx: "Context") -> None:
    """含协议契约测试与端到端集成测试。"""
    _parallel_tests(ctx, "server")


def step_openapi(ctx: "Context") -> None:
    """服务端 schema 改了但 ``web/openapi.json`` 没重新导出时，在这里拦住。"""
    run([ctx.python, "server/tools/dump_openapi.py", "--check"])


def step_web_types(ctx: "Context") -> None:
    """``schema.d.ts`` 必须就是从 ``openapi.json`` 生成出来的那一份。

    判据是"重新生成一遍、逐字节比较"，**不用 git diff**：git diff 比的是工作树与
    HEAD，而开发中工作树本来就有未提交的改动，于是那种检查在最常见的场景下恒红 ——
    假警报会训练人无视这一步。
    """
    schema = WEB / "src" / "api" / "schema.d.ts"
    before = schema.read_bytes()
    run([ctx.npm, "run", "gen:types"], cwd=WEB)
    if schema.read_bytes() != before:
        raise StepFailed(
            "web/src/api/schema.d.ts 与 web/openapi.json 不同步：重新生成的结果与仓库里的不一样。"
            "请把刚重新生成的 schema.d.ts 一起提交。"
        )


def step_licenses(ctx: "Context") -> None:
    """``THIRD-PARTY-LICENSES.md`` 与前端清单要和已安装的依赖一致（需要 ``web/node_modules``）。"""
    run([ctx.python, "scripts/collect_licenses.py", "--check"])


def step_web_build(ctx: "Context") -> None:
    """``npm run build`` 已经串了 check:routes → typecheck → vite build。"""
    run([ctx.npm, "run", "build"], cwd=WEB)


#: (名字, 标题, 函数, 是否属于前端)。顺序就是执行顺序。
STEPS: List[Tuple[str, str, Callable[["Context"], None], bool]] = [
    ("py38", "Python 3.8 兼容门禁", step_py38, False),
    ("ps1-bom", "PowerShell 脚本的编码", step_ps1_bom, False),
    ("versions", "各处版本号一致", step_versions, False),
    ("agent-tests", "Agent 测试", step_agent_tests, False),
    ("server-tests", "服务端测试（含协议契约与端到端集成）", step_server_tests, False),
    ("openapi", "OpenAPI 与服务端 schema 是否同步", step_openapi, False),
    ("web-types", "前端类型是否已从 openapi.json 重新生成", step_web_types, True),
    ("licenses", "第三方许可证声明与依赖一致", step_licenses, True),
    ("web-build", "前端入口对账、类型检查与构建", step_web_build, True),
]


# --------------------------------------------------------------------------- #
# 驱动
# --------------------------------------------------------------------------- #


class Context:
    def __init__(self, python: str, jobs: str, npm: Optional[str]) -> None:
        self.python = python
        self.jobs = jobs
        self.npm = npm


def _prepare_environment() -> None:
    # 子进程的 stdin/stdout 一律 UTF-8。这是**进程间约定**，不是"这台机器的问题"：
    # Windows 上子进程默认按控制台代码页（GBK）写中文，Linux CI 在 LC_ALL=C 下会
    # 退化成 ascii —— test_agent_contract 按 UTF-8 解码 Agent 生成的样本，当场炸。
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    # 刻意**不**设 SYNCOJ_STATE_DIR：旧的 check.ps1 全局设了它，而有用例正是要验
    # "没有这个变量时"的行为（test_registration_contract 里两条）—— 于是同一套
    # 测试在两个入口下结果不同。需要状态目录的用例各自 monkeypatch。
    os.environ.pop("SYNCOJ_STATE_DIR", None)


def _find_npm() -> Optional[str]:
    # Windows 上是 npm.cmd；shutil.which 会按 PATHEXT 找到它
    return shutil.which("npm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SyncOJ 全量检查")
    parser.add_argument("--skip-web", action="store_true", help="跳过前端两步")
    parser.add_argument("--fail-fast", action="store_true", help="第一步失败就停（默认跑完再汇总）")
    parser.add_argument(
        "--only", action="append", default=[], metavar="STEP",
        help="只跑这一步（可重复）。步骤名见 --list",
    )
    parser.add_argument(
        "--skip", action="append", default=[], metavar="STEP",
        help="跳过这一步（可重复）。步骤名见 --list",
    )
    parser.add_argument("--list", action="store_true", help="列出步骤名后退出")
    parser.add_argument(
        "--jobs", default=os.environ.get("SYNCOJ_TEST_JOBS", DEFAULT_JOBS),
        help="测试并行 worker 数（默认取 SYNCOJ_TEST_JOBS，再默认 %s）" % DEFAULT_JOBS,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    names = [name for name, _title, _fn, _web in STEPS]

    if args.list:
        for name, title, _fn, _web in STEPS:
            print("%-14s %s" % (name, title))
        return 0
    unknown = [name for name in args.only + args.skip if name not in names]
    if unknown:
        print("未知步骤：%s（可选：%s）" % (", ".join(unknown), ", ".join(names)), file=sys.stderr)
        return 2

    os.chdir(str(REPO_ROOT))
    _prepare_environment()
    npm = _find_npm()
    web_ready = not args.skip_web and npm is not None and (WEB / "node_modules").is_dir()
    ctx = Context(python=sys.executable, jobs=str(args.jobs), npm=npm)
    print("使用 Python: %s（%s）" % (sys.executable, sys.version.split()[0]))

    failed: List[Tuple[str, str]] = []
    skipped: List[str] = []
    started = time.monotonic()
    for name, title, fn, is_web in STEPS:
        if name in args.skip:
            skipped.append(name)
            continue
        if args.only and name not in args.only:
            continue
        if is_web and not web_ready:
            skipped.append(name)
            continue
        _banner("%s  [%s]" % (title, name))
        try:
            fn(ctx)
        except StepFailed as exc:
            print("[失败] %s：%s" % (name, exc), flush=True)
            failed.append((name, str(exc)))
            if args.fail_fast:
                break

    _banner("汇总（%.0f 秒）" % (time.monotonic() - started))
    if skipped:
        print("跳过：%s" % ", ".join(skipped))
    if failed:
        for name, reason in failed:
            print("失败：%-14s %s" % (name, reason))
        return 1
    print("全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
