"""InfoZIP 传统加密（ZipCrypto，``zip -P`` 那一套）的读写。

为什么是 ZipCrypto 而不是 AES-256
---------------------------------
学生的机器上是 **Archive Manager（GNOME file-roller）**，它只认传统加密：
AES-256 的 zip 它打不开（除非另外装了 p7zip），而 ZipCrypto 的 zip 会弹框要密码。
需求是"学生能打开"，不是"密码学上漂亮"，所以这里只能选前者。

**必须诚实：ZipCrypto 是弱加密。**
它的密钥流由 CRC-32 与一个 16 位非线性函数产生，已知明文攻击可以恢复内部状态，
进而解出同包其它成员；测试数据（题面、样例）的结构又几乎全部已知。
所以这个功能的正确定位是"挡得住随手翻看，挡不住有心人"，**不是**信息安全手段。
页面上与错误文案里都不许出现"安全加密"这类说法 —— 它们会给人一种它不提供的保证。

读为什么用标准库
----------------
``zipfile.ZipFile.open(name, pwd=...)`` 原生支持读 ZipCrypto 成员（解压 + 校验
CRC 都做了），所以"改密码"= 用旧密码读出来 + 用新密码重写；"打密码"= 明文读出来
+ 加密重写。这里**不重复实现读**，读的那一半交给标准库 —— 也就是说，我们写出去的
东西能不能读回来，是由一个**独立实现**判定的，而不是自己给自己作证。

写为什么自己实现
----------------
标准库能读加密 zip，但**写不了**加密 zip（``ZipFile(..., mode="w")`` 没有密码
参数，``PyZipFile`` 那条路也一样）。所以本地头与加密头这一段必须自己拼。

实现要点
--------
* **加密头**：每个成员独立一套密钥（从密码重新初始化），先写 12 字节随机头，
  前 11 字节随机，第 12 字节是校验字节 —— 用 **CRC 的高字节**（``crc >> 24``）。
  这条要求成员写入时把 ``flag_bits`` 的 bit 3（data descriptor）清掉：若置了
  bit 3，校验字节就得改成 DOS 时间的高字节。我们本地头里直接写 CRC 与长度，
  所以走 CRC 那一套。
* **密钥流**：``key0/key1/key2`` 三个 32 位状态；每字节
  ``cipher = plain ^ stream_byte()``，然后**用明文字节**更新状态。
* **压缩方式必须保留**：源成员是 store 就写 store，是 deflate 就用 zlib 的
  **裸流**（``wbits=-15``）重压一遍。把 store 当成 deflate（或反过来）会让
  reader 解出一坨垃圾，而 CRC 报错时看起来像"密码不对"。
* **目录项、时间戳、注释、属性尽量原样保留**：目录项就是"文件名为空、以 / 结尾"
  的普通成员，按同一套流程重写；DOS 时间、external_attr、成员注释、整包注释、
  UTF-8 名字标志位都从原件带过来。
* **hash 稳定性**：加密头是随机的，所以同一个包用同一个密码重写两次，字节是
  不一样的（这是 PKZIP 的设计，不是 bug）。接口的"同名同内容复用"因此只看
  ``password.txt`` 的正文，不看 zip 的 sha。

这个模块零依赖（只用标准库），并且可以在 Python 3.8 上跑。
"""

from __future__ import annotations

import os
import secrets
import struct
import time
import zlib
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from typing import BinaryIO, List, Optional

__all__ = [
    "ZipCryptoError",
    "NotAZipError",
    "PasswordRequiredError",
    "BadPasswordError",
    "UnsupportedCompressionError",
    "PASSWORD_ALPHABET",
    "PASSWORD_LENGTH",
    "generate_password",
    "pack_member",
    "probe_encrypted",
    "rewrite_zip",
]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class ZipCryptoError(Exception):
    """本模块的基类：调用方 catch 它就能把这一类失败翻译成一条人话。"""


class NotAZipError(ZipCryptoError):
    """给进来的字节不是能识别的 zip。"""


class PasswordRequiredError(ZipCryptoError):
    """包里有加密成员，但调用方没给旧密码。"""


class BadPasswordError(ZipCryptoError):
    """旧密码不对（或解出来的字节 CRC 对不上）。"""


class UnsupportedCompressionError(ZipCryptoError):
    """成员的压缩方式不是 store / deflate，重新打包会改变它的语义。"""


# --------------------------------------------------------------------------- #
# 随机密码
# --------------------------------------------------------------------------- #

#: 随机密码长度。12 位足够挡住"随手翻看"，也短到能抄在纸上。
PASSWORD_LENGTH = 12

#: 随机密码字符集：大小写字母 + 数字，但**去掉易混字符** ``0 O 1 l I``。
#:
#: 这个功能的使用现场是"教师念/学生抄"，不是"机器之间传输"。把 0 和 O、1 和 l
#: 混在一起，抄错一位得到的不是"密码错"而是一个很难自己发现是哪一位错了的包。
PASSWORD_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 大写，去掉 I 与 O
    "abcdefghijkmnopqrstuvwxyz"  # 小写，去掉 l
    "23456789"  # 数字，去掉 0 与 1
)


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """生成一个随机密码。

    用 ``secrets``（``SystemRandom``）而不是 ``random``：后者是可预测的
    Mersenne Twister，而这里的密码会变成学生机器上一份文件的打开口令 ——
    在同一场考试里连续生成两次的模式是能被推出来的。
    """
    if length <= 0:
        raise ValueError("密码长度必须为正数")
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_MASK_ENCRYPTED = 0x0001
_MASK_DATA_DESCRIPTOR = 0x0008
#: bit 6 = "strong encryption"。我们只写传统加密，必须把它清掉，否则 reader
#: 会去找一个根本不存在的 AES 头。
_MASK_STRONG_ENCRYPTION = 0x0040
_MASK_UTF8_NAME = 0x0800

_LOCAL_FMT = "<4sHHHHHIIIHH"  # 30 字节，不含文件名/附加区
_CENTRAL_FMT = "<4sHHHHHHIIIHHHHHII"  # 46 字节，不含文件名/附加区/注释
_EOCD_FMT = "<4sHHHHIIH"  # 22 字节，不含整包注释
_PATCH_FMT = "<III"  # 本地头里 CRC / 压缩后大小 / 原始大小 三个字段

_CHUNK = 1024 * 1024
#: 每个加密成员前面那 12 字节随机头（也叫"加密头"）。它**计入**压缩后大小。
_ENCRYPTION_HEADER_SIZE = 12
_ZIP64_LIMIT = 0xFFFFFFFF
_MAX_ENTRIES = 0xFFFF


def _build_crc_table() -> List[int]:
    """ZIP 用的 CRC-32 表（与 ``zlib.crc32`` 同一套多项式）。

    自己查表而不是逐字节调 ``zlib.crc32``：加密每个字节都要更新一次状态，
    调 C 函数的话每次还要构造一个 bytes 对象；一张表能少掉几十万次临时分配。
    """
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            if value & 1:
                value = 0xEDB88320 ^ (value >> 1)
            else:
                value = value >> 1
        table.append(value & 0xFFFFFFFF)
    return table


_CRC_TABLE = _build_crc_table()


class _ZipCrypto:
    """一个成员的 ZipCrypto 加密器。

    每个成员**重新按密码初始化**一套密钥（PKZIP 的规矩），所以同一个密码在不同
    成员上的密钥流不同 —— 不要跨成员复用同一个实例。
    """

    __slots__ = ("_key0", "_key1", "_key2")

    def __init__(self, password: bytes) -> None:
        self._key0 = 0x12345678
        self._key1 = 0x23456789
        self._key2 = 0x34567890
        for byte in password:
            self._update(byte)

    def _update(self, byte: int) -> None:
        table = _CRC_TABLE
        key0 = table[(self._key0 ^ byte) & 0xFF] ^ (self._key0 >> 8)
        self._key0 = key0 & 0xFFFFFFFF
        key1 = (self._key1 + (key0 & 0xFF)) & 0xFFFFFFFF
        key1 = (key1 * 134775813 + 1) & 0xFFFFFFFF
        self._key1 = key1
        self._key2 = (table[(self._key2 ^ (key1 >> 24)) & 0xFF] ^ (self._key2 >> 8)) & 0xFFFFFFFF

    def encrypt(self, data: bytes) -> bytearray:
        """把一段明文加密成密文，并把状态推进到这段之后。

        流式调用是安全的：状态跨调用保持，所以可以一个 chunk 一个 chunk 地喂。
        """
        out = bytearray(len(data))
        table = _CRC_TABLE
        key0 = self._key0
        key1 = self._key1
        key2 = self._key2
        for index, plain in enumerate(data):
            key2_or_2 = (key2 | 2) & 0xFFFF
            out[index] = plain ^ (((key2_or_2 * (key2_or_2 ^ 1)) >> 8) & 0xFF)
            key0 = table[(key0 ^ plain) & 0xFF] ^ (key0 >> 8)
            key1 = (key1 + (key0 & 0xFF)) & 0xFFFFFFFF
            key1 = (key1 * 134775813 + 1) & 0xFFFFFFFF
            key2 = table[(key2 ^ (key1 >> 24)) & 0xFF] ^ (key2 >> 8)
        self._key0 = key0 & 0xFFFFFFFF
        self._key1 = key1
        self._key2 = key2 & 0xFFFFFFFF
        return out


class _Compressor:
    """按成员原本的压缩方式压一遍。

    deflate 用 **裸流**（``wbits=-15``）：zip 里没有 zlib 头，写进去会多出两个
    字节的魔数，reader 会当成压缩数据的一部分。
    """

    def __init__(self, method: int) -> None:
        if method == zipfile.ZIP_STORED:
            self._obj: Optional[object] = None
        elif method == zipfile.ZIP_DEFLATED:
            self._obj = zlib.compressobj(
                zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15
            )
        else:
            raise UnsupportedCompressionError(
                "成员用的是压缩方式 %d，这里只会原样保留 store / deflate" % method
            )

    def feed(self, data: bytes) -> bytes:
        if self._obj is None:
            return data
        return self._obj.compress(data)  # type: ignore[attr-defined]

    def finish(self) -> bytes:
        if self._obj is None:
            return b""
        return self._obj.flush()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 只读探测
# --------------------------------------------------------------------------- #


def _open_archive(source: BinaryIO) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise NotAZipError("不是能识别的 zip 文件：%s" % exc)


def probe_encrypted(source: BinaryIO) -> bool:
    """这个 zip 里有没有加密成员 —— 只看标志位，**不解压、不解密任何成员**。

    走标准库的目录解析：它读的是 zip 末尾的中央目录（zip 自己的索引），逐条看
    ``flag_bits`` 的第 0 位。教师上传的题面可能几百 MB，这个判据不能去读成员正文，
    否则一次列表点击就会把整包从磁盘上拖一遍。

    ``source`` 必须是可 seek 的二进制流。
    """
    with _open_archive(source) as archive:
        return any(
            bool(info.flag_bits & _MASK_ENCRYPTED) for info in archive.infolist()
        )


# --------------------------------------------------------------------------- #
# 重新打包
# --------------------------------------------------------------------------- #


@dataclass
class _WrittenEntry:
    """已经写出去的一个成员，攒够中央目录要用的字段。"""

    info: zipfile.ZipInfo
    name: bytes
    extra: bytes
    comment: bytes
    flags: int
    method: int
    dos_time: int
    dos_date: int
    crc: int
    comp_size: int
    uncomp_size: int
    local_offset: int


def _encode_name(info: zipfile.ZipInfo) -> bytes:
    """把成员名还原成字节。

    设了 UTF-8 标志位（bit 11）的按 UTF-8 编；否则 zipfile 读进来时用的是
    cp437，编码回去是逐字节可逆的。编码不回去时退回 UTF-8 并**不改标志位**，
    这种包本来就少见，宁可让它是现在的样子，也不要静默改掉名字的编码约定。
    """
    if info.flag_bits & _MASK_UTF8_NAME:
        return info.filename.encode("utf-8")
    try:
        return info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename.encode("utf-8")


def _dos_datetime(date_time) -> tuple:
    """``(Y, M, D, h, m, s)`` → ``(dos_time, dos_date)``。

    zip 的 DOS 时间从 1980 年起算，秒只有 2 秒精度 —— 和标准库写出的是同一套
    规则，所以"重写一遍"不会让时间戳漂移。
    """
    year, month, day, hour, minute, second = date_time
    if year < 1980:
        year = 1980
    dos_date = ((year - 1980) << 9) | (month << 5) | day
    dos_time = (hour << 11) | (minute << 5) | (second // 2)
    return dos_time & 0xFFFF, dos_date & 0xFFFF


def rewrite_zip(
    source: BinaryIO,
    dest: BinaryIO,
    *,
    new_password: bytes,
    old_password: Optional[bytes] = None,
) -> None:
    """把 ``source`` 里的 zip 用 ``new_password`` 重新写进 ``dest``。

    ``dest`` 必须是**可 seek 的二进制流**：本地头里的 CRC/长度要等数据写完才能
    知道（我们是边读边压边加密的，没有整包读进内存），所以先占位写完再回填。

    源成员按原来的压缩方式（store / deflate）与元数据重写；目录项、时间戳、注释、
    UTF-8 名字标志位都保留。已经在源里加密的成员必须给 ``old_password``。
    """
    if not new_password:
        raise ValueError("new_password 不能为空")

    with _open_archive(source) as archive:
        entries: List[_WrittenEntry] = []
        for info in archive.infolist():
            entries.append(
                _write_member(archive, info, dest, old_password, new_password)
            )
        central_offset = dest.tell()
        for entry in entries:
            _write_central(entry, dest)
        central_size = dest.tell() - central_offset
        _write_eocd(dest, len(entries), central_size, central_offset, archive.comment)


def pack_member(
    source: BinaryIO,
    dest: BinaryIO,
    *,
    member_name: str,
    password: Optional[bytes] = None,
    method: int = zipfile.ZIP_DEFLATED,
    date_time=None,
) -> None:
    """把一段原始字节（pdf、样例、任何**不是 zip** 的文件）包成一个单成员 zip。

    ``source`` 必须是可 seek 的二进制流。为什么读两遍：ZipCrypto 的加密头里那个
    校验字节写在成员数据**之前**，而它是 CRC 的高字节 —— 不知道 CRC 就写不出加密
    头，所以只能先扫一遍把 CRC 算出来，再回头压缩+加密。想一遍过就得改用 data
    descriptor，那是另一套本地头（见模块 docstring）。

    ``member_name`` 一律按 UTF-8 编码并置上 bit 11 标志位：教师上传的是
    ``题面.pdf`` 这种名字，zip 默认的 cp437 装不下它。

    ``password`` 为空时写出一个**明文** zip —— "打包"与"加密码"本来就是两件事，
    调用方要哪一件由它自己决定（接口层永远不会给出空密码，见 ``api/admin.py``）。
    """
    name = member_name.encode("utf-8")
    when = tuple(date_time) if date_time else time.localtime()[:6]
    dos_time, dos_date = _dos_datetime(when)

    crc = 0
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            break
        crc = zlib.crc32(chunk, crc)
    crc &= 0xFFFFFFFF
    source.seek(0)

    info = zipfile.ZipInfo(member_name, date_time=when)
    info.compress_type = method
    info.extract_version = 20

    entry = _write_entry(
        dest,
        name=name,
        extra=b"",
        comment=b"",
        flags=_MASK_UTF8_NAME,
        method=method,
        dos_time=dos_time,
        dos_date=dos_date,
        crc=crc,
        reader=lambda: nullcontext(source),
        new_password=password,
        info=info,
    )
    central_offset = dest.tell()
    _write_central(entry, dest)
    central_size = dest.tell() - central_offset
    _write_eocd(dest, 1, central_size, central_offset, b"")


def _write_entry(
    dest: BinaryIO,
    *,
    name: bytes,
    extra: bytes,
    comment: bytes,
    flags: int,
    method: int,
    dos_time: int,
    dos_date: int,
    crc: int,
    reader,
    new_password: Optional[bytes],
    info: zipfile.ZipInfo,
) -> _WrittenEntry:
    """写一个成员（本地头 + 名字/附加区 + 数据 + 回填），返回中央目录要用的字段。

    这一层**不知道明文是从哪来的**：改密码那条路传进来的是 ``archive.open`` 打开的
    旧成员，打包那条路传进来的是原始文件的字节流。两条路共享同一份"加密头、CRC、
    压缩后大小含 12 字节头"的实现 —— 这些坑踩过一次就够了（见模块 docstring）。

    ``reader`` 是**工厂**而不是现成的流：调用它拿到的对象要支持 ``with``，这样
    退出时能保证成员被关掉（改密码那条路打开的正是 zip 成员）。

    ``crc`` 必须在写数据**之前**就给出来：ZipCrypto 加密头里的校验字节是它的高
    字节，而校验字节写在成员数据前面。所以两条路都先扫一遍源。
    """
    # 清掉 data descriptor（我们把 CRC/长度写进了本地头，见模块 docstring）与
    # strong encryption（我们写的是传统加密）；加密位跟着有没有密码走。
    flags = (flags & ~_MASK_DATA_DESCRIPTOR) & ~_MASK_STRONG_ENCRYPTION
    if new_password:
        flags |= _MASK_ENCRYPTED
    else:
        flags &= ~_MASK_ENCRYPTED
    flags &= 0xFFFF

    local_offset = dest.tell()
    if local_offset > _ZIP64_LIMIT:  # pragma: no cover - 2 GB 上限下走不到
        raise ZipCryptoError("包太大，超过 4 GB 的 zip 不支持重新打包")
    dest.write(
        struct.pack(
            _LOCAL_FMT,
            b"PK\x03\x04",
            info.extract_version or 20,
            flags,
            method,
            dos_time,
            dos_date,
            0,  # crc 占位，数据写完回填
            0,  # 压缩后大小占位
            0,  # 原始大小占位
            len(name),
            len(extra),
        )
    )
    dest.write(name)
    dest.write(extra)

    # 加密头：12 字节，第 12 字节是校验字节。本地头里写的是 CRC，所以用 CRC 高字节
    # （bit 3 已经清掉了，reader 也会来找 CRC 高字节）。没有密码时这一段整个不写。
    crypto = _ZipCrypto(new_password) if new_password else None
    if crypto is not None:
        check_byte = (crc >> 24) & 0xFF
        dest.write(crypto.encrypt(os.urandom(11) + bytes((check_byte,))))

    compressor = _Compressor(method)
    actual_crc = 0
    payload_size = 0
    uncomp_size = 0
    with reader() as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            uncomp_size += len(chunk)
            actual_crc = zlib.crc32(chunk, actual_crc)
            packed = compressor.feed(chunk)
            if packed:
                payload_size += len(packed)
                dest.write(crypto.encrypt(packed) if crypto is not None else packed)
    tail = compressor.finish()
    if tail:
        payload_size += len(tail)
        dest.write(crypto.encrypt(tail) if crypto is not None else tail)

    actual_crc &= 0xFFFFFFFF
    if actual_crc != crc:  # pragma: no cover - 改密码那条路标准库已经校验过一遍
        # 打包那条路的源要读两遍（第一遍算 CRC），两遍不一致说明源在被改；
        # 与其写出一个自相矛盾的包，不如在这里停住。
        raise ZipCryptoError("「%s」的 CRC 与第一遍读出来的不一致" % info.filename)

    # **加密成员的大小要把 12 字节加密头算进去**（APPNOTE：压缩后大小包含加密头）。
    # 标准库读的时候正是这么用的：它先把 compress_size 减掉 12 再当密文长度，
    # 漏掉这 12 字节的话密文会被截短一截，解出来的数据 CRC 对不上 —— 报出来是
    # "Bad CRC-32"，看起来像密码错，实际是长度写错了。
    comp_size = payload_size + (_ENCRYPTION_HEADER_SIZE if crypto is not None else 0)
    if comp_size > _ZIP64_LIMIT or uncomp_size > _ZIP64_LIMIT:  # pragma: no cover
        raise ZipCryptoError("成员「%s」超过 4 GB，不支持重新打包" % info.filename)

    end = dest.tell()
    dest.seek(local_offset + 14)
    dest.write(struct.pack(_PATCH_FMT, actual_crc, comp_size, uncomp_size))
    dest.seek(end)

    return _WrittenEntry(
        info=info,
        name=name,
        extra=extra,
        comment=comment,
        flags=flags,
        method=method,
        dos_time=dos_time,
        dos_date=dos_date,
        crc=actual_crc,
        comp_size=comp_size,
        uncomp_size=uncomp_size,
        local_offset=local_offset,
    )


def _write_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    dest: BinaryIO,
    old_password: Optional[bytes],
    new_password: bytes,
) -> _WrittenEntry:
    encrypted = bool(info.flag_bits & _MASK_ENCRYPTED)
    if encrypted and not old_password:
        raise PasswordRequiredError(
            "「%s」在原来的包里就是加密的，必须给旧密码" % info.filename
        )

    def reader():
        return archive.open(info, "r", old_password if encrypted else None)

    name = _encode_name(info)
    # 成员注释在标准库里是 bytes；万一将来变成 str 也不至于把包写坏
    comment = info.comment or b""
    if isinstance(comment, str):
        comment = comment.encode("utf-8")
    dos_time, dos_date = _dos_datetime(info.date_time)

    try:
        return _write_entry(
            dest,
            name=name,
            extra=info.extra or b"",
            comment=comment,
            flags=info.flag_bits,
            method=info.compress_type,
            dos_time=dos_time,
            dos_date=dos_date,
            crc=info.CRC & 0xFFFFFFFF,
            reader=reader,
            new_password=new_password,
            info=info,
        )
    except (RuntimeError, zipfile.BadZipFile) as exc:
        # 新版 Python 在加密头那一关就抛 RuntimeError("Bad password")；
        # 老版本（含 3.8）要读到底、CRC 对不上才抛 BadZipFile。两种都是"密码不对"。
        raise BadPasswordError(
            "「%s」的旧密码不对（%s）" % (info.filename, exc)
        )
    except zlib.error as exc:
        # Python 3.8 的 zipfile **不校验加密头**，密码不对时会把解密出来的垃圾
        # 直接喂给 zlib，于是这里先炸的是 zlib.error。不接住它的话，3.8 上
        # "旧密码错"会变成一个 500，而不是我们要的 400 那句话。
        if encrypted:
            raise BadPasswordError(
                "「%s」的旧密码不对（解出来不是有效的压缩数据）" % info.filename
            )
        raise ZipCryptoError(
            "「%s」的压缩数据坏了（%s）" % (info.filename, exc)
        )


def _write_central(entry: _WrittenEntry, dest: BinaryIO) -> None:
    info = entry.info
    version_made = ((info.create_system or 0) << 8) | (info.create_version or 20)
    dest.write(
        struct.pack(
            _CENTRAL_FMT,
            b"PK\x01\x02",
            version_made & 0xFFFF,
            info.extract_version or 20,
            entry.flags,
            entry.method,
            entry.dos_time,
            entry.dos_date,
            entry.crc,
            entry.comp_size,
            entry.uncomp_size,
            len(entry.name),
            len(entry.extra),
            len(entry.comment),
            0,  # disk start
            info.internal_attr or 0,
            info.external_attr or 0,
            entry.local_offset,
        )
    )
    dest.write(entry.name)
    dest.write(entry.extra)
    dest.write(entry.comment)


def _write_eocd(
    dest: BinaryIO, count: int, central_size: int, central_offset: int, comment: bytes
) -> None:
    if count > _MAX_ENTRIES:  # pragma: no cover - 2 GB 上限下走不到
        raise ZipCryptoError("成员太多，超过 65535 个的 zip 不支持重新打包")
    dest.write(
        struct.pack(
            _EOCD_FMT,
            b"PK\x05\x06",
            0,
            0,
            count,
            count,
            central_size,
            central_offset,
            len(comment or b""),
        )
    )
    if comment:
        dest.write(comment)
