# -*- coding: utf-8 -*-
"""**执行体额度状态**（2026-11-03 加）—— 状态驱动的"额度用完就不再调"。

**为什么要有这个**（用户 11-03 原话）：
> 「额度用完后，你应该状态发生变化，后续就不调额度用完的模型和端点」

**问题**：早先只在**调用失败并回报 quota** 时才在进程内记一笔 ⇒
① 要**先浪费一轮**（甚至十分钟）才知道；② 进程一重启就忘了；
③ **端点级**（整个账号/端点的额度都没了）根本没有状态。

**现在**：
- 状态落在一个**文件**里（``config/wb_exec_quota.json``）⇒ **跨进程、跨重启**；
- **模型级**（``executor:model``）与**端点级**（``executor``）都支持；
- 两种来源：
  ① **被动**：调用失败回报 quota/free_tier_limit ⇒ 进程内登记（快，但不及时）；
  ② **主动**：`scripts/check_exec_quota.py` **定期探测各端点** ⇒ 写文件
     ⇒ 下次 pick 立刻跳过（**不等它失败**）。

⇒ **判定全部来自"上游明确报的状态"**，不是猜、不是时间闸。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("api.wb_exec.quota_state")

#: 状态文件（**在仓库里** ⇒ 可版本化、可人工编辑；只放状态，不放密钥）。
STATE_PATH = Path(os.environ.get(
    "WB_EXEC_QUOTA_STATE", "")) if os.environ.get("WB_EXEC_QUOTA_STATE") else (
    Path(__file__).resolve().parents[3] / "config" / "wb_exec_quota.json")

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_stamp: tuple[float, int] = (0.0, -1)


def _stamp(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def load() -> dict[str, Any]:
    """读状态文件（**按 mtime+size 缓存**，不每次都读盘）。"""
    global _cache, _cache_stamp
    path = Path(STATE_PATH)
    stamp = _stamp(path)
    with _lock:
        if stamp == _cache_stamp and _cache:
            return _cache
        if stamp == (0.0, -1):
            _cache, _cache_stamp = {}, stamp
            return _cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception as exc:  # noqa: BLE001 读不到＝没状态，不阻断
            logger.warning("[wb_exec] 额度状态文件读不了（当作没状态）：%s", exc)
            data = {}
        _cache, _cache_stamp = data, stamp
        return data


def _expired(entry: Any, now: float) -> bool:
    """[临时额度机制 2026-11-15 · 用户令] 这条标记**过期了吗**？

    ``expires_at``＝额度预计恢复时刻（从上游报文解析，如 Devin 的
    「limit will reset in 6 hours 25 minutes (at … UTC)」）。到点 ⇒
    **自动算不 exhausted**（让路结束、该档回池）；没写 ⇒ 永久（等巡检
    真调纠正）。**读侧过滤，不写盘**——物理清除交给巡检/下次 write_state。
    """
    if not isinstance(entry, dict):
        return False
    exp = str(entry.get("expires_at") or "").strip()
    if not exp:
        return False
    try:
        from datetime import datetime
        t = datetime.fromisoformat(exp)
        return t.timestamp() <= now
    except Exception:  # noqa: BLE001 解析不了＝当没写过过期时间
        return False


def exhausted_map() -> dict[str, dict]:
    """``{key: {reason, at, expires_at?}}``；key 形如 ``executor:model``（模型级）或 ``executor``（端点级）。

    [临时额度机制 2026-11-15] **过期条目在这里被过滤**（读侧）——
    额度到点自动恢复，不需要任何人守着清。
    """
    raw = (load() or {}).get("exhausted") or {}
    out: dict[str, dict] = {}
    now = _now_ts()
    if isinstance(raw, dict):
        for k, v in raw.items():
            if _expired(v, now):
                continue
            if isinstance(v, dict):
                out[str(k)] = v
            else:
                out[str(k)] = {"reason": str(v)}
    return out


def _now_ts() -> float:
    from datetime import datetime
    return datetime.now().timestamp()


def is_exhausted(executor_id: str, model: str = "") -> bool:
    """这个（执行体[, 模型]）**额度用完了**吗？（只认状态文件里明确写下的）"""
    ex = str(executor_id)
    m = exhausted_map()
    if ex in m:
        return True
    if model and f"{ex}:{model}" in m:
        return True
    return False


def reason_of(executor_id: str, model: str = "") -> str:
    """额度用完的原因（给日志/卡面看）。"""
    m = exhausted_map()
    for key in (f"{executor_id}:{model}" if model else "", str(executor_id)):
        if key and key in m:
            return str((m[key] or {}).get("reason") or "额度耗尽")
    return ""


def write_state(exhausted: dict[str, Any], *, extra: dict[str, Any] | None = None) -> str:
    """写状态文件（**给监控脚本用**）。"""
    data: dict[str, Any] = {
        "updated_at": _now_iso(),
        "exhausted": exhausted or {},
    }
    if extra:
        data.update(extra)
    path = Path(STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    global _cache, _cache_stamp
    with _lock:
        _cache, _cache_stamp = data, _stamp(path)
    return str(path)


def merge_uncovered(exhausted: dict[str, Any],
                    covered: set[str]) -> dict[str, Any]:
    """巡检脚本整表重写前，**把「本轮没探测到」的旧标记并回来**。

    [热更新 2026-11-13] ``check_exec_quota.py`` 每轮用本轮探测结果整表覆盖
    ``exhausted``（这样"额度恢复"能自动清掉旧标记），但它**只探测清单里的
    模型** ⇒ 清单外的条目（人工让路、已下架档）会被悄悄冲空（线上第 22 次
    巡检复现）。语义分工：

    - ``covered``＝本轮真调过的 ``executor:model`` ⇒ 由本轮结果裁决，旧标记不保留；
    - 不在 ``covered`` ⇒ 原样保留（人工登记不该被定时脚本抹掉）。

    只读旧状态、不写盘；返回合并后的新字典，交给 ``write_state``。
    """
    out: dict[str, Any] = dict(exhausted or {})
    keys = {str(k) for k in (covered or set())}
    for k, v in exhausted_map().items():
        if k not in keys:
            out.setdefault(k, v)
    return out


def mark_exhausted(executor_id: str, model: str, reason: str,
                   expires_at: str | None = None) -> str:
    """**登记一条"额度耗尽"并落盘**（2026-11-04 加）。

    ★ **为什么要落盘**：``registry.mark_exhausted`` 原来只记**进程内存**
    ⇒ **生产一重启就忘了**（实测：opencode 免费档 ``free_tier_limit`` 用完后
    进程重启 ⇒ 又去试它 ⇒ 白跑一轮）⇒ 落盘后**跨重启生效**。

    ⇒ **合并写入**（不覆盖别人写的条目，例如脚本登记的"促销到期"）。

    [临时额度机制 2026-11-15 · 用户令] ``expires_at``＝额度预计恢复时刻
    （ISO 本地时间，由上游报文里的「limit will reset … (at …)」解析）。
    写了 ⇒ 到点自动算不 exhausted（``exhausted_map`` 读侧过滤）；
    不写 ⇒ 原语义（等巡检真调纠正）。
    """
    key = f"{executor_id}:{model}" if model else str(executor_id)
    current = dict(exhausted_map())
    entry: dict[str, Any] = {"reason": str(reason or "额度耗尽"),
                             "at": _now_iso(),
                             "kind": "free_tier_limit"}
    if expires_at:
        entry["expires_at"] = str(expires_at)
    current[key] = entry
    path = Path(STATE_PATH)
    extra: dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                for k, v in (old.get("exhausted") or {}).items():
                    current.setdefault(str(k), v)
                for k in ("free_models", "reports", "note"):
                    if k in old:
                        extra[k] = old[k]
        except Exception as exc:  # noqa: BLE001 读坏了就当没有（不阻断）
            logger.warning("[wb_exec] 额度状态文件读不了（按只写新的处理）：%s", exc)
    return write_state(current, extra=extra or None)


def _now_iso() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def clear_exhausted(executor_id: str, model: str = "") -> bool:
    """**清掉状态文件里的这一条**（真调成功 ⇒ 额度回来了 ⇒ 恢复参与路由）。

    ★ 2026-11-04：原来只清进程内存 ⇒ 重启后又被旧标记挡住
    ⇒ 一条误判会变成"永久让路"（现场：Devin 被误判成额度耗尽 ⇒ 全路由到别的）。
    """
    key = f"{executor_id}:{model}" if model else str(executor_id)
    current = dict(exhausted_map())
    if key not in current:
        return False
    current.pop(key, None)
    extra: dict[str, Any] = {}
    path = Path(STATE_PATH)
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                for k in ("free_models", "reports", "note"):
                    if k in old:
                        extra[k] = old[k]
        except Exception:  # noqa: BLE001 读坏了就只写 exhausted
            pass
    write_state(current, extra=extra or None)
    return True
