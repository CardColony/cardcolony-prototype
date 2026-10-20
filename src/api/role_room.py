# -*- coding: utf-8 -*-
"""角色卡机制（5710）——离树的观察者。

设计（与用户 2026-10-20 对齐）：
- 角色卡不挂树：``card_type=role``、``parent=""``，由根卡创建并写职责书。
- 连线＝订阅：工作卡 links 上 ``type="角色"`` 指向角色卡；角色卡 links 上
  ``type="关注"`` 指向工作卡。两边同写，任一侧都能查出关系。
- 同步是机械的：工作卡回合收口时，把「自上次巡检以来新增的会话块」按 seq
  区间记进角色卡的 sync.jsonl；**不复制正文**，唤醒时再按区间读源会话。
- 判断是模型的：角色卡被调起后自己判断发言/沉默；**沉默也必须落账**
  （record_verdict）。模型没记录时，系统机械补一条 silent。
- 双通道：建议走 send_card_message（进工作卡收件箱）；警报走 raise_alarm
  （进 alarms.jsonl，人可见）。
- 游标只在角色回合成功收口后推进；失败则保留待下一次重放（最多重复，不丢）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..storage import card_store, session_facade, session_ops, shadow_store

logger = logging.getLogger("api.role_room")

ROLE_TYPE = "role"
LINK_TYPE_ROLE = "角色"    # 工作卡 -> 角色卡
LINK_TYPE_WATCH = "关注"   # 角色卡 -> 工作卡

#: 单次唤醒最多带多少字符的同步材料（防一次喂爆上下文）。
_MAX_BATCH_CHARS = 24000
#: 单个会话块最多带多少字符。
_MAX_BLOCK_CHARS = 4000
#: 两次唤醒之间的最小间隔（秒）——防「建议→改动→再建议」高速打转。
_SPAWN_COOLDOWN = 2.0

_LAST_SPAWN: dict[tuple[str, str], float] = {}
_ACTIVE: dict[tuple[str, str], dict[str, Any]] = {}
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


# ============================================================
# 基础
# ============================================================

def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _role_dir(work_id: str, role_id: str) -> Path:
    d = card_store.WORKS_DIR / work_id / "roles" / role_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(work_id: str, role_id: str, name: str) -> Path:
    return _role_dir(work_id, role_id) / name


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 读不到＝默认值（fail-open 到空）
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except FileNotFoundError:
        return []
    return out


def _lock_for(work_id: str, role_id: str) -> asyncio.Lock:
    key = (work_id, role_id)
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


def _meta_of(work_id: str, card_id: str) -> dict[str, Any]:
    try:
        return card_store._meta_of(work_id, card_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def is_role(work_id: str, card_id: str) -> bool:
    return str(_meta_of(work_id, card_id).get("card_type") or "") == ROLE_TYPE


def list_roles(work_id: str) -> list[dict[str, Any]]:
    try:
        metas = card_store.iter_card_metas(work_id)
    except Exception:  # noqa: BLE001
        return []
    return [m for m in metas.values()
            if str(m.get("card_type") or "") == ROLE_TYPE]


async def _last_seq(work_id: str, card_id: str) -> int:
    """该卡会话最后一个块 seq；空会话 = -1。"""
    try:
        blocks = await session_facade.read(work_id, card_id)
    except Exception:  # noqa: BLE001
        return -1
    seqs = [int(b["seq"]) for b in blocks
            if isinstance(b, dict) and isinstance(b.get("seq"), int)]
    return max(seqs) if seqs else -1


def _cursor_path(work_id: str, role_id: str) -> Path:
    return _path(work_id, role_id, "cursor.json")


def _cursor(work_id: str, role_id: str) -> dict[str, int]:
    raw = _read_json(_cursor_path(work_id, role_id), {})
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def _set_cursor(work_id: str, role_id: str, cursors: dict[str, int]) -> None:
    _write_json(_cursor_path(work_id, role_id), cursors)


def _sync_path(work_id: str, role_id: str) -> Path:
    return _path(work_id, role_id, "sync.jsonl")


def _verdict_path(work_id: str, role_id: str) -> Path:
    return _path(work_id, role_id, "verdicts.jsonl")


def _alarm_path(work_id: str, role_id: str) -> Path:
    return _path(work_id, role_id, "alarms.jsonl")


# ============================================================
# 连线＝订阅
# ============================================================

def watched_cards(work_id: str, role_id: str) -> list[dict[str, Any]]:
    meta = _meta_of(work_id, role_id)
    metas = card_store.iter_card_metas(work_id)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel in (meta.get("links") or []):
        if not isinstance(rel, dict) or rel.get("type") != LINK_TYPE_WATCH:
            continue
        tid = str(rel.get("target") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        m = metas.get(tid)
        if m:
            out.append(m)
    out.sort(key=lambda m: str(m.get("card_id") or ""))
    return out


def roles_watching(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """哪些角色卡关注这张工作卡（角色 links 的 关注 边为准）。"""
    out: list[dict[str, Any]] = []
    for m in list_roles(work_id):
        for rel in (m.get("links") or []):
            if (isinstance(rel, dict) and rel.get("type") == LINK_TYPE_WATCH
                    and str(rel.get("target") or "") == card_id):
                out.append(m)
                break
    return out


def linked(work_id: str, a: str, b: str) -> bool:
    """a、b 之间是否有「角色—工作」订阅边（任一方向）。"""
    am, bm = _meta_of(work_id, a), _meta_of(work_id, b)
    if str(am.get("card_type") or "") == ROLE_TYPE:
        return any(isinstance(r, dict) and r.get("type") == LINK_TYPE_WATCH
                   and str(r.get("target") or "") == b
                   for r in (am.get("links") or []))
    if str(bm.get("card_type") or "") == ROLE_TYPE:
        return any(isinstance(r, dict) and r.get("type") == LINK_TYPE_WATCH
                   and str(r.get("target") or "") == a
                   for r in (bm.get("links") or []))
    return False


async def link(work_id: str, role_id: str, source_id: str,
               actor: str = "用户") -> dict[str, Any]:
    """建立订阅：角色关注工作卡。幂等。"""
    role = _meta_of(work_id, role_id)
    source = _meta_of(work_id, source_id)
    if str(role.get("card_type") or "") != ROLE_TYPE:
        return {"error": f"不是角色卡: {role_id}"}
    if not source:
        return {"error": f"卡片不存在: {source_id}"}
    if str(source.get("card_type") or "") == ROLE_TYPE:
        return {"error": "角色卡不能被另一个角色卡关注（v1 角色之间不通信）"}
    if role_id == source_id:
        return {"error": "角色卡不能关注自己"}

    async def _add(owner_id: str, rel: dict[str, Any]) -> None:
        cur = await card_store.read_card(work_id, owner_id)
        links = list(cur["meta"].get("links") or [])
        for r in links:
            if (isinstance(r, dict) and r.get("target") == rel["target"]
                    and r.get("type") == rel["type"]):
                return
        links.append(rel)
        await card_store.update_card(
            work_id, owner_id, {"links": links},
            f"[角色] {actor}建立订阅：{owner_id} ↔ {rel['target']}",
        )

    await _add(role_id, {"target": source_id, "type": LINK_TYPE_WATCH,
                         "note": f"由{actor}建立"})
    await _add(source_id, {"target": role_id, "type": LINK_TYPE_ROLE,
                           "note": f"由{actor}建立"})
    # 建线即从「现在」开始算，不回头同步历史（避免角色一上任就被灌爆）。
    async with _lock_for(work_id, role_id):
        cursors = _cursor(work_id, role_id)
        cursors[source_id] = await _last_seq(work_id, source_id)
        _set_cursor(work_id, role_id, cursors)
    session_ops.ops_log("role_link", work_id, role_id,
                        {"source": source_id, "actor": actor})
    return {"ok": True, "role_id": role_id, "source_id": source_id}


async def unlink(work_id: str, role_id: str, source_id: str,
                 actor: str = "用户") -> dict[str, Any]:
    async def _drop(owner_id: str, target: str, typ: str) -> int:
        try:
            cur = await card_store.read_card(work_id, owner_id)
        except Exception:  # noqa: BLE001
            return 0
        links = list(cur["meta"].get("links") or [])
        keep = [r for r in links
                if not (isinstance(r, dict) and r.get("target") == target
                        and r.get("type") == typ)]
        removed = len(links) - len(keep)
        if removed:
            await card_store.update_card(
                work_id, owner_id, {"links": keep},
                f"[角色] {actor}解除订阅：{owner_id} ↔ {target}",
            )
        return removed

    a = await _drop(role_id, source_id, LINK_TYPE_WATCH)
    b = await _drop(source_id, role_id, LINK_TYPE_ROLE)
    async with _lock_for(work_id, role_id):
        cursors = _cursor(work_id, role_id)
        cursors.pop(source_id, None)
        _set_cursor(work_id, role_id, cursors)
    session_ops.ops_log("role_unlink", work_id, role_id,
                        {"source": source_id, "actor": actor, "removed": a + b})
    return {"ok": True, "removed": a + b}


# ============================================================
# 同步（机械）
# ============================================================

def record_sync(work_id: str, role_id: str, source_id: str,
                from_seq: int, to_seq: int) -> Optional[dict[str, Any]]:
    """记一条同步事件（只记 seq 区间，不复制正文）。同一源区间幂等。"""
    events = _read_jsonl(_sync_path(work_id, role_id))
    for ev in reversed(events):
        if (str(ev.get("source") or "") == source_id
                and int(ev.get("to_seq") or -1) >= to_seq):
            return None
    rec = {
        "seq": len(events) + 1,
        "ts": _now(),
        "source": source_id,
        "from_seq": int(from_seq),
        "to_seq": int(to_seq),
    }
    _append_jsonl(_sync_path(work_id, role_id), rec)
    return rec


def pending_batch(work_id: str, role_id: str) -> list[dict[str, Any]]:
    """按游标聚出待巡检批次：每个来源一条，含 seq 区间。"""
    cursors = _cursor(work_id, role_id)
    events = _read_jsonl(_sync_path(work_id, role_id))
    by_source: dict[str, dict[str, int]] = {}
    for ev in events:
        src = str(ev.get("source") or "")
        if not src:
            continue
        cur = int(cursors.get(src, -1))
        to_seq = int(ev.get("to_seq") or -1)
        if to_seq <= cur:
            continue
        item = by_source.setdefault(src, {"from": int(ev.get("from_seq") or 0),
                                          "to": to_seq})
        item["from"] = min(item["from"], int(ev.get("from_seq") or 0))
        item["to"] = max(item["to"], to_seq)
    out: list[dict[str, Any]] = []
    for src, rng in by_source.items():
        out.append({"source": src, "from_seq": rng["from"], "to_seq": rng["to"]})
    out.sort(key=lambda x: x["source"])
    return out


async def build_batch(work_id: str, role_id: str) -> list[dict[str, Any]]:
    """把 seq 区间展开成可读材料（此时才读源会话，不落副本）。"""
    items = pending_batch(work_id, role_id)
    out: list[dict[str, Any]] = []
    total = 0
    for item in items:
        src = item["source"]
        meta = _meta_of(work_id, src)
        try:
            blocks = await session_facade.read(work_id, src)
        except Exception:  # noqa: BLE001
            blocks = []
        picked = [b for b in blocks
                  if isinstance(b, dict) and isinstance(b.get("seq"), int)
                  and item["from_seq"] <= int(b["seq"]) <= item["to_seq"]]
        lines: list[str] = []
        length = 0
        for b in picked:
            actor = str(b.get("actor") or b.get("role") or "?")
            text = str(b.get("content") or "").strip()
            if len(text) > _MAX_BLOCK_CHARS:
                text = text[:_MAX_BLOCK_CHARS] + "……（截断）"
            piece = f"[{actor}] {text}" if text else f"[{actor}]（空正文）"
            if total + length + len(piece) > _MAX_BATCH_CHARS:
                lines.append("……（本批材料超长，已截断；完整内容见源卡会话）")
                break
            lines.append(piece)
            length += len(piece)
        total += length
        out.append({
            "source": src,
            "title": str(meta.get("title") or ""),
            "from_seq": item["from_seq"],
            "to_seq": item["to_seq"],
            "blocks": len(picked),
            "text": "\n".join(lines) if lines else "（该区间没有可读块）",
        })
    return out


# ============================================================
# 判词 / 警报台账
# ============================================================

def record_verdict(work_id: str, role_id: str, decision: str, reason: str,
                   targets: Optional[list[str]] = None,
                   batch: Optional[list[dict[str, Any]]] = None,
                   auto: bool = False) -> dict[str, Any]:
    decision = str(decision or "").strip().lower()
    if decision not in ("speak", "silent"):
        decision = "silent"
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "decision": decision,
        "reason": str(reason or "").strip(),
        "targets": [str(t) for t in (targets or [])],
        "sources": [str(i.get("source") or "") for i in (batch or [])],
        "auto": bool(auto),
    }
    _append_jsonl(_verdict_path(work_id, role_id), rec)
    session_ops.ops_log("role_verdict", work_id, role_id, {
        "decision": decision, "reason": rec["reason"][:160],
        "targets": rec["targets"], "auto": rec["auto"],
    })
    return rec


def list_verdicts(work_id: str, role_id: str, limit: int = 20) -> list[dict[str, Any]]:
    items = _read_jsonl(_verdict_path(work_id, role_id))
    return items[-limit:][::-1]


def record_alarm(work_id: str, role_id: str, severity: str, title: str,
                 detail: str) -> dict[str, Any]:
    severity = str(severity or "warn").strip().lower()
    if severity not in ("info", "warn", "critical"):
        severity = "warn"
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "role_id": role_id,
        "role_title": str(_meta_of(work_id, role_id).get("title") or ""),
        "severity": severity,
        "title": str(title or "").strip() or "角色警报",
        "detail": str(detail or "").strip(),
        "status": "open",
    }
    _append_jsonl(_alarm_path(work_id, role_id), rec)
    session_ops.ops_log("role_alarm", work_id, role_id, {
        "severity": severity, "title": rec["title"][:120],
    })
    try:
        from . import event_stream
        event_stream.emit(work_id, role_id, "role_alarm", {
            "id": rec["id"], "severity": severity,
            "title": rec["title"], "detail": rec["detail"][:500],
        })
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    return rec


def list_alarms(work_id: str, status: str = "open") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for role in list_roles(work_id):
        rid = str(role.get("card_id") or "")
        for item in _read_jsonl(_alarm_path(work_id, rid)):
            if status and str(item.get("status") or "open") != status:
                continue
            out.append(item)
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return out


def ack_alarm(work_id: str, role_id: str, alarm_id: str) -> bool:
    path = _alarm_path(work_id, role_id)
    items = _read_jsonl(path)
    hit = False
    for it in items:
        if str(it.get("id") or "") == alarm_id:
            it["status"] = "acked"
            it["acked_at"] = _now()
            hit = True
    if hit:
        path.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n",
            encoding="utf-8")
    return hit


# ============================================================
# 唤醒与收口
# ============================================================

def _batch_ref(batch: list[dict[str, Any]]) -> str:
    if not batch:
        return ""
    return ",".join(f"{i['source']}:{i['from_seq']}-{i['to_seq']}" for i in batch)


def active_batch(work_id: str, role_id: str) -> list[dict[str, Any]]:
    """当前巡检回合正在处理的批次（供 record_verdict 记来源）。"""
    item = _ACTIVE.get((work_id, role_id)) or {}
    batch = item.get("batch") or []
    return batch if isinstance(batch, list) else []


def build_prompt(work_id: str, role_id: str,
                 batch: list[dict[str, Any]]) -> str:
    meta = _meta_of(work_id, role_id)
    title = str(meta.get("title") or role_id)
    watched = watched_cards(work_id, role_id)
    watch_lines = "\n".join(
        f"- {m.get('card_id')}《{m.get('title')}》" for m in watched) or "（暂无）"
    parts = [
        f"〔角色巡检〕你是本项目的角色卡《{title}》（{role_id}）。",
        "你不是作业卡：你不做具体工作，你对你负责的领域保持观察、判断，"
        "只在必要时给出建议或警报。",
        "",
        "## 你的职责书（根卡立项，用户可改）",
        _mission_text(work_id, role_id) or "（职责书为空——请按卡片标题自行理解）",
        "",
        "## 你关注的工作卡",
        watch_lines,
        "",
        "## 自上次巡检以来，你关注的工作卡新增了以下内容",
    ]
    for item in batch:
        parts.append(
            f"### {item['source']}《{item['title']}》 块 {item['from_seq']}..{item['to_seq']}"
            f"（{item['blocks']} 块）")
        parts.append(item["text"])
    parts += [
        "",
        "## 你本轮必须做的事",
        "1. 判断：这些新内容里，有没有值得你发言的事？只按职责书里写明要关注的范围判断。",
        "2. **无论发言还是沉默，必须调用 record_verdict(decision, reason)** 记录本次判断：",
        "   - decision=\"speak\"：你决定发言（建议或警报）；",
        "   - decision=\"silent\"：你判断无需干预。",
        "   reason 一句话写清依据（引用你看到的具体内容）。沉默也是判断，必须落账。",
        "3. 若发言：",
        "   - 普通建议：调用 send_card_message 发给最相关的那张工作卡"
        "（默认发回本次内容的来源卡），类型用 msg_notify；",
        "   - 严重风险：调用 raise_alarm 直通用户；可同时发建议。",
        "4. 可以用 update_status / append_ruling 更新**你自己**的现状与定论（只限你自己）。",
        "5. 不得修改别人的卡、不得建卡、不得调文件或命令。",
        "",
        "判断标准：没有新增风险就沉默。不要为了说话而说话。",
    ]
    return "\n".join(parts)


def _mission_text(work_id: str, role_id: str) -> str:
    """同步读角色卡正文的使命块（build_prompt 是同步函数，故走库读）。"""
    try:
        from ..storage import card_db
        row = card_db.read_sync(
            work_id,
            lambda conn: card_db.get_card_row(conn, role_id), None)
        if row is None:
            return ""
        from ..storage.hash_utils import split_body_sections
        _meta, body = _row_body(row)
        return str(split_body_sections(body).get("mission") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _row_body(row: Any) -> tuple[dict[str, Any], str]:
    """把 cards 行还原成 (meta, 正文)——与 card_store.read_card 同口径的最小版。"""
    from ..storage import card_db
    meta = card_db.row_to_meta(row)
    body = "\n".join([
        "## 使命", str(row["mission"] or ""),
        "## 现状", str(row["status_block"] or ""),
        "## 定论", str(row["rulings"] or ""),
    ])
    return meta, body


async def _finalize(work_id: str, role_id: str, batch: list[dict[str, Any]],
                    before: int, resp: Any) -> None:
    """角色回合收口：推进游标、机械补记沉默、落影子日志、释放活跃位。"""
    key = (work_id, role_id)
    _ACTIVE.pop(key, None)
    ok = isinstance(resp, dict)
    if not ok:
        session_ops.ops_log("role_turn_failed", work_id, role_id,
                            {"batch": _batch_ref(batch)})
        logger.warning("[role] 巡检回合失败，游标不推进 role=%s", role_id)
        return
    # 1) 推进游标（只推进本批已处理的区间）
    try:
        cursors = _cursor(work_id, role_id)
        for item in batch:
            cursors[item["source"]] = max(
                int(cursors.get(item["source"], -1)), int(item["to_seq"]))
        _set_cursor(work_id, role_id, cursors)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[role] 游标推进失败 role=%s: %s", role_id, exc)
    # 2) 机械补记：模型没调用 record_verdict ⇒ 沉默仍要落账
    verdicts = _read_jsonl(_verdict_path(work_id, role_id))
    if len(verdicts) <= before:
        rec = record_verdict(
            work_id, role_id, "silent",
            "（机械补记：本轮未调用 record_verdict，按沉默处理）",
            batch=batch, auto=True)
        verdicts.append(rec)
    last = verdicts[-1] if verdicts else {}
    decision = str(last.get("decision") or "silent")
    reason = str(last.get("reason") or "")
    # 3) 角色自己的过程日志（影子 ## 进度）
    try:
        await shadow_store.append_to_section(
            work_id, role_id, "## 进度",
            [f"- [{_now()}] 巡检 {len(batch)} 源 · {decision} · {reason[:160]}"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import event_stream
        event_stream.emit(work_id, role_id, "role_judged", {
            "decision": decision, "reason": reason[:300],
            "sources": [i["source"] for i in batch],
            "auto": bool(last.get("auto")),
        })
    except Exception:  # noqa: BLE001
        pass
    # 4) 巡检期间又有新同步 ⇒ 冷却后追一次（角色自己的回合不会再触发 after_turn）
    try:
        if pending_batch(work_id, role_id):
            from . import message_runner as _mr

            async def _followup() -> None:
                await asyncio.sleep(_SPAWN_COOLDOWN + 0.1)
                spawn(work_id, role_id)

            _mr._spawn_tracked(_followup())
    except Exception:  # noqa: BLE001
        pass


def spawn(work_id: str, role_id: str) -> bool:
    """起一次角色巡检回合（异步，不阻塞调用方）。"""
    key = (work_id, role_id)
    if key in _ACTIVE:
        return False
    now = asyncio.get_event_loop().time()
    if now - _LAST_SPAWN.get(key, 0.0) < _SPAWN_COOLDOWN:
        return False
    try:
        if session_facade.turn_holder(work_id, role_id):
            return False
    except Exception:  # noqa: BLE001
        pass
    batch = pending_batch(work_id, role_id)
    if not batch:
        return False
    _LAST_SPAWN[key] = now
    _ACTIVE[key] = {"batch": batch, "started": _now()}
    before = len(_read_jsonl(_verdict_path(work_id, role_id)))
    try:
        from . import message_runner as _mr
        from . import routes as _routes

        async def _run() -> None:
            material = await build_batch(work_id, role_id)
            _ACTIVE[key]["batch"] = material
            real_prompt = build_prompt(work_id, role_id, material)
            resp = await _routes._run_message(
                work_id, role_id, real_prompt, None, append_user=False)
            await _finalize(work_id, role_id, material, before, resp)

        _mr._spawn_tracked(_run())
        return True
    except Exception as exc:  # noqa: BLE001
        _ACTIVE.pop(key, None)
        logger.error("[role] 起巡检回合失败 role=%s: %s", role_id, exc)
        return False


async def after_turn(work_id: str, card_id: str, result: Any = None) -> None:
    """工作卡回合收口：把新增会话同步给关注它的角色卡。永不抛出。"""
    try:
        meta = _meta_of(work_id, card_id)
        if not meta or str(meta.get("card_type") or "") == ROLE_TYPE:
            return
        roles = roles_watching(work_id, card_id)
        if not roles:
            return
        last = await _last_seq(work_id, card_id)
        if last < 0:
            return
        for role in roles:
            role_id = str(role.get("card_id") or "")
            if not role_id:
                continue
            async with _lock_for(work_id, role_id):
                cur = int(_cursor(work_id, role_id).get(card_id, -1))
                if last > cur:
                    record_sync(work_id, role_id, card_id, cur + 1, last)
            spawn(work_id, role_id)
    except Exception as exc:  # noqa: BLE001 同步失败不得吃掉工作卡回合
        logger.warning("[role] after_turn 同步失败 work=%s card=%s: %s",
                       work_id, card_id, exc)


async def manual_check(work_id: str, role_id: str) -> dict[str, Any]:
    """人工触发一次巡检：先把关注卡的新增补进队列，再起回合。"""
    for m in watched_cards(work_id, role_id):
        src = str(m.get("card_id") or "")
        if not src:
            continue
        last = await _last_seq(work_id, src)
        if last < 0:
            continue
        async with _lock_for(work_id, role_id):
            cur = int(_cursor(work_id, role_id).get(src, -1))
            if last > cur:
                record_sync(work_id, role_id, src, cur + 1, last)
    started = spawn(work_id, role_id)
    return {"started": started,
            "pending_sources": [p["source"] for p in pending_batch(work_id, role_id)]}


# ============================================================
# 查询（UI）
# ============================================================

def role_summary(work_id: str, role_id: str) -> dict[str, Any]:
    meta = _meta_of(work_id, role_id)
    watched = watched_cards(work_id, role_id)
    verdicts = list_verdicts(work_id, role_id, limit=20)
    alarms = [a for a in list_alarms(work_id, status="open")
              if str(a.get("role_id") or "") == role_id]
    pending = pending_batch(work_id, role_id)
    mission = _mission_text(work_id, role_id)
    return {
        "role_id": role_id,
        "title": str(meta.get("title") or ""),
        "card_type": str(meta.get("card_type") or ""),
        "mission": mission,
        "watched": [{"card_id": str(m.get("card_id") or ""),
                     "title": str(m.get("title") or ""),
                     "card_type": str(m.get("card_type") or "")} for m in watched],
        "verdicts": verdicts,
        "alarms": alarms,
        "pending_sources": [p["source"] for p in pending],
        "open_alarm_count": len(alarms),
    }
