# 工具卡底座 · 任务书 · p3 质疑报告

> 链：taskbook · p3（专项质疑环节）
> 被审对象：`docs\exploration\工具卡底座-任务书-草稿-20261009.md`
> 事实底座：`docs\exploration\工具卡底座-任务书-p1-调研纪要-20261009.md`
> 需求源头：`docs\exploration\工具卡底座-方案-定稿-20261009.md`（33,270B / SHA256 2CC986E6…4165）
> 对账底册：`THINK-ECO-toolcard-对账-方案条目清单-20261009.json`（EC-001~EC-184）
> 判定时间：2026-10-09 13:55

---

## 一、九条逐条判定

### 1. 【通过】实读核验

**抽验条数**：24 条（远超要求 3 条），全部用 read 工具回读原文比对。

| # | 草稿引用 | 回读实测 | 判 |
|---|---|---|---|
| 1 | `card_tools.py` L534 `TOOL_REGISTRY` | L534 逐字 `TOOL_REGISTRY: dict[str, ToolDef] = {` | ✓ |
| 2 | `card_tools.py` L790 `_register_file_tools` | L790 逐字 `def _register_file_tools() -> None:` | ✓ |
| 3 | `card_tools.py` L816 调用 | L816 逐字 `_register_file_tools()` | ✓ |
| 4 | `card_tools.py` L828 `READONLY_TOOLS` | L828-830 逐字 `READONLY_TOOLS = frozenset(name for name, def_ in TOOL_REGISTRY.items() if def_.readonly)` | ✓ |
| 5 | `card_tools.py` L833 `build_tool_schemas()` | L833 逐字 `def build_tool_schemas() -> list[dict[str, Any]]:` — **签名确无入参** | ✓ |
| 6 | `card_tools.py` L839 全量导出 | L839 逐字 `return [_schema_of(def_) for def_ in TOOL_REGISTRY.values()]` — **无任何过滤** | ✓ |
| 7 | `card_tools.py` L842 `_collect_card_meta` | L842 逐字 `def _collect_card_meta(work_id: str) -> dict[str, dict[str, Any]]:` | ✓ |
| 8 | `card_tools.py` L855 `can_read` | L855 逐字 `def can_read(` — docstring L859 逐字「[卡片权限改造 2026-10-07] 全盘放开——target 存在即放行」 | ✓ |
| 9 | `card_tools.py` L877 `can_edit` | L877 逐字 `def can_edit(current_id: str, target_id: str, cards: dict[str, dict[str, Any]]) -> bool:` | ✓ |
| 10 | `card_tools.py` L1038 `execute_tool` | L1038 逐字 `async def execute_tool(` | ✓ |
| 11 | `card_tools.py` L1059 查表命中即调 | L1059 逐字 `def_ = TOOL_REGISTRY.get(tool_name)` → L1062 `result = await def_.handler(...)` — **执行前无任何权限过滤** | ✓ |
| 12 | `card_tools.py` L231-233 影子守卫 | L232-233 逐字 `if cards[target_id].get("card_type") == "shadow": return err("denied", "影子卡只能由对应正卡经 update_shadow 修改")` | ✓ |
| 13 | `card_tools.py` L136 `_h_read_card` | L136 逐字 `async def _h_read_card(work_id: str, current_id: str,` | ✓ |
| 14 | `card_tools.py` L143 内联 can_read | L143 逐字 `if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):` | ✓ |
| 15 | `shadow_store.py` L26-37 三节模板 | L26-37 逐字 `## 进度` / `## 产物清单` / `## 迭代历史`，各带「（暂无记录）」 | ✓ |
| 16 | `shadow_store.py` L119 `read_shadow_raw` | L119 逐字 `async def read_shadow_raw(...)` — **返回全文**（L127 `return await asyncio.to_thread(p.read_text, encoding="utf-8")`） | ✓ |
| 17 | `shadow_store.py` L130 `write_shadow_raw` | L130 逐字；L136 `lock = card_store._get_lock(p)`；L137 `await card_store._acquire(lock)`；L143 `await data_snapshot.snapshot(...)` | ✓ |
| 18 | `shadow_store.py` L164 `update_shadow` | L164 逐字 `async def update_shadow(work_id: str, owner_card_id: str, body: str)` — docstring 逐字「覆盖写正文三节」 | ✓ |
| 19 | `shadow_store.py` L6 docstring | L6 逐字「由正卡（模型）经 update_shadow 工具主动书写，**系统不自动写内容**」 | ✓ |
| 20 | `card_store.py` L497 `_write_gate` | L497 逐字 `def _write_gate(work_id: str, action: str, card_id: str,`；L508 闸3 注释逐字「工具卡 owner —— card_type=="tool" 时 owner_card_id 非空且 owner 合法」 | ✓ |
| 21 | `card_store.py` L594 闸3 判据 | L594 逐字 `if owner_type != "sub_project":` → L595-596 逐字 `return {"error": f"owner「{owner}」的 card_type「{owner_type}」不是 sub_project，不能作工具卡 owner"}` | ✓ |
| 22 | `card_store.py` L597-606 不可有子卡 | L597-606 逐字（遍历 `card_*.md`，`m2.get("parent") == card_id` 即拒） | ✓ |
| 23 | `message_runner.py` L141 `check_control` | L141 逐字 `def check_control(state: RunState) -> None:` — L142 docstring 逐字「step 边界的控制钩子（**空实现**）」 | ✓ |
| 24 | `message_runner.py` L201 调用点 | L201 逐字 `state.tools = card_tools.build_tool_schemas()` — **确无入参**；L219 逐字 `check_control(state)          # 中止缝：入口挂点（空实现）` | ✓ |

**比对结果：24/24 全部对上，零编造、零行号漂移。**

### 2. 【通过】逐条对照

对账底册 184 条（EC-001~EC-184）逐条对回草稿落点：

- **有落点（covered）168 条**
- **语义承接但未逐字进任务书（weak）16 条**
- **无落点（empty）0 条**

16 条 weak 逐条列明（见对账三张表 forward 表），性质分三类：
- EC-014（17 个已退役校验函数名）——文档元信息，语义由第一节「质量保障＝剧本内复核步骤」承接
- EC-020~EC-027（ZhuGe 八件照搬映射）——**功能已由 T1-T6 逐件承接**，未落的是「哪个模块对应 ZhuGe 哪个文件」的溯源
- EC-146~EC-162（需求逐条对账 16 条）／EC-163~EC-178（我定的实现选择 16 条）——**两整表未进任务书**；其中实现选择多条散见于 T3/T7/T8/T9/T10 正文，未汇成一节

**判定：无「漏一条」情形**——16 条 weak 全为文档元信息与溯源记录，其实质内容均有任务项承接。**但 2 类建议补**（见下「差距清单」）。

### 3. 【通过】遗漏检查

**空段检查**：六段全实料——背景与裁定（专项要求四条逐字贴入）／现场锚（全扎 p1 纪要 file:line）／任务分解 T1-T12／红线 R-A~R-G／验收 V1-V12／回报要求。**零空段。**

**验收可机械判定检查**：V1-V12 逐条过——

| V | 断言 | 可机械判定 |
|---|---|---|
| V1 | 六文件存在且可导入 | ✓ 文件存在性＋import |
| V2 | `driver.start` 返回第一步指令；重复调用幂等 | ✓ 直调比对 |
| V3 | `driver.advance` 照单推进 | ✓ 直调比对 |
| V4 | `driver.done` 后运行时清空、卡面落值 | ✓ 直调＋读盘 |
| V5 | `build_tool_schemas(<主卡id>)` 不含 7 件、含 `run_tool_card` | ✓ 集合比对 |
| V6 | `build_tool_schemas(<工具卡id>)` 不受角色过滤影响 | ✓ 集合比对 |
| V7 | `execute_tool(<主卡id>,"edit_card")` 执行层被拦 | ✓ 直调返回 |
| V8 | 真实跑一次：建卡属性＋产物落点＋重做落 `-02` | ✓ 磁盘产物 |
| V9 | 产物清单节出现「路径＋说明」行；重复不重复登记 | ✓ 读盘比对 |
| V10 | 闸3：根卡 owner 成功／scope 外仍拒／注册表与代码一致 | ✓ 直调＋读文件 |
| V11 | 全仓扫描无新增判定逻辑 | ✓ grep |
| V12 | 测试实例回归通过；生产 works 逐字节零变化 | ✓ 哈希比对 |

**零条「运行正常」类不可判定断言。**

### 4. 【通过】自由发挥检查

T1-T12 逐条挂 EC 编号，**无一条无据**。逐条溯源：

- T1←EC-028~031｜T2←EC-032~035｜T3←EC-036~040｜T4←EC-041~044｜T5←EC-045~047｜T6←EC-048~053
- T7←EC-054~059｜T8←EC-086~114｜T9←EC-063~085｜T10←EC-115~145｜T11←EC-183＋风险点3｜T12←EC-180＋R-B

**零自加需求。**

### 5. 【通过】执行者自由度检查

全文扫描含糊表述（`酌情`／`视情况`／`等`／`适当`／`尽量`）：

- 唯一命中是 T1 的「剧本存放目录落点：**由执行方选定并在回报中报明**」——**这是明确的授权边界**（选哪由你定，但必须报明），不是含糊表述；且它是**实现选择**（方案第八节第 1 条已列为"我定的"）。
- **零「酌情／视情况／适当／尽量」类词。**

### 6. 【通过】关联面核对

对照 p1 纪要的关联模块清单逐项对：

| 纪要列出的关联面 | 草稿是否列入 |
|---|---|
| `card_tools.py`（L534/790/816/828/833/842/855/877/1038） | ✓ 第二节现场锚全列 |
| `shadow_store.py`（L26/64/119/130/146/164/179） | ✓ 全列 |
| `card_store.py`（L85/497/583-596/597-606/764/960/1042） | ✓ 全列 |
| `message_runner.py`（L87-131/141/201/219/392/346/377） | ✓ 全列 |
| `context_assembler.py`（L33/55/113） | ✓ 全列 |
| **调用点**：`build_tool_schemas`（L201）／`check_control`（L219/L392）／`execute_tool`（L346）／`_write_gate`（L764/960/1042） | ✓ 全列 |
| **待侦察项 R1-R8** | ✓ 列入第二节＋第六节回报要求 |
| **fixture**：`tests/` 下调用旧函数的三个文件（`test_card_tree.py`／`test_cardperm_readopen.py`／`test_perm_r3_equivalence.py`） | ⚠ **未列入**（见差距清单） |

### 7. 【通过】生产安全

已知病根逐条比对：

| 病根 | 草稿处置 |
|---|---|
| 并发写（读改写窗口） | ✓ T8 明写「一次锁内完成读-改-写」，复用 `write_shadow_raw` L136 同一把锁 |
| 重启依赖 | ✓ R-B「生产只做部署窗口、部署与重启须另行获许可」 |
| 直写库文件 | ✓ 无（状态走卡面字段经 `_write_gate`，产物走 work 目录） |
| 时间/长度/轮次闸 | ✓ R-C 明文禁止 |
| 生产 works 数据触碰 | ✓ R-B 明文；V12 逐字节核验 |
| 两线共用函数冲突 | ✓ R-D 明文＋T10 独立 helper 设计 |

### 8. 【通过】测试服先行

**白纸黑字**：R-B 逐字「任何动作**先在测试实例跑通**；生产实例只做部署窗口、生产零触碰」＋T12 逐字「测试实例先全量跑通 V 系，再进入部署窗口」。**非「应该会注意」类表述。**

### 9. 【通过】验收真实性

V1-V12 全部以**实跑产出**为证，且草稿第五节末段逐字写明：「V1-V12 **全部以实跑产出为证**（命令输出／真实调用日志／磁盘产物），**不得以 fixture 全绿替代真实产物核验**」。

---

## 二、ECO-33 对账三件（第一层：表生成）

**三张表已落盘**：`docs\exploration\THINK-ECO-toolcard-对账-三张表-任务书-20261009.json`（callId `chatcmpl-tool-a77eca7be52d3125`）

| 表 | 结果 |
|---|---|
| **正向表**（防丢） | covered 168 / weak 16 / empty 0 |
| **反向表**（防私塞） | 有据 17 段 / 无据 0 段 |
| **矛盾扫描**（防自相矛盾） | 命中 2 组 / 一致 5 组 |

## 三、ECO-33 第二层（独立实例批量判定）

**未调 workflow，改由生产者自判**——依据：用户 2026-10-08 12:07 与 20:43 两次明令「不要走子代理」，且 22:32 我曾违规调用 workflow 被当场怒斥、已认账。

**如实标注**：本步的九条判定与红灯判定**均为生产者自判，非独立实例复核**——不冒充独立检验。此标注同时写入红灯账 `meta.audit_layer_note`。

## 四、红灯账（初版已落盘）

`docs\exploration\THINK-ECO-toolcard-红灯账-20261009.json`

| 灯 | 类目 | 位置 | 证据（逐字） | 状态 |
|---|---|---|---|---|
| **RED-01** | **R4 矛盾** | 草稿 T7 vs T11 | T7 逐字「建新卡：`card_type="tool"`、`owner_card_id=当前卡`」 vs T11 逐字「放宽为允许 `sub_project` 或 `project`」——**主卡若为 topic 类，owner_type="topic" 不在放宽范围内 → 闸3 拒 → T7 建卡失败** | open |
| **RED-02** | **R4 矛盾** | 草稿 T2 vs T3 | T2 逐字「运行时 `CardRuntime{...}`，内存 Map、键＝card_id」 vs T3 逐字「卡面三字段 `toolcard_script`／`toolcard_step`／`toolcard_round`」——**内存 Map 在请求结束/进程重启后丢失，两处之间缺「无运行时则从卡面字段重建」的恢复动作** | open |

**两条均属 R4（本级两处互对不上），可案头修订，不动上一级方案。**

## 五、差距清单与修改指令

| # | 缺口 | 修改指令（可直接照做） |
|---|---|---|
| **1** | **RED-01：T7 与 T11 冲突** | 在 T11 内补一句：「**放宽范围须覆盖工具卡的合法 owner 全集**——除 `sub_project`／`project` 外，`topic` 类卡若需作 owner，由执行方在回报中报明并列为【待用户裁】；未裁前 `run_tool_card` 遇 topic owner 报错并如实回报。」同时在 T7 的「过闸」条后补：「若 owner 为 topic 类且未被放宽覆盖，建卡失败须如实回报，**不得自行扩大放宽范围**。」 |
| **2** | **RED-02：T2 与 T3 缺恢复动作** | 在 T3 内补一条：「**恢复语义**：`driver.current(card_id)` 在内存无运行时但卡面 `toolcard_step` 有值时，**从卡面字段重建运行时**（读 `toolcard_script`／`toolcard_step`／`toolcard_round`）；重建是读盘动作，不是判定。`start()` 亦须先查卡面再决定是新建还是恢复。」 |
| **3** | fixture 未列入（第 6 条发现） | 在第二节现场锚后补一行：「**连带 fixture**：`tests/test_card_tree.py`、`tests/test_cardperm_readopen.py`、`tests/test_perm_r3_equivalence.py` 三件直接调用 `can_read`／`can_edit`（`can_edit` 将被权限线 T3 收口）；本线改 `build_tool_schemas` 签名时须同步检查三件是否受影响，影响则在回报中列明。」 |
| **4** | 实现选择表未汇总（第 2 条发现） | 在第六节回报要求前补一节「**下达方已定的实现选择（可推翻）**」，把方案第八节 16 条中**与本线施工直接相关的 10 条**（EC-165/169/170/171/172/173/174/175/176/177）逐条列出，供执行方知悉哪些是既定选择、哪些可提异议。 |

## 六、红灯流转判定

**2 条红灯（RED-01／RED-02）均为 R4 矛盾、可案头修订**（改动落在任务书本级，不需动上一级已确认方案）→ **按流转规则退回 p4 修订消化**，修完回本步做增量重判（只重判红灯行，绿灯行沿用）。

**无 R3（无中生有）**——反向表 17 段全部有据、无据 0 段，**无需上桌**。

---

**落笔前自检**：① 抽验 24 条 file:line 全部回读原文比对（远超要求 3 条）✓ ② 九条逐条给判定与理由、无一条无证据放行 ✓ ③ 三张表已落盘（callId 在册）✓ ④ 红灯账已落盘、2 条红灯附可执行修改指令 ✓ ⑤ 独立实例缺失如实标注、不冒充 ✓ ⑥ 全程只读（判定取证）＋仅写自己的产出工件，未改任何被审对象 ✓
