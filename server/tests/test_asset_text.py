"""新建文本资产：在界面里写一段话，直接当成下发文件。

这条入口的价值全在"教师想说的那句话本来就是在浏览器里打的"。所以测试盯的是
**"界面里写的"和"落盘的"逐字节一致**，以及几条会让人白忙一场的边界：

* 换行要统一成 LF、BOM 要去掉 —— 目标机是 Linux，留着 `\\r` 会显示成 `^M`，
  BOM 会变成开头三个看不见的字节，两种都像"文件坏了"。
* 二进制扩展名要拦住 —— 用文本入口发一个 zip，只会造出一个打不开的文件，
  而现场看到的是"发下去了但学生打不开"。
* 与上传共用去重：同场次 + 同名 + 同内容只该有一条资产，否则界面上会变成
  "我明明只发了一次"。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEXT_URL = "/api/v1/admin/contests/%d/assets/text"


def create(client: TestClient, headers: dict, contest_id: int, **payload) -> dict:
    body = {"filename": "须知.txt", "content": "把代码存到桌面/考号/题目名/题目名.cpp\n"}
    body.update(payload)
    return client.post(TEXT_URL % contest_id, json=body, headers=headers)


def asset_content(client: TestClient, headers: dict, asset: dict) -> bytes:
    """按 sha256 从插件库把内容取出来 —— 断言落到磁盘上的字节，而不是接口回显。

    接口回显只能证明"服务端说它存了什么"，而这套东西的价值恰恰在"发给学生的那份
    就是教师写的那份"。所以直接读 blob。
    """
    from syncoj_server.models import Asset

    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        row = session.get(Asset, asset["id"])
        assert row is not None
        with ctx.blobs.open(row.sha256) as handle:
            return handle.read()


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_写一段文本就得到一个资产(client: TestClient, admin_headers: dict, contest: dict) -> None:
    response = create(client, admin_headers, contest["id"])

    assert response.status_code == 200, response.text
    asset = response.json()
    assert asset["filename"] == "须知.txt"
    assert asset["size"] == len("把代码存到桌面/考号/题目名/题目名.cpp\n".encode("utf-8"))
    assert asset_content(client, admin_headers, asset) == (
        "把代码存到桌面/考号/题目名/题目名.cpp\n".encode("utf-8")
    )


def test_内容与_sha256_一致(client: TestClient, admin_headers: dict, contest: dict) -> None:
    """sha256 是内容寻址的键：它错了，去重和"这份文件是不是那份"全都跟着错。"""
    text = "注意事项：开考前把手机放到讲台。\n"
    asset = create(client, admin_headers, contest["id"], content=text).json()

    assert asset["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_能直接下发出去(client: TestClient, admin_headers: dict, contest: dict, player: dict) -> None:
    """发完之后它和上传的资产**完全一样** —— 所以下游的下发流程一行都不用改。"""
    asset = create(client, admin_headers, contest["id"]).json()

    deployed = client.post(
        "/api/v1/admin/contests/%d/deploys" % contest["id"],
        json={
            "asset_id": asset["id"],
            "dest_dir": "桌面",
            "target_kind": "all",
            "mode": "overwrite",
        },
        headers=admin_headers,
    )

    assert deployed.status_code == 200, deployed.text
    body = deployed.json()
    assert body["asset_id"] == asset["id"]
    assert body["filename"] == "须知.txt", "落地用的名字就是资产名"


def test_空内容也允许(client: TestClient, admin_headers: dict, contest: dict) -> None:
    """一个空文件是合法的（比如"占位，见黑板"），不该因此报错。"""
    response = create(client, admin_headers, contest["id"], content="")

    assert response.status_code == 200
    assert response.json()["size"] == 0


# --------------------------------------------------------------------------- #
# 逐字节一致：换行与 BOM
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("A\r\nB\r\n", "A\nB\n"),          # 从 Windows 记事本粘过来
        ("A\rB", "A\nB"),                   # 老 Mac 换行
        ("A\nB", "A\nB"),                   # 本来就对，别动它
        ("\ufeffA\n", "A\n"),               # 记事本的 BOM
        ("A\n\n\n", "A\n\n\n"),             # 结尾的空行是内容的一部分，不许裁
    ],
)
def test_换行与_BOM_被归一化(
    client: TestClient, admin_headers: dict, contest: dict, raw: str, expected: str
) -> None:
    """目标机是 Linux：留着 ``\\r`` 会在选手编辑器里显示成 ``^M``，BOM 会变成
    开头三个看不见的字节 —— 两种都像"文件坏了"，而教师照着界面看是看不出来的。
    """
    asset = create(client, admin_headers, contest["id"], content=raw).json()

    assert asset_content(client, admin_headers, asset) == expected.encode("utf-8")


# --------------------------------------------------------------------------- #
# 拦住"用错入口"
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["题面.zip", "样例.tar.gz", "testdata.exe", "图.png", "a.pdf"])
def test_二进制扩展名被拦住并指向上传(
    client: TestClient, admin_headers: dict, contest: dict, name: str
) -> None:
    """否则会造出一个打不开的文件，而现场看到的是"发下去了但学生打不开"。"""
    response = create(client, admin_headers, contest["id"], filename=name)

    assert response.status_code == 400
    assert "上传" in response.json()["detail"]


@pytest.mark.parametrize("name", ["", "  ", "../evil.txt", "a/b.txt", ".", ".."])
def test_文件名不合法被拦住(
    client: TestClient, admin_headers: dict, contest: dict, name: str
) -> None:
    """文件名会参与 Agent 侧的落地路径，所以按路径段的规则校验。"""
    response = create(client, admin_headers, contest["id"], filename=name)

    assert response.status_code in (400, 422), response.text


def test_没有场次就拒绝(client: TestClient, admin_headers: dict) -> None:
    response = create(client, admin_headers, 99999)

    assert response.status_code == 404
    assert "场次" in response.json()["detail"]


def test_要管理员(client: TestClient, contest: dict) -> None:
    """下发内容给学生是管理动作。"""
    assert create(client, {}, contest["id"]).status_code == 401


# --------------------------------------------------------------------------- #
# 与上传共用去重
# --------------------------------------------------------------------------- #


def test_同名同内容只留一条(client: TestClient, admin_headers: dict, contest: dict) -> None:
    """各写一遍去重的话，一条会去重、另一条会造两份 —— 界面上变成"我明明只发了一次"。"""
    first = create(client, admin_headers, contest["id"]).json()
    second = create(client, admin_headers, contest["id"]).json()

    assert first["id"] == second["id"]

    listed = client.get(
        "/api/v1/admin/contests/%d/assets" % contest["id"], headers=admin_headers
    ).json()
    assert listed["total"] == 1


def test_内容改了就是另一条(client: TestClient, admin_headers: dict, contest: dict) -> None:
    """改了内容就是另一份资产 —— 这是"改名不改内容、改内容就是新版本"的体现。"""
    first = create(client, admin_headers, contest["id"], content="第一版\n").json()
    second = create(client, admin_headers, contest["id"], content="第二版\n").json()

    assert first["id"] != second["id"]


def test_文本与上传撞在一起也算同一条(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """教师先用记事本写了一版、又在界面里写了同样内容：不该变成两份。

    这要求两条创建路共用同一段去重逻辑（`_create_asset`），而不是各写一遍。
    """
    text = "同一句话\n"
    typed = create(client, admin_headers, contest["id"], content=text).json()
    uploaded = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": ("须知.txt", text.encode("utf-8"), "text/plain")},
        data={"kind": "testdata"},
        headers=admin_headers,
    ).json()

    assert typed["id"] == uploaded["id"]
    assert typed["sha256"] == uploaded["sha256"]
