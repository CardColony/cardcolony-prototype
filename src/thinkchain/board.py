# -*- coding: utf-8 -*-
"""思维链板子基建（迭代二 S1 / 方案 §4.3＋§4.6）。

职责单一：板上数据结构与三层落盘。不含工具语义（S2 plan_tools）、不含
自判收口（S3）、不含 runner 接缝（S4）；`src/api/` 路由层零引用本件
（任务书红线，S1 交付判据之一）。

板上数据（plans/plan_{card_id}.json，任务书 S1 步骤 1 逐字）::

    {"card_id", "name",
     "steps": [{"id", "prompt", "status", "type", "has_artifact"}],
     "current", "revisions": [...], "created_at"}

type 值域＝``VALID_STEP_TYPES``（迭代三 S3 建板入口契约：**写入口两处**
校验——create_board 与 apply_revision·insert；读路径（get_board/恢复）
零校验，存量空串板原样可读不升格）；revisions 由改板动作自动记（动作/
时间/改前快照/改后快照）；dirty 是内存调度标志不落盘。

三层落盘（任务书 S1 步骤 2）：建板同步写（一次模型调用的产物，丢了亏）；
**步进指针同步写**（step_complete 处理器内、返回工具结果之前完成
tmp→replace——救命信息不等异步）；产物正文与修订异步（create_task＋
置脏合并——「异步」指调度时机，写本身仍原子）。

落盘路径：`works/{work_id}/plans/plan_{card_id}.json`，tmp→os.replace
原子替换（memory_store.py:179-182 同法）；产物正文落
`works/{work_id}/thinkchain/{card_id}/{seq}-{step_id}.md`（S3 finalize
核对位，红线 8 白名单双目录）。

内存键（R2-① 用户裁定）：``(work_id, card_id)`` 双键——card_id 在 work
内自增、非全局唯一，单键跨 work 会撞车。语义仍是「绑卡不绑会话」。

锁（任务书 S1 步骤 7）：per-card 轻锁，不碰回合互斥。⚠️ 施工裁定（呈报
项）：任务书原文「锁内挪指针、锁外写盘」存在竞态窗口——置脏合并任务
持有的旧快照可回退覆盖新指针（违反验收④「重复指针写不撕裂」与防护③
「指针同步落」）；本件取**锁内同步写**（_atomic_write_json 无 await 让
出点、持锁微秒级），串行化全部写盘，消窗口且不违「轻锁」意图。

恢复序（任务书 S1 步骤 4）：内存有→返回；内存无→读盘重建→返回；盘也
无→None。**不做产物重放、不设恢复校验器**；JSON 解析失败明报
BoardRestoreError（含路径），绝不静默、绝不自动从头。

ops 与 revision 消费（12:24 版任务书 S1 步骤 8）：
  * 建/推/改/删/孤儿动作直接 ``session_ops.ops_log``（fail-open），action
    ＝plan_create/plan_advance/plan_revise/plan_finalize/plan_cleanup；
  * 改板四动作另调 ``revision_store.record_revision(work_id, "plan",
    change, detail)``＋板上 revisions[] 追加（双留痕：板内结构＋ops 流水）。

卡面终态标记（S3 finalize 消费）：front matter 键 ``thinkchain_final``——
与 state_store.FM_FINAL="toolcard_final" 同法，经 card_store.update_card
写闸（card_store 键清单已同批扩入本键）。孤儿扫描判定＝查此键，**零
时间戳**（不按 mtime/age 判生死，只按卡面事实）。

路径纪律：一切路径动态取 ``session_store.WORKS_DIR``（session_ops.
ops_path 同法）——conftest 沙箱 monkeypatch 模块级常量后本件自动跟随，
绝不缓存 Path 于模块级。

清理（任务书 S1 步骤 5）：链终态→删 plan 文件＋plans/ 同目录 .tmp 残留
＋内存条目；删除 fail-open（失败留运维日志，下次启动扫描兜住）。产物
目录 thinkchain/{card_id}/ 是模型产物档案，**不在清理范围**。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from ..storage import card_store, revision_store, session_ops, session_store
from ..storage.hash_utils import parse_front_matter

logger = logging.getLogger("thinkchain.board")

# 卡面终态键（命名法同 state_store.FM_FINAL="toolcard_final"）
FM_THINKCHAIN_FINAL = "thinkchain_final"

# [迭代三 S3·决策 A] 步 type 值域权威＝代码常量（注册表只管语义 display/
# note/judge）。将来加第三种类型时改为读 behavior_registry.json 的 types 键，
# 读取失败回落本两值拒绝（fail-safe 方向＝「拒绝非法」不是「放行一切」）。
# 校验只出现在两处写入口：create_board 与 apply_revision·insert（读路径
# 零校验——存量空串板原样可读、不报错、不升格，防在途任务被误伤）。
VALID_STEP_TYPES: list[str] = ["execute", "think"]

LOCK_TIMEOUT = 1.0  # 与 card_store.LOCK_TIMEOUT 同值：锁内是内存操作＋小文件原子写

_PLANS_DIR = "plans"            # works/{w}/plans/ —— 板文件目录（红线 8 白名单）
_THINKCHAIN_DIR = "thinkchain"  # works/{w}/thinkchain/{card}/ —— 产物目录

# 内存注册表（R2-① 双键）
_boards: dict[tuple[str, str], "PlanBoard"] = {}

_locks: dict[tuple[str, str], asyncio.Lock] = {}

# 背景任务强引用集（asyncio 文档要求：create_task 返回值不持引用可能被 GC）
_background_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------- 异常族

class BoardBusyError(Exception):
    """板锁获取超时（错误码 423 语义，防护④：锁超时明报，不静默等待）。"""

    code = 423


class BoardRestoreError(Exception):
    """板文件断尾／不可解析，重建失败（防护②：不做产物重放，明报不编造）。"""


# ---------------------------------------------------------------- 路径（动态取，沙箱跟随）

def _work_root(work_id: str) -> Path:
    return session_store.WORKS_DIR / work_id


def _plans_dir(work_id: str) -> Path:
    return _work_root(work_id) / _PLANS_DIR


def plan_path(work_id: str, card_id: str) -> Path:
    """板文件：works/{w}/plans/plan_{card_id}.json（任务书 S1 步骤 3）。"""
    return _plans_dir(work_id) / f"plan_{card_id}.json"


def artifacts_dir(work_id: str, card_id: str) -> Path:
    """产物目录：works/{w}/thinkchain/{card}/（红线 8 白名单第二目录）。"""
    return _work_root(work_id) / _THINKCHAIN_DIR / card_id


def artifact_path(work_id: str, card_id: str, seq: int, step_id: str) -> Path:
    """产物文件：works/{w}/thinkchain/{card}/{seq}-{step_id}.md（S3 核对位）。"""
    return artifacts_dir(work_id, card_id) / f"{seq:02d}-{step_id}.md"


# ---------------------------------------------------------------- 锁

def _get_lock(key: tuple[str, str]) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


async def _acquire(lock: asyncio.Lock) -> None:
    try:
        await asyncio.wait_for(lock.acquire(), timeout=LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        raise BoardBusyError("板锁获取超时(423)，稍后重试") from None


# ---------------------------------------------------------------- 原子写

def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """tmp→fsync→os.replace 原子落盘（memory_store.py:179-182 同法）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- PlanBoard

class PlanBoard:
    """一块板：持有 (work_id, card_id) 与板上数据。

    实例不长期游离——恢复/新建后即注册进 _boards；cleanup 后从注册表
    摘除，持有旧引用的调用方再操作会被 ``_require_live`` 拒绝（防护⑤：
    板已清理→如实回报，绝不借旧引用复活落盘）。
    """

    def __init__(self, work_id: str, card_id: str, data: dict[str, Any]) -> None:
        self._work_id = work_id
        self._card_id = card_id
        self._data = data
        self._flush_pending = False  # 置脏合并标志（内存态，不落盘）

    # -- 只读视图 -----------------------------------------------------

    @property
    def work_id(self) -> str:
        return self._work_id

    @property
    def card_id(self) -> str:
        return self._card_id

    def to_snapshot(self) -> dict[str, Any]:
        """深拷贝快照（给工具层返回模型用，不含内部引用与脏标志）。"""
        snap = copy.deepcopy(self._data)
        snap["total"] = len(snap["steps"])
        snap["current_prompt"] = self.current_step()["prompt"] if self.current_step() else None
        return snap

    def current_step(self) -> Optional[dict[str, Any]]:
        cur = self._data["current"]
        steps = self._data["steps"]
        if 0 <= cur < len(steps):
            return steps[cur]
        return None  # 越界＝全部步已推进（位置事实，不是评价）

    def _require_live(self) -> None:
        if _boards.get((self._work_id, self._card_id)) is not self:
            raise BoardRestoreError("本计划已终结（板已清理），无可推进")

    # -- 写路径 -------------------------------------------------------

    async def _save_locked(self) -> None:
        """原子落盘。**须持锁调用**——串行化写盘是「不撕裂」的根
        （施工裁定：任务书「锁外写盘」与验收④存在竞态窗口，见模块头）。"""
        _atomic_write_json(plan_path(self._work_id, self._card_id), self._data)

    def _schedule_flush(self) -> None:
        """修订/产物置脏合并落盘（任务书 S1 步骤 2：create_task＋置脏合并）。

        同圈多次置脏合并为一次写（_flush_pending 挡重）；锁内同步写，
        串行化保证不撕裂。
        """
        if self._flush_pending:
            return
        self._flush_pending = True

        async def _flush() -> None:
            await asyncio.sleep(0)  # 让出一次：同圈后续置脏合并进来
            key = (self._work_id, self._card_id)
            lock = _get_lock(key)
            try:
                await _acquire(lock)
            except BoardBusyError:
                self._flush_pending = False
                self._schedule_flush()  # 锁忙→重新排队（不丢写）
                return
            try:
                self._flush_pending = False
                if _boards.get(key) is self:  # 清理后不再回头写
                    await self._save_locked()
            finally:
                lock.release()

        task = asyncio.get_running_loop().create_task(_flush())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def mark_done_and_advance(self, result_summary: str = "") -> dict[str, Any]:
        """当前步标 done＋指针前移，同锁原子＋指针同步落盘（防护③）。

        幂等口径（任务书 S2 步骤 3）：跨请求重复交无法从板面识别，防护
        以同锁重入为准——进锁后发现当前步已是 done（指针/状态不一致的
        异常态），不推进、不撕裂，如实返回当前步要求。result_summary 只
        随 ops 留痕；空 result 的拒绝是工具层（S2）的判定，本件不重复判。
        """
        key = (self._work_id, self._card_id)
        lock = _get_lock(key)
        await _acquire(lock)
        try:
            self._require_live()
            steps = self._data["steps"]
            cur = self._data["current"]
            if cur >= len(steps):
                # 越界再调：接得住（防护⑤/⑥家族），不抛
                return {"ok": True, "advanced": False, "all_steps_done": True, "prompt": None}
            st = steps[cur]
            if st["status"] == "done":
                return {"ok": True, "advanced": False, "idempotent": True,
                        "prompt": st["prompt"], "all_steps_done": False}
            st["status"] = "done"
            self._data["current"] = cur + 1
            await self._save_locked()  # 指针同步落盘（锁内原子，返回工具结果之前完成）
            nxt = steps[cur + 1] if cur + 1 < len(steps) else None
            session_ops.ops_log("plan_advance", self._work_id, self._card_id, {
                "step": st["id"],
                "next": nxt["id"] if nxt else None,
                "all_done": nxt is None,
            })
            return {"ok": True, "advanced": True, "step": st["id"],
                    "type": st.get("type", ""),   # [迭代三 S4] 步类型随返回（扩值拼 behavior 用）
                    "prompt": nxt["prompt"] if nxt else None,
                    "all_steps_done": nxt is None}
        finally:
            lock.release()

    async def apply_revision(self, change: str, detail: dict[str, Any]) -> dict[str, Any]:
        """改板四动作（reorder/insert/remove/modify）——纯数组序/成员变更，
        结构性拒绝零内容判断（screenplay-board 重排先例）。

        每次改板：板上 revisions[] 追加（动作/时间/改前快照/改后快照，代码
        自动记）＋ revision_store.record_revision(entity="plan")＋ops
        plan_revise；board.json 走**置脏异步合并落盘**（修订不是救命信息，
        指针才是——同步写只留给建板与步进）。
        """
        key = (self._work_id, self._card_id)
        lock = _get_lock(key)
        await _acquire(lock)
        try:
            self._require_live()
            steps = self._data["steps"]
            old_cur = self._data["current"]
            old_id = steps[old_cur]["id"] if 0 <= old_cur < len(steps) else None

            if change == "reorder":
                order = detail.get("order")
                if (not isinstance(order, list)
                        or sorted(map(str, order)) != sorted(s["id"] for s in steps)):
                    return {"ok": False, "error": "order 必须是现有步 id 的全排列"}
                by_id = {s["id"]: s for s in steps}
                steps = [by_id[i] for i in order]
            elif change == "insert":
                after_id = detail.get("after_id")  # None = 头部
                new_step = detail.get("step") or {}
                prompt = new_step.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    return {"ok": False, "error": "insert 需要非空 prompt"}
                new_id = new_step.get("id")
                if not isinstance(new_id, str) or not new_id.strip():
                    return {"ok": False, "error": "insert 需要步 id"}
                if any(s["id"] == new_id for s in steps):
                    return {"ok": False, "error": f"步 id 已存在: {new_id}"}
                if after_id is None:
                    idx = 0
                else:
                    idx = next((i + 1 for i, s in enumerate(steps) if s["id"] == after_id), None)
                    if idx is None:
                        return {"ok": False, "error": f"insert 锚点不存在: {after_id}"}
                # [迭代三 S3·步骤 5] type 值域校验（写入口②）：与 create_board
                # 同值域同拒绝形态（读路径零校验不动）。
                ntype = new_step.get("type")
                if ntype not in VALID_STEP_TYPES:
                    return {"ok": False, "error": (
                        f"insert 步（{new_id}）type 缺或非法：type 必填且只能取 "
                        f"{'/'.join(VALID_STEP_TYPES)}，实为 {ntype!r}")}
                steps.insert(idx, {"id": new_id, "prompt": prompt, "status": "todo",
                                   "type": ntype, "has_artifact": False})
            elif change == "remove":
                rid = detail.get("id")
                target = next((s for s in steps if s["id"] == rid), None)
                if target is None:
                    return {"ok": False, "error": f"步不存在: {rid}"}
                if old_id is not None and rid == old_id:
                    # 任务书 S2 步骤 2：当前步不可删——删了指针没处指，结构性拒绝
                    return {"ok": False, "error": "当前步不可删"}
                if len(steps) <= 1:
                    return {"ok": False, "error": "至少保留一步"}
                steps = [s for s in steps if s["id"] != rid]
            elif change == "modify":
                rid = detail.get("id")
                prompt = detail.get("prompt")
                target = next((s for s in steps if s["id"] == rid), None)
                if target is None:
                    return {"ok": False, "error": f"步不存在: {rid}"}
                if not isinstance(prompt, str) or not prompt.strip():
                    return {"ok": False, "error": "modify 需要非空 prompt"}
                target["prompt"] = prompt
            else:
                return {"ok": False, "error": f"未知 change: {change}"}

            # 指针重定位（按 id 优先；被删则退回原位截断）
            if old_id is not None and any(s["id"] == old_id for s in steps):
                new_cur = next(i for i, s in enumerate(steps) if s["id"] == old_id)
            else:
                new_cur = min(old_cur, len(steps) - 1) if steps else 0

            before_snap = copy.deepcopy(self._data["steps"])
            self._data["steps"] = steps
            self._data["current"] = new_cur
            after_snap = copy.deepcopy(steps)
            self._data["revisions"].append({
                "action": change,
                "at": card_store._now_iso(),
                "before": before_snap,
                "after": after_snap,
            })
            self._schedule_flush()  # 修订置脏异步合并落盘（锁外调度，写时重新拿锁）
            revision_store.record_revision(self._work_id, "plan", change, detail)
            session_ops.ops_log("plan_revise", self._work_id, self._card_id,
                                {"change": change, **detail})
            return {"ok": True, "board": self.to_snapshot()}
        finally:
            lock.release()

    async def save_artifact_async(self, step_id: str, content: str) -> dict[str, Any]:
        """产物异步落库（works/{w}/thinkchain/{card}/{seq}-{step_id}.md）。

        「异步」＝调度时机：本调用即刻返回调度事实（写本身仍原子）。后台
        任务写完产物后置 has_artifact 并置脏合并回板。
        """
        self._require_live()
        idx = next((i for i, s in enumerate(self._data["steps"]) if s["id"] == step_id), None)
        if idx is None:
            return {"ok": False, "error": f"步不存在: {step_id}"}
        path = artifact_path(self._work_id, self._card_id, idx + 1, step_id)

        async def _write() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".md.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
                key = (self._work_id, self._card_id)
                lock = _get_lock(key)
                await _acquire(lock)
                try:
                    if _boards.get(key) is self:  # 清理后不再回头置位
                        for s in self._data["steps"]:
                            if s["id"] == step_id:
                                s["has_artifact"] = True
                        self._schedule_flush()
                finally:
                    lock.release()
            except Exception as exc:  # noqa: BLE001 产物落库失败不炸回合
                logger.warning("产物落库失败 card=%s step=%s: %s",
                               self._card_id, step_id, exc)
                session_ops.ops_log("plan_revise", self._work_id, self._card_id,
                                    {"artifact_failed": step_id, "err": str(exc)})

        task = asyncio.get_running_loop().create_task(_write())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"ok": True, "scheduled": True, "path": str(path)}

    async def cleanup_board(self) -> dict[str, Any]:
        """终态清理（任务书 S1 步骤 5）：删 plan 文件＋plans/ 同目录 .tmp 残留
        ＋内存条目。fail-open（永不抛；失败留运维日志，下次启动扫描兜住）。

        删除范围限死 plans/plan_{card_id}.json 与 plans/*.tmp（本迭代自产
        工件）；产物目录 thinkchain/{card_id}/ 是档案，不在清理范围。
        """
        key = (self._work_id, self._card_id)
        try:
            lock = _get_lock(key)
            await _acquire(lock)
            try:
                _boards.pop(key, None)
                p = plan_path(self._work_id, self._card_id)
                if p.exists():
                    p.unlink()
                pd = _plans_dir(self._work_id)
                if pd.exists():
                    for t in pd.glob("*.tmp"):
                        try:
                            t.unlink()
                        except OSError as exc:
                            logger.warning(".tmp 残留清理失败 %s: %s", t, exc)
                session_ops.ops_log("plan_cleanup", self._work_id, self._card_id, {})
                return {"ok": True}
            finally:
                lock.release()
        except Exception as exc:  # noqa: BLE001
            logger.warning("板清理异常 card=%s: %s", self._card_id, exc)
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 模块级入口

async def create_board(work_id: str, card_id: str, name: str,
                       steps_def: list[dict[str, Any]]) -> dict[str, Any]:
    """建板：同步落盘＋注册内存＋ops 留痕。已有活板→拒绝不覆盖。

    steps_def＝模型给的步定义 [{"id","prompt","has_artifact"?}]——校验仅
    结构性（steps 非空、id 唯一、prompt 非空，非法拒绝并给拒绝理由）；
    status 恒 todo 起步、type 恒空串（预留位，不校验）。
    """
    if not isinstance(steps_def, list) or not steps_def:
        return {"ok": False, "error": "steps 非法：必须是非空列表"}
    seen: set[str] = set()
    norm_steps: list[dict[str, Any]] = []
    for i, sd in enumerate(steps_def):
        if not isinstance(sd, dict):
            return {"ok": False, "error": f"第 {i + 1} 步非法：不是对象"}
        sid = sd.get("id")
        prompt = sd.get("prompt")
        if not isinstance(sid, str) or not sid.strip():
            return {"ok": False, "error": f"第 {i + 1} 步缺 id"}
        if sid in seen:
            return {"ok": False, "error": f"步 id 重复: {sid}"}
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": f"步 {sid} 缺非空 prompt"}
        # [迭代三 S3·步骤 4] type 值域校验（写入口①）：缺/非法 → 结构性拒绝，
        # 错误信息含步序号「第 N 步」。
        stype = sd.get("type")
        if stype not in VALID_STEP_TYPES:
            return {"ok": False, "error": (
                f"第 {i + 1} 步（{sid}）缺 type 或非法：type 必填且只能取 "
                f"{'/'.join(VALID_STEP_TYPES)}，实为 {stype!r}")}
        seen.add(sid)
        norm_steps.append({"id": sid, "prompt": prompt, "status": "todo",
                           "type": stype,
                           "has_artifact": bool(sd.get("has_artifact", False))})

    key = (work_id, card_id)
    lock = _get_lock(key)
    await _acquire(lock)
    try:
        existing = _boards.get(key)
        if existing is not None:
            return {"ok": False, "error": "board_exists", "board": existing.to_snapshot()}
        p = plan_path(work_id, card_id)
        if p.exists():  # 盘上有活板而内存无（重启后重建场景）→ 交恢复序，不覆盖
            try:
                restored = await get_board(work_id, card_id)
            except BoardRestoreError:
                restored = None
            if restored is not None:
                return {"ok": False, "error": "board_exists", "board": restored.to_snapshot()}
        data = {
            "card_id": card_id,
            "name": name if isinstance(name, str) and name.strip() else "",
            "steps": norm_steps,
            "current": 0,
            "revisions": [],
            "created_at": card_store._now_iso(),
        }
        board = PlanBoard(work_id, card_id, data)
        await board._save_locked()  # 建板同步落盘（一次模型调用的产物，丢了亏）
        _boards[key] = board
        session_ops.ops_log("plan_create", work_id, card_id, {"steps": len(norm_steps)})
        return {"ok": True, "board": board.to_snapshot()}
    finally:
        lock.release()


async def get_board(work_id: str, card_id: str) -> Optional[PlanBoard]:
    """取板（恢复序统一入口）：内存有→返回；内存无→读盘重建→返回；
    盘也无→None。

    **不做产物重放、不设恢复校验器**（任务书 S1 步骤 4）；JSON 解析失败
    → BoardRestoreError 明报（含路径），绝不静默、绝不自动从头。
    """
    key = (work_id, card_id)
    board = _boards.get(key)
    if board is not None:
        return board
    p = plan_path(work_id, card_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoardRestoreError(f"板文件断尾或不可读，不做产物重放，路径: {p}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise BoardRestoreError(f"板文件结构不可用，不做产物重放，路径: {p}")
    board = PlanBoard(work_id, card_id, data)
    _boards[key] = board
    return board


def has_live_board(work_id: str, card_id: str) -> bool:
    """活板存在？（内存命中或盘上板文件在——只看存在性，不解析。）"""
    return (work_id, card_id) in _boards or plan_path(work_id, card_id).exists()


def snapshot_sync(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """同步读板快照（S4 t165 装配热路径专用——build_context_regions 是同步
    函数，不能 await get_board）。

    内存命中→快照；未命中读盘（**只读，不注册进 _boards**——不劫持恢复序）；
    无板/断尾/结构坏 → None（装配热路径 fail-safe：区不出现，绝不抛）。
    """
    b = _boards.get((work_id, card_id))
    if b is not None:
        return b.to_snapshot()
    p = plan_path(work_id, card_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return None
    snap = copy.deepcopy(data)
    snap["total"] = len(snap["steps"])
    cur = snap.get("current")
    steps = snap["steps"]
    snap["current_prompt"] = (
        steps[cur].get("prompt")
        if isinstance(cur, int) and 0 <= cur < len(steps) else None
    )
    return snap


async def current_step_prompt(work_id: str, card_id: str) -> Optional[str]:
    """当前步要求（热路径：内存查优先）。

    无板/恢复失败 → None＋留痕，**不抛**——热路径在 runner 回合内，
    恢复失败不允许炸回合（S4 催化接缝依赖本语义）。
    """
    try:
        board = await get_board(work_id, card_id)
    except BoardRestoreError as exc:
        logger.warning("热路径取板失败 work=%s card=%s: %s", work_id, card_id, exc)
        return None
    if board is None:
        return None
    st = board.current_step()
    return st["prompt"] if st else None


async def finalize_board(work_id: str, card_id: str) -> dict[str, Any]:
    """收口终态：ops 留痕＋卡面写 thinkchain_final（write_final 同法）。

    不做清理（cleanup_board 由收口流程随后显式调）；卡面写失败如实
    返回 card_marked=False——孤儿扫描兜底（终态缺失的板由三查处置）。
    """
    session_ops.ops_log("plan_finalize", work_id, card_id, {})
    updates = {FM_THINKCHAIN_FINAL: {"at": card_store._now_iso()}}
    try:
        result = await card_store.update_card(
            work_id, card_id, updates, "思维链板收口终态",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("思维链终态写入异常 card=%s: %s", card_id, exc)
        return {"ok": False, "card_marked": False, "error": str(exc)}
    if isinstance(result, dict) and "error" in result:
        logger.warning("思维链终态写入被拒 card=%s: %s", card_id, result.get("error"))
        return {"ok": False, "card_marked": False, "error": result.get("error")}
    return {"ok": True, "card_marked": True}


# ---------------------------------------------------------------- 孤儿三查（同步，启动钩子用）

def sweep_orphans(work_id: str) -> dict[str, Any]:
    """单 work 孤儿扫描（任务书 S1 步骤 6）：扫 plans/ 全目录，逐 plan 文件
    对照卡面三查——**零时间戳**（只看卡面事实）：

      查一：卡文件存在且 front matter 含 thinkchain_final → 板已终结，删板文件；
      查二：卡文件不存在 → 卡已亡，删板文件；
      查三：卡活无终态 → 留（正常在途板）。

    删除范围限死 plans/ 下的 plan 文件（本迭代自产工件）。
    """
    pdir = _plans_dir(work_id)
    if not pdir.exists():
        return {"removed": [], "kept": []}
    removed: list[str] = []
    kept: list[str] = []
    for f in sorted(pdir.glob("plan_*.json")):
        card_id = f.stem[len("plan_"):]
        card_file = _work_root(work_id) / "cards" / f"{card_id}.md"
        if not card_file.exists():
            _unlink_quiet(f)   # 查二：卡无
            removed.append(card_id)
            continue
        try:
            meta, _ = parse_front_matter(card_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 读不动→宁可留（fail-open，不误删）
            kept.append(card_id)
            continue
        if isinstance(meta, dict) and FM_THINKCHAIN_FINAL in meta:
            _unlink_quiet(f)   # 查一：终态
            removed.append(card_id)
        else:
            kept.append(card_id)  # 查三：卡活无终态
    if removed:
        logger.info("孤儿板清理 work=%s removed=%s", work_id, removed)
    return {"removed": removed, "kept": kept}


def _unlink_quiet(f: Path) -> None:
    try:
        f.unlink()
    except OSError as exc:
        logger.warning("孤儿板文件删除失败 %s: %s", f, exc)


def sweep_all_orphans() -> dict[str, Any]:
    """全 works 孤儿三查（R2-③：main.py startup_init 内一行调用）。"""
    results: dict[str, dict[str, Any]] = {}
    total = 0
    works_root = session_store.WORKS_DIR
    if works_root.exists():
        for wd in sorted(works_root.iterdir()):
            if not wd.is_dir() or wd.name.startswith("_"):
                continue
            r = sweep_orphans(wd.name)
            if r["removed"] or r["kept"]:
                results[wd.name] = r
                total += len(r["removed"])
    return {"works": results, "removed_total": total}
