"""goal 状态服务 —— 对应 ``goal/src/index.ts``（592 行，逐条移植）。

与原版一一对应的语义：
- ``get`` / ``disarm`` / ``create`` / ``edit`` / ``pause`` / ``resume`` /
  ``complete`` / ``block`` / ``clear``
- 每次变更都是**一条 durable ``goal/change`` 会话事件**（全量快照）
- ``GoalRef{id, revision}`` 比较并交换；过期 ref 一律拒
- **activation 永不落盘**：进程启动 / 会话开始一律 disarmed；只有
  ``create`` 与 ``resume`` 会 arm
- 任何**不是本进程刚写**的 goal/change，都会把 activation 重置为 disarmed
  （原版 ``sync`` 的 ``pendingActivation.seq === event.seq`` 判据）

与我们的存储对接：
- 会话事件 = ``events/<card>.jsonl``（``event_log.append_sync``，带 seq）
- 数据库列 = **投影**（``cards.goal_*``），供画布与旧读方；权威永远是事件日志
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..storage import card_db, card_store, event_log
from . import fold as fold_mod
from .runtime import (
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_CHANGE_EVENT,
    GOAL_CHANGE_VERSION,
    GoalError,
)
from .types import (
    CreateGoalRequest,
    EditGoalRequest,
    GoalBlockReason,
    GoalChanged,
    GoalRef,
    GoalSnapshot,
    GoalView,
)

logger = logging.getLogger(__name__)

_KEBAB_OK = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _resolve_max_rounds(value: Optional[int], default: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GoalError("maxGoalRounds must be a positive safe integer", "GOAL_INVALID_MAX_ROUNDS")
    return value


def _resolve_objective(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoalError("goal objective must be a non-empty string", "GOAL_INVALID_OBJECTIVE")
    return value.strip()


def _resolve_block_reason(code: str, message: str) -> GoalBlockReason:
    ok = bool(code) and code[0].isalpha() and code[0].islower() and all(
        c.isdigit() or c.islower() or c == "-" for c in code
    ) and "--" not in code and not code.endswith("-")
    if not ok or not isinstance(message, str) or not message.strip():
        raise GoalError(
            "goal block reason requires a lower-kebab-case code and a non-empty message",
            "GOAL_INVALID_BLOCK_REASON",
        )
    return GoalBlockReason(code=code, message=message.strip())


@dataclass
class _GoalCache:
    state: fold_mod.GoalFoldState
    activation: str = "disarmed"
    observed_seq: int = 0
    pending_activation: Optional[tuple[int, str]] = None
    backfilled: bool = False


@dataclass
class _Listeners:
    callbacks: list[Callable[[str, str, GoalChanged], None]] = field(default_factory=list)


class GoalService:
    """``ctx.goals`` 的对应物。"""

    def __init__(self, default_max_rounds: int = DEFAULT_MAX_GOAL_ROUNDS) -> None:
        self.default_max_rounds = _resolve_max_rounds(default_max_rounds, DEFAULT_MAX_GOAL_ROUNDS)
        self._caches: dict[tuple[str, str], _GoalCache] = {}
        # [runner 分槽环 2026-11-05] asyncio.Lock 跨环首绑即死锁；而本锁的
        # 冲突域是 (work,card) 键——同卡操作天然在同一槽环串行、跨环碰的
        # 永远是不同键 ⇒ 按环分槽既安全又必须。
        self._mutexes: dict[int, asyncio.Lock] = {}
        self._listeners = _Listeners()

    def _mutex(self) -> asyncio.Lock:
        lp = asyncio.get_running_loop()
        m = self._mutexes.get(id(lp))
        if m is None:
            m = asyncio.Lock()
            self._mutexes[id(lp)] = m
        return m

    # ------------------------------------------------------------ 监听
    def on_changed(self, cb: Callable[[str, str, GoalChanged], None]) -> None:
        """订阅 ``goal/changed``（原版 scoped 事件；此处按 (work, card) 回调）。"""
        self._listeners.callbacks.append(cb)

    def _emit(self, work_id: str, card_id: str, change: GoalChanged) -> None:
        for cb in list(self._listeners.callbacks):
            try:
                cb(work_id, card_id, change)
            except Exception as exc:  # noqa: BLE001 监听失败必须被隔离（原版 contained）
                logger.warning("[goal] goal/changed 监听异常 card=%s: %s", card_id, exc)

    # ------------------------------------------------------------ 缓存 / 同步
    def _legacy_meta(self, work_id: str, card_id: str) -> dict[str, Any]:
        try:
            row = card_db.read_sync(work_id, lambda conn: card_db.get_card_row(conn, card_id), None)
            return dict(row) if row is not None else {}
        except Exception:  # noqa: BLE001 读不出就当没有
            return {}

    def _seed(self, work_id: str, card_id: str) -> _GoalCache:
        """首次接触：从事件日志重放；旧数据（有 goal_state 无 goal/change）一次性回填。"""
        state = fold_mod.empty_goal_fold_state()
        events = event_log.read(work_id, card_id)
        observed = 0
        for ev in events:
            try:
                fold_mod.apply_goal_event(state, ev)
            except Exception as exc:  # noqa: BLE001 坏流 fail-loud，但不能拖死服务
                logger.error("[goal] 事件流重放失败 card=%s seq=%s: %s", card_id, ev.get("seq"), exc)
                raise GoalError(
                    f"durable goal stream is corrupt at seq {ev.get('seq')}: {exc}",
                    "GOAL_INVALID_TRANSITION",
                ) from exc
            observed = int(ev.get("seq") or observed)
        cache = _GoalCache(state=state, activation="disarmed", observed_seq=observed)
        self._caches[(work_id, card_id)] = cache
        return cache

    def _cache(self, work_id: str, card_id: str) -> _GoalCache:
        key = (work_id, card_id)
        cache = self._caches.get(key)
        if cache is None:
            cache = self._seed(work_id, card_id)
        return cache

    def _sync(self, work_id: str, card_id: str, cache: _GoalCache) -> None:
        """增量吃进新事件；activation 只保留本进程刚写的那个（原版 sync 判据）。"""
        # event_log.read 是 **seq >= from_seq**（闭区间），所以要从"已读到的下一个"开始
        events = event_log.read(work_id, card_id, from_seq=cache.observed_seq + 1)
        for ev in events:
            fold_mod.apply_goal_event(cache.state, ev)
            if ev.get("type") == GOAL_CHANGE_EVENT:
                seq = ev.get("seq")
                if cache.pending_activation is not None and cache.pending_activation[0] == seq:
                    cache.activation = cache.pending_activation[1]
                else:
                    cache.activation = "disarmed"
            cache.observed_seq = int(ev.get("seq") or cache.observed_seq) or cache.observed_seq

    async def _prepare(self, work_id: str, card_id: str) -> _GoalCache:
        if not card_store.card_exists(work_id, card_id):
            raise GoalError(f"card {card_id} is not live", "GOAL_AGENT_NOT_LIVE")
        cache = self._cache(work_id, card_id)
        self._sync(work_id, card_id, cache)
        if cache.state.goal is None and not cache.backfilled:
            cache.backfilled = True
            await self._backfill_legacy(work_id, card_id, cache)
        return cache

    async def _backfill_legacy(self, work_id: str, card_id: str, cache: _GoalCache) -> None:
        """旧数据迁移：DB 里有 goal_state 但事件日志里没有 goal/change ⇒ 补一条 create。

        为什么需要：本端口之前，目标状态只存在 ``cards.goal_state`` 一列，没有事件
        溯源。若不回填，历史卡（5703 的 32 张）在严格 fold 下会「没有目标」，
        续轮驱动直接不管它们——那就等于换了个系统。
        """
        row = self._legacy_meta(work_id, card_id)
        phase = str(row.get("goal_state") or "")
        objective = str(row.get("goal") or "").strip()
        if not phase or not objective:
            return
        if phase not in ("active", "paused", "blocked", "complete"):
            return
        now = _now_ms()
        snap = GoalSnapshot(
            id=f"goal-{work_id}-{card_id}", revision=1,
            objective=objective, phase="active",
            max_goal_rounds=DEFAULT_MAX_GOAL_ROUNDS,
        )
        change = {
            "kind": GOAL_CHANGE_EVENT, "version": GOAL_CHANGE_VERSION, "operation": "create",
            "goal": snap.as_json(), "roundsStarted": 0,
            "createdAt": now, "updatedAt": now,
        }
        self._commit(work_id, card_id, cache, change, "disarmed")
        if phase != "active":
            target = phase
            op = {"paused": "pause", "blocked": "block", "complete": "complete"}[target]
            nxt = GoalSnapshot(
                id=snap.id, revision=2, objective=objective, phase=target,  # type: ignore[arg-type]
                max_goal_rounds=snap.max_goal_rounds,
                blocked_reason=GoalBlockReason("legacy-migrated", "旧数据迁移：原状态 " + target)
                if target == "blocked" else None,
            )
            change2 = {
                "kind": GOAL_CHANGE_EVENT, "version": GOAL_CHANGE_VERSION, "operation": op,
                "goal": nxt.as_json(), "roundsStarted": 0,
                "createdAt": now, "updatedAt": max(now, now),
            }
            self._commit(work_id, card_id, cache, change2, "disarmed")
        logger.info("[goal] 旧目标已回填为事件 card=%s phase=%s", card_id, phase)

    # ------------------------------------------------------------ 读
    async def get(self, work_id: str, card_id: str) -> Optional[GoalView]:
        cache = await self._prepare(work_id, card_id)
        return self._view(cache)

    async def refresh_projection(self, work_id: str, card_id: str) -> None:
        """把 fold 的最新结果重新投影到 DB 列。

        为什么需要：``roundsStarted`` 是在**续轮消息被接纳**时推进的（fold 里），
        不是 goal/change 提交时。若不在这里补一次投影，DB 的 ``goal_rounds``
        会一直停在旧值（原版由 sessionProjections 逐事件刷新，不需要这步）。
        """
        cache = self._caches.get((work_id, card_id))
        if cache is None:
            return
        try:
            self._sync(work_id, card_id, cache)
            self._project(work_id, card_id, cache)
        except Exception as exc:  # noqa: BLE001 投影失败不阻断
            logger.warning("[goal] 投影刷新失败 card=%s: %s", card_id, exc)

    def is_armed(self, work_id: str, card_id: str) -> bool:
        cache = self._caches.get((work_id, card_id))
        return bool(cache and cache.activation == "armed")

    async def disarm(self, work_id: str, card_id: str) -> Optional[GoalView]:
        cache = await self._prepare(work_id, card_id)
        cache.activation = "disarmed"
        return self._view(cache)

    async def arm(self, work_id: str, card_id: str) -> Optional[GoalView]:
        """重新授予续轮权（**重启恢复用**）。

        [goal 重启恢复 2026-10-28 · 用户实测「goal 明明没完成却停下了」]
        ``activation`` 按原版设计**永不落盘**，进程一重启全部 disarmed——
        数据库里 goal 还是 active、界面上看着在跑，驱动却再也不认它。
        这个方法就是"重启后把 active 的目标重新授上权"的唯一入口。

        只在 ``phase == "active"`` 时生效：paused/blocked/complete 不给权
        （它们本来就不该自动跑，要跑得显式 resume）。
        """
        cache = await self._prepare(work_id, card_id)
        goal = cache.state.goal
        if goal is None or goal.phase != "active":
            return None
        cache.activation = "armed"
        return self._view(cache)

    # ------------------------------------------------------------ 写
    async def create(self, work_id: str, card_id: str, request: CreateGoalRequest) -> GoalView:
        async with self._mutex():
            cache = await self._prepare(work_id, card_id)
            current = cache.state.goal
            if current is not None and current.phase != "complete":
                raise GoalError(
                    f'goal "{current.id}" already exists with phase "{current.phase}"',
                    "GOAL_ALREADY_EXISTS",
                )
            now = _now_ms()
            snap = GoalSnapshot(
                id=f"goal-{int(time.time() * 1000)}-{card_id}",
                revision=1,
                objective=_resolve_objective(request.objective),
                phase="active",
                max_goal_rounds=_resolve_max_rounds(request.max_goal_rounds, self.default_max_rounds),
            )
            change = {
                "kind": GOAL_CHANGE_EVENT, "version": GOAL_CHANGE_VERSION, "operation": "create",
                "goal": snap.as_json(), "roundsStarted": 0, "createdAt": now, "updatedAt": now,
            }
            self._commit(work_id, card_id, cache, change, "armed")
            return self._require_view(cache)

    async def edit(self, work_id: str, card_id: str, ref: GoalRef,
                   request: EditGoalRequest) -> GoalView:
        async with self._mutex():
            cache = await self._prepare(work_id, card_id)
            current = self._expect_current(cache, ref)
            if request.objective is None and request.max_goal_rounds is None:
                raise GoalError("goal edit requires objective and/or maxGoalRounds", "GOAL_INVALID_EDIT")
            snap = GoalSnapshot(
                id=current.id,
                revision=current.revision + 1,
                objective=_resolve_objective(request.objective) if request.objective is not None else current.objective,
                phase=current.phase,
                max_goal_rounds=(_resolve_max_rounds(request.max_goal_rounds, self.default_max_rounds)
                                 if request.max_goal_rounds is not None else current.max_goal_rounds),
                blocked_reason=current.blocked_reason,
            )
            self._commit_current(work_id, card_id, cache, "edit", snap, cache.activation)
            return self._require_view(cache)

    async def pause(self, work_id: str, card_id: str, ref: GoalRef) -> GoalView:
        return await self._transition(work_id, card_id, ref, "pause", ("active",), "paused", "disarmed")

    async def resume(self, work_id: str, card_id: str, ref: GoalRef) -> GoalView:
        async with self._mutex():
            cache = await self._prepare(work_id, card_id)
            current = self._expect_current(cache, ref)
            if current.phase not in ("active", "paused", "blocked"):
                raise self._transition_error(current, "resume", ("active", "paused", "blocked"))
            if current.phase == "active" and cache.activation == "armed":
                raise GoalError(f'goal "{current.id}" is already active and armed', "GOAL_INVALID_TRANSITION")
            if cache.state.rounds_started >= current.max_goal_rounds:
                raise GoalError(
                    f'goal "{current.id}" exhausted {current.max_goal_rounds} goal rounds; '
                    "increase maxGoalRounds before resuming",
                    "GOAL_INVALID_TRANSITION",
                )
            snap = GoalSnapshot(current.id, current.revision + 1, current.objective, "active",
                                current.max_goal_rounds, None)
            self._commit_current(work_id, card_id, cache, "resume", snap, "armed")
            return self._require_view(cache)

    async def complete(self, work_id: str, card_id: str, ref: GoalRef) -> GoalView:
        return await self._transition(
            work_id, card_id, ref, "complete", ("active", "paused", "blocked"), "complete", "disarmed")

    async def block(self, work_id: str, card_id: str, ref: GoalRef,
                    code: str, message: str) -> GoalView:
        async with self._mutex():
            cache = await self._prepare(work_id, card_id)
            current = self._expect_current(cache, ref)
            if current.phase != "active":
                raise self._transition_error(current, "block", ("active",))
            snap = GoalSnapshot(current.id, current.revision + 1, current.objective, "blocked",
                                current.max_goal_rounds, _resolve_block_reason(code, message))
            self._commit_current(work_id, card_id, cache, "block", snap, "disarmed")
            return self._require_view(cache)

    async def clear(self, work_id: str, card_id: str, ref: GoalRef) -> GoalRef:
        async with self._mutex():
            cache = await self._prepare(work_id, card_id)
            current = self._expect_current(cache, ref)
            tombstone = GoalRef(current.id, current.revision + 1)
            change = {
                "kind": GOAL_CHANGE_EVENT, "version": GOAL_CHANGE_VERSION, "operation": "clear",
                "cleared": tombstone.as_json(), "clearedAt": self._next_mutation_time(cache),
            }
            self._commit(work_id, card_id, cache, change, "disarmed")
            return tombstone

    async def _transition(self, work_id: str, card_id: str, ref: GoalRef, operation: str,
                          allowed: tuple[str, ...], phase: str, activation: str) -> GoalView:
        async with self._mutex():
            cache = await self._prepare(work_id, card_id)
            current = self._expect_current(cache, ref)
            if current.phase not in allowed:
                raise self._transition_error(current, operation, allowed)
            snap = GoalSnapshot(current.id, current.revision + 1, current.objective, phase,  # type: ignore[arg-type]
                                current.max_goal_rounds, None)
            self._commit_current(work_id, card_id, cache, operation, snap, activation)
            return self._require_view(cache)

    # ------------------------------------------------------------ 内部
    def _expect_current(self, cache: _GoalCache, ref: GoalRef) -> GoalSnapshot:
        current = cache.state.goal
        if current is None:
            raise GoalError("no current goal", "GOAL_NOT_FOUND")
        if ref.id != current.id or ref.revision != current.revision:
            raise GoalError(
                f'stale goal ref "{ref.id}" revision {ref.revision}; '
                f'current is "{current.id}" revision {current.revision}',
                "GOAL_STALE_REVISION",
            )
        return current

    def _transition_error(self, current: GoalSnapshot, operation: str,
                          allowed: tuple[str, ...]) -> GoalError:
        return GoalError(
            f'cannot {operation} goal "{current.id}" from phase "{current.phase}"; '
            f'expected {" or ".join(allowed)}',
            "GOAL_INVALID_TRANSITION",
        )

    def _next_mutation_time(self, cache: _GoalCache) -> int:
        updated = cache.state.updated_at
        if updated is None:
            raise GoalError("current goal cache lacks updatedAt", "GOAL_INVALID_TRANSITION")
        return max(_now_ms(), int(updated))

    def _commit_current(self, work_id: str, card_id: str, cache: _GoalCache, operation: str,
                        snap: GoalSnapshot, activation: str) -> None:
        created = cache.state.created_at
        if created is None:
            raise GoalError("current goal cache lacks createdAt", "GOAL_INVALID_TRANSITION")
        change = {
            "kind": GOAL_CHANGE_EVENT, "version": GOAL_CHANGE_VERSION, "operation": operation,
            "goal": snap.as_json(), "roundsStarted": cache.state.rounds_started,
            "createdAt": created, "updatedAt": self._next_mutation_time(cache),
        }
        self._commit(work_id, card_id, cache, change, activation)

    def _commit(self, work_id: str, card_id: str, cache: _GoalCache,
                change: dict[str, Any], activation: str) -> None:
        """写一条 durable 事件 → 增量同步 → 投影 → 发活通知。"""
        cache.pending_activation = (int(event_log.last_seq(work_id, card_id)) + 1, activation)
        try:
            rec = event_log.append_sync(work_id, card_id, {"type": GOAL_CHANGE_EVENT, "data": change})
            if not rec:
                raise GoalError("goal change could not be persisted", "GOAL_INVALID_TRANSITION")
            self._sync(work_id, card_id, cache)
        finally:
            cache.pending_activation = None
        self._project(work_id, card_id, cache)
        goal = self._view(cache)
        if change["operation"] == "clear":
            ref = GoalRef(change["cleared"]["id"], change["cleared"]["revision"])
        else:
            ref = GoalRef(change["goal"]["id"], change["goal"]["revision"])
        self._emit(work_id, card_id, GoalChanged(
            operation=change["operation"],
            ref=ref,
            goal=goal,
        ))

    def _project(self, work_id: str, card_id: str, cache: _GoalCache) -> None:
        """把 fold 结果投影进 ``cards`` 列（权威仍是事件日志）。"""
        goal = cache.state.goal
        fields: dict[str, Any] = {
            "goal_state": goal.phase if goal is not None else "",
            "goal_rounds": cache.state.rounds_started,
        }
        if goal is None:
            fields.update({"goal_id": "", "goal_revision": 0, "goal_max_rounds": 0,
                           "goal_created_at": 0, "goal_updated_at": 0,
                           "goal_blocked_code": "", "goal_blocked_message": ""})
        else:
            fields.update({
                "goal": goal.objective,
                "goal_id": goal.id,
                "goal_revision": goal.revision,
                "goal_max_rounds": goal.max_goal_rounds,
                "goal_created_at": int(cache.state.created_at or 0),
                "goal_updated_at": int(cache.state.updated_at or 0),
                "goal_blocked_code": goal.blocked_reason.code if goal.blocked_reason else "",
                "goal_blocked_message": goal.blocked_reason.message if goal.blocked_reason else "",
            })
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [card_id]

        def _write(conn: Any) -> None:
            conn.execute(f"UPDATE cards SET {cols} WHERE card_id=?", values)

        try:
            card_db._write_sync(work_id, _write)  # noqa: SLF001 与 run_write 同一把锁
        except Exception as exc:  # noqa: BLE001 投影失败不阻断（事件已落盘）
            logger.warning("[goal] 投影写入失败 card=%s: %s", card_id, exc)

    def _view(self, cache: _GoalCache) -> Optional[GoalView]:
        goal = cache.state.goal
        if goal is None:
            return None
        return GoalView(
            id=goal.id, revision=goal.revision, objective=goal.objective, phase=goal.phase,
            max_goal_rounds=goal.max_goal_rounds, rounds_started=cache.state.rounds_started,
            created_at=float(cache.state.created_at or 0), updated_at=float(cache.state.updated_at or 0),
            activation=cache.activation, blocked_reason=goal.blocked_reason,  # type: ignore[arg-type]
        )

    def _require_view(self, cache: _GoalCache) -> GoalView:
        view = self._view(cache)
        if view is None:
            raise GoalError("snapshot commit cleared the goal unexpectedly", "GOAL_NOT_FOUND")
        return view


#: 进程内单例（原版 ``ctx.goals``）。
SERVICE = GoalService()


def get_service() -> GoalService:
    return SERVICE
