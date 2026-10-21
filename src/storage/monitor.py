# -*- coding: utf-8 -*-
"""用量监控（照抄 dsh `gen3/lifecycle/monitor.ts` 的账本部分）。

## 它治什么

记录**每次模型调用的真实上下文长度**（`promptTokens`，即输入侧实际 token 数）——
这是"两套机制谁更省"的**主判据**。输出全是 **jsonl 追加**，不参与任何决策。

## 照抄的字段（`UsageEntry`）

`sessionId / sessionCwd / mechanismTag / ts / callSeq / model / promptTokens /
completionTokens / textLen / cacheReadTokens / cacheWriteTokens / reasoningTokens /
shadowOldTokens / shadowNewTokens`

- `callSeq`＝**每会话累计调用序号**（1 起），由 `record_usage` 分配；
- **影子对照**：同一轮额外估算"老版（无 memory 装配）"的 token，与新版同轮入日志，
  供对比"memory 省了多少"。**纯计算、零干扰**（不读/不改会话与装配）；
- token 估算＝`字符数 / 4`（dsh 同值，仅作量尺，非精确计费）。

## 与 dsh 的差异（如实）

- dsh 的 `mechanismTag` 来自 `split`（新/旧机制流量闸）——本项目无旧机制并存 ⇒ 恒 `'new'`；
- dsh 的 `recordShadow` 要扫平台 session 事件算"老版文本"——本项目改由调用方传 `old_text`，
  不在此模块里认识平台结构（依赖倒置）。

## 失败语义

**打点失败只告警、绝不抛**——观测不得拖垮主链路（dsh 同精神）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from . import obs_paths

logger = logging.getLogger("storage.monitor")

#: token 估算：字符/4（dsh 同值，仅作量尺）
TOKEN_CHARS = 4

#: 账本文件（`obs/monitor/<name>.jsonl`）
USAGE_NAME = "usage"
CONDENSE_TRACE_NAME = "condense_trace"
DEPTH_NAME = "depth"


def estimate_tokens(text: str) -> int:
    """字符数 / 4（向上取整）。"""
    if not text:
        return 0
    return -(-len(str(text)) // TOKEN_CHARS)


# ---------------------------------------------------------------------------
# 写出口（可注入——测试用内存 sink，生产用文件）
# ---------------------------------------------------------------------------

Writer = Callable[[str], None]

_call_seq_by_session: dict[str, int] = {}


def _default_writer(name: str) -> Writer:
    def _write(line: str) -> None:
        try:
            path = obs_paths.obs_monitor_path(name)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:  # noqa: BLE001 打点失败不抛
            logger.warning("[monitor] 写入失败 name=%s: %s", name, exc)
    return _write


_writers: dict[str, Writer] = {}


def set_writer(fn: Writer, name: str = USAGE_NAME) -> None:
    """测试钩子：把某本账的输出改道（如内存数组）。"""
    _writers[name] = fn


def reset_writer(name: Optional[str] = None) -> None:
    """测试钩子：恢复默认文件输出。"""
    if name is None:
        _writers.clear()
    else:
        _writers.pop(name, None)


def reset_call_seq(session_id: Optional[str] = None) -> None:
    """测试钩子：清零每会话调用序号。"""
    if session_id is None:
        _call_seq_by_session.clear()
    else:
        _call_seq_by_session.pop(session_id, None)


def _write(name: str, payload: dict[str, Any]) -> None:
    """写一行账本。**任何失败（含注入 writer 抛错）都只告警、绝不外抛**——
    观测不得拖垮主链路（dsh 同精神）。"""
    fn = _writers.get(name) or _default_writer(name)
    try:
        fn(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 打点失败不抛
        logger.warning("[monitor] 打点失败 name=%s: %s", name, exc)


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------

def record_usage(*, session_id: str = "", session_cwd: str = "", model: str,
                 prompt_tokens: int, completion_tokens: int = 0, text_len: int = 0,
                 cache_read_tokens: Optional[int] = None,
                 cache_write_tokens: Optional[int] = None,
                 reasoning_tokens: Optional[int] = None,
                 shadow_old_tokens: Optional[int] = None,
                 shadow_new_tokens: Optional[int] = None,
                 mechanism_tag: str = "new",
                 call_seq: Optional[int] = None,
                 ts: Optional[int] = None) -> dict[str, Any]:
    """记一条模型调用用量。返回落账的完整条目（含分配好的 `callSeq`）。"""
    seq = call_seq
    if session_id:
        nxt = _call_seq_by_session.get(session_id, 0) + 1
        _call_seq_by_session[session_id] = nxt
        if seq is None:
            seq = nxt
    entry: dict[str, Any] = {
        "sessionId": session_id,
        "sessionCwd": session_cwd,
        "mechanismTag": mechanism_tag,
        "ts": int(ts) if ts is not None else int(datetime.now().timestamp() * 1000),
        "callSeq": seq,
        "model": model,
        "promptTokens": int(prompt_tokens),
        "completionTokens": int(completion_tokens),
        "textLen": int(text_len),
    }
    for key, val in (("cacheReadTokens", cache_read_tokens),
                     ("cacheWriteTokens", cache_write_tokens),
                     ("reasoningTokens", reasoning_tokens),
                     ("shadowOldTokens", shadow_old_tokens),
                     ("shadowNewTokens", shadow_new_tokens)):
        if val is not None:
            entry[key] = int(val)
    _write(USAGE_NAME, entry)
    return entry


def record_shadow(*, session_id: str, old_text: str, new_text: str,
                  session_cwd: str = "", ts: Optional[int] = None) -> dict[str, Any]:
    """影子对照：同一轮把"老版"与"新版"的 token 估算同轮入账（纯计算、零干扰）。"""
    return record_usage(
        session_id=session_id, session_cwd=session_cwd, model="shadow",
        prompt_tokens=estimate_tokens(new_text), completion_tokens=0,
        text_len=len(new_text or ""),
        shadow_old_tokens=estimate_tokens(old_text),
        shadow_new_tokens=estimate_tokens(new_text),
        ts=ts)


def record_condense_batch(*, session_id: str, batch_no: int, budget: int,
                          window_seqs: int, consumed_seqs: int,
                          new_mark: int, nodes: int,
                          extra: Optional[dict[str, Any]] = None) -> None:
    """记一批凝练（窗口预算/窗口 seq 数/消耗 seq 数/新 mark/节点数）。"""
    rec: dict[str, Any] = {
        "ts": int(datetime.now().timestamp() * 1000),
        "sessionId": session_id,
        "batchNo": int(batch_no),
        "budget": int(budget),
        "windowSeqs": int(window_seqs),
        "consumedSeqs": int(consumed_seqs),
        "newMark": int(new_mark),
        "nodes": int(nodes),
    }
    if extra:
        rec.update(extra)
    _write(CONDENSE_TRACE_NAME, rec)


def record_depth(*, session_id: str, depth: int, nodes: int,
                 extra: Optional[dict[str, Any]] = None) -> None:
    """深度遥测（**只落盘、不触发任何动作**——深度不设硬顶）。"""
    rec: dict[str, Any] = {
        "ts": int(datetime.now().timestamp() * 1000),
        "sessionId": session_id, "depth": int(depth), "nodes": int(nodes),
    }
    if extra:
        rec.update(extra)
    _write(DEPTH_NAME, rec)
