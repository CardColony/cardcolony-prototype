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
    # [常驻当前卡 2026-10-19 · permexp] 当前卡正文与影子**取数一次、常驻上下文**：
    # 模型不必每轮 read_card/read_shadow 才知道自己是什么状态（卡片是持久对象，
    # 打开就该看到自己的全部状态）。cur_body＝{mission,status_block,rulings}；
    # cur_shadow＝{progress,coordination,artifacts,iterations}。
    cur_body: dict[str, Any] = field(default_factory=dict)
    cur_shadow: dict[str, str] = field(default_factory=dict)
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
    # [依赖关联线 2026-10-16] 双向依赖关联线取数结果（装配前算好，渲染器只读）：
    #   dep_relations_out ＝ 正向（当前卡自己的 relations 条目）
    #   dep_relations_in  ＝ 反向（全库扫 target == 当前卡的条目，带 source）
    # 反向扫**零额外 IO**：在已加载的全库卡索引上算（见 context_assembler）。
    dep_relations_out: list[dict[str, Any]] = field(default_factory=list)
    dep_relations_in: list[dict[str, Any]] = field(default_factory=list)
    # [第二层区 2026-10-20 · card_004] 职能角色现状与机械信号（只对项目根卡取数；
    # 别的卡为 {}，区不出现）。取数在 context_assembler._load_role_cabinet（唯一 IO），
    # 渲染器只读它。存在理由见 render_role_cabinet_region 的 docstring。
    role_cabinet: dict[str, Any] = field(default_factory=dict)
    # [function 实体化第二步 2026-10-21 · card_031] 两个新来源，**取数在装配层**
    # （context_assembler.build_ctx），渲染器只读、绝不自己查库：
    #   function_roster ＝ 本 work 全部职能档案（function_store.list_profiles）
    #                    —— 只给项目根卡看（只有根卡能组阁/调阁）；
    #   function_meta    ＝ **当前这一回合是谁**的职能档案（含 charter 全���、
    #                    watch、speak_contract）。职能**不是卡**：它没有 cards 行、
    #                    没有正卡三块、没有影子；它靠这个字段看到「我是谁」。
    # 两者都缺省空 ⇒ 对应区输出空串（「没有来源就不出现这个区」）。
    function_roster: list[dict[str, Any]] = field(default_factory=list)
    function_meta: dict[str, Any] = field(default_factory=dict)


def _count_duty_lines(ctx: RegionCtx, text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith("- "))


def render_role_cabinet_region(ctx: RegionCtx) -> str:
    """第二层（职能角色）现状 ＋ **机械可检的局面变化** ＋ 一句中性问话。

    **为什么这个区必须存在（实测依据，不是设计偏好）**
    card_004 的组阁判断力实验（`docs/5713-组阁判断力实验-报告-20261020.md`）：
      · 零干预下根卡 45 次工具调用里一次没组阁，尽管工具在手、四个跨卡问题全部发现；
      · goal 末尾加一句「该不该有人专门盯着」，它立刻组出高质量角色（排他键唯一、
        触发线逐条、主动划界）；
      · 局面变了（新增一张冲突卡）后，它 `list_roles` → `read_card` **读过角色卡**，
        仍然不改档、不撤岗、不扩关注范围。
    ⇒ **它会设，不会调**。所以「设/调」不能只写在纪律文档里等人记得——
      要由机制在每轮把现状与变化摊到它眼前，让它不用"想起来"。

    **边界（守 D02「职能不定死」）**
    本区只给事实（阁员现状 ＋ 机械检测到的变化），**不给"你应该组阁"的指令**，
    也不给职能清单。设不设、设什么域、改不改档，是当前卡（CEO）的判断；
    判断为不需要就继续干别的，不必解释。这样机制触发不会退化成"服从测试"。
    """
    cab = ctx.role_cabinet or {}
    if not cab:
        return ""
    roles = list(cab.get("roles") or [])

    # 工作卡＝树里的非根卡、非角色卡（角色卡离树，不在此列）
    work_cards = [c for c in (ctx.cards or [])
                  if c.get("card_id") != ctx.work_card_id
                  and str(c.get("card_type") or "") != "role"
                  and str(c.get("parent") or "") != ""]
    if not roles and len(work_cards) < 2:
        return ""          # 还没到"有跨卡面"的程度，不出现

    active = [r for r in roles if r.get("duty") == "active"]
    archived = [r for r in roles if r.get("duty") == "archived"]

    lines: list[str] = ["## 第二层（职能角色）· 阁员现状与变化信号"]
    lines.append("本卡是项目根卡：组阁与调阁的通道都在你手上"
                 "（create_function / list_functions / function_watch_cards / "
                 "update_function_profile / set_function_duty）。")

    if roles:
        lines.append("")
        lines.append(f"**当前阁员 {len(active)} 人在值**"
                     + (f"（另有 {len(archived)} 人已撤岗、台账保留）" if archived else ""))
        title_of = {c.get("card_id"): str(c.get("title") or "")
                    for c in (ctx.cards or [])}
        for r in active:
            watch = r.get("watch") or []
            watch_s = ",".join(f"{w}({title_of.get(w, '?')})" for w in watch) or "（空）"
            recent = r.get("recent") or []
            if recent:
                v0 = recent[0]
                last = f"{v0.get('decision')}@{str(v0.get('ts') or '')[5:16].replace('T', ' ')}"
            else:
                last = "尚未巡检"
            lines.append(f"- {r.get('role_id')}｜域={r.get('exclusive_key') or '?'}"
                         f"｜值班={r.get('duty')}｜职责书v{r.get('charter_version')}"
                         f"｜关注={watch_s}｜最近判词={last}")

    sig: list[str] = []
    if not roles:
        sig.append(f"当前项目有 {len(work_cards)} 张工作卡，**没有任何角色在关注它们**")
    watched: set[str] = set()
    for r in active:
        watched.update(str(c) for c in (r.get("watch") or []))
    role_born = [str(r.get("created_at") or "") for r in roles if r.get("created_at")]
    born_min = min(role_born) if role_born else ""
    for c in work_cards:
        cid = str(c.get("card_id") or "")
        if cid in watched:
            continue
        created = str(c.get("created_at") or "")
        if roles and born_min and created and created > born_min:
            sig.append(f"**组阁之后新增的卡、没有角色在看**：{cid}「{c.get('title')}」"
                       f"（本卡 {created[:16].replace('T', ' ')} 建立，"
                       f"全部角色 {born_min[:16].replace('T', ' ')} 之前建立）")
        elif roles:
            sig.append(f"没有角色在看：{cid}「{c.get('title')}」")
    for r in active:
        rid = str(r.get("role_id") or "")
        if not (r.get("watch") or []):
            sig.append(f"{rid} 的关注集合为空（在值但没有观察对象）")
        recent = r.get("recent") or []
        if len(recent) >= 3 and all(str(v.get("decision")) == "silent" for v in recent):
            sig.append(f"{rid} 最近 {len(recent)} 轮判词全为 silent（可能已无触发对象）")

    if sig:
        lines.append("")
        lines.append("**机械检测到的局面变化**（事实，不是建议）")
        lines.extend(f"- {x}" for x in sig)

    lines.append("")
    lines.append("要不要改档／停职／撤岗／新设，由你按局面判断；"
                 "判断为不需要就继续当前工作，不必解释。")
    return "\n".join(lines)


# ============================================================
# [function 实体化第二步 2026-10-21 · card_031] 两个职能区
# ============================================================


def render_function_roster_region(ctx: RegionCtx) -> str:
    """93 区渲染（现有职能）：**只对项目根卡出**，一行一个职能。

    为什么单独占一个区：91 区（card_029）只给「职能该按什么标准定」的判断标准，
    是**没有存量**时的引导；本区给的是**已经建了哪些职能**这个事实——根卡要调阁
    （改档／撤岗／扩关注）就得先看得见现状，否则和没建一样（实测「会设不会调」，
    依据见 render_role_cabinet_region 的 docstring）。

    判据（两条同时成立才出，fail-open）：
      1. 当前卡是项目根卡：``cur_meta.card_id``（无则退回 ``ctx.card_id``）
         等于 ``ctx.work_card_id``——只有根卡能组阁/调阁，给别的卡既无用也是噪音；
      2. 本 work 至少有一个职能（``function_roster`` 非空）。
    一个都没有 ⇒ 空串（「没有来源就不出现这个区」）——**不催建、不编造职能**。

    纯函数：数据由 context_assembler.build_ctx 一次取好（function_store.
    list_profiles），渲染器只读、自己不查库。
    """
    meta = ctx.cur_meta or {}
    cur_card_id = str(meta.get("card_id") or ctx.card_id or "")
    if not cur_card_id or not ctx.work_card_id:
        return ""
    if cur_card_id != str(ctx.work_card_id):
        return ""                       # 非根卡一律空串
    roster = [f for f in (ctx.function_roster or []) if isinstance(f, dict)]
    if not roster:
        return ""                       # 零职能＝没有来源＝不出这个区
    lines: list[str] = ["## 现有职能"]
    for f in roster:
        fid = str(f.get("function_id") or "")
        name = str(f.get("function_name") or "")
        jur = f.get("jurisdiction") or {}
        stmt = str(jur.get("statement") or jur.get("domain") or "（未写职责说明）")
        watch_cards = (f.get("watch") or {}).get("cards") or []
        duty = str((f.get("duty") or {}).get("state") or "active")
        lines.append(f"- {name}（{fid}）：{stmt}"
                     f"｜盯 {len(watch_cards)} 张卡｜值班 {duty}")
    return "\n".join(lines)


def render_function_self_region(ctx: RegionCtx) -> str:
    """145 区渲染（你的职能）：职能**自己**回合的「我是谁」。

    职能不是卡 ⇒ 没有 cards 行、没有正卡三块、没有影子、也不出现在卡片清单里。
    以前角色卡靠「自己是张卡」天然看到自己的身份；职能把这层皮剥掉之后，
    **必须由机制显式把档案摊给它**，否则它不知道自己是谁、也接不到活。
    本区就是那个机制：只讲事实（我是谁／职责书／盯谁／什么时候开口），
    末尾一句把它的行为边界说死——不干活、只盯、该开口才开口。

    ``ctx.function_meta`` 为空（缺省／取数失败）⇒ 输出空串，**fail-open 不抛**
    （「没有来源就不出现这个区」）。
    """
    meta = ctx.function_meta
    if not isinstance(meta, dict) or not meta:
        return ""
    fid = str(meta.get("function_id") or "")
    name = str(meta.get("function_name") or "")
    if not fid and not name:
        return ""                       # 没有可辨识的身份 ⇒ 不出这个区

    lines: list[str] = ["## 你的职能", f"你是 {name or fid}（{fid}）"]

    charter = str((meta.get("charter") or {}).get("text") or "").strip()
    if charter:
        lines += ["", "**职责书**", charter]

    watch = (meta.get("watch") or {})
    watch_cards = [str(c) for c in (watch.get("cards") or [])]
    lines += ["", "**你盯的卡**"]
    lines += [f"- {c}" for c in watch_cards] or ["- （空：你还没有盯任何卡）"]

    sc = meta.get("speak_contract") or {}
    advice = [str(x) for x in (sc.get("advice_when") or [])]
    alarm = [str(x) for x in (sc.get("alarm_when") or [])]
    lines += [
        "",
        "**你什么时候开口**",
        f"- 建议（advice_when）：{'；'.join(advice) if advice else '（未设）'}",
        f"- 警报（alarm_when）：{'；'.join(alarm) if alarm else '（未设）'}",
        "",
        "你不在任务树里，不干活，只盯；该开口时才开口，否则沉默。",
    ]
    return "\n".join(lines)



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
        "source": "prompts.select_base_prompt（BASE_PROMPT / AUTO_BASE_PROMPT 常量，逐字引用不复制——区只持指针）",
        "render": lambda ctx: prompts.select_base_prompt(ctx.cur_meta),
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
        # [职能组建区 2026-10-21] order 91：紧跟 90 卡片职责之后、92 卡片能力之前。
        # 只对**项目根卡 ＋ 自动模式**注入；非根卡 / 非自动模式 → 空区不出现。
        # 只给判断标准，**不建职能表、不做白名单校验**（守 D02「职能不定死」）。
        "key": "team_function_instruction",
        "order": 91,
        "name": "组建职能",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "逐字常量（无外部来源）：职能判断标准，仅根卡自动模式出区",
        "render": lambda ctx: render_team_function_instruction_region(ctx),
        "count": 1,
    },
    {
        # [卡片能力区 2026-10-21] order 92：紧随 90 卡片职责之后、95 卡片思维之前。
        # 90 讲「这类卡是什么」，本区讲「**你这张卡**手上有什么、动手前先想什么」。
        # 来源＝卡自己的 perms（建卡时写死在卡上，不查配置、不看上级任务书）
        # ⇒ 能力由系统授予，不依赖上级是否记得往下传（实测 30+ 张子卡 0 张被告知）。
        "key": "card_ability",
        "order": 92,
        "name": "卡片能力",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "当前卡 meta.perms（建卡时按 config/permissions.json 算一次写死）",
        "render": lambda ctx: render_card_ability_region(ctx),
        "count": 1,
    },
    {
        # [function 实体化第二步 2026-10-21 · card_031] order 93：在 92 卡片能力
        # 之后、95 卡片思维之前。**只对项目根卡出**（只有根卡能组阁/调阁）。
        # 与 91 区（card_029）的分工：91 给「职能该按什么标准定」的判断标准
        # （没有存量时的引导、逐字常量），本区给**已建了哪些**的事实（可调可撤）。
        # 零职能 ⇒ 空串（不催建、不编造，守 D02「职能不定死」）。
        "key": "function_roster",
        "order": 93,
        "name": "现有职能",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main",),
        "source": "function_store.list_profiles（装配层一次 read_sync，仅项目根卡）",
        "render": lambda ctx: render_function_roster_region(ctx),
        "count": lambda ctx, text: len(ctx.function_roster or []),
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
        # [function 实体化第二步 2026-10-21 · card_031] order 145：在 100 卡片清单
        # 之后、150 当前卡之前——「我是谁」紧跟在「这棵树上有哪些卡」之后，
        # 早于「当前卡」。职能**不是卡**，所以 100 区不会有它的一行、150 区也没有
        # 它的正文三块：本区是它唯一的身份来源（function_meta 由装配层取好）。
        "key": "function_self",
        "order": 145,
        "name": "你的职能",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main",),
        "source": "function_store.get_profile（装配层取好当前这一回合所属职能的档案）",
        "render": lambda ctx: render_function_self_region(ctx),
        "count": lambda ctx, text: 1 if text else 0,
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
        "source": "当前卡身份 ＋ 正文三块 ＋ 影子三节（常驻；取数在装配层一次完成）",
        "render": lambda ctx: render_current_card_region(ctx),
        "count": 1,
    },
    {
        # [自动模式 2026-10-17 · 回灌 5700 块0.4] order 152：紧随 150 当前卡——
        # 「我是谁」之后立刻讲「我在追什么目标」。**只有 auto_mode=1 的卡有内容**
        # （渲染器对非 auto 卡返回空串 ⇒ 按「没有来源就不出现这个区」自然不出现）。
        # 渲染 当前目标 / 目标态 / 轮次计数（模型得知道自己在第几轮、目标是什么）。
        # 自主推进的**指令**在 AUTO_BASE_PROMPT 的「## 自动模式」节（本节只给事实）。
        "key": "goal",
        "order": 152,
        "name": "自动模式·目标",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "当前卡 meta 的 auto_mode/goal/goal_state/goal_rounds（非 auto ⇒ 空串）",
        "render": lambda ctx: render_goal_region(ctx.cur_meta),
        "count": lambda ctx, text: 1 if text else 0,
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
        # [第二层区 2026-10-20 · card_004] order 175：紧跟 170 直接子卡现状之后——
        # 170 讲「作业树现在什么样」（做事的那层），本区讲「职能层现在什么样、变了什么」。
        # **只对项目根卡出现**（取数层已按 card_id == work_card_id 过滤）：
        # 只有根卡能组阁/调阁，把阁员状态摊给别的卡既无用也是噪音。
        # 实测依据见 render_role_cabinet_region 的 docstring（会设、不会调）。
        "key": "role_cabinet",
        "order": 175,
        "name": "第二层·职能角色",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main",),
        "source": "role_profiles ＋ role_verdicts（装配层一次 read_sync，仅根卡）",
        "render": lambda ctx: render_role_cabinet_region(ctx),
        "count": lambda ctx, text: len((ctx.role_cabinet or {}).get("roles") or []),
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
        # [依赖关联线 2026-10-16] order 250：200 记忆之后、300 对话历史之前
        # （用户裁定 6「位置不能太靠上」）。渲染当前卡的**双向**依赖关联线：
        # 正向（我依赖/关联谁）＋反向（谁依赖/关联我）；无线 → 空串（沿用
        # 「没有来源就不出现这个区」）。
        # ★ seats 取 main only，且 **profiles 也只取 main**——本项目真正的消费者
        #   闸门是 profiles（seats 不参与过滤，实测「记忆区」标 seats=main 但
        #   profiles 三席，照样进 viewpoint）。要真做到「三席零变化」，必须
        #   profiles=("main",)。（见施工回报：任务书字面写三席，与硬判据4 冲突，
        #   本处按硬判据4 取 main-only。）
        "key": "relations",
        "order": 250,
        "name": "依赖关联",
        "channel": "system",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main",),
        "source": "当前卡的依赖关联线（relations）：正向＝本卡自己的条目；反向＝全库卡索引扫 target==本卡（零额外 IO）",
        "render": lambda ctx: render_dep_relations_region(
            ctx.dep_relations_out, ctx.dep_relations_in),
        "count": lambda ctx, text: len(ctx.dep_relations_out) + len(ctx.dep_relations_in),
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
        "render": lambda ctx: render_judgment_region(ctx),
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


def _heal_tool_pairs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具块配对自愈（N1 红线：不得出现孤立 tool 块）。

    [工具结果落会话 2026-10-17] 两处会打断配对：
    ① 凝练水位 mark 恰好切在「块 A（assistant+tool_calls）」与其「块 B（tool）」
       之间 ⇒ 投影从 B 起 ⇒ 孤立 tool；
    ② 调用方 allowed_roles 排除 "tool"（如 card_draft 只收 user/assistant）
       ⇒ A 的 tool 结果被滤掉 ⇒ A 的 tool_calls 无对应 tool 消息。
    两者都会让端点 400（role=tool 必须紧跟其配对的 assistant(tool_calls)）。

    自愈规则：
    - assistant(tool_calls) 的**全部** tool_call_id 都有对应 tool 消息在场
      → 整组保留；否则**剥离该 assistant 块的 tool_calls**（保留其 content
      正文；content 为空则整块丢弃——空 assistant 块进请求无意义）；
    - tool 消息若其 tool_call_id 不在任何「保留的 assistant(tool_calls)」里 → 丢弃。
    """
    tool_ids_present = {str(it.get("tool_call_id") or "")
                        for it in items if it.get("role") == "tool"}
    out: list[dict[str, Any]] = []
    kept_call_ids: set[str] = set()
    for it in items:
        role = it.get("role")
        if role == "assistant" and it.get("tool_calls"):
            ids = [str(tc.get("id") or "") for tc in it["tool_calls"]]
            if ids and all(i in tool_ids_present for i in ids):
                kept_call_ids.update(ids)
                out.append(it)
            else:
                # 配对不全：剥离 tool_calls，仅在有正文时保留为普通 assistant 块
                content = str(it.get("content") or "")
                if content.strip():
                    out.append({k: v for k, v in it.items() if k != "tool_calls"})
        elif role == "tool":
            if str(it.get("tool_call_id") or "") in kept_call_ids:
                out.append(it)
        else:
            out.append(it)
    return out


def project_history_messages(
    blocks: list[dict[str, Any]], mark: int | None = None,
    allowed_roles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """历史区的统一投影：按凝练水位切片、滤过程块、转 card-msg 头，保留思考回传。

    这是纯函数；卡片会话文件不改。mark 越界时回退全量，与旧投影口径一致。

    [工具结果落会话 2026-10-17] 放行工具块（对齐通用 agent 的对话形态）：
    - actor=assistant 且带 tool_calls → {"role":"assistant","content":…,"tool_calls":[…]}
    - actor=tool → {"role":"tool","tool_call_id":…,"content":…}
    其余不变（reasoning_content 保留、卡间块换头）。**工具结果块 behavior=""
    必须放行（模型要看）；环节留痕块（triage/plan/step/final/revision）behavior
    非空仍被上面 ``if m.get("behavior")`` 滤掉——这条区分不得改**（见 §2.3）。
    出口经 ``_heal_tool_pairs`` 配对自愈（mark 切在 A/B 之间或 allowed_roles
    排除 tool 时，避免出现孤立 tool 块 → 端点 400）。
    """
    source = blocks or []
    if mark is not None:
        source = source[mark:] if 0 <= mark <= len(source) else source
    out: list[dict[str, Any]] = []
    from . import card_msg
    for m in source:
        if m.get("behavior"):
            # 环节留痕块（behavior 非空）不进投影；工具结果块 behavior="" 放行。
            continue
        role = str(m.get("role") or "")
        if allowed_roles is not None and role not in allowed_roles:
            continue
        actor = str(m.get("actor") or "")
        item = {
            "role": role,
            "content": (card_msg.render_for_context(m["content"])
                        if actor.startswith("card:")
                        else m["content"]),
        }
        if actor == "assistant" and m.get("tool_calls"):
            item["tool_calls"] = m["tool_calls"]
        if actor == "tool":
            item["tool_call_id"] = m.get("tool_call_id") or ""
        if m.get("reasoning_content"):
            item["reasoning_content"] = m["reasoning_content"]
        out.append(item)
    return _heal_tool_pairs(out)


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


# ============================================================
# [职能组建区 2026-10-21 · order 91]
# ============================================================
# 这个区**只给判断标准，不给职能清单**（守 D02「职能不定死」）：
#   职能名、设几个、盯什么卡，全部由模型看着眼下这件事自己判断；
#   系统**不建职能表、不做白名单/枚举校验**，职能名对不对由它自己负责。
# 为什么值得单独占一个区：写进 goal 里靠自觉，且只在「本轮目标恰好提到」时生效；
# 而组阁恰恰是根卡「每一轮的第一件事」，漏一轮就漏一整段盯防。
# 边界：本区**不做轮次闸门**（不判「有没有职能」「建过没有子卡」）
#       —— 那是别的卡的事；这里只回答「职能该按什么标准定」。
_TEAM_FUNCTION_INSTRUCTION_TEXT = """## 组建职能（你这一轮的第一件事）

在动手做事之前，先把这件事需要的**职能**建出来。

**职能＝人类团队里真实存在的岗位。** 产品、架构、研发、质量、安全、运维、数据、体验、交付……这些只是让你理解什么叫职能，**不是清单，也不许照抄**。它该是什么，由你看着眼下这件事自己判断。

判断标准只有一条：**如果这个职能在你熟悉的人类团队里找不到对应的人，就不要建它。**

- 不要把「任务」当职能（「写文档」是任务，不是职能）。
- 不要把「模块」当职能（「存储层」是模块，不是职能）。
- 不要为了凑数编一个。想不出该设什么职能，就直说这件事不需要专职职能、理由是什么——那比编一个好。

职能不挂在你的任务树下，它不干活，只盯。你想清楚要盯什么、什么情况下它该开口，再建。"""


def render_team_function_instruction_region(ctx: RegionCtx) -> str:
    """91 区渲染（组建职能）：**只有项目根卡 ＋ 自动模式**才出，逐字常量注入。

    两条渲染条件同时满足才输出，否则空串（fail-open，读不到就不出这个区）：
      1. 当前卡是项目根卡：`cur_meta.card_id == work_card_id`
         （`cur_meta` 里没有 card_id 时退回用 `ctx.card_id` 比）；
      2. 自动模式：`bool(cur_meta.auto_mode)`。
    故意 fail-open：这两项读不到（空 dict／坏数据）时宁可**不出**，
    也不猜「大概它是根卡吧」—— 猜错会给非根卡注入组织指令。
    """
    meta = ctx.cur_meta or {}
    cur_card_id = str(meta.get("card_id") or ctx.card_id or "")
    if not cur_card_id or cur_card_id != str(ctx.work_card_id or ""):
        return ""
    if not bool(meta.get("auto_mode")):
        return ""
    return _TEAM_FUNCTION_INSTRUCTION_TEXT


# ============================================================
# [卡片能力区 2026-10-21 · 洞察来自 card_006 研究 + 用户裁定]
# ============================================================
# 为什么要有这个区（实测依据，不是设计偏好）：
#   v1–v9 共 30+ 张子卡，**没有一张**被上级告知「你可以建自己的子卡 / 写自己的
#   定论 / 发消息」。这项知识一直只写在「给根卡的那段提示」里，要不要往下传
#   全靠上级自觉 ⇒ 实测 0/30 传下去了。子卡不知道该能干什么，于是只执行；
#   上级以为子卡只能执行，于是把判断全做完再派活 —— 「子卡被用成子 agent」
#   就是这么生产出来的。
#   ⇒ 修法不是继续教上级怎么写任务书（那要靠自觉），而是**系统按每张卡自己的
#   perms 直接注入**：上级写不写都不影响；执行体也照实看到「你没有建卡权」。
# 与 90 区（卡片职责）的分工：
#   90 讲「这类卡在系统里是什么」（类型级职责，来自注册表）；
#   本区讲「**你这张卡**手上有什么、动手前该先想什么」（实例级能力，来自 perms）。
# 空来源（读不到 perms）→ 只出纪律那一段，不编造能力。
_ABILITY_SWITCH_LINES: tuple[tuple[str, str], ...] = (
    ("create_child", "建自己的子卡"),
    ("edit", "改自己的正文、写自己的定论"),
    ("send_card_message", "发卡间消息（先建通信线）"),
    ("read_linked", "沿通信线读对方卡"),
    ("link", "建线/解线"),
    ("plan", "建计划并推进步骤"),
    ("tools_file", "读写文件、跑命令"),
    ("tools_web", "联网查证"),
    ("manuscript", "查本项目存档资料"),
    ("invoke_tool", "调用工具卡"),
    ("delete", "删卡"),
)

_RULE_TAIL = (
    "**动手之前先想清楚：这件事该怎样拆解才合理** —— 要不要拆、拆成几块、"
    "块与块之间是什么关系。"
)

_DISCIPLINE = (
    "- 你负责范围内的判断由**你自己下**，写进自己的定论块；不要等上级把判据替你定完。"
)

# [2026-10-21 修正] 「还没拆过」是**提示**，不是闸门。
# 用户裁定：第一轮只做组织过于机械——一张卡面对的第一轮完全可能是对话、
# 讨论、澄清，这时该照常回答，不该被逼着拆。所以这里只把事实说出来，
# 由它自己判断这一轮要不要拆；工具面不动。
_NOT_SPLIT_HINT = (
    "- 你负责的这件事**还没拆过**。如果它需要拆（分得开、能各自负责到底），"
    "先按职责把卡建好、把每张卡的使命写清，再动手；"
    "如果这一轮只是讨论、澄清或问情况，就照常回答——**不必为了拆而拆**。"
)


def render_card_ability_region(ctx: RegionCtx) -> str:
    """92 区渲染（卡片能力）：把「这张卡手上有什么」按**它自己的 perms** 渲染。

    三条硬规矩（改这个区时不要破坏）：
    1. **来源是卡自己的 perms**（建卡时按 permissions.json 算一次写死在卡上），
       不是卡类型猜的、不是上级任务书里写的 ⇒ 上级传不传都不影响。
    2. 能力必须**照实**：没有 create_child 就明说没有（执行体不该被暗示能组织），
       有就明说可以、并明说「不必等上级替你想好怎么分」。
    3. perms 读不到（旧卡/坏数据）→ 不编造能力，只出纪律段（fail-open 不阻断装配）。
    """
    meta = ctx.cur_meta or {}
    card_type = str(meta.get("card_type") or "")
    card_id = str(meta.get("card_id") or ctx.card_id or "")
    perms = meta.get("perms")
    has = set(perms) if isinstance(perms, list) else None

    head = ["## 你的能力（系统按卡类型授予，与上级怎么写你的任务书无关）"]
    if card_type or card_id:
        head.append(f"你是 `{card_id}`，卡类型 `{card_type}`。")
    head.append("下面这些能力是**系统直接给这张卡**的；上级写不写进任务书都不增减，"
                "也不用去问上级要。")

    lines: list[str] = list(head)
    # [拆分流程 2026-10-21] 使命还是模板占位 => 直接告诉它去要，别自己猜。
    # 实测背景：使命通道打通前，子卡拿到占位符模板却照样开工，
    # 于是只能从 goal 反推自己该干什么——那正是「子 agent」的由来。
    _mission_txt = str(meta.get("mission") or "").strip()
    if (not _mission_txt) or _mission_txt.startswith("（本卡作用/目标"):
        lines.append(
            "- **你的使命尚未设定**（现在是模板占位）。先向上级要清「你负责哪一块、"
            "负责到什么程度」，不要从目标反推、也不要自己猜。"
        )
    if has is not None:
        if "create_child" in has:
            lines.append(
                "- **建自己的子卡：可以。** 你负责的这一块如果还能再分"
                "（几个可独立推进的部件、几条互不依赖的活），就自己建子卡，"
                "**不必等上级替你想好怎么分**；建出来的卡同样拿到这份能力说明。")
        # [2026-10-21 修正] 还没拆过 ⇒ 把事实说出来，让它自己判断这一轮要不要拆。
        # 不做工具面闸门（那会挡住「第一轮其实是讨论」的正常回合）。
        # 判据：有建卡权（能拆）＋ 系统里还没有它的直接子卡（没拆过）＋ 自动模式卡。
        _has_child_perms = isinstance(has, set) and "create_child" in has
        _no_children = not (ctx.direct_child_status or [])
        if _has_child_perms and _no_children and bool(meta.get("auto_mode")):
            lines.append(_NOT_SPLIT_HINT)

        else:
            why = ("你是执行体：一次一件事、做完即完工、不长树"
                   if card_type == "coder" else "本卡类型不长树")
            lines.append(
                f"- **建自己的子卡：没有这个能力**（{why}）。"
                "这一块如果需要再分，是派你活的上级该解决的问题——把你看到的"
                "分工问题如实报上去，不要自己硬扛。")
        for sw, label in _ABILITY_SWITCH_LINES:
            if sw == "create_child" or sw not in has:
                continue
            lines.append(f"- {label}：可以。")
        missing = [label for sw, label in _ABILITY_SWITCH_LINES
                   if sw != "create_child" and sw not in has]
        if missing:
            lines.append(f"- 没有：{'、'.join(missing)}。")
    else:
        lines.append("- （系统未读到本卡的权限表，能力以系统提示为准。）")

    lines.append(_DISCIPLINE)
    lines.append(_RULE_TAIL)
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


def render_current_card_region(ctx: "RegionCtx") -> str:
    """150 区渲染：当前卡身份 ＋ 正文三块 ＋ 影子三节（**常驻**）。

    [常驻当前卡 2026-10-19 · permexp] 用户令：卡片自己的现状与影子必须在上下文里，
    不能让模型每轮靠 read_card/read_shadow 去拉——卡片是持久对象，打开就该看见自己
    的全部状态。取数在装配层一次完成（RegionCtx.cur_body / cur_shadow），本函数纯渲染。

    裁剪口径：影子只出**产物清单 / 待协作事项 / 进度**三节；「迭代历史」是系统机械
    留痕（155 区已告知模型"你不用写"），且随时间无限增长，常驻只会挤占上下文。
    空块不出现（没有内容就不占篇幅）；整卡无正文时只出身份行。
    """
    cur = ctx.cur
    if cur is None:
        return ""
    # 延迟 import：card_draft 反向依赖本模块，模块级 import 会成环（同 _child_status_body）。
    from .card_draft import _is_placeholder  # noqa: PLC0415

    third = _THIRD_BLOCK_NAME_BY_TREE.get(ctx.project_class or "", "定论")
    body = ctx.cur_body or {}
    lines = ["## 当前卡", f"{cur['card_id']} · {cur['title']}"]

    for name, key in (("使命", "mission"), ("现状", "status_block"), (third, "rulings")):
        text = str(body.get(key) or "").strip()
        # 骨架模板（「（本卡作用/目标…）」这类）不是内容：常驻进去会让模型
        # 把模板当既有事实照抄（实测 system prompt 里整段模板原样出现）。
        if text and not _is_placeholder(text):
            lines += ["", f"### {name}", text]

    shadow = ctx.cur_shadow or {}
    sh_lines: list[str] = []
    for name, key in (("产物清单", "artifacts"), ("待协作事项", "coordination"),
                      ("进度", "progress")):
        text = str(shadow.get(key) or "").strip()
        if text and not _is_placeholder(text):
            sh_lines += [f"【{name}】", text, ""]
    if sh_lines:
        lines += ["", "### 影子（本卡私有，别的卡读不到）", *sh_lines[:-1]]

    return "\n".join(lines)


def render_judgment_region(ctx: RegionCtx) -> list[dict[str, Any]]:
    """1000 区渲染：判断协议注入（一次性、不进轮槽）。

    ★ [自动模式 2026-10-17 · 回灌 5700 块0.4 · TC5] **auto 卡不注入判断协议**。
      TC5 口径：auto 卡已有 goal，再判「直答还是上板」会跟 goal 打架；故只对
      auto 卡屏蔽判断协议。非 auto 卡**逐字不变**（仍原样返回 ``request_once``）
      ——ND1 红线 11：全局思维链不进生产拆除名单，屏蔽面只有 auto 卡。
    """
    # [纯单模型 2026-10-17] 关链：判断协议一律不注入
    from ..thinkchain import enabled as _tc_enabled
    if not _tc_enabled():
        return []
    if bool((ctx.cur_meta or {}).get("auto_mode")):
        return []
    return ctx.request_once


def render_goal_region(meta: dict[str, Any] | None) -> str:
    """152 区渲染（自动模式 · 目标）：当前目标 / 目标态 / 轮次计数。

    **只给事实、不给指令**——自主推进的指令在 AUTO_BASE_PROMPT 的「## 自动模式」节。
    非 auto 卡 ⇒ 空串（「没有来源就不出现这个区」）；故本区对非 auto 卡零影响。
    """
    m = meta or {}
    if not bool(m.get("auto_mode")):
        return ""
    goal = str(m.get("goal") or "").strip()
    state = str(m.get("goal_state") or "").strip()
    try:
        rounds = int(m.get("goal_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return "\n".join([
        "## 自动模式 · 目标",
        f"- 目标：{goal or '（尚未设定——本卡收到的首条消息即成为目标）'}",
        f"- 目标状态：{state or '（未设定）'}",
        f"- 已跑轮次：{rounds}",
    ])


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
            # [角色机制 5710] 角色卡不在规则卡分区体系内（行为由根卡写的职责书
            # 约束，走专用 ROLE_CARD_BASE_PROMPT），不计入规则六区。
            if code and code != "role" and code not in out:
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
        "定论——本课题已经作出的决定与裁定（定了什么、为什么这么定）。\n"
        "      写法：只追加、不删不改；一条一行，写清“决定了什么、依据是什么”；\n"
        "            某条失效就在原处标一行“→ 被Dxx取代”，不保留原文。\n"
        "      判据：只能被接受或推翻的（取舍、方案选择、边界划定）写这里；\n"
        "            能被别人拿去验证的事实不写这里，写现状。"
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
        f"现状——{subject}现在实际是什么样（事实，不是打算）。",
        f"      别的卡读{'根卡' if is_root else '这张卡'}，先读的就是它——"
        "所以这里只写别人能拿去验证的东西：",
        "      · 已经做成了什么：交付物在哪、能跑的命令、跑出什么结果；",
        "      · 对外接口：提供什么，输入输出、调用方式、约束是什么；",
        f"      · 现在卡在哪：缺什么、在等什么（依赖哪些卡、变更会波及谁）。",
        "      写法：整块覆盖、只保留最新一份；只写能核实的事实；",
        "            不写计划、不写「我打算怎么做」、不写过程与流水账。",
        f"      判据：能被别人拿去验证的，写这里；只是「定了什么、要什么」"
        f"而无法验证的，写{third}。",
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


def _rel_suffix(kind: str, detail: str) -> str:
    """依赖关联条目的括号内文案：``{kind}·{detail}``（缺项则不写）。"""
    return "·".join(p for p in (kind, detail) if p)


def render_dep_relations_region(
    out_entries: list[dict[str, Any]],
    in_entries: list[dict[str, Any]],
) -> str:
    """250 区渲染（依赖关联）：当前卡的**双向**依赖关联线。

    - 正向：本卡自己的 ``relations`` → 「我依赖/关联：{target}（{kind}·{detail}）」；
    - 反向：全库扫出的 ``target == 本卡`` 条目 → 「依赖/关联我的：{source}（…）」；
    - 两向都无 → 空串（「没有来源就不出现这个区」）；
    - 条目原样呈现（kind/detail 不加工、不截断；渲染零加工）。

    纯函数：不做 IO（模块头硬约束）。双向取数由 ``context_assembler.build_ctx``
    在已加载的全库卡索引上算好（零额外 IO）后传入。
    """
    lines: list[str] = []
    for r in out_entries or []:
        if not isinstance(r, dict):
            continue
        target = str(r.get("target") or "")
        if not target:
            continue
        lines.append(
            f"- 我依赖/关联：{target}（{_rel_suffix(str(r.get('kind') or ''), str(r.get('detail') or ''))}）")
    for r in in_entries or []:
        if not isinstance(r, dict):
            continue
        source = str(r.get("source") or "")
        if not source:
            continue
        lines.append(
            f"- 依赖/关联我的：{source}（{_rel_suffix(str(r.get('kind') or ''), str(r.get('detail') or ''))}）")
    if not lines:
        return ""
    return "## 依赖关联\n" + "\n".join(lines)


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
