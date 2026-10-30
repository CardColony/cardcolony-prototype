# -*- coding: utf-8 -*-
"""不变量一：卡片 CRUD ＋ 权限由树机械算出（card_012 工作项 2）。

权限是产品的第一不变量：谁能读、谁能写、谁能删，全部由 parent 树与
relations 算出——不看内容、不看类型、不问模型。树一动就坏的东西，先钉死。

约定（card_tools.py 权威定义）：
- 读 = 全盘可读（[卡片权限改造 2026-10-07] 存在即放行；原亲属链/连线/锚卡读限整制去除）
- 写 = 仅自己的子树（血统链上级不可写）
- 越权一律返回 {"error": ...} 数据，不抛异常（工具循环靠这个继续跑）
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_db, card_store, shadow_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


# ---------------------------------------------------------------- 根卡与序号

async def test_work_root_card_shape(work):
    wid, root = work["work_id"], work["work_card_id"]
    assert root == "card_001"

    data = await card_store.read_card(wid, root)
    # [销红 2026-10-12] D.1 卡类型改名裁定：根卡类型「剧本」→ project
    # （config/card_type_registry.json:85 "code":"project"、:98 「剧本」deprecated/write_blocked；
    #   底座收口 2026-10-08，docs/卡片类型底座收口与建卡入口适配_任务书v1.md:27）。
    assert data["meta"]["card_type"] == "project"
    assert data["meta"]["parent"] == ""          # 剧本卡无视 parent 入参
    assert data["meta"]["card_id"] == root

    # 一正一影：建卡即派生（[卡片存储迁移 SQLite] 影子在库内，不再有 md）
    assert shadow_store._meta_of(wid, root) is not None
    # 序号在工作内独立自增
    assert card_store._next_seq(wid) == 2


# ---------------------------------------------------------------- 权限纯函数

async def test_permission_is_computed_from_tree(typed_registry, work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)         # card_002
    b = await _mk_child(wid, "乙", a)            # card_003（甲的子）
    c = await _mk_child(wid, "丙", root)         # card_004（甲的旁支）
    cards = card_tools._collect_card_meta(wid)

    # 读：[权限底座重做 2026-10-12] 注册类型全授 read，存在即放行（含旁支）；
    # 判据本体已从「policy 位图表」换成「卡上固化的 perms」，行为不变。
    policy.check(a, root, policy.SWITCH_READ, cards)
    policy.check(a, b, policy.SWITCH_READ, cards)
    policy.check(b, root, policy.SWITCH_READ, cards)
    policy.check(root, c, policy.SWITCH_READ, cards)   # 根可读全部
    policy.check(a, c, policy.SWITCH_READ, cards)
    policy.check(c, b, policy.SWITCH_READ, cards)

    # 写：只有自己的子树（edit 属树类开关，叠树作用域），血统链上级也不可写
    policy.check(a, a, policy.SWITCH_EDIT, cards)
    policy.check(a, b, policy.SWITCH_EDIT, cards)
    with pytest.raises(PermissionError):
        policy.check(a, root, policy.SWITCH_EDIT, cards)
    with pytest.raises(PermissionError):
        policy.check(b, a, policy.SWITCH_EDIT, cards)
    with pytest.raises(PermissionError):
        policy.check(a, c, policy.SWITCH_EDIT, cards)

    # 不存在的卡一律无权（判存在在工具层走 not_found；关口层抛 PermissionError）
    with pytest.raises(PermissionError):
        policy.check(a, "card_999", policy.SWITCH_READ, cards)
    with pytest.raises(PermissionError):
        policy.check(a, "card_999", policy.SWITCH_EDIT, cards)


# ---------------------------------------------------------------- 工具层语义

async def test_tool_errors_are_data_not_exceptions(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    c = await _mk_child(wid, "丙", root)
    cards = card_tools._collect_card_meta(wid)

    # [销红 2026-10-12] B 组信封结构裁定：execute_tool 统一返回信封
    # {ok, error{code,message}|None, data}（src/api/card_tools.py:141-142,157,162；
    # execute_tool:1595-1601 对非信封 handler 结果做 ok_() 包装）。
    # [卡片权限改造 2026-10-07] 读侧全盘放开：旁支 read_card 放行（原越权断言退役）
    r = await card_tools.execute_tool(wid, b, "read_card", {"card_id": c}, cards)
    assert r["ok"] is True and r["data"]["card_id"] == c

    # 先判存在再判权限：已删/不存在的卡报「不存在」，不误报越权
    r = await card_tools.execute_tool(wid, b, "read_card", {"card_id": "card_999"}, cards)
    assert "不存在" in r["error"]["message"]

    r = await card_tools.execute_tool(wid, b, "edit_card",
                                      {"card_id": root, "content": "越权改根"}, cards)
    assert "越权" in r["error"]["message"]
    # 越权必须真的没落盘
    # [销红 2026-10-12] C 组卡面字段改名裁定（S2-5）：body 旧三键 → 新四键，
    # 「## 结论区」⇒ status_block（src/storage/card_store.py:938-947；写入侧仍兼容
    # conclusion 旧键 :963，读取侧必须用 status_block）。
    root_body = (await card_store.read_card(wid, root))["body"]["status_block"]
    assert "越权改根" not in root_body

    # [卡片权限改造 2026-10-07] 旁支读影子放行（原越权断言随读限退役；
    # 影子写侧独占不变）——错误即数据性质由下方越权改根用例继续覆盖


async def test_edit_own_subtree_and_shadow_backdoor(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    cards = card_tools._collect_card_meta(wid)

    # [2026-10-27 用户裁定] edit_card 改成**精准编辑**（block + old_string/new_string）：
    # 先写一块做底，再精准改一处——顺带钉住"只动目标块"。
    await card_store.update_card(wid, b, {"status_block": "乙的旧正文"}, "造底")
    # [2026-10-28] 现状改走 edit_status（edit_card 只留使命）
    r = await card_tools.execute_tool(
        wid, a, "edit_status",
        {"card_id": b, "old_string": "乙的旧正文", "new_string": "乙的新正文"}, cards)
    assert r.get("ok") is True
    assert (await card_store.read_card(wid, b))["body"]["status_block"] == "乙的新正文"

    # 影子进同一 meta 字典，但只能由对应正卡经 update_shadow 写
    sid = shadow_store.shadow_id_of(b)
    assert sid in cards
    assert cards[sid]["card_type"] == "shadow"
    assert cards[sid]["parent"] == b

    r = await card_tools.execute_tool(
        wid, a, "edit_card",
        {"card_id": sid, "block": "现状",
         "old_string": "x", "new_string": "黑入影子"}, cards)
    # [销红 2026-10-12] B.4（信封结构 + 影子后门文案实读核实）：_h_edit_card 对影子目标
    # 返回 card_tools.py:327 `err("denied", "影子卡只能由对应正卡经 update_shadow 修改")`
    # ⇒ 信封 r["ok"] is False，message 含 "update_shadow"。
    assert r["ok"] is False and "update_shadow" in r["error"]["message"]


async def test_update_shadow_is_silent_for_canvas(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    c = await _mk_child(wid, "丙", root)
    cards = card_tools._collect_card_meta(wid)

    canvas = card_store.WORKS_DIR / wid / "canvas.json"
    before = canvas.read_bytes()

    body = ("## 进度\n- 已建网\n\n## 产物清单\n- tests/\n\n## 迭代历史\n- v1\n")
    r = await card_tools._h_update_shadow(wid, a, {"content": body}, cards)
    assert r.get("ok") is True
    # 影子写入不得扰动画布缓存（影子不进画布）
    assert canvas.read_bytes() == before

    raw = await shadow_store.read_shadow_raw(wid, a)
    assert raw is not None and "## 进度" in raw and "tests/" in raw
    # 影子写侧恒为当前卡自己的影子——正是「被写入者」是 a
    assert f"shadow_of: \"{a}\"" in raw

    # 读侧跟随 can_read：[卡片权限改造 2026-10-07] 全盘放开——旁支读影子放行
    # （原越权断言随读限机制退役；影子写侧独占不变）
    r = await card_tools.execute_tool(wid, c, "read_shadow", {"card_id": a}, cards)
    # [销红 2026-10-12] C.3（信封结构裁定 + _h_read_shadow 源码实读核实）：
    # card_tools.py:445-449 返回 {owner_card_id, updated_at, content}，
    # 经 execute_tool:1601 ok_() 包装 ⇒ 字段在 r["data"] 下（同 tests/test_cardperm_readopen.py:119 写法）。
    assert r["data"]["owner_card_id"] == a and "## 进度" in r["data"]["content"]
    # 上级可读下级影子（推送到达机制）
    r = await card_tools.execute_tool(wid, root, "read_shadow", {"card_id": a}, cards)
    assert r["data"]["owner_card_id"] == a and "## 进度" in r["data"]["content"]


# ---------------------------------------------------------------- 删除护栏

async def test_delete_guards_and_cascade(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    c = await _mk_child(wid, "丙", root)
    cards = card_tools._collect_card_meta(wid)

    # 剧本根卡不可删
    with pytest.raises(card_store.CardDeleteDeniedError):
        await card_store.delete_card(wid, root)
    # 有子卡的卡不可删（防孤儿）
    with pytest.raises(card_store.CardDeleteDeniedError):
        await card_store.delete_card(wid, a)
    # 工具层：越权删在权限层就被拦
    # [销红 2026-10-12] B.5（信封结构裁定）：err() 信封的 error 是 {code,message}，
    # 断言须落到 ["message"]（card_tools.py:157）。
    r = await card_tools.execute_tool(wid, c, "delete_card", {"card_id": a}, cards)
    assert "越权" in r["error"]["message"]

    # 叶子可删：卡（库行）＋ session（文件）＋ 影子（库行）三件同去
    seq = b.split("_", 1)[1]
    session_file = card_store.WORKS_DIR / wid / "sessions" / f"session_{seq}.md"
    assert session_file.exists() and shadow_store._meta_of(wid, b) is not None

    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": b}, cards)
    # [销红 2026-10-12] 级联修正（修 B.5 后本行才执行到，非红名单条目，但不改则整条仍红）：
    # card_store.delete_card 返回 {"ok": True, "deleted": card_id}——缺 error/data 键，
    # 不满足 _is_envelope，故 execute_tool 再包一层 ok_() ⇒ deleted 在 r["data"] 下。
    assert r.get("ok") is True and r["data"]["deleted"] == b
    assert not card_store.card_exists(wid, b)
    assert shadow_store._meta_of(wid, b) is None
    assert not session_file.exists()
    # 同轮后续工具不得再看到已删卡（缓存同步）
    assert b not in cards


# ---------------------------------------------------------------- 读不改库

async def test_read_card_is_pure(work):
    """[卡片存储迁移 SQLite] 外部修改检测机制已退役（不留 md、不让手改后失去对象）。

    留一条更弱的钉子：纯读不得改库——连读 3 次，修改历史与库版本号都不变。
    """
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    hist_before = (await card_store.read_card(wid, a))["body"]["history"]
    rev_before = card_db.read_rev(wid)
    for _ in range(3):
        data = await card_store.read_card(wid, a)
    assert (await card_store.read_card(wid, a))["body"]["history"] == hist_before
    assert card_db.read_rev(wid) == rev_before
    assert not any("外部修改" in line for line in data["body"]["history"])