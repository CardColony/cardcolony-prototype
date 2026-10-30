"""元提示词的事实源。

## 元提示词里混着两种东西

- **事实**：系统长什么样——有几条线、几块、哪些工具／消息类型／错误码／卡类型。
  事实**会变**（加工具、改字段、加类型），手写必然过期。
- **判断**：该怎么做事——「过程不进卡面」「汇报写证据不自述成功」。
  判断**不会变**，手写是对的。

所以机制只有一句话：**事实不许手写，判断不许自动生成。**

## 这个模块装什么

每一类事实在这里**只有一个源**。提示词从它渲染，`scripts/prompt_audit.py`
拿它对照现有提示词，`tests/test_prompt_facts.py` 钉住「源头与代码一致」。

## 为什么先要有这个模块

改前「三条线」根本没有事实源：职责散在代码注释、提示词、字段名三处，
谁都能写一版。没有源，就谈不上「保证准确」——只能靠人记得同步。

实测（2026-10-27）元提示词与代码的四处不符：

- `run_tool_card` 提示词里写着，注册表里没有（handler 在，但从没注册）⇒ 幽灵工具
- 消息类型 7 种只提 3 种；错误码 7 种只提 5 种；卡类型 9 种只提 5 种
- `parent` 只写「父子」，没写它是**权限管理线**
- `hash_utils.py:186` 注释还写「新区名…定论」，而 106 行早已统一为「需求文档」
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 三条线
@dataclass(frozen=True)
class Line:
    """一条线：管什么、**不管什么**。

    「不管什么」比「管什么」更要紧——三条线被混用，都是因为只知道它管什么。
    """

    key: str
    name: str
    duty: str
    cannot: str


#: 三种线（用户 2026-10-27 裁定）。**这是唯一声明处**，提示词与文档都从这里取。
LINES: tuple[Line, ...] = (
    Line("parent", "权限管理线",
         "管「我能管谁」：子树作用域、谁能建谁的子卡、谁能停谁、谁能改谁。",
         "**不承载通信**。父子之间要发消息，也得走 links（建子卡时默认连一条）。"),
    Line("links", "通信线",
         "管「我能跟谁说话」：同步与唤醒都必须沿它走，没线就发不出去。",
         "**不管权限**。有通信线不等于有写权，能说话不等于能改。"),
    Line("relations", "依赖关系线",
         "管「我消费谁的什么、我动什么会波及谁」：依赖的内容／功能／接口，"
         "以及反过来我会影响谁。",
         "**不通信、不管权限**。它只让信息常驻上下文，让你迭代时知道自己不孤立。"),
)


def render_lines() -> str:
    out = ["卡与卡之间有三种线，各管一件事，不能互相替代："]
    for ln in LINES:
        out.append(f"- **{ln.key}（{ln.name}）**：{ln.duty}{ln.cannot}")
    return "\n".join(out)


# ---------------------------------------------------------------- 三块
@dataclass(frozen=True)
class Block:
    key: str        # 库列名
    heading: str    # 区头（不含 ## ）
    question: str   # 它回答什么问题
    test: str       # 怎么判断写对了


#: 卡面三块。区头从 `hash_utils.NEW_SECTION_MAPPING` 派生（唯一源在那），
#: 这里只补「回答什么问题」与「怎么算写对」——那是判断，代码推不出来。
_BLOCK_DOC: dict[str, tuple[str, str, str]] = {
    "mission": ("使命", "为什么做这件事",
                "删掉卡面其余全部、只留它，还知道为什么做"),
    "status_block": ("现状", "现在实际是什么样（实然）",
                     "每句话都能被磁盘／日志／命令输出核实；核实不了的就不是现状"),
    "requirements": ("需求文档", "做到什么样算完成（应然）",
                "一个不知道上下文的人只看它，能不能判断交付物合不合格"),
}


def blocks() -> tuple[Block, ...]:
    """三块（不含「修改历史」——那是系统记的留痕，不是内容块）。

    按**库列名去重**：`NEW_SECTION_MAPPING` 里「## 需求文档」与「## 定论」都映射到
    `requirements`（后者是存量卡保形用的旧区头），不能因此渲染出两个"定论"。
    """
    from ..storage.hash_utils import NEW_SECTION_MAPPING  # noqa: PLC0415 延迟

    seen: dict[str, Block] = {}
    for heading, key in NEW_SECTION_MAPPING.items():
        if key not in _BLOCK_DOC or key in seen:
            continue
        name, q, test = _BLOCK_DOC[key]
        seen[key] = Block(key=key, heading=name, question=q, test=test)
    # 按三块的自然顺序（使命→现状→需求文档），不按 mapping 的文件序
    order = ("mission", "status_block", "requirements")
    return tuple(seen[k] for k in order if k in seen)


def render_blocks() -> str:
    out = ["卡面分三块，各自回答一个不同的问题："]
    for b in blocks():
        out.append(f"- **{b.heading}**：回答「{b.question}」。"
                   f"写得对不对，看一条：{b.test}。")
    return "\n".join(out)


# ---------------------------------------------------------------- 错误码
#: 工具错误码。**这是唯一枚举**——`tests/test_prompt_facts.py` 断言代码里
#: 用到的每个 `err("...")` code 都在这里，新增 code 不改这张表就红。
ERROR_CODES: dict[str, str] = {
    "denied": "权限不允许（目标不在你能管的范围）。换目标，不要重试。",
    "not_found": "目标不存在。先用只读工具确认真实存在再行动。",
    "locked": "目标正被占用。稍后重试一次；仍失败则放弃该目标并说明。",
    "validation": "参数缺失或不合法。按工具定义修正参数后重试。",
    "invalid": "参数在语义上不成立（例如给自己发消息、类型对不上）。"
               "换个做法，不要原样重试。",
    "ambiguous": "匹配到多处、无法确定改哪一处。带上更多上下文让它唯一，"
                 "或明确要求全部替换。",
    "internal": "系统内部错误。不要重试同一调用，改用其他路径或如实说明。",
}


def render_error_codes() -> str:
    out = ["工具失败时你会收到 `[tool_error code=<码>] <说明>`："]
    for code, advice in ERROR_CODES.items():
        out.append(f"- {code}：{advice}")
    return "\n".join(out)


# ---------------------------------------------------------------- 消息类型
def msg_types() -> list[tuple[str, str]]:
    """(类型码, 显示名) —— 从 `card_msg` 注册表取，不手写。

    只取**在役**类型（`deprecated` 的旧类型不进提示词，但注册表里留着——
    历史消息还要渲染）。
    """
    from . import card_msg  # noqa: PLC0415 延迟
    return [(c, card_msg.display_of(c)) for c in card_msg.active_type_codes()]


def render_msg_types() -> str:
    out = ["卡间消息的类型（系统注册表维护，新增自动纳入）："]
    for code, disp in msg_types():
        out.append(f"- {code}（{disp}）")
    return "\n".join(out)


# ---------------------------------------------------------------- 卡类型
def card_types() -> list[str]:
    """全部卡类型码 —— 从 `card_type_registry.json` 取。"""
    d = json.loads((_ROOT / "config" / "card_type_registry.json")
                   .read_text(encoding="utf-8"))
    out: list[str] = []
    for v in (d.get("l1_vocabularies") or {}).values():
        for t in (v.get("types") or []):
            code = str(t.get("code") or "")
            if code and code not in out:
                out.append(code)
    return out


def card_type_matrix() -> list[dict[str, Any]]:
    """卡类型 ＋ 能力（能否建子卡／是否离树）。从注册表取，不手写。"""
    d = json.loads((_ROOT / "config" / "card_type_registry.json")
                   .read_text(encoding="utf-8"))
    seen: dict[str, dict[str, Any]] = {}
    for v in (d.get("l1_vocabularies") or {}).values():
        for t in (v.get("types") or []):
            code = str(t.get("code") or "")
            if not code or code in seen:
                continue
            seen[code] = {
                "code": code,
                "label": str(t.get("label") or code),
                "children": bool(t.get("can_have_children")),
                "off_tree": bool(t.get("off_tree")),
            }
    return list(seen.values())


def render_card_types() -> str:
    out = ["卡分九种，能力不同（能不能建子卡、在不在任务树上）："]
    for t in card_type_matrix():
        bits = [t["label"],
                "能建子卡" if t["children"] else "不能建子卡",
                "离树" if t["off_tree"] else "在任务树上"]
        out.append(f"- **{t['code']}**：{'，'.join(bits)}")
    out.append("")
    out.append("**离树＝不在任务树上**：它没有父子作用域，也不会出现在别人的"
               "「直接子卡」里。职能卡与评审卡都是离树的。")
    return "\n".join(out)


# ---------------------------------------------------------------- 职能
#: 每个职能「管什么」的一句话——**判断性内容，手写**。
#: `charter` 是职能自己的职责书（全文，不截断），那是它自己的上下文；
#: 别的卡只需要知道「有哪些岗、各自管什么」。手写的代价靠断言兜：
#: 目录里新增一个职能而这里没写 brief ⇒ 测试红。
FUNCTION_BRIEF: dict[str, str] = {
    "ceo": "对用户唯一的汇报口，全局决策的收口点，引导块（需求文档／裁定）的执笔人。",
    "pm": "需求与前提的持有者：这份工作解决的是不是用户要的问题，前提有没有来源。",
    "cto": "技术方向与标准的持有者：方案是否与既定裁定一致，有没有被否掉的方向复活。",
    "qa": "可验收性的持有者：判据开工前落面，收口按判据表态，证据以三方对账为准。",
    "emergence_keeper": "涌现观测站：审计度量真实性，报告「看起来健康」的假信号。",
}


def functions() -> list[dict[str, Any]]:
    """职能目录条目 —— 从 `function_catalog.json` 取。"""
    d = json.loads((_ROOT / "config" / "function_catalog.json")
                   .read_text(encoding="utf-8"))
    return [f for f in (d.get("functions") or [])
            if isinstance(f, dict) and f.get("code")]


def render_functions() -> str:
    """职能名册：名字 ＋ 一句话管什么。**不截断 charter**——它不进别的卡的提示词。"""
    out = ["本项目的职能岗（专家岗，只在需求评审时被叫来）："]
    for f in functions():
        code = str(f.get("code") or "")
        brief = FUNCTION_BRIEF.get(code) or ""
        out.append(f"- **{f.get('name') or code}**（{code}）：{brief}")
    return "\n".join(out)
