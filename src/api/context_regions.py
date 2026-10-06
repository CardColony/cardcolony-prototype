# -*- coding: utf-8 -*-
"""上下文装配线 · 区表定义（THINK-ECO-T11 · 移植对应总表 #1/#2/#3 直接搬）。

dsh 的区表组织方式照搬、内容换皮（dsh 八区 → 推演六区，移植对应总表 #1）：
- 区 = {order, name, channel, seats, source}；装配动作只按 order 收区、拼接，
  不评价、不裁剪、不判断，不引入任何预算/配额/截断（渲染零加工，总表 #2/#3）。
- 「没有来源就不出现这个区」是 dsh 原生原则，本表沿用：内容为空的区不进 join。
- 区表不设独立「当前轮」区（用户 2026-10-04 23:26 明令，不引入 dsh 轮槽截获
  机制）；999 区仅为「用户消息排最后」的位置规则——用户原文经出口 user 位
  排在 messages 最后，不另设区内容。

join 规则（方案 §五，逐字复现是硬要求）：
- system(main)      ＝ 使用说明（BASE_PROMPT 指针，不复制）＋卡片清单＋当前卡
- system(viewpoint) ＝ 卡片清单＋当前卡（职责分离裁定 2026-10-02：三席零指令）
- messages          ＝ 对话历史＋本轮工具轮

本模块只含纯数据与纯函数，不做 IO、不持有状态。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .prompts import BASE_PROMPT

# 区表（方案 §五六区；order 即装配顺序）
REGION_TABLE: list[dict[str, Any]] = [
    {
        "order": 0,
        "name": "使用说明",
        "channel": "system",
        "seats": ("main",),
        "source": "prompts.BASE_PROMPT 常量（逐字引用，不复制——区只持指针）",
    },
    {
        "order": 100,
        "name": "卡片清单",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "source": "context_assembler._load_cards_index（现状扫描，每卡一行 id·标题·父卡）",
    },
    {
        "order": 150,
        "name": "当前卡",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "source": "现状（cards 索引按 current_id 匹配；空行＋当前卡一行）",
    },
    {
        # [T12 二期] 凝练记忆区（condense_fold）——order 200 空闲位插入（REL-01：
        # 只 append 200，T11 六区条目一字不动）。空记忆区跳过（「没有来源就不出现」）。
        "order": 200,
        "name": "记忆（condense_fold）",
        "channel": "system",
        "seats": ("main",),  # 仅 main：viewpoint join 规则「清单＋当前卡」逐字不动（方案 §五职责分离）
        "source": "memory_store.load_memory（works/{work_id}/memory/{card_id}.json 凝练归拢 entries；调用方完成 IO 后传入渲染）",
    },
    {
        "order": 300,
        "name": "对话历史",
        "channel": "messages",
        "seats": ("main", "viewpoints"),
        "source": "session_store.read_session → role/content 两键投影（现状）",
    },
    {
        "order": 900,
        "name": "本轮工具轮",
        "channel": "messages",
        "seats": ("main",),
        "source": "本轮工具循环（tool_fold.ToolTurnSlot 轮槽；折叠与否见 tool_fold）",
    },
    {
        "order": 999,
        "name": "用户消息",
        "channel": "user",
        "seats": ("main", "viewpoints"),
        "source": "现状（用户原文；仅为「排最后」位置规则，非独立区内容）",
    },
]


def render_card_list_region(cards: list[dict[str, Any]], work_card_id: str) -> str:
    """100 区渲染：每卡一行（id · 标题 · 父卡）。行文案与改造前逐字一致。"""
    lines: list[str] = ["## 卡片清单"]
    for c in cards:
        if c["card_id"] == work_card_id or not c["parent"]:
            lines.append(f"{c['card_id']} · {c['title']}（根）")
        else:
            lines.append(f"{c['card_id']} · {c['title']}（父：{c['parent']}）")
    return "\n".join(lines)


def render_current_card_region(cur: dict[str, Any] | None) -> str:
    """150 区渲染：当前卡一行。无当前卡 → 空串（没有来源就不出现这个区）。"""
    if cur is None:
        return ""
    return "\n".join(["## 当前卡", f"{cur['card_id']} · {cur['title']}"])


def render_memory_region(entries: list[dict[str, Any]]) -> str:
    """200 区渲染（T12 二期）：condense_fold 记忆条目「原样取＋客观信息」投影——
    不排序、不截断、不设预算（渲染零加工，总表 #2/#3；RD-05 无长度限制）。

    entries 由调用方完成 IO 后传入（本模块纯函数不做 IO——模块头 L17 硬约束；
    mark 读取 IO 分离在 routes/assembler 层，SP-05）。条目存储序即输出序
    （一卡一链，seq＝读时现算块序号锚点）。空条目 → 空串（没有来源就不出现这个区）。
    """
    if not entries:
        return ""
    lines = ["## 记忆（condense_fold 凝练归拢）"]
    for e in entries:
        if not isinstance(e, dict):
            continue
        seq = e.get("seq", "?")
        occurred_at = str(e.get("occurred_at", "") or "")
        head = f"[块{seq}" + (f" · {occurred_at}" if occurred_at else "") + "]"
        conclusion = str(e.get("conclusion", "") or "")
        lines.append(f"{head} {conclusion}".rstrip())
        evidence = str(e.get("evidence", "") or "")
        fingerprint = str(e.get("fingerprint", "") or "")
        if evidence:
            lines.append(evidence + (f"（{fingerprint}）" if fingerprint else ""))
    return "\n".join(lines)


def assemble_regions(region_texts: list[str]) -> str:
    """装配动作（总表 #3）：只按给定顺序收区、拼接；空区跳过；零内容判断。

    与改造前 build_neutral_context 的 "\n".join(lines) 逐字等价：
    150 区存在时两者都以空行分隔（"清单…\\n\\n## 当前卡\\n…"）。
    """
    return "\n\n".join(t for t in region_texts if t)


def build_log_regions(
    system_regions: list[dict[str, Any]],
    history_messages: list[dict[str, Any]],
    turn_messages: list[dict[str, Any]],
    user_content: str,
) -> list[dict[str, Any]]:
    """T7：prompt 日志 regions 键的条目构建（只存指针与指纹，不复制正文）。

    每区一条：order/名称/通道/席位/字节数/指纹/条目数。六区口径：
    0＝BASE_PROMPT 指针（计字节与指纹便于对账）；100/150＝system 分区渲染件；
    300＝对话历史投影；900＝本轮工具轮（本轮循环内 append 的全部消息——
    含首轮固化与抢救回注，与 300 合计恰好划分整条 messages）；999＝用户原文。
    """
    def _entry(order: int, name: str, channel: str, seats, blob: str,
               entries: int) -> dict[str, Any]:
        raw = blob.encode("utf-8")
        return {
            "order": order, "name": name, "channel": channel,
            "seats": list(seats), "bytes": len(raw),
            "fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
            "entries": entries,
        }

    out = [_entry(0, "使用说明", "system", ("main",), BASE_PROMPT, 1)]
    for r in system_regions:
        out.append(_entry(r["order"], r["name"], r["channel"], r["seats"],
                          r["text"], r["entries"]))
    out.append(_entry(300, "对话历史", "messages", ("main", "viewpoints"),
                      json.dumps(history_messages, ensure_ascii=False),
                      len(history_messages)))
    out.append(_entry(900, "本轮工具轮", "messages", ("main",),
                      json.dumps(turn_messages, ensure_ascii=False),
                      len(turn_messages)))
    out.append(_entry(999, "用户消息", "user", ("main", "viewpoints"),
                      user_content, 1))
    return out
