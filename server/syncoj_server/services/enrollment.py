"""机器注册与配对。

新模型只有一条路：

    装机（root 一次性）        机器首次开机              教师
    bootstrap.key  ──换凭据──▶  桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的**人**

配对是**永久**的：绑的是名单条目（人），不是某场比赛的选手。所以同一个学生
换一场比赛不用重新配，只要新场次应用了那份名单。

配对之后认机器不再靠配对码，而是两条依据：

* ``machine_uuid`` —— Agent 首次运行时生成并持久化，**首选**
* ``machine_fingerprint`` —— 硬件指纹，快照还原把 UUID 一起抹掉时的第二道

为什么指纹那条要附加"原机器当前离线"的条件
------------------------------------------
指纹只能证明**硬件**相同。快照还原的时序是"关机 → 还原 → 再开机"，
期间原机器是离线的；而克隆镜像时"母机多半还开着"，一台新机器却报出同样的
指纹。后者如果把身份拿走了，那个学生的成绩就会被另一台机器的代码污染，
而且完全静默。所以原机器在线时一律不认回，改走人工配对。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Agent, BootstrapKey, Contest, EventLog, Player, RosterEntry, utcnow
from ..security import hash_bootstrap_key, hash_pair_code, hash_token

__all__ = [
    "PAIR_CODE_TTL_SECONDS",
    "BindOutcome",
    "BootstrapRejected",
    "EnrollOutcome",
    "bind_machine",
    "enroll_machine",
    "find_unbound_by_pair_code",
    "fingerprint_duplicates",
    "issue_pair_code",
    "pair_code_is_valid",
]

log = logging.getLogger(__name__)

#: 配对码有效期。它是"人在机器前确认这是哪台"的凭证，不是长期凭据 ——
#: 过期了就重新生成一次，成本极低，而窗口越小越安全。
PAIR_CODE_TTL_SECONDS = 30 * 60


class BootstrapRejected(Exception):
    """统一密钥不可用，或配对动作被拒。

    **带上 HTTP 状态码与机器可读的错误码**：调用方是 HTTP 接口，排错与客户端
    分支都要靠它们 —— "密钥压根不存在"和"存在但被吊销"是不同的状态码，
    "这台机器已经配对给别人了"和"这个人已经有机器了"是不同的 ``code``。
    只给 ``detail`` 的话，前端就只能拿中文字符串做比较，而那句话随时会被改得更通顺。
    """

    def __init__(self, detail: str, status_code: int = 403, code: str = "conflict") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code


@dataclass
class EnrollOutcome:
    """注册结果。

    ``created`` 只用于日志与审计：新机器与老机器重新注册走的是同一条路，
    但"这台是第一次来"在排错时是有用的信息。
    """

    agent: Agent
    created: bool
    #: 认回老机器时说明是靠什么认回的（uuid / fingerprint），便于排错
    matched_by: Optional[str] = None
    #: 本次是否生成了新的配对码
    new_pair_code: Optional[str] = None


@dataclass
class BindOutcome:
    agent: Agent
    entry: RosterEntry
    #: 目标条目此前已经绑过别的机器（这次是覆盖？不 —— 那是错误）
    previously_bound_agent: Optional[str] = None


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
        raise BootstrapRejected("统一密钥无效", status_code=404, code="bootstrap_key_invalid")
    if key.revoked_at is not None:
        raise BootstrapRejected("统一密钥已被吊销", code="bootstrap_key_revoked")
    if key.expires_at is not None and key.expires_at < now:
        raise BootstrapRejected("统一密钥已过期", code="bootstrap_key_expired")
    key.last_used_at = now
    key.use_count = int(key.use_count or 0) + 1
    return key


# --------------------------------------------------------------------------- #
# 配对码
# --------------------------------------------------------------------------- #


def issue_pair_code(
    agent: Agent, now, raw_code: str, ttl_seconds: Optional[int] = None
) -> None:
    """把新配对码写进 agent（只存哈希）。

    ``ttl_seconds`` 缺省时用模块常量；接口层从配置传进来，测试里才好把
    时钟推快一点，而不是靠 sleep。
    """
    ttl = PAIR_CODE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    agent.pair_code_hash = hash_pair_code(raw_code)
    agent.pair_code_expires_at = now + timedelta(seconds=ttl)


def pair_code_is_valid(agent: Agent, raw_code: str, now) -> bool:
    if not raw_code or agent.pair_code_hash is None:
        return False
    if agent.pair_code_expires_at is not None and agent.pair_code_expires_at < now:
        return False
    return agent.pair_code_hash == hash_pair_code(raw_code)


def find_unbound_by_pair_code(
    session: Session, raw_code: str, now
) -> Optional[Agent]:
    """按配对码找一台未配对的机器。

    逐个比对哈希，而不是按哈希直接查表 —— 后者需要一个"码 → 机器"的索引，
    那意味着要存明文或者可逆的东西，不值得为这点性能换。
    未配对的机器通常只有个位数。
    """
    for agent in session.execute(
        select(Agent).where(
            Agent.roster_entry_id.is_(None),
            Agent.pair_code_hash.isnot(None),
            Agent.revoked_at.is_(None),
        )
    ).scalars():
        if pair_code_is_valid(agent, raw_code, now):
            return agent
    return None


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #


def _machine_by_uuid(session: Session, machine_uuid: Optional[str]) -> Optional[Agent]:
    if not machine_uuid:
        return None
    return session.execute(
        select(Agent).where(Agent.machine_uuid == machine_uuid, Agent.revoked_at.is_(None))
    ).scalar_one_or_none()


def _machines_by_fingerprint(session: Session, fingerprint: Optional[str]) -> List[Agent]:
    """按指纹找机器。指纹为空/太短时返回空 —— **不能拿空值互相匹配**。

    否则所有读不到指纹的机器（虚拟机、SMBIOS 不可读）会彼此"认回"，
    那等于把一台机器的身份白送给另一台。
    """
    if not fingerprint or len(fingerprint) < 8:
        return []
    return list(
        session.execute(
            select(Agent).where(
                Agent.machine_fingerprint == fingerprint, Agent.revoked_at.is_(None)
            )
        ).scalars()
    )


def _is_live(agent: Agent, now, offline_after_seconds: int) -> bool:
    """这台机器还在心跳吗？—— 决定指纹认回能不能自动进行，见模块 docstring。"""
    if agent.last_seen_at is None:
        return False
    return (now - agent.last_seen_at).total_seconds() < offline_after_seconds


def fingerprint_duplicates(session: Session) -> List[Tuple[str, int]]:
    """找出被多台机器共用的指纹 —— 克隆镜像的告警信号。

    正常情况下每台物理机的 SMBIOS UUID 都不同。撞了指纹说明镜像是在某台机器
    **跑过之后**才克隆的，那批机器里可能已经有人的凭据被一起拷了进去。
    """
    rows = session.execute(
        select(Agent.machine_fingerprint, func.count(Agent.id))
        .where(Agent.machine_fingerprint.isnot(None), Agent.revoked_at.is_(None))
        .group_by(Agent.machine_fingerprint)
        .having(func.count(Agent.id) > 1)
    ).all()
    return [(str(fingerprint), int(count)) for fingerprint, count in rows]


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


def enroll_machine(
    session: Session,
    raw_key: str,
    *,
    machine_id: str,
    machine_uuid: Optional[str],
    machine_fingerprint: Optional[str],
    hostname: Optional[str],
    os_info: Optional[str],
    agent_version: Optional[str],
    raw_token: str,
    raw_pair_code: str,
    offline_after_seconds: int,
    pair_code_ttl_seconds: Optional[int] = None,
) -> EnrollOutcome:
    """用统一密钥注册（或重新注册）。

    认机器的顺序刻意是 **UUID 优先、指纹兜底**：

    1. UUID 能对上 → 就是它。这是配对之后的常态，一次索引命中
    2. UUID 新（快照还原把它抹了）但指纹对上一台**离线**的机器 → 认回它，
       顺带把新 UUID 记下来
    3. 都对不上 → 新机器，发一个配对码等教师绑人

    指纹那条要附加"离线"条件，理由见模块 docstring。
    """
    now = utcnow()
    _load_key(session, raw_key, now)
    token_hash = hash_token(raw_token)

    agent = _machine_by_uuid(session, machine_uuid)
    matched_by = "uuid" if agent is not None else None

    if agent is None:
        matches = _machines_by_fingerprint(session, machine_fingerprint)
        live = [item for item in matches if _is_live(item, now, offline_after_seconds)]

        if len(matches) == 1 and not live:
            agent = matches[0]
            matched_by = "fingerprint"
        elif live:
            # 指纹相同的机器**正在心跳**，却又来了一台报同一指纹的。
            # 这几乎不可能是"同一台机器回来了"（它明明在线），更可能是克隆 ——
            # 而克隆机拿走原机器的身份，那个学生的成绩就会被另一台机器的代码
            # 污染，且完全静默。拒绝自动认回，改走人工配对。
            session.add(
                EventLog(
                    level="warning",
                    category="enroll_conflict",
                    message=(
                        "有机器上报的硬件指纹（%s…）与一台**在线**机器的相同，"
                        "已拒绝自动认回、改为待配对。"
                        "若确实是在做镜像克隆，请先关掉母机。"
                        % (machine_fingerprint or "")[:16]
                    ),
                )
            )
        elif len(matches) > 1:
            session.add(
                EventLog(
                    level="warning",
                    category="enroll_ambiguous",
                    message=(
                        "有 %d 台机器共用同一个硬件指纹（%s…），无法自动认回。"
                        "这台机器已进入待配对列表，请人工确认。"
                        "常见原因：镜像是在某台机器跑过之后才克隆的。"
                        % (len(matches), (machine_fingerprint or "")[:16])
                    ),
                )
            )
        else:
            matched_by = None

    if agent is None:
        agent = Agent(
            token_hash=token_hash,
            machine_id=machine_id,
            machine_uuid=machine_uuid or None,
            machine_fingerprint=machine_fingerprint or None,
            hostname=hostname,
            os_info=os_info,
            agent_version=agent_version,
            enrolled_at=now,
            last_enrolled_at=now,
            last_seen_at=now,
        )
        issue_pair_code(agent, now, raw_pair_code, pair_code_ttl_seconds)
        session.add(agent)
        session.add(
            EventLog(
                level="info",
                category="enroll",
                message="新机器注册，等待配对：machine_id=%s host=%s"
                % (machine_id[:16], hostname or "未知"),
            )
        )
        session.flush()
        return EnrollOutcome(agent=agent, created=True, new_pair_code=raw_pair_code)

    # 认回了老机器：换发凭据，绑定关系原样保留
    agent.token_hash = token_hash
    agent.last_enrolled_at = now
    agent.last_seen_at = now
    agent.machine_id = machine_id or agent.machine_id
    agent.hostname = hostname or agent.hostname
    agent.os_info = os_info or agent.os_info
    agent.agent_version = agent_version or agent.agent_version
    if machine_uuid:
        agent.machine_uuid = machine_uuid
    if machine_fingerprint:
        agent.machine_fingerprint = machine_fingerprint

    # 还没配对的机器重新注册时换一个新码：教师手上那个可能早就过期了
    new_code = None
    if agent.roster_entry_id is None:
        new_code = raw_pair_code
        issue_pair_code(agent, now, new_code, pair_code_ttl_seconds)

    session.add(
        EventLog(
            level="info",
            category="enroll",
            message="机器重新注册（按 %s 认回，%s）"
            % (
                matched_by or "machine_id",
                "已配对给 %s" % agent.roster_entry_id
                if agent.roster_entry_id is not None
                else "尚未配对",
            ),
        )
    )
    session.flush()
    return EnrollOutcome(
        agent=agent, created=False, matched_by=matched_by, new_pair_code=new_code
    )


# --------------------------------------------------------------------------- #
# 配对
# --------------------------------------------------------------------------- #


def bind_machine(
    session: Session,
    agent: Agent,
    entry: RosterEntry,
    now=None,
) -> BindOutcome:
    """把一台机器配对到名单里的某个人。**这是永久绑定。**

    两条必须守住的规则：

    1. **一个人只能有一台机器。** 两台机器绑同一个人，代码会往同一个目录里写，
       而且完全静默（成绩矩阵只是看起来"这个人交了两遍"）。
    2. **一台机器只能属于一个人。** 覆盖已有绑定要显式「改派」，
       不能让"再输一次配对码"悄悄换掉一个人的身份 —— 那可能是教师在另一台
       机器上输错了数字。
    """
    now = now or utcnow()

    if agent.roster_entry_id is not None and agent.roster_entry_id != entry.id:
        current = session.get(RosterEntry, agent.roster_entry_id)
        raise BootstrapRejected(
            "这台机器已经配对给 %s 了。要换人请用「改派」，"
            "或者在列表里先解除绑定。" % (current.player_no if current else "?"),
            status_code=409,
            code="machine_already_bound",
        )

    taken = session.execute(
        select(Agent).where(
            Agent.roster_entry_id == entry.id,
            Agent.revoked_at.is_(None),
            Agent.id != agent.id,
        )
    ).scalars().all()
    if taken:
        raise BootstrapRejected(
            "%s 已经有一台机器了（%s）。要换机器请先作废那一台，或用「改派」。"
            % (entry.player_no, taken[0].machine_id[:16]),
            status_code=409,
            code="roster_entry_taken",
        )

    agent.roster_entry_id = entry.id
    agent.claimed_at = now
    # 配对码是一次性的：用过就作废，且只在绑定时用
    agent.pair_code_hash = None
    agent.pair_code_expires_at = None

    session.add(
        EventLog(
            level="info",
            category="bind",
            message="机器配对：%s ← %s @ %s"
            % (entry.player_no, agent.hostname or "未知主机", agent.machine_id[:16]),
        )
    )
    session.flush()
    return BindOutcome(agent=agent, entry=entry)
