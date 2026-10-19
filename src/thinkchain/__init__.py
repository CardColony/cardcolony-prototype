# -*- coding: utf-8 -*-
"""思维链（thinkchain）包：迭代二 S1-S4 施工面。

S1 本件为板子基建 board.py；S2 工具层 plan_tools.py；S3 自判与收口；
S4 message_runner / context_assembler 接缝。src/api/ 路由层不感知本包
（任务书红线：板子只被工具层与 runner 接缝消费）。

=============================================================================
[纯单模型模式 2026-10-17 · 用户令] ENABLED = False
=============================================================================

用户裁定：5700 的思维链功能（首轮判型 / 拆解 / 上板 / 步骤推进 / 三席统合）
全部拆出生产链路，卡片思维回归**最简单的单模型自处理**。

实现方式＝**总开关**，不是删代码：
  - 本包所有模块原样保留（board/plan_tools/step_region/finalize/model_pick），
    随时可 `ENABLED = True` 恢复全链路；
  - 各接线点统一读本开关短路（工具注册 / 判断协议注入 / 165 区注入 /
    步类型选模 / 板催化 / 步事件）——见 card_tools / message_runner /
    current_turn / context_regions 的同日注释；
  - E 盘改前全树备份：`E:\\_bak_thinkchain_removal_20261017\\code_snapshot.tar.gz`
    （src/tests/static/config/docs/templates 共 1574 条目）。

关闭后模型可见面：基础工具（读卡/写文件/建卡/搜索等）＋ 使用说明，
无 create_plan / step_complete / plan_* 六件，无判断协议、无 165 区步骤指令、
无三席质询统合。一轮对话＝单模型带工具直干、落回复。
"""
from __future__ import annotations

#: 思维链总开关。False = 纯单模型模式（当前生产口径）；True = 原思维链全链路。
ENABLED: bool = False


def enabled() -> bool:
    """接线点统一判据（读模块常量，运行期可被测试 monkeypatch）。"""
    return ENABLED
