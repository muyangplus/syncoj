"""安装策略：``bootstrap_key_policy`` / ``config_policy`` / ``upgrade_mode``。

发布包（顶层 ``install_policy.json``）与服务端装机台账都可以带这三样 —— 键名是
**冻结的接口**。机器侧在**装机**时按它们行动：

* ``bootstrap_key_policy``：包内带了注册密钥时，机器上已有的那把要不要被覆盖
  （默认 ``replace``，**先备份旧的**）。现场就是被"旧的已吊销密钥挡住新的"坑到的。
* ``config_policy``：``agent.ini`` 逐键三态 —— ``keep``（默认，一个字节不动）/
  ``default``（只更新"没人动过"的键）/ ``force``（无条件覆盖）。
* ``upgrade_mode``：写进 ``agent.ini`` 的自更新模式，默认 ``apply``。

**永不改**：状态与凭据文件（``credential.json`` / ``machine_uuid`` / ``machine_id``
/ 哈希缓存）—— 这条不设开关。
"""

from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

PORT = "0.1.0"


@pytest.fixture()
def installer(installer_module, workdir: Path):
    """造一个只为测策略而存在的 Installer（不碰 systemd、不联网）。"""

    def factory(argv=()):
        options = installer_module.build_parser().parse_args(
            ["--user", "noi", "--server", "https://10.0.0.1:8443"] + list(argv)
        )
        options.config_dir = str(workdir / "etc")
        options.state_dir = str(workdir / "state")
        options.prefix = str(workdir / "opt")
        options.unit_dir = str(workdir / "units")
        instance = installer_module.Installer(
            options, installer_module.Reporter(quiet=True)
        )
        # 模块级常量（文件名等）挂在实例上，测试里少传一个参数
        instance.agent_module = installer_module
        return instance

    return factory


def collect(instance):
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


def seed_release(instance, version: str = PORT, public_key: bool = True) -> None:
    """铺一个版本目录；``public_key=True`` 时带上发布公钥（可升级的信任锚）。"""
    release = instance.prefix / "releases" / version
    release.mkdir(parents=True, exist_ok=True)
    if public_key:
        (release / instance.agent_module.PUBLIC_KEY_FILENAME).write_text(
            '{"kty": "RSA"}\n', encoding="utf-8"
        )


def read_values(path: Path) -> dict:
    parser = configparser.ConfigParser()
    parser.optionxform = str.lower
    parser.read_string(path.read_text(encoding="utf-8"))
    return {section: dict(parser.items(section)) for section in parser.sections()}


def seed_config(instance, text: str, *, snapshot: str = None) -> Path:
    """在配置目录里放一份"机器上已有的 agent.ini"（可选带快照）。"""
    instance.config_dir.mkdir(parents=True, exist_ok=True)
    path = instance.config_dir / instance.agent_module.CONFIG_FILENAME
    _write_text(path, text)
    if snapshot is not None:
        _write_text(
            instance.config_dir / instance.agent_module.CONFIG_SNAPSHOT_FILENAME,
            snapshot,
        )
    return path


def _write_text(path: Path, text: str) -> None:
    """``write_text(newline=)`` 是 3.10 才有的；目标机是 3.8（check_py38 会挡）。"""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


OLD_CONFIG = """\
[server]
url = https://10.0.0.1:8443

[scan]
; 教师手工改过这一行
interval = 30
"""


# --------------------------------------------------------------------------- #
# 策略从哪儿来、怎么解析
# --------------------------------------------------------------------------- #


def test_包内策略文件会被读到(installer, workdir: Path) -> None:
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "install_policy.json").write_text(
        json.dumps(
            {
                "upgrade_mode": "stage",
                "bootstrap_key_policy": "keep",
                "config_policy": {"scan.interval": "force"},
            }
        ),
        encoding="utf-8",
    )

    policy = installer().load_install_policy(source)

    assert policy.upgrade_mode == "stage"
    assert policy.bootstrap_key_policy == "keep"
    assert policy.config_action("scan", "interval") == "force"
    assert policy.origin == "包内策略"


def test_台账里的策略优先于包内(installer, workdir: Path) -> None:
    """``--from-server`` 时台账是这台服务端**现在**的说法，包可能是几天前打的。"""
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "install_policy.json").write_text(
        json.dumps({"upgrade_mode": "off"}), encoding="utf-8"
    )
    instance = installer()
    instance.ledger = {"upgrade_mode": "apply"}

    policy = instance.load_install_policy(source)

    assert policy.upgrade_mode == "apply", "台账没有覆盖包内策略"
    assert policy.origin == "包内策略 + 服务端台账"


def test_策略名认不出来时警告并忽略(installer_module, installer) -> None:
    """策略是锦上添花的东西 —— 取值写错不能让整台机器装不上，但必须说出来。"""
    instance = installer()
    messages = collect(instance)

    policy = installer_module.parse_install_policy(
        {
            "upgrade_mode": "sometimes",
            "bootstrap_key_policy": "nope",
            "config_policy": {"scan.interval": "maybe", "nosuchkey": "force", "bad": 1},
        },
        report=instance.report,
    )

    assert policy.upgrade_mode == ""
    assert policy.bootstrap_key_policy == ""
    assert policy.config_action("scan", "interval") == installer_module.CONFIG_POLICY_KEEP
    warning = texts(messages, "warn")
    assert "upgrade_mode" in warning and "bootstrap_key_policy" in warning, warning
    assert "maybekey" or "maybe" in warning, warning


# --------------------------------------------------------------------------- #
# 1) bootstrap_key_policy
# --------------------------------------------------------------------------- #


def test_策略_keep_时不覆盖机器上那把(installer, workdir: Path, installer_module) -> None:
    """包内带密钥 + 显式 keep → 机器上那把不动（旧行为，仍然可用）。"""
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "bootstrap.key").write_text("BUNDLED\n", encoding="utf-8")
    (source / "install_policy.json").write_text(
        json.dumps({"bootstrap_key_policy": "keep"}), encoding="utf-8"
    )
    instance = installer()
    instance.install_policy = instance.load_install_policy(source)
    target = instance.config_dir / instance.agent_module.BOOTSTRAP_KEY_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("EXISTING\n", encoding="utf-8")

    instance.install_bootstrap_key(PORT, source)

    assert target.read_text(encoding="utf-8").strip() == "EXISTING"


def test_显式给密钥永远最优先(installer, workdir: Path) -> None:
    """``--bootstrap-key`` 是操作员明确说的，任何策略都不能把它压掉。"""
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "bootstrap.key").write_text("BUNDLED\n", encoding="utf-8")
    (source / "install_policy.json").write_text(
        json.dumps({"bootstrap_key_policy": "replace"}), encoding="utf-8"
    )
    instance = installer(["--bootstrap-key", "EXPLICIT"])
    instance.install_policy = instance.load_install_policy(source)

    assert instance.install_bootstrap_key(PORT, source) is True

    target = instance.config_dir / instance.agent_module.BOOTSTRAP_KEY_FILENAME
    assert target.read_text(encoding="utf-8").strip() == "EXPLICIT"


def test_包内不带密钥时机器上那把不动(
    installer, workdir: Path, installer_module
) -> None:
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "install_policy.json").write_text(
        json.dumps({"bootstrap_key_policy": "replace"}), encoding="utf-8"
    )
    instance = installer()
    instance.install_policy = instance.load_install_policy(source)
    target = instance.config_dir / instance.agent_module.BOOTSTRAP_KEY_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("EXISTING\n", encoding="utf-8")

    assert instance.install_bootstrap_key(PORT, source) is True

    assert target.read_text(encoding="utf-8").strip() == "EXISTING", (
        "包内没带密钥，策略不该有任何效果"
    )
    assert not list(target.parent.glob("bootstrap.key.replaced-*"))


# --------------------------------------------------------------------------- #
# 2) config_policy：逐键三态
# --------------------------------------------------------------------------- #


def policy_for(installer_module, mapping: dict):
    return installer_module.parse_install_policy(mapping)


def test_默认策略下老配置一个字节都不动(installer, installer_module) -> None:
    """**默认全部 keep** —— 教师改过的东西不许被悄悄改掉。"""
    instance = installer()
    instance.install_policy = policy_for(installer_module, {})
    path = seed_config(instance, OLD_CONFIG)
    before = path.read_bytes()

    instance.write_config(PORT)

    assert path.read_bytes() == before


def test_default_只补齐缺失的键(installer, workdir: Path, installer_module) -> None:
    """机器上没有这个键 = 用的就是默认值 → 可以写成新的。"""
    instance = installer()
    seed_release(instance)
    instance.install_policy = policy_for(
        installer_module, {"config_policy": {"upgrade.mode": "default"}}
    )
    path = seed_config(instance, OLD_CONFIG)

    instance.write_config(PORT)

    values = read_values(path)
    assert values["upgrade"]["mode"] == "apply"
    assert values["scan"]["interval"] == "30", "没点名的键不许动"


def test_default_不覆盖被人改过的键(installer, installer_module) -> None:
    """**default 的全部意义**：值被人改过 → 保留，哪怕它是"模板默认"。"""
    instance = installer()
    seed_release(instance, version=PORT, public_key=True)
    instance.install_policy = policy_for(
        installer_module, {"config_policy": {"scan.interval": "default"}}
    )
    # 快照里写的是安装器上次写的值（默认 60），现场被教师改成了 30
    seed_config(instance, OLD_CONFIG, snapshot="[scan]\ninterval = 60\n")

    instance.write_config(PORT)

    assert read_values(instance.config_path)["scan"]["interval"] == "30", "教师改的值被覆盖了"


def test_default_在快照缺失时按人改过处理(installer, installer_module) -> None:
    """老机器没有快照 → 安全侧：当作"人可能改过"，保留。"""
    instance = installer()
    seed_release(instance)
    instance.install_policy = policy_for(
        installer_module, {"config_policy": {"scan.interval": "default"}}
    )
    path = seed_config(instance, OLD_CONFIG)  # 故意不给快照

    instance.write_config(PORT)

    assert read_values(path)["scan"]["interval"] == "30"


def test_force_无条件覆盖(installer, installer_module) -> None:
    instance = installer()
    seed_release(instance)
    instance.install_policy = policy_for(
        installer_module, {"config_policy": {"scan.interval": "force"}}
    )
    path = seed_config(instance, OLD_CONFIG, snapshot="[scan]\ninterval = 60\n")
    # force 要能在"模板默认值"上生效：60 → 渲染出来的新值
    _write_text(path, OLD_CONFIG.replace("interval = 30", "interval = 60"))

    instance.write_config(PORT)

    values = read_values(path)
    assert values["scan"]["interval"] == "60"
    # 注释与其它内容原样保留
    assert "教师手工改过这一行" in path.read_text(encoding="utf-8")


def test_逐键更新不碰其它内容(installer, installer_module) -> None:
    """只改点名的键：注释、顺序、没点名的键都得原样留着。"""
    instance = installer()
    seed_release(instance)
    instance.install_policy = policy_for(
        installer_module, {"config_policy": {"scan.interval": "force"}}
    )
    text = OLD_CONFIG + "\n[extra]\n; 教师自己加的东西\nmine = 1\n"
    path = seed_config(instance, text, snapshot="[scan]\ninterval = 60\n")

    instance.write_config(PORT)

    after = path.read_text(encoding="utf-8")
    assert "教师手工改过这一行" in after
    assert "教师自己加的东西" in after
    assert read_values(path)["extra"]["mine"] == "1"
    assert read_values(path)["scan"]["interval"] == "60"


def test_force_config_仍然整份重建(installer, installer_module) -> None:
    """显式 ``--force-config`` 是操作员说的"覆盖它"，优先级高于逐键策略。"""
    instance = installer(["--force-config"])
    seed_release(instance)
    instance.install_policy = policy_for(
        installer_module, {"config_policy": {"scan.interval": "default"}}
    )
    path = seed_config(instance, OLD_CONFIG)

    instance.write_config(PORT)

    after = path.read_text(encoding="utf-8")
    assert "教师手工改过这一行" not in after, "force-config 应当整份重建"
    assert read_values(path)["scan"]["interval"] == "60"


def test_每次写配置都会留下快照(installer, installer_module) -> None:
    instance = installer()
    seed_release(instance)
    instance.write_config(PORT)

    snapshot = instance.config_dir / instance.agent_module.CONFIG_SNAPSHOT_FILENAME
    assert snapshot.is_file()
    assert snapshot.read_text(encoding="utf-8") == instance.config_path.read_text(
        encoding="utf-8"
    )


def test_状态与凭据文件永远不在策略的作用域里(
    installer, installer_module
) -> None:
    """``credential.json`` / ``machine_uuid`` 之类不设开关，也不许被顺手改掉。"""
    instance = installer()
    seed_release(instance)
    instance.install_policy = policy_for(
        installer_module,
        {
            "config_policy": {
                # 有人试图用策略点名状态文件：它们不是 agent.ini 的键 → 忽略并警告
                "state.credential": "force",
                "agent.state_dir": "force",
            }
        },
    )
    instance.state_dir.mkdir(parents=True, exist_ok=True)
    credential = instance.state_dir / "credential.json"
    machine_uuid = instance.state_dir / "machine_uuid"
    credential.write_text('{"token": "x"}\n', encoding="utf-8")
    machine_uuid.write_text("uuid\n", encoding="utf-8")
    seed_config(instance, OLD_CONFIG)
    messages = collect(instance)

    instance.write_config(PORT)

    assert credential.read_text(encoding="utf-8") == '{"token": "x"}\n'
    assert machine_uuid.read_text(encoding="utf-8") == "uuid\n"
    assert "不存在" in texts(messages, "warn"), texts(messages, "warn")


# --------------------------------------------------------------------------- #
# 3) upgrade_mode
# --------------------------------------------------------------------------- #


def test_默认升级模式是_apply(installer, installer_module) -> None:
    instance = installer()
    seed_release(instance)

    instance.write_config(PORT)

    assert read_values(instance.config_path)["upgrade"]["mode"] == "apply"


def test_包内策略能覆盖模板默认(installer, workdir: Path) -> None:
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "install_policy.json").write_text(
        json.dumps({"upgrade_mode": "stage"}), encoding="utf-8"
    )
    instance = installer()
    seed_release(instance)
    instance.install_policy = instance.load_install_policy(source)

    instance.write_config(PORT)

    assert read_values(instance.config_path)["upgrade"]["mode"] == "stage"


def test_显式_cli_优先于包内策略(installer, workdir: Path) -> None:
    source = workdir / "bundle"
    source.mkdir(parents=True)
    (source / "install_policy.json").write_text(
        json.dumps({"upgrade_mode": "apply"}), encoding="utf-8"
    )
    instance = installer(["--upgrade-mode", "off"])
    seed_release(instance)
    instance.install_policy = instance.load_install_policy(source)

    instance.write_config(PORT)

    assert read_values(instance.config_path)["upgrade"]["mode"] == "off"
    assert instance.resolve_upgrade_mode() == ("off", "--upgrade-mode")


def test_没有公钥时自动降级成_off(installer, installer_module) -> None:
    """没有信任锚就验不了签名 —— 写个升不了的模式只会让 Agent 起不来。"""
    instance = installer()
    seed_release(instance, public_key=False)
    messages = collect(instance)

    instance.write_config(PORT)

    assert read_values(instance.config_path)["upgrade"]["mode"] == "off"
    assert "没有信任锚" in texts(messages, "warn")


def test_老配置里的_mode_off_在默认策略下不会被改(installer, installer_module) -> None:
    """**这是用户特别点名的场景。**

    老机器上写死的 ``mode = off`` 属于"机器已有配置"：默认策略是 keep，
    安装器不许把它悄悄改成 apply（想改就得 force 或 --force-config）。
    """
    instance = installer()
    seed_release(instance)
    old = "[upgrade]\nmode = off\ninstall_root = /opt/syncoj\n"
    path = seed_config(instance, old, snapshot=old)
    # 包带了"apply"策略，但 config_policy 没点名 upgrade.mode → keep
    instance.install_policy = policy_for(
        installer_module, {"upgrade_mode": "apply"}
    )

    instance.write_config(PORT)

    assert read_values(path)["upgrade"]["mode"] == "off", "老配置被悄悄改了"
