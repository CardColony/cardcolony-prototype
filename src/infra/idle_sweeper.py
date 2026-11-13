# -*- coding: utf-8 -*-
"""静默清扫器（2026-11-12 · 用户令「执行卡静默12h自动归档」）。

harness 分工照旧：本模块只产**机械事实**——按周期把本进程属主的卡
扫一遍，算出每张卡「静默多久了」（现在 − 最后一条事实 ts；一条事实
都没有就用建卡时间）。静默过 ``_EMIT_FLOOR`` 的卡落一条 ``card_idle``
事实（silent_sec/card_type/card_state）——**多久算久、哪种卡该归档、
归不归，全在 harness_policies.json**，这里一字不判。

- 节流：每卡每 ``_EMIT_GAP`` 至多一条（过线卡一次静默期 ~1 条/小时，
  不灌事件流——策略点火撕本子管不到发射侧，额度只能发射侧自己记）。
- 分片：只扫本 runner 属主的卡（单卡单写者）；all/串行全归 0 号；
  web/condense 由 install 调用方闸，不进来。
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("infra.idle_sweeper")

#: 清扫周期（秒）：6 小时一扫（2026-11-12 用户令）——12h 级阈值配
#: 分钟级扫描纯浪费；代价是归档落在静默 12h～18h 之间的窗口，可接受。
_SWEEP_SEC = 6 * 3600.0

#: 事实地板线（秒）：静默过这条才落 card_idle。刻意压在 12h 下方一小截
#: ——「几小时算久」留给策略判；将来要更早的静默事实，调小这一行。
_EMIT_FLOOR = 11 * 3600.0

#: 每卡重发间隔（秒）：过线卡一次静默期至多每小时一条。
_EMIT_GAP = 3600.0

#: (work, card) → 上次落 card_idle 的 epoch。内存账：重启清零只是
#: 过线卡重报一次，不丢语义（事件里 silent_sec 照样是真的）。
_last_emit: dict[tuple[str, str], float] = {}

_TASK: "asyncio.Task | None" = None


def _meta_ts(meta: dict) -> float:
    """meta.created_at → epoch（ISO 串或数都吃）；没有 ⇒ 0。"""
    from . import fact_store
    v = (meta or {}).get("created_at")
    if isinstance(v, (int, float)) and v:
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(fact_store.parse_ts(v) or 0)
        except Exception:  # noqa: BLE001 解析不动按 0
            return 0.0
    return 0.0


#: 不计"卡活动"的事实类型——自检类（card_idle 本身）与系统送达类
#: （harness_message＝策略投递/回执）。**card_idle 必须排除**：它落卡
#: 自身事件流，不排的话发射即重置静默度，策略的 12h 阈值永远等不到。
_NOISE_TYPES = frozenset({"card_idle", "harness_message"})

#: 往前看多少条找"最后一条非噪声事实"。20 条全是噪声 ⇒ 放弃本卡本轮
#: （被催爆的卡罕见；放弃＝不归档，是安全方向）。
_NOISE_SCAN = 20


def _silent_sec(work_id: str, card_id: str, meta: dict, now: float) -> float:
    """静默秒数；算不出（无事实且无 created_at）⇒ -1（不冒充已知）。

    口径：now − **最后一条非噪声事实**的 ts（噪声＝自检/系统送达，不是
    卡的活动）；一条非噪声事实都没有就用 created_at。
    """
    last_ts = 0.0
    try:
        from . import fact_store
        evs = fact_store.store().query(
            work_id, card=card_id, limit=_NOISE_SCAN, newest_first=True)
        for e in (evs or []):          # newest_first ⇒ 降序：首个即最新
            if str(e.get("type") or "") in _NOISE_TYPES:
                continue
            t = e.get("_t")            # epoch 在 _t（"ts" 键是 ISO 串）
            if isinstance(t, (int, float)) and t > 0:
                last_ts = float(t)
            break
    except Exception:  # noqa: BLE001 查不到按无事实回落
        pass
    if last_ts <= 0:
        last_ts = _meta_ts(meta)
    return (now - last_ts) if last_ts > 0 else -1.0


async def _sweep_once() -> int:
    """扫一遍：本进程属主、活着、静默过线、距上次发射超 _EMIT_GAP ⇒ 落事实。"""
    from . import procs
    from ..api import event_stream
    from ..storage import card_store, work_store

    try:
        works = work_store.list_works() or []
    except Exception as exc:  # noqa: BLE001 列不出本轮空扫
        logger.warning("[idle_sweeper] 列 work 失败: %s", exc)
        return 0
    my = procs.runner_index()
    now = time.time()
    n = 0
    for w in works:
        wid = str((w or {}).get("work_id") or (w or {}).get("id") or "")
        if not wid:
            continue
        try:
            metas = card_store.iter_card_metas(wid) or {}
        except Exception as exc:  # noqa: BLE001 一个 work 读不动不拖全扫
            logger.warning("[idle_sweeper] 读卡表失败 %s: %s", wid, exc)
            continue
        for cid, m in metas.items():
            state = str((m or {}).get("state") or "")
            if state in ("archived", "deleted"):
                continue  # 死卡没有"静默"可言
            try:
                if procs.owner_index_of(wid, cid) != my:
                    continue  # 不归本片 ⇒ 它的属主 runner 扫
            except Exception:  # noqa: BLE001 分片算不出按不归本片（宁可漏发）
                continue
            if now - _last_emit.get((wid, cid), 0.0) < _EMIT_GAP:
                continue  # 本小时已报过
            try:
                sec = _silent_sec(wid, cid, m, now)
            except Exception as exc:  # noqa: BLE001 一张算不动不拖全扫
                logger.debug("[idle_sweeper] 静默度算不出 %s/%s: %s",
                             wid, cid, exc)
                continue
            if sec < _EMIT_FLOOR:
                continue
            try:
                event_stream.emit(wid, cid, "card_idle", {
                    "silent_sec": round(sec, 1),
                    "card_type": str((m or {}).get("card_type") or ""),
                    "card_state": state,
                })
            except Exception as exc:  # noqa: BLE001 落不出不记额度，下轮重试
                logger.warning("[idle_sweeper] emit 失败 %s/%s: %s",
                               wid, cid, exc)
                continue
            _last_emit[(wid, cid)] = now
            n += 1
    return n


async def _loop() -> None:
    while True:
        try:
            n = await _sweep_once()
            if n:
                logger.info("[idle_sweeper] 落 card_idle ×%d", n)
        except Exception as exc:  # noqa: BLE001 一轮失败不杀死循环
            logger.warning("[idle_sweeper] 清扫失败: %s", exc)
        await asyncio.sleep(_SWEEP_SEC)


def install() -> None:
    """装静默清扫器（幂等）。runner 侧（all/runner）才装——
    web 没有跑卡域、condense 只管凝练，都不该碰卡事件流。"""
    global _TASK
    if _TASK is not None:
        return
    try:
        from . import procs
        if not procs.runner_side():
            return
    except Exception:  # noqa: BLE001 角色判不出按不装（宁可不扫）
        return
    try:
        _TASK = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        logger.warning("[idle_sweeper] 无事件循环，装不了")


def _reset_for_test() -> None:
    """测试用：清发射额度账。"""
    _last_emit.clear()
