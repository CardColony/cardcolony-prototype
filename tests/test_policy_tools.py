# -*- coding: utf-8 -*-
"""`design_policy` 工具单测：根卡自己设计盯梢策略。

守三件事：
  ① 只有根卡能配（子卡各配各的会打架，也难排查）；
  ② 坏条件当场拒（用与配置文件同一套校验，不等到运行时才炸）；
  ③ 写完立刻生效（热加载），且只对自己项目生效（work 字段锁死）。
"""
from __future__ import annotations

import json
import time

import pytest

from src.infra import policy_set as ps, policy_tools


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """把策略表指到临时文件，避免动仓库里的配置。"""
    p = tmp_path / "harness_policies.json"
    monkeypatch.setenv("ECO_POLICIES_PATH", str(p))
    ps.reset_set(ps.PolicySet())
    yield p
    ps.reset_set(ps.PolicySet())


def _args(**kw):
    base = {
        "id": "watch_child_done",
        "desc": "子卡完成了通知我",
        "scope": "card",
        "watch": ["goal/change"],
        "window": "30m",
        "when": {"event": {"types": ["goal/change"], "where": {"operation": "complete"}}},
        "target": "parent",
        "message": "{card_id} 完成了",
        "wake": True,
    }
    base.update(kw)
    return base


ROOT = {"card_001": {"card_type": "project"}}
CHILD = {"card_002": {"card_type": "coder"}}


async def test_non_root_is_denied(cfg):
    r = await policy_tools.handle("w", "card_002", _args(), CHILD)
    assert r["ok"] is False
    assert r["error"]["code"] == "denied"
    assert not cfg.exists(), "被拒了还写了文件"


async def test_bad_condition_is_rejected_before_write(cfg):
    # all 必须是数组——这是校验器真会拒的形状（未知键会被忽略，不算错）
    r = await policy_tools.handle("w", "card_001", _args(when={"all": "not-a-list"}), ROOT)
    assert r["ok"] is False and r["error"]["code"] == "validation"
    assert not cfg.exists(), "坏策略不该落盘"


async def test_missing_id_is_rejected(cfg):
    r = await policy_tools.handle("w", "card_001", _args(id=""), ROOT)
    assert r["ok"] is False and "id" in r["error"]["message"]


async def test_root_writes_and_reloads_immediately(cfg):
    r = await policy_tools.handle("w", "card_001", _args(), ROOT)
    assert r["ok"] is True, r
    assert cfg.exists()

    doc = json.loads(cfg.read_text(encoding="utf-8"))
    pol = [x for x in doc["policies"] if x["id"] == "watch_child_done"][0]
    assert pol["work"] == "w", "策略没锁到本项目"
    assert pol["then"]["target"] == "parent"

    # 热加载：立刻在 dispatcher 里可见，且只对 w 生效
    got = ps.dispatcher().policy("watch_child_done")
    assert got is not None and got.work == "w"


async def test_same_id_overwrites(cfg):
    await policy_tools.handle("w", "card_001", _args(), ROOT)
    r2 = await policy_tools.handle(
        "w", "card_001", _args(message="改过的提醒", window="1h"), ROOT)
    assert r2["ok"] is True and r2["data"]["replaced"] is True
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    same = [x for x in doc["policies"] if x["id"] == "watch_child_done"]
    assert len(same) == 1, "同 id 没覆盖，写出两条了"
    assert same[0]["then"]["message"] == "改过的提醒"


async def test_policy_only_fires_for_its_own_work(cfg):
    """work 字段锁死：别的 work 的事件不该命中这条。"""
    await policy_tools.handle("w", "card_001", _args(), ROOT)
    pset = ps.dispatcher()
    # _t 用**现在**：窗口 30m，时间戳太老会被惰性淘汰剔掉，那样测的就不是 work 过滤了
    fact = {"type": "goal/change", "card": "card_002", "work": "w",
            "operation": "complete", "_t": time.time(), "ts": ""}
    assert len(pset.dispatch("w", fact)) == 1
    fact2 = dict(fact, work="other")
    assert pset.dispatch("other", fact2) == [], "串到别的 work 去了"


# ---------------------------------------------------------------- 反馈够不够


async def test_unknown_event_type_is_rejected(cfg):
    """watch 里拼错一个字母 ⇒ 这条策略永远不会触发，必须当场拒。

    `fields_of()` 对未登记类型也返回空契约（非空 dict），判不出来；
    这里守的是「直接查目录」那个改法。
    """
    r = await policy_tools.handle(
        "w", "card_001", _args(watch=["tool_cal"]), ROOT)
    assert r["ok"] is False and r["error"]["code"] == "validation"
    assert "tool_cal" in r["error"]["message"]
    assert not cfg.exists()


async def test_trial_reports_would_fire(cfg, monkeypatch):
    """配完要告诉它「按现在的数据会不会触发」——否则它不知道这条是死是活。"""
    import time as _t

    from src.infra import fact_store as fs

    class _FakeStore:
        def query(self, work_id, **kw):
            now = _t.time()
            return [{"type": "tool_call", "card": "c1", "_t": now, "ts": ""}
                    for _ in range(3)]

    monkeypatch.setattr(fs, "store", lambda: _FakeStore())

    r = await policy_tools.handle("w", "card_001", _args(
        id="t_trial", watch=["tool_call"], window="24h",
        when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2},
        target="self", message="x"), ROOT)
    assert r["ok"] is True, r
    trial = r["data"]["trial"]
    assert trial["scanned"] == 3
    assert trial["would_fire"] == 1, trial


async def test_trial_zero_when_condition_too_strict(cfg, monkeypatch):
    """条件太严 ⇒ would_fire=0，模型能据此判断要不要调松。"""
    import time as _t

    from src.infra import fact_store as fs

    class _FakeStore:
        def query(self, work_id, **kw):
            return [{"type": "tool_call", "card": "c1", "_t": _t.time(), "ts": ""}]

    monkeypatch.setattr(fs, "store", lambda: _FakeStore())
    r = await policy_tools.handle("w", "card_001", _args(
        id="t_strict", watch=["tool_call"], window="24h",
        when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99999},
        target="self", message="x"), ROOT)
    assert r["ok"] is True
    assert r["data"]["trial"]["would_fire"] == 0


# ---------------------------------------------------------------- 第三道接口：提示词注入


async def test_inject_appears_in_system_exactly_once(work, cfg):
    """注入只出现在**一轮**的 system 里：装配一次后队列清空，下次不再出现。

    [2026-10-28 用户口径]「整轮保留，但不是轮内每次调用都注入」——
    system 每轮只组一次、整轮复用，所以它出现一次就够，不会每次调用都塞一遍。
    """
    from src.api import context_assembler
    from src.storage import card_store

    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.push_pending_inject(wid, cid, "注意：测试注入内容")

    first = context_assembler.build_context_bundle(wid, cid)
    once_text = " ".join(str(m.get("content", "")) for m in (first["main"]["turn_once"] or []))
    assert "注意：测试注入内容" in once_text, "注入没进当轮消息"
    assert "注意：测试注入内容" not in first["main"]["system_prompt"], "注入不该进 system"

    # 第二次装配（＝下一轮）不该再有它：不进历史、用完即清
    second = context_assembler.build_context_bundle(wid, cid)
    once2 = " ".join(str(m.get("content", "")) for m in (second["main"]["turn_once"] or []))
    assert "注意：测试注入内容" not in once2


async def test_inject_does_not_enter_session_history(work, cfg):
    """注入**不进会话历史**——这与 message 手段的根本区别。"""
    from src.storage import card_store, session_facade

    wid, cid = work["work_id"], work["work_card_id"]
    path = session_facade._session_path(wid, cid)
    before = path.stat().st_size if path.exists() else 0
    await card_store.push_pending_inject(wid, cid, "不该进历史的一段话")
    after = path.stat().st_size if path.exists() else 0
    assert after == before, "注入写进了会话历史"


async def test_inject_mode_never_wakes(work, cfg, monkeypatch):
    """注入不唤醒：即使 wake=True 也不起轮（用户口径「而且不会唤醒」）。"""
    from src.api import message_runner
    from src.infra import harness

    called: list[str] = []

    def _fake_spawn(coro):
        called.append("spawn")
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass

    monkeypatch.setattr(message_runner, "_spawn_tracked", _fake_spawn)
    wid, cid = work["work_id"], work["work_card_id"]
    r = await harness.deliver(wid, cid, "提示", rule_id="p", wake=True, mode="inject")
    assert r and r.startswith("inject:")
    assert called == [], "注入不该唤醒"
