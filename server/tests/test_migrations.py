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
#: 定义逐字照抄当时的模型（DDL 从 ``CreateTable`` 导出），只有三处不同：
#: ``agent`` / ``contest`` 去掉本次新增的列，``problem`` 去掉上一次加漏迁移的列。
LEGACY_SCHEMA = """
CREATE TABLE contest (
    id INTEGER NOT NULL PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    starts_at DATETIME,
    ends_at DATETIME,
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
"""

#: 手工维护，用来守上面的 ``LEGACY_SCHEMA`` 不腐烂：这些表在模型里的列，
#: 与老结构之间**允许**有哪些差异（也就是迁移负责补上的那些）。
#:
#: 模型新增了列却忘了写迁移时，这份清单会红 —— 那正是我们要拦的时刻。
DECLARED_DIFFERENCES = {
    "agent": {"machine_uuid", "machine_fingerprint", "claimed_at"},
    "contest": {"default_roster_id", "enrollment_mode"},
    "problem": {"file_patterns"},
    "player": set(),
    "agent_status": set(),
}

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


def rows(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


# --------------------------------------------------------------------------- #
# 看守老结构不腐烂
# --------------------------------------------------------------------------- #


def test_legacy_fixture_tracks_the_models(legacy_db: Path) -> None:
    """老结构 fixture 必须跟着模型走 —— 模型加了列却没写迁移，这里要红。

    这是整份测试里唯一一条能拦住"忘了写迁移"的断言。没有它，上面那些
    "升级后结构一致"的检查会因为两边都从同一份 fixture 出发而自动通过，
    线上的老库则会在某个接口上以 500 的形式炸掉。

    红的时候有两种正确反应，选一种：
      * 给 ``migrations.py`` 加一步 ``_add_column``（真加了列）
      * 把它记进 ``DECLARED_DIFFERENCES`` 并在 ``_BASELINE_COLUMNS`` 里补上
        （确认这列本来就该由基线补齐）
    """
    from syncoj_server.models import Base

    engine = make_engine(legacy_db)
    for table, allowed in DECLARED_DIFFERENCES.items():
        assert table in Base.metadata.tables, "模型里没有表 %s 了？" % table
        model_columns = {c.name for c in Base.metadata.tables[table].columns}
        legacy_columns = {c["name"] for c in inspect(engine).get_columns(table)}

        unexpected = model_columns - legacy_columns - allowed
        assert not unexpected, (
            "表 %s 在模型里有这些列，但既不在老结构里、也没被声明为迁移要补的：%s\n"
            "请给 migrations.py 加一步，或者把它记进 DECLARED_DIFFERENCES。"
            % (table, sorted(unexpected))
        )

        stale = legacy_columns - model_columns
        assert not stale, (
            "表 %s 的老结构里有模型已经不认的列：%s —— fixture 该更新了"
            % (table, sorted(stale))
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

    assert {"machine_uuid", "machine_fingerprint", "claimed_at"} <= agent_columns
    assert {"default_roster_id", "enrollment_mode"} <= contest_columns
    # 上一轮加的列也要被基线补齐（它当年没留迁移记录）
    assert "file_patterns" in {c["name"] for c in inspect(engine).get_columns("problem")}
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


def test_legacy_upgrade_backfills_enrollment_mode(legacy_db: Path) -> None:
    """老场次必须被理解为"每选手注册码" —— 那本来就是它们的行为。

    留成 NULL 的话，新代码读到的默认值如果有偏差，老场次的行为会悄悄变掉。
    """
    engine = make_engine(legacy_db)
    migrations.migrate(engine)

    mode = rows(engine, "SELECT enrollment_mode FROM contest WHERE id=1")[0][0]
    assert mode == "per_player_code"
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
    interesting = sorted(DECLARED_DIFFERENCES) + [
        "roster",
        "roster_entry",
        "bootstrap_key",
        "machine_claim",
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
