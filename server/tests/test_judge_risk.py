"""风险标注与筛选（§12.11）：runner 记风险事件、分级、计数入库、列表筛选、明细与源码接口、迁移 024。

runner 一侧用 rlimit 后端真编译真运行（需要 g++）；逃出进程组只在以 root 跑、能换成专用账号时测。
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from syncoj_server import migrations
from syncoj_server.judging import precheck, runner, sandbox

from test_builtin_judge import AB, scores, set_builtin, submit, upload
from test_judge_workers import bearer, poll
from test_judging_runner import ab_data, factory

needs_gcc = pytest.mark.skipif(
    shutil.which("g++") is None or sys.platform == "win32", reason="需要 Linux + g++"
)


def judge_with(tmp_path: Path, code: str, data: Path, make=None) -> runner.JudgeOutcome:
    source = tmp_path / "ab.cpp"
    source.write_text(code, encoding="utf-8")
    return runner.judge(source, "ab.cpp", data, runner.ProblemConfig(ident="ab"), make or factory(tmp_path))


def codes(outcome: runner.JudgeOutcome) -> list:
    return sorted({event["code"] for event in outcome.risk_events})


# --------------------------------------------------------------------------- #
# 分级
# --------------------------------------------------------------------------- #


def test_分级_风险事件与拦截类为风险_预警类为疑似违规() -> None:
    for code in ("workdir_symlink", "tamper_checker", "escaped_process", "precheck_blocked"):
        assert precheck.level_of(code) == "risk"
    for code in precheck.BLOCK_RULES:
        assert precheck.level_of(code) == "risk"
    for code in precheck.WARN_RULES:
        assert precheck.level_of(code) == "warning"
    assert precheck.level_of("没见过的") == "warning"


def test_计数_拦截类出现在预警里也算风险() -> None:
    document = {
        "precheck_warnings": [{"code": "pragma", "line": 1}, {"code": "long_line", "line": 2}, "坏项", {}],
        "risk_events": [{"code": "workdir_symlink", "case": "1"}, {"message": "没有 code"}],
    }
    assert precheck.count_flags(document) == (1, 2)
    assert precheck.count_flags({"score": 1}) == (0, 0)
    assert precheck.count_flags_json(json.dumps(document)) == (1, 2)
    assert precheck.count_flags_json("{坏的") == (0, 0)
    assert precheck.count_flags_json(None) == (0, 0)


# --------------------------------------------------------------------------- #
# runner：风险事件
# --------------------------------------------------------------------------- #


def test_工作目录检查_各种痕迹() -> None:
    import hashlib
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        (work / "prog").write_bytes(b"new")
        (work / "a").write_bytes(b"x")
        os.link(str(work / "a"), str(work / "b"))
        os.symlink("x", str(work / "y"))
        os.mkfifo(str(work / "fifo"))
        (work / "sub").mkdir()
        (work / "sub" / "checker").write_bytes(b"#!/bin/sh\n")
        events = runner.inspect_workdir(work, "3", "prog", hashlib.sha256(b"old").hexdigest())
    by_code = {event.code: event for event in events}
    assert set(by_code) == {"workdir_symlink", "tamper_checker"}
    assert by_code["workdir_symlink"].case == "3"
    for part in ("y（符号链接）", "fifo（FIFO）", "硬链接 2"):
        assert part in by_code["workdir_symlink"].message
    assert "sub/checker" in by_code["tamper_checker"].message and "prog 被改写" in by_code["tamper_checker"].message


def test_工作目录干净_没有风险事件(tmp_path) -> None:
    import hashlib

    (tmp_path / "prog").write_bytes(b"same")
    (tmp_path / "input.txt").write_bytes(b"1 2\n")
    assert runner.inspect_workdir(tmp_path, "1", "prog", hashlib.sha256(b"same").hexdigest()) == []


@needs_gcc
def test_程序造符号链接_记workdir_symlink_不改判分(tmp_path) -> None:
    code = ("#include <cstdio>\n#include <unistd.h>\nint main(){symlink(\"x\",\"y\");long long a,b;"
            "scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n")
    outcome = judge_with(tmp_path, code, ab_data(tmp_path))
    assert outcome.status == "AC" and outcome.score == 100
    assert codes(outcome) == ["workdir_symlink"]
    assert sorted(event["case"] for event in outcome.risk_events) == ["1", "10", "2"]
    assert outcome.to_result_json()["risk_events"] == outcome.risk_events


@needs_gcc
def test_程序写chk文件_记tamper_checker(tmp_path) -> None:
    code = ("#include <cstdio>\nint main(){FILE*f=fopen(\"chk\",\"w\");if(f){fputs(\"x\",f);fclose(f);}"
            "long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n")
    data = ab_data(tmp_path, {"config.json": json.dumps({"precheck": {"disabled": ["file_access"]}})})
    outcome = judge_with(tmp_path, code, data)
    assert outcome.status == "AC"
    assert codes(outcome) == ["tamper_checker"]
    assert "chk" in outcome.risk_events[0]["message"]


@needs_gcc
def test_正常程序没有风险事件(tmp_path) -> None:
    outcome = judge_with(tmp_path, AB.decode("utf-8"), ab_data(tmp_path))
    assert outcome.status == "AC" and outcome.risk_events == []
    assert "risk_events" not in outcome.to_result_json()


def _spare_account():
    """逃逸测试要按账号杀进程：挑一个别的测试不用的系统账号（并行跑的测试用的是 nobody）。"""
    for name in ("daemon", "bin", "sys"):
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            continue
        if entry.pw_uid != 0:
            return name
    return None


@needs_gcc
@pytest.mark.skipif(not (hasattr(os, "geteuid") and os.geteuid() == 0), reason="要以 root 跑才能换账号")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_fork加setsid逃出进程组_记escaped_process(tmp_path) -> None:
    account = _spare_account()
    if account is None:
        pytest.skip("没有可用的专用账号")
    os.chmod(str(tmp_path), 0o755)
    code = ("#include <cstdio>\n#include <unistd.h>\nint main(){if(fork()==0){setsid();sleep(30);return 0;}"
            "long long a,b;scanf(\"%lld%lld\",&a,&b);printf(\"%lld\\n\",a+b);}\n")
    data = ab_data(tmp_path, {"config.json": json.dumps({"precheck": {"disabled": ["syscall"]}})})
    outcome = judge_with(
        tmp_path, code, data,
        make=lambda: sandbox.RlimitSandbox(base=tmp_path, run_as=account, kill_strays=True),
    )
    assert outcome.status == "AC"
    escaped = [event for event in outcome.risk_events if event["code"] == "escaped_process"]
    assert sorted(event["case"] for event in escaped) == ["1", "10", "2"]
    assert "清掉 1 个" in escaped[0]["message"]


@needs_gcc
def test_正常程序不算逃出进程组(tmp_path) -> None:
    account = _spare_account()
    if account is None or not (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("要以 root 跑、有专用账号")
    os.chmod(str(tmp_path), 0o755)
    slow = "#include <cstdio>\nint main(){volatile long long x=0;for(;;)x++;}\n"
    outcome = judge_with(
        tmp_path, slow, ab_data(tmp_path),
        make=lambda: sandbox.RlimitSandbox(base=tmp_path, run_as=account, kill_strays=True),
    )
    assert outcome.status == "TLE" and outcome.risk_events == []


def test_源码预检拦截_作为风险事件带行号(tmp_path) -> None:
    def no_sandbox():
        raise AssertionError("拦截后不该再编译")

    code = "#include <cstdio>\n#pragma GCC optimize(2)\nint main(){system(\"ls\");}\n"
    outcome = judge_with(tmp_path, code, ab_data(tmp_path), make=no_sandbox)
    assert outcome.status == "CE"
    events = [(e["code"], e["line"], e["rule"], e["case"]) for e in outcome.risk_events]
    assert events == [("precheck_blocked", 2, "pragma", None), ("precheck_blocked", 3, "syscall", None)]


# --------------------------------------------------------------------------- #
# 服务端：计数入库、筛选、明细
# --------------------------------------------------------------------------- #

RISKY_DETAIL = {
    "precheck_warnings": [
        {"code": "long_line", "line": 2, "message": "单行 900 个字符", "excerpt": "int a[]={"},
        {"code": "pragma", "line": 1, "message": "禁止使用 #pragma", "excerpt": "#pragma GCC"},
    ],
    "risk_events": [{"code": "workdir_symlink", "case": "1", "message": "工作目录里出现 y（符号链接）"}],
}


@pytest.fixture()
def judged(client, admin_headers, contest, enrolled, issued):
    """S001 交了代码，评测机领走任务、交回一份带预警与风险事件的结果。"""
    problem = client.post("/api/v1/admin/contests/%d/problems" % contest["id"],
                          json=[{"ident": "ab"}], headers=admin_headers).json()["problems"][0]
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], json={"force": True}, headers=admin_headers)
    task = poll(client, issued["token"], want=1)["task"]
    response = client.post(
        "/api/v1/judge/tasks/%d/result" % task["id"],
        json={"score": 100, "max_score": 100, "status": "AC", "detail": RISKY_DETAIL},
        headers=bearer(issued["token"]),
    )
    assert response.status_code == 200, response.text
    return task


@pytest.fixture()
def issued(client, admin_headers) -> dict:
    return client.post("/api/v1/admin/judge-workers", json={"name": "评测机1"}, headers=admin_headers).json()


def test_计数入库_任务与成绩记录都带(client, admin_headers, contest, judged) -> None:
    base = "/api/v1/admin/contests/%d" % contest["id"]
    tasks = client.get(base + "/judge/tasks", headers=admin_headers).json()["items"]
    assert [(t["warning_count"], t["risk_count"]) for t in tasks] == [(1, 2)]

    cell = scores(client, admin_headers, contest)[("S001", "ab")]
    assert (cell["warning_count"], cell["risk_count"]) == (1, 2)
    runs = client.get(base + "/judge/runs", headers=admin_headers).json()["items"]
    assert [(r["warning_count"], r["risk_count"]) for r in runs] == [(1, 2)]


def test_明细带分级与风险事件(client, admin_headers, contest, judged) -> None:
    base = "/api/v1/admin/contests/%d" % contest["id"]
    detail = client.get(base + "/judge/tasks/%d" % judged["id"], headers=admin_headers).json()
    assert [(item["code"], item["level"]) for item in detail["precheck_warnings"]] == [
        ("long_line", "warning"), ("pragma", "risk")]
    assert detail["risk_events"] == [{"code": "workdir_symlink", "case": "1", "line": 0,
                                      "message": "工作目录里出现 y（符号链接）", "rule": "", "excerpt": "",
                                      "level": "risk"}]
    cell = scores(client, admin_headers, contest)[("S001", "ab")]
    run = client.get(base + "/judge/runs/%d" % cell["run_id"], headers=admin_headers).json()
    assert run["risk_events"][0]["code"] == "workdir_symlink" and run["task_id"] == judged["id"]


def _write_result(client, contest, player_no: str, ident: str, document: dict) -> None:
    root = Path(client.app.state.ctx.settings.judge_result_root) / contest["slug"] / player_no / ident
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.json").write_text(json.dumps(dict({"score": 10, "max_score": 100, "status": "WA"},
                                                      **document)), encoding="utf-8")


def test_成绩记录按flag筛选(client, admin_headers, contest, player) -> None:
    for ident, document in (
        ("p1", {}),
        ("p2", {"precheck_warnings": [{"code": "macro_heavy", "line": 1}]}),
        ("p3", {"risk_events": [{"code": "tamper_checker", "case": "1"}]}),
        ("p4", {"precheck_warnings": [{"code": "big_table", "line": 5}],
                "risk_events": [{"code": "escaped_process", "case": "2"}]}),
    ):
        _write_result(client, contest, player["player_no"], ident, document)
    scores(client, admin_headers, contest)
    url = "/api/v1/admin/contests/%d/judge/runs" % contest["id"]

    def listed(**params):
        body = client.get(url, params=params, headers=admin_headers).json()
        return sorted(item["problem"] for item in body["items"]), body["total"]

    assert listed() == (["p1", "p2", "p3", "p4"], 4)
    assert listed(flag="any") == (["p2", "p3", "p4"], 3)
    assert listed(flag="warning") == (["p2", "p4"], 2)
    assert listed(flag="risk") == (["p3", "p4"], 2)
    assert client.get(url, params={"flag": "nope"}, headers=admin_headers).status_code == 422


def test_评测任务按flag筛选(client, admin_headers, contest, judged) -> None:
    url = "/api/v1/admin/contests/%d/judge/tasks" % contest["id"]
    for flag, total in (("risk", 1), ("warning", 1), ("any", 1)):
        assert client.get(url, params={"flag": flag}, headers=admin_headers).json()["total"] == total
    ctx = client.app.state.ctx
    from syncoj_server.models import JudgeTask

    with ctx.db.session() as session:
        session.query(JudgeTask).one().warning_count = 0
    assert client.get(url, params={"flag": "warning"}, headers=admin_headers).json()["total"] == 0
    assert client.get(url, params={"flag": "risk"}, headers=admin_headers).json()["total"] == 1


# --------------------------------------------------------------------------- #
# 源码接口
# --------------------------------------------------------------------------- #


def test_源码接口_按任务的sha取_限1MB_跨场次404(client, admin_headers, contest, judged, monkeypatch) -> None:
    url = "/api/v1/admin/contests/%d/judge/tasks/%d/source" % (contest["id"], judged["id"])
    body = client.get(url, headers=admin_headers).json()
    assert body["content"] == AB.decode("utf-8") and body["path"] == "ab/ab.cpp"
    assert body["size"] == len(AB) and body["task_id"] == judged["id"]

    assert client.get(url).status_code == 401
    other = "/api/v1/admin/contests/%d/judge/tasks/%d/source" % (contest["id"] + 999, judged["id"])
    assert client.get(other, headers=admin_headers).status_code == 404

    from syncoj_server.api import admin_judge

    monkeypatch.setattr(admin_judge, "MAX_SOURCE_VIEW_BYTES", 10)
    response = client.get(url, headers=admin_headers)
    assert response.status_code == 413 and response.json()["code"] == "payload_too_large"


def test_源码接口_还没领到代码的任务404(client, admin_headers, contest, enrolled) -> None:
    problem = client.post("/api/v1/admin/contests/%d/problems" % contest["id"],
                          json=[{"ident": "ab"}], headers=admin_headers).json()["problems"][0]
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"], json={"force": True}, headers=admin_headers)
    task = client.get("/api/v1/admin/contests/%d/judge/tasks" % contest["id"], headers=admin_headers).json()["items"][0]
    url = "/api/v1/admin/contests/%d/judge/tasks/%d/source" % (contest["id"], task["id"])
    assert client.get(url, headers=admin_headers).status_code == 404


# --------------------------------------------------------------------------- #
# 迁移 024
# --------------------------------------------------------------------------- #


def test_迁移024_补列并按老结果回填计数(workdir: Path) -> None:
    engine = create_engine("sqlite:///%s" % (workdir / "v23.db"))
    risky = json.dumps(dict({"score": 0}, **RISKY_DETAIL))
    with engine.begin() as conn:
        for statement in (
            "CREATE TABLE judge_worker (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL,"
            " token_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL)",
            "CREATE TABLE judge_task (id INTEGER PRIMARY KEY, contest_id INTEGER, player_id INTEGER,"
            " problem VARCHAR(64), status VARCHAR(16), result_json TEXT)",
            "CREATE TABLE judge_run (id INTEGER PRIMARY KEY, contest_id INTEGER, player_id INTEGER,"
            " problem VARCHAR(64), raw_json TEXT)",
            "INSERT INTO judge_worker VALUES (1, '老评测机', 'x', '2026-01-01')",
            "PRAGMA user_version = 23",
        ):
            conn.execute(text(statement))
        conn.execute(text("INSERT INTO judge_task VALUES (1, 1, 1, 'ab', 'done', :r), (2, 1, 1, 'ab', 'queued', NULL)"),
                     {"r": risky})
        conn.execute(text("INSERT INTO judge_run VALUES (1, 1, 1, 'ab', :r), (2, 1, 2, 'ab', '{\"score\": 1}'),"
                          " (3, 1, 3, 'ab', 'not json')"), {"r": risky})

    applied = migrations.migrate(engine)

    with engine.connect() as conn:
        tasks = conn.execute(text("SELECT id, warning_count, risk_count FROM judge_task ORDER BY id")).all()
        runs = conn.execute(text("SELECT id, warning_count, risk_count FROM judge_run ORDER BY id")).all()
        worker = conn.execute(text("SELECT desired_slots, features FROM judge_worker")).one()
    assert migrations.current_version(engine) == migrations.LATEST_VERSION
    assert migrations.migrate(engine) == []
    engine.dispose()
    assert any(step.startswith("024 ") for step in applied)
    assert [tuple(row) for row in tasks] == [(1, 1, 2), (2, 0, 0)]
    assert [tuple(row) for row in runs] == [(1, 1, 2), (2, 0, 0), (3, 0, 0)]
    assert tuple(worker) == (None, None)
