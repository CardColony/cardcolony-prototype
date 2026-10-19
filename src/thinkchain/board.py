# -*- coding: utf-8 -*-
"""思维链板子基建（迭代二 S1 原型；[思维链真实落地 2026-10-11] 五态重写）。

职责单一：板上数据结构与三层落盘。不含工具语义（plan_tools）、不含提交
轮三席统合（message_runner.run_seats）、不含 runner 接缝；`src/api/` 路由
层零引用本件。

板上数据（plans/plan_{card_id}.json）::

    {"card_id", "name",
     "steps": [{"id", "prompt", "criteria", "deliverable", "type",
                "status", "result"}],
     "current", "revisions": [...], "created_at"}

[思维链真实落地 2026-10-11 用户裁定 C2/笔 2]：
  * 步结构重写为 {id,prompt,criteria,deliverable,type,status,result}——
    每步必带完成判据（criteria，可观测产出物事实）与产出物声明
    （deliverable）；步完成落 result（模型在 step_complete 交来的成果
    原文）。
  * status 值域五态 ``VALID_STEP_STATUSES``：pending（待做）／doing
    （进行中）／done（完成）／blocked（受阻）／dropped（弃）。建板起步
    pending、step_complete 置 done；doing/blocked/dropped 经 plan_modify
    由模型自主标注（模型状态自述，代码不判内容）。
  * 步产物位字段与产物落库机制整体退役——步产出物以 deliverable 声明＋
    result 成果为准；真文件产物由模型经 write_file 工具落盘（file_tools
    通道），板上不再记产物位。
  * 步执行提示词模板 ``STEP_BRIEF_TEMPLATE``（任务书附录 C 全文）单点
    定义在本件：message_runner（催化挂点）、plan_tools（步引导）、
    step_region（165 区）三处消费，禁各自复制防漂移。

type 值域＝``VALID_STEP_TYPES``（execute/think；校验只出现在写入口）。
revisions 由改板动作自动记（动作/时间/改前快照/改后快照）；dirty 是内存
调度标志不落盘。

三层落盘：建板同步写；**步进指针同步写**（step_complete 处理器内、返回
工具结果之前完成 tmp→replace——救命信息不等异步）；修订异步（create_task
＋置脏合并——「异步」指调度时机，写本身仍原子）。

落盘路径：`works/{work_id}/plans/plan_{card_id}.json`，tmp→os.replace
原子替换（memory_store.py:179-182 同法）。

内存键（R2-① 用户裁定）：``(work_id, card_id)`` 双键——card_id 在 work
内自增、非全局唯一。语义仍是「绑卡不绑会话」。

锁：per-card 轻锁，不碰回合互斥。锁内同步写（_atomic_write_json 无
await 让出点、持锁微秒级），串行化全部写盘。

恢复序：内存有→返回；内存无→读盘重建→返回；盘也无→None。**不做产物
重放、不设恢复校验器**；JSON 解析失败明报 BoardRestoreError（含路径），
绝不静默、绝不自动从头。``restore(snapshot)``＝从快照 dict 显式重建运行
时并注册（强杀/重启后从盘上快照续链的统一入口，get_board 盘路径内部
复用本件）。

ops 与 revision 消费：建/推/改/删/收口动作直接 ``session_ops.ops_log``
（fail-open）；改板动作另调 revision_store.record_revision＋板上
revisions[] 追加。

卡面终态标记：front matter 键 ``thinkchain_final``（孤儿扫描判定源，
零时间戳）。

路径纪律：一切路径动态取 ``session_store.WORKS_DIR``——conftest 沙箱
monkeypatch 模块级常量后本件自动跟随，绝不缓存 Path 于模块级。

清理：链终态→删 plan 文件＋plans/ 同目录 .tmp 残留＋内存条目；删除
fail-open。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from ..storage import card_store, revision_store, session_ops, session_store
from ..storage.hash_utils import parse_front_matter

logger = logging.getLogger("thinkchain.board")

# 卡面终态键（命名法同 state_store.FM_FINAL="toolcard_final"）
FM_THINKCHAIN_FINAL = "thinkchain_final"

# [迭代三 S3] 步 type 值域权威＝代码常量。校验只出现在写入口：create_board
# 与 apply_revision·insert；读路径零校验。
# [规划驱动 2026-10-19 · permexp] 加 delegate：一步可以是「交给一个新节点做」。
# 原值域只有 execute/think ⇒ 规划只能拆成「我自己要做的动作」，长不出组织。
VALID_STEP_TYPES: list[str] = ["execute", "think", "delegate"]

# [思维链真实落地 2026-10-11] 步 status 值域五态（笔 2）：pending（待做，
# 建板起步）／doing（进行中）／done（完成，step_complete 置）／blocked
# （受阻）／dropped（弃）。doing/blocked/dropped 由模型经 plan_modify 自主
# 标注；推进与指针判据只用 done 与 current，其余三态是模型状态自述的载体。
VALID_STEP_STATUSES: list[str] = ["pending", "doing", "done", "blocked", "dropped"]

# [思维链真实落地 2026-10-11] 步执行提示词模板（任务书附录 C 全文，一字不改；
# 拼上该步字段——prompt/criteria/deliverable）。单点定义：message_runner
# （催化挂点）、plan_tools（步引导）、step_region（165 区）三处消费。
STEP_BRIEF_TEMPLATE = """\
【推进一步】你在推进计划的一步。这一步可以由你自己做，也可以交给一个新节点做——按这一步的类型来。

本步要求：{prompt}
完成判据：{criteria}（满足它，这一步才算完成）
产出物：{deliverable}

推进纪律：
1. 先判断这一步该谁做：一件事需要独立推进、且能独立验收，就建节点派出去；只有你自己才能做的（拆解、整合、判断、收口），才自己做。
2. 派出去时，把「要什么结果、什么算完成」写进对方的 goal，不写怎么做、不写用什么技术。
3. 自己做时，需要取证就调工具，不凭想象。
4. 完成判据不满足，就说"没完成"，不许谎报"已完成"。
5. 做完立刻调 step_complete：result 写本步成果，criteria_met 写"判据如何满足"。"""


DELEGATE_BRIEF_TEMPLATE = """\
【推进一步 · 派活】这一步交给一个新节点做，不是你自己做。

要它达成什么：{prompt}
它交付什么算完成：{criteria}
交给谁：{deliverable}

怎么做：
1. 建一张子卡（create_card），把上面「要它达成什么 + 它交付什么算完成」写进它的 goal。
   goal 里**只写要什么结果**——不写怎么做、不写用什么技术、不写放哪个目录，那些是它的决定。
2. 建完直接结束本回合，**不要自己动手做这件事**。系统会在子卡完工后叫醒你。
3. 被叫醒后：read_card 读它的现状，核对上面那条判据是否真的满足；
   满足了就调 step_complete（result 写它交付了什么，criteria_met 写判据如何满足），
   没满足就把差什么写清楚、让它继续做或重派。"""


def step_brief(step: dict[str, Any]) -> str:
    """按步类型拼提示词（纯函数；缺字段容错——如实标注，不编造）。

    [规划驱动 2026-10-19 · permexp] 按 type 分流：delegate 步走派活模板
    （建子卡 → 结束回合 → 被唤醒后核对判据），其余走通用推进模板。
    旧行为（无 type 或 execute/think）逐字不变。
    """
    tpl = DELEGATE_BRIEF_TEMPLATE if str(step.get("type") or "") == "delegate" else STEP_BRIEF_TEMPLATE
    return tpl.format(
        prompt=step.get("prompt", ""),
        criteria=step.get("criteria") or "（本步未定判据——以本步要求达成与否为准）",
        deliverable=step.get("deliverable") or "（本步未定产出物——以 result 记录成果）",
    )


# [当前轮注入消息事件 2026-10-14] 把附录 C 模板拆成「固定总领」与「动态行模板」
# 两半——**单点来源仍是 STEP_BRIEF_TEMPLATE**，不新增第二份文本：
#   * 含占位变量（{prompt}/{criteria}/{deliverable}）的行 = 随步变化的动态行；
#   * 其余行 = 不随步骤数据变化的固定执行纪律（进 context_preamble）。
# 拆分是纯机械的行过滤，不做任何语义判断。
_BRIEF_PLACEHOLDERS = ("{prompt}", "{criteria}", "{deliverable}")

# 拆分结果缓存（模块常量不变 ⇒ 一次算定）
_BRIEF_PARTS_CACHE: dict[str, tuple[str, str]] = {}


def _brief_parts() -> tuple[str, str]:
    """(固定部分, 动态行模板)；结果只依赖模块常量，缓存一次。"""
    cached = _BRIEF_PARTS_CACHE.get("v")
    if cached is not None:
        return cached
    fixed_lines: list[str] = []
    dynamic_lines: list[str] = []
    for ln in STEP_BRIEF_TEMPLATE.split("\n"):
        if any(p in ln for p in _BRIEF_PLACEHOLDERS):
            dynamic_lines.append(ln)
        else:
            fixed_lines.append(ln)
    fixed = "\n".join(fixed_lines).strip("\n")
    fixed = re.sub(r"\n{3,}", "\n\n", fixed)   # 抽走动态行留下的连续空行归一
    parts = (fixed, "\n".join(dynamic_lines))
    _BRIEF_PARTS_CACHE["v"] = parts
    return parts


def step_brief_fixed() -> str:
    """附录 C 模板的固定部分（不含任何占位变量）——context_preamble 正文来源。"""
    return _brief_parts()[0]


def step_brief_dynamic(step: dict[str, Any]) -> str:
    """动态部分（本步要求／完成判据／产出物），按步字段填充。

    [规划驱动 2026-10-19 · permexp] delegate 步返回**完整派活模板**：它的执行动作
    （建子卡 → 结束回合 → 被唤醒后核对判据）与通用推进模板不同，不能只给三行字段。
    """
    if str(step.get("type") or "") == "delegate":
        return DELEGATE_BRIEF_TEMPLATE.format(
            prompt=step.get("prompt", ""),
            criteria=step.get("criteria") or "（本步未定判据——以本步要求达成与否为准）",
            deliverable=step.get("deliverable") or "（本步未定产出物——以 result 记录成果）",
        )
    return _brief_parts()[1].format(
        prompt=step.get("prompt", ""),
        criteria=step.get("criteria") or "（本步未定判据——以本步要求达成与否为准）",
        deliverable=step.get("deliverable") or "（本步未定产出物——以 result 记录成果）",
    )


LOCK_TIMEOUT = 1.0  # 与 card_store.LOCK_TIMEOUT 同值：锁内是内存操作＋小文件原子写

_PLANS_DIR = "plans"            # works/{w}/plans/ —— 板文件目录

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
    """板文件：works/{w}/plans/plan_{card_id}.json。"""
    return _plans_dir(work_id) / f"plan_{card_id}.json"


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
        """原子落盘。**须持锁调用**——串行化写盘是「不撕裂」的根。"""
        _atomic_write_json(plan_path(self._work_id, self._card_id), self._data)

    def _schedule_flush(self) -> None:
        """修订置脏合并落盘（create_task＋置脏合并；锁内同步写，串行化
        保证不撕裂）。"""
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
        """当前步标 done＋落 result＋指针前移，同锁原子＋指针同步落盘（防护③）。

        result_summary＝模型 step_complete 交来的本步成果原文，落步级
        result 字段（跨回合不丢步成果——提交轮三席质询的材料）。
        幂等口径：跨请求重复交无法从板面识别，防护以同锁重入为准——进锁后
        发现当前步已是 done（指针/状态不一致的异常态），不推进、不撕裂，
        如实返回当前步要求。
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
            st["result"] = result_summary  # 步成果落板（跨回合可恢复、提交轮可查）
            self._data["current"] = cur + 1
            await self._save_locked()  # 指针同步落盘（锁内原子，返回工具结果之前完成）
            nxt = steps[cur + 1] if cur + 1 < len(steps) else None
            session_ops.ops_log("plan_advance", self._work_id, self._card_id, {
                "step": st["id"],
                "next": nxt["id"] if nxt else None,
                "all_done": nxt is None,
            })
            out = {"ok": True, "advanced": True, "step": st["id"],
                   "type": st.get("type", ""),   # 步类型随返回（扩值拼 behavior 用）
                   "prompt": nxt["prompt"] if nxt else None,
                   "all_steps_done": nxt is None}
            if nxt is not None:
                # 下一步全字段随返回（plan_tools 按附录 C 模板拼引导）
                out["next_step"] = copy.deepcopy(nxt)
            return out
        finally:
            lock.release()

    async def apply_revision(self, change: str, detail: dict[str, Any]) -> dict[str, Any]:
        """改板四动作（reorder/insert/remove/modify）——纯数组序/成员变更，
        结构性拒绝零内容判断。

        [思维链真实落地 2026-10-11] 新字段适配：insert 步必带 criteria/
        deliverable（与 create_plan 同契约）；modify 除 prompt 外可选改
        criteria/deliverable/status（status 限五态值域——doing/blocked/
        dropped 由模型自主标注的写入口）。

        每次改板：板上 revisions[] 追加＋revision_store.record_revision
        ＋ops plan_revise；板文件走置脏异步合并落盘（修订不是救命信息，
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
                # type 值域校验（写入口②）：与 create_board 同值域同拒绝形态。
                ntype = new_step.get("type")
                if ntype not in VALID_STEP_TYPES:
                    return {"ok": False, "error": (
                        f"insert 步（{new_id}）type 缺或非法：type 必填且只能取 "
                        f"{'/'.join(VALID_STEP_TYPES)}，实为 {ntype!r}")}
                # criteria/deliverable 非空校验（写入口②）：与 create_board 同契约
                ncrit = new_step.get("criteria")
                ndeli = new_step.get("deliverable")
                if not isinstance(ncrit, str) or not ncrit.strip():
                    return {"ok": False, "error": f"insert 步（{new_id}）缺非空 criteria"}
                if not isinstance(ndeli, str) or not ndeli.strip():
                    return {"ok": False, "error": f"insert 步（{new_id}）缺非空 deliverable"}
                steps.insert(idx, {"id": new_id, "prompt": prompt,
                                   "criteria": ncrit, "deliverable": ndeli,
                                   "type": ntype, "status": "pending", "result": ""})
            elif change == "remove":
                rid = detail.get("id")
                target = next((s for s in steps if s["id"] == rid), None)
                if target is None:
                    return {"ok": False, "error": f"步不存在: {rid}"}
                if old_id is not None and rid == old_id:
                    # 当前步不可删——删了指针没处指，结构性拒绝
                    return {"ok": False, "error": "当前步不可删"}
                if len(steps) <= 1:
                    return {"ok": False, "error": "至少保留一步"}
                steps = [s for s in steps if s["id"] != rid]
            elif change == "modify":
                rid = detail.get("id")
                target = next((s for s in steps if s["id"] == rid), None)
                if target is None:
                    return {"ok": False, "error": f"步不存在: {rid}"}
                prompt = detail.get("prompt")
                criteria = detail.get("criteria")
                deliverable = detail.get("deliverable")
                status = detail.get("status")
                if prompt is not None:
                    if not isinstance(prompt, str) or not prompt.strip():
                        return {"ok": False, "error": "modify 的 prompt 须为非空字符串"}
                    target["prompt"] = prompt
                if criteria is not None:
                    if not isinstance(criteria, str) or not criteria.strip():
                        return {"ok": False, "error": "modify 的 criteria 须为非空字符串"}
                    target["criteria"] = criteria
                if deliverable is not None:
                    if not isinstance(deliverable, str) or not deliverable.strip():
                        return {"ok": False, "error": "modify 的 deliverable 须为非空字符串"}
                    target["deliverable"] = deliverable
                if status is not None:
                    if status not in VALID_STEP_STATUSES:
                        return {"ok": False, "error": (
                            f"modify 的 status 只能取 {'/'.join(VALID_STEP_STATUSES)}，"
                            f"实为 {status!r}")}
                    target["status"] = status
                if prompt is None and criteria is None and deliverable is None and status is None:
                    return {"ok": False, "error": "modify 至少要给 prompt/criteria/deliverable/status 其一"}
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

    async def cleanup_board(self) -> dict[str, Any]:
        """终态清理：删 plan 文件＋plans/ 同目录 .tmp 残留＋内存条目。
        fail-open（永不抛；失败留运维日志，下次启动扫描兜住）。"""
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

    steps_def＝模型给的步定义 [{"id","prompt","criteria","deliverable",
    "type"}]——校验仅结构性（steps 非空、id 唯一、prompt/criteria/
    deliverable 非空、type 在值域），非法拒绝并给拒绝理由；status 恒
    pending 起步、result 恒空串。criteria/deliverable 非空是「完成判据
    上板」的结构性保障（C2：判据不满足不许谎报已完成）。
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
        criteria = sd.get("criteria")
        deliverable = sd.get("deliverable")
        if not isinstance(sid, str) or not sid.strip():
            return {"ok": False, "error": f"第 {i + 1} 步缺 id"}
        if sid in seen:
            return {"ok": False, "error": f"步 id 重复: {sid}"}
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": f"步 {sid} 缺非空 prompt"}
        if not isinstance(criteria, str) or not criteria.strip():
            return {"ok": False, "error": (
                f"第 {i + 1} 步（{sid}）缺非空 criteria：完成判据必填，"
                "必须落在可观测的产出物上（文件存在/内容含 X/命令输出 Y）")}
        if not isinstance(deliverable, str) or not deliverable.strip():
            return {"ok": False, "error": (
                f"第 {i + 1} 步（{sid}）缺非空 deliverable：产出物声明必填"
                "（落盘路径或明确数据/结论名）")}
        stype = sd.get("type")
        if stype not in VALID_STEP_TYPES:
            return {"ok": False, "error": (
                f"第 {i + 1} 步（{sid}）缺 type 或非法：type 必填且只能取 "
                f"{'/'.join(VALID_STEP_TYPES)}，实为 {stype!r}")}
        seen.add(sid)
        norm_steps.append({"id": sid, "prompt": prompt,
                           "criteria": criteria, "deliverable": deliverable,
                           "type": stype, "status": "pending", "result": ""})

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


def restore(snapshot: dict[str, Any], work_id: Optional[str] = None,
            card_id: Optional[str] = None) -> PlanBoard:
    """[思维链真实落地 2026-10-11 笔 2] 从快照 dict 显式重建运行时并注册。

    强杀/重启后从盘上快照续链的统一入口：snapshot 结构同板文件（card_id/
    name/steps/current/revisions/created_at）；work_id/card_id 缺省从
    snapshot 读。结构非法（非 dict / steps 非列表）→ BoardRestoreError
    明报——绝不静默、绝不自动从头。
    """
    if not isinstance(snapshot, dict):
        raise BoardRestoreError("restore 快照非法：不是对象")
    wid = work_id or snapshot.get("work_id")
    cid = card_id or snapshot.get("card_id")
    if not wid or not cid:
        raise BoardRestoreError("restore 快照缺 work_id/card_id，无法定位")
    if not isinstance(snapshot.get("steps"), list):
        raise BoardRestoreError(f"restore 快照结构不可用（steps 非列表）: {wid}/{cid}")
    board = PlanBoard(wid, cid, snapshot)
    _boards[(wid, cid)] = board
    return board


async def get_board(work_id: str, card_id: str) -> Optional[PlanBoard]:
    """取板（恢复序统一入口）：内存有→返回；内存无→读盘重建→返回；
    盘也无→None。

    **不做产物重放、不设恢复校验器**；JSON 解析失败→BoardRestoreError
    明报（含路径），绝不静默、绝不自动从头。盘上重建走 restore()——
    与强杀演练同一恢复路径（单一事实源）。
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
    data.setdefault("work_id", work_id)  # restore 定位用（板文件本身不落此键）
    board = restore(data, work_id=work_id, card_id=card_id)
    return board


def has_live_board(work_id: str, card_id: str) -> bool:
    """活板存在？（内存命中或盘上板文件在——只看存在性，不解析。）"""
    return (work_id, card_id) in _boards or plan_path(work_id, card_id).exists()


def snapshot_sync(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """同步读板快照（装配热路径专用——build_context_regions 是同步函数，
    不能 await get_board）。

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
    恢复失败不允许炸回合。
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
    """单 work 孤儿扫描：扫 plans/ 全目录，逐 plan 文件对照卡面三查——
    **零时间戳**（只看卡面事实）：

      查一：卡文件存在且 front matter 含 thinkchain_final → 板已终结，删板文件；
      查二：卡文件不存在 → 卡已亡，删板文件；
      查三：卡活无终态 → 留（正常在途板）。

    删除范围限死 plans/ 下的 plan 文件（本迭代自产工件）。
    """
    pdir = _plans_dir(work_id)
    if not pdir.exists():
        return {"removed": [], "kept": []}
    from ..storage import card_db  # 局部 import：避免包初始化期循环

    removed: list[str] = []
    kept: list[str] = []
    for f in sorted(pdir.glob("plan_*.json")):
        card_id = f.stem[len("plan_"):]
        try:
            def _fn(conn: Any) -> Optional[dict[str, Any]]:
                row = card_db.get_card_row(conn, card_id)
                return None if row is None else dict(row)

            row = card_db.read_sync(work_id, _fn, None)
        except Exception:  # noqa: BLE001 读不动→宁可留（fail-open，不误删）
            kept.append(card_id)
            continue
        if row is None:
            _unlink_quiet(f)   # 查二：卡无
            removed.append(card_id)
            continue
        if row.get(FM_THINKCHAIN_FINAL) is not None:
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
