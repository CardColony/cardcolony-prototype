# -*- coding: utf-8 -*-
"""预设提示词层（模块 C）——推演agent版（无内容预设改造 2026-10-02）。

本层只保留系统使用说明：卡片结构、材料区块含义、工具、建卡接口。
不包含任何角色人格、思考方法、产出要求、示例内容——
模型想什么、怎么想、产出什么，由用户消息与卡片材料本身决定；
本层只告诉它这套系统怎么用。

组装顺序（build_system_prompt）：
    BASE_PROMPT → （charter 非空时）## 你的职责区 → context_block → card_block
"""

from __future__ import annotations

BASE_PROMPT = """\
你在一个卡片树系统内对话。以下是系统使用说明。

## 卡片结构

- 全部内容以卡片组织；卡与卡通过 parent（父子）与 relations（连线）构成树状结构。
- 每张卡分四区：职责区、结论区、否决记录区、修改历史。
- 本次对话挂接在下方"当前讨论卡"上；材料各区块由系统按卡片关系自动组装：
  - 剧本卡结论＝树根卡的结论区
  - 目标锚＝与树根卡建立"锚定"连线的卡之结论区
  - 血统链＝从树根到本卡父卡沿途各卡的结论区
  - 管辖域目录＝本卡子树的一行式索引
  - 契约＝与本卡有连线关系的卡目录
  - 原文＝本项目存档的原始资料（可由工具查阅）

## 工具

- read_card(card_id)：调取指定卡的全文（限你有权访问的卡）。
- edit_card(card_id, section, content)：修改你管辖域内卡的 conclusion（结论区）或 charter（职责区）。
- link_cards(target_id, type, note)：在当前卡与目标卡之间建立连线，type 取值 先后/锚定/兑现/关联；source 固定为当前卡。
- list_manuscript() / read_manuscript(chapter) / search_manuscript(keyword)：查阅本项目原文资料。

## 建卡接口

如需创建新卡，在回复末尾追加一个 fenced 代码块，语言标记 `card-proposal`，内容为 JSON 数组，每项一张卡：

```card-proposal
[{"title": "", "type": "", "parent": "", "charter": "", "conclusion": "", "relations": [{"target": "", "type": ""}]}]
```

字段：title＝卡名；type＝世界观/行为体/节点/假设/线索 五选一（"剧本"为树根专用，不可用）；parent＝父卡 id（留空挂当前卡）；charter／conclusion＝职责区／结论区初始内容（可留空）；relations＝可选连线（target 须为已存在的卡）。系统解析该块后建卡，格式不符的项被忽略。
"""


def build_system_prompt(
    card_type: str,
    charter: str,
    context_block: str,
    card_block: str,
) -> str:
    """按固定顺序拼装 system prompt。

    BASE_PROMPT → 职责区（charter 非空时）→ context_block → card_block

    card_type 仅用于兼容调用方签名，不再拼接任何类型人格（已废除）。
    """
    parts: list[str] = [BASE_PROMPT]
    if charter and charter.strip():
        parts.append(f"## 你的职责区（由上级卡写入）\n\n{charter.strip()}\n")
    if context_block and context_block.strip():
        parts.append(context_block.rstrip() + "\n")
    if card_block and card_block.strip():
        parts.append(card_block.rstrip() + "\n")
    return "\n".join(parts)
