"""考生页登录令牌：准考证号 + 场次考生页密码换来的短期凭据。

为什么不是会话表
----------------
管理端会话存库，是因为要能"按人吊销"。考生页令牌不需要：它只读、只看自己、
有效期几个小时，真正的吊销手段是**改掉或清除场次的考生页密码** —— 令牌里签进了
密码哈希的指纹（``pwfp``），密码一换，之前签出去的令牌全部验不过。于是这里用
无状态的 HMAC 令牌，服务端重启也不丢（密钥落盘），不给库添一张只增不减的表。

令牌形状
--------
``v1.<contest_id>.<player_id>.<过期 Unix 秒>.<签名>``，签名是
``HMAC-SHA256(密钥, "player-token:v1:<contest_id>:<player_id>:<exp>:<pwfp>")``
的 urlsafe base64。``pwfp`` 不出现在令牌里，只参与签名：验证时从库里现取。

密钥
----
``<data_root>/player-token.key``，首次用到时生成（32 字节随机，0600）。放在数据
目录而不是 ``.key/``：``.key/`` 可能不存在（pip 安装的布局），而这把钥匙丢了的
后果只是"所有考生重新登录一次"，不值得为它要求运维多配一个目录。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .. import keys

__all__ = [
    "TOKEN_TTL_SECONDS",
    "PlayerTokenClaims",
    "issue_token",
    "load_or_create_secret",
    "parse_token",
    "password_fingerprint",
    "verify_token",
]

#: 令牌有效期：一场考试再长也在 8 小时内。过期了重新输一次密码即可。
TOKEN_TTL_SECONDS = 8 * 3600

_VERSION = "v1"
_SECRET_NAME = "player-token.key"
_lock = threading.Lock()


@dataclass(frozen=True)
class PlayerTokenClaims:
    contest_id: int
    player_id: int
    expires_at: int


def load_or_create_secret(data_root: Path) -> bytes:
    """读出（没有就生成）签令牌用的密钥。

    加锁是为了"两个请求同时第一次登录"时不各生成一把、后写的覆盖先写的，
    让先签出去的那枚令牌立刻失效。
    """
    path = Path(data_root) / _SECRET_NAME
    with _lock:
        try:
            text = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            text = ""
        try:
            secret = bytes.fromhex(text)
        except ValueError:
            secret = b""
        if len(secret) >= 32:
            return secret
        secret = secrets.token_bytes(32)
        keys.write_secret_text(path, secret.hex() + "\n")
        return secret


def password_fingerprint(password_hash: Optional[str]) -> str:
    """密码哈希的指纹。签进令牌，换密码 / 清密码即吊销全部旧令牌。"""
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:32]


def _sign(secret: bytes, contest_id: int, player_id: int, expires_at: int, pwfp: str) -> str:
    message = "player-token:%s:%d:%d:%d:%s" % (_VERSION, contest_id, player_id, expires_at, pwfp)
    digest = hmac.new(secret, message.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_token(
    secret: bytes, contest_id: int, player_id: int, password_hash: str, now: int
) -> Tuple[str, int]:
    """签一枚令牌，返回 ``(令牌, 过期 Unix 秒)``。"""
    expires_at = int(now) + TOKEN_TTL_SECONDS
    signature = _sign(
        secret, contest_id, player_id, expires_at, password_fingerprint(password_hash)
    )
    token = "%s.%d.%d.%d.%s" % (_VERSION, contest_id, player_id, expires_at, signature)
    return token, expires_at


def parse_token(token: str) -> Optional[PlayerTokenClaims]:
    """只拆形状、不验签。拆不开就是 ``None``。"""
    parts = (token or "").strip().split(".")
    if len(parts) != 5 or parts[0] != _VERSION:
        return None
    try:
        contest_id, player_id, expires_at = (int(parts[1]), int(parts[2]), int(parts[3]))
    except ValueError:
        return None
    if contest_id <= 0 or player_id <= 0:
        return None
    return PlayerTokenClaims(contest_id, player_id, expires_at)


def verify_token(
    secret: bytes, token: str, password_hash: Optional[str], now: int
) -> Optional[PlayerTokenClaims]:
    """验签 + 验过期。``password_hash`` 是场次**当前**的考生页密码哈希。

    场次已经没有密码（被清除）时一律不通过：清除密码就是"关掉这条登录路径"。
    """
    claims = parse_token(token)
    if claims is None or not password_hash:
        return None
    expected = _sign(
        secret,
        claims.contest_id,
        claims.player_id,
        claims.expires_at,
        password_fingerprint(password_hash),
    )
    signature = token.strip().split(".")[4]
    if not hmac.compare_digest(signature, expected):
        return None
    if int(now) >= claims.expires_at:
        return None
    return claims
