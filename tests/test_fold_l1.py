# -*- coding: utf-8 -*-
"""低频归拢（照抄 dsh `condense/fold-l1.ts`）。

判据：
F1 闸：每 5 批一次
F2 可归拢顶层：level=1 ∧ gist ∧ upper 空 ∧ done ∧ 未沉底
F3 顶层 < 2 ⇒ judge-empty（**零模型调用**）
F4 模型说没得并 ⇒ judge-empty（链不动）
F5 合并：新节点 + 旧节点标 supersededBy（**不删**）+ 覆盖区间合并
F6 模型输出不成形/抛错 ⇒ abort，**链原样**
F7 「三数」：merges / 点名 / 落地；点不中**照样建节点**（dsh 09-24 裁定）
"""
from __future__ import annotations

import json

from src.storage import fold_l1, memory_domain as md


def _top(seq: int, gist: str, *, status="done", level=1, covered=None, **kw) -> dict:
    n = md.new_node("s1", seq, gist, kind="decision", status=status)
    n["level"] = level
    n["upper"], n["lower"] = [], []
    n["coveredSeqs"] = covered if covered is not None else [seq]
    n.update(kw)
    return n


# ---------------------------------------------------------------- F1/F2

def test_should_fold_every_five_batches():
    assert fold_l1.should_fold(4, 0) is False
    assert fold_l1.should_fold(5, 0) is True
    assert fold_l1.should_fold(9, 5) is False
    assert fold_l1.should_fold(10, 5) is True


def test_foldable_tops_filter():
    chain = [
        _top(1, "甲"),
        _top(2, "乙", status="ongoing"),      # 未完成 ⇒ 不可归拢
        _top(3, "丙", sunk=True),             # 沉底 ⇒ 不可归拢
        _top(4, "丁", level=2),               # 非顶层 ⇒ 不可归拢
    ]
    assert [n["anchor"]["seq"] for n in fold_l1.foldable_tops(chain)] == [1]


# ---------------------------------------------------------------- F3

def test_judge_empty_without_calling_model():
    called = []
    chain, rep = fold_l1.run_fold([_top(1, "只有一条")],
                                  lambda p: called.append(1) or '{"merges":[]}')
    assert rep["outcome"] == "judge-empty" and rep["reason"] == "tops<2"
    assert called == [], "顶层<2 必须零模型调用"


# ---------------------------------------------------------------- F4

def test_no_merge_keeps_chain():
    chain = [_top(1, "甲"), _top(2, "乙")]
    before = json.dumps(chain, ensure_ascii=False, sort_keys=True)
    out, rep = fold_l1.run_fold(chain, lambda p: '{"merges":[]}')
    assert rep["outcome"] == "judge-empty"
    assert json.dumps(out, ensure_ascii=False, sort_keys=True) == before, "链一字不动"


# ---------------------------------------------------------------- F5/F7

def test_merge_creates_node_and_marks_superseded():
    chain = [_top(1, "甲事开始", covered=[1, 2]), _top(3, "甲事完成", covered=[3, 4]),
             _top(9, "另一件不相干")]
    payload = {"merges": [{"covers": [{"seq": 1}, {"seq": 3}],
                           "gist": "甲事：起因→做完", "start": "起因",
                           "process": "过程", "result": "结果", "kind": "decision"}]}
    out, rep = fold_l1.run_fold(chain, lambda p: json.dumps(payload, ensure_ascii=False))
    assert rep["outcome"] == "committed"
    assert (rep["merges"], rep["named"], rep["landed"]) == (1, 2, 2)
    merged = [n for n in out if n.get("supersedes")]
    assert len(merged) == 1
    assert merged[0]["coveredSeqs"] == [1, 2, 3, 4], "覆盖区间取并"
    assert merged[0]["level"] == 1 and merged[0]["upper"] == []
    olds = [n for n in out if int(n["anchor"]["seq"]) in (1, 3)]
    assert all(n.get("supersededBy") for n in olds), "旧节点标 supersededBy"
    assert len(out) == 4, "旧节点不删"
    assert md.check_invariants(out) == [], md.check_invariants(out)


def test_missed_targets_still_create_node():
    """点不中（链上没有该 seq）**照样建节点**（dsh 09-24 裁定），并记落空原因。"""
    chain = [_top(1, "甲"), _top(2, "乙")]
    payload = {"merges": [{"covers": [{"seq": 1}, {"seq": 77}], "gist": "并后"}]}
    out, rep = fold_l1.run_fold(chain, lambda p: json.dumps(payload, ensure_ascii=False))
    assert rep["landed"] == 1 and len(rep["missed"]) == 1
    assert rep["missed"][0]["seq"] == 77 and rep["missed"][0]["why"]
    assert any(n.get("supersedes") for n in out), "点不中也建节点"


# ---------------------------------------------------------------- F6

def test_bad_output_aborts_without_touching_chain():
    chain = [_top(1, "甲"), _top(2, "乙")]
    before = json.dumps(chain, ensure_ascii=False, sort_keys=True)
    out, rep = fold_l1.run_fold(chain, lambda p: "我建议把这两条合并一下")
    assert rep["outcome"] == "abort" and rep["reason"] == "bad-output"
    assert json.dumps(out, ensure_ascii=False, sort_keys=True) == before


def test_llm_error_aborts():
    def _boom(_p):
        raise RuntimeError("network down")
    chain = [_top(1, "甲"), _top(2, "乙")]
    out, rep = fold_l1.run_fold(chain, _boom)
    assert rep["outcome"] == "abort" and rep["reason"] == "llm-error"
    assert len(out) == 2
