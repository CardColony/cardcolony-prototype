# -*- coding: utf-8 -*-
"""接线层（policy_runner）单测：事件进来 → 写数据集 → 喂值班员 → 投递。

守的是三件事：
  ① 每条事件都要进数据集（否则新建策略回溯时会漏掉刚发生的事）；
  ② 只有凑够条件才投递，投递失败不拖垮别的策略；
  ③ 关掉开关就完全不工作（排障用）。
"""
from __future__ import annotations

import time

import pytest

from src.infra import fact_store as fs
from src.infra import harness, policy_runner as runner, policy_set as ps


def _policy(**kw) -> dict:
    base = {
        "id": "p1", "active": True,
        "scope": {"kind": "card"},
        "watch": ["tool_call"],
        "window": "24h",
        "when": {"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2},
        "then": {"target": "self", "message": "该汇报了:{card_id}", "wake": False},
    }
    base.update(kw)
    return base


def _rec(seq: int, type_: str = "tool_call", card: str = "c1", **data) -> dict:
    return {"seq": seq, "ts": time.time(), "type": type_,
            "data": data or {"tool": "bash"}, "card_id": card}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把两个单例都指到临时目录/测试策略上。"""
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load([_policy()])
    assert errs == []
    ps.reset_set(pset)
    yield store, pset
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


def test_feed_writes_dataset_for_every_event(wired):
    """每条事件都要进数据集——不管有没有策略关心它。"""
    store, pset = wired
    runner.feed("w", _rec(1, "done", "c9"))          # 没策略关心
    assert len(store.query("w")) == 1, "不关心的事件没进数据集"


def test_feed_grows_state_and_fires(wired):
    store, pset = wired
    assert runner.feed("w", _rec(1)) == []
    fired = runner.feed("w", _rec(2))
    assert len(fired) == 1
    assert fired[0]["policy"] == "p1"
    assert fired[0]["ctx"]["card_id"] == "c1"


def test_feed_is_zero_blocking_on_bad_event(wired):
    """坏记录不该抛出去——接线在事件落盘的主链路上。"""
    store, pset = wired
    assert runner.feed("w", {}) == []
    assert runner.feed("", _rec(1)) == []
    assert runner.feed("w", {"type": "", "seq": 1}) == []


def test_off_switch_disables_everything(wired, monkeypatch):
    store, pset = wired
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    assert runner.feed("w", _rec(1)) == []
    assert store.query("w") == [], "开关关掉了还在写库"
    assert pset.states() == []


async def test_deliver_fired_renders_and_sends(wired, monkeypatch):
    """触发记录 → 真的调用投递，模板占位符被替换。"""
    calls: list[tuple] = []

    async def fake_deliver(work_id, target, text, *, rule_id="", wake=True, mode="message"):
        calls.append((work_id, target, text, rule_id, wake))
        return "mid"

    monkeypatch.setattr(harness, "deliver", fake_deliver)

    n = await runner.deliver_fired("w", [{
        "policy": "p1", "scope": ["c1"],
        "then": {"target": "self", "message": "该汇报了:{card_id}", "wake": False},
        "ctx": {"work_id": "w", "card_id": "c1", "parent": ""},
    }])
    assert n == 1
    work_id, target, text, rule_id, wake = calls[0]
    assert target == "c1"
    assert "c1" in text and "{card_id}" not in text
    assert rule_id == "p1" and wake is False


async def test_deliver_fired_one_failure_does_not_stop_others(wired, monkeypatch):
    """一条投递失败，后面的照发。"""
    calls: list[str] = []

    async def fake_deliver(work_id, target, text, *, rule_id="", wake=True, mode="message"):
        if target == "boom":
            raise RuntimeError("投递炸了")
        calls.append(target)
        return "mid"

    monkeypatch.setattr(harness, "deliver", fake_deliver)
    n = await runner.deliver_fired("w", [
        {"policy": "p1", "scope": ["boom"], "then": {"target": "self", "message": "x"},
         "ctx": {"work_id": "w", "card_id": "boom", "parent": ""}},
        {"policy": "p1", "scope": ["ok"], "then": {"target": "self", "message": "x"},
         "ctx": {"work_id": "w", "card_id": "ok", "parent": ""}},
    ])
    assert n == 1 and calls == ["ok"]


def test_bootstrap_seeds_from_dataset(tmp_path, monkeypatch):
    """启动回溯：把数据集里窗口内的事实补进本子，补完就满足的立刻触发。"""
    import json

    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    now = time.time()
    events = tmp_path / "events"
    events.mkdir()
    (events / "c1.jsonl").write_text("\n".join(json.dumps({
        "seq": i, "ts": now - 100 + i, "type": "tool_call",
        "data": {"tool": "bash"}, "card_id": "c1"}) for i in range(3)) + "\n",
        encoding="utf-8")

    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    pset.load([_policy()])
    ps.reset_set(pset)

    out = runner.bootstrap("w", since=now - 86400, events_dir=events)
    assert out["facts"] == 3
    assert out["seeded"].get("p1") == 3
    assert out["fired"] == 1, "回溯完已满足却没触发"
    assert pset.states()[0]["entries"] == 0, "触发后没撕本子"
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


def test_goal_block_edit_hits_the_notify_policy(tmp_path, monkeypatch):
    """改 goal ⇒ `card_block_edited(block="goal")` ⇒ 命中「目标已更新」这条策略。

    这条守的是用户 2026-10-28 的口径：「更新保存，它就会收到提醒」。
    """
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load([{
        "id": "goal_updated_notify", "active": True,
        "scope": {"kind": "card"}, "watch": ["card_block_edited"],
        "window": "1h",
        "when": {"event": {"types": ["card_block_edited"],
                           "where": {"block": "goal"}}},
        "then": {"target": "self", "message": "目标已更新", "wake": True},
    }])
    assert errs == []
    ps.reset_set(pset)

    rec = {"seq": 1, "ts": time.time(), "type": "card_block_edited",
           "data": {"block": "goal", "card_type": "sub_project",
                    "old": "旧目标", "new": "新目标", "by": "card_001"},
           "card_id": "card_009"}
    fired = runner.feed("w", rec)
    assert len(fired) == 1 and fired[0]["policy"] == "goal_updated_notify"

    # 改的是别的块 ⇒ 不该提醒
    rec2 = dict(rec, seq=2, data={**rec["data"], "block": "mission"})
    assert runner.feed("w", rec2) == []
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


def test_goal_block_edit_hits_the_notify_policy(tmp_path, monkeypatch):
    """改 goal ⇒ `card_block_edited(block="goal")` ⇒ 命中「目标已更新」这条策略。

    这条守的是用户 2026-10-28 的口径：「更新保存，它就会收到提醒」。
    """
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load([{
        "id": "goal_updated_notify", "active": True,
        "scope": {"kind": "card"}, "watch": ["card_block_edited"],
        "window": "1h",
        "when": {"event": {"types": ["card_block_edited"],
                           "where": {"block": "goal"}}},
        "then": {"target": "self", "message": "目标已更新", "wake": True},
    }])
    assert errs == []
    ps.reset_set(pset)

    rec = {"seq": 1, "ts": time.time(), "type": "card_block_edited",
           "data": {"block": "goal", "card_type": "sub_project",
                    "old": "旧目标", "new": "新目标", "by": "card_001"},
           "card_id": "card_009"}
    fired = runner.feed("w", rec)
    assert len(fired) == 1 and fired[0]["policy"] == "goal_updated_notify"

    # 改的是别的块 ⇒ 不该提醒
    rec2 = dict(rec, seq=2, data={**rec["data"], "block": "mission"})
    assert runner.feed("w", rec2) == []
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())
