"""数据库迁移测试。

迁移是这个项目里**唯一一处出错就会毁掉已有数据**的代码，所以测试的重点不是
"新库能建起来"（那几乎总会通过），而是**老库能不能安全升上来**。

具体守住三件事：

1. 老库（缺列、``user_version=0``）升级后：新列在、老数据**一行不少**、
   原有的关联数据（这里是 ``agent_status``）**没有被外键级联吃掉**。
2. 升级是幂等的：跑第二遍什么都不做。
3. 全新库与"老库升级上来的库"结构一致 —— 否则测试永远只覆盖到全新的那条路径，
   而线上跑的是升级上来的那些。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from syncoj_server import migrations


def make_engine(path: Path):
    engine = create_engine("sqlite+pysqlite:///" + str(path).replace("\\", "/"), future=True)
    return engine


#: 迁移器出现**之前**的结构，只覆盖这次迁移真正会碰到的表。
#:
#: 刻意手写而不是从模型生成：从模型生成的话，"模型改了但忘了写迁移"这件事
#: 就永远不会被测出来 —— 生成出来的老结构会跟着模型一起变，两边永远一致。
#: 手写会腐烂，所以下面配了一条 ``test_legacy_fixture_...`` 专门看守它。
#:
#: 定义逐字照抄当时的模型（DDL 从 ``CreateTable`` 导出），只有几处不同：
#: ``agent`` / ``contest`` 去掉本次新增的列，``problem`` 去掉上一次加漏迁移的列，
#: ``agent_release`` 去掉迁移 006 才加的 ``bootstrap_key_id``。
#:
#: **``contest`` 里那条 ``player_notice`` 是故意留着的**，它是本文件唯一一处
#: "老结构比迁移器出现那天新"的地方：迁移 003 把它加上去、迁移 005 又把它拿掉，
#: 而现在每一个真的开发库（``runtime/server/syncoj.db`` 停在版本 4）都带着这一列。
#: 不把它放进来，005 的"重建删列"就只在我另写的那份测试里被覆盖，而这套
#: "升级上来的库" 会走一条比真实情况更干净的路径 —— 那正是这套测试最怕的事。
LEGACY_SCHEMA = """
CREATE TABLE contest (
    id INTEGER NOT NULL PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    starts_at DATETIME,
    ends_at DATETIME,
    note TEXT,
    player_notice TEXT,
    created_at DATETIME NOT NULL
);
CREATE TABLE player (
    id INTEGER NOT NULL PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contest(id) ON DELETE CASCADE,
    player_no VARCHAR(64) NOT NULL,
    name VARCHAR(64),
    seat VARCHAR(32),
    group_name VARCHAR(64),
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_player_contest_no UNIQUE (contest_id, player_no)
);
CREATE TABLE agent (
    id INTEGER NOT NULL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    machine_id VARCHAR(128) NOT NULL,
    hostname VARCHAR(128),
    os_info VARCHAR(200),
    agent_version VARCHAR(32),
    enrolled_at DATETIME NOT NULL,
    last_enrolled_at DATETIME NOT NULL,
    last_seen_at DATETIME,
    revoked_at DATETIME,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_agent_player_machine UNIQUE (player_id, machine_id)
);
CREATE TABLE agent_status (
    agent_id INTEGER NOT NULL PRIMARY KEY REFERENCES agent(id) ON DELETE CASCADE,
    online BOOLEAN NOT NULL,
    last_tick_at DATETIME,
    last_seen_ip VARCHAR(64),
    agent_version VARCHAR(32),
    scan_root VARCHAR(512),
    file_count INTEGER NOT NULL,
    disk_free BIGINT,
    last_error TEXT,
    updated_at DATETIME NOT NULL
);
CREATE TABLE problem (
    id INTEGER NOT NULL PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contest(id) ON DELETE CASCADE,
    ident VARCHAR(64) NOT NULL,
    title VARCHAR(200),
    order_index INTEGER NOT NULL,
    note TEXT,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_problem_contest_ident UNIQUE (contest_id, ident)
);
CREATE TABLE agent_release (
    id INTEGER NOT NULL PRIMARY KEY,
    version VARCHAR(32) NOT NULL UNIQUE,
    channel VARCHAR(16) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    signature TEXT NOT NULL,
    size BIGINT NOT NULL,
    notes TEXT,
    published_at DATETIME,
    yanked_at DATETIME,
    created_at DATETIME NOT NULL
);
"""

#: 模型里有、老结构里没有的列 —— 迁移负责补上（无论是 ADD COLUMN 还是重建）。
#:
#: 手工维护，用来守上面那份 ``LEGACY_SCHEMA`` 不腐烂。模型加了列却没人写迁移时，
#: 这份清单对不上，测试就红 —— 那正是我们要拦的时刻。
MIGRATION_ADDED_COLUMNS = {
    "agent": {
        "roster_entry_id",
        "contest_id",
        "machine_uuid",
        "machine_fingerprint",
        "pair_code_hash",
        "pair_code_expires_at",
        "claimed_at",
        #: ``last_seen_ip``：选手页按来源 IP 认机器要用它（§5.6）。
        "last_seen_ip",
        #: ``release_public_key_at``：机器最近一次报告"本机有发布公钥"的时刻。
        #: 远程卸载授权的门禁要用它（迁移 007）—— 没有信任锚的机器验不了签名，
        #: 而在那种机器上签授权等于回一句假的"操作成功"。
        "release_public_key_at",
        #: ``scan_missing_json``：本轮不存在的扫描根（迁移 009）。机器列表上的
        #: 「选手目录还没建」与状态变化时才记的那条审计都靠它。
        "scan_missing_json",
        #: ``scan_skipped``：本轮被题目预设挡掉的文件数（迁移 011）。它同时喂两处：
        #: 机器列表上的「有 N 个文件不符合题目预设」，以及"有没有被挡掉"这个
        #: 状态变化的判据（按轮记的话，一台机器一个上午就能刷出上千条事件）。
        "scan_skipped",
        #: ``diagnostics_requested_at``：教师点过「要一份诊断」、等下一次心跳取走的
        #: 一次性标记（迁移 012）。必须落库 —— 只放内存的话，服务端在"点完"到
        #: "机器心跳"之间重启一次，这次请求就静默消失了。
        "diagnostics_requested_at",
    },
    #: ``player_notice`` 曾经在这一档里（迁移 003 加的"给选手看的注意事项"）。
    #: 考场公告改成"下发一份 NOTICE.md 文件"之后它没有读者了，所以本轮由迁移 005
    #: 真的把它删掉 —— 它现在在 ``MIGRATION_DROPPED_COLUMNS`` 里。
    "contest": {"default_roster_id"},
    "problem": {"file_patterns"},
    "player": set(),
    "agent_status": set(),
    #: ``bootstrap_key_id``：发布记录记住"这个包附带的是哪把统一注册密钥"（迁移 006）。
    #: 老结构里没有它 —— 那一轮的发布记录本来就不带密钥，升上来一律是 NULL。
    "agent_release": {
        "bootstrap_key_id",
        #: 随包带下去的三条安装策略（迁移 008）。老记录没有它们，而它们的实际行为
        #: 就是"不改配置、密钥不动、按默认模式升级" —— 读的那一侧按默认值补。
        "bootstrap_key_policy",
        "config_policy_json",
        "upgrade_mode",
    },
}

#: 老结构里有、模型里已经不要的列 —— 迁移负责**删掉**（只能靠重建表）。
#:
#: 这份清单是防"半迁移"的：删了列却没写迁移，老库会带着一列永远没人读的数据
#: 跑下去，而新库没有那一列 —— 两条路径从此结构不同。
#:
#: ``contest.enrollment_mode`` **不在这里**：它是迁移 001 自己加进去的、
#: 又被 002 拿掉的。对"迁移器之前建的库"来说它压根不存在，
#: 所以老结构和模型在这张表上是天然一致的。
#:
#: ``contest.player_notice`` **在这里**，因为 ``LEGACY_SCHEMA`` 里特意留了这一列
#: （理由见那边的说明）：它是 003 加的、005 拿掉的，而现在的开发库都带着它。
#: 这一条是防"半迁移"最直接的例子 —— 005 如果哪天被改坏成"只补列、不重建"，
#: 这条断言会当场变红。
MIGRATION_DROPPED_COLUMNS = {
    "agent": {"player_id"},
    "contest": {"player_notice"},
    "problem": set(),
    "player": set(),
    "agent_status": set(),
    "agent_release": set(),
}

#: "开考/结束时间列之前"的 contest 形状：只有这张表少那两列（外加一张挂在它下面的
#: ``player``，用来确认重建没有顺着外键把子表带走）。
#: 单独写一份而不是去改 ``LEGACY_SCHEMA``：那一份是"升级路径"的样本，不该为了
#: 一条边界测试变形。
PRE_WINDOW_SCHEMA = """
CREATE TABLE contest (
    id INTEGER NOT NULL PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    note TEXT,
    created_at DATETIME NOT NULL
);
CREATE TABLE player (
    id INTEGER NOT NULL PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contest(id) ON DELETE CASCADE,
    player_no VARCHAR(64) NOT NULL,
    name VARCHAR(64),
    seat VARCHAR(32),
    group_name VARCHAR(64),
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_player_contest_no UNIQUE (contest_id, player_no)
);
INSERT INTO contest (id, slug, name, status, note, created_at)
    VALUES (1, 'mock-old', '校内模拟赛', 'running', '老备注', '2026-01-01 00:00:00');
INSERT INTO player (id, contest_id, player_no, name, created_at)
    VALUES (1, 1, 'S001', '张三', '2026-01-01 00:00:00');
"""

OLD_DATA = """
INSERT INTO contest (id, slug, name, status, created_at)
    VALUES (1, 'mock-1', '校内模拟赛', 'running', '2026-01-01 00:00:00');
INSERT INTO player (id, contest_id, player_no, name, created_at)
    VALUES (1, 1, 'S001', '张三', '2026-01-01 00:00:00');
INSERT INTO agent (id, player_id, token_hash, machine_id, enrolled_at, last_enrolled_at, created_at)
    VALUES (1, 1, 'hash-a', 'machine-1', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
            '2026-01-01 00:00:00');
INSERT INTO agent_status (agent_id, online, file_count, disk_free, updated_at)
    VALUES (1, 1, 7, 123456, '2026-01-01 00:00:00');
INSERT INTO problem (id, contest_id, ident, order_index, created_at)
    VALUES (1, 1, 'p1', 1, '2026-01-01 00:00:00');
INSERT INTO agent_release
        (id, version, channel, sha256, signature, size, notes, published_at, created_at)
    VALUES (1, '1.4.2', 'stable', 'sha-of-bundle', 'sig-of-bundle', 2048,
            '老版本', '2026-01-02 00:00:00', '2026-01-01 00:00:00');
"""


@pytest.fixture()
def legacy_db(workdir: Path) -> Path:
    """造一个"迁移器出现之前建的"库，塞进真实数据。"""
    path = workdir / "legacy.db"
    engine = make_engine(path)
    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
        for statement in OLD_DATA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
    engine.dispose()
    return path


@pytest.fixture()
def pre_window_db(workdir: Path) -> Path:
    """造一个"contest 还没有开考/结束时间列"的库（见 ``PRE_WINDOW_SCHEMA``）。"""
    path = workdir / "pre_window.db"
    engine = make_engine(path)
    with engine.begin() as conn:
        for statement in PRE_WINDOW_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
    engine.dispose()
    return path


def rows(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


# --------------------------------------------------------------------------- #
# 看守老结构不腐烂
# --------------------------------------------------------------------------- #


def test_legacy_fixture_tracks_the_models(legacy_db: Path) -> None:
    """老结构 fixture 必须跟着模型走 —— 模型加了列却没写迁移，这里要红。

    这是整份测试里唯一一条能拦住"忘了写迁移"的断言。没有它，那些"升级后结构
    一致"的检查会因为两边都从同一份 fixture 出发而自动通过，
    而线上的老库会在某个接口上以 500 的形式炸掉。

    两个方向都要拦：

    * 模型有、老结构没有 → 迁移得补上（``MIGRATION_ADDED_COLUMNS``）
    * 老结构有、模型没有 → 迁移得删掉（``MIGRATION_DROPPED_COLUMNS``）

    第二个方向容易漏。删列只能靠重建表，于是"删了模型里的列、没写迁移"
    会留下一个永久漂移：老库带着一列没人读的数据跑，新库没有那一列 ——
    两条路径从此结构不同，而只有老库那条会出问题。
    """
    from syncoj_server.models import Base

    engine = make_engine(legacy_db)
    for table in MIGRATION_ADDED_COLUMNS:
        assert table in Base.metadata.tables, "模型里没有表 %s 了？" % table
        model_columns = {c.name for c in Base.metadata.tables[table].columns}
        legacy_columns = {c["name"] for c in inspect(engine).get_columns(table)}

        unexpected = model_columns - legacy_columns - MIGRATION_ADDED_COLUMNS[table]
        assert not unexpected, (
            "表 %s 在模型里有这些列，但既不在老结构里、也没被声明为迁移要补的：%s\n"
            "请给 migrations.py 加一步，或把它记进 MIGRATION_ADDED_COLUMNS。"
            % (table, sorted(unexpected))
        )

        stale = legacy_columns - model_columns - MIGRATION_DROPPED_COLUMNS[table]
        assert not stale, (
            "表 %s 的老结构里有模型已经不认的列：%s\n"
            "要么把它从 LEGACY_SCHEMA 里删掉，要么在迁移里真的删掉它"
            "（记进 MIGRATION_DROPPED_COLUMNS 表示「已知、由重建负责」）。"
            % (table, sorted(stale))
        )

        # 声明过的差异必须真的有人处理：只声明不实现的话，两条路径会悄悄分叉
        declared_add = MIGRATION_ADDED_COLUMNS[table]
        declared_drop = MIGRATION_DROPPED_COLUMNS[table]
        assert declared_add == model_columns - legacy_columns, (
            "表 %s 的 MIGRATION_ADDED_COLUMNS 和实际差异对不上：声明 %s，实际 %s"
            % (table, sorted(declared_add), sorted(model_columns - legacy_columns))
        )
        assert declared_drop == legacy_columns - model_columns, (
            "表 %s 的 MIGRATION_DROPPED_COLUMNS 和实际差异对不上：声明 %s，实际 %s"
            % (table, sorted(declared_drop), sorted(legacy_columns - model_columns))
        )
    engine.dispose()


# --------------------------------------------------------------------------- #
# 老库升级
# --------------------------------------------------------------------------- #


def test_legacy_database_is_stamped_and_upgraded(legacy_db: Path) -> None:
    engine = make_engine(legacy_db)
    assert migrations.current_version(engine) == 0

    applied = migrations.migrate(engine)

    assert applied, "老库应当有迁移要跑"
    assert migrations.current_version(engine) == migrations.LATEST_VERSION
    engine.dispose()


def test_legacy_upgrade_adds_the_new_columns(legacy_db: Path) -> None:
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    agent_columns = {c["name"] for c in inspect(engine).get_columns("agent")}
    contest_columns = {c["name"] for c in inspect(engine).get_columns("contest")}

    assert {"roster_entry_id", "contest_id", "machine_uuid", "pair_code_hash"} <= agent_columns
    assert {"default_roster_id"} <= contest_columns
    # 上一轮加的列也要被基线补齐（它当年没留迁移记录）
    assert "file_patterns" in {c["name"] for c in inspect(engine).get_columns("problem")}
    engine.dispose()


def test_legacy_upgrade_drops_the_retired_columns(legacy_db: Path) -> None:
    """``agent.player_id`` 与 ``contest.player_notice`` 必须真的消失。

    SQLite 删不了列，只能重建表 —— 而重建是这套迁移里唯一会"整表搬动"的动作。
    没有这条断言的话，重建漏掉了某一列也能跑过去，而两条升级路径从此结构分叉：
    老库带着一列永远没人读的数据跑，新库没有那一列。

    ``enrollment_mode`` 一起断言只是顺带（它在 ``LEGACY_SCHEMA`` 里压根不存在）。
    """
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    assert "player_id" not in {c["name"] for c in inspect(engine).get_columns("agent")}
    contest_columns = {c["name"] for c in inspect(engine).get_columns("contest")}
    assert "player_notice" not in contest_columns
    assert "enrollment_mode" not in contest_columns
    engine.dispose()


def test_upgrade_adds_missing_contest_window_columns(pre_window_db: Path) -> None:
    """缺 ``starts_at`` / ``ends_at`` 的老库要在迁移 005 里被补上，**数据一行不少**。

    这两列从服务端第一版起就在模型里，所以"缺列的库"大概率只存在于更早的结构里；
    但"老库少一列"正是这套迁移机制唯一存在的理由（``db reset`` 只兜底开发机），
    而它一旦真的发生，表现是每一个带时间判定的接口 500 —— 那种"只有升级上来的库
    才坏"的故障最难查。所以这里手工造一个缺列的库，把那条路真的跑一遍。

    顺带盯住重建的副作用：``contest`` 下面挂着 ``player``，外键是
    ``ON DELETE CASCADE``。重建没按配方关外键的话，这里的孩子会被静默删光。
    """
    engine = make_engine(pre_window_db)
    migrations.migrate(engine)

    columns = {
        c["name"]: bool(c["nullable"]) for c in inspect(engine).get_columns("contest")
    }
    # 两列都要在，而且要**可空**："没配时间"必须能表示成 NULL
    assert columns.get("starts_at") is True
    assert columns.get("ends_at") is True

    assert rows(engine, "SELECT slug, name, status, note FROM contest") == [
        ("mock-old", "校内模拟赛", "running", "老备注")
    ], "重建把场次那一行弄丢了（或改了内容）"
    assert rows(engine, "SELECT COUNT(*) FROM player")[0][0] == 1, (
        "重建 contest 时子表被外键级联删掉了 —— 外键没有真正关掉"
    )
    engine.dispose()


def test_legacy_upgrade_adds_the_release_bootstrap_key_column(legacy_db: Path) -> None:
    """发布记录要补出 ``agent_release.bootstrap_key_id``。

    老库那一列不存在（那一轮的发布记录本来就不带密钥）。迁移 006 走的是**重建表**，
    而不是 ADD COLUMN —— 因为这一列带着 ``ON DELETE SET NULL`` 外键，而 SQLite 的
    ``ALTER TABLE ADD COLUMN`` 不接受带动作的外键子句。所以这里同时盯两件事：
    列补出来了，而且**老发布记录一行不少、字段没被搬错**。
    """
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("agent_release")}
    assert "bootstrap_key_id" in columns

    release = rows(
        engine,
        "SELECT version, channel, sha256, signature, size, notes, bootstrap_key_id "
        "FROM agent_release WHERE id = 1",
    )
    assert release == [
        ("1.4.2", "stable", "sha-of-bundle", "sig-of-bundle", 2048, "老版本", None)
    ], "重建 agent_release 时老记录被弄丢或搬错了：%r" % (release,)
    engine.dispose()


def test_release_survives_its_bootstrap_key_being_deleted(legacy_db: Path) -> None:
    """密钥被删之后**版本记录必须还在**（``ON DELETE SET NULL`` 而不是 CASCADE）。

    "这个包是哪天发出去的、当时带的是哪把钥匙"是排查现场的第一手材料 ——
    随手清掉一行密钥记录不该把它一起带走。这条同时守住重建出来的那张表真的带上了
    ``ON DELETE SET NULL`` 这个动作（重建时漏掉它，这里会变成一行都不剩）。
    """
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    # 迁移链里重建表时要临时关掉外键（配方见 ``_rebuild_table``），这里按真实
    # 服务端那样用 connect 钩子把它打开 —— db.py 的 ``_install_pragmas`` 就是这么做的。
    # 不打开的话，下面那条 DELETE 不会触发 ON DELETE SET NULL，测的就不是真行为。
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # 连接池里那条连接是在钩子挂上**之前**建的（上面那次 migrate 用掉的），
    # 不 dispose 的话它会原样被复用，钩子永远不生效。
    engine.dispose()

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO bootstrap_key (id, key_hash, use_count, created_at)"
                 " VALUES (7, 'hash-7', 0, '2026-01-01 00:00:00')")
        )
        conn.execute(
            text("UPDATE agent_release SET bootstrap_key_id = 7 WHERE id = 1")
        )
        conn.execute(text("DELETE FROM bootstrap_key WHERE id = 7"))

    release = rows(engine, "SELECT version, bootstrap_key_id FROM agent_release WHERE id = 1")
    assert release == [("1.4.2", None)], (
        "密钥被删之后版本记录不该跟着消失（应当置 NULL 而不是级联删除）：%r" % (release,)
    )
    engine.dispose()


def test_legacy_upgrade_removes_the_retired_tables(legacy_db: Path) -> None:
    """注册码与待认领表整条链路都去掉了。

    新模型里"未配对"只是 ``agent.roster_entry_id IS NULL``，不需要单独一张表；
    注册码被统一密钥 + 配对取代。
    """
    engine = make_engine(legacy_db)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE enroll_code (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE machine_claim (id INTEGER PRIMARY KEY)"))

    migrations.migrate(engine)

    tables = set(inspect(engine).get_table_names())
    assert "enroll_code" not in tables
    assert "machine_claim" not in tables
    engine.dispose()


def test_legacy_rebuild_keeps_child_table_rows(legacy_db: Path) -> None:
    """**这条是这份文件里最重要的一条。**

    重建 ``agent`` 要关外键强制，而 ``PRAGMA foreign_keys`` **在事务里是空操作**。
    如果哪天有人把它改回 ``engine.begin()`` 里执行，``DROP TABLE agent`` 会做一次
    隐式 DELETE，顺着 ``ON DELETE CASCADE`` 把 ``agent_status`` 整个删光 ——
    静默、无报错，等到现场发现"在线记录全没了"时早就无从追查。

    这是做过对照实验的：裸连接 + autocommit 下关外键，子表行保留；
    在事务里关（无效），子表行归零。
    """
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    assert rows(engine, "SELECT COUNT(*) FROM agent")[0][0] == 1
    status = rows(engine, "SELECT agent_id, file_count, disk_free FROM agent_status")
    assert status == [(1, 7, 123456)], (
        "agent_status 的行没了 —— 这是外键没真正关掉的典型症状"
    )
    engine.dispose()


def test_legacy_upgrade_keeps_every_row(legacy_db: Path) -> None:
    """升级不能丢数据 —— 这是整个迁移机制存在的唯一理由。

    顺带覆盖一个具体的坑：如果哪天有人把"改 agent 表结构"写成
    "建新表 → 搬数据 → DROP 老表"，``agent_status`` 会顺着
    ``ON DELETE CASCADE`` 一起被删掉。这条断言就是为了在那一刻变红。
    """
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    assert rows(engine, "SELECT COUNT(*) FROM agent")[0][0] == 1
    assert rows(engine, "SELECT COUNT(*) FROM player")[0][0] == 1
    assert rows(engine, "SELECT COUNT(*) FROM contest")[0][0] == 1
    assert rows(engine, "SELECT COUNT(*) FROM problem")[0][0] == 1

    status = rows(engine, "SELECT agent_id, file_count, disk_free FROM agent_status")
    assert status == [(1, 7, 123456)], "agent_status 被动过 —— 这是级联删除的典型症状"

    assert rows(engine, "SELECT token_hash FROM agent")[0][0] == "hash-a"
    engine.dispose()


def test_migration_is_idempotent(legacy_db: Path) -> None:
    engine = make_engine(legacy_db)
    migrations.migrate(engine)
    second = migrations.migrate(engine)

    assert second == [], "第二次迁移应当什么都不做"
    assert rows(engine, "SELECT COUNT(*) FROM agent")[0][0] == 1
    engine.dispose()


def test_migration_twice_from_scratch_is_stable(workdir: Path) -> None:
    """空库连跑两次也要稳。"""
    engine = make_engine(workdir / "fresh.db")
    first = migrations.migrate(engine)
    second = migrations.migrate(engine)

    assert first and second == []
    assert migrations.current_version(engine) == migrations.LATEST_VERSION
    engine.dispose()


def test_failed_migration_does_not_advance_the_version(workdir: Path, monkeypatch) -> None:
    """某一步失败时版本号必须**留在原地**，下次启动重试同一步。

    如果失败也推进版本号，库就停在一个谁也没设计过的中间状态，
    而且再也不会有人去补 —— 这是最难查的一类线上故障。
    """
    engine = make_engine(workdir / "boom.db")

    def explode(_engine):
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(
        migrations, "MIGRATIONS", [(99, "会炸的一步", explode)]
    )
    with pytest.raises(RuntimeError, match="模拟迁移失败"):
        migrations.migrate(engine, create_missing_tables=False)

    assert migrations.current_version(engine) == 0, "失败的迁移不该推进版本号"
    engine.dispose()


# --------------------------------------------------------------------------- #
# 全新库与升级库的结构一致性
# --------------------------------------------------------------------------- #


def test_fresh_and_upgraded_databases_have_the_same_shape(
    workdir: Path, legacy_db: Path
) -> None:
    """两条路径必须收敛到同一个结构。

    只测全新库是最常见的自欺：线上跑的永远是"老库升上来的"那一条，
    而它恰恰是唯一没被测到的。
    """
    fresh = make_engine(workdir / "fresh.db")
    migrations.migrate(fresh)

    upgraded = make_engine(legacy_db)
    migrations.migrate(upgraded)

    def shape(engine, tables):
        return {
            table: sorted(
                (c["name"], str(c["type"]), bool(c["nullable"]))
                for c in inspect(engine).get_columns(table)
            )
            for table in tables
        }

    # 只比对本次迁移会碰到的那些表（见 LEGACY_SCHEMA 的说明），
    # 再加上迁移引入的新表 —— 它们必须真的被建出来
    # 本次迁移会碰到的表 + 迁移引入的新表。**已废弃的表不在其中** ——
    # 它们必须"不存在"，那是另一条测试（test_legacy_upgrade_removes_...）的事
    interesting = sorted(MIGRATION_ADDED_COLUMNS) + [
        "roster",
        "roster_entry",
        "bootstrap_key",
        #: 运行参数表（迁移 010）。它是"新表交给 create_all"这一类里最新的一张 ——
        #: 老库升上来必须也有它，否则读运行参数会撞 no such table。
        "runtime_setting",
        #: 诊断包表（迁移 012）。每台机器只留最新一份，同样由 create_all 负责建；
        #: 放进这份清单是为了让它也受"全新库与升级库结构一致"的看守。
        "agent_diagnostic",
    ]
    fresh_tables = set(inspect(fresh).get_table_names())
    for table in interesting:
        assert table in fresh_tables, "全新库里没有表 %s" % table

    fresh_shape = shape(fresh, interesting)
    upgraded_shape = shape(upgraded, interesting)

    for table in interesting:
        assert fresh_shape[table] == upgraded_shape[table], (
            "表 %s 在「全新库」与「升级库」里结构不一致：\n  全新: %s\n  升级: %s"
            % (table, fresh_shape[table], upgraded_shape[table])
        )

    fresh.dispose()
    upgraded.dispose()
