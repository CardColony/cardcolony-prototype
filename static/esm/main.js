/* esm/main.js —— 画布页唯一入口。
 *
 * [阶段3 A3.3 2026-10-17] 双入口装配的**画布侧**：本入口只装画布所需模块图——
 *   util → api → store → canvas → nav_system → rule_panel。
 * **不加载 workspace.js / stream.js**（那是对话侧 main_chat.js 的）；纯度由
 * scripts/check_module_graph.py --check 机械断言。
 *
 * canvas.js 顶层即触发首帧画布加载（module import 即执行，时序与原 IIFE 一致）。
 * 兼容挂载（U-6 预算内第 2 处）：window.GetCanvasState 供调试/探针读画布状态
 * （store.state.canvasState 由 canvas.js 注册）。
 */
import { store } from "./store.js?v=30";
import { API } from "./api.js?v=35";
import "./util.js?v=30";
import "../canvas.js?v=41";
import { createNavSystem } from "./nav_system.js?v=31";
import { loadRulePanel } from "./rule_panel.js?v=30";

const WORK_ID = window.__WORK_ID__;
const navTree = document.getElementById("navTree");

window.GetCanvasState = () => store.state.canvasState;
window.API = API;   // 调试便利（非功能依赖；与 api.js 薄壳同属兼容层）

/* 导航系统（画布页侧装配）：本页没有 workspace 的 open()，点卡＝整页跳转到对话独立地址；
 * inspector 不存在（对话侧才挂），传 null——nav_system 内对 focusChild 分支已加空守卫。 */
const navSystem = createNavSystem({
  workId: WORK_ID,
  navTree,
  api: API,
  getCurrentCard: () => null,
  open: (cid) => { location.assign("/work/" + WORK_ID + "/card/" + cid); },
  inspector: null,
});
navSystem.startNav();

// 规则卡＝吸附右侧的常驻面板，画布页同样在场
loadRulePanel();
