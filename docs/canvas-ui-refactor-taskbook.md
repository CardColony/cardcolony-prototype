# 任务书：画布 UI 重构（位置入库 + 渲染增量）

- **日期**：2026-10-15
- **来源方案**：`docs/canvas-ui-refactor.md`（v2 自核修订版，已落盘）
- **目标**：① 卡片位置数据并入 cards 表，废除 layout.json"手拖信息专门存放点"；② 渲染机制从"全量重建"改为"增量更新"——展开/收起一张卡只动其子树，用户摆好的卡任何操作下不动。
- **执行前提（未满足不得开工）**：`static/` 外部施工点（最近写入 10-15 11:18:36 nav.css）已确认停手，且 git diff 已对齐。

---

## 一、已核实的前置事实（执行者以此为基准，不再重复勘查）

### 存储层
| 项 | 现状 | 锚点 |
|---|---|---|
| layout.json | 全库 13 个 work 0 个（迁移脚本 --apply 从未执行） | 实测 glob |
| canvas.json | positions 已清，仅 work_004/005 残留空 `{}` | 实测 13 库 |
| cards.db | 5 表（cards/card_history/inbox/meta/shadows）；cards 表 24 列无位置字段 | 实测 work_001~004 |
| 写链路 | 前端 persistCardPos（canvas.js L1414，防抖 500ms）→ PATCH /works/{id}/canvas/positions → routes.py L471 `merge_positions` → canvas_store.py L200 `save_layout` 写 layout.json；拖动即刻 pinned:true | 读码 |
| 读链路 | routes.py L463（nav 返回 positions）、L899（默认 work positions）都走 `load_layout`（canvas_store.py L220） | grep 实证 |

### 渲染层（canvas.js 1685 行全 LF，mtime 10-15 10:50:36）
| 函数 | 行号 | 职责 |
|---|---|---|
| layoutTree | L363 | 全树后序测高/前序放置 |
| separateOverlaps | L474 | 全局避让（按 y 分带、带内逐个挪） |
| computeProjection | L663 | 布局入口：layoutTree → separateOverlaps |
| reflowLayout | L716 | 快路径：只挪位置不重建 DOM（拖拽落定走它） |
| renderCanvas | L768 | 全量重建：先删光全部节点再重建 |
| toggleExpand | L1020 | 展开/折叠：翻 expanded 集合 → renderAll() |
| persistCardPos | L1414 | 拖动持久化入口 |
| renderAll | L1594 | 全量重绘入口 |

### 相关测试文件（现有）
- `tests/test_canvas_layout_store.py`（10-11）——layout.json 存储测试，**方案一落地后需改**
- `tests/test_card045_canvas_store.py`（10-11）——canvas_store 相关测试，**需改**
- `tests/canvas_layout.test.mjs`（10-11）——前端布局测试（node .mjs），**需扩展**

### 迁移脚本
- `scripts/migrate_canvas_positions.py`（10-11）——**本次归档对象**（--apply 从未执行，使命已终结）

---

## 二、任务清单

### 阶段 0：前置对齐（阻塞项）
- [ ] **0.1** 与 `static/` 外部施工点确认停手（11 个文件未提交改动：canvas.js/workspace.js/stream.js/nav.css/esm/* 等）。**未确认前不进入阶段 1/2。**
- [ ] **0.2** 备份现状：`git diff > /tmp/static_external.diff` 留档（防覆盖后无法还原外部改动）。

### 阶段 1：存储层（方案一，可独立验证闭环）

- [ ] **1.1 数据库迁移脚本**（新增 `scripts/migrate_cards_add_position_columns.py`）
  - 动作：遍历 `works/*/cards.db`，对每库执行幂等 ALTER：
    - `ALTER TABLE cards ADD COLUMN pos_x REAL`（若 `PRAGMA table_info(cards)` 无 pos_x 列）
    - `ALTER TABLE cards ADD COLUMN pos_y REAL`（同上判据）
    - `ALTER TABLE cards ADD COLUMN pinned INTEGER DEFAULT 0`（同上判据）
  - 完成标准：13 库全部含 3 列；**重复执行零副作用**；存量卡 pos_x/pos_y=NULL、pinned=0。

- [ ] **1.2 canvas_store.py 读写切换**
  - `merge_positions`（L189）：由 `save_layout` 委托改为直接 `UPDATE cards SET pos_x=?, pos_y=?, pinned=1 WHERE card_id=?`（按 work_id 定位库文件）；**position 指向不存在卡片时按 card_id 存在性过滤，不写脏行**（方案书风险 2）。
  - `load_layout`（L220）/`save_layout`（L241）：删除；新增长 `load_positions(work_id) -> dict[card_id, {x,y,pinned}]`（`SELECT card_id, pos_x, pos_y, pinned FROM cards WHERE pos_x IS NOT NULL`）。
  - 不变量注释：L100"用户坐标的唯一真相来源是 layout.json"改为"cards 表是唯一真相来源"；L5/L43 的接口边界注释保留（前端零改动仍成立）。
  - 完成标准：canvas_store.py 不再出现 layout.json 读写；全部调用点编译通过。

- [ ] **1.3 routes.py 三处调用点改走 cards 表**
  - L463：`data["positions"] = canvas_store.load_positions(work_id)`
  - L471：`return canvas_store.merge_positions(work_id, req.positions)`（签名不变，内部已换存储）
  - L899：`"positions": canvas_store.load_positions(DEFAULT_WORK_ID)`
  - 完成标准：PATCH /positions 与 GET /nav 的响应结构**逐字段不变**。

- [ ] **1.4 清理**
  - `scripts/migrate_canvas_positions.py` 移入 `scripts/archive/`，头部加"已废弃（2026-10-15）：位置数据已并入 cards 表，本脚本使命终结，勿再执行"。
  - canvas.json 的 positions 残留尾巴：work_004/005 清掉（load/rebuild 路径已不写 positions，仅存量残留；清理即删键）。
  - 完成标准：全库无 layout.json；canvas.json 无 positions 键；无任何代码路径再写 layout.json。

- [ ] **1.5 测试**
  - 改 `tests/test_canvas_layout_store.py`、`tests/test_card045_canvas_store.py`：layout.json 断言全部改为 cards 表断言。
  - 新增：位置读写往返（写→读→刷新等价）；pinned 置位；指向已删卡的 position 被过滤；迁移脚本幂等（跑两次）。
  - 完成标准：阶段 1 相关测试全绿。

### 阶段 2：渲染层（方案二，可独立验证闭环）

- [ ] **2.1 增量 DOM 入口**：新增 `renderSubtree(cardId)`——只创建/移除该卡子孙节点，其余节点 DOM 引用原样保留；`toggleExpand`（L1020）由 `renderAll()` 改为调 `renderSubtree(cardId)`。
  - 完成标准：展开卡 A 时，A 的兄弟/祖先/无关卡节点引用不变（同一 DOM 对象）。

- [ ] **2.2 子树单元布局**：`layoutTree`（L363）支持"以指定卡为根的子树单元"模式——展开卡 C 只重排 C 单元内部（C 自身位置不动，子卡在 C 子树区域内排布）；收起只折叠 C 的单元；初始加载仍全树排布一次（表现与现状一致）。
  - 完成标准：展开/收起任何单卡，不触发全树重排。

- [ ] **2.3 避让局部化 + pinned 保护**：`separateOverlaps`（L474）由全局避让改为只对新增/移动节点做局部重叠消除；**pinned 卡是绝对锚点，任何算法不得移动**——局部避让撞上 pinned 卡时让新增节点绕行。
  - 完成标准：pinned 卡在展开/收起/拖动其他卡时位置绝对不变。

- [ ] **2.4 前端测试**：扩展 `tests/canvas_layout.test.mjs`——增量 DOM（节点引用不变）、子树局部布局、pinned 保护三条用例。
  - 完成标准：`node tests/canvas_layout.test.mjs` 通过。

### 阶段 3：联调回归

- [ ] **3.1 全量测试**：项目全量 pytest 绿（基线 630 tests collected，MOCK_LLM=1）。
- [ ] **3.2 手工验收**：浏览器实测 8 条验收标准（见下）。**注意：本仓此前无浏览器实测历史（10-04~10-11 画布改造均未在真实界面见证），本次必须人机配合完成，不许跳。**
- [ ] **3.3 提交**：按项目 git 规矩提交代码（**works/ 不进 git**；提交前确认外部施工点改动已合入或分隔）。

---

## 三、验收标准（全部可测，直接对账）

1. 展开卡 A：A 的兄弟/祖先/无关卡 **DOM 不重建、位置不变**（节点引用对比验证）；
2. 收起卡 A：仅 A 的子孙节点移除，其余不动；
3. 拖动卡 → 刷新页面 → 位置保留（从 cards 表读出）；
4. pinned 卡在展开/收起/拖其他卡时**位置绝对不变**；
5. 初始加载全树排布一次，表现与现状一致；
6. `patchPositions` 接口结构不变，前端 API 层零改动；
7. 全库不再生成新的 layout.json；canvas.json 不再写 positions；
8. 项目全量测试绿。

---

## 四、约束（不可违反）

1. **外部施工点未停手不动 static/**（阶段 0 是硬闸门）。
2. 老的手拖数据**不迁移、不恢复**（用户裁决 2026-10-15 10:38）。
3. `patchPositions` 请求/响应结构**逐字段不变**，前端 API 层零改动。
4. 位置是卡片自身属性：**未摆过的卡 pos_x/pos_y=NULL，自动排每次现算、不落盘**（用户确认边界）。
5. 本次只改机制：**不做视觉美化、不改自动排参数、不做拖拽动画增强**。
6. 遵守 `VERSIONING.md` 三条禁令：禁对承载运行数据的目录做 git 分支操作；works/ 不进 git；环境与版本动作先取证再动手。
7. 迁移脚本必须幂等，兼容全部 13 个存量库。

---

## 五、交付物

1. `scripts/migrate_cards_add_position_columns.py`（新，幂等）
2. `src/storage/canvas_store.py`（改：cards 表读写，删 layout.json 链路）
3. `src/api/routes.py`（改：L463/L471/L899 三处调用点）
4. `static/canvas.js`（改：renderSubtree + 子树单元布局 + 避让局部化 + pinned 保护）
5. `scripts/archive/migrate_canvas_positions.py`（归档）
6. 测试文件（改 2 + 新 1 + 扩展 1）
7. 实施记录：每阶段完成后回报（做了什么/验证了什么/卡在哪），三阶段齐后出联调报告。

---

## 六、风险提示（执行者须知）

- **外部施工点活跃**：nav.css 最近写入 10-15 11:18:36——施工点可能随时再动 static/，阶段 2 全程需保持警惕，动文件前先查 mtime。
- **cards 表是核心表**：加列是结构变更，迁移前先备份（`backup_works.py` 或等价手段）。
- **前端无实测史**：渲染层改动必须由人机配合在真实浏览器验证，不能只靠单测交差。
