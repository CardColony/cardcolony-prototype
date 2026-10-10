# -*- coding: utf-8 -*-
"""[信息流重出 Task4] 逐字流与既有 17 类的隔离性回归（V-T4-2）。

判据（机械可判定）：
1. 事件本体论 = 19 类 = 17 原 + 2 逐字流，且新增两类确在 EVENT_TYPES 内；
2. **seq 单一取号**（[A5/T097]）：主通道与逐字流共用同一分配者，全量 seq 无重号；
3. 隔离：主事件写不进逐字流 journal，逐字流也写不进主 journal；
4. 逐字流不被过程卡的 300 条环形挤出（长回复不冲掉过程卡，反之亦然）；
5. [A5/T092/T094] journal 清零制退役——run_start/round_start 不再清逐字流，
   改落 ``data.segment`` 分段标记帧（旧段保留，前端按 type 判界呈现等价）；
6. 逐字流不污染 /progress 的 stage 语义（只心跳 ts），且终止态不被心跳复活；
7. 零真实 key、零网络、零 works 触碰；不 import main。
"""
from __future__ import annotations

from src.api import event_stream as es
from src.api import progress

W, C = "work_x", "card_x"


def _reset():
    es._journals.clear()
    es._subscribers.clear()
    es._seq_counters.clear()
    es._token_journals.clear()
    es._token_subscribers.clear()
    es._anchors.clear()          # [A5/T093] 回合锚登记表
    progress._store.clear()


def _texts(kind: str = "content_chunk") -> str:
    """逐字流 journal 里某类增量的拼接（滤掉轮界标记帧）。"""
    return "".join(e["data"].get("text", "")
                   for e in es.token_journal_of(W, C) if e["type"] == kind)


def _marker_count() -> int:
    """[A5/T094] 分段标记帧计数（data.segment；``_boundary`` 假帧已退役）。"""
    return sum(1 for e in es.token_journal_of(W, C)
               if (e.get("data") or {}).get("segment"))


# ---------- 1：19 类 ----------

def test_event_ontology_is_19_types():
    assert len(es.EVENT_TYPES) == 19, len(es.EVENT_TYPES)
    assert len(es.TOKEN_EVENT_TYPES) == 2
    assert set(es.TOKEN_EVENT_TYPES) <= set(es.EVENT_TYPES)
    legacy = [t for t in es.EVENT_TYPES if t not in es.TOKEN_EVENT_TYPES]
    assert len(legacy) == 17, len(legacy)


# ---------- 2/3：seq 不混合 + 双向隔离 ----------

def test_sequences_share_single_allocator():
    """[A5/T097] seq 单一取号：主通道与逐字流共用同一分配者，帧仅携带。

    旧红线「两路独立计数、互不占用」随分段制退役（设计输入 4.6②）；新判据：
    两路 journal 全量 seq 并集无重号、从 1 连续铺满——同一分配空间。
    客户端凭 seq 交织两路（T093）。
    """
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "tool_call", {"tool": "read_card"})
    es.emit_token(W, C, "content_chunk", "正")
    es.emit_token(W, C, "reasoning_chunk", "思")
    es.emit_token(W, C, "content_chunk", "文")

    main_seqs = [e["seq"] for e in es.journal_of(W, C)]
    token_seqs = [e["seq"] for e in es.token_journal_of(W, C)]
    # 主 2 帧（run_start＋tool_call）、逐字流 4 帧（run 段标记＋3 增量）
    assert len(main_seqs) == 2 and len(token_seqs) == 4
    all_seqs = sorted(main_seqs + token_seqs)
    assert len(all_seqs) == len(set(all_seqs)), "共用分配者不得重号"
    assert all_seqs == list(range(1, 7)), all_seqs  # 1..6 连续铺满＝单一分配空间
    assert len(es.journal_of(W, C)) == 2                # 逐字流没混进主 journal
    assert _texts() == "正文"
    assert _texts("reasoning_chunk") == "思"


# ---------- 4：环形互不挤出 ----------

def test_token_stream_not_evicted_by_process_events():
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit_token(W, C, "content_chunk", "开头")
    # 灌超过过程卡上限（300）的主事件
    for i in range(400):
        es.emit(W, C, "tool_result", {"i": i})
    tokens = es.token_journal_of(W, C)
    assert _texts() == "开头", "逐字流不应被主事件挤出"
    assert len(tokens) == 2, "边界标记 + 1 条增量"


def test_process_card_not_evicted_by_token_stream():
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "tool_call", {"tool": "read_card"})
    # 灌远超旧逐字流上限(5000)的逐字流（[对话流渲染 T4] 该环形已拆，此处验主通道不被挤）
    for i in range(5200):
        es.emit_token(W, C, "content_chunk", "x")
    main = es.journal_of(W, C)
    assert len(main) == 2, "过程卡不应被逐字流挤出"
    assert main[1]["type"] == "tool_call"


# ---------- 5：run_start 清逐字流、seq 计数器不清 ----------

def test_run_start_opens_segment_and_keeps_old_tokens():
    """[A5/T092] run_start 清零制退役：逐字流旧段保留，改落分段标记帧。

    旧判据「新一轮不该带出上一轮正文」是清零制语义，判据反转——后端
    journal 全量保留（前端按 type 判界清屏，呈现等价、消费面零改动 T096）。
    帧附 (pair_id, seq, turn) 三锚（T093）。
    """
    _reset()
    es.emit(W, C, "run_start", {"pair_id": "p1"})
    es.emit_token(W, C, "content_chunk", "上一轮")
    assert _texts() == "上一轮"

    es.emit(W, C, "run_start", {"pair_id": "p2"})       # 新回合
    assert _texts() == "上一轮", "清零已退役：旧段必须保留（T092）"
    # 新回合在逐字流落一枚 run 段标记帧（type 沿用，data.segment 语义 T094/T095）
    segs = [e for e in es.token_journal_of(W, C)
            if (e.get("data") or {}).get("segment")]
    assert [s["data"]["segment"] for s in segs] == ["run", "run"]
    assert segs[-1]["pair_id"] == "p2", "标记帧携带回合锚（T093）"

    nxt = es.emit_token(W, C, "content_chunk", "新一轮")
    assert nxt["seq"] > segs[-1]["seq"], "seq 单一取号继续递增（T097）"
    assert nxt["pair_id"] == "p2", "增量帧锚随 run_start 就地更新（T093）"


# ---------- 6：不污染 progress stage 语义 ----------

def test_tokens_heartbeat_progress_without_faking_stage():
    _reset()
    es.emit(W, C, "run_start", {"detail": "d"})          # set_stage → started
    before = progress.get_stage(W, C)
    assert before["stage"] == "started" and before["active"] is True

    es.emit_token(W, C, "content_chunk", "正文")          # 只心跳
    after = progress.get_stage(W, C)
    assert after["stage"] == "started", "逐字流不得改写阶段语义"
    assert after["active"] is True
    assert after["ts"] >= before["ts"], "心跳应刷新 ts（长请求不 stale）"


def test_terminal_state_is_not_revived_by_heartbeat():
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "done", {})
    st = progress.get_stage(W, C)
    assert st["active"] is False

    es.emit_token(W, C, "content_chunk", "收尾后的残余")
    assert progress.get_stage(W, C)["active"] is False, "终止态不得被心跳复活"


def test_oversized_token_frame_kept_whole():
    """[对话流渲染 T4] 单帧 8KB 截断已拆除（丢字闸）——超长一帧必须逐字完整。

    原用例编码的是旧闸行为（丢后半但不算丢帧）；无闸裁定后判据反转为「完整保留」。
    """
    _reset()
    es.emit(W, C, "run_start", {})
    big = "长" * 20000
    evt = es.emit_token(W, C, "content_chunk", big)
    assert "truncated" not in evt["data"], "不得再有截断标记"
    assert "_preview" not in evt["data"]
    assert evt["data"]["text"] == big, "一帧的字必须逐字完整"
    assert _texts() == big


def test_no_max_token_events_ring_pop():
    """[对话流渲染 T4] 5000 条环形弹出已拆除——超出旧上限的早期增量不得被丢掉。"""
    _reset()
    es.emit(W, C, "run_start", {})
    for _ in range(5200):
        es.emit_token(W, C, "content_chunk", "x")
    assert _texts() == "x" * 5200, "旧上限(5000)之外的早期帧不得被环形弹出丢弃"


def test_no_max_events_ring_pop():
    """[对话流渲染 T4] 主通道 300 条环形弹出同属丢步骤闸，一并退场。"""
    _reset()
    es.emit(W, C, "run_start", {})
    for i in range(400):
        es.emit(W, C, "tool_result", {"i": i})
    main = es.journal_of(W, C)
    assert len(main) == 401, "主通道不得再按 300 条环形弹出丢最前步骤"
    assert main[1]["data"]["i"] == 0, "最早的一条步骤仍在"


def test_queue_full_is_not_silently_dropped(monkeypatch):
    """[对话流渲染 T4] 订阅队列满：不得静默吞——丢最旧中间帧、保最新帧、打警告。"""
    import asyncio as _aio

    _reset()
    es.emit(W, C, "run_start", {})
    key = es._key(W, C)
    q: "_aio.Queue" = _aio.Queue(maxsize=2)
    es._token_subscribers.setdefault(key, set()).add(q)

    warnings: list[str] = []
    monkeypatch.setattr(es.logger, "warning", lambda *a, **k: warnings.append(a[0] % a[1:]))

    for i in range(4):
        es.emit_token(W, C, "content_chunk", str(i))

    # 队列仍满额（2），且保有**最新**帧（最后落的那条），最旧的被弹出
    assert q.qsize() == 2, q.qsize()
    last_seq = es.token_journal_of(W, C)[-1]["seq"]
    kept = [q.get_nowait(), q.get_nowait()]
    assert max(e["seq"] for e in kept) == last_seq, "必须保住最新 seq 游标"
    assert any("订阅队列满" in w for w in warnings), f"须打警告留痕: {warnings}"


def test_no_patch_frames_on_holes():
    """[对话流渲染 T4/R-01] journal 空洞为正常态——后端不补任何补丁帧。"""
    _reset()
    es.emit(W, C, "run_start", {})
    for i in range(5):
        es.emit_token(W, C, "content_chunk", str(i))
    seqs = [e["seq"] for e in es.token_journal_of(W, C)]
    assert seqs, "journal 非空"
    # 无补丁帧：每个 seq 只出现一次、严格递增
    assert len(seqs) == len(set(seqs)), "seq 不得重复（补丁帧特征）"
    assert seqs == sorted(seqs), "seq 必须单调"


def test_empty_text_is_not_emitted():
    _reset()
    es.emit(W, C, "run_start", {})
    assert es.emit_token(W, C, "content_chunk", "") == {}
    assert _texts() == "", "空文本不应产出增量帧"


def test_unknown_token_type_is_rejected():
    _reset()
    assert es.emit_token(W, C, "tool_call", "x") == {}   # 不属逐字流 → 拒收
    assert es.emit_token(W, C, "unknown_thing", "x") == {}


# ---------- 轮次边界（工具循环多轮） ----------

def test_round_start_emits_segment_marker_and_keeps_journal():
    """[A5/T092/T094/T095] round_start 不再清逐字流——分段标记帧承载轮界。

    旧判据「新轮清空＋一枚轮界标记帧」随清零制退役；新判据：旧段保留、
    每次轮界落一枚 ``data.segment`` 标记帧（type 沿用 run_start/round_start，
    前端按 type 判界、呈现等价、消费面零改动 T096）。
    """
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "round_start", {"round": 1, "phase": "main"})
    es.emit_token(W, C, "content_chunk", "第1轮说")
    es.emit_token(W, C, "content_chunk", "……")

    es.emit(W, C, "round_start", {"round": 2, "phase": "synthesize"})
    # T092 清零退役：旧段保留
    assert _texts() == "第1轮说……"
    # T094/T095：分段标记帧——run 段 1 枚 + round 段 2 枚
    assert _marker_count() == 3, _marker_count()
    markers = [e for e in es.token_journal_of(W, C)
               if (e.get("data") or {}).get("segment")]
    assert all(m["type"] in ("run_start", "round_start") for m in markers)
    assert markers[-1]["data"]["segment"] == "round"
    assert markers[-1]["data"]["round"] == 2
    assert markers[-1]["turn"] == 2, "轮锚随 round_start 就地更新（T093）"

    es.emit_token(W, C, "content_chunk", "最终回复")
    assert _texts() == "第1轮说……最终回复", "journal 全量保留（屏由前端按 type 清）"


def test_boundary_marker_reuses_existing_ontology_type():
    """轮界帧复用已有 type，不新增第 20 类（红线4：19 类）。"""
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "round_start", {"round": 1})
    types = {e["type"] for e in es.token_journal_of(W, C)}
    assert types <= set(es.EVENT_TYPES)
    assert "round_start" in types
    assert len(es.EVENT_TYPES) == 19, "本体论仍须为 19 类"


def test_seq_counter_never_resets_across_rounds():
    """前端靠标记帧判界；seq 同时只能增不能清（长连游标安全）。"""
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "round_start", {"round": 1})
    a = es.emit_token(W, C, "content_chunk", "a")["seq"]
    es.emit(W, C, "round_start", {"round": 2})
    b = es.emit_token(W, C, "content_chunk", "b")["seq"]
    assert b > a, f"seq 必须继续递增（{a} -> {b}）"


def test_progress_stays_active_across_rounds():
    _reset()
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "round_start", {"round": 1})
    es.emit_token(W, C, "content_chunk", "a")
    assert progress.get_stage(W, C)["active"] is True
    es.emit(W, C, "round_start", {"round": 2})
    assert progress.get_stage(W, C)["active"] is True