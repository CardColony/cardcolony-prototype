# -*- coding: utf-8 -*-
"""工具卡底座：让「工具卡」这个类型拥有按步骤自动跑起来的机制。

**本包最高原则（EC-011 / EC-013 / EC-015 / EC-016 / EC-017，任务书 §四 R-A）**

代码只做四件事：**执行、存储、渲染、调起提醒**。一切判断归模型。

- 代码**只能记录「我做了什么」**，不能产出「这算什么」。
- 分界线：IO 事实记录可留（写盘成功没成功）／**判定结果一条不留**
  （产物在不在、好不好、这步算不算完成、走完没走完）。

因此本包内不出现：产物存在性检查、非空检查、内容检查、终态判定、轮数封顶、
回边、长度闸、超时闸。落进文件与返回值的**只有事实与指针**，评价一律由模型写。

六件机器：
    script_loader  剧本装载（只读只解析）
    driver         步骤推进（照单收下）
    state_store    卡面状态持久化（fail-open）
    artifact_store 产物落库（记录不是闸门）
    finalize       终态收口（收下声明、链结束）
    step_prompt    步骤指令组装（注入事实，不注入评价）
"""
from __future__ import annotations

__all__ = [
    "script_loader",
    "driver",
    "state_store",
    "artifact_store",
    "finalize",
    "step_prompt",
]
