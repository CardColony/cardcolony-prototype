# -*- coding: utf-8 -*-
"""T3 · 状态持久化（EC-036 / EC-037 / EC-038 / EC-039 / EC-040）。

卡面三字段（EC-037）：

======================  ==========================================================
``toolcard_script``     哪条剧本（剧本名）
``toolcard_step``       **当前步指针**（位置，不是评价）
``toolcard_round``      第几次（**只计数不设限**）
======================  ==========================================================

影子卡 ``## 进度`` 节是**展示用第二载体**，不是真值源（真值源＝卡面三字段）。

**字段命名纪律（EC-038）**：**不得命名为 ``step_status: done``**。
「done」这个字面本身就是判定——它宣称了「这步算完成」，而这一步只能由模型说。
本模块只有**位置**，没有**评价**：``toolcard_step`` 存的是「当前在哪个 step id」，
由模型自报、代码照落。

挂点（EC-036）：``card_store.update_card`` 是唯一写路径，它经
``card_store._write_gate``（L497 定义、L960 update 调用位）——本线的状态写入
走同一条门，不另开写路径。

**fail-open（EC-039）**：读异常 → 返 ``None`` 并 warn；写异常 → 返 ``False``
并 warn；**一律不抛**。状态读不出来不是「链断了」的判据，只是一条 IO 事实。

⚠️ ``write_state`` 的返回值是 **IO 事实**（写成功没有），**不是判定结果**
（内容对不对、状态合不合理）——**已在下方 ``write_state`` 函数注释写死**，
防后人误用（EC-040）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..storage import card_store

logger = logging.getLogger("toolcard.state_store")

# 卡面三字段名（唯一来源；driver 与恢复路径都从这里取，不各自写字面量）
FM_SCRIPT = "toolcard_script"
FM_STEP = "toolcard_step"
FM_ROUND = "toolcard_round"
FM_FINAL = "toolcard_final"

# 历史记录用的中性措辞——**不写「完成/通过/done」**（那是对内容的评价）。
# 这里只记「谁在什么时候把指针写到哪」，是纯粹的 IO 留痕。
_HISTORY_NOTE = "工具卡状态指针写入：script={script} step={step} round={rnd}"


async def write_state(
    work_id: str, card_id: str, script_name: str, step_id: str, round_no: int,
) -> bool:
    """把三字段写进卡面 front matter。**返回 True/False＝写盘成功没有**。

    ============================  ==================================================
    **这个返回值是 IO 事实，不是判定结果**（EC-040）
    ============================  ==================================================
    ``True`` ＝ 三行字段落进了磁盘文件。**它不代表**：这一步做得对不对、
    产物写没写、状态合不合理、这步算不算完成——那些代码一律不产出。
    调用方**不得**由 ``True`` 推出任何关于质量的结论；要说「这步成了」，
    只能由模型自己说。
    ============================  ==================================================

    fail-open：任何异常 → 返 ``False`` 并 warn，**不抛**（EC-039）。
    """
    updates = {
        FM_SCRIPT: script_name,
        FM_STEP: step_id,
        FM_ROUND: int(round_no),
    }
    note = _HISTORY_NOTE.format(script=script_name, step=step_id, rnd=round_no)
    try:
        result = await card_store.update_card(work_id, card_id, updates, note)
    except Exception as exc:  # noqa: BLE001
        # fail-open：写不进去是一条 IO 事实，不是链断的判据
        logger.warning("工具卡状态写入异常 card=%s: %s", card_id, exc)
        return False
    if isinstance(result, dict) and "error" in result:
        # 写门拒绝（如卡不存在）——仍是 IO 事实，如实返 False，不抛、不改写文案
        logger.warning("工具卡状态写入被拒 card=%s: %s", card_id, result.get("error"))
        return False
    return True


async def write_final(work_id: str, card_id: str, verdict: Any) -> bool:
    """落终态声明 ``toolcard_final: {verdict, at}``。**返回写盘成功没有**。

    ``verdict`` 是**模型自报的终态声明，代码照单收下**：不判它对不对、不校验
    取值域、不看它像不像「完成」（EC-047）。落盘的是「模型说了这句话」这件事。
    """
    from ..storage.card_store import _now_iso  # 复用存储层同一时间源

    updates = {FM_FINAL: {"verdict": verdict, "at": _now_iso()}}
    try:
        result = await card_store.update_card(
            work_id, card_id, updates, "工具卡终态声明落卡",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具卡终态写入异常 card=%s: %s", card_id, exc)
        return False
    if isinstance(result, dict) and "error" in result:
        logger.warning("工具卡终态写入被拒 card=%s: %s", card_id, result.get("error"))
        return False
    return True


def read_state(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """读卡面三字段。**读盘动作，不是判定**（EC-037 恢复语义）。

    返回 ``{"script_name","step_id","round"}``；卡不存在／无这三字段／读异常
    → ``None`` ＋ warn（fail-open，不抛，EC-039）。

    **本函数是 driver 跨请求续跑（恢复）路径的读取端**。缺此动作，状态就成
    「只被写、从不被读」，本件（跨请求续跑）的立件理由即不成立。
    """
    try:
        meta = card_store._meta_of(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具卡状态读取异常 card=%s: %s", card_id, exc)
        return None
    if not meta:
        return None
    step = meta.get(FM_STEP)
    if step in (None, ""):
        # 没有步指针＝卡面没写过状态。这是一条 IO 事实，由调用方决定怎么办
        # （driver.start 据此走「新建」分支），本函数不据此下任何结论。
        return None
    script_name = str(meta.get(FM_SCRIPT, "") or "")
    raw_round = meta.get(FM_ROUND, 0)
    try:
        round_no = int(raw_round)
    except (TypeError, ValueError):
        # 读出来的值转不成整数＝一条 IO 事实；退回 0 并 warn，**不判它是否合法**
        logger.warning("工具卡 round 字段读出异常 card=%s raw=%r", card_id, raw_round)
        round_no = 0
    return {"script_name": script_name, "step_id": str(step), "round": round_no}


def read_final(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """读终态声明（``{"verdict","at"}``）；没写过／读异常 → None ＋ warn。"""
    try:
        meta = card_store._meta_of(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具卡终态读取异常 card=%s: %s", card_id, exc)
        return None
    if not meta:
        return None
    final = meta.get(FM_FINAL)
    return final if isinstance(final, dict) else None


def card_is_tool(work_id: str, card_id: str) -> bool:
    """卡面 ``card_type`` 是不是 ``tool``——**读一个字段而已**。

    这是**角色查表**（权限表查询），**不是内容判定**：读 ``card_type`` 字段与
    ``can_read`` 读 ``cards`` 字典同性质。返回 False 时只意味着「查不到 tool
    类型」，调用方据此走主卡分支；**本函数不判卡片是否合规、是否该有角色**。
    """
    try:
        meta = card_store._meta_of(work_id, card_id) or {}
    except Exception:  # noqa: BLE001
        return False
    return str(meta.get("card_type", "") or "") == "tool"
