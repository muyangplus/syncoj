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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Tuple

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
        if self.options.skip_user:
            self.report.skip("跳过用户创建（--skip-user）")
            return
        if _user_exists(self.run_user):
            self.report.skip("用户 %s 已存在" % self.run_user)
            return
        if self.report.dry_run:
            self.report.plan("创建系统用户 %s（无登录 shell）" % self.run_user)
            return
        run(["useradd", "--system", "--shell", "/usr/sbin/nologin",
             "--home-dir", str(self.state_dir), "--no-create-home", self.run_user])
        self.report.action("已创建用户 %s" % self.run_user)

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

        # 状态目录只给 Agent 用户读写 —— 里面有凭据
        if self.state_dir.is_dir() and not self.options.skip_user and not self.report.dry_run:
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
            from syncoj_agent import discovery

            key = discovery.load_public_key(self.options.public_key)
            if key is None:
                raise InstallError("--public-key 读不出来: %s" % self.options.public_key)
            outcome = discovery.discover(
                key,
                timeout=self.options.discover_timeout,
                machine_id=discovery.machine_id_hint(),
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
        from syncoj_agent import discovery
        from syncoj_agent.rsa import verify_pkcs1v15_sha256

        key = discovery.load_public_key(key_path)
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


    def install_release(self, source: Path) -> str:
        """把 Agent 代码放进 ``releases/<版本>/``，返回版本号。

        已存在同版本时**直接复用**，不重装 —— 幂等性的核心：重复执行不该产生
        任何实际变化。
        """
        self.report.section("安装 Agent")

        if self.options.from_dir:
            stage = Path(self.options.from_dir)
            version = read_bundle_version(stage)
            release_dir = self.prefix / RELEASES_DIR / version
            if release_dir.is_dir():
                self.report.skip("版本 %s 已安装，跳过" % version)
                return version

            if self.report.dry_run:
                self.report.plan("把 %s 复制到 %s" % (stage, release_dir))
                return version

            staging = self.prefix / RELEASES_DIR / (".staging-" + version)
            if staging.exists():
                shutil.rmtree(str(staging), ignore_errors=True)
            shutil.copytree(str(stage), str(staging))
            os.replace(str(staging), str(release_dir))
            self.report.action("已安装版本 %s" % version)
            return version

        bundle = source
        if self.report.dry_run:
            self.report.plan("校验并解压 %s" % bundle)
            self.report.plan("安装到 %s/<版本>/" % (self.prefix / RELEASES_DIR))
            return self.options.version or "0.0.0"

        self._verify_checksum(bundle)

        staging = self.prefix / RELEASES_DIR / (".staging-" + str(os.getpid()))
        if staging.exists():
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
            release_dir = self.prefix / RELEASES_DIR / version

            if release_dir.is_dir():
                self.report.skip("版本 %s 已安装，跳过解压" % version)
                return version

            os.replace(str(staging), str(release_dir))
        except BaseException:
            shutil.rmtree(str(staging), ignore_errors=True)
            raise

        self.report.action("已安装版本 %s" % version)
        return version

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
            # deploy_root / scan_roots 是**字符串模板**，可能含 {desktop}/{player_no}，
            # 不能当 Path 处理（Path 会保留大括号，但改配置时容易误伤）
            deploy_root=self.options.deploy_root,
            scan_roots=self.options.scan_root,
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

        from syncoj_agent import discovery  # 延迟 import：只有这条路才用得上

        key = discovery.load_public_key(public_key_path)
        if key is None:
            return None, "发布公钥读不出来"

        if self.report.dry_run:
            self.report.plan("在局域网里寻找服务端（UDP 广播）")
            return None, "预览模式不真的发探测"

        targets = None
        if self.options.discover_address:
            targets = [self.options.discover_address]
        outcome = discovery.discover(
            key,
            timeout=self.options.discover_timeout,
            machine_id=discovery.machine_id_hint(),
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

    def install_bootstrap_key(self) -> bool:
        """把统一密钥文件放到配置目录，**0600 且属主 root**。

        密钥由教师在服务端签发（``syncoj-server bootstrap-key issue``），
        装机时用 ``--bootstrap-key`` 传进来。它放在这里而不是 agent.ini 里，
        原因是那个文件 chown 给了选手账号 —— 学生读得到里面的每一个字节。

        返回值是"这台机器现在有没有可用的密钥"，**不是**"这次有没有写"：
        重复执行安装器时通常不会再传一遍密钥（那是个一眼都不该多看的秘密），
        如果这里返回 False，``install_enroll_unit`` 就会把上一次装好的
        开机注册单元删掉 —— 表现是"什么都没改，但下次开机不再注册了"。
        """
        target = self.config_dir / BOOTSTRAP_KEY_FILENAME
        raw = (self.options.bootstrap_key or "").strip()

        if not raw:
            # 没给密钥 ≠ 没有密钥：上一次装机可能已经放好了一份
            return target.is_file()

        self.report.section("统一注册密钥")

        if self.report.dry_run:
            self.report.plan("写入 %s（0600，属主 root）" % target)
            return True

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
        else:
            self.report.warn(
                "systemctl %s 失败（退出码 %s）。用 'journalctl -u %s -n 50' 查看原因"
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
        # 重复执行安装器时通常不会再传一遍密钥，见 install_bootstrap_key
        has_bootstrap_key = self.install_bootstrap_key()

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
            self.report.note("查看日志: journalctl -u %s -n 50" % self.service_name)
        return EXIT_OK


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
ReadWritePaths=%(state)s

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

    **权限模型变了**：Agent 现在以**选手登录用户的身份**运行（而不是专用的
    syncoj 账号），因为代码和下发文件都在选手自己的桌面下，跨用户授权在现场
    很容易装成"服务起来了但什么都不传"。

    这带来一个直接后果：``ProtectHome=read-only`` 与 ``ProtectSystem=strict``
    会把家目录整个变成只读，Agent 就没法往桌面写东西了。所以：

    - **不设 ProtectHome**（Agent 本来就要读写自己的家目录）
    - ``ReadWritePaths=%h`` —— systemd 会把 ``%h`` 展开成 ``User=`` 的家目录，
      正好覆盖 ``{desktop}`` 及其下的一切
    - 显式写死的绝对路径（不含占位符的）单独加进来；模板路径都在 ``%h`` 之下，
      再加一遍是多余的

    ``ProtectSystem=strict`` 仍然保留：它把 ``/usr``、``/etc``、``/boot`` 等
    系统目录全部只读，这才是这条指令的价值所在。
    """
    writable = ["%h", _posix(state_dir)]

    # 只把**不含占位符的绝对路径**加进来；含 {desktop}/{player_no} 的都在 %h 下
    #
    # 用 PurePosixPath 而不是 Path 判绝对性：安装器可能在 Windows 上跑
    # （构建镜像的开发机），而 Path("/srv/code").is_absolute() 在 Windows 上是
    # False —— 那条路径就会被静默漏掉白名单，表现为"服务起来了但读不到目录"。
    # 目标机是 Linux，判定必须用 POSIX 语义。
    for candidate in [deploy_root] + scan_roots.replace(",", "\n").splitlines():
        text = candidate.strip()
        if not text or "{" in text:
            continue
        if PurePosixPath(text).is_absolute():
            writable.append(text)

    # 去重但保持顺序
    seen = set()
    writable = [p for p in writable if not (p in seen or seen.add(p))]

    lines = [
        "[Unit]",
        "Description=SyncOJ Agent（选手端代码回收，无界面静默运行）",
        "After=network-online.target",
        "Wants=network-online.target",
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
        "Restart=always",
        "RestartSec=5",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
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
        # %h 由 systemd 展开成 User= 的家目录，覆盖 {desktop} 及其下的一切
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
    layout.add_argument("--user", default=DEFAULT_RUN_USER)
    layout.add_argument("--python", default="", help="Agent 使用的 python3 路径")

    behaviour = parser.add_argument_group("行为")
    behaviour.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何改动")
    behaviour.add_argument("--quiet", action="store_true")
    behaviour.add_argument("--skip-user", action="store_true", help="不创建系统用户")
    behaviour.add_argument("--skip-service", action="store_true", help="不碰 systemd")
    behaviour.add_argument("--skip-python-check", action="store_true")
    return parser


def resolve_bootstrap_key(options: argparse.Namespace, report: Reporter) -> Optional[str]:
    """定出这次该写进机器的统一注册密钥；没有就返回 ``None``。

    优先级：``--bootstrap-key``（明文，最高）> ``--bootstrap-key-file`` >
    ``<仓库>/.key/bootstrap.key``（存在才用）> 没有。

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
