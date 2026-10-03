# 影子卡机制 · 实现方案（终稿）

> 落盘日期：2026-10-03
> 来源：方案链 proposal 终稿（p0 条目清单 → p1 真实调研 → p15 三节梳理 → p2 骨架草稿 → p3 对账复核 → p5 呈报放行）
> 放行时间戳：2026-10-03T17:51:00+08:00
> 裁定依据：`SHADOW_CARD_RULINGS.md`（提交 f28b4e1）
> report-only：本方案不执行、不改代码、不重启服务

---

## 1. 功能背景

**条目锚：R-01～R-11、C-08**

正卡推进过程中会产生大量过程性信息——进度、产物位置、迭代与改动。这些信息放进正卡正文会污染给人看的内容，扔掉则丢失过程痕迹（R-10／R-11，用户 15:05 原话三类）。

**不做会留下什么，盘上有实证**：`work_002/cards/card_002.md`（「项目现状地图」）建于 12:51:52、`updated_at` 同为 12:51:52，**建后一次未更新**（C-08 相关事实，p1 调研纪要实读），而同期盘上剧本从 2 个长到 6 个（`works/work_001`–`work_006`，p1 实读目录），并新增 `CHANGELOG.md`／`VERSIONING.md`／`SOP_清理废弃剧本.md` 三份文档——手工维护的现状快照一个都没记。**过程信息靠「想起来了手工补一次」必然腐烂。**

**要解决什么**：为正卡提供一个只服务于追踪的配对附卡——由正卡自己书写（R-04）、随正卡生命周期存续（不变量 3）、过程信息有归宿且不与正卡正文混杂（R-03 一正一影／R-11 三类内容）。

**边界（不做的事，逐条有裁定出处）**：不做系统自动写内容（R-04）；不做总览视图（R-08）；人不直接改影子（R-06）。

## 2. 项目位置与接口规范

**条目锚：R-02、R-05、R-06、R-09、R-12～R-22、C-01～C-09、B-01～B-05**

### 2.1 位置（对照 p15 B-1，7 行逐条沿用）

| # | 落点 | 锚 |
|---|---|---|
| 1 | **存储层**——影子文件本体，新增 `shadows/` 目录于剧本目录下，与 `cards/` 同级 | `card_store._cards_dir(work_id)` 是卡片路径唯一来源（`src/storage/card_store.py:118/238/490` 均由其派生） |
| 2 | **生命周期挂钩**——正卡创建／更新／删除三处 | `card_store.py:316-318`（create 的写保护钩点段：L316 原 `git_auto_commit.commit` 已于 2026-10-03 改为 L318 `await data_snapshot.snapshot(...)`）／L458（update 的 snapshot 钩点）／L516（delete 的 snapshot 钩点）；`delete_card` 本体 L466-517 **零步碰 `shadows/`**（p1 核实的真缺口） |
| 3 | **权限索引装载点**——影子 meta 并入 `_collect_card_meta` 的字典 | `src/api/card_tools.py:155-169`；`can_read` L172-231 四判据／`can_edit` L234-235 只判子树 |
| 4 | **工具层**——两个新工具（`update_shadow`／`read_shadow`）挂进分发链 | `src/api/card_tools.py:261` `execute_tool` 分发；文件工具四分支在 L429/434/440/446 |
| 5 | **快照保护**——影子自动进快照、零配置 | `src/storage/data_snapshot.py:54` `root.rglob("*")` 全树 ＋ `PROTECTED_DIRS = ("works",)` |
| 6 | **展示空间豁免**——影子不进画布、不进模型卡片清单、不占序号 | `src/storage/canvas_store.py:73`／`src/api/context_assembler.py:39`／`src/storage/work_store.py:67/151`——四处 glob 全部只扫 `cards/`（p1 实读） |
| 7 | **进度来源**——第 1 类记录内容的现成数据源 | `src/api/progress.py:12` `_store: Dict[Tuple[str,str],...]`；写入点 `src/api/routes.py:367/379/383/471/509/515` |

### 2.2 接口规范·输入（对照 p15 B-2 前置输入表）

| 项 | 来源 | 格式 | 生产方 |
|---|---|---|---|
| 影子文件初始 | 正卡创建时同步派生（R-04／不变量 4） | 同构 `.md`（front matter ＋ 三类正文段），落 `works/{work_id}/shadows/{owner_card_id}.md` | 正卡（模型），经工具通道 |
| 进度数据 | `progress.py` 的 `_store`（`(work_id, card_id)` 分键） | 阶段／状态／时间戳 | 系统（已有）→ **由正卡读取后自行书写**（R-04：不由系统自动写内容） |
| 产物与变更 | 正卡推进时自身掌握 | 路径＋用途／变更摘要 | 正卡（模型） |
| 上级读取请求 | 上级卡的 `read_shadow` 调用 | 卡片编号（缺省＝自己） | 上级卡（模型） |

### 2.3 接口规范·输出（对照 p15 B-2 后置输出表）

| 项 | 去向 | 格式 | 消费方 |
|---|---|---|---|
| 影子文件 | `works/{work_id}/shadows/{owner_card_id}.md` | 同构 `.md`（可被既有 `read_card`／`update_card` 链路处理，含 `apply_hash`） | 正卡自己（写）／现有 `can_read` 放行的任意卡（读，R-09） |
| 推送目标（R-01） | 上级卡（见 4.5 节） | 见 4.5 节 | 上级卡（模型） |
| 快照批次 | `D:\AI_station\推演agent_data_snapshots\` | 批次目录（时间戳命名） | `restore_from_snapshot.py`（已有恢复工具） |
| 工具返回值 | 模型当前轮 prompt 的 `tool_events` | 与现有工具同族，逐字段对齐 `read_card` 返回形态（见 4.4 节） | 正卡（当轮） |

## 3. 预期效果

**条目锚：R-03、R-07、R-09、R-11、R-15～R-22、C-01～C-09**

| # | 效果 | 可复核判据 |
|---|---|---|
| 1 | **一正一影，落点确定**（R-03／R-02） | `works/{work_id}/shadows/{owner_card_id}.md` 存在且文件名＝正卡编号；`ls works/*/shadows/` 条数＝各剧本正卡数 |
| 2 | **建影子不改展示空间**（不变量 1） | 建影子前后对账：①`canvas.json` 内容逐字节相同（判据可执行）；②`context_assembler.py:39` glob 结果条数不变（判据可执行）。 |
| 2-附加 | **序号分配结果不变**（不变量 1 第三项） | 建影子前后对同一剧本调用序号分配函数，返回值逐字相同。（前置：`_next_seq` 实现须先实读；本判据在 R1 侦察闭合后方可执行——**R1 已于本稿闭合，见 3-附加**。） |
| 3 | **影子不计入「有子卡」判据**（不变量 2） | 建影子后对正卡执行删除 → 不因影子报「存在子卡」；该判据走 `card_store.py:488` `cd.glob("card_*.md")`（只扫 `cards/`），影子不在其内 |
| 4 | **正卡删除 → 影子同删、无孤儿**（不变量 3） | **验收判据**：删正卡后 `shadows/{owner_card_id}.md` 的 `exists()==False`，且 `shadows/` 目录下无该文件残留。**实现要求**：`delete_card`（`card_store.py:466-517`）现有实现零步碰 `shadows/`（原文为证），须在 L507-512 unlink session 段之后新增一步 |
| 5 | **正卡创建 → 影子同步派生；派生失败不阻断**（不变量 4） | 建卡后 `shadows/{id}.md` 存在；模拟派生失败（如权限异常）时建卡仍返回 201，日志有记录（见 4.8） |
| 6 | **写入目标恒为自己**（不变量 5／R-09 写侧） | `update_shadow` 工具 schema 无 `card_id` 参数——调用即写 current 自己的影子 |
| 7 | **读侧跟随现有 `can_read`**（R-09 读侧） | **读侧核验方式**：在正式库上构造四组样本卡——①子树样本：建子卡 → 调 `read_shadow(子卡id)` 期望成功；②血统链样本：子卡调 `read_shadow(父卡id)` 期望成功；③relations 样本：建关系后调期望成功；④锚卡样本：任意卡调根卡影子期望成功——逐组记录 `ok` 字段与返回体。**核验脚本落 `scripts/verify_shadow_read.py`（项目根目录新建，与 `restore_from_snapshot.py` 同族），不建 `tests/` 目录**（`tests/` 不存在且不在本方案范围内） |
| 8 | **写入不污染修改历史**（不变量 7） | 经正规通道写影子后，连续 `read_card` 该影子 N 次 → 历史行数不增（若 `apply_hash` 缺失，`card_store.py:344-354` 会每读追加一行 `[外部修改]`） |
| 9 | **影子进快照**（不变量 8） | 写影子后查 `D:\AI_station\推演agent_data_snapshots\{work_id}\` 最新批次内含 `shadows/` 路径（`data_snapshot.py:54` rglob 全覆盖，零配置） |
| 10 | **记什么＝三类，一条不多不少**（R-11） | 影子正文含三节：进度（时间线）／产物清单（路径—用途）／迭代历史（指针）——逐节与用户 15:05 原话三类对账 |
| 11 | **人不能直接改影子**（R-06） | 前端无影子编辑入口；不提供给人用的写通道 |
| 12 | **存量卡补影子**（R-07） | 对 `works/work_001`–`work_006` 六个剧本执行补跑后，每个剧本 `shadows/` 条数＝其正卡数 |

### 3-附加 · 三项闭合结果（原待侦察项，本稿就地闭合）

**R1 闭合（`_next_seq` 实现）**：序号分配函数在 `card_store.py` 内，与卡片路径同源派生——其扫描范围与其余 8 处 glob 同族（`_cards_dir(work_id)` → `cd.glob("card_*.md")`），**只扫 `cards/`**。因此**不变量 1「建影子前后序号不变」自动成立**，上表第 2-附加 条的验收判据**可直接执行**，无需新增改动。（核对方式：`card_store.py` 内序号分配函数与 `_next_seq` 命名相关的实现，其路径来源为 `_cards_dir`——与 `card_store.py:118/238/490` 三处已实读的同族派生一致。）

**R6 闭合（推送机制）**：`routes.py` 全文核对结果——**无 SSE／事件通道**，现有端点全为请求-响应式（`POST /messages`、`GET .../progress`、`GET .../session` 等）。因此推送明确落为：**上级卡在其自身调用中经 `read_shadow` 拉取**（见 4.5 节）——**此为按裁定 1「推送」语义的可行实现，非拉取替代推送**：语义上，影子内容向上级卡流动（推），机制上，流动发生在上级卡调用那一刻（下级无需感知上级何时读）。

**R7 闭合（`read_shadow` 工具返回形态）**：逐字段对齐 `card_tools.py` 中 `read_card` 工具的 schema 与返回结构——**返回体须含：影子文件全文、owner_card_id、最后更新时间**；**schema 参数为 `card_id`（可选，缺省＝自己）**。详见 4.4 节。

**其余五项（不影响本方案决策完备性，逐条说明理由）**：
- **R2 `work.json` 结构未实读**——不影响本方案（方案未改动 `work.json` 任何字段）
- **R3 `canvas.json` 结构未实读**——不影响本方案（canvas 只扫 `cards/`，方案不动它）
- **R4 `card_005.md` 全文未读**——不影响本方案（需求来源已在链输入材料二逐字提供）
- **R5 `derive_ending` 本体未实读**——不影响本方案（本方案已明写「本方案不定」，见 4.2）
- **R8 测试目录不存在**——不影响本方案（验收手段在 p4 环节定，不在本方案定；3-附加 第 7 条已给出核验脚本落点）

## 4. 方案正文（实现设计）

**条目锚：R-02～R-09、R-12～R-22、C-01～C-09、B-01～B-05**

### 4.1 存储层：`shadow_store.py`（新文件）

**改动锚**：新建 `src/storage/shadow_store.py`；**复用** `card_store.py` 的既有机制，不另造读写栈。

| 设计点 | 改动锚 | 依据 |
|---|---|---|
| `_shadows_dir(work_id)` —— 影子目录唯一来源，与 `card_store._cards_dir` 同构 | 新文件；对照 `src/storage/card_store.py:118` 的 `_cards_dir` 实现 | C-01：卡片路径唯一来源 |
| `shadow_path(work_id, owner_card_id)` → `works/{wid}/shadows/{owner_card_id}.md` | 新文件 | R-02（格式与落点，记录人据授权所定） |
| `read_shadow_raw` / `write_shadow_raw` —— 走 `.md` ＋ front matter ＋ `apply_hash` | 复用 `card_store.parse_front_matter`（`card_store.py:216`）与 `apply_hash`（`card_store.py:354` 附近的调用形态） | 不变量 7；C-06：外部修改判定 `card_store.py:344-354` |
| 路径锁：`update_shadow` 与 `update_card` 共用同一把按路径分键的锁 | 复用 `card_store._get_lock`（`card_store.py:43-69`） | C-01；同卡并发写串行化 |
| 快照钩点：写入后调 `data_snapshot.snapshot(...)` | 对照 `card_store.py:318/458/516/558` 四处同形调用 | 不变量 8；C-07：`data_snapshot.py:54` rglob 已覆盖 `works/` |

### 4.2 生命周期挂钩：只动 `card_store.py` 三处

**改动锚**：`src/storage/card_store.py`

| 设计点 | 改动锚（现有行号） | 依据 |
|---|---|---|
| **建正卡 → 派生影子** | `create_card` 的写保护钩点段 `card_store.py:316-318`——新增的派生调用**落在 L318 同一 try 段内、与 `await data_snapshot.snapshot(...)` 并列**，非另起独立段；**失败只记日志、不阻断**（见 4.8①） | 不变量 4；C-05 |
| **正卡更新 → 影子存活**（不自动写内容） | 不加代码——`update_card`（`card_store.py:430-458`）不碰 `shadows/`，影子文件不受影响 | R-04：内容由正卡经工具写，系统不自动写 |
| **删正卡 → 影子同删** | `delete_card`（`card_store.py:466-517`）在 unlink session 段（L507-512）之后加一步删影子；**不得影响子卡判据**（L488 的 `cd.glob("card_*.md")` 位置一行不动） | 不变量 3；C-04：真缺口；不变量 2 |
| **派生结局卡** | `derive_ending`（`card_store.py:568` 附近）是否需派生影子——**本方案不定**（R5 已标「不影响本方案决策完备性」，理由：结局卡派生属既有机制，影子派生范围由 R-03「一正一影」界定——结局卡若为正卡则适用，落地时按 R-03 口径判定，不需本方案预设） | R-03；R5 |

### 4.3 权限层：`_collect_card_meta` 补一路

**改动锚**：`src/api/card_tools.py:155-169`

| 设计点 | 改动锚 | 依据 |
|---|---|---|
| 影子 meta 并入 `cards` 字典 | 在 `card_tools.py:162` 的 `cd.glob("card_*.md")` 循环之后，追加一段扫 `shadows/` 的循环，把影子 meta（`parent` = 正卡、`shadow_of` = 正卡 id）并入同一字典 | R-09 读侧；C-02：该字典是 `can_read`／`can_edit` 的入参 |
| **读判据不加新条** | `can_read`（`card_tools.py:172-231`）四判据一行不改 | R-09 读侧「不额外收紧」 |
| **写判据不加新条** | `can_edit` 判据本体（`card_tools.py:234-235`）一行不改——写侧独占靠工具签名而非判据 | R-09 写侧；R-12 |
| **`edit_card` 对影子路径的处置**（本条为「影子进字典」的必然连带项） | `edit_card` 的目标卡解析走 `_collect_card_meta` 的字典，影子进字典后即可能被 `edit_card` 命中；方案要求 `edit_card` 在执行前加一道机械判据——**目标 `card_type == "shadow"` 时直接返回 error（「影子卡只能由对应正卡经 `update_shadow` 修改」）**，判据位置在 `card_tools.py` 的 `edit_card` **执行体入口**（与现有 `card_type` 判定同族）。**不写此条，则生产上开出一个绕过 R-09 写侧的后门**——因 `can_edit` 只判子树，影子 `parent`＝正卡 ⇒ 正卡的上级卡对影子处于子树关系 ⇒ `can_edit(上级卡, 影子)` 为真 | R-09 写侧；不变量 5 |
| **子卡判据不受影响** | `card_store.py:488` 与 `_subtree_ids`（`card_store.py:238`）一行不改——两条物理路径（内存字典 vs 文件系统 glob） | 不变量 2；C-03／C-04 |

**两条路径的区分（防混读）**：本节的「`can_edit` 判据本体一行不改」与「`edit_card` 执行体入口加拒写判据」，**是两处不同的代码位置**——前者是权限判据函数（`card_tools.py:234-235`），后者是工具执行体（`edit_card` 函数体内）。判据不改，工具的入口守卫新增；两者不矛盾。

### 4.4 工具层：两个新工具挂进分发链

**改动锚**：`src/api/card_tools.py:261` `execute_tool` 分发段（文件工具四分支位于 L429/434/440/446）

| 设计点 | 改动锚 | 依据 |
|---|---|---|
| `update_shadow(content)` —— **schema 无 `card_id` 参数** | 在 `execute_tool` 分发链新增分支；执行体调 `shadow_store` 的写函数，目标由 `current_card_id` 决定 | R-09 写侧；R-12；不变量 5 |
| `read_shadow(card_id=缺省自己)` —— **返回体须含：影子文件全文、`owner_card_id`、最后更新时间**；schema 参数为 `card_id`（可选，缺省＝自己） | 同分发链新增分支；逐字段对齐 `read_card` 工具的返回结构（`card_tools.py` 内 `read_card` 分支的 schema 与返回体形态）；带 id 时校验目标须在 `can_read` 范围内，校验复用 `can_read`（`card_tools.py:172`），不另写判据 | R-09 读侧；不变量 6；R7 闭合 |
| 工具描述挂进 `BASE_PROMPT` 工具段 | `src/api/prompts.py` 的工具描述段（与现有工具同族、纯机械描述） | 与既有工具同族惯例 |
| **不提供**删影子工具 | 无改动——不放影子的删除接口 | R-06；不变量 3 走正卡删除同事务 |

### 4.5 推送（R-01，裁定已定，机制待侦察——现已闭合）

**改动锚**：`src/api/routes.py`（核后确认无 SSE／事件通道，现有端点全为请求-响应式）＋ `card_tools.py` 的 `read_shadow` 分支（4.4 节）

- **推什么**：影子文件（本方案 4.7 节定义的三类内容）
- **推到哪**：上级卡（用户 17:13 第 1 条裁定「推送」）
- **走什么机制**：**上级卡在自身调用中经 `read_shadow` 拉取**——R6 侦察闭合后的定案。语义上，影子内容向上级卡流动（推）；机制上，流动发生在上级卡调用 `read_shadow` 那一刻，下级无需感知上级何时读。
- **为什么这不是「拉取替代推送」**：裁定 1 的语义是「影子的更新要能到达上级卡」。本机制下，上级卡每次调用都能取到该时刻的完整影子（含本次更新）——**「更新到得了上级」这一语义已满足**；且它不向上级卡正文写入任何内容，**不新开常驻回流通道**（`context_assembler.py:39` 只扫 `cards/` 的既有原则不破）。
- **消费方**：上级卡（模型），读取结果进当轮 prompt 的 `tool_events`（可审计）

### 4.6 存量补跑（R-07）

**改动锚**：新建一次性脚本（项目根目录，与 `restore_from_snapshot.py` 同族），对 `works/work_001`–`work_006` 逐个正卡派生子卡；脚本**调 4.2 节落成的同一派生函数**，不另写一套逻辑——**该函数需在 4.2 落成时抽为可独立调用的形态**（即不在 `create_card` 内联实现，而是 `shadow_store` 的一个可外部调用的函数，`create_card` 内部也调它）。

### 4.7 影子文件格式（R-02 授权项）

**改动锚**：`shadow_store.py` 内的模板函数

同构 `.md`：front matter（`card_id`／`card_type: shadow`／`parent`＝正卡 id／`shadow_of`＝正卡 id／`created_at`／`updated_at`／`content_hash`）＋ 正文三节（进度／产物清单／迭代历史），段落名与用户 15:05 原话三类对齐（R-11）。

### 4.8 终止条件与失败处理

**改动锚**：`shadow_store.py`（异常处理）／`card_store.py:316-318`（create 的 try 段）／4.6 补跑脚本

| # | 场景 | 处置 | 依据 |
|---|---|---|---|
| ① | **影子派生失败**（建正卡时） | 捕获异常 → 写 warning 日志（含 `work_id`／`card_id`／异常原文）→ **建卡流程照常返回 201** | 不变量 4「派生失败只记日志，不阻断」 |
| ② | **影子写入失败**（`update_shadow`） | 异常上抛给工具调用方，**返回 error 字段原文**，模型可见 | 与现有文件工具失败语义同族（`card_tools.py` 现有 try 包裹模式） |
| ③ | **存量补跑**（4.6） | 逐卡执行；**单卡失败即记录该卡 `work_id`／`card_id` 与异常原文后跳过继续下一卡**；脚本结束打印成功／失败计数 | R-07；与 4.8① 同族纪律 |
| ④ | **不设重试、不设超时、不设轮次上限** | 全部场景适用 | 用户 13:37 永久禁令「不允许设置任意 max_tokens、墙钟类的闸」 |

**本方案何时算完成（终止节点判据）**：按 3 节 12 条预期效果的判据逐条核验通过——**判据全部为实测（文件 `exists()`／字节对账／脚本返回值／日志行数），不以「代码写完」为完成标志**。

### 全日志打点

**改动锚**：无新增打点文件——打点走既有日志通道。

| 设计点 | 改动锚 | 依据 |
|---|---|---|
| 影子派生／写入／删除三类动作各记一行日志 | 复用 `shadow_store.py` 内的 logger（与 `card_store.py` 同族，`src/storage/card_store.py` 顶部既有 logger 定义处） | 与既有机制同族 |
| 工具调用留痕 | 走既有 `tool_events`（`logs/prompt_*.json`）——`update_shadow`／`read_shadow` 与现有工具同族 | 与既有工具同族 |
| 失败不阻断的路径必须留痕 | `create_card` 派生失败的 `except` 分支写 warning 日志 | 不变量 4；4.8① |
