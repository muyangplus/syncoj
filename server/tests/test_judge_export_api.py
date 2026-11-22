"""题目配置导出接口，以及原生格式的无损往返。"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.models import Problem
from syncoj_server.services import problem_io
from syncoj_server.services.problem_io import FormatSpec
from syncoj_server.services.problem_io.notices import notice

from problem_io_fixtures import make_testdata_zip, sha256_hex

from test_judge_import_api import ADMIN, accept_all, apply, make_contest, preview

#: 往返时要逐列相同的列（id、contest_id、时间戳除外）
COLUMNS = (
    "ident", "title", "order_index", "note", "file_patterns", "judge_mode", "time_limit_ms",
    "memory_limit_mb", "io_mode", "input_file", "output_file", "compile_flags",
    "testdata_sha256", "testdata_name", "testdata_size", "testdata_info",
)


def setup_problems(client: TestClient, contest: dict, headers: dict) -> Dict[str, dict]:
    """三道题：文件 IO + 自定义文件名 + checker；外部评测无数据包；自带评测但缺数据包。"""
    rows = client.post(
        "%s/contests/%d/problems" % (ADMIN, contest["id"]),
        json=[
            {"ident": "sum", "title": "求和", "note": "第一题", "file_patterns": ["sum/*.cpp"]},
            {"ident": "ext", "order_index": 9},
            {"ident": "nodata"},
        ],
        headers=headers,
    ).json()["problems"]
    by_ident = {row["ident"]: row for row in rows}
    response = client.put(
        "%s/problems/%d/judge" % (ADMIN, by_ident["sum"]["id"]),
        json={"judge_mode": "builtin", "time_limit_ms": 2000, "memory_limit_mb": 512, "io_mode": "file",
              "input_file": "data.in", "compile_flags": "-std=c++17 -O2"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    client.put("%s/problems/%d/judge" % (ADMIN, by_ident["nodata"]["id"]),
               json={"judge_mode": "builtin"}, headers=headers)
    testdata = make_testdata_zip(count=4, subtasks=[{"score": 60, "cases": ["3", "4"]}], scores=[20, 20, 0, 0],
                                 checker='#include "testlib.h"\nint main(){}\n', support={"testlib.h": "// t\n"})
    response = client.post("%s/problems/%d/testdata" % (ADMIN, by_ident["sum"]["id"]),
                           files={"file": ("原始数据.zip", testdata)}, headers=headers)
    assert response.status_code == 200, response.text
    by_ident["sum"] = response.json()
    by_ident["sum"]["_zip"] = testdata
    return by_ident


def columns(app, contest_id: int) -> Dict[str, tuple]:
    with app.state.ctx.db.session() as session:
        rows = session.execute(select(Problem).where(Problem.contest_id == contest_id)).scalars()
        return {row.ident: tuple(getattr(row, c) for c in COLUMNS) for row in rows}


def export_zip(client: TestClient, url: str, headers: dict) -> zipfile.ZipFile:
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


# --------------------------------------------------------------------------- #


def test_native_round_trip_is_lossless(client: TestClient, contest: dict, admin_headers: dict, app) -> None:
    setup_problems(client, contest, admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=syncoj" % (ADMIN, contest["id"]), admin_headers)
    manifest = json.loads(archive.read("syncoj-problems.json"))
    assert manifest["format"] == "syncoj-judge" and manifest["version"] == 1
    assert sorted(p["ident"] for p in manifest["problems"]) == ["ext", "nodata", "sum"]

    target = make_contest(client, admin_headers, "round-trip")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name in archive.namelist():
            out.writestr(name, archive.read(name))
    body = preview(client, target, admin_headers, buffer.getvalue()).json()
    assert body["format"] == "syncoj"
    assert all(not p["errors"] for p in body["problems"])
    result = apply(client, target, admin_headers, body["token"], accept_all(body)).json()
    assert result["created"] == 3

    assert columns(app, target["id"]) == columns(app, contest["id"])


def test_native_export_keeps_blob_bytes(client: TestClient, contest: dict, admin_headers: dict) -> None:
    problems = setup_problems(client, contest, admin_headers)
    archive = export_zip(client, "%s/problems/%d/judge-export?format=syncoj" % (ADMIN, problems["sum"]["id"]),
                         admin_headers)
    assert archive.read("sum/testdata.zip") == problems["sum"]["_zip"]
    (entry,) = json.loads(archive.read("syncoj-problems.json"))["problems"]
    assert entry["testdata"] == {
        "path": "sum/testdata.zip", "name": "原始数据.zip",
        "sha256": sha256_hex(problems["sum"]["_zip"]), "size": len(problems["sum"]["_zip"]),
    }
    assert (entry["input_file"], entry["output_file"]) == ("data.in", None)
    assert entry["file_patterns"] == ["sum/*.cpp"]


def test_download_testdata_file(client: TestClient, contest: dict, admin_headers: dict) -> None:
    problems = setup_problems(client, contest, admin_headers)
    response = client.get("%s/problems/%d/testdata/file" % (ADMIN, problems["sum"]["id"]), headers=admin_headers)
    assert response.status_code == 200
    assert response.content == problems["sum"]["_zip"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    response = client.get("%s/problems/%d/testdata/file" % (ADMIN, problems["ext"]["id"]), headers=admin_headers)
    assert response.status_code == 404


def test_native_preview_lists_every_problem(client: TestClient, contest: dict, admin_headers: dict) -> None:
    setup_problems(client, contest, admin_headers)
    body = client.get("%s/contests/%d/judge-export/preview?format=syncoj" % (ADMIN, contest["id"]),
                      headers=admin_headers).json()
    assert body["format"] == "syncoj"
    assert [(p["ident"], p["included"]) for p in body["problems"]] == [("sum", True), ("nodata", True), ("ext", True)]


def _fake_export(problems, open_blob, zf) -> Dict[int, List]:
    out = {}
    for problem in problems:
        zf.writestr("%s.txt" % problem.ident, problem.ident)
        out[problem.id] = [notice("flags_dropped", flags=problem.compile_flags or "")] if problem.compile_flags else []
    return out


@pytest.fixture()
def fake_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """换一个假的 hoj 导出实现：只测框架（选题、跳过、警告汇总），不依赖具体格式。"""
    spec = problem_io.FORMATS["hoj"]
    monkeypatch.setitem(problem_io.FORMATS, "hoj", FormatSpec("hoj", spec.label, spec.parse, _fake_export))


def test_non_native_default_selection_and_skips(
    client: TestClient, contest: dict, admin_headers: dict, fake_format: None
) -> None:
    problems = setup_problems(client, contest, admin_headers)
    # 默认：只取带数据包的自带评测题
    body = client.get("%s/contests/%d/judge-export/preview?format=hoj" % (ADMIN, contest["id"]),
                      headers=admin_headers).json()
    assert [(p["ident"], p["included"]) for p in body["problems"]] == [("sum", True)]
    assert [w["code"] for w in body["problems"][0]["warnings"]] == ["flags_dropped"]
    # 显式选了没数据包的题：跳过并提示
    url = "%s/contests/%d/judge-export/preview?format=hoj&problem_id=%d&problem_id=%d" % (
        ADMIN, contest["id"], problems["sum"]["id"], problems["nodata"]["id"])
    body = client.get(url, headers=admin_headers).json()
    assert {p["ident"]: (p["included"], [w["code"] for w in p["warnings"]]) for p in body["problems"]} == {
        "sum": (True, ["flags_dropped"]), "nodata": (False, ["no_testdata"]),
    }
    archive = export_zip(client, url.replace("/preview", ""), admin_headers)
    assert archive.namelist() == ["sum.txt"]


def test_nothing_to_export(client: TestClient, contest: dict, admin_headers: dict, fake_format: None) -> None:
    problems = setup_problems(client, contest, admin_headers)
    url = "%s/problems/%d/judge-export?format=hoj" % (ADMIN, problems["ext"]["id"])
    response = client.get(url, headers=admin_headers)
    assert response.status_code == 400
    assert response.json()["details"]["code_detail"] == "no_exportable"


def test_problem_ids_must_belong_to_the_contest(client: TestClient, contest: dict, admin_headers: dict) -> None:
    other = make_contest(client, admin_headers, "other")
    problems = setup_problems(client, contest, admin_headers)
    url = "%s/contests/%d/judge-export?format=syncoj&problem_id=%d" % (ADMIN, other["id"], problems["sum"]["id"])
    assert client.get(url, headers=admin_headers).status_code == 404


def test_export_formats_are_validated(client: TestClient, contest: dict, admin_headers: dict) -> None:
    url = "%s/contests/%d/judge-export?format=qduoj" % (ADMIN, contest["id"])
    assert client.get(url, headers=admin_headers).status_code == 422
