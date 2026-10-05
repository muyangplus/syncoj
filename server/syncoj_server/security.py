"""认证与密钥派生。

设计取舍
--------
* **不使用 JWT**。Agent token 与管理员会话都是不透明随机串，服务端只存其
  SHA-256。理由：JWT 无法按机吊销，而"某台机器泄密要单独吊销"是本项目的硬需求。
* **口令用 ``hashlib.scrypt``**（标准库）。不引入 passlib/bcrypt，少一个依赖，
  且 scrypt 本身就是抗 GPU 的现代选择。
* token 长度取 32 字节（``secrets.token_urlsafe`` → 43 字符），枚举不可行。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

__all__ = [
    "new_token",
    "hash_token",
    "tokens_equal",
    "new_enroll_code",
    "hash_enroll_code",
    "new_pair_code",
    "hash_pair_code",
    "new_bootstrap_key",
    "hash_bootstrap_key",
    "hash_password",
    "verify_password",
]

#: 念得出、抄得对的字符集。去掉 I/O/0/1 —— 配对码要被人从考试机屏幕上读出来、
#: 再在教师的电脑上敲进去，这两个字形是抄错的头号来源。
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# scrypt 参数。n=2**14 在服务端约 50~100ms，足以让离线爆破不划算，
# 又不至于让管理员登录明显卡顿。内存开销约 16MB。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16


def new_token(nbytes: int = 32) -> str:
    """生成不透明 token（明文只在此刻存在一次）。"""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw: str) -> str:
    """token 的存储形态。

    用单轮 SHA-256 而非 scrypt 是有意的：token 是 256 位高熵随机串，
    离线爆破不可行，因此不需要慢哈希 —— 而它每次请求都要算，必须足够快。
    """
    if not isinstance(raw, str):
        raise TypeError("token 必须是字符串")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tokens_equal(a: Optional[str], b: Optional[str]) -> bool:
    """恒定时间比较，避免时序侧信道。"""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a, b)


def new_enroll_code(nbytes: int = 16) -> str:
    """生成注册码。为了让教师能念/抄，用大写字母数字分组形式。"""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(nbytes))
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def hash_enroll_code(raw: str) -> str:
    """注册码的存储形态。先做规范化，容忍教师输入时的大小写与分隔符差异。"""
    normalized = "".join(ch for ch in raw.upper() if ch.isalnum())
    return hashlib.sha256(("enroll:" + normalized).encode("utf-8")).hexdigest()


def new_pair_code(length: int = 6) -> str:
    """生成配对短码。

    6 位、32 个字符可选 ≈ 2^30 ≈ 10 亿种组合。这个空间单独看不算大，
    但短码**只在几分钟内有效、用一次就作废、而且必须由管理员在后台输入**，
    所以暴力试的窗口极小。真要靠它当长期凭据就不行了 —— 它从来不是凭据，
    只是"人在机器前，确认这台是哪台"的凭证。
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def hash_pair_code(raw: str) -> str:
    """配对码的存储形态。和注册码一样，容忍大小写、空格与连字符。"""
    normalized = "".join(ch for ch in raw.upper() if ch.isalnum())
    return hashlib.sha256(("pair:" + normalized).encode("utf-8")).hexdigest()


def new_bootstrap_key(nbytes: int = 32) -> str:
    """生成镜像内置的统一注册密钥。

    比注册码长得多（32 字节 vs 16）：一把注册码对应一个选手，泄漏了只是
    "多了一台冒充某个人的机器"；一把 bootstrap key 对应**整间机房**，
    泄漏了等于交出"无限注册"的能力。所以熵要够，而且它不该出现在
    ``agent.ini`` 这种选手读得到的地方。
    """
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(nbytes))
    return "-".join(raw[i : i + 8] for i in range(0, len(raw), 8))


def hash_bootstrap_key(raw: str) -> str:
    normalized = "".join(ch for ch in raw.upper() if ch.isalnum())
    return hashlib.sha256(("bootstrap:" + normalized).encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    import base64

    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    """返回 ``scrypt$n$r$p$salt$hash`` 形式的自描述串。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        _b64(salt),
        _b64(digest),
    )


def verify_password(password: str, encoded: str) -> bool:
    """校验口令。任何解析失败都返回 False，不抛异常。"""
    if not password or not encoded:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, hash_raw = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = _unb64(salt_raw)
        expected = _unb64(hash_raw)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)
