# 权限管理任务书链 · 输入包（20261008 23:13）

> 本包是任务书链（think chain=taskbook）输入的唯一基线。链内一切产出必须对回本包条目。

## 一、方案定稿（任务书链的源材料，唯一上游）

- **路径**：`D:\AI_station\推演agent\docs\exploration\THINK-ECO-permgmt-方案终稿-20261008.md`
- **尺寸/SHA**：SIZE=7269、SHA256=C5C8AE960D1A78BC1D80646B49BA97BB14E69C36CFEF451C35CC80A1EE190E73（已回读核验：FFFD=0、锚点 4/4 命中）
- **来源**：proposal 链全程产出（p0 对账 24 条 → p1 实读 → p15 三节 → p2 骨架草稿 → p3 八问复核＋红灯增量重判 → 复核放行），方案链 7 份产物实测在盘。

## 二、用户两道令（逐字，任务书须体现）

1. **（10-08 22:06）「你来用方案链，做出可实现方案，而且不允许你做任意的私自判断，必须忠实方案想要实现的效果」** —— 任务书链沿用此约束：不替用户拍板、不加用户没要的设计、不改方案设计意图。
2. **（10-08 23:08）「任务书路径发给我」** —— 本轮目标：走任务书链产出可直接派工的任务书，把路径报给用户。

## 三、四个已知硬事实（任务书落点修正的依据，禁止回退到错误路径）

1. **原方案文档落点全错，须修正到真实代码**——原方案写 `src/agent/card_tools.py`、`src/agent/file_tools.py`，真实在 `src/api/`（`src/agent/` 目录不存在）。
2. **`src/agent/schema_builder.py` 全项目无此文件**（glob `**/schema_builder*.py` = No files found）——可见性改造落到 `src/api/card_tools.py` 的 `build_tool_schemas()`（L833）。
3. **工具名 `search_cards`/`list_dir`/`update_card` 在真实注册表里不存在**——真实 18 件：read_card/read_memory/create_card/edit_card/delete_card/update_shadow/read_shadow/list_manuscript/read_manuscript/search_manuscript/web_search/fetch_web/link_cards/unlink_cards/list_files/read_file/search_files/write_file。
4. **原方案「17 个分支」是 10-08 新增 create_card 前的死数**——当前注册表 18 个工具。

## 四、方案核心意图（忠实对象，一字不改）

把散落在工具层的权限判断收拢为「**一张按卡片分类发的权限表（POLICY）＋一个统一检查关口（check）**」——权限集中、统一入口、可见性从分类派生、默认关门、补三个漏检口（unlink_cards / update_shadow / card_proposal 收口）、文件层按分类锁路径（FILE_SCOPE＋check_file_access）；`card_store.py` 不改（保留底层防御纵深）。

## 五、两处【待用户裁】（任务书须标注，施工遇到即停报，不得自行择一）

1. **T1 现行读/写语义是否保留**：现行 `can_read`「全盘放开」、`can_edit`「只建自己的子卡片」收口进 POLICY 后，读侧是否还全盘、写侧是否还子树。原方案没写死。
2. **T2 POLICY 表 key 与双层拓扑对应**：真实分类是 `project_class（rd_project/research_project）＋card_type（sub_project/tool/topic/project）` 两层；原方案五类平铺作 key。平铺照抄还是区分两层。

## 六、纪律（任务书须继承）

- 不动 works 用户数据；部署/重启/提交须用户另发令；交付即止（只产任务书不施工）。
- 只述代码现状、不标「用户裁定/亲令」（用户 10-08 新规：称「用户裁定」须有逐字原话＋出处）。
- 不走子代理（用户 12:07 令）。
- R3 为施工先决：验证 `_is_in_subtree` 与现行 `can_edit`（L877）严格等价通过后才删 can_read/can_edit，未通过则保留、由 check_with_scope 兜底。