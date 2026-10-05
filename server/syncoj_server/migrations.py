"""数据库结构迁移。

为什么需要它
------------
``Base.metadata.create_all()`` 只会**建缺失的表**。它看不见"已有表少了一列"，
于是给老库加字段只能靠人肉 ``ALTER TABLE`` —— 一旦有人忘了，故障会以
"升级后某个接口 500" 的形式出现在考场上，那时候再查已经晚了。

这里用 SQLite 自带的 ``PRAGMA user_version`` 做一个足够小的迁移器：版本号是个整数，
迁移是一串"从 N 到 N+1"的步骤，按序执行、有事务。对单文件 SQLite 来说这就够了，
引入 Alembic 要多一层依赖和一整套要学的目录约定。

三条规矩
--------
1. **只前进，不回退。** 回退要处理数据丢失，收益远小于风险；要退就还原备份。
2. **绝不在迁移里重建（rebuild）已有表。** SQLite 改不了列的可空性，网上给出的
   办法是"建新表→搬数据→换名"，但那需要关闭外键强制，而 ``PRAGMA foreign_keys``
   **在事务里是空操作**。真的关不掉时，``DROP TABLE`` 会做一次隐式 DELETE，
   顺着 ``ON DELETE CASCADE`` 把子表数据一起删掉 —— 在考试系统里这是不可接受的
   风险。所以本轮的"未认领机器"**另立一张表**（``machine_claim``），
   而不是把 ``agent.player_id`` 改成可空。
3. **新表交给 create_all，新列交给迁移。** 这个分工省掉了在迁移里写
   ``CREATE TABLE``，也解释了为什么下面的步骤里看不到建表语句。
"""

from __future__ import annotations

import logging
from typing import Callable, List, Sequence, Tuple

from sqlalchemy import Engine, inspect, text

__all__ = ["LATEST_VERSION", "MIGRATIONS", "current_version", "migrate"]

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _tables(engine: Engine) -> List[str]:
    return inspect(engine).get_table_names()


def _columns(engine: Engine, table: str) -> List[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return []
    return [column["name"] for column in inspector.get_columns(table)]


def _add_column(engine: Engine, table: str, name: str, ddl: str) -> bool:
    """加一列。返回是否真的加了。

    DDL 里带 ``NOT NULL`` 时必须自带 ``DEFAULT``：SQLite 的 ADD COLUMN
    不允许给已有行留空。这里一律加可空列，回填由迁移步骤自己做 ——
    "加列"和"填什么值"是两件事，混在一起会让回填逻辑没法单独重试。
    """
    if name in _columns(engine, table):
        return False
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl)))
    log.info("迁移：%s 增加列 %s", table, name)
    return True


def _rebuild_table(engine: Engine, table: str) -> bool:
    """按**当前模型**重建一张表，保留同名同列的数据。

    为什么需要它：SQLite 改不了列的可空性、也删不掉列（3.35 之前），
    而"删掉 ``agent.player_id``"这种事只能靠重建。

    **必须用裸连接在 autocommit 下关外键。** 对照实验（``.pytest-tmp`` 里留下过）：

    ================================================  =========  ==============
    重建时外键                                        agent 数据  agent_status
    ================================================  =========  ==============
    裸连接 + autocommit 里 ``PRAGMA foreign_keys=OFF``  保留       **保留**
    在 ``engine.begin()`` 里关（**空操作**）             保留       **被级联删光**
    ================================================  =========  ==============

    ``PRAGMA foreign_keys`` **在事务里是空操作** —— 这是整件事唯一的坑，
    而它的后果是静默删掉整张子表。所以这里刻意不走 ``engine.begin()``，
    而是自己 ``BEGIN`` / ``COMMIT``。

    ``legacy_alter_table=ON`` 同理必要：现代 SQLite 的 ``RENAME`` 会顺手改写
    *其它表*里对被改名表的引用，而我们正是要在别的表还引用 ``agent`` 的情况下
    把它换掉。
    """
    from sqlalchemy.schema import CreateIndex, CreateTable

    from .models import Base

    if table not in Base.metadata.tables or table not in _tables(engine):
        return False

    model_table = Base.metadata.tables[table]
    new_name = "%s__new" % table

    # DDL 从**当前模型**生成，而不是手写一遍：手写的 DDL 会和模型一起腐烂，
    # 而"迁移建出来的表和模型不一致"极难发现（新库没有它、老库带着它）。
    #
    # 表名靠文本替换改掉。试过 ``Table.to_metadata(name=...)``，它因为
    # 目标 MetaData 里已经有同名表而**静默不复制**（只在日志里留一条 SAWarning），
    # 于是后面拿到的还是旧定义 —— 那种失败方式在这里是灾难性的。
    # 只替换 `CREATE TABLE <名字> (` 这一处，不碰约束名里的同名片段。
    old_prefix = "CREATE TABLE %s (" % table
    ddl = str(CreateTable(model_table).compile(dialect=engine.dialect))
    if old_prefix not in ddl:  # pragma: no cover - 方言变了才会走到
        raise RuntimeError("无法重写建表语句的表名：%s" % ddl[:120])
    create_sql = ddl.replace(old_prefix, "CREATE TABLE %s (" % new_name, 1)

    old_columns = _columns(engine, table)
    shared = [c.name for c in model_table.columns if c.name in old_columns]

    raw = engine.raw_connection()
    try:
        raw.isolation_level = None  # autocommit：PRAGMA 才不会被事务吞掉
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("PRAGMA legacy_alter_table=ON")
        cursor.execute("BEGIN")
        try:
            cursor.execute("DROP TABLE IF EXISTS %s" % new_name)
            cursor.execute(create_sql)
            if shared:
                cols = ", ".join(shared)
                cursor.execute(
                    "INSERT INTO %s (%s) SELECT %s FROM %s"
                    % (new_name, cols, cols, table)
                )
            cursor.execute("DROP TABLE %s" % table)
            cursor.execute("ALTER TABLE %s RENAME TO %s" % (new_name, table))
            # 索引跟着模型重建：DROP TABLE 已经把旧索引带走了。
            # 模型生成的索引 DDL 写的是 `ON <原表名>`，而改名之后那个名字
            # 正好又是我们的表 —— 所以原样执行即可
            for index in model_table.indexes:
                cursor.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()

    log.info("迁移：重建表 %s（保留 %d 列数据）", table, len(shared))
    return True


def _drop_table(engine: Engine, table: str) -> bool:
    """删表。外键关掉再删 —— 留着子表引用一张已经不存在的表没有意义，
    而开着外键删会顺着 CASCADE 把子表数据一起带走。"""
    if table not in _tables(engine):
        return False
    raw = engine.raw_connection()
    try:
        raw.isolation_level = None
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("DROP TABLE IF EXISTS %s" % table)
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()
    log.info("迁移：删除表 %s", table)
    return True


# --------------------------------------------------------------------------- #
# 基线同步：只为"迁移器出现之前建的库"服务
# --------------------------------------------------------------------------- #

#: 曾经靠 create_all **之外**的手段加过、或者加的时候没留迁移记录的列。
#:
#: 特意用手写清单而不是拿 ``Base.metadata`` 全量比对：全量比对会把
#: "模型改了但忘了写迁移"这种真正的疏忽悄悄补上，反而掩盖了问题。
#: 这里列出来的都是**已知的、有意的**历史欠账。
_BASELINE_COLUMNS: Sequence[Tuple[str, str, str]] = (
    # 题目代码路径模式（上一轮加的）
    ("problem", "file_patterns", "TEXT"),
)


def _sync_baseline(engine: Engine) -> None:
    """把 ``user_version`` 还是 0 的库补齐到"迁移器上线那天"的基线。

    版本号是 0 可能有两种情况，两种都该补齐：
    * 迁移器之前建的老库 —— 缺的正是这份清单里的东西
    * 全新的空库 —— 一张表都没有，这里全是空操作

    补齐的判据是"这一列在不在"，不是版本号，所以重复调用是安全的。
    """
    for table, name, ddl in _BASELINE_COLUMNS:
        if table in _tables(engine):
            _add_column(engine, table, name, ddl)


# --------------------------------------------------------------------------- #
# 迁移步骤
# --------------------------------------------------------------------------- #


def _migration_001_enrollment(engine: Engine) -> None:
    """统一密钥注册 + 名单库。

    本版新增的表（``roster`` / ``roster_entry`` / ``bootstrap_key`` /
    ``machine_claim``）由 ``create_all`` 负责，这里只管已有表上的新列。

    全是 ``ADD COLUMN`` + 可空 —— 没有任何重建，理由见模块 docstring。
    """
    # 机器身份与认领时间
    if "agent" in _tables(engine):
        _add_column(engine, "agent", "machine_uuid", "VARCHAR(64)")
        _add_column(engine, "agent", "machine_fingerprint", "VARCHAR(128)")
        _add_column(engine, "agent", "claimed_at", "DATETIME")

    if "contest" in _tables(engine):
        # 场次引用的默认名单。可空 —— 没配就还是"逐场次手导选手"
        _add_column(engine, "contest", "default_roster_id", "INTEGER")
        _add_column(engine, "contest", "enrollment_mode", "VARCHAR(24)")

    # 老场次全部按"每选手注册码"理解 —— 那本来就是它们的行为。
    # 回填单独一步：万一失败，读取侧还有 ``effective_enrollment_mode`` 兜底，
    # 不会因为一个字段是 NULL 就让注册流程整个走不动。
    if "contest" in _tables(engine) and "enrollment_mode" in _columns(engine, "contest"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE contest SET enrollment_mode='per_player_code' "
                    "WHERE enrollment_mode IS NULL OR enrollment_mode=''"
                )
            )


def _migration_002_roster_binding(engine: Engine) -> None:
    """机器改为**永久绑定名单条目**，注册码整条链路去掉。

    这一版是**一次性**的：把结构整体推到新形态，不保留任何过渡期兼容。
    开发阶段的取舍 —— 迁移链每多一环就多一份要维护和测试的兼容代码，
    而这套结构还没上线过。

    做四件事：

    1. 重建 ``agent``：``player_id`` 换成 ``roster_entry_id`` + ``contest_id``，
       加配对码列与 UUID 唯一约束
    2. 尽力把老机器按 ``player_no`` 映射回名单条目 —— 迁移**能救则救**，
       实在对不上就让它们回到"未配对"状态（教师重新配一次，而不是机器失联）
    3. 删掉 ``machine_claim`` 与 ``enroll_code`` 两张表
    4. 重建 ``contest`` 去掉 ``enrollment_mode``
    """
    tables = _tables(engine)

    # ---- 1 & 2. agent ----
    if "agent" in tables:
        had_player_id = "player_id" in _columns(engine, "agent")
        # **先**把老的"机器 → 场次选手"读出来：重建之后 player_id 就没了，
        # 而这份映射是"能救则救"的全部依据
        legacy_bindings = _read_legacy_agent_bindings(engine) if had_player_id else []

        _rebuild_table(engine, "agent")

        if legacy_bindings:
            _bind_legacy_agents_to_roster(engine, legacy_bindings)

    # ---- 3. 两张不再需要的表 ----
    _drop_table(engine, "machine_claim")
    _drop_table(engine, "enroll_code")

    # ---- 4. contest 去掉注册方式 ----
    # 只剩一条注册路径，"两种模式二选一"这件事本身就不存在了
    if "contest" in _tables(engine) and "enrollment_mode" in _columns(engine, "contest"):
        _rebuild_table(engine, "contest")


def _read_legacy_agent_bindings(engine: Engine) -> List[Tuple[int, str]]:
    """读出 ``[(agent_id, player_no)]``。

    必须在重建 ``agent`` **之前**读 —— 重建之后 ``player_id`` 那一列就没了。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.id AS agent_id, p.player_no AS player_no "
                "FROM agent a JOIN player p ON p.id = a.player_id"
            )
        ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def _bind_legacy_agents_to_roster(engine: Engine, bindings: List[Tuple[int, str]]) -> None:
    """把老机器的归属从"某场次的人"翻译成"名单里的人"。

    映射依据是 ``player_no``：名单条目与场次选手用的是同一个编号，
    这是整个系统的约定。**只在唯一时映射** —— 同一个编号出现在两份名单里
    时有歧义，宁可让它回到未配对，也不能绑错人（绑错的代价是一个学生的代码
    落进另一个人的目录，而且完全静默）。
    """
    with engine.begin() as conn:
        ambiguous: List[str] = []
        unmatched: List[str] = []
        bound = 0
        for agent_id, player_no in bindings:
            candidates = conn.execute(
                text("SELECT id FROM roster_entry WHERE player_no = :no"),
                {"no": player_no},
            ).fetchall()
            if len(candidates) == 1:
                conn.execute(
                    text("UPDATE agent SET roster_entry_id = :rid WHERE id = :aid"),
                    {"rid": candidates[0][0], "aid": agent_id},
                )
                bound += 1
            elif len(candidates) > 1:
                ambiguous.append(player_no)
            else:
                unmatched.append(player_no)

    if bound:
        log.info("迁移：%d 台老机器按编号映射回名单条目", bound)
    for label, items in (("编号在多份名单里出现", ambiguous), ("编号不在任何名单里", unmatched)):
        if items:
            log.warning(
                "迁移：%d 台老机器无法自动映射（%s）：%s —— 它们会回到未配对状态，"
                "请在「机器配对」里重新配对",
                len(items),
                label,
                "、".join(sorted(set(items))[:20]),
            )


MIGRATIONS: List[Tuple[int, str, Callable[[Engine], None]]] = [
    (1, "统一密钥注册 + 名单库所需的结构", _migration_001_enrollment),
    (2, "机器永久绑定名单条目；去掉注册码链路", _migration_002_roster_binding),
]

LATEST_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


def current_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("PRAGMA user_version")).scalar() or 0)


def _set_version(engine: Engine, version: int) -> None:
    # PRAGMA 不支持参数绑定，只能拼字符串。version 来自我们自己的常量表，
    # 不是外部输入 —— 但这句话得写下来，免得以后有人照抄到这个模式去拼外部数据。
    with engine.begin() as conn:
        conn.execute(text("PRAGMA user_version=%d" % int(version)))


def migrate(engine: Engine, create_missing_tables: bool = True) -> List[str]:
    """把库升到最新结构，返回实际执行过的步骤说明。

    幂等：已经在最新版本上时是空操作（只建缺失的新表）。

    每一步失败都**不推进版本号**，于是下一次启动会重试同一步。绝不允许
    "跳过出错的步骤继续往前跑" —— 那会让库停在一个谁也没设计过的中间状态。

    **新表先建、迁移步骤后跑。** 这个顺序是有原因的：迁移步骤可能要引用
    新表（比如"把老机器按准考证号映射回名单条目"要查 ``roster_entry``），
    而 ``create_all`` 只建缺失的表、不碰已有的表，所以对老库是安全的。
    反过来的话，迁移步骤会撞上"表还不存在"。
    """
    if create_missing_tables:
        from .models import Base

        Base.metadata.create_all(engine)

    version = current_version(engine)
    if version == 0:
        _sync_baseline(engine)

    applied: List[str] = []
    for number, description, step in MIGRATIONS:
        if number <= version:
            continue
        log.info("应用迁移 %03d：%s", number, description)
        step(engine)
        _set_version(engine, number)
        version = number
        applied.append("%03d %s" % (number, description))

    return applied
