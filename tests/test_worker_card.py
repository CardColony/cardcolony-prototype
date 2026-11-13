# -*- coding: utf-8 -*-
"""[worker 卡 2026-10-15 · §六块2/块3] worker 卡全部新测试（任务书 §四 白名单件）。

覆盖：
- 判据5/块1 硬判据：工具集指纹（grep 19 / tools-tc-6 / t21-1）、开关挂接、
  worker perms 恰 9、search/list_memory 预留机关在册；
- 判据1/2：工具面分化（worker 见 report_done 不见 create_card 族；主卡见
  create_worker_card 不见 report_done——第三段 card_type 闸）；
- 判据3：report_done 双保险（非 worker 拒）＋完工三件套（completed/coder_final
  落库/影子完工行/owner 收件箱 worker_done/回执 msg_id）；
- 判据4：create_worker_card 派生即调起（卡面/使命块/perms=4 值/首跑 run_start）；
- 块3：完工回执调起主卡验收 → S4 自动回发 answer 落worker 卡收件箱留档、
  worker 卡**无新回合**（K3 跳过＋信仍 pending）；completed worker POST /messages
  → 400；worker 卡不可删。

注册表：本套件**自指真实** config/card_type_registry.json（含 worker）——
tests/_fixtures 副本属 §四 白名单外文件，零触碰。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import tests.async_settle  # 测试侧等待原语（同目录模块）

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
def worker_registry(monkeypatch):
    """worker 卡词表走真实配置（config/card_type_registry.json 含 worker）。"""
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


async def _tree(worker_registry, work):
    """root → 子项目 A → worker 卡 C。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "子项目A", root)
    c = await _mk(wid, "worker 卡C", a, card_type="worker")
    return wid, a, c


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    return app


# ══════════════ 指纹/开关/perm（判据5 ＋ 块1 硬判据） ══════════════

def test_toolset_fingerprints_and_switch_bindings():
    src = (REPO / "src" / "api" / "card_tools.py").read_text(encoding="utf-8")
    # [worker 卡 判据5] 字面量口径（任务书 §六 grep 口径）
    # [规则卡 2026-10-15] 18 → 19（第 19 件 draft_rule_edit，§六块3 硬判据 1）。
    # [依赖关联线 2026-10-16] 19 → 21（第 20/21 件 link_rel / unlink_rel）。
    # [更新现状按钮改造 2026-10-16] 21 → 22（第 22 件 propose_status，列 OP_ONLY_TOOLS）。
    assert src.count('": ToolDef(') == 36  # [2026-11-09 画线看全图] 35→36（+list_cards）
    # [规则卡 2026-10-15] tc-5 → tc-6（新增 draft_rule_edit ＋第四段根卡闸）。
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel）。
    # [更新现状按钮改造 2026-10-16] tc-7 → tc-8（新增 propose_status）。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-31"  # [2026-11-14] 委办线三件工具描述
    # [规则卡 2026-10-15] t20-1 → t21-1（新增「## 规则卡」节）。
    # [依赖关联线 2026-10-16] t21-1 → t22-1。
    assert prompts.BASE_PROMPT_VERSION == "t27-19"          # [2026-11-01] 随现配置对齐
    # 挂接（§六块2 · v1.1 修订：report_done 挂 tools_file，不设新开关；
    # 挂 edit 会让 readopen:287/:252 零翻新下无法同时成立——见 card_tools 注释）
    assert card_tools.TOOL_SWITCH["report_done"] == policy.SWITCH_TOOLS_FILE
    assert card_tools.TOOL_SWITCH["create_worker_card"] == policy.SWITCH_CREATE_CHILD
    # [C2 裁定A] read_memory 仍挂 SWITCH_READ，一动不动
    assert card_tools.TOOL_SWITCH["read_memory"] == policy.SWITCH_READ
    # 第三段闸的表内常量：report_done 是唯一 worker 专属工具
    assert set(card_tools.WORKER_ONLY_TOOLS) == {"report_done"}


def test_worker_perms_v4_exactly_nine_and_reserved_switches():
    # [块1 硬判据2/3] v4 口径：9 项（search/list_memory 为预留位）
    # [v12 2026-10-22 · 用户裁定「worker 至少能给自己的父卡发信」] 追加
    # send_card_message —— v4 的原意是「完工回执不要模型自己发」（由 report_done
    # 系统发），执行成了「整类卡不给发信」，于是 worker 干到一半卡住时**说不了话**。
    # [worker 改名 2026-11-05 · 用户裁定「编程卡读档有什么问题」] 追加
    # manuscript——执行体做后端/文档活时对照存档底稿是本职需求。
    assert set(policy.perms_for("rd_project", "worker")) == {
        "read", "edit", "tools_file", "tools_web", "search", "plan",
        "shadow_write", "read_linked", "list_memory", "send_card_message",
        "manuscript",
    }
    assert "search" in policy.SWITCHES and hasattr(policy, "SWITCH_SEARCH")
    assert "list_memory" in policy.SWITCHES and hasattr(policy, "SWITCH_LIST_MEMORY")
    # 仍不含建树/删卡/调工具（发信已在 v12 放开，手稿在 worker 改名时放开，见上）
    for banned in ("create_child", "delete", "invoke_tool"):
        assert banned not in policy.perms_for("rd_project", "worker")


# ══════════════ 判据1/2 · 工具面分化（第三段 card_type 闸） ══════════════

async def test_toolface_worker_vs_main(worker_registry, work):
    wid, a, c = await _tree(worker_registry, work)
    cards = card_tools._collect_card_meta(wid)

    worker_face = {t["name"] for t in card_tools.build_tool_schemas(
        c, work_id=wid, cards=cards)}
    assert "report_done" in worker_face
    for n in ("read_card", "read_memory", "read_shadow", "list_files",
              "read_file", "search_files", "write_file", "web_search",
              "fetch_web"):
        assert n in worker_face, f"worker 应见 {n}"
    # worker 卡不长树、不删卡、不调工具卡（也不见派生工具）。
    assert not ({"create_card", "delete_card",
                 "run_tool_card", "create_worker_card"} & worker_face), worker_face
    # [通信收拢 2026-10-31] sync 只追加给 sub_project/topic，worker 没有任何
    # 发信通道（report_done 是系统回执不算模型发信）。
    # ⚠️ 悬而未决：v12「worker 至少能向父卡发信（卡住时能问）」当前无通道承载。
    #   复核已把此事列为待用户裁定项——恢复与否是产品决定，此处先钉现状。
    assert "sync" not in worker_face, "通信收拢现状：worker 无发信通道（v12 问题待裁定）"

    # [需求文档生命周期 2026-10-28 · 用户令] 子卡**没有正稿 ⇒ 摘派工三件**
    # （create_card/create_worker_card/direct）。要验派工工具可见，先给 A
    # 一份 approved 正稿——这才符合闸的设计语义（而不是把闸当 bug）。
    await card_store.update_card(
        wid, a, {"requirements": "交付 X", "requirements_state": "approved"},
        "测试：转正稿")
    cards = card_tools._collect_card_meta(wid)
    main_face = {t["name"] for t in card_tools.build_tool_schemas(
        a, work_id=wid, cards=cards)}
    assert "create_worker_card" in main_face
    assert "report_done" not in main_face, "第三段闸：主卡不得见 report_done"


# ══════════════ 判据2/3 · report_done 双保险与完工三件套 ══════════════

async def test_report_done_denied_for_non_worker(worker_registry, work):
    """执行层双保险：非 worker 卡调 report_done → err denied（挂 edit 也白挂）。"""
    wid, a, _c = await _tree(worker_registry, work)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "report_done",
                                      {"summary": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r


async def test_report_done_active_worker_finishes(worker_registry, work, monkeypatch):
    """判据3：真跑一轮 write_file（tool_call 事件落账）→ 完工三件套全绿。"""
    from src.storage import event_log

    wid, a, c = await _tree(worker_registry, work)
    cards = card_tools._collect_card_meta(wid)

    # 首轮：模型带一次 write_file 工具调用（真实回合才会 emit tool_call——
    # 完工行的机械计数器读的就是 events/{card}.jsonl）；次轮收口。
    state = {"issued": False}

    

async def test_worker_cannot_delete(worker_registry, work):
    """留档判据：worker 无 delete 开关 → 删自己（或任何卡）denied，卡仍在。"""
    wid, _a, c = await _tree(worker_registry, work)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, c, "delete_card",
                                      {"card_id": c}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r
    assert card_store.card_exists(wid, c)


# ══════════ 交工不是许可证（2026-11-08 线上回归） ══════════

async def test_report_done_on_completed_worker_still_delivers(
        worker_registry, work, monkeypatch):
    """已完工 worker 再被派活 ⇒ 仍能交工、仍能发 worker_done 回执。

    线上实况（11-07/11-08 共 86 条收口日志）：父卡 send_directive 走
    deliver_now(text) 直接起轮，不经过 _revive_worker_card，卡停在 completed；
    旧版 report_done 拿 state 当「本轮能不能交工」的许可，把这类轮全拒收口
    ⇒ 父卡零回执、重复派活。交工本身是幂等动作：状态列只记「交过工」，
    不该拦本轮交付，也不该重复写状态/重复发状态事件。
    """
    import sqlite3
    from src.storage import card_db as _cdb, event_log as _elog

    wid, _a, c = await _tree(worker_registry, work)
    await card_store.update_card(wid, c, {"state": "completed"}, "上一轮已交工")
    await _elog.drain()
    ev_path = _elog.event_path(wid, c)

    def _state_events() -> int:
        if not ev_path.is_file():
            return 0
        return sum(1 for ln in ev_path.read_text(encoding="utf-8").splitlines()
                   if '"block": "state"' in ln or '"block":"state"' in ln)

    before = _state_events()
    sent: list[dict] = []

    async def _fake_send(w, from_id, *, target_id, msg_type, content, **kw):
        sent.append({"target": target_id, "type": msg_type, "content": content})
        return {"ok": True, "msg_id": "fake-1"}

    monkeypatch.setattr(card_tools, "send_card_message", _fake_send)
    meta = (await card_store.read_card(wid, c))["meta"]
    res = await card_tools._h_report_done(
        wid, c, {"summary": "第二轮交付正文"}, {c: dict(meta)})

    assert isinstance(res, dict) and res.get("ok") is True, res
    assert res.get("state") == "completed"
    assert len(sent) == 1 and sent[0]["type"] == "worker_done", sent
    assert "第二轮交付正文" in sent[0]["content"]
    # 本轮正文确实落库（coder_final 不走 read_card 的 meta 投影）
    with sqlite3.connect(_cdb.db_path(wid)) as conn:
        row = conn.execute("SELECT coder_final FROM cards WHERE card_id=?",
                           (c,)).fetchone()
    assert row and "第二轮交付正文" in str(row[0])
    # 幂等：不重复写状态 ⇒ 不重复发 state 事件、不重复挂完工兜底网
    await _elog.drain()
    assert _state_events() == before
    after = await card_store.read_card(wid, c)
    assert after["meta"]["state"] == "completed"


# ══════════════ 完工未报兜底（2026-11-10 · worker_unreported_fallback）══════════════
# worker 干了活（turn_tool_call_count>0）但回合结束没走 report_done/report_up ⇒
# 末轮输出＋产物落点作 worker_done 补发父卡——「干了活却没交差」不允许静默。
# 判重：模型本回合报过 / _CODER_DONE_SENT_AT ≥ turn_start（兜底网已发）→ 不双发。

async def _fb_tree(worker_registry, work):
    return await _tree(worker_registry, work)


def _fb_state(**kw):
    from types import SimpleNamespace
    base = dict(tool_events=[], turn_tool_call_count=3, turn_start_ts=1.0,
                clean_text="已把 LOD 双轨并成单源，产物见落点。", final_only="")
    base.update(kw)
    return SimpleNamespace(**base)


def _capture_append(monkeypatch):
    sent = []

    async def _fake(w, target, *, from_card, text, msg_type, **kw):
        sent.append({"to": target, "from": from_card, "type": msg_type,
                     "text": text})
        return {"seq": 1}

    monkeypatch.setattr(session_facade, "append_card_message", _fake)
    return sent


async def test_unreported_fallback_sends_receipt(worker_registry, work,
                                                 monkeypatch):
    wid, a, c = await _tree(worker_registry, work)
    sent = _capture_append(monkeypatch)
    card_tools._CODER_DONE_SENT_AT.pop(c, None)
    await card_tools.worker_unreported_fallback(wid, c, _fb_state())
    assert len(sent) == 1 and sent[0]["to"] == a, sent
    assert sent[0]["type"] == "worker_done" and sent[0]["from"] == c
    assert "未走 report_done" in sent[0]["text"] and "LOD" in sent[0]["text"]
    # 置位 ⇒ 完工兜底网 4s 复查见「已发」跳过，绝不双发
    assert card_tools._CODER_DONE_SENT_AT.get(c, 0) > 0
    card_tools._CODER_DONE_SENT_AT.pop(c, None)


async def test_unreported_fallback_skips_reported(worker_registry, work,
                                                  monkeypatch):
    wid, a, c = await _tree(worker_registry, work)
    sent = _capture_append(monkeypatch)
    # 模型本回合自己调过 report_done ⇒ 不补
    st = _fb_state(tool_events=[{"tool": "report_done", "ok": True}])
    await card_tools.worker_unreported_fallback(wid, c, st)
    assert sent == []
    # report_up 同理（父卡已收到回执，不交差但不算静默）
    st = _fb_state(tool_events=[{"tool": "report_up", "ok": True}])
    await card_tools.worker_unreported_fallback(wid, c, st)
    assert sent == []


async def test_unreported_fallback_skips_no_work(worker_registry, work,
                                                 monkeypatch):
    wid, a, c = await _tree(worker_registry, work)
    sent = _capture_append(monkeypatch)
    # 零工具回合（纯应答/被秒停）⇒ 没有可交付内容不补
    await card_tools.worker_unreported_fallback(
        wid, c, _fb_state(turn_tool_call_count=0))
    assert sent == []
    # 兜底网已发过（_CODER_DONE_SENT_AT ≥ turn_start）⇒ 不双发
    card_tools._CODER_DONE_SENT_AT[c] = 999.0
    await card_tools.worker_unreported_fallback(wid, c, _fb_state())
    assert sent == []
    card_tools._CODER_DONE_SENT_AT.pop(c, None)
    # 空正文且无产物 ⇒ 没啥可报
    await card_tools.worker_unreported_fallback(
        wid, c, _fb_state(clean_text="", final_only=""))
    assert sent == []
