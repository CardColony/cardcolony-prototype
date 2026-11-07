# 画布页视觉设计稿 v1 · 截图清单（待补 PNG）

目标文件：`推演agent/docs/prototype-canvas-page-design-v1.html`（file:// 直开，零外链）。
本目录目前**只有这份清单，没有 PNG**——原因见文末「为什么空着」。

## 需要的 8 张

| 文件名 | 怎么出 |
|---|---|
| `00-dark-full.png` | 打开页面（默认深色、54% 适配）→ 直接截全景 |
| `01-light-full.png` | 点顶栏 ☀/☾ 切浅色 → 全景 |
| `02-nodes-states-dark.png` | 深色下缩放到 100%（点 + 数次）→ 截画布上半：运行中/已完成/受阻/已暂停/排队中 各一张卡同屏 |
| `03-lod-four.png` | 54%（far）／64%（near）／84%（full）／104%（ultra）四档各截一张卡的特写 |
| `04-bookmarks.png` | 右缘书签组三段相接（事件·规则卡·归档）+ 打开「规则卡」后的浮窗 |
| `05-toolbar.png` | 顶栏（缩放组 −/百分比/+/适配、⌕、⋯、☀、◐ 设计说明）+ 底部全局模型条七格 |
| `06-fourstates.png` | 演示 ▾ 打开四态面板，依次截 正常 / 加载 / 空 / 错误 / 无权限（重点：加载 vs 空） |
| `07-design-panel.png` | ◐ 设计说明 → 令牌表 + 决策页 + 自检页 + 待裁定页 |

## 复现步骤（有 shell 的卡照做即可）

```bash
# 1) 起一个静态服务（或直接 file:// 打开，页面零外链）
# 2) 用 headless Chrome 截图，视口 1280×800、deviceScaleFactor=2
# 3) 深/浅两套用 --evaluate 切 data-theme：
#    document.documentElement.dataset.theme='light'
# 4) 缩放用页面按钮 #zoomIn / #zoomOut / #fitBtn，百分比读 #zoomPct
```

## 为什么这里空着

本卡（UI 卡）没有 bash/python，文件工具可见范围只到 `trading-workspace` 与 `推演agent` 两棵树；
而 browser 的截图一律落 `works/work_027/browser_sessions/b_card_464/artifacts/`，
`move_file` 看不见那个目录（已实测：`not_found`）。

所以：**PNG 需要由能跑命令的 worker 卡从上面那个 artifacts 目录复制进来**，
或由它按上表自行 headless 截图。已产出的会话内截图（未搬运）：`shot_002.png` 等若干，均在上述 artifacts 目录。
