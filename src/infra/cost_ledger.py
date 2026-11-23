"""[批七·统一逐请求台账] 发送边界账＋端点用量对账（观测接线/离线对账/回放取证）。

数据源（**复用现有入口，不新设发送闸**）：
- 物理尝试：回放探针/宿主按 ``attempt_record()`` 形态记账——进入
  ``client.send`` 即一笔；预算/权限在发送前拒绝记 ``control``（尝试计零）。
- 端点用量：适配器原始 usage 字段原样进 ``usage_raw``，本模块只做
  分厂商归一化（``normalize_usage``）——原始值永远可查。
- 业务结果：调用方在 ``outcome`` 留宿主回执键（real_tool/receipt/
  mark/fold_cursor…）。

落盘形态：JSONL（``logs/cost_ledger-*.jsonl`` 或测试沙箱任意路径），
一行一笔尝试。``summarize()`` 重复读同一日志产出同一报表——
幂等由「attempt_id 唯一、每行独立事实」保证。

CLI：``python -m src.infra.cost_ledger report <jsonl...>`` 汇总；
``diff <a.jsonl> <b.jsonl>`` 出 A/B 任务级对照。
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

LEDGER_VERSION = 1


# ================================================================ 记账
def attempt_record(*, attempt_id: str, task: dict[str, Any],
                   seat: str = "", request_model: str = "",
                   adapter: str = "", trace_id: str = "", api_base: str = "",
                   logical_hop: Any = None, outer_retry: int = 0,
                   gateway_attempt: int = 0,
                   request_bytes: int = 0,
                   partitions: Optional[dict[str, int]] = None,
                   sent: bool = False, terminal: bool = False,
                   failure: str = "", control: str = "",
                   usage_raw: Optional[dict[str, Any]] = None,
                   outcome: Optional[dict[str, Any]] = None,
                   ts: Optional[float] = None) -> dict[str, Any]:
    """一笔物理尝试的统一记录。

    ``sent``=适配器确认发出；``terminal``=收到真实终帧；
    ``control`` 非空＝发送前拒绝（预算/权限/守卫）——此类记录
    ``request_bytes`` 计 0、不算物理尝试。"""
    return {"v": LEDGER_VERSION, "ts": float(ts or time.time()),
            "attempt": str(attempt_id), "task": dict(task or {}),
            "seat": str(seat or ""), "request_model": str(request_model or ""),
            "adapter": str(adapter or ""), "trace_id": str(trace_id or ""),
            "api_base": str(api_base or ""),
            "logical_hop": logical_hop, "outer_retry": int(outer_retry),
            "gateway_attempt": int(gateway_attempt),
            "request_bytes": int(request_bytes or 0),
            "partitions": dict(partitions or {}),
            "sent": bool(sent), "terminal": bool(terminal),
            "failure": str(failure or ""), "control": str(control or ""),
            "usage_raw": dict(usage_raw) if isinstance(usage_raw, dict) else None,
            "outcome": dict(outcome) if isinstance(outcome, dict) else None}


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _merge_attempt(old: Optional[dict[str, Any]],
                   new: dict[str, Any]) -> dict[str, Any]:
    """同一 attempt 的后续回执**合并**（补记回执/乱序到达都归并）：

    布尔位取或（sent/terminal 后到的 True 生效）；标量/字典以**非空
    后值**覆盖（usage_raw/outcome/seat/failure 补记生效）；ts 取最新。"""
    if old is None:
        return dict(new)
    m = dict(old)
    for k in ("sent", "terminal"):
        m[k] = bool(old.get(k)) or bool(new.get(k))
    for k in ("failure", "control", "seat", "request_model", "adapter",
              "trace_id", "request_model", "logical_hop", "outer_retry",
              "gateway_attempt", "request_bytes", "partitions",
              "usage_raw", "outcome", "task"):
        v = new.get(k)
        if v not in (None, "", {}, []):
            m[k] = v
    m["ts"] = max(float(old.get("ts") or 0), float(new.get("ts") or 0))
    return m


class LedgerRows(list):
    """合并后的台账行以及本次文件读取的完整性证据。"""

    def __init__(self, rows: Iterable[dict[str, Any]], health: dict[str, Any]):
        super().__init__(rows)
        self.health = health


def read_jsonl(*paths: Path | str) -> LedgerRows:
    """按尝试/事件编号归并，完整性取文件中的写入者检查点。"""
    drained = drain()
    if not paths:
        # 零路径＝读当前台账目录（drain 后 glob——后台写线程异步，
        # 调用方预 glob 可能抓到落盘前的空目录，这里统一兜住）。
        paths = tuple(sorted(Path(_ledger_path()).parent
                             .glob("cost_ledger-*.jsonl")))
    merged: dict[str, dict[str, Any]] = {}
    health: dict[str, Any] = {"complete": None, "scope": "checkpointed_input",
                              "drained": drained, "legacy_rows": 0,
                              "malformed_rows": 0, "unreadable_paths": [], "sources": []}
    incomplete = not drained
    for p in dict.fromkeys(Path(path).resolve() for path in paths):
        sequences: dict[str, set[int]] = {}
        checkpoints: dict[str, dict[str, Any]] = {}
        try:
            lines = p.read_bytes().splitlines()
        except OSError:
            health["unreadable_paths"].append(str(p))
            incomplete = True
            continue
        for n, ln in enumerate(lines, 1):
            try:
                rec = json.loads(ln)
            except (ValueError, UnicodeError):
                rec = None
            if not isinstance(rec, dict):
                health["malformed_rows"] += 1
                incomplete = True
                continue
            writer = rec.get("ledger_writer")
            seq = rec.get("ledger_seq")
            if rec.get("kind") == "ledger_checkpoint":
                through = rec.get("through")
                if (isinstance(writer, str) and writer and isinstance(through, int) and through > 0
                        and all(isinstance(rec.get(k), int) and rec[k] >= 0
                                for k in ("written", "write_failed", "dropped_full"))):
                    if through >= checkpoints.get(writer, {}).get("through", 0):
                        checkpoints[writer] = rec
                else:
                    health["malformed_rows"] += 1
                    incomplete = True
                continue
            if isinstance(writer, str) and writer and isinstance(seq, int) and seq > 0:
                sequences.setdefault(writer, set()).add(seq)
            else:
                health["legacy_rows"] += 1
            key = (f"attempt:{rec['attempt']}" if rec.get("attempt") else
                   f"event:{rec['event_id']}" if rec.get("event_id") else
                   f"line:{p}:{n}")
            merged[key] = _merge_attempt(merged.get(key), rec)
        for writer in sorted(sequences.keys() | checkpoints.keys()):
            seen = sequences.get(writer, set())
            checkpoint = checkpoints.get(writer, {})
            through = checkpoint.get("through")
            complete = bool(through and len(seen) == through and max(seen, default=0) == through
                            and checkpoint["written"] == through
                            and not (checkpoint["write_failed"] or checkpoint["dropped_full"]))
            incomplete |= not complete
            health["sources"].append({"path": str(p), "writer": writer, "rows": len(seen),
                                      "through": through, "checkpoint_ts": checkpoint.get("ts"),
                                      "write_failed": checkpoint.get("write_failed"),
                                      "dropped_full": checkpoint.get("dropped_full"),
                                      "complete": complete})
    # 跨日文件或被截掉的基准行可能只留下补记，补记本身不计物理发送。
    orphan_keys = [key for key, row in merged.items() if row.get("attempt") and "task" not in row]
    for key in orphan_keys:
        del merged[key]
    health["orphan_updates"] = len(orphan_keys)
    health["write_failed"] = sum(s["write_failed"] or 0 for s in health["sources"])
    health["dropped_full"] = sum(s["dropped_full"] or 0 for s in health["sources"])
    if incomplete or orphan_keys:
        health["complete"] = False
    elif health["sources"] and not health["legacy_rows"]:
        health["complete"] = True
    return LedgerRows(merged.values(), health)


# ================================================================ 用量归一化
_MISSING = object()          # 字段缺席（≠0）：未知如实报未知


def normalize_usage(raw: Optional[dict[str, Any]], adapter: str = "") -> dict[str, Any]:
    """端点原始 usage → 统一口径。**保留原语义**：

    - ``input_total``：端点声明的总输入（openai 系=prompt_tokens 原值，
      含缓存读）；``cache_read``/``cache_write`` 分列；
      ``net_input``＝适配器内部口径（openai 兼容＝总输入−缓存读，与
      translate.py 的 ``prompt_tokens - cached`` 同式）；
      字段缺席 ⇒ ``None``（未知），显式 0 保留为 0；
    - ``output``：完成/输出 token；``reasoning`` 独立列；
      端点已把思考计入输出时 ``output_includes_reasoning=True`` 沿总量。
    - SIWC/Responses：``input_tokens`` 即总输入（缓存是其中一项）；
      Anthropic：cache_creation_input_tokens/cache_read_input_tokens。
    """
    out = {"input_total": None, "cache_read": None, "cache_write": None,
           "net_input": None, "output": None, "reasoning": None,
           "output_includes_reasoning": False, "fields_present": []}
    if not isinstance(raw, dict):
        return out
    out["fields_present"] = sorted(k for k, v in raw.items() if v is not None)

    def _get(*names: str) -> Any:
        for n in names:
            if n in raw and raw[n] is not None:
                return raw[n]
        return None

    ad = str(adapter or "")
    if ad in ("openai_compat", "deepseek", ""):
        # wire 原值：prompt_tokens＝总输入（含缓存）；cached tokens 在
        # prompt_cache_hit_tokens 或 prompt_tokens_details.cached_tokens。
        total = _get("prompt_tokens", "input_tokens")
        details = raw.get("prompt_tokens_details") or {}
        cached = _get("prompt_cache_hit_tokens")
        if cached is None and isinstance(details, dict):
            cached = details.get("cached_tokens")
        out["input_total"] = total
        out["cache_read"] = cached
        if total is not None and cached is not None:
            out["net_input"] = int(total) - int(cached)
        elif total is not None:
            out["net_input"] = int(total)
        out["output"] = _get("completion_tokens", "output_tokens")
        cd = raw.get("completion_tokens_details") or {}
        out["reasoning"] = (cd.get("reasoning_tokens")
                            if isinstance(cd, dict) else None)
        if out["reasoning"] is None:
            out["reasoning"] = _get("reasoning_tokens")
        # deepseek/openai 系：reasoning 已计入 completion_tokens
        out["output_includes_reasoning"] = True
    elif ad in ("siwc", "responses"):
        total = _get("input_tokens")   # [返修] total_tokens 不混进输入账
        cached = _get("input_tokens_details.cached_tokens")  # 兼容平铺
        if cached is None:
            d = raw.get("input_tokens_details")
            if isinstance(d, dict):
                cached = d.get("cached_tokens")
        out["input_total"] = total   # total_tokens 不作输入账（口径不明）
        out["cache_read"] = cached
        if total is not None and cached is not None:
            out["net_input"] = int(total) - int(cached)
        elif total is not None:
            out["net_input"] = int(total)
        out["output"] = _get("output_tokens", "completion_tokens")
        d2 = raw.get("output_tokens_details")
        out["reasoning"] = (d2.get("reasoning_tokens")
                            if isinstance(d2, dict) else None)
        out["output_includes_reasoning"] = True
    elif ad == "anthropic":
        total = _get("input_tokens")
        created = _get("cache_creation_input_tokens")
        read_ = _get("cache_read_input_tokens")
        # anthropic 总输入＝input_tokens + 缓存创建 + 缓存读（三者并立）
        out["input_total"] = (int(total or 0) + int(created or 0)
                              + int(read_ or 0)) if total is not None else None
        out["cache_write"] = created
        out["cache_read"] = read_
        out["net_input"] = total
        out["output"] = _get("output_tokens")
        out["output_includes_reasoning"] = False
    else:
        out["input_total"] = _get("prompt_tokens", "input_tokens")
        out["output"] = _get("completion_tokens", "output_tokens")
        out["net_input"] = out["input_total"]
    return out


# ================================================================ 汇总
# 逐跳/逐次调用的易变字段一律从任务身份剥掉——同任务的首跳/续跳/重试/
# 换座/合成步归入同一任务账（caller/trace_id 保留在尝试行本体供关联）。
_HOP_KEYS = ("hop", "logical_hop", "round", "attempt",
             "caller", "trace_id", "outer_retry")


def _task_id(task: dict[str, Any]) -> dict[str, Any]:
    """任务身份：**去掉逐跳字段**——同一条任务的首跳/续跳/重试归入
    同一任务账（跳次另列 ``hops`` 明细，不拆任务）。"""
    return {k: v for k, v in (task or {}).items() if k not in _HOP_KEYS}


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """台账 → 统一报表（任务书 §二.2 表）。重复读同一输入产出同一结果。"""
    tasks: dict[str, dict[str, Any]] = {}
    for rec in records:
        ident = _task_id(rec.get("task") or {})
        key = json.dumps(ident, sort_keys=True, ensure_ascii=False)
        t = tasks.setdefault(key, _new_task(rec, ident))
        if str(rec.get("control") or ""):
            t["control_closures"] += 1
            # [返修·业务回执] 控制行也收 outcome/failure/seat——零出站
            # 的预算拒绝已写结果与座位，汇总不许丢。
            if rec.get("outcome"):
                t["outcomes"].append(rec["outcome"])
            if rec.get("failure"):
                t["failures"][str(rec["failure"])] = \
                    t["failures"].get(str(rec["failure"]), 0) + 1
            _s = str(rec.get("seat") or "")
            if _s and _s not in t["seats"]:
                t["seats"].append(_s)
            continue                       # 发送前拒绝：物理尝试计零
        _fold(t, rec)
    # [返修④] 字段级覆盖收尾：某用量字段在全部尝试里都未知 ⇒ 合计记
    # None（未知），不许落成 0 再被当成「缓存读为 0/省了用量」。
    for t in tasks.values():
        u = t["usage"]
        for k in ("input_total", "cache_read", "cache_write",
                  "net_input", "output", "reasoning"):
            if not u["coverage"].get(k):
                u[k] = None
    return {"v": LEDGER_VERSION,
            "tasks": [t for _k, t in sorted(tasks.items())],
            "totals": _totals(list(tasks.values())),
            # 残缺账可见：队列丢行/写失败/清账超时都在此露面——
            # complete=False 时本报表不许当完整成本。
            "ledger_health": records.health if isinstance(records, LedgerRows) else ledger_health()}


def _new_task(rec: dict[str, Any], ident: dict[str, Any]) -> dict[str, Any]:
    return {"task": ident, "hops": [],
            "attempts": 0, "sent_confirmed": 0, "terminal_frames": 0,
            "control_closures": 0,
            "failures": {}, "seats": [], "request_bytes": 0,
            "partitions": {},
            "usage": {"input_total": 0, "cache_read": 0, "cache_write": 0,
                      "net_input": 0, "output": 0, "reasoning": 0,
                      "attempts_with_usage": 0, "attempts_unknown_usage": 0,
                      "coverage": {}},   # 字段 → 有该字段已知值的尝试数
            "outcomes": []}


def _fold(t: dict[str, Any], rec: dict[str, Any]) -> None:
    if not rec.get("attempt"):
        # 无尝试编号的行＝任务级回执/失败留痕——进 outcomes/failures/
        # seats，不算物理尝试（发送前失败、零出站回执都从这里保真）。
        if rec.get("outcome"):
            t["outcomes"].append(rec["outcome"])
        if rec.get("failure"):
            t["failures"][str(rec["failure"])] = \
                t["failures"].get(str(rec["failure"]), 0) + 1
        _s = str(rec.get("seat") or "")
        if _s and _s not in t["seats"]:
            t["seats"].append(_s)
        return
    t["attempts"] += 1
    _tk = rec.get("task") or {}
    hop = _tk.get("hop", _tk.get("logical_hop", rec.get("logical_hop")))
    if hop not in (None, "") and hop not in t["hops"]:
        t["hops"].append(hop)
    if rec.get("sent"):
        t["sent_confirmed"] += 1
    if rec.get("terminal"):
        t["terminal_frames"] += 1
    if rec.get("failure"):
        t["failures"][str(rec["failure"])] = t["failures"].get(
            str(rec["failure"]), 0) + 1
    seat = str(rec.get("seat") or "")
    if seat and seat not in t["seats"]:
        t["seats"].append(seat)
    t["request_bytes"] += int(rec.get("request_bytes") or 0)
    for k, v in (rec.get("partitions") or {}).items():
        t["partitions"][k] = t["partitions"].get(k, 0) + int(v or 0)
    u = t["usage"]
    raw = rec.get("usage_raw")
    if not isinstance(raw, dict) or not raw:       # 缺席/空对象＝未知覆盖
        u["attempts_unknown_usage"] += 1
    else:
        u["attempts_with_usage"] += 1
        nu = normalize_usage(raw, str(rec.get("adapter") or ""))
        for k in ("input_total", "cache_read", "cache_write",
                  "net_input", "output", "reasoning"):
            if nu[k] is not None:
                u[k] += int(nu[k])
                u["coverage"][k] = u["coverage"].get(k, 0) + 1
    if rec.get("outcome"):
        t["outcomes"].append(rec["outcome"])


def _totals(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    out = {"tasks": len(tasks), "attempts": 0, "sent_confirmed": 0,
           "terminal_frames": 0, "control_closures": 0, "request_bytes": 0,
           "usage": {"input_total": None, "cache_read": None,
                     "cache_write": None, "net_input": None, "output": None,
                     "reasoning": None,
                     "attempts_with_usage": 0, "attempts_unknown_usage": 0}}
    for t in tasks:
        for k in ("attempts", "sent_confirmed", "terminal_frames",
                  "control_closures", "request_bytes"):
            out[k] += t[k]
        for k in ("input_total", "cache_read", "cache_write",
                  "net_input", "output", "reasoning"):
            v = t["usage"].get(k)
            if v is not None:
                out["usage"][k] = (out["usage"][k] or 0) + v
        for k in ("attempts_with_usage", "attempts_unknown_usage"):
            out["usage"][k] += t["usage"][k]
    return out


def diff(summary_a: dict[str, Any], summary_b: dict[str, Any]) -> dict[str, Any]:
    """同任务 A/B 对照：按 task 键逐项给绝对读数与差量（少发几次/几字节）。"""
    a = {json.dumps(t["task"], sort_keys=True, ensure_ascii=False): t
         for t in summary_a.get("tasks") or []}
    b = {json.dumps(t["task"], sort_keys=True, ensure_ascii=False): t
         for t in summary_b.get("tasks") or []}
    ha, hb = summary_a.get("ledger_health", {}), summary_b.get("ledger_health", {})
    complete = (False if any(h.get("complete") is False for h in (ha, hb)) else
                True if all(h.get("complete") is True for h in (ha, hb)) else None)
    rows = []
    for key in sorted(set(a) | set(b)):
        ta, tb = a.get(key), b.get(key)
        row = {"task": json.loads(key),
               "a": _brief(ta), "b": _brief(tb)}
        if ta and tb:
            row["delta"] = {k: tb[k] - ta[k] for k in
                            ("attempts", "request_bytes")}
            # [返修④·紧] 覆盖不足的**整任务**用量差收起为未知：
            # 任一侧有任意一笔未知用量尝试 ⇒ 全部用量差 None
            # （不许拿另一侧已知合计当节省）；两侧均全量覆盖后再
            # 逐字段看该字段零覆盖。
            _covered = (not ta["usage"]["attempts_unknown_usage"]
                        and not tb["usage"]["attempts_unknown_usage"]
                        and ta["usage"]["attempts_with_usage"]
                        and tb["usage"]["attempts_with_usage"])
            for k in ("input_total", "cache_read", "cache_write",
                      "net_input", "output", "reasoning"):
                # [返修·逐字段全覆盖] 差值只在两侧**每笔尝试**都报了
                # 该字段时才给（coverage[k]==attempts）——一侧某笔
                # 缺字段 ⇒ 该字段差 None，不许拿合计差当节省。
                if (_covered and ta["attempts"]
                        and ta["usage"]["coverage"].get(k) == ta["attempts"]
                        and tb["usage"]["coverage"].get(k) == tb["attempts"]):
                    row["delta"][k] = tb["usage"][k] - ta["usage"][k]
                else:
                    row["delta"][k] = None
            row["delta"]["usage_covered"] = bool(_covered)
            # [返修③] 业务结果对齐：两组做的是不是同一件事必须能看见——
            # 任一侧缺回执 ⇒ same_result=None（缺证不判同）；两侧都有
            # 回执才比结果集。
            sa = _outcome_set(ta); sb = _outcome_set(tb)
            row["business"] = {"a_outcomes": sa, "b_outcomes": sb,
                               "same_result": _compare_business(ta, tb)}
            row["comparison_eligible"] = complete is True and row["business"]["same_result"] is True
            if complete is not True and any(h.get("scope") == "checkpointed_input" for h in (ha, hb)):
                row["observed_delta"] = row["delta"]
                row["delta"] = dict.fromkeys(row["observed_delta"])
                row["delta"]["usage_covered"] = False
        rows.append(row)
    return {"v": LEDGER_VERSION, "pairs": rows,
            "ledger_health": {"a": ha, "b": hb, "complete": complete},
            "a_only": sum(1 for k in a if k not in b),
            "b_only": sum(1 for k in b if k not in a)}


def _outcome_set(t: dict[str, Any]) -> list[str]:
    out = []
    for o in t.get("outcomes") or []:
        if isinstance(o, dict):
            s = str(o.get("status") or o.get("result") or o.get("state") or "?")
        else:
            s = str(o)
        if s not in out:
            out.append(s)
    return out


def _business_evidence(task: dict[str, Any]) -> tuple[set[str], bool]:
    keys = set()
    verified = True
    for outcome in task["outcomes"]:
        if not isinstance(outcome, dict):
            verified = False
            continue
        status = outcome.get("status") or outcome.get("result") or outcome.get("state")
        if not status:
            verified = False
            continue
        effects = sorted(json.dumps(effect, sort_keys=True, ensure_ascii=False)
                         for effect in (outcome.get("effects") or []))
        keys.add(json.dumps({"status": status, "created_cards": outcome.get("created_cards"),
                             "acceptance": outcome.get("acceptance"), "effects": effects},
                            sort_keys=True, ensure_ascii=False))
        if outcome.get("kind") == "business_receipt":
            verified &= status == "verified" and outcome.get("reply_delivered") is True
        else:
            # 无结构化证据的历史回执可以显示，不能单凭同一标签判定同一业务结果。
            verified = False
    return keys, verified


def _compare_business(a: dict[str, Any], b: dict[str, Any]) -> bool | None:
    ka, va = _business_evidence(a)
    kb, vb = _business_evidence(b)
    if not ka or not kb:
        return None
    if ka != kb:
        return False
    return True if va and vb else None


def _brief(t: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if t is None:
        return None
    return {"attempts": t["attempts"], "sent_confirmed": t["sent_confirmed"],
            "terminal_frames": t["terminal_frames"],
            "request_bytes": t["request_bytes"],
            "usage": t["usage"], "seats": t["seats"],
            "hops": t["hops"], "outcomes": t["outcomes"],
            "failures": t["failures"]}


# ================================================================ 发送边界接线
# 复用点：llm/transport.send_stream（物理尝试＋实发确认）与
# gateway._stream_once（终帧/失败/原始用量）。note_* 永远向
# ``observe.LOGS_DIR/cost_ledger-*.jsonl`` 落一行（尽力而为、绝不挡主路）；
# 活动 AttemptLedger 在场时同步进内存 records（隔离回放就地取数）。
# 活动台账走 contextvar——并发的异步调用各自一本账，互不串台。
_ACTIVE: contextvars.ContextVar["AttemptLedger | None"] = \
    contextvars.ContextVar("cost_ledger_active", default=None)
# 外层调用在创建流任务前设置，流任务继承该编号，只用于观测。
OUTER_RETRY: contextvars.ContextVar[int] = contextvars.ContextVar("cost_ledger_outer_retry", default=0)
_LAST_ID: str = ""   # 无台账兜底：note_sent/note_result 找最近一笔尝试
#: 显式不绑定哨兵：回执调用方用它声明「本回执不属于任何已发尝试」
#: （同一跳内：首笔已发送、第二笔换座后发送前就失败——失败不许
#: 覆盖首笔的失败原因与座位，单独记本任务的零出站失败行）。
UNBOUND = "__unbound__"
# [返修·并发防串] 任务身份 → 本任务末笔尝试：业务回执凭任务键合账，
# 并发 A/B 各自回自己任务那笔，不再吃全局「最后一笔」。有界表（长驻
# 进程任务无限累积不许把内存拖大）——FIFO 逐出，容量内零行为变化。
_TASK_LAST_CAP = 8192
_TASK_LAST: "OrderedDict[str, str]" = OrderedDict()


def _task_key(task: dict[str, Any]) -> str:
    return json.dumps(_task_id(task or {}), sort_keys=True,
                      ensure_ascii=False)


def _ledger_path() -> Path:
    """台账文件坐标：随 observe.LOGS_DIR（沙箱 monkeypatch 即跟着走）。"""
    from . import observe
    return Path(observe.LOGS_DIR) / f"cost_ledger-{datetime.now():%Y%m%d}.jsonl"


try:
    _QUEUE_MAX = max(1, int(os.environ.get("COST_LEDGER_QUEUE_MAX") or 8192))
except ValueError:
    _QUEUE_MAX = 8192
_Q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()
_STATS_LOCK = threading.Lock()
_WRITER_ID = uuid.uuid4().hex
_STATS: dict[str, int] = {"enqueued": 0, "written": 0, "write_failed": 0,
                          "dropped_full": 0, "enqueue_failed": 0,
                          "checkpoint_failed": 0, "drain_timeouts": 0}


@dataclass
class _FileProgress:
    offered: int = 0
    written: int = 0
    write_failed: int = 0
    dropped_full: int = 0
    checkpointed: int = 0


@dataclass
class _Flush:
    through: dict[Path, int]
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False


_FILES: dict[Path, _FileProgress] = {}


def _write_checkpoints() -> None:
    """标出每个文件已结算的前缀；仍在写或遭遇丢行的前缀不能冒充完整。"""
    with _STATS_LOCK:
        ready = [(path, {"kind": "ledger_checkpoint", "ledger_writer": _WRITER_ID,
                         "through": s.offered, "written": s.written,
                         "write_failed": s.write_failed, "dropped_full": s.dropped_full,
                         "ts": time.time()})
                 for path, s in _FILES.items()
                 if s.offered != s.checkpointed
                 and s.written + s.write_failed + s.dropped_full == s.offered]
    for path, row in ready:
        try:
            append_jsonl(path, row)
        except Exception:
            with _STATS_LOCK:
                _STATS["checkpoint_failed"] += 1
        else:
            with _STATS_LOCK:
                _FILES[path].checkpointed = row["through"]


def _writer_loop(inbox: queue.Queue) -> None:
    while True:
        try:
            item = inbox.get(timeout=1.0)
        except queue.Empty:
            _write_checkpoints()
            continue
        try:
            if isinstance(item, _Flush):
                _write_checkpoints()
                with _STATS_LOCK:
                    item.ok = all(_FILES[path].checkpointed >= through
                                  for path, through in item.through.items())
                item.done.set()
                continue
            path, row = item
            try:
                append_jsonl(path, row)
            except Exception:
                with _STATS_LOCK:
                    _STATS["write_failed"] += 1
                    _FILES[path].write_failed += 1
            else:
                with _STATS_LOCK:
                    _STATS["written"] += 1
                    _FILES[path].written += 1
        finally:
            inbox.task_done()


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_writer_loop, args=(_Q,),
                                       name="cost-ledger-writer", daemon=True)
            _WORKER.start()


def _append_row(row: dict[str, Any]) -> None:
    try:
        path = _ledger_path()
        _ensure_worker()
        with _STATS_LOCK:
            p = _FILES.setdefault(path, _FileProgress())
            p.offered += 1
            tagged = {**row, "ledger_writer": _WRITER_ID, "ledger_seq": p.offered}
            try:
                _Q.put_nowait((path, tagged))
            except queue.Full:
                p.dropped_full += 1
                _STATS["dropped_full"] += 1
            else:
                _STATS["enqueued"] += 1
    except Exception:
        with _STATS_LOCK:
            _STATS["enqueue_failed"] += 1


def drain(timeout: float = 5.0) -> bool:
    """等待此前入队的账目及其检查点；报告进程没有待写记录时直接返回。"""
    with _STATS_LOCK:
        pending = {path: p.offered for path, p in _FILES.items() if p.offered > p.checkpointed}
    if not pending:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    flush = _Flush(pending)
    try:
        _Q.put(flush, timeout=max(0.0, deadline - time.monotonic()))
    except queue.Full:
        pass
    if flush.done.wait(max(0.0, deadline - time.monotonic())):
        return flush.ok
    with _STATS_LOCK:
        _STATS["drain_timeouts"] += 1
    return False


def ledger_health() -> dict[str, Any]:
    """台账健康读数：队列深度/落盘/丢行/写失败/清账超时。
    ``complete=False`` 时本账为残缺成本，不许当完整成本用。"""
    with _STATS_LOCK:
        s = dict(_STATS)
        s["queue_depth"] = _Q.qsize()
        s["queue_max"] = _Q.maxsize
        s["scope"] = "process"
        s["complete"] = not (s["dropped_full"] or s["write_failed"] or s["enqueue_failed"]
                             or s["drain_timeouts"]
                             or any(p.offered != p.checkpointed for p in _FILES.values()))
    return s


def _new_attempt_id(prefix: str = "att") -> str:
    """全局唯一——多个台账上下文同写一份文件时也不撞号。"""
    return f"{prefix}-{uuid.uuid4().hex}"


class AttemptLedger:
    """一本活台账：``with AttemptLedger(task=…)`` 期间发送边界自动入账。

    真实路径由 gateway.call/stream_call 按跳开本账（work/card/caller/trace
    为任务身份）；隔离回放可在更外层开总账——内层账只影响内存归集，
    文件行两边都写，离线 ``read_jsonl`` 合并视图一致。
    """

    _MISSING = object()

    def __init__(self, *, task: Optional[dict] = None) -> None:
        self.task = dict(task or {})
        self.records: list[dict[str, Any]] = []
        self._prev: Any = self._MISSING

    def open(self) -> "AttemptLedger":
        # 不用 token reset——异步生成器的开/关账可能落在不同 Context，
        # token 跨 Context 复位会炸；记前值、退出时 set 回，天然兼容嵌套账。
        self._prev = _ACTIVE.get()
        if self._prev is not None:
            # 嵌套账（路由任务级账内再开跳级账）：内层继承外层任务字段，
            # 自己的键覆盖——尝试行携带完整任务身份，合账不断链。
            self.task = {**self._prev.task, **self.task}
        _ACTIVE.set(self)
        return self

    def close(self) -> None:
        if self._prev is not self._MISSING:
            _ACTIVE.set(self._prev)
            self._prev = self._MISSING

    def __enter__(self) -> "AttemptLedger":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def summarize(self) -> dict[str, Any]:
        return summarize(self.records)


def _register_task_last(task: dict[str, Any], aid: str) -> None:
    key = _task_key(task)
    _TASK_LAST.pop(key, None)
    _TASK_LAST[key] = aid
    while len(_TASK_LAST) > _TASK_LAST_CAP:
        _TASK_LAST.popitem(last=False)


def _led_of(ledger: "AttemptLedger | None") -> "AttemptLedger | None":
    """显式台账优先，否则 contextvar——调用方链上有显式账时
    一律带 ``ledger=``（被 raced-anext 驱动的生成器里 var 不可见）。"""
    return ledger if ledger is not None else _ACTIVE.get()


def note_attempt(ledger: "AttemptLedger | None" = None, **kw: Any) -> str:
    """进入适配器实际发送调用＝一笔物理尝试（client.send 边界）。

    **返回本次 attempt_id**——发送边界把它交给调用方，回执凭它补记，
    并发收口的确认/终帧/失败不串到别人的尝试上。"""
    global _LAST_ID
    led = _led_of(ledger)
    task = dict(led.task) if led is not None else {}
    kw.setdefault("logical_hop", task.get("logical_hop"))
    kw.setdefault("outer_retry", task.get("outer_retry") or 0)
    rec = attempt_record(attempt_id=_new_attempt_id(), task=task, **kw)
    _LAST_ID = rec["attempt"]
    _register_task_last(rec.get("task") or {}, rec["attempt"])
    if led is not None:
        led.records.append(rec)
    _append_row(rec)
    return rec["attempt"]


def _find_rec(led: "AttemptLedger | None", aid: str) -> int:
    """在活动台账里按 attempt_id 找记录下标（从尾找，回执常跟最近一笔）。"""
    if led is None or not aid:
        return -1
    for i in range(len(led.records) - 1, -1, -1):
        if led.records[i].get("attempt") == aid:
            return i
    return -1


def _resolve_attempt(led: "AttemptLedger | None",
                     attempt: Optional[str]) -> tuple[str, int]:
    """回执归属：**显式 attempt_id 优先**；缺省回落本账最近一笔，
    再无账才吃全局末笔（仅兜底，并发路径应一律显式传号）。"""
    aid = str(attempt or "")
    if aid:
        return aid, _find_rec(led, aid)
    if led is not None:
        if led.records:
            return str(led.records[-1].get("attempt") or ""), \
                len(led.records) - 1
        return "", -1          # 本账零笔——不吃别家任务的全局末笔（串账防线）
    return _LAST_ID, -1        # 完全无账（旧回放路径）才吃全局兜底


def _note_update(kw: dict[str, Any], attempt: Optional[str] = None,
                 ledger: "AttemptLedger | None" = None) -> None:
    led = _led_of(ledger)
    if attempt == UNBOUND:
        # 显式不绑定：本回执不属于任何已发尝试（发送前失败）——
        # 按本账任务身份落无尝试行，不回退覆盖本账末笔。
        row = {"event_id": _new_attempt_id("preflight"),
               "task": dict(led.task) if led is not None else {},
               **kw, "ts": time.time()}
        if led is not None:
            led.records.append(dict(row))
        _append_row(row)
        return
    aid, idx = _resolve_attempt(led, attempt)
    if idx >= 0 and led is not None:
        led.records[idx] = _merge_attempt(led.records[idx], kw)
    if aid:
        _append_row({"attempt": aid, **kw})
    elif led is not None:
        # 本账零笔、又无显式编号——不落别家任务的全局末笔编号（串账
        # 防线）：按本账任务身份落无尝试行，_fold 归到本任务 failures/
        # outcomes、不计物理尝试。
        row = {"event_id": _new_attempt_id("preflight"),
               "task": dict(led.task), **kw, "ts": time.time()}
        led.records.append(dict(row))
        _append_row(row)


def note_sent(attempt: Optional[str] = None,
              ledger: "AttemptLedger | None" = None, **kw: Any) -> None:
    """实发确认：请求体已交给传输层（响应头到手同样证明已发）。
    ``attempt``＝发送边界发还的编号——并发场景必须显式传。"""
    _note_update({**kw, "sent": True}, attempt, ledger)


def note_result(attempt: Optional[str] = None,
                ledger: "AttemptLedger | None" = None, **kw: Any) -> None:
    """真实终帧/失败类别/端点声明模型/原始用量——补记进**指定**尝试。
    ``attempt`` 缺省才吃本账末笔（顺序单跳路径），并发必须显式传。"""
    _note_update(kw, attempt, ledger)


def note_control(control: str,
                 ledger: "AttemptLedger | None" = None, **kw: Any) -> None:
    """发送前拒绝（预算/权限/守卫）：独立控制行，物理尝试计零。"""
    global _LAST_ID
    led = _led_of(ledger)
    rec = attempt_record(attempt_id=_new_attempt_id("ctl"),
                         task=dict(led.task) if led is not None else {},
                         control=control, sent=False, terminal=False,
                         request_bytes=0, **kw)
    _LAST_ID = rec["attempt"]
    _register_task_last(rec.get("task") or {}, rec["attempt"])
    if led is not None:
        led.records.append(rec)
    _append_row(rec)


def note_outcome(*, task: dict[str, Any] | None = None, **kw: Any) -> None:
    """正式收口按任务单独记账；旧调用可继续用活动账本挂回执。"""
    if task is not None:
        _append_row({"event_id": _new_attempt_id("outcome"), "task": _task_id(task),
               "outcome": dict(kw), "ts": time.time()})
        return
    led = _ACTIVE.get()
    task = dict(led.task) if led is not None else {}
    aid = _TASK_LAST.get(_task_key(task), "")
    idx = _find_rec(led, aid)
    if idx >= 0 and led is not None:
        led.records[idx] = _merge_attempt(
            led.records[idx], {"outcome": dict(kw)})
    if aid:
        _append_row({"attempt": aid, "outcome": dict(kw)})
    else:
        # 零出站也要保住业务回执——独立任务行（_fold 只收 outcomes，
        # 不计物理尝试）。
        _append_row({"event_id": _new_attempt_id("outcome"), "task": task,
               "outcome": dict(kw), "ts": time.time()})


# ================================================================ CLI
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cost_ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="台账 JSONL → 汇总报表（幂等）")
    rp.add_argument("files", nargs="+")
    dp = sub.add_parser("diff", help="A/B 台账对照")
    dp.add_argument("a"); dp.add_argument("b")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        print(json.dumps(summarize(read_jsonl(*args.files)),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(diff(summarize(read_jsonl(args.a)),
                              summarize(read_jsonl(args.b))),
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
