# -*- coding: utf-8 -*-
"""**对话模块边界网**（[5715]）——分页 / 错误态 / 权限 / 无障碍。

前两张网（``assembler_run_scope`` / ``test_conversation_flow``）覆盖的是
「正常路径 ＋ 归属作用域」；这张补上**边界路径**——它们同样是产品面，
但上一轮完全没有断言保护。

| 面 | 覆盖点 | 判据出处 |
|---|---|---|
| **分页** | ``from_seq`` 取后半段不重不漏；全量取全；软删（revoke）后被撤段剔除 | ``event_log.timeline`` 文档 |
| **错误态** | 模型失败 ⇒ ``error`` 事件＋路由如实报 502；人停 ⇒ ``end=human_stop``；判不了交人 ⇒ ``end=policy_stop`` | ``session_facade.END_VALUES`` |
| **权限** | 无卡上下文 ⇒ 工具面空（fail-closed）；perms 收紧 ⇒ 写工具不下发；**工具面与执行层同源** | ``card_tools._filter_by_category`` 文档 |
| **无障碍** | 每个按钮有可访问名；纯符号按钮须有 ``aria-label``；``:focus-visible`` 有可见焦点环；触控目标 ≥24px | WCAG 2.1 A/AA 常见项 |

**不凑数**：每条断言都对着产品里写明的契约；写不出契约的（比如「跨 work
改卡」——本项目的 card_id 本就按 work 命名空间隔离，同名卡是两张不同的卡，
不存在越权面）就不立这条用例。

零外网：模型走 ``ScriptedGateway``（见 ``test_conversation_flow.py``）。

跑法::

    pytest tests/test_conversation_edges.py -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.api import control, model_service
from src.storage import card_store, event_log, session_facade

ROOT = Path(__file__).resolve().parents[1]
UI_CSS = (ROOT / "static" / "ui.css").read_text(encoding="utf-8")
CARD_HTML = (ROOT / "templates" / "card.html").read_text(encoding="utf-8")

#: 纯符号按钮判定：可见文本剥掉标签后只剩符号/标点 ⇒ 屏幕阅读器读不出名字。
_SYMBOL_ONLY = re.compile(r"^[\s×✕✖✗⨯+－\-—·•]+$")


def _scripted(script=None):
    """取一张 ``ScriptedGateway``（对话流程网里的模型替身）。"""
    from test_conversation_flow import ScriptedGateway

    return ScriptedGateway(script or [{"content": "答"}])


# ================================================================ 分页

async def test_paging_from_seq_returns_later_half_only(client, sandbox):
    """``from_seq`` 只给**之后**的记录，不重不漏。"""
    model_service._gateway = _scripted([{"content": "答一"}])
    wid = (await client.post("/api/works", json={"title": "分页剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "问一"})
    model_service._gateway = _scripted([{"content": "答二"}])
    await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "问二"})

    full = event_log.timeline(wid, "card_001")
    assert len(full) >= 4
    cut = full[len(full) // 2]["seq"]
    later = event_log.timeline(wid, "card_001", from_seq=cut)
    assert all(e["seq"] >= cut for e in later), "from_seq 之后不得混入更早的记录"
    assert len(later) < len(full), "分页必须真的少给"
    assert len(later) == len([e for e in full if e["seq"] >= cut]), "分页不得漏给"


async def test_paging_from_zero_returns_everything(client, sandbox):
    """``from_seq=0``（默认）⇒ 全量。"""
    model_service._gateway = _scripted([{"content": "答"}])
    wid = (await client.post("/api/works", json={"title": "全量剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "问"})
    full = event_log.timeline(wid, "card_001")
    assert full == event_log.timeline(wid, "card_001", from_seq=0)
    assert all(e["seq"] >= 0 for e in full)


async def test_soft_delete_revoked_segment_dropped_from_timeline(client, sandbox):
    """软删（revoke）：被撤段从 timeline 剔除，revoke 自身是控制位不进渲染面。"""
    model_service._gateway = _scripted([{"content": "答"}])
    wid = (await client.post("/api/works", json={"title": "软删剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "第一条"})

    items = event_log.timeline(wid, "card_001")
    first_user = next(e for e in items if e.get("type") == "user_message")
    cut = first_user["seq"]
    event_log.append_sync(wid, "card_001", {"type": "revoke", "data": {"from_seq": cut}})

    after = event_log.timeline(wid, "card_001")
    assert not any(e["seq"] >= cut and e.get("type") == "user_message" for e in after), \
        "被 revoke 覆盖的段必须从 timeline 剔除"
    assert not any(e.get("type") == "revoke" for e in after), "revoke 是控制位，不进渲染面"
    # 但**原始读**仍看得到（只追加、不物理删）——软删不是删档
    assert any(e.get("type") == "revoke" for e in event_log.read(wid, "card_001")), \
        "revoke 记录本身必须留在原始读里（只追加语义）"


# ================================================================ 错误态

async def test_model_failure_lands_error_event_and_502(client, sandbox):
    """模型失败 ⇒ 落 ``error`` 事件，路由**如实报 502**（不假装成功）。"""
    model_service._gateway = _scripted([{"content": "x"}])

    class _Boom:
        config = _scripted().config

        async def call(self, *a, **kw):
            raise RuntimeError("模拟模型炸了")

        async def stream_call(self, *a, **kw):
            raise RuntimeError("模拟模型炸了")
            yield  # pragma: no cover —— 让它成为 async generator

    model_service._gateway = _Boom()
    wid = (await client.post("/api/works", json={"title": "炸了剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "问"})
    assert r.status_code == 502, f"模型失败应如实报 502：{r.status_code}"

    # 事件落盘走后台任务（event_stream.emit → event_log.append_bg），
    # 错误事件是最后一条 ⇒ 读盘前必须 drain，否则读到的是「还没写进去」的中间态。
    await event_log.drain()
    items = event_log.timeline(wid, "card_001")
    assert any(e.get("type") == "error" for e in items), "失败必须落 error 事件"
    err = next(e for e in items if e.get("type") == "error")
    assert "模拟模型炸了" in str((err.get("data") or {}).get("error") or ""), \
        "error 事件要带上原因（界面据此显示失败行）"


async def test_failed_turn_does_not_claim_model_done(client, sandbox):
    """失败回合**不得**在会话里留下 ``end=model_done``（那等于谎报成功）。

    判据不钉「必须落 end=error」——那会改动模型上下文（下一轮会读到一条
    失败块），属于产品口径未裁的事；这里只钉**不说谎**这一半。
    """
    model_service._gateway = _scripted([{"content": "x"}])

    class _Boom:
        config = _scripted().config

        async def call(self, *a, **kw):
            raise RuntimeError("模拟模型炸了")

        async def stream_call(self, *a, **kw):
            raise RuntimeError("模拟模型炸了")
            yield  # pragma: no cover

    model_service._gateway = _Boom()
    wid = (await client.post("/api/works", json={"title": "不谎报剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "问"})

    sess = (await client.get(f"/api/works/{wid}/cards/card_001/session")).json()
    ends = [str(m.get("end") or "") for m in sess if m.get("role") == "assistant"]
    assert "model_done" not in ends, f"失败回合不得谎报 model_done：{ends}"


async def test_human_stop_lands_human_stop_end(client, sandbox):
    """人停 ⇒ 会话块 ``end=human_stop``（且只作用于当轮）。"""
    model_service._gateway = _scripted([{"content": "被打断"}])
    wid = (await client.post("/api/works", json={"title": "人停剧本"})).json()["work_id"]

    # 人停置位要**有在途回合**（控制面口径：无在途回合＝幂等 no-op），
    # 故先占住该卡互斥体再置位，置完立刻释放。
    owner = "turn:edge-test"
    assert session_facade.turn_try_acquire(wid, "card_001", owner) is True
    try:
        res = control.request_stop(wid, "card_001")
        assert res["stopped"] is True, f"在途回合里人停应置位：{res}"
    finally:
        session_facade.turn_release(wid, "card_001", owner)

    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "问"})
    assert r.status_code in (200, 502), r.text
    sess = (await client.get(f"/api/works/{wid}/cards/card_001/session")).json()
    ends = [str(m.get("end") or "") for m in sess if m.get("role") == "assistant"]
    assert "human_stop" in ends, f"人停必须留痕 end=human_stop：{ends}"


async def test_policy_stop_lands_policy_stop_end(client, sandbox, monkeypatch):
    """判不了交人 ⇒ 会话块 ``end=policy_stop``。

    迭代三的注册表判据位还空着（``control.check`` 恒 ``CONTINUE``），
    消费侧路由保留着。此处把判据顶出来，验的是**消费侧接线**（不是判据本身）。
    """
    monkeypatch.setattr(control, "check", lambda work_id, card_id: control.DEFER_HUMAN)
    model_service._gateway = _scripted([{"content": "交人"}])
    wid = (await client.post("/api/works", json={"title": "交人剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages", json={"content": "问"})

    sess = (await client.get(f"/api/works/{wid}/cards/card_001/session")).json()
    ends = [str(m.get("end") or "") for m in sess if m.get("role") == "assistant"]
    assert "policy_stop" in ends, f"判不了交人必须留痕 end=policy_stop：{ends}"


# ================================================================ 权限

async def test_no_card_context_yields_empty_tool_face(client, sandbox):
    """认不出这张卡是谁 ⇒ **工具面为空**（默认关门，fail-closed）。

    口径出处：``card_tools._filter_by_category`` 文档「无卡上下文 ⇒ 默认关门」。
    """
    from src.api import card_tools

    model_service._gateway = _scripted([{"content": "x"}])
    wid = (await client.post("/api/works", json={"title": "关门剧本"})).json()["work_id"]
    assert card_tools.build_tool_schemas(card_id="card_001", work_id=wid) == [], \
        "无卡上下文时不得下发任何工具（fail-closed）"
    assert card_tools.build_tool_schemas(card_id="card_001", work_id=wid,
                                          cards={}) == [], \
        "卡不在 cards 里同样关门"


async def test_perms_drop_removes_write_tools_but_keeps_read_tools(client, sandbox):
    """perms 收紧 ⇒ 写工具**不下发**（模型压根看不见），读工具仍在。

    口径出处：``_filter_by_category``「用户裁定：没有权限的卡片应该压根看不见
    对应的工具」。
    """
    from src.api import card_tools

    model_service._gateway = _scripted([{"content": "x"}])
    wid = (await client.post("/api/works", json={"title": "收权剧本"})).json()["work_id"]
    metas = {cid: dict(m) for cid, m in card_store.iter_card_metas(wid).items()}
    root = next(iter(metas))

    full = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=root, work_id=wid, cards=metas)}
    assert {"edit_card", "create_card"} <= full, "默认 perms 下写工具应在（前置断言）"

    metas[root]["perms"] = [p for p in metas[root]["perms"]
                            if p not in ("edit", "create_child")]
    tight = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=root, work_id=wid, cards=metas)}
    assert "edit_card" not in tight, "去掉 edit 权限后 edit_card 不得下发"
    assert "create_card" not in tight, "去掉 create_child 权限后 create_card 不得下发"
    assert "read_card" in tight, "收权只收写面，读面必须还在"


async def test_execution_layer_denies_tool_without_perm(client, sandbox):
    """工具面与执行层**同源**：看不见的工具，调下去也必须被拒（且不落盘）。"""
    from src.api import card_tools

    model_service._gateway = _scripted([{"content": "x"}])
    wid = (await client.post("/api/works", json={"title": "同源剧本"})).json()["work_id"]
    metas = {cid: dict(m) for cid, m in card_store.iter_card_metas(wid).items()}
    root = next(iter(metas))
    metas[root]["perms"] = [p for p in metas[root]["perms"] if p != "edit"]

    before = (await client.get(f"/api/works/{wid}/cards/{root}")).json()
    res = await card_tools.execute_tool(wid, root, "edit_card",
                                        {"card_id": root, "content": "偷改"}, metas)
    assert res.get("ok") is False, f"无 edit 权限时 edit_card 必须被拒：{res}"
    assert (res.get("error") or {}).get("code") == "denied", \
        f"拒绝码应是 denied：{res.get('error')}"
    after = (await client.get(f"/api/works/{wid}/cards/{root}")).json()
    assert json.dumps(after, ensure_ascii=False, sort_keys=True) == \
        json.dumps(before, ensure_ascii=False, sort_keys=True), \
        "被拒的工具调用不得改动任何卡面"


# ================================================================ 无障碍

def _buttons(html: str) -> list[tuple[str, str, str]]:
    """抽出 (id, 整段标签, 内部可见文本)。"""
    out = []
    for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", html, re.S):
        attrs, inner = m.group(1), m.group(2)
        bid = re.search(r'id="([^"]+)"', attrs)
        out.append((bid.group(1) if bid else "", m.group(0), inner))
    return out


def test_every_button_has_accessible_name():
    """每个按钮都得有可访问名（``aria-label`` / ``aria-labelledby`` / 可见文本 / ``title`` 四选一）。

    屏幕阅读器只认这几种来源；纯图标按钮没名字＝读出来是「按钮」。
    """
    missing = []
    for bid, tag, inner in _buttons(CARD_HTML):
        text = re.sub(r"<[^>]+>", "", inner)
        text = re.sub(r"\s+", " ", text).strip()
        named = ("aria-label" in tag or "aria-labelledby" in tag
                 or "title=" in tag or bool(text))
        if not named:
            missing.append(bid or "(无 id)")
    assert not missing, f"这些按钮没有可访问名：{missing}"


def test_symbol_only_button_has_aria_label():
    """**纯符号**按钮（可见文本只有 "×" 之类）必须补 ``aria-label``。

    "×" 会被读成「乘号」，读屏用户听到的是「乘号 按钮」——不是「关闭通知」。
    """
    bad = []
    for bid, tag, inner in _buttons(CARD_HTML):
        text = re.sub(r"<[^>]+>", "", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or not _SYMBOL_ONLY.match(text):
            continue          # 纯图标（无文本）或带实义文字的按钮另说
        if "aria-label" not in tag and "aria-labelledby" not in tag:
            bad.append((bid or "(无 id)", text))
    assert not bad, f"纯符号按钮缺 aria-label：{bad}"


def test_focus_visible_has_visible_ring():
    """键盘焦点必须看得见（``:focus-visible`` 有 outline，且没被显式关掉）。"""
    m = re.search(r"body\.page-chat\s+:focus-visible\s*\{([^}]*)\}", UI_CSS)
    assert m, "对话页应能定位 :focus-visible 规则"
    rule = m.group(1)
    assert "outline" in rule, f"焦点环必须有 outline：{rule.strip()}"
    assert not re.search(r"outline\s*:\s*(none|0)\b", rule), \
        f"焦点环不能被显式关掉：{rule.strip()}"


def test_touch_targets_not_too_small():
    """顶栏主按钮的触控目标不小于 24px（顶栏动作区是触屏最常点的地方）。"""
    m = re.search(r"body\.page-chat\s+\.tb-btn\s*\{([^}]*)\}", UI_CSS)
    assert m, "应能定位 .tb-btn 规则"
    rule = m.group(1)
    w = re.search(r"width:\s*(\d+)px", rule)
    h = re.search(r"height:\s*(\d+)px", rule)
    assert w and h, f".tb-btn 应有显式宽高：{rule.strip()}"
    assert int(w.group(1)) >= 24 and int(h.group(1)) >= 24, \
        f"顶栏按钮 {w.group(1)}x{h.group(1)} 小于 24px 触控下限"
