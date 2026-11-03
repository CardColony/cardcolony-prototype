# -*- coding: utf-8 -*-
"""评审流量治理判据（2026-11-02 · 用户裁定：评审过程消息不需要记忆）。

判据链：
1. actor=评审型卡（card_type='review'）的块：**凝练口径**（``_read_origin``，
   keep=0）全蒸发——不进触发计量、不进凝练窗口；
2. **上下文口径**（keep=None）：只保留最新 ``REVIEW_CTX_KEEP``（缺省 3）条在场；
3. 评审型卡自身：凝练恒空、上下文只留最近 ``REVIEW_SELF_CTX_SEQ`` 个 seq；
4. 非评审流量（human/assistant/普通卡消息）一概不受影响。
"""
from __future__ import annotations

import pytest

from src.storage import memory_store as M, review_scope, session_facade


_REVID = "card_rev1"


def _pin_review_ids(monkeypatch, *ids: str) -> None:
    """评审判据只认「该 work 的 review 型卡集合」——钉住集合本身，
    让被测逻辑（蒸发/留存/豁免）在无注册表的沙箱里可复现。"""
    monkeypatch.setattr(review_scope, "review_card_ids",
                        lambda _w: frozenset(ids))


@pytest.mark.asyncio
async def test_review_traffic_evicted_from_pending(sandbox, work, monkeypatch):
    """凝练口径：评审流量块**一条都不进**待凝池（不需要记忆）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    _pin_review_ids(monkeypatch, _REVID)
    for i in range(5):
        await session_facade.append_card_message(
            wid, cid, from_card=_REVID, text=f"派单快照 v{i}", msg_type="msg_directive")
    await session_facade.append(wid, cid, {"role": "user", "content": "真活1"})
    await session_facade.append(wid, cid, {"role": "assistant", "content": "真答1"})

    pending = M._read_origin(wid, cid, 0)
    actors = {str(b.get("actor")) for b in pending}
    assert f"card:{_REVID}" not in actors, "评审流量必须从待凝池全蒸发"
    assert any(b.get("actor") == "human" or str(b.get("role")) == "user"
               for b in pending), "正常流量不受影响"


@pytest.mark.asyncio
async def test_review_traffic_context_keeps_latest(sandbox, work, monkeypatch):
    """上下文口径：评审流量只留最新 K 条在场（当前批次干活要看得见）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    _pin_review_ids(monkeypatch, _REVID)
    for i in range(6):
        await session_facade.append_card_message(
            wid, cid, from_card=_REVID, text=f"派单 v{i}", msg_type="msg_directive")
    blocks = await session_facade.read(wid, cid)
    kept = review_scope.drop_stale_review(wid, cid, blocks)
    rev = [b for b in kept if str(b.get("actor")) == f"card:{_REVID}"]
    assert len(rev) == review_scope._keep_n() == 3, "只留最新 3 条"
    assert "派单 v5" in str(rev[-1].get("content")), "最新一条必须在场"
    texts = [str(b.get("content") or "") for b in rev]
    assert "派单 v3" in texts[-3] and "派单 v0" not in "".join(texts), \
        "更早的蒸发、最新 3 条按序在场"


@pytest.mark.asyncio
async def test_review_card_self_fully_exempt(sandbox, work, monkeypatch):
    """评审型卡自身：凝练池恒空（全流过程化）；上下文只留最近 N seq。"""
    wid, rid = work["work_id"], work["work_card_id"]   # 拿根卡当「评审卡」钉
    _pin_review_ids(monkeypatch, rid)
    for i in range(6):
        await session_facade.append(wid, rid,
                                    {"role": "user", "content": f"工作{i}"})
    assert M._read_origin(wid, rid, 0) == [], "评审卡待凝池必须恒空"
    monkeypatch.setenv("REVIEW_SELF_CTX_SEQ", "3")
    blocks = await session_facade.read(wid, rid)
    kept = review_scope.drop_stale_review(wid, rid, blocks)
    seqs = [int(b.get("seq") or 0) for b in kept]
    assert max(seqs) - min(seqs) <= 3 and len(kept) <= 4, \
        "上下文只留最近 N seq（截尾窗口）"


@pytest.mark.asyncio
async def test_normal_card_messages_unaffected(sandbox, work, monkeypatch):
    """普通卡间消息不是评审流量：全量进池（2026-10-27 裁定不受影响）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    _pin_review_ids(monkeypatch, _REVID)            # 有评审卡在场也不误伤
    await session_facade.append_card_message(
        wid, cid, from_card="card_999", text="普通协作", msg_type="msg_sync")
    pending = M._read_origin(wid, cid, 0)
    assert any(str(b.get("actor")) == "card:card_999" for b in pending), \
        "非评审卡的消息正常进凝练池"
