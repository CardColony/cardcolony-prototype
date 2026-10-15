# 画布 UI 重构方案书

- **日期**：2026-10-15
- **版本**：v2（自核修订版）
- **状态**：待审
- **范围**：① 位置数据并入卡片库（废除"手拖信息专门存放点"）　② 渲染机制从"全量重建"改"增量更新"

---

## 一、现状事实（全部已实测核实，2026-10-15）

### 存储层

| 项 | 现状 | 证据 |
|---|---|---|
| `layout.json`（手拖位置落点） | 全库 13 个 work **0 个**——10-11 搬存储后，迁移脚本 `migrate_canvas_positions.py` 的 `--apply` **从未执行过** | 实测 glob 全库 |
| `canvas.json`（老载体） | positions 已被代码清空，仅 work_004/005 残留空 `{}` 尾巴 | 实测 13 库 |
| `cards.db` | 5 张表（cards/card_history/inbox/meta/shadows），cards 表 **24 列，无任何位置字段** | 实测 work_001~004 |
| 位置读写链路 | 前端 `persistCardPos`（canvas.js L1414，防抖 500ms）→ `API.patchPositions` → 后端 `merge_positions`（canvas_store.py L189）→ `save_layout`（L241）写 layout.json；**拖动即刻标 `pinned:true`** | 读码核实 |
| 后端调用点 | `routes.py` **3 处**：L463（nav 返回 positions）、L471（patchPositions）、L899（默认 work positions）；`canvas_store.py`：`merge_positions` L189 / `load_layout` L220 / `save_layout` L241，L43-48 注释明确"端点与 merge_positions 签名原样——前端零改动是拆存储的边界条件"，L100 注释"用户坐标的唯一真相来源是 layout.json" | grep + 读码 |

### 渲染层（canvas.js 当前稳定版，**1685 行全 LF**）

| 环节 | 现状 | 证据 |
|---|---|---|
| 展开/折叠 | `toggleExpand`（L1018）只翻 `expanded` 集合 → **调 `renderAll()`** | 读码 |
| 全量重建 | `renderCanvas`（L768）先 `node.remove()` **删光全部节点** → 按可见集重建全部 | 读码 |
| 布局计算 | `computeProjection`（L663）→ `layoutTree`（L363 全树后序测高/前序放置）→ `separateOverlaps`（L474 全局避让） | 读码 |
| 已有快路径 | `reflowLayout`（L716）**只挪位置、不重建 DOM**——拖拽落定走它；但展开/折叠没走它 | 读码 |

**结论**：用户观察"展开一张卡、全场重排"属实，且实际比"重算位置"更重——**DOM 全销毁重建 + 全树布局 + 全局避让**。病根是结构性耦合（布局全树现算 + 避让全局连锁），不是实现瑕疵。

---

## 二、方案一：位置数据并入卡片库

**设计原则**（用户裁决 2026-10-15 10:51）：位置是**卡片自己的属性**，不是"手拖信息"这种特殊东西。全项目位置数据只有一个来源——卡片本身。

### 1. cards 表加 3 列

```
pos_x      REAL     -- 画布 X 坐标（NULL = 未摆过，走自动排）
pos_y      REAL     -- 画布 Y 坐标（同上）
pinned     INTEGER  DEFAULT 0  -- 1 = 用户亲手摆过，布局算法不得移动
```

SQLite `ALTER TABLE cards ADD COLUMN` 三条，存量卡默认 NULL/0（未摆过）。

### 2. 读写切换

- **读**：加载卡片时 `SELECT` 直接带出位置，**不再单独读 layout.json**；
- **写**：`patchPositions` 改 `UPDATE cards SET pos_x/pos_y/pinned WHERE card_id`；
- **删**：卡片删除，位置**同存同灭**（天然满足，无需额外清理）；
- **未摆过的卡**：`pos_x/pos_y = NULL`，自动排每次现算、不落盘（用户确认的边界）。

### 3. 废除与清理

- `canvas_store.py` 的 `load_layout`/`save_layout` 及 layout.json 读写整条链路删除；
- `routes.py` L463/L471/L899 三处调用点改走 cards 表读写；
- `scripts/migrate_canvas_positions.py` **归档**（留着会误导后人，以为迁移是当前职责）；
- `canvas.json` 的 positions 残留尾巴清掉，`canvas_store.py` 的不变量注释同步改（L100"layout.json 是唯一真相来源"改为"cards 表是唯一真相来源"）。

### 4. 接口兼容

`patchPositions` 的请求/响应结构**不变**——前端 API 层零改动，只换存储后端。

---

## 三、方案二：渲染机制改"增量更新"

**目标**：展开/收起一张卡，代价 = **O(该卡子树)**，不是 O(全场)；用户摆好的卡**任何操作下不动**。

### ① 增量 DOM

展开/折叠不再走 `renderAll` 全删全建，改走新入口 `renderSubtree(cardId)`：只创建/移除被操作卡的子孙节点，其余节点 DOM 引用**原样保留**（DOM 不重建 = 事件绑定、内部状态全保留）。基础直接借现有的 `reflowLayout` 快路径（只挪位置不重建 DOM），扩展它而非另起炉灶。

### ② 布局单元化

把"全树单次现算"改为"**子树单元制**"：

- 每张已展开的卡，其子树是一个**独立布局单元**，单元内部自算；
- 展开卡 C：只重排 **C 的单元内部**（C 的位置不动，C 的子卡在 C 的子树区域内排布）；
- 收起卡 C：C 的单元折叠，C 自身位置不动；
- 初始加载时才做一次全树排布（表现与现状一致），此后**任何单卡操作都不触发全场重算**。

### ③ 避让局部化 + pinned 保护

`separateOverlaps` 从"全局避让"改为：

- 只对**新增/移动节点**做局部重叠消除；
- **pinned 卡（用户摆过的）是绝对锚点，任何算法不得移动它**；局部避让撞上 pinned 卡时，让新增节点绕行，而不是推走锚点。

### 效果对比

| 操作 | 现在 | 改后 |
|---|---|---|
| 展开一张卡 | 删光全部 DOM + 全树重排 + 全局避让 | 只建该卡子孙节点 + 只排该子树 + 局部避让 |
| 收起一张卡 | 同上（全量） | 只移除子孙节点，其他不动 |
| 拖一张卡 | 快路径（保留） | 不变 |
| 用户摆好的卡 | 可能被全局避让推走 | **任何操作下绝对不动** |

---

## 四、改动清单

| 文件 | 改动 |
|---|---|
| `static/canvas.js` | `toggleExpand` 改走增量路径；新增 `renderSubtree`；`layoutTree` 支持子树单元布局；`separateOverlaps` 局部化 + pinned 保护；`persistCardPos` 保留（接口不变） |
| `src/storage/canvas_store.py` | 删 `load_layout`/`save_layout`；`merge_positions` 改走 cards 表；不变量注释改"cards 表是唯一真相来源" |
| `src/api/routes.py` | L463/L471/L899 三处调用点改走 cards 表读写（接口结构不变） |
| 数据库迁移脚本（新增） | 13 个 work 的 cards.db 各加 3 列，存量默认 NULL/0 |
| `scripts/migrate_canvas_positions.py` | 归档（移入 `scripts/archive/` 或加"已废弃"头注） |
| `static/canvas.json` 相关（`canvas_store.py` 内） | 清 positions 残留尾巴，不变量同步 |
| 测试 | 位置读写单测（后端）；pinned 保护测试；渲染行为测试（若前端可测） |

---

## 五、验收标准（全部可测）

1. 展开卡 A：A 的兄弟/祖先/无关卡 **DOM 不重建、位置不变**（用节点引用对比验证）；
2. 收起卡 A：仅 A 的子孙节点移除，其余不动；
3. 拖动卡 → 刷新页面 → 位置保留（这次从 cards 表读出）；
4. pinned 卡在展开/收起/拖其他卡时**位置绝对不变**；
5. 初始加载全树排布一次，表现与现状一致；
6. `patchPositions` 接口结构不变，前端 API 层零改动；
7. 全库不再生成新的 layout.json；canvas.json 不再写 positions；
8. 项目全量测试绿。

---

## 六、风险与边界

### 风险 1（必须处理）：static/ 存在外部施工点

- 事实：`static/` 下 11 个文件相对 HEAD 有未提交改动（88+/50-，其中 canvas.js 12 行）；`nav.css` 的 mtime 在 **2026-10-15 11:18:36** 仍被写入——**外部会话/进程此刻仍在动 static/**。
- 处置：实施前必须 `git diff` 对齐 + 与该施工点确认停手，否则会覆盖他人改动。
- 注：此前"canvas.js 从 1682 行变成 1452 行（-230 行）"的说法**系测量错误**（文件实际 1685 行全 LF，"1452"为按 CRLF 数行的错值；git diff 证实 canvas.js 相对 HEAD 仅 12 行改动）。真实风险是"外部在写"，不是"文件被大改"。

### 风险 2：cards 表加列是核心表结构变更

迁移脚本需兼容全部存量库（13 个 work），且 `merge_positions` 需处理"position 指向已删除卡片"的残留（写时按 card_id 存在性过滤）。

### 边界

- 本次只改机制，不做视觉美化；
- 自动排的参数（间距/层级缩进）不在本次调整范围；
- 不做拖拽动画增强；
- 老的手拖数据不迁移、不恢复（用户裁决 2026-10-15 10:38）。

---

## 七、实施顺序

1. **存储层先行**：迁移脚本 → 后端读写切换 → 测试（方案一闭环，可独立验证）；
2. **渲染层**：增量 DOM → 子树布局 → 避让局部化 → 测试（方案二闭环）；
3. **联调回归**：全量测试 + 手工验证 8 条验收标准。

---

## 附：决策记录

| 日期 | 决策 |
|---|---|
| 2026-10-15 10:38 | 用户裁决：老的手拖信息不需要了，老存放点也不需要了；位置数据全项目只有一份，手拖也是编辑那个位置 |
| 2026-10-15 10:51 | 用户确认：位置数据进卡片库；渲染"展开全场重排"机制粗糙，需要更合理逻辑 |
| 2026-10-15 11:01 | 用户拍板：按方案书方向出方案 |
