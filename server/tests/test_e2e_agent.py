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

from conftest import bind_by_code, issue_bootstrap_key, make_roster

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def age_agent(app, machine_uuid: str, seconds: int = 7200) -> None:
    """把这台机器的"最后心跳"推到过去 = 模拟它关机了。

    快照还原的真实时序是"关机 → 还原 → 再开机"，期间原机器一定超过了离线
    判定阈值。少了这一步，服务端会（正确地）把重新注册的机器当成克隆镜像。
    """
    from datetime import timedelta

    from syncoj_server.models import Agent, utcnow

    with app.state.ctx.db.session() as session:
        record = session.execute(
            select(Agent).where(Agent.machine_uuid == machine_uuid)
        ).scalar_one()
        record.last_seen_at = utcnow() - timedelta(seconds=seconds)


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
           player: dict, workdir: Path) -> dict:
    """建好场次/选手/名单条目，并把统一密钥写成一个**真文件**。

    密钥写成文件而不是塞进某个测试属性，是因为 Agent 读的就是文件 ——
    那条"root 只读、装机单元一次性读走"的路径要端到端成立，中间不能有捷径。
    """
    roster = make_roster(
        client, admin_headers, "端到端班", entries=[{"player_no": player["player_no"]}]
    )
    raw = issue_bootstrap_key(client, admin_headers, label="端到端")
    key_file = workdir / "bootstrap.key"
    key_file.write_text(raw + "\n", encoding="utf-8")
    return {
        "contest": contest,
        "player": player,
        "entry": roster["entries"][0],
        "key_file": key_file,
    }


def build_agent_config(
    workdir: Path,
    base_url: str,
    seeded: dict,
    code_dir: Path,
    scan_root=None,
    prefix: str = "none",
    machine_id: str = "machine-e2e",
):
    from syncoj_agent.config import AgentConfig

    state_dir = workdir / "state"
    deploy_root = workdir / "deploy"
    state_dir.mkdir(parents=True, exist_ok=True)
    deploy_root.mkdir(parents=True, exist_ok=True)

    config = AgentConfig()
    config.server_url = base_url
    config.verify_tls = False
    config.ca_file = None
    # 注册只有一条路：镜像里那份 root 只读的统一密钥
    if seeded.get("key_file") is not None:
        config.bootstrap_key_file = seeded["key_file"]
    # 固定 machine_id 让测试能在服务端台账里认出这台机器（真机上由 Agent 生成并持久化）
    config.machine_id = machine_id
    config.state_dir = state_dir
    config.deploy_root = deploy_root
    config.scan_roots = [scan_root if scan_root is not None else code_dir]
    config.scan_prefix = prefix
    config.scan_interval = 5
    config.log_level = "DEBUG"
    config.log_to_stderr = False
    config.request_timeout = 10
    config.download_timeout = 30
    return config


def start_agent(
    workdir: Path,
    base_url: str,
    seeded: dict,
    client: TestClient,
    admin_headers: dict,
    code_dir: Path,
    **kwargs,
):
    """建一个**已经配对完成**的 Agent，返回 ``(config, agent)``。

    配对必须分两步走，因为配对码是机器自己注册时才生成的：

    1. 先让它跑一轮 —— 这一轮它会用统一密钥注册、拿到配对码，然后收到 403
       并安静地把码存下来（这正是它该做的：不要去重新注册，那会把教师手上
       刚读到的码换掉）
    2. 教师在管理界面上读码、配对
    3. 后面每一轮 cycle 就都能干活了

    这正好是现场的真实顺序，所以它不是"测试脚手架的绕路"，而是一段真实的
    多角色时序。
    """
    from syncoj_agent.main import Agent
    from syncoj_agent.state import STATE_READY, load_credential

    config = build_agent_config(workdir, base_url, seeded, code_dir, **kwargs)
    agent = Agent(config)
    agent.cycle()

    credential = load_credential(config.credential_path)
    assert credential is not None, "第一轮就该拿到凭据"
    assert credential.state != STATE_READY, "还没配对，不该是 READY"
    assert credential.pair_code, "未配对时服务端必须下发配对码"

    bind_by_code(client, admin_headers, credential.pair_code, seeded["entry"]["id"])
    return config, agent


def test_agent_cycle_uploads_source_code(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """核心链路：Agent 扫描 → 注册 → tick → 上传 → 落到 source/。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    (code_dir / "sub").mkdir()
    (code_dir / "sub" / "util.h").write_text("#pragma once\n", encoding="utf-8")
    (code_dir / "notes.txt").write_text("不该被回收\n", encoding="utf-8")

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
    try:
        agent.cycle()
    finally:
        agent.client.close()

    assert (config.state_dir / "credential.json").is_file(), "注册后应保存凭据"

    # 默认 scan.prefix = none（不加前缀）：本机只有一个选手时，
    # 加前缀会让 source/ 里准考证号出现两次
    landed = settings.source_root / seeded["contest"]["slug"] / seeded["player"]["player_no"]
    assert (landed / "main.cpp").read_text(encoding="utf-8") == "int main(){return 0;}\n"
    assert (landed / "sub" / "util.h").is_file()
    assert not (landed / "notes.txt").exists(), "非白名单后缀不该被回收"


def test_agent_reuses_credential_without_re_enrolling(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """第二轮不该重新注册 —— 每次 tick 都换 token 会让服务端凭据表疯狂膨胀。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "a.cpp").write_text("a", encoding="utf-8")
    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
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
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """稳态下不该反复重传未变化的文件 —— 否则每轮都在传整个代码目录。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "stable.cpp").write_text("stable", encoding="utf-8")
    (code_dir / "changing.cpp").write_text("v1", encoding="utf-8")

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
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

    assert uploaded == ["changing.cpp"], (
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

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
    try:
        agent.cycle()
        target.unlink()
        agent.cycle()
    finally:
        agent.client.close()

    events = client.get(
        "/api/v1/admin/contests/%d/events" % seeded["contest"]["id"], headers=admin_headers
    ).json()["items"]
    assert "file_deleted" in [e["category"] for e in events]


def test_agent_downloads_deployed_asset(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """下发链路：教师上传 → 建任务 → Agent 领取 → 下载落盘 → 回报完成。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()

    data = b"TESTDATA-CONTENT\n" * 3000  # ~50KB
    asset = client.post(
        "/api/v1/admin/contests/%d/assets" % seeded["contest"]["id"],
        files={"file": ("testdata.zip", data, "application/octet-stream")},
        headers=admin_headers,
    ).json()
    task = client.post(
        "/api/v1/admin/contests/%d/deploys" % seeded["contest"]["id"],
        json={
            "asset_id": asset["id"],
            "target_kind": "all",
            "dest_dir": "exam",
            "mode": "overwrite",
        },
        headers=admin_headers,
    ).json()

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
    try:
        # 第一轮：tick 拿到 deploy_jobs 并完成下载
        agent.cycle()
        landed = config.deploy_root / "exam" / "testdata.zip"
        assert landed.is_file(), "下发文件应当落到 deploy_root/exam/ 下"
        assert landed.read_bytes() == data, "落盘内容必须与上传内容逐字节一致"

        # 第二轮：回报 completed_assets
        agent.cycle()
    finally:
        agent.client.close()

    detail = client.get(
        "/api/v1/admin/deploys/%d" % task["id"], headers=admin_headers
    ).json()
    assert detail["done"] == 1, "Agent 回报后目标应标为完成：%r" % detail
    assert detail["status"] == "done"
    assert detail["targets"][0]["status"] == "done"


def test_agent_resumes_interrupted_download(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """断点续传的真实路径：磁盘上先放半个分片，Agent 应当只下剩余部分。

    这里验证的是"分片在 de活目录里被发现 → offset 上报 → 服务端回填"这条
    完整链路，而不是 download_asset 的内部逻辑（那个已有单元测试覆盖）。
    """
    from syncoj_agent.download import part_path_for
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()

    data = bytes(range(256)) * 400  # 102400 字节
    asset = client.post(
        "/api/v1/admin/contests/%d/assets" % seeded["contest"]["id"],
        files={"file": ("big.bin", data, "application/octet-stream")},
        headers=admin_headers,
    ).json()
    client.post(
        "/api/v1/admin/contests/%d/deploys" % seeded["contest"]["id"],
        json={"asset_id": asset["id"], "target_kind": "all", "dest_dir": "exam"},
        headers=admin_headers,
    )

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
    dest = config.deploy_root / "exam" / "big.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)

    already = 40000
    part_path_for(dest, asset["id"]).write_bytes(data[:already])

    try:
        agent.cycle()
    finally:
        agent.client.close()

    assert dest.read_bytes() == data, "续传结果必须与完整内容一致"
    assert not part_path_for(dest, asset["id"]).exists(), "完成后不该留下分片"


def test_agent_skips_already_deployed_file(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """内容已一致时不重复下载 —— 否则每次下发都要把 500MB 测试点重传一遍。"""
    from syncoj_agent.main import Agent

    code_dir = workdir / "code"
    code_dir.mkdir()

    data = b"unchanged payload" * 100
    asset = client.post(
        "/api/v1/admin/contests/%d/assets" % seeded["contest"]["id"],
        files={"file": ("same.zip", data, "application/octet-stream")},
        headers=admin_headers,
    ).json()
    client.post(
        "/api/v1/admin/contests/%d/deploys" % seeded["contest"]["id"],
        json={"asset_id": asset["id"], "target_kind": "all", "dest_dir": "exam"},
        headers=admin_headers,
    )

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
    # 文件已经在了，且内容正确
    target = config.deploy_root / "exam" / "same.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    mtime_before = target.stat().st_mtime_ns

    try:
        agent.cycle()
    finally:
        agent.client.close()

    assert target.read_bytes() == data
    assert target.stat().st_mtime_ns == mtime_before, "内容一致时不该重写文件"


def test_agent_self_heals_after_credential_loss(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict, app, monkeypatch,
) -> None:
    """NOI Linux 快照还原的核心场景：凭据没了、UUID 也没了，Agent 自动重生。

    还原之后机器能自己回到原来的身份，靠的是**硬件指纹**（快照抹不掉它）
    加上"原机器那时是关机的"这个前提。所以测试必须把时序摆成还原的样子：
    关机 → 清空状态目录 → 再开机。少了"关机"这一步，服务端会（正确地）
    把它当成克隆镜像，这条测试就变成在测另一件事了。

    指纹在真实硬件上来自 SMBIOS，开发机上读不到，所以这里 monkeypatch 掉
    ``resolve_machine_fingerprint``。**不**给 Agent 开测试专用参数：
    那会让"指纹到底怎么来的"在测试里失真，而这条路径的全部意义
    恰恰在于"指纹是硬件给的、还原后不变"。
    """
    import syncoj_agent.main as agent_main
    from syncoj_agent.main import Agent

    fingerprint = "4c4c4544-0031-3010-8043-b7c04f4d4432"
    monkeypatch.setattr(agent_main, "resolve_machine_fingerprint", lambda: fingerprint)

    code_dir = workdir / "code"
    code_dir.mkdir()
    (code_dir / "main.cpp").write_text("int main(){}", encoding="utf-8")

    config, first = start_agent(
        workdir, live_server, seeded, client, admin_headers, code_dir
    )
    try:
        first.cycle()
        machine_id = first.machine_id
        agent_id = first._credential.agent_id
        machine_uuid = first.machine_uuid
        assert agent_id, "配对之后应当拿回 agent_id"
    finally:
        first.client.close()

    # 关机（让心跳过期）→ 整机还原（凭据、UUID、哈希缓存全没了）→ 再开机
    age_agent(app, machine_uuid)
    shutil.rmtree(config.state_dir, ignore_errors=True)

    second = Agent(config)
    try:
        second.cycle()
        assert second._credential is not None, "应当自动重新注册"
        assert second.machine_id == machine_id, (
            "machine_id 必须稳定，否则服务端会把同一台机器当成新机器"
        )
        assert second._credential.agent_id == agent_id, "不该产生新的 Agent 记录"
        assert second._credential.player_no == "S001", "配对关系必须保留"
    finally:
        second.client.close()

    with Database(settings).session() as session:
        agents = list(session.execute(select(AgentRecord)).scalars())
    assert len(agents) == 1, "重复注册不该产生第二台机器记录：%r" % agents


# --------------------------------------------------------------------------- #
# 桌面路径约定
# --------------------------------------------------------------------------- #
#
# 约定：代码在 桌面/<准考证号>/<题目名>/<题目名>.cpp
#
# 这条链路上有三个必须对齐的地方，任何一处不对都会在开考时才暴露：
#   1. Agent 的扫描根是 桌面/<准考证号>（准考证号要等注册后才知道）
#   2. Agent 的上报前缀为空（否则 source/ 里准考证号会出现两次）
#   3. 服务端下发时把 {player_no} 逐选手展开成各自的准考证号


def test_desktop_layout_end_to_end(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """完整走一遍约定：登记题目 → 下发到 桌面/<准考证号>/<题目名>/ →
    选手在约定位置写代码 → 回收落到 source/<场次>/<编号>/<题目名>/<题目名>.cpp。"""
    from syncoj_agent.main import Agent

    contest_id = seeded["contest"]["id"]
    player_no = seeded["player"]["player_no"]

    # 1. 登记题目
    client.post(
        "/api/v1/admin/contests/%d/problems" % contest_id,
        json=[{"ident": "p1", "title": "签到题", "order_index": 10}],
        headers=admin_headers,
    )

    # 2. 下发题面到 {player_no}/p1 —— 即 桌面/<准考证号>/p1/
    statement = b"# A+B Problem\n"
    asset = client.post(
        "/api/v1/admin/contests/%d/assets" % contest_id,
        files={"file": ("题面.md", statement, "text/markdown")},
        headers=admin_headers,
    ).json()
    task = client.post(
        "/api/v1/admin/contests/%d/deploys" % contest_id,
        json={"asset_id": asset["id"], "target_kind": "all", "dest_dir": "{player_no}/p1"},
        headers=admin_headers,
    ).json()
    assert task["dest_dir"] == "{player_no}/p1", "入库的应当是模板本身"

    # 3. Agent 的扫描根是 桌面/<准考证号>，用 {player_no} 占位符表示
    desktop = workdir / "桌面"
    desktop.mkdir()
    config, agent = start_agent(
        workdir,
        live_server,
        seeded,
        client,
        admin_headers,
        desktop,  # 占位，会被 scan_root 覆盖
        scan_root=desktop / "{player_no}",
        prefix="none",
    )
    # 下发根目录 = 桌面（与 agent.ini 的默认值一致）
    config.deploy_root = desktop

    try:
        # 第一轮：注册 + 领下发任务 + 下载题面
        agent.cycle()
    finally:
        agent.client.close()

    landed_dir = desktop / player_no / "p1"
    assert landed_dir.is_dir(), "应当按 {player_no}/p1 展开后落到桌面下"
    assert (landed_dir / "题面.md").read_bytes() == statement
    assert task["id"]

    # 4. 选手在约定位置写代码
    (landed_dir / "p1.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

    # 5. 第二轮：扫描并回收
    agent2 = Agent(config)
    try:
        agent2.cycle()
    finally:
        agent2.client.close()

    expected = (
        settings.source_root / seeded["contest"]["slug"] / player_no / "p1" / "p1.cpp"
    )
    assert expected.is_file(), (
        "代码应当落到 source/<场次>/<准考证号>/<题目名>/<题目名>.cpp\n"
        "实际 source/ 内容：%s"
        % sorted(p.relative_to(settings.source_root).as_posix() for p in settings.source_root.rglob("*"))
    )
    assert expected.read_text(encoding="utf-8") == "int main(){return 0;}\n"


def test_bootstrap_enrollment_end_to_end(
    workdir: Path, settings: Settings, live_server: str, client: TestClient,
    admin_headers: dict, contest: dict,
) -> None:
    """统一密钥这条路的完整链路，跑**真的 Agent 代码**打到**真的服务端**。

    这个流程涉及四个角色的时序（root 装机 → 机器注册 → 教师配对 → 机器开始收代码），
    单元测试各自覆盖得再好，也覆盖不到它们**接起来**的样子 ——
    而"接不起来"恰恰是这类多角色流程最常见的失败方式。
    """
    from syncoj_agent.main import Agent
    from syncoj_agent.state import load_credential

    contest_id = contest["id"]

    # 1. 教师建名单、登选手、签发密钥（明文只出现这一次）
    player = client.post(
        "/api/v1/admin/contests/%d/players" % contest_id,
        json=[{"player_no": "S001", "name": "张三"}],
        headers=admin_headers,
    ).json()["players"][0]
    roster = make_roster(
        client, admin_headers, "统一密钥班", entries=[{"player_no": "S001", "name": "张三"}]
    )
    key = issue_bootstrap_key(client, admin_headers, label="机房镜像")

    desktop = workdir / "桌面"
    desktop.mkdir()
    code_dir = desktop / "S001"
    code_dir.mkdir()

    key_file = workdir / "etc" / "bootstrap.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key + "\n", encoding="utf-8")

    seeded = {"contest": contest, "player": player, "entry": roster["entries"][0]}
    config = build_agent_config(
        workdir, live_server, seeded, code_dir, machine_id="machine-boot"
    )
    config.bootstrap_key_file = key_file
    config.deploy_root = desktop

    # 2. 机器首次开机：注册，拿到一个"没有归属"的凭据 + 配对码
    agent = Agent(config)
    try:
        delay = agent.cycle()
    finally:
        agent.client.close()

    credential = load_credential(config.credential_path)
    assert credential is not None
    assert credential.claimed is False, "统一密钥注册出来的机器不该直接有归属"
    assert credential.pair_code

    # 配对码要落到桌面上 —— 这是教师唯一能看见它的地方
    pair_file = desktop / config.pairing_file_name
    assert pair_file.is_file(), "配对码没写到桌面上，教师无从读起"
    assert credential.pair_code in pair_file.read_text(encoding="utf-8")

    # 未配对时它应当只心跳、不扫描（扫出来的路径没法归属到任何人）
    assert delay > 0
    assert sorted(p.name for p in settings.source_root.rglob("*")) == []

    # 3. 教师读码、配对
    claim = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": credential.pair_code, "roster_entry_id": roster["entries"][0]["id"]},
        headers=admin_headers,
    )
    assert claim.status_code == 200, claim.text

    # 4. 机器下一轮就应当"活过来"：接住身份、开始扫描
    (code_dir / "p1.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    agent2 = Agent(config)
    try:
        agent2.cycle()
    finally:
        agent2.client.close()

    adopted = load_credential(config.credential_path)
    assert adopted is not None
    assert adopted.claimed is True
    assert adopted.player_no == "S001"
    assert adopted.pair_code == "", "配对之后不该再留着配对码"
    assert not pair_file.exists(), "配对之后桌面上的配对码文件应当消失"

    landed = settings.source_root / contest["slug"] / "S001" / "p1.cpp"
    assert landed.is_file(), (
        "配对之后代码应当开始回收\n实际 source/ 内容：%s"
        % sorted(p.relative_to(settings.source_root).as_posix() for p in settings.source_root.rglob("*"))
    )


def test_bootstrap_restore_keeps_pairing_end_to_end(
    workdir: Path, settings: Settings, live_server: str, client: TestClient,
    admin_headers: dict, contest: dict, app, monkeypatch,
) -> None:
    """快照还原：凭据没了、UUID 没了、指纹还在 → 认回原机器，**不用重新配对**。

    这条链路的价值全在"不用重新配对"上。如果每个快照还原都要教师配对一次，
    50 台机器就是 50 次人工，整套方案直接不可用 —— 所以它必须被端到端验证一次，
    而不是只测服务端的判断逻辑。

    指纹在真实硬件上来自 SMBIOS，开发机上读不到，所以这里 monkeypatch 掉
    ``resolve_machine_fingerprint``。**不**在 Agent 上开测试专用参数：
    那会让"指纹到底怎么来的"这件事在测试里失真，而这条路径的全部意义
    恰恰在于"指纹是硬件给的、还原后不变"。
    """
    from datetime import timedelta

    from sqlalchemy import select

    import syncoj_agent.main as agent_main
    from syncoj_agent.main import Agent
    from syncoj_agent.state import load_credential

    fingerprint = "4c4c4544-0031-3010-8043-b7c04f4d4432"
    monkeypatch.setattr(agent_main, "resolve_machine_fingerprint", lambda: fingerprint)

    contest_id = contest["id"]
    player = client.post(
        "/api/v1/admin/contests/%d/players" % contest_id,
        json=[{"player_no": "S001"}],
        headers=admin_headers,
    ).json()["players"][0]
    roster = make_roster(
        client, admin_headers, "还原班", entries=[{"player_no": "S001"}]
    )
    key = issue_bootstrap_key(client, admin_headers)

    desktop = workdir / "桌面"
    desktop.mkdir()
    code_dir = desktop / "S001"
    code_dir.mkdir()

    key_file = workdir / "etc" / "bootstrap.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key + "\n", encoding="utf-8")

    seeded = {"contest": contest, "player": player, "entry": roster["entries"][0]}
    config = build_agent_config(
        workdir, live_server, seeded, code_dir, machine_id="machine-restore"
    )
    config.bootstrap_key_file = key_file
    config.deploy_root = desktop

    agent = Agent(config)
    machine_uuid = None
    try:
        agent.cycle()
        machine_uuid = agent.machine_uuid
    finally:
        agent.client.close()

    credential = load_credential(config.credential_path)
    assert credential is not None and credential.pair_code
    claim = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": credential.pair_code, "roster_entry_id": roster["entries"][0]["id"]},
        headers=admin_headers,
    )
    assert claim.status_code == 200, claim.text

    # 机器关机一段时间（快照还原的真实时序：关机 → 还原 → 再开机）
    age_agent(app, machine_uuid)

    # 还原：凭据与 UUID 一起没了，指纹（与统一密钥）还在
    config.credential_path.unlink()
    (config.state_dir / "machine_uuid").unlink()

    restored = Agent(config)
    try:
        restored.cycle()
    finally:
        restored.client.close()

    adopted = load_credential(config.credential_path)
    assert adopted is not None
    assert adopted.claimed is True, "指纹认回之后不该回到待配对状态"
    assert adopted.player_no == "S001", "配对关系必须保留"

    # 服务端上也应当还是同一台机器，而不是多出来一台待配对的
    pending = client.get("/api/v1/admin/machines/pending", headers=admin_headers).json()
    assert pending["items"] == [], "还原不该产生新的待配对机器"
    assert pending["total"] == 0


def test_prefix_none_avoids_duplicated_player_no(
    workdir: Path, settings: Settings, live_server: str, seeded: dict,
    client: TestClient, admin_headers: dict,
) -> None:
    """前缀为空是默认值，理由很具体：扫描根的名字就是准考证号，
    再加前缀会让 source/ 里准考证号出现两次。"""
    from syncoj_agent.main import Agent

    player_no = seeded["player"]["player_no"]
    desktop = workdir / "桌面"
    desktop.mkdir()

    config, agent = start_agent(
        workdir, live_server, seeded, client, admin_headers, desktop,
        scan_root=desktop / "{player_no}", prefix="none",
    )
    try:
        agent._ensure_credential()
        agent._ensure_roots(player_no)
        assert agent._roots == [("", desktop / player_no)], agent._roots
        # 扫描根不存在时会被创建出来（选手可能还没建过目录）
        assert (desktop / player_no).is_dir()
    finally:
        agent.client.close()
