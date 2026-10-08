# -*- coding: utf-8 -*-
"""[卡片权限改造 2026-10-07] 读侧全盘放开的新政策断言（任务书 V1-V4）。

改策：can_read 存在即放行——任一卡可读全项目任何卡；写侧不随读放开
（can_edit「自己＋子树」一字不动）；护栏（根卡拒删/有子卡拒删/影子写
签名级独占）原样。

改前对照组（旧判据拒绝同场景）见 _cardperm_20261007/verify_control.py
——施工留痕目录，不入测试套件；本文件只钉新政策。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_store, memory_store, shadow_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_v1_full_open_read_nonkin(work):
    """V1 读放开：非亲属/非连线/非锚定的旁支卡可读（改前旧判据必拒的场景）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)   # 与甲互为旁支：无子树/血统/连线/锚定关系
    cards = card_tools._collect_card_meta(wid)

    # 纯函数层：存在即放行；不存在是唯一拒因
    assert card_tools.can_read(a, b, cards)
    assert card_tools.can_read(b, a, cards)
    assert not card_tools.can_read(a, "card_999", cards)

    # 工具层：真实走 execute_tool → card_store
    r = await card_tools.execute_tool(wid, a, "read_card", {"card_id": b}, cards)
    assert "error" not in r
    assert r["card_id"] == b and r["title"] == "乙"


async def test_v2_write_still_restricted(work):
    """V2 写仍受限：越权 edit/delete 明确拒绝且不落盘。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    assert not card_tools.can_edit(a, b, cards)   # 旁支不可写（写侧未随读放开）
    assert card_tools.can_edit(a, a, cards)       # 自己可写

    before = (await card_store.read_card(wid, b))["body"]["status_block"]
    r = await card_tools.execute_tool(
        wid, a, "edit_card", {"card_id": b, "content": "越权写入"}, cards)
    assert "越权" in r["error"]
    assert (await card_store.read_card(wid, b))["body"]["status_block"] == before

    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": b}, cards)
    assert "越权" in r["error"]
    assert card_store._card_path(wid, b).exists()   # 未删


async def test_v3_guards_intact(work):
    """V3 护栏不破：根卡拒删/有子卡拒删/影子写签名级独占（读放开后行为不变）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    cards = card_tools._collect_card_meta(wid)

    # 剧本根卡不可删（工具层显式拦截）
    r = await card_tools.execute_tool(wid, root, "delete_card", {"card_id": root}, cards)
    assert "不可删除" in r["error"]
    # 有子卡的卡不可删（存储层结构护栏）
    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": a}, cards)
    assert "error" in r
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


async def test_v4_shadow_and_memory_read_open(work):
    """V4 影子/记忆读随 can_read 一并放开；影子写独占不破（见 V3）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    body = "## 进度\n- 甲的影子正文\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools.execute_tool(wid, a, "update_shadow", {"content": body}, cards)
    assert r.get("ok") is True

    # 旁支读影子：放行
    r = await card_tools.execute_tool(wid, b, "read_shadow", {"card_id": a}, cards)
    assert "error" not in r
    assert r["owner_card_id"] == a and "甲的影子正文" in r["content"]

    # 旁支读记忆：放行
    await memory_store.save_memory(wid, a, {
        "card_id": a, "mark": 1,
        "entries": [{"seq": 0, "conclusion": "【结论】甲结论",
                     "evidence": "【依据】read_card",
                     "occurred_at": "2026-10-07T12:00:00+08:00",
                     "fingerprint": "sha256:ff"}]})
    r = await card_tools.execute_tool(wid, b, "read_memory", {"card_id": a}, cards)
    assert "error" not in r
    assert r["card_id"] == a
    assert r["entries"][0]["conclusion"] == "【结论】甲结论"
