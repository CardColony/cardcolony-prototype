# -*- coding: utf-8 -*-
"""伴生评审卡：工具层行为（[伴生评审卡 2026-10-27]）。

钉需求里明示的几条（全部走真实 handler，不 mock）：
- open_review 只在 sub_project/topic 上开，成员＝在职职能
- approve 只接受**成员**，非成员被拒
- 全员通过才 all_passed；没调过的不算通过（默认未通过，P3）
- set_criteria 让判据版本 +1 ⇒ 已通过的作废
- 现状块由系统渲染，不是模型写的
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_store, function_store, review_store, work_store


async def _make_work_with_functions(n: int = 2):
    wdata = await work_store.create_work("评审工具测试")
    wid = str(wdata["work_id"])
    fids = []
    for i in range(n):
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": f"f{i}"}})
        fids.append(str(prof.get("function_id") or ""))
    r = await card_store.create_card(wid, "子项目甲", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    # [需求文档生命周期 2026-10-28] 开需求评审的前提是**草稿非空**
    # （没有草稿就没有可评审的内容）。本文件的用例测的是评审机制本身，
    # 所以这里先给它一份草稿。
    await card_store.update_card(
        wid, cid, {"requirements_draft": "R1 草稿（测试用）",
                   "requirements_state": "draft"}, "测试前置：写草稿")
    return wid, cid, fids


async def test_open_review_creates_batch_with_members(sandbox):
    wid, cid, fids = await _make_work_with_functions()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "focus": "R1 收尾", "brief": "背景X\n现状Y\n评审Z"}, cards)
    assert out.get("ok"), out
    rid = out["data"]["review_card_id"]
    assert out["data"]["members"] == fids
    st = await review_store.status(wid, rid)
    assert st["batch"].startswith("rv_")
    assert set(st["pending"]) == set(fids), "默认全员未通过"


async def test_approve_rejects_non_member(sandbox):
    wid, cid, fids = await _make_work_with_functions()
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]
    bad = await card_tools._h_approve(
        wid, cid, {"review_card_id": rid}, cards)   # 子卡自己不是成员
    assert bad.get("ok") is not True
    assert "成员" in str(bad.get("error") or bad)


async def test_all_approve_closes_only_when_everyone_passed(sandbox):
    wid, cid, fids = await _make_work_with_functions(3)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]

    a = await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    assert a["data"]["all_passed"] is False and len(a["data"]["pending"]) == 2

    await card_tools._h_approve(wid, fids[1], {"review_card_id": rid}, cards)
    last = await card_tools._h_approve(wid, fids[2], {"review_card_id": rid}, cards)
    assert last["data"]["all_passed"] is True and last["data"]["pending"] == []


async def test_set_criteria_invalidates_approvals(sandbox):
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]
    # 只让一人通过（批次仍开着，未收口）
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    st0 = await review_store.status(wid, rid)
    assert fids[0] in st0["passed"] and st0["all_passed"] is False

    await card_tools._h_set_criteria(
        wid, cid, {"review_card_id": rid, "text": "C-1 状态行一致\nC-2 有证据"}, cards)
    st = await review_store.status(wid, rid)
    assert fids[0] not in st["passed"], "判据变了，旧通过必须作废"
    assert st["criteria"] == 2


async def test_status_block_is_rendered_by_system(sandbox):
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    body = (await card_store.read_card(wid, rid))["body"]
    sb = str(body.get("status_block") or "")
    assert "通过 1/2" in sb, f"现状块应由系统渲染出进度，实际：{sb[:200]}"
    assert fids[0] in sb and fids[1] in sb


async def test_review_context_is_verbatim_and_untruncated(sandbox):
    """评审上下文构造（用户 2026-10-27 硬要求）：
    brief 原样、子卡三块原样、判据在、上一批次正稿在——**一律不截断**。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(1)
    long_brief = "【任务背景】" + "背" * 3000 + "\n【当前状况】做到第3步\n【要评审的内容】R1 收尾"
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": long_brief}, cards)
    rid = out["data"]["review_card_id"]
    await card_tools._h_set_criteria(
        wid, cid, {"review_card_id": rid, "text": "C-1 状态行三处一致"}, cards)

    st = await review_store.state_of(wid, rid)
    text = await review_runner._user_prompt(wid, rid, st)
    assert long_brief in text, "brief 必须原样进上下文（不得截断）"
    body = (await card_store.read_card(wid, cid))["body"]
    assert str(body.get("mission") or "") in text, "子卡使命原样进上下文"
    assert str(body.get("status_block") or "") in text, "子卡现状原样进上下文"
    # [2026-10-29 修·用户抓到] 评审对象是**草稿**，不是正稿。
    # 原来这里断言 requirements（正稿）进上下文，等于把 bug 钉成了规格。
    meta = (await card_store.read_card(wid, cid))["meta"]
    assert str(meta.get("requirements_draft") or "") in text, "待评审的草稿必须原样进上下文"
    assert str(body.get("requirements") or "") not in text, \
        "正稿（卡面模板说明）不该当评审对象塞进来"
    assert "C-1 状态行三处一致" in text, "判据进上下文"


async def test_review_card_not_in_canvas_or_nav(sandbox):
    """评审卡离树：不进画布卡列表、不进卡树（P5 结构上不可能，不靠前端过滤）。"""
    from src.api import routes

    wid, cid, fids = await _make_work_with_functions(1)
    rid = await review_store.review_card_of(wid, cid)
    assert rid, "伴生评审卡应已派生"
    canvas = await routes.get_work_canvas(wid)
    ids = {str(c.get("card_id")) for c in (canvas.get("cards") or [])}
    assert cid in ids, "子卡应在画布上"
    assert rid not in ids, "评审卡不得出现在画布卡列表"


async def test_review_card_dies_with_child(sandbox):
    """P7：子卡删了，伴生评审卡一起走（不留孤儿）。"""
    wid, cid, fids = await _make_work_with_functions(1)
    rid = await review_store.review_card_of(wid, cid)
    assert rid
    await card_store.delete_card(wid, cid, cascade=False)
    assert not card_store.card_exists(wid, rid), "子卡删了评审卡不该留"


async def test_verdict_visible_on_card_after_close(sandbox):
    """收口后正稿必须能在卡面上读到（P1：卡面是权威事实源，不只在 state 里）。"""
    wid, cid, fids = await _make_work_with_functions(1)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    sb = str((await card_store.read_card(wid, rid))["body"].get("status_block") or "")
    assert "全员通过" in sb, f"收口后卡面应能看到正稿，实际：{sb[:200]}"


# ---------------------------------------------------------------- 重评触发器
async def test_draft_edit_advances_version_but_keeps_votes(sandbox):
    """[2026-10-29 用户口径] 改**草稿**推进版本；已投的票**不作废**。

    「通过之后，它就不再被唤起进行后续的评审（因为还会有其他成员不通过，
    所以即便 A 通过，后面也会继续改继续评审）」

    票若随草稿失效：要么批次永远收不了口（票废了却不叫回来投），
    要么每轮全员重跑（叫回来投）——work_027 实跑就是后者，总裁被叫了三次。

    另外：改**现状块**不算草稿变。评审对象只是草稿，子卡写一句「评审中」
    不该把评审打回原点。
    """
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    st = await review_store.status(wid, rid)
    assert st["passed"] == [fids[0]] and st["draft"] == 1

    # 改现状块：**不**推进草稿版本，票不动
    await card_store.update_card(wid, cid, {"status_block": "新的现状"},
                                 "改现状")
    st1 = await review_store.status(wid, rid)
    assert st1["draft"] == 1, "现状块不是评审材料，不该推进草稿版本"
    assert st1["passed"] == [fids[0]]

    # 改草稿：推进版本，但票仍在
    await card_store.update_card(wid, cid,
                                 {"requirements_draft": "R2 按意见改过"},
                                 "改草稿")
    st2 = await review_store.status(wid, rid)
    assert st2["draft"] == 2, "改草稿 ⇒ 草稿版本必须推进"
    assert st2["passed"] == [fids[0]], "已通过的票不因草稿改版而作废"
    assert fids[1] in st2["pending"]
    assert st2["all_passed"] is False


async def test_maybe_rerun_is_noop_without_change(sandbox):
    """卡面没变就不推进版本（否则每写一次都空转一轮）。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    assert await review_runner.maybe_rerun(wid, cid) is False
    assert (await review_store.status(wid, rid))["draft"] == 1

    # 没有伴生评审卡的卡（评审卡自身）不触发
    assert await review_runner.maybe_rerun(wid, rid) is False


async def test_round_loop_revises_draft_then_reviews_again(sandbox, monkeypatch):
    """一轮发言后有人没过 ⇒ **叫子卡改稿** ⇒ 改了 ⇒ 再来一轮（收敛核心）。

    [2026-10-29 用户口径]「一次评审全员发言，子卡直接在评审卡内，对草稿进行
    修改，然后再全员评审，直到所有成员通过为止」。
    改前靠"版本推进"驱动循环，现在靠"子卡真的改了草稿"驱动。
    """
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    spoken: list[str] = []
    revised: list[int] = []

    async def fake_speak(work_id, review_id, role, st=None):
        spoken.append(role)
        # 第一轮全员不通过（只发言不投票）；第二轮起全部 approve
        if len(spoken) > len(fids):
            await review_store.approve(work_id, review_id, role)
        return {"role": role}

    async def fake_revise(work_id, review_id, st=None):
        revised.append(1)
        await card_store.update_card(
            wid, cid, {"requirements_draft": f"R{len(revised) + 1} 按意见改过"},
            "子卡改草稿")
        return {"role": cid, "changed": True}

    monkeypatch.setattr(review_runner, "speak", fake_speak)
    monkeypatch.setattr(review_runner, "revise", fake_revise)
    await review_runner._round_loop(wid, rid)

    # [2026-10-29] 全过之后不再自动收口（要子卡调 promote_draft），
    # `_round_loop` 因此可能多转一圈再停——改稿次数不再是精确 1。
    assert len(revised) >= 1, f"未通过后应叫子卡改稿，实际 {len(revised)}"
    # [2026-10-30] 全过不再自动收口（等 promote_draft），`_round_loop` 可能多转
    # 半圈才停；只断言「至少两轮全员都发过言」，不再钉精确次数。
    assert len(spoken) >= 4, f"至少两轮全员发言，实际 {spoken}"
    assert {"fn_001", "fn_002"} <= set(spoken)
    st = await review_store.status(wid, rid)
    assert st["all_passed"] is True
    assert st["draft"] == 2


async def test_round_loop_stops_when_child_refuses_to_change(sandbox, monkeypatch):
    """子卡看过意见但**不改** ⇒ 停在原地（不催办、不超时、不空转）。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    spoken: list[str] = []
    revised: list[int] = []

    async def fake_speak(work_id, review_id, role, st=None):
        spoken.append(role)
        return {"role": role}          # 永远不通过

    async def fake_revise(work_id, review_id, st=None):
        revised.append(1)
        return {"role": cid, "changed": False}   # 改了但没有实际改动

    monkeypatch.setattr(review_runner, "speak", fake_speak)
    monkeypatch.setattr(review_runner, "revise", fake_revise)
    await review_runner._round_loop(wid, rid)

    assert len(spoken) == 2, f"只该跑一轮，实际 {spoken}"
    assert len(revised) == 1, "应当叫过一次子卡改稿"
    assert (await review_store.status(wid, rid))["all_passed"] is False


async def test_round_loop_stops_when_nobody_can_speak(sandbox, monkeypatch):
    """一个成员都没表态成功（模型挂了）⇒ 停，不让子卡反复改材料空烧。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    revised: list[int] = []

    async def boom(*a, **k):
        raise RuntimeError("模型不可用")

    async def fake_revise(work_id, review_id, st=None):
        revised.append(1)
        return {"role": cid, "changed": True}

    monkeypatch.setattr(review_runner, "speak", boom)
    monkeypatch.setattr(review_runner, "revise", fake_revise)
    await review_runner._round_loop(wid, rid)

    assert revised == [], "没人能表态时不该再让子卡改稿"


async def test_run_round_not_reentrant(sandbox, monkeypatch):
    """同一张评审卡同时只跑一轮：已在跑就不重复起（否则同一版本评两遍）。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    async def boom(*a, **k):
        raise AssertionError("已在跑时不该再发言")

    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setattr(review_runner, "speak", boom)
    review_runner._INFLIGHT.add(rid)
    try:
        assert await review_runner.run_round(wid, rid) == []
    finally:
        review_runner._INFLIGHT.discard(rid)


# ---------------------------------------------------------------- 重评触发器（端到端接线）


async def test_card_write_only_bumps_draft_never_spawns(sandbox, monkeypatch):
    """子卡写卡面**只推进草稿版本，不起轮**（[2026-10-30 用户令]）。

    「谁让你做这么多独立的起轮口子」——写卡原来会顺手起一轮重评，而一次改稿里
    子卡会写十几次卡面 ⇒ 十几次起轮全撞在改稿那把锁上、秒回 busy、又被当成
    "还没跑完"继续叫 ⇒ 空转。写卡不是起轮的时机。
    """
    import asyncio
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    tasks: list = []
    real_spawn = review_runner.spawn_round

    def rec_spawn(w, r):
        t = real_spawn(w, r)
        tasks.append(t)
        return t

    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    monkeypatch.setattr(review_runner, "spawn_round", rec_spawn)

    before = (await review_store.status(wid, rid))["draft"]
    await card_store.update_card(wid, cid, {"requirements_draft": "改过的草稿"},
                                 "改草稿")
    await asyncio.sleep(0)

    assert tasks == [], f"写卡不该起轮，实际起了 {len(tasks)} 轮"
    after = (await review_store.status(wid, rid))["draft"]
    assert after > before, f"草稿版本要推进：{before} → {after}"


# ---------------------------------------------------------------- 工具面归属
def test_review_tools_scoped_to_right_roles():
    """评审五件各自归谁（P5：结构上不让它漏给不相干的卡）。

    - `approve` / `set_criteria` / `revoke_approve` / `decide` 不进基础池
      （否则 `tool` 卡也有 edit，会漏——test_cardperm_readopen 抓过）
    - `open_review` 挂建子卡开关（只有能拆活的卡才发起评审）
    - 总裁是评审成员 ⇒ 目录 allow 必须有 `approve`，否则它永远不通过、批次永远收不了口
    """
    import json
    from pathlib import Path

    from src.permissions import policy

    assert {"approve", "set_criteria", "revoke_approve", "decide"} \
        <= card_tools.FUNCTION_ONLY_TOOLS
    assert card_tools.TOOL_SWITCH["open_review"] == policy.SWITCH_CREATE_CHILD

    root = Path(__file__).resolve().parents[1]
    cat = json.loads((root / "config" / "function_catalog.json")
                     .read_text(encoding="utf-8"))
    ceo = [f for f in cat["functions"] if f["code"] == "ceo"][0]
    allow = set((ceo.get("toolface") or {}).get("allow") or [])
    assert {"approve", "set_criteria", "revoke_approve", "decide"} <= allow, allow


async def test_speak_accepts_flat_tool_calls(sandbox, monkeypatch):
    """网关的 tool_call 是**扁平**结构 {name, arguments}——approve 必须被执行。

    实弹跑出来的死结：原实现按 OpenAI 嵌套形态取 tc["function"]["name"]，
    拿到空串 ⇒ 每次正确的 approve 都被判"只允许 approve"拒掉 ⇒
    **全员通过永远不可能发生**。这条钉住两种形态都认。
    """
    import json as _json

    from src.api import model_service, review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    async def flat_call(*a, **k):
        return ({"content": "", "tool_calls": [
            {"name": "approve", "arguments": {"review_card_id": rid}}]},
            "fake-model")

    monkeypatch.setattr(model_service, "call_main", flat_call)
    got = await review_runner.speak(wid, rid, fids[0])
    assert got["tools"] and got["tools"][0]["ok"] is True, got
    st = await review_store.status(wid, rid)
    assert fids[0] in st["passed"], st

    async def nested_call(*a, **k):
        return ({"content": "", "tool_calls": [
            {"function": {"name": "approve",
                          "arguments": _json.dumps({"review_card_id": rid})}}]},
            "fake-model")

    monkeypatch.setattr(model_service, "call_main", nested_call)
    got2 = await review_runner.speak(wid, rid, fids[1])
    assert got2["tools"] and got2["tools"][0]["ok"] is True, got2


async def test_status_after_close_reports_passed(sandbox):
    """收口后 batch 清空，status 仍要报 all_passed=True（实弹收口后读不出来）。"""
    wid, cid, fids = await _make_work_with_functions(1)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)

    # [2026-10-29] 全员通过只是「可以定稿」，批次留着等 promote_draft
    st0 = await review_store.status(wid, rid)
    assert st0["all_passed"] is True and st0["pending"] == []
    assert st0["batch"] != "", "全过不该提前清批次（清了就没人去写正稿）"

    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)

    st = await review_store.status(wid, rid)
    assert st["batch"] == "" and st["closed"] is True
    assert st["all_passed"] is True, st
    assert st["pending"] == []
    assert st["verdict"]


async def test_status_after_close_reports_passed(sandbox):
    """收口后 batch 清空，status 仍要报 all_passed=True（实弹收口后读不出来）。"""
    wid, cid, fids = await _make_work_with_functions(1)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)

    # [2026-10-29] 全员通过只是「可以定稿」，批次留着等 promote_draft
    st0 = await review_store.status(wid, rid)
    assert st0["all_passed"] is True and st0["pending"] == []
    assert st0["batch"] != "", "全过不该提前清批次（清了就没人去写正稿）"

    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)

    st = await review_store.status(wid, rid)
    assert st["batch"] == "" and st["closed"] is True
    assert st["all_passed"] is True, st
    assert st["pending"] == []
    assert st["verdict"]


async def test_full_approve_promotes_draft_to_final(sandbox):
    """全员通过 ⇒ **草稿转正稿**（`requirements` ← `requirements_draft`）。

    [2026-10-29] 这是「确保它通过」的机械证据：三席都调 approve 之后，
    子卡的 requirements 必须变成草稿的内容、state 变 approved。
    实弹里跑到第 23 版没收口，就是因为有人只在文字里说通过、没调 approve。
    """
    wid, cid, fids = await _make_work_with_functions(3)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]

    before = (await card_store.read_card(wid, cid))
    final_before = str(before["body"].get("requirements") or "")
    draft = str(before["meta"].get("requirements_draft") or "")
    assert draft, "送审前必须有草稿"

    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)

    # [2026-10-29 用户口径「最后也一定是调工具写正稿为结束」]
    # 全员通过**不再自动转正稿**：要子卡自己调 promote_draft。
    mid = await card_store.read_card(wid, cid)
    assert mid["meta"].get("requirements_state") != "approved", \
        "全员通过只是「可以定稿」，系统不该代劳"
    pr = await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)
    assert pr.get("ok"), pr

    after = await card_store.read_card(wid, cid)
    assert after["meta"].get("requirements_state") == "approved"
    assert str(after["body"].get("requirements") or "") == draft, \
        "正稿必须换成草稿的内容"
    assert str(after["body"].get("requirements") or "") != final_before or not final_before


async def test_full_approve_promotes_draft_to_final(sandbox):
    """全员通过 ⇒ **草稿转正稿**（`requirements` ← `requirements_draft`）。

    [2026-10-29] 这是「确保它通过」的机械证据：三席都调 approve 之后，
    子卡的 requirements 必须变成草稿的内容、state 变 approved。
    实弹里跑到第 23 版没收口，就是因为有人只在文字里说通过、没调 approve。
    """
    wid, cid, fids = await _make_work_with_functions(3)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]

    before = (await card_store.read_card(wid, cid))
    final_before = str(before["body"].get("requirements") or "")
    draft = str(before["meta"].get("requirements_draft") or "")
    assert draft, "送审前必须有草稿"

    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)

    # [2026-10-29 用户口径「最后也一定是调工具写正稿为结束」]
    # 全员通过**不再自动转正稿**：要子卡自己调 promote_draft。
    mid = await card_store.read_card(wid, cid)
    assert mid["meta"].get("requirements_state") != "approved",         "全员通过只是「可以定稿」，系统不该代劳"
    pr = await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)
    assert pr.get("ok"), pr

    after = await card_store.read_card(wid, cid)
    assert after["meta"].get("requirements_state") == "approved"
    assert str(after["body"].get("requirements") or "") == draft, \
        "正稿必须换成草稿的内容"
    assert str(after["body"].get("requirements") or "") != final_before or not final_before


def test_parse_text_tool_calls():
    """文本形态的工具调用要能还原（[2026-10-29] 实弹：economy 模型把调用写成正文）。

    `<tool_call>{"name":..., "arguments":{...}}</tool_call>` —— 网关只认结构化
    delta.tool_calls，这些调用原来一个都不执行，模型却以为改了。
    """
    from src.api.model_service import _parse_text_tool_calls

    text = (
        "核过两轮意见，逐条改了。\n\n"
        '<tool_call>\n{"name": "edit_requirements", "arguments": {"card_id": "c1", "block": "需求文档"}}\n</tool_call>\n'
        '<tool_call>{"name": "read_card", "arguments": {"card_id": "c2"}}</tool_call>\n'
    )
    got = _parse_text_tool_calls(text)
    assert [c["name"] for c in got] == ["edit_requirements", "read_card"]
    assert got[0]["arguments"]["block"] == "需求文档"

    # 不是合法 JSON / 缺 name ⇒ 当正文，不当调用
    assert _parse_text_tool_calls('<tool_call>{不是json}</tool_call>') == []
    assert _parse_text_tool_calls('<tool_call>{"arguments": {}}</tool_call>') == []
    # 没有 tool_call 块 ⇒ 空
    assert _parse_text_tool_calls("只是说话") == []


def test_parse_text_tool_calls():
    """文本形态的工具调用要能还原（[2026-10-29] 实弹：economy 模型把调用写成正文）。

    `<tool_call>{"name":..., "arguments":{...}}</tool_call>` —— 网关只认结构化
    delta.tool_calls，这些调用原来一个都不执行，模型却以为改了。
    """
    from src.api.model_service import _parse_text_tool_calls

    text = (
        "核过两轮意见，逐条改了。\n\n"
        '<tool_call>\n{"name": "edit_requirements", "arguments": {"card_id": "c1", "block": "需求文档"}}\n</tool_call>\n'
        '<tool_call>{"name": "read_card", "arguments": {"card_id": "c2"}}</tool_call>\n'
    )
    got = _parse_text_tool_calls(text)
    assert [c["name"] for c in got] == ["edit_requirements", "read_card"]
    assert got[0]["arguments"]["block"] == "需求文档"

    # 不是合法 JSON / 缺 name ⇒ 当正文，不当调用
    assert _parse_text_tool_calls('<tool_call>{不是json}</tool_call>') == []
    assert _parse_text_tool_calls('<tool_call>{"arguments": {}}</tool_call>') == []
    # 没有 tool_call 块 ⇒ 空
    assert _parse_text_tool_calls("只是说话") == []


async def test_closeout_feeds_back_to_child_conversation(sandbox):
    """收口后结果要回喂到**子卡自己的会话**（[2026-10-29 用户令]）。

    「评审结束，你得回到对话里，将结果回喂给对话」——否则子卡下一轮醒来
    只看到自己改过草稿，不知道评审已过、正稿是哪一份。
    """
    from src.storage import session_facade as sf

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]

    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)
    # 收口＝正稿写完那一刻（不是全员通过那一刻）
    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)

    path = sf._session_path(wid, cid)
    assert path.exists(), "子卡会话文件应存在"
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert "评审通过" in text, "收口结果必须写进子卡会话"
    assert "正稿" in text


async def test_closeout_feeds_back_to_child_conversation(sandbox):
    """收口后结果要回喂到**子卡自己的会话**（[2026-10-29 用户令]）。

    「评审结束，你得回到对话里，将结果回喂给对话」——否则子卡下一轮醒来
    只看到自己改过草稿，不知道评审已过、正稿是哪一份。
    """
    from src.storage import session_facade as sf

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]

    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)
    # 收口＝正稿写完那一刻（不是全员通过那一刻）
    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)

    path = sf._session_path(wid, cid)
    assert path.exists(), "子卡会话文件应存在"
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert "评审通过" in text, "收口结果必须写进子卡会话"
    assert "正稿" in text


def test_review_seat_can_read_any_card():
    """评审席上的人能读整棵树的卡面（[2026-10-29 用户令]「卡面才是唯一事实源」）。

    评审成员本身是普通卡/职能卡，按常规作用域读不到远亲；坐进评审席后
    要能校对全树。只放开**读**——写仍走原规则表。
    """
    import pytest as _pytest

    from src.permissions import policy

    # perms 是卡上固化的开关清单（has_perm 照读）；这里给两张卡都授 read
    cards = {
        "card_a": {"card_id": "card_a", "parent": "", "card_type": "sub_project",
                   "perms": ["read", "edit"]},
        "card_b": {"card_id": "card_b", "parent": "card_x", "card_type": "sub_project",
                   "perms": ["read", "edit"]},
        "card_x": {"card_id": "card_x", "parent": "", "card_type": "sub_project",
                   "perms": ["read", "edit"]},
    }
    # 坐上评审席：放开读
    seat = dict(cards["card_a"])
    seat["_review_seat"] = True
    cards["card_a"] = seat
    assert policy.check("card_a", "card_b", policy.SWITCH_READ, cards) is True

    # 写不放开：评审席只放开读，写仍走原规则表
    with _pytest.raises(PermissionError):
        policy.check("card_a", "card_b", policy.SWITCH_EDIT, cards)


def test_review_seat_can_read_any_card():
    """评审席上的人能读整棵树的卡面（[2026-10-29 用户令]「卡面才是唯一事实源」）。

    评审成员本身是普通卡/职能卡，按常规作用域读不到远亲；坐进评审席后
    要能校对全树。只放开**读**——写仍走原规则表。
    """
    import pytest as _pytest

    from src.permissions import policy

    # perms 是卡上固化的开关清单（has_perm 照读）；这里给两张卡都授 read
    cards = {
        "card_a": {"card_id": "card_a", "parent": "", "card_type": "sub_project",
                   "perms": ["read", "edit"]},
        "card_b": {"card_id": "card_b", "parent": "card_x", "card_type": "sub_project",
                   "perms": ["read", "edit"]},
        "card_x": {"card_id": "card_x", "parent": "", "card_type": "sub_project",
                   "perms": ["read", "edit"]},
    }
    # 坐上评审席：放开读
    seat = dict(cards["card_a"])
    seat["_review_seat"] = True
    cards["card_a"] = seat
    assert policy.check("card_a", "card_b", policy.SWITCH_READ, cards) is True

    # 写不放开：评审席只放开读，写仍走原规则表
    with _pytest.raises(PermissionError):
        policy.check("card_a", "card_b", policy.SWITCH_EDIT, cards)


async def test_run_once_does_not_revise_when_member_approved(sandbox, monkeypatch):
    """成员**通过**时不该叫子卡改稿（[2026-10-30 实弹] card_116）。

    原来一步里无条件改稿：总裁 approve → 叫子卡改 → 子卡说「不用改」
    ⇒ changed=False ⇒ 判定到站 ⇒ 另外两个成员一次都没轮到，批次永久停住。
    改后：没人 reject 就不改稿，只往下叫下一个成员。
    """
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(3)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    revised: list[int] = []

    async def fake_speak(work_id, review_id, role, st=None):
        # 只发言、调 approve，不改稿
        await review_store.approve(work_id, review_id, role)
        return {"role": role, "tools": [{"tool": "approve", "ok": True}]}

    async def fake_revise(work_id, review_id, st=None, **kw):
        revised.append(1)
        return {"role": cid, "changed": False}

    monkeypatch.setattr(review_runner, "speak", fake_speak)
    monkeypatch.setattr(review_runner, "revise", fake_revise)
    # conftest 默认关掉自动起轮；这条测的就是 run_once 本体，得放行
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    r1 = await review_runner.run_once(wid, rid)
    assert r1["reason"] == "spoken", r1
    assert r1["continued"] is True, "还没全过 ⇒ 要继续叫下一个"
    assert revised == [], "成员通过时不该叫子卡改稿"
