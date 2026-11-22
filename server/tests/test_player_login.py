"""考生页：降级预览、场次考生页密码、准考证号 + 密码登录、网页下载。

四条边界，各由一组测试守着：

1. **降级预览谁都能看，所以只给场次与全场公告**。只发给部分人的 ``NOTICE.md``
   不出现；任何选手的考号、姓名、个人下发清单都不出现。
2. **登录只认"精确考号 + 场次密码"**。失败统一 403（不区分考号不存在 / 密码错），
   场次没设密码时这条路关着，按 IP 限速。
3. **令牌**绑定场次 + 选手、有期限、签了密码指纹：过期、篡改、换密码都作废。
4. **网页下载**只给确实下发给这个人、且没取消的资产；身份来自令牌或本机 IP。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from syncoj_server import migrations
from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.services import player_tokens

from conftest import bind_by_code, do_tick as tick, enroll_machine

PREVIEW = "/api/v1/player/preview"
LOGIN = "/api/v1/player/login"
CONTEXT = "/api/v1/player/context"
TOKEN_HEADER = "X-Player-Token"
PASSWORD = "kaosheng-123"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def set_password(client, admin_headers, contest, password=PASSWORD):
    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"player_password": password},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def add_players(client, admin_headers, contest, players):
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"], json=players, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    return response.json()["players"]


def make_contest(client, admin_headers, slug, **extra):
    body = {"name": "场次 " + slug, "slug": slug, "status": "running"}
    body.update(extra)
    response = client.post("/api/v1/admin/contests", json=body, headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()


def text_asset(client, contest, admin_headers, filename, content):
    response = client.post(
        "/api/v1/admin/contests/%d/assets/text" % contest["id"],
        json={"filename": filename, "content": content},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_asset(client, contest, admin_headers, name, data):
    response = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": (name, data, "application/octet-stream")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def deploy(client, contest, admin_headers, asset_id, *, kind="player", player_ids=None, group=None):
    body = {"asset_id": asset_id, "target_kind": kind, "dest_dir": "", "mode": "overwrite"}
    if player_ids is not None:
        body["player_ids"] = player_ids
    if group is not None:
        body["target_group"] = group
    response = client.post(
        "/api/v1/admin/contests/%d/deploys" % contest["id"], json=body, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def login(client, contest="mock-1", player_no="S001", password=PASSWORD):
    return client.post(LOGIN, json={"contest": contest, "player_no": player_no, "password": password})


def token_of(client, **kwargs) -> str:
    response = login(client, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def asset_url(asset_id: int) -> str:
    return "/api/v1/player/assets/%d" % asset_id


# --------------------------------------------------------------------------- #
# 1. 降级预览
# --------------------------------------------------------------------------- #


def test_预览只含在考场次与全场公告_不含任何个人信息(client, contest, admin_headers, player):
    others = add_players(
        client, admin_headers, contest,
        [{"player_no": "S002", "name": "李四", "seat": "B7", "group_name": "高一"}],
    )
    everyone = text_asset(client, contest, admin_headers, "NOTICE.md", "# 全场都看\n开考后不准离场")
    deploy(client, contest, admin_headers, everyone["id"], kind="all")
    # 只发给一部分人的公告（更晚下发）：降级预览**不能**拿它顶替全场公告
    partial = text_asset(client, contest, admin_headers, "notice.md", "只给 S002 的悄悄话")
    deploy(client, contest, admin_headers, partial["id"], player_ids=[others[0]["id"]])
    grouped = text_asset(client, contest, admin_headers, "Notice.MD", "只给高一的话")
    deploy(client, contest, admin_headers, grouped["id"], kind="group", group="高一")
    secret = upload_asset(client, contest, admin_headers, "个人题面.zip", b"secret")
    deploy(client, contest, admin_headers, secret["id"], player_ids=[player["id"]])

    response = client.get(PREVIEW)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"contests", "server_time"}
    assert isinstance(body["server_time"], int)
    assert len(body["contests"]) == 1
    item = body["contests"][0]
    assert set(item) == {
        "slug", "name", "status", "starts_at", "ends_at", "notice", "login_enabled",
    }
    assert item["slug"] == "mock-1"
    assert item["notice"] == {"filename": "NOTICE.md", "content": "# 全场都看\n开考后不准离场"}
    assert item["login_enabled"] is False

    raw = response.text
    for leaked in ("S001", "S002", "张三", "李四", "A1", "B7", "高一", "悄悄话", "个人题面"):
        assert leaked not in raw, leaked


def test_只有部分人的公告时预览里没有公告(client, contest, admin_headers, player):
    asset = text_asset(client, contest, admin_headers, "NOTICE.md", "只给 S001")
    deploy(client, contest, admin_headers, asset["id"], player_ids=[player["id"]])
    item = client.get(PREVIEW).json()["contests"][0]
    assert item["notice"] is None


def test_取消了的全场公告不再出现(client, contest, admin_headers, player):
    asset = text_asset(client, contest, admin_headers, "NOTICE.md", "作废的话")
    task = deploy(client, contest, admin_headers, asset["id"], kind="all")
    response = client.post("/api/v1/admin/deploys/%d/cancel" % task["id"], headers=admin_headers)
    assert response.status_code == 200, response.text
    assert client.get(PREVIEW).json()["contests"][0]["notice"] is None


def test_多场在考都列出来_非在考的不列(client, admin_headers):
    now = time.time()

    def iso(offset):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + offset))

    make_contest(client, admin_headers, "b-late", starts_at=iso(-600), ends_at=iso(3600))
    make_contest(client, admin_headers, "a-early", starts_at=iso(-3600), ends_at=iso(3600))
    make_contest(client, admin_headers, "frozen", status="frozen")
    make_contest(client, admin_headers, "soon", starts_at=iso(600), ends_at=iso(7200))
    make_contest(client, admin_headers, "draft", status="draft")
    make_contest(client, admin_headers, "closed", status="closed")
    # 状态还是 running、但已经过了结束时间（后台循环还没来得及把它置成 closed）
    make_contest(client, admin_headers, "ended", starts_at=iso(-7200), ends_at=iso(-60))

    slugs = [item["slug"] for item in client.get(PREVIEW).json()["contests"]]
    # 没配开考时间的排最前，其余按开考时间；还没开考的也在（页面显示距开考）
    assert slugs == ["frozen", "a-early", "b-late", "soon"]


def test_预览标出哪些场次开放了登录(client, contest, admin_headers):
    assert client.get(PREVIEW).json()["contests"][0]["login_enabled"] is False
    set_password(client, admin_headers, contest)
    assert client.get(PREVIEW).json()["contests"][0]["login_enabled"] is True


# --------------------------------------------------------------------------- #
# 2. 场次考生页密码（管理端，只写不读）
# --------------------------------------------------------------------------- #


def test_密码只写不读_可设置可清除(client, contest, admin_headers):
    assert contest["has_player_password"] is False
    out = set_password(client, admin_headers, contest)
    assert out["has_player_password"] is True
    assert PASSWORD not in str(out)

    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()["items"][0]
    assert listed["has_player_password"] is True
    assert not any("password" in key and key != "has_player_password" for key in listed)

    # 只改别的字段：密码不动
    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"], json={"note": "x"}, headers=admin_headers
    )
    assert response.json()["has_player_password"] is True

    response = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"clear_player_password": True},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["has_player_password"] is False


def test_密码太短或全是空白被拒(client, contest, admin_headers):
    url = "/api/v1/admin/contests/%d" % contest["id"]
    assert client.patch(url, json={"player_password": "abc"}, headers=admin_headers).status_code == 422
    assert client.patch(url, json={"player_password": "     "}, headers=admin_headers).status_code == 400


# --------------------------------------------------------------------------- #
# 3. 登录与令牌
# --------------------------------------------------------------------------- #


def test_准考证号加密码登录后看到完整上下文(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    asset = upload_asset(client, contest, admin_headers, "题面.zip", b"statement")
    deploy(client, contest, admin_headers, asset["id"], player_ids=[player["id"]])

    response = login(client, player_no="  S001 ")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_at"] - time.time() > 7 * 3600

    context = client.get(CONTEXT, headers={TOKEN_HEADER: body["token"]})
    assert context.status_code == 200, context.text
    data = context.json()
    assert data["matched_by"] == "token"
    assert data["player"] == {"player_no": "S001", "name": "张三", "seat": "A1", "group_name": None}
    assert [(item["id"], item["filename"]) for item in data["assets"]] == [(asset["id"], "题面.zip")]


def test_登录失败统一_403_不区分考号与密码(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    wrong_password = login(client, password="not-it")
    no_such_player = login(client, player_no="S999")
    # 考号只做精确匹配：大小写不同也算不存在
    near_miss = login(client, player_no="s001")
    for response in (wrong_password, no_such_player, near_miss):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "player_login_failed"
    assert wrong_password.json()["detail"] == no_such_player.json()["detail"]


def test_场次没设密码时登录不可用(client, contest, player):
    response = login(client)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "player_login_disabled"


def test_场次不存在是_404(client, contest, player):
    response = login(client, contest="no-such")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_登录按_ip_限速(app, client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    app.state.ctx.player_lookup_limiter.limit = 2
    codes = [login(client, password="x" * 6).status_code for _ in range(2)]
    codes.append(login(client).status_code)
    assert codes == [403, 403, 429]
    assert login(client).json()["code"] == "rate_limited"


def test_过期与篡改的令牌都作废(app, client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    token = token_of(client)
    ctx = app.state.ctx

    def status(value):
        response = client.get(CONTEXT, headers={TOKEN_HEADER: value})
        if response.status_code != 200:
            assert response.json()["code"] == "player_token_invalid"
        return response.status_code

    assert status(token) == 200

    version, contest_id, player_id, expires, signature = token.split(".")
    # 换成别人的 player_id / 改有效期 / 改签名
    assert status(".".join([version, contest_id, str(int(player_id) + 1), expires, signature])) == 403
    assert status(".".join([version, contest_id, player_id, str(int(expires) + 3600), signature])) == 403
    assert status(".".join([version, contest_id, player_id, expires, signature[:-2] + "AA"])) == 403
    assert status("garbage") == 403

    # 正确签名、但已过期（签发时间往回拨 9 小时）
    with ctx.db.session() as session:
        from syncoj_server.models import Contest

        password_hash = session.get(Contest, contest["id"]).player_password_hash
    expired, _ = player_tokens.issue_token(
        ctx.player_token_secret, contest["id"], player["id"], password_hash,
        int(time.time()) - 9 * 3600,
    )
    assert status(expired) == 403


def test_失效令牌不是_401(client, contest, admin_headers, player):
    """401 会被管理端前端当成"管理员会话过期"，教师在自己的浏览器里会被踢回登录页。"""
    response = client.get(CONTEXT, headers={TOKEN_HEADER: "v1.1.1.1.x"})
    assert response.status_code == 403


def test_换密码或清密码后旧令牌作废(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    token = token_of(client)
    set_password(client, admin_headers, contest, password="another-pass")
    assert client.get(CONTEXT, headers={TOKEN_HEADER: token}).status_code == 403

    token = token_of(client, password="another-pass")
    client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"clear_player_password": True},
        headers=admin_headers,
    )
    assert client.get(CONTEXT, headers={TOKEN_HEADER: token}).status_code == 403


def test_令牌密钥落盘_重启后仍然有效(app, client, contest, admin_headers, player, settings):
    set_password(client, admin_headers, contest)
    token = token_of(client)
    key_file = Path(settings.data_root) / "player-token.key"
    assert key_file.is_file()
    # 新起一个 app（相当于服务端重启）：内存里的密钥没了，要从盘上读回同一把
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(CONTEXT, headers={TOKEN_HEADER: token}).status_code == 200


# --------------------------------------------------------------------------- #
# 4. 网页下载
# --------------------------------------------------------------------------- #


def test_令牌下载下发给自己的文件(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    asset = upload_asset(client, contest, admin_headers, '题面 "a"\r\n.zip', b"statement-bytes")
    deploy(client, contest, admin_headers, asset["id"], player_ids=[player["id"]])
    token = token_of(client)

    response = client.get(asset_url(asset["id"]), headers={TOKEN_HEADER: token})
    assert response.status_code == 200, response.text
    assert response.content == b"statement-bytes"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "\r" not in disposition and "\n" not in disposition


def test_非目标选手下载是_403_未下发的资产是_404(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    other = add_players(client, admin_headers, contest, [{"player_no": "S002"}])[0]
    theirs = upload_asset(client, contest, admin_headers, "theirs.zip", b"theirs")
    deploy(client, contest, admin_headers, theirs["id"], player_ids=[other["id"]])
    never = upload_asset(client, contest, admin_headers, "never.zip", b"never")
    token = token_of(client)
    headers = {TOKEN_HEADER: token}

    assert client.get(asset_url(theirs["id"]), headers=headers).status_code == 403
    assert client.get(asset_url(never["id"]), headers=headers).status_code == 404
    assert client.get(asset_url(999999), headers=headers).status_code == 404
    # 对照：S002 自己能下
    other_token = token_of(client, player_no="S002")
    assert client.get(asset_url(theirs["id"]), headers={TOKEN_HEADER: other_token}).status_code == 200


def test_别的场次的资产即使下发过也拿不到(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    second = make_contest(client, admin_headers, "mock-2")
    them = add_players(client, admin_headers, second, [{"player_no": "S001"}])[0]
    asset = upload_asset(client, second, admin_headers, "x.zip", b"x")
    deploy(client, second, admin_headers, asset["id"], player_ids=[them["id"]])
    token = token_of(client)
    assert client.get(asset_url(asset["id"]), headers={TOKEN_HEADER: token}).status_code == 404


def test_取消的下发不能再下载(client, contest, admin_headers, player):
    set_password(client, admin_headers, contest)
    asset = upload_asset(client, contest, admin_headers, "a.zip", b"a")
    task = deploy(client, contest, admin_headers, asset["id"], player_ids=[player["id"]])
    token = token_of(client)
    assert client.get(asset_url(asset["id"]), headers={TOKEN_HEADER: token}).status_code == 200
    client.post("/api/v1/admin/deploys/%d/cancel" % task["id"], headers=admin_headers)
    assert client.get(asset_url(asset["id"]), headers={TOKEN_HEADER: token}).status_code == 403


def test_没有令牌也认不出本机时下载是_403(client, contest, admin_headers, player):
    asset = upload_asset(client, contest, admin_headers, "a.zip", b"a")
    deploy(client, contest, admin_headers, asset["id"], player_ids=[player["id"]])
    response = client.get(asset_url(asset["id"]))
    assert response.status_code == 403
    assert client.get(asset_url(asset["id"]), headers={TOKEN_HEADER: "bad"}).status_code == 403


def test_本机_IP_自动匹配后可下载自己的文件(
    app, client, contest, admin_headers, bootstrap_key, player, roster
):
    entries = {entry["player_no"]: entry for entry in roster["entries"]}
    other = add_players(client, admin_headers, contest, [{"player_no": "S002"}])[0]
    ip_client = TestClient(app, client=("10.9.1.1", 50000))
    machine = enroll_machine(ip_client, bootstrap_key)
    bind_by_code(client, admin_headers, machine["pair_code"], entries["S001"]["id"])
    body = enroll_machine(
        ip_client, bootstrap_key,
        machine_id=machine["machine_id"], machine_uuid=machine["machine_uuid"],
    )
    tick(ip_client, body["token"], [], machine_id=machine["machine_id"])

    mine = upload_asset(client, contest, admin_headers, "mine.zip", b"mine")
    deploy(client, contest, admin_headers, mine["id"], player_ids=[player["id"]])
    theirs = upload_asset(client, contest, admin_headers, "theirs.zip", b"theirs")
    deploy(client, contest, admin_headers, theirs["id"], player_ids=[other["id"]])

    assert ip_client.get(CONTEXT).json()["matched_by"] == "machine"
    response = ip_client.get(asset_url(mine["id"]))
    assert response.status_code == 200, response.text
    assert response.content == b"mine"
    assert ip_client.get(asset_url(theirs["id"])).status_code == 403


# --------------------------------------------------------------------------- #
# 5. 公开端口
# --------------------------------------------------------------------------- #


def test_公开端口上考生页接口可用_管理接口仍不可用(workdir: Path) -> None:
    settings = Settings()
    settings.data_root = workdir / "data"
    app = create_app(settings)
    app.state.public_port = 80
    with TestClient(app, base_url="http://testserver") as client:
        preview = client.get(PREVIEW)
        assert preview.status_code == 200, preview.text
        assert preview.json()["contests"] == []

        response = login(client, contest="nope")
        assert response.status_code == 404
        assert response.json()["detail"] != "Not Found", "被端口门禁挡住了"

        download = client.get(asset_url(1))
        assert download.status_code == 403, download.text

        gated = client.patch("/api/v1/admin/contests/1", json={"player_password": "abcd"})
        assert gated.status_code == 404
        assert gated.json()["detail"] == "Not Found"


# --------------------------------------------------------------------------- #
# 6. 迁移 022
# --------------------------------------------------------------------------- #


def test_老库升级补上考生页密码列_老场次不开放登录(workdir: Path) -> None:
    engine = create_engine("sqlite:///%s" % (workdir / "v19.db"))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE contest (id INTEGER PRIMARY KEY, slug VARCHAR(64), name VARCHAR(200),"
            " status VARCHAR(16), created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO contest (id, slug, name, status, created_at)"
            " VALUES (1, 'old', '老场次', 'running', '2026-01-01 00:00:00')"
        ))
        conn.execute(text("PRAGMA user_version = 19"))

    applied = migrations.migrate(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("contest")}
    with engine.connect() as conn:
        value = conn.execute(text("SELECT player_password_hash FROM contest WHERE id = 1")).scalar()
    assert migrations.current_version(engine) == migrations.LATEST_VERSION
    engine.dispose()
    assert any(step.startswith("022 ") for step in applied)
    assert "player_password_hash" in columns
    assert value is None
