# 权限管理落地方案 · p15 梳理三节（20261008）

> proposal 链 p15 环节产出。把原方案《权限管理_落地方案.md》要实现的**功能意图**梳理为三节，作为 p2 草稿骨架。忠实对象＝方案功能意图与目标效果（P001-P024 对账清单 24/24 已全绿）；落点全部修正到真实代码（p1 实证），不照抄原方案错误路径；原方案未明、必须定的事项标【待用户裁】，不替用户判断。

## 一、功能背景

**要实现的根本效果**（对回 P001-P024，逐条不发挥）：把散落在工具层各处的权限判断收拢为「**一张按卡片分类发的权限表（POLICY）+ 一个统一检查关口（check）**」——权限该集中、该统一入口、该从分类派生工具可见性、该默认关门、该补三个漏检口、文件层该按分类锁路径。

**现状（p1 实证，file:line 为当前盘上真实位置）：**
- `can_read`（card_tools.py **L855**）：读侧「全盘放开——target 存在即放行」；`can_edit`（**L877**）：写侧「target 在 current 的子树内（含自身）→ True；血统上级不可写、契约卡不可写」。调用点 `can_read`×4（**L143/L164/L326/L373**）＋`can_edit`×3（**L193/L234/L291**）＋card_proposal.py **L225**——7+1 处内联 if not can_xxx，无统一关口。
- `build_tool_schemas()`（**L833**）：遍历注册表**全量导出 18 个工具，无按卡分类过滤**——所有卡看到全部工具。
- 三个漏检口：`unlink_cards`（**L397-430**）**零权限检查**；`update_shadow`（**L307-313**）无分类校验（接口 schema 无 card_id、天然只写自己）；`card_proposal`（**L225**）**已有 parent 子树校验**（原方案 P017 说「不校验 parent」系误述，施工如实标注：改的是「收口到统一 check」，不是「补校验」）。
- 文件层 `file_tools.py`：`ALLOWED_ROOTS = (BASE_DIR,)`（**L34**）全盘可读写，无按卡分类的路径锁；`_is_credential`（**L68**）。

**落点修正依据（p1 实证，非私自判断）**：原方案写 `src/agent/card_tools.py`、`src/agent/file_tools.py` → 真实在 `src/api/`（`src/agent/` 目录不存在）；`src/agent/schema_builder.py` → **全项目无此文件**（可见性改造落到 `build_tool_schemas()` L833）；工具名 `search_cards`/`list_dir`/`update_card` → 注册表里不存在（真名 18 件：read_card/read_memory/create_card/edit_card/delete_card/update_shadow/read_shadow/list_manuscript/read_manuscript/search_manuscript/web_search/fetch_web/link_cards/unlink_cards/list_files/read_file/search_files/write_file）。

## 二、位置与接口

| 落点（真实代码） | 动作 | 对回 |
|---|---|---|
| **新建** `src/permissions/policy.py` | `Perm` Flag 枚举 + `POLICY` 常量表 + `DEFAULT_POLICY=Perm.NONE` + `check()` + `check_with_scope()` + `_is_in_subtree()` | P001/P006/P009/P011/P012 |
| `src/api/card_tools.py` | 删 `can_read`/`can_edit` 及 7 处内联，工具分支改调 `check_with_scope()`；`build_tool_schemas()`（L833）改从 POLICY 按当前卡分类派生可见工具 | P002/P015/P016/P021 |
| `src/api/card_proposal.py` | L225 can_edit 改调 `check(parent, Perm.CREATE_CHILD)` | P017 |
| `src/api/card_tools.py` `_h_unlink_cards` | 加 `check(target, Perm.UNLINK)` | P017 |
| `src/api/card_tools.py` `_h_update_shadow` | 加 `check(Perm.SHADOW_WRITE)`（如实标注：接口天然只写自己，加的是分类关） | P017 |
| `src/api/file_tools.py` | 加 `FILE_SCOPE` 常量 + 接入 `check_file_access()` | P004/P018/P019 |
| `src/storage/card_store.py` | **不改**——根卡拒删/有子卡拒删保留为底层防御纵深 | P005/P020 |

## 三、预期效果（忠实原方案 P023 六条验收，逐条原文不改）

1. `grep -r "can_read\|can_edit" src/` 零命中
2. `grep -r "_is_credential" src/` 零命中
3. 所有卡片操作调用栈必经 `policy.check()`
4. `tool` 分类的卡让模型读写文件 → 模型回复「无此工具」
5. 绕过 schema 直接请求 file 工具 → `check()` 抛 PermissionError
6. 影子卡写入非 shadow 分类的卡 → 拒绝

## 【待用户裁】（不私自判断，原方案未明、却必须定的两处）

- **T1**：现行 `can_read`「全盘放开」/`can_edit`「只能建自己的子卡片」语义，本次收口后是否保留（收口进 POLICY 表后读侧是否还全盘、写侧是否还子树）——原方案没写死，不替裁。
- **T2**：POLICY 表 key 与真实 `project_class（rd/research）+ card_type（sub_project/tool/topic/project）` 双层拓扑的对应方式（平铺与否影响 `project` 根卡会不会被默认关门连读拒死）——原方案平铺作 key，是否修正不替裁。

> 链纪律声明：本方案只述代码现状语义，不将任何一条标注为「用户裁定/亲令」（记忆层取不到原始逐字原文，用户 21:48 已指正该成像习惯）。