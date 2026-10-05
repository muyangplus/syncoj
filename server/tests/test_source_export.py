"""代码文件：下载、打包导出、清理。

导出是这个项目里最"实用主义"的功能：教师拿到归档要能核对"是不是收全了、
有没有传坏"。所以测试的重点不是"zip 能打开"，而是：

* 包里的目录结构与 ``source/`` 一致，解压出来能直接喂给评测器
* **清单里的字节数与 SHA256 与实际内容对得上** —— 那是核对的唯一依据
* 内容缺失的条目也要出现在清单里（教师要知道少了哪个），而不是悄悄消失
* 删记录不能把别人还在用的内容一起删掉（内容是按哈希共享的）
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Dict, List

from fastapi.testclient import TestClient

from conftest import agent_headers, do_tick as tick, scan_entry as entry, sha256_of


def report_files(client: TestClient, enrolled: dict, files: Dict[str, bytes]) -> None:
    """报一批文件（一次 tick 报全，然后逐个上传）。

    **必须一次性报全**：``scan`` 是"目录里现在有什么"的**全量索引**，
    只报新增的那一个会被服务端正确地理解成"其余文件都被删了"。
    这个坑在测试里踩一次就够了，所以 helper 直接要求调用方把完整集合给它。
    """
    scan = [entry(path, data) for path, data in files.items()]
    tick(client, enrolled["token"], scan, machine_id=enrolled["machine_id"])
    for path, data in files.items():
        response = client.post(
            "/api/v1/agent/files",
            data={"path": path, "sha256": sha256_of(data)},
            files={"file": ("code.bin", data, "application/octet-stream")},
            headers=agent_headers(enrolled["token"]),
        )
        assert response.status_code == 200, response.text


def files_of(client: TestClient, headers: dict, contest: dict) -> List[Dict]:
    body = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"], headers=headers
    ).json()
    return body["items"]


def add_problem(client: TestClient, headers: dict, contest: dict, ident: str) -> None:
    response = client.post(
        "/api/v1/admin/contests/%d/problems" % contest["id"],
        json=[{"ident": ident}],
        headers=headers,
    )
    assert response.status_code == 200, response.text


def read_export(
    client: TestClient, headers: dict, contest: dict, query: str = ""
) -> zipfile.ZipFile:
    response = client.get(
        "/api/v1/admin/contests/%d/files/export%s" % (contest["id"], query),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


def manifest_rows(archive: zipfile.ZipFile) -> List[Dict[str, str]]:
    raw = archive.read("清单.csv").decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(raw)))


# --------------------------------------------------------------------------- #
# 单文件下载
# --------------------------------------------------------------------------- #


def test_download_single_file(client: TestClient, admin_headers: dict, contest: dict,
                              enrolled: dict) -> None:
    content = b"int main() { return 0; }\n"
    report_files(client, enrolled, {"p1/p1.cpp": content})
    file_id = files_of(client, admin_headers, contest)[0]["id"]

    response = client.get("/api/v1/admin/files/%d/content" % file_id, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.content == content
    assert "p1.cpp" in response.headers["content-disposition"]


def test_download_is_admin_only(client: TestClient, contest: dict, enrolled: dict) -> None:
    report_files(client, enrolled, {"p1/p1.cpp": b"x"})
    files = client.get(
        "/api/v1/admin/contests/%d/files" % contest["id"],
        headers={"Authorization": "Bearer " + enrolled["token"]},
    )
    assert files.status_code == 401


def test_download_missing_content_says_why(client: TestClient, admin_headers: dict,
                                           contest: dict, enrolled: dict, app) -> None:
    """只上报了索引、内容从未上传成功过 —— 要说清楚是哪种缺，而不是 404 了事。"""
    from syncoj_server.models import Player, SourceFile, utcnow

    with app.state.ctx.db.session() as session:
        player = session.query(Player).first()
        row = SourceFile(
            player_id=player.id,
            rel_path="p1/ghost.cpp",
            sha256="0" * 64,
            size=10,
            mtime=1,
            revision=1,
            content_stored=False,
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )
        session.add(row)
        session.flush()
        file_id = row.id

    response = client.get("/api/v1/admin/files/%d/content" % file_id, headers=admin_headers)
    assert response.status_code == 404
    assert "从未成功上传" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 打包导出
# --------------------------------------------------------------------------- #


def test_export_zip_layout_matches_source(client: TestClient, admin_headers: dict,
                                          contest: dict, enrolled: dict) -> None:
    """解压出来就能直接喂给评测器 —— 目录结构与 source/ 一致。"""
    first = b"int main(){return 0;}\n"
    report_files(client, enrolled, {"p1/p1.cpp": first, "p1/extra.h": b"#pragma once\n"})

    with read_export(client, admin_headers, contest) as archive:
        names = set(archive.namelist())
        assert "清单.csv" in names
        assert "代码/S001/p1/p1.cpp" in names
        assert "代码/S001/p1/extra.h" in names
        assert archive.read("代码/S001/p1/p1.cpp") == first


def test_export_manifest_carries_exact_sizes_and_hashes(
    client: TestClient, admin_headers: dict, contest: dict, enrolled: dict
) -> None:
    """清单是核对的唯一依据：字节数与 SHA256 必须与实际内容一致。"""
    first = b"int main(){return 0;}\n"
    second = b"#include <bits/stdc++.h>\n"
    add_problem(client, admin_headers, contest, "p1")
    report_files(client, enrolled, {"p1/p1.cpp": first, "p1/util.h": second})

    with read_export(client, admin_headers, contest) as archive:
        rows = manifest_rows(archive)

    by_path = {row["相对路径"]: row for row in rows}
    assert set(by_path) == {"p1/p1.cpp", "p1/util.h"}

    assert by_path["p1/p1.cpp"]["字节数"] == str(len(first))
    assert by_path["p1/p1.cpp"]["sha256"] == sha256_of(first)
    assert by_path["p1/p1.cpp"]["准考证号"] == "S001"
    assert by_path["p1/p1.cpp"]["题目"] == "p1"
    assert by_path["p1/p1.cpp"]["内容完整"] == "是"

    assert by_path["p1/util.h"]["字节数"] == str(len(second))
    assert by_path["p1/util.h"]["sha256"] == sha256_of(second)


def test_export_manifest_lists_missing_content(client: TestClient, admin_headers: dict,
                                               contest: dict, enrolled: dict, app) -> None:
    """内容缺失的条目也要在清单里 —— 教师要知道少了哪个，而不是打开 zip 才发现。"""
    from syncoj_server.models import Player, SourceFile, utcnow

    with app.state.ctx.db.session() as session:
        player = session.query(Player).first()
        session.add(
            SourceFile(
                player_id=player.id,
                rel_path="p1/lost.cpp",
                sha256="1" * 64,
                size=42,
                mtime=1,
                revision=1,
                content_stored=False,
                first_seen_at=utcnow(),
                last_seen_at=utcnow(),
            )
        )

    with read_export(client, admin_headers, contest) as archive:
        names = archive.namelist()
        assert "代码/S001/p1/lost.cpp" not in names, "内容都没有，不该造一个空文件出来"
        rows = manifest_rows(archive)

    row = next(r for r in rows if r["相对路径"] == "p1/lost.cpp")
    assert row["内容完整"] == "否（只有索引）"
    assert row["字节数"] == "42", "即使内容不在，清单里也该有它上报过的大小"


def test_export_can_filter_by_player_and_problem(
    client: TestClient, admin_headers: dict, contest: dict, enrolled: dict
) -> None:
    add_problem(client, admin_headers, contest, "p1")
    add_problem(client, admin_headers, contest, "p2")
    report_files(client, enrolled, {"p1/p1.cpp": b"one", "p2/p2.cpp": b"two"})

    with read_export(client, admin_headers, contest, "?problem=p1") as archive:
        assert set(archive.namelist()) == {"清单.csv", "代码/S001/p1/p1.cpp"}

    player_id = client.get(
        "/api/v1/admin/contests/%d/players" % contest["id"], headers=admin_headers
    ).json()["items"][0]["id"]
    with read_export(client, admin_headers, contest, "?player_id=%d" % player_id) as archive:
        assert "代码/S001/p1/p1.cpp" in archive.namelist()


def test_export_with_no_matches_is_a_clear_404(client: TestClient, admin_headers: dict,
                                               contest: dict, enrolled: dict) -> None:
    report_files(client, enrolled, {"p1/p1.cpp": b"x"})
    response = client.get(
        "/api/v1/admin/contests/%d/files/export?problem=不存在的题" % contest["id"],
        headers=admin_headers,
    )
    assert response.status_code == 404
    assert "没有符合条件" in response.json()["detail"]


def test_export_includes_deleted_when_asked(client: TestClient, admin_headers: dict,
                                            contest: dict, enrolled: dict) -> None:
    """归档时通常只想拿活着的；但"连已消失的一起，看看谁删过东西"也是合理需求。"""
    keep = b"keep"
    report_files(client, enrolled, {"p1/keep.cpp": keep, "p1/gone.cpp": b"bye"})
    # 第二次 tick 不带 gone.cpp = Agent 明确上报"它没了"
    report_files(client, enrolled, {"p1/keep.cpp": keep})

    with read_export(client, admin_headers, contest) as archive:
        assert "代码/S001/p1/gone.cpp" not in archive.namelist()

    with read_export(client, admin_headers, contest, "?include_deleted=true") as archive:
        assert "代码/S001/p1/gone.cpp" in archive.namelist()


# --------------------------------------------------------------------------- #
# 清理台账
# --------------------------------------------------------------------------- #


def test_clear_files_defaults_to_tombstones_only(client: TestClient, admin_headers: dict,
                                                 contest: dict, enrolled: dict) -> None:
    """默认只清"已消失"的墓碑记录 —— 那才是这个操作真正持久的用途。

    ``purge=true`` 删掉活记录之后，机器下一轮 tick 会把它们原样收回来，
    所以它只适合"比赛结束、归档完毕、准备清场"。
    """
    keep = b"keep"
    report_files(client, enrolled, {"p1/keep.cpp": keep, "p1/gone.cpp": b"bye"})
    report_files(client, enrolled, {"p1/keep.cpp": keep})

    missing = client.post(
        "/api/v1/admin/contests/%d/files/clear" % contest["id"],
        json={},
        headers=admin_headers,
    )
    assert missing.status_code == 422, "清空台账要打场次标识"

    response = client.post(
        "/api/v1/admin/contests/%d/files/clear" % contest["id"],
        json={"confirm": contest["slug"]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "已清理 1 条" in response.json()["detail"]

    left = files_of(client, admin_headers, contest)
    assert [row["rel_path"] for row in left] == ["p1/keep.cpp"]


def test_purge_removes_live_records_too(client: TestClient, admin_headers: dict,
                                       contest: dict, enrolled: dict) -> None:
    report_files(client, enrolled, {"p1/a.cpp": b"a"})
    response = client.post(
        "/api/v1/admin/contests/%d/files/clear" % contest["id"],
        json={"confirm": contest["slug"], "purge": True},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert "机器还在报的文件会在下一轮重新出现" in response.json()["detail"]
    assert files_of(client, admin_headers, contest) == []


def test_deleting_a_file_frees_its_content(client: TestClient, admin_headers: dict,
                                           contest: dict, enrolled: dict, app) -> None:
    report_files(client, enrolled, {"p1/only.cpp": b"unique-content"})
    row = files_of(client, admin_headers, contest)[0]
    sha = row["sha256"]
    assert app.state.ctx.blobs.has(sha)

    response = client.delete("/api/v1/admin/files/%d" % row["id"], headers=admin_headers)
    assert response.status_code == 200, response.text
    assert "内容已从存储中清除" in response.json()["detail"]
    assert not app.state.ctx.blobs.has(sha)


def test_deleting_one_copy_keeps_shared_content(
    client: TestClient, admin_headers: dict, contest: dict, enrolled: dict, app
) -> None:
    """内容按哈希共享。删掉一份引用不能把别人还在用的内容一起删掉 ——
    否则表现是"某个早就归档的文件突然取不到内容"，而且不报错，只是 404。
    """
    same = b"identical-bytes"
    report_files(client, enrolled, {"p1/a.cpp": same, "p2/b.cpp": same})

    rows = files_of(client, admin_headers, contest)
    assert len(rows) == 2 and rows[0]["sha256"] == rows[1]["sha256"]
    sha = rows[0]["sha256"]

    response = client.delete(
        "/api/v1/admin/files/%d" % rows[0]["id"], headers=admin_headers
    )
    assert "内容已从存储中清除" not in response.json()["detail"]
    assert app.state.ctx.blobs.has(sha), "另一条记录还在引用它"

    # 剩下那条仍然能下载
    remaining = files_of(client, admin_headers, contest)[0]
    assert client.get(
        "/api/v1/admin/files/%d/content" % remaining["id"], headers=admin_headers
    ).content == same


def test_deleting_a_file_does_not_touch_a_shared_asset(
    client: TestClient, admin_headers: dict, contest: dict, enrolled: dict, app
) -> None:
    """代码台账与下发资产共用同一个内容寻址存储。

    只查代码那一侧的话，"同一份内容既被收上来又被下发过"时会误删 ——
    而表现是某个已经发出去的文件突然取不到内容。
    """
    same = b"shared-with-an-asset"
    asset = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": ("题面.zip", same, "application/zip")},
        headers=admin_headers,
    ).json()
    report_files(client, enrolled, {"p1/same.bin": same})

    row = next(
        r for r in files_of(client, admin_headers, contest) if r["sha256"] == asset["sha256"]
    )
    client.delete("/api/v1/admin/files/%d" % row["id"], headers=admin_headers)
    assert app.state.ctx.blobs.has(asset["sha256"]), "资产还在引用这份内容"
