# -*- coding: utf-8 -*-
"""收口四件序单测（迭代二 S3 验收）。

任务书 S3 验收：finalize 三件序单测＋产物命名核对。四件序＝①结论回写
卡面（计划名＋终态＋结论＋产物指针四样，status_block 区）②产物核对落库
（has_artifact 步核对文件、未声明不强造）③final 块落档 ④S1 终态标记＋
清理。产物命名核对＝thinkchain/{card}/{seq:02d}-{step_id}.md。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.storage import session_facade, session_store  # noqa: F401
from src.thinkchain import board as board_mod
from src.thinkchain import finalize as finalize_mod
from src.thinkchain import plan_tools


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


async def _setup_board_with_artifact(work_id: str, card_id: str) -> None:
    """两步板：第一步声明产物（真落一个文件），第二步普通。推完第一步。"""
    r = await board_mod.create_board(work_id, card_id, "报告计划",
                                     [{"id": "make", "prompt": "写报告", "type": "execute", "has_artifact": True},
                                      {"id": "deliver", "prompt": "交付", "type": "think"}])
    assert r["ok"], r
    b = await board_mod.get_board(work_id, card_id)
    ret = await b.save_artifact_async("make", "# 报告正文")
    assert ret["ok"]
    path = board_mod.artifact_path(work_id, card_id, 1, "make")
    for _ in range(300):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    assert path.exists()
    # 等 has_artifact 置位刷回
    pp = board_mod.plan_path(work_id, card_id)
    for _ in range(300):
        data = json.loads(pp.read_text(encoding="utf-8"))
        if data["steps"][0]["has_artifact"]:
            break
        await asyncio.sleep(0.01)
    await b.mark_done_and_advance("第一步报告已写出")


async def test_finalize_four_step_sequence(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _setup_board_with_artifact(wid, cid)

    note = await finalize_mod.run_finalize(wid, cid, "报告已完成并交付")
    assert "收口完成" in note

    card_file = session_store.WORKS_DIR / wid / "cards" / f"{cid}.md"
    card_text = card_file.read_text(encoding="utf-8")
    # ① 卡面四样：计划名／终态／结论／产物指针（status_block 区）
    assert "计划名: 报告计划" in card_text
    assert "终态: 已收口" in card_text
    assert "结论: 报告已完成并交付" in card_text
    # 产物命名核对：thinkchain/{card}/{seq:02d}-{step_id}.md
    expected = board_mod.artifact_path(wid, cid, 1, "make")
    assert f"产物:\n- 步 make: {expected}（在库）" in card_text
    # ④ 卡面终态键（孤儿扫描判定源）
    assert "thinkchain_final" in card_text

    # ③ final 块落档（结论＋产物清单）
    blocks = await session_facade.read(wid, cid)
    fb = [b for b in blocks if b.get("behavior") == "final"]
    assert fb and "报告已完成并交付" in fb[-1]["content"] and "产物清单" in fb[-1]["content"]

    # ④ 清理：plan 文件删、内存摘除
    assert not board_mod.plan_path(wid, cid).exists()
    assert not board_mod.has_live_board(wid, cid)
    # 产物档案目录**不在清理范围**（模型产物是档案）
    assert expected.exists()

    # ops 留痕：plan_finalize ＋ plan_cleanup
    ops = session_store.WORKS_DIR / "_ops" / "session_ops.jsonl"
    actions = [json.loads(l)["action"] for l in ops.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "plan_finalize" in actions and "plan_cleanup" in actions


async def test_finalize_missing_artifact_recorded_not_fabricated(work):
    """未声明/缺失的不强造：has_artifact=True 但文件被删 → 如实记「缺失」。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await _setup_board_with_artifact(wid, cid)
    # 删掉产物文件制造缺失
    board_mod.artifact_path(wid, cid, 1, "make").unlink()

    note = await finalize_mod.run_finalize(wid, cid, "结论")
    assert "收口完成" in note
    card_text = (session_store.WORKS_DIR / wid / "cards" / f"{cid}.md").read_text(encoding="utf-8")
    assert "缺失（未声明产物不强造）" in card_text


async def test_finalize_without_artifact_steps(work):
    """无声明产物的板：产物清单如实写「无声明产物的步」，不补造文件。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "n", [{"id": "only", "prompt": "a", "type": "execute"}])
    b = await board_mod.get_board(wid, cid)
    await b.mark_done_and_advance("完事")
    note = await finalize_mod.run_finalize(wid, cid, "结论X")
    assert "产物 0 件核对" in note
    card_text = (session_store.WORKS_DIR / wid / "cards" / f"{cid}.md").read_text(encoding="utf-8")
    assert "（无声明产物的步）" in card_text


async def test_finalize_when_board_gone_is_plain(work):
    wid, cid = work["work_id"], work["work_card_id"]
    note = await finalize_mod.run_finalize(wid, cid, "x")  # 无板：明文，不抛
    assert "收口未执行" in note


async def test_step_complete_last_step_triggers_finalize_end_to_end(work):
    """集成：step_complete 末步 → 自动收口四件序（钉死自动，无手动工具）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    from src.api.card_tools import execute_tool
    r = await execute_tool(wid, cid, "create_plan",
                           {"name": "集成计划", "steps": [{"id": "s1", "prompt": "做事", "type": "execute"}]},
                           {})
    assert r["ok"], r
    r2 = await execute_tool(wid, cid, "step_complete", {"result": "事情做完，结论是Y"}, {})
    assert r2["ok"] and r2["data"]["all_steps_done"] is True
    assert "收口完成" in r2["data"]["message"]
    # 收口四件全部在盘
    card_text = (session_store.WORKS_DIR / wid / "cards" / f"{cid}.md").read_text(encoding="utf-8")
    assert "结论: 事情做完，结论是Y" in card_text
    assert "thinkchain_final" in card_text
    assert not board_mod.plan_path(wid, cid).exists()
    blocks = await session_facade.read(wid, cid)
    assert [b for b in blocks if b.get("behavior") == "final"]
