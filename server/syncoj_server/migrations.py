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
2. **重建表（rebuild）只能按 :func:`_rebuild_table` 的配方来。** 删一列、改一次
   可空性，在 SQLite 里只能"建新表→搬数据→换名"。本方案一开始写的是"绝不重建"
   （怕关不掉外键，让 ``DROP TABLE`` 顺着 ``ON DELETE CASCADE`` 把子表删光），
   但那等于永久放弃改结构，代价太大 —— 所以改成把配方定死、并加看守测试
   （见 ``docs/design/04-server.md`` §4.8 与 ``tests/test_migrations.py``）。**三条必须同时成立**：
   裸连接 + autocommit 下关 ``foreign_keys``（``PRAGMA`` 在事务里是空操作）、
   打开 ``legacy_alter_table``（否则 ``RENAME`` 会去改写引用方）、DDL 用文本改写
   生成而不是 ``to_metadata()``。少任何一条都是静默删数据的级别。
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


def _migration_003_player_notice(engine: Engine) -> None:
    """给场次加"给选手看的注意事项"（``contest.player_notice``）。

    刻意做成最朴素的那种一步：**一个可空列的 ADD COLUMN**，不重建表、不回填、
    不引入新表。

    为什么不塞进 ``_BASELINE_COLUMNS``：基线只服务 ``user_version == 0`` 的库
    （迁移器出现之前建的那些）。开发期已经停在版本 2 上的库不会被基线碰到 ——
    而"老库少一列"恰恰只会在它身上发生，所以这里必须是一条真会跑的迁移。
    漏掉的表现是：选手页在那个库上直接 500（``no such column``），
    而新库一切正常 —— 那种"只有升级上来的库才坏"的故障最难查。

    新库那边由 ``create_all`` 直接建出这一列，跑这里时是空操作；版本号照样推进，
    否则两条路径的 ``user_version`` 会分叉。
    """
    if "contest" in _tables(engine):
        _add_column(engine, "contest", "player_notice", "TEXT")


def _migration_004_agent_last_seen_ip(engine: Engine) -> None:
    """给机器加"最近一次请求的来源 IP"（``agent.last_seen_ip``）。

    选手页的"自动匹配本机"要靠它：选手什么都不填，服务端按来源 IP 找到那台机器。
    所以这一列必须在**每一次**带凭据的请求上更新，而不只是心跳时 —— 选手页
    自己不会触发心跳（它拿着一个还没有身份的请求进来）。

    同样是一个可空列的 ``ADD COLUMN``：老机器还没有这一列时是 NULL，
    匹配不上就 404 并请选手手填场次与考号，而不是猜。
    """
    if "agent" in _tables(engine):
        _add_column(engine, "agent", "last_seen_ip", "VARCHAR(64)")


def _migration_005_contest_window(engine: Engine) -> None:
    """场次的可配置考试时间窗：补上 ``starts_at`` / ``ends_at``，撤掉 ``player_notice``。

    这一版一次做成两件事，因为它们动的是**同一张表、同一个重建动作**：

    1. 补上 ``starts_at`` / ``ends_at``。这两列从服务端第一版起就在模型里，
       但从来没有接口读写、也没留过迁移记录 —— 开发期的库结构靠
       ``db reset --yes`` 兜底、不保证兼容，所以凡是缺列的库都在这里补上。
    2. 删掉 ``player_notice``（迁移 003 加的）。考场公告改成"下发一份 NOTICE.md
       文件"之后，这个字段没有任何读者了；留着它，老库会带着一列永远没人读的数据
       跑下去，而新库没有那一列 —— 两条升级路径从此结构不同。

    **为什么不去改 003 让它别加这一列**：003 早就跑过了。现在每一个真的开发库
    （``runtime/server/syncoj.db`` 停在版本 4）里那一列都在，改历史只会让
    "已经加上的列"永远没人删。所以是"003 加、005 减"，链条上看着多余，
    但这正是迁移只能往前走的那一面。

    重建只在**真的需要**时做：重建是这套迁移里唯一"整表搬动"的动作，配方见
    :func:`_rebuild_table`（关外键 + autocommit，少一条就会顺着 ``ON DELETE
    CASCADE`` 把 ``player`` / ``problem`` / ``agent_status`` 清空）。
    表已经和模型一致时不重建 —— 无谓地跑一遍就是白冒一次丢数据的风险。
    """
    if "contest" not in _tables(engine):
        return

    columns = _columns(engine, "contest")
    if "player_notice" not in columns and "starts_at" in columns and "ends_at" in columns:
        # 已经和模型一致：什么都不用做（新库、以及已经升上来的库都走这一支）
        return

    _rebuild_table(engine, "contest")


def _migration_006_release_bootstrap_key(engine: Engine) -> None:
    """给发布记录加"这个包附带的是哪把统一注册密钥"（``agent_release.bootstrap_key_id``）。

    需求是"发版时可以选择附带一把统一注册密钥"。服务端只存密钥的哈希、拿不到明文，
    所以带不动"已经签发过的那一把" —— 只能在构建那一刻现场签一把新的，然后把
    **它的 id** 记在这一列上，界面据此显示标签、并给一个就地吊销的入口。

    **为什么是重建而不是 ADD COLUMN**：这一列要带 ``ON DELETE SET NULL``，
    而 SQLite 的 ``ALTER TABLE ADD COLUMN`` 不接受带动作的外键子句
    （会报 "Cannot add a REFERENCES column with non-NULL default value" 这一族错误）。
    ``agent_release`` 没有子表、也没有外部索引，重建的代价只是一行数据搬移；
    配方照 :func:`_rebuild_table`（关外键 + autocommit），见模块 docstring 的三条规矩。

    ``SET NULL`` 而不是 ``CASCADE``：密钥被删之后**版本记录必须还在**。
    "这个包是哪天发出去的、当时带的是哪把钥匙"是排查现场的第一手材料，
    不能因为随手清掉一行密钥记录就一起消失。

    新库由 ``create_all`` 直接建出这一列，跑这里时是空操作 —— 但版本号照样推进，
    否则两条路径的 ``user_version`` 会分叉。
    """
    if "agent_release" not in _tables(engine):
        return
    if "bootstrap_key_id" in _columns(engine, "agent_release"):
        # 已经和模型一致（新库、以及已经升上来的库都走这一支）
        return
    _rebuild_table(engine, "agent_release")


def _migration_007_release_public_key(engine: Engine) -> None:
    """机器记录**最近一次报告有发布公钥**的时刻（``agent.release_public_key_at``）。

    用途是远程卸载授权的门禁：卸载要 root，而机器只能用升级信任锚验那枚令牌，
    所以在没报告过公钥的机器上签授权是白费 —— 它一定拒收，教师那边却显示成功。

    全是 ``ADD COLUMN`` + 可空：老库里的机器**没有**这一列的历史值，而"没报过"
    与"没有"在判据上应当得到同一个结论，所以空白就是正确初始状态，不需要回填。
    """
    if "agent" not in _tables(engine):
        return
    _add_column(engine, "agent", "release_public_key_at", "DATETIME")


def _migration_008_release_install_policy(engine: Engine) -> None:
    """给发布记录加随包带下去的三条安装策略（``agent_release`` 的三个新列）。

    用途见 ``services/install_policy.py``：装机与升级时机器按它们行动，而台账与
    升级清单要在**不读包**的情况下把它们报出来。

    三列全是 ``ADD COLUMN`` + 可空，**不回填**：老记录的实际行为就是"不改配置、
    密钥不动、按默认模式升级"，而读的那一侧（``build_install_policy``）会按默认值
    补 —— 回填一份默认值只是把同一件事写两遍，将来默认值变了还得解释"库里那批
    旧值算不算历史事实"。
    """
    if "agent_release" not in _tables(engine):
        return
    _add_column(engine, "agent_release", "bootstrap_key_policy", "VARCHAR(16)")
    _add_column(engine, "agent_release", "config_policy_json", "TEXT")
    _add_column(engine, "agent_release", "upgrade_mode", "VARCHAR(16)")


def _migration_009_agent_scan_missing(engine: Engine) -> None:
    """机器记录"本机上不存在的扫描根"（``agent.scan_missing_json``）。

    Agent 每轮心跳都会报当前不存在的扫描根（最多 5 条绝对路径）。服务端拿它做两件
    事：在机器列表上写一句"选手目录还没建"，以及**在状态变化时**记一条审计 ——
    缺失出现是 warning、恢复是 info。后者要求"这一次和上一次比有没有变"，
    所以必须落库，不能只放内存。

    只有一个 ``ADD COLUMN`` + 可空：老库里的机器**没有**这一列的历史值，而"没报过"
    与"没有已知缺失"在判据上是同一个结论，空白就是正确初始状态。
    """
    if "agent" not in _tables(engine):
        return
    _add_column(engine, "agent", "scan_missing_json", "TEXT")


def _migration_010_runtime_settings(engine: Engine) -> None:
    """运行参数表（``runtime_setting``）：心跳节奏与离线判定。

    这张表由 ``create_all`` 负责建（新库直接就有），迁移这一步只推进版本号 ——
    但**必须存在**：不写的话，老库升上来时 ``user_version`` 会停在上一步，而
    "新库与老库结构一致"那条守卫测试会红。理由与其他"新表交给 create_all"的
    步骤一致（见模块 docstring 第 3 条）。
    """
    return


MIGRATIONS: List[Tuple[int, str, Callable[[Engine], None]]] = [
    (1, "统一密钥注册 + 名单库所需的结构", _migration_001_enrollment),
    (2, "机器永久绑定名单条目；去掉注册码链路", _migration_002_roster_binding),
    (3, "场次增加给选手看的注意事项", _migration_003_player_notice),
    (4, "机器记录最近一次请求的来源 IP", _migration_004_agent_last_seen_ip),
    (
        5,
        "场次增加开考/结束时间；去掉场次上的选手注意事项",
        _migration_005_contest_window,
    ),
    (
        6,
        "发布记录记住随包附带的那把统一注册密钥",
        _migration_006_release_bootstrap_key,
    ),
    (
        7,
        "机器记录最近一次报告有发布公钥的时刻",
        _migration_007_release_public_key,
    ),
    (
        8,
        "发布记录记住随包带走的三条安装策略",
        _migration_008_release_install_policy,
    ),
    (
        9,
        "机器记录本轮不存在的扫描根",
        _migration_009_agent_scan_missing,
    ),
    (
        10,
        "运行参数表（心跳节奏与离线判定）",
        _migration_010_runtime_settings,
    ),
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
