"""``.key/`` 的默认行为：安装器自动找密钥、安装包自带升级公钥。

这一批测试守的是"改默认行为"最容易出的两类事故：

1. **默认值把原来能用的方式弄坏了** —— 显式给的参数必须仍然最优先，不在源码
   仓库里跑时必须与从前一模一样。
2. **默认值悄悄做了危险的事** —— 比如凭空信任一个来路不明的公钥，或者把
   "装好了但没有密钥"变成安装失败。

按仓库的规矩，每条守卫都用变异验过"改坏会不会红"。
"""

from __future__ import annotations

import importlib.util
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
PACKAGING = AGENT_ROOT / "packaging"

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installer():
    return load_module("syncoj_installer_keys", PACKAGING / "install.py")


@pytest.fixture(scope="module")
def bundler():
    return load_module("syncoj_bundler_keys", PACKAGING / "build_bundle.py")


@pytest.fixture()
def workdir() -> Iterator[Path]:
    root = REPO_ROOT / ".pytest-tmp" / ("keys-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        import shutil

        shutil.rmtree(str(root), ignore_errors=True)


def make_installer(installer, workdir: Path, argv=()):
    options = installer.build_parser().parse_args(["--user", "tester", *argv])
    options.config_dir = str(workdir / "etc")
    options.state_dir = str(workdir / "state")
    options.prefix = str(workdir / "opt")
    options.unit_dir = str(workdir / "units")
    return installer.Installer(options, installer.Reporter(quiet=True))


# --------------------------------------------------------------------------- #
# 统一密钥：从哪来
# --------------------------------------------------------------------------- #


def key_readme(installer) -> object:
    return installer.Reporter(quiet=True)


def test_explicit_value_beats_every_default(installer, monkeypatch) -> None:
    """``--bootstrap-key`` 明文最优先 —— 显式给的必须赢过所有默认值。"""
    monkeypatch.setattr(installer, "DEFAULT_BOOTSTRAP_KEY_FILE", Path("nope/missing"))

    options = installer.build_parser().parse_args(
        ["--bootstrap-key", "  EXPLICIT-KEY  ", "--bootstrap-key-file", "also/missing"]
    )
    result = installer.resolve_bootstrap_key(options, key_readme(installer))

    assert result == "EXPLICIT-KEY"


def test_explicit_file_is_read(installer, workdir: Path, monkeypatch) -> None:
    monkeypatch.setattr(installer, "DEFAULT_BOOTSTRAP_KEY_FILE", Path("nope/missing"))
    path = workdir / "given.key"
    path.write_text("FROM-FILE\n", encoding="utf-8", newline="\n")

    options = installer.build_parser().parse_args(["--bootstrap-key-file", str(path)])
    assert installer.resolve_bootstrap_key(options, key_readme(installer)) == "FROM-FILE"


def test_missing_explicit_file_is_not_fatal(installer, workdir: Path, monkeypatch) -> None:
    """装机现场最常见的场景就是密钥还没放好。

    这时正确的结果是"装完了、下次补一个密钥"，而不是整个安装中断 ——
    中断会让教师以为安装包坏了。
    """
    monkeypatch.setattr(installer, "DEFAULT_BOOTSTRAP_KEY_FILE", Path("nope/missing"))
    options = installer.build_parser().parse_args(
        ["--bootstrap-key-file", str(workdir / "absent.key")]
    )
    assert installer.resolve_bootstrap_key(options, key_readme(installer)) is None


def test_empty_key_file_means_no_key(installer, workdir: Path, monkeypatch) -> None:
    """空文件不能让空串被当成密钥写进机器 —— 那会让注册单元带着一个空值开机。"""
    monkeypatch.setattr(installer, "DEFAULT_BOOTSTRAP_KEY_FILE", Path("nope/missing"))
    path = workdir / "empty.key"
    path.write_text("\n  \n", encoding="utf-8", newline="\n")

    options = installer.build_parser().parse_args(["--bootstrap-key-file", str(path)])
    assert installer.resolve_bootstrap_key(options, key_readme(installer)) is None


def test_checkout_default_is_used_when_nothing_is_passed(
    installer, workdir: Path, monkeypatch
) -> None:
    """源码仓库里跑时，默认就用 ``<仓库>/.key/bootstrap.key``。

    这是这次唯一"行为变了"的地方：教师不必再把密钥粘到命令行上（粘到
    shell 历史、屏幕、jump host 日志里的密钥是要收拾的）。
    """
    path = workdir / "bootstrap.key"
    path.write_text("FROM-CHECKOUT\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(installer, "DEFAULT_BOOTSTRAP_KEY_FILE", path)

    options = installer.build_parser().parse_args([])
    assert installer.resolve_bootstrap_key(options, key_readme(installer)) == "FROM-CHECKOUT"


def test_production_without_a_checkout_behaves_exactly_as_before(
    installer, monkeypatch
) -> None:
    """不在源码仓库里跑（安装器被拷进镜像/安装包）→ 与从前完全一样：没有密钥。

    这条守的是"两个平台给出的通过含义不同"那类事故：默认值只能在真的看到
    那个文件时才生效，猜出来的路径一律不算。
    """
    monkeypatch.setattr(installer, "DEFAULT_BOOTSTRAP_KEY_FILE", Path("definitely/missing"))

    options = installer.build_parser().parse_args([])
    assert installer.resolve_bootstrap_key(options, key_readme(installer)) is None


# --------------------------------------------------------------------------- #
# 升级公钥：打进包里、装到机器上
# --------------------------------------------------------------------------- #


def fake_public_key(workdir: Path) -> Path:
    path = workdir / "release-key.pub.json"
    path.write_text(
        '{\n  "algorithm": "RSA",\n  "n": "00",\n  "e": "10001",\n  "key_id": "test-key"\n}\n',
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_resolve_public_key_prefers_explicit(bundler, workdir: Path) -> None:
    path = fake_public_key(workdir)
    assert bundler.resolve_public_key(str(path)) == path


def test_resolve_public_key_refuses_a_missing_explicit_path(bundler, workdir: Path) -> None:
    """显式指定了不存在的公钥是**错误**，不是"那就别打进去"。

    静默打出一个不含公钥的包，会让人以为升级已经配好了 —— 而那正是最难查的
    失效方式：装得上、跑得动、就是不升级，且没有任何报错。
    """
    with pytest.raises(SystemExit):
        bundler.resolve_public_key(str(workdir / "absent.json"))


def test_resolve_public_key_falls_back_to_the_checkout(bundler, workdir: Path, monkeypatch) -> None:
    path = fake_public_key(workdir)
    monkeypatch.setattr(bundler, "DEFAULT_PUBLIC_KEY", path)
    assert bundler.resolve_public_key(None) == path


def test_resolve_public_key_is_none_when_the_checkout_has_none(bundler, monkeypatch) -> None:
    monkeypatch.setattr(bundler, "DEFAULT_PUBLIC_KEY", Path("definitely/missing.json"))
    assert bundler.resolve_public_key(None) is None


def test_bundle_embeds_the_public_key(bundler, workdir: Path) -> None:
    """包里有公钥，而且落在包目录**之外**（它是给安装器看的，不是 Agent 模块）。"""
    out = workdir / "with-key.tar.gz"
    bundler.build(out, AGENT_ROOT, public_key=fake_public_key(workdir))

    with tarfile.open(str(out)) as archive:
        names = archive.getnames()

    assert "release-key.pub.json" in names
    assert "run_agent.py" in names
    assert any(name.startswith("syncoj_agent/") for name in names)
    assert not any(name.startswith("syncoj_agent/release-key") for name in names)


def test_bundle_without_a_public_key_is_unchanged(bundler, workdir: Path) -> None:
    """没有公钥时打的包与从前一致：不凭空多一个文件出来。"""
    out = workdir / "without-key.tar.gz"
    bundler.build(out, AGENT_ROOT, public_key=None)

    with tarfile.open(str(out)) as archive:
        names = archive.getnames()

    assert "release-key.pub.json" not in names
    assert "run_agent.py" in names


def test_installer_installs_the_embedded_public_key(installer, workdir: Path) -> None:
    """安装包自带的公钥要落到 ``<config-dir>/``，配置指向那份副本。

    为什么不是直接指向 ``releases/<版本>/`` 里的原件：版本目录会被清理，
    而公钥是信任锚 —— 指向它意味着某天清理旧版本会顺手把签名验证整体废掉。
    """
    instance = make_installer(installer, workdir)
    release = Path(instance.options.prefix) / installer.RELEASES_DIR / "0.1.0"
    release.mkdir(parents=True, exist_ok=True)
    (release / installer.PUBLIC_KEY_FILENAME).write_text("{}\n", encoding="utf-8")

    result = instance.install_public_key("0.1.0")

    installed = Path(instance.options.config_dir) / installer.PUBLIC_KEY_FILENAME
    assert installed.is_file()
    assert result.replace("\\", "/") == installed.as_posix()
    # 副本必须独立于版本目录：把版本目录删掉，信任锚还在
    import shutil

    shutil.rmtree(str(Path(instance.options.prefix) / installer.RELEASES_DIR))
    assert installed.is_file()


def test_installer_keeps_an_explicit_public_key_where_it_is(installer, workdir: Path) -> None:
    """``--public-key`` 是运维自己放好的路径，我们不该搬动它。"""
    explicit = workdir / "ops-managed.pub.json"
    explicit.write_text("{}\n", encoding="utf-8")
    instance = make_installer(installer, workdir, ["--public-key", str(explicit)])

    assert instance.install_public_key("0.1.0") == str(explicit)
    assert not (Path(instance.options.config_dir) / installer.PUBLIC_KEY_FILENAME).exists()


def test_no_embedded_public_key_means_empty_config_value(installer, workdir: Path) -> None:
    """都没有 → 空串 → 自更新保持关闭，且**创建不出任何文件**。"""
    instance = make_installer(installer, workdir)
    Path(instance.options.prefix).mkdir(parents=True, exist_ok=True)

    assert instance.install_public_key("0.1.0") == ""
    assert not (Path(instance.options.config_dir) / installer.PUBLIC_KEY_FILENAME).exists()


def test_rendered_config_points_at_the_installed_public_key(installer, workdir: Path) -> None:
    """端到端：打包 → 装上 → 渲染出来的 ``agent.ini`` 指到那份副本。

    只测 ``install_public_key`` 的返回值不够 —— 真正决定行为的是**渲染进配置
    的那个值**，两处接错（装了但不写进配置）在返回值那一层看不出来。
    """
    instance = make_installer(installer, workdir)
    release = Path(instance.options.prefix) / installer.RELEASES_DIR / "0.1.0"
    release.mkdir(parents=True, exist_ok=True)
    (release / installer.PUBLIC_KEY_FILENAME).write_text("{}\n", encoding="utf-8")

    instance.write_config("0.1.0")

    config_text = Path(instance.config_path).read_text(encoding="utf-8")
    expected = (
        Path(instance.options.config_dir) / installer.PUBLIC_KEY_FILENAME
    ).as_posix()
    assert expected in config_text
    assert "public_key" in config_text
