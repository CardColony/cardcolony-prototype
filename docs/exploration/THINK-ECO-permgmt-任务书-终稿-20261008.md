# 《权限管理_落地方案》可实现化 · 任务书

> taskbook 链 p4 终稿 · 2026-10-08
> 对账底册：THINK-ECO-permgmt-对账-任务书方案条目清单-20261008.json（PM-01~PM-30）
> 上游：THINK-ECO-permgmt-方案终稿-20261008.md（SHA256 C5C8AE96）

## 一、背景与裁定

**任务**：把《权限管理_落地方案》可实现方案（`docs/exploration/THINK-ECO-permgmt-方案终稿-20261008.md`，SHA256 C5C8AE96）转成可直接派工、可直接执行的任务书。方案本体已经 proposal 链全程走完（p0 对账 24 条 → p1 实读 → p15 三节 → p2 骨架 → p3 八问复核＋红灯增量重判 → 复核放行），本任务书只做执行化转译，不改变方案意图。后面执行这份任务书的环节只能做本任务书明说的事——写含糊一句，后面就会自由发挥一句。

**用户当次专项要求（逐字贴入，一条不落）**：
1. 「你来用方案链，做出可实现方案，而且不允许你做任意的私自判断，必须忠实方案想要实现的效果」（2026-10-08 22:06）
2. 「任务书路径发给我」（2026-10-08 23:08）

**方案要实现的根本效果（PM-01）**：把散落在工具层各处的权限判断，收拢为「一张按卡片分类发的权限表（POLICY）＋一个统一检查关口（check）」；工具可见性从 POLICY 派生；默认关门（未注册分类全拒）；补三个漏检口；文件层按分类锁路径。（PM-01/02/03/04/05）

**裁定继承（不自作主张）**：
- 忠实的是方案的功能意图与目标效果，不是错误路径——原方案落点全错（PM-06：`src/agent/` 目录不存在、`schema_builder.py` 全项目无此文件、工具名 `search_cards/list_dir/update_card` 注册表里不存在），落点全部按 p1 实证修正到真实代码。
- 不私自判断：原方案未定死两处——PM-27（现行读/写语义收口后是否保留）、PM-28（POLICY 表 key 与 `project_class/card_type` 双层拓扑对应）标【待用户裁】，执行中触及即停报、不自行择一。

## 二、现场锚（file:line，p1 调研纪要实读）

| 锚点 | 位置 | 现状 |
|---|---|---|
| `can_read` | `src/api/card_tools.py` L855 | 读侧全盘放开、存在即放行 |
| `can_edit` | `src/api/card_tools.py` L877 | 写侧 target 在 current 子树内→True；血统上级不可写（L882-902 防环遍历） |
| 内联 can_read×4 | L143(`_h_read_card`)/L164(`_h_read_memory`)/L326(`_h_read_shadow`)/L373(`_h_link_cards`) | 7+1 处内联 if not can_xxx，无统一 check 入口（PM-02） |
| 内联 can_edit×3 | L193(`_h_create_card`)/L234(`_h_edit_card`)/L291(`_h_delete_card`) | 同上 |
| 建卡通道 | `src/api/card_proposal.py` L225 | 已有 `if not card_tools.can_edit(...)` parent 校验（PM-04；此处置收口非补缺） |
| `build_tool_schemas` | `src/api/card_tools.py` L833 | 签名无入参、全量导出 18 工具、无按卡过滤（PM-03） |
| `unlink_cards` | `src/api/card_tools.py` L397-432 | 仅目标非空/关系类型合法/关系存在 三项校验，零权限检查（PM-04） |
| `update_shadow` | `src/api/card_tools.py` L307-313 | 无分类校验，接口天然只能写自己（PM-04） |
| 文件层 | `src/api/file_tools.py` L34 `ALLOWED_ROOTS`/L68 `_is_credential`/L81 `_resolve`/L285 `write_file` | 全盘可读写、无按卡分类路径锁（PM-05） |

**待侦察项（查明回报，纪要不含标 R#，禁止编造）**：
- **R1** `src/api/message_runner.py` 装配层 `build_tool_schemas()` 调用点（加 card_id 上游改动位置，PM-15）
- **R2** `config/card_type_registry.json` 全量 key 与 POLICY 表 key 逐项比对（PM-28 前置）
- **R3** 新 `_is_in_subtree` 与现行 `can_edit`（L877）是否严格等价（PM-22 施工先决）
- **R4** 工具清单按真实 18 个工具列出，不留「17 个分支」死数（PM-06）
- **R5** T1/T2 未裁前 POLICY 表值一律不填（PM-27/28）

## 三、任务分解

- **T1 · 新建 `src/permissions/policy.py`**（PM-07/21）：
  - `Perm` Flag 枚举：卡片操作位 `READ/EDIT/DELETE/CREATE_CHILD/LINK/UNLINK/TREE_SCOPE`＋工具组位 `SEARCH/TOOLS_FILE/TOOLS_WEB/TOOLS_CARD`＋通道位 `CARD_PROPOSAL/SHADOW_WRITE`
  - `DEFAULT_POLICY = Perm.NONE`（默认关门）
  - `POLICY` 常量表：按 `card_type` 映射权限组，忠实原方案五 key（rd_project/research_project/sub_project/tool/topic）不改表项不增补；**搭配值在 T2 裁定前一律留占位不填**（R5/PM-28）
  - `check(card_id, action, card_store)`：取卡→`POLICY.get(card.card_type, DEFAULT_POLICY)`→判 action 在位；拒绝抛 `PermissionError`
  - `check_with_scope(actor, target, action, card_store)`：先过 check；树类动作且 target 分类含 `TREE_SCOPE` 时经 `_is_in_subtree` 限定作用域
  - `_is_in_subtree(actor, target, cards)`：沿 target parent 链判定 actor 作用域

- **T2 · R3 施工先决验证**（PM-22）：
  - 写测试对比 `_is_in_subtree`（新）与现行 `can_edit`（L877）对同一组卡树的全部判定结果，**逐例相等才算严格等价**（R3）
  - 验证报告（对照表）落盘 `docs/exploration/THINK-ECO-permgmt-R3-等价验证-<ts>.md`，作 T3 可否删函数的前置证据

- **T3 · card_tools 权限收口**（PM-08/22）：
  - 删 `can_read`（L855）/`can_edit`（L877）——**仅在 T2 验证通过后删**；未通过则保留两者、由 `check_with_scope` 内部调用兜底
  - 7 处内联改调 `check_with_scope(current_id, target_id, Perm.<动作>, cards)`：L143/L164/L326/L373（读）、L193/L234/L291（写）
  - `card_proposal.py` L225 的 `can_edit` 改调 `check(parent_id, Perm.CREATE_CHILD, card_store)`

- **T4 · `build_tool_schemas(card_id)` 派生可见工具**（PM-15/16）：
  - 签名加 `card_id` 入参（现 L833 无入参）
  - 按当前卡分类 POLICY 过滤 TOOL_REGISTRY：未授 `TOOLS_FILE` 不下发 write_file/read_file/list_files/search_files；未授 `TOOLS_WEB` 不下发 web_search/fetch_web
  - 上游 `message_runner.py` 装配层补传当前卡 id（R1 查明后落位）

- **T5 · 补三个漏检口**（PM-23）：
  - `unlink_cards`（L397-432）：加 `check_with_scope(current_id, target_id, Perm.UNLINK, cards)`
  - `update_shadow`（L307-313）：加 `check(current_card_id, Perm.SHADOW_WRITE, card_store)`
  - `card_proposal.py` L225：收口到统一 check（同 T3）

- **T6 · file_tools 文件路径锁**（PM-12/24）：
  - 新增 `FILE_SCOPE` 常量表：按 card_type 映射可访问路径前缀
  - 新增 `check_file_access(path, card_type)`：先落 `ALLOWED_ROOTS`（L34）全盘边界、再叠加当前卡分类 FILE_SCOPE 前缀判定，返回 bool
  - `write_file`（L285）/`read_file`/`list_files`/`search_files` 各调 `check_file_access`
  - `_is_credential`（L68）由独立函数收敛为 `check_file_access` 内部凭证排除（使 grep 清零）

- **T7 · 全日志打点**（PM-14/26）：
  - `check()`/`check_with_scope()` 每次判定：记录 `card_id/actor/action/target`、查表结果（分类、POLICY 命中位）、`granted/denied`，落 `prompt_log`（在 check 唯一返回路径埋点、不分叉）
  - `build_tool_schemas(card_id)` 每次派生：记录 `card_id`、分类、下发工具名、被过滤的文件/网页工具，落 `prompt_log`
  - `check_file_access(path, card_type)` 每次判定：记录 `path/card_type/命中前缀/granted`，落 `prompt_log`
  - **打点全为机械字段、不做二次判定、不设任何时间/长度/轮次闸**（日志打点是留痕非闸）

- **T8 · 施工顺序与验收收口**（PM-25）：
  - 按序：T1 建 policy → T2 验证 R3 → T3 收口 → T4 派生 → T5 补口 → T6 路径锁 → T7 打点 → T8 回归（读/写/建/删/连线/解线/影子全路径不回归）
  - 测试实例先全量跑通验收，再进入部署窗口；每步完成即验证、失败即停报

## 四、红线

- **R-A 生产零触碰**：任何动作先在测试实例跑通；生产实例只做部署窗口，且**生产只部署不重启**，重启须另行获许可；生产 works 用户数据零触碰（PM-25/29）
- **R-B R3 先决**：`_is_in_subtree` 与 `can_edit`（L877）严格等价验证通过前，不得删 `can_read`/`can_edit`；未通过则保留、由 `check_with_scope` 兜底（PM-22）
- **R-C 不引入任何闸**：不得新增 max_tokens/timeout/轮次封顶/墙钟判据；日志打点是留痕非闸（PM-26）
- **R-D T1/T2 未裁前 POLICY 表值一律不填**（占位标记待裁定），不自行选值、不替用户拍（PM-27/28）
- **R-E 不越界**：不超出本任务书范围自加需求；不用「酌情/视情况/等」含糊措辞（纪律 4）
- **R-F 忠实方案**：只实现方案列明的一套（policy 新建＋card_tools 收口＋三漏检口＋file_tools 路径锁），`src/storage/card_store.py` 不改（PM-13/29）；`src/storage/models.yaml` 等运行态/临时件/快照不进任何 git 提交（PM-29）

## 五、验收（断言可机械判定，实跑产出）

- **V1** `grep -r "can_read\|can_edit" src/` → **零命中**（PM-15）
- **V2** `grep -r "_is_credential" src/` → **零命中**（PM-16）
- **V3** `def check(` 唯一定义，各工具函数调用 `check` ≥7（PM-17；R3 验证通过后成立）
- **V4** 直调 `build_tool_schemas(<tool 分类卡 id>)` → 返回不含 `write_file/read_file`（PM-18）
- **V5** 无文件权限分类的卡请求文件工具 → `check_file_access` 返回 False / `check()` 抛 `PermissionError`（单测触发，实跑记录为证）（PM-19）
- **V6** 影子卡写非 shadow 分类 → `check(Perm.SHADOW_WRITE)` 拒绝（单测触发，实跑记录为证）（PM-20）
- **V7** T2 的 R3 等价验证对照表落盘（作为「通过才删 can_read/can_edit」的机械证据）（PM-22）
- **V8** 三处打点（check/check_with_scope、build_tool_schemas、check_file_access）在 `prompt_log` 留有判定记录（PM-14/26）

## 六、回报要求

- 每个任务干完回报：改了哪个文件、哪几处（file:line）、跑通了哪几条验收号
- **R1-R5 待侦察项逐项查明回报**：R1 message_runner 装配层 build_tool_schemas 调用点、R2 card_type_registry 与 POLICY 逐 key 比对、R3 严格等价验证、R4 真实 18 工具清单、R5 T1/T2 未裁前不填表值的执行确认
- 若执行中触及 **T1**（现行 `can_read`「全盘放开」/`can_edit`「只建自己的子卡片」收口后语义是否保留）或 **T2**（POLICY 表 key 与 `project_class/card_type` 双层拓扑对应）——**立即停报，不下决定，等用户裁定**（PM-27/28）
- 最终交付：V1-V8 全部通过的报告＋每项实跑证据（命令输出/单测运行记录/日志打点行，非 fixture 全绿）