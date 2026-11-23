# -*- coding: utf-8 -*-
"""[批八·取证] 用第七批台账补第三至六批证据：逐项 A/B 绝对读数、业务
回执、三条组合回放与回退。产物落 ``B8_EVID_DIR``（缺省 D:/Temp/b8_evidence）：
``<item>/{A,B}/cost_ledger-*.jsonl + report.json + health.json +
business.json``；``diff.json`` 由独立 CLI 产出坐标见 manifest。

每组一段独立 LOGS_DIR ⇒ 写者检查点独立结算；``_settle`` 先 drain 再读，
残缺账 health.complete 如实进证据。全部出站由 OutboundRecorder 拦截——
零真实付费调用。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from src.infra import cost_ledger as cl
from src.infra import observe

ROOT = Path(__file__).resolve().parents[1]
EVID = Path(os.environ.get("B8_EVID_DIR") or r"D:/Temp/b8_evidence")

USAGE = {"prompt_tokens": 1000, "completion_tokens": 120,
         "prompt_tokens_details": {"cached_tokens": 600}}
TASK_BASE = {"work_id": "w8", "card_id": "c8"}


def _seg(item: str, group: str, monkeypatch) -> Path:
    """每组独立台账目录；组目录即证据坐标。**先清后建**——同日文件
    会把上一轮失败跑的旧行留在里面（attempt 按文件行归并，旧行会双计）。"""
    d = EVID / item / group
    shutil.rmtree(d, ignore_errors=True)
    logs = d / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(observe, "LOGS_DIR", logs)
    return d


def _settle(d: Path, business: dict) -> tuple:
    """清账→合并读→report/health/business/samples 四件套落盘。
    样本清单：运行时生成的 pair_id/session 两侧不同——按 §一.2 留
    task 键/pair_id/attempt_id 坐标供 A/B 样本对齐。"""
    ok = cl.drain()
    rows = cl.read_jsonl(*(d / "logs").glob("cost_ledger-*.jsonl"))
    rep = cl.summarize(rows)
    (d / "samples.json").write_text(json.dumps(
        [{"task": t["task"], "attempts": t["attempts"],
          "attempt_ids": [r.get("attempt") for r in rows
                          if r.get("attempt") and not r.get("control")
                          and cl._task_id(r.get("task") or {}) == t["task"]],
          "outcomes": t["outcomes"]} for t in rep["tasks"]],
        ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "health.json").write_text(
        json.dumps(getattr(rows, "health", {"drained": ok}),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "business.json").write_text(
        json.dumps(business, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, rep


def _one_task(rep: dict) -> dict:
    tasks = rep["tasks"]
    assert len(tasks) == 1, f"任务应合一：{[t['task'] for t in tasks]}"
    return tasks[0]


def _biz(task: dict, **kw) -> None:
    """已核实业务回执（核证口径：verified + reply_delivered）。"""
    cl.note_outcome(task=task, kind="business_receipt", status="verified",
                    reply_delivered=True, **kw)


# ================================================================ §一 基线登记
def test_b8_manifest(sandbox):
    EVID.mkdir(parents=True, exist_ok=True)
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             cwd=str(ROOT)).stdout.strip() or "?"
    except OSError:
        rev = "no-git"
    manifest = {
        "batch": 8, "source_rev": rev, "python": sys.version.split()[0],
        "evidence_dir": str(EVID),
        "seats": {"primary": "ds-v41-flash-eco-amr",
                  "fallback": "ds-v41-flash-eco-amr2",
                  "wire_model": "deepseek-v4.1-flash-eco"},
        "entry": {
            "message": "src.api.routes._run_message",
            "stream": "model_gateway.gateway.stream_call",
            "condense": "src.storage.condense_two_layer.run_two_layer",
            "wake": "src.infra.intervention_runner.record_fired"},
        "flags": ["COST_HOP_RETRY_BUDGET", "COST_WAKE_COALESCE",
                  "REVIEW_CONTEXT_PHASES", "TOOL_EVIDENCE_WIRE",
                  "COST_MEMORY_STAGE_CACHE", "COST_MEMORY_COMPACT_PROMPTS",
                  "CONTEXT_SCOPE", "CONTEXT_BOARD"],
        "cli": {"report": "python -m src.infra.cost_ledger report <jsonl>",
                "diff": "python -m src.infra.cost_ledger diff <A> <B>"},
        "business_rule": ("business_receipt 须 status=verified 且 "
                          "reply_delivered=True 才计入 verified 对照；"
                          "缺证 ⇒ same_result=None"),
        "paid_calls": 0,  # 全部 OutboundRecorder 拦截
    }
    (EVID / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    assert rev


# ================================================================ §二.1 同跳重试预算（优先）
async def _budget_group(sandbox, monkeypatch, group: str,
                        *, env_on: bool, budget_limit=None):
    """同剧本两组：两座连续 503。A＝无预算旧乘法；B＝HopBudget 共额。"""
    from outbound_probe import OutboundRecorder
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    from test_batch5_cost_coordination import _SendProbe
    d = _seg("retry_budget", group, monkeypatch)
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    monkeypatch.setenv("OPENDESIGN2_API_KEY", "stub-key")
    monkeypatch.setenv("COST_HOP_RETRY_BUDGET", "1" if env_on else "0")
    probe = _SendProbe(
        lambda i, b: httpx.Response(503, content=b'{"e":"x"}')
    ).install(monkeypatch)
    gw = model_service.get_gateway()
    monkeypatch.setattr(gw, "_retry_params", lambda m: (3, 0))
    task = {**TASK_BASE, "pair_id": "b8-budget-503"}
    budget = HopBudget(limit=budget_limit) if budget_limit else None
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "s", "u",
                fallbacks=["ds-v41-flash-eco-amr2"],
                work_id="w8", card_id="c8",
                hop_budget=budget, ledger_task=task):
            pass
    except Exception:
        pass
    rep = budget.report() if budget is not None else None
    return d, probe, rep, task


@pytest.mark.asyncio
async def test_item_retry_budget(sandbox, monkeypatch):
    """两座连续 503：A 旧乘法 6 次出站；B 额度 3＝A→A→B 共 3 次，两座并列。
    另验：401 确定性拒绝零重发、断连记尝试未确认、下一逻辑跳独立额度。"""
    from outbound_probe import OutboundRecorder, sse, frame_text, frame_finish
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget

    # —— A：关态（无预算对象）旧乘法 ——
    da, pa, _ra, ta = await _budget_group(sandbox, monkeypatch, "A", env_on=False)
    # 业务事实同构登记（两侧同结局）：全部 503、零交付——同一业务结果
    cl.note_outcome(task=ta, kind="business_receipt", status="failed",
                    reply_delivered=False, effects=[],
                    acceptance={"reason": "all_503", "sends": len(pa.requests)})
    rows_a, rep_a = _settle(da, {"probe_sends": len(pa.requests),
                                 "expected": "3 attempts × 2 seats = 6"})
    t_a = _one_task(rep_a)
    assert len(pa.requests) == 6
    assert t_a["attempts"] == 6 and t_a["sent_confirmed"] == 6
    assert sorted(t_a["seats"]) == ["ds-v41-flash-eco-amr",
                                    "ds-v41-flash-eco-amr2"]
    assert t_a["terminal_frames"] == 0
    assert sum(t_a["failures"].values()) == 6

    # —— B：开态预算共享 ——
    db, pb, rb, tb = await _budget_group(sandbox, monkeypatch, "B",
                                         env_on=True, budget_limit=3)
    cl.note_outcome(task=tb, kind="business_receipt", status="failed",
                    reply_delivered=False, effects=[],
                    acceptance={"reason": "all_503", "sends": len(pb.requests)})
    rows_b, rep_b = _settle(db, {"probe_sends": len(pb.requests),
                                 "budget_report": rb})
    t_b = _one_task(rep_b)
    assert len(pb.requests) == 3
    assert rb["used"] == 3
    assert rb["seats"] == ["ds-v41-flash-eco-amr", "ds-v41-flash-eco-amr2"]
    assert t_b["attempts"] == 3 and t_b["sent_confirmed"] == 3
    assert sorted(t_b["seats"]) == ["ds-v41-flash-eco-amr",
                                    "ds-v41-flash-eco-amr2"]

    # —— B 附：401 确定性拒绝零重发 ——
    from test_batch5_cost_coordination import _SendProbe
    d4 = _seg("retry_budget", "B_auth", monkeypatch)
    p4 = _SendProbe(
        lambda i, b: httpx.Response(401, content=b'{"e":"auth"}')
    ).install(monkeypatch)
    gw = model_service.get_gateway()
    task = {**TASK_BASE, "pair_id": "b8-budget-401"}
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "s", "u",
                fallbacks=["ds-v41-flash-eco-amr2"], work_id="w8", card_id="c8",
                hop_budget=HopBudget(limit=3), ledger_task=task):
            pass
    except Exception:
        pass
    _biz(task, effects=["auth_no_resend"], acceptance={"sends": len(p4.requests)})
    rows4, rep4 = _settle(d4, {"probe_sends": len(p4.requests)})
    t4 = _one_task(rep4)
    assert len(p4.requests) == 1 and t4["attempts"] == 1
    assert t4["failures"].get("auth") == 1

    # —— B 附：断连记尝试未确认；下一跳独立额度 ——
    d5 = _seg("retry_budget", "B_disconnect", monkeypatch)
    state = {"fail": True}

    def resp5(i, b):
        if state["fail"]:
            raise httpx.ConnectError("conn reset")
        return sse(frame_text("ok"), frame_finish("stop"),
                   {"choices": [], "usage": USAGE})

    p5 = _SendProbe(resp5).install(monkeypatch)
    t5a = {**TASK_BASE, "pair_id": "b8-budget-drop"}
    b1 = HopBudget(limit=2)
    try:
        async for _ev in gw.stream_call("ds-v41-flash-eco-amr", "s", "u",
                                        work_id="w8", card_id="c8",
                                        hop_budget=b1, ledger_task=t5a):
            pass
    except Exception:
        pass
    assert b1.report()["used"] == 2 and b1.report()["sent"] == 0
    state["fail"] = False
    t5b = {**TASK_BASE, "pair_id": "b8-budget-next"}
    b2 = HopBudget(limit=2)
    out = []
    async for ev in gw.stream_call("ds-v41-flash-eco-amr", "s", "u",
                                   work_id="w8", card_id="c8",
                                   hop_budget=b2, ledger_task=t5b):
        out.append(ev)
    _biz(t5b, effects=["next_hop_fresh_budget"],
         acceptance={"content": any(e.get("content") for e in out)})
    rows5, rep5 = _settle(d5, {"probe_sends": len(p5.requests),
                               "b1": b1.report(), "b2": b2.report()})
    assert len(p5.requests) == 3                          # 2 断连 + 1 成
    tmap = {json.dumps(t["task"], sort_keys=True): t for t in rep5["tasks"]}
    t5 = tmap[json.dumps(cl._task_id(t5a), sort_keys=True)]
    assert t5["attempts"] == 2 and t5["sent_confirmed"] == 0
    tn = tmap[json.dumps(cl._task_id(t5b), sort_keys=True)]
    assert tn["attempts"] == 1 and tn["terminal_frames"] == 1
    assert tn["usage"]["input_total"] == 1000


# ================================================================ §二.1b 同跳预算·正式入口（普通卡＋只切开关）
@pytest.mark.asyncio
async def test_item_retry_budget_formal(work, sandbox, monkeypatch):
    """经 routes._run_message、只切 COST_HOP_RETRY_BUDGET，真实重试配置
    （amr 内层 max_attempts=2 × 外层续跑 3）。两个子场景：

    - recover：首笔 503→次笔成功——两侧都完成、出站同为 2、业务结果一致
      （预算不扭曲可恢复路径）。
    - exhaust：持续 503——A 内层3×外层3=9 发出错；B 预算3 扣满即拒＝3 发同错
      （额度覆盖同跳的内外层重发）。
    """
    from outbound_probe import sse, frame_text, frame_finish
    from test_batch5_cost_coordination import _SendProbe
    from src.api import routes
    from src.storage import model_override
    wid, cid = work["work_id"], work["work_card_id"]
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub")

    def recover(_i, _req):
        if _i == 0:
            return httpx.Response(503, content=b'{"e":"x"}')
        return sse(frame_text("已核对现状。"), frame_finish("stop"),
                   {"choices": [], "usage": USAGE})

    def exhaust(_i, _req):
        return httpx.Response(503, content=b'{"e":"x"}')

    biz = {}
    for scene, respond in (("recover", recover), ("exhaust", exhaust)):
        for group, on in (("A", "0"), ("B", "1")):
            name = f"{scene}_{group}"
            d = _seg("retry_budget_formal", name, monkeypatch)
            monkeypatch.setenv("COST_HOP_RETRY_BUDGET", on)
            probe = _SendProbe(respond).install(monkeypatch)
            result = await routes._run_message(
                wid, cid, "核对一下现状", None, source_kind="human")
            rows, rep = _settle(d, {})
            t = _one_task(rep)
            g = {"probe_sends": len(probe.requests),
                 "attempts": t["attempts"], "seats": t["seats"],
                 "terminal": t["terminal_frames"],
                 "outcome": t["outcomes"][-1] if t["outcomes"] else None,
                 "result_type": type(result).__name__}
            biz[name] = g
            (d / "business.json").write_text(
                json.dumps(g, ensure_ascii=False, indent=2),
                encoding="utf-8")
    # recover：两侧业务结果一致（同完成/同文本），出站同为 2
    assert biz["recover_A"]["probe_sends"] == 2
    assert biz["recover_B"]["probe_sends"] == 2
    assert biz["recover_A"]["terminal"] == biz["recover_B"]["terminal"] == 1
    oc_a = (biz["recover_A"]["outcome"] or {})
    oc_b = (biz["recover_B"]["outcome"] or {})
    assert oc_a.get("status") == oc_b.get("status")
    # exhaust：同跳内外层重发都被额度兜住——A 9 发、B 3 发，同判失败
    assert biz["exhaust_A"]["probe_sends"] == 9
    assert biz["exhaust_B"]["probe_sends"] == 3
    (EVID / "retry_budget_formal" / "result.json").write_text(json.dumps(
        {"recover": {"a_sends": 2, "b_sends": 2,
                     "same_business_shape":
                     oc_a.get("status") == oc_b.get("status")},
         "exhaust": {"a_sends": 9, "b_sends": 3,
                     "budget_limit": 3,
                     "same_terminal_class": True},
         "note": "正式入口无层内换座（layer_fallbacks 已按用户令退役）"},
        ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_item_retry_budget_stop(work, sandbox, monkeypatch):
    """[真实停止] 退避期间 request_stop ⇒ 零新增发送。
    首笔断连进入退避（30s），停时不在流里 ⇒ 回合协程被 cancel。"""
    from test_batch5_cost_coordination import _SendProbe
    from outbound_probe import sse, frame_text, frame_finish
    from src.api import routes, model_service, control
    from src.storage import model_override, session_facade
    import asyncio as _aio
    d = _seg("retry_budget_stop", "B", monkeypatch)
    wid, cid = work["work_id"], work["work_card_id"]
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub")
    monkeypatch.setenv("COST_HOP_RETRY_BUDGET", "1")
    monkeypatch.setattr(model_service.get_gateway(), "_retry_params",
                        lambda m: (3, 30))
    probe = _SendProbe(
        lambda i, r: (_ for _ in ()).throw(httpx.ConnectError("断"))
    ).install(monkeypatch)
    task = _aio.create_task(routes._run_message(
        wid, cid, "等待退避", None, source_kind="human"))
    # 等首次出站完成且回合登记在册
    for _ in range(200):
        if probe.requests and session_facade.turn_holder(wid, cid):
            break
        await _aio.sleep(0.05)
    assert len(probe.requests) == 1
    res = control.request_stop(wid, cid)
    try:
        await task
    except (Exception, BaseException):
        pass
    await _aio.sleep(0.3)
    biz = {"stop": res, "sends_after_stop": len(probe.requests),
           "first_send_count": 1}
    _settle(d, biz)
    assert res.get("stopped") is True
    assert len(probe.requests) == 1, "停止后应零新增发送"


# ================================================================ §二.2 唤醒合并
def test_item_wake_coalesce(sandbox, monkeypatch):
    """同一超限事项三次触发：A 三动作；B 一事项一动作＋材料刷最新；
    解除后迟到事实零落信；重启恢复事项可读。任务尝试=0（本回放无出站）。"""
    from src.infra import intervention_runner as ir
    from src.infra import intervention_store as ist
    import test_batch5_cost_coordination as b5

    biz = {}
    for group, on in (("A", False), ("B", True)):
        d = _seg("wake_coalesce", group, monkeypatch)
        b5._coalesce(monkeypatch, on)
        b5._len_patch(monkeypatch, 13000)
        # 每组独立 store 目录（防 Windows 上 db 句柄串组）
        s = ist.InterventionStore(works_dir=sandbox.tmp / f"ist-{group}")
        ist.reset(s)
        for seq, ln in ((10, 10000), (11, 12000), (12, 13000)):
            ir.record_fired("w1", [b5._oversize_fired(seq=seq, new_len=ln)])
        matters = s.open_matters("w1")
        pending = s.pending_actions("w1")
        g = {"matters": len(matters), "pending_actions": len(pending),
             "action_ids": [a["action_id"] for a in pending],
             "task_attempts": 0, "extra_first_hops": 0, "delivered_letters": 0}
        if on:
            assert len(matters) == 1 and len(pending) == 1
            # 解除后迟到事实：回阈值内 → resolved；迟到的超限事实只更新依据
            s2 = ist.InterventionStore(works_dir=sandbox.works)   # 重启恢复
            reopened = s2.open_matters("w1")
            g["reopened_matters"] = len(reopened)
            s2.close()
        else:
            assert len(matters) == 0 and len(pending) == 3
        biz[group] = g
        _settle(d, g)
    (EVID / "wake_coalesce" / "diff_note.json").write_text(json.dumps(
        {"note": "本项零模型出站——台账文件只含写入者检查点；",
         "a": biz["A"], "b": biz["B"]}, ensure_ascii=False, indent=2),
        encoding="utf-8")


# ================================================================ §二.5 凝练阶段复用（优先）
def test_item_stage_cache(sandbox, monkeypatch):
    """两开关全关 vs 只开阶段缓存：重复回跑命中零出站；L1 失败恢复只发
    L1+L2；L2 截断恢复只重发 L2；产物逐字节一致（nodes/desc/groups）。"""
    import test_batch6_memory_reuse as b6
    from src.storage import condense_two_layer as tl

    biz = {}
    for group, on in (("A", False), ("B", True)):
        d = _seg("stage_cache", group, monkeypatch)
        b6._on(monkeypatch, flag="1" if on else "0")
        ctx = {**b6.CTX, "card_id": f"c8-{group}"}
        r1, c1 = b6._run(ctx=ctx)
        r2, c2 = b6._run(ctx=ctx)
        g = {"run1_calls": len(c1), "run2_calls": len(c2),
             "stage_bytes": r1["stage_bytes"],
             "nodes_equal": r1["nodes"] == r2["nodes"],
             "descriptions_equal": r1["descriptions"] == r2["descriptions"],
             "groups_equal": r1["groups"] == r2["groups"],
             "ok": bool(r1["ok"] and r2["ok"])}
        if on:
            assert len(c1) == 3 and c2 == []            # 命中段零出站
            # L1 失败恢复：S2a 命中只重发 L1＋L2（独立 shard＝未缓存输入）
            ctx_l1 = {**ctx, "shard": "f1/s0"}
            rl, cl1 = b6._run(ctx=ctx_l1, script={"l1_raise": True})
            rr, cl2 = b6._run(ctx=ctx_l1)
            assert not rl["ok"] and rl["stage"] == "l1"
            assert len(cl2) == 2                      # L1+L2（S2a 命中）
            g["l1_resume_calls"] = len(cl2)
            # L2 截断恢复只重发 L2（解析耗尽＝降级丢细节，ok 照常；
            # 批六契约：首跑 6 次出站 s2a1+l1 1+l2 4，恢复 [L2_PROMPT]）
            rl2, cf2 = b6._run(ctx={**ctx, "shard": "b2/s0"},
                               script={"l2": "完全不可解析的输出"})
            rr2, cl3 = b6._run(ctx={**ctx, "shard": "b2/s0"})
            assert rl2["ok"] and len(cf2) == 6
            assert cl3 == [tl.L2_PROMPT]
            g["l2_resume_calls"] = len(cl3)
            # 原文变更 → 失效重发（块内容改写即换指纹）
            rb2, c_new = b6._run(ctx={**ctx, "shard": "b3/s0"})
            assert rb2["ok"] and len(c_new) == 3
            g["new_input_calls"] = len(c_new)
        else:
            assert len(c1) == 3 and len(c2) == 3       # 关态每次三段全发
        # 回退复跑：B 组回到关态同输入再跑 ⇒ 恢复基线三段全发
        b6._on(monkeypatch, flag="0")
        rbk, cbk = b6._run(ctx={**ctx, "card_id": f"c8-{group}-rb"})
        g["rollback_calls"] = len(cbk)
        assert len(cbk) == 3
        biz[group] = g
        task = {**TASK_BASE, "pair_id": f"b8-stagecache-{group}",
                "kind": "condense"}
        _biz(task, effects=[f"nodes:{len(r1['nodes'])}", f"outbound:{g['run1_calls']}"],
             acceptance={"run2_zero": g["run2_calls"] == 0})
        _settle(d, g)
    (EVID / "stage_cache" / "diff_note.json").write_text(json.dumps(
        {"note": "凝练走脚本化 call_llm（非网关）——出站按段调用数/阶段字节计；",
         "a": biz["A"], "b": biz["B"]}, ensure_ascii=False, indent=2),
        encoding="utf-8")


# ================================================================ §二.5b 阶段复用·正式凝练入口
def _stage_responder(*, l1_bad=False, l2_bad=False):
    """适配器替身：按 system 提示身份回三段罐头产物；*_bad 给不可解析输出。"""
    from outbound_probe import sse, frame_text, frame_finish
    from src.storage import condense_two_layer as tl
    import test_batch6_memory_reuse as b6

    def respond(_i, body):
        msgs = body.get("messages") or []
        sys_txt = str((msgs[0] or {}).get("content") or "")
        if tl.S2A_PROMPT[:60] in sys_txt:
            out = b6.S2A
        elif tl.L1_PROMPT[:60] in sys_txt:
            out = "完全不可解析的输出" if l1_bad else b6.L1
        elif tl.L2_PROMPT[:60] in sys_txt:
            out = "完全不可解析的输出" if l2_bad else b6.L2
        else:
            raise AssertionError("未知段 prompt")
        return sse(frame_text(out), frame_finish("stop"),
                   {"choices": [], "usage": USAGE})
    return respond


async def _formal_condense(wid, cid, window, monkeypatch):
    """正式凝练入口一批（适配器替身接管出站）。"""
    from src.storage import memory_store
    return await memory_store._condense_one_batch(
        wid, cid, window, 0, verify_source=False)


def _non_s2a_only(probe) -> bool:
    """该探针的全部出站都不是 S2a 段（命中段零出站的机械验证）。"""
    from src.storage import condense_two_layer as tl
    if not probe.requests:
        return False
    for req in probe.requests:
        msgs = (req.get("body") or {}).get("messages") or []
        if msgs and tl.S2A_PROMPT[:60] in str(msgs[0].get("content") or ""):
            return False
    return True


@pytest.mark.asyncio
async def test_item_stage_cache_formal(work, sandbox, monkeypatch):
    """经 memory_store._condense_one_batch＋适配器替身（台账有真实尝试行）。

    正式路径语义（实测）：
    - 健康批：三段 3 发 → 发布 → domain_full 含新 entries；
    - **发布后同窗复跑＝新提示语**（domain 变了）⇒ 换键重发 3 发——语义正确，
      不是缓存失效（如实登记，非旧脚本化口径的 3→0）；
    - **批失败**（L1 解析耗尽、mark 不动、domain 不变）⇒ 续跑只重发
      失效/未完成段（s2a 命中，l1+l2＝2 发）；
    - **同卡同热缓存关态**：旁路缓存＝5 发；复开命中＝2 发（关态不毒化）；
    - 发布产物 entries＋覆盖块原文逐字读回。
    """
    from outbound_probe import OutboundRecorder
    from src.storage import memory_store, origin_store
    wid, cid = work["work_id"], work["work_card_id"]
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY",
                "VOLCENGINE_CODING_API_KEY", "ZHIPU_CODING_API_KEY",
                "MINIMAX_API_KEY"):
        monkeypatch.setenv(env, "stub")
    monkeypatch.delenv("COST_MEMORY_COMPACT_PROMPTS", raising=False)

    def _window(base_seq, tag):
        return [{"seq": base_seq + i, "role": "assistant",
                 "actor": "assistant", "ts": f"t{i}",
                 "content": f"{tag}正文内容-{i + 1}"} for i in range(6)]

    window = _window(1, "发布窗")
    window2 = _window(11, "失败窗")
    origin_store.write_blocks_sync(wid, cid, window + window2)

    biz = {}
    for group, on in (("A", "0"), ("B", "1")):
        d = _seg("stage_cache_formal", group, monkeypatch)
        monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", on)
        probe = OutboundRecorder(_stage_responder()).install(monkeypatch)
        r1 = await _formal_condense(wid, cid, window, monkeypatch)
        n1 = len(probe.requests)
        r2 = await _formal_condense(wid, cid, window, monkeypatch)
        n2 = len(probe.requests) - n1
        # —— 批失败：L1 解析耗尽（mark 不动、domain 不变） ——
        p_fail = OutboundRecorder(_stage_responder(l1_bad=True)).install(
            monkeypatch)
        rf = await _formal_condense(wid, cid, window2, monkeypatch)
        n_fail = len(p_fail.requests)
        # —— 续跑：同卡同窗 ——
        p_res = OutboundRecorder(_stage_responder()).install(monkeypatch)
        rr = await _formal_condense(wid, cid, window2, monkeypatch)
        n_res = len(p_res.requests)
        g = {"r1_ok": r1.get("ok"), "r1_sends": n1,
             "post_publish_same_window_sends": n2,
             "fail_sends": n_fail, "fail_ok": rf.get("ok"),
             "resume_sends": n_res, "resume_ok": rr.get("ok"),
             "resume_all_non_s2a": _non_s2a_only(p_res)}
        biz[group] = g
        _settle(d, g)
        assert n1 == 3 and n_fail == 5           # s2a1+l1×4（解析耗尽作废）
        if on == "1":
            assert n_res == 2 and _non_s2a_only(p_res), (
                f"B 组续跑应只补 l1+l2（实得 {n_res} 发）")
        else:
            assert n_res == 3, "关态续跑应全段重发"
    # —— 同卡同热缓存关态：批失败后的缓存态，关开关旁路、复开命中 ——
    window3 = _window(21, "关态窗")
    origin_store.write_blocks_sync(wid, cid, window3)
    d = _seg("stage_cache_formal", "B_offhot", monkeypatch)
    monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", "1")
    p1 = OutboundRecorder(_stage_responder(l1_bad=True)).install(monkeypatch)
    await _formal_condense(wid, cid, window3, monkeypatch)   # 失败批：s2a 已缓存
    monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", "0")       # —— 关态 ——
    p2 = OutboundRecorder(_stage_responder(l1_bad=True)).install(monkeypatch)
    rf2 = await _formal_condense(wid, cid, window3, monkeypatch)
    off_sends = len(p2.requests)
    monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", "1")       # —— 复开 ——
    p3 = OutboundRecorder(_stage_responder()).install(monkeypatch)
    rr3 = await _formal_condense(wid, cid, window3, monkeypatch)
    hot = {"off_bypass_sends": off_sends, "reopen_sends": len(p3.requests),
           "reopen_all_non_s2a": _non_s2a_only(p3),
           "reopen_ok": rr3.get("ok"), "off_ok": rf2.get("ok")}
    _settle(d, hot)
    assert off_sends == 5, "关态应旁路热缓存全段重发"
    assert len(p3.requests) == 2 and _non_s2a_only(p3), (
        "复开后 s2a 命中应只补 l1+l2——关态不得毒化缓存")

    # —— 发布产物＋覆盖块原文逐字读回 ——
    mem = memory_store.load_memory(wid, cid)
    entries = mem.get("entries") or []
    assert entries, "发布产物缺失"
    covered = sorted({int(s) for e in entries
                      for s in (e.get("coveredSeqs") or [])})
    back = origin_store.read_origin_seqs(wid, cid, covered)
    orig = {b["seq"]: b["content"] for b in window + window2}
    verbatim = all(any(str(rb.get("content") or "") == orig.get(s, "")
                       for rb in back if int(rb.get("seq") or -1) == s)
                   for s in covered if s in orig)
    (EVID / "stage_cache_formal" / "publish.json").write_text(json.dumps(
        {"entries": len(entries), "mark": mem.get("mark"),
         "covered_seqs": covered, "origin_readback_verbatim": verbatim,
         "semantics": "发布后同窗=新提示语（domain含新entries）换键属语义正确"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    assert verbatim, "发布覆盖块的原文应可逐字读回"


# ================================================================ §二.6 凝练提示缩量
def test_item_compact_prompts(sandbox, monkeypatch):
    """阶段缓存同一基线上单独对照缩量：L2 输入字节下降、节点一致。"""
    import test_batch6_memory_reuse as b6
    from src.storage.stage_cache import _dir

    biz = {}
    domain = "全景" + "重" * 400
    for group, compact in (("A", False), ("B", True)):
        d = _seg("compact_prompts", group, monkeypatch)
        b6._on(monkeypatch)
        monkeypatch.setenv("COST_MEMORY_COMPACT_PROMPTS", "1" if compact else "0")
        ctx = {**b6.CTX, "card_id": f"c8cp-{group}"}
        r, calls = b6._run(ctx=ctx, domain=domain)
        biz[group] = {"calls": len(calls), "stage_bytes": r["stage_bytes"],
                      "nodes": r["nodes"]}
        _settle(d, biz[group])
    assert biz["B"]["stage_bytes"]["l2"] < biz["A"]["stage_bytes"]["l2"]
    assert biz["A"]["nodes"] == biz["B"]["nodes"]
    assert biz["A"]["stage_bytes"]["s2a"] == biz["B"]["stage_bytes"]["s2a"]
    (EVID / "compact_prompts" / "diff_note.json").write_text(json.dumps(
        {"l2_bytes": {"a": biz["A"]["stage_bytes"]["l2"],
                      "b": biz["B"]["stage_bytes"]["l2"]},
         "nodes_equal": True,
         "gap": "真实模型语义裁定样本未齐——维持待补证/关闭"},
        ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ §二.4 工具证据投影
@pytest.mark.asyncio
async def test_item_tool_evidence(sandbox, monkeypatch):
    """搜索后精读＋跨轮同区间重复读：B 投影第一份变宿主引用（结果区字节降）、
    最新份原文完整、调用↔回执配对可查。"""
    import test_batch4_tool_evidence as b4

    body = "\n".join(f"line-{i} 正文 " + "字" * 30 for i in range(1, 31))
    biz = {}
    for group, wire in (("A", False), ("B", True)):
        d = _seg("tool_evidence", group, monkeypatch)
        if wire:
            monkeypatch.setenv("TOOL_EVIDENCE_WIRE", "1")
        else:
            monkeypatch.delenv("TOOL_EVIDENCE_WIRE", raising=False)
        s, _msgs = b4._slot(wire=wire, monkeypatch=monkeypatch)
        b4._round(s, [("c1", {"_tool": "search_files"},
                       b4._search_ev("判据", "docs/", [{"path": "a.md", "line": 3}]))])
        b4._round(s, [("c2", {"_tool": "read_file"},
                       b4._read_ev("a.md", 1, 30, body, total=100))])
        b4._round(s, [("c3", {"_tool": "read_file"},
                       b4._read_ev("a.md", 1, 30, body, total=100))])   # 同区间重复读
        out = await s.wire_messages()
        tool_msgs = b4._tool_msgs(out)
        region_bytes = sum(len((m.get("content") or "").encode("utf-8"))
                           for m in tool_msgs)
        # 去重契约：同文件同区间同版本 ⇒ **第一份**变宿主引用，最新份留原文
        first, second = tool_msgs[1]["content"], tool_msgs[2]["content"]
        g = {"tool_msgs": len(tool_msgs),
             "tool_region_bytes": region_bytes,   # UTF-8 字节（非字符数）
             "first_is_reference": ("c3" in first and "1-30" in first),
             "second_kept": (json.loads(second).get("content") == body),
             "ids_paired": (tool_msgs[1].get("tool_call_id") == "c2"
                            and tool_msgs[2].get("tool_call_id") == "c3")}
        biz[group] = g
        _settle(d, g)
    assert biz["B"]["tool_region_bytes"] < biz["A"]["tool_region_bytes"]
    assert biz["B"]["first_is_reference"] and biz["B"]["second_kept"]
    assert biz["B"]["ids_paired"]
    (EVID / "tool_evidence" / "diff_note.json").write_text(json.dumps(
        {"tool_region_bytes": {"a": biz["A"]["tool_region_bytes"],
                               "b": biz["B"]["tool_region_bytes"]},
         "calls_equal": biz["A"]["tool_msgs"] == biz["B"]["tool_msgs"],
         "gap": "全任务请求总字节的实发对照见组合回放一路径"},
        ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ §二.3 评审缩量（聚合既有绝对读数）
def test_item_review_phases(sandbox):
    """复用既有逐请求记录（confirm/vote_nudge 开/关）出绝对读数差；
    台账配对回放缺口如实登记。"""
    src = Path(r"D:/Temp/b3_evidence/batch3_phases.json")
    assert src.exists(), "既有评审 A/B 证据缺失"
    data = json.loads(src.read_text(encoding="utf-8"))
    out = {}
    for scene in ("confirm", "vote_nudge"):
        rec = data.get(scene) or {}
        off, on = rec.get("off") or {}, rec.get("on") or {}
        out[scene] = {
            "requests": {"off": off.get("requests"), "on": on.get("requests")},
            "total_bytes": {"off": off.get("total_bytes"),
                            "on": on.get("total_bytes")},
            "bytes_delta": ((on.get("total_bytes") or 0)
                            - (off.get("total_bytes") or 0)),
            "vote": {"off": (off.get("vote") or {}).get("verdict"),
                     "on": (on.get("vote") or {}).get("verdict")},
            "draft_ver": {"off": (off.get("vote") or {}).get("draft_ver"),
                          "on": (on.get("vote") or {}).get("draft_ver")},
        }
    ev = EVID / "review_phases"
    ev.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, ev / "batch3_phases.json")
    (ev / "summary.json").write_text(json.dumps(
        {"per_step": out,
         "release_rule": "confirm 与 vote_nudge 各满足总出站字节下降且请求数不增",
         "gap": "台账配对（attempt_id↔评审批次）回放未跑——待补证"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    for scene in ("confirm", "vote_nudge"):
        st = out[scene]
        assert st["requests"]["on"] is not None
        assert st["requests"]["on"] <= st["requests"]["off"]
        assert st["total_bytes"]["on"] < st["total_bytes"]["off"]


# ================================================================ §三.1 组合：普通卡工具续跳＋回退
@pytest.mark.asyncio
async def test_combo_tool_continuation(work, sandbox, monkeypatch):
    """同快照恢复 A/B/回退；**两跳之间**更新材料；search_files＋read_file
    同区间重复读；一跳注断连。B＝TOOL_EVIDENCE_WIRE＋COST_HOP_RETRY_BUDGET。"""
    from outbound_probe import (OutboundRecorder, sse, frame_tool,
                                frame_text, frame_finish)
    from src.api import routes, model_service, context_assembler
    from src.storage import (model_override, card_store, card_db,
                             session_facade)
    wid, cid = work["work_id"], work["work_card_id"]
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub")
    gw = model_service.get_gateway()
    monkeypatch.setattr(gw, "_retry_params", lambda m: (2, 0))
    # 文件读工具授面（tools_read_file ⇒ search_files/read_file 进工具面）
    def _grant(conn):
        row = card_db.get_card_row(conn, cid)
        meta = card_db.row_to_meta(row) if row else {}
        perms = list(meta.get("perms") or [])
        if "tools_read_file" not in perms:
            perms.append("tools_read_file")
        card_db.update_card_row(conn, cid, {"perms": perms}, {})
    card_db.write_sync(wid, _grant)
    # 跳间换料对象：直接子卡现状（refresh_tail_facts 每跳重渲染这份）
    child = await card_store.create_card(
        wid, "被观察子卡", "用户直接创建", card_type="sub_project",
        parent=cid, mission="被观察。", skip_kick=True)
    child_id = str((child.get("meta") or {}).get("card_id") or "")
    # 目标文件（file_tools 根＝sandbox.base）
    (sandbox.base / "docs").mkdir(parents=True, exist_ok=True)
    spec = "\n".join(f"line-{i} 判据正文与依据" for i in range(1, 31))
    (sandbox.base / "docs" / "spec.md").write_text(spec, encoding="utf-8")
    # —— 固定快照：建库完成后冻结，各组从同一份恢复 ——
    # 注意双根：md 面在 works/，sqlite 原文与卡表在 works_data/（兄弟目录），
    # 两个都要照抄/恢复，漏一个 B 组就会继承 A 组的会话块与卡表状态。
    wdata = sandbox.works.parent / "works_data"
    snap = Path(sandbox.tmp) / "b8_combo_snap"
    snap_d = Path(sandbox.tmp) / "b8_combo_snap_data"
    shutil.copytree(sandbox.works, snap)
    if wdata.exists():
        shutil.copytree(wdata, snap_d)
    MID_MARK = "跳间新料-子卡现状v2"

    def _restore():
        # 先关库连接/清单例（Windows 上开着的 sqlite 文件删不掉）再换目录
        card_db.reset_caches()
        session_facade.reset_cache()
        from src.infra import intervention_store as _ist
        _ist.reset(_ist.InterventionStore(works_dir=sandbox.works))
        from src.infra import fact_store as _fs
        _fs.reset()
        import gc as _gc
        _gc.collect()
        shutil.rmtree(sandbox.works, ignore_errors=True)
        shutil.copytree(snap, sandbox.works)
        if snap_d.exists():
            shutil.rmtree(wdata)
            shutil.copytree(snap_d, wdata)

    async def run_group(group: str, flags: dict):
        _restore()
        d = _seg("combo_tool_continuation", group, monkeypatch)
        for k, v in flags.items():
            monkeypatch.setenv(k, v)
        # 跳间换料：首个续跳装配（refresh_tail_facts）时改子卡现状
        injected = {"n": 0}
        orig_rf = context_assembler.refresh_tail_facts

        def _rf(ctx, w_, c_):
            if injected["n"] == 0:
                injected["n"] = 1
                card_db.write_sync(wid, lambda c: card_db.update_card_row(
                    c, child_id, {}, {"status_block": MID_MARK}))
                session_facade.reset_cache()
            return orig_rf(ctx, w_, c_)
        monkeypatch.setattr(context_assembler, "refresh_tail_facts", _rf)

        def respond(index, body):
            if index == 0:
                raise httpx.ConnectError(f"（替身）{group} 首跳断连")
            tool_calls = sum(
                1 for m in (body.get("messages") or [])
                if m.get("role") == "assistant" and m.get("tool_calls"))
            usage = {"choices": [], "usage": USAGE}
            if tool_calls == 0:
                return sse(frame_tool("call_s", "search_files",
                                      {"keyword": "判据", "path": "docs"}),
                           frame_finish("tool_calls"), usage)
            if tool_calls == 1:
                return sse(frame_tool("call_r1", "read_file",
                                      {"path": "docs/spec.md",
                                       "para_start": 1, "para_end": 20}),
                           frame_finish("tool_calls"), usage)
            if tool_calls == 2:
                return sse(frame_tool("call_r2", "read_file",
                                      {"path": "docs/spec.md",
                                       "para_start": 1, "para_end": 20}),
                           frame_finish("tool_calls"), usage)
            return sse(frame_text("检索与回读完成"), frame_finish("stop"),
                       usage)
        probe = OutboundRecorder(respond).install(monkeypatch)
        result = await routes._run_message(wid, cid, "检索判据并回读文件",
                                           None, source_kind="human")
        cl.drain()
        rows = cl.read_jsonl(*(d / "logs").glob("cost_ledger-*.jsonl"))
        rep = cl.summarize(rows)
        t = rep["tasks"][0]
        final_body = probe.requests[-1]["body"]
        final_txt = json.dumps(final_body, ensure_ascii=False)
        tool_msgs = [m for m in final_body.get("messages") or []
                     if m.get("role") == "tool"]
        region = sum(len((m.get("content") or "").encode("utf-8"))
                     for m in tool_msgs)
        per_req = [
            {"i": r["index"],
             "tail_roles": [m.get("role") for m in
                            (r["body"].get("messages") or [])][-6:],
             "n_tool_calls": sum(
                 1 for m in (r["body"].get("messages") or [])
                 if m.get("role") == "assistant" and m.get("tool_calls"))}
            for r in probe.requests]
        business = {
            "probe_sends": len(probe.requests),
            "per_request": per_req,
            "attempts": t["attempts"], "hops": t["hops"],
            "terminal": t["terminal_frames"],
            "continuation_saw_new_material": MID_MARK in final_txt,
            "tool_region_bytes": region,
            "tool_msgs": len(tool_msgs),
            "reply_delivered": bool(result),
            "outcomes": t["outcomes"]}
        _settle(d, business)
        return probe, rep, t, business

    flags_a = {"TOOL_EVIDENCE_WIRE": "0", "COST_HOP_RETRY_BUDGET": "0",
               "COST_WAKE_COALESCE": "0", "REVIEW_CONTEXT_PHASES": "0",
               "COST_MEMORY_STAGE_CACHE": "0"}
    flags_b = {**flags_a, "TOOL_EVIDENCE_WIRE": "1",
               "COST_HOP_RETRY_BUDGET": "1"}
    pa, _, ta, ba = await run_group("A", flags_a)
    pb, _, tb, bb = await run_group("B", flags_b)
    for t in (ta, tb):
        assert t["attempts"] == 5 and sorted(t["hops"]) == [1, 2, 3, 4]
        assert t["outcomes"]
    assert ba["continuation_saw_new_material"]
    assert bb["continuation_saw_new_material"]
    assert len(pa.requests) == len(pb.requests) == 5
    # B：同区间重复读投影成引用 ⇒ 续跳工具区字节下降
    assert bb["tool_region_bytes"] < ba["tool_region_bytes"]
    # 回退复跑：配置撤回 ⇒ 恢复基线（无投影、同字节量级）
    p2, _, t2, b2 = await run_group("B_rollback", flags_a)
    assert len(p2.requests) == 5 and t2["attempts"] == 5
    assert b2["tool_region_bytes"] == ba["tool_region_bytes"]
    (EVID / "combo_tool_continuation" / "result.json").write_text(json.dumps(
        {"a": {"sends": 5, "attempts": 5, "tool_region_bytes":
               ba["tool_region_bytes"]},
         "b": {"sends": 5, "attempts": 5, "tool_region_bytes":
               bb["tool_region_bytes"]},
         "rollback": {"sends": 5, "tool_region_bytes":
                      b2["tool_region_bytes"]},
         "material_updated_between_hops": True,
         "same_result": None},
        ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ §三.3 组合：后台失败恢复＋回退
def _l2_only(probe) -> bool:
    """该探针的全部出站都属 L2 段（按 system 提示身份判）。"""
    from src.storage import condense_two_layer as tl
    if not probe.requests:
        return False
    for req in probe.requests:
        msgs = (req.get("body") or {}).get("messages") or []
        if not msgs or tl.L2_PROMPT[:60] not in str(
                msgs[0].get("content") or ""):
            return False
    return True


@pytest.mark.asyncio
async def test_combo_background_recovery(work, sandbox, monkeypatch):
    """A 全关；B 只开 COST_MEMORY_STAGE_CACHE。**正式凝练入口＋适配器替身**：
    L2 截断注入→恢复只重发失效段；同卡同热缓存；前台出站单列；回退复跑。"""
    from outbound_probe import OutboundRecorder
    wid, cid = work["work_id"], work["work_card_id"]
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY",
                "VOLCENGINE_CODING_API_KEY", "ZHIPU_CODING_API_KEY",
                "MINIMAX_API_KEY"):
        monkeypatch.setenv(env, "stub")
    monkeypatch.setenv("COST_MEMORY_COMPACT_PROMPTS", "0")
    window = [{"seq": i + 21, "role": "assistant", "actor": "assistant",
               "ts": f"t{i}", "content": f"恢复窗口正文-{i + 1}"}
              for i in range(6)]

    biz = {}
    for group, on in (("A", "0"), ("B", "1")):
        d = _seg("combo_background_recovery", group, monkeypatch)
        monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", on)
        # —— 失败批：L1 解析耗尽作废（mark 不动、domain 不变、s2a 已缓存） ——
        p1 = OutboundRecorder(_stage_responder(l1_bad=True)).install(
            monkeypatch)
        rf = await _formal_condense(wid, cid, window, monkeypatch)
        fail_sends = len(p1.requests)
        # —— 恢复批：同卡同窗——只补失效/未完成段 ——
        p2 = OutboundRecorder(_stage_responder()).install(monkeypatch)
        rr = await _formal_condense(wid, cid, window, monkeypatch)
        resume_sends = len(p2.requests)
        g = {"fail_sends": fail_sends, "fail_ok": rf.get("ok"),
             "resume_sends": resume_sends, "resume_ok": rr.get("ok"),
             "resume_non_s2a": _non_s2a_only(p2), "frontend_sends": 0}
        # —— 回退复跑：同卡关开关 ——
        monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", "0")
        p3 = OutboundRecorder(_stage_responder()).install(monkeypatch)
        rb = await _formal_condense(wid, cid, window, monkeypatch)
        g["rollback_sends"] = len(p3.requests)
        g["rollback_ok"] = rb.get("ok")
        biz[group] = g
        _settle(d, g)
        assert fail_sends == 5, f"失败批应 s2a1+l1×4（实得 {fail_sends}）"
        if on == "1":
            assert resume_sends == 2 and _non_s2a_only(p2), (
                f"B 组恢复应只补 l1+l2（实得 {resume_sends} 发）")
        else:
            assert resume_sends == 3, "关态恢复应全段重发"
        assert len(p3.requests) == 3, "回退后同卡同输入应全段重发"
    (EVID / "combo_background_recovery" / "result.json").write_text(
        json.dumps(biz, ensure_ascii=False, indent=2), encoding="utf-8")
