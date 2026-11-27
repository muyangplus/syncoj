"""题型的服务端一侧（§12.13）与评测状态（§12.5）：题型列与数据包一致、按能力派任务、提交答案题
的回收与领取、取消（CA）、阶段（CP / JG）、失败分 SF / SE、外部结果的状态别名。
"""

from __future__ import annotations

import json

import pytest

from conftest import do_tick as tick
from test_builtin_judge import (
    AB, make_worker, needs_gcc, scores, set_builtin, submit, upload, zip_bytes,
)
from syncoj_server.judging import worker
from syncoj_server.services import judge as judge_service

GRADER_DATA = zip_bytes({"1.in": "1 2\n", "1.ans": "3\n", "grader.cpp": "int main(){}", "add.h": "",
                         "config.json": json.dumps({"type": "grader"})})
ANSWER_DATA = zip_bytes({"1.in": "", "1.ans": "1\n", "2.in": "", "2.ans": "2\n"})


@pytest.fixture()
def problem(client, admin_headers, contest) -> dict:
    response = client.post("/api/v1/admin/contests/%d/problems" % contest["id"], json=[{"ident": "ab"}],
                           headers=admin_headers)
    return response.json()["problems"][0]


@pytest.fixture()
def worker_token(client, admin_headers) -> str:
    return client.post("/api/v1/admin/judge-workers", json={"name": "w"}, headers=admin_headers).json()["token"]


def poll(client, token, features=None):
    body = {"version": "0.5.0", "want": 1, "boot_id": "b1"}
    if features is not None:
        body["features"] = features
    response = client.post("/api/v1/judge/poll", json=body, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.text
    return response.json()["task"]


def queue(client, admin_headers, contest):
    return client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], json={"force": True},
                       headers=admin_headers).json()


def progress(client, admin_headers, contest):
    return client.get("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], headers=admin_headers).json()


def test_数据包题型与题目不一致_422_改为并上传(client, admin_headers, problem) -> None:
    set_builtin(client, admin_headers, problem)
    response = upload(client, admin_headers, problem, GRADER_DATA)
    assert response.status_code == 422 and response.json()["code"] == "testdata_type_mismatch"
    assert response.json()["details"] == {"problem_type": "traditional", "declared": "grader"}
    ok = client.post("/api/v1/admin/problems/%d/testdata?problem_type=grader" % problem["id"],
                     files={"file": ("g.zip", GRADER_DATA, "application/zip")}, headers=admin_headers)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["problem_type"] == "grader" and body["testdata"]["grader_files"] == ["add.h"]


def test_改题型时按新题型校验已有数据包(client, admin_headers, problem) -> None:
    set_builtin(client, admin_headers, problem)
    assert upload(client, admin_headers, problem).status_code == 200
    bad = set_builtin(client, admin_headers, problem, problem_type="interactive")
    assert bad.status_code == 422 and bad.json()["code"] == "testdata_invalid_for_type"
    conflict = set_builtin(client, admin_headers, problem, problem_type="interactive", io_mode="file")
    assert conflict.json()["code"] == "io_mode_conflict"
    answer = set_builtin(client, admin_headers, problem, problem_type="answer_only", compile_flags="-O2")
    assert answer.status_code == 200
    body = answer.json()
    assert body["problem_type"] == "answer_only" and body["time_limit_ms"] is None and body["compile_flags"] is None
    assert body["file_patterns"] == ["{ident}/{ident}*.out"]
    assert body["testdata"]["answer_count"] == 2 and body["testdata"]["answer_exts"] == [".out"]
    # 不传题型 = 不改
    assert set_builtin(client, admin_headers, problem).json()["problem_type"] == "answer_only"
    assert set_builtin(client, admin_headers, problem, compare_mode="strict").json()["compare_mode"] == "strict"


def test_按能力派任务_旧评测机只领传统题(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    client.post("/api/v1/admin/problems/%d/testdata?problem_type=grader" % problem["id"],
                files={"file": ("g.zip", GRADER_DATA, "application/zip")}, headers=admin_headers)
    queue(client, admin_headers, contest)
    headers = {"Authorization": "Bearer " + worker_token}
    assert client.post("/api/v1/judge/lease", headers=headers).status_code == 204
    assert poll(client, worker_token, ["slots"]) is None
    state = progress(client, admin_headers, contest)
    assert state["queued"] == 1 and state["unservable"] == {"grader": 1}
    task = poll(client, worker_token, ["slots", "type:grader"])
    assert task is not None and task["problem"]["problem_type"] == "grader" and task["attempt"] == 1
    workers = client.get("/api/v1/admin/judge-workers", headers=admin_headers).json()["items"]
    assert workers[0]["types"] == ["traditional", "grader"]


def test_提交答案题_回收答案文件_领到清单(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    set_builtin(client, admin_headers, problem, problem_type="answer_only")
    assert client.post("/api/v1/admin/problems/%d/testdata" % problem["id"],
                       files={"file": ("a.zip", ANSWER_DATA, "application/zip")},
                       headers=admin_headers).status_code == 200
    reply = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert ".out" in reply["config"]["extensions"]
    from conftest import scan_entry

    seen = tick(client, enrolled["token"], [scan_entry("ab/a.out", b"x"), scan_entry("ab/sub/ab1.out", b"9")],
                machine_id=enrolled["machine_id"])
    assert seen["need_upload"] == []  # 编译产物 a.out、子目录里的同名文件都不归这道题
    submit(client, enrolled, {"ab/ab1.out": b"1\n"})
    queue(client, admin_headers, contest)
    task = poll(client, worker_token, ["type:answer_only"])
    assert task is not None
    sources = {item["case"]: item for item in task["sources"]}
    assert sources["1"]["path"] == "ab/ab1.out" and sources["2"]["sha256"] is None
    headers = {"Authorization": "Bearer " + worker_token}
    blob = client.get("/api/v1/judge/tasks/%d/blobs/%s" % (task["id"], sources["1"]["sha256"]), headers=headers)
    assert blob.status_code == 200 and blob.content == b"1\n"


def test_提交答案题_答案超过单文件上限时提醒(client, admin_headers, problem) -> None:
    set_builtin(client, admin_headers, problem, problem_type="answer_only")
    limit = client.app.state.ctx.settings.max_file_size
    data = zip_bytes({"1.in": "", "1.ans": "x" * (limit + 1)})
    body = client.post("/api/v1/admin/problems/%d/testdata" % problem["id"],
                       files={"file": ("a.zip", data, "application/zip")}, headers=admin_headers).json()
    assert [w["code"] for w in body["testdata"]["warnings"]] == ["answer_too_large"]


def test_评测侧box号段校验(tmp_path) -> None:
    token = tmp_path / "token"
    token.write_text("t", encoding="utf-8")
    base = ["--server", "http://127.0.0.1:9", "--token-file", str(token), "--backend", "isolate",
            "--cache-dir", str(tmp_path / "c")]
    assert worker.main(base + ["--box-id", "0", "--slots", "2", "--trusted-box-id", "1"]) == 2
    assert worker.main(base + ["--box-id", "600", "--trusted-box-id", "1000"]) == 2


def test_没有提交答案题的场次不多收扩展名(client, admin_headers, problem, enrolled) -> None:
    reply = tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert ".out" not in reply["config"]["extensions"]


@needs_gcc
def test_提交答案题_端到端(client, admin_headers, contest, problem, enrolled, worker_token, tmp_path) -> None:
    set_builtin(client, admin_headers, problem, problem_type="answer_only")
    client.post("/api/v1/admin/problems/%d/testdata" % problem["id"],
                files={"file": ("a.zip", ANSWER_DATA, "application/zip")}, headers=admin_headers)
    submit(client, enrolled, {"ab/ab1.out": b"1\n"})
    queue(client, admin_headers, contest)
    judge = make_worker(client, worker_token, tmp_path)
    assert judge.run_once() is True
    cell = scores(client, admin_headers, contest)[("S001", "ab")]
    assert (cell["score"], cell["status"]) == (50, "WA")
    detail = client.get("/api/v1/admin/contests/%d/judge/runs/%d" % (contest["id"], cell["run_id"]),
                        headers=admin_headers).json()
    assert detail["problem_type"] == "answer_only"
    assert [case["file"] for case in detail["cases"]] == ["ab1.out", "ab2.out"]


def test_取消任务_CA_交结果被丢弃(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    queue(client, admin_headers, contest)
    task = poll(client, worker_token, [])
    headers = {"Authorization": "Bearer " + worker_token}
    assert client.post("/api/v1/judge/tasks/%d/phase" % task["id"], json={"phase": "compiling"},
                       headers=headers).status_code == 200
    assert progress(client, admin_headers, contest)["compiling"] == 1
    listed = client.get("/api/v1/admin/contests/%d/judge/tasks" % contest["id"], headers=admin_headers).json()
    assert listed["items"][0]["phase"] == "compiling"
    cancelled = client.post("/api/v1/admin/contests/%d/judge/builtin/cancel" % contest["id"], json={},
                            headers=admin_headers).json()
    assert cancelled["cancelled"] == 1
    late = client.post("/api/v1/judge/tasks/%d/result" % task["id"],
                       json={"score": 100, "max_score": 100, "status": "AC"}, headers=headers)
    assert late.status_code == 404
    state = progress(client, admin_headers, contest)
    assert state["cancelled"] == 1 and state["running"] == 0
    listed = client.get("/api/v1/admin/contests/%d/judge/tasks" % contest["id"], headers=admin_headers).json()
    assert listed["items"][0]["status"] == "cancelled" and listed["items"][0]["outcome"] == "CA"


def test_取消的任务不算评过_智能模式照样再排(client, admin_headers, contest, problem, enrolled) -> None:
    from syncoj_server.services import smart_judge

    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    queue(client, admin_headers, contest)
    client.post("/api/v1/admin/contests/%d/judge/builtin/cancel" % contest["id"], json={}, headers=admin_headers)
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        from syncoj_server.models import Contest

        row = session.get(Contest, contest["id"])
        pairs, _activity = smart_judge.contest_inputs(session, row, ctx.settings)
    assert [pair.covered for pair in pairs] == [False]


def test_失败分SF与SE(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    queue(client, admin_headers, contest)
    client.delete("/api/v1/admin/problems/%d/testdata" % problem["id"], headers=admin_headers)
    assert poll(client, worker_token, []) is None  # 题目没有数据包：题目配置失败
    state = progress(client, admin_headers, contest)
    assert state["failed"] == 1 and state["submit_failed"] == 1 and state["failures"][0]["code"] == "SF"


def test_外部结果的状态别名规整() -> None:
    assert judge_service._as_status("Partially Correct") == "PA"
    assert judge_service._as_status("PC") == "PA"
    assert judge_service._as_status("Partial Accepted") == "PA"
    assert judge_service._as_status("Presentation Error") == "PE"
    assert judge_service._as_status("WA") == "WA"
    assert judge_service._worst_status(["AC", "PA", "PE"]) == "PE"
    assert judge_service._worst_status(["PA", "AC"]) == "PA"


def test_旧评测机不报能力也能评传统题(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    queue(client, admin_headers, contest)
    assert poll(client, worker_token) is not None


def test_评测机报题型能力(tmp_path) -> None:
    assert "type:interactive" in worker.features_for("isolate")
    assert "type:interactive" not in worker.features_for("rlimit")  # 评测侧目录与选手同账号
    assert "type:interactive" not in worker.features_for("winjob")
    assert {"type:grader", "type:answer_only"} <= set(worker.features_for("winjob"))
