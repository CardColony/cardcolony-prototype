# 权限管理 · 整体汇报（2026-10-09）

> 覆盖范围：原方案 → 方案链 → 任务书链 → 执行批一（T1/T2 停报候裁）→ 交办单定夺 → 执行批二（T3-T8 交付）
> 链文档之家：本目录 `THINK-ECO-permgmt-*` 系列（方案终稿 SHA256 C5C8AE96…、任务书终稿 p4、对账底册 PM-01~30、交办单、各执行回报）
> 状态：**代码全部落盘（测试仓），V1-V8 验收全过，回归零新增红；部署窗口与 5701 重启待许可，未提交 git**

---

## 一、一句话结论

散落在工具层各处的权限判断（读 4 处＋写 3 处＋建卡通道 1 处内联、解线/影子两漏检口、全量工具面、全盘文件访问），已收拢为「**一张按卡片分类发的权限表（POLICY）＋一个统一检查关口（check）**」：权限集中、统一入口、工具可见性从分类派生、未注册分类默认关门、三个漏检口补齐、文件层按分类锁路径——方案要实现的根本效果（PM-01）逐条落地，验收 V1-V8 全部以实跑证据通过，全量回归对比基线**零新增失败**。

## 二、要治的病（p1 实读实证，file:line）

| 病灶 | 收口前现场 | 对回 |
|---|---|---|
| ① 权限散落、无统一关口 | `can_read`（读＝存在即放行，2026-10-07 全盘放开）与 `can_edit`（写＝子树内）被 7＋1 处内联 if 调用（card_tools L143/164/193/234/291/326/373＋card_proposal L225），改一处漏一片 | P002 |
| ② 工具可见性与执行两套表达 | `build_tool_schemas()` 无入参、全量导出 18 件工具，任何卡都看全部工具 | P003 |
| ③ 口子无锁 | `unlink_cards` 零权限检查；`update_shadow` 无分类校验（仅靠签名无 card_id 兜底）；proposal 有子树校验但未走统一关口 | P017 |
| ④ 文件层全盘无锁 | `ALLOWED_ROOTS=(BASE_DIR,)`＝整个 D:\AI_station 可读写（10-04 用户裁定全盘），无按分类路径锁 | P004 |

## 三、走过的路（全链时间线）

| 阶段 | 产物 | 要点 |
|---|---|---|
| 原方案 | trading-workspace/docs/权限管理_落地方案.md（card_040） | 提出 Perm/POLICY/check 骨架；落点全错（src/agent/ 不存在、schema_builder 无此文件、工具名对不上），表值自注「等定稿后填入真实值，现在先用占位」 |
| 方案链全程 | 方案终稿 2026-10-08（C5C8AE96） | p0 对账→p1 实读→p15→p2→p3 八问复核放行；忠实功能意图、落点全部修正到真实代码；两处没定死的东西标【待用户裁】（PM-27 读写语义、PM-28 键拓扑），未替用户拍 |
| 任务书链 | 任务书终稿 p4＋对账底册 PM-01~30 | 转成可派工指令；红线六条（生产零触碰/R3 先决/不引入闸/表值不填/不越界/忠实方案）＋验收 V1-V8 |
| 执行批一（10-09 00:15） | 执行回报-T1T2-停报候裁 | 产仓树认定为唯一施工落点（测试仓是旧版）；T1 policy.py 落盘（表值全占位）；T2/V7 R3 等价验证 **27,605 对 0 破例**；R1-R5 全查明；触及两裁定夺项即**停报** |
| 交办单（10-09） | THINK-ECO-permgmt-交办-T3-T8-20261009.md | 用户 10:52「如果不是方向问题，你能定吧？」授权后，定夺人将两待裁项定性为技术实现层并定夺：**定夺 1** 表值＝原方案 §2.2 L48-78 逐字＋五键各加 SHADOW_WRITE；**定夺 2** 查表键＝`project_class or card_type`；施工落点改测试仓（备份＋同步产仓 src） |
| 执行批二（10-09） | 执行回报-T3-T8-20261009.md（测试仓 docs） | T3-T8 全部交付；V1-V8 实跑全过；回归 20 failed/106 passed＝基线 24 − 4 修复，零新增红 |

## 四、最终形态（现在盘上是什么）

**权限核心 `src/permissions/policy.py`（新建）**
- `Perm` Flag 三组十三位＋NONE：卡片操作位 READ/EDIT/DELETE/CREATE_CHILD/LINK/UNLINK/TREE_SCOPE＋工具组位 SEARCH/TOOLS_FILE/TOOLS_WEB/TOOLS_CARD＋通道位 CARD_PROPOSAL/SHADOW_WRITE。
- `POLICY` 五键（定夺 1）：rd_project / research_project＝READ+EDIT+CREATE_CHILD+LINK+UNLINK+TREE_SCOPE+SEARCH+TOOLS_FILE+TOOLS_WEB+TOOLS_CARD+CARD_PROPOSAL+**SHADOW_WRITE**；sub_project / topic＝同前**无 CARD_PROPOSAL、加 DELETE**；tool＝READ+EDIT+DELETE+SEARCH+TOOLS_CARD+**SHADOW_WRITE**（无 CREATE_CHILD/TREE_SCOPE/TOOLS_FILE/TOOLS_WEB）。偏离位 SHADOW_WRITE 逐键注明理由（原方案五行均无该位而任务书 T5 强制影子关口，相加＝影子机制整体停摆；判原方案自相矛盾，按**保持现状**给位）。
- `_type_key`（定夺 2）：查表键先取 project_class（仅根卡）后取 card_type——实盘 4 张根卡 card_type="project" 不在五键内，平铺直取＝整树连读拒死；registry L87 明文「根卡身份由 project_class 承载，project 只是类型名」。
- `check`（唯一关口，拒绝抛 PermissionError）/ `check_with_scope`（树类动作 EDIT/DELETE/LINK/UNLINK/CREATE_CHILD 且目标分类含 TREE_SCOPE 时追加子树限定）/ `_is_in_subtree`（与已删子树判据逐字同口径，guard 边界实测 ≤1000 真/1001 假）。
- 未注册分类（影子条目 shadow、遗留值、随手值）走 DEFAULT_POLICY=NONE＝默认关门。

**工具层 `card_tools.py`**：两判据函数删除；10 处调用全走关口（7 处原内联＋unlink＋update_shadow＋proposal）；拒绝转既有 denied 信封、缺卡仍走 not_found（错误链不混）；「剧本根卡不可删除」护栏前移一行保文案逐字；`build_tool_schemas(card_id, cards)` 按分类过滤（未授 TOOLS_FILE 不下发文件四件、未授 TOOLS_WEB 不下发联网两件），上游唯一调用点 message_runner L203 补传当前卡。

**文件层 `file_tools.py`**：`FILE_SCOPE` 表＋`check_file_access(path, card_type)` 三层机械判定（分类 TOOLS_FILE 位 → ALLOWED_ROOTS 边界 → 凭证排除＋前缀）；原独立凭证函数收敛为内部排除层（grep 清零）；四件文件工具全接入。

**打点 `prompt_log.py`**：`append_perm_log` → `logs/prompt_perm_audit.jsonl`，四个判定面（check/check_with_scope/build_tool_schemas/check_file_access）每次判定一条机械字段记录、两态同记、**无轮转无上限无采样**（打点是留痕非闸）。

## 五、验收 V1-V8（全部实跑证据，无 fixture 全绿替代）

| V | 判据 | 结果 |
|---|---|---|
| V1 | `grep -rn "can_read\|can_edit" src/` | **命中 0**（含 7 处注释文字一并清扫） |
| V2 | `grep -rn "_is_credential" src/` | **命中 0** |
| V3 | `def check(` 唯一＋工具函数调用 ≥7 | policy L140 唯一；调用 10 处（card_tools 9＋proposal 1） |
| V4 | tool 分类卡不含 write_file/read_file | 构造 meta 实测：恰 12 件、文件四件与联网两件全滤；正控（sub_project/topic/根卡按 project_class 取键）均 18 件 |
| V5 | 无文件权限分类 → False / PermissionError | tool→False、「线索」→False、.env 凭证→False；未注册 check 抛 |
| V6 | 影子写拒绝 | 定夺 1 五键全授 SHADOW_WRITE → 已注册分类不拒，拒绝证据按交办单口径改未注册分类真实 execute_tool 触发；注册分类正控照写 |
| V7 | R3 等价对照表落盘 | **27,605 对 0 破例**（手工边界树 121＋深链 guard 7＋定种子随机林 27,477）；交付后另加「定格副本」回归锚持续有效 |
| V8 | prompt_log 留判定记录 | 实跑日志行存证（check 两态/scope_applied/派生清单/路径锁判定四种 fn 齐全） |

**回归归因（三时点）**：同步前测试仓原生 6 红/110 绿 → 按令仅同步产仓 src 后 24 红/92 绿（同步引入 18 红＝测试空间旧断言对不上产仓新一代代码，两仓测试谱系本就不同源，备份可整树回滚）→ T3-T8 交付后 **20 红/106 绿＝基线 24 − 4 修复，diff 零新增条目**。残余 20 红全部为基线内旧红（flowlane/offline_e2e/card_tree/file_boundaries/snapshot/t11/t12/t14 的陈旧断言冲突），清单在案（`_permgmt_after_t8_failed.txt`），未越界处理。新增 `typed_registry` 夹具＋registry 沙箱副本解决了「无注册表沙箱造不出注册分类卡」的验收障碍，autouse 沙箱全局行为零改动。

## 六、语义变化全景（用户/模型可感知）

**不变（收口只换通道）**：读＝注册分类存在即放行（10-07 口令延续）；写＝自己子树内（10-08 口令延续，经 TREE_SCOPE 子树限定）；影子照写（定夺 1 偏离位的本意）；根卡拒删文案逐字；结构护栏（根卡拒删/有子卡拒删/影子签名级独占）原样。

**收紧**：①连线/解线由「任意两卡」收紧为「只能连/解自己子树内的卡」（交办单「待用户知悉」明示，tool 目标完全不可连——已有测试实证）；②未注册分类默认关门（影子条目作 read_card/read_memory 目标由可读转拒，read_shadow 实路径剥前缀不受影响）；③tool 分类卡模型侧看不到文件/联网工具（PM-18 本意）。

**放宽**：④文本建卡通道（card_proposal 兜底通道）parent 校验由「子树内」放宽为「parent 分类授 CREATE_CHILD」——任务书 T3/交办单明令统一 check 的直接后果；拒绝文案沿用原文，「子树」表述与新判据的文面张力已呈报待定文案。

**恢复**：⑤凭证排除对模型文件工具恢复生效（10-04 曾撤；T6 依令收敛回归，可推翻）。

**文案**：⑥模型文件工具越界提示并入越权信封（file_tools 内部文案未动）。

## 七、纪律执行（红线逐条对账）

- **生产零触碰**：5700/5701 两实例全程在跑但零触碰、零重启；生产 works 用户数据零读写；产仓树执行批二零新增改动（git status 实证）。
- **R3 先决**：先等价验证（27,605 对 0 破例）后删函数，顺序未破。
- **不引入任何闸**：无 max_tokens/timeout/轮次封顶/墙钟判据；打点无轮转无上限。
- **停报纪律**：执行批一触及 PM-27/28 即停报候裁，未自行择值；执行批二所有解释性决策（悬空线豁免、护栏前移、FILE_SCOPE 哨兵值、空路径放行等六项）均属行为保持型且逐项记录在回报。
- **不越界/忠实**：card_store.py、card_type_registry.json、models.yaml 零改动；SEARCH/TOOLS_CARD/CARD_PROPOSAL 三位照填但无接线点（任务书未列，不自行发挥）；无任何 git 提交。

## 八、待办与待许可

| 项 | 内容 | 等谁 |
|---|---|---|
| 部署窗口 | 测试仓改动面向产仓同步（src 七文件＋src/permissions＋tests 四件＋fixtures），**只部署不重启** | 用户许可 |
| 5701 重启 | 如需测试实例活跑新码，restart_safe_5701 口径 | 用户许可 |
| FILE_SCOPE 真实前缀 | 机制已就绪，值现状＝全放行哨兵（保持 10-04 全盘现状档）；真实前缀属部署配置 | 用户给值 |
| proposal 拒绝文案 | 「子树」表述与新判据的文面张力 | 用户定文案 |
| 产仓 tests 是否同步 | 两仓测试谱系不同源，残余 20 红如何处置（同步产仓 tests/ 或维持现状） | 用户决断 |

## 九、证据与文档索引

| 件 | 位置 |
|---|---|
| 方案终稿（C5C8AE96）/任务书终稿/对账底册 PM-01~30/交办单 | 产仓 docs/exploration/ |
| T1/T2 执行回报＋R3 等价验证报告（V7）＋对照表全量输出 | 产仓 docs/exploration/ ＋ 产仓根 _t_perm_r3_table.* |
| T3-T8 执行回报（本汇报的施工细节版） | 测试仓 docs/exploration/THINK-ECO-permgmt-执行回报-T3-T8-20261009.md |
| 权限核心/工具层/文件层/打点 | 测试仓 src/permissions/policy.py、src/api/{card_tools,card_proposal,message_runner,file_tools,prompt_log}.py、src/storage/shadow_store.py |
| 验收测试 | 测试仓 tests/test_perm_gateway_acceptance.py（10 条）、tests/test_cardperm_readopen.py（4 条改写）、tests/test_card_tree.py（权限用例改写） |
| 回归基线存证 | 测试仓根 _permgmt_baseline_postsync_24.txt / _permgmt_after_t8_failed.txt / _permgmt_v8_evidence.txt |
| 回滚保险 | 测试仓 _permgmt_src_backup_20261009/（同步前整树） |
