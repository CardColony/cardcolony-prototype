# -*- coding: utf-8 -*-
"""需求文档两轮评审（[两轮评审 2026-11-14 · 用户裁定]）。

钉的口径：
- open_review 先开「对齐批」：总裁（根卡）一席判偏离，第二轮名单存批态
- 对齐批通过 ⇒ 同卡自动开三席批（advance）；否决 ⇒ 终局——通知落正卡
  会话＋requirements_state=vetoed，不走修订循环
- 定稿后只能块内追加：append_requirement 只补已有 ### 块、只限自己、
  禁 ### 入文、无块老文档落文末
- update_card 写闸：approved 后 requirements 写入必须带来源标记——
  promote/append/user/final 四条通道，curate_face 不带标记自然被拒
"""
from __future__ import annotations

from src.api import card_tools, review_runner
from src.storage import (card_store, function_store, review_store,
                         session_facade, work_store)


async def _mk_work(n_fns: int = 3):
    wdata = await work_store.create_work("两轮评审测试")
    wid = str(wdata["work_id"])
    root = str(work_store.work_card_id_of(wid) or "")
    codes = ("ceo", "pm", "cto")
    fids = []
    for i in range(n_fns):
        code = codes[i] if i < len(codes) else f"f{i}"
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": code}})
        fids.append(str(prof.get("function_id") or ""))
    return wid, root, fids


async def _mk_child(wid, draft="R1 草稿：接口以 v2 为准"):
    r = await card_store.create_card(wid, "子项目甲", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"requirements_draft": draft,
                   "requirements_state": "draft"}, "测试前置：写草稿")
    return cid


def _cards(wid):
    return card_store.iter_card_metas(wid)


async def _open(wid, cid, **extra):
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "focus": "评审", "brief": "背景",
                   **extra}, _cards(wid))
    assert out.get("ok"), out
    return str(out["data"]["review_card_id"]), out["data"]


async def _approve(wid, rid, seat):
    return await card_tools._h_approve(
        wid, seat, {"review_card_id": rid}, _cards(wid))


async def _reject(wid, rid, seat):
    return await card_tools._h_reject(
        wid, seat, {"review_card_id": rid}, _cards(wid))


async def _session_texts(wid, cid) -> str:
    blocks = await session_facade.read(wid, cid) or []
    if isinstance(blocks, dict):
        blocks = blocks.get("blocks") or []
    return "\n".join(str(b.get("content") or "") for b in blocks)


# ================================================================ 两轮结构

async def test_open_review_starts_align_batch(sandbox):
    """requirements 型评审先开对齐批：成员＝根卡一席，三席名单存批态。"""
    wid, root, fids = await _mk_work()
    cid = await _mk_child(wid)
    rid, data = await _open(wid, cid)
    st = await review_store.state_of(wid, rid)
    assert st["type"] == "requirements_align"
    assert st["members"] == [root]                    # 总裁一席
    assert st["next_members"] == fids                 # 三席名单留给第二轮
    assert data["members"] == [root]
    assert data["round"] == "align"


async def test_align_approve_advances_to_full_batch(sandbox):
    """总裁通过 ⇒ classify 出 advance ⇒ 关对齐批、自动开 requirements 批。"""
    wid, root, fids = await _mk_work()
    cid = await _mk_child(wid)
    rid, _ = await _open(wid, cid)
    st0 = await review_store.state_of(wid, rid)
    align_batch = st0["batch"]

    r = await _approve(wid, rid, root)
    assert r.get("ok"), r
    st_now = await review_store.status(wid, rid)
    assert st_now["all_passed"]
    # classify：对齐批全过 ⇒ advance（不是 promote 派活）
    nxt, _ = review_runner._classify(
        st0, st_now, set(), promoted=False, tail_seq=None, has_draft=True)
    assert nxt == ("advance",)

    await review_runner._run_step(wid, rid)
    st2 = await review_store.state_of(wid, rid)
    assert st2["batch"] and st2["batch"] != align_batch
    assert st2["type"] == "requirements"
    assert st2["members"] == fids                      # 第二轮原班三席
    assert st2["closed_batch"] == align_batch          # 对齐批已关


async def test_align_veto_terminates_and_reports_back(sandbox):
    """总裁否决 ⇒ 终局：关批、通知落子卡会话、state=vetoed，不开新批。"""
    wid, root, fids = await _mk_work()
    cid = await _mk_child(wid)
    rid, _ = await _open(wid, cid)

    r = await _reject(wid, rid, root)
    assert r.get("ok"), r
    st0 = await review_store.state_of(wid, rid)
    st_now = await review_store.status(wid, rid)
    nxt, _ = review_runner._classify(
        st0, st_now, set(), promoted=False, tail_seq=None, has_draft=True)
    assert nxt == ("veto",)

    await review_runner._run_step(wid, rid)
    st2 = await review_store.state_of(wid, rid)
    assert not st2.get("batch")                        # 批次已关
    assert not st2.get("members")
    meta = (await card_store.read_card(wid, cid)).get("meta") or {}
    assert str(meta.get("requirements_state") or "") == "vetoed"
    assert str(meta.get("requirements_draft") or "").strip()  # 草稿不清
    texts = await _session_texts(wid, cid)
    assert "对齐轮否决" in texts and "report_up" in texts \
        and "open_review" in texts
    # [用户口径「评审问题和结果发回给总裁裁决」] 总裁本体会话拿到
    # 终局裁决件：意见原样＋裁决手段提示。
    ceo_texts = await _session_texts(wid, root)
    assert "对齐轮终局" in ceo_texts and cid in ceo_texts \
        and "confirm_with_user" in ceo_texts
    # 终局后 classify 不再出动作（批次已关）
    st3 = await review_store.status(wid, rid)
    nxt2, _ = review_runner._classify(
        st2, st3, set(), promoted=False, tail_seq=None, has_draft=True)
    assert nxt2 is None


async def test_promote_denied_during_align_batch(sandbox):
    """对齐批的 all_passed 只晋级——promote_draft 在对齐批期间被拒。"""
    wid, root, fids = await _mk_work()
    cid = await _mk_child(wid)
    rid, _ = await _open(wid, cid)
    await _approve(wid, rid, root)
    out = await card_tools._h_promote_draft(wid, cid, {}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "validation", out


# ================================================================ 追加通道

async def _mk_approved(wid, req="### 接口\n- v2 为准\n### 数据\n- 只读"):
    cid = await _mk_child(wid)
    await card_store.update_card(
        wid, cid, {"requirements": req, "requirements_draft": "",
                   "requirements_state": "approved"}, "测试前置：已定稿")
    return cid


async def test_append_into_named_block(sandbox):
    wid, root, _ = await _mk_work()
    cid = await _mk_approved(wid)
    out = await card_tools._h_append_requirement(
        wid, cid, {"block": "接口", "text": "- 超时 3s"}, _cards(wid))
    assert out.get("ok"), out
    req = str((await card_store.read_card(wid, cid))
              .get("body", {}).get("requirements") or "")
    assert req == "### 接口\n- v2 为准\n- 超时 3s\n### 数据\n- 只读", req


async def test_append_requires_existing_block(sandbox):
    wid, root, _ = await _mk_work()
    cid = await _mk_approved(wid)
    out = await card_tools._h_append_requirement(
        wid, cid, {"block": "不存在的块", "text": "x"}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "validation"
    assert "接口" in str(out.get("error", {}).get("message") or "")
    # 分块文档不给 block 也要拒
    out2 = await card_tools._h_append_requirement(
        wid, cid, {"text": "x"}, _cards(wid))
    assert (out2.get("error") or {}).get("code") == "validation"


async def test_append_forbids_new_block_headings(sandbox):
    wid, root, _ = await _mk_work()
    cid = await _mk_approved(wid)
    out = await card_tools._h_append_requirement(
        wid, cid, {"block": "接口", "text": "### 新块\n偷建"}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "validation"


async def test_append_blockless_doc_lands_at_end(sandbox):
    """无 ### 的老文档＝整篇一个隐式块，追加落文末。"""
    wid, root, _ = await _mk_work()
    cid = await _mk_approved(wid, req="接口以 v2 为准\n数据只读")
    out = await card_tools._h_append_requirement(
        wid, cid, {"text": "超时 3s"}, _cards(wid))
    assert out.get("ok"), out
    req = str((await card_store.read_card(wid, cid))
              .get("body", {}).get("requirements") or "")
    assert req.endswith("超时 3s\n"), req
    assert "接口以 v2 为准" in req


async def test_append_self_only_and_approved_only(sandbox):
    wid, root, _ = await _mk_work()
    cid = await _mk_approved(wid)
    other = await _mk_child(wid, draft="x")
    # 替别人追加 → 拒
    out = await card_tools._h_append_requirement(
        wid, cid, {"card_id": other, "text": "x"}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "denied"
    # 未定稿 → 拒
    out2 = await card_tools._h_append_requirement(
        wid, other, {"text": "x"}, _cards(wid))
    assert (out2.get("error") or {}).get("code") == "denied"


async def test_append_denied_on_root(sandbox):
    wid, root, _ = await _mk_work()
    await card_store.update_card(
        wid, root, {"requirements": "根文档", "requirements_state": "approved"},
        "前置")
    out = await card_tools._h_append_requirement(
        wid, root, {"text": "x"}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "denied"


# ================================================================ 写闸

async def test_update_card_gate_on_approved_requirements(sandbox):
    """approved 后 requirements 写入必须带来源标记，否则拒。"""
    wid, root, _ = await _mk_work()
    cid = await _mk_approved(wid)
    # 无标记 → 拒
    r = await card_store.update_card(
        wid, cid, {"requirements": "绕过通道的修改"}, "无来源直写")
    assert isinstance(r, dict) and r.get("error"), r
    # 四条合法通道 → 放行
    for via in ("promote", "append", "user", "final"):
        r2 = await card_store.update_card(
            wid, cid, {"requirements": f"经 {via} 的修改", "_req_via": via},
            "带来源写")
        assert not (isinstance(r2, dict) and r2.get("error")), (via, r2)
    # 非 requirements 字段不受闸
    r3 = await card_store.update_card(
        wid, cid, {"title": "改名"}, "普通写")
    assert not (isinstance(r3, dict) and r3.get("error")), r3


async def test_curate_face_blocked_post_approval(sandbox):
    """总裁执笔对已 approved 的子卡文档被写闸拒（裁决靠 direct，不靠改稿）。"""
    wid, root, _ = await _mk_work()
    # 根卡挂 ceo 档案 ⇒ _is_ceo_curator 成立，走到 update_card 才被闸
    await function_store.ensure_profile(
        wid, root, {"function_name": "总裁",
                    "jurisdiction": {"catalog_code": "ceo"}})
    cid = await _mk_approved(wid)
    out = await card_tools._h_curate_face(
        wid, root, {"card_id": cid, "old_string": "- v2 为准",
                    "new_string": "- v3 为准"}, _cards(wid))
    assert out.get("error"), "总裁直改已定稿文档必须被拒"
    # 定稿文档没被改动
    req = str((await card_store.read_card(wid, cid))
              .get("body", {}).get("requirements") or "")
    assert "- v2 为准" in req


async def test_modification_goes_back_through_review(sandbox):
    """修改已定稿文档＝起草修改版→重新 open_review→又是两轮（对齐批先行）。"""
    wid, root, fids = await _mk_work()
    cid = await _mk_approved(wid)
    # 起草修改版（edit_requirements 以正稿为底写草稿列）
    out = await card_tools._h_edit_requirements(
        wid, cid, {"old_string": "- v2 为准", "new_string": "- v3 为准"},
        _cards(wid))
    assert out.get("ok"), out
    rid, data = await _open(wid, cid)
    st = await review_store.state_of(wid, rid)
    assert st["type"] == "requirements_align"   # 修改也要重过对齐闸
