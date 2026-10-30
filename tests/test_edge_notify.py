"""依赖变更通知：能力具备、开关后置。

用户 2026-10-27：「你先把【能实现自动通知】的底层能力具备了，
要不要自动通知，后续都可以开关。」
"""

from __future__ import annotations

from src.api import edge_notify
from src.storage import card_db, card_store, session_facade, work_store


async def _make_pair():
    """A、B 两张卡，B 在依赖线里声明「依赖 A 的现状」。"""
    w = await work_store.create_work("依赖通知测试")
    wid = str(w["work_id"])
    a = await card_store.create_card(wid, "上游A", "用户直接创建",
                                     card_type="sub_project")
    b = await card_store.create_card(wid, "下游B", "用户直接创建",
                                     card_type="sub_project")
    aid, bid = str(a["meta"]["card_id"]), str(b["meta"]["card_id"])
    await card_store.update_card(
        wid, bid,
        {"relations": [{"target": aid, "kind": "依赖",
                        "detail": "依赖 A 现状块里的接口清单"}]},
        "建依赖线")
    return wid, aid, bid


async def _blocks_of(wid: str, cid: str) -> list[str]:
    got = await session_facade.read(wid, cid)
    blocks = got.get("blocks") if isinstance(got, dict) else got
    return [str(b.get("content") or "") for b in (blocks or [])]


async def test_dependents_query_works(sandbox):
    """底层能力一：查「谁依赖我」。"""
    wid, aid, bid = await _make_pair()
    deps = await edge_notify.dependents_of(wid, aid)
    assert [d["from_card"] for d in deps] == [bid], deps
    assert "接口清单" in deps[0]["detail"]


async def test_disabled_by_default_does_not_send(sandbox):
    """默认关：改卡面不产生任何通知。"""
    assert edge_notify.enabled() is False, "开关默认必须是关"
    wid, aid, bid = await _make_pair()
    await card_store.update_card(wid, aid, {"status_block": "接口清单：三个"}, "改现状")
    texts = await _blocks_of(wid, bid)
    assert not any("依赖变更" in t for t in texts), "开关关着却发了通知"


async def test_enabled_sends_sync_without_waking(sandbox, monkeypatch):
    """打开开关：给依赖方发一条同步，且**不唤醒**（不调 deliver_now）。"""
    monkeypatch.setattr(edge_notify, "enabled", lambda: True)
    wid, aid, bid = await _make_pair()
    sent = await edge_notify.notify_dependents(wid, aid, ["status_block"])
    assert sent == [bid], sent

    texts = await _blocks_of(wid, bid)
    hit = [t for t in texts if "依赖变更" in t]
    assert hit, "通知没落到依赖方会话"
    assert "现状" in hit[0], "没写清改了哪一块"
    assert "接口清单" in hit[0], "没带上对方声明的依赖内容"


async def test_notify_skips_when_no_change(sandbox, monkeypatch):
    """没有正文变化就不发（空 changed_blocks）。"""
    monkeypatch.setattr(edge_notify, "enabled", lambda: True)
    wid, aid, bid = await _make_pair()
    assert await edge_notify.notify_dependents(wid, aid, []) == []


async def test_notify_skips_self(sandbox, monkeypatch):
    """依赖自己（异常数据）不许给自己发。"""
    monkeypatch.setattr(edge_notify, "enabled", lambda: True)
    wid, aid, bid = await _make_pair()
    await card_db.run_write(
        wid, lambda conn: card_db.upsert_relation(conn, aid, aid, "依赖", "自环"))
    assert await edge_notify.notify_dependents(wid, aid, ["status_block"]) == [bid]


async def test_change_hook_fires_when_enabled(sandbox, monkeypatch):
    """端到端：开关打开后，**改卡面本身**就能触发通知（不靠人调函数）。"""
    monkeypatch.setattr(edge_notify, "enabled", lambda: True)
    wid, aid, bid = await _make_pair()
    await card_store.update_card(wid, aid, {"status_block": "新现状"}, "改现状")
    texts = await _blocks_of(wid, bid)
    assert any("依赖变更" in t for t in texts), "写卡钩子没接上依赖通知"
