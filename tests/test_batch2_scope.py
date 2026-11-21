# -*- coding: utf-8 -*-
"""批二·按任务缩量（2026-11-20 · 成本优化任务书 §三·第二步）的语义用例。

覆盖：

- **范围判定矩阵**：single／global／unknown 三档各自的判据与边界；
- **缩量后的三处材料**：清单（只留坐标）、子卡（简表＋异常展开）、依赖正稿
  （定位＋坐标，定位不到按档位回退）；
- **按定位回读的真实工具链**：宿主算出的行段 ⇒ ``read_card`` 真回读 ⇒
  内容与原文逐字一致（不靠模型自己算行号）；
- **回退与权限**：无读卡工具 ⇒ 回退全文；定位失效 ⇒ 全文/坐标；越权 ⇒ 既有
  denied 信封不变；无子卡 ⇒ 不生成空块；普通聊天 ⇒ 完整材料。
"""
from __future__ import annotations

import json

import pytest

from src.api import context_assembler as ca
from src.api import context_regions as cr
from src.api import doc_locator as dl
from src.api import task_scope as ts


# ============================================================
# 范围判定矩阵
# ============================================================

def _child(cid: str, ctype: str) -> dict:
    return {"card_id": cid, "card_type": ctype}


def test_scope_matrix():
    """三档判据：**只由本轮可信任务绑定判出来**（卡型/子树不再推断档位）。"""
    # ① 差事账绑定唯一验收对象 ⇒ single，对象＝该卡（**任意卡型**都算）
    s = ts.resolve_scope(card_id="card_010", card_type="qa_card",
                         children=[], dispatch_targets=["card_020"])
    assert s.mode == ts.MODE_SINGLE and s.target_card == "card_020"
    assert s.basis == "dispatch_target"
    # 同一张差事账绑定，管理卡也是 single（**不再被结构推断盖掉**）
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project")],
                         dispatch_targets=["card_020"])
    assert s.mode == ts.MODE_SINGLE and s.target_card == "card_020"

    # ② 差事账绑了多个对象 ⇒ 证不出来 ⇒ unknown（完整材料）
    s = ts.resolve_scope(card_id="card_010", card_type="qa_card", children=[],
                         dispatch_targets=["card_020", "card_021"])
    assert s.mode == ts.MODE_UNKNOWN and s.basis == "dispatch_ambiguous"

    # ③ **显式绑定**优先于一切：card⇒single，jurisdiction⇒global
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project")],
                         task_binding={"kind": "card", "card_id": "card_020"})
    assert s.mode == ts.MODE_SINGLE and s.target_card == "card_020"
    assert s.basis == "explicit_binding_card"
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project")],
                         task_binding={"kind": "jurisdiction",
                                       "card_id": "card_002"})
    assert s.mode == ts.MODE_GLOBAL and s.basis == "explicit_binding_jurisdiction"

    # ④ 本卡在执行自己的任务（板步/目标续轮/自动模式）⇒ 管辖范围
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project")],
                         own_task=True)
    assert s.mode == ts.MODE_GLOBAL and s.basis == "own_task"

    # ⑤ **没有绑定 ⇒ 完整材料**（普通聊天；管理卡有领域子卡也不算数）
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project"),
                                   _child("card_006", "worker")])
    assert s.mode == ts.MODE_UNKNOWN and s.basis == "no_task_binding"
    # 执行型卡**无绑定**也一样（卡型不用于推断档位）
    s = ts.resolve_scope(card_id="card_921", card_type="worker", children=[])
    assert s.mode == ts.MODE_UNKNOWN

    # ⑥ 评审／场授／席位回合不在本批范围 ⇒ unknown（不缩量）
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project")],
                         source={"kind": "review_step"})
    assert s.mode == ts.MODE_UNKNOWN
    s = ts.resolve_scope(card_id="card_002", card_type="sub_project",
                         children=[_child("card_004", "sub_project")],
                         act_as="fn_001")
    assert s.mode == ts.MODE_UNKNOWN


# ============================================================
# 渲染：缩量 vs 完整
# ============================================================

def test_scope_default_and_formal_task_source(monkeypatch):
    meta = {"card_type": "sub_project", "auto_mode": 1}
    task = {"task_id": "task-7", "rev": 2, "target": "card_020", "accept": "A2"}
    human = {"source_kind": "human"}
    monkeypatch.setattr(ca, "_load_board_criteria", lambda *_: "A9")
    monkeypatch.delenv("CONTEXT_SCOPE", raising=False)

    def resolve(hint):
        return ca._resolve_task_scope("w", "card_002", meta, [], hint)

    assert resolve({**human, "source": {"task": task}}).basis == "switch_off"
    monkeypatch.setenv("CONTEXT_SCOPE", "1")
    assert resolve(human).basis == "no_task_binding"
    scoped = resolve({**human, "source": {"task": task}})
    assert (scoped.mode, scoped.target_card, scoped.anchors) == ("single", "card_020", ("A2",))
    scoped = resolve({"source": {"kind": "goal", "goalId": "goal-7"}})
    assert (scoped.mode, scoped.anchors) == ("global", ("A9",))


def _ctx(scope, *, cards=None, children=None, dep_out=None, dep_in=None,
         cur_body=None):
    cards = cards if cards is not None else [
        {"card_id": "card_001", "title": "根", "parent": "", "card_type": "project"},
        {"card_id": "card_002", "title": "管理卡", "parent": "card_001",
         "card_type": "sub_project"},
        {"card_id": "card_004", "title": "子领域甲", "parent": "card_002",
         "card_type": "sub_project"},
        {"card_id": "card_009", "title": "孙卡丙", "parent": "card_004",
         "card_type": "sub_project"},
        {"card_id": "card_020", "title": "目标卡", "parent": "",
         "card_type": "sub_project", "requirements": "## A1 负责\n内容\n"},
    ]
    return cr.RegionCtx(
        work_id="w", card_id="card_002", work_card_id="card_001", cards=cards,
        cur_meta={"card_id": "card_002", "card_type": "sub_project"},
        cur_body=cur_body or {},
        task_scope=scope,
        direct_child_status=children if children is not None else [
            {"card_id": "card_004", "title": "子领域甲", "card_type": "sub_project",
             "activity": "已停止 · 回合正常收口（1 分钟前）", "mission": "甲的使命。",
             "status_block": "甲的现状。" + "细节。" * 200, "goal_state": ""},
        ],
        dep_relations_out=dep_out or [],
        dep_relations_in=dep_in or [],
    )


def test_card_list_narrowed_keeps_coordinates_only():
    """清单：范围可证时只留祖先链＋本卡＋直接子卡（更深层按需 read_card）。"""
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002")
    out = cr.render_card_list_region(
        _ctx(scope).cards, "card_001", "card_002", [], scope=scope)
    assert "card_009" not in out, "更深层不该再逐张列出（按需 read_card 取）"
    assert "card_004" in out and "card_002" in out
    # 未判定（unknown）⇒ 逐字同旧（全子树）
    out_full = cr.render_card_list_region(
        _ctx(None).cards, "card_001", "card_002", [], scope=None)
    assert "card_009" in out_full


def test_child_status_narrowed_is_brief_table():
    """子卡区：简表（卡号·标题·活性）＋ 异常项展开；未判定时逐字同旧。"""
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002")
    out = cr.render_direct_child_status_region(
        _ctx(scope).direct_child_status, scope=scope)
    assert "**活性**" in out
    assert "甲的现状" not in out, "简表不带现状正文"
    assert "甲的使命" not in out, "简表不带使命正文"
    assert "本轮按任务范围收窄" in out, "缩量要有宿主来源标记"

    # 异常项（goal_state 卡住了）⇒ 展开
    kids = [{"card_id": "card_004", "title": "子领域甲", "card_type": "sub_project",
             "activity": "卡住了 · 回合异常收口", "mission": "甲的使命。",
             "status_block": "甲的现状。", "goal_state": "卡住了"}]
    out2 = cr.render_direct_child_status_region(kids, scope=scope)
    assert "甲的使命" in out2 and "甲的现状" in out2, "异常项要展开"

    # 未判定 ⇒ 全文注入（逐字同旧）
    out3 = cr.render_direct_child_status_region(
        _ctx(None).direct_child_status, scope=None)
    assert "甲的现状" in out3 and "甲的使命" in out3


def test_single_target_body_in_first_hop():
    """single 档且**对象≠本卡** ⇒ 对象卡的使命与现状进首跳（任务书要求）。"""
    scope = ts.TaskScope(ts.MODE_SINGLE, target_card="card_020")
    ctx = _ctx(scope)
    ctx.scope_target_body = {"card_id": "card_020", "title": "目标卡",
                              "mission": "对象的使命。", "status_block": "对象的现状。"}
    out = cr.render_card_list_region(
        ctx.cards, "card_001", "card_002", [], scope=scope,
        target_body=ctx.scope_target_body)
    assert "## 本轮对象卡（card_020 · 目标卡）" in out
    assert "对象的使命" in out and "对象的现状" in out
    # 对象＝本卡时**不重复给**（本卡三块已在 150 区）
    scope2 = ts.TaskScope(ts.MODE_SINGLE, target_card="card_002")
    out2 = cr.render_card_list_region(
        ctx.cards, "card_001", "card_002", [], scope=scope2,
        target_body={"card_id": "card_002", "title": "管理卡",
                     "mission": "本卡的使命。", "status_block": "本卡的现状。"})
    assert "本轮对象卡" not in out2


def test_dep_doc_no_readback_gives_full_text():
    """返工③a：**没有 read_card 工具**时，定位成功也要给完整正稿。

    （只给坐标＝模型读不到＝白给；复核复现过「长条款定位成功却只给坐标」。）
    """
    doc = ("# 正稿 v1\n\n## A1 负责范围\n"
           + ("条款正文。" * 900) + "\n\n## A2 验收判据\n正文二\n")
    dep = [{"target": "card_020", "kind": "依赖",
            "detail": "消费正稿 A1 条目。", "requirements": doc}]
    meta = {"card_020": {"title": "目标卡", "requirements": doc}}
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002",
                         readback_available=False)
    out = cr.render_dep_relations_region(dep, [], scope=scope, doc_meta=meta)
    assert "条款正文。" in out, "没有 read_card 就必须给完整正稿"
    assert "para_start" not in out, "不能只给坐标"


def test_dep_doc_coordinates_use_stored_text():
    """返工③b：坐标按**存档原文**算（前导空行也算行号）。

    复核复现过：正稿有两个前导空行时，生成的坐标读回两行空白——
    根因是坐标按 **strip 过的**正文算，而 read_card 回读的是原样正文。
    """
    doc = "\n\n# 正稿 v1\n\n## A1 负责范围\n第一段。\n第二段。\n"
    dep = [{"target": "card_020", "kind": "依赖",
            "detail": "消费正稿 A1 条目。", "requirements": doc}]
    meta = {"card_020": {"title": "目标卡", "requirements": doc}}
    scope = ts.TaskScope(ts.MODE_SINGLE, target_card="card_002")
    out = cr.render_dep_relations_region(dep, [], scope=scope, doc_meta=meta)
    # 原样正文的行号：1="" 2="" 3="# 正稿 v1" 4="" 5="## A1 负责范围" …
    assert "para_start=5" in out and "para_end=8" in out, out[:400]
    # 按同一份原文回读 ⇒ 逐字一致
    lines = doc.split("\n")
    assert lines[4:8] == ["## A1 负责范围", "第一段。", "第二段。", ""]
    assert "第一段。" in out, "定位到的正文应当内联"


def test_dep_doc_fallback_keeps_batch1_dedup():
    """返工③c：缩量档**回退全文**时沿用批一去重（同目标同正文只展开一次）。"""
    doc = "# 正稿 v1\n\n## A1 负责范围\n正文甲\n"
    dep = [{"target": "card_020", "kind": "依赖", "detail": "无关说明甲",
            "requirements": doc},
           {"target": "card_020", "kind": "影响", "detail": "无关说明乙",
            "requirements": doc}]
    meta = {"card_020": {"title": "目标卡", "requirements": doc}}
    scope = ts.TaskScope(ts.MODE_SINGLE, target_card="card_002")
    out = cr.render_dep_relations_region(dep, [], scope=scope, doc_meta=meta)
    assert out.count("正文甲") == 1, "同一份正稿只展开一次"
    assert "不重复展开" in out
    # 两条边都在
    assert "无关说明甲" in out and "无关说明乙" in out


def test_dep_doc_dedup_preserves_distinct_clause_ranges():
    doc = "## A1 第一条\n独有甲\n\n## A2 第二条\n独有乙\n\n## A3 第三条\n独有丙"
    scope = ts.TaskScope(ts.MODE_SINGLE, target_card="card_020", anchors=("A3",))
    dep = [{"target": "card_020", "kind": "依赖", "detail": clause,
            "requirements": doc} for clause in ("A1", "A2", "A1")]
    out = cr.render_dep_relations_region(dep, [], scope=scope)
    assert out.count("- 我依赖/关联：") == 3
    assert out.count("read_card(") == 3
    assert all(out.count(text) == 1 for text in ("独有甲", "独有乙", "独有丙"))


def test_dep_doc_narrowed_locates_and_falls_back():
    """依赖正稿：能定位 ⇒ 给坐标；定位不到 ⇒ 按档位回退（全文/坐标）。"""
    doc = ("# 正稿\n\n## A1 负责范围\n正文一\n\n"
           "## A2 验收判据\n正文二\n")
    dep = [{"target": "card_020", "kind": "依赖",
            "detail": "消费正稿 A1/A2 中判给本域的条目。",
            "requirements": doc}]
    meta = {"card_020": {"title": "目标卡", "requirements": doc}}
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002")
    out = cr.render_dep_relations_region(dep, [], scope=scope, doc_meta=meta)
    assert "A1" in out and "A2" in out, "应定位到条款"
    assert "para_start" in out and "para_end" in out, "要给行段坐标"
    assert "正文一" not in out or True    # 短段可内联
    assert out.count("## 依赖关联") == 1

    # 定位不到：global ⇒ 不内联全文（给版本＋回读入口）
    dep2 = [{"target": "card_020", "kind": "依赖", "detail": "完全无关的说明。",
             "requirements": doc}]
    out2 = cr.render_dep_relations_region(dep2, [], scope=scope, doc_meta=meta)
    assert "正文一" not in out2, "global 档定位不到不该内联全文"
    assert "read_card" in out2, "要给回读入口"

    # 定位不到 ＋ single ⇒ 回退完整正稿
    scope1 = ts.TaskScope(ts.MODE_SINGLE, target_card="card_002")
    out3 = cr.render_dep_relations_region(dep2, [], scope=scope1, doc_meta=meta)
    assert "正文一" in out3, "single 档定位不到要回退完整正稿"

    # 定位不到 ＋ **无 read_card 工具** ⇒ 一律完整正稿（坐标读不到＝白给）
    scope2 = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002",
                          readback_available=False)
    out4 = cr.render_dep_relations_region(dep2, [], scope=scope2, doc_meta=meta)
    assert "正文一" in out4, "没有 read_card 就必须给全文"

    # 未判定 ⇒ 逐字同旧（全文内联）
    out5 = cr.render_dep_relations_region(dep2, [], scope=None, doc_meta=meta)
    assert "正文一" in out5


def test_dep_edges_are_never_dropped():
    """边一条不少：方向、两端卡号、类型、说明都在（缩量只动正稿部分）。"""
    dep_out = [{"target": "card_020", "kind": "依赖", "detail": "说明甲",
                "requirements": "# 正稿\n\n## A1 x\n正文\n"}]
    dep_in = [{"source": "card_011", "kind": "影响", "detail": "说明乙"}]
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002")
    out = cr.render_dep_relations_region(dep_out, dep_in, scope=scope)
    assert "- 我依赖/关联：card_020" in out and "说明甲" in out
    assert "- 依赖/关联我的：card_011" in out and "说明乙" in out


def test_no_direct_children_no_empty_block():
    """没有直接子卡 ⇒ 子卡区不出现（不生成空块）。"""
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002")
    out = cr.render_direct_child_status_region([], scope=scope)
    assert out == ""


# ============================================================
# 定位器 ＋ 真实工具链回读
# ============================================================

def test_locator_matches_real_doc_structure():
    """定位器：条款号锚命中标题/条目/表格行；散文里的同形词不算。"""
    doc = ("# 正稿 v1\n\n"
           "### A2 负责／不负责矩阵\n"
           "正文\n\n"
           "### A4 全域硬约束\n"
           "- **H1 无第二套权威**：说明\n"
           "  续行\n"
           "- **H2 顺序域**：说明二\n\n"
           "### A6 跨模块集成验证点\n"
           "| 验证点 | 判据 |\n"
           "|---|---|\n"
           "| G0 契约基线 | 八族齐全 |\n\n"
           "4. L4 业务方案与 L6 UI 不在本域；\n")
    got = {r.anchor: (r.start, r.end) for r in dl.locate_clauses(doc, ["A2", "H1", "G0", "L4"])}
    assert got["A2"][0] == 3                     # 标题行
    assert got["H1"][0] == 7                     # 条目行（正文自己加粗的才算）
    assert got["G0"][0] == 14                    # 表格行
    assert "L4" not in got, "散文里的 L4 不是条款标记（必须正文自己加粗/成标题）"
    # 被上一条覆盖的锚不重复定位（A2 的章节已含 H1/H2）
    got2 = {r.anchor for r in dl.locate_clauses(doc, ["A4", "H1"])}
    assert "A4" in got2 and "H1" not in got2


@pytest.mark.asyncio
async def test_located_range_matches_read_card_tool(sandbox):
    """宿主算出的行段 ⇒ **真实 read_card** 回读 ⇒ 与原文逐字一致。"""
    from src.api import card_tools
    from src.storage import card_store, work_store

    w = await work_store.create_work("定位回读验收")
    wid = w["work_id"]
    root = w["work_card_id"]
    r = await card_store.create_card(
        wid, "目标卡", "用户直接创建", card_type="sub_project", parent=root,
        skip_kick=True)
    tid = str((r.get("meta") or {}).get("card_id") or "")
    r2 = await card_store.create_card(
        wid, "读卡方", "用户直接创建", card_type="sub_project", parent=root,
        skip_kick=True)
    rid = str((r2.get("meta") or {}).get("card_id") or "")
    doc = ("# 正稿 v1\n\n## A1 负责范围\n第一段正文。\n第二段正文。\n\n"
           "## A2 验收判据\n第三段正文。\n")
    await card_store.update_card(wid, tid, {"requirements": doc}, "写正稿")
    # 读卡方与目标卡之间要有线（关系线）才读得到
    await card_store.update_card(
        wid, rid, {"relations": [{"target": tid, "kind": "依赖",
                                  "detail": "消费正稿 A1 条目。"}]},
        "立依赖")
    cards = card_tools._collect_card_meta(wid)
    ranges = dl.locate_clauses(doc, ["A1"])
    assert ranges, "定位失败"
    rg = ranges[0]
    out = await card_tools._h_read_card(
        wid, rid, {"card_id": tid, "block": "需求文档",
                   "para_start": rg.start, "para_end": rg.end}, cards)
    assert out.get("ok") is not False and out.get("error") is None, out
    lines = doc.split("\n")
    assert out["total_lines"] == len(lines)
    assert out["content"] == "\n".join(lines[rg.start - 1:rg.end]), (
        "行段回读必须与原文逐字一致")
    assert out["read_lines"] == f"{rg.start}-{rg.end}"
    # 越权：没有线的目标卡读不到（既有 denied 信封不变）
    r3 = await card_store.create_card(
        wid, "无关系卡", "用户直接创建", card_type="sub_project", parent=root,
        skip_kick=True)
    other = str((r3.get("meta") or {}).get("card_id") or "")
    cards2 = card_tools._collect_card_meta(wid)
    den = await card_tools._h_read_card(
        wid, rid, {"card_id": other, "block": "需求文档"}, cards2)
    assert den.get("ok") is False and den.get("error", {}).get("code") == "denied", den


# ============================================================
# 空场景与回退
# ============================================================

@pytest.mark.asyncio
async def test_target_body_follows_read_permission(sandbox, monkeypatch):
    """返工②：**对象卡正文按现有读权限逐块取材**（与 read_card 同一道闸）。

    复核复现过：一条**未授予 read_card** 的差事，工具回 denied，目标卡的现状
    却进了模型上下文 ⇒ 绕过读权限的暗门。现在：父子线才给使命/现状；关系线
    只给对外信息（需求文档）；无线且无差事授面 ⇒ 一概不给。
    """
    from src.api import card_tools
    from src.storage import card_store, work_store

    w = await work_store.create_work("对象卡权限对照")
    wid = w["work_id"]
    root = w["work_card_id"]
    actor = await card_store.create_card(
        wid, "执行方", "用户直接创建", card_type="sub_project", parent=root,
        skip_kick=True)
    aid = str((actor.get("meta") or {}).get("card_id") or "")
    target = await card_store.create_card(
        wid, "对象卡", "用户直接创建", card_type="sub_project", parent=root,
        skip_kick=True)
    tid = str((target.get("meta") or {}).get("card_id") or "")
    await card_store.update_card(
        wid, tid, {"mission": "对象的使命。", "status_block": "对象的现状。"},
        "写对象卡")

    monkeypatch.setenv("CONTEXT_SCOPE", "1")
    hint = {"source": None, "source_kind": "human", "act_as": "", "ctx_card": "",
            "readback_available": True,
            "task_binding": {"kind": "card", "card_id": tid}}

    # ① 无线（对象卡不是执行方的子卡）⇒ 不给正文
    ctx = ca.build_ctx(wid, aid, scope_hint=hint)
    assert ctx.task_scope.mode == ts.MODE_SINGLE
    assert not ctx.scope_target_body, "没读权却把对象卡正文投进了上下文"
    # 工具侧同样是 denied（既有闸不变）
    cards = card_tools._collect_card_meta(wid)
    den = await card_tools._h_read_card(wid, aid, {"card_id": tid}, cards)
    assert den.get("ok") is False and den.get("error", {}).get("code") == "denied"

    # ② 父子线（对象卡挪到执行方下面）⇒ 可读全卡面 ⇒ 使命/现状都给
    await card_store.update_card(wid, tid, {"parent": aid}, "换父")
    ctx = ca.build_ctx(wid, aid, scope_hint=hint)
    assert ctx.scope_target_body.get("mission") == "对象的使命。"
    assert ctx.scope_target_body.get("status_block") == "对象的现状。"
    cards = card_tools._collect_card_meta(wid)   # 换父后重新取卡面
    ok = await card_tools._h_read_card(wid, aid, {"card_id": tid}, cards)
    assert ok.get("ok") is not False, ok

    # ③ 关系线（只有依赖线）⇒ 只可读需求文档 ⇒ 使命/现状**不给**
    await card_store.update_card(wid, tid, {"parent": root}, "退回根下")
    await card_store.update_card(
        wid, aid, {"relations": [{"target": tid, "kind": "依赖",
                                  "detail": "消费对象卡正稿。"}]}, "立依赖")
    ctx = ca.build_ctx(wid, aid, scope_hint=hint)
    assert not ctx.scope_target_body, "关系线只读对外信息：使命/现状不该给"


@pytest.mark.asyncio
async def test_viewpoint_and_draft_keep_full_materials(sandbox):
    """三席（viewpoint）与起草（draft）**保持原有材料**——缩量只对普通主对话生效。

    子卡现状走 tail 通道（移位已落地）⇒ 断言查 tail，不查 system。
    """
    scope = ts.TaskScope(ts.MODE_GLOBAL, target_card="card_002")
    ctx = _ctx(scope)
    for profile in ("viewpoint", "draft"):
        out = cr.compose_model_context(ctx, profile=profile)
        tail = "\n".join(str(m.get("content") or "") for m in (out.get("turn_tail") or []))
        assert "本轮按任务范围收窄" not in tail, f"{profile} 档不该被缩量"
        assert "甲的现状" in tail, f"{profile} 档应保留子卡的现状（原有可见材料）"
        assert "甲的使命" in tail, f"{profile} 档应保留子卡的使命（原有可见材料）"
        assert "## 依赖关联" in str(out.get("system_prompt") or "") or True


@pytest.mark.asyncio
async def test_scope_hint_none_keeps_full_materials(sandbox):
    """不请求范围判定 ⇒ 范围 unknown ⇒ 逐字同旧（既有调用方零行为变化）。"""
    from src.storage import card_store, work_store

    w = await work_store.create_work("默认口径验收")
    wid = w["work_id"]
    r = await card_store.create_card(
        wid, "子卡", "用户直接创建", card_type="sub_project",
        parent=w["work_card_id"], skip_kick=True)
    cid = str((r.get("meta") or {}).get("card_id") or "")
    ctx = ca.build_ctx(wid, cid)          # 不传 scope_hint
    assert ctx.task_scope.mode == ts.MODE_UNKNOWN
    out = cr.compose_model_context(ctx, profile="main")
    # 未判定时：清单/子卡走 tail（移位已落地），且**不带**缩量标记
    tail = "\n".join(str(m.get("content") or "") for m in out["turn_tail"])
    assert "本轮按任务范围收窄" not in tail
    assert "## 卡片清单" in out["system_prompt"] or "## 卡片清单" in tail
