"""[批六·阶段复用] COST_MEMORY_STAGE_CACHE / COST_MEMORY_COMPACT_PROMPTS。

隔离回放覆盖验收矩阵：
- 同一输入重复 ⇒ 第二次三段全部命中，**实际出站为零**；
- L1 失败 ⇒ S2a 命中只重发 L1/L2；L2 解析耗尽 ⇒ 恢复只重发 L2（1 次）；
- 等长改写 / 块边界变化 / 顺序变化 / 隔离态变化 / 新块入窗 ⇒ 对应段失效；
- 损坏文件 / 半写文件 ⇒ 未命中结算；
- 跨 work/card 同文字 ⇒ 不命中（租户边界）；
- 关态 ⇒ 每次三段全发（基线）；
- 缩量试点 ⇒ L2 输入字节下降且只在阶段缓存开时生效；
- 归拢组键：成员指纹含 gist/覆盖/timeline/level/子条/隔离态——
  顶层 seq+gist 恒定时的成员内容变化即换键。
"""
import asyncio
import json
from copy import deepcopy

import pytest

from src.storage import condense_two_layer as tl
from src.storage import stage_cache
from tests.outbound_probe import OutboundRecorder, frame_finish, frame_text, sse


BLOCKS = [{"seq": i + 1, "role": "assistant", "actor": "assistant",
           "ts": f"t{i}", "content": f"正文内容-{i + 1}"} for i in range(6)]
CTX = {"work_id": "w6", "card_id": "c6", "model": "cond-model", "shard": "b1/s0"}

S2A = '{"groups":[{"end":2},{"end":5}]}'
L1 = ("【块1】\n开始：起1\n经过：过1\n完成：果1\n"
      "【块2】\n开始：起2\n完成：果2\n教训：训2")
L2 = ('{"l2":[{"group":1,"subtopics":[{"title":"t1","gist":"d1","end":2}]},'
      '{"group":2,"subtopics":[]}]}')


def _on(monkeypatch, flag="1"):
    monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", flag)


def _runner(script=None, counter=None):
    """脚本化 call_llm：按 S2A/L1/L2 prompt 识别段位，计实际出站。"""
    script = script or {}
    calls = counter if counter is not None else []

    async def call(system, user):
        calls.append(system)
        if system is tl.S2A_PROMPT:
            if script.get("s2a_raise"):
                raise RuntimeError("s2a boom")
            return script.get("s2a", S2A)
        if system is tl.L1_PROMPT:
            if script.get("l1_raise"):
                raise RuntimeError("connection reset by peer")   # 可重试类
            return script.get("l1", L1)
        if system is tl.L2_PROMPT:
            if script.get("l2_raise"):
                raise RuntimeError("l2 boom")
            return script.get("l2", L2)
        raise AssertionError("未知段")

    return call, calls


def _run(blocks=None, script=None, ctx=CTX, counter=None, domain="全景"):
    call, calls = _runner(script, counter)
    res = asyncio.run(tl.run_two_layer(
        blocks if blocks is not None else BLOCKS, call,
        session_id="c6", domain_full=domain, stage_ctx=ctx))
    return res, calls


# ================================================================ 同输入重复
def test_repeat_identical_second_run_zero_outbound(sandbox, monkeypatch):
    """同一未发布快照回跑：第二次三段全命中，出站=0，产物逐字节一致。"""
    _on(monkeypatch)
    r1, c1 = _run()
    r2, c2 = _run()
    assert r1["ok"] and len(c1) == 3
    assert r2["ok"] and c2 == []                      # 命中段零出站
    assert r1["nodes"] == r2["nodes"]
    assert r1["descriptions"] == r2["descriptions"]
    assert r1["groups"] == r2["groups"]
    assert r2["stage_bytes"] == {}                    # 零发出 ⇒ 零输入字节


def test_stage_bytes_reported_on_send(sandbox, monkeypatch):
    _on(monkeypatch)
    r, calls = _run()
    assert set(r["stage_bytes"]) == {"s2a", "l1", "l2"}
    assert all(v > 0 for v in r["stage_bytes"].values())


# ================================================================ 中途失败恢复
def test_l1_failure_resume_reuses_s2a(sandbox, monkeypatch):
    """首跑 L1 全挂：S2a 已落缓存。恢复只发 L1＋L2（2 次），S2a 零出站。"""
    _on(monkeypatch)
    r1, c1 = _run(script={"l1_raise": True})
    assert not r1["ok"] and r1["stage"] == "l1" and len(c1) == 5  # s2a 1 + l1 4
    r2, c2 = _run()
    assert r2["ok"] and len(c2) == 2
    assert tl.L1_PROMPT in c2 and tl.L2_PROMPT in c2
    assert tl.S2A_PROMPT not in c2                    # S2a 命中


def test_l2_parse_exhausted_resume_sends_l2_only(sandbox, monkeypatch):
    """L2 解析耗尽（结论照常、留明状态）⇒ 恢复只发 L2 一次。"""
    _on(monkeypatch)
    r1, c1 = _run(script={"l2": "完全不可解析的输出"})
    assert r1["ok"]                                   # L2 失败只丢细节
    assert len(c1) == 6                               # s2a 1 + l1 1 + l2 4
    r2, c2 = _run()
    assert r2["ok"] and c2 == [tl.L2_PROMPT]          # 只重发 L2
    assert r2["descriptions"]                         # 这次细节回来了


def test_s2a_bad_output_not_cached(sandbox, monkeypatch):
    """S2a 解析失败 ⇒ 不落缓存；恢复后 S2a 重新出站。"""
    _on(monkeypatch)
    r1, c1 = _run(script={"s2a": '{"groups":[{"end":99}]}'})   # 越界
    assert not r1["ok"] and r1["stage"] == "s2a" and len(c1) == 2
    r2, c2 = _run()
    assert r2["ok"] and tl.S2A_PROMPT in c2           # S2a 重发


# ================================================================ 失效矩阵
def test_equal_length_edit_invalidates(sandbox, monkeypatch):
    """等长改写正文 ⇒ 全部三段重新出站（块内容指纹入键）。"""
    _on(monkeypatch)
    _run()
    edited = [dict(b) for b in BLOCKS]
    edited[2] = dict(edited[2], content="正文内容-X")   # 等长
    r, calls = _run(blocks=edited)
    assert len(calls) == 3 and r["ok"]


def test_order_change_invalidates(sandbox, monkeypatch):
    _on(monkeypatch)
    _run()
    swapped = [BLOCKS[1], BLOCKS[0]] + BLOCKS[2:]
    r, calls = _run(blocks=swapped)
    assert len(calls) == 3


def test_quarantine_change_invalidates_s2a(sandbox, monkeypatch):
    """隔离态变化 ⇒ S2a 重发（块身份含宿主字段）；L1/L2 收到的组体
    正文不变 ⇒ 依契约复用（阶段输入逐项一致）。共 1 次出站。"""
    _on(monkeypatch)
    _run()
    q = [dict(b) for b in BLOCKS]
    q[0] = dict(q[0], quarantine="pseudo_call")
    r, calls = _run(blocks=q)
    assert r["ok"] and calls == [tl.S2A_PROMPT]


def test_new_block_outside_consumed_allows_reuse(sandbox, monkeypatch):
    """窗口外追加（未被任何组覆盖）⇒ S2a 键变重发，L1/L2 实际
    输入逐项一致 ⇒ 按契约复用。共 1 次出站。"""
    _on(monkeypatch)
    _run()
    grown = BLOCKS + [{"seq": 7, "role": "assistant", "content": "新块"}]
    r, calls = _run(blocks=grown)
    assert r["ok"] and calls == [tl.S2A_PROMPT]


def test_new_block_inside_groups_invalidates_downstream(sandbox, monkeypatch):
    """新块进入组覆盖范围 ⇒ L1/L2 组体正文变 ⇒ 三段全发。"""
    _on(monkeypatch)
    _run()
    grown = (BLOCKS[:3]
             + [{"seq": 99, "role": "assistant", "content": "插队块"}]
             + BLOCKS[3:])
    r, calls = _run(blocks=grown, script={
        "s2a": '{"groups":[{"end":3},{"end":6}]}'})   # 组仍覆盖全部
    assert len(calls) == 3


def test_l1_change_invalidates_l2_only(sandbox, monkeypatch):
    """L1 产物换版 ⇒ S2a/L1 命中、L2 重发（依赖链下游失效）。"""
    _on(monkeypatch)
    _run()
    # 篡改已缓存的 L1 产物文本（模拟换版产物被复核不过——同键不同产物
    # 不会发生；改为直接验证「L1 依赖换版」：往 L1 记录里塞别的合法产物）
    from src.storage.stage_cache import _dir
    d = _dir("w6", "c6")
    l1_file = next(p for p in d.glob("stage-*.json")
                   if json.loads(p.read_text("utf-8"))["stage"] == "cond_l1")
    rec = json.loads(l1_file.read_text("utf-8"))
    rec["product"] = {"text": ("【块1】\n开始：起X\n完成：果X\n"
                               "【块2】\n开始：起Y\n完成：果Y")}
    l1_file.write_text(json.dumps(rec), "utf-8")
    r, calls = _run()
    # L1 命中（仍是合法产物）但 L1 文本变 ⇒ L2 键含 l1 指纹 ⇒ 重发
    assert calls == [tl.L2_PROMPT]
    assert any(n.get("start") == "起X" for n in r["nodes"])


# ================================================================ 损坏/半写/租户
def test_corrupt_and_half_written_records_miss(sandbox, monkeypatch):
    _on(monkeypatch)
    _run()
    from src.storage.stage_cache import _dir
    d = _dir("w6", "c6")
    files = sorted(d.glob("stage-*.json"))
    assert len(files) == 3
    files[0].write_text("{损坏", "utf-8")               # 坏 JSON
    files[1].write_text("", "utf-8")                    # 半写
    files[2].write_text("[]", "utf-8")                  # 结构错
    r, calls = _run()
    assert r["ok"] and len(calls) == 3                  # 三份全按未命中重发


def test_tenant_boundary_no_cross_hit(sandbox, monkeypatch):
    """相同文字与块号、跨 work/card ⇒ 不命中。"""
    _on(monkeypatch)
    _run()
    r, calls = _run(ctx={**CTX, "work_id": "w7"})
    assert len(calls) == 3
    r, calls = _run(ctx={**CTX, "card_id": "c7"})
    assert len(calls) == 3


def test_seat_change_invalidates(sandbox, monkeypatch):
    _on(monkeypatch)
    _run()
    r, calls = _run(ctx={**CTX, "model": "other-seat"})
    assert len(calls) == 3


def test_options_change_invalidates(sandbox, monkeypatch):
    """[返修③] 有效选项变化（如输出上限 8000→9000）⇒ 全段失效。"""
    _on(monkeypatch)
    _run()
    r, calls = _run(ctx={**CTX, "options_fp": "cap9000"})
    assert len(calls) == 3


def test_origin_visibility_change_invalidates(sandbox, monkeypatch):
    """[返修③] 原块隔离状态凭据变化（投影正文相同）⇒ S2a 失效。"""
    _on(monkeypatch)
    _run()
    r, calls = _run(ctx={**CTX, "origin_fp": "quarantine-flip"})
    assert r["ok"] and calls == [tl.S2A_PROMPT]   # L1/L2 组体输入不变 ⇒ 复用


def test_shard_stable_across_batch_renumber(sandbox, monkeypatch):
    """[返修④] shard 由窗口首末块号构成——批号重排不换键，跨批恢复
    不重复付费。"""
    _on(monkeypatch)
    _run(ctx={**CTX, "shard": "seq1-6/s0"})
    r, calls = _run(ctx={**CTX, "shard": "seq1-6/s0"})   # 同窗口同片 ⇒ 命中
    assert r["ok"] and calls == []


def test_prompt_change_invalidates(sandbox, monkeypatch):
    _on(monkeypatch)
    _run()
    monkeypatch.setattr(tl, "S2A_PROMPT", tl.S2A_PROMPT + " ")
    r, calls = _run()
    assert tl.S2A_PROMPT in calls                     # 提示换版 ⇒ s2a 重发


# ================================================================ 关态基线
def test_off_state_always_sends(sandbox, monkeypatch):
    """开关关闭 ⇒ 每次三段全发（旧路，零缓存读）。"""
    _on(monkeypatch, flag="0")
    for _ in range(2):
        r, calls = _run()
        assert r["ok"] and len(calls) == 3


# ================================================================ 缩量试点
def test_compact_l2_shrinks_input(sandbox, monkeypatch):
    """C 组：阶段复用＋缩量 ⇒ L2 输入字节减少，节点/描述一致。"""
    _on(monkeypatch)
    monkeypatch.setenv("COST_MEMORY_COMPACT_PROMPTS", "1")
    domain = "全景" + "重" * 400                        # 全景够大才有读数
    r1, _ = _run(domain=domain)
    b_small = r1["stage_bytes"]["l2"]
    monkeypatch.delenv("COST_MEMORY_COMPACT_PROMPTS")
    from src.storage.stage_cache import _dir
    import shutil
    shutil.rmtree(_dir("w6", "c6"), ignore_errors=True)
    r2, _ = _run(domain=domain)
    assert r2["stage_bytes"]["l2"] > b_small          # 缩量生效
    assert r1["nodes"] == r2["nodes"]
    # s2a/l1 不受缩量影响
    assert r1["stage_bytes"]["s2a"] == r2["stage_bytes"]["s2a"]


def test_compact_requires_stage_cache(sandbox, monkeypatch):
    """缩量只在阶段缓存开时生效。"""
    monkeypatch.setenv("COST_MEMORY_STAGE_CACHE", "0")
    monkeypatch.setenv("COST_MEMORY_COMPACT_PROMPTS", "1")
    assert not stage_cache.compact_on()


# ================================================================ 归拢组键
def test_fold_member_fingerprint_covers_content():
    """顶层 seq+gist 恒定时，成员经过/覆盖/level/子条/隔离态变化即换指纹。"""
    node = {"anchor": {"seq": 5}, "gist": "同一句话",
            "coveredSeqs": [3, 4, 5], "level": 1,
            "timeline": {"from": "a", "to": "b"}, "lower": [1]}
    base = stage_cache.member_fp(node)
    for change in ({"gist": "同一句话"},                      # 不变对照
                   {"covers": [3, 4]},                       # 覆盖变
                   {"timeline": {"from": "a", "to": "c"}},   # 时序变
                   {"level": 2},                             # 可见性变
                   {"sunk": True},                           # 隔离/沉底变
                   {"lower": [1, 9]}):                       # 子条变
        fp = stage_cache.member_fp({**node, **change})
        if change == {"gist": "同一句话"}:
            assert fp == base
        else:
            assert fp != base, change


def test_stage_key_field_sensitivity(sandbox, monkeypatch):
    """工作/卡/会话/阶段/模型座/提示/选项各自独立换键。"""
    _on(monkeypatch)
    kw = dict(work_id="w", card_id="c", session_id="s", stage="st",
              inputs={"x": 1}, model_seat="m", prompt_fp="p")
    k = stage_cache.stage_key(**kw)
    for field in ("work_id", "card_id", "session_id", "stage",
                  "model_seat", "prompt_fp"):
        assert stage_cache.stage_key(**{**kw, field: "other"}) != k, field
    assert stage_cache.stage_key(**{**kw, "inputs": {"x": 2}}) != k
    assert stage_cache.stage_key(**{**kw, "options_fp": "o"}) != k


def test_lookup_revalidate_rejects_stale_product(sandbox, monkeypatch):
    """命中但复核不过（当前成员对不上）⇒ 未命中结算。"""
    _on(monkeypatch)
    stage_cache.store_valid("w", "c", stage="s", key="k",
                            input_fp={}, product={"text": "abc"})
    assert stage_cache.lookup("w", "c", stage="s", key="k",
                              revalidate=lambda p: p["text"] == "abc")
    assert stage_cache.lookup("w", "c", stage="s", key="k",
                              revalidate=lambda p: False) is None


def test_prune_bounds_retention(sandbox, monkeypatch):
    """保留有界：同阶段最多 KEEP_PER_STAGE 份。"""
    _on(monkeypatch)
    for i in range(stage_cache.KEEP_PER_STAGE + 5):
        stage_cache.store_valid("w", "c", stage="s", key=f"k{i}",
                                input_fp={}, product={"i": i})
    from src.storage.stage_cache import _dir
    files = [p for p in _dir("w", "c").glob("stage-*.json")
             if json.loads(p.read_text("utf-8"))["stage"] == "s"]
    assert len(files) <= stage_cache.KEEP_PER_STAGE


def test_bad_sibling_file_does_not_break_new_write(sandbox, monkeypatch):
    """[返修⑤] 目录里躺着合法但非 dict 的坏文件（``[]``/半写）时，
    新产物写入与回收都不抛错、产物照样可命中。"""
    _on(monkeypatch)
    from src.storage.stage_cache import _dir
    d = _dir("w", "c")
    d.mkdir(parents=True, exist_ok=True)
    (d / "stage-oldbad.json").write_text("[]", "utf-8")
    (d / "stage-half.json").write_text('{"v":1,"key":"k"', "utf-8")
    for i in range(stage_cache.KEEP_PER_STAGE + 2):
        stage_cache.store_valid("w", "c", stage="s", key=f"k{i}",
                                input_fp={}, product={"i": i})
    hit = stage_cache.lookup("w", "c", stage="s", key="k0",
                             revalidate=lambda p: True)
    assert hit is None or hit.get("i") == 0        # 可能被回收，但不许抛错
    assert stage_cache.lookup("w", "c", stage="s", key=f"k{stage_cache.KEEP_PER_STAGE + 1}",
                              revalidate=lambda p: True) is not None


@pytest.fixture
def stage_route(sandbox, monkeypatch):
    from src.api import model_config

    _on(monkeypatch)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    model = "ds-v41-flash-eco-amr"
    pick = model_config.pick_one
    monkeypatch.setattr(model_config, "pick_one",
                        lambda role, **kw: model if role == "condense" else pick(role, **kw))
    return model


async def _seed_fold():
    from src.storage import memory_store as ms
    from tests.test_memory_retrieval import _fold_fixture

    wid, cid, entries = await _fold_fixture()
    await ms.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                    "batch_count": 8, "entries": entries})
    return wid, cid


def _fold_reply(_index, body):
    user = "\n".join(str(m.get("content") or "") for m in body["messages"] if m["role"] == "user")
    if "【展开材料】" in user:
        content = "【成员】10,20\n【标题】归拢结论\n【起因】q\n【经过】p\n【结果】r\n【教训】l"
    elif "【成员材料】" in user:
        content = "【需展开：seq 10,20】"
    else:
        assert "【因果链】" in user
        content = '结论：有合并\n{"groups":[{"label":"甲乙合","members":[10,20]}]}'
    return sse(frame_text(content), frame_finish("stop"))


def _hold_publication(monkeypatch):
    from src.storage import memory_store as ms

    save = ms.save_memory

    async def hold(*args, **kw):
        if kw.get("expected_state") is not None:
            raise ms._StaleCondenseBatch("测试暂缓提交")
        return await save(*args, **kw)

    monkeypatch.setattr(ms, "save_memory", hold)


@pytest.mark.asyncio
async def test_cached_fold_rejects_source_change_while_waiting_to_publish(stage_route, monkeypatch):
    from src.storage import card_db, memory_store as ms

    wid, cid = await _seed_fold()
    initial = ms.load_memory(wid, cid)
    probe = OutboundRecorder(_fold_reply).install(monkeypatch)
    with monkeypatch.context() as ctx:
        _hold_publication(ctx)
        await ms._maybe_fold(wid, cid)
    assert len(probe.requests) == 3

    publishing = asyncio.Event()
    original = ms.save_memory

    async def notice(*args, **kw):
        if kw.get("expected_state") is not None:
            publishing.set()
        return await original(*args, **kw)

    monkeypatch.setattr(ms, "save_memory", notice)
    async with ms._memory_lock(wid, cid):
        task = asyncio.create_task(ms._maybe_fold(wid, cid))
        await asyncio.wait_for(publishing.wait(), timeout=10)
        assert not task.done() and len(probe.requests) == 3
        card_db._write_sync(wid, lambda db: db.execute(
            "UPDATE session_blocks SET data=json_set(data, '$.ctx_content', '新原文') "
            "WHERE card_id=? AND seq=10", (cid,)))
    await asyncio.wait_for(task, timeout=10)
    result = ms.load_memory(wid, cid)
    assert result["entries"] == initial["entries"]
    assert (result.get("last_fold_check") or 0) == 0


@pytest.mark.asyncio
async def test_fold_route_change_invalidates_all_three_confirmed_stages(stage_route, monkeypatch):
    from src.api import model_service
    from src.model_gateway.gateway import build_gateway
    from src.storage import memory_store as ms

    wid, cid = await _seed_fold()
    probe = OutboundRecorder(_fold_reply).install(monkeypatch)
    with monkeypatch.context() as ctx:
        _hold_publication(ctx)
        await ms._maybe_fold(wid, cid)
    assert len(probe.requests) == 3
    cfg = deepcopy(model_service.get_gateway().config)
    cfg["models"][stage_route]["base_url"] = "https://batch6-new-fold.invalid/v1"
    monkeypatch.setattr(model_service, "_gateway", build_gateway(cfg))
    await ms._maybe_fold(wid, cid)
    assert len(probe.requests) == 6
    assert all("batch6-new-fold.invalid" in r["url"] for r in probe.requests[3:])
    assert ms.load_memory(wid, cid)["last_fold_check"] == 8
    rows = [json.loads(p.read_text("utf-8"))
            for p in stage_cache._dir(wid, cid).glob("stage-*.json")]
    assert len(rows) == 6 and len({r["origin_call"] for r in rows if r["origin_call"]}) == 6


@pytest.mark.asyncio
async def test_condense_skips_cache_when_transport_receipt_is_missing(stage_route, monkeypatch):
    from src.api import model_service
    from src.storage import memory_store as ms
    from tests.test_section3_watermark_publish import _append, _mk

    wid, cid = await _mk()
    await _append(wid, cid, ["保留原文依据"])
    stages = []

    def reply(_index, body):
        system = next(m["content"] for m in body["messages"] if m["role"] == "system")
        stage, content = {
            tl.S2A_PROMPT: ("s2a", '{"groups":[{"end":0}]}'),
            tl.L1_PROMPT: ("l1", "【块1】\n开始：任务\n经过：核对\n完成：结论"),
            tl.L2_PROMPT: ("l2", '{"l2":[{"group":1,"subtopics":[]}]}'),
        }[system]
        stages.append(stage)
        return sse(frame_text(content), frame_finish("stop"))

    OutboundRecorder(reply).install(monkeypatch)
    call_main = model_service.call_main

    async def without_s2a_proof(system, *args, **kw):
        if system == tl.S2A_PROMPT:
            kw.pop("on_transport", None)
        return await call_main(system, *args, **kw)

    with monkeypatch.context() as ctx:
        _hold_publication(ctx)
        ctx.setattr(model_service, "call_main", without_s2a_proof)
        staged = await ms._condense_one_batch(wid, cid, ms._read_origin(wid, cid), 0)
    assert staged["stage"] == "source_changed" and stages == ["s2a", "l1", "l2"]
    recovered = await ms._condense_one_batch(wid, cid, ms._read_origin(wid, cid), 0)
    assert recovered["ok"] and recovered["mark"] == 1
    assert stages == ["s2a", "l1", "l2", "s2a"]
