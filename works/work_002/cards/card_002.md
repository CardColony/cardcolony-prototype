---
title: "项目现状地图"
source: "模型生成"
card_type: "线索"
parent: "card_001"
content_hash: "dec56b679c067dfd2f5764083e86228b3cb87005ab0af24d18436f95e55b4d7f"
tags: []
relations: []
session_ref: "sessions/session_002.md"
methodology_refs: []
pending_sync: false
created_at: "2026-10-03T12:51:52.357750+08:00"
updated_at: "2026-10-03T12:51:52.357750+08:00"
---

## 职责区

## 结论区

# 项目现状地图（list_files / read_file 实测，2026-10-03）

## 是什么
卡片式多模型推演系统。FastAPI:5700 + 静态前端（剧本架 / 画布 / 卡片工作区）+ 本地文件存储。一张卡 = 一个 .md（YAML front matter + 四区正文），写入走 git 自动提交。

## 一轮对话的链路
读卡 → 取历史 → 追加用户消息 → 组装中性上下文（不推正文）→ 三思考席并发（无工具）→ 统合官带工具产出唯一回复 → 文本形态工具调用抢救并执行 → 解析 card-proposal 建卡 → 落 prompt 日志 → 存 session。

席位：思考席 MiniMax-M3 / qwen3.8-max / deepseek-v4-pro；统合官 deepseek-v4.1-flash。

## 盘上实况
根目录：main.py · README.md · SOP_清理废弃剧本.md · TEMP_LEDGER.md · requirements.txt · start.bat · stop.bat · restart_safe.py · restore_card.py · check_before_delete.py · _server.err.log / _server.out.log / _server_err.log / _server_out.log（重复两对，最大 59KB）· .gitignore · .server.pid

src/api/（13 件）：routes 22KB · card_tools 19KB · file_tools 15KB · card_proposal 8KB · simple_service · text_tool_rescue · search_tool · context_assembler · model_service · prompts · prompt_log · progress · __init__
src/storage/ · src/infra/（config · logging · errors · json_utils · text_utils · trace_recorder）· src/model_gateway/gateway.py（35KB，标注禁止修改）· src/utils/（内容未看）

config/：只有 models.yaml
methodology/：空
logs/：31 文件，最大 324KB，无轮转
works/：work_001（股权财政，4 卡）· work_002（本剧本）
tests/：不存在
_trash_recovery_error/：杂物目录

## 设计哲学（代码注释里被反复执行的裁定）
零内容预设 · 拉取不推送 · 无闸（max_tokens / timeout / 熔断全删）· 可审计（prompt 日志 + git 双通道）· 机械兜底接住模型缺陷（已知五种泄漏形态）

## 否决记录区

## 修改历史

- [2026-10-03T12:51:52.357750+08:00] 卡片创建
