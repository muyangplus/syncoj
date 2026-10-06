#!/usr/bin/env python3
"""SyncOJ Agent 幂等安装器。

为什么是 Python 而不是 install.sh
---------------------------------
安装器本质是"一堆文件系统操作 + 分支逻辑"，而这正是 shell 最容易写错、
最难测试的地方。用 Python 写有三个好处：

1. **可测**。它能在 CI 里以 ``--prefix`` 装到临时目录、以 ``--dry-run`` 只打印
   计划，因此每条分支都被真实执行过。一个没被跑过的安装脚本等于没有安装脚本。
2. **有类型和异常**。路径拼错、权限失败、tar 结构异常都能明确报错，而不是
   在 shell 里静默地继续往下跑。
3. **零额外依赖**。目标机是 NOI Linux，python3 必然存在 —— 与 Agent 同一个约束。

三个入口共用这一份逻辑
----------------------
::

    # 镜像预装（构建镜像时跑）。密钥用 syncoj-server bootstrap-key issue 签发，
    # 明文只显示一次 —— 拿到就写进镜像，别再落在别处
    sudo python3 install.py --server https://10.0.0.1:8443 \
        --bootstrap-key <43 字符的密钥> --user student

    # 离线包安装（考场无网）
    sudo python3 install.py --bundle ./syncoj-agent-0.1.0.tar.gz --server ... \
        --bootstrap-key <密钥>

    # 在线自举（网络可达）
    sudo python3 install.py --download-url https://10.0.0.1:8443/dist/syncoj-agent-0.1.0.tar.gz ...

**注册只有一条路**：镜像里那份 root 只读的统一密钥。它换来的是"一台还没有归属
的机器"，教师再用六位配对码把它绑到名单里的**人**（见 docs/protocol.md §1）。

**幂等**是硬要求：重复执行结果一致。尤其是 —— **绝不覆盖已存在的 agent.ini**。
教师可能已经在里面改了扫描目录，安装器把它们冲掉是灾难性的。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Tuple

#: 本文件在仓库里的位置。安装器也会被拷进安装包/镜像，那时 ``parents[2]`` 不再
#: 指向仓库 —— 所以凡是用到它的地方都要先判存在性，猜错只能退化成"没有这个默认值"，
#: 绝不能猜出一个文件路径来。
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PREFIX = Path("/opt/syncoj")
DEFAULT_CONFIG_DIR = Path("/etc/syncoj")
DEFAULT_STATE_DIR = Path("/var/lib/syncoj")
DEFAULT_DEPLOY_ROOT = "{desktop}"
DEFAULT_SCAN_ROOT = "{desktop}/{player_no}"
DEFAULT_SCAN_PREFIX = "none"
#: 兜底运行账号。**只有**在既拿不到 ``SUDO_USER``、也拿不到当前用户时才用它
#: （例如非 POSIX 的构建机上做 dry-run）。正常运行**不再默认新建专用账号** ——
#: 默认是"跑安装的那个人"，见 :func:`default_run_user`。
DEFAULT_RUN_USER = "syncoj"
SERVICE_NAME = "syncoj-agent"

CONFIG_FILENAME = "agent.ini"
UNIT_FILENAME = SERVICE_NAME + ".service"
#: 统一注册密钥。**root 只读** —— 它能注册整间机房，不该躺在学生读得到的地方
BOOTSTRAP_KEY_FILENAME = "bootstrap.key"
#: 升级签名公钥。它不是秘密（只是信任锚），0644 即可。
#:
#: 安装包可以自带一份（``build_bundle.py`` 从仓库 ``.key/`` 打进去），落点固定在
#: ``<config-dir>/`` 而不是版本目录里 —— 版本目录会被清理，把信任锚放在那儿等于
#: 某天升级会因为"公钥不见了"而整体失效，而那时没人会想到是删旧版本删出来的。
PUBLIC_KEY_FILENAME = "release-key.pub.json"

#: 包内内嵌的服务端地址（``build_bundle.py --server-url`` 写的那个文件）。
SERVER_URL_FILENAME = "server.json"

#: 服务端装机入口的路径。**必须与 ``api/agent.py`` 里的常量一致** ——
#: 两边各写一遍的话，改了路由就会在这里变成一个 404，而现象是"装机装不上"。
#: 有一条测试（``tests/test_install_entry.py``）从服务端那一侧盯着它们。
INSTALL_LEDGER_PATH = "/api/v1/agent/install.json"
INSTALL_BUNDLE_PATH = "/api/v1/agent/install/bundle"

#: ``--server`` 与包内地址都缺、局域网也问不到时用的值。
#:
#: 这个默认值是**故意留成回环**的：它是一个明确的"还没配好"，而不是一个看起来
#: 像配好了的猜值。安装器会为此打一条显眼的警告 —— 因为把它当成真实地址的后果
#: 是机器去连自己，而现场只看到"注册不上"。
DEFAULT_SERVER_URL = "https://127.0.0.1:8000"
#: 从源码仓库跑安装器时，密钥的默认位置（``syncoj-server init`` 生成的地方）。
DEFAULT_BOOTSTRAP_KEY_FILE = REPO_ROOT / ".key" / BOOTSTRAP_KEY_FILENAME
#: 装机时以 root 身份跑一次注册的单元。
#:
#: 为什么必须是独立单元而不是让 Agent 自己注册：Agent 以选手身份运行，
#: 读不到 root 只读的密钥。注册这件事只能由 root 做一次，做完把凭据交给选手。
ENROLL_UNIT_FILENAME = SERVICE_NAME + "-enroll.service"
#: 安装根目录下与 Agent 运行期共享的布局常量，必须与 syncoj_agent/upgrade.py 一致
RELEASES_DIR = "releases"
CURRENT_LINK = "current"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ROOT = 2

#: 安装包内应当存在的顶层目录。用它来确认拿到的是真的 Agent 包，
#: 而不是一个名字很像的压缩文件。
EXPECTED_TOP_LEVEL = "syncoj_agent"
#: 发布根目录下的启动器。systemd 的 ExecStart 指向它，而不是包内的 main.py
#: —— 后者使用包内相对导入，当脚本直接跑会 ImportError。
LAUNCHER_NAME = "run_agent.py"
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20000

#: 版本目录里记录"这一份内容指纹"的标记文件。
#:
#: 幂等判断**按内容**而不是"版本目录在不在"：同名版本号重新构建的包，如果只因
#: ``releases/<版本>/`` 存在就跳过，装过的机器永远拿不到新内容。放在版本目录
#: **内部** —— 删版本目录/卸载时它自然一起消失，不会留下指向不存在版本的记录。
BUNDLE_SHA_MARKER_FILENAME = ".syncoj-bundle-sha256"

#: 卸载时用来判断"这个目录看起来是不是 SyncOJ 装出来的"的标记。
#: ``--prefix`` / ``--config-dir`` / ``--state-dir`` 都是可覆盖的，所以**不能**
#: 因为路径对得上就删 —— 用户把它们指到别处时，宁可不卸也不能清人家的目录。
UNINSTALL_PREFIX_MARKERS = (RELEASES_DIR, CURRENT_LINK, LAUNCHER_NAME)
#: 配置目录里的凭据：统一注册密钥 + 升级信任锚。
UNINSTALL_CONFIG_MARKERS = (CONFIG_FILENAME, BOOTSTRAP_KEY_FILENAME, PUBLIC_KEY_FILENAME)

#: 状态目录里记录"这个运行账号是本安装器创建的"的标记文件。
#:
#: 卸载默认会删运行账号，而默认运行账号是**跑安装的那个人自己的账号** ——
#: 没有证据就删账号是灾难级的。所以只删"有据可查是我们建的"那一个；老版本装的
#: 机器没有这个文件（`--keep-state` 会把它留下，但这里只认文件内容）→ 一律不删。
CREATED_USER_MARKER_FILENAME = ".syncoj-created-user"

#: 家目录下"桌面"可能叫什么。顺序与 ``syncoj_agent/state.py::detect_desktop``
#: 一致：先看 ``XDG_DESKTOP_DIR``，再看这几个实际存在的名字，都没有就按中文环境猜。
DESKTOP_CANDIDATES = ("桌面", "Desktop", "desktop")
#: 状态目录里的本机身份与日志。
UNINSTALL_STATE_MARKERS = ("credential.json", "machine_uuid", "agent.log")


class InstallError(Exception):
    """安装过程中的可预期失败。"""


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


class Reporter:
    def __init__(self, dry_run: bool = False, quiet: bool = False) -> None:
        self.dry_run = dry_run
        self.quiet = quiet
        self.changes = 0

    def _emit(self, prefix: str, message: str) -> None:
        if not self.quiet:
            print("%s %s" % (prefix, message), flush=True)

    def plan(self, message: str) -> None:
        suffix = "  [dry-run]" if self.dry_run else ""
        self._emit("  ·", message + suffix)

    def action(self, message: str) -> None:
        self.changes += 1
        self._emit("  +", message)

    def skip(self, message: str) -> None:
        self._emit("  =", message)

    def note(self, message: str) -> None:
        self._emit("   ", message)

    def warn(self, message: str) -> None:
        self._emit("  !", message)

    def section(self, title: str) -> None:
        if not self.quiet:
            print("\n%s" % title, flush=True)


# --------------------------------------------------------------------------- #
# 安全解包
# --------------------------------------------------------------------------- #
#
# 这里**刻意重写一遍**而不是 import syncoj_agent.upgrade：安装器运行时目标机上
# 还没有 Agent 代码（鸡生蛋问题），必须自包含。
#
# 同时也是纵深防御：与 Agent 侧那份独立实现互为交叉验证，任何一份被改坏，
# 另一份仍然拦得住。


def _check_tar_member(member: tarfile.TarInfo, dest: Path) -> Path:
    name = member.name
    if not name:
        raise InstallError("安装包中存在无名成员")
    if "\\" in name:
        raise InstallError("安装包成员名含反斜杠: %r" % name)
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        raise InstallError("安装包成员使用绝对路径: %r" % name)
    if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
        raise InstallError("安装包成员是设备/管道节点: %r" % name)
    if member.issym() or member.islnk():
        raise InstallError("安装包成员是链接，已拒绝: %r" % name)

    target = (dest / name).resolve()
    dest_resolved = dest.resolve()
    try:
        common = os.path.commonpath([str(target), str(dest_resolved)])
    except ValueError:
        raise InstallError("安装包成员路径逃逸: %r" % name)
    if common != str(dest_resolved):
        raise InstallError("安装包成员路径逃逸出解压目录: %r" % name)
    return target


def safe_extract(bundle: Path, dest: Path) -> List[str]:
    """安全解压，返回顶层条目名列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    tops: List[str] = []
    total = 0

    try:
        archive = tarfile.open(str(bundle), mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise InstallError("无法打开安装包（不是有效的 tar 归档）: %s" % exc)

    with archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise InstallError("安装包成员数 %d 超过上限" % len(members))

        for member in members:
            target = _check_tar_member(member, dest)

            first = member.name.split("/", 1)[0]
            if first and first not in tops:
                tops.append(first)

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise InstallError("不支持的安装包成员类型: %r" % member.name)

            total += max(0, member.size)
            if total > MAX_EXTRACTED_BYTES:
                raise InstallError("解压后总大小超过上限")

            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover
                raise InstallError("无法读取安装包成员: %r" % member.name)
            with source, open(str(target), "wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            try:
                # 剥掉 SUID/SGID/Sticky
                os.chmod(str(target), (member.mode & 0o777) or 0o644)
            except OSError:
                pass

    return tops


# --------------------------------------------------------------------------- #
# 版本识别
# --------------------------------------------------------------------------- #


def read_bundle_version(root: Path) -> str:
    """从解压出来的包里读出版本号。

    不依赖 ``import syncoj_agent`` —— 那是目标机上要装的代码，在安装器进程里
    导入等于让"要安装的东西"先在安装器里跑一遍，既没必要也不安全。
    """
    init_file = root / EXPECTED_TOP_LEVEL / "__init__.py"
    if not init_file.is_file():
        raise InstallError(
            "安装包里找不到 %s/__init__.py，可能不是 SyncOJ Agent 包" % EXPECTED_TOP_LEVEL
        )
    try:
        text = init_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("无法读取版本信息: %s" % exc)

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__"):
            _, _, value = stripped.partition("=")
            return value.strip().strip("'\"")
    raise InstallError("安装包中找不到 __version__")


def current_version(prefix: Path) -> Optional[str]:
    link = prefix / CURRENT_LINK
    try:
        if not link.is_symlink():
            return None
        return Path(os.readlink(str(link))).name
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(root: Path) -> str:
    """对一棵目录树做**确定性**摘要，用于 ``--from-dir`` 的内容指纹。

    ``--from-dir`` 没有 tar 包可算 sha256，但"同版本重建的目录"同样需要能触发
    覆盖安装，所以按"相对路径 + 文件内容"算。按路径排序，结果与遍历顺序无关；
    符号链接跳过（安装包本来也不允许链接，且要防止成环）。
    """
    items = [entry for entry in root.rglob("*") if not entry.is_symlink()]
    digest = hashlib.sha256()
    for entry in sorted(items, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(entry.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        if entry.is_file():
            with open(str(entry), "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_release_marker(release_dir: Path) -> Optional[str]:
    """读版本目录里记录的内容指纹。

    没有这个文件（历史安装）、或读不出来，都返回 ``None`` —— 调用方按"来源未知"
    处理，也就是**覆盖安装一次**，之后这个版本目录就有记录了。
    """
    marker = release_dir / BUNDLE_SHA_MARKER_FILENAME
    try:
        text = marker.read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _write_release_marker(staging: Path, fingerprint: str) -> None:
    """把内容指纹写进**待替换的 staging**，随目录一起换上去。"""
    marker = staging / BUNDLE_SHA_MARKER_FILENAME
    # 3.8 上 write_text 没有 newline 参数，用 open
    with marker.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(fingerprint + "\n")


def _decode_signature(text: str) -> Optional[bytes]:
    """base64url 解码，容忍缺填充。解不出来就是 None（调用方按验签失败处理）。"""
    import base64
    import binascii

    cleaned = (text or "").strip().replace("+", "-").replace("/", "_")
    if not cleaned:
        return None
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 发布公钥解析与 RSA 验签（刻意自包含，与"安全解包"同一个理由）
# --------------------------------------------------------------------------- #
#
# ``bootstrap.sh`` 下发给目标机的**只有 install.py 一个文件**（落点形如
# ``/tmp/syncoj-bootstrap.XXXX/``）。那一刻机器上既没有 ``syncoj_agent`` 包、
# 也没解包过任何东西 —— 所以安装器里**任何** ``from syncoj_agent import ...``
# 都必然 ModuleNotFoundError，而现场表现是"装到一半就炸"。
#
# 还有一条更硬的安全底线：**绝不能先把包解开、把解出来的目录加进 sys.path，
# 再 import 来验签**。那等于拿一份尚未验证的包里的代码去验证它自己，验签白做。
# 验签实现只能来自安装器自身（或机器上已经装好的旧版本）—— 这里选前者。
#
# 下面是对 ``syncoj_agent/rsa.py`` 与 ``syncoj_agent/discovery.py`` 相关部分的
# **独立重写**，行为保持一致：两份实现互为交叉验证，任何一份被改坏，另一份仍然
# 拦得住。

#: RFC 8017 §9.2 的 SHA-256 DigestInfo 前缀（含外层 SEQUENCE）。
#: 结构：SEQUENCE(30 31) SEQUENCE(30 0d) OID(06 09 60 86 48 01 65 03 04 02 01)
#:       NULL(05 00) OCTET STRING(04 20)
PKCS1_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

#: RSA 模数的最小位数，低于它直接拒绝。与 syncoj_agent/rsa.py 一致。
MIN_MODULUS_BITS = 2048


class SignatureError(Exception):
    """公钥格式不合法。签名不匹配属于正常结果，用返回值 False 表达。"""


def _unb64(text: str) -> bytes:
    """解码 base64url（容忍缺少的填充与标准 base64 的 +/）。"""
    cleaned = str(text).strip().replace("+", "-").replace("/", "_")
    padding = "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(cleaned + padding)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("base64 解码失败: %s" % exc)


class RSAPublicKey:
    """只够验签用的 RSA 公钥（验证只需要 n、e）。"""

    __slots__ = ("n", "e", "key_id")

    def __init__(self, n: int, e: int, key_id: str = "") -> None:
        if n <= 0:
            raise SignatureError("模数必须为正整数")
        if e < 3 or e % 2 == 0:
            raise SignatureError("公钥指数必须是大于等于 3 的奇数")
        if n.bit_length() < MIN_MODULUS_BITS:
            raise SignatureError(
                "模数只有 %d 位，低于安全下限 %d 位" % (n.bit_length(), MIN_MODULUS_BITS)
            )
        self.n = n
        self.e = e
        self.key_id = key_id

    @property
    def byte_length(self) -> int:
        """模数的字节长度，即合法签名的长度。"""
        return (self.n.bit_length() + 7) // 8

    @classmethod
    def from_dict(cls, data) -> "RSAPublicKey":
        if not isinstance(data, dict):
            raise SignatureError("公钥必须是 JSON 对象")
        alg = data.get("alg")
        if alg not in (None, "RS256"):
            raise SignatureError("不支持的算法: %r" % alg)
        try:
            n = int.from_bytes(_unb64(data["n"]), "big")
            e = int.from_bytes(_unb64(data["e"]), "big")
        except KeyError as exc:
            raise SignatureError("公钥缺少字段: %s" % exc)
        return cls(n=n, e=e, key_id=str(data.get("key_id", "")))


def load_public_key(path) -> Optional["RSAPublicKey"]:
    """读发布公钥；读不出来返回 ``None``（调用方按"不能验"处理）。

    与 ``syncoj_agent.discovery.load_public_key`` 行为一致：刻意不区分"文件不存在"
    和"内容不合法" —— 两种情况的调用方动作一样（不发现 / 跳过验签）。
    """
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return RSAPublicKey.from_dict(json.loads(text))
    except (ValueError, SignatureError):
        return None


def expected_encoded_message(byte_length: int, message: bytes) -> bytes:
    """按 RFC 8017 §9.2 构造 EMSA-PKCS1-v1_5 编码。"""
    digest_info = PKCS1_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = byte_length - len(digest_info) - 3
    if padding_length < 8:
        raise SignatureError("模数太短，容不下 PKCS#1 v1.5 填充")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info


def verify_pkcs1v15_sha256(
    public_key: "RSAPublicKey",
    message: bytes,
    signature: bytes,
) -> bool:
    """验证签名。任何异常情况都返回 ``False``，不抛异常。

    严格按 RFC 8017 §8.2.2 **逐字节重建再整体比对** —— 不解析、不容忍、不留余量。
    宽松校验正是 PKCS#1 v1.5 历史上多次被伪造打穿的入口。
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

    return hmac.compare_digest(encoded, expected)


# --------------------------------------------------------------------------- #
# 局域网发现（客户端，同样刻意自包含）
# --------------------------------------------------------------------------- #
#
# 安装器只在两处用得上它：``--from-server`` 没给地址时，以及写配置时包里没内嵌
# 地址。原来的实现 import 了 ``syncoj_agent.discovery``，同样会在"单文件安装器"
# 这个真实上下文里直接炸掉。
#
# 这里只重写**客户端**那一半：探测、验签、不猜歧义。协议常量必须与服务端
# ``syncoj_server/services/discovery.py`` 保持一致（互通性由两边各自的测试盯着）。

#: 探测报文的魔数与版本。
DISCOVERY_MAGIC = "syncoj"
DISCOVERY_PROTOCOL_VERSION = 1
DISCOVERY_MIN_PROBE_BYTES = 640
DISCOVERY_PORT = 45871
#: 探测文本前缀（与服务端共用一个字面量，跨协议隔离）。
DISCOVERY_SIGNATURE_PREFIX = "syncoj-discovery-v1"
#: 等应答的默认秒数。局域网往返是个位数毫秒。
DISCOVERY_TIMEOUT_SECONDS = 1.5
#: 一次最多看几个应答（防"被应答淹没"的上界，不是答案数量）。
DISCOVERY_MAX_REPLIES = 16


#: 与原 ``syncoj_agent.discovery`` 保持同样的诊断输出（没配 handler 时走
#: logging 的 lastResort，直接进 stderr）。现场排查靠它区分"没人应答"与
#: "有人应答但验签没过"。
log = logging.getLogger(__name__)


class DiscoveryOutcome(NamedTuple):
    """一次探测的结果。

    ``url`` 只有**恰好一个**验过签的答案时才非空。``candidates`` 用来解释为什么
    没定下来（两个服务端 = 歧义；空 = 没人应答）。
    """

    url: Optional[str]
    candidates: List[str]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    def explain(self) -> str:
        if self.url:
            return "找到服务端：%s" % self.url
        if self.ambiguous:
            return "局域网里有多个服务端都回了应答（%s）—— 需要显式指定地址" % (
                "、".join(self.candidates)
            )
        return "局域网里没有服务端应答"


def canonical_text(nonce: str, url: str) -> str:
    """要被签名的那段文本。**与服务端逐字节一致**。"""
    return "%s\n%s\n%s" % (DISCOVERY_SIGNATURE_PREFIX, nonce, url)


def build_probe(nonce: str, machine_id: str = "", min_bytes: int = DISCOVERY_MIN_PROBE_BYTES) -> bytes:
    """造探测报文，**带填充**凑到 ``min_bytes``。

    填充是协议的一部分：服务端立了一条"应答不得大于探测"的规矩来避免自己变成
    放大器，太短的探测它直接不理。
    """
    payload = {
        "syncoj": DISCOVERY_MAGIC,
        "v": DISCOVERY_PROTOCOL_VERSION,
        "nonce": nonce,
        "machine_id": machine_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) < min_bytes:
        payload["pad"] = "0" * max(0, min_bytes - len(raw) - 12)
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    while len(raw) < min_bytes:
        payload["pad"] = str(payload.get("pad", "")) + "0" * (min_bytes - len(raw))
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return raw


def broadcast_targets() -> List[str]:
    """往哪些地址喊。

    永远包含受限广播 ``255.255.255.255``。此外**在 Linux 上**从 ``/proc/net/route``
    读出各接口的子网广播地址 —— 有些考场的内网没有默认路由，受限广播不一定出得去。
    读不到（Windows、或被限制）时静默跳过：那不是错误。
    """
    targets = ["255.255.255.255"]
    try:
        with open("/proc/net/route", "r", encoding="ascii") as handle:
            lines = handle.read().splitlines()[1:]
    except OSError:
        return targets

    for line in lines:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            # 这两列是**小端**十六进制（内核按内存里的字节序直接打印）
            destination = int(fields[1], 16)
            mask = int(fields[7], 16)
        except ValueError:
            continue
        if mask == 0:
            continue  # 默认路由：它的"广播地址"没有意义
        network = destination & mask
        broadcast = network | (~mask & 0xFFFFFFFF)
        target = socket.inet_ntoa(broadcast.to_bytes(4, "big"))
        if target not in targets:
            targets.append(target)
    return targets


def machine_id_hint() -> str:
    """探测里带的主机名，纯为服务端日志好读。读不到就空着。"""
    try:
        return socket.gethostname()[:64]
    except OSError:  # pragma: no cover
        return ""


def parse_discovery_reply(raw: bytes, nonce: str, public_key: "RSAPublicKey") -> Optional[str]:
    """验一个应答；通过就返回它宣称的地址，否则 ``None``。

    四道都要过：能解析、随机数对得上、地址像样、**签名验得过**。少任何一道，
    这个应答都不能用来决定"把注册密钥发给谁"。
    """
    if len(raw) > 4096:  # pragma: no cover - 防御性
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("syncoj") != DISCOVERY_MAGIC:
        return None
    if payload.get("v") != DISCOVERY_PROTOCOL_VERSION:
        return None
    if payload.get("nonce") != nonce:
        # 重放：别人录下上一次的应答原样发过来，随机数就对不上了
        log.debug("发现应答的随机数对不上，忽略（可能是重放）")
        return None
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    signature = _decode_signature(str(payload.get("sig") or ""))
    if signature is None:
        return None
    if not verify_pkcs1v15_sha256(
        public_key, canonical_text(nonce, url).encode("utf-8"), signature
    ):
        # 这是整套东西存在的理由：局域网里任何人想冒充服务端，都签不出这一段
        log.warning("发现应答的签名验不过，忽略：%s", url)
        return None
    return url.rstrip("/")


def discover(
    public_key: Optional["RSAPublicKey"],
    *,
    port: int = DISCOVERY_PORT,
    timeout: float = DISCOVERY_TIMEOUT_SECONDS,
    machine_id: str = "",
    targets: Optional[List[str]] = None,
) -> DiscoveryOutcome:
    """在局域网里找服务端。**不抛异常**，找不到就返回空结果。

    ``public_key`` 为 ``None``（机器上没装发布公钥）时直接返回空 —— 没有公钥就
    无法分辨"服务端"和"局域网里随便一个应答者"，那就不猜。
    """
    if public_key is None:
        log.info("没有发布公钥，跳过局域网发现（无法验证应答来源）")
        return DiscoveryOutcome(None, [])

    nonce = secrets.token_hex(16)
    probe = build_probe(nonce, machine_id=machine_id)
    destinations = list(targets) if targets else broadcast_targets()
    seen = {}  # type: dict
    deadline = time.monotonic() + max(0.05, timeout)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sent = 0
        for target in destinations:
            try:
                sock.sendto(probe, (target, port))
                sent += 1
            except OSError as exc:
                # 某个网段的广播发不出去很正常（没有到那个网段的路由），
                # 继续试下一个；全都发不出去才需要报出来。
                log.debug("向 %s 发探测失败：%s", target, exc)
        if not sent:
            log.warning(
                "局域网发现：没有一个地址能发出去（试过 %s）", ", ".join(destinations)
            )
            return DiscoveryOutcome(None, [])

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(seen) >= DISCOVERY_MAX_REPLIES:
                break
            sock.settimeout(remaining)
            try:
                raw, _peer = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError as exc:  # pragma: no cover - 网卡没了之类
                log.warning("局域网发现：收应答失败：%s", exc)
                break
            url = parse_discovery_reply(raw, nonce, public_key)
            if url:
                seen[url] = seen.get(url, 0) + 1
    finally:
        sock.close()

    candidates = sorted(seen)
    if len(candidates) == 1:
        return DiscoveryOutcome(candidates[0], candidates)
    return DiscoveryOutcome(None, candidates)


class Installer:
    def __init__(self, options: argparse.Namespace, report: Reporter) -> None:
        self.options = options
        self.report = report

        self.prefix = Path(options.prefix)
        self.config_dir = Path(options.config_dir)
        self.state_dir = Path(options.state_dir)
        # 保留为字符串：这两个是路径模板，可能含 {desktop} / {player_no}
        self.scan_root = options.scan_root
        self.deploy_root = options.deploy_root
        self.run_user = options.user
        self.service_name = options.service_name
        self.unit_path = Path(options.unit_dir) / UNIT_FILENAME
        self.config_path = self.config_dir / CONFIG_FILENAME

    # ---------------------------------------------------------------- #
    # 运行账号 / 家目录
    # ---------------------------------------------------------------- #

    @property
    def home_dir(self) -> Optional[Path]:
        """运行账号的家目录；查不到（非 POSIX / 账号不存在）时为 ``None``。"""
        return _home_of(self.run_user)

    @property
    def desktop_dir(self) -> Optional[Path]:
        """运行账号家目录下的桌面；查不到时为 ``None``。"""
        home = self.home_dir
        return _desktop_of(home) if home is not None else None

    def _expand_user_paths(self, raw: str) -> str:
        """把配置模板里的 ``{desktop}`` 与开头的 ``~`` 展开成**运行账号**的真实路径。

        为什么要在安装时展开：``~`` 在这里会按"跑安装的人"（通常是 root）解释，
        而 Agent 跑的是另一个账号 —— 留着 ``~`` 会指错人；``{desktop}`` 同理，
        装完当场看一眼 ``agent.ini`` 就知道到底扫哪里，不用等 Agent 跑起来才知道。

        ``{player_no}`` / ``{contest_slug}`` **保留**：它们要等注册之后才有值。
        查不到运行账号的家目录时（非 POSIX 构建机）原样返回 —— 让 Agent 运行时
        按自己那个账号展开，总比写死一个构建机路径强。
        """
        if not raw:
            return raw
        home = self.home_dir
        desktop = self.desktop_dir
        if home is None or desktop is None:
            return raw

        parts: List[str] = []
        for item in raw.replace(",", "\n").splitlines():
            text = item.strip()
            if not text:
                continue
            if text == "~":
                text = str(home)
            elif text.startswith("~/"):
                text = str(home / text[2:])
            text = text.replace("{desktop}", str(desktop))
            parts.append(text)
        return ", ".join(parts)

    def _created_user_marker(self) -> Path:
        return self.state_dir / CREATED_USER_MARKER_FILENAME

    def _record_created_user(self, name: str) -> None:
        """记一笔"这个账号是本安装器建的"，卸载时只认这个证据。"""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self._created_user_marker().open(
                "w", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(name + "\n")
        except OSError as exc:
            self.report.warn(
                "无法记录“运行账号是安装器创建的”（%s）；卸载时**不会**删除它" % exc
            )

    def _user_created_by_installer(self) -> Optional[str]:
        """安装器记录下来的"我建过的账号"；没有记录返回 ``None``（当"不是我建的"）。"""
        try:
            text = self._created_user_marker().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return text or None

    # ---------------------------------------------------------------- #
    # 前置检查
    # ---------------------------------------------------------------- #

    def preflight(self) -> None:
        self.report.section("检查环境")

        needs_root = not self.report.dry_run and not self.options.skip_user
        if needs_root and not _is_root():
            raise InstallError(
                "需要 root 权限（创建用户、写 /etc 与 /opt、注册 systemd 单元）。"
                "请用 sudo 运行，或加 --dry-run 预览计划。"
            )

        sources = [bool(self.options.bundle), bool(self.options.download_url),
                   bool(self.options.from_dir), bool(self.options.from_server)]
        if sum(sources) != 1:
            raise InstallError(
                "必须且只能指定一个来源：--bundle / --download-url / --from-server / --from-dir"
            )

        if self.options.bundle and not Path(self.options.bundle).is_file():
            raise InstallError("安装包不存在: %s" % self.options.bundle)

        if self.options.from_dir and not (Path(self.options.from_dir) / EXPECTED_TOP_LEVEL).is_dir():
            raise InstallError(
                "目录 %s 下找不到 %s/" % (self.options.from_dir, EXPECTED_TOP_LEVEL)
            )

        python = self.options.python or sys.executable or "python3"
        if not self.options.skip_python_check:
            if not _python_ok(python):
                raise InstallError(
                    "%s 不可用或无执行权限。目标机需要 Python 3 才能运行 Agent。" % python
                )
            self.report.skip("Python 解释器可用: %s" % python)

        self.report.skip("安装根目录: %s" % self.prefix)
        self.report.skip("配置目录: %s" % self.config_dir)
        self.report.skip("状态目录: %s" % self.state_dir)

    # ---------------------------------------------------------------- #
    # 步骤
    # ---------------------------------------------------------------- #

    def ensure_user(self) -> None:
        """运行账号：存在就什么都不做；不存在才创建，并留下"是我建的"证据。

        **默认不新建专用账号**：默认运行账号是"跑安装的那个人"（见
        :func:`default_run_user`），它必然已经存在。只有显式 ``--user <不存在的
        系统账号>`` 才会走到创建这一支 —— 那时才写
        :data:`CREATED_USER_MARKER_FILENAME`，卸载也只删这种有据可查的账号，
        绝不会去删人自己的账号。
        """
        if self.options.skip_user:
            self.report.skip("跳过用户创建（--skip-user）")
            return
        if _user_exists(self.run_user):
            self.report.skip("用户 %s 已存在（不新建、不修改）" % self.run_user)
            return
        if self.report.dry_run:
            self.report.plan("创建系统用户 %s（无登录 shell）" % self.run_user)
            return
        run(["useradd", "--system", "--shell", "/usr/sbin/nologin",
             "--home-dir", str(self.state_dir), "--no-create-home", self.run_user])
        self._record_created_user(self.run_user)
        self.report.action("已创建用户 %s（卸载时会删掉它）" % self.run_user)

    def ensure_dirs(self) -> None:
        for path in (self.prefix, self.prefix / RELEASES_DIR, self.config_dir, self.state_dir):
            if path.is_dir():
                self.report.skip("目录已存在: %s" % path)
                continue
            if self.report.dry_run:
                self.report.plan("创建目录 %s" % path)
                continue
            path.mkdir(parents=True, exist_ok=True)
            self.report.action("已创建目录 %s" % path)

        # 状态目录只给**运行 Agent 的那个账号**读写 —— 里面有凭据、日志、哈希缓存。
        # **绝不能留给 root**：Agent 以运行账号启动，写不进去的表现是"服务能起来，
        # 但一写状态就 Permission denied"（token 存不下、日志也写不了）。
        if (
            self.state_dir.is_dir()
            and not self.report.dry_run
            and _user_exists(self.run_user)
        ):
            try:
                shutil.chown(str(self.state_dir), user=self.run_user)
                os.chmod(str(self.state_dir), 0o750)
            except (OSError, LookupError) as exc:
                self.report.warn("设置状态目录属主失败（后续可能权限不足）: %s" % exc)

    def fetch_bundle(self) -> Path:
        """把安装包弄到本地，返回路径。"""
        if self.options.bundle:
            return Path(self.options.bundle)

        if self.options.from_server:
            return self.fetch_from_server()

        if self.options.download_url:
            url = self.options.download_url
            target = Path(tempfile.mkdtemp(prefix="syncoj-install-")) / "bundle.tar.gz"
            if self.report.dry_run:
                self.report.plan("从 %s 下载安装包" % url)
                return target
            self.report.plan("从 %s 下载安装包" % url)
            self._download(url, target)
            self.report.action("已下载 %s" % url)
            return target

        # --from-dir：直接用一个已经解开的目录（镜像预装时最方便）
        return Path(self.options.from_dir)

    def _download(self, url: str, target: Path) -> int:
        """下载到 ``target``，返回字节数。"""
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                with open(str(target), "wb") as handle:
                    shutil.copyfileobj(response, handle, 1024 * 1024)
        except (urllib.error.URLError, OSError) as exc:
            raise InstallError("下载失败 %s: %s" % (url, exc))
        return target.stat().st_size

    def fetch_from_server(self) -> Path:
        """``--from-server``：从服务端直接拿当前可装机的那个版本。

        ## 鉴权取舍（这是有意选的，不是漏了）

        初次安装时机器手上**什么都没有** —— 没有 Agent、没有凭据，所以服务端那
        三个装机端点（``/api/v1/agent/install.json``、``/install/bundle``、
        ``/install/installer``）**不鉴权**。装完之后靠**配对**建立信任：机器注册
        上来是"待认领"状态，教师必须在管理界面把它绑到名单里的某个人，它才开始
        收代码、收文件、收成绩。

        ## 能验的都验上

        这一层的职责是把免费的检查全部做掉，并**如实报告做了哪些**：

        * **sha256 一定验**（对照服务端给的台账）。它挡的是传输损坏与"传了一半"。
          挡不住恶意服务端 —— 台账和包来自同一个未鉴权的地方，这是明摆着的。
        * **有发布公钥就再验签名**。那一步才真正挡住"有人替你换了一个包"：
          签名由发布私钥签，机器上用公钥验，中间人凑不出来。
          公钥可以来自 ``--public-key``，也可以来自已经装好的机器。
        * 两者都没有 → **明说未验签**，并把 sha256 打出来，供人跟服务端界面上那个
          对一遍。"装了但不知道装的是什么"比"装不上"更糟。
        """
        base = self._fetch_base_url()
        if self.report.dry_run:
            # 预览**不联网**（与 --check 同一条约定）：拿不到版本号就不显示它，
            # 而不是先去把台账拉下来 —— 那样"预览"会在服务端没起来时直接失败。
            self.report.plan("从 %s 取装机台账（当前已铺开的版本）" % base)
            self.report.plan("下载并校验 sha256" + ("与签名" if self.options.public_key else "（未给 --public-key，只能校验 sha256）"))
            return Path(tempfile.mkdtemp(prefix="syncoj-install-")) / "bundle.tar.gz"

        ledger_url = "%s%s" % (base, INSTALL_LEDGER_PATH)
        self.report.plan("从 %s 取装机台账" % base)
        ledger = self._fetch_ledger(ledger_url)

        version = str(ledger.get("version") or "").strip()
        expected = str(ledger.get("sha256") or "").strip().lower()
        if not version or len(expected) != 64:
            raise InstallError("服务端给的装机台账不合法（version=%r sha256=%r）" % (version, expected))
        # 教师从界面上抄来的校验和是**更强**的证据（它不来自那台服务端），
        # 所以两边都要对得上，而不是"有一个就行"。
        given = (self.options.sha256 or "").strip().lower()
        if given and given != expected:
            raise InstallError(
                "服务端给的校验和与你给的 --sha256 不一致：\n"
                "  服务端: %s\n  你给的: %s\n"
                "这通常意味着中间有人改过（或者界面上的版本换了）。停下来是对的。" % (expected, given)
            )

        target = Path(tempfile.mkdtemp(prefix="syncoj-install-")) / "bundle.tar.gz"
        bundle_path = str(ledger.get("bundle") or INSTALL_BUNDLE_PATH)
        url = bundle_path if bundle_path.startswith("http") else base + bundle_path
        size = self._download(url, target)
        self.report.action("已下载 Agent %s（%d 字节）" % (version, size))

        actual = _sha256_file(target)
        if actual != expected:
            raise InstallError(
                "下载下来的包和台账对不上：\n  台账: %s\n  实际: %s\n"
                "可能是传输损坏，也可能是中间有人改了包。" % (expected, actual)
            )
        self.report.note("sha256 校验通过: %s" % expected[:16])

        self._verify_signature_if_possible(ledger, expected)
        return target

    def _fetch_base_url(self) -> str:
        """``--from-server`` 该往哪个地址要包。

        只认两条：显式 ``--server``，或者**验得过签的**局域网发现。刻意**不接**
        :meth:`resolve_server_url` 里那条"退回 127.0.0.1"的兜底 —— 那条兜底会让人
        从一个不存在的本机服务端下载，而报错会是"下载失败"，指不到真正的原因。
        """
        if self.options.server:
            return self.options.server.rstrip("/")
        if self.options.public_key:
            key = load_public_key(self.options.public_key)
            if key is None:
                raise InstallError("--public-key 读不出来: %s" % self.options.public_key)
            outcome = discover(
                key,
                timeout=self.options.discover_timeout,
                machine_id=machine_id_hint(),
            )
            if outcome.url:
                return outcome.url
            raise InstallError("没找到服务端：%s" % outcome.explain())
        raise InstallError(
            "用 --from-server 时必须给 --server，或者给 --public-key 让它去局域网里找：\n"
            "  sudo python3 install.py --from-server --server http://10.0.0.5:8000\n"
            "（不给公钥就没法验证局域网里那个应答是不是服务端发的，所以不接受"
            "「不给地址、盲信一个应答」。）"
        )

    def _fetch_ledger(self, url: str) -> dict:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 404 是"还没铺开任何版本"，不是网络问题 —— 把它和网络错误分开报，
            # 否则教师会去查网线，而真正要做的动作是在界面上点「铺开」。
            detail = ""
            try:
                detail = str(json.loads(exc.read().decode("utf-8")).get("detail") or "")
            except (ValueError, OSError):
                pass
            if exc.code == 404:
                raise InstallError("服务端说还没有可装机的版本：%s" % (detail or url))
            raise InstallError("取装机台账失败（HTTP %d）%s" % (exc.code, ("：" + detail) if detail else ""))
        except (urllib.error.URLError, OSError) as exc:
            raise InstallError("连不上服务端 %s: %s" % (url, exc))
        try:
            ledger = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise InstallError("装机台账不是合法 JSON: %s" % exc)
        if not isinstance(ledger, dict):
            raise InstallError("装机台账不是一个对象")
        return ledger

    def _verify_signature_if_possible(self, ledger: dict, sha256_hex: str) -> None:
        """有公钥就验签；没有就**明说没验**。"""
        signature = str(ledger.get("signature") or "").strip()
        key_path = self.options.public_key
        if not key_path:
            installed = self.config_dir / PUBLIC_KEY_FILENAME
            key_path = str(installed) if installed.is_file() else None
        if not key_path:
            self.report.warn(
                "包**未验签**：机器上没有发布公钥，只校验了 sha256（它来自同一台服务端，"
                "挡得住传输损坏、挡不住恶意服务端）"
            )
            self.report.note("要验签就加 --public-key <release-key.pub.json>；")
            self.report.note("抄下这个校验和，跟服务端「Agent 发布」页上那个对一遍：")
            self.report.note("  %s" % sha256_hex)
            return
        if not signature:
            self.report.warn("服务端没给签名，跳过验签（机器上有公钥却用不上）")
            return
        key = load_public_key(key_path)
        if key is None:
            self.report.warn("发布公钥读不出来（%s），跳过验签" % key_path)
            return
        raw = _decode_signature(signature)
        if raw is None or not verify_pkcs1v15_sha256(key, sha256_hex.encode("ascii"), raw):
            raise InstallError(
                "**签名校验不通过** —— 这个包不是这台服务端的发布私钥签的。\n"
                "安装被中止。请核对：服务端是不是换过密钥、这个地址是不是你以为的那台。"
            )
        self.report.action("签名校验通过（key_id=%s）" % (ledger.get("key_id") or "?"))


    def _staging_dir(self) -> Path:
        """解包/拷贝用的临时版本目录。

        与 ``releases/<版本>/`` **同层**（同一个文件系统），这样后面换目录才能用
        一次 rename 原子完成；名字带 pid，两个安装器同时跑也不会互相踩。
        """
        return self.prefix / RELEASES_DIR / (".staging-" + str(os.getpid()))

    def _plan_release(self, version: str, fingerprint: str) -> None:
        """预览（--dry-run）时说清"会跳过还是覆盖"，只读不写。"""
        release_dir = self.prefix / RELEASES_DIR / version
        if not release_dir.is_dir():
            self.report.plan("安装版本 %s 到 %s" % (version, release_dir))
            return
        marker = _read_release_marker(release_dir)
        if marker and marker == fingerprint:
            self.report.skip("版本 %s 已是最新（内容指纹一致），跳过" % version)
            return
        self.report.plan(
            "覆盖安装版本 %s（%s）"
            % (version, "内容指纹变了" if marker else "没有指纹记录，按历史安装处理")
        )

    def install_release(self, source: Path) -> str:
        """把 Agent 代码放进 ``releases/<版本>/``，返回版本号。

        **幂等靠内容指纹，不靠"版本目录在不在"。** 同名版本号重新构建的包，如果
        只因为 ``releases/<版本>/`` 就跳过，装过的机器永远拿不到新内容 —— 现场
        踩过：用户勾了"附带密钥"重新构建、铺开同一个版本号，装过的机器上那份
        密钥/代码都进不去。

        规则（指纹记在版本目录内的 ``.syncoj-bundle-sha256``）：

        * 这一份来源的指纹与记录相同 → 跳过复用（重复执行不产生实际变化）；
        * 指纹不同，或者老目录里**没有**记录（历史安装，来源未知）→ **覆盖安装**；
        * 覆盖是"安全替换"：先解到 ``.staging-<pid>`` 并校验，再把老目录 rename
          成 ``.old-<pid>``、staging rename 上去，成功后才删老目录；被换的正是
          当前激活版本时先停服务（后面"启动服务"那一步会再拉起来）。

        ``--from-dir`` 没有 tar 包，指纹按目录树算（:func:`_sha256_tree`）。
        """
        self.report.section("安装 Agent")
        bundle = Path(source)

        if self.options.from_dir:
            version = read_bundle_version(bundle)
            fingerprint = _sha256_tree(bundle)
            if self.report.dry_run:
                self._plan_release(version, fingerprint)
                return version
            staging = self._staging_dir()
            shutil.rmtree(str(staging), ignore_errors=True)
            try:
                shutil.copytree(str(bundle), str(staging))
                return self._finish_release(staging, version, fingerprint)
            finally:
                # 覆盖那条路已把 staging rename 走（这里是无害的空操作）；
                # "跳过"与异常路径都靠它清掉 .staging-*，别留在 releases/ 里。
                shutil.rmtree(str(staging), ignore_errors=True)

        if self.report.dry_run:
            # 预览不解包：版本号与指纹都要解包后才知道，所以只列计划
            self.report.plan("校验并解压 %s" % bundle)
            self.report.plan("安装到 %s/<版本>/" % (self.prefix / RELEASES_DIR))
            return self.options.version or "0.0.0"

        self._verify_checksum(bundle)
        fingerprint = _sha256_file(bundle)

        staging = self._staging_dir()
        shutil.rmtree(str(staging), ignore_errors=True)
        try:
            tops = safe_extract(bundle, staging)
            if EXPECTED_TOP_LEVEL not in tops:
                raise InstallError(
                    "安装包顶层缺少 %s/（实际顶层: %s）"
                    % (EXPECTED_TOP_LEVEL, ", ".join(tops) or "空")
                )
            if LAUNCHER_NAME not in tops:
                raise InstallError(
                    "安装包顶层缺少启动器 %s。没有它 Agent 在目标机上起不来 —— "
                    "syncoj_agent/main.py 使用包内相对导入，无法当脚本直接执行。"
                    % LAUNCHER_NAME
                )
            version = read_bundle_version(staging)
            return self._finish_release(staging, version, fingerprint)
        finally:
            # 解包失败 / 跳过 / 覆盖成功后，staging 都不该留下
            shutil.rmtree(str(staging), ignore_errors=True)

    def _finish_release(self, staging: Path, version: str, fingerprint: str) -> str:
        """``staging`` 里已经装好这一份内容；决定"跳过复用"还是"原子替换"。"""
        release_dir = self.prefix / RELEASES_DIR / version
        marker = _read_release_marker(release_dir)
        if release_dir.is_dir() and marker and marker == fingerprint:
            self.report.skip("版本 %s 已是最新（内容指纹一致），跳过" % version)
            return version

        # 指纹写进 staging：替换完成后它就是"这一份内容"的凭据
        _write_release_marker(staging, fingerprint)

        if release_dir.is_dir():
            self._replace_release_dir(staging, release_dir)
            self.report.action(
                "已覆盖安装版本 %s（内容指纹 %s）" % (version, fingerprint[:12])
            )
            return version

        release_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(staging), str(release_dir))
        self.report.action("已安装版本 %s" % version)
        return version

    def _replace_release_dir(self, staging: Path, release_dir: Path) -> None:
        """把 ``staging`` 原子换到 ``release_dir``。

        老目录先 rename 成 ``.old-<pid>``（同在 releases/ 下，rename 是原子的），
        再把 staging rename 成版本目录，成功后才删老目录；第二步失败就把老目录
        搬回去。被换的正是当前激活版本时**先停服务** —— 否则进程可能运行在半新
        半旧的目录上（后面"启动服务"那步会再把它拉起来）。

        任何路径都不把 ``.old-*`` 留在 releases/ 里（除非连回滚都失败，那时大声
        报警并指出老目录在哪）。
        """
        parent = release_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        old = parent / (".old-" + str(os.getpid()))
        # 上一次崩在中间留下的 .old-* 先清掉：rename 到已存在的非空目录会失败
        shutil.rmtree(str(old), ignore_errors=True)

        if current_version(self.prefix) == release_dir.name:
            self._stop_service_for_replace()

        os.replace(str(release_dir), str(old))
        swapped = False
        try:
            os.replace(str(staging), str(release_dir))
            swapped = True
        finally:
            if swapped:
                shutil.rmtree(str(old), ignore_errors=True)
            else:
                # 回滚：把老目录搬回原位，别让机器连 current 都指不到
                try:
                    os.replace(str(old), str(release_dir))
                except OSError as exc:  # pragma: no cover - 罕见
                    self.report.warn(
                        "覆盖失败且回滚也失败：老版本在 %s，请手工恢复（%s）" % (old, exc)
                    )

    def _stop_service_for_replace(self) -> None:
        """替换当前激活版本之前停一次服务。

        不停的话，进程可能运行在"一半旧一半新"的目录上（目录被逐个替换），而
        systemd 的 ``Restart=always`` 还会立刻把它拉起来。换完由 ``run()`` 里
        "启动服务"那一步照常拉起。
        """
        self.report.note("被替换的是当前激活版本，先停服务再换目录")
        if not _which("systemctl"):
            self.report.warn(
                "找不到 systemctl，无法停服务；替换期间 Agent 可能读到半新半旧的目录"
            )
            return
        result = run(["systemctl", "stop", self.service_name], check=False)
        if result == 0:
            self.report.action("已停止 %s（换完由后面那一步重新拉起）" % self.service_name)
        else:
            self.report.warn(
                "systemctl stop %s 失败（退出码 %s），继续替换"
                % (self.service_name, result)
            )

    def _verify_checksum(self, bundle: Path) -> None:
        expected = (self.options.sha256 or "").strip().lower()
        if not expected:
            self.report.warn(
                "未提供 --sha256，跳过安装包完整性校验（建议从服务端界面抄一份校验和）"
            )
            return
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise InstallError("--sha256 格式不合法")

        digest = hashlib.sha256()
        with open(str(bundle), "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise InstallError(
                "安装包 sha256 不符：期望 %s，实际 %s" % (expected[:12], actual[:12])
            )
        self.report.skip("安装包校验和匹配")

    def activate(self, version: str) -> None:
        self.report.section("激活版本")
        active = current_version(self.prefix)
        if active == version:
            self.report.skip("版本 %s 已是当前激活版本" % version)
            return

        target = self.prefix / RELEASES_DIR / version
        if not self.report.dry_run and not target.is_dir():
            raise InstallError("版本目录不存在: %s" % target)

        if self.report.dry_run:
            self.report.plan("把 %s/%s 指向 %s" % (self.prefix, CURRENT_LINK, target))
            return

        link = self.prefix / CURRENT_LINK
        tmp_link = self.prefix / (CURRENT_LINK + ".tmp")
        try:
            if tmp_link.is_symlink() or tmp_link.exists():
                tmp_link.unlink()
            os.symlink(str(target), str(tmp_link))
            os.replace(str(tmp_link), str(link))
        except OSError as exc:
            raise InstallError(
                "创建符号链接失败: %s。"
                "Windows 上需开发者模式；Linux 上请检查目录权限。" % exc
            )
        self.report.action("已激活版本 %s（原版本 %s）" % (version, active or "无"))

    def write_config(self, version: str) -> None:
        self.report.section("写入配置")

        if self.config_path.is_file() and not self.options.force_config:
            # 教师很可能已经改过扫描目录。覆盖它是灾难性的。
            self.report.skip("配置已存在，保持不变: %s" % self.config_path)
            self.report.note("（如需重建请加 --force-config，会覆盖现有配置）")
            return

        server_url, origin = self.resolve_server_url(version)
        self.report.note("服务端地址: %s（%s）" % (server_url, origin))

        content = render_config(
            server_url=server_url,
            verify_tls=not self.options.insecure,
            ca_file=self.options.ca_file or "",
            bootstrap_key_file=_posix(self.config_dir / BOOTSTRAP_KEY_FILENAME),
            state_dir=self.state_dir,
            # deploy_root / scan_roots 是**字符串模板**，可能含 {desktop}/{player_no}。
            # 这里先把 {desktop} 与开头的 ~ 按**运行账号**的家目录展开成真实路径
            # （systemd 不认 ~，而且 ~ 会按 root 解释、指错人）；{player_no} 等
            # 要等注册后才知道的占位符原样留下，由 Agent 运行时展开。
            deploy_root=self._expand_user_paths(self.options.deploy_root),
            scan_roots=self._expand_user_paths(self.options.scan_root),
            scan_prefix=self.options.scan_prefix,
            upgrade_mode=self.options.upgrade_mode,
            install_root=self.prefix,
            public_key=self.install_public_key(version),
        )

        if self.report.dry_run:
            self.report.plan("写入 %s" % self.config_path)
            return

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".ini.tmp")
        # newline="\n" 是必须的：``write_text`` 默认会把 \n 翻译成当前平台的换行，
        # 于是在 Windows 上生成的 agent.ini 是 CRLF。目标机是 Linux，那份配置里
        # 每个值末尾都会多一个不可见的 \r。统一按 LF 写，产物与生成平台无关。
        with tmp.open("w", encoding="utf-8", newline="\n") as _handle:
            _handle.write(content)
        # 临时文件先给 0600，避免在 replace 之前有一瞬间是宽权限
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(self.config_path))
        self._restrict_config_to_run_user()
        self.report.action("已写入 %s" % self.config_path)

    def resolve_server_url(self, version: str) -> "Tuple[str, str]":
        """定出 ``server.url``，返回 ``(地址, 这个地址是哪来的)``。

        四条来源，**按可信度**：

        1. ``--server`` —— 操作员明确说的，永远最优先
        2. 安装包内嵌的 ``server.json`` —— 包是那台服务端自己打的，所以这就是
           它自己知道的地址。这是主路径：装 50 台一个字都不用输
        3. **局域网发现** —— 包没带地址（别处打的包），或者地址变了。应答用发布
           公钥验过签才认
        4. 出厂默认 —— 都没找到。这时**必须说出来**，因为默认值是
           ``127.0.0.1``，而它意味着机器会去连自己

        第 3 条失败时**不退回**一个猜出来的地址：见 :meth:`discover_server_url`。
        """
        if self.options.server:
            return self.options.server.rstrip("/"), "--server"

        embedded = self.bundle_server_url(version)
        if embedded:
            return embedded, "安装包内嵌（服务端自己写的）"

        if self.options.no_discover:
            self.report.warn("已指定 --no-discover，跳过局域网发现")
        else:
            found, reason = self.discover_server_url(version)
            if found:
                return found, "局域网发现（%s）" % reason

        self.report.warn(
            "没能自动确定服务端地址，先用默认值 127.0.0.1 —— "
            "**这台机器会去连自己**，必须改成服务端的真实地址才能注册"
        )
        return DEFAULT_SERVER_URL, "默认值（需要手工改）"

    def bundle_server_url(self, version: str) -> Optional[str]:
        """安装包里内嵌的服务端地址；没有就 ``None``。

        只读不写。来源是打包时 ``build_bundle.py --server-url`` 写的
        ``server.json``（用 ``--from-dir`` 时就是那个目录下的同名文件）。
        """
        for base in (
            self.prefix / RELEASES_DIR / version,
            Path(self.options.from_dir) if self.options.from_dir else None,
        ):
            if base is None:
                continue
            candidate = base / SERVER_URL_FILENAME
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.report.warn("包内 %s 读不出来，忽略：%s" % (candidate, exc))
                continue
            # 合法 JSON 不等于合法内容：这个文件是人/脚本写的，也能被工具改坏。
            # 少这一句 `isinstance`，一个 `[]` 就会让安装器当场抛 AttributeError。
            if not isinstance(payload, dict):
                self.report.warn("包内 %s 不是对象，忽略" % candidate)
                continue
            url = str(payload.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url.rstrip("/")
            self.report.warn("包内 %s 里的地址不像话，忽略：%r" % (candidate, url))
        return None

    def discover_server_url(self, version: str) -> "Tuple[Optional[str], str]":
        """在局域网里问一次。返回 ``(地址 或 None, 说明)``。

        **验签用的公钥来自包本身**（那份复制到 ``<config-dir>`` 的信任锚），
        所以"应答是不是这台服务端发的"有据可依。没有公钥就**不问** —— 分不出
        "服务端"和"局域网里随便一个应答者"时，宁可让人去手填地址。

        歧义（两个都验得过）时也**不猜**：猜错的表现是"代码交上去了但成绩是空的"，
        现场看不出来。
        """
        public_key_path = self.prefix / RELEASES_DIR / version / PUBLIC_KEY_FILENAME
        if not public_key_path.is_file():
            return None, "机器上没有发布公钥，无法验证应答来源"

        key = load_public_key(public_key_path)
        if key is None:
            return None, "发布公钥读不出来"

        if self.report.dry_run:
            self.report.plan("在局域网里寻找服务端（UDP 广播）")
            return None, "预览模式不真的发探测"

        targets = None
        if self.options.discover_address:
            targets = [self.options.discover_address]
        outcome = discover(
            key,
            timeout=self.options.discover_timeout,
            machine_id=machine_id_hint(),
            targets=targets,
        )
        if outcome.url:
            return outcome.url, "在局域网里问到的"
        if outcome.ambiguous:
            self.report.warn(outcome.explain())
        return None, outcome.explain()

    def install_public_key(self, version: str) -> str:
        """把升级公钥放到机器上，返回 ``agent.ini`` 里 ``public_key`` 该写的路径。

        三种来源，优先级从高到低：

        1. ``--public-key <路径>`` —— 操作员明确指定，**原样写进配置**，我们不搬动它
           （生产上这通常是运维自己发到 ``/etc/syncoj/`` 的那份）
        2. 安装包自带 ``release-key.pub.json`` —— 由 ``build_bundle.py`` 从仓库
           ``.key/`` 打进去的。复制到 ``<config-dir>/`` 并把配置指向这份副本
        3. 都没有 → 空串 → 自更新保持关闭（``--upgrade-mode`` 默认就是 off）

        第 2 步为什么要**复制出**版本目录，而不是直接指向包里的那份：版本目录
        （``releases/<版本>/``）是会被清理的，而公钥是信任锚 —— 指向它意味着某天
        清理旧版本会顺手把签名验证整体废掉，而那时没人会想到是删旧版本删出来的。
        """
        if self.options.public_key:
            return self.options.public_key

        source = self.prefix / RELEASES_DIR / version / PUBLIC_KEY_FILENAME
        if not source.is_file():
            return ""

        target = self.config_dir / PUBLIC_KEY_FILENAME
        if self.report.dry_run:
            self.report.plan("安装升级公钥 %s → %s" % (source, target))
            return _posix(target)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(source), str(target))
            os.chmod(str(target), 0o644)
        except OSError as exc:
            self.report.warn("安装升级公钥失败：%s（自更新会保持关闭）" % exc)
            return ""

        self.report.action("已安装升级公钥 %s" % target)
        self.report.note("它是信任锚，随安装包一起来的 —— 不需要手工从服务端拷")
        return _posix(target)

    def _restrict_config_to_run_user(self) -> None:
        """把配置文件交给**运行 Agent 的那个用户**，别人（包括 root 之外的所有人）读不到。

        这里必须 chown，不能只 chmod：安装器是 root 跑的，文件默认属主是 root，
        `0640` 给的是 **root 组**的读权限。而 systemd 单元里写的是
        `User=<选手登录用户>` —— 那个用户既不是 root 也不在 root 组，
        结果是 **Agent 读不到自己的配置，装完起不来**。

        `0600` 而不是 `0640`：这份文件里没有密钥（密钥在单独一份 root 只读的文件
        里），但也没有任何其他账号需要读它 —— 收紧权限没有代价。

        由此推出一条必须记住的性质：**`agent.ini` 对选手登录账号是可读的**。
        所以共享密钥、长期有效的凭据这类东西，绝不该放在这个文件里 ——
        学生账号能读到镜像里的每一个文件。
        """
        if self.report.dry_run:  # pragma: no cover - dry_run 在上面就 return 了
            return
        try:
            shutil.chown(str(self.config_path), user=self.run_user)
        except (OSError, LookupError) as exc:
            # 用户不存在（--skip-user）或没有权限时不致命，但必须说出来：
            # 静默跳过会让"装完起不来"变成一道需要现场排查的谜题
            self.report.warn(
                "无法把 %s 的属主改为 %s：%s\n"
                "  Agent 以该用户运行，读不到配置就会启动失败。"
                "请手工执行：chown %s %s"
                % (self.config_path, self.run_user, exc, self.run_user, self.config_path)
            )
            return
        try:
            os.chmod(str(self.config_path), 0o600)
        except OSError as exc:
            self.report.warn("无法收紧 %s 的权限：%s" % (self.config_path, exc))

    def install_unit(self, agent_version: str) -> None:
        self.report.section("注册 systemd 单元")

        if self.options.skip_service:
            self.report.skip("跳过 systemd 配置（--skip-service）")
            return

        content = render_unit(
            prefix=self.prefix,
            config_path=self.config_path,
            state_dir=self.state_dir,
            deploy_root=self.deploy_root,
            scan_roots=self.options.scan_root,
            run_user=self.run_user,
            python=self.options.python or "/usr/bin/python3",
        )

        existing = None
        if self.unit_path.is_file():
            try:
                existing = self.unit_path.read_text(encoding="utf-8")
            except OSError:
                existing = None

        if existing == content:
            self.report.skip("systemd 单元已是最新: %s" % self.unit_path)
        elif self.report.dry_run:
            self.report.plan("写入 %s" % self.unit_path)
        else:
            self.unit_path.parent.mkdir(parents=True, exist_ok=True)
            # 和 agent.ini 同理：systemd 单元文件里多一个 \r 会直接解析失败
            with self.unit_path.open("w", encoding="utf-8", newline="\n") as _handle:
                _handle.write(content)
            self.report.action("已写入 %s" % self.unit_path)

            if _which("systemctl"):
                run(["systemctl", "daemon-reload"], check=False)
                run(["systemctl", "enable", self.service_name], check=False)
                self.report.action("已启用开机自启")
            else:
                self.report.warn("找不到 systemctl，请手工启用服务")

    def install_enroll_unit(self, has_bootstrap_key: bool) -> None:
        """装"以 root 身份注册一次"的单元。

        注册只有这一条路：密钥是 root 只读的，而 Agent 服务以选手身份运行、
        读不到它。所以必须有一个 root 身份的一次性单元把凭据换回来。

        没有密钥时**不留**这个单元：它会每次开机失败一次，把 journal 刷脏，
        而真正的问题是"这台机器没有密钥，注册不了"。装密钥之后再跑一遍安装器
        即可 —— 那时它会自己补上。
        """
        self.enroll_unit_path = self.unit_path.parent / ENROLL_UNIT_FILENAME
        self.report.section("注册单元")

        if not has_bootstrap_key:
            # 没有密钥就没有注册可做。留一个永远失败的单元只会掩盖真正的问题。
            self._remove_enroll_unit()
            self.report.skip("没有统一密钥，装不出注册单元")
            self.report.note(
                "拿到密钥后重跑安装器：install.py --bootstrap-key <密钥> ..."
            )
            return

        content = render_enroll_unit(
            prefix=self.prefix,
            config_path=self.config_path,
            state_dir=self.state_dir,
            run_user=self.run_user,
            python=self.options.python or "/usr/bin/python3",
        )

        existing = None
        if self.enroll_unit_path.is_file():
            try:
                existing = self.enroll_unit_path.read_text(encoding="utf-8")
            except OSError:
                existing = None

        if existing == content:
            self.report.skip("注册单元已是最新: %s" % self.enroll_unit_path)
        elif self.report.dry_run:
            self.report.plan("写入 %s" % self.enroll_unit_path)
        else:
            self.enroll_unit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.enroll_unit_path.open("w", encoding="utf-8", newline="\n") as _handle:
                _handle.write(content)
            self.report.action("已写入 %s" % self.enroll_unit_path)

            if _which("systemctl"):
                run(["systemctl", "daemon-reload"], check=False)
                run(["systemctl", "enable", ENROLL_UNIT_FILENAME], check=False)
                self.report.action("已启用开机注册")
            else:
                self.report.warn("找不到 systemctl，请手工启用 %s" % ENROLL_UNIT_FILENAME)

    def _remove_enroll_unit(self) -> None:
        if not self.enroll_unit_path.is_file() or self.report.dry_run:
            return
        try:
            if _which("systemctl"):
                run(["systemctl", "disable", ENROLL_UNIT_FILENAME], check=False)
            self.enroll_unit_path.unlink()
            self.report.action("已删除不再需要的注册单元")
        except OSError as exc:
            self.report.warn("删除注册单元失败：%s" % exc)

    def _write_bootstrap_key(self, raw: str) -> bool:
        """把统一密钥内容落成 ``<config-dir>/bootstrap.key``，0600 且属主 root。

        **先建成 0600 再写内容**：反过来的话，从"文件出现"到"chmod 收紧"之间会
        有一瞬间是宽权限，而这份密钥能注册整间机房。返回 False 表示没写成功。
        """
        target = self.config_dir / BOOTSTRAP_KEY_FILENAME
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 先建成 0600 再写内容，避免有一瞬间是宽权限
            target.touch(mode=0o600, exist_ok=True)
            os.chmod(str(target), 0o600)
            with target.open("w", encoding="utf-8", newline="\n") as _handle:
                _handle.write(raw + "\n")
        except OSError as exc:
            self.report.warn("写入 %s 失败：%s" % (target, exc))
            return False

        # 明确把属主钉成 root：安装器可能以普通用户加 sudo 跑，
        # 而这份文件必须只有 root 读得到
        try:
            shutil.chown(str(target), user="root", group="root")
        except (OSError, LookupError) as exc:
            # 用户不存在（非 Linux 上测试）不算致命，但要说出来：
            # 静默留一份属主不对的密钥，等于把"能注册整间机房"的钥匙交出去
            self.report.warn(
                "无法把 %s 的属主改为 root:root：%s\n"
                "  请手工执行：chown root:root %s && chmod 0600 %s"
                % (target, exc, target, target)
            )

        self.report.action("已写入 %s（0600，属主 root）" % target)
        self.report.note("密钥能注册整间机房，所以只给 root 读 —— 不要放进 agent.ini")
        return True

    def _read_bundled_bootstrap_key(self, source: Optional[Path]) -> Optional[str]:
        """从**这次拿到的本地来源**里读包内附带的 ``bootstrap.key``；没有就 ``None``。

        刻意**直接读来源**，而不是看 ``releases/<版本>/bootstrap.key``：

        现场踩过一次 —— 用户勾了"附带密钥"重新构建、铺开了**同一个版本号**，
        安装器看到 ``releases/<版本>/`` 已存在就"跳过解压"，于是那份密钥永远
        落不到磁盘，"没有统一密钥，装不出注册单元"。所以这里不依赖解包这一步：

        * ``source`` 是目录（``--from-dir``）→ 目录根下的 ``bootstrap.key``；
        * ``source`` 是文件（tar.gz）→ **在归档里按成员名找，只在内存里读**，
          绝不把它解到磁盘上。

        "只读本地来源"同样是安全底线：密钥**绝不允许**为了它去访问网络。
        """
        if source is None:
            return None
        path = Path(source)

        if path.is_dir():
            candidate = path / BOOTSTRAP_KEY_FILENAME
            if not candidate.is_file():
                return None
            try:
                return candidate.read_text(encoding="utf-8").strip() or None
            except (OSError, UnicodeDecodeError) as exc:
                self.report.warn("包内附带的 %s 读不出来，忽略：%s" % (candidate, exc))
                return None

        if not path.is_file():
            return None

        try:
            with tarfile.open(str(path), mode="r:*") as archive:
                member = None
                for candidate in archive.getmembers():
                    # 契约是"tar 根目录下的 bootstrap.key"；`./bootstrap.key` 也认
                    if candidate.name in (
                        BOOTSTRAP_KEY_FILENAME,
                        "./" + BOOTSTRAP_KEY_FILENAME,
                    ):
                        member = candidate
                        break
                if member is None or not member.isfile():
                    return None
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - 上面已确认是普通文件
                    return None
                with handle:
                    # 上限只是防一个坏归档里有超大成员；密钥本体只有几十字节
                    payload = handle.read(64 * 1024)
        except (tarfile.TarError, OSError) as exc:
            self.report.warn(
                "从安装包里读 %s 失败，忽略：%s" % (BOOTSTRAP_KEY_FILENAME, exc)
            )
            return None

        try:
            return payload.decode("utf-8").strip() or None
        except UnicodeDecodeError as exc:
            self.report.warn(
                "安装包内的 %s 不是 UTF-8，忽略：%s" % (BOOTSTRAP_KEY_FILENAME, exc)
            )
            return None

    def _drop_extracted_bootstrap_key(self, version: Optional[str]) -> None:
        """清掉**解包时**落进 ``releases/<版本>/`` 的那份密钥。

        ``releases/<版本>/`` 对选手账号可读，而这份密钥能注册整间机房 ——
        解包产物不能留在那儿。注意只清这一个路径：``--from-dir`` 给的**源目录**
        是操作员自己的目录，不能动。

        同版本已安装时根本没解包，这里自然是空操作；读密钥已经改成直接读来源
        （见 :meth:`_read_bundled_bootstrap_key`），不再依赖这一步。
        """
        if not version:
            return
        stale = self.prefix / RELEASES_DIR / version / BOOTSTRAP_KEY_FILENAME
        if not stale.is_file():
            return
        if self.report.dry_run:
            self.report.plan("删除 %s（解包残留，不能留在选手可读的版本目录里）" % stale)
            return
        try:
            stale.unlink()
        except OSError as exc:
            self.report.warn("删除 %s 失败：%s（它留在选手可读的版本目录里）" % (stale, exc))
            return
        self.report.action("已删除 %s（解包残留，不能留在选手可读的版本目录里）" % stale)

    def install_bootstrap_key(
        self, version: Optional[str] = None, source: Optional[Path] = None
    ) -> bool:
        """把统一密钥放到配置目录，**0600 且属主 root**。

        密钥由教师在服务端签发（``syncoj-server bootstrap-key issue``）。三个来源，
        按优先级：

        1. ``--bootstrap-key`` / ``--bootstrap-key-file`` 显式给的（最高）；
        2. 目标机上已经存在的 ``<config-dir>/bootstrap.key``（重复装机时的常态）；
        3. **这次拿到的安装包里附带的 ``bootstrap.key``**（``source``：tar.gz 或
           ``--from-dir`` 目录）—— 包自带注册凭证。

        ``source`` 必须**直接读**，不能等"解包到 ``releases/<版本>/``"：同版本
        已安装时 ``install_release`` 会跳过解压，那条路根本走不到（现场踩过）。

        **安全底线：密钥只能来自这三个本地来源，绝不允许为了它去访问网络**
        （比如"问服务端要一把"）。那等于任何人只要连得到服务端就能拿到注册凭证，
        这把密钥"只随镜像走、不随网络流动"的设计就白做了。

        它放在 ``<config-dir>/`` 而不是 ``agent.ini`` 里，原因是那个文件 chown 给了
        选手账号 —— 学生读得到里面的每一个字节。

        返回值是"这台机器现在有没有可用的密钥"，**不是**"这次有没有写"：
        重复执行安装器时通常不会再传一遍密钥（那是个一眼都不该多看的秘密），
        如果这里返回 False，``install_enroll_unit`` 就会把上一次装好的
        开机注册单元删掉 —— 表现是"什么都没改，但下次开机不再注册了"。
        """
        target = self.config_dir / BOOTSTRAP_KEY_FILENAME
        raw = (self.options.bootstrap_key or "").strip()

        # 1) 显式给的（明文 --bootstrap-key，或 --bootstrap-key-file 的内容）
        if raw:
            self.report.section("统一注册密钥")
            if self.report.dry_run:
                self.report.plan("写入 %s（0600，属主 root）" % target)
                self._drop_extracted_bootstrap_key(version)
                return True
            written = self._write_bootstrap_key(raw)
            self._drop_extracted_bootstrap_key(version)
            return written

        # 2) 没给密钥 ≠ 没有密钥：上一次装机可能已经放好了一份
        if target.is_file():
            self._drop_extracted_bootstrap_key(version)
            return True

        # 3) 安装包里附带的那份（直接读本地来源，绝不联网去要）
        bundled_raw = self._read_bundled_bootstrap_key(source)
        if not bundled_raw:
            # 包内没有（或读不出来）→ 与从前一样：这台机器没有密钥
            self._drop_extracted_bootstrap_key(version)
            return False

        message = "使用安装包内附带的统一注册密钥"
        if version:
            message += "（版本 %s 附带）" % version

        self.report.section("统一注册密钥")
        if self.report.dry_run:
            self.report.plan(message)
            self.report.plan("写入 %s（0600，属主 root）" % target)
            self._drop_extracted_bootstrap_key(version)
            return True

        self.report.action(message)
        self.report.note(
            "它等于一张“能注册进这台服务端”的通行证 —— "
            "这个包不要给选手、不要放在学生读得到的地方"
        )
        written = self._write_bootstrap_key(bundled_raw)
        # 解包时落进版本目录的那份也要清掉：那个目录对选手账号可读，
        # 留着的危害比"这次没装上密钥"大得多（后者重跑一次就能补）。
        self._drop_extracted_bootstrap_key(version)
        return written

    def check_identity_leftovers(self) -> None:
        """建镜像时：状态目录里不该有凭据/身份文件。

        这是**最容易犯也最难查**的一个错误：在母机上装好并跑过一次 Agent，
        然后把整机做成镜像。这样每一台克隆机都带着**同一个身份**开机，
        服务端会看到一批同 ID 的机器互相覆盖，而文件是静默错的。

        我们不自动删除（那可能真的删掉一台在用机器的凭据），只大声说出来。
        """
        found = [
            str(entry)
            for entry in (
                self.state_dir / "credential.json",
                self.state_dir / "machine_uuid",
            )
            if entry.exists()
        ]
        if not found:
            return

        self.report.warn(
            "状态目录里已经有身份文件：\n"
            + "\n".join("    %s" % name for name in found)
            + "\n  如果这是**在母机上建镜像**，请务必在克隆之前删掉它们，"
            "否则每一台克隆机都会带着同一个身份开机、互相覆盖。\n"
            "  命令：rm -f %s/credential.json %s/machine_uuid"
            % (_posix(self.state_dir), _posix(self.state_dir))
        )

    def start_service(self, restarted: bool) -> None:
        if self.options.skip_service or self.report.dry_run:
            return
        if not _which("systemctl"):
            self.report.warn("找不到 systemctl，请手工启动服务")
            return

        self.report.section("启动服务")
        action = "restart" if restarted or _service_active(self.service_name) else "start"
        result = run(["systemctl", action, self.service_name], check=False)
        if result == 0:
            self.report.action("已 %s %s" % (action, self.service_name))
            # `systemctl start` 返回 0 只代表进程被拉起来了（Type=simple），
            # 它随后立刻退出时会一直 auto-restart。把排查入口写在回执里，
            # 现场就不用猜"服务到底起没起来"。
            self.report.note(
                "若状态一直是 activating (auto-restart)，先看 "
                "'journalctl -u %s -n 30'：多半是沙箱路径不存在（226/NAMESPACE）"
                "或配置有问题" % self.service_name
            )
        else:
            self.report.warn(
                "systemctl %s 失败（退出码 %s）。先看 'journalctl -u %s -n 30' —— "
                "如果状态是 activating (auto-restart)，多半是单元里的沙箱路径在"
                "真机上不存在（226/NAMESPACE）或配置有问题"
                % (action, result, self.service_name)
            )

    # ---------------------------------------------------------------- #
    # 主流程
    # ---------------------------------------------------------------- #

    def run(self) -> int:
        self.preflight()
        self.ensure_user()
        self.ensure_dirs()

        source = self.fetch_bundle()
        version = self.install_release(source)
        self.activate(version)

        # 建镜像时最容易犯的错：在母机上跑过一次 Agent 就把整机做成镜像。
        # 那样每台克隆机都带着同一个身份开机、互相覆盖，而且完全静默。
        # 放在写配置之前检查 —— 那之后我们就要往状态目录里写东西了。
        self.check_identity_leftovers()

        # 注意这个返回值是"这台机器现在有没有可用的密钥"，不是"这次有没有写" ——
        # 重复执行安装器时通常不会再传一遍密钥，见 install_bootstrap_key。
        # 必须把 source 传进去：同版本已安装时 install_release 会跳过解压，
        # 包内那份 bootstrap.key 只能从**下载下来的那个包**里直接读。
        has_bootstrap_key = self.install_bootstrap_key(version, source)

        self.write_config(version)
        self.install_unit(version)
        self.install_enroll_unit(has_bootstrap_key=has_bootstrap_key)
        self.start_service(restarted=True)

        self.report.section("完成")
        if self.report.dry_run:
            self.report.note("以上为计划，未做任何改动（--dry-run）")
        else:
            self.report.note("共 %d 处改动" % self.report.changes)
            self.report.note("查看状态: systemctl status %s" % self.service_name)
            self.report.note("查看日志: journalctl -u %s -n 30" % self.service_name)
        return EXIT_OK

    # ---------------------------------------------------------------- #
    # 卸载
    # ---------------------------------------------------------------- #

    def uninstall(self) -> int:
        """把安装器装到这台机器上的东西**按顺序**卸掉，返回退出码。

        ## 它会删掉哪些凭据（这是"卸载必须 --yes"的全部理由）

        * ``<config-dir>/bootstrap.key`` —— **统一注册密钥**：一张"能注册进这台
          服务端"的通行证。它泄露 = 别人能把任意一台机器注册进来。
        * ``<config-dir>/release-key.pub.json`` —— 升级信任锚（本身不是秘密，
          但删掉之后自更新会整体失效）。
        * ``<state-dir>/credential.json``、``machine_uuid`` —— 本机身份与长期
          凭据，也就是这台机器在服务端那边的"户口"。

        所以默认删除是**不可逆**的：不看一眼就删掉，事后只能重新走一遍注册。

        ## 明确不动的东西

        选手桌面上的下发文件、``/tmp`` 下的临时目录、运行用户的家目录都不属于
        Agent 本身 —— 一个卸载工具顺手删掉学生的作业是灾难性的，所以它们都不在
        删除范围内（结尾会把这几条再明说一遍）。

        ## 幂等

        没装过的机器照样返回成功，并明确说"什么都没做" —— 卸载脚本被重复执行
        是常态（重装之前先清一遍），它不该在那时变成一条错误。
        """
        self.report.section("卸载 SyncOJ Agent")
        self._require_uninstall_confirmation()

        # 删目录之前先把"运行账号是不是我建的"读出来 —— 状态目录马上会被删掉。
        # 没有证据（老版本装的机器、或运行账号本来就是人的账号）→ 一律不删账号。
        created_user = self._user_created_by_installer()

        removed = False
        failed = False
        for did, bad in (
            self._uninstall_units(),
            self._remove_tree(
                self.prefix, "安装根目录", UNINSTALL_PREFIX_MARKERS
            ),
            self._remove_tree(
                self.config_dir,
                "配置目录（含统一注册密钥与升级信任锚）",
                UNINSTALL_CONFIG_MARKERS,
            ),
            self._uninstall_state(),
            self._remove_user(created_user),
        ):
            removed = removed or did
            failed = failed or bad

        self.report.section("卸载结果")
        if not removed:
            # 幂等：没装过也**成功**，而且必须明说，否则现场会以为"是不是卡住了"
            self.report.note("这台机器上没有装 Agent，什么都没做")
        elif self.report.dry_run:
            self.report.note("以上为将要卸载的内容（--dry-run，未做任何改动）")
        else:
            self.report.note("已按上面列出的每一项卸载完毕")

        if removed:
            # 这两份就是"卸载必须 --yes"的全部理由：删掉之后只能重新注册/配信任锚
            self.report.note(
                "这条卸载路径涉及两份凭据：%s（统一注册密钥）、%s（升级信任锚）"
                % (
                    self.config_dir / BOOTSTRAP_KEY_FILENAME,
                    self.config_dir / PUBLIC_KEY_FILENAME,
                )
            )
            if not self.options.keep_state:
                self.report.note(
                    "以及本机身份与长期凭据：%s/credential.json、machine_uuid"
                    % _posix(self.state_dir)
                )
        self.report.note(
            "明确未动：选手桌面上的下发文件、/tmp 下的临时目录、运行用户的家目录"
            "都不属于 Agent 本身，未删除。"
        )
        if self.report.dry_run:
            self.report.note("以上为计划，未做任何改动（--dry-run）")
        return EXIT_ERROR if failed else EXIT_OK

    def _require_uninstall_confirmation(self) -> None:
        """卸载前的确认：非交互必须 ``--yes``，交互要把名字原样打一遍。

        ``curl … | sh -s -- --uninstall`` 这种管道里 stdin 不是终端（``input``
        会立刻 EOF），所以**必须**要求 ``--yes``；否则一台机器可能在没人看着的
        时候被拆掉，而删掉的凭据是恢复不了的。

        交互时的规矩跟仓库里"结构性删除要打名字"（服务端 ``require_confirm``）
        一致：不是问"是否确定"，而是**把名字打一遍** —— "是"会被手指肌肉记忆点掉。
        """
        if self.report.dry_run:
            self.report.note("预览模式：只列出将会停止/删除哪些单元与路径，不做任何改动")
            return
        if self.options.yes:
            self.report.note("已确认（--yes）")
            return
        if not sys.stdin.isatty():
            raise InstallError(
                "卸载是**不可逆**操作：会删掉本机凭据（统一注册密钥、本机身份），"
                "删掉之后只能重新注册。当前 stdin 不是终端，无法交互确认 ——\n"
                "  这是不可逆操作，确认请显式加 --yes：\n"
                "  sudo python3 install.py --uninstall --yes"
            )
        # 交互：把名字原样打一遍（见上面 docstring）
        self.report.note(
            "这将删除 Agent、配置目录（含统一注册密钥）与状态目录（含本机凭据）。"
        )
        try:
            answer = input(
                "要卸载的是 Agent「%s」，请把它原样输一遍再确认（输入别的会取消）: "
                % self.service_name
            )
        except EOFError:
            raise InstallError("读不到确认输入，已取消，未做任何改动")
        if answer.strip() != self.service_name:
            raise InstallError("确认不匹配，已取消，未做任何改动")
        self.report.note("已确认（手工输入 %s）" % self.service_name)

    @staticmethod
    def _marker_present(directory: Path, marker: str) -> bool:
        candidate = directory / marker
        # is_symlink 也要看：``current`` 是符号链接，断链时 exists() 会给 False
        return candidate.exists() or candidate.is_symlink()

    def _looks_like_sync_oj(self, path: Path, markers: "Tuple[str, ...]") -> bool:
        """这个目录看起来是不是 SyncOJ 装出来的？

        空目录算"是" —— 删掉一个空目录没有任何信息损失。非空但不能确认时
        **一律跳过**：``--prefix`` 是用户可覆盖的，把它指到别处时宁可不卸，
        也不能因为"路径长得对"就把人家的目录清了。
        """
        if not path.is_dir():
            return False
        try:
            entries = list(path.iterdir())
        except OSError:
            return False
        if not entries:
            return True
        return any(self._marker_present(path, marker) for marker in markers)

    def _remove_tree(
        self, path: Path, label: str, markers: "Tuple[str, ...]"
    ) -> "Tuple[bool, bool]":
        """删掉一个安装器建的目录。返回 ``(是否删了/会删, 是否出错)``。"""
        self.report.section("删除%s" % label)
        if not path.exists() and not path.is_symlink():
            self.report.skip("%s不存在，跳过：%s" % (label, path))
            return False, False
        if not self._looks_like_sync_oj(path, markers):
            self.report.warn(
                "%s 看起来不是 SyncOJ 的安装（缺少 %s 之类的标记），"
                "为免误删已跳过：%s" % (label, " / ".join(markers), path)
            )
            return False, False
        if self.report.dry_run:
            self.report.plan("删除 %s" % path)
            return True, False
        try:
            shutil.rmtree(str(path))
        except OSError as exc:
            self.report.warn("删除 %s 失败：%s" % (path, exc))
            return False, True
        self.report.action("已删除 %s" % path)
        return True, False

    def _uninstall_state(self) -> "Tuple[bool, bool]":
        """状态目录：本机凭据与日志都在这里，``--keep-state`` 可以留下它。"""
        if self.options.keep_state:
            self.report.section("状态目录")
            self.report.skip("保留状态目录 %s（--keep-state）" % self.state_dir)
            self.report.note("日志也在这里；要复盘请先把它拷走")
            return False, False
        return self._remove_tree(
            self.state_dir, "状态目录（含本机凭据与日志）", UNINSTALL_STATE_MARKERS
        )

    def _uninstall_units(self) -> "Tuple[bool, bool]":
        """停掉并删掉两个单元：Agent 本体与"以 root 注册一次"的那个。

        单元名一律从 ``UNIT_FILENAME`` / ``ENROLL_UNIT_FILENAME`` 派生（就是
        ``unit_path.name``），不写死字符串 —— 改名时漏掉一处，现场表现是
        "卸载说删了，可开机还在跑"。
        """
        self.report.section("停止并删除 systemd 单元")
        units = [
            self.unit_path,  # syncoj-agent.service
            self.unit_path.parent / ENROLL_UNIT_FILENAME,  # syncoj-agent-enroll.service
        ]
        removed = False
        failed = False
        for unit in units:
            if not unit.is_file():
                self.report.skip("单元不存在，跳过：%s" % unit)
                continue
            removed = True
            if self.report.dry_run:
                self.report.plan("systemctl disable --now %s" % unit.name)
                self.report.plan("删除 %s" % unit)
                continue
            if _which("systemctl"):
                result = run(["systemctl", "disable", "--now", unit.name], check=False)
                if result == 0:
                    self.report.action("已停止并禁用 %s" % unit.name)
                else:
                    self.report.warn(
                        "systemctl disable --now %s 失败（退出码 %s），继续删单元文件"
                        % (unit.name, result)
                    )
            else:
                self.report.warn("找不到 systemctl，跳过停止/禁用，直接删单元文件")
            try:
                unit.unlink()
            except OSError as exc:
                self.report.warn("删除 %s 失败：%s" % (unit, exc))
                failed = True
                continue
            self.report.action("已删除 %s" % unit)
        if removed and not self.report.dry_run and _which("systemctl"):
            run(["systemctl", "daemon-reload"], check=False)
            self.report.action("已执行 systemctl daemon-reload")
        return removed, failed

    def _remove_user(self, created_user: Optional[str] = None) -> "Tuple[bool, bool]":
        """删运行账号，**只删本安装器建过的那个**；``--keep-user`` 仍然跳过。

        ``--keep-user`` 不再是唯一保险：默认运行账号是"跑安装的那个人自己的
        账号"，误删是灾难级的。所以判据是硬证据 —— 安装时写下的
        :data:`CREATED_USER_MARKER_FILENAME`。没有证据（老版本装的机器、或本来
        就是人的账号）**一律不删**，只在回执里说明。

        **刻意不用 ``userdel -r``**：安装器建账号时是 ``--no-create-home``，
        而 home-dir 恰好被设成了状态目录 —— ``-r`` 会把状态目录（含日志、凭据）
        一起删掉。而且"这个家目录到底是不是安装器建的"在这里也无从确认，
        删别人的数据是不可逆的。
        """
        self.report.section("运行账号")
        if self.options.keep_user:
            self.report.skip("保留运行账号 %s（--keep-user）" % self.run_user)
            return False, False
        if not _user_exists(self.run_user):
            self.report.skip("运行账号不存在，跳过：%s" % self.run_user)
            return False, False
        if created_user != self.run_user:
            self.report.skip(
                "未删除运行账号 %s —— 它不是本安装器建的（只删自己建的账号）"
                % self.run_user
            )
            self.report.note(
                "它是人自己的账号，删掉不可逆。确实要删请手工执行："
                "userdel %s（不会动家目录）" % self.run_user
            )
            return False, False
        if self.report.dry_run:
            self.report.plan(
                "删除运行账号 %s（状态目录里有“是安装器建的”记录；**不删**家目录）"
                % self.run_user
            )
            return True, False
        result = run(["userdel", self.run_user], check=False)
        if result != 0:
            self.report.warn(
                "删除运行账号 %s 失败（退出码 %s），请手工处理" % (self.run_user, result)
            )
            return True, True
        self.report.action("已删除运行账号 %s（安装器建的；未动家目录）" % self.run_user)
        return True, False


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #


def _posix(path: Path) -> str:
    """把路径渲染成 POSIX 形式。

    生成的 agent.ini 与 systemd 单元**只被 Linux 读取**，所以无论安装器跑在什么
    平台上，里面都必须写正斜杠。直接 ``str(Path)`` 在 Windows 上会得到
    ``\\opt\\syncoj`` —— 那样产出的配置拿到目标机上就是废的，而问题只在
    现场暴露。
    """
    return path.as_posix()


def render_config(
    server_url: str,
    verify_tls: bool,
    ca_file: str,
    bootstrap_key_file: str,
    state_dir: Path,
    deploy_root: str,
    scan_roots: str,
    scan_prefix: str,
    upgrade_mode: str,
    install_root: Path,
    public_key: str,
) -> str:
    """生成 agent.ini。

    由代码生成而不是 `sed` 替换模板：路径里可能含特殊字符，
    `sed` 会因为分隔符或转义把它们改坏，而且失败得悄无声息。

    ``deploy_root`` 与 ``scan_roots`` 是**字符串模板**（可能含
    ``{desktop}`` / ``{player_no}``），不能当成 Path 处理。

    ``bootstrap_key_file`` 必须写进去，而且必须指向**这次实际放密钥的位置**：
    它不再是"反正默认值就对"的东西 —— 自定义 ``--config-dir`` 时默认值
    ``/etc/syncoj/bootstrap.key`` 会指到一个空的路径，表现是"装完起不来、
    日志说找不到统一密钥"，而密钥明明就在旁边。
    """
    return """\
; SyncOJ Agent 配置（由安装器生成）
;
; 格式是 INI 而不是 TOML —— 目标机 NOI Linux 2.0 自带 Python 3.8，
; 而 tomllib 是 Python 3.11 才进标准库的。

[server]
url = {server_url}
verify_tls = {verify_tls}
ca_file = {ca_file}

[agent]
; 统一注册密钥文件（整间机房一份），**root 只读**。
; 密钥的**内容**绝不写进本文件：agent.ini 的属主是选手账号，
; 学生读得到里面的每一个字节，而那把钥匙能注册整间机房。
; 真正干活的是 syncoj-enroll.service（以 root 身份跑一次注册）。
bootstrap_key_file = {bootstrap_key_file}
state_dir = {state_dir}
machine_id =
; 下发文件的落地根目录。{{desktop}} 自动探测当前用户的桌面
; （兼容「桌面」与 Desktop 两种命名）。
; 最终落点：桌面/<准考证号>/<题目名>/<文件名>
deploy_root = {deploy_root}

[pairing]
; 把本机状态写到桌面上给教师看：
;   未配对 -> <桌面>/配对码.txt（六位配对码）
;   已配对但没场次 -> <桌面>/等待场次.txt
; 能干活之后这两个文件都会自动消失。桌面不可写时可以关掉它。
show_on_desktop = true
file_name = 配对码.txt

[scan]
; 要回收的代码目录。占位符在运行时展开：
;   {{desktop}}       = 当前用户的桌面（自动探测）
;   {{home}}          = 当前用户的家目录
;   {{player_no}}     = 准考证号（注册成功后才知道）
;   {{contest_slug}}  = 场次标识（注册成功后才知道）
; 默认约定：桌面/<准考证号>/<题目名>/<题目名>.cpp
roots = {scan_roots}
; 上报路径前缀：none = 不加（本机只有一个选手时推荐，否则 source/ 里
; 准考证号会出现两次）；auto = 取根目录名；其他字面量直接用
; （前缀里也能用 {{player_no}} / {{contest_slug}}）
prefix = {scan_prefix}
interval = 60
max_file_size = 2097152

[log]
level = INFO
file =
to_stderr = false

[upgrade]
; off = 只上报不下载（考场推荐）；stage = 下载验签解包但不激活；apply = 完整执行
mode = {upgrade_mode}
install_root = {install_root}
public_key = {public_key}
""".format(
        server_url=server_url,
        verify_tls="true" if verify_tls else "false",
        ca_file=ca_file,
        bootstrap_key_file=bootstrap_key_file,
        state_dir=_posix(state_dir),
        deploy_root=deploy_root,
        scan_roots=scan_roots,
        scan_prefix=scan_prefix,
        upgrade_mode=upgrade_mode,
        install_root=_posix(install_root),
        public_key=public_key,
    )


def _writable_paths(prefix: Path, state_dir: Path) -> List[str]:
    """``ReadWritePaths=`` 该开哪些目录 —— 只列**确实有理由写**的，且一律带 ``-``。

    为什么每一条都带 ``-``：systemd 设沙箱时，``ReadWritePaths=`` 里只要有一条
    路径**不存在**，整个服务就起不来，报的是 ``226/NAMESPACE`` —— 现场看到的
    只有 ``code=exited, status=226/NAMESPACE`` 和一条无尽的重启记录。带 ``-``
    的路径不存在时会被 systemd **跳过**。真机上就是这么炸的：单元里写了一条
    ``/home/student/code``，而那台机器上根本没有那个目录。

    为什么只列这三个：

    * ``%h`` —— systemd 展开成 ``User=`` 的家目录。Agent 以那个账号运行，桌面、
      配对码文件、下发落点都在它下面；不放开写权限，``ProtectSystem=strict``
      会把家目录也变成只读，表现是"服务起来了但什么都不传"。
    * 安装根目录 —— 自更新要往 ``releases/`` 里写。
    * 状态目录 —— 日志、凭据、哈希缓存。

    刻意**不**列 ``deploy_root`` / ``scan.roots`` 的具体值：它们是可选配置、
    真机上可能不存在，而默认值都在 ``%h`` 之下（已经覆盖），多列一条就多一个
    226 的机会。也刻意**不**列配置目录：Agent 只读它（写凭据的是
    ``syncoj-agent-enroll`` 那个 root 单元），列成可写等于让选手账号能改
    ``bootstrap.key`` / ``agent.ini``，那是安全降级。
    """
    writable = ["-%h", "-%s" % _posix(prefix), "-%s" % _posix(state_dir)]
    # 去重但保持顺序（--prefix 与 --state-dir 被指到同一个目录时会出现重复）
    seen = set()
    return [p for p in writable if not (p in seen or seen.add(p))]


def render_enroll_unit(
    prefix: Path,
    config_path: Path,
    state_dir: Path,
    run_user: str,
    python: str,
) -> str:
    """生成"以 root 身份注册一次"的 oneshot 单元。

    要解决的问题：统一密钥是 **root 只读** 的（它泄露等于交出"无限注册"的能力），
    而 Agent 服务以选手身份运行、根本读不到它。所以注册由这个单元做一次，
    把凭据写进状态目录并交给选手账号，之后 Agent 只读凭据。

    几个容易踩的点：

    * ``Type=oneshot`` + ``RemainAfterExit=no``：它只在开机时跑一次就好。
      **必须**在 Agent 之前跑完（``Before=``）—— 否则 Agent 先起来，发现没有
      凭据就报错退出，然后被 ``Restart=`` 拉起，日志里刷一堆没必要的失败。
    * **不要 ``Restart=``**：注册失败的原因多半是服务端没起、密钥被吊销、
      网还没通。无限重试只会把日志刷满，``systemctl status`` 里反而看不清原因。
      开机一次失败不致命 —— 下次开机还会再试，而且密钥没换的话它本来就会再试。
    * ``chown-to`` 交给选手：写出来的凭据属主是 root，而读它的是选手。
      不交出去的话，表现是"服务起来了但一直重新注册"，很难联想到是属主问题 ——
      我们在 ``agent.ini`` 上已经踩过一次同样的坑。
    """
    return """\
[Unit]
Description=SyncOJ Agent 注册（用镜像里的统一密钥换回本机凭据，只跑一次）
# 先有网再注册；Agent 本体必须等这一步做完
After=network-online.target
Wants=network-online.target
Before=%(service)s

[Service]
Type=oneshot
RemainAfterExit=no
User=root
# 和 Agent 本体同一条启动路径：-E -s + run_agent.py。
# 用同一份代码注册，才不会出现"装机时能注册、开机后认不出来"
ExecStart=%(python)s -E -s %(prefix)s/%(current)s/%(launcher)s \\
    --config %(config)s --provision --chown-to %(user)s
StandardOutput=journal
StandardError=journal
# 注册会读 status dir 与 /etc，不需要写系统目录
ProtectSystem=strict
ProtectControlGroups=yes
NoNewPrivileges=yes
# 带 `-`：状态目录不在时跳过这一条，而不是让整个单元起不来（226/NAMESPACE）
ReadWritePaths=-%(state)s

[Install]
WantedBy=multi-user.target
""" % {
        "service": UNIT_FILENAME,
        "python": python,
        "prefix": _posix(prefix),
        "current": CURRENT_LINK,
        "launcher": LAUNCHER_NAME,
        "config": _posix(config_path),
        "user": run_user,
        "state": _posix(state_dir),
    }


def render_unit(
    prefix: Path,
    config_path: Path,
    state_dir: Path,
    deploy_root: str,
    scan_roots: str,
    run_user: str,
    python: str,
) -> str:
    """生成 systemd 单元。

    **权限模型**：Agent 以**选手登录用户的身份**运行（而不是专用 syncoj 账号），
    因为代码与下发文件都在选手自己的桌面下，跨用户授权在现场很容易装成
    "服务起来了但什么都不传"。

    这带来一个直接后果：``ProtectHome=read-only`` 与 ``ProtectSystem=strict``
    会把家目录整个变成只读，Agent 就没法往桌面写东西了。所以：

    - **不设 ProtectHome**（Agent 本来就要读写自己的家目录）
    - ``ReadWritePaths=`` 里放**确实有理由写**的目录，且**每一条都带 ``-``**
      （下面 :func:`_writable_paths` 有完整理由）

    ``ProtectSystem=strict`` 仍然保留：它把 ``/usr``、``/etc``、``/boot`` 等
    系统目录全部只读，这才是这条指令的价值所在。

    ``deploy_root`` / ``scan_roots`` 两个参数**刻意不再写进单元**：它们是可选
    配置，真机上可能根本不存在，而 ``ReadWritePaths=`` 里只要有一条不存在的
    路径，systemd 就会在设沙箱时报 ``226/NAMESPACE``、服务永远起不来。真机上
    踩过一次：单元里有 ``/home/student/code``，而现场那台机器的选手目录不在
    那里。默认值 ``{desktop}`` / ``{desktop}/{player_no}`` 都在 ``%h`` 之下，
    本来就被覆盖；写进来只会多一条可能踩雷的指令。参数保留只是为了调用方签名
    稳定（单元内容不再依赖它们）。
    """
    writable = _writable_paths(prefix, state_dir)

    lines = [
        "[Unit]",
        "Description=SyncOJ Agent（选手端代码回收，无界面静默运行）",
        "After=network-online.target",
        "Wants=network-online.target",
        # 重启上限必须在 [Unit] 段。写在 [Service] 里的 StartLimit*Sec 会被
        # systemd 当成未知键**忽略**，于是"起不来"就变成无限重启（真机上刷到过
        # 第 28 次，日志和 CPU 都白烧）。放这里才能让它停下来等人看。
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        # 必须走 run_agent.py 而不是 syncoj_agent/main.py：
        # main.py 用包内相对导入，当脚本直接执行会
        # ImportError: attempted relative import with no known parent package。
        # 而 -E 会忽略 PYTHONPATH，没法靠环境变量把包目录告诉解释器，
        # 所以只能靠启动器显式设置 sys.path。
        #
        # -E 忽略所有 PYTHON* 环境变量，-s 忽略 user site-packages：
        # 选手怎么 pip install 都污染不到 Agent
        "ExecStart=%s -E -s %s/%s/%s --config %s"
        % (
            python,
            _posix(prefix),
            CURRENT_LINK,
            LAUNCHER_NAME,
            _posix(config_path),
        ),
        "",
        "# ---- 静默 ----",
        "StandardOutput=null",
        "StandardError=journal",
        "",
        "# ---- 自愈 ----",
        # **on-failure 而不是 always**：自卸载成功时 Agent 以 exit 0 正常退出，
        # always 会立刻把它重新拉起来 —— 而那时 /opt/syncoj、/etc/syncoj 都已经
        # 被删掉，拉起来只会刷一堆"找不到文件"，现场看起来像"卸载失败"。
        # 崩溃（非 0 退出）仍然要自愈，所以是 on-failure 不是 no；"反复起不来"
        # 的上限交给 [Unit] 段里的 StartLimitIntervalSec/Burst。
        #
        # 注意：`syncoj-agent-enroll.service`（provision 那个 oneshot）不带
        # Restart，规则与这里无关，别一起改。
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "# ---- 权限隔离 ----",
        "User=%s" % run_user,
        "Group=%s" % run_user,
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        # strict 把 /usr /etc /boot 等系统目录全部只读 —— 这才是它的价值
        "ProtectSystem=strict",
        # 刻意**不设 ProtectHome**：Agent 以选手身份运行，本来就要读写自己的桌面。
        # 设成 read-only 会让它一个文件都写不出去，表现为"服务起来了但什么都不传"。
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6",
        "LockPersonality=yes",
        "",
        # 每一条都带 `-`：路径不存在时 systemd 跳过它，而不是报 226 让服务起不来
        "ReadWritePaths=%s" % " ".join(writable),
        "",
        "# ---- 资源限制 ----",
        "MemoryMax=200M",
        "MemoryHigh=160M",
        "CPUQuota=25%",
        "IOSchedulingClass=idle",
        "IOSchedulingPriority=7",
        "LimitNOFILE=1024",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(line for line in lines if line is not None)


# --------------------------------------------------------------------------- #
# 系统辅助
# --------------------------------------------------------------------------- #


def run(cmd: List[str], check: bool = True) -> int:
    try:
        completed = subprocess.run(cmd, capture_output=True)
    except (OSError, FileNotFoundError) as exc:
        if check:
            raise InstallError("执行 %s 失败: %s" % (" ".join(cmd), exc))
        return 127
    if check and completed.returncode != 0:
        raise InstallError(
            "执行 %s 失败（退出码 %d）: %s"
            % (" ".join(cmd), completed.returncode,
               (completed.stderr or b"").decode("utf-8", "replace").strip())
        )
    return completed.returncode


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _is_root() -> bool:
    """Windows 上没有 geteuid —— 那种环境下安装器本来也跑不了（会走 --dry-run）。"""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    try:
        return geteuid() == 0
    except OSError:  # pragma: no cover
        return False


def _user_exists(name: str) -> bool:
    try:
        import pwd
    except ImportError:  # pragma: no cover - 非 POSIX
        return False
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _current_user_name() -> Optional[str]:
    """当前**有效**用户的名字；取不到（非 POSIX）返回 None。"""
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - Windows
        return None
    try:
        import pwd
    except ImportError:  # pragma: no cover - 非 POSIX
        return None
    try:
        return pwd.getpwuid(getuid()).pw_name
    except (KeyError, OSError):  # pragma: no cover - 极罕见
        return None


def default_run_user() -> str:
    """默认 Agent 以哪个账号运行：**跑安装的那个人**。

    顺序：

    1. ``SUDO_USER`` —— 用 ``sudo`` 跑安装时 sudo 会把它设成真正坐在机器前的
       那个人的名字（而有效用户是 root）；
    2. 当前有效用户（``os.getuid()`` → ``pwd``）；
    3. 兜底 ``DEFAULT_RUN_USER``（只在非 POSIX 的构建机/dry-run 上会用到）。

    为什么必须是"那个人"：Agent 要读的是**选手桌面上的文件**，而桌面是那个人
    家目录下的东西。安装器自己新建一个专用账号（``syncoj``）的话，它的家目录
    其实是状态目录 —— 读不到选手的桌面，表现是"服务起来了但扫描/下发全是空的"。
    而且专用账号还多出"卸载时可能误删人账号"的风险，见 :data:`CREATED_USER_MARKER_FILENAME`。
    """
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if sudo_user:
        return sudo_user
    return _current_user_name() or DEFAULT_RUN_USER


def _home_of(user: str) -> Optional[Path]:
    """某个系统账号的家目录；查不到（非 POSIX / 账号不存在）返回 None。"""
    try:
        import pwd
    except ImportError:  # pragma: no cover - 非 POSIX
        return None
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except (KeyError, OSError):
        return None


def _desktop_from_xdg(home: Path) -> Optional[Path]:
    """读 ``~/.config/user-dirs.dirs`` 里的 ``XDG_DESKTOP_DIR``（最权威）。"""
    path = home / ".config" / "user-dirs.dirs"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("XDG_DESKTOP_DIR"):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip("\"'").replace("${HOME}", str(home)).replace("$HOME", str(home))
        if value:
            return Path(value)
    return None


def _desktop_of(home: Path) -> Path:
    """某个账号家目录下的桌面。

    与 ``syncoj_agent/state.py::detect_desktop`` 同一套顺序 —— 安装器**不能**
    import 那段代码（单文件自包含，目标机上此刻还没有 Agent 包），所以刻意重写
    一遍，与"安全解包"同一个先例。找不到实际存在的桌面时按中文环境猜 ``~/桌面``：
    退回家目录会让 Agent 把整个家目录当成工作区扫。
    """
    from_xdg = _desktop_from_xdg(home)
    if from_xdg is not None:
        return from_xdg
    for name in DESKTOP_CANDIDATES:
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / "桌面"


def _python_ok(python: str) -> bool:
    try:
        completed = subprocess.run(
            [python, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 3)"],
            capture_output=True,
        )
        return completed.returncode == 0
    except (OSError, FileNotFoundError):
        return False


def _service_active(name: str) -> bool:
    try:
        completed = subprocess.run(
            ["systemctl", "is-active", "--quiet", name], capture_output=True
        )
        return completed.returncode == 0
    except (OSError, FileNotFoundError):
        return False


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="SyncOJ Agent 幂等安装器（三个入口共用同一份逻辑）",
    )
    parser.add_argument("--version", action="store_true", help="打印安装器版本后退出")

    source = parser.add_argument_group("安装包来源（三选一）")
    source.add_argument("--bundle", help="本地 tar.gz 安装包（离线安装）")
    source.add_argument("--download-url", help="下载安装包的 URL（在线自举）")
    source.add_argument(
        "--from-server",
        action="store_true",
        help=(
            "直接从服务端拿**当前已铺开**的 Agent 版本（装机入口，不鉴权）。"
            "地址取 --server；不给 --server 时会去局域网里找（那时必须给 "
            "--public-key 才能验证应答）。有公钥就验签，没有就只验 sha256 并如实警告。"
        ),
    )
    source.add_argument("--from-dir", help="已解开的 Agent 目录（镜像预装）")
    source.add_argument("--sha256", default="", help="安装包 sha256，用于完整性校验")

    config = parser.add_argument_group("配置")
    config.add_argument(
        "--server",
        help=(
            "服务端地址，如 https://10.0.0.1:8443。**通常不用传**：安装包里内嵌了"
            "服务端自己写的地址；包没带的话会先在局域网里问一次（应答要验签）。"
            "传了就完全听这个。"
        ),
    )
    config.add_argument(
        "--no-discover",
        action="store_true",
        help="跳过局域网发现。包内地址与 --server 都没有时就直接用默认值并警告。",
    )
    config.add_argument(
        "--discover-address",
        default=None,
        help="直接问这个地址而不是广播（有些交换机禁广播）。",
    )
    config.add_argument(
        "--discover-timeout",
        type=float,
        default=1.5,
        help="等发现应答的秒数，默认 1.5。",
    )
    config.add_argument(
        "--bootstrap-key",
        help=(
            "镜像统一注册密钥（syncoj-server bootstrap-key issue 签发的）。"
            "写入 <config-dir>/bootstrap.key（0600，属主 root），"
            "并装上开机注册单元。**这是唯一的注册方式** —— 已经是密钥的"
            "机器不必再传一遍，省略它不会影响已有的密钥。"
        ),
    )
    config.add_argument(
        "--bootstrap-key-file",
        default=None,
        help=(
            "从文件读统一注册密钥。不指定时，如果正从源码仓库里跑，会自动用 "
            "<仓库>/.key/bootstrap.key（syncoj-server init 生成的位置）；"
            "都不是就与从前一样（不写密钥）。--bootstrap-key 优先于它。"
        ),
    )
    config.add_argument(
        "--scan-root",
        default=DEFAULT_SCAN_ROOT,
        help="选手代码目录；支持 {desktop} 与 {player_no} 占位符，多个用逗号分隔"
        "（默认 %s）" % DEFAULT_SCAN_ROOT,
    )
    config.add_argument(
        "--scan-prefix",
        default=DEFAULT_SCAN_PREFIX,
        help="上报路径前缀：none=不加（默认）、auto=取根目录名、其他字面量",
    )
    config.add_argument(
        "--deploy-root",
        default=DEFAULT_DEPLOY_ROOT,
        help="下发文件落地根目录；支持 {desktop} 占位符（默认 %s）" % DEFAULT_DEPLOY_ROOT,
    )
    config.add_argument("--ca-file", default="", help="自签 CA 证书路径")
    config.add_argument("--insecure", action="store_true",
                        help="不校验服务端证书（仅调试用，会让中间人读走凭据）")
    config.add_argument("--force-config", action="store_true",
                        help="覆盖已存在的 agent.ini（默认保留教师的手工修改）")

    upgrade = parser.add_argument_group("自更新")
    upgrade.add_argument("--upgrade-mode", default="off", choices=["off", "stage", "apply"],
                         help="off=不自动升级（默认）")
    upgrade.add_argument("--public-key", default="", help="发布签名公钥路径")

    layout = parser.add_argument_group("布局（测试与定制用）")
    layout.add_argument("--prefix", default=str(DEFAULT_PREFIX))
    layout.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    layout.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    layout.add_argument("--unit-dir", default="/etc/systemd/system")
    layout.add_argument("--service-name", default=SERVICE_NAME)
    layout.add_argument(
        "--user",
        default=default_run_user(),
        help=(
            "Agent 以哪个账号运行。**默认就是跑安装的那个人**（sudo 时为 SUDO_USER，"
            "否则为当前用户）—— 因为选手桌面在他的家目录下。显式指定一个不存在的"
            "系统账号时才会创建它；卸载只删这种“安装器建的”账号。"
        ),
    )
    layout.add_argument("--python", default="", help="Agent 使用的 python3 路径")

    behaviour = parser.add_argument_group("行为")
    behaviour.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何改动")
    behaviour.add_argument("--quiet", action="store_true")
    behaviour.add_argument("--skip-user", action="store_true", help="不创建系统用户")
    behaviour.add_argument("--skip-service", action="store_true", help="不碰 systemd")
    behaviour.add_argument("--skip-python-check", action="store_true")

    removal = parser.add_argument_group("卸载")
    removal.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "卸载 Agent：停掉并删除两个 systemd 单元，删除安装/配置/状态目录，"
            "最后删除运行用户。默认**全卸**（不可逆，会删掉本机凭据）。"
        ),
    )
    removal.add_argument(
        "--yes",
        action="store_true",
        help="确认卸载。stdin 不是终端（比如 curl | sh）时**必须**显式加上。",
    )
    removal.add_argument("--keep-user", action="store_true", help="卸载时保留运行用户")
    removal.add_argument(
        "--keep-state",
        action="store_true",
        help="卸载时保留状态目录（日志也在这里，要复盘就先拷贝）",
    )
    return parser


def resolve_bootstrap_key(options: argparse.Namespace, report: Reporter) -> Optional[str]:
    """定出这次该写进机器的统一注册密钥；没有就返回 ``None``。

    优先级：``--bootstrap-key``（明文，最高）> ``--bootstrap-key-file`` >
    ``<仓库>/.key/bootstrap.key``（存在才用）> 没有。

    这里只解决**显式/仓库**来源；机器上已有的那份与安装包内附带的
    ``bootstrap.key`` 由 :meth:`Installer.install_bootstrap_key` 接着往下找，
    整体顺序是"显式 > 机器上已有 > 包内附带"，且**全部都是本地来源**。

    最后那条默认值是这次改动里唯一"行为变了"的地方，所以它有两个约束：
    直接传明文仍然完全优先；不在源码仓库里跑（安装器被拷进镜像/安装包之后
    ``REPO_ROOT`` 就指着别处了）时那个路径**根本不存在**，于是行为和以前一样。

    读文件失败**不**当成致命错误：装机现场最常见的场景是密钥文件还没放好，
    这时正确的结果是"装好了但没有密钥、下次补一个"，而不是整个安装中断。
    """
    if options.bootstrap_key:
        return options.bootstrap_key.strip()

    path: Optional[Path] = None
    if options.bootstrap_key_file:
        path = Path(options.bootstrap_key_file).expanduser()
    elif DEFAULT_BOOTSTRAP_KEY_FILE.is_file():
        path = DEFAULT_BOOTSTRAP_KEY_FILE
        report.note("统一密钥取自仓库默认位置 %s（可用 --bootstrap-key-file 覆盖）"
                    % path)

    if path is None:
        return None

    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        report.warn("读取统一密钥文件失败：%s（%s）" % (path, exc))
        return None

    if not raw:
        report.warn("统一密钥文件是空的：%s" % path)
        return None
    return raw


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print("SyncOJ installer 1.0")
        return EXIT_OK

    report = Reporter(dry_run=args.dry_run, quiet=args.quiet)

    if args.uninstall:
        installer = Installer(args, report)
        try:
            return installer.uninstall()
        except InstallError as exc:
            print("\n卸载失败：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        except KeyboardInterrupt:  # pragma: no cover
            print("\n已中断", file=sys.stderr)
            return EXIT_ERROR

    args.bootstrap_key = resolve_bootstrap_key(args, report)
    installer = Installer(args, report)

    try:
        return installer.run()
    except InstallError as exc:
        print("\n安装失败：%s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
