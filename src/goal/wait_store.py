"""试点自动卡的等待账，按卡由属主写入。

核对原派工的双方、目标 ID 和版本，再读取本卡的真实汇报或完工回执。
回执到达先标可恢复，首个模型请求发出前消费恢复权。旧台账按卡读取，
旧派工缺目标 ID 时进入待核实。隔离试点由 ECO_GOAL_WAIT_WORKS 选择。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

#: 回执事件类型白名单——「这一步有结果了」的可追溯信号。
RECEIPT_TYPES = ("worker_done", "msg_report")

#: 单次核销消费界：一次 refresh 最多过这么多条新回执，剩下的留到
#: 下次（游标不过线 ⇒ 不漏）。回执白名单本身已把量限住，界是兜底。
_PAGE = 500

#: 回执先到窗口：声明时刻往前留的可认账跨度（先于登记到达的回执）。
_LOOKBACK_S = 7 * 86400

#: 派工事件回看窗口：核销时回原派工找版本印，按声明时刻往前 30 天界住。
_DISPATCH_LOOKBACK_S = 30 * 86400


def enabled(work_id: str) -> bool:
    return work_id in {item.strip() for item in
                       os.environ.get("ECO_GOAL_WAIT_WORKS", "").split(",") if item.strip()}


def _path(work_id: str) -> Path:
    from ..storage import card_store
    return Path(card_store.WORKS_DIR) / work_id / "goal_waits.json"


def _load(work_id: str) -> dict[str, list[dict[str, Any]]]:
    """读旧台账及已按卡拆分的记录，供排查和存量读取。"""
    try:
        data = json.loads(_path(work_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    for path in _path(work_id).with_suffix("").glob("*.json"):
        data[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _read_card_waits(work_id: str, card_id: str) -> list[dict[str, Any]]:
    path = _path(work_id)
    try:
        return json.loads((path.with_suffix("") / f"{card_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            return json.loads(path.read_text(encoding="utf-8")).get(card_id, [])
        except FileNotFoundError:
            return []


def _save(work_id: str, card_id: str, waits: list[dict[str, Any]]) -> None:
    # 卡的属主只更新自己的文件。不同 runner 上的卡可以同时写。
    path = _path(work_id).with_suffix("") / f"{card_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(waits, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def declare(work_id: str, card_id: str, *, goal_id: str, revision: int,
            task_id: str, expect: str = "", from_card: str = "",
            note: str = "", independent: bool = False) -> dict[str, Any]:
    """登记一条等待；落库即返回（核销在 refresh 里做——登记时刻不猜）。"""
    rec = {
        "wait_id": f"wait_{uuid.uuid4().hex[:10]}",
        "goal_id": str(goal_id or ""),
        "revision": int(revision or 0),
        "task_id": str(task_id or ""),
        "expect": str(expect or ""),       # worker_done | msg_report | ""=任一
        "from": str(from_card or ""),
        "note": str(note or "")[:240],
        "independent": bool(independent),
        "declared_ts": round(time.time(), 3),
        "checked_seq": 0,                  # 游标：回执先到也要能捞到
        "satisfied": None,                 # {"by_seq","by_msg","rev_via","ts"}
        "partial": False,
        # 派工版本印（懒解析，首次 refresh 时回原派工事件找）：
        #   dispatch_found 找到与否；dispatch_rev 印上的版本（""=没印）。
        "dispatch_checked": False,
        "dispatch_found": False,
        "dispatch_rev": "",
    }
    waits = _read_card_waits(work_id, card_id)
    for wait in waits:
        if all(wait.get(key) == rec[key] for key in
               ("goal_id", "revision", "task_id", "expect", "from")):
            if not wait.get("satisfied") and wait.get("independent") != rec["independent"]:
                wait["independent"] = rec["independent"]
                _save(work_id, card_id, waits)
            return wait
    waits.append(rec)
    _save(work_id, card_id, waits)
    return rec


def _receipts(work_id: str, card_id: str, since_seq: int, *,
              task_id: str, from_card: str, expect: str,
              since_ts: float = 0.0,
              limit: int = _PAGE) -> tuple[list[dict[str, Any]], bool]:
    """按游标增量读回执事件（升序），返回 (本次消费的行, 是否读尽)。

    先按派工、发件方及类型过滤，再沿序号取页。
    """
    try:
        from ..infra import fact_store
        from ..storage import card_store as _cs
        # 先增量喂本卡事件文件：卡间回执经 event_log 落 jsonl、不内联
        # ingest——本趟按字节水位只读新增（有界），回执才进得了 facts 表。
        _ev = Path(_cs.WORKS_DIR) / work_id / "events" / f"{card_id}.jsonl"
        fact_store.rebuild_from_file(
            fact_store.store(), work_id, _ev,
            since=(since_ts if since_ts > 0 else None), incremental=True)
        rows = fact_store.store().card_facts_after(
            work_id, card_id, since_seq, types=(expect,) if expect else RECEIPT_TYPES,
            limit=limit + 1, since=since_ts or None, task_id=task_id, sender=from_card)
    except Exception:  # noqa: BLE001 读不出按空、但如实标读数不全
        return [], False
    return rows[:limit], len(rows) <= limit


def _field(evt: dict[str, Any], *keys: str) -> str:
    """事件字段兼容读：emit_message_event 把零件落在 ``data`` 里
    （from_card→``data["from"]``、reply_to→``data["reply_to"]``、
    extra 平铺），直接 emit 的事件则顶格——按别名序两处都看。"""
    data = evt.get("data") or evt.get("payload") or {}
    for k in keys:
        v = str(data.get(k) or evt.get(k) or "")
        if v:
            return v
    return ""


def _find_dispatch(work_id: str, card_id: str, wait: dict[str, Any]) -> dict[str, Any] | None:
    """从收件卡事件流按消息编号及派工双方查找原指令。"""
    frm = str(wait.get("from") or "")
    task = str(wait.get("task_id") or "")
    if not frm or not task:
        return None
    try:
        from ..infra import fact_store
        from ..storage import card_store as _cs
        _ev = Path(_cs.WORKS_DIR) / work_id / "events" / f"{frm}.jsonl"
        fact_store.rebuild_from_file(
            fact_store.store(), work_id, _ev, incremental=True)
        _since = float(wait.get("declared_ts") or 0) - _DISPATCH_LOOKBACK_S
        rows = fact_store.store().card_facts_after(
            work_id, frm, -1, types=("msg_directive",), since=_since if _since > 0 else None,
            limit=1, msg_id=task, sender=card_id)
    except Exception:  # noqa: BLE001 查不出按没找到（如实，等下趟再试由 checked 记）
        return None
    for r in rows or []:
        mid = str((r.get("data") or {}).get("msg_id")
                  or r.get("msg_id") or "")
        if mid == task and _field(r, "from") == card_id and _field(r, "to") == frm:
            return r
    return None


def _unbound(wait: dict[str, Any]) -> str:
    """按原派工的目标 ID 和版本返回待核实原因，空串表示归属一致。"""
    if not wait.get("dispatch_found"):
        return "dispatch_missing"
    if not wait.get("dispatch_goal_id"):
        return "dispatch_unstamped"
    if wait["dispatch_goal_id"] != wait.get("goal_id"):
        return "dispatch_goal_mismatch"
    if str(wait.get("dispatch_rev") or "") != str(wait.get("revision") or ""):
        return ("dispatch_unstamped" if not wait.get("dispatch_rev")
                else "dispatch_rev_mismatch")
    return ""


def _match(wait: dict[str, Any], evt: dict[str, Any]) -> bool:
    """核对回执类型、任务编号、来源卡和原派工的目标归属。"""
    if wait.get("expect") and str(evt.get("type") or "") != str(wait["expect"]):
        return False
    if _field(evt, "dispatch_task_id", "reply_to") != str(wait.get("task_id") or ""):
        return False
    want_from = str(wait.get("from") or "")
    if not want_from or _field(evt, "from", "from_card") != want_from:
        return False
    return not _unbound(wait)


def _unverified(wait: dict[str, Any], evt: dict[str, Any]) -> str:
    """看得见回执、证明不了归属 ⇒ 返回拒绝因（""＝不算本等待的反证）。

    只收类型合 expect 的回执——别的类型压根不是回执，谈不上核不核。
    """
    if wait.get("expect") and str(evt.get("type") or "") != str(wait["expect"]):
        return ""
    if _match(wait, evt):
        return ""
    rt = _field(evt, "dispatch_task_id", "reply_to")
    if rt != str(wait.get("task_id") or ""):
        return "other_task"             # 同卡另一笔活的回执
    want_from = str(wait.get("from") or "")
    if want_from and _field(evt, "from", "from_card") != want_from:
        return "wrong_source"
    return _unbound(wait)


def refresh_and_open(work_id: str, card_id: str, *,
                     goal_id: str, revision: int) -> dict[str, Any]:
    """扫一次回执增量、核销满足的等待，回当前版本的未满足等待。

    返回 ``{"open":[…], "satisfied_now":[…], "unverified":[…],
    "stale":[…], "replayed":[…], "partial":bool}``——只读不改语义：
    核销/游标落库，是否让位由驱动判。
    """
    waits = _read_card_waits(work_id, card_id)
    out: dict[str, Any] = {"open": [], "satisfied_now": [], "unverified": [],
                           "stale": [], "replayed": [], "ready": [], "resumed": False,
                           "partial": False}
    if not waits:
        return out
    cur_key = (str(goal_id or ""), int(revision or 0))
    dirty = False
    for w in waits:
        wkey = (str(w.get("goal_id") or ""), int(w.get("revision") or 0))
        if w.get("satisfied") and wkey != cur_key:
            continue                     # 旧版已核销：彻底沉底，不再占游标
        if not w.get("satisfied") and wkey != cur_key:
            out["stale"].append(w["wait_id"])   # 旧版等待：留档不点火
            continue
        # 派工版本印懒解析（每个等待只查一次；事件只会更旧不会重写）。
        if "dispatch_goal_id" not in w or not w.get("dispatch_found"):
            dispatch = _find_dispatch(work_id, card_id, w)
            w["dispatch_found"] = dispatch is not None
            w["dispatch_rev"] = _field(dispatch or {}, "target_rev")
            w["dispatch_goal_id"] = _field(dispatch or {}, "dispatch_goal_id")
            dirty = True
        if _unbound(w):
            if not w.get("satisfied"):
                out["open"].append(w)
            continue
        if w.get("independent_started"):
            out["resumed"] = True
        fresh, complete = _receipts(
            work_id, card_id, int(w.get("checked_seq") or 0),
            task_id=w["task_id"], from_card=w["from"], expect=w.get("expect") or "",
            since_ts=float(w.get("declared_ts") or 0) - _LOOKBACK_S)
        if not complete:
            w["partial"] = True
            out["partial"] = True
        max_seq = int(w.get("checked_seq") or 0)
        for evt in fresh:
            try:
                seq = int(evt.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            max_seq = max(max_seq, seq)
            if w.get("satisfied"):
                # 已核销等待的重放侦测：同 reply_to＋同来源再来一笔 ⇒ 重放。
                if _match(w, evt):
                    out["replayed"].append(
                        {"wait_id": w["wait_id"], "seq": seq,
                         "type": evt.get("type")})
                continue
            if _match(w, evt):
                w["satisfied"] = {
                    "by_seq": seq,
                    "by_msg": str((evt.get("data") or {}).get("msg_id")
                                  or evt.get("msg_id") or ""),
                    # 版本归属的出处：派工印（权威）/ 回执自带（兜底）。
                    "rev_via": "dispatch",
                    "ts": round(time.time(), 3)}
                out["satisfied_now"].append(w["wait_id"])
                dirty = True
                break
            why = _unverified(w, evt)
            if why and not any(u.get("wait_id") == w["wait_id"]
                               and u.get("seq") == seq
                               for u in out["unverified"]):
                out["unverified"].append(
                    {"wait_id": w["wait_id"], "type": evt.get("type"),
                     "seq": seq, "reason": why})
        w["checked_seq"] = max(max_seq, int(w.get("checked_seq") or 0))
        dirty = True
        if w.get("satisfied"):
            if w["satisfied"].get("resumed"):
                out["resumed"] = True
            else:
                out["ready"].append(w)
        else:
            out["open"].append(w)
    if dirty:
        _save(work_id, card_id, waits)
    return out


def blocking_waits(work_id: str, card_id: str, *,
                   goal_id: str, revision: int) -> dict[str, Any]:
    """驱动判据：**拦不拦**。``open`` 里剔除 independent 与「永远证不了」
    的等待后还有剩余＝只剩已声明的等待 ⇒ 让出；independent 的等待只回在
    ``waiting_but_free``；证不了的（缺关联/派工版本印对不上）如实进
    ``unverified``——拦着只会空转，不拦。"""
    res = refresh_and_open(work_id, card_id,
                           goal_id=goal_id, revision=revision)
    for w in res["open"]:
        if not (w.get("task_id") and w.get("from")):
            res["unverified"].append(
                {"wait_id": w["wait_id"], "type": "missing_linkage",
                 "seq": 0})
            continue
        why = _unbound(w)
        if why:
            res["unverified"].append(
                {"wait_id": w["wait_id"], "type": why, "seq": 0})
    held = [w for w in res["open"]
            if w.get("task_id") and w.get("from")
            and not w.get("independent") and not _unbound(w)]
    free = [w for w in res["open"]
            if w.get("independent") and w.get("task_id") and w.get("from")
            and not _unbound(w)]
    return {"held": held, "waiting_but_free": free,
            "unverified": res["unverified"], "partial": res["partial"],
            "stale": res["stale"], "satisfied_now": res["satisfied_now"],
            "replayed": res["replayed"], "ready": res["ready"], "resumed": res["resumed"]}


def consume_resume(work_id: str, card_id: str, goal_id: str, revision: int, wait_ids: tuple[str, ...]) -> None:
    """选择的回执续轮即将发出首个模型请求，才消费当前目标的续轮权。"""
    waits = _read_card_waits(work_id, card_id)
    for wait in waits:
        if (wait["wait_id"] in wait_ids and wait["goal_id"] == goal_id
                and wait["revision"] == revision):
            if wait.get("satisfied"):
                wait["satisfied"]["resumed"] = True
            else:
                wait["independent_started"] = True
    _save(work_id, card_id, waits)


async def receipt_gate(work_id: str, card_id: str, *,
                       receipt: dict[str, Any] | None = None) -> str:
    """回执类消息**唤醒前**的等待裁决（deliver_now 在「空闲⇒起轮」前调它）。

    - ``"driver"``：这笔回执核销了等待——续轮归 goal 驱动统一入口
      （调用方改走 ``driver.request_drive``，**不**另起卡间回合）；
    - ``"hold"``：仍有阻塞等待没核销、或这回执是已消费过的重放——
      不起轮（消息已落会话历史，不丢）；
    - ``"normal"``：无相关等待（没目标/没等待/只剩 independent）——
      照旧投递。

    「先核回执再决定起不起轮」是验收修：改前 report_up 直走
    deliver_now，错任务回执也白烧一轮。
    """
    from ..storage import card_store

    if not enabled(work_id) or not (card_store._meta_of(work_id, card_id) or {}).get("auto_mode"):
        return "normal"
    try:
        from .service import get_service
        goal = await get_service().get(work_id, card_id)
    except Exception:  # noqa: BLE001 目标读不出 ⇒ 不当等待卡，照常投递
        return "normal"
    if goal is None or goal.phase != "active" or goal.activation != "armed":
        if receipt and _field(receipt, "dispatch_task_id"):
            if any(_match(wait, receipt) for wait in _read_card_waits(work_id, card_id)):
                return "hold"
        return "normal"
    res = blocking_waits(work_id, card_id,
                         goal_id=goal.id, revision=int(goal.revision))
    if res["ready"]:
        return "driver"
    if res["held"] or res["replayed"]:
        return "hold"
    # 另一个驱动可能已经扫过本条回执。按发送入口携带的派工身份复核。
    if receipt and _field(receipt, "dispatch_task_id"):
        for wait in _read_card_waits(work_id, card_id):
            if _match(wait, receipt) and (
                    wait.get("satisfied") or wait["goal_id"] != goal.id
                    or wait["revision"] != goal.revision):
                return "hold"
    return "normal"
