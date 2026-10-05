"""发布包签名测试。

**openssl 交叉验证是本文件的重点。** 手写密码学代码的正确性无法通过自测证明 ——
自签自验永远能过。唯一可信的证据是：拿一个独立实现（openssl）签出来的东西，
我们的代码能验；反过来我们的签名与它逐字节相同。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from syncoj_server.services.signing import (
    DEFAULT_KEY_BITS,
    DerError,
    SigningKey,
    generate_keypair,
    load_signing_key,
    openssl_available,
)
from syncoj_server.services.signing import (
    PKCS1_SHA256_DIGEST_INFO_PREFIX as SERVER_PREFIX,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from syncoj_agent.rsa import (  # noqa: E402
    PKCS1_SHA256_DIGEST_INFO_PREFIX as AGENT_PREFIX,
)
from syncoj_agent.rsa import RSAPublicKey, SignatureError, verify_pkcs1v15_sha256  # noqa: E402

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")


@pytest.fixture(scope="module")
def key_dir() -> Iterator[Path]:
    """模块级临时目录。

    刻意不用 pytest 的 ``tmp_path_factory``：我们把 tmpdir 插件禁掉了，因为它
    底层依赖的 ``tempfile`` 在受限环境下会因 chmod 失败而留下删不掉的残留目录，
    污染 git 工作区。
    """
    root = REPO_ROOT / ".pytest-tmp" / ("signing-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def primary(key_dir: Path) -> Tuple[SigningKey, Path]:
    """模块级主密钥。

    必须**只生成一次**并被所有用例共用：如果 ``keypair`` 和 ``key_pem`` 各自
    生成一把，用 openssl 签、用我们的代码验就会因为不是同一把密钥而失败 ——
    那种失败看起来像实现 bug，实际是夹具 bug，很浪费时间。
    """
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    target = key_dir / "primary.pem"
    return generate_keypair(target, bits=DEFAULT_KEY_BITS), target


@pytest.fixture(scope="module")
def keypair(primary) -> SigningKey:
    return primary[0]


@pytest.fixture(scope="module")
def key_pem(primary) -> Path:
    return primary[1]


def public_of(key: SigningKey) -> RSAPublicKey:
    return RSAPublicKey.from_dict(key.public_key_dict())


def openssl_sign(pem: Path, message: bytes, out: Path) -> bytes:
    subprocess.run(
        [openssl_available(), "dgst", "-sha256", "-sign", str(pem), "-out", str(out)],
        input=message, check=True, capture_output=True,
    )
    return out.read_bytes()


# --------------------------------------------------------------------------- #
# 两端常量必须一致，否则一签一验必然对不上
# --------------------------------------------------------------------------- #


def test_digest_info_prefix_matches_agent() -> None:
    assert SERVER_PREFIX == AGENT_PREFIX
    assert len(SERVER_PREFIX) == 19
    assert SERVER_PREFIX.startswith(bytes.fromhex("3031300d0609608648016503040201"))


def test_expected_encoding_matches_agent_reconstruction() -> None:
    """两端独立构造的 EM 必须逐字节一致。"""
    from syncoj_server.services.signing import _expected_encoded_message

    message = b"payload"
    length = 256
    server_em = _expected_encoded_message(length, message)

    digest_info = AGENT_PREFIX + hashlib.sha256(message).digest()
    agent_em = b"\x00\x01" + b"\xff" * (length - len(digest_info) - 3) + b"\x00" + digest_info

    assert server_em == agent_em
    assert len(server_em) == length


# --------------------------------------------------------------------------- #
# 密钥生成与载入
# --------------------------------------------------------------------------- #


@requires_openssl
def test_generated_key_is_well_formed(keypair: SigningKey) -> None:
    assert keypair.bits == DEFAULT_KEY_BITS
    assert keypair.e == 65537
    assert keypair.key_id
    assert keypair.p and keypair.q and keypair.dp and keypair.dq and keypair.qinv


@requires_openssl
def test_generate_refuses_to_overwrite(key_dir: Path) -> None:
    """误覆盖私钥会让所有已发布的签名失效，必须拒绝。"""
    existing = key_dir / "exists.pem"
    existing.write_text("占位", encoding="utf-8")
    with pytest.raises(FileExistsError):
        generate_keypair(existing)


def _to_pkcs1_der(openssl: str, pem: Path, out: Path) -> None:
    """把 PEM 转成 **PKCS#1** DER，不管这台机器上是哪个 openssl。

    ``-traditional`` 是 OpenSSL **3.0** 才有的开关：3.0 起 ``openssl rsa`` 默认
    输出 PKCS#8，所以要显式要求老格式；而 1.x 里没有这个开关，它的默认输**出就是**
    PKCS#1（实测 Git 自带的 1.1.1q 会直接报 ``rsa: Unrecognized flag traditional``）。

    所以按能力选参数，而不是假定一个版本。这也是本测试的原意 ——
    "换个 openssl 版本就崩是不可接受的"。
    """
    base = [openssl, "rsa", "-in", str(pem)]
    attempt = subprocess.run(
        base + ["-traditional", "-outform", "DER", "-out", str(out)],
        capture_output=True,
    )
    if attempt.returncode == 0:
        return
    subprocess.run(base + ["-outform", "DER", "-out", str(out)], check=True, capture_output=True)


def _looks_like_pkcs1_der(data: bytes) -> bool:
    """这个 DER 是不是真的 PKCS#1 ``RSAPrivateKey``。

    ``RSAPrivateKey`` 与 PKCS#8 ``PrivateKeyInfo`` 都以 SEQUENCE 开头，区别在
    紧随其后的东西：前者是 ``INTEGER version, INTEGER modulus``（``02 01 00 02``），
    后者是 ``INTEGER version, SEQUENCE(AlgorithmIdentifier)``。

    必须真的验一下：否则哪天某个 openssl 版本又改了默认值，这个测试会**安静地
    把 PKCS#8 读两遍**，而它存在的全部意义就是证明两种格式都读得了。
    """
    head = data[:12]
    # 长度头可能是 4 字节（30 82 LL LL）或 3 字节（30 81 LL）
    return head[4:8] == b"\x02\x01\x00\x02" or head[3:7] == b"\x02\x01\x00\x02"


@requires_openssl
def test_loads_pkcs1_and_pkcs8(keypair: SigningKey, key_pem: Path, key_dir: Path) -> None:
    """OpenSSL 3.0 起 ``openssl rsa`` 默认输出 PKCS#8，老版本输出 PKCS#1 ——
    换个 openssl 版本就崩是不可接受的，两种都必须能读。"""
    openssl = openssl_available()

    der = key_dir / "pkcs1.der"
    _to_pkcs1_der(openssl, key_pem, der)
    body = der.read_bytes()
    assert _looks_like_pkcs1_der(body), (
        "这台 openssl 生成的并不是 PKCS#1，测试会在不知情的情况下把 PKCS#8 读两遍"
    )
    assert load_signing_key(der).n == keypair.n

    p8 = key_dir / "pkcs8.pem"
    subprocess.run(
        [openssl, "pkcs8", "-topk8", "-nocrypt", "-in", str(key_pem), "-out", str(p8)],
        check=True, capture_output=True,
    )
    assert load_signing_key(p8).n == keypair.n

    assert load_signing_key(key_pem).n == keypair.n


def test_load_rejects_garbage(workdir: Path) -> None:
    bad = workdir / "garbage.der"
    bad.write_bytes(b"\x30\x05\x02\x03\x01\x02\x03junk")
    with pytest.raises(DerError):
        load_signing_key(bad)


def test_load_rejects_missing_file(workdir: Path) -> None:
    with pytest.raises(DerError):
        load_signing_key(workdir / "nope.pem")


def test_load_rejects_truncated_der(workdir: Path) -> None:
    truncated = workdir / "short.der"
    truncated.write_bytes(b"\x30\x82\x01\x00\x02")
    with pytest.raises(DerError):
        load_signing_key(truncated)


def test_weak_key_is_rejected() -> None:
    with pytest.raises(SignatureError):
        RSAPublicKey(n=3, e=65537)


def test_even_exponent_is_rejected() -> None:
    with pytest.raises(SignatureError):
        RSAPublicKey(n=(1 << 2048) + 1, e=4)


def test_public_key_rejects_unknown_algorithm() -> None:
    with pytest.raises(SignatureError):
        RSAPublicKey.from_dict({"alg": "ES256", "n": "AQAB", "e": "AQAB"})


def test_public_key_dict_roundtrip(keypair: SigningKey) -> None:
    restored = RSAPublicKey.from_dict(keypair.public_key_dict())
    assert restored.n == keypair.n
    assert restored.e == keypair.e
    assert restored.key_id == keypair.key_id


# --------------------------------------------------------------------------- #
# 签名 / 验证
# --------------------------------------------------------------------------- #


@requires_openssl
def test_sign_verify_roundtrip(keypair: SigningKey) -> None:
    message = b"release bundle v1.2.0"
    signature = keypair.sign(message)
    assert len(signature) == keypair.byte_length
    assert verify_pkcs1v15_sha256(public_of(keypair), message, signature)


@requires_openssl
def test_signature_is_byte_identical_to_openssl(
    keypair: SigningKey, key_pem: Path, key_dir: Path
) -> None:
    """PKCS#1 v1.5 是确定性签名 —— 实现正确的话必须逐字节相同。

    这比"能互相验证"更强：任何填充差异都会立刻暴露，而不是碰巧还能验过。
    """
    message = b"deterministic payload"
    ours = keypair.sign(message)
    theirs = openssl_sign(key_pem, message, key_dir / "det.sig")
    assert ours == theirs


@requires_openssl
def test_verifies_openssl_signatures(keypair: SigningKey, key_pem: Path, key_dir: Path) -> None:
    """**本文件最重要的一条**：独立实现签出来的东西，我们的验证代码要能认。"""
    public = public_of(keypair)
    messages = [
        b"",
        b"a",
        b"x" * 5000,
        "中文内容与 emoji 🎯".encode("utf-8"),
        bytes(range(256)),
    ]
    for index, message in enumerate(messages):
        signature = openssl_sign(key_pem, message, key_dir / ("msg%d.sig" % index))
        assert verify_pkcs1v15_sha256(public, message, signature), (
            "openssl 对 %d 字节消息的签名未被接受" % len(message)
        )


@requires_openssl
def test_rejects_modified_message(keypair: SigningKey) -> None:
    assert not verify_pkcs1v15_sha256(public_of(keypair), b"modified", keypair.sign(b"original"))


@requires_openssl
def test_rejects_modified_signature(keypair: SigningKey) -> None:
    signature = bytearray(keypair.sign(b"payload"))
    signature[5] ^= 0x01
    assert not verify_pkcs1v15_sha256(public_of(keypair), b"payload", bytes(signature))


@requires_openssl
def test_rejects_truncated_signature(keypair: SigningKey) -> None:
    signature = keypair.sign(b"payload")
    assert not verify_pkcs1v15_sha256(public_of(keypair), b"payload", signature[:-1])


@requires_openssl
def test_rejects_zero_padded_signature(keypair: SigningKey) -> None:
    """左补零是经典的历史漏洞：宽松实现会把它当成合法签名。"""
    signature = keypair.sign(b"payload")
    assert not verify_pkcs1v15_sha256(public_of(keypair), b"payload", b"\x00" + signature)


@requires_openssl
def test_rejects_loose_padding_forgery(keypair: SigningKey) -> None:
    """Bleichenbacher 2006 式伪造。

    若验证器是"从右侧提取 digest 再比较 hash"，攻击者可以构造
    ``00 01 FF ... FF 00 <DigestInfo> <垃圾>`` —— 整数解析忽略尾部垃圾，
    伪造就通过了。我们的实现是**整体重建后恒定时间比较**，必须拒绝。
    """
    from syncoj_server.services.signing import _expected_encoded_message

    public = public_of(keypair)
    message = b"legitimate"
    length = public.byte_length

    strict = _expected_encoded_message(length, message)
    loose = strict[:-10] + b"\x00" * 10
    forged = pow(int.from_bytes(loose, "big"), keypair.d, keypair.n).to_bytes(length, "big")

    assert not verify_pkcs1v15_sha256(public, message, forged)


@requires_openssl
def test_rejects_signature_from_wrong_key(keypair: SigningKey, key_dir: Path) -> None:
    other, _path = generate_keypair(key_dir / "other.pem", bits=DEFAULT_KEY_BITS), None
    signature = keypair.sign(b"payload")
    assert not verify_pkcs1v15_sha256(public_of(other), b"payload", signature)


@requires_openssl
def test_verify_never_raises_on_bad_input(keypair: SigningKey) -> None:
    """验证接口只返回 True/False —— 崩溃会拖垮 Agent 主循环。"""
    public = public_of(keypair)
    length = public.byte_length
    for bad in (b"", b"\x00" * length, b"\xff" * length, b"\xff" * (length + 10), None, 12345):
        assert verify_pkcs1v15_sha256(public, b"m", bad) is False  # type: ignore[arg-type]
    assert verify_pkcs1v15_sha256(public, None, b"\x00" * length) is False  # type: ignore[arg-type]
