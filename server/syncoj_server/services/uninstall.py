"""远程卸载授权（服务端）。

为什么要有这一层
----------------
Agent 以**选手登录账号**的身份运行，而卸载要删的 ``/opt/syncoj``、
``/etc/syncoj``（里面有统一注册密钥）、systemd 单元全是 root 的 —— 所以卸载
必须由 root 执行，而触发它的念头来自教师点的那一下按钮。

两端之间隔着一条**不可信链路**，所以授权不能是"服务端说一句卸载吧"：

* 令牌只有在教师点过按钮之后才存在。选手拿不到签名私钥，伪造不出来 ——
  这正是"不让选手自己卸掉监控"的全部依据。
* 令牌绑死本机 ``machine_uuid``，而且带短有效期（15 分钟），搬不到别的机器上。
* 机器本地用**已有的**升级信任锚（``/etc/syncoj/release-key.pub.json``）验签。
  刻意不新增第二套信任链：多一把密钥就多一个要铺、要轮换、要出错的东西。

签名与验签各自复用现成实现
--------------------------
签名走 :mod:`services.signing`（与发布包同一套 RSASSA-PKCS1-v1_5 + SHA-256），
签名的对象是 ``b64url(payload)`` 那一段 ASCII 字符串**本身**，不是原始 JSON ——
JSON 的键序与空白在两端不保证相同，签原文等于把"两边编码一模一样"变成一条
谁也不知道的隐含契约。

待卸载登记表**只在内存里**
--------------------------
"教师点过卸载"这个事实不需要落库，反而**不该**落库：一旦落库，任何拿到库文件
（备份、误传、随手拷走的 ``syncoj.db``）的人就拿到一枚能在那台机器上换一次
root 删除的凭据。放内存的代价是服务端重启后这次请求消失，教师重点一次即可 ——
比"库文件里躺着 root 凭据"便宜得多。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models import unix_seconds, utcnow
from .signing import SigningKey, b64url_decode, b64url_encode

__all__ = [
    "KIND",
    "VERSION",
    "TOKEN_TTL_SECONDS",
    "PENDING_TTL_SECONDS",
    "INSTALL_BOOTSTRAP_PATH",
    "PendingUninstall",
    "PendingUninstalls",
    "mint_token",
    "token_payload",
    "verify_token",
    "nonce_for",
    "fallback_uninstall_command",
]

log = logging.getLogger(__name__)

#: 令牌种类。写进 payload 而不是靠路径区分 —— 机器上只有这一份验签代码，
#: 将来若再加入别的"一次性 root 动作"，它必须能看出这枚令牌是给谁用的。
KIND = "agent_uninstall"

#: 令牌格式版本。将来 payload 结构要变时靠它拒绝老格式，而不是猜字段。
VERSION = 1

#: 令牌有效期。短到"选手捡到也没用"，长到"教师点完按钮、机器的下一次心跳还赶得上"。
#: Agent 空闲时每 20 秒 tick 一次，15 分钟是 45 个周期 —— 余量很足，而一条被
#: 误点的卸载请求最多也就在服务端挂着这么久。
TOKEN_TTL_SECONDS = 900

#: 待卸载登记在服务端保留多久。与令牌同寿：过了这一刻就没必要再往那台机器上
#: 发任何东西，教师重新点一次即可。
PENDING_TTL_SECONDS = TOKEN_TTL_SECONDS

#: 随机数派生用的域分隔前缀。改它会改变所有已签发令牌里的 nonce，所以它不是
#: 一个"随手调的常量"，而是这套格式的一部分。
_NONCE_DOMAIN = b"syncoj.agent_uninstall.nonce.v1"

#: 装机入口里那条自举脚本的路径。
#:
#: 它**刻意定义在这里而不是 ``api/agent.py``**（那个文件才是路由所在）：发不出
#: 卸载授权的错误提示里要带一条能照着做的替代命令，而主动去 import 一整个
#: 路由模块只为拿一个字符串常量、还顺带制造循环依赖，不值。
#: ``api/agent.py`` 与 ``api/admin.py`` 两边都从这里取，路径因此只有一处真相。
INSTALL_BOOTSTRAP_PATH = "/api/v1/agent/install/bootstrap.sh"


def fallback_uninstall_command(base_url: str) -> str:
    """站到机器前面手工卸载的那条命令。

    ``--yes`` 不是可选项：管道里 stdin 不是终端，安装器在非交互时会拒绝执行
    卸载（它删的是统一注册密钥与升级信任锚，不看一眼就删掉只能重新注册）。
    与装机页 ``web/src/api/install.ts`` 里那条命令逐字一致 —— 两边写得不一样
    的话，教师抄的是哪一条就得靠运气。
    """
    base = (base_url or "").rstrip("/") or "<服务端>"
    return "curl -fsSL %s%s | sudo sh -s -- --uninstall --yes" % (
        base,
        INSTALL_BOOTSTRAP_PATH,
    )


def nonce_for(machine_uuid: str, requested_at: int) -> str:
    """由 ``(机器, 点击时刻)`` 推出令牌里的随机数。

    它必须是**确定的**而不能是每次心跳现取一个随机数：同一枚令牌要被那台机器在
    多轮心跳里拿到同一份（机器可能一次没收到、也可能写盘失败重来），而"教师又
    点了一次"必须换一枚新的。用点击的时刻做输入，这两个要求同时满足。

    HMAC 而不是裸 ``sha256``：输入里有机器可控的 ``machine_uuid``，裸哈希会让
    它有机会去撞另一个 ``(uuid, 时刻)`` 组合的值。HMAC 的密钥是进程私有的
    随机串，机器端推不出任何别的取值。
    """
    message = (
        _NONCE_DOMAIN
        + b"\x00"
        + machine_uuid.encode("utf-8")
        + b"\x00"
        + str(int(requested_at)).encode("ascii")
    )
    return hmac.new(_nonce_key(), message, hashlib.sha256).hexdigest()[:32]


_NONCE_KEY: Optional[bytes] = None


def _nonce_key() -> bytes:
    """进程级 nonce 派生密钥。**不落盘** —— 见模块 docstring。"""
    global _NONCE_KEY
    if _NONCE_KEY is None:
        _NONCE_KEY = secrets.token_bytes(32)
    return _NONCE_KEY


@dataclass(frozen=True)
class PendingUninstall:
    """一次"教师点过卸载"的记录。"""

    machine_uuid: str
    issued_at: int
    nonce: str
    admin: str
    agent_id: int

    @property
    def expires_at(self) -> int:
        return self.issued_at + PENDING_TTL_SECONDS


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #


def mint_token(
    key: SigningKey,
    *,
    machine_uuid: str,
    agent_id: int,
    nonce: str,
    issued_at: int,
) -> str:
    """签一枚卸载授权令牌：``b64url(payload) + "." + b64url(signature)``。

    ``key_id`` 取自私钥本身（它是模数的哈希），因此与
    ``release-key.pub.json`` 里那个字段天然一致 —— 机器那边靠它认出"这是不是我
    信任锚里那把钥匙"。
    """
    payload = {
        "v": VERSION,
        "kind": KIND,
        "machine_uuid": machine_uuid,
        "agent_id": int(agent_id),
        "nonce": nonce,
        "issued_at": int(issued_at),
        "expires_at": int(issued_at) + TOKEN_TTL_SECONDS,
        "key_id": key.key_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signed = b64url_encode(raw)
    signature = key.sign(signed.encode("ascii"))
    return "%s.%s" % (signed, b64url_encode(signature))


def token_payload(token: str) -> Dict[str, Any]:
    """取出令牌里的 payload。**只解码不验签** —— 验签是机器的事。

    服务端自己要用它的地方只有一个：测试与排查时确认签出来的东西读得回来。
    """
    signed, _dot, _signature = token.partition(".")
    if not signed:
        raise ValueError("令牌缺少 payload 段")
    return json.loads(b64url_decode(signed).decode("utf-8"))


def verify_token(public_key: Any, token: str) -> bool:
    """用 ``public_key`` 验一枚令牌。返回是否通过。

    用的是**机器侧那份独立实现**（``agent/syncoj_agent/rsa.py``）：这里要回答的
    问题不是"服务端能不能验自己签的东西"，而是"那台机器会不会认它"。两边同时
    对同一枚令牌点头，才算真的对接上了。

    Agent 源码不在 ``sys.path`` 上时抛 ``ImportError``（生产部署里服务端可能只装了
    ``server/``）：这是个**开发期与测试期的核对工具**，不是运行时代码。
    """
    from syncoj_agent.rsa import verify_pkcs1v15_sha256

    signed, dot, signature = token.partition(".")
    if not dot or not signed or not signature:
        return False
    try:
        raw_signature = b64url_decode(signature)
    except (ValueError, TypeError):
        return False
    try:
        return bool(
            verify_pkcs1v15_sha256(public_key, signed.encode("ascii"), raw_signature)
        )
    except Exception:  # pragma: no cover - 验签实现不抛，这里只为兜底
        return False


# --------------------------------------------------------------------------- #
# 待卸载登记表
# --------------------------------------------------------------------------- #


class PendingUninstalls:
    """``machine_uuid -> 待执行的卸载``。只在内存里，见模块 docstring。

    线程安全：FastAPI 的同步端点跑在线程池里，管理员点按钮与机器心跳可能同时
    改这张表。临界区里只有 dict 操作，锁竞争可以忽略。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, PendingUninstall] = {}

    # ---------------------------------------------------------------- #
    # 写入
    # ---------------------------------------------------------------- #

    def request(
        self,
        machine_uuid: str,
        *,
        agent_id: int,
        admin: str,
        now: Optional[datetime] = None,
    ) -> PendingUninstall:
        """记下"教师点了这台机器的卸载"，返回这一次的回执。

        重复点击是**允许**的，而且必须换掉上一次：它是"重新签发一枚"的表达，
        旧令牌因此自然作废。同一秒里点两次会派生出同一个 nonce，所以要往后挪
        一秒再算 —— 否则第二枚会签成和第一枚一模一样，而教师看到的却是"重新
        签发了一枚"，实际什么都没变。
        """
        stamp = unix_seconds(now or utcnow())
        with self._lock:
            while any(
                item.machine_uuid == machine_uuid
                and item.nonce == nonce_for(machine_uuid, stamp)
                for item in self._items.values()
            ):
                stamp += 1

            record = PendingUninstall(
                machine_uuid=machine_uuid,
                issued_at=stamp,
                nonce=nonce_for(machine_uuid, stamp),
                admin=admin,
                agent_id=int(agent_id),
            )
            self._items[machine_uuid] = record
            return record

    def forget(self, machine_uuid: str) -> None:
        with self._lock:
            self._items.pop(machine_uuid, None)

    def prune(self, now: Optional[datetime] = None) -> int:
        """丢掉已经过期的登记，返回丢掉了几条。

        机器卸载完就不会再有心跳了，所以这些条目不会自己走掉；而只要不清理，
        进程就会带着一批早已无效的 ``machine_uuid`` 一直活下去。
        """
        stamp = unix_seconds(now or utcnow())
        with self._lock:
            gone = [
                uuid_key
                for uuid_key, item in self._items.items()
                if item.expires_at <= stamp
            ]
            for uuid_key in gone:
                del self._items[uuid_key]
        return len(gone)

    # ---------------------------------------------------------------- #
    # 读取
    # ---------------------------------------------------------------- #

    def peek(
        self, machine_uuid: str, now: Optional[datetime] = None
    ) -> Optional[PendingUninstall]:
        """取这一台机器当前待执行的卸载；没有或已过期就返回 ``None``。

        读的时候顺手丢掉过期条目：这是最热的路径（每次心跳都会走），过期判定
        放在这里就不需要单独的定时任务。
        """
        stamp = unix_seconds(now or utcnow())
        with self._lock:
            item = self._items.get(machine_uuid)
            if item is None:
                return None
            if item.expires_at <= stamp:
                del self._items[machine_uuid]
                return None
            return item

    def items(self) -> List[PendingUninstall]:
        with self._lock:
            return list(self._items.values())
