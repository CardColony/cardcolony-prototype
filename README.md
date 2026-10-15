# 推演agent

一个**处理复杂项目的通用型 agent**。把一件复杂的事拆成一张张卡片，沿卡片树一层层推进，
多模型协作收敛成结论，每一步都留痕、可对账、可翻案。

> 项目名沿用「推演agent」，但定位已从早期的「单一剧本推演」泛化为「通用复杂项目处理」——
> 处理什么项目取决于你搭什么卡片树、给什么任务，系统本身不绑定任何领域。

## 它解决什么

复杂项目的失败通常不在单轮能力，而在过程中**丢掉的结论、复活的错误、被吞掉的判断**。
本系统用四条机制防这些：

1. **结论是卡片间唯一接口** —— 卡内推理过程留在各自的独立对话里，只有沉淀后的结论跨卡流动，
   历史结论不淹没过程、被否定的方向不会复活。
2. **权限由卡片树机械决定** —— 读侧全盘可读（存在即放行），写侧严格限自己子树；
   **建卡只能建自己的子卡片**（parent 须落在本卡子树内，缺省＝本卡自身）——
   越权由代码机械拒绝，与 edit/delete 同一判据。
3. **全量留痕可对账** —— 每次模型调用的输入输出全文落 `logs/`、每次卡片改动入 git、
   每次工具调用记 `tool_events`；「说了做」与「真做了」可三方对账定案。
4. **机械兜底接住模型缺陷** —— 工具调用被模型以文本形态泄漏（已知五种形态）、日志/解析异常等，
   一律在代码层接住，不依赖模型自觉。

## 能力面

| 层 | 内容 |
|---|---|
| 卡片树 | 建卡／编辑正文／建线／解线／删卡（用户侧按钮＋模型侧工具，护栏：根卡与有子卡者拒删） |
| 思维链 | 入口自判 direct（直答）／plan（拆解上板）；复杂任务拆成多步、逐步推进、改板、收口回写 |
| 多模型协作 | 三思考席（MiniMax-M3 / qwen3.8-max / glm-5.3）并行分析 → 统合官收敛成唯一结论；三席无工具权限 |
| 工具层（25 件·单点注册表） | 卡片 9：create_card／read_card／read_memory／edit_card／delete_card／read_shadow／update_shadow／link_cards／unlink_cards；资料 3：list_manuscript／read_manuscript／search_manuscript；本地文件 4：list_files／read_file／search_files／write_file；联网 2（**条件提供**）：web_search／fetch_web；工具卡 1：run_tool_card |
| 思维链工具（6 件） | create_plan／plan_reorder／plan_insert／plan_remove／plan_modify／step_complete（`escalate_to_plan` 已退役） |
| 凝练记忆 | condense_fold：长对话凝练归拢成【结论】/【依据】条目，可按块序号下探取回 |
| 卡间通信 | 卡与卡之间发消息（类型注册表可扩展），收件方应对由系统自动回发 |
| 本地文件能力 | 边界＝**整个 `D:\AI_station`**（不限某几个子目录——实测子目录之外的路径同样放行）；凭证类文件（`.env` 及其变体／`.netrc`／`.npmrc`／`.pypirc`／`credentials.json`／`id_rsa`／`id_ed25519`／`kubeconfig`／`secrets.yaml` 等 14 项精确名单）**不可读、不可写、不可搜索、也不出现在目录清单里**；四道兜底：写前强制备份原文到 `_backups/`、二进制拒写、单次写入上限 5MB、依赖目录（.venv/site-packages/__pycache__ 等）剪枝 |
| 联网能力 | **可选插件**：requests 可用 且 至少配了 `SEARXNG_URL` 或 `ALIYUN_TOKEN_PLAN_API_KEY` 才注册 `web_search`/`fetch_web`；没配则整件不提供（出厂不带即不出厂坏）。设 `SEARCH_CHANNEL=searxng\|aliyun` 即钉死通道，钉死后该通道失败会**点名报错、不静默换通道** |
| 文本泄漏抢救 | 模型把工具调用写进正文（DeepSeek 系特殊标记泄漏）时，机械解析并真实执行；解析不出则回注「未识别未执行」告知模型 |
| 实时反馈 | 发消息后可见：自判分流／三席分析进度／统合推理／工具循环轮次／建卡，逐阶段上报 |

## 起停

```bat
start.bat          :: 启动服务（后端 5700）
stop.bat           :: 停止服务
```

**重启（改代码后装载）只走 `restart_safe.py`** —— 它是唯一许可的重启方式：先判定「有无调用在跑」
（progress 活跃态＋连接信号复核），有则拒载，绝不杀掉正在执行的调用。

```bat
python restart_safe.py --check   :: 只判定，不重启
python restart_safe.py           :: 判定通过后重启
```

访问：**http://127.0.0.1:5700/**

## 数据在哪

| 路径 | 内容 |
|---|---|
| `works/<work_id>/cards.db` | **卡片与影子本体**（SQLite：卡面字段 ＋ 正文三块 ＋ 修改历史 ＋ 影子三节）——**库是唯一真相** |
| `works/<work_id>/sessions/*.md` | 每张卡背后独立对话的完整记录 |
| `works/<work_id>/canvas.json` | 画布布局与连线 |
| `logs/prompt_*.json` | **每次模型调用的输入输出全文**（system_prompt／user_prompt／response／tool_events／error） |
| `_backups/<原路径>/<时间戳>/` | 文件工具覆盖写之前的原文备份（不依赖 git 的兜底） |
| `methodology/` | 方法论文档 |

> [2026-10-14 卡片存储迁移] 卡片与影子**已进 SQLite**（`works/<work_id>/cards.db`），
> 不再有 `works/<work_id>/cards/*.md` 与 `works/<work_id>/shadows/*.md`——
> 各区域在库内**各占独立列**，取什么查什么。`sessions/`、`canvas.json`、`layout.json` 仍为文件。

## 版本线

```bat
git log --oneline          :: 全部提交（卡片增删改由 auto-commit 自动提交，带 [auto][work_xxx] 前缀）
git tag                    :: 里程碑
git checkout <commit> -- <file>   :: 取回任一历史版本
python restore_card.py     :: 把某张卡恢复到 git 任一历史版本
```

## 目录结构

```
main.py                 服务入口（FastAPI，端口 5700）
src/api/                路由、提示词、工具层、上下文装配、解析器、进度上报
  ├ routes.py           全部 HTTP 端点（prefix=/api）+ 消息六段链
  ├ message_runner.py   单步编排内核（RunState，step 循环 + 控制中止缝）
  ├ card_tools.py       标准工具注册表（18 件）schema 与执行分发
  ├ file_tools.py       本地文件工具（四件＋边界＋四道兜底）
  ├ context_regions.py  上下文六区装配（使用说明/卡片清单/当前卡/记忆/对话/工具轮）
  ├ text_tool_rescue.py 文本形态工具调用抢救器
  ├ card_proposal.py    建卡提案解析（文本通道兜底）
  ├ prompts.py          系统使用说明（BASE_PROMPT，版本化）
  └ simple_service.py   三席分析＋统合
src/thinkchain/         思维链（入口自判 triage + 计划板 board + 七工具）
src/toolcard/           工具卡底座（代码只做执行/存储/渲染，判断归模型）
src/model_gateway/      模型网关（litellm 封装、多端点、重试、日志）
src/storage/            卡片/会话/记忆/画布存储、快照、git 自动提交
src/permissions/        统一权限关口
config/models.yaml      模型席位配置（改模型只改这里）
```

## 边界与已知限制

- **它慢**：一轮复杂任务 1–8 分钟（三席并行＋统合＋工具循环），适合「把事想透」，不适合「快点干完」。
- **输出质量＝模型的分析质量**：卡片、权限、留痕只保证错误不扩散、可回溯，不保证推理本身正确。
- **文件能力不设删除**：模型没有删文件工具（删除不可逆）；不需要的文件由人或系统外清理。
- **模型自述不是状态依据**：任何「已完成 X」的声称，以 `logs/`、磁盘、git 三方对账为准。
