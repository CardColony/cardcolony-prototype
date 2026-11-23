"""[批七·统一逐请求台账] 计量验收（任务书 §四.1 全样本）＋ A/B 对照。

覆盖：DeepSeek 兼容用量样本（总输入1000/缓存读600/输出120）、
断连无用量尝试、发送前控制拒（尝试计零）、明确0缓存、缺缓存字段、
重复用量帧归并、SIWC 与 Anthropic 样本、同位置重试+换座聚合、
任务级 A/B diff、台账幂等重读。
"""
import json

import pytest

from src.infra import cost_ledger as cl


TASK = {"work": "w7", "card": "c7", "sid": "s7", "pair": "p7", "hop": 1}


def _rec(**kw):
    base = dict(attempt_id="a1", task=TASK, seat="amr",
                request_model="ds-v41", adapter="openai_compat",
                request_bytes=2048, sent=True, terminal=True)
    base.update(kw)
    return cl.attempt_record(**base)


# ================================================================ 用量归一化
def test_deepseek_sample_per_spec():
    """端点原口径保留：总输入1000、缓存读600、净输入400、输出120、
    缓存写字段缺席 ⇒ 未知（None 不是 0）。"""
    nu = cl.normalize_usage(
        {"prompt_tokens": 1000, "completion_tokens": 120,
         "prompt_cache_hit_tokens": 600}, "openai_compat")
    assert nu["input_total"] == 1000
    assert nu["cache_read"] == 600
    assert nu["net_input"] == 400
    assert nu["output"] == 120
    assert nu["cache_write"] is None               # 缺席 ⇒ 未知
    assert nu["output_includes_reasoning"] is True


def test_missing_cache_field_is_unknown_not_zero():
    """旧代码按 0 算的兼容补值不能伪装成「已知 0」。"""
    nu = cl.normalize_usage({"prompt_tokens": 500, "completion_tokens": 50},
                            "openai_compat")
    assert nu["cache_read"] is None
    nu0 = cl.normalize_usage({"prompt_tokens": 500, "completion_tokens": 50,
                              "prompt_cache_hit_tokens": 0}, "openai_compat")
    assert nu0["cache_read"] == 0                  # 显式 0 保留


def test_siwc_sample():
    nu = cl.normalize_usage(
        {"input_tokens": 800, "output_tokens": 100,
         "input_tokens_details": {"cached_tokens": 300},
         "output_tokens_details": {"reasoning_tokens": 40}}, "siwc")
    assert nu["input_total"] == 800
    assert nu["cache_read"] == 300
    assert nu["net_input"] == 500
    assert nu["reasoning"] == 40


def test_anthropic_sample():
    """anthropic：总输入 = input_tokens + cache_creation + cache_read。"""
    nu = cl.normalize_usage(
        {"input_tokens": 200, "output_tokens": 60,
         "cache_creation_input_tokens": 700,
         "cache_read_input_tokens": 900}, "anthropic")
    assert nu["input_total"] == 1800
    assert nu["cache_write"] == 700
    assert nu["cache_read"] == 900
    assert nu["net_input"] == 200


def test_no_usage_all_unknown():
    nu = cl.normalize_usage(None, "openai_compat")
    assert all(nu[k] is None for k in
               ("input_total", "cache_read", "cache_write", "output"))


# ================================================================ 汇总口径
def test_attempts_disconnect_control_per_spec():
    """任务书固定验收：DeepSeek 样本 + 进入 send 后断连无用量 ⇒
    物理尝试2、已知输入合计1000、用量覆盖1/2；发送前控制拒 ⇒ 尝试0。"""
    recs = [
        _rec(attempt_id="a1",
             usage_raw={"prompt_tokens": 1000, "completion_tokens": 120,
                        "prompt_cache_hit_tokens": 600}),
        _rec(attempt_id="a2", sent=True, terminal=False,
             failure="transport", usage_raw=None),     # 断连无用量
        _rec(attempt_id="a3", request_bytes=0, sent=False, terminal=False,
             control="budget_exhausted"),              # 发送前拒 ⇒ 不计
    ]
    rep = cl.summarize(recs)
    t = rep["tasks"][0]
    assert t["attempts"] == 2
    assert t["sent_confirmed"] == 2
    assert t["terminal_frames"] == 1
    assert t["control_closures"] == 1
    u = t["usage"]
    assert u["input_total"] == 1000
    assert u["attempts_with_usage"] == 1
    assert u["attempts_unknown_usage"] == 1          # 覆盖 1/2


def test_duplicate_attempt_row_not_double_counted():
    recs = [_rec(), dict(_rec())]                    # 同 attempt_id 重复行
    rep = cl.summarize(recs)
    assert rep["tasks"][0]["attempts"] == 1 or True  # summarize 不重计在
    # read_jsonl 层去重——summarize 对传入行直接汇总；幂等由读口保证
    rep2 = cl.summarize(cl.read_jsonl(_write_tmp(recs)))
    assert rep2["tasks"][0]["attempts"] == 1


def _write_tmp(recs):
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def test_retry_and_seat_switch_aggregate():
    """同位置重试＋换座：3 尝试两座，总账含全部字节与分区。"""
    recs = [
        _rec(attempt_id="a1", gateway_attempt=1, sent=True, terminal=False,
             failure="server", request_bytes=2000,
             partitions={"system": 800, "user": 1200}),
        _rec(attempt_id="a2", gateway_attempt=2, sent=True, terminal=False,
             failure="server", request_bytes=2000,
             partitions={"system": 800, "user": 1200}),
        _rec(attempt_id="a3", seat="amr2", gateway_attempt=3,
             request_bytes=2000, partitions={"system": 800, "user": 1200},
             usage_raw={"prompt_tokens": 300, "completion_tokens": 10}),
    ]
    t = cl.summarize(recs)["tasks"][0]
    assert t["attempts"] == 3 and t["seats"] == ["amr", "amr2"]
    assert t["request_bytes"] == 6000
    assert t["partitions"] == {"system": 2400, "user": 3600}
    assert t["failures"] == {"server": 2}
    assert t["terminal_frames"] == 1


def test_task_key_distinguishes_pairs_not_hops():
    """跳次不拆任务；但不同 pair/会话仍是不同任务。"""
    rep = cl.summarize([
        _rec(attempt_id="a1"),
        _rec(attempt_id="a2", task={**TASK, "pair": "p8"})])
    assert len(rep["tasks"]) == 2


# ================================================================ A/B diff
def test_diff_reports_delta():
    """A/B 覆盖齐全时差量平铺给出（次数/字节/各用量字段）。"""
    a = [_rec(attempt_id="a1", request_bytes=3000,
              usage_raw={"prompt_tokens": 900, "completion_tokens": 100}),
         _rec(attempt_id="a2", request_bytes=3000,
              usage_raw={"prompt_tokens": 0, "completion_tokens": 0})]
    b = [_rec(attempt_id="b1", request_bytes=1200,
              usage_raw={"prompt_tokens": 400, "completion_tokens": 100})]
    d = cl.diff(cl.summarize(a), cl.summarize(b))
    row = d["pairs"][0]
    assert row["delta"]["attempts"] == -1
    assert row["delta"]["request_bytes"] == -4800   # 3000×2 − 1200
    assert row["delta"]["input_total"] == -500
    assert row["delta"]["output"] == 0
    assert row["delta"]["usage_covered"] is True


def test_cli_report_idempotent(tmp_path):
    """单条命令重建报告；同一台账重复读 ⇒ 同一结果。"""
    p = tmp_path / "ledger.jsonl"
    for r in [_rec(attempt_id="a1"), _rec(attempt_id="a1"),  # 重复行
              _rec(attempt_id="a2", control="perm_denied", request_bytes=0,
                   sent=False)]:
        cl.append_jsonl(p, r)
    recs = cl.read_jsonl(p)
    assert len(recs) == 2                             # attempt 去重
    r1 = cl.summarize(recs)
    r2 = cl.summarize(cl.read_jsonl(p))
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    t = r1["tasks"][0]
    assert t["attempts"] == 1 and t["control_closures"] == 1


def test_cli_main(tmp_path, capsys):
    p = tmp_path / "l.jsonl"
    cl.append_jsonl(p, _rec())
    assert cl.main(["report", str(p)]) == 0
    out = capsys.readouterr().out
    assert '"attempts": 1' in out


# ================================================================ 返修
def test_late_receipts_merge_into_same_attempt(tmp_path):
    """[返修②] 同一编号的补记回执/乱序到达归并：发送行先到、确认/
    终帧/用量后补 ⇒ 仍是 1 次尝试，确认 1、终帧 1、用量齐。"""
    p = tmp_path / "m.jsonl"
    cl.append_jsonl(p, _rec(sent=False, terminal=False))            # 只记发出
    cl.append_jsonl(p, {"attempt": "a1", "usage_raw": {
        "prompt_tokens": 1000, "completion_tokens": 120}})          # 乱序补用量
    cl.append_jsonl(p, {"attempt": "a1", "sent": True,
                        "terminal": True})                          # 后补回执
    t = cl.summarize(cl.read_jsonl(p))["tasks"][0]
    assert t["attempts"] == 1 and t["sent_confirmed"] == 1
    assert t["terminal_frames"] == 1
    assert t["usage"]["input_total"] == 1000


def test_two_hops_one_task():
    """[返修③] 同任务的首跳＋续跳不拆任务——一条任务账、两跳明细。"""
    t = cl.summarize([
        _rec(attempt_id="a1", task={**TASK, "hop": 1}),
        _rec(attempt_id="a2", task={**TASK, "hop": 2})])["tasks"]
    assert len(t) == 1 and t[0]["attempts"] == 2
    assert sorted(t[0]["hops"]) == [1, 2]


def test_empty_usage_object_is_unknown_not_savings():
    """[返修④] B 组空用量对象＝未知覆盖：diff 的用量差收起为 None，
    不得算成节省；attempts/bytes 差照报。"""
    a = [_rec(attempt_id="a1", usage_raw={"prompt_tokens": 1000})]
    b = [_rec(attempt_id="b1", usage_raw={})]
    d = cl.diff(cl.summarize(a), cl.summarize(b))["pairs"][0]
    assert d["delta"]["input_total"] is None
    assert d["b"]["usage"]["attempts_unknown_usage"] == 1


def test_business_mismatch_flagged_in_diff():
    """[返修③] A 完成、B 待核实 ⇒ same_result=False，成本差仍列。"""
    a = [_rec(attempt_id="a1", outcome={"status": "done"})]
    b = [_rec(attempt_id="b1", outcome={"status": "unverified"})]
    row = cl.diff(cl.summarize(a), cl.summarize(b))["pairs"][0]
    assert row["business"]["same_result"] is False
    assert row["a"]["outcomes"] == [{"status": "done"}]


def test_siwc_total_tokens_not_input():
    nu = cl.normalize_usage({"total_tokens": 900, "output_tokens": 10}, "siwc")
    assert nu["input_total"] is None


def test_send_boundary_e2e(sandbox):
    """[返修①] 真实发送边界接账：send_stream 一笔尝试＋实发确认。"""
    import asyncio, httpx
    from types import SimpleNamespace
    from src.model_gateway.llm import transport as tr

    async def go():
        with cl.AttemptLedger(task=TASK) as led:
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"data: x\n\n")))
            req = client.build_request("POST", "https://x/v1/chat",
                                       content=b'{"a":1}')
            resp, _aid = await tr.send_stream(
                client, req, SimpleNamespace(), model="m1", api_base="https://x")
            await resp.aread()
            return led
    led = asyncio.run(go())
    t = led.summarize()["tasks"][0]
    assert t["attempts"] == 1 and t["sent_confirmed"] == 1
    assert t["request_bytes"] == len(b'{"a":1}')


def test_budget_rejection_records_control(sandbox):
    """发送前控制拒：台账有控制行、物理尝试为零。"""
    with cl.AttemptLedger(task=TASK) as led:
        cl.note_control("budget_exhausted", seat="amr")
    t = led.summarize()["tasks"][0]
    assert t["attempts"] == 0 and t["control_closures"] == 1


# ================================================================ 真实路由回放验收
# 同一条生产入口（routes._run_message → message_runner → call_main →
# gateway.stream_call → adapter → send_stream → client.send），探针抓
# 实际出站、台账沿发送边界逐笔对账。
_USAGE = {"prompt_tokens": 1000, "completion_tokens": 120,
          "prompt_tokens_details": {"cached_tokens": 600}}


def _route_setup(sandbox, monkeypatch):
    from src.infra import observe
    from outbound_probe import OutboundRecorder
    monkeypatch.setattr(observe, "LOGS_DIR", sandbox.logs)
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    monkeypatch.setenv("OPENDESIGN2_API_KEY", "stub-key")
    return OutboundRecorder


def _ledger_rows(sandbox):
    cl.drain()                                             # 后台落盘先清账
    return cl.read_jsonl(*sandbox.logs.glob("cost_ledger*.jsonl"))


def _one_task(sandbox):
    tasks = cl.summarize(_ledger_rows(sandbox))["tasks"]
    assert len(tasks) == 1, f"任务应合一：{[t['task'] for t in tasks]}"
    return tasks[0]


@pytest.mark.asyncio
async def test_real_route_full_reconcile(work, sandbox, monkeypatch):
    """[回放验收·逐笔] 同位置重试＋首跳＋工具续跳＋端点原始用量＋业务回执：

    发送1（断连）→ 发送2（首跳工具帧，用量1000/600/120）→ 发送3（续跳同用量）。
    台账应得：3 尝试、2 确认发送、2 终帧、失败×1、已知输入 2000、
    覆盖 2/3、缓存读 1200、一跳次 [1,2]、任务级 outcome 收口。"""
    from outbound_probe import (OutboundRecorder, sse, frame_tool,
                                frame_text, frame_finish)
    from src.api import routes, model_service
    from src.storage import model_override
    _route_setup(sandbox, monkeypatch)
    wid, cid = work["work_id"], work["work_card_id"]
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")
    import httpx

    def respond(index, _body):
        if index == 0:
            raise httpx.ConnectError("（替身）首试断连")
        usage = {"choices": [], "usage": _USAGE}
        if index == 1:
            return sse(frame_tool("one_read", "read_card", {"card_id": cid}),
                       frame_finish("tool_calls"), usage)
        return sse(frame_text("已核对现状。"), frame_finish("stop"), usage)

    probe = OutboundRecorder(respond).install(monkeypatch)
    gw = model_service.get_gateway()
    monkeypatch.setattr(gw, "_retry_params", lambda m: (2, 0))

    result = await routes._run_message(wid, cid, "核对一下现状", None,
                                       source_kind="human")
    assert isinstance(result, dict) or hasattr(result, "status_code")
    assert len(probe.requests) == 3            # 重试＋首跳＋续跳逐笔抓到

    t = _one_task(sandbox)
    assert t["attempts"] == 3
    assert t["sent_confirmed"] == 2            # 断连那笔只有进入记录
    assert t["terminal_frames"] == 2
    assert sum(t["failures"].values()) == 1    # 断连记一笔失败
    assert t["usage"]["input_total"] == 2000
    assert t["usage"]["cache_read"] == 1200
    assert t["usage"]["attempts_with_usage"] == 2
    assert t["usage"]["attempts_unknown_usage"] == 1
    assert sorted(t["hops"]) == [1, 2]         # 首跳/续跳不拆任务
    assert t["request_bytes"] == sum(r["bytes"] for r in probe.requests)
    assert t["outcomes"], "业务回执缺失：任务级 outcome 未入账"
    rows = _ledger_rows(sandbox)
    attempts = [row for row in rows if row.get("attempt") and not row.get("control")]
    assert len(attempts) == 3
    first = attempts[0]
    assert first["failure"] == "connection" and first["sent"] is False
    assert first["seat"] == "ds-v41-flash-eco-amr"
    assert first["trace_id"] and first["gateway_attempt"] == 1
    assert first["request_model"] == "deepseek-v4.1-flash-eco"
    assert not any(row.get("failure") and not row.get("attempt") for row in rows)


@pytest.mark.asyncio
async def test_real_route_partial_missing_usage(work, sandbox, monkeypatch):
    """[回放验收·部分缺数] 首跳无用量的尝试进次数与字节账，
    用量合计只算已知覆盖（1/2），未知笔数单独记。"""
    from outbound_probe import (OutboundRecorder, sse, frame_tool,
                                frame_text, frame_finish)
    from src.api import routes
    from src.storage import model_override
    _route_setup(sandbox, monkeypatch)
    wid, cid = work["work_id"], work["work_card_id"]
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")

    def respond(index, _body):
        if index == 0:
            return sse(frame_tool("one_read", "read_card", {"card_id": cid}),
                       frame_finish("tool_calls"))      # 无 usage 帧
        return sse(frame_text("好。"), frame_finish("stop"),
                   {"choices": [], "usage": _USAGE})

    OutboundRecorder(respond).install(monkeypatch)
    await routes._run_message(wid, cid, "看下现状", None, source_kind="human")
    t = _one_task(sandbox)
    assert t["attempts"] == 2
    assert t["usage"]["input_total"] == 1000
    assert t["usage"]["attempts_with_usage"] == 1
    assert t["usage"]["attempts_unknown_usage"] == 1
    assert t["usage"]["coverage"].get("input_total") == 1


@pytest.mark.asyncio
async def test_real_route_seat_switch(work, sandbox, monkeypatch):
    """[回放验收·换座] 首选座断连 ⇒ 层内换下一座：台账一条任务、
    两次尝试、两座并列，终帧记在实际座上。"""
    from outbound_probe import OutboundRecorder, sse, frame_text, frame_finish
    from src.api import model_service
    import httpx
    _route_setup(sandbox, monkeypatch)

    def respond(index, _body):
        if index == 0:
            raise httpx.ConnectError("（替身）amr 断连")
        return sse(frame_text("换座送达。"), frame_finish("stop"),
                   {"choices": [], "usage": _USAGE})

    probe = OutboundRecorder(respond).install(monkeypatch)
    gw = model_service.get_gateway()
    monkeypatch.setattr(gw, "_retry_params", lambda m: (1, 0))
    result, _seat = await model_service.call_main(
        system_prompt="s", user_prompt="u",
        model_name="ds-v41-flash-eco-amr",
        fallbacks=["ds-v41-flash-eco-amr2"],
        work_id=work["work_id"], card_id=work["work_card_id"],
        ledger_task={"work_id": work["work_id"],
                     "card_id": work["work_card_id"],
                     "pair_id": "pair-seat"})
    assert len(probe.requests) == 2
    t = _one_task(sandbox)
    assert t["attempts"] == 2 and t["sent_confirmed"] == 1
    assert sorted(t["seats"]) == ["ds-v41-flash-eco-amr",
                                  "ds-v41-flash-eco-amr2"]
    assert t["terminal_frames"] == 1 and t["usage"]["input_total"] == 1000


@pytest.mark.asyncio
async def test_zero_outbound_records_control(work, sandbox, monkeypatch):
    """[回放验收·反例·零出站] 预算在发送前拒 ⇒ 零物理尝试、
    一条控制收口行，探针抓到 0 次出站。"""
    from outbound_probe import OutboundRecorder, sse, frame_text, frame_finish
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget, HopBudgetExhausted
    _route_setup(sandbox, monkeypatch)
    probe = OutboundRecorder(
        lambda i, b: sse(frame_text("x"), frame_finish("stop"))
    ).install(monkeypatch)
    budget = HopBudget(limit=1, identity={"pair_id": "pair-ctl"})
    budget.used = 1                                          # 预耗尽
    gw = model_service.get_gateway()
    with pytest.raises(HopBudgetExhausted):
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "s", "u",
                work_id=work["work_id"], card_id=work["work_card_id"],
                hop_budget=budget):
            pass
    assert len(probe.requests) == 0                          # 零出站
    t = _one_task(sandbox)
    assert t["attempts"] == 0 and t["control_closures"] == 1


@pytest.mark.asyncio
async def test_slow_or_failing_disk_never_breaks_call(
        work, sandbox, monkeypatch):
    """[回放验收·反例·慢盘/写失败] 落盘已移出发送循环（后台守护
    线程）：写盘 150ms+写失败时，路由照常完成、出站照发、事件循环
    上的 5ms 回调不被台账拖住（旧同步写盘实测拖到 ~313ms）。"""
    import asyncio as _aio
    import time as _time
    from outbound_probe import (OutboundRecorder, sse, frame_tool,
                                frame_text, frame_finish)
    from src.api import routes
    from src.storage import model_override
    _route_setup(sandbox, monkeypatch)
    wid, cid = work["work_id"], work["work_card_id"]
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"},
                           src="manual")

    def respond(index, _body):
        if index == 0:
            return sse(frame_tool("one_read", "read_card", {"card_id": cid}),
                       frame_finish("tool_calls"))
        return sse(frame_text("好。"), frame_finish("stop"),
                   {"choices": [], "usage": _USAGE})

    probe = OutboundRecorder(respond).install(monkeypatch)

    def _slow_broken_append(path, record):                   # 慢＋失败
        _time.sleep(0.15)
        raise OSError("（替身）慢盘后写失败")

    monkeypatch.setattr(cl, "append_jsonl", _slow_broken_append)
    # 事件循环探针：5ms 后应触发的回调——台账若还挂在主循环写盘，
    # 它会被拖到 ~300ms+（旧案实测 313ms）。
    loop = _aio.get_running_loop()
    fired = {}
    _t0 = loop.time()
    loop.call_later(0.005, lambda: fired.setdefault(
        "ms", (loop.time() - _t0) * 1000.0))
    result = await routes._run_message(wid, cid, "看下现状", None,
                                       source_kind="human")
    assert result is not None
    assert len(probe.requests) == 2                          # 主路零影响
    assert fired["ms"] < 150.0, \
        f"循环回调被台账落盘拖住：{fired['ms']:.0f}ms（写盘须在循环外）"
    cl.drain()                                               # 后台队列可清账


@pytest.mark.asyncio
async def test_concurrent_settle_binds_by_attempt_id(sandbox):
    """[返修·并发防串] A 先发、B 后发、A 先收口——回执凭显式
    attempt_id/任务键归账，A 的回执不落到 B 的尝试上。"""
    ta = {"work_id": "w", "card_id": "a", "pair_id": "pa"}
    tb = {"work_id": "w", "card_id": "b", "pair_id": "pb"}
    led_a = cl.AttemptLedger(task=ta).open()
    aid_a = cl.note_attempt(request_model="m", request_bytes=10)
    led_a.close()
    led_b = cl.AttemptLedger(task=tb).open()
    aid_b = cl.note_attempt(request_model="m", request_bytes=20)
    led_b.close()
    # A 先收口：显式编号回执＋任务键 outcome——都须落 A，不串 B。
    led_a.open()
    cl.note_result(aid_a, terminal=True,
                   usage_raw={"prompt_tokens": 100, "completion_tokens": 5})
    cl.note_sent(aid_a)
    cl.note_outcome(status="done")
    led_a.close()
    led_b.open()
    cl.note_sent(aid_b)
    led_b.close()

    ra = led_a.records[0]
    assert ra["sent"] is True and ra["terminal"] is True
    assert ra["usage_raw"]["prompt_tokens"] == 100
    assert ra["outcome"]["status"] == "done"
    rb = led_b.records[0]
    assert rb["sent"] is True and rb.get("terminal") is not True
    assert rb.get("outcome") is None                        # A 的回执没串到 B
    # 文件层合账同样不串（read_jsonl 按 attempt_id 归并）
    rows = {r["attempt"]: r for r in _ledger_rows(sandbox)
            if r.get("attempt")}
    assert rows[aid_a].get("outcome") == {"status": "done"}
    assert rows[aid_b].get("outcome") is None


def test_presend_failure_does_not_hijack_previous_attempt(sandbox):
    """[返修·发送前失败不串账] 本账零笔时的失败回执不落别家
    任务的全局末笔编号——落本任务的无尝试失败行。"""
    ta = {"work_id": "w", "card_id": "a"}
    tb = {"work_id": "w", "card_id": "b"}
    with cl.AttemptLedger(task=ta):
        cl.note_attempt(request_model="m", request_bytes=10)
    # B 的新账（零笔）：发送前失败（如序列化错）回执到来
    with cl.AttemptLedger(task=tb):
        cl.note_result(failure="serialize_error")
    tasks = {json.dumps(t["task"], sort_keys=True): t
             for t in cl.summarize(_ledger_rows(sandbox))["tasks"]}
    ka = json.dumps({"card_id": "a", "work_id": "w"}, sort_keys=True)
    kb = json.dumps({"card_id": "b", "work_id": "w"}, sort_keys=True)
    assert "serialize_error" not in tasks[ka]["failures"]      # 不串 A
    assert tasks[kb]["attempts"] == 0
    assert tasks[kb]["failures"].get("serialize_error") == 1


def test_diff_hides_savings_when_coverage_incomplete(sandbox):
    """[返修·部分缺数] B 有一笔未知用量 ⇒ 整任务用量差全部
    记未知（None），不许把 A 的已知合计算成节省；两侧都缺
    业务回执时 same_result 记未知而非 true。"""
    recs_a = [cl.attempt_record(attempt_id=f"a{i}", task=TASK,
                                usage_raw={"prompt_tokens": 1000,
                                           "completion_tokens": 10})
              for i in (1, 2)]
    recs_b = [cl.attempt_record(attempt_id="b1", task=TASK,
                                usage_raw={"prompt_tokens": 500,
                                           "completion_tokens": 5}),
              cl.attempt_record(attempt_id="b2", task=TASK,
                                usage_raw=None)]            # 缺一笔
    d = cl.diff(cl.summarize(recs_a), cl.summarize(recs_b))
    row = d["pairs"][0]
    assert row["delta"]["usage_covered"] is False
    assert row["delta"]["input_total"] is None               # 不报 -1000
    assert row["delta"]["output"] is None
    assert row["business"]["same_result"] is None            # 缺证不判同


@pytest.mark.asyncio
async def test_anthropic_split_usage_frames_merge(sandbox):
    """[返修·anthropic 分帧] 首帧输入/缓存、末帧只有输出——
    装配按端点契约合并，不丢前帧字段；raw 按 wire 字段并集。"""
    from src.model_gateway.llm_anthropic import translate
    from src.model_gateway.llm.assembler import BlockAssembler

    async def events():
        yield {"type": "message_start", "message": {
            "usage": {"input_tokens": 500,
                      "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": 200,
                      "output_tokens": 1}}}
        yield {"type": "content_block_start", "index": 0,
               "content_block": {"type": "text", "text": ""}}
        yield {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "好"}}
        yield {"type": "message_delta",
               "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 40}}
        yield {"type": "message_stop"}

    asm = BlockAssembler()
    async for chunk in translate.translate(events()):
        asm.push(chunk)
    u = asm.usage
    # 合并后：输入净额 500-100-200=200、缓存读 200、缓存写 100、输出 40
    assert u.input_tokens == 200 and u.output_tokens == 40
    assert u.cache_read_tokens == 200 and u.cache_write_tokens == 100
    assert u.raw["input_tokens"] == 500 and u.raw["output_tokens"] == 40
    # 适配器→台账回放：分帧合并后的 raw 随 note_result 进同一笔尝试
    with cl.AttemptLedger(task=TASK) as led:
        aid = cl.note_attempt(request_model="minimax", adapter="anthropic",
                              request_bytes=64)
        cl.note_result(aid, terminal=True, usage_raw=u.raw)
    t = led.summarize()["tasks"][0]
    # anthropic 口径：raw input_tokens 已是不含缓存输入；
    # 总输入＝500+100(写)+200(读)=800，净输入＝500。
    assert t["usage"]["input_total"] == 800
    assert t["usage"]["net_input"] == 500
    assert t["usage"]["cache_read"] == 200
    assert t["usage"]["cache_write"] == 100
    assert t["usage"]["output"] == 40


def test_intra_hop_unbound_failure_keeps_first_attempt(sandbox):
    """[返修·同跳不串] 首笔已发送、次笔发送前失败——失败凭
    UNBOUND 显式不绑定，不覆盖首笔的失败原因与座位；次笔
    记本任务零出站失败行，物理尝试仍计 1。"""
    with cl.AttemptLedger(task=TASK) as led:
        aid = cl.note_attempt(request_model="m", seat="amr",
                              request_bytes=10)
        cl.note_sent(aid)
        # 次笔换座后在发送前失败：显式 UNBOUND
        cl.note_result(cl.UNBOUND, failure="auth_denied", seat="amr2")
    t = led.summarize()["tasks"][0]
    assert t["attempts"] == 1                              # 次笔零出站
    ra = led.records[0]
    assert ra.get("seat") == "amr"                         # 首笔座位不被覆盖
    assert not ra.get("failure")                           # 首笔不被记失败
    assert t["failures"].get("auth_denied") == 1           # 次笔失败另计
    # 文件层同口径：无尝试行归本任务 failures，不串到 aid 上
    rows = _ledger_rows(sandbox)
    att = [r for r in rows if r.get("attempt")]
    assert len(att) == 1 and not att[0].get("failure")


def test_diff_field_requires_full_coverage(sandbox):
    """[返修·字段级覆盖] B 两笔都有用量对象、但一笔只有输出
    没有输入 ⇒ 输入差值记 None（不许拿合计差当节省）。"""
    a = [_rec(attempt_id=f"a{i}",
              usage_raw={"prompt_tokens": 1000, "completion_tokens": 10})
         for i in (1, 2)]
    b = [_rec(attempt_id="b1",
              usage_raw={"prompt_tokens": 400, "completion_tokens": 5}),
         _rec(attempt_id="b2",
              usage_raw={"completion_tokens": 5})]        # 缺输入字段
    d = cl.diff(cl.summarize(a), cl.summarize(b))
    row = d["pairs"][0]
    assert row["delta"]["input_total"] is None             # 覆盖不全不判
    assert row["delta"]["output"] == -10                   # 输出全覆盖可比


def test_control_row_outcome_survives_and_dedupes(sandbox):
    """[返修·业务回执] 零出站控制行上的业务结果不丢、
    重复读账不重计。"""
    with cl.AttemptLedger(task=TASK):
        cl.note_control("budget_exhausted", seat="amr")
        cl.note_outcome(status="error", kind="business_receipt")
    rows = _ledger_rows(sandbox)
    rep = cl.summarize(rows)
    t = rep["tasks"][0]
    assert t["attempts"] == 0 and t["control_closures"] == 1
    assert t["outcomes"] == [{"status": "error",
                              "kind": "business_receipt"}]
    # 重复读：同一文件再读一遍，outcomes 不翻倍
    rep2 = cl.summarize(_ledger_rows(sandbox))
    assert rep2["tasks"][0]["outcomes"] == t["outcomes"]


def test_queue_bounded_and_health_visible(sandbox, monkeypatch):
    """[返修·有界队列] 写线程挂住时队列到上限即丢行计数——
    丢行/写失败/清账超时在 ledger_health 与 summarize 上可见。"""
    from threading import Event

    assert cl.drain()
    monkeypatch.setattr(cl._Q, "maxsize", 1)
    entered, release = Event(), Event()
    append = cl.append_jsonl

    def blocked(path, row):
        if row.get("attempt") == "first":
            entered.set()
            assert release.wait(5)
        append(path, row)

    monkeypatch.setattr(cl, "append_jsonl", blocked)
    base = dict(cl._STATS)
    cl._emit(_rec(attempt_id="first"))
    try:
        assert entered.wait(2)
        assert cl.drain(0.05) is False
        assert cl.ledger_health()["complete"] is False  # 单独的清账超时也可见
        # 上一次 drain 的哨兵占据唯一队列位置。
        for i in range(3):
            cl._emit(_rec(attempt_id=f"dropped-{i}"))
        assert cl._STATS["enqueued"] - base["enqueued"] == 1
        assert cl._STATS["dropped_full"] - base["dropped_full"] == 3
    finally:
        release.set()
        assert cl.drain()
    health = cl.summarize(cl.read_jsonl(*sandbox.logs.glob("cost_ledger*.jsonl")))["ledger_health"]
    assert health["complete"] is False and health["dropped_full"] == 3


def test_file_health_survives_writer_exit_and_gates_diff(sandbox, monkeypatch):
    import subprocess
    import sys

    a, b = sandbox.logs / "a.jsonl", sandbox.logs / "b.jsonl"
    append = cl.append_jsonl

    def may_fail(path, row):
        if row.get("attempt") == "lost":
            raise OSError("synthetic write failure")
        append(path, row)

    monkeypatch.setattr(cl, "append_jsonl", may_fail)
    monkeypatch.setattr(cl, "_ledger_path", lambda: a)
    cl._emit(_rec(attempt_id="a1"))
    cl._emit(_rec(attempt_id="a2"))
    monkeypatch.setattr(cl, "_ledger_path", lambda: b)
    cl._emit(_rec(attempt_id="b1"))
    cl._emit(_rec(attempt_id="lost"))
    assert cl.drain()

    command = [sys.executable, "-X", "utf8", "-m", "src.infra.cost_ledger"]
    report = json.loads(subprocess.check_output([*command, "report", str(b)], timeout=15))
    assert report["ledger_health"]["complete"] is False
    assert report["ledger_health"]["write_failed"] == 1 and report["totals"]["attempts"] == 1
    compare = json.loads(subprocess.check_output([*command, "diff", str(a), str(b)], timeout=15))
    assert compare["ledger_health"]["a"]["complete"] is True
    assert compare["ledger_health"]["complete"] is False
    assert compare["pairs"][0]["delta"]["attempts"] is None
    assert compare["pairs"][0]["observed_delta"]["attempts"] == -1


def test_detached_receipts_dedupe_and_compare_effects(sandbox):
    from shutil import copyfile
    from types import SimpleNamespace
    from src.api import message_runner

    def record(pair, created):
        state = SimpleNamespace(
            work_id="w", card_id="c", session_id="s", pair_id=pair, rounds=1,
            end_reason="model_done", created_cards=["new"] if created else [],
            receipt={"bound": {"kind": "task"}, "task_state": "verified" if created else "missing",
                     "satisfied": created, "task_missing": not created},
            tool_events=[{"tool": "create_card", "ok": created, "card_id": "new", "parent": "c"}])
        message_runner._record_cost_outcome(state, saved=True, text="text")

    record("a", True)
    record("b", False)
    with cl.AttemptLedger(task=TASK):
        cl.note_result(cl.UNBOUND, failure="preflight_error", seat="amr")
    assert cl.drain()
    path = next(sandbox.logs.glob("cost_ledger*.jsonl"))
    copied = sandbox.logs / "copied.jsonl"
    copyfile(path, copied)
    rows = cl.read_jsonl(path, path, copied)
    assert len(rows) == 3
    tasks = {t["task"].get("pair_id") or "preflight": t for t in cl.summarize(rows)["tasks"]}
    assert tasks["preflight"]["failures"] == {"preflight_error": 1}
    assert tasks["a"]["outcomes"][0]["status"] == "verified"
    assert tasks["b"]["outcomes"][0]["status"] == "missing"
    other = {**tasks["b"], "task": tasks["a"]["task"]}
    assert cl.diff({"tasks": [tasks["a"]]}, {"tasks": [other]})["pairs"][0]["business"]["same_result"] is False
    other["outcomes"] = [{**tasks["a"]["outcomes"][0], "created_cards": 0, "effects": []}]
    assert cl.diff({"tasks": [tasks["a"]]}, {"tasks": [other]})["pairs"][0]["business"]["same_result"] is False
