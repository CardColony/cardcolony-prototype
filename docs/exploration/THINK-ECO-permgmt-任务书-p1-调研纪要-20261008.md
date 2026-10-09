# 权限管理任务书 · p1 调研纪要（20261008）

对账对象：`THINK-ECO-permgmt-方案终稿-20261008.md`（方案链终稿，64 行）
对账基线：p0 落盘的《方案条目清单》30 条（PM-01~PM-30）
本纪要每条发现带真实 file:line（本次实读/grep 定位，行号以当前盘上为准）。

## 现状

《权限管理_落地方案》可实现化——把散落在工具层的权限判断收拢为「一张按卡片分类发的权限表（POLICY）+ 一个统一检查关口（check）」。真实代码里权限判定现状：读侧全盘放开（存在即放行）、写侧子树可写，判定函数散落在工具函数内联调用，无统一关口；工具可见性全量导出、无按卡过滤；两个动作口子无权限检查。

## 相关模块（每条带 file:line，方案锚点逐一实读对回）

**① 权限判定函数本体（card_tools.py）**
- `can_read` 定义 **L855**：读侧「全盘放开——target 存在即放行」（docstring L859-868 明确「用户令 2026-10-07 11:26」；L870-874 target/current 存在性校验）。【方案锚 L855 ✓ 实读对回】
- `can_edit` 定义 **L877**：写侧「target 在 current 子树内（含自身）→ True；血统链上级不可写、契约卡不可写」（L882-902，反向遍历 target parent 链可否到达 current，含 1000 步防环守卫）。

**② 内联权限调用点（7+1 处，全仓 grep call_1fcc267f + 分段实读确认）**
- 读侧 can_read×4：`_h_read_card` **L143**、`_h_read_memory` **L164**、`_h_read_shadow` **L326**、`_h_link_cards` **L373**（均 `if not can_read(...)` → err denied）
- 写侧 can_edit×3：`_h_create_card` **L193**、`_h_edit_card` **L234**、`_h_delete_card` **L291**（均 `if not can_edit(...)` → err denied）
- 建卡通道：`card_proposal.py` **L225** `if not card_tools.can_edit(current_card_id, parent, cards)`「越权：只能建自己的子卡片」【方案锚 L225 ✓ 实读对回】
- 无统一关口：全部 7+1 处为各自的 `if not can_xxx` 内联，无 policy.check() 集中入口。

**③ 工具可见性（card_tools.py）**
- `build_tool_schemas()` **L833**（docstring L836-838「改前是 14 段字面量 list + file_tools 追加；改为遍历注册表派生，顺序＝插入序」）：**签名无入参**，`return [_schema_of(def_) for def_ in TOOL_REGISTRY.values()]`——全量导出 18 个工具，无按卡分类过滤。【方案锚 L833 ✓ 实读对回；方案要求加 card_id 入参——现状确无参，该改动成立】

**④ 两个无权限检查的动作口（card_tools.py）**
- `_h_unlink_cards` **L397-432**：L400 注释明确「不检查目标卡存在性」；仅 target 非空（L405）/关系类型合法（L407）/关系存在（L419-423）三项校验，走 update_card(relations) 通道，**零 can_read/can_edit 权限检查**。与 link_cards（L373 有 can_read）不对称。【方案锚 L397-430 ✓ 实读对回】
- `_h_update_shadow` **L307-313**：L310-311 注释「schema 无 card_id——写入目标恒为当前卡自己的影子（R-09 写侧：不是可绕过的校验规则，是工具签名本身写不出别的目标）」；直接 `shadow_store.update_shadow(work_id, current_id, content)`，**无分类校验**（读侧 read_shadow L326 有 can_read，写侧无）。【方案锚 L307-313 ✓ 实读对回】

**⑤ 文件层（file_tools.py）**
- `ALLOWED_ROOTS = (BASE_DIR.resolve(),)` **L34**、`_resolve` **L81**（L85 规则「相对路径以 BASE_DIR 为基准；resolve 后必须落在 ALLOWED_ROOTS 之下」）、`_is_credential` **L68**——四道兜底（目录拒写 L290/凭证与依赖目录拦截已撤 L292/大小上限 L296/二进制拒写 L312）。`write_file` **L285** 无按卡分类路径锁。【方案锚 L34/L68/L285 ✓ 实读对回】

**⑥ 三个漏检口的真实状态（与方案 P017 对照）**
- `unlink_cards` 零权限检查 ✓ 如实
- `update_shadow` 无分类校验 ✓ 如实（但接口天然只能写自己）
- `card_proposal.py` L225 **已有** parent 子树校验——方案 P017 说「不校验 parent」系误述，终稿已按真实状态改为「收口到统一 check」，本纪要与终稿一致。

## 关联方案/前序任务书（记忆索引检索结果）

- `memory_expand` 检索权限管理相关裁定锚点（seq 1000000144 等）→ **原文缺失/不可用**（记忆层取不到用户裁定逐字原话）。
- **据此纪律**：本任务书凡涉「读侧全盘可读 / 写侧只能建自己的子卡片」两条语义，一律以**代码注释所记现状**描述（can_read docstring L859「用户令 2026-10-07 11:26」、card_proposal L26「用户 14:14 定」为代码内既有标注），不另行声明为「用户裁定」。方案终稿第 1 节已对此类标注仅作代码现状陈述，本任务书沿用。
- 无其他关联方案/前序任务书命中。

## 风险点

1. **POLICY 表与 card_type 双层拓扑对齐**（原方案五个平铺 key vs 真实 `project_class(rd/research)+card_type(sub_project/tool/topic/project)`）：平铺直取 card.card_type 查表时，真实存在的 `project` 根卡不在表内、落 DEFAULT_POLICY=Perm.NONE 连读拒死。→ 方案已标【待用户裁】T2，施工前若未裁定，表值一律不填。
2. **现行读/写语义是否被收口推翻**：can_read「全盘放开」、can_edit「只能建自己的子卡片」收口进 POLICY 后语义变化方向——方案已标【待用户裁】T1。
3. **删 can_read/can_edit 的 R3 施工先决**：方案终稿第 3 节标注「(R3 验证通过后成立)——施工前须验证 `_is_in_subtree` 与现行 `can_edit`（L877）子树判定严格等价，通过才删、未通过保留由 check_with_scope 兜底」。
4. **原方案落点已修正**（src/agent/ 不存在、schema_builder.py 全项目无此文件、工具名 search_cards/list_dir/update_card 注册表无）——终稿已按 p1 实证修正到真实代码，任务书沿用终稿落点，不背错误路径。

## 待侦察项（R# 编号，本步未读/需施工前补）

- **R1**：`build_tool_schemas` 加 `card_id` 入参后，上游 `message_runner.py` 装配层的调用点与 tools 传递（本次未读 message_runner，施工前必须实读确认其在哪处调 build_tool_schemas、如何补传当前卡 id）。
- **R2**：`config/card_type_registry.json` 全量 key 与 POLICY 表五个 key 的逐项比对（本次未重读 registry；POLICY 搭配值施工前必补，且受 T2 待裁约束）。
- **R3**：`_is_in_subtree》 （新 policy.py）与现行 `can_edit`（card_tools L877）子树判定**严格等价验证**（施工先决，方案约定）。
- **R4**：建卡工具化新增 create_card 后注册表实为 18 个工具（原「17 分支」差异已知）——施工清单按真实 18 工具列，不留「17」死数。
- **R5**：`POLICY` / `FILE_SCOPE` 具体搭配值在 T1/T2 未裁定前**不填**（方案约定「定之前表值一律不填」），任务书红线须带上此约束。