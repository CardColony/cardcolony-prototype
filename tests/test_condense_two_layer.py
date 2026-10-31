# -*- coding: utf-8 -*-
"""两层凝练（照抄 dsh `condense.ts` 的 S2a → L1 → L2）。

判据：
T1 分组解析：按 end 切；**非法 ⇒ None**（F3：G-1 闸，整批作废——逐块静默兜底已处死）
T2 L1：**组数不符 ⇒ None**（整批拒绝；F6 低格式优先、JSON 回退）
T3 L2：组数不符 ⇒ None（只丢细节）
T4 装配：结论节点 `coveredSeqs` **机械算**（取组区间）；gist＝完成栏（无则开始栏）
T5 描述层：子题区间**机械算**（从上一个 end+1 起，不信模型给的起点）
T6 编排：L1 失败/组数不符 ⇒ `ok=False`（调用方不得改 mark）；L2 失败只丢细节；
   F7：L1/L2 解析失败同批重试（最多 3 次）超限才作废；F3：S2a 失败重试 1 次后 abort
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


def test_parse_groups_invalid_returns_none():
    """[F3] 解析不出/越界/非递增 ⇒ None（整批作废），不再逐块兜底。"""
    assert tl.parse_groups("模型没按格式回", list(range(3))) is None, "解析不出 ⇒ None"
    assert tl.parse_groups('{"groups":[]}', list(range(3))) is None, "空 groups ⇒ None"
    assert tl.parse_groups('{"groups":[{"end":5}]}', list(range(3))) is None, "越界 ⇒ None"
    assert tl.parse_groups('{"groups":[{"end":2},{"end":1}]}', list(range(3))) is None, \
        "非递增 ⇒ None"
    assert tl.parse_groups('{"groups":[{"end":"x"}]}', list(range(3))) is None, \
        "end 非整数 ⇒ None"
    assert tl.parse_groups('{"groups":[{"end":1},"x"]}', list(range(3))) is None, \
        "组元素非对象 ⇒ None"


def test_parse_groups_keeps_tail_for_next_batch():
    # [2026-10-30] dsh 口径：「被消耗段 = 最后一个 end+1 条，可以是窗口前缀——
    # 拿不准的后段留给下一批」。补尾巴会把模型说的「拿不准」改写成「就是一组」，
    # 等于把两件事并成一条结论。原断言 [(0,2),(3,5)] 是补尾版。
    g = tl.parse_groups('{"groups":[{"end":2}]}', list(range(6)))
    assert g == [(0, 2)], "只消耗模型定的前缀，尾巴留待下一批"


# ---------------------------------------------------------------- T1b 低格式（F6）

def test_parse_low_l1_blocks():
    text = ("<think>思考过程不该吃</think>"
            "【块1】\n开始：起1\n经过：过1\n完成：果1\n"
            "【块2】\n开始：起2\n完成：果2\n教训：训2")
    rows = tl.parse_low_l1(text, 2)
    assert rows is not None and len(rows) == 2
    assert rows[0] == {"开始": "起1", "经过": "过1", "完成": "果1", "教训": ""}
    assert rows[1]["完成"] == "果2" and rows[1]["教训"] == "训2"
    # 块号容忍【块1/3】＋ 标签别名（起因→开始、结果→完成）
    text2 = "【块1/3】\n起因：起\n结果：果"
    rows2 = tl.parse_low_l1(text2, 1)
    assert rows2 is not None and rows2[0]["开始"] == "起" and rows2[0]["完成"] == "果"
    # 组数不符 ⇒ None
    assert tl.parse_low_l1("【块1】\n开始：x", 2) is None


def test_parse_low_l2_blocks():
    text = ("【块1】\n◇ 子题一\n细节正文一（至块2）\n◇ 子题二\n细节正文二\n"
            "【块2】\n（无子题）")
    groups = tl.parse_low_l2(text, 2)
    assert groups is not None
    assert [s["title"] for s in groups[0]] == ["子题一", "子题二"]
    assert groups[0][0]["end"] == 2, "尾标剥离并捕获 end"
    assert groups[0][0]["gist"] == "细节正文一", "尾标不进正文"
    assert groups[1] == [], "无子题声明 ⇒ 空数组"
    # 整块合成单子题兜底：没有 ◇ 也没有声明
    groups2 = tl.parse_low_l2("【块1】\n一整段细节正文", 1)
    assert groups2 is not None and len(groups2[0]) == 1
    assert groups2[0][0]["gist"] == "一整段细节正文"
    # JSON 仍兼容（回退路径）
    groups3 = tl.parse_l2('{"l2":[{"group":1,"subtopics":[{"title":"t","gist":"g","end":1}]}]}', 1)
    assert groups3 is not None and groups3[0][0]["gist"] == "g"
    # L1 JSON 回退
    rows3 = tl.parse_l1('{"l1":[{"开始":"a","经过":"b","完成":"c","教训":""}]}', 1)
    assert rows3 is not None and rows3[0]["完成"] == "c"


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
    # [2026-10-30] 父的首块（0）已被 L1 占作 anchor ⇒ 子题从它**之后**起算，
    # 否则首个子题的 anchor 与父撞号（I1 身份重复）。原断言 [[0,1],[2,3]] 是撞号版。
    assert [d["coveredSeqs"] for d in descs] == [[1], [2, 3]], "父首块归 L1，子题从它之后起"
    assert [d["seq"] for d in descs] == [1, 2]
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
    """[2026-10-31 处置口径] L2 的断连类失败（重试耗尽）⇒ 只丢细节、结论照常；
    失效类（401 等）⇒ 整批上抛报用户（见 test_f8a_truncation...l2 段）。"""
    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return '{"groups":[{"end":5}]}'
        if "L1 层" in system:
            return '{"l1":[{"开始":"起","完成":"果"}]}'
        raise RuntimeError("connection reset by peer")     # 断连类 ⇒ 重试耗尽后丢细节
    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is True and len(r["nodes"]) == 1 and r["descriptions"] == []


def test_run_two_layer_l2_fatal_raises():
    """[处置] L2 段的失效类（401）⇒ 上抛（不再静默丢细节）。"""
    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return '{"groups":[{"end":5}]}'
        if "L1 层" in system:
            return '{"l1":[{"开始":"起","完成":"果"}]}'
        raise RuntimeError("401 unauthorized: key 失效")
    import pytest as _pt
    with _pt.raises(tl.CondenseTruncated.__mro__[1]):  # RuntimeError 系
        asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))


# ---------------------------------------------------------------- T7（F3/F4/F7 · 2026-10-31）

def test_run_two_layer_s2a_invalid_abort_after_retry():
    """[F3] S2a 非法：带错误报告重试 1 次，二次失败 ⇒ 整批作废（ok=False, stage=s2a）。"""
    calls = {"n": 0, "retry_seen": False}

    async def _call(system: str, user: str) -> str:
        calls["n"] += 1
        if "上次产出被拒" in user:
            calls["retry_seen"] = True
        return "彻底不成形的产出"

    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is False and r["stage"] == "s2a" and r["nodes"] == []
    assert calls["n"] == 2, "恰好一次重试（共两次调用）"
    assert calls["retry_seen"], "重试带机械错误报告"


def test_run_two_layer_s2a_recovers_on_retry():
    """[F3] 第一次非法、重试成形 ⇒ 整批继续。"""
    state = {"n": 0}

    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            state["n"] += 1
            if state["n"] == 1:
                return "垃圾"
            return '{"groups":[{"end":5}]}'
        if "L1 层" in system:
            return '{"l1":[{"开始":"起","完成":"果"}]}'
        return '{"l2":[{"subtopics":[]}]}'

    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is True and len(r["nodes"]) == 1


def test_run_two_layer_l1_retries_then_ok():
    """[F7] L1 组数不符 → 同批重试，第 2 次成形 ⇒ ok=True。"""
    state = {"n": 0}

    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return '{"groups":[{"end":2},{"end":5}]}'
        if "L1 层" in system:
            state["n"] += 1
            if state["n"] == 1:
                return '{"l1":[{"开始":"a"}]}'          # 组数不符
            return ('【块1】\n开始：起1\n完成：果1\n【块2】\n开始：起2\n完成：果2')
        return '{"l2":[{"subtopics":[]},{"subtopics":[]}]}'

    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is True and len(r["nodes"]) == 2
    assert r["retries"] == 1 and state["n"] == 2


def test_run_two_layer_l1_retries_exhausted_aborts():
    """[F7] L1 连续 4 次（1+3 重试）不成形 ⇒ 整批作废。"""
    calls = {"l1": 0}

    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return '{"groups":[{"end":2},{"end":5}]}'   # 2 组 ⇒ L1 须回 2 组
        if "L1 层" in system:
            calls["l1"] += 1
            return '{"l1":[{"开始":"a"}]}'              # 永远组数不符
        return '{"l2":[{"subtopics":[]},{"subtopics":[]}]}'

    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is False and r["stage"] == "l1"
    assert calls["l1"] == 1 + tl.MAX_STAGE_RETRIES


def test_run_two_layer_low_format_end_to_end():
    """[F6] 低格式产出全链可解析：S2a JSON + L1 低格式 + L2 低格式（含尾标）。"""
    async def _call(system: str, user: str) -> str:
        if "分块器" in system:
            return '{"groups":[{"end":2},{"end":5}]}'
        if "L1 层" in system:
            return "【块1】\n开始：起1\n经过：过1\n完成：果1\n【块2】\n开始：起2\n完成：果2\n教训：训2"
        return "【块1】\n◇ 子题一\n细节一（至块1）\n【块2】\n（无子题）"

    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert r["ok"] is True and len(r["nodes"]) == 2
    assert r["nodes"][0]["gist"] == "果1"
    assert r["nodes"][1]["lesson"] == "训2"
    assert len(r["descriptions"]) == 1 and r["descriptions"][0]["process"] == "细节一"


def test_run_two_layer_injects_domain_panorama():
    """[F4] domain_full ⇒ 三段消息头都带【memory 全景】。"""
    seen: list[str] = []

    async def _call(system: str, user: str) -> str:
        seen.append(user)
        if "分块器" in system:
            return '{"groups":[{"end":5}]}'
        if "L1 层" in system:
            return '{"l1":[{"开始":"起","完成":"果"}]}'
        return '{"l2":[{"subtopics":[]}]}'

    r = asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1",
                                     domain_full="[seq 0][done][fact] 已有条目"))
    assert r["ok"] is True
    assert len(seen) == 3 and all("【memory 全景】" in u and "已有条目" in u for u in seen), \
        "S2a/L1/L2 三段都注入全景"
    # 不传全景 ⇒ 不注入（老调用方兼容）
    seen.clear()
    asyncio.run(tl.run_two_layer(BLOCKS, _call, "s1"))
    assert all("【memory 全景】" not in u for u in seen)


def test_assemble_scrubs_marker_forms():
    """[F23 同源 · 实测] 模型把【结论】/【依据】抄进字段值 ⇒ 装配时剥除。"""
    groups = [(0, 5)]
    l1 = [{"开始": "【结论】起因是X", "经过": "【依据】条1–条3",
           "完成": "【结论】已交付Y", "教训": ""}]
    nodes, descs = tl.assemble(list(range(6)), groups, l1, None, "s1")
    assert nodes[0]["gist"] == "已交付Y"
    assert nodes[0]["start"] == "起因是X"
    assert nodes[0]["process"] == "条1–条3"
