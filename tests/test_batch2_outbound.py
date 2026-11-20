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
from typing import Any, Callable, Optional

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

LIST_MARK = "〔卡片清单〕"
CHILD_MARK = "〔直接子卡现状〕"
DEP_MARK = "## 依赖关联"


# ============================================================
# 出站抓取器
# ============================================================

class OutboundRecorder:
    """拦 httpx 出站，记录**实际发送的请求正文**。"""

    def __init__(self, responder: Callable[[int, dict], Any]):
        self.requests: list[dict[str, Any]] = []
        self._responder = responder

    async def send(self, client, request, **kwargs):  # noqa: ANN001
        raw = request.content or b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 非 JSON 体也记下来（字节照样算）
            body = {}
        rec = {"index": len(self.requests), "url": str(request.url),
               "bytes": len(raw), "body": body, "raw": raw}
        self.requests.append(rec)
        out = self._responder(rec["index"], body)
        if isinstance(out, BaseException):
            raise out
        return httpx.Response(200, content=out, request=request)

    def install(self, monkeypatch) -> "OutboundRecorder":
        """挂到 httpx 上。**必须是普通函数**（挂成 bound method 不会重绑 self，
        参数会错位——实测踩过：适配器侧报的是「API request … failed」）。"""
        rec = self

        async def _send(client, request, **kwargs):  # noqa: ANN001
            return await rec.send(client, request, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "send", _send)
        return self

    # -- 取数面 ---------------------------------------------------------
    def texts(self, index: int) -> list[str]:
        """第 index 次出站里所有消息的正文（按 wire 顺序）。"""
        body = self.requests[index]["body"] or {}
        out: list[str] = []
        for m in body.get("messages") or []:
            c = m.get("content")
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                out.append("".join(str(x.get("text") or "") for x in c
                                   if isinstance(x, dict)))
        return out

    def joined(self, index: int) -> str:
        return "\n".join(self.texts(index))

    def count_mark(self, index: int, mark: str) -> int:
        return self.joined(index).count(mark)

    def system_text(self, index: int) -> str:
        body = self.requests[index]["body"] or {}
        for m in body.get("messages") or []:
            if m.get("role") == "system":
                c = m.get("content")
                return c if isinstance(c, str) else str(c or "")
        return ""


def sse(*frames: dict) -> bytes:
    """OpenAI 兼容 chat-completions 的 SSE 帧序列。"""
    out = ["data: " + json.dumps(fr, ensure_ascii=False) + "\n\n" for fr in frames]
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def frame_text(text: str, model: str = "fake-model") -> dict:
    return {"model": model, "choices": [{"index": 0, "delta": {"content": text}}]}


def frame_tool(call_id: str, name: str, args: dict, model: str = "fake-model") -> dict:
    return {"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": call_id, "type": "function",
         "function": {"name": name,
                      "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


def frame_finish(reason: str = "tool_calls", model: str = "fake-model") -> dict:
    return {"model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def lcp_bytes(a: str, b: str) -> int:
    """两段文本的最长公共前缀（UTF-8 字节）。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return len(a[:i].encode("utf-8"))


def _seg_bytes(joined: str, mark: str, header: str) -> int:
    """某份材料在出站正文里的字节数。

    口径：tail 档的材料是**独立消息**（按消息边界算）；system 档的材料是区段
    （从区头到下一个区头）。
    """
    if mark and mark in joined:
        # 独立消息：标记在消息开头 ⇒ 取整条消息长度
        i = joined.find(mark)
        start = joined.rfind("\n", 0, i) + 1
        j = joined.find("\n", i)
        # 该消息到下一个消息为止：找下一个「标记行或角色行」的起点不靠猜——
        # 直接按「下一个换行 + 另一个标记」切，够用且不误伤。
        nxt = len(joined)
        for other in (CHILD_MARK, LIST_MARK):
            k = joined.find(other, i + 1)
            if k > 0:
                nxt = min(nxt, joined.rfind("\n", 0, k) + 1)
        seg = joined[start:nxt] if nxt > start else joined[start:]
        return len(seg.encode("utf-8"))
    i = joined.find(header)
    if i < 0:
        return 0
    j = joined.find("\n## ", i + 1)
    seg = joined[i:j if j > 0 else len(joined)]
    return len(seg.encode("utf-8"))


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
    tid = await _mk("目标卡", root)

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


# ============================================================
# 回包脚本：首跳调工具 → 续跳直答；第一次尝试可故意断连（同跳重试）
# ============================================================

class TurnScript:
    """按请求内容回包（不靠次数）：带工具结果 ⇒ 续跳；否则 ⇒ 首跳。"""

    def __init__(self, *, fail_first: bool = False, on_first_hop=None):
        self.fail_first = fail_first
        self.on_first_hop = on_first_hop
        self.served = 0

    def __call__(self, index: int, body: dict) -> Any:
        msgs = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        if has_tool_result:
            # 工具续跳轮：直答收口
            return sse(frame_text("已按判据核对完毕。"), frame_finish("stop"))
        # 首跳：先按需断连一次（同位置重发），否则真调一个工具
        if self.fail_first and self.served == 0:
            self.served += 1
            raise httpx.ConnectError("（替身）首试断连——触发同位置重发")
        if self.on_first_hop is not None:
            self.on_first_hop()
        return sse(
            frame_tool("call_1", "read_card", {"card_id": "", "block": "现状"}),
            frame_finish("tool_calls"),
        )


async def _run_turn(wid: str, card_id: str, script: TurnScript,
                     monkeypatch) -> OutboundRecorder:
    """走生产入口跑一轮（routes._run_message），出站全被记录。"""
    from src.api import routes
    rec = OutboundRecorder(script)
    rec.install(monkeypatch)
    await routes._run_message(wid, card_id, "把这件事按判据做完", None,
                              source_kind="human")
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
async def test_three_groups_readings(sandbox, monkeypatch):
    """三组读数：第一批完成／仅移位／按任务缩量（同一棵树、同一入口）。"""
    from src.api import context_regions

    w = await _mk_world(sandbox)
    wid = w["wid"]

    async def _run_group(delivery: str, scope_on: bool) -> dict[str, Any]:
        """跑一组：每组**独立一棵工作树**（否则上一组的对话历史会进下一组的
        prior，回包脚本按「有没有 tool 结果」判轮次就会认错）。"""
        monkeypatch.setenv("CONTEXT_SCOPE", "1" if scope_on else "0")
        g = await _mk_world(sandbox)
        patched = []
        for spec in context_regions.REGION_TABLE:
            if spec.get("key") in ("card_list", "child_status"):
                patched.append((spec, spec.get("delivery"), spec.get("render")))
                spec["delivery"] = delivery
                if delivery == "system":
                    # 移位前：这两区的载荷是**字符串**（无 tail 来源标记）。
                    # 现在它们渲染成 tail 事实消息 ⇒ 这里取回正文（去掉标记行）。
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
            rec = await _run_turn(g["wid"], g["mid"], script, monkeypatch)
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
                "list_bytes": _seg_bytes(joined, LIST_MARK, "## 卡片清单"),
                "child_bytes": _seg_bytes(joined, CHILD_MARK, "## 直接子卡"),
                "dep_bytes": _seg_bytes(joined, DEP_MARK, "## 依赖关联"),
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

    a = await _run_group("system", False)   # 第一批完成（清单/子卡还在 system）
    b = await _run_group("tail", False)     # 仅移位（清单/子卡走当轮，未缩量）
    c = await _run_group("tail", True)      # 按任务缩量（移位＋缩量）

    # A：清单/子卡在 system（无来源标记）；B/C：在 tail（system 里没有）
    assert a["list_header_in_system"] == 1 and a["child_header_in_system"] == 1
    assert b["list_header_in_system"] == 0 and b["child_header_in_system"] == 0
    assert c["list_header_in_system"] == 0 and c["child_header_in_system"] == 0
    assert b["list_in_tail"] == 1 and c["list_in_tail"] == 1
    assert b["child_in_tail"] == 1 and c["child_in_tail"] == 1
    # 缩量 ⇒ 出站更小
    assert c["total_bytes"] <= b["total_bytes"], "缩量后不该变大"
    evidence = {"group_a_batch1_only": a, "group_b_tail_only": b,
                "group_c_tail_and_narrowed": c}
    out_dir = os.environ.get("BATCH2_EVIDENCE_DIR")
    if out_dir:
        out = Path(out_dir) / "batch2_groups.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


@pytest.mark.asyncio
async def test_prefix_divergence_moves_later(sandbox, monkeypatch):
    """前缀分歧：只改**一项子卡状态**，两次可比请求的首次分歧位置与公共前缀。

    口径（任务书 §五.2）：按**适配器实际交付的消息顺序**比较——把出站请求的
    system 段与 messages 段按 wire 顺序拼成一条可比文本，取最长公共前缀。
    移位前那项状态在 system（分歧早），移位后在 tail（分歧后移）。
    """
    from src.api import context_assembler, context_regions
    from src.storage import card_store

    async def _model_input(wid: str, card_id: str) -> tuple[str, str]:
        ctx = context_assembler.build_ctx(wid, card_id)
        out = context_regions.compose_model_context(ctx, profile="main")
        parts = [out["system_prompt"]]
        for m in (out["prior_messages"] or []):
            parts.append(str(m.get("content") or ""))
        parts.append(str(out.get("user_prompt") or ""))
        for m in (out.get("turn_tail") or []):
            parts.append(str(m.get("content") or ""))
        return "\n".join(parts), str(out["system_prompt"])

    async def _measure(delivery: str) -> dict[str, Any]:
        # **只比移位**：两档都用完整材料（CONTEXT_SCOPE=0），差别只在槽位
        # （system ↔ tail）⇒ 分歧位置的差异才是移位带来的。
        monkeypatch.setenv("CONTEXT_SCOPE", "0")
        g = await _mk_world(sandbox)
        wid, mid = g["wid"], g["mid"]
        child = g["children"][0]
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
            before, before_sys = await _model_input(wid, mid)
            # 只改**这一项**子卡状态（别的材料不动）
            await card_store.update_card(
                wid, child, {"status_block": "甲的现状。〔本轮改了状态〕" + "细节。" * 400},
                "改一项状态")
            after, after_sys = await _model_input(wid, mid)
        finally:
            for spec, old_delivery, old_render in patched:
                spec["delivery"] = old_delivery
                spec["render"] = old_render
        # 整条输入的公共前缀
        n = min(len(before), len(after))
        i = 0
        while i < n and before[i] == after[i]:
            i += 1
        lcp = len(before[:i].encode("utf-8"))
        # **可缓存前缀（system）**的公共前缀——这才是前缀缓存真正能复用的那段
        ns = min(len(before_sys), len(after_sys))
        j = 0
        while j < ns and before_sys[j] == after_sys[j]:
            j += 1
        sys_lcp = len(before_sys[:j].encode("utf-8"))
        return {
            "delivery": delivery,
            "before_bytes": len(before.encode("utf-8")),
            "after_bytes": len(after.encode("utf-8")),
            "system_bytes": len(before_sys.encode("utf-8")),
            "input_common_prefix_bytes": lcp,
            "input_divergence_ratio": round(lcp / max(1, len(before.encode("utf-8"))), 4),
            "system_common_prefix_bytes": sys_lcp,
            "system_divergence_ratio": round(
                sys_lcp / max(1, len(before_sys.encode("utf-8"))), 4),
            "divergence_context": before[max(0, i - 70):i + 130],
        }

    a = await _measure("system")   # 移位前：状态在 system ⇒ 可缓存前缀被截断
    b = await _measure("tail")     # 移位后：状态在 tail ⇒ 可缓存前缀完整
    assert b["system_common_prefix_bytes"] >= a["system_common_prefix_bytes"], (
        f"移位后可缓存前缀不得变短：移位前 {a['system_common_prefix_bytes']}B "
        f"→ 移位后 {b['system_common_prefix_bytes']}B")
    assert b["input_common_prefix_bytes"] > a["input_common_prefix_bytes"], (
        f"移位后分歧必须后移：移位前 {a['input_common_prefix_bytes']}B "
        f"→ 移位后 {b['input_common_prefix_bytes']}B")
    out_dir = os.environ.get("BATCH2_EVIDENCE_DIR")
    if out_dir:
        out = Path(out_dir) / "batch2_divergence.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"before_shift": a, "after_shift": b},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"before_shift": a, "after_shift": b},
                     ensure_ascii=False, indent=2))
