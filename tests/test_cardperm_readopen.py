# -*- coding: utf-8 -*-
"""[卡片权限改造 2026-10-07 → 权限收口 2026-10-09] 读侧全盘放开的政策断言。

收口后（THINK-ECO-permgmt 交办单定夺 1/2）：读＝全注册分类授 READ（存在即
放行的现行口径保留）；写＝EDIT/DELETE＋TREE_SCOPE 子树限定（现行口径保留）；
护栏（根卡拒删/有子卡拒删/影子写签名级独占）原样。原直调旧纯函数的断言
（L33-52）改写为统一关口等价断言；execute_tool 断言随 T-0010 信封口径
（成败读 ok 字段、错误读 error.message）。

改前对照组见 _cardperm_20261007/verify_control.py（施工留痕，不入套件）。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, memory_store, shadow_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_v1_full_open_read_nonkin(typed_registry, work):
    """V1 读放开：非亲属/非连线/非锚定的旁支卡可读（注册分类全授 READ）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)   # 与甲互为旁支
    cards = card_tools._collect_card_meta(wid)

    # 关口层：注册分类全授 READ → 不抛即放行；缺卡在关口抛
    # （工具层有存在性先判，走 not_found，关口见不到缺卡）
    policy.check_with_scope(a, b, policy.Perm.READ, cards)
    policy.check_with_scope(b, a, policy.Perm.READ, cards)
    with pytest.raises(PermissionError):
        policy.check_with_scope(a, "card_999", policy.Perm.READ, cards)

    # 工具层：真实走 execute_tool → card_store（T-0010 信封：载荷在 data）
    r = await card_tools.execute_tool(wid, a, "read_card", {"card_id": b}, cards)
    assert r["ok"] is True and r["error"] is None
    assert r["data"]["card_id"] == b and r["data"]["title"] == "乙"


async def test_v2_write_still_restricted(typed_registry, work):
    """V2 写仍受限：越权 edit/delete 明确拒绝且不落盘。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    # 关口层：旁支不可写（TREE_SCOPE 子树限定），自己可写
    with pytest.raises(PermissionError):
        policy.check_with_scope(a, b, policy.Perm.EDIT, cards)
    policy.check_with_scope(a, a, policy.Perm.EDIT, cards)

    before = (await card_store.read_card(wid, b))["body"]
    r = await card_tools.execute_tool(
        wid, a, "edit_card", {"card_id": b, "content": "越权写入"}, cards)
    assert "越权" in r["error"]["message"]
    assert (await card_store.read_card(wid, b))["body"] == before

    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": b}, cards)
    assert "越权" in r["error"]["message"]
    assert card_store._card_path(wid, b).exists()   # 未删


async def test_v3_guards_intact(typed_registry, work):
    """V3 护栏不破：根卡拒删/有子卡拒删/影子写签名级独占（读放开后行为不变）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    cards = card_tools._collect_card_meta(wid)

    # 根卡不可删（工具层身份护栏；收口后前移一行，拒绝文案逐字不变）
    r = await card_tools.execute_tool(wid, root, "delete_card", {"card_id": root}, cards)
    assert "不可删除" in r["error"]["message"]
    # 有子卡的卡不可删（存储层结构护栏；关口 DELETE 对自卡放行后由存储层拒）
    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": a}, cards)
    assert r["ok"] is False
    assert card_store._card_path(wid, a).exists()

    # update_shadow 签名级独占：schema 无 card_id；恶意注入 card_id 也写不出别的目标
    schema = next(t for t in card_tools.build_tool_schemas()
                  if t["name"] == "update_shadow")
    assert "card_id" not in schema["parameters"]["properties"]
    raw_b_before = shadow_store.shadow_path(wid, b).read_bytes()
    body = "## 进度\n- 标记XYZ\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools.execute_tool(
        wid, a, "update_shadow", {"card_id": b, "content": body}, cards)
    assert r.get("ok") is True
    raw_a = shadow_store.shadow_path(wid, a).read_text(encoding="utf-8")
    assert f"shadow_of: \"{a}\"" in raw_a and "标记XYZ" in raw_a
    assert shadow_store.shadow_path(wid, b).read_bytes() == raw_b_before


async def test_v4_shadow_and_memory_read_open(typed_registry, work):
    """V4 影子/记忆读随读放开一并放行；影子写对注册分类放行（定夺 1 偏离位）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    body = "## 进度\n- 甲的影子正文\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools.execute_tool(wid, a, "update_shadow", {"content": body}, cards)
    assert r.get("ok") is True

    # 旁支读影子：放行
    r = await card_tools.execute_tool(wid, b, "read_shadow", {"card_id": a}, cards)
    assert r["ok"] is True
    assert r["data"]["owner_card_id"] == a and "甲的影子正文" in r["data"]["content"]

    # 旁支读记忆：放行
    await memory_store.save_memory(wid, a, {
        "card_id": a, "mark": 1,
        "entries": [{"seq": 0, "conclusion": "【结论】甲结论",
                     "evidence": "【依据】read_card",
                     "occurred_at": "2026-10-07T12:00:00+08:00",
                     "fingerprint": "sha256:ff"}]})
    r = await card_tools.execute_tool(wid, b, "read_memory", {"card_id": a}, cards)
    assert r["ok"] is True
    assert r["data"]["card_id"] == a
    assert r["data"]["entries"][0]["conclusion"] == "【结论】甲结论"
