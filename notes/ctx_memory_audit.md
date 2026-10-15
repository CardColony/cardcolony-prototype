# 上下文记忆系统 · 代码实读现状笔记

> 实读时间：2026-10（本轮）。范围：`D:\AI_station\推演agent\src\`。
> 说明：本文只记「代码现在是什么」，是 card_002 现状地图的对码依据。逐文件标注实际磁盘路径。

## 一、上下文组装（核心）

### src/api/context_assembler.py（139 行）
- `_load_cards_index(work_id)`：只扫 `cards/card_*.md`，返回 `[{card_id,title,parent}]`，**不含类型/正文摘要/空位提示**。无正则二次过滤（只靠 glob 前缀 `card_`）。
- `build_context_regions(work_id, current_id, memory_state=None)`：分区构建。渲染 100/150 两个 system 区 + 可选 160（工具卡步骤）/165（思维链活板）/200（凝练记忆）区。
- `build_neutral_context(work_id, current_id)`：降为薄包装，`assemble_regions` join。
- 不推送任何卡片正文（回流断根裁定仍在）。

### src/api/context_regions.py（172 行）
- 区表 `REGION_TABLE` 七区（order 为装配顺序）：
  - 0 使用说明（BASE_PROMPT 指针，main）
  - 100 卡片清单（main+viewpoints）
  - 150 当前卡（main+viewpoints）
  - 160 工具卡步骤指令（仅该卡有工具卡运行时出现）
  - 165 思维链活板事实三行（与 160 互斥）
  - 200 凝练记忆 condense_fold（仅 main）
  - 300 对话历史（messages，main+viewpoints）
  - 900 本轮工具轮（messages，main）
  - 999 用户消息（user，main+viewpoints）
- 纯函数、零 IO、零预算/配额/截断（"渲染零加工"）。
- `assemble_regions`：空区跳过，`\n\n` join。
- `build_log_regions`：prompt 日志 regions 键（只存指针+指纹，不复制正文）。

### src/api/prompts.py（117 行）
- `BASE_PROMPT_VERSION = "t17-1"`。
- `JUDGMENT_PROTOCOL`（判断协议）注入 BASE_PROMPT 开头（思维链真实落地，代码层零判定）。
- BASE_PROMPT 只描述系统使用（卡片结构/工具/建卡格式/错误码/卡间消息）。
- `build_system_prompt(context_block)`：BASE_PROMPT + 中性上下文。
- `build_viewpoint_system_prompt`：仅中性上下文，零指令。

## 二、对话历史与会话（300 区来源）

### src/storage/session_store.py（223 行）
- V1 格式：`---MSG---` 起、`k: v` 头至 `---`，正文可多行。追加式、永不改写。
- `_session_path`：`works/{work_id}/sessions/session_{seq}.md`（seq 取 card_id 后半）。
- `read_session`：解析为消息列表，读时现算 seq（0 起）。
- `last_ts`：尾部反向扫描（64KB 窗口 ×4 扩窗）。

### src/storage/session_facade.py（851 行）
- **会话统一读入门面**（迭代一会话底座）。门面对外、解析器对内（字节格式唯一知情层）。
- V1 路径：委托 session_store（零拷贝）；V2 格式：`---SESSION---` 文件头 + `---MSG---/---END---` 块。
- V2 块头白名单九字段：seq/actor/from/turn/pair/model/behavior/end/ts。白名单外字段保留原值+unknown_field 标记（前向兼容）。
- `read(work_id, card_id, from_seq, to_seq, actor, turn, limit)`：统一读入口。
- `append`：写时分配 seq/turn/pair，V1 现场升格 V2。
- `create_with_session`：建卡铺会话事务（失败回滚移 damaged/，不删字节）。
- `_active_turns` 逻辑互斥体：主线回合与凝练支线同一登记处（同卡 409 不排队）。
- `health_check` / `repair_damaged_tail`：打开体检与损坏处置（残块旁路 damaged/，永不删）。
- 投影 `rebuild/project`：从真相（会话文件）重建，写路径永不读投影。

## 三、凝练记忆（200 区来源）

### src/storage/memory_store.py（663 行）
- 记忆链单位＝卡片对话：`works/{work_id}/memory/{card_id}.json`。
- 水位 mark＝已凝练到的会话块序号（半开区间）。
- entries 五字段：seq/conclusion/evidence/occurred_at/fingerprint。
- 触发线：`DEFAULT_TRIGGER_TOKENS = 800_000`（tiktoken cl100k_base 真编码，2026-10-10 由字节口径改 token）。
- 单批窗口预算：`DEFAULT_WINDOW_BUDGET_TOKENS = 300_000`。
- `DEFAULT_MAX_BATCHES = 20`；`FOLD_EVERY_BATCHES = 5`（归拢游标）。
- 开关：`CONDENSE_ENABLED` 缺省关。
- 管线：取未凝段 → 模型写【结论】+【依据】→ 解析 → 幂等（fingerprint）→ 落 entries → 推进 mark。
- fold-l1 归拢线：模型判合并 → 机械校验 → 提交（成员指纹入 folded 账，物理保留、消费侧过滤）。
- 失败哲学：水位一分不动、留痕、不阻断不重试不设闸。

## 四、工具轮折叠

### src/api/tool_fold.py（258 行）
- 折叠单元＝工具轮（含工具调用的 assistant + 对应工具返回）。
- 保留窗口 `KEEP_WINDOW = 2`（最近 2 轮全文保留）。
- 两件套：【结论】模型压出（不设长度限制）；【依据】工具名+参数+原文位置（代码机械写）。
- 开关：`TOOL_FOLD_ENABLED` 缺省关。
- 不折红线：首轮用户原文固化、抢救回注（append_plain 非轮）；当前轮（K 窗口保证）。
- 折叠只压内容不删消息（tool_call_id ↔ tool_calls 配对结构不变）。
- 失败→该轮不折、全文保留、fold_ops 记错误。

## 五、影子卡

### src/storage/shadow_store.py（313 行）
- `works/{work_id}/shadows/{owner_card_id}.md`（文件名=正卡编号）。
- front matter 七字段：card_id(shadow_xxx)/card_type=shadow/parent=正卡/shadow_of/created_at/updated_at/content_hash。
- 三节模板：## 进度 / ## 产物清单 / ## 迭代历史。
- `update_shadow`：覆盖写正文三节（created_at 保留，updated_at 刷新）。
- `append_to_section`：工具卡底座 T8 自动回传通道——只往「## 产物清单」追加路径指针（占位符替换+机械去重+不抛）。
- `derive_shadow`：建卡派生（幂等，失败不阻断）；`remove_shadow`：删卡同删（唯一调用点 delete_card 钩点）。
- 读侧统一关口 READ 判定；写侧只有自己的正卡（工具签名无 card_id）。
- `iter_shadow_metas`：影子 meta 并入 `_collect_card_meta` 字典（键 shadow_xxx，挂在正卡下）。

## 六、卡片结构与写入

### src/storage/hash_utils.py（211 行）
- 哈希：Front Matter 内删 content_hash 行后 SHA-256。
- `parse_front_matter`：YAML front matter + 正文。
- 正文分块（新区名→键）：
  - ## 使命 → mission
  - ## 现状 → status_block
  - ## 定论 → rulings
  - ## 需求文档 → requirements（研发卡第三块，2026-10-12）
  - ## 修改历史 → history_text
- 旧区名（## 职责区/## 结论区/## 否决记录区）归一为新键。
- `split_body_sections`：按区头拆分，返回规范新键+形态标记。old/new/mixed 自适应，mixed 取外层优先（保形）。

### src/storage/card_store.py（1230 行）
- 卡片 = YAML Front Matter + Markdown 正文。
- 路径 `works/{work_id}/cards/card_NNN.md`，序号每 work 独立自增。
- `card_type` 取值校验走 `config/card_type_registry.json`（唯一事实源），废除静默降级。
- 文件锁 per-path asyncio.Lock，超时 423。
- 结构性防护：根卡/有子卡不可删。

## 七、卡片编辑机制（2026-10-12 改造）

- **edit_card / update_shadow 工具已收回**（不再出现在工具清单）。
- 模型只负责起草（draft 通道，无工具纯文本）；用户按钮确认写入（commit）。
- 使命块永远不经起草/确认通道（立项定稿）。
- src/api/card_draft.py（12945 字节）：draft/commit 逻辑。
- routes：`/draft`（DraftRequest kind=requirements|status）+ `/commit`（CommitRequest 含 base_hash，hash 冲突 409）。

## 八、权限

### src/permissions/policy.py（11419 字节）
- 权限纯函数。读=子树∪血统链∪连线∪锚卡；写=自己的子树。
- `card_tools.build_tool_schemas`：遍历注册表派生 + 两级过滤（角色/分类）。
- 无卡上下文 ⇒ 默认关门（返回空工具列表）。

## 九、主链路

### src/api/routes.py（915 行，43KB）
- `_run_message`：读卡→取历史→追加用户消息→组装→工具循环→三席→日志→落盘。
- `_run_message_inner`：只做装配+循环步进+落盘返回，六段细节迁至 message_runner。
- 同卡互斥：`session_facade.turn_try_acquire` 409 不排队。
- 新增端点：/nav、/nav/children、/nav/running、/nav/version、/draft、/commit、/card-messages、/stop。
- 兼容层固定转发 work_001。

### src/api/message_runner.py（1195 行，68KB）
- `RunState`：显式状态对象（只装数据）。
- `step(state)`：单步推进；四值 continue/seats/deliver/error。
- `build_context(state)`：装配 system_prompt/vp_system_prompt/工具列/轮槽。
  - 300 区收窄：凝练开关开→按水位 mark 切片；关→全量投影。
  - prior_for_gateway 投影 role/content 两键，过滤 behavior 非空留痕块。
  - `slot = tool_fold.ToolTurnSlot(prior_for_gateway)`。
  - `history_len` 一次算定（后续列表被轮槽就地 append）。
- `check_control(state)`：中止缝（人工停止/工具卡驱动共存）。
- 三席后置（E-探针制）：提交轮 board 全 done 后才触发。
- 工具结果全文累积 `tool_full_texts`（口径④，供三席注入，不折叠不截断）。

## 十、思维链与工具卡

### src/thinkchain/（board.py 34KB / plan_tools.py 24KB / step_region.py / model_pick.py / finalize.py）
- board：计划板（create_plan/plan_* 工具）。
- step_region：165 区（当前计划名/第几步/本步要求原文）。
- model_pick：步类型→模型路由（_pick_step_model）。

### src/toolcard/（driver.py / step_prompt.py / artifact_store.py / state_store.py / script_loader.py / finalize.py）
- 工具卡底座：run_tool_card 剧本、step_prompt（160 区）、artifact_store、script_loader。
- append_to_section 回传通道（产物清单路径指针）。

## 十一、工作（work）

### src/storage/work_store.py（182 行）
- work = 独立推演剧本：`works/work_NNN/`，含 cards/sessions/canvas/shadows/memory。
- work.json：{work_id,title,work_card_id,ending_derived,created_at}。
- work 不可删除。
- 根卡 = card_type=project，parent=""，project_class 挂根卡。

## 十二、其他

### src/api/card_msg.py（190 行）
- 卡间消息：类型注册表读取层 + 头行纯函数。`config/message_type_registry.json`。
- 头行 `[[card-msg id=... type=... from=... reply_to=...]]`。

### src/api/text_tool_rescue.py（164 行）
- 文本形态工具调用抢救（等待黑洞修复）。形态A（DeepSeek 原生标记）/形态B（围栏 JSON）。
- 疑似标记零解析 → 回注"未识别未执行"反馈句。

### src/api/progress.py（69 行）
- 调用进度跟踪，单进程内存态，纯展示层，不拦截不进模型输入（不是闸）。
- 新增 active 位 + iter_active（/nav/running 消费）。

### src/api/prompt_log.py（102 行）
- prompt 日志落盘 `logs/prompt_{card_id}_{stamp}.json`。
- 字段：system_prompt/prior_messages/user_prompt/response/error/tool_events/pair_id/regions/fold_ops。
- regions 只存指针+指纹（不复制正文）。

## 十三、测试与配置

### tests/（70+ 文件）
- 已存在，含 _fixtures/_golden/s2/s3/s4 子目录。
- conftest.py、test_condfold/test_card_msg/test_board/test_card_draft/test_blockset 等。
- README.md 3390 字节。

### config/
- models.yaml、card_type_registry.json、message_type_registry.json。

## 十四、关键开关（环境变量）

| 开关 | 缺省 | 作用 |
|---|---|---|
| CONDENSE_ENABLED | 关 | 凝练记忆（200 区 + 300 区收窄） |
| TOOL_FOLD_ENABLED | 关 | 工具轮折叠 |
| CONDENSE_TRIGGER_TOKENS | 800000 | 凝练触发线（token） |
| CONDENSE_WINDOW_BUDGET_TOKENS | 300000 | 单批窗口预算 |
| CONDENSE_MAX_BATCHES | 20 | 单 job 批上限 |
