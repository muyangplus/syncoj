"""`install.py --from-server`：空机器从服务端直接把 Agent 装上去。

这条路**不鉴权**（初次安装时机器上什么都没有），所以测试的重点不是"能不能下"，
而是"能验的都验了、没验的如实说了"：

* sha256 与台账不一致 → 停下（挡传输损坏与"传了一半"）
* 教师给的 ``--sha256`` 与服务端台账不一致 → 停下（前者是更强的证据）
* 有公钥而签名不对 → 停下（这一步才真正挡住"有人替你换了包"）
* 没有公钥 → **装，但明说未验签**，并把 sha256 打出来供人对照

服务端是用**真的 HTTP 服务**（stdlib ``http.server``）起的，不是打桩的 ——
要测的正是 urllib 那条真实路径上的行为（404 的 JSON detail、Range、连接失败）。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

SERVER_DIR = AGENT_ROOT.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from syncoj_server.services.signing import generate_keypair, openssl_available  # noqa: E402

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

LEDGER_PATH = "/api/v1/agent/install.json"


def canonical(nonce: str, url: str) -> str:  # 只为下面的签名工具占位
    return "%s\n%s\n%s" % ("syncoj-discovery-v1", nonce, url)


@contextlib.contextmanager
def fake_server(ledger, bundle: bytes, *, status: int = 200):
    """一个真的 HTTP 服务端：给出台账和包体。

    ``ledger=None`` 时模拟"还没有铺开任何版本"（404 + 一句中文 detail）。
    """
    body = bundle

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # 测试输出别刷屏
            pass

        def _send(self, code: int, payload: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 - http.server 的接口
            if self.path.startswith(LEDGER_PATH):
                if ledger is None:
                    payload = json.dumps(
                        {"detail": "还没有铺开任何 Agent 版本", "code": "install_unavailable"},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self._send(404, payload, "application/json")
                    return
                self._send(200, json.dumps(ledger, ensure_ascii=False).encode("utf-8"), "application/json")
                return
            if self.path.startswith("/api/v1/agent/install/bundle"):
                self._send(status, body, "application/gzip")
                return
            self._send(404, b"{}", "application/json")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, base="http://127.0.0.1:%d" % port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def make_ledger(bundle: bytes, version: str = "0.1.1", key=None, **overrides) -> dict:
    ledger = {
        "version": version,
        "sha256": hashlib.sha256(bundle).hexdigest(),
        "size": len(bundle),
        "signature": None,
        "key_id": None,
        "notes": None,
        "bundle": "/api/v1/agent/install/bundle",
        "installer": "/api/v1/agent/install/installer",
    }
    if key is not None:
        signature = key.sign(ledger["sha256"].encode("ascii"))
        ledger["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        ledger["key_id"] = key.key_id
    ledger.update(overrides)
    return ledger


@pytest.fixture()
def make_installer(installer_module, workdir: Path):
    """造一个只为测 fetch_bundle 而存在的 Installer。"""

    def factory(*argv, **kwargs):
        options = installer_module.build_parser().parse_args(["--user", "syncoj"] + list(argv))
        options.config_dir = str(workdir / "etc")
        options.state_dir = str(workdir / "state")
        options.prefix = str(workdir / "opt")
        options.unit_dir = str(workdir / "units")
        instance = installer_module.Installer(options, installer_module.Reporter(quiet=True))
        Path(options.config_dir).mkdir(parents=True, exist_ok=True)
        collected = []
        instance.report.warn = lambda message: collected.append(("warn", message))
        instance.report.note = lambda message: collected.append(("note", message))
        instance.report.action = lambda message: collected.append(("action", message))
        instance.report.plan = lambda message: collected.append(("plan", message))
        instance.messages = collected
        return instance

    return factory


def installer_module_errors(name: str):
    """按名字取 ``install.py`` 里的异常类。

    它不是一个包（``packaging/`` 不在 Agent 的 import 路径上），所以按路径加载
    之后才能拿到 ``InstallError``。
    """
    return getattr(sys.modules["syncoj_installer"], name)


def messages_of(instance, kind: str) -> str:
    return "\n".join(text for tag, text in instance.messages if tag == kind)


BUNDLE = b"fake-agent-bundle-bytes"


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_从服务端拿到包并校验_sha256(make_installer) -> None:
    instance = make_installer("--from-server")
    with fake_server(make_ledger(BUNDLE), BUNDLE) as server:
        instance.options.server = server.base
        path = instance.fetch_bundle()

    assert path.read_bytes() == BUNDLE
    assert "sha256 校验通过" in messages_of(instance, "note")


def test_没有公钥时明说未验签并打出校验和(make_installer) -> None:
    """**"装了但不知道装的是什么"比"装不上"更糟。**

    所以没公钥时不能静默放行：要警告、要留下 sha256 让人能跟界面对一遍。
    """
    instance = make_installer("--from-server")
    with fake_server(make_ledger(BUNDLE), BUNDLE) as server:
        instance.options.server = server.base
        instance.fetch_bundle()

    warning = messages_of(instance, "warn")
    assert "未验签" in warning
    assert hashlib.sha256(BUNDLE).hexdigest() in messages_of(instance, "note")


@requires_openssl
def test_有公钥时验签通过(make_installer, workdir: Path) -> None:
    key = generate_keypair(workdir / "release.pem", bits=2048)
    pub = workdir / "release-key.pub.json"
    from syncoj_server.services.signing import write_public_key_json

    write_public_key_json(pub, key)
    instance = make_installer("--from-server", "--public-key", str(pub))
    with fake_server(make_ledger(BUNDLE, key=key), BUNDLE) as server:
        instance.options.server = server.base
        instance.fetch_bundle()

    assert "签名校验通过" in messages_of(instance, "action")
    assert "未验签" not in messages_of(instance, "warn")


# --------------------------------------------------------------------------- #
# 每一类"不该信"的都停下
# --------------------------------------------------------------------------- #


@requires_openssl
def test_签名不对就中止(make_installer, workdir: Path) -> None:
    """服务端被换掉、或者中间人换了个包 —— 这一步是唯一能挡住它的。"""
    key = generate_keypair(workdir / "release.pem", bits=2048)
    other = generate_keypair(workdir / "other.pem", bits=2048)
    pub = workdir / "release-key.pub.json"
    from syncoj_server.services.signing import write_public_key_json

    write_public_key_json(pub, key)
    # 台账里那份包**是别人签的**
    ledger = make_ledger(BUNDLE, key=other)
    instance = make_installer("--from-server", "--public-key", str(pub))
    with fake_server(ledger, BUNDLE) as server:
        instance.options.server = server.base
        with pytest.raises(installer_module_errors("InstallError")) as exc:
            instance.fetch_bundle()

    assert "签名校验不通过" in str(exc.value)


@requires_openssl
def test_台账里的签名被改一个字符也过不去(make_installer, workdir: Path) -> None:
    """签名不是"存在就行"，要真的验得过。"""
    key = generate_keypair(workdir / "release.pem", bits=2048)
    pub = workdir / "release-key.pub.json"
    from syncoj_server.services.signing import write_public_key_json

    write_public_key_json(pub, key)
    ledger = make_ledger(BUNDLE, key=key)
    ledger["signature"] = "A" + ledger["signature"][1:]
    instance = make_installer("--from-server", "--public-key", str(pub))
    with fake_server(ledger, BUNDLE) as server:
        instance.options.server = server.base
        with pytest.raises(installer_module_errors("InstallError")):
            instance.fetch_bundle()


def test_包与台账对不上就中止(make_installer) -> None:
    """传了一半、或者中间被改了一个字节。"""
    ledger = make_ledger(BUNDLE)
    instance = make_installer("--from-server")
    with fake_server(ledger, BUNDLE + b"tampered") as server:
        instance.options.server = server.base
        with pytest.raises(installer_module_errors("InstallError")) as exc:
            instance.fetch_bundle()

    assert "对不上" in str(exc.value)


def test_教师给的校验和与服务端不一致就中止(make_installer) -> None:
    """教师从界面上抄来的那个是**更强**的证据（不来自这台服务端）。

    两边都要对得上，而不是"有一个就行"。
    """
    instance = make_installer("--from-server", "--sha256", "0" * 64)
    with fake_server(make_ledger(BUNDLE), BUNDLE) as server:
        instance.options.server = server.base
        with pytest.raises(installer_module_errors("InstallError")) as exc:
            instance.fetch_bundle()

    assert "--sha256" in str(exc.value)


def test_服务端说还没铺开时给人话(make_installer) -> None:
    """要能分清"还没铺开"和"连不上" —— 前者要做的是去界面点「铺开」。"""
    instance = make_installer("--from-server")
    with fake_server(None, BUNDLE) as server:
        instance.options.server = server.base
        with pytest.raises(installer_module_errors("InstallError")) as exc:
            instance.fetch_bundle()

    assert "还没有可装机" in str(exc.value)


def test_台账不是对象时拒绝(make_installer) -> None:
    instance = make_installer("--from-server")
    with fake_server(["不是对象"], BUNDLE) as server:
        instance.options.server = server.base
        with pytest.raises(installer_module_errors("InstallError")) as exc:
            instance.fetch_bundle()

    assert "对象" in str(exc.value)


# --------------------------------------------------------------------------- #
# 地址从哪来：不许悄悄用一个"看起来配好了"的默认值
# --------------------------------------------------------------------------- #


def test_没有地址也没有公钥时拒绝并说清怎么办(make_installer) -> None:
    """**不给地址就盲信一个应答**是最坏的一种：机器会连到别人选的服务端上。

    所以这里要求"要么给我地址，要么给我公钥去验局域网里的应答"。
    """
    instance = make_installer("--from-server")

    with pytest.raises(installer_module_errors("InstallError")) as exc:
        instance.fetch_bundle()

    message = str(exc.value)
    assert "--server" in message
    assert "--public-key" in message


def test_不给地址时不会退回_127_0_0_1(make_installer) -> None:
    """`resolve_server_url` 里有一条"退回默认值"的兜底，那是给**写配置**用的。

    取包这条路刻意不接它 —— 否则会去连本机的一个不存在的服务端，而报错是
    "下载失败"，指不到真正的原因。
    """
    instance = make_installer("--from-server")

    with pytest.raises(installer_module_errors("InstallError")) as exc:
        instance.fetch_bundle()

    assert "127.0.0.1" not in str(exc.value)


# --------------------------------------------------------------------------- #
# 预览不联网
# --------------------------------------------------------------------------- #


def test_预览模式不联网也能给计划(make_installer) -> None:
    """与 `--check` 同一条约定：预览不该因为服务端没起来就失败。"""
    instance = make_installer("--dry-run", "--from-server", "--server", "http://10.255.255.1:9")
    instance.options.dry_run = True
    instance.report.dry_run = True

    path = instance.fetch_bundle()

    assert path.name == "bundle.tar.gz"
    assert "取装机台账" in messages_of(instance, "plan")


def test_来源只能指定一个(make_installer) -> None:
    """`--from-server` 与 `--bundle` 同时给是写错了，不该猜哪个优先。"""
    instance = make_installer("--dry-run", "--from-server", "--bundle", "/nonexistent.tar.gz")
    # 预览模式：跳过 root 检查，才能走到"来源只能一个"那一条
    instance.options.dry_run = True
    instance.report.dry_run = True

    with pytest.raises(installer_module_errors("InstallError")) as exc:
        instance.preflight()

    assert "只能指定一个来源" in str(exc.value)
    assert "--from-server" in str(exc.value), "报错要把新加的来源列出来"
