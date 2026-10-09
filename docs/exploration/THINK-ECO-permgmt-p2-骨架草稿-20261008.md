# 权限管理落地方案 · p2 骨架草稿（20261008）

> proposal 链 p2 环节产出。基于 p0 对账清单（24 条全绿）、p1 调研纪要、p15 梳理三节，草拟可实现方案骨架。**忠实对象＝原方案功能意图与目标效果**；落点全部按 p1 实证的真实代码；原方案未明、必须定的两处标【待用户裁】，不替用户判断。

## 背景与裁定（对的方案意图）

1. **根意图**（对回 P001-P024）：把散落在工具层的权限判断收拢为「一张按卡片分类发的权限表（POLICY）+ 一个统一检查关口（check）」，权限该集中、该统一入口、该从分类派生工具可见性、该默认关门、该补漏检口、文件层该按分类锁路径。
2. **只述代码现状，不标「用户裁定」**：记忆层取不到裁定原文（memory_expand 返回「原文缺失」），全方案对现语义（can_read「全盘放开」/can_edit「子树可写」）只描述为**代码现状**。
3. **落点修正**（p1 实证，非私自判断）：`src/agent/` → `src/api/`；`schema_builder.py` 不存在 → 可见性改造落到 `build_tool_schemas()` L833；工具名用真实 18 件（含 create_card，原方案「17 分支」是 10-08 新增前的死数）。

## 任务清单

### Task 1 · 新建权限核心模块 `src/permissions/policy.py`
- **1-1** `Perm` Flag 枚举（对回 P006）：`READ / EDIT / DELETE / CREATE_CHILD / LINK / UNLINK / TREE_SCOPE / SHADOW_WRITE / PROPOSAL / FILE_SCOPE_RD / FILE_SCOPE_RW / 工具组（SEARCH/FILE/WEB/CARD）`
- **1-2** `POLICY` 常量表（对回 P009/P010），key 与真实 card_type/policy_class 拓扑的对应方式 → **T2【待用户裁】**；搭配值由任务书在 T2 用户裁定后依真实 card_type 拓扑填写，不依赖任何单卡定稿（card_041 作假落库前科卡不作裁决方）；T2 未裁定前一律不填、不自行选值
- **1-3** `DEFAULT_POLICY = Perm.NONE`（对回 P021「默认关门」）
- **1-4** `check(card_id, action, card_store)`（对回 P011）——由卡分类查 POLICY 表
- **1-5** `check_with_scope(actor_id, target_id, action, card_store)`（对回 P012）——分类关 + `_is_in_subtree` 树范围
- **1-6** `_is_in_subtree(actor, target)`（对回 P012）——与现行 can_edit 子树判定严格等价性 → **R3 施工前必验证**

### Task 2 · card_tools.py 权限收口（对回 P002/P015/P016/P021）
- **2-1** 删 `can_read`（L855）、`can_edit`（L877）及 7 处内联调用（can_read×4：L143/164/326/373；can_edit×3：L193/234/291），工具分支改调 `check_with_scope()`
- **2-2** `build_tool_schemas()`（L833）改从 POLICY 按当前卡分类派生可见工具（对回 P015）
- **2-3** grep 验收：`can_read`/`can_edit` 零命中（对回 P023-1）

### Task 3 · 补漏检口（对回 P017）
- **3-1** `_h_unlink_cards`（L397）：加 `check(target, Perm.UNLINK)`（现零权限检查，实证）
- **3-2** `_h_update_shadow`（L307）：加 `check(Perm.SHADOW_WRITE)`（接口天然只写自己，加的是分类关）
- **3-3** `card_proposal.py` L225：`can_edit` 改调 `check(parent_id, Perm.CREATE_CHILD)`（已有 parent 校验，改的是收口，不是补校验——如实标注）

### Task 4 · file_tools.py 路径锁（对回 P004/P018/P019）
- **4-1** 加 `FILE_SCOPE` 常量（按分类→允许路径）
- **4-2** 加 `check_file_access()`；接入 `write_file`（L285 附近）等文件工具
- 注意：FILE_SCOPE 的 key 与真实分类拓扑对应 → **T2【待用户裁】共用**

### Task 5 · card_store.py 不改（对回 P005/P020）
根卡拒删/有子卡拒删保留为底层防御纵深。声明确认零改动。

### Task 6 · 施工顺序与移交边界（对回 P022）
按序施工，每步完成即验证、失败即停报：
6-1 新建 src/permissions/policy.py（Perm/POLICY/DEFAULT_POLICY/check/check_with_scope/_is_in_subtree），单元桩验证 check 按分类返回正确；
6-2 验证 _is_in_subtree 与现行 can_edit（L877）子树判定严格等价（**R3 施工先决**）——验证通过才删 can_read/can_edit，未通过则保留两函数、由 check_with_scope 内部调用兜底；
6-3 card_tools 收口：7 处内联改调 check_with_scope（can_read×4：L143/164/326/373；can_edit×3：L193/234/291），build_tool_schemas(card_id) 改从 POLICY 派生可见工具；
6-4 补三漏检口：unlink L397 加 check(target, UNLINK)；update_shadow L307 加 check(SHADOW_WRITE)；card_proposal L225 改调 check(parent_id, CREATE_CHILD)；
6-5 file_tools 路径锁：加 FILE_SCOPE + check_file_access，接入 write_file(L285) 等文件工具，_is_credential 收敛为 check_file_access 内部凭证排除；
6-6 grep 清零验证：can_read/can_edit/_is_credential 零命中；
6-7 回归验证：读/写/建/删/连线/解线/影子全路径不回归；
6-8 部署窗口：只部署不重启，重启另待用户令（红线 5）。
⚠ T2 未裁定前 POLICY 搭配值一律不填（占位标记待裁定），不自行选值。

## 红线
1. **只动产出清单内的文件与位置**——不新增、不发挥、不替用户加任何设计
2. **两处 T1/T2【待用户裁】未裁定前，不得自行选值**（T1 读/写侧收口后是否保留全盘/子树语义；T2 POLICY key 与双层拓扑对应）
3. 全程**只述代码现状，不标「用户裁定/亲令」**
4. 不动 works 用户数据
5. 部署/重启/提交须用户另发令
6. **R3 验证为施工先决条件**——验证 _is_in_subtree 与现行 can_edit（L877）严格等价通过后才删 can_read/can_edit；未通过则保留两函数、由 check_with_scope 内部调用兜底

## 验收（对回 P023 六条，忠实原文）
1. `grep -r "can_read\|can_edit" src/` 零命中
2. `grep -r "_is_credential" src/` 零命中
3. 所有卡片操作调用栈必经 `policy.check()`（R3 验证通过后成立）
4. `tool` 分类的卡让模型读写文件 → 模型回复「无此工具」
5. 绕过 schema 直接请求 file 工具 → `check()` 抛 PermissionError
6. 影子卡写入非 shadow 分类的卡 → 拒绝

## 回报要求
施工方逐项回报：每项改动 file:line + diff 摘要；R3（_is_in_subtree 与现行 can_edit 严格等价）验证结果须专项回报；两处 T1/T2 若触及施工（需默认值才能动）必须停下报"待裁定"，不得自行选择。