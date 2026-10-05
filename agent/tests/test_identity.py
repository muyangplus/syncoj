"""机器身份的生成与持久化。

身份错了，代码就会落进另一个人的目录，而且**完全静默** —— 成绩矩阵看起来只是
"谁少交了"。整个方案里换机器、换场次、快照还原都靠这三样东西辨认：

======================  ====================================================================
``machine_uuid``        Agent 首次运行时生成并持久化。**首选身份**
``machine_fingerprint`` 主板 SMBIOS UUID。整机快照还原后**不变**，而 UUID 会一起消失
``machine_id``          ``/etc/machine-id``。**仅供人工辨认**，不参与身份判定
======================  ====================================================================

后两者的分工最容易搞混，而搞混的代价最大：

* 拿 ``machine_id`` 当身份 —— 克隆镜像时它整批相同，一批机器会互相覆盖
* 指纹读不到时拿主机名或 ``machine_id`` 顶替 —— 克隆镜像同样整批相同，
  把"认回原机器"变成"认错机器"

所以下面每一条断言的都是**"错了会安静地出事"**的那一面。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from syncoj_agent import state as state_module
from syncoj_agent.state import (
    detect_desktop,
    resolve_machine_fingerprint,
    resolve_machine_id,
    resolve_machine_uuid,
)


# --------------------------------------------------------------------------- #
# machine_uuid
# --------------------------------------------------------------------------- #


def test_uuid_is_generated_and_persisted(workdir: Path) -> None:
    state_dir = workdir / "state"

    generated = resolve_machine_uuid(state_dir)

    assert generated, "首次运行必须生成一个 UUID"
    assert len(generated) == 32 and all(ch in "0123456789abcdef" for ch in generated)
    assert (state_dir / "machine_uuid").is_file(), "UUID 必须落盘"


def test_uuid_is_not_regenerated_when_the_state_dir_survives(workdir: Path) -> None:
    """重启、崩溃、被 kill、systemd 拉起 —— 状态目录还在就必须是**同一台机器**。

    每次启动都换 UUID 的话，服务端每轮都会看到一个"新机器"，于是那台机器会被
    反复放进待配对列表，而配对过的记录一条也用不上。
    """
    state_dir = workdir / "state"

    first = resolve_machine_uuid(state_dir)
    for _ in range(5):
        assert resolve_machine_uuid(state_dir) == first


def test_snapshot_restore_produces_a_new_uuid(workdir: Path) -> None:
    """整机快照还原会把状态目录一起抹掉 —— 那时 UUID 必须**变**。

    这里断言的是"变"，而不是"保持不变"：UUID 是 Agent 自己生成的随机值，
    它随状态目录一起消失，这正是服务端需要硬件指纹当第二道依据的原因
    （见 ``test_fingerprint_is_the_only_thing_that_survives_a_wipe``）。
    """
    state_dir = workdir / "state"
    before = resolve_machine_uuid(state_dir)

    # 模拟整机快照还原：状态目录整个不见
    (state_dir / "machine_uuid").unlink()
    after = resolve_machine_uuid(state_dir)

    assert after != before, "状态目录被抹掉之后必须重新生成 UUID"


def test_fingerprint_does_not_depend_on_local_state() -> None:
    """它必须**与状态目录无关** —— 状态目录正是快照还原会抹掉的东西。

    这条断言守的是一个改动方向：谁要是"顺手"把指纹也缓存进状态目录（看起来很
    自然：少读一次 ``/sys``），快照还原之后它就跟着没了，而那正是唯一需要它的
    时刻。所以它的签名里不许出现任何本地状态。

    看起来像在测实现，其实测的是**这条设计约束本身**：一旦有人加了参数，
    红的就是这里，而失败信息直接说出为什么不能加。
    """
    import inspect

    params = inspect.signature(state_module.resolve_machine_fingerprint).parameters
    assert not params, (
        "指纹不能依赖任何本地状态（状态目录会被快照还原抹掉），"
        "实际参数：%s" % list(params)
    )


def test_uuid_survives_an_unwritable_state_dir(workdir: Path, monkeypatch) -> None:
    """写不进状态目录时不能崩。

    那种情况下 UUID 每次启动都会变（服务端会看到"新机器"，走人工配对），
    但 Agent 本身必须还能跑起来 —— 宁可多几次人工配对，也不能整场考试收不到代码。
    """
    state_dir = workdir / "state"

    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(state_module, "atomic_write_text", boom)

    generated = resolve_machine_uuid(state_dir)
    assert generated
    assert not (state_dir / "machine_uuid").exists()


def test_uuid_file_is_sanitized_on_read(workdir: Path) -> None:
    """落盘的文件可能被人手工改过（比如塞了换行或奇怪字符）。

    它会被放进请求体、日志、数据库里，所以读的时候同样要收紧字符集。
    """
    state_dir = workdir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "machine_uuid").write_text("  ABC\n\tdef  \n", encoding="utf-8")

    assert resolve_machine_uuid(state_dir) == "ABC-def"


# --------------------------------------------------------------------------- #
# machine_id：只给人看
# --------------------------------------------------------------------------- #


def test_configured_machine_id_wins(workdir: Path) -> None:
    assert resolve_machine_id("configured-id", workdir / "state") == "configured-id"


def test_machine_id_falls_back_to_a_persisted_value(workdir: Path, monkeypatch) -> None:
    """没有任何系统标识时（Windows 开发机、特殊环境）退到随机值 ——

    但必须**持久化**：每次启动都不一样的话，服务端眼里就是一台新机器。
    """
    monkeypatch.setattr(state_module, "_read_text", lambda path: "")

    state_dir = workdir / "state"
    first = resolve_machine_id("", state_dir)
    assert resolve_machine_id("", state_dir) == first


def test_machine_id_is_never_used_as_a_fingerprint(workdir: Path, monkeypatch) -> None:
    """指纹读不到时返回**空串**，绝不能退到 machine-id 或主机名。

    克隆镜像时 ``/etc/machine-id`` 常常整批相同，主机名也一样（都是同一份镜像
    起的）—— 退回去等于把"认回原机器"变成"认错机器"。
    """
    monkeypatch.setattr(state_module, "_read_text", lambda path: "")

    fingerprint = resolve_machine_fingerprint()
    assert fingerprint == ""
    assert fingerprint != socket.gethostname()


# --------------------------------------------------------------------------- #
# machine_fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_is_empty_or_a_real_hardware_uuid() -> None:
    """要么读到真东西，要么空着。

    空串是安全的：服务端见到空指纹就当新机器处理（走人工配对），而不是拿空值
    去和别的机器"互相认回"。编一个值出来才是危险的。
    """
    fingerprint = resolve_machine_fingerprint()
    if fingerprint == "":
        return
    compact = fingerprint.replace("-", "")
    assert len(compact) == 32, "不像 SMBIOS UUID: %r" % fingerprint
    assert all(ch in "0123456789abcdefABCDEF" for ch in compact)


def test_fingerprint_reads_smbios_first(workdir: Path, monkeypatch) -> None:
    """从 ``/sys/class/dmi/id/product_uuid`` 读 —— 那是主板的标识。"""
    seen = []

    def fake_read(path) -> str:
        seen.append(str(path))
        if "product_uuid" in str(path):
            return "4c4c4544-0031-3010-8043-b7c04f4d4432\n"
        return ""

    monkeypatch.setattr(state_module, "_read_text", fake_read)

    assert resolve_machine_fingerprint() == "4c4c4544-0031-3010-8043-b7c04f4d4432"
    assert seen, "应该去读 SMBIOS"


@pytest.mark.parametrize(
    "bogus",
    [
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "00000000000000000000000000000000",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
    ],
)
def test_fingerprint_rejects_placeholder_uuids(bogus: str, monkeypatch) -> None:
    """虚拟机与某些 OEM 主板会给出全 0 或全 F 的 UUID。

    那不是身份 —— 一台物理机上装着 50 台这样的虚拟机时，它们会全部整批相同，
    于是 50 台机器互相"认回"，而其中 49 台的代码会写进同一个人的目录。
    """
    monkeypatch.setattr(state_module, "_read_text", lambda path: bogus)

    assert resolve_machine_fingerprint() == ""


def test_fingerprint_skips_unreadable_sources(monkeypatch) -> None:
    """第一个来源读不到就试下一个，而不是直接放弃。"""
    calls = []

    def fake_read(path) -> str:
        calls.append(str(path))
        if "devices/virtual" in str(path):
            return "4c4c4544-0031-3010-8043-b7c04f4d4432"
        return ""

    monkeypatch.setattr(state_module, "_read_text", fake_read)

    assert resolve_machine_fingerprint() == "4c4c4544-0031-3010-8043-b7c04f4d4432"
    assert len(calls) >= 2


# --------------------------------------------------------------------------- #
# 桌面探测（配对码与提示文件要写到这里）
# --------------------------------------------------------------------------- #


def test_desktop_prefers_the_xdg_configuration(workdir: Path) -> None:
    """用户可能把桌面挪到别处，或者设成英文名 —— ``user-dirs.dirs`` 最权威。"""
    home = workdir / "home"
    custom = home / "我的工作区"
    custom.mkdir(parents=True)
    config_dir = home / ".config"
    config_dir.mkdir(parents=True)
    (config_dir / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/我的工作区"\n', encoding="utf-8"
    )

    assert detect_desktop(home) == custom


def test_desktop_ignores_a_stale_xdg_path(workdir: Path) -> None:
    """配置里指着一个已经不存在的目录（用户删了它）时，别把 Agent 卡在那儿。"""
    home = workdir / "home"
    (home / "桌面").mkdir(parents=True)
    config_dir = home / ".config"
    config_dir.mkdir(parents=True)
    (config_dir / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/早就不在了"\n', encoding="utf-8"
    )

    assert detect_desktop(home) == home / "桌面"


def test_desktop_recognises_both_naming_conventions(workdir: Path) -> None:
    """中文 locale 是「桌面」，英文是 ``Desktop`` —— NOI Linux 不一定装了语言包。

    每个候选各用一个独立的 home：Windows 的文件名不区分大小写，
    否则 ``Desktop`` 与 ``desktop`` 会落到同一个目录里，测试自己先撞上。
    """
    for index, name in enumerate(("桌面", "Desktop", "desktop")):
        home = workdir / ("home-%d" % index)
        (home / name).mkdir(parents=True)
        assert detect_desktop(home) == home / name


def test_desktop_falls_back_to_chinese_not_home(workdir: Path) -> None:
    """探测不出来时按中文环境猜，**不要**退回家目录。

    退回家目录会让 Agent 把整个家目录当成工作区扫，收上来一堆无关文件 ——
    那是"回收了一堆不认识的代码"，比"少收几个文件"难查得多。
    """
    home = workdir / "empty-home"
    home.mkdir(parents=True)

    assert detect_desktop(home) == home / "桌面"
