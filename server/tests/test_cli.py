"""命令行场次/选手管理测试。

界面已经能做这些事了，CLI 存在的意义是**脚本化部署**：按教务系统导出的名单
一次灌进去、批量打印注册码。现场常见做法是把名单从 Excel 复制到文件里，
所以解析必须容忍表头、注释行、各种分隔符。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from syncoj_server.cli import _read_roster, main
from syncoj_server.config import Settings


@pytest.fixture()
def cli_env(workdir, monkeypatch):
    """把 CLI 指到临时数据目录。

    CLI 通过 ``--data-root`` 接收路径，不需要环境变量 —— 显式传参更不容易出错。
    """
    data_root = workdir / "data"
    base = ["--data-root", str(data_root)]
    return {"workdir": workdir, "data_root": data_root, "base": base}


def run_cli(cli_env, argv):
    return main(cli_env["base"] + argv)


def lines_of(output: str) -> list:
    """从 CLI 输出里挑出真正的数据行，滤掉提示语。

    `capsys.readouterr()` 会把自上次读取以来的**全部**输出一起给出，
    所以调用方要么先清一次缓冲，要么就把噪声滤掉 —— 这里选后者，更不容易写错。
    """
    return [
        line
        for line in output.splitlines()
        if line
        and not line.startswith(("[+]", "选手编号", "#", "下一步", "  "))
    ]


# --------------------------------------------------------------------------- #
# 名单解析
# --------------------------------------------------------------------------- #


def test_read_roster_comma_separated(workdir) -> None:
    path = workdir / "roster.csv"
    path.write_text("S001,张三,A1,A组\nS002,李四,A2,A组\n", encoding="utf-8")
    assert _read_roster(str(path)) == [
        ("S001", "张三", "A1", "A组"),
        ("S002", "李四", "A2", "A组"),
    ]


def test_read_roster_tab_separated(workdir) -> None:
    """从 Excel 直接复制粘贴得到的是制表符分隔。"""
    path = workdir / "roster.tsv"
    path.write_text("S001\t张三\tA1\tA组\nS002\t李四\n", encoding="utf-8")
    assert _read_roster(str(path)) == [
        ("S001", "张三", "A1", "A组"),
        ("S002", "李四", "", ""),
    ]


def test_read_roster_skips_header_and_comments(workdir) -> None:
    """现场手写的名单经常带表头和注释，不该被当成选手。"""
    path = workdir / "roster.csv"
    path.write_text(
        "# 2025 校内模拟赛名单\n"
        "选手编号,姓名,座位,分组\n"
        "S001,张三,A1,A组\n"
        "\n"
        "# 以下为补录\n"
        "S002,李四,A2,A组\n",
        encoding="utf-8",
    )
    assert _read_roster(str(path)) == [
        ("S001", "张三", "A1", "A组"),
        ("S002", "李四", "A2", "A组"),
    ]


def test_read_roster_accepts_bom(workdir) -> None:
    """Excel 导出的 CSV 常带 BOM，不加处理的话第一列会多一个不可见字符。"""
    path = workdir / "roster.csv"
    path.write_bytes("\ufeffS001,张三\n".encode("utf-8"))
    assert _read_roster(str(path)) == [("S001", "张三", "", "")]


def test_read_roster_only_number_is_enough(workdir) -> None:
    path = workdir / "roster.csv"
    path.write_text("S001\nS002\n", encoding="utf-8")
    assert _read_roster(str(path)) == [("S001", "", "", ""), ("S002", "", "", "")]


def test_read_roster_empty_file(workdir) -> None:
    path = workdir / "empty.csv"
    path.write_text("", encoding="utf-8")
    assert _read_roster(str(path)) == []


# --------------------------------------------------------------------------- #
# 建场次
# --------------------------------------------------------------------------- #


def test_create_contest(cli_env) -> None:
    assert run_cli(cli_env, ["contest", "create", "--name", "2025 校内模拟赛"]) == 0

    settings = Settings()
    settings.data_root = cli_env["data_root"]
    from syncoj_server.context import AppContext
    from syncoj_server.models import Contest

    ctx = AppContext.create(settings)
    with ctx.db.session() as session:
        contest = session.execute(select(Contest)).scalar_one()
    assert contest.name == "2025 校内模拟赛"
    assert contest.slug == "2025"
    assert contest.status == "running"


def test_create_contest_with_explicit_slug(cli_env) -> None:
    assert run_cli(
        cli_env, ["contest", "create", "--name", "测试", "--slug", "mock-1", "--status", "draft"]
    ) == 0

    settings = Settings()
    settings.data_root = cli_env["data_root"]
    from syncoj_server.context import AppContext
    from syncoj_server.models import Contest

    ctx = AppContext.create(settings)
    with ctx.db.session() as session:
        contest = session.execute(select(Contest)).scalar_one()
    assert contest.slug == "mock-1"
    assert contest.status == "draft"


def test_create_duplicate_slug_fails(cli_env) -> None:
    assert run_cli(cli_env, ["contest", "create", "--name", "A", "--slug", "same"]) == 0
    # 第二次应当失败并给出非零退出码，而不是静默创建第二个同标识场次
    assert run_cli(cli_env, ["contest", "create", "--name", "B", "--slug", "same"]) == 1


def test_contest_list_on_empty_database(cli_env) -> None:
    assert run_cli(cli_env, ["contest", "list"]) == 0


# --------------------------------------------------------------------------- #
# 导入选手
# --------------------------------------------------------------------------- #


def prepare_contest(cli_env, name="A", slug="c1") -> None:
    assert run_cli(cli_env, ["contest", "create", "--name", name, "--slug", slug]) == 0


def load_players(cli_env, slug="c1"):
    settings = Settings()
    settings.data_root = cli_env["data_root"]
    from syncoj_server.context import AppContext
    from syncoj_server.models import Contest, Player

    ctx = AppContext.create(settings)
    with ctx.db.session() as session:
        contest = session.execute(select(Contest).where(Contest.slug == slug)).scalar_one()
        return list(
            session.execute(
                select(Player).where(Player.contest_id == contest.id).order_by(Player.player_no)
            ).scalars()
        )


def test_import_players(cli_env) -> None:
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("S001,张三,A1,A组\nS002,李四,A2,B组\n", encoding="utf-8")

    assert run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)]) == 0

    players = load_players(cli_env)
    assert [p.player_no for p in players] == ["S001", "S002"]
    assert players[0].name == "张三"
    assert players[1].group_name == "B组"


def test_import_players_is_idempotent(cli_env) -> None:
    """同一份名单反复导入不该产生重复选手 —— 补录名单是常态。"""
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("S001,张三\n", encoding="utf-8")

    for _ in range(3):
        assert run_cli(
            cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)]
        ) == 0

    assert len(load_players(cli_env)) == 1


def test_import_players_updates_existing(cli_env) -> None:
    """重复导入时以新名单为准（改座位、改分组是常见需求）。"""
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"

    roster.write_text("S001,张三,A1,A组\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    roster.write_text("S001,张三,A9,Z组\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    players = load_players(cli_env)
    assert len(players) == 1
    assert players[0].seat == "A9"
    assert players[0].group_name == "Z组"


def test_import_players_appends(cli_env) -> None:
    """第二次导入只新增，不动已有选手。"""
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"

    roster.write_text("S001,张三\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    roster.write_text("S002,李四\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    assert [p.player_no for p in load_players(cli_env)] == ["S001", "S002"]


def test_import_into_missing_contest_fails(cli_env) -> None:
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("S001\n", encoding="utf-8")
    assert run_cli(
        cli_env, ["contest", "import-players", "--contest", "nope", "--file", str(roster)]
    ) == 1


def test_import_empty_roster_fails(cli_env) -> None:
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("# 只有注释\n", encoding="utf-8")
    assert run_cli(
        cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)]
    ) == 1


# --------------------------------------------------------------------------- #
# 签发注册码
# --------------------------------------------------------------------------- #


def test_enroll_codes_issued_for_all_players(cli_env, capsys) -> None:
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("S001,张三\nS002,李四\nS003,王五\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    assert run_cli(cli_env, ["contest", "enroll-codes", "--contest", "c1"]) == 0

    output = capsys.readouterr().out
    lines = lines_of(output)
    assert len(lines) == 3, "期望 3 行注册码，实际：%r" % lines
    codes = [line.split(",")[1] for line in lines]
    assert len(set(codes)) == 3, "每个选手的注册码必须不同"
    assert all(len(code) >= 16 for code in codes)


def test_enroll_codes_stored_hashed_not_plaintext(cli_env, capsys) -> None:
    """服务端只能存哈希 —— 库里出现明文注册码等于把凭据摊在磁盘上。"""
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("S001\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    capsys.readouterr()  # 丢掉前面的噪声
    run_cli(cli_env, ["contest", "enroll-codes", "--contest", "c1"])
    plain = lines_of(capsys.readouterr().out)[0].split(",")[1]
    assert plain, "没解析出注册码，测试前提不成立"

    settings = Settings()
    settings.data_root = cli_env["data_root"]
    from syncoj_server.context import AppContext
    from syncoj_server.models import EnrollCode

    ctx = AppContext.create(settings)
    with ctx.db.session() as session:
        code = session.execute(select(EnrollCode)).scalar_one()
    assert plain not in code.code_hash
    assert len(code.code_hash) == 64


def test_enroll_codes_revokes_previous(cli_env, capsys) -> None:
    """重新签发必须吊销旧的 —— 否则一张流落在外的旧注册码仍然能用。"""
    prepare_contest(cli_env)
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text("S001\n", encoding="utf-8")
    run_cli(cli_env, ["contest", "import-players", "--contest", "c1", "--file", str(roster)])

    run_cli(cli_env, ["contest", "enroll-codes", "--contest", "c1"])
    capsys.readouterr()
    run_cli(cli_env, ["contest", "enroll-codes", "--contest", "c1"])
    capsys.readouterr()

    settings = Settings()
    settings.data_root = cli_env["data_root"]
    from syncoj_server.context import AppContext
    from syncoj_server.models import EnrollCode

    ctx = AppContext.create(settings)
    with ctx.db.session() as session:
        codes = list(session.execute(select(EnrollCode)).scalars())
    assert len(codes) == 2
    assert sum(1 for c in codes if c.revoked_at is not None) == 1, "旧码应当被吊销"
    assert sum(1 for c in codes if c.revoked_at is None) == 1


def test_enroll_codes_without_players_fails(cli_env) -> None:
    prepare_contest(cli_env)
    assert run_cli(cli_env, ["contest", "enroll-codes", "--contest", "c1"]) == 1


# --------------------------------------------------------------------------- #
# 整体流程
# --------------------------------------------------------------------------- #


def test_full_setup_from_command_line(cli_env, capsys) -> None:
    """一条命令链把场次准备好 —— 这正是 CLI 存在的意义。"""
    roster = cli_env["workdir"] / "roster.csv"
    roster.write_text(
        "# 2025 校内模拟赛\n选手编号,姓名,座位,分组\nS001,张三,A1,A组\nS002,李四,A2,B组\n",
        encoding="utf-8",
    )

    assert run_cli(cli_env, ["contest", "create", "--name", "模拟赛", "--slug", "mock"]) == 0
    assert run_cli(
        cli_env, ["contest", "import-players", "--contest", "mock", "--file", str(roster)]
    ) == 0
    assert run_cli(cli_env, ["contest", "enroll-codes", "--contest", "mock"]) == 0
    assert run_cli(cli_env, ["contest", "list"]) == 0

    output = capsys.readouterr().out
    assert "mock" in output
    assert "S001" in output and "S002" in output
    assert len(load_players(cli_env, "mock")) == 2
