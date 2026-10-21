# -*- coding: utf-8 -*-
"""记忆生命周期 · 时效账本与回收回归（2026-10-20 · 照抄 dsh record-use/reclaim）。

判据：
L1  entry_touch_update 刷新目标**并沿父链向上**（gist 走父链；description 不走）
L2  revive_update 清 sunk 并同次落账
L3  深层沉没：level≥2 超 T_SINK_MS ⇒ 整棵子树 sunk；**无 touch 账豁免**
L4  顶层退常驻：done 超 T_TOP_MS 且不在最近 R_TOP 条 ⇒ sunk
L5  幂等：已 sunk 不重复计数
L6  visible_chain 只剔 sunk，条目本身不删
"""
from __future__ import annotations

from src.storage import memory_domain as md, memory_lifecycle as lc

DAY = 24 * 60 * 60 * 1000


def _root(seq: int, status: str = "ongoing", **kw) -> dict:
    n = md.new_node("s1", seq, f"根{seq}", kind="decision", status=status)
    n["level"] = 1
    n["upper"], n["lower"] = [], []
    n.update(kw)
    return n


def _child(seq: int, parent: int, **kw) -> dict:
    n = md.new_node("s1", seq, f"子{seq}", kind="fact")
    n["level"] = 2
    n["upper"] = [{"sessionId": "s1", "seq": parent}]
    n.update(kw)
    return n


def _family() -> list[dict]:
    """root(1) ← child(2) 的合法两层链。"""
    r = _root(1, lower=[{"sessionId": "s1", "seq": 2}])
    c = _child(2, 1)
    return [r, c]


# ---------------------------------------------------------------- L1

def test_touch_refreshes_target_and_ancestors():
    chain = _family()
    now = 1_000_000
    out = lc.entry_touch_update(chain, 2, "gist", now)
    by = {n["anchor"]["seq"]: n for n in out}
    assert by[2]["lastUsedAt"] == now, "目标要刷新"
    assert by[1]["lastUsedAt"] == now, "父链要一起刷新"
    assert chain[1].get("lastUsedAt") is None, "不改入参"


def test_touch_description_does_not_propagate():
    """description 子条刷新时**不**沿父链传播（dsh 同口径）。"""
    chain = _family()
    desc = _child(2, 1)
    desc["role"] = "description"
    chain.append(desc)
    out = lc.entry_touch_update(chain, 2, "description", 5)
    by = {(n["anchor"]["seq"], n.get("role") or "gist"): n for n in out}
    assert by[(2, "description")]["lastUsedAt"] == 5
    assert by[(1, "gist")].get("lastUsedAt") is None, "description 不沿父链刷新"
    assert by[(2, "gist")].get("lastUsedAt") is None, "同锚 gist 不受 description 刷新影响"


# ---------------------------------------------------------------- L2

def test_revive_clears_sunk():
    chain = _family()
    chain[1]["sunk"] = True
    out = lc.revive_update(chain, 2, 999)
    by = {n["anchor"]["seq"]: n for n in out}
    assert by[2]["sunk"] is False and by[2]["lastUsedAt"] == 999
    assert by[1]["lastUsedAt"] == 999


# ---------------------------------------------------------------- L3

def test_deep_sink_after_ttl():
    chain = _family()
    chain[1]["lastUsedAt"] = 0            # 很久没用了
    out, rep = lc.reclaim(chain, 10 * DAY)
    by = {n["anchor"]["seq"]: n for n in out}
    assert by[2].get("sunk") is True, "深层超 T_SINK 要沉没"
    assert rep["newly_sunk"] >= 1


def test_deep_without_touch_is_exempt():
    chain = _family()                      # 没有 lastUsedAt
    out, rep = lc.reclaim(chain, 10 * DAY)
    assert all(not n.get("sunk") for n in out), "无 touch 账豁免（零误伤优先）"
    assert rep["newly_sunk"] == 0


# ---------------------------------------------------------------- L4

def test_top_done_retires_but_recent_exempt():
    """超过 R_TOP(6) 条时，最老的 done 顶层退常驻，最近 6 条豁免。"""
    chain = [_root(i, status="done", lastUsedAt=(i + 1) * DAY) for i in range(8)]
    out, _ = lc.reclaim(chain, 30 * DAY)
    by = {n["anchor"]["seq"]: n for n in out}
    # 最近 6 条＝seq 7..2（lastUsedAt 最大）；seq 0/1 不在最近 6 且超 T_TOP ⇒ 沉没
    assert by[0].get("sunk") is True
    assert by[1].get("sunk") is True
    assert not by[7].get("sunk") and not by[6].get("sunk"), "最近 6 条豁免"


def test_top_ongoing_never_retires():
    n = _root(1, status="ongoing", lastUsedAt=0)
    out, _ = lc.reclaim([n], 10 * DAY)
    assert not out[0].get("sunk"), "只有 done 才退常驻"


# ---------------------------------------------------------------- L5

def test_reclaim_idempotent():
    chain = _family()
    chain[1]["lastUsedAt"] = 0
    once, _ = lc.reclaim(chain, 10 * DAY)
    twice, rep2 = lc.reclaim(once, 10 * DAY)
    assert rep2["newly_sunk"] == 0, "已 sunk 不重复计数"
    assert [n.get("sunk") for n in twice] == [n.get("sunk") for n in once]


# ---------------------------------------------------------------- L6

def test_visible_chain_drops_only_sunk():
    chain = _family()
    chain[1]["sunk"] = True
    vis = lc.visible_chain(chain)
    assert [n["anchor"]["seq"] for n in vis] == [1]
    assert len(chain) == 2, "原链条目一个不删"


def test_constants_match_dsh():
    assert lc.T_SINK_MS == 7 * DAY
    assert lc.T_TOP_MS == 3 * DAY
    assert lc.R_TOP == 6
