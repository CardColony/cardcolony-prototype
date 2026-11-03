# -*- coding: utf-8 -*-
"""三步凝练（S2a→L1→L2）接入后的验收：**L2 必出、锚点不重复、不变量零违例**。

[三步凝练 2026-10-29] 对照原版 dsh：
- 模型零锚点义务（只给切点 `end` 与文字），覆盖区间与锚点由 `assemble` 机械算；
- L2 子题落成 **level=2 的 gist 子节点**（`condense.ts:3278` 口径），
  不是 archive 物化那套 `description`（anchor 与父同）。
这组用例守的正是「原版一定会产出 L2」这一条。
"""
from __future__ import annotations

import pytest

from src.storage import condense_two_layer as tl
from src.storage import memory_domain as md

from tests._condense_stub import as_llm, three_stage_stub


def _blocks(n: int) -> list[dict]:
    return [{"seq": i, "content": f"第{i}块内容"} for i in range(n)]


async def test_l2_is_produced_and_invariants_hold():
    """一组两子题 ⇒ 1 个 L1 + 2 个 L2，父子互链、覆盖不重叠、I1–I5 零违例。"""
    blocks = _blocks(10)
    subs = [{"title": "子题一", "gist": "细节一", "end": 4},
            {"title": "子题二", "gist": "细节二", "end": 9}]
    stub = three_stage_stub(gist="结论甲", subtopics=subs)

    res = await tl.run_two_layer(blocks, as_llm(stub), session_id="card_x")
    assert res["ok"] is True
    nodes, descs = res["nodes"], res["descriptions"]
    assert len(nodes) == 1, "一组 ⇒ 一个 L1"
    assert len(descs) == 2, "两个子题 ⇒ 两条 L2"

    chain, added = md.attach_descriptions(nodes, descs, "card_x")
    assert added == 2
    assert len(chain) == 3

    l1 = chain[0]
    l2 = chain[1:]
    assert l1["level"] == 1 and not l1["upper"]
    assert [c["level"] for c in l2] == [2, 2]
    assert all(c["role"] == "gist" for c in l2), "L2 是 gist 子节点，不是 description"
    # 锚点：L2 取自己区间首块，与父不同 ⇒ 全链唯一
    assert len({n["anchor"]["seq"] for n in chain}) == 3
    # 父子互链（I1）
    assert all(c["upper"] == [l1["anchor"]] for c in l2)
    assert len(l1["lower"]) == 2
    # 覆盖不重叠（I3）
    cov = [set(c["coveredSeqs"]) for c in l2]
    assert not (cov[0] & cov[1])
    # 不变量全过
    assert md.check_invariants(chain) == []


async def test_multiple_groups_never_share_anchor():
    """多组时 L1 锚点互不相同——「同一 seq 两条结论」在结构上不可能。"""
    blocks = _blocks(12)
    groups = [{"end": 3}, {"end": 7}, {"end": 11}]
    stub = three_stage_stub(gist="结论", groups=groups)

    res = await tl.run_two_layer(blocks, as_llm(stub), session_id="card_y")
    assert res["ok"] is True
    nodes = res["nodes"]
    assert len(nodes) == 3
    seqs = [n["anchor"]["seq"] for n in nodes]
    assert seqs == [0, 4, 8], "锚点＝各组首块"
    assert len(set(seqs)) == 3, "锚点必须唯一"
    assert md.check_invariants(nodes) == []


async def test_s2a_prefix_leaves_tail_for_next_batch():
    """S2a 只定前缀 ⇒ 后续块不被吞掉（尾巴留给下一批）。"""
    blocks = _blocks(10)
    stub = three_stage_stub(gist="结论", groups=[{"end": 3}])   # 只定前 4 块

    res = await tl.run_two_layer(blocks, as_llm(stub), session_id="card_z")
    assert res["ok"] is True
    covered = {s for n in res["nodes"] for s in n["coveredSeqs"]}
    assert covered == {0, 1, 2, 3}, "只覆盖前 4 块"
    assert 9 not in covered, "尾块留待下一批"


async def test_batch_commit_carries_l2_into_chain(sandbox, monkeypatch):
    """走完整批处理路径：L2 随批落进 chain，I1–I5 零违例。

    原单次凝练这条路会怎样：模型自填 anchor → 重复 → 物化撞 I1 → 整批拒写 → L2 全丢。
    现在锚点由 assemble 机械算，这里断言的是「落库后链里真的有两层且自洽」。
    """
    from src.storage import card_store, memory_store, work_store

    wdata = await work_store.create_work("三步凝练验收")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")

    window = [{"seq": i, "role": "user", "actor": "human",
               "content": f"第{i}块内容"} for i in range(6)]
    subs = [{"title": "子题一", "gist": "细节一", "end": 2},
            {"title": "子题二", "gist": "细节二", "end": 5}]
    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="结论甲", subtopics=subs))

    ok = await memory_store._condense_one_batch(wid, "card_001", window, 0)
    assert ok is True

    st = memory_store.load_memory(wid, "card_001")
    assert st["mark"] == 6, "整窗被覆盖 ⇒ 水位推到底"
    entries = st["entries"]
    l1 = [e for e in entries if int(e.get("level", 1) or 1) == 1]
    l2 = [e for e in entries if int(e.get("level", 1) or 1) > 1]
    assert len(l1) == 1 and len(l2) == 2, f"应为 1 结论 + 2 细节，实际 {len(l1)}/{len(l2)}"
    # 落库后仍自洽：锚点唯一、父子互链
    assert md.check_invariants(entries) == []
    assert len({(e.get("anchor") or {}).get("seq") for e in entries}) == 3
