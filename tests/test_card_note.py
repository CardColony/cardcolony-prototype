"""卡片笔记：影子第五区。

用户 2026-10-27：「给每张卡片的影子卡卡面，增加一个 note 区域，以及记笔记的工具
（精准编辑不要整块覆盖）。note 的内容常驻卡片上下文，但其他卡片不能读取。」

四条判据：
1. 笔记只动 `shadows.note` 一列，不碰四节
2. `edit_note` 是精准编辑（找不到拒／多处拒／归一化命中）
3. 笔记常驻 150 区（【笔记】节）
4. **别的卡读不到**：`read_linked(part=影子)` 剥掉笔记；`read_shadow` 跨卡同样剥
   （[2026-11-01 泄漏修复]）；本卡 `read_shadow` 仍拿得到
"""

from __future__ import annotations

from src.api import card_tools, context_regions
from src.storage import card_store, shadow_store, work_store


async def _pair():
    """A、B 两张卡，A 沿通信线连到 B。"""
    w = await work_store.create_work("笔记测试")
    wid = str(w["work_id"])
    a = await card_store.create_card(wid, "读方A", "用户直接创建",
                                     card_type="sub_project")
    b = await card_store.create_card(wid, "写方B", "用户直接创建",
                                     card_type="sub_project")
    aid, bid = str(a["meta"]["card_id"]), str(b["meta"]["card_id"])
    await card_store.update_card(
        wid, aid, {"links": [{"target": bid, "type": "关联"}]}, "建通信线")
    return wid, aid, bid


# ---------------------------------------------------------------- 存储
async def test_note_is_its_own_column(sandbox):
    """笔记只写 note 列，四节一个字不动。"""
    wid, aid, bid = await _pair()
    await shadow_store.update_shadow(
        wid, bid, "## 进度\n\n做到第二步\n\n## 待协作事项\n\n无\n\n"
                  "## 产物清单\n\n无\n\n## 迭代历史\n\n无\n")
    before = await shadow_store.read_shadow_raw(wid, bid)
    await shadow_store.write_note(wid, bid, "第一条笔记")
    after = await shadow_store.read_shadow_raw(wid, bid)
    assert "第一条笔记" in after
    # 四节内容不变（除了多出的笔记节）
    for head in ("## 进度", "## 待协作事项", "## 产物清单", "## 迭代历史"):
        assert before.count(head) == after.count(head)
    assert "做到第二步" in after


# ---------------------------------------------------------------- 精准编辑
async def test_edit_note_replaces_one_spot(sandbox):
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "第一行\n第二行\n第三行")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_note(
        wid, bid, {"old_string": "第二行", "new_string": "第二行（改）"}, cards)
    assert out.get("ok"), out
    assert out["at_line"] == 2
    assert await shadow_store.read_note(wid, bid) == "第一行\n第二行（改）\n第三行"


async def test_edit_note_not_found_refuses(sandbox):
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "只有这一行")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_note(
        wid, bid, {"old_string": "不存在的", "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"
    assert await shadow_store.read_note(wid, bid) == "只有这一行", "拒了就不许改"


async def test_edit_note_ambiguous_refuses(sandbox):
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "重复\n重复")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_note(
        wid, bid, {"old_string": "重复", "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "ambiguous"
    assert await shadow_store.read_note(wid, bid) == "重复\n重复"


async def test_edit_note_delete_a_line(sandbox):
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "甲\n乙\n丙")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_note(
        wid, bid, {"old_string": "乙\n", "new_string": ""}, cards)
    assert out.get("ok"), out
    assert await shadow_store.read_note(wid, bid) == "甲\n丙"


async def test_edit_note_empty_writes_first_entry(sandbox):
    """笔记空着＝没有原文可定位：old_string 给空串即首次写入。

    [2026-10-29 用户令] 改前空串一律被拒，而整块覆盖通道已退役 ⇒ 空笔记永远写不进。
    """
    wid, aid, bid = await _pair()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_note(
        wid, bid, {"old_string": "", "new_string": "第一笔笔记"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "empty_insert"
    assert await shadow_store.read_note(wid, bid) == "第一笔笔记"


async def test_edit_note_empty_old_on_nonempty_refuses(sandbox):
    """已有内容时给空串＝想整块覆盖，那条通道有意取消：拒且零改动。"""
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "已有内容")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_note(
        wid, bid, {"old_string": "", "new_string": "覆盖"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "validation"
    assert await shadow_store.read_note(wid, bid) == "已有内容"


# ---------------------------------------------------------------- 常驻上下文
async def test_note_renders_in_current_card_region(sandbox):
    """笔记要出现在 150 区（常驻），且排在产物清单之前。"""
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "这是B的私密笔记")
    meta = card_store.iter_card_metas(wid)
    from src.api import context_assembler
    body, shadow = context_assembler._load_current_card_state(wid, bid)
    ctx = context_regions.RegionCtx(
        work_id=wid, card_id=bid, cur=meta.get(bid),
        cur_body=body, cur_shadow=shadow,
    )
    text = context_regions.render_current_card_region(ctx)
    assert "【笔记】" in text and "这是B的私密笔记" in text
    assert "### 影子（本卡私有，别的卡读不到）" in text


# ---------------------------------------------------------------- 别人读不到
async def test_other_card_cannot_read_note_via_read_linked(sandbox):
    """**关键判据**：A 沿通信线读 B 的影子，拿不到 B 的笔记。"""
    wid, aid, bid = await _pair()
    await shadow_store.update_shadow(
        wid, bid, "## 进度\n\n做到第二步\n\n## 待协作事项\n\n无\n\n"
                  "## 产物清单\n\n无\n\n## 迭代历史\n\n无\n")
    await shadow_store.write_note(wid, bid, "B 的私密笔记：不要外传")

    r = await card_tools.read_via(wid, aid, bid, "影子")
    assert r.get("ok"), r
    value = str(r["value"])
    assert "B 的私密笔记" not in value, "笔记被别的卡读到了"
    assert "## 笔记" not in value, "笔记节被别的卡看到了"
    assert "做到第二步" in value, "非私密节应该照常可读"


async def test_own_card_still_reads_own_note(sandbox):
    """本卡读自己的影子，笔记照常可见。"""
    wid, aid, bid = await _pair()
    await shadow_store.write_note(wid, bid, "只有我能看")
    raw = await shadow_store.read_shadow_raw(wid, bid)
    assert "只有我能看" in raw and "## 笔记" in raw


async def test_other_card_cannot_read_note_via_read_shadow(sandbox):
    """[笔记泄漏修复 2026-11-01] `read_shadow` 跨卡路径同样剥笔记：

    父卡沿父子线读子卡影子拿不到「## 笔记」节（修前 include_private 恒 True，
    与 read_via 口径不一、父卡能读到子卡笔记）；本卡自读仍是全文。
    """
    w = await work_store.create_work("笔记测试")
    wid = str(w["work_id"])
    p = await card_store.create_card(wid, "父卡P", "用户直接创建",
                                     card_type="sub_project")
    pid = str(p["meta"]["card_id"])
    c = await card_store.create_card(wid, "子卡C", "用户直接创建",
                                     card_type="sub_project", parent=pid)
    cid = str(c["meta"]["card_id"])
    await shadow_store.update_shadow(
        wid, cid, "## 进度\n\n做到第二步\n\n## 待协作事项\n\n无\n\n"
                  "## 产物清单\n\n无\n\n## 迭代历史\n\n无\n")
    await shadow_store.write_note(wid, cid, "C 的私密笔记：父卡也不给")

    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_read_shadow(wid, pid, {"card_id": cid}, cards)
    assert "error" not in out, out
    assert "C 的私密笔记" not in out["content"], "笔记被父卡读到了"
    assert "## 笔记" not in out["content"], "笔记节被父卡看到了"
    assert "做到第二步" in out["content"], "非私密节应该照常可读"

    own = await card_tools._h_read_shadow(wid, cid, {"card_id": cid}, cards)
    assert "error" not in own, own
    assert "C 的私密笔记" in own["content"], "本卡自读应含笔记"
