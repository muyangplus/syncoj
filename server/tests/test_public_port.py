"""公开端口（默认 80）：考生页与装机页，没有管理端。

要守住两件事：

1. **管理端在公开端口上不存在。** 回 404 而不是 403 —— 后者等于指路"去另一个
   端口试试"。这条隔离的意义在于：考场上学生随手敲的就是 ``http://10.0.0.5/``，
   那个地址上不该有登录框、更不该有接口地图（``/docs`` 不需要登录就能列出全部
   管理接口）。
2. **前端知道自己在公开端口上**，而且这件事由服务端注入
   （``window.__SYNCOJ__``），不是让前端自己数端口 —— http 的 80 在浏览器里
   ``location.port === ""``，数也数不准。

还有一条同样重要的**反面**：这套隔离只在"服务端真的在公开端口上听着"时生效。

* ``Settings.public_port`` 是"想开在哪"，``app.state.public_port`` 是"真的开在
  哪"。绑不上 80 时 ``serve`` 会降级（只留管理端口），降级之后必须**不能**再挡
  管理端 —— 否则一台绑不上特权端口的服务端会顺带把自己的管理界面关掉，现场
  只看到"管理界面 404"。
* 而 ``TestClient`` 的默认 ``base_url`` 恰好就是 80 端口（``http://testserver``
  → ``scope["server"] == ("testserver", 80)``）。没有上面这条界，全部既有测试都会
  被误判成"来自公开端口"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from syncoj_server import cli
from syncoj_server.config import Settings
from syncoj_server.main import create_app

#: 带 ``</head>`` 的产物，和真实构建产物一致。
INDEX_WITH_HEAD = (
    '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">'
    "<title>SyncOJ</title></head><body><div id=\"app\"></div></body></html>"
)

#: 极简产物（没有 ``</head>``）：注入得另找落点。
INDEX_BARE = '<!doctype html><html><body><div id="app"></div></body></html>'

#: 有 doctype、但连闭合标签都没有 —— 只能追加到末尾。
INDEX_TINY = '<!doctype html><div id="app"></div>'

#: 不带端口 = 80，和考场上学生敲的那个地址一模一样。
PUBLIC = "http://testserver"
#: 管理端口。
ADMIN = "http://testserver:8000"


def _dist(workdir: Path, index_html: str = INDEX_WITH_HEAD) -> Path:
    dist = workdir / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(index_html, encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return dist


def make_app(workdir: Path, *, public_port: int | None, index_html: str = INDEX_WITH_HEAD):
    """建一个服务端。``public_port`` 模拟 ``serve`` 真的绑上的那个端口。"""
    settings = Settings()
    settings.data_root = workdir / "data"
    settings.web_dist = _dist(workdir, index_html)
    app = create_app(settings)
    if public_port is not None:
        app.state.public_port = public_port
    return app


def _gated(response) -> bool:
    """这个 404 是端口门禁回的吗？（门禁的响应体是 ``{detail: "Not Found"}``）"""
    if response.status_code != 404:
        return False
    if not response.headers.get("content-type", "").startswith("application/json"):
        return False
    body = response.json()
    return body.get("code") == "not_found" and body.get("detail") == "Not Found"


# --------------------------------------------------------------------------- #
# 管理端在公开端口上不存在
# --------------------------------------------------------------------------- #


def test_admin_api_is_hidden_on_public_port(workdir: Path) -> None:
    """真接口在公开端口上必须回 404（而不是 401：那个等于承认接口在这儿）。"""
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        # 登录：管理端口上它是 401（接口在，只是密码错）
        assert client.post(
            "/api/v1/admin/login", json={"username": "admin", "password": "x"}
        ).status_code == 404
        # 其它管理接口也一样，包括免登录的 /me
        assert client.get("/api/v1/admin/me").status_code == 404
        assert client.get("/api/v1/admin/contests").status_code == 404


def test_api_map_is_hidden_on_public_port(workdir: Path) -> None:
    """/docs 与 /openapi.json 不需要登录就能列出全部管理接口，不能在考生面前。"""
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/healthz"):
            assert _gated(client.get(path)), path


def test_admin_port_is_unaffected(workdir: Path) -> None:
    """同一个 app，从管理端口进就一切照旧 —— 隔离是按请求来的，不是按进程。"""
    app = make_app(workdir, public_port=80)
    with TestClient(app, base_url=ADMIN) as client:
        assert client.post(
            "/api/v1/admin/login", json={"username": "admin", "password": "x"}
        ).status_code == 401
        assert client.get("/docs").status_code == 200
        assert client.get("/healthz").status_code == 200


def test_gate_follows_the_bound_port_not_the_setting(workdir: Path) -> None:
    """判据是"真的开在哪个端口"，不是配置里那个数字。

    这里把公开端口设成 8080：8000 与 80 都不该被挡，8080 才该被挡。
    """
    app = make_app(workdir, public_port=8080)
    with TestClient(app, base_url="http://testserver:8000") as client:
        assert client.get("/healthz").status_code == 200
    with TestClient(app, base_url=PUBLIC) as client:
        assert client.get("/healthz").status_code == 200
    with TestClient(app, base_url="http://testserver:8080") as client:
        assert _gated(client.get("/healthz"))

def test_no_gate_when_no_public_port_is_bound(workdir: Path) -> None:
    """**最重要的反面。**

    没有第二个监听时（``--factory`` 直接起、已经降级、以及全部既有测试），
    默认的 ``base_url`` 恰好就是 80 端口 —— 这时绝不能被当成公开端口，
    否则整个既有测试套件都会突然 404。
    """
    with TestClient(make_app(workdir, public_port=None)) as client:
        assert client.post(
            "/api/v1/admin/login", json={"username": "admin", "password": "x"}
        ).status_code == 401
        assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- #
# 公开端口上仍然可用的东西
# --------------------------------------------------------------------------- #


def test_player_and_install_apis_stay_reachable(workdir: Path) -> None:
    """被挡住的是**管理端**，不是"除两页之外的一切"。

    装机页与选手页要靠这两个命名空间吃饭：前者取装机台账/安装器，后者取自己的
    场次、考号与公告。挡错了它们，现场表现是"这两页白屏/转圈"。
    """
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        # 选手页：这台机器还没注册上来时它**自己**就是 404，带着自己的那句 detail。
        # 这里要区分的是"页面自己的失败态"和"端口门禁" —— 后者只有一句 Not Found。
        context = client.get("/api/v1/player/context")
        assert not _gated(context), context.text
        assert context.json()["detail"] != "Not Found"

        # 装机台账：没铺开版本时可以是错误，但**不能是"这个端口上没有"**
        ledger = client.get("/api/v1/agent/install.json")
        assert not _gated(ledger), ledger.text


def test_assets_are_still_served_on_public_port(workdir: Path) -> None:
    """前端自己的 js/css 当然要发得出去，别把 /assets 也挡了。"""
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        response = client.get("/assets/app.js")
        assert response.status_code == 200
        assert "console.log" in response.text


# --------------------------------------------------------------------------- #
# 前端怎么知道自己在公开端口上
# --------------------------------------------------------------------------- #


def test_index_carries_the_public_only_flag(workdir: Path) -> None:
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        text = client.get("/").text
        assert '"publicOnly":true' in text, text

    # 管理端口上那份**不带**这个标记，否则管理界面会自己跳去考生页
    with TestClient(make_app(workdir, public_port=80), base_url=ADMIN) as client:
        assert "__SYNCOJ__" not in client.get("/").text


def test_admin_port_is_injected_for_the_install_command(workdir: Path) -> None:
    """装机命令里那个地址要指向 **API 端口**，不是这一页恰好在的那个端口。

    命令里那个地址会被写进机器、从此长期用下去，而公开端口是个"方便用的"端口、
    可以关掉（``SYNCOJ_PUBLIC_PORT=0``）。两者混同的后果是：教师在 80 端口那一页
    装出来的那批机器，将来全都找不着服务端。
    """
    app = make_app(workdir, public_port=80)
    app.state.admin_port = 8000
    with TestClient(app, base_url=PUBLIC) as client:
        assert '"adminPort":8000' in client.get("/").text

    # 没告诉它 API 端口时**不要编一个**：前端会退回用它自己的 origin
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        assert "adminPort" not in client.get("/").text


def test_spa_routes_still_fall_back_on_public_port(workdir: Path) -> None:
    """兜底照旧：否则学生手打一个路径刷新就 404。

    注意这里**不能**把 /contests 这类管理路径挡在服务端 —— 得让它们照常发出
    index.html，由前端路由把访客送回考生页。服务端挡掉的话，学生看到的是一句
    "Not Found"，而不是考生页。
    """
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        for path in ("/", "/player", "/contests", "/whatever"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert '"publicOnly":true' in response.text, path


def test_mark_is_injected_inside_head(workdir: Path) -> None:
    with TestClient(make_app(workdir, public_port=80), base_url=PUBLIC) as client:
        text = client.get("/").text
        assert text.index("__SYNCOJ__") < text.index("</head>")


def test_mark_never_lands_before_the_doctype(workdir: Path) -> None:
    """``<!doctype html>`` 之前出现任何内容都会让浏览器进 quirks 模式。

    真实产物一定是完整的 HTML，但"注入点找不到"这种分支必须往**安全**的方向
    退化：宁可标记晚一点生效，也不能把页面布局搞乱。
    """
    for index_html in (INDEX_BARE, INDEX_TINY):
        with TestClient(
            make_app(workdir, public_port=80, index_html=index_html), base_url=PUBLIC
        ) as client:
            text = client.get("/").text
            assert '"publicOnly":true' in text, index_html
            assert text.startswith("<!doctype html"), index_html


# --------------------------------------------------------------------------- #
# serve：两个监听，以及绑不上时降级
# --------------------------------------------------------------------------- #


class _FakeSocket:
    def __init__(self, port: int) -> None:
        self.port = port
        self.closed = False

    def close(self) -> None:  # pragma: no cover - 只用来确认会被关掉
        self.closed = True


def _fake_listen(privileged: set[int]):
    def listen(host: str, port: int) -> _FakeSocket:
        if port in privileged:
            raise PermissionError(13, "Permission denied")
        return _FakeSocket(port)

    return listen


def _capture_serve(monkeypatch) -> dict:
    """抓住 ``serve`` 实际交给 uvicorn 的那批 socket，而不真的起服务。"""
    captured: dict = {}

    def fake_run(self, sockets=None):
        captured["ports"] = [sock.port for sock in (sockets or [])]
        captured["app"] = self.config.app

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    return captured


def test_serve_binds_both_ports(workdir: Path, monkeypatch, capsys) -> None:
    captured = _capture_serve(monkeypatch)
    monkeypatch.setattr(cli, "_listen_socket", _fake_listen(set()))

    assert cli.main(["--data-root", str(workdir), "serve"]) == 0

    assert captured["ports"] == [8000, 80]
    # 门禁的总开关就是这个值
    assert captured["app"].state.public_port == 80
    # 装机命令要用的 API 端口
    assert captured["app"].state.admin_port == 8000
    # 两个地址都要打出来：公开端口在浏览器里是省掉的，不说出来没人知道管理端在哪个端口
    out = capsys.readouterr().out
    assert "http://127.0.0.1/" in out and "8000" in out


def test_serve_degrades_when_public_port_is_privileged(
    workdir: Path, monkeypatch, capsys
) -> None:
    """绑不上 80 时：管理端口照常、**门禁关掉**、并且把两条修法打出来。

    这里最容易犯的错是"绑不上就当成公开端口不存在" —— 而现场看到的只是
    "考生页打不开"，没有人会想到是权限问题。所以提示必须给出两条明确的修法。
    """
    captured = _capture_serve(monkeypatch)
    monkeypatch.setattr(cli, "_listen_socket", _fake_listen({80}))

    assert cli.main(["--data-root", str(workdir), "serve"]) == 0

    assert captured["ports"] == [8000]
    # 降级之后管理界面必须仍然进得去
    assert not getattr(captured["app"].state, "public_port", None)

    err = capsys.readouterr().err
    assert "CAP_NET_BIND_SERVICE" in err
    assert "SYNCOJ_PUBLIC_PORT" in err


def test_serve_does_not_gate_when_public_port_is_the_admin_port(
    workdir: Path, monkeypatch, capsys
) -> None:
    """``--port 80``：本来就只有一套界面，不该再开第二个监听、也不该挡自己。"""
    captured = _capture_serve(monkeypatch)
    monkeypatch.setattr(cli, "_listen_socket", _fake_listen(set()))

    assert cli.main(["--data-root", str(workdir), "serve", "--port", "80"]) == 0

    assert captured["ports"] == [80]
    assert not getattr(captured["app"].state, "public_port", None)
    assert "不隔离端口" in capsys.readouterr().out


def test_serve_reports_admin_port_failure(workdir: Path, monkeypatch, capsys) -> None:
    """管理端口起不来就是真的起不来 —— 要说清楚是哪个端口、为什么。"""
    captured = _capture_serve(monkeypatch)
    monkeypatch.setattr(cli, "_listen_socket", _fake_listen({8000}))

    assert cli.main(["--data-root", str(workdir), "serve"]) == 2

    assert "ports" not in captured
    err = capsys.readouterr().err
    assert "8000" in err


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_listen_socket_binds_and_does_not_listen(host: str) -> None:
    """自己绑 socket 是这套双端口方案的地基，至少要确认它真的绑得上。

    端口取 0 = 让内核挑一个空闲端口：测试不该去抢固定端口，否则并行跑或本机
    已经有服务端时会随机红。
    """
    try:
        sock = cli._listen_socket(host, 0)
    except OSError as exc:  # pragma: no cover - 只在没有 IPv6 的机器上走到
        pytest.skip("这个环境绑不了 %s：%s" % (host, exc))
    try:
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()