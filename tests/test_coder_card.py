# -*- coding: utf-8 -*-
"""[编程卡 2026-10-15 · §六块2/块3] 编程卡全部新测试（任务书 §四 白名单件）。

覆盖：
- 判据5/块1 硬判据：工具集指纹（grep 19 / tools-tc-6 / t21-1）、开关挂接、
  coder perms 恰 9、search/list_memory 预留机关在册；
- 判据1/2：工具面分化（coder 见 report_done 不见 create_card 族；主卡见
  create_coder_card 不见 report_done——第三段 card_type 闸）；
- 判据3：report_done 双保险（非 coder 拒）＋完工三件套（completed/coder_final
  落库/影子完工行/owner 收件箱 coder_done/回执 msg_id）；
- 判据4：create_coder_card 派生即调起（卡面/使命块/perms=4 值/首跑 run_start）；
- 块3：完工回执调起主卡验收 → S4 自动回发 answer 落编程卡收件箱留档、
  编程卡**无新回合**（K3 跳过＋信仍 pending）；completed coder POST /messages
  → 400；编程卡不可删。

注册表：本套件**自指真实** config/card_type_registry.json（含 coder）——
tests/_fixtures 副本属 §四 白名单外文件，零触碰。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from src.api import (
    card_msg, card_tools, event_stream, message_runner, model_service, prompts, routes,
)
from src.permissions import policy
from src.storage import card_db, card_store, session_facade, shadow_store

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def coder_registry(monkeypatch):
    """编程卡词表走真实配置（config/card_type_registry.json 含 coder）。"""
    monkeypatch.setattr(
        card_store, "TYPE_REGISTRY_PATH",
        REPO / "config" / "card_type_registry.json",
    )


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """进程级状态进/出例清零（两处都是**跨用例不清**的模块表，本套件却要断言
    「没发生过」——不清会把上一例的痕迹算到本例头上）：

    1. ``card_msg.TRIGGERS``：遗留的 (work_001, card_003) 会在本例 schedule 时
       把刚建的同号卡白白调起；
    2. ``event_stream._journals``：work/card 编号每例从头计，上一例的 run_start
       还挂在同名键下（conftest 只清 event_log 游标，不清这张内存表）。
    """
    card_msg.TRIGGERS.clear()
    event_stream._journals.clear()
    yield
    card_msg.TRIGGERS.clear()
    event_stream._journals.clear()


async def _mk(wid: str, title: str, parent: str, **kw) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent, **kw)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _tree(coder_registry, work):
    """root → 子项目 A → 编程卡 C。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "子项目A", root)
    c = await _mk(wid, "编程卡C", a, card_type="coder")
    return wid, a, c


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    return app


# ══════════════ 指纹/开关/perm（判据5 ＋ 块1 硬判据） ══════════════

def test_toolset_fingerprints_and_switch_bindings():
    src = (REPO / "src" / "api" / "card_tools.py").read_text(encoding="utf-8")
    # [编程卡 判据5] 字面量口径（任务书 §六 grep 口径）
    # [规则卡 2026-10-15] 18 → 19（第 19 件 draft_rule_edit，§六块3 硬判据 1）。
    # [依赖关联线 2026-10-16] 19 → 21（第 20/21 件 link_rel / unlink_rel）。
    # [更新现状按钮改造 2026-10-16] 21 → 22（第 22 件 propose_status，列 OP_ONLY_TOOLS）。
    assert src.count('": ToolDef(') == 25  # [自动模式 2026-10-17 · G2] 23→25（+send_card_message/read_inbox）
    # [规则卡 2026-10-15] tc-5 → tc-6（新增 draft_rule_edit ＋第四段根卡闸）。
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel）。
    # [更新现状按钮改造 2026-10-16] tc-7 → tc-8（新增 propose_status）。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-10"  # [自动模式块0.4] tc-8→tc-9
    # [规则卡 2026-10-15] t20-1 → t21-1（新增「## 规则卡」节）。
    # [依赖关联线 2026-10-16] t21-1 → t22-1。
    assert prompts.BASE_PROMPT_VERSION == "t22-1"
    # 挂接（§六块2 · v1.1 修订：report_done 挂 tools_file，不设新开关；
    # 挂 edit 会让 readopen:287/:252 零翻新下无法同时成立——见 card_tools 注释）
    assert card_tools.TOOL_SWITCH["report_done"] == policy.SWITCH_TOOLS_FILE
    assert card_tools.TOOL_SWITCH["create_coder_card"] == policy.SWITCH_CREATE_CHILD
    # [C2 裁定A] read_memory 仍挂 SWITCH_READ，一动不动
    assert card_tools.TOOL_SWITCH["read_memory"] == policy.SWITCH_READ
    # 第三段闸的表内常量：report_done 是唯一 coder 专属工具
    assert set(card_tools.CODER_ONLY_TOOLS) == {"report_done"}


def test_coder_perms_v4_exactly_nine_and_reserved_switches():
    # [块1 硬判据2/3] 恰 9 项（search/list_memory 为预留位）
    assert set(policy.perms_for("rd_project", "coder")) == {
        "read", "edit", "tools_file", "tools_web", "search", "plan",
        "shadow_write", "read_linked", "list_memory",
    }
    assert "search" in policy.SWITCHES and hasattr(policy, "SWITCH_SEARCH")
    assert "list_memory" in policy.SWITCHES and hasattr(policy, "SWITCH_LIST_MEMORY")
    # 不含建删/发信/调工具/手稿
    for banned in ("create_child", "delete", "send_card_message",
                   "invoke_tool", "manuscript"):
        assert banned not in policy.perms_for("rd_project", "coder")


# ══════════════ 判据1/2 · 工具面分化（第三段 card_type 闸） ══════════════

async def test_toolface_coder_vs_main(coder_registry, work):
    wid, a, c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)

    coder_face = {t["name"] for t in card_tools.build_tool_schemas(
        c, work_id=wid, cards=cards)}
    assert "report_done" in coder_face
    for n in ("read_card", "read_memory", "read_shadow", "list_files",
              "read_file", "search_files", "write_file", "web_search",
              "fetch_web"):
        assert n in coder_face, f"coder 应见 {n}"
    # 编程卡不长树、不删卡、无发信、不调工具卡（也不见派生工具）
    assert not ({"create_card", "delete_card", "send_card_message",
                 "run_tool_card", "create_coder_card"} & coder_face), coder_face

    main_face = {t["name"] for t in card_tools.build_tool_schemas(
        a, work_id=wid, cards=cards)}
    assert "create_coder_card" in main_face
    assert "report_done" not in main_face, "第三段闸：主卡不得见 report_done"


# ══════════════ 判据2/3 · report_done 双保险与完工三件套 ══════════════

async def test_report_done_denied_for_non_coder(coder_registry, work):
    """执行层双保险：非 coder 卡调 report_done → err denied（挂 edit 也白挂）。"""
    wid, a, _c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "report_done",
                                      {"summary": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r


async def test_report_done_active_coder_finishes(coder_registry, work, monkeypatch):
    """判据3：真跑一轮 write_file（tool_call 事件落账）→ 完工三件套全绿。"""
    from src.storage import event_log

    wid, a, c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)

    # 首轮：模型带一次 write_file 工具调用（真实回合才会 emit tool_call——
    # 完工行的机械计数器读的就是 events/{card}.jsonl）；次轮收口。
    state = {"issued": False}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        if not state["issued"] and tools:
            state["issued"] = True
            return ({"content": "", "tool_calls": [{
                "name": "write_file",
                "arguments": {"path": "trading-workspace/coder_out.txt",
                              "content": "产物内容"},
            }]}, "fake")
        return ({"content": "任务完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    await routes._run_message(wid, c, "开工")
    await event_log.drain()

    r = await card_tools.execute_tool(wid, c, "report_done",
                                      {"summary": "已修复监听器泄漏"}, cards)
    assert r["ok"] is True, r
    d = r["data"]                      # T-0010：成功载荷整体在 data 内
    assert d["state"] == "completed"
    assert d["write_file_calls"] == 1, d
    assert d["owner"] == a and d["receipt_msg_id"], d
    assert "静默" in d["note"]

    # ② 库内事实：state=completed ＋ coder_final 原样收下 summary
    meta = (await card_store.read_card(wid, c))["meta"]
    assert meta["state"] == "completed"
    row = await card_db.run_read(
        wid, lambda conn: dict(conn.execute(
            "SELECT coder_final, state FROM cards WHERE card_id = ?",
            (c,)).fetchone() or {}), {})
    assert row.get("coder_final") == "已修复监听器泄漏"
    assert row.get("state") == "completed"

    # ③ 影子完工行（时间＋summary＋write_file 调用数，机械统计）
    shadow = await shadow_store.read_shadow_raw(wid, c)
    assert "完工：已修复监听器泄漏" in shadow
    assert "write_file 调用 1 次" in shadow

    # ④ 系统发信：owner 收件箱出现 coder_done（pending 留档，K3 待调起）
    pending = await card_db.list_pending(wid, a)
    assert any(p.get("type") == "coder_done" and p.get("from_card") == c
               for p in pending), pending
    assert (wid, a) in card_msg.TRIGGERS, "落信即记 TRIGGERS（K3 调起意图）"

    # 完工即终态：再报一次 → denied（completed 不可再收口）
    r2 = await card_tools.execute_tool(wid, c, "report_done",
                                       {"summary": "再报一次"}, cards)
    assert r2["ok"] is False and r2["error"]["code"] == "denied", r2


# ══════════════ 判据4 · create_coder_card 派生即调起 ══════════════

async def test_create_coder_card_builds_and_starts(coder_registry, work, monkeypatch):
    wid, a, _ = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)
    captured: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        captured.append(str(user_prompt))
        return ({"content": "收到，开工。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    r = await card_tools.execute_tool(
        wid, a, "create_coder_card",
        {"title": "修复监听器泄漏", "task": "修复 src/api 下的监听器泄漏并补测"},
        cards)
    assert r["ok"] is True, r
    d = r["data"]                      # T-0010：成功载荷整体在 data 内
    nid = d["card_id"]
    assert d["card_type"] == "coder" and d["parent"] == a
    assert d["started"] is True, d

    got = await card_store.read_card(wid, nid)
    meta, blob = got["meta"], card_tools._card_body_text(got)
    assert meta["card_type"] == "coder"
    assert meta["parent"] == a
    assert meta["perms"] == policy.perms_for("rd_project", "coder"), "perms＝v4 值"
    assert "## 使命" in blob and "修复 src/api 下的监听器泄漏并补测" in blob, blob[:200]

    # 建卡即首跑：等首跑回合收口（激励文 → assistant 块）
    for _ in range(80):
        await asyncio.sleep(0.05)
        blocks = await session_facade.read(wid, nid)
        if any(b.get("actor") == "assistant" for b in blocks):
            break
    blocks = await session_facade.read(wid, nid)
    assert any(b.get("actor") == "assistant" for b in blocks), "建卡即首跑（回合已产出）"
    types = [e["type"] for e in event_stream.journal_of(wid, nid)]
    assert "run_start" in types, types
    # 首跑激励文带任务原文（形态照 _build_card_msg_prompt：卡名＋任务内容）
    assert any("修复 src/api 下的监听器泄漏并补测" in p for p in captured), captured[:2]


# ══════════════ 块3 · 完工调起验收 ＋ 双保险静默 ══════════════

async def test_report_done_calls_owner_and_coder_stays_silent(
        coder_registry, work, monkeypatch):
    """全链路收口：完工回执 → 主卡被调起验收 → answer 落编程卡收件箱留档 →
    编程卡无新回合（K3 对 completed coder 跳过、信仍 pending）。"""
    wid, a, c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)
    captured: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        captured.append(str(user_prompt))
        return ({"content": "验收通过，收工。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    r = await card_tools.execute_tool(wid, c, "report_done",
                                      {"summary": "完工摘要A"}, cards)
    assert r["ok"] is True, r

    # ① 回收口触发：主卡被调起（K3 现有链，零改）
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a, content="x"))
    for _ in range(80):
        await asyncio.sleep(0.05)
        blocks_a = await session_facade.read(wid, a)
        if any(b.get("actor") == "assistant" for b in blocks_a):
            break
    blocks_a = await session_facade.read(wid, a)
    assert any(b.get("actor") == "assistant" for b in blocks_a), "完工回执应调起主卡验收"
    assert any("完工摘要A" in p for p in captured), "激励文应带完工内容"

    # ② S4 自动回发：验收 answer 落编程卡收件箱（留档，pending）
    answer = None
    for _ in range(80):
        await asyncio.sleep(0.05)
        rows = await card_db.list_pending(wid, c)
        answer = next((p for p in rows if p.get("type") == "msg_answer"), None)
        if answer:
            break
    assert answer is not None, "主卡验收 answer 应回落编程卡收件箱留档"

    # ③ 编程卡静默：档案无新块、事件流无 run_start
    blocks_c_before = await session_facade.read(wid, c)
    types_c_before = [e["type"] for e in event_stream.journal_of(wid, c)]
    assert "run_start" not in types_c_before, "完工前编程卡未被调起过"

    # ④ K3 再收口：completed coder 被跳过——不调起、信仍 pending、不打 ops deliver
    assert (wid, c) in card_msg.TRIGGERS, "S4 回发已登记调起意图（待下个收口消费）"
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a, content="x2"))
    for _ in range(20):
        await asyncio.sleep(0.05)
    rows_after = await card_db.list_pending(wid, c)
    assert any(p.get("msg_id") == answer.get("msg_id") for p in rows_after), \
        "验收信必须仍 pending（留档，不消费）"
    blocks_c_after = await session_facade.read(wid, c)
    assert blocks_c_after == blocks_c_before, "编程卡不得有新回合"
    types_c_after = [e["type"] for e in event_stream.journal_of(wid, c)]
    assert "run_start" not in types_c_after, "K3 必须跳过 completed coder"


async def test_completed_coder_post_message_is_400(coder_registry, work, monkeypatch):
    """静默判定①：completed coder 走 POST /messages → 400（不再激活）。"""
    wid, a, c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, c, "report_done",
                                      {"summary": "完工"}, cards)
    assert r["ok"] is True, r

    # 未完工卡的对照请求要真跑一个回合 → 网关替身
    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        return ({"content": "在。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as client:
        resp = await client.post(f"/api/works/{wid}/cards/{c}/messages",
                                 json={"content": "再改一版"})
        assert resp.status_code == 400, (resp.status_code, resp.text)
        assert "编程卡已完工" in resp.json().get("error", "")
        # 未完工卡不受静默判定影响：同一路由照常开跑（判误拦）
        resp_a = await client.post(f"/api/works/{wid}/cards/{a}/messages",
                                   json={"content": "在吗"})
    assert resp_a.status_code == 200, (resp_a.status_code, resp_a.text)


async def test_coder_cannot_delete(coder_registry, work):
    """留档判据：coder 无 delete 开关 → 删自己（或任何卡）denied，卡仍在。"""
    wid, _a, c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, c, "delete_card",
                                      {"card_id": c}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r
    assert card_store.card_exists(wid, c)
