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
    review_codes = ("ceo", "pm", "cto")
    for i in range(n):
        code = review_codes[i] if i < len(review_codes) else f"f{i}"
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": code}})
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


async def _seated(wid: str, rid: str, *fids: str) -> None:
    """让席位**落座**＝模拟"它被叫过一轮"：`_seat_turn` 开场落的两件机械
    事实（mark_seen 回应水位 + mark_spoken 说过话）一起补上。

    投票闸已拆（2026-10-31 用户令：投票零限制），这两件只喂挑席判据。
    """
    for f in fids:
        await review_store.mark_seen(wid, rid, f)
        await review_store.mark_spoken(wid, rid, f)


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


async def test_review_read_tools_follow_each_function_abilities(sandbox):
    """席位工具面＝`build_tool_schemas(act_as)`（它自己的能力上限）∩ 场授面。

    [venue 2026-10-31] 场授面＝全部只读件 ∪ {approve,reject}（[两源模型
    2026-11-01] write_ledger 退役）；读件跟着**席位自己的能力挂载**走——
    不再是评审代码手搓的固定清单。
    """
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    normal_face_before = {t["name"] for t in card_tools.build_tool_schemas(cid, wid, cards)}
    # 模拟两个受邀席位各自不同的能力挂载。
    cards[fids[0]] = {"card_id": fids[0], "card_type": "function",
                      "mode": "manual", "abilities": ["read_card"]}
    cards[fids[1]] = {"card_id": fids[1], "card_type": "function",
                      "mode": "manual", "abilities": ["read_files"]}

    venue = review_runner._seat_toolface()
    first = {t["name"] for t in card_tools.build_tool_schemas(
        fids[0], wid, cards)} & venue
    second = {t["name"] for t in card_tools.build_tool_schemas(
        fids[1], wid, cards)} & venue
    assert "read_card" in first
    assert not ({"read_file", "list_files", "search_files", "glob_files"} & first)
    assert {"read_file", "list_files", "search_files", "glob_files"} <= second
    assert "read_card" not in second
    # 表态件由场授予（目录 allow 或固定追加），不随读件能力变。
    assert "approve" in first and "approve" in second
    assert all(name in review_runner.VENUE_GRANT or
               card_tools.TOOL_REGISTRY[name].readonly
               for name in first | second)
    normal_face_after = {t["name"] for t in card_tools.build_tool_schemas(cid, wid, cards)}
    assert normal_face_after == normal_face_before, "评审场景筛选不得改变被评子卡的常规工具面"


async def test_seat_turn_passes_venue_toolface_and_anchors(sandbox, monkeypatch):
    """席位回合＝普通 `_run_message` 的三向锚点装配（[venue 2026-10-31]）。

    钉装配契约：执行锚＝评审卡、身份锚＝席位（act_as）、上下文锚＝被评子卡
    （ctx_card）、场授面＝tool_allow、mandate 不落会话（append_user=False）、
    source 带 review_step。席位能力∩场授面的收窄由 build_context 自己完成。
    """
    from src.api import review_runner, routes

    wid, cid, fids = await _make_work_with_functions(2)
    rid = await review_store.ensure_review_card(wid, cid, "子项目甲")
    await review_store.open_batch(wid, rid, "判据", members=fids, target=cid)
    st = await review_store.state_of(wid, rid)

    captured: dict = {}

    async def fake_run(work_id, card_id, content, **kw):
        captured.update(kw)
        captured["work_id"] = work_id
        captured["card_id"] = card_id
        captured["content"] = content
        return {"reply": "（假回合）"}

    monkeypatch.setattr(routes, "_run_message", fake_run)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")

    assert captured["card_id"] == rid, "执行锚＝评审卡（会话/锁/事件流在它）"
    assert captured["act_as"] == fids[0], "身份锚＝席位"
    assert captured["ctx_card"] == cid, "上下文锚＝被评子卡"
    assert captured["append_user"] is False, "mandate 不落会话"
    src = captured.get("source") or {}
    assert src.get("kind") == "review_step" and src.get("seat") == fids[0]
    assert src.get("batch"), "source 带批次号"
    face = set(captured.get("tool_allow") or set())
    assert {"approve", "reject"} <= face
    assert "edit_card" not in face and "bash" not in face, "场授面只放读件+表态"
    meta = captured.get("act_meta") or {}
    assert meta.get("function_id") == fids[0]
    assert "R1 草稿（测试用）" in captured["content"], "mandate 带评审材料"


async def test_open_review_invites_only_ceo_pm_cto(sandbox):
    """固定邀请集只有总裁、产品经理、技术总监；其他在职职能不进名单。"""
    wid, cid, fids = await _make_work_with_functions(4)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    assert out.get("ok") is True, out
    assert out["data"]["members"] == fids[:3]
    assert fids[3] not in out["data"]["members"]


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

    await _seated(wid, rid, *fids)
    a = await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    assert a["data"]["all_passed"] is False and len(a["data"]["pending"]) == 2

    await card_tools._h_approve(wid, fids[1], {"review_card_id": rid}, cards)
    last = await card_tools._h_approve(wid, fids[2], {"review_card_id": rid}, cards)
    assert last["data"]["all_passed"] is True and last["data"]["pending"] == []



async def test_status_block_is_rendered_by_system(sandbox):
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(wid, cid, {"card_id": cid, "brief": "背景/现状/评审内容"}, cards)
    rid = out["data"]["review_card_id"]
    await _seated(wid, rid, fids[0])
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    body = (await card_store.read_card(wid, rid))["body"]
    sb = str(body.get("status_block") or "")
    assert "通过 1/2" in sb, f"现状块应由系统渲染出进度，实际：{sb[:200]}"
    assert fids[0] in sb and fids[1] in sb


async def test_review_context_is_verbatim_and_untruncated(sandbox):
    """评审 mandate 材料（用户 2026-10-27 硬要求 ＋ 2026-11 去重令）：
    brief 原样、**草稿**原样——这两样是装配层给不了的唯一通道；
    子卡使命/现状/正稿由 ctx_card 的 bundle 供给，mandate **不再复抄**。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(1)
    long_brief = "【任务背景】" + "背" * 3000 + "\n【当前状况】做到第3步\n【要评审的内容】R1 收尾"
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": long_brief}, cards)
    rid = out["data"]["review_card_id"]

    st = await review_store.state_of(wid, rid)
    text = await review_runner._materials(wid, rid, st)
    assert long_brief in text, "brief 必须原样进上下文（不得截断）"
    body = (await card_store.read_card(wid, cid))["body"]
    meta = (await card_store.read_card(wid, cid))["meta"]
    assert str(meta.get("requirements_draft") or "") in text, \
        "待评审的草稿必须原样进上下文——它不进 bundle/read_card，这是唯一通道"
    # [去重令] 子卡卡面走 ctx_card 装配的 bundle：mandate 里再出现＝送两次
    assert str(body.get("mission") or "") not in text, "使命已在 bundle，mandate 不复抄"
    assert str(body.get("status_block") or "") not in text, "现状已在 bundle，mandate 不复抄"
    assert str(body.get("requirements") or "") not in text, \
        "正稿已在 bundle（上一批正稿≈它），mandate 不复抄"


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
    await _seated(wid, rid, fids[0])
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
    await _seated(wid, rid, fids[0])
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


async def test_step_flow_revises_draft_then_reviews_again(sandbox, monkeypatch):
    """一步＝一个机械动作，harness 按事件接力：否决 ⇒ 派活改稿 ⇒ 回应到 ⇒ 重评。

    [venue 2026-10-31] 一次唤醒一个动作：`_run_step` 只读 `review_state`
    机械算下一步（叫一席／派活／到站），**没有循环**——"再评一轮"是
    多次唤醒的接力，不是进程内的 loop。
    """
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    seats: list[tuple[str, str]] = []
    dispatched: list[str] = []

    async def fake_seat(work_id, review_id, seat, st, *, kind, hint=""):
        seats.append((seat, kind))
        await review_store.mark_spoken(work_id, review_id, seat)
        await review_store.mark_seen(work_id, review_id, seat)
        cur = int(st.get("draft") or 1)
        if cur == 1:
            await review_store.reject(work_id, review_id, seat)
            return {"seat": seat, "kind": kind, "voted": True, "vote": "reject"}
        await review_store.approve(work_id, review_id, seat)
        return {"seat": seat, "kind": kind, "voted": True, "vote": "approve"}

    async def fake_dispatch(work_id, review_id, st, *, kind):
        dispatched.append(kind)
        if kind == "revise":
            await review_store.mark_asked(work_id, review_id)
            await card_store.update_card(
                wid, cid, {"requirements_draft": "R2 按意见改过"}, "子卡改草稿")
            await review_store.mark_responded(work_id, review_id)
        else:
            # 照真件 _dispatch_child：派过定稿要落 promote_asked，
            # 否则每次唤醒都会重复派（harness 续跑是真接力的）。
            await review_store.mark_promote_asked(work_id, review_id)
        return True

    monkeypatch.setattr(review_runner, "_seat_turn", fake_seat)
    monkeypatch.setattr(review_runner, "_dispatch_child", fake_dispatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    # [harness 驱动 2026-11-01] round_done{has_next} 会让策略真续跑——手动
    # run_once 只是额外点火；撞 _RUNNING 的迭代让出轮转给派生步跑完。
    import asyncio as _aio
    for _ in range(120):
        r = await review_runner.run_once(wid, rid)
        st_now = await review_store.status(wid, rid)
        if st_now.get("all_passed") and "promote" in dispatched:
            break
        await _aio.sleep(0)

    assert dispatched == ["revise", "promote"], dispatched
    assert seats[:2] == [(fids[0], "speak"), (fids[1], "speak")], seats
    assert {s for s, _k in seats[2:]} == set(fids[:2]), "改版后全员再评一轮"
    st = await review_store.status(wid, rid)
    assert st["all_passed"] is True
    assert st["draft"] == 2


async def test_step_stops_when_child_turn_is_pending(sandbox, monkeypatch):
    """已派活等正卡交回 ⇒ 到站，不重复派（`asked_seq > responded_seq`）。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    # 两席都已落座且否掉（机械状态直接落，不跑回合）。
    for f in fids:
        await review_store.mark_spoken(wid, rid, f)
        await review_store.mark_seen(wid, rid, f)
        await review_store.reject(wid, rid, f)
    await review_store.mark_asked(wid, rid)      # 已派过一次活，还没交回

    async def never_dispatch(*a, **k):
        raise AssertionError("等回应期间不该再派活")

    monkeypatch.setattr(review_runner, "_dispatch_child", never_dispatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    r = await review_runner.run_once(wid, rid)
    assert r["reason"] == "awaiting_target" and r["continued"] is False


async def test_step_stops_when_seat_turn_fails(sandbox, monkeypatch):
    """席位回合失败（模型挂）⇒ 这一步如实报 error、不续跑（空烧坑）。

    [venue 2026-10-31] 席位回合的异常不穿透 `run_once`：`review_round_done`
    照常落（continued=false），harness 不会无限续叫挂掉的模型。
    """
    from src.api import review_runner, routes

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    dispatched: list[str] = []

    async def boom(*a, **k):
        raise RuntimeError("模型不可用")

    async def fake_dispatch(work_id, review_id, st, *, kind):
        dispatched.append(kind)
        return True

    monkeypatch.setattr(routes, "_run_message", boom)
    monkeypatch.setattr(review_runner, "_dispatch_child", fake_dispatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    r = await review_runner.run_once(wid, rid)

    assert r["reason"] == "seat_done" and r["continued"] is False, r
    assert r.get("error"), "失败要如实上报"
    assert dispatched == [], "席位回合挂了不该派活给子卡"


async def test_run_once_not_reentrant(sandbox, monkeypatch):
    """同一张评审卡同时只跑一步：已在跑就不重复起（否则同一版本评两遍）。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    async def boom(*a, **k):
        raise AssertionError("已在跑时不该再发言")

    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setattr(review_runner, "_seat_turn", boom)
    review_runner._RUNNING.add(f"{wid}/{rid}")
    try:
        r = await review_runner.run_once(wid, rid)
        assert r["reason"] == "already_running" and r["continued"] is False
    finally:
        review_runner._RUNNING.discard(f"{wid}/{rid}")


async def test_all_passed_inside_failed_turn_still_promotes(
        sandbox, monkeypatch, tmp_path):
    """[实跑回放 2026-11-01] 末席在回合内投出通过票：

      1. `review_all_passed` 当场发 ⇒ 续跑唤醒**撞上**席位步还在占的
         `_RUNNING`（旧版这把火就丢了）；
      2. 该席位回合随后出错 ⇒ `round_done{continued:false, has_next:true}`
         由策略**再点火** ⇒ run_once 派活请正卡定稿。

    钉的是：撞锁不丢火——事实（has_next）在回合自然收口后再被消费。

    [2026-11-01 · 策略隔离配套] conftest 把 `ECO_POLICIES_PATH` 指向空表
    （staff_* deploy 策略防真点火）——本用例钉的恰是**策略续跑**，自己
    把两条评审策略装进临时文件再换单例（先例：test_deploy_runner.py）。
    """
    import json

    from src.api import review_runner
    from src.infra import policy_config, policy_set as ps
    from tests import async_settle

    pols = {"policies": [
        {"id": "review_all_passed_need_promote", "active": True,
         "scope": {"kind": "card"}, "watch": ["review_all_passed"],
         "window": "none",
         "when": {"event": {"types": ["review_all_passed"], "within": "turn"}},
         "then": {"target": "self", "mode": "continue", "wake": True,
                  "message": "（续接）全员通过，请正卡定稿。"}},
        {"id": "review_step_not_finished", "active": True,
         "scope": {"kind": "card"}, "watch": ["review_round_done"],
         "window": "none",
         "when": {"any": [
             {"event": {"types": ["review_round_done"],
                        "where": {"continued": True}, "within": "turn"}},
             {"event": {"types": ["review_round_done"],
                        "where": {"has_next": True}, "within": "turn"}}]},
         "then": {"target": "self", "mode": "continue", "wake": True,
                  "message": "（续接）评审还没结束，继续跑下一步。"}},
    ]}
    pf = tmp_path / "review_pols.json"
    pf.write_text(json.dumps(pols, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ECO_POLICIES_PATH", str(pf))
    pset = ps.PolicySet()
    assert pset.load(policy_config.load_raw()) == []
    ps.reset_set(pset)

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    dispatched: list[str] = []
    n_calls = {"n": 0}

    async def fake_seat(work_id, review_id, seat, st, *, kind, hint=""):
        n_calls["n"] += 1
        await review_store.mark_spoken(work_id, review_id, seat)
        await review_store.mark_seen(work_id, review_id, seat)
        # 末席在**回合内**调真 handler 投票——review_all_passed 的续跑
        # 此刻必撞 _RUNNING（外层 run_once 正占着）⇒ already_running。
        ap = await card_tools._h_approve(
            wid, seat, {"review_card_id": rid}, card_store.iter_card_metas(wid))
        assert ap.get("ok"), ap
        last = n_calls["n"] == 2
        err = "turn_http_502" if last else ""
        return {"seat": seat, "kind": kind, "voted": True, "vote": "approve",
                "error": err,
                "_done": {"reason": f"seat:{kind}", "seat": seat,
                          "voted": True, "continued": not err,
                          "error": err}}

    async def fake_dispatch(work_id, review_id, st, *, kind):
        dispatched.append(kind)
        await review_store.mark_promote_asked(work_id, review_id)
        return True

    monkeypatch.setattr(review_runner, "_seat_turn", fake_seat)
    monkeypatch.setattr(review_runner, "_dispatch_child", fake_dispatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    await review_runner.run_once(wid, rid)   # 第一席（harness 接力跑后面）
    # settle() 会被常驻后台任务拖到超时——谓词即断言：等 promote 真派出。
    await async_settle.wait_until(
        lambda: "promote" in dispatched,
        what="末席回合崩了但票已齐 ⇒ has_next 该把派活步重新点火")

    assert n_calls["n"] == 2, "两席都应各被叫过一次"
    assert dispatched == ["promote"], (
        "末席回合崩了但票已齐 ⇒ has_next 该把派活步重新点火")
    st = await review_store.state_of(wid, rid)
    assert st.get("promote_asked"), "派活已落 ⇒ promote_asked 机械事实在"


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

    async def noop_seat(*a, **k):
        return {}

    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    monkeypatch.setattr(review_runner, "spawn_round", rec_spawn)
    monkeypatch.setattr(review_runner, "_seat_turn", noop_seat)

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

    - `approve` / `revoke_approve` / `decide` 不进基础池
      （否则 `tool` 卡也有 edit，会漏——test_cardperm_readopen 抓过）
    - `open_review` 挂建子卡开关（只有能拆活的卡才发起评审）
    - 总裁是评审成员 ⇒ 目录 allow 必须有 `approve`，否则它永远不通过、批次永远收不了口
    """
    import json
    from pathlib import Path

    from src.permissions import policy

    assert {"approve", "revoke_approve", "decide"} <= card_tools.FUNCTION_ONLY_TOOLS
    assert "set_criteria" not in card_tools.FUNCTION_ONLY_TOOLS, \
        "set_criteria 已删除（用户：不要判据）"
    assert "set_criteria" not in card_tools.TOOL_REGISTRY, "工具本身必须已摘除"
    assert card_tools.TOOL_SWITCH["open_review"] == policy.SWITCH_CREATE_CHILD

    root = Path(__file__).resolve().parents[1]
    cat = json.loads((root / "config" / "function_catalog.json")
                     .read_text(encoding="utf-8"))
    ceo = [f for f in cat["functions"] if f["code"] == "ceo"][0]
    allow = set((ceo.get("toolface") or {}).get("allow") or [])
    assert {"approve", "revoke_approve", "decide"} <= allow, allow


async def test_seat_turn_accepts_flat_tool_calls(sandbox, monkeypatch):
    """网关的 tool_call 是**扁平**结构 {name, arguments}——approve 必须被执行。

    实弹跑出来的死结：按 OpenAI 嵌套形态取 tc["function"]["name"]，拿到空串
    ⇒ 每次正确的 approve 都被当"没表态"漏掉 ⇒ **全员通过永远不可能发生**。
    这条钉住两种形态都认——现在走普通回合（`_seat_turn` → `_run_message`），
    工具派发与所有卡同一通道。
    """
    import json as _json

    from src.api import model_service, review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    st = await review_store.state_of(wid, rid)

    n1 = {"c": 0}

    async def flat_call(**kw):
        # call_main 是 kwargs 调用；工具循环无封顶——调用只发一次，之后纯文本。
        n1["c"] += 1
        if n1["c"] == 1:
            return ({"content": "意见说完", "tool_calls": [
                {"name": "approve", "arguments": {"review_card_id": rid}}]},
                    "fake-model")
        return ({"content": "说完了", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", flat_call)
    got = await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")
    assert got.get("voted") is True and got.get("vote") == "approve", got
    st1 = await review_store.status(wid, rid)
    assert fids[0] in st1["passed"], st1

    n2 = {"c": 0}

    async def nested_call(**kw):
        n2["c"] += 1
        if n2["c"] == 1:
            return ({"content": "意见说完", "tool_calls": [
                {"function": {"name": "approve",
                              "arguments": _json.dumps({"review_card_id": rid})}}]},
                    "fake-model")
        return ({"content": "说完了", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", nested_call)
    got2 = await review_runner._seat_turn(wid, rid, fids[1], st, kind="speak")
    assert got2.get("voted") is True and got2.get("vote") == "approve", got2


async def test_status_after_close_reports_passed(sandbox):
    """收口后 batch 清空，status 仍要报 all_passed=True（实弹收口后读不出来）。"""
    wid, cid, fids = await _make_work_with_functions(1)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]
    await _seated(wid, rid, fids[0])
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
    await _seated(wid, rid, fids[0])
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

    await _seated(wid, rid, *fids)
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

    await _seated(wid, rid, *fids)
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

    await _seated(wid, rid, *fids)
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

    await _seated(wid, rid, *fids)
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

    dispatched: list[str] = []

    async def fake_seat(work_id, review_id, seat, st=None, *, kind, hint=""):
        # 只发言、调 approve，不改稿
        await review_store.mark_spoken(work_id, review_id, seat)
        await review_store.mark_seen(work_id, review_id, seat)
        await review_store.approve(work_id, review_id, seat)
        return {"seat": seat, "kind": kind, "voted": True, "vote": "approve"}

    async def fake_dispatch(work_id, review_id, st=None, *, kind):
        dispatched.append(kind)
        return True

    monkeypatch.setattr(review_runner, "_seat_turn", fake_seat)
    monkeypatch.setattr(review_runner, "_dispatch_child", fake_dispatch)
    # conftest 默认关掉自动起轮；这条测的就是 run_once 本体，得放行
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    r1 = await review_runner.run_once(wid, rid)
    assert r1["reason"] == "seat_done", r1
    assert r1["continued"] is True, "还没全过 ⇒ 要继续叫下一个"
    assert r1["seat"] == fids[0] and r1["voted"] is True
    assert dispatched == [], "成员通过时不该叫子卡改稿"


# ---------------------------------------------------------------- 票仓机械事实
async def test_vote_gate_member_only(sandbox):
    """[2026-10-31 用户令] 投票零限制：成员随时可投（不查落没落座），
    非成员永远不可投。"""
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    # 没落座也能投——先不先发言是席位自己的分寸，机制不闸
    ok = await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    assert ok.get("ok"), ok

    # 非成员（含被评卡自己）永远拒
    bad = await card_tools._h_approve(
        wid, cid, {"review_card_id": rid}, cards)
    assert bad.get("ok") is not True
    assert "成员" in str(bad.get("error") or bad), bad


async def test_reject_overwrites_approve_same_seat(sandbox):
    """真票：同席位先 approve 后 reject ⇒ 不通过（后票覆盖前票）。"""
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    await _seated(wid, rid, fids[0])
    await card_tools._h_approve(wid, fids[0], {"review_card_id": rid}, cards)
    assert fids[0] in (await review_store.status(wid, rid))["passed"]

    r = await card_tools._h_reject(wid, fids[0], {"review_card_id": rid}, cards)
    assert r.get("ok"), r
    st = await review_store.status(wid, rid)
    assert fids[0] not in st["passed"]
    assert fids[0] in st["rejected"], st
    assert st["all_passed"] is False


async def test_repeated_reject_is_just_a_vote(sandbox):
    """[2026-10-31 用户令] 同稿同席拒多少次都只是一张票——不设机械僵局
    检测、不落 divergence、不发 review_deadlock。收敛靠上下文，不靠机制数。"""
    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    await _seated(wid, rid, fids[0])
    r1 = await card_tools._h_reject(wid, fids[0], {"review_card_id": rid}, cards)
    r2 = await card_tools._h_reject(wid, fids[0], {"review_card_id": rid}, cards)
    assert r1.get("ok") and r2.get("ok"), (r1, r2)
    assert "deadlock" not in (r1.get("data") or {})
    assert "deadlock" not in (r2.get("data") or {})

    st = await review_store.state_of(wid, rid)
    assert not st.get("deadlock"), "状态里不该有僵局标记"
    st1 = await review_store.status(wid, rid)
    assert fids[0] in st1["rejected"], st1
    # [两源模型 2026-11-01] divergence 台账已随 write_ledger 退役——
    # 分歧走票+sync 上浮+事件流，不留私账。


async def test_rejected_seat_dispatches_revise_not_deadlock(sandbox, monkeypatch):
    """否决席没有新回应可看 ⇒ 这一步＝派活请正卡回应（不是挂待裁停住）。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    # 两席连拒两次（用户令：重复否决不设机制结局）
    await _seated(wid, rid, *fids)
    for _ in range(2):
        for f in fids:
            await card_tools._h_reject(wid, f, {"review_card_id": rid}, cards)

    dispatched: list[str] = []

    async def fake_dispatch(work_id, review_id, st, *, kind):
        dispatched.append(kind)
        if kind == "revise":
            await review_store.mark_asked(work_id, review_id)
        return True

    monkeypatch.setattr(review_runner, "_dispatch_child", fake_dispatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    r = await review_runner.run_once(wid, rid)
    assert dispatched == ["revise"] and r["reason"] == "dispatch_revise", r


async def test_promote_version_gate_stale_approver(sandbox, monkeypatch):
    """[版本闸 · 已签收窄口径] 全员批过 v1、收口时草稿已是 v2 ⇒ promote 拒收。

    挂 drift_pending + 发 review_draft_drift；下一步叫 stale 席（批旧版者）
    跑「只评增量」确认轮——pending 席不打扰、不重投全文。
    """
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    await _seated(wid, rid, *fids)
    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)
    # 全过的是 v1；之后草稿又改成 v2（迭代中不打扰任何人）。
    await card_store.update_card(
        wid, cid, {"requirements_draft": "R2 全过之后改的"}, "漂移")
    st = await review_store.status(wid, rid)
    assert st["draft"] == 2 and set(st["stale"]) == set(fids), st

    bad = await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)
    assert bad.get("ok") is not True
    assert "stale" in str(bad.get("error") or "") or "旧版" in str(bad.get("error") or "")
    st2 = await review_store.state_of(wid, rid)
    assert st2.get("drift_pending"), "撞闸后应挂 drift_pending"

    # 下一步：叫 stale 席补「只评增量」确认轮。
    seen: list[tuple[str, str]] = []

    async def fake_seat(work_id, review_id, seat, st, *, kind, hint=""):
        seen.append((seat, kind))
        await review_store.mark_spoken(work_id, review_id, seat)
        await review_store.mark_seen(work_id, review_id, seat)
        await review_store.approve(work_id, review_id, seat)   # 确认：增量不动摇
        return {"seat": seat, "kind": kind, "voted": True, "vote": "approve"}

    monkeypatch.setattr(review_runner, "_seat_turn", fake_seat)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    await review_runner.run_once(wid, rid)
    assert seen[0] == (fids[0], "confirm"), seen


async def test_step_dispatches_promote_when_all_passed(sandbox, monkeypatch):
    """全过（无漂移、未请过）⇒ 这一步＝派活请正卡调 promote_draft。"""
    from src.api import review_runner

    wid, cid, fids = await _make_work_with_functions(2)
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    rid = out["data"]["review_card_id"]

    await _seated(wid, rid, *fids)
    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)

    # 只 mock 唤醒层：真实 `_dispatch_child`（落消息＋promote_asked 机械事实）
    # 要原样跑完——标记住在它内部，整个换假等于把被测逻辑跳了。
    from src.api import message_runner as _mr

    delivered: list[str] = []

    async def fake_deliver(work_id, card_id, **_kw):
        delivered.append(card_id)
        return True

    monkeypatch.setattr(_mr, "deliver_now", fake_deliver)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    r = await review_runner.run_once(wid, rid)
    assert delivered == [cid] and r["reason"] == "dispatch_promote", r
    st2 = await review_store.state_of(wid, rid)
    assert st2.get("promote_asked"), "已请过定稿要落成机械事实"
