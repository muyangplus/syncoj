"""代码台账："看到的"与"存下的"分开记（``SourceFile.scanned_sha256``）。

曾经只有一列 ``sha256``，tick 在上传**之前**就把它改成新扫描值、却不复位
``content_stored``。于是改过的文件只要那一轮没传上（断网、看门狗、Agent 重启、
need_upload 被截断、时间窗 409），下一轮就被判成"没变、已存"，再也不要求上传：
``source/`` 停在旧版本，台账却显示新指纹 —— 最后几次保存静默丢失，界面上看不出来。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select, text

from conftest import agent_headers, do_tick as tick, scan_entry as entry, sha256_of
from syncoj_server.migrations import migrate
from syncoj_server.models import SourceFile

V1 = b"int main(){return 1;}\n"
V2 = b"int main(){return 2;}\n"
V3 = b"int main(){return 3;}\n"
PATH = "p1/p1.cpp"


def upload(client, token: str, path: str, content: bytes):
    return client.post(
        "/api/v1/agent/files",
        data={"path": path, "sha256": sha256_of(content)},
        files={"file": (Path(path).name, content, "application/octet-stream")},
        headers=agent_headers(token),
    )


def source_file(settings, path: str = PATH) -> Path:
    return Path(settings.source_root) / "mock-1" / "S001" / path


def ledger_row(app, path: str = PATH) -> SourceFile:
    with app.state.ctx.db.session() as session:
        row = session.execute(select(SourceFile).where(SourceFile.rel_path == path)).scalar_one()
        session.expunge(row)
        return row


def test_没传上的新版本下一轮仍然要求上传(client, enrolled, settings, app) -> None:
    token, mid = enrolled["token"], enrolled["machine_id"]
    assert tick(client, token, [entry(PATH, V1)], machine_id=mid)["need_upload"] == [PATH]
    assert upload(client, token, PATH, V1).status_code == 200

    changed = [entry(PATH, V2, mtime=1767225600)]
    assert tick(client, token, changed, machine_id=mid)["need_upload"] == [PATH]
    # 这一轮没传上（断网 / 看门狗 / 进程被杀）……
    body = tick(client, token, changed, machine_id=mid)
    # ……下一轮必须照样要
    assert body["need_upload"] == [PATH], "新版本只因一轮没传上就再也不被要求 —— 代码丢失"

    row = ledger_row(app)
    assert row.sha256 == sha256_of(V1), "已存的那一版不该被扫描值改写"
    assert row.scanned_sha256 == sha256_of(V2)
    assert source_file(settings).read_bytes() == V1

    assert upload(client, token, PATH, V2).status_code == 200
    assert tick(client, token, changed, machine_id=mid)["need_upload"] == []
    assert source_file(settings).read_bytes() == V2
    assert ledger_row(app).revision == 2


def test_迟到的旧内容被拒_只收最近一次扫描到的那一版(client, enrolled, settings) -> None:
    token, mid = enrolled["token"], enrolled["machine_id"]
    tick(client, token, [entry(PATH, V1)], machine_id=mid)
    tick(client, token, [entry(PATH, V2, mtime=1767225600)], machine_id=mid)

    # V1 从来没存上过，但它已经不是"最近一次扫描到的"那一版了
    stale = upload(client, token, PATH, V1)
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_upload"
    assert stale.json()["details"]["expected"] == sha256_of(V2)
    assert not source_file(settings).exists(), "旧内容不该落到 source/"

    assert upload(client, token, PATH, V2).status_code == 200
    assert source_file(settings).read_bytes() == V2


def test_改回已存的那一版就不再要求上传(client, enrolled) -> None:
    """V1 已存 → 改成 V2（没传上）→ 又改回 V1：已存的就是当前的，不用再传。"""
    token, mid = enrolled["token"], enrolled["machine_id"]
    tick(client, token, [entry(PATH, V1)], machine_id=mid)
    assert upload(client, token, PATH, V1).status_code == 200
    tick(client, token, [entry(PATH, V2, mtime=1767225600)], machine_id=mid)

    body = tick(client, token, [entry(PATH, V1, mtime=1767225700)], machine_id=mid)

    assert body["need_upload"] == []


def test_物化失败时台账不记成已存_下一轮还会再要(client, enrolled, settings, app) -> None:
    """先物化、后改台账：物化失败（这里是 source/ 里同名路径是个目录）不能让台账
    以为"已存下"—— 否则下一轮不再要求上传，source/ 永远缺这个文件。"""
    token, mid = enrolled["token"], enrolled["machine_id"]
    tick(client, token, [entry(PATH, V1)], machine_id=mid)
    blocker = source_file(settings)
    blocker.mkdir(parents=True)
    (blocker / "occupied").write_text("x", encoding="utf-8")

    failed = upload(client, token, PATH, V1)
    assert failed.status_code == 500

    row = ledger_row(app)
    assert row.content_stored is False
    assert row.revision == 0
    assert tick(client, token, [entry(PATH, V1)], machine_id=mid)["need_upload"] == [PATH]

    (blocker / "occupied").unlink()
    blocker.rmdir()
    assert upload(client, token, PATH, V1).status_code == 200
    assert source_file(settings).read_bytes() == V1


def test_迁移_013_用_sha256_回填最近一次扫描(workdir: Path) -> None:
    path = workdir / "v12.db"
    engine = create_engine("sqlite:///%s" % path.as_posix())
    migrate(engine)
    with engine.begin() as conn:
        # 退回"迁移 013 之前"的结构：没有 scanned_sha256 这一列
        conn.execute(text("ALTER TABLE source_file DROP COLUMN scanned_sha256"))
        conn.execute(text("PRAGMA user_version = 12"))
        conn.execute(text(
            "INSERT INTO contest (id, slug, name, status, created_at) "
            "VALUES (1, 'c', 'c', 'running', '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "INSERT INTO player (id, contest_id, player_no, name, created_at) "
            "VALUES (1, 1, 'S001', 'n', '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "INSERT INTO source_file (player_id, rel_path, sha256, size, mtime, revision,"
            " first_seen_at, last_seen_at, content_stored) VALUES"
            " (1, 'a.cpp', :a, 1, 0, 1, '2026-01-01', '2026-01-01', 1),"
            " (1, 'b.cpp', :b, 1, 0, 0, '2026-01-01', '2026-01-01', 0)"
        ), {"a": "a" * 64, "b": "b" * 64})

    migrate(engine)

    with engine.connect() as conn:
        version = conn.execute(text("PRAGMA user_version")).scalar()
        rows = conn.execute(text(
            "SELECT rel_path, sha256, scanned_sha256 FROM source_file ORDER BY rel_path"
        )).fetchall()
    engine.dispose()
    assert version >= 13
    assert [(r[0], r[2]) for r in rows] == [("a.cpp", "a" * 64), ("b.cpp", "b" * 64)]


# --------------------------------------------------------------------------- #
# 扫描是按别人的身份展开的（机器刚被改派）：整份不对账
# --------------------------------------------------------------------------- #


def test_按别人的身份展开的扫描整份拒收(client, enrolled, app) -> None:
    """机器刚从 S001 改派到 S002 的那一轮，Agent 送来的还是 S001 目录的扫描。

    服务端此刻解析出的身份是新的那个人；把这份扫描记进来，就是把上一个人的代码
    写进新身份的台账并要求上传。这一轮必须整份拒收，响应照常带回新身份。
    """
    token, mid = enrolled["token"], enrolled["machine_id"]

    body = tick(
        client, token, [entry(PATH, V1)], machine_id=mid,
        scanned_for={"player_no": "S999", "contest_slug": "mock-1"},
    )

    assert body["need_upload"] == []
    assert body["player_no"] == "S001"
    with app.state.ctx.db.session() as session:
        assert session.execute(select(SourceFile)).first() is None, "上一个人的文件进了台账"
        categories = [
            row[0] for row in session.execute(text("SELECT category FROM event_log"))
        ]
    assert "scan_identity_mismatch" in categories


def test_身份一致的扫描照常对账(client, enrolled) -> None:
    token, mid = enrolled["token"], enrolled["machine_id"]

    body = tick(
        client, token, [entry(PATH, V1)], machine_id=mid,
        scanned_for={"player_no": "S001", "contest_slug": "mock-1"},
    )

    assert body["need_upload"] == [PATH]


def test_服务端截断扫描时不把截掉的文件判成删除(client, enrolled, app) -> None:
    """扫描超过 max_files_per_scan 被截断时，截掉的那部分服务端根本没看见。

    曾经仍按 Agent 报的 scan_complete=true 做删除判定 —— 被截掉的文件全被标成
    "选手删除了"（审计已复现：上限 3、上报 6 个，f3..f5 全被标删）。
    """
    token, mid = enrolled["token"], enrolled["machine_id"]
    ctx = app.state.ctx
    files = [entry("d/f%d.cpp" % i, b"x%d" % i) for i in range(6)]
    tick(client, token, files, machine_id=mid)

    ctx.settings.max_files_per_scan = 3
    tick(client, token, files, machine_id=mid, scan_complete=True)

    with ctx.db.session() as session:
        deleted = [
            row.rel_path for row in session.execute(select(SourceFile)).scalars()
            if row.deleted_at is not None
        ]
    assert deleted == []


def test_scan_complete_是必填字段(client, enrolled) -> None:
    """它曾缺省为 true —— 漏写字段的报文被当成"完整扫描"，恰好是会误删的那一边。"""
    token, mid = enrolled["token"], enrolled["machine_id"]
    response = client.post(
        "/api/v1/agent/tick",
        json={"machine_id": mid, "ts": 1, "scan": [], "partials": [], "stats": {}},
        headers=agent_headers(token),
    )
    assert response.status_code == 422
