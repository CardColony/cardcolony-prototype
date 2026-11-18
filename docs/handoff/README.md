# CardColony 前端交接包

给前端实现的完整交接材料。真源是单文件可执行原型
`docs/prototype-canvas-page-ui-devin-v4-paper.html`（浏览器直接打开即可跑），
本包所有数值/截图均由它实测导出，非估计值。

## 内容

| 文件 | 内容 |
|---|---|
| `visual-spec.md` | 视觉规范：深浅双主题令牌、色板语义、字体、间距、圆角、阴影、动效 |
| `pages-and-interactions.md` | 页面骨架、两视图结构、左列表/画布/对话页、全部交互与状态机 |
| `assets/logo.svg` | CardColony 品牌标（顶栏 22px 用） |
| `assets/icons/` | 图标 SVG 集：类型 7 枚 + 左栏 9 枚（16px 视窗、currentColor 描边） |
| `assets/slices/` | UI 元素切图 PNG（@2x）：整页 + 各元素/状态裁片 |
| `HANDOFF-NOTES.md` | 对接注意点：mock 与真实口径的差异、真实现文件映射 |

## 切图清单（assets/slices/）

**整页**：`page-canvas.png` 画布｜`page-canvas-nav.png` 列表推出｜
`page-chat.png` 对话页｜`page-canvas-dark.png`/`page-chat-dark.png` 深色

**结构件**：`el-topbar.png`/`el-topbar-chat.png` 顶栏两态｜`el-brand-zone.png` logo 列头｜
`el-nav-list.png`/`el-nav-top.png`/`el-nav-phead.png`/`el-nav-current.png` 左列表｜
`el-nav-review-row.png`/`el-nav-align-row.png`/`el-nav-fn-row.png` 场务行｜
`el-drawer.png` 对话抽屉｜`el-bookmark-rail.png` 右缘书签｜`el-legend.png` 图例

**画布卡**：`el-card-master.png`/`el-card-manager.png`/`el-card-tok.png` 三级+执行卡｜
`el-card-running.png`/`el-card-blocked.png`/`el-card-paused.png`/
`el-card-queued.png`/`el-card-done.png` 五状态｜`el-venue-badges.png` 场务签

**对话件**：`el-composer.png` 输入卡｜`el-msg-user.png` 用户泡｜
`el-msg-assistant.png` 卡回复｜`el-msg-cmsg.png` 卡间信｜
`el-step-row.png`/`el-think.png`/`el-date-divider.png` 过程件｜
`el-fwin-review.png` 评审窗｜`el-fn-panel.png` 职能档案

## 设计一句话

多层级卡树 = 分工；全局透明 = 一切可见可介入；黄 = 卡的实体；
平级面板 = 同层同底不遮盖；金 = 唯一的细笔强调；蓝 = 发送。
