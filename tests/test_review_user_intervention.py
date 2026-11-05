# -*- coding: utf-8 -*-
"""评审·甲方介入 与 评审席身份 回归网（2026-10-31）。

钉的是用户 2026-10-31 当面提出的三条实测缺陷，每条都有取证出处：

① **用户完全不能介入评审**（work_027/card_124，2026-10-30 23:09 实弹）
   用户在评审卡里发言 ⇒ 落普通 human 块，首行没有 `· rv_xxx〕` 批次标记 ⇒
   `_prior_messages` 按标记过滤时丢掉 ⇒ 后续评审轮看不见；不进 review_state ⇒
   只是文本，模型口头「我撤回」在系统层面零效果；不触发续轮 ⇒ 那次发言之后
   事件流里零个 review_* 事件，批次停在 draft=1 / pending 全 5 人。

② **身份错位**（同一份日志的 system prompt，134,942 字）
   装配锚点是子卡 ⇒ 145 区说「你是 总裁（card_001）」，92 区却说
   「你是 `card_074`，卡类型 `sub_project`」；150 当前卡／170 直接子卡／200 记忆
   全是子卡的；**总裁自己的正稿（6,961 字）一个字都不在 system 里**。

③ **凭空造依据**（同一轮 response 全文逐串核验）
   「六条功能线已经跟你对齐过了（对话／画布／权限面板／通知待办／设置／桌面框架）」
   「用户当场裁定的分工是「桌面框架、整体页面组装…这些是你的工作」」
   ⇒ 对 system+prior+user 全文（365,151 字）与 card_001 正稿：**两条都是 0 命中**。

④ **停不下来 / 收不了口**
   `review_runner` 全文对 control 零引用，评审轮的 task 也不登记进 `_turn_tasks`
   ⇒ 停止端点 cancel 不到、停止位没人消费；`speak` 把终态写成正文标签时只会
   「再问一遍」而循环无出口（实测一条日志里 4 次重试、2 次标签态 reject）。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api import (card_tools, context_regions, control, model_service,
                     review_runner, routes)
from src.storage import (card_store, function_store, review_store, session_facade,
                         work_store)


async def _setup(n: int = 3):
    """三个评审席（ceo/pm/cto）＋一张有草稿的子项目卡＋一个开好的批次。"""
    wdata = await work_store.create_work("甲方介入测试")
    wid = str(wdata["work_id"])
    codes = ("ceo", "pm", "cto")
    fids = []
    for i in range(n):
        code = codes[i] if i < len(codes) else f"f{i}"
        prof = await function_store.create_function(
            wid, f"评审席{i}", {"jurisdiction": {"catalog_code": code}})
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
    assert out.get("ok"), out
    return wid, cid, fids, str(out["data"]["review_card_id"])


def _mute_background_round(monkeypatch):
    """掐掉 `_h_open_review` 后台 spawn 的第一步（本文件里锁敏感的用例都要先调它）。

    它是 `_spawn_tracked` 起的 task，**要等下一次 await 才真正执行** ⇒ 那时用例往往
    已经 `delenv(ECO_NO_AUTO_REVIEW)`，于是它跑起来、先占住评审卡的回合锁，把本测的
    `run_once` 挤成 `already_running`。
    """
    def _no_round(*_a, **_kw):
        return None
    monkeypatch.setattr(review_runner, "spawn_round", _no_round)


# ============================================================
# ① 甲方裁定：入状态、进上下文、能召回、能续轮
# ============================================================






async def test_seat_gets_the_childs_context_not_its_own(sandbox, monkeypatch):
    """上下文的**上下文锚＝子卡**（ctx_card）；席位自己的卡不是上下文锚。

    [venue] 三锚分离：执行锚＝评审卡（会话/锁/事件流）、身份锚＝席位（act_as）、
    上下文锚＝被评子卡（ctx_card——[2026-10-30 用户裁定]「取子卡的上下文，
    直接接入」）。这里钉的是：传给普通回合的 ctx_card 是被评子卡——
    子卡卡面**由它的 bundle 供给**，mandate 只留差事＋草稿（[2026-11 去重令]，
    不再复抄卡面）；**席位自己的卡面不出现**。
    """
    wid, cid, fids, rid = await _setup()
    root = work_store.work_card_id_of(wid)
    await card_store.update_card(wid, cid, {"status_block": "子卡现状标记CHILDMARK"}, "测试")
    await card_store.update_card(wid, root, {"status_block": "根卡现状标记SEATMARK"}, "测试")

    captured: list[dict] = []

    async def fake_run(work_id, card_id, content, **kw):
        captured.append({"content": content, **kw})
        return {"reply": "ok"}

    monkeypatch.setattr(routes, "_run_message", fake_run)
    st = await review_store.state_of(wid, rid)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")

    assert captured, "席位回合要走普通回合"
    got = captured[-1]
    assert got.get("act_as") == fids[0], "身份锚＝席位"
    assert got.get("ctx_card") == cid, "上下文锚＝被评子卡"
    assert "CHILDMARK" not in got["content"], \
        "子卡现状由 ctx_card 的 bundle 供给，mandate 不再复抄（去重令）"
    assert "SEATMARK" not in got["content"], \
        "评审席自己那张卡的信息不许进——用户裁定：那些全都只是污染"






async def test_stop_cancels_the_inflight_review_round(sandbox, monkeypatch):
    """端到端：评审轮在跑时用户点停止 ⇒ 协程被 cancel、回合锁被释放。

    改前的实况（work_027/card_124 实测）：评审轮既不占回合锁、也不登记 turn task、
    且全文对 control 零引用 ⇒ 停止端点三条路全断，用户只能等它自己跑完。
    """
    _mute_background_round(monkeypatch)
    wid, cid, fids, rid = await _setup()
    gate = asyncio.Event()

    async def parked(**kw):
        await gate.wait()
        return ({"content": "看完了", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", parked)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    task = asyncio.create_task(review_runner.run_once(wid, rid))
    # 等它真的占到回合锁（状态判据，不靠固定睡眠、不设墙钟）。
    # [venue] 席位回合＝普通回合 ⇒ 锁 owner 与普通回合同形（"turn:<id>"）。
    for _ in range(300):
        if str(session_facade.turn_holder(wid, rid) or "").startswith("turn:"):
            break
        await asyncio.sleep(0.01)
    assert str(session_facade.turn_holder(wid, rid) or "").startswith("turn:"), \
        "评审轮必须占回合锁，否则停止端点判它「无在途回合」直接 no-op"

    res = control.request_stop(wid, rid)
    assert res.get("stopped") is True, res
    gate.set()
    # 收口有两条合法路径，都在 request_stop 里按状态选（在不在模型流里）：
    #   cancel 路 ⇒ 协程被取消；signal 路 ⇒ 下一圈入口消费停止位、如实报 stopped。
    # 这里钉的是**用户看得见的契约**：停止之后这一轮必须结束，不再往下跑。
    try:
        rv = await task
    except asyncio.CancelledError:
        rv = "cancelled"
    assert rv == "cancelled" or (isinstance(rv, dict) and rv.get("reason") == "stopped"), rv
    assert not session_facade.turn_holder(wid, rid), "收口后必须释放回合锁"
    assert f"{wid}/{rid}" not in review_runner._RUNNING, "并发闸要清干净，否则后续步永远 already_running"


async def test_run_once_registers_a_cancellable_turn(sandbox, monkeypatch):
    """评审轮的席位回合＝普通回合：在途期间 `_turn_tasks` 里必须能查到
    （登记点在 _run_message 内部）；收口后解登记。"""
    _mute_background_round(monkeypatch)
    wid, cid, fids, rid = await _setup()
    gate = asyncio.Event()

    async def parked(**kw):
        await gate.wait()
        return ({"content": "看完了", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", parked)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    task = asyncio.create_task(review_runner.run_once(wid, rid))
    for _ in range(300):
        if str(session_facade.turn_holder(wid, rid) or "").startswith("turn:"):
            break
        await asyncio.sleep(0.01)
    assert str(session_facade.turn_holder(wid, rid) or "").startswith("turn:"), \
        "席位回合在途必须占回合锁"
    from src.api import control as _c
    assert (wid, rid) in _c._turn_tasks, "在途评审轮必须登记 turn task，否则停止端点找不到目标"
    gate.set()
    await task
    assert (wid, rid) not in _c._turn_tasks, "收口要解登记，否则会误杀后续回合"
    assert not session_facade.turn_holder(wid, rid), "收口要释放评审卡的回合锁"


async def test_second_step_yields_while_the_card_is_busy(sandbox, monkeypatch):
    """评审卡被别的回合占着 ⇒ 本步让路并报 review_busy（不排队、不并发交错）。"""
    _mute_background_round(monkeypatch)
    wid, cid, fids, rid = await _setup()
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    owner = "user:msg"
    assert session_facade.turn_try_acquire(wid, rid, owner)
    try:
        out = await review_runner.run_once(wid, rid)
        # [2026-10-31] run_once 返回体带 reason（review_busy/stopped/...）
        assert out.get("continued") is False, out
    finally:
        session_facade.turn_release(wid, rid, owner)


# ============================================================
# ⑤ 装配层两处噪声（身份错位的同源问题）
# ============================================================
def test_ability_region_never_contradicts_itself():
    """有建卡权 ⇒ 只说「可以」；没建卡权 ⇒ 只说「没有」。不许同时出现。"""
    from src.api.context_regions import RegionCtx, render_card_ability_region

    base = dict(work_id="w", card_id="c1", cards=[], work_card_id="c0", cur=None,
                cur_body={}, cur_shadow={}, dep_relations_out=[], dep_relations_in=[],
                role_cabinet={}, function_roster=[], function_meta={},
                project_class="rd_project", type_registry={}, type_texts={},
                memory_state=None, memory_entries=[], controls={}, rule_sections={},
                history_source=[], history_mark=0, pending_inject=[])

    granted = RegionCtx(**{**base, "direct_child_status": [{"card_id": "k1"}],
                           "cur_meta": {"card_id": "c1", "card_type": "sub_project",
                                        "mission": "干活", "auto_mode": 0,
                                        "abilities": ["create_child"]}})
    t1 = render_card_ability_region(granted)
    assert "建自己的子卡：可以" in t1
    assert "没有这个能力" not in t1, "改前裸 else 会让两条矛盾行并列"

    denied = RegionCtx(**{**base, "direct_child_status": [],
                          "cur_meta": {"card_id": "c2", "card_type": "worker",
                                       "mission": "干活", "auto_mode": 0,
                                       "abilities": ["write_own_face"]}})
    t2 = render_card_ability_region(denied)
    assert "没有这个能力" in t2 and "建自己的子卡：可以" not in t2


def test_duty_region_has_no_duplicate_type_codes():
    """三套词表里重复声明的 code 只渲染一行（改前职能卡出现 3 行）。"""
    reg = card_store.load_type_registry()
    text = context_regions.render_card_duty_region(reg)
    codes = [l.split("（")[1].split("）")[0] for l in text.split("\n")
             if l.startswith("- ") and "（" in l]
    assert codes, "职责区不该为空"
    dup = sorted({c for c in codes if codes.count(c) > 1})
    assert not dup, f"同一卡型重复渲染：{dup}"


async def test_no_guidance_prompts_in_review_code(sandbox):
    """[2026-10-31 用户令] 代码里**绝不允许**加"告诉模型该怎么做"的提示词。

    「为什么要写这种引导性的提示词？去掉所有这种告诉它做什么的提示词，
      且绝不允许你加任意引导行为的提示词」

    ⇒ 怎么评、该不该查、查到什么程度，属于**职责书**（config/review_charters.json，
      用户写）的事；代码只提供事实（对象是谁、判据是什么、裁定原文是什么）。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "api" / "review_runner.py").read_text(
        encoding="utf-8")
    banned = [
        "需要核实别处", "不要为了找评审对象", "超出评审范围的意见不要提",
        "不得写成断言", "要下判定前请先用查阅工具核实", "按裁定改口",
        "## 出处纪律", "## 评审席身份", "最高优先",
    ]
    hit = [b for b in banned if b in src]
    assert not hit, f"代码里出现了指导性提示词（应写进职责书）：{hit}"


