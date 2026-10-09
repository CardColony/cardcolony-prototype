# THINK-ECO-permgmt · 执行回报 T1/T2 · 停报候裁（2026-10-09 00:15）

> 任务书：《权限管理_落地方案》可实现化 · 任务书终稿（p4，2026-10-08）
> 对账底册：THINK-ECO-permgmt-对账-任务书方案条目清单-20261008.json（PM-01~30）
> 本批结论先行：**T1 落盘（表值占位）、T2/V7 过（R3 严格等价 27,605 对 0 破例）、R1-R5 全查明；T3-T8 按 PM-27/PM-28 停报候裁，未自行择值。**

## 一、现场核验（先于动刀）

1. **施工落点＝产仓树 `D:\AI_station\推演agent`**。依据：任务书全部现场锚（card_tools L855/877/833、TOOL_REGISTRY、message_runner、config/card_type_registry.json）只在产仓树成立；测试仓 `推演agent_test` 的 card_tools.py 是旧版（build_tool_schemas 在 L46、无 TOOL_REGISTRY/registry/message_runner.py），不承载本任务。5700（产，pid 22948）/5701（测，pid 30408）两实例在跑，**全程零触碰**（验收走 pytest 沙箱，conftest 把 works/logs/ALLOWED_ROOTS 全部重定向 tmp_path）。
2. **任务书锚点逐条命中**：L143/164/326/373（读）、L193/234/291（写）、L307-313（update_shadow 无检查）、L397-432（unlink 零权限检查）、L833（build_tool_schemas 无入参）、L855（can_read 全盘放开）、L877（can_edit 子树）、card_proposal.py L225——行号零漂移。
3. 方案终稿 SHA256 实测 `c5c8ae96…` ✓；对账底册 30 条与任务书一致。

## 二、R1-R5 待侦察项逐项回报（任务书第六节要求）

| # | 结论 | 证据 |
|---|---|---|
| R1 | `build_tool_schemas()` 装配层调用点唯一：`src/api/message_runner.py` **L201** `state.tools = card_tools.build_tool_schemas()`。同函数 L177 event_stream.emit 与 L346 `execute_tool(state.work_id, state.card_id, …)` 证实 `state.card_id` 即当前对话卡——T4 补传即在此处加 `card_id=state.card_id`。全 src 无第二调用点 | grep src 全量 |
| R2 | registry 双层拓扑：`project_classes`＝{rd_project, research_project}（仅根卡）＋`l1_vocabularies`＝rd:{sub_project, tool}、research:{topic}、root:{project}；legacy（剧本/线索/世界观/行为体/节点/假设）存量 0；shadow 走独立通道不入体系。**实卡分布（6 works 只读盘点）：project×4（卡面带 project_class：rd×2/research×2）、sub_project×33、topic×35、tool×0。方案五 key 中 rd_project/research_project 是 project_class 层值，不是任何实卡的 card_type** → 平铺直取 card.card_type 时两行永不命中、4 张根卡落默认关门 | config/card_type_registry.json 实读＋works/*/cards/*.md 只读 grep |
| R3 | **严格等价成立**：`_is_in_subtree`（新）vs `can_edit`（L877）27,605 对判定（手工边界树 121＋深链 guard 边界 7＋定种子随机林 27,477），破例 0。报告：`THINK-ECO-permgmt-R3-等价验证-20261009_0010.md`（V7 ✓）。附带钉死：guard 真实边界＝depth≤1000 真/1001 假（return True 在 guard 自增后、循环复检前）；自指即真不查存在性；悬空 parent 链字面命中 current 判真——全部既有语义逐字继承 | tests/test_perm_r3_equivalence.py（6 passed）＋_t_perm_r3_table.py/_t_perm_r3_table_out.txt |
| R4 | 真实 18 工具清单：read_card / read_memory / create_card / edit_card / delete_card / update_shadow / read_shadow / list_manuscript / read_manuscript / search_manuscript / web_search / fetch_web / link_cards / unlink_cards（14 内置）＋ list_files / read_file / search_files / write_file（4 文件，模块加载期 `_register_file_tools()` 并入 TOOL_REGISTRY） | TOOL_REGISTRY 实读（card_tools.py L534-816） |
| R5 | POLICY 五 key 全部占位 None，未填任何值；`check`/`check_with_scope` 在占位态一律抛 PermissionError（族 D 单测实证）；全部接线（7+1 处改调、schema 过滤、文件锁）未动，现行行为零变化 | src/permissions/policy.py＋tests 族 D |

**清扫面补报（V1 相关）**：`grep "can_read\|can_edit" src/` 除 7 处内联＋proposal 1 处外，还有 **7 处注释/文档字符串**含此二字（card_tools.py L17/180-182/230/319/570/848、card_proposal.py L192、shadow_store.py L8 文档）——V1 是 grep 判据，裁定后 T3 一并改写，避免 V1 假红。

## 三、本批交付物（改动面）

| 件 | 性质 |
|---|---|
| `src/permissions/__init__.py`、`src/permissions/policy.py` | **T1 新建**：Perm 三组十三位＋NONE、DEFAULT_POLICY=Perm.NONE、POLICY 五 key 全占位（R-D）、check / check_with_scope / _is_in_subtree（遍历口径与 can_edit L877 逐字同源）、树类动作集＝{EDIT,DELETE,LINK,UNLINK,CREATE_CHILD}（原方案 §3.2 逐字）。占位＝未配置走默认关门。**无任何调用方，运行零影响** |
| `tests/test_perm_r3_equivalence.py` | T2 等价验证 6 条（族 A/B/C 对照＋族 D 占位态拒绝 smoke） |
| `_t_perm_r3_table.py` / `_t_perm_r3_table_out.txt` | 对照表机械枚举脚本＋全量输出（137 行） |
| `docs/exploration/THINK-ECO-permgmt-R3-等价验证-20261009_0010.md` | V7 报告 |

**回归**：全量 pytest `131 passed, 1 skipped`（skip＝test_cardperm_live 真模型实弹需显式 CARDPERM_LIVE=1，非红）。**红线执行**：card_store.py 零改动；无任何 git 提交；5700/5701 零触碰、零重启；无新增时间/长度/轮次闸（R-C）；models.yaml 等运行态文件未触碰（R-F）。test_cardperm_readopen.py 直接调用 `card_tools.can_read/can_edit`（tests/ 不在 V1 grep 面内，但删除函数会断套件）——裁定后 T3 联动改写，届时其断言语义随 PM-27 裁定同步。

## 四、【停报候裁】两处【待用户裁】（触及即停，未择值）

**PM-27 · 卡片操作位/通道位按哪套语义填**（工具组位四件不受此裁影响：V4 硬性要求 tool 分类卡看不到 write_file/read_file，四组位一律按方案五行表随行填）：

| 档 | 内容 | 主要后果 |
|---|---|---|
| A 原方案五行表值 | rd/research：READ+EDIT+CREATE_CHILD+LINK+UNLINK+TREE_SCOPE+CARD_PROPOSAL（禁删）；sub_project：上＋DELETE；tool：READ+EDIT+DELETE；topic：同 sub_project | 读不再全盘；五行均无 SHADOW_WRITE ⇒ update_shadow 全分类关门（影子推送通道停用）；树类动作经 _is_in_subtree 子树限定 |
| B 保全现行语义 | 全分类：READ＋EDIT/DELETE/CREATE_CHILD＋TREE_SCOPE＋LINK/UNLINK/SHADOW_WRITE | 读/写/影子行为零变化；但 TREE_SCOPE 是分类级位 ⇒ link/unlink 由「全项目存在性」收紧为子树内（方案枚举表达力的既定约束）；V5/V6 拒绝证据只能由未注册分类触发 |
| C 折中：读放开＋写按方案表 | READ 全分类；其余按五行表 | 读全盘延续；SHADOW_WRITE 无行 ⇒ 影子写停用（同 A） |

**PM-28 · POLICY 查表 key 与双层分类的对应**：

| 档 | 内容 | 主要后果 |
|---|---|---|
| A 平铺照抄（字面忠实） | key＝card.card_type 直取，五 key 不动 | rd/research 两行成死行；4 张 project 根卡默认关门连读拒死（方案终稿自点的后果） |
| B 根卡按 project_class 取行 | key＝`project_class or card_type`（根卡卡面已带 project_class，registry 闸 1 明文「仅根卡可写」） | 五行全活、无增补、无死行；影子条目（shadow）不在五行→默认关门（实路径读侧先剥 shadow_ 前缀，不受影响） |
| C 增补 project 行 | 五 key 照抄＋第 6 行 "project"（仅根卡），行值需一并给出 | 即用户明令增补（原方案「不改表项不增补」被此令解除）；rd/research 两行仍为死行 |

两档裁定正交可组合。**裁定一到即续**：填表值 → T3 删 can_read/can_edit＋7+1 处改接（含 7 处注释文字）→ T4 build_tool_schemas(card_id)＋message_runner L201 补传 → T5 补三漏检口 → T6 FILE_SCOPE＋check_file_access 接入四文件工具＋_is_credential 收敛清零 → T7 三处打点落 prompt_log → T8 V1-V8 全验收＋全量回归＋停报待部署窗口（生产只部署不重启，重启另请令）。

## 五、V 状态

| V | 状态 |
|---|---|
| V7 | ✅ 本批产出（R3 等价对照表落盘，27,605 对 0 破例） |
| V1-V6、V8 | 候裁后 T3-T8 产出（全部依赖 POLICY 表值落定后的接线） |
