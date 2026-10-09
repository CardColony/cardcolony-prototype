# -*- coding: utf-8 -*-
"""[主界面优化 E1] compute_card_activity 单测：own/branch 同源、孤儿支、环防护。"""
from __future__ import annotations

from src.storage import activity, card_store, work_store


async def _mk(work, title, parent):
    r = await card_store.create_card(work["work_id"], title, "用户直接创建", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_branch_activity_own_branch_orphan_cycle(sandbox, work, monkeypatch):
    # ★ activity.py 自带 WORKS_DIR 常量（T17 沙箱盲区）：不补丁则本测试读到真实 works。
    # conftest 一字不动（本线改动面纪律），在本测试内补齐铁律一。
    from src.storage import session_store

    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(work, "甲", root)
    b = await _mk(work, "乙", a)        # 甲的子卡
    c = await _mk(work, "丙", root)     # 甲的旁支
    # 乙有会话活跃（写一条会话末条），甲/丙无
    await session_store.append_message(wid, b, "user", "hi")

    m = activity.compute_card_activity(wid)
    # own 口径与 card_last_active 同源逐卡一致
    for cid in (root, a, b, c):
        meta = card_store._meta_of(wid, cid) or {}
        assert m[cid]["own"] == activity.card_last_active(wid, cid, meta.get("updated_at", ""))
    # 分支归并：branch(甲) = max(own甲, own乙)；branch(乙)=own乙
    assert m[a]["branch"] == m[b]["own"]          # 乙比甲新 → 甲的分支被乙顶起
    assert m[b]["branch"] == m[b]["own"]
    assert m[root]["branch"] == m[b]["own"]       # 根卡分支被深层乙顶起（N5 祖先链语义）

    # 孤儿卡（parent 指向已删卡）自成一支：branch=own，不向上归并、不抛错
    o = await _mk(work, "孤", root)
    card_store._card_path(wid, o).write_text(
        card_store._card_path(wid, o).read_text(encoding="utf-8").replace(
            f'parent: "{root}"', 'parent: "card_999"'), encoding="utf-8")
    m2 = activity.compute_card_activity(wid)
    assert m2[o]["branch"] == m2[o]["own"]

    # 环防护：手工把甲↔乙 parent 互指（front-matter 直改），不无限递归、可解
    pa = card_store._card_path(wid, a)
    pb = card_store._card_path(wid, b)
    pa.write_text(pa.read_text(encoding="utf-8").replace(f'parent: "{root}"', f'parent: "{b}"'),
                  encoding="utf-8")
    m3 = activity.compute_card_activity(wid)
    assert m3[a]["branch"] >= m3[a]["own"]
    assert m3[b]["branch"] >= m3[b]["own"]
