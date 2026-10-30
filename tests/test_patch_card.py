# -*- coding: utf-8 -*-
"""卡面精准编辑（[2026-10-27 用户裁定] 「把你编辑卡面的工具迭代了，改成精准编辑」）。

`edit_card` 从「整篇覆盖」改成「old_string → new_string 只改一处」。
钉四条性质：
- 只动指定的一处，**另外两块字节不变**（结构上不可能被清空）
- old_string 找不到 / 不唯一 ⇒ 拒，且**卡面零改动**
- 使命块照样锁（精准编辑不是例外通道）
- 回执带落面后的内容
"""
from __future__ import annotations

from src.api import card_tools
from src.storage import card_store, work_store

MISSION = "把评审换成伴生评审卡机制。"
STATUS = "第一批：后端已落地。\n第二批：前端未做。\n第三批：待验收。"
RULINGS = "C-1 伴生卡随子卡派生。\nC-2 approve 绑版本。\nC-3 全员通过才收口。"


async def _make_card():
    w = await work_store.create_work("精准编辑测试")
    wid = str(w["work_id"])
    root = str(w["work_card_id"])
    r = await card_store.create_card(
        wid, "子项目·精准编辑", "用户直接创建", card_type="sub_project",
        charter=f"## 使命\n{MISSION}\n\n## 现状\n{STATUS}\n\n## 需求文档\n{RULINGS}")
    return wid, root, str(r["meta"]["card_id"])


async def _body(wid: str, cid: str) -> dict:
    return (await card_store.read_card(wid, cid))["body"]


async def test_edit_changes_only_target_spot(sandbox):
    wid, root, cid = await _make_card()
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "现状",
         "old_string": "第二批：前端未做。", "new_string": "第二批：前端三处已开工。"},
        cards)
    assert out.get("ok"), out
    assert out["block"] == "现状" and out["replaced"] == 1

    after = await _body(wid, cid)
    assert "前端三处已开工" in after["status_block"]
    # 另外两块逐字不变 —— 这条就是"结构上不可能被清空"
    assert after["mission"] == before["mission"]
    assert after["requirements"] == before["requirements"]
    # 块内其余行原样
    assert "第一批：后端已落地。" in after["status_block"]
    assert "第三批：待验收。" in after["status_block"]
    # 回执带落面后的内容
    assert out["blocks"]["现状"] == after["status_block"]


async def test_edit_missing_old_refuses_and_touches_nothing(sandbox):
    wid, root, cid = await _make_card()
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "现状",
         "old_string": "这句话卡面上根本没有", "new_string": "x"},
        cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"
    assert await _body(wid, cid) == before, "拒的时候必须零改动"


async def test_edit_ambiguous_refuses(sandbox):
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    await card_store.update_card(
        wid, cid, {"status_block": "重复行\n中间\n重复行"}, "造重复")

    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "现状",
         "old_string": "重复行", "new_string": "改掉"},
        cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "ambiguous"
    assert (await _body(wid, cid))["status_block"] == "重复行\n中间\n重复行"

    # 带更多上下文即可唯一
    out2 = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "现状",
         "old_string": "重复行\n中间", "new_string": "改掉\n中间"},
        cards)
    assert out2.get("ok"), out2
    assert (await _body(wid, cid))["status_block"] == "改掉\n中间\n重复行"


async def test_edit_replace_all(sandbox):
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    await card_store.update_card(wid, cid, {"status_block": "甲\n乙\n甲"}, "造重复")

    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "现状",
         "old_string": "甲", "new_string": "丙", "replace_all": True},
        cards)
    assert out.get("ok"), out
    assert out["replaced"] == 2
    assert (await _body(wid, cid))["status_block"] == "丙\n乙\n丙"


async def test_edit_can_delete_exactly_one_spot(sandbox):
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "需求文档",
         "old_string": "C-2 approve 绑版本。\n", "new_string": ""},
        cards)
    assert out.get("ok"), out
    after = await _body(wid, cid)
    assert after["requirements"] == "C-1 伴生卡随子卡派生。\nC-3 全员通过才收口。"
    assert after["mission"] == MISSION and after["status_block"] == STATUS


async def test_edit_mission_still_locked(sandbox):
    """使命锁对精准编辑同样生效——它不是绕过闸的后门。"""
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "使命",
         "old_string": "把评审换成", "new_string": "把评审彻底换成"},
        cards)
    assert out.get("ok") is False
    assert (await _body(wid, cid))["mission"] == MISSION


async def test_edit_rejects_bad_block_name(sandbox):
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root,
        {"card_id": cid, "block": "D1", "old_string": "x", "new_string": "y"},
        cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "validation"


async def test_edit_rejects_whole_content_call(sandbox):
    """旧的整篇覆盖调用方式（content=）必须被拒——否则误删通道还开着。"""
    wid, root, cid = await _make_card()
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "content": "## 现状\n就一句话"}, cards)
    assert out.get("ok") is False
    assert await _body(wid, cid) == before


# ---------------------------------------------------------------- 按行定位
LINES = "\n".join(["第一行", "第二行", "第三行", "第四行"])


async def _card_with_lines():
    w = await work_store.create_work("按行编辑测试")
    wid = str(w["work_id"])
    root = str(w["work_card_id"])
    r = await card_store.create_card(
        wid, "子项目·按行", "用户直接创建", card_type="sub_project",
        charter="## 使命\n做一件事。\n\n## 现状\n" + LINES + "\n\n## 需求文档\n")
    return wid, root, str(r["meta"]["card_id"])


async def _sb(wid: str, cid: str) -> str:
    return (await card_store.read_card(wid, cid))["body"]["status_block"]


async def test_edit_by_line_replaces_one_line(sandbox):
    wid, root, cid = await _card_with_lines()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "lines": "2", "new_string": "第二行（改）"}, cards)
    assert out.get("ok"), out
    assert out["mode"] == "lines" and out["at_line"] == 2
    assert await _sb(wid, cid) == "第一行\n第二行（改）\n第三行\n第四行"
    assert out["before"] == "第二行", "回执要给被替换的原行，便于核对没改错行"


async def test_edit_by_line_range(sandbox):
    wid, root, cid = await _card_with_lines()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "lines": "2-3", "new_string": "合并成一行"}, cards)
    assert out.get("ok"), out
    assert await _sb(wid, cid) == "第一行\n合并成一行\n第四行"


async def test_edit_by_line_deletes_lines(sandbox):
    wid, root, cid = await _card_with_lines()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "lines": "3", "new_string": ""}, cards)
    assert out.get("ok"), out
    assert await _sb(wid, cid) == "第一行\n第二行\n第四行"


async def test_edit_insert_after_line(sandbox):
    wid, root, cid = await _card_with_lines()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "insert_after": 1, "new_string": "插在第二行前"}, cards)
    assert out.get("ok"), out
    assert await _sb(wid, cid) == "第一行\n插在第二行前\n第二行\n第三行\n第四行"

    out0 = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "insert_after": 0, "new_string": "块首"}, cards)
    assert out0.get("ok"), out0
    assert (await _sb(wid, cid)).startswith("块首\n第一行")


async def test_edit_line_out_of_range_refuses(sandbox):
    wid, root, cid = await _card_with_lines()
    before = await _sb(wid, cid)
    cards = card_store.iter_card_metas(wid)
    for spec in ("0", "5", "3-9", "9-3"):
        out = await card_tools._h_edit_card(
            wid, root, {"card_id": cid, "block": "现状",
                        "lines": spec, "new_string": "x"}, cards)
        assert out.get("ok") is False, spec
        assert out["error"]["code"] == "validation"
        assert await _sb(wid, cid) == before, f"越界必须零改动（{spec}）"

    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "insert_after": 99, "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert await _sb(wid, cid) == before


async def test_edit_rejects_both_locators(sandbox):
    wid, root, cid = await _card_with_lines()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状", "old_string": "第一行",
                    "lines": "1", "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "validation"


# ---------------------------------------------------------------- 标点归一化匹配
# [2026-10-27 用户裁定「按 Codex 的做」] 只解决「长得一样、编码不同」：
# 模型抄原文常把中文标点抄成 ASCII，肉眼一样、逐字节不等。
CN_QUOTE = "他说：\u201c先不动。\u201d"          # 卡面原文：中文弯引号
ASCII_QUOTE = '他说："先不动。"'                  # 模型抄成 ASCII 引号（冒号仍是全角）
CN_SPACE = "第一行\u3000缩进"                     # 全角空格
ASCII_SPACE = "第一行 缩进"


async def _card_with(text: str):
    w = await work_store.create_work("归一化测试")
    wid = str(w["work_id"])
    root = str(w["work_card_id"])
    r = await card_store.create_card(
        wid, "子项目·归一化", "用户直接创建", card_type="sub_project",
        charter="## 使命\n做一件事。\n\n## 现状\n" + text + "\n\n## 需求文档\n")
    return wid, root, str(r["meta"]["card_id"])


async def test_normalized_match_when_punctuation_differs(sandbox):
    """中文引号 vs ASCII 引号：命中，但**写回用卡面原文**，回执标明 normalized。"""
    wid, root, cid = await _card_with(CN_QUOTE)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": ASCII_QUOTE, "new_string": "他说：先不动。"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "normalized"
    assert out["before"] == CN_QUOTE, "回执必须给出卡面上的真实原文"
    assert "归一化" in out.get("note", "")
    assert (await _body(wid, cid))["status_block"] == "他说：先不动。"


async def test_normalized_match_fullwidth_space(sandbox):
    wid, root, cid = await _card_with(CN_SPACE)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": ASCII_SPACE, "new_string": "第一行 已改"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "normalized"
    assert (await _body(wid, cid))["status_block"] == "第一行 已改"


async def test_exact_match_not_flagged(sandbox):
    wid, root, cid = await _card_with(CN_QUOTE)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": CN_QUOTE, "new_string": "改掉"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "exact"
    assert "note" not in out


async def test_normalized_still_refuses_when_not_found(sandbox):
    """归一化只救「标点编码不同」，差字照样拒。"""
    wid, root, cid = await _card_with(CN_QUOTE)
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": '他说："先别动。"', "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"
    assert await _body(wid, cid) == before


async def test_normalized_ambiguous_still_refused(sandbox):
    wid, root, cid = await _card_with(CN_QUOTE + "\n" + CN_QUOTE)
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": ASCII_QUOTE, "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "ambiguous"
    assert await _body(wid, cid) == before


async def test_normalized_fullwidth_colon(sandbox):
    """全角冒号 vs 半角冒号——中文卡面上最常见的混用，也要能命中。"""
    wid, root, cid = await _card_with("状态：已完成")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": "状态:已完成", "new_string": "状态：已验收"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "normalized"
    assert out["before"] == "状态：已完成", "回执给的是卡面真实原文"
    assert (await _body(wid, cid))["status_block"] == "状态：已验收"


async def test_normalized_does_not_map_cjk_brackets(sandbox):
    """「」与 ASCII 引号字形不同，不当作同一处——宁拒不错。"""
    wid, root, cid = await _card_with("他说「先不动」")
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": '他说"先不动"', "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"
    assert await _body(wid, cid) == before


async def test_normalized_fullwidth_colon(sandbox):
    """全角冒号 vs 半角冒号——中文卡面上最常见的混用，也要能命中。"""
    wid, root, cid = await _card_with("状态：已完成")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": "状态:已完成", "new_string": "状态：已验收"}, cards)
    assert out.get("ok"), out
    assert out["matched"] == "normalized"
    assert out["before"] == "状态：已完成", "回执给的是卡面真实原文"
    assert (await _body(wid, cid))["status_block"] == "状态：已验收"


async def test_normalized_does_not_map_cjk_brackets(sandbox):
    """「」与 ASCII 引号字形不同，不当作同一处——宁拒不错。"""
    wid, root, cid = await _card_with("他说「先不动」")
    before = await _body(wid, cid)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_card(
        wid, root, {"card_id": cid, "block": "现状",
                    "old_string": '他说"先不动"', "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"
    assert await _body(wid, cid) == before
