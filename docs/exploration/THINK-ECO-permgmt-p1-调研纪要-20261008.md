# 权限管理落地方案 · p1 调研纪要（20261008）

> proposal 链 p1 环节产出。把原方案每个落点钉到当前真实代码（本会话实读，行号以当前盘上为准，全部 file:line 实证）。忠实对象＝原方案功能意图；落点修正＝真实代码位置（原文档路径/文件名/工具名有误处逐条注明「原文档写 X，实为 Y」）。不改任何设计意图。

## ① 权限判定现状（card_tools.py）

- `can_read(current_id, target_id, cards, work_card_id)` = **L855**。docstring 逐字：「[卡片权限改造 2026-10-07] 全盘放开——target 存在即放行」「任一卡可读全项目任何卡的信息，但不放开写；原按亲属链限读…整制退役」。实现：target 或 current 不在 cards → False，否则 True。**读侧当前＝存在即放行，无分类关。**（对回 P002/P008 意图「读侧收口到分类」→ 落点修正）
- `can_edit(current_id, target_id, cards)` = **L877**。docstring：「写权限：target 在 current 的子树内（含 current 自身）→ True；血统链上级不可写、契约卡不可写」。实现：target==current→True；否则反向走 target 的 parent 链能否到 current。**写侧当前＝子树语义，非分类。**（对回 P002/P008 意图「写侧收口」→ 落点修正）
- 散落调用点实测：can_read 调用 **L143/164/326/373**；can_edit 调用 **L193/234/291**＋card_proposal.py **L225**——**7 处内联 if not can_xxx，无统一关口。**（对回 P015「17 分支内联」→ 实测当前 7 处 + card_proposal 1 处，具体数随当日代码演化，机制「散落」属实）

## ② 工具可见性现状

- `build_tool_schemas()` = **L833**。「遍历注册表派生（改前是 14 段字面量 list ＋ file_tools 追加）」，**全量导出 TOOL_REGISTRY 全部工具，无按卡过滤**——所有卡看到全部 18 个工具。（对回 P003「手写描述与执行漂移」：现非字面量手写而是注册表派生，但**仍全量无分类过滤**，模型看的≠该卡该看的）
- 无 `schema_builder.py`（全项目 glob 实测不存在）——原文档 P003/P014 落点指向的不存在的文件，见「落点修正」节。

## ③ 三个漏检口现状

- `unlink_cards`（`_h_unlink_cards`）＝ **L397-430**。校验仅：target_id 非空、rel_type 合法、关系存在（removed==0 返回 not_found）。**无任何权限检查**（不查当前卡能否解线）。**对回 P017「零检查」成立。**
- `card_proposal`（card_proposal.py `apply_proposals`）＝ **L183-248**。L225：`if not card_tools.can_edit(current_card_id, parent, cards): reason="越权：只能建自己的子卡片，{parent} 不在 {current_card_id} 的子树内"`。**有 parent 子树校验**。**对回 P017「不校验 parent」不实**——方案误述，如实标注（补的是"走统一 check"而非"从零加"）。
- `update_shadow`（`_h_update_shadow`）＝ **L307-313**。直接 `shadow_store.update_shadow(work_id, current_id, content)`，schema 无 card_id（只能写自己）。**无分类校验**。对回 P017「不查分类」成立（但接口天然只写自己）。

## ④ 文件层现状（file_tools.py，真实路径 src/api/file_tools.py，原文档 src/agent/ 错）

- `ALLOWED_ROOTS = (BASE_DIR.resolve(),)` = **L34**；`BASE_DIR = Path(r"D:\AI_station")` = L33；`_resolve(raw)` = **L81**（resolve 后必须落在 ALLOWED_ROOTS 之下，相对路径以 BASE_DIR 为基准）；`_is_credential(path)` = **L68**；`BACKUP_DIR = BASE_DIR/"推演agent"/"_backups"` = L63；`write_file` = **L285**（含备份 L318）。
- **当前＝全盘可读写（ALLOWED_ROOTS=整个 D:\AI_station，10-04 用户授权），无按卡分类的 FILE_SCOPE 路径锁。**（对回 P004/P018/P019 → 落点修正：原文档 src/agent/file_tools.py 实为 src/api/file_tools.py；FILE_SCOPE 常量与 check_file_access 是新增）

## ⑤ card_type 真实拓扑（config/card_type_registry.json，实读）

- 双层结构实测：`project_class`（`rd_project` / `research_project`）＋ L1（`sub_project` / `tool` / `topic`）＋ `root`（`project`）。
- notes 明示：「三张根卡已迁移 project（底座收口 2026-10-08）」「存量 35+32 张已归位 sub_project/topic」。
- **与方案 POLICY 表（P008 五个平铺 key：rd_project/research_project/sub_project/tool/topic）的结构对齐点**：方案把 project_class 与 L1 平铺作 key。**真实卡面上 card_type 取值是 L1+root（sub_project/tool/topic/project 等），project_class 是否出现在 card_type 上需施工前置确认**——标【待确认】，不替用户裁决。

## 落点修正汇总（原文档错 → 真实位置）

| 原文档 | 真实 | 性质 |
|---|---|---|
| `src/agent/card_tools.py` | `src/api/card_tools.py` | 路径修正 |
| `src/agent/file_tools.py` | `src/api/file_tools.py` | 路径修正 |
| `src/agent/schema_builder.py` | 文件不存在 | 落点消失→可见性改造落到 `build_tool_schemas()`（card_tools.py L833）+ prompts.py 工具说明 |
| 「17 个分支」 | 当前 7 处 can_read/can_edit 调用 + card_proposal 1 处 | 数字随代码演化，机制「散落」属实 |
| 工具名 search_cards/list_dir/update_card | 注册表 18 件真名（create_card/read_card/read_memory/edit_card/delete_card/update_shadow/read_shadow/…/web_search/fetch_web/link_cards/unlink_cards/list_files/read_file/search_files/write_file） | 工具名修正 |

## 待施工前置确认（不替用户裁决，标出）

1. `can_read`/`can_edit` 现行语义（10-07 读侧全盘放开、10-08 写侧子树「只能建自己的子卡片」）是否在本次收口中保留——**方案要推翻这些语义，属用户裁定领域，列【待用户裁】**。
2. `project_class` 与 `card_type` 在 POLICY 表的 key 对齐方式——列【待用户裁】。