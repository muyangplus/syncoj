"""RSA PKCS#1 v1.5 (SHA-256) 签名验证 —— 纯标准库实现。

为什么是 RSA 而不是 Ed25519
---------------------------
方案里原本写的是 Ed25519，但 Python 3.8 标准库**没有**任何非对称签名原语
（``hashlib`` 只有摘要，没有椭圆曲线）。而 Agent 必须零依赖。

RSA 的验证恰好是纯 Python 能做到的：整个验证只需要一次模幂
``pow(signature, 65537, n)``（公钥指数只有 17 位，因此很快），其余全是字节比较。
相比之下 Ed25519 需要实现完整的 Curve25519 点运算 —— 那是几百行精心设计的
大整数运算，手写风险高得多。

本模块**只做验证**。Agent 永远不持有私钥，也不该包含签名代码。

严格校验
--------
PKCS#1 v1.5 签名历史上被 Bleichenbacher (2006) 等攻击打穿过，**全部**源于宽松
校验：解析出 hash 就认为通过、容忍多余字节、或允许 padding 长度不合规。

因此这里按 RFC 8017 §8.2.2 做**逐字节精确重建再比较**：

- 签名长度必须恰好等于模数字节数
- 重建出完整的 ``EM``，再用 ``hmac.compare_digest`` 整体比对

不解析、不容忍、不留任何余量。任何一处不符合就直接拒绝。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Dict

__all__ = [
    "RSAPublicKey",
    "SignatureError",
    "verify_pkcs1v15_sha256",
    "PKCS1_SHA256_DIGEST_INFO_PREFIX",
]

#: RFC 8017 §9.2 规定的 SHA-256 DigestInfo 前缀（包含外层 SEQUENCE）。
#: 结构：SEQUENCE(30 31) SEQUENCE(30 0d) OID(06 09 60 86 48 01 65 03 04 02 01)
#:       NULL(05 00) OCTET STRING(04 20)
PKCS1_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)

#: RSA 模数的最小位数。低于这个长度在现代算力下不安全 —— 与其接受一个弱密钥，
#: 不如直接拒绝启动。
MIN_MODULUS_BITS = 2048


class SignatureError(Exception):
    """公钥格式不合法。签名不匹配属于正常结果，用返回值 False 表达。"""


def _unb64(text: str) -> bytes:
    """解码 base64url（容忍缺少的填充与标准 base64 的 +/）。"""
    cleaned = text.strip().replace("+", "-").replace("/", "_")
    padding = "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(cleaned + padding)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("base64 解码失败: %s" % exc)


@dataclass(frozen=True)
class RSAPublicKey:
    n: int
    e: int
    #: 密钥标识，仅用于日志与轮换时区分，不参与密码学运算
    key_id: str = ""

    def __post_init__(self) -> None:
        if self.n <= 0:
            raise SignatureError("模数必须为正整数")
        if self.e < 3 or self.e % 2 == 0:
            raise SignatureError("公钥指数必须是大于等于 3 的奇数")
        if self.n.bit_length() < MIN_MODULUS_BITS:
            raise SignatureError(
                "模数只有 %d 位，低于安全下限 %d 位" % (self.n.bit_length(), MIN_MODULUS_BITS)
            )

    @property
    def byte_length(self) -> int:
        """模数的字节长度，即合法签名的长度。"""
        return (self.n.bit_length() + 7) // 8

    @property
    def bits(self) -> int:
        return self.n.bit_length()

    def to_dict(self) -> Dict[str, str]:
        return {
            "alg": "RS256",
            "key_id": self.key_id,
            "n": _b64(self.n),
            "e": _b64(self.e),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RSAPublicKey":
        if not isinstance(data, dict):
            raise SignatureError("公钥必须是 JSON 对象")
        alg = data.get("alg")
        if alg not in (None, "RS256"):
            raise SignatureError("不支持的算法: %r" % alg)
        try:
            n = int.from_bytes(_unb64(str(data["n"])), "big")
            e = int.from_bytes(_unb64(str(data["e"])), "big")
        except KeyError as exc:
            raise SignatureError("公钥缺少字段: %s" % exc)
        return cls(n=n, e=e, key_id=str(data.get("key_id", "")))


def _b64(raw: int) -> str:
    length = (raw.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(raw.to_bytes(length, "big")).decode("ascii").rstrip("=")


def expected_encoded_message(byte_length: int, message: bytes) -> bytes:
    """按 RFC 8017 §9.2 构造 EMSA-PKCS1-v1_5 编码。

    独立成函数是为了让服务端的签名侧复用同一套构造逻辑 —— 两边算出来的
    ``EM`` 必须逐字节相同。
    """
    digest_info = PKCS1_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = byte_length - len(digest_info) - 3
    if padding_length < 8:
        raise SignatureError("模数太短，容不下 PKCS#1 v1.5 填充")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info


def verify_pkcs1v15_sha256(
    public_key: RSAPublicKey,
    message: bytes,
    signature: bytes,
) -> bool:
    """验证签名。任何异常情况都返回 ``False``，不抛异常。

    返回 ``False`` 的原因可能是签名被篡改，也可能是格式非法 —— 调用方不需要
    区分，两种情况都应当拒绝。
    """
    if not isinstance(message, (bytes, bytearray)):
        return False
    if not isinstance(signature, (bytes, bytearray)):
        return False

    length = public_key.byte_length
    # 长度必须精确匹配。容忍短签名（左填充 0）是经典的历史漏洞
    if len(signature) != length:
        return False

    s = int.from_bytes(signature, "big")
    # s 必须落在 [0, n)。s >= n 会让模幂结果产生歧义
    if s >= public_key.n:
        return False

    try:
        recovered = pow(s, public_key.e, public_key.n)
        encoded = recovered.to_bytes(length, "big")
        expected = expected_encoded_message(length, bytes(message))
    except (ValueError, OverflowError, SignatureError):
        return False

    # 整体恒定时间比对，而不是"解析出来再看 hash 对不对"。
    # 后者正是历史上多次伪造攻击的入口。
    return hmac.compare_digest(encoded, expected)
