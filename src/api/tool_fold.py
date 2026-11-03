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
``wire_messages()`` 无触发时**恒返回原列表对象**；触发体积上限时返回
**投影副本**（原列表与其内 dict 永不改写）。

[2026-11-02 · card_021 实案] 「工具结果只在当前轮展示」不等于「当前轮无顶」：
重读循环把同一 8 万 token 的块×12 轮压上 1M 模型窗口。``wire_messages()``
因此在投影侧加了四道纯机械闸——单条硬顶 / 同回合去重 / 旧轮截头 /
累计预算（详 `wire_messages` docstring）。这是**体积边界**，不是折叠：
不发明【结论】/【依据】式标记、不改条数与配对、不写回原列表，
提示语全走 `[...]` 说明文本（与 tool_error 同通道）。

- append 面：首轮用户原文固化与抢救回注走 append_plain（非轮）；
  assistant＋tool 结果走 begin_round／append_tool_result（成轮）。
- 边界：原列表永不改写（prompt 日志与失败兜底都吃原文）。
- ``fold_ops`` 属性保留为恒空列表（prompt_log「空则不同构」——空 ⇒ 键不落盘）。
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("api.tool_fold")


# [2026-11-02 · card_021 实案] 回合内 wire 顶——折叠移除后 wire 零收口：
# 同一工具结果每轮全量驻留到回合结束。实案：模型改需求文档反复 not_found，
# 把 8 万 token 的块整块回读 12 轮，当轮上线 109 万 token 撞穿 1M 窗口。
# 以下四道机械闸在 wire_messages() 投影时生效（投影不改写原列表）：
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        logger.warning("[tool_fold] %s 非法（%r），用缺省 %s", name, raw, default)
        return default


def _wire_result_cap() -> int:
    """单条工具结果的上线硬顶（字符）——再大的结果也只带前 N 字符。"""
    return _env_int("WIRE_RESULT_MAX_CHARS", 60000)


def _wire_old_cap() -> int:
    """非最新轮的工具结果在 wire 上的保留头长（字符）。"""
    return _env_int("WIRE_OLD_RESULT_CHARS", 3000)


def _wire_budget() -> int:
    """本回合 wire 上工具结果的累计上限（字符）。超了从最早的开始省略。"""
    return _env_int("WIRE_BUDGET_CHARS", 300000)


def _wire_dedup_min() -> int:
    """小于此长度的相同工具结果不去重（去重提示反而比原文长）。"""
    return _env_int("WIRE_DEDUP_MIN_CHARS", 2000)


def _wire_note(msg: str) -> str:
    """wire 投影提示语——纯方括号说明，与 tool_error 同通道、不含工具
    调用形态标记（【结论】/【依据】诱发假调用之鉴，见顶部裁定注）。"""
    return f"[{msg}]"


class ToolTurnSlot:
    """请求局部轮槽：包住 routes 的 prior_for_gateway 同一条 messages 列表。

    - ``wire_messages()`` 无触发时**恒返回原列表对象**（折叠已移除
      2026-10-31 用户裁定），工具结果只在当前轮展示、更早回合不进上下文；
      触发体积上限时返回投影副本（2026-11-02 card_021 实案），原列表不改写。
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
        """装配：**无触发时恒返回原列表对象本身**；触发上限时返回投影副本。

        折叠（【结论】/【依据】两件套）已于 2026-10-31 按用户裁定整体移除：
        它把工具调用压成文本标记、误导模型在正文输出假工具调用。工具结果的
        生命周期归 ``predicates.enters_context``：当前轮在场、更早回合不进上下文。

        [2026-11-02 · card_021 实案] 但「只在当前轮展示」≠「当前轮无顶」——
        同一工具结果每轮全量驻留，重读循环把 8 万 token 的块×12 轮压上
        1M 窗口。三道机械闸（仅工具结果内容、仅投影；原列表与配对不动）：
          ① 去重——同回合逐字节相同的大结果只留**最新一份**，旧的出现位换
             成一行指引（小结果不去重，提示比原文还长）；
          ② 单条硬顶——任何一条超过 WIRE_RESULT_MAX_CHARS 截头；
          ③ 累计预算——WIRE_BUDGET_CHARS 超了才动旧轮：先截头、仍超再从
             最早开始整段省略、还超压最新轮。**不过线则完全零加工**
             （恒返回原列表对象——2026-10-31 裁定的 T6 判据原样成立）。
        提示语走 ``[...]`` 纯说明文本（与 tool_error 同通道），**绝不引入**
        任何调用形态标记——裁定删的是诱发假调用的标记，不是体积边界。
        投影全部在副本上做：``self._messages`` 与其内 dict 永不改写
        （prompt 日志与失败兜底继续吃原文）。
        """
        msgs = self._messages
        tool_pos: list[tuple[int, int]] = []   # (round_idx, msg_idx) wire 序
        for r_i, r in enumerate(self._rounds):
            for idx in r.get("tool_idxs") or []:
                if 0 <= idx < len(msgs) and msgs[idx].get("role") == "tool":
                    tool_pos.append((r_i, idx))
        if not tool_pos:
            return msgs

        last_round = tool_pos[-1][0]
        out: list[dict[str, Any]] = list(msgs)
        changed = 0

        # ① 同回合去重：逐字节相同的大结果，仅最新一份保留全文。
        by_content: dict[str, list[int]] = {}
        for _, idx in tool_pos:
            by_content.setdefault(str(msgs[idx].get("content") or ""), []).append(idx)
        dedup_min = _wire_dedup_min()
        for content, idxs in by_content.items():
            if len(idxs) < 2 or len(content) < dedup_min:
                continue
            keep_idx = idxs[-1]
            keep_id = str(msgs[keep_idx].get("tool_call_id") or "")
            for idx in idxs[:-1]:
                out[idx] = {**msgs[idx], "content": _wire_note(
                    f"本轮工具结果与下文 {keep_id} 完全相同"
                    f"（{len(content)} 字符），不再重复展示")}
                changed += 1

        # ② 单条硬顶（任何轮——单条怪物本身就够撞窗）。
        res_cap, old_cap = _wire_result_cap(), _wire_old_cap()
        for r_i, idx in tool_pos:
            if out[idx] is not msgs[idx]:
                continue                       # 已是去重指引
            content = str(out[idx].get("content") or "")
            if len(content) > res_cap:
                out[idx] = {**msgs[idx], "content": (
                    content[:res_cap] + _wire_note(
                        f"结果共 {len(content)} 字符，仅展示前 "
                        f"{res_cap}；需要更多请按行段重读来源"))}
                changed += 1

        # ③ 累计预算——只有过线才动旧轮：先截头、仍超再整段省略、
        #    还超压最新轮。预算内零加工（同对象返回的判据靠这道闸守着）。
        budget = _wire_budget()
        total = sum(len(str(out[i].get("content") or "")) for _, i in tool_pos)
        if total > budget:
            for r_i, idx in tool_pos:
                if total <= budget or r_i == last_round:
                    continue
                m = out[idx]
                content = str(m.get("content") or "")
                if len(content) > old_cap:
                    m2 = {**msgs[idx], "content": (
                        content[:old_cap] + _wire_note(
                            f"本轮较早的工具结果共 {len(content)} 字符，"
                            f"仅保留前 {old_cap}；需要原文请重新调用该工具"))}
                    out[idx] = m2
                    total -= len(content) - len(str(m2["content"]))
                    changed += 1
            if total > budget:
                for r_i, idx in tool_pos:
                    if total <= budget or r_i == last_round:
                        continue
                    content = str(out[idx].get("content") or "")
                    note = _wire_note(
                        f"本轮较早的工具结果已省略（原 {len(content)} 字符，"
                        f"tool_call_id={out[idx].get('tool_call_id')}）；"
                        "需要内容请重新调用该工具")
                    out[idx] = {**msgs[idx], "content": note}
                    total -= len(content) - len(note)
                    changed += 1
            if total > budget:
                for r_i, idx in reversed(tool_pos):
                    if total <= budget or r_i != last_round:
                        continue
                    content = str(out[idx].get("content") or "")
                    if len(content) <= old_cap:
                        continue
                    out[idx] = {**msgs[idx], "content": (
                        content[:old_cap] + _wire_note(
                            f"结果共 {len(content)} 字符，本轮 wire 已超预算，"
                            f"仅展示前 {old_cap}；需要更多请按行段重读来源"))}
                    total -= len(content) - old_cap
                    changed += 1

        if not changed:
            return msgs
        logger.info("[tool_fold] wire 投影生效 msgs=%s tool=%s 改写=%s",
                    len(msgs), len(tool_pos), changed)
        return out
