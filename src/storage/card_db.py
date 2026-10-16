# -*- coding: utf-8 -*-
"""卡片 SQLite 库（卡片存储迁移 SQLite · 第一步地基）。

每 work 一个库：``works/{work_id}/cards.db``。**库是卡片与影子的唯一真相**，
不再有卡片/影子 md（方案 §0 裁定 1/2）。

三条纪律：

1. **不设模块级路径常量**。库路径一律调用时从 ``card_store.WORKS_DIR`` 派生
   ——否则 ``tests/conftest.py`` 的沙箱 patch 失效，测试会写进真实 ``works/``。
2. **并发模型**：每 work 一条**长活连接** ＋ 一把可重入线程锁（见下方常量区的
   实测理由）；库操作走 ``asyncio.to_thread``（sqlite3 是同步库，直接调会阻塞
   事件循环）；``journal_mode=WAL``；写事务 ``BEGIN IMMEDIATE``／``COMMIT``。
3. **自检**：某 work 目录有 ``cards/*.md`` 但无 ``cards.db`` → 抛
   ``CardsNotMigratedError``，绝不静默建空库、绝不静默返空
   （防「未迁移数据被当成空树」）。

表结构见方案 §1.2：cards（正文各块各占独立列）／card_history（逐行）／
shadows（各节各占独立列）／meta（rev、编号序列）。

[卡片区域语义 2026-10-14 · v3] ``coordination``（影子·待协作事项）是加在迁移
跑完之后的一列：13 个既有库按旧 schema 建好，而 ``_SCHEMA`` 只对**新建库**生效。
故 ``_ensure_schema`` 另带一段幂等的「缺列即 ALTER TABLE ADD COLUMN」升级，见
``_COLUMN_UPGRADES``。``duty``（职责与边界）曾加过一列，用户 17:56 令**整体取消**，
走 ``_COLUMN_DROPS`` 幂等删列（见该常量处的说明）。

术语：本模块的 ``run_write`` / ``run_read`` 是**事务与线程的边界**；
``conn`` 上的一切 SQL 由调用方（card_store / shadow_store）书写。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("storage.carddb")

_DB_NAME = "cards.db"

# 连接忙等（秒）。写由进程内锁串行化，此处兜底跨进程（生产 5700 与测试脚本
# 可能同时摸同一个库）。
_BUSY_TIMEOUT_S = 5.0


class CardsNotMigratedError(Exception):
    """该 work 尚未迁移：盘上有 cards/*.md 但没有 cards.db。

    明确报错而不是静默建空库/返空——否则「未迁移」会被误读成「这个 work 没有卡」。
    """


# ---------------------------------------------------------------------------
# 列定义（与方案 §1.2 表结构一一对应）
# ---------------------------------------------------------------------------

# [依赖关联线 2026-10-16] 通信线让出 `relations` 这个名字（改叫 `links`），
# `relations` 归新增的第三种线「依赖关联线」——两列并存：
#   links     ＝ 通信线（原 relations 字段，read_linked / send_card_message 沿它寻址）
#   relations ＝ 依赖关联线（新线，模型侧 link_rel/unlink_rel 建，不参与通信）
_CARD_COLUMNS: tuple[str, ...] = (
    "card_id", "work_id", "title", "source", "card_type", "parent",
    "project_class", "owner_card_id", "state", "tags", "links", "relations",
    "session_ref", "toolcard_script", "toolcard_step", "toolcard_round",
    "toolcard_final", "thinkchain_final", "perms", "perms_version",
    "created_at", "updated_at", "mission", "status_block", "rulings",
)

# 以小 JSON 文本存的列（短列表 / 小字典，不拆表——方案 §1.3.4）
_JSON_COLUMNS: frozenset[str] = frozenset(
    {"tags", "links", "relations", "toolcard_final", "thinkchain_final", "perms"}
)

# 正文块（不进 meta）
_BODY_COLUMNS: tuple[str, ...] = ("mission", "status_block", "rulings")

# 列表型 JSON 列的空值（旧渲染恒写 []，读侧恒给 []）
_JSON_LIST_COLUMNS: frozenset[str] = frozenset({"tags", "links", "relations"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id          TEXT PRIMARY KEY,
    work_id          TEXT NOT NULL,
    title            TEXT,
    source           TEXT,
    card_type        TEXT,
    parent           TEXT,
    project_class    TEXT,
    owner_card_id    TEXT,
    state            TEXT,
    tags             TEXT,
    links            TEXT,
    relations        TEXT,
    session_ref      TEXT,
    toolcard_script  TEXT,
    toolcard_step    TEXT,
    toolcard_round   INTEGER,
    toolcard_final   TEXT,
    thinkchain_final TEXT,
    perms            TEXT,
    perms_version    INTEGER,
    created_at       TEXT,
    updated_at       TEXT,
    mission          TEXT,
    status_block     TEXT,
    rulings          TEXT,
    pos_x            REAL,
    pos_y            REAL,
    pinned           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cards_parent ON cards(parent);
CREATE INDEX IF NOT EXISTS idx_cards_type   ON cards(card_type);
CREATE INDEX IF NOT EXISTS idx_cards_state  ON cards(state);
CREATE INDEX IF NOT EXISTS idx_cards_work   ON cards(work_id);

CREATE TABLE IF NOT EXISTS card_history (
    card_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      TEXT,
    text    TEXT,
    PRIMARY KEY (card_id, seq)
);

CREATE TABLE IF NOT EXISTS shadows (
    owner_card_id TEXT PRIMARY KEY,
    work_id       TEXT NOT NULL,
    created_at    TEXT,
    updated_at    TEXT,
    progress      TEXT,
    coordination  TEXT,
    artifacts     TEXT,
    iterations    TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadows_work ON shadows(work_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ===========================================================================
# inbox（卡间通信 · 待处理收件箱 —— [卡片底座第一步 2026-10-14]）
#
# 通信的本质是「消息进入 B 模型的处理流程」，不是「存哪」。本表是消息的
# **待处理队列**（进模型视野的通道）：投递＝status=pending，处理完＝置
# consumed（**移出＝状态置位，不删行**，留痕可回溯）。
#
# 与 cards/shadows 一样**每 work 一个库**；表是纯新增——不接线时零影响。
# 单列 DDL（不用 executescript）：既有库的建表路径（_SCHEMA）只对新建库跑一次，
# 故这里另走一段**幂等**补建（CREATE TABLE IF NOT EXISTS），见 _ensure_tables。
# ===========================================================================

_INBOX_COLUMNS: tuple[str, ...] = (
    "msg_id", "work_id", "target_card_id", "from_card", "type",
    "content", "reply_to", "status", "seq", "created_at", "consumed_at",
)

_INBOX_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS inbox (
    msg_id          TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    target_card_id  TEXT,
    from_card       TEXT,
    type            TEXT,
    content         TEXT,
    reply_to        TEXT,
    status          TEXT,
    seq             INTEGER,
    created_at      TEXT,
    consumed_at     TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_inbox_target ON inbox(target_card_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_work   ON inbox(work_id)",
)

#: 待处理状态值（唯一的两态；不设第三态）
INBOX_PENDING = "pending"
INBOX_CONSUMED = "consumed"

# ===========================================================================
# 路径与连接
# ===========================================================================

# 每 work 一条**长活连接** ＋ 一把可重入线程锁。
#
# 为什么连接要缓存（实测）：WAL 模式下每次 connect 都要新建 `-shm` 共享内存
# 文件、close 时再 checkpoint 并删掉 `-wal`/`-shm`——本机实测 **约 56 ms/次**
# （非 WAL 只要 1 ms）。「每操作新开连接」会把每次卡片读写拖到几十毫秒
# （实测 run_write 84 ms/op），整仓测试从 2 min 涨到 12 min。故按任务书 §1a
# 的原设计：**每 work 一个连接**，跨操作复用。
#
# 为什么是 threading.RLock 而不是 asyncio.Lock：连接**不是线程安全**的，而
# 库操作分两路——异步写走 `asyncio.to_thread`（工作线程）、**必须保持同步签名**
# 的读（`_meta_of` / `iter_card_metas` / `card_exists`，见红线 1）在调用方线程。
# 两路共享同一条连接 ⇒ 用线程锁串行化；可重入是必需的：写事务内的 `_write_gate`
# 会回调 `_meta_of`（同一线程再进一次锁）。
# 键＝库文件绝对路径（含沙箱 tmp 前缀 ⇒ 用例之间天然隔离）。
_conns: dict[str, sqlite3.Connection] = {}
_locks: dict[str, threading.RLock] = {}


def db_path(work_id: str) -> Path:
    """库文件路径。**调用时**从 card_store.WORKS_DIR 派生（不缓存、不设常量）。"""
    from . import card_store  # 延迟 import：避免 storage 包内循环
    return card_store.WORKS_DIR / work_id / _DB_NAME


def _lock_for(key: str) -> threading.RLock:
    lock = _locks.get(key)
    if lock is None:
        lock = threading.RLock()
        _locks[key] = lock
    return lock


def reset_caches() -> None:
    """测试沙箱用：关掉全部连接、清空缓存（用例之间 work_id 相同但库在不同 tmp 目录）。"""
    for conn in _conns.values():
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conns.clear()
    _locks.clear()


def close_work(work_id: str) -> None:
    """关掉该 work 的缓存连接（下次访问自动重开）。

    用在**库文件被从外部替换**之后（如 `data_snapshot.restore_from_snapshot`
    恢复 cards.db）：长活连接仍握着旧的 `-wal`/`-shm`，不关掉的话恢复不生效。
    """
    conn = _conns.pop(str(db_path(work_id)), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _guard_migration(work_id: str, db: Path) -> None:
    """有 cards/*.md 但无 cards.db → 明确报错（见模块 docstring 纪律 3）。"""
    if db.exists():
        return
    cards_dir = db.parent / "cards"
    try:
        has_md = cards_dir.is_dir() and next(cards_dir.glob("card_*.md"), None) is not None
    except OSError:
        has_md = False
    if has_md:
        raise CardsNotMigratedError(
            f"该 work 尚未迁移到 SQLite（{cards_dir} 有 card_*.md 但无 {_DB_NAME}）: {work_id}"
        )


# [画布重构 2026-10-15] cards 表的位置三列。
#
# 卡片位置不再另开 ``layout.json`` 存放——位置是**卡片自身属性**（任务书约束 4），
# 与 title/parent 同级，就住在 cards 表里。「唯一真相来源」由文件变列（见
# ``canvas_store`` 的不变量区）。
#
# 为什么**既进 ``_SCHEMA`` 又进 ``_COLUMN_UPGRADES``**：``_SCHEMA`` 只对**新建库**
# 跑一次，13 个存量库得靠 ``_COLUMN_UPGRADES`` 的缺列即 ALTER 升级——两条路都留，
# 意味着「新库天生有列」「老库第一次被打开时补列」，任何时刻都不会读到缺列的表。
# 显式迁移脚本 ``scripts/migrate_cards_add_position_columns.py`` 引用的是**同一份**
# 本定义（唯一真相，避免脚本与运行时两处各写一遍 DDL 而悄悄漂移）。
#
# 取值纪律：
# - ``pos_x`` / ``pos_y``：REAL，**不带 DEFAULT**（未摆过＝NULL＝"没写过"，
#   自动排每次现算、不落盘——任务书约束 4）。只钉不摆的卡允许「有 pinned、坐标 NULL」。
# - ``pinned``：INTEGER 0/1，**带 DEFAULT 0**（存量卡迁移后 pinned=0，符合任务书
#   完成标准；开关列的 NULL 与 0 同义，取 DEFAULT 只是让读侧少一层 COALESCE）。
POSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pos_x", "pos_x REAL"),
    ("pos_y", "pos_y REAL"),
    ("pinned", "pinned INTEGER DEFAULT 0"),
)


# 幂等补列清单：(表名, 列名, ADD COLUMN 片段)
#
# [卡片区域语义 2026-10-14 · 裁定 1 甲] SQLite 第二步迁移已于当日跑完，13 个
# work 的库按**旧 schema** 建好；而上面那段 ``_SCHEMA`` 只对**今后新建的库**
# 生效（``CREATE TABLE IF NOT EXISTS`` 对已存在的表是空操作）。故既有库的加列
# 只能走这里：首次连接时 ``PRAGMA table_info`` 查缺列，缺则 ALTER TABLE 补上。
#
# 纪律：
# 1. **不带 DEFAULT** —— 红 2「旧卡零内容改动：新列为空即空，不填默认值」。
#    SQLite 的 ADD COLUMN 不带 DEFAULT 时，既有行该列一律为 NULL，读侧
#    ``row[...] or ""`` 归一成空串，与「新卡该块为空」同形。
#    ★ **例外：``POSITION_COLUMNS`` 里的 ``pinned``** —— 它是开关列不是内容列，
#    「没写过」与「没钉住」本就是同一件事，故按任务书带 ``DEFAULT 0``（见该常量）。
# 2. **不碰既有行** —— ADD COLUMN 只改表结构，不读写任何一行。
# 3. **幂等** —— 已有该列即跳过；反复跑不出错，可安全地每次建连接都调。
_COLUMN_UPGRADES: tuple[tuple[str, str, str], ...] = (
    ("shadows", "coordination", "coordination TEXT"),
) + tuple(("cards", col, ddl) for col, ddl in POSITION_COLUMNS) + (
    # [依赖关联线 2026-10-16] 老库经 `_COLUMN_RENAMES` 把旧 relations 改名成
    # links 之后，需补回**新线**的 relations 列；新库走 _SCHEMA 已含则跳过。
    ("cards", "relations", "relations TEXT"),
)


# 幂等改名清单：(表名, 旧列, 新列)
#
# [依赖关联线 2026-10-16] 本项目**第一段** RENAME 逻辑（此前只有 ADD/DROP）。
# 命名腾挪：通信线让出 `relations` 这个名字给新增的第三种线「依赖关联线」，
# 自己改叫 `links`。老库需 ``ALTER TABLE cards RENAME COLUMN relations TO links``；
# 新库走 _SCHEMA 已直接带 links，无需改。
#
# 纪律：
# 1. **幂等**：判据＝「有旧列且无新列」才改；已有新列即跳过，反复跑不出错。
# 2. **排在 ``_ensure_columns`` 之前**——先改名腾位置，再补新列（否则补列会把
#    旧 relations 当成「新列已存在」而漏补）。
# 3. 只改表结构：行数据随列搬迁，零读写（SQLite ``RENAME COLUMN`` 语义）。
_COLUMN_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("cards", "relations", "links"),
)


# 幂等删列清单：(表名, 列名)
#
# [卡片区域语义 2026-10-14 · v3] ``duty``（正卡·职责与边界）加过一列，用户 17:56
# 令**整体取消**："不需要负责什么不负责什么，毫无意义的描述，模型也根本不会遵守，
# 边界得靠权限来限制。" 故按 v2 任务书 §三.1 走 ``ALTER TABLE ... DROP COLUMN``，
# **不保留空列**（留空列＝留死东西）。
#
# 纪律：
# 1. **删前必须确认为空**——本机 SQLite 3.45.1 的 DROP COLUMN 会拒绝删掉被索引/
#    约束依赖的列，但对有数据的列是**直接销毁**。duty 加上去时值恒为 NULL，
#    故删列零损失；真要有人往里写过东西，这里必须先拒、不能默默删。
# 2. 幂等：列不存在即跳过，可安全地每次建连接都调。
_COLUMN_DROPS: tuple[tuple[str, str], ...] = (
    ("cards", "duty"),
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表建索引（幂等）＋ 改名（幂等）＋ 缺列补列（幂等）＋ 多余列删列（幂等）。

    建表：已建则只做一次极轻的存在性查询，不重跑 DDL。
    改名：见 ``_COLUMN_RENAMES``——**必须排在补列之前**（先腾出旧列名，再补新列）。
    补列：见 ``_COLUMN_UPGRADES``——迁移跑在加列之前的那些库靠这一步升级。
    删列：见 ``_COLUMN_DROPS``——已加过又被用户裁掉的列（如 duty）靠这一步退役。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards'"
    ).fetchone()
    if row is None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
    _rename_columns(conn)
    _ensure_columns(conn)
    _drop_columns(conn)
    _ensure_tables(conn)


def _rename_columns(conn: sqlite3.Connection) -> None:
    """旧列 → 新列改名（幂等）。**表／列不存在则跳过。**

    判据：表里有旧列且**没有**新列 → ``ALTER TABLE ... RENAME COLUMN``；否则跳过
    （已有新列＝已改过；旧列也无＝空表／新库）。SQLite 3.25+ 支持 RENAME COLUMN
    （本机 3.45.1）。DDL 自带提交（连接 ``isolation_level=None``），与
    ``_ensure_columns`` 同批、同序执行。
    """
    for table, old, new in _COLUMN_RENAMES:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # 表还没建出来 → _SCHEMA 会带上新列名
        names = {r["name"] for r in rows}
        if old in names and new not in names:
            logger.info("库结构改名：%s.%s → %s", table, old, new)
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """幂等补建**后续新增**的表（如 inbox）。

    ``_SCHEMA`` 里的一切 ``CREATE TABLE IF NOT EXISTS`` 只在**新建库**那条
    路径上被执行一次；迁移跑在加表之前的既有库不会经过它。故新增表必须在此
    单独补一遍——``CREATE TABLE IF NOT EXISTS`` 本身幂等，反复跑不出错，
    可安全地每次建连接都调。**不碰任何既有表、不读写任何既有行。**
    """
    for ddl in _INBOX_DDL:
        conn.execute(ddl)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """缺列即 ALTER TABLE ADD COLUMN（幂等）。**表不存在则跳过。**

    表不存在说明这是新库——那种库的 ``_SCHEMA`` 已含新列，查出来齐了直接跳过
    （硬判据 5：新库走 _SCHEMA → 检查发现列齐即跳过）。

    事务：``_ensure_schema`` 只在 ``_get_conn`` 建连接时调一次，早于调用方的
    ``BEGIN IMMEDIATE``；连接是 ``isolation_level=None``（autocommit），
    故 DDL 自带提交，不会与任何写事务打架。
    """
    for table, col, ddl in _COLUMN_UPGRADES:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # 表还没建出来 → _SCHEMA 会带上新列
        if any(r["name"] == col for r in rows):
            continue  # 已有 → 幂等跳过
        logger.info("库结构升级：补列 table=%s column=%s", table, col)
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _drop_columns(conn: sqlite3.Connection) -> None:
    """多余列即 ALTER TABLE DROP COLUMN（幂等）。**表／列不存在则跳过。**

    ★ **非空则拒删，绝不默默销毁数据**（任务书 §三.1 的红线：留空列＝留死东西，
    但有内容的列删掉＝毁留痕）。duty 加上去时值恒 NULL，故正常路径是零损失；
    一旦发现有人写过东西，这里记 error 并**保留该列**，把决定权交回给人。
    """
    for table, col in _COLUMN_DROPS:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows or not any(r["name"] == col for r in rows):
            continue  # 表没建出来／列已退役 → 幂等跳过
        nonempty = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL"
            f" AND TRIM(COALESCE({col}, '')) <> ''"
        ).fetchone()[0]
        if nonempty:
            logger.error(
                "库结构降级：拒绝删列 table=%s column=%s（非空行=%s，有内容不销毁）",
                table, col, nonempty,
            )
            continue
        logger.info("库结构降级：删列 table=%s column=%s（全空，零损失）", table, col)
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")


def _get_conn(work_id: str, *, create: bool, force: bool = False) -> Optional[sqlite3.Connection]:
    """取（或建）该 work 的长活连接；无库且 create=False → None。**调用方须持锁。**

    ``force=True`` **只给迁移脚本**用：正常读写路径经 ``_guard_migration`` 拦
    「有 md 无库」，而迁移正是要把有 md 的 work 建成库——放行必须显式传参。
    """
    key = str(db_path(work_id))
    conn = _conns.get(key)
    if conn is not None:
        return conn
    db = db_path(work_id)
    if not db.exists():
        if not force:
            _guard_migration(work_id, db)
        if not create:
            return None
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db), timeout=_BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
    _ensure_schema(conn)
    _conns[key] = conn
    return conn


async def create_db(work_id: str, *, allow_unmigrated: bool = False) -> None:
    """建库建表（**迁移脚本专用**；正常读写路径不需要它）。

    默认仍受 ``_guard_migration`` 拦（有 md 无库即报错）；迁移脚本显式传
    ``allow_unmigrated=True`` 才放行——放行是**显式的一次性动作**，不是默认态。
    """
    await asyncio.to_thread(_create_db_sync, work_id, allow_unmigrated)


def _create_db_sync(work_id: str, allow_unmigrated: bool) -> None:
    with _lock_for(str(db_path(work_id))):
        _get_conn(work_id, create=True, force=allow_unmigrated)


def clear_all(conn: sqlite3.Connection) -> None:
    """清空该 work 库内**全部数据行**（不动 schema）——迁移脚本幂等重跑用。"""
    for table in ("card_history", "cards", "shadows", "meta", "inbox"):
        conn.execute(f"DELETE FROM {table}")


# ===========================================================================
# 线程 / 事务边界
# ===========================================================================

async def run_write(work_id: str, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    """写事务：work 连接锁内 → 线程内 → BEGIN IMMEDIATE / COMMIT。

    fn 在事务内执行；抛异常则 ROLLBACK 并向上抛。fn 的返回值原样返回。
    """
    return await asyncio.to_thread(_write_sync, work_id, fn)


def _write_sync(work_id: str, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    with _lock_for(str(db_path(work_id))):
        conn = _get_conn(work_id, create=True)
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result


async def run_read(work_id: str, fn: Callable[[sqlite3.Connection], Any],
                   default: Any = None) -> Any:
    """只读：线程内执行 fn(conn)；库不存在返回 default。"""
    return await asyncio.to_thread(_read_sync, work_id, fn, default)


def _read_sync(work_id: str, fn: Callable[[sqlite3.Connection], Any], default: Any) -> Any:
    with _lock_for(str(db_path(work_id))):
        conn = _get_conn(work_id, create=False)
        if conn is None:
            return default
        return fn(conn)


def read_sync(work_id: str, fn: Callable[[sqlite3.Connection], Any],
              default: Any = None) -> Any:
    """**同步**只读（供必须保持同步签名的公开接口，如 iter_card_metas）。

    单条索引 SELECT 级别的短操作，与改造前的同步文件读同量级。
    """
    return _read_sync(work_id, fn, default)


# ===========================================================================
# 行 ↔ 字典
# ===========================================================================

def row_to_meta(row: sqlite3.Row) -> dict[str, Any]:
    """cards 行 → meta 字典（契约与旧 front matter 读侧一致）。

    旧读侧（``parse_front_matter``）的键集＝「front matter 里写了的键」；
    这里等价为「列非 NULL 才出键」——NULL 即「没写过」。tags/links/relations 例外：
    旧渲染恒写 ``[]``，故 NULL 也给 ``[]``。
    """
    meta: dict[str, Any] = {}
    for col in _CARD_COLUMNS:
        if col in _BODY_COLUMNS or col == "work_id":
            continue
        val = row[col]
        if col in _JSON_COLUMNS:
            if val is None:
                if col in _JSON_LIST_COLUMNS:
                    meta[col] = []
                continue
            try:
                meta[col] = json.loads(val)
            except (TypeError, ValueError):
                meta[col] = [] if col in _JSON_LIST_COLUMNS else None
            continue
        if val is None:
            continue
        meta[col] = val
    return meta


def _encode(value: Any) -> Any:
    """meta 值 → SQL 绑定值（list/dict → JSON 文本）。"""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def insert_card(conn: sqlite3.Connection, meta: dict[str, Any],
                body: dict[str, str]) -> None:
    """INSERT 一行卡片（正文各块取自 body）。"""
    values: dict[str, Any] = {}
    for col in _CARD_COLUMNS:
        if col in _BODY_COLUMNS:
            values[col] = body.get(col, "")
        elif col in _JSON_COLUMNS:
            values[col] = _encode(meta.get(col)) if meta.get(col) is not None else None
        else:
            values[col] = meta.get(col)
    cols = ", ".join(_CARD_COLUMNS)
    marks = ", ".join("?" for _ in _CARD_COLUMNS)
    conn.execute(
        f"INSERT INTO cards ({cols}) VALUES ({marks})",
        tuple(values[c] for c in _CARD_COLUMNS),
    )


def update_card_row(conn: sqlite3.Connection, card_id: str,
                    meta_patch: dict[str, Any], body_patch: dict[str, str]) -> None:
    """UPDATE 指定卡的若干列（meta 字段 + 正文块）。"""
    sets: list[str] = []
    params: list[Any] = []
    for key, val in meta_patch.items():
        if key in _BODY_COLUMNS:
            sets.append(f"{key} = ?")
            params.append(val)
        elif key in _CARD_COLUMNS:
            sets.append(f"{key} = ?")
            params.append(_encode(val) if val is not None else None)
    for key, val in body_patch.items():
        if key in _BODY_COLUMNS:
            sets.append(f"{key} = ?")
            params.append(val)
    if not sets:
        return
    params.append(card_id)
    conn.execute(f"UPDATE cards SET {', '.join(sets)} WHERE card_id = ?", tuple(params))


def delete_card_row(conn: sqlite3.Connection, card_id: str) -> int:
    """DELETE 卡片行 + 其修改历史行；返回删掉的卡片行数。"""
    n = conn.execute("DELETE FROM cards WHERE card_id = ?", (card_id,)).rowcount
    conn.execute("DELETE FROM card_history WHERE card_id = ?", (card_id,))
    return n


def get_card_row(conn: sqlite3.Connection, card_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()


def get_history(conn: sqlite3.Connection, card_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM card_history WHERE card_id = ? ORDER BY seq", (card_id,)
    ).fetchall()
    return [r["text"] for r in rows]


def append_history(conn: sqlite3.Connection, card_id: str, line: str,
                   ts: str = "") -> None:
    """追加一行修改历史（seq 卡内自增）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM card_history WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    nxt = int(row["m"]) + 1
    conn.execute(
        "INSERT INTO card_history (card_id, seq, ts, text) VALUES (?, ?, ?, ?)",
        (card_id, nxt, ts or _line_ts(line), line),
    )


def _line_ts(line: str) -> Optional[str]:
    """从历史行里抽 ``[<ts>]``（好查用；无消费方依赖，抽不到给 NULL）。"""
    start = line.find("[")
    end = line.find("]", start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        return line[start + 1:end]
    return None


# ---------------------------------------------------------------------------
# shadows
# ---------------------------------------------------------------------------

_SHADOW_COLUMNS: tuple[str, ...] = (
    "owner_card_id", "work_id", "created_at", "updated_at",
    "progress", "coordination", "artifacts", "iterations",
)

# 影子各节的**内容列**（不含四个身份/时间列）。insert/update 按它拼列名，
# 新增节只需在此登记一次，不必再改 SQL 字面量。
_SHADOW_SECTION_COLUMNS: tuple[str, ...] = (
    "progress", "coordination", "artifacts", "iterations",
)

# 影子节标题 → 列名（与 shadow_store 的节标题一一对应）
# [卡片区域语义 2026-10-14] 新增「## 待协作事项」→ coordination（方案 §4.2）。
# 顺序＝渲染顺序，与 shadow_store._SECTION_ORDER 同源。
SHADOW_SECTIONS: dict[str, str] = {
    "## 进度": "progress",
    "## 待协作事项": "coordination",
    "## 产物清单": "artifacts",
    "## 迭代历史": "iterations",
}


def get_shadow_row(conn: sqlite3.Connection, owner_card_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM shadows WHERE owner_card_id = ?", (owner_card_id,)
    ).fetchone()


def iter_shadow_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM shadows ORDER BY owner_card_id").fetchall()


def insert_shadow(conn: sqlite3.Connection, owner_card_id: str, work_id: str,
                  created_at: str, updated_at: str,
                  sections: dict[str, str]) -> None:
    # 列名由 _SHADOW_SECTION_COLUMNS 派生：新增节登记一次即可，不再改 SQL 字面量。
    cols = ("owner_card_id", "work_id", "created_at", "updated_at") + _SHADOW_SECTION_COLUMNS
    vals: list[Any] = [owner_card_id, work_id, created_at, updated_at]
    vals += [sections.get(c, "") for c in _SHADOW_SECTION_COLUMNS]
    conn.execute(
        f"INSERT INTO shadows ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(vals),
    )


def update_shadow_sections(conn: sqlite3.Connection, owner_card_id: str,
                           sections: dict[str, str], updated_at: str) -> None:
    """UPDATE 影子指定节。**只写 sections 里出现的列**（append_to_section 传单列）。"""
    sets = ["updated_at = ?"]
    params: list[Any] = [updated_at]
    for col in _SHADOW_SECTION_COLUMNS:
        if col in sections:
            sets.append(f"{col} = ?")
            params.append(sections[col])
    params.append(owner_card_id)
    conn.execute(f"UPDATE shadows SET {', '.join(sets)} WHERE owner_card_id = ?",
                 tuple(params))


def delete_shadow_row(conn: sqlite3.Connection, owner_card_id: str) -> int:
    return conn.execute(
        "DELETE FROM shadows WHERE owner_card_id = ?", (owner_card_id,)
    ).rowcount


# ---------------------------------------------------------------------------
# meta（rev / 编号序列）
# ---------------------------------------------------------------------------

def get_meta_value(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    return row["value"]


def set_meta_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)),
    )


def next_seq(conn: sqlite3.Connection) -> int:
    """发下一个卡号（不落盘；由调用方与建卡同事务写回）。"""
    raw = get_meta_value(conn, "seq", "0")
    try:
        return int(raw) + 1
    except (TypeError, ValueError):
        return 1


def bump_rev(conn: sqlite3.Connection) -> int:
    """单调递增的库版本号（每次写 +1）。/nav/version 指纹读它——不依赖
    WAL 下可能不更新的主库文件 mtime（方案 §7.1.1）。"""
    raw = get_meta_value(conn, "rev", "0")
    try:
        rev = int(raw) + 1
    except (TypeError, ValueError):
        rev = 1
    set_meta_value(conn, "rev", str(rev))
    return rev


# ---------------------------------------------------------------------------
# inbox（卡间通信 · 待处理收件箱）
#
# 三个原语：投递（append_inbox）／列待处理（list_pending）／移出（mark_consumed）。
# 全部**纯新增**——不接线时零影响；行只增不删（consumed 是状态位，不是删除）。
#
# [卡片端点改读收件箱 2026-10-15] 补第四个只读原语 `list_inbox`（列**全量**含
# consumed，可按 work 或按收件卡）：通知端点的历史面靠它——消费掉的消息不该
# 从面板消失，但也不该再计入未读（status 自行区分，见 routes.list_card_messages）。
# ---------------------------------------------------------------------------

def _inbox_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {col: row[col] for col in _INBOX_COLUMNS}


def _now_iso() -> str:
    """本地时区 ISO 时间戳（与 session_store._now_iso 同格式）。"""
    from datetime import datetime
    return datetime.now().astimezone().isoformat()


def insert_inbox(conn: sqlite3.Connection, *, msg_id: str, work_id: str,
                 target_card_id: str, from_card: str, type: str,
                 content: str, reply_to: str = "", created_at: str = "") -> int:
    """投递一条消息（status=pending）。返回其 seq（work 内投递序，1 起）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM inbox WHERE work_id = ?",
        (work_id,),
    ).fetchone()
    nxt = int(row["m"]) + 1
    conn.execute(
        "INSERT INTO inbox (msg_id, work_id, target_card_id, from_card, type,"
        " content, reply_to, status, seq, created_at, consumed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (msg_id, work_id, target_card_id, from_card, type, content, reply_to,
         INBOX_PENDING, nxt, created_at),
    )
    return nxt


def select_pending(conn: sqlite3.Connection, work_id: str,
                   target_card_id: str) -> list[dict[str, Any]]:
    """列某收件卡的**待处理**消息（按投递序升序）。"""
    rows = conn.execute(
        "SELECT * FROM inbox WHERE work_id = ? AND target_card_id = ?"
        " AND status = ? ORDER BY seq",
        (work_id, target_card_id, INBOX_PENDING),
    ).fetchall()
    return [_inbox_row_to_dict(r) for r in rows]


def select_inbox(conn: sqlite3.Connection, work_id: str,
                 target_card_id: Optional[str] = None) -> list[dict[str, Any]]:
    """列该 work 的**全部**消息行（**含 consumed**），按投递序升序。

    与 `select_pending` 的分工：后者只列待处理（进模型视野的那一批），前者
    列历史全量（通知面板「看历史」的数据面）。`target_card_id=None` 时不限收件卡
    ——通知端点按 work 聚合未读角标就靠这一次查询（省得逐卡 N 次）。

    [卡片端点改读收件箱 2026-10-15] 纯新增原语；既有三原语一字未动。
    """
    if target_card_id:
        rows = conn.execute(
            "SELECT * FROM inbox WHERE work_id = ? AND target_card_id = ? ORDER BY seq",
            (work_id, target_card_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM inbox WHERE work_id = ? ORDER BY seq", (work_id,)
        ).fetchall()
    return [_inbox_row_to_dict(r) for r in rows]


def set_consumed(conn: sqlite3.Connection, msg_id: str,
                 consumed_at: str = "") -> int:
    """把一条消息移出待处理（status→consumed，**不删行**）。返回受影响行数。"""
    cur = conn.execute(
        "UPDATE inbox SET status = ?, consumed_at = ?"
        " WHERE msg_id = ? AND status = ?",
        (INBOX_CONSUMED, consumed_at, msg_id, INBOX_PENDING),
    )
    return cur.rowcount


async def append_inbox(work_id: str, *, msg_id: str, target_card_id: str,
                       from_card: str, type: str, content: str,
                       reply_to: str = "", created_at: str = "") -> int:
    """投递一条消息进待处理收件箱（写事务）。"""
    return await run_write(work_id, lambda conn: insert_inbox(
        conn, msg_id=msg_id, work_id=work_id, target_card_id=target_card_id,
        from_card=from_card, type=type, content=content, reply_to=reply_to,
        created_at=created_at or _now_iso(),
    ))


async def list_pending(work_id: str, target_card_id: str) -> list[dict[str, Any]]:
    """列某收件卡的待处理消息（库不存在／无记录 → 空列表）。"""
    out = await run_read(
        work_id, lambda conn: select_pending(conn, work_id, target_card_id), [],
    )
    return out or []


async def list_inbox(work_id: str, target_card_id: Optional[str] = None) -> list[dict[str, Any]]:
    """列该 work 的全部消息行（**含 consumed**；库不存在／无记录 → 空列表）。

    [卡片端点改读收件箱 2026-10-15] 通知端点的数据源原语：看历史用全量
    （consumed 不删行，故历史天然在），算未读按 `status == INBOX_PENDING` 过滤。
    """
    out = await run_read(
        work_id, lambda conn: select_inbox(conn, work_id, target_card_id), [],
    )
    return out or []


async def mark_consumed(work_id: str, msg_id: str, consumed_at: str = "") -> int:
    """把一条消息移出待处理（写事务）。返回受影响行数（0＝已不在待处理）。"""
    return await run_write(
        work_id, lambda conn: set_consumed(conn, msg_id, consumed_at or _now_iso()),
    )


def read_rev(work_id: str) -> Optional[int]:
    """**同步**读 rev（供 routes._work_fingerprint）；无库返回 None。"""
    def _fn(conn: sqlite3.Connection) -> Optional[int]:
        raw = get_meta_value(conn, "rev", None)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return _read_sync(work_id, _fn, None)


async def read_rev_async(work_id: str) -> Optional[int]:
    return await asyncio.to_thread(read_rev, work_id)
