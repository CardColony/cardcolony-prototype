# -*- coding: utf-8 -*-
"""自动模式 · 执行闸与排队（回灌 5700 任务书 §块0.3 / 迭代方案 §3.8）。

**串行模式**：同一 work 内，同一时刻只有一个卡在跑；其余**排队等待**——这是与
今天行为的关键差别（今天的卡互斥体是「同卡在途 → 409 拒绝、不排队」，见
``session_facade.turn_try_acquire``；本闸是**每 work 一把**、拿不到就 await）。

**并行模式**：不启用（直接放行，＝今天的行为）。

**锁的持有期＝一张卡的整个目标生命周期**（起跑 → 目标终止）：
- 拿锁：``routes._run_message`` 起回合前 ``await acquire``（同卡已持有＝no-op）；
- 放锁：本卡**不再起轮**时（``_run_message`` 回合末按 ``auto_driver.will_continue``
  判定）／目标终止（``update_goal`` 三态）／手动停止（``control.stop_auto``
  会显式释放）。
⇒ 满足 U6/U8「一张子卡的全部任务都完成，再下一个子卡」。

★ **死锁代价**（用户已知并接受，迭代方案 §8）：某卡永久不终止（模型不声明完成）⇒
  队列后面全部饿死。这是「无闸 ＋ 串行」的固有代价；兜底＝手动停止（停止释放锁）。
★ 本模块**不设任何超时**——超时即是一种闸（拆闸铁律）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger("api.serial_gate")

MODE_SERIAL = "serial"
MODE_PARALLEL = "parallel"

#: 执行模式配置路径（模块级常量 ⇒ 测试可 monkeypatch 隔离，与 policy.PERMISSIONS_PATH
#: 同一套手法）。
EXEC_MODE_PATH = Path(__file__).resolve().parents[2] / "config" / "exec_mode.json"

#: 每 work 一把执行锁 ＋ 当前持有者（card_id）。锁对象懒建（首次 acquire 时建）。
_locks: dict[str, asyncio.Lock] = {}
_holder: dict[str, str] = {}


def exec_mode() -> str:
    """当前执行模式（``serial`` / ``parallel``）。

    TC2 裁决：默认 **serial**（先 serial 灰度，压测后转 parallel）——
    ``config/exec_mode.json`` 即默认配置，本函数只在文件缺失/损坏时 fail-open
    到 parallel（「默认必须等于现状」的另一半：配置坏了不能把系统卡死）。
    """
    try:
        data = json.loads(EXEC_MODE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MODE_PARALLEL
    mode = str((data or {}).get("mode") or "").strip().lower()
    return mode if mode in (MODE_SERIAL, MODE_PARALLEL) else MODE_PARALLEL


def is_serial() -> bool:
    return exec_mode() == MODE_SERIAL


def _lock_for(work_id: str) -> asyncio.Lock:
    lock = _locks.get(work_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[work_id] = lock
    return lock


def holder_of(work_id: str) -> str:
    """当前持有该 work 执行锁的卡（无人持有 ⇒ 空串）。诊断/断言用。"""
    return _holder.get(work_id, "")


async def acquire(work_id: str, card_id: str) -> bool:
    """取执行锁（串行模式）。返回**本次调用是否真的等到了锁**（True＝由本调用取得）。

    并行模式 ⇒ 直接返回 False（未启用，调用方无需放）。
    同卡已持有 ⇒ 直接返回 False（**no-op**，不是重入计数——「整个目标生命周期」
    的持有靠调用方**不释放**表达，见 ``routes._run_message`` 回合末的 keep 判定）。
    否则 ``await lock.acquire()``——**拿不到＝排队等待**（不是 409）。
    """
    if not is_serial():
        return False
    if _holder.get(work_id) == card_id:
        return False
    lock = _lock_for(work_id)
    await lock.acquire()
    _holder[work_id] = card_id
    logger.info("[serial_gate] 取得执行锁 work=%s card=%s", work_id, card_id)
    return True


def release(work_id: str, card_id: str) -> bool:
    """放执行锁（**只有当前持有者能放**；否则 no-op）。返回是否真的放了。"""
    if _holder.get(work_id) != card_id:
        return False
    _holder.pop(work_id, None)
    lock = _locks.get(work_id)
    if lock is not None and lock.locked():
        lock.release()
    logger.info("[serial_gate] 释放执行锁 work=%s card=%s", work_id, card_id)
    return True


def release_work(work_id: str) -> bool:
    """无条件释放该 work 的执行锁（**停止路径**用：锁可能挂在本卡或其后代上）。"""
    holder = _holder.get(work_id)
    if not holder:
        return False
    return release(work_id, holder)


def reset() -> None:
    """测试沙箱用：清空锁与持有者（用例之间事件循环不同，锁不可跨循环复用）。"""
    _locks.clear()
    _holder.clear()
