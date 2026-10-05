"""统一密钥注册与短码配对。

两个动作，一个目的：让「一份镜像装遍整间机房」这件事成立，同时不丢掉
"这台机器是哪个学生"。

* ``enroll_with_bootstrap_key`` —— 用镜像里的统一密钥换回一台机器的凭据。
  换出来的机器**没有归属**（`MachineClaim`），只有一张待认领的排队票。
* ``claim_machine`` —— 教师把这台机器认领到某个选手头上，此时才产生
  ``Agent`` 行，同一个 token 原样转过去。

为什么未认领的机器另立一张表
----------------------------
把它塞进 ``agent`` 需要 ``player_id`` 可空，而 SQLite 改不了列的可空性，
只能重建表；重建要关外键强制，而 ``PRAGMA foreign_keys`` 在事务里是空操作，
真关不掉时 ``DROP TABLE`` 会顺着级联把 ``agent_status`` 删光。

另立一张表还带来一个语义上的好处：未认领的机器**根本不知道自己的准考证号**，
它算不出扫描目录、扫不了代码、传不了文件 —— 它的全部工作就是"等着"。

快照还原怎么自愈
----------------
机器上的 ``credential.json`` 会随快照还原消失，连自己生成的 UUID 也一起没了。
所以注册时还要带上**硬件指纹**（SMBIOS UUID 之类）。指纹在还原后不变，
服务端靠它认出"这是原来那台机器"，直接把原凭据换发出去，**配对关系保留** ——
否则每还原一次就要教师重新配对一次，50 台机器就是 50 次人工。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Agent, BootstrapKey, EventLog, MachineClaim, utcnow
from ..security import hash_bootstrap_key

__all__ = [
    "PAIR_CODE_TTL_SECONDS",
    "BootstrapRejected",
    "ClaimResult",
    "claim_machine",
    "enroll_with_bootstrap_key",
    "find_claim_by_token",
    "fingerprint_duplicates",
    "issue_pair_code",
]

log = logging.getLogger(__name__)

#: 配对码有效期。它是"人在机器前确认这是哪台"的凭证，不是长期凭据 ——
#: 过期了就重新生成一次，成本极低，而窗口越小越安全。
PAIR_CODE_TTL_SECONDS = 30 * 60


class BootstrapRejected(Exception):
    """统一密钥不可用。**带上 HTTP 状态码**，因为调用方是 HTTP 接口：
    把"密钥错了"和"密钥被吊销了"分成不同的状态码，排错时才有方向。
    """

    def __init__(self, detail: str, status_code: int = 403) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class ClaimResult:
    """认领结果：agent_id + 那台机器的信息。"""

    agent_id: int
    player_id: int
    player_no: str
    contest_id: int
    contest_slug: str
    contest_name: str
    machine_id: str
    hostname: Optional[str]


@dataclass
class BootstrapEnrollment:
    """统一密钥注册的结果。

    ``mode`` 两种取值，**互斥地**决定哪个字段有值：

    * ``pending``     —— 全新的机器，``claim`` 有值，等着被认领
    * ``reactivated`` —— 指纹认回了原来那台机器，``agent`` 有值，配对关系保留

    刻意不做成"两个字段都可能为空、让调用方自己判断"：那样早晚会有人只处理
    一种情况，而另一种会以 AttributeError 的形式在考场上出现。
    """

    mode: str
    agent: Optional[Agent] = None
    claim: Optional[MachineClaim] = None

    @property
    def is_pending(self) -> bool:
        return self.mode == "pending"

    def require_agent(self) -> Agent:
        if self.agent is None:  # pragma: no cover - 由构造方式保证
            raise RuntimeError("这个注册结果没有归属机器（mode=%s）" % self.mode)
        return self.agent

    def require_claim(self) -> MachineClaim:
        if self.claim is None:  # pragma: no cover - 由构造方式保证
            raise RuntimeError("这个注册结果没有待认领记录（mode=%s）" % self.mode)
        return self.claim


# --------------------------------------------------------------------------- #
# 统一密钥
# --------------------------------------------------------------------------- #


def _load_key(session: Session, raw_key: str, now) -> BootstrapKey:
    key = session.execute(
        select(BootstrapKey).where(BootstrapKey.key_hash == hash_bootstrap_key(raw_key))
    ).scalar_one_or_none()
    if key is None:
        # 404 而不是 403：密钥压根不存在和存在但被吊销，是两件不同的事，
        # 现场排查时这个区别很值钱
        raise BootstrapRejected("统一密钥无效", status_code=404)
    if key.revoked_at is not None:
        raise BootstrapRejected("统一密钥已被吊销")
    if key.expires_at is not None and key.expires_at < now:
        raise BootstrapRejected("统一密钥已过期")
    key.last_used_at = now
    key.use_count = int(key.use_count or 0) + 1
    return key


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #


def _agent_by_fingerprint(session: Session, fingerprint: Optional[str]) -> List[Agent]:
    """按指纹找机器。指纹为空/无效时返回空列表 —— 不能拿"空指纹"去匹配。"""
    if not fingerprint or len(fingerprint) < 8:
        return []
    return list(
        session.execute(
            select(Agent).where(
                Agent.machine_fingerprint == fingerprint,
                Agent.revoked_at.is_(None),
            )
        ).scalars()
    )


def _is_live(agent: Agent, now, offline_after_seconds: int) -> bool:
    """这台机器还在心跳吗？

    这是"指纹认回"能否自动进行的关键判据，不是可有可无的优化：

    指纹只能证明**硬件**相同。快照还原时"机器关机了、还原、再开机"，
    期间原机器是离线的；而克隆镜像时"母机多半还开着"，一台新机器却报出同样的
    指纹 —— 后者如果把身份拿走了，那个学生的成绩就会被另一台机器的代码污染，
    而且完全静默。所以在线时一律不认回，改走人工配对。
    """
    if agent.last_seen_at is None:
        return False
    return (now - agent.last_seen_at).total_seconds() < offline_after_seconds


def fingerprint_duplicates(session: Session) -> List[Tuple[str, int]]:
    """找出被多台机器共用的指纹。

    这是**克隆镜像的告警信号**：正常情况下每个物理机器的 SMBIOS UUID 都不同。
    如果一批机器报同一个指纹，说明镜像是在某台机器"跑过之后"才做的，
    或者那批机器本来就是同一个虚拟机模板 —— 无论哪种，配对与成绩归属都可能串。
    """
    rows = session.execute(
        select(Agent.machine_fingerprint, func.count(Agent.id))
        .where(Agent.machine_fingerprint.isnot(None), Agent.revoked_at.is_(None))
        .group_by(Agent.machine_fingerprint)
        .having(func.count(Agent.id) > 1)
    ).all()
    return [(str(fingerprint), int(count)) for fingerprint, count in rows]


# --------------------------------------------------------------------------- #
# 配对码
# --------------------------------------------------------------------------- #


def issue_pair_code(claim: MachineClaim, now, raw_code: str) -> None:
    """把新配对码写进 claim（只存哈希）。"""
    from ..security import hash_pair_code

    claim.pair_code_hash = hash_pair_code(raw_code)
    claim.pair_code_expires_at = now + timedelta(seconds=PAIR_CODE_TTL_SECONDS)


def claim_code_is_valid(claim: MachineClaim, raw_code: str, now) -> bool:
    from ..security import hash_pair_code

    if not raw_code or claim.pair_code_hash is None:
        return False
    if claim.pair_code_expires_at is not None and claim.pair_code_expires_at < now:
        return False
    return claim.pair_code_hash == hash_pair_code(raw_code)


# --------------------------------------------------------------------------- #
# 用统一密钥注册
# --------------------------------------------------------------------------- #


def enroll_with_bootstrap_key(
    session: Session,
    raw_key: str,
    *,
    machine_id: str,
    hostname: Optional[str],
    os_info: Optional[str],
    agent_version: Optional[str],
    machine_uuid: Optional[str],
    machine_fingerprint: Optional[str],
    raw_token: str,
    raw_pair_code: str,
    offline_after_seconds: int,
) -> BootstrapEnrollment:
    """用统一密钥注册。

    指纹认得出原来那台机器、**而且它当前是离线的**，就把凭据换发给它
    （配对关系保留，快照还原走这条）。否则进待认领队列。
    """
    from ..security import hash_token

    now = utcnow()
    _load_key(session, raw_key, now)

    token_hash = hash_token(raw_token)
    matches = _agent_by_fingerprint(session, machine_fingerprint)
    live = [agent for agent in matches if _is_live(agent, now, offline_after_seconds)]

    if len(matches) > 1 and not live:
        # 指纹撞了。不能"猜一个"—— 猜错就是把成绩算到别人头上。
        # 老老实实进待认领，并留下一条显眼的审计事件让教师去看。
        session.add(
            EventLog(
                level="warning",
                category="enroll_ambiguous",
                message=(
                    "有 %d 台机器共用同一个硬件指纹（%s…），无法自动认回。"
                    "这台机器已进入待认领列表，请人工确认。"
                    "常见原因：镜像是在某台机器跑过之后才克隆的。"
                    % (len(matches), (machine_fingerprint or "")[:16])
                ),
            )
        )
    elif len(matches) == 1 and not live:
        agent = matches[0]
        agent.token_hash = token_hash
        agent.last_enrolled_at = now
        agent.last_seen_at = now
        agent.machine_id = machine_id or agent.machine_id
        agent.hostname = hostname or agent.hostname
        agent.os_info = os_info or agent.os_info
        agent.agent_version = agent_version or agent.agent_version
        if machine_uuid:
            agent.machine_uuid = machine_uuid

        # 同一台机器重新注册时，把它的待认领队列清掉（如果还留着一条）
        _drop_claim_by_uuid(session, machine_uuid)

        session.add(
            EventLog(
                level="info",
                category="enroll",
                contest_id=_contest_id_of(session, agent),
                player_id=agent.player_id,
                message="按硬件指纹认回已有机器，配对关系保留：machine_id=%s"
                % (machine_id or "")[:16],
            )
        )
        session.flush()
        return BootstrapEnrollment(mode="reactivated", agent=agent)

    if live:
        # 指纹对应的机器**正在心跳**，却又来了一台报同一个指纹的。
        # 这几乎不可能是"同一台机器回来了"（它明明在线），更可能是克隆镜像 ——
        # 而克隆出来的机器如果拿走了原机器的身份，那个学生的成绩就会被
        # 另一台机器的代码污染，且完全静默。
        session.add(
            EventLog(
                level="warning",
                category="enroll_conflict",
                message=(
                    "有机器上报的硬件指纹（%s…）与一台**在线**机器的相同，"
                    "已拒绝自动认回、改为待认领。"
                    "若确实是在做镜像克隆，请先关掉母机。"
                    % (machine_fingerprint or "")[:16]
                ),
            )
        )

    claim = MachineClaim(
        token_hash=token_hash,
        machine_uuid=machine_uuid,
        machine_fingerprint=machine_fingerprint,
        machine_id=machine_id,
        hostname=hostname,
        os_info=os_info,
        agent_version=agent_version,
        created_at=now,
        last_seen_at=now,
    )
    issue_pair_code(claim, now, raw_pair_code)
    session.add(claim)
    session.add(
        EventLog(
            level="info",
            category="enroll",
            message="新机器用统一密钥注册，等待配对：machine_id=%s host=%s"
            % ((machine_id or "")[:16], hostname or "未知"),
        )
    )
    session.flush()
    return BootstrapEnrollment(mode="pending", claim=claim)


def _drop_claim_by_uuid(session: Session, machine_uuid: Optional[str]) -> None:
    if not machine_uuid:
        return
    for stale in session.execute(
        select(MachineClaim).where(MachineClaim.machine_uuid == machine_uuid)
    ).scalars():
        session.delete(stale)


def _contest_id_of(session: Session, agent: Agent) -> Optional[int]:
    from ..models import Player

    player = session.get(Player, agent.player_id) if agent.player_id else None
    return player.contest_id if player else None


# --------------------------------------------------------------------------- #
# 认领
# --------------------------------------------------------------------------- #


def find_claim_by_token(session: Session, token_hash: str) -> Optional[MachineClaim]:
    return session.execute(
        select(MachineClaim).where(
            MachineClaim.token_hash == token_hash, MachineClaim.revoked_at.is_(None)
        )
    ).scalar_one_or_none()


def claim_machine(
    session: Session,
    claim: MachineClaim,
    player,
    contest,
    now=None,
) -> ClaimResult:
    """把一台待认领的机器认领到某个选手。

    **一个选手只能有一台机器。** 如果那个选手已经有别的机器在线，直接拒绝 ——
    放行的话两台机器的代码会往同一个目录里写，而这是静默的：成绩矩阵只是
    看起来"这个人交了两遍"，谁也不知道哪一份是真的。

    换绑（把一台已经认领过的机器改给别人）走的是另一条路：
    那需要先解绑，属于管理动作，不该混在配对里。
    """
    from ..models import EnrollmentMode

    now = now or utcnow()

    # 场次的注册方式是**有意义**的：它声明"这个场次的机器是怎么进来的"。
    # 让配对无视它，那个设置就只是装饰 —— 而一个没有效果的设置比没有设置更糟，
    # 因为它会让人以为"我明明选对了"。
    #
    # 反方向不拦：一个场次声明用统一密钥时，教师**特意**为某个选手签发的
    # 单人码仍然可用 —— 那是有意为之的补位动作。
    if contest.effective_enrollment_mode != EnrollmentMode.BOOTSTRAP:
        raise BootstrapRejected(
            "场次「%s」的注册方式是「每选手注册码」，不接受统一密钥注册上来的机器。\n"
            "要么把场次的注册方式改成「镜像统一密钥 + 短码配对」，"
            "要么用为该选手签发的注册码单独注册这台机器。"
            % contest.name,
            status_code=409,
        )

    occupied = session.execute(
        select(Agent).where(Agent.player_id == player.id, Agent.revoked_at.is_(None))
    ).scalars().all()
    if occupied:
        raise BootstrapRejected(
            "选手 %s 已经绑定了一台机器（%s）。"
            "要换机器请先解绑原来那台。" % (player.player_no, occupied[0].machine_id[:16]),
            status_code=409,
        )

    agent = Agent(
        player_id=player.id,
        token_hash=claim.token_hash,
        machine_id=claim.machine_id or "unknown",
        hostname=claim.hostname,
        os_info=claim.os_info,
        agent_version=claim.agent_version,
        machine_uuid=claim.machine_uuid,
        machine_fingerprint=claim.machine_fingerprint,
        enrolled_at=claim.created_at or now,
        last_enrolled_at=now,
        last_seen_at=claim.last_seen_at,
        claimed_at=now,
    )
    session.add(agent)
    session.delete(claim)

    session.add(
        EventLog(
            level="info",
            category="claim",
            contest_id=contest.id,
            player_id=player.id,
            message="机器配对完成：%s ← %s @ %s"
            % (player.player_no, claim.hostname or "未知主机", (claim.machine_id or "")[:16]),
        )
    )
    session.flush()

    return ClaimResult(
        agent_id=agent.id,
        player_id=player.id,
        player_no=player.player_no,
        contest_id=contest.id,
        contest_slug=contest.slug,
        contest_name=contest.name,
        machine_id=agent.machine_id,
        hostname=agent.hostname,
    )
