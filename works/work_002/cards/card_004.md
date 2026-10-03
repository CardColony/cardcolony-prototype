---
title: "迭代路线与优先级"
source: "模型生成"
card_type: "线索"
parent: "card_001"
content_hash: "0830ed617982713be1b1c8c5368276a6f8820d859c938681a822fef56980d5e4"
tags: []
relations: []
session_ref: "sessions/session_004.md"
methodology_refs: []
pending_sync: false
created_at: "2026-10-03T12:51:52.726821+08:00"
updated_at: "2026-10-03T12:51:52.726821+08:00"
---

## 职责区

## 结论区

# 迭代路线与优先级（待定，尚未执行任何代码动作）

## 四个切入口
**A｜清账（纯减法，风险低）**
删 config.py / trace_recorder.py / _trash_recovery_error/ / 重复的 _server*.log；修 card_store.py:4 铁律注释（与 delete_card 二选一，需裁定）；清 methodology/ 空目录与 init_methodology；logs/ 加轮转或归档。

**B｜补地基（最大空白）**
建 tests/，先覆盖纯函数：card_proposal 多形态解析、can_read / can_edit、text_tool_rescue 五形态、file_tools 边界与四道兜底。

**C｜上下文管理（需设计）**
闸全删了，替代方案没建：每轮全量历史入 prior_messages，无截断 / 摘要 / 预算。work_001 会话已 155KB，logs 单文件已 324KB。要给「不减信息的压缩」或「分层保留」。这是唯一需要多模型会诊的题目。

**D｜项目管理上卡（已起步）**
本卡树即成果。

## 建议顺序
A → D → B → C：先清噪音 → 项目真上卡 → 补测试 → 碰最需设计的上下文。

## 纪律
每条路线必须配「落盘物」（commit / 文件改动），否则不算完成 —— 见 root 卡铁律 5。

## 首轮代码动作记录
（空 —— 待批准后填：改了什么 / 依据哪张卡 / 怎么验）

## 否决记录区

## 修改历史

- [2026-10-03T12:51:52.726821+08:00] 卡片创建
