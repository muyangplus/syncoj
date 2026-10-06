"""真机上下文下的安装器：把 ``install.py`` 单独一个文件摆进空目录，脱离仓库跑。

为什么单开这一个文件
--------------------
其余安装器测试都是 ``importlib`` 把 ``install.py`` 当**模块**加载，于是 pytest 的
工作目录 / 包路径把 repo 放进了 ``sys.path`` —— 安装器里任何
``from syncoj_agent import ...`` 在那个上下文里都能 import 成功，而真机上必然：

.. code-block:: text

    File "/tmp/syncoj-bootstrap.2563/install.py", line 578, in _verify_signature_if_possible
        from syncoj_agent import discovery
    ModuleNotFoundError: No module named 'syncoj_agent'

``bootstrap.sh`` 下发的正是**单个** ``install.py``（落到
``/tmp/syncoj-bootstrap.XXXX/``），这里也照真机来：

* 把 ``install.py`` 复制到一个**只有它**的临时目录（不含 ``syncoj_agent/``）；
* ``cwd`` 换到另一个干净的临时目录；
* 清空 ``PYTHONPATH``；
* 用子进程把它当**脚本**跑起来。

这样"安装器 import 自己要安装的包"会立刻变成 ModuleNotFoundError，而不是被测试
环境的 ``sys.path`` 掩盖过去。三条曾经踩雷的路线各有一条：验签、``--from-server``
时的局域网发现、写配置时的局域网发现。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
PACKAGING = AGENT_ROOT / "packaging"
SERVER_DIR = REPO_ROOT / "server"

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from syncoj_agent.upgrade import symlinks_supported  # noqa: E402
from syncoj_server.services.signing import (  # noqa: E402
    generate_keypair,
    openssl_available,
    write_public_key_json,
)

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

#: 发现协议里那段被签名的文本前缀。**手写**，故意不 import 任何一端 ——
#: 这里要能发现"某一边把前缀改了"，而不是跟着它一起改。
DISCOVERY_SIGNATURE_PREFIX = "syncoj-discovery-v1"
#: 发现用的固定端口（安装器的客户端里没有暴露端口的开关）。
DISCOVERY_PORT = 45871


# --------------------------------------------------------------------------- #
# 真机上下文的跑法
# --------------------------------------------------------------------------- #


class Standalone:
    """``install.py`` 单文件 + 干净 cwd + 清空环境的子进程跑法。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        #: 模拟 bootstrap.sh 的落点：/tmp/syncoj-bootstrap.XXXX/ —— 里面**只有**安装器
        self.bootstrap = tmp_path / "syncoj-bootstrap.standalone"
        self.bootstrap.mkdir()
        self.installer = self.bootstrap / "install.py"
        shutil.copyfile(str(PACKAGING / "install.py"), str(self.installer))

        self.cwd = tmp_path / "cwd"
        self.cwd.mkdir()

        self.env = dict(os.environ)
        # 真机上没有 PYTHONPATH；清掉它，"repo 在 sys.path 里"这条退路就不存在了
        self.env.pop("PYTHONPATH", None)
        # 管道里也按 UTF-8 写，下面按文案断言才可靠
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["PYTHONUNBUFFERED"] = "1"

    def _spawn(self, argv, extra_env=None, timeout: int = 120):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            argv,
            cwd=str(self.cwd),
            env=env,
            capture_output=True,
            timeout=timeout,
        )

    def run(self, args, extra_env=None, timeout: int = 120):
        """把 install.py 当脚本跑起来。"""
        return self._spawn([sys.executable, str(self.installer)] + list(args), extra_env, timeout)

    def python(self, code: str, extra_env=None):
        """在同一个上下文里跑一段一次性代码（用来证明这里确实 import 不到包）。"""
        return self._spawn([sys.executable, "-c", code], extra_env, 60)


@pytest.fixture()
def standalone(tmp_path: Path) -> Standalone:
    return Standalone(tmp_path)


def text_of(result) -> str:
    """stdout + stderr 合成一段文本（子进程已按 UTF-8 写）。"""
    return (result.stdout + result.stderr).decode("utf-8", "replace")


def layout_args(standalone: Standalone, prefix: Path = None):
    prefix = prefix or (standalone.tmp / "opt")
    return [
        "--prefix", str(prefix),
        "--config-dir", str(standalone.tmp / "etc"),
        "--state-dir", str(standalone.tmp / "state"),
        "--unit-dir", str(standalone.tmp / "units"),
        "--skip-user", "--skip-service", "--skip-python-check",
    ]


# --------------------------------------------------------------------------- #
# 夹具的守卫：先证明这个上下文里真的 import 不到包
# --------------------------------------------------------------------------- #


def test_真机上下文里根本_import_不到包(standalone: Standalone) -> None:
    """守卫的守卫。

    如果这里能 import 成功，说明 repo 仍然在 ``sys.path`` 里 —— 那下面那些用例
    就可能是"被环境放水"才通过的，白写。
    """
    result = standalone.python("import syncoj_agent")

    assert result.returncode != 0, "这个上下文里居然能 import syncoj_agent，说明没脱离仓库"
    assert "ModuleNotFoundError" in text_of(result)


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #


def make_bundle(path: Path, version: str = "0.1.0") -> Path:
    """一个结构合法的 Agent 包（顶层 syncoj_agent/ + run_agent.py）。"""
    entries = [
        ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version),
        ("syncoj_agent/main.py", "print('hi')\n"),
        ("run_agent.py", "print('launcher')\n"),
    ]
    with tarfile.open(str(path), "w:gz") as archive:
        for name, content in entries:
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return path


def make_ledger(bundle: bytes, key, version: str = "0.1.0") -> dict:
    """服务端那份装机台账（签名盖在 sha256 上）。"""
    sha = hashlib.sha256(bundle).hexdigest()
    signature = (
        base64.urlsafe_b64encode(key.sign(sha.encode("ascii"))).decode("ascii").rstrip("=")
    )
    return {
        "version": version,
        "sha256": sha,
        "size": len(bundle),
        "signature": signature,
        "key_id": key.key_id,
        "notes": None,
        "bundle": "/api/v1/agent/install/bundle",
        "installer": "/api/v1/agent/install/installer",
    }


def fake_public_key_json(byte_length: int = 256) -> str:
    """造一份**格式合法**的公钥 JSON（不要求真是 RSA 密钥，够验签器解析即可）。

    这样"写配置时的发现"那条用例不必依赖 openssl。
    """
    def b64(value: int) -> str:
        length = (value.bit_length() + 7) // 8 or 1
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode("ascii").rstrip("=")

    n = (1 << (byte_length * 8 - 1)) + 12345  # 恰好 byte_length*8 位
    return json.dumps({"alg": "RS256", "key_id": "standalone", "n": b64(n), "e": b64(65537)})


@contextlib.contextmanager
def fake_http(ledger: dict, bundle: bytes):
    """一个真的 HTTP 服务端：给出台账与包体（走 urllib 那条真实路径）。"""

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
            if self.path.startswith("/api/v1/agent/install.json"):
                payload = json.dumps(ledger, ensure_ascii=False).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            if self.path.startswith("/api/v1/agent/install/bundle"):
                self._send(200, bundle, "application/gzip")
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


def canonical(nonce: str, url: str) -> str:
    return "%s\n%s\n%s" % (DISCOVERY_SIGNATURE_PREFIX, nonce, url)


class FakeDiscovery:
    """一个只会应答的假服务端：从探测里取 nonce，回一条**签过名**的应答。

    安装器在 ``--from-server`` 这条路里是**广播**（客户端没有端口开关，
    ``--discover-address`` 在那条路上不生效），所以这里绑 ``0.0.0.0`` 收广播。
    """

    def __init__(self, signing_key, url: str, port: int = DISCOVERY_PORT) -> None:
        self.signing_key = signing_key
        self.url = url
        self.seen = []
        self.stop = threading.Event()
        self.thread = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("", port))
        except OSError:
            self.sock.close()
            pytest.skip("发现端口 %d 被占用，无法架设假服务端" % port)

    def __enter__(self) -> "FakeDiscovery":
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.sock.close()

    def _loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                raw, peer = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover - 套接字被关掉了
                return
            self.seen.append(raw)
            try:
                nonce = json.loads(raw.decode("utf-8"))["nonce"]
            except (UnicodeDecodeError, ValueError, KeyError, TypeError):
                continue
            signature = (
                base64.urlsafe_b64encode(
                    self.signing_key.sign(canonical(nonce, self.url).encode("utf-8"))
                )
                .decode("ascii")
                .rstrip("=")
            )
            reply = json.dumps(
                {
                    "syncoj": "syncoj",
                    "v": 1,
                    "nonce": nonce,
                    "url": self.url,
                    "key_id": "test",
                    "sig": signature,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                self.sock.sendto(reply, peer)
            except OSError:  # pragma: no cover
                return


# --------------------------------------------------------------------------- #
# 1) 验签：有公钥就要真的验，而且脱离仓库也能验
# --------------------------------------------------------------------------- #


@requires_openssl
def test_脱离仓库也能验签成功(standalone: Standalone) -> None:
    """走到"_verify_signature_if_possible"那一步并**真的验过**。

    从前这里是 ``from syncoj_agent import discovery`` + ``from syncoj_agent.rsa
    import verify_pkcs1v15_sha256`` —— 在单文件安装器里就是真机那条
    ModuleNotFoundError。
    """
    key = generate_keypair(standalone.tmp / "release.pem", bits=2048)
    pub = standalone.tmp / "release-key.pub.json"
    write_public_key_json(pub, key)

    bundle = make_bundle(standalone.tmp / "bundle.tar.gz").read_bytes()
    ledger = make_ledger(bundle, key)
    prefix = standalone.tmp / "opt"

    with fake_http(ledger, bundle) as server:
        result = standalone.run(
            ["--from-server", "--server", server.base, "--public-key", str(pub)]
            + layout_args(standalone, prefix)
        )

    text = text_of(result)
    assert "ModuleNotFoundError" not in text, text
    assert "签名校验通过" in text, text
    # 验签通过之后才会走到"安装版本"那一步 —— 落盘的版本目录是硬证据
    assert (prefix / "releases" / "0.1.0" / "syncoj_agent" / "__init__.py").is_file(), text
    if symlinks_supported():
        assert result.returncode == 0, text


@requires_openssl
def test_脱离仓库也真的验签_签名不对就拒绝(standalone: Standalone) -> None:
    """自包含实现不能是"永远返回 True"的空壳：别人签的必须被拒。"""
    key = generate_keypair(standalone.tmp / "release.pem", bits=2048)
    other = generate_keypair(standalone.tmp / "other.pem", bits=2048)
    pub = standalone.tmp / "release-key.pub.json"
    write_public_key_json(pub, key)

    bundle = make_bundle(standalone.tmp / "bundle.tar.gz").read_bytes()
    ledger = make_ledger(bundle, other)  # 包是**别人**签的
    prefix = standalone.tmp / "opt"

    with fake_http(ledger, bundle) as server:
        result = standalone.run(
            ["--from-server", "--server", server.base, "--public-key", str(pub)]
            + layout_args(standalone, prefix)
        )

    text = text_of(result)
    assert "ModuleNotFoundError" not in text, text
    assert "签名校验不通过" in text, text
    assert result.returncode != 0
    assert not (prefix / "releases" / "0.1.0").exists(), "验签没过却把包装上了"


# --------------------------------------------------------------------------- #
# 2) 局域网发现：不给 --server 时自己去发现（真实 UDP 往返 + 验签）
# --------------------------------------------------------------------------- #


@requires_openssl
def test_脱离仓库也能在局域网里验签找到服务端(standalone: Standalone) -> None:
    """``--from-server`` 没给 ``--server`` 时走发现路（原来在 line 514 import）。

    预览模式足以走完整条发现：``_fetch_base_url`` 在 dry-run 判断**之前**就发探测。
    假服务端签一段发现应答，安装器必须验得过并把它当成 base url。
    """
    key = generate_keypair(standalone.tmp / "release.pem", bits=2048)
    pub = standalone.tmp / "release-key.pub.json"
    write_public_key_json(pub, key)

    url = "http://127.0.0.1:18000"
    with FakeDiscovery(key, url) as fake:
        result = standalone.run(
            [
                "--from-server", "--dry-run",
                "--public-key", str(pub),
                "--discover-timeout", "1.5",
            ]
            + layout_args(standalone)
        )

    text = text_of(result)
    # 这一条最关键：不管网络通不通，都不允许是 ModuleNotFoundError
    assert "ModuleNotFoundError" not in text, text
    if not fake.seen:
        pytest.skip("本环境发不出 UDP 广播，验不了完整的发现链路")
    assert result.returncode == 0, text
    assert url in text, text


# --------------------------------------------------------------------------- #
# 3) 写配置时的发现路（原来在 line 839 延迟 import，dry-run 也一样炸）
# --------------------------------------------------------------------------- #


def test_脱离仓库也能走写配置时的发现路(standalone: Standalone) -> None:
    """包里没内嵌地址、机器上已有发布公钥时，写配置会去局域网发现。

    预览不真的发探测，但**公钥解析**与发现分支必须真的跑到 —— 那句
    "在局域网里寻找服务端"就是走到了的证据（公钥读不出来时根本到不了这里）。
    """
    version = "9.9.9"
    agent_dir = standalone.tmp / "agent"
    (agent_dir / "syncoj_agent").mkdir(parents=True)
    (agent_dir / "syncoj_agent" / "__init__.py").write_text(
        '__version__ = "%s"\n' % version, encoding="utf-8"
    )

    prefix = standalone.tmp / "opt"
    release = prefix / "releases" / version
    release.mkdir(parents=True)
    # 信任锚已经装在这台机器上（install_public_key 的落点）
    (release / "release-key.pub.json").write_text(fake_public_key_json(), encoding="utf-8")

    result = standalone.run(
        ["--from-dir", str(agent_dir), "--dry-run"] + layout_args(standalone, prefix)
    )

    text = text_of(result)
    assert "ModuleNotFoundError" not in text, text
    assert result.returncode == 0, text
    assert "在局域网里寻找服务端" in text, text
