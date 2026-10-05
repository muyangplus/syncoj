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

    # 镜像预装（构建镜像时跑）
    sudo python3 install.py --server https://10.0.0.1:8443 --enroll-code XXXX-...

    # 离线包安装（考场无网）
    sudo python3 install.py --bundle ./syncoj-agent-0.1.0.tar.gz --server ... --enroll-code ...

    # 在线自举（网络可达）
    sudo python3 install.py --download-url https://10.0.0.1:8443/dist/syncoj-agent-0.1.0.tar.gz ...

**幂等**是硬要求：重复执行结果一致。尤其是 —— **绝不覆盖已存在的 agent.ini**。
教师可能已经在里面改了扫描目录或注册码，安装器把它们冲掉是灾难性的。
"""

from __future__ import annotations

import argparse
import hashlib
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
                   bool(self.options.from_dir)]
        if sum(sources) != 1:
            raise InstallError("必须且只能指定一个来源：--bundle / --download-url / --from-dir")

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

        if self.options.download_url:
            url = self.options.download_url
            target = Path(tempfile.mkdtemp(prefix="syncoj-install-")) / "bundle.tar.gz"
            if self.report.dry_run:
                self.report.plan("从 %s 下载安装包" % url)
                return target
            self.report.plan("从 %s 下载安装包" % url)
            try:
                with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                    with open(str(target), "wb") as handle:
                        shutil.copyfileobj(response, handle, 1024 * 1024)
            except (urllib.error.URLError, OSError) as exc:
                raise InstallError("下载失败: %s" % exc)
            self.report.action("已下载 %s" % url)
            return target

        # --from-dir：直接用一个已经解开的目录（镜像预装时最方便）
        return Path(self.options.from_dir)

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
            # 教师很可能已经改过扫描目录或注册码。覆盖它是灾难性的。
            self.report.skip("配置已存在，保持不变: %s" % self.config_path)
            self.report.note("（如需重建请加 --force-config，会覆盖现有配置）")
            return

        content = render_config(
            server_url=self.options.server or "https://127.0.0.1:8000",
            verify_tls=not self.options.insecure,
            ca_file=self.options.ca_file or "",
            enroll_code=self.options.enroll_code or "",
            state_dir=self.state_dir,
            # deploy_root / scan_roots 是**字符串模板**，可能含 {desktop}/{player_no}，
            # 不能当 Path 处理（Path 会保留大括号，但改配置时容易误伤）
            deploy_root=self.options.deploy_root,
            scan_roots=self.options.scan_root,
            scan_prefix=self.options.scan_prefix,
            upgrade_mode=self.options.upgrade_mode,
            install_root=self.prefix,
            public_key=self.options.public_key or "",
        )

        if self.report.dry_run:
            self.report.plan("写入 %s" % self.config_path)
            return

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".ini.tmp")
        tmp.write_text(content, encoding="utf-8")
        # 临时文件先给 0600，避免在 replace 之前有一瞬间是宽权限
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(self.config_path))
        self._restrict_config_to_run_user()
        self.report.action("已写入 %s" % self.config_path)

        if self.options.enroll_code:
            self.report.note("注册码已写入配置；机器快照还原后 Agent 会用它自动重新注册")

    def _restrict_config_to_run_user(self) -> None:
        """把配置文件交给**运行 Agent 的那个用户**，别人（包括 root 之外的所有人）读不到。

        这里必须 chown，不能只 chmod：安装器是 root 跑的，文件默认属主是 root，
        `0640` 给的是 **root 组**的读权限。而 systemd 单元里写的是
        `User=<选手登录用户>` —— 那个用户既不是 root 也不在 root 组，
        结果是 **Agent 读不到自己的配置，装完起不来**。

        `0600` 而不是 `0640`：这份文件里可能有注册码，没有任何其他账号需要读它。

        由此还推出一条必须记住的性质：**`agent.ini` 对选手登录账号是可读的**。
        所以共享密钥、长期有效的凭据这类东西，不该放在这个文件里 ——
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
            self.unit_path.write_text(content, encoding="utf-8")
            self.report.action("已写入 %s" % self.unit_path)

            if _which("systemctl"):
                run(["systemctl", "daemon-reload"], check=False)
                run(["systemctl", "enable", self.service_name], check=False)
                self.report.action("已启用开机自启")
            else:
                self.report.warn("找不到 systemctl，请手工启用服务")

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

        self.write_config(version)
        self.install_unit(version)
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
    enroll_code: str,
    state_dir: Path,
    deploy_root: str,
    scan_roots: str,
    scan_prefix: str,
    upgrade_mode: str,
    install_root: Path,
    public_key: str,
) -> str:
    """生成 agent.ini。

    由代码生成而不是 `sed` 替换模板：注册码、路径里可能含特殊字符，
    `sed` 会因为分隔符或转义把它们改坏，而且失败得悄无声息。

    ``deploy_root`` 与 ``scan_roots`` 是**字符串模板**（可能含
    ``{desktop}`` / ``{player_no}``），不能当成 Path 处理。
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
; 注册码：教师端签发，首次注册后与本机 machine_id 绑定。
; 机器被快照还原、凭据文件丢失后，靠它自动重新注册，无需人工干预。
enroll_code = {enroll_code}
state_dir = {state_dir}
machine_id =
; 下发文件的落地根目录。{{desktop}} 自动探测当前用户的桌面
; （兼容「桌面」与 Desktop 两种命名）。
; 最终落点：桌面/<准考证号>/<题目名>/<文件名>
deploy_root = {deploy_root}

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
        enroll_code=enroll_code,
        state_dir=_posix(state_dir),
        deploy_root=deploy_root,
        scan_roots=scan_roots,
        scan_prefix=scan_prefix,
        upgrade_mode=upgrade_mode,
        install_root=_posix(install_root),
        public_key=public_key,
    )


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
    source.add_argument("--from-dir", help="已解开的 Agent 目录（镜像预装）")
    source.add_argument("--sha256", default="", help="安装包 sha256，用于完整性校验")

    config = parser.add_argument_group("配置")
    config.add_argument("--server", help="服务端地址，如 https://10.0.0.1:8443")
    config.add_argument("--enroll-code", help="教师端签发的注册码")
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


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print("SyncOJ installer 1.0")
        return EXIT_OK

    report = Reporter(dry_run=args.dry_run, quiet=args.quiet)
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
