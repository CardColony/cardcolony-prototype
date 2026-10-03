---
title: "欠账清单（已核实）"
source: "模型生成"
card_type: "线索"
parent: "card_001"
content_hash: "b7061ff0c7cac8cdbca6a40b3bbc982ca4c87709d9ada1bb74b2c8521e716f25"
tags: []
relations: []
session_ref: "sessions/session_003.md"
methodology_refs: []
pending_sync: false
created_at: "2026-10-03T12:51:52.531797+08:00"
updated_at: "2026-10-03T12:51:52.531797+08:00"
---

## 职责区

## 结论区

# 欠账清单（已核实 2026-10-03）

## A. 确证为真
1. **无测试体系** —— tests/ 不存在，全项目 0 处 `def test_`；requirements 挂着 pytest / pytest-asyncio / httpx 却没用。
2. **logs/ 无轮转** —— 31 文件，单文件最大 324KB，命名 prompt_card_001_时间戳.json。
3. **config.py 死代码** —— 读 config_dir/settings.yaml（:87），而 config/ 只有 models.yaml；`infra.config` / `load_settings` 全项目 0 引用。
4. **trace_recorder.py 死代码** —— 全项目 0 引用。
5. **铁律与代码打架** —— card_store.py:4 写「任何代码路径都不允许删除 cards\ 或 sessions\ 下的文件」，而 delete_card(:462) 正在 unlink 这两处。
6. **work_002 曾被误删** —— TEMP_LEDGER.md 事故记录：2026-10-03 12:30 被误当临时品删除（当时 session 内已有用户 12:14 原话与模型 12:17 的 3811 字回复），已从 git 3ea3b82 完整恢复，并立台账 + SOP。

## B. 我上一轮说错的（自纠）
- 根目录「`_t_filetools_e2e.py`、`_t_filetools_regression.py` 两个临时脚本」—— **不存在**（实测 0 命中）。即测试是彻底空白，连临时脚本都没有。
- 「`derive_ending` 已断链」—— **错**。它在 work_store.create_work 里活跃调用（card_store.py:455 注释、work_store.py:131）。
- 「`init_methodology()` 空转」—— 半对。它是 main.py:37 定义、:85 调用的 no-op，不是断链。

## C. 未证（待核）
- routes.py DELETE 端点注释 vs card_tools 的 delete_card（README 已称删卡是「用户侧按钮＋模型侧工具」，矛盾很可能为真，但未读到那段注释）
- progress.py 进程内存态（多 worker 失效）
- can_read 的「锚卡全局可读」路径是否还有活卡
- src/utils/ 内容
- work_store.update_work 里那条「不会再触发的路径」
- 模型工具表实际件数（README 称 14 件，未数）

## 否决记录区

## 修改历史

- [2026-10-03T12:51:52.531797+08:00] 卡片创建
