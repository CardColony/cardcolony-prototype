# 《权限管理》T3-T8 续做 · 交办单

> 承接：THINK-ECO-permgmt 执行回报 T1/T2（2026-10-09 00:15）
> 上游：THINK-ECO-permgmt-任务书-终稿-20261008.md（PM-01~PM-30 对账底册）
> 本单性质：把 T1/T2 停报的两处【待用户裁】**定性为技术实现层并定夺**（用户 2026-10-09 10:52「如果不是方向问题，你能定吧？」），转化为可立即施工的指令。
> 定夺人：诸葛（**我定的**；无任何用户逐字原话支撑处一律不署用户之名）

---

## 一、定夺（已定，照做，不再请示）

### 定夺 1 · 权限表值（原 PM-27）

按原方案 `D:\AI_station\trading-workspace\docs\权限管理_落地方案.md` L48-78 **逐字照填**五键；**唯一偏离**：五键各加 `SHADOW_WRITE` 位（理由见下）。

位名以现 `src/permissions/policy.py` 为准（`SEARCH / TOOLS_FILE / TOOLS_WEB / TOOLS_CARD / CARD_PROPOSAL / SHADOW_WRITE`）：

```
rd_project       = READ|EDIT|CREATE_CHILD|LINK|UNLINK|TREE_SCOPE|SEARCH|TOOLS_FILE|TOOLS_WEB|TOOLS_CARD|CARD_PROPOSAL|SHADOW_WRITE
research_project = 同 rd_project
sub_project      = READ|EDIT|CREATE_CHILD|DELETE|LINK|UNLINK|TREE_SCOPE|SEARCH|TOOLS_FILE|TOOLS_WEB|TOOLS_CARD|SHADOW_WRITE
topic            = 同 sub_project
tool             = READ|EDIT|DELETE|SEARCH|TOOLS_CARD|SHADOW_WRITE
```

**偏离说明（必须写进代码注释）**：原方案表值无任何 `SHADOW_WRITE`，而任务书 T5 要求给 `update_shadow` 加 `check(SHADOW_WRITE)`——两者相加＝全部已注册分类都写不了影子，影子机制（10-03 上线，72 张影子在盘）整体停摆。判定为**原方案自相矛盾**（本方案要治的是权限散落，不是废影子卡），按**保持现状**处理：给位，影子照写。

依据性质：表值＝原方案原文（逐字）；加 `SHADOW_WRITE`＝**我定的**。

### 定夺 2 · 查表取键（原 PM-28）

`policy.py` 查表**先取 `project_class`、无则取 `card_type`**：

- 新增 helper：`_type_key(card) = (card or {}).get("project_class") or (card or {}).get("card_type", "")`
- `_grants_for` 的入参、`check()` L104-106、`check_with_scope()` L130 的 `TREE_SCOPE` 判定处，统一改用该 helper
- **不动** `config/card_type_registry.json`、**不动** `card_store.py`、**不加第 6 个 key**

依据（实测）：
- `card_type_registry.json` L7 逐字：「L0 project_class（仅根卡，决定本树 L1 词表作用域）→ L1 card_type（子卡类型）」；L87 逐字：「根卡身份由 project_class 承载，project 只是类型名」
- 4 张真实根卡实读：`card_type:"project"` ＋ `project_class:"rd_project"/"research_project"`，两字段都在卡面
- 亲跑 `card_store.iter_card_metas()` 实测：返回 dict **带 `project_class`** 字段（work_001 实测 keys 含之）——权限层拿得到，零数据结构改动
- 不改的后果：根卡 `card_type="project"` 查表得 None → `DEFAULT_POLICY=Perm.NONE` → **4 张根卡连 READ 都拒，整棵树打不开**

依据性质：**我定的**（方案功能意图「权限按分类派生」的实现方式）。

---

## 二、施工落点与边界

- **T3-T8 全部在测试空间 `D:\AI_station\推演agent_test` 施工**（用户铁律一「一切测试只在测试空间」）。开工第一步：把产仓 `src/` 当前版本同步到测试仓（让任务书现场锚在测试仓成立）；同步前先备份测试仓 `src/`。
- 产仓树已落的 `src/permissions/__init__.py`、`policy.py` **保留、不接线**（零调用方、运行零影响）；产仓树**本批不再新增改动**。
- **产仓（5700）只走最后的部署窗口，且只部署不重启**；重启须另行获许可。生产 `works/` 用户数据零触碰。
- **不提交 git**（提交须另行许可）；**不引入任何闸**（无 max_tokens / timeout / 轮次封顶 / 墙钟判据）。

---

## 三、T3-T8 任务清单（按任务书逐条）

- **T3 · card_tools 权限收口**：删 `can_read`（L855）/`can_edit`（L877）——R3 已通过（27,605 对 0 破例，报告在盘），**可删**；7 处内联改调 `check_with_scope`：L143/L164/L326/L373（读）、L193/L234/L291（写）；`card_proposal.py` L225 改调 `check(parent_id, Perm.CREATE_CHILD, card_store)`。
- **T4 · `build_tool_schemas(card_id)`**：签名加 `card_id`；按当前卡分类过滤 `TOOL_REGISTRY`（未授 `TOOLS_FILE` 不下发文件四件、未授 `TOOLS_WEB` 不下发 `web_search`/`fetch_web`）；上游唯一调用点 `message_runner.py` **L201** 补传 `state.card_id`。
- **T5 · 补三个漏检口**：`_h_unlink_cards`（L397-432）加 `check_with_scope(current_id, target_id, Perm.UNLINK, cards)`；`_h_update_shadow`（L307-313）加 `check(current_id, Perm.SHADOW_WRITE, cards)`；`card_proposal.py` L225 同上。
- **T6 · file_tools 路径锁**：新增 `FILE_SCOPE` 表＋`check_file_access(path, card_type)`（先落 `ALLOWED_ROOTS` L34 边界、再叠加分类前缀）；`write_file`（L285）/`read_file`/`list_files`/`search_files` 接入；`_is_credential`（L68）收敛为内部凭证排除。
- **T7 · 三处打点**：`check`/`check_with_scope`、`build_tool_schemas`、`check_file_access` 各落一条机械字段记录到 `prompt_log`（不做二次判定、非闸）。
- **T8 · 全量回归＋验收**：V1-V8 逐条跑；测试实例先跑通，再报部署窗口等许可。

---

## 四、必须同步处理的连带（否则验收假红 / 断套件）

1. **11 处注释/文档字符串含 `can_read`/`can_edit`**（V1 是 grep 判据，不改则假红）：
   `card_tools.py` L17 / L180-182 / L230 / L319 / L570 / L848；
   `card_proposal.py` L192（**注意：该行含「用户 14:14 定」的冒名表述，一并改为「代码现状」口径**）；
   `shadow_store.py` L8；
   `policy.py` L13 / L146（自审）。
2. **三个测试文件直接调用被删函数**（不在 V1 grep 面内，但会断套件）：
   `tests/test_card_tree.py`（L55-71）、`tests/test_cardperm_readopen.py`（L33-52）、`tests/test_perm_r3_equivalence.py`（L93/113/141）。
3. **异常语义差异（关键）**：旧 `can_read` 对不存在的卡**返回 False**；新 `check()` 对卡不存在**抛 PermissionError**（`policy.py` L107-111）。工具层须把 `PermissionError` 转成既有 `err("denied", ...)` 返回；**卡不存在仍走 `err("not_found", ...)`**，不得混为一谈。这条不处理会断错误处理链。

---

## 五、验收口径的两处调整（我定，须在回报中据实记录）

1. **V4 / V5**：`tool` 分类卡**存量＝0**（实盘：project 4 / sub_project 33 / topic 35 / tool 0）→ 须在测试实例**构造**工具卡 meta 验证，不得用生产数据造卡。
2. **V6**：因五类均授 `SHADOW_WRITE`，已注册分类不会被拒 → 「拒绝证据」改用**未注册分类**触发（可达且有实据）。

---

## 六、回报要求

- 每个任务回报：改了哪个文件、哪几处（file:line）、跑通哪几条验收号。
- V1 须报 grep 命令原文与命中数（须 0）；V4-V6 须报实跑记录（命令输出/单测运行记录/日志打点行），**不接受 fixture 全绿替代**。
- 第四节三条连带逐条回报执行确认；第五节两条口径调整逐条回报执行确认。
- 施工中若触及本单未覆盖的**用户可见行为变化** → 停报，不自行择一。

---

**待用户知悉（可推翻）**：本单执行后，**连线/解线**将由「任意两卡可连」收紧为「只能连自己子树内的卡」、`tool` 类完全不能连。来源＝原方案 §3.2 树范围机制（方案明文设计），非本单新增；因它会改变用户可感知的操作，单独列出，用户可推翻。
