"""对外地址只从登录了的教师请求里学；/admin/health 要登录。

审计：``/healthz`` 与 ``/admin/health`` 都不鉴权、恒回 200，而"对外地址"从成功的
管理端 / 探活请求的 Host 头学 —— 局域网里任何一个学生发一个 ``Host: 6.6.6.6:9``
的匿名请求，之后打的离线包就内嵌那个地址、发现应答也报它。``/admin/health`` 匿名
还会泄露 ``data_root`` 的绝对路径。
"""

from __future__ import annotations


def test_匿名探活改不了对外地址(client, app) -> None:
    before = app.state.ctx.public_url_hint

    assert client.get("/healthz", headers={"host": "6.6.6.6:9"}).status_code == 200

    assert app.state.ctx.public_url_hint == before


def test_登录了的教师请求才会被学(client, app, admin_headers) -> None:
    response = client.get(
        "/api/v1/admin/me", headers=dict(admin_headers, host="10.0.0.9:8000")
    )
    assert response.status_code == 200

    assert app.state.ctx.public_url_hint == "http://10.0.0.9:8000"


def test_不信客户端自己写的_x_forwarded_proto(client, app, admin_headers) -> None:
    client.get(
        "/api/v1/admin/me",
        headers=dict(admin_headers, host="10.0.0.9:8000", **{"x-forwarded-proto": "gopher"}),
    )
    assert app.state.ctx.public_url_hint == "http://10.0.0.9:8000"


def test_管理端探活要登录(client, admin_headers) -> None:
    assert client.get("/api/v1/admin/health").status_code == 401
    body = client.get("/api/v1/admin/health", headers=admin_headers).json()
    assert set(body) == {"ok", "agents_total", "agents_online", "data_root"}
