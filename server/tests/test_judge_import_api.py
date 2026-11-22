"""题目配置导入接口：POST /contests/{id}/judge-import/preview 与 /apply。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Dict, List

from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.models import EventLog, Problem

from problem_io_fixtures import (
    make_native_zip,
    make_testdata_zip,
    make_zip,
    native_problem,
    sha256_hex,
)

ADMIN = "/api/v1/admin"


def preview(client: TestClient, contest: dict, headers: dict, data: bytes, name: str = "pkg.zip", fmt: str = "auto"):
    return client.post(
        "%s/contests/%d/judge-import/preview?format=%s" % (ADMIN, contest["id"], fmt),
        files={"file": (name, data, "application/zip")},
        headers=headers,
    )


def apply(client: TestClient, contest: dict, headers: dict, token: str, items: List[dict], **extra):
    body: Dict[str, object] = {"token": token, "items": items}
    body.update(extra)
    return client.post("%s/contests/%d/judge-import/apply" % (ADMIN, contest["id"]), json=body, headers=headers)


def accept_all(body: dict) -> List[dict]:
    return [{"key": p["key"], "ident": p["ident"], "action": p["action"]} for p in body["problems"]]


def make_contest(client: TestClient, headers: dict, slug: str) -> dict:
    response = client.post("%s/contests" % ADMIN, json={"name": slug, "slug": slug}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def staging_dir(app) -> Path:
    return app.state.ctx.settings.data_root / "tmp" / "judge-import"


# --------------------------------------------------------------------------- #


def test_preview_then_create(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    data = make_native_zip([
        native_problem("sum", input_file="data.txt", output_file=None),
        native_problem("ext", judge_mode="external", testdata=None, io_mode="stdio", note="备注",
                       file_patterns=["ext/*.cpp"], order_index=7),
    ])
    response = preview(client, contest, admin_headers, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "syncoj"
    first, second = body["problems"]
    assert (first["ident"], first["action"], first["exists"]) == ("sum", "create", False)
    assert (first["cases"], first["total_score"], first["input_file"]) == (3, 100, "data.txt")
    assert (second["ident"], second["cases"], second["judge_mode"]) == ("ext", 0, "external")
    assert (staging_dir(app) / (body["token"] + ".bin")).is_file()

    response = apply(client, contest, admin_headers, body["token"], accept_all(body))
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["created"], result["updated"], result["skipped"]) == (2, 0, 0)
    sum_out, ext_out = result["problems"]
    assert sum_out["judge_mode"] == "builtin" and sum_out["testdata"]["cases"] == 3
    assert sum_out["input_file"] == "data.txt" and sum_out["compile_flags"] == "-std=c++17"
    assert ext_out["judge_mode"] == "external" and ext_out["testdata"] is None
    assert ext_out["note"] == "备注" and ext_out["file_patterns"] == ["ext/*.cpp"] and ext_out["order_index"] == 7
    # 用完的暂存删掉；写了审计日志
    assert not (staging_dir(app) / (body["token"] + ".bin")).exists()
    with app.state.ctx.db.session() as session:
        logs = list(session.execute(select(EventLog).where(EventLog.category == "judge_import")).scalars())
        assert len(logs) == 1 and "新建 2 道" in logs[0].message


def test_update_replaces_testdata_and_reclaims_old_blob(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    created = client.post("%s/contests/%d/problems" % (ADMIN, contest["id"]), json=[{"ident": "sum", "title": "旧"}],
                          headers=admin_headers).json()["problems"][0]
    old_zip = make_testdata_zip(count=2)
    upload = client.post("%s/problems/%d/testdata" % (ADMIN, created["id"]),
                         files={"file": ("old.zip", old_zip)}, headers=admin_headers)
    assert upload.status_code == 200, upload.text
    old_sha = upload.json()["testdata"]["sha256"]
    assert app.state.ctx.blobs.has(old_sha)

    body = preview(client, contest, admin_headers, make_native_zip([native_problem("sum", title=None)])).json()
    (item,) = body["problems"]
    assert (item["action"], item["exists"], item["existing_problem_id"]) == ("update", True, created["id"])
    result = apply(client, contest, admin_headers, body["token"], accept_all(body)).json()
    assert (result["created"], result["updated"]) == (0, 1)
    (out,) = result["problems"]
    assert out["id"] == created["id"]
    assert out["title"] == "旧"  # 包里没有标题时不覆盖
    assert out["testdata"]["cases"] == 3
    assert not app.state.ctx.blobs.has(old_sha)


def test_skip_and_rename(client: TestClient, contest: dict, admin_headers: dict) -> None:
    data = make_native_zip([native_problem("a"), native_problem("b", input_file=None, output_file=None)])
    body = preview(client, contest, admin_headers, data).json()
    items = [
        {"key": body["problems"][0]["key"], "ident": "a", "action": "skip"},
        {"key": body["problems"][1]["key"], "ident": "renamed", "action": "create"},
    ]
    result = apply(client, contest, admin_headers, body["token"], items).json()
    assert (result["created"], result["skipped"]) == (1, 1)
    (out,) = result["problems"]
    assert out["ident"] == "renamed"
    # 改名后文件 IO 仍读写包里原来的文件名
    assert (out["input_file"], out["output_file"]) == ("b.in", "b.out")


def test_duplicate_idents_in_one_batch_conflict(client: TestClient, contest: dict, admin_headers: dict) -> None:
    body = preview(client, contest, admin_headers, make_native_zip([native_problem("a"), native_problem("b")])).json()
    items = [{"key": p["key"], "ident": "same", "action": "create"} for p in body["problems"]]
    response = apply(client, contest, admin_headers, body["token"], items)
    assert response.status_code == 409 and response.json()["code"] == "ident_conflict"
    assert response.json()["details"]["ident"] == "same"


def test_create_over_existing_conflicts(client: TestClient, contest: dict, admin_headers: dict) -> None:
    client.post("%s/contests/%d/problems" % (ADMIN, contest["id"]), json=[{"ident": "sum"}], headers=admin_headers)
    body = preview(client, contest, admin_headers, make_native_zip()).json()
    items = [{"key": body["problems"][0]["key"], "ident": "sum", "action": "create"}]
    response = apply(client, contest, admin_headers, body["token"], items)
    assert response.status_code == 409 and response.json()["code"] == "ident_conflict"


def test_item_with_errors_can_only_be_skipped(client: TestClient, contest: dict, admin_headers: dict) -> None:
    body = preview(client, contest, admin_headers, make_native_zip(tamper_sha=True)).json()
    (item,) = body["problems"]
    assert item["action"] == "skip" and [e["code"] for e in item["errors"]] == ["sha_mismatch"]
    response = apply(client, contest, admin_headers, body["token"],
                     [{"key": item["key"], "ident": "sum", "action": "create"}])
    assert response.status_code == 400 and response.json()["code"] == "validation_error"


def test_token_is_bound_to_contest_and_expires(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    body = preview(client, contest, admin_headers, make_native_zip()).json()
    other = make_contest(client, admin_headers, "other")
    response = apply(client, other, admin_headers, body["token"], accept_all(body))
    assert response.status_code == 404 and response.json()["code"] == "import_token_expired"
    for bad in ("../../etc", "f" * 32):
        response = apply(client, contest, admin_headers, bad, [])
        assert response.status_code == 404 and response.json()["code"] == "import_token_expired"

    meta_path = staging_dir(app) / (body["token"] + ".json")
    meta = json.loads(meta_path.read_text())
    meta["created_at"] -= 3 * 3600
    meta_path.write_text(json.dumps(meta))
    response = apply(client, contest, admin_headers, body["token"], accept_all(body))
    assert response.status_code == 404 and response.json()["code"] == "import_token_expired"
    assert not meta_path.exists()


def test_at_most_five_staged_per_contest(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    tokens = [preview(client, contest, admin_headers, make_native_zip()).json()["token"] for _ in range(6)]
    staged = {p.stem for p in staging_dir(app).glob("*.json")}
    assert staged == set(tokens[1:])
    response = apply(client, contest, admin_headers, tokens[0], [])
    assert response.status_code == 404


def test_too_many_problems(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    data = make_native_zip([native_problem("p%d" % i, testdata=None) for i in range(201)])
    response = preview(client, contest, admin_headers, data)
    assert response.status_code == 413 and response.json()["code"] == "too_many_problems"
    assert list(staging_dir(app).glob("*.bin")) == []


def test_package_level_errors_name_the_reason(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    response = preview(client, contest, admin_headers, make_testdata_zip())
    assert response.status_code == 400
    assert response.json()["details"]["code_detail"] == "plain_testdata"
    response = preview(client, contest, admin_headers, b"\x20\x11\x11\x27binary", name="sample.cdf")
    assert response.json()["details"]["code_detail"] == "cdf_binary"
    response = preview(client, contest, admin_headers, make_zip({"x.txt": "1"}))
    assert response.json()["details"]["code_detail"] == "unknown_format"
    assert list(staging_dir(app).glob("*")) == []


def test_unknown_contest(client: TestClient, admin_headers: dict) -> None:
    response = preview(client, {"id": 9999}, admin_headers, make_native_zip())
    assert response.status_code == 404


def test_requires_admin(client: TestClient, contest: dict) -> None:
    assert preview(client, contest, {}, make_native_zip()).status_code == 401


def test_blob_is_shared_with_identical_upload(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    """原生导入写回的数据包字节不变：sha256 与清单里的一致。"""
    inner = make_testdata_zip(count=4)
    body = preview(client, contest, admin_headers, make_native_zip([native_problem("x", testdata=inner)])).json()
    (out,) = apply(client, contest, admin_headers, body["token"], accept_all(body)).json()["problems"]
    assert out["testdata"]["sha256"] == sha256_hex(inner)
    assert out["testdata"]["name"] == "x.zip"
    with app.state.ctx.db.session() as session:
        row = session.execute(select(Problem).where(Problem.ident == "x")).scalar_one()
        assert json.loads(row.testdata_info)["cases"] == 4


def _native_with_testdata_names(names: List[object]) -> bytes:
    """原生包，第 i 道题清单里的 ``testdata.name`` 改成 ``names[i]``。"""
    problems = [native_problem("p%d" % i) for i in range(len(names))]
    raw = make_native_zip(problems)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(files["syncoj-problems.json"])
    for entry, name in zip(manifest["problems"], names):
        entry["testdata"]["name"] = name
    files["syncoj-problems.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    return make_zip(files)


def test_testdata_name_from_manifest_is_cleaned(client: TestClient, contest: dict, admin_headers: dict) -> None:
    """清单里的 testdata.name 含 CR/LF 时，入库前去掉控制字符；空了用 <ident>.zip。下载头里不出现控制字符。"""
    names = ["a\r\nX-Evil: 1.zip", "\r\n\t", None, "dir/sub\\数据.zip", "x" * 300 + ".zip"]
    body = preview(client, contest, admin_headers, _native_with_testdata_names(names)).json()
    result = apply(client, contest, admin_headers, body["token"], accept_all(body)).json()
    assert result["created"] == 5, result
    stored = [p["testdata"]["name"] for p in result["problems"]]
    assert stored == ["aX-Evil: 1.zip", "p1.zip", "p2.zip", "数据.zip", "x" * 255]
    for problem in result["problems"]:
        response = client.get("%s/problems/%d/testdata/file" % (ADMIN, problem["id"]), headers=admin_headers)
        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in disposition), disposition


def test_attachment_header_drops_control_characters() -> None:
    from syncoj_server.api.admin import _attachment_header

    header = _attachment_header('a\r\nX-Evil: 1\x00"\\.zip')
    assert header.startswith('attachment; filename="a__X-Evil: 1___.zip"; filename*=UTF-8\'\'')
    assert "\r" not in header and "\n" not in header and "%0D" not in header and "%0A" not in header
    assert _attachment_header("") == "attachment; filename=\"download\"; filename*=UTF-8''download"
    assert "filename*=UTF-8''%E6%95%B0%E6%8D%AE.zip" in _attachment_header("数据.zip")


def test_apply_streams_blobs_instead_of_bytes(client: TestClient, contest: dict, admin_headers: dict, app,
                                              monkeypatch) -> None:
    """应用时数据包流式入库（put_stream），不整包读成 bytes 再 put_bytes。原生包与外来格式都一样。"""
    from problem_io_fixtures import lemon_task, make_lemon_zip

    def forbidden(*args, **kwargs):
        raise AssertionError("不应整包读进内存")

    monkeypatch.setattr(app.state.ctx.blobs, "put_bytes", forbidden)
    for data, ident in ((make_native_zip([native_problem("nat")]), "nat"),
                        (make_lemon_zip([lemon_task("lem")]), "lem")):
        body = preview(client, contest, admin_headers, data).json()
        assert [p["ident"] for p in body["problems"]] == [ident]
        response = apply(client, contest, admin_headers, body["token"], accept_all(body))
        assert response.status_code == 200, response.text
        (out,) = response.json()["problems"]
        assert out["testdata"]["cases"] == body["problems"][0]["cases"] > 0
