"""注册限速。

单独一个文件而不是塞进集成测试里：限速逻辑本身不难，难的是**它得真的挡住**，
而且不能把正常的 50 台机器开机注册误伤。这两件事都要能单独验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.services.ratelimit import RateLimitExceeded, RateLimiter


# --------------------------------------------------------------------------- #
# 计数器本身
# --------------------------------------------------------------------------- #


def test_limiter_allows_up_to_the_limit() -> None:
    limiter = RateLimiter(limit=3)
    for _ in range(3):
        limiter.check("ip", now=100.0)


def test_limiter_rejects_over_the_limit() -> None:
    limiter = RateLimiter(limit=2)
    limiter.check("ip", now=100.0)
    limiter.check("ip", now=100.0)
    with pytest.raises(RateLimitExceeded):
        limiter.check("ip", now=100.0)


def test_limiter_window_resets() -> None:
    """窗口过去之后必须恢复 —— 否则机器重连一次就永久被挡。"""
    limiter = RateLimiter(limit=1)
    limiter.check("ip", now=100.0)
    with pytest.raises(RateLimitExceeded):
        limiter.check("ip", now=100.5)
    limiter.check("ip", now=101.5)  # 新窗口


def test_limiter_keys_are_independent() -> None:
    """一台机器刷爆了不该连累别的机器。"""
    limiter = RateLimiter(limit=1)
    limiter.check("10.0.0.1", now=100.0)
    limiter.check("10.0.0.2", now=100.0)  # 不该抛


def test_zero_limit_means_unlimited() -> None:
    limiter = RateLimiter(limit=0)
    for _ in range(1000):
        limiter.check("ip", now=100.0)


def test_limiter_prunes_stale_keys() -> None:
    """表不能无限增长 —— 伪造源 IP 就能让它涨，那本身就是攻击面。"""
    limiter = RateLimiter(limit=1)
    for index in range(5000):
        limiter.check("ip-%d" % index, now=100.0 + index * 0.001)

    assert len(limiter._buckets) < 5000, "过期记录没有被清掉，表会一直涨"


def test_pruning_does_not_reset_the_current_key() -> None:
    """清理**不能**把正在计数的那个 key 顺手删掉。

    否则攻击者只要在刷够量之后继续刷，就能靠"触发清理 → 自己被清空"
    把自己变回零次，限速形同虚设。
    """
    limiter = RateLimiter(limit=1)
    for index in range(5000):
        limiter.check("ip-%d" % index, now=100.0 + index * 0.001)

    last = 100.0 + 4999 * 0.001
    assert "ip-4999" in limiter._buckets, "正在计数的 key 被清理掉了"

    # 同一个窗口内再来一次就该超限 —— 说明它的计数确实被保留了
    with pytest.raises(RateLimitExceeded):
        limiter.check("ip-4999", now=last + 0.001)


# --------------------------------------------------------------------------- #
# 接口层
# --------------------------------------------------------------------------- #


@pytest.fixture()
def tight_app(workdir: Path):
    """一个把限速调到很紧的服务端。"""
    settings = Settings()
    settings.data_root = workdir / "data"
    settings.enroll_per_ip_per_second = 2
    settings.enroll_global_per_second = 5
    return create_app(settings)


@pytest.fixture()
def tight_client(tight_app):
    with TestClient(tight_app) as client:
        yield client


def _login(client: TestClient) -> dict:
    from syncoj_server.models import Admin
    from syncoj_server.security import hash_password

    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        session.add(Admin(username="admin", password_hash=hash_password("pw-123456")))
    response = client.post(
        "/api/v1/admin/login", json={"username": "admin", "password": "pw-123456"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_enroll_is_rate_limited_per_ip(tight_client: TestClient) -> None:
    headers = _login(tight_client)
    key = tight_client.post(
        "/api/v1/admin/bootstrap-keys", json={}, headers=headers
    ).json()["key"]

    codes = []
    for index in range(5):
        response = tight_client.post(
            "/api/v1/agent/enroll",
            json={"bootstrap_key": key, "machine_id": "m-%d" % index},
        )
        codes.append(response.status_code)

    assert 429 in codes, "限速没生效：codes=%s" % codes
    # 前两次应当成功
    assert codes[0] == 200 and codes[1] == 200, codes

    limited = tight_client.post(
        "/api/v1/agent/enroll", json={"bootstrap_key": key, "machine_id": "m-x"}
    )
    assert limited.status_code == 429
    assert limited.headers.get("retry-after")


def test_rate_limit_does_not_break_the_first_machines(tight_client: TestClient) -> None:
    """**别把正常的开机注册误伤掉。**

    50 台机器同时开机是真实场景。限速的目标是"挡住无限刷"，
    不是"每台机器只许注册一次" —— 后者会让整间机房有一半上不来。
    """
    headers = _login(tight_client)
    key = tight_client.post(
        "/api/v1/admin/bootstrap-keys", json={}, headers=headers
    ).json()["key"]

    first = tight_client.post(
        "/api/v1/agent/enroll", json={"bootstrap_key": key, "machine_id": "m-1"}
    )
    assert first.status_code == 200, first.text

    # 重新注册（快照还原、服务重启）永远要能过 —— 它在服务端看是同一台机器，
    # 但仍然走同一条限速路径，所以这里验证的是"重试不会被累计惩罚"
    second = tight_client.post(
        "/api/v1/agent/enroll", json={"bootstrap_key": key, "machine_id": "m-1"}
    )
    assert second.status_code == 200, second.text
