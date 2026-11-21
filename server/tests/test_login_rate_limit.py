"""管理端登录按来源 IP 限速。

scrypt 让单次口令校验变慢，但不限次数的话局域网里可以一直试下去 —— 一个考场的
学生有整整一场考试的时间。
"""

from __future__ import annotations


def test_同一个_ip_一分钟内试太多次会被限速(client, app) -> None:
    app.state.ctx.login_limiter.limit = 3
    attempts = [
        client.post("/api/v1/admin/login", json={"username": "admin", "password": "wrong"})
        for _ in range(5)
    ]

    assert [r.status_code for r in attempts[:3]] == [401, 401, 401]
    assert [r.status_code for r in attempts[3:]] == [429, 429]
    assert attempts[-1].json()["code"] == "rate_limited"
