"""Agent 主循环。

一轮 ``cycle()`` 的顺序是固定的：

    扫描 -> tick -> 上传 -> 下载 -> （升级） -> 按服务端指定周期休眠

**为什么扫描放在最前**：tick 请求体里带的就是这一轮扫描结果，扫描必须是最新的。
**为什么上传在 tick 之后**：服务端用它自己的台账决定"要什么"，Agent 不自行判断
该传什么 —— 判断权在服务端，Agent 只执行。这让"谁传过什么"只有一个权威来源。

未配对 / 没有场次的机器走的是**另一条路**：只心跳、不扫描（见 ``_pending_tick``）。
它没有准考证号，扫描目录里的 ``{player_no}`` 展开不出来，扫出来的相对路径也没法
归属到任何人。这两种状态看起来都像"注册成功了"，但该做的事完全不同。
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from . import diagnostics
from . import discovery
from .client import (
    AgentClient,
    AgentError,
    AuthError,
    BootstrapKeyError,
    NetworkError,
    PayloadTooLarge,
    ProtocolError,
    RateLimited,
    StaleUpload,
    UnboundError,
)
from .config import (
    PREFIX_AUTO,
    PREFIX_NONE,
    WAITING_FILE_NAME,
    AgentConfig,
    ConfigError,
    expand_placeholders,
)
from .download import download_asset, find_partial_sizes, sweep_stale_parts
from .policy import DEFAULT_POLICY, merge_policy
from .rsa import RSAPublicKey, SignatureError
from .scan import ScanPolicy, scan_directory
from .state import (
    STATE_READY,
    STATE_UNCLAIMED,
    STATE_WAITING,
    Credential,
    HashCache,
    atomic_write_text,
    declared_xdg_desktop,
    describe_os,
    load_credential,
    read_bootstrap_key,
    resolve_machine_fingerprint,
    resolve_machine_id,
    resolve_machine_uuid,
    save_credential,
    sha256_file,
)
from .upgrade import (
    MAX_BUNDLE_BYTES,
    ReleaseManifest,
    UpgradeError,
    UpgradeMode,
    UpgradeState,
    activate_release,
    current_release,
    mark_healthy,
    note_boot,
    parse_version,
    read_release_fingerprint,
    rollback_release,
    save_state,
    stage_release,
    verify_bundle,
)

__all__ = [
    "Agent",
    "main",
    "build_tick_payload",
    "join_report_path",
    "state_from_payload",
]

log = logging.getLogger("syncoj.agent")

#: 出错后的退避上限。网络长时间不通时不要疯狂重连
MAX_BACKOFF = 300.0
#: 事件积压上限，防止长时间离线导致内存里堆一堆事件
MAX_PENDING_EVENTS = 200

#: 连续多少轮连不上服务端之后，去局域网里问一次它在哪。
#:
#: 攒够再问是因为"连不上"的常见原因不是地址变了（服务端重启、网线抖动、
#: Wi-Fi 重连都会连不上），而发现要往整个局域网广播。三轮配合指数退避大约是
#: 5+10+20 秒 —— 够排除抖动，也不会让一台换了网段的机器干等太久。
REDISCOVER_AFTER_FAILURES = 3

#: 连续多少个成功周期后才认为新版本可信、清掉回滚状态。
#: 不用"启动成功"作为判据 —— 起得来但一 tick 就崩的版本同样必须回滚。
HEALTHY_CYCLES_BEFORE_TRUST = 3

#: 自卸载脚本的固定路径。**在 current 软链之外**：sudo 的 NOPASSWD 白名单按
#: 字面路径匹配、不解析符号链接（见 install.py 的同名常量与注释）。
SELF_UNINSTALL_PATH = "/usr/local/lib/syncoj/self_uninstall.sh"

#: 一次性卸载令牌的长度上限（服务端签出来的只有几百字节）。
MAX_UNINSTALL_TOKEN_BYTES = 8 * 1024

#: 卸载执行失败后的重试间隔（秒）。**绝不每个 tick 都去撞 sudo**：失败可能是
#: sudoers 没装好、令牌过期、脚本缺失，连着撞只会把日志刷爆、也烧 CPU。
UNINSTALL_RETRY_SECONDS = 3600.0

#: 等自卸载脚本跑完的上限（秒）。它要删目录、要调 systemctl，但不会太久。
UNINSTALL_TIMEOUT_SECONDS = 120.0

#: 注册单元的单元名（root 身份跑一次注册的那个）。Agent **永远不自己注册** ——
#: 它以选手身份运行，读不到 root 只读的统一密钥；撞上去只会 PermissionError →
#: 退出 → 被 systemd 反复重启（真机上就这么卡住过）。所以没凭据时它只等这个
#: 单元把凭据放进来。
ENROLL_UNIT_NAME = "syncoj-agent-enroll.service"

#: 等注册凭据的轮询间隔（秒）。一分钟足够及时，也不会刷日志。
REGISTRATION_POLL_SECONDS = 60.0

#: 每轮 tick 最多上报几个"当前不存在的扫描根"。
#:
#: 为什么要上限：这是给管理端"后台提示"用的，不是台账 —— 一个配错的
#: ``scan.roots`` 可能一次列几十条，全塞进每轮报文只会把它撑肥。超出的部分
#: 在本地日志里说明（运维要查全量在那儿查）。
SCAN_MISSING_MAX = 5

# --- 诊断回传（定期把现场留给服务端）--------------------------------------- #
#
# 机器一旦离线就什么都抓不到了，所以要在**健康的时候**就把现场留下：出事时
# 管理端看到的至少是"最后一份"。触发节奏三条：
#
# * 健康时每 10 分钟一份（``reason="periodic"``）；
# * 出错立即补一份（``reason="error"``），但两次之间至少隔 60 秒 —— 一次故障
#   里连着十轮失败，不该刷十份包（服务端也就只允 60 秒一份）；
# * 服务端在 tick 里下 ``diagnostics_request=true``（管理端的按钮）→ 一份
#   ``reason="manual"``。
#
# 上传失败**绝不影响心跳**（只记 DEBUG，连续多次才 WARNING），见
# :meth:`Agent._maybe_send_diagnostics`。
DIAGNOSTICS_PERIOD_SECONDS = 600.0
DIAGNOSTICS_MIN_INTERVAL_SECONDS = 60.0
#: 连续失败到第几个周期才去催"出错包"
DIAGNOSTICS_ERROR_CONSECUTIVE_FAILURES = 2
#: 上传连续失败到第几次才升到 WARNING（之后每 10 次再提一次，避免刷屏）
DIAGNOSTICS_WARN_AFTER_FAILURES = 3

#: **整轮看门狗**的下限（秒）。
#:
#: 真正的保命符：任何一次阻塞调用（socket 读、文件系统、网络盘上的 ``os.utime``）
#: 都不允许让心跳停摆超过这个时间。它比"给每个调用都加超时"可靠 —— 不用去
#: 穷举所有阻塞点。
#:
#: 取值 ``max(120, request_timeout * 3)``：单次控制面请求最多 30 秒，撞上长连接
#: 竞态时最多重试两次 → 90 秒，再留 30 秒余量。**升级切换那一段会临时放宽**
#: （见 ``Agent._watchdog_paused``）：下载上百 MB 的包、切软链、交给 systemd
#: 重启，本来就该花更久，拿"单轮 120 秒"去掐它是误伤。
WATCHDOG_MIN_SECONDS = 120.0


class RoundTimeout(Exception):
    """看门狗掐掉了一轮 —— **中止这一轮**，不是退出进程。

    它由 SIGALRM 的处理函数抛出，落进 ``run_forever`` 那个 ``except``，
    记一条 WARNING 后照常进入下一轮。
    """


def watchdog_supported() -> bool:
    """这个平台能不能用 SIGALRM/``setitimer``（Windows 上没有）。"""
    return hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")


def _watchdog_handler(signum, frame) -> None:  # pragma: no cover - 由信号触发
    raise RoundTimeout("这一轮超过了看门狗上限")


def arm_watchdog(seconds: float) -> bool:
    """给当前这一轮上闹钟；返回是否真的装上了。

    只有**主线程**能用 SIGALRM —— ``run_forever`` 就是主线程。``--once`` /
    ``--check`` 与测试**不装**它：那几条路径要么是排障、要么要跑到自己想要的
    地方为止。

    SIGALRM 与既有的 SIGTERM/SIGINT **是两条线**：那两个把 ``self._stop`` 置位
    （"跑完这一轮就退出"），SIGALRM 抛异常（"这一轮不算，重来"）。两者互不覆盖：
    这里只注册 SIGALRM，不动另外两个。
    """
    if not watchdog_supported():
        return False
    signal.signal(signal.SIGALRM, _watchdog_handler)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    return True


def disarm_watchdog() -> None:
    """撤掉闹钟。**每轮结束时都要撤**，否则会漂到下一轮里去。"""
    if not watchdog_supported():
        return
    signal.setitimer(signal.ITIMER_REAL, 0)


def _run_uninstall_command(argv, stdin_bytes):
    """真正执行 ``sudo -n <self_uninstall.sh>``：令牌**只走 stdin**。

    返回 ``(returncode, stderr 摘要)``。做成模块级函数是为了让测试注入替身 ——
    单元测试里不该真去碰 sudo 与 systemd。
    """
    try:
        completed = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            timeout=UNINSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "自卸载脚本超时（%d 秒）" % UNINSTALL_TIMEOUT_SECONDS
    except OSError as exc:
        return 127, "无法执行自卸载脚本: %s" % exc
    stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
    return completed.returncode, stderr


def join_report_path(prefix: str, rel_path: str) -> str:
    """拼出上报给服务端的路径。

    ``prefix`` 为空时直接返回相对路径 —— 本机只有一个选手时不需要前缀，
    否则 ``source/<场次>/<选手编号>/<准考证号>/…`` 里准考证号会出现两次。
    """
    return "%s/%s" % (prefix, rel_path) if prefix else rel_path


def state_from_payload(data: Dict[str, object]) -> str:
    """从服务端报文里读出三态（``claimed`` × ``bound``）。

    缺字段时按**能干活**处理：这个函数的用途是"发现状态变化"，不是判权限 ——
    默认成"没配对"会让一台本来好好的机器突然停下来等一个永远不会来的配对码。
    真正的权威判据永远在服务端的响应里。
    """
    if not bool(data.get("claimed", True)):
        return STATE_UNCLAIMED
    return STATE_READY if bool(data.get("bound", True)) else STATE_WAITING


def _read_desktop_text(path: Path) -> Optional[str]:
    """读桌面提示文件的现有内容；读不到（不存在/读不了/不是 UTF-8）返回 ``None``。

    ``None`` 一律当作"内容不一致" → 会触发重写。文件被人删掉正是靠这一条自愈的。
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _as_int_or_none(value) -> Optional[int]:
    """把服务端给的数收成 int；给不出就 ``None``（诊断包里原样体现"不知道"）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _current_user() -> str:
    """当前进程的用户名（诊断包里报"Agent 以谁在跑"）。拿不到就空串。"""
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - 极端环境下取不到
        return ""


def _scan_missing_for_payload(raw) -> List[str]:
    """把"不存在的扫描根"整理成上报用的列表：去重、按路径排序、上限 ``SCAN_MISSING_MAX``。

    **只在这一处加工**：`build_tick_payload` 是报文体的唯一出口（``tools/build_fixture.py``
    也用它生成样本），所以排序/截断/日志都落在这里，别在调用方再拼一份 ——
    两份必然漂，而报文里的字段漂了最难查。
    """
    items = sorted({str(item) for item in (raw or []) if str(item)})
    if len(items) > SCAN_MISSING_MAX:
        log.info(
            "当前有 %d 个扫描根不存在，报文里只报前 %d 个，完整列表：%s",
            len(items),
            SCAN_MISSING_MAX,
            "、".join(items),
        )
    return items[:SCAN_MISSING_MAX]


def build_tick_payload(
    machine_id: str,
    agent_version: str,
    scan_root: str,
    results,
    partials: List[dict],
    stats: Dict[str, object],
    completed_assets: Optional[List[int]] = None,
    scan_complete: bool = True,
    release_public_key: bool = False,
):
    """构造 tick 请求体。

    ``results`` 是 ``[(前缀, ScanOutcome), ...]``；前缀可以为空串。
    返回 ``(payload, oversize, errors)`` —— 后两项只用于产出审计事件，不进请求体；
    报文体里只放统计摘要，避免随问题数量膨胀。

    ``scan_complete=False`` 用于"这一轮压根没扫描"（未配对 / 没有场次）：空
    ``scan`` 与"选手把文件全删了"在报文里长得一模一样，而只有我们自己知道
    这一次是前者。老实说"这次没扫"，比说"扫完了，一个文件都没有"安全 ——
    后者是一句谎话，靠服务端记得丢弃它才不致命。

    做成模块级函数而不是 ``Agent`` 的方法，是为了让 ``tools/build_fixture.py``
    能在无网络、无 Agent 实例的情况下生成**真实**报文样本，供服务端的契约测试
    校验。若样本是测试里手写的，它会与真实代码一起漂移，契约测试就失去了意义。
    """
    entries: List[dict] = []
    complete = scan_complete
    oversize: List[str] = []
    errors: List[str] = []

    for name, outcome in results:
        for entry in outcome.entries:
            item = entry.to_dict()
            item["path"] = join_report_path(name, entry.path)
            entries.append(item)
        if not outcome.complete:
            complete = False
        oversize.extend(join_report_path(name, item) for item in outcome.oversize)
        errors.extend("%s: %s" % (name or "-", item) for item in outcome.errors[:5])

    final_stats = {
        "disk_free": stats.get("disk_free"),
        "last_error": stats.get("last_error") or (errors[0] if errors else None),
        "queue": int(stats.get("queue", 0)),  # type: ignore[arg-type]
        # 当前不存在的扫描根（绝对路径）。**每轮都报**：服务端拿它做"后台提示"
        # （"这台机器的桌面目录还没有"），而"消失"这件事只有在每轮报文里才看得出来。
        # 排序稳定（服务端与日志才好对账），最多 SCAN_MISSING_MAX 条。
        "scan_missing": _scan_missing_for_payload(stats.get("scan_missing")),
    }

    payload = {
        "agent_version": agent_version,
        "machine_id": machine_id,
        "ts": int(time.time()),
        "scan_root": scan_root,
        "scan": entries,
        # 扫描不完整时服务端会跳过删除判定，避免把漏扫误判成删除
        "scan_complete": complete,
        "partials": list(partials),
        # 本地已确认完整的下发资源。服务端据此把下发目标标为完成 ——
        # 由 Agent 显式上报，而不是让服务端从"partials 里没有它"去推断
        "completed_assets": list(completed_assets or []),
        # 本机有没有可用的发布公钥（升级信任锚）。服务端据此决定"能不能下发
        # 远程卸载授权"：没有公钥的机器一定验不了签，发了也是白发。
        # 只报"在不在"，不含任何密钥内容。
        "release_public_key": bool(release_public_key),
        "stats": final_stats,
    }
    return payload, oversize, errors


class Agent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)

        self.machine_id = resolve_machine_id(config.machine_id, config.state_dir)
        #: 本机身份：**优先读安装器写的权威那份**（<config-dir>/machine_uuid），
        #: 读不到才退回状态目录里那份。远程卸载令牌绑的就是这个值。
        self.machine_uuid = resolve_machine_uuid(
            config.state_dir, config.machine_uuid_file
        )
        #: 硬件指纹：快照还原后仍然不变，服务端靠它认回"原来那台机器"
        self.machine_fingerprint = resolve_machine_fingerprint()
        self.client = AgentClient(
            base_url=config.base_url,
            verify_tls=config.verify_tls,
            ca_file=config.ca_file,
            timeout=config.request_timeout,
            download_timeout=config.download_timeout,
        )
        self.cache = HashCache.load(config.hash_cache_path)
        self.policy: Dict[str, object] = dict(DEFAULT_POLICY)

        self._stop = False
        self._backoff = 0.0
        self._pending_events: List[dict] = []
        #: 已确认完整、待上报给服务端的下发资源
        self._completed_assets: List[int] = []
        self._credential: Optional[Credential] = None
        #: 收到"激活新版本"后置位，主循环据此退出让 systemd 重启
        self._restart_requested = False
        #: 连续成功周期计数，用于确认新版本可用后清掉回滚状态
        self._healthy_cycles = 0
        self._public_key = self._load_release_public_key()
        #: 连续多少轮连不上服务端。攒够了才去局域网里问一次地址 ——
        #: 服务端重启一下、网线抖一下都会连不上，而那些情况下地址并没有变，
        #: 每轮都广播只会打扰整个局域网（见 REDISCOVER_AFTER_FAILURES）
        self._network_failures = 0
        #: 扫描根与上报前缀。注册拿到准考证号之后才能确定，见 _resolve_roots()
        self._roots: List[Tuple[str, Path]] = []
        self._roots_ready = False
        #: 服务端可见路径 -> 本地绝对路径。每轮扫描后重建
        self._local_paths: Dict[str, Path] = {}
        #: 上一轮结束时机器处于哪一态。**状态变化**时才报审计事件 ——
        #: 每轮都报会把事件通道刷满，而它本来是用来查异常的
        self._last_state: Optional[str] = None

        #: 远程卸载：上一次失败的退避截止时刻（monotonic 秒）。**绝不每轮都去撞
        #: sudo** —— 失败可能是 sudoers 没装好、令牌过期、脚本缺失，连着撞只会刷日志。
        self._uninstall_blocked_until = 0.0
        #: 上一次失败的摘要（进 tick 的 stats.last_error；**绝不含令牌**）。
        self._last_error: Optional[str] = None
        #: 执行卸载命令的 runner：``(argv, stdin_bytes) -> (returncode, stderr)``。
        #: 默认真的跑 `sudo -n`；测试里换成假的，不碰 sudo 与 systemd。
        self._run_uninstall = _run_uninstall_command
        #: "还没注册凭据、在等注册单元"这条日志只说一次（别的每轮轮询不刷屏）
        self._registration_logged = False
        #: 上一轮"用不了的扫描根"（路径 -> 原因）。日志只在状态变化时说一次 ——
        #: 开考前那个目录**本来就不该存在**，每轮刷一条会把真正的问题埋掉。
        self._scan_missing_prev: Dict[str, str] = {}
        #: 已经记过"碰目录 mtime 失败"的目录（每个目录只说一次，debug 级）
        self._refresh_nudge_failed: set = set()
        #: 桌面一致性核对只说一次
        self._desktop_checked = False
        #: 整轮看门狗：秒数（见 WATCHDOG_MIN_SECONDS）与"这一轮有没有装着"
        self.watchdog_seconds = max(
            WATCHDOG_MIN_SECONDS, float(config.request_timeout) * 3
        )
        self._watchdog_active = False
        # ---- 诊断回传 ----
        #: 上一次**尝试**上传的时刻（成功、被限速、失败都算）。周期与最小间隔
        #: 都看它，所以"发失败"不会变成每轮都撞。
        self._diag_last_attempt = 0.0
        #: 攒着的"立即补一份"请求：``"manual"`` 或 ``"error"``（manual 优先）
        self._diag_pending: Optional[str] = None
        #: 连续上传失败次数（成功或 429 清零）
        self._diag_failures = 0
        #: 给诊断包用的几个计数（tick 次数、最近一次错误、服务端给的下一轮间隔）
        self._diag_tick_count = 0
        self._diag_last_error: Optional[str] = None
        self._diag_next_tick: Optional[int] = None
        #: 连续失败的轮数（诊断包的 tick.consecutive_failures，也是"出错补一份"的判据）
        self._consecutive_failures = 0
        #: 时钟可注入（测试推进时间，不用真等 10 分钟）
        self._monotonic = time.monotonic

    # ---------------------------------------------------------------- #
    # 生命周期
    # ---------------------------------------------------------------- #

    def request_stop(self, *_args) -> None:
        if not self._stop:
            log.info("收到停止信号，将在当前轮结束后退出")
        self._stop = True

    def _resolve_roots(self, player_no: str, contest_slug: str = "") -> List[Tuple[str, Path]]:
        """确定扫描根与上报前缀。

        前缀策略：
          ``none``  不加前缀 —— 本机只有一个选手时路径最干净
          ``auto``  取根目录名 —— 配了多个扫描目录时用它区分同名文件
          其他      字面量（支持 ``{player_no}`` / ``{contest_slug}``）

        **必须在拿到凭据后调用**：默认的扫描目录是 ``桌面/<准考证号>``，
        准考证号是注册的产物。注册之前那个目录叫什么名字根本无从得知。

        **刻意不创建缺失的根**：选手的考号目录由他自己保存文件时产生，我们不代他
        建 —— 现场表现是"老师打开桌面就多出一堆空文件夹"，而且那还掩盖了"这台机器
        根本没在收文件"这件事。缺失的根由 :meth:`_missing_scan_roots` 逐轮识别、
        上报到 ``stats.scan_missing``，扫描时跳过它（当作"这个根现在没有文件"）。
        """
        roots = self.config.resolved_roots(player_no, contest_slug)
        if not roots:
            raise ConfigError("scan.roots 至少要配置一个目录")

        policy = (self.config.scan_prefix or PREFIX_NONE).strip()
        named: List[Tuple[str, Path]] = []
        used: Dict[str, Path] = {}

        for root in roots:
            if policy == PREFIX_NONE:
                name = ""
            elif policy == PREFIX_AUTO:
                name = root.name or "root"
            else:
                name = expand_placeholders(
                    policy, player_no=player_no, contest_slug=contest_slug
                )

            if name:
                if name in used:
                    raise ConfigError(
                        "扫描目录的上报前缀重复：%s（%s 与 %s）。"
                        "请改 scan.prefix，或给其中一个目录换个名字。"
                        % (name, used[name], root)
                    )
                used[name] = root
            named.append((name, root))

        return named

    def _root_problem(self, root: Path) -> Optional[str]:
        """这个扫描根为什么用不了；能用就返回 ``None``。

        "不存在"与"被一个同名文件占着"都给一句人话 —— 后者尤其要说清：
        教师看着配置没错，而现场表现是"一个文件都收不上来"。
        """
        if root.is_dir():
            return None
        if root.exists():
            return "不是一个目录（被同名文件占着）"
        return "目录不存在"

    def _missing_scan_roots(self) -> Dict[str, str]:
        """当前用不了的扫描根：``绝对路径 -> 原因``。

        **每轮重算**，不吃 ``_roots_ready`` 缓存 —— 根会出现（选手开始保存文件）
        也可能消失，缓存了就等于永远不更新。顺便按"状态变化"记一次日志：
        缺失时记一次，后来出现了再记一次"已出现"，中间不刷。
        """
        missing: Dict[str, str] = {}
        for _name, root in self._roots:
            problem = self._root_problem(root)
            if problem is not None:
                missing[str(root)] = problem

        appeared = [path for path in self._scan_missing_prev if path not in missing]
        newly_missing = [path for path in missing if path not in self._scan_missing_prev]
        for path in sorted(newly_missing):
            log.warning(
                "扫描根不可用（%s），本轮跳过它（选手保存文件后会自动出现）：%s",
                missing[path],
                path,
            )
        for path in sorted(appeared):
            log.info("扫描根已出现，开始扫描：%s", path)
        self._scan_missing_prev = dict(missing)
        return missing

    def _ensure_roots(self, player_no: str, contest_slug: str = "") -> None:
        if self._roots_ready:
            return
        self._roots = self._resolve_roots(player_no, contest_slug)
        self._roots_ready = True
        log.info(
            "扫描目录: %s",
            ", ".join(
                "%s%s" % (name + "/" if name else "", root) for name, root in self._roots
            ),
        )

    # ---------------------------------------------------------------- #
    # 扫描
    # ---------------------------------------------------------------- #

    def _ensure_credential(self) -> Optional[Credential]:
        return self.ensure_credential()

    def ensure_credential(self, *, allow_enroll: bool = False) -> Optional[Credential]:
        """确保手上有一份可用凭据；没有就返回 ``None``（或按参数去注册）。

        **常驻服务永不自己注册**（``allow_enroll=False``，默认）：注册密钥是 root
        只读的，注册由 root 的 ``syncoj-agent-enroll.service`` 做一次。Agent 以
        选手身份跑，读不到密钥 —— 去撞它只会 ``PermissionError`` → 抛异常 → 退出
        → 被 systemd 反复重启，而**注册单元从来没被跑过**（真机上就是这么卡住的）。
        所以没凭据时返回 ``None``，由 :meth:`_wait_for_credential` 进入"等凭据"这个
        **正常状态**。

        ``--provision``（root 跑的那条路）传 ``allow_enroll=True`` —— 它才是唯一
        会注册的地方，缺密钥/读不到密钥在那里仍然是错误（那时它是 root，读不到
        就是真的配置错了）。

        公开名字（无下划线）是因为 ``--provision`` 也要用它 —— 装机时的 root
        一次性单元与常驻循环**必须走同一条凭据读取路径**，否则"装机时能注册、
        开机后认不出来"这类问题会以最难查的方式出现。
        """
        if self._credential is not None:
            return self._credential

        stored = load_credential(self.config.credential_path)
        if stored is not None and stored.server_url in ("", self.config.base_url):
            self._credential = stored
            self.client.set_token(stored.token)
            if stored.state == STATE_UNCLAIMED:
                log.info(
                    "这台机器还没配对到人（配对码 %s），等待认领", stored.pair_code
                )
            elif stored.state == STATE_WAITING:
                log.info(
                    "已配对给 %s，但当前没有包含本人的场次，等待开赛",
                    stored.player_no or "?",
                )
            else:
                log.info("使用已保存的凭据：选手 %s @ %s", stored.player_no, stored.contest_slug)
            return stored

        if not allow_enroll:
            return None

        return self._enroll()

    def _enroll(self) -> Credential:
        """注册。**只有一条路**：读镜像里那份 root 只读的统一密钥。

        没有「每选手注册码」那条路了 —— 它已经被"机器永久绑定名单条目"取代
        （见 docs/reference/protocol.md §1）。密钥缺失或读不出来时把下一步动作说清楚 ——
        而不是笼统地说"注册失败"。
        """
        key_path = self.config.bootstrap_key_file or "/etc/syncoj/bootstrap.key"
        bootstrap_key = read_bootstrap_key(self.config.bootstrap_key_file)
        if bootstrap_key is None:
            raise ConfigError(
                "找不到统一密钥文件 %s。\n"
                "  这台机器还没有注册过，而注册只有这一条路 —— 需要镜像里那份\n"
                "  root 只读的统一密钥（在服务端用 syncoj-server bootstrap-key issue 签发）：\n"
                "    sudo install -m 0600 -o root -g root <密钥文件> %s\n"
                "  装好之后重启 %s（它以 root 身份跑一次注册）。"
                % (key_path, key_path, ENROLL_UNIT_NAME)
            )
        if not bootstrap_key:
            raise ConfigError(
                "统一密钥文件 %s 存在但读不出来。\n"
                "Agent 以选手身份运行，读不到 root 只读的文件 —— 这是**设计如此**：\n"
                "密钥能注册整间机房，不该躺在学生读得到的地方。\n"
                "请交给装机时的 %s 去注册。"
                % (key_path, ENROLL_UNIT_NAME)
            )

        hostname = socket.gethostname()
        log.info("正在注册（machine_id=%s，machine_uuid=%s）", self.machine_id, self.machine_uuid)
        data = self.client.enroll(
            bootstrap_key=bootstrap_key,
            machine_id=self.machine_id,
            machine_uuid=self.machine_uuid,
            machine_fingerprint=self.machine_fingerprint,
            hostname=hostname,
            agent_version=__version__,
            os_info=describe_os(),
        )

        credential = Credential(
            token=data["token"],
            agent_id=int(data.get("agent_id") or 0),
            player_no=data.get("player_no") or "",
            contest_id=int(data.get("contest_id") or 0),
            contest_slug=data.get("contest_slug") or "",
            contest_name=data.get("contest_name", ""),
            machine_id=self.machine_id,
            server_url=self.config.base_url,
            claimed=bool(data.get("claimed", True)),
            bound=bool(data.get("bound", True)),
            pair_code=data.get("pair_code") or "",
        )
        # Credential.from_dict 里做过同一件事（未配对就不可能是"能干活"），
        # 这里直接构造对象绕过了它 —— 手工补上，免得后面各处都要再拼一次
        credential.bound = credential.bound and credential.claimed
        save_credential(self.config.credential_path, credential)
        self.client.set_token(credential.token)
        self._credential = credential

        config = data.get("config")
        if isinstance(config, dict):
            self.policy = merge_policy(config)

        reason = str(data.get("reason") or "")
        if credential.state == STATE_READY:
            log.info(
                "注册成功：选手 %s，场次 %s（agent_id=%d）",
                credential.player_no, credential.contest_slug, credential.agent_id,
            )
        elif credential.state == STATE_UNCLAIMED:
            # 未配对**不是失败**：这个 token 是有效的，只是还没有归属。
            # 把它当失败丢掉会导致下一轮重新注册，而服务端每次注册都会换一个
            # 新配对码 —— 教师刚在屏幕上读到的那个当场失效。
            log.warning(
                "这台机器还没有配对到人。配对码：%s\n"
                "请教师在管理界面「机器配对」里用这个码认领到人。",
                credential.pair_code or "（等服务端下发）",
            )
        else:
            log.warning("已配对给 %s，但当前场次里没有这个人：%s",
                        credential.player_no or "?", reason or "（服务端未说明）")

        # **无论哪一态**都要把桌面上的提示文件刷成对的：这次注册直接就是
        # "能干活"时（快照还原后重注册的常见结果），上一轮留下的「配对码.txt」
        # 必须消失 —— 否则下一个人看见会以为这台机器还没配对。
        self._sync_notice_files(credential, reason)
        return credential

    # ---------------------------------------------------------------- #
    # 配对码 / 等待场次：写在桌面上给教师看
    # ---------------------------------------------------------------- #

    def _pair_code_path(self) -> Path:
        return self.config.deploy_root / self.config.pairing_file_name

    def _waiting_file_path(self) -> Path:
        return self.config.deploy_root / WAITING_FILE_NAME

    def _show_pair_code(self, code: str) -> None:
        """把配对码写到桌面上，并在日志里再说一遍。

        为什么要落到桌面：配对码的价值在于**被人在机器前读到**。日志要
        journalctl 才看得到，而教师是走到机器前看屏幕的 —— 桌面上一个
        「配对码.txt」是他最可能看见的东西，也是这个部署方式里唯一顺手的公示位。
        """
        if not self.config.pairing_show_on_desktop or not code:
            return
        path = self._pair_code_path()
        text = (
            "SyncOJ 配对码\n"
            "\n"
            "    %s\n"
            "\n"
            "把这串码告诉老师，或让老师在管理界面「机器配对」里输入它。\n"
            "配对成功后这个文件会自动消失。\n" % code
        )
        self._write_desktop_file(path, text, "配对码")

    def _write_waiting_file(self, reason: str) -> None:
        """写「等待场次.txt」。

        为什么需要这个中间状态：机器绑的是**人**，而"这场比赛有没有这个人"
        取决于那份名单有没有被应用到场次里。所以配对成功不等于马上能干活 ——
        说不出来的话，客户端只能猜，猜错的后果是它去扫一个用准考证号展开不出来的
        目录；而教师看到的是"这台机器就是不收代码"。
        """
        if not self.config.pairing_show_on_desktop:
            return
        path = self._waiting_file_path()
        text = (
            "SyncOJ 等待场次\n"
            "\n"
            "    %s\n"
            "\n"
            "这台机器已经配对好了，但当前场次里还没有你。\n"
            "请告诉老师，等老师把名单应用到场次里；之后这个文件会自动消失。\n"
            % (reason.strip() or "还没有包含你的场次")
        )
        self._write_desktop_file(path, text, "等待场次说明")

    def _write_desktop_file(self, path: Path, text: str, label: str) -> None:
        """把提示文件刷成 ``text``；**内容已经一致就什么都不做**。

        为什么还要"每轮都来"：教师可能刚开机才看桌面，而上一次写是在重启之前
        ——文件被删掉、被改坏、或被别的程序覆盖，都要能自己补回来。

        为什么"一致就不写"：以前是无条件重写，心跳改成 30 秒之后
        「等待场次.txt」会在 ``agent.log`` 里留下一天几千行"已写到…"，而日志
        4 MB 就轮转 —— 真正要看的"状态变化"会被这些"我还活着"冲掉。

        **自愈性质没丢**：文件被人删掉 = 读不到 = 内容不一致 → 下一轮自然补写。
        这条日志的价值在"状态（或内容）变了"，不在"进程还活着"。
        """
        try:
            if _read_desktop_text(path) == text:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text, mode=0o644)
            log.info("%s 已写到 %s", label, path)
        except OSError as exc:
            # 写不进去不算致命：日志里还有一份，教师可以查日志
            log.warning("无法把%s写到 %s：%s（日志里还有一份）", label, path, exc)

    def _remove_desktop_file(self, path: Path, label: str) -> None:
        try:
            path.unlink()
            log.info("已删除%s %s", label, path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("无法删除%s %s：%s", label, path, exc)

    def clear_pair_code_file(self) -> None:
        """配对成功后把桌面上的那个文件删掉。

        留着它会让人以为还没配对 —— 下一个人走过来看见，就会去问老师，
        老师再查一遍，白折腾一轮。
        """
        self._remove_desktop_file(self._pair_code_path(), "配对码文件")

    def clear_waiting_file(self) -> None:
        """拿到场次之后把「等待场次.txt」删掉。理由同上。"""
        self._remove_desktop_file(self._waiting_file_path(), "等待场次文件")

    def _show_state_files(self, credential: Credential, reason: str = "") -> None:
        """按当前三态把桌面上的提示文件刷成对的。

        **每轮都来核对一遍**（不只是状态变化时）：教师可能刚开机才来看，文件
        可能在上次写完之后被删掉/被改坏 —— 那都要能补回来。但**内容一致就不写、
        也不记日志**（判据在 :meth:`_write_desktop_file`）：心跳 30 秒一轮，
        无条件重写会把 agent.log 刷满。无关的文件也要清掉 ——
        「配对码.txt」和「等待场次.txt」同时躺在桌面上，谁也不知道该信哪个。

        ``reason`` 是服务端给的那句话，直接写进「等待场次.txt」：**只有服务端
        知道为什么**（"这场没有你"还是"名单还没应用"），客户端猜不出来。
        """
        if credential.state == STATE_UNCLAIMED:
            self.clear_waiting_file()
            if credential.pair_code:
                self._show_pair_code(credential.pair_code)
        elif credential.state == STATE_WAITING:
            self.clear_pair_code_file()
            self._write_waiting_file(reason)
        else:
            self.clear_pair_code_file()
            self.clear_waiting_file()

    def _sync_notice_files(self, credential: Credential, reason: str = "") -> None:
        """状态**变化**时把桌面上的提示文件刷成对的，并留一条审计事件。

        只在变化时做：每轮都去 unlink 是每两秒两次系统调用，而它要防的那件事
        （桌面上留着一个已经作废的提示文件）只在状态刚变的那一瞬间才会出现。
        """
        if self._last_state == credential.state:
            return
        self._note_state(credential, reason)
        self._show_state_files(credential, reason)

    def _note_state(self, credential: Credential, reason: str = "") -> str:
        """状态变化时留一条审计事件，返回新状态。

        **只在变化时报**：这一轮的 tick 每 2~60 秒就可能来一次，每轮报一条会让
        事件列表全是噪音，而它本来是给"出事了"用的。
        """
        state = credential.state
        if state == self._last_state:
            return state
        previous, self._last_state = self._last_state, state
        log.info("机器状态：%s -> %s", previous or "（启动）", state)
        if state == STATE_UNCLAIMED:
            self._emit(
                "info", "pairing_required",
                "这台机器还没有配对到人，配对码：%s" % (credential.pair_code or "?"),
                {"pair_code": credential.pair_code},
            )
        elif state == STATE_WAITING:
            message = reason.strip() or "已配对，但当前没有包含本人的场次"
            self._emit("info", "pairing_wait", message)
        return state

    def _adopt_identity(self, data: Dict[str, object]) -> Credential:
        """从 tick 响应里接住当前状态以及随之而来的身份。

        服务端把身份放在每次 tick 的响应里，而不是要求 Agent 再 enroll 一次 ——
        少一次可能失败的网络往返，也不会多刷一条注册审计。

        三种状态都走这里：服务端才是权威，它说"现在还没配对"那就还没配对。
        """
        credential = self._credential
        if credential is None:  # pragma: no cover - 调用方已经确保
            return credential  # type: ignore[return-value]

        credential.claimed = bool(data.get("claimed", True))
        credential.bound = bool(data.get("bound", True)) and credential.claimed
        credential.player_no = str(data.get("player_no") or "")
        credential.contest_slug = str(data.get("contest_slug") or "")
        credential.contest_id = int(data.get("contest_id") or 0)
        credential.contest_name = str(data.get("contest_name") or "")
        # 未配对时服务端可能每轮都带一个新码回来（旧的可能已过期），接住它
        credential.pair_code = str(data.get("pair_code") or "")
        save_credential(self.config.credential_path, credential)

        self._note_state(credential, str(data.get("reason") or ""))
        self._show_state_files(credential, str(data.get("reason") or ""))
        return credential

    def _mark_unbound(self, reason: str) -> None:
        """服务端说"这台机器还没配对到人"时，把本地状态同步过去。

        服务端是权威。不同步的话，我们会继续扫一个用准考证号展开不出来的目录，
        每一轮都失败 —— 看起来像"Agent 坏了"，其实是本地状态没跟上服务端。
        """
        credential = self._credential
        if credential is None:  # pragma: no cover - 调用方已经确保
            return
        credential.claimed = False
        credential.bound = False
        save_credential(self.config.credential_path, credential)
        self._note_state(credential, reason)
        self._show_state_files(credential)

    def _forget_credential(self, reason: str) -> None:
        """凭据被服务端拒绝时清空本地凭据，**回到"等注册"状态**。

        **服务不会自己重新注册**：注册密钥是 root 只读的，注册只由
        :data:`ENROLL_UNIT_NAME` 那条 root 一次性单元做（见
        :meth:`ensure_credential`）。所以清掉之后的下一步是把那个单元再跑一次
        —— 它每次开机都会跑（``WantedBy=multi-user.target``），也可以马上手工
        触发。日志里必须说清这一步，否则现场只看到"凭据失效"，然后机器一直不出现。
        """
        log.warning(
            "凭据被服务端拒绝（%s），已清除本地凭据，回到等待注册状态。\n"
            "  Agent 自己不会重新注册（注册密钥 root 只读）；请让注册单元再跑一次：\n"
            "  systemctl reset-failed %s; systemctl start %s（下次开机也会自动重试）",
            reason,
            ENROLL_UNIT_NAME,
            ENROLL_UNIT_NAME,
        )
        self._credential = None
        self._registration_logged = False
        self.client.set_token(None)
        self._roots_ready = False
        self._roots = []
        # 把状态记忆也清掉：重新注册之后可能直接就是另一态，
        # 不清的话"未配对 -> 未配对"这种变化会被当成没变化，审计事件就不报了
        self._last_state = None
        try:
            self.config.credential_path.unlink()
        except OSError:
            pass

    # ---------------------------------------------------------------- #
    # 扫描
    # ---------------------------------------------------------------- #

    def _scan(self):
        """扫描全部根目录，返回 ``(扫描结果列表, 本地路径索引, 有没有根没扫成)``。

        **用不了的根直接跳过**（不创建、不报错、不进 ``last_error``）：它现在就是
        "没有文件"，不是故障。哪几个根用不了由 :meth:`_missing_scan_roots` 单独
        识别并上报，扫描这里只负责别去碰它。
        """
        scan_policy = ScanPolicy.from_mapping(self.policy)
        max_files = int(self.policy.get("max_files", 5000))  # type: ignore[arg-type]
        results = []
        local_paths: Dict[str, Path] = {}
        skipped = 0

        for name, root in self._roots:
            if self._root_problem(root) is not None:
                skipped += 1
                continue
            outcome = scan_directory(root, scan_policy, self.cache, max_files=max_files)
            results.append((name, outcome))
            for entry in outcome.entries:
                local_paths[join_report_path(name, entry.path)] = root / entry.path

        return results, local_paths, skipped

    def _build_tick_payload(self, results, local_paths, scan_complete: bool = True):
        return build_tick_payload(
            machine_id=self.machine_id,
            agent_version=__version__,
            scan_root=", ".join(str(r) for _n, r in self._roots)[:512],
            results=results,
            # 未完成下载的偏移量从磁盘实况收集，不依赖上一轮作业列表 ——
            # Agent 重启后照样能续上
            partials=[
                {"asset_id": asset_id, "bytes_done": bytes_done}
                for asset_id, bytes_done in find_partial_sizes(self.config.deploy_root).items()
            ],
            stats={
                "disk_free": _disk_free(self.config.state_dir),
                "queue": len(self._pending_events),
                # 上一次失败的摘要（比如按管理端授权卸载失败的原因）。**绝不含
                # 令牌** —— 令牌是一次 root 删除的凭据，不进任何上报字段。
                "last_error": self._last_error,
                # 哪几个扫描根现在用不了（绝对路径）。**每轮都报** —— 服务端拿它
                # 做后台提示，而"根出现了"这件事只有每轮报文里才看得出来。
                "scan_missing": sorted(self._missing_scan_roots()),
            },
            # 上一轮下载完成的结果在这里回报。差一轮无所谓 —— 而且服务端在收到
            # 回报前会继续下发该作业，Agent 会走 "内容已一致" 的跳过分支并再次
            # 上报，这恰好让"回报丢失"能自愈。
            completed_assets=self._completed_assets,
            scan_complete=scan_complete,
            # 服务端据此决定"能不能发远程卸载授权"：没有信任锚的机器一定验不了签
            release_public_key=self._has_release_public_key(),
        )

    # ---------------------------------------------------------------- #
    # 上传
    # ---------------------------------------------------------------- #

    def _upload(self, paths: List[str]) -> Tuple[int, int]:
        """串行上传。

        **刻意不并发**：``http.client`` 的连接对象不是线程安全的，每个文件用
        独立连接又会丢掉 keep-alive 的收益。而单文件上限只有 2MB（服务端策略
        决定），50 个文件在千兆内网上是一秒级的开销 —— 并发带来的复杂度
        （连接池、错误聚合、部分失败语义）换不来可感知的收益。
        """
        ok = 0
        failed = 0
        for rel_path in paths:
            if self._stop:
                break
            local = self._local_paths.get(rel_path)
            if local is None or not local.is_file():
                # 文件在扫描之后被删了/改名了，跳过即可，下一轮 tick 会同步掉
                log.debug("待上传文件已不存在，跳过：%s", rel_path)
                continue

            try:
                # 以磁盘**当前**内容重算哈希，而不是用 tick 时报的那个：
                # 若文件在这期间又变了，服务端会按"迟到旧版本"（409）拒绝，
                # 下一轮 tick 自然会拿到新版本。这比发送过期哈希导致的
                # 400 "哈希不符" 更准确，也不会污染错误统计。
                actual = sha256_file(local)
                self.client.upload_file(rel_path, local, actual)
                ok += 1
            except StaleUpload as exc:
                log.info("上传 %s 被标记为过期版本，交给下一轮：%s", rel_path, exc)
            except PayloadTooLarge as exc:
                failed += 1
                self._emit("warning", "upload_rejected", "服务端拒绝过大文件 %s: %s" % (rel_path, exc))
            except (AuthError, RateLimited):
                # 凭据坏了要重新注册、被限速要按服务端的节奏退避 —— 两件事都
                # 得让整轮停下来交给 run_forever，不能在这里当"这个文件失败了"
                raise
            except (NetworkError, ProtocolError, AgentError) as exc:
                failed += 1
                log.warning("上传 %s 失败：%s", rel_path, exc)
                # 网络类错误通常影响后续所有上传，早点退出这轮更高效
                if isinstance(exc, NetworkError):
                    break
            except OSError as exc:
                failed += 1
                log.warning("读取 %s 失败：%s", rel_path, exc)
        return ok, failed

    # ---------------------------------------------------------------- #
    # 下载
    # ---------------------------------------------------------------- #

    def _download(self, jobs: List[dict]) -> Tuple[int, int]:
        ok = 0
        failed = 0
        for job in jobs:
            if self._stop:
                break
            try:
                outcome = download_asset(
                    self.client, job, self.config.deploy_root, on_progress=None
                )
            except Exception as exc:  # pragma: no cover - download_asset 已兜底
                failed += 1
                log.exception("下发作业执行异常: %s", exc)
                continue

            if outcome.status == "ok":
                ok += 1
                self._note_completed(outcome.asset_id)
                log.info(
                    "下发完成 %s（%d 字节）",
                    outcome.dest, outcome.bytes_written,
                )
                # 只有**确实写进去**才碰目录 mtime（跳过/失败都不碰）：
                # 目的是让文件管理器立刻显示出新文件，而不是替它做轮询
                self._nudge_after_write(outcome.dest)
            elif outcome.status == "skipped":
                ok += 1
                # 内容已一致也算完成 —— Agent 重启后正是靠这条路径把"其实早就
                # 下好了"的事实补报给服务端，避免任务永远停在"进行中"
                self._note_completed(outcome.asset_id)
                log.debug("下发跳过 %s（内容已一致）", outcome.dest)
            else:
                failed += 1
                log.warning("下发失败 %s：%s", outcome.dest, outcome.error)
                self._emit(
                    "warning", "deploy_failed",
                    "下发 %s 失败：%s" % (outcome.dest, outcome.error),
                    {"asset_id": outcome.asset_id, "dest": outcome.dest},
                )
        return ok, failed

    # ---------------------------------------------------------------- #
    # 桌面刷新（best-effort）
    # ---------------------------------------------------------------- #

    def _nudge_desktop_refresh(self, directory: Path) -> None:
        """碰一下目录的 mtime，让文件管理器注意到"这里刚多了东西"。

        现场报的是"下发完了但桌面上不刷新，要进文件夹里才看得到"。落盘本身是对的
        （``.syncoj-part`` + 同目录 ``os.replace``），缺的是**通知桌面** ——
        文件管理器（GNOME Files / Nautilus 等）监听目录变更事件，而"目录 mtime 变了"
        是最常见、最不挑实现的触发条件。

        **纯 best-effort**：失败只记一次 debug（每个目录一次），绝不向上抛 ——
        文件已经落盘了，刷新不了只是"要多点一下刷新"，不能因此把下发判成失败。
        """
        try:
            os.utime(str(directory), None)
        except OSError as exc:
            key = str(directory)
            if key not in self._refresh_nudge_failed:
                self._refresh_nudge_failed.add(key)
                log.debug("碰目录 mtime 失败（不影响下发，只是桌面可能要手动刷新）：%s: %s", directory, exc)

    def _nudge_after_write(self, dest: str) -> None:
        """一次下发**确实写成功之后**才调用：碰该刷新的目录。

        分开两个目录是有原因的：文件落在 ``deploy_root`` 根上时，要刷新的是桌面
        自己；落在子目录里时，桌面上那张图标没变（子目录早就在），要刷新的是那个
        子目录 —— 但**子目录可能是这一次才建出来的**，那时桌面上会多出一个文件夹，
        所以桌面根也要碰一下。两条都只是 ``utime``，代价可以忽略。
        """
        parent = (self.config.deploy_root / dest).parent
        self._nudge_desktop_refresh(parent)
        if parent != self.config.deploy_root:
            self._nudge_desktop_refresh(self.config.deploy_root)

    # ---------------------------------------------------------------- #
    # 事件
    # ---------------------------------------------------------------- #
    def _note_completed(self, asset_id: int) -> None:
        """记录一个已确认完整的下发资源，等待下一轮 tick 上报。"""
        if asset_id and asset_id not in self._completed_assets:
            self._completed_assets.append(asset_id)

    # ---------------------------------------------------------------- #
    # 自更新
    # ---------------------------------------------------------------- #

    def _load_release_public_key(self) -> Optional[RSAPublicKey]:
        path = self.config.release_public_key
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("发布公钥读取失败 %s: %s", path, exc)
            return None
        try:
            return RSAPublicKey.from_dict(data)
        except SignatureError as exc:
            log.error("发布公钥格式不合法 %s: %s", path, exc)
            return None

    def _has_release_public_key(self) -> bool:
        """本机有没有可用的发布公钥（升级信任锚）。

        每次心跳重新看一眼，而不是复用启动时那份：文件是安装器放的，但运维也
        可能后补 —— 早期报 False 会让服务端不发卸载授权，教师那边看到的是
        "点了没反应"。这里**静默**判定（`_load_release_public_key` 会打日志，
        那是启动期该做的事，心跳里每轮打一条就把 journal 刷爆了）。
        """
        path = self.config.release_public_key
        if path is None:
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            RSAPublicKey.from_dict(data)
        except (OSError, ValueError, SignatureError, AttributeError):
            return False
        return True

    def _handle_uninstall_token(self, token: str) -> bool:
        """收到一次性卸载授权：交给自卸载脚本，成功就退出。返回是否已执行卸载。

        **令牌只走 stdin**：不进 argv（ps 能看到）、不写文件、不进日志、不进
        ``last_error``。失败要退避 —— 失败原因多半是 sudoers/令牌/脚本的问题，
        每轮都撞一次 sudo 只会把日志刷爆。
        """
        now = time.monotonic()
        if now < self._uninstall_blocked_until:
            log.debug(
                "卸载授权还在退避窗口内，跳过（还有 %.0f 秒）",
                self._uninstall_blocked_until - now,
            )
            return False

        payload = token.encode("utf-8")
        if len(payload) > MAX_UNINSTALL_TOKEN_BYTES:
            # 连长度都不对，不用去撞 sudo；但仍要退避，否则每轮都会走到这里
            log.warning("卸载授权令牌过长（%d 字节），拒绝执行", len(payload))
            self._uninstall_blocked_until = now + UNINSTALL_RETRY_SECONDS
            self._last_error = "按管理端授权卸载失败：令牌过长"
            self._emit("error", "uninstall_failed", self._last_error)
            return False

        argv = ["sudo", "-n", SELF_UNINSTALL_PATH]
        try:
            returncode, stderr = self._run_uninstall(argv, payload)
        except Exception as exc:  # pragma: no cover - 注入的 runner 不该抛
            returncode, stderr = 127, "执行卸载脚本失败: %s" % exc

        if returncode == 0:
            # 单元是 Restart=on-failure，exit 0 不会被再拉起来。
            log.warning("已按管理端授权卸载，本机 Agent 将退出")
            self._last_error = None
            self._stop = True
            return True

        summary = self._redact(" ".join((stderr or "").split()), token)[:200]
        if not summary:
            summary = "退出码 %d" % returncode
        message = "按管理端授权卸载失败：%s" % summary
        log.error("%s（%.0f 秒后再试）", message, UNINSTALL_RETRY_SECONDS)
        self._uninstall_blocked_until = now + UNINSTALL_RETRY_SECONDS
        self._last_error = message
        self._emit("error", "uninstall_failed", message)
        return False

    @staticmethod
    def _redact(text: str, secret: str) -> str:
        """把不该出现的令牌从文本里抹掉（底层命令万一把 stdin 回显出来时兜底）。"""
        if secret and secret in text:
            return text.replace(secret, "<token>")
        return text

    def _guard_boot(self) -> bool:
        """启动守卫。返回 True 表示刚完成回滚，调用方应当退出让 systemd 重启。

        自更新最可怕的失败模式不是恶意包（签名挡住了），而是**新版本起不来** ——
        一次操作把 50 台考试机同时变成砖。签名对此毫无帮助，只有回滚能救。
        """
        if self.config.upgrade_mode == UpgradeMode.OFF:
            return False

        install_root = self.config.install_root
        running = current_release(install_root) or __version__
        rollback_to = note_boot(install_root, running)
        if rollback_to is None:
            return False

        try:
            rollback_release(install_root, rollback_to)
        except UpgradeError as exc:
            log.error("自动回滚失败: %s", exc)
            self._emit("error", "upgrade_rollback_failed", "自动回滚失败：%s" % exc)
            return False

        log.warning("版本 %s 反复启动失败，已回滚到 %s", running, rollback_to)
        self._emit(
            "warning", "upgrade_rolled_back",
            "版本 %s 反复启动失败，已自动回滚到 %s" % (running, rollback_to),
        )
        return True

    def _handle_upgrade(self, info: dict) -> None:
        """处理服务端下发的发布清单。任何失败都只是记录，绝不中断服务。"""
        mode = self.config.upgrade_mode
        version = str(info.get("version") or "?")

        if mode == UpgradeMode.OFF:
            # 默认路径。刻意不做任何动作 —— 静默地在考试机上升级是高风险操作，
            # 必须由教师显式开启。
            log.info("服务端提供 Agent 版本 %s，但 upgrade.mode=off，不做任何动作", version)
            return

        public_key = self._public_key
        if public_key is None:
            self._emit(
                "warning", "upgrade_no_key",
                "upgrade.mode=%s 但缺少可用的发布公钥，拒绝升级 %s" % (mode, version),
            )
            return

        try:
            manifest = ReleaseManifest.from_dict(info)
        except UpgradeError as exc:
            self._emit("warning", "upgrade_bad_manifest", "发布清单不合法：%s" % exc)
            return

        install_root = self.config.install_root
        running = current_release(install_root) or __version__

        # **版本号管降级、sha 管内容**（与安装器 install.py 同一套记录）：
        #
        #   * 低于当前版本 → 一律拒绝。否则攻击者可以重放一个历史版本的真实
        #     签名包，把 Agent 退回已知有漏洞的状态。
        #   * 等于当前版本 → 不再一律 return，而是比内容指纹：清单 sha256 与
        #     "本机 releases/<版本>/ 里记的那一份"不同 = 同版本重建，照常
        #     stage/apply；相同 = 真的无操作（幂等，不反复下载/解包）。
        #
        # 接受的一面：**同一版本号内的内容回滚从此理论可行** —— 有人重放一份
        # 旧的、签过名的同版本 manifest，会被当成"内容不同"照单接收。跨版本号的
        # 降级闸仍是上面那条比较；"同版本号内的内容回退"事前无法与"同版本重建"
        # 区分，所以只能接受这个代价（好在仍要过签名）。
        # 另一面：同版本内容换掉之后**没有"上一份"可退**，所以 apply 时不给这种
        # 替换设回滚点（见下面那段），启动守卫会走"无路可退就清状态"那条路，
        # 而不是把同一个（可能已坏的）版本反复回滚。
        try:
            manifest_version = parse_version(manifest.version)
            running_version = parse_version(running)
        except UpgradeError as exc:
            log.warning("版本号无法比较，拒绝升级: %s", exc)
            return

        if manifest_version < running_version:
            log.debug(
                "忽略低于当前版本的发布 %s（当前 %s）", manifest.version, running
            )
            return

        if manifest_version == running_version:
            installed_sha = read_release_fingerprint(install_root, running)
            if installed_sha is not None and installed_sha == manifest.sha256:
                log.debug(
                    "同版本 %s 且内容指纹一致，无操作", manifest.version
                )
                return
            log.warning(
                "同版本号 %s 但内容指纹不同（本机 %s / 清单 %s），按覆盖安装处理",
                manifest.version,
                (installed_sha or "无记录")[:12],
                manifest.sha256[:12],
            )

        bundle = self.config.state_dir / "upgrade" / ("%s.tar.gz" % manifest.version)
        try:
            size = self.client.download_to_file(manifest.url, bundle, MAX_BUNDLE_BYTES)
            log.info("已下载发布包 %s（%d 字节），开始校验", manifest.version, size)
            verify_bundle(public_key, bundle, manifest)
            stage_release(install_root, manifest.version, bundle)
        except (UpgradeError, AgentError) as exc:
            log.error("升级包处理失败: %s", exc)
            self._emit("warning", "upgrade_failed", "升级 %s 失败：%s" % (version, exc))
            # 升级失败是典型的"必须留现场"：下一轮就把诊断包补上（受 60 秒
            # 最小间隔约束），管理端不必等机器彻底离线才发现。
            self.request_diagnostics("error")
            return

        log.info("版本 %s 已暂存到 %s", manifest.version, install_root)
        if mode != UpgradeMode.APPLY:
            self._emit(
                "info", "upgrade_staged",
                "版本 %s 已下载并通过签名校验，等待激活（当前 mode=stage）" % manifest.version,
            )
            return

        # 顺序很重要：**先写回滚状态再切软链**。
        # 反过来的话，切完软链、写状态之前崩溃，新版本就没有任何保护。
        # 按现在的顺序，中途崩溃只会让状态指向一个尚未激活的版本 ——
        # note_boot 发现版本不符会直接丢弃，不会误判。
        try:
            previous = current_release(install_root)
            if previous == manifest.version:
                # **同版本内容替换没有"上一个版本"可退。**
                #
                # 拿同一个版本目录当 previous，回滚只会回到那份已经被换成新内容的
                # 目录，于是变成"回滚→重启→还起不来"的死循环，比不回滚更糟。
                # 所以不留可回滚点：把 previous 记成 None，让启动守卫走
                # "无路可退就清状态"那条路（note_boot 在 previous 为空时放弃
                # 守卫，而不是反复重启）。
                #
                # 想留退路，正解是**换版本号** —— 版本单调本来就是这套设计的假设。
                # 支持同版本重建是为了"内容能到机器上"，不是为了"还有个旧版本
                # 可以退"。
                log.warning(
                    "同版本号 %s 内容替换：没有可回滚的旧版本，启动守卫不设回滚点",
                    manifest.version,
                )
                previous = None
            save_state(
                install_root,
                UpgradeState(
                    version=manifest.version,
                    previous=previous,
                    applied_at=time.time(),
                ),
            )
            activate_release(install_root, manifest.version)
        except UpgradeError as exc:
            log.error("激活版本 %s 失败: %s", manifest.version, exc)
            self._emit("error", "upgrade_activate_failed", "激活失败：%s" % exc)
            self.request_diagnostics("error")
            return

        log.warning(
            "已激活版本 %s（原 %s），即将重启以生效",
            manifest.version,
            previous or "无",
        )
        self._emit(
            "warning", "upgrade_activated",
            "已升级到 %s 并重启（原版本 %s）" % (manifest.version, previous or "无"),
        )
        # 由 systemd 的 Restart=always 拉起新版本 —— 不需要我们自己调 systemctl，
        # 那会要求额外的 polkit 授权，平白扩大 Agent 的权限面
        self._restart_requested = True

    # ---------------------------------------------------------------- #
    # 诊断回传
    # ---------------------------------------------------------------- #

    def request_diagnostics(self, reason: str = "manual") -> None:
        """记下"下次有机会就补一份"。``manual``（管理端按钮）优先于 ``error``。"""
        if reason == "manual" or self._diag_pending is None:
            self._diag_pending = reason

    def _diagnostics_reason_now(self) -> Optional[str]:
        """现在该不该传、以什么理由。返回 ``None`` 表示还不到时候。"""
        now = self._monotonic()
        if self._diag_pending:
            # 立即补一份，但仍要守住 60 秒最小间隔（服务端也只允 60 秒一份）。
            # 守住的方式是**留着这个请求**，下一轮再看 —— 不是丢掉它。
            if now - self._diag_last_attempt >= DIAGNOSTICS_MIN_INTERVAL_SECONDS:
                return self._diag_pending
            return None
        if now - self._diag_last_attempt >= DIAGNOSTICS_PERIOD_SECONDS:
            return "periodic"
        return None

    def _build_diagnostics(self, reason: str) -> bytes:
        bundle = diagnostics.build_bundle(
            version=__version__,
            reason=reason,
            state=(
                self._credential.state if self._credential is not None else STATE_UNCLAIMED
            ),
            machine_id=self.machine_id,
            machine_uuid=self.machine_uuid,
            hostname=socket.gethostname(),
            run_user=self.config.run_user or _current_user(),
            os_info=describe_os(),
            tick={
                "count": self._diag_tick_count,
                "last_error": self._diag_last_error,
                "consecutive_failures": self._consecutive_failures,
                "next_tick_seconds": self._diag_next_tick,
            },
            policy=self.policy,
            config=self.config,
            log_path=self.config.resolved_log_file,
        )
        blob, dropped = diagnostics.encode_bundle(bundle)
        if dropped:
            log.debug("诊断包超过上限，已丢掉日志尾部（其余字段照常上报）")
        return blob

    def _maybe_send_diagnostics(self) -> None:
        """一个"顺便"的动作：该传就传一份。

        **绝不抛异常、绝不影响心跳**：它失败只记 DEBUG（连续多次才 WARNING），
        而且超时只有 15 秒。做不成这件事的唯一后果就是"管理端少看到一份现场"。
        """
        try:
            reason = self._diagnostics_reason_now()
            if reason is None:
                return
            blob = self._build_diagnostics(reason)
            self._diag_last_attempt = self._monotonic()
            if self._diag_pending == reason:
                self._diag_pending = None
            self.client.upload_diagnostics(blob)
            self._diag_failures = 0
            log.debug("诊断包已上传（%s，%d 字节）", reason, len(blob))
        except RateLimited as exc:
            # 服务端限速 = "这次没传成"，不是错误：不重试、不报错，下一轮自然再来
            self._diag_last_attempt = self._monotonic()
            self._diag_failures = 0
            log.debug("诊断包被服务端限速，这次没传成（下一轮再试）：%s", exc)
        except Exception as exc:
            self._diag_last_attempt = self._monotonic()
            self._diag_failures += 1
            if self._diag_failures == DIAGNOSTICS_WARN_AFTER_FAILURES or (
                self._diag_failures > DIAGNOSTICS_WARN_AFTER_FAILURES
                and self._diag_failures % 10 == 0
            ):
                log.warning("诊断包连续 %d 次没传出去（不影响心跳）：%s", self._diag_failures, exc)
            else:
                log.debug("诊断包没传出去（不影响心跳）：%s", exc)

    @contextmanager
    def _watchdog_paused(self):
        """临时**放宽**看门狗：升级那一段本来就该花很久。

        ``_handle_upgrade`` 里要下载（可能上百 MB）、验签、写回滚状态、切软链，
        最后还要交回给 systemd 重启 —— 拿"单轮 120 秒"去掐它是误伤，而且掐在
        切软链的中间会把机器留在最难查的状态里。所以这一段关掉闹钟，出去时按
        **完整的** ``watchdog_seconds`` 重新上（不是"剩下的零头"）。
        """
        active = self._watchdog_active
        if active:
            disarm_watchdog()
        try:
            yield
        finally:
            if active and self._watchdog_active:
                arm_watchdog(self.watchdog_seconds)

    def _emit(self, level: str, category: str, message: str, meta: Optional[dict] = None) -> None:
        if len(self._pending_events) >= MAX_PENDING_EVENTS:
            return
        self._pending_events.append(
            {
                "level": level,
                "category": category,
                "message": message[:2000],
                "meta": meta,
            }
        )

    def _pending_tick(self) -> Tuple[str, float]:
        """还没法干活的一轮：只心跳，不扫描。返回 ``(轮末状态, 等待秒数)``。

        覆盖两种状态 —— 未配对到人、以及配对好了但当前没有含本人的场次。
        轮末状态变成 ``STATE_READY`` 时，调用方接着在**同一次 cycle** 里走正常
        路径：教师刚配对完还要再等一个整周期才开始收代码，在开考前那几分钟很刺眼。

        扫描是**故意**跳过的：没有准考证号，``scan.roots`` 里的 ``{player_no}``
        展开不出来，扫出来的相对路径也没法归属到任何人。收上去只会污染台账，
        而服务端也会（正确地）把它丢掉 —— 与其传一堆没人要的数据，不如安静等着。
        """
        payload, _oversize, _errors = self._build_tick_payload([], {}, scan_complete=False)
        try:
            tick = self.client.tick(payload)
        except UnboundError as exc:
            # 403：凭据**有效**，但服务端说这台机器还没配对到人（可能是刚刚被
            # 解绑，也可能是配对还没生效）。这里最要紧的是**不要重新注册** ——
            # 那样每轮都会换一个新配对码，教师手上那个永远对不上。
            log.info("服务端认为这台机器还没有配对（%s），继续等待", exc.code or exc.status)
            # 兜底用的是服务端真正会发的那个码（``pairing_required``），
            # 而不是另编一个服务端从来不发的名字 —— 两边的码表要能对上，
            # 否则日志里出现的码在服务端代码里 grep 不到。
            self._mark_unbound(exc.code or "pairing_required")
            return STATE_UNCLAIMED, self._clamp_wait(None)

        remote_config = tick.get("config")
        if isinstance(remote_config, dict):
            self.policy = merge_policy(remote_config)

        self._flush_events()

        credential = self._adopt_identity(tick)
        wait = self._clamp_wait(tick.get("next_tick_seconds"))
        return credential.state, wait

    def _clamp_wait(self, seconds: object) -> float:
        """把服务端给的间隔收进 [5, 3600]。

        下界 5 秒是为了不让一个有 bug 的服务端把 Agent 变成压测工具；上界
        3600 秒是为了让"服务端其实已经好了但还在按老节奏睡"不至于拖一小时。
        """
        try:
            value = int(seconds)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = self.config.scan_interval
        return float(max(5, min(value, 3600)))

    def _flush_events(self) -> None:
        if not self._pending_events:
            return
        batch = self._pending_events[:]
        try:
            self.client.report_events(batch)
        except (AuthError, RateLimited):
            raise
        except AgentError as exc:
            log.debug("事件上报失败，继续积压：%s", exc)
            return
        # 成功后按数量弹出，防止上报期间又有新事件被丢掉
        del self._pending_events[: len(batch)]

    # ---------------------------------------------------------------- #
    # 一轮
    # ---------------------------------------------------------------- #

    def cycle(self) -> float:
        """执行一轮，返回下次执行前的等待秒数。"""
        credential = self._ensure_credential()
        if credential is None:
            # 还没注册：**等 root 的注册单元**，不退出、不联网、不刷日志。
            return self._wait_for_credential()
        self._registration_logged = False

        # 未配对 / 没有场次：这台机器没有准考证号，算不出扫描目录，扫出来的东西
        # 也没法归属到任何人。它这一轮唯一该做的就是心跳一下、顺便问一句状态变了没有。
        if credential.state != STATE_READY:
            state, wait = self._pending_tick()
            if state != STATE_READY:
                return wait
            # 刚能干活了 —— **同一次 cycle 就接着往下走**
            credential = self._credential or credential

        # 从磁盘上的凭据直接起来（没走注册）时，也要在这里把桌面上的提示文件
        # 清一次 —— 上一轮留下的「配对码.txt」不清掉，下一个人看见会以为
        # 这台机器还没配对。
        self._sync_notice_files(credential)

        # 扫描目录里可能含 {player_no} / {contest_slug}，必须等拿到凭据之后才能确定
        self._ensure_roots(credential.player_no, credential.contest_slug)

        results, self._local_paths, skipped_roots = self._scan()
        payload, oversize, errors = self._build_tick_payload(
            results,
            self._local_paths,
            # 有根没扫成（不存在/不是目录）时**不能说"扫完了"**：服务端拿
            # scan_complete 决定要不要做删除判定，而"这个根现在没有"与"这些文件
            # 被删了"在报文里长得一模一样。缺失的根另有 stats.scan_missing 明说，
            # 所以这里保守一点只是少一次删除判定，不会丢信息。
            scan_complete=(skipped_roots == 0),
        )

        if oversize:
            self._emit(
                "warning", "oversize_skipped",
                "有 %d 个文件超过大小上限，未回收" % len(oversize),
                {"paths": oversize[:20]},
            )
        if errors:
            self._emit(
                "warning", "scan_error",
                "扫描过程中出现 %d 个错误" % len(errors),
                {"errors": errors[:10]},
            )

        # 未完成下载的偏移量已在 _build_tick_payload 里从磁盘实况收集，
        # 不依赖上一轮的作业列表 —— Agent 重启后照样能续传。
        try:
            tick = self.client.tick(payload)
        except UnboundError as exc:
            # 中途被解绑了（比如教师在场次里把这个人删了）。这不是故障，
            # 是状态变化 —— 所以不进指数退避，退回"安静等待"那条路。
            log.warning("tick 被拒（%s），退回等待配对", exc.code or exc.status)
            self._mark_unbound(exc.code or "pairing_required")
            return self._clamp_wait(None)

        # tick 成功 = 本轮携带的完成项已被服务端接收，可以清空。
        # 必须在 tick 之后、下载之前清 —— 下载新产生的完成项属于下一轮。
        del self._completed_assets[:]

        # 诊断包需要的几个计数：tick 成功一轮、服务端给的下一轮间隔、最近一次错误
        self._diag_tick_count += 1
        self._diag_last_error = self._last_error
        self._diag_next_tick = _as_int_or_none(tick.get("next_tick_seconds"))
        if tick.get("diagnostics_request"):
            # 管理端按了"抓一份现场"：下一轮循环里就发（受 60 秒最小间隔约束，
            # 到点了立刻发 —— 请求会一直留着，不会被丢掉）
            log.info("服务端要求回传一份诊断包（manual）")
            self.request_diagnostics("manual")

        remote_state = state_from_payload(tick)
        if remote_state != STATE_READY:
            # 服务端说我们已经不能干活了（被解绑 / 场次里没有这个人 / 场次结束了）。
            # 同步状态并退回等待路径 —— 继续扫下去只会每轮都失败。
            log.warning("服务端报告状态为 %s，退回等待", remote_state)
            self._adopt_identity(tick)
            return self._clamp_wait(tick.get("next_tick_seconds"))

        remote_config = tick.get("config")
        if isinstance(remote_config, dict):
            self.policy = merge_policy(remote_config)

        # 管理端授权的一次性卸载：本地验签 + 执行成功就直接退出（exit 0，
        # 单元是 Restart=on-failure 所以不会被再拉起来）。放在下载/上传之前 ——
        # 都要卸载了，没必要再搬文件。令牌只活在本地变量里，不进 argv/日志/上报。
        token = tick.get("uninstall_token")
        if isinstance(token, str) and token.strip():
            if self._handle_uninstall_token(token.strip()):
                return 0.0

        jobs = tick.get("deploy_jobs") or []
        need_upload = tick.get("need_upload") or []

        if jobs:
            self._download(jobs)
        if need_upload:
            self._upload(list(need_upload))

        upgrade = tick.get("upgrade")
        if upgrade:
            # 升级这一段关掉看门狗（理由见 _watchdog_paused）
            with self._watchdog_paused():
                self._handle_upgrade(upgrade)

        self._flush_events()
        self.cache.save(self.config.hash_cache_path)

        # 跑够几个周期说明新版本确实能用，清掉回滚状态。
        # 连续计数而不是"启动就算成功"：起得来但一 tick 就崩的版本同样要回滚。
        self._healthy_cycles += 1
        if self._healthy_cycles == HEALTHY_CYCLES_BEFORE_TRUST:
            try:
                mark_healthy(self.config.install_root)
            except OSError as exc:  # pragma: no cover
                log.debug("清除回滚状态失败: %s", exc)

        next_tick = int(tick.get("next_tick_seconds") or self.config.scan_interval)
        return float(max(5, min(next_tick, 3600)))

    def _wait_for_credential(self) -> float:
        """还没注册：等 root 的注册单元把凭据放进来。**这是正常状态，不是错误。**

        **设计如此**：统一注册密钥是 root 只读的，注册由 ``syncoj-agent-enroll.service``
        （root 身份跑一次）完成；Agent 以选手身份运行，读不到密钥，也不该自己注册。
        真机事故就是这里搞反了：服务去撞密钥 → PermissionError → 抛 ConfigError →
        exit 1 → 被 systemd 反复重启，5 次后撞上 StartLimit 罢手，而**注册单元从来
        没被跑过**，于是机器永远不出现。

        所以这里：不退出、不联网、不刷日志（只第一次说清下一步），每
        :data:`REGISTRATION_POLL_SECONDS` 秒看一眼凭据是否出现 —— 出现了下一轮
        就照常同步。
        """
        if not self._registration_logged:
            self._registration_logged = True
            log.warning(
                "这台机器还没有注册凭据，等待 %s 完成注册（每 %.0f 秒检查一次）。\n"
                "  **这是设计如此**：统一注册密钥是 root 只读的，注册由 root 的 %s\n"
                "  完成一次；Agent 以选手身份运行，读不到密钥，也不该自己注册。\n"
                "  若一直等不到：`systemctl status %s` 看注册结果，必要时\n"
                "  `systemctl reset-failed %s && systemctl start %s` 手工再来一次。",
                ENROLL_UNIT_NAME,
                REGISTRATION_POLL_SECONDS,
                ENROLL_UNIT_NAME,
                ENROLL_UNIT_NAME,
                ENROLL_UNIT_NAME,
                ENROLL_UNIT_NAME,
            )
        return REGISTRATION_POLL_SECONDS

    # ---------------------------------------------------------------- #
    # 常驻
    # ---------------------------------------------------------------- #

    def _check_desktop_consistency(self) -> None:
        """核一遍"配置里的下发落点"与 XDG 声明的桌面是不是同一个目录（只说一次）。

        现场第二种"看不到文件"的原因：落点没错、文件也在，只是**不在 GNOME 显示的
        那个桌面上**（桌面被挪到别处了，或者配置写的是 ``{home}/Desktop`` 而实际是
        「桌面」）。:func:`detect_desktop` 已经优先读 ``XDG_DESKTOP_DIR``，但
        ``agent.ini`` 里的 ``deploy_root`` 是教师/安装器写死的值，两者可能不一致。

        这里**只警告、不改配置**：那份文件是教师的，Agent 悄悄改它会让"配置文件里
        写的是什么"失去意义。警告里要给出"去哪儿看"，否则教师只会以为下发坏了。
        """
        if self._desktop_checked:
            return
        self._desktop_checked = True
        try:
            declared = declared_xdg_desktop()
        except Exception:  # pragma: no cover - 解析不该抛，真抛了也不值当影响启动
            return
        if declared is None:
            return
        configured = Path(self.config.deploy_root)
        if configured == declared:
            return
        log.warning(
            "下发落点（%s）与系统声明的桌面（%s，来自 ~/.config/user-dirs.dirs）"
            "不一致 —— 文件可能不会出现在桌面上，去 %s 看。"
            "要改落点请改 agent.ini 的 deploy_root 后重启服务。",
            configured,
            declared,
            configured,
        )

    def run_forever(self) -> int:
        log.info(
            "SyncOJ Agent %s 启动（machine_id=%s，自更新 %s）",
            __version__,
            self.machine_id,
            self.config.upgrade_mode,
        )

        # 启动守卫要在做任何网络操作之前跑：如果上一版本留下的状态表明新版本
        # 反复起不来，应当立刻回滚退出，而不是先联网折腾一圈
        try:
            if self._guard_boot():
                self.client.close()
                return 0
        except Exception:
            log.exception("启动守卫执行失败，继续正常运行")

        sweep_stale_parts(self.config.deploy_root)
        # 桌面刷新问题的第二个成因（落点不在真正的桌面上）在这里核一遍 ——
        # 启动时说一次就够，不占每轮的报文，也不改任何配置
        self._check_desktop_consistency()

        if not watchdog_supported():
            log.debug("这个平台没有 SIGALRM/setitimer，整轮看门狗不可用（不影响功能）")

        while not self._stop:
            if self._restart_requested:
                log.info("为应用新版本而退出，systemd 将以新版本重新拉起")
                break
            # **整轮看门狗**：每轮开始上闹钟、结束撤掉（finally）。任何一次阻塞
            # 调用都不能让心跳停摆超过 watchdog_seconds —— 现场那次卡在一次 socket
            # 读上 34 分钟，就是没有被兜住。
            self._watchdog_active = arm_watchdog(self.watchdog_seconds)
            round_ok = False
            try:
                delay = self.cycle()
                self._backoff = 0.0
                round_ok = True
            except RoundTimeout as exc:
                # 看门狗掐掉这一轮：**不退出进程**，把话说清楚后照常进入下一轮。
                # 下一次心跳用短一点的间隔，别让教师等满一个周期才知道出过事。
                log.warning(
                    "这一轮超过 %.0f 秒没有结束（%s），已中止；下次心跳继续",
                    self.watchdog_seconds,
                    exc,
                )
                delay = max(5.0, min(float(self.config.scan_interval), 3600.0))
            except AuthError as exc:
                # 只有这一种错误该清掉凭据重新注册（401）。403 走的是
                # UnboundError，它在上面的循环里就被消化掉了 —— 见 client.py 的注释。
                self._forget_credential(str(exc))
                delay = 30.0
            except BootstrapKeyError as exc:
                # 密钥不对是**部署问题**，重试解决不了。这里刻意不进指数退避循环
                # 快速刷屏，而是用最长间隔慢慢重试 —— 万一运维正好在换密钥，
                # 一个长周期后自己就好了，不需要重启 50 台机器。
                log.error("统一密钥不可用，请更换密钥后重启：%s", exc)
                self._emit("error", "bootstrap_key_rejected", str(exc))
                delay = MAX_BACKOFF
            except RateLimited as exc:
                # 退避节奏由服务端决定：它才知道限速窗口有多长。
                # 按自己的节奏硬撞只会把窗口一直顶开。
                # 上界仍由本地的 MAX_BACKOFF 兜住 —— 服务端写错一个数量级
                # （比如 86400）不该让整间机房停摆一整天。
                if exc.retry_after > 0:
                    delay = min(float(exc.retry_after), MAX_BACKOFF)
                else:
                    delay = MAX_BACKOFF
                log.warning("被服务端限速，%.0f 秒后重试：%s", delay, exc)
            except ConfigError:
                raise
            except NetworkError as exc:
                self._backoff = min(MAX_BACKOFF, max(5.0, self._backoff * 2 or 5.0))
                log.warning("网络不可用，%.0f 秒后重试：%s", self._backoff, exc)
                delay = self._backoff
                self._network_failures += 1
                if self._network_failures >= REDISCOVER_AFTER_FAILURES:
                    # 计数在**尝试之后**清零：发现失败也要再等 N 轮才重试，
                    # 否则退避就白做了（每轮都往局域网里广播一次）。
                    self._network_failures = 0
                    self._try_rediscover()
            except AgentError as exc:
                self._backoff = min(MAX_BACKOFF, max(10.0, self._backoff * 2 or 10.0))
                log.warning("本轮失败，%.0f 秒后重试：%s", self._backoff, exc)
                delay = self._backoff
            except Exception:
                self._backoff = min(MAX_BACKOFF, max(10.0, self._backoff * 2 or 10.0))
                log.exception("未预期的错误，%.0f 秒后重试", self._backoff)
                delay = self._backoff
            finally:
                # 必须撤掉：闹钟漂到下一轮里，就会在"下一轮明明很正常"的时候
                # 把它掐掉，而且报出来的是一条看不懂的超时。
                if self._watchdog_active:
                    disarm_watchdog()
                    self._watchdog_active = False
                # 连续失败计数（诊断包的 tick.consecutive_failures，也是"出错补
                # 一份现场"的判据）。成功一轮就清零。
                self._consecutive_failures = 0 if round_ok else self._consecutive_failures + 1

            # 连续失败攒到阈值 → 攒一份"出错包"（不是立刻发：60 秒最小间隔由
            # `_maybe_send_diagnostics` 守）。这里只记"该发"，发不发得出去不影响
            # 下面这一轮的等待。
            if self._consecutive_failures >= DIAGNOSTICS_ERROR_CONSECUTIVE_FAILURES:
                self.request_diagnostics("error")

            # 诊断回传是**旁路**：该发就发一份，失败只记 DEBUG。
            # 放在 `_sleep` 之前：它成功与否都不影响这一轮的节奏。
            self._maybe_send_diagnostics()

            self._sleep(delay)

        log.info("Agent 已停止")
        self.client.close()
        return 0

    def _try_rediscover(self) -> None:
        """连不上服务端时，去局域网里问一次：它是不是换地址了？

        **只在同一个地址连续失败若干轮之后**才做这一件事。理由是代价不对称：
        服务端重启一下、网线抖一下、Wi-Fi 重连都会连不上，而那些情况下地址并没有
        变 —— 每轮都广播一次会打扰整个局域网，而它换地址是很少见的事。

        问到的新地址**只在这个进程内生效**，不写回 ``agent.ini``：那是教师的文件，
        Agent 悄悄改它会让"配置文件里写的是什么"失去意义。代价是重启之后再发现
        一次，这可以接受（几秒钟，而且是机器自己的事）。

        **认的是签名，不是"谁先回答"**：见 ``syncoj_agent/discovery.py``。
        """
        if self._public_key is None:
            log.info("连不上服务端，但机器上没有发布公钥 —— 无法验证应答来源，不做发现")
            return

        outcome = discovery.discover(
            self._public_key,
            machine_id=discovery.machine_id_hint(),
        )
        if not outcome.url:
            log.warning("局域网发现没结果：%s", outcome.explain())
            return

        current = (self.config.server_url or "").rstrip("/")
        if outcome.url == current:
            # 最常见的结局：地址没变，是网络或服务端本身的问题。这条日志能省掉
            # 一次"是不是地址配错了"的怀疑。
            log.warning("局域网发现的地址与配置一致（%s）：问题不在地址上", outcome.url)
            return

        log.warning(
            "服务端换地址了：%s → %s（本次运行内生效；agent.ini 里仍是旧值）",
            current or "（空）",
            outcome.url,
        )
        self._emit(
            "warning",
            "server_address_changed",
            "服务端地址变了，已自动改用 %s（原 %s）" % (outcome.url, current or "（空）"),
            {"from": current, "to": outcome.url},
        )
        try:
            self.client.repoint(outcome.url)
        except AgentError as exc:
            log.error("改用新地址失败，继续用原地址：%s", exc)
            return
        self.config.server_url = outcome.url
        # 地址换了，之前那份凭据对应的服务端可能不是同一台 —— 不清凭据，
        # 但要让下一轮重新走一遍身份解析（服务端会自己判断这台机器是谁）。
        self._roots_ready = False
        self._backoff = 0.0

    def _sleep(self, seconds: float) -> None:
        """可被停止信号打断的休眠。

        用切片轮询而不是一次 ``time.sleep(60)``：否则 systemd 发来 SIGTERM 后
        最多要等一整轮才退出，systemctl restart 会明显卡顿。
        """
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _disk_free(path: Path) -> Optional[int]:
    try:
        usage = os.statvfs(str(path))
        return int(usage.f_bavail * usage.f_frsize)
    except (AttributeError, OSError):
        pass
    try:
        import shutil

        return int(shutil.disk_usage(str(path)).free)
    except (OSError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #


def setup_logging(config: AgentConfig, verbose: bool = False) -> None:
    """配置日志。

    默认写**文件**而不是 stdout：systemd 单元里 ``StandardOutput=null``，
    任何写 stdout 的行为都是无意义的噪音。同时保留 ``to_stderr`` 选项，
    方便前台调试。
    """
    level = logging.DEBUG if verbose else getattr(logging, config.log_level, logging.INFO)
    root = logging.getLogger("syncoj")
    root.setLevel(level)
    root.handlers = []

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    log_file = config.resolved_log_file
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError as exc:
        # 日志文件写不了不能导致 Agent 起不来
        print("警告：无法写入日志文件 %s: %s" % (log_file, exc), file=sys.stderr)

    if config.log_to_stderr or verbose:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if not root.handlers:
        root.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncoj-agent",
        description="SyncOJ 选手端 Agent（无界面，静默运行）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（INI）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志到 stderr")
    parser.add_argument(
        "--check", action="store_true", help="只校验配置，不联网、不运行"
    )
    parser.add_argument(
        "--once", action="store_true", help="只跑一轮就退出（用于联调与排障）"
    )
    parser.add_argument(
        "--dump-config", action="store_true", help="打印解析后的配置（JSON）后退出"
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help="只注册、拿到凭据就退出（供装机时的 root 一次性单元调用）",
    )
    parser.add_argument(
        "--chown-to",
        default=None,
        help="配合 --provision：把写出来的凭据/身份文件交给这个用户（root 运行时装机用）",
    )
    parser.add_argument(
        "--pair-code", action="store_true", help="打印本机的配对短码（未配对时才有）"
    )
    return parser


def _provision_hint(config: Optional[AgentConfig] = None) -> str:
    """``--provision`` 失败时统一给的"下一步"。

    现场最贵的成本不是失败本身，而是**失败了却一个字都没有**（退出 2、journal
    空白，只能靠猜）。所以这条提示宁可啰嗦：说清去看哪个文件、怎么手工重试。
    """
    key = "/etc/syncoj/bootstrap.key"
    if config is not None and config.bootstrap_key_file is not None:
        key = str(config.bootstrap_key_file)
    return (
        "下一步：检查 agent.ini 的 run_user 与 server.url，确认 %s 存在且 root 可读；"
        "改完手工重试：systemctl reset-failed %s; systemctl start %s"
        % (key, ENROLL_UNIT_NAME, ENROLL_UNIT_NAME)
    )


def _print_provision_hint(config: Optional[AgentConfig] = None) -> None:
    print(_provision_hint(config), file=sys.stderr)


def _run_provision(args, config: AgentConfig, agent: "Agent") -> int:
    """只注册、拿凭据就退出。供装机时的 root 一次性单元调用。

    为什么要单独一个模式：统一密钥是 **root 只读** 的，而 Agent 服务以选手身份
    运行、根本读不到它。所以注册这件事必须由 root 做一次，做完把凭据交给选手
    账号，之后 Agent 只读凭据。

    ``--chown-to`` 只在以 root 运行时有意义：写出来的文件属主是 root，
    而读它们的是选手 —— 不交出去的话，服务起来了但读不到凭据，
    表现是"一直重新注册"，很难联想到是属主问题。这两件事我们已经在安装器的
    配置文件上踩过一次。

    这条路上**任何**失败都带 "下一步"（见 :func:`_provision_hint`）：
    它以 root 一次性单元的身份在 journal 里留痕，是排障时唯一能看到的线索。
    """
    try:
        agent.ensure_credential(allow_enroll=True)
    except RateLimited as exc:
        # 限速不是失败 —— 凭据没拿到，但再等一会儿就行。装机时同时开机的机器
        # 可能正好把注册限速顶满，报"失败"会让教师以为装机装坏了。
        # 报出来的等待时间与 run_forever 用**同一个上界**（MAX_BACKOFF）：
        # 服务端写错一个数量级（比如 86400）不该让运维按它的数字去等。
        wait = min(float(exc.retry_after), MAX_BACKOFF) if exc.retry_after > 0 else MAX_BACKOFF
        print(
            "注册被限速（%.0f 秒后可重试）：%s" % (wait, exc),
            file=sys.stderr,
        )
        _print_provision_hint(config)
        return 1
    except (AgentError, ConfigError) as exc:
        log.error("注册失败：%s", exc)
        print("注册失败：%s" % exc, file=sys.stderr)
        _print_provision_hint(config)
        return 1
    finally:
        agent.client.close()

    if args.chown_to:
        _chown_state_files(config, args.chown_to)

    credential = load_credential(config.credential_path)
    if credential is None:
        print("注册似乎成功了，但没有读回凭据 —— 请检查状态目录权限。", file=sys.stderr)
        _print_provision_hint(config)
        return 1
    if credential.state == STATE_UNCLAIMED:
        print("已注册，等待配对。配对码：%s" % credential.pair_code)
    elif credential.state == STATE_WAITING:
        # 这是**正常的中间状态**，不是失败：配对好了，但这个场次里还没有这个人。
        # 报成"失败"会让教师去重装 Agent，而真正该做的是把名单应用到场次里。
        print(
            "已配对给 %s，但当前场次里没有这个人 —— 请把名单应用到场次里。"
            % (credential.player_no or "?")
        )
    else:
        print("已注册：选手 %s @ %s" % (credential.player_no, credential.contest_slug))
        # 注册完就把配对码文件清掉 —— 它可能来自上一次未配对的尝试
        agent.clear_pair_code_file()
    return 0


def _chown_state_files(config: AgentConfig, user: str) -> None:
    """把状态目录里 Agent 要读写的文件交给运行用户。"""
    import shutil

    targets = [
        config.state_dir,
        config.credential_path,
        config.state_dir / "machine_uuid",
        config.state_dir / "machine_id",
        config.hash_cache_path,
    ]
    for target in targets:
        if not target.exists():
            continue
        try:
            shutil.chown(str(target), user=user)
        except (OSError, LookupError) as exc:
            log.warning("无法把 %s 的属主改为 %s：%s", target, user, exc)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = AgentConfig.load(
            Path(args.config) if args.config else None,
            # 注册以 **root** 跑，而 {home}/{desktop} 得按"要交给的那个账号"展开
            # —— 否则会展开成 /root/桌面/...，那台机器上不存在。没给
            # --chown-to 时退回 agent.ini 里记的 run_user（见 AgentConfig.load）。
            expand_for_user=args.chown_to,
        )
    except ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        if args.provision:
            _print_provision_hint()
        return 2

    setup_logging(config, verbose=args.verbose)

    try:
        config.validate(for_provision=args.provision)
    except ConfigError as exc:
        log.error("%s", exc)
        # **无条件**打到 stderr，而且**只打一遍**（异常里已经带了"配置有误"，
        # 再包一层前缀会变成"配置校验失败：配置有误：……"）。
        #
        # 为什么不加条件：日志文件以运行账号的身份写，而注册单元以 root 跑
        # —— 不出现在 stderr 就等于 journal 里一个字都没有。现场就是
        # "退出码 2、systemctl status 只有 systemd 自己那几行"，比错误本身贵得多。
        print(str(exc), file=sys.stderr)
        if args.provision:
            _print_provision_hint(config)
        return 2

    if args.dump_config:
        print(json.dumps(
            {
                "server_url": config.base_url,
                "verify_tls": config.verify_tls,
                "state_dir": str(config.state_dir),
                "deploy_root": str(config.deploy_root),
                "scan_roots": [str(p) for p in config.scan_roots],
                "max_file_size": config.max_file_size,
                "log_file": str(config.resolved_log_file),
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    if args.check:
        print("配置校验通过")
        return 0

    try:
        agent = Agent(config)
    except ConfigError as exc:
        log.error("%s", exc)
        print("启动失败：%s" % exc, file=sys.stderr)
        if args.provision:
            _print_provision_hint(config)
        return 2

    if args.pair_code:
        credential = load_credential(config.credential_path)
        if credential is None:
            # 名字从常量来：这一行是印给现场照着敲的，手打错了就是 "Unit not found"
            print("还没有凭据 —— 先让 %s 注册一次。" % ENROLL_UNIT_NAME)
            return 1
        if credential.state != STATE_UNCLAIMED:
            print("这台机器已经配对过了（或不需要配对）。")
            return 1
        if not credential.pair_code:
            # 未配对但手上没有码：服务端下一轮 tick 会再发一个过来。
            # 这里如实说出来，比打印一个空行强。
            print("还没有拿到配对码 —— 等下一轮 tick（服务端会带过来）。")
            return 1
        print(credential.pair_code)
        return 0

    if args.provision:
        return _run_provision(args, config, agent)

    if args.once:
        try:
            delay = agent.cycle()
        except (AgentError, ConfigError) as exc:
            log.error("单轮执行失败：%s", exc)
            return 1
        finally:
            agent.client.close()
        print("单轮执行完成，服务端建议下次间隔 %.0f 秒" % delay)
        return 0

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, agent.request_stop)
        except (ValueError, OSError):  # pragma: no cover - 非主线程
            pass

    return agent.run_forever()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
