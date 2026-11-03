# -*- coding: utf-8 -*-
"""干预台账（intervention_store）单测。

守三件事：
  ① 动作按 (fire_key, target) 去重——重启重演同一触发不重复登记；
  ② 事项按 (policy, scope, key) 只许一个 open——重复命中不重复立案；
  ③ 关事项时其上未投完的动作一并标 stale——已解决的问题不再打扰。
"""
from __future__ import annotations

import pytest

from src.infra import intervention_store as ist


@pytest.fixture()
def store(tmp_path):
    s = ist.InterventionStore(works_dir=tmp_path)
    ist.reset(s)
    yield s
    s.close()
    ist.reset(ist.InterventionStore(works_dir=tmp_path))


def test_action_dedup_by_fire_key(store):
    aid1, new1 = store.record_action(
        "w1", policy_id="p1", scope_key="card:c1",
        fire_key="p1|card:c1|c1:7", target="c1", mode="continue",
        wake=True, message="去定稿")
    aid2, new2 = store.record_action(
        "w1", policy_id="p1", scope_key="card:c1",
        fire_key="p1|card:c1|c1:7", target="c1", mode="continue",
        wake=True, message="去定稿")
    assert new1 and not new2 and aid1 == aid2


def test_action_same_fire_other_target_is_separate(store):
    a1, n1 = store.record_action(
        "w1", policy_id="p1", scope_key="card:c1",
        fire_key="p1|card:c1|c1:7", target="c1", mode="message",
        wake=False, message="x")
    a2, n2 = store.record_action(
        "w1", policy_id="p1", scope_key="card:c1",
        fire_key="p1|card:c1|c1:7", target="c2", mode="message",
        wake=False, message="x")
    assert n1 and n2 and a1 != a2


def test_matter_dedup_while_open(store):
    m1, n1 = store.open_matter(
        "w1", policy_id="p1", scope_key="card:r1",
        kind="review_promote", matter_key="b1", objects={"review_card": "r1"})
    m2, n2 = store.open_matter(
        "w1", policy_id="p1", scope_key="card:r1",
        kind="review_promote", matter_key="b1", objects={"review_card": "r1"})
    assert n1 and not n2 and m1 == m2


def test_matter_reopen_after_close(store):
    m1, _ = store.open_matter(
        "w1", policy_id="p1", scope_key="card:r1",
        kind="review_promote", matter_key="b1", objects={})
    assert store.close_matter("w1", m1, state="resolved", evidence={"v": 1})
    m2, n2 = store.open_matter(
        "w1", policy_id="p1", scope_key="card:r1",
        kind="review_promote", matter_key="b1", objects={})
    assert n2 and m2 != m1


def test_close_matter_stales_pending_actions(store):
    mid, _ = store.open_matter(
        "w1", policy_id="p1", scope_key="card:r1",
        kind="review_promote", matter_key="b1", objects={})
    aid, _ = store.record_action(
        "w1", policy_id="p1", scope_key="card:r1",
        fire_key="p1|card:r1|r1:3", target="r1", mode="continue",
        wake=True, message="去定稿", matter_id=mid)
    store.close_matter("w1", mid, state="resolved", evidence={})
    a = store.get_action("w1", aid)
    assert a["state"] == "stale"


def test_pending_and_mark_roundtrip(store):
    aid, _ = store.record_action(
        "w1", policy_id="p1", scope_key="card:c1",
        fire_key="k1", target="c1", mode="message", wake=False, message="x")
    assert [a["action_id"] for a in store.pending_actions("w1")] == [aid]
    assert store.mark_action("w1", aid, "delivered", receipt={"result": "5"})
    assert store.pending_actions("w1") == []
    a = store.get_action("w1", aid)
    assert a["state"] == "delivered" and a["receipt"]["result"] == "5"


# ---------------------------------------------------------- 错误台账
def test_error_record_and_dedup(store):
    """同一签名去重累计：count++、last_ts 刷新、凭据取最新。"""
    assert store.record_error(
        "w1", stage="deliver", kind="delivery_failed",
        action_id="act-1", detail={"result": ""})
    errs = store.list_errors("w1")
    assert len(errs) == 1 and errs[0]["count"] == 1
    assert errs[0]["stage"] == "deliver" and errs[0]["action_id"] == "act-1"

    assert store.record_error(
        "w1", stage="deliver", kind="delivery_failed",
        action_id="act-1", detail={"result": "第二次"})
    errs = store.list_errors("w1")
    assert len(errs) == 1 and errs[0]["count"] == 2
    assert errs[0]["detail"]["result"] == "第二次"
    assert errs[0]["first_ts"] <= errs[0]["last_ts"]


def test_error_distinct_signatures_and_key(store):
    """不同归属/不同 key 各自一行；key 让同类不同错不并账。"""
    store.record_error("w1", stage="config", kind="load_error",
                       policy_id="p1", key="p1: watch 必须是列表")
    store.record_error("w1", stage="config", kind="load_error",
                       policy_id="p2", key="p2: scope.kind 非法")
    store.record_error("w1", stage="resolve", kind="RuntimeError",
                       policy_id="p1")
    errs = store.list_errors("w1")
    assert len(errs) == 3
    assert {e["stage"] for e in errs} == {"config", "resolve"}


def test_error_string_detail_and_truncation(store):
    """字符串 detail 也收；超长 detail 截断不炸。"""
    assert store.record_error("w1", stage="check", kind="check_exc",
                              detail="x" * 5000)
    assert store.record_error("w1", stage="check", kind="check_exc",
                              matter_id="m1", detail={"error": "boom"})
    errs = store.list_errors("w1")
    assert len(errs) == 2
    assert all(isinstance(e["detail"], dict) for e in errs)
