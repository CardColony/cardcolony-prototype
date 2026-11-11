# -*- coding: utf-8 -*-
"""卡片底座第一步 · 沿关系的数据访问与通信（V1–V5 验收网）。

覆盖任务书验收标准：
- V1 A 沿通信线能读 B 的正文／影子／对话（窗口摘要）；
  断线（unlink 后）read_via 返回「无通信线」；
- V2 read_via 未授 read_linked 时被 policy 拒（denied 信封）；
- V3 A 发消息 → 收件箱出现待处理消息；事件流照旧有 msg_* 事件；
- V4 B 回合被调起、模型在激励文里明确看到待处理消息正文、回复；
     回复后读取水位推进（不删行）；
- V5 新消息不进对话块（session 无新 card: 块）；B 的 assistant 回复仍进对话块。

「复用不重做」的判据：读的三路各映射到现成读工具（read_card / read_shadow_raw /
对话窗口摘要）——本文件只验「沿边寻址」这一步加上去之后的行为。

★ 号位注意：sub_project 建卡即派生伴生评审卡（占一个 card_id）⇒ 子卡A＝card_002、
孙卡A1＝card_004（card_003/005 是各自的评审卡）。旧断言里的 card_003 全部顺移。
"""
from __future__ import annotations

import asyncio
import json

from src.api import card_tools, card_msg, message_runner, model_service
from src.storage import (card_db, card_store, event_log, session_facade,
                         work_store)
from conftest import prime_card_session


# ══════════════ 夹具 ══════════════

async def _tree(wid: str) -> dict:
    """根卡 card_001（create_work 自带）→ 002=子卡A（父001）→ 004=孙卡A1（父002）。"""
    await card_store.create_card(wid, "子卡A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "孙卡A1", "用户直接创建",
                                 card_type="sub_project", parent="card_002")
    # 按生产形态铺会话（建卡即铺；测试直建卡不带）——见 conftest.prime_card_session
    for _cid in ("card_002", "card_004"):
        prime_card_session(wid, _cid)
    return card_store.iter_card_metas(wid)


async def _wait_for(predicate, timeout_s: float = 6.0) -> bool:
    for _ in range(int(timeout_s / 0.05)):
        await asyncio.sleep(0.05)
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
    return False


# ══════════════ V1 · 沿通信线读（正文／影子／对话）＋ 断线失效 ══════════════

async def test_v1_read_via_three_parts_and_unlink(sandbox):
    """A(card_002) 连线到 B(card_004) 后，三路可读；unlink 后返回「无通信线」。"""
    wid = (await work_store.create_work("V1沿读剧本"))["work_id"]
    cards = await _tree(wid)
    a_id, b_id = "card_002", "card_004"

    # 建立通信线 A → B（004 是 002 的子卡，link 开关即可）
    r = await card_tools._h_link_cards(
        wid, a_id, {"target_id": b_id, "type": "关联"}, cards)
    assert "error" not in r, r

    # 无连线前：未连的目标 → 无通信线（评审卡 card_003 与 002 无边）
    out0 = await card_tools.read_via(wid, a_id, "card_003", "正文")
    assert out0["ok"] is False and "无通信线" in out0["message"]

    # ① 正文（复用 card_store.read_card 三块＋关系位置）
    out = await card_tools.read_via(wid, a_id, b_id, "正文")
    assert out["ok"] is True and out["part"] == "正文"
    assert {"title", "mission", "status_block", "requirements",
            "relations", "relations_in"} <= set(out["value"])

    # ② 影子（复用 shadow_store.read_shadow_raw）
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "B 的既有话", "end": "model_done"})
    out = await card_tools.read_via(wid, a_id, b_id, "影子")
    assert out["ok"] is True and isinstance(out["value"], str)

    # ③ [2026-10-25] 对话 part 保留但**只返尾部窗口摘要**——全量 timeline
    # 实测两张卡合计 287 万字符顶爆上下文；摘要看进展够用（不搬历史）。
    out = await card_tools.read_via(wid, a_id, b_id, "对话")
    assert out["ok"] is True and isinstance(out["value"], dict)
    assert "events" in out["value"] and "note" in out["value"]
    assert card_tools.READ_VIA_PARTS == ("正文", "影子", "对话")

    # part 非法 → unknown_part（不抛）
    out = await card_tools.read_via(wid, a_id, b_id, "天书")
    assert out["ok"] is False and out["reason"] == "unknown_part"

    # 断线：unlink 后 read_via 返回「无通信线」
    r = await card_tools._h_unlink_cards(wid, a_id, {"target_id": b_id}, cards)
    assert "error" not in r, r
    out = await card_tools.read_via(wid, a_id, b_id, "正文")
    assert out["ok"] is False and out["reason"] == "no_link"
    assert "无通信线" in out["message"]


# ══════════════ V2 · 未授 read_linked 被拒（denied 信封） ══════════════

async def test_v2_read_linked_denied_without_perm(sandbox):
    """目标卡 perms 不含 read_linked → handler 回 denied 信封（同现状口径）。"""
    wid = (await work_store.create_work("V2越权剧本"))["work_id"]
    cards = {
        "card_002": {"card_id": "card_002", "card_type": "sub_project",
                     "parent": "card_001", "perms": ["read"]},
        "card_003": {"card_id": "card_003", "card_type": "sub_project",
                     "parent": "card_002", "perms": ["read"]},
    }
    r = await card_tools._h_read_linked(
        wid, "card_002", {"target_id": "card_003", "part": "正文"}, cards)
    assert r["error"]["code"] == "denied", r


async def test_v2_read_linked_granted_reads(sandbox):
    """目标卡 perms 含 read_linked ＋ 有连线 → 读到（正例，走 handler 全链）。"""
    wid = (await work_store.create_work("V2正例剧本"))["work_id"]
    cards = await _tree(wid)
    await card_tools._h_link_cards(
        wid, "card_002", {"target_id": "card_004", "type": "关联"}, cards)
    r = await card_tools._h_read_linked(
        wid, "card_002", {"target_id": "card_004", "part": "正文"}, cards)
    assert "error" not in r, r
    assert r["part"] == "正文" and "title" in r["value"]


# ══════════════ V3/V4/V5 · 通信闭环端到端 ══════════════

async def test_v3_v4_v5_loop_end_to_end(sandbox, monkeypatch):
    """[消息落会话块 2026-10-26/27 · 用户裁定] inbox 已取消，语义改写：

    - V3：信落 B 的**会话块**（actor=``card:<发件卡>``，块头带 msg_id/类型），
      事件流照旧有 msg_* 事件（收件方键）；messages 表**不写**（inbox 取消）。
    - V4：发信即唤醒——``deliver_now`` 立即起 B 一轮（text 模式：唤醒文只
      指路，正文由上下文历史/会话块承载）；不落 user 块伪装人声。
    - V5：消息**就是**会话块（历史的一部分，参与凝练）；B 的 assistant
      回复同入会话块。
    """
    wid = (await work_store.create_work("V345闭环剧本"))["work_id"]
    await _tree(wid)
    a_id, b_id = "card_002", "card_004"
    captured: list[dict] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        captured.append({"user": str(user_prompt),
                         "prior": prior_messages})
        return ({"content": "B 的回答：第二节数字为 42。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    # conftest 默认关自动投递（ECO_NO_AUTO_DELIVER=1，防老用例时序抖动）；
    # 本用例恰恰要验「发信即唤醒」⇒ 摘掉开关。
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)

    # A 沿通信线发消息（系统薄壳：查线不查开关；002→004 有父子默认线）
    r = await card_tools.send_card_message(
        wid, a_id, target_id=b_id, msg_type="msg_ask", content="第二节数字是多少？")
    msg_id = r["msg_id"]

    # V3：信落 B 的会话块——actor=card:<发件卡>、块头带 msg_id 与类型、正文在块里
    blocks = await session_facade.read(wid, b_id)
    msg_blocks = [b for b in blocks
                  if str(b.get("actor") or "") == f"card:{a_id}"]
    assert len(msg_blocks) == 1, blocks
    _ct = str(msg_blocks[0].get("content") or "")
    assert msg_id in _ct and "第二节数字是多少？" in _ct
    # messages 表不写（inbox 已取消——没有 pending/水位这套状态机）
    _rows = await card_db.run_read(
        wid, lambda c: [dict(x) for x in c.execute(
            "SELECT msg_id FROM messages WHERE target_card_id=?",
            (b_id,)).fetchall()], [])
    assert _rows == [], _rows
    # V3：事件流照旧有 msg_* 事件（收件方键）
    evs = [e for e in event_log.read(wid, b_id) if e.get("type") == "msg_ask"]
    assert evs, "事件流应照旧留痕 msg_*"
    assert msg_id in json.dumps(evs[-1], ensure_ascii=False)

    # V4：发信即唤醒（deliver_now 立即起一轮，不等回合收口兜底）
    assert await _wait_for(lambda: bool(captured)), "B 应被立即调起"
    assert "你收到一条来自 card_002 的消息" in captured[-1]["user"]
    # B 在 prior 会话历史里看得到消息正文（卡间块映射成 role=user 进上下文）
    _prior_text = json.dumps(captured[-1]["prior"], ensure_ascii=False)
    assert "第二节数字是多少？" in _prior_text, captured[-1]["prior"]

    # V5：B 的 assistant 回复进会话块；来信块与回复同档留存（都是历史）
    async def _b_replied():
        return any(b.get("actor") == "assistant"
                   for b in await session_facade.read(wid, b_id))

    assert await _wait_for(_b_replied), "B 应回复"
    blocks_b = await session_facade.read(wid, b_id)
    assert any(b.get("actor") == "assistant"
               and "B 的回答" in str(b.get("content") or "") for b in blocks_b)
    assert any(str(b.get("actor") or "") == f"card:{a_id}" for b in blocks_b)
    # 激励文不落 user 块（不伪装人声）
    assert not any(b.get("actor") == "human"
                   and "你收到一条来自" in str(b.get("content") or "")
                   for b in blocks_b)


# ══════════════ 工具面 · read_linked 注册与开关 ══════════════

def test_read_linked_tool_registered_and_readonly():
    assert "read_linked" in card_tools.TOOL_REGISTRY
    assert "read_linked" in card_tools.READONLY_TOOLS
    assert card_tools.TOOL_SWITCH["read_linked"] == "read_linked"
    # [worker 卡 2026-10-15] tc-4 → tc-5（新增 report_done / create_worker_card 两件）。
    # [规则卡 2026-10-15] tc-5 → tc-6（新增 draft_rule_edit ＋第四段根卡闸）。
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel 两件，
    # 依赖关联线；通信线 relations→links 改名）。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-27"  # [2026-11-01] 随现配置对齐
