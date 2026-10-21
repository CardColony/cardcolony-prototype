# -*- coding: utf-8 -*-
"""两层凝练（照抄 dsh `condense.ts` 的 S2a → L1 → L2）。

判据：
T1 分组解析：按 end 切；**解析不出 ⇒ 逐块兜底**；尾巴没收进来要补一组
T2 L1：**组数不符 ⇒ None**（整批拒绝）
T3 L2：组数不符 ⇒ None（只丢细节）
T4 装配：结论节点 `coveredSeqs` **机械算**（取组区间）；gist＝完成栏（无则开始栏）
T5 描述层：子题区间**机械算**（从上一个 end+1 起，不信模型给的起点）
T6 编排：L1 失败/组数不符 ⇒ `ok=False`（调用方不得改 mark）；L2 失败只丢细节
"""
from __future__ import annotations

import asyncio
import json

from src.storage import condense_two_layer as tl, memory_domain as md

BLOCKS = [{"seq": s, "content": f"内容{s}"} for s in range(6)]   # 块 0..5


# ---------------------------------------------------------------- T1

def test_parse_groups_by_end():
    g = tl.parse_groups('{"groups":[{"end":1},{"end":3},{"end":5}]}', list(range(6)))
    assert g == [(0, 1), (2, 3), (4, 5)]


def test_parse_groups_falls_back_to_per_block():
    g = tl.parse_groups("模型没按格式回", list(range(3)))
    assert g == [(0, 0), (1, 1), (2, 2)], "解析不出 ⇒ 逐块兜底（宁可多切）"


def test_parse_groups_fills_tail():
    g = tl.parse_groups('{"groups":[{"end":2}]}', list(range(6)))
    assert g == [(0, 2), (3, 5)], "尾巴要补一组"


# ---------------------------------------------------------------- T2/T3

def test_l1_group_count_mismatch_rejected():
    assert tl.parse_l1('{"l1":[{"开始":"a"}]}', 2) is None
    ok = tl.parse_l1('{"l1":[{"开始":"a"},{"完成":"b"}]}', 2)
    assert ok is not None and len(ok) == 2


def test_l2_group_count_mismatch_rejected():
    assert tl.parse_l2('{"l2":[{"subtopics":[]}]}', 2) is None
    ok = tl.parse_l2('{"l2":[{"subtopics":[]},{"subtopics":[]}]}', 2)
    assert ok == [[], []]


# ---------------------------------------------------------------- T4/T5

def test_assemble_mechanical_covered_seqs():
    groups = [(0, 1), (2, 3), (4, 5)]
    l1 = [{"开始": "起1", "经过": "过1", "完成": "果1", "教训": ""},
          {"开始": "起2", "经过": "过2", "完成": "", "教训": "教训2"},   # 没完成 ⇒ ongoing
          {"开始": "起3", "经过": "", "完成": "果3", "教训": ""}]
    nodes, descs = tl.assemble(list(range(6)), groups, l1, None, "s1")
    assert [n["coveredSeqs"] for n in nodes] == [[0, 1], [2, 3], [4, 5]], "覆盖机械算"
    assert nodes[0]["gist"] == "果1", "gist 取完成栏"
    assert nodes[1]["gist"] == "起2", "没有完成栏 ⇒ 取开始栏"
    assert nodes[1]["status"] == "ongoing"
    assert nodes[1]["lesson"] == "教训2"
    assert descs == []


def test_assemble_description_ranges_mechanical():
    groups = [(0, 3)]
    l1 = [{"开始": "起", "经过": "过", "完成": "果", "教训": ""}]
    l2 = [[{"title": "子1", "gist": "细节1", "end": 1},
           {"title": "子2", "gist": "细节2", "end": 3}]]
    nodes, descs = tl.assemble([0, 1, 2, 3], groups, l1, l2, "s1")
    assert [d["coveredSeqs"] for d in descs] == [[0, 1], [2, 3]], "从上一 end+1 起"
    assert [d["seq"] for d in descs] == [0, 2]
    assert descs[0]["parent_seq"] == 0


# ---------------------------------------------------------------- T6

def _runner(script: dict):
    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return script.get("s2a", '{"groups":[{"end":5}]}')
        if "L1 层" in system:
            return script.get("l1", '{"l1":[{"开始":"起","经过":"过","完成":"果"}]}')
        return script.get("l2", '{"l2":[{"subtopics":[{"title":"子","gist":"细节","end":5}]}]}')
    return _call


def test_run_two_layer_happy_path():
    r = asyncio.run(tl.run_two_layer(BLOCKS, _runner({}), "s1"))
    assert r["ok"] is True and len(r["nodes"]) == 1
    assert r["nodes"][0]["coveredSeqs"] == [0, 1, 2, 3, 4, 5]
    assert len(r["descriptions"]) == 1
    assert md.check_invariants(r["nodes"]) == []


def test_run_two_layer_l1_bad_rejects_whole_batch():
    r = asyncio.run(tl.run_two_layer(BLOCKS, _runner({"l1": '{"l1":[]}'}), "s1"))
    assert r["ok"] is False and r["stage"] == "l1"
    assert r["nodes"] == [], "整批作废 ⇒ 调用方不得改 mark"


def test_run_two_layer_l2_failure_keeps_nodes():
    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return '{"groups":[{"end":5}]}'
        if "L1 层" in system:
            return '{"l1":[{"开始":"起","完成":"果"}]}'
        raise RuntimeError("l2 down")
    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is True and len(r["nodes"]) == 1 and r["descriptions"] == []
