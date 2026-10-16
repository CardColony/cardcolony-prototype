/* esm/main.js —— work 页唯一入口（迭代四 F2④：四 script → 单 module）。
 * import 顺序＝依赖序：util → api → store → stream → canvas → workspace。
 * canvas.js 顶层即触发 reloadCanvas（module import 即执行，时序与原 IIFE 一致）。
 * 兼容挂载（U-6 预算内第 2 处）：window.GetCanvasState 供调试/探针读画布状态
 * （store.state.canvasState 由 canvas.js 注册）。
 */
import { store } from "./store.js?v=30";
import { API } from "./api.js?v=35";
import "./util.js?v=30";
import "../stream.js?v=36";
import "../canvas.js?v=40";
import "../workspace.js?v=41";

window.GetCanvasState = () => store.state.canvasState;
window.API = API;   // 调试便利（非功能依赖；不计功能挂载——与 api.js 薄壳同属兼容层）
