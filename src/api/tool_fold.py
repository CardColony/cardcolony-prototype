# -*- coding: utf-8 -*-
"""轮槽（THINK-ECO-T11 · 移植对应总表 #9 新造）。

[2026-10-31 · 用户裁定：折叠整体移除] 原本本模块还承担「工具轮折叠」——把窗口外
旧工具轮压成【结论】…【依据】… 文本标记。**这套折叠已删除**，理由（裁定原文口径）：

- 它把工具调用压成 `【结论】/【依据】` 文本标记，会导致模型模仿这个形态、
  在正文里输出假工具调用（而非走结构化 ``tool_calls`` 通道）——这正是原版
  dsh ``scrubMarkerLines`` / 动作通道提醒治的病；
- 工具结果**只在当前轮展示**：更早回合的工具结果本来就不进上下文
  （``predicates.enters_context`` 排除工具块，该逻辑保留、不是缺陷）。

现在本模块只剩**轮槽**这一件事：请求局部对象，包住 routes/message_runner 的
prior messages 列表，提供三类 append 口（普通块 / 工具轮开轮 / 工具结果），
``wire_messages()`` **恒返回原列表**（零加工——T6 判据的强化版：连开关都没有了）。

- append 面：首轮用户原文固化与抢救回注走 append_plain（非轮）；
  assistant＋tool 结果走 begin_round／append_tool_result（成轮）。
- 边界：原列表永不改写（prompt 日志与失败兜底都吃原文）。
- ``fold_ops`` 属性保留为恒空列表（prompt_log「空则不同构」——空 ⇒ 键不落盘）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("api.tool_fold")


class ToolTurnSlot:
    """请求局部轮槽：包住 routes 的 prior_for_gateway 同一条 messages 列表。

    - ``wire_messages()`` **恒返回原列表本身**（零加工）——折叠已移除
      （2026-10-31 用户裁定），工具结果只在当前轮展示、更早回合不进上下文。
    - append 面：首轮用户原文固化与抢救回注走 append_plain（非轮）；
      assistant＋tool 结果走 begin_round／append_tool_result（成轮）。
    """

    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = messages
        # 每轮：{"assistant_idx", "tool_idxs"}——只记结构，不再承担任何折叠状态。
        self._rounds: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None
        self.fold_ops: list[dict[str, Any]] = []  # 恒空（折叠已移除；prompt_log 空则不同构）

    # ------------------------------------------------------------------
    # append 面
    # ------------------------------------------------------------------
    def append_plain(self, msg: dict[str, Any]) -> None:
        """非轮消息：首轮用户原文固化、抢救回注等。"""
        self._messages.append(msg)

    def begin_round(self, assistant_msg: dict[str, Any]) -> None:
        """开新轮：含工具调用的 assistant 消息（结构由调用方按原样构造）。"""
        r = {"assistant_idx": len(self._messages), "tool_idxs": []}
        self._rounds.append(r)
        self._open = r
        self._messages.append(assistant_msg)

    def append_tool_result(self, msg: dict[str, Any]) -> None:
        """工具返回入槽：归入当前开放轮（配对结构由调用方的 tool_call_id 保证）。"""
        if self._open is not None:
            self._open["tool_idxs"].append(len(self._messages))
        self._messages.append(msg)

    # ------------------------------------------------------------------
    # 装配面
    # ------------------------------------------------------------------
    @property
    def round_count(self) -> int:
        return len(self._rounds)

    async def wire_messages(self) -> list[dict[str, Any]]:
        """装配：**恒返回原列表**（零加工）。

        折叠（【结论】/【依据】两件套）已于 2026-10-31 按用户裁定整体移除：
        它把工具调用压成文本标记、误导模型在正文输出假工具调用。工具结果的
        生命周期归 ``predicates.enters_context``：当前轮在场、更早回合不进上下文。
        """
        return self._messages
