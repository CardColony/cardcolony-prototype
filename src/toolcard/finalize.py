# -*- coding: utf-8 -*-
"""T5 · 终态收口（EC-045 / EC-046 / EC-047）。

接 ``driver.done`` ＋ ``artifact_store.write_manifest``（EC-045），落两样东西：

1. manifest 文件（产物清单落盘）；
2. 卡面 ``toolcard_final: {verdict, at}``（模型那句终态声明原样落卡）。

外加一件收口时的旁挂：**把产物路径回传到 owner（主卡）影子卡的
「## 产物清单」节**（EC-086~EC-114，任务书 T8）。**终态时一次**，
不是每步一次——每步回传就得先判「值不值得回传」，那是判定，最高原则不许。

**收下终态声明、链结束（EC-047 逐字）**：

- **不查「走完没」**——不比对剧本步骤表、不看当前步是不是最后一步；
- **不查轮数**——第 3 轮收口与第 300 轮收口待遇完全相同；
- **不判 verdict 对不对**——不校验取值域、不看它像不像「完成」、
  不因为 verdict 是空的就拒收。

本函数**永不抛**：收口是链的最后一环，任何异常都记日志后照常返回，
不让「产物没落全」变成「链断在半路」。
"""
from __future__ import annotations

import logging
from typing import Any

from ..storage import card_store
from . import artifact_store, state_store

logger = logging.getLogger("toolcard.finalize")


def owner_of(work_id: str, card_id: str) -> str:
    """取工具卡的 owner（发起它的卡）id；读不到返回空串。**纯读，不判。**"""
    try:
        meta = card_store._meta_of(work_id, card_id) or {}
    except Exception:  # noqa: BLE001
        return ""
    return str(meta.get("owner_card_id", "") or "")


def describe(entries: list[dict[str, Any]]) -> list[str]:
    """把 manifest 条目转成「路径＋一句话说明」行（EC-087/EC-088 逐字边界）。

    该节边界：**「路径与用途指针（一行一个：路径＋一句话说明它是什么），
    只写指针不搬运内容正文」**。故此处每行**只带路径＋步号**，
    **不读文件、不搬正文、不摘首行**——用途说明由模型在卡面/对话里说，
    代码不替它编（编用途＝产出判断）。
    """
    return [
        f"- {e.get('文件', '')}：{e.get('stepId', '')} 步落库产物（指针）"
        for e in entries
    ]


async def finalize(
    work_id: str, card_id: str, verdict: Any, runtime: Any = None,
) -> dict[str, Any]:
    """终态收口主流程。**收下声明、落两样、链结束。永不抛。**

    顺序（每一步都是 IO 动作，不含任何「该不该」的判断）：
    ① 失败账终态重试一次 → ② manifest 落盘 → ③ 终态声明落卡面
    → ④ 产物路径回传 owner 影子卡「## 产物清单」节（**仅此一次**）。
    """
    result: dict[str, Any] = {
        "ok": True, "card_id": card_id, "verdict": verdict,
        "manifest_written": False, "final_written": False,
        "retried": {"retried": 0, "ok": 0}, "relayed": False,
    }
    try:
        # ① 终态重试一次（EC-044）：不试第三次、不阻断收口
        result["retried"] = artifact_store.retry_pending_once(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("终态重试异常 card=%s: %s", card_id, exc)

    try:
        # ② manifest 落盘（IO 事实）
        result["manifest_written"] = artifact_store.write_manifest(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("manifest 收口异常 card=%s: %s", card_id, exc)

    try:
        # ③ 终态声明落卡面：verdict 原样收下，不校验（EC-047）
        result["final_written"] = await state_store.write_final(
            work_id, card_id, verdict,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("终态声明落卡异常 card=%s: %s", card_id, exc)

    try:
        # ④ 回传 owner 影子卡「## 产物清单」节（终态一次）
        await relay_to_owner(work_id, card_id)
        result["relayed"] = True
    except Exception as exc:  # noqa: BLE001
        # 失败记失败账、不抛、不阻断链收口（EC-109）
        logger.warning("产物回传异常 card=%s: %s", card_id, exc)
        result["relayed"] = False
    return result


async def relay_to_owner(work_id: str, card_id: str) -> bool:
    """把本次产物路径回传 owner 影子卡的「## 产物清单」节。**终态一次。**

    记失败账、不抛、不阻断链收口（EC-109）。回传的是**路径指针**，
    不是内容正文（EC-087/088）；重复回传同一行由 ``append_to_section``
    的机械去重挡掉（EC-098）。
    """
    from ..storage import shadow_store  # 局部导入避免包初始化期的循环依赖

    owner = owner_of(work_id, card_id)
    if not owner:
        return False
    lines = describe(artifact_store.manifest_entries(card_id))
    if not lines:
        # 没有登记条目＝没有可回传的指针。这是一条 IO 事实（登记簿是空的），
        # 不是「这次没产出」或「产出不合格」的判断。
        return False
    ok = await shadow_store.append_to_section(
        work_id, owner, "## 产物清单", lines,
    )
    if not ok:
        logger.warning("产物回传未落盘 card=%s owner=%s", card_id, owner)
    return ok
