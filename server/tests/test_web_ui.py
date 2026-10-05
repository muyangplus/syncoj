"""管理界面的托管与 SPA 兜底。

这里最要紧的一条：**API 路径绝不能被 SPA 兜底吞掉**。否则打错一个接口路径会
返回一段 HTML，调用方拿到 `<!doctype html>` 去 JSON.parse，报出来的错跟真实
原因（路径拼错了）毫无关系。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncoj_server.config import Settings
from syncoj_server.main import create_app

INDEX_HTML = "<!doctype html><html><body><div id=\"app\"></div></body></html>"


@pytest.fixture()
def fake_dist(workdir: Path) -> Path:
    """造一个最小可用的前端产物。"""
    dist = workdir / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return dist


def make_client(workdir: Path, dist: Path | None) -> TestClient:
    settings = Settings()
    settings.data_root = workdir / "data"
    settings.web_dist = dist
    return TestClient(create_app(settings))


# --------------------------------------------------------------------------- #
# 没有前端产物时
# --------------------------------------------------------------------------- #


def test_api_still_works_without_frontend(workdir: Path) -> None:
    """前端没构建不该让服务端起不来 —— API 与 /docs 仍要可用。"""
    with make_client(workdir, None) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").json()["web_ui"] is False
        # OpenAPI 文档还在，纯 API 部署时靠它交互
        assert client.get("/openapi.json").status_code == 200


def test_root_is_404_without_frontend(workdir: Path) -> None:
    with make_client(workdir, None) as client:
        assert client.get("/").status_code == 404


def test_missing_dist_directory_is_tolerated(workdir: Path) -> None:
    """配置指向了一个不存在的目录，也要能启动。"""
    with make_client(workdir, workdir / "nope") as client:
        assert client.get("/healthz").json()["web_ui"] is False


# --------------------------------------------------------------------------- #
# 有前端产物时
# --------------------------------------------------------------------------- #


def test_root_serves_index(workdir: Path, fake_dist: Path) -> None:
    with make_client(workdir, fake_dist) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "id=\"app\"" in response.text


def test_healthz_reports_ui_enabled(workdir: Path, fake_dist: Path) -> None:
    with make_client(workdir, fake_dist) as client:
        assert client.get("/healthz").json()["web_ui"] is True


def test_spa_routes_fall_back_to_index(workdir: Path, fake_dist: Path) -> None:
    """history 模式的前端路由要靠服务端兜底，否则刷新页面就 404。"""
    with make_client(workdir, fake_dist) as client:
        for path in ("/overview", "/files", "/deploys", "/scores", "/events", "/releases"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "id=\"app\"" in response.text, path


def test_assets_are_served(workdir: Path, fake_dist: Path) -> None:
    with make_client(workdir, fake_dist) as client:
        response = client.get("/assets/app.js")
        assert response.status_code == 200
        assert "console.log" in response.text


def test_api_routes_are_not_shadowed(workdir: Path, fake_dist: Path) -> None:
    """API 必须优先于 SPA 兜底 —— FastAPI 按注册顺序匹配，这里验证顺序没搞反。"""
    with make_client(workdir, fake_dist) as client:
        # /docs 是 FastAPI 自带的，不能被 index.html 顶掉
        assert "swagger" in client.get("/docs").text.lower()

        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        assert openapi.json()["info"]["title"] == "SyncOJ"

        health = client.get("/healthz").json()
        assert "ok" in health


def test_unknown_api_path_returns_json_404_not_html(
    workdir: Path, fake_dist: Path
) -> None:
    """最重要的一条。

    打错接口路径时返回 HTML，调用方会拿 `<!doctype html>` 去 JSON.parse，
    报出来的错误跟真实原因（路径拼错了）毫无关系。
    """
    with make_client(workdir, fake_dist) as client:
        for path in (
            "/api/v1/admin/nonexistent",
            "/api/v1/agent/nonexistent",
            "/api/v1",
            "/openapi.json.bak",
        ):
            response = client.get(path)
            assert response.status_code == 404, path
            assert "id=\"app\"" not in response.text, "%s 被 SPA 兜底吞掉了" % path
            # 应当是 JSON，不是 HTML
            assert response.headers["content-type"].startswith("application/json"), path


def test_real_api_endpoints_still_functional_with_frontend(
    workdir: Path, fake_dist: Path
) -> None:
    """挂了前端之后，真实接口也要照常工作（包括 401 这类中间件行为）。"""
    with make_client(workdir, fake_dist) as client:
        response = client.post(
            "/api/v1/admin/login", json={"username": "nobody", "password": "x"}
        )
        assert response.status_code == 401
        assert "detail" in response.json()


def test_post_to_unknown_path_is_not_caught_by_spa(workdir: Path, fake_dist: Path) -> None:
    """兜底只处理 GET —— POST 打到未知路径应当老老实实 405/404。"""
    with make_client(workdir, fake_dist) as client:
        response = client.post("/overview", json={})
        assert response.status_code in (404, 405)
