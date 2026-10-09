# 《权限管理_落地方案》可实现方案（终稿）

原方案文档（`trading-workspace\docs\权限管理_落地方案.md`）经方案链全程（p0 对账 24 条 → p1 实读调研 → p15 梳理三节 → p2 骨架草稿 → p3 八问复核＋红灯增量重判 → 复核放行）产出的可直接派工版本。**忠实原方案要实现的根本效果，落点全部按真实代码修正**——你要我来做可实现方案、不许我私自判断，这两条我都守住了：方案意图一字没改，两处原方案没定死的东西标了【待你裁】没替我拍。

---

## 1. 功能背景

本方案把散落在工具层各处的权限判断，收拢为「**一张按卡片分类发的权限表（POLICY）＋一个统一检查关口（check）**」——权限集中、统一入口、可见性从分类派生、默认关门、补三个漏检口、文件层按分类锁路径（对回原方案 P001/P002/P003）。

真实代码里三个真病灶（p1 实测，file:line）：

**① 权限散落、无统一关口**（对回 P002）——`can_read`（`src/api/card_tools.py` L855，读侧「全盘放开、target 存在即放行」）、`can_edit`（L877，写侧「target 在 current 子树内→可写；血统上级不可写」）被 7＋1 处内联调用（读：L143/164/326/373，写：L193/234/291，加 card_proposal.py L225），改一个漏一片、无单一可改处。

**② 工具可见性与执行判据两套表达**（对回 P003）——`build_tool_schemas()`（L833）全量导出 18 个工具、无按卡过滤，所有卡看到全部工具；工具说明又写死在 prompts.py 文字里——三处三套，必然漂移。

**③ 口子无锁**（对回 P017）——`unlink_cards`（L397-430）零权限检查；`update_shadow`（L307-313）无分类校验（仅靠接口无 card_id 天然只写自己兜底）；`card_proposal`（L225）**已有** parent 子树校验（原方案说「不校验 parent」系误述，本方案按其真实状态处理：改的是收口到统一 check，不是补缺）。

文件层（对回 P004）：`file_tools.py` `ALLOWED_ROOTS=(BASE_DIR,)`（L34）全盘可读写，无按卡路径锁。

> **落点修正说明（忠实效果、不背错误路径）**：原方案写的 `src/agent/card_tools.py`、`src/agent/schema_builder.py` 在真实代码里**不存在**（真实在 `src/api/`；`schema_builder.py` 全项目无此文件），工具名 `search_cards/list_dir/update_card` 注册表里也没有。本方案忠实的是原方案的**功能意图与目标效果**，落点全部按 p1 实证修正到真实代码。

## 2. 项目位置与接口规范

| 落点 | 动作 | 锚 |
|---|---|---|
| **新建** `src/permissions/policy.py` | `Perm` 枚举（READ/EDIT/DELETE/CREATE_CHILD/LINK/UNLINK/TREE_SCOPE＋SEARCH/TOOLS_FILE/TOOLS_WEB/TOOLS_CARD＋CARD_PROPOSAL/SHADOW_WRITE）＋`POLICY` 表＋`DEFAULT_POLICY=Perm.NONE`＋`check()`＋`check_with_scope()`＋`_is_in_subtree()` | 新文件（对回 P001/P006/P009/P011/P012） |
| `src/api/card_tools.py` | 删 `can_read`/`can_edit`，7 处内联改调 `check_with_scope()`；`build_tool_schemas()` 改从 POLICY 派生可见工具 | L855/877/143/164/326/373/193/234/291/833（对回 P002/P015/P016/P021） |
| `src/api/card_proposal.py` | L225 `can_edit` 改调 `check(parent, Perm.CREATE_CHILD)` | L225（对回 P017） |
| `src/api/card_tools.py` `_h_unlink_cards` | 加 `check_with_scope(target, Perm.UNLINK)` | L397-430（对回 P017） |
| `src/api/card_tools.py` `_h_update_shadow` | 加 `check(current, Perm.SHADOW_WRITE)` | L307-313（对回 P017） |
| `src/api/file_tools.py` | 加 `FILE_SCOPE` 表＋`check_file_access()` 接入文件工具；`_is_credential` 收敛为内部凭证排除 | L34/68/81/285（对回 P004/P018/P019） |
| `src/storage/card_store.py` | **不改**，保留为底层防御纵深 | 对回 P020 |

**接口**：`check(card_id, action, card_store)` 与 `check_with_scope(actor, target, action, card_store)` 返回 bool、拒绝抛 PermissionError（消费方＝各工具分支）；`build_tool_schemas(card_id)` 返回过滤后工具清单（消费方＝model_service call_main 的 tools；card_id 由 message_runner 装配层补传）；`check_file_access(path, card_type)` 返回 bool（消费方＝file_tools）。每次判定打点落 prompt_log（可审计，对回 P021）。

## 3. 预期效果

1. `grep -r "can_read\|can_edit" src/` → 零命中
2. `grep -r "_is_credential" src/` → 零命中
3. 所有卡片操作必经 `policy.check()`（`def check(` 唯一定义、各工具调用 check ≥7）**（R3 验证通过后成立）**
4. `tool` 分类卡请求文件 → `build_tool_schemas(tool卡id)` 不含 write_file（模型回「无此工具」）
5. 绕过 schema 直接请求 file 工具 → `check_file_access` 返回 False / `check()` 抛 PermissionError（单测可触发）
6. 影子卡写非 shadow 分类 → `check(SHADOW_WRITE)` 拒绝（单测可触发）

## 4. 方案正文（实现设计）

- **§4.1 权限核心**：新建 `policy.py`——枚举、POLICY 表（忠实原方案五 key：rd_project/research_project/sub_project/tool/topic，不改表项）、默认关门、三函数。
- **§4.2 卡片工具收口**：删两判定函数（**以 R3 为施工先决**——先验证 `_is_in_subtree` 与现行 `can_edit` 子树语义严格等价，通过才删、未通过保留由 check_with_scope 兜底）；7 处内联改调；`build_tool_schemas(card_id)` 从 POLICY 派生。
- **§4.3 补三漏检口**：unlink/update_shadow 补检查、card_proposal 收口到统一 check。
- **§4.4 文件路径锁**：FILE_SCOPE＋check_file_access 接入四文件工具。
- **施工顺序（Task 6，对回 P022）**：建 policy → 验 R3 → 收口 → 补口 → 路径锁 → grep 清零 → 回归 → 部署窗口只部署不重启。
- **全日志打点**：机械字段、不做二次判定、**不设任何时间/长度/轮次闸**。

## §你还没定的两处【待用户裁】（不影响方案骨架）

1. **现行读/写语义是否保留**——现行 `can_read`「全盘放开」、`can_edit`「只建自己的子卡片」收口进 POLICY 后，读侧是否还全盘、写侧是否还子树。原方案没写死，我不拍。
2. **POLICY 表 key 与双层拓扑的对应**——真实分类是 `project_class（rd_project/research_project）＋card_type（sub_project/tool/topic/project）` 两层；原方案五类平铺作 key。若平铺直取 `card.card_type`，真实 `project` 根卡不在表内、会被默认关门连读拒死。平铺照抄还是区分两层，你定；定之前表值一律不填。

---

**边界**：只动方案列的这一套（policy 新建＋card_tools 收口＋三漏检口＋file_tools 路径锁），`card_store.py` 不动，不加任何原方案没要的设计；`src/storage/models.yaml` 等运行态、临时件、快照一律不进任何 git 提交。

**状态**：方案链全程走完（产品齐全、落盘于 `推演agent\docs\exploration\THINK-ECO-permgmt-*` 系列、红灯全消、复核放行）。**下一步是任务书链**（把本方案转成可派工任务书）——你一句话我就开，或者你先拍上面两个待裁定项。在开工令之前，代码一个字节没动。