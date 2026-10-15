# 权限管理第一次 · sub_project 卡 — 方案

> 2026-10-14 · 待审（未动任何代码，仅完成 E 盘备份与 worktree 同步）
> 需求原话：sub_project 卡按用户 11 条工具清单授权，**没列的工具全部取消**；manuscript（小说原文）三件权限取消且**代码全删**；只做权限管理，**工具本身不改版**（弹窗确认、按区域读取、edit_shadow 改名等一律不做）。

---

## 一、sub_project 权限表（before → after）

| 开关 | 管哪些工具 | v2 | v3 | 依据 |
|---|---|---|---|---|
| read | read_card / read_memory / read_shadow | 有 | **有** | 你的第 1、2 条 |
| create_child | create_card | 有 | 有 | 第 3 条 |
| delete | delete_card | 有 | 有 | 第 4 条 |
| shadow_write | update_shadow | 有 | 有 | 第 5 条（工具名不改，改版不做） |
| invoke_tool | run_tool_card | 有 | 有 | 第 6 条 |
| link / unlink | link_cards / unlink_cards | 有 | 有 | 第 7 条 |
| tools_web | web_search / fetch_web | 有 | 有 | 第 9 条 |
| tools_file | list_files / read_file / search_files / **write_file** | 有 | 有（但见 §三） | 第 10 条 |
| plan | 思维链六件 | 有 | 有 | 第 11 条 |
| **edit** | edit_card | 有 | **无** | 没列＝不要 |
| **manuscript** | manuscript 三件 | 有 | **开关删除** | 第 8 条＋代码全删（§二） |

**v2 → v3 实际变化：sub_project 只减一个 `edit`。其余 10 个开关 v2 本来就有，与你 11 条一一对应。**

---

## 二、manuscript 全删清单（代码逐文件）

| 文件 | 动作 |
|---|---|
| `src/storage/manuscript_store.py` | **整文件删除** |
| `src/api/card_tools.py` | 删 3 个工具注册（list/read/search_manuscript）＋3 个 handler＋TOOL_SWITCH 3 行＋import |
| `src/api/routes.py` | 删 2 个 HTTP 端点（GET /works/{w}/manuscript、/{chapter}）＋import（manuscript_store、ChapterNotFoundError） |
| `src/api/prompts.py` | 删 BASE_PROMPT 里 manuscript 那行；**版本号 t18-1 → t19-1**（BASE_PROMPT 文本变更须递增，既有规矩） |
| `src/api/message_runner.py` | 删 625 行 target_id 里的 chapter/keyword 兜底＋注释 |
| `src/permissions/policy.py` | 删 SWITCH_MANUSCRIPT 常量＋SWITCHES 集合项 |
| `config/permissions.json` | 全部 5 个组合的 manuscript 值删除 |
| `tests/conftest.py`、`_golden/capture_golden.py`、`scripts/run_test_instance_5701.py`、`s4_compare_snapshot.py`、`s5_crash_drill.py` | 删 patch 列表里的 manuscript_store 项 |
| `tests/test_t11_tool_fold.py` | 测试夹具用 list_manuscript 当折叠对象 → **换成 search_files**（同为 readonly 工具，测试语义不变） |
| `tests/test_cardperm_readopen.py`、`test_perm_gateway_acceptance.py` | 删/改 manuscript 断言（可见清单、_EXPECTED 表） |
| 前端 `esm/` | 已核实**零引用**，无需动 |
| 数据 `works/*/manuscript/` | 已核实**不存在**，无需清数据 |

---

## 三、两个隐藏的「共用开关」问题（不拆就拒不掉）

**问题**：你清单没收的工具里，有两件**不是独立开关**，混在别的开关里——只动配置拒不掉它们：

| 工具 | 现在挂在哪个开关 | 问题 |
|---|---|---|
| read_memory | read | 你要 read_card/read_shadow（第 1、2 条），但 read_memory 也归 read——**不拆，sub_project 有 read 就会看见它** |
| write_file | tools_file | 你要读三件（第 10 条），但 write_file 也归 tools_file——**不拆，sub_project 有 tools_file 就能写文件** |

**方案**：拆两个新开关（12 → 13 个，同时删 manuscript，净 12→13）：

| 新开关 | 管什么 | sub_project | 其他四类卡 |
|---|---|---|---|
| `memory` | read_memory | **不授**（看不见） | 授（保持 v2 等价：v2 时它们一直可见） |
| `file_write` | write_file | **不授**（看不见＋执行层拒） | 授（同上） |

改动点：`policy.py` 加 2 常量、`card_tools.py` TOOL_SWITCH 改 2 行、`file_tools.py` write 路径关口改查 file_write、配置各组合补值。

---

## 四、其他类型卡（本次一律不动，只做机械连带）

| 类型 | 动作 |
|---|---|
| rd_project/project（根卡 ×9） | 只删 manuscript 值；补 memory/file_write（保持 v2 等价） |
| rd_project/tool（存量 0 张） | 同上 |
| research_project/project（根卡 ×2） | 同上 |
| research_project/topic（35 张） | 同上（它的权限收敛等后续发令，本次不动） |

配置 version **v2 → v3**，改完跑 `refresh_perms.py` 刷 93 张存量卡。

---

## 五、明确不做（工具改版，等后续发令）

- create_card / delete_card **弹窗确认**（你第 3、4 条的后半句）
- read_card / read_shadow **按区域读取**（第 1、2 条前半句）
- update_shadow **改名 edit_shadow、单区域编辑**（第 5 条）
- 前三项涉及工具 schema/前端/交互改造，本次权限管理不碰

---

## 六、验证计划

1. 全量 `MOCK_LLM=1 pytest -q`：14 failed 基线不变、新增断言全绿
2. 实弹脚本：sub_project 工具面应见 19 件（25 − manuscript 3 − read_memory − write_file − edit_card）；
   - 删 edit ⇒ edit_card 不可见（原有）
   - 不授 memory ⇒ read_memory 不可见（新）
   - 不授 file_write ⇒ write_file 不可见＋强调用被拒（新）
3. 全仓 `grep -rn manuscript src/ tests/ scripts/ config/ esm/` 零命中
4. 服务重启 + 生产验证

## 七、施工顺序（方案过审后）

worktree 改 → 测绿 → 回填 prod → 删 manuscript_store.py → 刷存量卡 → 重启验证 → 施工回报
