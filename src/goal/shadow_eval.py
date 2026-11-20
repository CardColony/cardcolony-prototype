"""[旁路对照 · 目标推进方案 2026-11-20 §施工顺序-1] 现行驱动的**影子评估器**。

挂在 ``driver.drive`` 的每个决策出口：对**同一份现场状态**，记录
「现行决定」与「候选策略判断」两条，落 ``works/<wid>/goal_shadow.jsonl``。
纯观察、零行为影响——旁路判断**只记结果**，不点火、不拦截、不叫醒。

候选判断（本阶段能机械算出的部分）：

- ``hold_identical``：本次 idle 的现场签名与上一次续轮点火时**完全相同**
  （目标版本、活跃子卡集合、自上次记录以来零新事实）——方案「相同状态的
  idle 不再开模型回合」：现行决定 spawn 而候选说 hold ⇒ 分歧行，对账用。
- ``advance``：签名有新意（新事实/新子卡/目标改版）⇒ 续轮有事由。
- ``yield`` 系：让位时列出挡住了父卡的直系活跃子卡——为「父卡有独立
  待办是否该继续」的边界对照攒原始素材（独立待办要等模型声明才有，
  本阶段如实标 ``unknown``）。
- ``mirror``：候选与现行车同路（auto 关/无目标/预算尽——机械闸两边一致）。

「等待对象/已声明下一步」本阶段还没有登记源（那是模型在回合里声明的事，
属方案后续批次）——记录里如实留 ``declared: {}`` 空位。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parents[2]

#: 「进展」白名单（2026-11-20 反例修）：只数**可追溯的任务状态与回执**。
#: 反例一：一轮只重复正文，round_start/context/assistant_text 等自产件
#: 曾被当成"有新事实"⇒ 假进展。 narration/驱动自产件一律不算。
_PROGRESS_TYPES = (
    "worker_done", "msg_report", "msg_answer", "msg_directive",
    "msg_sync", "msg_ask", "card_msg",
    "tool_result", "tool_call",
    "card_created", "card_archived", "card_halted", "card_unhalted",
    "submit_revision", "promote_draft", "rule_draft_pending",
)

#: 游标增量页界：一次 note 最多认这么多条新进展，读不尽如实标 partial
#: （反例二：回执被 400 条噪声挤到截尾外 ⇒ 改按类型过滤＋游标，不再截尾）。
_PAGE = 500

#: 进程内：每张卡上次影子记录的现场签名与游标（重启即失——影子账不怕丢）。
_sigs: dict[tuple[str, str], dict[str, Any]] = {}


def _pilot_on(work_id: str) -> bool:
    """同步采样只在隔离试点开启（方案 §二：不进生产路径）。
    ``ECO_SHADOW_WORKS=work_073,work_001`` 逗号表；缺省空集＝全关。"""
    import os
    env = os.environ.get("ECO_SHADOW_WORKS") or ""
    return work_id in {w.strip() for w in env.split(",") if w.strip()}


def _log_path(work_id: str) -> Path:
    # 跟 card_store.WORKS_DIR 走（sandbox 重定向它；不硬编码仓内 works/）。
    try:
        from ..storage import card_store
        d = Path(card_store.WORKS_DIR) / work_id
    except Exception:  # noqa: BLE001
        d = _BASE / "works" / work_id
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d / "goal_shadow.jsonl"


def _active_children(work_id: str, card_id: str) -> list[str]:
    """直系且 goal_state=active 的子卡——与 auto_driver.has_active_child
    同口径，但要**名单**（让位记录要知道是谁挡的）。"""
    try:
        from ..storage import card_store
        out = []
        for cid, m in (card_store.iter_card_metas(work_id) or {}).items():
            if cid == card_id:
                continue
            if str((m or {}).get("parent") or "") != card_id:
                continue
            if str((m or {}).get("goal_state") or "") == "active":
                out.append(cid)
        return sorted(out)
    except Exception:  # noqa: BLE001 读不出按无子卡
        return []


def _facts_since(work_id: str, card_id: str, last_seq: int
                 ) -> tuple[int, int, bool]:
    """游标增量：返回 (白名单类型的新进展数, 本次游标到, 是否读尽)。

    只查 _PROGRESS_TYPES——噪声天然进不来（反例一）；类型过滤＋seq 游标，
    不按"最新 N"截尾（反例二：真回执不再被噪声挤出读数窗）。
    """
    try:
        from ..infra import fact_store
        rows = fact_store.store().card_facts_after(
            work_id, card_id, last_seq, types=_PROGRESS_TYPES, limit=_PAGE + 1)
    except Exception:  # noqa: BLE001 读不出＝读数不全，如实标
        return 0, last_seq, False
    newest = last_seq
    fresh = 0
    complete = True
    for r in rows or []:
        try:
            seq = int(r.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if seq <= last_seq:
            continue
        if fresh >= _PAGE:
            complete = False
            break
        newest = seq
        fresh += 1
    return fresh, newest, complete


def _signature(work_id: str, card_id: str, goal: Any,
               children: list[str]) -> tuple[dict[str, Any], int, int]:
    """现场签名＝目标身份+版本+轮次、活跃子卡集合、自上次记录的新事实数。"""
    prev = _sigs.get((work_id, card_id)) or {}
    last_seq = int(prev.get("last_seq") or 0)
    fresh, newest, complete = _facts_since(work_id, card_id, last_seq)
    sig = {
        # 轮次不进签名：它每轮必增，进签名「相同现场」永远不成立。
        "goal_id": getattr(goal, "id", None),
        "revision": getattr(goal, "revision", None),
        "active_children": children,
    }
    return sig, fresh, newest, complete


def note(work_id: str, card_id: str, decision: str, *,
         outcome: str = "", goal: Any = None, detail: str = "") -> None:
    """决策出口挂点：**同步、零阻断**。decision＝现行驱动的结论
    （``spawn``/``yield_children``/``skip:*``/``block:*``/``disarm:*``）。"""
    try:
        if not _pilot_on(work_id):
            return                  # 同步采样只在隔离试点（方案 §二）
        children = _active_children(work_id, card_id)
        sig, fresh, newest, complete = _signature(
            work_id, card_id, goal, children)
        prev_sig = (_sigs.get((work_id, card_id)) or {}).get("sig")
        same = prev_sig == sig

        if decision == "intent_dropped":
            candidate = "accounting"  # 意图/实际分账行，不是判断
        elif not complete:
            candidate = "pending_verification"   # 读数不全，如实标
        elif decision == "spawn":
            candidate = "hold_identical" if (same and fresh == 0) else "advance"
        elif decision.startswith(("yield", "hold")):
            candidate = "yield"       # 让位/等待本身两边一致
        else:
            candidate = "mirror"      # auto 关/无目标/预算尽：机械闸同判

        rec = {
            "ts": round(time.time(), 3),
            "card": card_id,
            "decision": decision,
            "candidate": candidate,
            "same_sig_as_last": same,
            "new_facts_since_last": fresh,
            "read_complete": complete,
            "active_children": children,
            "goal": {"id": sig["goal_id"], "revision": sig["revision"],
                     "rounds": getattr(goal, "rounds_started", None)},
            "outcome": outcome,
            "detail": detail[:200],
            "declared": {},           # 等待/下一步登记源：后续批次才接
        }
        _sigs[(work_id, card_id)] = {"sig": sig, "last_seq": newest}
        with _log_path(work_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if decision == "spawn" and candidate == "hold_identical":
            logger.info("[shadow] 相同现场重复续轮 card=%s（候选判 hold）",
                        card_id)
    except Exception as exc:  # noqa: BLE001 影子账绝不挡主路
        logger.debug("[shadow] 影子记录失败 %s/%s: %s", work_id, card_id, exc)


def model_request(work_id: str, card_id: str, source: dict[str, Any], *,
                  pair_id: str, attempt: int) -> None:
    if not _pilot_on(work_id):
        return
    record = {
        "ts": round(time.time(), 3), "card": card_id, "decision": "model_request",
        "source": source.get("kind"), "goal": {
            "id": source.get("goalId"), "revision": source.get("revision"),
            "rounds": source.get("round")},
        "pair_id": pair_id, "attempt": attempt,
    }
    try:
        with _log_path(work_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("[shadow] 模型请求记录失败 %s/%s", work_id, card_id, exc_info=True)


def reset() -> None:
    """测试口：清进程内签名账。"""
    _sigs.clear()
