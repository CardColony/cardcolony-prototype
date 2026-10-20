/* esm/main_chat.js —— 对话页唯一入口。
 *
 * [阶段3 A3.3 2026-10-17] 双入口装配的**对话侧**：模块图 util → api → store →
 *   stream → workspace（workspace 内部再装配 nav_system / rule_panel）。
 * **不加载 canvas.js**（那是画布侧 main.js 的）；纯度由
 * scripts/check_module_graph.py --check 机械断言。点卡回画布＝整页跳转 /work/{wid}。
 *
 * 与画布页的差异：本页不挂 window.GetCanvasState（画布状态本页不存在）；
 * workspace.js 的消费点自带 `window.GetCanvasState &&` 空守卫。
 */
import { store } from "./store.js?v=30";
import { API } from "./api.js?v=41";
import "./util.js?v=30";
import { initTheme, initProcToggle, initNavDrawer, initSearchClear, initShortcuts } from "./theme.js?v=6";
import { initModelBadge } from "./model_picker.js?v=3";
import "../stream.js?v=51";
import "../workspace.js?v=67";

window.API = API;   // 调试便利（非功能依赖；与 api.js 薄壳同属兼容层）

/* [对话界面商业化 2026-10-17] 主题在装配前就位：先于 workspace 的首帧渲染，
   避免「深色骨架 → 浅色内容」的闪变。 */
initTheme();
initProcToggle();
initNavDrawer();
initSearchClear();
initShortcuts();

/* [模型设置 2026-10-19 产品裁定] 点哪里用哪里换：卡片页顶栏模型徽标。
   __WORK_ID__ / __CARD_ID__ 由 card.html 注入。 */
initModelBadge({
  mountId: "modelBadgeMount",
  workId: window.__WORK_ID__,
  cardId: window.__CARD_ID__ || "",
});
