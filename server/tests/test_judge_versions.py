"""代码版本历史与按版本记的评测（§12.9）：版本记录、blob 不被回收、自动入队与去抖、
复用相同结果、手动各范围、历史版本重测不覆盖矩阵、采用、timeline 接口、迁移 020。

评测机多数用例直接走 HTTP（领任务、交一个编出来的结果），有 g++ 时另跑一条端到端。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from conftest import sha256_of
from test_builtin_judge import AB, make_worker, scores, set_builtin, submit, upload

from syncoj_server import migrations
from syncoj_server.api.admin import _reclaim_blobs
from syncoj_server.models import JudgeWorker, utcnow
from syncoj_server.services import builtin_judge

needs_gcc = pytest.mark.skipif(shutil.which("g++") is None or sys.platform == "win32", reason="需要 Linux + g++")

WA = AB.replace(b"a+b", b"a-b")


def url(contest, tail: str) -> str:
    return "/api/v1/admin/contests/%d/%s" % (contest["id"], tail)


@pytest.fixture()
def problem(client, admin_headers, contest) -> dict:
    response = client.post(url(contest, "problems"), json=[{"ident": "ab"}], headers=admin_headers)
    assert response.status_code == 200, response.text
    row = response.json()["problems"][0]
    assert set_builtin(client, admin_headers, row).status_code == 200
    assert upload(client, admin_headers, row).status_code == 200
    return row


@pytest.fixture()
def worker_token(client, admin_headers) -> str:
    response = client.post("/api/v1/admin/judge-workers", json={"name": "评测机1"}, headers=admin_headers)
    return response.json()["token"]


def wh(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def lease(client, token):
    response = client.post("/api/v1/judge/lease", headers=wh(token))
    assert response.status_code in (200, 204), response.text
    return response.json() if response.status_code == 200 else None


def finish(client, token, task, score: int, status: str = "WA") -> None:
    response = client.post("/api/v1/judge/tasks/%d/result" % task["id"], headers=wh(token),
                           json={"score": score, "max_score": 100, "status": status,
                                 "detail": {"judge": "syncoj-builtin", "cases": [
                                     {"name": "1", "status": status, "score": score}]}})
    assert response.status_code == 200, response.text


def queue(client, admin_headers, contest, **body):
    body.setdefault("force", True)
    return client.post(url(contest, "judge/builtin"), json=body, headers=admin_headers)


def versions(client, admin_headers, contest, player_id: int, problem=None) -> list:
    params = {"player_id": player_id, "limit": 500}
    if problem:
        params["problem"] = problem
    response = client.get(url(contest, "judge/versions"), params=params, headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def tasks(client, admin_headers, contest, **params) -> list:
    response = client.get(url(contest, "judge/tasks"), params=params, headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def progress(client, admin_headers, contest) -> dict:
    return client.get(url(contest, "judge/builtin"), headers=admin_headers).json()


def set_auto(client, admin_headers, contest, mode="on_change", delay=60):
    response = client.put(url(contest, "judge/auto"), json={"auto_judge": mode, "auto_judge_delay": delay},
                          headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 版本记录与内容回收
# --------------------------------------------------------------------------- #


def test_每份新内容记一个版本_旧版本内容不被回收(client, admin_headers, contest, enrolled, player) -> None:
    ctx = client.app.state.ctx
    submit(client, enrolled, {"ab/ab.cpp": WA})
    submit(client, enrolled, {"ab/ab.cpp": AB})
    items = versions(client, admin_headers, contest, player["id"])
    assert [item["sha256"] for item in items] == [sha256_of(WA), sha256_of(AB)]
    assert [item["current"] for item in items] == [False, True]
    assert items[0]["judge"] is None and items[0]["size"] == len(WA)

    # 被替换掉的旧版本仍被版本历史引用：回收时留着
    _reclaim_blobs(ctx, [sha256_of(WA)])
    assert ctx.blobs.has(sha256_of(WA))

    # 删选手：台账与历史一起没了，内容回收
    client.delete("/api/v1/admin/players/%d" % player["id"], params={"confirm": player["player_no"]},
                  headers=admin_headers)
    assert not ctx.blobs.has(sha256_of(WA)) and not ctx.blobs.has(sha256_of(AB))


def test_清空代码台账连历史一起删(client, admin_headers, contest, enrolled, player) -> None:
    ctx = client.app.state.ctx
    submit(client, enrolled, {"ab/ab.cpp": WA})
    submit(client, enrolled, {"ab/ab.cpp": AB})
    # 只清已消失的：这个文件还在，历史不动
    response = client.post(url(contest, "files/clear"), json={"confirm": contest["slug"]}, headers=admin_headers)
    assert response.status_code == 200
    assert len(versions(client, admin_headers, contest, player["id"])) == 2

    response = client.post(url(contest, "files/clear"), json={"confirm": contest["slug"], "purge": True},
                           headers=admin_headers)
    assert response.status_code == 200, response.text
    assert versions(client, admin_headers, contest, player["id"]) == []
    assert not ctx.blobs.has(sha256_of(WA))


def test_同内容重传不记新版本(client, admin_headers, contest, enrolled, player) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    submit(client, enrolled, {"ab/ab.cpp": AB})
    assert len(versions(client, admin_headers, contest, player["id"])) == 1


# --------------------------------------------------------------------------- #
# 自动入队与去抖
# --------------------------------------------------------------------------- #


def test_自动入队默认关闭(client, admin_headers, contest, problem, enrolled) -> None:
    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()["items"][0]
    assert (listed["auto_judge"], listed["auto_judge_delay"]) == ("off", 60)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    assert progress(client, admin_headers, contest)["total"] == 0


def test_自动入队_去抖只评最后一版_场次进行中也排(
    client, admin_headers, contest, problem, enrolled, player, worker_token
) -> None:
    ctx = client.app.state.ctx
    assert set_auto(client, admin_headers, contest, delay=60)["auto_judge"] == "on_change"
    submit(client, enrolled, {"ab/ab.cpp": WA})
    submit(client, enrolled, {"ab/ab.cpp": AB})
    state = progress(client, admin_headers, contest)
    assert (state["queued"], state["delayed"]) == (1, 1)
    assert lease(client, worker_token) is None, "去抖时间没到，不发给评测机"

    queued = tasks(client, admin_headers, contest)
    assert len(queued) == 1 and queued[0]["trigger"] == "auto" and queued[0]["not_before"]

    with ctx.db.session() as session:
        worker_id = session.query(JudgeWorker).one().id
    leased = builtin_judge.lease_next(ctx, worker_id, now=utcnow() + timedelta(seconds=120))
    assert leased is not None and leased.task.source_sha256 == sha256_of(AB)


def test_自动入队只排自带评测且有数据包的题(client, admin_headers, contest, enrolled) -> None:
    rows = client.post(url(contest, "problems"), json=[
        {"ident": "ab", "file_patterns": ["{ident}/**"]}, {"ident": "cd"}, {"ident": "ef"},
    ], headers=admin_headers).json()["problems"]
    set_builtin(client, admin_headers, rows[0])
    upload(client, admin_headers, rows[0])
    set_builtin(client, admin_headers, rows[1])  # 没有数据包
    set_auto(client, admin_headers, contest, delay=0)
    # ab 下的不是源码、cd 没数据包、ef 是外部评测器
    submit(client, enrolled, {"ab/note.txt": b"x", "cd/cd.cpp": AB, "ef/ef.cpp": AB})
    assert progress(client, admin_headers, contest)["total"] == 0
    submit(client, enrolled, {"ab/ab.cpp": AB})
    assert progress(client, admin_headers, contest)["queued"] == 1


# --------------------------------------------------------------------------- #
# 复用、手动范围
# --------------------------------------------------------------------------- #


def test_相同数据包配置代码复用结果_rerun_真跑(
    client, admin_headers, contest, problem, enrolled, player, worker_token
) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    queue(client, admin_headers, contest)
    finish(client, worker_token, lease(client, worker_token), 100, "AC")

    queue(client, admin_headers, contest)
    assert lease(client, worker_token) is None, "复用结果，不占评测机"
    newest = tasks(client, admin_headers, contest)[0]
    assert newest["status"] == "done" and newest["reused_from"] and newest["score"] == 100

    queue(client, admin_headers, contest, rerun=True)
    assert lease(client, worker_token) is not None

    # 改了评测配置：指纹变了，不复用
    with client.app.state.ctx.db.session() as session:
        session.execute(text("DELETE FROM judge_task WHERE status = 'running'"))
    set_builtin(client, admin_headers, problem, time_limit_ms=2000)
    queue(client, admin_headers, contest)
    assert lease(client, worker_token) is not None


def test_系统错误的结果不复用(client, admin_headers, contest, problem, enrolled, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    queue(client, admin_headers, contest)
    finish(client, worker_token, lease(client, worker_token), 0, "SE")
    queue(client, admin_headers, contest)
    assert lease(client, worker_token) is not None


def test_手动排任务_全场_某人_某题_某人某题(client, admin_headers, contest, player) -> None:
    client.post(url(contest, "players"), json=[{"player_no": "S002"}], headers=admin_headers)
    created = client.post(url(contest, "problems"), json=[{"ident": "ab"}, {"ident": "cd"}],
                          headers=admin_headers).json()["problems"]
    for row in created:
        set_builtin(client, admin_headers, row)
        upload(client, admin_headers, row)
    assert queue(client, admin_headers, contest).json()["queued"] == 4
    assert queue(client, admin_headers, contest, player_id=player["id"]).json()["queued"] == 2
    assert queue(client, admin_headers, contest, problem="cd").json()["queued"] == 2
    assert queue(client, admin_headers, contest, player_id=player["id"], problem="cd").json()["queued"] == 1
    # 还在排队的同一对被替换，不重复
    assert progress(client, admin_headers, contest)["queued"] == 4
    assert {item["trigger"] for item in tasks(client, admin_headers, contest)} == {"manual"}
    assert tasks(client, admin_headers, contest)[0]["requested_by"]
    assert queue(client, admin_headers, contest, player_id=999999).status_code == 404


# --------------------------------------------------------------------------- #
# 历史版本重测、采用、乱序
# --------------------------------------------------------------------------- #


def test_重测历史版本不覆盖矩阵_采用后才换(
    client, admin_headers, contest, problem, enrolled, player, worker_token
) -> None:
    submit(client, enrolled, {"ab/ab.cpp": WA})
    queue(client, admin_headers, contest)
    finish(client, worker_token, lease(client, worker_token), 30)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    queue(client, admin_headers, contest)
    finish(client, worker_token, lease(client, worker_token), 100, "AC")
    assert scores(client, admin_headers, contest)[("S001", "ab")]["score"] == 100

    first, second = versions(client, admin_headers, contest, player["id"], "ab")
    assert (first["judge"]["score"], second["judge"]["score"]) == (30, 100)
    assert (first["in_matrix"], second["in_matrix"]) == (False, True)

    # 重测第一版：场次进行中也不拦（结果只进历史）
    response = queue(client, admin_headers, contest, version_id=first["id"], force=False, rerun=True)
    assert response.status_code == 200, response.text
    task = lease(client, worker_token)
    assert task["source"]["sha256"] == sha256_of(WA)
    finish(client, worker_token, task, 40)
    assert scores(client, admin_headers, contest)[("S001", "ab")]["score"] == 100

    first = versions(client, admin_headers, contest, player["id"], "ab")[0]
    assert first["judge"]["score"] == 40 and first["judge"]["trigger"] == "rejudge"
    assert first["tasks"] == 2 and not first["judge"]["applied"]
    detail = client.get(url(contest, "judge/tasks/%d" % first["judge"]["task_id"]), headers=admin_headers).json()
    assert detail["cases"][0]["score"] == 40 and detail["source_sha256"] == sha256_of(WA)

    adopted = client.post(url(contest, "judge/tasks/%d/adopt" % first["judge"]["task_id"]), headers=admin_headers)
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["in_matrix"] is True
    cell = scores(client, admin_headers, contest)[("S001", "ab")]
    assert cell["score"] == 40
    run = client.get(url(contest, "judge/runs/%d" % cell["run_id"]), headers=admin_headers).json()
    assert run["task_id"] == first["judge"]["task_id"]


def test_采用_没有结果或手工成绩时拒绝(client, admin_headers, contest, problem, enrolled, player, worker_token) -> None:
    submit(client, enrolled, {"ab/ab.cpp": AB})
    queue(client, admin_headers, contest)
    pending = tasks(client, admin_headers, contest)[0]
    response = client.post(url(contest, "judge/tasks/%d/adopt" % pending["id"]), headers=admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "judge_task_no_result"

    finish(client, worker_token, lease(client, worker_token), 100, "AC")
    client.put(url(contest, "judge/score"), json={"player_id": player["id"], "problem": "ab", "score": 7},
               headers=admin_headers)
    response = client.post(url(contest, "judge/tasks/%d/adopt" % pending["id"]), headers=admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "manual_score"


def test_旧版本晚交回不覆盖新版本的结果(
    client, admin_headers, contest, problem, enrolled, player, worker_token
) -> None:
    root = Path(client.app.state.ctx.settings.judge_result_root)
    submit(client, enrolled, {"ab/ab.cpp": WA})
    queue(client, admin_headers, contest)
    old = lease(client, worker_token)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    queue(client, admin_headers, contest)
    new = lease(client, worker_token)
    assert new["source"]["sha256"] == sha256_of(AB)
    finish(client, worker_token, new, 100, "AC")
    finish(client, worker_token, old, 0)

    written = json.loads((root / contest["slug"] / "S001" / "ab" / "result.json").read_text(encoding="utf-8"))
    assert written["score"] == 100 and written["task_id"] == new["id"]
    old_task = [t for t in tasks(client, admin_headers, contest) if t["id"] == old["id"]][0]
    assert old_task["status"] == "done" and old_task["applied"] is False


def test_历史版本必须属于本场次且归到自带评测题(client, admin_headers, contest, problem, enrolled, player) -> None:
    client.post(url(contest, "problems"), json=[{"ident": "zz", "file_patterns": ["ab/*.txt"]}],
                headers=admin_headers)
    submit(client, enrolled, {"ab/ab.cpp": AB, "ab/note.txt": b"x"})
    items = {item["rel_path"]: item for item in versions(client, admin_headers, contest, player["id"])}
    assert items["ab/note.txt"]["problem"] == "zz"
    bad = queue(client, admin_headers, contest, version_id=items["ab/note.txt"]["id"])
    assert bad.status_code == 400
    assert queue(client, admin_headers, contest, version_id=999999).status_code == 404
    assert queue(client, admin_headers, contest, version_id=items["ab/ab.cpp"]["id"]).json()["queued"] == 1


def test_timeline_按题目筛选_选手须在本场次(client, admin_headers, contest, problem, enrolled, player) -> None:
    client.post(url(contest, "problems"), json=[{"ident": "cd"}], headers=admin_headers)
    submit(client, enrolled, {"ab/ab.cpp": AB, "cd/cd.cpp": WA})
    assert [item["problem"] for item in versions(client, admin_headers, contest, player["id"], "cd")] == ["cd"]
    assert len(versions(client, admin_headers, contest, player["id"])) == 2
    response = client.get(url(contest, "judge/versions"), params={"player_id": 999999}, headers=admin_headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 端到端（真 g++）
# --------------------------------------------------------------------------- #


@needs_gcc
def test_端到端_自动评测_改回旧代码复用结果(
    client, admin_headers, contest, problem, enrolled, player, worker_token, tmp_path
) -> None:
    set_auto(client, admin_headers, contest, delay=0)
    judge = make_worker(client, worker_token, tmp_path)

    submit(client, enrolled, {"ab/ab.cpp": WA})
    assert judge.run_once() is True
    assert scores(client, admin_headers, contest)[("S001", "ab")]["status"] == "WA"

    submit(client, enrolled, {"ab/ab.cpp": AB})
    assert judge.run_once() is True
    assert scores(client, admin_headers, contest)[("S001", "ab")]["score"] == 100

    submit(client, enrolled, {"ab/ab.cpp": WA})
    assert judge.run_once() is False, "与第一版内容相同：直接复用，评测机没活"
    cell = scores(client, admin_headers, contest)[("S001", "ab")]
    assert cell["status"] == "WA"

    timeline = versions(client, admin_headers, contest, player["id"], "ab")
    assert [item["judge"]["outcome"] for item in timeline] == ["WA", "AC", "WA"]
    assert timeline[2]["judge"]["reused"] is True and timeline[2]["in_matrix"] is True
    assert [item["judge"]["trigger"] for item in timeline] == ["auto"] * 3


# --------------------------------------------------------------------------- #
# 迁移 020
# --------------------------------------------------------------------------- #


def test_迁移020_补初始版本_任务表去掉唯一约束(workdir: Path) -> None:
    engine = create_engine("sqlite:///%s" % (workdir / "v19.db"))
    migrations.migrate(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO contest (id, slug, name, status, created_at) VALUES (1, 'c', 'c', 'running', '2026-01-01')"
        ))
        conn.execute(text("UPDATE contest SET auto_judge = NULL, auto_judge_delay = NULL"))
        conn.execute(text(
            "INSERT INTO player (id, contest_id, player_no, created_at) VALUES (1, 1, 'S001', '2026-01-01')"
        ))
        for sid, path, stored in ((1, "ab/ab.cpp", 1), (2, "cd/cd.cpp", 0)):
            conn.execute(text(
                "INSERT INTO source_file (id, player_id, rel_path, sha256, size, mtime, revision, first_seen_at,"
                " last_seen_at, content_stored) VALUES (:id, 1, :path, :sha, 3, 0, 1, '2026-01-01',"
                " '2026-01-02 03:04:05', :stored)"
            ), {"id": sid, "path": path, "sha": str(sid) * 64, "stored": stored})
        # 退回迁移 020 之前的 judge_task：(场次, 选手, 题目) 唯一，没有版本列
        conn.execute(text("DROP TABLE judge_task"))
        conn.execute(text(
            "CREATE TABLE judge_task (id INTEGER PRIMARY KEY, contest_id INTEGER NOT NULL,"
            " player_id INTEGER NOT NULL, problem VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL,"
            " attempt INTEGER NOT NULL, worker_id INTEGER, lease_until DATETIME, source_path VARCHAR(1024),"
            " source_sha256 VARCHAR(64), outcome VARCHAR(16), error TEXT, created_at DATETIME NOT NULL,"
            " finished_at DATETIME, CONSTRAINT uq_judge_task UNIQUE (contest_id, player_id, problem))"
        ))
        conn.execute(text(
            "INSERT INTO judge_task (id, contest_id, player_id, problem, status, attempt, outcome, created_at)"
            " VALUES (1, 1, 1, 'ab', 'done', 1, 'AC', '2026-01-02')"
        ))
        conn.execute(text("PRAGMA user_version = 19"))

    applied = migrations.migrate(engine)
    assert any(step.startswith("020 ") for step in applied)
    with engine.begin() as conn:
        found = conn.execute(text("SELECT player_id, rel_path, sha256, received_at, problem FROM source_version"))
        assert [tuple(row) for row in found] == [(1, "ab/ab.cpp", "1" * 64, "2026-01-02 03:04:05", None)]
        assert conn.execute(text("SELECT auto_judge, auto_judge_delay FROM contest")).one() == ("off", 60)
        assert conn.execute(text("SELECT trigger, outcome FROM judge_task")).one() == (None, "AC")
        conn.execute(text(
            "INSERT INTO judge_task (contest_id, player_id, problem, status, attempt, created_at)"
            " VALUES (1, 1, 'ab', 'queued', 0, '2026-01-03')"
        ))
    assert "source_version_id" in {c["name"] for c in inspect(engine).get_columns("judge_task")}

    # 重跑不重复补
    with engine.begin() as conn:
        conn.execute(text("PRAGMA user_version = 19"))
    migrations.migrate(engine)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM source_version")).scalar() == 1
    engine.dispose()
