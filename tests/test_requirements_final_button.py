"""【需求确定】按钮：一次专用回合，按卡类型注入不同的任务。

[2026-10-29 用户令 · 第一手]
- 「把根卡卡面的【需求确定】按钮，调整为，唤醒根卡，给它这一轮的权限，可以写入需求文档正稿」
- 「因为根卡唤不出评审，也不需要评审，根卡的评审人主要是我」
- 「其他卡这个按钮，也是调起一轮，让模型拟一次草稿，然后进评审」
- 「两边的实现差不多，但注入的提示词不同，流程也有差异」

钉住：
T1 根卡那一轮的工具面有 write_requirements_final；普通回合没有
T2 非根卡拿不到这件工具（即使伪造 op）
T3 根卡写正稿：落 requirements、state=approved、草稿清空
T4 非根卡调 write_requirements_final ⇒ denied
T5 注入按卡类型分两段：根卡＝改正稿；其他卡＝拟草稿后开评审
"""

from __future__ import annotations

import pytest

from src.api import card_tools, current_turn, function_room
from src.storage import card_store, work_store


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH,
                       str(function_room._DEFAULT_PATH))
    function_room.reset_cache()


async def _seed(monkeypatch):
    _use_repo_catalog(monkeypatch)
    w = await work_store.create_work("需求确定按钮")
    wid = str(w["work_id"])
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "子卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    return wid, root, str(sub)


def _face(wid, cid, op=""):
    cards = card_tools._collect_card_meta(wid)
    return {str(t.get("name")) for t in card_tools.build_tool_schemas(
        card_id=cid, work_id=wid, cards=cards, op=op)}


# ---------------------------------------------------------------- T1 / T2

async def test_root_final_turn_gets_final_tool(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    assert "write_requirements_final" not in _face(wid, root), \
        "普通回合不该看到写正稿的工具"
    final = _face(wid, root, op=card_tools.OP_REQUIREMENTS_FINAL)
    assert "write_requirements_final" in final, \
        "根卡的【需求确定】回合应拿到写正稿的工具"
    # 这一轮只有一条写需求的路：草稿工具摘掉（根卡没有评审，写草稿没人能转正）
    assert "edit_requirements" not in final


async def test_sub_card_never_gets_final_tool(sandbox, monkeypatch):
    """其他卡的正稿只能由评审产生——伪造 op 也拿不到这件工具。"""
    wid, root, sub = await _seed(monkeypatch)
    assert "write_requirements_final" not in _face(
        wid, sub, op=card_tools.OP_REQUIREMENTS_FINAL)


# ---------------------------------------------------------------- T3 / T4

async def test_root_writes_final_and_clears_draft(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    await card_store.update_card(wid, root, {"requirements_draft": "旧草稿"},
                                 "预置草稿")
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_write_requirements_final(
        wid, root, {"old_string": "", "new_string": "需求一条：能跑起来"}, cards)
    assert out.get("ok"), out
    card = await card_store.read_card(wid, root)
    assert "需求一条：能跑起来" in str(card["body"]["requirements"])
    assert (card.get("meta") or {}).get("requirements_state") == "approved"
    assert not str((card.get("meta") or {}).get("requirements_draft") or ""), \
        "根卡正稿写完后不该残留草稿"


async def test_sub_card_write_final_denied(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_write_requirements_final(
        wid, sub, {"old_string": "", "new_string": "x"}, cards)
    assert out.get("ok") is False
    assert out["error"]["code"] == "denied"


# ---------------------------------------------------------------- T5

async def test_injection_differs_by_card_type(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    ev_root = current_turn.build_requirements_task_event(wid, root, final=True)
    assert ev_root["type"] == current_turn.TYPE_REQUIREMENTS_FINAL_TASK
    body_root = ev_root["data"]["content"]
    assert "正稿" in body_root and "write_requirements_final" in body_root
    assert "open_review" not in body_root, "根卡不走评审，不该让它开评审"

    ev_sub = current_turn.build_requirements_task_event(wid, sub, final=False)
    assert ev_sub["type"] == current_turn.TYPE_REQUIREMENTS_DRAFT_TASK
    body_sub = ev_sub["data"]["content"]
    assert "草稿" in body_sub and "open_review" in body_sub
    assert "write_requirements_final" not in body_sub

    msg = current_turn.project_requirements_task_message(ev_root["data"])
    assert msg["content"].startswith("〔需求确定〕")


# ---------------------------------------------------------------- 接线

async def test_runner_injects_task_once(sandbox, monkeypatch):
    """message_runner 的接线：op 回合轮首注入一条，本 run 只发一次。"""
    from src.api import message_runner

    wid, root, sub = await _seed(monkeypatch)
    st = message_runner.RunState(work_id=wid, card_id=root, content="需求确定",
                                 op=card_tools.OP_REQUIREMENTS_FINAL, rounds=1)
    message_runner._sync_requirements_task(st)
    assert len(st.turn_tail) == 1
    assert "正稿" in st.turn_tail[0]["content"]
    message_runner._sync_requirements_task(st)
    assert len(st.turn_tail) == 1, "同一 run 不该重复注入"

    st2 = message_runner.RunState(work_id=wid, card_id=sub, content="随便聊聊",
                                  rounds=1)
    message_runner._sync_requirements_task(st2)
    assert st2.turn_tail == [], "普通回合不该注入"


# ---------------------------------------------------------------- 对话里可见

def test_frontend_renders_both_task_events():
    """注入要**直接出现在对话里**：前端两个文件都得认得这两个事件类型，
    否则这一轮在对话流里什么都看不到（只有模型的动作，没有「在干什么」）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    asm = (root / "static" / "esm" / "assembler.js").read_text(encoding="utf-8")
    st = (root / "static" / "stream.js").read_text(encoding="utf-8")
    for t in ("requirements_final_task", "requirements_draft_task"):
        assert f'{t}: {{ kind: "injection" }}' in asm, f"assembler.js 没登记 {t}"
        assert f'case "{t}"' in st, f"stream.js 没渲染 {t}"
