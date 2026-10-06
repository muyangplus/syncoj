"""选手页（免登录）测试。

这一页的用途是"选手坐在考场上，确认自己要交什么、老师有什么话要说"，
于是它有四条硬边界，各由一组测试守着：

1. **只看得到自己的东西**。免登录意味着没有凭据兜底，定位只能靠"这台机器"或者
   "场次 + 考号"。一旦过滤条件写漏（比如忘了 ``DeployTarget.player_id``），
   任何选手输自己的考号、或者任何一台机器打开这一页，就能看到**全场**的下发清单 ——
   页面照样正常渲染，没人会发现。``test_别人的资产不会出现在他的清单里`` 与
   ``test_同一个_IP_不会拿到别人的清单`` 就是为这两行过滤条件写的；
   ``test_发给别人的公告本选手看不到`` 盯的是同一件事在**公告**上的表现。
2. **自动匹配本机**（主路径）：不传参数时按来源 IP 认机器，再复用 ``/tick`` 那套
   ``resolve_machine`` 解析场次与选手。认不出来就 404 人话、配对没完成就 403、
   解析不出场次就按 ``resolve_machine`` 的 code 报 —— 一条都不许猜。
3. **不多给一个字节**。响应里不该出现代码、成绩、别人的名字。
   ``test_响应里没有代码与成绩字段`` 用 key 集合把这件事钉死：以后有人"顺手"
   在模型里加一个 files 字段，它会红。
4. **公告来自"下发给他的文件"**（``NOTICE.md``），不是一个场次配置字段。
   没下发就是 ``null``；发给别人、发给别场次、非 UTF-8、超过 64KB 的都不算 ——
   这一页不能因为一个坏文件空掉，也不能因此 500。

最后一组是迁移：这一页的"自动匹配本机"依赖 ``agent.last_seen_ip`` 这一列，
而"新库能建出来"和"已有的库能自动加上"是两条不同的路径 —— 只测其中一条，
另一条就会在升级那一刻带着 ``no such column`` 炸掉。这一轮同时删掉了
``contest.player_notice``，所以两条路径也都不能再有那一列：留着它就意味着
"新库升级上来的库"和"新建的库"结构分叉。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from syncoj_server import migrations
from syncoj_server.models import DeployTarget, DeployTask

from conftest import (
    bind_by_code,
    do_tick as tick,
    enroll_machine,
    make_roster,
    sha256_of,
)

PLAYER_PATH = "/api/v1/player/context"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def add_players(client, admin_headers, contest, players):
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest["id"],
        json=players,
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["players"]


def upload_asset(client, contest, admin_headers, name, data=b"payload"):
    response = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": (name, data, "application/octet-stream")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def text_asset(client, contest, admin_headers, filename, content):
    """走「新建文本文件」那条入口造一个资产 —— 公告就是这么发出来的。"""
    response = client.post(
        "/api/v1/admin/contests/%d/assets/text" % contest["id"],
        json={"filename": filename, "content": content},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def deploy_to(client, contest, admin_headers, asset_id, player_ids, dest_dir=""):
    response = client.post(
        "/api/v1/admin/contests/%d/deploys" % contest["id"],
        json={
            "asset_id": asset_id,
            "target_kind": "player",
            "player_ids": player_ids,
            "dest_dir": dest_dir,
            "mode": "overwrite",
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def fetch(client, contest: str = "mock-1", player_no: str = "S001", headers=None):
    return client.get(
        PLAYER_PATH,
        params={"contest": contest, "player_no": player_no},
        headers=headers or {},
    )


def client_from(app, ip: str) -> TestClient:
    """一个"来源 IP 是 ip"的客户端。

    TestClient 默认把来源写成 ``testclient``，那会让所有机器在测试里共用一个 IP ——
    而"按来源 IP 自动匹配本机"最怕的恰恰是共用：测试全绿，线上串人。
    所以这一页的测试自己指定 IP。

    **不进入上下文管理器**：lifespan（后台任务）已经由 ``client`` 夹具那个实例
    起过一次，再进一次会在同一个 ctx 上并起第二套后台循环。
    """
    return TestClient(app, client=(ip, 50000))


def enroll_bound_machine(
    client, ip_client, admin_headers, bootstrap_key, roster_entry_id, *, expect_bound=True
):
    """造一台"已经配对好、并且从某个 IP 说过话"的机器。

    来源 IP 是机器**上一次请求**时写下来的，所以这里必须真的用 ``ip_client``
    走一遍注册 + 一次 tick，而不是往库里塞一个字段：塞字段测的就不是"记 IP"
    这条链路了，而那条链路正是主路径能不能用的全部依据。
    """
    machine = enroll_machine(ip_client, bootstrap_key)
    bind_by_code(client, admin_headers, machine["pair_code"], roster_entry_id)
    body = enroll_machine(
        ip_client,
        bootstrap_key,
        machine_id=machine["machine_id"],
        machine_uuid=machine["machine_uuid"],
    )
    assert body["bound"] is expect_bound, body
    # 一次心跳：把来源 IP 记到这台机器上（未配对的机器也走同一个入口）
    tick(ip_client, body["token"], [], machine_id=machine["machine_id"])
    return body


# --------------------------------------------------------------------------- #
# 1. 显式查找（兜底路径）
# --------------------------------------------------------------------------- #


def test_清单只包含下发给他的资产(client, contest, admin_headers, player):
    other = add_players(client, admin_headers, contest, [{"player_no": "S002", "name": "李四"}])[0]
    mine = upload_asset(client, contest, admin_headers, "题面.zip", b"statement")
    theirs = upload_asset(client, contest, admin_headers, "样例.zip", b"samples")
    # 上传了、但一条下发任务都没有的：既不是发给他的，也不该出现
    upload_asset(client, contest, admin_headers, "备用.zip", b"backup")

    deploy_to(client, contest, admin_headers, mine["id"], [player["id"]], dest_dir="")
    deploy_to(
        client, contest, admin_headers, theirs["id"], [other["id"]], dest_dir="{player_no}/p1"
    )

    response = fetch(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert [asset["filename"] for asset in body["assets"]] == ["题面.zip"]
    asset = body["assets"][0]
    assert asset["sha256"] == sha256_of(b"statement")
    assert asset["size"] == len(b"statement")
    assert asset["status"] == "pending"
    assert asset["finished_at"] is None
    # 留空 = 直接落在桌面上。原样回一个空串的话，页面上只能显示一个"—"，
    # 而那个文件其实就在选手桌面上
    assert asset["dest_dir"] == "桌面"

    assert body["matched_by"] == "explicit"
    assert body["contest"] == {
        "slug": "mock-1",
        "name": "校内模拟赛",
        "status": "running",
        # 没配时间窗 = 不限制，如实回 null（页面显示"不限"）
        "starts_at": None,
        "ends_at": None,
    }
    assert body["player"] == {
        "player_no": "S001",
        "name": "张三",
        "seat": "A1",
        "group_name": None,
    }
    # 没有下发公告：这一份清单里只有题面
    assert body["notice"] is None


def test_别的选手的资产不会出现在他的清单里(client, contest, admin_headers, player):
    """这条守的是 ``DeployTarget.player_id`` 那一行过滤。

    去掉它，S001 的页面照样正常渲染 —— 只是清单里多了别人的文件，
    而"文件多了一个"在现场几乎不会有人发现。所以这里断言的是**精确相等**：
    他的清单里有几个、是哪几个，一个不多一个不少。
    """
    other = add_players(client, admin_headers, contest, [{"player_no": "S002"}])[0]
    first = upload_asset(client, contest, admin_headers, "a.zip", b"aaa")
    second = upload_asset(client, contest, admin_headers, "b.zip", b"bbb")
    deploy_to(client, contest, admin_headers, first["id"], [other["id"]])
    deploy_to(client, contest, admin_headers, second["id"], [other["id"]])

    mine = fetch(client).json()
    assert mine["assets"] == [], "别人的下发出现在了他的清单里"
    assert mine["player"]["player_no"] == "S001"

    # 反过来也要成立：另一个人自己看得到 —— 否则"空清单"可能是因为
    # 我们把下发整个写坏了，而这条测试会误以为它守住了什么
    theirs = fetch(client, player_no="S002").json()
    assert [asset["filename"] for asset in theirs["assets"]] == ["b.zip", "a.zip"]


def test_场次不存在与考号不存在都是_404_人话(client, contest, player):
    missing_contest = fetch(client, contest="no-such")
    assert missing_contest.status_code == 404, missing_contest.text
    body = missing_contest.json()
    assert body["code"] == "not_found"
    assert "no-such" in body["detail"], "报错要说清是哪个场次找不到"

    missing_player = fetch(client, player_no="S999")
    assert missing_player.status_code == 404, missing_player.text
    body = missing_player.json()
    assert body["code"] == "not_found"
    assert "S999" in body["detail"]
    assert "没有编号" in body["detail"], "要说清是「这个场次里没有这个编号」，而不是「查不到」"


def test_只填一个参数是_400(client, contest, player):
    """一个参数定位不到人，而"猜另一半"正是这一页最不该做的事。"""
    only_contest = client.get(PLAYER_PATH, params={"contest": "mock-1"})
    assert only_contest.status_code == 400, only_contest.text
    assert only_contest.json()["code"] == "bad_request"

    only_player = client.get(PLAYER_PATH, params={"player_no": "S001"})
    assert only_player.status_code == 400, only_player.text
    assert only_player.json()["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# 2. 考场公告：从下发给他的资产里认出一个 NOTICE.md
# --------------------------------------------------------------------------- #


def test_下发的_NOTICE_md_成为考场公告(client, contest, admin_headers, player):
    """公告走的就是「文件下发」那条路：它既是公告，也是一份普通的下发文件。

    这条同时钉住两件事：正文与文件名原样回来、以及它**同时**出现在 ``assets`` 里 ——
    "并入文件下发"的意思就是后者，删掉它等于造出第二套下发机制。
    """
    body = "# 考场须知\n\n开考 30 分钟内不准离场。\n交卷前先存盘。"
    asset = text_asset(client, contest, admin_headers, "NOTICE.md", body)
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    response = fetch(client)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["notice"] == {"filename": "NOTICE.md", "content": body}
    # 公告同时是清单里的一行 —— 少了它，"公告走文件下发"这句话就不成立了
    assert [item["filename"] for item in payload["assets"]] == ["NOTICE.md"]
    # 字段集钉死：以后有人往公告里塞一个 is_notice / html 之类的字段，这条会红
    assert set(payload["notice"]) == {"filename", "content"}


def test_没下发公告时_notice_是_null(client, contest, admin_headers, player):
    """没公告就是 ``null``，不是一个空对象。

    空对象会让前端判"有没有公告"变成"content 是不是空串"——两处判据迟早分叉，
    而且契约里写死了 ``Optional[PlayerNoticeOut]``，这里按契约钉死。
    """
    asset = upload_asset(client, contest, admin_headers, "题面.zip", b"statement")
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    payload = fetch(client).json()
    assert payload["notice"] is None
    assert [item["filename"] for item in payload["assets"]] == ["题面.zip"]


def test_发给别人的公告本选手看不到(client, contest, admin_headers, player):
    """这条守的是公告查询里的 ``DeployTarget.player_id``。

    去掉它，S001 的页面会显示**发给 S002 的公告** —— 页面照样正常渲染，
    而公告常常是"XX 号同学请留下"这类只说给一个人听的话，发错人比看不到更糟。
    """
    other = add_players(client, admin_headers, contest, [{"player_no": "S002"}])[0]
    asset = text_asset(client, contest, admin_headers, "NOTICE.md", "这是只说给 S002 的话")
    deploy_to(client, contest, admin_headers, asset["id"], [other["id"]])

    mine = fetch(client).json()
    assert mine["player"]["player_no"] == "S001"
    assert mine["notice"] is None, "别人的公告出现在了他的页面上"
    assert mine["assets"] == []

    # 反过来：S002 自己看得到 —— 否则"看不到"可能只是因为公告整条链路坏了，
    # 而这条测试会误以为它守住了边界
    theirs = fetch(client, player_no="S002").json()
    assert theirs["notice"]["content"] == "这是只说给 S002 的话"


def test_别的场次的公告不会出现在这一场(client, contest, admin_headers, player):
    """这条守的是公告查询里的 ``DeployTask.contest_id``。

    同一个人可能在多场里都在，公告只该显示**这一场**发的那一份。少了这个条件，
    选手页会把另一场的公告混进来 —— 而两场的话可能互相矛盾。
    """
    second = client.post(
        "/api/v1/admin/contests",
        json={"name": "第二场", "slug": "mock-2", "status": "running"},
        headers=admin_headers,
    )
    assert second.status_code == 200, second.text
    second = second.json()
    second_player = add_players(client, admin_headers, second, [{"player_no": "S001"}])[0]

    asset = text_asset(client, second, admin_headers, "NOTICE.md", "这是第二场的话")
    deploy_to(client, second, admin_headers, asset["id"], [second_player["id"]])

    # 第一场里的同考号选手：看不到第二场的公告
    assert fetch(client).json()["notice"] is None
    # 第二场里的他：看得到
    assert fetch(client, contest="mock-2").json()["notice"]["content"] == "这是第二场的话"


def test_接口之外造出来的脏数据也带不出另一场的公告(
    app, client, contest, admin_headers, player
):
    """这条咬的是公告查询里 ``DeployTask.contest_id == 本场`` 那一半。

    正常数据下两半过滤是**冗余**的：``DeployTarget.player_id`` 指向的 Player 行
    本来就只属于一个场次，所以接口根本造不出"这场的目标挂在另一场的任务上"。
    冗余不等于没用 —— 它是纵深防御，挡的是有人直接改库、或者以后允许一场的
    选手被另一场复用时的那类数据。既然接口造不出来，这里就绕过接口直接塞一条：
    把第二场那条公告任务，也挂到本场这位选手头上。

    少了 contest 这一半，本场选手的页面上就会出现第二场的公告（下面的断言会红）。
    """
    second = client.post(
        "/api/v1/admin/contests",
        json={"name": "第二场", "slug": "mock-2", "status": "running"},
        headers=admin_headers,
    )
    assert second.status_code == 200, second.text
    second = second.json()
    second_player = add_players(client, admin_headers, second, [{"player_no": "S001"}])[0]
    asset = text_asset(client, second, admin_headers, "NOTICE.md", "第二场的话")
    deploy_to(client, second, admin_headers, asset["id"], [second_player["id"]])

    ctx = app.state.ctx
    with ctx.db.session() as session:
        task = session.execute(
            select(DeployTask).where(DeployTask.contest_id == second["id"])
        ).scalar_one()
        session.add(DeployTarget(task_id=task.id, player_id=player["id"]))

    payload = fetch(client).json()
    assert payload["notice"] is None, "另一场的公告被 contest 过滤漏进了这一场"
    # 脏数据也不该让清单多出一行：清单与公告走的是同一套过滤
    assert payload["assets"] == []


def test_公告文件名大小写不敏感(client, contest, admin_headers, player):
    """教师按约定命名为 NOTICE.md，但写成 Notice.MD 也该认。

    拼写刻意用**大小写混着**的那一种：常量写的是小写 ``notice.md``，所以
    "去掉了 ``.lower()`` 还能不能过"这件事只有混合大小写才咬得住 ——
    拿全小写文件名来测的话，去掉 ``.lower()`` 也会绿。
    而 ``filename`` 要回**实际的那个名字**，不能硬编码成 ``NOTICE.md``：
    页面上写的是"来自 xxx"，写错了学生对不上。
    """
    asset = text_asset(client, contest, admin_headers, "Notice.MD", "大小写混着也认")
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    payload = fetch(client).json()
    assert payload["notice"] == {"filename": "Notice.MD", "content": "大小写混着也认"}


def test_多个公告取最后下发的那个(client, contest, admin_headers, player):
    """同一场给同一个人发了两个公告：取最后下发的（id 最大）并记 warning。

    这是配置失误，但不该让页面空着 —— 选手更需要看到公告。反过来，如果实现
    取的是先下发的那个，"教师发第二份更正"就永远不生效，而现场只会觉得"改了没用"。
    """
    first = text_asset(client, contest, admin_headers, "NOTICE.md", "第一份公告")
    deploy_to(client, contest, admin_headers, first["id"], [player["id"]])
    second = text_asset(client, contest, admin_headers, "notice.md", "第二份公告（更正）")
    deploy_to(client, contest, admin_headers, second["id"], [player["id"]])

    payload = fetch(client).json()
    assert payload["notice"]["content"] == "第二份公告（更正）"
    # 两份都在清单里：公告只是"清单里被认出来的那一个"，不是被摘出去的
    assert sorted(item["filename"] for item in payload["assets"]) == ["NOTICE.md", "notice.md"]


def test_不是_UTF8_的公告被跳过(client, contest, admin_headers, player):
    """一个二进制文件恰好叫 NOTICE.md 时：当它不是公告，页面照常返回。

    跳过而不是 500：免登录的这一页是考场入口，坏文件不该拿全场的时间去换。
    它仍然是普通下发文件，照常出现在清单里。
    """
    asset = upload_asset(client, contest, admin_headers, "NOTICE.md", b"\xff\xfe\x00bad")
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    response = fetch(client)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["notice"] is None
    assert [item["filename"] for item in payload["assets"]] == ["NOTICE.md"]


def test_超过_64KB_的公告被跳过(client, contest, admin_headers, player):
    """大于 64 KB 的不算公告：几十 MB 的东西塞进 JSON 会拖死浏览器，而它不是公告。

    没有上限时这条会红：正文会被原样塞进响应。
    """
    big = b"A" * (64 * 1024 + 1)
    asset = upload_asset(client, contest, admin_headers, "NOTICE.md", big)
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    response = fetch(client)
    assert response.status_code == 200, response.text
    assert response.json()["notice"] is None


def test_正好_64KB_的公告能显示(client, contest, admin_headers, player):
    """上限是"超过 64 KB 才跳过"：正好 64 KB 仍然要显示。

    否则边界写成了 ``>=`` 时，一份刚好卡在上限的公告会静静地不出现，
    而现场只会以为教师没发。
    """
    limit = b"B" * (64 * 1024)
    asset = upload_asset(client, contest, admin_headers, "NOTICE.md", limit)
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    payload = fetch(client).json()
    assert payload["notice"]["content"] == "B" * (64 * 1024)


def test_公告正文去_BOM_并统一换行(client, contest, admin_headers, player):
    """BOM 与 CRLF 是"字节层面的噪声"，要在服务端清掉。

    从 Windows 记事本拖进来的文件常带 BOM / CRLF；落到 Linux 上显示成 ``^M``
    或者开头三个看不见的字节，看起来就是文件坏了。处理方式与「新建文本文件」
    那条入口一致。
    """
    raw = "\ufeff第一行\r\n第二行\r第三行".encode("utf-8")
    asset = upload_asset(client, contest, admin_headers, "NOTICE.md", raw)
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    assert fetch(client).json()["notice"]["content"] == "第一行\n第二行\n第三行"


# --------------------------------------------------------------------------- #
# 3. 免登录与"不多给一个字节"
# --------------------------------------------------------------------------- #


def test_不鉴权(client, contest, player):
    """免登录是这一页存在的前提：带上假凭据也不该变成 401。"""
    assert fetch(client).status_code == 200
    assert fetch(client, headers={"Authorization": "Bearer nonsense"}).status_code == 200


def test_响应里没有代码与成绩字段(client, contest, admin_headers, player):
    """边界靠**响应形状**守住，不靠前端不显示。

    字段集用的是等号：以后谁往模型里加一个 ``files`` / ``score``，
    这条会直接红，而不是让一个多余字段悄悄发到考场的浏览器里。
    """
    asset = upload_asset(client, contest, admin_headers, "题面.zip", b"statement")
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]])

    body = fetch(client).json()
    assert set(body) == {
        "contest",
        "player",
        "notice",
        "assets",
        "matched_by",
        "server_time",
    }
    assert set(body["contest"]) == {"slug", "name", "status", "starts_at", "ends_at"}
    assert set(body["player"]) == {"player_no", "name", "seat", "group_name"}
    # 没下发公告时是 null，而不是一个空壳对象
    assert body["notice"] is None
    assert set(body["assets"][0]) == {
        "filename",
        "dest_dir",
        "status",
        "size",
        "sha256",
        "finished_at",
    }

    forbidden = {"files", "source_files", "submissions", "scores", "judge_runs", "results", "code"}
    assert not (set(body) & forbidden)
    assert isinstance(body["server_time"], int)


def test_页面上能看见开考与结束时间(client, contest, admin_headers, player):
    """选手页要给考试时间窗 —— 那是他自己确认"还收不收卷"的唯一依据。

    到点之后服务端会拒收（409 ``contest_ended``），但选手在此之前就该知道几点结束。
    "留空 = 不限制"在页面上也必须是看得出来的（显示"不限"），不能是一片空白。
    """
    # 没配时间窗：如实回 null
    body = fetch(client).json()
    assert body["contest"]["starts_at"] is None
    assert body["contest"]["ends_at"] is None

    patched = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={"starts_at": "2026-03-01T01:00:00Z", "ends_at": "2026-03-01T03:00:00Z"},
        headers=admin_headers,
    )
    assert patched.status_code == 200, patched.text

    # 配好之后原样出现在选手页上（同一个时刻，不是"服务端本地时间"）
    body = fetch(client).json()
    assert body["contest"]["starts_at"] == "2026-03-01T01:00:00Z"
    assert body["contest"]["ends_at"] == "2026-03-01T03:00:00Z"


def test_服务端时间是真正的_unix_秒(client, contest, player):
    """``utcnow()`` 是 naive UTC，取 epoch 前必须补回 tzinfo。

    直接对 naive 值调 ``.timestamp()`` 会按**本机时区**解释，在东八区的机器上
    整整差 8 小时 —— 而页面上"清单是服务端几点取的"就是靠这个数显示的。
    """
    import time

    body = fetch(client).json()
    assert abs(body["server_time"] - int(time.time())) < 300


# --------------------------------------------------------------------------- #
# 4. 自动匹配本机（主路径）
# --------------------------------------------------------------------------- #


def test_不带参数时按来源_IP_自动匹配本机(
    app, client, contest, admin_headers, bootstrap_key, player, roster_entry
):
    ip = "10.9.0.1"
    ip_client = client_from(app, ip)
    enroll_bound_machine(
        client, ip_client, admin_headers, bootstrap_key, roster_entry["id"]
    )

    asset = upload_asset(client, contest, admin_headers, "题面.zip", b"statement")
    deploy_to(client, contest, admin_headers, asset["id"], [player["id"]], dest_dir="")

    response = ip_client.get(PLAYER_PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matched_by"] == "machine"
    assert body["player"]["player_no"] == "S001"
    assert [item["filename"] for item in body["assets"]] == ["题面.zip"]


def test_来源_IP_认不出机器时是_404_人话(
    app, client, contest, admin_headers, bootstrap_key, player, roster_entry
):
    """库里有一台别人用过的机器时，没见过的 IP 也不能被"顺手认领"。

    如果只造一个空库来测，这条测试在"实现根本不看 IP"时也会绿 ——
    所以这里先让另一台机器在别的 IP 上说一次话。
    """
    used_ip = "10.9.0.240"
    known = client_from(app, used_ip)
    enroll_bound_machine(
        client, known, admin_headers, bootstrap_key, roster_entry["id"]
    )
    assert known.get(PLAYER_PATH).status_code == 200

    stranger = client_from(app, "10.9.0.250")
    response = stranger.get(PLAYER_PATH)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "not_found"
    assert "这台机器还没有注册上来" in body["detail"]
    assert body["details"]["matched_by"] == "machine"


def test_IP_命中但还没配对时是_403(app, client, contest, bootstrap_key):
    ip = "10.9.0.2"
    ip_client = client_from(app, ip)
    machine = enroll_machine(ip_client, bootstrap_key)
    # 未配对的机器也要先 tick 一次：来源 IP 是"上一次请求"写下来的
    tick(ip_client, machine["token"], [], machine_id=machine["machine_id"])

    response = ip_client.get(PLAYER_PATH)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "pairing_required"
    assert "配对" in body["detail"]


def test_IP_命中但配对的人没有场次时是_403(
    app, client, contest, admin_headers, bootstrap_key
):
    """配了人、但那个人不在任何进行中的场次里 —— 直接造这么一台机器。"""
    roster = make_roster(
        client,
        admin_headers,
        name="没有场次的名单",
        entries=[{"player_no": "S900", "name": "没场次"}],
    )
    entry = roster["entries"][0]
    ip = "10.9.0.3"
    ip_client = client_from(app, ip)
    enroll_bound_machine(
        client, ip_client, admin_headers, bootstrap_key, entry["id"], expect_bound=False
    )

    response = ip_client.get(PLAYER_PATH)
    assert response.status_code == 403, response.text
    body = response.json()
    # 与 /tick 用的是同一个 resolve_machine，所以 code 也必须一模一样
    assert body["code"] == "no_active_contest"
    assert "场次" in body["detail"]


def test_同一个_IP_不会拿到别人的清单(
    app, client, contest, admin_headers, bootstrap_key, player, roster
):
    """两台机器、两个 IP、两个人：各自查到的必须是各自的清单。

    这一条打的是"按 IP 认机器"最危险的失败方式：只要实现里少一个"只认这台机器"
    的条件（拿 registry 里第一台机器、忽略 IP、或者把 assets 的 player_id 过滤
    去掉），它就会红 —— 而线上的表现是**一个学生看到另一个学生的文件清单**，
    静默，且没有任何人会发现。
    """
    second = add_players(
        client, admin_headers, contest, [{"player_no": "S002", "name": "李四"}]
    )[0]
    first_entry = next(e for e in roster["entries"] if e["player_no"] == "S001")
    second_entry = next(e for e in roster["entries"] if e["player_no"] == "S002")

    only_first = upload_asset(client, contest, admin_headers, "只给S001.zip", b"one")
    only_second = upload_asset(client, contest, admin_headers, "只给S002.zip", b"two")
    deploy_to(client, contest, admin_headers, only_first["id"], [player["id"]])
    deploy_to(client, contest, admin_headers, only_second["id"], [second["id"]])

    first_ip, second_ip = "10.9.1.1", "10.9.1.2"
    first_client = client_from(app, first_ip)
    second_client = client_from(app, second_ip)
    enroll_bound_machine(
        client, first_client, admin_headers, bootstrap_key, first_entry["id"]
    )
    enroll_bound_machine(
        client, second_client, admin_headers, bootstrap_key, second_entry["id"]
    )

    one = first_client.get(PLAYER_PATH)
    two = second_client.get(PLAYER_PATH)
    assert one.status_code == 200, one.text
    assert two.status_code == 200, two.text

    assert one.json()["player"]["player_no"] == "S001"
    assert [item["filename"] for item in one.json()["assets"]] == ["只给S001.zip"]
    assert two.json()["player"]["player_no"] == "S002"
    assert [item["filename"] for item in two.json()["assets"]] == ["只给S002.zip"]


# --------------------------------------------------------------------------- #
# 5. 迁移：选手页依赖的列在"新库"与"已有的库"上都要在
# --------------------------------------------------------------------------- #


#: 迁移 002 之后、本轮新列之前的结构（``user_version=2``）。
#:
#: 刻意手写而不是从模型生成：从模型生成的话，"模型加了列但忘了写迁移"这件事
#: 永远不会被测出来 —— 生成出来的老结构会跟着模型一起变。
#:
#: **必须是 version=2 而不是 0**：002 会用**当前模型**重建 ``agent``/``contest``，
#: 于是新列会被那次重建顺手带进来，003 之后就成了空操作 ——
#: 测出来的"绿"跟迁移有没有写一点关系都没有。
#:
#: 其余列照抄当时的模型，避免"老库"缺一些本次不该碰的列而把结论搅浑。
LEGACY_V2_SCHEMA = """
CREATE TABLE contest (
    id INTEGER NOT NULL PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    starts_at DATETIME,
    ends_at DATETIME,
    note TEXT,
    default_roster_id INTEGER,
    created_at DATETIME NOT NULL
);
CREATE TABLE agent (
    id INTEGER NOT NULL PRIMARY KEY,
    roster_entry_id INTEGER,
    contest_id INTEGER,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    machine_id VARCHAR(128) NOT NULL,
    machine_uuid VARCHAR(64),
    machine_fingerprint VARCHAR(128),
    pair_code_hash VARCHAR(64),
    pair_code_expires_at DATETIME,
    hostname VARCHAR(128),
    os_info VARCHAR(200),
    agent_version VARCHAR(32),
    enrolled_at DATETIME NOT NULL,
    last_enrolled_at DATETIME NOT NULL,
    last_seen_at DATETIME,
    revoked_at DATETIME,
    claimed_at DATETIME,
    created_at DATETIME NOT NULL
);
PRAGMA user_version=2;
INSERT INTO contest (id, slug, name, status, note, created_at)
    VALUES (1, 'mock-1', '校内模拟赛', 'running', '教师自己的备注', '2026-01-01 00:00:00');
INSERT INTO agent (id, token_hash, machine_id, enrolled_at, last_enrolled_at, created_at)
    VALUES (1, 'hash-a', 'machine-1', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
            '2026-01-01 00:00:00');
"""


def make_engine(path: Path):
    return create_engine("sqlite+pysqlite:///" + str(path).replace("\\", "/"), future=True)


def columns(engine, table) -> set:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def rows(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def test_新库直接建出选手页要用的列(workdir: Path):
    engine = make_engine(workdir / "fresh.db")
    migrations.migrate(engine)

    assert migrations.current_version(engine) == migrations.LATEST_VERSION
    # "自动匹配本机"靠它认机器（见 §5.6）；少了这一列，选手页会 500
    assert "last_seen_ip" in columns(engine, "agent")
    # ``contest.player_notice`` 随这一轮改动被删掉：新库里不该再有它
    assert "player_notice" not in columns(engine, "contest")
    engine.dispose()


def test_已有的_dev_库会自动补上选手页要用的列(workdir: Path):
    """这是整份文件里最像"线上会出事"的那一条。

    开发机上这个库已经停在 version=2 了：新列在模型里、在新库里都有，
    唯独它没有 —— 而线上跑的正是"升级上来"的那一条路径。
    漏掉迁移的表现是选手页在那个库上直接 500（``no such column``），
    新库却一切正常。
    """
    engine = make_engine(workdir / "dev.db")
    with engine.begin() as conn:
        for statement in LEGACY_V2_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))

    applied = migrations.migrate(engine)

    assert applied, "version=2 的老库应当有新迁移要跑"
    assert "last_seen_ip" in columns(engine, "agent")
    # 这轮把 ``contest.player_notice`` 也一起收掉了：升级路径必须把老库上的这一列
    # 也去掉，否则"新库没有、老库还带着"会让两条路径的结构分叉（下一条测试会红）
    assert "player_notice" not in columns(engine, "contest")
    # 加列不能动数据：老行的每一个字段都要原样在
    assert rows(engine, "SELECT slug, note FROM contest") == [("mock-1", "教师自己的备注")]
    assert rows(engine, "SELECT token_hash, machine_id FROM agent") == [("hash-a", "machine-1")]
    # 已有的库默认值：NULL 表示"还没说过话"，不是空串
    assert rows(engine, "SELECT last_seen_ip FROM agent") == [(None,)]

    # 第二次是空操作 —— 迁移必须幂等，否则每次启动都会重跑一遍
    assert migrations.migrate(engine) == []
    engine.dispose()


def test_升级库与全新库在两张表上结构一致(workdir: Path):
    """两条路径必须收敛到同一个结构。

    只测全新库是最常见的自欺：线上跑的永远是"老库升上来"的那一条。
    """
    fresh = make_engine(workdir / "fresh.db")
    migrations.migrate(fresh)

    upgraded = make_engine(workdir / "dev.db")
    with upgraded.begin() as conn:
        for statement in LEGACY_V2_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
    migrations.migrate(upgraded)

    for table in ("contest", "agent"):
        assert sorted(columns(fresh, table)) == sorted(columns(upgraded, table)), table

    fresh.dispose()
    upgraded.dispose()
