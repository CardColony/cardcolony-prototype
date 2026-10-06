# THINK-ECO-E-taskbook · p1 实读调研纪要（编排落点定死＋兼容面）

线：E 探针制（三席后置）任务书链 · p1
2026-10-07 00:35

## 一、现场实读：现行编排真身（全部测试空间实测，行号现场）

**simple_service.py `call_simple`（L124-199）**：
- L158-186：**观点阶段在进门处**——`asyncio.gather` 并发调三席 `_call_viewpoint(gw, n, vp_system_prompt, user_prompt, prior_messages)`；此时 `user_prompt` 只有用户原话＋历史，工具一次未查。
- L188-189：`ok_metas` 筛选后 `final_user_prompt = build_viewpoint_prompt(user_prompt, ok_metas)`（三席观点拼进统合官输入）。
- L196-198：统合官 `call_main(..., tools=tools)` **带 tools**——工具查证发生在统合官这层、三席之后。

**routes.py 工具循环（L479-548）**：
- L479 `while True`（工具循环焊枪）。
- L481-491：首轮（rounds==1）走 `call_simple`（三席＋统合一把抓）。
- L492-503：续跳轮（rounds>1）走 `call_main`（**不调 simple、不重问三席**）带 tools 续查。
- L504：`tool_calls = result.get("tool_calls")` 回接。
- L530-541：无 tool_calls → `final_reply = assistant_content` → break 收尾。
- L543-547：首轮进工具轮时固化用户原文入 slot。
- L596-592（读段外围）：工具执行＋`slot.append_tool_result` 回喂＋L592 `user_prompt="工具结果已返回。"` 自动续跑。
- L652-658：交付段 `delivered = "\n\n".join(analysis_parts + [final_reply])`。

**结论（铁证）**：三席在首轮进门处（simple L158）空手出观点；工具查询在统合官层（simple L196-198）及续跳轮（routes L498）进行、可多轮累积；三席之后再无被咨询的机会。工具结果越查越多、三席永远见不着——「三席先于工具、永不回头」由这段代码钉死。

## 二、切点形态定案（诸葛拍，可推翻）

**时机**：工具查证循环全部收尾（routes L530 无 tool_calls 判定）之后、交付段（L652）之前——三席在该点进场**一次**。

**落点**：复用 simple_service 的 `build_viewpoint_prompt`＋`_call_viewpoint`（观点并发逻辑），**拆为可独立调用的函数**，由 routes 收尾处调用：三席进场（注入全量工具结果）→ 三席观点拼入统合官输入 → 统合官最终统合交付。

**为什么不用「simple 内后置」**：simple 只在首轮被 routes 调一次（L484），simple 内部只能看到首轮统合官那次查回的结果；多轮工具累积发生在 routes 续跳轮（simple 之外）——simple 内后置吃不到全量工具结果。只有 routes 收尾处才拿得到「该轮全部工具结果」，才是用户 00:13「工具结果出来之后再三席思考」的完整语义。

**留推翻口**：若任务书环节发现 simple 内部自闭环能覆盖多轮（工具结果回传 simple 内），改为 simple 内重排更快；当前定案以「拿全量工具结果」为第一优先。

## 三、与既有钩子的兼容面

| 既有钩 | 落点 | 与三席后置的兼容 |
|---|---|---|
| **T14 `analysis_parts` 采集** | routes L568（工具轮 assistant 正文采集）、L652-658（交付拼接） | 三席观点是「注入统合官输入」而非直接进 delivered，走统合官 final 输出——不撞 analysis_parts 结构；三席后置点在交付段之前、采集逻辑在工具轮内，时序不冲突 |
| **T12 回合末凝练触发 `condense_turn_end`** | routes L691-693（assistant 落盘之后异步触发） | 三席后置发生在「最终统合交付」之前、凝练在交付之后——三席先、凝练后，时序无冲突 |
| **T11 装配线 slot / wire_messages** | routes L462 slot、L497 wire_messages（装配给主模型）、L407（probe） | 三席输入不走 slot/装配（三席 tools=None，靠注入）；工具结果注入三席的形态是新增通道、不替代 slot 装配主模型历史——两轨独立，不冲突 |

## 四、红线复核（读码确认）

- **不设闸**：routes L479 `while True` 无任何 cap；simple 无 max_tokens/timeout（拆闸注释 L159 在案）——后置改造不得引入任何轮次/墙钟/max_tokens 上限。
- **三席 tools=None 不动**：simple L96 `tools=None` 在案——后置后三席仍无 tools、只靠注入全量工具结果。
- **call_main 合同不动**：统合官 `call_main(system_prompt, user_prompt, prior_messages, tools=tools)` 签名保持；仅注入内容从 user_prompt 扩展为含工具结果。

## 五、留任务书环细化的点（不在此圆）

1. 工具结果如何「累积取全」——slot 内工具结果/`prior` 中该轮 tool 结果的收集形态。
2. 三席注入输入的组装——工具结果全文注入（不折叠不截断）、`build_viewpoint_prompt` 入参从 user_prompt 扩展字段。
3. routes 收尾调用三席时，统合官「最终统合」与现行「final_reply=assistant_content」的衔接——三席观点拼入后，统合官再走一次最终交付。

（p1 完整、全部现场实读。行号会漂，任务书以现场为准。）