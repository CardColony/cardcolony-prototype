# -*- coding: utf-8 -*-
"""F11/F12/F13 · 渲染/上下文修复（2026-10-31 移植对齐第三批）。

F11 记忆渲染补全（对照 dsh projectMyMemory）：逐栏/日期前缀/锚点行/文档行/
展开说明/孤儿 L2 照出/L2 process 全文/近期远期分段。
F12 区级异常隔离：单区渲染炸 ⇒ 记日志跳过、整轮装配继续。
F13 卡间消息 system 化：卡间块投影为 role=system，不再冒充用户发言。
"""
from __future__ import annotations

import json

import pytest

from src.api import context_regions
from src.api import card_msg
from src.storage import memory_domain, memory_store


def _row(nid: str, batch: int, ordinal: int, gist: str, *, level: int = 1,
         role: str = "gist", upper_id: str = "", anchor_seq: int = 0,
         occurred_at: str = "2026-10-05T12:00:00+08:00", **extra):
    """造一行 memory_nodes（渲染器的真实输入）。"""
    r = {"id": nid, "work_id": "w", "card_id": "c", "batch": batch,
         "ordinal": ordinal, "occurred_at": occurred_at, "level": level,
         "role": role, "gist": gist, "anchor_seq": anchor_seq,
         "upper_id": upper_id}
    r.update(extra)
    return r


# ---------------------------------------------------------------- F11

def test_f11_l1_columns_anchor_and_expand_howto():
    rows = [_row("w/c/B0001/0001", 1, 1, "完成果", anchor_seq=3,
                 start_text="起因甲", process="经过乙", result="结果丙", lesson="教训丁")]
    text = context_regions.render_memory_region(rows)
    # 逐栏四件 + 日期前缀 + 锚点行 + 展开说明
    assert "起因：起因甲" in text and "经过：经过乙" in text
    assert "结果：结果丙" in text and "教训：教训丁" in text
    assert "（10-05 12:00）完成果" in text, "首栏日期前缀（MM-DD HH:mm）"
    assert "@锚点{c,3}" in text, "锚点行"
    assert "memory_expand" in text, "区头展开说明"


def test_f11_doc_lines_from_doc_paths_refs():
    rows = [_row("w/c/B0001/0001", 1, 1, "带文档结论", anchor_seq=2,
                 doc_paths=json.dumps(["docs/a.md"]),
                 doc_refs=json.dumps([{"path": "docs/a.md", "seq": 5}]))]
    text = context_regions.render_memory_region(rows)
    assert "文档：docs/a.md @seq 5" in text, "文档行带首现 seq"


def test_f11_orphan_l2_rendered_at_tail():
    """孤儿 L2（父不在链上）挂段末照出——不静默丢。"""
    rows = [
        _row("w/c/B0001/0001", 1, 1, "有父结论"),
        _row("w/c/B0001/0001/d", 1, 2, "有父细节", level=2,
             role="description", upper_id="w/c/B0001/0001", process="细节正文"),
        _row("w/c/B0000/0009", 0, 3, "孤儿细节", level=2, upper_id="w/c/不存在"),
    ]
    text = context_regions.render_memory_region(rows)
    assert "有父细节" in text and "细节正文" in text, "L2 出 gist+process 全文"
    assert "孤儿细节" in text, "孤儿 L2 照出"
    assert "未挂父的细节" in text


def test_f11_folded_member_renders_as_l2_under_fold_node():
    """F2 归拢降层后：成员（level=2）作为新锚点的细节在近期段照出。"""
    rows = [
        _row("w/c/B0002/0001", 2, 1, "归拢新结论", anchor_seq=1000000000),
        _row("w/c/B0001/0001", 1, 2, "旧结论甲", level=2,
             upper_id="w/c/B0002/0001", anchor_seq=1, process="旧经过"),
    ]
    text = context_regions.render_memory_region(rows)
    assert "归拢新结论" in text
    assert "旧结论甲" in text, "降层成员仍可见（挂在归拢锚点下）"
    assert "旧经过" in text
    # 顶层判据＝level==1：旧结论不再作顶层散条
    assert text.count("旧结论甲") == 1


def test_f11_sunk_still_renders_in_baseline():
    """F15 口径：sunk 只影响凝练全景，**不淘汰上下文基线渲染**。"""
    rows = [_row("w/c/B0001/0001", 1, 1, "已沉没但基线可见", anchor_seq=1,
                 sunk=1)]
    text = context_regions.render_memory_region(rows)
    assert "已沉没但基线可见" in text


# ---------------------------------------------------------------- F12

def test_f12_region_exception_isolation(monkeypatch):
    """单区渲染抛异常 ⇒ 该区跳过、其余区照常装配（不炸整轮）。"""
    ctx = context_regions.RegionCtx(work_id="w", card_id="c")
    original = dict(context_regions.REGION_TABLE[0]) if context_regions.REGION_TABLE else None
    assert original is not None
    spec = dict(original)
    spec["render"] = lambda _ctx: (_ for _ in ()).throw(RuntimeError("渲染炸了"))
    fake_table = [spec] + [s for s in context_regions.REGION_TABLE[1:]]
    monkeypatch.setattr(context_regions, "REGION_TABLE", fake_table)
    out = context_regions.render_registered_regions(
        ctx, profile="main", deliveries={"system"})
    assert all(r["key"] != spec["key"] for r in out), "炸掉的区不出现"
    assert out, "其余区照常"


# ---------------------------------------------------------------- F13

def test_f13_card_message_role_system():
    """卡间消息投影为 role=system（不冒充用户发言）。"""
    block = {"role": "user", "actor": "card:card_002", "seq": 1,
             "content": card_msg.CONTEXT_HEAD + "\n正文"}
    out = context_regions.project_history_messages([block])
    assert len(out) == 1
    assert out[0]["role"] == "system", "卡间块不再以 user 角色进上下文"


def test_f13_human_message_stays_user():
    """真人发言不受影响：仍是 role=user。"""
    block = {"role": "user", "actor": "human", "seq": 1, "content": "真话"}
    out = context_regions.project_history_messages([block])
    assert out[0]["role"] == "user"


# ---------------------------------------------------------------- F15/F19（接线面）

async def test_f15_reclaim_wiring_sinks_old_deep_nodes(sandbox, work):
    """[F15] reclaim_turn_end：深层超 7d 未触达 ⇒ sunk=True 落链落 entries；
    基线渲染不看 sunk（已在 F11 用例钉），此处钉存储位。"""
    import time as _t
    wid, cid = work["work_id"], work["work_card_id"]
    old_ms = int(_t.time() * 1000) - 8 * 24 * 3600 * 1000   # 8 天前（> T_SINK 7d）
    top = {"anchor": {"sessionId": cid, "seq": 1}, "gist": "顶层", "level": 1,
           "role": "gist", "status": "done", "upper": [], "lower": [],
           "coveredSeqs": [1]}
    deep = {"anchor": {"sessionId": cid, "seq": 2}, "gist": "深层细节", "level": 2,
            "role": "gist", "upper": [dict(top["anchor"])], "lower": [],
            "coveredSeqs": [2], "lastUsedAt": old_ms}
    top["lower"] = [dict(deep["anchor"])]
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 3,
                                              "entries": [top, deep]})
    await memory_store.reclaim_turn_end(wid, cid)
    chain = memory_domain.load_chain(wid, cid)
    by_seq = {int(n["anchor"]["seq"]): n for n in chain}
    assert by_seq[2].get("sunk") is True, "深层超 7d ⇒ 沉没"
    assert by_seq[1].get("sunk") is not True, "顶层不沉（无 touch 豁免）"
    st = memory_store.load_memory(wid, cid)
    deep_entry = next(e for e in st["entries"] if int((e.get("anchor") or {}).get("seq") or 0) == 2)
    assert deep_entry.get("sunk") is True, "sunk 同步回 entries（链与条目同源）"
    # [F4 口径] 凝练全景消失、基线还在
    assert "深层细节" not in memory_store.render_condense_memory(st["entries"])
    from src.api import context_regions as _cr
    assert "深层细节" in _cr.render_memory_region(
        memory_store.load_nodes(wid, cid)), "基线渲染照出"


async def test_f19_memory_expand_tool_registered_and_expands(sandbox, work):
    """[F19] memory_expand 已注册：沿锚点展开＝结论＋细节组＋原文；越权拒。"""
    from src.api import card_tools
    assert "memory_expand" in card_tools.TOOL_REGISTRY
    assert "memory_expand" in card_tools.READONLY_TOOLS
    wid, cid = work["work_id"], work["work_card_id"]
    top = {"anchor": {"sessionId": cid, "seq": 1}, "gist": "顶层结论", "level": 1,
           "role": "gist", "status": "done", "upper": [], "lower": [],
           "coveredSeqs": [1], "process": "顶层经过"}
    child = {"anchor": {"sessionId": cid, "seq": 2}, "gist": "细节一", "level": 2,
             "role": "gist", "upper": [dict(top["anchor"])], "lower": [],
             "coveredSeqs": [2]}
    top["lower"] = [dict(child["anchor"])]
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 3,
                                              "entries": [top, child]})
    meta = {cid: {"card_id": cid, "parent": "", "card_type": "project",
                  "perms": ["read"]}}
    r = await card_tools.execute_tool(wid, cid, "memory_expand", {"seq": 1}, meta)
    assert r["ok"] is True and r["data"]["found"] is True
    text = r["data"]["text"]
    assert "【记忆】顶层结论" in text  # [F23] 回执形态去【结论】
    assert "细节一" in text, "整组展开：细节子条随组给出"
    # 缺 seq ⇒ 校验错误
    r2 = await card_tools.execute_tool(wid, cid, "memory_expand", {}, meta)
    assert r2["ok"] is False


# ---------------------------------------------------------------- F22（观测面）

def test_f22_observe_jsonl_written(tmp_path, monkeypatch):
    """[F22] 打点落 JSONL：model_call 行含 trace/caller/usage；永不抛。"""
    from src.infra import observe
    monkeypatch.setattr(observe, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(observe, "_dir_ready", False)
    observe.log_model_call(trace_id="tr-x", caller="condense:batch1", model="m",
                           prompt_tokens=11, completion_tokens=22,
                           latency_ms=33, finish_reason="stop")
    observe.log_condense_batch(work_id="w", card_id="c", batch_no=1, budget=60_000,
                               window_seqs=6, consumed=6, new_mark=6,
                               node_count=1, retries=0)
    observe.log_fold(work_id="w", card_id="c", merges=1, members=2, mark=3)
    observe.log_reclaim(work_id="w", card_id="c", sunk_total=2, newly_sunk=1)
    observe.log_regions(work_id="w", card_id="c",
                        regions=[{"key": "memory", "order": 200, "payload": "abc",
                                  "entries": 1}])
    files = list(tmp_path.glob("observe-*.jsonl"))
    assert files, "JSONL 文件落盘"
    lines = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    kinds = [l["kind"] for l in lines]
    assert kinds == ["model_call", "condense_batch", "fold", "reclaim", "regions"]
    mc = lines[0]
    assert mc["trace_id"] == "tr-x" and mc["caller"] == "condense:batch1"
    assert mc["prompt_tokens"] == 11 and mc["completion_tokens"] == 22
    cb = lines[1]
    assert cb["window_seqs"] == 6 and cb["new_mark"] == 6 and cb["node_count"] == 1


async def test_f22_call_main_emits_model_call_line(tmp_path, monkeypatch, sandbox):
    """[F22] 走 model_service.call_main 的真实链路（mock 网关）⇒ model_call JSONL 一条。"""
    import asyncio as _aio
    from src.infra import observe as _obs
    from src.api import model_service
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setattr(model_service, "_gateway", None)
    monkeypatch.setattr(_obs, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(_obs, "_dir_ready", False)
    chunks = []
    result, model_used = await model_service.call_main(
        "sys", "hello", on_chunk=chunks.append,
        trace_id="tr-e2e", caller="test", work_id="w", card_id="c")
    assert result.get("content"), "mock 网关有产出"
    assert chunks, "on_chunk 旁路收到 chunk（F8 静默判死的计数依据）"
    files = list(tmp_path.glob("observe-*.jsonl"))
    lines = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()
             if l.strip()] if files else []
    mc = [l for l in lines if l["kind"] == "model_call"]
    assert mc, "model_call 打点落盘"
    assert mc[0]["trace_id"] == "tr-e2e" and mc[0]["caller"] == "test"
    # [二轮审计] 卡/席位必须真填上（改前结构恒空）
    assert mc[0]["work_id"] == "w" and mc[0]["card_id"] == "c"
    assert mc[0]["seat"], "席位＝座位模型名，不得为空"
    # 在途台账：调用结束后 inflight 必须清空
    assert _obs.inflight() == []


# ---------------------------------------------------------------- F8（容错面）

async def test_f8_breaker_on_consecutive_real_failures(sandbox, monkeypatch):
    """[F8] 熔断只吃**真实失败反馈**：连续 3 次连接类错误 ⇒ 放弃本批。"""
    import asyncio as _aio
    from src.storage import memory_store as ms
    from src.api import model_service
    calls = {"n": 0}

    async def conn_fail(system_prompt, user_prompt, **_):
        calls["n"] += 1
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(model_service, "call_main", conn_fail)
    monkeypatch.setattr(ms, "CONDENSE_CALL_BACKOFF_SEC", 0.01)
    with pytest.raises(RuntimeError, match="熔断"):
        await ms._condense_call("hello", "sys", stage="t")
    assert calls["n"] == ms.CONDENSE_BREAKER_THRESHOLD


def test_f8_no_wallclock_no_kill():
    """[防回魂 · 用户令「根本不需要墙钟」「杀进程任务就完成了？」] 容错层里
    不许再出现判死钟/杀任务——慢不是故障，真断连 socket 会抛（反馈）。"""
    import inspect
    from src.storage import memory_store as ms
    src = open(ms.__file__, encoding="utf-8").read()
    assert "CONDENSE_SILENCE_DEATH" not in src, "静默判死钟不得回魂"
    assert "call_task.cancel" not in src, "机械杀任务不得回魂"
    body = inspect.getsource(ms._guarded_model_call)
    assert ".cancel()" not in body
    assert "asyncio.wait" in body, "观测循环在场（纯状态汇报）"


async def test_f8_truncation_raises_signal(sandbox, monkeypatch):
    """[F8a] finish_reason=length ⇒ CondenseTruncated（不是解析失败）。"""
    from src.storage import memory_store as ms
    from src.api import model_service

    async def trunc_call(system_prompt, user_prompt, prior_messages=None, tools=None,
                         model_name=None, **_):
        return ({"content": "半截产出", "tool_calls": [], "finish_reason": "length",
                 "truncated_tool_call": None}, "m")

    monkeypatch.setattr(model_service, "call_main", trunc_call)
    with pytest.raises(ms.CondenseTruncated):
        await ms._condense_call("hello", "sys", stage="t")


async def test_f8_nonretryable_error_raises_immediately(sandbox, monkeypatch):
    """[F8] auth/配置类不进 30 次梯——直接上抛（网关已重试过）。"""
    import asyncio as _aio
    from src.storage import memory_store as ms
    from src.api import model_service
    calls = {"n": 0}

    async def auth_fail(system_prompt, user_prompt, **_):
        calls["n"] += 1
        raise RuntimeError("401 unauthorized: bad api key")

    monkeypatch.setattr(model_service, "call_main", auth_fail)
    with pytest.raises(RuntimeError, match="401"):
        await ms._condense_call("hello", "sys", stage="t")
    assert calls["n"] == 1, "非可重试类只调一次"


async def test_fold_step_cache_resume_pays_no_twice(sandbox, work, monkeypatch):
    """[续跑基建] s1/s2 落盘缓存：第二次跑同指纹归拢 ⇒ 零模型调用（不重付）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    entries = [{"anchor": {"sessionId": cid, "seq": i}, "gist": f"条目{i}",
                "status": "done", "kind": "fact", "level": 1, "role": "gist",
                "upper": [], "lower": [], "coveredSeqs": [i]}
               for i in range(4)]
    await memory_store.save_memory(wid, cid, {
        "card_id": cid, "mark": 5, "batch_count": 5, "last_fold_check": 0,
        "entries": entries})
    calls = {"n": 0}

    async def fake_fold(system, user, tools=None, **_):
        calls["n"] += 1
        if "只转填" in system:
            return {"tool_calls": [{"name": "apply_folds",
                                    "arguments": {"merges": []}}]}
        if "凝练合并" in system:
            return {"content": "细看材料：无可合并。"}
        return {"content": "看了看，结论：无合并"}

    monkeypatch.setattr(memory_store, "_fold_call", fake_fold)
    await memory_store._maybe_fold(wid, cid)
    first = calls["n"]
    assert first >= 1, "首跑要调模型"

    # 游标重开（模拟下一轮触发），链未变 ⇒ 指纹同 ⇒ s1 复用缓存、零调用
    st = memory_store.load_memory(wid, cid)
    st["last_fold_check"] = 0
    await memory_store.save_memory(wid, cid, st)
    await memory_store._maybe_fold(wid, cid)
    assert calls["n"] == first, "同指纹第二跑必须复用缓存、零模型调用"


# ---------------------------------------------------------------- F8a 透传（审计补口）

def test_f8a_truncation_propagates_through_all_stages():
    """[F8a · 审计] 截断信号必须从三段的宽 except 里**透传**出去——被吞的后果
    实测过：伪装成「解析失败」、L2 段截断后 mark 照推进。"""
    import asyncio as _aio
    from src.storage import condense_two_layer as tl

    async def _run(stage_marker):
        async def call(system, user):
            if stage_marker in system:
                raise tl.CondenseTruncated("产出被 max-tokens 截断")
            if "分块器" in system:
                return '{"groups":[{"end":5}]}'
            if "L1 层" in system:
                return '{"l1":[{"开始":"起","经过":"过","完成":"果","教训":""}]}'
            return '{"l2":[{"subtopics":[]}]}'
        await tl.run_two_layer(
            [{"seq": s, "content": f"内容{s}"} for s in range(6)], call, "s1")

    for marker, name in (("分块器", "s2a"), ("L1 层", "l1"), ("L2 层", "l2")):
        with pytest.raises(tl.CondenseTruncated):
            _aio.run(_run(marker))
        # 每一段都不许吞——到此没抛就是失败
    print("三段截断全部透传")


def test_f5_l2_prompt_has_three_constraints():
    """[F5 · 审计] L2 prompt 必须带人称硬要求／保真红线／史书读者契约——
    二轮审计：只钉标题太薄，钉条款本体。"""
    t = tl_L2()
    # 人称硬要求：第一人称自述 + 对方称用户 + 禁第三人称
    assert "第一人称" in t and "「用户」" in t and "第三人称" in t
    # 保真红线：不虚构/不臆测
    assert "不得补" in t and "不臆测" in t
    # 史书读者契约：无上下文读者 + 编号括注
    assert "没有任何上下文的未来读者" in t and "括注" in t


def tl_L2():
    from src.storage import condense_two_layer as tl
    return tl.L2_PROMPT


async def test_singleflight_release_knob(sandbox, work, monkeypatch):
    """[自愈旋钮 · 二轮审计] 持有者在途调用超阈值无帧 ⇒ **只放锁不杀任务**；
    默认关（0）＝纯观测。"""
    import asyncio as _aio
    from src.storage import memory_store as ms

    wid, cid = work["work_id"], work["work_card_id"]
    # 占住单飞＋伪造持有者账
    monkeypatch.setattr(ms, "_GLOBAL_FLYING", True)
    monkeypatch.setattr(ms, "_FLYING_HOLDER",
                        {"work": wid, "card": cid, "since": __import__("time").time() - 999})
    # 台账里塞一条挂死的在途调用（无帧 999s）
    from src.infra import observe as _obs
    _obs.call_begin("tr-stuck", caller="condense:batch1", work_id=wid,
                    card_id=cid, seat="m", model="m")
    _obs._INFLIGHT["tr-stuck"]["last_chunk"] = __import__("time").time() - 999
    _obs._INFLIGHT["tr-stuck"]["started"] = __import__("time").time() - 999

    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0, "entries": []})
    # 空卡过不了触发线 ⇒ 钳 K=0 强制走到单飞分支
    monkeypatch.setattr(ms, "trigger_k_tokens", lambda: 0)
    events = []
    monkeypatch.setattr("src.api.event_stream.emit",
                        lambda w, c, t, d=None: events.append((w, c, t, d)) or {"seq": 1})
    # [处置 · 默认 900s] 挂死 999s > 900 ⇒ 放锁 + 报错给用户
    await ms.condense_turn_end(wid, cid)
    assert ms._GLOBAL_FLYING is False, "超阈值 ⇒ 只放锁"
    assert "tr-stuck" in _obs._INFLIGHT, "挂住的任务本体不被杀"
    assert any(t == "error" and "挂死" in str(d.get("error")) for _w, _c, t, d in events),         "挂死处置必须报错给用户（事件面）"
    _obs.call_end("tr-stuck")

    # 阈值调 0＝关（纯观测）：不放锁
    ms._GLOBAL_FLYING = True
    ms._FLYING_HOLDER.update({"work": wid, "card": cid, "since": __import__("time").time()})
    _obs.call_begin("tr-stuck2", caller="condense:batch1", work_id=wid,
                    card_id=cid, seat="m", model="m")
    _obs._INFLIGHT["tr-stuck2"]["last_chunk"] = __import__("time").time() - 999
    _obs._INFLIGHT["tr-stuck2"]["started"] = __import__("time").time() - 999
    monkeypatch.setenv("CONDENSE_FLYING_RELEASE_SEC", "0")
    await ms.condense_turn_end(wid, cid)
    assert ms._GLOBAL_FLYING is True, "阈值 0 ⇒ 纯观测不放锁"
    _obs.call_end("tr-stuck2")


async def test_fatal_model_error_reports_to_user(sandbox, work, monkeypatch):
    """[处置 · 用户令「模型失效、key 失效等不可能再运转下去的情况，报错给用户」]
    401 类错误 ⇒ 事件面出现用户可见的 error（不再吞在日志里）。"""
    from src.api import event_stream
    wid, cid = work["work_id"], work["work_card_id"]
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0, "entries": []})
    window = [{"seq": 0, "actor": "human", "content": "讨论内容", "ts": "t"}]

    async def fatal_call(user_prompt, system="", *a, **k):
        raise RuntimeError("401 unauthorized: api key 已失效")

    monkeypatch.setattr(memory_store, "_condense_call", fatal_call)
    events = []
    monkeypatch.setattr(event_stream, "emit",
                        lambda w, c, t, d=None: events.append((w, c, t, d)) or {"seq": 1})
    ok = await memory_store._condense_one_batch(wid, cid, window, 0)
    assert ok is False
    errs = [d for _w, _c, t, d in events if t == "error"]
    assert errs and "无法继续使用" in str(errs[0].get("error")), "失效类必须报错给用户"

    # 断连类：不报「无法继续」；管线级失败在触发面报「已记账重试」
    # （run_two_layer 会把断连异常吃进同批重试 ⇒ ok=False，批级不抛）
    events.clear()

    async def conn_call(user_prompt, system="", *a, **k):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(memory_store, "_condense_call", conn_call)
    ok = await memory_store._condense_one_batch(wid, cid, window, 0)
    assert ok is False and not [d for _w, _c, t, d in events if t == "error"],         "批级断连失败不打扰用户（留给重试）"

    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)

    async def pipeline_boom(*a, **k):
        raise RuntimeError("connection reset by peer")
    monkeypatch.setattr(memory_store, "_run_condense_pipeline", pipeline_boom)
    await memory_store.condense_turn_end(wid, cid)
    errs2 = [d for _w, _c, t, d in events if t == "error"]
    assert errs2 and "自动重试" in str(errs2[0].get("error")), "断连类＝记账重试口径"



async def test_oversized_block_chunk_condensed(work, monkeypatch):
    """[超大单块处置 · 二版] 巨块**分块凝**：切段各走三段管线，锚点取 2e9 合成段、
    coveredSeqs 回填原块号、水位越过、不变量零违例——不再有"上块牌子不凝"的出口。"""
    from _condense_stub import three_stage_stub
    wid, cid = work["work_id"], work["work_card_id"]
    giant = "巨块内容第×段。" * (memory_store.window_budget_tokens() * 2)   # 远超窗口
    window = [{"seq": 1, "actor": "assistant", "content": giant, "ts": "t"}]
    calls = []
    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="巨块段结论", record=calls))
    ok = await memory_store._condense_one_batch(wid, cid, window, 0)
    assert ok is True and calls, "分块凝必须真调模型"
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 2, "水位越过巨块"
    chunk_nodes = [e for e in st["entries"]
                   if int((e.get("anchor") or {}).get("seq") or 0) >= 2_000_000_000]
    assert chunk_nodes, "各段产出独立 L1（合成锚点段）"
    assert all(e.get("coveredSeqs") == [1] for e in chunk_nodes),         "coveredSeqs 统一记原块号"
    from src.storage import memory_domain as md
    assert md.check_invariants(memory_domain.chain_from_entries(st["entries"], cid)) == []
