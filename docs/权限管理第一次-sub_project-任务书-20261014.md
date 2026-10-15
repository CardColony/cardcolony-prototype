# 任务书：权限管理第一次 · sub_project 收权 + manuscript 全删

> 日期：2026-10-14
> 性质：实现任务书。只定义规格、边界、验收；不在本文写代码。
> 方案母本：`docs/权限管理第一次-sub_project-方案-20261014.md`（已过审部分＝本任务书 §1~§5；§6 遗留待拍项见 §8）
> 用户口径（2026-10-14 17:50~18:30，逐字要点）：
> - 「先给 sub project 卡定以下权限，其余权限都取消」（随后列 11 条工具清单）
> - 「我没列的工具就是不要啊」
> - 「工具的改版不需要你处理，你只需要做权限管理」
> - 「小说相关的东西全部砍掉，还要把代码都删掉」

---

## 0. 一句话规格

sub_project 卡的工具面收成用户 11 条对应的 19 件工具；两件混在共用开关里的漏网工具（read_memory／write_file）拆独立开关拒掉；manuscript 三件**连代码带配置全删**（含 BASE_PROMPT 行与版本递增）；其余类型只做机械连带保持 v2 等价；**工具本身一行不改**（弹窗、区域化、改名等明确不做）。

---

## 1. 现状基线（代码实读，2026-10-14）

| 件 | 现状 | 落点 |
|---|---|---|
| 工具注册表 | 25 件（19 标准 + 6 思维链） | `card_tools.TOOL_REGISTRY` |
| 开关词汇表 | 12 个（read/edit/delete/create_child/link/unlink/shadow_write/manuscript/invoke_tool/tools_file/tools_web/plan） | `policy.SWITCHES` |
| 工具面过滤 | `TOOL_SWITCH` 逐件映射 → `_filter_by_category` 按卡上固化 perms 逐件查 `has_perm`；映射表外工具不下发（默认关门） | `card_tools.py` |
| sub_project 配置 | v2 = 12 开关全量（含 edit/manuscript） | `config/permissions.json` |
| read_memory 开关 | **无独立开关**，挂 `read` | `card_tools.TOOL_SWITCH` |
| write_file 开关 | **无独立开关**，挂 `tools_file`；执行层关口 `check_file_access` 查 `tools_file` | `card_tools.TOOL_SWITCH`、`file_tools.check_file_access` |
| manuscript 引用 | 代码 6 文件 + 测试 4 文件 + scripts 3 文件 + 配置 1 文件（详见 §3 清单） | 全仓 grep 核实 |
| 前端 esm/ | **零 manuscript 引用**（已核实） | — |
| works 数据 | **无 manuscript/ 目录**（13 个 work 全查，已核实） | — |
| BASE_PROMPT 版本 | `t18-1`（文本变更须递增） | `prompts.py:41` |
| 测试基线 | 全量 pytest = 403 passed / **14 failed**（基线名单见 §6 V9） | MOCK_LLM=1 |
| 存量卡 | 93 张全在 perms_version=2（sub_project 48 / topic 35 / project 11 / tool 0） | works/*/cards.db |
| 备份 | E 盘 `perm-v3-subproject-20261014T175253`（17 个源文件） | 已完成 |

---

## 2. 权限变更规格（配置 v2 → v3）

### 2.1 开关词汇表：12 → 13

| 动作 | 开关 | 说明 |
|---|---|---|
| 删 | `manuscript` | 工具已不存在，开关随之死 |
| 增 | `memory` | 单管 read_memory（从 read 拆出） |
| 增 | `file_write` | 单管 write_file（从 tools_file 拆出） |

`policy.py`：删 `SWITCH_MANUSCRIPT` 常量与集合项；增 `SWITCH_MEMORY = "memory"`、`SWITCH_FILE_WRITE = "file_write"`。

### 2.2 各类型配置（permissions.json 全表）

| 工牌/类型 | v3 开关清单 | 对 v2 的差 |
|---|---|---|
| rd_project/project | read, create_child, link, unlink, invoke_tool, plan, **memory**, tools_file, **file_write**, tools_web, shadow_write | −manuscript −edit；**根卡也收 edit**（没列＝不要的同一口径；如需保留说一声） |
| **rd_project/sub_project** | read, create_child, delete, link, unlink, invoke_tool, plan, shadow_write, tools_file, tools_web | **−edit −manuscript −memory −file_write**（用户 11 条正表） |
| rd_project/tool | read, delete, plan, shadow_write, **memory**, **file_write** | −edit −manuscript；补 memory/file_write（v2 等价：它的 read_memory/write_file 一直可见，本次不静默收权） |
| research_project/project | 同 rd_project/project | 同上 |
| research_project/topic | 同 rd_project/sub_project 外加 memory/file_write | −edit −manuscript；补两项保持等价（**topic 的权限收敛属第二次管理，本次不动语义**） |

> **待拍项已按默认执行**：tool/topic 不静默收权（保持 v2 可见面等价）；根卡收 edit。不同意任何一条，回报时说明即改。

### 2.3 映射与执行层（两处代码改动，非工具改版）

| 处 | 改动 |
|---|---|
| `card_tools.TOOL_SWITCH` | `"read_memory": SWITCH_MEMORY`（原 read）；`"write_file": SWITCH_FILE_WRITE`（原 tools_file）；删 manuscript 3 行 |
| `file_tools.check_file_access` | **写路径**须另查 `file_write`：`_h_write_file` 调用处传 write 标志（或在 handler 内对 write_file 加 `has_perm(card, SWITCH_FILE_WRITE)` 关口）；读三件照旧查 tools_file。**最小改法**：`_h_write_file` handler 加 3 行关口（与既有 denied 信封同款），`check_file_access` 签名不动 |

---

## 3. manuscript 全删清单（逐文件）

| # | 文件 | 动作 | 验证 |
|---|---|---|---|
| 1 | `src/storage/manuscript_store.py` | **整文件删除** | 文件不存在 |
| 2 | `src/api/card_tools.py` | 删 3 个 ToolDef 注册、3 个 handler（_h_list/read/search_manuscript）、TOOL_SWITCH 3 行、import 里 manuscript_store | import 成功、TOOL_REGISTRY=22 件 |
| 3 | `src/api/routes.py` | 删 2 端点（GET /works/{w}/manuscript、/{chapter}）＋2 处 import（manuscript_store、ChapterNotFoundError） | 端点 404（FastAPI 自动） |
| 4 | `src/api/prompts.py` | 删 BASE_PROMPT 中 manuscript 行；`BASE_PROMPT_VERSION` t18-1 → **t19-1** | 版本断言 |
| 5 | `src/api/message_runner.py` | 删 target_id 兜底中 `args.get("chapter") or args.get("keyword")` 与相应注释行 | grep 零命中 |
| 6 | `src/permissions/policy.py` | 删 SWITCH_MANUSCRIPT 常量＋SWITCHES 集合项 | SWITCHES=13 |
| 7 | `config/permissions.json` | 5 组合删 manuscript 值；version=3；_meta.notes 补一行 v3 说明 | config_version()==3 |
| 8 | `tests/conftest.py` | 删 patch 列表 manuscript_store 项（2 处：import 行＋for 行） | pytest 收集正常 |
| 9 | `tests/_golden/capture_golden.py` | 同上（2 处） | 同上 |
| 10 | `scripts/run_test_instance_5701.py`、`scripts/s4_compare_snapshot.py`、`scripts/s5_crash_drill.py` | 同上（各 2 处） | `py_compile` 过 |
| 11 | `tests/test_t11_tool_fold.py` | 折叠夹具对象 list_manuscript → **search_files**（readonly 同类；round_3 mock 调用同步换） | 该文件全绿 |
| 12 | `tests/test_cardperm_readopen.py`、`tests/test_perm_gateway_acceptance.py` | 删/改 manuscript 断言与 _EXPECTED 表项；**根卡 24 件断言同步**（25→22 后根卡无 edit_card ⇒ 21） | 全绿 |

**残留核验**：`grep -rn "manuscript" src/ tests/ scripts/ config/ esm/ --include="*.py" --include="*.json" --include="*.js"` 除 `.pyc` 外零命中（pyc 缓存随重启重建，不在删除范围）。

---

## 4. 明确不做（红线级）

| # | 不做 | 依据 |
|---|---|---|
| N1 | create_card/delete_card **弹窗确认** | 「工具的改版不需要你处理」 |
| N2 | read_card/read_shadow **按区域读取** | 同上 |
| N3 | update_shadow **改名 edit_shadow／单区域编辑** | 同上 |
| N4 | **不改任何工具的 schema/description/handler 逻辑**（manuscript 三件的删除除外） | 同上 |
| N5 | 不动 `works/` 任何数据、不删任何 .md/.db | 无 manuscript 数据，也无需动 |
| N6 | 不动 topic 的权限语义（只补 memory/file_write 保持等价） | 第二次管理的事 |
| N7 | 不自行 git commit／不部署 restart | 既有纪律 |

---

## 5. 施工顺序（分块，每块独立验证）

| 块 | 内容 | 完成判据 |
|---|---|---|
| 块0 | worktree（.worktrees/permbase）与 prod 逐字节核对（已同步至 2026-10-14 17:52 状态） | cmp 全一致 |
| 块1 | policy.py 开关表（删 manuscript、加 memory/file_write）＋permissions.json v3 | 开关合法性自查脚本过 |
| 块2 | card_tools.py：TOOL_SWITCH 改 2 行＋manuscript 三件全删＋_h_write_file 加 file_write 关口 | TOOL_REGISTRY=22、映射完备性自查过 |
| 块3 | routes.py 删端点＋prompts.py t19-1＋message_runner.py 删兜底 | grep 零命中 |
| 块4 | tests/scripts 六文件机械删引用＋test_t11 夹具换 search_files | 受影响测试文件单独跑绿 |
| 块5 | 全量 pytest 回基线（14 failed 名单逐条对照）＋实弹验证脚本（§6 V2~V5） | 证据齐 |
| 块6 | 回填 prod（cmp 逐字节）→ 删 manuscript_store.py → `refresh_perms.py` 刷 93 张（v3）→ 正文 md5 零丢失核验 | 全部过 |
| 块7 | 服务重启（判在途）＋生产实弹（sub_project 卡工具面 19 件）＋施工回报落盘 | 回报文件在 |

---

## 6. 验收（严格版）

> 全绿只是最低门槛。每条须有可复现证据；任一失败 → 停手如实报告。

### 正向判据

| # | 判据 | 证据要求 |
|---|---|---|
| V1 | SWITCHES = 13 个（含 memory/file_write，无 manuscript） | 打印集合 |
| V2 | sub_project 工具面 = **19 件**（read_card/read_shadow/create_card/delete_card/update_shadow/link_cards/unlink_cards/run_tool_card/web_search/fetch_web/list_files/read_file/search_files/plan 六件） | 实弹脚本逐件打印 |
| V3 | sub_project **看不见**：edit_card、read_memory、write_file、manuscript 三件 | 同上（负向打印） |
| V4 | 不授 file_write ⇒ write_file 强调用被拒（执行层 denied 信封） | 直调 _h_write_file 测试 |
| V5 | tool/topic/根卡工具面与 v2 **等价**（memory/file_write 补上后可见面不变） | 三类卡改前后可见集 diff=∅ |
| V6 | TOOL_REGISTRY=22 件、TOOL_SWITCH 映射 22/22 完备 | 完备性断言 |
| V7 | BASE_PROMPT_VERSION == "t19-1" 且 BASE_PROMPT 无 manuscript 字样 | 断言＋grep |
| V8 | 93 张卡 perms_version=3、正文 md5 与刷新前一致（剔留痕行） | 刷新脚本输出＋md5 脚本 |
| V9 | 全量 pytest：passed ≥ 基线（403+新增），failed = 14 且**名单逐条相同** | junit 对照表 |
| V10 | 生产服务重启后 sub_project 卡真实工具面 = 19 件 | 生产 API/timeline 实测 |

### 负向判据（不许出现）

| # | 不许 |
|---|---|
| N-A | 不得出现任何工具 schema/description 改动（V3 除外——那三件是删除不是改版） |
| N-B | 不得在刷新存量卡时改任何卡正文（只许 front matter perms 字段＋留痕行） |
| N-C | 不得删 works/ 下任何文件 |
| N-D | 不得让 topic 收权（等价性 V5 是硬判据） |
| N-E | 不得自行 commit／restart 之外的部署动作 |

---

## 7. 遗留与待拍（不阻塞施工）

| # | 项 | 本任务书默认 | 反悔成本 |
|---|---|---|---|
| L1 | 根卡（project）的 edit 收不收 | **收**（没列＝不要同口径） | 配置一行，随手改 |
| L2 | tool 卡的 memory/file_write | **给**（v2 等价，不静默收权） | 同上 |
| L3 | topic 的权限语义收敛 | **不动**（第二次管理的事） | — |

---

## 8. 红线

- 工具零改版（N1~N4）；只动权限层与 manuscript 删除。
- 每块改完即验，测绿才进下一块；失败停手回报。
- prod 回填必须 cmp 逐字节；存量刷新必须 md5 核正文。
- 重启只走判在途流程（progress 目录空＋ops 末条已收尾）。
- 全程不碰 `works/` 数据、不 git commit、不部署到 5701。
