"""发布包签名（服务端）。

与 ``agent/syncoj_agent/rsa.py`` 是**两份独立实现**：Agent 那份只做验证，这份
只做签名。Agent 永远不持有私钥，也不该包含签名代码 —— 少一个能出错的入口。

密钥来源
--------
用 ``openssl`` 生成，本模块只负责解析与使用：

::

    python -m syncoj_server.cli genkey --out /etc/syncoj/release-key.pem

解析同时兼容 PKCS#1 与 PKCS#8 —— OpenSSL 3.0 起 ``openssl rsa`` 默认输出
PKCS#8，而老版本输出 PKCS#1，两边都得能读，否则换个 openssl 版本就崩。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DerError",
    "SigningKey",
    "load_signing_key",
    "generate_keypair",
    "openssl_available",
    "parse_version",
    "write_public_key_json",
]


def parse_version(text: str) -> Tuple[int, ...]:
    """把 ``"1.2.3"`` 解析成 ``(1, 2, 3)``。

    数字比较而非字符串比较：``"1.10.0" > "1.9.0"`` 在版本语义上成立，但用字符串
    比较会得出相反结论 —— 那会让 1.10 被判定为"不比 1.9 新"而拒绝铺开。
    """
    if not isinstance(text, str):
        raise ValueError("版本号必须是字符串")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("版本号为空")

    parts: List[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))

    if not parts:
        raise ValueError("无法解析版本号: %r" % text)
    return tuple(parts)

log = logging.getLogger(__name__)

MIN_KEY_BITS = 2048
DEFAULT_KEY_BITS = 2048

#: 与 Agent 侧保持逐字节一致（契约测试会核对）
PKCS1_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)


class DerError(ValueError):
    """DER 结构不符合预期。"""


# --------------------------------------------------------------------------- #
# 极简 DER 解析
# --------------------------------------------------------------------------- #


def _read_tlv(data: bytes, offset: int) -> Tuple[int, bytes, int]:
    """读一个 TLV，返回 ``(tag, content, next_offset)``。"""
    if offset + 2 > len(data):
        raise DerError("数据在标签处截断")
    tag = data[offset]
    offset += 1
    first = data[offset]
    offset += 1

    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0:
            raise DerError("不支持不定长编码")
        if count > 4:
            raise DerError("长度字段过长")
        if offset + count > len(data):
            raise DerError("长度字段截断")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count

    if offset + length > len(data):
        raise DerError("内容截断：声明 %d 字节，实际只剩 %d" % (length, len(data) - offset))
    return tag, data[offset : offset + length], offset + length


def _parse_pkcs1_private(content: bytes) -> List[int]:
    """解析 RSAPrivateKey 结构，返回 9 个整数。"""
    integers: List[int] = []
    offset = 0
    while offset < len(content):
        tag, value, offset = _read_tlv(content, offset)
        if tag != 0x02:
            raise DerError("期望 INTEGER（0x02），实际是 0x%02x" % tag)
        integers.append(int.from_bytes(value, "big"))
    return integers


def parse_rsa_private_key_der(der: bytes) -> Dict[str, int]:
    """解析 DER 编码的 RSA 私钥，兼容 PKCS#1 与 PKCS#8。"""
    tag, content, end = _read_tlv(der, 0)
    if tag != 0x30:
        raise DerError("顶层应当是 SEQUENCE，实际是 0x%02x" % tag)
    if end != len(der):
        # 尾部有多余数据通常意味着传进来的是整个 PEM 文件或拼接了别的东西
        raise DerError("DER 末尾有 %d 字节多余数据" % (len(der) - end))

    # 探测内部结构来区分两种格式。判据必须是**完整的标签序列**：
    # PKCS#1 的内层是 9 个 INTEGER，PKCS#8 是 {INTEGER, SEQUENCE, OCTET STRING}。
    # 只看第一个 tag 是分不出来的 —— 两者都以 version INTEGER 开头。
    offset = 0
    tags: List[int] = []
    while offset < len(content):
        tag, _value, offset = _read_tlv(content, offset)
        tags.append(tag)

    if tags == [0x02, 0x30, 0x04]:
        # PKCS#8 PrivateKeyInfo：取出 privateKey OCTET STRING 再递归解析
        offset = 0
        _t, _v, offset = _read_tlv(content, offset)  # version
        _t, _v, offset = _read_tlv(content, offset)  # algorithm
        inner_tag, inner, _end = _read_tlv(content, offset)
        if inner_tag != 0x04:
            raise DerError("PKCS#8 的 privateKey 应当是 OCTET STRING")
        return parse_rsa_private_key_der(inner)

    if len(tags) != 9 or any(t != 0x02 for t in tags):
        raise DerError(
            "不是可识别的 RSA 私钥结构：内层标签序列为 %s"
            % " ".join("0x%02x" % t for t in tags[:12])
        )

    integers = _parse_pkcs1_private(content)
    if len(integers) != 9:
        raise DerError("RSA 私钥应当有 9 个整数，实际 %d 个" % len(integers))

    version, n, e, d, p, q, dp, dq, qinv = integers
    if version != 0:
        raise DerError("不支持的 RSA 私钥版本: %d" % version)
    if n <= 0 or d <= 0:
        raise DerError("私钥参数不合法")

    return {
        "n": n, "e": e, "d": d,
        "p": p, "q": q, "dp": dp, "dq": dq, "qinv": qinv,
    }


def _pem_to_der(text: str) -> bytes:
    """从 PEM 文本里取出 base64 主体并解码为 DER。"""
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("-----")
    ]
    if not lines:
        raise DerError("PEM 内容为空")
    payload = "".join(lines)
    padding = "=" * (-len(payload) % 4)
    try:
        return base64.b64decode(payload + padding)
    except Exception as exc:
        raise DerError("PEM base64 解码失败: %s" % exc)


# --------------------------------------------------------------------------- #
# 签名密钥
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SigningKey:
    n: int
    e: int
    d: int
    p: int = 0
    q: int = 0
    dp: int = 0
    dq: int = 0
    qinv: int = 0
    key_id: str = ""

    def __post_init__(self) -> None:
        if self.n.bit_length() < MIN_KEY_BITS:
            raise DerError(
                "私钥只有 %d 位，低于安全下限 %d 位" % (self.n.bit_length(), MIN_KEY_BITS)
            )
        if self.n.bit_length() % 8 != 0:
            # 非整数倍会让签名字节长度出现歧义
            raise DerError("模数位数必须是 8 的整数倍")

    @property
    def byte_length(self) -> int:
        return (self.n.bit_length() + 7) // 8

    @property
    def bits(self) -> int:
        return self.n.bit_length()

    def public_key_dict(self) -> Dict[str, str]:
        """下发给 Agent 的公钥形式。"""
        return {
            "alg": "RS256",
            "key_id": self.key_id,
            "n": _b64(self.n),
            "e": _b64(self.e),
        }

    def sign(self, message: bytes) -> bytes:
        """PKCS#1 v1.5 签名。

        有 p/q 时走 CRT（两次 1024 位模幂），比直接 ``pow(m, d, n)`` 快约 3 倍。
        签名是低频操作，但 CRT 顺手写来也不费事。
        """
        encoded = _expected_encoded_message(self.byte_length, message)
        m = int.from_bytes(encoded, "big")
        if m >= self.n:
            raise DerError("待签名内容超模数长度")

        if self.p and self.q and self.dp and self.dq and self.qinv:
            m1 = pow(m, self.dp, self.p)
            m2 = pow(m, self.dq, self.q)
            h = (self.qinv * (m1 - m2)) % self.p
            signature = m2 + h * self.q
        else:
            signature = pow(m, self.d, self.n)

        return signature.to_bytes(self.byte_length, "big")


def _b64(raw: int) -> str:
    length = (raw.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(raw.to_bytes(length, "big")).decode("ascii").rstrip("=")


def _expected_encoded_message(byte_length: int, message: bytes) -> bytes:
    digest_info = PKCS1_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = byte_length - len(digest_info) - 3
    if padding_length < 8:
        raise DerError("模数太短，容不下 PKCS#1 v1.5 填充")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info


def _key_id_for(n: int) -> str:
    """密钥指纹，用于日志与轮换时区分。取模数哈希的前 8 字节。"""
    return hashlib.sha256(n.to_bytes((n.bit_length() + 7) // 8, "big")).hexdigest()[:16]


def load_signing_key(path: Path) -> SigningKey:
    """从 PEM / DER / JSON 载入私钥。"""
    path = Path(path)
    if not path.is_file():
        raise DerError("私钥文件不存在: %s" % path)

    raw = path.read_bytes()

    # 我们自己的 JSON 缓存格式
    if raw.lstrip()[:1] == b"{":
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DerError("私钥 JSON 解析失败: %s" % exc)
        try:
            n = int.from_bytes(_unb64(str(data["n"])), "big")
        except (KeyError, ValueError) as exc:
            raise DerError("私钥 JSON 缺少字段: %s" % exc)
        return SigningKey(
            n=n,
            e=int.from_bytes(_unb64(str(data.get("e", ""))) or b"\x01\x00\x01", "big"),
            d=int.from_bytes(_unb64(str(data["d"])), "big"),
            key_id=str(data.get("key_id") or _key_id_for(n)),
        )

    if raw.lstrip().startswith(b"-----"):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DerError("PEM 不是 UTF-8 文本: %s" % exc)
        der = _pem_to_der(text)
    elif raw.lstrip()[:1] == b"\x30":
        der = raw
    else:
        raise DerError(
            "无法识别的私钥格式（既不是 PEM、DER，也不是 JSON）: %s" % path
        )

    params = parse_rsa_private_key_der(der)
    return SigningKey(
        n=params["n"], e=params["e"], d=params["d"],
        p=params.get("p", 0), q=params.get("q", 0),
        dp=params.get("dp", 0), dq=params.get("dq", 0), qinv=params.get("qinv", 0),
        key_id=_key_id_for(params["n"]),
    )


def _unb64(text: str) -> bytes:
    cleaned = text.strip().replace("+", "-").replace("/", "_")
    padding = "=" * (-len(cleaned) % 4)
    return base64.urlsafe_b64decode(cleaned + padding)


# --------------------------------------------------------------------------- #
# 密钥生成
# --------------------------------------------------------------------------- #


def openssl_available() -> Optional[str]:
    """返回 openssl 可执行文件路径，没有则 None。"""
    return shutil.which("openssl")


def generate_keypair(out_path: Path, bits: int = DEFAULT_KEY_BITS) -> SigningKey:
    """调用 ``openssl`` 生成 RSA 私钥并落盘为 PEM。

    刻意不自己实现素数生成：Miller-Rabin 与随机素数搜索是好写但极易写错的
    密码学代码，而这一步是**一次性**的管理操作，依赖 openssl 完全可接受
    （Linux 上到处都是）。
    """
    if bits < MIN_KEY_BITS:
        raise ValueError("密钥长度不得低于 %d 位" % MIN_KEY_BITS)

    openssl = openssl_available()
    if not openssl:
        raise RuntimeError(
            "找不到 openssl。请先安装（Debian/Ubuntu: apt install openssl），"
            "或手工生成密钥后放到指定路径。"
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        raise FileExistsError("私钥已存在，拒绝覆盖: %s" % out_path)

    # 先写临时文件再原子改名：避免生成到一半失败留下一个半截私钥
    fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent), suffix=".pem.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        subprocess.run(
            [openssl, "genrsa", "-out", str(tmp_path), str(bits)],
            check=True,
            capture_output=True,
        )
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, out_path)
    except subprocess.CalledProcessError as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            "openssl 生成密钥失败: %s" % (exc.stderr or b"").decode("utf-8", "replace")
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return load_signing_key(out_path)


def write_public_key_json(path: Path, key: SigningKey) -> None:
    """把公钥写成 Agent 读得懂的 JSON（``release-key.pub.json``）。

    **这里是这个文件格式的唯一定义。** 它是 Agent 的信任锚：写歪一个字段名，
    表现是每台机器都拒绝升级，而服务端这边一切正常 —— 而它会同时在两处被写
    （``syncoj-server init``/``genkey``，以及测试夹具），两处各写一遍迟早会漂。

    公钥不是秘密，0644 就好；但行尾必须是 LF：它要在开发机和服务器之间搬运、
    被 sha256 比对，字节不同会让人怀疑"是不是换了密钥"。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(key.public_key_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
