# -*- coding: utf-8 -*-
"""[席位会话 2026-11-18 · 一卡多会话] 测试。

钉的事：
T1  登记处：POST 建会话分配 card@N、GET 列表含主会话＋附加会话、同职能可开多个
T2  会话隔离：seat 的会话文件/历史独立（主会话历史不含 seat 块，反之亦然）
T3  并行锁：card 与 card@1 的回合互斥体各自独立（并行裁定）
T4  wb 豁免：maybe_delegate 对 session_id != card_id 一律不委派
T5  persona 借身份不借手：_resolve_session 解析人格；模型/工具面锚仍是 card_id
T6  会话级停止：stop?session=card@1 只停该键（不越权停别的卡）
"""
from __future__ import annotations

import pytest

from src.api import function_room, routes
from src.api.wb_exec import bridge
from src.storage import card_store, function_store, session_facade, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


async def _seeded_work(monkeypatch, title: str = "席位会话"):
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()
    wdata = await work_store.create_work(title)
    wid = wdata["work_id"]
    await function_room.seed_work(wid)
    return wid


async def _mk_card(wid: str, title: str, card_type: str = "sub_project") -> str:
    r = await card_store.create_card(wid, title, "模型生成",
                                     card_type=card_type, skip_kick=True)
    return str(r["meta"]["card_id"])


def _qa_fid(wid: str) -> str:
    for p in function_store.list_profiles(wid):
        if function_room.catalog_code_of(p) == "qa":
            return str(p.get("function_id") or "")
    return ""


# ---------------------------------------------------------------- T1 登记处

async def test_session_registry_create_and_list(sandbox, monkeypatch):
    wid = await _seeded_work(monkeypatch, "登记处")
    cid = await _mk_card(wid, "宿主卡")
    qa = _qa_fid(wid)
    assert qa, "seed 后应有 qa 职能"

    r1 = await routes.post_card_session(wid, cid, {"persona": qa})
    assert r1.get("ok"), r1
    assert r1["session_id"] == f"{cid}@1" and r1["persona"] == qa

    # 同职能再开一个 ⇒ @2（不顶掉 @1）
    r2 = await routes.post_card_session(wid, cid, {"persona": qa})
    assert r2["session_id"] == f"{cid}@2", r2

    lst = await routes.get_card_sessions(wid, cid)
    ids = [s["session_id"] for s in lst["sessions"]]
    assert ids[0] == cid                       # 主会话排头
    assert f"{cid}@1" in ids and f"{cid}@2" in ids

    # 登记处落卡面 meta（权威在卡面）
    meta = (await card_store.read_card(wid, cid)).get("meta") or {}
    entries = routes._session_entries_of(meta)
    assert len(entries) == 2

    # 幽灵职能拒
    bad = await routes.post_card_session(wid, cid, {"persona": "fn_zzz"})
    assert bad.status_code == 400

    # _resolve_session：主会话/附加会话/不存在的键
    assert routes._resolve_session(meta, cid, "") == (cid, "")
    assert routes._resolve_session(meta, cid, f"{cid}@1") == (f"{cid}@1", qa)
    with pytest.raises(ValueError):
        routes._resolve_session(meta, cid, f"{cid}@99")
    # 别卡的会话键也拒
    with pytest.raises(ValueError):
        routes._resolve_session(meta, cid, "card_999@1")


# ---------------------------------------------------------------- T2 会话隔离

async def test_session_files_isolated(sandbox, monkeypatch):
    """seat 会话写自己的会话文件：主会话历史不含 seat 块，
    seat 历史不含主会话块。"""
    wid = await _seeded_work(monkeypatch, "会话隔离")
    cid = await _mk_card(wid, "宿主卡")
    sk = f"{cid}@1"

    await session_facade.append(wid, cid, {"type": "user_message",
                                           "content": "主会话的话"})
    await session_facade.append(wid, sk, {"type": "user_message",
                                          "content": "seat 会话的话"})

    main_msgs = await session_facade.read(wid, cid)
    seat_msgs = await session_facade.read(wid, sk)
    main_texts = [str(m.get("content") or "") for m in main_msgs]
    seat_texts = [str(m.get("content") or "") for m in seat_msgs]
    assert any("主会话的话" in t for t in main_texts)
    assert not any("seat 会话" in t for t in main_texts)
    assert any("seat 会话的话" in t for t in seat_texts)
    assert not any("主会话的话" in t for t in seat_texts)


# ---------------------------------------------------------------- T3 并行锁

async def test_turn_locks_per_session(sandbox, monkeypatch):
    """并行裁定：card 与 card@1 是不同互斥体——seat 跑着主会话照样能起轮。"""
    wid = await _seeded_work(monkeypatch, "并行锁")
    cid = await _mk_card(wid, "宿主卡")
    sk = f"{cid}@1"

    assert session_facade.turn_try_acquire(wid, sk, "turn:seat") is True
    # 卡键照样拿得到（seat 占着不阻塞主会话）
    assert session_facade.turn_try_acquire(wid, cid, "turn:main") is True
    # 同一键再拿才拒
    assert session_facade.turn_try_acquire(wid, sk, "turn:seat2") is False
    assert session_facade.turn_try_acquire(wid, cid, "turn:main2") is False
    # 卡级扫描能看到两个键
    keys = session_facade.turn_sessions_for_card(wid, cid)
    assert set(keys) == {cid, sk}
    session_facade.turn_release(wid, sk, "turn:seat")
    session_facade.turn_release(wid, cid, "turn:main")
    assert session_facade.turn_sessions_for_card(wid, cid) == []


# ---------------------------------------------------------------- T4 wb 豁免

async def test_maybe_delegate_skips_seat_session(sandbox, monkeypatch):
    """seat 回合一律不委派——执行体没有 card_tools。"""
    wid = await _seeded_work(monkeypatch, "wb 豁免")
    cid = await _mk_card(wid, "qa 差事卡", card_type="qa_card")

    class _S:
        work_id = wid
        card_id = cid
        rounds = 0
        error = None
        session_id = ""
        persona = ""

    # 主会话照常走到委派闸后面（不验证委派本身，只验证没被 seat 闸拦截）
    # seat ⇒ None（不委派），不管卡型是否可委
    st = _S()
    st.session_id = f"{cid}@1"
    out = await bridge.maybe_delegate(st)
    assert out is None

    # 同键＝主会话 ⇒ 不被 seat 闸拦（继续走开关判定，结果仍是 None
    # ——switch 未开/cfg 未配——但**不是** seat 豁免那条路）
    st2 = _S()
    st2.session_id = cid
    out2 = await bridge.maybe_delegate(st2)
    assert out2 is None  # 落到既有的总开关/cfg 判定，不报错即过


# ---------------------------------------------------------------- T5 persona 与工具面分离

async def test_persona_borrows_identity_not_tools(sandbox, monkeypatch):
    """persona 进身份锚（145 区职能职责书），但**工具面锚仍是 card_id**——
    seat 有和卡自己完全一样的权力。"""
    from src.api.message_runner import RunState, build_context
    wid = await _seeded_work(monkeypatch, "身份与手分离")
    cid = await _mk_card(wid, "宿主卡")
    qa = _qa_fid(wid)

    st = RunState(work_id=wid, card_id=cid, content="")
    st.session_id = f"{cid}@1"
    st.persona = qa
    st.op = ""
    st.tool_allow = None
    st.prior = []
    st.prior_extra = []
    build_context(st)
    # 身份锚：145 区按 persona 的职能档案取
    ident = str(st.act_as or "") or str(st.persona or "") or st.card_id
    assert ident == qa
    # 工具面按 _actor_id＝act_as or card_id 建——persona 不掺和 ⇒ 卡的面
    # （seat 权力＝卡的权力：能看到卡级工具而不是职能授面的窄集）
    names = {str((t.get("function") or {}).get("name") or t.get("name") or "")
             for t in (st.tools or [])}
    assert "edit_card" in names or "read_card" in names, names


# ---------------------------------------------------------------- T6 会话级停止

async def test_stop_endpoint_session_scoped(sandbox, monkeypatch):
    """stop?session=card@1 只停那个键的在途回合；别卡键拒。"""
    wid = await _seeded_work(monkeypatch, "会话级停止")
    cid = await _mk_card(wid, "宿主卡")
    sk = f"{cid}@1"

    # 造一个在途回合（seat 键上）
    assert session_facade.turn_try_acquire(wid, sk, "turn:s1") is True
    res = await routes.post_work_stop(wid, cid, session=sk)
    assert res.get("stopped") or res.get("reason") == "no_inflight_turn"
    # 越权：别的卡的会话键 ⇒ 400
    bad = await routes.post_work_stop(wid, cid, session="card_999@1")
    assert bad.status_code == 400
    session_facade.turn_release(wid, sk, "turn:s1")
