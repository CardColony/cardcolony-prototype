"""工具说明的唯一源。

## 一条铁律

**工具说明只有一份源**（`ToolDoc`）。两处呈现——tools 参数的 `description`
与 system prompt 里的工具节——都从它渲染，不许各自手写。

## 为什么

改前是三份：schema 里手写的 `description`、`prompts._SEC_TOOLS`（25 件）、
`prompts._SEC_TOOLS_AUTO`（35 件，整段复制 + 10 件追加）。实测三者互相矛盾：

- 提示词告诉一张作业卡「你有 bash / write_file / python」，它一件都没有；
- 提示词对 `sync` / `direct` / `report_up` 一个字不提，而这三件是线上调用
  最多的通信工具（446 / 147 / 151 次）；
- `sync` 的说明写「只入收件箱」，而实现早已改落会话块（`messages` 表
  `msg_sync=0` 即证）。

## 加一件工具必须触发的一系列改动

`ToolDoc` 四个字段全是必填，构造期就校验：

- `group`：能力域（决定它在工具节里排哪一组）
- `scene`：**什么时候该用**（判断性，只能手写）
- `usage`：**怎么用、坑在哪**（判断性，只能手写）
- `effect`：**效果事实**（落哪、唤不唤醒、要不要线）——必须与实现一致，
  由 `tests/test_tool_doc_facts.py` 的 AST 校验对照 handler 源码。

`effect` 声明了而 handler 里找不到对应调用 ⇒ 测试红。这条是「改动必须对照
事实」的落地：不是对照人写的散文，是对照代码实际做了什么。
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- 能力域
#: 工具节的编排顺序（也是「能力地图」的分组）。加新组要同时想清楚它排在
#: 哪一组旁边——相邻关系本身就是模型判断「什么时候用哪个」的线索。
GROUP_ORDER: tuple[str, ...] = (
    "读卡",      # 看事实
    "写卡面",    # 改事实
    "通信",      # 同步 / 唤醒（link 线）
    "依赖",      # 依赖 / 影响（relation 线）——不是通信
    "组织",      # 建卡、拆卡、换父、归档
    "治理",      # 停、沉默、唤醒、阶段
    "评审",      # 伴生评审卡
    "目标",      # goal 域
    "职能",      # 职能台账与指令
    "文件",      # 磁盘读写
    "执行",      # bash / python / 工具卡
    "联网",      # 搜索与抓取
)

#: 各能力域的一句话职责——写在工具节每组的小标题下，给模型「这组管什么」。
GROUP_DUTY: dict[str, str] = {
    "读卡": "看别处的事实。改之前先读，别凭印象。",
    "写卡面": "改卡面上的事实。只写能核实的，过程不进卡面。",
    "通信": "把话送到另一张卡。同步不叫醒，唤醒叫起来干活。",
    "依赖": "声明我消费谁的什么、我动什么会波及谁。它不通信，但决定你能不能孤立迭代。",
    "组织": "改任务树的形状：建卡、拆卡、换父、归档。",
    "治理": "改别的卡的状态：停、沉默、唤醒、开阶段。",
    "评审": "在伴生评审卡上过闸：定判据、表态、收口。",
    "目标": "自动模式的跨轮推进：读目标、改目标、拉回停下的子卡。",
    "职能": "职能岗的台账与指令：判词、警报、指令、指令核验。",
    "文件": "磁盘上的读写改删。写之前先读，改之前先备份。",
    "执行": "跑命令、跑脚本、调工具卡。失败不是错误，由你判断。",
    "联网": "查网页、抓正文。结果是外部信息，要标来源。",
}


@dataclass(frozen=True)
class ToolEffect:
    """工具的效果事实——**必须与实现一致**，由 AST 校验对照。

    sink：
      - ``session_block``：落对方上下文（会话块，永久、进 prior_messages）
      - ``inbox``：落待处理收件箱（有状态、要主动取）
      - ``card``：改卡面三块
      - ``shadow``：改影子（过程记录四节／私有笔记）——不走 update_card，
        走 shadow_store.write_section
      - ``none``：不产生消息/卡面副作用（纯读、纯控制）
    wake：是否叫起目标卡一轮（handler 里必须能找到 deliver_now / _spawn_tracked）
    wire：是否要求两卡之间已有通信线（handler 里必须能找到 check_linked /
          _require_wire_switch(SWITCH_SEND_MESSAGE)）
    targets：收件人算法（``explicit`` / ``parent`` / ``ancestors`` / ``links`` /
             ``all_functions`` / ``self`` / ``caller`` / ``""``）
    delegate：本工具是别的工具的壳时，写被委托的工具名（AST 校验跳过本层）
    """

    sink: str = "none"
    wake: bool = False
    wire: bool = False
    targets: str = ""
    delegate: str = ""

    def __post_init__(self) -> None:
        if self.sink not in ("none", "session_block", "inbox", "card", "shadow"):
            raise ValueError(f"ToolEffect.sink 非法：{self.sink!r}")


@dataclass(frozen=True)
class ToolDoc:
    """工具说明的唯一源。四个字段全必填——**漏一个就不许注册**。"""

    group: str
    scene: str
    usage: str
    effect: ToolEffect = field(default_factory=ToolEffect)

    def __post_init__(self) -> None:
        if self.group not in GROUP_ORDER:
            raise ValueError(
                f"ToolDoc.group 非法：{self.group!r}（须是 GROUP_ORDER 之一）")
        if not str(self.scene or "").strip():
            raise ValueError("ToolDoc.scene 必填：什么时候该用这件工具")
        if not str(self.usage or "").strip():
            raise ValueError("ToolDoc.usage 必填：怎么用、坑在哪")


# ---------------------------------------------------------------- 渲染
_SINK_ZH = {
    "session_block": "落对方上下文（会话块，永久可读，进它下一轮）",
    "inbox": "落对方收件箱（有状态，它得主动取才看得到）",
    "card": "改卡面三块",
    "none": "",
}


def render_effect(e: ToolEffect) -> str:
    """效果事实 → 一句人话。**只从声明渲染，不读散文**。

    只写**这个工具真的会做的事**：
    - 纯读／纯写卡面字段（sink=none 且不唤醒）⇒ 空串，不占版面；
    - 唤醒但不落存储 ⇒ 必须明说「不落任何存储」——这是模型最该知道的事实，
      不说就等于让它在不知情的情况下汇报（`report_up` 现状即此）。
    """
    if e.delegate:
        return f"本工具是 `{e.delegate}` 的壳，效果同它"
    bits: list[str] = []
    zh = _SINK_ZH.get(e.sink, "")
    if zh:
        bits.append(zh)
    if e.wake:
        bits.append("**会叫起对方跑一轮**")
        if not zh:
            bits.append("但**不落任何存储**——正文只活在它这一轮的输入里，"
                        "下一轮就查不到了")
    elif zh:
        bits.append("不唤醒对方")
    if e.wire:
        bits.append("要求两卡之间已有通信线（没有先 link_cards）")
    return "；".join(bits)


def render_description(name: str, doc: ToolDoc) -> str:
    """唯一渲染入口：场景 ＋ 用法 ＋ 效果。tools 参数与提示词都用它。"""
    parts = [f"**什么时候用**：{doc.scene}", f"**怎么用**：{doc.usage}"]
    eff = render_effect(doc.effect)
    if eff:
        parts.append(f"**效果**：{eff}")
    return "\n".join(parts)


def render_section(names: list[str], docs: dict[str, ToolDoc]) -> str:
    """按能力域渲染整个工具节——**只渲染实际下发的工具**（结构性防逾越）。"""
    by_group: dict[str, list[str]] = {}
    for n in names:
        d = docs.get(n)
        if d is None:
            continue
        by_group.setdefault(d.group, []).append(n)
    out: list[str] = ["## 工具"]
    for g in GROUP_ORDER:
        got = by_group.get(g) or []
        if not got:
            continue
        out.append(f"\n### {g}——{GROUP_DUTY.get(g, '')}")
        for n in got:
            out.append(f"- **{n}**：{render_description(n, docs[n])}")
    return "\n".join(out)


# ---------------------------------------------------------------- 事实校验
#: 声明 → 必须在 handler 源码里出现的调用名（任一命中即算）
_EVIDENCE: dict[tuple[str, str], tuple[str, ...]] = {
    ("sink", "session_block"): ("append_card_message", "session_facade.append"),
    ("sink", "inbox"): ("append_inbox",),
    ("sink", "card"): ("update_card",),
    # [影子精准编辑 2026-10-27] 影子的写入走 shadow_store，不经 card_store.update_card
    ("sink", "shadow"): ("write_section", "write_note"),
    ("wake", "True"): ("deliver_now", "_spawn_tracked"),
    ("wire", "True"): ("check_linked", "_require_wire_switch"),
}


def _called_names(handler: Callable[..., Any]) -> set[str]:
    """handler 源码里出现过的**调用名**（函数名 + 属性名尾段）。"""
    try:
        src = inspect.getsource(handler)
        tree = ast.parse(src.lstrip() if src.startswith((" ", "\t")) else src)
    except Exception:  # noqa: BLE001 取不到源码就不校验（不误报）
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


def verify_effect(name: str, doc: ToolDoc,
                  handler: Callable[..., Any]) -> list[str]:
    """对照事实：声明了的效果，handler 里必须找得到对应调用。

    返回问题列表（空＝一致）。**只做「声明了就必须有」这一个方向**——
    反方向（没声明却有）会误报壳函数与间接调用，等声明全填齐后再收紧。
    """
    e = doc.effect
    if e.delegate:
        return []          # 壳：效果由被委托的那件负责
    called = _called_names(handler)
    if not called:
        return []          # 取不到源码，不误报
    bad: list[str] = []
    for (kind, val), evid in _EVIDENCE.items():
        declared = {"sink": e.sink, "wake": str(e.wake), "wire": str(e.wire)}[kind]
        if str(declared) != val:
            continue
        if not any(x.split(".")[-1] in called for x in evid):
            bad.append(f"{name}: 声明 {kind}={val}，但 handler 里找不到 "
                       f"{'/'.join(evid)} 的调用")
    return bad
