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
import { API } from "./api.js?v=35";
import "./util.js?v=30";
import "../stream.js?v=36";
import "../workspace.js?v=45";

window.API = API;   // 调试便利（非功能依赖；与 api.js 薄壳同属兼容层）
