"""影子四节的精准编辑（`edit_shadow`）。

[空节首次写入 2026-10-29] 用户报「笔记节为空写不进」后查出的同源病：
某节空着时没有原文可定位，`old_string` 必须允许空串＝首次写入；
已有内容时空串仍拒（整块覆盖通道有意取消）。

判据：
1. 空节 + old_string="" ⇒ 写入成功
2. 有内容的节 + old_string="" ⇒ 拒（validation）且零改动
3. 写一节不碰其余节
"""

from __future__ import annotations

from src.api import card_tools
from src.storage import card_store, shadow_store, work_store


async def _card():
    w = await work_store.create_work("影子编辑测试")
    wid = str(w["work_id"])
    c = await card_store.create_card(wid, "卡A", "用户直接创建",
                                     card_type="sub_project")
    return wid, str(c["meta"]["card_id"])


async def test_empty_section_writes_first_entry(sandbox):
    wid, cid = await _card()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_shadow(
        wid, cid, {"section": "进度", "old_string": "", "new_string": "开工了"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "empty_insert"
    assert await shadow_store.read_section(wid, cid, "## 进度") == "开工了"


async def test_empty_old_on_nonempty_section_refuses(sandbox):
    wid, cid = await _card()
    await shadow_store.write_section(wid, cid, "## 进度", "已有进度")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_shadow(
        wid, cid, {"section": "进度", "old_string": "", "new_string": "覆盖"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "validation"
    assert await shadow_store.read_section(wid, cid, "## 进度") == "已有进度"


async def test_write_one_section_leaves_others(sandbox):
    wid, cid = await _card()
    await shadow_store.write_section(wid, cid, "## 产物清单", "a.py")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_shadow(
        wid, cid, {"section": "进度", "old_string": "", "new_string": "开工"}, cards)
    assert out.get("ok"), out
    assert await shadow_store.read_section(wid, cid, "## 产物清单") == "a.py"
