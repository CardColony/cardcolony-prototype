# -*- coding: utf-8 -*-
"""[总裁制 P2-P5 2026-10-24] 汇聚协议/汇报广播/总裁执笔/确认与汇报/摘权 的测试。

组织(用户裁定):总裁=根卡兼职能卡(用户对话口+汇聚参与者+统筹执笔);
汇报=广播同刻到达父卡+盯线职能+总裁,职能观点→父卡统合→总裁统筹;
子卡只有汇报权(无横向发信);引导块唯总裁执笔。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools, function_room, prompts
from src.api import routes as api_routes
from src.storage import card_store, function_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()




async def _card_msgs(wid, cid):
    """该卡会话里收到的**卡间消息块**正文。

    [2026-10-27 · 用户裁定「inbox 直接取消」] 消息不再进收件箱，改落会话块，
    所以断言从 `card_db.list_pending` 改成读会话里 actor=card: 的块。
    """
    from src.storage import session_facade
    got = await session_facade.read(wid, cid)
    blocks = got.get("blocks") if isinstance(got, dict) else got
    return [str(b.get("content") or "") for b in (blocks or [])
            if str(b.get("actor") or "").startswith("card:")]

async def _card_msgs(wid, cid):
    """该卡会话里收到的**卡间消息块**正文。

    [2026-10-27 · 用户裁定「inbox 直接取消」] 消息不再进收件箱，改落会话块，
    所以断言从 `card_db.list_pending` 改成读会话里 actor=card: 的块。
    """
    from src.storage import session_facade
    got = await session_facade.read(wid, cid)
    blocks = got.get("blocks") if isinstance(got, dict) else got
    return [str(b.get("content") or "") for b in (blocks or [])
            if str(b.get("actor") or "").startswith("card:")]

async def _seeded(monkeypatch, title="汇聚") -> tuple[str, dict]:
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work(title)
    wid = wdata["work_id"]
    await function_room.seed_work(wid)
    return wid, card_tools._collect_card_meta(wid)


# ------------------------------------------------------------- P2 汇聚

# ------------------------------------------------------------- P4 汇报广播

async def test_report_up_only_to_parent(sandbox, monkeypatch):
    """[通信收拢 2026-10-28] 汇报**只发直接父卡**：不发总裁、不召集职能。

    改前：done/phase 还会给每张在职职能直发一条〔阶段收尾·请验收〕；
    用户裁定「report 只向上一级汇报，不应该发到总裁」⇒ 这两条一并撤掉。
    """
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "干活的卡", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_report_up(
        wid, cid, {"content": "完成A,证据:文件x"}, cards)
    assert out.get("ok") is not False, out
    assert [d["to"] for d in out["delivered"]] == [root_id], out
    assert all(d.get("msg_id") for d in out["delivered"])
    # 职能一条都收不到
    fn_ids = [p["function_id"] for p in function_store.list_profiles(wid)
              if function_room.catalog_code_of(p) != "ceo"]
    for f in fn_ids:
        msgs = await _card_msgs(wid, f)
        assert not any("阶段收尾" in t for t in msgs), (f, msgs)


async def test_report_up_validation(sandbox, monkeypatch):
    """[通信收拢 2026-10-28] 没有 kind 了；只剩三条校验：正文非空／总裁不汇报／离树卡不汇报。"""
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "子卡B", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    empty = await card_tools._h_report_up(wid, cid, {"content": ""}, cards)
    assert empty.get("ok") is False
    ceo = await card_tools._h_report_up(wid, root_id, {"content": "x"}, cards)
    assert ceo.get("ok") is False                               # 总裁不汇报


# ------------------------------------------------------------- P3 执笔

async def test_curate_face_ceo_only_and_precise_edit(sandbox, monkeypatch):
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被执行卡", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    await card_store.update_card(wid, cid, {"requirements": "R1: 原始需求\nR2: 待改"}, "init")
    denied = await card_tools._h_curate_face(
        wid, cid, {"card_id": cid, "text": "篡改"}, cards)
    assert denied.get("ok") is False                            # 非总裁被拒
    ok = await card_tools._h_curate_face(
        wid, root_id, {"card_id": cid, "old_string": "R2: 待改",
                       "new_string": "R2: 已由总裁修订", "note": "汇聚结论"},
        cards)
    assert ok.get("mode") == "edit", ok
    body = (await card_store.read_card(wid, cid))["body"]
    assert "已由总裁修订" in body["requirements"] and "原始需求" in body["requirements"]
    vs = function_store.list_verdicts(wid, root_id, 5)
    assert any("策展" in str(v.get("reason") or "") for v in vs)  # 决策簿留痕


async def test_curate_face_requires_unique_old_string(sandbox, monkeypatch):
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "唯一性卡", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    await card_store.update_card(wid, cid, {"requirements": "X\nX"}, "init")
    bad = await card_tools._h_curate_face(
        wid, root_id, {"card_id": cid, "old_string": "X", "new_string": "Y"}, cards)
    assert bad.get("ok") is False                               # 多次匹配 ⇒ 拒


# ------------------------------------------------------------- P5 总裁对用户

async def test_confirm_and_report_user_gates(sandbox, monkeypatch):
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "冒充者", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    d1 = await card_tools._h_confirm_with_user(
        wid, cid, {"subject": "s", "premises": ["p"]}, cards)
    d2 = await card_tools._h_report_to_user(wid, cid, {"content": "x"}, cards)
    assert d1.get("ok") is False and d2.get("ok") is False      # 非总裁被拒
    ok1 = await card_tools._h_confirm_with_user(
        wid, root_id, {"subject": "量级前提",
                       "premises": ["假设并发 100", "假设免费额度无限"],
                       "context": "拆解发现"}, cards)
    assert ok1.get("asked") is True and ok1["premises"] == 2
    ok2 = await card_tools._h_report_to_user(
        wid, root_id, {"content": "两条线已开工,一条阻塞"}, cards)
    assert ok2.get("reported") is True
    vs = function_store.list_verdicts(wid, root_id, 10)
    assert any("user" in (v.get("targets") or []) for v in vs)  # 留痕 targets=user


# ------------------------------------------------------------- 工具面与组织节

async def test_subcard_has_report_up(sandbox, monkeypatch):
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "工具面卡", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    tools = card_tools.build_tool_schemas(card_id=cid, work_id=wid, cards=cards)
    names = {str(t.get("name")) for t in tools}
    assert "report_up" in names                                 # 汇报权人人有
    assert "send_card_message" not in names                     # lateral 闸收走





async def test_ceo_deny_applied(sandbox, monkeypatch):
    """总裁目录 deny 生效:不发信、不 edit_requirements(出口=curate_face/确认/汇报)。"""
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    tools = card_tools.build_tool_schemas(card_id=root_id, work_id=wid, cards=cards)
    names = {str(t.get("name")) for t in tools}
    assert "edit_requirements" not in names                         # ceo deny(执笔走 curate_face)


async def test_ceo_toolface_has_curate_and_user_tools(sandbox, monkeypatch):
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    tools = card_tools.build_tool_schemas(card_id=root_id, work_id=wid, cards=cards)
    names = {str(t.get("name")) for t in tools}
    for t in ("curate_face", "confirm_with_user", "report_to_user"):
        assert t in names, (t, names)


async def test_org_section_injected_when_catalog_on(sandbox, monkeypatch, tmp_path):
    """AUTO 提示词:目录开 ⇒ 组织与汇报节在;目录关 ⇒ 不在(逐字回落)。"""
    _use_repo_catalog(monkeypatch)
    import importlib
    assert "组织与汇报" in prompts.AUTO_BASE_PROMPT
    off = tmp_path / "off.json"
    off.write_text('{"seed_on_create": false, "functions": []}', encoding="utf-8")
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(off))
    function_room.reset_cache()
    importlib.reload(prompts)
    try:
        assert "组织与汇报" not in prompts.AUTO_BASE_PROMPT
    finally:
        _use_repo_catalog(monkeypatch)
        importlib.reload(prompts)
    assert "组织与汇报" in prompts.AUTO_BASE_PROMPT


# ------------------------------------------------------------- 卡面定制

async def test_face_blocks_drive_per_function_face(sandbox, monkeypatch):
    """各职能的面按目录 face_blocks 分叉:PM 有前提台账无指令簿;
    总裁有决策簿/汇报簿/分歧待裁;非目录存量职能保持默认五节。"""
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    pm = next(p for p in function_store.list_profiles(wid)
              if function_room.catalog_code_of(p) == "pm")
    s_pm = function_room.summary(wid, pm["function_id"])
    assert "## 前提台账" in s_pm["text"] and "## 指令簿" not in s_pm["text"]
    s_ceo = function_room.summary(wid, root_id)
    for sec in ("决策簿", "汇报簿", "分歧待裁"):
        assert f"## {sec}" in s_ceo["text"], sec
    # 非目录存量:默认五节
    prof = await function_store.create_function(
        wid, "存量职能", {"charter": {"version": 1, "text": "旧职责"}})
    s_old = function_room.summary(wid, prof["function_id"])
    for sec in ("职责书", "管辖", "观点簿", "警报簿", "指令簿"):
        assert f"## {sec}" in s_old["text"]
    assert "前提台账" not in s_old["text"]


async def test_write_ledger_own_codes_and_divergence(sandbox, monkeypatch):
    """write_ledger 三道闸:职能回合限定/只写自己职责面(或 divergence)/text 必填。"""
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    pm = next(p for p in function_store.list_profiles(wid)
              if function_room.catalog_code_of(p) == "pm")
    cto = next(p for p in function_store.list_profiles(wid)
               if function_room.catalog_code_of(p) == "cto")
    ok = await card_tools._h_write_ledger(
        wid, pm["function_id"],
        {"ledger": "premises",
         "text": "P1 并发100〔推定待确认〕;P2 TPM 是物理约束〔物理〕"},
        cards)
    assert ok.get("ok") is True, ok
    # PM 不能写 CTO 的台账
    bad = await card_tools._h_write_ledger(
        wid, pm["function_id"], {"ledger": "tech_requirements", "text": "越权"}, cards)
    assert bad.get("ok") is False
    # 任何职能可写 divergence(总裁面上可见)
    dv = await card_tools._h_write_ledger(
        wid, cto["function_id"],
        {"ledger": "divergence",
         "text": "与父卡统合相左:统合判「通过」,我判「B2 未过」,依据…"}, cards)
    assert dv.get("ok") is True
    # 非职能回合被拒
    den = await card_tools._h_write_ledger(
        wid, root_id, {"ledger": "premises", "text": "x"}, cards)
    assert den.get("ok") is False
    # PM 面上出现前提台账条目;总裁面上出现分歧条目
    s_pm = function_room.summary(wid, pm["function_id"])
    assert "P1 并发100" in s_pm["text"]
    s_ceo = function_room.summary(wid, root_id)
    assert "与父卡统合相左" in s_ceo["text"]


async def test_ceo_ledgers_populated_by_actions(sandbox, monkeypatch):
    """总裁的决策簿/汇报簿由 curate_face/report_to_user 自动落账。"""
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "执笔对象", "模型生成", parent=root_id)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    # [去全量更新 2026-10-28] curate_face 的整块写通道已取消，改用精准编辑
    await card_store.update_card(wid, cid, {"requirements": "（骨架占位）"}, "init")
    cards = card_tools._collect_card_meta(wid)
    await card_tools._h_curate_face(
        wid, root_id, {"card_id": cid, "old_string": "（骨架占位）",
                       "new_string": "R1 总裁定稿", "note": "汇聚"},
        cards)
    await card_tools._h_report_to_user(wid, root_id, {"content": "进展汇报正文"}, cards)
    s = function_room.summary(wid, root_id)
    assert "策展" in s["text"] and "进展汇报正文" in s["text"]
    dec = function_store.list_ledger(wid, root_id, "decisions", 5)
    rep = function_store.list_ledger(wid, root_id, "reports", 5)
    assert dec and rep


# ------------------------------------------------------------- 介入点重构

async def test_mechanical_phase_close_on_all_children_done(sandbox, monkeypatch):
    """[通信收拢 2026-10-28 改写] 子卡汇报只落到父卡，不再直发职能。

    改前：子卡 report_up 会顺手给每张在职职能直发〔阶段收尾·请验收〕，
    等于替上级决定了「这一阶段该验收了」。用户裁定汇报只有一条路（只到上一级）
    ⇒ 职能不再自动收到；要它介入，由父卡 sync/direct 或 harness 事件判据来做。
    """
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    c1 = (await card_store.create_card(wid, "子1", "模型生成", parent=root_id))["meta"]["card_id"]
    c2 = (await card_store.create_card(wid, "子2", "模型生成", parent=root_id))["meta"]["card_id"]
    await card_store.update_card(wid, c1, {"state": "completed"}, "done1")
    await card_store.update_card(wid, c2, {"state": "completed"}, "done2")
    cards = card_tools._collect_card_meta(wid)      # 建卡后必须重取（父卡从 meta 算）
    r = await card_tools._h_report_up(
        wid, c2, {"content": "阶段收口:两卡完成"}, cards)
    assert [d["to"] for d in r["delivered"]] == [root_id], r
    fn_ids = [p["function_id"] for p in function_store.list_profiles(wid)
              if function_room.catalog_code_of(p) != "ceo"]
    msgs_all = {f: await _card_msgs(wid, f) for f in fn_ids}
    assert not any("阶段收尾" in t for msgs in msgs_all.values() for t in msgs)


async def test_stage_open_does_not_wake_functions(sandbox, monkeypatch):
    """[职能唤醒收口 2026-10-28 · 用户令「摘掉所有现有的【评审】之外的唤醒」
     「立项评审不算，评审目前只算【发起评审】这个动作」]

    改前：stage_open 给在职非总裁职能各直发一条〔阶段开启·立项评审〕⇒ 每开一个
    阶段就叫醒四个职能各跑一整轮模型。改后：**开阶段零唤醒**，职能只被
    open_review 叫醒。这里钉住「零唤醒」，防止它悄悄长回来。
    """
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    mid = (await card_store.create_card(wid, "中间层", "模型生成",
                                        parent=root_id))["meta"]["card_id"]
    deep = (await card_store.create_card(wid, "深层孙卡", "模型生成",
                                         parent=mid))["meta"]["card_id"]
    fn_ids = [p["function_id"] for p in function_store.list_profiles(wid)
              if function_room.catalog_code_of(p) != "ceo"]
    before = {f: len(await _card_msgs(wid, f)) for f in fn_ids}
    await card_tools._h_stage_open(
        wid, root_id, {"card_id": deep}, cards)
    after = {f: len(await _card_msgs(wid, f)) for f in fn_ids}
    assert after == before, f"开阶段不该给职能发消息：{before} → {after}"


async def test_create_card_no_wake(sandbox, monkeypatch):
    """[头尾介入 v6] 建卡=过程,零事件零唤醒(头在需求首次填写完整时才发)。"""
    wid, cards = await _seeded(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_card(
        wid, root_id, {"title": "新派工卡", "goal": "完成X 并交付 Y", "mission": "负责X 模块的交付与验收, 边界到 Y 为止"}, cards)
    assert r.get("ok") is True
    wakes = [p["source"] for prof in function_store.list_profiles(wid)
             for p in function_store.pending_syncs(wid, prof["function_id"])]
    assert wakes == [], wakes


# ------------------------------------------------------------- 主流程注入
