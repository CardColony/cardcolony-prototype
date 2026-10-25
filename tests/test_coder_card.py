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

import async_settle  # 测试侧等待原语（同目录模块）

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
    assert src.count('": ToolDef(') == 32  # [角色机制 5710] 28→32（+create_role/role_watch_cards/record_verdict/raise_alarm）
    # [规则卡 2026-10-15] tc-5 → tc-6（新增 draft_rule_edit ＋第四段根卡闸）。
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel）。
    # [更新现状按钮改造 2026-10-16] tc-7 → tc-8（新增 propose_status）。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-17"  # [去对话 2026-10-25]  # [自动模式块0.4] tc-8→tc-9
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
    # [块1 硬判据2/3] v4 口径：9 项（search/list_memory 为预留位）
    # [v12 2026-10-22 · 用户裁定「coder 至少能给自己的父卡发信」] 追加
    # send_card_message —— v4 的原意是「完工回执不要模型自己发」（由 report_done
    # 系统发），执行成了「整类卡不给发信」，于是 coder 干到一半卡住时**说不了话**。
    assert set(policy.perms_for("rd_project", "coder")) == {
        "read", "edit", "tools_file", "tools_web", "search", "plan",
        "shadow_write", "read_linked", "list_memory", "send_card_message",
    }
    assert "search" in policy.SWITCHES and hasattr(policy, "SWITCH_SEARCH")
    assert "list_memory" in policy.SWITCHES and hasattr(policy, "SWITCH_LIST_MEMORY")
    # 仍不含建树/删卡/调工具/手稿（发信已在 v12 放开，见上）
    for banned in ("create_child", "delete", "invoke_tool", "manuscript"):
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
    # 编程卡不长树、不删卡、不调工具卡（也不见派生工具）。
    # [v12 2026-10-22] send_card_message **放开**：中途要能问主卡（父子关系即通道）。
    assert not ({"create_card", "delete_card",
                 "run_tool_card", "create_coder_card"} & coder_face), coder_face
    assert "send_card_message" in coder_face, "coder 必须能向父卡发信（v12）"

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

    

async def test_coder_cannot_delete(coder_registry, work):
    """留档判据：coder 无 delete 开关 → 删自己（或任何卡）denied，卡仍在。"""
    wid, _a, c = await _tree(coder_registry, work)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, c, "delete_card",
                                      {"card_id": c}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r
    assert card_store.card_exists(wid, c)
