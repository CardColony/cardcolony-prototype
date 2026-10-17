# -*- coding: utf-8 -*-
"""收口三件序单测（迭代二 S3 原型；[思维链真实落地 2026-10-11 笔 3] 重写）。

收口三件序＝①final 块落档（收口终态＋计划名＋各步成果×判据摘要）
②终态标记（thinkchain_final 进卡面 front matter，孤儿扫描判定源）
③清理（plan 文件删＋内存摘除）。卡面 status_block 结论回写已挪至提交轮
统合完成后（message_runner.run_seats._write_chain_summary，由提交轮 e2e
覆盖）；产物核对位随步产物位字段退役。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.storage import card_store, session_facade, session_store  # noqa: F401
from src.thinkchain import board as board_mod
from src.thinkchain import finalize as finalize_mod

# ===========================================================================
# [纯单模型模式 2026-10-17 · 用户令] 本模块用例全为思维链行为（判型/拆解/上板/
# 步推进/三席统合/步事件）。生产已把思维链拆出链路（thinkchain.ENABLED=False）
# ⇒ 整模块跳过；开关置 True 即全部自动恢复（判据同并行线逐函数 skipif，此处
# 收敛为模块级，语义不变）。
# ===========================================================================
pytestmark = pytest.mark.skipif(
    not __import__("src.thinkchain", fromlist=["enabled"]).enabled(),
    reason="纯单模型模式（2026-10-17 用户令）：思维链已拆出生产链路",
)


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def _steps(*prompts: str) -> list[dict]:
    return [{"id": f"s{i + 1}", "prompt": p, "criteria": f"{p}的判据",
             "deliverable": f"{p}的产出", "type": "execute"}
            for i, p in enumerate(prompts)]


async def _setup_board_two_steps(work_id: str, card_id: str) -> None:
    """两步板：推完第一步（result 落板）。"""
    r = await board_mod.create_board(work_id, card_id, "报告计划", _steps("写报告", "交付"))
    assert r["ok"], r
    b = await board_mod.get_board(work_id, card_id)
    await b.mark_done_and_advance("第一步报告已写出")


async def test_finalize_three_step_sequence(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _setup_board_two_steps(wid, cid)

    note = await finalize_mod.run_finalize(wid, cid, "报告已完成并交付")
    assert "收口完成" in note

    # ② 卡面终态键（孤儿扫描判定源；[卡片存储迁移 SQLite] 在库列 thinkchain_final）
    assert "thinkchain_final" in (card_store._meta_of(wid, cid) or {})

    # ① final 块落档（收口终态＋计划名＋各步成果×判据）
    blocks = await session_facade.read(wid, cid)
    fb = [b for b in blocks if b.get("behavior") == "final"]
    assert fb and "已收口" in fb[-1]["content"] and "报告计划" in fb[-1]["content"]
    assert "成果" in fb[-1]["content"] and "判据" in fb[-1]["content"]
    assert "第一步报告已写出" in fb[-1]["content"]

    # ③ 清理：plan 文件删、内存摘除
    assert not board_mod.plan_path(wid, cid).exists()
    assert not board_mod.has_live_board(wid, cid)

    # ops 留痕：plan_finalize ＋ plan_cleanup
    ops = session_store.WORKS_DIR / "_ops" / "session_ops.jsonl"
    actions = [json.loads(l)["action"] for l in ops.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "plan_finalize" in actions and "plan_cleanup" in actions


async def test_finalize_digest_covers_all_steps(work):
    """final 块摘要覆盖全部步（含未推进步）——提交轮三席质询的档案面。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await _setup_board_two_steps(wid, cid)
    await finalize_mod.run_finalize(wid, cid, "结论X")
    blocks = await session_facade.read(wid, cid)
    fb = [b for b in blocks if b.get("behavior") == "final"]
    content = fb[-1]["content"]
    assert "步 s1 [done]" in content and "写报告" in content
    assert "步 s2 [pending]" in content and "交付" in content


async def test_finalize_when_board_gone_is_plain(work):
    wid, cid = work["work_id"], work["work_card_id"]
    note = await finalize_mod.run_finalize(wid, cid, "x")  # 无板：明文，不抛
    assert "收口未执行" in note


async def test_step_complete_last_step_triggers_finalize_end_to_end(work):
    """集成：step_complete 末步 → 自动收口三件序（钉死自动，无手动工具）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    from src.api.card_tools import execute_tool
    from src.storage import card_store
    # [工具面补齐 2026-10-12] 思维链六件有 plan 开关的执行层关口，空卡表会被
    # 默认关门拒掉。传真实卡表（该卡按当前配置建，perms 含 plan）。
    cards = card_store.iter_card_metas(wid)
    r = await execute_tool(wid, cid, "create_plan",
                           {"name": "集成计划",
                            "steps": [{"id": "s1", "prompt": "做事", "criteria": "做事判据",
                                       "deliverable": "做事产出", "type": "execute"}]},
                           cards)
    assert r["ok"], r
    r2 = await execute_tool(wid, cid, "step_complete",
                            {"result": "事情做完，结论是Y", "criteria_met": "判据满足"}, cards)
    assert r2["ok"] and r2["data"]["all_steps_done"] is True
    assert "收口完成" in r2["data"]["message"]
    # 收口三件全部在盘：终态键＋final 块＋板清理
    assert "thinkchain_final" in (card_store._meta_of(wid, cid) or {})
    assert not board_mod.plan_path(wid, cid).exists()
    blocks = await session_facade.read(wid, cid)
    assert [b for b in blocks if b.get("behavior") == "final"]
