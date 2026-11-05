# -*- coding: utf-8 -*-
"""依赖关联线 · 本任务全部新测试（2026-10-16）。

一句话：**通信线让出 ``relations`` 这个名字，改叫 ``links``；``relations``
归新增的第三种线「依赖关联线」。**

覆盖任务书 §六 三块的全部硬判据：
- 块1 存储层：老库改名迁移（幂等）／新库两列／两路表结构一致／read_card 两键；
- 块2 工具层：工具数 21／版本号／link_rel·unlink_rel 建解线／**隔离**（依赖线不
  参与 read_linked / send_card_message）／越权／read_card 两键；
- 块3 注入区：16 区含 order 250／双向渲染／无线不出现／顺序 200<250<300／
  三席零变化／前端改名。

纪律：所有用例走 conftest 沙箱（WORKS_DIR 重定向到 tmp），**不写真实 works/**。
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

from src.api import card_tools, context_assembler, context_regions, prompts, routes
from src.storage import card_db, card_store, canvas_store, work_store

ROOT = Path(__file__).resolve().parents[1]


# ════════════════════════════════════════════════════════════════════
# 夹具
# ════════════════════════════════════════════════════════════════════

async def _tree(wid: str) -> dict:
    """根卡 card_001（create_work 自带）→ 002=子卡A（父001）→ 004=孙卡A1（父002）。

    ★ 号位注意：sub_project/topic 建卡即派生**伴生评审卡**（card_store.create_card
    尾部，离树、占一个 card_id）⇒ 子卡A 是 card_002、它的评审卡是 card_003、
    孙卡A1 实为 **card_004**（伴生评审卡 card_005）。旧版断言里的 card_003
    指的是孙卡，全部顺移为 card_004。

    perms 由建卡时按 config/permissions.json 固化（rd_project/sub_project 含
    link/unlink/read_linked/link_rel/unlink_rel），无需注册表夹具。
    """
    await card_store.create_card(wid, "子卡A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "孙卡A1", "用户直接创建",
                                 card_type="sub_project", parent="card_002")
    return card_store.iter_card_metas(wid)


def _columns_of(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return [r["name"] for r in conn.execute("PRAGMA table_info(cards)")]
    finally:
        conn.close()


def _make_legacy_db(db_path: Path, rel_json: str) -> None:
    """造一个「老 schema」库：cards 表带旧列 relations（无 links）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE cards (
                card_id TEXT PRIMARY KEY, work_id TEXT, title TEXT, source TEXT,
                card_type TEXT, parent TEXT, project_class TEXT, owner_card_id TEXT,
                state TEXT, tags TEXT, relations TEXT, session_ref TEXT,
                toolcard_script TEXT, toolcard_step TEXT, toolcard_round INTEGER,
                toolcard_final TEXT, thinkchain_final TEXT, perms TEXT,
                perms_version INTEGER, created_at TEXT, updated_at TEXT,
                mission TEXT, status_block TEXT, requirements TEXT);
        """)
        conn.execute(
            "INSERT INTO cards (card_id, work_id, title, relations)"
            " VALUES (?, ?, ?, ?)",
            ("card_001", db_path.parent.name, "老卡", rel_json))
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════
# 块1 · 存储层
# ════════════════════════════════════════════════════════════════════

async def test_b1_legacy_db_renames_relations_to_links(sandbox):
    """硬判据1：老库跑一次连接后同时含 links 与 relations；links 保有旧数据。"""
    wid = "work_legacy"
    rel_json = json.dumps([{"target": "card_002", "type": "关联"}],
                          ensure_ascii=False)
    db = card_db.db_path(wid)
    _make_legacy_db(db, rel_json)
    assert "relations" in _columns_of(db) and "links" not in _columns_of(db)

    def _row(conn):
        return dict(conn.execute(
            "SELECT links, relations FROM cards WHERE card_id='card_001'").fetchone())

    row = card_db.read_sync(wid, _row, {})
    cols = _columns_of(db)
    assert "links" in cols and "relations" in cols
    # links 保有原 relations 的旧数据（逐字节一致）；新 relations 列全空
    assert row["links"] == rel_json
    assert row["relations"] is None
    card_db.reset_caches()


async def test_b1_new_db_has_both_columns(sandbox, work):
    """硬判据2：新库建库即含 links＋relations 两列。"""
    cols = _columns_of(card_db.db_path(work["work_id"]))
    assert "links" in cols and "relations" in cols


async def test_b1_new_and_migrated_schema_consistent(sandbox):
    """硬判据2（续）：新库列集合 == 老库升级后列集合。

    ★ 逐列比对按**列集合**（顺序不可能一致：老库的 relations 走 ADD COLUMN
    恒追加在末尾，新库按 _SCHEMA 位置；对读路径无影响——row_to_meta 按
    _CARD_COLUMNS 迭代，不依赖库内列序）。
    """
    # 新库
    new_wid = (await work_store.create_work("新库"))["work_id"]
    new_cols = set(_columns_of(card_db.db_path(new_wid)))
    # 老库（升级后）
    old_wid = "work_legacy_schema"
    db = card_db.db_path(old_wid)
    _make_legacy_db(db, "[]")
    card_db.read_sync(old_wid, lambda c: c.execute("SELECT 1").fetchone(), None)
    old_cols = set(_columns_of(db))
    assert new_cols == old_cols, (new_cols ^ old_cols)
    card_db.reset_caches()


async def test_b1_rename_is_idempotent(sandbox):
    """硬判据3：同一库重复跑 _ensure_schema 多次，结构与数据零变化。"""
    wid = "work_legacy_idem"
    rel_json = json.dumps([{"target": "card_002", "type": "先后"}],
                          ensure_ascii=False)
    db = card_db.db_path(wid)
    _make_legacy_db(db, rel_json)

    def _snapshot(conn):
        return (_columns_of(db), dict(conn.execute(
            "SELECT links, relations FROM cards WHERE card_id='card_001'").fetchone()))

    first = None
    for _ in range(4):
        card_db.reset_caches()   # 强制重开连接 → 重跑 _ensure_schema
        snap = card_db.read_sync(wid, lambda c: dict(c.execute(
            "SELECT links, relations FROM cards WHERE card_id='card_001'").fetchone()), {})
        cols = _columns_of(db)
        if first is None:
            first = (cols, snap)
        else:
            assert (cols, snap) == first, "重复 _ensure_schema 改动了结构或数据"
    assert first[1]["links"] == rel_json
    card_db.reset_caches()


async def test_b1_read_card_meta_has_both_keys(sandbox, work):
    """硬判据5：card_store.read_card 返回的 meta 含 links 与 relations 两键。"""
    wid = work["work_id"]
    result = await card_store.read_card(wid, "card_001")
    meta = result["meta"]
    assert "links" in meta and "relations" in meta
    assert meta["links"] == [] and meta["relations"] == []


# ════════════════════════════════════════════════════════════════════
# 块2 · 工具层
# ════════════════════════════════════════════════════════════════════

def test_b2_tool_table_and_versions():
    """硬判据1：注册表字面量 22 件；tools-tc-8；t22-1。

    [更新现状按钮改造 2026-10-16] 字面量 21 → 22（新增 propose_status，
    列 OP_ONLY_TOOLS、不进常规工具面）；TOOL_TABLE_VERSION tools-tc-7 → tools-tc-8。
    """
    src = (ROOT / "src" / "api" / "card_tools.py").read_text(encoding="utf-8")
    assert src.count('": ToolDef(') == 35  # [2026-11-02 UI 卡] 34→35（+create_ui_card）
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-25"  # [2026-11-02] 新增 create_ui_card
    assert prompts.BASE_PROMPT_VERSION == "t27-11"  # [2026-11-01] 随现配置对齐（事实分层 t27-2→t27-3）
    assert "link_rel" in card_tools.TOOL_REGISTRY
    assert "unlink_rel" in card_tools.TOOL_REGISTRY
    # [2026-11-01] 开关词汇表扩列后 link_rel/unlink_rel 已是独立开关
    # （不再复用 link/unlink——SWITCHES 里真有这两个码）。
    assert card_tools.TOOL_SWITCH["link_rel"] == "link_rel"
    assert card_tools.TOOL_SWITCH["unlink_rel"] == "unlink_rel"
    # 写工具（不进只读白名单）
    assert "link_rel" not in card_tools.READONLY_TOOLS
    assert "unlink_rel" not in card_tools.READONLY_TOOLS


async def test_b2_link_rel_writes_relations_only(sandbox):
    """硬判据2：link_rel 建线后卡上 relations 增一条 {target,kind,detail}；links 零变化。"""
    wid = (await work_store.create_work("link_rel 剧本"))["work_id"]
    cards = await _tree(wid)
    r = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖",
                          "detail": "依赖它的接口设计"}, cards)
    assert "error" not in r, r
    meta = (await card_store.read_card(wid, "card_002"))["meta"]
    assert meta["relations"] == [
        {"target": "card_004", "kind": "依赖", "detail": "依赖它的接口设计"}]
    # 通信线零变化：仍只有建卡自带的父子默认线（002→001），link_rel 不碰 links
    assert [l["target"] for l in meta["links"]] == ["card_001"]

    # 幂等：同 (target, kind) 不重复追加
    r2 = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖",
                          "detail": "依赖它的接口设计"}, cards)
    assert r2.get("existed") is True
    meta2 = (await card_store.read_card(wid, "card_002"))["meta"]
    assert len(meta2["relations"])== 1
    # detail 变化 → 就地更新（不重复追加）
    r3 = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖",
                          "detail": "改为依赖它的数据口径"}, cards)
    assert r3.get("updated") is True
    meta3 = (await card_store.read_card(wid, "card_002"))["meta"]
    assert len(meta3["relations"]) == 1
    assert meta3["relations"][0]["detail"] == "改为依赖它的数据口径"

    # 另一 kind 同 target：两条并存（kind 词汇表＝依赖/影响）
    await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "影响",
                          "detail": "同属一个模块"}, cards)
    meta4 = (await card_store.read_card(wid, "card_002"))["meta"]
    assert len(meta4["relations"]) == 2

    # [relations_in 2026-11-01 · 用户令「谁依赖我应该在对方卡面上」]
    # 被指的卡读自己的 meta 能反查到入边（存一处、读侧投影，不写第二份）。
    meta_b = (await card_store.read_card(wid, "card_004"))["meta"]
    assert {x["source"] for x in meta_b["relations_in"]} == {"card_002"}


async def test_b2_isolation_read_linked_ignores_relations(sandbox):
    """硬判据3（核心）：只有依赖线、没有通信线 → read_linked 报「无通信线」。"""
    wid = (await work_store.create_work("隔离·read_linked"))["work_id"]
    cards = await _tree(wid)
    await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖",
                          "detail": "依赖它"}, cards)
    # 依赖线确实建上了
    assert (await card_store.read_card(wid, "card_002"))["meta"]["relations"]
    # 但沿它 read_linked 不可达（只认 links）
    r = await card_tools._h_read_linked(
        wid, "card_002", {"target_id": "card_004", "part": "正文"}, cards)
    assert r["ok"] is False
    assert "无通信线" in r["error"]["message"]


async def test_b2_isolation_send_card_message_not_reached_by_relations(sandbox):
    """硬判据3（核心）：依赖线不构成 send_card_message 的可达集。

    构造：把依赖线**直接写进卡**（绕过 link_rel 的子树校验）指向一张
    **不在发送方子树内**的兄弟卡——若发信会读依赖线，就会被放行；
    实测仍 denied（发信只看树，不读任何线）。
    """
    wid = (await work_store.create_work("隔离·发信"))["work_id"]
    # 根卡 001 → 002(A)、004(B) 两兄弟（003/005 是各自的伴生评审卡）
    await card_store.create_card(wid, "A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "B", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.update_card(
        wid, "card_002",
        {"relations": [{"target": "card_004", "kind": "影响", "detail": "手写依赖线"}]},
        "测试：手写依赖线")
    cards = card_store.iter_card_metas(wid)
    r = await card_tools._h_send_card_message(
        wid, "card_002",
        {"target_card_id": "card_004", "msg_type": "msg_ask", "content": "hi"}, cards)
    assert r["error"]["code"] == "denied", r
    # 机械佐证：[5713] 改口径——发信执行体**不读依赖线**（relations 不构成通行证），
    # 通信线的判定单点收在 policy.check_linked（执行体只调这一个关口）。
    src = inspect.getsource(card_tools._h_send_card_message)
    assert "relations" not in src
    assert "policy.check_linked" in src


async def test_b2_unlink_rel_default_and_by_kind(sandbox):
    """硬判据4：unlink_rel 缺省解全部指向该 target；指定 kind 只解对应条目。"""
    wid = (await work_store.create_work("unlink_rel 剧本"))["work_id"]
    cards = await _tree(wid)
    await card_tools._h_link_rel(wid, "card_002",
                                 {"target_id": "card_004", "kind": "依赖", "detail": "a"}, cards)
    await card_tools._h_link_rel(wid, "card_002",
                                 {"target_id": "card_004", "kind": "影响", "detail": "b"}, cards)
    # 指定 kind 只解一条
    r = await card_tools._h_unlink_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖"}, cards)
    assert r.get("removed") == 1
    rels = (await card_store.read_card(wid, "card_002"))["meta"]["relations"]
    assert [x["kind"] for x in rels] == ["影响"]
    # 缺省解全部
    r2 = await card_tools._h_unlink_rel(wid, "card_002", {"target_id": "card_004"}, cards)
    assert r2.get("removed") == 1
    assert (await card_store.read_card(wid, "card_002"))["meta"]["relations"] == []
    # 已无 → not_found
    r3 = await card_tools._h_unlink_rel(wid, "card_002", {"target_id": "card_004"}, cards)
    assert r3["error"]["code"] == "not_found"


async def test_b2_link_rel_no_tree_scope_limit(sandbox):
    """硬判据5：link_rel 建线**不受 parent 树约束**（平级卡可自主建线）。

    [5713 通道试走 2026-10-20·用户口径「卡间通信有专门的通信线」] 本用例原名
    ``test_b2_link_rel_denied_outside_subtree``，断言「连子树外 → denied」。
    该断言属于**旧的树作用域**口径，已被本次改动有意推翻：建线的边界是线本身，
    平级卡若连不出线，就永远通不了话。故改为：兄弟卡建线放行；
    target/kind/detail 的校验一律不变（仍是 validation）。
    """
    wid = (await work_store.create_work("越权剧本"))["work_id"]
    # 根 001 → A(002)、B(004) 兄弟（003/005 是伴生评审卡）；A 连 B（不在 A 子树内）放行
    await card_store.create_card(wid, "A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "B", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    cards = card_store.iter_card_metas(wid)
    r = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖", "detail": "x"}, cards)
    assert r.get("ok") is True, r          # [5713] 平级卡建线放行
    _m = (await card_store.read_card(wid, "card_002"))["meta"]
    assert [x["target"] for x in _m["relations"]] == ["card_004"]
    # 自身 → validation
    r2 = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_002", "kind": "依赖", "detail": "x"}, cards)
    assert r2["error"]["code"] == "validation"
    # kind 非法 → validation；detail 空 → validation
    r3 = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "耦合", "detail": "x"}, cards)
    assert r3["error"]["code"] == "validation"
    r4 = await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖", "detail": "  "}, cards)
    assert r4["error"]["code"] == "validation"


def test_b2_tool_face_gated_by_link_rel_switch():
    """硬判据5（续）：link_rel/unlink_rel 是**独立开关**，不复用 link/unlink。

    [三线拆开关 2026-10-27 · 用户裁定] 依赖线的开关与通信线分开授：只有
    link/unlink（通信线权）没有 link_rel/unlink_rel 的卡，工具面看不到
    依赖线工具；反过来也成立。
    """
    def _visible(perms):
        tools = [{"name": n} for n in card_tools.TOOL_SWITCH]
        cards = {"card_x": {"card_id": "card_x", "card_type": "sub_project",
                            "perms": perms}}
        return {t["name"] for t in
                card_tools._filter_by_category(tools, "card_x", "", cards)}

    with_rel = _visible(["read", "link_rel", "unlink_rel"])
    assert "link_rel" in with_rel and "unlink_rel" in with_rel
    # 只有通信线开关、没有依赖线开关 ⇒ 依赖线工具不可见（不复用）
    comm_only = _visible(["read", "link", "unlink", "edit"])
    assert "link_rel" not in comm_only and "unlink_rel" not in comm_only
    neither = _visible(["read", "edit"])
    assert "link_rel" not in neither and "unlink_rel" not in neither


async def test_b2_read_card_returns_both_keys(sandbox):
    """硬判据6：read_card（工具）返回同时含 links/relations/relations_in 三键。"""
    wid = (await work_store.create_work("read_card 两键"))["work_id"]
    cards = await _tree(wid)
    r = await card_tools._h_read_card(wid, "card_002", {"card_id": "card_004"}, cards)
    assert "links" in r and "relations" in r and "relations_in" in r


async def test_b2_read_via_body_carries_relations_position(sandbox):
    """[relations_in 2026-11-01] 沿通信线读正文，关系位置（出边＋入边）随卡面给。"""
    wid = (await work_store.create_work("沿读关系位置"))["work_id"]
    cards = await _tree(wid)
    # 孙卡 004 声明依赖子卡 002 → 002 读 004 时应看到入边投影
    await card_tools._h_link_rel(
        wid, "card_004", {"target_id": "card_002", "kind": "依赖",
                          "detail": "依赖它的任务分派"}, cards)
    # 002→004 有父子默认通信线？默认线是子→父方向（004→002），002 出边无 004。
    # 显式建一条 002→004 通信线让 read_via 可达。
    r = await card_tools._h_link_cards(
        wid, "card_002", {"target_id": "card_004", "type": "关联"}, cards)
    assert "error" not in r, r
    out = await card_tools.read_via(wid, "card_002", "card_004", "正文")
    assert out["ok"] is True, out
    val = out["value"]
    assert "requirements" in val, "正文必须带需求文档（rulings 化石键已修）"
    assert [x["target"] for x in val["relations"]] == ["card_002"]
    assert val["relations_in"] == []


async def test_b2_link_rel_on_real_toolface(sandbox):
    """[能力底座补齐 2026-11-01] 真建出来的卡工具面带 link_rel/unlink_rel。

    回归网：此前 permissions.json 漏补这两个开关 ⇒ abilities_for 挂载时
    link_rel 能力全被滤掉 ⇒ 依赖线工具全网不可见（见 v15 注释）。
    """
    wid = (await work_store.create_work("依赖线工具面"))["work_id"]
    await _tree(wid)
    names = {t["name"] for t in card_tools.build_tool_schemas(
        card_id="card_002", work_id=wid,
        cards=card_store.iter_card_metas(wid))}
    assert "link_rel" in names and "unlink_rel" in names, sorted(names)


# ════════════════════════════════════════════════════════════════════
# 块3 · 注入区 + 前端
# ════════════════════════════════════════════════════════════════════

def test_b3_region_table_16_regions_with_250():
    """硬判据1：REGION_TABLE 17 区、orders 含 250。

    [自动模式 2026-10-17 · 回灌 5700 块0.4] 16→17 区：新增 order 152「自动模式·目标」
    （150 当前卡之后、155 区域说明之前；只对 auto 卡有内容）。
    """
    orders = [r["order"] for r in context_regions.REGION_TABLE]
    # [2026-10-21] 配置演进至 v9：新增 91/92/93/145/175 五区，17→22 同步快照。
    # [2026-11-01] 22→23：新增 order 88 区（类型思考等），快照随行。
    assert len(context_regions.REGION_TABLE) == 23
    assert orders == [0, 88, 85, 90, 91, 92, 93, 95, 100, 145, 150, 152, 155,
                      170, 175, 200, 250, 300, 900, 999, 1000, 1010, 1020]
    by_key = {r["key"]: r for r in context_regions.REGION_TABLE}
    assert by_key["goal"]["name"] == "自动模式·目标"
    assert by_key["relations"]["name"] == "依赖关联"
    # [2026-11-01 · 用户令「谁依赖我应该在对端可见，需要知道的不只是它自己」]
    # 关系位置是卡面事实，三席做判断都要看得到 ⇒ seats/profiles 放开为三席
    # （profiles 才是真正的消费者闸门）。
    assert by_key["relations"]["seats"] == ("main", "viewpoint", "draft")
    assert by_key["relations"]["profiles"] == ("main", "viewpoint", "draft")


def test_b3_config_v5_has_relations_region():
    """硬判据1（续）：区控制 v6 生效；坏 JSON → fail-open 按注册表默认。

    [自动模式 2026-10-17 · 回灌 5700 块0.4] v5→v6（加 goal 区条目）。
    """
    cfg = json.loads((ROOT / "config" / "context_regions.json").read_text(encoding="utf-8"))
    # [2026-10-21] 配置演进 v6→v9（新增 function 相关区），同步版本号断言
    assert cfg["_meta"]["version"] == "context-regions-v9"
    assert cfg["regions"]["relations"] == {"enabled": True, "order": 250}
    assert cfg["regions"]["goal"] == {"enabled": True, "order": 152}


def test_b3_render_empty_is_blank():
    """硬判据2（续）：无线 → 空串（没有来源就不出现这个区）。"""
    assert context_regions.render_dep_relations_region([], []) == ""


def test_b3_render_both_directions():
    """硬判据2：正向「我依赖/关联：B」＋反向「依赖/关联我的：A」；出边带目标需求文档。"""
    out = context_regions.render_dep_relations_region(
        [{"target": "card_003", "kind": "依赖", "detail": "它的接口",
          "requirements": "接口要支持批量"}], [])
    assert out.startswith("## 依赖关联")
    assert "- 我依赖/关联：card_003（依赖·它的接口）" in out
    # [2026-11-01] 出边投影目标当前需求文档——接口改了经关系线自动更新
    assert "card_003 的需求文档（随它卡面更新）：" in out
    assert "接口要支持批量" in out
    inn = context_regions.render_dep_relations_region(
        [], [{"source": "card_001", "kind": "影响", "detail": "同模块"}])
    assert "- 依赖/关联我的：card_001（影响·同模块）" in inn


async def test_b3_injection_two_directions(sandbox):
    """硬判据2（端到端）：A 建线指向 B → A/B 上下文各见对应方向；出边带目标需求文档。

    [2026-11-01 · 用户两条裁定] ①入边「谁依赖我」属于对方卡面事实，三席都可见；
    ②接口变更经关系线自动更新——出边条目投影目标**当前**需求文档，不靠通知。
    """
    wid = (await work_store.create_work("双向注入"))["work_id"]
    cards = await _tree(wid)
    await card_tools._h_link_rel(
        wid, "card_002", {"target_id": "card_004", "kind": "依赖",
                          "detail": "依赖它的接口设计"}, cards)
    # 目标卡写下对外信息（需求文档正稿）
    await card_store.update_card(
        wid, "card_004", {"requirements": "接口 v2：必填字段 repo/branch"},
        "测试：写需求文档")

    ctx_a = context_assembler.build_ctx(wid, "card_002")
    ctx_b = context_assembler.build_ctx(wid, "card_004")
    main_a = context_regions.compose_model_context(ctx_a, profile="main")
    main_b = context_regions.compose_model_context(ctx_b, profile="main")
    assert "- 我依赖/关联：card_004" in main_a["system_prompt"]
    assert "- 依赖/关联我的：card_002" in main_b["system_prompt"]
    # A 的上下文不该出现「依赖/关联我的：…」（A 没被指）
    assert "依赖/关联我的：" not in main_a["system_prompt"]

    # 出边投影：A 的上下文里就是 B 的当前需求文档——它改卡面，下一轮自动新
    assert "card_004 的需求文档（随它卡面更新）：" in main_a["system_prompt"]
    assert "接口 v2：必填字段 repo/branch" in main_a["system_prompt"]

    # [2026-11-01] 关系位置是卡面事实 ⇒ 三席（viewpoint）也看得到这个区
    vp_a = context_regions.compose_model_context(ctx_a, profile="viewpoint")
    assert "## 依赖关联" in (vp_a.get("neutral_context") or ""
                            or vp_a.get("system_prompt") or "")


async def test_b3_no_relations_region_absent(sandbox):
    """硬判据2（续）：无线 → 该区不出现（compose 结果里没有 250）。"""
    wid = (await work_store.create_work("无线不出现"))["work_id"]
    await _tree(wid)
    ctx = context_assembler.build_ctx(wid, "card_002")
    main = context_regions.compose_model_context(ctx, profile="main")
    # 按区头判（BASE_PROMPT 文案里也有「依赖关联线」字样，不能用裸词）
    assert "## 依赖关联" not in main["system_prompt"]
    orders = [r["order"] for r in main["system_regions"]]
    assert 250 not in orders


async def test_b3_order_between_memory_and_history(sandbox):
    """硬判据3：order 250 位于 200（记忆）之后、300（对话历史）之前。"""
    wid = (await work_store.create_work("顺序"))["work_id"]
    await _tree(wid)
    ctx = context_assembler.build_ctx(wid, "card_002")
    # [2026-11-01] 记忆区数据源已迁 memory_nodes（ctx.memory_entries）；
    # memory_state 入口退役 ⇒ 直接喂装配产物。
    ctx.memory_entries = [{"id": "n1", "level": 1, "role": "gist",
                           "gist": "旧结论", "batch": 1}]
    ctx.dep_relations_out = [{"target": "card_004", "kind": "依赖", "detail": "d"}]
    main = context_regions.compose_model_context(ctx, profile="main")
    sp = main["system_prompt"]
    i_mem = sp.index("## 记忆")
    i_rel = sp.index("## 依赖关联")
    assert i_mem < i_rel, "250 应排在 200 记忆之后"
    # 300 对话历史在 prior 槽（不在 system_prompt 里）——按注册表 order 断言
    orders = [r["order"] for r in context_regions.REGION_TABLE]
    assert orders.index(200) < orders.index(250) < orders.index(300)


def test_b3_frontend_renamed():
    """硬判据5：前端 vm.relations → vm.links；canvas.js/workspace.js 不再有 relations。"""
    canvas = (ROOT / "static" / "canvas.js").read_text(encoding="utf-8")
    ws = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "relations" not in canvas, "canvas.js 仍有 relations 标识符"
    assert "relations" not in ws, "workspace.js 仍有 relations 标识符"
    # canvas.js 消费 vm.links（通信线）；数据键 data.links
    assert "vm.links" in canvas and "data.links" in canvas
    # workspace 面板读写 meta.links（通信线）
    assert "meta.links" in ws


async def test_b3_canvas_store_projection_key_is_links(sandbox):
    """硬判据5（续）：canvas_store 投影键改名为 links（画布只吃通信线）。"""
    wid = (await work_store.create_work("画布投影"))["work_id"]
    cards = await _tree(wid)
    await card_tools._h_link_cards(
        wid, "card_002", {"target_id": "card_004", "type": "关联"}, cards)
    data = await canvas_store.update_cache(wid)
    assert "links" in data and "relations" not in data
    assert any(l["source"] == "card_002" and l["target"] == "card_004"
               for l in data["links"])
