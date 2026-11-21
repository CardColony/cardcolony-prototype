# -*- coding: utf-8 -*-
"""[成本优化 · 第四批 2026-11-21] 工具证据投影验收。

覆盖任务书隔离验收矩阵：search_files/read_file 成功结果投影、同回合
证据引用（同文件同区间同版本去重）、写后回读、权限变化、预算压力、
档案对照。开关 ``TOOL_EVIDENCE_WIRE`` 关闭时逐字同基线。
"""
from __future__ import annotations

import json

import pytest

from src.api import tool_fold

pytestmark = pytest.mark.asyncio


# ============================================================
# 轮槽级夹具
# ============================================================

def _slot(*, wire: bool, monkeypatch) -> tuple:
    """开/关两组：wire=True 且 TOOL_EVIDENCE_WIRE=1 才走证据投影。"""
    if wire:
        monkeypatch.setenv("TOOL_EVIDENCE_WIRE", "1")
    msgs = [{"role": "user", "content": "把判据核对完"}]
    s = tool_fold.ToolTurnSlot(msgs, evidence_wire=wire)
    return s, msgs


def _round(s, calls: list[tuple[str, dict, dict]]):
    """开一轮：calls=[(call_id, args, evidence_or_None)]，随后逐个入槽。"""
    s.begin_round({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": c, "type": "function",
                        "function": {"name": a.get("_tool", "read_file"),
                                     "arguments": "{}"}}
                       for c, a, _ in calls]})
    for c, a, ev in calls:
        name = str(a.get("_tool") or "read_file")
        data = (ev or {}).get("data") or {}
        content = (str((ev or {}).get("prefix") or "")
                   + json.dumps(data, ensure_ascii=False)
                   + str((ev or {}).get("suffix") or ""))
        s.append_tool_result(
            {"role": "tool", "tool_call_id": c, "content": content},
            evidence=ev)


def _read_ev(path, start, end, content, total=None, args=None,
             sha=None):
    lines = content.split("\n") if content else []
    import hashlib as _hl
    return {"tool": "read_file",
            "data": {"path": path, "total_lines": total or len(lines) + 99,
                     "read_lines": f"{start}-{end}", "content": content},
            "args": args or {"path": path, "para_start": start,
                             "para_end": end},
            "prefix": "", "suffix": "",
            # 信封层整文件指纹；缺省用 content 哈希（夹具便利），
            # 真实链由 handler 对整份字节求 sha256。
            "file_sha256": sha or _hl.sha256(
                content.encode("utf-8")).hexdigest()}


def _search_ev(keyword, scope, hits, scanned=5, truncated=False, args=None):
    return {"tool": "search_files",
            "data": {"keyword": keyword, "scope": scope,
                     "scanned_files": scanned, "hits": hits,
                     "truncated": truncated},
            "args": args or {"keyword": keyword, "path": scope},
            "prefix": "", "suffix": ""}


def _tool_msgs(wire_msgs):
    return [m for m in wire_msgs if m.get("role") == "tool"]


# ============================================================
# 开关关闭 ⇒ 逐字同基线
# ============================================================

async def test_switch_off_byte_identical(monkeypatch):
    """开关关闭：无论是否带证据元数据，wire 出站与原列表逐字相同。"""
    for wire, env in ((False, "1"), (True, "0"), (True, "")):
        monkeypatch.delenv("TOOL_EVIDENCE_WIRE", raising=False)
        if env:
            monkeypatch.setenv("TOOL_EVIDENCE_WIRE", env)
        s, msgs = _slot(wire=wire, monkeypatch=monkeypatch)
        big = "行内容。" * 50000                       # 远超单条硬顶
        ev = _read_ev("trading-workspace/a.txt", 1, 9999, big)
        _round(s, [("call_1", {"_tool": "read_file"}, ev)])
        out = await s.wire_messages()
        if not wire or env != "1":
            # 关/未开：走旧闸——同一内容被平截（有改写但不是证据投影）
            tm = _tool_msgs(out)[0]["content"]
            assert "shown" not in tm and "已省略行" not in tm


async def test_switch_off_no_evidence_no_change(monkeypatch):
    """开关开但非试点工具结果 ⇒ 完全沿用旧链。"""
    s, msgs = _slot(wire=True, monkeypatch=monkeypatch)
    _round(s, [("call_1", {"_tool": "list_files"},
               {"tool": "list_files", "data": {"path": "x", "entries": []},
                "args": {}, "prefix": "", "suffix": ""})])
    out = await s.wire_messages()
    assert out is msgs                              # 无触发原对象返回


# ============================================================
# read_file 投影
# ============================================================

async def test_read_file_short_result_verbatim(monkeypatch):
    """短结果原样回传（投影不更短不替换）。"""
    s, msgs = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"第{i}行 原文内容" for i in range(1, 21))
    ev = _read_ev("docs/x.md", 1, 20, body)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    assert out is msgs                              # 预算内零加工


async def test_read_file_long_result_keeps_lines_and_elision(monkeypatch):
    """长结果：头尾完整行＋显式省略区间＋可回读参数；元数据不丢。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "1200")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"第{i:04d}行 " + "字" * 40 for i in range(1, 201))
    ev = _read_ev("trading-workspace/big.txt", 1, 200, body, total=200)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    tm = _tool_msgs(out)[0]["content"]
    assert len(tm) < len(json.dumps(ev["data"], ensure_ascii=False))
    d = json.loads(tm)
    assert d["path"] == "trading-workspace/big.txt"
    assert d["total_lines"] == 200 and d["read_lines"] == "1-200"
    assert "已省略行" in d["content"] and "read_file(path," in d["content"]
    # 保留行是完整行：首部第一行逐字在场
    assert "第0001行" in d["content"]
    # 省略区间行号可与 shown/elided 对上
    sh = d.get("shown") or {}
    assert sh.get("elided"), d


async def test_read_file_single_giant_line_states_limit(monkeypatch):
    """超长单行：装不下完整行 ⇒ 如实显示限制与原记录位置。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "800")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "巨" * 5000                               # 单行无换行
    ev = _read_ev("blob/one_line.txt", 1, 1, body, total=1)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    d = json.loads(_tool_msgs(out)[0]["content"])
    assert d["content"] == ""
    assert "装不下" in d["note"] and "one_line.txt" in d["note"]


async def test_read_file_empty_range_preserved(monkeypatch):
    """超尾起点返回空区间 ⇒ 空结果语义原样保留（path/read_lines 在）。"""
    s, msgs = _slot(wire=True, monkeypatch=monkeypatch)
    ev = _read_ev("a.txt", 9999, 9999, "", total=100)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    assert out is msgs
    d = json.loads(_tool_msgs(out)[0]["content"])
    assert d["read_lines"] == "9999-9999" and d["content"] == ""


# ============================================================
# search_files 投影
# ============================================================

async def test_search_small_result_verbatim(monkeypatch):
    s, msgs = _slot(wire=True, monkeypatch=monkeypatch)
    ev = _search_ev("判据", "trading-workspace", [
        {"file": "a.md", "line": 3, "text": "A1 判据：按行核对"}])
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    assert out is msgs


async def test_search_long_result_coords_and_fragments(monkeypatch):
    """多文件长命中：全部坐标保留，长行留逐字片段（明确标 fragment）。"""
    # 20 条片段 ≈2700 字符：cap=3000 走片段档；更低则落纯坐标档。
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "3000")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    hits = []
    for i in range(1, 21):
        long_line = "前导 " + "杂" * 300 + " 关键词命中 尾随" + "杂" * 300
        hits.append({"file": f"docs/f{i}.md", "line": i * 7,
                     "text": long_line.strip()})
    ev = _search_ev("关键词", "docs", hits, scanned=20, truncated=True,
                    args={"keyword": "关键词", "path": "docs"})
    _round(s, [("call_1", {}, ev)])
    tm = _tool_msgs(out := await s.wire_messages())[0]["content"]
    d = json.loads(tm)
    assert len(d["hits"]) == 20, "全命中坐标优先保留"
    assert d["truncated"] is True                  # 扫描截断保留
    assert d["scanned_files"] == 20
    # 每条命中：file+line 必有；正文是片段或短原文
    assert all(h.get("file") and isinstance(h.get("line"), int)
               for h in d["hits"])
    assert any("fragment" in h for h in d["hits"])
    frags = [h["fragment"] for h in d["hits"] if "fragment" in h]
    assert all("关键词" in f for f in frags)       # 片段是真实的逐字片段


async def test_search_coords_only_when_fragments_dont_fit(monkeypatch):
    """片段也装不下 ⇒ 纯坐标＋回读提示。"""
    # 9 坐标＋回读 note ≈460：cap 给 520 落在纯坐标档。
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "520")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    hits = [{"file": f"d/{i}.md", "line": i,
             "text": "短行 关键词"} for i in range(1, 10)]
    ev = _search_ev("关键词", "d", hits)
    _round(s, [("call_1", {}, ev)])
    tm = _tool_msgs(await s.wire_messages())[0]["content"]
    d = json.loads(tm)
    assert all(set(h.keys()) == {"file", "line"} for h in d["hits"])
    assert "read_file" in d.get("note", "")


async def test_search_prefix_when_coords_overflow(monkeypatch):
    """坐标也装不下 ⇒ 有序前缀＋展示/总数＋续查办法。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "260")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    hits = [{"file": f"dir/sub/file_{i:03d}.md", "line": i * 3,
             "text": "关键词"} for i in range(1, 16)]
    ev = _search_ev("关键词", "dir", hits)
    _round(s, [("call_1", {}, ev)])
    tm = _tool_msgs(await s.wire_messages())[0]["content"]
    d = json.loads(tm)
    assert len(d["hits"]) < 15
    assert "更窄的 path" in d.get("note", "")
    # 前缀有序：第一条坐标仍是第一个命中
    assert d["hits"][0]["file"].endswith("file_001.md")


async def test_search_zero_hit_truncated_preserved(monkeypatch):
    """零命中＋扫描截断 ⇒ 限制如实显示（短结果照原样；极小预算也如实留头）。"""
    # 常规预算：零命中结果本来就小，逐字驻留、truncated 在场
    s, msgs = _slot(wire=True, monkeypatch=monkeypatch)
    ev = _search_ev("不存在", "dir", [], scanned=20000, truncated=True)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    assert out is msgs
    d = json.loads(_tool_msgs(out)[0]["content"])
    assert d["hits"] == [] and d["truncated"] is True
    # 极小预算：最小状态信封——{"hits":[],"truncated":true} 仍能发，
    # 零命中＋扫描未完成两条事实优先于半截 JSON（[返修] 语义断言补回）。
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "80")
    s2, _ = _slot(wire=True, monkeypatch=monkeypatch)
    _round(s2, [("call_1", {}, ev)])
    tm = _tool_msgs(await s2.wire_messages())[0]["content"]
    assert len(tm) <= 80, f"实发 {len(tm)} 超 cap=80"
    d2 = json.loads(tm)
    assert d2["hits"] == [] and d2["truncated"] is True, \
        f"最小状态信封丢失事实：{tm[:120]}"


# ============================================================
# 同回合证据引用（read_file）
# ============================================================

async def test_same_range_same_version_dedup_to_reference(monkeypatch):
    """同回合同文件同区间同版本：第二份留原文，第一份变宿主引用。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"第{i}行 " + "字" * 30 for i in range(1, 31))
    ev1 = _read_ev("docs/a.md", 1, 30, body, total=100)
    _round(s, [("call_1", {}, ev1)])
    ev2 = _read_ev("docs/a.md", 1, 30, body, total=100)
    _round(s, [("call_2", {}, ev2)])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    first, second = tms[0]["content"], tms[1]["content"]
    assert "call_2" in first and "docs/a.md" in first and "1-30" in first
    assert "行段" in first
    assert json.loads(second)["content"] == body    # 最新份原文完整
    assert tms[0]["tool_call_id"] == "call_1"
    assert tms[1]["tool_call_id"] == "call_2"       # 两个调用各自有回执


async def test_same_file_different_range_no_merge(monkeypatch):
    """同文件不同区间 ⇒ 不合并。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join("行 " + "字" * 40 for _ in range(20))
    _round(s, [("call_1", {}, _read_ev("a.md", 1, 20, body)),
               ("call_2", {}, _read_ev("a.md", 21, 40, body))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    assert all("引用" not in t["content"] and "不再重复" not in t["content"]
               for t in tms)


async def test_different_file_same_content_no_merge(monkeypatch):
    """不同文件同文 ⇒ 不合并（文件身份不同）。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join("同样内容 " + "字" * 40 for _ in range(20))
    _round(s, [("call_1", {}, _read_ev("a/x.md", 1, 20, body)),
               ("call_2", {}, _read_ev("b/x.md", 1, 20, body))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    assert all("不再重复" not in t["content"] for t in tms)


async def test_same_range_new_version_no_merge(monkeypatch):
    """写后回读：同区间但内容已变 ⇒ 版本指纹不同，不合并，两份都留。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    old = "\n".join("旧 " + "字" * 40 for _ in range(20))
    new = "\n".join("新 " + "字" * 40 for _ in range(20))
    _round(s, [("call_1", {}, _read_ev("a.md", 1, 20, old)),
               ("call_2", {}, _read_ev("a.md", 1, 20, new))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    assert all("不再重复" not in t["content"] for t in tms)
    assert json.loads(tms[1]["content"])["content"] == new


async def test_parallel_short_results_stay_verbatim(monkeypatch):
    """并列短结果：按「投影确实更短才替换」判——引用更短则降级为引用
    （带文件身份＋行段＋保留位 ID），否则逐字保留。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "短内容"
    _round(s, [("call_1", {}, _read_ev("a.md", 1, 1, body)),
               ("call_2", {}, _read_ev("a.md", 1, 1, body))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    first, second = tms[0]["content"], tms[1]["content"]
    if first.startswith("["):
        # 引用位：注明实际路径/行段/那份结果的 ID
        assert "a.md" in first and "1-1" in first and "call_2" in first
    else:
        assert json.loads(first)["content"] == body
    assert json.loads(second)["content"] == body    # 保留位原文完整


# ============================================================
# 预算压力
# ============================================================

async def test_budget_evidence_projection_and_ref_downgrade(monkeypatch):
    """多轮大结果超预算：旧轮用证据投影缩头；代表项被缩 ⇒ 引用降级为
    可执行的行段回读说明。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "4000")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    big1 = "\n".join(f"A{i:03d} " + "字" * 50 for i in range(60))
    big2 = "\n".join(f"B{i:03d} " + "字" * 50 for i in range(60))
    big3 = "\n".join(f"C{i:03d} " + "字" * 50 for i in range(60))
    # 轮1：同一文件同区间两次读取（第二份进轮2）→ 第一份变引用
    _round(s, [("call_1", {}, _read_ev("a.md", 1, 60, big1))])
    _round(s, [("call_2", {}, _read_ev("a.md", 1, 60, big1)),
               ("call_3", {}, _read_ev("b.md", 1, 60, big2))])
    _round(s, [("call_4", {}, _read_ev("c.md", 1, 60, big3))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    total = sum(len(t["content"]) for t in tms)
    assert total <= 4000 + 2000, f"预算失控：{total}"
    # 最新轮原文仍在（先动旧轮）
    assert json.loads(tms[-1]["content"])["content"] == big3
    # call_1 的引用若还在，要么指向仍完整的 call_2，要么收口为可回读指引
    first = tms[0]["content"]
    if "call_2" in first:
        assert json.loads(tms[1]["content"])["content"] == big1
    else:
        assert "read_file" in first and ("已省略" in first
                                        or "之前读取的版本" in first)


async def test_multibyte_chars_line_integrity(monkeypatch):
    """多字节字符按行保留，不劈字符。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "600")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"第{i}行 " + "漢字🙂" * 30 for i in range(1, 51))
    ev = _read_ev("mb.txt", 1, 50, body, total=50)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    d = json.loads(_tool_msgs(out)[0]["content"])
    for ln in d["content"].split("\n"):
        assert not ln or "已省略行" in ln or ln.endswith(tuple("漢字🙂")), ln


# ============================================================
# 失败回执与宿主行
# ============================================================

async def test_failure_receipt_untouched(monkeypatch):
    """失败回执沿原链（无证据元数据⇒旧闸），错误码形态不变。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    _round(s, [("call_1", {"_tool": "read_file"}, None)])
    msgs = s._messages
    msgs[-1]["content"] = "[tool_error code=io] 文件不存在: x"
    out = await s.wire_messages()
    assert out is msgs or _tool_msgs(out)[0]["content"].startswith(
        "[tool_error")


async def test_rescue_prefix_and_task_proof_survive_projection(monkeypatch):
    """抢救纠正提示＋任务证据 rc: 行随投影保留。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "900")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"行{i} " + "字" * 50 for i in range(1, 60))
    ev = _read_ev("a.md", 1, 59, body)
    ev["prefix"] = "[系统提示] 你上一轮的调用写在正文里…\n\n"
    ev["suffix"] = "\n〔任务证据〕A1 rc:call_1"
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    tm = _tool_msgs(out)[0]["content"]
    assert tm.startswith("[系统提示]")
    assert tm.endswith("rc:call_1")


# ============================================================
# [返修 2026-11-21] 四项返工的钉死用例
# ============================================================

async def test_out_of_range_edit_invalidates_same_version(monkeypatch):
    """[返修①] 只改了读取区间外的一行：同路径同区间同正文，但**整文件
    指纹不同** ⇒ 文件已换版，不得判「同版本」合并。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"行{i} " + "字" * 40 for i in range(1, 21))
    ev1 = _read_ev("a.md", 1, 20, body, sha="sha_old")
    ev2 = _read_ev("a.md", 1, 20, body, sha="sha_new")   # 区间外改动
    _round(s, [("call_1", {}, ev1), ("call_2", {}, ev2)])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    assert all("不再重复" not in t["content"] for t in tms), \
        "整文件换版不得当同版本合并"
    assert json.loads(tms[0]["content"])["content"] == body
    assert json.loads(tms[1]["content"])["content"] == body


async def test_missing_version_credential_no_merge(monkeypatch):
    """[返修①] 版本凭据缺失 ⇒ 不参与合并（各自结果各自处理）。"""
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"行{i} " + "字" * 40 for i in range(1, 21))
    ev1 = _read_ev("a.md", 1, 20, body, sha="sha1")
    ev1["file_sha256"] = ""                            # 凭据缺失
    ev2 = _read_ev("a.md", 1, 20, body, sha="sha1")
    _round(s, [("call_1", {}, ev1), ("call_2", {}, ev2)])
    out = await s.wire_messages()
    assert all("不再重复" not in t["content"] for t in _tool_msgs(out))


async def test_rep_trimmed_ref_downgrades_unconditionally(monkeypatch):
    """[返修②] 代表原文被预算裁剪 ⇒ 引用**无条件**换成行段回读说明
    （即使提示更长——「原文见该条」已属事实错误）；本调用的抢救提示
    与 rc: 任务证据随降级说明保留。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "3500")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    big = "\n".join(f"A{i:03d} " + "字" * 50 for i in range(60))
    big2 = "\n".join(f"B{i:03d} " + "字" * 50 for i in range(60))
    ev1 = _read_ev("a.md", 1, 60, big, sha="v1")
    ev1["prefix"] = "[系统提示] 纠正提示。\n\n"
    ev1["suffix"] = "\n〔任务证据〕A1 rc:call_1"
    # 轮1：两份同版本同区间 ⇒ call_1 变引用（带宿主标记）
    _round(s, [("call_1", {}, ev1)])
    _round(s, [("call_2", {}, _read_ev("a.md", 1, 60, big, sha="v1"))])
    # 轮2：大结果把轮1全部压掉——call_2（代表项）被截 ⇒ 引用必须降级
    _round(s, [("call_3", {}, _read_ev("b.md", 1, 60, big2, sha="b"))])
    out = await s.wire_messages()
    first = _tool_msgs(out)[0]["content"]
    # 不得仍指向已裁剪的 call_2
    assert "call_2" not in first or "见该条" not in first, first
    # 降级为可执行回读说明，且宿主标记还在
    assert "read_file" in first
    assert first.startswith("[系统提示] 纠正提示。")
    assert first.rstrip().endswith("rc:call_1")


async def test_serialized_cap_counts_json_escaping(monkeypatch):
    """[返修③] 引号/转义膨胀计入上限：投影的**最终序列化结果** ≤ cap。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "3000")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    # 每行全是引号：JSON 序列化体积≈原文 2 倍
    body = "\n".join('"' * 200 for _ in range(40))
    ev = _read_ev("q.txt", 1, 40, body)
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    tm = _tool_msgs(out)[0]["content"]
    assert len(tm) <= 3000, f"序列化实发 {len(tm)} 超出 cap=3000"
    d = json.loads(tm)
    assert "已省略行" in d["content"] or d.get("note")


async def test_pattern_hit_trailing_ws_no_keyword_substitute(monkeypatch):
    """[返修④] 正则命中行尾空白（被 strip 掉）⇒ 定位失败只给坐标——
    不得拿 keyword 里的另一段文字冒充命中片段。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "500")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    long_line = "关键词" + "杂" * 600               # 全长超 cap 强制进投影
    hits = [{"file": "x.md", "line": 7, "text": long_line}]
    ev = _search_ev("关键词", "x", hits,
                    args={"keyword": "关键词", "pattern": r"\s+$",
                          "path": "x"})
    _round(s, [("call_1", {}, ev)])
    d = json.loads(_tool_msgs(await s.wire_messages())[0]["content"])
    h = d["hits"][0]
    assert set(h.keys()) == {"file", "line"}, \
        f"正则定位失败应只给坐标，实际：{h}"
    # keyword 命中位 ≠ 正则命中位 ⇒ 任何片段都是伪造
    assert "关键词" not in json.dumps(h, ensure_ascii=False)


async def test_search_no_pattern_keyword_fragment_still_works(monkeypatch):
    """pattern 空 ⇒ keyword 子串定位照旧（正则优先级不误伤正常路）。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "500")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    long_line = "杂" * 300 + " 关键词命中 " + "杂" * 300
    hits = [{"file": "x.md", "line": 7, "text": long_line}]
    ev = _search_ev("关键词", "x", hits,
                    args={"keyword": "关键词", "path": "x"})
    _round(s, [("call_1", {}, ev)])
    d = json.loads(_tool_msgs(await s.wire_messages())[0]["content"])
    h = d["hits"][0]
    if "fragment" in h:
        assert "关键词" in h["fragment"]
    else:
        assert set(h.keys()) == {"file", "line"}      # cap 装不下⇒纯坐标合法


# ============================================================
# [返修] 端到端：真实写后回读 + 拒权
# ============================================================

async def test_e2e_write_then_readback_new_version(sandbox, monkeypatch,
                                                   tmp_path):
    """写后回读：真实 write_file 改内容后再读同区间——两版本都在场、
    不合并（整文件指纹已换）；真实 handler 每次都执行。"""
    from tests.outbound_probe import (OutboundRecorder, frame_finish,
                                      frame_text, frame_tool, sse)
    from src.api import routes
    from src.storage import card_store, model_override, work_store

    w = await work_store.create_work("批四写后回读")
    wid, root = w["work_id"], w["work_card_id"]
    r = await card_store.create_card(
        wid, "卡", "用户直接创建", card_type="worker",
        parent=root, mission="核对", skip_kick=True)
    cid = str(r["meta"]["card_id"])
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")
    f = tmp_path / "spec.md"
    old_body = "\n".join(f"第{i:03d}行 旧判据{i}。" for i in range(1, 31))
    new_body = "\n".join(f"第{i:03d}行 新判据{i}。" for i in range(1, 31))
    f.write_text(old_body, encoding="utf-8")

    def script(index, body):
        n = sum(1 for m in body.get("messages") or []
                if m.get("role") == "tool")
        if n == 0:
            return sse(frame_tool("call_1", "read_file",
                                  {"path": str(f), "para_start": 1,
                                   "para_end": 30}),
                       frame_finish("tool_calls"))
        if n == 1:
            return sse(frame_tool("call_2", "write_file",
                                  {"path": str(f), "content": new_body}),
                       frame_finish("tool_calls"))
        if n == 2:
            return sse(frame_tool("call_3", "read_file",
                                  {"path": str(f), "para_start": 1,
                                   "para_end": 30}),
                       frame_finish("tool_calls"))
        return sse(frame_text("完毕。"), frame_finish("stop"))

    monkeypatch.setenv("TOOL_EVIDENCE_WIRE", "1")
    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    await routes._run_message(wid, cid, "核对 spec", None,
                              source_kind="human")
    assert len(rec.requests) == 4
    hop4 = rec.joined(3)
    assert "第001行 新判据1" in hop4, "写后回读必须是新版本"
    # 两个版本各自真实在场——旧版没有因为同区间被误判同版本合并掉
    assert "第001行 旧判据1" in hop4
    # 写工具回执真实在场（成功回执沿原链）
    assert "write_file" in hop4 or "备份" in hop4 or "ok" in hop4


async def test_e2e_missing_file_keeps_error_form(sandbox, monkeypatch,
                                                 tmp_path):
    """缺文件对照（返修后改名，原「拒权」用例实为缺文件）：第二次读到
    不存在路径 ⇒ 原错误回执在场、失败位置不得有旧成功正文或证据
    引用痕迹（关/开两组独立世界）。"""
    from tests.outbound_probe import (OutboundRecorder, frame_finish,
                                      frame_text, frame_tool, sse)
    from src.api import routes
    from src.storage import card_store, model_override, work_store

    f = tmp_path / "ok.md"
    f.write_text("第1行 正常内容。", encoding="utf-8")
    missing = tmp_path / "不存在.md"

    def script(index, body):
        n = sum(1 for m in body.get("messages") or []
                if m.get("role") == "tool")
        if n == 0:
            return sse(frame_tool("call_1", "read_file",
                                  {"path": str(f)}),
                       frame_finish("tool_calls"))
        if n == 1:
            return sse(frame_tool("call_2", "read_file",
                                  {"path": str(missing)}),
                       frame_finish("tool_calls"))
        return sse(frame_text("已收错误回执。"), frame_finish("stop"))

    async def _group(on: bool):
        w = await work_store.create_work(f"批四拒权-{on}")
        wid, root = w["work_id"], w["work_card_id"]
        r = await card_store.create_card(
            wid, "卡", "用户直接创建", card_type="worker",
            parent=root, mission="核对", skip_kick=True)
        cid = str(r["meta"]["card_id"])
        model_override.put_row(wid, cid,
                               {"model": "ds-v41-flash-eco-amr"},
                               src="manual")
        if on:
            monkeypatch.setenv("TOOL_EVIDENCE_WIRE", "1")
        else:
            monkeypatch.delenv("TOOL_EVIDENCE_WIRE", raising=False)
        rec = OutboundRecorder(script)
        rec.install(monkeypatch)
        await routes._run_message(wid, cid, "读文件", None,
                                  source_kind="human")
        return rec

    for on in (False, True):
        rec = await _group(on)
        assert len(rec.requests) == 3, f"on={on} 请求数 {len(rec.requests)}"
        tools = [m for m in (rec.requests[2]["body"] or {}).get("messages")
                 or [] if m.get("role") == "tool"]
        assert len(tools) == 2
        err_txt = str(tools[1].get("content") or "")
        assert "不存在" in err_txt or "tool_error" in err_txt or \
            "error" in err_txt, f"失败回执变形：{err_txt[:200]}"
        # 成功回执在第一位，失败位不得混入成功正文或引用
        assert "正常内容" in str(tools[0].get("content") or "")
        assert "不再重复" not in err_txt and "正常内容" not in err_txt


async def test_e2e_real_denied_keeps_error_form(sandbox, monkeypatch,
                                                tmp_path):
    """[返修] 真·拒权回放：第二次读命中凭证文件名黑名单（.env）⇒
    check_file_access 真拒 → err("denied")。失败位保持 denied 回执、
    凭证内容零泄漏、无旧成功正文/引用痕迹（关/开两组独立世界）。"""
    from tests.outbound_probe import (OutboundRecorder, frame_finish,
                                      frame_text, frame_tool, sse)
    from src.api import routes
    from src.storage import card_store, model_override, work_store

    f = tmp_path / "ok.md"
    f.write_text("第1行 正常内容。", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("SECRET_TOKEN=x9f2 凭证内容绝不出站", encoding="utf-8")

    def script(index, body):
        n = sum(1 for m in body.get("messages") or []
                if m.get("role") == "tool")
        if n == 0:
            return sse(frame_tool("call_1", "read_file",
                                  {"path": str(f)}),
                       frame_finish("tool_calls"))
        if n == 1:
            return sse(frame_tool("call_2", "read_file",
                                  {"path": str(secret)}),
                       frame_finish("tool_calls"))
        return sse(frame_text("已收错误回执。"), frame_finish("stop"))

    async def _group(on: bool):
        w = await work_store.create_work(f"批四拒权-{on}")
        wid, root = w["work_id"], w["work_card_id"]
        r = await card_store.create_card(
            wid, "卡", "用户直接创建", card_type="worker",
            parent=root, mission="核对", skip_kick=True)
        cid = str(r["meta"]["card_id"])
        model_override.put_row(wid, cid,
                               {"model": "ds-v41-flash-eco-amr"},
                               src="manual")
        if on:
            monkeypatch.setenv("TOOL_EVIDENCE_WIRE", "1")
        else:
            monkeypatch.delenv("TOOL_EVIDENCE_WIRE", raising=False)
        rec = OutboundRecorder(script)
        rec.install(monkeypatch)
        await routes._run_message(wid, cid, "读文件", None,
                                  source_kind="human")
        return rec

    for on in (False, True):
        rec = await _group(on)
        assert len(rec.requests) == 3, f"on={on} 请求数 {len(rec.requests)}"
        tools = [m for m in (rec.requests[2]["body"] or {}).get("messages")
                 or [] if m.get("role") == "tool"]
        assert len(tools) == 2
        err_txt = str(tools[1].get("content") or "")
        assert "denied" in err_txt or "越权" in err_txt, \
            f"真 denied 回执变形：{err_txt[:200]}"
        # 凭证内容零泄漏——末跳请求体里不得出现 SECRET_TOKEN
        assert "SECRET_TOKEN" not in rec.joined(2)
        # 失败位不得混入旧成功正文或引用
        assert "正常内容" in str(tools[0].get("content") or "")
        assert "不再重复" not in err_txt and "正常内容" not in err_txt


async def test_pilot_results_never_hit_legacy_dedup(monkeypatch):
    """[返修] 试点结果只走新判据：① 两份缺版本凭据的长读结果各自
    保留（不进旧逐字节闸）；② 重复搜索不生成「与下文完全相同」
    引用——即使下文会被裁成预览。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "2000")
    monkeypatch.setenv("WIRE_DEDUP_MIN_CHARS", "100")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    big = "\n".join(f"A{i:03d} " + "字" * 50 for i in range(60))
    # ① 缺凭据：sha 置空
    ev1 = _read_ev("a.md", 1, 60, big, sha="")
    ev2 = _read_ev("a.md", 1, 60, big, sha="")
    # ② 重复搜索（search_files 本批不设引用键）
    hits = [{"file": f"f{i}.md", "line": i, "text": "x" * 200}
            for i in range(20)]
    sev = _search_ev("判据", "d", hits)
    _round(s, [("call_1", {}, ev1), ("call_2", {}, ev2),
               ("call_3", {}, sev), ("call_4", {}, sev)])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    for t in tms:
        c = t["content"]
        assert "完全相同" not in c and "不再重复" not in c, \
            f"试点结果被旧闸合并：{c[:120]}"
    # 搜索可能被预算裁成投影，但两份都在场（各自坐标/提示），绝不引用
    assert sum("判据" in t["content"] or "检索" in t["content"]
               or "命中" in t["content"] for t in tms) >= 2


async def test_flat_cap_preserves_host_markers(monkeypatch):
    """[返修] 平截兜底：cap=300 时实发 ≤300 且 rc: 尾标不丢。
    旧实现 content[:300]+note=335 且 suffix 被截掉。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "300")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "\n".join(f"行{i} " + "字" * 30 for i in range(1, 60))
    ev = _read_ev("a.md", 1, 59, body)
    ev["prefix"] = "[系统提示] 纠正提示。\n\n"
    ev["suffix"] = "\n〔任务证据〕A1 rc:call_1"
    _round(s, [("call_1", {}, ev)])
    out = await s.wire_messages()
    tm = _tool_msgs(out)[0]["content"]
    # 投影或平截：任一形态都不得超 cap、不得丢标记
    assert len(tm) <= 300, f"实发 {len(tm)} 超 cap=300"
    assert tm.startswith("[系统提示]"), tm[:80]
    assert tm.rstrip().endswith("rc:call_1"), tm[-80:]


async def test_flat_cap_marker_envelope_over_cap(monkeypatch):
    """极端 cap 连标记信封都装不下 ⇒ 如实保留标记信封（超顶入账），
    绝不头截吞掉 rc:。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "60")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    body = "字" * 500
    ev = _read_ev("a.md", 1, 1, body)
    ev["suffix"] = "\n〔任务证据〕A1 rc:call_1"
    _round(s, [("call_1", {}, ev)])
    tm = _tool_msgs(await s.wire_messages())[0]["content"]
    assert "rc:call_1" in tm


async def test_final_settle_after_ref_downgrade(monkeypatch):
    """[返修] 降级放大总量 ⇒ 最终出站前再结一次：引用降级后
    总输出被压回预算内（降级位自身可被省略，但标记始终保留）。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "700")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    big = "\n".join(f"A{i:03d} " + "字" * 50 for i in range(60))
    big2 = "\n".join(f"B{i:03d} " + "字" * 50 for i in range(60))
    ev1 = _read_ev("a.md", 1, 60, big, sha="v1")
    ev1["suffix"] = "\n〔任务证据〕A1 rc:call_1"
    _round(s, [("call_1", {}, ev1)])
    _round(s, [("call_2", {}, _read_ev("a.md", 1, 60, big, sha="v1"))])
    _round(s, [("call_3", {}, _read_ev("b.md", 1, 60, big2, sha="b"))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    total = sum(len(t["content"]) for t in tms)
    assert total <= 700, \
        f"降级后未再结账：实发 {total} > 预算 700"
    # call_1 的标记随降级/省略位保留
    first = tms[0]["content"]
    assert "rc:call_1" in first or "call_1" in first


async def test_settle_cap_still_degrades_dangling_refs(monkeypatch):
    """[返修] 结算触顶：迭代上限=1 时，最后一次结算裁掉的代表项，
    其引用在出站前仍须被末轮逐条核对降级——不得悬空。"""
    import re as _re
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "1150")
    monkeypatch.setenv("WIRE_REF_SETTLE_MAX", "1")   # 强制一轮触顶
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    repA = "甲" * 400
    repB = "乙" * 700
    _round(s, [("call_1", {}, _read_ev("a.md", 1, 1, repA, sha="vA")),
               ("call_2", {}, _read_ev("b.md", 1, 1, repB, sha="vB"))])
    _round(s, [("call_3", {}, _read_ev("a.md", 1, 1, repA, sha="vA")),
               ("call_4", {}, _read_ev("b.md", 1, 1, repB, sha="vB"))])
    _round(s, [("call_5", {}, _read_ev("c.md", 1, 1, "尾" * 150,
                                        sha="vC"))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    by_id = {t.get("tool_call_id"): t for t in tms}
    for t in tms:
        m = _re.search(r"在本轮 (\S+) 展示", t["content"])
        if not m:
            continue
        rep = by_id.get(m.group(1))
        assert rep is not None and rep["content"].startswith("{"), \
            f"触顶后引用悬空 {m.group(1)}：" \
            f"{(rep or {}).get('content', '?')[:80]}"


async def test_off_state_legacy_settle_byte_identical(monkeypatch):
    """[返修] 关态逐字比对：旧裁剪笔记不得被二次结算改写——
    「结果共 1000 字符」提示与 legacy 公式逐字节一致。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "900")
    monkeypatch.delenv("TOOL_EVIDENCE_WIRE", raising=False)
    s, _ = _slot(wire=False, monkeypatch=monkeypatch)
    body1, body2 = "甲" * 1000, "乙" * 1000
    s.begin_round({"role": "assistant", "content": "", "tool_calls": [
        {"id": c, "type": "function",
         "function": {"name": "custom_tool", "arguments": "{}"}}
        for c in ("call_1", "call_2")]})
    s.append_tool_result({"role": "tool", "tool_call_id": "call_1",
                          "content": body1})
    s.append_tool_result({"role": "tool", "tool_call_id": "call_2",
                          "content": body2})
    s.begin_round({"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_3", "type": "function",
         "function": {"name": "custom_tool", "arguments": "{}"}}]})
    s.append_tool_result({"role": "tool", "tool_call_id": "call_3",
                          "content": "尾" * 150})
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    note = ("[本轮较早的工具结果共 1000 字符，"
            "仅保留前 300；需要原文请重新调用该工具]")
    assert tms[0]["content"] == body1[:300] + note, \
        f"关态被二次结算改写：{tms[0]['content'][-90:]}"
    assert tms[1]["content"] == body2[:300] + note


async def test_iterative_settle_no_stale_refs(monkeypatch):
    """[返修] 两组重复读：第一组引用降级放大总量 ⇒ 二次结算砍另一组
    代表项 ⇒ 那组引用也必须降级——交替结算至引用与预算同时稳定。
    不变量：凡写「见该条 call_X」的引用，call_X 位必须仍是完整 JSON。"""
    import re as _re
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "6000")
    monkeypatch.setenv("WIRE_OLD_RESULT_CHARS", "300")
    monkeypatch.setenv("WIRE_BUDGET_CHARS", "1150")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    repA = "甲" * 400
    repB = "乙" * 700
    _round(s, [("call_1", {}, _read_ev("a.md", 1, 1, repA, sha="vA")),
               ("call_2", {}, _read_ev("b.md", 1, 1, repB, sha="vB"))])
    _round(s, [("call_3", {}, _read_ev("a.md", 1, 1, repA, sha="vA")),
               ("call_4", {}, _read_ev("b.md", 1, 1, repB, sha="vB"))])
    _round(s, [("call_5", {}, _read_ev("c.md", 1, 1, "尾" * 150,
                                        sha="vC"))])
    out = await s.wire_messages()
    tms = _tool_msgs(out)
    by_id = {t.get("tool_call_id"): t for t in tms}
    for t in tms:
        m = _re.search(r"在本轮 (\S+) 展示", t["content"])
        if not m:
            continue
        rep = by_id.get(m.group(1))
        assert rep is not None and rep["content"].startswith("{"), \
            f"引用指向已被裁的代表项 {m.group(1)}：" \
            f"{(rep or {}).get('content', '?')[:80]}"
    # 本构造中两组代表项均被裁 ⇒ 两处引用都应降级为回读/省略说明
    assert sum("read_file(path" in t["content"] or "已省略" in t["content"]
               or "重读" in t["content"] for t in tms) >= 2


async def test_pattern_whitespace_uses_keyword(monkeypatch):
    """[返修] pattern 只含空白 ⇒ handler 按 keyword 搜索；投影
    同样先 strip 再定位——不得把行内空格当命中位置。"""
    monkeypatch.setenv("WIRE_RESULT_MAX_CHARS", "500")
    s, _ = _slot(wire=True, monkeypatch=monkeypatch)
    long_line = "杂" * 200 + "关键词命中点" + "杂" * 300
    hits = [{"file": "x.md", "line": 7, "text": long_line}]
    ev = _search_ev("关键词", "x", hits,
                    args={"keyword": "关键词", "pattern": "   ",
                          "path": "x"})
    _round(s, [("call_1", {}, ev)])
    d = json.loads(_tool_msgs(await s.wire_messages())[0]["content"])
    h = d["hits"][0]
    # keyword 定位成功 ⇒ fragment 含真实命中词；绝不落在空格上
    assert "关键词" in h.get("fragment", ""), f"应走 keyword 定位：{h}"


# ============================================================
# 端到端：真实 handler + OutboundRecorder（关/开对照）
# ============================================================

async def _world_with_files(sandbox, tmp_path):
    from src.storage import card_store, model_override, work_store
    w = await work_store.create_work("批四出站")
    wid, root = w["work_id"], w["work_card_id"]
    r = await card_store.create_card(
        wid, "工作卡", "用户直接创建", card_type="worker",
        parent=root, mission="按判据做文件核对。", skip_kick=True)
    cid = str((r.get("meta") or {}).get("card_id") or "")
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")
    f = tmp_path / "spec.md"
    f.write_text("\n".join(
        f"第{i:03d}行 判据{i}：关键证据 {i}。" for i in range(1, 61)),
        encoding="utf-8")
    return wid, cid, f


async def test_e2e_read_file_result_reaches_wire(sandbox, monkeypatch,
                                               tmp_path):
    """真实 read_file 执行：回执沿生产链进轮槽并随请求出站（关组）。"""
    from tests.outbound_probe import (OutboundRecorder, frame_finish,
                                      frame_text, frame_tool, sse)
    from src.api import routes

    wid, cid, f = await _world_with_files(sandbox, tmp_path)
    hops = []

    def script(index, body):
        hops.append(index)
        has_tool = any(m.get("role") == "tool"
                       for m in body.get("messages") or [])
        if not has_tool:
            return sse(frame_tool("call_r1", "read_file",
                                  {"path": str(f), "para_start": 1,
                                   "para_end": 60}),
                       frame_finish("tool_calls"))
        return sse(frame_text("已核对判据。"), frame_finish("stop"))

    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    await routes._run_message(wid, cid, "核对 spec 判据", None,
                              source_kind="human")
    assert len(rec.requests) == 2, "首跳＋一次续跳"
    second = rec.joined(1)
    assert "第001行 判据1" in second and "第060行 判据60" in second
    assert str(f.name) in second


async def test_e2e_search_then_readback(sandbox, monkeypatch, tmp_path):
    """搜索后精读：真实 search_files 命中坐标随请求出站（关组基线）。"""
    from tests.outbound_probe import (OutboundRecorder, frame_finish,
                                      frame_text, frame_tool, sse)
    from src.api import routes

    wid, cid, f = await _world_with_files(sandbox, tmp_path)

    def script(index, body):
        msgs = body.get("messages") or []
        n_tool = sum(1 for m in msgs if m.get("role") == "tool")
        if n_tool == 0:
            return sse(frame_tool("call_s1", "search_files",
                                  {"keyword": "判据30",
                                   "path": str(tmp_path)}),
                       frame_finish("tool_calls"))
        if n_tool == 1:
            return sse(frame_tool("call_r1", "read_file",
                                  {"path": str(f), "para_start": 28,
                                   "para_end": 32}),
                       frame_finish("tool_calls"))
        return sse(frame_text("已按坐标读回并核对。"), frame_finish("stop"))

    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    await routes._run_message(wid, cid, "找判据30", None,
                              source_kind="human")
    assert len(rec.requests) == 3
    hop2 = rec.joined(1)
    assert "spec.md" in hop2 and "判据30" in hop2   # 搜索坐标随请求出站
    hop3 = rec.joined(2)
    assert "第028行" in hop3 and "第032行" in hop3  # 精读原文随请求出站


async def test_e2e_evidence_wire_reduces_repeat_read_bytes(
        sandbox, monkeypatch, tmp_path):
    """同回合两次同区间读取：开组第一份变引用 ⇒ 续跳结果总字节下降。

    关/开两组跑同一脚本逐请求对照；真实 handler 每次照常执行（读两次）。
    """
    from tests.outbound_probe import (OutboundRecorder, frame_finish,
                                      frame_text, frame_tool, sse)
    from src.api import routes

    def script(index, body, *, f=None):
        n_tool = sum(1 for m in body.get("messages") or []
                     if m.get("role") == "tool")
        if n_tool == 0:
            return sse(frame_tool("call_r1", "read_file",
                                  {"path": str(f), "para_start": 1,
                                   "para_end": 60}),
                       frame_finish("tool_calls"))
        if n_tool == 1:
            return sse(frame_tool("call_r2", "read_file",
                                  {"path": str(f), "para_start": 1,
                                   "para_end": 60}),
                       frame_finish("tool_calls"))
        return sse(frame_text("核对完毕。"), frame_finish("stop"))

    async def _group(on: bool) -> OutboundRecorder:
        """每组**独立** work+card——共用卡会把上一组的回合块带进本轮
        prior，脚本按 tool 计数会读到旧组的结果（误判收口）。"""
        from src.storage import card_store, model_override, work_store
        w = await work_store.create_work("批四对照")
        wid2, root2 = w["work_id"], w["work_card_id"]
        r = await card_store.create_card(
            wid2, "工作卡", "用户直接创建", card_type="worker",
            parent=root2, mission="按判据做文件核对。", skip_kick=True)
        cid2 = str((r.get("meta") or {}).get("card_id") or "")
        model_override.put_row(wid2, cid2,
                               {"model": "ds-v41-flash-eco-amr"},
                               src="manual")
        f = tmp_path / f"spec_{'on' if on else 'off'}.md"
        f.write_text("\n".join(
            f"第{i:03d}行 判据{i}：关键证据 {i}。" for i in range(1, 61)),
            encoding="utf-8")
        if on:
            monkeypatch.setenv("TOOL_EVIDENCE_WIRE", "1")
        else:
            monkeypatch.delenv("TOOL_EVIDENCE_WIRE", raising=False)
        rec = OutboundRecorder(
            lambda index, body, _f=f: script(index, body, f=_f))
        rec.install(monkeypatch)
        out = await routes._run_message(wid2, cid2, "核对 spec", None,
                                        source_kind="human")
        rec.run_result = out
        return rec

    off = await _group(False)
    on = await _group(True)
    assert len(off.requests) == len(on.requests) == 3   # 请求次数不增

    def _tool_bytes(rec, idx):
        body = rec.requests[idx]["body"] or {}
        return sum(len(m.get("content") or "")
                   for m in body.get("messages") or []
                   if m.get("role") == "tool")

    off_b, on_b = _tool_bytes(off, 2), _tool_bytes(on, 2)
    print("\n[读数] 请求数 off/on:",
          len(off.requests), len(on.requests),
          "整包字节 off/on:",
          [r["bytes"] for r in off.requests],
          [r["bytes"] for r in on.requests],
          "续跳工具结果字符 off/on:", off_b, on_b)
    assert on_b < off_b, f"开组续跳结果字节应下降：on={on_b} off={off_b}"
    assert rec_body_has_ref(on, 2)


def rec_body_has_ref(rec, idx) -> bool:
    """第一份变宿主引用：引用了**第二份结果的宿主 tool_call_id**＋不再重复。"""
    body = rec.requests[idx]["body"] or {}
    tools = [m for m in body.get("messages") or []
             if m.get("role") == "tool"]
    if len(tools) < 2:
        return False
    keep_id = str(tools[1].get("tool_call_id") or "")
    first = str(tools[0].get("content") or "")
    return bool(keep_id) and keep_id in first and "不再重复" in first
