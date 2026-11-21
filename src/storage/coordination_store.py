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
    " saved_at TEXT, saved_by TEXT)"
)
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
                     note: str = "", expected_rev: Optional[int] = None,
                     ) -> dict[str, Any]:
    """状态与控制操作：pause/resume/revoke/finish/wake/set_mode/set_grant。

    每次成功操作 op_seq+1（旧执行令牌失效）；**每次状态变更都递增 rev**
    （pause/resume/revoke/finish/set_mode/set_grant——[2026-11-21 · 用户令]
    旧页面拿旧版本再提交必得 409）；wake 是入账，不递增。

    [读入版本 2026-11-21 · 用户令] 任务书「任命以外的变更带当前任命标识和
    **读入版本**」：变更类操作（pause/resume/revoke/finish/set_mode/set_grant）
    校验 expected_rev——不匹配 ⇒ conflict 带最新版本（防旧面板盲发控制）。
    expected_rev=None 表示调用方未带（直调兼容）；HTTP 层要求必带。
    wake 是入账不是变更，不校验版本。
    """
    _REV_CHECK_OPS = ("pause", "resume", "revoke", "finish", "set_mode",
                      "set_grant", "set_review_at")
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


async def save_plan(work_id: str, plan_card_id: str, *, assignment_id: str,
                    expected_rev: int, plan: Any,
                    by: str = "user", source_cards: Optional[set[str]] = None) -> dict[str, Any]:
    """保存完整新计划（乐观并发）：expected_rev 不匹配 ⇒ CONFLICT+最新版本。"""
    if not isinstance(plan, dict):
        raise CoordinationError("validation", "plan 必须是对象（完整的新计划）")
    plan = {key: value for key, value in plan.items() if key != "_coord_sources"}
    if source_cards is not None:
        plan["_coord_sources"] = sorted(source_cards)
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
        new_rev = cur_rev + 1
        conn.execute(
            "UPDATE coordination_appointments"
            " SET plan=?, rev=rev+1, updated_at=?, op_seq=op_seq+1"
            " WHERE id=?", (_dumps(plan), now, row["id"]))
        conn.execute(
            "INSERT INTO coordination_plan_revisions"
            " (work_id,plan_card_id,assignment_id,rev,plan,saved_at,saved_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (work_id, plan_card_id, assignment_id, new_rev, _dumps(plan),
             now, str(by)))
        row2 = conn.execute(
            "SELECT * FROM coordination_appointments WHERE id=?",
            (row["id"],)).fetchone()
        return {"ok": True, "appointment": _row_to_appt(row2)}

    return await card_db.run_write(work_id, _fn)


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

async def add_observation(work_id: str, plan_card_id: str, *,
                          assignment_id: str, kind: str, status: str,
                          batch_id: str = "", decision: str = "",
                          item_id: str = "", reason: str = "",
                          evidence_refs: Any = None, question: str = "",
                          model_used: str = "", usage: Any = None,
                          detail: Any = None) -> dict[str, Any]:
    """记一条模型调用账（弱模型观察 / 强模型起轮）。只记账，不产生效果。"""
    now = _now()

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
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

    return await card_db.run_write(work_id, _fn)


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
                        " length(plan) AS plan_len"
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
                     if k != "_coord_sources"},
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
        if appt is not None:
            last_turn = conn.execute("SELECT status,detail,created_at FROM coordination_observations"
                                     " WHERE assignment_id=? AND kind='strong_turn'"
                                     " ORDER BY id DESC LIMIT 1", (appt["assignment_id"],)).fetchone()
            if last_turn:
                detail = _loads(last_turn["detail"], {})
                out["last_turn"] = {"status": last_turn["status"],
                                    "trigger": detail.get("trigger", ""),
                                    "at": last_turn["created_at"]}
            origin = conn.execute("SELECT saved_by,rev FROM coordination_plan_revisions"
                                  " WHERE assignment_id=? ORDER BY id DESC LIMIT 1",
                                  (appt["assignment_id"],)).fetchone()
            if origin and str(origin["saved_by"]).startswith("strong:"):
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
