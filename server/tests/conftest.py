"""pytest 公共夹具。

刻意**不用 ``tempfile``**：在受限的执行环境里它对新建目录做 chmod 会失败。
改为在仓库内建临时目录，用完 ``ignore_errors=True`` 清理。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.models import Admin
from syncoj_server.security import hash_password

REPO_ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = REPO_ROOT / ".pytest-tmp"

ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-battery"


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
def settings(workdir: Path) -> Settings:
    config = Settings()
    config.data_root = workdir / "data"
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
    return response.json()[0]


@pytest.fixture()
def enrolled(client: TestClient, admin_headers: dict, player: dict) -> dict:
    """走完整注册链路，返回 {token, agent_id, machine_id, player_no, ...}。"""
    code_response = client.post(
        "/api/v1/admin/players/%d/enroll-code" % player["id"], headers=admin_headers
    )
    assert code_response.status_code == 200, code_response.text
    code = code_response.json()["code"]

    machine_id = "machine-" + uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/agent/enroll",
        json={
            "enroll_code": code,
            "machine_id": machine_id,
            "hostname": "exam-pc-01",
            "agent_version": "0.1.0",
            "os_info": "NOI Linux 2.0",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    body["machine_id"] = machine_id
    return body
