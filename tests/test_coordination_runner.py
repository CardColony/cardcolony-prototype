# -*- coding: utf-8 -*-
"""[项目统筹 2026-11-21 · 任务二第三版] 查证轮＋四重校验＋完整 JSON 校验。

钉的事（任务书任务二＋用户实测三条）：
T1  材料隔离（A2）：scope=cards 时弱/强材料不含范围外卡号；scope=work 含全部卡
T1b 证据过滤：同批混入范围外/**他 work 同号卡**事件 ⇒ 出站正文、合法引用
    集合、待办均不含；引用被过滤掉的坐标 ⇒ 未判断
T2  弱模型协议：合法 wake ⇒ pending 记 weak_wake；pass ⇒ passed 留理由
T3  完整 JSON 校验：[]、[{"decision":"wake"}]（数组截取逃逸）、decision 非法、
    字段类型错、引用越界、模型不可用 ⇒ 全部 unjudged＋批次保留＋账 detail
    **含过滤后完整批次**（凭记录可回读证据）
T4  「修改标准」「给某卡下令」无控制效力
T5  强模型查证轮：**直接从任命入口起轮、不预填计划**——首跳材料只有目标/
    授权/卡清单；模型用只读查证指令从真实来源取需求原句与版本、查证
    **静默卡**；提交走 coord_submit_plan，空计划与 items:[{}] 不记成功；
    留痕进计划专属附加会话，**与主会话隔离**
T6  任命触发首轮（带任命标识）＋起轮抛错不影响任命
T7  后台绑定三反例：暂停/换人后旧任务 skipped；call 中途暂停 ⇒ 落账前复查
T8  [四重校验] 弱模型 call 返回后才暂停/改版本 ⇒ 同事务核对，wake 待办
    **零写入**，观察改记未判断
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.api import card_tools, coordination_runner, function_room, model_service, model_config, routes
from src.infra import coordination_watcher
from src.storage import card_store, coordination_store, function_store, session_facade, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()
    monkeypatch.setattr(model_config, "load",
                        lambda: {"models": {"strong-test-route": {},
                                            "weak-test-route": {}}})


async def _seeded_work(monkeypatch, title: str = "统筹任务二"):
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work(title)
    wid = wdata["work_id"]
    await function_room.seed_work(wid)
    fids = {}
    for p in function_store.list_profiles(wid):
        code = function_room.catalog_code_of(p)
        if code:
            fids[code] = str(p.get("function_id") or "")
    return wid, card_store.iter_card_metas(wid), fids


async def _mk_card(wid: str, title: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成",
                                     card_type="sub_project",
                                     skip_kick=True)
    return str(r["meta"]["card_id"])


def _rec(card: str, seq: int, etype: str, data: dict | None = None) -> dict:
    return {"seq": seq, "card_id": card, "type": etype,
            "data": data or {}, "run_id": "", "round": 0, "model": ""}


def _future_ts(hours: float = 1.0) -> str:
    return (datetime.now(timezone(timedelta(hours=8)))
            + timedelta(hours=hours)).isoformat()


def _appoint_kwargs(**over):
    kw = dict(
        models={"strong": "strong-test-route", "weak": "weak-test-route"},
        next_review_at=_future_ts(),
        observe={"scope": "work"},
        instruct={"scope": "cards", "ids": ["card_x"]},
        mode="observe", objective="按现行需求推进底座重构，先核无卡 agent 接入",
    )
    kw.update(over)
    return kw


async def _appointed(monkeypatch, wid: str, fids: dict, plan_card: str,
                     **over) -> str:
    """任命并把首轮起轮打桩掉（单独测起轮的用例自己恢复）。"""
    called = []

    async def _fake_first(work_id, card_id, assignment_id=""):
        called.append((work_id, card_id, assignment_id))
        return {"status": "parsed", "rev": 1}
    monkeypatch.setattr(routes, "_coord_first_turn", _fake_first)
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs(**over))
    assert out.get("ok"), out
    return out["appointment"]["assignment_id"]


def _fake_call_main(content: str, model_used: str = "weak-test-route",
                    usage: dict | None = None):
    """固定输出的 call_main 打桩（记录 system/user/prior）。"""
    calls = []

    async def _fake(system_prompt, user_prompt, **kw):
        calls.append({"system": system_prompt, "user": user_prompt,
                      "prior": kw.get("prior_messages"),
                      "model_name": kw.get("model_name"),
                      "tools": kw.get("tools")})
        return ({"content": content, "usage": usage or
                 {"prompt_tokens": 10, "completion_tokens": 5}},
                kw.get("model_name") or model_used)
    return _fake, calls


def _sequence_call_main(responses: list[str],
                        model_used: str = "strong-test-route",
                        hook=None):
    """按跳次序返回不同输出的 call_main 打桩（查证轮多跳；hook 可中途改状态）。"""
    calls = []

    async def _fake(system_prompt, user_prompt, **kw):
        calls.append({"system": system_prompt, "user": user_prompt,
                      "prior": kw.get("prior_messages"),
                      "model_name": kw.get("model_name")})
        if hook is not None:
            await hook(len(calls))
        i = min(len(calls) - 1, len(responses) - 1)
        return ({"content": responses[i], "usage":
                 {"prompt_tokens": 10, "completion_tokens": 5}},
                model_used)
    return _fake, calls


PLAN_JSON = {"plan": {
    "objective": "先核无卡 agent 接入",
    "requirements": [{"ref": "登录功能卡", "version": "approved",
                      "quote": "外部 agent 无卡也能接入"}],
    "items": [{"item_id": "participant-access", "title": "核接入",
               "owner_card": "card_x", "depends": [],
               "next_result": "一张无卡 agent 实际接入", "status": "todo"}],
    "wake_standards": [{"standard_id": "s1",
                        "protect": "外部 agent 无卡也能接入",
                        "ignore": "模块内部加可选关联",
                        "escalate": "公共协议把卡号改为必填"}],
    "direct_nodes": [], "next_check": "接入用例跑通"}}


# ---------------------------------------------------------------- T1 材料隔离

async def test_material_isolation(sandbox, monkeypatch):
    """[A2] scope=cards：范围外卡号一个字不进弱/强材料；scope=work 含全部卡。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "隔离计划")
    in_scope = await _mk_card(wid, "范围内卡")
    out_scope = await _mk_card(wid, "范围外卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [in_scope]})

    batch = {"batch_id": "b_t1", "changes": [
        {"coord": f"event:{wid}:{in_scope}:1", "card_id": in_scope,
         "kind": "card_block_edited", "summary": "改了一行", "evidence": "原文"}]}

    # 弱模型材料
    fake, calls = _fake_call_main(json.dumps({"decision": "pass", "reason": "普通"}))
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out["status"] == "passed"
    sent = calls[-1]
    assert in_scope in sent["user"]
    assert out_scope not in sent["user"]          # 范围外不泄露
    assert sent["model_name"] == "weak-test-route"
    assert sent["tools"] is None                   # 一次无工具调用

    # 强模型查证轮首跳材料：只有目标/授权/卡清单（不预填需求与任务）
    hop_plan = json.dumps({"action": "coord_read_card", "card_id": in_scope})
    fake2, calls2 = _sequence_call_main([hop_plan, json.dumps(PLAN_JSON)])
    monkeypatch.setattr(model_service, "call_main", fake2)
    s = await coordination_runner.run_strong_first_turn(wid, plan_card,
                                                        assignment_id=aid)
    assert s["status"] == "parsed"
    sent2 = calls2[0]
    assert in_scope in sent2["user"]
    assert out_scope not in sent2["user"]          # 强模型读取同样不泄露
    assert sent2["model_name"] == "strong-test-route"
    # 第二跳的查证结果回填里也没有范围外卡
    assert calls2[1]["user"].count(out_scope) == 0

    # 对照：scope=work 时全部卡都进材料
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="revoke")
    b2 = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"],
        **_appoint_kwargs(observe={"scope": "work"}, objective="o2"))
    aid2 = b2["appointment"]["assignment_id"]
    fake3, calls3 = _sequence_call_main(
        [hop_plan, json.dumps(PLAN_JSON)])
    monkeypatch.setattr(model_service, "call_main", fake3)
    await coordination_runner.run_strong_first_turn(wid, plan_card,
                                                    assignment_id=aid2)
    sent3 = calls3[0]["user"]
    assert in_scope in sent3 and out_scope in sent3


# ---------------------------------------------------------------- T1b 证据过滤

async def test_weak_batch_filtered_by_scope(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 同批混入范围内/范围外/**他 work 同号卡**事件 ⇒
    先过滤再组装：出站正文、合法引用集合、待办均不含；引用被过滤掉的
    坐标 ⇒ 未判断。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "过滤计划")
    in_scope = await _mk_card(wid, "范围内卡")
    out_scope = await _mk_card(wid, "范围外卡")
    await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [in_scope]})

    coord_in = f"event:{wid}:{in_scope}:10"
    coord_out = f"event:{wid}:{out_scope}:20"
    coord_other_work = f"event:work_OTHER:{in_scope}:99"   # 同号卡、他 work 前缀
    coord_mismatch = f"event:{wid}:{out_scope}:30"         # 坐标卡号与声明来源卡不一致
    batch = {"batch_id": "b_filter", "changes": [
        {"coord": coord_in, "card_id": in_scope, "kind": "card_block_edited",
         "summary": "范围内变化", "evidence": "原文A"},
        {"coord": coord_out, "card_id": out_scope, "kind": "msg_report",
         "summary": "范围外变化（不该出现在任何地方）", "evidence": "原文B"},
        {"coord": coord_other_work, "card_id": in_scope,
         "kind": "card_block_edited", "summary": "他 work 的同号卡证据",
         "evidence": "原文C"},
        # [2026-11-21 · 用户令实测] coord 里的卡号 ≠ 声明的 card_id ⇒ 整条丢弃
        {"coord": coord_mismatch, "card_id": in_scope,
         "kind": "card_block_edited", "summary": "坐标与声明对不上",
         "evidence": "原文D"}]}

    # 弱模型老实只引用范围内坐标 ⇒ passed
    fake, calls = _fake_call_main(json.dumps(
        {"decision": "pass", "reason": "只看范围内", "evidence_refs": [coord_in]}))
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out["status"] == "passed"
    sent = calls[-1]["user"]
    # 出站正文：范围内在；范围外与他 work 的整条都不出现
    assert coord_in in sent and "范围内变化" in sent
    assert coord_out not in sent and "范围外变化" not in sent and "原文B" not in sent
    assert coord_other_work not in sent and "他 work 的同号卡证据" not in sent
    assert coord_mismatch not in sent and "坐标与声明对不上" not in sent
    assert out_scope not in sent
    # 待办不含范围外证据
    obs = coordination_store.list_observations(wid, plan_card)
    assert obs[0]["status"] == "passed"
    all_refs = json.dumps(obs[0]["evidence_refs"])
    assert coord_out not in all_refs and coord_other_work not in all_refs

    # 引用被过滤掉的坐标（范围外 / 他 work 前缀）⇒ 未判断
    for bad in (coord_out, coord_other_work):
        fake2, _ = _fake_call_main(json.dumps({
            "decision": "wake", "reason": "想引用范围外",
            "evidence_refs": [bad]}))
        monkeypatch.setattr(model_service, "call_main", fake2)
        out2 = await coordination_runner.run_weak_observation(wid, plan_card, batch)
        assert out2["status"] == "unjudged"
        assert out2["detail"]["error"] == "引用不存在"
    # 待办：unjudged_batch 里不出现任何范围外坐标
    panel = coordination_store.panel(wid, plan_card)
    pending_text = json.dumps(panel["pending"], ensure_ascii=False)
    assert coord_out not in pending_text and coord_other_work not in pending_text


# ---------------------------------------------------------------- T2 弱模型协议

async def test_weak_protocol_wake_and_pass(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "协议计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)
    coord = f"event:{wid}:{src_card}:42"
    batch = {"batch_id": "b_t2", "changes": [
        {"coord": coord, "card_id": src_card, "kind": "card_block_edited",
         "summary": "公共协议把卡号改为必填", "evidence": "卡号: 必填"}]}

    # 合法 wake（引用合法）⇒ status=wake ＋ pending 记 weak_wake
    fake, _ = _fake_call_main(json.dumps({
        "decision": "wake", "item_id": "participant-access",
        "reason": "公共协议将卡号设为必填，无卡参与者可能无法接入",
        "evidence_refs": [coord],
        "question": "这个必填字段会不会挡住已承诺的无卡接入？"}))
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out["status"] == "wake" and out["item_id"] == "participant-access"
    panel = coordination_store.panel(wid, plan_card)
    wakes = [p for p in panel["pending"] if p["kind"] == "weak_wake"]
    assert len(wakes) == 1 and wakes[0]["item_id"] == "participant-access"
    obs = coordination_store.list_observations(wid, plan_card)
    assert obs[0]["status"] == "wake" and obs[0]["model_used"] == "weak-test-route"
    assert obs[0]["usage"].get("prompt_tokens") == 10

    # pass ⇒ passed，理由留账，pending 不新增 wake
    fake2, _ = _fake_call_main(json.dumps(
        {"decision": "pass", "reason": "普通进展不值得叫醒", "evidence_refs": []}))
    monkeypatch.setattr(model_service, "call_main", fake2)
    out2 = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out2["status"] == "passed"
    panel2 = coordination_store.panel(wid, plan_card)
    assert len([p for p in panel2["pending"]
                if p["kind"] == "weak_wake"]) == 1
    obs2 = coordination_store.list_observations(wid, plan_card)
    assert obs2[0]["status"] == "passed" and obs2[0]["reason"]


# ---------------------------------------------------------------- T3 未判断＋可回读

async def test_weak_unjudged_paths(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "未判断计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)
    coord = f"event:{wid}:{src_card}:7"
    batch = {"batch_id": "b_t3", "changes": [
        {"coord": coord, "card_id": src_card, "kind": "msg_directive",
         "summary": "收到指令", "evidence": "原文"}]}

    async def _run(content):
        fake, _ = _fake_call_main(content)
        monkeypatch.setattr(model_service, "call_main", fake)
        return await coordination_runner.run_weak_observation(
            wid, plan_card, batch)

    # 非法 JSON ⇒ unjudged＋批次保留
    assert (await _run("这不是JSON"))["status"] == "unjudged"
    # 顶层是数组（裸 []）⇒ unjudged
    out_arr = await _run("[]")
    assert out_arr["status"] == "unjudged"
    assert "顶层必须是 JSON 对象" in out_arr["detail"]["error"]
    # [2026-11-21 · 用户令实测] [{"decision":"wake"}] —— 数组截取逃逸 ⇒ unjudged
    out_nested = await _run('[{"decision": "wake", "evidence_refs": []}]')
    assert out_nested["status"] == "unjudged"
    assert "顶层必须是 JSON 对象" in out_nested["detail"]["error"]
    # decision 非法 ⇒ unjudged
    out_bad = await _run(json.dumps({"decision": "escalate_immediately",
                                     "evidence_refs": [coord]}))
    assert out_bad["status"] == "unjudged"
    # 字段类型错（evidence_refs 不是数组）⇒ unjudged
    out_type = await _run(json.dumps({
        "decision": "wake", "evidence_refs": "event:not-a-list"}))
    assert out_type["status"] == "unjudged"
    # 引用不属于过滤后集合 ⇒ unjudged
    out_ref = await _run(json.dumps({
        "decision": "wake", "evidence_refs": [f"event:{wid}:card_zzz:1"]}))
    assert out_ref["status"] == "unjudged"
    # 模型不可用 ⇒ unjudged
    async def _boom(*a, **kw):
        raise RuntimeError("连接超时")
    monkeypatch.setattr(model_service, "call_main", _boom)
    out4 = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out4["status"] == "unjudged"

    # 账本：7 条 unjudged 全落库；每条 detail 带**过滤后完整批次**（凭记录可回读）
    obs = coordination_store.list_observations(wid, plan_card)
    unjudged = [o for o in obs if o["status"] == "unjudged"]
    assert len(unjudged) == 7
    for o in unjudged:
        fb = o["detail"].get("filtered_batch") or {}
        assert fb.get("batch_id") == "b_t3"
        assert [c["coord"] for c in fb.get("changes") or []] == [coord]
        assert [c["evidence"] for c in fb.get("changes") or []] == ["原文"]
    # 待办保留批次（可重新处理）
    p = coordination_store.panel(wid, plan_card)
    kept = [x for x in p["pending"] if x["kind"] == "unjudged_batch"
            and x["batch_id"] == "b_t3"]
    assert len(kept) == 1


# ---------------------------------------------------------------- T4 无控制效力

async def test_weak_output_has_no_power(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "无效力计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)
    coord = f"event:{wid}:{src_card}:3"
    batch = {"batch_id": "b_t4", "changes": [
        {"coord": coord, "card_id": src_card, "kind": "card_block_edited",
         "summary": "需求草稿变了", "evidence": "原文"}]}
    evil = json.dumps({
        "decision": "pass", "reason": "顺便我把标准改了：给 card_x 下令开工",
        "evidence_refs": [coord],
        "new_wake_standards": [{"standard_id": "hack", "protect": "我说了算"}],
        "directive": {"target": "card_x", "instruction": "开工"}})
    fake, _ = _fake_call_main(evil)
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out["status"] == "passed"
    panel = coordination_store.panel(wid, plan_card)
    assert panel["rev"] == 0
    assert panel["grants"]["instruct"] == {"scope": "cards", "ids": ["card_x"]}
    assert all(p["kind"] in ("weak_wake", "unjudged_batch", "user_wake")
               for p in panel["pending"])
    assert not any(p["kind"] == "weak_wake" for p in panel["pending"])


# ---------------------------------------------------------------- T5 强模型查证轮

async def test_strong_first_turn_real_sources(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] **直接从任命入口起轮、不预填计划**：
    首跳材料只有目标/授权/卡清单；模型用只读查证指令从真实来源取需求
    原句与版本、主动查证**静默卡**；提交后留痕进计划专属附加会话，
    与主会话隔离。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "查证轮计划")
    src_card = await _mk_card(wid, "登录功能卡")
    silent_card = await _mk_card(wid, "静默卡（从未发言）")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    # 真实来源：需求正稿写在卡上（appointments 的 plan 只含 objective——不预填）
    await card_store.update_card(
        wid, src_card,
        {"requirements": "1. 外部 agent 无卡也能接入\n2. 卡号改为公共入口必填项",
         "mission": "打通外部 agent 接入"},
        "测试预置需求正稿")

    # 主会话先落一条消息（隔离对照基准）
    await session_facade.append_card_message(
        wid, plan_card, from_card=plan_card,
        text="主会话基准消息（统筹不该写这里）",
        msg_type="msg_notify")

    # 查证轮：hop0 查需求正稿卡 → hop1 主动查**静默卡** → hop2 提交
    # （提交的 requirements[].version 回指 hop0 读到的版本坐标——从查证结果
    #  里提取 sha256 指纹填进 plan，证明引用能回指当时读到的需求版本）
    hop0 = json.dumps({"action": "coord_read_card", "card_id": src_card})
    hop1 = json.dumps({"action": "coord_read_events", "card_id": silent_card})
    calls = []

    async def _verify_loop(system_prompt, user_prompt, **kw):
        calls.append({"system": system_prompt, "user": user_prompt,
                      "prior": kw.get("prior_messages"),
                      "model_name": kw.get("model_name")})
        if len(calls) == 1:
            return ({"content": hop0, "usage":
                     {"prompt_tokens": 10, "completion_tokens": 5}},
                    "strong-test-route")
        if len(calls) == 2:
            return ({"content": hop1, "usage":
                     {"prompt_tokens": 7, "completion_tokens": 3}},
                    "strong-test-route")
        import re as _re
        # 提取点＝hop0 的查证结果（第二跳的 user 文本里带 sha256 坐标）
        m = _re.search(r'"sha256": "([0-9a-f]+)"', calls[1]["user"])
        plan = json.loads(json.dumps(PLAN_JSON))
        plan["plan"]["requirements"][0]["version"] = m.group(1) if m else "?"
        plan["note"] = "已核对静默卡无在途工作"
        return ({"content": json.dumps(plan), "usage":
                 {"prompt_tokens": 12, "completion_tokens": 6}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _verify_loop)

    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out["status"] == "parsed" and out["rev"] == 1
    assert out["session"] == f"{plan_card}@coordination"

    # 多跳用量累计：3 跳（10+7+12 / 5+3+6）
    obs = coordination_store.list_observations(wid, plan_card, kind="strong_turn")
    parsed_obs = [o for o in obs if o["status"] == "parsed"][0]
    assert parsed_obs["usage"]["prompt_tokens"] == 29
    assert parsed_obs["usage"]["completion_tokens"] == 14

    # 首跳材料：不预填——卡上需求原文与任务都不在；只有目标/授权/卡清单
    first = calls[0]["user"]
    assert "卡号改为公共入口必填项" not in first      # 卡上需求原文不在材料里
    assert "participant-access" not in first          # 不预填任务
    assert plan_card in first and src_card in first and silent_card in first

    # 模型主动查证：第二跳回填含**真实需求原句**＋可核对版本坐标
    second = calls[1]["user"]
    assert "外部 agent 无卡也能接入" in second            # 从真实来源读到
    assert '"sha256"' in second                            # 版本坐标（内容指纹）
    assert "requirements_state" in second                 # 状态字段在查证结果里
    third = calls[2]["user"]
    # 静默卡主动查证：查到它的真实事件流（建卡事实——如实返回，不编造）
    assert "card_created" in third and silent_card in third

    # 计划落库（来自查证）且引用**回指查证坐标**
    panel = coordination_store.panel(wid, plan_card)
    assert panel["rev"] == 1
    assert panel["plan"]["requirements"][0]["quote"] == "外部 agent 无卡也能接入"
    import re as _re2
    fp_in_result = _re2.search(r'"sha256": "([0-9a-f]+)"',
                               calls[1]["user"]).group(1)
    assert panel["plan"]["requirements"][0]["version"] == fp_in_result

    # 职能身份：system 带**任命职能的职责书**（identity_for 注入宿主身份）
    assert "职责与授权" in calls[0]["system"]

    # 会话隔离：统筹留痕在专属附加会话；主会话零统筹内容
    main_text = session_facade._session_path(wid, plan_card).read_text(encoding="utf-8")
    coord_text = session_facade._session_path(
        wid, f"{plan_card}@coordination").read_text(encoding="utf-8")
    assert "统筹首轮" not in main_text                     # 主会话零写入
    assert "主会话基准消息" in main_text                   # 主会话原有内容仍在
    assert "统筹首轮" in coord_text and "查证 3 跳" in coord_text
    assert "主会话基准消息" not in coord_text              # 主会话内容不串进统筹会话

    # 空计划不记成功
    fake2, _ = _sequence_call_main([json.dumps(
        {"plan": {"objective": "", "items": [], "wake_standards": []}})])
    monkeypatch.setattr(model_service, "call_main", fake2)
    out_empty = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out_empty["status"] == "unparsed"
    assert "空计划" in out_empty["detail"]["error"]

    # items:[{}] 劣质形状不记成功
    bad = json.loads(json.dumps(PLAN_JSON))
    bad["plan"]["items"] = [{}]
    fake3, _ = _sequence_call_main([json.dumps(bad)])
    monkeypatch.setattr(model_service, "call_main", fake3)
    out_bad = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out_bad["status"] == "unparsed"
    assert "items 元素形状无效" in out_bad["detail"]["error"]


# ---------------------------------------------------------------- T6 任命触发

async def test_appoint_triggers_first_turn(client, sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "触发计划")
    called = []

    async def _fake_first(work_id, card_id, assignment_id=""):
        called.append((work_id, card_id, assignment_id))
        return {"status": "parsed"}
    monkeypatch.setattr(routes, "_coord_first_turn", _fake_first)

    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "appoint", "function_id": fids["pm"],
                                "objective": "按现行需求推进",
                                "observe": {"scope": "work"},
                                "instruct": {"scope": "cards",
                                             "ids": ["card_demo_protocol"]},
                                "models": {"strong": "strong-test-route",
                                           "weak": "weak-test-route"},
                                "next_review_at": _future_ts()})
    assert r.status_code == 200
    aid = r.json()["assignment_id"]
    for _ in range(50):
        if called:
            break
        await asyncio.sleep(0.02)
    assert called and called[0] == (wid, plan_card, aid)

    async def _boom(work_id, card_id, assignment_id=""):
        raise RuntimeError("起轮炸了")
    monkeypatch.setattr(routes, "_coord_first_turn", _boom)
    plan_b = await _mk_card(wid, "第二张计划")
    r2 = await client.post(f"/api/works/{wid}/cards/{plan_b}/coordination",
                           json={"op": "appoint", "function_id": fids["cto"],
                                 "objective": "另一份计划",
                                 "observe": {"scope": "work"},
                                 "instruct": {"scope": "work_development_cards"},
                                 "models": {"strong": "strong-test-route",
                                            "weak": "weak-test-route"},
                                 "next_review_at": _future_ts()})
    assert r2.status_code == 200


# ---------------------------------------------------------------- T7 后台绑定

async def test_first_turn_bound_to_assignment(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "绑定计划")

    # 反例一：任命 A 后**暂停**，A 的后台任务才执行 ⇒ 不启动，不落计划
    a = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid_a = a["appointment"]["assignment_id"]
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid_a, op="pause")
    out_paused = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid_a)
    assert out_paused["status"] == "skipped"
    assert coordination_store.panel(wid, plan_card)["rev"] == 1
    obs = coordination_store.list_observations(wid, plan_card)
    assert obs[0]["status"] == "skipped" and "paused" in obs[0]["detail"]["why"]

    # 反例二：撤回 A、任命 B，A 的后台任务**这时才**执行 ⇒ 不启动、不写入 B
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid_a, op="revoke")
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"],
        **_appoint_kwargs(objective="B 的目标"))
    aid_b = b["appointment"]["assignment_id"]
    fake, _ = _sequence_call_main([json.dumps(PLAN_JSON)])
    monkeypatch.setattr(model_service, "call_main", fake)
    out_stale = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid_a)
    assert out_stale["status"] == "skipped"
    pb = coordination_store.panel(wid, plan_card)
    assert pb["assignment_id"] == aid_b
    assert pb["plan"]["objective"] == "B 的目标"
    obs_b = [o for o in coordination_store.list_observations(wid, plan_card)
             if o["assignment_id"] == aid_b]
    assert all(o["status"] != "parsed" for o in obs_b)

    # 反例三：call **中途**暂停（落账前复查）⇒ 计划不写入
    async def _pause_midway(system_prompt, user_prompt, **kw):
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid_b, op="pause")
        return ({"content": json.dumps(PLAN_JSON), "usage": {}}, "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _pause_midway)
    out_mid = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid_b)
    assert out_mid["status"] == "skipped"
    assert out_mid["detail"]["why"] == "任命已暂停"
    pb2 = coordination_store.panel(wid, plan_card)
    assert pb2["state"] == "paused"
    assert pb2["plan"]["objective"] == "B 的目标"


# ---------------------------------------------------------------- T8 四重校验（弱模型侧）

async def test_weak_pending_atomic_checks(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 弱模型 call 返回后才暂停/改版本 ⇒ **同一事务**
    核对任命、活跃状态、读入版本——wake 待办零写入，观察改记未判断。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "原子核对计划")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    coord = f"event:{wid}:{src_card}:5"
    batch = {"batch_id": "b_t8", "changes": [
        {"coord": coord, "card_id": src_card, "kind": "card_block_edited",
         "summary": "变化", "evidence": "原文"}]}
    wake_out = json.dumps({"decision": "wake", "item_id": "i1",
                           "reason": "r", "evidence_refs": [coord],
                           "question": "q"})

    # 场景一：call **返回前**暂停（hook 在 call_main 返回时执行 pause）
    async def _hook_pause(_n):
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="pause")
    fake, _ = _sequence_call_main([wake_out], "weak-test-route", hook=_hook_pause)
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out["status"] == "unjudged"
    panel = coordination_store.panel(wid, plan_card)
    assert not any(p["kind"] == "weak_wake" for p in panel["pending"])
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="resume")

    # 场景二：call 返回后**改了计划**（rev 推进）⇒ 读入版本不符，待办零写入
    rev_before_edit = coordination_store.panel(wid, plan_card)["rev"]
    await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=rev_before_edit,
        plan={"objective": "用户改过", "items": []})
    rev_now = coordination_store.panel(wid, plan_card)["rev"]

    async def _hook_edit(_n):
        await coordination_store.save_plan(
            wid, plan_card, assignment_id=aid, expected_rev=rev_now,
            plan={"objective": "用户又改", "items": []})
    fake2, _ = _sequence_call_main([wake_out], "weak-test-route", hook=_hook_edit)
    monkeypatch.setattr(model_service, "call_main", fake2)
    out2 = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out2["status"] == "unjudged"
    panel2 = coordination_store.panel(wid, plan_card)
    assert not any(p["kind"] == "weak_wake" for p in panel2["pending"])

    # 场景三：任命/版本都没变 ⇒ 正常写入
    fake3, _ = _fake_call_main(wake_out)
    monkeypatch.setattr(model_service, "call_main", fake3)
    out3 = await coordination_runner.run_weak_observation(wid, plan_card, batch)
    assert out3["status"] == "wake"
    panel3 = coordination_store.panel(wid, plan_card)
    assert any(p["kind"] == "weak_wake" for p in panel3["pending"])


# ---------------------------------------------------------------- T9 撤权即时拦截

async def test_revocation_blocks_next_read_and_hop(sandbox, monkeypatch):
    """[2026-11-21 · 用户令 · 最优先] 第一跳返回前收窄观察权限（set_grant
    推进 rev）⇒ **查证前复核**拦住读取、**出站前复核**拦住下一跳——被撤的
    卡正文一个字都不出站，不再等到最后保存才被拦。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "撤权拦截计划")
    card_a = await _mk_card(wid, "将被撤权的卡")
    card_b = await _mk_card(wid, "保留下来的卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [card_a, card_b]})

    # 卡 A 写上敏感正文——撤权后绝不该出现在任何出站里
    await card_store.update_card(
        wid, card_a, {"requirements": "撤权后不可见的正文", "mission": "敏感使命"},
        "测试预置")

    hop0 = json.dumps({"action": "coord_read_card", "card_id": card_a})
    calls = []

    async def _loop(system_prompt, user_prompt, **kw):
        calls.append({"user": user_prompt, "prior": kw.get("prior_messages")})
        if len(calls) == 1:
            # 第一跳返回**前**，用户收窄观察权限（set_grant ⇒ rev 推进）
            await coordination_store.control_op(
                wid, plan_card, assignment_id=aid, op="set_grant",
                observe={"scope": "cards", "ids": [card_b]})
            return ({"content": hop0, "usage":
                     {"prompt_tokens": 10, "completion_tokens": 5}},
                    "strong-test-route")
        return ({"content": json.dumps(PLAN_JSON), "usage":
                 {"prompt_tokens": 5, "completion_tokens": 5}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _loop)

    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    # 查证前复核（读入版本已过期）⇒ 轮终止，卡 A 没被读、计划没被保存
    assert out["status"] == "skipped"
    assert "查证前复核未通过" in out["detail"]["why"]
    assert "读入版本" in out["detail"]["why"]
    # 出站里没有卡 A 的任何内容（首跳材料在撤权前生成、含卡号属合法；
    # 被撤卡的**正文/使命**任何跳都不出现，且撤权后没有第二跳出站）
    for c in calls:
        assert "撤权后不可见的正文" not in c["user"]
        assert "敏感使命" not in c["user"]
    # 只有第一跳出站过；没有第二跳
    assert len(calls) == 1
    # 计划没被保存（rev 停在 set_grant 推进后的值：appoint 0 → set_grant 1）
    panel = coordination_store.panel(wid, plan_card)
    assert panel["rev"] == 1 and panel["state"] == "active"
    assert panel["plan"].get("objective", "").startswith("按现行需求")
    # 账本 skipped 且 hops 里没有查证执行记录
    obs = coordination_store.list_observations(wid, plan_card, kind="strong_turn")
    assert obs[0]["status"] == "skipped"
    assert all(h.get("action") != "coord_read_card" or h["hop"] != 0
               for h in obs[0]["detail"].get("hops", []))


async def test_revocation_blocks_outbound_before_next_hop(sandbox, monkeypatch):
    """查证成功执行后、下一跳出站**前**撤权 ⇒ 出站被拦（查证结果不出站）。

    插桩点＝每跳留痕 _log_hop（它在查证执行之后、出站复核之前被 await）——
    第一次留痕（查证结果落专属会话）完成后立即撤回任命。
    """
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "出站拦截计划")
    card_a = await _mk_card(wid, "卡A")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [card_a]})

    hop0 = json.dumps({"action": "coord_read_card", "card_id": card_a})
    calls = []
    log_calls = [0]

    orig_append = session_facade.append

    async def _append_then_revoke(wid_, sid_, block, **kw):
        r = await orig_append(wid_, sid_, block, **kw)
        if str(sid_).endswith("@coordination"):
            log_calls[0] += 1
            if log_calls[0] == 2:  # 第一次＝hop0 输出留痕；第二次＝查证结果留痕
                await coordination_store.control_op(
                    wid_, plan_card, assignment_id=aid, op="revoke")
        return r

    async def _loop(system_prompt, user_prompt, **kw):
        calls.append({"user": user_prompt})
        if len(calls) == 1:
            return ({"content": hop0, "usage": {}}, "strong-test-route")
        return ({"content": json.dumps(PLAN_JSON), "usage": {}},
                "strong-test-route")

    monkeypatch.setattr(session_facade, "append", _append_then_revoke)
    monkeypatch.setattr(model_service, "call_main", _loop)

    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    # 出站前复核（任命已撤回）⇒ 第二跳不发、查证结果不出站、计划不保存
    assert out["status"] == "skipped"
    assert "出站前复核未通过" in out["detail"]["why"]
    assert len(calls) == 1                              # 第二跳从未发生
    assert "PLAN_JSON" not in json.dumps(calls, default=str)
    # 计划零写入：panel 的 plan 仍是任命时的 objective
    pb = coordination_store.panel(wid, plan_card)
    assert pb["state"] == "revoked"
    assert pb["plan"].get("objective", "").startswith("按现行需求")


# ---------------------------------------------------------------- T10 发送边界守卫

async def test_send_guard_semantics(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 强弱两路 call_main 都挂**发送边界守卫**：
    复核任命标识、活跃状态、读入版本——闭包在网关取得并发额度后、适配器
    发送前被调（网关 _checked_stream 原语）。"""
    from src.model_gateway.gateway import _checked_stream

    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "守卫计划")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    guards = []

    async def _capture(system, user, **kw):
        guards.append(kw.get("before_send"))
        return ({"content": json.dumps(PLAN_JSON), "usage": {}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _capture)
    await coordination_runner.run_strong_first_turn(wid, plan_card,
                                                    assignment_id=aid)
    guard = guards[-1]
    assert guard is not None                       # 守卫闭包真的传给了网关通道

    # 三态：正常通过（不抛）
    g = coordination_runner._make_send_guard(wid, plan_card, aid, 1)
    await g()                                       # 不抛＝通过
    # 暂停 ⇒ 抛
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause")
    with pytest.raises(RuntimeError, match="已paused"):
        await g()
    # 撤回 ⇒ 抛（assignment 仍匹配，走「非活跃」分支：任命已revoked）
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="resume")
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="revoke")
    with pytest.raises(RuntimeError, match="revoked"):
        await g()
    # 版本推进分支：重新任命 B（cto）并改计划
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"],
        **_appoint_kwargs(objective="B"))
    assert b.get("ok"), b
    aid_b = b["appointment"]["assignment_id"]
    # 换人 ⇒ 抛（B 已在任；g 属于已撤回的旧任命 aid——assignment 不匹配分支）
    with pytest.raises(RuntimeError, match="换人或已撤回"):
        await g()
    # 读入版本过期 ⇒ 抛
    g2 = coordination_runner._make_send_guard(wid, plan_card, aid_b, 0)
    await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid_b, expected_rev=0,
        plan={"objective": "推进", "items": []})
    with pytest.raises(RuntimeError, match="已过期"):
        await g2()

    # 网关原语直测：before_send 抛 ⇒ 守卫异常包出、适配器流零产出
    sent = []

    async def _adapter_gen():
        sent.append(1)                              # 适配器体执行标记
        yield "chunk"

    async def _reject():
        raise RuntimeError("守卫拒绝")

    agen = _checked_stream(_adapter_gen(), _reject)
    consumed = []
    with pytest.raises(Exception):
        async for c in agen:
            consumed.append(c)
    assert sent == []                               # 适配器 0 调用
    assert consumed == []

    # 守卫通过 ⇒ 适配器正常产出
    async def _pass():
        return None
    agen2 = _checked_stream(_adapter_gen(), _pass)
    got = [c async for c in agen2]
    assert got == ["chunk"] and sent == [1]


# ---------------------------------------------------------------- T11 专属会话正式入口＋续用

async def test_coord_session_registered_and_reused(client, sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 起轮即把 @coordination 登记进 cards.sessions
    （正式入口可查）；查证留痕可从 timeline?session= 正式读回；下一轮起轮
    上下文带上一轮统筹会话记录（不再从空历史组装）。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "登记续用计划")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    await card_store.update_card(
        wid, src_card, {"requirements": "外部 agent 无卡也能接入"},
        "预置需求")

    hop0 = json.dumps({"action": "coord_read_card", "card_id": src_card})
    calls = []

    async def _loop(system_prompt, user_prompt, **kw):
        calls.append({"user": user_prompt, "system": system_prompt})
        if len(calls) == 1:
            return ({"content": hop0, "usage": {}}, "strong-test-route")
        return ({"content": json.dumps(PLAN_JSON), "usage": {}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _loop)

    sid = f"{plan_card}@coordination"
    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out["status"] == "parsed"

    # ① 登记进正式入口：GET sessions 含统筹会话（persona=任命职能）
    r = await client.get(f"/api/works/{wid}/cards/{plan_card}/sessions")
    assert r.status_code == 200
    entries = {e["session_id"]: e for e in r.json()["sessions"]}
    assert sid in entries
    assert entries[sid]["persona"] == fids["pm"]
    assert entries[sid]["label"] == "统筹会话"

    # ② 正式读取入口查回查证留痕（timeline?session=）
    r2 = await client.get(
        f"/api/works/{wid}/cards/{plan_card}/timeline?session={sid}")
    assert r2.status_code == 200
    timeline_text = json.dumps(r2.json(), ensure_ascii=False)
    assert "统筹输出" in timeline_text or "查证结果" in timeline_text

    # ③ 下一轮续用同一专属会话：首跳材料带上一轮统筹会话记录
    calls.clear()
    out2 = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out2["status"] == "parsed"
    first2 = calls[0]["user"]
    assert "上一轮统筹会话记录" in first2
    assert "统筹输出" in first2                      # 上一轮留痕被读回
    assert "职责与授权" in calls[0]["system"]         # 同一任命职能职责书身份
    # 仍写同一会话（登记处不重复建）
    r3 = await client.get(f"/api/works/{wid}/cards/{plan_card}/sessions")
    coord_entries = [e for e in r3.json()["sessions"]
                     if e["session_id"] == sid]
    assert len(coord_entries) == 1


# ---------------------------------------------------------------- T12 历史回灌按授权过滤

async def test_history_readback_respects_revocation(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 撤权后新起一轮，首跳**实际模型输入**：
    被撤卡的旧需求原文消失（不回流），仍获授权卡的上一轮记录可续用。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "回灌过滤计划")
    card_a = await _mk_card(wid, "将被撤权的卡")
    card_b = await _mk_card(wid, "保留授权的卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [card_a, card_b]})

    await card_store.update_card(
        wid, card_a, {"requirements": "撤权后不可见的正文"}, "预置 A 需求")

    # 第一轮：查 A（留痕标注 A）→ 查 B（留痕标注 B）→ 提交（标注当时授权集）
    hop0 = json.dumps({"action": "coord_read_card", "card_id": card_a})
    hop1 = json.dumps({"action": "coord_read_card", "card_id": card_b})
    calls1 = []

    async def _loop1(system_prompt, user_prompt, **kw):
        calls1.append({"user": user_prompt})
        if len(calls1) == 1:
            return ({"content": hop0, "usage": {}}, "strong-test-route")
        if len(calls1) == 2:
            return ({"content": hop1, "usage": {}}, "strong-test-route")
        return ({"content": json.dumps(PLAN_JSON), "usage": {}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _loop1)
    out1 = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out1["status"] == "parsed"
    assert "撤权后不可见的正文" in calls1[1]["user"]  # 第一轮确实查到过 A 原文

    # 撤回重新任命（scope 收窄：只保留 B）
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="revoke")
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"],
        **_appoint_kwargs(objective="B 任期", observe={
            "scope": "cards", "ids": [card_b]}))
    aid_b = b["appointment"]["assignment_id"]

    # 新一轮：首跳实际输入——A 的原文消失，B 的上一轮查证记录续用
    calls2 = []

    async def _loop2(system_prompt, user_prompt, **kw):
        calls2.append({"user": user_prompt})
        return ({"content": json.dumps(PLAN_JSON), "usage": {}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _loop2)
    out2 = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid_b)
    assert out2["status"] == "parsed"
    first2 = calls2[0]["user"]
    assert "撤权后不可见的正文" not in first2        # 被撤卡原文不回流
    assert "上一轮统筹会话记录" in first2            # 历史通道仍在
    assert "coord_read_card" in first2               # 仍授权卡（B）的查证记录续用


# ---------------------------------------------------------------- T13 旧块排除＋收口推进

async def test_legacy_body_marks_cannot_authorize_history(sandbox, monkeypatch):
    """旧正文即使碰巧含旧的文本标记，也不会获得宿主来源字段。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "旧块排除计划")
    card_a = await _mk_card(wid, "已撤权卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [card_a]})

    # 用户实测复现：**旧格式**（无标注）写入一条已撤权卡的需求原文
    from src.storage import session_facade
    sid = coordination_runner._session_key(plan_card)
    await session_facade.append_card_message(
        wid, sid, from_card=plan_card,
        text="【查证结果】旧正文引用 [coord:cards=]，包含旧格式无标注的已撤卡需求原文",
        msg_type="msg_notify")

    # 撤回重新任命收缩授权（card_a 不在新授权集）
    other = await _mk_card(wid, "新授权卡")
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="revoke")
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"],
        **_appoint_kwargs(objective="新任期", observe={
            "scope": "cards", "ids": [other]}))
    aid_b = b["appointment"]["assignment_id"]

    calls = []

    async def _loop(system_prompt, user_prompt, **kw):
        calls.append({"user": user_prompt})
        return ({"content": json.dumps(PLAN_JSON), "usage": {}},
                "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _loop)
    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid_b)
    assert out["status"] == "parsed"
    first = calls[0]["user"]
    # ① 无标注的旧块不回流（被撤卡需求原文消失）
    assert "旧格式无标注的已撤卡需求原文" not in first

    assert any("旧格式无标注的已撤卡需求原文" in b["content"]
               for b in await session_facade.read(wid, sid))


# ---------------------------------------------------------------- T14 任务四：发令受理

async def test_instruction_intake_gates(sandbox, monkeypatch):
    """[任务四] 发令同步受理点：范围双闸/非开发卡/证据核对/幂等/旧令牌。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "发令计划")
    dev_card = await _mk_card(wid, "开发卡")
    out_dev = await _mk_card(wid, "范围外开发卡")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "work"}, instruct={"scope": "cards", "ids": [dev_card]})

    async def intake(**kw):
        kw.setdefault("turn_id", "turn_t14_1")
        kw.setdefault("rev", kw.pop("_rev", 0))
        return await coordination_runner.coordination_intake(
            wid, plan_card, assignment_id=aid, **kw)

    coord_ok = f"event:{wid}:{src_card}:9"
    # ① 证据先入观察（seen）——用 feed 真实进批
    coordination_watcher.feed(wid, src_card, _rec(src_card, 9, "card_block_edited"))
    # ② 不在发令范围 ⇒ 拒
    r = await intake(rev=0, target_card=out_dev, instruction="开工",
                     item_id=None, evidence_refs=[coord_ok])
    assert not r["ok"] and r["code"] == "not_instructable"
    # ③ 证据不属于本任命观察事件 ⇒ 拒
    r = await intake(rev=0, target_card=dev_card, instruction="开工",
                     item_id=None, evidence_refs=[f"event:{wid}:{src_card}:999"])
    assert not r["ok"] and r["code"] == "bad_evidence"
    # ④ 合法受理 ⇒ 受理号 + 落信
    r = await intake(rev=0, target_card=dev_card, instruction="开工",
                     item_id="i1", evidence_refs=[coord_ok])
    assert r["ok"] and r["receipt_id"].startswith("rcp_")
    # 落信：开发卡会话收到统筹指令（统筹身份标注）
    dev_sess = session_facade._session_path(wid, dev_card).read_text(encoding="utf-8")
    assert "统筹发令" in dev_sess and "开工" in dev_sess and r["receipt_id"] in dev_sess
    # ⑤ 幂等：同内容重放 ⇒ 同一受理号
    r2 = await intake(rev=0, target_card=dev_card, instruction="开工",
                      item_id="i1", evidence_refs=[coord_ok])
    assert r2["ok"] and r2["receipt_id"] == r["receipt_id"] and r2["replayed"]
    # ⑥ 每轮最多一条：同 turn_id 第二条（不同内容）⇒ 拒
    r3 = await intake(rev=0, target_card=dev_card, instruction="改需求",
                      item_id=None, evidence_refs=[coord_ok])
    assert not r3["ok"] and r3["code"] == "turn_exhausted"
    # ⑦ 旧令牌：rev 过期 ⇒ 拒
    r4 = await intake(rev=99, turn_id="turn_t14_2", target_card=dev_card,
                      instruction="新内容指令", item_id=None,
                      evidence_refs=[coord_ok])
    assert not r4["ok"] and r4["code"] == "stale_token"
    # ⑧ 非开发卡：显式卡集下职能卡先撞发令范围 ⇒ not_instructable
    r5 = await intake(rev=0, turn_id="turn_t14_3", target_card=fids["pm"],
                      instruction="改职责", item_id=None,
                      evidence_refs=[coord_ok])
    assert not r5["ok"] and r5["code"] == "not_instructable"
    # ⑧b 全部开发卡范围下职能卡仍拒（非开发卡判据）⇒ not_developable
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="revoke")
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"],
        **{"models": {"strong": "strong-test-route", "weak": "weak-test-route"},
           "next_review_at": _future_ts(), "observe": {"scope": "work"},
           "instruct": {"scope": "work_development_cards"},
           "mode": "observe", "objective": "全部开发卡授权"})
    aid_b = b["appointment"]["assignment_id"]
    r6 = await coordination_runner.coordination_intake(
        wid, plan_card, assignment_id=aid_b, rev=0, turn_id="turn_t14_4",
        target_card=fids["pm"], instruction="改职责", item_id=None,
        evidence_refs=[coord_ok])
    assert not r6["ok"] and r6["code"] == "not_developable"


async def test_instruction_via_turn_tool(sandbox, monkeypatch):
    """[任务四] 查证轮内模型以正式动作发令：受理成功 ⇒ 落信＋审计＋轮终结。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "轮内发令计划")
    dev_card = await _mk_card(wid, "开发卡")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "work"}, instruct={"scope": "cards", "ids": [dev_card]})

    coord = f"event:{wid}:{src_card}:4"
    coordination_watcher.feed(wid, src_card, _rec(src_card, 4, "worker_done"))

    hop0 = json.dumps({"action": "coord_read_card", "card_id": dev_card})
    hop1 = json.dumps({"action": "coord_submit_instruction",
                       "target_card": dev_card, "item_id": "i1",
                       "instruction": "按需求核对无卡接入用例",
                       "evidence_refs": [coord]})
    fake, calls = _sequence_call_main([hop0, hop1, json.dumps(PLAN_JSON)])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out["status"] == "parsed" and out.get("receipt_id", "").startswith("rcp_")
    # 落信
    dev_sess = session_facade._session_path(wid, dev_card).read_text(encoding="utf-8")
    assert "统筹发令" in dev_sess and out["receipt_id"] in dev_sess
    # 审计
    obs = coordination_store.list_observations(wid, plan_card, kind="instruction")
    assert obs and obs[0]["status"] == "delivered"
    assert obs[0]["detail"]["target_card"] == dev_card


async def test_ask_function_tool(sandbox, monkeypatch):
    """[任务四] 问专家：向职能卡发问询（不算发令）＋审计；幽灵职能拒绝。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "问询计划")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    hop0 = json.dumps({"action": "coord_ask_function",
                       "function_id": fids["qa"], "question": "登录用例过了吗？"})
    hop1 = json.dumps(PLAN_JSON)
    fake, calls = _sequence_call_main([hop0, hop1])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out["status"] == "parsed"
    qa_sess = session_facade._session_path(wid, fids["qa"]).read_text(encoding="utf-8")
    assert "统筹问询" in qa_sess and "登录用例" in qa_sess
    obs = coordination_store.list_observations(wid, plan_card, kind="ask")
    assert obs and obs[0]["status"] == "delivered"

    # 幽灵职能 ⇒ 拒（回给模型，继续轮）
    ghost = json.dumps({"action": "coord_ask_function",
                        "function_id": "fn_ghost", "question": "x"})
    fake2, _ = _sequence_call_main([ghost, json.dumps(PLAN_JSON)])
    monkeypatch.setattr(model_service, "call_main", fake2)
    out2 = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out2["status"] == "parsed"     # 被拒后轮继续走完
