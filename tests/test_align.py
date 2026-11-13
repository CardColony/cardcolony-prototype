# -*- coding: utf-8 -*-
"""对齐场（[对齐场 2026-11-11 · 用户令]）：两件工具＋轮转驱动的机械事实。

钉需求里明示的几条（全部走真实 handler / 真实场态，不 mock 存储）：
- 根卡发起：点 2-3 张卡，根卡＝开场席（**非**轮转成员，说完不再唤起）
- 子卡发起：邀 1-2 张，发起方＝轮转首位（先发言）、无场外开场席
- 成员必须在卡树内；对齐卡/评审卡不能当成员；一场至多 3 张
- align_end 只有成员能调；全员调完场散（卡 completed）
- 轮转顺序＝participants 固定序、轮流、跳过已 ended 的；上限闸防失控
- 工具面：align_end 不进常规面、场授名单点名才下发；create_align 常规面
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.api import align_runner, card_tools, message_runner
from src.storage import (align_store, card_db, card_store,
                         session_facade, work_store)


async def _mk_work():
    wdata = await work_store.create_work("对齐测试")
    return str(wdata["work_id"])


async def _mk_cards(wid: str, n: int = 2):
    """建 n 张普通子卡（挂根卡下），返回 id 列表。"""
    root = work_store.work_card_id_of(wid)
    out = []
    for i in range(n):
        r = await card_store.create_card(wid, f"子卡{i}", "用户直接创建",
                                         card_type="sub_project",
                                         parent=root)
        out.append(str(r["meta"]["card_id"]))
    return out


def _cards(wid: str):
    return card_store.iter_card_metas(wid)


async def _open_align(wid: str, initiator: str, cards_arg: list[str],
                      monkeypatch) -> str:
    monkeypatch.setattr(align_runner, "kick", lambda w, cid: True)
    out = await card_tools._h_create_align(
        wid, initiator, {"topic": "T", "cards": cards_arg}, _cards(wid))
    assert out.get("ok"), out
    return str(out["data"]["align_card_id"])


# ---------------------------------------------------------------- 建场
async def test_root_creates_align_with_opener(sandbox, monkeypatch):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    a, b = await _mk_cards(wid, 2)
    kicked = []
    monkeypatch.setattr(align_runner, "kick", lambda w, c: kicked.append(c) or True)
    out = await card_tools._h_create_align(
        wid, root, {"topic": "接口口径", "cards": [a, b]}, _cards(wid))
    assert out.get("ok"), out
    aid = out["data"]["align_card_id"]
    assert kicked == [aid]
    st = await align_store.state_of(wid, aid)
    assert st["status"] == "open"
    assert st["participants"] == [a, b]
    assert st["opener"] == root            # 根卡＝开场席
    assert root not in st["participants"]  # 但不在轮转成员里
    meta = _cards(wid).get(aid) or {}
    assert meta.get("card_type") == "align"
    assert meta.get("parent") == root


async def test_child_creates_align_self_first(sandbox, monkeypatch):
    wid = await _mk_work()
    a, b, c = await _mk_cards(wid, 3)
    monkeypatch.setattr(align_runner, "kick", lambda w, cid: True)
    out = await card_tools._h_create_align(
        wid, a, {"topic": "分工边界", "cards": [b, c]}, _cards(wid))
    assert out.get("ok"), out
    st = await align_store.state_of(wid, out["data"]["align_card_id"])
    assert st["participants"] == [a, b, c]  # 发起方＝轮转首位
    assert st["opener"] == ""                # 无场外开场席
    assert st["initiator"] == a


async def test_create_align_validation(sandbox, monkeypatch):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    a, b, c, d = await _mk_cards(wid, 4)
    monkeypatch.setattr(align_runner, "kick", lambda w, cid: True)
    cards = _cards(wid)
    # 根卡：1 张或 4 张都不行
    for arg in ([a], [a, b, c, d]):
        out = await card_tools._h_create_align(
            wid, root, {"topic": "x", "cards": arg}, cards)
        assert not out.get("ok") and out["error"]["code"] == "validation", out
    # 根卡把自己点进名单 → 拒
    out = await card_tools._h_create_align(
        wid, root, {"topic": "x", "cards": [root, a]}, cards)
    assert not out.get("ok") and out["error"]["code"] == "validation"
    # 子卡：邀 0 张或 3 张都不行
    for arg in ([], [b, c, d]):
        out = await card_tools._h_create_align(
            wid, a, {"topic": "x", "cards": arg}, cards)
        assert not out.get("ok") and out["error"]["code"] == "validation", out
    # 树外卡 → not_found
    out = await card_tools._h_create_align(
        wid, a, {"topic": "x", "cards": ["card_zzz"]}, cards)
    assert not out.get("ok") and out["error"]["code"] == "not_found"
    # 对齐卡不能当成员（一次性场）
    r = await card_store.create_card(wid, "旧场", "用户直接创建", card_type="align",
                                     parent=root)
    old_aid = str(r["meta"]["card_id"])
    out = await card_tools._h_create_align(
        wid, a, {"topic": "x", "cards": [old_aid]}, cards)
    assert not out.get("ok") and out["error"]["code"] == "validation"


# ---------------------------------------------------------------- align_end
async def test_align_end_only_members_and_closes(sandbox, monkeypatch):
    wid = await _mk_work()
    a, b = await _mk_cards(wid, 2)
    aid = await _open_align(wid, a, [b], monkeypatch)
    cards = _cards(wid)
    # 非成员 → denied
    root = work_store.work_card_id_of(wid)
    out = await card_tools._h_align_end(wid, root, {"align_card_id": aid}, cards)
    assert not out.get("ok") and out["error"]["code"] == "denied"
    # 成员 a → 记 ended，场还开着
    out = await card_tools._h_align_end(wid, a, {"align_card_id": aid}, cards)
    assert out.get("ok") and not out["data"]["closed"]
    assert out["data"]["remaining"] == [b]
    # 幂等：a 再调一次不重复记
    await card_tools._h_align_end(wid, a, {"align_card_id": aid}, cards)
    st = await align_store.state_of(wid, aid)
    assert st["ended"].count(a) == 1
    # 成员 b → 全员 ended ⇒ 散场＋卡退役
    out = await card_tools._h_align_end(wid, b, {"align_card_id": aid}, cards)
    assert out.get("ok") and out["data"]["closed"]
    st = await align_store.state_of(wid, aid)
    assert st["status"] == "closed"
    assert (_cards(wid).get(aid) or {}).get("state") == "completed"
    # 散场后再调 → validation（一次性场，再对齐须另开新场）
    out = await card_tools._h_align_end(wid, a, {"align_card_id": aid}, cards)
    assert not out.get("ok") and out["error"]["code"] == "validation"


# ---------------------------------------------------------------- 轮转驱动
async def test_runner_root_opener_then_round_robin(sandbox, monkeypatch):
    """根卡发起：开场席说一次（不再被叫），然后成员按序轮流直到全员 ended。"""
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    a, b = await _mk_cards(wid, 2)
    monkeypatch.setattr(align_runner, "kick", lambda w, cid: True)
    out = await card_tools._h_create_align(
        wid, root, {"topic": "T", "cards": [a, b]}, _cards(wid))
    aid = str(out["data"]["align_card_id"])

    calls: list[tuple[str, bool]] = []

    async def _fake_turn(w, c, seat, st, *, opener=False):
        calls.append((seat, opener))
        if seat == b:
            await align_store.mark_ended(w, c, b)
        if seat == a:
            await align_store.mark_ended(w, c, a)
        return {"seat": seat, "ok": True, "error": ""}

    monkeypatch.setattr(align_runner, "_seat_turn", _fake_turn)
    await align_runner.run_align(wid, aid)
    assert calls[0] == (root, True), "开场席先说、且 opener=True"
    assert calls[1] == (a, False) and calls[2] == (b, False), \
        f"成员按 participants 序轮流：{calls}"
    assert [s for s, _ in calls].count(root) == 1, "开场席只说一次、不再被唤起"
    st = await align_store.state_of(wid, aid)
    assert st["status"] == "closed"
    assert (_cards(wid).get(aid) or {}).get("state") == "completed"


async def test_runner_max_turns_caps(sandbox, monkeypatch):
    """谁也不调 align_end ⇒ 轮数到 max_turns 强制散场（防失控空烧）。"""
    wid = await _mk_work()
    a, b = await _mk_cards(wid, 2)
    aid = await _open_align(wid, a, [b], monkeypatch)

    calls: list[str] = []

    async def _fake_turn(w, c, seat, st, *, opener=False):
        calls.append(seat)
        return {"seat": seat, "ok": True, "error": ""}

    monkeypatch.setattr(align_runner, "_seat_turn", _fake_turn)
    # 把 turns 顶到上限前一格：再跑一轮就触发上限闸
    st0 = await align_store.state_of(wid, aid)
    st0["turns"] = int(st0["max_turns"]) - 1

    def _fn(conn):
        conn.execute("UPDATE cards SET align_state=? WHERE card_id=?",
                     (json.dumps(st0, ensure_ascii=False), aid))
    await card_db.run_write(wid, _fn)

    await align_runner.run_align(wid, aid)
    st = await align_store.state_of(wid, aid)
    assert st["status"] == "closed"
    assert "max_turns" in str(st.get("close_reason") or "")
    assert len(calls) <= 2, f"上限闸该及时收：{calls}"


# ---------------------------------------------------------------- 工具面
def test_align_end_not_in_normal_face(sandbox):
    """align_end 是场授专用件：常规工具面里没有它。"""
    names = {t.get("name") for t in card_tools.build_tool_schemas()}
    assert "align_end" not in names
    assert "align_end" in card_tools.VENUE_ONLY_TOOLS


async def test_create_align_in_normal_face(sandbox):
    """create_align 走常规面：有 read 开关的卡看得见。"""
    wid = await _mk_work()
    a, _b = await _mk_cards(wid, 2)
    names = {t.get("name") for t in
             card_tools.build_tool_schemas(a, wid, _cards(wid))}
    assert "create_align" in names
    assert "align_end" not in names


async def test_venue_grant_appends_align_end(sandbox):
    """场授面点了 align_end ⇒ 收窄后按名补回（成员在场里才看得见它）。"""
    wid = await _mk_work()
    a, b = await _mk_cards(wid, 2)
    r = await card_store.create_card(wid, "场", "用户直接创建", card_type="align",
                                     parent=a)
    aid = str(r["meta"]["card_id"])
    state = SimpleNamespace(
        work_id=wid, card_id=aid, act_as=a, ctx_card=a, op="",
        tool_allow={"read_card", "align_end"},
        condense_state=None, act_meta=None, pair_id="",
        prior=[], prior_extra=None, source={},
    )
    message_runner.build_context(state)
    names = {str((t.get("function") or {}).get("name") or t.get("name") or "")
             for t in state.tools}
    assert "align_end" in names, f"场授名单点了名就该在面里：{names}"
    assert "read_card" in names
    assert "write_file" not in names, "场授面∩能力上限：名单外的不下发"


# ---------------------------------------------------------------- 结论回投
async def _session_texts(wid: str, cid: str) -> list[str]:
    blocks = await session_facade.read(wid, cid) or []
    if isinstance(blocks, dict):
        blocks = blocks.get("blocks") or []
    return [str(b.get("content") or "") for b in blocks]


async def test_conclusion_collected_and_delivered(sandbox, monkeypatch):
    """各席自述结论 ⇒ 落场态；散场时汇总回投进**每个成员**自己的卡会话
    （成员会话里原本没有任何场内对话——这一块是它带走的全部）。"""
    wid = await _mk_work()
    a, b = await _mk_cards(wid, 2)
    aid = await _open_align(wid, a, [b], monkeypatch)
    cards = _cards(wid)
    out = await card_tools._h_align_end(
        wid, a, {"align_card_id": aid, "conclusion": "接口以 v2 为准"}, cards)
    assert out.get("ok") and not out["data"]["closed"]
    st = await align_store.state_of(wid, aid)
    assert st["conclusions"] == {a: "接口以 v2 为准"}
    # 未散场 ⇒ 还没回投（a 的会话里没有结论块）
    assert not any("对齐场散场" in t for t in await _session_texts(wid, a))
    out = await card_tools._h_align_end(
        wid, b, {"align_card_id": aid, "conclusion": "下周五前联调"}, cards)
    assert out["data"]["closed"]
    # 每个成员自己的会话里都落了汇总块（两份结论都在）
    for m in (a, b):
        hit = [t for t in await _session_texts(wid, m) if "对齐场散场" in t]
        assert hit, f"{m} 会话里该有结论回投块"
        assert "接口以 v2 为准" in hit[-1] and "下周五前联调" in hit[-1]
    # 场卡自己也落了收尾块（退役卡的记录完整）
    assert any("对齐场散场" in t for t in await _session_texts(wid, aid))
    # 幂等：占位已占，再投返回 0、不重复落块
    assert await align_runner.deliver_conclusions(wid, aid) == 0
    assert sum("对齐场散场" in t for t in await _session_texts(wid, a)) == 1


async def test_conclusion_not_to_root_opener(sandbox, monkeypatch):
    """用户裁定「只回投成员」：根卡发起时根卡只是开场席——
    散场结论落成员会话，**根卡会话一个块都不进**。"""
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    a, b = await _mk_cards(wid, 2)
    monkeypatch.setattr(align_runner, "kick", lambda w, cid: True)
    out = await card_tools._h_create_align(
        wid, root, {"topic": "T", "cards": [a, b]}, _cards(wid))
    aid = str(out["data"]["align_card_id"])
    cards = _cards(wid)
    await card_tools._h_align_end(wid, a, {"align_card_id": aid}, cards)
    await card_tools._h_align_end(
        wid, b, {"align_card_id": aid, "conclusion": "就这么定"}, cards)
    assert not any("对齐场散场" in t for t in await _session_texts(wid, root))
    assert any("就这么定" in t for t in await _session_texts(wid, a))


# ---------------------------------------------------------------- 场授件豁免
async def test_member_without_ability_can_align_end(sandbox, monkeypatch):
    """[场授件豁免 2026-11-15] align_end 的授权是场成员资格不是卡能力：
    abilities=[] 的成员（qa_card 天花板没列 align）也调得动——
    card_028 实陷：看得见 align_end 却被能力闸拒，席位永远收不了场。"""
    wid = await _mk_work()
    a, b = await _mk_cards(wid, 2)
    aid = await _open_align(wid, a, [b], monkeypatch)
    cards = _cards(wid)
    cards[a]["abilities"] = []            # 模拟 qa_card：零能力挂载
    out = await card_tools.execute_tool(
        wid, a, "align_end", {"align_card_id": aid}, cards, perm_gate=True)
    assert out.get("ok"), out
    assert a in (await align_store.state_of(wid, aid))["ended"]
    # 非成员没沾这层豁免：零能力照旧被能力闸拒（授权只是换层，不是开门）
    root = work_store.work_card_id_of(wid)
    cards[root] = dict(cards[root], abilities=[])
    out = await card_tools.execute_tool(
        wid, root, "align_end", {"align_card_id": aid}, cards, perm_gate=True)
    assert not out.get("ok") and out["error"]["code"] == "denied", out


async def test_runner_halt_closes_field(sandbox, monkeypatch):
    """场被停 ⇒ 驱动机械收场（closed＋卡退役＋结论回投），不留 open 尸。
    card_029 实陷：halt 裸 return，场永远 open/active 挂尸。"""
    wid = await _mk_work()
    a, b = await _mk_cards(wid, 2)
    aid = await _open_align(wid, a, [b], monkeypatch)
    # 先留一份结论，验证 halt 收口照常回投
    cards = _cards(wid)
    await card_tools._h_align_end(
        wid, a, {"align_card_id": aid, "conclusion": "已对齐"}, cards)
    monkeypatch.setattr(align_runner.control, "is_halted",
                        lambda w, cid: str(cid) == aid)
    await align_runner.run_align(wid, aid)
    st = await align_store.state_of(wid, aid)
    assert st["status"] == "closed"
    assert st.get("close_reason") == "halted"
    assert (_cards(wid).get(aid) or {}).get("state") == "completed"
    # 已登记的结论照样回投进成员会话（b 未留结论也收到汇总块）
    for m in (a, b):
        assert any("对齐场散场" in t for t in await _session_texts(wid, m))
