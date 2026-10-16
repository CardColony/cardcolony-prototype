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
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from . import prompts
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
    direct_child_status: list[dict[str, Any]] = field(default_factory=list)
    # [卡片思维区 2026-10-15] 按卡类型挂的文本（多维度）：{维度: {类型 code: 文本}}。
    # 由装配前一处 IO（context_assembler.load_type_texts）取好放入，渲染器只读。
    type_texts: dict[str, dict[str, str]] = field(default_factory=dict)
    project_class: str = ""
    type_registry: dict[str, Any] = field(default_factory=dict)
    memory_state: Optional[dict[str, Any]] = None
    memory_entries: list[dict[str, Any]] = field(default_factory=list)
    controls: dict[str, Any] = field(default_factory=dict)
    history_source: list[dict[str, Any]] = field(default_factory=list)
    history_mark: int = 0
    request_prior: Optional[list[dict[str, Any]]] = None
    request_prior_key: str = "history"
    request_user: Optional[str] = None
    request_once: list[dict[str, Any]] = field(default_factory=list)
    request_tail: list[dict[str, Any]] = field(default_factory=list)
    request_seat_identity: list[dict[str, Any]] = field(default_factory=list)
    rule_sections: dict[str, str] = field(default_factory=dict)


def _count_duty_lines(ctx: RegionCtx, text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith("- "))


# ---------------------------------------------------------------------------
# 区注册表（底座 · 2026-10-15）：**区的唯一声明处**。
#
# 每项＝一个可组合的区组件：
#   key          稳定标识（供控制面 config/context_regions.json 与日志引用）
#   order        装配顺序
#   name/channel 展示名与语义通道
#   owner        **唯一运行 owner＝context base**（本次全量接管）；旧代码模块仅是 source_owner
#   delivery     请求槽位：system/prior/user/once/tail
#   profiles     消费者：main/viewpoint/draft
#   source_owner 原先的代码路径（审计/迁移信息，不再拥有装配权）
#   render       纯函数 RegionCtx → payload；空值＝「没有来源就不出现这个区」
#   count        entries（供 prompt 日志）
#
# 所有活动上下文区统一由 compose_model_context 遍历并编入模型请求；
# prompts/message_runner/simple_service/card_draft 不再各自拼装上下文区。
# ---------------------------------------------------------------------------
REGION_TABLE: list[dict[str, Any]] = [
    {
        "key": "instruction",
        "order": 0,
        "name": "使用说明",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "prompt",
        "delivery": "system",
        "profiles": ("main",),
        "source": "prompts.BASE_PROMPT 常量（逐字引用，不复制——区只持指针）",
        "render": lambda ctx: BASE_PROMPT,
        "count": 1,
    },
    {
        # [规则卡 2026-10-15] order 85：instruction 之后、卡片职责之前（用户裁定 6）。
        # 来源＝规则卡正文按卡类型切区后的 rule_sections（context_assembler.build_ctx
        # 一次 IO）；按当前卡 card_type 择区，空/缺→空串（没有来源就不出现这个区）。
        "key": "rule",
        "order": 85,
        "name": "规则注入",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "规则卡（card_type=rule）正文按 `## {区名}` 切区，取当前卡 card_type 对应区",
        "render": lambda ctx: render_rule_region(
            ctx.rule_sections, str((ctx.cur_meta or {}).get("card_type") or "")),
        "count": lambda ctx, text: 1 if text else 0,
    },
    {
        # [卡片职责区 2026-10-14] order 90：类型级职责（每类卡一行），紧邻 100 之前。
        # 来源＝类型注册表；读不到 → 区不出现（「没有来源就不出现这个区」）。
        "key": "card_duty",
        "order": 90,
        "name": "卡片职责",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "card_store.load_type_registry（l1_vocabularies.*.types[] 的 label/code/description）",
        "render": lambda ctx: render_card_duty_region(ctx.type_registry),
        "count": _count_duty_lines,
    },
    {
        # [卡片思维区 2026-10-15] order 95：紧随 90 卡片职责之后——
        # 90 讲「这类卡**干什么**」（类型级职责，来自类型注册表），
        # 本区讲「这类卡**该怎么想**」（类型级文本，来自 config/type_texts.json
        # 的 thinking 维度）。**只取当前卡 card_type 的那一段**，不并列别类；
        # 该类型在该维度没有文本 → 区不出现（「没有来源就不出现这个区」）。
        "key": "type_thinking",
        "order": 95,
        "name": "卡片思维",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "config/type_texts.json 的 thinking[本卡 card_type]（空/缺 → 区不出现）",
        "render": lambda ctx: render_type_thinking_region(
            str((ctx.cur_meta or {}).get("card_type") or ""), ctx.type_texts),
        "count": 1,
    },
    {
        "key": "card_list",
        "order": 100,
        "name": "卡片清单",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
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
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
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
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "context_regions.render_card_region_guide（静态文案，按树×卡种择套）",
        "render": lambda ctx: render_card_region_guide(
            str((ctx.cur_meta or {}).get("card_type") or ""), ctx.project_class),
        "count": 7,  # 正卡三块 ＋ 影子四节
    },
    {
        # [父卡展示子卡现状 2026-10-15] order 170：紧随 155 之后、200 记忆之前——
        # 「我是谁」→「我的直接子卡现在怎么样」→「我的记忆」。
        # 来源＝直接子卡的 status_block（**只一层，不含孙级**；SQL 不递归）。
        "key": "child_status",
        "order": 170,
        "name": "直接子卡现状",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "当前卡的**直接**子卡 status_block（WHERE parent=?，不递归；空现状不注入）",
        "render": lambda ctx: render_direct_child_status_region(ctx.direct_child_status),
        "count": lambda ctx, text: sum(
            1 for child in ctx.direct_child_status if _child_status_body(child)),
    },
    {
        # [T12 二期] 凝练记忆区（condense_fold）——order 200 空闲位插入（REL-01：
        # 只 append 200，T11 六区条目一字不动）。空记忆区跳过（「没有来源就不出现」）。
        "key": "memory",
        "order": 200,
        "name": "记忆（condense_fold）",
        "channel": "system",
        "seats": ("main",),  # 仅 main：viewpoint join 规则「清单＋当前卡」逐字不动（方案 §五职责分离）
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
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
        "owner": "context",
        "source_owner": "runner",
        "delivery": "prior",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "session_facade.read → 统一投影后对话历史",
        "render": lambda ctx: _render_prior_for(ctx, "history"),
        "count": 0,
    },
    {
        "key": "tool_turn",
        "order": 900,
        "name": "本轮工具轮",
        "channel": "messages",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "runner",
        "delivery": "prior",
        "profiles": ("main",),
        "source": "ToolTurnSlot.wire_messages → 统一投影为本轮对话续接",
        "render": lambda ctx: _render_prior_for(ctx, "tool_turn"),
        "count": 0,
    },
    {
        "key": "user_message",
        "order": 999,
        "name": "用户消息",
        "channel": "user",
        "seats": ("main", "viewpoints"),
        "owner": "context",
        "source_owner": "runner",
        "delivery": "user",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "当前调用的 user 内容（包括用户发言、续跳指令或质询输入）",
        "render": lambda ctx: ctx.request_user if ctx.request_user is not None else "",
        "count": 0,
    },
    {
        "key": "judgment_protocol",
        "order": 1000,
        "name": "判断协议注入",
        "channel": "messages",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "current_turn/message_runner",
        "delivery": "once",
        "profiles": ("main",),
        "source": "本轮一次性注入；不进入轮槽",
        "render": lambda ctx: ctx.request_once,
        "count": lambda ctx, payload: len(payload),
    },
    {
        "key": "context_tail",
        "order": 1010,
        "name": "工作规范与流程注入",
        "channel": "messages",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "current_turn/message_runner",
        "delivery": "tail",
        "profiles": ("main",),
        "source": "工作规范/流程注入；轮首后固化进轮槽",
        "render": lambda ctx: ctx.request_tail,
        "count": lambda ctx, payload: len(payload),
    },
    {
        "key": "seat_identity",
        "order": 1020,
        "name": "三席身份注入",
        "channel": "messages",
        "seats": ("viewpoints",),
        "owner": "context",
        "source_owner": "simple_service/message_runner",
        "delivery": "tail",
        "profiles": ("viewpoint",),
        "source": "三席调用的身份提示消息",
        "render": lambda ctx: ctx.request_seat_identity,
        "count": lambda ctx, payload: len(payload),
    },
]


def project_history_messages(
    blocks: list[dict[str, Any]], mark: int | None = None,
    allowed_roles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """历史区的统一投影：按凝练水位切片、滤过程块、转 card-msg 头，保留思考回传。

    这是纯函数；卡片会话文件不改。mark 越界时回退全量，与旧投影口径一致。
    """
    source = blocks or []
    if mark is not None:
        source = source[mark:] if 0 <= mark <= len(source) else source
    out: list[dict[str, Any]] = []
    from . import card_msg
    for m in source:
        if m.get("behavior"):
            continue
        role = str(m.get("role") or "")
        if allowed_roles is not None and role not in allowed_roles:
            continue
        item = {
            "role": role,
            "content": (card_msg.render_for_context(m["content"])
                        if str(m.get("actor") or "").startswith("card:")
                        else m["content"]),
        }
        if m.get("reasoning_content"):
            item["reasoning_content"] = m["reasoning_content"]
        out.append(item)
    return out


def _render_prior_for(ctx: RegionCtx, key: str) -> list[dict[str, Any]]:
    """prior 槽的两个区共用一个 payload：**谁匹配谁出，另一个让位**。

    `ToolTurnSlot.wire_messages()` 返回的是**整条 prior**（历史＋本轮工具轮），
    故 prior_key="tool_turn" 时 history 必须返回空——否则历史会被发两遍。
    """
    if ctx.request_prior is not None:
        return ctx.request_prior if ctx.request_prior_key == key else []
    if key == "history":
        return project_history_messages(ctx.history_source, ctx.history_mark)
    return []


def _is_enabled(ctx: RegionCtx, spec: dict[str, Any]) -> bool:
    ctl = (ctx.controls or {}).get(spec["key"])
    return not (isinstance(ctl, dict) and ctl.get("enabled") is False)


def render_registered_regions(
    ctx: RegionCtx,
    *,
    profile: str,
    deliveries: set[str],
    source_owner: str | None = None,
) -> list[dict[str, Any]]:
    """Context base 唯一执行入口：按注册表/控制面渲染任意通道的区。

    owner 已统一为 context；source_owner 仅是查询过滤参数（如日志兼容视图），
    不代表运行时交接给旧模块。
    """
    out: list[dict[str, Any]] = []
    for spec in sorted(REGION_TABLE, key=lambda s: s["order"]):
        if profile not in spec.get("profiles", ()):
            continue
        if spec.get("delivery") not in deliveries or not _is_enabled(ctx, spec):
            continue
        if source_owner is not None and spec.get("source_owner") != source_owner:
            continue
        ctl = (ctx.controls or {}).get(spec["key"])
        order = spec["order"]
        if isinstance(ctl, dict) and isinstance(ctl.get("order"), int):
            order = ctl["order"]
        payload = spec["render"](ctx)
        # user prompt 允许空文本（它仍是一个消息位）；其他空 payload 表示无来源。
        if not payload and spec.get("delivery") != "user":
            continue
        count_fn = spec.get("count", 0)
        entries = count_fn(ctx, payload) if callable(count_fn) else count_fn
        out.append({
            "key": spec["key"], "order": order, "name": spec["name"],
            "channel": spec["channel"], "seats": spec["seats"],
            "delivery": spec["delivery"], "source_owner": spec.get("source_owner", "context"),
            "payload": payload, "entries": entries,
        })
    out.sort(key=lambda r: r["order"])
    return out


def compose_model_context(
    ctx: RegionCtx,
    *,
    profile: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    prior_key: str = "history",
    user_content: Optional[str] = None,
    turn_once: Optional[list[dict[str, Any]]] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
    seat_identity: Optional[list[dict[str, Any]]] = None,
    system_override: Optional[str] = None,
    system_prefix: Optional[str] = None,
) -> dict[str, Any]:
    """一次性把注册表区组合成模型调用契约（system/prior/user/once/tail）。

    主模型、三席、起草调用共享此路由；具体调用方只提供动态数据，不自己拼装区。
    顺序仍按 gateway 契约：system → prior → user → turn_once → turn_tail。
    """
    request_ctx = replace(
        ctx,
        request_prior=prior_messages,
        request_prior_key=prior_key,
        request_user=user_content,
        request_once=turn_once or [],
        request_tail=turn_tail or [],
        request_seat_identity=seat_identity or [],
    )
    rendered = render_registered_regions(
        request_ctx, profile=profile,
        deliveries={"system", "prior", "user", "once", "tail"})
    system_regions = [r for r in rendered if r["delivery"] == "system"]
    instruction = next((r["payload"] for r in system_regions if r["key"] == "instruction"), "")
    content_regions = [r for r in system_regions if r["key"] != "instruction"]
    neutral = assemble_regions([r["payload"] for r in content_regions])
    if system_override is not None:
        system_prompt = system_override
    elif profile == "main":
        system_prompt = prompts.build_system_prompt(neutral, base_prompt=instruction)
    elif profile == "viewpoint":
        system_prompt = prompts.build_viewpoint_system_prompt(neutral)
    elif profile == "draft":
        system_prompt = ((system_prefix or "") + ("\n" if system_prefix and neutral else "")
                         + neutral)
    else:
        raise ValueError(f"unknown context profile: {profile}")

    prior = [m for r in rendered if r["delivery"] == "prior" for m in r["payload"]]
    user_items = [r["payload"] for r in rendered if r["delivery"] == "user"]
    user_prompt = user_items[0] if user_items else ""
    once = [m for r in rendered if r["delivery"] == "once" for m in r["payload"]]
    tail = [m for r in rendered if r["delivery"] == "tail" for m in r["payload"]]
    assembler_regions = [
        {"order": r["order"], "name": r["name"], "channel": r["channel"],
         "seats": r["seats"], "text": r["payload"], "entries": r["entries"]}
        for r in content_regions if r["source_owner"] == "assembler"
    ]
    return {
        "system_prompt": system_prompt,
        "neutral_context": neutral,
        "system_regions": assembler_regions,
        "prior_messages": prior,
        "user_prompt": user_prompt,
        "turn_once": once,
        "turn_tail": tail,
    }


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


def render_type_thinking_region(card_type: str, type_texts: dict[str, dict[str, str]]) -> str:
    """卡片思维区（order 95）：只出**本卡那一类**在 `thinking` 维度的文本。

    - 取 `type_texts["thinking"][card_type]`；**不并列别类的文本**（一类卡只读
      自己那一段）；
    - 空串/条目缺失/无 card_type/无该维度 → 空串（「没有来源就不出现这个区」）；
    - 正文原样呈现，不加工、不截断（渲染零加工）。
    """
    dim = type_texts.get("thinking") if isinstance(type_texts, dict) else None
    if not card_type or not isinstance(dim, dict):
        return ""
    text = str(dim.get(card_type) or "")
    if not text.strip():
        return ""
    return "## 卡片思维\n" + text


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


def split_rule_sections(body: str) -> dict[str, str]:
    """规则卡正文按 `## ` 区头切区：{区名: 区内容}（区内容不含区头行，末尾 rstrip）。

    纯函数：区头＝以 `## ` 开头的非空行；区头行本身不进内容。区头前若有游离
    正文（无归属行）归入 key `""`——规则面板与注入均不消费它（只作存储完整性）。
    同名区头后出现者覆盖先出现者（幂等读，不追加）。
    """
    sections: dict[str, str] = {}
    if not body:
        return sections
    current: str | None = None
    buf: list[str] = []
    for ln in body.splitlines():
        if ln.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).rstrip()
            current = ln[3:].strip()
            buf = []
        else:
            if current is None:
                current = ""
            buf.append(ln)
    if current is not None:
        sections[current] = "\n".join(buf).rstrip()
    return sections


def render_rule_region(rule_sections: dict[str, str], card_type: str) -> str:
    """85 区渲染：按当前卡 card_type 取规则卡对应区，空/缺→空串。

    带区头 `## 规则`（硬判据：render_rule_region({"sub_project":"准则A"},
    "sub_project") 含区头）。rule_sections={} 或无对应区 → ""（没有来源就不
    出现这个区）。
    """
    if not rule_sections or not card_type:
        return ""
    text = rule_sections.get(card_type)
    if not text:
        return ""
    return "## 规则\n" + text


def rule_section_names(registry: dict[str, Any]) -> list[str]:
    """规则卡合法分区名集合＝注册表全词表 codes（root 在前、去重、声明序）。

    纯函数（不做 IO）：调用方传入类型注册表（``card_store`` 的
    ``load_type_registry`` 返回值）。
    例：rd_project＋research_project＋root ⇒ ["project","sub_project","tool",
    "coder","rule","topic"]（恰六区）。注册表缺失/无词表 → 空列表。
    """
    vocabs = registry.get("l1_vocabularies") if isinstance(registry, dict) else None
    if not isinstance(vocabs, dict) or not vocabs:
        return []
    scopes = (["root"] if "root" in vocabs else []) + [k for k in vocabs if k != "root"]
    out: list[str] = []
    for scope in scopes:
        vocab = vocabs.get(scope)
        if not isinstance(vocab, dict):
            continue
        for t in (vocab.get("types") or []):
            code = str((t or {}).get("code") or "")
            if code and code not in out:
                out.append(code)
    return out


def rule_inject_counts(registry: dict[str, Any]) -> dict[str, int]:
    """每分区「正被 N 类卡注入」的静态口径：注册表内 code 出现次数。

    纯函数。例：rule 出现在 rd/research 两词表 ⇒ 2；sub_project ⇒ 1。
    """
    out: dict[str, int] = {}
    for name in rule_section_names(registry):
        out[name] = 0
    vocabs = registry.get("l1_vocabularies") if isinstance(registry, dict) else None
    if not isinstance(vocabs, dict):
        return out
    for vocab in vocabs.values():
        if not isinstance(vocab, dict):
            continue
        for t in (vocab.get("types") or []):
            code = str((t or {}).get("code") or "")
            if code in out:
                out[code] += 1
    return out


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


def _child_status_body(child: dict[str, Any]) -> str:
    """子卡「现状」正文：空/占位符（新建卡骨架模板）一律当**未写**。

    占位符判据复用 `card_draft._is_placeholder`（单一口径，避免两处各判一套）。
    延迟 import：`card_draft` 反向依赖本模块，模块级 import 会成环。
    """
    from .card_draft import _is_placeholder  # noqa: PLC0415 延迟 import 破环
    status = str((child or {}).get("status_block") or "")
    return "" if _is_placeholder(status) else status


def render_direct_child_status_region(children: list[dict[str, Any]]) -> str:
    """直接子卡现状区（order 170）：只呈现当前卡**直接**子卡的 status_block。

    - 上游 SQL 只按 `parent=?` 取一层 ⇒ **结构上不可能含孙级**；
    - 只输出现状正文，不带使命、第三块（需求文档/定论）、修改历史或影子；
    - 空/纯空白/**占位符**（新建卡骨架）都没有信息 ⇒ 跳过该子卡；全空 ⇒
      整个区不出现（沿「没有来源就不出现这个区」）；
    - 子卡顺序由上游按 card_id 固定排序；正文原样呈现，不摘要、不截断
      （渲染零加工）。
    """
    sections: list[str] = []
    for child in children or []:
        status = _child_status_body(child)
        if not status:
            continue
        card_id = str(child.get("card_id") or "")
        title = str(child.get("title") or "")
        sections.append(f"### {card_id} · {title}\n{status}")
    if not sections:
        return ""
    return "## 直接子卡现状\n" + "\n\n".join(sections)


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


def _spec_by_key(key: str) -> Optional[dict[str, Any]]:
    """按 key 反查注册项——**注册表是区的唯一声明处**（底座 2026-10-15）。

    线性扫当前 ``REGION_TABLE``、**不缓存派生索引**：注册表被整体替换（如测试
    monkeypatch、或运行时换表）后仍读到新表，不留第二份会过期的声明。
    """
    for s in REGION_TABLE:
        if s.get("key") == key:
            return s
    return None


def build_log_regions(
    system_regions: list[dict[str, Any]],
    history_messages: list[dict[str, Any]],
    turn_messages: list[dict[str, Any]],
    user_content: str,
) -> list[dict[str, Any]]:
    """T7：prompt 日志 regions 键的条目构建（只存指针与指纹，不复制正文）。

    每区一条：order/名称/通道/席位/字节数/指纹/条目数。八区口径：
    instruction＝BASE_PROMPT 指针（计字节与指纹便于对账）；card_list/current_card
    等＝system 分区渲染件；history＝对话历史投影；tool_turn＝本轮工具轮（本轮
    循环内 append 的全部消息——含首轮固化与抢救回注，与 history 合计恰好划分整条
    messages）；user_message＝用户原文。

    [底座 2026-10-15] **order／名称／通道／席位一律从 ``REGION_TABLE`` 取**，本
    函数不再另行硬编码：非 assembler 区按 key 反查（instruction/history/tool_turn/
    user_message），system 区由装配器从注册表带出后透传（其 order 是控制面覆盖后的
    **生效值**，非默认值）。改注册表即改日志，杜绝「上下文改了、日志还按旧口径记」
    这类静默不一致——日志是审计凭据，它必须跟着区的唯一声明走。

    注册项缺失（表被改坏）时**降级保审计**：``name`` 标 ``[未注册:key]``、order
    取 -1（不与任何真实 order 相撞），字节与指纹照算——绝不因声明缺失丢掉整条记录。
    """
    def _entry_from_spec(key: str, blob: str, entries: int) -> dict[str, Any]:
        spec = _spec_by_key(key)
        raw = blob.encode("utf-8")
        return {
            "order": spec["order"] if spec else -1,
            "name": spec["name"] if spec else f"[未注册:{key}]",
            "channel": spec["channel"] if spec else "",
            "seats": list(spec["seats"]) if spec else [],
            "bytes": len(raw),
            "fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
            "entries": entries,
        }

    def _entry_passed(r: dict[str, Any]) -> dict[str, Any]:
        """system 区：元数据由装配器按注册表带出，直接透传。"""
        raw = r["text"].encode("utf-8")
        return {
            "order": r["order"], "name": r["name"], "channel": r["channel"],
            "seats": list(r["seats"]), "bytes": len(raw),
            "fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
            "entries": r["entries"],
        }

    out = [_entry_from_spec("instruction", BASE_PROMPT, 1)]
    out.extend(_entry_passed(r) for r in system_regions)
    out.append(_entry_from_spec(
        "history", json.dumps(history_messages, ensure_ascii=False),
        len(history_messages)))
    out.append(_entry_from_spec(
        "tool_turn", json.dumps(turn_messages, ensure_ascii=False),
        len(turn_messages)))
    out.append(_entry_from_spec("user_message", user_content, 1))
    # 非 assembler 区的 order 现在来自注册表 ⇒ 改注册表可能打乱上面的位置序，
    # 按 order 升序收口（sort 稳定：同 order 时保持 0 → system → history →
    # tool_turn → user 的位置序＝改前口径）。
    out.sort(key=lambda e: e["order"])
    return out
