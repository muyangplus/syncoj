"""从仓库源码打出一个 Agent 离线包（「发布当前版本」的后端）。

## 为什么不 import，而是起子进程

``agent/packaging/build_bundle.py`` 只用标准库，看上去直接 import 更省事。但那个
脚本失败时抛的是 ``SystemExit``（``collect_files``、``launcher_path``、
``read_version`` 三处都是），而 ``SystemExit`` **不是** ``Exception`` 的子类 ——
``except Exception`` 接不住它，FastAPI 的错误处理器也接不住，它会直接穿过请求
处理一路冒上去。起子进程之后，它只是一个非零退出码，还能把 stderr 一起带回来。

顺带赚到两条：构建崩了不会把服务端进程带走；以及"在界面上点一下"和"手工跑那条
命令"走的是同一个入口 —— 后者已经被可复现打包保证过（mtime=0、gzip 头不带文件名），
所以两者产物逐字节相同。

## 产物怎么落地

打到一个临时文件，再交给 ``BlobStore.put_stream`` —— 和"上传一个包"完全同一条
存路径。于是 sha256、内容去重、``max_release_size`` 上限的行为全都不用重写，
也就不可能和上传那条路悄悄长出两套规矩。

## 为什么这里绝不 import ``syncoj_agent``

服务端**不能**依赖 Agent 的代码：Agent 是 Python 3.8 + 零第三方依赖、并且在目标机
上被 systemd 用 ``-E -s`` 拉起（忽略 PYTHONPATH）。服务端 import 它，就会让
"服务端能起来"多依赖一堆它不该关心的东西。所以版本号是**按行读**出来的，不是
import 出来的 —— 难看一点，但这条边界值得。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import keys
from .install_policy import POLICY_JSON_NAME

__all__ = [
    "BUILD_TIMEOUT_SECONDS",
    "BOOTSTRAP_KEY_NAME",
    "BuildError",
    "SourceProbe",
    "agent_root",
    "build_agent_bundle",
    "probe_source",
    "source_version",
]

#: Agent 源码在仓库里的位置。**只在这里写一次** —— build_bundle.py 里的
#: ``AGENT_ROOT`` 是同一个位置，两边都靠仓库布局，不靠猜。
AGENT_DIR_NAME = "agent"
AGENT_PACKAGE_NAME = "syncoj_agent"
BUILDER_RELPATH = ("packaging", "build_bundle.py")
INSTALLER_RELPATH = ("packaging", "install.py")
BOOTSTRAP_RELPATH = ("packaging", "bootstrap.sh")
LAUNCHER_NAME = "run_agent.py"
INIT_FILE_NAME = "__init__.py"
VERSION_ATTR = "__version__"

#: 随包附带的统一注册密钥在包里的名字。
#:
#: 放在 tar.gz **根目录**下（与 ``syncoj_agent/``、``run_agent.py`` 同级），
#: 和 ``release-key.pub.json`` / ``server.json`` 同一个位置 —— 它们都是给
#: **安装器**看的，不是包的一部分，混进 ``syncoj_agent/`` 里只会被当成模块或数据文件。
#: 权限 0600：这把钥匙能注册整间机房，不该躺在别人读得到的地方。
BOOTSTRAP_KEY_NAME = "bootstrap.key"

#: 附带的密钥在包内的权限位。**不能**跟着其它成员走 0644。
#: 策略文件（``install_policy.json``）不是秘密，仍然走 0644。
BOOTSTRAP_KEY_MODE = 0o600

#: 构建超时。这个包只有几百 KB，正常一两秒；给到 2 分钟是因为目标机可能同时在
#: 跑评测或收代码，而"误杀一次构建"比"多等 100 秒"的代价大得多。
BUILD_TIMEOUT_SECONDS = 120


class BuildError(RuntimeError):
    """构建失败。``detail`` 是一句能直接显示给教师的中文。

    ``log`` 是给服务端日志/审计用的原始输出，可能很长，**不要**回给客户端。
    """

    def __init__(self, detail: str, log: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.log = log or detail


@dataclass
class SourceProbe:
    """"本机能不能构建、会构建出什么"这件事的完整回答。

    做成一个"永远不抛异常"的形状，是因为它要被**对话框打开时**调用：这时候
    "没有源码"是完全正常的一种状态（生产上服务端可能根本没 checkout），
    该显示成一句解释，而不是一个 500。

    ``public_key`` 是会被内嵌进包里的那把公钥的路径（没有就是 ``None``）。
    把它一起报出来是为了让界面能**在点之前**说清"这样发出去机器验不了"，
    而不是等构建成功、铺开、然后所有机器静默不升级。

    ``public_url`` 是会被内嵌进包里的服务端地址（没有就是 ``None``）。它同样是
    "点之前就该看见"的信息：装 50 台时机器就靠它找服务端，而它取决于有没有人从
    局域网打开过界面 —— 不说出来的话，教师不会知道这个包"能不能自己找到服务器"。
    """

    available: bool
    version: Optional[str] = None
    agent_root: Optional[str] = None
    reason: Optional[str] = None
    public_key: Optional[str] = None
    public_url: Optional[str] = None


def agent_root() -> Path:
    """仓库里的 ``agent/`` 目录；找不到就抛 :class:`BuildError`。"""
    root = keys.repo_root()
    if root is None:
        raise BuildError(
            "服务端不是从源码仓库运行的，找不到 agent/ 源码。"
            "可以改用「上传升级包」，或者在仓库里手工跑 "
            "python agent/packaging/build_bundle.py 之后再上传。"
        )
    candidate = root / AGENT_DIR_NAME
    if not (candidate / AGENT_PACKAGE_NAME).is_dir():
        raise BuildError(
            "仓库里没有 %s/ 源码目录（期望在 %s）" % (AGENT_DIR_NAME, candidate)
        )
    if not (candidate / LAUNCHER_NAME).is_file():
        # 提前说清楚，而不是等 build_bundle 抛 SystemExit 再翻译
        raise BuildError(
            "agent/ 里缺少启动器 %s —— 没有它打出的包在目标机上起不来"
            % LAUNCHER_NAME
        )
    return candidate


def source_version(root: Optional[Path] = None) -> str:
    """按行读 ``agent/syncoj_agent/__init__.py`` 里的 ``__version__``。

    刻意不 import：理由见模块开头的说明。刻意也不做正则/exec —— 这一行是
    人写的，按 ``=`` 切一刀足够，切不出来就报错让人去改，而不是猜。
    """
    where = Path(root) if root is not None else agent_root()
    init_file = where / AGENT_PACKAGE_NAME / INIT_FILE_NAME
    try:
        text = init_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError("读不到 %s：%s" % (init_file, exc))
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(VERSION_ATTR):
            value = stripped.partition("=")[2].strip().strip("'\"")
            if value:
                return value
            break
    raise BuildError("%s 里没有可用的 %s" % (init_file, VERSION_ATTR))


def probe_source(public_url: Optional[str] = None) -> SourceProbe:
    """能不能构建、会打出哪个版本、包里会带哪把公钥和哪个地址。**不抛异常。**

    ``public_url`` 由调用方算好传进来（它需要请求上下文才能确定，见
    :func:`syncoj_server.services.discovery.describe_advertised_url`）——
    这里只负责如实报出去，不自己猜。
    """
    public_key = keys.release_public_key_path()
    try:
        root = agent_root()
        version = source_version(root)
    except BuildError as exc:
        return SourceProbe(
            available=False,
            reason=exc.detail,
            public_key=_as_str(public_key),
            public_url=public_url,
        )
    return SourceProbe(
        available=True,
        version=version,
        agent_root=str(root),
        public_key=_as_str(public_key),
        public_url=public_url,
    )


def _as_str(path: Optional[Path]) -> Optional[str]:
    return str(path) if path is not None else None


def installer_path() -> Path:
    """``install.py`` —— 装机入口要发给空机器的那一个文件。

    它**不在**离线包里：``build_bundle.py`` 的 ``EXCLUDE_DIRS`` 明确排除了
    ``packaging/``，因为包是给机器运行用的，安装器是给**装机那一次**用的。
    所以空机器上只有一个 curl 的时候，第一件要拿到的东西由服务端直接发。

    与构建同一条定位方式（都是"服务端跑在仓库里"这个前提）；不在仓库布局里就
    抛 :class:`BuildError`，调用方翻译成一句人话。
    """
    return _packaging_file(INSTALLER_RELPATH, "安装器")


def bootstrap_path() -> Path:
    """``bootstrap.sh`` —— 那条"一条 curl 起头"的自举脚本。

    它只做三件事：拿到 ``install.py``、把它跑起来、把参数原样传下去。
    真正的逻辑全在 Python 里（shell 写错难查，而且没法被 CI 覆盖）。
    """
    return _packaging_file(BOOTSTRAP_RELPATH, "自举脚本")


def _packaging_file(relpath, label: str) -> Path:
    path = agent_root().joinpath(*relpath)
    if not path.is_file():
        raise BuildError("找不到%s %s" % (label, path))
    return path


def build_agent_bundle(
    destination: Path,
    *,
    server_url: Optional[str] = None,
    bootstrap_key: Optional[str] = None,
    install_policy: Optional[Dict[str, Any]] = None,
) -> Tuple[str, int, str]:
    """把仓库里的 Agent 源码打成 ``destination``，返回 ``(版本, 字节数, sha256)``。

    ``server_url`` 会被写进包内 ``server.json`` —— 装 50 台机器时，地址是唯一
    还要人手填的一项，而它是打包这台服务端**自己就知道**的。

    ``bootstrap_key`` 给定时，它的明文会被写进包内 ``bootstrap.key``（根目录、
    0600）。**服务端只存密钥的哈希、拿不到明文**，所以这里接的是"构建那一刻
    现场签发的那把新密钥"，不是"从库里挑一把" —— 那条路根本走不通。

    ``install_policy`` 会被写成包内 ``install_policy.json``（根目录）：离线装机
    时机器就读它。传进来的必须是 ``services/install_policy.build_install_policy``
    的产物 —— 台账与升级清单用的是同一个 helper，三处因此不会各自漂。

    明文只经过这个函数的局部变量和那个临时 tar 成员：不落库、不进日志、
    不进任何响应，见 ``api/admin.py`` 的 ``build_release``。

    sha256 是**我们自己**对产物算的，不用构建脚本打印的那个 —— 它打印的值要经过
    一次 stdout 往返（还有编码），而签名正是签在这个值上：宁可信文件本身。
    """
    root = agent_root()
    version = source_version(root)
    builder = root.joinpath(*BUILDER_RELPATH)
    if not builder.is_file():
        raise BuildError("找不到打包脚本 %s" % builder)

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        sys.executable,
        str(builder),
        "--agent-root",
        str(root),
        "--out",
        str(destination),
    ]
    # 公钥显式给：不给的话构建脚本会自己回退到 <仓库>/.key/release-key.pub.json，
    # 那条回退与本文件里的 keys.release_public_key_path() 必须指向同一个文件。
    # 显式传参让"两者指向不同文件"这种漂移不可能发生 —— 而它一旦发生，表现是
    # 每台机器都拒绝升级，且没有任何报错。
    public_key = keys.release_public_key_path()
    if public_key is not None:
        argv += ["--public-key", str(public_key)]
    if server_url:
        argv += ["--server-url", server_url]

    env = dict(os.environ)
    # 子进程按 UTF-8 写 stdout/stderr，父进程也按 UTF-8 读。不钉这一下的话，
    # 简中 Windows 上子进程按 cp936 写、父进程按 utf-8 解，报出来的是一个
    # 跟构建毫无关系的 UnicodeDecodeError。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        completed = subprocess.run(
            argv,
            cwd=str(root.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BUILD_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise BuildError("打包超时（超过 %d 秒）" % BUILD_TIMEOUT_SECONDS)
    except OSError as exc:
        raise BuildError("无法启动打包进程：%s" % exc)

    if completed.returncode != 0 or not destination.is_file():
        log = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        # 构建脚本用 SystemExit("一句中文") 报错，那句话就是最好的说明
        tail = log.splitlines()[-1].strip() if log else ""
        raise BuildError(
            "打包失败（退出码 %d）%s" % (completed.returncode, ("：" + tail) if tail else ""),
            log=log,
        )

    if bootstrap_key or install_policy is not None:
        # 公钥/地址由构建脚本按同一套规矩打进去（顺序、mtime=0、uid/gid=0）；
        # 密钥与策略是**服务端**才知道的东西，只能在包打好之后补进去。
        # 两者一起补：分两次"解开重打"没有意义，还多一次丢掉可复现性的机会。
        _inject_bundle_members(
            destination,
            bootstrap_key=bootstrap_key,
            install_policy=install_policy,
        )

    digest = hashlib.sha256()
    size = 0
    with destination.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return version, size, digest.hexdigest()


def _inject_bundle_members(
    bundle_path: Path,
    *,
    bootstrap_key: Optional[str] = None,
    install_policy: Optional[Dict[str, Any]] = None,
) -> None:
    """把服务端才知道的成员补进已经打好的包（根目录下的密钥与安装策略）。

    为什么要补而不是让 ``build_bundle.py`` 打进去：那个脚本属 ``agent/``，它得同时
    服务"人手跑一条命令打包"这条路径，而密钥是服务端构建时才签出来的、策略是发布
    记录上那一份。把它做成命令行参数，密钥明文就会出现在进程参数表里（任何同机
    进程都能看到 ``ps``），比写在临时文件里更糟。

    为什么是"解开重打"而不是"往 tar.gz 后面追加"：gzip 是流式压缩，
    追加的字节根本不会被解压出来。所以走"先解开 → 补成员 → 按同一套
    可复现规矩重打一份"。

    重打时**顺序固定成路径排序**、成员一律 ``mtime=0``/``uid=gid=0``，
    并用 ``GzipFile(filename="", mtime=0)`` —— 可复现性不能因为多带了一两个成员
    就丢掉，否则同一次发版打两遍会得到两个 sha256，而签名签的正是那个值。
    """
    payloads: Dict[str, bytes] = {}
    if bootstrap_key:
        payloads[BOOTSTRAP_KEY_NAME] = (bootstrap_key.strip() + "\n").encode("utf-8")
    if install_policy is not None:
        payloads[POLICY_JSON_NAME] = (
            json.dumps(install_policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    with tempfile.TemporaryDirectory(prefix="bundle-") as tmp:
        staging = Path(tmp)
        extracted = staging / "tree"
        with tarfile.open(str(bundle_path), "r:gz") as archive:
            _extract_safely(archive, extracted)

        names = _relative_files(extracted)
        for name, payload in payloads.items():
            (extracted / name).write_bytes(payload)

        _write_reproducible_tar(
            bundle_path, extracted, sorted(names + list(payloads))
        )


def _extract_safely(archive: tarfile.TarFile, destination: Path) -> None:
    """把包解开到 ``destination``，**拒绝任何逃出这个目录的成员**。

    这个包是**我们刚打出来的**，理论上不会有害成员；但"输入恰好总是正常的"
    不是一个可以依赖的性质，而这里一个 ``../`` 就能写到临时目录之外。
    """
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for member in archive.getmembers():
        if member.isdir():
            continue
        if not member.isfile():
            # 符号链接/设备节点在包里没有用武之地，直接不搬
            continue
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents:
            raise BuildError("包内成员的路径不合法：%s" % member.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:  # pragma: no cover - 上面已经过滤过非普通文件
            continue
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
        finally:
            source.close()


def _relative_files(root: Path) -> List[str]:
    """树里所有普通文件的相对路径（``/`` 分隔、排序）。"""
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )


def _write_reproducible_tar(target: Path, root: Path, names: List[str]) -> None:
    """按 ``names`` 的顺序把 ``root`` 下的文件重打成 ``target``（可复现）。

    内层 tar 不压缩，外层 gzip 只压一次（``compresslevel=9``，与
    ``build_bundle.py`` 一致，免得"带密钥的包"和"不带密钥的包"两套规矩）。
    """
    with target.open("wb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
        ) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for name in names:
                    path = root / name
                    info = tarfile.TarInfo(name)
                    info.size = path.stat().st_size
                    info.mode = (
                        BOOTSTRAP_KEY_MODE if name == BOOTSTRAP_KEY_NAME else 0o644
                    )
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = 0
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
