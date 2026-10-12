# card_045 · 画布统一坐标来源（第一步·数据流收口）实施方案

状态：方案草案，未动代码。
落点文件：`推演agent/static/canvas.js`（唯一改动文件）
依据：上级定论 D01–D07（见第一节）。本文只写怎么做，不替代定论。

---

## 一、依据的定论（摘录，原文见上级卡）

- D01 `positions` 是唯一需要存盘的坐标真相；拖拽、整理画布、建卡只写它。
- D02 删除 `state._baseLayout`。`_layout` 改为派生值，每次变更整份重算；`persistCardPos` 不再写 `_layout`。
- D03 避让结果不回写 `positions`，只影响当次显示。
- D04 pinned 卡不参与避让，位置完全由 `positions` 决定。
- D05 布局函数目标形态：`layout(树, positions, expanded, sizes) → 坐标表`，只读输入、不写 `state`、不碰 DOM。
- D06 不新增第二份缓存修补回弹。
- D07 `persistCardPos` 保留写 `positions` 与存盘，只删对 `_layout` 的补丁。

## 二、现状（已读码核实，行号以读码时为准，动手前必须重核）

| 位置（约） | 现状 | 问题 |
|---|---|---|
| `reflowLayout` L512–526 | 基准取 `state._baseLayout`，避让后写 `state._layout`，回写 DOM `left/top`，调 `renderEdges` | 基准是旧快照，拖动结果被覆盖（回弹根源） |
| `renderCanvas` L574–578 | 先清空 DOM，再 `computeLayout` 并赋值 `state._baseLayout` | 算布局时 DOM 为空，只能用回退尺寸 220/86、深层 36 |
| `persistCardPos` L1264–1273 | 写 `positions`、写 `_layout[cid]`、防抖存盘 | `_layout` 双写 |
| `onDragUp` L756–757 | 存位置后用 `state._layout` 画连线 | 删除 `_layout` 写入后会读到拖动前旧坐标 |
| `resolveDrop` L1212 | 松手后把位置推到不相交空档，再经 `persistCardPos` 存为 pinned | 系统推开的坐标被写成用户意图（违反 D03） |
| `computeLayout` L127–155 | 直接读 `state.expanded`（L154），经 `layoutTree` 调 `measureSizes()`（L327，读 `nodeMap` 与 DOM） | 非纯函数，违反 D05 |
| `separateOverlaps` L427–470 | 只读传入参数，pinned 双卡跳过，单 pinned 只推非 pinned | 符合 D04/D05，不需改语义 |
| `_layout` 读取者 | 仅 `onDragUp` L757 | 其余为写入点（L517、L631、L1267–1268） |

未核实：
- D01 所说“整理画布”只写 `positions`，尚未找到对应代码，动手前必须定位。
- T18 在航改动可能使行号偏移，动手前按当前版本重新核对。

## 三、目标形态

1. 单一真相：`state.positions`（存盘）。
2. 派生值：`state._layout` 由一个函数整份生成，任何变更后整份重算，不做局部补丁。
3. 布局函数：`layout(树, positions, expanded, sizes) → 坐标表`，`expanded` 与 `sizes` 由调用方传入，函数内不读 `state`、不碰 DOM。
4. 尺寸：采用实测尺寸（甲）。布局在 DOM 已就绪时计算，`sizes` 从 DOM 量取后传入。
5. 显示与存储分离：避让后的坐标只用于显示，不写回 `positions`。
6. 松手为唯一写入点：`onDragUp` 只存用户松手的原始坐标，之后调用一次 `reflowLayout()` 统一刷新 `_layout`、DOM 与连线。

## 四、实施步骤

1. **重核**：按当前版本重新读取 `computeLayout`、`layoutTree`、`measureSizes`、`separateOverlaps`、`reflowLayout`、`renderCanvas`、`persistCardPos`、`onDragUp`、`resolveDrop`，逐段标注对 `state` 与 DOM 的读写；定位“整理画布”的写入代码。
2. **布局函数纯化**：`computeLayout` 去掉对 `state.expanded` 的直接读取，改为参数；`layoutTree` 去掉 `measureSizes()` 调用，改为读传入的 `sizes`；同步更新所有调用点。`t07SwitchLog` 之类日志留原处，不影响结果。
3. **新增派生入口** `computeBaseLayout()`（或等价命名）：从 `state.positions`、`state.expanded`、当前 DOM 量取的 `sizes` 生成坐标表。`reflowLayout` 与 `renderCanvas` 都只走它。
4. **删除 `_baseLayout`**：删 L513 读取、L578 写入，删除该字段。
5. **`reflowLayout`**：基准改为 `computeBaseLayout()`，避让、回写 DOM、`renderEdges` 逻辑保持。避让结果只进 `_layout` 用于显示。
6. **`persistCardPos`**：删 L1267–1268 对 `_layout` 的写入，保留 `positions` 写入与防抖存盘。
7. **`resolveDrop`**：仲裁仍可算出不重叠的显示位置，但存盘只用用户松手的原始坐标。需要把“存的坐标”和“显示的坐标”在此分开。
8. **`onDragUp`**：存松手坐标后调用 `reflowLayout()`，替换 L757 的 `renderEdges(..., state._layout)` 直接调用。
9. **检查 `renderCanvas`**：`_layout` 只能来自整份重算。
10. **清理**：删除因此失效的引用，不改渲染与样式。

## 五、验收标准

自动：
- `pytest tests/ -q` 失败数不高于改前基线（改前先跑一次记录基线）。
- 代码中 `_baseLayout` 无任何引用；`_layout` 只有派生赋值。
- `persistCardPos` 中无 `_layout` 写入。

浏览器（本环境无浏览器，需由你或测试环境执行）：
1. 拖动卡 X 到新位置，松手。控制台比对 `positions[X]` 与 DOM 位置一致。
2. 触发 LOD 切换、展开/折叠、摘要回填（悬停未缓存卡），X 位置不变。
3. 刷新页面，X 位置保持。
4. pinned 卡不被相邻卡挤开。
5. 拖动后连线立即端点正确。
6. 非 pinned 卡排布与改前截图对比，允许因实测尺寸产生的细微变化，需逐卡记录差异。
7. 整理画布结果与改前一致或差异可解释。

## 六、风险与未闭合项

- 排布变化：实测尺寸替代回退值，非 pinned 卡间距会变（已选甲，接受）。
- 行为结论为读码推断，尤其“回弹”现象，需浏览器复现确认。改前若无法复现，需说明改动是为消除双写而做，而非修复已确认的故障。
- 与 T18 在航改动可能冲突，需先合并或协调。
- “整理画布”写入路径未找到，可能破坏 D01 的“只写 `positions`”假设。
- 行号均为读码时记录，不可直接沿用。

## 七、不在本步范围

- pinned 双卡相交的避让规则（另立工单）
- “拖一下即永久钉住”的 pinned 语义（另卡，与 card_044 相关）
- 渲染、样式、存储拆分（canvas.json）、框架选型
- 化石清理（card_034）
- `computeLayout` 完全可注入化（留给 card_014）

## 八、回滚

单文件 `static/canvas.js` 回退即可；无数据迁移，`positions` 持久格式不变。
