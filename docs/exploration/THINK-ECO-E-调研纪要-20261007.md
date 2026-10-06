# THINK-ECO-E 调研纪要（proposal p1）· E 探针制（三席后置）

## 一、对账基线

输入包 `E探针制-方案链输入包-20261007.md`（6,857 B／55 行／SHA16 f36dc0399efc4f7f）。对账底册 29 条（R-001~R-029）逐字取回、MISS 空。

## 二、现场实读锚点（行号全部现场实测，不抄输入包/素材文档）

### A. simple_service.py（后置要改的编排层）

| 锚 | 行号 | 原文/实测 |
|---|---|---|
| 文档头职责描述 | L2-14 | 「[phase9-E] simple 多模型编排器：观点并发 + 单模型统合…观点阶段：并发调 models.yaml simple.viewpoints 各模型、tools=None…统合阶段：user_prompt 追加参考观点段，走现有 call_main（带 tools）」 |
| VIEWPOINT_HEADER | L31 | `## 其他模型的输出` |
| VIEWPOINT_TASK_NOTE（「三份」硬编码） | L36 | 「以下是**三份**独立分析的参考材料，不是用户的问题。你的任务是回答上面用户提出的问题。」 |
| `call_simple` 签名 | L124-131 | `async def call_simple(system_prompt, vp_system_prompt, user_prompt, prior_messages=None, tools=None, on_progress=None)` —— 有 `tools` 入参 |
| 三席并发出观点（tools=None） | L158-186 | `_report("viewpoints", ...)` → `asyncio.gather(*[_call_viewpoint(gw, n, vp_system_prompt, user_prompt, prior_messages, ...) for n in viewpoints])` —— **三席在 call_simple 进门处、手头只有 user_prompt＋prior** |
| `_call_viewpoint` tools=None | L96 | `tools=None`（10-02 零工具裁定） |
| build_viewpoint_prompt（观点拼进统合输入） | L54-66 | 无 ok_metas 原样返回；有则 `f"{user_prompt}\n\n{VIEWPOINT_HEADER}\n{VIEWPOINT_TASK_NOTE}\n{sections}"` |
| 统合阶段（带 tools） | L196-198 | `call_main(system_prompt, final_user_prompt, prior_messages, tools=tools)` —— **统合官带工具，在观点之后** |

### B. routes.py（现有 while 工具循环）

| 锚 | 行号 | 原文/实测 |
|---|---|---|
| 工具循环 while | L424 | `while True:`（10-02 拆闸后不设封顶） |
| 首轮走 simple | L426-436 | `if rounds == 1: ... call_simple(..., tools=tools, ...)` |
| 续跳轮只走统合官 | L437-448 | `else: ... call_main(system_prompt, user_prompt, wire_messages, tools=tools)`（注释「续跳轮不重复咨询观点模型」）——**三席永不回头** |
| 无 tool_calls → 收尾 | L475-486 | `if not tool_calls: final_reply = assistant_content; ... break`（或 text_rescue_note 续跑） |
| 首轮用户原文固化 | L491-492 | `if rounds == 1: slot.append_plain({"role": "user", "content": content})` |
| 工具轮入槽 | L497-508 | `slot.begin_round({role: assistant, tool_calls: [...]})` |
| T14 只读采集 | L512-513 | `if assistant_content: analysis_parts.append(assistant_content)` |
| 工具执行＋回喂 | L527-535 | `execute_tool(...)` → `slot.append_tool_result(...)` |
| 续跑 prompt | L537 | `user_prompt = "工具结果已返回。"`（进变量，不落盘） |

### C. prompts.py

| L17 | `BASE_PROMPT_VERSION = "t12-1"` |

### D. 关键再确认（三席输入源）

`_call_viewpoint`（L69-99）：`gw.call(name, system_prompt, user_prompt, ... tools=None, prior_messages=prior_messages or None)` —— 三席只收到 `vp_system_prompt`（中性上下文）＋`user_prompt`（用户问题）＋历史。**工具结果零注入。**

## 三、病灶与目标（按用户 00:13③ 重述，行号坐实）

- **现状**：三席 `simple_service.py:158-186` 在每轮最开头并发出观点、tools=None（L96）——此刻工具一次没查，三席靠记忆与旧料猜；统合官 `L196-198`/`routes L426-436` 才带工具查；查回结果后，续跳轮只走统合官 `routes L437-448`、**不重问三席**。**三席永远先于工具、且永不回头。**
- **目标**：主模型先带工具查事实（复用 routes 现有 while 探查＋续跑收集）→ 工具结果出来 → 注入三席输入 → 三席踩着事实出观点（tools=None，10-02 裁定不变）→ 统合交付（tools=None，纯交付不再发工具）。
- **本质改动**：三席调用从「首轮最开头」挪到「工具轮之后」，把**工具结果**（经【结论】＋【依据】折叠）装进三席输入。

## 四、设计判断（p1 阶段·为草稿打底）

1. **切点**：把 `simple_service.call_simple` 从「三席前置＋统合带工具」改成「编排状态机＋工具结果入参」：
   - 探查段（事实收集）留在 routes 的 `while True`（复用现有 tool_calls 收集、begin_round 入槽、append_tool_result 回喂——**不新造第二套 while**）；
   - 探查收敛（无 tool_calls）后，把该轮累积的工具结果打包，作为**新入参**传给 simple 的三席段（tools=None）；
   - 三席出观点 → 统合段 `call_main(tools=None)` 纯交付（第三步后再发工具会使三席观点过时——这是后置成立的关键，须写进方案）。
2. **合同边界**：`call_main` 合同不动（R-023/BASE_PROMPT_VERSION t12-1 递增）；三席 `tools=None` 不动（R-010）；`call_simple` 签名可扩（加工具结果入参）——它是内部编排入口，非红线所指合同。
3. **VIEWPOINT_TASK_NOTE 改造**：后置后统合官输入含「工具事实段＋三席观点段」两段，现 L36 那句「三份独立分析…回答上面用户问题」指错对象（三席不是三份、用户问题已被探查替代）——须拆成两段说明或改写为新契约，`【结论】＋【依据】`折叠形态（用户 10-05 裁定）注入工具事实。
4. **成本口径（须上桌等用户拍，方案不得代办）**：每轮皆三席后置 vs 仅指定轮触发——①每轮皆＝成本≈5-6 次/轮、每轮三席交叉审；②仅指定轮＝省成本、部分轮无三席交叉。此为设计层选择，按用户 10-04 记忆单位裁定端上桌。
5. **待拍项**：除成本口径外，还有「统合段 tools=None（纯交付）是否接受」——这是对「统合官是唯一工具出口」现状的改变（探查完成后不再给统合官发工具），须写进方案待用户拍。

## 五、备注

- routes.py `while True` 与三席后置是**协作**不是**嵌套**：探查放 while、观点收尾不进 while（无新 while）。
- T11 装配/T14 交付拼接已叠加在 routes while 上（L424 起多层注释），改动只涉及「从 while 收敛后进三席段」这一个衔接点，未动既有轮槽/采样绑定。
- 素材文档《E探针制实现方案.md》全程未读正文引用（只读合成；其伪代码与 MAX_TOOL_CALLS 作废，未复用）。