"""装机时 `server.url` 从哪来 —— 四层优先级，以及每层失败时**不许瞎猜**。

这件事的现场代价很高：地址错了，机器连不上服务端，而现象只有"注册不上"。
所以每一条来源都要能说明白，而"都没找到"时必须**明确警告** —— 出厂默认是
127.0.0.1，把它当真意味着 50 台机器各自连自己。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from syncoj_agent import discovery  # noqa: E402


@pytest.fixture()
def make(installer_module, workdir: Path):
    """造一个够用的 Installer，并把 ``releases/<版本>/`` 摆出来。"""

    def factory(version: str = "1.0.0", *, argv=None, server_json=None, public_key=True):
        options = installer_module.build_parser().parse_args(["--user", "syncoj"] + list(argv or []))
        options.config_dir = str(workdir / "etc")
        options.state_dir = str(workdir / "state")
        options.prefix = str(workdir / "opt")
        options.unit_dir = str(workdir / "units")
        instance = installer_module.Installer(
            options, installer_module.Reporter(quiet=True)
        )
        release = Path(options.prefix) / "releases" / version
        release.mkdir(parents=True, exist_ok=True)
        if server_json is not None:
            (release / "server.json").write_text(server_json, encoding="utf-8", newline="\n")
        if public_key:
            (release / installer_module.PUBLIC_KEY_FILENAME).write_text(
                json.dumps({"n": "12345", "e": "65537"}), encoding="utf-8", newline="\n"
            )
        return instance

    return factory


def allow_discovery(monkeypatch, outcome):
    """把"有可用公钥"和"发现的结果"一起钉住。

    夹具里那份 ``{"n":"12345"}`` 不是合法 RSA 公钥，真的走一遍 `load_public_key`
    会得到 None，于是发现这条路根本不成立 —— 而用例想测的是优先级。
    """
    monkeypatch.setattr(discovery, "load_public_key", lambda path: object())
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: outcome)


# --------------------------------------------------------------------------- #
# 优先级
# --------------------------------------------------------------------------- #


def test_包内地址优先于一切自动手段(make) -> None:
    """**主路径**：包是那台服务端自己打的，所以这个地址最可信，且不用人输。"""
    instance = make(server_json='{"v": 1, "url": "http://10.0.0.5:8000"}\n')

    url, origin = instance.resolve_server_url("1.0.0")

    assert url == "http://10.0.0.5:8000"
    assert "内嵌" in origin


def test_命令行给的地址压过包内(make) -> None:
    """操作员明确说了就听他的 —— 现场总有测试机要指到别的服务端。"""
    instance = make(
        server_json='{"v": 1, "url": "http://10.0.0.5:8000"}\n',
        argv=["--server", "https://10.0.0.9:8443/"],
    )

    url, origin = instance.resolve_server_url("1.0.0")

    assert url == "https://10.0.0.9:8443"
    assert origin == "--server"


def test_包内地址收尾斜杠被抹掉(make) -> None:
    """拼路径时多一个斜杠会变成 `//agent/tick`，有些反代会拒。"""
    instance = make(server_json='{"v": 1, "url": "http://10.0.0.5:8000/"}\n')

    url, _ = instance.resolve_server_url("1.0.0")

    assert url == "http://10.0.0.5:8000"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{}",
        '{"url": ""}',
        '{"url": "10.0.0.5:8000"}',          # 少了 scheme
        '{"url": "ftp://10.0.0.5"}',         # 不是 http(s)
        "[]",
    ],
)
def test_包内地址不像话就当没有(make, monkeypatch, payload) -> None:
    """坏数据要**跳过并继续往下找**，不是崩掉，也不是硬着头皮用。"""
    allow_discovery(monkeypatch, discovery.DiscoveryOutcome(None, []))
    instance = make(server_json=payload)

    url, origin = instance.resolve_server_url("1.0.0")

    assert url == instance_module_default(instance)
    assert "默认值" in origin


def instance_module_default(instance) -> str:
    return "https://127.0.0.1:8000"


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #


def test_包内没地址时用发现到的(make, monkeypatch) -> None:
    allow_discovery(
        monkeypatch,
        discovery.DiscoveryOutcome("http://10.0.0.7:8000", ["http://10.0.0.7:8000"]),
    )
    instance = make()

    url, origin = instance.resolve_server_url("1.0.0")

    assert url == "http://10.0.0.7:8000"
    assert "发现" in origin


def test_没有公钥就完全不问(make, monkeypatch) -> None:
    """没有公钥就分不出"服务端"和"随便一个应答者"，那就不该往局域网上喊。

    与"没有密钥就不升级"同一个默认。
    """
    called = []

    def boom(*args, **kwargs):  # pragma: no cover - 不该被调用
        called.append(args)
        raise AssertionError("没有公钥时不该发探测")

    monkeypatch.setattr(discovery, "discover", boom)
    instance = make(public_key=False)

    url, origin = instance.resolve_server_url("1.0.0")

    assert called == []
    assert "默认值" in origin


def test_歧义时不猜_退回默认并说明(make, monkeypatch) -> None:
    """两个服务端都回了应答。猜错的表现是"交上去了但成绩是空的"，且现场看不出来。"""
    allow_discovery(
        monkeypatch,
        discovery.DiscoveryOutcome(
            None, ["http://10.0.0.5:8000", "http://10.0.0.6:8000"]
        ),
    )
    instance = make()
    warnings = []
    instance.report.warn = lambda message: warnings.append(message)

    url, origin = instance.resolve_server_url("1.0.0")

    assert url == "https://127.0.0.1:8000"
    assert "默认值" in origin
    assert any("多个服务端" in message for message in warnings), warnings


def test_no_discover_跳过发现(make, monkeypatch) -> None:
    def boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("--no-discover 时不该发探测")

    monkeypatch.setattr(discovery, "discover", boom)
    instance = make(argv=["--no-discover"])
    warnings = []
    instance.report.warn = lambda message: warnings.append(message)

    url, origin = instance.resolve_server_url("1.0.0")

    assert url == "https://127.0.0.1:8000"
    assert "默认值" in origin
    assert any("no-discover" in message for message in warnings), warnings


def test_discover_address_直接问指定地址(make, monkeypatch) -> None:
    """有些交换机禁广播 —— 那时教师至少能填一个地址让它直接问。"""
    seen = {}

    monkeypatch.setattr(discovery, "load_public_key", lambda path: object())

    def fake_discover(public_key, **kwargs):
        seen.update(kwargs)
        return discovery.DiscoveryOutcome("http://10.0.0.7:8000", ["http://10.0.0.7:8000"])

    monkeypatch.setattr(discovery, "discover", fake_discover)
    instance = make(argv=["--discover-address", "10.0.0.7", "--discover-timeout", "0.4"])

    instance.resolve_server_url("1.0.0")

    assert seen["targets"] == ["10.0.0.7"]
    assert seen["timeout"] == 0.4


# --------------------------------------------------------------------------- #
# 都没找到时的警告：这条是"别让人查半天"
# --------------------------------------------------------------------------- #


def test_都没找到时必须警告说清后果(make, monkeypatch) -> None:
    """出厂默认是 127.0.0.1 —— 把它当真，50 台机器会各自连自己。

    所以这条警告必须同时说清**用了什么值**和**会出什么事**，否则现场只会看到
    "注册不上"，然后去查网络、查密钥、查服务端日志，就是不会想到配置文件里那行。
    """
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: discovery.DiscoveryOutcome(None, []))
    instance = make()
    warnings = []
    instance.report.warn = lambda message: warnings.append(message)

    url, _ = instance.resolve_server_url("1.0.0")

    assert url == "https://127.0.0.1:8000"
    joined = " ".join(warnings)
    assert "127.0.0.1" in joined
    assert "连自己" in joined
    assert "必须改" in joined


def test_成功时不留警告(make) -> None:
    """找到了就别说废话 —— 警告刷多了就没人看了。"""
    instance = make(server_json='{"v": 1, "url": "http://10.0.0.5:8000"}\n')
    warnings = []
    instance.report.warn = lambda message: warnings.append(message)

    instance.resolve_server_url("1.0.0")

    assert warnings == []


def test_内嵌地址与实际写进配置的一致(make, installer_module) -> None:
    """解析出来的地址必须真的落进 agent.ini —— 否则前面这些都没意义。

    用预览模式跑 ``write_config``：它会走完解析与渲染，但不碰磁盘。
    """
    instance = make(server_json='{"v": 1, "url": "http://10.0.0.5:8000"}\n')
    instance.report.dry_run = True
    instance.options.dry_run = True
    instance.options.server = None
    instance.options.bootstrap_key = "SECRET"

    rendered = {}

    def fake_render(**kwargs):
        rendered.update(kwargs)
        return "[server]\nurl = %s\n" % kwargs["server_url"]

    instance_module = installer_module
    original = instance_module.render_config
    instance_module.render_config = fake_render
    try:
        instance.write_config("1.0.0")
    finally:
        instance_module.render_config = original

    assert rendered["server_url"] == "http://10.0.0.5:8000"

def test_公钥读不出来就不发现(make, monkeypatch) -> None:
    """分不清"服务端"和"随便一个应答者"时，不猜。"""
    called = []

    def boom(*args, **kwargs):  # pragma: no cover - 不该被调用
        called.append(args)
        raise AssertionError("公钥读不出来时不该发探测")

    monkeypatch.setattr(discovery, "load_public_key", lambda path: None)
    monkeypatch.setattr(discovery, "discover", boom)
    instance = make()

    url, origin = instance.resolve_server_url("1.0.0")

    assert called == []
    assert url == "https://127.0.0.1:8000"
    assert "默认值" in origin
