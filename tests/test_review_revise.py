# -*- coding: utf-8 -*-
"""评审循环里「子卡在评审卡现场改草稿」这一环（[2026-10-29 用户口径]）。

用户原话：
> 「评审链路是一个一边评审一边修改草稿的链路，所以，一次评审全员发言，
>   子卡直接在评审卡内，对草稿进行修改，然后再全员评审，直到所有成员通过为止」

钉四件事：
1. 子卡能用 `edit_requirements` 改**自己的草稿**，改完 `changed=True`；
2. 正稿（`requirements`）一个字都不动——正稿只有评审通过才产生；
3. 子卡**不能 approve**（它是被评方，不是评审成员）；
4. 它拿到的上下文里有**未通过成员的原话**（不是全批次流水账）。
"""
from __future__ import annotations

from src.api import card_tools, model_service, review_runner
from src.storage import (card_store, function_store, review_store, session_facade,
                         work_store)


async def _setup(n: int = 2):
    wdata = await work_store.create_work("评审改稿测试")
    wid = str(wdata["work_id"])
    fids = []
    # [2026-10-31 对齐] 评审邀请集已收紧成**总裁／产品经理／技术总监**三个职能目录
    # code（card_tools._h_open_review，2026-10-30 23:07 改）⇒ 夹具必须按这三个 code
    # 造职能，否则开批次直接被判「没有在职的总裁、产品经理或技术总监可参与评审」，
    # 本文件 10 条用例全在 _setup 里炸（TypeError: 'NoneType'）。
    # 与 tests/test_review_tools.py::_make_work_with_functions 同一口径。
    review_codes = ("ceo", "pm", "cto")
    for i in range(n):
        code = review_codes[i] if i < len(review_codes) else f"f{i}"
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": code}})
        fids.append(str(prof.get("function_id") or ""))
    r = await card_store.create_card(wid, "子项目甲", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"requirements": "R0 正稿（不该被改）",
                   "requirements_draft": "R1 草稿（测试用）",
                   "requirements_state": "draft"}, "测试前置")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "focus": "R1 收尾",
                   "brief": "背景X\n现状Y\n评审Z"}, cards)
    return wid, cid, fids, str(out["data"]["review_card_id"])


async def test_revise_edits_own_draft_and_leaves_final_untouched(sandbox, monkeypatch):
    """子卡按意见改草稿 ⇒ changed=True；正稿不动。"""
    wid, cid, fids, rid = await _setup()

    calls = {"n": 0}

    async def fake_call(sys_p, user_p, convo, tools, model, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "", "tool_calls": [{
                "name": "edit_requirements",
                "arguments": {"card_id": cid,
                              "old_string": "R1 草稿（测试用）",
                              "new_string": "R1 改过（按意见补了证据）"}}]},
                "fake-model")
        return ({"content": "已改：补了证据", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    out = await review_runner.revise(wid, rid)

    assert out["changed"] is True, out
    card = await card_store.read_card(wid, cid)
    meta, body = card.get("meta") or {}, card.get("body") or {}
    assert "R1 改过" in str(meta.get("requirements_draft") or "")
    assert str(body.get("requirements") or "") == "R0 正稿（不该被改）", \
        "子卡只能改草稿，正稿由评审通过产生"

    # 改稿要推进草稿版本（下一轮据此记录"第几版"；票不因它失效）
    st = await review_store.status(wid, rid)
    assert st["draft"] == 2, st

    # 发言落在**评审卡**会话里（现场共享，后面发言的人看得到）
    blocks = await session_facade.read(wid, review_runner._sid(rid, str((await review_store.state_of(wid, rid)).get('batch') or ''))) or []
    assert any("已改" in str(b.get("content") or "") for b in blocks), blocks


async def test_revise_cannot_approve(sandbox, monkeypatch):
    """子卡不是评审成员：approve 必须被拒，票不能变。"""
    wid, cid, fids, rid = await _setup()

    async def fake_call(sys_p, user_p, convo, tools, model, **_):
        return ({"content": "", "tool_calls": [{
            "name": "approve",
            "arguments": {"review_card_id": rid}}]}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    out = await review_runner.revise(wid, rid)

    assert out["changed"] is False
    assert any(t.get("tool") == "approve" and not t.get("ok")
               for t in (out.get("tools") or [])), out
    st = await review_store.status(wid, rid)
    assert cid not in st["passed"], "被评卡不能给自己投票"


async def test_revise_prompt_carries_pending_opinions_only(sandbox, monkeypatch):
    """上下文里要有**未通过成员的原话**，而不是全批次流水账。"""
    wid, cid, fids, rid = await _setup()

    async def say(sys_p, user_p, convo, tools, model):
        # [2026-10-29] 发言必须有终态（approve / reject），否则会被再调起
        return ({"content": "第三条不满足：没有证据",
                 "tool_calls": [{"name": "reject", "arguments": {}}]}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", say)
    await review_runner.speak(wid, rid, fids[0])

    captured: dict[str, str] = {}

    async def capture(sys_p, user_p, convo, tools, model):
        # [2026-10-31 对齐] 只记**第一圈**的工具面：改稿轮从第 2 圈起收敛成只剩终态
        # 工具 submit_revision（与 speak 第 2 圈收敛成 approve/reject 同一手法，
        # 2026-10-30 设计）⇒ 「子卡能不能改草稿」这条断言必须看第一圈的面，
        # 看最后一圈会误判成"它只有 submit_revision、拿不到 edit_requirements"。
        if "tools" not in captured:
            captured["user"] = user_p
            captured["sys"] = sys_p
            captured["tools"] = [
                (t.get("function") or {}).get("name") or t.get("name") or ""
                for t in (tools or [])]
        return ({"content": "（不改）", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", capture)
    await review_runner.revise(wid, rid)

    assert "第三条不满足" in captured["user"], "未通过成员的原话必须进上下文"
    assert "R1 草稿（测试用）" in captured["user"], "当前草稿必须在上下文里"
    # [2026-10-29 用户令] 子卡拿不到 approve 是**工具面**的事，不靠提示词说一句
    # 「你不能 approve」——那是教它做事。这里钉工具面本身。
    assert "approve" not in captured["tools"], captured["tools"]
    assert "edit_requirements" in captured["tools"]


async def test_revise_no_change_reports_false(sandbox, monkeypatch):
    """模型说了话但没动草稿 ⇒ changed=False（调用方据此停止循环）。"""
    wid, cid, fids, rid = await _setup()

    async def fake_call(sys_p, user_p, convo, tools, model, **_):
        return ({"content": "我认为不用改，理由如下……", "tool_calls": []},
                "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    out = await review_runner.revise(wid, rid)
    assert out["changed"] is False


async def test_revise_skips_when_child_turn_is_running(sandbox, monkeypatch):
    """子卡自己的回合在跑（open_review 就在它那一轮里）⇒ 不抢它的互斥体。

    抢不到就本轮跳过：等它这一轮结束，改了草稿会自己触发下一轮，
    没改就等同"子卡不改"，评审停在原地。绝不并发跑两个模型去改同一张卡。
    """
    wid, cid, fids, rid = await _setup()

    called = {"n": 0}

    async def fake_call(*a, **k):
        called["n"] += 1
        return ({"content": "不该被调到", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    session_facade.turn_try_acquire(wid, cid, "user-turn")
    try:
        out = await review_runner.revise(wid, rid)
    finally:
        session_facade.turn_release(wid, cid, "user-turn")

    assert out.get("busy") is True and out["changed"] is False
    assert called["n"] == 0, "抢不到互斥体就不该调模型"


async def test_full_loop_revise_then_pass_promotes_draft(sandbox, monkeypatch):
    """端到端：开批次 → 一轮全员不过 → 子卡改稿 → 二轮全员通过 → **草稿转正稿**。

    这条是整条链路的收口验证：用户要的「一边评审一边改草稿，直到所有人通过」
    最终必须落到「正稿产生」——不是停在"全员通过"这个状态上。
    """
    wid, cid, fids, rid = await _setup()

    spoken: list[str] = []

    async def fake_speak(work_id, review_id, role, st=None):
        spoken.append(role)
        if len(spoken) > len(fids):      # 第二轮起全部通过
            cards = card_store.iter_card_metas(work_id)
            await card_tools._h_approve(
                work_id, role, {"review_card_id": review_id}, cards)
        return {"role": role}

    async def fake_revise(work_id, review_id, st=None):
        await card_store.update_card(
            wid, cid, {"requirements_draft": "R2 最终稿（按意见改完）"},
            "子卡改草稿")
        return {"role": cid, "changed": True}

    monkeypatch.setattr(review_runner, "speak", fake_speak)
    monkeypatch.setattr(review_runner, "revise", fake_revise)
    await review_runner._round_loop(wid, rid)

    # [2026-10-29 用户口径「最后也一定是调工具写正稿为结束」]
    # 全员通过不再自动转稿；收口要子卡自己调 promote_draft。
    mid = await card_store.read_card(wid, cid)
    assert str((mid.get("meta") or {}).get("requirements_state") or "") != "approved"
    await card_tools._h_promote_draft(
        wid, cid, {"card_id": cid}, card_store.iter_card_metas(wid))

    card = await card_store.read_card(wid, cid)
    body, meta = card.get("body") or {}, card.get("meta") or {}
    assert str(body.get("requirements") or "") == "R2 最终稿（按意见改完）", \
        "调 promote_draft 之后 ⇒ 草稿必须转成正稿"
    assert str(meta.get("requirements_draft") or "") == "", "转正后草稿列清空"
    assert str(meta.get("requirements_state") or "") == "approved"
    st = await review_store.status(wid, rid)
    assert st["all_passed"] is True and st["batch"] == "", "批次应已收口"


async def test_member_sees_the_draft_not_the_final(sandbox, monkeypatch):
    """评审成员看的必须是**草稿**，不是正稿（[2026-10-29 用户抓到]）。

    `_user_prompt` 原来取 `body["requirements"]`（正稿）⇒ 整条评审一直在评
    一份已经定稿的东西，"改草稿"链路从未被真正评过。
    """
    wid, cid, fids, rid = await _setup()

    captured: dict[str, str] = {}

    async def cap(sys_p, user_p, convo, tools, model):
        captured["user"] = user_p
        return ({"content": "看完了",
                 "tool_calls": [{"name": "reject", "arguments": {}}]}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", cap)
    await review_runner.speak(wid, rid, fids[0])

    assert "R1 草稿（测试用）" in captured["user"], "评审对象必须是草稿"
    assert "R0 正稿" not in captured["user"], "正稿不能混进评审对象"


async def test_system_prompt_reads_review_charter_not_daily_one(sandbox):
    """评审席上读的是**评审职责书**，不是日常职责（[2026-10-29 用户令]）。

    「它的日常职责和它的评审职责，是两个职责，评审职责是专门写的」
    ⇒ `config/review_charters.json` 里有这个角色 ⇒ 用那一份；
       没有 ⇒ 回落日常职责，并且标题上标明"还没写评审职责"（能看出缺哪几份）。
    """
    wid, cid, fids, rid = await _setup()

    # [2026-10-30 同构改造] 身份不再单独手写，而是走「你的职能」区：
    # `function_meta` 里塞的就是评审职责书 ⇒ 断言职责书**正文**出现即可。
    sys_p = await review_runner._system_prompt(wid, "card_001", rid)
    assert "核心价值和利益" in sys_p, "总裁有评审职责书，必须用它（正文）"
    assert "## 你的职能" in sys_p, "身份走「你的职能」区（与子卡同构）"

    # 没写评审职责的角色 ⇒ 回落日常职责（内容不同，但仍是同一区）
    sys_p2 = await review_runner._system_prompt(wid, fids[0], rid)
    assert "## 你的职能" in sys_p2


async def test_system_prompt_reads_review_charter_not_daily_one(sandbox):
    """评审席上读的是**评审职责书**，不是日常职责（[2026-10-29 用户令]）。

    「它的日常职责和它的评审职责，是两个职责，评审职责是专门写的」
    ⇒ `config/review_charters.json` 里有这个角色 ⇒ 用那一份；
       没有 ⇒ 回落日常职责，并且标题上标明"还没写评审职责"（能看出缺哪几份）。
    """
    wid, cid, fids, rid = await _setup()

    # [2026-10-30 同构改造] 身份不再单独手写，而是走「你的职能」区：
    # `function_meta` 里塞的就是评审职责书 ⇒ 断言职责书**正文**出现即可。
    sys_p = await review_runner._system_prompt(wid, "card_001", rid)
    assert "核心价值和利益" in sys_p, "总裁有评审职责书，必须用它（正文）"
    assert "## 你的职能" in sys_p, "身份走「你的职能」区（与子卡同构）"

    # 没写评审职责的角色 ⇒ 回落日常职责（内容不同，但仍是同一区）
    sys_p2 = await review_runner._system_prompt(wid, fids[0], rid)
    assert "## 你的职能" in sys_p2


def test_tool_results_are_not_truncated():
    """工具结果不许硬截断。

    [2026-10-29 实弹] `read_card card_075` 的返回 JSON 总长 20375 字符，
    其中 `requirements` 字段从第 **12217** 字符处才开始；review_runner 当时写的是
    `json.dumps(r)[:12000]` ⇒ 需求块**整块被砍掉**。技术总监据此在发言里写
    「card_075 需求块正文我读不到，下面按「未核实」处理」，评审结论因此偏了。

    口径同 `_user_prompt` 的 [P6]：**事实源一律不截断**。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "api" / "review_runner.py").read_text(encoding="utf-8")
    assert "[:12000]" not in src, "工具结果不许砍到 12000"
    assert "[:4000]" not in src, "改稿回合的工具结果同样不许砍"
    assert src.count("payload = json.dumps(r, ensure_ascii=False)") >= 3


def test_tool_results_are_not_truncated():
    """工具结果不许硬截断。

    [2026-10-29 实弹] `read_card card_075` 的返回 JSON 总长 20375 字符，
    其中 `requirements` 字段从第 **12217** 字符处才开始；review_runner 当时写的是
    `json.dumps(r)[:12000]` ⇒ 需求块**整块被砍掉**。技术总监据此在发言里写
    「card_075 需求块正文我读不到，下面按「未核实」处理」，评审结论因此偏了。

    口径同 `_user_prompt` 的 [P6]：**事实源一律不截断**。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "api" / "review_runner.py").read_text(encoding="utf-8")
    assert "[:12000]" not in src, "工具结果不许砍到 12000"
    assert "[:4000]" not in src, "改稿回合的工具结果同样不许砍"
    assert src.count("payload = json.dumps(r, ensure_ascii=False)") >= 3


async def test_speak_requires_a_terminal_tool(sandbox, monkeypatch):
    """发言必须以 approve / reject 收尾。

    [2026-10-29 用户令]「你让它每一次必须调工具，要么不通过要么通过，否则就再调起它」
    实弹里产品经理在文字里写「结论：通过」但没调 approve ⇒ 系统只认工具 ⇒ 它永远
    pending ⇒ 子卡被反复叫去改稿，跑到第 23 版还没收口。

    钉两件事：① 只说话不表态 ⇒ 再调起一次；② 再调起时**工具面收敛成二选一**
    （只剩 approve / reject，不给查阅工具）——这是结构，不是次数上限。
    """
    wid, cid, fids, rid = await _setup()
    calls: list[list[str]] = []

    async def model(sys_p, user_p, convo, tools, model_name):
        names = [str((t.get("function") or t).get("name") or "") for t in (tools or [])]
        calls.append(names)
        if len(calls) == 1:
            # 第一次：只在文字里说通过，不调工具
            return ({"content": "结论：通过。", "tool_calls": []}, "fake")
        return ({"content": "通过",
                 "tool_calls": [{"name": "approve",
                                 "arguments": {"review_card_id": rid}}]}, "fake")

    monkeypatch.setattr(model_service, "call_main", model)
    await review_runner.speak(wid, rid, fids[0])

    # [2026-10-31 用户令] 不做内部重试、不收敛工具面：
    # 「模型不可能无限查下去」。不调工具 ⇒ 这一步结束，交 harness。
    assert len(calls) == 1, "只说话不表态 ⇒ 结束，由 harness 决定下一步"
    assert "read_card" in calls[0], "给完整工具面（可查阅）"



def test_text_tool_tag_detects_misformatted_calls():
    """工具调用写成标签 ⇒ 要能被认出来（[2026-10-29 用户令]）。

    「工具调错，你应该继续调起模型，让它继续尝试，怎么会卡住？」
    实弹：card_038 改稿时输出 `<read_file">\\n<path>..</path>`，既不是结构化调用、
    也不是"不用改"的表态，旧流程直接 break ⇒ 批次停在 pending。
    """
    from src.api.review_runner import _text_tool_tag

    tools = [{"function": {"name": "read_file"}},
             {"function": {"name": "edit_requirements"}}]
    assert _text_tool_tag('<read_file">\n<path>x</path>', tools) == "read_file"
    assert _text_tool_tag("<edit_requirements>", tools) == "edit_requirements"
    assert _text_tool_tag("我认为不用改，判据都过了", tools) == ""
    assert _text_tool_tag("<div>hi</div>", tools) == ""      # 不是工具名的标签不算


def test_text_tool_tag_detects_misformatted_calls():
    """工具调用写成标签 ⇒ 要能被认出来（[2026-10-29 用户令]）。

    「工具调错，你应该继续调起模型，让它继续尝试，怎么会卡住？」
    实弹：card_038 改稿时输出 `<read_file">\n<path>..</path>`，既不是结构化调用、
    也不是"不用改"的表态，旧流程直接 break ⇒ 批次停在 pending。
    """
    from src.api.review_runner import _text_tool_tag

    tools = [{"function": {"name": "read_file"}},
             {"function": {"name": "edit_requirements"}}]
    assert _text_tool_tag('<read_file">\n<path>x</path>', tools) == "read_file"
    assert _text_tool_tag("<edit_requirements>", tools) == "edit_requirements"
    assert _text_tool_tag("我认为不用改，判据都过了", tools) == ""
    assert _text_tool_tag("<div>hi</div>", tools) == ""      # 不是工具名的标签不算


async def test_promote_turn_tells_child_to_write_final(sandbox, monkeypatch):
    """全员通过后叫子卡，话必须说「写正稿」，不能还是「改草稿」。

    [2026-10-29] 不给这句话的话，它看到"按意见改草稿"就会去改草稿，
    永远不写正稿，批次转不出去。
    """
    wid, cid, fids, rid = await _setup()
    captured: dict = {}

    async def fake_call(sys_p, user_p, convo, tools, model, **_):
        captured["user"] = user_p
        captured["tools"] = [str((t.get("function") or t).get("name") or "") for t in tools]
        return ({"content": "好", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    await review_runner.revise(wid, rid, ask_promote=True)

    assert "promote_draft" in captured["user"], "话里要指明调哪个工具"
    assert "已全员通过" in captured["user"]
    assert "promote_draft" in captured["tools"], "工具面要给 promote_draft"


async def test_promote_turn_tells_child_to_write_final(sandbox, monkeypatch):
    """全员通过后叫子卡，话必须说「写正稿」，不能还是「改草稿」。

    [2026-10-29] 不给这句话的话，它看到"按意见改草稿"就会去改草稿，
    永远不写正稿，批次转不出去。
    """
    wid, cid, fids, rid = await _setup()
    captured: dict = {}

    async def fake_call(sys_p, user_p, convo, tools, model, **_):
        captured["user"] = user_p
        captured["tools"] = [str((t.get("function") or t).get("name") or "") for t in tools]
        return ({"content": "好", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    await review_runner.revise(wid, rid, ask_promote=True)

    assert "promote_draft" in captured["user"], "话里要指明调哪个工具"
    assert "已全员通过" in captured["user"]
    assert "promote_draft" in captured["tools"], "工具面要给 promote_draft"
