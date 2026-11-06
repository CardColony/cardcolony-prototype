# -*- coding: utf-8 -*-
"""执行体「表现分」：老中断就扣分，分低了永久除名（**只能人工加回**）。

用户令（2026-11-06）：「比如模型的表现，任务一直中断，就给它记个分值，
分值一低，就永久去掉，除非我说加回去」。

★ 与额度状态分开存：``config/wb_exec_quota.json`` 每 12 小时被探针整表重写，
  表现分不能被它冲掉。
★ 除名后 :func:`note_success` **不会**解禁 ⇒ 恢复只有 :func:`restore` 一条路，
  由用户发令调用（不搞自动恢复）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("api.wb_exec.scoring")

#: 可调参数（集中放这儿，不往代码里散埋魔法数）
START_SCORE = 100
MAX_SCORE = 100
PENALTY_INTERRUPT = 20      # 每次「没干成活就断」扣
REWARD_SUCCESS = 10         # 每次真跑成加
BAN_BELOW = 40              # <= 此分永久除名

STATE_PATH = Path(os.environ.get("WB_EXEC_SCORES_STATE", "")) if os.environ.get(
    "WB_EXEC_SCORES_STATE") else (
    Path(__file__).resolve().parents[3] / "config" / "wb_exec_scores.json")

_lock = threading.RLock()
_cache: dict[str, Any] = {}
_cache_stamp: tuple[float, int] = (0.0, -1)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _stamp(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def _key(executor: str, model: str) -> str:
    return f"{executor}|{model}"


def _load() -> dict[str, Any]:
    global _cache, _cache_stamp
    path = Path(STATE_PATH)
    stamp = _stamp(path)
    with _lock:
        if stamp == _cache_stamp and _cache:
            return _cache
        if stamp == (0.0, -1):
            _cache, _cache_stamp = {"scores": {}}, stamp
            return _cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"scores": {}}
            data.setdefault("scores", {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wb_exec] 表现分文件读不了（当作没有）：%s", exc)
            data = {"scores": {}}
        _cache, _cache_stamp = data, stamp
        return data


def _write(data: dict[str, Any]) -> None:
    global _cache, _cache_stamp
    path = Path(STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    with _lock:
        _cache, _cache_stamp = data, _stamp(path)


def _entry(executor: str, model: str) -> dict[str, Any]:
    scores = _load().setdefault("scores", {})
    e = scores.get(_key(executor, model))
    if not isinstance(e, dict):
        e = {"score": START_SCORE, "interrupts": 0, "successes": 0,
             "banned": False, "reason": "", "ban_reason": "", "last_at": ""}
        scores[_key(executor, model)] = e
    return e


def is_banned(executor: str, model: str) -> bool:
    """这个候选是不是已被永久除名。"""
    try:
        return bool(_entry(executor, model).get("banned"))
    except Exception:  # noqa: BLE001 读不到状态＝没除名，不阻断派活
        return False


def note_interrupt(executor: str, model: str, reason: str = "") -> dict[str, Any]:
    """一次「没干成活就断」⇒ 扣分；分低了永久除名。"""
    with _lock:
        data = _load()
        e = _entry(executor, model)
        e["score"] = max(0, int(e.get("score", START_SCORE)) - PENALTY_INTERRUPT)
        e["interrupts"] = int(e.get("interrupts", 0)) + 1
        e["last_at"] = _now()
        e["reason"] = str(reason or "")
        if e["score"] <= BAN_BELOW:
            e["banned"] = True
            e["ban_reason"] = (
                f"连续中断 {e['interrupts']} 次，表现分 {e['score']} ≤ {BAN_BELOW}")
            logger.warning("[wb_exec] %s/%s 表现分 %s ⇒ 永久除名（需人工加回）",
                           executor, model, e["score"])
        _write(data)
        return dict(e)


def note_success(executor: str, model: str) -> dict[str, Any]:
    """真跑成一次 ⇒ 回一点分。**已被除名的不解禁**（只能人工加回）。"""
    with _lock:
        data = _load()
        e = _entry(executor, model)
        e["score"] = min(MAX_SCORE, int(e.get("score", START_SCORE)) + REWARD_SUCCESS)
        e["successes"] = int(e.get("successes", 0)) + 1
        e["last_at"] = _now()
        _write(data)
        return dict(e)


def restore(executor: str, model: str, score: int = START_SCORE) -> dict[str, Any]:
    """★ **人工加回**：只有用户发令才调。清掉除名并重置分值。"""
    with _lock:
        data = _load()
        scores = data.setdefault("scores", {})
        e = {"score": int(score), "interrupts": 0, "successes": 0,
             "banned": False, "reason": "人工加回", "ban_reason": "",
             "last_at": _now()}
        scores[_key(executor, model)] = e
        _write(data)
        logger.info("[wb_exec] %s/%s 已人工加回（分值 %s）", executor, model, score)
        return dict(e)


def snapshot() -> dict[str, Any]:
    """给诊断/卡面看（只读）。"""
    try:
        return dict(_load().get("scores") or {})
    except Exception:  # noqa: BLE001
        return {}
