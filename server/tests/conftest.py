"""pytest 公共夹具。

刻意**不用 ``tempfile``**：在受限的执行环境里它对新建目录做 chmod 会失败。
改为在仓库内建临时目录，用完 ``ignore_errors=True`` 清理。
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from syncoj_server import keys
from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.models import Admin
from syncoj_server.security import hash_password

REPO_ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = REPO_ROOT / ".pytest-tmp"

ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session", autouse=True)
def _basetemp_must_not_be_our_workdir_root(pytestconfig) -> None:
    """``--basetemp`` 不能和这个文件里的 ``TMP_ROOT`` 是同一个目录（也不能互相包含）。

    踩过一次，而且现象极具误导性：basetemp 曾经就是 ``.pytest-tmp`` 本身，而
    ``workdir`` 直接把临时目录建在它里面 —— 于是 pytest 在**第一次**用到
    ``tmp_path`` 时会清空整个 basetemp，把本次会话里已经在用的临时目录一起删掉。
    那些目录里有日志文件被处理器占着（Windows 上删不掉），报出来是一串

        PermissionError: [WinError 32] 另一个程序正在使用此文件

    指向一个跟被测代码毫无关系的文件，而且只在某些测试**顺序**下出现。

    读的是命令行给的 ``--basetemp``，不是 ``tmp_path_factory.getbasetemp()``：
    后者会在夹具里**建目录**，而它的父目录不存在时抛的是
    ``FileNotFoundError`` —— 一个关于路径、与被测代码毫无关系的报错。
    """
    given = getattr(pytestconfig.option, "basetemp", None)
    if not given:
        return  # 没给 --basetemp 就落在系统临时目录里，不可能和我们撞
    base = Path(given).resolve()
    ours = TMP_ROOT.resolve()
    # 危险的是**basetemp 把 TMP_ROOT 包住**（或者就等于它）：pytest 清空 basetemp
    # 时会把 TMP_ROOT 连同里面正在用的临时目录一起删掉。
    # 反过来（basetemp 落在 TMP_ROOT 里面，比如 .pytest-tmp/basetemp/server）是安全的 ——
    # 清空只动它自己那一棵。这个方向我一开始写反了，代价是整个服务端套件
    # 596 条全部 setup 失败。
    dangerous = base == ours or base in ours.parents
    assert not dangerous, (
        "--basetemp（%s）把 TMP_ROOT（%s）包住了，或者就等于它。\n"
        "pytest 会清空 basetemp，于是这里正在用的临时目录会被一起删掉；\n"
        "表现是 Windows 上删文件失败引发的 PermissionError。\n"
        "请在 scripts/check.sh 里把 basetemp 指到 .pytest-tmp/basetemp/ 这类独立目录。"
        % (base, ours)
    )


@pytest.fixture(autouse=True)
def isolated_key_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把密钥目录钉在临时目录里，**每个测试一个**。

    没有这一条时 ``Settings()`` 会按仓库布局解析到开发者**真实的** ``.key/``，
    于是整套测试都跑在"服务端配好了签名私钥"的状态下。那会把最重要的一条安全
    默认值测反 —— "没有私钥就不提供升级" —— 而测试自己还是绿的。

    目录**故意不预先建出来**：默认状态就该是"没有隐式密钥"，与加入 ``.key/``
    之前完全一致。要测有密钥的场景，测试自己往里写文件。

    这里是 autouse 且被 :func:`settings` 显式依赖：两个 fixture 都读环境变量，
    顺序错了就会得到"一半测试用真密钥、一半不用"这种最难查的结果。
    """
    where = tmp_path / "keys"
    monkeypatch.setenv(keys.KEY_DIR_ENV, str(where))
    return where


# --------------------------------------------------------------------------- #
# 测试辅助（跨测试文件共用 —— 放这里而不是某个 test_*.py 里，
# 避免测试文件之间互相 import）
# --------------------------------------------------------------------------- #


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def agent_headers(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def scan_entry(path: str, data: bytes, mtime: int = 1767225500) -> dict:
    return {"path": path, "sha256": sha256_of(data), "size": len(data), "mtime": mtime}


def do_tick(client, token: str, entries, *, machine_id: str, **kwargs) -> dict:
    """发一次 tick 并断言成功，返回响应体。

    ``stats`` 可以整体覆盖（"扫描根不存在"那条链路要往里面塞 ``scan_missing``），
    其余字段与 Agent 的上报形状保持一致。
    """
    stats = {"disk_free": 10 ** 10, "last_error": None, "queue": 0}
    stats.update(kwargs.pop("stats", {}) or {})
    payload = {
        "agent_version": "0.1.0",
        "machine_id": machine_id,
        "ts": 1767225600,
        "scan_root": "/home/student/code",
        "scan": entries,
        "partials": [],
        "stats": stats,
    }
    payload.update(kwargs)
    response = client.post("/api/v1/agent/tick", json=payload, headers=agent_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 新配对模型的公共动作
#
# 老模型是"逐台发注册码"，新模型是"镜像里一把统一密钥 + 机器上显示配对码"，
# 于是走完一遍注册要三步：签发密钥 → 机器注册（拿配对码）→ 教师绑定到人。
# 这三步几乎每个测试文件都要走，所以放在这里，而不是在每个文件里抄一遍。
# --------------------------------------------------------------------------- #


def new_machine_id() -> str:
    return "machine-" + uuid.uuid4().hex[:8]


def issue_bootstrap_key(client, headers: dict, label: str = "测试密钥") -> str:
    """签发一把统一密钥，返回**明文**（只在这一刻存在）。"""
    response = client.post(
        "/api/v1/admin/bootstrap-keys", json={"label": label}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["key"]


def enroll_machine(
    client,
    bootstrap_key: str,
    *,
    machine_id: str = None,
    machine_uuid: str = None,
    hostname: str = "exam-pc-01",
    fingerprint: str = None,
) -> dict:
    """走一次 /agent/enroll。返回响应体 + 本次用的 machine_id/machine_uuid。

    ``machine_uuid`` 默认**每次都是新的**：这正是"一台全新的机器"该有的样子。
    要模拟"同一台机器重新注册"，把这个值传回来即可。
    """
    machine_id = machine_id or new_machine_id()
    machine_uuid = machine_uuid or uuid.uuid4().hex
    response = client.post(
        "/api/v1/agent/enroll",
        json={
            "bootstrap_key": bootstrap_key,
            "machine_id": machine_id,
            "machine_uuid": machine_uuid,
            "machine_fingerprint": fingerprint,
            "hostname": hostname,
            "agent_version": "0.1.0",
            "os_info": "NOI Linux 2.0",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    body["machine_id"] = machine_id
    body["machine_uuid"] = machine_uuid
    return body


def make_roster(client, headers: dict, name: str = "高一(1)班", entries=None) -> dict:
    """建一份名单（可带条目），返回名单详情。"""
    response = client.post("/api/v1/admin/rosters", json={"name": name}, headers=headers)
    assert response.status_code == 200, response.text
    roster = response.json()
    if entries:
        add = client.post(
            "/api/v1/admin/rosters/%d/entries" % roster["id"], json=entries, headers=headers
        )
        assert add.status_code == 200, add.text
        detail = client.get("/api/v1/admin/rosters/%d" % roster["id"], headers=headers)
        assert detail.status_code == 200, detail.text
        roster = detail.json()
    return roster


def bind_by_code(client, headers: dict, pair_code: str, roster_entry_id: int) -> dict:
    response = client.post(
        "/api/v1/admin/machines/bind-by-code",
        json={"pair_code": pair_code, "roster_entry_id": roster_entry_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def workdir() -> Iterator[Path]:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / uuid.uuid4().hex[:12]
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def settings(workdir: Path, isolated_key_dir: Path) -> Settings:
    # 显式依赖 isolated_key_dir：保证环境变量在 Settings() 构造**之前**就位
    config = Settings()
    config.data_root = workdir / "data"
    # 默认关掉局域网发现。开着的话每个进入 lifespan 的测试都会去绑 UDP 端口 ——
    # 而测试进程里同时活着的 app 不止一个，于是"端口被占"的警告会刷满日志，
    # 真正测发现的那几条反而淹在里面。要测它的用 test_discovery.py 自己开。
    config.discovery_enabled = False
    return config


@pytest.fixture()
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    # 用上下文管理器进入，才能触发 lifespan（后台维护任务随之启停）
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers(app, client: TestClient) -> dict:
    ctx = app.state.ctx
    with ctx.db.session() as session:
        session.add(Admin(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD)))
    response = client.post(
        "/api/v1/admin/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


@pytest.fixture()
def contest(client: TestClient, admin_headers: dict) -> dict:
    response = client.post(
        "/api/v1/admin/contests",
        json={"name": "校内模拟赛", "slug": "mock-1", "status": "running"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def player(client: TestClient, admin_headers: dict, contest: dict) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S001", "name": "张三", "seat": "A1"}],
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["players"][0]


@pytest.fixture()
def bootstrap_key(client: TestClient, admin_headers: dict) -> str:
    """镜像里那把统一注册密钥（明文）。"""
    return issue_bootstrap_key(client, admin_headers)


@pytest.fixture()
def roster(client: TestClient, admin_headers: dict) -> dict:
    """一份含 S001 / S002 的名单 —— 与 ``player`` 夹具的 S001 对得上。"""
    return make_roster(
        client,
        admin_headers,
        entries=[
            {"player_no": "S001", "name": "张三", "seat": "A1"},
            {"player_no": "S002", "name": "李四"},
        ],
    )


@pytest.fixture()
def roster_entry(roster: dict) -> dict:
    """名单里 S001 的那一条 —— 「一个人」。"""
    return next(e for e in roster["entries"] if e["player_no"] == "S001")


@pytest.fixture()
def machine(client: TestClient, bootstrap_key: str, roster_entry: dict) -> dict:
    """一台还没配对到人的机器（待配对），返回注册响应体 + 本次的 machine_id。"""
    return enroll_machine(client, bootstrap_key)


@pytest.fixture()
def enrolled(
    client: TestClient,
    admin_headers: dict,
    player: dict,
    bootstrap_key: str,
    roster_entry: dict,
) -> dict:
    """走完整注册链路：签发密钥 → 机器注册 → 绑定到 S001 → 再注册一次。

    最后那一次 ``enroll`` 是必要的：机器必须**在配对之后**才知道自己该扫哪个
    目录、准考证号是多少 —— 而这些正是客户端真正要用的东西。少了这一步，
    夹具返回的就是注册那一刻的"还没配对"快照（``player_no`` 为空），
    用它去断言落盘路径会得出一个假的失败。

    返回注册响应体，并额外带上 ``machine_id`` / ``machine_uuid`` /
    ``roster_entry_id``。``player`` 夹具保证 S001 是这场比赛的选手、
    ``contest`` 是 running 的，所以这台的 ``bound`` 一定是 True。
    """
    machine = enroll_machine(client, bootstrap_key)
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry["id"])

    body = enroll_machine(
        client,
        bootstrap_key,
        machine_id=machine["machine_id"],
        machine_uuid=machine["machine_uuid"],
    )
    assert body["bound"] is True, body
    body["roster_entry_id"] = roster_entry["id"]
    return body
