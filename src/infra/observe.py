# -*- coding: utf-8 -*-
"""观测打点（F22 · 2026-10-31 移植修复方案「观测重建」——比原版 monitor.ts 更精细）。

**新建，不是改**：移植版观测约 0%（对比文档 §三），且已知「全日志断链」——
gateway 调用不记调用方/trace_id，model_call_success 与调用方侧无因果链对不上。
本模块把四类事件落成 **JSONL**（`logs/observe-YYYYMMDD.jsonl`，一行一个 JSON）：

| kind | 时机 | 关键字段 |
|---|---|---|
| `model_call` | 每次模型调用（model_service.call_main / 网关壳） | trace_id / caller / card / model / prompt_tokens / completion_tokens / latency_ms / finish_reason |
| `condense_trigger` | 回合末/预检触发判定 | work / card / block_tokens / request_tokens / threshold / triggered |
| `condense_batch` | 凝练每批（原版 recordCondenseBatch 六口径） | batch_no / budget / window_seqs / consumed / new_mark / node_count |
| `fold` | 归拢提交 | merges / members / mark / violations |
| `reclaim` | 时效回收 | sunk_total / newly_sunk |
| `regions` | 上下文区装配体积 | regions=[{key,order,bytes,entries}] |

纪律：
- **打点永不阻断业务**：全部写入包 try/except，失败只在本模块留痕一次；
- 不复制正文（只有指针/数字/指纹级字段）——prompt 正文审计仍归 prompt_log；
- 每次写都 open-append-close（无长持句柄），多进程写同一文件可容忍交错
  （JSONL 每行独立，读侧按行容错）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("infra.observe")

BASE_DIR = Path(__file__).resolve().parents[2]
LOGS_DIR = BASE_DIR / "logs"

# 进程内防抖：目录建过一次就不再试；写失败只告警一次（防每调用刷屏）
_dir_ready = False
_warned = False
_lock = threading.Lock()


def _logfile() -> Path:
    return LOGS_DIR / f"observe-{datetime.now():%Y%m%d}.jsonl"


def _write(record: dict[str, Any]) -> None:
    """append 一行 JSON。任何失败静默（观测永不阻断业务），只首次告警。"""
    global _dir_ready, _warned
    try:
        if not _dir_ready:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            _dir_ready = True
        record = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with open(_logfile(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        if not _warned:
            _warned = True
            logger.warning("[observe] 打点写失败（后续静默）: %s", exc)


def log_model_call(*, trace_id: str = "", caller: str = "",
                   work_id: str = "", card_id: str = "", seat: str = "",
                   model: str = "", prompt_tokens: int | None = None,
                   completion_tokens: int | None = None,
                   tokens_used: int | None = None, reasoning_chars: int | None = None,
                   latency_ms: int | None = None, finish_reason: str = "",
                   text_len: int | None = None, tool_calls: int | None = None,
                   error: str = "") -> None:
    """每次模型调用一条（原版 recordUsage 同位；字段比原版多 caller/trace 对账键）。"""
    _write({"kind": "model_call", "trace_id": str(trace_id or ""),
            "caller": str(caller or ""), "work_id": str(work_id or ""),
            "card_id": str(card_id or ""), "seat": str(seat or ""),
            "model": str(model or ""), "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "tokens_used": tokens_used,
            "reasoning_chars": reasoning_chars, "latency_ms": latency_ms,
            "finish_reason": str(finish_reason or ""), "text_len": text_len,
            "tool_calls": tool_calls, "error": str(error or "")[:300]})


def log_condense_trigger(*, work_id: str, card_id: str, block_tokens: int = 0,
                         request_tokens: int | None = None, threshold: int = 0,
                         triggered: bool = False, source: str = "",
                         pending_blocks: int = 0, mark: int = 0) -> None:
    """触发判定数字（原版 recordCondenseDebug 同位）：谁、多大、过没过线。"""
    _write({"kind": "condense_trigger", "work_id": work_id, "card_id": card_id,
            "block_tokens": block_tokens, "request_tokens": request_tokens,
            "threshold": threshold, "triggered": bool(triggered),
            "source": str(source or ""), "pending_blocks": pending_blocks,
            "mark": mark})


def log_condense_batch(*, work_id: str, card_id: str, batch_no: int, budget: int,
                       window_seqs: int, consumed: int, new_mark: int,
                       node_count: int, retries: int = 0, stage: str = "done",
                       duration_ms: int | None = None) -> None:
    """凝练每批六口径（原版 recordCondenseBatch 同位）：
    batchNo / budget / windowSeqs / consumed / newMark / nodeCount（+retries）。"""
    _write({"kind": "condense_batch", "work_id": work_id, "card_id": card_id,
            "batch_no": batch_no, "budget": budget, "window_seqs": window_seqs,
            "consumed": consumed, "new_mark": new_mark, "node_count": node_count,
            "retries": retries, "stage": str(stage or ""),
            "duration_ms": duration_ms})


def log_fold(*, work_id: str, card_id: str, merges: int, members: int, mark: int,
             batch_count: int = 0, violations: int = 0) -> None:
    """归拢提交动作（原版 recordDepth 同位）。"""
    _write({"kind": "fold", "work_id": work_id, "card_id": card_id,
            "merges": merges, "members": members, "mark": mark,
            "batch_count": batch_count, "violations": violations})


def log_reclaim(*, work_id: str, card_id: str, sunk_total: int, newly_sunk: int) -> None:
    """时效回收动作（原版 reclaim 打点同位）。"""
    _write({"kind": "reclaim", "work_id": work_id, "card_id": card_id,
            "sunk_total": sunk_total, "newly_sunk": newly_sunk})


def log_regions(*, work_id: str = "", card_id: str = "",
                regions: list[dict[str, Any]] | None = None) -> None:
    """区装配体积：每区 key/order/bytes/entries——memory 区与 system 总量的账。"""
    _write({"kind": "regions", "work_id": str(work_id or ""), "card_id": str(card_id or ""),
            "regions": [
                {"key": str(r.get("key") or ""), "order": r.get("order"),
                 "bytes": len(str(r.get("payload") or "").encode("utf-8"))
                 if not isinstance(r.get("payload"), (list, dict)) else
                 len(json.dumps(r.get("payload"), ensure_ascii=False, default=str).encode("utf-8")),
                 "entries": r.get("entries")}
                for r in (regions or []) if isinstance(r, dict)]})


def new_trace_id() -> str:
    """短 trace id（进程内唯一即可：时间戳＋线程序）。"""
    return f"tr-{int(time.time() * 1000):x}-{os.getpid():x}-{id(threading.current_thread()) % 0xffff:x}"


# ===========================================================================
# [2026-10-31 · 用户问「你对模型调用的状态没有任何监控吗？」] 在途调用台账。
# 纯**事实账**：谁在跑、什么时候开始的、收了多少帧、多久没吐帧——
# 「是不是停了，看状态就知道」的字面落实。**不做任何裁决**（不杀、不放、不判死）；
# 消费方（单飞自愈/人）自己看账决定。
# ===========================================================================

_INFLIGHT: dict[str, dict[str, Any]] = {}


def call_begin(trace_id: str, caller: str = "", work_id: str = "", card_id: str = "",
               seat: str = "", model: str = "") -> None:
    """调用入账（gateway 发起时）。"""
    import time as _t
    rec = {"trace_id": str(trace_id), "caller": str(caller or ""),
           "work_id": str(work_id or ""), "card_id": str(card_id or ""),
           "seat": str(seat or ""), "model": str(model or ""),
           "started": _t.time(), "chunks": 0, "last_chunk": _t.time()}
    with _lock:
        _INFLIGHT[str(trace_id)] = rec
    _write({"kind": "call_begin", **{k: rec[k] for k in
            ("trace_id", "caller", "work_id", "card_id", "seat", "model")}})


def call_chunk(trace_id: str) -> None:
    """每收一帧记一笔（内存账；不落 JSONL——逐帧落盘太吵，状态看 inflight()）。"""
    import time as _t
    with _lock:
        rec = _INFLIGHT.get(str(trace_id))
        if rec is not None:
            rec["chunks"] += 1
            rec["last_chunk"] = _t.time()


def call_end(trace_id: str) -> None:
    """调用出账（终态——成功/失败都出；终态明细由 model_call 行承载）。"""
    with _lock:
        _INFLIGHT.pop(str(trace_id), None)


def inflight() -> list[dict[str, Any]]:
    """此刻在途的模型调用（带 age 与 no_chunk_for 两个读数）。"""
    import time as _t
    now = _t.time()
    with _lock:
        out = []
        for rec in _INFLIGHT.values():
            r = dict(rec)
            r["age"] = round(now - rec["started"], 1)
            r["no_chunk_for"] = round(now - rec["last_chunk"], 1)
            out.append(r)
    return sorted(out, key=lambda r: r["started"])
