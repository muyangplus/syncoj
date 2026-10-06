"""机器侧「管理端授权卸载」：令牌验签 + Agent 侧行为。

授权模型：服务端用发布私钥签一枚一次性令牌，机器本地用**已有的**升级信任锚
（``release-key.pub.json``）验签，验过才以 root 删。这里盯三件事：

* **验签真的挡住伪造**：别的密钥签的、过期的、绑别的机器的令牌都必须被拒；
* **令牌只走 stdin**：不进 argv（ps 能看到）、不进日志、不进 ``last_error``；
* **执行成功就退出**：卸载脚本 exit 0 → Agent 自己 exit 0（单元是
  ``Restart=on-failure``，不会再被拉起来）。

sudo/systemd 一律用注入的 runner 顶掉 —— 测试不碰真实系统。
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
SERVER_DIR = REPO_ROOT / "server"
for candidate in (AGENT_ROOT, SERVER_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from syncoj_agent.config import AgentConfig  # noqa: E402
from syncoj_agent.main import Agent  # noqa: E402
from syncoj_agent.state import STATE_READY, resolve_machine_uuid  # noqa: E402

try:
    from syncoj_server.services.signing import (  # noqa: E402
        generate_keypair,
        openssl_available,
        write_public_key_json,
    )

    HAS_OPENSSL = openssl_available() is not None
except ImportError:  # pragma: no cover
    HAS_OPENSSL = False

requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

MACHINE_UUID = "a" * 32
TOKEN = "FAKE.TOKEN.PAYLOAD.FOR.TESTS"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(
    key,
    *,
    machine_uuid: str = MACHINE_UUID,
    kind: str = "agent_uninstall",
    version: int = 1,
    ttl: int = 900,
    signer=None,
) -> str:
    """按冻结的格式签一枚令牌：``b64url(payload) + "." + b64url(signature)``。

    签名对象是**第一段那串 ASCII 字符本身**（不是原始 JSON）—— JSON 的键序与
    空白在两端不保证相同。这里手写一份而不是 import 服务端实现：要能从外部
    独立核对，"两边一起改错"也能通过就白测了。
    """
    now = int(time.time())
    payload = {
        "v": version,
        "kind": kind,
        "machine_uuid": machine_uuid,
        "agent_id": 7,
        "nonce": "b" * 32,
        "issued_at": now,
        "expires_at": now + ttl,
        "key_id": key.key_id,
    }
    signed = b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = (signer or key).sign(signed.encode("ascii"))
    return "%s.%s" % (signed, b64url(signature))


@pytest.fixture()
def signing_key(tmp_path: Path):
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    return generate_keypair(tmp_path / "release.pem", bits=2048)


@pytest.fixture()
def verifier_env(installer_module, signing_key, tmp_path: Path):
    """摆好验签脚本 + 公钥 + 权威机器身份，返回它们的路径。"""
    script = tmp_path / "verify_uninstall_token.py"
    with script.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(installer_module._VERIFY_TOKEN_SCRIPT)

    pub = tmp_path / "release-key.pub.json"
    write_public_key_json(pub, signing_key)

    uuid_file = tmp_path / "machine_uuid"
    with uuid_file.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(MACHINE_UUID + "\n")

    return SimpleNamespace(
        script=script, key=signing_key, pub=pub, uuid_file=uuid_file
    )


def decode(raw: bytes) -> str:
    """子进程输出按 UTF-8 优先解码。

    生产上是 ``python3 -E -s``，locale 是 UTF-8；但 ``-E`` 会让
    ``PYTHONIOENCODING`` 失效，Windows 开发机上管道默认走 ANSI（cp936）——
    所以这里要能退回去，不然断言会因为乱码而误报。
    """
    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_verifier(env, token: str, now: float = None):
    argv = [
        sys.executable, "-E", "-s", str(env.script),
        "--public-key", str(env.pub),
        "--machine-uuid-file", str(env.uuid_file),
    ]
    if now is not None:
        argv += ["--now", str(now)]
    result = subprocess.run(
        argv, input=token.encode("ascii", "replace"), capture_output=True
    )
    result.text = decode(result.stdout) + decode(result.stderr)
    return result


# --------------------------------------------------------------------------- #
# 验签：正确要过，伪造/过期/绑别机必须拒
# --------------------------------------------------------------------------- #


@requires_openssl
def test_正确的令牌通过(verifier_env) -> None:
    result = run_verifier(verifier_env, mint(verifier_env.key))

    text = result.text
    assert result.returncode == 0, text
    assert "通过" in text
    assert mint(verifier_env.key) not in text, "令牌不该被打印出来"


@requires_openssl
def test_别的密钥签的令牌被拒(verifier_env, tmp_path: Path) -> None:
    other = generate_keypair(tmp_path / "other.pem", bits=2048)
    token = mint(verifier_env.key, signer=other)

    result = run_verifier(verifier_env, token)

    text = result.text
    assert result.returncode != 0
    assert "签名" in text
    assert token not in text, "拒绝原因里不该回显令牌"


@requires_openssl
def test_过期令牌被拒(verifier_env) -> None:
    token = mint(verifier_env.key)
    # 把 --now 挪到 expires_at 之后
    result = run_verifier(verifier_env, token, now=time.time() + 10_000)

    text = result.text
    assert result.returncode != 0
    assert "过期" in text


@requires_openssl
def test_绑别的机器的令牌被拒(verifier_env) -> None:
    token = mint(verifier_env.key, machine_uuid="c" * 32)

    result = run_verifier(verifier_env, token)

    text = result.text
    assert result.returncode != 0
    assert "不是发给这台机器" in text


@requires_openssl
def test_错误种类或版本的令牌被拒(verifier_env) -> None:
    bad_kind = run_verifier(verifier_env, mint(verifier_env.key, kind="something_else"))
    bad_version = run_verifier(verifier_env, mint(verifier_env.key, version=2))

    assert bad_kind.returncode != 0
    assert bad_version.returncode != 0


def test_空输入与超长输入被拒(verifier_env) -> None:
    empty = run_verifier(verifier_env, "")
    oversize = run_verifier(verifier_env, "x" * (9 * 1024))

    assert empty.returncode != 0
    assert "没有从 stdin 读到" in empty.text
    assert oversize.returncode != 0
    assert "过长" in oversize.text


# --------------------------------------------------------------------------- #
# Agent：心跳上报 + 收到令牌怎么处理
# --------------------------------------------------------------------------- #


def make_agent(tmp_path: Path) -> Agent:
    config = AgentConfig.load(None)
    config.state_dir = tmp_path / "state"
    config.deploy_root = tmp_path / "desktop"
    config.scan_roots = [tmp_path / "code"]
    config.bootstrap_key_file = tmp_path / "bootstrap.key"
    config.server_url = "https://127.0.0.1:8000"
    config.machine_uuid_file = tmp_path / "etc" / "machine_uuid"
    for path in (config.state_dir, config.deploy_root, config.scan_roots[0]):
        path.mkdir(parents=True, exist_ok=True)
    with config.bootstrap_key_file.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("SECRET\n")
    return Agent(config)


def test_心跳上报有没有可用的发布公钥(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    # 没配公钥 → False
    agent.config.release_public_key = None
    payload, _oversize, _errors = agent._build_tick_payload(
        [], {}, scan_complete=False
    )
    assert payload["release_public_key"] is False

    # 配了但文件不合法 → False（服务端发了令牌它也验不了，别给假希望）
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    agent.config.release_public_key = broken
    payload, _o, _e = agent._build_tick_payload([], {}, scan_complete=False)
    assert payload["release_public_key"] is False


def test_令牌只出现在_stdin(tmp_path: Path) -> None:
    """argv 里没有、stdin 里有 —— 这是"不进 ps"的全部依据。"""
    agent = make_agent(tmp_path)
    seen = []

    def runner(argv, stdin_bytes):
        seen.append((list(argv), stdin_bytes))
        return 0, ""

    agent._run_uninstall = runner

    assert agent._handle_uninstall_token(TOKEN) is True

    assert len(seen) == 1
    argv, stdin_bytes = seen[0]
    assert TOKEN not in " ".join(argv), argv
    assert stdin_bytes == TOKEN.encode("utf-8")
    assert argv[:2] == ["sudo", "-n"]
    assert argv[2].endswith("self_uninstall.sh")
    # 成功 → 主循环退出（exit 0；单元是 Restart=on-failure，不会再拉起来）
    assert agent._stop is True
    assert agent._last_error is None


def test_卸载成功时_agent_退出(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent._run_uninstall = lambda argv, stdin_bytes: (0, "")

    handled = agent._handle_uninstall_token(TOKEN)

    assert handled is True
    assert agent._stop is True


def test_卸载失败写_last_error_并退避(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    calls = []

    def runner(argv, stdin_bytes):
        calls.append(1)
        return 1, "sudo: no tty present and no askpass program specified\n"

    agent._run_uninstall = runner

    assert agent._handle_uninstall_token(TOKEN) is False
    assert agent._stop is False
    assert agent._last_error is not None
    assert "卸载失败" in agent._last_error
    assert agent._uninstall_blocked_until > 0

    # 退避窗口内不再去撞 sudo
    assert agent._handle_uninstall_token(TOKEN) is False
    assert len(calls) == 1


def test_失败摘要里不含令牌(tmp_path: Path, caplog) -> None:
    """底层命令万一回显了 stdin，摘要也必须把它抹掉 —— 它是一次 root 删除的凭据。"""
    agent = make_agent(tmp_path)
    agent._run_uninstall = lambda argv, stdin_bytes: (1, "echo: %s" % TOKEN)

    with caplog.at_level(logging.DEBUG):
        assert agent._handle_uninstall_token(TOKEN) is False

    assert TOKEN not in (agent._last_error or ""), agent._last_error
    assert TOKEN not in caplog.text, "令牌进了日志"


def test_cycle_收到令牌就执行并返回零(tmp_path: Path, monkeypatch) -> None:
    """把"心跳带令牌 → 执行 → 停止主循环"这条线完整走一遍（不碰真 sudo/systemd）。"""
    agent = make_agent(tmp_path)
    credential = SimpleNamespace(
        state=STATE_READY, player_no="P001", contest_slug="C001"
    )
    monkeypatch.setattr(agent, "_ensure_credential", lambda: credential)
    monkeypatch.setattr(agent, "_sync_notice_files", lambda cred: None)
    monkeypatch.setattr(agent, "_ensure_roots", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_scan", lambda: ([], {}))
    monkeypatch.setattr(
        agent, "_build_tick_payload", lambda *a, **k: ({}, [], [])
    )
    monkeypatch.setattr(
        agent.client,
        "tick",
        lambda payload: {
            "claimed": True,
            "bound": True,
            "uninstall_token": TOKEN,
            "next_tick_seconds": 20,
        },
    )
    seen = []
    monkeypatch.setattr(
        agent,
        "_run_uninstall",
        lambda argv, stdin_bytes: seen.append(stdin_bytes) or (0, ""),
    )

    delay = agent.cycle()

    assert seen == [TOKEN.encode("utf-8")]
    assert delay == 0.0
    assert agent._stop is True


# --------------------------------------------------------------------------- #
# 权威机器身份
# --------------------------------------------------------------------------- #


def test_机器身份优先读权威那份(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "machine_uuid").write_text("state-side\n", encoding="utf-8")
    authority = tmp_path / "machine_uuid"
    authority.write_text("authority-side\n", encoding="utf-8")

    assert resolve_machine_uuid(state_dir, authority) == "authority-side"


def test_没有权威那份时退回状态目录并警告(tmp_path: Path, caplog) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "machine_uuid").write_text("state-side\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        value = resolve_machine_uuid(state_dir, tmp_path / "absent")

    assert value == "state-side"
    assert "退回状态目录" in caplog.text
