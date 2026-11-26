"""自动评测的智能模式（§12.12）：判定纯函数的正反例、真 app 上排出的任务与领任务顺序、迁移 025。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from conftest import sha256_of
from test_builtin_judge import AB, set_builtin, submit, upload

from syncoj_server import migrations
from syncoj_server.models import Contest, ContestStatus, JudgeTask, JudgeWorker, utcnow
from syncoj_server.services import builtin_judge, smart_judge
from syncoj_server.services.smart_judge import (
    CONTEST_END,
    LONG_STABLE,
    MOVED_ON,
    NEAR_END,
    OFFLINE,
    ContestClock,
    PairState,
    Thresholds,
    decide,
)

T0 = datetime(2026, 11, 26, 8, 0, 0)
TH = Thresholds()  # 180 / 600 / 900 / 300 / 20
WA = AB.replace(b"a+b", b"a-b")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def pair(player=1, problem="a", version=1, received=0, **extra) -> PairState:
    return PairState(player_id=player, problem=problem, version_id=version, received_at=at(received), **extra)


def run(pairs, now, *, activity=None, offline=(), clock=None, thresholds=TH, queued=0, capacity=1):
    if activity is None:
        activity = {}
        for item in pairs:
            current = activity.setdefault(item.player_id, {}).get(item.problem)
            if current is None or item.received_at > current:
                activity[item.player_id][item.problem] = item.received_at
    return decide(
        now=at(now), pairs=list(pairs), activity=activity, offline=set(offline),
        clock=clock or ContestClock(), thresholds=thresholds, queued=queued, capacity=capacity,
    )


def reasons(picks):
    return [(p.player_id, p.problem, p.reason) for p in picks]


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_换题_稳定够久且之后改了别的题():
    pairs = [pair(problem="a", received=0), pair(problem="b", version=2, received=100)]
    assert reasons(run(pairs, now=180)) == [(1, "a", MOVED_ON)]
    # 稳定不够久
    assert run(pairs, now=179) == []
    # 别的题的改动在这道题之前：不算换题
    assert reasons(run([pair(problem="a", received=100), pair(problem="b", version=2, received=0)], now=200)) \
        == [(1, "b", MOVED_ON)]


def test_换题看的是全部题目的活动_含不评测的题():
    pairs = [pair(problem="a", received=0)]
    assert reasons(run(pairs, now=200, activity={1: {"a": at(0), "notes": at(50)}})) == [(1, "a", MOVED_ON)]
    # 别的选手的活动不算
    assert run(pairs, now=200, activity={1: {"a": at(0)}, 2: {"b": at(50)}}) == []


def test_空闲_稳定超过空闲阈值():
    assert run([pair(received=0)], now=599) == []
    assert reasons(run([pair(received=0)], now=600)) == [(1, "a", LONG_STABLE)]


def test_已评或已排的最新版本不再排():
    assert run([pair(received=0, covered=True)], now=10000) == []


def test_冷却_换题与空闲受限():
    pairs = [pair(received=0, last_smart_at=at(400))]
    assert run(pairs, now=699) == []
    assert reasons(run(pairs, now=700)) == [(1, "a", LONG_STABLE)]
    tight = Thresholds(cooldown=0)
    assert reasons(run(pairs, now=600, thresholds=tight)) == [(1, "a", LONG_STABLE)]


def test_将结束_每对各一次_之后按冷却():
    clock = ContestClock(ends_at=at(1000))
    # 距结束 900 秒以内：刚改完也排
    assert reasons(run([pair(received=95)], now=100, clock=clock)) == [(1, "a", NEAR_END)]
    # 距结束 901 秒：还不算将结束
    assert run([pair(received=50)], now=99, clock=clock) == []
    # 排过一次后又改：冷却没过不排，过了再排
    again = [pair(version=2, received=200, near_end_done=True, last_smart_at=at(100))]
    assert run(again, now=399, clock=clock) == []
    assert reasons(run(again, now=400, clock=clock)) == [(1, "a", NEAR_END)]
    # 0 = 不用这个信号；结束时间已过不算将结束
    assert run([pair(received=95)], now=100, clock=clock, thresholds=Thresholds(near_end=0)) == []
    assert run([pair(received=95)], now=100, clock=ContestClock(ends_at=at(100))) == []


def test_掉线_不受冷却():
    pairs = [pair(received=0, last_smart_at=at(5))]
    assert reasons(run(pairs, now=10, offline={1})) == [(1, "a", OFFLINE)]
    assert run(pairs, now=10, offline={2}) == []
    assert run([pair(received=0, covered=True)], now=10, offline={1}) == [], "同一版本不重复排"


def test_队列过载只保留三类():
    pairs = [
        pair(player=1, received=0),                        # 空闲
        pair(player=2, received=0),                        # 掉线
        pair(player=3, problem="a", received=0),           # 换题
        pair(player=3, problem="b", version=4, received=10),
    ]
    normal = reasons(run(pairs, now=700, offline={2}, queued=20, capacity=1))
    assert (1, "a", LONG_STABLE) in normal and (3, "a", MOVED_ON) in normal
    loaded = reasons(run(pairs, now=700, offline={2}, queued=21, capacity=1))
    assert loaded == [(2, "a", OFFLINE)]
    # 并发总数按至少 1 算；并发越多容忍越多
    assert reasons(run(pairs, now=700, offline={2}, queued=21, capacity=0)) == [(2, "a", OFFLINE)]
    assert len(run(pairs, now=700, offline={2}, queued=21, capacity=2)) == 4
    near = ContestClock(ends_at=at(800))
    assert {r for _, _, r in reasons(run(pairs, now=700, offline={2}, clock=near, queued=99))} == {OFFLINE, NEAR_END}


def test_优先级_每对取最高的信号_结果按优先级排():
    clock = ContestClock(ends_at=at(1000))
    pairs = [
        pair(player=1, received=0),                 # 将结束（也满足空闲）
        pair(player=2, received=0),                 # 掉线 > 将结束
    ]
    picks = run(pairs, now=700, offline={2}, clock=clock)
    assert reasons(picks) == [(2, "a", OFFLINE), (1, "a", NEAR_END)]
    assert [p.priority for p in picks] == [50, 40]
    assert smart_judge.PRIORITY[CONTEST_END] > smart_judge.PRIORITY[OFFLINE] > smart_judge.PRIORITY[NEAR_END] \
        > smart_judge.PRIORITY[MOVED_ON] > smart_judge.PRIORITY[LONG_STABLE] > builtin_judge.PRIORITY_AUTO
    assert builtin_judge.PRIORITY_MANUAL > smart_judge.PRIORITY[CONTEST_END]


def test_考试结束_全员未评的各一次_做过就不再做():
    pairs = [pair(player=1, received=0), pair(player=2, received=0, covered=True), pair(player=3, received=5)]
    ended = ContestClock(ended=True)
    picks = run(pairs, now=10, clock=ended, queued=999)
    assert reasons(picks) == [(1, "a", CONTEST_END), (3, "a", CONTEST_END)]
    assert run(pairs, now=10, clock=ContestClock(ended=True, end_done=True)) == []


def test_归档不评():
    assert run([pair(received=0)], now=10000, offline={1}, clock=ContestClock(archived=True)) == []
    assert run([pair(received=0)], now=10, clock=ContestClock(ended=True, archived=True)) == []


# --------------------------------------------------------------------------- #
# 真 app
# --------------------------------------------------------------------------- #


def url(contest, tail: str) -> str:
    return "/api/v1/admin/contests/%d/%s" % (contest["id"], tail)


@pytest.fixture()
def problems(client, admin_headers, contest) -> list:
    response = client.post(url(contest, "problems"), json=[{"ident": "ab"}, {"ident": "cd"}], headers=admin_headers)
    assert response.status_code == 200, response.text
    rows = response.json()["problems"]
    for row in rows:
        assert set_builtin(client, admin_headers, row).status_code == 200
        assert upload(client, admin_headers, row).status_code == 200
    return rows


def set_smart(client, admin_headers, contest, **extra) -> dict:
    body = {"auto_judge": "smart", "auto_judge_delay": 60}
    body.update(extra)
    response = client.put(url(contest, "judge/auto"), json=body, headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()


def smart_tasks(ctx, contest) -> list:
    with ctx.db.session() as session:
        return [
            (row.problem, row.reason, row.priority, row.status)
            for row in session.query(JudgeTask).filter(JudgeTask.contest_id == contest["id"]).order_by(JudgeTask.id)
        ]


def edit_contest(ctx, contest, **fields) -> None:
    with ctx.db.session() as session:
        row = session.get(Contest, contest["id"])
        for key, value in fields.items():
            setattr(row, key, value)


def test_接口_三态与阈值_回显与校验(client, admin_headers, contest) -> None:
    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()["items"][0]
    assert (listed["smart_stable_seconds"], listed["smart_idle_seconds"], listed["smart_near_end_seconds"],
            listed["smart_cooldown_seconds"], listed["smart_queue_factor"]) == (180, 600, 900, 300, 20)
    saved = set_smart(client, admin_headers, contest, smart_stable_seconds=120, smart_queue_factor=5)
    assert saved["auto_judge"] == "smart" and saved["smart_stable_seconds"] == 120
    assert saved["smart_queue_factor"] == 5 and saved["smart_idle_seconds"] == 600
    # 不传阈值 = 不改
    again = set_smart(client, admin_headers, contest)
    assert again["smart_stable_seconds"] == 120
    listed = client.get("/api/v1/admin/contests", headers=admin_headers).json()["items"][0]
    assert listed["auto_judge"] == "smart" and listed["smart_stable_seconds"] == 120
    for bad in ({"smart_stable_seconds": 10}, {"smart_queue_factor": 0}, {"smart_near_end_seconds": -1},
                {"auto_judge": "sometimes"}):
        body = {"auto_judge": "smart"}
        body.update(bad)
        assert client.put(url(contest, "judge/auto"), json=body, headers=admin_headers).status_code == 422


def test_换题与空闲_排出的任务_同一版本不重复(client, admin_headers, contest, problems, enrolled) -> None:
    ctx = client.app.state.ctx
    set_smart(client, admin_headers, contest)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    base = utcnow()
    assert smart_judge.evaluate_contest(ctx, contest["id"], base + timedelta(seconds=60)) == []
    submit(client, enrolled, {"cd/cd.cpp": AB})

    picks = smart_judge.evaluate_contest(ctx, contest["id"], base + timedelta(seconds=200))
    assert [(p.problem, p.reason, p.priority) for p in picks] == [("ab", MOVED_ON, 30)]
    assert smart_judge.evaluate_contest(ctx, contest["id"], base + timedelta(seconds=300)) == []

    picks = smart_judge.evaluate_contest(ctx, contest["id"], base + timedelta(seconds=700))
    assert [(p.problem, p.reason) for p in picks] == [("cd", LONG_STABLE)]
    assert smart_tasks(ctx, contest) == [("ab", MOVED_ON, 30, "queued"), ("cd", LONG_STABLE, 20, "queued")]

    listed = client.get(url(contest, "judge/tasks"), params={"trigger": "smart", "reason": "moved_on"},
                        headers=admin_headers).json()["items"]
    assert [(t["problem"], t["trigger"], t["reason"], t["priority"]) for t in listed] == [("ab", "smart", "moved_on", 30)]


def test_不是智能模式或已归档的场次不排(client, admin_headers, contest, problems, enrolled) -> None:
    ctx = client.app.state.ctx
    submit(client, enrolled, {"ab/ab.cpp": AB})
    later = utcnow() + timedelta(hours=2)
    assert smart_judge.run_round(ctx, later) == 0
    set_smart(client, admin_headers, contest)
    edit_contest(ctx, contest, status=ContestStatus.ARCHIVED)
    assert smart_judge.run_round(ctx, later) == 0
    edit_contest(ctx, contest, status=ContestStatus.RUNNING)
    assert smart_judge.run_round(ctx, later) == 1


def test_将结束_掉线(client, admin_headers, contest, problems, enrolled) -> None:
    ctx = client.app.state.ctx
    set_smart(client, admin_headers, contest)
    now = utcnow()
    edit_contest(ctx, contest, ends_at=now + timedelta(seconds=600))
    submit(client, enrolled, {"ab/ab.cpp": WA})
    picks = smart_judge.evaluate_contest(ctx, contest["id"], now + timedelta(seconds=5))
    assert [(p.problem, p.reason, p.priority) for p in picks] == [("ab", NEAR_END, 40)]

    # 掉线：排队中的任务已覆盖最新版本时不重复；把它领走评完后改一版再掉线
    with ctx.db.session() as session:
        session.query(JudgeTask).update({"status": builtin_judge.DONE})
    submit(client, enrolled, {"ab/ab.cpp": AB})
    for runtime in ctx.registry.all(contest["id"]):
        ctx.registry.mark_offline(runtime.agent_id)
    picks = smart_judge.evaluate_contest(ctx, contest["id"], now + timedelta(seconds=10))
    assert [(p.problem, p.reason, p.priority) for p in picks] == [("ab", OFFLINE, 50)]
    assert smart_judge.evaluate_contest(ctx, contest["id"], now + timedelta(seconds=20)) == []


def test_过载时跳过换题与空闲(client, admin_headers, contest, problems, enrolled) -> None:
    ctx = client.app.state.ctx
    set_smart(client, admin_headers, contest, smart_queue_factor=1)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    builtin_judge_enqueue_other(ctx, contest, count=2)
    later = utcnow() + timedelta(hours=1)
    assert smart_judge.evaluate_contest(ctx, contest["id"], later) == []
    with ctx.db.session() as session:
        session.query(JudgeTask).delete()
    assert [p.reason for p in smart_judge.evaluate_contest(ctx, contest["id"], later)] == [LONG_STABLE]


def builtin_judge_enqueue_other(ctx, contest, count: int) -> None:
    """往别的场次塞几个排队任务（过载按全局排队数算）。"""
    with ctx.db.session() as session:
        other = Contest(slug="other", name="other", status=ContestStatus.RUNNING)
        session.add(other)
        session.flush()
        from syncoj_server.models import Player

        player = Player(contest_id=other.id, player_no="X1")
        session.add(player)
        session.flush()
        for index in range(count):
            session.add(JudgeTask(contest_id=other.id, player_id=player.id, problem="p%d" % index,
                                  status=builtin_judge.QUEUED, trigger="manual", priority=100))


def test_考试结束_全员评一遍只一次_相同代码复用结果(client, admin_headers, contest, problems, enrolled) -> None:
    ctx = client.app.state.ctx
    response = client.post("/api/v1/admin/judge-workers", json={"name": "评测机1"}, headers=admin_headers)
    token = response.json()["token"]
    set_smart(client, admin_headers, contest)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    # 手动评一次 AB 并交回结果
    response = client.post(url(contest, "judge/builtin"), json={"force": True, "problem": "ab"}, headers=admin_headers)
    assert response.status_code == 200, response.text
    leased = client.post("/api/v1/judge/lease", headers={"Authorization": "Bearer " + token}).json()
    client.post("/api/v1/judge/tasks/%d/result" % leased["id"], headers={"Authorization": "Bearer " + token},
                json={"score": 100, "max_score": 100, "status": "AC",
                      "detail": {"judge": "syncoj-builtin", "cases": [{"name": "1", "status": "AC", "score": 100}]}})
    # 改成 WA 再改回 AB：最新版本是新的一条版本记录，但代码与评过的相同
    submit(client, enrolled, {"ab/ab.cpp": WA})
    submit(client, enrolled, {"ab/ab.cpp": AB, "cd/cd.cpp": WA})
    now = utcnow()
    edit_contest(ctx, contest, status=ContestStatus.CLOSED)
    picks = smart_judge.evaluate_contest(ctx, contest["id"], now)
    assert sorted((p.problem, p.reason, p.priority) for p in picks) == [("ab", CONTEST_END, 60), ("cd", CONTEST_END, 60)]
    assert smart_judge.evaluate_contest(ctx, contest["id"], now + timedelta(minutes=5)) == []
    with ctx.db.session() as session:
        assert session.get(Contest, contest["id"]).smart_end_done_at is not None

    # 领任务：ab 与评过的代码相同，就地复用；领到的是 cd
    with ctx.db.session() as session:
        worker_id = session.query(JudgeWorker).one().id
    leased = builtin_judge.lease_next(ctx, worker_id)
    assert leased is not None and leased.task.problem == "cd"
    with ctx.db.session() as session:
        reused = session.query(JudgeTask).filter(JudgeTask.reason == CONTEST_END, JudgeTask.problem == "ab").one()
        assert reused.status == builtin_judge.DONE and reused.reused_from is not None
        assert reused.source_sha256 == sha256_of(AB)

    # 重新开放（结束时间改到以后）：标记清空，下次结束再评一遍
    edit_contest(ctx, contest, status=ContestStatus.RUNNING, ends_at=now + timedelta(hours=3))
    smart_judge.evaluate_contest(ctx, contest["id"], now + timedelta(minutes=6))
    with ctx.db.session() as session:
        assert session.get(Contest, contest["id"]).smart_end_done_at is None


def test_领任务按优先级_同级按先后(client, admin_headers, contest, problems, enrolled) -> None:
    ctx = client.app.state.ctx
    submit(client, enrolled, {"ab/ab.cpp": AB, "cd/cd.cpp": WA})
    with ctx.db.session() as session:
        player_id = session.get(Contest, contest["id"]).players[0].id
        for problem, trigger, reason, priority in (
            ("ab", "auto", None, 10),
            ("cd", "smart", LONG_STABLE, 20),
            ("ab", "smart", CONTEST_END, 60),
            ("cd", "manual", None, 100),
            ("ab", "smart", MOVED_ON, 30),
            ("cd", "smart", MOVED_ON, 30),
        ):
            session.add(JudgeTask(contest_id=contest["id"], player_id=player_id, problem=problem,
                                  status=builtin_judge.QUEUED, trigger=trigger, reason=reason, priority=priority))
    response = client.post("/api/v1/admin/judge-workers", json={"name": "评测机1"}, headers=admin_headers)
    assert response.status_code == 200
    with ctx.db.session() as session:
        worker_id = session.query(JudgeWorker).one().id
    order = []
    while True:
        leased = builtin_judge.lease_next(ctx, worker_id)
        if leased is None:
            break
        order.append((leased.task.trigger, leased.task.reason, leased.task.priority))
    assert order == [
        ("manual", None, 100), ("smart", CONTEST_END, 60), ("smart", MOVED_ON, 30), ("smart", MOVED_ON, 30),
        ("smart", LONG_STABLE, 20), ("auto", None, 10),
    ]


def test_手动与自动排的任务带优先级(client, admin_headers, contest, problems, enrolled) -> None:
    client.put(url(contest, "judge/auto"), json={"auto_judge": "on_change", "auto_judge_delay": 0},
               headers=admin_headers)
    submit(client, enrolled, {"ab/ab.cpp": AB})
    client.post(url(contest, "judge/builtin"), json={"force": True, "problem": "cd"}, headers=admin_headers)
    items = client.get(url(contest, "judge/tasks"), headers=admin_headers).json()["items"]
    assert sorted((t["problem"], t["trigger"], t["priority"], t["reason"]) for t in items) == [
        ("ab", "auto", 10, None), ("cd", "manual", 100, None),
    ]


# --------------------------------------------------------------------------- #
# 迁移 025
# --------------------------------------------------------------------------- #


def test_迁移025_加列_按触发方式回填优先级(workdir: Path) -> None:
    engine = create_engine("sqlite:///%s" % (workdir / "v24.db"))
    with engine.begin() as conn:
        # 024 的两张表（只留本迁移碰到的列）：没有阈值、优先级与信号
        conn.execute(text(
            "CREATE TABLE contest (id INTEGER PRIMARY KEY, slug VARCHAR(64), name VARCHAR(200),"
            " status VARCHAR(16), auto_judge VARCHAR(16), created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO contest (id, slug, name, status, auto_judge, created_at)"
            " VALUES (1, 'c', 'c', 'running', 'off', '2026-01-01')"
        ))
        conn.execute(text(
            "CREATE TABLE judge_task (id INTEGER PRIMARY KEY, contest_id INTEGER, player_id INTEGER,"
            " problem VARCHAR(64), status VARCHAR(16), attempt INTEGER, created_at DATETIME, trigger VARCHAR(16))"
        ))
        for task_id, trigger in ((1, "auto"), (2, "manual"), (3, "rejudge"), (4, None)):
            conn.execute(text(
                "INSERT INTO judge_task (id, contest_id, player_id, problem, status, attempt, created_at, trigger)"
                " VALUES (:id, 1, 1, 'ab', 'queued', 0, '2026-01-02', :trigger)"
            ), {"id": task_id, "trigger": trigger})
        conn.execute(text("PRAGMA user_version = 24"))

    applied = migrations.migrate(engine)
    assert any(step.startswith("025 ") for step in applied)
    with engine.begin() as conn:
        found = conn.execute(text("SELECT id, priority, reason, target_version_id FROM judge_task ORDER BY id")).all()
        assert [tuple(row) for row in found] == [(1, 10, None, None), (2, 100, None, None), (3, 100, None, None),
                                                (4, 100, None, None)]
        assert conn.execute(text("SELECT auto_judge, smart_stable_seconds, smart_end_done_at FROM contest")).one() \
            == ("off", None, None)
        # 重跑不改已有值
        conn.execute(text("UPDATE judge_task SET priority = 7 WHERE id = 1"))
        conn.execute(text("PRAGMA user_version = 24"))
    migrations.migrate(engine)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT priority FROM judge_task WHERE id = 1")).scalar() == 7
    engine.dispose()
