# -*- coding: utf-8 -*-
"""卡片区域语义与上下文支持 · 卡片模块（2026-10-14 · **v3**）回归网。

v3 相对 v2 的变化：用户 17:48／17:56 三条令 ⇒「职责与边界」（duty）**整体取消**
（"边界得靠权限来限制，不靠文字描述"）。本文件相应撤掉 duty 的全部用例，改测：

- 影子 **coordination**（待协作事项）——独立成列、可写可读回；
- 库结构**可升可降**：缺列即 ADD、冗余列即 DROP，两向都幂等；
- duty 列在库与代码里**已彻底退役**（grep 判据 ＋ 结构判据）；
- 「上级卡读子卡影子」的文本**已清除**；
- ``read_card`` 工具开口只给三块（使命／现状／第三块），不含修改历史。

铁律：沙箱内跑（conftest autouse fixture），不碰真实 works/。

与 ``test_card_duty_region.py`` **不是一回事**：那份测的是**类型级**「卡片职责」
（来源 card_type_registry.json），本份测的是**卡级**正文块结构。
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from src.api import card_draft, card_tools, prompts, routes
from src.storage import card_db, card_store, shadow_store, work_store


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


async def _mk_work(project_class: str = "research_project") -> dict:
    w = await work_store.create_work("区域语义剧本", project_class=project_class)
    assert "error" not in w, w
    return w


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


def _cols(work_id: str, table: str) -> set[str]:
    def _fn(conn):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    return card_db.read_sync(work_id, _fn, set())


def _raw_cols(db_file, table: str) -> set[str]:
    """**绕开 card_db** 直读盘上文件的列名。

    必须绕开：``card_db`` 的任何一次连接都会触发 ``_ensure_schema`` → 补列/删列。
    用它来断言「结构变更之前的样子」会自我实现（连一次就改好了）。
    """
    conn = sqlite3.connect(str(db_file))
    out = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return out


def _nonnull(work_id: str, table: str, col: str) -> int:
    def _fn(conn):
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL"
        ).fetchone()[0]

    return card_db.read_sync(work_id, _fn, -1)


# 「迁移跑完、加列还没发生」的旧 schema：由模块自身列清单反推，夹具不与真实漂移。
_OLD_CARD_COLS = [c for c in card_db._CARD_COLUMNS if c != "coordination"]


def _ddl(table: str, cols: list[str], pk: str) -> str:
    """建表 DDL。主键列显式指定——不做字符串替换（'card_id' 是 'owner_card_id' 的子串）。"""
    body = ",\n  ".join(f"{c} TEXT{' PRIMARY KEY' if c == pk else ''}" for c in cols)
    return f"CREATE TABLE {table} (\n  {body}\n)"


def _make_legacy_db(path, *, card_rows=2, shadow_rows=2):
    """造一个「迁移已跑完、加列还没发生」的库（真旧 schema + 存量行 + 其余表）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(_ddl("cards", _OLD_CARD_COLS, pk="card_id"))
    shadow_cols = [c for c in card_db._SHADOW_COLUMNS if c != "coordination"]
    conn.execute(_ddl("shadows", shadow_cols, pk="owner_card_id"))
    conn.execute(
        "CREATE TABLE card_history (card_id TEXT NOT NULL, seq INTEGER NOT NULL,"
        " ts TEXT, text TEXT, PRIMARY KEY (card_id, seq))"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for i in range(1, card_rows + 1):
        vals = {
            "card_id": f"card_{i:03d}", "work_id": "work_001", "title": f"旧卡{i}",
            "created_at": "t0", "updated_at": "t0",
            "mission": f"使命{i}", "status_block": f"现状{i}", "requirements": f"定论{i}",
        }
        conn.execute(
            f"INSERT INTO cards ({', '.join(_OLD_CARD_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _OLD_CARD_COLS)})",
            [vals.get(c) for c in _OLD_CARD_COLS],
        )
    for i in range(1, shadow_rows + 1):
        vals = {
            "owner_card_id": f"card_{i:03d}", "work_id": "work_001",
            "created_at": "t0", "updated_at": "t0",
            "progress": f"进度{i}", "artifacts": f"产物{i}", "iterations": f"迭代{i}",
        }
        conn.execute(
            f"INSERT INTO shadows ({', '.join(shadow_cols)}) "
            f"VALUES ({', '.join('?' for _ in shadow_cols)})",
            [vals.get(c) for c in shadow_cols],
        )
    conn.commit()
    conn.close()


# ===========================================================================
# 判据 1：duty 已彻底退役（v3 的核心回撤）
# ===========================================================================


def test_duty_not_in_any_column_manifest():
    assert "duty" not in card_db._CARD_COLUMNS
    assert "duty" not in card_db._BODY_COLUMNS
    assert all(c != "duty" for _, c, _ in card_db._COLUMN_UPGRADES)
    assert ("cards", "duty") in card_db._COLUMN_DROPS


async def test_new_db_has_no_duty_column(sandbox):
    wid = (await _mk_work())["work_id"]
    assert "duty" not in _cols(wid, "cards")


async def test_schema_does_not_create_duty(sandbox):
    """新建库的 _SCHEMA 里没有 duty。"""
    assert "duty" not in card_db._SCHEMA


async def test_duty_column_dropped_on_first_access(sandbox):
    """库里有 duty（v2 建过）⇒ 首次访问即 DROP COLUMN。"""
    wid = "work_001"
    db_file = card_db.db_path(wid)
    _make_legacy_db(db_file)
    # 先把 duty 造出来（模拟 v2 建过列的库）
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE cards ADD COLUMN duty TEXT")
    conn.commit()
    conn.close()
    card_db.reset_caches()

    assert "duty" in _raw_cols(db_file, "cards")  # 盘上确实是 v2 形态

    assert card_store.card_exists(wid, "card_001")  # 触发 _ensure_schema

    assert "duty" not in _raw_cols(db_file, "cards")
    # 存量行一字未动
    r = await card_store.read_card(wid, "card_001")
    assert (r["body"]["mission"], r["body"]["status_block"], r["body"]["requirements"]) \
        == ("使命1", "现状1", "定论1")


async def test_drop_refuses_when_column_has_content(sandbox):
    """★ 非空则拒删、绝不默默销毁数据（保真优先）。"""
    wid = "work_001"
    db_file = card_db.db_path(wid)
    _make_legacy_db(db_file)
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE cards ADD COLUMN duty TEXT")
    conn.execute("UPDATE cards SET duty = '有人写过东西' WHERE card_id = 'card_001'")
    conn.commit()
    conn.close()
    card_db.reset_caches()

    assert card_store.card_exists(wid, "card_001")

    # 列还在，内容还在
    assert "duty" in _raw_cols(db_file, "cards")
    conn = sqlite3.connect(str(db_file))
    val = conn.execute(
        "SELECT duty FROM cards WHERE card_id = 'card_001'"
    ).fetchone()[0]
    conn.close()
    assert val == "有人写过东西"


async def test_schema_changes_are_idempotent_both_ways(sandbox):
    """升（补 coordination）与降（删 duty）反复跑都无异常、内容不变。"""
    wid = "work_001"
    db_file = card_db.db_path(wid)
    _make_legacy_db(db_file)
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE cards ADD COLUMN duty TEXT")
    conn.commit()
    conn.close()
    card_db.reset_caches()

    for _ in range(3):
        card_db.reset_caches()
        assert card_store.card_exists(wid, "card_001")

    cols = _raw_cols(db_file, "cards")
    assert "duty" not in cols
    assert "coordination" in _raw_cols(db_file, "shadows")
    assert (await card_store.read_card(wid, "card_001"))["body"]["mission"] == "使命1"


# ===========================================================================
# 判据 2：read_card 的 body 仍是三块（无 duty）
# ===========================================================================


async def test_read_card_body_has_no_duty(sandbox):
    w = await _mk_work()
    cid = await _mk_child(w["work_id"], "子卡", w["work_card_id"])
    body = (await card_store.read_card(w["work_id"], cid))["body"]
    assert "duty" not in body
    # [goal 变卡面区域 2026-10-28] body 多了 goal 键（不随 project_class 改名）
    assert {"mission", "status_block", "requirements", "goal", "history"} == set(body)


async def test_apply_block_set_has_no_duty(sandbox):
    """rd 分支是写死键集重建 body——回归网盯住它不带 duty。"""
    for pc in ("rd_project", "research_project"):
        w = await _mk_work(pc)
        wid, root = w["work_id"], w["work_card_id"]
        cid = await _mk_child(wid, "子卡", root)
        await card_store.update_card(wid, cid, {"requirements": "R"}, "测试")
        out = routes._apply_block_set(await card_store.read_card(wid, cid), wid)
        assert "duty" not in out["body"], pc


# ===========================================================================
# 判据 3：read_card 工具开口 = 三块，且不含修改历史
# ===========================================================================


async def test_read_card_tool_returns_three_blocks(sandbox):
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    await card_store.update_card(
        wid, cid,
        {"mission": "M", "status_block": "S", "requirements": "R"},
        "测试",
    )
    # [关系治理 2026-10-30] 读按线判：旁支读不到，改由父卡（根卡）读子卡
    out = await card_tools._h_read_card(
        wid, root, {"card_id": cid}, card_tools._collect_card_meta(wid)
    )
    assert "error" not in out
    assert out["使命"] == "M"
    assert out["现状"] == "S"
    assert out["body"] == "S"                       # 旧键保留不变
    # [正名 2026-10-28] 第三块模型侧统一叫「需求文档」（与 使命/现状 同为中文键）
    assert out["需求文档"] == "R"
    assert "requirements" not in out and "rulings" not in out
    assert "职责与边界" not in out                  # v3：已取消
    assert "history" not in out and "修改历史" not in out  # 不给模型


async def test_read_card_tool_third_block_named_requirements_on_rd_tree(sandbox):
    w = await _mk_work("rd_project")
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    await card_store.update_card(wid, cid, {"requirements": "REQ"}, "测试")
    # [关系治理 2026-10-30] 同上：改由父卡读子卡
    out = await card_tools._h_read_card(
        wid, root, {"card_id": cid}, card_tools._collect_card_meta(wid)
    )
    # [正名 2026-10-28] 键名统一中文；旧键 requirements/rulings 都不外露
    assert out["需求文档"] == "REQ"
    assert "requirements" not in out and "rulings" not in out


async def test_read_card_tool_envelope_unchanged(sandbox):
    """信封与错误码不变。"""
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    reader = await _mk_child(wid, "读者卡", root)
    out = await card_tools._h_read_card(
        wid, reader, {"card_id": "card_999"}, card_tools._collect_card_meta(wid)
    )
    assert out["error"]["code"] == "not_found"


# ===========================================================================
# 判据 4：duty 不再是合法 kind（写通道已撤）
# ===========================================================================


async def test_duty_kind_rejected(sandbox):
    """v3：duty 经此口 → 拒。"""
    os.environ["MOCK_LLM"] = "1"
    monkey = None  # 无需 mock —— 拒在调模型之前
    assert "duty" not in card_draft.ALLOWED_KINDS

    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "duty")
    assert "error" in d
    c = await card_draft.commit(wid, cid, "duty", "x", "y")
    assert "error" in c


async def test_mission_kind_still_rejected(sandbox):
    os.environ["MOCK_LLM"] = "1"
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    await card_store.update_card(wid, cid, {"mission": "原使命"}, "测试")

    assert "mission" not in card_draft.ALLOWED_KINDS
    assert "error" in await card_draft.draft(wid, cid, "mission")
    assert "error" in await card_draft.commit(wid, cid, "mission", "x", "y")
    assert (await card_store.read_card(wid, cid))["body"]["mission"] == "原使命"


# ===========================================================================
# 判据 5：影子 coordination 可写、可读回
# ===========================================================================


async def test_coordination_declared_in_manifests(sandbox):
    assert "coordination" in card_db._SHADOW_COLUMNS
    assert "coordination" in card_db._SHADOW_SECTION_COLUMNS
    assert card_db.SHADOW_SECTIONS["## 待协作事项"] == "coordination"
    assert ("shadows", "coordination", "coordination TEXT") in card_db._COLUMN_UPGRADES


async def test_shadow_sections_have_coordination(sandbox):
    secs = shadow_store.split_shadow_sections("## 待协作事项\n\n等 B 卡回传 X。")
    assert secs["coordination"] == "等 B 卡回传 X。"
    assert {"progress", "artifacts", "iterations"} <= set(secs)


async def test_shadow_render_includes_new_section(sandbox):
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    await shadow_store.update_shadow(
        wid, cid,
        "## 进度\n\n进度内容\n\n## 待协作事项\n\n需要 B 卡提供接口说明\n\n"
        "## 产物清单\n\n产物路径 X\n\n## 迭代历史\n\n迭代一行\n",
    )
    raw = await shadow_store.read_shadow_raw(wid, cid)
    assert "## 待协作事项" in raw
    assert "需要 B 卡提供接口说明" in raw
    # 既有三节内容一字未动
    assert "进度内容" in raw and "产物路径 X" in raw and "迭代一行" in raw


async def test_shadow_coordination_persisted_in_column(sandbox):
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    await shadow_store.update_shadow(
        wid, cid,
        "## 进度\n\nP\n\n## 待协作事项\n\n等接口\n\n## 产物清单\n\nA\n\n## 迭代历史\n\nI\n",
    )

    def _fn(conn):
        return conn.execute(
            "SELECT progress, coordination, artifacts, iterations FROM shadows"
            " WHERE owner_card_id = ?", (cid,),
        ).fetchone()

    row = card_db.read_sync(wid, _fn, None)
    assert row["coordination"] == "等接口"
    assert (row["progress"], row["artifacts"], row["iterations"]) == ("P", "A", "I")


async def test_shadow_append_to_new_section(sandbox):
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    assert await shadow_store.append_to_section(wid, cid, "## 待协作事项", ["- 等接口"]) is True
    assert await shadow_store.append_to_section(wid, cid, "## 待协作事项", ["- 等接口"]) is True
    raw = await shadow_store.read_shadow_raw(wid, cid)
    assert raw.count("- 等接口") == 1  # 幂等：机械去重


async def test_shadow_derived_starts_without_coordination_content(sandbox):
    w = await _mk_work()
    wid, root = w["work_id"], w["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    def _fn(conn):
        return conn.execute(
            "SELECT coordination FROM shadows WHERE owner_card_id = ?", (cid,)
        ).fetchone()

    row = card_db.read_sync(wid, _fn, None)
    assert row is not None
    assert not (row["coordination"] or "").strip()


# ===========================================================================
# 判据 6：块 C ——「父卡读子卡影子」三处文本已清除
# ===========================================================================


def test_no_parent_reads_child_shadow_in_base_prompt():
    assert "上级卡" not in prompts.BASE_PROMPT
    assert "子卡的影子" not in prompts.BASE_PROMPT
    assert "只读本卡自己的影子" in prompts.BASE_PROMPT


def test_no_parent_reads_child_shadow_in_tool_schema():
    """发给模型的那一份 description（grep 判据）。

    ``ToolDef.schema`` 允许两种形态：dict 字面量，或延迟求值的 Callable
    （file_tools 四件用后者）——两种都要能取到文本。
    """
    def _desc(td) -> str:
        s = td.schema
        if callable(s):
            s = s()
        return (s or {}).get("description", "") or ""

    blob = "\n".join(_desc(td) for td in card_tools.TOOL_REGISTRY.values())
    assert "上级卡" not in blob
    assert "子卡的影子" not in blob
    assert "只读本卡自己的影子" in blob


def test_base_prompt_version_bumped():
    # [worker 卡 2026-10-15] t19-1 → t20-1（增两工具行＋「## worker 卡」节）。
    # [规则卡 2026-10-15] t20-1 → t21-1（新增「## 规则卡」节）。
    # [依赖关联线 2026-10-16] t21-1 → t22-1。
    # [事实分层 2026-11-01] t27-2 → t27-3（_SEC_CARDMSG 语义修正）。
    assert prompts.BASE_PROMPT_VERSION == "t27-11"
