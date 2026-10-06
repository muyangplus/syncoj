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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
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


def build_tick_payload(
    machine_id: str,
    agent_version: str,
    scan_root: str,
    results,
    partials: List[dict],
    stats: Dict[str, object],
    completed_assets: Optional[List[int]] = None,
    scan_complete: bool = True,
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
        "stats": final_stats,
    }
    return payload, oversize, errors


class Agent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)

        self.machine_id = resolve_machine_id(config.machine_id, config.state_dir)
        #: 本机身份：首次运行时生成并持久化。比 /etc/machine-id 更适合当身份 ——
        #: 克隆镜像没做通用化时 machine-id 是整批相同的
        self.machine_uuid = resolve_machine_uuid(config.state_dir)
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

        # 扫描根不存在时**创建它**，而不是报错。
        # 它是选手的工作目录，此刻选手可能还没建 —— 提前建好反而省事；
        # 报错则会在开考前刷一屏"目录不存在"，掩盖真正的问题。
        for _name, root in named:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("无法创建扫描目录 %s: %s", root, exc)

        return named

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

    def _ensure_credential(self) -> Credential:
        return self.ensure_credential()

    def ensure_credential(self) -> Credential:
        """确保手上有一份可用凭据；没有就注册。

        公开名字（无下划线）是因为 ``--provision`` 也要用它 —— 装机时的 root
        一次性单元与常驻循环**必须走同一条注册路径**，否则"装机时能注册、
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

        return self._enroll()

    def _enroll(self) -> Credential:
        """注册。**只有一条路**：读镜像里那份 root 只读的统一密钥。

        没有「每选手注册码」那条路了 —— 它已经被"机器永久绑定名单条目"取代
        （见 docs/protocol.md §1）。密钥缺失或读不出来时把下一步动作说清楚 ——
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
                "  装好之后重启 syncoj-enroll.service（它以 root 身份跑一次注册）。"
                % (key_path, key_path)
            )
        if not bootstrap_key:
            raise ConfigError(
                "统一密钥文件 %s 存在但读不出来。\n"
                "Agent 以选手身份运行，读不到 root 只读的文件 —— 这是**设计如此**：\n"
                "密钥能注册整间机房，不该躺在学生读得到的地方。\n"
                "请交给装机时的 syncoj-enroll.service 去注册。"
                % key_path
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
        try:
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

        **每一轮都重写**，而不是只在状态变化时写一次：教师可能刚开机才来看，
        而文件只在内存里有一份时，重启一次就看不见了。无关的文件也要清掉 ——
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
        """凭据失效时清空本地凭据，下一轮会走重新注册。"""
        log.warning("凭据失效（%s），将尝试重新注册", reason)
        self._credential = None
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
        """扫描全部根目录，返回 ``(扫描结果列表, 本地路径索引)``。"""
        scan_policy = ScanPolicy.from_mapping(self.policy)
        max_files = int(self.policy.get("max_files", 5000))  # type: ignore[arg-type]
        results = []
        local_paths: Dict[str, Path] = {}

        for name, root in self._roots:
            outcome = scan_directory(root, scan_policy, self.cache, max_files=max_files)
            results.append((name, outcome))
            for entry in outcome.entries:
                local_paths[join_report_path(name, entry.path)] = root / entry.path

        return results, local_paths

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
            },
            # 上一轮下载完成的结果在这里回报。差一轮无所谓 —— 而且服务端在收到
            # 回报前会继续下发该作业，Agent 会走 "内容已一致" 的跳过分支并再次
            # 上报，这恰好让"回报丢失"能自愈。
            completed_assets=self._completed_assets,
            scan_complete=scan_complete,
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

        # 版本单调：拒绝"升级"到不高于当前的版本，否则攻击者可以重放一个
        # 历史版本的真实签名包，把 Agent 退回已知有漏洞的状态
        try:
            if parse_version(manifest.version) <= parse_version(running):
                log.debug("忽略不高于当前版本的发布 %s（当前 %s）", manifest.version, running)
                return
        except UpgradeError as exc:
            log.warning("版本号无法比较，拒绝升级: %s", exc)
            return

        bundle = self.config.state_dir / "upgrade" / ("%s.tar.gz" % manifest.version)
        try:
            size = self.client.download_to_file(manifest.url, bundle, MAX_BUNDLE_BYTES)
            log.info("已下载发布包 %s（%d 字节），开始校验", manifest.version, size)
            verify_bundle(public_key, bundle, manifest)
            stage_release(install_root, manifest.version, bundle)
        except (UpgradeError, AgentError) as exc:
            log.error("升级包处理失败: %s", exc)
            self._emit("warning", "upgrade_failed", "升级 %s 失败：%s" % (version, exc))
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
            return

        log.warning("已激活版本 %s（原 %s），即将重启以生效", manifest.version, previous)
        self._emit(
            "warning", "upgrade_activated",
            "已升级到 %s 并重启（原版本 %s）" % (manifest.version, previous),
        )
        # 由 systemd 的 Restart=always 拉起新版本 —— 不需要我们自己调 systemctl，
        # 那会要求额外的 polkit 授权，平白扩大 Agent 的权限面
        self._restart_requested = True

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

        results, self._local_paths = self._scan()
        payload, oversize, errors = self._build_tick_payload(results, self._local_paths)

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

        jobs = tick.get("deploy_jobs") or []
        need_upload = tick.get("need_upload") or []

        if jobs:
            self._download(jobs)
        if need_upload:
            self._upload(list(need_upload))

        upgrade = tick.get("upgrade")
        if upgrade:
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

    # ---------------------------------------------------------------- #
    # 常驻
    # ---------------------------------------------------------------- #

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

        while not self._stop:
            if self._restart_requested:
                log.info("为应用新版本而退出，systemd 将以新版本重新拉起")
                break
            try:
                delay = self.cycle()
                self._backoff = 0.0
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


def _run_provision(args, config: AgentConfig, agent: "Agent") -> int:
    """只注册、拿凭据就退出。供装机时的 root 一次性单元调用。

    为什么要单独一个模式：统一密钥是 **root 只读** 的，而 Agent 服务以选手身份
    运行、根本读不到它。所以注册这件事必须由 root 做一次，做完把凭据交给选手
    账号，之后 Agent 只读凭据。

    ``--chown-to`` 只在以 root 运行时有意义：写出来的文件属主是 root，
    而读它们的是选手 —— 不交出去的话，服务起来了但读不到凭据，
    表现是"一直重新注册"，很难联想到是属主问题。这两件事我们已经在安装器的
    配置文件上踩过一次。
    """
    try:
        agent.ensure_credential()
    except RateLimited as exc:
        # 限速不是失败 —— 凭据没拿到，但再等一会儿就行。装机时同时开机的机器
        # 可能正好把注册限速顶满，报"失败"会让教师以为装机装坏了。
        print(
            "注册被限速（%.0f 秒后可重试）：%s" % (exc.retry_after or MAX_BACKOFF, exc),
            file=sys.stderr,
        )
        return 1
    except (AgentError, ConfigError) as exc:
        log.error("注册失败：%s", exc)
        print("注册失败：%s" % exc, file=sys.stderr)
        return 1
    finally:
        agent.client.close()

    if args.chown_to:
        _chown_state_files(config, args.chown_to)

    credential = load_credential(config.credential_path)
    if credential is None:
        print("注册似乎成功了，但没有读回凭据 —— 请检查状态目录权限。", file=sys.stderr)
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
        config = AgentConfig.load(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return 2

    setup_logging(config, verbose=args.verbose)

    try:
        config.validate()
    except ConfigError as exc:
        log.error("%s", exc)
        if args.check or args.verbose:
            print("配置校验失败：%s" % exc, file=sys.stderr)
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
        return 2

    if args.pair_code:
        credential = load_credential(config.credential_path)
        if credential is None:
            print("还没有凭据 —— 先让 syncoj-enroll.service 注册一次。")
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
