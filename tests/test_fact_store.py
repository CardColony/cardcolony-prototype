# -*- coding: utf-8 -*-
"""数据集（fact_store · sqlite 版）单测：部件化、时间窗、跨卡、淘汰、增量重建。

这些断言守的是「条件能不能被问出来」，不是"代码跑得通"。
"""
from __future__ import annotations

import time

import pytest

from src.infra import fact_store as fs


def _rec(seq: int, t, type_: str, data: dict, card: str = "card_001",
         run: str = "r1", round_: int = 1) -> dict:
    return {"seq": seq, "ts": t, "type": type_, "data": data,
            "card_id": card, "run_id": run, "round": round_}


@pytest.fixture()
def store(tmp_path):
    st = fs.FactStore(works_dir=tmp_path, retention_sec=7 * 86400)
    yield st
    st.close()


# ---------------------------------------------------------------- 部件化
def test_flatten_takes_parts_not_payload(store):
    """消息事件：身份零件进来，正文原文不进来。"""
    store.ingest("w", _rec(1, time.time(), "msg_directive", {
        "content": "这是正文，不该进条件面",
        "from": "card_009", "to": "card_010",
        "msg_type": "directive", "msg_id": "12", "reply_to": "7",
    }))
    f = store.query("w")[0]
    assert f["from"] == "card_009"
    assert f["to"] == "card_010"
    assert f["msg_type"] == "directive"
    assert f["msg_id"] == "12"
    assert f["reply_to"] == "7"
    assert "content" not in f, "原文进了条件面"


def test_payload_object_is_not_flattened(store):
    """``goal`` 在契约里是 payload（原文对象）⇒ 一个键都不该进条件面。"""
    store.ingest("w", _rec(1, time.time(), "goal/change", {
        "kind": "goal", "operation": "update", "version": 3,
        "goal": {"objective": "很长的正文"},
        "roundsStarted": 2,
    }))
    f = store.query("w")[0]
    assert f["kind"] == "goal"
    assert f["operation"] == "update"
    assert f["version"] == 3
    assert f["roundsStarted"] == 2
    assert "goal" not in f and "goal.objective" not in f, "payload 对象进了条件面"


def test_tool_result_parts_only(store):
    """工具结果：ok/tool/elapsed 进来，result 原文不进来。"""
    store.ingest("w", _rec(1, time.time(), "tool_result", {
        "tool": "edit_card", "ok": True, "elapsed_ms": 12,
        "result": "巨大的输出正文" * 100,
    }))
    f = store.query("w")[0]
    assert f["tool"] == "edit_card" and f["ok"] is True
    assert f["elapsed_ms"] == 12
    assert "result" not in f


# ---------------------------------------------------------------- 时间窗
def test_time_window_slices_by_time_not_by_count(store):
    """【24小时内】必须是时间，不是"最近 N 条"。"""
    now = time.time()
    store.ingest("w", _rec(1, now - 30 * 3600, "done", {}))
    store.ingest("w", _rec(2, now - 2 * 3600, "done", {}))
    store.ingest("w", _rec(3, now - 60, "done", {}))

    day = store.query("w", since=now - 24 * 3600)
    assert [f["seq"] for f in day] == [2, 3], "24 小时窗切错了"

    week = store.query("w", since=now - 7 * 86400)
    assert [f["seq"] for f in week] == [1, 2, 3]


def test_undated_fact_stays_out_of_window(store):
    """解析不出时间的记录：不进时间窗，也不被淘汰（宁可多留）。

    ``since`` 一旦给出就是"时间窗"语义——没有时间戳的事实一律不进来；
    不带 ``since`` 才是"全部事实"，那时它该在（它确实是一条事实）。
    """
    store.ingest("w", {"seq": 1, "ts": "", "type": "done", "data": {}, "card_id": "c"})
    store.ingest("w", _rec(2, time.time(), "done", {}))
    assert [f["seq"] for f in store.query("w", since=0)] == [2]
    assert {f["seq"] for f in store.query("w")} == {1, 2}
    assert [f["seq"] for f in store.undated("w")] == [1]


# ---------------------------------------------------------------- 跨卡
def test_cross_card_query_finds_parent_to_child(store):
    """【父卡】对【子卡】做了什么——一次查询拿到，不用读多个文件。"""
    now = time.time()
    store.ingest("w", _rec(1, now - 100, "msg_directive",
                           {"from": "card_001", "to": "card_005",
                            "msg_type": "directive"}, card="card_005"))
    store.ingest("w", _rec(2, now - 90, "msg_directive",
                           {"from": "card_001", "to": "card_006",
                            "msg_type": "directive"}, card="card_006"))
    store.ingest("w", _rec(3, now - 80, "msg_directive",
                           {"from": "card_009", "to": "card_005",
                            "msg_type": "directive"}, card="card_005"))
    store.ingest("w", _rec(4, now - 70, "tool_call",
                           {"tool": "create_card", "target": "card_007"},
                           card="card_001"))

    hits = store.query("w", types=["msg_directive"], from_="card_001",
                       since=now - 24 * 3600)
    assert {f["to"] for f in hits} == {"card_005", "card_006"}
    assert all(f["from"] == "card_001" for f in hits)


def test_cards_filter_takes_a_group(store):
    """``cards`` 收一组卡：父卡查「子卡们」发生了什么。"""
    now = time.time()
    for i, c in enumerate(("c1", "c2", "c3", "c4")):
        store.ingest("w", _rec(i, now - 50 + i, "card_created",
                               {"by": "parent", "cause": "system"}, card=c))
    hits = store.query("w", cards=["c1", "c3"], where={"by": "parent"})
    assert {f["card"] for f in hits} == {"c1", "c3"}


def test_where_supports_comparators(store):
    """字段条件支持比较器（"失败过"这类判断不用写死字符串）。"""
    now = time.time()
    store.ingest("w", _rec(1, now - 10, "tool_result",
                           {"tool": "report_up", "ok": False}, card="c"))
    store.ingest("w", _rec(2, now - 5, "tool_result",
                           {"tool": "report_up", "ok": True}, card="c"))
    bad = store.query("w", where={"tool": "report_up", "ok": {"eq": False}})
    assert [f["seq"] for f in bad] == [1]


def test_where_in_list(store):
    """``in`` 用来表达「子卡名单」这种集合条件。"""
    now = time.time()
    store.ingest("w", _rec(1, now - 9, "card_created", {"by": "p"}, card="c1"))
    store.ingest("w", _rec(2, now - 8, "card_created", {"by": "p"}, card="c9"))
    hits = store.query("w", where={"card": {"in": ["c1", "c2"]}})
    assert [f["card"] for f in hits] == ["c1"]


# ---------------------------------------------------------------- 唯一性
def test_same_seq_on_different_cards_do_not_collide(store):
    """seq 只在**卡内**唯一：不同卡的同号事实必须都在。

    实测数据里 card_001 / shadow_card_001 / card_002 全都是 seq=1——
    主键若只用 seq，会互相覆盖（查一类事件时静默少数据）。
    """
    now = time.time()
    for c in ("card_001", "shadow_card_001", "card_002"):
        store.ingest("w", _rec(1, now, "card_created",
                               {"by": "root", "cause": "system"}, card=c))
    hits = store.query("w", types=["card_created"])
    assert {f["card"] for f in hits} == {"card_001", "shadow_card_001", "card_002"}


# ---------------------------------------------------------------- 有界
def test_evict_drops_oldest_only(store):
    """保留期到了删掉最老的；窗口内的一个不动。"""
    now = time.time()
    store.ingest("w", _rec(1, now - 8 * 86400, "done", {}))
    store.ingest("w", _rec(2, now - 6 * 86400, "done", {}))
    store.ingest("w", _rec(3, now - 60, "done", {}))
    n = store.evict("w", now=now)
    assert n == 1
    assert [f["seq"] for f in store.query("w")] == [2, 3]
    assert [f["seq"] for f in store.query("w", types=["done"])] == [2, 3]


# ---------------------------------------------------------------- 逐字流
def test_token_frames_are_not_collected(store):
    """逐字流帧不进数据集：它是渲染增量，不是事实（实测占 work_027 条数 83%）。"""
    now = time.time()
    store.ingest("w", _rec(1, now, "reasoning_chunk", {"text": "逐字", "round": 1}))
    store.ingest("w", _rec(2, now, "content_chunk", {"text": "逐字"}))
    store.ingest("w", _rec(3, now, "tool_call", {"tool": "bash"}))
    assert [f["type"] for f in store.query("w")] == ["tool_call"]


def test_rebuild_skips_token_frames_without_parsing(tmp_path):
    """重建时逐字流帧按行首跳过（不 json.loads），但同一文件里的事实照收。"""
    import json

    now = time.time()
    p = tmp_path / "c.jsonl"
    lines = []
    for i in range(10):
        lines.append(json.dumps({"seq": i, "ts": now - 100 + i,
                                 "type": "reasoning_chunk",
                                 "data": {"text": "x" * 500}, "card_id": "c"}))
    lines.append(json.dumps({"seq": 99, "ts": now, "type": "done",
                             "data": {"card_type": "worker"}, "card_id": "c"}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = fs.FactStore(works_dir=tmp_path / "works")
    fed = fs.rebuild_from_file(st, "w", p, incremental=False)
    assert fed == 1, f"应该只喂 1 条事实，实际 {fed}"
    assert [f["type"] for f in st.query("w")] == ["done"]
    st.close()


def test_rebuild_reads_tail_only(tmp_path):
    """倒读重建：只吃窗口内的事实，超出窗口的一条不进。"""
    import json

    now = time.time()
    p = tmp_path / "card_001.jsonl"
    lines = []
    for i in range(40):
        lines.append(json.dumps({
            "seq": i, "ts": now - (40 - i) * 3600, "type": "done",
            "data": {"rounds": i}, "card_id": "card_001"}, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = fs.FactStore(works_dir=tmp_path / "works")
    fed = fs.rebuild_from_file(st, "w", p, since=now - 24 * 3600, incremental=False)
    assert fed == 24, f"重建吃了 {fed} 条，应该只吃 24 小时内的"
    got = st.query("w")
    assert all(f["_t"] >= now - 24 * 3600 - 1 for f in got)
    st.close()


def test_rebuild_is_incremental(tmp_path):
    """文件没长过 ⇒ 第二次重建零劳动；长了 ⇒ 只读新增那段。"""
    import json

    now = time.time()
    p = tmp_path / "card_001.jsonl"
    p.write_text("\n".join(json.dumps({
        "seq": i, "ts": now - 100 + i, "type": "done",
        "data": {"rounds": i}, "card_id": "card_001"}) for i in range(5)) + "\n",
        encoding="utf-8")

    st = fs.FactStore(works_dir=tmp_path / "works")
    first = fs.rebuild_from_file(st, "w", p)
    assert first == 5
    assert fs.rebuild_from_file(st, "w", p) == 0, "没变化却又读了"

    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 9, "ts": now, "type": "done",
                             "data": {"rounds": 9}, "card_id": "card_001"}) + "\n")
    added = fs.rebuild_from_file(st, "w", p)
    assert added == 1, f"增量应该只读 1 条，实际 {added}"
    assert len(st.query("w")) == 6
    st.close()


# ---------------------------------------------------------------- 边界
def test_db_lives_under_work_dir(tmp_path):
    """库落在 ``<works根>_data/<work>/facts.db``（施工目录外）——**不碰 cards.db、不碰 events**。"""
    st = fs.FactStore(works_dir=tmp_path)
    st.ingest("work_x", _rec(1, time.time(), "done", {}))
    # [库搬出施工目录 2026-11-07] facts.db 不再在 works/{wid}/ 里，
    # 挪到平级的 {works根名}_data/{wid}/——卡 exec 摸不到。
    assert st.db_path("work_x") == (
        tmp_path.parent / f"{tmp_path.name}_data" / "work_x" / "facts.db")
    assert st.db_path("work_x").exists()
    assert not (tmp_path / "work_x" / "facts.db").exists()
    assert not (tmp_path / "work_x" / "cards.db").exists()
    st.close()


def test_clear_only_touches_own_db(tmp_path):
    """清空只清事实表，不删库文件、不碰别的东西。"""
    st = fs.FactStore(works_dir=tmp_path)
    st.ingest("work_x", _rec(1, time.time(), "done", {}))
    st.clear("work_x")
    assert st.query("work_x") == []
    assert st.db_path("work_x").exists()
    st.close()
