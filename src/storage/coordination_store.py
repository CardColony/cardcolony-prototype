# -*- coding: utf-8 -*-
"""项目统筹（coordination）的持久化层——计划、任命与版本史。

[项目统筹职能试点 2026-11-21 · 任务一] 本模块只做**机械事实**：计划怎么存、
任命状态机怎么转、版本怎么比。谁被叫醒、何时叫醒、发令——判断归模型、
调度归任务三，本层不掺。

两表（每 work 库内，事务内幂等补建——照 function_store._ensure_deployments
先例，不动 card_db._ensure_tables 中心注册点）：

- ``coordination_appointments``：任命行。归属键＝``work_id + plan_card_id``
  （一张计划卡一份当前计划）；``assignment_id`` 每次任命新生成（撤回后
  重新任命是新任命，旧令牌随 op_seq 递增天然失效）。
- ``coordination_plan_revisions``：计划历史版本（每次计划保存插一行）。

唯一性约束（**写事务内**保证，并发下不会双开）：
- 同一张计划卡同时至多一个 active/paused 任命（一份计划一个负责人；
  [2026-11-21 · 用户令] 唯一性是**计划卡级**不是 work 级——同一 work 可以
  有多份计划，各计划各管各的。想换负责人先撤回）。
- revoked/finished 的旧任命保留（历史可回看），重新任命产生新 assignment_id。

状态机（任务书任务一第 4 条）：
    appoint → active ──pause──→ paused ──resume──→ active
                  └──revoke──→ revoked（终态）   └──revoke/finish──→ 终态
控制变更（pause/resume/revoke/finish/set_mode/set_grant/计划修改）递增
``op_seq``（旧执行令牌失效）与计划修改的 ``rev``；普通观察与回执入账不递增。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from . import card_db

logger = logging.getLogger("storage.coordination_store")

# ---------------------------------------------------------------- 常量

ACTIVE_STATES = ("active", "paused")
ALL_STATES = ("active", "paused", "revoked", "finished")
MODES = ("observe", "live")

#: 观察范围口径：整个 work（含后来加入的卡）或显式卡集合
OBSERVE_SCOPES = ("work", "cards")
#: 发令范围口径：显式目标集合或用户明确授予的本 work 全部开发卡
INSTRUCT_SCOPES = ("cards", "work_development_cards")


class CoordinationError(Exception):
    """带 HTTP 语义的协调层错误（routes 捕获后转 _err）。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code          # not_found / validation / conflict / denied
        self.message = message
        self.extra = dict(extra or {})


def _now() -> str:
    """ISO 时间戳（UTC+8，与职能/会话层同形态）。"""
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _parse_future_time(ts: str, field: str = "next_review_at") -> datetime:
    """解析并校验「带时区、指向将来」的绝对时间（任命硬要求）。"""
    raw = str(ts or "").strip()
    if not raw:
        raise CoordinationError("validation", f"{field} 不能为空（须是带时区的将来时间）")
    txt = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError as exc:
        raise CoordinationError("validation",
                                f"{field} 不是合法时间: {raw}") from exc
    if dt.tzinfo is None:
        raise CoordinationError("validation",
                                f"{field} 必须带时区（如 2026-11-21T10:30:00+08:00）: {raw}")
    if dt <= datetime.now(dt.tzinfo):
        raise CoordinationError("validation",
                                f"{field} 必须是将来的时间: {raw}")
    return dt


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------- DDL

_APPOINTMENTS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_appointments ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL UNIQUE,"
    " function_id TEXT NOT NULL,"
    " state TEXT NOT NULL DEFAULT 'active',"
    " mode TEXT NOT NULL DEFAULT 'observe',"
    " rev INTEGER NOT NULL DEFAULT 0,"
    " plan TEXT NOT NULL DEFAULT '',"
    " observe_scope TEXT NOT NULL DEFAULT '',"
    " instruct_scope TEXT NOT NULL DEFAULT '',"
    " models TEXT NOT NULL DEFAULT '',"
    " next_review_at TEXT NOT NULL DEFAULT '',"
    " op_seq INTEGER NOT NULL DEFAULT 0,"
    " pending TEXT NOT NULL DEFAULT '[]',"
    " actions TEXT NOT NULL DEFAULT '[]',"
    " last_review TEXT NOT NULL DEFAULT '',"
    " created_at TEXT, updated_at TEXT)"
)
_APPOINTMENTS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_coord_appt_card"
    " ON coordination_appointments(work_id, plan_card_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_coord_appt_state"
    " ON coordination_appointments(work_id, state)",
)
_REVISIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_plan_revisions ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL,"
    " rev INTEGER NOT NULL,"
    " plan TEXT NOT NULL,"
    " saved_at TEXT, saved_by TEXT,"
    " detail TEXT NOT NULL DEFAULT '')"
)
# [2026-11-23 · 计划板] 既有库补列（CREATE 不覆盖已存在表）
_REVISIONS_ALTERS = ("detail TEXT NOT NULL DEFAULT ''",)
_REVISIONS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_coord_rev_card"
    " ON coordination_plan_revisions(work_id, plan_card_id, rev)",
)

# [任务二 2026-11-21] 两种模型的调用账本：弱模型观察（一次只读调用）与
# 强模型起轮（首轮拟计划）都记这里——实际路由、用量、判断结论、未判断原因。
_OBSERVATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_observations ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL,"
    " kind TEXT NOT NULL,"            # weak_observation / strong_turn
    " batch_id TEXT NOT NULL DEFAULT '',"
    " decision TEXT NOT NULL DEFAULT '',"   # wake/pass（弱模型）
    " item_id TEXT NOT NULL DEFAULT '',"
    " status TEXT NOT NULL,"          # wake/passed/unjudged/parsed/unparsed/error
    " reason TEXT NOT NULL DEFAULT '',"
    " evidence_refs TEXT NOT NULL DEFAULT '[]',"
    " question TEXT NOT NULL DEFAULT '',"
    " model_used TEXT NOT NULL DEFAULT '',"
    " usage TEXT NOT NULL DEFAULT '',"
    " detail TEXT NOT NULL DEFAULT '',"
    " created_at TEXT, updated_at TEXT)"
)
_OBSERVATIONS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_coord_obs_card"
    " ON coordination_observations(work_id, plan_card_id, created_at)",
)


def _ensure(conn: sqlite3.Connection) -> None:
    """事务内幂等补建（不能用 executescript——它隐式提交事务）。"""
    conn.execute(_APPOINTMENTS_DDL)
    for idx in _APPOINTMENTS_IDX:
        conn.execute(idx)
    conn.execute(_REVISIONS_DDL)
    for idx in _REVISIONS_IDX:
        conn.execute(idx)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(coordination_plan_revisions)").fetchall()}
    for col_sql in _REVISIONS_ALTERS:
        name = col_sql.split()[0]
        if name not in cols:
            conn.execute(
                f"ALTER TABLE coordination_plan_revisions ADD COLUMN {col_sql}")
    conn.execute(_OBSERVATIONS_DDL)
    for idx in _OBSERVATIONS_IDX:
        conn.execute(idx)


def _row_to_appt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "work_id": row["work_id"],
        "plan_card_id": row["plan_card_id"],
        "assignment_id": row["assignment_id"],
        "function_id": row["function_id"],
        "state": row["state"],
        "mode": row["mode"],
        "rev": int(row["rev"]),
        "plan": _loads(row["plan"], {}),
        "observe_scope": _loads(row["observe_scope"], {}),
        "instruct_scope": _loads(row["instruct_scope"], {}),
        "models": _loads(row["models"], {}),
        "next_review_at": row["next_review_at"],
        "op_seq": int(row["op_seq"]),
        "pending": _loads(row["pending"], []),
        "actions": _loads(row["actions"], []),
        "last_review": _loads(row["last_review"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------- 输入校验

def _validate_scope(scope: Any, kinds: tuple[str, ...], field: str) -> dict[str, Any]:
    """范围字段：{"scope": kind, "ids": [...]}。cards 口径必须带非空 ids。"""
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise CoordinationError("validation", f"{field} 必须是对象")
    kind = str(scope.get("scope") or "").strip()
    if kind not in kinds:
        raise CoordinationError("validation",
                                f"{field}.scope 非法: {kind!r}（允许 {'/'.join(kinds)}）")
    out: dict[str, Any] = {"scope": kind}
    if kind == "cards":
        ids = [str(x).strip() for x in (scope.get("ids") or []) if str(x).strip()]
        if not ids:
            raise CoordinationError("validation",
                                    f"{field}.scope=cards 时 ids 不能为空")
        out["ids"] = ids
    return out


def _validate_models(models: Any) -> dict[str, str]:
    """两条模型路由必须都给（弱模型判断/强模型起轮依赖它们）。"""
    strong = str((models or {}).get("strong") or "").strip()
    weak = str((models or {}).get("weak") or "").strip()
    if not isinstance(models, dict) or not strong or not weak:
        raise CoordinationError("validation",
                                "缺少模型配置：models.strong 与 models.weak 都必填"
                                "（界面应从已有模型配置中选择）")
    return {"strong": strong, "weak": weak}


def validate_model_routes(models: dict[str, str]) -> None:
    """校验路由名在 models.yaml 里真实存在（缺配置时界面直接指出缺项）。"""
    try:
        from ..api import model_config
        compiled = model_config.load() or {}
        known = set((compiled.get("models") or {}).keys())
    except Exception:  # noqa: BLE001 配置读不到 ⇒ 不拦任命（运行期再报）
        return
    if not known:
        return
    for key, route in (models or {}).items():
        if route and route not in known:
            raise CoordinationError(
                "validation", f"models.{key} 不是已知模型路由: {route}")


def _validate_function(work_id: str, function_id: str) -> None:
    """任职职能必须真实存在，且是**非根职能卡**（第一期口径）。"""
    fid = str(function_id or "").strip()
    if not fid:
        raise CoordinationError("validation", "function_id 不能为空（任命哪位职能）")
    from . import function_store, work_store
    if not function_store.function_exists(work_id, fid):
        raise CoordinationError("validation", f"职能不存在: {fid}")
    try:
        root_id = str(work_store.work_card_id_of(work_id) or "")
    except Exception:  # noqa: BLE001 读不到根卡不拦（职能存在性已验过）
        root_id = ""
    if fid and fid == root_id:
        raise CoordinationError("validation",
                                "第一期统筹只任命给非根职能卡（总裁＝根卡兼任，不在第一期）")


def _validate_plan_card(work_id: str, plan_card_id: str) -> None:
    """计划载体必须是 work 内已有的**普通卡**（不是根卡、不是职能卡）。"""
    cid = str(plan_card_id or "").strip()
    if not cid:
        raise CoordinationError("validation", "plan_card_id 不能为空（选哪张卡承载计划）")
    from . import card_store
    metas = card_store.iter_card_metas(work_id)
    meta = metas.get(cid)
    if not meta:
        raise CoordinationError("not_found", f"计划卡不存在: {cid}")
    if cid.startswith("fn_"):
        raise CoordinationError("validation",
                                "计划载体须是普通卡，不能是职能卡（fn_NNN）")
    if not str(meta.get("parent") or ""):
        raise CoordinationError("validation",
                                "计划载体须是普通卡，不能是根卡")


# ---------------------------------------------------------------- 存储 API

async def appoint(work_id: str, plan_card_id: str, *, function_id: str,
                  models: Any, next_review_at: str,
                  observe: Any = None, instruct: Any = None,
                  mode: str = "observe", objective: str = "",
                  by: str = "user", schedule_first: bool = False) -> dict[str, Any]:
    """任命统筹。写事务内保证计划卡级 active/paused 唯一——并发第二个任命
    拿到 conflict 而不是第二个在任负责人。"""
    _validate_plan_card(work_id, plan_card_id)
    _validate_function(work_id, function_id)
    mdls = _validate_models(models)
    validate_model_routes(mdls)
    obs = _validate_scope(observe, OBSERVE_SCOPES, "observe")
    ins = _validate_scope(instruct, INSTRUCT_SCOPES, "instruct")
    if str(mode or "") not in MODES:
        raise CoordinationError("validation",
                                f"mode 非法: {mode!r}（允许 {'/'.join(MODES)}）")
    review_dt = _parse_future_time(next_review_at)
    now = _now()
    plan: dict[str, Any] = {"objective": str(objective or "").strip()}
    if not plan["objective"]:
        raise CoordinationError("validation", "objective 不能为空（这期计划要达成什么）")

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
        # [唯一性＝计划卡级 2026-11-21 · 用户令] 同一份计划（一张计划卡）
        # 只能有一位在任统筹；同一 work 可以有多份计划、各计划各管各的。
        # BEGIN IMMEDIATE 串行化写 ⇒ 同卡并发任命只有一个成功，另一个拿 conflict。
        row = conn.execute(
            "SELECT * FROM coordination_appointments"
            " WHERE work_id=? AND plan_card_id=?"
            " AND state IN ('active','paused') LIMIT 1",
            (work_id, plan_card_id)).fetchone()
        if row is not None:
            cur = _row_to_appt(row)
            raise CoordinationError(
                "conflict",
                f"这张计划卡已有在任任命（负责人 {cur['function_id']}）。"
                "改负责人先撤回现任命。",
                current_assignment=cur["assignment_id"],
                plan_card_id=cur["plan_card_id"])
        aid = f"asg_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO coordination_appointments"
            " (work_id,plan_card_id,assignment_id,function_id,state,mode,"
            "  rev,plan,observe_scope,instruct_scope,models,next_review_at,"
            "  op_seq,pending,actions,last_review,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_id, plan_card_id, aid, str(function_id), "active",
             str(mode), 0, _dumps(plan), _dumps(obs), _dumps(ins),
             _dumps(mdls), review_dt.isoformat(), 0,
             _dumps([{"kind": "appoint", "at": now, "request_id": aid}])
             if schedule_first else "[]", "[]", "",
             now, now))
        conn.execute(
            "INSERT INTO coordination_plan_revisions"
            " (work_id,plan_card_id,assignment_id,rev,plan,saved_at,saved_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (work_id, plan_card_id, aid, 0, _dumps(plan), now, str(by)))
        row2 = conn.execute(
            "SELECT * FROM coordination_appointments WHERE assignment_id=?",
            (aid,)).fetchone()
        return {"ok": True, "appointment": _row_to_appt(row2)}

    return await card_db.run_write(work_id, _fn)


def _require_appt(conn: sqlite3.Connection, work_id: str, plan_card_id: str,
                  assignment_id: str, *,
                  need_states: tuple[str, ...] = ACTIVE_STATES) -> sqlite3.Row:
    """定位任命行并校验：存在、assignment 匹配、状态合法（越权 ⇒ denied）。"""
    row = conn.execute(
        "SELECT * FROM coordination_appointments"
        " WHERE work_id=? AND plan_card_id=?"
        " ORDER BY id DESC LIMIT 1",
        (work_id, plan_card_id)).fetchone()
    if row is None:
        raise CoordinationError("not_found", "这张卡还没有任命")
    if str(row["assignment_id"]) != str(assignment_id or "").strip():
        raise CoordinationError(
            "denied",
            "assignment_id 不匹配（任命可能已被撤回重建）——请刷新面板后重试",
            current_assignment=row["assignment_id"])
    if str(row["state"]) not in need_states:
        raise CoordinationError(
            "denied",
            f"当前状态 {row['state']} 不允许此操作（需要 {'/'.join(need_states)}）")
    return row


async def control_op(work_id: str, plan_card_id: str, *, assignment_id: str,
                     op: str, mode: str = "", observe: Any = None,
                     instruct: Any = None, next_review_at: str = "",
                     models: Any = None,
                     note: str = "", expected_rev: Optional[int] = None,
                     ) -> dict[str, Any]:
    """管理统筹状态、授权、模型与下次回看。

    每次成功操作 op_seq+1（旧执行令牌失效）；**每次状态变更都递增 rev**
    （包括状态、模式、授权、模型和回看时间的变更。
    旧页面拿旧版本再提交必得 409）；wake 是入账，不递增。

    [读入版本 2026-11-21 · 用户令] 任务书「任命以外的变更带当前任命标识和
    **读入版本**」：变更类操作
    校验 expected_rev——不匹配 ⇒ conflict 带最新版本（防旧面板盲发控制）。
    expected_rev=None 表示调用方未带（直调兼容）；HTTP 层要求必带。
    wake 是入账不是变更，不校验版本。
    """
    _REV_CHECK_OPS = ("pause", "resume", "revoke", "finish", "set_mode",
                     "set_grant", "set_review_at", "set_models")
    now = _now()

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
        row = _require_appt(conn, work_id, plan_card_id, assignment_id)
        if op in _REV_CHECK_OPS and expected_rev is not None \
                and int(expected_rev) != int(row["rev"]):
            raise CoordinationError(
                "conflict",
                f"读入版本已过期（你提交的是 {expected_rev}，当前 {row['rev']}）"
                "——请刷新面板后按最新版本重试",
                current_rev=int(row["rev"]), state=row["state"])
        sets: list[str] = ["updated_at=?", "op_seq=op_seq+1"]
        args: list[Any] = [now]
        nonlocal_x: dict[str, Any] = {}
        # [状态变更推进 rev 2026-11-21 · 用户令] pause/resume/revoke/finish
        # 也是控制变更，**每次都递增 rev**（此前只有 set_mode/set_grant 递增
        # ⇒ 旧页面拿同一个 rev 反复暂停/恢复永不 409）。wake 是入账，不递增。
        if op == "pause":
            if row["state"] != "active":
                raise CoordinationError("validation", "只有活跃的任命可以暂停")
            sets.append("state='paused'")
            sets.append("rev=rev+1")
        elif op == "resume":
            if row["state"] != "paused":
                raise CoordinationError("validation", "只有暂停中的任命可以恢复")
            sets.append("state='active'")
            sets.append("rev=rev+1")
        elif op == "revoke":
            if row["state"] not in ACTIVE_STATES:
                raise CoordinationError("validation", "任命已结束，无需撤回")
            sets.append("state='revoked'")
            sets.append("rev=rev+1")
        elif op == "finish":
            if row["state"] not in ACTIVE_STATES:
                raise CoordinationError("validation", "任命已结束，无需重复结束")
            sets.append("state='finished'")
            sets.append("rev=rev+1")
        elif op == "wake":
            if row["state"] != "active":
                raise CoordinationError(
                    "validation", "暂停中的任命不接唤醒（先恢复）")
            pending = _loads(row["pending"], [])
            pending.append({"kind": "user_wake", "at": now, "request_id": uuid.uuid4().hex,
                            "note": str(note or "")})
            nonlocal_x["pending"] = _dumps(pending)
            sets.append("pending=?")
            args.append(nonlocal_x["pending"])
        elif op == "set_mode":
            if row["state"] not in ACTIVE_STATES:
                raise CoordinationError("denied", "任命已结束，不能再改运行模式")
            if str(mode or "") not in MODES:
                raise CoordinationError("validation",
                                        f"mode 非法: {mode!r}（允许 {'/'.join(MODES)}）")
            sets.append("mode=?")
            args.append(str(mode))
            sets.append("rev=rev+1")
        elif op == "set_models":
            if row["state"] not in ACTIVE_STATES:
                raise CoordinationError("denied", "任命已结束，不能再改模型")
            configured = _validate_models(models)
            validate_model_routes(configured)
            sets.extend(("models=?", "rev=rev+1"))
            args.append(_dumps(configured))
        elif op == "set_review_at":
            date = _parse_future_time(next_review_at)
            current = datetime.fromisoformat(row["next_review_at"])
            if date > current:
                raise CoordinationError("validation", "可在此提前回看，延期须完成独立回看")
            pending = [p for p in _loads(row["pending"], []) if p.get("kind") != "review_due"]
            sets.extend(("next_review_at=?", "pending=?", "rev=rev+1"))
            args.extend((date.isoformat(), _dumps(pending)))
        elif op == "set_grant":
            if row["state"] not in ACTIVE_STATES:
                raise CoordinationError("denied", "任命已结束，不能再改授权")
            if observe is not None:
                obs = _validate_scope(observe, OBSERVE_SCOPES, "observe")
                sets.append("observe_scope=?")
                args.append(_dumps(obs))
            if instruct is not None:
                ins = _validate_scope(instruct, INSTRUCT_SCOPES, "instruct")
                sets.append("instruct_scope=?")
                args.append(_dumps(ins))
            if observe is None and instruct is None:
                raise CoordinationError("validation",
                                        "set_grant 需要至少给 observe 或 instruct 之一")
            sets.append("rev=rev+1")
        else:
            raise CoordinationError("validation", f"未知操作: {op!r}")
        args.append(row["id"])
        conn.execute(
            f"UPDATE coordination_appointments SET {', '.join(sets)}"
            " WHERE id=?", tuple(args))
        row2 = conn.execute(
            "SELECT * FROM coordination_appointments WHERE id=?",
            (row["id"],)).fetchone()
        return {"ok": True, "appointment": _row_to_appt(row2)}

    return await card_db.run_write(work_id, _fn)


def _board_items_for(conn, assignment_id: str, plan: dict) -> list[dict]:
    from . import coordination_board

    items = coordination_board.materialize_items(plan.get("items"))
    if not any("source_cards" not in it for it in items):
        return items
    legacy_model_items = set()
    for row in conn.execute(
            "SELECT detail FROM coordination_plan_revisions WHERE assignment_id=? AND saved_by='model'",
            (assignment_id,)):
        legacy_model_items.update(_loads(row["detail"], {}).get("changed") or [])
    for item in items:
        # 旧版模型改板未记录来源，原件留在用户板面，模型出站按未知来源处理。
        if item["item_id"] in legacy_model_items:
            item.setdefault("source_cards", None)
        elif "_coord_sources" in plan:
            item.setdefault("source_cards", plan["_coord_sources"])
    return items


async def save_plan(work_id: str, plan_card_id: str, *, assignment_id: str,
                    expected_rev: int, plan: Any,
                    by: str = "user", source_cards: Optional[set[str]] = None) -> dict[str, Any]:
    """保存完整新计划（乐观并发）：expected_rev 不匹配 ⇒ CONFLICT+最新版本。"""
    if not isinstance(plan, dict):
        raise CoordinationError("validation", "plan 必须是对象（完整的新计划）")
    plan = {key: value for key, value in plan.items()
            if key not in ("_coord_sources", "_board_requirement_sources")}
    now = _now()

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
        row = _require_appt(conn, work_id, plan_card_id, assignment_id)
        if row["state"] not in ACTIVE_STATES:
            raise CoordinationError(
                "denied", f"当前状态 {row['state']} 的任命不能再改计划")
        cur_rev = int(row["rev"])
        if int(expected_rev) != cur_rev:
            raise CoordinationError(
                "conflict",
                f"计划版本已过期（你提交的是 {expected_rev}，当前 {cur_rev}）"
                "——请读取最新版本后再改",
                current_rev=cur_rev, plan=_loads(row["plan"], {}))
        # 兼容尚无条目的旧版首份计划。已有条目沿计划板更新。
        from . import coordination_board as _board
        previous = _loads(row["plan"], {})
        old_items = _board_items_for(conn, assignment_id, previous)
        if not old_items:
            old_items = _board.materialize_items(plan.get("items"))
            for item in old_items:
                item.pop("source_cards", None)
                if source_cards is not None:
                    item["source_cards"] = sorted(source_cards)
        updated = {**plan, "items": old_items}
        if source_cards is not None:
            updated["_coord_sources"] = sorted(source_cards)
        elif "_coord_sources" in previous:
            updated["_coord_sources"] = previous["_coord_sources"]
        if "_board_requirement_sources" in previous:
            updated["_board_requirement_sources"] = previous["_board_requirement_sources"]
        new_rev = cur_rev + 1
        conn.execute(
            "UPDATE coordination_appointments"
            " SET plan=?, rev=rev+1, updated_at=?, op_seq=op_seq+1"
            " WHERE id=?", (_dumps(updated), now, row["id"]))
        conn.execute(
            "INSERT INTO coordination_plan_revisions"
            " (work_id,plan_card_id,assignment_id,rev,plan,saved_at,saved_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (work_id, plan_card_id, assignment_id, new_rev, _dumps(updated),
             now, str(by)))
        row2 = conn.execute(
            "SELECT * FROM coordination_appointments WHERE id=?",
            (row["id"],)).fetchone()
        return {"ok": True, "appointment": _row_to_appt(row2)}

    return await card_db.run_write(work_id, _fn)


def _verify_board_item(conn, work_id: str, plan_card_id: str, assignment_id: str,
                       item: dict, old: dict, requirements: dict,
                       allowed: Optional[set[str]], reads: dict[str, dict]) -> None:
    import hashlib
    import re
    from . import event_log

    iid = item["item_id"]
    prior_sources = item.get("source_cards", [])
    sources = set(prior_sources) if isinstance(prior_sources, list) else set()
    if allowed is not None and any(item.get(k) != old.get(k)
                                   for k in ("title", "next_result", "result_note")):
        sources.update(allowed)

    def accept_source(cid: str) -> None:
        if not cid or (allowed is not None and cid not in allowed):
            raise CoordinationError("validation", f"事项 {iid} 的来源超出观察范围")
        sources.add(cid)

    owner = item["owner_card"]
    if owner and owner != old.get("owner_card"):
        if not conn.execute("SELECT 1 FROM cards WHERE card_id=?", (owner,)).fetchone():
            raise CoordinationError("validation", f"事项 {iid} 的责任卡不存在")
        accept_source(owner)
    completing = item["status"] == "done" and old.get("status") != "done"
    if completing or item["requirement_refs"] != old.get("requirement_refs", []):
        canonical = []
        for ref in item["requirement_refs"]:
            read = reads.get(ref)
            if read and read.get("action") == "coord_read_card":
                cid = read["card_id"]
                version = read.get("version") or {}
                body = read.get("requirements") or ""
                fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
                if body.strip() and fingerprint == version.get("sha256"):
                    ref = f"req:{work_id}:{cid}:{fingerprint}"
                    requirements[ref] = {"card_id": cid, "version": version, "text": body}
            record = requirements.get(ref)
            if not record:
                # 用户可引用当前正稿的卡号；存档统一转换为带版本的坐标。
                parts = ref.split(":")
                cid = parts[2] if len(parts) == 4 and parts[:2] == ["req", work_id] else ref
                if not re.fullmatch(r"card_[A-Za-z0-9_-]+", cid):
                    raise CoordinationError("validation", f"事项 {iid} 的需求引用不可核验: {ref}")
                current = conn.execute(
                    "SELECT requirements,requirements_state,updated_at FROM cards WHERE card_id=?",
                    (cid,)).fetchone()
                body = str(current["requirements"] or "") if current else ""
                fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
                if not body.strip() or (len(parts) == 4 and parts[3] != fingerprint):
                    raise CoordinationError("validation", f"事项 {iid} 的需求版本不可核验: {ref}")
                ref = f"req:{work_id}:{cid}:{fingerprint}"
                record = {"card_id": cid, "text": body, "version": {
                    "sha256": fingerprint, "state": current["requirements_state"],
                    "updated_at": current["updated_at"]}}
                requirements[ref] = record
            accept_source(record["card_id"])
            canonical.append(ref)
        item["requirement_refs"] = list(dict.fromkeys(canonical))

    if completing or item["evidence_refs"] != old.get("evidence_refs", []):
        for ref in item["evidence_refs"]:
            parts = ref.split(":")
            if len(parts) == 2 and parts[0] == "receipt" and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordination_evidence'"
                    ).fetchone():
                receipt = conn.execute(
                    "SELECT from_card FROM coordination_evidence WHERE work_id=? AND plan_card_id=?"
                    " AND assignment_id=? AND item_id=? AND reply_msg_id=? AND status='verified'",
                    (work_id, plan_card_id, assignment_id, iid, parts[1])).fetchone()
                if receipt:
                    accept_source(receipt["from_card"])
                    continue
            if (len(parts) == 4 and parts[:2] == ["event", work_id]
                    and re.fullmatch(r"(card|fn)_[A-Za-z0-9_-]+", parts[2])
                    and parts[3].isascii() and parts[3].isdigit() and len(parts[3]) <= 18):
                cid, seq = parts[2], int(parts[3])
                accept_source(cid)
                event = next((ev for ev in event_log.timeline(
                    work_id, cid, from_seq=seq, before=seq + 1)
                    if ev.get("seq") == seq), None)
                if event and ((event.get("data") or {}).get("item_id") == iid
                              or (cid == owner and not completing)):
                    continue
            raise CoordinationError("validation", f"事项 {iid} 的证据引用不可核验: {ref}")

    if completing and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordination_qa'"
            ).fetchone():
        qa = conn.execute(
            "SELECT verdict FROM coordination_qa WHERE work_id=? AND plan_card_id=?"
            " AND assignment_id=? AND item_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (work_id, plan_card_id, assignment_id, iid)).fetchone()
        if qa and qa["verdict"] != "passed":
            raise CoordinationError("validation", f"事项 {iid} 的最新 QA 尚未通过")
    item["source_cards"] = sorted(sources) if isinstance(prior_sources, list) else None


async def board_apply(work_id: str, plan_card_id: str, *, assignment_id: str,
                      expected_rev: int, changes: Any, reason: str = "",
                      by: str = "user", source_cards: Optional[set[str]] = None,
                      requirement_reads: Optional[dict[str, dict]] = None) -> dict[str, Any]:
    """计划板批量变更（与 save_plan 同一版本保存入口）：单事务
    校验整批 → 落库 → 版本推进。任一条不合法 ⇒ 整批零写入。

    [2026-11-23 · 统筹计划板任务书]
    - 状态闸同 save_plan：active/paused 可写（用户面板在暂停期仍可维护，
      强模型只在活跃回合里调到这层）；revoked/finished 一律拒绝。
    - expected_rev 不匹配 ⇒ conflict，带回当前版本与当前条目投影。
    - 新条目 ID 由宿主分配（模型给的 item_id 撞名即拒，不覆盖既有事项）。
    - 历史：完成/受阻/重开/排序/插入都进 coordination_plan_revisions，
      detail 记 kind/reason/changed/allocated——可回读操作者、时间、理由。
    """
    from . import coordination_board as _board
    now = _now()

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
        row = _require_appt(conn, work_id, plan_card_id, assignment_id)
        if row["state"] not in (("active",) if source_cards is not None else ACTIVE_STATES):
            raise CoordinationError(
                "denied", f"当前状态 {row['state']} 的任命不能再改计划板")
        cur_rev = int(row["rev"])
        plan = _loads(row["plan"], {})
        items_now = _board_items_for(conn, assignment_id, plan)
        for item in items_now:
            item.setdefault("source_cards", plan.get("_coord_sources", []))
        visible = _board.visible_items({"plan": {"items": items_now}}, source_cards)
        if int(expected_rev) != cur_rev:
            raise CoordinationError(
                "conflict",
                f"计划版本已过期（你读入的是 {expected_rev}，当前 {cur_rev}）"
                "——请重读当前板后再改",
                current_rev=cur_rev, items=visible)
        merged, changed, allocated, errors = _board.apply_changes(
            items_now, changes)
        if errors:
            raise CoordinationError(
                "validation", "；".join(errors),
                errors=errors, current_rev=cur_rev)
        old = {item["item_id"]: item for item in items_now}
        visible_ids = {item["item_id"] for item in visible}
        requirements = dict(plan.get("_board_requirement_sources") or {})
        for it in merged:
            iid = it["item_id"]
            if iid not in changed:
                continue
            if iid in old and iid not in visible_ids:
                raise CoordinationError("denied", "事项不可编辑")
            _verify_board_item(conn, work_id, plan_card_id, assignment_id, it,
                               old.get(iid, {}), requirements, source_cards,
                               requirement_reads or {})
        plan["items"] = merged
        if requirements:
            plan["_board_requirement_sources"] = requirements
        new_rev = cur_rev + 1
        conn.execute(
            "UPDATE coordination_appointments"
            " SET plan=?, rev=rev+1, updated_at=?, op_seq=op_seq+1"
            " WHERE id=?", (_dumps(plan), now, row["id"]))
        conn.execute(
            "INSERT INTO coordination_plan_revisions"
            " (work_id,plan_card_id,assignment_id,rev,plan,saved_at,saved_by,"
            " detail) VALUES (?,?,?,?,?,?,?,?)",
            (work_id, plan_card_id, assignment_id, new_rev, _dumps(plan),
             now, str(by),
             _dumps({"kind": "board_apply", "reason": str(reason or ""),
                     "changed": changed, "allocated": allocated})))
        return {"ok": True, "rev": new_rev, "changed": changed,
                "allocated": allocated}

    return await card_db.run_write(work_id, _fn)


def board_item_receipts(work_id: str, plan_card_id: str, assignment_id: str,
                        item_id: str, allowed: set[str]) -> dict:
    """单项查板的可引用执行回执与 QA 结论。"""
    def _read(conn):
        keys = (work_id, plan_card_id, assignment_id, item_id)
        try:
            evidence = conn.execute(
                "SELECT from_card,reply_msg_id FROM coordination_evidence WHERE work_id=?"
                " AND plan_card_id=? AND assignment_id=? AND item_id=? AND status='verified'",
                keys).fetchall()
        except sqlite3.OperationalError:
            evidence = []
        result = {"verified_receipts": [f"receipt:{e['reply_msg_id']}" for e in evidence
                                        if e["from_card"] in allowed]}
        try:
            qa = conn.execute(
                "SELECT target_card,verdict FROM coordination_qa WHERE work_id=? AND plan_card_id=?"
                " AND assignment_id=? AND item_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                keys).fetchone()
        except sqlite3.OperationalError:
            qa = None
        if qa and qa["target_card"] in allowed:
            result["qa_verdict"] = qa["verdict"] or "unverified"
        return result
    return card_db.read_sync(work_id, _read, {})


def _overdue(next_review_at: str) -> bool:
    """到期时间已过且无回看收口 ⇒ 欠办（纯读侧计算，不落库）。"""
    raw = str(next_review_at or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.tzinfo is not None and dt <= datetime.now(dt.tzinfo)
    except ValueError:
        return False


async def review_closure(work_id: str, plan_card_id: str, *,
                         assignment_id: str,
                         expected_rev: int, due_at: str,
                         receipt: dict[str, Any]) -> dict[str, Any]:
    """同事务核对本次欠办，再发布回看记录和下一次时间。"""
    if not receipt.get("evidence") or not (receipt.get("requirements") or receipt.get("open_items")):
        raise CoordinationError("validation", "独立回看须有查证回执及需求引用或尚缺事项")
    next_review_at = _parse_future_time(receipt["next_review_at"]).isoformat()

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
        sql = ("SELECT * FROM coordination_appointments"
               " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
               " AND state='active' AND rev=? AND next_review_at=?")
        row = conn.execute(sql, (work_id, plan_card_id, assignment_id,
                                 expected_rev, due_at)).fetchone()
        if row is None:
            return {"ok": False, "why": "任命、版本或回看期限已变更"}
        pending = _loads(row["pending"], [])
        kept = [p for p in pending if not (p.get("kind") == "review_due"
                                            and p.get("due_at") == due_at)]
        if len(kept) == len(pending):
            return {"ok": False, "why": "没有匹配本轮的回看欠办"}
        completed_at = _now()
        conn.execute(
            "UPDATE coordination_appointments"
            " SET next_review_at=?, pending=?, last_review=?, updated_at=? WHERE id=?",
            (next_review_at, _dumps(kept),
             _dumps({**receipt, "completed_at": completed_at, "due_at": due_at,
                     "rev": expected_rev}), completed_at, row["id"]))
        return {"ok": True, "next_review_at": str(next_review_at),
                "cleared": len(pending) - len(kept)}

    return await card_db.run_write(work_id, _fn)


def wake_key(pending: dict[str, Any]) -> str:
    return str(pending.get("request_id") or pending.get("due_at")
               or pending.get("batch_id") or pending.get("at") or "")


async def consume_wake(work_id: str, assignment_id: str, kind: str, key: str) -> None:
    def _fn(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT id,pending FROM coordination_appointments WHERE assignment_id=?",
            (assignment_id,)).fetchone()
        if row is None:
            return
        pending = _loads(row["pending"], [])
        remaining = [entry for entry in pending
                     if not (entry.get("kind") == kind and wake_key(entry) == key)]
        if remaining != pending:
            conn.execute("UPDATE coordination_appointments SET pending=? WHERE id=?",
                         (_dumps(remaining), row["id"]))

    await card_db.run_write(work_id, _fn)


# ---------------------------------------------------------------- 调用账本

async def add_observation(work_id: str, plan_card_id: str,
                          **observation: Any) -> dict[str, Any]:
    """记一条模型调用或统筹操作的账。"""
    return await card_db.run_write(
        work_id, lambda conn: add_observation_tx(conn, work_id, plan_card_id, **observation))


def add_observation_tx(conn: sqlite3.Connection, work_id: str, plan_card_id: str, *,
                       assignment_id: str, kind: str, status: str,
                       batch_id: str = "", decision: str = "",
                       item_id: str = "", reason: str = "",
                       evidence_refs: Any = None, question: str = "",
                       model_used: str = "", usage: Any = None,
                       detail: Any = None) -> dict[str, Any]:
    """在调用方事务内记账。"""
    _ensure(conn)
    now = _now()
    cur = conn.execute(
        "INSERT INTO coordination_observations"
        " (work_id,plan_card_id,assignment_id,kind,batch_id,decision,"
        "  item_id,status,reason,evidence_refs,question,model_used,"
        "  usage,detail,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (work_id, plan_card_id, str(assignment_id), str(kind),
         str(batch_id or ""), str(decision or ""), str(item_id or ""),
         str(status), str(reason or ""),
         _dumps(list(evidence_refs or [])), str(question or ""),
         str(model_used or ""), _dumps(usage or {}),
         _dumps(detail or {}), now, now))
    return {"ok": True, "id": int(cur.lastrowid)}


def list_observations(work_id: str, plan_card_id: str, *,
                      kind: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """某计划卡的调用账（倒序）。"""

    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT * FROM coordination_observations"
                   " WHERE work_id=? AND plan_card_id=?")
            args: list[Any] = [work_id, plan_card_id]
            if kind:
                sql += " AND kind=?"
                args.append(str(kind))
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(max(1, min(int(limit or 50), 500)))
            rows = conn.execute(sql, tuple(args)).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": int(r["id"]),
                "assignment_id": r["assignment_id"],
                "kind": r["kind"],
                "batch_id": r["batch_id"],
                "decision": r["decision"],
                "item_id": r["item_id"],
                "status": r["status"],
                "reason": r["reason"],
                "evidence_refs": _loads(r["evidence_refs"], []),
                "question": r["question"],
                "model_used": r["model_used"],
                "usage": _loads(r["usage"], {}),
                "detail": _loads(r["detail"], {}),
                "created_at": r["created_at"],
            })
        return out

    return card_db.read_sync(work_id, _fn, [])


def panel(work_id: str, plan_card_id: str, *,
          history: bool = False, rev: Optional[int] = None,
          history_id: Optional[int] = None) -> dict[str, Any]:
    """卡面「统筹计划」面板（**只读，不写库**——空面板不建表不落行）。

    [历史正文回读 2026-11-21 · 用户令] 两种回读键：
    - ``history_id=N``＝修订记录**主键**，全局唯一定位——撤回后重新任命
      会出现两个 rev.0，只有按主键回读才能把前后两次的正文都打开
      （[2026-11-21 · 用户令] 撞号实测后裁定）。
    - ``rev=N``＝当前任命链内的版本号；跨任命重号时取**最新**一次任命
      的该版本（ORDER BY id DESC）。找不到 ⇒ not_found。
    ``history=1`` 返回摘要索引，每条带 history_id/rev/assignment_id。
    """
    if history_id is not None:
        def _hid_fn(conn: sqlite3.Connection) -> dict[str, Any]:
            try:
                row = conn.execute(
                    "SELECT * FROM coordination_plan_revisions"
                    " WHERE work_id=? AND plan_card_id=? AND id=?",
                    (work_id, plan_card_id, int(history_id))).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is None:
                raise CoordinationError(
                    "not_found", f"历史版本不存在: history_id={history_id}")
            return {
                "plan_card_id": plan_card_id,
                "history_id": int(row["id"]),
                "rev": int(row["rev"]),
                "plan": _loads(row["plan"], {}),
                "saved_at": row["saved_at"],
                "saved_by": row["saved_by"],
                "assignment_id": row["assignment_id"],
                "detail": _loads(row["detail"], {}) if "detail" in row.keys() else {},
            }

        return card_db.read_sync(work_id, _hid_fn, None) or {
            "plan_card_id": plan_card_id, "history_id": int(history_id),
            "rev": 0, "plan": {},
        }

    if rev is not None:
        def _rev_fn(conn: sqlite3.Connection) -> dict[str, Any]:
            try:
                row = conn.execute(
                    "SELECT * FROM coordination_plan_revisions"
                    " WHERE work_id=? AND plan_card_id=? AND rev=?"
                    " ORDER BY id DESC LIMIT 1",
                    (work_id, plan_card_id, int(rev))).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is None:
                raise CoordinationError(
                    "not_found", f"版本不存在: rev.{rev}")
            return {
                "plan_card_id": plan_card_id,
                "history_id": int(row["id"]),
                "rev": int(row["rev"]),
                "plan": _loads(row["plan"], {}),
                "saved_at": row["saved_at"],
                "saved_by": row["saved_by"],
                "assignment_id": row["assignment_id"],
                "detail": _loads(row["detail"], {}) if "detail" in row.keys() else {},
            }

        return card_db.read_sync(work_id, _rev_fn, None) or {
            "plan_card_id": plan_card_id, "rev": int(rev), "plan": {},
        }

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        try:
            row = conn.execute(
                "SELECT * FROM coordination_appointments"
                " WHERE work_id=? AND plan_card_id=?"
                " ORDER BY id DESC LIMIT 1",
                (work_id, plan_card_id)).fetchone()
        except sqlite3.OperationalError:
            row = None  # 表还没建＝从没任命过 ⇒ 空面板
        if row is None:
            appt: Optional[dict[str, Any]] = None
        else:
            appt = _row_to_appt(row)
        revs: list[dict[str, Any]] = []
        if history:
            try:
                # [2026-11-21 · 用户令] 摘要按修订记录主键序（时间序），
                # 不按 rev 排——跨任命 rev 重号会把两次任命的 rev.0 混在一起；
                # 每条带 history_id（回读键）与 assignment_id（区分哪次任命）。
                for r in conn.execute(
                        "SELECT id, rev, assignment_id, saved_at, saved_by,"
                        " length(plan) AS plan_len, detail"
                        " FROM coordination_plan_revisions"
                        " WHERE work_id=? AND plan_card_id=?"
                        " ORDER BY id DESC LIMIT 50",
                        (work_id, plan_card_id)).fetchall():
                    revs.append({
                        "history_id": int(r["id"]),
                        "rev": int(r["rev"]),
                        "assignment_id": r["assignment_id"],
                        "saved_at": r["saved_at"],
                        "saved_by": r["saved_by"],
                        "plan_len": int(r["plan_len"] or 0),
                        "detail": _loads(r["detail"], {}),
                    })
            except sqlite3.OperationalError:
                revs = []
        out: dict[str, Any] = {
            "plan_card_id": plan_card_id,
            "assignment_id": (appt or {}).get("assignment_id", ""),
            "function_id": (appt or {}).get("function_id", ""),
            "rev": (appt or {}).get("rev", 0),
            "state": (appt or {}).get("state", "none"),
            "mode": (appt or {}).get("mode", ""),
            "plan": {k: v for k, v in (appt or {}).get("plan", {}).items()
                     if k not in ("_coord_sources", "_board_requirement_sources")},
            "grants": {
                "observe": (appt or {}).get("observe_scope", {}),
                "instruct": (appt or {}).get("instruct_scope", {}),
            },
            "models": (appt or {}).get("models", {}),
            "last_review": (appt or {}).get("last_review", {}),
            "next_review_at": (appt or {}).get("next_review_at", ""),
            "pending": (appt or {}).get("pending", []),
            "actions": (appt or {}).get("actions", []),
            "created_at": (appt or {}).get("created_at", ""),
            "updated_at": (appt or {}).get("updated_at", ""),
            "overdue": _overdue((appt or {}).get("next_review_at", "")),
        }
        # [2026-11-23 · 计划板] 面板板面投影（读侧归一，不改库）：
        # 未完成＝todo/doing/blocked 按 (priority,item_id) 序；
        # 已完成单独成区——原 item_id 与关联证据原样保留。
        from . import coordination_board as _board
        raw_items = _board_items_for(conn, appt["assignment_id"], appt["plan"]) if appt else []
        if appt and "items" in out["plan"]:
            out["plan"]["items"] = raw_items
        _items = _board.project_items(raw_items)
        out["board"] = {
            "rev": out["rev"],
            "items": _items,
            "requirements": (appt or {}).get("plan", {}).get("_board_requirement_sources", {}),
            "unfinished": [it for it in _items
                           if it["status"] in _board.ITEM_STATUSES[:3]],
            "done": [it for it in _items if it["status"] == "done"],
        }
        if appt is not None:
            last_turn = conn.execute("SELECT status,detail,created_at FROM coordination_observations"
                                     " WHERE assignment_id=? AND kind='strong_turn'"
                                     " ORDER BY id DESC LIMIT 1", (appt["assignment_id"],)).fetchone()
            if last_turn:
                detail = _loads(last_turn["detail"], {})
                out["last_turn"] = {"status": last_turn["status"],
                                    "trigger": detail.get("trigger", ""),
                                    "at": last_turn["created_at"]}
            if "_coord_sources" in appt["plan"]:
                initial = conn.execute("SELECT plan FROM coordination_plan_revisions"
                                       " WHERE assignment_id=? AND rev=0 LIMIT 1",
                                       (appt["assignment_id"],)).fetchone()
                out["model_plan_sources"] = appt["plan"].get("_coord_sources")
                out["user_objective"] = _loads(initial["plan"], {}).get("objective", "") if initial else ""
        if history:
            out["history"] = revs
        return out

    return card_db.read_sync(work_id, _fn, {
        "plan_card_id": plan_card_id, "assignment_id": "",
        "function_id": "", "rev": 0, "state": "none", "mode": "",
        "plan": {}, "grants": {}, "models": {}, "last_review": {},
        "next_review_at": "", "pending": [], "actions": [],
        "created_at": "", "updated_at": "", "overdue": False,
        "board": {"rev": 0, "items": [], "unfinished": [], "done": []},
    })


def list_plans(work_id: str) -> list[dict[str, Any]]:
    """work 级计划列表（导航用，只读）。只列有过任命的计划卡，带卡标题。"""
    titles: dict[str, str] = {}
    try:
        from . import card_store
        titles = {cid: str((m or {}).get("title") or "")
                  for cid, m in card_store.iter_card_metas(work_id).items()}
    except Exception:  # noqa: BLE001 标题取不到不拦列表
        titles = {}

    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            rows = conn.execute(
                "SELECT * FROM coordination_appointments"
                " WHERE work_id=? ORDER BY id DESC",
                (work_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            appt = _row_to_appt(r)
            if appt["plan_card_id"] in seen:
                continue  # 同一计划卡只出最新一次任命
            seen.add(appt["plan_card_id"])
            out.append({
                "plan_card_id": appt["plan_card_id"],
                "title": titles.get(appt["plan_card_id"], ""),
                "assignment_id": appt["assignment_id"],
                "function_id": appt["function_id"],
                "state": appt["state"],
                "mode": appt["mode"],
                "rev": appt["rev"],
                "next_review_at": appt["next_review_at"],
                "overdue": _overdue(appt["next_review_at"]),
                "updated_at": appt["updated_at"],
            })
        return out

    return card_db.read_sync(work_id, _fn, [])
