# -*- coding: utf-8 -*-
"""T2 · 步骤推进（EC-032 / EC-033 / EC-034 / EC-035）。

**本模块的七不（EC-035，最高原则的具体落地，逐条对应）**

1. **不判环节对错** —— ``advance(step=...)`` 的 step 参数是**模型自报**，
   代码照落。它说跳到 ``p4`` 就落 ``p4``，哪怕剧本顺序是 p1→p2→p3。
2. **不判产出空满** —— 本模块**根本不看产物**：不 stat、不读长度、不数条目。
3. **不判终态** —— ``done(verdict)`` 收下声明即结束，不问「真的走完了吗」。
4. **不查轮数** —— ``round`` **只计数不设限**（任务书 §六第 10 条），
   第 3 轮和第 300 轮待遇完全相同，不封顶、不告警、不减速。
5. **不回边** —— 不检查前后依赖，不阻止跳步、退步、循环。
6. **不拒绝** —— 除下列两种**结构性拒绝**外，一律照单执行。
7. **不判账空** —— 不看 manifest 里有没有条目、不看产物文件在不在。

**两种结构性拒绝**（这不是判定，是对「找不到」这一事实的回话，与
``card_store`` 的 ``CardNotFoundError`` 同性质）：

- **无运行时**：这张卡没有进行中的工具卡运行时（⇒ 调用方拿到的只能是事实）。
- **目标步不存在**：``advance`` 的 step_id 在剧本里查无此步。

⚠️ **只有这两种。** 不因「跳步了」「轮数多了」「没产物」「verdict 怪」
「产物难看看不懂」而拒绝——那些全是判断，判断归模型（EC-011/013/015）。

运行时形态（EC-033）：``CardRuntime{card_id, script_name, step_id, round,
artifacts}``，存**进程内内存 Map，键＝card_id**。
跨请求续跑靠 **state_store 的卡面字段重建**（见 ``start`` 的恢复分支）——
内存只是快路径，卡面才是真值源。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import artifact_store, finalize, script_loader, state_store

logger = logging.getLogger("toolcard.driver")


@dataclass
class CardRuntime:
    """一张工具卡的运行时（**只有位置与计数，没有评价**）。

    ``step_id`` 是**模型自报的当前步指针**（位置）；
    ``round`` 是第几次（**只计数不设限**）；
    ``artifacts`` 是代码自己落过的产物登记（IO 记录）。
    ——本 dataclass **刻意没有** ``done``／``passed``／``ok`` 之类字段：
    「这步算不算完成」不是代码能说的话（EC-038）。
    """

    card_id: str
    script_name: str
    step_id: str
    round: int = 1
    artifacts: list[dict[str, Any]] = field(default_factory=list)


# 进程内运行时表（键＝card_id，EC-033）
_runtimes: dict[str, CardRuntime] = {}


def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
    """结构性拒绝的统一返回（**只有两种理由会走到这里**）。"""
    return {"ok": False, "refused": reason, **extra}


def _view(rt: CardRuntime) -> dict[str, Any]:
    """运行时对外视图（原样字段，**不加工不评价**）。"""
    return {
        "ok": True,
        "card_id": rt.card_id,
        "script_name": rt.script_name,
        "step_id": rt.step_id,
        "round": rt.round,
        "artifacts": [e.get("文件") for e in rt.artifacts],
    }


def current(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """取当前运行时视图；无运行时 → None。**只读，不判。**"""
    rt = _runtimes.get(card_id)
    return _view(rt) if rt is not None else None


async def start(work_id: str, card_id: str, script_name: str = "") -> dict[str, Any]:
    """起一张工具卡的运行时。**幂等**（EC-034）。

    决策顺序（**恢复优先**）：

    1. 内存里已有运行时 → **原样返回**，不重置、不重头（幂等）；
    2. 内存没有、但**卡面** ``toolcard_script``/``toolcard_step`` 有值 →
       **从卡面重建运行时**（恢复：读盘动作，不是判定）。
       缺这一步，状态就成了「只被写、从不被读」，跨请求续跑无从谈起；
    3. 内存与卡面都没有、且给了 ``script_name`` → **新建**，落在剧本第一步；
    4. 都没有 → 结构性拒绝（无运行时）。

    **不判**：剧本该不该起、步骤够不够、这一步合不合适。
    剧本文件读不到 → 由 ``script_loader`` 抛错（解析器的事实，不是判定）。
    """
    rt = _runtimes.get(card_id)
    if rt is not None:
        return _view(rt)                                  # 1 · 幂等

    face = state_store.read_state(work_id, card_id)       # 2 · 读卡面（IO 动作）
    if face is not None and face.get("step_id"):
        rt = CardRuntime(
            card_id=card_id,
            script_name=face.get("script_name", "") or script_name,
            step_id=face["step_id"],
            round=int(face.get("round", 1) or 1),
        )
        _runtimes[card_id] = rt
        # 恢复不重写卡面（值本来就在）——写盘是 IO 动作，没必要多动一次
        return _view(rt)

    if not script_name:                                   # 4 · 无脚本名且无卡面
        return _refuse("无运行时", detail="卡面无 toolcard_step 且未给 script_name")

    script = script_loader.load_script(script_name)       # 读不到会抛（EC-031）
    if not script.steps:
        # 剧本 steps 为空 → 无「第一步」可落。这是「取不到位置」的结构性事实，
        # 不是「剧本写得不对」的判断。
        return _refuse("目标步不存在", detail=f"剧本无步骤: {script_name}")
    rt = CardRuntime(                                    # 3 · 新建
        card_id=card_id, script_name=script_name,
        step_id=script.steps[0].id, round=1,
    )
    _runtimes[card_id] = rt
    await state_store.write_state(
        work_id, card_id, script_name, rt.step_id, rt.round,
    )
    return _view(rt)


async def advance(work_id: str, card_id: str, step_id: str) -> dict[str, Any]:
    """把指针推进到 ``step_id``。**照单收下**（EC-034/EC-035）。

    ``step_id`` 是**模型自报**的：代码不核它对不对、不核它是不是下一步、
    不核能不能跳。**唯一**的检查是「剧本里查无此步」（结构性拒绝），
    因为那样连「照单收下」的落点都不存在。

    附带把 ``round`` 加一 —— **只计数不设限**：第 300 轮与第 3 轮同等对待。
    """
    rt = _runtimes.get(card_id)
    if rt is None:
        rt = await _restore_or_refuse(work_id, card_id, script_name="")
        if rt is None:
            return _refuse("无运行时", card_id=card_id)
    try:
        script = script_loader.load_script(rt.script_name)
    except script_loader.ScriptError:
        # 剧本文件读不到了：这是 IO 事实。让它抛（与 start 同口径），
        # 而不是把「剧本没了」判成「链条有问题」。
        raise
    if script.find_step(step_id) is None:
        return _refuse(                                   # 唯一的内容相关拒绝
            "目标步不存在", card_id=card_id, step_id=step_id,
            script=rt.script_name,
        )
    rt.step_id = step_id                                  # 照单落
    rt.round += 1                                         # 只计数不设限
    await state_store.write_state(
        work_id, card_id, rt.script_name, rt.step_id, rt.round,
    )
    return _view(rt)


async def _restore_or_refuse(
    work_id: str, card_id: str, script_name: str,
) -> Optional[CardRuntime]:
    """内存无运行时时试从卡面恢复一条；恢复不出返回 None（供调用方结构性拒绝）。"""
    rt = _runtimes.get(card_id)
    if rt is not None:
        return rt
    face = state_store.read_state(work_id, card_id)
    if face is None or not face.get("step_id"):
        return None
    rt = CardRuntime(
        card_id=card_id,
        script_name=face.get("script_name", "") or script_name,
        step_id=face["step_id"],
        round=int(face.get("round", 1) or 1),
    )
    _runtimes[card_id] = rt
    return rt


async def commit(
    work_id: str, card_id: str, seq: int, name: str, content: str, ext: str = "md",
) -> dict[str, Any]:
    """把本步产物落库并登记进运行时（**记录，不是闸门**）。

    落库失败**不拦推进、不抛**（EC-043）：失败进 ``_failures.json``，
    运行时照样前进。返回值里的 ``ok`` 只是「写盘成功没有」。
    """
    rt = _runtimes.get(card_id)
    step_id = rt.step_id if rt is not None else ""
    rec = artifact_store.commit_artifact(
        work_id, card_id, seq, step_id, name, content, ext,
    )
    if rt is not None:
        rt.artifacts.append({
            "序号": seq, "stepId": step_id, "文件": rec["filename"],
            "写入成功": rec["ok"],
        })
    return rec


async def done(work_id: str, card_id: str, verdict: Any) -> dict[str, Any]:
    """收下终态声明并收口。**照单执行**（EC-034/EC-047）。

    **不查「走完没」、不查轮数、不判 verdict 对不对、不校验取值域。**
    模型说完成就是完成——收下这句话、把它落进卡面、把产物清单落盘、链结束。

    无运行时 → 结构性拒绝（"收口"这件事需要有一条链可收）。
    """
    rt = _runtimes.get(card_id)
    if rt is None:
        rt = await _restore_or_refuse(work_id, card_id, script_name="")
        if rt is None:
            return _refuse("无运行时", card_id=card_id)
    summary = await finalize.finalize(work_id, card_id, verdict, rt)
    _runtimes.pop(card_id, None)      # 运行时清空（链结束）
    return summary


def on_step_boundary(state: Any) -> Optional[str]:
    """step 边界的驱动挂点（**EC-032：挂在 ``message_runner.check_control``**）。

    同步、纯内存、零 IO、零判断：把当前卡的运行时步指针读出来返回，
    没有运行时就返回 None。**不推进、不评价、不拒绝**——它只是让 step 边界
    有一个能看见工具卡进度的地方。
    """
    card_id = getattr(state, "card_id", "") or ""
    if not card_id:
        return None
    rt = _runtimes.get(card_id)
    return rt.step_id if rt is not None else None


def drop(card_id: str) -> None:
    """丢掉某卡的进程内运行时（供测试隔离；生产不调）。"""
    _runtimes.pop(card_id, None)
