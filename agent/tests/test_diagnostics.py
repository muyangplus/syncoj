"""诊断回传：定期把现场留给服务端（机器离线之后就抓不到了）。

三条硬要求各有一组用例：

1. **绝不带密钥与 token** —— ``config_summary`` 是**白名单**构造的，还要证明
   "把真实的 ``bootstrap.key`` 内容与 token 放进环境里构造出来的包，序列化字节里
   一个都搜不到"。
2. **触发节奏** —— 健康时 10 分钟一份、出错立即补（最小间隔 60 秒）、
   服务端在 tick 里 ``diagnostics_request=true`` 时传一份 ``manual``。
3. **上传失败绝不影响心跳** —— 只记 DEBUG（连续多次才 WARNING）、429 当作
   "这次没传成"、失败不会把 tick 带崩。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

import syncoj_agent.main as main_module  # noqa: E402
from syncoj_agent.client import NetworkError, RateLimited  # noqa: E402
from syncoj_agent.config import AgentConfig  # noqa: E402
from syncoj_agent.diagnostics import (  # noqa: E402
    CONFIG_SUMMARY_KEYS,
    LOG_TAIL_MAX_BYTES,
    LOG_TAIL_MAX_LINES,
    MAX_BUNDLE_BYTES,
    build_bundle,
    config_summary,
    encode_bundle,
    read_log_tail,
)
from syncoj_agent.main import (  # noqa: E402
    DIAGNOSTICS_MIN_INTERVAL_SECONDS,
    DIAGNOSTICS_PERIOD_SECONDS,
    Agent,
)
from syncoj_agent.state import STATE_READY, STATE_UNCLAIMED  # noqa: E402

#: 这些值会以"真实参与构造"的方式放进环境里，然后在包里搜它们 —— 搜到就是泄漏
SECRET_KEY_CONTENT = "SECRET-BOOTSTRAP-KEY-abcdef0123456789"
SECRET_TOKEN = "tok-1f4a9c2b7e5d8a3f-不要在诊断包里出现"
CREDENTIAL_BODY = '{"token": "%s", "player_no": "S001"}' % SECRET_TOKEN

#: 节奏是**契约**（任务书里写死的）：健康时 10 分钟、任何两次之间至少 60 秒。
#:
#: 时间推进刻意用这两个数而不是 `main.DIAGNOSTICS_*` —— 后者被改小之后，
#: 测试里的"推进 599 秒"会跟着变小，于是变异被自己掩盖过去（踩过一次：
#: 把周期改成 0.0 时测试反而"更绿"了）。所以：**断言常量等于契约值**，
#: 再用契约值做算术。
HEALTHY_PERIOD_SECONDS = 600.0
MIN_INTERVAL_SECONDS = 60.0


@pytest.fixture()
def config(workdir: Path) -> AgentConfig:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code", workdir / "code2"]
    config.bootstrap_key_file = workdir / "etc" / "bootstrap.key"
    config.run_user = "noi"
    config.server_url = "https://10.0.0.1:8443"
    for path in (config.state_dir, config.deploy_root, workdir / "etc"):
        path.mkdir(parents=True, exist_ok=True)
    # **真的**把密钥内容写进去：包里的值必须是路径，不是内容
    config.bootstrap_key_file.write_text(SECRET_KEY_CONTENT + "\n", encoding="utf-8")
    (config.state_dir / "credential.json").write_text(CREDENTIAL_BODY, encoding="utf-8")
    return config


def make_agent(config: AgentConfig, client=None) -> Agent:
    agent = Agent(config)
    if client is not None:
        agent.client.close()
        agent.client = client  # type: ignore[assignment]
    return agent


class RecordingClient:
    """只记录诊断上传与 tick，不联网。"""

    def __init__(self) -> None:
        self.uploads: List[bytes] = []
        self.tick_calls = 0
        self.upload_error: Optional[BaseException] = None
        self.tick_response = {"next_tick_seconds": 30, "claimed": True, "bound": True}

    def tick(self, payload):
        self.tick_calls += 1
        return dict(self.tick_response)

    def upload_diagnostics(self, blob, timeout=None):
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append(blob)
        return {}

    def set_token(self, token):
        pass

    def close(self):
        pass


def sample_bundle(config: AgentConfig, reason: str = "periodic"):
    return build_bundle(
        version="0.1.0",
        reason=reason,
        state=STATE_READY,
        machine_id="mid-1",
        machine_uuid="uuid-1",
        hostname="host-1",
        run_user="noi",
        os_info="NOI Linux 2.0",
        tick={
            "count": 12,
            "last_error": None,
            "consecutive_failures": 0,
            "next_tick_seconds": 30,
        },
        policy={"scan_interval": 30, "max_file_size": 2097152, "max_files": 5000},
        config=config,
        log_path=config.resolved_log_file,
        generated_at=1767225600,
    )


# --------------------------------------------------------------------------- #
# 一、形状（字段名是冻结的）
# --------------------------------------------------------------------------- #


def test_包的字段名与冻结的形状一致(config: AgentConfig) -> None:
    bundle = sample_bundle(config)

    assert set(bundle) == {
        "version",
        "generated_at",
        "reason",
        "state",
        "machine_id",
        "machine_uuid",
        "hostname",
        "run_user",
        "os_info",
        "tick",
        "process",
        "disk",
        "policy",
        "config_summary",
        "log_tail",
    }
    assert set(bundle["tick"]) == {
        "count",
        "last_error",
        "consecutive_failures",
        "next_tick_seconds",
    }
    assert set(bundle["process"]) == {"pid", "uptime_seconds", "rss_bytes", "threads"}
    assert set(bundle["disk"]) == {"free_bytes", "total_bytes"}
    assert set(bundle["policy"]) == {"scan_interval", "max_file_size", "max_files"}
    assert bundle["reason"] == "periodic"
    assert bundle["generated_at"] == 1767225600
    assert bundle["tick"]["next_tick_seconds"] == 30
    # 压缩后必须是能解回来的 gzip JSON（服务端按这个收）
    blob, dropped = encode_bundle(bundle)
    assert dropped is False
    assert len(blob) <= MAX_BUNDLE_BYTES
    assert json.loads(gzip.decompress(blob).decode("utf-8"))["machine_id"] == "mid-1"


def test_配置摘要是白名单构造的(config: AgentConfig) -> None:
    summary = config_summary(config)

    assert set(summary) == set(CONFIG_SUMMARY_KEYS)
    # **不是**"整份配置再删几个键"：任何没在白名单里的键都不许出现
    assert "agent.bootstrap_key_file" in summary
    assert summary["agent.bootstrap_key_file"] == str(config.bootstrap_key_file)
    assert summary["agent.run_user"] == "noi"
    assert "scan.roots" in summary and "code" in summary["scan.roots"]


# --------------------------------------------------------------------------- #
# 二、绝不带密钥 / token / 凭据内容
# --------------------------------------------------------------------------- #


def test_包里搜不到密钥内容_token_与凭据内容(config: AgentConfig) -> None:
    """把**真实的值**放进去构造，再在序列化结果里搜它们。

    搜的是**解压后**的字节（用 gzip 掩盖不住的形态），同时也搜一遍压缩后的
    字节 —— 两条都过才算过。
    """
    bundle = sample_bundle(config)
    blob, _dropped = encode_bundle(bundle)
    raw = gzip.decompress(blob).decode("utf-8")

    for secret, label in (
        (SECRET_KEY_CONTENT, "bootstrap.key 内容"),
        (SECRET_TOKEN, "token"),
        (CREDENTIAL_BODY, "credential.json 内容"),
    ):
        assert secret not in raw, "%s 进了诊断包" % label
        assert secret.encode("utf-8") not in blob, "%s 进了压缩后的字节" % label

    # 但**路径**要在（那是排查需要的，且不是秘密）
    parsed = json.loads(raw)
    assert parsed["config_summary"]["agent.bootstrap_key_file"] == str(
        config.bootstrap_key_file
    )
    # 也别把整个 agent.ini 塞进去：摘要里只有白名单那几个键
    # （`machine_id` 是**顶层**字段，本来就要报，不算"摘要泄漏"）
    for forbidden in ("verify_tls", "ca_file", "pairing_file_name"):
        assert forbidden not in raw, "%s 不该出现在诊断包里" % forbidden


def test_白名单外的配置键一个都不进包(config: AgentConfig, workdir: Path) -> None:
    """给配置加一个"看起来像秘密"的新键 —— 白名单机制下它默认**不出门**。"""
    config.pairing_file_name = "秘密配对码.txt"  # 不在白名单里

    raw = gzip.decompress(encode_bundle(sample_bundle(config))[0]).decode("utf-8")

    assert "秘密配对码" not in raw


# --------------------------------------------------------------------------- #
# 三、日志尾部：最后 200 行与最后 64 KB 里更小的那个
# --------------------------------------------------------------------------- #


def test_日志尾部只取最后两百行(workdir: Path) -> None:
    path = workdir / "agent.log"
    path.write_text(
        "".join("第 %04d 行\n" % index for index in range(300)), encoding="utf-8"
    )

    tail = read_log_tail(path)

    lines = tail.splitlines()
    assert len(lines) == LOG_TAIL_MAX_LINES == 200
    assert lines[0] == "第 0100 行"
    assert lines[-1] == "第 0299 行"


def test_日志尾部只取最后六十四_K(workdir: Path) -> None:
    path = workdir / "agent.log"
    # 每行 100 字节 × 2000 行 = 200 KB，远超 64 KB
    path.write_text(("x" * 99 + "\n") * 2000, encoding="utf-8")

    tail = read_log_tail(path)

    assert len(tail.encode("utf-8")) <= LOG_TAIL_MAX_BYTES, "没有按字节数截断"
    assert len(tail.splitlines()) <= LOG_TAIL_MAX_LINES
    # 必须是**结尾**那段（最后一行完整、且不是从中间切出来的半截行）
    assert tail.endswith("x" * 99)


def test_日志文件不存在时返回空串(workdir: Path) -> None:
    assert read_log_tail(workdir / "没有这个文件.log") == ""


def test_压缩后超限时丢掉日志尾部(config: AgentConfig) -> None:
    bundle = sample_bundle(config)
    # 用**高熵**内容：一串 "y" 会被 gzip 压到几百字节，测不出"超限"这条路
    bundle["log_tail"] = os.urandom(400 * 1024).hex()

    blob, dropped = encode_bundle(bundle)

    assert dropped is True
    assert len(blob) <= MAX_BUNDLE_BYTES
    assert json.loads(gzip.decompress(blob).decode("utf-8"))["log_tail"] == ""


def test_真到极限也要么截断要么拒发(config: AgentConfig) -> None:
    """连丢掉日志尾部都超限时**抛 ValueError**，绝不发一个服务端会拒收的超大包。"""
    bundle = sample_bundle(config)
    bundle["log_tail"] = os.urandom(400 * 1024).hex()

    with pytest.raises(ValueError):
        encode_bundle(bundle, max_bytes=64)


# --------------------------------------------------------------------------- #
# 四、触发节奏（时钟可注入，别真等 10 分钟）
# --------------------------------------------------------------------------- #


def test_健康时每十分钟一份(config: AgentConfig, monkeypatch) -> None:
    assert DIAGNOSTICS_PERIOD_SECONDS == HEALTHY_PERIOD_SECONDS, "周期契约被改了？"
    client = RecordingClient()
    agent = make_agent(config, client)
    now = [1000.0]
    agent._monotonic = lambda: now[0]  # type: ignore[assignment]

    agent._maybe_send_diagnostics()  # 启动后的第一份（基线）
    assert len(client.uploads) == 1, "启动后应当先留一份基线"

    now[0] += HEALTHY_PERIOD_SECONDS - 1
    agent._maybe_send_diagnostics()
    assert len(client.uploads) == 1, "没到 10 分钟不该再传"

    now[0] += 1
    agent._maybe_send_diagnostics()
    assert len(client.uploads) == 2, "到点应当再来一份"

    reasons = [
        json.loads(gzip.decompress(blob).decode("utf-8"))["reason"]
        for blob in client.uploads
    ]
    assert reasons == ["periodic", "periodic"]


def test_出错立即补一份但守住六十秒最小间隔(config: AgentConfig) -> None:
    assert DIAGNOSTICS_MIN_INTERVAL_SECONDS == MIN_INTERVAL_SECONDS, "最小间隔契约被改了？"
    client = RecordingClient()
    agent = make_agent(config, client)
    now = [1000.0]
    agent._monotonic = lambda: now[0]  # type: ignore[assignment]

    agent._maybe_send_diagnostics()  # 基线（periodic）
    assert len(client.uploads) == 1

    now[0] += 5
    agent.request_diagnostics("error")
    agent._maybe_send_diagnostics()
    assert len(client.uploads) == 1, "5 秒就补包会刷屏（服务端也只允 60 秒一份）"
    assert agent._diag_pending == "error", "被最小间隔挡住时必须**留着**这个请求"

    now[0] += MIN_INTERVAL_SECONDS - 5
    agent._maybe_send_diagnostics()
    assert len(client.uploads) == 2
    assert json.loads(gzip.decompress(client.uploads[-1]).decode())[
        "reason"
    ] == "error"
    assert agent._diag_pending is None, "发出去之后请求要清掉"


def test_服务端在_tick_里要求时下一轮就传_manual(config: AgentConfig) -> None:
    """走**真的** cycle()：tick 响应里带 ``diagnostics_request=true`` 就该记下来。"""
    client = RecordingClient()
    agent = make_agent(config, client)
    now = [5000.0]
    agent._monotonic = lambda: now[0]  # type: ignore[assignment]
    agent._maybe_send_diagnostics()  # 基线
    assert len(client.uploads) == 1

    credential = SimpleNamespace(
        state=STATE_READY, player_no="S001", contest_slug="mock-1"
    )
    agent._ensure_credential = lambda: credential  # type: ignore[assignment]
    agent._sync_notice_files = lambda cred, reason="": None  # type: ignore[assignment]
    agent._ensure_roots = lambda *a, **k: None  # type: ignore[assignment]
    agent._scan = lambda: ([], {}, 0)  # type: ignore[assignment]
    agent._build_tick_payload = lambda *a, **k: ({}, [], [])  # type: ignore[assignment]
    client.tick_response = {
        "claimed": True,
        "bound": True,
        "next_tick_seconds": 30,
        "diagnostics_request": True,
    }

    agent.cycle()

    assert agent._diag_pending == "manual", "服务端要一份的时候没有记下来"
    now[0] += MIN_INTERVAL_SECONDS
    agent._maybe_send_diagnostics()

    reasons = [
        json.loads(gzip.decompress(blob).decode("utf-8"))["reason"]
        for blob in client.uploads
    ]
    assert reasons == ["periodic", "manual"]


def test_待配对路径也认服务端的诊断请求(config: AgentConfig) -> None:
    """待配对 / 没有场次的机器也要被服务端点得动。

    这条心跳走的是 ``_pending_tick``，而正常 tick 里读 ``diagnostics_request``
    的那一段在**扫描之后** —— 待配对路径提前返回，永远走不到那里。
    """
    client = RecordingClient()
    agent = make_agent(config, client)
    agent._build_tick_payload = lambda *a, **k: ({}, [], [])  # type: ignore[assignment]
    agent._flush_events = lambda: None  # type: ignore[assignment]
    agent._adopt_identity = lambda data: SimpleNamespace(state=STATE_UNCLAIMED)  # type: ignore[assignment]
    client.tick_response = {
        "claimed": False,
        "bound": False,
        "next_tick_seconds": 30,
        "diagnostics_request": True,
    }

    state, wait = agent._pending_tick()

    assert state == STATE_UNCLAIMED
    assert wait == 30.0
    assert agent._diag_pending == "manual", "待配对那条路径没有认 diagnostics_request"


def test_待配对路径也会把诊断包发出去(config: AgentConfig) -> None:
    """``cycle()`` 在还不能干活时**提前返回** —— 那条路径从前不会走到发送那一步。

    后果是：一台卡在配对阶段的机器，服务端点「要一份」永远没反应，周期性的那份
    也从来没发过 —— 而"卡在配对"正是最需要现场的一类故障。
    """
    client = RecordingClient()
    agent = make_agent(config, client)
    now = [9000.0]
    agent._monotonic = lambda: now[0]  # type: ignore[assignment]
    agent._maybe_send_diagnostics()  # 基线
    assert len(client.uploads) == 1

    agent._ensure_credential = lambda: SimpleNamespace(state=STATE_UNCLAIMED)  # type: ignore[assignment]
    agent._pending_tick = lambda: (STATE_UNCLAIMED, 12.0)  # type: ignore[assignment]
    now[0] += HEALTHY_PERIOD_SECONDS

    assert agent.cycle() == 12.0
    assert len(client.uploads) == 2, "待配对那条路径没有把该传的诊断包传出去"
    assert json.loads(gzip.decompress(client.uploads[-1]).decode())[
        "state"
    ] == STATE_UNCLAIMED


def test_manual_优先于_error(config: AgentConfig) -> None:
    client = RecordingClient()
    agent = make_agent(config, client)
    agent._monotonic = lambda: 10000.0  # type: ignore[assignment]

    agent.request_diagnostics("error")
    agent.request_diagnostics("manual")
    agent._maybe_send_diagnostics()

    assert json.loads(gzip.decompress(client.uploads[-1]).decode())[
        "reason"
    ] == "manual"


# --------------------------------------------------------------------------- #
# 五、失败处理：429 不算错、失败不影响心跳
# --------------------------------------------------------------------------- #


def test_被限速时当作这次没传成(config: AgentConfig, capture_logs) -> None:
    records = capture_logs("syncoj.agent")
    client = RecordingClient()
    client.upload_error = RateLimited(
        "限速", status=429, code="rate_limited", retry_after=30
    )
    agent = make_agent(config, client)
    now = [2000.0]
    agent._monotonic = lambda: now[0]  # type: ignore[assignment]

    agent._maybe_send_diagnostics()  # 不抛
    assert agent._diag_failures == 0, "限速不是我们的失败"
    assert agent._diag_pending is None

    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings == [], [r.getMessage() for r in warnings]


def test_上传失败只记_debug_连续多次才_warning(config: AgentConfig, capture_logs) -> None:
    records = capture_logs("syncoj.agent")
    client = RecordingClient()
    client.upload_error = NetworkError("服务端不理我")
    agent = make_agent(config, client)
    now = [3000.0]
    agent._monotonic = lambda: now[0]  # type: ignore[assignment]

    for index in range(3):
        now[0] = 3000.0 + index * (HEALTHY_PERIOD_SECONDS + 1)
        agent._maybe_send_diagnostics()

    warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "连续 3 次" in warnings[0]
    assert agent._diag_failures == 3


def test_上传失败不影响心跳(monkeypatch, config: AgentConfig, capture_logs) -> None:
    """上传必炸，但 tick 照样跑满两轮 —— 而且不许出现 WARNING 级噪音。"""
    records = capture_logs("syncoj.agent")
    client = RecordingClient()
    client.upload_error = NetworkError("上传必炸")
    agent = make_agent(config, client)
    rounds: List[int] = []
    delays: List[float] = []

    def cycle() -> float:
        client.tick({})
        rounds.append(len(rounds) + 1)
        return 7.0

    monkeypatch.setattr(agent, "cycle", cycle)

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 2:
            agent.request_stop()

    agent._sleep = fake_sleep  # type: ignore[assignment]

    assert agent.run_forever() == 0
    assert rounds == [1, 2], "上传失败把心跳带崩了：%r" % rounds
    assert client.tick_calls == 2
    assert delays == [7.0, 7.0]
    warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert warnings == [], warnings
