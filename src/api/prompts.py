# -*- coding: utf-8 -*-
"""系统使用说明层（白板版 2026-10-02）。

只描述系统如何使用：卡片结构事实、工具、建卡接口的机械格式。
不含角色、思考方法、产出要求、类型菜单、示例内容。

分工（用户裁定 2026-10-02 15:07）：
- 三思考席：只做分析——system prompt 仅为中性上下文（卡片清单＋当前卡），
  零指令、零工具、零建卡接口。
- 主模型（统合官）：唯一拆卡出口——收到 BASE_PROMPT（使用说明）＋中性上下文。
"""

from __future__ import annotations

BASE_PROMPT = """\
你在一个卡片系统内工作。以下是系统使用说明。

## 系统结构

- 全部内容以卡片组织；卡与卡通过 parent（父子）与 relations（连线）构成树状结构。
- 本次对话挂接在下方"当前卡"上；系统全部卡片的清单附在下方。

## 工具

- read_card(card_id)：读取指定卡（标题、父卡、连线、正文）。只能读取你有权访问的卡。
- edit_card(card_id, content)：改写你管辖范围内指定卡的正文（覆盖写）。
- delete_card(card_id)：删除你管辖范围内指定编号的卡片及其对话。剧本根卡与有子卡的卡不可删除。
- update_shadow(content)：覆盖写当前卡自己的影子卡（过程性记录，正文三节：进度/产物清单/迭代历史）。无 card_id 参数——只能写自己的影子。
- read_shadow(card_id)：读取指定卡的影子卡全文（card_id 缺省＝当前卡）。上级卡用它查看子卡的影子——子卡影子的更新由此到达上级。
- link_cards(target_id, type, note)：在当前卡与目标卡之间建立连线（type：先后/关联）。
- unlink_cards(target_id, type)：解除当前卡上指向目标卡的连线；目标卡已删时也可用（清理悬空线）。type 缺省解除全部指向该目标的连线。
- list_manuscript() / read_manuscript(chapter) / search_manuscript(keyword)：查阅本项目存档的原始资料。
- web_search(query)：查询网页搜索服务，返回结果列表（标题、链接、摘要、来源、日期）。
- fetch_web(url)：获取指定网页的文本内容。
- list_files(path, limit)：列出一个目录下的条目（一层）。范围限 D:\AI_station 下的 trading-workspace 与 推演agent 两个目录。
- read_file(path, para_start, para_end)：读取一个文本文件的内容，可按行区间分段。二进制与凭证类文件不可读。
- search_files(keyword, path, limit)：在目录下逐文件检索关键词（不区分大小写），返回命中的文件、行号与该行内容。
- write_file(path, content)：写入一个文本文件（已存在则覆盖，覆盖前原文件自动备份到 _backups 下）。单次上限 5MB。

## 建卡接口

如需创建新卡，在回复末尾追加一个 fenced 代码块，语言标记 `card-proposal`，内容为 JSON 数组，每项一张卡：

```card-proposal
[{"title": "", "parent": "", "content": ""}]
```

字段：title＝卡名；parent＝父卡 id（留空挂当前卡）；content＝卡的正文。系统解析该块后建卡，格式不符的项被忽略。
"""


def build_system_prompt(context_block: str) -> str:
    """主模型（统合官）system prompt：使用说明 ＋ 中性上下文。"""
    if context_block and context_block.strip():
        return BASE_PROMPT + "\n" + context_block.rstrip() + "\n"
    return BASE_PROMPT


def build_viewpoint_system_prompt(context_block: str) -> str:
    """三思考席 system prompt：仅中性上下文，零指令。"""
    return context_block or ""
