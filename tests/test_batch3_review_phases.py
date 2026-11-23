# -*- coding: utf-8 -*-
"""批三验收（2026-11-20 · 成本优化任务书 §四·第三批）：评审席按阶段缩量。

试点：普通需求评审 ``requirements`` 的 ``confirm``（增量确认）与
``vote_nudge``（同版补票）——只发完成本次表态所需的材料。
开关 ``REVIEW_CONTEXT_PHASES=1``，缺省沿用现行完整装配。

证据口径与批二同一把尺：``httpx.AsyncClient.send`` 抓**适配器实际出站**的请求
正文（首跳／工具续跳／同位置重试全覆盖），不拿模板或手拼字符串代替。

覆盖 §四 复验清单：
- 增量确认：正式投票工具建旧版通过票 → 修稿 → 正式步骤进 confirm；
  两处独立改动都在出站 diff 内；原票/评审依据/当前稿次完整保留；
  通过与否决两路回放；票落账可读回 评审/批次/版本/席位。
- 同版补票：同版真实发言未投票 → runner 选 vote_nudge；首跳带原意见
  （发言锚）＋对应草稿原文片段＋待投票态；一次真实读取＋一次同跳重试
  的材料一致；投票回执、工具配对可查。
- 回退与过期：缺快照／空 diff／改稿未推版（暂缓）／发言缺锚／旧稿意见／
  条款定位失败／新否决依据／用户新裁定——逐例核对选材或过期原因。
- 并发与用户控制：装配后换批/改稿（含同稿次正文变更）⇒ 旧材料的票
  在写事务内被拒落；真实停止入口零新增请求；插话进下一有效请求。
- 边界与权限：speak/re_eval/对齐/修订/定稿/普通回合材料与流程不变；
  席位实际工具表＝能力∩场授；跨卡读草稿与非成员投票被真实拒。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from outbound_probe import (CHILD_MARK, LIST_MARK, OutboundRecorder,
                            frame_finish, frame_text, frame_tool, sse)

ROOT = Path(__file__).resolve().parents[1]
ENV = "REVIEW_CONTEXT_PHASES"


@pytest.fixture(autouse=True)
def _stub_keys(monkeypatch):
    """替身用的假 key（请求体不受影响，**不出网**：httpx 已被拦）。"""
    for env in ("OPENDESIGN_API_KEY", "VOLCENGINE_CODING_API_KEY",
                "ZHIPU_CODING_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.setenv(env, "stub-key-not-used")


# ============================================================
# 世界构造：长草稿＋长历史＋子卡＋场内纠正的基线
# ============================================================

def _block(name: str, mark: str, pad: int = 40) -> str:
    return (f"### {name}\n"
            f"{mark}\n" + ("本块正文铺垫行。" * pad))


def _draft_v1() -> str:
    return "\n".join([
        _block("目标与范围", "本卡目标是〈目标标记001〉验收链路闭环。"),
        _block("功能需求", "- 功能A：〈功能标记002〉按判据核对。\n- 功能B：〈功能乙003〉照常。"),
        _block("验收标准", "1. 〈验收标记004〉逐项可核。"),
        _block("非目标", "不做跨树移植。"),
    ])


def _draft_v2() -> str:
    """两处独立改动：功能需求块加行、验收标准改判据——diff 两块都要在。"""
    return "\n".join([
        _block("目标与范围", "本卡目标是〈目标标记001〉验收链路闭环。"),
        _block("功能需求", "- 功能A：〈功能标记002〉按判据核对。\n- 功能B：〈功能乙003〉照常。\n- 功能C：〈新增改动标记C〉补票场景专项。"),
        _block("验收标准", "1. 〈验收标记004〉逐项可核。\n2. 〈新增改动标记D〉判据收紧版。"),
        _block("非目标", "不做跨树移植。"),
    ])


USER_RULING_TEXT = "用户裁定：功能C 的验收口径以正稿 A2 为准。"
BRIEF_TEXT = "【任务背景】评审 R 系列草稿\n【要评审的内容】两处新改动"


async def _mk_world(sandbox, monkeypatch, *, n_fn: int = 2) -> dict[str, Any]:
    """建基线世界：目标卡（长草稿+长历史）＋两席位职能＋场内评审批。"""
    from src.api import card_tools, review_runner  # noqa: F401
    from src.storage import (card_store, function_store, review_store,
                             session_facade, work_store)

    wdata = await work_store.create_work("批三评审缩量")
    wid = str(wdata["work_id"])
    root = str(wdata["work_card_id"])
    fids = []
    for i, code in enumerate(("ceo", "pm", "cto")[:n_fn]):
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": code}})
        fids.append(str(prof.get("function_id") or ""))
    r = await card_store.create_card(wid, "被评子卡", "用户直接创建",
                                     card_type="sub_project", parent=root)
    cid = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"requirements": "R0 正稿",
                   "requirements_draft": _draft_v1(),
                   "requirements_state": "draft",
                   "status_block": "现状：评审中。" * 60}, "铺前置")
    # 被评卡长历史（缩量对象之一）
    for i in range(30):
        await session_facade.append(
            wid, cid, {"role": "user", "content": f"历史提问{i}「长历史标记」" + "水" * 200})
        await session_facade.append(
            wid, cid, {"role": "assistant", "content": f"历史答复{i}" + "水" * 200})
    # 目标卡的直接子卡 ×3（子卡现状区材料——受限步整个区不出站）
    for i in range(3):
        child = await card_store.create_card(wid, f"目标子卡{i}", "用户直接创建",
                                             card_type="sub_project", parent=cid)
        kid = str(child["meta"]["card_id"])
        await card_store.update_card(
            wid, kid, {"status_block": "子卡现状标记。" + f"细节{i}。" * 400}, "铺子卡")
    # 兄弟卡 ×6（卡片清单区材料——受限步整棵清单不出站）
    for i in range(6):
        sib = await card_store.create_card(wid, f"兄弟卡{i}", "用户直接创建",
                                           card_type="sub_project", parent=root)
        sid_c = str(sib["meta"]["card_id"])
        await card_store.update_card(
            wid, sid_c, {"status_block": f"兄弟{i}现状。" + "铺陈。" * 200}, "铺兄弟")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "focus": "R 收尾", "brief": BRIEF_TEXT}, cards)
    rid = str(out["data"]["review_card_id"])
    # [两轮评审] 过对齐批：总裁 approve ＋ 一步 advance ⇒ 三席正式批
    await card_tools._h_approve(wid, root, {"review_card_id": rid}, cards)
    await review_runner._run_step(wid, rid)
    st = await review_store.state_of(wid, rid)
    assert str(st.get("type") or "") == "requirements", st
    assert [str(m) for m in (st.get("members") or [])] == fids[:len(st.get("members") or [])]
    # 场内用户裁定（有效用户发言——缩量后仍须在席）
    await session_facade.append(
        wid, rid, {"role": "user", "content": USER_RULING_TEXT})
    # 席位模型绑定到生产在用的 ECO 座（openai 兼容直连，出站可抓）
    from src.storage import model_override as _mo
    for f in fids:
        _mo.put_row(wid, f, {"model": "ds-v41-flash-eco-amr"}, src="manual")
    # ECO_NO_AUTO_REVIEW 保留（conftest 默认设）：全部步骤由本测试显式
    # run_once/_seat_turn 驱动——resume_after_vote 不自动续跑，出站全在
    # 抓取器装机后发生，绝无后台回合打向真实端点。
    return {"wid": wid, "root": root, "cid": cid, "rid": rid,
            "fids": fids, "batch": str(st.get("batch") or ""),
            "cards": cards}


def _fp(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


async def _edit_draft(wid: str, cid: str, text: str) -> None:
    from src.storage import card_store
    await card_store.update_card(wid, cid, {"requirements_draft": text}, "修稿")


async def _sync(wid: str, rid: str) -> bool:
    from src.storage import review_store
    return await review_store.sync_draft(wid, rid)


async def _venue_append(wid: str, rid: str, role: str, content: str,
                        **extra) -> None:
    from src.storage import session_facade
    blk = {"role": role, "content": content}
    blk.update(extra)
    await session_facade.append(wid, rid, blk)


# ============================================================
# 出站脚本：按计划回帧（真实读取 → 投票 → 收尾；或文本收口）
# ============================================================

class _Plan:
    """按 hop 序回帧：`steps[i]` 给出第 i 次出站的 SSE 帧列表；
    用尽的 hop 之后默认收文本。``fail_first``＝首试断连触发同位置重发。"""

    def __init__(self, steps: list[list[dict]], *, fail_first: bool = False):
        self.steps = steps
        self.fail_first = fail_first
        self.served = 0
        self.hops = 0

    def __call__(self, index: int, body: dict) -> Any:
        self.hops += 1
        if self.fail_first and self.served == 0:
            self.served += 1
            raise httpx.ConnectError("（替身）首试断连——触发同位置重发")
        i = index - (1 if self.fail_first else 0)
        frames = self.steps[min(max(i, 0), len(self.steps) - 1)]
        return sse(*frames)


def _approve_frames(rid: str, model: str = "fake-model") -> list[dict]:
    return [frame_tool("call_v1", "approve", {"review_card_id": rid}, model=model),
            frame_finish("tool_calls", model=model)]


def _reject_frames(rid: str, model: str = "fake-model") -> list[dict]:
    return [frame_tool("call_v1", "reject", {"review_card_id": rid}, model=model),
            frame_finish("tool_calls", model=model)]


def _read_frames(cid: str, model: str = "fake-model") -> list[dict]:
    return [frame_tool("call_r1", "read_card", {"card_id": cid}, model=model),
            frame_finish("tool_calls", model=model)]


def _text_frames(text: str, model: str = "fake-model") -> list[dict]:
    return [frame_text(text, model=model), frame_finish("stop", model=model)]


async def _step_once(wid: str, rid: str, script, monkeypatch) -> tuple[Any, OutboundRecorder]:
    """装抓取器 → 沿正式入口 run_once 跑一步 → 返回 (结果, 记录)。

    ``ECO_NO_AUTO_REVIEW`` 只在 ``await run_once`` 期间撤、返回即恢复——
    回合内 ``resume_after_vote`` 撞 ``_RUNNING`` 即回不连锁；**不恢复的话**
    同用例后续建场（``_h_approve`` → resume_after_vote）会起后台 run_once
    抢跑步骤、打向真实端点。"""
    from src.api import review_runner
    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    try:
        r = await review_runner.run_once(wid, rid)
    finally:
        monkeypatch.setenv(review_runner.ENV_NO_AUTO, "1")
    return r, rec


def _joined_all(rec: OutboundRecorder) -> str:
    return "\n".join(rec.joined(i) for i in range(len(rec.requests)))


def _tool_names(rec: OutboundRecorder, index: int) -> list[str]:
    body = rec.requests[index]["body"] or {}
    return [str((t.get("function") or {}).get("name") or t.get("name") or "")
            for t in body.get("tools") or []]


# ============================================================
# 一、增量确认（confirm）
# ============================================================

@pytest.mark.asyncio
async def test_confirm_lite_outbound_and_receipt(sandbox, monkeypatch):
    """confirm 轻量装配：两处独立改动都在出站 diff 内；原票稿次/评审依据/
    当前稿次/席位职责保留；清单/子卡/长历史/整篇草稿不出站；票落 v2。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store, session_facade
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    # 正式投票工具落 v1 通过票（snapshot=v1 原文）
    cards = w["cards"]
    from src.api import card_tools
    a = await card_tools._h_approve(wid, f0, {"review_card_id": rid}, cards)
    assert a.get("ok"), a
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)     # 其余席直接落票（同版）
    # 修稿 → 收口推版 v2 → promote 撞闸挂 drift
    await _edit_draft(wid, cid, _draft_v2())
    assert await _sync(wid, rid), "版本应推进到 v2"
    await review_store.set_drift_pending(wid, rid, [f0])
    # 正式步骤进 confirm（回放：通过票）
    script = _Plan([_approve_frames(rid), _text_frames("增量不动摇我的通过。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert r.get("kind") == "confirm" or "confirm" in str(
        (r.get("_done") or {}).get("reason") or ""), r
    assert len(rec.requests) >= 2, "应有首跳＋工具续跳"
    first = rec.joined(0)
    # 必须给到的材料
    assert "增量确认" in first
    assert BRIEF_TEXT.split("\n")[0] in first, "评审依据（brief）须在"
    assert "approve" in first and "v1" in first, "原票与其稿次须在"
    assert "v2" in first, "现稿版本须在"
    assert "〈新增改动标记C〉" in first and "〈新增改动标记D〉" in first, \
        "两处独立改动都要在 diff 内"
    # 不该出现的材料
    assert LIST_MARK not in first, "整棵清单不该出站"
    assert CHILD_MARK not in first, "子卡现状不该出站"
    assert "长历史标记" not in first, "被评卡长历史不该出站"
    assert "本卡目标是〈目标标记001〉" not in first or "diff" in first, \
        "未触及的远块不该整篇出站（diff 外的无关段）"
    # 真实投票回执：票落当前稿 v2
    st = await review_store.status(wid, rid)
    v = (st.get("votes") or {}).get(f0) or {}
    assert v.get("verdict") == "approve" and int(v.get("draft_ver") or 0) == 2, st.get("votes")
    # 发言锚：confirm 轮的回复块带材料身份锚（实际送给席位的 v2）
    blocks = await session_facade.read(wid, rid) or []
    anchors = [b.get("review_anchor") for b in blocks
               if isinstance(b.get("review_anchor"), dict)]
    assert anchors, "席位发言块应带 review_anchor"
    a0 = anchors[-1]
    assert a0.get("step") == "confirm" and a0.get("seat") == f0 \
        and int(a0.get("draft_ver") or 0) == 2 \
        and a0.get("draft_fp") == _fp(_draft_v2()), a0


@pytest.mark.asyncio
async def test_confirm_reject_path_lands_reject(sandbox, monkeypatch):
    """confirm 回放否决 ⇒ 真票落 reject（流程不变）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    await _edit_draft(wid, cid, _draft_v2())
    await _sync(wid, rid)
    await review_store.set_drift_pending(wid, rid, [f0])
    script = _Plan([_reject_frames(rid), _text_frames("改动动摇通过：功能C无判据。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    st = await review_store.status(wid, rid)
    v = (st.get("votes") or {}).get(f0) or {}
    assert v.get("verdict") == "reject", st.get("votes")


@pytest.mark.asyncio
async def test_confirm_identical_shows_both_versions(sandbox, monkeypatch):
    """两份可信原文逐字相同 ⇒ 显示「正文一致」并并列两版（仍走投票流程）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # v1 → v2 改动 → v3 还原：现稿与原票快照**逐字相同**、稿次已推进
    await _edit_draft(wid, cid, _draft_v2())
    await _sync(wid, rid)
    await _edit_draft(wid, cid, _draft_v1())
    await _sync(wid, rid)
    st = await review_store.state_of(wid, rid)
    assert int(st.get("draft") or 0) == 3, st.get("draft")
    await review_store.set_drift_pending(wid, rid, [f0])
    script = _Plan([_approve_frames(rid), _text_frames("正文一致，维持通过。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    first = rec.joined(0)
    assert "逐字相同" in first and "正文一致" in first, first[:400]
    assert "v1" in first and "v3" in first, "两版版本要并列"


@pytest.mark.asyncio
async def test_confirm_fallback_missing_snapshot(sandbox, monkeypatch):
    """旧票快照缺失 ⇒ 回退完整装配（清单等材料照常出站）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import card_db, review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # 把 f0 的票快照抠掉（缺证场景）
    batch = w["batch"]
    def _blank(conn):
        conn.execute("UPDATE review_approvals SET draft_snapshot=''"
                     " WHERE work_id=? AND batch_id=? AND role_id=?",
                     (wid, batch, f0))
    await card_db.run_write(wid, _blank)
    await _edit_draft(wid, cid, _draft_v2())
    await _sync(wid, rid)
    await review_store.set_drift_pending(wid, rid, [f0])
    script = _Plan([_approve_frames(rid), _text_frames("看完了。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    first = rec.joined(0)
    assert LIST_MARK in first, "缺快照必须回退完整装配（清单应在）"
    # [返修④c] 缺快照的确认＝**完整重评**：不许再发「只评增量」的空差事，
    # 当前全文与原评审条件必须给齐。
    assert "只评增量" not in first, "缺快照还发「只评增量」＝空差事"
    assert "重新评审" in first or "重新评" in first, "差事要如实写重评"
    assert BRIEF_TEXT.split("\n")[0] in first, "原评审条件（brief）不许漏"
    assert "〈功能标记002〉" in first and "〈验收标记004〉" in first, \
        "当前全文必须整篇在"


@pytest.mark.asyncio
async def test_defer_when_fp_unsynced_then_proceed(sandbox, monkeypatch):
    """内容已变但稿次未推进（fp≠登记指纹）⇒ 席位步暂缓、零出站；
    推版后同一步照常跑（意见跨版 ⇒ 回退完整装配）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # f0 同版发言未投票 ⇒ 下一动作＝vote_nudge（席位步，进材料快照链）
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    # 直接改草稿原文、**不走** sync_draft——指纹已换、稿次还是 v1
    await _edit_draft(wid, cid, _draft_v2())
    rec = OutboundRecorder(_Plan([_text_frames("不该到")]))
    rec.install(monkeypatch)
    from src.api import review_runner
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    try:
        r = await review_runner.run_once(wid, rid)
    finally:
        monkeypatch.setenv(review_runner.ENV_NO_AUTO, "1")
    assert r.get("reason") == "awaiting_draft_sync", r
    assert len(rec.requests) == 0, "暂缓这一步不该发模型请求"
    # 收口推版 v2 后同一步照常跑：意见锚在 v1 已跨版 ⇒ 回退完整装配
    assert await _sync(wid, rid)
    script = _Plan([_approve_frames(rid), _text_frames("看完了。")])
    r2, rec2 = await _step_once(wid, rid, script, monkeypatch)
    assert len(rec2.requests) >= 1, "推版后这一步应正常出站"
    assert LIST_MARK in rec2.joined(0), "意见跨版须回退完整装配"


# ============================================================
# 二、同版补票（vote_nudge）
# ============================================================

async def _seat_speaks(wid: str, rid: str, seat: str, st: dict,
                       opinion: str, monkeypatch) -> OutboundRecorder:
    """跑一席真实 speak 回合：替身回一段正文发言（不落票）。"""
    from src.api import review_runner
    rec = OutboundRecorder(_Plan([_text_frames(opinion)]))
    rec.install(monkeypatch)
    await review_runner._seat_turn(wid, rid, seat, st, kind="speak")
    return rec


@pytest.mark.asyncio
async def test_nudge_lite_outbound_receipt_retry(sandbox, monkeypatch):
    """同版补票：首跳给原意见＋发言锚＋对应草稿片段坐标＋当前票态；
    一次真实读取＋一次同跳重试材料一致；票落本版。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store, session_facade
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    # f1 先落票（nudge 只挑没表态的发言席）
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # f0 真实发言：点名块名＋引文（可定位、不落票）
    opinion = ("功能需求 一节里「功能A：〈功能标记002〉按判据核对」"
               "这条缺验收口径——该补。其余块我没意见。")
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    # runner 选 vote_nudge（该席发过言、没表态）
    script = _Plan([
        _read_frames(cid),                    # 一次真实读取
        _approve_frames(rid),                  # 然后落票
        _text_frames("票已落。"),
    ], fail_first=True)                        # 同跳重试一次
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert r.get("kind") == "vote_nudge" or "vote_nudge" in str(r), r
    # 断连首试＋同位置重发＋读取续跳＋投票续跳＋收尾
    assert len(rec.requests) >= 3, f"出站数不足：{len(rec.requests)}"
    # 同跳重试材料逐字一致
    assert rec.requests[0]["raw"] == rec.requests[1]["raw"], "同位置重试必须复用同一份材料"
    first = rec.joined(1)  # 第一次成功出站（index0 是断连那次，raw 相同）
    assert "同版补票" in first or "补票" in first
    assert opinion[:20] in first, "该席原意见（原文）须在"
    assert "发言锚" in first, "意见要带发言锚"
    assert "功能需求" in first and "〈功能标记002〉" in first, \
        "意见涉及的草稿片段＋坐标须在"
    assert "当前票态" in first, "待投票态须在"
    # 不该出现：整篇草稿其余块/清单/长历史
    assert "本卡目标是〈目标标记001〉" not in first, "未涉及的草稿块不该整发"
    assert "〈验收标记004〉" not in first, "未涉及的草稿块不该整发"
    assert LIST_MARK not in first and CHILD_MARK not in first
    assert "长历史标记" not in first
    # 同一意见不经两个入口发两遍（场内历史里的同一条发言被剔掉）
    assert first.count(opinion[:20]) == 1, "原意见只能经一个入口出现一次"
    # 工具配对：read_card 调用与结果成对在续跳请求里
    cont = rec.requests[2]["body"]
    roles = [m.get("role") for m in cont.get("messages") or []]
    assert "tool" in roles, "工具续跳要带工具结果"
    # 投票回执：落在本版 v1
    st = await review_store.status(wid, rid)
    v = (st.get("votes") or {}).get(f0) or {}
    assert v.get("verdict") == "approve" and int(v.get("draft_ver") or 0) == 1


@pytest.mark.asyncio
async def test_nudge_fallback_unlocatable_opinion(sandbox, monkeypatch):
    """意见定位不到草稿条款 ⇒ 回退完整装配（清单照出）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    opinion = "总体看着还行，但我对一些细节有疑虑，说不上具体是哪条。"  # 定位不出
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    first = rec.joined(0)
    assert LIST_MARK in first, "条款定位失败必须回退完整装配"


@pytest.mark.asyncio
async def test_nudge_fallback_new_user_ruling(sandbox, monkeypatch):
    """发言锚之后场内来新用户裁定 ⇒ 回退完整装配。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    # 发言之后又一条用户裁定落场
    await _venue_append(wid, rid, "user", "用户补充裁定：功能A 的验收口径已改。")
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert LIST_MARK in rec.joined(0), "新用户裁定必须回退完整装配"


@pytest.mark.asyncio
async def test_nudge_fallback_new_reject_basis(sandbox, monkeypatch):
    """他席在本稿上新否决（意见之后）⇒ 回退完整装配。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    f1 = w["fids"][1] if len(w["fids"]) > 1 else ""
    # f1 先落票——稍后改成否决（时间在 f0 意见之后）
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    import asyncio as _aio
    await _aio.sleep(1.1)                     # 票时比较按秒精度
    if f1:
        await review_store.reject(wid, rid, f1)   # 他席新否决（本稿）
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert LIST_MARK in rec.joined(0), "新否决依据必须回退完整装配"


@pytest.mark.asyncio
async def test_nudge_fallback_unanchored_or_crossver_opinion(sandbox, monkeypatch):
    """同席发言缺锚（老块）或意见绑在旧稿 ⇒ 回退完整装配。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # 老块：手工落一条**无 review_anchor** 的发言（from=f0）
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    await _venue_append(wid, rid, "assistant", opinion, **{"from": f0})
    await review_store.mark_spoken(wid, rid, f0)
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert r.get("kind") == "vote_nudge" or "vote_nudge" in str(r), r
    assert LIST_MARK in rec.joined(0), "缺锚发言必须回退完整装配"


# ============================================================
# 三、并发与过期（投票写事务内核验）
# ============================================================

@pytest.mark.asyncio
async def test_vote_stale_material_refused(sandbox, monkeypatch):
    """装配后改稿（同稿次正文变更）⇒ 写事务内复核拒落；批次换过同样拒。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools, review_runner
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    # 取本席当前材料身份（与回合内 source.material 同一份快照）
    material, _ = await review_runner._material_snapshot(
        wid, rid, f0, st, "vote_nudge")
    card_tools.set_tool_turn_material(material)
    # 装配与写票之间一次**真实变更**：改稿（版本未推、正文已变）
    await _edit_draft(wid, cid, _draft_v2())
    out = await card_tools._h_approve(wid, f0, {"review_card_id": rid},
                                    w["cards"])
    assert not out.get("ok"), f"旧材料的票落进新稿：{out}"
    assert "stale" in str(out.get("error") or out), out
    # 票没落：f0 仍无有效票
    st2 = await review_store.status(wid, rid)
    assert f0 not in (st2.get("votes") or {}), st2.get("votes")
    # 换批同样拒：材料绑旧批
    card_tools.set_tool_turn_material(
        {**material, "batch": "batch_old_999"})
    out2 = await card_tools._h_approve(wid, f0, {"review_card_id": rid},
                                       w["cards"])
    assert not out2.get("ok") and "stale" in str(out2.get("error") or out2), out2


@pytest.mark.asyncio
async def test_legit_member_vote_unchanged(sandbox, monkeypatch):
    """合法成员在有效批次内依旧可直接投票、改投（材料绑定不误伤）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    # 无材料绑定的直投（场授成员入口）照常落
    a = await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    assert a.get("ok"), a
    r = await card_tools._h_reject(wid, f0, {"review_card_id": rid}, w["cards"])
    assert r.get("ok"), r
    st = await review_store.status(wid, rid)
    assert (st.get("votes") or {}).get(f0, {}).get("verdict") == "reject"


# ============================================================
# 四、用户控制与边界
# ============================================================

@pytest.mark.asyncio
async def test_human_stop_mid_turn_halt(sandbox, monkeypatch):
    """真实停止入口（回合中停）：首跳后 read_card 执行点调 request_stop ⇒
    中止缝消费停止标记，**不再发第二个模型请求**，票不落。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import control, card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    # 真实停止入口：在 read_card 执行点（回合在途、holder 已登记）按停。
    orig_exec = card_tools.execute_tool
    async def _stop_at_tool(*a, **kw):
        res = control.request_stop(wid, rid)
        assert res.get("stopped"), res          # 在途回合必须受理
        return await orig_exec(*a, **kw)
    monkeypatch.setattr(card_tools, "execute_tool", _stop_at_tool)
    script = _Plan([_read_frames(cid), _approve_frames(rid),
                    _text_frames("不该到")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert len(rec.requests) == 1, f"人停后不该有第二个模型请求：{len(rec.requests)}"
    st2 = await review_store.status(wid, rid)
    assert f0 not in (st2.get("votes") or {}), "停下的回合不许落票"


@pytest.mark.asyncio
async def test_speak_step_keeps_full_assembly(sandbox, monkeypatch):
    """非试点步（speak）在开关开时仍走完整装配——清单/草稿全量在场。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    st = await review_store.state_of(wid, rid)
    script = _Plan([_text_frames("我看完草稿了。")])
    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    from src.api import review_runner
    await review_runner._seat_turn(wid, rid, f0, st, kind="speak")
    first = rec.joined(0)
    assert LIST_MARK in first, "speak 非试点：清单须完整出站"
    assert CHILD_MARK in first, "speak 非试点：子卡现状须完整出站"
    assert "〈功能标记002〉" in first, "speak：完整草稿须在"


@pytest.mark.asyncio
async def test_seat_toolface_on_nudge(sandbox, monkeypatch):
    """补票试点：实际出站工具表＝席位能力∩场授（approve/reject/只读件在，
    edit_card/bash 等写件不在）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    names = set(_tool_names(rec, 0))
    assert {"approve", "reject"} <= names, f"表态工具须在：{names}"
    assert "read_card" in names, f"只读件须在：{names}"
    assert "edit_card" not in names and "bash" not in names, f"写件不许在场：{names}"


@pytest.mark.asyncio
async def test_cross_card_draft_read_denied_and_nonmember_vote(sandbox, monkeypatch):
    """跨卡读草稿与非成员投票：沿真实工具入口被拒。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    # 席位跨卡读草稿：read_card 对 requirements_draft 块本就不可达
    out = await card_tools._h_read_card(
        wid, f0, {"card_id": cid, "block": "requirements_draft"}, w["cards"])
    assert not out.get("ok") or "requirements_draft" not in json.dumps(out, ensure_ascii=False), out
    # 非成员（被评卡自己）投票被拒
    bad = await card_tools._h_approve(wid, cid, {"review_card_id": rid}, w["cards"])
    assert not bad.get("ok"), bad


# ============================================================
# 五、放行证据：开关关/开两组出站字节对照
# ============================================================

async def _confirm_world(sandbox, monkeypatch):
    """confirm 场景就绪（各席长发言＋v1 票＋v2 稿＋drift）。"""
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    # 各席真实发言（基线 prior 每席最新一条随行；受限步整段剔除）
    st = await review_store.state_of(wid, rid)
    for i, f in enumerate(w["fids"]):
        await _seat_speaks(wid, rid, f, st,
                           f"席位{i}评审意见：目标与范围一节整体可接受，"
                           "功能需求的功能A验收口径要写死。" + "详述。" * 300,
                           monkeypatch)
    await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    await _edit_draft(wid, cid, _draft_v2())
    await _sync(wid, rid)
    await review_store.set_drift_pending(wid, rid, [f0])
    return w


async def _nudge_world(sandbox, monkeypatch):
    """nudge 场景就绪（f0 同版发言未投票；他席长发言在场）。"""
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    # 他席先发言再落票（基线 prior 每席最新一条随行）
    st = await review_store.state_of(wid, rid)
    for i, f in enumerate(w["fids"][1:], 1):
        await _seat_speaks(wid, rid, f, st,
                           f"席位{i}意见：验收标准一节判据齐全，可以通过。"
                           + "详述。" * 300, monkeypatch)
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    return w


def _region_bytes(body: dict, outbound_bytes: int) -> dict[str, int]:
    """按适配器 JSON 编码统计消息对象、工具表和其余请求字节。"""
    regions: dict[str, int] = {}
    for m in body.get("messages") or []:
        role = str(m.get("role") or "other")
        size = len(json.dumps(m, ensure_ascii=False).encode("utf-8"))
        regions[role] = regions.get(role, 0) + size
    if body.get("tools"):
        regions["tools_schema"] = len(
            json.dumps(body["tools"], ensure_ascii=False).encode("utf-8"))
    overhead = outbound_bytes - sum(regions.values())
    assert overhead >= 0
    regions["request_overhead"] = overhead
    return regions


@pytest.mark.asyncio
async def test_phase_evidence_bytes_and_votes(sandbox, monkeypatch):
    """§五放行证据：confirm/vote_nudge 各跑开关关/开两组——
    相同业务结果下缩量组**总出站字节更低且请求数不增**。
    [返修⑤] 逐请求记录实际出站座/批次/稿次/席位/材料指纹/真实回执/
    分区字节/首跳或续跳——票态标签全部取读回事实，不假设版本号。"""
    from src.storage import review_store
    from src.api import card_tools, review_runner
    evidence: dict[str, Any] = {}

    def _request_records(rec: OutboundRecorder) -> list[dict]:
        out = []
        for i, x in enumerate(rec.requests):
            body = x.get("body") or {}
            assert json.dumps(body, ensure_ascii=False).encode("utf-8") == x["raw"]
            out.append({
                "index": x["index"],
                "hop": "initial" if i == 0 else "continuation",
                "outbound_model": str(body.get("model") or ""),
                "bytes": x["bytes"],
                "region_bytes": _region_bytes(body, x["bytes"]),
                "tools": _tool_names(rec, x["index"]),
            })
        return out

    def _step_record(w: dict, r: Any, rec: OutboundRecorder,
                     st: dict, material: dict) -> dict[str, Any]:
        seat = w["fids"][0]
        return {
            "seat": seat,
            "review_id": w["rid"],
            "batch": str(st.get("batch") or w.get("batch") or ""),
            "material": {k: material.get(k) for k in
                         ("batch", "draft_ver", "draft_fp", "restricted", "reason")},
            "step_result": {"error": str(r.get("error") or ""),
                            "reason": str(r.get("reason")
                                          or (r.get("_done") or {}).get("reason") or "")},
            "requests": len(rec.requests),
            "total_bytes": sum(x["bytes"] for x in rec.requests),
            "vote": (st.get("votes") or {}).get(seat),
            "per_request": _request_records(rec),
        }

    async def _run_confirm(on: bool) -> dict[str, Any]:
        monkeypatch.setenv(ENV, "1" if on else "0")
        w = await _confirm_world(sandbox, monkeypatch)
        wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
        st0 = await review_store.state_of(wid, rid)
        material, _mc = await review_runner._material_snapshot(
            wid, rid, f0, st0, "confirm")
        script = _Plan([_approve_frames(rid), _text_frames("增量不动摇通过。")])
        r, rec = await _step_once(wid, rid, script, monkeypatch)
        st = await review_store.status(wid, rid)
        return _step_record(w, r, rec, st, material)

    async def _run_nudge(on: bool, *, steer_text: str = "") -> dict[str, Any]:
        monkeypatch.setenv(ENV, "1" if on else "0")
        w = await _nudge_world(sandbox, monkeypatch)
        wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
        st0 = await review_store.state_of(wid, rid)
        material, _mc = await review_runner._material_snapshot(
            wid, rid, f0, st0, "vote_nudge")
        if steer_text:
            # 真实插话对照：回合在途时（工具执行点）走 steer 正式入口投递——
            # _apply_steer 在下一跳边界把它注进续跳上下文（原控制链）。
            orig_exec = card_tools.execute_tool
            from src.api import steer as _steer

            async def _steer_mid_tool(*a, **kw):
                res = await orig_exec(*a, **kw)
                st_res = await _steer.request_steer(wid, rid, steer_text)
                assert st_res.get("applied"), st_res   # 在途回合必须受理
                return res
            monkeypatch.setattr(card_tools, "execute_tool", _steer_mid_tool)
        script = _Plan([_read_frames(cid), _approve_frames(rid),
                        _text_frames("票已落。")])
        r, rec = await _step_once(wid, rid, script, monkeypatch)
        st = await review_store.status(wid, rid)
        rec_d = _step_record(w, r, rec, st, material)
        if steer_text:
            rec_d["steer_text"] = steer_text
            rec_d["steer_reached_later_hop"] = any(
                steer_text in rec.joined(i) for i in range(1, len(rec.requests)))
        return rec_d

    c_off = await _run_confirm(False)
    c_on = await _run_confirm(True)
    n_off = await _run_nudge(False)
    n_on = await _run_nudge(True)
    n_steer = await _run_nudge(
        True, steer_text="用户插话对照：验收口径按最新裁定执行。")

    evidence["confirm"] = {"off": c_off, "on": c_on}
    evidence["vote_nudge"] = {"off": n_off, "on": n_on,
                              "steer_comparison": n_steer}
    # 相同业务结果（票都落了、verdict 相同）＋ 请求数不增 ＋ 总字节降低
    assert c_off["vote"].get("verdict") == c_on["vote"].get("verdict") == "approve", (c_off["vote"], c_on["vote"])
    assert c_on["requests"] <= c_off["requests"], (c_on["requests"], c_off["requests"])
    assert c_on["total_bytes"] < c_off["total_bytes"], \
        f"confirm 缩量未降字节：{c_on['total_bytes']} vs {c_off['total_bytes']}"
    assert n_off["vote"].get("verdict") == n_on["vote"].get("verdict") == "approve"
    assert n_on["requests"] <= n_off["requests"]
    assert n_on["total_bytes"] < n_off["total_bytes"], \
        f"vote_nudge 缩量未降字节：{n_on['total_bytes']} vs {n_off['total_bytes']}"
    # 真实插话对照：插话在场、业务结果不变（票同样落）
    assert n_steer["vote"].get("verdict") == "approve", n_steer["vote"]
    out_dir = os.environ.get("BATCH3_EVIDENCE_DIR")
    if out_dir:
        p = Path(out_dir) / "batch3_phases.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(evidence, ensure_ascii=False, indent=2,
                                default=str), encoding="utf-8")
    print(json.dumps({k: {kk: {"requests": vv["requests"],
                               "total_bytes": vv["total_bytes"]}
                          for kk, vv in v.items()}
                     for k, v in evidence.items()}, ensure_ascii=False, indent=2))


# ============================================================
# 六、返修验收（用户独立复跑反例五项）
# ============================================================

@pytest.mark.asyncio
async def test_stale_turn_cleanup_keeps_new_batch_records(sandbox, monkeypatch):
    """[返修①] 旧回合退场只清自己的记录：换批后新批合法票＋新批用户
    消息必须读得回；只有本回合（旧批＋旧 pair）的残块被清。"""
    from src.storage import card_db, review_store, session_facade
    from src.api import review_runner, routes
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    old_batch = w["batch"]
    # 旧批先落一张合法票——稍后验证它被「按旧批点名」撤掉
    a0 = await review_store.approve(wid, rid, f0)
    assert a0.get("ok"), a0
    st_old = await review_store.state_of(wid, rid)

    async def _fake_run(*a, **kw):
        # ① 旧文件里的本回合残块（归档前）
        await session_facade.append(wid, rid, {
            "role": "assistant", "content": "旧回合残块A",
            "from": f0, "pair_id": "tp_old",
            "review_anchor": {"batch": old_batch, "seat": f0, "step": "speak",
                              "draft_ver": 1, "draft_fp": _fp(_draft_v1())}})
        # ② 回合中途换批：open_batch 归档旧会话、新批生效
        await review_store.open_batch(wid, rid, "新批次",
                                      members=w["fids"], target=cid,
                                      brief=BRIEF_TEXT,
                                      batch_type="requirements")
        # ③ 新批的合法票＋用户消息
        await review_store.approve(wid, rid, f0)
        await session_facade.append(
            wid, rid, {"role": "user", "content": "新批用户消息KEEP"})
        # ④ 本回合跨归档残块：锚记旧批的发言＋同 pair 的工具块
        await session_facade.append(wid, rid, {
            "role": "assistant", "content": "旧回合残块B",
            "from": f0, "pair_id": "tp_old",
            "review_anchor": {"batch": old_batch, "seat": f0, "step": "speak",
                              "draft_ver": 1, "draft_fp": _fp(_draft_v1())}})
        await session_facade.append(wid, rid, {
            "role": "tool", "content": "工具残块", "from": f0,
            "actor": "tool", "pair_id": "tp_old",
            "tool_call_id": "call_x"})
        return {"pair_id": "tp_old"}

    monkeypatch.setattr(routes, "_run_message", _fake_run)
    r = await review_runner._seat_turn(wid, rid, f0, st_old, kind="speak")
    assert r.get("error") == "stale_batch", r

    # 验收：新批票可读回
    st = await review_store.status(wid, rid)
    v = (st.get("votes") or {}).get(f0) or {}
    assert v.get("verdict") == "approve", \
        f"新批合法票被误撤：{st.get('votes')}"
    # 验收：新批用户消息可读回
    blocks = await session_facade.read(wid, rid) or []
    body = "\n".join(str(b.get("content") or "") for b in blocks)
    assert "新批用户消息KEEP" in body, "新批用户消息被误清"
    # 验收：本回合残块被清（跨归档两种形态）
    assert "旧回合残块B" not in body, "锚记旧批的发言残块没清掉"
    assert "工具残块" not in body, "同 pair 的工具残块没清掉"
    # 验收：票表只剩新批的合法行（open_batch 自清旧批表决；
    # revoke 按捕获的旧批号点名——新批行 revoked=0 不可动）
    rows = await card_db.run_read(wid, lambda conn: [
        dict(x) for x in conn.execute(
            "SELECT batch_id, revoked FROM review_approvals"
            " WHERE work_id=? AND role_id=? ORDER BY batch_id",
            (wid, f0)).fetchall()], [])
    cur_batch = str((await review_store.state_of(wid, rid)).get("batch") or "")
    assert cur_batch != old_batch, "测试前提：批次应已更换"
    new_rows = [x for x in rows if x["batch_id"] == cur_batch]
    assert new_rows and int(new_rows[0]["revoked"]) == 0, \
        f"新批票被误撤：{rows}"


@pytest.mark.asyncio
async def test_vote_txn_writes_validated_text(sandbox, monkeypatch):
    """[返修②] 核验与落票同事务同一份原文：事务外读到的「另一版本」
    不许进票快照——`_draft_text` 被换芯成 B 时，票里存的必须是
    事务内读到的真实 A。"""
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, f0 = w["wid"], w["rid"], w["fids"][0]
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "TEXT_B_事务外读到的另一版本"

    # 事务外读数入口换芯：谁再拿它喂快照，断言立现。
    monkeypatch.setattr(review_store, "_draft_text", _boom)
    out = await review_store.approve(wid, rid, f0)
    assert out.get("ok"), out
    st = await review_store.status(wid, rid)
    v = (st.get("votes") or {}).get(f0) or {}
    assert str(v.get("snapshot") or "") == _draft_v1(), \
        f"票快照不是事务内核验的那份原文：{str(v.get('snapshot'))[:80]}"
    assert called["n"] == 0, "落票还在吃事务外读数"


@pytest.mark.asyncio
async def test_vote_txn_abab_validates_current(sandbox, monkeypatch):
    """[返修②] A→B→A：expect 绑 A，事务内读到 A ⇒ 核验过、票存 A；
    expect 绑 B（事务外快照）⇒ 事务内读到 A ⇒ 拒落。"""
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, f0 = w["wid"], w["rid"], w["fids"][0]
    batch = w["batch"]
    fp_a = _fp(_draft_v1())
    # 绑 A ⇒ 事务内读 A ⇒ 过、快照存 A
    ok = await review_store.approve(
        wid, rid, f0, expect={"batch": batch, "draft": 1, "draft_fp": fp_a})
    assert ok.get("ok"), ok
    st = await review_store.status(wid, rid)
    assert str(((st.get("votes") or {}).get(f0) or {}).get("snapshot") or "") \
        == _draft_v1()
    # 绑 B ⇒ 事务内读 A ⇒ fp 不符 ⇒ 拒
    stale = await review_store.approve(
        wid, rid, w["fids"][1],
        expect={"batch": batch, "draft": 1, "draft_fp": _fp("TEXT_B")})
    assert not stale.get("ok") and stale.get("stale") == "draft_fp", stale


@pytest.mark.asyncio
async def test_outbound_guard_before_first_hop(sandbox, monkeypatch):
    """[返修③] 轻量差事生成后、首跳前改稿 ⇒ 发送边界复核拦下：
    零出站、零续跳，按过期路径交回。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import message_runner
    w = await _nudge_world(sandbox, monkeypatch)
    wid, cid, rid = w["wid"], w["cid"], w["rid"]
    # 装配（build_context/mandate 已过）与首个发送边界之间注入真实变更：
    # _apply_steer 是 step 内**装配之后、出站之前**的 async 点。
    orig = message_runner._apply_steer

    async def _mutate(*a, **kw):
        await _edit_draft(wid, cid, _draft_v2())   # 同稿次正文已变（未推版）
        return await orig(*a, **kw)

    monkeypatch.setattr(message_runner, "_apply_steer", _mutate)
    script = _Plan([_read_frames(cid), _approve_frames(rid),
                    _text_frames("不该到")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert len(rec.requests) == 0, \
        f"材料过期还发首跳/续跳：{len(rec.requests)} 次出站"
    assert "stale" in str(r.get("error") or "") \
        or "stale" in str(r.get("reason") or "") \
        or "draft_fp" in str(r), r
    # 票没落（该席仍无有效票）
    st = await review_store.status(wid, rid)
    assert w["fids"][0] not in (st.get("votes") or {})


@pytest.mark.asyncio
async def test_outbound_guard_before_continuation(sandbox, monkeypatch):
    """[返修③] 首跳后发、续跳前改稿 ⇒ 第二个出站边界拦下：
    实际出站恰为 1 次，续跳请求不存在。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _nudge_world(sandbox, monkeypatch)
    wid, cid, rid = w["wid"], w["cid"], w["rid"]
    orig_exec = card_tools.execute_tool

    async def _mutate_after_tool(*a, **kw):
        res = await orig_exec(*a, **kw)
        await _edit_draft(wid, cid, _draft_v2())   # 续跳前真实变更
        return res

    monkeypatch.setattr(card_tools, "execute_tool", _mutate_after_tool)
    script = _Plan([_read_frames(cid), _approve_frames(rid),
                    _text_frames("不该到")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert len(rec.requests) == 1, \
        f"材料过期后还发续跳：{len(rec.requests)} 次出站"
    st = await review_store.status(wid, rid)
    assert w["fids"][0] not in (st.get("votes") or {})


@pytest.mark.asyncio
async def test_legit_turn_after_material_check(sandbox, monkeypatch):
    """[返修③] 正向面：材料未动的合法回合正常收口——守卫不误伤。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _nudge_world(sandbox, monkeypatch)
    wid, rid, f0 = w["wid"], w["rid"], w["fids"][0]
    script = _Plan([_approve_frames(rid), _text_frames("票已落。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert len(rec.requests) >= 2, "合法回合该有首跳＋续跳"
    st = await review_store.status(wid, rid)
    assert (st.get("votes") or {}).get(f0, {}).get("verdict") == "approve"


@pytest.mark.asyncio
async def test_nudge_crossver_identical_text_falls_back(sandbox, monkeypatch):
    """[返修④a] v1 与 v3 正文逐字相同：v1 意见锚的稿次≠当前稿次，
    不许复用——回退完整装配（旧代码只核指纹会误复用）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # f0 在 v1 真实发言（锚 draft_ver=1 · fp=fp(v1)）
    opinion = "功能需求 一节的「功能A：〈功能标记002〉按判据核对」缺验收口径。"
    st = await review_store.state_of(wid, rid)
    assert int(st.get("draft") or 0) == 1
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    # v1→v2（变）→v3（回 A 原文：与 v1 逐字相同、稿次 3）
    await _edit_draft(wid, cid, _draft_v2())
    assert await _sync(wid, rid)
    await _edit_draft(wid, cid, _draft_v1())
    assert await _sync(wid, rid)
    st = await review_store.state_of(wid, rid)
    assert int(st.get("draft") or 0) == 3, st.get("draft")
    # 发言登记按稿次作废旧了——v3 重新盖章让 runner 挑 vote_nudge
    await review_store.mark_spoken(wid, rid, f0)
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert "vote_nudge" in str(r), r
    first = rec.joined(0)
    assert LIST_MARK in first, \
        "v1 意见在 v3 同文下不许缩量复用（稿次锚不符⇒完整装配）"
    assert "你的原意见" not in first, "旧版意见不该出现在轻量差事里"


@pytest.mark.asyncio
async def test_nudge_partial_clause_location_falls_back(sandbox, monkeypatch):
    """[返修④b] 两条可定位依据只定到一条 ⇒ 定不齐＝缺证，
    回退完整装配（不是「定到一条就算」）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    # 引文①在稿内、引文②查无此句——两条依据只定到一条
    opinion = ("「功能A：〈功能标记002〉按判据核对」这条缺验收口径；"
               "另外「不存在的第二条款ZZ9」也有问题。")
    st = await review_store.state_of(wid, rid)
    await _seat_speaks(wid, rid, f0, st, opinion, monkeypatch)
    script = _Plan([_approve_frames(rid), _text_frames("好。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    assert "vote_nudge" in str(r), r
    assert LIST_MARK in rec.joined(0), \
        "条款定位不齐必须回退完整装配"


@pytest.mark.asyncio
async def test_confirm_appr_rationale_quarantined(sandbox, monkeypatch):
    """[返修④d] 原批准理由走隔离投影：锚定的批准发言带
    quarantine＋ctx_content 时，轻量差事只能带投影视图——
    已隔离的伪调用原文不许回流。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store
    from src.api import card_tools
    w = await _mk_world(sandbox, monkeypatch)
    wid, cid, rid, f0 = w["wid"], w["cid"], w["rid"], w["fids"][0]
    batch = w["batch"]
    RAW_FAKE = "理由尾部带伪调用 invoke-tag 原文 FAKEMARK"
    SANITIZED = "理由投影：功能A口径可接受（已隔离异常片段）"
    # 手工落一条「带锚＋已隔离」的原批准发言（锚绑被批准稿 fp(v1)）
    await _venue_append(
        wid, rid, "assistant", RAW_FAKE,
        **{"from": f0, "quarantine": "pseudo_call",
           "ctx_content": SANITIZED,
           "review_anchor": {"batch": batch, "seat": f0, "step": "speak",
                             "draft_ver": 1, "draft_fp": _fp(_draft_v1())}})
    # 正式投票工具落 v1 通过票（快照＝v1 原文）→ 修稿 → confirm
    await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    for f in w["fids"][1:]:
        await review_store.approve(wid, rid, f)
    await _edit_draft(wid, cid, _draft_v2())
    await _sync(wid, rid)
    await review_store.set_drift_pending(wid, rid, [f0])
    script = _Plan([_approve_frames(rid), _text_frames("增量不动摇通过。")])
    r, rec = await _step_once(wid, rid, script, monkeypatch)
    first = rec.joined(0)
    assert SANITIZED in first, "隔离发言的投影视图没在差事里"
    assert "FAKEMARK" not in first, "已隔离的原文回流进了轻量差事"


# ============================================================
# 七、返修验收·二轮（用户独立复跑反例四项）
# ============================================================

@pytest.mark.asyncio
async def test_purge_keeps_new_same_seat_and_syncs_db(sandbox, monkeypatch):
    """[返修①·二轮] 换批后新批同席发言（seq>旧水位、锚记新批）不许随
    旧回合残块被清；清场要同步删 SQLite 原文账，残块全文留
    ``{rid}-purged-{batch}`` 旁档供审计。"""
    from src.storage import card_db, review_store, session_facade
    from src.api import review_runner
    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, cid, seat = w["wid"], w["rid"], w["cid"], w["fids"][0]
    old = w["batch"]
    prior = await session_facade.read(wid, rid)
    pre = max((int(b["seq"]) for b in prior), default=0)
    new = await review_store.open_batch(
        wid, rid, "Second batch", members=w["fids"], target=cid,
        brief=BRIEF_TEXT, batch_type="requirements")
    for _ in range(pre + 2):
        await session_facade.append(wid, rid, {"role": "user", "content": "user_pad"})
    ack = await review_store.approve(wid, rid, seat)
    assert ack["ok"]
    fresh = await session_facade.append(wid, rid, {
        "role": "assistant", "content": "NEW_SAME_SEAT", "from": seat,
        "pair_id": "pair_new",
        "review_anchor": {"batch": new["batch"], "seat": seat}})
    old_reply = await session_facade.append(wid, rid, {
        "role": "assistant", "content": "OLD_REPLY", "from": seat,
        "pair_id": "pair_old",
        "review_anchor": {"batch": old, "seat": seat}})
    old_tool = await session_facade.append(wid, rid, {
        "role": "tool", "content": "OLD_TOOL", "pair_id": "pair_old",
        "tool_call_id": "call_old"})
    assert fresh["seq"] > pre and old_reply["seq"] > pre

    await review_runner._purge_turn_blocks(wid, rid, seat, old, pre, "pair_old")

    md_contents = [b.get("content") for b in await session_facade.read(wid, rid)]
    db_contents = await card_db.run_read(wid, lambda c: [
        r[0] for r in c.execute(
            "SELECT content FROM session_blocks WHERE card_id=? AND seq IN (?,?,?)",
            (rid, fresh["seq"], old_reply["seq"], old_tool["seq"]))], [])
    assert "NEW_SAME_SEAT" in md_contents, "新批同席发言被误清"
    assert "NEW_SAME_SEAT" in db_contents, "新批发言的原文账行被误删"
    assert "OLD_REPLY" not in md_contents and "OLD_TOOL" not in md_contents
    assert "OLD_TOOL" not in db_contents and "OLD_REPLY" not in db_contents, \
        "旧残块仍留在 SQLite 原文账"
    # 板投影同步：残块 seq 不许留在 context_board
    board_rows = await card_db.run_read(wid, lambda c: [
        r[0] for r in c.execute(
            "SELECT seq FROM context_board WHERE card_id=? AND seq IN (?,?)",
            (rid, old_reply["seq"], old_tool["seq"]))], [])
    assert board_rows == [], f"残块仍在 context_board：{board_rows}"
    # 审计原文：残块全文留 purged 旁档，不毁出处
    arch = session_facade._session_path(wid, f"{rid}-purged-{old}")
    assert arch.exists(), "purged 旁档没写——审计原文没了"
    arch_text = arch.read_text(encoding="utf-8")
    assert "OLD_REPLY" in arch_text and "OLD_TOOL" in arch_text


@pytest.mark.asyncio
async def test_purge_user_append_race_survives(sandbox, monkeypatch):
    """[返修①·二轮] 清场读头与重写之间插入的用户消息不许被整文件
    重写覆盖——锁内重读必须把它读回来。"""
    from src.storage import session_facade, session_store
    from src.api import review_runner
    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, seat = w["wid"], w["rid"], w["fids"][0]
    old = w["batch"]
    await session_facade.append(wid, rid, {
        "role": "assistant", "from": seat, "content": "OLD_REPLY",
        "pair_id": "pair_old",
        "review_anchor": {"batch": old, "seat": seat}})
    async with session_store._session_lock(wid, rid):
        appended = asyncio.create_task(session_facade.append(
            wid, rid, {"role": "user", "content": "USER_APPENDED"}))
        await asyncio.sleep(0)
        purge = asyncio.create_task(
            review_runner._purge_turn_blocks(wid, rid, seat, old, 0, "pair_old"))
        await asyncio.sleep(0)
        assert not appended.done() and not purge.done()
    await asyncio.gather(appended, purge)
    blocks = await session_facade.read(wid, rid)
    contents = [b.get("content") for b in blocks]
    assert "USER_APPENDED" in contents, "锁外插入的用户消息被重写覆盖"
    assert "OLD_REPLY" not in contents, "旧回合残块没清掉"


@pytest.mark.asyncio
async def test_purge_rollback_preserves_file_origin_and_board(sandbox, monkeypatch):
    from src.api import review_runner
    from src.storage import card_db, context_board, origin_store, session_facade

    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, seat, batch = w["wid"], w["rid"], w["fids"][0], w["batch"]
    await session_facade.append(wid, rid, {
        "role": "assistant", "from": seat, "content": "OLD_ROLLBACK",
        "pair_id": "old-pair", "review_anchor": {"batch": batch, "seat": seat}})
    active = session_facade._session_path(wid, rid)
    archived = session_facade._session_path(wid, f"{rid}-purged-{batch}")
    original = active.read_bytes()
    origin = origin_store.read_origin_span(wid, rid, 0, 100)
    board = context_board.read_board(wid, rid)

    await card_db.run_write(wid, lambda conn: conn.execute(
        "CREATE TRIGGER purge_blocked BEFORE DELETE ON session_blocks "
        "WHEN OLD.content='OLD_ROLLBACK' BEGIN SELECT RAISE(ABORT,'purge blocked'); END"))
    await review_runner._purge_turn_blocks(wid, rid, seat, batch, 0, "old-pair")
    assert active.read_bytes() == original and not archived.exists()
    assert origin_store.read_origin_span(wid, rid, 0, 100) == origin
    assert context_board.read_board(wid, rid) == board
    assert not any(e["type"] == "review_purge" for e in session_facade.event_log.read(wid, rid))

    await card_db.run_write(wid, lambda conn: conn.execute("DROP TRIGGER purge_blocked"))
    await review_runner._purge_turn_blocks(wid, rid, seat, batch, 0, "old-pair")
    assert "OLD_ROLLBACK" not in active.read_text(encoding="utf-8")
    assert "OLD_ROLLBACK" in archived.read_text(encoding="utf-8")
    purges = [e for e in session_facade.event_log.read(wid, rid) if e["type"] == "review_purge"]
    assert len(purges) == 1 and purges[0]["data"]["archive"] == archived.name


@pytest.mark.asyncio
async def test_interrupted_batch_publish_recovers_and_resets_projection(sandbox, monkeypatch):
    from src.storage import context_board, origin_store, review_store, session_facade

    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, cid, batch = w["wid"], w["rid"], w["cid"], w["batch"]
    await session_facade.append(wid, rid, {"role": "user", "content": "OLD_BATCH"})
    active = session_facade._session_path(wid, rid)
    archive = session_facade._session_path(wid, f"{rid}-{batch}")
    original = active.read_bytes()
    board = context_board.read_board(wid, rid)
    write = session_facade._atomic_write_text

    def interrupted(path, text):
        write(path, text)
        if path == active:
            raise OSError("interrupted after file replacement")

    def recovery_unavailable(*args):
        raise OSError("recovery unavailable")

    async def open_next():
        return await review_store.open_batch(
            wid, rid, "new", members=w["fids"], target=cid, brief=BRIEF_TEXT,
            expect_batch=batch)

    with monkeypatch.context() as fault:
        fault.setattr(session_facade, "_atomic_write_text", interrupted)
        fault.setattr(session_facade, "_recover_review_publication_sync", recovery_unavailable)
        with pytest.raises(OSError, match="recovery unavailable"):
            await open_next()
    assert (await review_store.state_of(wid, rid))["batch"] == batch
    assert context_board.read_board(wid, rid) == board
    assert active.read_bytes() != original
    assert "OLD_BATCH" in str(await session_facade.read(wid, rid))
    assert active.read_bytes() == original and not archive.exists()

    opened = await open_next()
    assert opened["batch"] != batch
    assert await session_facade.read(wid, rid) == []
    assert context_board.read_board(wid, rid) == []
    assert not origin_store.read_origin_span(wid, rid, 0, 100)["events"]
    assert "OLD_BATCH" in archive.read_text(encoding="utf-8")
    assert "OLD_BATCH" in str(origin_store.read_origin_span(wid, f"{rid}-{batch}", 0, 100))
    block = await session_facade.append(wid, rid, {"role": "user", "content": "NEW_BATCH"})
    assert block["seq"] == 0 and "OLD_BATCH" not in str(context_board.read_board(wid, rid))


@pytest.mark.asyncio
async def test_gateway_guard_rechecks_after_semaphore_queue(sandbox, monkeypatch):
    from src.api import model_service

    monkeypatch.setenv(ENV, "1")
    w = await _nudge_world(sandbox, monkeypatch)
    wid, cid, rid = w["wid"], w["cid"], w["rid"]
    waiting = asyncio.Event()

    class OccupiedSeat(asyncio.Semaphore):
        async def __aenter__(self):
            waiting.set()
            return await super().__aenter__()

    slot = OccupiedSeat(0)
    monkeypatch.setattr(model_service.get_gateway(), "_sem_for", lambda model: slot)
    task = asyncio.create_task(
        _step_once(wid, rid, _Plan([_text_frames("stale request")]), monkeypatch))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=20)
        await _edit_draft(wid, cid, _draft_v2())
    finally:
        slot.release()
    result, rec = await asyncio.wait_for(task, timeout=20)
    assert not rec.requests
    assert result["stale"] is True and result["error"] == "stale_material"


@pytest.mark.asyncio
async def test_gateway_retry_guard_blocks_second_send(sandbox, monkeypatch):
    """[返修②·二轮] 网关内部重试也过材料闸：第一次真实出站时改稿＋
    断连 ⇒ 第二次 attempt 被 before_send 拦下——恰 1 次出站、
    控制回执收口（不是 seat_done）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import card_db
    w = await _nudge_world(sandbox, monkeypatch)
    wid, cid, rid = w["wid"], w["cid"], w["rid"]

    class MutateThenFail(_Plan):
        def __call__(self, index, body):
            if index == 0:
                def write(conn):
                    conn.execute(
                        "UPDATE cards SET requirements_draft=? WHERE card_id=?",
                        (_draft_v2(), cid))
                card_db.write_sync(wid, write)
            return super().__call__(index, body)

    result, rec = await _step_once(
        wid, rid, MutateThenFail([_text_frames("done")], fail_first=True),
        monkeypatch)
    truth = await card_db.run_read(wid, lambda c: c.execute(
        "SELECT requirements_draft FROM cards WHERE card_id=?",
        (cid,)).fetchone()[0], "")
    assert truth == _draft_v2()
    assert len(rec.requests) == 1, \
        f"材料过期后网关仍发了第二次请求：{len(rec.requests)} 次出站"
    assert result.get("stale") or str(result.get("error") or "") \
        .startswith("stale"), f"过期应按控制回执退出：{result}"


@pytest.mark.asyncio
async def test_outer_model_retry_reaches_transport_and_lands_vote(sandbox, monkeypatch):
    from src.api import message_runner, model_service
    from src.storage import review_store

    monkeypatch.setenv(ENV, "1")
    monkeypatch.setenv("COST_HOP_RETRY_BUDGET", "0")
    w = await _nudge_world(sandbox, monkeypatch)
    wid, rid, seat = w["wid"], w["rid"], w["fids"][0]

    # Exhaust the gateway on the first disconnect so the runner retries the
    # same hop with its existing material guard.
    monkeypatch.setattr(model_service.get_gateway(), "_retry_params", lambda _: (1, 0))
    monkeypatch.setattr(message_runner.model_config, "layer_fallbacks",
                        lambda *args, **kwargs: [])
    response, rec = await _step_once(
        wid, rid, _Plan([_approve_frames(rid), _text_frames("通过。")], fail_first=True),
        monkeypatch)

    assert not response.get("error"), response
    assert len(rec.requests) >= 2
    assert rec.requests[0]["raw"] == rec.requests[1]["raw"]
    status = await review_store.status(wid, rid)
    vote = status["votes"][seat]
    assert vote["verdict"] == "approve" and vote["draft_ver"] == status["draft"]


@pytest.mark.asyncio
async def test_member_check_inside_vote_txn(sandbox, monkeypatch):
    """[返修③·二轮] 换批竞速：旧批成员闸过后切到不含该席的新批 ⇒
    落票事务按新批名单拒落，新批票表零写入。"""
    from src.api import card_tools
    from src.storage import card_db, review_store
    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, cid, f0 = w["wid"], w["rid"], w["cid"], w["fids"][0]
    gate = card_tools._vote_gate
    next_batch: dict = {}

    async def gated(*a, **kw):
        ans = await gate(*a, **kw)
        assert ans is None
        next_batch.update(await review_store.open_batch(
            wid, rid, "new", members=w["fids"][1:], target=cid,
            brief=BRIEF_TEXT, batch_type="requirements"))
        return ans

    monkeypatch.setattr(card_tools, "_vote_gate", gated)
    result = await card_tools._h_approve(wid, f0, {"review_card_id": rid}, w["cards"])
    rows = await card_db.run_read(wid, lambda c: [tuple(r) for r in c.execute(
        "SELECT batch_id, role_id FROM review_approvals"
        " WHERE batch_id=? AND role_id=?",
        (next_batch["batch"], f0))], [])
    assert not result.get("ok"), f"非成员票被接收：{result}"
    assert rows == [], f"非成员票落进了新批票表：{rows}"


@pytest.mark.asyncio
async def test_nudge_short_missing_quote_falls_back(sandbox, monkeypatch):
    """[返修④·二轮] 短引文一样是必要依据：意见带一条可定位块名＋
    一条查无原文的「缺失项」 ⇒ 回退完整装配。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store, session_facade
    from src.api import review_runner
    w = await _nudge_world(sandbox, monkeypatch)
    wid, rid, f0 = w["wid"], w["rid"], w["fids"][0]
    st = await review_store.state_of(wid, rid)
    await session_facade.append(wid, rid, {
        "role": "assistant", "from": f0,
        "content": "功能需求一节可以。请核对「缺失项」。",
        "review_anchor": {"review_id": rid, "seat": f0, "batch": st["batch"],
                          "step": "speak", "draft_ver": 1,
                          "draft_fp": _fp(_draft_v1())}})
    material, _ctx = await review_runner._material_snapshot(
        wid, rid, f0, st, "vote_nudge")
    assert not material["restricted"], \
        f"短引文缺证仍走轻量：{material['reason']}"


@pytest.mark.asyncio
async def test_confirm_reason_bound_to_approved_ver(sandbox, monkeypatch):
    """[返修④·二轮] v1→v2→v3 且 v3 正文回到 v1 原文：锚记 v3 的
    发言不许被当成 v1 批准理由（稿次号＋指纹双核对）。"""
    monkeypatch.setenv(ENV, "1")
    from src.storage import review_store, session_facade
    from src.api import review_runner
    w = await _mk_world(sandbox, monkeypatch)
    wid, rid, cid, f0 = w["wid"], w["rid"], w["cid"], w["fids"][0]
    await review_store.approve(wid, rid, f0)
    await session_facade.append(wid, rid, {
        "role": "assistant", "from": f0, "content": "APPROVED_V1_REASON",
        "review_anchor": {"batch": w["batch"], "draft_ver": 1,
                          "draft_fp": _fp(_draft_v1())}})
    await _edit_draft(wid, cid, _draft_v2())
    assert await _sync(wid, rid)
    await _edit_draft(wid, cid, _draft_v1())
    assert await _sync(wid, rid)
    await session_facade.append(wid, rid, {
        "role": "assistant", "from": f0, "content": "V3_LATER_OPINION",
        "review_anchor": {"batch": w["batch"], "draft_ver": 3,
                          "draft_fp": _fp(_draft_v1())}})
    st = await review_store.state_of(wid, rid)
    mat, ctx = await review_runner._material_snapshot(
        wid, rid, f0, st, "confirm")
    previous = ctx.get("appr_speech") or {}
    assert previous.get("content") != "V3_LATER_OPINION", \
        "v3 同文发言被当成 v1 批准理由"
    # 有真 v1 理由时应选它（稿次=被批准的 1），不是留空
    assert previous.get("content") == "APPROVED_V1_REASON", \
        f"真 v1 理由没选回：{previous.get('content')}"
