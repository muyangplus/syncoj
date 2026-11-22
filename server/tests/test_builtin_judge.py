"""自带评测的服务端一侧（§12）：数据包上传、排任务、评测机接口、结果进成绩矩阵。

评测机用真的 :class:`judging.worker.Worker`（rlimit 后端、真 g++），HTTP 换成 TestClient。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import agent_headers, do_tick as tick, scan_entry as entry, sha256_of
from syncoj_server.judging import sandbox, worker

needs_gcc = pytest.mark.skipif(shutil.which("g++") is None or sys.platform == "win32", reason="需要 Linux + g++")

AB = b"#include <cstdio>\nint main(){long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n"


def zip_bytes(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


AB_DATA = zip_bytes({"1.in": "1 2\n", "1.ans": "3\n", "2.in": "5 7\n", "2.ans": "12\n"})


def submit(client, enrolled, files: dict) -> None:
    tick(client, enrolled["token"], [entry(p, d) for p, d in files.items()], machine_id=enrolled["machine_id"])
    for path, data in files.items():
        response = client.post(
            "/api/v1/agent/files", data={"path": path, "sha256": sha256_of(data)},
            files={"file": ("code.bin", data, "application/octet-stream")},
            headers=agent_headers(enrolled["token"]),
        )
        assert response.status_code == 200, response.text


@pytest.fixture()
def problem(client: TestClient, admin_headers: dict, contest: dict) -> dict:
    response = client.post("/api/v1/admin/contests/%d/problems" % contest["id"],
                           json=[{"ident": "ab"}], headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()["problems"][0]


def set_builtin(client, admin_headers, problem, **extra) -> dict:
    body = {"judge_mode": "builtin", "time_limit_ms": 1000, "memory_limit_mb": 128, "io_mode": "stdio"}
    body.update(extra)
    response = client.put("/api/v1/admin/problems/%d/judge" % problem["id"], json=body, headers=admin_headers)
    return response


def upload(client, admin_headers, problem, data: bytes = AB_DATA):
    return client.post("/api/v1/admin/problems/%d/testdata" % problem["id"],
                       files={"file": ("ab.zip", data, "application/zip")}, headers=admin_headers)


@pytest.fixture()
def worker_token(client: TestClient, admin_headers: dict) -> str:
    response = client.post("/api/v1/admin/judge-workers", json={"name": "评测机1"}, headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()["token"]


class ClientTransport(worker.Transport):
    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self.token = token

    def request(self, method, path, body=None):
        response = self.client.request(method, path, json=body, headers={"Authorization": "Bearer " + self.token})
        return response.status_code, response.content


def make_worker(client, token, tmp_path) -> worker.Worker:
    run_as = "nobody" if hasattr(os, "geteuid") and os.geteuid() == 0 else None
    base = tmp_path / "boxes"
    base.mkdir()
    os.chmod(str(base), 0o755)
    return worker.Worker(ClientTransport(client, token),
                         lambda: sandbox.RlimitSandbox(base=base, run_as=run_as), tmp_path / "cache")


def scores(client, admin_headers, contest) -> dict:
    client.post("/api/v1/admin/contests/%d/judge/rescan" % contest["id"], headers=admin_headers)
    body = client.get("/api/v1/admin/contests/%d/scores" % contest["id"], headers=admin_headers).json()
    return {(row["player_no"], cell["problem"]): cell for row in body["rows"] for cell in row["cells"]}


# --------------------------------------------------------------------------- #


def test_数据包上传时校验_摘要回到题目上(client, admin_headers, problem) -> None:
    bad = upload(client, admin_headers, problem, zip_bytes({"1.in": "x"}))
    assert bad.status_code == 400 and "答案" in bad.json()["detail"]
    assert upload(client, admin_headers, problem, b"not a zip").status_code == 400

    body = upload(client, admin_headers, problem).json()
    assert body["testdata"]["cases"] == 2 and body["testdata"]["total_score"] == 100
    listed = client.get("/api/v1/admin/contests/%d/problems" % problem["contest_id"], headers=admin_headers).json()
    assert listed["items"][0]["testdata"]["name"] == "ab.zip"


def test_编译参数只收白名单(client, admin_headers, problem) -> None:
    assert set_builtin(client, admin_headers, problem, compile_flags="-std=c++17 -O2 -DONLINE").status_code == 200
    for bad in ("-o /etc/passwd", "-fplugin=x.so", "-B/tmp", "-specs=x"):
        assert set_builtin(client, admin_headers, problem, compile_flags=bad).status_code == 400, bad


def test_考试进行中不评_除非确认(client, admin_headers, contest, problem) -> None:
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    url = "/api/v1/admin/contests/%d/judge/builtin" % contest["id"]
    response = client.post(url, json={}, headers=admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "contest_in_progress"
    assert client.post(url, json={"force": True}, headers=admin_headers).status_code == 200


def test_没有数据包不排(client, admin_headers, contest, problem) -> None:
    set_builtin(client, admin_headers, problem)
    response = client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"],
                           json={"force": True}, headers=admin_headers)
    assert response.status_code == 400 and "数据包" in response.json()["detail"]


def test_评测机令牌只能领任务(client, admin_headers, worker_token) -> None:
    assert client.post("/api/v1/judge/lease", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/v1/judge/lease", headers={"Authorization": "Bearer " + worker_token}).status_code == 204
    assert client.get("/api/v1/admin/contests", headers={"Authorization": "Bearer " + worker_token}).status_code == 401


@needs_gcc
def test_端到端_评完进成绩矩阵(client, admin_headers, contest, problem, enrolled, worker_token, tmp_path) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    queued = client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"],
                         json={"force": True}, headers=admin_headers).json()
    assert queued["queued"] == 1

    judge = make_worker(client, worker_token, tmp_path)
    assert judge.run_once() is True
    assert judge.run_once() is False

    cell = scores(client, admin_headers, contest)[("S001", "ab")]
    assert (cell["score"], cell["max_score"], cell["status"], cell["parse_status"]) == (100, 100, "AC", "ok")
    progress = client.get("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], headers=admin_headers).json()
    assert progress["done"] == 1 and progress["failed"] == 0

    result = json.loads((Path(client.app.state.ctx.settings.judge_result_root) / "mock-1" / "S001" / "ab"
                         / "result.json").read_text(encoding="utf-8"))
    assert result["judge"] == "syncoj-builtin" and len(result["cases"]) == 2
    assert result["source_path"] == "ab/ab.cpp"

    assert cell["has_cases"] is True
    detail = client.get("/api/v1/admin/contests/%d/judge/runs/%d" % (contest["id"], cell["run_id"]),
                        headers=admin_headers).json()
    assert [case["status"] for case in detail["cases"]] == ["AC", "AC"]
    assert detail["language"] == "cpp" and detail["judged_source"] == "ab/ab.cpp"


def test_结果文件里的测试点明细可查(client, admin_headers, contest, player) -> None:
    root = Path(client.app.state.ctx.settings.judge_result_root) / contest["slug"] / player["player_no"] / "ab"
    root.mkdir(parents=True)
    (root / "result.json").write_text(json.dumps({
        "score": 50, "max_score": 100, "status": "WA", "judge": "syncoj-builtin", "isolated": False,
        "language": "cpp", "compile_message": "",
        "cases": [
            {"name": "1", "status": "AC", "score": 50, "time_ms": 3, "memory_kb": 1024, "message": ""},
            {"name": "2", "status": "WA", "score": 0, "time_ms": 4, "memory_kb": 1024, "message": "第 1 行不同"},
            "坏项",
        ],
        "subtasks": [{"index": 1, "score": 0, "max_score": 100, "cases": ["1", "2"]}],
    }), encoding="utf-8")

    cell = scores(client, admin_headers, contest)[(player["player_no"], "ab")]
    assert cell["has_cases"] is True and cell["run_id"]
    detail = client.get("/api/v1/admin/contests/%d/judge/runs/%d" % (contest["id"], cell["run_id"]),
                        headers=admin_headers).json()
    assert [(case["name"], case["status"]) for case in detail["cases"]] == [("1", "AC"), ("2", "WA")]
    assert detail["cases"][1]["message"] == "第 1 行不同"
    assert detail["subtasks"][0]["cases"] == ["1", "2"] and detail["isolated"] is False

    other = client.get("/api/v1/admin/contests/%d/judge/runs/%d" % (contest["id"] + 999, cell["run_id"]),
                       headers=admin_headers)
    assert other.status_code == 404


@needs_gcc
def test_重测替换旧结果_手工成绩不被覆盖(client, admin_headers, contest, problem, enrolled, worker_token, tmp_path) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB.replace(b"a+b", b"a-b")})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    url = "/api/v1/admin/contests/%d/judge/builtin" % contest["id"]
    client.post(url, json={"force": True}, headers=admin_headers)
    judge = make_worker(client, worker_token, tmp_path)
    judge.run_once()
    assert scores(client, admin_headers, contest)[("S001", "ab")]["status"] == "WA"

    submit(client, enrolled, {"ab/ab.cpp": AB})
    client.post(url, json={"force": True}, headers=admin_headers)
    judge.run_once()
    assert scores(client, admin_headers, contest)[("S001", "ab")]["score"] == 100


def test_没交代码的选手不写成绩(client, admin_headers, contest, problem, player, worker_token) -> None:
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], json={"force": True}, headers=admin_headers)
    assert client.post("/api/v1/judge/lease", headers={"Authorization": "Bearer " + worker_token}).status_code == 204
    progress = client.get("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], headers=admin_headers).json()
    assert progress["done"] == 1
    assert scores(client, admin_headers, contest)[("S001", "ab")]["parse_status"] == "missing"


def test_只能取自己任务的内容(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    client.post("/api/v1/admin/contests/%d/problems" % contest["id"], json=[{"ident": "cd"}], headers=admin_headers)
    submit(client, enrolled, {"ab/ab.cpp": AB, "cd/cd.cpp": b"secret"})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], json={"force": True}, headers=admin_headers)
    headers = {"Authorization": "Bearer " + worker_token}
    task = client.post("/api/v1/judge/lease", headers=headers).json()
    ok = client.get("/api/v1/judge/tasks/%d/blobs/%s" % (task["id"], task["source"]["sha256"]), headers=headers)
    assert ok.status_code == 200 and ok.content == AB
    other = client.get("/api/v1/judge/tasks/%d/blobs/%s" % (task["id"], sha256_of(b"secret")), headers=headers)
    assert other.status_code == 404


def test_评测机出错退回队列_三次后记失败(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], json={"force": True}, headers=admin_headers)
    headers = {"Authorization": "Bearer " + worker_token}
    for _ in range(3):
        task = client.post("/api/v1/judge/lease", headers=headers).json()
        assert client.post("/api/v1/judge/tasks/%d/fail" % task["id"], json={"message": "沙箱坏了"},
                           headers=headers).status_code == 200
    assert client.post("/api/v1/judge/lease", headers=headers).status_code == 204
    progress = client.get("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], headers=admin_headers).json()
    assert progress["failed"] == 1 and progress["failures"][0]["error"] == "沙箱坏了"


def test_删题回收数据包(client, admin_headers, problem) -> None:
    sha = upload(client, admin_headers, problem).json()["testdata"]["sha256"]
    blobs = client.app.state.ctx.blobs
    assert blobs.has(sha)
    client.delete("/api/v1/admin/problems/%d" % problem["id"], params={"confirm": "ab"}, headers=admin_headers)
    assert not blobs.has(sha)
