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
from dataclasses import dataclass, field
from typing import Any, Optional

from .prompts import BASE_PROMPT


@dataclass
class RegionCtx:
    """装配上下文（底座 2026-10-15）：**取数一次**，渲染器一律纯函数。

    IO 全在装配前（`context_assembler.build_ctx`）；渲染器只读它，不自己取数。
    """

    work_id: str = ""
    card_id: str = ""
    cards: list[dict[str, Any]] = field(default_factory=list)
    work_card_id: str = ""
    cur: Optional[dict[str, Any]] = None
    cur_meta: dict[str, Any] = field(default_factory=dict)
    project_class: str = ""
    type_registry: dict[str, Any] = field(default_factory=dict)
    memory_state: Optional[dict[str, Any]] = None
    memory_entries: list[dict[str, Any]] = field(default_factory=list)


def _count_duty_lines(ctx: RegionCtx, text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith("- "))


# ---------------------------------------------------------------------------
# 区注册表（底座 · 2026-10-15）：**区的唯一声明处**。
#
# 每项＝一个可组合的区组件：
#   key     稳定标识（供控制面 config/context_regions.json 与日志引用）
#   order   装配顺序
#   owner   谁装配它——"assembler"（本装配器）/"prompt"（prompts 出口）/"runner"
#           （message_runner）；**build_context_regions 只渲染 owner=="assembler" 的区**
#   render  渲染器：纯函数 RegionCtx → 文本；**空串＝「没有来源就不出现这个区」**
#   count   entries（int 或 callable(ctx, text) → int），供 prompt 日志 regions
#
# 加 / 删 / 调序一个区＝改这张表（或控制面配置），**装配器一字不动**。
# ---------------------------------------------------------------------------
REGION_TABLE: list[dict[str, Any]] = [
    {
        "key": "instruction",
        "order": 0,
        "name": "使用说明",
        "channel": "system",
        "seats": ("main",),
        "owner": "prompt",  # 由 prompts.build_system_prompt 出口引用
        "source": "prompts.BASE_PROMPT 常量（逐字引用，不复制——区只持指针）",
        "render": lambda ctx: BASE_PROMPT,
        "count": 1,
    },
    {
        # [卡片职责区 2026-10-14] order 90：类型级职责（每类卡一行），紧邻 100 之前。
        # 来源＝类型注册表；读不到 → 区不出现（「没有来源就不出现这个区」）。
        "key": "card_duty",
        "order": 90,
        "name": "卡片职责",
        "channel": "system",
        "seats": ("main",),
        "owner": "assembler",
        "source": "card_store.load_type_registry（l1_vocabularies.*.types[] 的 label/code/description）",
        "render": lambda ctx: render_card_duty_region(ctx.type_registry),
        "count": _count_duty_lines,
    },
    {
        "key": "card_list",
        "order": 100,
        "name": "卡片清单",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "assembler",
        "source": "context_assembler._load_cards_index（查库，每卡一行 id·标题·父卡）",
        "render": lambda ctx: render_card_list_region(ctx.cards, ctx.work_card_id),
        "count": lambda ctx, text: len(ctx.cards),
    },
    {
        "key": "current_card",
        "order": 150,
        "name": "当前卡",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "assembler",
        "source": "现状（cards 索引按 current_id 匹配；空行＋当前卡一行）",
        "render": lambda ctx: render_current_card_region(ctx.cur),
        "count": 1,
    },
    {
        # [上下文管理 2026-10-14] order 155：卡片区域说明——讲清正卡三块与影子
        # 四节「是什么、怎么写」，紧随 150 当前卡（它讲的就是当前卡的块）。
        # 文案按「本树 project_class × 本卡 card_type」择套；第三块**只出本树那
        # 一个名字**（不并列、不互相提及）；**不含「修改历史」**（模型不需要知道）。
        # 依据：docs/卡片区域说明-上下文文案-20261014.md（＝卡片模块方案 v3 §六
        # ＋用户 2026-10-14 22:04 三条纠正 ＋「要写清每块怎么写」）。
        "key": "region_guide",
        "order": 155,
        "name": "卡片区域说明",
        "channel": "system",
        "seats": ("main",),
        "owner": "assembler",
        "source": "context_regions.render_card_region_guide（静态文案，按树×卡种择套）",
        "render": lambda ctx: render_card_region_guide(
            str((ctx.cur_meta or {}).get("card_type") or ""), ctx.project_class),
        "count": 7,  # 正卡三块 ＋ 影子四节
    },
    {
        # [T12 二期] 凝练记忆区（condense_fold）——order 200 空闲位插入（REL-01：
        # 只 append 200，T11 六区条目一字不动）。空记忆区跳过（「没有来源就不出现」）。
        "key": "memory",
        "order": 200,
        "name": "记忆（condense_fold）",
        "channel": "system",
        "seats": ("main",),  # 仅 main：viewpoint join 规则「清单＋当前卡」逐字不动（方案 §五职责分离）
        "owner": "assembler",
        "source": "memory_store.load_memory（works/{work_id}/memory/{card_id}.json 凝练归拢 entries；调用方完成 IO 后传入渲染）",
        "render": lambda ctx: render_memory_region(ctx.memory_entries),
        "count": lambda ctx, text: len(ctx.memory_entries),
    },
    {
        "key": "history",
        "order": 300,
        "name": "对话历史",
        "channel": "messages",
        "seats": ("main", "viewpoints"),
        "owner": "runner",  # 由 message_runner 装配
        "source": "session_store.read_session → role/content 两键投影（现状）",
        "render": lambda ctx: "",
        "count": 0,
    },
    {
        "key": "tool_turn",
        "order": 900,
        "name": "本轮工具轮",
        "channel": "messages",
        "seats": ("main",),
        "owner": "runner",
        "source": "本轮工具循环（tool_fold.ToolTurnSlot 轮槽；折叠与否见 tool_fold）",
        "render": lambda ctx: "",
        "count": 0,
    },
    {
        "key": "user_message",
        "order": 999,
        "name": "用户消息",
        "channel": "user",
        "seats": ("main", "viewpoints"),
        "owner": "runner",
        "source": "现状（用户原文；仅为「排最后」位置规则，非独立区内容）",
        "render": lambda ctx: "",
        "count": 0,
    },
]


def render_card_duty_region(registry: dict[str, Any]) -> str:
    """90 区渲染（卡片职责）：每类卡一行 `- {label}（{code}）：{description}`。

    来源＝类型注册表 `l1_vocabularies.*.types[]`（每项 label/code/description）。
    **必须从注册表读**：新增一种卡＝注册表加一项（含 description）⇒ 本区自动多
    一行、零代码改动；不得在代码里硬编码类型清单（否则扩展性失效）。
    顺序＝`root` 作用域在前，其余按 `l1_vocabularies` 声明序。
    渲染零加工：description 原样取，不加工、不截断。
    空注册表（读不到/坏 JSON → `{}`）或无任何类型 → 空串（「没有来源就不出现
    这个区」，不报错、不阻断装配）。
    """
    vocabs = registry.get("l1_vocabularies") if isinstance(registry, dict) else None
    if not isinstance(vocabs, dict) or not vocabs:
        return ""
    # root 作用域在前，其余按注册表声明序（root 是独立 scope，非 project_class）
    scopes = (["root"] if "root" in vocabs else []) + [k for k in vocabs if k != "root"]
    lines: list[str] = ["## 卡片职责"]
    for scope in scopes:
        vocab = vocabs.get(scope)
        if not isinstance(vocab, dict):
            continue
        for t in (vocab.get("types") or []):
            if not isinstance(t, dict):
                continue
            label = str(t.get("label") or "")
            code = str(t.get("code") or "")
            desc = str(t.get("description") or "")
            lines.append(f"- {label}（{code}）：{desc}")
    if len(lines) == 1:  # 注册表里没有任何类型 → 空区
        return ""
    return "\n".join(lines)


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


# 第三块：本树 project_class → 对外名（与 routes._apply_block_set 同源口径）
_THIRD_BLOCK_NAME_BY_TREE = {"rd_project": "需求文档"}

# 第三块「怎么写」——两套，按本树只出一套
_THIRD_BLOCK_HOWTO = {
    "需求文档": (
        "需求文档——本功能全部需求的确定版详细描述，是本功能需求的唯一确定源。\n"
        "      写法：按需求逐条写清“要什么、验收标准是什么”；只写确定版；\n"
        "            讨论、备选、过程不写这里。"
    ),
    "定论": (
        "定论——本课题已经作出的决定与收敛。\n"
        "      写法：只追加、不删不改；一条一行，写清“决定了什么、依据是什么”；\n"
        "            某条失效就在原处标一行“→ 被Dxx取代”，不保留原文。"
    ),
}


def render_card_region_guide(card_type: str, project_class: str) -> str:
    """155 区渲染（卡片区域说明）：讲清正卡三块与影子四节「是什么、怎么写」。

    - **第三块的名字由本树决定**（`rd_project` → 需求文档；其余 → 定论）——
      文案里**只出那一个名字**，不并列、不互相提及（用户 2026-10-14 22:04）。
    - **影子四节一并说明**（本卡私有，只有本卡读得到）。
    - **不含「修改历史」**（系统留痕，模型不需要知道）。
    - 无来源（card_type 为空）→ 空串（「没有来源就不出现这个区」）。
    """
    if not card_type:
        return ""
    is_root = (card_type == "project")
    third = _THIRD_BLOCK_NAME_BY_TREE.get(project_class or "", "定论")
    subject = "项目" if is_root else "这张卡"
    head = ("〔卡片区域说明〕这张是项目根卡，正文分三块，各管各的，不要混写："
            if is_root else
            "〔卡片区域说明〕这张卡的正文分三块，各管各的，不要混写：")
    mission = ("使命——整个项目要做成什么（总目标与范围）。"
               if is_root else
               "使命——这张卡要做什么、要做成什么（目标与交付物）。")
    return "\n".join([
        head,
        "",
        mission,
        "      写法：≤5 行，一句话说清；立项定稿，只有用户能改，你没有写它的通道。",
        "",
        f"现状——{subject}当前确定的状态与结论。",
        f"      别的卡读{'根卡' if is_root else '这张卡'}，先读的就是它——"
        "所以这里写别的卡继续工作要知道的三类东西：",
        "      · 当前确定状态：现在已经确定了什么、哪些能作为其他卡工作的依据；",
        f"      · 接口与约定：{subject}对外提供什么，输入输出、调用方式、约束是什么；",
        f"      · 依赖与影响范围：{subject}依赖哪些模块；它变更时哪些卡或功能会受影响。",
        "      写法：整块覆盖、只保留最新一份；只写已经确定的结论；",
        "            不写过程、不写“做到哪”的流水账。",
        "",
        _THIRD_BLOCK_HOWTO[third],
        "",
        "〔影子卡区域说明〕影子是本卡自己的过程记录，别的卡读不到，只有你能读写：",
        "",
        "产物清单——交接物与权威位置：完成的结果在哪里，哪份文档/代码/数据是准的。",
        "      写法：一行一条指针（路径＋用途），只写指针，不搬正文。",
        "",
        "待协作事项——需要谁提供什么、卡在什么外部条件上。",
        "      写法：一条一项，写清“等谁确认什么、确认后本卡才能继续”。",
        "",
        "迭代历史——系统自动记的改动留痕，你不用写。",
        "",
        "进度——不重要，不必特意维护。",
    ])


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
