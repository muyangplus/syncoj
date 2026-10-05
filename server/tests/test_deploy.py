"""文件下发测试：资产上传、任务编排、目标解析、进度聚合、取消与重试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import (
    agent_headers,
    bind_by_code,
    enroll_machine,
    do_tick as tick,
    sha256_of,
)


def upload_asset(client: TestClient, contest: dict, admin_headers: dict, name: str, data: bytes):
    return client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": (name, data, "application/octet-stream")},
        headers=admin_headers,
    )


def make_deploy(client: TestClient, contest: dict, admin_headers: dict, asset_id: int, **kwargs):
    payload = {"asset_id": asset_id, "target_kind": "all", "dest_dir": "exam", "mode": "overwrite"}
    payload.update(kwargs)
    return client.post(
        "/api/v1/admin/contests/%d/deploys" % contest["id"],
        json=payload,
        headers=admin_headers,
    )


# --------------------------------------------------------------------------- #
# 资产管理
# --------------------------------------------------------------------------- #


def test_upload_asset_stores_content_addressed(client: TestClient, contest: dict,
                                               admin_headers: dict, settings) -> None:
    data = b"test data content" * 100
    response = upload_asset(client, contest, admin_headers, "testdata.zip", data)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sha256"] == sha256_of(data)
    assert body["size"] == len(data)
    assert body["filename"] == "testdata.zip"
    assert (settings.blob_root / sha256_of(data)[:2] / sha256_of(data)).is_file()


def test_same_content_uploaded_twice_is_deduplicated(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """同一份 500MB 测试点重复上传不该占两份磁盘。"""
    data = b"identical content"
    first = upload_asset(client, contest, admin_headers, "a.zip", data).json()
    second = upload_asset(client, contest, admin_headers, "a.zip", data).json()

    assert first["id"] == second["id"]

    listed = client.get(
        "/api/v1/admin/contests/%d/assets" % contest["id"], headers=admin_headers
    ).json()
    assert listed["total"] == 1
    assert len(listed["items"]) == 1


def test_asset_requires_admin(client: TestClient, contest: dict) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": ("x.zip", b"x", "application/octet-stream")},
    )
    assert response.status_code == 401


def test_rename_asset_reports_how_many_targets_are_still_unfinished(
    client: TestClient, contest: dict, admin_headers: dict, player: dict
) -> None:
    """改名会影响**还没落地**的下发：那些目标会按新名字写进选手的目录。

    这个数字必须跟着响应回去。算了不报出去，教师就只能靠猜 ——
    而猜错的方向恰好是"我以为已经发完了"，于是改完名之后
    一部分机器的桌面上还是旧文件名。
    """
    asset = upload_asset(client, contest, admin_headers, "题面(1).pdf", b"statement").json()
    assert asset["pending_targets"] == 0, "还没建下发任务时是 0"

    make_deploy(client, contest, admin_headers, asset["id"], dest_dir="{player_no}")
    listed = client.get(
        "/api/v1/admin/contests/%d/assets" % contest["id"], headers=admin_headers
    ).json()["items"]
    assert listed[0]["pending_targets"] == 1, "列资产时就该看得到'还有几台没下完'"

    renamed = client.patch(
        "/api/v1/admin/assets/%d" % asset["id"],
        json={"filename": "题面.pdf"},
        headers=admin_headers,
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["filename"] == "题面.pdf"
    assert body["pending_targets"] == 1, "改名回执必须说清会影响几个还没落地的目标"
    # 内容不动：它按 sha256 存，改名只是换标签
    assert body["sha256"] == asset["sha256"]


def test_rename_asset_rejects_a_path(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    """文件名会变成选手桌面上的路径 —— 带斜杠的"名字"必须当场拒绝。"""
    asset = upload_asset(client, contest, admin_headers, "ok.pdf", b"x").json()
    response = client.patch(
        "/api/v1/admin/assets/%d" % asset["id"],
        json={"filename": "../../etc/passwd"},
        headers=admin_headers,
    )
    assert response.status_code == 400, response.text


# --------------------------------------------------------------------------- #
# 任务创建
# --------------------------------------------------------------------------- #


def test_create_deploy_all_targets(client: TestClient, contest: dict, admin_headers: dict,
                                   player: dict) -> None:
    client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S002"}],
        headers=admin_headers,
    )
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    response = make_deploy(client, contest, admin_headers, asset["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["pending"] == 2
    assert body["done"] == 0
    assert body["status"] == "pending"


def test_create_deploy_specific_players(client: TestClient, contest: dict, admin_headers: dict,
                                        player: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    response = make_deploy(
        client, contest, admin_headers, asset["id"],
        target_kind="player", player_ids=[player["id"]],
    )
    assert response.json()["total"] == 1
    assert response.json()["target_kind"] == "player"


def test_create_deploy_by_group(client: TestClient, contest: dict, admin_headers: dict) -> None:
    client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[
            {"player_no": "A1", "group_name": "A组"},
            {"player_no": "A2", "group_name": "A组"},
            {"player_no": "B1", "group_name": "B组"},
        ],
        headers=admin_headers,
    )
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    response = make_deploy(
        client, contest, admin_headers, asset["id"], target_kind="group", target_group="A组"
    )

    body = response.json()
    assert body["total"] == 2
    assert body["target_kind"] == "group"


def test_deploy_with_no_matching_players_is_rejected(
    client: TestClient, contest: dict, admin_headers: dict
) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    response = make_deploy(
        client, contest, admin_headers, asset["id"], target_kind="player", player_ids=[9999]
    )
    assert response.status_code == 400


def test_deploy_rejects_traversal_dest_dir(client: TestClient, contest: dict,
                                           admin_headers: dict, player: dict) -> None:
    """dest_dir 会成为 Agent 侧落地路径的一部分，必须在入口拦住。

    特别注意 "/absolute"：先归一化再校验的写法会把它洗成 "absolute" 并放行，
    等于校验失效。这里的断言就是防止那种写法回归。
    """
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()

    for bad in ("../etc", "/etc", "/absolute", "a/../../b", "a\\b", "C:/x", ".."):
        response = make_deploy(client, contest, admin_headers, asset["id"], dest_dir=bad)
        assert response.status_code == 400, "dest_dir=%r 应当被拒绝" % bad


def test_deploy_accepts_natural_dest_dir_spellings(client: TestClient, contest: dict,
                                                   admin_headers: dict, player: dict) -> None:
    """教师写 "exam/" 或 "exam//" 这类多余的斜杠不该被为难。"""
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()

    for ok, expected in (("exam", "exam"), ("exam/", "exam"), ("exam//", "exam"), ("", "")):
        response = make_deploy(client, contest, admin_headers, asset["id"], dest_dir=ok)
        assert response.status_code == 200, "dest_dir=%r 应当被接受" % ok
        assert response.json()["dest_dir"] == expected


# --------------------------------------------------------------------------- #
# 目标目录模板 {player_no}
# --------------------------------------------------------------------------- #


def test_dest_template_helpers() -> None:
    from syncoj_server.services.deploy import (
        DEST_PLAYER_TOKEN,
        expand_dest_template,
        validate_dest_template,
    )

    assert DEST_PLAYER_TOKEN == "{player_no}"
    assert expand_dest_template("{player_no}/p1", "S001") == "S001/p1"
    assert expand_dest_template("fixed/p1", "S001") == "fixed/p1"
    assert expand_dest_template("", "S001") == ""

    # 入库的是模板本身（只归一化末尾斜杠），不是展开后的样例
    assert validate_dest_template("{player_no}/p1/") == "{player_no}/p1"

    # 校验时占位符会被换成合法样例，所以非法结构照样能拦下 ——
    # 直接拿 "{player_no}/../x" 去匹配路径规则是匹配不出来的
    for bad in ("../{player_no}", "/{player_no}", "{player_no}/../x", "{player_no}\\x"):
        try:
            validate_dest_template(bad)
        except ValueError:
            continue
        raise AssertionError("dest_dir=%r 应当被拒绝" % bad)


def test_deploy_expands_player_no_per_target(client: TestClient, contest: dict,
                                             admin_headers: dict, enrolled: dict) -> None:
    """**全员下发时每台机器的目标目录都不同** —— 这正是必须由服务端展开的原因。

    客户端只知道自己的准考证号，不知道这次下发是给谁的。
    """
    asset = upload_asset(client, contest, admin_headers, "p1.pdf", b"statement").json()
    response = make_deploy(
        client, contest, admin_headers, asset["id"], dest_dir="{player_no}/p1"
    )
    assert response.status_code == 200, response.text
    assert response.json()["dest_dir"] == "{player_no}/p1"

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert len(body["deploy_jobs"]) == 1
    assert body["deploy_jobs"][0]["dest"] == "%s/p1/p1.pdf" % enrolled["player_no"]


def test_empty_dest_dir_lands_on_desktop_root(client: TestClient, contest: dict,
                                              admin_headers: dict, enrolled: dict) -> None:
    """留空目标目录 = 直接落在桌面根目录。

    这是**默认**行为：最常见的一次下发是题面 PDF + 样例，它们就该躺在桌面上，
    选手双击就能看。要是逼教师每次都写目录名，早晚会有人把题面塞进
    ``桌面/S001/p1/`` 里，然后一堆人找不到题。

    落地路径里不能出现空路径段（``/p1.pdf`` 或 ``S001//p1.pdf`` 都是错的）。
    """
    asset = upload_asset(client, contest, admin_headers, "题面.pdf", b"statement").json()
    response = make_deploy(client, contest, admin_headers, asset["id"], dest_dir="")
    assert response.status_code == 200, response.text
    assert response.json()["dest_dir"] == ""

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert len(body["deploy_jobs"]) == 1
    assert body["deploy_jobs"][0]["dest"] == "题面.pdf"


def test_dest_template_keeps_players_isolated(client: TestClient, contest: dict,
                                              admin_headers: dict, enrolled: dict, app,
                                              roster: dict, bootstrap_key: str) -> None:
    """两个选手拿到的目标路径必须不同。"""
    from sqlalchemy import select

    from syncoj_server.models import Player

    client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=[{"player_no": "S002"}], headers=admin_headers,
    )
    asset = upload_asset(client, contest, admin_headers, "p1.pdf", b"statement").json()
    make_deploy(client, contest, admin_headers, asset["id"], dest_dir="{player_no}/p1")

    first = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert first["deploy_jobs"][0]["dest"] == "%s/p1/p1.pdf" % enrolled["player_no"]

    # 另一台机器配给另一个人。机器绑的是**人**，所以下发路径必须按各自的人展开 ——
    # 展开成同一份就是"把 A 的题面发到 B 的目录"，而界面上看不出任何异常。
    with app.state.ctx.db.session() as session:
        assert session.execute(
            select(Player).where(Player.player_no == "S002")
        ).scalar_one()

    second_entry = next(e for e in roster["entries"] if e["player_no"] == "S002")
    second_machine = enroll_machine(client, bootstrap_key, hostname="exam-pc-02")
    bind_by_code(client, admin_headers, second_machine["pair_code"], second_entry["id"])

    second = tick(
        client, second_machine["token"], [], machine_id=second_machine["machine_id"]
    )
    assert second["deploy_jobs"][0]["dest"] == "S002/p1/p1.pdf"
    assert second["deploy_jobs"][0]["dest"] != first["deploy_jobs"][0]["dest"]


def test_deploy_rejects_bad_mode(client: TestClient, contest: dict, admin_headers: dict,
                                 player: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    response = make_deploy(client, contest, admin_headers, asset["id"], mode="yolo")
    assert response.status_code == 400


def test_deploy_rejects_asset_from_another_contest(
    client: TestClient, contest: dict, admin_headers: dict, player: dict
) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    other = client.post(
        "/api/v1/admin/contests",
        json={"name": "另一场", "slug": "other", "status": "running"},
        headers=admin_headers,
    ).json()

    response = client.post(
        "/api/v1/admin/contests/%d/deploys" % other["id"],
        json={"asset_id": asset["id"], "target_kind": "all"},
        headers=admin_headers,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 作业下发
# --------------------------------------------------------------------------- #


def test_tick_returns_pending_deploy_job(client: TestClient, contest: dict, admin_headers: dict,
                                         enrolled: dict) -> None:
    data = b"payload for download"
    asset = upload_asset(client, contest, admin_headers, "testdata.zip", data).json()
    make_deploy(client, contest, admin_headers, asset["id"], dest_dir="exam")

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])

    assert len(body["deploy_jobs"]) == 1
    job = body["deploy_jobs"][0]
    assert job["asset_id"] == asset["id"]
    assert job["dest"] == "exam/testdata.zip"
    assert job["sha256"] == sha256_of(data)
    assert job["size"] == len(data)
    assert job["offset"] == 0
    assert job["url"].endswith("/assets/%d" % asset["id"])


def test_tick_job_offset_reflects_reported_partial(client: TestClient, contest: dict,
                                                   admin_headers: dict, enrolled: dict) -> None:
    data = b"x" * 4096
    asset = upload_asset(client, contest, admin_headers, "big.zip", data).json()
    make_deploy(client, contest, admin_headers, asset["id"])

    body = tick(
        client, enrolled["token"], [],
        machine_id=enrolled["machine_id"],
        partials=[{"asset_id": asset["id"], "bytes_done": 1024}],
    )
    assert body["deploy_jobs"][0]["offset"] == 1024


def test_tick_drops_job_when_already_fully_downloaded(client: TestClient, contest: dict,
                                                      admin_headers: dict, enrolled: dict) -> None:
    """客户端报告已下载全部字节时不该再下发 —— 它在等自己上报完成。"""
    data = b"y" * 500
    asset = upload_asset(client, contest, admin_headers, "full.zip", data).json()
    make_deploy(client, contest, admin_headers, asset["id"])

    body = tick(
        client, enrolled["token"], [],
        machine_id=enrolled["machine_id"],
        partials=[{"asset_id": asset["id"], "bytes_done": len(data)}],
    )
    assert body["deploy_jobs"] == []


def test_cancel_stops_further_delivery(client: TestClient, contest: dict, admin_headers: dict,
                                       enrolled: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    task = make_deploy(client, contest, admin_headers, asset["id"]).json()

    cancel = client.post("/api/v1/admin/deploys/%d/cancel" % task["id"], headers=admin_headers)
    assert cancel.status_code == 200

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert body["deploy_jobs"] == [], "取消后不该再下发"


def test_cancel_completed_task_is_rejected(client: TestClient, contest: dict,
                                           admin_headers: dict, enrolled: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    task = make_deploy(client, contest, admin_headers, asset["id"]).json()

    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    # 上报完成
    tick(
        client, enrolled["token"], [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )

    response = client.post("/api/v1/admin/deploys/%d/cancel" % task["id"], headers=admin_headers)
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# 进度聚合
# --------------------------------------------------------------------------- #


def test_completed_assets_marks_target_done(client: TestClient, contest: dict,
                                            admin_headers: dict, enrolled: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    task = make_deploy(client, contest, admin_headers, asset["id"]).json()

    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    tick(
        client, enrolled["token"], [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )

    body = client.get("/api/v1/admin/deploys/%d" % task["id"], headers=admin_headers).json()
    assert body["done"] == 1
    assert body["status"] == "done"
    assert body["targets"][0]["status"] == "done"


def test_completed_job_is_not_offered_again(client: TestClient, contest: dict,
                                            admin_headers: dict, enrolled: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    make_deploy(client, contest, admin_headers, asset["id"])

    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"],
         completed_assets=[asset["id"]])

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert body["deploy_jobs"] == []


def test_retry_resets_failed_targets(client: TestClient, contest: dict, admin_headers: dict,
                                     enrolled: dict, app) -> None:
    from syncoj_server.models import DeployStatus, DeployTarget

    asset = upload_asset(client, contest, admin_headers, "p.zip", b"data").json()
    task = make_deploy(client, contest, admin_headers, asset["id"]).json()

    with app.state.ctx.db.session() as session:
        from sqlalchemy import select

        target = session.execute(
            select(DeployTarget).where(DeployTarget.task_id == task["id"])
        ).scalar_one()
        target.status = DeployStatus.FAILED
        target.last_error = "模拟失败"

    detail = client.get("/api/v1/admin/deploys/%d" % task["id"], headers=admin_headers).json()
    assert detail["failed"] == 1
    assert detail["status"] == "failed"

    retry = client.post("/api/v1/admin/deploys/%d/retry" % task["id"], headers=admin_headers)
    assert retry.status_code == 200
    assert "1" in retry.json()["detail"]

    detail = client.get("/api/v1/admin/deploys/%d" % task["id"], headers=admin_headers).json()
    assert detail["pending"] == 1
    assert detail["failed"] == 0

    body = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert len(body["deploy_jobs"]) == 1, "重试后应当重新下发"


# --------------------------------------------------------------------------- #
# 下载授权
# --------------------------------------------------------------------------- #


def test_asset_download_requires_being_a_target(client: TestClient, contest: dict,
                                                admin_headers: dict, enrolled: dict) -> None:
    """仅凭据有效是不够的 —— 否则任一选手能拖走全场测试点。"""
    asset = upload_asset(client, contest, admin_headers, "secret.zip", b"secret data").json()

    response = client.get(
        "/api/v1/agent/assets/%d" % asset["id"], headers=agent_headers(enrolled["token"])
    )
    assert response.status_code == 403, "未下发给本人的资源必须拒绝"


def test_asset_download_works_for_target(client: TestClient, contest: dict, admin_headers: dict,
                                         enrolled: dict) -> None:
    data = b"downloadable content" * 50
    asset = upload_asset(client, contest, admin_headers, "ok.zip", data).json()
    make_deploy(client, contest, admin_headers, asset["id"])

    response = client.get(
        "/api/v1/agent/assets/%d" % asset["id"], headers=agent_headers(enrolled["token"])
    )
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["accept-ranges"] == "bytes"


def test_asset_download_range_returns_partial(client: TestClient, contest: dict,
                                              admin_headers: dict, enrolled: dict) -> None:
    data = bytes(range(256)) * 40  # 10240 字节
    asset = upload_asset(client, contest, admin_headers, "range.bin", data).json()
    make_deploy(client, contest, admin_headers, asset["id"])

    response = client.get(
        "/api/v1/agent/assets/%d" % asset["id"],
        headers=dict(agent_headers(enrolled["token"]), Range="bytes=1000-"),
    )
    assert response.status_code == 206
    assert response.content == data[1000:]
    assert response.headers["content-range"] == "bytes 1000-10239/10240"
    assert response.headers["content-length"] == str(len(data) - 1000)


def test_range_beyond_eof_returns_416(client: TestClient, contest: dict, admin_headers: dict,
                                      enrolled: dict) -> None:
    data = b"short"
    asset = upload_asset(client, contest, admin_headers, "s.bin", data).json()
    make_deploy(client, contest, admin_headers, asset["id"])

    response = client.get(
        "/api/v1/agent/assets/%d" % asset["id"],
        headers=dict(agent_headers(enrolled["token"]), Range="bytes=99999-"),
    )
    assert response.status_code == 416


def test_asset_download_requires_auth(client: TestClient, contest: dict, admin_headers: dict) -> None:
    asset = upload_asset(client, contest, admin_headers, "a.zip", b"x").json()
    assert client.get("/api/v1/agent/assets/%d" % asset["id"]).status_code == 401


def test_asset_from_other_contest_is_not_found(client: TestClient, contest: dict,
                                               admin_headers: dict, enrolled: dict) -> None:
    other = client.post(
        "/api/v1/admin/contests",
        json={"name": "别的场次", "slug": "other-2", "status": "running"},
        headers=admin_headers,
    ).json()
    asset = upload_asset(client, other, admin_headers, "x.zip", b"x").json()

    response = client.get(
        "/api/v1/agent/assets/%d" % asset["id"], headers=agent_headers(enrolled["token"])
    )
    assert response.status_code == 404
