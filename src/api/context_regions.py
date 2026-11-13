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
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from ..storage import rule_store
from . import prompts
from .prompts import BASE_PROMPT, THIRD_BLOCK_NAME_BY_TREE as _THIRD_BLOCK_NAME_BY_TREE

logger = logging.getLogger("api.context_regions")


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
    # 打开就该看到自己的全部状态）。cur_body＝{mission,status_block,requirements}；
    # cur_shadow＝{progress,coordination,artifacts,iterations}。
    cur_body: dict[str, Any] = field(default_factory=dict)
    cur_shadow: dict[str, str] = field(default_factory=dict)
    #: [harness 提示词注入 2026-10-28 · 第三道接口] 本轮的注入提示（装配时取走并清空）。
    #: 只在**这一轮**的 system 里出现：整轮共用、不进会话历史、不唤醒。
    pending_inject: list[str] = field(default_factory=list)
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
    #: [缓存板 2026-11-12] 运行时附加块（prior_extra，评审现场发言）——
    #: 它们是**别的卡的会话块**（带真 seq，不能靠 seqless 与 history_source
    #: 区分），独立通道传入；不属于原文库、不进板、不受水位约束，
    #: 板路照旧接在板行之后（旧路混在 history_source 里语义不变）。
    history_extra: list[dict[str, Any]] = field(default_factory=list)
    #: [本轮消息双重计数 2026-11-14 · 用户实录] 本轮 user 块的 pair_id。
    #: 写钩在落盘瞬间已把它放进板 ⇒ 装配读板时会把它当**历史**又端一遍，
    #: 同一条消息在 prior 与 user_prompt 各出现一次（"你粘贴了两遍"）。
    #: 板路按它滤掉本轮已落块的行；空串＝无本轮块可滤（评审/草稿路）。
    current_pair_id: str = ""
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
    # [上游需求文档 2026-11-10 · 用户令「需求文档是它们的参照物，应直接进入
    # 上下文」] 执行/差事卡（ui/worker/qa_card/pm_card）的参照尺投影：
    #   ui/worker → 父卡；qa/pm → 父卡（派遣方）＋对象卡（差事账 target_card）。
    # 取数在 context_assembler._load_upstream_requirements（类型闸外零 IO），
    # 渲染器只读。别的卡型恒空 ⇒ 区不出现。
    upstream_requirements: list[dict[str, Any]] = field(default_factory=list)
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
                       f"（本卡 {created.replace('T', ' ')} 建立，"
                       f"全部角色 {born_min.replace('T', ' ')} 之前建立）")
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
    本区就是那个机制：只讲事实（我是谁／职责书／我的专业准则／在派差事），
    末尾一句把它的行为边界说死——不干活、只在被叫到时在场。

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

    # [两源模型 2026-11-01] 职能的判断依据只有两件：被验卡的需求文档（场卡的面，
    # 在上下文别处）＋自己的专业准则（rules.md 的 `## function`/「## function.
    # <code>」区，在这里注入）。准则写进**规则文件**——它是项目基建、人（用户/
    # 根卡提议+用户确认）才能改，不是职能私记的台账：准则要被裁定过才算数。
    code = str(meta.get("catalog_code")
               or (meta.get("jurisdiction") or {}).get("catalog_code") or "")
    shared = str(ctx.rule_sections.get("function") or "").strip()
    own = (str(ctx.rule_sections.get(f"function.{code}") or "").strip()
           if code else "")
    if shared or own:
        lines += ["", "**你的规则**（rules.md——你只有读的份；要加要改说给"
                  "叫你/找你的人，由人去改）"]
        if shared:
            lines += ["### 全体职能", shared]
        if own:
            lines += [f"### 只对你（function.{code}）", own]

    # [职能唤醒收口 2026-10-28 → 2026-11-01] watch/speak_contract 两段是自动
    # 唤醒时代的档案字段——现在不存在「盯到信号自动醒」，渲染它们只会教错
    # 在场方式。被用到的通道：① 差事派你 ② 评审叫你 ③ 用户直接找你。
    _is_ceo = code == "ceo"
    boundary = ("你是总裁：统筹决策、跨卡引导块执笔、对用户汇报与确认；"
                "不执行具体工作——执行下树。") if _is_ceo else (
        "你不在任务树里，不干具体的活。你只在被叫到时在场："
        "差事派你、评审叫你、用户直接找你；其余时候你不在场。")
    lines += ["", boundary]
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
def render_pending_inject(items: list[str]) -> list[dict[str, Any]]:
    """把待注入提示渲染成**当轮消息**（once 通道），不是 system 文本。

    [2026-10-28 用户口径]「所有注入都要在当轮的位置」——注入和
    ``context_preamble`` / ``context_injection`` 是同一类东西：进当前轮消息序列，
    不进 system、不进历史。抬头用〔本轮提示〕标明来源，角色沿用 assistant
    （与既有注入一致，不冒充用户发言）。
    """
    clean = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not clean:
        return []
    body = "\n".join(f"- {x}" for x in clean)
    # 角色沿用 assistant（与 context_preamble/context_injection 一致，
    # 不冒充用户发言）；常量在 current_turn，这里用字面量避免循环导入。
    return [{"role": "assistant",
             "content": "〔本轮提示〕\n" + body}]


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
        "render": lambda ctx: prompts.select_base_prompt(ctx.cur_meta, ctx.project_class),
        "count": 1,
    },
    {
        # [harness 提示词注入 2026-10-28 · 用户定义第三道接口] order 88：
        # 规则注入之后、卡片职责之前——它是"这一轮额外要它注意的事"，
        # 排在规则之后、任务之前最自然。
        # 只在 main 席位出现（三席是思考席，不需要被注入打断）。
        "key": "inject",
        "order": 88,
        "name": "本轮提示",
        "channel": "messages",
        "seats": ("main",),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "once",
        "profiles": ("main",),
        "source": "cards.pending_inject（harness 注入队列，装配时取走并清空）",
        "render": lambda ctx: render_pending_inject(ctx.pending_inject),
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
        "render": lambda ctx: render_card_list_region(
            ctx.cards, ctx.work_card_id, ctx.card_id,
            [{"card_id": str(r.get("target") or ""), "dir": "out",
              "kind": str(r.get("kind") or ""), "detail": str(r.get("detail") or "")}
             for r in (ctx.dep_relations_out or [])]
            + [{"card_id": str(r.get("source") or ""), "dir": "in",
                "kind": str(r.get("kind") or ""), "detail": str(r.get("detail") or "")}
               for r in (ctx.dep_relations_in or [])]),
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
        # [2026-10-30 用户口径「评审卡是子卡的同构上下文，只是几个区域做替换」]
        # 评审席（viewpoint）和改稿（draft）也要出这一区——它们的「我是谁」
        # 由 function_meta 显式给（评审职责书 / 子卡自己的档案）。
        "profiles": ("main", "viewpoint", "draft"),
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
        # [上游需求文档 2026-11-10 · 用户令] order 160：紧随 155 区域说明——
        # 「我是谁/我在追什么」之后先给「我拿什么尺干活」，再看子卡。
        # **只对执行/差事卡（ui/worker/qa_card/pm_card）出现**（取数层类型闸，
        # 别的卡型零 IO、区不出现）。内容＝上游需求文档正稿的**读侧投影**：
        # 每轮装配现取卡面当前值，上游改了这区跟着变——不复制、不进会话、
        # 不会出现"开工时塞进任务书然后过期"的残本。
        "key": "upstream_requirements",
        "order": 160,
        "name": "上游需求文档",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": ("context_assembler._load_upstream_requirements：父卡（派遣方）"
                   "＋对象卡（差事账 target_card）的需求文档正稿，"
                   "ui/qa_card/pm_card 才取"),
        "render": lambda ctx: render_upstream_requirements_region(
            ctx.upstream_requirements),
        "count": lambda ctx, text: len(ctx.upstream_requirements),
    },
    {
        # [父卡展示子卡现状 2026-10-15] order 170：紧随 155 之后、200 记忆之前——
        # 「我是谁」→「我的直接子卡现在怎么样」→「我的记忆」。
        # 来源＝直接子卡的 status_block（**只一层，不含孙级**；SQL 不递归）。
        "key": "child_status",
        "order": 170,
        "name": "直接子卡（活性·使命·现状）",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": ("当前卡的**直接**子卡 mission ＋ status_block（WHERE parent=?，不递归；"
                   "需求文档不给——用户 2026-10-27 裁定「太长」；两块都空则跳过）"),
        "render": lambda ctx: render_direct_child_status_region(ctx.direct_child_status),
        "count": lambda ctx, text: sum(
            1 for child in ctx.direct_child_status
            if _child_status_body(child) or _child_mission_body(child)),
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
        # [2026-11-01 · 用户两条裁定] ①「谁依赖我应该在对方卡面上，需要知道的
        #   不只是它自己」——关系位置是**卡面事实**，三席做判断都要看得到，
        #   profiles 由 main-only 放开为三席；②「接口改了经关系线自动更新，
        #   不需要通知」——出边条目带上目标**当前**需求文档（对外信息），
        #   装配即最新，取代推送式通知。
        "key": "relations",
        "order": 250,
        "name": "依赖关联",
        "channel": "system",
        "seats": ("main", "viewpoint", "draft"),
        "owner": "context",
        "source_owner": "assembler",
        "delivery": "system",
        "profiles": ("main", "viewpoint", "draft"),
        "source": "当前卡的依赖关联线（relations）：正向＝本卡自己的条目（带目标当前需求文档）；反向＝全库卡索引扫 target==本卡（零额外 IO）",
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


# ---------------------------------------------------------------------------
# [调用对存根回灌 2026-11-14 · 用户令「试下方案1」]
# 背景：工具结果**不进上下文**（2026-10-23 裁定，card_005 实测结果占 92% token），
# enters_context 把 actor=tool 与配对的 assistant(tool_calls) 成对剥掉——历史里
# 的调用通道示范为零，而失败伪调用文本（<confirm> 等）干净留存、逐轮累积，
# flash 级模型出现"正文写调用壳"漂移（card_021 17:44–17:52 实录）。
# 修法：**只放最近 _TOOL_PAIR_STUB_KEEP 组完整调用对，且结果体折成一行存根**——
# 恢复"调用→回执"的协议形态示范，代价每对几十字符而非几千。谓词
# enters_context 语义不动（token 计量/凝练池/板写闸仍按"工具块不进"算）；
# 存根纯投影层产物，真相源（会话文件/板）不变。
_TOOL_PAIR_STUB_KEEP = 24


def _is_toolish(b: dict[str, Any]) -> bool:
    """工具对块：actor=tool（结果）或 actor=assistant 且带 tool_calls（调用壳）。"""
    actor = str(b.get("actor") or "")
    return actor == "tool" or (actor == "assistant" and bool(b.get("tool_calls")))


def _seq_key(b: dict[str, Any]) -> int:
    s = b.get("seq")
    try:
        return int(s)
    except (TypeError, ValueError):
        return 1 << 60


def _merge_toolish_into(
    rows: list[dict[str, Any]], source_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 source 里的工具对块按 seq 合进 rows（板行永不含工具块 ⇒ 无重复）。"""
    toolish = [b for b in source_blocks if _is_toolish(b)]
    if not toolish:
        return list(rows)
    return sorted(list(rows) + toolish, key=_seq_key)


def _collect_tool_stub_ids(
    source: list[dict[str, Any]],
) -> tuple[set[int], dict[str, str]]:
    """倒扫 source 取最近 _TOOL_PAIR_STUB_KEEP 组**完整**调用对。

    一组＝一个 assistant(tool_calls) 头块 ＋ 紧随其后的连续 actor=tool 块。
    孤儿 tool 块（头不是调用壳、或头部被水位切走）不救、不凑——不完整的对
    没有示范价值。返回 (保留块 id 集, tool_call_id → 工具名)。
    """
    keep: set[int] = set()
    names: dict[str, str] = {}
    kept = 0
    i = len(source) - 1
    while i >= 0 and kept < _TOOL_PAIR_STUB_KEEP:
        if str(source[i].get("actor") or "") != "tool":
            i -= 1
            continue
        j = i
        while j >= 0 and str(source[j].get("actor") or "") == "tool":
            j -= 1
        head = source[j] if j >= 0 else None
        if (head is not None and str(head.get("actor") or "") == "assistant"
                and head.get("tool_calls")):
            for t in range(j, i + 1):
                keep.add(id(source[t]))
            for tc in head.get("tool_calls") or []:
                fn = tc.get("function") or {}
                cid = str(tc.get("id") or "")
                if cid:
                    names[cid] = str(fn.get("name") or tc.get("name") or "")
            kept += 1
        i = j - 1
    return keep, names


_TOOL_STUB_ARGS_CAP = 240


def _tool_stub_wire(m: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """工具对块的存根投影：调用壳原样留（长参数折叠），结果折成一行回执。"""
    actor = str(m.get("actor") or "")
    if actor == "assistant":
        tcs: list[dict[str, Any]] = []
        for tc in m.get("tool_calls") or []:
            tc2 = dict(tc)
            fn = dict(tc2.get("function") or {})
            args = str(fn.get("arguments") or "")
            if len(args) > _TOOL_STUB_ARGS_CAP:
                fn["arguments"] = args[:_TOOL_STUB_ARGS_CAP] + "…[参数已折叠]"
            tc2["function"] = fn
            tcs.append(tc2)
        item = {"role": "assistant",
                "content": str(m.get("content") or ""),
                "tool_calls": tcs}
        if m.get("reasoning_content"):
            item["reasoning_content"] = m["reasoning_content"]
        return item
    cid = str(m.get("tool_call_id") or "")
    raw = str(m.get("content") or "")
    failed = ('"ok": false' in raw) or ('"ok":false' in raw)
    nm = names.get(cid) or "?"
    return {"role": "tool", "tool_call_id": cid,
            "content": (f"工具回执已折叠：{nm} · {'error' if failed else 'ok'}"
                        f" · 原文 {len(raw)} 字符")}


def project_history_messages(
    blocks: list[dict[str, Any]], mark: int | None = None,
    allowed_roles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """历史区的统一投影：按凝练水位切片、滤过程块、转 card-msg 头，保留思考回传。

    这是纯函数；卡片会话文件不改。

    [mark 是 seq 水位 · 2026-11-12] mark=已凝最后一块 seq+1（memory_store 口径），
    **不是列表下标**：按 ``seq >= mark`` 过滤。改前 ``source[mark:]`` 按下标切——
    seq 有空洞即错位；更致命的是先经 ``drop_stale_review`` 蒸发后列表短于 mark
    时 ``0 <= mark <= len`` 不成立 → 回退**全量**（水位静默失效，card_021 实案：
    mark=20236、过滤后 20215 → 2828 条全量历史进上下文）。无 seq 的运行时附加块
    （prior_extra）不属于原文库、不受水位约束，保留。

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
        source = [m for m in source
                  if m.get("seq") is None or int(m.get("seq") or 0) >= int(mark)]
    # [调用对存根 2026-11-14] 先对 source 尾部的完整调用对建存根白名单——
    # 它们在下方 enters_context 闸前优先放行（以存根形态），谓词语义不变。
    _stub_ids, _stub_names = _collect_tool_stub_ids(source)
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
        if id(m) in _stub_ids:
            out.append(_tool_stub_wire(m, _stub_names))
            continue
        # [历史去工具结果 2026-10-23 · 用户需求] 工具结果**不进上下文**：
        # 留着的代价太大（实测 card_005：工具结果占 92% 的 token——104 万里 98 万），
        # 而模型真正需要的是它自己的结论（assistant 正文）＋记忆条目。
        # 必须成对丢：只丢 tool 不丢 assistant(tool_calls) 会留下孤立调用 ⇒ 端点 400，
        # 所以带 tool_calls 的那条 A 块（content 恒为空）一起丢。
        from ..storage import predicates as _pred
        if not _pred.enters_context(m):
            continue
        item = {
            # [F13 · 2026-10-31 卡间消息 system 化（对齐 dsh renderHistory 区头升格）]
            # 改前卡间块换头后仍 role=user——**机器生成的消息冒充用户发言**，模型会
            # 把别的卡的话当成人的指令。原版明令「不得裸 user 冒充用户发言」：区头
            # 升格 role=system。内容仍带 card_msg 的固定头标记（〔来自卡X〕语义）。
            "role": ("system" if actor.startswith("card:") else role),
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
        from ..storage import context_board as _cb
        if _cb.enabled():
            # [缓存板 2026-11-12] 视野在写时已算好：板=「过闸 且 seq>=水位 且
            # 评审 keep-N」的物化答案，这里只做机械投影、零筛选。
            # prior_extra（运行时追加块）不属于板，走独立通道照旧接在板后。
            # [调用对存根 2026-11-14] 板行永不含工具块（写时过闸剥掉）——从
            # history_source 原文把工具对块按 seq 合回，投影层才能见到它们
            # 并落存根；mark 照传，水位下旧对不进、extras 走独立通道。
            rows = _cb.read_board(ctx.work_id, ctx.card_id)
            if ctx.current_pair_id:
                # [本轮消息双重计数 2026-11-14] 本轮 user 块落盘瞬间已进板；
                # 它归 user 通道，不归历史——按 pair_id 从板行滤掉。
                rows = [r for r in rows
                        if str(r.get("pair_id") or "") != ctx.current_pair_id]
            merged = _merge_toolish_into(rows, ctx.history_source)
            return project_history_messages(
                merged, ctx.history_mark) + project_history_messages(
                ctx.history_extra)
        # [评审流量治理 2026-11-02 · 用户裁定] 评审过程消息只留最新数条在场，
        # 更早的蒸发——评审持久记录归 review_store，会话流量不需要记忆。
        from ..storage import review_scope as _rs
        # prior_extra 是别的卡的会话块（seq 相对它自己，恒小于本卡水位）——
        # seq 水位过滤会把它误杀；拆出 extras（按对象身份，不猜位置）单独投影接尾。
        _ext_ids = {id(m) for m in ctx.history_extra}
        _head = [m for m in ctx.history_source if id(m) not in _ext_ids]
        return project_history_messages(
            _rs.drop_stale_review(ctx.work_id, ctx.card_id, _head),
            ctx.history_mark) + project_history_messages(ctx.history_extra)
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
        # [F12 · 2026-10-31 区级异常隔离（对齐 dsh projection-assembler:247-258）]
        # 单区渲染炸了 ⇒ 记日志、跳过该区、**不中断整轮装配**——改前一个区抛异常
        # 整个 render_registered_regions 陪葬，模型一个字都收不到。
        try:
            payload = spec["render"](ctx)
        except Exception as exc:  # noqa: BLE001 单区失败不炸整轮
            logger.error(
                "[region] 区渲染异常（跳过该区、其余照常）key=%s order=%s name=%s: "
                "%s: %s", spec["key"], order, spec["name"], type(exc).__name__, exc)
            continue
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
    # [F22 · 2026-10-31] 区装配体积打点：每区 key/order/bytes/entries 落 JSONL——
    # 记忆区/历史区占多大、哪天开始膨胀，数字说话。打点失败不阻断（observe 内吞）。
    try:
        from ..infra import observe as _obs
        _obs.log_regions(work_id=str(ctx.work_id or ""), card_id=str(ctx.card_id or ""),
                         regions=rendered)
    except Exception:  # noqa: BLE001 观测永不阻断装配
        pass
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
    # [2026-10-31 修·职责区重复] 三套词表（root / rd_project / research_project）里
    # function、rule、role、review 是**同一个 code 重复声明** ⇒ 原来逐词表平铺，
    # 同一类型出现 2–3 行（实测评审席 system 里「职能卡」3 行、「规则卡」2 行、
    # 「角色卡」2 行）。按 code 去重，保留首次出现的那份描述。
    seen: set[str] = set()
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
            if code and code in seen:
                continue                      # 同一类型在别的词表里已渲染过
            if code:
                seen.add(code)
            lines.append(f"- {label}（{code}）：{desc}")
    if len(lines) == 1:  # 注册表里没有任何类型 → 空区
        return ""
    return "\n".join(lines)


# ============================================================
# [职能组建区 2026-10-21 · order 91 · 改为「轮首必须做判断」注入]
# ============================================================
# 这个区**只给判断标准，不给职能清单**（守 D02「职能不定死」）：
#   职能名、设几个、盯什么卡，全部由模型看着眼下这件事自己判断；
#   系统**不建职能表、不做白名单/枚举校验**，职能名对不对由它自己负责。
# 为什么值得单独占一个区：写进 goal 里靠自觉，且只在「本轮目标恰好提到」时生效；
# 而拆职能恰恰是根卡「自动模式**轮首**的第一件事」，漏掉就漏一整段盯防。
# 由此本区加**轮首闸门**：只在自动模式第 0 轮（goal_rounds==0）出区；
#   goal_rounds>0 时返回空串，避免变成每一轮都念的墙纸。
# 边界：本区**不做职能闸门**（不判「有没有职能」「建过没有子卡」）
#       —— 判断靠提示词要求，不靠闸门剥夺工具；这里只问「要不要拆、按什么标准拆」。
_TEAM_FUNCTION_INSTRUCTION_TEXT = """## 拆职能（你这一轮的第一件事）

动任何手之前，**先做一个明确判断：这次任务要不要拆职能（角色）？**

**要拆**——按人类团队里真实存在的岗位来拆。产品、架构、研发、质量、安全、运维、数据、体验、交付……这些只是让你理解什么叫「职能」，**不是清单，也不许照抄**。它该是什么，由你看着眼下这件事自己判断。每个职能说清三件事：负责哪一块、盯什么、什么情况下该开口。

判断标准只有一条：**如果这个职能在你熟悉的人类团队里找不到对应的人，就不要建它。**

- 不要把「任务」当职能（「写文档」是任务，不是职能）。
- 不要把「模块」当职能（「存储层」是模块，不是职能）。

**不拆**——就直说这件事不需要专职职能、理由是什么。那比编一个凑数好。

**判断结果要落到卡面上**：写进你的需求文档（edit_requirements）或现状（edit_status），不要只在心里想。后面接手的人要能看出你判断过。

职能不挂在你的任务树下，它不干活，只盯。你想清楚要盯什么、什么情况下它该开口，再建。"""


def render_team_function_instruction_region(ctx: RegionCtx) -> str:
    """91 区渲染（拆职能）：**项目根卡 ＋ 自动模式 ＋ 轮首**才出，逐字常量注入。

    三条渲染条件**同时**满足才输出，否则空串：
      1. 当前卡是项目根卡：`cur_meta.card_id == work_card_id`
         （`cur_meta` 里没有 card_id 时退回用 `ctx.card_id` 比）；
      2. 自动模式：`bool(cur_meta.auto_mode)`；
      3. 轮首：`int(cur_meta.goal_rounds or 0) == 0`（读不到/为空/抛异常 ⇒ 按 0 处理）。
    fail-open 方向刻意不同：
      · 1、2 条读不到（空 dict／坏数据）时宁可**不出**——那是「可能不是根卡」的信号，
        猜错会给非根卡注入组织指令；
      · 第 3 条读不到只意味着「不知道跑了多少轮」，代价只是多判断一次，
        故**按 0 fail-open 出区**，不可静默吞掉用户明确要求的这一步。
    """
    meta = ctx.cur_meta or {}
    cur_card_id = str(meta.get("card_id") or ctx.card_id or "")
    if not cur_card_id or cur_card_id != str(ctx.work_card_id or ""):
        return ""
    if not bool(meta.get("auto_mode")):
        return ""
    try:
        rounds = int(meta.get("goal_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    if rounds > 0:
        return ""
    # [预设职能 P-A 2026-10-24] 目录启用 ⇒ 职能已预设就位(总裁+四职能,建项目
    # 即播种),「由你自由拆职能」的引导退役——继续注入会教根卡走已收起的旧路
    # (create_function 工具同批从工具面摘除)。目录读不出/未启用 ⇒ 旧行为逐字不变。
    try:
        from . import function_room as _fr
        if _fr.load().get("enabled"):
            return ""
    except Exception:  # noqa: BLE001 判不出目录 ⇒ 保旧文(不过度收)
        pass
    return _TEAM_FUNCTION_INSTRUCTION_TEXT


# ============================================================
# [卡片能力区 2026-10-21 · 洞察来自 card_006 研究 + 用户裁定]
# ============================================================
# 为什么要有这个区（实测依据，不是设计偏好）：
#   v1–v9 共 30+ 张子卡，**没有一张**被上级告知「你可以建自己的子卡 / 写自己的
#   需求文档 / 发消息」。这项知识一直只写在「给根卡的那段提示」里，要不要往下传
#   全靠上级自觉 ⇒ 实测 0/30 传下去了。子卡不知道该能干什么，于是只执行；
#   上级以为子卡只能执行，于是把判断全做完再派活 —— 「子卡被用成子 agent」
#   就是这么生产出来的。
#   ⇒ 修法不是继续教上级怎么写任务书（那要靠自觉），而是**系统按每张卡自己的
#   perms 直接注入**：上级写不写都不影响；执行体也照实看到「你没有建卡权」。
# 与 90 区（卡片职责）的分工：
#   90 讲「这类卡在系统里是什么」（类型级职责，来自注册表）；
#   本区讲「**你这张卡**手上有什么、动手前该先想什么」（实例级能力，来自 perms）。
# 空来源（读不到 perms）→ 只出纪律那一段，不编造能力。
# [能力底座 2026-10-30] 键从"权限开关"换成"能力 code"——92 区要说的是
# 「你手上有什么」，能力才是这个问题的单位；perms 已退役。
_ABILITY_SWITCH_LINES: tuple[tuple[str, str], ...] = (
    ("create_child", "建自己的子卡"),
    ("write_own_face", "改自己的正文、写自己的需求文档"),
    ("read_linked", "沿通信线读对方卡"),
    ("link", "建通信线"),
    ("unlink", "解通信线"),
    ("link_rel", "记依赖/影响关系"),
    ("read_files", "看文件"),
    ("write_files", "写文件"),
    ("run_code", "跑命令"),
    ("web", "联网查证"),
    ("manuscript", "查本项目存档资料"),
    ("dispose_card", "删卡与归档"),
)

_RULE_TAIL = (
    "**动手之前先想清楚：这件事该怎样拆解才合理** —— 要不要拆、拆成几块、"
    "块与块之间是什么关系。"
)

_DISCIPLINE = (
    "- 你负责范围内的判断由**你自己下**，写进自己的需求文档块；不要等上级把判据替你定完。"
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
    """92 区渲染（卡片能力）：把「这张卡手上有什么」按**它自己挂的能力**渲染。

    三条硬规矩（改这个区时不要破坏）：
    1. **来源是卡自己的 abilities**（建卡时按模式清单 ∩ 卡型上限算好写在卡上），
       不是卡类型猜的、不是上级任务书里写的 ⇒ 上级传不传都不影响。
    2. 能力必须**照实**：没有 create_child 就明说没有（执行体不该被暗示能组织），
       有就明说可以、并明说「不必等上级替你想好怎么分」。
    3. 挂载读不到（旧卡/坏数据）→ 不编造能力，只出纪律段（fail-open 不阻断装配）。
    """
    meta = ctx.cur_meta or {}
    card_type = str(meta.get("card_type") or "")
    card_id = str(meta.get("card_id") or ctx.card_id or "")
    # [能力底座 2026-10-30] 来源改成**卡上挂的能力**（perms 已退役）。
    # 读不到 ⇒ None ⇒ 只出纪律段（不编造能力，也不阻断装配）。
    _ab = meta.get("abilities")
    has = {str(x) for x in _ab} if isinstance(_ab, list) else None

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
            # [派卡权明说 2026-11-05 · card_167 病灶] 能力表只写「建子卡」时，
            # 模型把「我没有写文件/跑命令」误推成「这活不归我」往上推——
            # 它不知道建卡权里就含着派执行卡。把三种卡各自干什么点破：
            # **不能亲手做 ≠ 不能办成**。
            lines.append(
                "- **派执行卡干活：可以。** `create_worker_card`（worker 卡："
                "除 UI 外一切执行——编程/写改文档/跑命令/起服务/构建测试/查档）、"
                "`create_ui_card`（UI 卡：写页面＋浏览器验收，没有 bash/python）。"
                "**能力表里没有的活（写文件、跑命令）不等于办不了——"
                "派执行卡替你干，照样是你办。**")
        if "deploy_function" in has:
            lines.append(
                "- **召职能差事：可以。** `create_deploy_card`——qa＝验收卡、"
                "pm＝执笔卡，召进场干专项活，办完自动收工、结论回执给你。")
        # [2026-10-21 修正] 还没拆过 ⇒ 把事实说出来，让它自己判断这一轮要不要拆。
        # 不做工具面闸门（那会挡住「第一轮其实是讨论」的正常回合）。
        # 判据：有建卡权（能拆）＋ 系统里还没有它的直接子卡（没拆过）＋ 自动模式卡。
        _has_child_perms = isinstance(has, set) and "create_child" in has
        _no_children = not (ctx.direct_child_status or [])
        if _has_child_perms and _no_children and bool(meta.get("auto_mode")):
            lines.append(_NOT_SPLIT_HINT)
        elif not _has_child_perms:
            # [2026-10-31 修·能力区自相矛盾] 原来这里是裸 `else`：**有**建卡权、
            # 但「已有子卡」或「非自动模式」时也掉进来 ⇒ 同一份「你的能力」里同时
            # 出现「建自己的子卡：可以」与「没有这个能力」两条互相矛盾的行
            # （实测 work_027/card_124 评审席 system prompt 里两条并列）。
            # 判据改成只看**有没有这个能力**；有权限但这一轮不提示拆分的，什么都不说。
            why = ("你是执行体：一次一件事、做完即完工、不长树"
                   if card_type == "worker" else "本卡类型不长树")
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
        lines.append("- （系统未读到本卡的能力挂载，能力以系统提示为准。）")

    lines.append(_DISCIPLINE)
    # [通信收拢 2026-10-28 · 用户裁定「接口只有两个，且只能沿连线」]
    # 场景表重写：review_request / scene_report 已退役，通信只剩同步与唤醒两条。
    lines += [
        "",
        "## 通信场景表(什么时候用哪个)",
        "- **sync**:让对方**知道**一个事实(改了接口/定了规范/状态变了)——"
        "沿通信线送达、**不叫醒它**,它下次回合自然看到;",
        "- **direct**:要对方**现在就做**一件事——沿通信线送达并**叫醒它**,"
        "验收判据必填(怎么算做到);只能对你子树内的卡下达;",
        "- **report_up**:一段工作做完(或做不下去)时**向上一级交代**——只发直接父卡,"
        "没有 kind、没有请示;汇报得不好,上级会 direct 下发修改;",
        "- 三者都要求两卡之间**已有通信线**（父子之间有默认线；平级要先 link_cards）。",
    ]
    # [治理工具说明 2026-10-24 · 用户令「新增工具都要在工具面提供信息说明」]
    # 根卡/父卡(project/sub_project/topic)追加治理段:stop_card 与 silence_card
    # 不经 perms(治理权,类型闸直接下发)——92 区照实说明,不然模型不知道
    # 自己手里有这两件、也不知道界限(子树/完工/总裁不可停)。
    if card_type in ("project", "sub_project", "topic"):
        lines += [
            "",
            "## 你的治理工具（管理下级用，不影响你自己的执行）",
            "- **stop_card(card_id, mode, reason)**——打断或强制停止下级卡"
            "（总裁可停全树，父卡限自己子树）：`mode=interrupt` 只终止它当前"
            "一轮（那轮跑偏/卡住时用，之后它照常继续）；`mode=halt`（缺省）"
            "**强制停止**——终止当轮＋暂停它的目标（连同它的子树）＋拒绝一切"
            "内部唤醒，直到用户发话或 resume_child 恢复。reason 必写（留痕）。",
            "- **silence_card(card_id, action, reason)**——让**完工的卡**沉默"
            "或重新激活：沉默后它不再接收任何内部消息（信留存但不投递），"
            "持久生效直到 activate；只对 state=completed 的卡生效。",
            "- **continue_task(card_id, task, note)**——给完工/暂停的**worker 卡**"
            "续派下一件任务：同一张卡接着干，之前交付的代码、回执、会话上下文"
            "全部延续。返工/迭代/同域下一件用续派；全新领域才新开worker 卡。",
            "- **stage_open(card_id)**——开启子卡的阶段任务：子卡需求填写完整"
            "后调用，职能卡做立项评审。**开启子卡任务必须先走这一步**。"
            "子卡完工经 report_up(kind=done) 汇报——职能做阶段验收。",
            "- 界限：总裁(根卡)不可被停/沉默；兄弟卡互不管辖；每次动作都留痕，"
            "被治理的卡面与用户看得到原因。",
        ]
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


def render_card_list_region(
    cards: list[dict[str, Any]],
    work_card_id: str,
    current_id: str = "",
    related: list[dict[str, Any]] | None = None,
) -> str:
    """100 区渲染：**只列与当前卡有关系的卡**——父子线（祖先链＋子树）∪ 关系线对方。

    [关系治理 2026-10-30 · 用户令「没关系的卡不应出现在清单里，这是污染」]
    改前列的是**全树每卡一行**：模型看得见一张卡、却和它毫无关系，只会在无关上下文
    上浪费注意力，还容易误以为可以操作它。可见即有关系。

    [2026-10-30 修正] 关系线对方**不进这个区**——那是 250 区的事。本区只列
    父子线上的卡（祖先＋子树）；线上的卡（依赖/影响）由 250 区带方向与内容列出。

    - **父子线**：我的祖先链 ＋ 我的子树（双向）
    - **关系线**：与我之间有依赖/影响关系的卡（双向，内容由装配层取好传进来）
    - **通信线不进清单**：它只管发消息，不代表这棵树上有它
    - 职能卡 / 角色卡仍然排除（不是协作对象，无寻址权）

    ``related`` 每项：``{"card_id","dir","kind","detail"}``，dir ∈ ``out``（我指向它）
    / ``in``（它指向我）。``current_id`` 缺省 ⇒ 退回旧行为（全树），不静默清空。

    纯函数：不做 IO，父子关系从 ``cards`` 的 parent 字段现算。
    """
    parent: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    for c in cards or []:
        cid = str(c.get("card_id") or "")
        if not cid:
            continue
        pid = str(c.get("parent") or "")
        parent[cid] = pid
        if pid:
            children.setdefault(pid, []).append(cid)

    rel_lines: dict[str, list[str]] = {}
    for r in related or []:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("card_id") or "")
        if not rid:
            continue
        kind = str(r.get("kind") or "")
        detail = str(r.get("detail") or "").strip()
        if str(r.get("dir") or "") == "out":
            head = "我依赖它" if kind == "依赖" else ("我影响它" if kind == "影响" else f"我{kind}它")
        else:
            head = "它依赖我" if kind == "依赖" else ("它影响我" if kind == "影响" else f"它{kind}我")
        rel_lines.setdefault(rid, []).append(f"{head}：{detail}" if detail else head)

    if current_id and current_id in parent:
        visible: set[str] = {current_id}
        cur = current_id                       # 祖先链
        while parent.get(cur):
            cur = parent[cur]
            if cur in visible:
                break
            visible.add(cur)
        stack = list(children.get(current_id, []))   # 子树
        while stack:
            x = stack.pop()
            if x in visible:
                continue
            visible.add(x)
            stack.extend(children.get(x, []))
        # [2026-10-30 修正] **不把关系线对方并进来**：100 区列的是"树上的卡"
        # （父子线：祖先＋子树），线上的卡由 250 区列。两种关系不同，混在一处
        # 会让同一张卡在两区各出现一次，且看不出它是"树上的"还是"线上的"。
    else:
        visible = set(parent)                  # 旧行为：全树

    lines: list[str] = ["## 卡片清单"]
    for c in cards or []:
        cid = str(c.get("card_id") or "")
        if cid not in visible:
            continue
        if str(c.get("card_type") or "") in ("function", "role"):
            continue
        if cid == work_card_id or not c.get("parent"):
            head = f"{cid} · {c.get('title')}（根）"
        else:
            head = f"{cid} · {c.get('title')}（父：{c.get('parent')}）"
        rels = rel_lines.get(cid)
        if rels:
            head += "｜" + "；".join(rels)
        lines.append(head)
    return "\n".join(lines)


def _number_lines(text: str) -> str:
    """给块内容逐行加行号（`  3│ 内容`），供 edit_* 的 ``lines`` 定位直接用。

    [行号定位 2026-10-29 · 用户令「如果你觉得需要填写原文才能编辑，那么为什么不是
    代码代劳？为什么需要它自己复制黏贴？」]
    精准编辑本来就有 ``lines="12-15"`` 这条纯行号通道（代码按行替换，模型一个字
    都不用抄），但上下文里从来没给过行号 ⇒ 模型算不出第几行 ⇒ 只能退回
    ``old_string`` 逐字复制原文。**定位所需的信息本来就该由代码给。**

    口径：行号＝``text.split("\\n")`` 的下标 +1，与 ``_h_edit_card`` 的行模式
    **逐字对齐**（那边也是 split("\\n")，空行照样占一个号）。所以这里的 ``text``
    必须是**工具端会拿到的那一份**（现状块＝status_block；需求文档＝**正稿**——
    [2026-11-01 用户令] 草稿不进上下文，改草稿的行号见 edit_requirements 回执），
    否则标出来的号跟工具对不上，比不给还坏。
    """
    rows = str(text or "").split("\n")
    width = max(2, len(str(len(rows))))
    out: list[str] = []
    for i, row in enumerate(rows, 1):
        out.append(f"{i:>{width}}│ {row}" if row.strip() else f"{i:>{width}}│")
    return "\n".join(out)


def render_current_card_region(ctx: "RegionCtx") -> str:
    """150 区渲染：当前卡身份 ＋ 正文三块 ＋ 影子三节（**常驻**）。

    [常驻当前卡 2026-10-19 · permexp] 用户令：卡片自己的现状与影子必须在上下文里，
    不能让模型每轮靠 read_card/read_shadow 去拉——卡片是持久对象，打开就该看见自己
    的全部状态。取数在装配层一次完成（RegionCtx.cur_body / cur_shadow），本函数纯渲染。

    裁剪口径：影子只出**产物清单 / 待协作事项 / 进度**三节；「迭代历史」是系统机械
    留痕（155 区已告知模型"你不用写"），且随时间无限增长，常驻只会挤占上下文。

    [卡面区域语义 2026-10-28] **空块也出**：三块各带一段"这块回答什么问题／写什么／
    不写什么／谁写"的定义。改前空块整段不出现，模型看不到那块的定义——而空块正是它
    最需要知道"这里该放什么"的时候。整卡无正文时仍只出身份行 ＋ 三块定义。
    """
    cur = ctx.cur
    if cur is None:
        return ""
    # 延迟 import：card_draft 反向依赖本模块，模块级 import 会成环（同 _child_status_body）。
    from .card_draft import _is_placeholder  # noqa: PLC0415

    third = _THIRD_BLOCK_NAME_BY_TREE.get(ctx.project_class or "", "需求文档")
    body = ctx.cur_body or {}
    lines = ["## 当前卡", f"{cur['card_id']} · {cur['title']}"]

    # ============================================================
    # [卡面区域语义 2026-10-28 · 用户令「模型根本没真正理解卡面各区域的含义和用法，
    #  所以才会胡乱使用」「写的详细清晰一些」]
    #
    # 改前：三块只出 `### 块名` ＋ 内容，**一个字定义都没有**。模型看自己卡面时
    # 只能靠提示词里那段"用哪个工具写"去推这块是干什么的 ⇒ 实测三种写混：
    #   · 把「我打算先做 A 再做 B」写进现状（那是计划，不是事实）
    #   · 把「已经改完 a.py」写进需求文档（那是进度，不是要求）
    #   · 把「要支持导出」写进现状（那是需求）
    #
    # 改后：**定义跟着内容走**——每块标题下先给"这块回答什么问题 / 写什么 /
    # 不写什么 / 谁写"，再给内容。定义不依赖模型记住提示词里的某一段。
    # 块为空时**也出定义**（空块正是模型最需要知道"这里该放什么"的时候）。
    # ============================================================
    _BLOCK_DEF: dict[str, str] = {
        "mission": (
            "*这块回答：**这张卡为什么存在、负责哪一块、管到哪为止**。*\n"
            "*写：「我负责什么、负责到什么程度、不负责什么」，一两句说清。*\n"
            "*不写：做法、步骤、文件清单、验收标准、进度——它是**身份**，不是状态，不随进度变。*\n"
            "*谁写：立项时定稿（上级或用户写）；你自己改不了（会被拒）。*\n"
            "*为什么重要：它是判断「这件事该不该由我做」的唯一依据。*"
        ),
        "requirements": (
            "*这块回答：**做到什么算完成**。*\n"
            "*写：逐条列「要什么」，每条带一条**能验的判据**（跑什么命令、看哪个文件、"
            "得到什么结果、哪些情况算不合格）。*\n"
            "*不写：已经做成什么（那是现状）、你打算怎么做（那是执行的事）、"
            "过程记录（那是影子）。*\n"
            "*谁写：**你自己可以改**（也能改上级/总裁定的）；上级与总裁也会策展、修订。*\n"
            "*为什么重要：它是**收口时唯一的判据**——逐条对照，做不到就不算完成。*\n"
            "*★ **只写「确定的需求」**：确定＝经过评审、已经定下来要按它验收。"
            "猜测的、待确认的、可选的不要写——写进来就成了判据，会把这张卡逼进「永远差一条」的死局。"
            "不确定的放到：现状的「现在卡在哪」／影子的「待协作事项」／直接问上级。*\n"
            "*★ **两版**：edit_requirements 写的是**草稿**——草稿不进上下文"
            "（只在回执与评审材料里看得到），它唯一的用途是评审：经需求评审全员"
            "通过后转成**正稿**（所有人读到的、验收对照的都是正稿）。"
            "正稿要改：改草稿 → 再走一遍评审 → 通过才替换。*"
        ),
        "status_block": (
            "*这块回答：**现在实际是什么样**。*\n"
            "*写：已经做成什么（交付物在哪、跑什么命令能验、跑出什么结果）、"
            "对外接口/契约是什么、现在卡在哪（阻塞点）。*\n"
            "*不写：计划、「我打算怎么做」、要什么（那是需求文档）、思考过程。*\n"
            "*一条读数过时就**换成新的**，作废的就**删掉**——它是**快照**，不是账本；"
            "历史在版本（每卡 20 版可回退）与影子的「迭代历史」里。*\n"
            "*谁写：本卡自己；上级、接手的人、下游卡都靠它判断「这卡到哪了、能不能用」。*"
        ),
    }
    lines += [
        "",
        "## 卡面三块：每块只回答一个问题",
        "**三块是一条链**：使命（为什么存在）→ 需求文档（做到什么算完成）→ 现状（现在到哪）。"
        "后一块以前一块为判据；**同一件事不要在两块重复**。",
        "**最常见的三种写混**（都是实测发生过的）："
        "① 把「我打算先做 A 再做 B」写进现状——那是计划，不是事实；"
        "② 把「已经改完 a.py」写进需求文档——那是进度，不是要求；"
        "③ 把「要支持导出」写进现状——那是需求，该在需求文档。",
        # [行号定位 2026-10-29 · 用户令「为什么不是代码代劳」] 给行号＝不用复制原文。
        "**行号是给你用的**：现状与需求文档的内容逐行标了号（形如 `12│ 内容`）。"
        "要改某几行 ⇒ lines 填 12 或 12-15（1 起算、含两端），代码按行替换，"
        "**不用抄原文**。行号只是给你看的，不要写进 new_string。"
        "需求文档的号按**正稿**标（上下文只给正稿）；改草稿的行号在 "
        "edit_requirements 的回执里——它带草稿全文的标号。",
    ]

    for name, key in (("使命", "mission"), ("现状", "status_block"), (third, "requirements")):
        text = str(body.get(key) or "").strip()
        # 骨架模板（「（本卡作用/目标…）」这类）不是内容：常驻进去会让模型
        # 把模板当既有事实照抄（实测 system prompt 里整段模板原样出现）。
        _label = name
        _draft_note = ""
        if key == "requirements":
            # [2026-10-31 修] 状态必须读 **cur_meta**：`ctx.cur` 是卡片索引行，
            # 只有 6 个键（card_id/title/parent/relations/created_at/card_type），
            # 没有 requirements_state ⇒ 改前标签恒判「尚无正稿」。
            # cur_meta 由 `card_store._meta_of` 取，两列都在。
            _src = {**(ctx.cur or {}), **(ctx.cur_meta or {})}
            _state = str(_src.get("requirements_state") or "")
            _label = name + ("（正稿）" if _state == "approved"
                             else "（尚无正稿）" if _state in ("", "draft")
                             else "（评审中）")
            # [2026-11-01 · 用户令「草稿和正稿不该同时出现在上下文里——
            # 只有正稿始终出现，草稿的出现是污染；草稿只有一个用途：修改
            # 正稿前用来评审」] 草稿正文**不渲染**：它在 edit_requirements
            # 的回执和评审材料里，不在这。这里只留一行状态说明。
            _has_draft = bool(str(_src.get("requirements_draft") or "").strip())
            if _has_draft:
                _draft_note = ("*另有一份草稿在改/在评（它不进上下文；"
                               "edit_requirements 的回执带草稿全文行号）。*")
        lines += ["", f"### {_label}", _BLOCK_DEF[key]]
        if _draft_note:
            lines.append(_draft_note)
        if text and not _is_placeholder(text):
            # [行号定位 2026-10-29 · 用户令「为什么需要它自己复制黏贴」]
            # 使命没有写通道 ⇒ 不给行号（给了等于暗示能改）。
            # [2026-11-01] 需求文档恒标正稿——草稿不在场，不存在「两份标号
            # 拿错去改」的问题了。
            lines += ["", text if key == "mission" else _number_lines(text)]

    shadow = ctx.cur_shadow or {}
    sh_lines: list[str] = []
    # [影子区域语义 2026-10-28] 与三块同口径：**定义跟着内容走**。
    # 改前影子只有【节名】＋内容，没有一句"这节放什么"——而 update_shadow 退役时
    # 把原来写在它 description 里的四节定义一起带走了（语义缺口）。
    _SHADOW_DEF = {
        "笔记": "*只给自己：判断、备忘、待验证的猜想——写完它还在你上下文里，下轮也读得到。*",
        "产物清单": "*产生的文件/文档：路径＋一句话用途，一行一个；只写指针。*",
        "待协作事项": "*要别人给什么、卡在哪、等谁；不放产物位置、不放改动记录。*",
        "进度": "*时间线：什么时候做到哪一步、现在什么状态；不写结论与杂事。*",
    }
    # [卡片笔记 2026-10-27 · 用户令] 笔记排最前：它是卡自己写给自己的，最该先看到。
    for name, key in (("笔记", "note"), ("产物清单", "artifacts"),
                      ("待协作事项", "coordination"), ("进度", "progress")):
        text = str(shadow.get(key) or "").strip()
        if text and not _is_placeholder(text):
            sh_lines += [f"【{name}】", _SHADOW_DEF[name], text, ""]
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
    """规则正文按 `## 区名` 切区：{区名: 区内容}。

    [规则=一个项目一个文件 2026-10-28] 实现搬到 ``storage.rule_store.split_sections``
    （切区是存储格式的一部分，规则文件在 storage 层）。这里保留同名入口，
    调用方零改动、且只有一份实现。
    """
    from ..storage import rule_store  # noqa: PLC0415 局部 import 避免循环

    return rule_store.split_sections(body)


def render_rule_region(rule_sections: dict[str, str], card_type: str) -> str:
    """85 区渲染：**总规则 + 本类卡规则**，都空 → 空串（没有来源就不出现这个区）。

    [规则节点 2026-10-28 · 用户令「每一个分区会进入对应卡类型的上下文规则区；
    还有一个总规则，则会进入所有卡的上下文」]

    两段都进同一个区，因为对模型来说它们都是「本项目对本卡的要求」，分两个区
    只会让它多一次拼接、让注册表/测试/前端多一份东西。顺序固定：先总规则
    （所有卡都适用），再本类卡（更具体，冲突时以更具体的为准）。
    """
    if not rule_sections:
        return ""
    picked: list[tuple[str, str]] = []
    total = str(rule_sections.get(rule_store.ALL_SECTION) or "").strip()
    if total:
        picked.append(("总规则（所有卡）", total))
    own = str(rule_sections.get(card_type) or "").strip() if card_type else ""
    if own:
        label = rule_store.SECTION_LABELS.get(card_type, card_type)
        picked.append((f"本类卡（{label}）", own))
    if not picked:
        return ""
    if len(picked) == 1:
        return "## 规则\n" + picked[0][1]
    body = "\n\n".join(f"### {name}\n{text}" for name, text in picked)
    return "## 规则\n" + body


def rule_section_names(registry: dict[str, Any],
                       fn_codes: list[str] | None = None) -> list[str]:
    """规则合法分区名＝**总规则区 + 注册表全词表 codes + 按职能区**（声明序）。

    纯函数（不做 IO）：调用方传入类型注册表（``card_store`` 的
    ``load_type_registry`` 返回值）。
    例：rd_project＋research_project ⇒ ["all","project","function","sub_project",
    "tool","worker","rule","review","topic"]。

    [规则节点 2026-10-28 · 用户令「还有一个总规则，则会进入所有卡的上下文」]
    ``all`` 不是 card_type，是「所有卡」这一档，固定排第一。注册表缺失/无词表
    ⇒ 只剩 ``["all"]``（总规则与词表无关，不该跟着注册表一起消失）。

    [两源模型 2026-11-01 · 用户令「QA 的验收准则写在规则卡内」] 职能的准则
    按 ``function.<目录 code>`` 分区（例 ``function.qa``）：``function`` 区
    全体职能共享，``function.qa`` 只进 QA 的上下文。``fn_codes``＝职能目录
    的 code 清单（调用方从 function_catalog 取）；这些区紧跟 ``function``。
    """
    out: list[str] = [rule_store.ALL_SECTION]
    vocabs = registry.get("l1_vocabularies") if isinstance(registry, dict) else None
    if isinstance(vocabs, dict) and vocabs:
        scopes = (["root"] if "root" in vocabs else []) + \
                 [k for k in vocabs if k != "root"]
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
    fn_names = [f"function.{str(c).strip()}"
                for c in (fn_codes or []) if str(c or "").strip()]
    if fn_names:
        pos = out.index("function") + 1 if "function" in out else len(out)
        out[pos:pos] = fn_names
    return out


def rule_inject_counts(registry: dict[str, Any],
                       fn_codes: list[str] | None = None) -> dict[str, int]:
    """每分区「正被 N 类卡注入」的静态口径：注册表内 code 出现次数。

    纯函数。例：rule 出现在 rd/research 两词表 ⇒ 2；sub_project ⇒ 1；
    ``function.qa`` ⇒ 1（只进 QA 这一个职能的上下文）。
    """
    out: dict[str, int] = {}
    for name in rule_section_names(registry, fn_codes):
        # 按职能区不随卡类型计数——它只进「那一位职能」的上下文。
        out[name] = 1 if name.startswith("function.") else 0
    # [规则节点 2026-10-28] 总规则进所有卡 ⇒ 计数＝本树类型总数
    #（按职能区不是类型区，不计入）。
    out[rule_store.ALL_SECTION] = len(
        [n for n in out if n != rule_store.ALL_SECTION
         and not n.startswith("function.")])
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
# [第三块命名单一来源 2026-10-22] 映射表已上移到 prompts.THIRD_BLOCK_NAME_BY_TREE——
# BASE_PROMPT（按树渲染）与 155 区共用同一处，避免两套定义漂移；
# 本模块以 `_THIRD_BLOCK_NAME_BY_TREE` 引用（见文件头 import）。

# 第三块「怎么写」——两套，按本树只出一套
_THIRD_BLOCK_HOWTO = {
    # [第三块统一命名 2026-10-22 · 用户裁定] 全系统只有「需求文档」一种，
    # 双名制（需求文档／需求文档）废除——原「需求文档」那套「只追加 D{nn} 条目」写法不再出现。
    "需求文档": (
        "需求文档——本卡全部需求的确定版详细描述，是本卡需求的唯一确定源。\n"
        "      写法：按需求逐条写清“要什么、验收标准是什么”；只写确定版；\n"
        "            讨论、备选、过程不写这里——判断与「为什么定」写影子·笔记，\n"
        "            何时改的不用记（自动留痕）；对别人成立的决定落成条文本身，\n"
        "            不写「某日裁定」。"
    ),
}


# ===========================================================================
# [事实分层 2026-11-01] 「卡面是唯一权威事实源」的**常驻说明**。
#
# 核心语义是【基准】（用户 2026-11-01 裁定）：不是「消息说了要去核实」的仪式——
# 消息忠实转达卡面变化，和去读卡面等价；真正的问题是**模型做判断、做工作时
# 参照的对象是什么**。它看到的信息太多（用户发言、文档、对话历史、别的卡的
# 消息、记忆条目），都是干扰项；卡面给它唯一的决策依据。
#
# 为什么在这里：这条原则原来只在 _SEC_ORG_TEXT 末尾有一句话（prompts.py），
# 三个缺陷——①只进 AUTO_BASE_PROMPT（手动卡看不到）；②随职能目录开关一起消失；
# ③埋在组织汇报段末尾。三席/评审席按设计零指令、拿不到任何使用说明，
# 但它们恰恰是「判断事实」的席位——所以放在 155 区（中性上下文，三席都发），
# 跟着卡面内容一起给，不写进 BASE_PROMPT（也不动版本号）。
#
# 用户同批裁定（不落笔的两条）：
#   · 卡面历史版本只是留痕/回退用，**不能拿来引用**——否则多一个污染源；
#   · 使命修订是**上级操作**，不增卡片自己的修订通道。
# ===========================================================================
_FACT_AUTHORITY_FUNCTION = (
    "〔事实以卡面为准〕你做判断、做工作时参照的对象只有两个——"
    "被验卡的需求文档（场卡的面）与你的专业准则（rules.md 里你的区，"
    "见「你的职能」一节）。"
    "其余一切都是信息输入，不是依据：用户发言、文档、对话历史、"
    "别的卡发来的消息、记忆条目——它们告诉你「有什么」，不给你「按什么判」；"
    "卡面的历史版本只是留痕，不能拿来引用。"
    "判断依据从卡面与事件里自己取，不靠对方口述；"
    "卡面对交付物的声称，能查证就查证（读文件/跑命令）。"
)


def _fact_authority_workcard(third: str) -> str:
    """三块卡的「事实以卡面为准」收尾段（155 区最后一块）。``third``＝第三块名。"""
    return "\n".join([
        "〔事实以卡面为准〕你做判断、做工作时参照的对象只有一个——卡面：",
        "      · 使命＝这卡为什么存在；"
        f"{third}正稿＝做到什么算完成（验收的判据）；",
        "        现状＝现在实际什么样（别人按它验收/对接）。",
        "      · 其余一切都是信息输入，不是依据：用户的发言、文档、对话历史、",
        "        别的卡发来的消息、记忆条目——它们告诉你「有什么」，不给你「按什么判」；",
        "        卡面的历史版本只是留痕，不能拿来引用。",
        "      · 要采纳一个输入，先落成卡面（自己的、或请对方落它的）——",
        "        不落面不能当依据；不落面的事也等于没发生（别的卡读不到对话）。",
        "      · 卡面与任何输入冲突时以卡面为准；",
        "        卡面对交付物的声称，能查证就查证（读文件/跑命令）。",
    ])


def render_card_region_guide(card_type: str, project_class: str) -> str:
    """155 区渲染（卡片区域说明）：讲清正卡三块与影子四节「是什么、怎么写」。

    - **第三块的名字由本树决定**（`rd_project` → 需求文档；其余 → 需求文档）——
      文案里**只出那一个名字**，不并列、不互相提及（用户 2026-10-14 22:04）。
    - **影子四节一并说明**（本卡私有，只有本卡读得到）。
    - **不含「修改历史」**（系统留痕，模型不需要知道）。
    - 无来源（card_type 为空）→ 空串（「没有来源就不出现这个区」）。
    """
    if not card_type:
        return ""
    # [预设职能 P-B 2026-10-24 → 两源模型 2026-11-01] 职能卡不是三块卡:它的
    # 「面」是职责书＋差事账（function_room.summary）,不是台账动态拼装——
    # 台账概念已随用户「卡面是唯一权威事实源,不要第二事实源」退役。
    # [2026-10-31 用户令「判断、警报我明确不需要，拆掉」] 观点簿/警报簿两节
    # 已随 record_verdict / raise_alarm 一起删除,本节不再提。
    if card_type in ("function", "role"):
        return (
            "〔卡片区域说明〕你是职能卡,不是作业卡——你的「卡面」不是三块正文,"
            "是你的职责书与差事记录:\n\n"
            "职责书——你是谁、对什么负责。建卡时定,"
            "经版本化修订(总裁/用户裁定才动)。\n"
            "差事——你被派去哪、去干什么、办完没有(差事账,机器记)。\n\n"
            "你不干活、不建卡、不私记结论;你的专业产出落在被叫到的场:"
            "评审投你的票、验收/执笔把结论写进场卡。\n\n"
            + _FACT_AUTHORITY_FUNCTION
        )
    is_root = (card_type == "project")
    third = _THIRD_BLOCK_NAME_BY_TREE.get(project_class or "", "需求文档")
    subject = "项目" if is_root else "这张卡"
    head = ("〔卡片区域说明〕这张是项目根卡，内容分三个块，各管各的，不要混写："
            if is_root else
            "〔卡片区域说明〕这张卡的内容分三个块，各管各的，不要混写：")
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
        # [错放纠正 2026-11-02] 笔记此前只在 150 区有一行小字，这里从未正式
        # 介绍——它是「过程/备忘/判断依据」的唯一合法去处，模型不知有它，
        # 才会往现状与需求文档里倒。
        "笔记——你自己的备忘与判断：为什么这么做、验证中的猜想、要提醒自己的事。",
        "      写给自己看的，别的卡读不到；它常驻你的上下文，下一轮还读得到。",
        "      写法：一条一事；这是给你自己攒的线索，不是对外文档。",
        "",
        # [事实分层 2026-11-01] 产物清单从「权威位置」降格为「本卡台账」：
        # 对外声称的权威在**现状**块（卡面），影子是私有过程稿——
        # 否则交付物位置有两个权威（tc-2「指针唯一家」与现状「交付物在哪」并立）。
        "产物清单——本卡的产物台账：完成的结果在哪里、哪份文档/代码/数据是准的"
        "（工具卡完工时系统也会自动登记指针）。",
        "      写法：一行一条指针（路径＋用途），只写指针，不搬内容。",
        "      它是台账不是对外声明——沿通信线的卡能读到它；",
        "      「现在交付了什么」的权威声称在**现状**块（别人验收/对接读的是现状）。",
        "",
        "待协作事项——你自己盯的等待账：需要谁提供什么、卡在什么外部条件、进展到哪。",
        "      写法：一条一项，写清“等谁确认什么、确认后本卡才能继续”。",
        "      与现状的分工：**对外要别人知道的卡点写现状**；这里记你自己跟踪的等待明细。",
        "",
        "迭代历史——系统自动记的改动留痕，你不用写。",
        "",
        "进度——不重要，不必特意维护。",
        "",
        # [错放纠正 2026-11-02 · 用户令] 只写禁令不给出路，内容就往最近的块倒。
        # 这张表把「想写的东西 → 该去哪」一次说清。
        "〔写什么该去哪〕想落笔时先对一下：",
        "      · 过程、备忘、为什么这么做、待验证的猜想 → 影子·笔记（edit_note）；",
        "      · 等谁确认什么、卡在外部条件 → 现状「现在卡在哪」（对外）／影子·待协作事项（自己盯）；",
        "      · 文件/文档/代码在哪、哪份是准的 → 影子·产物清单（台账）＋ 现状（对外声明）；",
        "      · 改过什么、什么时候改的、谁改的 → 不写——版本与事件自动留痕，写出来就是噪音；",
        f"      · 对别人成立的决定/约束 → 落成{third}条文本身，不写「某日裁定」。",
        "",
        # [2026-10-27 用户裁定] 「它需要知道的信息，应该出现在上下文里」——
        # 光把子卡内容推上来不够，还得说清这个区是什么，否则模型当它是自己
        # 刚读到的东西，或者干脆当成本卡的一部分。
        "〔直接子卡区〕上下文里有一段「直接子卡（活性·使命·现状）」，那是**系统推给你的**：",
        "      · 只有你的**直接**子卡（一层，不含孙级）；",
        "      · 每卡先有一行**活性**——它此刻在跑（运行中+阶段）还是停了，",
        "        停了再带最后一条信息的落态（正常收口/出错/被中止/被停），",
        "        实时算的，不用为查「活没活着」再去 read_card；",
        "      · 再给**使命与现状**两块——需求文档不在里面，要看得自己 read_card；",
        "      · 原文照给、不截断；有活性的子卡就算两块还空也会出现。",
        "      它和 read_card 读到的是**同一份事实**，不用为了「确认」再读一遍；",
        "      要需求文档、要看孙级，才需要 read_card。",
        "",
        _fact_authority_workcard(third),
    ])


def _child_status_body(child: dict[str, Any]) -> str:
    """子卡「现状」正文：空/占位符（新建卡骨架模板）一律当**未写**。

    占位符判据复用 `card_draft._is_placeholder`（单一口径，避免两处各判一套）。
    延迟 import：`card_draft` 反向依赖本模块，模块级 import 会成环。
    """
    from .card_draft import _is_placeholder  # noqa: PLC0415 延迟 import 破环
    status = str((child or {}).get("status_block") or "")
    return "" if _is_placeholder(status) else status


def _child_mission_body(child: dict[str, Any]) -> str:
    """子卡「使命」正文（[2026-10-27 用户裁定] 使命随现状一起给父卡看）。"""
    from .card_draft import _is_placeholder  # noqa: PLC0415
    mission = str((child or {}).get("mission") or "")
    return "" if _is_placeholder(mission) else mission


def render_upstream_requirements_region(
        entries: list[dict[str, Any]]) -> str:
    """上游需求文档区（order 160）：执行/差事卡的**参照尺**。

    [2026-11-10 · 用户令「需求文档是它们的参照物，是应该直接进入它们上下文的」]
    - 只对 ui/worker/qa_card/pm_card 出（取数层类型闸）；**正稿全文、不截断**——
      需求文档就是 QA 的验收尺 / pm 的执笔底本 / UI 的界面规格，截一字＝
      拿残本判卷（差事 kick 里刻意不截断的同款理由）；
    - 读侧投影：每轮装配现取，上游改了这区就是新的；
    - 父卡还没有正稿 ⇒ 该条不出（没有参照物不硬撑一行）。
    """
    secs: list[str] = []
    for e in entries or []:
        req = str(e.get("requirements") or "").strip()
        if not req:
            continue
        secs.append(f"### {e.get('label')} {e.get('card_id')}"
                    f"（{e.get('title')}）\n\n{req}")
    if not secs:
        return ""
    return ("## 上游需求文档（你的参照尺）\n\n"
            "*干活／验收都对照它。每轮装配现取——上游改了，你看到的这份就是新的；"
            "不用为拿它去 read_card 上卡。*\n\n" + "\n\n".join(secs))


def render_direct_child_status_region(children: list[dict[str, Any]]) -> str:
    """直接子卡区（order 170）：呈现当前卡**直接**子卡的**活性 ＋ 使命 ＋ 现状**。

    - 上游 SQL 只按 `parent=?` 取一层 ⇒ **结构上不可能含孙级**；
    - [2026-11-02 · 用户令「父卡需要看到子卡的实时状态，是在运行中，还是停止了」]
      每个子卡头部一行 **活性**，与 read_card 的「活性」同一出处（装配层算好带来）。
      [2026-11-02 · 活性口径裁定] 活性＝此刻在不在跑（turn_holder）＋最后一条
      信息的落态（回合边界事件）；goal_state 只作目标位标签（已收口/卡住了/
      已暂停），「active」不再冒充"待推进"。
    - [2026-10-27 用户裁定] 使命也进来。依据是实跑复盘：父卡 367 次读直接子卡，
      原文是「我核对一下两张卡的**使命、现状和需求文档**，分别确认哪些已填、
      哪些还空着」——只给现状等于答非所问，它只好自己再读一遍。
    - **需求文档仍不给**（用户裁定：太长）。孙级仍不给（只一层）。
    - 不带修改历史、不碰影子。
    - 空/纯空白/**占位符**（新建卡骨架）都没有信息 ⇒ 该块不出现；
      活性行总在 ⇒ 有活性的子卡即使两块都空也会出现（父卡要知道它活着与否）；
      没有直接子卡 ⇒ 整个区不出现。
    - 子卡顺序由上游按 card_id 固定排序。
    - [2026-10-24 · 用户令「不允许截断,截断不如不发」] **全文注入,不截断**。
    """
    sections: list[str] = []
    for child in children or []:
        card_id = str(child.get("card_id") or "")
        title = str(child.get("title") or "")
        ctype = str(child.get("card_type") or "")
        activity = str(child.get("activity") or "").strip()
        # [worker 无现状块 2026-11-05 改名前为 coder 无现状块 · 用户令] worker 的产出=代码+report_done
        # 回执,现状块对它不存在——活性与使命照给（父卡照样要知道它跑没跑）。
        status = "" if ctype == "worker" else _child_status_body(child)
        mission = _child_mission_body(child)
        if not activity and not status and not mission:
            continue
        parts = [f"### {card_id} · {title}"]
        if activity:
            parts.append(f"**活性**：{activity}")
        if mission:
            parts.append(f"**使命**：{mission}")
        if status:
            parts.append(f"**现状**：\n{status}")
        sections.append("\n".join(parts))
    if not sections:
        return ""
    return "## 直接子卡（活性·使命·现状）\n" + "\n\n".join(sections)


#: [F11 · 展开说明] 区头下挂——教模型沿锚点展开（依赖 memory_expand 工具，F19）
_MEMORY_EXPAND_HOWTO = (
    "- 要看某条结论的**细节与原文**：调 `memory_expand` 工具，`seq` 取锚点行里的"
    " seq（如 @锚点{card_001,12} → seq=12）；它会给出该条的描述层与逐字原文。"
)


def _mem_dt_prefix(row: dict[str, Any]) -> str:
    """首栏日期前缀：``（MM-DD HH:MM）`` 单点事件；``（MM-DD HH:MM→MM-DD HH:MM）``
    覆盖窗口（同日省略重复日期）。

    [2026-11-01 · 记忆/当下可分辨] 归拢 digest 的「结果/当前」是它**覆盖末端**
    时点的快照——只渲起点会让「冻结未宣布（截至 09:06）」与「BD45 签发
    （09:44）」看起来平权，模型分辨不出哪句更新。窗口末端就是它认知的边界。
    """

    def _fmt(ts: str, *, with_date: bool) -> str:
        try:
            dt = datetime.fromisoformat(ts)
            return f"{dt:%m-%d %H:%M}" if with_date else f"{dt:%H:%M}"
        except ValueError:
            return ts.replace("T", " ")

    t_from = str(row.get("timeline_from") or "") or str(row.get("occurred_at") or "")
    t_to = str(row.get("timeline_to") or "")
    if not t_from:
        return f"（→{_fmt(t_to, with_date=True)}）" if t_to else ""
    head = _fmt(t_from, with_date=True)
    if not t_to or t_to == t_from:
        return f"（{head}）"
    try:
        same_day = (datetime.fromisoformat(t_from).date()
                    == datetime.fromisoformat(t_to).date())
    except ValueError:
        same_day = False
    tail = _fmt(t_to, with_date=not same_day)
    if tail == head:
        return f"（{head}）"
    return f"（{head}→{tail}）"


def _render_memory_l1(row: dict[str, Any], *, with_columns: bool) -> list[str]:
    """一条 L1 的渲染行（projectMyMemory 同位）：日期前缀＋gist＋逐栏＋锚点＋文档行。"""
    lines = [f"- {_mem_dt_prefix(row)}{str(row.get('gist') or '')}".rstrip()]
    if with_columns:
        for label, key in (("起因", "start_text"), ("经过", "process"),
                           ("结果", "result"), ("教训", "lesson")):
            v = str(row.get(key) or "").strip()
            if v:
                lines.append(f"  {label}：{v}")
    # 锚点行（原版 @锚点{session,seq}——memory_expand 的寻址依据）
    sid = str(row.get("card_id") or "")
    lines.append(f"  @锚点{{{sid},{int(row.get('anchor_seq') or 0)}}}".rstrip())
    # 文档行（原版 `文档：path @seq N`——F9 机械归属的 docPaths/docRefs）
    docs: list[str] = []
    try:
        dp = json.loads(row.get("doc_paths") or "[]")
    except (ValueError, TypeError):
        dp = []
    try:
        dr = json.loads(row.get("doc_refs") or "[]")
    except (ValueError, TypeError):
        dr = []
    for p in (dp or []):
        first_seq = next((int(r.get("seq") or 0) for r in (dr or [])
                          if isinstance(r, dict) and str(r.get("path") or "") == p), 0)
        docs.append(f"  文档：{p} @seq {first_seq}" if first_seq else f"  文档：{p}")
    lines.extend(docs)
    return lines


def render_memory_region(entries: list[dict[str, Any]]) -> str:
    """200 区渲染（F11 · 2026-10-31 对齐 dsh `projectMyMemory`）。

    **顶层判据 ＝ level==1 ∧ role==gist**（与归拢降层 F2 配套；★ 不过滤 sunk——
    F15 口径：sunk 只影响凝练全景的体积，不淘汰上下文基线）。L2 靠 upper 挂父。

    逐项对照原版 provider.ts：起因/经过/结果/教训逐栏 ✓ 首栏日期前缀 ✓
    锚点行 @锚点{session,seq} ✓ 文档行 文档：path @seq N ✓ 展开说明 ✓
    孤儿 L2 挂段末照出 ✓ L2 出 gist+process 全文 ✓ 近期/远期分段 ✓

    分段口径（沿用 2026-10-29 裁定）：**远期**只出 L1；**最近一批** L1＋L2 全文。
    批次 0 ＝ 时间未知，按远期处理。不截断、不设预算（RD-05）。
    空条目 → 空串。entries 由调用方完成 IO 后传入（纯函数不做 IO）。
    """
    if not entries:
        return ""
    rows = [e for e in entries if isinstance(e, dict)]
    if not rows:
        return ""

    tops = [r for r in rows
            if int(r.get("level") or 1) <= 1 and str(r.get("role") or "gist") == "gist"]
    if not tops and not any(int(r.get("level") or 1) > 1 for r in rows):
        return ""

    # L2 按 upper_id 挂父。归属是沿 upper 链的**传递关系**：折叠递进产生
    # L3+ 深层条目（父是 L2 成员、祖父才是顶层），它们永远有归属，只是不在
    # 顶层——沿链爬得到顶层＝树内材料（随顶层可见性，不进基线）；upper_id
    # 空 / 指向不存在的行 / 链中途断掉或成环＝真孤儿（数据违例，段末照出
    # 备查不静默吞）。
    top_ids = {str(r.get("id") or "") for r in tops}
    by_id = {str(r.get("id") or ""): r for r in rows}

    def _climbs_to_top(node: dict[str, Any]) -> bool:
        cur, seen = node, {str(node.get("id") or "")}
        while True:
            up = str(cur.get("upper_id") or "")
            if not up or up in seen:
                return False
            seen.add(up)
            parent = by_id.get(up)
            if parent is None:
                return False
            if int(parent.get("level") or 1) <= 1:
                return True
            cur = parent

    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    orphans: list[dict[str, Any]] = []
    for r in rows:
        if int(r.get("level") or 1) <= 1:
            continue
        up = str(r.get("upper_id") or "")
        if up and up in top_ids:
            children_by_parent.setdefault(up, []).append(r)
        elif not _climbs_to_top(r):
            orphans.append(r)

    lines = ["## 记忆（凝练归拢）",
             # [2026-11-01 · 记忆/当下可分辨] 用户令：割裂靠「日期+记忆/当下」
             # 让模型自己分辨，不靠合并消灭——条内「结果/当前」是它时戳窗口
             # 末端的快照，未必是此刻；时戳较新的条目优先，当下以卡面为准。
             "- 本区是**记忆——历史记录，不是当下**：条内「结果/最新/当前」指"
             "它时戳窗口末端那个时点；多条并读时时戳较新者为准，判断现状以"
             "卡面为最终依据。",
             "- 结论层给「谁-因-事-果」；细节层只在最近一批展开。",
             _MEMORY_EXPAND_HOWTO]

    def _emit_l1(r: dict[str, Any], *, with_children: bool) -> None:
        lines.extend(_render_memory_l1(r, with_columns=True))
        if not with_children:
            return
        for c in children_by_parent.get(str(r.get("id") or ""), []):
            gist = str(c.get("gist") or "")
            proc = str(c.get("process") or "").strip()
            lines.append(f"  ◇ {gist}".rstrip())
            if proc:
                lines.append(f"    {proc}")

    # 近期/远期分段：批次号升序＝时间序；最后一批＝近期（L1＋L2 全文）
    def _batch(r: dict[str, Any]) -> int:
        try:
            return int(r.get("batch") or 0)
        except (TypeError, ValueError):
            return 0

    # [2026-11-01 · 修批号污染] 「最近一批」只算**原生凝练批**——fold digest 节点
    # 拿的是 sync_nodes 派的流水批号（锚点 ≥1e9 合成段），它若成了"最新批"，
    # 被它降层的成员会以 L2 细节层**全文弹回**，归拢刚减掉的体积立刻回涨。
    _SYNTH = 1_000_000_000
    batches = sorted({_batch(r) for r in tops
                      if int(r.get("anchor_seq") or 0) < _SYNTH})
    last_batch = batches[-1] if batches else 0
    recent = [r for r in tops
              if _batch(r) == last_batch and last_batch > 0
              and int(r.get("anchor_seq") or 0) < _SYNTH]
    older = [r for r in tops
             if not (_batch(r) == last_batch and last_batch > 0
                     and int(r.get("anchor_seq") or 0) < _SYNTH)]

    if older:
        lines.append("")
        lines.append("### 远期")
        # 远期按 timeline_from 排（内容年代优先于流水批号——digest 讲的是它
        # 吸收的那段历史，该插回对应年代而不是堆在尾部）；无 timeline 沉底。
        for r in sorted(older, key=lambda x: (str(x.get("timeline_from") or "\uffff"),
                                              _batch(x), int(x.get("ordinal") or 0))):
            _emit_l1(r, with_children=False)
    if recent:
        lines.append("")
        lines.append(f"### 近期（批{last_batch:04d}，最近一批，附细节层）")
        for r in sorted(recent, key=lambda x: int(x.get("ordinal") or 0)):
            _emit_l1(r, with_children=True)
    if orphans:
        lines.append("")
        lines.append("### 未挂父的细节（父结论不在链上，照出备查）")
        for r in sorted(orphans, key=lambda x: int(x.get("ordinal") or 0)):
            gist = str(r.get("gist") or "")
            proc = str(r.get("process") or "").strip()
            lines.append(f"  ◇ {gist}".rstrip())
            if proc:
                lines.append(f"    {proc}")
    return "\n".join(lines)


def _rel_suffix(kind: str, detail: str) -> str:
    """依赖关联条目的括号内文案：``{kind}·{detail}``（缺项则不写）。"""
    return "·".join(p for p in (kind, detail) if p)


def render_dep_relations_region(
    out_entries: list[dict[str, Any]],
    in_entries: list[dict[str, Any]],
) -> str:
    """250 区渲染（依赖关联）：当前卡的**双向**依赖关联线。

    - 正向：本卡自己的 ``relations`` → 「我依赖/关联：{target}（{kind}·{detail}）」
      ＋目标的**当前**需求文档（对外信息，装配层随边投影——接口改了经关系线
      自动更新，不依赖通知）；
    - 反向：全库扫出的 ``target == 本卡`` 条目 → 「依赖/关联我的：{source}（…）」；
    - 两向都无 → 空串（「没有来源就不出现这个区」）；
    - 条目原样呈现（kind/detail/需求文档不加工、不截断；渲染零加工）。

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
        # [2026-11-01] 依赖线的意义＝我消费它的对外信息。把目标**当前**需求
        # 文档投影进来：它改了卡面，我这里下一轮就是新的——关系线本身即更新
        # 通道，不需要「告诉你变了再自己去读」的通知。
        req = str(r.get("requirements") or "").strip()
        if req:
            lines.append(f"  {target} 的需求文档（随它卡面更新）：")
            lines.extend(f"  {ln}" for ln in req.split("\n"))
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
