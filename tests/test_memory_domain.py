# -*- coding: utf-8 -*-
"""记忆因果链 · 五条不变量回归（2026-10-20 · 照抄 dsh memory-domain + entry-invariants）。

判据：I1 互链／I2 层级差／I3 同层收敛／I4 顶层／I5 森林，逐条各造一个**违例样本**
＋一个**合法样本**，并钉住消息以不变量编号开头（dsh 同措辞）。
"""
from __future__ import annotations

from src.storage import card_db, memory_domain as md


def _anchor(seq: int, sid: str = "s1") -> dict:
    return {"sessionId": sid, "seq": seq}


def _root(seq: int, covered: list[int] | None = None, **kw) -> dict:
    n = md.new_node("s1", seq, f"根{seq}", kind="decision")
    n["coveredSeqs"] = covered if covered is not None else [seq]
    n.update(kw)
    return n


def _child(seq: int, parent: int, covered: list[int] | None = None, **kw) -> dict:
    n = md.new_node("s1", seq, f"子{seq}", kind="fact")
    n["coveredSeqs"] = covered if covered is not None else [seq]
    n["level"] = 2
    n["upper"] = [_anchor(parent)]
    n.update(kw)
    return n


# ---------------------------------------------------------------- 合法样本

def test_clean_chain_has_no_violation():
    parent = _root(1, lower=[_anchor(2), _anchor(3)])
    c1 = _child(2, 1)
    c2 = _child(3, 1)
    assert md.check_invariants([parent, c1, c2]) == []
    assert md.is_valid([parent, c1, c2]) is True


def test_single_node_chain_is_valid():
    assert md.check_invariants([_root(0)]) == []


# ---------------------------------------------------------------- I1 互链

def test_i1_half_chain_detected():
    """父 lower 没反向持有子 ⇒ 半边链。"""
    parent = _root(1)                      # lower 空
    child = _child(2, 1)                   # upper 指父
    v = md.check_invariants([parent, child])
    assert any(x.startswith("I1:") and "半边链" in x for x in v), v


def test_i1_dangling_lower_detected():
    """父 lower 有元素，但没有任何条目 upper 回指 ⇒ 悬空。"""
    parent = _root(1, lower=[_anchor(9)])
    v = md.check_invariants([parent])
    assert any(x.startswith("I1:") and "悬空" in x for x in v), v


def test_i1_duplicate_gist_identity_detected():
    a = _root(1)
    b = _root(1)
    v = md.check_invariants([a, b])
    assert any(x.startswith("I1:") and "身份重复" in x for x in v), v


def test_i1_upper_points_to_missing_gist():
    orphan = _child(2, 99)
    v = md.check_invariants([orphan])
    assert any(x.startswith("I1:") and "不存在的 gist" in x for x in v), v


# ---------------------------------------------------------------- I2 层级差

def test_i2_level_gap_detected():
    parent = _root(1, lower=[_anchor(2)])
    child = _child(2, 1)
    child["level"] = 3                     # 应为 2
    v = md.check_invariants([parent, child])
    assert any(x.startswith("I2:") for x in v), v


# ---------------------------------------------------------------- I3 同层收敛

def test_i3_overlapping_covered_seqs_detected():
    parent = _root(1, lower=[_anchor(2), _anchor(3)])
    c1 = _child(2, 1, covered=[10, 11])
    c2 = _child(3, 1, covered=[11, 12])    # 与 c1 重叠 11
    v = md.check_invariants([parent, c1, c2])
    assert any(x.startswith("I3:") and "重叠" in x for x in v), v


# ---------------------------------------------------------------- I4 顶层

def test_i4_level_one_with_upper_detected():
    parent = _root(1, lower=[_anchor(2)])
    child = _child(2, 1)
    child["level"] = 1                     # 应为 2
    v = md.check_invariants([parent, child])
    assert any(x.startswith("I4:") and "level=1 但 upper 非空" in x for x in v), v


def test_i4_level_two_without_upper_detected():
    n = md.new_node("s1", 5, "孤儿", kind="fact")
    n["level"] = 2
    v = md.check_invariants([n])
    assert any(x.startswith("I4:") and "upper 为空" in x for x in v), v


# ---------------------------------------------------------------- I5 森林

def test_i5_two_parents_detected():
    p1 = _root(1, lower=[_anchor(3)])
    p2 = _root(2, lower=[_anchor(3)])
    child = _child(3, 1)
    child["upper"] = [_anchor(1), _anchor(2)]
    v = md.check_invariants([p1, p2, child])
    assert any(x.startswith("I5:") and "|upper|" in x for x in v), v


# ---------------------------------------------------------------- 追溯

def test_covered_seqs_of_chain():
    chain = [_root(1, covered=[1, 2]), _child(3, 1, covered=[3, 4, 5])]
    assert md.covered_seqs_of(chain) == [1, 2, 3, 4, 5]


def test_new_node_rejects_bad_enum():
    import pytest
    with pytest.raises(ValueError):
        md.new_node("s1", 0, "x", kind="unknown")
    with pytest.raises(ValueError):
        md.new_node("s1", 0, "x", status="paused")


# ---------------------------------------------------------------- 表

def test_memory_tables_in_schema():
    ddl = "\n".join(card_db._MEMORY_DDL)
    assert "CREATE TABLE IF NOT EXISTS memory" in ddl
    assert "memory_archive" in ddl
    assert "condense_mark" in ddl and "fold_mark" in ddl


# ---------------------------------------------------------------- 凝练产出 → 节点

def test_parse_node_json():
    """模型按新契约产出的节点 JSON → 条目（含 kind/coveredSeqs/start/result/process/lesson）。"""
    import json
    from src.storage import memory_store

    payload = {"nodes": [
        {"anchor": {"seq": 3}, "gist": "用户要求把原文入库，实施者落库并跑通追溯",
         "kind": "decision", "coveredSeqs": [3, 4, 5],
         "start": "用户发现追溯断了", "result": "库内可追溯",
         "process": "读 dsh origin 契约后双写", "lesson": "注释不是事实，要读实现"},
    ]}
    entries = memory_store._parse_condense_output(json.dumps(payload, ensure_ascii=False), 0)
    assert len(entries) == 1
    e = entries[0]
    assert e["seq"] == 3 and e["kind"] == "decision"
    assert e["coveredSeqs"] == [3, 4, 5]
    assert e["conclusion"].startswith("用户要求把原文入库")
    assert e["lesson"].startswith("注释不是事实")


def test_parse_legacy_marker_format_still_works():
    """旧【结论】/【依据】格式仍能解析（JSON 优先、标记回退）。"""
    from src.storage import memory_store

    text = "【结论】甲\n【依据】[块2] 原文\n【结论】乙\n【依据】[块7] 原文"
    entries = memory_store._parse_condense_output(text, 0)
    assert [e["seq"] for e in entries] == [2, 7]
    assert [e["conclusion"] for e in entries] == ["甲", "乙"]
    assert all("kind" not in e for e in entries), "旧格式不硬造新键"


def test_parse_json_wrapped_in_prose():
    """模型偶尔会带前后废话 ⇒ 宽松截取仍能解。"""
    from src.storage import memory_store

    text = '好的，结果如下：\n{"nodes":[{"anchor":{"seq":1},"gist":"甲","kind":"fact"}]}\n以上。'
    entries = memory_store._parse_condense_output(text, 0)
    assert len(entries) == 1 and entries[0]["seq"] == 1


def test_entries_to_chain_keeps_node_fields():
    """条目 → 链：kind/coveredSeqs/process/lesson 都跟着走，且链合法。"""
    entries = [{
        "seq": 3, "conclusion": "甲", "evidence": "依据", "kind": "todo",
        "coveredSeqs": [3, 4], "start": "起因", "result": "结果",
        "process": "过程", "lesson": "教训",
    }]
    chain = md.chain_from_entries(entries, "s1")
    n = chain[0]
    assert n["kind"] == "todo" and n["coveredSeqs"] == [3, 4]
    assert n["start"] == "起因" and n["result"] == "结果" and n["lesson"] == "教训"
    assert md.check_invariants(chain) == []


# ---------------------------------------------------------------- archive 物化（多层链）

def test_make_description_child_follows_dsh_rules():
    parent = md.new_node("s1", 5, "父结论", kind="decision")
    parent["level"] = 1
    parent["coveredSeqs"] = [5, 6]
    child = md.make_description_child(parent, "描述层正文")
    assert child["anchor"] == parent["anchor"], "anchor 与父同（ADJ-1=A）"
    assert child["role"] == "description"
    assert child["level"] == 2, "level = 父 + 1"
    assert child["upper"] == [parent["anchor"]]
    assert child["coveredSeqs"] == [5, 6], "coveredSeqs 拷贝父的"
    assert not child.get("degraded"), "文本不同 ⇒ 不降级"
    same = md.make_description_child(parent, "父结论")
    assert same.get("degraded") is True, "文本 == 父 gist ⇒ degraded"


def test_migrate_archive_creates_description_children():
    parent = md.new_node("s1", 5, "父结论", kind="decision")
    parent["level"] = 1
    chain, rep = md.migrate_archive([parent], [{"seq": 5, "process": "描述正文"}])
    assert rep["created"] == 1 and rep["skippedOrphans"] == 0
    assert rep["violations"] == [], rep["violations"]
    desc = [n for n in chain if n.get("role") == "description"]
    assert len(desc) == 1
    assert desc[0]["gist"] == "描述正文"
    # 父的 lower 必须回填（I1 互链的另一半）
    parent_after = next(n for n in chain if n.get("role") != "description")
    assert parent_after["lower"] == [desc[0]["anchor"]], "父的 lower 要指回子"
    assert md.check_invariants(chain) == []


def test_migrate_archive_skips_orphans():
    chain, rep = md.migrate_archive([], [{"seq": 99, "process": "孤儿描述"}])
    assert rep["created"] == 0 and rep["skippedOrphans"] == 1
    assert chain == []


def test_migrate_archive_is_idempotent():
    parent = md.new_node("s1", 5, "父结论", kind="decision")
    parent["level"] = 1
    once, rep1 = md.migrate_archive([parent], [{"seq": 5, "process": "描述正文"}])
    twice, rep2 = md.migrate_archive(once, [{"seq": 5, "process": "描述正文"}])
    assert rep1["created"] == 1 and rep2["created"] == 0, "幂等：不重复造"
    assert len(twice) == len(once)
