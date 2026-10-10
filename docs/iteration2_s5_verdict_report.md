# 迭代二·S5 全量验证报告（十五条判据逐条）

> 任务书：《迭代二施工任务书-20261010.md》（基准时点 12:24 修订版，首行实读确认）。
> 判据源：《迭代二方案-20261010.md》§3 判据 1-15（:48-66 逐字）。施工面：S1-S4 已交付
> （board.py v2 / plan_tools.py 七工具 / triage·finalize·model_pick / 接缝四处）。
> 本报告所有数字均来自 junitxml 权威指标（`_pytest_full_iter2_final.xml`）与脚本实跑输出，
> 无 fixture 全绿替代。

## 一、总判定

**十五条全过，S5 验收通过。**

| 维度 | 权威数字 |
|---|---|
| 全量回归（相对 `_pytest_baseline_iter1.xml`，迭代一交卷 12:11 基线） | 总数 213 → **299**（净增 86） |
| failed | 基线 20 / 本次 20，**双向差集为空**（逐名比对，零新增零消失） |
| errors / skipped | 0 / 1 |
| 本迭代新增测试件 | 7 文件 53 用例（board 10＋plan_tools 17＋triage 7＋finalize 5＋model_pick 4＋s4_seams 7＋s5_verdicts 3）**全绿** |
| 强杀演练 | `scripts/s5_crash_drill.py` **DRILL PASS**（真 taskkill /F 硬杀＋全新进程恢复） |

## 二、十五条判据逐条

### 判据 1 · 自判分流实测 —— 过
- **动作**：沙箱 HTTP 全链路（ASGITransport＋MOCK_LLM）发两条真消息：「你好呀」（寒暄级）／「帮我做三件事」（任务级），triage 判定注入桩。
- **证据**：`test_s4_seams::test_triage_direct_limits_tools_to_escape`（direct：triage 块落档含 "direct"）；`test_triage_plan_keeps_full_tools`（plan：全量工具面含 create_plan/step_complete/escalate_to_plan）。
- **结论**：direct→直答席（工具面收窄）、plan→拆解流（板文件出现），分流实测成立。

### 判据 2 · 解析失败默认拆解 —— 过
- **证据**：`test_triage::test_branch_illegal_defaults_to_plan`（非法 JSON→默认 plan）；`test_branch_call_exception_defaults_to_plan`（模型调用异常→同样默认 plan）；两者均有 caplog 日志留痕断言。
- **结论**：解析失败/异常双路默认拆解＋留痕，代价不对称设计落地。

### 判据 3 · 直答无工具 —— 过
- **证据**：`test_s4_seams::test_triage_direct_limits_tools_to_escape` 断言 `gw.calls[0]["tools"] == ["escalate_to_plan"]`（清单数量=1，仅逃生口）。
- **结论**：直答回合工具面物理收窄至仅 escalate_to_plan。

### 判据 4 · 逃生口实测 —— 过
- **证据**：`test_s5_verdicts::test_escape_hatch_real_call`——direct 席模型**真调** escalate_to_plan；当轮终止（deliver/escalate）；tool_events 中 `ok=True`；档案补 behavior=escalate 块、content 含 reason 原文（绝不静默换路的留痕面）。
- **结论**：逃生口真调→当轮转拆解流→档案连续在案。

### 判据 5 · 拆解上板（含一步板）—— 过
- **证据**：`test_plan_tools::test_create_plan_returns_first_prompt_verbatim`（返回值＝steps[0].prompt **逐字**，模型写入参数零加工）；`test_board::test_create_board_persists_and_logs`（落盘 JSON 每步三字段 id/prompt/status 齐＋ops 留痕）；一步板：`test_s4_seams::test_catalyst_nags_until_step_complete` 全链路用 1 步板跑通。
- **结论**：上板形态与「提示词逐字＝模型参数」判据成立。

### 判据 6 · 改板四动作 —— 过
- **证据**：`test_plan_tools::test_plan_reorder_positive_and_negative`（挪）、`test_plan_insert_remove_modify`（插/删/改）、`test_board::test_apply_revision_dual_ledger_and_dirty_flush`（revisions[] 追加＋ops 双留痕＋置脏异步合并落盘）。
- **结论**：四动作各一次真跑，revisions 记录与改前改后快照齐（结构事实代码记满、理由可空）。

### 判据 7 · 中断恢复实测 —— 过（真 taskkill 硬杀）
- **动作**：`scripts/s5_crash_drill.py` 三阶段——①子进程 A 建 4 步板、推完第 1 步（＝执行到第 2/4 步：s1 done、指针指 s2）、读盘确认后挂住；②父进程 `taskkill /F /PID … /T` **硬杀**（A 退出码=1，非 0＝未走任何清理路径）；③全新子进程 B（内存板注册表必空）恢复序 get_board→同卡 HTTP 再发消息。
- **证据**（脚本实跑输出，DRILL PASS）：
  - 恢复态精确：`current=1(s2) s1=done s2=todo total=4`；
  - t165 装配区指 `2/4` 且带 s2 原文「第二步要求BBB」、不含 s1 原文；
  - 3 次 step_complete 恰好收口（若从头重跑需 4 次且 s1 被二次执行）；自判零调用；
  - 收口三件：板文件清理＋无活板＋final 块落档；
  - 沙箱验后销毁，真实 works 零触碰。
- **结论**：强杀→重启→同卡再发→**从第 2 步继续（不从头、第 1 步不重跑）**——直接对标 10:42③「不要 dsh 那个 bug」。

### 判据 8 · 催化实测 —— 过
- **证据**：`test_s4_seams::test_catalyst_nags_until_step_complete`——两轮空手均被催（催促文含本步要求原文「本步要求原文XYZ」＋「step_complete」＋「未调之前这一步不算完成」）；第三轮补调推进；圈 4 为工具结果回灌轮（"工具结果已返回。"）；末步交作业→收口→无活板→正常交付。
- **结论**：催化双挂点（主循环＋统合官路径）实测成立。

### 判据 9 · 防伪造 —— 过
- **证据**：`test_s5_verdicts::test_forgery_claim_still_nagged`——模型正文声称「已调 step_complete」但 tool_calls 为空（判据＝tool_calls 事实）→ 三圈催促文均带本步要求原文＋step_complete 字样，伪造不改变推进判据。
- **结论**：嘴上伪造无效，判据锚在工具调用事实。

### 判据 10 · 人停刹车 —— 过
- **证据**：`test_s5_verdicts::test_human_stop_brakes_nag_loop`——催化循环进行中 `POST /stop`→`stopped=True`→下一圈入口 check_control 消费（consume-once）→回合闭合，档案末 assistant 块 `end=human_stop`。
- **结论**：两道天然刹车之一（人停）真跑成立；另一道（用户新消息=新回合）由 T084 互斥机制承载（既有）。

### 判据 11 · 六条推不动防护 —— 过
- **证据**：`test_plan_tools::test_guard_1_no_board_plain_text`（无板→明文普通文本）、`test_guard_2_duplicate_submit_idempotent`（重复交→幂等返回当前步）、`test_guard_3_empty_result_not_advanced`（参数空→「未推进」＋催促）、`test_guard_4_board_cleaned_stale_reference`（清理后调推进→明文「已终结」）、`test_guard_5_truncated_board_plain_error`（断尾→明错）、`test_guard_6_lock_timeout_plain_error`（锁超时→明错）；恢复重建：`test_board::test_restore_after_restart_keeps_pointer`；清理后拒绝：`test_board::test_cleanup_then_stale_reference_rejected`。
- **结论**：六防护＋恢复重建逐条真跑，四态零异常零静默。

### 判据 12 · 收口三件 —— 过
- **证据**：`test_finalize_flow` 5 用例（结论回写卡面 thinkchain_final 区四样／产物核对未声明不强造／final 块落档／finalize_board＋cleanup_board，永不抛）；产物落库：`test_board::test_save_artifact_async_lands_file`（落 `works/{w}/thinkchain/{c}/{seq}-{step_id}.md`）；档案五类块齐：triage（test_triage）、plan＋revision（`test_plan_tools::test_plan_and_revision_blocks_landed`）、step＋final（`test_s4_seams::test_catalyst` 断言 behaviors 含 step＋final）；执行期间卡面零写入由 finalize 形态保证（照 toolcard/finalize.py:57-99 四件序）。
- **结论**：收口三件齐，五类块落档有据。

### 判据 13 · 垃圾不积压 —— 过
- **证据**：`test_board::test_orphan_three_states_no_timestamp`（孤儿三态：终态卡删／已删卡删／活卡留，**零时间戳判据**）；`test_s4_seams::test_catalyst` 断言收口后 `plan_path` 不存在＋无活板；启动钩子：`main.py` startup_init 接 `sweep_all_orphans()`（fail-open，已 COMPILE_OK）。
- **结论**：终态即清＋重启扫描兜底，垃圾不积压。

### 判据 14 · 模型选择 —— 过
- **证据**：`test_model_pick` 4 用例（`pick_model(role="default")` 恒返 main、查表函数、缺表兜底）；强杀演练 B 阶段三步推进全部经 `model_service.call_main`（ScriptedGateway.calls 记录即调用日志，三步无一走非 main 槽位）。
- **结论**：所有步打 main＋model_pick 经查表函数成立（MOCK 环境下「日志核」口径＝gateway 调用记录；真实日志核留部署窗口）。

### 判据 15 · 边界 —— 过
- **类型四件零痕迹**：本迭代新增文件清单（src/thinkchain/ 八件、config/model_pick.json、tests 七件、scripts 两件）不含任何类型清单/注册表/判据/路由表文件；`config/behavior_registry.json` 仍为迭代一空壳 `"types": {}`（mtime 10-10 10:32＝迭代一时点）；`card_type_registry.json` mtime 10-09（更早批次）。
- **X 线五件零触碰**（方案 §2.2「不动的」五类，与红线 2 同构）：
  1. `routes.py`——M 状态为迭代一遗留 diff，全文零迭代二痕迹（唯一关键词命中为迭代一注释「排队策略归迭代二」预留字样）；
  2. `session_store.py`——diff 零迭代二痕迹（0 命中）；
  3. `session_facade.py`——untracked（迭代一新建件），内容仅 2 处「迭代二」字样均为迭代一注释预留，零 thinkchain 引用；
  4. `toolcard/` 七文件——`git diff HEAD -- src/toolcard/` 零改动（0 文件）；
  5. 提示词文件——`src/api/prompts.py` 零改动（git status 空）。
- **结论**：必不含件一件没碰，边界干净。

## 三、R2 回流确认件（代拍一致性呈报）

`docs/iteration1_slots_spec.md` :28-34 代拍决议 A 原文：「方案板按**会话实例数据**落 `works/{work_id}/`，与工具卡剧本库（`toolcard_scripts/`，全局共享）分层；不做全局剧本库挂载」。

**迭代二实际落地与决议 A 完全一致**：板文件落 `works/{work_id}/plans/plan_{card_id}.json`、步产物落 `works/{work_id}/thinkchain/{card_id}/{seq}-{step_id}.md`——均在会话实例数据目录之下（路径动态取 `session_store.WORKS_DIR`，沙箱跟随）；无任何全局剧本库挂载形态。决议 A 按任务书 R2 条款回流确认，呈报用户知悉（用户可事后推翻，推翻时迁移责任由推翻方裁定）。

## 四、施工裁定呈报清单（累计 14 项）

| # | 裁定 | 性质 |
|---|---|---|
| 1 | 锁内同步写落盘（任务书「锁内挪指针、锁外写盘」与验收④存在竞态——flush 旧快照会回退覆盖新指针；`_atomic_write_json` 持锁微秒级无 await 让出） | 竞态规避 |
| 2 | card_store.py 两处同改（update_card 键清单＋_render_card 条件渲染行）——「不进清单即被静默丢弃」源码注释实锤后的必要施工 | 任务书条款补全 |
| 3 | 本迭代测试落 `tests/`（任务书未指路径） | 位置裁定 |
| 4 | S4 接缝四处插入位（message_runner 自判 :268／催化 :331＋:518、context_assembler t165 互斥 :482 else 分支） | 位置呈报 |
| 5 | prior 投影过滤 `if not m.get("behavior")`（session_facade 落盘 role 恒由 actor 映射，环节留痕块进投影会炸真实端点；旧块无 behavior 键零影响金样本安全） | 接缝必要施工 |
| 6 | escalate 消费接缝（工具循环内识别标记→deliver/escalate，下轮 triage 自然重判）——任务书条款空隙 | 空隙补全 |
| 7 | triage 块 end=""（END_VALUES 四值不含 direct_answer/plan_created，落盘层兼容） | 口径裁定 |
| 8 | 对照硬门禁分层口径（对话层逐字 diff=0 硬门禁；环节层为 S3 明文新增产物不在「与改动前一致」约束域） | 口径裁定 |
| 9 | 工具卡席含七件（EC-131 现状：思维链七工具注册进 TOOL_REGISTRY 19→26） | 现状呈报 |
| 10 | main.py 一行启动钩子 sweep_all_orphans（R2-③ 启动扫描，fail-open） | 授权内施工 |
| 11 | 测试断言随新档案形态更新（test_t14/test_consumer_switch/test_uiopt_e2e/test_perm_gateway_acceptance——行为正确、断言口径更新，非回归） | 断言更新呈报 |
| 12 | **ScriptedGateway 加 `await asyncio.sleep(0)` 让出点（S5 诊断定案：Mock 网关零 IO → 催化循环独占事件循环 → 人停刹车饿死不可达 → 挂死；生产链路每圈真实网关 IO 必然让出、刹车语义有效——测试件缺陷，生产代码零改动）** | 本轮新增·测试件修复 |
| 13 | **强杀演练「从第 2 步继续」证据口径＝t165 装配区指 2/4 带 s2 原文＋3 次 step_complete 恰收口（从头需 4 次）；首圈 user_prompt＝用户原文是既有行为、非推进判据** | 本轮新增·口径呈报 |
| 14 | 全量回归存量 M 文件（event_stream/activity/memory_store）经 diff 关键词核验零迭代二痕迹 | 本轮新增·边界核验 |

## 五、红线八条自查

| 红线 | 状态 | 依据 |
|---|---|---|
| 1 生产 5700 零触碰 | ✓ | 本会话零生产进程操作；全部验证走进程内 ASGITransport＋临时沙箱 |
| 2 不动 toolcard/提示词/迭代一施工面 | ✓ | 判据 15 五件逐项核验（见上） |
| 3 message_runner 三处＋context_assembler 一处 | ✓ | S4 施工记录与 diff 一致（自判/催化双挂点/escalate 消费；t165 互斥） |
| 4 旧防护零退役 | ✓ | 重发去重/写锁/pair 全程带跑，未动旧防护代码 |
| 5 新工具只操作 plans/＋thinkchain/ 两目录＋板子内存 | ✓ | board.py 路径面白名单；card_store.py:4 铁律辖区零接触（仅授权两处键位） |
| 6 测试数据＝生产副本＋报备/留痕/验后销毁 | ✓ | 全部验证用临时沙箱＋验后销毁（s5_crash_drill 沙箱 rmtree）；真实 works 零触碰 |
| 7 强杀测试只在测试服测试 work | ✓ | taskkill 对象为演练专用子进程（沙箱 work） |
| 8 产物落盘白名单 | ✓ | plans/＋thinkchain/ 两目录之外零写入（ops 留痕走 R6 既有通道） |

## 六、遗留与边界说明

1. **「日志核」口径**：判据 1/3/14 的「日志可查」在 MOCK 沙箱下以 gateway 调用记录＋caplog 断言承载；真实模型日志核留部署窗口（与迭代一残余条件同口径，不阻塞本验收）。
2. **junitxml 权威数字**：`_pytest_full_iter2_final.xml`（tests=299 failures=20 errors=0 skipped=1）与 `_pytest_baseline_iter1.xml`（tests=213 failures=20）已留仓根对账。
3. **净增 86 用例**＝本迭代新增件 53 用例＋基线后既有测试文件增量 33 用例（基线 12:11 时点后并入收集范围的既有件，含迭代一 S8/S9 批）——两笔均全绿或与基线同态。
4. 本报告与施工裁定一并呈报总工复核；主仓变更保持 uncommitted 留复核（分期分批提交建议：先迭代一面、后迭代二面）。
