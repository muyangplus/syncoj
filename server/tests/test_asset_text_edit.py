"""在线修改纯文本资产的正文。

下发资产是**内容寻址**的（一行只有 ``sha256`` + ``size``，blob 不可变），所以
"在线改正文" = 用**同一个 asset id** 换掉它的 ``sha256``/``size``。这一层要守的
判据比看上去多，每条都有具体的现场后果：

1. **改完是新的那一份**：sha256/size 变了、正文是新的、**文件名没变**（文件名
   参与机器上的落地路径，它不该被内容编辑顺手改掉）。
2. **不能改的必须拦住，而且一个字节都不能动**：二进制扩展名 / 解不出 UTF-8 /
   超过体积上限。拦住不难，难的是"拦在动手之前" —— 先写后验的话，一份不该被
   碰的资产会被改掉一半才发现不对，而原始字节拿不回来。
3. **已经下发出去的目标要重排**（``done`` → ``pending``）且**续传偏移清零**。
   这是整条链路上唯一会"静默说谎"的地方：tick 按当前 ``asset.sha256`` 组装 job、
   只取 ``pending/ready`` 的目标，所以不重排的话选手页显示新正文、机器上躺着的
   还是旧文件。偏移不清零则更糟 —— 拿旧文件的偏移去续新文件，落出来的是坏文件。
4. **跨场次越权必须是 404**：只按 asset_id 取的话，别场的 id 也能读、能改。
5. **审计**：改的是已经发出去的东西，事后必须查得到"哪个场次、哪个文件、
   从哪一版到哪一版、影响了几台"。
6. **归一规则与「新建文本文件」一致**：去 BOM、``\\r\\n``/``\\r`` → ``\\n``。
7. **选手页跟着变**：公告就是一份走"文件下发"发下去的 ``NOTICE.md``，改完正文
   之后选手页必须立刻显示新的那一份 —— 这条把两个功能串起来。

关于 ``bytes_done``：它在服务端**没有任何写入路径**（``agent.py`` 只用客户端上报的
``partials`` 算 offset，这个字段是留给界面看的镜像）。所以"重排时清零"这条断言
必须从一个**我们亲手设的**非零值出发，否则 0 → 0 永远测不出问题。
"""

from __future__ import annotations

from typing import List, Tuple

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.models import Asset, DeployStatus, DeployTarget, DeployTask, EventLog

from conftest import do_tick

ASSETS_URL = "/api/v1/admin/contests/%d/assets"
TEXT_URL = "/api/v1/admin/contests/%d/assets/text"
EDIT_URL = "/api/v1/admin/contests/%d/assets/%d/text"
PLAYER_PATH = "/api/v1/player/context"

ESSAY = "须知.txt"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def create_text(
    client: TestClient,
    headers: dict,
    contest_id: int,
    *,
    filename: str = ESSAY,
    content: str = "旧正文\n",
    kind: str = "testdata",
) -> dict:
    response = client.post(
        TEXT_URL % contest_id,
        json={"filename": filename, "content": content, "kind": kind},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload(client: TestClient, headers: dict, contest_id: int, filename: str, data: bytes) -> dict:
    response = client.post(
        ASSETS_URL % contest_id,
        files={"file": (filename, data, "application/octet-stream")},
        data={"kind": "testdata"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def read_text(client: TestClient, headers: dict, contest_id: int, asset_id: int):
    return client.get(EDIT_URL % (contest_id, asset_id), headers=headers)


def save_text(client: TestClient, headers: dict, contest_id: int, asset_id: int, content: str):
    return client.put(
        EDIT_URL % (contest_id, asset_id), json={"content": content}, headers=headers
    )


def stored(client: TestClient, asset_id: int) -> Tuple[str, int, str]:
    """库上那一行：``(sha256, size, filename)``。

    "一个字节都没动"这类断言必须打在**库**上 —— 接口回显只能证明服务端**说**它
    存了什么。
    """
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        row = session.get(Asset, asset_id)
        assert row is not None
        return row.sha256, int(row.size), row.filename


def blob_bytes(client: TestClient, sha256: str) -> bytes:
    ctx = client.app.state.ctx
    with ctx.blobs.open(sha256) as handle:
        return handle.read()


def listed_asset(client: TestClient, headers: dict, contest_id: int, asset_id: int) -> dict:
    page = client.get(ASSETS_URL % contest_id, headers=headers)
    assert page.status_code == 200, page.text
    return next(item for item in page.json()["items"] if item["id"] == asset_id)


def deploy_to(
    client: TestClient, headers: dict, contest_id: int, asset_id: int, player_ids: List[int]
) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/deploys" % contest_id,
        json={
            "asset_id": asset_id,
            "target_kind": "player",
            "player_ids": player_ids,
            "dest_dir": "",
            "mode": "overwrite",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def add_player(client: TestClient, headers: dict, contest_id: int, player_no: str) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest_id,
        json=[{"player_no": player_no}],
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["players"][0]


def _target_rows(session, asset_id: int) -> List[DeployTarget]:
    return list(
        session.execute(
            select(DeployTarget)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(DeployTask.asset_id == asset_id)
            .order_by(DeployTarget.id)
        ).scalars()
    )


def target_states(client: TestClient, asset_id: int) -> List[Tuple[str, int]]:
    """每个目标的 ``(status, bytes_done)``。

    只把简单值带出会话：ORM 行一旦离开 session，读属性就可能触发一次已经关掉的
    懒加载。
    """
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        return [(t.status, int(t.bytes_done)) for t in _target_rows(session, asset_id)]


def pin_bytes_done(client: TestClient, asset_id: int, value: int) -> None:
    """把一个目标的续传偏移钉成非零值（理由见模块说明）。"""
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        for target in _target_rows(session, asset_id):
            target.bytes_done = value


def audit_events(client: TestClient) -> List[dict]:
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        rows = session.execute(
            select(EventLog)
            .where(EventLog.category == "asset_text_edited")
            .order_by(EventLog.id)
        ).scalars()
        return [
            {
                "level": row.level,
                "category": row.category,
                "contest_id": row.contest_id,
                "message": row.message,
            }
            for row in rows
        ]


def fetch_player(client: TestClient, contest_slug: str, player_no: str):
    return client.get(
        PLAYER_PATH, params={"contest": contest_slug, "player_no": player_no}
    )


# --------------------------------------------------------------------------- #
# 1. 改完就是新的一份
# --------------------------------------------------------------------------- #


def test_改完正文_sha_与_size_都变了_文件名没变(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = create_text(client, admin_headers, contest["id"], content="第一版\n")
    before = stored(client, asset["id"])

    response = save_text(client, admin_headers, contest["id"], asset["id"], "第二版\n内容\n")

    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["requeued"] == 0, "一个目标都没有下发过，没有机器需要重排"
    assert saved["asset"]["id"] == asset["id"], "改内容不是新建一条资产"
    assert saved["asset"]["filename"] == ESSAY, "内容编辑不该动文件名"

    expected = "第二版\n内容\n".encode("utf-8")
    assert saved["asset"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert saved["asset"]["size"] == len(expected)

    after = stored(client, asset["id"])
    assert after[0] != before[0], "sha256 必须换成新的那一份"
    assert after[1] == len(expected)
    assert after[2] == ESSAY, "库里的文件名也不能动"
    assert blob_bytes(client, after[0]) == expected, "落盘的就是教师写的那份字节"

    read = read_text(client, admin_headers, contest["id"], asset["id"])
    assert read.status_code == 200, read.text
    assert read.json() == {"filename": ESSAY, "content": "第二版\n内容\n"}


def test_列表页的_editable_只看元数据(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """`editable` 是界面上"给不给编辑按钮"的唯一依据，由服务端算。

    一个小的 `.txt` 该能改；`.zip` 与超大的 `.txt` 不该。这一条同时钉住"列表
    不许为此打开 blob"：这里放一份**内容很正常的** `.txt`，只要判据是纯元数据的，
    列表就不会因为它而去读盘。
    """
    editable = create_text(client, admin_headers, contest["id"], filename="说明.txt")
    archive = upload(client, admin_headers, contest["id"], "testdata.zip", b"PK\x03\x04not really")
    huge = upload(client, admin_headers, contest["id"], "大.txt", b"a" * (1100 * 1024))

    assert listed_asset(client, admin_headers, contest["id"], editable["id"])["editable"] is True
    assert listed_asset(client, admin_headers, contest["id"], archive["id"])["editable"] is False
    assert listed_asset(client, admin_headers, contest["id"], huge["id"])["editable"] is False


# --------------------------------------------------------------------------- #
# 2. 不能改的：400，而且一个字节都没动
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "filename, data, expected_code, expected_editable",
    [
        # 白名单外的扩展名，但内容是**合法 UTF-8**。这一点是刻意的：判据一旦被
        # 放宽成"任意文件"，这一条会因为内容能解出来而放行 —— 测试必须因此变红。
        ("题面.zip", "看起来像文本的 ascii\n".encode("utf-8"), "asset_not_editable", False),
        # 扩展名过关，但字节根本不是 UTF-8。列表上它仍然是 editable=True ——
        # 这不是漏判，而是这条判据的**设计边界**：列表只看元数据、不打开 blob，
        # 所以"这一份到底解不解得出来"只有 GET/PUT 那一刻才知道。真判必须是 400。
        ("坏.txt", b"\xff\xfe\x00\x01raw\x00bytes", "asset_not_utf8", True),
        # 扩展名与编码都过关，只是超过体积上限（上限 1 MB：**比新建那条路的
        # 200000 字符宽**，否则"刚写好的公告回头改不了"）
        ("大.txt", b"a" * (1100 * 1024), "asset_not_editable", False),
    ],
    # 显式给 id：第三个用例带 1 MB 的字节串，让它自动生成 id 会把整份
    # pytest 输出淹在 110 万个 a 里（真实的失败信息反而看不见）。
    ids=["扩展名不在白名单", "字节不是_UTF8", "超过体积上限"],
)
def test_不满足判据的资产_读写都_400_且库与_blob_都没动(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    filename: str,
    data: bytes,
    expected_code: str,
    expected_editable: bool,
) -> None:
    asset = upload(client, admin_headers, contest["id"], filename, data)
    before = stored(client, asset["id"])
    assert (
        listed_asset(client, admin_headers, contest["id"], asset["id"])["editable"]
        is expected_editable
    )

    read = read_text(client, admin_headers, contest["id"], asset["id"])
    assert read.status_code == 400, read.text
    assert read.json()["code"] == expected_code
    assert read.json()["detail"], "detail 必须是一句能直接显示给人看的话"

    write = save_text(client, admin_headers, contest["id"], asset["id"], "改一下\n")
    assert write.status_code == 400, write.text
    assert write.json()["code"] == expected_code

    # 两件事都要断言：库上的 sha256/size 没动，**原始 blob 也还在**
    assert stored(client, asset["id"]) == before
    assert blob_bytes(client, before[0]) == data


def test_保存时的新正文也要过_UTF8_那道关(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """请求体里的 ``content`` 是 JSON 字符串，pydantic 已经保证它是合法 UTF-8，
    所以这里能测的是"它被原样存下来了"。留这条是为了说清：PUT 的 400 只会来自
    **服务端手上那一份**，不会来自请求体。"""
    asset = create_text(client, admin_headers, contest["id"])
    text = "中文 / emoji 🙂 / 制表符\t结束\n"

    saved = save_text(client, admin_headers, contest["id"], asset["id"], text)

    assert saved.status_code == 200, saved.text
    assert blob_bytes(client, saved.json()["asset"]["sha256"]) == text.encode("utf-8")


# --------------------------------------------------------------------------- #
# 3. 重排已下发的目标，并把续传偏移清零
# --------------------------------------------------------------------------- #


def test_已下发的目标被重排且偏移清零(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    player: dict,
    enrolled: dict,
) -> None:
    asset = create_text(client, admin_headers, contest["id"], content="第一版\n")
    deploy_to(client, admin_headers, contest["id"], asset["id"], [player["id"]])

    # 走**真实的** Agent 上报路径把这一条目标标成 done，而不是往库里塞状态：
    # "已经下发出去"这件事必须真的发生过一次。
    do_tick(
        client,
        enrolled["token"],
        [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )
    assert target_states(client, asset["id"]) == [(DeployStatus.DONE, 0)]

    # 非零偏移：服务端没有任何路径会写这个字段，不亲手钉就没有东西可清零
    pin_bytes_done(client, asset["id"], 64 * 1024)

    saved = save_text(client, admin_headers, contest["id"], asset["id"], "第二版\n")
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["requeued"] == 1

    status, offset = target_states(client, asset["id"])[0]
    assert status == DeployStatus.PENDING, "已经落地的机器必须重新领一次"
    assert offset == 0, "旧文件的偏移对新文件毫无意义，必须清零"

    # 下一轮 tick 必须真的把这个作业发出去，而且带着**新**的 sha256
    jobs = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])[
        "deploy_jobs"
    ]
    job = next(j for j in jobs if j["asset_id"] == asset["id"])
    assert job["sha256"] == body["asset"]["sha256"]
    assert job["size"] == body["asset"]["size"]
    assert job["offset"] == 0


def test_还没落地的目标不被重排也不被清零(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    player: dict,
    enrolled: dict,
) -> None:
    """只有 ``done`` 的要重排。``pending`` 的那条本来就等着领作业，动它没有好处 ——
    把它顺手清零等于让一台下到一半的机器从头发一遍。"""
    other = add_player(client, admin_headers, contest["id"], "S002")
    asset = create_text(client, admin_headers, contest["id"], content="第一版\n")
    deploy_to(
        client, admin_headers, contest["id"], asset["id"], [player["id"], other["id"]]
    )

    do_tick(
        client,
        enrolled["token"],
        [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )
    pin_bytes_done(client, asset["id"], 4096)
    before = target_states(client, asset["id"])
    assert sorted(status for status, _ in before) == [DeployStatus.DONE, DeployStatus.PENDING]

    saved = save_text(client, admin_headers, contest["id"], asset["id"], "第二版\n")

    assert saved.json()["requeued"] == 1, "只有一条 done，重排数就该是 1"
    after = target_states(client, asset["id"])
    assert sorted(status for status, _ in after) == [DeployStatus.PENDING, DeployStatus.PENDING]
    assert sorted(offset for _, offset in after) == [0, 4096], (
        "还没落地那条的偏移不该被动过（它可能是另一半内容的下到一半）"
    )


def test_没有下发过的资产_requeued_是_0(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = create_text(client, admin_headers, contest["id"])

    saved = save_text(client, admin_headers, contest["id"], asset["id"], "新正文\n")

    assert saved.status_code == 200, saved.text
    assert saved.json()["requeued"] == 0


# --------------------------------------------------------------------------- #
# 4. 跨场次 / 不存在的资产
# --------------------------------------------------------------------------- #


def test_别的场次的资产_id_是_404(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = create_text(client, admin_headers, contest["id"], content="第一版\n")
    before = stored(client, asset["id"])
    other = client.post(
        "/api/v1/admin/contests",
        json={"name": "另一场", "slug": "other-1", "status": "running"},
        headers=admin_headers,
    ).json()

    assert read_text(client, admin_headers, other["id"], asset["id"]).status_code == 404
    assert (
        save_text(client, admin_headers, other["id"], asset["id"], "偷改\n").status_code
        == 404
    )
    assert stored(client, asset["id"]) == before, "越权那一次不该碰到任何东西"

    # 压根不存在的 id 也是 404，不是 500、更不是"悄悄建一个新的"
    assert read_text(client, admin_headers, contest["id"], 999999).status_code == 404
    assert save_text(client, admin_headers, contest["id"], 999999, "x\n").status_code == 404


# --------------------------------------------------------------------------- #
# 5. 审计
# --------------------------------------------------------------------------- #


def test_审计事件写清了场次文件与前后版本(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = create_text(client, admin_headers, contest["id"], content="第一版\n")
    old_sha = stored(client, asset["id"])[0]

    saved = save_text(client, admin_headers, contest["id"], asset["id"], "第二版\n").json()

    events = audit_events(client)
    assert len(events) == 1
    event = events[0]
    assert event["level"] == "info"
    assert event["category"] == "asset_text_edited"
    assert event["contest_id"] == contest["id"]
    assert ESSAY in event["message"], "必须说清是哪个文件"
    assert old_sha[:8] in event["message"] and saved["asset"]["sha256"][:8] in event["message"], (
        "必须说清改前改后的 sha，否则事后没法判断改动落在了哪一版"
    )
    assert "重新排队 0 台" in event["message"], "重新排队的台数也要在里面"


# --------------------------------------------------------------------------- #
# 6. 归一：BOM 与换行
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("\ufeffA\r\nB\rC\n", "A\nB\nC\n"),   # 记事本 BOM + 三种换行混在一起
        ("A\r\nB\r\n", "A\nB\n"),             # 从 Windows 记事本粘过来
        ("A\rB", "A\nB"),                     # 老 Mac 换行
        ("A\nB", "A\nB"),                     # 本来就对，别动它
        ("A\n\n\n", "A\n\n\n"),               # 结尾的空行是内容的一部分，不许裁
    ],
)
def test_保存时按新建文本那条规则归一(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    raw: str,
    expected: str,
) -> None:
    asset = create_text(client, admin_headers, contest["id"])

    saved = save_text(client, admin_headers, contest["id"], asset["id"], raw)

    assert saved.status_code == 200, saved.text
    assert saved.json()["asset"]["sha256"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert blob_bytes(client, saved.json()["asset"]["sha256"]) == expected.encode("utf-8")
    assert read_text(client, admin_headers, contest["id"], asset["id"]).json()["content"] == expected


def test_打开再原样保存是幂等的(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """一份 CRLF 文件读进来要先归一，否则每被打开保存一次 sha 就变一次 ——
    而那会把已经下发的机器全部重排一遍（"我什么都没改，机器怎么又收了一遍"）。"""
    asset = upload(client, admin_headers, contest["id"], "crlf.txt", b"A\r\nB\r\n")

    opened = read_text(client, admin_headers, contest["id"], asset["id"])
    assert opened.json()["content"] == "A\nB\n", "GET 就要归一，否则对话框里显示的是另一份东西"

    first = save_text(
        client, admin_headers, contest["id"], asset["id"], opened.json()["content"]
    ).json()
    second = save_text(
        client, admin_headers, contest["id"], asset["id"], opened.json()["content"]
    ).json()

    assert first["asset"]["sha256"] == second["asset"]["sha256"]


# --------------------------------------------------------------------------- #
# 7. 选手页跟着变（公告就是一份下发文件）
# --------------------------------------------------------------------------- #


def test_改完之后选手页的公告正文跟着变(
    client: TestClient, admin_headers: dict, contest: dict, player: dict
) -> None:
    asset = create_text(
        client, admin_headers, contest["id"], filename="NOTICE.md", content="旧公告\n"
    )
    deploy_to(client, admin_headers, contest["id"], asset["id"], [player["id"]])

    before = fetch_player(client, contest["slug"], player["player_no"])
    assert before.status_code == 200, before.text
    assert before.json()["notice"] == {"filename": "NOTICE.md", "content": "旧公告\n"}

    saved = save_text(client, admin_headers, contest["id"], asset["id"], "新公告\n")
    assert saved.status_code == 200, saved.text

    after = fetch_player(client, contest["slug"], player["player_no"])
    assert after.status_code == 200, after.text
    assert after.json()["notice"] == {"filename": "NOTICE.md", "content": "新公告\n"}
