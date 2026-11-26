# -*- coding: utf-8 -*-
"""统筹计划板：plan.items 的结构化条目层（纯函数，不碰库）。

[2026-11-23 · 统筹计划板任务书] 板不是第二套计划——是同一份
``appointments.plan.items`` 的结构化编辑/投影：

- 字段：item_id（新条目宿主分配）、title、next_result、owner_card（可空＝
  归属待核）、priority（正整数，小者优先；缺省按旧顺序投影 100/200/300）、
  status（todo/doing/blocked/done）、depends（已存在 item_id 集合）、
  requirement_refs、result_note、evidence_refs。
- 排序＝(priority, item_id)——同优先级按 ID 稳定序。
- 未完成＝todo/doing/blocked（blocked 也是待推进）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

ITEM_STATUSES = ("todo", "doing", "blocked", "done")
_UNFINISHED = ("todo", "doing", "blocked")
_PRIORITY_STEP = 100

# 条目可编辑字段白名单（apply update/add 只认这些；item_id 不可改）
ITEM_FIELDS = ("title", "next_result", "owner_card", "priority", "status",
               "depends", "requirement_refs", "evidence_refs", "result_note")

# 快照软上限：优先保索引——条目段超过即截断，给出总数与回读方式
_SNAPSHOT_CHARS = 8000


def _is_priority(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if isinstance(x, str) and str(x).strip()]


def normalize_item(raw: Any, idx: int = 0) -> dict[str, Any]:
    """单条目视图：补齐缺省字段。priority 缺省按旧顺序投影 100*(idx+1)。

    只做读侧投影/写前归一——读取本身不改库；首次有效编辑随新版本落库。
    """
    it = dict(raw) if isinstance(raw, dict) else {}
    it["item_id"] = str(it.get("item_id") or "")
    status = str(it.get("status") or "todo")
    it["status"] = status if status in ITEM_STATUSES else "todo"
    p = it.get("priority")
    it["priority"] = p if _is_priority(p) else _PRIORITY_STEP * (idx + 1)
    for key in ("depends", "requirement_refs", "evidence_refs"):
        it[key] = _str_list(it.get(key))
    for key in ("title", "next_result", "result_note", "owner_card"):
        it[key] = str(it.get(key) or "")
    return it


def materialize_items(items: Any) -> list[dict[str, Any]]:
    """写库侧归一：保持原列表顺序（新增在尾），缺省字段实体化。"""
    return [normalize_item(x, i) for i, x in enumerate(items or [])
            if isinstance(x, dict)]


def project_items(items: Any) -> list[dict[str, Any]]:
    """读侧投影：归一后按 (priority, item_id) 稳定排序。"""
    return sorted(materialize_items(items),
                  key=lambda it: (it["priority"], it["item_id"]))


def unfinished_items(items: Any) -> list[dict[str, Any]]:
    return [it for it in project_items(items) if it["status"] in _UNFINISHED]


# ---------------------------------------------------------------- 批量变更


def _fill_fields(it: dict[str, Any], ch: dict[str, Any],
                 loc: str, errors: list[str]) -> None:
    """只写「明确提交」的白名单字段；类型不合规登记 errors（整批作废）。"""
    for key in ITEM_FIELDS:
        if key not in ch:
            continue
        v = ch[key]
        if key == "priority":
            if not _is_priority(v):
                errors.append(f"{loc} priority 必须是正整数")
                continue
        elif key == "status":
            v = str(v or "")
            if v not in ITEM_STATUSES:
                errors.append(
                    f"{loc} status 只接受 {'/'.join(ITEM_STATUSES)}")
                continue
        elif key in ("depends", "requirement_refs", "evidence_refs"):
            if not isinstance(v, list) or any(
                    not isinstance(x, str) or not x.strip() for x in v):
                errors.append(f"{loc} {key} 必须是非空字符串数组")
                continue
            v = _str_list(v)
        elif not isinstance(v, str):
            errors.append(f"{loc} {key} 必须是字符串")
            continue
        elif key == "title" and not v.strip():
            errors.append(f"{loc} title 不能为空")
            continue
        it[key] = v


def apply_changes(items: list[dict[str, Any]], changes: Any
                  ) -> tuple[list[dict[str, Any]], list[str], dict[str, str],
                             list[str]]:
    """校验并应用整批变更（纯函数——调用方在事务里决定落库与否）。

    返回 (merged, changed_ids, allocated, errors)。任一不合法 ⇒
    merged 原样返回＋errors 非空（整批零写入）。"""
    if not isinstance(changes, list) or not changes:
        return items, [], {}, ["changes 必须是非空数组"]
    if len(changes) > 200:
        return items, [], {}, ["changes 一批最多 200 条"]
    by_id = {it["item_id"]: i for i, it in enumerate(items)}
    merged = [dict(it) for it in items]
    changed: list[str] = []
    allocated: dict[str, str] = {}
    errors: list[str] = []

    for n, ch in enumerate(changes):
        loc = f"changes[{n}]"
        if not isinstance(ch, dict):
            errors.append(f"{loc} 必须是对象")
            continue
        op = str(ch.get("op") or "")
        if op == "add":
            title = str(ch.get("title") or "").strip()
            if not title:
                errors.append(f"{loc} add 缺非空 title")
                continue
            if ch.get("item_id"):
                errors.append(f"{loc} 新事项 ID 由宿主分配")
                continue
            iid = f"it_{uuid.uuid4().hex[:10]}"
            it = {"item_id": iid, "title": title, "status": "todo",
                  "depends": [], "requirement_refs": [], "evidence_refs": [],
                  "next_result": "", "result_note": "", "owner_card": "",
                  "priority": _PRIORITY_STEP * (len(merged) + 1)}
            before = len(errors)
            _fill_fields(it, ch, loc, errors)
            if len(errors) != before:
                continue
            if it["status"] == "done" and not it["result_note"].strip():
                errors.append(f"{loc} status=done 必须同时提交 result_note 完成依据")
                continue
            merged.append(it)
            by_id[iid] = len(merged) - 1
            changed.append(iid)
            allocated[f"add:{n}"] = iid
        elif op == "update":
            iid = str(ch.get("item_id") or "").strip()
            if not iid or iid not in by_id:
                errors.append(f"{loc} update 引用不存在的事项 {iid!r}")
                continue
            if str(ch.get("status") or "") == "done" \
                    and not str(ch.get("result_note") or "").strip():
                errors.append(
                    f"{loc} status=done 必须同时提交 result_note 完成依据")
                continue
            it = merged[by_id[iid]]
            before = len(errors)
            _fill_fields(it, ch, loc, errors)
            if len(errors) != before:
                continue
            changed.append(iid)
        else:
            errors.append(f"{loc} op 只接受 add/update（实际 {op!r}）")
    if errors:
        return items, [], {}, errors
    # 依赖闭包核验：depends 必须指向合并后的已存在事项
    ids = {it["item_id"] for it in merged}
    for it in merged:
        for dep in it.get("depends") or []:
            if dep not in ids:
                errors.append(f"事项 {it['item_id']} 依赖不存在的事项 {dep!r}")
    if errors:
        return items, [], {}, errors
    return merged, list(dict.fromkeys(changed)), allocated, []


# ---------------------------------------------------------------- 板投影


def visible_items(appt: dict[str, Any], allowed: Optional[set[str]]) -> list[dict[str, Any]]:
    from .coordination_store import project_observes_all

    items = project_items((appt.get("plan") or {}).get("items"))
    if allowed is None or project_observes_all(appt):
        return items
    fallback = appt.get("model_plan_sources", [])
    return [it for it in items
            if isinstance((sources := it.get("source_cards", fallback)), list)
            and all(isinstance(cid, str) and cid in allowed for cid in sources)]


def board_snapshot(appt: dict[str, Any],
                   allowed: Optional[set[str]] = None) -> dict[str, Any]:
    """优先级顺序的未完成事项，超长时保留完整精简索引。"""
    rev = int(appt.get("rev") or 0)
    out = [{"item_id": it["item_id"], **{key: it[key] for key in ITEM_FIELDS
                                         if key != "result_note"}}
           for it in visible_items(appt, allowed) if it["status"] in _UNFINISHED]
    snap: dict[str, Any] = {"rev": rev, "unfinished_count": len(out),
                            "items": out}
    if not out:
        snap["empty_text"] = "当前无未完成事项"
    elif len(json.dumps(snap, ensure_ascii=False, separators=(",", ":"))) > _SNAPSHOT_CHARS:
        snap["items"] = [{**{k: it[k] for k in ("item_id", "priority", "status")},
                          "title": it["title"][:80]} for it in out]
        snap["read"] = "已精简为完整索引；用 coord_plan_board read 的 item_id 或 offset/limit 查看详情。"
        if len(json.dumps(snap, ensure_ascii=False, separators=(",", ":"))) > _SNAPSHOT_CHARS:
            snap["items"] = [{k: it[k] for k in ("item_id", "priority", "status")} for it in out]
        if len(json.dumps(snap, ensure_ascii=False, separators=(",", ":"))) > _SNAPSHOT_CHARS:
            snap["items"] = snap["items"][:80]
            snap["next_offset"] = len(snap["items"])
    return snap


def read_board(appt: dict[str, Any], allowed: Optional[set[str]] = None,
               item_id: Optional[str] = None, *, offset: Optional[int] = None,
               limit: int = 20) -> dict[str, Any]:
    """coord_plan_board op=read：默认未完成列表；显式 item_id 可回读已完成项。"""
    snap = board_snapshot(appt, allowed)
    iid = str(item_id or "").strip()
    if not iid and offset is None:
        return snap
    visible = visible_items(appt, allowed)
    if iid:
        for it in visible:
            if it["item_id"] == iid:
                return {"rev": snap["rev"], "item": {k: v for k, v in it.items()
                                                    if k in (*ITEM_FIELDS, "item_id")}}
        return {"rev": snap["rev"], "item_id": iid, "error": "事项不可读取"}
    pending = [it for it in visible if it["status"] in _UNFINISHED]
    start = max(0, offset or 0)
    page = [{k: v for k, v in it.items() if k in (*ITEM_FIELDS, "item_id")}
            for it in pending[start:start + max(1, min(limit, 40))]]
    return {"rev": snap["rev"], "total": len(pending), "offset": start, "items": page,
            "next_offset": start + len(page) if start + len(page) < len(pending) else None}
