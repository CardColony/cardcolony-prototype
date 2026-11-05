# -*- coding: utf-8 -*-
"""原文判定（照抄 dsh `gen3/context/predicates.ts`）。

**这个模块存在的唯一理由**：把「什么算原文」收成**一处判据**，让凝练、折叠、导入
三个消费方共用，谁也不用 import 别人的模块、也不会各自写一套。

## dsh 原判据（逐条对应）

```ts
MESSAGE_TYPES = {'user/message', 'assistant/message', 'tool/result'}

isOriginalEvent(e):
  不是上面三类 → false
  user/message:
    data.source 未定义          → true   （人类直输，原文）
    source.kind === 'plugin'    → false  （**统一排除一切 plugin 注入**，含本机制自己的注入）
    source.kind === 'steer'     → false  （轮末第三方复核续命指令：机器生成，非原文）
    压缩检查点源                 → false  （防御性双保险）
  assistant/message、tool/result → true

isCondenseOriginalEvent(e) = isOriginalEvent(e) 且 **排除 tool/result**
  —— isOriginalEvent 保留 tool/result=true（供 fold 配对用）；
     凝练侧用本谓词实现「工具结果正文不录凝练」。
```

## 为什么 `plugin` 那条不能删（dsh 原注，逐字保留其意）

`source.kind === 'plugin'` 是**通用的「注入 vs 原文」判据**，不是折叠专用：它排除**所有**
plugin 注入的 user/message 行（折叠占位符、基线注入等）。删了它，凝练窗口会把折叠占位符
和注入行**当原文再凝一遍**。

## 本项目的映射

| dsh | 本项目 |
|---|---|
| `SessionEvent.type` | 块的事件型（`origin_store.event_type_of` 同口径：user_message/assistant_text/tool_result/tool_call） |
| `data.source` | 块上的 `source` 字段（`session_facade` 已随块落盘，如 goal 域） |

⚠️ 本项目**多一类** `tool_call`（带 tool_calls 的 assistant 块）。按 dsh 口径它属于
`assistant/message` ⇒ **是原文**（供 fold 配对）；**凝练侧同样排除**（它是空 content 的调用壳）。
"""

from __future__ import annotations

from typing import Any, Optional

#: 视为"消息类"的事件型（dsh `MESSAGE_TYPES` 的本项目映射）
MESSAGE_TYPES: frozenset[str] = frozenset({
    "user_message",       # ← user/message
    "assistant_text",     # ← assistant/message
    "tool_call",          # ← assistant/message（带 tool_calls 的 assistant 块）
    "tool_result",        # ← tool/result
    # [卡间消息进凝练 2026-10-27 · 用户令「消息会留在上下文里直到被凝练掉，
    # 所以你需要确保这东西会被计算进记忆凝练」] 消息块（actor=card:xxx）的事件型
    # 是 msg_*，改前不在本集合里 ⇒ `is_original_event` 判 false ⇒ **消息永远不进
    # 凝练**，落在上下文里就再也去不掉，只增不减。
    # 加进来后它们与 human/assistant 同级：进凝练、可被折叠成条目。
    "msg_ask", "msg_answer", "msg_notify",
    "msg_directive", "msg_sync", "msg_report", "coder_done", "worker_done",
})

#: 凝练侧额外排除的型（工具结果正文不录凝练）
_CONDENSE_EXCLUDED: frozenset[str] = frozenset({"tool_result", "tool_call"})


def _source_kind(block: dict[str, Any]) -> Optional[str]:
    """取 `source.kind`（没有 source / 不是 dict ⇒ None＝人类直输）。"""
    src = block.get("source")
    if not isinstance(src, dict):
        return None
    kind = src.get("kind")
    return str(kind) if kind is not None else None


def is_original_event(block: dict[str, Any], event_type: Optional[str] = None) -> bool:
    """这条块是不是**原文**（dsh `isOriginalEvent` 同口径）。

    `event_type` 不给时按 `origin_store.event_type_of` 现算（保持两处同尺）。
    """
    etype = event_type
    if etype is None:
        from .origin_store import event_type_of
        etype = event_type_of(block)
    if etype not in MESSAGE_TYPES:
        return False
    if etype == "user_message":
        kind = _source_kind(block)
        if kind is None:
            return True                 # 无 source：人类直输，原文
        if kind == "plugin":
            return False                # 统一排除一切 plugin 注入（**删了会重凝占位符**）
        if kind == "steer":
            return False                # 轮末第三方复核续命指令：机器生成，非原文
        if kind in ("compaction_checkpoint", "compact_checkpoint"):
            return False                # 压缩检查点源（防御性双保险）
        return True
    return True                         # assistant_text / tool_call / tool_result：原文


def is_condense_original_event(block: dict[str, Any],
                               event_type: Optional[str] = None) -> bool:
    """凝练侧原文判定＝原文 **且不是工具块**（dsh `isCondenseOriginalEvent` 同口径）。"""
    etype = event_type
    if etype is None:
        from .origin_store import event_type_of
        etype = event_type_of(block)
    if etype in _CONDENSE_EXCLUDED:
        return False
    return is_original_event(block, etype)


def is_original_node(block: dict[str, Any]) -> bool:
    """seq 级判据（dsh `isOriginalNode` 的对应物）：单块是否原文。"""
    return is_original_event(block)


def enters_context(block: dict[str, Any]) -> bool:
    """该块是否**进入模型上下文**（历史区投影的唯一判据）。

    [2026-10-23 用户需求] 工具结果不再进上下文 ⇒ 判据收紧为：
    - 环节留痕块（behavior 非空）不进；
    - actor=tool（工具结果）不进；
    - actor=assistant 且带 tool_calls 的 A 块（content 恒空）不进——
      必须与工具结果成对丢弃，否则留孤立调用 ⇒ 端点 400。
    其余（human / 有正文的 assistant / 卡间消息）都进。

    **触发线、窗口预算、销账都以此为准**——三尺同一把尺，别再各写一份。
    """
    if block.get("behavior"):
        return False
    actor = str(block.get("actor") or "")
    if actor == "tool":
        return False
    if actor == "assistant" and block.get("tool_calls"):
        return False
    return True
