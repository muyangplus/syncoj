"""回收的门：时间窗之外不对账、上传只收"正在等上传"的、请求体在读之前就限长。

审计 H5 / H7：

* 时间窗外 tick 照常对账、照样要上传，只在上传端点 409 —— 开考前每台机器每
  2 秒把全部文件整包传一遍再被拒（multipart 在进处理函数前已经整包收下），
  结束之后的修改还会改写台账；
* 上传端点不看台账：选手账号读得到 token，就能绕过对账与题目规则往 source/
  写任意路径；
* ``max_upload_request_bytes`` 只是配置里的一个数，没有任何代码读它。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from conftest import agent_headers, do_tick as tick, scan_entry as entry, sha256_of
from syncoj_server.models import Contest, SourceFile, utcnow

PATH = "p1/p1.cpp"
CODE = b"int main(){return 0;}\n"


def upload(client, token: str, path: str, content: bytes):
    return client.post(
        "/api/v1/agent/files",
        data={"path": path, "sha256": sha256_of(content)},
        files={"file": (Path(path).name, content, "application/octet-stream")},
        headers=agent_headers(token),
    )


def set_window(app, *, starts_in: timedelta) -> None:
    with app.state.ctx.db.session() as session:
        contest = session.execute(select(Contest)).scalars().first()
        contest.starts_at = utcnow() + starts_in
        contest.ends_at = None


def blob_count(settings) -> int:
    root = Path(settings.blob_root)
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0


# --------------------------------------------------------------------------- #
# 时间窗之外：不对账、不要求上传、空闲节奏
# --------------------------------------------------------------------------- #


def test_开考前的_tick_不对账也不要求上传(client, enrolled, app) -> None:
    set_window(app, starts_in=timedelta(hours=1))

    body = tick(client, enrolled["token"], [entry(PATH, CODE)], machine_id=enrolled["machine_id"])

    assert body["need_upload"] == []
    with app.state.ctx.db.session() as session:
        assert session.execute(select(SourceFile)).first() is None
    # 没活干：走空闲节奏（出厂 30 秒），而不是"有活"的 2 秒
    assert body["next_tick_seconds"] == 30


def test_开考之后照常对账(client, enrolled, app) -> None:
    set_window(app, starts_in=timedelta(hours=-1))

    body = tick(client, enrolled["token"], [entry(PATH, CODE)], machine_id=enrolled["machine_id"])

    assert body["need_upload"] == [PATH]


# --------------------------------------------------------------------------- #
# 上传只收台账里正在等的
# --------------------------------------------------------------------------- #


def test_没被_tick_要过的路径拒收_而且一个字节都不落盘(client, enrolled, settings) -> None:
    before = blob_count(settings)

    response = upload(client, enrolled["token"], "../whatever/x.cpp".replace("../", ""), CODE)

    assert response.status_code == 409
    assert response.json()["code"] == "upload_not_requested"
    assert blob_count(settings) == before, "不该收的内容不该写进 blobs/"
    assert not (Path(settings.source_root) / "mock-1" / "S001" / "whatever").exists()


def test_同一份内容重传是幂等的(client, enrolled, app) -> None:
    """上一次提交成功、响应却丢了，Agent 重试：成功，不加版本号。"""
    tick(client, enrolled["token"], [entry(PATH, CODE)], machine_id=enrolled["machine_id"])
    first = upload(client, enrolled["token"], PATH, CODE)
    again = upload(client, enrolled["token"], PATH, CODE)

    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["revision"] == first.json()["revision"] == 1


# --------------------------------------------------------------------------- #
# 请求体在读之前限长
# --------------------------------------------------------------------------- #


def test_上传请求体超过上限在读之前就被挡(client, enrolled, app) -> None:
    app.state.ctx.settings.max_upload_request_bytes = 1024
    response = client.post(
        "/api/v1/agent/files",
        content=b"x" * 4096,
        headers=dict(agent_headers(enrolled["token"]), **{"Content-Type": "multipart/form-data; boundary=b"}),
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_tick_请求体超过上限在读之前就被挡(client, enrolled, app) -> None:
    app.state.ctx.settings.max_tick_request_bytes = 1024
    response = client.post(
        "/api/v1/agent/tick",
        content=b"{" + b" " * 4096 + b"}",
        headers=dict(agent_headers(enrolled["token"]), **{"Content-Type": "application/json"}),
    )
    assert response.status_code == 413


def test_不带_content_length_的上传拒收(client, enrolled) -> None:
    def chunks():
        yield b"--b\r\n"

    response = client.post(
        "/api/v1/agent/files",
        content=chunks(),
        headers=dict(agent_headers(enrolled["token"]), **{"Content-Type": "multipart/form-data; boundary=b"}),
    )
    assert response.status_code == 411
