# -*- coding: utf-8 -*-
"""卡片 SQLite 库（卡片存储迁移 SQLite · 第一步地基）。

每 work 一个库：``works_data/{work_id}/cards.db``（2026-11-07 起，原 ``works/``
下由 ``_ensure_db_location`` 惰性搬迁——库搬出卡的 exec 施工目录）。
**库是卡片与影子的唯一真相**，
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
    "toolcard_final", "thinkchain_final",
    # [能力底座 2026-10-30] 模式 code（manual / auto / 未来第三种）＋ 这张卡挂了
    # 哪些能力（能力清单的 code 数组）。工具面从此按 mode→能力上限 ∩ abilities 算。
    "mode", "abilities",
    "created_at", "updated_at", "mission", "status_block", "requirements",
    # [需求文档生命周期 2026-10-28 · 用户令] 需求文档分草稿/正稿两版：
    #   requirements        ＝**正稿**（所有人可读，作为验收判据）
    #   requirements_draft  ＝**草稿**（本卡私有，不进 read_card、不进别人的上下文）
    #   requirements_state  ＝ ""（没写过）/ draft / in_review / approved
    "requirements_draft", "requirements_state",
    # [harness 提示词注入 2026-10-28] 待注入队列（JSON 数组文本）
    "pending_inject",
    # [自动模式 2026-10-17 · 回灌 5700 块0.1] 四列进 meta 管线（insert/update/
    # row_to_meta 都走本清单）——不进则写了不落、读了不带，驱动器/工具/权限全挂。
    "auto_mode", "goal", "goal_state", "goal_rounds", "goal_await_children",
    "goal_await_wakes",
    # [goal 域 2026-10-20 · 照抄 dsh-goal] 目标的事件溯源投影列。权威是
    # ``events/<card>.jsonl`` 里的 goal/change 事件；这几列只是供画布/旧读方
    # 直接查的缓存（原版 sessionProjections 的 goal 单元同一定位）。
    "goal_id", "goal_revision", "goal_max_rounds", "goal_created_at",
    "goal_updated_at", "goal_blocked_code", "goal_blocked_message",
    # [委办线 2026-11-14] 建卡方 card_id（委办上级：可删/收汇报/可 direct）。
    # 进 meta 管线 ⇒ row_to_meta 自动带上，工具层 cards[cid]["created_by"] 直接读。
    "created_by",
)

# 以小 JSON 文本存的列（短列表 / 小字典，不拆表——方案 §1.3.4）
_JSON_COLUMNS: frozenset[str] = frozenset(
    {"tags", "links", "relations", "toolcard_final", "thinkchain_final",
     # [能力底座 2026-10-30] 挂载的能力 code 数组
     "abilities"}
)

# 正文块（不进 meta）
_BODY_COLUMNS: tuple[str, ...] = ("mission", "status_block", "requirements")

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
    mode             TEXT,
    abilities        TEXT,
    created_at       TEXT,
    updated_at       TEXT,
    mission          TEXT,
    status_block     TEXT,
    requirements     TEXT,
    requirements_draft TEXT,
    requirements_state TEXT,
    pos_x            REAL,
    pos_y            REAL,
    pinned           INTEGER DEFAULT 0,
    auto_mode        INTEGER DEFAULT 0,
    goal             TEXT,
    goal_state       TEXT,
    goal_rounds      INTEGER DEFAULT 0,
    -- [harness 提示词注入 2026-10-28] 待注入队列（JSON 数组文本）：装配时读出拼入、用完清空
    pending_inject   TEXT,
    -- [6702 移植 2026-10-17 · 等子卡恢复] 纯**意图**标志位（0/1）：
    --   语义＝「本卡**自己**声明 pause 是在等子卡」，不是「不做了」。
    --   存在理由：``goal_state=paused`` 本身**不可区分**「模型等子卡」与
    --   「用户按停」；没有这一位，机械唤醒就无法判断该不该复活。
    goal_await_children INTEGER DEFAULT 0,
    -- [6702 移植 2026-10-17 · 等子卡活锁兜底] 恢复轮**唤醒事实**计数器：
    --   每被唤醒（子卡完工触发 wake）一次 +1；用于「空 wake 活锁」机械兜底——
    --   第 _AWAIT_RESUME_DEADLOCK_GIVEUP 次仍无子卡进度则放弃等子卡自动恢复。
    goal_await_wakes INTEGER DEFAULT 0,
    -- [伴生评审卡 2026-10-27] 评审卡指向它服务的子卡；非评审卡为空。
    -- 评审卡不设 parent（离树），靠这一列与子卡配对，一对一。
    review_of        TEXT,
    -- [伴生评审卡 2026-10-27] 当前批次状态（JSON：batch/draft/criteria/focus/opened_at）。
    -- 只是"当前态"，历史批次在事件流与 review_approvals 里。
    review_state     TEXT,
    -- [委办线 2026-11-14] 这张卡是谁建的（卡间建卡＝建卡方 card_id；
    -- UI/系统建的为空）。权属带三权：创建者可删它（离树卡的唯一删域）、
    -- 它的 report_up 抄送创建者、创建者对它天然有通信线（可 direct）。
    created_by       TEXT
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
    iterations    TEXT,
    note          TEXT
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

# ===========================================================================
# 原文表（session_blocks —— [原文入库 2026-10-20] 照抄 dsh origin-contract/selfread）
#
# dsh 的原文是「事件流」（`seq / type / time / data / surfaceOp / sourceEventSeqs`），
# 我们的原文是「会话块」。⇒ 一条块 = 一个事件，`data` 存整块原样（零加工透传）。
#
# 为什么要这张表：记忆条目靠 `seq` 指向原文，而原文原先只在 md 里 ⇒ 库里追不到。
# 有这张表，`记忆条目.seq_from/seq_to` → 一条 SELECT 就能取回原文块。
#
# 纪律同 inbox：既有库的建表路径（_SCHEMA）只对新建库跑一次，故另走幂等补建。
# ===========================================================================

_SESSION_BLOCKS_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS session_blocks (
    card_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    type      TEXT,
    time      INTEGER,
    content   TEXT,
    data      TEXT,
    PRIMARY KEY (card_id, seq)
)""",
    "CREATE INDEX IF NOT EXISTS idx_sb_card_seq ON session_blocks(card_id, seq)",
)

# ===========================================================================
# 记忆域表（[记忆因果链 2026-10-20] 照抄 dsh `memory-domain.ts` 的四表之二的本地化）
#
# dsh 的 `memory` 表 = 「每个会话一条：因果链 chain ＋ 两个半开水位」；
# `archive` 表 = 「描述层（process）／文档路径／锚点索引」。
# （规则 rules／知识 knowledge／soul 三表按用户令**不搬**。）
#
# 我们的 `memory_store` 现在仍写 json（entries）；本组表是**因果链模型**的落点，
# 用于 I1–I5 不变量校验与 coveredSeqs 追溯。
# ===========================================================================

_MEMORY_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS events (
    card_id    TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    title      TEXT,
    status     TEXT,
    summary    TEXT,
    subject    TEXT,
    object_key TEXT,
    seq_from   INTEGER,
    seq_to     INTEGER,
    salience   INTEGER,
    confidence TEXT,
    version_no INTEGER,
    created_at INTEGER,
    updated_at INTEGER,
    PRIMARY KEY (card_id, event_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_ev_card_status ON events(card_id, status)",
    """CREATE TABLE IF NOT EXISTS memory (
    card_id       TEXT PRIMARY KEY,
    chain         TEXT,
    condense_mark INTEGER,
    fold_mark     INTEGER,
    batch_starts  TEXT,
    updated_at    TEXT
)""",
    """CREATE TABLE IF NOT EXISTS memory_archive (
    card_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    role      TEXT NOT NULL,
    process   TEXT,
    doc_paths TEXT,
    doc_refs  TEXT,
    PRIMARY KEY (card_id, seq, role)
)""",
)

# ===========================================================================
# 上下文缓存板（context_board —— [缓存板 2026-11-12] 对齐 dsh projection-board
# 的「写时维护」思想：哪些历史在模型视野内，写侧增量算好，装配只投影）
#
# 板上只存「已过闸的可见块」：enters_context 为真 且 seq>=凝练水位。
# 三个写口维护：会话块落库追加（enters_context 过闸才进）、凝练 mark 推进
# （同事务 DELETE seq<mark）、评审流量逐出（写时 keep-N）。
# 板是 session_blocks 的衍生视图——原文账永远是真相，板脏了整份重建。
# ===========================================================================

_CONTEXT_BOARD_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS context_board (
    card_id           TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    role              TEXT,
    actor             TEXT,
    content           TEXT,
    reasoning_content TEXT,
    pair_id           TEXT,
    is_review         INTEGER DEFAULT 0,
    PRIMARY KEY (card_id, seq)
)""",
    "CREATE INDEX IF NOT EXISTS idx_cb_card ON context_board(card_id, seq)",
)

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

# ===========================================================================
# [取消 inbox 状态机 2026-10-22 · 用户裁定 D25]
#
# 用户：「收件箱是个垃圾机制，就给完全被两档通信取消掉」
#
# 实测支撑：生产 work_018 的 inbox 108 条里 37 条长期 pending，最早躺 34 小时；
# 我给 card_041 发的 5 条 msg_ask 全 pending 而它明明在处理 ⇒ pending/consumed
# 这个状态机跟实际处理已完全脱节：既不能驱动处理，也不能证明处理。
#
# 改法：**消息记录保留（审计 / reply_to 应答链需要），状态机删除**，
# 改为「每卡一个读取水位」——「待处理」是算出来的（seq > 水位），不是存出来的。
#   · messages 表：与 inbox 同列，去掉 status / consumed_at
#   · 水位存 meta：key = f"msg_seen_{card_id}"，value = 最后处理到的 seq
#   · 送达靠两档保证：对方空闲→立即点火；对方在跑→本轮收口时按水位自动续一轮
# ===========================================================================

_MESSAGES_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS messages (
    msg_id          TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    target_card_id  TEXT,
    from_card       TEXT,
    type            TEXT,
    content         TEXT,
    reply_to        TEXT,
    seq             INTEGER,
    created_at      TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(target_card_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_messages_work   ON messages(work_id, seq)",
)

# ===========================================================================
# 评审通过记录（[伴生评审卡 2026-10-27]）
#
# 一行＝某角色在某批次上的一次 `approve`。有效票只看 `revoked=0`。
# [2026-10-31 用户令] 评审不再有判据 ⇒ `criteria_ver` 列已删。
#
# 撤销＝`revoked=1`（软删，不删行）。一个角色一个批次一行：重评即覆盖本行。
# ===========================================================================
_REVIEW_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS review_approvals (
    work_id      TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    role_id      TEXT NOT NULL,
    draft_ver    INTEGER NOT NULL DEFAULT 0,
    ts           TEXT DEFAULT '',
    revoked      INTEGER DEFAULT 0,
    verdict      TEXT,
    draft_snapshot TEXT,
    PRIMARY KEY (work_id, batch_id, role_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_review_appr_batch"
    " ON review_approvals(work_id, batch_id)",
)


def _migrate_inbox_to_messages(conn: sqlite3.Connection) -> int:
    """把旧 inbox 表的行搬进 messages（幂等）。返回本次搬运行数。

    **不删旧表**——保留作兜底与审计；新代码只读写 messages。
    """
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inbox'"
        ).fetchone()
        if row is None:
            return 0
        before = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO messages"
            " (msg_id, work_id, target_card_id, from_card, type,"
            "  content, reply_to, seq, created_at)"
            " SELECT msg_id, work_id, target_card_id, from_card, type,"
            "        content, reply_to, seq, created_at FROM inbox"
        )
        after = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        return int(after) - int(before)
    except sqlite3.Error:
        return 0


def get_msg_seen_seq(conn: sqlite3.Connection, work_id: str, card_id: str) -> int:
    """该卡最后处理到的消息 seq（水位）。读不到 ⇒ -1（一条都没读过）。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("msg_seen_" + str(card_id),)
        ).fetchone()
    except sqlite3.Error:
        return -1
    if row is None:
        return -1
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return -1


def set_msg_seen_seq(conn: sqlite3.Connection, work_id: str, card_id: str,
                     seq: int) -> None:
    """把该卡读取水位推到 seq（**只增不减**，防并发回退）。"""
    cur = get_msg_seen_seq(conn, work_id, card_id)
    if int(seq) <= cur:
        return
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("msg_seen_" + str(card_id), str(int(seq))),
    )


async def get_seen_seq(work_id: str, card_id: str) -> int:
    """异步读某卡的读取水位（库不存在 ⇒ -1）。给 routes / 回合收口用。"""
    out = await run_read(
        work_id,
        lambda conn: get_msg_seen_seq(conn, work_id, card_id),
        -1,
    )
    try:
        return int(out)
    except (TypeError, ValueError):
        return -1


async def advance_seen_seq(work_id: str, card_id: str, seq: int) -> int:
    """异步推进某卡的读取水位。返回推进后的水位。"""
    await run_write(
        work_id,
        lambda conn: set_msg_seen_seq(conn, work_id, card_id, seq),
    )
    return await get_seen_seq(work_id, card_id)

#: 待处理状态值（唯一的两态；不设第三态）
# [取消 inbox 状态机 2026-10-22 · D25] 两个状态常量已删除。
# 「待处理」现在由读取水位算出来（get_msg_seen_seq），不再是一个会失真的状态位。
# 若仍有代码引用这两个名字，说明该处是待清理的旧逻辑，应改为按水位判断。

# ===========================================================================
# 回收站（删卡可撤销）
# ===========================================================================
# [画布 2026-10-19] 删卡原来是**硬删**：行、修改历史、会话文件一起消失，界面上
# 只有一句"此操作不可撤销"。卡片正文动辄几千字，误删一次就是不可挽回的损失。
#
# 做法：删除前在**同一个写事务里**把整行 + 全部修改历史 + 会话正文塞进 trash 表，
# 再删原行。撤销＝按**原 card_id** 插回去——id 必须不变，否则正文里
# "见 card_037" 这类引用、以及 links 里的 id 全部指向空气。
#
# 保留策略：不自动清理（回收站很小，一条几百 KB 量级），需要时手动清。
_TRASH_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS trash (
    card_id     TEXT PRIMARY KEY,
    work_id     TEXT NOT NULL,
    deleted_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_trash_work ON trash(work_id)",
)


# ===========================================================================
# [卡面版本 2026-10-24 · 用户令「加历史版本记录，让模型操作失误后找回」]
# 每次正文写入**之前**，把被改动的那一块的**旧值**存一行。
#
# 与 card_history 的分工（两张表同一次事务里写，互不替代）：
#   card_history   ＝「谁在什么时候改了什么」——一句话流水账，界面展示用；
#   card_revisions ＝「改之前那块长什么样」——内容快照，**回退用**。
#
# 触发：只在 `card_store.update_card` 里记（所有正文写入的唯一门）。
# 只存**变了的块**：一次只动现状就只存一行现状旧值，不复制未动块。
# 保留：每卡最近 `_REVISION_KEEP` 个版本，超了删最旧（防 63KB×N 撑爆库）。
# ===========================================================================
_REVISION_KEEP = 20

_REVISIONS_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS card_revisions (
    card_id  TEXT NOT NULL,
    version  INTEGER NOT NULL,
    ts       TEXT NOT NULL,
    block    TEXT NOT NULL,
    content  TEXT NOT NULL,
    note     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (card_id, version, block)
)""",
    "CREATE INDEX IF NOT EXISTS idx_rev_card ON card_revisions(card_id, version DESC)",
)


# ===========================================================================
# [角色机制 5710] 角色卡独立结构：档案 / 判断簿 / 警报簿 / 同步队列 / 职责书史
# ===========================================================================
# 角色卡不再把职责、判词、警报塞进普通卡三块正文；本组表是它的正式结构。
# 与 inbox/trash 同款：幂等 CREATE，既有 work 首次连接即补建。
_ROLE_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS role_profiles (
    card_id         TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL DEFAULT '{}',
    watch           TEXT NOT NULL DEFAULT '{}',
    speak_contract  TEXT NOT NULL DEFAULT '{}',
    escalation      TEXT NOT NULL DEFAULT '{}',
    duty            TEXT NOT NULL DEFAULT '{}',
    charter         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT,
    updated_at      TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_role_profiles_work ON role_profiles(work_id)",
    """CREATE TABLE IF NOT EXISTS role_verdicts (
    id              TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    role_id         TEXT NOT NULL,
    ts              TEXT,
    decision        TEXT,
    reason          TEXT,
    sources         TEXT,
    targets         TEXT,
    evidence        TEXT,
    auto            INTEGER DEFAULT 0,
    superseded_by   TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_role_verdicts_role ON role_verdicts(role_id, ts)",
    """CREATE TABLE IF NOT EXISTS role_alarms (
    id              TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    role_id         TEXT NOT NULL,
    ts              TEXT,
    severity        TEXT,
    title           TEXT,
    detail          TEXT,
    status          TEXT DEFAULT 'open',
    acked_at        TEXT DEFAULT '',
    corrected_by    TEXT DEFAULT '',
    supersedes      TEXT DEFAULT '',
    question        TEXT DEFAULT '',
    options         TEXT DEFAULT '[]',
    recommendation  TEXT DEFAULT '',
    default_action  TEXT DEFAULT '',
    deadline        TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_role_alarms_role ON role_alarms(role_id, status)",
    """CREATE TABLE IF NOT EXISTS role_sync (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id         TEXT NOT NULL,
    role_id         TEXT NOT NULL,
    source          TEXT NOT NULL,
    from_seq        INTEGER,
    to_seq          INTEGER,
    signals         TEXT,
    status          TEXT DEFAULT 'pending',
    created_at      TEXT,
    consumed_at     TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_role_sync_pending ON role_sync(role_id, status)",
    """CREATE TABLE IF NOT EXISTS role_directives (
    id              TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    role_id         TEXT NOT NULL,
    target_card_id  TEXT NOT NULL,
    instruction     TEXT,
    acceptance      TEXT,
    msg_id          TEXT DEFAULT '',
    status          TEXT DEFAULT 'issued',
    evidence        TEXT DEFAULT '',
    created_at      TEXT,
    updated_at      TEXT,
    verified_at     TEXT DEFAULT '',
    due_at          TEXT DEFAULT '',
    parent_id       TEXT DEFAULT '',
    attempt         INTEGER DEFAULT 1
)""",
    "CREATE INDEX IF NOT EXISTS idx_role_directives_role ON role_directives(role_id, status)",
    """CREATE TABLE IF NOT EXISTS role_charter_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id         TEXT NOT NULL,
    role_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    text            TEXT,
    ratified_by     TEXT,
    ratified_at     TEXT,
    note            TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_role_charter_role ON role_charter_history(role_id, version)",
)

# ===========================================================================
# [function 实体化第一步 2026-10-21 · card_028] 职能（function）独立五表
# ===========================================================================
# 职能是「角色」剥掉卡的皮之后的独立实体：身份用独立号段 fn_NNN（不再是
# card_NNN）、**不写 cards 表**——因此不出现在画布/卡片清单/卡树/任何按卡遍历。
# 表结构与 role_* 五张表一一对应（纯改名迁移：card_id/role_id → function_id），
# 另按任务书要求 profile 增加 ``function_name`` 列——职能没有 cards 行，名字只能
# 自己扛（模型自填，本层只存不管内容，不设白名单）。
#
# 与 inbox/trash/role_* 同款：幂等 CREATE 挂在 ``_ensure_tables``（每次连接都跑，
# 已存在即空操作），存量 work 首次连接即补建、不动任何既有行。
# 存储操作在 ``src/storage/function_store.py``（纯存储，不 import api/card_store）。
_FUNCTION_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS function_profiles (
    function_id     TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    function_name   TEXT NOT NULL DEFAULT '',
    jurisdiction    TEXT NOT NULL DEFAULT '{}',
    watch           TEXT NOT NULL DEFAULT '{}',
    speak_contract  TEXT NOT NULL DEFAULT '{}',
    escalation      TEXT NOT NULL DEFAULT '{}',
    duty            TEXT NOT NULL DEFAULT '{}',
    charter         TEXT NOT NULL DEFAULT '{}',
    abilities       TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT,
    updated_at      TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_function_profiles_work ON function_profiles(work_id)",
    """CREATE TABLE IF NOT EXISTS function_verdicts (
    id              TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    function_id     TEXT NOT NULL,
    ts              TEXT,
    decision        TEXT,
    reason          TEXT,
    sources         TEXT,
    targets         TEXT,
    evidence        TEXT,
    auto            INTEGER DEFAULT 0,
    superseded_by   TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_function_verdicts_fn ON function_verdicts(function_id, ts)",
    """CREATE TABLE IF NOT EXISTS function_alarms (
    id              TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    function_id     TEXT NOT NULL,
    ts              TEXT,
    severity        TEXT,
    title           TEXT,
    detail          TEXT,
    status          TEXT DEFAULT 'open',
    acked_at        TEXT DEFAULT '',
    corrected_by    TEXT DEFAULT '',
    supersedes      TEXT DEFAULT '',
    question        TEXT DEFAULT '',
    options         TEXT DEFAULT '[]',
    recommendation  TEXT DEFAULT '',
    default_action  TEXT DEFAULT '',
    deadline        TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_function_alarms_fn ON function_alarms(function_id, status)",
    """CREATE TABLE IF NOT EXISTS function_sync (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id         TEXT NOT NULL,
    function_id     TEXT NOT NULL,
    source          TEXT NOT NULL,
    from_seq        INTEGER,
    to_seq          INTEGER,
    signals         TEXT,
    status          TEXT DEFAULT 'pending',
    created_at      TEXT,
    consumed_at     TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_function_sync_pending ON function_sync(function_id, status)",
    """CREATE TABLE IF NOT EXISTS function_charter_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id         TEXT NOT NULL,
    function_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    text            TEXT,
    ratified_by     TEXT,
    ratified_at     TEXT,
    note            TEXT DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_function_charter_fn ON function_charter_history(function_id, version)",
)


#: 每个 work 的回收站上限。payload 里含完整会话正文（可能几百 KB），
#: 不封顶就会随删卡次数无限增长——回收站是"手滑保险"，不是归档系统。
TRASH_KEEP_PER_WORK = 100


def trash_card(conn: sqlite3.Connection, card_id: str, work_id: str,
               payload: str, deleted_at: str) -> None:
    """把一份删除快照写进回收站（同 card_id 重复删除时覆盖），并裁掉超额的旧记录。"""
    conn.execute(
        "INSERT OR REPLACE INTO trash (card_id, work_id, deleted_at, payload) VALUES (?,?,?,?)",
        (card_id, work_id, deleted_at, payload),
    )
    # 只留最近 N 条：按 deleted_at 倒序，第 N 条之后的整批删掉。
    # 用 SQL 子查询做，避免把 payload 全读进内存再挑。
    conn.execute(
        "DELETE FROM trash WHERE work_id = ? AND card_id NOT IN ("
        "  SELECT card_id FROM trash WHERE work_id = ? ORDER BY deleted_at DESC LIMIT ?"
        ")",
        (work_id, work_id, TRASH_KEEP_PER_WORK),
    )


def get_trash(conn: sqlite3.Connection, card_id: str):
    return conn.execute("SELECT work_id, deleted_at, payload FROM trash WHERE card_id = ?",
                        (card_id,)).fetchone()


def drop_trash(conn: sqlite3.Connection, card_id: str) -> None:
    conn.execute("DELETE FROM trash WHERE card_id = ?", (card_id,))


def list_trash(conn: sqlite3.Connection, work_id: str):
    return conn.execute(
        "SELECT card_id, deleted_at, payload FROM trash WHERE work_id = ? ORDER BY deleted_at DESC",
        (work_id,),
    ).fetchall()


def restore_card_row(conn: sqlite3.Connection, row: dict[str, Any],
                     history: list[Any]) -> bool:
    """按快照把一张卡原样插回去（含修改历史）。已存在则返回 False（幂等）。

    列拆分必须走本模块的 _CARD_COLUMNS / _BODY_COLUMNS / _JSON_COLUMNS ——
    payload 里的 JSON 列**已经是 TEXT**，若当作 list 再 _encode 一次就会变成
    双重编码的字符串，读侧解析直接炸。这里把正文列交给 body、其余交给 meta，
    _encode 对 str 原样返回，所以不会二次编码。
    """
    if conn.execute("SELECT 1 FROM cards WHERE card_id = ?", (row.get("card_id"),)).fetchone():
        return False
    body = {c: row.get(c, "") or "" for c in _BODY_COLUMNS}
    meta = {k: v for k, v in row.items() if k not in _BODY_COLUMNS}
    insert_card(conn, meta, body)
    # 历史按**原始 seq / ts** 写回：修改历史是时间线，重排序号或改写时间戳
    # 等于伪造历史（读者会以为改动的先后顺序变了）。兼容旧的纯文本形态。
    for i, h in enumerate(history or []):
        if isinstance(h, dict):
            seq, ts, text = h.get("seq", i), h.get("ts") or "", h.get("text") or ""
        else:
            seq, ts, text = i, row.get("updated_at") or "", str(h)
        conn.execute(
            "INSERT OR REPLACE INTO card_history (card_id, seq, ts, text) VALUES (?,?,?,?)",
            (row.get("card_id"), seq, ts, text),
        )
    return True


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
    """库文件路径。**调用时**从 card_store.WORKS_DIR 派生（不缓存、不设常量）。

    [库搬出施工目录 2026-11-07] 生产库放 ``works_works_data/{wid}/cards.db``（与 works/
    平级）；旧位置 ``works/{wid}/cards.db`` 由 ``_ensure_db_location`` 在
    建连接前惰性搬走（只搬不判存——纯路径函数保持零副作用）。
    """
    from . import card_store  # 延迟 import：避免 storage 包内循环
    return card_store.db_dir(work_id) / _DB_NAME


def _ensure_db_location(work_id: str) -> Path:
    """建连接前过一遍：旧位置 ``works/{wid}/cards.db`` 还在 ⇒ 原子搬进 ``data/``。

    由 ``_get_conn`` 在持 ``_lock_for`` 期间调用（串行 ⇒ 两连接不会抢着搬）。
    """
    from . import card_store
    return card_store.migrate_db_to_data_dir(work_id, _DB_NAME)


def _lock_for(key: str) -> threading.RLock:
    lock = _locks.get(key)
    if lock is None:
        lock = threading.RLock()
        _locks[key] = lock
    return lock


# ============================================================
# [存储拆锁 2026-10-19 · 5704 承载力迭代]
#
# 病根：原先**所有读**都经 `_read_sync` 抢同一把写锁（`_lock_for`）＋共用同一条
# 连接 ⇒ 104 个并发回合的读全在排队，前端渲染也跟着排，画布卡死。
#
# 拆法：只读走**每线程一条独立只读连接**（`mode=ro`）。WAL 天然支持「多读一写」，
# 读与读、读与写都不再互斥；写仍然串行（一写）。
#
# 约束：`run_read` 的 fn **必须是纯查询**——只读连接上执行写会抛
# `sqlite3.OperationalError: attempt to write a readonly database`。这是有意的
# 硬约束：真要在读路径里写，那是调用方语义错了，应该走 `run_write`。
# ============================================================
#: 只读连接池，key = (库路径, 线程 id)。
#: **不用 threading.local**：`asyncio.to_thread` 走线程池，每个线程一条连接，
#: 而 thread-local 只能从**本线程**访问到自己的 `.conn` ⇒ `reset_caches` 清不掉
#: 别的线程的连接，测试之间会读到上一个 tmp 库（实测 `test_v3_v4_v5_loop_end_to_end`
#: 单跑通过、整套失败，正是这个原因）。显式 map 才能全量关闭。
_read_conns: dict[tuple[str, int], sqlite3.Connection] = {}


def _read_conn(work_id: str) -> Optional[sqlite3.Connection]:
    """取该 work 的**只读**连接（每线程一条，懒建）。库不存在 ⇒ None。"""
    # 先过搬迁判存：旧位置还在 ⇒ 搬进 data/ 再判（os.replace 原子，
    # 与 _get_conn 锁内迁移竞跑也安全——后到者见旧文件已失即用新路径）。
    db = _ensure_db_location(work_id)
    if not db.exists():
        # 与 `_get_conn` 同口径：有 md 无库 ⇒ 明确报错，绝不静默返空
        _guard_migration(work_id, db)
        return None
    key = (str(db), threading.get_ident())
    conn = _read_conns.get(key)
    if conn is None:
        # 首次建只读连接前，**先走写连接做一次 schema 就绪检查**（`_ensure_schema`
        # 里有 ALTER/删列等写操作，只读连接做不了）。只在这一刻持锁，之后所有读
        # 都不再抢锁——这正是"拆锁"的意义。
        try:
            with _lock_for(str(db)):
                _get_conn(work_id, create=False)
        except Exception:  # noqa: BLE001 schema 检查失败不阻断读（旧数据可能仍可读）
            logger.warning("[card_db] schema 就绪检查失败（继续只读）work=%s", work_id)
        try:
            conn = sqlite3.connect(
                f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:  # noqa: BLE001 建不了只读连接 ⇒ 退回共享连接路径
            return None
        _read_conns[key] = conn
    return conn


def reset_caches() -> None:
    """测试沙箱用：关掉全部连接、清空缓存（用例之间 work_id 相同但库在不同 tmp 目录）。"""
    for conn in _conns.values():
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conns.clear()
    _locks.clear()
    # [存储拆锁 2026-10-19] 只读连接同样要关：用例之间库文件换目录，旧连接会
    # 指着上一个 tmp 库（读到空表/旧数据，测试互染）。
    for conn in _read_conns.values():
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _read_conns.clear()


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
    # [存储拆锁 2026-10-19] 只读连接也握 -wal/-shm：恢复快照后不关，读会拿到旧视图。
    # key 是 (库路径, 线程 id) ⇒ 要按库路径扫出所有线程的那条。
    _db = str(db_path(work_id))
    for _k in [k for k in _read_conns if k[0] == _db]:
        ro = _read_conns.pop(_k, None)
        if ro is not None:
            try:
                ro.close()
            except Exception:  # noqa: BLE001
                pass


def _guard_migration(work_id: str, db: Path) -> None:
    """有 cards/*.md 但无 cards.db → 明确报错（见模块 docstring 纪律 3）。"""
    if db.exists():
        return
    # [库搬出施工目录 2026-11-07] cards/*.md 恒在 works/{wid}/ 下，
    # 不能再用 db.parent（库已搬到 works_works_data/{wid}/）。
    from . import card_store
    cards_dir = card_store.WORKS_DIR / str(work_id) / "cards"
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
) + (
    # [自动模式 2026-10-17 · 回灌 5700 块0.1] 四列。开关列/轮次列带 DEFAULT
    # （照 POSITION_COLUMNS 的 pinned 先例：「没写过」与「没开」/「没跑过」是
    # 同一件事）；目标文本两列不带 DEFAULT——「没有目标」不是空串是 NULL。
    ("cards", "auto_mode", "auto_mode INTEGER DEFAULT 0"),
    # [需求文档生命周期 2026-10-28] 草稿列与状态列（老库靠这一步补上）
    ("cards", "requirements_draft", "requirements_draft TEXT"),
    ("cards", "requirements_state", "requirements_state TEXT"),
    # [能力底座 2026-10-30] 模式与能力挂载（老库靠这一步补上）
    ("cards", "mode", "mode TEXT"),
    ("cards", "abilities", "abilities TEXT"),
    ("cards", "goal", "goal TEXT"),
    ("cards", "goal_state", "goal_state TEXT"),
    ("cards", "goal_rounds", "goal_rounds INTEGER DEFAULT 0"),
    # [6702 移植 2026-10-17] 等子卡意图位。开关列带 DEFAULT 0（存量行取 0＝
    # 「没等过子卡」），与 auto_mode/pinned 同口径。
    ("cards", "goal_await_children", "goal_await_children INTEGER DEFAULT 0"),
    # [6702 移植 2026-10-17] 等子卡活锁兜底。唤醒计数器带 DEFAULT 0（存量行取
    # 0＝「没被唤醒过」），与 goal_rounds 同口径。
    ("cards", "goal_await_wakes", "goal_await_wakes INTEGER DEFAULT 0"),
    # [harness 提示词注入 2026-10-28 · 用户定义第三种手段] 待注入队列：
    # JSON 数组，每条是一段要拼进下一轮上下文的话。装配时读出、拼入、清空。
    # 与「消息」的区别：消息进会话历史（永久），注入只影响下一次，不留痕。
    ("cards", "pending_inject", "pending_inject TEXT"),
    # [goal 域 2026-10-20] 事件溯源的投影列（见 _CARD_COLUMNS 处说明）。
    ("cards", "goal_id", "goal_id TEXT DEFAULT ''"),
    ("cards", "goal_revision", "goal_revision INTEGER DEFAULT 0"),
    ("cards", "goal_max_rounds", "goal_max_rounds INTEGER DEFAULT 0"),
    ("cards", "goal_created_at", "goal_created_at INTEGER DEFAULT 0"),
    ("cards", "goal_updated_at", "goal_updated_at INTEGER DEFAULT 0"),
    ("cards", "goal_blocked_code", "goal_blocked_code TEXT DEFAULT ''"),
    ("cards", "goal_blocked_message", "goal_blocked_message TEXT DEFAULT ''"),
    # [伴生评审卡 2026-10-27] 内容列，不带 DEFAULT（照红 2：存量卡该列为 NULL
    # ⇒ 读侧归一成空串 ⇒ 非评审卡）。评审卡不设 parent，靠本列与子卡一对一配对。
    ("cards", "review_of", "review_of TEXT"),
    ("cards", "review_state", "review_state TEXT"),
    # [对齐场 2026-11-11] 对齐卡的场态（JSON：topic/participants/initiator/
    # opener/ended/status/turns/max_turns）。场是一次性的，列只在对齐卡上有值。
    ("cards", "align_state", "align_state TEXT"),
    # [评审真票 2026-10-31 · venue 对齐] verdict=票型（approve/reject，upsert 后票
    # 覆盖前票——先通过又不通过＝不通过）；draft_snapshot=批准时草稿原文
    # （promote 版本闸的 stale 判据与「只评增量」diff 的原料）。
    # 老库补列不带 DEFAULT：存量票 verdict=NULL ⇒ 读侧按 approve 归一（历史上
    # 有票即通过），行为与改前一致。
    ("review_approvals", "verdict", "verdict TEXT"),
    ("review_approvals", "draft_snapshot", "draft_snapshot TEXT"),
    # [卡片笔记 2026-10-27] 影子第五列：卡自己的私密笔记。
    ("shadows", "note", "note TEXT"),
    # [委办线 2026-11-14] 不带 DEFAULT：存量卡 NULL＝没记录（系统/用户/建卡
    # 事件早于本列 ⇒ 无法确证创建者，保守留空——职能对它们无权属，删不掉）。
    ("cards", "created_by", "created_by TEXT"),
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
    # [需求文档正名 2026-10-28 · 用户令「ruling 这个词就不对，全部重新对齐需求文档」]
    # 第三块本树叫「需求文档」，列名却一直沿用旧时代「裁定」的 requirements。
    # 老库首次连接时自动改名（幂等：有旧列且无新列才动），新库走 _SCHEMA 直接是新列。
    ("cards", "rulings", "requirements"),
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
    conn.execute("PRAGMA journal_mode=WAL")
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards'"
    ).fetchone()
    if row is None:
        conn.executescript(_SCHEMA)
    _rename_columns(conn)
    _ensure_columns(conn)
    _drop_columns(conn)
    _ensure_tables(conn)
    _backfill_requirements_state(conn)


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


def _backfill_requirements_state(conn: sqlite3.Connection) -> None:
    """存量卡认定：已有需求文档内容、但没有状态位的卡 ⇒ 视为**正稿**（approved）。

    [需求文档生命周期 2026-10-28] 新流程要求「有正稿才允许派工／才点火」。
    存量卡是在草稿/正稿机制之前写的，若按新规则一律算「无正稿」，整棵树会立刻瘫痪
    （15 张子卡全部失去派工权）。故一次性认定，**只认定内容已存在且状态为空的行**，
    幂等：跑过一次之后没有行再满足条件。

    只动 `requirements_state` 一列，不碰正文，不写历史。
    """
    try:
        rows = conn.execute(
            "SELECT count(*) FROM cards WHERE COALESCE(requirements,'')<>'' "
            "AND COALESCE(requirements_state,'')=''").fetchone()
        n = int(rows[0] or 0) if rows else 0
        if n:
            conn.execute(
                "UPDATE cards SET requirements_state='approved' "
                "WHERE COALESCE(requirements,'')<>'' "
                "AND COALESCE(requirements_state,'')=''")
            logger.info("需求文档状态回填：%s 张存量卡认定为正稿", n)
    except sqlite3.OperationalError as exc:  # 老库还没补列
        logger.debug("需求文档状态回填跳过（列未就绪）：%s", exc)


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """幂等补建**后续新增**的表（如 inbox）。

    ``_SCHEMA`` 里的一切 ``CREATE TABLE IF NOT EXISTS`` 只在**新建库**那条
    路径上被执行一次；迁移跑在加表之前的既有库不会经过它。故新增表必须在此
    单独补一遍——``CREATE TABLE IF NOT EXISTS`` 本身幂等，反复跑不出错，
    可安全地每次建连接都调。**不碰任何既有表、不读写任何既有行。**
    """
    for ddl in _INBOX_DDL:
        conn.execute(ddl)
    for ddl in _SESSION_BLOCKS_DDL:
        conn.execute(ddl)
    for ddl in _MEMORY_DDL:
        conn.execute(ddl)
    for ddl in _TRASH_DDL:
        conn.execute(ddl)
    for ddl in _REVISIONS_DDL:
        conn.execute(ddl)
    for ddl in _ROLE_DDL:
        conn.execute(ddl)
    for ddl in _FUNCTION_DDL:
        conn.execute(ddl)
    # [取消 inbox 状态机 2026-10-22 · D25] messages 表 + 旧 inbox 数据迁移。
    # 迁移是幂等的（INSERT OR IGNORE），存量 work 首次连接即完成搬迁；
    # 旧 inbox 表**保留不删**——兜底与审计用，新代码只读写 messages。
    for ddl in _MESSAGES_DDL:
        conn.execute(ddl)
    for ddl in _REVIEW_DDL:
        conn.execute(ddl)
    # [缓存板 2026-11-12] 上下文可见历史物化板
    for ddl in _CONTEXT_BOARD_DDL:
        conn.execute(ddl)
    # [三线边表 2026-10-27] 建三张线表 + 从 cards 字段幂等搬迁（只读字段、只写新表）
    for ddl in _EDGES_DDL:
        conn.execute(ddl)
    _migrate_edges_from_cards(conn)
    # [三线拆开关 2026-10-27] 存量卡补 link_rel/unlink_rel（只加不减）
    _ensure_link_rel_perm(conn)
    # [评审出口 2026-11-01] 存量卡补 respond_review（被评卡交回回应的能力；
    # 该能力晚于一批卡的建卡时刻注册，它们的显式 abilities 清单里没有）
    _ensure_respond_review_perm(conn)
    # [worker 改名 2026-11-05] 存量卡 card_type coder→worker＋abilities 补
    # manuscript（worker 新权限行带查档三件套）
    _ensure_worker_card_type(conn)
    # [对齐场 2026-11-11] 存量卡补 align 能力（create_align 的挂载件；
    # 该能力晚于一大批卡的建卡时刻注册，显式清单里没有就永远看不见工具）
    _ensure_align_perm(conn)
    # [委办线 2026-11-14] ① 职能卡补 dispose_card（委办线的删域挂载件）；
    # ② work_events.card_created.data.by 回填 cards.created_by＋创建者通信线。
    _ensure_dispose_perm(conn)
    _backfill_created_by(conn)
    _migrate_inbox_to_messages(conn)
    _ensure_role_columns(conn)
    _ensure_function_columns(conn)


def _ensure_worker_card_type(conn: sqlite3.Connection) -> None:
    """存量卡 card_type coder→worker（2026-11-05 编程卡改名 worker 卡并扩域）。

    ① ``cards.card_type`` 'coder'→'worker'——改名是类型码级；legacy_types 的
       coder 条（write_blocked）只兜未迁库的读路径，迁完应零残留。
    ② 被迁卡 ``abilities`` 补 ``manuscript``——worker 新权限行带查档三件套，
       而 abilities 是建卡时刻的显式清单，不补则老 worker 卡永远看不到
       list/read/search_manuscript。
    ③ ``card_archive.card_type`` 与 ``trash.payload``（删卡快照 JSON）同口径
       改写——归档/回收站里的卡复原时不能把旧类型码带回活表。
    幂等：重跑 UPDATE 零行、能力已存在跳过。
    """
    try:
        rows = list(conn.execute(
            "SELECT card_id, abilities FROM cards WHERE card_type='coder'"))
    except sqlite3.Error:
        return
    if rows:
        conn.execute("UPDATE cards SET card_type='worker' WHERE card_type='coder'")
        for r in rows:
            try:
                ab = json.loads(r["abilities"] or "[]")
            except Exception:  # noqa: BLE001 坏 abilities 跳过
                continue
            if not isinstance(ab, list) or "manuscript" in ab:
                continue
            ab.append("manuscript")
            conn.execute("UPDATE cards SET abilities=? WHERE card_id=?",
                         (json.dumps(ab, ensure_ascii=False), r["card_id"]))
    try:
        conn.execute(
            "UPDATE card_archive SET card_type='worker' WHERE card_type='coder'")
    except sqlite3.Error:
        pass
    try:
        for r in conn.execute("SELECT card_id, payload FROM trash").fetchall():
            try:
                pl = json.loads(r["payload"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(pl, dict) or pl.get("card_type") != "coder":
                continue
            pl["card_type"] = "worker"
            conn.execute("UPDATE trash SET payload=? WHERE card_id=?",
                         (json.dumps(pl, ensure_ascii=False), r["card_id"]))
    except sqlite3.Error:
        pass


def _ensure_role_columns(conn: sqlite3.Connection) -> None:
    """role_directives 的增量列（既有库幂等补列）。"""
    try:
        cols = {str(r["name"]) for r in conn.execute(
            "PRAGMA table_info(role_directives)").fetchall()}
    except Exception:  # noqa: BLE001
        return
    for name, ddl in (("parent_id", "TEXT DEFAULT ''"),
                      ("attempt", "INTEGER DEFAULT 1")):
        if name not in cols:
            try:
                conn.execute(f"ALTER TABLE role_directives ADD COLUMN {name} {ddl}")
            except Exception:  # noqa: BLE001 并发首建时可能已加
                pass
    try:
        acols = {str(r["name"]) for r in conn.execute(
            "PRAGMA table_info(role_alarms)").fetchall()}
    except Exception:  # noqa: BLE001
        acols = set()
    for name, ddl in (("question", "TEXT DEFAULT ''"),
                      ("options", "TEXT DEFAULT '[]'"),
                      ("recommendation", "TEXT DEFAULT ''"),
                      ("default_action", "TEXT DEFAULT ''"),
                      ("deadline", "TEXT DEFAULT ''")):
        if name not in acols:
            try:
                conn.execute(f"ALTER TABLE role_alarms ADD COLUMN {name} {ddl}")
            except Exception:  # noqa: BLE001
                pass


def _ensure_function_columns(conn: sqlite3.Connection) -> None:
    """function_profiles 的增量列（既有库幂等补列）。

    [能力清单 2026-10-30] ``abilities``：这张职能卡挂了哪些能力（code 数组）。
    空数组＝没挂＝工具面按既有路径走（清单未生效，行为零变化）。
    """
    try:
        cols = {str(r["name"]) for r in conn.execute(
            "PRAGMA table_info(function_profiles)").fetchall()}
    except Exception:  # noqa: BLE001 表还没建出来 ⇒ 建表语句已带该列
        return
    if "abilities" not in cols:
        try:
            conn.execute(
                "ALTER TABLE function_profiles ADD COLUMN abilities TEXT NOT NULL DEFAULT '[]'")
        except Exception:  # noqa: BLE001 并发首建时可能已加
            pass


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
    # [库搬出施工目录 2026-11-07] 先把旧位置的库搬进 data/ 再取路径——
    # 在锁内做 ⇒ 迁移只发生一次，之后的连接全走新路径。
    db = _ensure_db_location(work_id)
    key = str(db)
    conn = _conns.get(key)
    if conn is not None:
        return conn
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
    """只读执行（**不抢写锁**，走线程内独立只读连接）。

    [存储拆锁 2026-10-19] 见 `_read_conn` 的病根段：原先持写锁 ⇒ 读全排队。
    现在读走 `mode=ro` 独立连接，WAL 下与写并行；`_read_conn` 拿不到（库不存在
    / 建连失败）⇒ 返回 default（与旧行为一致）。
    """
    conn = _read_conn(work_id)
    if conn is None:
        return default
    try:
        return fn(conn)
    except sqlite3.OperationalError as exc:
        # 只读连接上遇到写操作 ⇒ 调用方语义错（该走 run_write），如实抛出便于定位
        raise sqlite3.OperationalError(
            f"只读路径出现写操作（run_read 的 fn 必须纯查询）: {exc}"
        ) from exc


def write_sync(work_id: str, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    """**同步**写（供必须保持同步签名的装配链用，如取走待注入队列）。

    [harness 提示词注入 2026-10-28] 上下文装配链（build_ctx）是同步签名，
    而「取走待注入」是读+清空的写操作；为此暴露这一条，与 read_sync 同量级。
    """
    return _write_sync(work_id, fn)


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


# ---------------------------------------------------------------------------
# [卡面版本 2026-10-24] 正文块历史版本原语（表结构见 _REVISIONS_DDL）
# ---------------------------------------------------------------------------
_BODY_BLOCKS: tuple[str, ...] = ("mission", "status_block", "requirements")


def append_revisions(conn: sqlite3.Connection, card_id: str, ts: str, note: str,
                     old_blocks: dict[str, str],
                     new_blocks: dict[str, str]) -> Optional[int]:
    """把**变了的块**的旧值存成一个新版本；无变化返 ``None``（不占版本号）。

    一次改动共用一个 version（多块同时变就多行）。旧值＝调用方读到的 row 值，
    即「这次改动之前那块长什么样」——回退到 version N 就是回到那次改动**之前**。
    """
    changed = {
        b: (old_blocks.get(b) or "")
        for b in _BODY_BLOCKS
        if (old_blocks.get(b) or "") != (new_blocks.get(b) or "")
    }
    if not changed:
        return None
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS m FROM card_revisions WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    ver = int(row["m"]) + 1
    for block, content in changed.items():
        conn.execute(
            "INSERT INTO card_revisions (card_id, version, ts, block, content, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (card_id, ver, ts, block, content, note or ""),
        )
    # 保留最近 N 版：删掉 version <= 当前-keep 的（同事务，幂等）
    conn.execute(
        "DELETE FROM card_revisions WHERE card_id = ? AND version <= ?",
        (card_id, ver - _REVISION_KEEP),
    )
    return ver


def list_revisions(conn: sqlite3.Connection, card_id: str,
                   limit: int = _REVISION_KEEP) -> list[dict[str, Any]]:
    """版本列表（version 倒序）：每项 ``{version, ts, note, blocks, sizes}``。

    只给**摘要**（各块字节数），不搬全文——列版本不该把 63KB 拉进上下文。
    要拿内容走 ``get_revision``（回退时内部用）。
    """
    rows = conn.execute(
        "SELECT version, ts, block, length(content) AS n, note FROM card_revisions "
        "WHERE card_id = ? ORDER BY version DESC, block",
        (card_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    by_ver: dict[int, dict[str, Any]] = {}
    for r in rows:
        v = int(r["version"])
        e = by_ver.get(v)
        if e is None:
            e = {"version": v, "ts": r["ts"], "note": r["note"], "blocks": [], "sizes": {}}
            by_ver[v] = e
            out.append(e)
        e["blocks"].append(r["block"])
        e["sizes"][r["block"]] = int(r["n"])
    return out[:limit]


def get_revision(conn: sqlite3.Connection, card_id: str,
                 version: int) -> dict[str, str]:
    """某版本存下的块内容 ``{block: 旧值}``；版本不存在返空 dict。"""
    rows = conn.execute(
        "SELECT block, content FROM card_revisions WHERE card_id = ? AND version = ?",
        (card_id, version),
    ).fetchall()
    return {r["block"]: r["content"] for r in rows}


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
    # [卡片笔记 2026-10-27 · 用户令] 第五区：卡自己的私密笔记。
    # 常驻 150 区；**别的卡读不到**（`read_via` 的影子分支剥掉它）。
    "## 笔记": "note",
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
    """发下一个卡号（不落盘；由调用方与建卡同事务写回）。

    [编号脱节修复 2026-10-25 · work_027 建子卡 UNIQUE 冲突] 取
    ``max(meta.seq, 库内实际最大 card_NNN) + 1``，**不再只信 meta.seq**。

    为什么：``create_card`` 支持调用方**强制指定 card_id**（复活归档卡
    ``coder_fold.unarchive_coder``、建职能卡 ``create_function`` 的 fn 号段）。
    那条路 id 不按 seq 走，seq 只按「建了几张」前进 ⇒ 一旦某张强制 id 用了
    比 seq 大的号，seq 就落后于实际最大号；此后正常发号会发到一个**已存在**
    的号上，建卡直接 ``UNIQUE constraint failed: cards.card_id``。
    以实际最大号兜底后，两条路径不再脱节（代价：一次 MIN/MAX 扫描，走主键）。
    """
    raw = get_meta_value(conn, "seq", "0")
    try:
        base = int(raw)
    except (TypeError, ValueError):
        base = 0
    # [发号兜底 2026-11-05] 扫描面从活表扩到 trash／card_archive／coder_archive：
    # 已删/已归档卡的号同样**永久占号**（重发会让新卡继承旧影子/旧事件史——
    # Face-as-Truth 被污染）。meta.seq 万一被 restore 回退到它们之下，
    # 三表兜底仍把号顶上去。表可能不存在（老库）⇒ 逐表 try，缺席跳过。
    top = 0
    for table in ("cards", "trash", "card_archive", "coder_archive"):
        try:
            row = conn.execute(
                f"SELECT MAX(CAST(SUBSTR(card_id, 6) AS INTEGER)) AS m "
                f"FROM {table} WHERE card_id LIKE 'card_%'"
            ).fetchone()
            top = max(top, int((row["m"] if row is not None else 0) or 0))
        except sqlite3.Error:
            continue
    return max(base, top) + 1



def next_fn_seq(conn: sqlite3.Connection) -> int:
    """发下一个**职能号**（独立号段 ``meta.fn_seq``；不落盘；由调用方与建行同事务写回）。

    [function 实体化第一步 2026-10-21 · card_028] 与 ``next_seq``（cards 的
    meta.seq）同款先例，但键独立——职能号段与卡号段互不侵占；职能身份是
    ``fn_001`` 形态，不占 card_NNN。
    """
    raw = get_meta_value(conn, "fn_seq", "0")
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
    """行 → dict。**兼容 messages 表**（无 status / consumed_at 两列）。

    [取消 inbox 状态机 2026-10-22 · D25] messages 表没有状态列，这里补 None，
    保证老调用方拿到的 dict 形状不变；但 status 已不再是任何逻辑的驱动源，
    读它的代码都属于待清理项（见 routes.py 的未读角标）。
    """
    try:
        keys = set(row.keys())
    except Exception:  # noqa: BLE001
        keys = set(_INBOX_COLUMNS)
    return {col: (row[col] if col in keys else None) for col in _INBOX_COLUMNS}


def _now_iso() -> str:
    """本地时区 ISO 时间戳（与 session_store._now_iso 同格式）。"""
    from datetime import datetime
    return datetime.now().astimezone().isoformat()


def insert_inbox(conn: sqlite3.Connection, *, msg_id: str, work_id: str,
                 target_card_id: str, from_card: str, type: str,
                 content: str, reply_to: str = "", created_at: str = "") -> int:
    """投递一条消息。返回其 seq（work 内投递序号，1 起）。

    [取消 inbox 状态机 2026-10-22 · D25] 写 `messages` 表，**不再写 status**——
    消息只是一条记录；是否被处理由「读取水位」算出来（见 get_msg_seen_seq）。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE work_id = ?",
        (work_id,),
    ).fetchone()
    nxt = int((row[0] if row is not None else 0) or 0) + 1
    conn.execute(
        "INSERT INTO messages (msg_id, work_id, target_card_id, from_card, type,"
        " content, reply_to, seq, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, work_id, target_card_id, from_card, type, content, reply_to,
         nxt, created_at),
    )
    return nxt
def select_pending(conn: sqlite3.Connection, work_id: str,
                   target_card_id: str) -> list[dict[str, Any]]:
    """列某收件卡**尚未处理**的消息（按投递序升序）。

    [取消 inbox 状态机 2026-10-22 · D25] **不再是 status 查询，而是水位比较**：
    该卡读过的最后一条 seq 之后的消息就是待处理。水位只增不减，所以这里算出的
    「待处理」不会像旧的 pending 状态位那样失真——它反映的是真实处理进度。
    """
    seen = get_msg_seen_seq(conn, work_id, target_card_id)
    rows = conn.execute(
        "SELECT * FROM messages WHERE work_id = ? AND target_card_id = ?"
        " AND seq > ? ORDER BY seq",
        (work_id, target_card_id, seen),
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
            "SELECT * FROM messages WHERE work_id = ? AND target_card_id = ? ORDER BY seq",
            (work_id, target_card_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages WHERE work_id = ? ORDER BY seq", (work_id,)
        ).fetchall()
    return [_inbox_row_to_dict(r) for r in rows]


def set_consumed(conn: sqlite3.Connection, msg_id: str,
                 consumed_at: str = "") -> int:
    """标记一条消息已被处理。返回受影响行数（1＝推进成功，0＝找不到该消息）。

    [取消 inbox 状态机 2026-10-22 · D25] **不再逐条改状态，而是推进该卡的读取水位**：
    处理到这条消息 = 该卡读过它及之前的全部消息。水位只增不减，
    所以并发/乱序处理都不会把已读的退回成未读。
    """
    row = conn.execute(
        "SELECT work_id, target_card_id, seq FROM messages WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    if row is None:
        return 0
    try:
        set_msg_seen_seq(conn, str(row["work_id"]),
                         str(row["target_card_id"] or ""), int(row["seq"]))
    except (TypeError, ValueError, IndexError):
        return 0
    return 1


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


def take_pending_in_tx(conn: sqlite3.Connection, work_id: str,
                       target_card_id: str) -> list[dict[str, Any]]:
    """事务内取走待处理消息：读出 + 把水位推到本批最大 seq。"""
    pending = select_pending(conn, work_id, target_card_id)
    if pending:
        mx = max(int(it.get("seq") or 0) for it in pending)
        set_msg_seen_seq(conn, work_id, target_card_id, mx)
    return pending


async def take_pending(work_id: str, target_card_id: str) -> list[dict[str, Any]]:
    """**取走**该卡的待处理消息：同一写事务内「读出 + 推水位到本批最大 seq」。

    [增量投递 2026-10-23 · 用户令「读过的信息就从 box 里清掉，存起来，让它不必
    再读到」] 逻辑是增量：**投递即消费**。行不删（messages 表永久留档，可回溯），
    只是水位越过它 ⇒ 下次只投递水位之后的新消息。

    改前的问题（实测 work_025）：投递（deliver_now / K3）把 pending 全文拼进
    prompt 却不推水位；而水位**只**在模型主动调 ``read_inbox(scope="unread")``
    时推进（全仓唯一调用点 card_tools.py 的 advance_seen_seq）。模型在 prompt
    里已经看到消息，没有理由再调 read_inbox ⇒ 水位永远不动 ⇒ 同一批被反复投递：
      · 299 条永久 pending，17 张卡水位=0（从没调过 read_inbox）
      · card_029 一次投递 46 条 / 82,720 字符 = prompt 的 98%
      · 模型自己在会话里写「已是前一轮归档过的内容，无需重复处理」

    原子性：读与推水位在同一个 BEGIN IMMEDIATE 写事务里 ⇒ 两个并发投递不会
    拿到同一批各投一次。
    """
    out = await run_write(
        work_id, lambda conn: take_pending_in_tx(conn, work_id, target_card_id))
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


# ============================================================
# [三线边表 2026-10-27 · 用户裁定] 三条线不再是卡上的字段，各建一张表。
#
# 用户原话：「所有的通信必须基于底层，而不是基于卡本身」「而且要三张表吧」。
#
# 为什么建表：
#   · 反向查询（谁连了我／谁依赖我）原来是全表扫，建索引后 O(1)；
#   · 删卡能自动清边，不再靠人调 unlink_*；
#   · 图查询（环闭合率／自主连线率）有了数据基础。
#
# 为什么三张不是一张：**约束不同**。
#   card_parent  —— 一张卡只能有一个父（主键即卡号，唯一性天然成立）
#   card_links   —— 同 (from,to,type) 唯一，可多条，字段是 type/note
#   card_relations —— 同 (from,to,kind) 唯一，可多条，字段是 kind/detail
# 通用边表表达不了「唯一」，且字段会退化成 JSON。
#
# links 存**一行、双向可用**：现在 check_linked 的语义是「任一侧有就算有线」，
# A 建线 B 能回话。存两行会有「删一半」的中间态。
#
# 迁移**只读 cards、只写新表**：cards.parent/links/relations 一个字节不改，
# 迁移错了删表重来即可。
# ============================================================
_EDGES_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS card_parent (
    card_id     TEXT PRIMARY KEY,
    parent_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_card_parent_parent ON card_parent(parent_id)",
    """CREATE TABLE IF NOT EXISTS card_links (
    from_card   TEXT NOT NULL,
    to_card     TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (from_card, to_card, type)
)""",
    "CREATE INDEX IF NOT EXISTS idx_card_links_to ON card_links(to_card)",
    """CREATE TABLE IF NOT EXISTS card_relations (
    from_card   TEXT NOT NULL,
    to_card     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (from_card, to_card, kind)
)""",
    "CREATE INDEX IF NOT EXISTS idx_card_relations_to ON card_relations(to_card)",
)


def _json_list(raw: Any) -> list[dict[str, Any]]:
    """容错解析卡上存的 JSON 数组（坏数据当空，不炸迁移）。"""
    import json as _json
    if not raw:
        return []
    try:
        got = _json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []


def _migrate_edges_from_cards(conn: sqlite3.Connection) -> None:
    """把 cards.parent / links / relations 搬进三张边表。**幂等**。

    只读 cards、只写新表；`INSERT OR IGNORE` 重复跑不出错。
    存量 work 首次连接即完成搬迁，新 work 建卡时同步写入（不走这条）。
    """
    try:
        conn.execute(
            "INSERT OR IGNORE INTO card_parent(card_id, parent_id, created_at) "
            "SELECT card_id, parent, COALESCE(created_at, '') FROM cards "
            "WHERE parent IS NOT NULL AND TRIM(parent) != ''"
        )
    except sqlite3.Error:
        return                      # cards 表还没有 parent 列等情形：静默跳过
    for r in list(conn.execute(
            "SELECT card_id, links, relations, created_at FROM cards")):
        cid, ts = r["card_id"], (r["created_at"] or "")
        for e in _json_list(r["links"]):
            tgt = str(e.get("target") or "").strip()
            if not tgt:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO card_links"
                "(from_card,to_card,type,note,created_at) VALUES(?,?,?,?,?)",
                (cid, tgt, str(e.get("type") or ""), str(e.get("note") or ""), ts))
        for e in _json_list(r["relations"]):
            tgt = str(e.get("target") or "").strip()
            if not tgt:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO card_relations"
                "(from_card,to_card,kind,detail,created_at) VALUES(?,?,?,?,?)",
                (cid, tgt, str(e.get("kind") or ""), str(e.get("detail") or ""), ts))

    # ③ 存量父子补默认通信线。
    # 新建卡在 create_card 里已经补了；存量卡没有 ⇒ 这里补。
    # 不补的话，「父子默认有线」只对新卡成立——将来拆掉 _is_in_subtree 退路，
    # 存量父子会立刻通信断掉。
    # INSERT OR IGNORE 按 (from,to,type) 去重：父子之间已有「关联」线的就不补
    # （有线的意思就是能通信，不必再多一条）。
    for r in conn.execute("SELECT card_id, parent_id FROM card_parent"):
        conn.execute(
            "INSERT OR IGNORE INTO card_links"
            "(from_card,to_card,type,note,created_at) VALUES(?,?,?,?,?)",
            (r["card_id"], r["parent_id"], "关联", "父子默认通信线", ""))


# ---------------------------------------------------------------- 边表读
def edges_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """一次读三张表 → `{card_id: {"parent":…, "links":[…], "relations":[…]}}`。

    只放**有的键**：某卡在表里没有任何行，就不会出现在返回里——
    调用方据此回退到 cards 行上的旧字段（迁移失败时系统仍可用）。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT card_id, parent_id FROM card_parent"):
        out.setdefault(r["card_id"], {})["parent"] = r["parent_id"]
    for r in conn.execute(
            "SELECT from_card,to_card,type,note FROM card_links ORDER BY rowid"):
        out.setdefault(r["from_card"], {}).setdefault("links", []).append(
            {"target": r["to_card"], "type": r["type"], "note": r["note"]})
    for r in conn.execute(
            "SELECT from_card,to_card,kind,detail FROM card_relations ORDER BY rowid"):
        out.setdefault(r["from_card"], {}).setdefault("relations", []).append(
            {"target": r["to_card"], "kind": r["kind"], "detail": r["detail"]})
        # [relations_in 2026-11-01 · 用户裁定「谁依赖我应该在 B 的卡面上，
        # 需要知道的不只是 B」] 同一条边反向投影到目标卡：写入只有一份
        # （声明方出边），读侧双向投影，不复制存储。
        out.setdefault(r["to_card"], {}).setdefault("relations_in", []).append(
            {"source": r["from_card"], "kind": r["kind"], "detail": r["detail"]})
    return out


def edges_of(conn: sqlite3.Connection, card_id: str) -> dict[str, Any]:
    """单卡的三条线（没有的键不出现）。"""
    out: dict[str, Any] = {}
    r = conn.execute("SELECT parent_id FROM card_parent WHERE card_id=?",
                     (card_id,)).fetchone()
    if r is not None:
        out["parent"] = r["parent_id"]
    links = [{"target": x["to_card"], "type": x["type"], "note": x["note"]}
             for x in conn.execute(
                 "SELECT to_card,type,note FROM card_links WHERE from_card=? "
                 "ORDER BY rowid", (card_id,))]
    if links:
        out["links"] = links
    rels = [{"target": x["to_card"], "kind": x["kind"], "detail": x["detail"]}
            for x in conn.execute(
                "SELECT to_card,kind,detail FROM card_relations WHERE from_card=? "
                "ORDER BY rowid", (card_id,))]
    if rels:
        out["relations"] = rels
    # [relations_in 2026-11-01] 反向投影「谁依赖我」——入边随卡面读侧走，
    # 存储仍只有声明方出边一份。
    rels_in = [{"source": x["from_card"], "kind": x["kind"], "detail": x["detail"]}
               for x in conn.execute(
                   "SELECT from_card,kind,detail FROM card_relations WHERE to_card=? "
                   "ORDER BY rowid", (card_id,))]
    if rels_in:
        out["relations_in"] = rels_in
    return out


def children_of(conn: sqlite3.Connection, card_id: str) -> list[str]:
    """直接子卡（parent 表反查）。"""
    return [r["card_id"] for r in conn.execute(
        "SELECT card_id FROM card_parent WHERE parent_id=? ORDER BY card_id",
        (card_id,))]


def linkers_of(conn: sqlite3.Connection, card_id: str) -> list[str]:
    """**谁连了我**（links 反向，双向语义：任一侧有即算）。"""
    seen: list[str] = []
    for r in conn.execute(
            "SELECT from_card AS other FROM card_links WHERE to_card=? "
            "UNION SELECT to_card AS other FROM card_links WHERE from_card=?",
            (card_id, card_id)):
        if r["other"] != card_id and r["other"] not in seen:
            seen.append(r["other"])
    return seen


def dependents_of(conn: sqlite3.Connection, card_id: str) -> list[str]:
    """**谁依赖我**（relations 反向，带 kind/detail 的原始行另见 dependents_detail）。"""
    return [r["from_card"] for r in conn.execute(
        "SELECT from_card FROM card_relations WHERE to_card=? ORDER BY from_card",
        (card_id,))]


def dependents_detail(conn: sqlite3.Connection, card_id: str) -> list[dict[str, Any]]:
    """谁依赖我 + 它们声明的 kind/detail（依赖通知要用它写清"改了会影响谁"）。"""
    return [{"from_card": r["from_card"], "kind": r["kind"], "detail": r["detail"]}
            for r in conn.execute(
                "SELECT from_card,kind,detail FROM card_relations WHERE to_card=? "
                "ORDER BY from_card", (card_id,))]


def has_link(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """a 与 b 之间有没有通信线（**双向**：任一侧有即算）。"""
    r = conn.execute(
        "SELECT 1 FROM card_links WHERE (from_card=? AND to_card=?) "
        "OR (from_card=? AND to_card=?) LIMIT 1",
        (a, b, b, a)).fetchone()
    return r is not None


# ---------------------------------------------------------------- 边表写
def upsert_parent(conn: sqlite3.Connection, card_id: str, parent_id: str,
                  created_at: str = "") -> None:
    if not parent_id:
        conn.execute("DELETE FROM card_parent WHERE card_id=?", (card_id,))
        return
    conn.execute(
        "INSERT INTO card_parent(card_id,parent_id,created_at) VALUES(?,?,?) "
        "ON CONFLICT(card_id) DO UPDATE SET parent_id=excluded.parent_id",
        (card_id, parent_id, created_at or _now_iso()))


def upsert_link(conn: sqlite3.Connection, from_card: str, to_card: str,
                type_: str = "", note: str = "", created_at: str = "") -> None:
    conn.execute(
        "INSERT INTO card_links(from_card,to_card,type,note,created_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(from_card,to_card,type) DO UPDATE "
        "SET note=excluded.note",
        (from_card, to_card, type_, note, created_at or _now_iso()))


def delete_link(conn: sqlite3.Connection, from_card: str, to_card: str,
                type_: str = "") -> int:
    """删线。type_ 为空 ⇒ 删该方向上全部类型。返回删除行数。"""
    if type_:
        cur = conn.execute(
            "DELETE FROM card_links WHERE from_card=? AND to_card=? AND type=?",
            (from_card, to_card, type_))
    else:
        cur = conn.execute(
            "DELETE FROM card_links WHERE from_card=? AND to_card=?",
            (from_card, to_card))
    return int(cur.rowcount or 0)


def upsert_relation(conn: sqlite3.Connection, from_card: str, to_card: str,
                    kind: str = "", detail: str = "",
                    created_at: str = "") -> None:
    conn.execute(
        "INSERT INTO card_relations(from_card,to_card,kind,detail,created_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(from_card,to_card,kind) DO UPDATE "
        "SET detail=excluded.detail",
        (from_card, to_card, kind, detail, created_at or _now_iso()))


def delete_relation(conn: sqlite3.Connection, from_card: str, to_card: str,
                    kind: str = "") -> int:
    if kind:
        cur = conn.execute(
            "DELETE FROM card_relations WHERE from_card=? AND to_card=? AND kind=?",
            (from_card, to_card, kind))
    else:
        cur = conn.execute(
            "DELETE FROM card_relations WHERE from_card=? AND to_card=?",
            (from_card, to_card))
    return int(cur.rowcount or 0)


def clear_edges_of(conn: sqlite3.Connection, card_id: str) -> None:
    """删卡时清掉与它相关的**全部边**（三个方向：作父、作子、作两端）。

    用户裁定「通信必须基于底层」的另一面：边是底层实体，卡没了边就该没。
    改前靠人调 unlink_cards 清理悬空线，漏一次就留一条指向空气的线。
    """
    conn.execute("DELETE FROM card_parent WHERE card_id=? OR parent_id=?",
                 (card_id, card_id))
    conn.execute("DELETE FROM card_links WHERE from_card=? OR to_card=?",
                 (card_id, card_id))
    conn.execute("DELETE FROM card_relations WHERE from_card=? OR to_card=?",
                 (card_id, card_id))


def _ensure_link_rel_perm(conn: sqlite3.Connection) -> None:
    """存量卡补 `link_rel` / `unlink_rel` 能力（2026-10-27 三条线拆开关）。

    [能力底座 2026-11-01 重写] 原来搬的是 ``cards.perms`` 列——该列已随
    perms 退役删除，此函数一度成了早退的死代码；而 ``permissions.json``
    的卡型模板也没有补这两个开关 ⇒ ``abilities_for`` 挂载时被 perms 上限
    滤掉 ⇒ **没有任何卡挂得上 link_rel 能力**。现在搬的是 ``abilities``
    列（能力挂载的真实存处）。

    **只加不减**：abilities 里有 ``link`` 的卡补 ``link_rel`` + ``unlink_rel``
    （有连线权的卡本来就能建依赖线，拆开不该让它失去这条能力）；没有的
    不动。``function_profiles.abilities`` 同口径。
    """
    for table, key_col in (("cards", "card_id"),
                           ("function_profiles", "function_id")):
        try:
            cols = {r["name"] for r in conn.execute(
                f"PRAGMA table_info({table})")}
            if "abilities" not in cols:
                continue
            rows = list(conn.execute(
                f"SELECT {key_col}, abilities FROM {table} "
                "WHERE abilities IS NOT NULL AND abilities <> ''"))
        except sqlite3.Error:
            continue
        for r in rows:
            try:
                ab = json.loads(r["abilities"])
            except Exception:  # noqa: BLE001 坏 abilities 跳过，不动它
                continue
            if not isinstance(ab, list) or "link" not in ab:
                continue
            changed = False
            for code in ("link_rel", "unlink_rel"):
                if code not in ab:
                    ab.append(code)
                    changed = True
            if changed:
                conn.execute(
                    f"UPDATE {table} SET abilities=? WHERE {key_col}=?",
                    (json.dumps(ab, ensure_ascii=False), r[key_col]))


def _ensure_respond_review_perm(conn: sqlite3.Connection) -> None:
    """存量卡补 `respond_review` 能力（评审出口工具 submit_revision 的挂载件）。

    [2026-11-01] `respond_review` 注册时 modes 表的 sub_project/topic 上限
    已带它——**新卡**由 ``abilities_for`` 正常挂载；但一批存量卡的
    ``abilities`` 列是建卡时刻的显式清单，早于该能力注册 ⇒
    ``_abilities_of`` 照清单原样返回 ⇒ 被评卡永远看不见 submit_revision，
    只能干瞪着评审场指令「改完调 submit_revision」。

    **只加不减**：abilities 里有 ``open_review`` 的卡补 ``respond_review``
    （能开评的卡就是会被评的卡——开评闸限自开，target==opener）；职能档案
    是评审席不是被评对象，不补。
    """
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
        if "abilities" not in cols:
            return
        rows = list(conn.execute(
            "SELECT card_id, abilities FROM cards "
            "WHERE abilities IS NOT NULL AND abilities <> ''"))
    except sqlite3.Error:
        return
    for r in rows:
        try:
            ab = json.loads(r["abilities"])
        except Exception:  # noqa: BLE001 坏 abilities 跳过，不动它
            continue
        if not isinstance(ab, list) or "open_review" not in ab:
            continue
        if "respond_review" in ab:
            continue
        ab.append("respond_review")
        conn.execute(
            "UPDATE cards SET abilities=? WHERE card_id=?",
            (json.dumps(ab, ensure_ascii=False), r["card_id"]))


def _ensure_align_perm(conn: sqlite3.Connection) -> None:
    """存量卡补 `align` 能力（create_align 的挂载件，对齐场 2026-11-11）。

    `align` 注册时 modes 表各作业卡型上限已带它——**新卡**由
    ``abilities_for`` 正常挂载；但存量卡的 ``abilities`` 列是建卡时刻的
    显式清单，早于该能力注册 ⇒ ``_abilities_of`` 照清单原样返回 ⇒
    老卡永远看不见 create_align。

    **只加不减**的锚（按「本来就该能开场的卡群」对位）：
    - ``open_review``：project/sub_project/topic/function/role——能开评
      的卡就是能张罗事的卡；
    - ``report_done``：worker——它是子卡，按用户令「子卡也可发起」；
    - ``report_up_chain``：ui——同 worker 口径的执行体子卡。
    tool/rule/场务卡（review/qa/pm/align）不补：不跑普通回合或不该开场
    （仍可被邀为成员——成员身份不看能力清单）。
    """
    anchors = ("open_review", "report_done", "report_up_chain")
    for table, key_col in (("cards", "card_id"),
                           ("function_profiles", "function_id")):
        try:
            cols = {r["name"] for r in conn.execute(
                f"PRAGMA table_info({table})")}
            if "abilities" not in cols:
                continue
            rows = list(conn.execute(
                f"SELECT {key_col}, abilities FROM {table} "
                "WHERE abilities IS NOT NULL AND abilities <> ''"))
        except sqlite3.Error:
            continue
        for r in rows:
            try:
                ab = json.loads(r["abilities"])
            except Exception:  # noqa: BLE001 坏 abilities 跳过，不动它
                continue
            if not isinstance(ab, list) or "align" in ab:
                continue
            if not any(a in ab for a in anchors):
                continue
            ab.append("align")
            conn.execute(
                f"UPDATE {table} SET abilities=? WHERE {key_col}=?",
                (json.dumps(ab, ensure_ascii=False), r[key_col]))


def _ensure_dispose_perm(conn: sqlite3.Connection) -> None:
    """存量职能卡补 `dispose_card` 能力（委办线·组织线的第二种 2026-11-14）。

    委办关系给了职能「删自己建的卡」的管辖权——但 ``dispose_card`` 晚于一大批
    职能卡的建卡时刻才进模式清单，它们的显式 abilities 里没有 ⇒
    ``_abilities_of`` 照清单原样返回 ⇒ ``delete_card`` 永远不在工具面。

    作用域收窄在 ``_h_delete_card``（离树卡只能删 ``created_by==自己`` 的卡）——
    这里补的是「工具面可见」那半；范围只到 function，不外溢到树内卡
    （树内卡的删域本来就是子树，不受委办关系影响）。

    ``function_profiles.abilities`` 同口径补（席位场授面按档案清单算）。
    """
    for table, key_col, where in (
            ("cards", "card_id", "card_type='function'"),
            ("function_profiles", "function_id", "1=1")):
        try:
            cols = {r["name"] for r in conn.execute(
                f"PRAGMA table_info({table})")}
            if "abilities" not in cols:
                continue
            rows = list(conn.execute(
                f"SELECT {key_col}, abilities FROM {table} "
                f"WHERE {where} AND abilities IS NOT NULL AND abilities <> ''"))
        except sqlite3.Error:
            continue
        for r in rows:
            try:
                ab = json.loads(r["abilities"])
            except Exception:  # noqa: BLE001 坏 abilities 跳过，不动它
                continue
            if not isinstance(ab, list) or "dispose_card" in ab:
                continue
            ab.append("dispose_card")
            conn.execute(
                f"UPDATE {table} SET abilities=? WHERE {key_col}=?",
                (json.dumps(ab, ensure_ascii=False), r[key_col]))


def _backfill_created_by(conn: sqlite3.Connection) -> None:
    """从 ``work_events`` 的 ``card_created`` 事件回填 ``cards.created_by``
    ＋补**创建者默认通信线**（委办线 2026-11-14）。

    建卡事件的 ``data.by`` 一直记着建卡方（``_notify_card_changed(by=created_by)``），
    只是此前没落列。本迁移把这份既有事实补成委办关系：

      ① ``cards.created_by``：空则填 ``by``（已有值不动——幂等、不覆盖人工修正）；
      ② ``card_links``：补 ``card→creator`` 的创建者默认通信线
         （``_linked_between`` 任一向即成立 ⇒ 创建者对它建的卡天然可 direct，
         与父子默认线同构）。created_by==parent 时父子线已覆盖，不重复写。

    ``by`` 为空 / ``by`` 不是现存卡（已删）⇒ 整行跳过（列与线都不落）——
    权属认不准就不给，保守方向与「没记录⇒无权属」一致。
    """
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
        if "created_by" not in cols:
            return
        rows = list(conn.execute(
            "SELECT card_id, data, ts FROM work_events "
            "WHERE type='card_created' ORDER BY seq"))
    except sqlite3.Error:
        return                                # 老库没 work_events 表 ⇒ 无源可回
    creators: dict[str, tuple[str, str]] = {}  # card_id → (by, ts)
    for r in rows:
        cid = str(r["card_id"] or "")
        if not cid or cid in creators:
            continue                          # 重复建卡事件取最早一条为准
        try:
            by = str((json.loads(r["data"] or "{}") or {}).get("by") or "")
        except (TypeError, ValueError):
            continue
        if by:
            creators[cid] = (by, str(r["ts"] or ""))
    for cid, (by, ts) in creators.items():
        if by == cid:
            continue
        if conn.execute("SELECT 1 FROM cards WHERE card_id=?",
                        (by,)).fetchone() is None:
            continue                          # 创建者已不在 ⇒ 不落权属不造假边
        prow = conn.execute(
            "SELECT created_by, parent FROM cards WHERE card_id=?",
            (cid,)).fetchone()
        if prow is None:
            continue
        if not prow["created_by"]:
            conn.execute("UPDATE cards SET created_by=? WHERE card_id=?",
                         (by, cid))
        if str(prow["parent"] or "") != by:
            conn.execute(
                "INSERT INTO card_links(from_card,to_card,type,note,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(from_card,to_card,type) DO NOTHING",
                (cid, by, "关联", "创建者默认通信线",
                 ts or _now_iso()))
