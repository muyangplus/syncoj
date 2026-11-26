"""交互题（§12.15）：选手程序与 interactor 用两根管道连起来同时跑，再按判定表给结果。

* :func:`run_pair` 起两个程序：选手在选手 box（只有 ``prog``），interactor 在评测侧 box
  （interactor、输入、答案）。选手的 stdout → interactor 的 stdin，interactor 的 stdout → 选手的
  stdin。选手先结束后给 interactor ``grace`` 秒收尾，到时杀掉。父进程在两边都起来之后关掉
  自己手上的四个管道端（不关的话对方永远读不到 EOF）。
* :func:`decide` 是纯函数：两边的运行结果 + interactor 的 testlib 判定 → (状态, 比例, 说明,
  要不要再跑 checker)。判定表自上而下取第一条命中（见 docs/design/10-builtin-judge.md §12.15）。

时间、内存只报选手的；interactor 的用时另记在测试点明细里。
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from . import sandbox as sb
from .verdict import FAIL, OK, PARTIAL, PE, WA, Verdict

__all__ = ["PairResult", "run_pair", "decide", "GRACE_SECONDS"]

GRACE_SECONDS = 2.0
#: 判"谁先结束"时 interactor 要比选手早这么多秒才算（见 run_pair）
FIRST_MARGIN = 0.2
SIGPIPE = getattr(signal, "SIGPIPE", 13)
USER, INTERACTOR = "user", "interactor"


@dataclass
class PairResult:
    user: sb.RunResult
    interactor: sb.RunResult
    #: 先结束的一方：``user`` / ``interactor``
    first: str
    #: interactor 在选手结束后超过 grace 仍没结束、被杀掉
    grace_killed: bool = False
    escaped: int = 0
    #: 选手的 CPU 时限：TLE 时 CPU 没用满 = 空等（多半是没 fflush）
    user_cpu_limit: float = float("inf")


def run_pair(
    user_box: sb.Sandbox, user_argv: Sequence[str], user_limits: sb.Limits,
    trusted_box: sb.Sandbox, inter_argv: Sequence[str], inter_limits: sb.Limits,
    *, inter_stderr: str, grace: float = GRACE_SECONDS,
) -> PairResult:
    to_inter_r, to_inter_w = os.pipe()  # 选手 stdout → interactor stdin
    to_user_r, to_user_w = os.pipe()    # interactor stdout → 选手 stdin
    since = sb.boot_ticks()
    user = inter = None
    try:
        user = user_box.spawn(user_argv, user_limits, stdin_fd=to_user_r, stdout_fd=to_inter_w, kill_strays=False)
        try:
            inter = trusted_box.spawn(inter_argv, inter_limits, stdin_fd=to_inter_r, stdout_fd=to_user_w,
                                      stderr=inter_stderr, kill_strays=False)
        except BaseException:
            user.kill()
            user.wait()
            raise
    finally:
        for fd in (to_inter_r, to_inter_w, to_user_r, to_user_w):
            try:
                os.close(fd)
            except OSError:
                pass
    grace_killed = False
    try:
        user_result = user.wait()
    except BaseException:
        inter.kill()
        inter.done(5)
        raise
    if not inter.done(grace):
        inter.kill()
        grace_killed = True
    inter_result = inter.wait()
    escaped = user_box.reap_strays(since) + (user_result.escaped or 0)
    # 选手被杀之后 interactor 才读到 EOF：两边的结束时刻只差几毫秒，记录时刻还受线程调度影响。
    # 只有 interactor 明显先结束（差出 FIRST_MARGIN）才算它先判了
    first = INTERACTOR if (inter.finished_at or 0) + FIRST_MARGIN < (user.finished_at or 0) else USER
    return PairResult(user_result, inter_result, first, grace_killed, escaped, user_limits.cpu_seconds)


def _by_verdict(verdict: Verdict) -> Tuple[str, float, str]:
    if verdict.kind == OK:
        return "AC", 1.0, verdict.message
    if verdict.kind == PARTIAL:
        return "PA", verdict.ratio, verdict.message
    if verdict.kind == PE:
        return "PE", 0.0, verdict.message
    return "WA", 0.0, verdict.message


def decide(pair: PairResult, verdict: Optional[Verdict], has_checker: bool) -> Tuple[str, float, str, bool]:
    """判定表（§12.15）。返回 ``(状态, 比例, 说明, 要不要再跑 checker)``；interactor 的问题抛
    :class:`sandbox.SandboxError`（记 SE）。"""
    user, inter = pair.user, pair.interactor
    if user.status == sb.SE or inter.status == sb.SE:
        raise sb.SandboxError("沙箱出错：%s" % (user.message or inter.message))
    user_pipe = user.status == sb.RE and user.signal == SIGPIPE
    user_ok = user.status == sb.OK or (user.status == sb.RE and user.signal is None)
    inter_broken = (
        inter.status in (sb.TLE, sb.MLE, sb.OLE)
        or (inter.signal is not None and inter.signal != SIGPIPE)
        or pair.grace_killed
    )
    kind = verdict.kind if verdict is not None else None
    # 2：选手正常结束而 interactor 超限、被信号杀或收尾超时 —— interactor 的问题
    if user_ok and inter_broken:
        raise sb.SandboxError("interactor 没有正常结束（%s%s）" % (
            inter.status, "，选手结束后 %d 秒仍未退出" % GRACE_SECONDS if pair.grace_killed else ""))
    # 3：interactor 先判了错，选手随后超时 / 出错 / 写断管道 —— 以 interactor 为准
    if (pair.first == INTERACTOR and kind in (WA, PE, PARTIAL)
            and (user.status in (sb.TLE, sb.RE) or user_pipe)):
        return _by_verdict(verdict) + (False,)
    # 4、5、6：选手自己的超限与运行错误
    if user.status == sb.TLE:
        idle = user.cpu_seconds < pair.user_cpu_limit
        return "TLE", 0.0, "空闲超时（等待交互时没有输出，检查是否 fflush）" if idle else "", False
    if user.status in (sb.MLE, sb.OLE):
        return user.status, 0.0, "", False
    if user.status == sb.RE and user.signal is not None and not user_pipe:
        return "RE", 0.0, "被信号 %d 终止" % user.signal, False
    # 7：选手写已关闭的管道
    if user_pipe:
        if kind == OK:
            return "RE", 0.0, "交互结束后仍在输出", False
        if kind in (WA, PE, PARTIAL):
            return _by_verdict(verdict) + (False,)
        raise sb.SandboxError("interactor 报告自身失败：%s" % (verdict.message if verdict else "没有可读的结果"))
    # 8：选手先结束，interactor 读到管道断开被杀
    if user_ok and inter.signal == SIGPIPE:
        return "WA", 0.0, "程序提前结束", False
    # 9：interactor 没给出可认的结果
    if kind is None or kind == FAIL:
        raise sb.SandboxError("interactor 报告自身失败：%s" % (verdict.message if verdict else "没有可读的结果"))
    if user.status == sb.RE:
        # 退出码非 0（没有信号）
        if kind == OK:
            return "RE", 0.0, "退出码 %s" % user.exit_code, False
    # 10、11
    status, ratio, message = _by_verdict(verdict)
    return status, ratio, message, has_checker and kind in (OK, PARTIAL)
