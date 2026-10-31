# -*- coding: utf-8 -*-
"""「更新现状」按钮改造 · 本任务全部新测试（2026-10-16）。

一句话：**「更新现状」从独立 /draft 起草通道，收回本卡一次「专用回合」**——
点按钮＝一次带 ``op="update_status"`` 的发送；后端轮首注入专用任务（含当前现状全文）；
模型用**只提议不落盘**的 ``propose_status`` 交出「新现状」；前端扫 timeline 弹
单面板行级 diff 对比窗（提议可改）；「同意」复用 commit 落盘。

覆盖任务书 §三 验收 V1–V8 里可由测试机械判定的部分：
- V2/V4 工具面：``propose_status`` **不是卡片内挂**（不进常规工具面），只在 op 回合追加；
- V3 零落盘：调 propose_status 后盘上 status_block 逐字节不变；
- V7 口径：注入正文含 155 区现状三类口径 + 当前现状全文；
- V1 注入：op 回合轮首恰一条 ``status_update_task``，普通回合不发；
- V6 绑卡：对比窗提交用**窗口自带 cardId**（源码级钉死）；
- V8 版本：TOOL_TABLE_VERSION / 本体论类数。

纪律：所有用例走 conftest 沙箱（WORKS_DIR 重定向到 tmp），**不写真实 works/**。
"""
from __future__ import annotations

import re

import hashlib
from pathlib import Path

from src.api import card_tools, current_turn, event_stream, message_runner, prompts
from src.api import card_draft
from src.permissions import policy
from src.storage import card_store, event_log, work_store

ROOT = Path(__file__).resolve().parents[1]


# ════════════════════════════════════════════════════════════════════
# 夹具
# ════════════════════════════════════════════════════════════════════

async def _mk_work_with_card() -> tuple[str, str]:
    """建一个 rd_project work（自带根卡 card_001）＋一张 sub_project 子卡。"""
    wdata = await work_store.create_work("现状更新剧本", project_class="rd_project")
    assert "error" not in wdata, wdata
    wid = wdata["work_id"]
    r = await card_store.create_card(wid, "子卡", "用户直接创建",
                                     card_type="sub_project", parent="card_001")
    assert "error" not in r, r
    return wid, r["meta"]["card_id"]


# ════════════════════════════════════════════════════════════════════
# V8 · 版本与本体论
# ════════════════════════════════════════════════════════════════════

def test_version_and_ontology():
    """硬判据：tools-tc-11；propose_status 进注册表/开关/OP_ONLY_TOOLS/只读；
    status_update_task 进事件本体论；BASE_PROMPT 未动（t22-1 不升）。

    [自动模式 2026-10-17 · 回灌 5700 块0.4] tc-8 → tc-9（新增 update_goal，
    auto 卡工具面件数变化）。
    """
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-18"  # [2026-10-31 F19] tc-17→tc-18（+memory_expand）
    assert "propose_status" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_REGISTRY["propose_status"].readonly is True
    assert "propose_status" in card_tools.TOOL_SWITCH
    assert "propose_status" in card_tools.OP_ONLY_TOOLS
    assert "status_update_task" in event_stream.EVENT_TYPES
    # 本轮没动 system 提示词 ⇒ 版本号不动（版本纪律：只动 BASE_PROMPT 才升）
    assert prompts.BASE_PROMPT_VERSION == "t27-2"


# ════════════════════════════════════════════════════════════════════
# V2/V4 · 工具面：不是卡片内挂，只在 op 回合出现
# ════════════════════════════════════════════════════════════════════

def _full_perm_card() -> dict:
    full = policy.perms_for("rd_project", "sub_project")
    return {"card_id": "card_x", "card_type": "sub_project", "perms": full}


def test_propose_status_not_in_normal_toolface():
    """常规回合（op 缺省）工具面**不含** propose_status；op 回合含、且追加在末位。"""
    cards = {"card_x": _full_perm_card()}
    base = card_tools.build_tool_schemas("card_x", cards=cards)
    names = [t["name"] for t in base]
    assert "propose_status" not in names, "非卡片内挂的工具不该出现在常规工具面"

    op = card_tools.build_tool_schemas("card_x", cards=cards,
                                       op=card_tools.OP_UPDATE_STATUS)
    op_names = [t["name"] for t in op]
    assert "propose_status" in op_names
    assert op_names[-1] == "propose_status", "专用工具追加在四段过滤之后（末位）"
    # 相对常规工具面**只多这一件**（不是把别的工具换掉）
    assert set(op_names) - set(names) == {"propose_status"}
    assert set(names) - set(op_names) == set()


def test_propose_status_visible_even_without_card_perm():
    """它不随卡 perms 收敛：只读卡（无 edit）也照样看得见——可见性唯一由 op 决定。"""
    ro = {"card_id": "card_x", "card_type": "sub_project", "perms": ["read"]}
    op = card_tools.build_tool_schemas("card_x", cards={"card_x": ro},
                                       op=card_tools.OP_UPDATE_STATUS)
    assert "propose_status" in [t["name"] for t in op]


# ════════════════════════════════════════════════════════════════════
# V3 · propose_status 只提议、不落盘
# ════════════════════════════════════════════════════════════════════

async def test_propose_status_proposes_without_writing(sandbox):
    """调 propose_status → 返回 {proposed, old, base_hash}；**盘上零改动**。"""
    wid, cid = await _mk_work_with_card()
    old_text = "## 当前确定状态\n- 接口已定稿\n"
    await card_store.update_card(wid, cid, {"status_block": old_text}, "预置现状")

    before = (await card_store.read_card(wid, cid))["body"]["status_block"]
    r = await card_tools._h_propose_status(
        wid, cid, {"content": "新的现状正文"}, {})
    assert r["proposed"] == "新的现状正文"
    assert r["old"] == old_text
    assert r["base_hash"] == hashlib.sha256(old_text.encode("utf-8")).hexdigest()

    after = (await card_store.read_card(wid, cid))["body"]["status_block"]
    assert after == before == old_text, "propose_status 必须零落盘"


async def test_propose_status_placeholder_treated_as_empty(sandbox):
    """占位符/空现状 → old 为空串（口径与 card_draft 同源），base_hash＝sha256("")。"""
    wid, cid = await _mk_work_with_card()
    r = await card_tools._h_propose_status(wid, cid, {"content": "从零写的现状"}, {})
    assert r["old"] == ""
    assert r["base_hash"] == hashlib.sha256(b"").hexdigest()


async def test_propose_status_rejects_empty_content(sandbox):
    """content 空/纯空白 → validation 失败信封（不静默产出空提议）。"""
    wid, cid = await _mk_work_with_card()
    r = await card_tools._h_propose_status(wid, cid, {"content": "   "}, {})
    assert r.get("ok") is False
    assert r["error"]["code"] == "validation"


# ════════════════════════════════════════════════════════════════════
# V7/V1 · 注入事件与投影
# ════════════════════════════════════════════════════════════════════

async def test_status_task_event_carries_three_kinds_and_current_status(sandbox):
    """注入正文：现状三类口径 + 当前现状全文；投影 role=assistant、抬头〔现状更新〕。"""
    wid, cid = await _mk_work_with_card()
    cur = "当前确定状态：接口已定稿"
    await card_store.update_card(wid, cid, {"status_block": cur}, "预置现状")

    ev = current_turn.build_status_task_event(wid, cid)
    assert ev["type"] == "status_update_task"
    assert ev["data"]["source_kind"] == "status_update_task"
    body = ev["data"]["content"]
    for token in ("当前确定状态", "接口与约定", "依赖与影响范围",
                  "整块覆盖", "不写过程", "propose_status"):
        assert token in body, token
    assert cur in body, "必须带上当前现状全文（供模型对照修订）"

    msg = current_turn.project_status_task_message(ev["data"])
    assert set(msg.keys()) == {"role", "content"}
    assert msg["role"] == "assistant"
    assert msg["content"].startswith("〔现状更新〕")
    assert cur in msg["content"]


async def test_status_task_event_empty_status_placeholder(sandbox):
    """现状为空/占位符 → 正文填「（暂无现状，从零起草）」。"""
    wid, cid = await _mk_work_with_card()
    ev = current_turn.build_status_task_event(wid, cid)
    assert "（暂无现状，从零起草）" in ev["data"]["content"]


async def test_status_task_injected_only_on_op_turn(sandbox):
    """op 回合轮首恰一条 status_update_task 且进 turn_tail；普通回合不发；幂等。"""
    wid, cid = await _mk_work_with_card()

    # 普通回合：不发
    plain = message_runner.RunState(work_id=wid, card_id=cid, content="随便聊聊")
    message_runner._sync_status_task(plain)
    assert plain.turn_tail == []
    assert [r for r in event_log.timeline(wid, cid)
            if r.get("type") == "status_update_task"] == []

    # op 回合：发一条、进 turn_tail
    st = message_runner.RunState(work_id=wid, card_id=cid, content="更新现状")
    st.op = card_tools.OP_UPDATE_STATUS
    st.rounds = 1
    message_runner._sync_status_task(st)
    assert len(st.turn_tail) == 1
    assert st.turn_tail[0]["role"] == "assistant"
    assert st.turn_tail[0]["content"].startswith("〔现状更新〕")
    await event_log.drain()   # 事件落盘走 append_bg（后台缓冲），取盘前先 drain
    emitted = [r for r in event_log.timeline(wid, cid)
               if r.get("type") == "status_update_task"]
    assert len(emitted) == 1

    # 幂等：同一 run 再调一次不重发
    message_runner._sync_status_task(st)
    assert len(st.turn_tail) == 1
    await event_log.drain()
    emitted = [r for r in event_log.timeline(wid, cid)
               if r.get("type") == "status_update_task"]
    assert len(emitted) == 1


async def test_judgment_protocol_suppressed_on_op_turn(sandbox):
    """专用回合不注入判断协议（本轮任务已由按钮定死，不需要入口判断）。"""
    wid, cid = await _mk_work_with_card()
    st = message_runner.RunState(work_id=wid, card_id=cid, content="更新现状")
    st.op = card_tools.OP_UPDATE_STATUS
    st.rounds = 1
    message_runner._sync_judgment(st)
    assert st.judgment_tail == []
    await event_log.drain()
    assert [r for r in event_log.timeline(wid, cid)
            if r.get("type") == "judgment_protocol"] == []


# ════════════════════════════════════════════════════════════════════
# V5/§五.5 · 老 /draft 通道保留（本次不退役）
# ════════════════════════════════════════════════════════════════════

def test_draft_status_branch_kept():
    """/draft 的 kind=status 分支**原样保留**（本次只让 UI 不再调它）。

    理由（用户 2026-10-16 定）：commit 仍必须收 kind=status（新流程复用它落盘），
    删 draft 分支零收益、只增改动面。
    """
    assert "status" in card_draft.ALLOWED_KINDS
    assert "requirements" in card_draft.ALLOWED_KINDS


# ════════════════════════════════════════════════════════════════════
# V1/V4/V5/V6 · 前端接线（源码级机械断言；DOM 行为由 jsdom 一次性脚本另验）
# ════════════════════════════════════════════════════════════════════

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_frontend_button_sends_op():
    """按钮不再走 openDraft("status")，改发起带 op 的专用回合。"""
    js = _read("static/workspace.js")
    assert 'statusDraftBtn.onclick = () => send("update_status")' in js
    assert 'openDraft("status")' not in js, "旧起草通道入口必须摘掉"


def test_frontend_api_passes_op():
    """api.js 的 postMessage 带 op（缺省不带 ⇒ 普通发送逐字不变）。"""
    js = _read("static/esm/api.js")
    assert "postMessage: (wid, cid, content, op)" in js
    assert "op ? { content, op } : { content }" in js


def test_frontend_status_modal_scans_propose_status_and_binds_card():
    """收口扫 propose_status 成功 tool_result；提交用**窗口自带 cardId**（防串卡）。"""
    js = _read("static/workspace.js")
    assert 'd.tool !== "propose_status"' in js
    assert "window.__maybeOpenStatusModal" in js
    # 绑卡：提交用 dmStatus.cardId，不是 currentCard
    assert "API.postCommit(WORK_ID, cid, {" in js
    assert 'dmStatus = { cardId: cardId || ""' in js


def test_frontend_modal_dom_and_diff_styles_exist():
    """独立 #statusModal 元素（不挤 #draftModal）＋ 行级 diff 样式＋可编辑提议框。
    [阶段3 A3.2 2026-10-17] 模板已拆分：对话弹窗归 templates/card.html。"""
    html = _read("templates/card.html")
    for el in ("statusModal", "statusDiffArea", "statusEditArea",
               "statusConfirmBtn", "statusCloseBtn"):
        assert f'id="{el}"' in html, el
    css = _read("static/style.css")
    for sel in (".sd-line.sd-del", ".sd-line.sd-add", ".status-edit-area",
                "#statusModal .draft-modal-panel"):
        assert sel in css, sel


def test_frontend_diff_segment_markers_present():
    """diff 纯函数段的起/终标记在位——tests/status_diff.test.mjs 靠剪段求值验证它。"""
    js = _read("static/workspace.js")
    assert "==================== 现状 diff（纯函数" in js
    assert "==================== 现状 diff 段终 ====================" in js


def test_frontend_version_stamps_bumped():
    """涉改模块升号；引用点同号（防双份代码并存）。

    [对话流照抄 dsh 完全版 v3 2026-10-17] stream 36→37、workspace 45→46、
    style 37→38、main_chat 1.2→1.3（它 import 了 stream/workspace）。
    [建项目流程 2026-10-17] workspace 46→51、stream 37→38、main_chat 1.3→1.8。
    [＋按钮删除 2026-10-17] input-bar 1→2、workspace 51→52、main_chat 1.8→1.9、
    style 38→39（命令面不存在，＋按钮整件删——死件不留）。
    [自动模式回灌 2026-10-17] workspace 52→53（autoToggle 开关＋insp 标记）、
    main_chat 1.9→2.0（import workspace 53）、style 39→40（.auto-* 样式段）。
    [6702 移植 P8] workspace 56→57（autoResume 按钮）、style 40→41（仅升戳）。
    [5702 · C-1 2026-10-20] stream 42→43、workspace 57→58、assembler 35→36、
    chat-view 3→4、main_chat 2.3→2.4（逐字流进装配器，旧 live 旁路退役）；
    style 41→45（本分支既存漂移：home/settings 仍是 40，本次统一到 45）。
    """
    # [版本断言 2026-10-21] 改「精确值」为「不低于」：版本号由
    # scripts/bump_frontend_versions.py 自动传播，每次都精确匹配会天天红。
    # 真正要防的回退是「版本变低/变回旧值」，不是「又升了一号」。
    def _ver(text: str, name: str) -> str:
        m = re.search(re.escape(name) + r"\?v=([0-9.]+)", text)
        assert m, f"{name} 在引用里找不到"
        return m.group(1)

    def _vkey(v: str):
        return tuple(int(x) for x in v.split("."))

    assert _vkey(_ver(_read("static/esm/main_chat.js"), "../stream.js")) >= (53,)
    assert _vkey(_ver(_read("static/esm/main_chat.js"), "../workspace.js")) >= (76,)
    assert _vkey(_ver(_read("static/workspace.js"), "./stream.js")) >= (53,)
    assert _vkey(_ver(_read("static/workspace.js"), "./esm/flow/input-bar.js")) >= (5,)
    assert _vkey(_ver(_read("templates/work.html"), "/static/style.css")) >= (49,)
    assert _vkey(_ver(_read("templates/card.html"), "/static/esm/main_chat.js")) >= (4, 8)


# ════════════════════════════════════════════════════════════════════
# V1/V2 · 端到端：op 真能穿透路由 → 编排 → 模型请求
# ════════════════════════════════════════════════════════════════════

class _RecordingGateway:
    """记录每圈 (model/tools/turn_tail/turn_once) 的替身网关（同 current_turn 回归手法）。"""

    def __init__(self) -> None:
        self.config = {
            "models": {"main": {"provider": "openai", "model": "fake-main",
                                "temperature": 0.3}},
            "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                         "compress": "main", "draft": "main"},
            "system": {"default_model": "main"},
            "simple": {"enabled": False},
        }
        self.calls: list[dict] = []

    async def call(self, *a, **k):
        raise AssertionError("主路径应走 stream_call")

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None,
                          turn_tail=None, turn_once=None, **_):
        import asyncio
        await asyncio.sleep(0)
        self.calls.append({
            "model": model_name,
            "tools": [str(t.get("name")) for t in (tools or [])],
            "turn_tail": list(turn_tail or []),
            "turn_once": list(turn_once or []),
        })
        yield {"chunk_type": "content", "text": "好的"}
        yield {"content": "好的", "tool_calls": [], "truncated_tool_call": None,
               "finish_reason": "stop", "tokens_used": 1, "latency_ms": 1}


def _mk_client():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api import routes
    app = FastAPI()
    app.include_router(routes.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_op_turn_end_to_end_through_route(sandbox, monkeypatch):
    """端到端：POST …/messages 带 op ⇒ 专用注入进 turn_tail、专用工具进工具面、
    事件落 timeline、模型走主流程席；不带 op ⇒ 两者皆无（普通回合逐字不变）。"""
    from src.api import model_service

    wdata = await work_store.create_work("现状更新端到端", project_class="rd_project")
    wid, cid = wdata["work_id"], wdata["work_card_id"]
    await card_store.update_card(wid, cid, {"status_block": "当前确定状态：X 已定"}, "预置")

    gw = _RecordingGateway()
    monkeypatch.setattr(model_service, "_gateway", gw)

    async with _mk_client() as client:
        r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                              json={"content": "更新现状", "op": "update_status"})
        assert r.status_code == 200, r.text

    assert gw.calls, "应发生模型调用"
    c0 = gw.calls[0]
    assert "propose_status" in c0["tools"], "op 回合工具面应含专用工具（非卡片内挂）"
    tails = [str(m.get("content") or "") for m in c0["turn_tail"]]
    assert any(t.startswith("〔现状更新〕") for t in tails), "专用任务应随 turn_tail 下发"
    assert any("当前确定状态：X 已定" in t for t in tails), "注入正文应带当前现状全文"
    assert c0["turn_once"] == [], "op 回合不该注入判断协议"

    await event_log.drain()
    types = [x.get("type") for x in event_log.timeline(wid, cid)]
    assert types.count("status_update_task") == 1

    # ---- 控制组：不带 op ⇒ 无专用注入、无专用工具 ----
    gw.calls.clear()
    async with _mk_client() as client:
        r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                              json={"content": "随便聊聊"})
        assert r.status_code == 200, r.text
    assert gw.calls, "控制组也应发生模型调用"
    c1 = gw.calls[0]
    assert "propose_status" not in c1["tools"], "普通回合不该看见专用工具"
    assert not any(str(m.get("content") or "").startswith("〔现状更新〕")
                   for m in c1["turn_tail"]), "普通回合不该注入专用任务"


# ════════════════════════════════════════════════════════════════════
# 空块首次写入（2026-10-29）
# ════════════════════════════════════════════════════════════════════

async def test_edit_status_first_write_on_new_card(sandbox):
    """新建卡的现状块是骨架占位符（「（当前状况快照：…」）。

    old_string 给空串＝首次写入：占位符当空，直接落第一笔。
    """
    wid, cid = await _mk_work_with_card()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_edit_status(
        wid, cid, {"old_string": "", "new_string": "接口已定稿，能跑"}, cards)
    assert out.get("ok"), out
    card = await card_store.read_card(wid, cid)
    body = str((card.get("body") or {}).get("status_block") or "")
    assert "接口已定稿，能跑" in body, body

