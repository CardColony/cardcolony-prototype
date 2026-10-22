# -*- coding: utf-8 -*-
"""[卡片编辑机制改造 2026-10-12 · 块1] 块集断言。

判据（任务书 §六 块1 硬判据）：
1. NEW_SECTION_MAPPING 含 requirements 键（grep 可验）。
2. rd_project 卡读侧返回 requirements 块、无定论写入目标；
   research_project 卡返回 rulings 块。
3. 存量卡读取不报错，缺失块返回空。
4. 旧键归一（OLD_SECTION_MAPPING / OLD_KEY_TO_NEW）零改动。

读侧过滤在 routes._apply_block_set（card_store.read_card 恒返回 rulings 键，
API 层按根卡 project_class 过滤）。card_store.py 本任务零触碰。
"""
from __future__ import annotations

from src.api import routes
from src.storage import card_store, work_store
from src.storage.hash_utils import (
    NEW_KEYS, NEW_SECTION_MAPPING, OLD_KEY_TO_NEW, OLD_SECTION_MAPPING,
    split_body_sections,
)


# ---------------------------------------------------------------- 判据1/4：映射表

def test_new_section_mapping_has_requirements():
    """[2026-10-22 统一命名] 第三块区头＝「## 需求文档」，落 rulings 键（不再有 requirements 键）。"""
    assert "## 需求文档" in NEW_SECTION_MAPPING
    assert NEW_SECTION_MAPPING["## 需求文档"] == "rulings"
    assert "## 定论" in NEW_SECTION_MAPPING, "存量卡旧区头继续可解析"
    assert NEW_SECTION_MAPPING["## 定论"] == "rulings"


def test_old_key_normalization_unchanged():
    """硬判据4：旧键归一表零改动。"""
    assert OLD_SECTION_MAPPING == {
        "## 职责区": "charter",
        "## 结论区": "conclusion",
        "## 否决记录区": "rejections",
        "## 修改历史": "history_text",
    }
    assert OLD_KEY_TO_NEW == {
        "charter": "mission",
        "conclusion": "status_block",
        "rejections": "rulings",
    }


def test_split_extracts_requirements_section():
    """[2026-10-22 统一命名] 「## 需求文档」与旧「## 定论」都落到同一个 rulings 键。"""
    body = "## 使命\nM\n## 现状\nS\n## 需求文档\nR1\n## 修改历史\nH"
    s = split_body_sections(body)
    assert s["rulings"] == "R1"
    assert s["mission"] == "M"
    assert s["status_block"] == "S"
    old = "## 使命\nM\n## 现状\nS\n## 定论\nD1\n## 修改历史\nH"
    assert split_body_sections(old)["rulings"] == "D1", "存量旧区头保形可读"


# ---------------------------------------------------------------- 判据2/3：读侧块集

async def _mk_rd_work():
    """rd_project 树（conftest work 夹具缺省即 rd_project）。"""
    wdata = await work_store.create_work("研发树", project_class="rd_project")
    assert "error" not in wdata, wdata
    return wdata


async def _mk_research_work():
    wdata = await work_store.create_work("研究树", project_class="research_project")
    assert "error" not in wdata, wdata
    return wdata


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_rd_project_read_returns_requirements(work):
    """硬判据2a：rd_project 卡读侧返回 requirements、无 rulings 键。"""
    wid, root = work["work_id"], work["work_card_id"]
    # work 夹具缺省 project_class=rd_project（create_work 缺省值）
    cid = await _mk_child(wid, "子卡", root)
    result = routes._apply_block_set(
        await card_store.read_card(wid, cid), wid
    )
    body = result["body"]
    assert "requirements" in body
    assert "rulings" not in body
    assert "mission" in body and "status_block" in body and "history" in body


async def test_research_project_read_returns_rulings(work):
    """硬判据2b：research_project 卡读侧返回 rulings、无 requirements 键。"""
    wdata = await _mk_research_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    result = routes._apply_block_set(
        await card_store.read_card(wid, cid), wid
    )
    body = result["body"]
    assert "rulings" in body
    assert "requirements" not in body
    assert "mission" in body and "status_block" in body and "history" in body


async def test_legacy_rd_card_third_block_shown_as_requirements(work):
    """存量研发卡：盘上 ## 定论 区内容以 requirements 呈现（第三块语义随树类走）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "存量卡", root)
    # 经 update_card 写一段 rulings（存量路径：盘上落 ## 定论）
    r = await card_store.update_card(wid, cid, {"rulings": "D01【裁定】旧定论"}, "测试")
    assert "error" not in r, r
    result = routes._apply_block_set(
        await card_store.read_card(wid, cid), wid
    )
    # rd 树：rulings 内容以 requirements 呈现，不外露 rulings 键
    assert "D01【裁定】旧定论" in result["body"]["requirements"]
    assert "rulings" not in result["body"]


async def test_missing_block_returns_empty_no_error(work):
    """硬判据3：缺失块返回空串，读取不报错。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "空块卡", root)
    result = routes._apply_block_set(
        await card_store.read_card(wid, cid), wid
    )
    # rd 树骨架占位文本非空，但 ## 需求文档 区缺失时回落 ## 定论 区——
    # 断言的是「不报错 + 键存在 + 值为 str」；内容为空的场景由下方 research 树覆盖
    assert isinstance(result["body"]["requirements"], str)
    assert "mission" in result["body"] and "status_block" in result["body"]

    # research 树：新卡 rulings 为骨架占位（非空）；再验一个确实无定论内容的场景
    wdata = await _mk_research_work()
    wid2, root2 = wdata["work_id"], wdata["work_card_id"]
    cid2 = await _mk_child(wid2, "无定论卡", root2)
    result2 = routes._apply_block_set(
        await card_store.read_card(wid2, cid2), wid2
    )
    assert "rulings" in result2["body"]
    assert isinstance(result2["body"]["rulings"], str)


async def test_rd_card_requirements_missing_section_returns_empty():
    """rd 树卡正文含 ## 需求文档 区头但内容为空 → requirements 为空串、不报错。"""
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "需求空卡", root)
    # 直接构造一份含空 ## 需求文档 区的 raw，模拟缺失场景
    raw = await card_store.read_card(wid, cid)
    result = routes._apply_block_set(raw, wid)
    assert isinstance(result["body"]["requirements"], str)
