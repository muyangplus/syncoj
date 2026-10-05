"""成绩解析与回写测试。

成绩算错是教师最不能容忍的故障（比功能缺失严重得多，因为它会直接改变排名）。
所以这里的重点不是"能解析多少格式"，而是**看不懂时必须如实报告**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import ADMIN_PASSWORD, ADMIN_USER
from syncoj_server.models import Contest, JudgeRun, Player
from syncoj_server.services.judge import (
    ParsedScore,
    parse_json_text,
    parse_keyvalue_text,
    parse_result_file,
    parse_xml_text,
    scan_contest_results,
)


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"score": 100}', 100),
        ('{"Score": 100}', 100),
        ('{"SCORE": 100}', 100),
        ('{"total_score": 88}', 88),
        ('{"totalScore": 88}', 88),
        ('{"总分": 77}', 77),
        ('{"得分": 66}', 66),
        ('{"score": "100"}', 100),
        ('{"score": "100/100"}', 100),
        ('{"score": "100分"}', 100),
        ('{"points": 30, "max_score": 50}', 30),
    ],
)
def test_json_direct_score(text: str, expected: int) -> None:
    parsed = parse_json_text(text)
    assert parsed is not None
    assert parsed.score == expected


def test_json_array_of_cases_is_summed() -> None:
    text = json.dumps([{"score": 10}, {"score": 20}, {"score": 30}])
    parsed = parse_json_text(text)
    assert parsed is not None
    assert parsed.score == 60
    assert "3 项" in parsed.detail


def test_json_nested_cases_container_is_summed() -> None:
    text = json.dumps({"cases": [{"score": 10, "max_score": 10}, {"score": 5, "max_score": 10}]})
    parsed = parse_json_text(text)
    assert parsed is not None
    assert parsed.score == 15
    assert parsed.max_score == 20


def test_json_partial_cases_are_refused() -> None:
    """三个测试点里有两个解析不出分数 —— 不能把剩下那个当总分。

    这是最危险的一类错误：静默给出一个偏低的分数，教师会以为是选手挂了测试点。
    """
    text = json.dumps([{"score": 10}, {"verdict": "WA"}, {"score": 30}])
    assert parse_json_text(text) is None


def test_json_without_score_returns_none() -> None:
    for text in ('{"time_limit": 1000}', '{"memory_limit": 256}', "[]", "{}", "42", '"text"'):
        assert parse_json_text(text) is None


def test_json_bool_is_not_a_score() -> None:
    """Python 里 True 是 int 的子类，不显式排除的话会被当成 1 分。"""
    assert parse_json_text('{"score": true}') is None


def test_json_float_non_integer_is_refused() -> None:
    assert parse_json_text('{"score": 88.5}') is None
    assert parse_json_text('{"score": 88.0}').score == 88


def test_json_does_not_blindly_search_nested_dicts() -> None:
    """盲目全树搜索会把评测配置里的数字当成分数。"""
    text = json.dumps({"meta": {"time_limit": 1000}, "config": {"score": 999}})
    # config.score 在已知容器键之外，不该被采纳
    assert parse_json_text(text) is None


def test_worst_status_wins() -> None:
    """["AC","AC","WA"] 的整题状态必须是 WA，否则一个错点被后面的正确掩盖。"""
    text = json.dumps([{"score": 10, "status": "AC"}, {"score": 0, "status": "WA"}])
    parsed = parse_json_text(text)
    assert parsed is not None
    assert parsed.status == "WA"


def test_unknown_status_ranks_as_most_severe() -> None:
    text = json.dumps([{"score": 10, "status": "AC"}, {"score": 0, "status": "奇妙状态"}])
    parsed = parse_json_text(text)
    assert parsed.status == "奇妙状态"


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #


def test_xml_single_result() -> None:
    parsed = parse_xml_text('<result score="100" max_score="100" status="AC"/>')
    assert parsed is not None
    assert parsed.score == 100
    assert parsed.max_score == 100
    assert parsed.status == "AC"


def test_xml_multiple_cases_are_summed() -> None:
    parsed = parse_xml_text(
        '<tests><test score="10" status="AC"/><test score="0" status="WA"/></tests>'
    )
    assert parsed is not None
    assert parsed.score == 10
    assert parsed.status == "WA"


def test_xml_without_score_returns_none() -> None:
    assert parse_xml_text("<config><time_limit>1000</time_limit></config>") is None


def test_xml_malformed_returns_none() -> None:
    assert parse_xml_text("<result score='100'") is None


# --------------------------------------------------------------------------- #
# key=value 文本
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("score = 100\n", 100),
        ("score: 100\n", 100),
        ("Score: 100\nMax Score: 100\n", 100),
        ("总分 = 77\n", 77),
        ("得分: 66\n满分: 100\n", 66),
    ],
)
def test_keyvalue_text(text: str, expected: int) -> None:
    parsed = parse_keyvalue_text(text)
    assert parsed is not None
    assert parsed.score == expected


def test_keyvalue_without_score_returns_none() -> None:
    assert parse_keyvalue_text("time_limit = 1000\nmemory = 256\n") is None
    assert parse_keyvalue_text("这不是键值对格式的文本\n") is None


# --------------------------------------------------------------------------- #
# 文件级解析
# --------------------------------------------------------------------------- #


def test_parse_file_prefers_json(workdir: Path) -> None:
    target = workdir / "result.json"
    target.write_text('{"score": 42}', encoding="utf-8")
    parsed, parser_name, _reason = parse_result_file(target)
    assert parsed is not None and parsed.score == 42
    assert parser_name == "json"


def test_parse_file_strips_bom(workdir: Path) -> None:
    """某些 Windows 工具导出的文件带 BOM，会让 json.loads 直接失败。"""
    target = workdir / "result.json"
    target.write_bytes("\ufeff".encode("utf-8") + b'{"score": 42}')
    parsed, _name, _reason = parse_result_file(target)
    assert parsed is not None and parsed.score == 42


def test_parse_file_handles_gb18030(workdir: Path) -> None:
    target = workdir / "result.txt"
    target.write_bytes("得分: 88\n".encode("gb18030"))
    parsed, _name, _reason = parse_result_file(target)
    assert parsed is not None and parsed.score == 88


def test_parse_file_rejects_binary(workdir: Path) -> None:
    target = workdir / "a.out"
    target.write_bytes(b"\x7fELF\x02\x01\x00\x00\x00")
    parsed, name, reason = parse_result_file(target)
    assert parsed is None
    assert "二进制" in reason


def test_parse_file_rejects_empty(workdir: Path) -> None:
    target = workdir / "empty.txt"
    target.write_bytes(b"")
    parsed, _name, reason = parse_result_file(target)
    assert parsed is None
    assert "空" in reason


def test_parse_file_reports_unrecognised_format(workdir: Path) -> None:
    """看不懂必须说"看不懂"，而不是返回一个 None 让人以为文件不存在。"""
    target = workdir / "weird.dat"
    target.write_text("这是评测器自定义的二进制表格\n没有键值也没有 JSON\n", encoding="utf-8")
    parsed, _name, reason = parse_result_file(target)
    assert parsed is None
    assert "没有解析器认得" in reason


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #


@pytest.fixture()
def judge_setup(app, client: TestClient, admin_headers: dict, contest: dict, player: dict):
    """准备一个带结果目录的场次。"""
    ctx = app.state.ctx
    root = ctx.settings.judge_result_root / contest["slug"]

    with ctx.db.session() as session:
        db_contest = session.get(Contest, contest["id"])
        db_player = session.get(Player, player["id"])

    return {
        "ctx": ctx,
        "root": root,
        "contest_id": contest["id"],
        "player_no": db_player.player_no,
        "player_id": db_player.id,
        "slug": contest["slug"],
    }


def write_result(setup, player_no: str, problem: str, filename: str, content: str) -> Path:
    target = setup["root"] / player_no / problem / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def do_scan(setup, force: bool = True):
    with setup["ctx"].db.session() as session:
        contest = session.get(Contest, setup["contest_id"])
        return scan_contest_results(session, setup["ctx"].settings, contest, force=force)


def test_scan_creates_judge_run(judge_setup) -> None:
    write_result(judge_setup, judge_setup["player_no"], "p1", "result.json", '{"score": 100, "max_score": 100, "status": "AC"}')
    report = do_scan(judge_setup)

    assert report.parsed == 1
    with judge_setup["ctx"].db.session() as session:
        runs = list(session.execute(select(JudgeRun)).scalars())
    assert len(runs) == 1
    assert runs[0].score == 100
    assert runs[0].status == "AC"
    assert runs[0].parse_status == "ok"
    assert runs[0].problem == "p1"


def test_scan_skips_unchanged_files(judge_setup) -> None:
    write_result(judge_setup, judge_setup["player_no"], "p1", "result.json", '{"score": 100}')
    do_scan(judge_setup)
    report = do_scan(judge_setup, force=False)

    assert report.unchanged == 1
    assert report.parsed == 0


def test_force_scan_reparses(judge_setup) -> None:
    write_result(judge_setup, judge_setup["player_no"], "p1", "result.json", '{"score": 100}')
    do_scan(judge_setup, force=False)
    report = do_scan(judge_setup, force=True)
    assert report.parsed == 1


def test_scan_picks_up_changed_score(judge_setup) -> None:
    path = write_result(judge_setup, judge_setup["player_no"], "p1", "result.json", '{"score": 40}')
    do_scan(judge_setup)
    path.write_text('{"score": 100}', encoding="utf-8")
    do_scan(judge_setup)

    with judge_setup["ctx"].db.session() as session:
        run = session.execute(select(JudgeRun)).scalar_one()
    assert run.score == 100


def test_unparsed_file_preserves_previous_score(judge_setup) -> None:
    """评测器重新导出一个看不懂的文件，不该把已有成绩抹掉。"""
    path = write_result(judge_setup, judge_setup["player_no"], "p1", "result.json", '{"score": 100}')
    do_scan(judge_setup)

    path.write_text("这是损坏的内容 {", encoding="utf-8")
    report = do_scan(judge_setup)

    assert report.unparsed == 1
    with judge_setup["ctx"].db.session() as session:
        run = session.execute(select(JudgeRun)).scalar_one()
    assert run.score == 100, "解析失败时不该清空已有成绩"
    assert run.parse_status == "unparsed", "但必须标记出来让教师看到"


def test_scan_reports_unknown_player_directory(judge_setup) -> None:
    write_result(judge_setup, "查无此人", "p1", "result.json", '{"score": 100}')
    report = do_scan(judge_setup)

    assert report.skipped == 1
    assert any("查无此人" in e for e in report.errors), report.errors


def test_scan_ignores_hidden_directories(judge_setup) -> None:
    write_result(judge_setup, ".cache", "p1", "result.json", '{"score": 100}')
    report = do_scan(judge_setup)
    assert report.parsed == 0


def test_scan_handles_missing_root(judge_setup) -> None:
    report = do_scan(judge_setup)
    assert report.parsed == 0
    assert report.errors == []


def test_scan_prefers_json_over_plain_text(judge_setup) -> None:
    """题目目录里常有多种文件，要挑最像结果的那个。"""
    problem_dir = judge_setup["root"] / judge_setup["player_no"] / "p1"
    problem_dir.mkdir(parents=True, exist_ok=True)
    (problem_dir / "notes.txt").write_text("随便写的笔记", encoding="utf-8")
    (problem_dir / "result.json").write_text('{"score": 77}', encoding="utf-8")

    do_scan(judge_setup)
    with judge_setup["ctx"].db.session() as session:
        run = session.execute(select(JudgeRun)).scalar_one()
    assert run.score == 77


def test_multiple_problems_are_tracked_separately(judge_setup) -> None:
    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 100}')
    write_result(judge_setup, judge_setup["player_no"], "p2", "r.json", '{"score": 50}')
    do_scan(judge_setup)

    with judge_setup["ctx"].db.session() as session:
        runs = {r.problem: r.score for r in session.execute(select(JudgeRun)).scalars()}
    assert runs == {"p1": 100, "p2": 50}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def test_score_matrix_endpoint(judge_setup, client: TestClient, admin_headers: dict) -> None:
    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 100, "max_score": 100}')
    do_scan(judge_setup)

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % judge_setup["contest_id"], headers=admin_headers
    ).json()

    assert matrix["problems"] == ["p1"]
    assert len(matrix["rows"]) == 1
    assert matrix["rows"][0]["total"] == 100
    assert matrix["rows"][0]["cells"][0]["parse_status"] == "ok"
    assert matrix["complete"] is True


def test_matrix_distinguishes_missing_from_zero(judge_setup, client: TestClient,
                                                admin_headers: dict) -> None:
    """0 分和"没有成绩"必须能区分 —— 都显示成 0 会让教师以为选手考砸了。"""
    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 0}')
    do_scan(judge_setup)

    matrix = client.get(
        "/api/v1/admin/contests/%d/scores" % judge_setup["contest_id"], headers=admin_headers
    ).json()
    cell = matrix["rows"][0]["cells"][0]
    assert cell["score"] == 0
    assert cell["parse_status"] == "ok"


def test_manual_score_entry(judge_setup, client: TestClient, admin_headers: dict) -> None:
    response = client.put(
        "/api/v1/admin/contests/%d/judge/score" % judge_setup["contest_id"],
        json={
            "player_id": judge_setup["player_id"],
            "problem": "p1",
            "score": 60,
            "max_score": 100,
            "status": "手工",
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parse_status"] == "manual"
    assert body["score"] == 60


def test_manual_score_is_not_overwritten_by_scan(judge_setup, client: TestClient,
                                                admin_headers: dict) -> None:
    """教师既然亲手改了，就该由教师负责 —— 自动扫描不能在下一轮把它覆盖掉。"""
    client.put(
        "/api/v1/admin/contests/%d/judge/score" % judge_setup["contest_id"],
        json={"player_id": judge_setup["player_id"], "problem": "p1", "score": 60},
        headers=admin_headers,
    )
    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 100}')
    report = do_scan(judge_setup)

    assert report.manual == 1
    with judge_setup["ctx"].db.session() as session:
        run = session.execute(select(JudgeRun)).scalar_one()
    assert run.score == 60, "手工录入的成绩不该被自动扫描覆盖"
    assert run.parse_status == "manual"


def test_manual_score_survives_forced_rescan(judge_setup, client: TestClient,
                                             admin_headers: dict) -> None:
    """点一下"重新扫描"在教师看来只是刷新，不该有破坏性 —— force 也不能覆盖手工值。"""
    client.put(
        "/api/v1/admin/contests/%d/judge/score" % judge_setup["contest_id"],
        json={"player_id": judge_setup["player_id"], "problem": "p1", "score": 60},
        headers=admin_headers,
    )
    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 100}')

    response = client.post(
        "/api/v1/admin/contests/%d/judge/rescan" % judge_setup["contest_id"],
        headers=admin_headers,
    )
    assert response.json()["manual"] == 1

    with judge_setup["ctx"].db.session() as session:
        run = session.execute(select(JudgeRun)).scalar_one()
    assert run.score == 60


def test_clearing_manual_score_re_enables_auto_scan(judge_setup, client: TestClient,
                                                    admin_headers: dict) -> None:
    """手工值只能由教师显式清除 —— 清除之后自动扫描恢复接管。"""
    client.put(
        "/api/v1/admin/contests/%d/judge/score" % judge_setup["contest_id"],
        json={"player_id": judge_setup["player_id"], "problem": "p1", "score": 60},
        headers=admin_headers,
    )
    clear = client.delete(
        "/api/v1/admin/contests/%d/judge/score" % judge_setup["contest_id"],
        params={"player_id": judge_setup["player_id"], "problem": "p1"},
        headers=admin_headers,
    )
    assert clear.status_code == 200

    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 100}')
    do_scan(judge_setup)

    with judge_setup["ctx"].db.session() as session:
        run = session.execute(select(JudgeRun)).scalar_one()
    assert run.score == 100
    assert run.parse_status == "ok"


def test_manual_score_requires_valid_player(judge_setup, client: TestClient,
                                            admin_headers: dict) -> None:
    response = client.put(
        "/api/v1/admin/contests/%d/judge/score" % judge_setup["contest_id"],
        json={"player_id": 99999, "problem": "p1", "score": 60},
        headers=admin_headers,
    )
    assert response.status_code == 404


def test_rescan_endpoint(judge_setup, client: TestClient, admin_headers: dict) -> None:
    write_result(judge_setup, judge_setup["player_no"], "p1", "r.json", '{"score": 100}')
    response = client.post(
        "/api/v1/admin/contests/%d/judge/rescan" % judge_setup["contest_id"],
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["parsed"] == 1


def test_list_judge_runs_filter_by_status(judge_setup, client: TestClient,
                                          admin_headers: dict) -> None:
    problem_dir = judge_setup["root"] / judge_setup["player_no"] / "p1"
    problem_dir.mkdir(parents=True, exist_ok=True)
    (problem_dir / "r.json").write_text("坏掉的内容 {", encoding="utf-8")
    do_scan(judge_setup)

    runs = client.get(
        "/api/v1/admin/contests/%d/judge/runs?parse_status=unparsed" % judge_setup["contest_id"],
        headers=admin_headers,
    ).json()
    assert len(runs) == 1
    assert runs[0]["parse_status"] == "unparsed"

    runs = client.get(
        "/api/v1/admin/contests/%d/judge/runs?parse_status=ok" % judge_setup["contest_id"],
        headers=admin_headers,
    ).json()
    assert runs == []


def test_scores_require_admin(judge_setup, client: TestClient) -> None:
    response = client.get(
        "/api/v1/admin/contests/%d/scores" % judge_setup["contest_id"]
    )
    assert response.status_code == 401
