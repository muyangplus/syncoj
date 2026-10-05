"""端到端集成测试：真实 Agent 代码 → 真实 HTTP → 真实服务端。

前面所有测试都是"服务端自己测自己"和"Agent 自己测自己"。这个文件不一样：
它把 Agent 的 ``cycle()`` 跑起来，让它通过 ``http.client`` 打到 uvicorn 起着的
真实服务端，最后检查文件是不是真的落到了评测器会读的那个目录里。

只有它能证明的两件事：
1. 两端的 JSON 字段在**真实字节流**上对得上（契约测试只验样本，不验传输）
2. multipart 编码、Range 请求这些手写的 HTTP 细节真的能被对方正确解析
"""

from __future__ import annotations

import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.config import Settings
from syncoj_server.db import Database
from syncoj_server.main import create_app
from syncoj_server.models import Agent as AgentRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_server(settings: Settings) -> Iterator[str]:
    """在后台线程里跑一个真的 uvicorn，返回 base_url。"""
    app = create_app(settings)
    port = _free_port()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("uvicorn 未能在 15 秒内启动")

    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture()
def seeded(settings: Settings, client: TestClient, admin_headers: dict, contest: dict,
           player: dict) -> dict:
    """建好场次/选手，并签发注册码。"""
    code = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    ).json()["code"]
    return {"contest": contest, "player": player, "code": code}


def build_agent_config(workdir: Path, base_url: str, code: str, code_dir: Path):
    from syncoj_agent.config import AgentConfig

    state_dir = workdir / "state"
    deploy_root = workdir / "deploy"
    state_dir.mkdir(parents=True, exist_ok=True)
    deploy_root.mkdir(parents=True, exist_ok=True)

    config = AgentConfig()
    config.server_url = base_url
    config.verify_tls = False
    config.ca_file = None
    config.enroll_code = code
    config.state_dir = state_dir
    config.deploy_root = deploy_root
    config.scan_roots = [code_dir]
    config.scan_interval = 5
    config.log_level = "DEBUG"
    config.log_to_stderr = False
    config.request_timeout = 10
    config.download_timeout = 30
    return config


def test_agent_cycle_uploads_source_code(
    workdir: Path, settings: Settings, live_server: str, seeded: dict
) -> None:
    """核心链路：Agent 扫描 → 注册 → tick → 上传 → 落到 source/。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    (code_dir / "sub").mkdir()
    (code_dir / "sub" / "util.h").write_text("#pragma once\n", encoding="utf-8")
    (code_dir / "notes.txt").write_text("不该被回收\n", encoding="utf-8")

    config = build_agent_config(workdir, live_server, seeded["code"], code_dir)
    agent = Agent(config)
    try:
        agent.cycle()
    finally:
        agent.client.close()

    assert (config.state_dir / "credential.json").is_file(), "注册后应保存凭据"

    # 上报路径带扫描根目录名做前缀（多根目录下同名文件靠它区分），
    # 这个前缀会原样体现在 source/ 目录结构里
    landed = settings.source_root / seeded["contest"]["slug"] / seeded["player"]["player_no"]
    assert (landed / "code" / "main.cpp").read_text(encoding="utf-8") == "int main(){return 0;}\n"
    assert (landed / "code" / "sub" / "util.h").is_file()
    assert not (landed / "code" / "notes.txt").exists(), "非白名单后缀不该被回收"


def test_agent_reuses_credential_without_re_enrolling(
    workdir: Path, settings: Settings, live_server: str, seeded: dict
) -> None:
    """第二轮不该重新注册 —— 每次 tick 都换 token 会让服务端凭据表疯狂膨胀。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "a.cpp").write_text("a", encoding="utf-8")
    config = build_agent_config(workdir, live_server, seeded["code"], code_dir)

    agent = Agent(config)
    try:
        agent.cycle()
        agent_id_first = agent._credential.agent_id
        token_first = agent._credential.token

        # 第二轮：换一个全新的 Agent 实例，模拟进程重启
        (code_dir / "b.cpp").write_text("b", encoding="utf-8")
        second = Agent(config)
        try:
            second.cycle()
        finally:
            second.client.close()

        assert second._credential is not None
        assert second._credential.agent_id == agent_id_first
        assert second._credential.token == token_first, "有可用凭据时不该换发 token"
    finally:
        agent.client.close()


def test_incremental_upload_only_sends_changed_file(
    workdir: Path, settings: Settings, live_server: str, seeded: dict
) -> None:
    """稳态下不该反复重传未变化的文件 —— 否则每轮都在传整个代码目录。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "stable.cpp").write_text("stable", encoding="utf-8")
    (code_dir / "changing.cpp").write_text("v1", encoding="utf-8")

    config = build_agent_config(workdir, live_server, seeded["code"], code_dir)
    agent = Agent(config)
    try:
        agent.cycle()

        uploaded: list = []
        original = agent.client.upload_file

        def spy(rel_path, file_path, sha256):
            uploaded.append(rel_path)
            return original(rel_path, file_path, sha256)

        agent.client.upload_file = spy  # type: ignore[assignment]
        (code_dir / "changing.cpp").write_text("v2-content", encoding="utf-8")
        agent.cycle()
    finally:
        agent.client.close()

    assert uploaded == ["code/changing.cpp"], (
        "只有变化的文件该被上传，实际上传：%r" % uploaded
    )


def test_deleted_file_is_recorded(workdir: Path, settings: Settings, live_server: str,
                                  seeded: dict, client: TestClient, admin_headers: dict) -> None:
    """选手删掉代码后，服务端必须留下审计记录（"仅审计"档防作弊的全部依据）。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    target = code_dir / "temp.cpp"
    target.write_text("temp", encoding="utf-8")

    config = build_agent_config(workdir, live_server, seeded["code"], code_dir)
    agent = Agent(config)
    try:
        agent.cycle()
        target.unlink()
        agent.cycle()
    finally:
        agent.client.close()

    events = client.get(
        "/api/v1/admin/contests/%d/events" % seeded["contest"]["id"], headers=admin_headers
    ).json()
    assert "file_deleted" in [e["category"] for e in events]


def test_agent_self_heals_after_credential_loss(
    workdir: Path, settings: Settings, live_server: str, seeded: dict
) -> None:
    """NOI Linux 快照还原的核心场景：凭据文件消失，Agent 靠注册码自动重生。

    这是注册码被设计成"可重复使用"而非一次性的全部原因。若它是一次性的，
    每次还原后都要人工重发注册码，50 台机器就是 50 次人工干预。
    """
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "main.cpp").write_text("int main(){}", encoding="utf-8")

    config = build_agent_config(workdir, live_server, seeded["code"], code_dir)
    first = Agent(config)
    try:
        first.cycle()
        machine_id = first.machine_id
        agent_id = first._credential.agent_id
    finally:
        first.client.close()

    # 模拟整机快照还原：状态目录被清空（凭据、哈希缓存全没了）
    shutil.rmtree(config.state_dir, ignore_errors=True)

    second = Agent(config)
    try:
        second.cycle()
        assert second._credential is not None, "应当用注册码自动重新注册"
        assert second.machine_id == machine_id, (
            "machine_id 必须稳定，否则服务端会把同一台机器当成新机器"
        )
        assert second._credential.agent_id == agent_id, "不该产生新的 Agent 记录"
    finally:
        second.client.close()

    with Database(settings).session() as session:
        agents = list(session.execute(select(AgentRecord)).scalars())
    assert len(agents) == 1, "重复注册不该产生第二台机器记录：%r" % agents
