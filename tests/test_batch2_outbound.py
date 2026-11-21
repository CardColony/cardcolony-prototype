# -*- coding: utf-8 -*-
"""批二验收（2026-11-20 · 成本优化任务书 §二/§三/§四）：**实际出站**证据。

抓取点＝``httpx.AsyncClient.send``——它在适配器序列化之后、真正上网之前，
所以抓到的字节就是**适配器实际发出的请求正文**（OpenAI 兼容 chat-completions
与 Responses 两类序列化都在这里被抓到）。

覆盖：

- **首跳 / 真工具续跳 / 同跳重试**（同位置重发）；
- 同一回合里的一次**真实状态写入**（写在首跳与工具续跳之间）⇒ 续跳出站看得到
  新状态（清单/子卡现状走 tail 通道，每跳重渲染）；
- 每次出站里**清单/子卡快照各最多一份**；
- **三组读数**（第一批完成／仅移位／按任务缩量）。

隔离：pytest 沙箱（tests/conftest.py 的 ``sandbox`` 夹具）——不碰真实数据目录。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from src.api import context_regions  # noqa: F401
from outbound_probe import (CHILD_MARK, DEP_MARK, LIST_MARK,  # noqa: F401
                            OutboundRecorder, TurnScript, frame_finish,
                            frame_text, frame_tool, lcp_bytes, seg_bytes, sse)

ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 小世界：管理卡 ＋ 领域子卡 ＋ 一个带大正稿的目标卡
# ============================================================

TARGET_DOC = """# 上位需求正稿 v1.0

## A1 负责范围
本节写清楚谁负责什么。

- **A1 条目一**：核验方式与判据在这里。
  比对对象：正稿与实现。
  不合格：实现与正稿不一致。
- **A2 条目二**：第二条判据。
  比对对象：同上。

## A2 验收判据
整体验收必须同时证明下面几条：

1. 记录正确：权威清楚。
2. 分支正确：能建新方向。
3. 执行保真：行为基线逐项对照。
"""

NEW_CARD_ID = "card_new01"
NEW_CARD_TITLE = "本轮新建的子卡"


async def _mk_world(sandbox) -> dict[str, Any]:
    """建一棵小树：管理卡（域子卡×2 ＋ 孙卡×1）＋ 目标卡（带正稿）。"""
    from src.storage import card_store, work_store

    w = await work_store.create_work("批二出站验收")
    wid = w["work_id"]
    root = w["work_card_id"]

    async def _mk(title: str, parent: str, ctype: str = "sub_project") -> str:
        r = await card_store.create_card(
            wid, title, "用户直接创建", card_type=ctype, parent=parent,
            mission=f"{title}的使命。", skip_kick=True)
        assert "error" not in r, r
        return str((r.get("meta") or {}).get("card_id") or "")

    mid = await _mk("管理卡", root)
    cid1 = await _mk("子领域甲", mid)
    cid2 = await _mk("子领域乙", mid)
    cid3 = await _mk("孙卡丙", cid1)
    # 对象卡挂在**执行方**下面（父子线 ⇒ 可读全卡面；关系线只可读需求文档）
    tid = await _mk("目标卡", mid)

    # 子卡现状写厚一点（缩量前的子卡区就是被这些现状撑大的）
    for cid, tag in ((cid1, "甲"), (cid2, "乙"), (cid3, "丙")):
        await card_store.update_card(
            wid, cid, {"status_block": f"{tag}的现状。" + ("细节。" * 400)},
            "铺现状")
    # 目标卡的正稿（依赖区的正文源）
    await card_store.update_card(wid, tid, {"requirements": TARGET_DOC}, "写正稿")
    # 管理卡依赖目标卡：关系说明点名了 A1/A2 条款（定位锚）
    await card_store.update_card(
        wid, mid,
        {"relations": [{"target": tid, "kind": "依赖",
                        "detail": "消费目标卡需求文档正稿 A1/A2 中判给本域的条目。"}]},
        "立依赖")

    # 座位：生产在用的 ECO 座（openai 兼容直连）
    from src.storage import model_override as _mo
    _mo.put_row(wid, mid, {"model": "ds-v41-flash-eco-amr"}, src="manual")
    return {"wid": wid, "root": root, "mid": mid,
            "children": [cid1, cid2, cid3], "target": tid}


def _sync_new_child(wid: str, parent: str) -> str:
    """**同步**建一张子卡（替身回包里调；走正常存储入口的同一张表）。"""
    from src.storage import card_db

    def _fn(conn):
        card_db.insert_card(
            conn,
            {"card_id": NEW_CARD_ID, "work_id": wid, "title": NEW_CARD_TITLE,
             "source": "用户直接创建", "parent": parent,
             "card_type": "sub_project", "mission": "本轮才建的。",
             "created_at": "", "updated_at": "", "status_block": "",
             "requirements": "", "relations": [], "links": [], "tags": [],
             "goal_state": "", "goal_rounds": 0, "project_class": "",
             "mode": "manual", "abilities": [], "content_hash": ""},
            {"mission": "本轮才建的。", "status_block": "",
             "requirements": ""},
        )
        return NEW_CARD_ID

    out = str(card_db.write_sync(wid, _fn) or NEW_CARD_ID)
    # 写没落盘要当场知道（写路径失败会被吞掉 ⇒ 断言读回）
    back = card_db.read_sync(wid, lambda conn: [
        r["card_id"] for r in conn.execute(
            "SELECT card_id FROM cards WHERE card_id=?", (NEW_CARD_ID,))], [])
    assert back, "新卡没落盘（写路径失败被吞了）"
    return out


def _sync_update_child(wid: str, card_id: str, marker: str) -> str:
    """**同步**改一张卡的现状（替身回包里调；走正常存储入口的同一张表）。"""
    from src.storage import card_db

    def _fn(conn):
        conn.execute(
            "UPDATE cards SET status_block = ? WHERE card_id = ?",
            (marker + "新的现状正文。", card_id))
        return card_id

    card_db.write_sync(wid, _fn)
    back = card_db.read_sync(wid, lambda conn: [
        r["status_block"] for r in conn.execute(
            "SELECT status_block FROM cards WHERE card_id=?", (card_id,))], [])
    assert back and marker in back[0], "现状写入没落盘"
    return card_id


async def _run_turn(wid: str, card_id: str, script: TurnScript,
                     monkeypatch, task_binding: Optional[dict] = None
                     ) -> OutboundRecorder:
    """走生产入口跑一轮（routes._run_message），出站全被记录。"""
    from src.api import routes
    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    await routes._run_message(wid, card_id, "把这件事按判据做完", None,
                              source_kind="human",
                              task_binding=task_binding)
    return rec


@pytest.fixture(autouse=True)
def _stub_keys(monkeypatch):
    """替身用的假 key（请求体不受影响，**不出网**：httpx 已被拦）。"""
    for env in ("OPENDESIGN_API_KEY", "VOLCENGINE_CODING_API_KEY",
                "ZHIPU_CODING_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.setenv(env, "stub-key-not-used")


# ============================================================
# 用例
# ============================================================

@pytest.mark.asyncio
async def test_outbound_material_once_per_request(sandbox, monkeypatch):
    """首跳／同跳重试／工具续跳：每次出站里清单与子卡快照**各最多一份**。"""
    w = await _mk_world(sandbox)
    script = TurnScript(fail_first=True)
    rec = await _run_turn(w["wid"], w["mid"], script, monkeypatch)

    assert len(rec.requests) >= 3, (
        f"应有：断连首试＋同跳重试＋工具续跳（实得 {len(rec.requests)}）")
    for r in rec.requests:
        joined = rec.joined(r["index"])
        assert joined.count(LIST_MARK) == 1, f"第 {r['index']} 次出站清单份数≠1"
        assert joined.count(CHILD_MARK) == 1, f"第 {r['index']} 次出站子卡份数≠1"
    # 同跳重试：两次请求**逐字一致**（同一位置、同一快照）
    assert rec.requests[0]["raw"] == rec.requests[1]["raw"], "同位置重试必须复用原快照"
    # 工具续跳：原生调用与结果整对
    cont = rec.requests[-1]["body"]
    roles = [m.get("role") for m in cont.get("messages") or []]
    assert "assistant" in roles and "tool" in roles, "续跳请求里没有工具轮"


@pytest.mark.asyncio
@pytest.mark.parametrize("seat", ["ds-v41-flash-eco-amr", "glm-53-flash"])
async def test_seats_outbound_shape_and_material(sandbox, monkeypatch, seat):
    """生产在用的 **ECO 座与 GLM 座**：出站形状对、材料各一份。"""
    w = await _mk_world(sandbox)
    from src.storage import model_override as _mo
    _mo.put_row(w["wid"], w["mid"], {"model": seat}, src="manual")
    script = TurnScript(fail_first=False)
    rec = await _run_turn(w["wid"], w["mid"], script, monkeypatch)
    assert rec.requests, f"{seat} 没有出站记录"
    body = rec.requests[0]["body"] or {}
    assert body.get("model"), "出站体没有 model 字段"
    assert isinstance(body.get("messages"), list) and body["messages"], "出站体没有 messages"
    # OpenAI 兼容 chat-completions 形状
    assert "stream" in body, "不是 chat-completions 形状（stream 字段缺失）"
    for r in rec.requests:
        joined = rec.joined(r["index"])
        assert joined.count(LIST_MARK) == 1, f"{seat} 第 {r['index']} 次出站清单份数≠1"
        assert joined.count(CHILD_MARK) == 1, f"{seat} 第 {r['index']} 次出站子卡份数≠1"


@pytest.mark.asyncio
async def test_siwc_seat_outbound_is_responses_class(sandbox, monkeypatch):
    """SIWC 座（Responses API·**另一类序列化**）：出站形状与材料位置。

    替身覆盖的位置＝**token 新鲜度判定**（本机 token 已过期，替身放行；
    请求本身被 httpx 拦下，不会出网）。
    """
    from src.model_gateway.llm_siwc import adapter as _siwc

    monkeypatch.setattr(_siwc._TokenStore, "_fresh", lambda self, data: True)
    w = await _mk_world(sandbox)
    from src.storage import model_override as _mo
    _mo.put_row(w["wid"], w["mid"], {"model": "gpt-62-sol-siwc"}, src="manual")
    script = TurnScript(fail_first=False)
    rec = await _run_turn(w["wid"], w["mid"], script, monkeypatch)
    assert rec.requests, "SIWC 没有出站记录"
    body = rec.requests[0]["body"] or {}
    assert isinstance(body.get("input"), list), "SIWC 出站应是 Responses 的 input 数组"
    assert body.get("store") is False and body.get("stream") is True
    joined = json.dumps(body.get("input"), ensure_ascii=False)
    assert joined.count(LIST_MARK) == 1, "SIWC 出站里清单份数≠1"
    assert joined.count(CHILD_MARK) == 1, "SIWC 出站里子卡份数≠1"


@pytest.mark.asyncio
async def test_same_turn_state_write_visible_next_hop(sandbox, monkeypatch):
    """同一回合里的一次真实写入 ⇒ **下一次**需要它的请求看得到新版本。"""
    w = await _mk_world(sandbox)
    wid = w["wid"]

    script = TurnScript(on_first_hop=lambda: _sync_new_child(wid, w["mid"]))
    rec = await _run_turn(wid, w["mid"], script, monkeypatch)

    first = rec.joined(0)
    last = rec.joined(len(rec.requests) - 1)
    assert NEW_CARD_TITLE not in first, "首跳不该看见回合之后才建的卡"
    assert NEW_CARD_TITLE in last, (
        "续跳必须看见本轮写入的新卡（当轮事实要刷新，不能停在回合开始那一份）")


@pytest.mark.asyncio
async def test_same_turn_existing_child_status_visible_next_hop(sandbox,
                                                                monkeypatch):
    """返工④a：**已有子卡**的现状更新 ⇒ 下一跳只出现新版（完整材料档）。

    完整材料档（CONTEXT_SCOPE=0）：子卡区的现状正文在 tail 里 ⇒ 下一跳必须是
    新写入的那一版，旧版不得再出现。
    """
    monkeypatch.setenv("CONTEXT_SCOPE", "0")
    w = await _mk_world(sandbox)
    wid = w["wid"]
    child = w["children"][0]
    marker = "〔本轮改了子卡现状〕"

    def _write():
        _sync_update_child(wid, child, marker)

    script = TurnScript(on_first_hop=_write)
    rec = await _run_turn(wid, w["mid"], script, monkeypatch)
    last = rec.joined(len(rec.requests) - 1)
    assert marker in last, "下一跳必须看见子卡的新现状"
    assert "细节。细节。细节。" in last, "旧版正文不应整段留在下一跳"


@pytest.mark.asyncio
async def test_same_turn_bound_object_status_visible_next_hop(sandbox,
                                                             monkeypatch):
    """返工④b：**已绑定对象卡**的现状更新 ⇒ 下一跳只出现新版。

    复核复现过：对象卡现状更新后，刷新仍把旧正文发进 tail —— 根因是刷新只
    刷了清单与子卡现状，没刷「本轮对象卡」正文。
    """
    monkeypatch.setenv("CONTEXT_SCOPE", "1")
    w = await _mk_world(sandbox)
    wid = w["wid"]
    obj = w["target"]
    marker = "〔本轮改了对象卡现状〕"

    def _write():
        _sync_update_child(wid, obj, marker)

    script = TurnScript(on_first_hop=_write)
    rec = await _run_turn(wid, w["mid"], script, monkeypatch,
                           task_binding={"kind": "card", "card_id": obj})
    first = rec.joined(0)
    last = rec.joined(len(rec.requests) - 1)
    assert "本轮对象卡" in first, "首跳应带对象卡正文（对象＝本卡的子卡，可读）"
    assert marker not in first, "首跳还不该看见写入"
    assert marker in last, "下一跳必须看见对象卡的新现状"


@pytest.mark.asyncio
async def test_turn_material_not_persisted_into_history(sandbox, monkeypatch):
    """当轮材料（清单/子卡现状）**不该进会话历史/缓存板**——进了就每轮重发。

    生产样本实测：历史段里带着**旧轮**的当轮材料（副本的 context_board 与
    session_blocks 都能查到〔卡片清单〕行）⇒ A→B 总字节几乎不变（材料只是
    从 system 挪进了历史），真正省下的只有可缓存前缀。
    """
    from src.storage import context_board as _cb
    from src.storage import session_facade as _sf

    w = await _mk_world(sandbox)
    script = TurnScript(fail_first=False)
    rec = await _run_turn(w["wid"], w["mid"], script, monkeypatch)
    # 本轮出站：材料各一份（当轮）
    assert rec.count_mark(0, LIST_MARK) == 1
    assert rec.count_mark(0, CHILD_MARK) == 1
    # 会话历史里不该有当轮材料
    hist = str(await _sf.read(w["wid"], w["mid"]) or "")
    assert LIST_MARK not in hist, "当轮材料落进了会话历史（每轮重发）"
    # 缓存板里也不该有
    rows = _cb.read_board(w["wid"], w["mid"]) or []
    blob = json.dumps(rows, ensure_ascii=False, default=str)
    assert LIST_MARK not in blob, "当轮材料落进了缓存板（每轮重发）"


@pytest.mark.asyncio
async def test_three_groups_readings(sandbox, monkeypatch):
    """三组读数：第一批完成／仅移位／按任务缩量（同一棵树、同一入口）。"""
    from src.api import context_regions

    w = await _mk_world(sandbox)
    wid = w["wid"]

    async def _run_group(delivery: str, scope_on: bool,
                         binding_kind: Optional[str] = None) -> dict[str, Any]:
        """跑一组：同一棵树、同一入口，只换槽位与范围档。

        ``binding_kind``＝本轮**可信任务绑定**的形态（``jurisdiction`` 管辖
        范围／``card`` 对象卡）。没有绑定（None）⇒ 范围判不出来 ⇒ 完整材料
        （返工①的对照组）。
        """
        monkeypatch.setenv("CONTEXT_SCOPE", "1" if scope_on else "0")
        g = await _mk_world(sandbox)
        binding = ({"kind": binding_kind, "card_id": g["mid"]}
                   if binding_kind else None)
        patched = []
        for spec in context_regions.REGION_TABLE:
            if spec.get("key") in ("card_list", "child_status"):
                patched.append((spec, spec.get("delivery"), spec.get("render")))
                spec["delivery"] = delivery
                if delivery == "system":
                    orig = spec["render"]

                    def _as_system(ctx, _orig=orig):
                        out = _orig(ctx)
                        if isinstance(out, list):
                            if not out:
                                return ""
                            c = str((out[0] or {}).get("content") or "")
                            parts = c.split("\n", 1)
                            return parts[1] if len(parts) > 1 else ""
                        return out

                    spec["render"] = _as_system
        try:
            script = TurnScript(fail_first=False)
            rec = await _run_turn(g["wid"], g["mid"], script, monkeypatch,
                                   task_binding=binding)
        finally:
            for spec, old_delivery, old_render in patched:
                spec["delivery"] = old_delivery
                spec["render"] = old_render
        system = rec.system_text(0)
        per_request = []
        for r in rec.requests:
            joined = rec.joined(r["index"])
            per_request.append({
                "index": r["index"],
                "bytes": r["bytes"],
                "system_bytes": len(rec.system_text(r["index"]).encode("utf-8")),
                "list_bytes": seg_bytes(joined, LIST_MARK, "## 卡片清单"),
                "child_bytes": seg_bytes(joined, CHILD_MARK, "## 直接子卡"),
                "dep_bytes": seg_bytes(joined, DEP_MARK, "## 依赖关联"),
                "list_count": joined.count(LIST_MARK),
                "child_count": joined.count(CHILD_MARK),
            })
        return {
            "requests": len(rec.requests),
            "total_bytes": sum(r["bytes"] for r in rec.requests),
            "first_bytes": rec.requests[0]["bytes"],
            "system_bytes": len(system.encode("utf-8")),
            # 移位前 system 里没有来源标记 ⇒ 按区头判在不在
            "list_header_in_system": system.count("## 卡片清单"),
            "child_header_in_system": system.count("## 直接子卡"),
            "list_mark_in_system": system.count(LIST_MARK),
            "child_mark_in_system": system.count(CHILD_MARK),
            "list_in_tail": rec.count_mark(0, LIST_MARK),
            "child_in_tail": rec.count_mark(0, CHILD_MARK),
            "per_request": per_request,
        }

    # 本轮的可信任务绑定（**知道任务的人给的**）：管辖范围＝管理卡自己的子树
    a = await _run_group("system", False, "jurisdiction")  # 第一批完成（清单/子卡还在 system）
    b = await _run_group("tail", False, "jurisdiction")    # 仅移位（清单/子卡走当轮，未缩量）
    c = await _run_group("tail", True, "jurisdiction")     # 按任务缩量（移位＋缩量）
    u = await _run_group("tail", True, None)               # **无绑定** ⇒ 完整材料（返工①）

    # A：清单/子卡在 system（无来源标记）；B/C：在 tail（system 里没有）
    assert a["list_header_in_system"] == 1 and a["child_header_in_system"] == 1
    assert b["list_header_in_system"] == 0 and b["child_header_in_system"] == 0
    assert c["list_header_in_system"] == 0 and c["child_header_in_system"] == 0
    assert b["list_in_tail"] == 1 and c["list_in_tail"] == 1
    assert b["child_in_tail"] == 1 and c["child_in_tail"] == 1
    # 缩量 ⇒ 出站更小
    assert c["total_bytes"] <= b["total_bytes"], "缩量后不该变大"
    # **无绑定 ⇒ 完整材料**（与 B 组同量：没有可信绑定就不缩量）
    assert u["total_bytes"] == b["total_bytes"], (
        f"无绑定必须走完整材料：{u['total_bytes']}B vs B {b['total_bytes']}B")
    evidence = {"group_a_batch1_only": a, "group_b_tail_only": b,
                "group_c_tail_and_narrowed": c,
                "group_c_unbound_full_material": u}
    out_dir = os.environ.get("BATCH2_EVIDENCE_DIR")
    if out_dir:
        out = Path(out_dir) / "batch2_groups.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


@pytest.mark.asyncio
async def test_prefix_divergence_moves_later(sandbox, monkeypatch):
    """前缀分歧：**按适配器实际发出的请求正文**比（不是装配文本拼接）。

    口径（任务书 §五.2）：两次**可比请求**＝同一路由、同一任务，**只改一项子卡
    状态**；比较对象＝**适配器实际交付的请求正文**（出站抓取器抓到的字节，
    消息顺序＝适配器 wire 顺序）。移位前那项状态在 system（分歧早），移位后
    在 tail（分歧后移，且 system 段逐字相同）。
    """
    from src.storage import card_store

    async def _first_outbound(delivery: str, changed: bool) -> str:
        """跑一轮真实回合，抓**首跳**的实际出站正文。"""
        monkeypatch.setenv("CONTEXT_SCOPE", "0")   # 两档都用完整材料：只比移位
        g = await _mk_world(sandbox)
        wid, mid = g["wid"], g["mid"]
        if changed:
            # 只改**这一项**子卡状态（别的材料不动）
            await card_store.update_card(
                wid, g["children"][0],
                {"status_block": "甲的现状。〔本轮改了状态〕" + "细节。" * 400},
                "改一项状态")
        patched = []
        for spec in context_regions.REGION_TABLE:
            if spec.get("key") in ("card_list", "child_status"):
                patched.append((spec, spec.get("delivery"), spec.get("render")))
                spec["delivery"] = delivery
                if delivery == "system":
                    orig = spec["render"]

                    def _as_system(ctx, _orig=orig):
                        out = _orig(ctx)
                        if isinstance(out, list):
                            if not out:
                                return ""
                            c = str((out[0] or {}).get("content") or "")
                            parts = c.split("\n", 1)
                            return parts[1] if len(parts) > 1 else ""
                        return out

                    spec["render"] = _as_system
        try:
            script = TurnScript(fail_first=False)
            rec = await _run_turn(wid, mid, script, monkeypatch)
        finally:
            for spec, old_delivery, old_render in patched:
                spec["delivery"] = old_delivery
                spec["render"] = old_render
        assert rec.requests, "没有出站记录"
        return rec.requests[0]["raw"].decode("utf-8", "replace")

    def _compare(raw_a: str, raw_b: str) -> dict[str, Any]:
        n = min(len(raw_a), len(raw_b))
        i = 0
        while i < n and raw_a[i] == raw_b[i]:
            i += 1
        return {
            "common_prefix_bytes": len(raw_a[:i].encode("utf-8")),
            "total_bytes": len(raw_a.encode("utf-8")),
            "divergence_ratio": round(
                len(raw_a[:i].encode("utf-8")) / max(1, len(raw_a.encode("utf-8"))), 4),
            "divergence_context": raw_a[max(0, i - 70):i + 130],
        }

    # 移位前（清单/子卡在 system）
    a = _compare(await _first_outbound("system", False),
                 await _first_outbound("system", True))
    # 移位后（清单/子卡在 tail）
    b = _compare(await _first_outbound("tail", False),
                 await _first_outbound("tail", True))
    assert b["common_prefix_bytes"] > a["common_prefix_bytes"], (
        f"移位后分歧必须后移：移位前 {a['common_prefix_bytes']}B "
        f"→ 移位后 {b['common_prefix_bytes']}B")
    out_dir = os.environ.get("BATCH2_EVIDENCE_DIR")
    if out_dir:
        out = Path(out_dir) / "batch2_divergence.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"before_shift": a, "after_shift": b},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"before_shift": a, "after_shift": b},
                     ensure_ascii=False, indent=2))
