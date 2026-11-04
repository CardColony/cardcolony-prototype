# -*- coding: utf-8 -*-
"""条件集（值班员与小本子）单测。

守的是三件事：
  ① 本子是**每个作用域一份**（一对父子一本），不是每条策略一份；
  ② 触发后**只撕那一本**，别的本子不动；
  ③ 超窗的老账在下次记账时惰性划掉，没有事件时本子不动。
"""
from __future__ import annotations

import time

import pytest

from src.infra import policy_set as ps


def _fact(type_: str, card: str, t: float | None = None, **data) -> dict:
    f = {"type": type_, "card": card, "_t": (t if t is not None else time.time()),
         "ts": "", "work": "w"}
    f.update(data)
    return f


def _parents(mapping: dict[str, str]):
    return lambda work, card: mapping.get(str(card), "")


def _policy(**kw) -> dict:
    base = {
        "id": "p1",
        "active": True,
        "scope": {"kind": "card"},
        "watch": ["tool_call"],
        "window": "24h",
        "when": {"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2},
        "then": {"target": "self", "message": "x"},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 时间窗
@pytest.mark.parametrize("raw,sec", [
    ("24h", 86400), ("7d", 7 * 86400), ("90m", 5400), ("45s", 45),
    ("1w", 7 * 86400), (None, None), ("none", None), ("", None),
])
def test_window_parsing(raw, sec):
    assert ps.parse_window(raw) == sec


def test_window_rejects_garbage():
    with pytest.raises(ValueError):
        ps.parse_window("24 小时")


# ---------------------------------------------------------------- 加载
def test_bad_policy_does_not_break_table():
    """坏条目只标错跳过，其余照常工作——一条写错不该让整排值班员停工。"""
    st = ps.PolicySet()
    errs = st.load([
        _policy(id="good"),
        {"id": "bad_scope", "scope": {"kind": "galaxy"},
         "when": {}, "then": {}},
        {"id": "no_then", "when": {}},
        {"id": "good"},                       # id 重复
    ])
    assert any("bad_scope" in e for e in errs)
    assert any("no_then" in e for e in errs)
    assert any("重复" in e for e in errs)
    assert [p.id for p in st.policies()] == ["good"], "坏条目把好条目挤掉了"


def test_inactive_policy_is_not_indexed():
    st = ps.PolicySet()
    st.load([_policy(id="off", active=False)])
    assert st.stats()["active"] == 0
    assert st.dispatch("w", _fact("tool_call", "c1")) == []


# ---------------------------------------------------------------- card 作用域
def test_card_scope_grows_state(monkeypatch):
    """事件进来就往本子上记，凑够条件才触发。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy()])

    assert st.dispatch("w", _fact("tool_call", "c1", now)) == []
    fired = st.dispatch("w", _fact("tool_call", "c1", now))
    assert len(fired) == 1
    assert fired[0]["policy"] == "p1"
    assert fired[0]["scope"] == ["c1"]


def test_watch_narrows_which_policies_see_event():
    """``watch`` 之外的事件，值班员连看都不看。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(watch=["tool_call"],
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 1})])
    assert st.dispatch("w", _fact("done", "c1", now)) == []
    assert st.states() == [], "不关心的事件也记了账"
    assert len(st.dispatch("w", _fact("tool_call", "c1", now))) == 1


# ---------------------------------------------------------------- relation 作用域
def test_relation_scope_is_per_parent_child_pair():
    """「父卡对子卡」的状态挂在**每一对**上，不是某一张卡上。"""
    now = time.time()
    st = ps.PolicySet(parent_provider=_parents({
        "card_002": "card_001", "card_003": "card_001"}),
        now_fn=lambda: now)
    st.load([_policy(
        id="rel", scope={"kind": "relation"}, watch=["msg_directive"],
        when={"count": {"types": ["msg_directive"]}, "cmp": ">=", "value": 1})])

    # 父卡 card_001 给 card_002 派活
    fired = st.dispatch("w", _fact("msg_directive", "card_002", now,
                                   **{"from": "card_001", "to": "card_002"}))
    assert fired and fired[0]["scope"] == ["card_001", "card_002"]

    # card_003 那本没被碰过（虽然同一个父卡）
    keys = {tuple(s["scope"]) for s in st.states()}
    assert keys == {("card_001", "card_002")}, f"串本子了: {keys}"


def test_relation_scope_covers_create_and_message():
    """建卡（by=父卡，事件落在子卡）和消息（from→to）都要落到同一对父子。"""
    now = time.time()
    st = ps.PolicySet(parent_provider=_parents({"card_002": "card_001"}),
                      now_fn=lambda: now)
    st.load([_policy(
        id="rel", scope={"kind": "relation"}, watch=["card_created", "msg_directive"],
        when={"count": {}, "cmp": ">=", "value": 2})])

    st.dispatch("w", _fact("card_created", "card_002", now, by="card_001"))
    fired = st.dispatch("w", _fact("msg_directive", "card_002", now,
                                   **{"from": "card_001", "to": "card_002"}))
    assert fired and fired[0]["scope"] == ["card_001", "card_002"]


def test_new_child_gets_its_own_state():
    """新子卡建出来，自动多一本空本子——不需要谁去登记。"""
    now = time.time()
    parents = {"card_002": "card_001"}
    st = ps.PolicySet(parent_provider=lambda w, c: parents.get(c, ""),
                      now_fn=lambda: now)
    st.load([_policy(
        id="rel", scope={"kind": "relation"}, watch=["card_created"],
        when={"count": {"types": ["card_created"]}, "cmp": ">=", "value": 2})])

    st.dispatch("w", _fact("card_created", "card_002", now, by="card_001"))
    parents["card_003"] = "card_001"          # 新子卡
    st.dispatch("w", _fact("card_created", "card_003", now, by="card_001"))

    keys = {tuple(s["scope"]) for s in st.states()}
    assert keys == {("card_001", "card_002"), ("card_001", "card_003")}


# ---------------------------------------------------------------- 惰性淘汰
def test_lazy_prune_drops_old_entries():
    """超窗的老账在下次记账时划掉；不记账时本子不动。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2})])

    st.dispatch("w", _fact("tool_call", "c1", now - 2 * 3600))   # 2 小时前：立刻出窗
    snap = st.states()[0]
    assert snap["entries"] == 0, "超窗的账没划掉"
    assert snap["counters"] == {}

    st.dispatch("w", _fact("tool_call", "c1", now))
    assert st.states()[0]["entries"] == 1


def test_prune_keeps_window_entries():
    """窗口内的账一条不划。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99})])
    for i in range(5):
        st.dispatch("w", _fact("tool_call", "c1", now - i * 60))
    assert st.states()[0]["entries"] == 5


# ---------------------------------------------------------------- 触发与清零
def test_fire_clears_only_that_state():
    """触发后只撕那一本；别的本子原样不动。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2})])

    st.dispatch("w", _fact("tool_call", "c1", now))
    st.dispatch("w", _fact("tool_call", "c2", now))      # c2 记 1 笔
    fired = st.dispatch("w", _fact("tool_call", "c1", now))   # c1 凑够 2 笔
    assert fired and fired[0]["scope"] == ["c1"]

    snaps = {tuple(s["scope"]): s["entries"] for s in st.states()}
    assert snaps[("c1",)] == 0, "触发后没撕本子"
    assert snaps[("c2",)] == 1, "把别人的本子也撕了"


def test_after_clear_needs_to_accumulate_again():
    """撕了本子就要重新攒——不会因为同一批账反复触发。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2})])
    st.dispatch("w", _fact("tool_call", "c1", now))
    assert len(st.dispatch("w", _fact("tool_call", "c1", now))) == 1
    assert st.dispatch("w", _fact("tool_call", "c1", now)) == [], "撕本子后还能触发"
    assert len(st.dispatch("w", _fact("tool_call", "c1", now))) == 1


# ---------------------------------------------------------------- 停用
def test_deactivate_stops_feeding_and_drops_state():
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99})])
    st.dispatch("w", _fact("tool_call", "c1", now))
    assert len(st.states()) == 1

    assert st.deactivate("p1") is True
    assert st.states() == [], "停用后本子没摘掉"
    assert st.dispatch("w", _fact("tool_call", "c1", now)) == [], "停用后还在喂事件"

    assert st.activate("p1") is True
    assert len(st.dispatch("w", _fact("tool_call", "c1", now))) == 0
    assert len(st.states()) == 1, "重新激活后不记新账了"


# ---------------------------------------------------------------- 回溯
def test_seed_builds_state_then_check_fires():
    """新建策略：回溯历史补记本子；补完若已满足，立刻触发，不用等下一个事件。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 3})])

    fed = st.seed("w", [
        _fact("tool_call", "c1", now - 300),
        _fact("tool_call", "c1", now - 200),
        _fact("tool_call", "c1", now - 100),
        _fact("tool_call", "c2", now - 50),
    ])
    assert fed.get("p1") == 4
    assert st.check_all("w"), "回溯完已满足却没立刻触发"
    # 触发后清零，再 check 不该重复
    assert st.check_all("w") == []


def test_seed_respects_window():
    """回溯只补窗口内的账，超窗的不进本子。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2})])
    st.seed("w", [
        _fact("tool_call", "c1", now - 5 * 3600),
        _fact("tool_call", "c1", now - 4 * 3600),
        _fact("tool_call", "c1", now - 60),
    ])
    snaps = st.states()
    assert len(snaps) == 1
    assert snaps[0]["entries"] == 1, "回溯把超窗的老账也补进去了"


# ---------------------------------------------------------------- 配置
def test_config_file_loads_and_is_valid():
    """仓库里的配置本身必须是合法的（改坏了测试就红）。

    直接读 ``DEFAULT_PATH`` 的仓库文件——conftest 沙箱把
    ``ECO_POLICIES_PATH`` 指向空策略表（防用例真点火），本用例验的
    恰是仓库真配置，不能走 env 指向。"""
    import json
    from src.infra import policy_config
    raw = json.loads(policy_config.DEFAULT_PATH.read_text(encoding="utf-8"))
    items = raw.get("policies") if isinstance(raw, dict) else raw
    st = ps.PolicySet()
    errs = st.load([x for x in (items or []) if isinstance(x, dict)])
    assert errs == [], f"配置有错: {errs}"
    assert st.policies(), "配置里一条策略都没有"


def test_late_event_does_not_stick_outside_window():
    """**迟到事件**必须插到正确位置，否则永远划不掉、窗口失效。

    `prune` 只从头部剔（那是 O(n²) 修掉后的做法），前提是 entries 升序。
    如果 `add` 无脑追加，一条比现有都旧的迟到事件会滞留在**尾部**——
    `prune` 永远看不到它，它会被永久计入 count。

    这个用例是子 agent 读工具描述时抓出来的：描述里写「entries 按时间升序」，
    但当时的实现是 `append`，两句话对不上。
    """
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99})])

    st.dispatch("w", _fact("tool_call", "c1", now))                  # 新
    st.dispatch("w", _fact("tool_call", "c1", now - 3 * 86400))      # 迟到的老事件
    snap = st.states()[0]
    assert snap["entries"] == 1, f"迟到事件滞留了: {snap}"
    assert snap["counters"] == {"tool_call": 1}, snap


def test_out_of_order_events_keep_entries_sorted():
    """乱序进来也要保持升序——`after` 依赖「新→旧」的索引顺序。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99})])
    for off in (0, -60, -120, -30, -90):
        st.dispatch("w", _fact("tool_call", "c1", now + off))
    entries = st.states()[0]["entries"]
    assert entries == 5
    # 内部顺序：_t 升序
    ts = [e["_t"] for e in st._states[("w", "p1", ("c1",))].entries]
    assert ts == sorted(ts), f"entries 不是升序: {ts}"


def test_late_event_does_not_stick_outside_window():
    """**迟到事件**必须插到正确位置，否则永远划不掉、窗口失效。

    `prune` 只从头部剔（那是 O(n²) 修掉后的做法），前提是 entries 升序。
    如果 `add` 无脑追加，一条比现有都旧的迟到事件会滞留在**尾部**——
    `prune` 永远看不到它，它会被永久计入 count。

    这个用例是子 agent 读工具描述时抓出来的：描述里写「entries 按时间升序」，
    但当时的实现是 `append`，两句话对不上。
    """
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99})])

    st.dispatch("w", _fact("tool_call", "c1", now))                  # 新
    st.dispatch("w", _fact("tool_call", "c1", now - 3 * 86400))      # 迟到的老事件
    snap = st.states()[0]
    assert snap["entries"] == 1, f"迟到事件滞留了: {snap}"
    assert snap["counters"] == {"tool_call": 1}, snap


def test_out_of_order_events_keep_entries_sorted():
    """乱序进来也要保持升序——`after` 依赖「新→旧」的索引顺序。"""
    now = time.time()
    st = ps.PolicySet(now_fn=lambda: now)
    st.load([_policy(window="1h",
                     when={"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 99})])
    for off in (0, -60, -120, -30, -90):
        st.dispatch("w", _fact("tool_call", "c1", now + off))
    entries = st.states()[0]["entries"]
    assert entries == 5
    # 内部顺序：_t 升序
    ts = [e["_t"] for e in st._states[("w", "p1", ("c1",))].entries]
    assert ts == sorted(ts), f"entries 不是升序: {ts}"

# ------------------------------------------------- 现状超长提醒（仓库真配置）
def _real_policies() -> list[dict]:
    """直读仓库配置——绕 conftest 的 ``ECO_POLICIES_PATH`` 空表
    （这组用例钉的是真策略本身）。"""
    import json
    from src.infra import policy_config
    raw = json.loads(policy_config.DEFAULT_PATH.read_text(encoding="utf-8"))
    items = raw.get("policies") if isinstance(raw, dict) else raw
    return [x for x in (items or []) if isinstance(x, dict)]


def test_status_oversize_reminder_fires_every_time():
    """status_block_oversize_reminder：status_block 且 new_len>=1 万 ⇒ 命中；
    不节流——超阈值期间每次编辑现状都再提醒一次（注意力会漂，催到底）。"""
    st = ps.PolicySet()
    assert st.load(_real_policies()) == []

    fired = st.dispatch("w1", _fact(
        "card_block_edited", "c1", block="status_block",
        new_len=12000, by="c1", cause="self"))
    hits = [f["policy"] for f in fired]
    assert "status_block_oversize_reminder" in hits
    f = next(f for f in fired
             if f["policy"] == "status_block_oversize_reminder")
    assert f["then"]["wake"] is True and f["then"]["target"] == "self"

    fired2 = st.dispatch("w1", _fact(
        "card_block_edited", "c2", block="status_block", new_len=500))
    assert "status_block_oversize_reminder" not in \
        [f["policy"] for f in fired2]

    # c3 已被提醒过、又写了一次超阈值的现状 ⇒ 再提醒一次（不节流）
    st.dispatch("w1", _fact(
        "harness_message", "c3",
        rule_id="status_block_oversize_reminder", to="c3"))
    fired3 = st.dispatch("w1", _fact(
        "card_block_edited", "c3", block="status_block", new_len=12000))
    assert "status_block_oversize_reminder" in \
        [f["policy"] for f in fired3]


def test_status_oversize_ignores_other_blocks():
    """使命/需求块写再长也不管——只盯 status_block。"""
    st = ps.PolicySet()
    st.load(_real_policies())
    fired = st.dispatch("w1", _fact(
        "card_block_edited", "c4", block="mission", new_len=30000))
    assert "status_block_oversize_reminder" not in \
        [f["policy"] for f in fired]

