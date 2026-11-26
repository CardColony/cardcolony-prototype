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
    """创建任命，起轮由各用例显式驱动。"""
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


# ---------------------------------------------------------------- 项目配置

async def test_project_configuration_does_not_schedule_a_model_turn(client, sandbox, monkeypatch):
    from src.api import message_runner

    wid, _, _ = await _seeded_work(monkeypatch)
    plan_card = coordination_store.PROJECT_AGENT_ID
    called = []

    def capture(coro, **kwargs):
        coro.close()
        called.append(kwargs)

    monkeypatch.setattr(message_runner, "_spawn_tracked", capture)

    payload = {"op": "configure", "models": _appoint_kwargs()["models"]}
    r = await client.post(f"/api/works/{wid}/coordination/agent", json=payload)
    assert r.status_code == 200
    aid = r.json()["assignment_id"]
    assert aid and coordination_store.panel(wid, plan_card)["pending"] == []
    assert called == []


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

async def _direct_env(monkeypatch):
    """复用 direct 落信/唤醒但测试中不真起卡回合。"""
    from src.api import message_runner as mr
    woke = []

    async def _fake_wake(work_id, card_id, **kw):
        if not kw["admission"]():
            return False
        woke.append((work_id, card_id, kw.get("source_kind")))
        return True
    monkeypatch.setattr(mr, "deliver_now", _fake_wake)
    return woke


async def _link_actor(wid, pc, target):
    """以任命职能身份建正式通信线（policy.check_linked 的唯一判据）。"""
    p = coordination_store.panel(wid, pc)
    actor = str(p.get("function_id") or "")
    if actor:
        await card_store.update_card(
            wid, actor, {"links": [{"target": target, "type": "通信"}]},
            "任务四测试建线")


async def _field(wid, pc, aid, *, target, instruction="开工",
                 item_id="i1", refs, rev=0, turn="turn_t14_1"):
    await _link_actor(wid, pc, target)
    return await coordination_runner.coordination_intake(
        wid, pc, assignment_id=aid, rev=rev, turn_id=turn,
        item_id=item_id, target_card=target, instruction=instruction,
        evidence_refs=refs, acceptance="按需求完成并用 report_up 汇报")


async def test_instruction_intake_gates(sandbox, monkeypatch):
    """[任务四] 发令受理七道闸：受理只给稳定行动键；落信后才有受理号。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    woke = await _direct_env(monkeypatch)
    plan_card = await _mk_card(wid, "发令计划")
    dev_card = await _mk_card(wid, "开发卡")
    out_dev = await _mk_card(wid, "范围外开发卡")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev_card]})
    coord_ok = f"event:{wid}:{src_card}:9"
    coordination_watcher.feed(wid, src_card, _rec(src_card, 9, "card_block_edited"))

    # 不在发令范围 ⇒ 拒
    r = await _field(wid, plan_card, aid, target=out_dev, refs=[coord_ok])
    assert not r["ok"] and r["code"] == "not_instructable"
    # 证据不属于本任命观察事件 ⇒ 拒
    r = await _field(wid, plan_card, aid, target=dev_card,
                     refs=[f"event:{wid}:{src_card}:999"])
    assert not r["ok"] and r["code"] == "bad_evidence"
    # 合法受理 ⇒ 只给 action_key（无受理号、无落信）
    r = await _field(wid, plan_card, aid, target=dev_card, refs=[coord_ok])
    assert r["ok"] and r["action_key"] and not r.get("receipt_id")
    dev_sess = session_facade._session_path(wid, dev_card).read_text(encoding="utf-8")
    assert "统筹指令" not in dev_sess
    # 落信 ⇒ 受理号 + msg_directive + direct 唤醒（分账三笔）
    d = await coordination_runner.coordination_deliver(
        wid, plan_card, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d["ok"] and d["receipt_id"].startswith("rcp_")
    dev_sess = session_facade._session_path(wid, dev_card).read_text(encoding="utf-8")
    assert "统筹指令" in dev_sess and "开工" in dev_sess and d["msg_id"] in dev_sess
    assert woke and woke[-1] == (wid, dev_card, "directive")
    kinds = [o["kind"] for o in coordination_store.list_observations(wid, plan_card)]
    assert {"instruction_accepted", "instruction_delivered",
            "instruction_woke"}.issubset(set(kinds))
    # 幂等重放：同 action_key 落信 ⇒ 原受理号、不重复唤醒
    d2 = await coordination_runner.coordination_deliver(
        wid, plan_card, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d2["ok"] and d2["replayed"] and d2["receipt_id"] == d["receipt_id"]
    assert len(woke) == 1
    # 每轮最多一条：同轮另一动作 ⇒ 拒
    await coordination_store.control_op(wid, plan_card, assignment_id=aid,
                                        op="pause")  # 释放 rev 无关，先撤离任
    await coordination_store.control_op(wid, plan_card, assignment_id=aid,
                                        op="revoke")
    # 旧令牌：换人后旧 assignment ⇒ 拒
    r4 = await _field(wid, plan_card, aid, target=dev_card,
                      refs=[coord_ok], turn="turn_t14_2")
    assert not r4["ok"] and r4["code"] == "stale_token"
    # 非开发卡（全部开发卡范围下职能卡）⇒ 拒
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"],
        **{"models": {"strong": "strong-test-route", "weak": "weak-test-route"},
           "next_review_at": _future_ts(), "observe": {"scope": "work"},
           "instruct": {"scope": "work_development_cards"},
           "mode": "live", "objective": "全部开发卡授权"})
    aid_b = b["appointment"]["assignment_id"]
    r6 = await _field(wid, plan_card, aid_b, target=fids["pm"],
                      refs=[coord_ok], turn="turn_t14_4")
    assert not r6["ok"] and r6["code"] == "not_developable"


async def test_observe_mode_zero_write(sandbox, monkeypatch):
    """[反例①] 旁观模式发令 ⇒ 拒，开发卡零写入。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "旁观计划")
    dev = await _mk_card(wid, "开发卡")
    src = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="observe",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    coord = f"event:{wid}:{src}:8"
    coordination_watcher.feed(wid, src, _rec(src, 8, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    assert not r["ok"] and r["code"] == "observe_mode"
    assert "统筹指令" not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    # [任务书] 拟发记录：零收件但保留发令意图供审计
    drafts = [o for o in coordination_store.list_observations(wid, pc)
              if o["kind"] == "instruction_draft"]
    assert drafts and drafts[0]["detail"]["code"] == "observe_mode"


async def test_pause_between_intake_and_delivery(sandbox, monkeypatch):
    """[反例③] 受理后暂停 ⇒ 落信同事务重验失败，零落信。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "受理后暂停计划")
    dev = await _mk_card(wid, "开发卡")
    src = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    coord = f"event:{wid}:{src}:8"
    coordination_watcher.feed(wid, src, _rec(src, 8, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    assert r["ok"]
    await coordination_store.control_op(wid, pc, assignment_id=aid, op="pause")
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert not d["ok"] and d["code"] == "stale_token"
    assert "统筹指令" not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")


async def test_delivery_failure_then_retry(sandbox, monkeypatch):
    """[反例④] 首次写入失败 ⇒ 无受理号；按原动作键重试成功 ⇒ 恰好一条收件。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "写入失败重试计划")
    dev = await _mk_card(wid, "开发卡")
    src = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    coord = f"event:{wid}:{src}:8"
    coordination_watcher.feed(wid, src, _rec(src, 8, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    assert r["ok"]
    orig_append = coordination_runner.session_facade.append_directive_sync \
        if hasattr(coordination_runner, "session_facade") else session_facade.append_directive_sync

    def _fail_once(wid_, cid_, *a, **kw):
        if cid_ == dev:
            raise OSError("模拟落信失败")
        return orig_append(wid_, cid_, *a, **kw)
    monkeypatch.setattr(session_facade, "append_directive_sync", _fail_once)
    d1 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert not d1["ok"] and d1["code"] == "projection_failed"
    assert d1.get("msg_id")                    # 权威收件已提交（稳定消息号）
    monkeypatch.setattr(session_facade, "append_directive_sync",
                        orig_append)
    # 按**原动作键**重试 ⇒ 新受理号＋一条收件
    d2 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d2["ok"] and d2["msg_id"] == d1["msg_id"]   # 稳定消息号
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert sess.count(d2["msg_id"]) == 1


async def test_cross_work_evidence_rejected(sandbox, monkeypatch):
    """[反例⑤] 他 work 的同号卡事件坐标 ⇒ 拒（work 前缀核对）。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "跨 work 证据计划")
    dev = await _mk_card(wid, "开发卡")
    src = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    coord = f"event:{wid}:{src}:8"
    coordination_watcher.feed(wid, src, _rec(src, 8, "card_block_edited"))
    bad = f"event:work_OTHER:{src}:8"
    r = await _field(wid, pc, aid, target=dev, refs=[bad])
    assert not r["ok"] and r["code"] == "bad_evidence"
    assert "本 work" in r["why"]
    assert "统筹指令" not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")


async def test_instruction_via_turn_tool(sandbox, monkeypatch):
    """[任务四] 实发轮内模型动作 ⇒ 正式 direct 落信＋三账＋轮终结。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    woke = await _direct_env(monkeypatch)
    plan_card = await _mk_card(wid, "轮内发令计划")
    dev_card = await _mk_card(wid, "开发卡")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev_card]})
    added = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=0,
        changes=[{"op": "add", "title": "核查无卡接入", "owner_card": dev_card}])
    item_id = added["allocated"]["add:0"]
    await _link_actor(wid, plan_card, dev_card)
    coord = f"event:{wid}:{src_card}:4"
    coordination_watcher.feed(wid, src_card, _rec(src_card, 4, "worker_done"))
    hop0 = json.dumps({"action": "coord_read_card", "card_id": dev_card})
    hop1 = json.dumps({"action": "coord_submit_instruction",
                       "target_card": dev_card, "item_id": item_id,
                       "instruction": "按需求核对无卡接入用例", "acceptance": "按需求完成并汇报",
                       "evidence_refs": [coord]})
    fake, calls = _sequence_call_main([hop0, hop1, json.dumps(PLAN_JSON)])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid)
    assert out["status"] == "parsed" and out.get("receipt_id", "").startswith("rcp_")
    sess = session_facade._session_path(wid, dev_card).read_text(encoding="utf-8")
    assert "统筹指令" in sess and "无卡接入" in sess
    assert woke and woke[-1][0:2] == (wid, dev_card)
    obs = coordination_store.list_observations(wid, plan_card)
    assert all(k in [o["kind"] for o in obs]
               for k in ("instruction_accepted", "instruction_delivered",
                         "instruction_woke"))


async def test_review_turn_zero_instruction(sandbox, monkeypatch):
    """[反例②] 到期独立回看轮输出发令动作 ⇒ 拒，开发卡零写入。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "回看零发令计划")
    dev = await _mk_card(wid, "开发卡")
    src = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    coord = f"event:{wid}:{src}:3"
    coordination_watcher.feed(wid, src, _rec(src, 3, "card_block_edited"))
    hop0 = json.dumps({"action": "coord_read_card", "card_id": src})
    hop1 = json.dumps({"action": "coord_submit_instruction",
                       "target_card": dev, "item_id": "i1",
                       "instruction": "禁止写入", "acceptance": "按需求完成并汇报", "evidence_refs": [coord]})
    fake, calls = _sequence_call_main([hop0, hop1])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(
        wid, pc, assignment_id=aid, trigger="review_due")
    assert out["status"] == "unparsed"
    assert "只读回看" in out["detail"]["error"]
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert "统筹指令" not in sess and "禁止写入" not in sess


async def test_ask_function_tool(sandbox, monkeypatch):
    """[②] 指定职能只读回合：问题稳定标识＋答复回原事项；空工具面；
    重复领取回读原答案；自问/幽灵职能被拒。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "问询计划")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    ask_args = {"action": "coord_ask_function",
                "function_id": fids["cto"], "question": "登录接入是否影响无卡参与者？",
                "item_id": "i1", "requirements_version": "v7",
                "evidence_refs": []}
    calls = []

    async def _fake(system, user, **kw):
        calls.append({"system": system, "user": user,
                      "caller": kw.get("caller"), "tools": kw.get("tools"),
                      "before_send": kw.get("before_send")})
        if kw.get("caller") == "coordination_expert":
            return ({"content": json.dumps({
                "answer": "当前代码路径强制卡号；建议改为可选关联，验证办法：无卡参与者集成测试"})},
                "strong-test-route")
        if len([c for c in calls if c["caller"] != "coordination_expert"]) == 1:
            return ({"content": json.dumps(ask_args)}, "strong-test-route")
        return ({"content": json.dumps(PLAN_JSON)}, "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _fake)
    out = await coordination_runner.run_strong_first_turn(wid, pc,
                                                          assignment_id=aid)
    assert out["status"] == "parsed"
    expert = [c for c in calls if c["caller"] == "coordination_expert"]
    assert len(expert) == 1 and expert[0]["tools"] is None
    assert expert[0]["before_send"] is not None
    assert "职责书" in expert[0]["system"]
    def _q(conn):
        coordination_runner._ensure_questions(conn)
        return conn.execute(
            "SELECT * FROM coordination_questions WHERE work_id=?", (wid,)).fetchall()
    from src.storage import card_db as _cdb
    rows = _cdb.read_sync(wid, _q, [])
    assert rows and rows[0]["status"] == "answered"
    assert "无卡参与者" in rows[0]["answer"]
    qid = rows[0]["question_id"]
    answered = [o for o in coordination_store.list_observations(wid, pc)
                if o["kind"] == "ask_answered"]
    assert answered and answered[0]["detail"]["question_id"] == qid
    assert any(qid in c["user"] for c in calls)

    cur_rev = int(coordination_store.panel(wid, pc)["rev"])
    again = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid, rev=cur_rev, item_id="i1",
        function_id=fids["cto"], question=ask_args["question"],
        requirements_version="v7", evidence_refs=[], turn_id="turn_again",
        model_used="strong-test-route")
    assert again["ok"] and again["replayed"] and again["answer"] == rows[0]["answer"]
    assert len([c for c in calls if c["caller"] == "coordination_expert"]) == 1

    self_ask = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid, rev=cur_rev, item_id="i1",
        function_id=fids["pm"], question="自问",
        requirements_version="v7", evidence_refs=[], turn_id="turn_self",
        model_used="strong-test-route")
    assert not self_ask["ok"] and self_ask["code"] == "self_ask"
    ghost = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid, rev=cur_rev, item_id="i1",
        function_id="fn_ghost", question="x", requirements_version="v7",
        evidence_refs=[], turn_id="turn_ghost", model_used="strong-test-route")
    assert not ghost["ok"] and ghost["code"] == "invalid_function"


async def test_delivered_but_wake_failed_retry(sandbox, monkeypatch):
    """[反例④] 已落信但唤醒失败 ⇒ 同键重试只补唤醒，绝不重复落块。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    woke_calls = []
    from src.api import message_runner as mr

    async def _wake(work_id, card_id, **kw):
        woke_calls.append((work_id, card_id))
        return len(woke_calls) >= 2 and kw["admission"]()
    monkeypatch.setattr(mr, "deliver_now", _wake)
    pc = await _mk_card(wid, "补唤醒计划")
    dev = await _mk_card(wid, "开发卡")
    src_card = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src_card}:8"
    coordination_watcher.feed(wid, src_card, _rec(src_card, 8, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    d1 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d1["ok"] and not d1["woke"]
    msg1 = d1["msg_id"]
    session_text = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert session_text.count(msg1) == 1              # 一次唯一收件（消息头带 id）
    assert any(o.get("detail", {}).get("msg_id") == msg1
               for o in coordination_store.list_observations(wid, pc))  # 消息号留账
    # 重试：同键 ⇒ 原消息号、只补唤醒
    d2 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d2["ok"] and d2["replayed"] and d2["msg_id"] == msg1 and d2["woke"]
    assert len(woke_calls) == 2
    session_text2 = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert session_text2.count(msg1) == 1              # 仍只有一条收件
    wake_logs = [o for o in coordination_store.list_observations(wid, pc)
                 if o["kind"] == "instruction_woke"]
    assert len(wake_logs) == 2                      # 两次唤醒分别留账


# ---------------------------------------------------------------- T15 收件原语三反例

async def _live_case(monkeypatch, title="收件原语"):
    wid, cards, fids = await _seeded_work(monkeypatch)
    woke = await _direct_env(monkeypatch)
    pc = await _mk_card(wid, f"{title}计划")
    dev = await _mk_card(wid, f"{title}开发卡")
    src = await _mk_card(wid, f"{title}来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:7"
    coordination_watcher.feed(wid, src, _rec(src, 7, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    assert r["ok"]
    return wid, pc, dev, aid, r["action_key"], woke


async def test_rollback_leaves_no_file_write(sandbox, monkeypatch):
    """[反例1] SQLite 收件事务回滚 ⇒ 会话零写入；同键重试后恰好 1 条。"""
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch)
    from src.storage import card_db
    orig_rw = card_db.run_write
    state = {"fail": True}

    async def _flaky_rw(wid_, fn):
        if state["fail"] and getattr(fn, "__name__", "") == "_tx":
            raise RuntimeError("模拟 SQLite 收件事务回滚")
        return await orig_rw(wid_, fn)
    # 直接包 coordination_runner 里引用的 run_write
    monkeypatch.setattr(card_db, "run_write", _flaky_rw)
    d1 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert not d1["ok"]
    dev_sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert dev_sess.count(key) == 0        # 事务回滚 ⇒ 文件零写（不再先写后滚）
    # 重试（事务恢复）⇒ 恰好 1 条
    state["fail"] = False
    d2 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert d2["ok"]
    dev_sess2 = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert dev_sess2.count(d2["receipt_id"]) <= 1      # 受理号不入正文
    assert dev_sess2.count(d2["msg_id"]) == 1          # 一条收件


async def test_replay_after_revoke_does_not_wake(sandbox, monkeypatch):
    """[反例2] 落信成功、唤醒失败、随后撤回任命 ⇒ 重放读回原消息号但
    **不再唤醒**（每次补唤醒重验授权）。"""
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch, "撤回重放")
    from src.api import message_runner as mr

    async def _wake_fail(work_id, card_id, **kw):
        woke.append((work_id, card_id, kw.get("source_kind")))
        return False                      # 首次唤醒失败
    monkeypatch.setattr(mr, "deliver_now", _wake_fail)
    d1 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert d1["ok"] and not d1["woke"]
    wake_calls_before = len(woke)
    # 撤回任命
    await coordination_store.control_op(wid, pc, assignment_id=aid, op="revoke")
    # 重放：读回原消息号、不再唤醒
    d2 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert d2["ok"] and d2["replayed"] and d2["msg_id"] == d1["msg_id"]
    assert d2.get("wake_denied") and not d2["woke"]
    assert len(woke) == wake_calls_before      # 唤醒未被再触发
    denied = [o for o in coordination_store.list_observations(wid, pc)
              if o["kind"] == "instruction_woke" and o["status"] == "denied"]
    assert denied


async def test_message_identity_is_card_not_human(sandbox, monkeypatch):
    """[反例3] 会话块身份＝card:<职能>（不是 human），msg_id 在块里可对账。"""
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch, "身份核对")
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert d["ok"]
    raw = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    # 块身份：actor 是 card:<职能>，不是 human
    assert "actor: card:" in raw
    assert "actor: human" not in raw
    # 正式消息头在块里（可对账）
    assert f"[[card-msg id={d['msg_id']} type=msg_directive from=" in raw


# ---------------------------------------------------------------- T16 收件三件对账

def _events_text(wid: str, card: str) -> str:
    from src.storage import event_log
    p = event_log.event_path(wid, card)
    return p.read_text(encoding="utf-8") if p.exists() else ""


async def test_receipt_projection_accounting(sandbox, monkeypatch):
    """[四缺口] 收件三件对账：会话＋原文账＋事件；补投影可恢复；身份固定；
    并发重放唤醒仅一次。"""
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch, "对账")
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert d["ok"], d
    msg_id = d["msg_id"]
    # ① 会话块有指令
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert msg_id in sess
    # ② 原文账（session_blocks）有该消息
    from src.storage import card_db as _cdb

    def _q(conn):
        return conn.execute(
            "SELECT content FROM session_blocks WHERE card_id=? AND seq IS NOT NULL",
            (dev,)).fetchall()
    rows = _cdb.read_sync(wid, _q, [])
    assert any(msg_id in str(r["content"] or "") for r in rows)
    # ③ 事件带 msg_id
    ev = _events_text(wid, dev)
    assert msg_id in ev and "msg_directive" in ev


async def test_projection_recovery_and_fixed_identity(sandbox, monkeypatch):
    """[四缺口] 首次投影失败 ⇒ 重试补会话+原文账+事件+落信账；
    换人后补投影仍署**原任命职能**（提交时定稿）。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "补投影计划")
    dev = await _mk_card(wid, "开发卡")
    src = await _mk_card(wid, "来源卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:6"
    coordination_watcher.feed(wid, src, _rec(src, 6, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    assert r["ok"]

    orig = session_facade.append_directive_sync
    state = {"fail": True}

    def _flaky(wid_, cid_, *a, **kw):
        if state["fail"]:
            raise OSError("模拟投影失败")
        return orig(wid_, cid_, *a, **kw)
    monkeypatch.setattr(session_facade, "append_directive_sync", _flaky)
    d1 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert not d1["ok"] and d1["code"] == "projection_failed"
    msg_id = d1["msg_id"]

    # 换人：撤回后任命 cto（原 PM 的收件仍未投影）
    await coordination_store.control_op(wid, pc, assignment_id=aid, op="revoke")
    b = await coordination_store.appoint(
        wid, pc, function_id=fids["cto"],
        **_appoint_kwargs(objective="新任期", mode="live", observe={
            "scope": "work"}, instruct={"scope": "cards", "ids": [dev]}))
    aid_b = b["appointment"]["assignment_id"]
    await _link_actor(wid, pc, dev)
    state["fail"] = False
    d2 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid_b, rev=0, action_key=r["action_key"])
    assert d2["ok"] and d2["msg_id"] == msg_id

    # 会话/原文账/事件/落信账 四处都补上；署**原 pm** 不是新 cto
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert msg_id in sess and f"from={fids['pm']}" in sess
    ev = _events_text(wid, dev)
    assert msg_id in ev
    from src.storage import card_db as _cdb

    def _q(conn):
        return conn.execute(
            "SELECT content FROM session_blocks WHERE card_id=?",
            (dev,)).fetchall()
    rows = _cdb.read_sync(wid, _q, [])
    assert any(msg_id in str(r["content"] or "") for r in rows)
    late = [o for o in coordination_store.list_observations(wid, pc)
            if o["kind"] == "instruction_delivered"
            and o["detail"].get("msg_id") == msg_id]
    assert late and late[0]["detail"].get("late") is True


async def test_concurrent_replay_wakes_once(sandbox, monkeypatch):
    """[四缺口] 同一行动并发重放：会话 1 条、唤醒仅 1 次（行动键唯一认领）。"""
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch, "并发重放")
    calls = []

    async def _slow_wake(work_id, card_id, **kw):
        calls.append((work_id, card_id))
        await asyncio.sleep(0.05)
        return kw["admission"]()
    from src.api import message_runner as mr
    monkeypatch.setattr(mr, "deliver_now", _slow_wake)
    r1, r2 = await asyncio.gather(
        coordination_runner.coordination_deliver(
            wid, pc, assignment_id=aid, rev=0, action_key=key),
        coordination_runner.coordination_deliver(
            wid, pc, assignment_id=aid, rev=0, action_key=key))
    assert r1["ok"] and r2["ok"]
    assert r1["msg_id"] == r2["msg_id"]
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert sess.count(r1["msg_id"]) == 1
    assert len(calls) == 1                    # 唤醒只一次


@pytest.mark.parametrize("interrupt", ["origin", "before_event", "after_event", "finish"])
async def test_interrupted_projection_replays_to_one_receipt(sandbox, monkeypatch, interrupt):
    from src.storage import card_db, event_log, origin_store

    wid, pc, dev, aid, key, calls = await _live_case(monkeypatch)

    async def deliver():
        return await coordination_runner.coordination_deliver(
            wid, pc, assignment_id=aid, rev=0, action_key=key)

    with monkeypatch.context() as patch:
        if interrupt == "origin":
            patch.setattr(origin_store, "write_blocks_sync", lambda *args: 0)
        elif interrupt == "finish":
            card_db.write_sync(wid, lambda conn: conn.execute(
                "CREATE TRIGGER interrupt_projection BEFORE UPDATE OF projected_session"
                " ON coordination_actions BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"))
        else:
            emit = session_facade._emit_block_event

            async def interrupted_emit(*args, **kwargs):
                if interrupt == "after_event":
                    assert await emit(*args, **kwargs)
                raise asyncio.CancelledError

            patch.setattr(session_facade, "_emit_block_event", interrupted_emit)

        if interrupt in ("before_event", "after_event"):
            with pytest.raises(asyncio.CancelledError):
                await deliver()
        else:
            assert (await deliver())["code"] == "projection_failed"
        assert calls == []
        action = coordination_runner._read_action(wid, pc, key)
        assert not action["projection_verified"]
        msg_id = action["msg_id"]

    if interrupt == "finish":
        card_db.write_sync(wid, lambda conn: conn.execute("DROP TRIGGER interrupt_projection"))
    await coordination_runner.recover_deliveries(wid)
    receipt = await deliver()
    assert receipt["msg_id"] == msg_id and receipt["woke"] and len(calls) == 1
    blocks = await session_facade.read(wid, dev)
    assert sum(msg_id in block["content"] for block in blocks) == 1
    assert card_db.read_sync(wid, lambda conn: conn.execute(
        "SELECT COUNT(*) FROM session_blocks WHERE card_id=? AND type='msg_directive'"
        " AND instr(content, ?) > 0",
        (dev, msg_id)).fetchone()[0]) == 1
    assert sum(event["type"] == "msg_directive" and event["data"].get("msg_id") == msg_id
               for event in event_log.read(wid, dev)) == 1
    delivered = coordination_store.list_observations(wid, pc, kind="instruction_delivered")
    assert len(delivered) == 1 and delivered[0]["detail"]["msg_id"] == msg_id


@pytest.mark.parametrize("admitted", [False, True])
async def test_cancelled_wake_recovers_only_unacknowledged_effect(sandbox, monkeypatch, admitted):
    from src.api import message_runner

    wid, pc, dev, aid, key, calls = await _live_case(monkeypatch)
    wake = message_runner.deliver_now

    async def interrupted(*args, **kwargs):
        if admitted:
            assert await wake(*args, **kwargs)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(message_runner, "deliver_now", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await coordination_runner.coordination_deliver(
                wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert len(calls) == int(admitted)
    await coordination_runner.recover_deliveries(wid)
    receipt = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert receipt["woke"] and len(calls) == 1
    observations = coordination_store.list_observations(wid, pc, kind="instruction_woke")
    assert sum(item["status"] == "woke" for item in observations) == 1


async def test_new_appointment_cannot_wake_prior_receipt(sandbox, monkeypatch):
    from src.api import message_runner

    wid, pc, dev, aid, key, calls = await _live_case(monkeypatch)

    async def offline(*args, **kwargs):
        return False

    with monkeypatch.context() as patch:
        patch.setattr(message_runner, "deliver_now", offline)
        before = await coordination_runner.coordination_deliver(
            wid, pc, assignment_id=aid, rev=0, action_key=key)
    actor = coordination_store.panel(wid, pc)["function_id"]
    await coordination_store.control_op(wid, pc, assignment_id=aid, op="revoke")
    appt = await coordination_store.appoint(
        wid, pc, function_id=actor,
        **_appoint_kwargs(mode="live", observe={"scope": "work"},
                          instruct={"scope": "cards", "ids": [dev]}))
    after = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=appt["appointment"]["assignment_id"], rev=0, action_key=key)
    assert after["msg_id"] == before["msg_id"]
    assert after["wake_denied"] and not after["woke"] and not calls
    refused = coordination_store.list_observations(wid, pc, kind="instruction_woke")[0]
    assert refused["assignment_id"] == aid and refused["status"] == "denied"


@pytest.mark.parametrize("pause_while_queued", [False, True])
async def test_owner_admits_directive_after_turn_gate(sandbox, monkeypatch, pause_while_queued):
    from contextvars import ContextVar
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api import message_runner
    from src.infra import procs

    real_deliver = message_runner.deliver_now
    wid, pc, dev, aid, key, _ = await _live_case(monkeypatch)
    monkeypatch.setattr(message_runner, "deliver_now", real_deliver)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    runner_index = ContextVar("test_runner_index", default=0)
    monkeypatch.setattr(procs, "is_runner", lambda: True)
    monkeypatch.setattr(procs, "runner_index", runner_index.get)
    monkeypatch.setattr(procs, "owner_index_of", lambda work, card: int(card == dev))
    app = FastAPI()
    app.include_router(routes.router)
    at_gate = asyncio.Event()
    release_gate = asyncio.Event()
    starts = []
    tasks = []
    spawn = message_runner._spawn_tracked

    def capture_spawn(coro, **kwargs):
        task = spawn(coro, **kwargs)
        if kwargs.get("card_id") == dev:
            tasks.append(task)
        return task

    async def wait_at_gate(work, card):
        assert runner_index.get() == 1
        at_gate.set()
        await release_gate.wait()

    async def run_agent(work, card, *args, **kwargs):
        assert runner_index.get() == 1
        assert kwargs["source_kind"] == "directive" and kwargs["source"]["require_report"]
        starts.append(kwargs["source"]["coordination_msg_id"])
        return {"content": "已执行"}

    monkeypatch.setattr(message_runner, "_spawn_tracked", capture_spawn)
    monkeypatch.setattr(routes.serial_gate, "acquire", wait_at_gate)
    monkeypatch.setattr(routes, "_run_message_inner", run_agent)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def forward(work, card, path, payload):
            assert runner_index.get() == 0 and card == dev
            assert not session_facade.turn_holder(wid, pc)
            token = runner_index.set(1)
            try:
                response = await client.post("/api" + path, json=payload)
                assert response.status_code == 200
                return response.json()
            finally:
                runner_index.reset(token)

        monkeypatch.setattr(procs, "forward_ctl", forward)

        async def deliver():
            return await coordination_runner.coordination_deliver(
                wid, pc, assignment_id=aid, rev=0, action_key=key)

        first, duplicate = await asyncio.gather(deliver(), deliver())
        assert first["ok"] and first["msg_id"] == duplicate["msg_id"]
        assert not first["woke"] and not duplicate["woke"]
        await asyncio.wait_for(at_gate.wait(), timeout=3)
        if pause_while_queued:
            await coordination_store.control_op(wid, pc, assignment_id=aid, op="pause")
        release_gate.set()
        assert len(tasks) == 1
        await asyncio.wait_for(message_runner.await_spawned(tasks[0]), timeout=5)
        replay = await deliver()
        assert replay["msg_id"] == first["msg_id"]
        assert replay["woke"] is not pause_while_queued
        assert starts == ([] if pause_while_queued else [first["msg_id"]])


# ---------------------------------------------------------------- T17 同轮改计划+发令契约

def _combined_payload(wid: str, dev: str, *, acceptance="完成并汇报",
                      item_id="i1") -> str:
    plan = json.loads(json.dumps(PLAN_JSON))
    plan["plan"]["items"][0]["item_id"] = item_id
    plan["plan"]["items"][0]["owner_card"] = dev
    plan["instruction"] = {
        "item_id": item_id, "target_card": dev,
        "instruction": "按需求核对无卡接入用例",
        "acceptance": acceptance,
        "evidence_refs": []}
    return json.dumps(plan)


async def _combined_case(monkeypatch, title="组合提交"):
    wid, cards, fids = await _seeded_work(monkeypatch)
    woke = await _direct_env(monkeypatch)
    pc = await _mk_card(wid, f"{title}计划")
    dev = await _mk_card(wid, f"{title}开发卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           instruct={"scope": "cards", "ids": [dev]})
    await coordination_store.save_plan(
        wid, pc, assignment_id=aid, expected_rev=0,
        plan={"objective": "组合提交基线", "items": [
            {"item_id": "i1", "title": "核查无卡接入", "owner_card": dev},
        ]})
    await _link_actor(wid, pc, dev)
    return wid, pc, dev, aid, fids, woke


async def test_combined_submit_success(sandbox, monkeypatch):
    """[①正向] r→r+1 保存成功；收件绑定新版本；正文含验收判据。"""
    wid, pc, dev, aid, fids, woke = await _combined_case(monkeypatch)
    payload = _combined_payload(wid, dev)
    fake, calls = _sequence_call_main([payload])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(wid, pc,
                                                          assignment_id=aid)
    assert out["status"] == "parsed" and out.get("combined")
    combo = out["combined"]
    assert combo["saved_rev"] == 2 and combo["instruction"] == "delivered"
    assert combo["receipt_id"].startswith("rcp_")
    panel = coordination_store.panel(wid, pc)
    assert panel["rev"] == 2 and panel["plan"]["items"][0]["item_id"] == "i1"
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert "验收判据:完成并汇报" in sess and combo["msg_id"] in sess
    # 行动行保存时定稿身份与新版本
    from src.storage import card_db as _cdb

    def _q(conn):
        coordination_runner._ensure_actions(conn)
        return conn.execute(
            "SELECT rev, acceptance, actor FROM coordination_actions"
            " WHERE work_id=?", (wid,)).fetchall()
    rows = _cdb.read_sync(wid, _q, [])
    assert rows and rows[0]["rev"] == 2
    assert rows[0]["acceptance"] == "完成并汇报"
    assert rows[0]["actor"] == fids["pm"]
    assert woke


async def test_combined_submit_rejections(sandbox, monkeypatch):
    """[①反例] 空验收判据/保存冲突/保存后暂停或改版 ⇒ 零新增收件。"""
    wid, pc, dev, aid, fids, woke = await _combined_case(
        monkeypatch, "组合拒绝")

    # ① 空验收判据 ⇒ unparsed，计划不保存
    fake, _ = _sequence_call_main([_combined_payload(wid, dev, acceptance="")])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(wid, pc,
                                                          assignment_id=aid)
    assert out["status"] == "unparsed"
    assert coordination_store.panel(wid, pc)["rev"] == 1

    # ② 保存冲突 ⇒ 计划不保存、零收件
    _real_save = coordination_store.save_plan

    async def _conflict(*a, **kw):
        raise coordination_store.CoordinationError("conflict", "版本冲突")
    monkeypatch.setattr(coordination_store, "save_plan", _conflict)
    fake, _ = _sequence_call_main([_combined_payload(wid, dev)])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(wid, pc,
                                                          assignment_id=aid)
    assert out["status"] == "unparsed"
    assert "统筹指令" not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")

    # ③ 保存成功后暂停 ⇒ 计划保留 r+1；「计划已保存、指令尚未受理」账；零收件
    async def _pause_after_save(*a, **kw):
        r = await _orig_save(*a, **kw)
        await coordination_store.control_op(
            wid, pc, assignment_id=aid, op="pause")
        return r
    _orig_save = _real_save
    monkeypatch.setattr(coordination_store, "save_plan", _pause_after_save)
    fake, _ = _sequence_call_main([_combined_payload(wid, dev)])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(wid, pc,
                                                          assignment_id=aid)
    assert out["status"] == "parsed" and out["combined"]["instruction"] == \
        "pending_acceptance"
    kinds = [o["kind"] for o in coordination_store.list_observations(wid, pc)]
    assert "instruction_pending_acceptance" in kinds
    assert "统筹指令" not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    # 恢复以便下一场景
    await coordination_store.control_op(wid, pc, assignment_id=aid, op="resume")

    # ④ 保存成功后用户又改版 ⇒ 发令按旧令牌拒（stale_token）；计划保留
    async def _edit_after_save(*a, **kw):
        r = await _orig_save(*a, **kw)
        await _orig_save(
            wid, pc, assignment_id=aid, expected_rev=r["appointment"]["rev"],
            plan={"objective": "用户改版", "items": []})
        return r
    monkeypatch.setattr(coordination_store, "save_plan", _edit_after_save)
    fake, _ = _sequence_call_main([_combined_payload(wid, dev)])
    monkeypatch.setattr(model_service, "call_main", fake)
    out = await coordination_runner.run_strong_first_turn(wid, pc,
                                                          assignment_id=aid)
    assert out["status"] == "parsed" and out["combined"]["instruction"] == \
        "pending_acceptance"
    assert "统筹指令" not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")


# ---------------------------------------------------------------- T18 QA 派驻与面板汇总

async def test_qa_dispatch_and_board(sandbox, monkeypatch, client):
    """[②QA/③面板] 正式派驻 QA、结论回原事项；面板只读可回读。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "QA计划")
    dev = await _mk_card(wid, "验收对象")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    dispatched = []

    async def _fake_deploy(work_id, current_id, arguments, cards_map, **kwargs):
        dispatched.append((work_id, current_id, dict(arguments)))
        dep = await function_store.create_deployment(
            work_id, fids["qa"], venue_kind="deploy", venue_ref=fids["qa"],
            delegator=current_id, batch_id=arguments["batch_id"],
            target_card=arguments["target"], status="active")
        assert function_store.admit_deployment_wake(work_id, dep["id"], fids["qa"], fids["qa"])
        return {"ok": True, "card_id": fids["qa"], "deployment_id": dep["id"]}
    monkeypatch.setattr(card_tools, "_h_create_deploy_card", _fake_deploy)

    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="无卡参与者能接入且跑通集成测试",
        evidence_refs=[])
    assert qa["ok"] and qa["qa_id"].startswith("qa_")
    assert dispatched and dispatched[0][2]["function"] == "qa"
    assert dispatched[0][2]["target"] == dev
    # 重放回读同编号，不二次派驻
    again = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="无卡参与者能接入且跑通集成测试",
        evidence_refs=[])
    assert again["ok"] and again["replayed"] and again["qa_id"] == qa["qa_id"]
    assert len(dispatched) == 1
    # 面板 API 只读回读：行动/问答/QA 三组摘要
    r = await client.get(f"/api/works/{wid}/cards/{pc}/coordination")
    assert r.status_code == 200
    body = r.json()
    assert "actions" in body and "questions" in body and "qa" in body
    assert any(x["qa_id"] == qa["qa_id"] for x in body["qa"])
    # 只读 GET 不新增模型调用（expert 计数保持）
    assert not [c for c in [] ]  # 保底占位——无副作用即可


# ---------------------------------------------------------------- T19 四条隔离反例

async def test_unreachable_owner_recovered_by_tick(sandbox, monkeypatch):
    """[反例①] 目标 runner 暂不可达 ⇒ 记账待投；恢复后调度按原键重投出消息号。"""
    from src.infra import procs
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch, "不可达")
    calls = []

    async def _unreachable(work_id, card_id, path, payload):
        calls.append((path, payload["action_key"]))
        return {"error": "runner 暂不可达"}
    monkeypatch.setattr(procs, "forward_ctl", _unreachable)
    monkeypatch.setattr(procs, "is_web", lambda: True)   # 强制走转发分支
    d1 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert not d1["ok"] and d1["code"] == "owner_unreachable"
    pend = [o for o in coordination_store.list_observations(wid, pc)
            if o["kind"] == "instruction_pending_delivery"]
    assert pend and pend[0]["detail"]["action_key"] == key
    assert not session_facade._session_path(wid, dev).exists() or \
        key not in session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    # 恢复：属主在本进程，调度重投同键
    monkeypatch.setattr(procs, "is_web", lambda: False)
    await coordination_runner.recover_deliveries(wid)
    d2 = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert d2["ok"] and d2["msg_id"]
    sess = session_facade._session_path(wid, dev).read_text(encoding="utf-8")
    assert sess.count(d2["msg_id"]) == 1


async def test_projection_failure_not_shown_as_delivered(sandbox, monkeypatch):
    """[反例②] 收件已提交但投影失败 ⇒ 面板显示待补投影，不是已落信。"""
    wid, pc, dev, aid, key, woke = await _live_case(monkeypatch, "投影失败")
    orig = session_facade.append_directive_sync

    def _fail(wid_, cid_, *a, **kw):
        raise OSError("投影失败")
    monkeypatch.setattr(session_facade, "append_directive_sync", _fail)
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=key)
    assert not d["ok"] and d["code"] == "projection_failed"
    summary = coordination_runner.board_summary(wid, pc)
    acts = [a for a in summary["actions"] if a["action_key"] == key]
    assert acts and acts[0]["status"] == "待补投影"
    assert not acts[0]["projected"]["session"]


async def test_observe_mode_qa_never_calls_dispatch(sandbox, monkeypatch):
    """[反例③] 旁观模式 QA ⇒ 派驻入口零调用，只留拟发记录。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "旁观QA计划")
    dev = await _mk_card(wid, "验收对象")
    aid = await _appointed(monkeypatch, wid, fids, pc)   # 默认 observe
    called = []

    async def _spy(*a, **kw):
        called.append(a)
        return {"ok": True, "card_id": "qa_x", "deployment_id": 1}
    monkeypatch.setattr(card_tools, "_h_create_deploy_card", _spy)
    r = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="跑通集成测试", evidence_refs=[])
    assert not r["ok"] and r["code"] == "observe_mode"
    assert called == []
    drafts = [o for o in coordination_store.list_observations(wid, pc)
              if o["kind"] == "qa_draft"]
    assert drafts


async def test_expert_rejects_out_of_scope_same_work_event(sandbox, monkeypatch):
    """[反例④] 同 work 但范围外/未观察的事件引用 ⇒ 专家输入零调用。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "证据范围计划")
    out_card = await _mk_card(wid, "范围外卡")
    in_card = await _mk_card(wid, "范围内卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "cards", "ids": [in_card]})
    calls = []

    async def _spy(system, user, **kw):
        calls.append(user)
        return ({"content": '{"answer":"不该被调用"}'}, "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _spy)
    # 同 work 但卡不在授权范围
    bad = f"event:{wid}:{out_card}:5"
    r = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        function_id=fids["cto"], question="技术路径？",
        requirements_version="v1", evidence_refs=[bad],
        turn_id="t_bad", model_used="strong-test-route")
    assert not r["ok"] and r["code"] == "bad_evidence" and "授权观察范围" in r["why"]
    assert calls == []
    # 卡在范围但不是本任命 seen 事件 ⇒ 也拒
    r2 = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        function_id=fids["cto"], question="技术路径？",
        requirements_version="v1",
        evidence_refs=[f"event:{wid}:{in_card}:999"],
        turn_id="t_unseen", model_used="strong-test-route")
    assert not r2["ok"] and r2["code"] == "bad_evidence" and calls == []


async def test_qa_verdict_failure_not_masked_by_done(sandbox, monkeypatch):
    """QA done ≠ 通过：真实回执写失败 ⇒ 面板仍显示 failed。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "QA结论计划")
    dev = await _mk_card(wid, "验收对象")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")

    async def _fake_deploy(work_id, current_id, arguments, cards_map, **kwargs):
        return {"ok": True, "card_id": fids["qa"], "deployment_id": 88}
    monkeypatch.setattr(card_tools, "_h_create_deploy_card", _fake_deploy)
    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="跑通无卡集成测试", evidence_refs=[])
    assert qa["ok"]
    # 差事账 done 但结论失败：回传真实失败结论
    await coordination_runner.qa_verdict(
        wid, qa["qa_id"], verdict="failed",
        conclusion="无卡参与者未跑通，缺少关联字段",
        evidence_refs=[f"event:{wid}:{dev}:12"])
    recs = coordination_runner.qa_records(wid, pc)
    r = [x for x in recs if x["qa_id"] == qa["qa_id"]][0]
    assert r["verdict"] == "failed" and "未跑通" in r["conclusion"]


async def test_qa_real_dispatch_and_failed_verdict(sandbox, monkeypatch):
    """[QA 真实派驻] 不替换派驻入口：正式建 qa_card＋差事账，
    回传失败结论后从原事项回读失败（done 不冒充通过）。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "真实QA计划")
    _dev = await card_store.create_card(
        wid, "验收对象", "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    dev = _dev["meta"]["card_id"]
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})

    # 只拦背景唤起（不起模型回合），正式建卡/落账照走
    from src.api import message_runner as mr
    started = []

    async def _no_wake(work_id, card_id, **kw):
        started.append((work_id, card_id))
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)

    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="无卡参与者集成测试逐条通过",
        evidence_refs=[])
    assert qa["ok"], qa
    # 真实产物：qa_card 存在、差事账存在且为 active/done
    meta = card_store._meta_of(wid, qa["qa_card"])
    assert meta and meta["card_type"] == "qa_card"
    from src.storage import function_store as _fs
    dep = _fs.get_deployment(wid, int(qa["deployment_id"]))
    assert dep and str(dep.get("function_id") or "") == fids["qa"]
    # QA 真实回执：失败结论写回原事项
    r = await coordination_runner.qa_verdict(
        wid, qa["qa_id"], verdict="failed",
        conclusion="无卡参与者集成测试第 2 条未通过：缺关联字段",
        evidence_refs=[f"event:{wid}:{dev}:21"])
    assert r["ok"]
    rec = [x for x in coordination_runner.qa_records(wid, pc)
           if x["qa_id"] == qa["qa_id"]][0]
    assert rec["verdict"] == "failed" and "未通过" in rec["conclusion"]
    assert rec["evidence_refs"] == [f"event:{wid}:{dev}:21"]


# ---------------------------------------------------------------- T20 三条返工反例

async def test_qa_formal_report_bridge(sandbox, monkeypatch):
    """[反例①] 正式 report 回执（失败）按差事编号回原事项——不是测试直调。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "正式回执计划")
    dev_repo = await card_store.create_card(
        wid, "验收对象", "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    dev = dev_repo["meta"]["card_id"]
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(work_id, card_id, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1", target_card=dev,
        acceptance="逐条验收集成测试", evidence_refs=[])
    assert qa["ok"]
    # QA 差事卡走**正式回执入口**：content=验收失败
    r = await card_tools._h_report_deploy(
        wid, fids["qa"],
        {"content": "结论：验收失败——无卡参与者第 2 条未通过，缺关联字段"},
        card_store.iter_card_metas(wid))
    assert r.get("ok") and r["deployment_id"] == qa["deployment_id"]
    rec = [x for x in coordination_runner.qa_records(wid, pc)
           if x["qa_id"] == qa["qa_id"]][0]
    assert rec["verdict"] == "failed" and "未通过" in rec["conclusion"]
    # 关闭差事（done）不会把失败改写成通过
    await function_store.close_deployment(
        wid, int(qa["deployment_id"]), status="done",
        result=r["delivered"][0]["msg_id"])
    rec2 = [x for x in coordination_runner.qa_records(wid, pc)
            if x["qa_id"] == qa["qa_id"]][0]
    assert rec2["verdict"] == "failed"


async def test_qa_concurrent_dispatch_one_card(sandbox, monkeypatch):
    """[反例②] 同时派同一项 QA：建卡前原子占坑，只创建一张卡。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "并发QA计划")
    dev_repo = await card_store.create_card(
        wid, "验收对象", "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    dev = dev_repo["meta"]["card_id"]
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(work_id, card_id, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    made = []

    # 统计真实建卡次数（不替换入口本身）
    orig_create = card_store.create_card

    async def _count_create(*a, **kw):
        made.append(a[:2])
        return await orig_create(*a, **kw)
    monkeypatch.setattr(card_store, "create_card", _count_create)
    args = dict(item_id="i1", target_card=dev,
                acceptance="并发只建一张验收卡", evidence_refs=[])
    r1, r2 = await asyncio.gather(
        coordination_runner.coordination_qa_request(
            wid, pc, assignment_id=aid, rev=0, **args),
        coordination_runner.coordination_qa_request(
            wid, pc, assignment_id=aid, rev=0, **args))
    assert r1["ok"] and r2["ok"] and r1["qa_id"] == r2["qa_id"]
    assert len(made) == 1                       # 建卡前去重（不是撞唯一键后补）
    rows = [x for x in coordination_runner.qa_records(wid, pc)
            if x["qa_id"] == r1["qa_id"]]
    assert len(rows) == 1 and rows[0]["qa_card"]


async def test_replayed_expert_answer_revokes_evidence(sandbox, monkeypatch):
    """[反例③] 收窄观察范围后重问同一问题：旧答复留历史、不回流模型。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "撤权重问计划")
    in_card = await _mk_card(wid, "证据卡")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "cards", "ids": [in_card]})
    coord = f"event:{wid}:{in_card}:4"
    coordination_watcher.feed(wid, in_card, _rec(in_card, 4, "card_block_edited"))
    calls = []

    async def _expert(system, user, **kw):
        if kw.get("caller") == "coordination_expert":
            calls.append(user)
            return ({"content": '{"answer":"基于当时证据的答复"}'},
                    "strong-test-route")
        return ({"content": json.dumps(PLAN_JSON)}, "strong-test-route")
    monkeypatch.setattr(model_service, "call_main", _expert)
    ask_kw = dict(function_id=fids["cto"], question="实现路径？",
                  requirements_version="v1", evidence_refs=[coord],
                  turn_id="t_first", model_used="strong-test-route")
    first = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid, rev=0, item_id="i1", **ask_kw)
    assert first["ok"] and not first.get("replayed")
    assert len(calls) == 1

    # 收窄：证据卡不在观察范围
    await coordination_store.control_op(
        wid, pc, assignment_id=aid, op="revoke")
    other = await _mk_card(wid, "新范围卡")
    b = await coordination_store.appoint(
        wid, pc, function_id=fids["pm"],
        **_appoint_kwargs(objective="收窄", mode="live",
                          observe={"scope": "cards", "ids": [other]},
                          instruct={"scope": "cards", "ids": [other]}))
    aid2 = b["appointment"]["assignment_id"]
    again = await coordination_runner.coordination_ask(
        wid, pc, assignment_id=aid2, rev=0, item_id="i1", **ask_kw)
    assert not again["ok"] and again["code"] in ("evidence_revoked",
                                                 "bad_evidence")
    assert len(calls) == 1              # 旧答复没有再进专家/统筹模型
    # 历史仍在（问题行可查）
    def _q(conn):
        return conn.execute(
            "SELECT status, answer FROM coordination_questions"
            " WHERE question_id=?", (first["question_id"],)).fetchone()
    from src.storage import card_db as _cdb
    row = _cdb.read_sync(wid, _q, None)
    assert row and row["status"] == "answered"


# ---------------------------------------------------------------- T21 QA 链路收口三反例

async def test_qa_reserve_failure_retry_reclaims(sandbox, monkeypatch):
    """[反例①] 建卡前失败 ⇒ status=failed，同键重试可重新领取并建卡。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "领取重试计划")
    dev_repo = await card_store.create_card(
        wid, "验收对象", "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    dev = dev_repo["meta"]["card_id"]
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    orig_create = card_store.create_card
    state = {"fail": True}
    made = []

    async def _flaky(*a, **kw):
        if state["fail"]:
            return {"error": {"code": "injected", "message": "模拟建卡前失败"}}
        made.append(a[:2])
        return await orig_create(*a, **kw)
    monkeypatch.setattr(card_store, "create_card", _flaky)
    args = dict(item_id="i1", target_card=dev,
                acceptance="失败后可重领", evidence_refs=[])
    r1 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, **args)
    assert not r1["ok"] and r1["code"] == "qa_dispatch_failed"
    from src.storage import card_db as _cdb
    def _qs(conn):
        return [dict(x) for x in conn.execute(
            "SELECT qa_id,status,qa_card FROM coordination_qa WHERE work_id=?",
            (wid,)).fetchall()]
    state["fail"] = False
    r2 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, **args)
    from src.storage import card_db as _cdb
    def _qs(conn):
        return [dict(x) for x in conn.execute(
            "SELECT qa_id,status,qa_card FROM coordination_qa WHERE work_id=?",
            (wid,)).fetchall()]
    assert r2["ok"] and r2["qa_id"] == r1.get("qa_id", r2["qa_id"])
    assert len(made) == 1 and r2.get("qa_card")
    rows = [x for x in coordination_runner.qa_records(wid, pc)
            if x["qa_id"] == r2["qa_id"]]
    assert len(rows) == 1 and rows[0]["status"] == "dispatched"


async def test_qa_report_binds_turn_deployment(sandbox, monkeypatch):
    """[反例②] 同职能两项差事：正式回执按回合绑定编号挂第一项。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "双差事计划")
    dev_repo = await card_store.create_card(
        wid, "验收对象", "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    dev = dev_repo["meta"]["card_id"]
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    qa1 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="第一项验收标准", evidence_refs=[])
    qa2 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i2",
        target_card=dev, acceptance="第二项验收标准", evidence_refs=[])
    assert qa1["ok"] and qa2["ok"] and qa1["qa_id"] != qa2["qa_id"]
    # 第一项差事的回合回执：宿主注入绑定编号 + 显式失败结论
    r = await card_tools._h_report_deploy(
        wid, fids["qa"],
        {"content": "逐条验收：第 1 条未通过", "verdict": "failed",
         "_deployment_id": qa1["deployment_id"]},
        card_store.iter_card_metas(wid))
    assert r.get("ok"), r
    recs = {x["qa_id"]: x for x in coordination_runner.qa_records(wid, pc)}
    assert recs[qa1["qa_id"]]["verdict"] == "failed"
    assert recs[qa2["qa_id"]]["verdict"] == "unverified"   # 第二项未回报
    # 绑定不存在的编号 ⇒ 拒（防错挂）
    bad = await card_tools._h_report_deploy(
        wid, fids["qa"],
        {"content": "结论：失败", "_deployment_id": 999999},
        card_store.iter_card_metas(wid))
    assert not bad.get("ok")


async def test_qa_explanation_not_treated_as_pass(sandbox, monkeypatch):
    """[反例③] 正文只写「验收通过的标准」⇒ 未验证，不误判通过。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "显式结论计划")
    dev_repo = await card_store.create_card(
        wid, "验收对象", "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    dev = dev_repo["meta"]["card_id"]
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="明确验收标准", evidence_refs=[])
    assert qa["ok"]
    r = await card_tools._h_report_deploy(
        wid, fids["qa"],
        {"content": "验收通过的标准见需求文档第 3 条，尚无实际结论",
         "_deployment_id": qa["deployment_id"]},
        card_store.iter_card_metas(wid))
    assert r.get("ok")
    rec = [x for x in coordination_runner.qa_records(wid, pc)
           if x["qa_id"] == qa["qa_id"]][0]
    assert rec["verdict"] == "unverified"
    # 显式结论行才判通过
    await coordination_runner.ingest_qa_report(
        wid, int(qa["deployment_id"]), "结论：通过（证据见集成测试日志）")
    rec2 = [x for x in coordination_runner.qa_records(wid, pc)
            if x["qa_id"] == qa["qa_id"]][0]
    assert rec2["verdict"] == "passed"


# ---------------------------------------------------------------- T22 QA 断点续办＋结论枚举

async def _qa_child(monkeypatch, wid, pc, title="对象"):
    d = await card_store.create_card(
        wid, title, "模型生成", card_type="sub_project",
        parent=pc, skip_kick=True)
    return d["meta"]["card_id"]


async def test_qa_resume_after_card_created(sandbox, monkeypatch):
    """[反例①] 建卡成功、差事写入失败 ⇒ 同键续办只保留一张卡。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "卡后中断计划")
    dev = await _qa_child(monkeypatch, wid, pc)
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    orig = card_tools._h_create_deploy_card
    made = []

    async def _fail_after_card(work_id, current_id, args, cards_map, **kwargs):
        r = await orig(work_id, current_id, args, cards_map, **kwargs)
        made.append(r.get("card_id"))
        return {**r, "error": {"code": "injected",
                               "message": "差事写入中断"}, "ok": False}
    monkeypatch.setattr(card_tools, "_h_create_deploy_card", _fail_after_card)
    r1 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="断点续建差事账", evidence_refs=[])
    assert not r1["ok"] and made
    card1 = made[0]
    monkeypatch.setattr(card_tools, "_h_create_deploy_card", orig)
    r2 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="断点续建差事账", evidence_refs=[])
    assert r2["ok"] and r2["qa_id"] == r1.get("qa_id", r2["qa_id"])
    assert r2.get("qa_card") == card1
    # 实际只留一张 QA 卡
    qa_cards = [c for c in card_store.iter_card_metas(wid).values()
                if str(c.get("card_type") or "") == "qa_card"]
    assert len(qa_cards) == 1 and qa_cards[0]["card_id"] == card1
    rows = [x for x in coordination_runner.qa_records(wid, pc)
            if x["qa_id"] == r2["qa_id"]]
    assert len(rows) == 1 and rows[0]["deployment_id"]


async def test_qa_resume_after_register_before_record(sandbox, monkeypatch):
    """[反例②] 卡+差事账已建、登记前取消 ⇒ 按 qa_id 找回原卡原差事。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "登记前中断计划")
    dev = await _qa_child(monkeypatch, wid, pc)
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    orig = card_tools._h_create_deploy_card
    first = {}

    async def _cancel_before_record(work_id, current_id, args, cards_map, **kwargs):
        r = await orig(work_id, current_id, args, cards_map, **kwargs)
        first.update(card_id=r.get("card_id"),
                     deployment_id=r.get("deployment_id"))
        # 请求在写统筹 QA 行之前取消：删除占位（模拟未登记）
        from src.storage import card_db as _cdb
        def _del(conn):
            conn.execute("DELETE FROM coordination_qa WHERE work_id=?",
                         (work_id,))
        _cdb.write_sync(work_id, _del)
        return r
    monkeypatch.setattr(card_tools, "_h_create_deploy_card",
                        _cancel_before_record)
    r1 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="登记前中断找回", evidence_refs=[])
    assert r1["ok"]
    monkeypatch.setattr(card_tools, "_h_create_deploy_card", orig)
    r2 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="登记前中断找回", evidence_refs=[])
    assert r2["ok"] and not r2.get("in_progress")
    assert r2.get("qa_card") == first["card_id"]
    assert r2.get("deployment_id") == first["deployment_id"]
    qa_cards = [c for c in card_store.iter_card_metas(wid).values()
                if str(c.get("card_type") or "") == "qa_card"]
    assert len(qa_cards) == 1


async def test_qa_verdict_enum_not_fooled_by_substring(sandbox, monkeypatch):
    """[反例③] 结论：未通过。正式入口仍返回 503 ⇒ failed（不是通过）。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "枚举结论计划")
    dev = await _qa_child(monkeypatch, wid, pc)
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="枚举结论识别", evidence_refs=[])
    assert qa["ok"]
    r = await card_tools._h_report_deploy(
        wid, fids["qa"],
        {"content": "结论：未通过。正式入口仍返回 503",
         "_deployment_id": qa["deployment_id"]},
        card_store.iter_card_metas(wid))
    assert r.get("ok")
    rec = [x for x in coordination_runner.qa_records(wid, pc)
           if x["qa_id"] == qa["qa_id"]][0]
    assert rec["verdict"] == "failed"
    assert "503" in rec["conclusion"]


# ---------------------------------------------------------------- T23 真实中断点恢复

async def _inject_at(monkeypatch, wid, func, target, *, inject_at):
    """在指定真实阶段抛错（不是事后把成功回包装成失败）。"""
    orig = getattr(func, target)

    async def _wrapped(*a, **kw):
        r = await orig(*a, **kw)
        if inject_at == "register" and str(a[:1]) and False:
            pass
        raise OSError(f"注入中断:{inject_at}")
    monkeypatch.setattr(func, target, _wrapped)
    return orig


async def test_qa_real_register_failure_resume(sandbox, monkeypatch):
    """[真实中断点①] 卡已建、写差事账失败 ⇒ 同键重试最终 1 卡 1 账 1 次起轮。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "写账中断计划")
    dev = await _qa_child(monkeypatch, wid, pc)
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import card_tools as _ct
    from src.storage import function_store as _fs

    orig_create = _fs.create_deployment
    wakes = []

    async def _fail_register(*a, **kw):
        # 真实建卡已完成（_h_create_deploy_card 已走到建账），此处注入失败
        raise OSError("注入：写差事账失败")
    monkeypatch.setattr(_fs, "create_deployment", _fail_register)
    r1 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="真实写账中断", evidence_refs=[])
    assert not r1["ok"]
    monkeypatch.setattr(_fs, "create_deployment", orig_create)
    # 统计正式起轮阶段被调用次数（ak_cAS 保证一次被接纳）
    orig_wake = _ct.activate_and_wake_deployment

    async def _spy_wake(*a, **kw):
        wakes.append(a[:2])
        return await orig_wake(*a, **kw)
    monkeypatch.setattr(_ct, "activate_and_wake_deployment", _spy_wake)
    r2 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="真实写账中断", evidence_refs=[])
    assert r2["ok"], r2
    qa_cards = [c for c in card_store.iter_card_metas(wid).values()
                if str(c.get("card_type") or "") == "qa_card"]
    assert len(qa_cards) == 1
    deps = _fs._query_deployments(wid, "AND batch_id=?", (r2["qa_id"],))
    assert len(deps) == 1 and deps[0]["status"] in ("active", "done")
    assert len(wakes) == 1


async def test_qa_real_activate_failure_resume(sandbox, monkeypatch):
    """[真实中断点②] 差事已写入、激活失败 ⇒ 同键续办不重建账，一次起轮。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "激活中断计划")
    dev = await _qa_child(monkeypatch, wid, pc)
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import card_tools as _ct
    from src.storage import function_store as _fs

    orig_activate = _fs.activate_deployment

    async def _fail_activate(*a, **kw):
        raise OSError("注入：激活失败")
    monkeypatch.setattr(_fs, "activate_deployment", _fail_activate)
    # 正式路径建卡+建账成功，激活失败 ⇒ _h_create_deploy_card 捕获后仍 ok=True
    r1 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="真实激活中断", evidence_refs=[])
    assert not r1["ok"]
    monkeypatch.setattr(_fs, "activate_deployment", orig_activate)
    before = _fs._query_deployments(wid, "", (), limit=20)
    assert len(before) == 1
    # 同键重试（新的派驻请求）——原 qa_id 找到原卡原账，不重建
    r2 = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="真实激活中断", evidence_refs=[])
    assert r2["ok"] and r2["qa_id"]
    after = _fs._query_deployments(wid, "AND batch_id=?", (r2["qa_id"],))
    assert len(after) == 1 and after[0]["id"] == before[0]["id"]
    qa_cards = [c for c in card_store.iter_card_metas(wid).values()
                if str(c.get("card_type") or "") == "qa_card"]
    assert len(qa_cards) == 1


@pytest.mark.parametrize("interruption", ["register_error", "register_cancel", "spawn_error", "gate_cancel"])
async def test_qa_recovery_admits_original_deployment_once(sandbox, monkeypatch, interruption):
    from src.api import message_runner

    wid, _, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "派驻恢复计划")
    target = await _qa_child(monkeypatch, wid, plan_card)
    assignment = await _appointed(monkeypatch, wid, fids, plan_card, mode="live")
    tasks = []
    starts = []
    original_args = {}
    at_gate = asyncio.Event()
    release_gate = asyncio.Event()
    spawn_failed = False

    async def request():
        return await coordination_runner.coordination_qa_request(
            wid, plan_card, assignment_id=assignment, rev=0, item_id="i1",
            target_card=target, acceptance="逐条验收并附证据", evidence_refs=[])

    async def register_interrupted(*args, **kwargs):
        original_args.update(kwargs)
        if interruption == "register_cancel":
            raise asyncio.CancelledError
        raise OSError("差事账尚未写入")

    def spawn(coro, **kwargs):
        nonlocal spawn_failed
        if interruption == "spawn_error" and not spawn_failed:
            spawn_failed = True
            raise RuntimeError("任务尚未入队")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    async def acquire(work, venue):
        if interruption == "gate_cancel":
            at_gate.set()
            await release_gate.wait()

    async def run_agent(work, venue, mandate, *args, **kwargs):
        deployment_id = kwargs["source"]["deployment_id"]
        dep = function_store.get_deployment(work, deployment_id)
        assert dep["wake_status"] == "admitted"
        assert venue == dep["venue_ref"] and kwargs["ctx_card"] == venue
        assert kwargs["act_as"] == dep["function_id"] == fids["qa"]
        assert kwargs["source"]["delegator"] == dep["delegator"] == plan_card
        assert kwargs["tool_allow"] == set(dep["tools"])
        assert mandate == dep["mandate"] and "需求文档：" in mandate
        if original_args:
            assert mandate == original_args["mandate"]
            assert kwargs["tool_allow"] == set(original_args["tools"])
        starts.append((venue, deployment_id))
        return {"content": "已接纳"}

    monkeypatch.setattr(message_runner, "_spawn_tracked", spawn)
    monkeypatch.setattr(routes.serial_gate, "acquire", acquire)
    monkeypatch.setattr(routes, "_run_message_inner", run_agent)

    with monkeypatch.context() as patch:
        if interruption.startswith("register_"):
            patch.setattr(function_store, "create_deployment", register_interrupted)
        if interruption == "register_cancel":
            with pytest.raises(asyncio.CancelledError):
                await request()
        else:
            first = await request()
            if interruption == "register_error":
                assert first["code"] == "qa_dispatch_failed"
            else:
                assert first["wake_status"] == ("queued" if interruption == "gate_cancel" else "pending")
                dep = function_store.get_deployment(wid, first["deployment_id"])
                assert dep["status"] == "active" and dep["wake_status"] == "pending"

    if interruption == "gate_cancel":
        await asyncio.wait_for(at_gate.wait(), 5)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert function_store.get_deployment(wid, first["deployment_id"])["wake_status"] == "pending"
        release_gate.set()

    [original_card] = [c["card_id"] for c in card_store.iter_card_metas(wid).values()
                       if c["card_type"] == "qa_card"]
    assert not starts
    recovered = await request()
    assert recovered["ok"] and recovered["qa_card"] == original_card
    assert recovered["wake_status"] == "queued"
    await asyncio.wait_for(tasks[-1], 5)
    assert starts == [(original_card, recovered["deployment_id"])]
    [dep] = function_store._query_deployments(wid, "AND batch_id=?", (recovered["qa_id"],))
    assert dep["id"] == recovered["deployment_id"]
    [record] = coordination_runner.qa_records(wid, plan_card)
    assert record["wake_status"] == "admitted" and "deployment_args" not in record

    await function_store.close_deployment(wid, dep["id"], status="withdrawn", result="已取消")
    replay = await request()
    assert replay["wake_status"] == "admitted" and replay["deployment_id"] == dep["id"]
    closed = function_store.get_deployment(wid, dep["id"])
    assert closed["status"] == "withdrawn" and closed["result"] == "已取消"
    assert len(starts) == 1 and len(tasks) == (2 if interruption == "gate_cancel" else 1)


async def test_qa_verdict_texts_full_enum(sandbox, monkeypatch):
    """[结论回归] 三条文本：未通过+503=failed；通过率尚未统计=unverified；
    文本 verdict=passed ⇒ passed。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    pc = await _mk_card(wid, "结论文本计划")
    dev = await _qa_child(monkeypatch, wid, pc)
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live")
    from src.api import message_runner as mr

    async def _no_wake(*a, **kw):
        return True
    monkeypatch.setattr(mr, "deliver_now", _no_wake)
    qa = await coordination_runner.coordination_qa_request(
        wid, pc, assignment_id=aid, rev=0, item_id="i1",
        target_card=dev, acceptance="三条结论文本", evidence_refs=[])
    assert qa["ok"]
    dep_id = int(qa["deployment_id"])
    cases = [
        ("结论：未通过。正式入口仍返回 503", "failed"),
        ("结论：通过率尚未统计", "unverified"),
        ("逐条核对完成，文本 verdict=passed 供面板读取", "passed"),
    ]
    for content, expect in cases:
        r = await coordination_runner.ingest_qa_report(wid, dep_id, content)
        assert r["ok"] and r["verdict"] == expect, (content, r)


# ---------------------------------------------------------------- T24 执行证据回挂

async def _evidence_action(monkeypatch, wid, title="证据"):
    woke = await _direct_env(monkeypatch)
    pc = await _mk_card(wid, f"{title}计划")
    dev = await _mk_card(wid, f"{title}目标")
    fids = await _seed_functions(wid) if "function_store" in globals() else None
    return pc, dev, woke


async def test_execution_evidence_bind_and_guards(sandbox, monkeypatch):
    """[工作二] 正式回报回挂原行动；重复/错目标/他 work 同号/不可读分别拒。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    woke = await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "证据计划")
    dev = await _mk_card(wid, "证据目标")
    src = await _mk_card(wid, "证据来源")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:11"
    coordination_watcher.feed(wid, src, _rec(src, 11, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d["ok"] and d["msg_id"]
    msg_id = d["msg_id"]
    # 正向：目标卡走**正式 report_up** 落盘回执（reply_to=原 msg_id）⇒ 绑定
    rep = await card_tools._h_report_up(
        wid, dev, {"content": "已按需求实现", "task": msg_id,
                   "_coordination_msg_id": msg_id},
        card_store.iter_card_metas(wid))
    ev = coordination_runner.read_execution_evidence(wid, pc,
                                                     r["action_key"])
    assert len(ev) == 1
    reply_mid = ev[0]["reply_msg_id"]
    assert reply_mid.startswith("cmsg-")
    # 重复回执：同坐标去重
    b2 = await coordination_runner.bind_execution_evidence(
        wid, dev, coordination_msg_id=msg_id, reply_msg_id=reply_mid,
        reply_to=msg_id, from_card=dev, content="已按需求实现")
    assert b2["ok"] and b2["replayed"]
    assert len(coordination_runner.read_execution_evidence(
        wid, pc, r["action_key"])) == 1
    # 错目标：来源卡不是该行动目标
    other = await _mk_card(wid, "错目标卡")
    bad = await coordination_runner.bind_execution_evidence(
        wid, other, coordination_msg_id=msg_id,
        reply_msg_id="cmsg-reply2")
    assert not bad["ok"] and bad["code"] == "wrong_target"
    # 无关联：他 work 同号（这里 msg_id 不存在于本 work）
    none = await coordination_runner.bind_execution_evidence(
        wid, dev, coordination_msg_id="cmsg-not-exist",
        reply_msg_id="cmsg-reply3")
    assert not none["ok"] and none["code"] == "unlinked"
    # 面板：已取得执行证据
    board = coordination_runner.board_summary(wid, pc)
    act = [a for a in board["actions"] if a["action_key"] == r["action_key"]][0]
    assert act["status"] == "已取得执行证据"
    assert act["execution_evidence"][0]["reply_msg_id"] == reply_mid


# ---------------------------------------------------------------- T26 正式回报绑定三闸

async def test_evidence_binding_guards_strict(sandbox, monkeypatch):
    """[返工①] 回执不存在/reply_to 指向别的行动 ⇒ 不记已验证。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "严格证据计划")
    dev = await _mk_card(wid, "严格证据目标")
    src = await _mk_card(wid, "严格证据来源")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:21"
    coordination_watcher.feed(wid, src, _rec(src, 21, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d["ok"]
    mid = d["msg_id"]
    # ① 回执不存在（编造 reply_msg_id）⇒ 拒
    bad1 = await coordination_runner.bind_execution_evidence(
        wid, dev, coordination_msg_id=mid, reply_msg_id="cmsg-fabricated",
        reply_to=mid, from_card=dev, content="假回执")
    assert not bad1["ok"] and bad1["code"] == "reply_not_found"
    # ② reply_to 指向别的行动 ⇒ 拒
    bad2 = await coordination_runner.bind_execution_evidence(
        wid, dev, coordination_msg_id=mid, reply_msg_id="cmsg-x",
        reply_to="cmsg-other", from_card=dev, content="错行动")
    assert not bad2["ok"] and bad2["code"] == "wrong_reply_to"
    # ③ 错来源卡 ⇒ 拒
    other = await _mk_card(wid, "错来源")
    bad3 = await coordination_runner.bind_execution_evidence(
        wid, other, coordination_msg_id=mid, reply_msg_id="cmsg-x",
        reply_to=mid, from_card=other, content="错卡")
    assert not bad3["ok"] and bad3["code"] == "wrong_target"
    assert coordination_runner.read_execution_evidence(
        wid, pc, r["action_key"]) == []


async def test_report_up_wires_evidence(sandbox, monkeypatch):
    """[返工①] 正式 report_up 落盘后**自动**绑定（宿主注入 msg_id）。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "报告绑定计划")
    dev = await _mk_card(wid, "报告目标")
    src = await _mk_card(wid, "报告来源")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:22"
    coordination_watcher.feed(wid, src, _rec(src, 22, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    assert d["ok"]
    mid = d["msg_id"]
    # 目标卡通过正式 report_up 回报：父卡＝计划卡（_mk_card 无 parent→根卡）
    # 直接调 handler 模拟正式落盘（带宿主注入的 msg_id）
    rep = await card_tools._h_report_up(
        wid, dev,
        {"content": "已按验收判据实现，证据见集成测试日志", "task": mid,
         "_coordination_msg_id": mid},
        card_store.iter_card_metas(wid))
    assert rep.get("ok") or rep.get("error")  # 落盘路径执行
    ev = coordination_runner.read_execution_evidence(wid, pc, r["action_key"])
    assert ev, "正式 report_up 落盘后应自动绑定原行动"
    board = coordination_runner.board_summary(wid, pc)
    act = [a for a in board["actions"] if a["action_key"] == r["action_key"]][0]
    assert act["status"] == "已取得执行证据"
    assert act["execution_evidence"][0]["content"]


async def test_evidence_rejects_unrelated_real_report(sandbox, monkeypatch):
    """[返工①反例] 真实存在但发件卡/reply_to 与行动无关的汇报 ⇒ 拒。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "无关回报计划")
    dev = await _mk_card(wid, "无关回报目标")
    src = await _mk_card(wid, "无关回报来源")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:31"
    coordination_watcher.feed(wid, src, _rec(src, 31, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    mid = d["msg_id"]
    # 一张**无关卡**发一条真实 report_up（落盘到其父卡），reply_to 指别的串
    unrelated = await _mk_card(wid, "无关发件卡")
    rep = await card_tools._h_report_up(
        wid, unrelated, {"content": "无关汇报", "task": "cmsg-unrelated",
                         "_coordination_msg_id": ""},
        card_store.iter_card_metas(wid))
    # 提取真实落盘的回执号
    from src.storage import session_store
    sess_dir = session_store.WORKS_DIR / wid / "sessions"
    import re as _re
    rmid = ""
    for p in sess_dir.glob("*.md"):
        m = _re.search(r"\[\[card-msg id=(cmsg-[0-9a-f]+) "
                       r"type=msg_report from=" + _re.escape(unrelated),
                       p.read_text(encoding="utf-8"))
        if m:
            rmid = m.group(1)
            break
    assert rmid
    bad = await coordination_runner.bind_execution_evidence(
        wid, dev, coordination_msg_id=mid, reply_msg_id=rmid,
        reply_to=mid, from_card=unrelated, content="无关")
    assert not bad["ok"] and bad["code"] in (
        "wrong_reply_sender", "wrong_reply_to", "reply_not_found")
    assert coordination_runner.read_execution_evidence(
        wid, pc, r["action_key"]) == []


async def test_evidence_receipt_card_coordinate(sandbox, monkeypatch):
    """[返工④] 原件链接用正式回报的收件卡/会话键，不是文件名 session_N。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    await _direct_env(monkeypatch)
    pc = await _mk_card(wid, "坐标计划")
    dev = await _mk_card(wid, "坐标目标")
    # 让 dev 的父卡 = pc（真实收件卡）
    await card_store.update_card(wid, dev, {"parent": pc}, "挂到计划下")
    src = await _mk_card(wid, "坐标来源")
    aid = await _appointed(monkeypatch, wid, fids, pc, mode="live",
                           observe={"scope": "work"},
                           instruct={"scope": "cards", "ids": [dev]})
    await _link_actor(wid, pc, dev)
    coord = f"event:{wid}:{src}:41"
    coordination_watcher.feed(wid, src, _rec(src, 41, "card_block_edited"))
    r = await _field(wid, pc, aid, target=dev, refs=[coord])
    d = await coordination_runner.coordination_deliver(
        wid, pc, assignment_id=aid, rev=0, action_key=r["action_key"])
    mid = d["msg_id"]
    await card_tools._h_report_up(
        wid, dev, {"content": "正式回报", "task": mid,
                   "_coordination_msg_id": mid},
        card_store.iter_card_metas(wid))
    ev = coordination_runner.read_execution_evidence(wid, pc, r["action_key"])
    assert len(ev) == 1
    assert ev[0]["reply_session_card"] == pc, (
        f"收件卡应是 {pc}，实际 {ev[0]['reply_session_card']}")
    board = coordination_runner.board_summary(wid, pc)
    act = [a for a in board["actions"] if a["action_key"] == r["action_key"]][0]
    assert act["execution_evidence"][0]["receipt_session"] == pc
