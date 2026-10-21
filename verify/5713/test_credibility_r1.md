# 5713 测试可信度 · 第一轮交付

**对象**：`D:\AI_station\推演agent\.worktrees\5713`，分支 `5713` @ `7cdc6ac`
**轮次口径**：首轮只取证，**不改代码、不改断言**
**硬边界**：全程只在 5713 worktree 与 `_flake_ev` 沙箱内操作，未触碰 5700

---

## 0. 一句话结论

**5713 的测试结果现在不能当判据用。** 不是因为"偶发失败"太多，而是因为**干净树上就有 75 条确定性红**（占可跑用例 6.4%）——任何真实的回归信号都被这 75 条噪声淹没，跑出红你无法区分"新坏"和"老红"。同时另有一类更隐蔽的问题：**2 条真 flake**（同 commit 同环境重复跑，结果翻转），任何把它们算进红线的判据都是假的。

要让它能用，只需两件事（见 §7）：**先立红基线 + 增量闸**，**再修 2 条真 flake**。这两件做完，剩下的红就都变成"可解释的历史欠账"，可以按序清偿。

---

## 1. 证据方法（全部可复现，不依赖推测）

| 编号 | 实验 | 命令 | 作用 |
|---|---|---|---|
| E1 | 基线重复跑 ×5 | `python -m pytest tests -q --junitxml=junit_runN.xml`（cwd=5713 worktree） | 同 commit／同环境／同命令重复跑，**结果翻转＝真 flake**（机械判据，无需读代码） |
| E2 | 隔离单跑 | `python -m pytest <nodeid> -q` 每条失败用例独立进程跑一次 | 区分"套件内才红"（环境/顺序依赖）与"单跑也红"（系统性） |
| E3 | 契约时间线 | `git log -S <常量/错误码> -- src config` ／ `git log -1 -- <测试文件>` | 比较"生产码改动提交"与"测试最后改动提交"的先后，机械化区分断言滞后与真回归 |
| E4 | 三档作用域对比 | 单测隔离 ×8（空载 4 ＋ CPU 负载 4）／整文件 ×5／整套 ×5 | 定位 flake 触发器：**负载不复现、文件作用域复现** ⇒ 顺序/累积状态依赖，而非时间窗或 CPU 竞争 |

原始产物：`_flake_ev/`（`junit_run1..5.xml`、`runs.json`、`matrix5.json`、`isolation.json`、`r1_assert_tails.txt`、`skip_reasons.txt`、`timeline.txt`、`groups.txt`）。
环境：Python 3.11.9，pytest 9.1.1，pytest-asyncio 1.4.0。**无 pytest-xdist / pytest-randomly / pytest-timeout / playwright**（这决定了下面所有"顺序相关""超时"类 flake 都无法被工具自动收敛）。

---

## 2. 基线数字（E1，5 轮，单轮 209–216 秒）

| 类别 | 数量 | 含义 |
|---|---|---|
| 用例总数 | 1179 | `--collect-only` 采集数 |
| **5 轮全红** | **75** | 确定性失败：不是噪声，是"本来就红" |
| 5 轮全绿 | 977 | 真正在守的用例 |
| 全跳过 | 125 | 不参与任何判定（见 §8 覆盖盲区） |
| **结果翻转（真 flake）** | **2** | `pass/FAIL` 在不同轮次间变动 |

上限口径：**任何"跑绿了"的结论必须附带轮次与作用域**。单轮全绿在 977/1179 的绿率下不具备判据力；单轮出现红，必须先过 §6 的噪声闸。

**E2 隔离复现结果（重要）**：75 条确定性红**全部单跑也红**（75/75，`_flake_ev/isolation.json`），**没有一条是"套件内才红"的伪红**。也就是说这 75 条不是环境/顺序造成的假象，而是代码与断言之间真实存在的分歧——这让 §4/§5 的定性有了硬地基。

---

## 3. 真 flake 清单（2 条，每条附机制与复现）

判定依据：**同 commit `7cdc6ac`、同命令、同机器，5 轮结果不一致**。这不是分析推断，是直接的观测事实。

### F1 · `tests/test_board.py::test_apply_revision_dual_ledger_and_dirty_flush`
- **观测（整套跑 ×5）**：`pass, FAIL, pass, pass, pass`（1/5 红）
- **三档作用域实测（E4）**：
  - 单测隔离 ×8（空载 4 ＋ CPU 负载 4）：**8/8 全绿** ⇒ 加负载**不复现**，"纯时间窗/CPU 竞争"假说被实验**证伪**；
  - 单文件 `tests/test_board.py` ×5：**5/5 有红，其中 3 次含本用例**（run3–5）⇒ 在更小的上下文里红得更频繁；
  - 整套 ×5：1/5 红。
- **结论**：触发器是**同文件内前序用例累积的进程／文件状态**（后台 dirty-flush 任务、card_db 连接缓存、共享 event loop 之类），**不是 CPU 竞争、也不是单纯的时间窗**。更要紧的是：**这条用例的"绿"是偶然，"红"才是常态**——这是"单次全绿不能当判据"最硬的例证。
- **代码层事实（已核实，但不构成触发器证明）**：`tests/test_board.py:77` 的 `_wait_disk(path, pred, tries=300)` 以 300×0.01s ≈ 3 秒固定窗口轮询，等异步 dirty-flush 把 `revisions` 落盘（期望 `len(revisions)==5`），窗口到点抛 `AssertionError: 双账状态未达预期`。固定窗口把"等待"写死成时间赌注，前序状态一旦让 flush 变慢就假红。
- **复现实验（两条命令的差异本身就是证据）**：
  - 红：`pytest tests/test_board.py -q` 连跑 5 次 → 5/5 有红、3 次命中本用例
  - 绿：`pytest tests/test_board.py::test_apply_revision_dual_ledger_and_dirty_flush -q` 连跑 8 次 → 8/8 绿

### F2 · `tests/test_card_msg.py::test_k3_live_a_to_b_invokes_receiver`
- **观测（整套跑 ×5）**：`pass, pass, FAIL, FAIL, pass`（2/5 红，本轮最不稳定）
- **三档作用域实测（E4）**：
  - 单测隔离 ×8（空载 4 ＋ CPU 负载 4）：**8/8 全绿** ⇒ 加负载不复现；
  - 单文件 `tests/test_card_msg.py` ×5：**4/5 绿、1/5 红** ⇒ 同文件上下文即可触发；
  - 整套 ×5：2/5 红。
- **代码层事实（已核实）**：等待循环以 **"B 的会话里出现 assistant 块"** 为退出条件（60×0.05s ≈ 3s），随后**断言另一件事**：`inbox` 行 `status == 'consumed'`。两者**不是同一个不变量**——assistant 块先落、inbox 行后标 consumed，中间有一个真实写入窗口；前序用例留下的状态把这一侧拖慢，断言就落进窗口读到 `pending`。
- **根因归类**：**顺序/累积状态依赖 ＋ 轮询谓词与断言目标不一致**（等待了一个前提，却断言了另一个后果）。
- **复现实验**：
  - 红：`pytest tests/test_card_msg.py -q` 连跑 5 次 → 1/5 红
  - 绿：`pytest tests/test_card_msg.py::test_k3_live_a_to_b_invokes_receiver -q` 连跑 8 次 → 8/8 绿
- **危害**：卡间消息（本轮刚改过通信边界的那条线）的验收用例本身不稳，红绿都不可信。

> 两条 flake 是**两个不同的机制**，共同点是「用不严谨的异步等待代替同步原语」。5 轮样本量下 1/5、2/5 的失败率置信区间很宽，**只能定性为"存在"，不能定量为"5%/40%"**——要给准概率需 ≥50 轮，成本不划算，先修机制即可。

---

## 4. 断言滞后清单（确定性红，生产契约已前移、断言未跟）

判定依据（E3，机械化）：**该断言钉的期望值属于过去某次提交时的生产事实；产出实际值的那次提交晚于测试文件的最后一次改动，且该提交未同步该断言。**

### G1 · 提示词版本常量 `t22-1` → `t24-1`（10 处）
- 实际值来源：`src/api/prompts.py:56` `BASE_PROMPT_VERSION = "t24-1"`，由 **`6414ae3`（2026-10-19 17:47，「实验分支快照：CEO 角色段 + 三块语义 …」）** 引入。
- 断言侧：10 条用例仍钉 `'t22-1'`，其最后一次改动分别在 2026-10-16（`beb408c`）／10-18（`7281076`），**都在 10-19 之前**。
- 涉及：`test_auto_mode`（version_and_section、rework_2_versions、auto_prompt_version_after_p7、rework_1_base_prompt_byte_identical_to_t22_1）、`test_card_draft`、`test_card_region_semantics`、`test_rule_card`、`test_status_update`、`test_t12_memory`、`test_auto_await_livelock`。
- **归类：断言滞后**（纯字面常量，改测试即可，无产品风险）。
- **复现**：`python -m pytest tests/test_auto_mode.py::test_rework_2_versions tests/test_card_draft.py::test_base_prompt_version_is_t20_1 -q`

### G2 · goal 工具直调守卫 `GOAL_TOOL_DRIVER_REQUIRED`（22 处，本轮最大簇）
- 实际值来源：错误码由 **`1559219`（2026-10-20 10:12，「自动模式照抄 dsh-goal：事件溯源 + idle 驱动 + 轮次上限 + 权威校验」）** 引入；该提交改了 **23 个文件、2347 行改动、`tests/` 0 个文件**。
- 断言侧：22 条用例断言"直接调 `update_goal` 等 goal 工具应成功"，实际返回 `{'code': 'GOAL_TOOL_DRIVER_REQUIRED', 'message': 'goal tools require an open model turn'}`。
- **归类：断言滞后（但性质更重，见下）**——测试钉的是"工具可被直调"的旧契约；生产改成了"goal 工具必须在一次打开的模型回合内被驱动"。
- **必须裁定的一点**：这不是"改个常量"能了结的。它是一条**对外契约变更**（谁能调 goal 工具），而落地时**没有同步任何测试，也没有留裁定**。要么承认新契约并重写这 22 条（走模型回合驱动），要么认定直调仍是合法入口（那就是真回归、要改代码）。**建议 CEO 层直接裁定，不要交给执行者猜。**
- **复现**：`python -m pytest tests/test_auto_mode.py::test_block4_update_goal_complete_sets_state tests/test_decision_gate.py::test_complete_rejected_without_decision -q`

### G3 · 工具集／权限集合常量化石（9 处）
- 断言钉死具体数目与集合：工具数 `35 vs 32`（`test_coder_card`、`test_dep_relations`，按 `": ToolDef("` 计数）、工具总数 `51 vs 50`（`test_tool_capability_boost`）、auto 面工具集与 `AUTO_PERMS_VERSION`（`test_auto_mode` 4 处）。
- 实际值来源：工具表在 10-19→10-20 间连续变动（`6414ae3` 加工具、`1559219` 加 goal 工具面、`580c817`／`a0853ed` 改卡间通信边界）。`AUTO_PERMS_VERSION` 由 `620c6bf`（10-17）引入，用例取到 `-1` ⇒ 该符号已不在被测模块的面上（迁移到新模块）。
- **归类：断言滞后**（数目型断言天生滞后，应改为"集合包含关系/能力断言"而非计数）。
- **复现**：`python -m pytest tests/test_tool_capability_boost.py::test_seven_new_tools_registered tests/test_coder_card.py::test_toolset_fingerprints_and_switch_bindings -q`

### G4 · 提示词文案逐字断言（7 处）
- 断言把整段说明书原文钉死（如"【步骤执行】你现在执行计划的一步…"、"接口与约定"、"当前确定状态"、auto 版工具行 `update_goal(action, reason?, await_children?)`）。文案随 t24-1 重写而变。
- 其中 `test_rule_card::test_inject_section_by_card_type` 直接 `ValueError: substring not found` —— 断言用的是 `str.index`，文案一变就抛异常而非断言失败，**连"哪里变了"都不报告**。
- **归类：断言滞后**（文案是易变物；应改为"结构/语义锚点"断言，不该钉全文）。
- **复现**：`python -m pytest tests/test_board.py::test_step_brief_template_verbatim tests/test_rule_card.py::test_inject_section_by_card_type -q`

### G5 · 模型名常量（2 处）
- `test_condfold`：期望 `glm-flash`，实际 `glm-53-flash`。`git log -S "glm-53-flash"` 命中 5 个提交（`b13fc60`、`f4417ea`、`35c6e5b`、`6414ae3`、`cf30128`），模型分层在 10-13~10-20 间反复迁移；`config/models.yaml` 最后一次改动 `b13fc60`（2026-10-20「config(tiers): 第一层换 aliyun deepseek-v4.1-flash-1017」）。
- `test_offline_e2e`：期望 `[mock:haiku]`，实际 `[mock:minimax-m31-flash-preview]`。
- **归类：断言滞后**（模型名是配置，不该被测试硬编码成常量）。
- **复现**：`python -m pytest tests/test_condfold.py::test_v3_condense_call_targets_glm_flash tests/test_offline_e2e.py::test_mock_llm_round_trip -q`

---

## 5. 行为语义类（25 处）· 已定性为"必须逐条判"，本轮不下断言

这 25 条**不是常量漂移**，断言的是**行为后果**。它们的失败可能是真回归、也可能仍是断言滞后，**必须用 §6 第 3 步（真实链路行为判据）逐条定性**，本轮不代替证据下结论。按风险排序列出：

**S1 数据安全语义（最高风险，优先取证）**
- `test_snapshot_roundtrip::test_snapshot_restore_roundtrip`：`restore_from_snapshot` 返回 `ok=True`，但读回仍是恢复后写入的"第二版正文"，**未回到快照态**。已核实代码路径：恢复前 `card_db.close_work()` 关连接 → 复制库文件 → 删 `-wal/-shm`（删不掉只 warning 不阻断）。**若"返回 ok 但实际没回滚"，这是会骗人的恢复**——必须优先证实。
- 复现：`python -m pytest tests/test_snapshot_roundtrip.py::test_snapshot_restore_roundtrip -q`

**S2 模型解析优先级（影响所有"用哪个模型"的判断）**
- `test_model_override_scope` 2 条：期望无卡时回到全局默认 `m1`，实际取到 work 级 `m2`。测试钉的正是 `fd11ce3`（2026-10-19「按卡片类型设置默认模型」）声明的**新**规则（项目级不再有单一默认），所以**代码很可能没跟上自己声明的契约** → 偏真回归。
- 复现：`python -m pytest tests/test_model_override_scope.py -q`

**S3 回合收口语义（人停优先权）**
- `test_slots_control::test_midflight_stop_closes_turn_human_stop`：正文交付正确（`"罐头\n"`），但块尾 `end` 是 `model_done` 而非 `human_stop`——**人按停的优先权丢了**。这条同时具备 flake 嫌疑需先过噪声闸。

**S4 goal/auto 语义族（18 条）**
- `goal_await_wakes` 计数不按新语义清零／置位（`test_auto_await_livelock` 6 条）、`goal_await_children` 标志位不置（`test_auto_await_explicit` 5 条、`test_auto_await_resume` 4 条）、驱动/默认值/唤醒时序（`test_auto_mode` 3 条：`block3_create_child_with_goal_kicks`、`block3_driver_runs_after_mutex_release`、`block3_shipped_default_is_serial`）。
- 这一族与 G2 同源（`1559219` 整体重写 goal 子系统、`src/goal/*` 全新模块），但**断言的是行为不是常量**，不能顺手打成"断言滞后"。
- 复现：`python -m pytest tests/test_auto_await_livelock.py tests/test_auto_await_explicit.py tests/test_auto_await_resume.py -q`

---

## 6. 机械判定口径：**一次失败，是真回归还是噪声？**

判据设计原则：**结论必须能由"跑一次命令 + 查一次 git"得出**，不依赖对代码的印象。

### 第 0 步（前置闸）：先问"这套测试现在有没有判据力"
- `当前确定性红数 > 0` ⇒ **判据失效**：单次红不可用，只能看**相对已记录基线的增量红**。
- 基线的定义：同一 commit、同一命令、连续 ≥3 轮，**取"每轮都红"的集合**记为 known-red 基线；只有"基线外的新红"才是回归候选。
- 当前基线（commit `7cdc6ac`）：**75 条 known-red**，清单见 `_flake_ev/matrix5.json` 的 `stable_red`。

### 第 1 步（噪声闸）：同 commit 重跑，任一次绿 ⇒ 噪声
- `python -m pytest <nodeid> -q` 连跑 3 次 + 套件内跑 3 次。
- **出现至少一次绿 ⇒ 判「噪声/自不稳定」，禁止当回归判据**，转第 4 步查机制。
- **6 次全红 ⇒ 系统性**，转第 2 步。
- 依据：这是纯观测判据，不需要任何因果假设。当前命中：F1、F2。

### 第 2 步（契约溯源）：系统性红的归因
- 取 `T = git log -1 --format=%H -- <测试文件>`（断言最后改动）、`C = git log -S <实际值来源符号> -- <源码目录>`（产出实际值的提交）。
- **若 `C` 是 `T` 的后代，且 `C` 未修改该测试文件 ⇒ 断言滞后**（生产前移、断言未跟）→ 修测试。
- **否则 ⇒ 回归候选**，转第 3 步。

### 第 3 步（行为判据）：断言钉的是"后果"时的唯一可靠办法
- 常量型断言（版本号/计数/文案/模型名）到此即可判。
- **行为型断言（工具能否直调、恢复是否生效、优先级谁高）不能靠字面比对定性**。必须写 10 行独立脚本走真实链路，只问一件事：**该提交说明所声明的意图，真实链路做到了没有？**
  - 真实链路符合意图、断言不符 ⇒ 断言滞后（改测试）。
  - 真实链路也不符 ⇒ **真回归**（改代码）。

### 第 4 步（噪声归因）：给 flake 定族再修
- 固定时间窗（sleep/固定轮询次数）／轮询谓词与断言目标不一致／跨用例共享状态／并发写同一文件——各配一个定性实验（单跑 vs 套件、加负载、改顺序）。
- **禁止的处置**：给 flake 加重试（`--reruns`）或放宽断言阈值。那只会把"偶发红"变成"永远绿"，把噪声藏起来而不是修掉。

### 一句话版
> **先看是不是噪声（重跑），再看是不是欠账（git），最后看行为本身对不对（真链路）。三步之外的任何判断都是猜。**

---

## 7. 稳定化优先序

排序依据：**是否阻断判据成立** × **修复成本** × **风险**。

| 优先级 | 事项 | 为什么排这里 | 完成判据 |
|---|---|---|---|
| **P0** | **立红基线 + 增量闸**：把当前 75 条 known-red 固化进基线文件，判据只认"基线外新红"，且新红必须先过第 1 步噪声闸 | 不做这步，其余所有工作都在噪声里做。**这是"能不能当判据"的开关** | 一条命令能输出「新增红／已清红／剩余 known-red」三张表 |
| **P1** | **修 2 条真 flake**（F1/F2：固定窗口等待 ＋ 轮询谓词≠断言目标；实测为**顺序/累积状态依赖**） | 数量最少、成本最低、**却直接决定红度量的真假**。P1 不做完，P0 的基线会一直漂 | **三档作用域都要全绿**：单测 ×20、整文件 ×20、整套 ×5。只在单测档全绿是假达标（本轮实测：单测 8/8 绿，整文件仍会红） |
| **P2** | **常量型断言滞后批量对齐**：G1(10)+G3(9)+G4(7)+G5(2)=**28 条** | 纯机械、零产品风险，一次可清 28/75（37%），且这类断言改成"集合/锚点/配置读取"后**不再复发** | 28 条转绿，且断言不再硬编码版本号/计数/文案全文 |
| **P3** | **裁定 G2 的契约归属**（goal 工具直调，22 条） | 卡在"这是要的新契约还是回归"上——**必须由 CEO/需求层裁定**，执行者无权自决。裁定前这 22 条保持 known-red | 一条裁定写进定论：新契约成立→改 22 条测试；不成立→改代码恢复直调 |
| **P4** | **S1/S2/S3 逐条按第 3 步定性并修**（数据安全 > 模型解析 > 人停优先权） | 这三条若为真回归，伤害的是**正确性本身**，不只是测试；但需要真实链路取证，成本高于 P2/P3 | 每条给出"真回归/断言滞后"定性与实测脚本 |
| **P5** | **S4 goal 语义族 18 条**随 P3 的裁定一起收敛 | 与 P3 同源，等裁定即可顺势做，单独动没意义 | 随 P3 一并转绿 |
| **P6** | **补回覆盖盲区**（124 条因「纯单模型模式」整块跳过） | 129 条不参与判定＝盲区；但优先级低于"现有判据可信" | 明确这些盲区是被废弃还是应恢复，给出清单与理由 |

**建议的验收目标**：P0+P1 做完 ⇒ 测试结果**可以**当判据用（增量闸可信）；P2 做完 ⇒ 剩余噪声降到 47 条可解释欠账；P3/P4/P5 做完 ⇒ 全绿基线成立。

---

## 8. 覆盖盲区（不参与任何判定的部分）

- **125 条跳过**：124 条同一原因「纯单模型模式（2026-10-17 用户令）：思维链已拆出生产链路」——**整块功能不再被测试覆盖**；1 条需 `CARDPERM_LIVE=1`（真模型实弹）。
- **前端 DOM 用例（`tests/*.mjs`、`s2/s3/s4/`）不在 pytest 采集内**：环境无 playwright/node 测试入口接入 pytest，**画布/前端这一大片目前没有任何自动化判据**。
- 结论：**"全绿"这个词在 5713 上永远不等于"全都验过"**——1179 里只有 977 条真的在守，另外 202 条（17%）不参与判定。

---

## 9. 复现实验索引（可直接照抄执行）

```bash
# 目录
cd /d D:\AI_station\推演agent\.worktrees\5713
PY=D:\AI_station\推演agent\.venv\Scripts\python.exe

# E1 基线重复跑（判 flake，5 轮）
for i in 1 2 3 4 5; do $PY -m pytest tests -q --junitxml=_flake_ev\junit_run$i.xml; done

# E2 隔离单跑（判"套件内才红"）
$PY -m pytest tests/test_board.py::test_apply_revision_dual_ledger_and_dirty_flush -q

# E3 契约时间线（判断言滞后）
git log -S "GOAL_TOOL_DRIVER_REQUIRED" -- src
git log -S '"t24-1"' -- src/api/prompts.py
git log -1 -- tests/test_auto_mode.py

# 样本：两簇的代表性复现
$PY -m pytest tests/test_auto_mode.py::test_block4_update_goal_complete_sets_state -q     # G2
$PY -m pytest tests/test_card_draft.py::test_base_prompt_version_is_t20_1 -q              # G1
$PY -m pytest tests/test_snapshot_roundtrip.py::test_snapshot_restore_roundtrip -q        # S1 风险最高

# E4 三档作用域（证伪"负载/时间窗"，定位"顺序累积状态"）
$PY -m pytest tests/test_board.py -q                     # 5 次：5/5 有红、3 次命中 F1
$PY -m pytest tests/test_board.py::test_apply_revision_dual_ledger_and_dirty_flush -q   # 8 次：全绿
$PY -m pytest tests/test_card_msg.py -q                  # 5 次：1/5 红（F2）
```

---

## 10. 本轮明确没做的事

- 没有改任何产品代码、没有改任何断言（遵守首轮边界）。
- 没有对 G6/S1–S4 的 25 条行为类下"真回归"结论——**证据不足就不下结论**，只给出判定路径与排队顺序。
- 没有给出 flake 的定量失败率（5 轮样本太窄），只定性"存在"与机制。