"""Agent 面 API：注册、tick、上传、下载、事件上报。

这是系统唯一的常态化流量入口。设计要点：

* ``/tick`` 是**幂等**的。重复调用、补发、乱序都不会破坏状态 —— 它比对的是
  "目录里现在有什么" 这个状态，而不是 "刚刚发生了什么" 这个事件序列。
* 除 ``/enroll`` 外全部要求 Bearer 凭据，且凭据只能访问**本选手本人**的资源。
* 所有来自客户端的路径都经 ``validate_relpath`` 校验，落盘前再经 ``safe_join``
  做符号链接检查。服务端不信任 Agent 的任何输入。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..context import AppContext
from ..errors import ApiError
from ..models import (
    Agent,
    AgentRelease,
    Asset,
    Contest,
    DeployStatus,
    DeployTarget,
    DeployTask,
    EventLog,
    Player,
    SourceFile,
    iso_utc,
    local_clock,
    unix_seconds,
    utcnow,
)
from ..paths import PathValidationError, safe_join, validate_relpath
from ..policy import build_policy
from ..schemas import (
    InstallLedgerOut,
    AgentEvent,
    EnrollRequest,
    EnrollResponse,
    SimpleAck,
    TickRequest,
    TickResponse,
    UpgradeInfo,
    UploadResult,
)
from ..security import hash_token, new_pair_code, new_token
from ..storage import BlobTooLarge, HashMismatch, materialize
from ..services import enrollment, install_policy, packaging, problem_rules, runtime_settings, scan_missing, uninstall
from ..services.collect import reconcile_scan, record_events
from ..services.deploy import collect_deploy_jobs
from ..services.ratelimit import RateLimitExceeded
from .deps import (
    AgentIdentity,
    MachineIdentity,
    Principal,
    get_ctx,
    require_agent,
    require_principal,
    resolve_machine,
)

__all__ = ["router"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_RANGE_CHUNK = 256 * 1024

#: 响应中最多回传多少条待上传路径，避免超大目录一次性撑爆响应体
MAX_NEED_UPLOAD = 500


def _reject_outside_window(ctx: AppContext, identity: AgentIdentity, now) -> None:
    """场次时间窗之外不收代码：还没开考、或者已经结束。

    **只挡上传这一个端点。** tick、下载 assets、事件上报都不走这里，因为到点之后
    机器还要活着：它还得拿题面、还得让教师看见它在线、还得把现场情况报上来。
    把门禁加到那些地方，效果是"到点那一刻整间机房失联"—— 而真正该停的只是收卷。

    **判据是服务端时间**（``utcnow()``）：考场机器的钟是最不可信的东西之一，
    让客户端报时间等于让选手自己决定还能不能交。到点那一刻（``now == ends_at``）
    按"已结束"处理，窗口是左闭右开的 ``[starts_at, ends_at)``。

    ``409`` 而不是 ``403``：凭据没问题、机器也没问题 —— 挡下来的是**这场现在的
    状态**不允许收文件，而不是"你是谁"或"你能不能碰这个资源"。同一个端点上的
    ``stale_upload`` 也是 409，403 在这个端点的语义已经被"该资源未下发给本选手"
    占着；两者混在一起，Agent 就分不清该重试还是该换人。附带的结构化字段让
    Agent 不必去解析上面那句中文（中文是给人看的）。
    """
    with ctx.db.session() as session:
        contest = session.get(Contest, identity.contest_id)
        if contest is None:  # pragma: no cover - 外键约束下不会发生
            return
        starts_at = contest.starts_at
        ends_at = contest.ends_at

        if ends_at is not None and now >= ends_at:
            raise ApiError(
                409,
                "contest_ended",
                "本场考试已于 %s 结束，代码不再接收" % local_clock(ends_at),
                {
                    "reason": "ended",
                    "starts_at": iso_utc(starts_at),
                    "ends_at": iso_utc(ends_at),
                },
            )
        if starts_at is not None and now < starts_at:
            raise ApiError(
                409,
                "contest_not_started",
                "本场考试还没开始（%s 开考），代码暂时不接收" % local_clock(starts_at),
                {
                    "reason": "not_started",
                    "starts_at": iso_utc(starts_at),
                    "ends_at": iso_utc(ends_at),
                },
            )


def _agent_config(ctx: AppContext) -> Dict[str, Any]:
    """下发给 Agent 的运行时策略。服务端是权威，客户端不得自行决定。

    ``scan_interval`` 与 ``tick_idle_seconds`` 同值：扫描发生在心跳里，两者在
    Agent 那边本来就是一回事（用户明确要求保持绑定）。心跳节奏是**运行参数**，
    改完下一轮心跳就生效，所以这里每次都现读。
    """
    settings = _runtime_settings(ctx)
    cfg = build_policy(
        max_file_size=ctx.settings.max_file_size,
        scan_interval=settings.tick_idle_seconds,
        max_files=ctx.settings.max_files_per_scan,
    )
    cfg["tick_idle_seconds"] = settings.tick_idle_seconds
    cfg["tick_active_seconds"] = settings.tick_active_seconds
    return cfg


def _runtime_settings(ctx: AppContext) -> runtime_settings.RuntimeSettings:
    """现读运行参数。

    心跳端点每轮都要问一次（而不是启动时缓存一份）：改完**一轮心跳内**生效、
    不用重启，是这三个参数存在的全部意义。读的是一次主键查询 + 一次会话，
    相对于这一轮里已经做的那些写操作可以忽略。
    """
    with ctx.db.session() as session:
        return runtime_settings.load(session)


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


def rate_limit(ctx: AppContext, request: Request, payload: EnrollRequest) -> None:
    """注册限速。见 ``services/ratelimit.py`` 里为什么需要它。

    先查全局再查单 IP：全局超了就直接拒，不用再动每个 IP 的计数 ——
    攻击者伪造源 IP 时，"每个 IP 一个计数器"本身就是可以被撑爆的东西。
    """
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    try:
        ctx.enroll_global_limiter.check("global", now)
        ctx.enroll_limiter.check(client_ip, now)
    except RateLimitExceeded as exc:
        log.warning(
            "注册被限速：ip=%s machine_id=%s", client_ip, (payload.machine_id or "")[:16]
        )
        raise ApiError(
            429,
            "rate_limited",
            exc.detail,
            headers={"Retry-After": str(exc.retry_after)},
        )


@router.post("/enroll", response_model=EnrollResponse)
def enroll(
    payload: EnrollRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> EnrollResponse:
    """用镜像里的统一密钥换回长期凭据。

    这是**唯一**的注册路径 —— 每选手注册码已经被"机器永久绑定名单条目"取代。
    逐台发码在"一份镜像装遍整间机房"的现实里根本不可行，而两种模式并存
    意味着每种都要维护、测试，并且迟早有人选错。

    注册出来的是三种状态之一（未配对 / 配好但没场次 / 能干活），
    Agent 必须按 `claimed` 与 `bound` 分开处理。
    """
    rate_limit(ctx, request, payload)

    raw_token = new_token(ctx.settings.token_bytes)
    raw_pair_code = new_pair_code(ctx.settings.pair_code_length)

    with ctx.db.session() as session:
        try:
            outcome = enrollment.enroll_machine(
                session,
                payload.bootstrap_key,
                machine_id=payload.machine_id,
                machine_uuid=payload.machine_uuid,
                machine_fingerprint=payload.machine_fingerprint,
                hostname=payload.hostname,
                os_info=payload.os_info,
                agent_version=payload.agent_version,
                raw_token=raw_token,
                raw_pair_code=raw_pair_code,
                # 注册冲突判定用的"还在线"阈值也走运行参数：它与离线扫描
                # 必须是同一个数，否则会出现"列表说它离线、注册说它还活着"
                offline_after_seconds=_runtime_settings(ctx).offline_after_seconds,
                pair_code_ttl_seconds=ctx.settings.pair_code_ttl_seconds,
            )
        except enrollment.BootstrapRejected as exc:
            # 码原样透出去：Agent 只对 ``bootstrap_key_*`` 做分支，
            # 「密钥不对」要让它停下来等人来修，而不是无限重试
            raise ApiError(exc.status_code, exc.code, exc.detail)

        agent = outcome.agent
        # 用**同一个会话**解析身份：刚注册的机器可能已经配好（UUID 认回），
        # 也可能还没配对 —— 两种都要如实回报，客户端才知道该做什么
        resolved = resolve_machine(session, agent)
        _remember_pair_code(ctx, agent, outcome.new_pair_code)
        response = _enroll_response(ctx, agent, outcome, resolved, raw_token, raw_pair_code)

    return response


def _remember_pair_code(ctx: AppContext, agent: Agent, code: Optional[str]) -> None:
    """把本次下发的配对码明文记进内存缓存。

    协议要求未配对的机器**每一轮 tick** 都带上当前有效的码，而库里只有哈希、
    还原不出来 —— 所以明文必须留一份在某处。这里选内存：
    它 30 分钟后自然失效、进程重启就没了，而代价只是教师重看一眼屏幕。
    理由详见 ``services/paircodes.py``。
    """
    if code:
        ctx.pair_codes.put(agent.id, code, agent.pair_code_expires_at)


def _enroll_response(
    ctx: AppContext,
    agent: Agent,
    outcome: enrollment.EnrollOutcome,
    resolved,
    raw_token: str,
    raw_pair_code: str,
) -> EnrollResponse:
    """把注册结果翻译成客户端的三个状态。

    这里的每一档都对应客户端一个不同的动作，混在一起就会出现
    "未配对的机器去扫代码"或者"配好的机器还在显示配对码"这类怪事。
    """
    paired = agent.roster_entry_id is not None
    payload = dict(
        token=raw_token,
        agent_id=agent.id,
        claimed=paired,
        bound=resolved.ok,
        config=_agent_config(ctx),
    )
    if not paired:
        payload["pair_code"] = raw_pair_code
        payload["reason"] = "这台机器还没有配对到人，请把配对码告诉老师"
    elif not resolved.ok:
        payload["reason"] = resolved.reason
    else:
        payload.update(
            player_no=resolved.player.player_no,
            player_name=resolved.player.name,
            contest_id=resolved.contest.id,
            contest_slug=resolved.contest.slug,
            contest_name=resolved.contest.name,
        )
    return EnrollResponse(**payload)



# --------------------------------------------------------------------------- #
# tick
# --------------------------------------------------------------------------- #


@router.post("/tick", response_model=TickResponse)
def tick(
    payload: TickRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    identity: Principal = Depends(require_principal),
) -> TickResponse:
    """一次心跳。已认领的机器收发文件，未认领的机器只在这里"排队等叫号"。"""
    if isinstance(identity, MachineIdentity):
        return _tick_pending(payload, ctx, identity)

    if payload.machine_id and payload.machine_id != identity.machine_id:
        # 凭据与声明的机器不符：可能是凭据被复制到了别的机器
        log.warning("machine_id 不匹配 agent=%s 声明=%s 实际=%s",
                    identity.agent_id, payload.machine_id, identity.machine_id)
        # 409 = "内容已不是当前版本"这一类**静默丢弃**的信号，Agent 不会重试；
        # 这里塞的是凭据被复制这种硬故障，Agent 会记日志并等下一条 tick，
        # 正是我们要的：不要让它重新注册，那会把现场线索换成一个新配对码
        raise ApiError(409, "machine_mismatch", "machine_id 与凭据不匹配")

    client_ip = request.client.host if request.client else None
    now = utcnow()
    # 只有"教师点过卸载"且"最近一次心跳报过公钥"时才有值，见 _pending_uninstall_token
    uninstall_token: Optional[str] = None

    with ctx.db.session() as session:
        # 「严格按题目预设回收」：只有**这一场配了题目**时才拿模式去过滤，
        # 一场都没配就照旧全收（理由见 services/collect.reconcile_scan 的 docstring）
        rules = problem_rules.rules_for(session, identity.contest_id, ctx.settings)
        collect = reconcile_scan(
            session,
            identity.player_id,
            payload.scan,
            ctx.settings,
            scan_complete=payload.scan_complete,
            rules=rules,
        )
        record_events(
            session,
            player_id=identity.player_id,
            contest_id=identity.contest_id,
            agent_id=identity.agent_id,
            events=collect.events,
        )

        partials = {p.asset_id: int(p.bytes_done) for p in payload.partials}
        jobs = collect_deploy_jobs(
            session, identity.player_id, identity.player_no, partials
        )

        if payload.completed_assets:
            _mark_deployments_done(session, identity.player_id, payload.completed_assets)

        upgrade = _pending_upgrade(session, ctx)

        agent = session.get(Agent, identity.agent_id)
        if agent is not None:
            agent.last_seen_at = now
            if payload.agent_version:
                agent.agent_version = payload.agent_version
            _note_release_public_key(agent, payload.release_public_key, now)
            _note_scan_missing(
                session,
                agent,
                payload.stats.scan_missing,
                player_id=identity.player_id,
                contest_id=identity.contest_id,
                now=now,
            )
            _note_scan_skipped(
                session,
                agent,
                collect,
                player_id=identity.player_id,
                contest_id=identity.contest_id,
            )
            uninstall_token = _pending_uninstall_token(ctx, agent, now)

    ctx.registry.note_tick(
        identity.agent_id,
        ip=client_ip,
        agent_version=payload.agent_version,
        scan_root=payload.scan_root,
        file_count=collect.seen_files + collect.new_files,
        disk_free=payload.stats.disk_free,
        last_error=payload.stats.last_error,
        scan_missing=payload.stats.scan_missing,
        scan_skipped=collect.skipped_unmatched,
    )

    # 自适应周期：有活干就收紧，没活干就放宽。两个值都是**运行参数**，现读 ——
    # 教师改完下一轮就按新节奏走，不用重启服务端
    live = _runtime_settings(ctx)
    active = bool(collect.need_upload or jobs or collect.deleted)
    next_tick = live.tick_active_seconds if active else live.tick_idle_seconds

    return TickResponse(
        server_time=unix_seconds(now),
        next_tick_seconds=next_tick,
        need_upload=collect.need_upload[:MAX_NEED_UPLOAD],
        deploy_jobs=jobs,
        cancel_assets=[],
        upgrade=upgrade,
        uninstall_token=uninstall_token,
        config=_agent_config(ctx),
        claimed=True,
        # 走到这一支就说明场次与选手都解析出来了 —— 这正是三态里的"能干活"那一档。
        # 漏掉它会让 Agent 自己都以为"还没轮到"，于是永远不去扫代码。
        bound=True,
        # 身份随每次 tick 一起回去：快照还原后 Agent 手上只剩 token，
        # 准考证号和场次都是在这一刻重新知道的。让它为此专门再 enroll 一次
        # 没必要 —— 那会多一次限速、多一条审计，还多一个可能失败的网络往返。
        player_no=identity.player_no,
        contest_slug=identity.contest_slug,
    )


def _tick_pending(
    payload: TickRequest, ctx: AppContext, identity: MachineIdentity
) -> TickResponse:
    """还干不了活的机器的心跳。

    把 last_seen 更新一下、然后如实告诉它卡在哪：

    * 还没配对 → ``claimed=false`` + **一个当前有效的配对码**，客户端把它
      显示给人看
    * 配好了但解析不出场次 → ``claimed=true, bound=false`` + 原因；
      **不要**再给配对码 —— 那会让教师以为配对没生效，跑去重配一遍，
      把好好的绑定搞乱

    它上报的扫描结果一律丢弃：没有准考证号，那些相对路径没法归属到任何人，
    收下来只会污染台账。

    周期固定用空闲值：它本来就没事可做。

    "发配对码"这件事必须在**每一次** tick 上做，而不是只在注册那一刻：
    机器可能被解绑、配对码可能过期、教师也可能过半小时才走到跟前。
    只在注册时发的话，解绑之后那台机器就永远拿不到新码了 ——
    它既没有 root 只读的统一密钥、又没有任何别的渠道能拿到码，
    只能靠重启碰运气。心跳是它唯一稳定的上行通道。

    还有一条同样重要：**同一个码在有效期内必须原样回同一串**。
    每轮换一个新的会让教师刚在屏幕上读到的数字当场作废，而现场看起来
    只是"配对码一直在跳"，没人会往心跳上想。

    "扫描根不存在"与配不配对无关，这里也照记：目录还没建这件事在**未配对**的
    机器上同样值得教师看见（它正是"机器在跑、但扫出来零个文件"的一半原因）。
    """
    now = utcnow()
    pair_code = None
    uninstall_token: Optional[str] = None
    with ctx.db.session() as session:
        agent = session.get(Agent, identity.agent_id)
        if agent is not None:
            agent.last_seen_at = now
            if payload.agent_version:
                agent.agent_version = payload.agent_version
            # 每次 tick 都更新主机名：教师常常一边装一边按座位改机器名，
            # 列表里显示旧名字会让人对着两台机器猜哪台是哪台
            if payload.hostname:
                agent.hostname = payload.hostname

            # 卸载授权与"配没配对"无关：要卸的可能是台配错人的机器、甚至是台
            # 还没认领的测试机。「机器」列表里有这一行的入口，这里就得能回话。
            _note_release_public_key(agent, payload.release_public_key, now)
            _note_scan_missing(
                session,
                agent,
                payload.stats.scan_missing,
                player_id=None,
                contest_id=None,
                now=now,
            )
            uninstall_token = _pending_uninstall_token(ctx, agent, now)

            if not identity.paired:
                # **有效性以库里的哈希与过期时刻为准**，缓存只负责提供明文。
                # 反过来（信缓存）的话，任何让库里那条码失效的动作 ——
                # 手工改过期时间、以后可能加的"立即作废配对码"按钮 ——
                # 都会被缓存里的旧值盖住，服务端继续回一个已经不该再用的数字。
                still_valid = (
                    agent.pair_code_hash is not None
                    and agent.pair_code_expires_at is not None
                    and agent.pair_code_expires_at >= now
                )
                if still_valid:
                    # 同一个码在有效期内**原样回同一串**：每轮换新的会让教师
                    # 刚读到的数字当场作废，而现场看起来只是"配对码一直在跳"
                    pair_code = ctx.pair_codes.get(agent.id, now)

                if pair_code is None:
                    # 第一次（缓存没了）、过期了、或者刚被解绑 —— 发一个新的
                    pair_code = new_pair_code(ctx.settings.pair_code_length)
                    enrollment.issue_pair_code(
                        agent, now, pair_code, ctx.settings.pair_code_ttl_seconds
                    )
                    ctx.pair_codes.put(agent.id, pair_code, agent.pair_code_expires_at)

    return TickResponse(
        server_time=unix_seconds(now),
        # 它本来就没事可做，用空闲周期（运行参数，现读）
        next_tick_seconds=_runtime_settings(ctx).tick_idle_seconds,
        claimed=identity.paired,
        pair_code=pair_code,
        bound=False,
        reason=identity.reason,
        uninstall_token=uninstall_token,
        config=_agent_config(ctx),
    )


def _note_release_public_key(agent: Agent, reported: bool, now) -> None:
    """记下这次心跳有没有报告发布公钥（升级信任锚）。

    报告"没有"时必须**清掉**旧值：信任锚是会被卸载脚本删掉的东西，只增不减的
    状态位会让服务端一直以为那台机器验得了签名 —— 而实际后果是签出去的授权
    到了那边一定被拒收，教师看到的却是"操作成功"。
    """
    agent.release_public_key_at = now if reported else None


def _note_scan_missing(
    session,
    agent: Agent,
    reported: List[str],
    *,
    now,
    player_id: Optional[int] = None,
    contest_id: Optional[int] = None,
) -> None:
    """记下本轮"不存在的扫描根"，**只在状态变化时**留一条审计。

    为什么按变化记而不是每轮记：心跳是 20 秒一次，一台机器一个上午能刷出上千条
    一模一样的事件 —— 那条审计就再也读不得了（真正要找的是"目录什么时候没的、
    什么时候回来的"）。

    判据是**集合**而不是列表：顺序变了不算变化。Agent 那边为了对账做了排序，
    但排序是它的实现细节，服务端不该因为对方换了排序方式就记一堆假事件。
    """
    current = scan_missing.normalize(reported)
    previous = scan_missing.load_scan_missing(agent.scan_missing_json)
    agent.scan_missing_json = scan_missing.dump_scan_missing(current)
    if current == previous:
        return

    if current:
        session.add(
            EventLog(
                level="warning",
                category="scan_missing",
                contest_id=contest_id,
                player_id=player_id,
                agent_id=agent.id,
                # 路径原样写进去：教师要去那台机器上建目录，需要的就是那几个字面路径
                message="扫描根不存在：%s" % "、".join(current),
            )
        )
    else:
        session.add(
            EventLog(
                level="info",
                category="scan_missing",
                contest_id=contest_id,
                player_id=player_id,
                agent_id=agent.id,
                message="扫描根已就位：%s" % "、".join(previous),
            )
        )


def _note_scan_skipped(
    session,
    agent: Agent,
    collect,
    *,
    player_id: Optional[int] = None,
    contest_id: Optional[int] = None,
) -> None:
    """记下本轮"被题目预设挡掉的文件数"，**只在状态变化时**留一条审计。

    与"扫描根不存在"同一个理由：心跳 30 秒一次，一台机器一上午能刷出上千条一样
    的事件，那条审计就再也读不得了。教师真正要找的是两个转折点 ——
    「从什么时候开始有文件被挡在外面」与「什么时候恢复正常」。

    判据是**有没有**而不是具体数量：9 个变 10 个不是状态变化，不值得再记一条
    （数量每轮都不同，按数量判等于每轮都记）。
    """
    current = int(collect.skipped_unmatched or 0)
    previous = int(agent.scan_skipped or 0)
    agent.scan_skipped = current or None
    if (previous > 0) == (current > 0):
        return

    if current > 0:
        session.add(
            EventLog(
                level="warning",
                category="scan_no_match",
                contest_id=contest_id,
                player_id=player_id,
                agent_id=agent.id,
                message="本轮有 %d 个文件不符合题目预设的位置或文件名，已跳过：%s"
                % (current, "、".join(collect.skipped_sample)),
                meta_json=json.dumps(
                    {
                        "count": current,
                        "sample": list(collect.skipped_sample),
                        "truncated": current > len(collect.skipped_sample),
                    },
                    ensure_ascii=False,
                ),
            )
        )
    else:
        session.add(
            EventLog(
                level="info",
                category="scan_no_match",
                contest_id=contest_id,
                player_id=player_id,
                agent_id=agent.id,
                message="不再有文件被挡在题目预设之外（上一次 %d 个）" % previous,
            )
        )


def _pending_uninstall_token(ctx: AppContext, agent: Agent, now) -> Optional[str]:
    """这台机器这一轮该拿到的卸载授权令牌；没有就是 ``None``。

    两个条件同时成立才签：**教师点过卸载**（内存里的登记表里有这台机器）且
    **最近一次心跳报过公钥**。第二个条件是"宁可不下发"的那一半：没有信任锚的
    机器验不了签名，发过去只会让它拒收，而教师那边显示的是操作成功 ——
    那条路必须在点按钮的时候就挡住（见 ``api/admin.py`` 的 uninstall_agent），
    这里再判一次是因为机器可能在这两次心跳之间把公钥弄丢了。

    同一枚令牌在多轮心跳里原样重发：机器可能一次没收到、也可能写盘失败重来。
    教师再点一次会换掉登记表里的那条记录，令牌随之变成新的一枚。
    """
    if agent.machine_uuid is None or not agent.release_public_key_at:
        return None
    if ctx.signing_key is None:
        # 运行时私钥被撤掉了（或者加载失败）：签不出来就老实不发，
        # 而不是发一个半成品让机器在那儿反复失败
        return None

    pending = ctx.pending_uninstalls.peek(agent.machine_uuid, now)
    if pending is None:
        return None
    return uninstall.mint_token(
        ctx.signing_key,
        machine_uuid=agent.machine_uuid,
        agent_id=agent.id,
        nonce=pending.nonce,
        issued_at=pending.issued_at,
    )


def _mark_deployments_done(
    session, player_id: int, asset_ids: List[int]
) -> None:
    """把该选手的这些资源对应的下发目标标记为完成，并同步任务整体状态。"""
    if not asset_ids:
        return
    targets = list(
        session.execute(
            select(DeployTarget)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(
                DeployTarget.player_id == player_id,
                DeployTask.asset_id.in_(asset_ids[:200]),
                DeployTarget.status.in_((DeployStatus.PENDING, DeployStatus.READY)),
            )
        ).scalars()
    )
    if not targets:
        return

    now = utcnow()
    affected_tasks = set()
    for target in targets:
        target.status = DeployStatus.DONE
        target.last_error = None
        target.updated_at = now
        affected_tasks.add(target.task_id)

    session.flush()
    for task_id in affected_tasks:
        _refresh_task_status(session, task_id)


def _refresh_task_status(session, task_id: int) -> None:
    """按目标的实际状态重算任务状态。

    不维护"任务状态"这个独立字段，而是每次从目标聚合 —— 两处独立维护的状态
    迟早会不一致，典型症状是 Web 上进度条卡在 90% 永远不动。
    """
    task = session.get(DeployTask, task_id)
    if task is None or task.status == DeployStatus.CANCELLED:
        return

    targets = list(
        session.execute(
            select(DeployTarget).where(DeployTarget.task_id == task_id)
        ).scalars()
    )
    if not targets:
        return

    done = sum(1 for t in targets if t.status == DeployStatus.DONE)
    failed = sum(1 for t in targets if t.status == DeployStatus.FAILED)
    if done == len(targets):
        task.status = DeployStatus.DONE
    elif done + failed == len(targets):
        task.status = DeployStatus.FAILED
    else:
        task.status = DeployStatus.PENDING


def _pending_upgrade(session, ctx: AppContext) -> Optional[UpgradeInfo]:
    """当前是否有向 Agent 铺开的升级版本。

    三个条件同时满足才下发：配置了签名私钥、有已铺开的版本、该版本未被撤回。
    缺任何一个都返回 None —— 宁可不下发，也不下发一个 Agent 必然拒收的包。

    随包带下去的三条安装策略与装机台账、包内 ``install_policy.json`` 逐字相同
    （同一个 helper）：机器升级时按同一套规则切换配置与密钥，不该因为"这次是升级
    而不是装机"就换一套。
    """
    if ctx.signing_key is None:
        return None
    row = session.execute(
        select(AgentRelease)
        .where(
            AgentRelease.published_at.isnot(None),
            AgentRelease.yanked_at.is_(None),
        )
        .order_by(AgentRelease.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return UpgradeInfo(
        version=row.version,
        url="/api/v1/agent/releases/%d" % row.id,
        sha256=row.sha256,
        signature=row.signature,
        size=int(row.size),
        notes=row.notes,
        **install_policy.build_install_policy(
            bootstrap_key_policy=row.bootstrap_key_policy,
            config_policy=install_policy.load_config_policy(row.config_policy_json),
            upgrade_mode=row.upgrade_mode,
            # 与台账那一处同一个理由：老记录没有策略列，但"夹带过密钥"这件事
            # 记在 bootstrap_key_id 上，不能用常量默认值把它盖掉
            has_bundled_key=row.bootstrap_key_id is not None,
        )
    )


# --------------------------------------------------------------------------- #
# 装机入口（**不鉴权**）
# --------------------------------------------------------------------------- #
#
# 这三个端点是"一台什么都没有的机器"唯一的起点：它上面没有 Agent、没有凭据、
# 什么都没有，只有一条 curl。所以它们**不能要求鉴权** —— 要求了就没有入口。
#
# 那"凭什么信"？分两段看：
#
# * **初次安装这一段不做鉴权**。这是有意选的：装机时服务端还不认识这台机器，
#   而包里装的只是一段"所有机器都要拿到的代码" —— 一场考试里没有秘密的另一半
#   是题面与测试点，那些走的是下发（有凭据、有进度、有审计）。
#   读者如果觉得这一步该验签，做法是**在镜像里预置发布公钥**，然后安装器就会
#   自己把它验上（``install.py --from-server`` 的行为：有公钥就验，没有就如实报
#   "未验签"并打印 sha256 供人核对）。
# * **装完之后靠配对建立信任**。机器注册上来是"待认领"状态，教师必须在管理界面
#   把它绑到名单里的某个人，它才开始收代码、收文件、收成绩 —— 这一步是有人看着的。
#
# 边界与别处一致：**只发已铺开、未撤回的版本**。"上传"和"铺开"的风险分界在这
# 里也成立 —— 一个刚构建出来、教师还没敢铺开的包，不该能被装机入口拿走。

#: 装机入口的路径。写成常量是为了让台账里的相对路径与真实路由**只有一处**，
#: 否则某天有人改了路径，台账会指到一个 404 上，而现象是"装机装不上"。
#:
#: 自举脚本那一条定义在 ``services/uninstall.py``：卸载授权发不出去时的替代命令
#: 也要用它，而那个提示是在管理端拼的 —— 两边共用一个常量才不会分叉。
INSTALL_LEDGER_PATH = "/api/v1/agent/install.json"
INSTALL_BUNDLE_PATH = "/api/v1/agent/install/bundle"
INSTALL_INSTALLER_PATH = "/api/v1/agent/install/installer"
INSTALL_BOOTSTRAP_PATH = uninstall.INSTALL_BOOTSTRAP_PATH


def _installable_release(session) -> Optional[AgentRelease]:
    """当前可以被装机的版本：**已铺开且未撤回**，取最新的那个。

    与 :func:`_pending_upgrade` 的判据一致（那边还多一条"配置了签名私钥"，
    因为下发未签名的包等于下发一个 Agent 必然拒收的东西）。这里不查私钥：
    装机入口给的是包体与校验和，客户端能不能验签是客户端的事。
    """
    return session.execute(
        select(AgentRelease)
        .where(
            AgentRelease.published_at.isnot(None),
            AgentRelease.yanked_at.is_(None),
        )
        .order_by(AgentRelease.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.get("/install.json", response_model=InstallLedgerOut)
def install_ledger(ctx: AppContext = Depends(get_ctx)) -> InstallLedgerOut:
    """当前可装机版本的台账。**不鉴权。**

    没有铺开任何版本时给 404 加一句人话 —— 而不是一个空对象：装机的人需要知道
    "是没铺开"还是"我地址打错了"，这两件事的处理完全不同。
    """
    with ctx.db.session() as session:
        row = _installable_release(session)
        if row is None:
            raise ApiError(
                404,
                "install_unavailable",
                "这台服务端还没有铺开任何 Agent 版本。装机入口只发已铺开的版本 ——"
                "在管理界面「Agent 发布」里选一个版本点「铺开」之后再来。",
            )
        return InstallLedgerOut(
            version=row.version,
            sha256=row.sha256,
            size=int(row.size),
            signature=row.signature,
            key_id=ctx.signing_key.key_id if ctx.signing_key else None,
            notes=row.notes,
            bundle=INSTALL_BUNDLE_PATH,
            installer=INSTALL_INSTALLER_PATH,
            bootstrap=INSTALL_BOOTSTRAP_PATH,
            **install_policy.build_install_policy(
                bootstrap_key_policy=row.bootstrap_key_policy,
                config_policy=install_policy.load_config_policy(row.config_policy_json),
                upgrade_mode=row.upgrade_mode,
                # 迁移之前建的记录没有策略列，可它确实夹带过密钥 —— 判据用
                # bootstrap_key_id，否则那批记录会被当成"教师选了不覆盖"
                has_bundled_key=row.bootstrap_key_id is not None,
            )
        )


@router.get("/install/bundle")
def install_bundle(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    """当前可装机版本的包体。**不鉴权**，支持 Range。

    与"给 Agent 升级用的下载端点"取的是同一份 blob、同一套 Range 逻辑 ——
    两条路如果各写一遍，早晚会出现"升级能续传、装机不能"这种说不通的差别。
    """
    with ctx.db.session() as session:
        row = _installable_release(session)
        if row is None:
            raise ApiError(
                404,
                "install_unavailable",
                "这台服务端还没有铺开任何 Agent 版本。",
            )
        sha256 = row.sha256
        expected_size = int(row.size)

    blob_path = ctx.blobs.path_for(sha256)
    if not blob_path.is_file():
        raise HTTPException(status_code=410, detail="安装包内容缺失")
    if expected_size and blob_path.stat().st_size != expected_size:
        raise HTTPException(status_code=500, detail="安装包内容损坏，大小与台账不符")
    return _range_response(blob_path, request)


@router.get("/install/installer")
def install_installer(ctx: AppContext = Depends(get_ctx)) -> Response:
    """安装器本体（``install.py``）。**不鉴权**。

    它**不在离线包里** —— 离线包是给机器**运行**用的，安装器是装机那一次用的，
    所以空机器必须先从服务端拿到它。这也是最短的那条自举链：
    一条 curl 拿到它，之后就全是 Python 的事了（shell 写错难查，能少写就少写）。
    """
    try:
        path = packaging.installer_path()
    except packaging.BuildError as exc:
        # 生产上服务端可能是 pip 装出来的、根本不在仓库里。这时老实说清楚，
        # 而不是给一个 404 让人以为是路径写错了。
        raise ApiError(404, "installer_unavailable", "%s发不出去：%s" % ("安装器", exc.detail))
    return FileResponse(
        str(path),
        media_type="text/x-python",
        filename="install.py",
    )


@router.get("/install/bootstrap.sh")
def install_bootstrap(ctx: AppContext = Depends(get_ctx)) -> Response:
    """自举脚本（``bootstrap.sh``）。**不鉴权。**

    给的是"**一条** curl 就能起头"的那条命令 —— 空机器上只有 shell，而教师不该
    需要先手工把 install.py 弄过去。脚本本身极短：抓 install.py、跑起来、参数原样
    传下去；真正的逻辑全在 Python 里。

    它与安装器一样**不在**离线包里（``packaging/`` 被排除），所以只能由服务端发。
    """
    try:
        path = packaging.bootstrap_path()
    except packaging.BuildError as exc:
        raise ApiError(404, "installer_unavailable", "%s发不出去：%s" % ("自举脚本", exc.detail))
    return FileResponse(str(path), media_type="text/x-sh", filename="bootstrap.sh")

@router.post("/files", response_model=UploadResult)
def upload_file(
    request: Request,
    path: str = Form(...),
    sha256: str = Form(...),
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> UploadResult:
    """接收一个文件内容。

    **旧版本保护**：若客户端送来的 ``sha256`` 与服务端台账当前记录的版本不一致，
    说明这是迟到的旧内容（断网补传、乱序重传），直接以 409 拒绝，让客户端在下一
    轮 tick 里拿到真正需要的版本。绝不覆盖更新的版本。
    """
    try:
        rel_path = validate_relpath(path, max_length=ctx.settings.max_relpath_length)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail="路径不合法: %s" % exc)

    if not SHA256_RE.match(sha256 or ""):
        raise HTTPException(status_code=400, detail="sha256 格式不合法")

    # 场次时间窗的门禁放在**读文件内容之前**：结束之后不该再往 blobs/ 里落任何
    # 一个字节。放晚了（比如放到写台账那一步）虽然接口也返回 409，但磁盘上已经
    # 多了一份内容、而且会被后续的"内容寻址去重"当成合法版本。
    _reject_outside_window(ctx, identity, utcnow())

    try:
        actual_sha, size = ctx.blobs.put_stream(
            file.file, max_bytes=ctx.settings.max_file_size, expected_sha256=sha256
        )
    except BlobTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except HashMismatch as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = utcnow()
    with ctx.db.session() as session:
        row = session.execute(
            select(SourceFile).where(
                SourceFile.player_id == identity.player_id,
                SourceFile.rel_path == rel_path,
            )
        ).scalar_one_or_none()

        if row is None:
            # 台账里还没有：Agent 比服务端先看到了这个文件。
            # 这不危险（路径与内容都已校验），建一条即可。
            row = SourceFile(
                player_id=identity.player_id,
                rel_path=rel_path,
                sha256=actual_sha,
                size=size,
                mtime=0,
                revision=0,
                content_stored=False,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(row)
        elif row.content_stored and row.sha256 != actual_sha:
            raise ApiError(
                409,
                "stale_upload",
                "该内容已不是当前版本，已在下一轮 tick 中请求最新版本",
                {"reason": "stale", "expected": row.sha256},
            )

        row.sha256 = actual_sha
        row.size = size
        row.revision += 1
        row.content_stored = True
        row.deleted_at = None
        row.last_seen_at = now
        session.flush()
        revision = row.revision

    # 物化到评测器可读的目录：source/<场次>/<选手>/<相对路径>
    contest_dir = Path(ctx.settings.source_root) / identity.contest_slug / identity.player_no
    try:
        dest = safe_join(contest_dir, rel_path, max_length=ctx.settings.max_relpath_length)
        materialize(ctx.blobs.path_for(actual_sha), dest)
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail="落盘路径不合法: %s" % exc)
    except OSError as exc:
        log.exception("物化到 source/ 失败: %s", rel_path)
        raise HTTPException(status_code=500, detail="落盘失败: %s" % exc)

    return UploadResult(path=rel_path, sha256=actual_sha, revision=revision, stored=True)


# --------------------------------------------------------------------------- #
# 下载（支持 Range 断点续传）
# --------------------------------------------------------------------------- #


@router.get("/assets/{asset_id}")
def download_asset(
    asset_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> Response:
    with ctx.db.session() as session:
        asset = session.get(Asset, asset_id)
        if asset is None or asset.contest_id != identity.contest_id:
            raise HTTPException(status_code=404, detail="资源不存在")

        # 授权：必须确实是"下发给这个选手"的资源。
        # 仅凭 token 有效是不够的 —— 否则任一选手可以拖走全场测试点。
        authorized = session.execute(
            select(DeployTarget.id)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(
                DeployTask.asset_id == asset_id,
                DeployTarget.player_id == identity.player_id,
            )
        ).first()
        if authorized is None:
            raise HTTPException(status_code=403, detail="该资源未下发给本选手")

        blob_sha = asset.sha256
        expected_size = int(asset.size)

    blob_path = ctx.blobs.path_for(blob_sha)
    if not blob_path.is_file():
        raise HTTPException(status_code=410, detail="资源内容缺失")
    if expected_size and blob_path.stat().st_size != expected_size:
        raise HTTPException(status_code=500, detail="资源内容损坏，大小与台账不符")

    return _range_response(blob_path, request)


def _range_response(file_path: Path, request: Request) -> Response:
    """实现 HTTP Range，用于断点续传。

    不依赖框架的 Range 支持：这是断点续传的核心路径，行为必须明确可测。
    """
    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            file_path,
            media_type="application/octet-stream",
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    match = _RANGE_RE.match(range_header.strip())
    if not match:
        return Response(
            status_code=416,
            headers={"Content-Range": "bytes */%d" % file_size},
        )

    start_raw, end_raw = match.groups()
    if start_raw == "":
        # suffix range：最后 N 字节
        if end_raw == "":
            return Response(status_code=416, headers={"Content-Range": "bytes */%d" % file_size})
        start = max(0, file_size - int(end_raw))
        end = file_size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1

    if start >= file_size or start > end:
        return Response(status_code=416, headers={"Content-Range": "bytes */%d" % file_size})
    end = min(end, file_size - 1)
    length = end - start + 1

    def iter_range() -> Iterator[bytes]:
        with file_path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_RANGE_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            "Content-Range": "bytes %d-%d/%d" % (start, end, file_size),
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


# --------------------------------------------------------------------------- #
# 自更新包下载
# --------------------------------------------------------------------------- #


@router.get("/releases/{release_id}")
def download_release(
    release_id: int,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> Response:
    """下载 Agent 升级包。

    只提供**已铺开且未撤回**的版本 —— 上传但未 rollout 的包对 Agent 不可见，
    这样"上传"和"铺开"的风险边界在服务端也是硬的，而不是只靠 Agent 自觉。
    """
    with ctx.db.session() as session:
        row = session.get(AgentRelease, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        if row.published_at is None:
            raise HTTPException(status_code=404, detail="该版本尚未铺开")
        if row.yanked_at is not None:
            raise HTTPException(status_code=410, detail="该版本已撤回")
        sha256 = row.sha256
        expected_size = int(row.size)

    blob_path = ctx.blobs.path_for(sha256)
    if not blob_path.is_file():
        raise HTTPException(status_code=410, detail="发布包内容缺失")
    if expected_size and blob_path.stat().st_size != expected_size:
        raise HTTPException(status_code=500, detail="发布包内容损坏，大小与台账不符")

    return _range_response(blob_path, request)


# --------------------------------------------------------------------------- #
# 事件上报
# --------------------------------------------------------------------------- #


@router.post("/events", response_model=SimpleAck)
def report_events(
    payload: List[AgentEvent],
    ctx: AppContext = Depends(get_ctx),
    identity: AgentIdentity = Depends(require_agent),
) -> SimpleAck:
    """Agent 主动上报的审计事件（权限异常、磁盘满、批量重写等）。

    刻意用列表批量提交：断网期间事件会堆积，逐条上报会在重连瞬间产生请求风暴。
    """
    if not payload:
        return SimpleAck(ok=True, detail="无事件")
    if len(payload) > 200:
        payload = payload[:200]

    with ctx.db.session() as session:
        for item in payload:
            session.add(
                EventLog(
                    level=item.level,
                    category=item.category[:32],
                    contest_id=identity.contest_id,
                    player_id=identity.player_id,
                    agent_id=identity.agent_id,
                    message=item.message[:2000],
                    meta_json=(
                        _dumps(item.meta) if item.meta else None
                    ),
                )
            )
    return SimpleAck(ok=True, detail="已记录 %d 条" % len(payload))


def _dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)[:8000]
    except (TypeError, ValueError):
        return "{}"


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


@router.get("/me")
def whoami(
    identity: AgentIdentity = Depends(require_agent),
    ctx: AppContext = Depends(get_ctx),
) -> Dict[str, Any]:
    runtime = ctx.registry.get(identity.agent_id)
    return {
        "agent_id": identity.agent_id,
        "player_no": identity.player_no,
        "player_name": identity.player_name,
        "contest_id": identity.contest_id,
        "contest_slug": identity.contest_slug,
        "contest_name": identity.contest_name,
        "contest_status": identity.contest_status,
        "machine_id": identity.machine_id,
        "online": bool(runtime and runtime.online),
        "config": _agent_config(ctx),
    }
