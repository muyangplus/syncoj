"""导入导出里的题型与比较方式（§12.13、§12.16）：原生格式 v2 往返、旧版本号、各格式的反向映射。

数据包都是现造的（problem_io_fixtures 风格），不用任何真实比赛数据。
"""

from __future__ import annotations

import io
import json
import zipfile

from fastapi.testclient import TestClient

from test_judge_export_api import export_zip
from test_judge_import_api import ADMIN, accept_all, apply, make_contest, preview


def _zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


ANSWER_DATA = _zip({"1.in": "", "1.ans": "1\n", "2.in": "", "2.ans": "2\n"})
GRADER_DATA = _zip({"1.in": "1 2\n", "1.ans": "3\n", "grader.cpp": "int main(){}", "add.h": "int add(int,int);"})
INTER_DATA = _zip({"1.in": "5\n", "interactor.cpp": "int main(){}"})


def _typed_problems(client: TestClient, contest: dict, headers: dict) -> dict:
    rows = client.post("%s/contests/%d/problems" % (ADMIN, contest["id"]),
                       json=[{"ident": "ans"}, {"ident": "add"}, {"ident": "guess"}, {"ident": "plain"}],
                       headers=headers).json()["problems"]
    by_ident = {row["ident"]: row for row in rows}
    for ident, kind, data in (("ans", "answer_only", ANSWER_DATA), ("add", "grader", GRADER_DATA),
                              ("guess", "interactive", INTER_DATA)):
        client.put("%s/problems/%d/judge" % (ADMIN, by_ident[ident]["id"]),
                   json={"judge_mode": "builtin", "problem_type": kind}, headers=headers)
        response = client.post("%s/problems/%d/testdata" % (ADMIN, by_ident[ident]["id"]),
                               files={"file": ("%s.zip" % ident, data)}, headers=headers)
        assert response.status_code == 200, response.text
    response = client.put("%s/problems/%d/judge" % (ADMIN, by_ident["plain"]["id"]),
                          json={"judge_mode": "builtin", "compare_mode": "strict", "io_mode": "auto"},
                          headers=headers)
    assert response.status_code == 200, response.text
    client.post("%s/problems/%d/testdata" % (ADMIN, by_ident["plain"]["id"]),
                files={"file": ("p.zip", _zip({"1.in": "1\n", "1.ans": "1\n"}))}, headers=headers)
    return by_ident


def _columns(client: TestClient, contest_id: int, headers: dict) -> dict:
    items = client.get("%s/contests/%d/problems" % (ADMIN, contest_id), headers=headers).json()["items"]
    return {row["ident"]: (row["problem_type"], row["compare_mode"], row["io_mode"],
                           (row["testdata"] or {}).get("sha256")) for row in items}


def test_原生格式_非传统题写v2_往返无损(client: TestClient, contest: dict, admin_headers: dict) -> None:
    _typed_problems(client, contest, admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=syncoj" % (ADMIN, contest["id"]), admin_headers)
    manifest = json.loads(archive.read("syncoj-problems.json"))
    assert manifest["version"] == 2
    kinds = {p["ident"]: (p["problem_type"], p["compare_mode"]) for p in manifest["problems"]}
    assert kinds["ans"] == ("answer_only", "both") and kinds["plain"] == ("traditional", "strict")

    target = make_contest(client, admin_headers, "types-round-trip")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name in archive.namelist():
            out.writestr(name, archive.read(name))
    body = preview(client, target, admin_headers, buffer.getvalue()).json()
    assert all(not p["errors"] for p in body["problems"]), body
    assert {p["ident"]: p["problem_type"] for p in body["problems"]}["guess"] == "interactive"
    apply(client, target, admin_headers, body["token"], accept_all(body))
    assert _columns(client, target["id"], admin_headers) == _columns(client, contest["id"], admin_headers)


def test_原生格式_只有传统题仍写v1(client: TestClient, contest: dict, admin_headers: dict) -> None:
    client.post("%s/contests/%d/problems" % (ADMIN, contest["id"]), json=[{"ident": "a"}], headers=admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=syncoj" % (ADMIN, contest["id"]), admin_headers)
    assert json.loads(archive.read("syncoj-problems.json"))["version"] == 1


def test_原生格式_比现在新的版本拒收(client: TestClient, contest: dict, admin_headers: dict) -> None:
    data = _zip({"syncoj-problems.json": json.dumps({"format": "syncoj-judge", "version": 3, "problems": []})})
    response = preview(client, contest, admin_headers, data)
    assert response.status_code == 400 and response.json()["details"]["code_detail"] == "version_unsupported"


def test_LemonLime导出_题型反向映射(client: TestClient, contest: dict, admin_headers: dict) -> None:
    _typed_problems(client, contest, admin_headers)
    response = client.get("%s/contests/%d/judge-export?format=lemonlime&dry_run=true" % (ADMIN, contest["id"]),
                          headers=admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=lemonlime" % (ADMIN, contest["id"]),
                         admin_headers)
    cdf = json.loads(archive.read("contest/contest.cdf"))
    tasks = {task["sourceFileName"]: task for task in cdf["tasks"]}
    assert "guess" not in tasks  # 交互题 LemonLime 没有对应
    assert tasks["ans"]["taskType"] == 1 and tasks["add"]["taskType"] == 2
    assert tasks["add"]["grader"] == "add/grader.cpp" and tasks["add"]["interactorName"] == "add.h"
    assert tasks["plain"]["comparisonMode"] == 0
    assert response.status_code == 200


def test_Hydro导出_题型反向映射(client: TestClient, contest: dict, admin_headers: dict) -> None:
    from syncoj_server.services.problem_io import yaml_lite

    _typed_problems(client, contest, admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=hydro" % (ADMIN, contest["id"]), admin_headers)

    def config(ident: str) -> dict:
        return yaml_lite.load(archive.read("%s/testdata/config.yaml" % ident))

    assert config("ans")["type"] == "submit_answer"
    assert config("guess")["type"] == "interactive" and config("guess")["interactor"] == "interactor.cpp"
    assert config("add")["user_extra_files"] == ["grader.cpp", "add.h"]
    assert config("plain")["checker_type"] == "strict"


def test_HOJ导出_交互题写judgeMode_其余题型跳过(client: TestClient, contest: dict, admin_headers: dict) -> None:
    _typed_problems(client, contest, admin_headers)
    archive = export_zip(client, "%s/contests/%d/judge-export?format=hoj" % (ADMIN, contest["id"]), admin_headers)
    docs = [json.loads(archive.read(n)) for n in archive.namelist() if n.endswith(".json")]
    modes = {doc["problem"]["problemId"]: doc["problem"]["judgeMode"] for doc in docs}
    assert modes.get("GUESS") == "interactive" and "ANS" not in modes and "ADD" not in modes
    plain = next(doc for doc in docs if doc["problem"]["problemId"] == "PLAIN")
    assert plain["problem"]["isRemoveEndBlank"] is False
