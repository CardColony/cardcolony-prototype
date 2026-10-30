# -*- coding: utf-8 -*-
"""职能卡的信息结构必须和普通卡区分开（2026-10-23 用户令）。

用户原话：「为什么你作的职能卡，卡片信息结构和普通卡是一样的？你不看角色卡长什么样的吗？」

**故障**：`ensure_function_card` 建卡时**一个块都不传** ⇒ 落
`card_store.NEW_CARD_SKELETON` 的通用占位模板：
    ## 使命    （本卡作用/目标、授权来源；≤5行；立项定稿，改动需用户确认）
    ## 现状    （当前状况快照：做到哪、什么状态、卡在哪…）
    ## 需求文档（本卡全部需求的确定版…）
⇒ 职能卡卡面和普通项目卡一模一样，看不出它是职能。

**角色卡不是这么建的**（`role_room`）：建卡时显式拼 body——
    使命＝职责书全文 / 现状＝「值班中；判词与警报见角色档案」/ 需求文档＝空

**修法**：`ensure_function_card` 改调 `_function_card_body()`，按角色卡同一形态拼：
    使命＝职能档案里的职责书（charter.text）
    现状＝「待命；判断、警报与裁定见职能档案」
    需求文档＝空（职能的裁定落在档案 verdicts，不写卡面）

判据：
F1 使命＝职责书全文（不是占位模板）
F2 现状＝职能专用文案（不是普通卡的"当前状况快照"占位）
F3 需求文档留空
F4 三个块都不含普通卡的占位模板字样
F5 档案里没职责书时，给的是"职责书待补"的明确提示，**不是**普通卡占位模板
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_store, function_store

PLACEHOLDER_PREFIX = "（本卡作用/目标、授权来源"

CHARTER = "【身份定位】\n我是本项目的架构评审人。我只指出缺陷，不批准设计。"


async def _mk_function(work, charter: str = CHARTER) -> tuple[str, str]:
    """建一个职能（含档案），返回 (work_id, function_id)。"""
    wid = work["work_id"]
    prof = {
        "jurisdiction": {"domain": "arch", "statement": "守架构自洽"},
        "watch": {"cards": [], "signals": ["turn_completed"]},
        "speak_contract": {"advice_when": ["契约互斥"]},
        "escalation": {"advice_to": "", "alarm_to": "", "critical_to": ""},
        "charter": {"version": 1, "text": charter, "ratified_by": "test"},
    }
    created = await function_store.create_function(wid, "架构评审人", prof)
    fid = str(created.get("function_id") or "")
    assert fid, created
    return wid, fid


@pytest.mark.asyncio
async def test_function_card_mission_is_charter_not_placeholder(work):
    """F1：使命＝职责书全文。"""
    wid, fid = await _mk_function(work)
    await card_tools.ensure_function_card(wid, fid, "架构评审人")

    card = await card_store.read_card(wid, fid)
    mission = str((card.get("body") or {}).get("mission") or "")
    assert mission == CHARTER.strip(), f"使命必须是职责书全文，实际：{mission[:80]!r}"


@pytest.mark.asyncio
async def test_function_card_status_is_function_specific(work):
    """F2/F3：现状是职能文案，需求文档留空。"""
    wid, fid = await _mk_function(work)
    await card_tools.ensure_function_card(wid, fid, "架构评审人")

    body = (await card_store.read_card(wid, fid)).get("body") or {}
    status = str(body.get("status_block") or "")
    requirements = str(body.get("requirements") or "")
    assert "职能档案" in status, f"现状应指向职能档案，实际：{status!r}"
    assert "当前状况快照" not in status, "不许落普通卡的现状占位"
    assert requirements.strip() == "", f"需求文档应留空，实际：{requirements[:60]!r}"


@pytest.mark.asyncio
async def test_function_card_has_no_generic_placeholders(work):
    """F4：三个块都不含普通卡占位模板字样。"""
    wid, fid = await _mk_function(work)
    await card_tools.ensure_function_card(wid, fid, "架构评审人")

    body = (await card_store.read_card(wid, fid)).get("body") or {}
    for key in ("mission", "status_block", "requirements"):
        val = str(body.get(key) or "")
        assert PLACEHOLDER_PREFIX not in val, f"{key} 落进了普通卡占位模板"
        assert "本卡全部需求的确定版" not in val, f"{key} 落进了普通卡占位模板"


@pytest.mark.asyncio
async def test_missing_charter_gives_explicit_hint(work):
    """F5：档案没职责书时给明确提示，而不是普通卡占位模板。"""
    wid, fid = await _mk_function(work, charter="")
    await card_tools.ensure_function_card(wid, fid, "架构评审人")

    mission = str((await card_store.read_card(wid, fid)).get("body", {}).get("mission") or "")
    assert "职责书待补" in mission, f"缺职责书要给明确提示，实际：{mission[:80]!r}"
    assert PLACEHOLDER_PREFIX not in mission


@pytest.mark.asyncio
async def test_body_builder_matches_role_card_shape(work):
    """F1-F3 的根：`_function_card_body` 与角色卡 body 同形态（三块区头一致）。"""
    wid, fid = await _mk_function(work)
    body = await card_tools._function_card_body(wid, fid)
    assert body.startswith("## 使命\n")
    assert "\n\n## 现状\n" in body
    assert body.rstrip().endswith("## 需求文档")
