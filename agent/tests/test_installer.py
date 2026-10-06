"""安装器测试。

把安装器写成 Python（而不是 install.sh）的最大理由就在这个文件：shell 脚本在
这个环境里根本跑不起来（没有可用 bash），我只能交付一个"看起来对"的东西。
Python 版本则每条分支都能被真实执行。
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
PACKAGING = AGENT_ROOT / "packaging"

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from syncoj_agent.upgrade import parse_version, symlinks_supported  # noqa: E402

requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(), reason="当前环境不允许创建符号链接（Windows 需开发者模式）"
)


@pytest.fixture(scope="module")
def installer():
    """把 install.py 当模块加载。它不是包的一部分，只能走 importlib。"""
    spec = importlib.util.spec_from_file_location("syncoj_installer", PACKAGING / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def workdir() -> Iterator[Path]:
    root = REPO_ROOT / ".pytest-tmp" / ("installer-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 构造测试用的安装包
# --------------------------------------------------------------------------- #


def make_bundle(path: Path, version: str = "1.2.3", extra: List[Tuple[str, str, str]] = None) -> Path:
    entries = [
        ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version, "file"),
        ("syncoj_agent/main.py", "print('hi')\n", "file"),
    ]
    entries.extend(extra or [])
    with tarfile.open(str(path), "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                payload = content.encode("utf-8")
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = content
                archive.addfile(info)
            elif kind == "dev":
                info.type = tarfile.CHRTYPE
                archive.addfile(info)
    return path


# --------------------------------------------------------------------------- #
# 安全解包
# --------------------------------------------------------------------------- #


def test_safe_extract_normal(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "ok.tar.gz")
    dest = workdir / "out"
    tops = installer.safe_extract(bundle, dest)

    assert tops == ["syncoj_agent"]
    assert (dest / "syncoj_agent" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "a/../../escape.py", "/etc/cron.d/pwn", "//etc/passwd",
     "C:/Windows/pwn", "a\\..\\..\\pwn.py"],
)
def test_safe_extract_rejects_traversal(installer, workdir: Path, name: str) -> None:
    bundle = make_bundle(workdir / "evil.tar.gz", extra=[(name, "pwned", "file")])
    with pytest.raises(installer.InstallError):
        installer.safe_extract(bundle, workdir / "out")


def test_safe_extract_rejects_symlink(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "sym.tar.gz", extra=[("syncoj_agent/link", "/etc", "sym")])
    with pytest.raises(installer.InstallError, match="链接"):
        installer.safe_extract(bundle, workdir / "out")


def test_safe_extract_rejects_device(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "dev.tar.gz", extra=[("syncoj_agent/null", "", "dev")])
    with pytest.raises(installer.InstallError, match="设备|节点"):
        installer.safe_extract(bundle, workdir / "out")


def test_safe_extract_rejects_non_tar(installer, workdir: Path) -> None:
    bogus = workdir / "not.tar.gz"
    bogus.write_bytes(b"definitely not a tar file")
    with pytest.raises(installer.InstallError, match="tar"):
        installer.safe_extract(bogus, workdir / "out")


def test_safe_extract_rejects_oversized(installer, workdir: Path, monkeypatch) -> None:
    monkeypatch.setattr(installer, "MAX_EXTRACTED_BYTES", 10)
    bundle = make_bundle(workdir / "bomb.tar.gz", extra=[("syncoj_agent/big.bin", "x" * 5000, "file")])
    with pytest.raises(installer.InstallError, match="总大小"):
        installer.safe_extract(bundle, workdir / "out")


# --------------------------------------------------------------------------- #
# 版本识别
# --------------------------------------------------------------------------- #


def test_read_bundle_version(installer, workdir: Path) -> None:
    dest = workdir / "extracted"
    installer.safe_extract(make_bundle(workdir / "v.tar.gz", version="4.5.6"), dest)
    assert installer.read_bundle_version(dest) == "4.5.6"


def test_read_bundle_version_rejects_wrong_layout(installer, workdir: Path) -> None:
    with pytest.raises(installer.InstallError, match="不是 SyncOJ Agent 包"):
        installer.read_bundle_version(workdir)


def test_read_bundle_version_rejects_missing_version(installer, workdir: Path) -> None:
    pkg = workdir / "syncoj_agent"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# 没有 __version__\n", encoding="utf-8")
    with pytest.raises(installer.InstallError, match="__version__"):
        installer.read_bundle_version(workdir)


# --------------------------------------------------------------------------- #
# 配置与单元模板
# --------------------------------------------------------------------------- #


def config_args(**overrides):
    """render_config 的默认参数。

    ``deploy_root`` 与 ``scan_roots`` 是**字符串模板**（可能含 ``{desktop}`` /
    ``{player_no}``），不是 Path —— 它们要原样写进配置文件。
    """
    args = dict(
        server_url="https://10.0.0.1:8443",
        verify_tls=True,
        ca_file="/etc/syncoj/ca.pem",
        bootstrap_key_file="/etc/syncoj/bootstrap.key",
        state_dir=Path("/var/lib/syncoj"),
        deploy_root="{desktop}",
        scan_roots="{desktop}/{player_no}",
        scan_prefix="none",
        upgrade_mode="off",
        install_root=Path("/opt/syncoj"),
        public_key="",
        run_user="noi",
    )
    args.update(overrides)
    return args


def test_render_config_contains_all_settings(installer) -> None:
    text = installer.render_config(**config_args())
    assert "url = https://10.0.0.1:8443" in text
    assert "verify_tls = true" in text
    assert "ca_file = /etc/syncoj/ca.pem" in text
    assert "bootstrap_key_file = /etc/syncoj/bootstrap.key" in text
    assert "roots = {desktop}/{player_no}" in text
    assert "deploy_root = {desktop}" in text
    assert "prefix = none" in text
    assert "mode = off" in text
    # 心跳兜底默认值必须与 syncoj_agent/config.py 里的默认值一致
    # （服务端策略仍然优先，这里只是"还没下发策略"时的兜底）
    assert "interval = 30" in text
    # 运行账号必须进配置：{home}/{desktop} 的展开只认它，不认"谁在跑"
    assert "run_user = noi" in text


def test_render_config_has_no_place_to_put_a_secret(installer) -> None:
    """**密钥只能以路径形式进配置，绝不以内容形式。**

    ``agent.ini`` 会被 chown 给选手账号 —— 学生读得到里面的每一个字节，
    而那把钥匙能注册整间机房。最可靠的判据是"这个函数的参数里根本没有能塞进
    密钥的地方"：只要没有这个入口，将来谁也没法顺手把它写进去。
    """
    import inspect

    params = set(inspect.signature(installer.render_config).parameters)
    assert "bootstrap_key_file" in params, "密钥路径必须可配"
    assert "bootstrap_key" not in params, "render_config 不该拿到密钥内容"
    assert "enroll_code" not in params


def test_rendered_config_has_no_enroll_code(installer) -> None:
    text = installer.render_config(**config_args())
    active = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith((";", "#"))
    )
    assert "enroll_code" not in active
    assert "bootstrap_key_file = /etc/syncoj/bootstrap.key" in active


def test_rendered_config_documents_every_placeholder(installer) -> None:
    """生成出来的配置要**把可用的占位符都写清楚**。

    配置文件是运维唯一会看的文档。少写一个占位符，就少一个人知道能用它 ——
    然后大家只能用 sed 去改路径，改完还不敢确认对不对。

    同时防止 ``.format()`` 里的花括号转义写错：漏一层转义会让占位符变成
    空字符串或直接抛 KeyError。
    """
    text = installer.render_config(**config_args())

    for token in ("{desktop}", "{home}", "{player_no}", "{contest_slug}"):
        assert token in text, "生成的配置里没有说明 %s" % token

    # 检查过的那行注释本身不该冒出多余的花括号（说明转义层级写错了）
    for line in text.splitlines():
        if line.strip().startswith(";"):
            assert "{{" not in line and "}}" not in line, line


def test_rendered_config_is_parseable(installer, workdir: Path) -> None:
    """生成出来的配置必须能被 Agent 的解析器读进去 —— 否则安装成功却起不来。

    这里把 {desktop} 换成一个真实存在的目录，因为 Agent 载入配置时就会展开它。
    """
    from syncoj_agent.config import AgentConfig

    desktop = workdir / "桌面"
    desktop.mkdir()
    text = installer.render_config(
        **config_args(
            ca_file="",
            deploy_root=str(desktop),
            scan_roots="%s/{player_no}" % desktop,
        )
    )
    config_file = workdir / "agent.ini"
    config_file.write_text(text, encoding="utf-8")

    config = AgentConfig.load(config_file)
    assert config.server_url == "https://10.0.0.1:8443"
    assert config.bootstrap_key_file == Path("/etc/syncoj/bootstrap.key")
    assert config.upgrade_mode == "off"
    assert config.deploy_root == desktop
    assert config.scan_prefix == "none"
    # {player_no} 保留到注册之后才展开。
    # 用 Path 比较而不是字符串：Windows 上 Path 会把 "/" 归一化成 "\"
    assert Path(str(config.scan_roots[0]).replace("{player_no}", "S001")) == desktop / "S001"


@pytest.fixture()
def fake_accounts(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 ``pwd.getpwnam`` 换成一个临时家目录，返回那个"家目录"。

    凡是加载**安装器生成的** agent.ini 的测试都得带上它：配置里记着 ``run_user``
    （运行时账号），而 ``{home}`` / ``{desktop}`` 一律按那个账号解析。测试不该
    依赖真机器的 ``/etc/passwd``（Windows 开发机上连 ``pwd`` 模块都没有）。
    """
    from syncoj_agent import config as config_module

    home = workdir / "home" / "runuser"
    (home / "Desktop").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_module, "_pwd_home", lambda user: home)
    return home


def test_rendered_config_supports_home_and_contest_slug(
    installer, workdir: Path, fake_accounts: Path
) -> None:
    """用 ``{home}`` / ``{contest_slug}`` 写出来的配置也必须能读进来。

    而且 ``{home}`` / ``{desktop}`` 是按**运行账号**（安装器定的那个）展开的，
    **不是**按"当前是谁在跑"。用户口径：**桌面应该是安装用户（运行账号）的桌面**
    —— 注册单元以 root 跑，退回当前用户就等于把桌面算成 ``/root/桌面``，
    那台机器上不存在：注册退出 2、journal 里一个字都没有（真机事故）。
    """
    from syncoj_agent.config import AgentConfig

    text = installer.render_config(
        **config_args(
            ca_file="",
            deploy_root="{desktop}",
            scan_roots="{home}/{contest_slug}/{player_no}",
        )
    )
    config_file = workdir / "agent.ini"
    config_file.write_text(text, encoding="utf-8")

    config = AgentConfig.load(config_file)
    assert config.run_user == "noi"
    # Desktop 真的存在于那个家里，所以探测必须选中它（而不是写死「桌面」）
    assert config.deploy_root == fake_accounts / "Desktop"
    assert Path(str(config.deploy_root).replace("\\", "/")) == fake_accounts / "Desktop"
    # 独立的一条：结果里不许出现 /root
    assert "/root" not in str(config.deploy_root).replace("\\", "/")
    assert config.needs_credential is True
    # 两个占位符都要能展开，而不是只展开左边那个
    resolved = config.resolved_roots("S001", "mock-1")
    assert resolved == [fake_accounts / "mock-1" / "S001"]
    assert all("/root" not in str(p).replace("\\", "/") for p in resolved)


def make_installer(installer, workdir: Path, user: str, *, quiet: bool = True, argv=None):
    """造一个只为测试单个方法而存在的 Installer 实例。

    不走 ``preflight()``（那会检查 root 权限、安装包是否存在），
    只把 ``__init__`` 需要的属性凑齐。

    ``argv`` 用来补上被测方法会读到的命令行选项（比如 ``--bootstrap-key``）。
    """
    options = installer.build_parser().parse_args(["--user", user] + list(argv or []))
    options.config_dir = str(workdir / "etc")
    options.state_dir = str(workdir / "state")
    options.prefix = str(workdir / "opt")
    options.unit_dir = str(workdir / "units")
    instance = installer.Installer(options, installer.Reporter(quiet=quiet))
    instance.config_path.parent.mkdir(parents=True, exist_ok=True)
    return instance


def test_config_is_handed_to_the_user_the_agent_runs_as(
    installer, workdir: Path, monkeypatch
) -> None:
    """配置文件必须 chown 给 `--user` 指定的那个用户，权限收紧到 0600。

    这条测试是为一个真实故障写的：安装器是 root 跑的，文件默认属主是 root，
    `chmod 0640` 给的是 **root 组**的读权限。而 systemd 单元里写的是
    `User=<选手登录用户>` —— 那个用户既不是 root 也不在 root 组，
    结果是 **Agent 读不到自己的配置，装完起不来**。它报的是权限错误，
    看起来像是"安装没做对"，很难想到是 chown 漏了。

    沙箱里没法真的 chown 到别的用户，所以这里拦住的是**调用意图**。
    """
    calls = []
    monkeypatch.setattr(
        shutil, "chown",
        lambda path, user=None, group=None: calls.append(("chown", str(path), user)),
    )
    monkeypatch.setattr(
        os, "chmod", lambda path, mode: calls.append(("chmod", str(path), mode))
    )

    instance = make_installer(installer, workdir, "student")
    instance.config_path.write_text("; x\n", encoding="utf-8")
    instance._restrict_config_to_run_user()

    target = str(instance.config_path)
    assert ("chown", target, "student") in calls, (
        "没有把配置 chown 给运行用户 —— Agent 会读不到配置而启动失败"
    )
    assert ("chmod", target, 0o600) in calls, (
        "配置权限应当是 0600：它没有别的账号需要读，而收紧权限没有代价"
    )
    # 不能再出现 0640 —— 那正是这个 bug 的成因
    assert ("chmod", target, 0o640) not in calls


def test_config_chown_failure_is_reported_not_swallowed(
    installer, workdir: Path, monkeypatch, capsys
) -> None:
    """chown 失败（比如 ``--skip-user`` 时用户不存在）必须**说出来**。

    静默跳过会把"装完起不来"变成一道需要现场排查的谜题。
    """
    def boom(path, user=None, group=None):
        raise LookupError("no such user")

    monkeypatch.setattr(shutil, "chown", boom)

    instance = make_installer(installer, workdir, "no-such-user", quiet=False)
    instance.config_path.write_text("; x\n", encoding="utf-8")
    instance._restrict_config_to_run_user()  # 不能抛

    printed = capsys.readouterr().out
    assert "no-such-user" in printed, "chown 失败被吞掉了：屏幕上连提都没提"
    assert "chown" in printed, "应当直接给出可照抄的补救命令"


def test_generated_files_use_lf_even_when_built_on_windows(installer, workdir: Path) -> None:
    """生成的 agent.ini 与 systemd 单元必须是 **LF**。

    安装器常在 Windows 上被运行（开发机、镜像构建机），而 ``Path.write_text``
    默认会把 ``\\n`` 翻译成当前平台的换行 —— 于是产物是 CRLF。目标机是 Linux：

    * ``agent.ini`` 里每个值末尾多一个不可见的 ``\\r``（解析器宽容，问题隐蔽）
    * systemd 单元文件里多一个 ``\\r`` 会**直接解析失败**，报的还是"格式错误"，
      看不出是换行符

    这个陷阱不会在 Linux 上暴露，所以必须由一条跑在开发机上的测试守住。
    """
    instance = make_installer(installer, workdir, "student")

    # 注意：**不能**先把配置文件建出来 —— ``write_config`` 见到已存在的文件会
    # 直接 skip（那是"别覆盖教师改过的配置"的保护）。上一版就是这么写的，
    # 于是这条测试恒绿：它根本没走到写入路径。
    assert not instance.config_path.exists()
    instance.write_config("1.2.3")
    data = instance.config_path.read_bytes()
    assert data, "配置没被写出来（测试没走到写入路径）"
    assert b"\r" not in data, "生成的 agent.ini 里有 CR —— 目标机是 Linux"

    unit_file = instance.unit_path
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    assert not unit_file.exists()
    instance.install_unit("1.2.3")
    unit_data = unit_file.read_bytes()
    assert unit_data, "单元文件没被写出来（测试没走到写入路径）"
    assert b"\r" not in unit_data, "生成的 systemd 单元里有 CR —— systemd 会解析失败"


def test_rendered_paths_are_always_posix(installer) -> None:
    """生成的配置与单元只被 Linux 读取，里面的路径必须恒为正斜杠。

    安装器可能在 Windows 上被运行（开发机/镜像构建机），若直接 ``str(Path)``
    会写出 ``\\opt\\syncoj`` —— 拿到目标机上就是废的，而问题只在现场暴露。
    """
    config_text = installer.render_config(**config_args())
    unit_text = installer.render_unit(
        prefix=Path("/opt/syncoj"), config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"), deploy_root="{desktop}",
        scan_roots="{desktop}/{player_no}", run_user="student", python="/usr/bin/python3",
    )

    for text, label in ((config_text, "config"), (unit_text, "unit")):
        assert "\\" not in text, "%s 中出现了反斜杠路径" % label
        assert "/var/lib/syncoj" in text


def test_render_config_with_insecure_tls(installer) -> None:
    text = installer.render_config(**config_args(verify_tls=False))
    assert "verify_tls = false" in text


def test_render_unit_permissions_follow_contestant_user(installer) -> None:
    """systemd 的读写白名单必须跟着"Agent 以选手身份运行"这个前提走。

    原来的配置有 ``ProtectHome=read-only`` —— 那是在 Agent 以专用 syncoj 账号
    运行的假设下写的。现在 Agent 要往**自己的桌面**写文件，这条会把家目录整个
    变成只读，表现是"服务起来了但什么都不传"，现场极难排查。
    """
    text = installer.render_unit(
        prefix=Path("/opt/syncoj"),
        config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"),
        deploy_root="{desktop}",
        scan_roots="{desktop}/{player_no}, /srv/shared/code",
        run_user="student",
        python="/usr/bin/python3",
    )

    # %h = User= 的家目录（覆盖选手桌面）；安装器自建的目录也开写权限。
    # **每一条都带 `-`**：路径不存在时 systemd 跳过它，而不是 226 起不来。
    # 后四条是给"管理端授权的远程卸载"用的（沙箱里要能删单元文件与辅助脚本），
    # 它们本身是 root:0755，运行账号在 DAC 上写不进去 —— 详见 _writable_paths。
    assert (
        "ReadWritePaths=-%h -/opt/syncoj -/var/lib/syncoj "
        "-/etc/syncoj -/etc/systemd/system -/etc/sudoers.d -/usr/local/lib/syncoj"
    ) in text
    # 模板路径与可选配置不该被塞进来 —— 它们可能不存在，正是 226 的来源
    assert "{desktop}" not in text
    assert "{player_no}" not in text
    assert "/srv/shared/code" not in text
    # 这条会把家目录变只读，绝不能出现
    assert "ProtectHome" not in text
    # 系统目录仍然全部只读，这才是 ProtectSystem=strict 的价值
    assert "ProtectSystem=strict" in text


def test_render_unit_uses_current_symlink(installer) -> None:
    """ExecStart 必须走 current 软链与 run_agent.py。

    软链是自更新的原子切换点；run_agent.py 见下面的回归测试。
    """
    text = installer.render_unit(
        prefix=Path("/opt/syncoj"), config_path=Path("/etc/syncoj/agent.ini"),
        state_dir=Path("/var/lib/syncoj"), deploy_root="{desktop}",
        scan_roots="{desktop}/{player_no}", run_user="student", python="/usr/bin/python3",
    )
    assert (
        "ExecStart=/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py"
        in text
    ), text
    # 绝不能指向包内的 main.py —— 它使用包内相对导入，当脚本执行会 ImportError
    assert "syncoj_agent/main.py" not in text
    # -E 忽略 PYTHON* 环境变量，-s 忽略 user site-packages：
    # 选手怎么 pip install 都污染不到 Agent
    assert " -E -s " in text
    assert "StandardOutput=null" in text
    # 沙箱还在（换一条代表性加固断言）：NoNewPrivileges 是**刻意去掉**的 ——
    # 它会挡死 setuid 的 sudo，让管理端授权的远程卸载没法工作，见 render_unit 注释。
    assert "ProtectSystem=strict" in text
    assert "NoNewPrivileges" not in text


# --------------------------------------------------------------------------- #
# 启动路径回归测试
# --------------------------------------------------------------------------- #


def test_main_py_cannot_be_run_as_a_script(workdir: Path) -> None:
    """钉死这个事实：syncoj_agent/main.py **不能**当脚本直接执行。

    它用包内相对导入（``from . import __version__``），直接跑会
    ``ImportError: attempted relative import with no known parent package``。
    这条测试是下面那条存在的前提 —— 一旦有人把 main.py 改成绝对导入，
    这里会红，提醒他同步调整启动方式。
    """
    package_main = AGENT_ROOT / "syncoj_agent" / "main.py"
    result = subprocess.run(
        [sys.executable, "-E", "-s", str(package_main), "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode != 0
    assert "relative import" in (result.stderr or ""), result.stderr


def test_launcher_works_exactly_like_systemd(workdir: Path) -> None:
    """回归测试：systemd 用 ``-E -s /opt/syncoj/current/run_agent.py`` 启动。

    **为什么需要这条测试**：在它出现之前，全项目 366 个测试全绿，而 Agent 在
    真实 NOI Linux 上**一次都起不来** —— 因为所有测试都是"按包导入"来调用代码的，
    没有任何一条走过 systemd 的真实启动路径。

    单元测试覆盖得再全，也覆盖不到"入口点本身是坏的"这种情况。
    """
    launcher = AGENT_ROOT / "run_agent.py"
    assert launcher.is_file(), "缺少启动器 agent/run_agent.py"

    # --version 由 argparse 直接处理，不读配置、不联网，因此是最干净的入口验证
    result = subprocess.run(
        [sys.executable, "-E", "-s", str(launcher), "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, (
        "启动器执行失败（这等价于 systemd 里 Agent 起不来）:\n%s" % result.stderr
    )
    assert result.stdout.strip(), "启动器没有任何输出"


def test_launcher_runs_from_a_copied_directory(workdir: Path) -> None:
    """模拟真实发布布局：启动器与包在同一个目录里，且从别处调用。

    启动器靠 ``__file__`` 推导发布根，因此换个目录也要能工作 ——
    /opt/syncoj/current 是软链，解析出来是 releases/<版本>/，
    不能假设 cwd 或调用路径。
    """
    release = workdir / "releases" / "1.0.0"
    shutil.copytree(str(AGENT_ROOT / "syncoj_agent"), str(release / "syncoj_agent"))
    shutil.copy2(str(AGENT_ROOT / "run_agent.py"), str(release / "run_agent.py"))

    result = subprocess.run(
        # 刻意用一个完全无关的 cwd，并带上 systemd 用的 -E -s
        [sys.executable, "-E", "-s", str(release / "run_agent.py"), "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, cwd=str(workdir),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_bundle_contains_launcher(workdir: Path) -> None:
    """打包器必须把启动器放进发布根目录，否则安装出来是一堆起不来的代码。"""
    spec = importlib.util.spec_from_file_location(
        "syncoj_build_bundle3", PACKAGING / "build_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    out = workdir / "bundle.tar.gz"
    module.build(out, AGENT_ROOT)

    with tarfile.open(str(out)) as archive:
        names = [m.name for m in archive.getmembers()]

    assert "run_agent.py" in names, "发布包里没有启动器"
    assert "syncoj_agent/main.py" in names


def test_render_unit_without_extra_paths(installer) -> None:
    """没配额外路径时，白名单只剩 %h 与安装器自建的目录 —— 但 %h 必须在。

    少了它，ProtectSystem=strict 会把家目录也变成只读。
    """
    text = installer.render_unit(
        prefix=Path("/o"), config_path=Path("/c"), state_dir=Path("/s"),
        deploy_root="", scan_roots="", run_user="student", python="python3",
    )
    line = [x for x in text.splitlines() if x.startswith("ReadWritePaths=")][0]
    tokens = line.split("=", 1)[1].split()
    assert tokens[0] == "-%h", line
    for token in ("-/o", "-/s", "-/etc/systemd/system", "-/etc/sudoers.d",
                  "-/usr/local/lib/syncoj"):
        assert token in tokens, "%s 不在白名单里：%s" % (token, line)
    # `--config-dir /` 这类误配不许把整个文件系统放开
    assert "-/" not in tokens, line


# --------------------------------------------------------------------------- #
# 命令行与流程（dry-run 不需要 root，可在任何平台跑）
# --------------------------------------------------------------------------- #


def run_installer(installer, argv: List[str]) -> int:
    return installer.main(argv)


def test_dry_run_makes_no_changes(installer, workdir: Path) -> None:
    prefix = workdir / "opt" / "syncoj"
    code = workdir / "code"
    code.mkdir()

    rc = run_installer(installer, [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--server", "https://127.0.0.1:8443",
        "--scan-root", str(code),
        "--prefix", str(prefix),
        "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--dry-run", "--skip-user", "--skip-service", "--quiet",
    ])

    assert rc == 0
    assert not prefix.exists(), "dry-run 不得创建任何东西"
    assert not (workdir / "etc").exists()


def test_requires_exactly_one_source(installer, workdir: Path) -> None:
    rc = run_installer(installer, ["--dry-run", "--skip-user", "--quiet"])
    assert rc == installer.EXIT_ERROR

    rc = run_installer(installer, [
        "--dry-run", "--skip-user", "--quiet",
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--download-url", "https://example.invalid/x.tar.gz",
    ])
    assert rc == installer.EXIT_ERROR


def test_missing_bundle_is_reported(installer, workdir: Path) -> None:
    rc = run_installer(installer, [
        "--bundle", str(workdir / "nope.tar.gz"),
        "--dry-run", "--skip-user", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR


@requires_symlinks
def test_full_install_and_idempotency(installer, workdir: Path) -> None:
    """完整安装 + 重复执行 —— 幂等性的核心断言是"第二次不产生任何改动"。"""
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    state_dir = workdir / "state"
    code = workdir / "code"
    code.mkdir()
    bundle = make_bundle(workdir / "b.tar.gz", version="1.0.0")

    base = [
        "--bundle", str(bundle),
        "--server", "https://10.0.0.1:8443",
        "--bootstrap-key", "SECRET-BOOTSTRAP-KEY",
        "--scan-root", str(code),
        "--deploy-root", str(workdir / "exam"),
        "--prefix", str(prefix),
        "--config-dir", str(config_dir),
        "--state-dir", str(state_dir),
        "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, base) == 0

    release = prefix / "releases" / "1.0.0"
    assert (release / "syncoj_agent" / "main.py").is_file()
    assert installer.current_version(prefix) == "1.0.0"
    assert (config_dir / "agent.ini").is_file()
    assert (workdir / "units" / "syncoj-agent.service").is_file()
    assert (config_dir / "bootstrap.key").is_file()
    assert (workdir / "units" / "syncoj-agent-enroll.service").is_file()

    # 第二次执行：结果必须一致
    assert run_installer(installer, base) == 0
    assert installer.current_version(prefix) == "1.0.0"


@requires_symlinks
def test_second_install_without_the_key_keeps_the_enroll_unit(
    installer, workdir: Path
) -> None:
    """**幂等性的一个真实坑。**

    重复执行安装器时通常不会再传一遍密钥（那是个一眼都不该多看的秘密）。
    如果"没传密钥"被理解成"这台机器没有密钥"，``install_enroll_unit`` 就会把
    上一次装好的开机注册单元删掉 —— 表现是"什么都没改，但下次开机不再注册了"。
    """
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    units = workdir / "units"
    code = workdir / "code"
    code.mkdir()
    bundle = make_bundle(workdir / "b.tar.gz")

    common = [
        "--bundle", str(bundle),
        "--server", "https://10.0.0.1:8443",
        "--scan-root", str(code),
        "--prefix", str(prefix),
        "--config-dir", str(config_dir),
        "--state-dir", str(workdir / "state"),
        "--unit-dir", str(units),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, common + ["--bootstrap-key", "SECRET"]) == 0
    enroll_unit = units / "syncoj-agent-enroll.service"
    key_file = config_dir / "bootstrap.key"
    assert enroll_unit.is_file() and key_file.is_file()

    # 第二次不传密钥 —— 已有的密钥和注册单元都必须原样留着
    assert run_installer(installer, common) == 0
    assert enroll_unit.is_file(), "第二次没传密钥就把开机注册单元删掉了"
    assert key_file.read_text(encoding="utf-8").strip() == "SECRET"


# --------------------------------------------------------------------------- #
# 统一密钥的落盘规则（不走整装流程，免得被 Windows 的符号链接限制挡住）
# --------------------------------------------------------------------------- #


def test_bootstrap_key_lands_where_the_config_says_it_is(
    installer, workdir: Path, fake_accounts: Path
) -> None:
    """**密钥写在哪儿，配置里必须就指到哪儿。**

    自定义 ``--config-dir`` 时默认值 ``/etc/syncoj/bootstrap.key`` 会指到一个
    空路径，表现是"装完起不来、日志说找不到统一密钥"，而密钥明明就躺在旁边
    那个自定义目录里。所以配置里那一行必须是**这次实际写的位置**。
    """
    from syncoj_agent.config import AgentConfig

    instance = make_installer(
        installer,
        workdir,
        "syncoj",
        argv=["--server", "https://10.0.0.1:8443", "--bootstrap-key", "SECRET-KEY"],
    )
    assert instance.install_bootstrap_key() is True
    instance.write_config("1.0.0")

    key_file = instance.config_dir / "bootstrap.key"
    assert key_file.read_text(encoding="utf-8").strip() == "SECRET-KEY"

    config = AgentConfig.load(instance.config_path)
    assert config.bootstrap_key_file == key_file, (
        "配置指向的密钥路径与实际写入位置不一致"
    )


def test_bootstrap_key_file_is_private(installer, workdir: Path, monkeypatch) -> None:
    """密钥能注册整间机房，所以绝不能对选手账号可读。

    Windows 上 ``chmod`` 对组/其他位基本是空操作（这里会看到 666），所以这个
    测试**记录调用**而不是只看结果；在 Linux 上再加一条对真实权限位的断言 ——
    目标机就是 Linux，那一行 chmod 在那里是实打实生效的。
    """
    chmod_calls = []
    real_chmod = os.chmod

    def spy(path, mode, *args, **kwargs):
        chmod_calls.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(installer.os, "chmod", spy)

    instance = make_installer(
        installer, workdir, "syncoj", argv=["--bootstrap-key", "SECRET-KEY"]
    )
    assert instance.install_bootstrap_key() is True

    key_file = instance.config_dir / "bootstrap.key"
    assert (str(key_file), 0o600) in chmod_calls, (
        "没有把密钥文件的权限显式收紧到 0600：%r" % (chmod_calls,)
    )
    if os.name == "posix":
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600, "密钥文件权限是 %o，不是 0600" % mode


def test_repeated_install_without_the_key_keeps_the_enroll_unit(
    installer, workdir: Path
) -> None:
    """第二次执行安装器时通常不会再传密钥 —— 它不能被当成"这台机器没有密钥"。

    被误判的后果是上一次装好的开机注册单元被删掉：表现是"什么都没改，
    但下次开机不再注册了"，而那时现场已经没有人在看安装了。
    """
    first = make_installer(
        installer, workdir, "syncoj", argv=["--bootstrap-key", "SECRET-KEY"]
    )
    assert first.install_bootstrap_key() is True
    first.install_enroll_unit(first.install_bootstrap_key())
    enroll_unit = first.enroll_unit_path
    assert enroll_unit.is_file()

    # 第二次：同一台机器、同一份配置目录，但**没传** --bootstrap-key
    second = make_installer(installer, workdir, "syncoj", argv=["--server", "https://x"])
    assert second.install_bootstrap_key() is True, "已有的密钥没被认出来"
    second.install_enroll_unit(second.install_bootstrap_key())

    assert enroll_unit.is_file(), "第二次没传密钥就把开机注册单元删掉了"
    assert (instance_key(first)).read_text(encoding="utf-8").strip() == "SECRET-KEY"


def instance_key(instance) -> Path:
    return instance.config_dir / "bootstrap.key"


def test_no_enroll_unit_when_there_is_no_key(installer, workdir: Path) -> None:
    """没有密钥就没有注册可做。

    留一个永远失败的单元只会每次开机把 journal 刷脏，而真正的问题是
    "这台机器没有密钥" —— 那要靠装密钥解决，不是靠重启。
    """
    instance = make_installer(installer, workdir, "syncoj")
    assert instance.install_bootstrap_key() is False

    instance.install_enroll_unit(False)

    assert not instance.enroll_unit_path.exists()
    assert not (instance.config_dir / "bootstrap.key").exists()


def test_enroll_unit_is_removed_when_the_key_goes_away(installer, workdir: Path) -> None:
    """密钥被删掉之后重跑安装器，那个永远失败的单元也该跟着走。"""
    instance = make_installer(
        installer, workdir, "syncoj", argv=["--bootstrap-key", "SECRET-KEY"]
    )
    instance.install_bootstrap_key()
    instance.install_enroll_unit(True)
    assert instance.enroll_unit_path.is_file()

    # 有人把密钥拿走了（比如吊销之后手工删掉），再跑一次安装器
    (instance.config_dir / "bootstrap.key").unlink()
    later = make_installer(installer, workdir, "syncoj")
    assert later.install_bootstrap_key() is False
    later.install_enroll_unit(False)

    assert not later.enroll_unit_path.exists()


@requires_symlinks
def test_existing_config_is_preserved(installer, workdir: Path) -> None:
    """**教师的手工修改绝不能被冲掉。**

    现场很可能已经改过扫描目录（或者挪过密钥路径）。安装器把它们覆盖是灾难性的 ——
    而且现场往往是在"重装一下试试"的时候才踩到。
    """
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "agent.ini"
    config_file.write_text("[server]\nurl = https://edited-by-teacher\n", encoding="utf-8")
    code = workdir / "code"
    code.mkdir()

    base = [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--server", "https://new-server",
        "--scan-root", str(code),
        "--prefix", str(prefix),
        "--config-dir", str(config_dir),
        "--state-dir", str(workdir / "state"),
        "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, base) == 0
    assert "edited-by-teacher" in config_file.read_text(encoding="utf-8")


@requires_symlinks
def test_force_config_overwrites(installer, workdir: Path) -> None:
    prefix = workdir / "opt" / "syncoj"
    config_dir = workdir / "etc" / "syncoj"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "agent.ini"
    config_file.write_text("[server]\nurl = https://old\n", encoding="utf-8")
    code = workdir / "code"
    code.mkdir()

    rc = run_installer(installer, [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--server", "https://new", "--scan-root", str(code),
        "--prefix", str(prefix), "--config-dir", str(config_dir),
        "--state-dir", str(workdir / "state"), "--unit-dir", str(workdir / "units"),
        "--force-config", "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == 0
    assert "https://new" in config_file.read_text(encoding="utf-8")


@requires_symlinks
def test_upgrade_to_new_version_switches_symlink(installer, workdir: Path) -> None:
    prefix = workdir / "opt" / "syncoj"
    code = workdir / "code"
    code.mkdir()

    common = [
        "--server", "https://x", "--scan-root", str(code),
        "--prefix", str(prefix), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"), "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ]

    assert run_installer(installer, common + ["--bundle", str(make_bundle(workdir / "v1.tar.gz", "1.0.0"))]) == 0
    assert installer.current_version(prefix) == "1.0.0"

    assert run_installer(installer, common + ["--bundle", str(make_bundle(workdir / "v2.tar.gz", "2.0.0"))]) == 0
    assert installer.current_version(prefix) == "2.0.0"

    # 旧版本保留，供回滚
    assert (prefix / "releases" / "1.0.0").is_dir()
    assert (prefix / "releases" / "2.0.0").is_dir()


@requires_symlinks
def test_install_from_directory(installer, workdir: Path) -> None:
    """镜像预装路径：直接从已解开的目录复制，不需要打包。"""
    source = workdir / "source" / "syncoj_agent"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('__version__ = "7.7.7"\n', encoding="utf-8")
    (source / "main.py").write_text("print('x')\n", encoding="utf-8")
    code = workdir / "code"
    code.mkdir()

    rc = run_installer(installer, [
        "--from-dir", str(workdir / "source"),
        "--server", "https://x", "--scan-root", str(code),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"), "--unit-dir", str(workdir / "units"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == 0
    assert installer.current_version(workdir / "opt") == "7.7.7"


def test_sha256_mismatch_is_rejected(installer, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz")
    rc = run_installer(installer, [
        "--bundle", str(bundle),
        "--sha256", "0" * 64,
        "--server", "https://x", "--scan-root", str(workdir),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR
    assert not (workdir / "opt" / "releases").exists() or not list(
        (workdir / "opt" / "releases").iterdir()
    )


def test_sha256_malformed_is_rejected(installer, workdir: Path) -> None:
    rc = run_installer(installer, [
        "--bundle", str(make_bundle(workdir / "b.tar.gz")),
        "--sha256", "zzz",
        "--server", "https://x", "--scan-root", str(workdir),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR


def test_rejects_bundle_with_wrong_top_level(installer, workdir: Path) -> None:
    """名字像压缩包的普通文件不该被当成 Agent 装进去。"""
    bundle = workdir / "wrong.tar.gz"
    with tarfile.open(str(bundle), "w:gz") as archive:
        payload = b"hello"
        info = tarfile.TarInfo("somethingelse/readme.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    rc = run_installer(installer, [
        "--bundle", str(bundle),
        "--server", "https://x", "--scan-root", str(workdir),
        "--prefix", str(workdir / "opt"), "--config-dir", str(workdir / "etc"),
        "--state-dir", str(workdir / "state"),
        "--skip-user", "--skip-service", "--skip-python-check", "--quiet",
    ])
    assert rc == installer.EXIT_ERROR


# --------------------------------------------------------------------------- #
# 打包器
# --------------------------------------------------------------------------- #


def test_build_bundle_produces_installable_archive(workdir: Path) -> None:
    """打包器产出的包必须与安装器的期望结构一致 —— 这是三处约定的交汇点
    （build_bundle / install.py / upgrade.safe_extract_tar）。"""
    spec = importlib.util.spec_from_file_location(
        "syncoj_build_bundle", PACKAGING / "build_bundle.py"
    )
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    out = workdir / "bundle.tar.gz"
    version, size, digest = builder.build(out, AGENT_ROOT)

    assert out.is_file() and size > 0 and len(digest) == 64
    assert parse_version(version), "产出的版本号必须可解析"

    with tarfile.open(str(out)) as archive:
        names = [m.name for m in archive.getmembers()]
    tops = {n.split("/")[0] for n in names}
    # 包目录 + 发布根上的启动器（main.py 用相对导入，必须靠启动器才能执行）
    assert tops == {"syncoj_agent", "run_agent.py"}
    assert "syncoj_agent/main.py" in names
    assert not any("__pycache__" in n for n in names)
    assert not any(n.startswith("syncoj_agent/tests/") for n in names)
    assert not any(".pytest-tmp" in n for n in names)


def test_build_bundle_is_reproducible(workdir: Path) -> None:
    """同样的内容必须产出同样的字节，**哪怕输出文件名不同**。

    否则"同一版本两次打包校验和不同"，签名的确定性与"这个包是不是那个包"的
    判断都会失效。注意这里刻意用了两个不同的文件名 —— gzip 头里的 FNAME 字段
    默认会带上输出文件名，那是这个坑最隐蔽的地方。
    """
    spec = importlib.util.spec_from_file_location(
        "syncoj_build_bundle2", PACKAGING / "build_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first = workdir / "version-a.tar.gz"
    second = workdir / "version-b-with-longer-name.tar.gz"
    _v1, _s1, d1 = module.build(first, AGENT_ROOT)
    _v2, _s2, d2 = module.build(second, AGENT_ROOT)

    assert d1 == d2, "同样内容两次打包的 sha256 不同"
    assert first.read_bytes() == second.read_bytes()
