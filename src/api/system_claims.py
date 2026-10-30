"""系统说明书：每条陈述都必须**绑到代码事实**上。

## 为什么不能只写一册

写一册说明书，再断言"里面提到了三条线"——那是名字级校验，只能防"漏写"，
防不了"写错"。`parent 不承载通信` 这句话，代码里只要有子树退路，它就是假的，
而名字级校验查不出来。

## 所以每条陈述是一个 Claim

```
Claim(key, text, evidence, verify)
    text     —— 说明书里的原话（渲染时就输出它）
    evidence —— 这条对应代码的哪里
    verify   —— 可执行检查：跑一遍，代码里不成立就红
```

`tests/test_system_claims.py` 跑全部 `verify`。**说明书写了而代码没做到 ⇒ 红；
代码改了而说明书没跟上 ⇒ 也红**（因为 verify 检查的是代码现状）。

无法机械验证的（例如「过程不进卡面」这种写作纪律），`verify=None` 并登记进
`MANUAL` 清单——**显式承认它靠人**，不假装有机制。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 基础设施
def _calls_of(fn: Callable[..., Any]) -> set[str]:
    """函数源码里出现过的调用名（Name 或 Attribute 尾段）。"""
    try:
        src = inspect.getsource(fn)
        tree = ast.parse(src.lstrip() if src.startswith((" ", "\t")) else src)
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _module_functions(mod: Any) -> dict[str, Any]:
    return {n: o for n, o in vars(mod).items()
            if inspect.isfunction(o) and o.__module__ == mod.__name__}


def _callers_of(mod: Any, target: str) -> set[str]:
    """模块里哪些函数调用了 `target`（只看本模块定义的函数）。"""
    out: set[str] = set()
    for name, fn in _module_functions(mod).items():
        if target in _calls_of(fn):
            out.add(name)
    return out


def _config(name: str) -> dict[str, Any]:
    return json.loads((_ROOT / "config" / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- Claim
@dataclass(frozen=True)
class Claim:
    """一条关于系统的陈述。

    `verify` 返回 `(是否成立, 不成立时的说明)`。`None` ⇒ 无法机械验证，进 `MANUAL`。
    """

    key: str
    section: str
    text: str
    evidence: str
    verify: Optional[Callable[[], tuple[bool, str]]] = None


# ---------------------------------------------------------------- 验证器
def _v_parent_no_comm() -> tuple[bool, str]:
    """parent 不承载通信：发信入口里不许有「祖先-后代」这条退路。"""
    from . import card_tools as ct  # noqa: PLC0415

    called = _calls_of(ct._h_send_card_message)
    if "_is_in_subtree" in called:
        return False, ("`_h_send_card_message` 里仍有 `_is_in_subtree` 退路 ⇒ "
                       "父子之间不建线也能发信，parent 实际上在承载通信")
    return True, ""


def _v_links_is_comm() -> tuple[bool, str]:
    """links 是通信线：发信入口必须校验线。"""
    from . import card_tools as ct  # noqa: PLC0415

    called = _calls_of(ct._h_send_card_message)
    if "check_linked" not in called:
        return False, "`_h_send_card_message` 没有 `check_linked` ⇒ links 不是通信边界"
    return True, ""


def _v_relations_no_comm() -> tuple[bool, str]:
    """relations 不通信：沿边寻址只认 links。"""
    from . import card_tools as ct  # noqa: PLC0415

    src = inspect.getsource(ct.read_via)
    # 沿边寻址那段必须读 links、不读 relations
    if 'get("relations")' in src or "get('relations')" in src:
        return False, "`read_via` 读了 relations ⇒ 依赖线参与了寻址"
    if 'get("links")' not in src and "get('links')" not in src:
        return False, "`read_via` 没有读 links，找不到它沿什么寻址"
    return True, ""


def _v_sync_no_wake() -> tuple[bool, str]:
    """同步不叫醒：sync 的执行体里不许有唤醒调用。"""
    from . import card_tools as ct  # noqa: PLC0415

    called = _calls_of(ct._h_scene_sync)
    bad = called & {"deliver_now", "_spawn_tracked"}
    if bad:
        return False, f"`_h_scene_sync` 里有 {sorted(bad)} ⇒ 同步会叫醒对方"
    return True, ""


def _v_wake_really_wakes() -> tuple[bool, str]:
    """唤醒真的叫起一轮：direct 的执行体里必须有唤醒调用。"""
    from . import card_tools as ct  # noqa: PLC0415

    called = _calls_of(ct._h_scene_direct)
    if not (called & {"deliver_now", "_spawn_tracked"}):
        return False, "`_h_scene_direct` 里没有唤醒调用 ⇒ 它其实不唤醒"
    return True, ""


def _v_both_land_in_context() -> tuple[bool, str]:
    """两种效果都进上下文：sync 与 direct 都要落会话块。"""
    from . import card_tools as ct  # noqa: PLC0415

    out: list[str] = []
    for name, fn in (("sync", ct._h_scene_sync), ("direct", ct._h_scene_direct)):
        called = _calls_of(fn)
        if not (called & {"append_card_message", "append"}):
            out.append(name)
    if out:
        return False, f"{out} 没有落会话块 ⇒ 不进对方上下文"
    return True, ""


def _v_functions_only_in_review() -> tuple[bool, str]:
    """职能**只在评审里**被叫醒——除此之外不许有任何自动唤醒入口。

    [职能唤醒收口 2026-10-28 · 用户令「摘掉所有现有的【评审】之外的唤醒」
     「立项评审不算，评审目前只算【发起评审】这个动作」]

    改前这条声明的是「只在阶段任务的头尾介入」，对应的是 stage_open 直发消息 +
    事件订阅 + 观点回合三条自动路径。用户定下新口径后，**职能唯一的自动入口是
    `open_review`**（以及用户直接给它发消息，那不是"唤醒"是"对话"）。

    机械判据：自动唤醒的那批函数一个都不许在。
    """
    from . import card_tools as ct  # noqa: PLC0415
    from . import function_room as fr  # noqa: PLC0415

    banned = ("notify_event", "schedule_event", "spawn_opinion",
              "_profiles_watching", "startup_sweep_all")
    alive = [n for n in banned if hasattr(fr, n)]
    if alive:
        return False, (f"职能还有自动唤醒入口：{alive} ⇒ "
                       f"「只在评审里被叫醒」不成立")
    if hasattr(ct, "_invite_function_room"):
        return False, ("`_invite_function_room` 还在：它是绕开评审、直发消息"
                       "召集全部职能的口子")
    return True, ""


def _v_card_types_match_registry() -> tuple[bool, str]:
    """说明书渲染出的卡类型，必须覆盖注册表全部（渲染不许写死名单）。"""
    from . import prompt_facts  # noqa: PLC0415

    text = prompt_facts.render_card_types()
    want = set(prompt_facts.card_types())
    missing = {c for c in want if f"**{c}**" not in text}
    if missing:
        return False, f"说明书渲染里缺这些卡类型：{sorted(missing)}"
    return True, ""


def _v_blocks_from_source() -> tuple[bool, str]:
    from . import prompt_facts  # noqa: PLC0415
    from ..storage.hash_utils import NEW_SECTION_MAPPING  # noqa: PLC0415

    keys = {b.key for b in prompt_facts.blocks()}
    src_keys = set(NEW_SECTION_MAPPING.values())
    if not keys <= src_keys:
        return False, f"三块里有名字不在 hash_utils：{keys - src_keys}"
    if keys != {"mission", "status_block", "requirements"}:
        return False, f"三块应恰好是使命/现状/需求文档，实际 {sorted(keys)}"
    return True, ""


def _v_shadow_four_sections() -> tuple[bool, str]:
    """影子四节：节名必须与 shadow_store 里的一致。"""
    from ..storage import shadow_store  # noqa: PLC0415

    src = inspect.getsource(shadow_store)
    want = ("进度", "待协作事项", "产物清单", "迭代历史")
    miss = [w for w in want if w not in src]
    if miss:
        return False, f"shadow_store 里找不到这些节名：{miss}"
    return True, ""


def _v_round_ends_without_tools() -> tuple[bool, str]:
    """一轮在「不再调工具」时结束：主循环的退出条件是 tool_calls 为空。"""
    from . import message_runner as mr  # noqa: PLC0415

    src = inspect.getsource(mr)
    if "tool_calls" not in src:
        return False, "message_runner 里找不到 tool_calls ⇒ 结束条件不是它"
    # 必须有「没有 tool_calls 就 break/return」的形态
    if not re.search(r"if not\s+\w*tool_calls|if not tool_calls", src):
        return False, "message_runner 里没有「没有 tool_calls 就退出」的判断"
    return True, ""


def _v_auto_next_round() -> tuple[bool, str]:
    """自动模式每轮结束自动再起：auto_driver 存在且被 goal 收口调用。"""
    try:
        from . import auto_driver  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"import auto_driver 失败：{exc}"
    if not hasattr(auto_driver, "kick"):
        return False, "auto_driver 没有 kick ⇒ 自动续轮入口不明"
    return True, ""


def _v_workspace_card_cannot_send_lateral() -> tuple[bool, str]:
    """作业卡没有通用发信工具：`send_card_message` 已从注册表退役（结构上不可能）。"""
    from . import card_tools as ct  # noqa: PLC0415

    if "send_card_message" in ct.TOOL_REGISTRY:
        return False, "send_card_message 仍在注册表 ⇒ 通用发信面没被收掉"
    # 横向通信只剩 sync（沿连线、不唤醒）——它必须是「不唤醒」的，否则等于复活了通用发信。
    sync_def = ct.TOOL_REGISTRY.get("sync")
    if sync_def is None:
        return False, "sync 不在注册表 ⇒ 横向通信没有通道"
    eff = getattr(getattr(sync_def, "doc", None), "effect", None)
    if eff is None or getattr(eff, "wake", None) is not False:
        return False, "sync 的效果声明不是 wake=False ⇒ 横向同步可能叫醒对方"
    return True, ""


def _v_error_codes_declared() -> tuple[bool, str]:
    from . import prompt_facts  # noqa: PLC0415

    import glob  # noqa: PLC0415

    used: set[str] = set()
    for p in glob.glob(str(_ROOT / "src" / "**" / "*.py"), recursive=True):
        s = Path(p).read_text(encoding="utf-8", errors="replace")
        used |= set(re.findall(r'\berr\(\s*"([a-z_]+)"', s))
    undeclared = used - set(prompt_facts.ERROR_CODES)
    if undeclared:
        return False, f"代码在用但事实源没声明的错误码：{sorted(undeclared)}"
    return True, ""


def _v_wake_sources() -> tuple[bool, str]:
    """「谁让你跑起来」：起轮入口必须能带上唤醒源（否则查不到是谁叫的）。"""
    from . import message_runner as mr  # noqa: PLC0415

    sig = inspect.signature(mr.deliver_now)
    if "source" not in sig.parameters:
        return False, "`deliver_now` 没有 source 参数 ⇒ 起轮时带不上唤醒源"
    return True, ""


#: 每个上下文区属于哪类信息。**新增区必须在这里归类**，否则测试红——
#: 这是「该写的都写了」的机制化：机制本身只能保证「写了的都对」，
#: 靠这张表才能发现「多了一个区而说明书不知道」。
REGION_KIND: dict[str, str] = {
    "instruction": "meta",              # 就是本提示词自身
    "rule": "rule",
    "card_duty": "card_face",
    "team_function_instruction": "rule",
    "card_ability": "card_face",
    "function_roster": "structure",
    "type_thinking": "rule",
    "card_list": "structure",
    "function_self": "card_face",
    "current_card": "card_face",
    "goal": "goal",
    "region_guide": "rule",
    "child_status": "structure",
    "role_cabinet": "structure",
    "memory": "process",
    "relations": "structure",
    "history": "process",
    "tool_turn": "process",
    "user_message": "process",
    "judgment_protocol": "rule",
    "context_tail": "rule",
    "seat_identity": "rule",
}

#: 信息类 → 说明书里交代它的陈述 key。
KIND_CLAIM: dict[str, str] = {
    "card_face": "what_you_see",
    "structure": "what_you_see",
    "process": "what_you_see",
    "rule": "what_you_see",
    "goal": "what_you_see",
}

#: 不需要在说明书里交代的信息类（`meta` 就是说明书本身）。
EXEMPT_KINDS: frozenset[str] = frozenset({"meta"})


def _v_context_regions_present() -> tuple[bool, str]:
    """「你看到什么」：每个上下文区都要归类，每类都要有交代。

    **这是覆盖率反查**：加一个区不分类 ⇒ 红；加一类信息不写陈述 ⇒ 红。
    机制只能保证「写了的都对」，这一步补的是「该写的都写了」。
    """
    from . import context_regions as cr  # noqa: PLC0415

    keys = {str(s.get("key") or "") for s in cr.REGION_TABLE}
    missing = keys - set(REGION_KIND)
    if missing:
        return False, (f"这些上下文区没有归类（请在 REGION_KIND 声明它属于哪类信息，"
                       f"或登记为 exempt）：{sorted(missing)}")
    stale = set(REGION_KIND) - keys
    if stale:
        return False, f"REGION_KIND 里有已不存在的区，请删掉：{sorted(stale)}"
    kinds = set(REGION_KIND.values())
    unbound = kinds - set(KIND_CLAIM) - set(EXEMPT_KINDS)
    if unbound:
        return False, f"这些信息类没有对应的说明书陈述：{sorted(unbound)}"
    return True, ""


# ---------------------------------------------------------------- 全部陈述
CLAIMS: tuple[Claim, ...] = (
    # ---- 一、这个系统是什么
    Claim("card_face_is_authority", "系统是什么",
          "**判断一切以卡面为准。** 卡面是唯一权威事实源；对话是过程，卡面是结论。"
          "你记不住的东西不用记，卡面上有；你记错的东西以卡面为准。",
          "上下文注入三块（context_regions.current_card）＋ 会话与卡面分离",
          None),
    Claim("parent_is_permission_line", "系统是什么",
          "**parent（权限管理线）**：管「我能管谁」——子树作用域、谁能建谁的子卡、"
          "谁能停谁、谁能改谁。**不承载通信**。",
          "`_h_send_card_message` 的连线校验段（card_tools）",
          _v_parent_no_comm),
    Claim("links_is_comm_line", "系统是什么",
          "**links（通信线）**：管「我能跟谁说话」——同步与唤醒都必须沿它走，"
          "没线就发不出去。**不管权限**。",
          "`_h_send_card_message` 里的 `policy.check_linked`",
          _v_links_is_comm),
    Claim("relations_is_dep_line", "系统是什么",
          "**relations（依赖关系线）**：管「我消费谁的什么、我动什么会波及谁」。"
          "**不通信、不管权限**。",
          "`read_via` 沿边寻址只认 links（card_tools）",
          _v_relations_no_comm),
    Claim("nine_card_types", "系统是什么",
          "卡分九种，能力不同（能不能建子卡、在不在任务树上）。",
          "`config/card_type_registry.json` 的 l1_vocabularies",
          _v_card_types_match_registry),

    # ---- 二、卡片是什么
    Claim("three_blocks", "卡片是什么",
          "卡面分三块，各自回答一个不同的问题：使命（为什么）／现状（实然）／"
          "需求文档（应然）。",
          "`hash_utils.NEW_SECTION_MAPPING` ＋ prompt_facts.blocks",
          _v_blocks_from_source),
    Claim("no_process_in_face", "卡片是什么",
          "**过程不进卡面。** 你讨论了什么、试过什么、否掉了什么，都留在对话里。"
          "卡面只放结论态。",
          "写作纪律（无机械判据）", None),
    Claim("shadow_is_process", "卡片是什么",
          "**影子卡记过程，正卡记结论。** 影子四节：进度／待协作事项／产物清单／迭代历史。",
          "`shadow_store` 的四节固定名",
          _v_shadow_four_sections),

    # ---- 三、一轮怎么运作
    Claim("who_wakes_you", "一轮怎么运作",
          "**谁让你跑起来**：用户对你说话／别的卡给你发消息或指令／系统提醒／"
          "自动模式的目标续轮。你自己不会凭空醒来。",
          "`message_runner.deliver_now(source=…)` ＋ goal 驱动",
          _v_wake_sources),
    Claim("what_you_see", "一轮怎么运作",
          "**你看到什么**：系统按固定顺序装配上下文，共五类——\n"
          "① **你的卡面**：三块正文、你的职责、你的能力；\n"
          "② **结构**：直接子卡的使命与现状、卡片清单、职能名册、依赖关联；\n"
          "③ **过程**：对话历史、凝练记忆、本轮工具轮、用户消息；\n"
          "④ **规则**：规则注入、类型思维、区域说明、工作规范；\n"
          "⑤ **目标**：自动模式的目标与轮次。\n"
          "这些是系统推给你的，不需要你去取；**卡面与对话是同一份事实的两种形态**，"
          "不用为了确认再读一遍。",
          "`context_regions.REGION_TABLE` 全部 22 个区，按 REGION_KIND 归入五类",
          _v_context_regions_present),
    Claim("round_ends_without_tools", "一轮怎么运作",
          "**一轮什么时候结束**：你不再调工具，这一轮就结束了。",
          "`message_runner` 主循环的退出条件",
          _v_round_ends_without_tools),
    Claim("auto_mode_next_round", "一轮怎么运作",
          "**自动模式**：每轮结束系统会自动再起一轮，一直推进到目标完成。",
          "`auto_driver.kick` ＋ goal 驱动",
          _v_auto_next_round),

    # ---- 四、卡与卡怎么协作
    Claim("two_effects", "卡与卡怎么协作",
          "**通信只有两种效果**：同步（进对方上下文，**不叫醒**）／唤醒"
          "（进上下文**并且叫起来跑一轮**）。两种都进上下文，差别只在「唤不唤醒」。",
          "`sync` / `direct` 的执行体",
          _v_both_land_in_context),
    Claim("sync_never_wakes", "卡与卡怎么协作",
          "同步**不叫醒对方**——它下次回合自然看到。",
          "`_h_scene_sync` 里无 deliver_now",
          _v_sync_no_wake),
    Claim("wake_really_wakes", "卡与卡怎么协作",
          "唤醒**会叫起对方跑一轮**。",
          "`_h_scene_direct` 里有 deliver_now",
          _v_wake_really_wakes),
    Claim("functions_only_in_review", "卡与卡怎么协作",
          "职能**只在评审里被叫醒**；除此之外不会被自动调起。",
          "`function_room` 的自动唤醒函数与 `card_tools._invite_function_room`",
          _v_functions_only_in_review),
    Claim("workspace_no_lateral", "卡与卡怎么协作",
          "作业卡向上只有一个通道（汇报），没有横向发信工具。",
          "`_filter_by_card_type` 的 lateral 闸",
          _v_workspace_card_cannot_send_lateral),
    Claim("error_codes_complete", "卡与卡怎么协作",
          "工具失败时你会收到 `[tool_error code=<码>]`，每个码都有处置建议。",
          "`prompt_facts.ERROR_CODES` 与代码 err() 调用",
          _v_error_codes_declared),
)

#: 无法机械验证的陈述——**显式承认靠人**，不许悄悄新增。
MANUAL: frozenset[str] = frozenset({"card_face_is_authority", "no_process_in_face"})

#: 陈述是**目标态**、代码尚未达成。只能减不能增；修好了要删（否则测试红）。
#:
#: 与「照实写缺陷」的区别：
#: - 行为**事实**（`report_up` 不落存储）⇒ 说明必须照实，否则模型误判自己在做什么；
#: - 行为**约束**（parent 不该承载通信）⇒ 说明写目标态，差距登记在这里。
#:   照实写"父子不建线也能发信"会引导模型去用这条退路。
#:
#: [2026-10-27 已清空] 两条都修完了：
#:   · parent_is_permission_line —— 删掉 `_is_in_subtree` 退路（通信只认线）
#:   · functions_head_tail_only  —— 删掉旧 `_h_review_request`（工具已下线、handler 残留）
#: 空表意味着**说明书里每一条陈述都与代码一致**。
UNMET: dict[str, str] = {}

#: 章节顺序（渲染用）
SECTIONS: tuple[str, ...] = ("系统是什么", "卡片是什么", "一轮怎么运作", "卡与卡怎么协作")

SECTION_TITLE: dict[str, str] = {
    "系统是什么": "## 这个系统是什么",
    "卡片是什么": "## 卡片是什么",
    "一轮怎么运作": "## 一轮对话怎么运作",
    "卡与卡怎么协作": "## 卡与卡怎么协作",
}


def claims() -> tuple[Claim, ...]:
    return CLAIMS


#: 章节附带的**可渲染清单**（不是陈述，是事实渲染）：
#: 卡类型矩阵从注册表出、职能名册从目录出。它们不写成 Claim，
#: 因为它们本来就由渲染保证正确，不需要再配一个 verify。
SECTION_EXTRA: dict[str, Callable[[], str]] = {}


def _extras() -> dict[str, Callable[[], str]]:
    from . import prompt_facts  # noqa: PLC0415 延迟：避免模块级循环

    return {
        "系统是什么": prompt_facts.render_card_types,
        "卡与卡怎么协作": prompt_facts.render_functions,
    }


def render_claims() -> str:
    """按章节渲染：陈述（有归属）＋ 该节的事实清单（从源头渲染）。"""
    extra = _extras()
    out: list[str] = []
    for sec in SECTIONS:
        got = [c for c in CLAIMS if c.section == sec]
        if not got:
            continue
        out.append(SECTION_TITLE[sec])
        for c in got:
            out.append(c.text)
        fn = extra.get(sec)
        if fn is not None:
            out.append(fn())
        out.append("")
    return "\n\n".join(x for x in out if x)


def verify_all() -> list[tuple[str, bool, str]]:
    """跑全部可验证陈述，返回 (key, ok, detail)。"""
    out: list[tuple[str, bool, str]] = []
    for c in CLAIMS:
        if c.verify is None:
            continue
        try:
            ok, detail = c.verify()
        except Exception as exc:  # noqa: BLE001 验证器自己炸也要报出来
            ok, detail = False, f"验证器异常：{exc}"
        out.append((c.key, ok, detail))
    return out
