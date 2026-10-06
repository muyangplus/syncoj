"""安装包内附带的统一注册密钥：``bootstrap.key``。

契约
----
* 附带形式：安装包（tar.gz）**根目录**下一个 ``bootstrap.key``，与
  ``syncoj_agent/``、``run_agent.py`` 同级。
* 找密钥的顺序：``--bootstrap-key`` / ``--bootstrap-key-file``（显式）>
  目标机上已有的 ``<config-dir>/bootstrap.key`` > **这次拿到的包内附带的那份**。
* 装上时是 ``<config-dir>/bootstrap.key``、0600、属主 root。
* 那份密钥是敏感文件：**解包产物**不许留在 ``releases/<版本>/``（选手账号可读）。

**必须直接读包，不能等解包**：现场踩过一次 —— 同版本已安装时
``install_release`` 会"跳过解压"，包内那份密钥永远落不到磁盘，于是
"没有统一密钥，装不出注册单元"。下面 ``test_版本已安装…`` 两条就是钉这个的。

**安全底线**（也是这些用例背后的理由）：密钥只能来自上述本地来源，绝不允许为了它
去访问网络 —— 否则任何人只要连得到服务端就能拿到注册凭证。
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

BUNDLE_KEY = "BUNDLED-BOOTSTRAP-KEY"
EXPLICIT_KEY = "EXPLICIT-BOOTSTRAP-KEY"
EXISTING_KEY = "ALREADY-ON-THE-MACHINE"
FROM_DIR_KEY = "FROM-DIR-BOOTSTRAP-KEY"
REBUILT_KEY = "REBUILT-SAME-VERSION-KEY"


def make_bundle(path: Path, version: str = "0.1.0", bootstrap_key=None) -> Path:
    """造一个结构合法的安装包；``bootstrap_key`` 非 None 时顶层带上它。"""
    entries = [
        ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version),
        ("syncoj_agent/main.py", "print('hi')\n"),
        ("run_agent.py", "print('launcher')\n"),
    ]
    if bootstrap_key is not None:
        # 顶层成员，与 syncoj_agent/、run_agent.py 同级 —— 契约就是这么定的
        entries.append(("bootstrap.key", bootstrap_key + "\n"))

    with tarfile.open(str(path), "w:gz") as archive:
        for name, content in entries:
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return path


def make_installer(installer_module, workdir: Path, argv=()):
    options = installer_module.build_parser().parse_args(["--user", "syncoj"] + list(argv))
    options.config_dir = str(workdir / "etc")
    options.state_dir = str(workdir / "state")
    options.prefix = str(workdir / "opt")
    options.unit_dir = str(workdir / "units")
    instance = installer_module.Installer(
        options, installer_module.Reporter(dry_run=options.dry_run, quiet=True)
    )
    return instance


def collect_messages(instance):
    """把 report 的四类输出收起来，供断言文案用。"""
    collected = []
    for kind in ("warn", "note", "action", "plan", "skip"):
        setattr(
            instance.report,
            kind,
            lambda message, k=kind: collected.append((k, message)),
        )
    return collected


def texts(collected, kind: str) -> str:
    return "\n".join(message for tag, message in collected if tag == kind)


def config_key_of(instance) -> Path:
    return Path(instance.options.config_dir) / "bootstrap.key"


def release_key_of(instance, version: str) -> Path:
    return Path(instance.options.prefix) / "releases" / version / "bootstrap.key"


def enroll_unit_of(instance) -> Path:
    return Path(instance.options.unit_dir) / "syncoj-agent-enroll.service"


# --------------------------------------------------------------------------- #
# 包里有 / 没有
# --------------------------------------------------------------------------- #


def test_包里有密钥就装上并清掉版本目录里那份(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=BUNDLE_KEY)
    instance = make_installer(installer_module, workdir)
    messages = collect_messages(instance)

    version = instance.install_release(bundle)
    stale = release_key_of(instance, version)
    assert stale.is_file(), "解包后这份密钥本来就在版本目录里 —— 前提没成立"

    assert instance.install_bootstrap_key(version, bundle) is True

    assert config_key_of(instance).read_text(encoding="utf-8").strip() == BUNDLE_KEY
    assert "使用安装包内附带的统一注册密钥" in texts(messages, "action")
    assert "版本 0.1.0" in texts(messages, "action")
    # 风险提示必须在
    assert "选手" in texts(messages, "note")
    assert not stale.exists(), "包里那份密钥不能留在选手可读的版本目录里"


def test_包里没有密钥时维持旧行为(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.2.0", bootstrap_key=None)
    instance = make_installer(installer_module, workdir)
    messages = collect_messages(instance)

    version = instance.install_release(bundle)

    assert instance.install_bootstrap_key(version, bundle) is False
    assert not config_key_of(instance).exists()
    assert "使用安装包内附带的统一注册密钥" not in texts(messages, "action")

    # 没有密钥 → 装不出注册单元（回执由 install_enroll_unit 负责说）
    instance.install_enroll_unit(False)
    assert not enroll_unit_of(instance).exists()


def test_from_dir_目录里带的密钥同样装上并清掉(installer_module, workdir: Path) -> None:
    """``--from-dir``（镜像预装）走的是目录来源：整棵目录被复制进版本目录。"""
    source = workdir / "agentdir"
    (source / "syncoj_agent").mkdir(parents=True)
    (source / "syncoj_agent" / "__init__.py").write_text(
        '__version__ = "0.3.0"\n', encoding="utf-8"
    )
    (source / "bootstrap.key").write_text(FROM_DIR_KEY + "\n", encoding="utf-8")

    instance = make_installer(installer_module, workdir, argv=["--from-dir", str(source)])
    version = instance.install_release(source)

    assert instance.install_bootstrap_key(version, source) is True
    assert config_key_of(instance).read_text(encoding="utf-8").strip() == FROM_DIR_KEY
    assert not release_key_of(instance, version).exists()
    # 只清解包产物；源目录是操作员自己的目录，不能动
    assert (source / "bootstrap.key").is_file(), "把 --from-dir 的源目录也清理了"


# --------------------------------------------------------------------------- #
# 现场踩过的组合：同版本已安装 + 包内带密钥
# --------------------------------------------------------------------------- #


def test_版本已安装时包内密钥仍然能装上(installer_module, workdir: Path) -> None:
    """用户勾了"附带密钥"重新构建、铺开了**同一个版本号**。

    装过的机器上 ``releases/0.1.0`` 已经存在；包里那份 ``bootstrap.key`` 必须能
    装上并长出注册单元 —— 现场就是断在这里（"没有统一密钥，装不出注册单元"）。
    """
    old = make_bundle(workdir / "old.tar.gz", version="0.1.0", bootstrap_key=None)
    # 同版本号重建，这次带上了密钥
    rebuilt = make_bundle(workdir / "rebuilt.tar.gz", version="0.1.0", bootstrap_key=REBUILT_KEY)

    instance = make_installer(installer_module, workdir)
    version = instance.install_release(old)
    assert version == "0.1.0"
    assert not release_key_of(instance, version).exists()

    # 同版本号、内容指纹变了 → 覆盖安装（老逻辑在这里"已安装，跳过解压"）
    assert instance.install_release(rebuilt) == "0.1.0"

    messages = collect_messages(instance)
    has_key = instance.install_bootstrap_key(version, rebuilt)

    assert has_key is True, "同版本已安装 + 包内带密钥：必须认出这份密钥"
    assert config_key_of(instance).read_text(encoding="utf-8").strip() == REBUILT_KEY
    assert "使用安装包内附带的统一注册密钥" in texts(messages, "action")

    # 这一步必须真的装上注册单元 —— 现场就是断在这里
    instance.install_enroll_unit(has_key)
    assert enroll_unit_of(instance).is_file()


def test_版本目录里没有密钥时仍然从包里读(installer_module, workdir: Path) -> None:
    """**读密钥必须直接读包，不能依赖"解包到 releases/<版本>/"。**

    真实的连续两次安装就是这样的：上一次已经把版本目录里那份密钥清掉了（那是
    刻意的 —— 那个目录选手可读），同时版本目录里留下了指纹 → 下一次
    ``install_release`` 会跳过、不会有任何解包产物。这时若配置目录里那份也没了
    （吊销之后手工删掉），重跑必须还能从**包**里把它补回来。
    """
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=BUNDLE_KEY)

    first = make_installer(installer_module, workdir)
    version = first.install_release(bundle)
    assert first.install_bootstrap_key(version, bundle) is True
    assert not release_key_of(first, version).exists(), (
        "上一次安装应当把版本目录里那份密钥清掉"
    )

    # 配置目录里的密钥没了（吊销/手工删除）
    config_key_of(first).unlink()

    second = make_installer(installer_module, workdir)
    assert second.install_release(bundle) == version, "指纹一致 → 跳过（幂等）"
    assert not release_key_of(second, version).exists(), "跳过时不会有解包产物"

    messages = collect_messages(second)
    has_key = second.install_bootstrap_key(version, bundle)

    assert has_key is True, "版本目录里没有密钥，但包里那份必须被读出来"
    assert config_key_of(second).read_text(encoding="utf-8").strip() == BUNDLE_KEY
    assert "使用安装包内附带的统一注册密钥" in texts(messages, "action")

    second.install_enroll_unit(has_key)
    assert enroll_unit_of(second).is_file()


def test_版本已安装且包内没有密钥时维持旧行为(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=None)
    instance = make_installer(installer_module, workdir)
    version = instance.install_release(bundle)
    # 同一份包 → 指纹一致 → 跳过（"已安装"）
    assert instance.install_release(bundle) == version

    messages = collect_messages(instance)
    has_key = instance.install_bootstrap_key(version, bundle)

    assert has_key is False
    assert not config_key_of(instance).exists()
    assert "使用安装包内附带的统一注册密钥" not in texts(messages, "action")

    instance.install_enroll_unit(has_key)
    assert not enroll_unit_of(instance).exists()


# --------------------------------------------------------------------------- #
# 优先级
# --------------------------------------------------------------------------- #


def test_显式给的最优先于包内(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=BUNDLE_KEY)
    instance = make_installer(installer_module, workdir, argv=["--bootstrap-key", EXPLICIT_KEY])
    messages = collect_messages(instance)

    version = instance.install_release(bundle)

    assert instance.install_bootstrap_key(version, bundle) is True
    assert config_key_of(instance).read_text(encoding="utf-8").strip() == EXPLICIT_KEY
    assert "使用安装包内附带的统一注册密钥" not in texts(messages, "action")
    # 没用上也不能留在版本目录里
    assert not release_key_of(instance, version).exists()


def test_bootstrap_key_file_内容也优先于包内(installer_module, workdir: Path) -> None:
    """``--bootstrap-key-file`` 是显式来源，与 ``--bootstrap-key`` 同一档。

    ``main()`` 会先把它读成 ``options.bootstrap_key``（见 ``resolve_bootstrap_key``），
    这里照那条路走一遍。
    """
    key_file = workdir / "explicit.key"
    key_file.write_text("FROM-FILE-KEY\n", encoding="utf-8")

    options = installer_module.build_parser().parse_args(
        ["--bootstrap-key-file", str(key_file)]
    )
    assert installer_module.resolve_bootstrap_key(options, installer_module.Reporter(quiet=True)) == "FROM-FILE-KEY"

    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=BUNDLE_KEY)
    instance = make_installer(installer_module, workdir, argv=["--bootstrap-key-file", str(key_file)])
    instance.options.bootstrap_key = "FROM-FILE-KEY"  # main() 就是这么塞进去的
    version = instance.install_release(bundle)

    assert instance.install_bootstrap_key(version, bundle) is True
    assert config_key_of(instance).read_text(encoding="utf-8").strip() == "FROM-FILE-KEY"
    assert not release_key_of(instance, version).exists()


def test_机器上已有的密钥优先于包内(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=BUNDLE_KEY)
    instance = make_installer(installer_module, workdir)

    config_key = config_key_of(instance)
    config_key.parent.mkdir(parents=True, exist_ok=True)
    config_key.write_text(EXISTING_KEY + "\n", encoding="utf-8")

    version = instance.install_release(bundle)

    assert instance.install_bootstrap_key(version, bundle) is True
    assert config_key.read_text(encoding="utf-8").strip() == EXISTING_KEY
    # 包内那份没被用上，但解包产物同样不能留在选手可读的版本目录里
    assert not release_key_of(instance, version).exists()


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #


def test_包内密钥装好后是_0600(installer_module, workdir: Path, monkeypatch) -> None:
    """它能注册整间机房，所以绝不能对选手账号可读。

    Windows 上 ``chmod`` 对组/其他位基本是空操作，所以**记录调用**；
    在 Linux（目标机）上再加一条真实权限位断言。
    """
    chmod_calls = []
    real_chmod = os.chmod

    def spy(path, mode, *args, **kwargs):
        chmod_calls.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(installer_module.os, "chmod", spy)

    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=BUNDLE_KEY)
    instance = make_installer(installer_module, workdir)
    version = instance.install_release(bundle)
    assert instance.install_bootstrap_key(version, bundle) is True

    config_key = config_key_of(instance)
    assert (str(config_key), 0o600) in chmod_calls
    if os.name == "posix":
        assert config_key.stat().st_mode & 0o777 == 0o600


def test_包里没有密钥时根本不会去写配置目录(installer_module, workdir: Path) -> None:
    bundle = make_bundle(workdir / "b.tar.gz", version="0.1.0", bootstrap_key=None)
    instance = make_installer(installer_module, workdir)
    version = instance.install_release(bundle)

    assert instance.install_bootstrap_key(version, bundle) is False
    assert not config_key_of(instance).exists()
