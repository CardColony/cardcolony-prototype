# 删除旧 role 路径 · 勘察清单（card_038，只读勘察，未改任何代码）

- 工作根：`D:\AI_station\推演agent\.worktrees\5713`（HEAD=6a2435e + 未提交改动；另有卡正在改 `src/api/card_tools.py`，该文件行号以**锚点串**为准，行号为 2026-10-22 实测快照）
- 判据方法：①python AST 解析函数/注册表定义；②正则全量 grep `src/ tests/ static/ config/ verify/`；③沿调用链人工追读（routes→handler→room→store）；④sqlite `mode=ro` 实查生产与 sandbox 库。
- ⚠️ **先读第 0 节的重大发现，它决定第 5 节的顺序。**

---

## 0. 重大发现：role_room 不只是旧路径，它是当前唯一在跑的「巡检引擎」

职能（function）侧目前只有：档案存储（`function_store.py`）、判断工具（record_verdict 等五件）、上下文区（order 93/145）。**职能侧没有任何"被同步→入队→起回合"的巡检循环**——`src/` 内不存在 function 版的 `after_turn / spawn / build_prompt / pending_batch / startup_sweep_all`。

也就是说：**删掉 `role_room.py`＝把"第二层观察者被工作卡收口唤醒"的机制整体带走，新路径没有承接者**。观察者机制会静默消失，而不是"平滑切换"。为此第 5 节的顺序把"职能侧补齐巡检引擎（或在裁决中明确接受观察者下线）"列为删 `role_room.py` 的前置条件。

（佐证：sandbox 里 function_profiles 仅 4 行、function_sync 0 行——职能从未被同步唤醒过；而 role_sync 有 141 行。）

---

## 1. 旧 role 路径的完整入口清单

### 1a. 存储层
| 入口 | 位置 | 职责 |
|---|---|---|
| `role_store.py`（整个模块，669 行 63 函数） | `src/storage/role_store.py` | role_* 六张表唯一读写口：档案/判词/警报/同步队列/指令/职责书版本 |
| `_ROLE_DDL`（六张表+索引） | `src/storage/card_db.py:233-321`（role_profiles L234、role_verdicts L247、role_alarms L261、role_sync L280、role_directives L293、role_charter_history L311） | 建表 DDL |
| `_ensure_tables` 建表调用 | `card_db.py:763-779`（L775 `for ddl in _ROLE_DDL`） | 每次连接幂等建表 |
| `_ensure_role_columns` | `card_db.py:782-808` | role_directives/role_alarms 增量补列 |

### 1b. API 层（引擎）
| 入口 | 位置 | 职责 |
|---|---|---|
| `role_room.py`（整个模块，1285 行 62 函数） | `src/api/role_room.py` | 订阅边维护、信号同步、巡检回合起停、prompt 组装、判词/警报/指令包装 |
| 关键常量 | `role_room.py:34-36` `ROLE_TYPE/LINK_TYPE_ROLE/LINK_TYPE_WATCH`；`:42-43 DEFAULT_SIGNALS/ALL_SIGNALS` | 类型名与信号词表 |
| 借给新路径的 `make_profile` | `role_room.py:108` | 纯函数：组默认档案——**create_function 与 POST /functions 都在用它**（见 2c 双轨） |

### 1c. 工具 handler（`src/api/card_tools.py`）
| 入口 | 位置 | 职责 | 引用方 |
|---|---|---|---|
| `_h_create_role` | :3869 | 组角色卡（写 cards+role_profiles） | `routes.py:1401` |
| `_h_role_watch_cards` | :3900 | 根卡调角色关注集 | `routes.py:1520` |
| `_h_update_role_profile` | :3932 | 改角色档案 | `routes.py:1445` |
| `_h_set_role_duty` | :3971 | 值班态 | `routes.py:1494` |
| `_h_issue_directive_legacy` | :4067 | 角色下指令 | `_h_issue_directive`(:3849) |
| `_h_verify_directive_legacy` | :4113 | 验收指令 | `_h_verify_directive`(:3861) |
| 五件判断工具的 role 分支 | record_verdict :3747、raise_alarm :3793、correct_alarm :3824-3828（`_h_record_verdict`/`_h_raise_alarm`/`_h_correct_alarm` :3718/:3755/:3807 的 else 侧） | 存量 role 卡回合写 role_* 表 | 工具执行链 |
| `_function_actor` 的 role 识别 | :3503-3531（:3522 cards 快捷、:3527 回库 `role_room.is_role`） | 回合身份判定 | 五件工具+指令两件 |
| 指令闸 | :715、:945（`role_room.open_directives_for`） | 未验收角色指令挡 update_goal complete / report_done | 收口链 |
| 读卡 role 分支 | :487-496（read_card 返回六节结构） | 模型读角色卡 | 读链 |
| 订阅边判据 | :3223（`role_room.linked`，通信线闸放行） | 角色↔关注卡互发信 | 发信链 |

### 1d. HTTP 端点（`src/api/routes.py`）
| 端点 | 行号 | | 端点 | 行号 |
|---|---|---|---|---|
| GET /works/{wid}/roles | :1382 | | POST /works/{wid}/roles | :1393 |
| GET …/roles/{rid} | :1416 | | PATCH …/roles/{rid} | :1428 |
| POST …/roles/{rid}/directives | :1452 | | POST …/directives/{did}/verify | :1470 |
| POST …/roles/{rid}/duty | :1487 | | POST …/roles/{rid}/check | :1503 |
| POST …/roles/{rid}/watch | :1514 | | DELETE …/watch/{cid} | :1530 |
| GET /works/{wid}/alarms | :1537 | | POST …/alarms/{aid}/ack | :1544 |
| POST …/alarms/{aid}/correct | :1556 | | | |

请求模型：`CreateRoleRequest :1341、RoleWatchRequest :1351、RoleProfileRequest :1356、RoleDutyRequest :1365、RoleDirectiveRequest :1370、RoleDirectiveVerifyRequest :1377`。
其它 routes 引用：`import :37`、警报红点 `:274`、读卡 role 分支 `:601-604`、回合收口 `after_turn :1861`。

### 1e. 上下文与提示词
| 入口 | 位置 | 职责 |
|---|---|---|
| `_load_role_cabinet` | `src/api/context_assembler.py:234-284`（直查 SQL role_profiles+role_verdicts，L262/273） | 阁员现状取数 |
| `render_role_cabinet_region` + order 175 区 | `src/api/context_regions.py:94-192、530-542` | 第二层角色区（根卡） |
| `ROLE_CARD_BASE_PROMPT/_SEC_ROLE_CARD/ROLE_TOOLS_NOTE/ROLE_CARD_PROMPT_VERSION` | `src/api/prompts.py:251/186/220/184`；`select_base_prompt :256-265` | 角色卡使用说明 |
| 规则区排除 role | `context_regions.py:1248`；工作卡排除 role `:120` | 区渲染口径 |

### 1f. 常量与别名
- `ROLE_ONLY_TOOLS`：`card_tools.py:113`（= FUNCTION_ONLY_TOOLS 纯别名，**活**——被 :4824 基础面过滤引用）；注释 :111-112。
- `_ROLE_FACE_DROP` / `_is_function_turn` ② / `_apply_function_toolface` / 别名 `_apply_role_toolface`：`card_tools.py:4988 / 4993-5013 / 5016-5033 / 5036`。
- `ROOT_ONLY_TOOLS`（只含 function 五件+draft_rule_edit）：`card_tools.py:4670-4672`——旧组阁工具**已不在注册表**。

### 1g. 前端
- `static/esm/api.js:148-162`：12 个 /roles 调用封装。
- `static/workspace.js:685,1088,1143,1168,1179,1192,1213,1240,1262,1273`：角色卡面板/组阁/指令 UI。

### 1h. 配置
- `config/card_type_registry.json:90-97、124-131`：两个 project_class 词表各含 `{"code":"role",…,"off_tree":true}` 条目。
- `config/permissions.json:75-82、117-124`：role 卡默认 perms（read/edit/read_linked/tools_read_file/send_card_message/shadow_write）。
- `config/context_regions.json:16,31,39,51`：function_regions 说明（role 无专条目，但 role_cabinet 区渲染依赖上文 1e）。

### 1i. 启动挂载
- `main.py:127-128`：`startup_sweep_all()` 启动续跑 pending 角色巡检。

---

## 2. 三类状态标注（判据：AST 定义清单 × 正则 grep 全部调用点 × 逐链追读）

### 2a. 活（删了会断）
| 入口 | 为什么活（调用链） |
|---|---|
| `role_room.after_turn` | routes.py:1861 每个工作卡回合收口都调 |
| `role_room.schedule_card_changed` | card_store.py:1237-1239 唯一写门后调（存储层反向 import api 的唯一一处） |
| `role_room.startup_sweep_all` | main.py:128 启动即调 |
| `role_room.open_directives_for` | card_tools.py:715/945、goal/tools.py:163 指令闸 |
| `role_store.list_profiles` | goal/tools.py:127 零角色收口闸判据③旧分支 |
| `role_room.role_summary` | card_tools.py:488、routes.py:603/1388、card_tools.py:4221 |
| `role_room.is_role` | card_tools.py:3527（_function_actor 回库自查） |
| `role_room.make_profile` | **card_tools.py:3563（create_function）与 routes.py:1617（POST /functions）——新路径反向依赖** |
| `role_room.linked` | card_tools.py:3223 通信线「角色↔关注卡」订阅边放行 |
| `role_room.list_roles` | routes.py:1387、card_tools.py:4218 |
| `role_room.list_alarms/ack_alarm/correct_alarm` | routes.py:274/1541/1548/1552/1560（警报红点+警报台）+ 五件工具 role 分支 |
| 10 个 /roles 端点 | 前端 workspace.js 实调（createRole :1213、updateRole :1143、setRoleDuty :1168-1192、checkRole :1088、issueDirective :1240、verifyDirective :1262、unwatchRole :1273、getRole :685） |
| `role_room.manual_check` | routes.py:1511 |
| `role_room.update_profile/set_duty/watched_cards/get_profile/link/unlink/create_role_card/record_directive/verify_directive/record_verdict/raise_alarm` | 被 1c 各活 handler 调 |
| `ROLE_ONLY_TOOLS` 别名 | card_tools.py:4824 基础面过滤在用 |
| `select_base_prompt` 的 role 分支 | context_regions.py:309（order 0 使用说明区）→ prompts.py:263 |

### 2b. 死（无任何调用方，可直接删）
| 入口 | 位置 | 判据 |
|---|---|---|
| `_h_record_verdict_legacy` | card_tools.py:3990 | AST+grep：全仓引用仅定义处 1 次 |
| `_h_raise_alarm_legacy` | card_tools.py:4017 | 同上 |
| `_h_correct_alarm_legacy` | card_tools.py:4047 | 同上 |
| `_h_list_roles` | card_tools.py:4210 | 同上（list_roles 工具名已不在 TOOL_REGISTRY） |
| `role_set_duty` 工具 | 无 | 0 引用 0 文件（使命预置数据复核一致）——**不需要删，它已经不存在** |
| `role_room.role_text` | role_room.py:1245 | 仅定义 1 次，无调用 |
| `role_room.active_batch` | role_room.py:815 | 仅内部残留引用，引擎整体处置时一并核销 |
| `_dbg_sync.py` / `_t31_check8.py` 等根目录调旧路径的散件 | 仓库根 | 调试残留，非生产链路 |

### 2c. 双轨（新旧并存、两套都在跑）
| 项 | 旧 | 新 | 现状 |
|---|---|---|---|
| 组阁 | `_h_create_role`+POST /roles（写 cards+role_profiles） | `create_function`+POST /functions（写 function_profiles） | 模型侧只见新（旧组阁工具不在 TOOL_REGISTRY）；UI 侧只见旧（function 无组阁 UI）——**同一能力两个入口各占一半** |
| 判断落账 | role 分支写 role_verdicts/role_alarms | fn_ 分支写 function_verdicts/alarms | 同一 handler 内 if/else 分流（:3737-3752 等） |
| 收口闸判据③ | role_profiles | function_profiles | goal/tools.py:110-135 两套都认，任一命中放行 |
| 指令 | role_directives（有表有链路） | **无表**（`_NO_FUNCTION_DIRECTIVE_LEDGER` :3834，fn_ 下指令显式拒） | 半轨：职能指令能力缺失 |
| 巡检引擎 | role_room 全套 | **无** | 单轨倒挂：引擎只在旧路径（见第 0 节） |

---

## 3. `card_type=="role"` 判定点全列表 + 存量卡处置

### 3a. 判定点（20 处，行号为实测快照）
role_room.py: `:70(is_role)` `:79(list_roles)` `:162(ensure_profile)` `:435,:437(linked)` `:488,:492(link)` `:640(_notify_card_changed)` `:693(after_turn)`
card_tools.py: `:487(read_card)` `:3522(_function_actor)` `:3910(_h_role_watch_cards)` `:3942(_h_update_role_profile)` `:4078(不给另一角色下指令)` `:5013(_is_function_turn ②，函数体 :4993-5013)`
routes.py: `:601(读卡 role 分支)` `:1423(GET role 校验)` `:1509(check 校验)`
card_store.py: `:682(写闸：role 卡不可有子卡)` `:1237(写门后同步钩子)`
prompts.py: `:263(select_base_prompt)`
context_regions.py: `:120(工作卡口径排除 role)` `:1248(规则区排除 role)`
goal/tools.py: `:87(收口闸工作卡口径排除 role)`（:106 role_profiles 判定见 2a）
配置：card_type_registry.json 词表条目、permissions.json 默认 perms。

### 3b. 存量 role 卡实查（sqlite mode=ro）
| 库 | role 卡 | 档案 |
|---|---|---|
| 生产 `D:\AI_station\推演agent\works\work_018` | card_011「涌现观察员」（活跃，last_wake 2026-10-21T00:09） | 1 行 |
| 生产 `…\works\work_022` | card_003「架构顾问」、card_004「产品负责人」（2026-10-21 新建） | 2 行 |
| sandbox `5713_sandbox\works\` | 33 张（22 个 work） | 36 行 |

### 3c. 删除后的影响与处置建议
影响：
1. **不会变成"无类型卡"**——card_type 存在于 cards 表存量行，注册表只闸写入不闸读取；但**带 `card_type="role"` 的任何 PATCH 会过不了闸2**（card_store.py:658-671：'role' 不在任何 L1 词表 ⇒ 报"不属于词表"）；不带 card_type 的 PATCH（只写现状块）仍可通过。
2. 读侧：read_card role 分支删除后回落普通三块返回，六节结构与 role_summary 消失；前端角色卡面板 getRole 报错。
3. 行为：无人再唤醒它们（engine 删除后）——观察者静默退役；但 3a 的"排除 role"判若不删，这些卡仍是"谁都不能改的僵尸"。

处置建议（推荐 A+B 组合）：
- **A（必做）导出留档**：把生产 work_018 的 26 条判词（涌现观察唯一研究原料）与 3 份档案导出 JSONL 存 `verify/5713/`，再谈删表。
- **B（推荐）迁移在用的 3 张**：card_011/card_003/card_004 的 charter/watch/signals 照搬 `function_store.create_function`（迁移脚本，新 fn_NNN），cards 表三张 role 卡标记废弃（见 C）；其余 sandbox 存量随库废弃不迁。
- **C（配套）注册表收口**：card_type_registry.json 把 `role` 从 L1 词表移除、加入 `legacy_types`（write_blocked=true）——写路径显式拦截、读路径不报错，机械上杜绝新建 role 卡，存量卡保留只读。
- 不建议"保留只读运行"：那是双轨永续，与"不留双轨"的立项目标矛盾。

---

## 4. 五张 role_* 表存量数据（只读统计，未删任何行）

### 4a. 生产（`D:\AI_station\推演agent\works\*\cards.db`，22 个库）
| work | role_profiles | role_verdicts | role_alarms | role_sync | role_directives | role_charter_history |
|---|---|---|---|---|---|---|
| work_018 | 1 | **26** | 1 | 28 | 0 | 4 |
| work_022 | 2 | 0 | 0 | 0 | 0 | 2 |
| 其余 20 个 work | 0 | 0 | 0 | 0 | 0 | 0 |
| **生产合计** | **3** | **26** | **1** | **28** | **0** | **6** |

### 4b. sandbox（`5713_sandbox\works\`，83 个带 cards.db 的 work）
| 表 | 行数 | 备注 |
|---|---|---|
| role_profiles | 36 | 分布：work_008/010/011/013/016/025/027/028/029/060/061/076 等多为 2 行 |
| role_verdicts | 79 | 最多 work_078=13、work_027=13 |
| role_alarms | 19 | |
| role_sync | 141 | work_078 独占 50 |
| role_directives | 41 | work_013=7、work_027=6、work_060/061=4×2 |
| role_charter_history | 46 | |
| function_profiles | 4 | work_081=3、work_083=1（新路径几乎无存量） |
| function_charter_history | 4 | |

### 4c. meta 表孤儿键（同步去重哈希/基线，role_state:/role_seen:/role_base: 前缀）
sandbox 实查：`role_state:` 54、`role_seen:` 21、`role_base:` 89，共 164 键、22 个 work 有残留。删表后成孤儿键（无害），建议删表脚本一并 `DELETE FROM meta WHERE key LIKE 'role\_%'`（注意 role_state/role_seen/role_base 三前缀）。

**"删表会丢多少"一句话**：生产丢 3 份档案+26 条判词+1 条警报+6 版职责书（work_018 判断簿是涌现研究唯一原料，**必须先导出**）；sandbox 丢约 36 档案+79 判词+19 警报+41 指令+46 职责书版本（实验残留，可随库废弃，但导出成本低、建议顺手存档）。

---

## 5. 删除顺序建议（每步之后系统可跑）

**一次性删的下场**：routes.py:37 与 main.py:127 仍 `import role_room` ⇒ 进程起不来；card_tools.py:3563 与 routes.py:1617（create_function 执行体）仍调 `role_room.make_profile` ⇒ 组职能工具炸；context_assembler 直查 role_profiles 表 ⇒ 根卡上下文装配炸。**role_room.py / role_store.py 必须最后删，且前置依赖要先摘。**

可分步施工单（每步独立可验收、pytest 不新增红为过闸线）：
1. **摘死代码**（零风险）：删 2b 全部入口（4 个 handler + role_text/active_batch + 根目录调试散件）。系统行为零变化。
2. **职能侧补引擎或明确裁决**（第 0 节前置）：要么实现 function 版 after_turn/spawn/build_prompt（function_sync 表已建好，schema 就绪），要么产品裁决"观察者机制整体下线"后按 3-7 直删。此步是**方向裁决**，不是纯技术。
3. **`make_profile` 搬家**：函数体（纯函数，默认值字典）挪入 `function_store.py` 或中性模块，card_tools.py:3563 / routes.py:1617 改引。此后 role_room 对新路径零依赖。
4. **断 HTTP 旧面**：删 routes.py 10 个 /roles 端点 + 6 个请求模型；**/alarms 三端点先不要删**——它们是全 work 警报台（含迁移后职能警报的展示面），应改挂 function_store 后再改名保留；前端 api.js:148-162、workspace.js 10 处同步摘除（或先给 410 响应再摘 UI）。此步后 UI 组阁入口消失，统一走根卡 create_function。
5. **摘工具执行体 role 分支**：五件判断工具删 else 分支（:3747-3752/:3793-3804/:3823-3828）；`_h_issue_directive`/`_h_verify_directive` 删 legacy 转发（:3849/:3861→:4067-4161），保留 fn_ 拒绝语义；`_function_actor` 删 role 识别（:3522/:3526-3530）；`_is_function_turn` 删 ②（:5013）。此步后存量 role 卡回合拿不到判断工具＝实质退役。指令闸（:715/:945）同步改挂 function_directives（若第 2 步建了台账）或摘除。
6. **摘闸与挂载**：goal/tools.py 判据③旧分支（:126-135 收敛为 function_store 单源）；main.py:127-128 摘 startup_sweep_all；routes.py:1861 摘 after_turn；**card_store.py:1237-1239 摘 schedule_card_changed**（顺带消除存储层→api 的反向 import，这是架构债）。
7. **迁移与导出**：跑 3c-A 导出脚本 + 3c-B 迁移脚本（若裁决迁移）。
8. **删上下文与配置**：context_assembler.py:234-284、context_regions order175 区与 :120/:1248 两处排除、prompts.py 角色卡段与 select_base_prompt 分支、permissions.json role 行、card_type_registry.json role 条目→legacy_types、api.js/settings 无关可留。
9. **删模块与表**：删 `src/api/role_room.py`、`src/storage/role_store.py`；card_db.py 删 `_ROLE_DDL`+`:775` 建表循环+`_ensure_role_columns`；一次性运维脚本 `DROP TABLE role_*`（六张）+清 4c 孤儿键。至此 import 断言可通过。

---

## 6. 删除验收判据建议（机械可执行）

1. **import 断言**：`python -c "import src.storage.role_store"` 与 `import src.api.role_room` 均非零退出。
2. **grep 断言**：`src/ tests/ static/ config/` 内 `role_store|role_room|ROLE_ONLY_TOOLS|create_role|role_watch_cards|update_role_profile|role_set_duty|/roles|role_profiles|role_verdicts|role_alarms|role_sync|role_directives|card_type.*["']role` 零命中。**豁免**：`src/roles.py`、`config/roles.json`、`settings_api.py:50`、`model_config.py`、`static/settings.js:578`——那是"模型绑定角色表"（主模型/审议席绑定），与角色卡同名单不同物，**不在删除范围**。
3. **表断言**：生产+sandbox 每个 `works/*/cards.db`：`sqlite_master` 无 role_ 前缀表；`function_*` 五表仍在、行数不减。
4. **端点断言**：`GET /api/works/{wid}/roles` → 404；`GET /api/works/{wid}/functions` → 200；`POST /api/works/{wid}/functions` → 201（新路径未误伤）。
5. **工具面断言**：TOOL_REGISTRY 仍含五件判断工具+五件职能工具（52 件总量按当前基线）；`build_tool_schemas` 对 fn_NNN 卡输出职能面、对普通/根卡不含 record_verdict 等。
6. **回归断言**：全量 pytest 对照 `verify/5713/baseline_known_red.json` 不新增红；**注意验收器自身要同步改**——`verify_function_tools.py:28,786`（H 项断言旧路径可 import）、`verify_function_entity.py:42,277`（F 项断言 role_* 表仍在）、`_t31_check8.py`、`tests/test_role_*.py` 六个文件、`tests/test_coder_card.py:93` 与 `test_dep_relations.py:177`（ToolDef 计数断言）、`tests/test_function_store.py:279-285`（用 role_store 造旧数据）在删除步必须一并翻转为新口径，否则它们会"红"。
7. **行为断言**（若走引擎迁移路线）：迁移后的职能在关注卡收口后被唤醒且能 record_verdict 落 function_verdicts。

---

## 附：本次勘察未改动任何代码的证明
`git status --porcelain` 在勘察前后对比，除本文件 `verify/5713/role_removal_inventory.md`（新增）外无任何变化。库一律 `file:...?mode=ro` 只读连接。
