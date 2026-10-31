# -*- coding: utf-8 -*-
"""角色卡机制 v2（5710）——独立结构、离树观察者。

角色卡不是“普通卡＋prompt”。它有独立的档案与三本台账（见
``src/storage/role_store.py`` 与 ``card_db._ROLE_DDL``）：

- ``jurisdiction``：管辖域（对什么负责；带排他键）
- ``watch``：关注卡与关注信号（同步按信号剪枝）
- ``speak_contract``：建议/警报/沉默的阈值
- ``escalation``：建议/警报/严重警报分别发给谁
- ``duty``：值班态（active/suspended/archived）、最后巡检、待同步数
- ``charter``：职责书（版本化，根卡/人批准）
- 判断簿 ``role_verdicts``：每次巡检的 speak/silent、依据、来源、证据、被取代
- 警报簿 ``role_alarms``：severity、状态、ack、更正链
- 同步队列 ``role_sync``：来源、seq 区间、命中信号、pending/consumed

离树：``parent=""``，不可有子卡；工作卡通过连线订阅它（连线只是传输，
``watch.cards`` 才是声明的管辖范围）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..storage import card_store, role_store, session_facade, session_ops, shadow_store

logger = logging.getLogger("api.role_room")

ROLE_TYPE = "role"
LINK_TYPE_ROLE = "角色"    # 工作卡 -> 角色卡
LINK_TYPE_WATCH = "关注"   # 角色卡 -> 工作卡

MAX_BATCH_CHARS = 24000
MAX_BLOCK_CHARS = 4000
SPAWN_COOLDOWN = 2.0

DEFAULT_SIGNALS = ("turn_completed",)
ALL_SIGNALS = ("turn_completed", "status_changed", "decision_appended", "turn_error")

_LAST_SPAWN: dict[tuple[str, str], float] = {}
_ACTIVE: dict[tuple[str, str], dict[str, Any]] = {}
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


# ============================================================
# 基础
# ============================================================

def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


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


def _lock_for(work_id: str, role_id: str) -> asyncio.Lock:
    key = (work_id, role_id)
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


async def _last_seq(work_id: str, card_id: str) -> int:
    try:
        blocks = await session_facade.read(work_id, card_id)
    except Exception:  # noqa: BLE001
        return -1
    seqs = [int(b["seq"]) for b in blocks
            if isinstance(b, dict) and isinstance(b.get("seq"), int)]
    return max(seqs) if seqs else -1


def _slug(text: str) -> str:
    s = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (text or ""))
    return s.strip("_")[:48] or "role"


# ============================================================
# 档案
# ============================================================

def make_profile(title: str, charter: str,
                 jurisdiction: Any = None,
                 watch_cards: Optional[list[str]] = None,
                 signals: Optional[list[str]] = None,
                 speak_contract: Optional[dict[str, Any]] = None,
                 escalation: Optional[dict[str, Any]] = None,
                 ratified_by: str = "root_card") -> dict[str, Any]:
    """构造一份合法的角色档案（调用方负责落库）。"""
    if isinstance(jurisdiction, str):
        jurisdiction = {"domain": _slug(jurisdiction), "statement": jurisdiction}
    if not isinstance(jurisdiction, dict):
        jurisdiction = {}
    domain = str(jurisdiction.get("domain") or _slug(title))
    jur = {
        "domain": domain,
        "statement": str(jurisdiction.get("statement") or "").strip(),
        "exclusive_key": str(jurisdiction.get("exclusive_key") or domain),
    }
    sigs = [s for s in (signals or DEFAULT_SIGNALS) if s in ALL_SIGNALS]
    if not sigs:
        sigs = list(DEFAULT_SIGNALS)
    sc = speak_contract if isinstance(speak_contract, dict) else {}
    esc = escalation if isinstance(escalation, dict) else {}
    now = _now()
    return {
        "jurisdiction": jur,
        "watch": {
            "cards": [str(c) for c in (watch_cards or []) if c],
            "signals": sigs,
        },
        "speak_contract": {
            "advice_when": [str(x) for x in (sc.get("advice_when") or ["gap_or_risk"])],
            "alarm_when": [str(x) for x in (sc.get("alarm_when") or ["mutually_exclusive_fact"])],
            "silence_default": bool(sc.get("silence_default", True)),
            "max_messages_per_wake": int(sc.get("max_messages_per_wake", 2)),
        },
        "escalation": {
            "advice_to": str(esc.get("advice_to") or "source_card"),
            "alarm_to": str(esc.get("alarm_to") or "human"),
            "critical_to": str(esc.get("critical_to") or "root_and_human"),
        },
        "duty": {"state": "active", "since": now,
                 "last_wake_at": "", "pending_sync": 0},
        "charter": {"version": 1, "text": str(charter or "").strip(),
                    "ratified_by": ratified_by, "ratified_at": now},
    }


async def ensure_profile(work_id: str, role_id: str) -> Optional[dict[str, Any]]:
    """角色档案缺失（老原型卡）→ 从卡面使命/连线派生一份，保证可继续用。"""
    prof = role_store.get_profile(work_id, role_id)
    if prof is not None:
        return prof
    meta = _meta_of(work_id, role_id)
    if not meta or str(meta.get("card_type") or "") != ROLE_TYPE:
        return None
    try:
        card = await card_store.read_card(work_id, role_id)
        charter = str((card.get("body") or {}).get("mission") or "").strip()
    except Exception:  # noqa: BLE001
        charter = ""
    watch_cards = []
    for rel in (meta.get("links") or []):
        if isinstance(rel, dict) and rel.get("type") == LINK_TYPE_WATCH:
            watch_cards.append(str(rel.get("target") or ""))
    prof = make_profile(str(meta.get("title") or role_id), charter,
                        watch_cards=[c for c in watch_cards if c])
    out = await role_store.ensure_profile(work_id, role_id, prof)
    await _import_legacy_ledgers(work_id, role_id)
    return out


async def _import_legacy_ledgers(work_id: str, role_id: str) -> None:
    """v1 原型把判词/警报放在 JSONL；首次建 v2 档案时导入 DB，台账不丢。"""
    base = Path(card_store.WORKS_DIR) / work_id / "roles" / role_id
    if not base.exists():
        return
    for name, importer in (("verdicts.jsonl", role_store.import_verdict),
                           ("alarms.jsonl", role_store.import_alarm)):
        p = base / name
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(rec, dict):
                    await importer(work_id, role_id, rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[role] 旧台账导入失败 %s: %s", p, exc)


async def get_profile(work_id: str, role_id: str) -> Optional[dict[str, Any]]:
    """公开读取角色档案（缺失时按卡面派生）。"""
    return await ensure_profile(work_id, role_id)


async def _domain_conflict(work_id: str, domain: str,
                           exclude_role: str = "") -> Optional[dict[str, Any]]:
    if not domain:
        return None
    for prof in await role_store.list_profiles(work_id):
        rid = str(prof.get("role_id") or "")
        if not rid or rid == exclude_role:
            continue
        if str((prof.get("duty") or {}).get("state") or "active") == "archived":
            continue
        jur = prof.get("jurisdiction") or {}
        if str(jur.get("exclusive_key") or jur.get("domain") or "") == domain:
            return prof
    return None


async def create_role_card(work_id: str, root_id: str, title: str,
                           charter: str, *,
                           jurisdiction: Any = None,
                           watch_cards: Optional[list[str]] = None,
                           signals: Optional[list[str]] = None,
                           speak_contract: Optional[dict[str, Any]] = None,
                           escalation: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """根卡组阁：建离树角色卡 + 档案 + 职责书 v1 + 订阅连线。"""
    title = str(title or "").strip()
    charter = str(charter or "").strip()
    if not title:
        return {"error": "title 不能为空"}
    if not charter:
        return {"error": "职责书不能为空（写清这个角色对什么负责）"}
    dop = make_profile(title, charter, jurisdiction=jurisdiction)
    conflict = await _domain_conflict(
        work_id, str((dop.get("jurisdiction") or {}).get("exclusive_key") or ""))
    if conflict is not None:
        return {"error": (
            f"责任域已被角色 {conflict.get('role_id')} 占用；同一排他域只能有一个角色"
        )}
    body = (f"## 使命\n{charter}\n\n## 现状\n（值班中；判词与警报见角色档案）\n\n"
            "## 需求文档\n")
    created = await card_store.create_card(
        work_id, title=title, source="系统派生", card_type=ROLE_TYPE,
        parent="", charter=body, skip_kick=True,
    )
    if "error" in created:
        return created
    role_id = str((created.get("meta") or {}).get("card_id") or "")
    if not role_id:
        return {"error": "角色卡创建失败（无卡号）"}
    prof = make_profile(title, charter, jurisdiction=jurisdiction,
                        watch_cards=watch_cards, signals=signals,
                        speak_contract=speak_contract, escalation=escalation,
                        ratified_by=f"root:{root_id}")
    await role_store.ensure_profile(work_id, role_id, prof)
    watched: list[str] = []
    for cid in (watch_cards or []):
        cid = str(cid or "").strip()
        if cid and cid != role_id:
            res = await link(work_id, role_id, cid, actor=f"根卡 {root_id}")
            if res.get("ok"):
                watched.append(cid)
    session_ops.ops_log("role_create", work_id, role_id,
                        {"root": root_id, "title": title,
                         "jurisdiction": prof["jurisdiction"],
                         "watched": watched})
    return {"role_card_id": role_id, "title": title,
            "jurisdiction": prof["jurisdiction"], "watched": watched}


async def update_profile(work_id: str, role_id: str,
                         patch: dict[str, Any],
                         actor: str = "用户") -> dict[str, Any]:
    """更新档案；职责书文本变化走版本化，不覆盖历史。"""
    prof = await ensure_profile(work_id, role_id)
    if prof is None:
        return {"error": f"不是角色卡或档案缺失: {role_id}"}
    allowed = ("jurisdiction", "watch", "speak_contract", "escalation")
    changed: dict[str, Any] = {}
    if isinstance(patch.get("jurisdiction"), str):
        jur = str(patch["jurisdiction"]).strip()
        patch["jurisdiction"] = {"domain": _slug(jur), "statement": jur,
                                 "exclusive_key": _slug(jur)}
    # [排他键保全 2026-10-20 · card_004] 改档不得静默丢掉排他键。
    # 真机实测（work_001）：根卡只是想把职责范围从"三张接口"改成"四张接口"，
    # 调 update_role_profile 时只写了 {domain, statement} —— jurisdiction 是**整字典
    # 覆盖**（下面 changed[key] = patch[key]），于是 exclusive_key 被擦成 None。
    # 后果：①我新加的 175 区渲染出「域=?」，根卡失去判断"这域归谁"的凭据；
    # ②排他键从稳定标识退化成"靠 domain 兜底"，改域/改档多次后不再可追溯。
    # 合并规则（最小、可预期）：
    #   · 传了 exclusive_key ⇒ 听传的；
    #   · 没传、且 domain 没变（原地改档）⇒ 继承旧键；
    #   · 没传、但 domain 变了（有意换域）⇒ 键跟新 domain 走，不继承旧键。
    if isinstance(patch.get("jurisdiction"), dict):
        jur = dict(patch["jurisdiction"])
        old_jur = dict(prof.get("jurisdiction") or {})
        old_key = str(old_jur.get("exclusive_key") or "").strip()
        old_dom = str(old_jur.get("domain") or "").strip()
        new_dom = str(jur.get("domain") or "").strip()
        if not new_dom and old_dom:
            jur["domain"] = old_dom          # 只改 statement 时，domain 也要在
            new_dom = old_dom
        if not str(jur.get("exclusive_key") or "").strip():
            jur["exclusive_key"] = (old_key if (old_key and new_dom == old_dom)
                                    else (new_dom or old_key))
        patch["jurisdiction"] = jur

    for key in allowed:
        if key in patch and isinstance(patch[key], (dict, list)):
            changed[key] = patch[key]
    if isinstance(patch.get("jurisdiction"), dict):
        jur = patch["jurisdiction"]
        domain = str(jur.get("exclusive_key") or jur.get("domain") or "")
        conflict = await _domain_conflict(work_id, domain, exclude_role=role_id)
        if conflict is not None:
            return {"error": f"责任域 {domain} 已被角色 {conflict.get('role_id')} 占用"}
    charter_text = patch.get("charter")
    if isinstance(charter_text, str) and charter_text.strip():
        old = str((prof.get("charter") or {}).get("text") or "").strip()
        if charter_text.strip() != old:
            charter = await role_store.append_charter(
                work_id, role_id, charter_text.strip(), actor, "职责书修订")
            changed["charter"] = charter
            try:
                await card_store.update_card(
                    work_id, role_id, {"mission": charter_text.strip()},
                    f"[角色] {actor}修订职责书")
            except Exception as exc:  # noqa: BLE001 卡面同步失败不阻断档案
                logger.warning("[role] 职责书同步卡面失败 role=%s: %s", role_id, exc)
    if "watch" in changed and isinstance(changed["watch"], dict):
        cards = [str(c) for c in (changed["watch"].get("cards") or []) if c]
        signals = [s for s in (changed["watch"].get("signals") or []) if s in ALL_SIGNALS]
        changed["watch"] = {"cards": cards, "signals": signals or list(DEFAULT_SIGNALS)}
        # 关注集合＝声明范围；连线跟着声明走
        current = {str(m.get("card_id")) for m in await watched_cards(work_id, role_id)}
        for cid in cards:
            if cid not in current:
                await link(work_id, role_id, cid, actor=actor)
        for cid in current - set(cards):
            await unlink(work_id, role_id, cid, actor=actor)
    if changed:
        await role_store.update_profile(work_id, role_id, changed)
    return await role_summary(work_id, role_id)


async def set_duty(work_id: str, role_id: str, state: str,
                   actor: str = "用户", note: str = "") -> dict[str, Any]:
    if state not in ("active", "suspended", "archived"):
        return {"error": f"非法值班态: {state}"}
    prof = await ensure_profile(work_id, role_id)
    if prof is None:
        return {"error": f"不是角色卡或档案缺失: {role_id}"}
    if state == "archived":
        for m in await watched_cards(work_id, role_id):
            await unlink(work_id, role_id, str(m.get("card_id")), actor=actor)
    duty = await role_store.set_duty_state(work_id, role_id, state, note)
    session_ops.ops_log("role_duty", work_id, role_id,
                        {"state": state, "actor": actor, "note": note})
    if state == "active":
        spawn(work_id, role_id)
    return {"ok": True, "duty": duty}


# ============================================================
# 连线＝订阅
# ============================================================

async def watched_cards(work_id: str, role_id: str) -> list[dict[str, Any]]:
    prof = await ensure_profile(work_id, role_id)
    metas = card_store.iter_card_metas(work_id)
    ids: list[str] = []
    if prof is not None:
        ids = [str(c) for c in ((prof.get("watch") or {}).get("cards") or [])]
    if not ids:
        meta = _meta_of(work_id, role_id)
        ids = [str(r.get("target")) for r in (meta.get("links") or [])
               if isinstance(r, dict) and r.get("type") == LINK_TYPE_WATCH]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cid in ids:
        if not cid or cid in seen:
            continue
        seen.add(cid)
        m = metas.get(cid)
        if m:
            out.append(m)
    out.sort(key=lambda m: str(m.get("card_id") or ""))
    return out


async def roles_watching(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """关注这张工作卡的 active 角色（档案 watch.cards 为准）。"""
    return await _roles_watching(work_id, card_id, active_only=True)


async def roles_watching_any(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """关注这张工作卡的角色，**含停职的**（只排除已撤岗）。

    为什么需要它：停职只该挡住「起回合」，不该挡住「事实入队」。否则停职期间
    工作卡改了现状，角色永远补不上这一段——复职后看到的还是停职前的材料。
    起回合这一层由 `spawn()` 按 duty 状态把关，天然分开。
    """
    return await _roles_watching(work_id, card_id, active_only=False)


async def _roles_watching(work_id: str, card_id: str, *,
                          active_only: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in list_roles(work_id):
        rid = str(m.get("card_id") or "")
        prof = await ensure_profile(work_id, rid)
        if prof is None:
            continue
        state = str((prof.get("duty") or {}).get("state") or "active")
        if active_only and state != "active":
            continue
        if not active_only and state == "archived":
            continue
        cards = [str(c) for c in ((prof.get("watch") or {}).get("cards") or [])]
        if card_id in cards:
            out.append(m)
    return out


def linked(work_id: str, a: str, b: str) -> bool:
    """a、b 之间是否有「角色—工作」订阅边（任一方向；同步查档案，廉价格式）。"""
    am, bm = _meta_of(work_id, a), _meta_of(work_id, b)
    if str(am.get("card_type") or "") == ROLE_TYPE:
        return _profile_watches_sync(work_id, a, b)
    if str(bm.get("card_type") or "") == ROLE_TYPE:
        return _profile_watches_sync(work_id, b, a)
    return False


def _profile_watches_sync(work_id: str, role_id: str, card_id: str) -> bool:
    prof = role_store.get_profile(work_id, role_id)
    if prof is not None:
        return card_id in [str(c) for c in ((prof.get("watch") or {}).get("cards") or [])]
    meta = _meta_of(work_id, role_id)
    return any(isinstance(r, dict) and r.get("type") == LINK_TYPE_WATCH
               and str(r.get("target") or "") == card_id
               for r in (meta.get("links") or []))


async def _add_link(work_id: str, owner_id: str, rel: dict[str, Any],
                    actor: str) -> None:
    cur = await card_store.read_card(work_id, owner_id)
    links = list(cur["meta"].get("links") or [])
    for r in links:
        if (isinstance(r, dict) and r.get("target") == rel["target"]
                and r.get("type") == rel["type"]):
            return
    links.append(rel)
    await card_store.update_card(
        work_id, owner_id, {"links": links},
        f"[角色] {actor}建立订阅：{owner_id} ↔ {rel['target']}")


async def _drop_link(work_id: str, owner_id: str, target: str,
                     typ: str, actor: str) -> int:
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
            f"[角色] {actor}解除订阅：{owner_id} ↔ {target}")
    return removed


async def link(work_id: str, role_id: str, source_id: str,
               actor: str = "用户") -> dict[str, Any]:
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
    await ensure_profile(work_id, role_id)
    prof = role_store.get_profile(work_id, role_id) or {}
    watch = dict(prof.get("watch") or {})
    cards = [str(c) for c in (watch.get("cards") or [])]
    if source_id not in cards:
        cards.append(source_id)
    watch["cards"] = cards
    watch.setdefault("signals", list(DEFAULT_SIGNALS))
    await role_store.update_profile(work_id, role_id, {"watch": watch})
    await _add_link(work_id, role_id,
                    {"target": source_id, "type": LINK_TYPE_WATCH,
                     "note": f"由{actor}建立"}, actor)
    await _add_link(work_id, source_id,
                    {"target": role_id, "type": LINK_TYPE_ROLE,
                     "note": f"由{actor}建立"}, actor)
    # 建线即从「现在」起算：把当前末尾记为基线，不回灌历史
    last = await _last_seq(work_id, source_id)
    await _set_baseline(work_id, role_id, source_id, last)
    session_ops.ops_log("role_link", work_id, role_id,
                        {"source": source_id, "actor": actor})
    return {"ok": True, "role_id": role_id, "source_id": source_id}


async def unlink(work_id: str, role_id: str, source_id: str,
                 actor: str = "用户") -> dict[str, Any]:
    prof = role_store.get_profile(work_id, role_id) or {}
    watch = dict(prof.get("watch") or {})
    cards = [str(c) for c in (watch.get("cards") or []) if str(c) != source_id]
    watch["cards"] = cards
    await role_store.update_profile(work_id, role_id, {"watch": watch})
    a = await _drop_link(work_id, role_id, source_id, LINK_TYPE_WATCH, actor)
    b = await _drop_link(work_id, source_id, role_id, LINK_TYPE_ROLE, actor)
    session_ops.ops_log("role_unlink", work_id, role_id,
                        {"source": source_id, "actor": actor, "removed": a + b})
    return {"ok": True, "removed": a + b}


# ============================================================
# 同步（机械）与信号
# ============================================================

def _state_key(work_id: str, card_id: str, field: str) -> str:
    return f"role_state:{work_id}:{card_id}:{field}"


def _role_state_key(work_id: str, role_id: str, card_id: str, field: str) -> str:
    """按 (work, role, source, field) 记的内容哈希键——「这个角色看过这份内容没有」。"""
    return f"role_seen:{work_id}:{role_id}:{card_id}:{field}"


def _baseline_key(work_id: str, role_id: str, source: str) -> str:
    return f"role_base:{work_id}:{role_id}:{source}"


def _get_meta(work_id: str, key: str, default: str = "") -> str:
    from ..storage import card_db
    return str(card_db.read_sync(
        work_id, lambda conn: card_db.get_meta_value(conn, key, default), default) or "")


async def _set_meta(work_id: str, key: str, value: str) -> None:
    from ..storage import card_db

    def _fn(conn: Any) -> None:
        card_db.set_meta_value(conn, key, value)

    await card_db.run_write(work_id, _fn)


async def _set_baseline(work_id: str, role_id: str, source: str, seq: int) -> None:
    await _set_meta(work_id, _baseline_key(work_id, role_id, source), str(int(seq)))


def _baseline(work_id: str, role_id: str, source: str) -> int:
    raw = _get_meta(work_id, _baseline_key(work_id, role_id, source), "-1")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


async def _sync_from(work_id: str, role_id: str, source: str) -> int:
    base = _baseline(work_id, role_id, source)
    consumed = role_store.last_consumed_seq(work_id, role_id, source)
    return max(base, consumed) + 1


async def _turn_signals(work_id: str, card_id: str, result: Any) -> list[str]:
    signals = ["turn_completed"]
    try:
        card = await card_store.read_card(work_id, card_id)
        body = card.get("body") or {}
        status = str(body.get("status_block") or "")
        third = str(body.get("requirements") or body.get("requirements") or "")
    except Exception:  # noqa: BLE001
        return signals
    for field, value, sig in (("status", status, "status_changed"),
                              ("third", third, "decision_appended")):
        key = _state_key(work_id, card_id, field)
        prev = _get_meta(work_id, key, "")
        now = _hash(value)
        if prev and prev != now:
            signals.append(sig)
        # 首次只建立基线，不把「没有历史」误判成「变了」
        await _set_meta(work_id, key, now)
    if not isinstance(result, dict):
        signals.append("turn_error")
    return signals


def schedule_card_changed(work_id: str, card_id: str, updates: dict[str, Any]) -> None:
    """卡片正文块变更 → 排一个异步任务通知关注角色（同步函数，给存储层调）。

    `card_store.update_card` 是唯一写门，它在事务提交后调本函数。这里只负责
    「拿到 running loop 就排任务」——绝不阻塞、绝不抛出：角色同步失败不能
    影响写卡结果。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(_notify_card_changed(work_id, card_id, dict(updates or {})))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[role] 排卡片变更通知失败 card=%s: %s", card_id, exc)


async def _notify_card_changed(work_id: str, card_id: str,
                              updates: dict[str, Any]) -> None:
    """正文块（现状/需求文档）被改 ⇒ 按角色声明的信号入队，再按值班态起/不起回合。

    三个口径：
    - **去重按 (work, role, source, field)** 记内容哈希：同一份内容重复 PATCH 不重复
      入队；不同角色各记各的（旧 `_state_key` 是**按卡**记的，两个角色订同一张卡时
      后一个会被前一个写下的哈希挡掉）；
    - **停职也要入队**（`roles_watching_any`，只排除 archived）：停职只该挡住起回合，
      不该挡住事实入队——否则复职后永远补不上停职那一段；
    - **无会话块的卡走快照口径**（from_seq=to_seq=0）：这类卡没有回合，材料由
      `build_batch` 按信号补「【当前现状】/【当前第三块】」。
    队列语义是「有一份新东西给你」，同一来源同时有一条 pending 就够（`add_sync`
    幂等），多次改动会合并成一次唤醒——与「同步按回合收口、不按事件逐条推」一致。
    """
    try:
        meta = _meta_of(work_id, card_id)
        if not meta or str(meta.get("card_type") or "") == ROLE_TYPE:
            return
        # 有会话块的工作卡：同步按「回合收口」走（`after_turn`），这里不插手——
        # 既避免一次改动触发两路同步/两起火，也保住「按回合收口、不按事件逐条推」
        # 的既有口径。这里只补 `after_turn` 够不着的那一类：**没有会话块的卡**
        # （只有现状/需求文档、用户直接改出来的卡）——它们没有回合，永远等不到收口。
        last = await _last_seq(work_id, card_id)
        if last >= 0:
            return
        roles = await roles_watching_any(work_id, card_id)
        if not roles:
            return
        card = await card_store.read_card(work_id, card_id)
        body = card.get("body") or {}
        status_h = _hash(str(body.get("status_block") or ""))
        third_h = _hash(str(body.get("requirements") or body.get("requirements") or ""))
        for role in roles:
            role_id = str(role.get("card_id") or "")
            if not role_id:
                continue
            prof = role_store.get_profile(work_id, role_id) or {}
            wanted = set((prof.get("watch") or {}).get("signals") or DEFAULT_SIGNALS)
            hit: list[str] = []
            if "status_block" in updates and "status_changed" in wanted:
                key = _role_state_key(work_id, role_id, card_id, "status")
                if _get_meta(work_id, key, "") != status_h:
                    await _set_meta(work_id, key, status_h)
                    hit.append("status_changed")
            if "requirements" in updates and "decision_appended" in wanted:
                key = _role_state_key(work_id, role_id, card_id, "third")
                if _get_meta(work_id, key, "") != third_h:
                    await _set_meta(work_id, key, third_h)
                    hit.append("decision_appended")
            if not hit:
                continue
            async with _lock_for(work_id, role_id):
                if last < 0:
                    await role_store.add_sync(work_id, role_id, card_id, 0, 0, hit)
                else:
                    from_seq = await _sync_from(work_id, role_id, card_id)
                    if last >= from_seq:
                        await role_store.add_sync(work_id, role_id, card_id,
                                                  from_seq, last, hit)
            spawn(work_id, role_id)
    except Exception as exc:  # noqa: BLE001 通知失败不得影响写卡/回合
        logger.warning("[role] 卡片变更通知失败 work=%s card=%s: %s",
                       work_id, card_id, exc)


async def after_turn(work_id: str, card_id: str, result: Any = None) -> None:
    """工作卡回合收口：按角色声明的信号决定是否同步、是否唤醒。永不抛出。"""
    try:
        meta = _meta_of(work_id, card_id)
        if not meta or str(meta.get("card_type") or "") == ROLE_TYPE:
            return
        roles = await roles_watching(work_id, card_id)
        if not roles:
            return
        last = await _last_seq(work_id, card_id)
        # [role-sync 2026-10-20 · card_004] 无会话块不再直接放弃：这类卡一样会改
        # 现状/需求文档，第二层角色必须看得到。快照口径（0..0）在下面分支覆盖。
        signals = await _turn_signals(work_id, card_id, result)
        for role in roles:
            role_id = str(role.get("card_id") or "")
            if not role_id:
                continue
            prof = role_store.get_profile(work_id, role_id) or {}
            wanted = set((prof.get("watch") or {}).get("signals") or DEFAULT_SIGNALS)
            hit = [s for s in signals if s in wanted]
            if not hit:
                session_ops.ops_log("role_sync_skipped", work_id, role_id,
                                    {"source": card_id, "signals": signals})
                continue
            if last < 0:
                # 快照口径：无会话块 ⇒ 目标物就是「当前现状 / 当前第三块」
                async with _lock_for(work_id, role_id):
                    await role_store.add_sync(work_id, role_id, card_id, 0, 0, hit)
                spawn(work_id, role_id)
                continue
            from_seq = await _sync_from(work_id, role_id, card_id)
            if last < from_seq:
                continue
            async with _lock_for(work_id, role_id):
                await role_store.add_sync(work_id, role_id, card_id,
                                          from_seq, last, hit)
            spawn(work_id, role_id)
    except Exception as exc:  # noqa: BLE001 同步失败不得吃掉工作卡回合
        logger.warning("[role] after_turn 同步失败 work=%s card=%s: %s",
                       work_id, card_id, exc)


def pending_batch(work_id: str, role_id: str) -> list[dict[str, Any]]:
    rows = role_store.pending_syncs(work_id, role_id)
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        src = str(row.get("source") or "")
        item = by_source.setdefault(src, {
            "source": src,
            "from_seq": int(row.get("from_seq") or 0),
            "to_seq": int(row.get("to_seq") or 0),
            "signals": [],
            "ids": [],
        })
        item["from_seq"] = min(item["from_seq"], int(row.get("from_seq") or 0))
        item["to_seq"] = max(item["to_seq"], int(row.get("to_seq") or 0))
        item["ids"].append(int(row.get("seq") or 0))
        for s in (row.get("signals") or []):
            if s not in item["signals"]:
                item["signals"].append(s)
    out = list(by_source.values())
    out.sort(key=lambda x: x["source"])
    return out


async def build_batch(work_id: str, role_id: str) -> list[dict[str, Any]]:
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
        # 状态/需求文档变化不在会话块里：按信号把当前正文补进材料，否则
        # “status_changed” 唤醒角色却看不到新现状，等于空跑。
        try:
            card = await card_store.read_card(work_id, src)
            cb = card.get("body") or {}
            if "status_changed" in (item.get("signals") or []):
                st = str(cb.get("status_block") or "").strip()
                if st:
                    lines.append("【当前现状】" + st[:MAX_BLOCK_CHARS])
            if "decision_appended" in (item.get("signals") or []):
                third = str(cb.get("requirements") or cb.get("requirements") or "").strip()
                if third:
                    lines.append("【当前第三块】" + third[:MAX_BLOCK_CHARS])
        except Exception:  # noqa: BLE001
            pass
        length = 0
        for b in picked:
            actor = str(b.get("actor") or b.get("role") or "?")
            text = str(b.get("content") or "").strip()
            if len(text) > MAX_BLOCK_CHARS:
                text = text[:MAX_BLOCK_CHARS] + "……（截断）"
            piece = f"[{actor}] {text}" if text else f"[{actor}]（空正文）"
            if total + length + len(piece) > MAX_BATCH_CHARS:
                lines.append("……（本批材料超长，已截断；完整内容见源卡会话）")
                break
            lines.append(piece)
            length += len(piece)
        total += length
        if not lines:
            try:
                card = await card_store.read_card(work_id, src)
                st = str((card.get("body") or {}).get("status_block") or "").strip()
                if st:
                    lines.append("【当前现状】" + st[:MAX_BLOCK_CHARS])
            except Exception:  # noqa: BLE001
                pass
        out.append({**item, "title": str(meta.get("title") or ""),
                    "blocks": len(picked),
                    "text": "\n".join(lines) if lines else "（该区间没有可读块）"})
    return out


# ============================================================
# 判词 / 警报
# ============================================================

def active_batch(work_id: str, role_id: str) -> list[dict[str, Any]]:
    item = _ACTIVE.get((work_id, role_id)) or {}
    batch = item.get("batch") or []
    return batch if isinstance(batch, list) else []


async def record_verdict(work_id: str, role_id: str, decision: str, reason: str,
                         targets: Optional[list[str]] = None,
                         evidence: Optional[list[str]] = None,
                         batch: Optional[list[dict[str, Any]]] = None,
                         auto: bool = False,
                         supersedes: str = "") -> dict[str, Any]:
    decision = str(decision or "").strip().lower()
    if decision not in ("speak", "silent"):
        decision = "silent"
    batch = batch if batch is not None else active_batch(work_id, role_id)
    sources = [str(i.get("source") or "") for i in batch]
    rec = await role_store.add_verdict(
        work_id, role_id, decision, str(reason or "").strip(),
        sources, [str(t) for t in (targets or [])],
        [str(e) for e in (evidence or [])], auto=auto)
    if supersedes:
        await role_store.supersede_verdict(work_id, supersedes, rec["id"])
        rec["supersedes"] = supersedes
    session_ops.ops_log("role_verdict", work_id, role_id, {
        "decision": decision, "reason": rec["reason"][:160],
        "targets": rec["targets"], "auto": rec["auto"],
    })
    return rec


async def list_verdicts(work_id: str, role_id: str,
                        limit: int = 30) -> list[dict[str, Any]]:
    return role_store.list_verdicts(work_id, role_id, limit)


async def raise_alarm(work_id: str, role_id: str, severity: str, title: str,
                      detail: str, supersedes: str = "",
                      question: str = "", options: Optional[list[str]] = None,
                      recommendation: str = "", default_action: str = "",
                      deadline: str = "") -> dict[str, Any]:
    """报警＝**决策请求**：只给“只有用户能决定”的事，必须带问题与默认动作。

    技术问题不进报警——角色应下指令/自己处理/让卡返工；把技术细节丢给不懂技术
    的用户，等于角色没有负责。
    """
    q = str(question or "").strip()
    default = str(default_action or "").strip()
    if not q:
        return {"error": "报警必须是决策请求：question 写清‘用户要决定什么’；技术问题请用指令"}
    if not default:
        return {"error": "报警必须带 default_action：用户不回复时角色按什么默认动作执行"}
    severity = str(severity or "warn").strip().lower()
    if severity not in ("info", "warn", "critical"):
        severity = "warn"
    rec = await role_store.add_alarm(
        work_id, role_id, severity,
        str(title or "").strip() or "角色决策请求",
        str(detail or "").strip(), supersedes,
        question=q, options=[str(x) for x in (options or [])],
        recommendation=str(recommendation or "").strip(),
        default_action=default, deadline=str(deadline or "").strip())
    rec["role_title"] = str(_meta_of(work_id, role_id).get("title") or "")
    session_ops.ops_log("role_alarm", work_id, role_id, {
        "severity": severity, "title": rec["title"][:120],
        "supersedes": supersedes,
    })
    try:
        from . import event_stream
        event_stream.emit(work_id, role_id, "role_alarm", {
            "id": rec["id"], "severity": severity,
            "title": rec["title"], "detail": rec["detail"][:500],
            "supersedes": supersedes,
        })
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    return rec


def list_alarms(work_id: str, status: str = "open",
                role_id: str = "") -> list[dict[str, Any]]:
    return role_store.list_alarms(work_id, status=status, role_id=role_id)


async def ack_alarm(work_id: str, alarm_id: str) -> bool:
    return await role_store.ack_alarm(work_id, alarm_id)


async def correct_alarm(work_id: str, alarm_id: str,
                        corrected_by: str) -> bool:
    return await role_store.correct_alarm(work_id, alarm_id, corrected_by)


# ============================================================
# 唤醒与收口
# ============================================================

def _batch_ref(batch: list[dict[str, Any]]) -> str:
    if not batch:
        return ""
    return ",".join(f"{i['source']}:{i['from_seq']}-{i['to_seq']}" for i in batch)


def _render_sections(prof: dict[str, Any],
                     verdicts: list[dict[str, Any]],
                     alarms: list[dict[str, Any]],
                     pending: list[str],
                     directives: Optional[list[dict[str, Any]]] = None) -> str:
    jur = prof.get("jurisdiction") or {}
    watch = prof.get("watch") or {}
    sc = prof.get("speak_contract") or {}
    esc = prof.get("escalation") or {}
    duty = prof.get("duty") or {}
    charter = prof.get("charter") or {}
    lines = [
        f"## 管辖\n{jur.get('statement') or '（未写）'}\n"
        f"责任域: {jur.get('domain','')}（排他键 {jur.get('exclusive_key','')}）",
        "## 关注\n"
        f"工作卡: {', '.join(watch.get('cards') or []) or '（无）'}\n"
        f"关注信号: {', '.join(watch.get('signals') or []) or '（无）'}",
        "## 发言契约\n"
        f"建议条件: {', '.join(sc.get('advice_when') or []) or '（未写）'}\n"
        f"警报条件: {', '.join(sc.get('alarm_when') or []) or '（未写）'}\n"
        f"默认沉默: {bool(sc.get('silence_default', True))}；"
        f"单次最多发言: {sc.get('max_messages_per_wake', 2)}",
        "## 升级线\n"
        f"建议→{esc.get('advice_to','source_card')}；"
        f"警报→{esc.get('alarm_to','human')}；"
        f"严重→{esc.get('critical_to','root_and_human')}",
        "## 值班\n"
        f"状态: {duty.get('state','active')}；最后巡检: {duty.get('last_wake_at','') or '（无）'}；"
        f"待同步: {len(pending)} 源",
        "## 判断簿\n" + ("\n".join(
            f"- {v['ts'][:16]} {v['decision']} · {v['reason'][:160]}"
            + (f" → 被 {v['superseded_by']} 取代" if v.get("superseded_by") else "")
            for v in verdicts[:10]) or "（暂无）"),
        "## 指令簿\n" + ("\n".join(
            f"- {d['created_at'][:16]} [{d['status']}] → {d['target_card_id']}："
            f"{d['instruction'][:120]}"
            + (f"（验收：{d['acceptance'][:80]}）" if d.get("acceptance") else "")
            for d in (directives or [])[:10]) or "（暂无）"),
        "## 警报簿\n" + ("\n".join(
            f"- {a['ts'][:16]} [{a['severity']}/{a['status']}] {a['title']}"
            + (f"（更正→{a['corrected_by']}）" if a.get("corrected_by") else "")
            for a in alarms[:10]) or "（暂无）"),
        f"## 职责书（v{charter.get('version', 1)}）\n{charter.get('text','')}",
    ]
    return "\n\n".join(lines)


def build_prompt(work_id: str, role_id: str,
                 batch: list[dict[str, Any]]) -> str:
    prof = role_store.get_profile(work_id, role_id) or {}
    title = str(_meta_of(work_id, role_id).get("title") or role_id)
    verdicts = role_store.list_verdicts(work_id, role_id, 15)
    alarms = role_store.list_alarms(work_id, status="", role_id=role_id)
    pending = [i["source"] for i in pending_batch(work_id, role_id)]
    directives = role_store.list_directives(work_id, role_id)
    body = _render_sections(prof, verdicts, alarms, pending, directives)
    parts = [
        f"〔角色巡检〕你是本项目的角色卡《{title}》（{role_id}）。",
        "你不是作业卡：你不做具体工作，你对你负责的管辖域保持观察、判断，"
        "只在必要时给出建议或警报。",
        "",
        body,
        "",
        "## 自上次巡检以来，你关注的工作卡新增了以下内容",
    ]
    for item in batch:
        parts.append(
            f"### {item['source']}《{item['title']}》 块 {item['from_seq']}..{item['to_seq']}"
            f"（命中信号：{', '.join(item.get('signals') or []) or 'turn_completed'}）")
        parts.append(item["text"])
    parts += [
        "",
        "## 本轮必须做的事",
        "1. 判断：这些新增内容里，有没有落在你的管辖域、且触发发言契约的事？",
        "2. 若发言：只是让最相关的工作卡**知道**一件事 → sync(content)（沿通信线、不叫醒它）；"
        "要它**现在就改** → direct(target_card_id, instruction, acceptance)（带验收判据，会把它叫起来）。",
        "3. 只能写**你自己**的档案/职责书（edit_status/edit_requirements）；"
        "不得修改别人的卡、不得建卡、不得调文件或命令。",
        "4. 没有新增风险就沉默，不要为了说话而说话。",
        # [2026-10-31 用户令「判断、警报我明确不需要，拆掉」] 原来第 2 条是
        # 「无论发言还是沉默都必须调 record_verdict 落账」——工具已删，这条
        # 指令会直接指向一个不存在的工具，故整条移除；警报同理。
    ]
    return "\n".join(parts)


async def _finalize(work_id: str, role_id: str,
                    batch: list[dict[str, Any]],
                    before: int, resp: Any) -> None:
    key = (work_id, role_id)
    _ACTIVE.pop(key, None)
    ok = isinstance(resp, dict)
    if not ok:
        session_ops.ops_log("role_turn_failed", work_id, role_id,
                            {"batch": _batch_ref(batch)})
        logger.warning("[role] 巡检回合失败，同步队列保留 role=%s", role_id)
        return
    ids = [int(i) for item in batch for i in (item.get("ids") or [])]
    try:
        await role_store.consume_syncs(work_id, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[role] 同步队列收口失败 role=%s: %s", role_id, exc)
    # [2026-10-31 用户令「判断、警报我明确不需要，拆掉」] 判断簿随
    # record_verdict 一起退役 ⇒ 不再「本轮没落账就机械补记一条 silent」。
    # 那条补记的唯一作用是逼模型调 record_verdict；工具已删，补记只会往
    # 退役的表里灌噪音。收口只记「巡检了几源」，不再产出判词。
    decision = "silent"
    reason = ""
    try:
        # [role-sync 2026-10-20 · card_007] 只合并记账字段，不整字典写回：
        # 旧写法拿旧 duty 快照整体覆盖，与 set_duty 并发时把刚改的 state
        #（如复职 active）冲回旧值（丢更新），复职补巡检从此静默不起。
        await role_store.touch_duty(
            work_id, role_id, last_wake_at=_now(),
            pending_sync=len(pending_batch(work_id, role_id)))
    except Exception:  # noqa: BLE001
        pass
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
            "auto": False,
        })
    except Exception:  # noqa: BLE001
        pass
    try:
        if pending_batch(work_id, role_id):
            from . import message_runner as _mr

            async def _followup() -> None:
                await asyncio.sleep(SPAWN_COOLDOWN + 0.1)
                spawn(work_id, role_id)

            _mr._spawn_tracked(_followup())
    except Exception:  # noqa: BLE001
        pass


def spawn(work_id: str, role_id: str) -> bool:
    """起一次角色巡检回合（异步，不阻塞调用方）。"""
    key = (work_id, role_id)
    if key in _ACTIVE:
        return False
    prof = role_store.get_profile(work_id, role_id)
    if prof is None:
        return False
    if str((prof.get("duty") or {}).get("state") or "active") != "active":
        return False
    now = asyncio.get_event_loop().time()
    if now - _LAST_SPAWN.get(key, 0.0) < SPAWN_COOLDOWN:
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
    before = len(role_store.list_verdicts(work_id, role_id, 1000))
    try:
        from . import message_runner as _mr
        from . import routes as _routes

        async def _run() -> None:
            try:
                material = await build_batch(work_id, role_id)
                _ACTIVE[key]["batch"] = material
                real_prompt = build_prompt(work_id, role_id, material)
                resp = await _routes._run_message(
                    work_id, role_id, real_prompt, None, append_user=False)
                await _finalize(work_id, role_id, material, before, resp)
            except Exception as exc:  # noqa: BLE001 单次巡检失败不拖死角色
                _ACTIVE.pop(key, None)
                logger.error("[role] 巡检回合异常 role=%s: %s", role_id, exc)
                session_ops.ops_log("role_turn_error", work_id, role_id,
                                    {"error": str(exc)[:200]})

        _mr._spawn_tracked(_run())
        return True
    except Exception as exc:  # noqa: BLE001
        _ACTIVE.pop(key, None)
        logger.error("[role] 起巡检回合失败 role=%s: %s", role_id, exc)
        return False


async def manual_check(work_id: str, role_id: str) -> dict[str, Any]:
    """人工触发一次巡检：把关注卡的新增补进队列，再起回合。"""
    prof = await ensure_profile(work_id, role_id)
    if prof is None:
        return {"error": f"不是角色卡或档案缺失: {role_id}"}
    for m in await watched_cards(work_id, role_id):
        src = str(m.get("card_id") or "")
        if not src:
            continue
        # 已有待消费同步 ⇒ 不重复灌（同一份材料进两次队只是让角色多看一遍）
        if any(str(p.get("source") or "") == src
               for p in role_store.pending_syncs(work_id, role_id)):
            continue
        last = await _last_seq(work_id, src)
        if last < 0:
            # 无会话块（用户直接改现状/预置场景）：快照口径入队（0..0）。
            # 旧写法把 last 归 0 再与 from_seq(≥1) 比较，0>=1 恒假 ——
            # 那段「兜底」从来没生效过，新现状进不了队。
            await role_store.add_sync(
                work_id, role_id, src, 0, 0,
                ["manual_check", "status_changed", "decision_appended"])
            continue
        from_seq = await _sync_from(work_id, role_id, src)
        if last >= from_seq:
            await role_store.add_sync(
                work_id, role_id, src, from_seq, last,
                ["manual_check", "status_changed", "decision_appended"])
    started = spawn(work_id, role_id)
    return {"started": started,
            "pending_sources": [p["source"] for p in pending_batch(work_id, role_id)]}


# ============================================================
# 指令（角色 -> 关注卡，直接、可验收）
# ============================================================

async def record_directive(work_id: str, role_id: str, target_card_id: str,
                           instruction: str, acceptance: str,
                           msg_id: str = "", due_at: str = "",
                           parent_id: str = "", attempt: int = 1) -> dict[str, Any]:
    rec = await role_store.add_directive(
        work_id, role_id, target_card_id, instruction, acceptance, msg_id,
        due_at, parent_id=parent_id, attempt=attempt)
    session_ops.ops_log("role_directive", work_id, role_id, {
        "directive_id": rec["id"], "target": target_card_id,
        "instruction": instruction[:160],
    })
    return rec


async def verify_directive(work_id: str, role_id: str, directive_id: str,
                           status: str, evidence: str = "") -> dict[str, Any]:
    await role_store.refresh_overdue(work_id, role_id)
    rec = role_store.get_directive(work_id, directive_id)
    if rec is None or str(rec.get("role_id") or "") != role_id:
        return {"error": f"指令不存在或不属于该角色: {directive_id}"}
    ok = await role_store.verify_directive(work_id, directive_id, status, evidence)
    if not ok:
        return {"error": f"验收失败（status={status}）"}
    rec = role_store.get_directive(work_id, directive_id) or rec
    session_ops.ops_log("role_directive_verify", work_id, role_id, {
        "directive_id": directive_id, "status": status,
    })
    return {"ok": True, "directive": rec}


# ============================================================
# 查询（UI / 模型）
# ============================================================

def open_directives_for(work_id: str, target_card_id: str) -> list[dict[str, Any]]:
    """目标卡上未验收的指令（issued/rejected/overdue）——收口闸的判据。"""
    return role_store.list_open_directives(work_id, target_card_id)


async def role_summary(work_id: str, role_id: str) -> dict[str, Any]:
    prof = await ensure_profile(work_id, role_id)
    if prof is None:
        return {"role_id": role_id, "error": "不是角色卡或档案缺失"}
    await role_store.refresh_overdue(work_id, role_id)
    meta = _meta_of(work_id, role_id)
    verdicts = role_store.list_verdicts(work_id, role_id, 30)
    alarms = role_store.list_alarms(work_id, status="", role_id=role_id)
    directives = role_store.list_directives(work_id, role_id)
    open_alarms = [a for a in alarms if a.get("status") == "open"]
    pending = pending_batch(work_id, role_id)
    watch = prof.get("watch") or {}
    watched = []
    metas = card_store.iter_card_metas(work_id)
    for cid in (watch.get("cards") or []):
        m = metas.get(str(cid))
        if m:
            watched.append({"card_id": str(m.get("card_id")),
                            "title": str(m.get("title") or ""),
                            "card_type": str(m.get("card_type") or "")})
    charter = prof.get("charter") or {}
    history = await role_store.charter_history(work_id, role_id, 10)
    duty = dict(prof.get("duty") or {})
    duty["pending_sync"] = len(pending)
    return {
        "role_id": role_id,
        "title": str(meta.get("title") or ""),
        "card_type": str(meta.get("card_type") or ""),
        "profile": prof,
        "jurisdiction": prof.get("jurisdiction") or {},
        "watch": {"cards": watched, "signals": watch.get("signals") or []},
        "speak_contract": prof.get("speak_contract") or {},
        "escalation": prof.get("escalation") or {},
        "duty": duty,
        "charter": {"version": charter.get("version", 1),
                    "text": charter.get("text", ""),
                    "ratified_by": charter.get("ratified_by", ""),
                    "history": history},
        "verdicts": verdicts,
        "directives": directives,
        "open_directives": [d for d in directives
                            if d.get("status") in ("issued", "rejected", "overdue")],
        "alarms": alarms,
        "open_alarms": open_alarms,
        "pending_sources": [p["source"] for p in pending],
        "text": _render_sections(prof, verdicts, alarms,
                                 [p["source"] for p in pending], directives),
    }


async def role_text(work_id: str, role_id: str) -> str:
    """给 read_card / UI 的角色六节正文。"""
    summary = await role_summary(work_id, role_id)
    return str(summary.get("text") or "")


async def startup_sweep_all(limit: int = 20) -> int:
    """进程启动后把「有 pending 同步的 active 角色」重新唤醒。

    同步队列与判断/警报台账都在 SQLite，重启不丢；本函数只负责续跑。
    fail-open：任何单个 work/角色失败不影响其他。
    """
    from ..storage import work_store

    started = 0
    try:
        works = work_store.list_works()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[role] 启动续跑取 work 列表失败: %s", exc)
        return 0
    for w in works[:50]:
        wid = str(w.get("work_id") or "")
        if not wid:
            continue
        try:
            for prof in await role_store.list_profiles(wid):
                rid = str(prof.get("role_id") or "")
                if not rid:
                    continue
                if str((prof.get("duty") or {}).get("state") or "active") != "active":
                    continue
                if not pending_batch(wid, rid):
                    continue
                if spawn(wid, rid):
                    started += 1
                if started >= limit:
                    return started
                await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[role] 启动续跑 work=%s 失败: %s", wid, exc)
    return started
