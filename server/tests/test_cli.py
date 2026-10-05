"""命令行场次/选手管理测试。

界面已经能做这些事了，CLI 存在的意义是**脚本化部署**：按教务系统导出的名单
一次灌进去、批量打印注册码。现场常见做法是把名单从 Excel 复制到文件里，
所以解析必须容忍表头、注释行、各种分隔符。
"""

from __future__ import annotations

import contextlib
import io
import os

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
# 统一密钥与待配对机器
#
# "逐台签注册码"已经被"镜像里一把统一密钥"取代，所以这一节盯的是新模型里
# 两件在脚本化部署中真正要做的事：**把密钥装进镜像**、**看哪些机器还没配对**。
# --------------------------------------------------------------------------- #


def open_ctx(cli_env):
    settings = Settings()
    settings.data_root = cli_env["data_root"]
    from syncoj_server.context import AppContext

    return AppContext.create(settings)


def issue_key(cli_env) -> str:
    """签发一把密钥并把明文从输出里捡回来。"""
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert run_cli(cli_env, ["bootstrap-key", "issue"]) == 0
    # 明文是唯一一行"看起来像密钥"的东西：32 字节 base64url
    candidates = [
        line.strip()
        for line in buffer.getvalue().splitlines()
        if len(line.strip()) >= 32 and "," not in line and not line.startswith("[+]")
    ]
    assert candidates, "没能从输出里解析出密钥明文：%r" % buffer.getvalue()
    return candidates[0]


def test_bootstrap_key_is_stored_hashed_not_plaintext(cli_env) -> None:
    """服务端只能存哈希 —— 库里出现明文密钥等于把整间机房的注册权摊在磁盘上。"""
    plain = issue_key(cli_env)

    from syncoj_server.models import BootstrapKey

    with open_ctx(cli_env).db.session() as session:
        key = session.execute(select(BootstrapKey)).scalar_one()
    assert plain not in key.key_hash
    assert len(key.key_hash) == 64


def test_bootstrap_key_issue_writes_a_private_file(cli_env) -> None:
    """装机时密钥要放进 root 只读的 /etc 里 —— 文件权限必须一开始就是 0600。

    先建成 0600 再写内容，否则会有"已经写进去了、但权限还是 644"的一瞬间。
    """
    target = cli_env["workdir"] / "bootstrap.key"
    assert run_cli(cli_env, ["bootstrap-key", "issue", "--out", str(target)]) == 0

    assert target.is_file()
    body = target.read_text(encoding="utf-8").strip()
    assert body, "文件里必须有明文密钥，否则装机脚本读到空值"
    assert "\r" not in target.read_text(encoding="utf-8"), "换行必须是 LF，systemd 会读它"

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        assert (target.stat().st_mode & 0o077) == 0, "同组/其他用户不该读得到"

    from syncoj_server.security import hash_bootstrap_key
    from syncoj_server.models import BootstrapKey

    with open_ctx(cli_env).db.session() as session:
        key = session.execute(select(BootstrapKey)).scalar_one()
    assert key.key_hash == hash_bootstrap_key(body)


def test_revoked_bootstrap_key_cannot_enroll_any_more(cli_env) -> None:
    """吊销只挡住"以后还想拿它注册"的机器 —— 已经注册好的机器不受影响。

    这条是排错方向的分水岭：如果吊销会把在线机器一起踢掉，
    教师就不敢在比赛期间吊销泄漏出去的密钥。
    """
    from syncoj_server.errors import ERROR_CODES
    from syncoj_server.models import BootstrapKey
    from syncoj_server import services

    plain = issue_key(cli_env)
    ctx = open_ctx(cli_env)

    from syncoj_server.services import enrollment

    def try_enroll(ctx, machine_uuid):
        with ctx.db.session() as session:
            try:
                enrollment.enroll_machine(
                    session, plain,
                    machine_id="m-1", machine_uuid=machine_uuid,
                    machine_fingerprint=None, hostname="pc", os_info=None,
                    agent_version="0.1.0", raw_token="t", raw_pair_code="123456",
                    offline_after_seconds=180,
                )
                return "ok"
            except enrollment.BootstrapRejected as exc:
                assert exc.code in ERROR_CODES, exc.code
                return exc.code

    assert try_enroll(ctx, "uuid-1") == "ok"

    with open_ctx(cli_env).db.session() as session:
        key = session.execute(select(BootstrapKey)).scalar_one()
        key_id = key.id

    assert run_cli(cli_env, ["bootstrap-key", "revoke", "--id", str(key_id)]) == 0

    fresh = open_ctx(cli_env)
    assert try_enroll(fresh, "uuid-2") == "bootstrap_key_revoked"


def test_bootstrap_key_revoke_unknown_id_fails(cli_env) -> None:
    assert run_cli(cli_env, ["bootstrap-key", "revoke", "--id", "999"]) == 1


def pending_rows(output: str) -> list:
    """只留真正的数据行（以 id 开头）—— 表头与结尾提示都不是行。"""
    return [line for line in output.splitlines() if line[:1].isdigit()]


def test_pending_lists_machines_that_are_not_paired_yet(cli_env, capsys) -> None:
    """``bootstrap-key pending`` 是现场排查的第一站：机器到底上来没有。

    按**最后心跳倒序** —— 教师站在机器前的时候，那台机器刚刚才心跳过，
    它就该在最上面；按注册时间排会让人从一堆久未上线的机器里翻找。
    """
    from syncoj_server.services import enrollment

    plain = issue_key(cli_env)
    ctx = open_ctx(cli_env)
    with ctx.db.session() as session:
        for i, hostname in enumerate(["pc-a", "pc-b", "pc-c"]):
            enrollment.enroll_machine(
                session, plain,
                machine_id="machine-%d" % i, machine_uuid="uuid-%d" % i,
                machine_fingerprint=None, hostname=hostname, os_info=None,
                agent_version="0.1.0", raw_token="token-%d" % i, raw_pair_code="12345%d" % i,
                offline_after_seconds=180,
            )

    capsys.readouterr()
    assert run_cli(cli_env, ["bootstrap-key", "pending"]) == 0
    rows = pending_rows(capsys.readouterr().out)
    assert len(rows) == 3, rows
    assert "pc-c" in rows[0], "最后心跳的那台必须排在最前面"
    assert "1799 秒" in rows[0] or "秒" in rows[0], "要显示配对码还剩多久过期"


def test_pending_says_so_when_there_is_nothing_to_pair(cli_env, capsys) -> None:
    """空列表要说人话，而不是印一个光秃秃的表头。"""
    assert run_cli(cli_env, ["bootstrap-key", "pending"]) == 0
    assert "没有待配对的机器" in capsys.readouterr().out
    assert pending_rows(capsys.readouterr().out) == []


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
    key_file = cli_env["workdir"] / "bootstrap.key"
    assert run_cli(cli_env, ["bootstrap-key", "issue", "--out", str(key_file)]) == 0
    assert key_file.read_text(encoding="utf-8").strip(), "密钥必须真的写进文件"

    assert run_cli(cli_env, ["contest", "list"]) == 0

    output = capsys.readouterr().out
    assert "mock" in output
    assert len(load_players(cli_env, "mock")) == 2

    # 装机的下一步是"把机器配上来"，所以 init 的提示也必须指向新路径
    hint = io.StringIO()
    with contextlib.redirect_stdout(hint):
        assert run_cli(cli_env, ["init", "--admin-password", "x" * 12]) == 0
    assert "bootstrap-key" in hint.getvalue() or "机器配对" in hint.getvalue()
    assert "enroll-code" not in hint.getvalue(), "旧注册码链路已经不存在了"
    assert "注册码" not in hint.getvalue()
