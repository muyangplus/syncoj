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


MIGRATIONS: List[Tuple[int, str, Callable[[Engine], None]]] = [
    (1, "统一密钥注册 + 名单库所需的结构", _migration_001_enrollment),
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
    """
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

    if create_missing_tables:
        from .models import Base

        Base.metadata.create_all(engine)

    return applied
