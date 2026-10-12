# 权限管理 · 生效任务书

目标：让新权限机制（policy.py 权限表 + check 关口）在产仓 D:\AI_station\推演agent 的运行进程中真实生效，并用实测证明：研发根卡与子项目卡的读、改、删、建子卡、连线、解连、提案、影子写入，都按权限表放行或拦截。

范围：只改产仓的接入与验证。不改 card_store.py、card_type_registry.json、models.yaml。不做 git 提交。不碰生产 works 用户数据。

## 一、前置事实（执行前必须复核，不符则停报）

1. 产仓存在 src/permissions/policy.py、src/permissions/__init__.py。
2. 产仓 src/agent/card_tools.py 中 check_with_scope 调用不少于 9 处，且不存在 can_read / can_edit 实际调用（注释除外）。
3. 产仓 works/work_002/cards/card_001.md：card_type="project"，project_class="rd_project"。
4. 产仓 config/card_type_registry.json：必须确认 project_class 键 rd_project 已登记。card_type="project" 是否登记，需记录结论。
5. 产仓 src/api/message_runner.py 调用 build_tool_schemas 时必须传 card_id 与 cards。

任一项不符：停止，写回执说明差异，不自行修补。

## 二、步骤

### S0 备份
- 复制产仓 src/、tests/、config/ 到 D:\AI_station\推演agent\_permgmt_live_backup_<日期>\。
- 记录 5700 进程启动时间与当前运行的代码版本标识。

### S1 代码一致性核对（只读）
- 对比产仓与测试仓（D:\AI_station\推演agent_test）下以下文件，记录差异清单：
  src/permissions/policy.py、src/agent/card_tools.py、src/agent/file_tools.py、src/api/message_runner.py、src/storage/shadow_store.py、src/api/prompt_log.py、src/api/card_proposal.py。
- 产仓版本为准。若测试仓更新，停报，不覆盖。

### S2 权限测试（产仓内执行）
```
cd D:\AI_station\推演agent
python -m pytest tests/test_perm_gateway_acceptance.py tests/test_cardperm_readopen.py tests/test_card_tree.py -q
```
- 记录通过数、失败数、失败用例名。
- 失败用例逐条判定：权限相关失败必须修；与权限无关的旧红（flowlane、offline_e2e、card_tree 旧断言、file_boundaries、snapshot、t11/t12/t14）单独列出，不纳入本任务修复。

### S3 注册表与表键核对（只读）
- 读 config/card_type_registry.json，确认 rd_project、sub_project、tool 等键的登记状态。
- 根卡 card_type="project" 若未登记，写入会被闸 1 拒绝。此项结论写入回执，由用户裁定处理方式，本任务不擅自改注册表。

### S4 运行态正负用例（需 S2 通过后执行）
在产仓测试沙箱（不是生产 works）中，以研发根卡与一张 sub_project 子卡为对象，逐项验证：

| 动作 | 根卡（rd_project） | 子项目卡（sub_project） | 期望 |
|---|---|---|---|
| 读 | 放行 | 放行 | 按表 READ |
| 改 | 放行（子树内） | 放行（子树内） | 按表 EDIT + TREE_SCOPE |
| 删根卡 | 拒绝 | — | 根卡禁删 |
| 删子卡 | — | 放行（子树内） | sub_project 含 DELETE |
| 建子卡 | 放行 | 放行 | CREATE_CHILD |
| 连/解连子树外卡 | 拒绝 | 拒绝 | TREE_SCOPE 限定 |
| 连/解连子树内卡 | 放行 | 放行 | LINK/UNLINK |
| 影子写入 | 放行 | 放行 | SHADOW_WRITE（见待裁） |
| 未注册分类卡 | 全拒 | 全拒 | DEFAULT_POLICY=NONE |

- 每项记录：请求、返回、audit 日志对应行。

### S5 fail-open 核查（只读，修复需裁定）
- 核查 card_tools.py 的 _filter_by_category：card_id 为空或 cards 缺省时是否直接返回全量工具。
- 若是，写入回执并标注"待裁"。修复方案（无卡上下文按最严档）由用户裁定后执行。

### S6 部署生效（需用户许可）
- 用户许可后，重启 5700（使用既有 restart_safe 口径，不得自行手工 kill）。
- 重启前记录当前进程 PID 与启动时间；重启后记录新 PID 与启动时间。
- 确认 logs/prompt_perm_audit.jsonl 在重启后出现 check / check_with_scope / build_tool_schemas 的新记录。无记录则视为未生效，停报。

### S7 验收
- 产仓权限测试：S2 所列文件全部权限相关用例通过。
- S4 正负用例全部与表一致。
- 重启后 audit 日志出现四类判定面记录。
- 回归：全量 pytest 失败集合不得超过基线（基线清单需在 S2 前一次性记录）。

## 三、待用户裁定（未裁定前，对应步骤不执行）

1. FILE_SCOPE 真实前缀（现为全放行占位，文件层未锁）。
2. SHADOW_WRITE 偏离位保留与否（五类均授予，与原方案不一致）。
3. card_type="project" 的注册表处理方式（S3 结论后定）。
4. S5 fail-open 是否修复为最严档。
5. proposal 拒绝文案中"子树"表述的定稿。
6. S6 重启许可。
7. 产仓 tests 与测试仓谱系不同源，残余旧红的处置。

## 四、回滚

- 任一步失败：用 S0 备份还原 src/、tests/、config/。
- 重启后异常：用既有重启前代码备份（重启前代码备份_20261011/）回退，再按 restart_safe 口径重启。

## 五、回执要求

每步写回执，格式：步骤号｜执行时间｜命令或操作｜关键输出（原文摘录）｜结论（通过/失败/待裁）。未实际执行的步骤不得标为通过。

