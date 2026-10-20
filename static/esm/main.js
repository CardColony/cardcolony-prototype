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
import { API } from "./api.js?v=39";
import "./util.js?v=30";
import "../canvas.js?v=82";
import { createNavSystem } from "./nav_system.js?v=37";
import { loadRulePanel } from "./rule_panel.js?v=33";

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

/* [阶段2 A2.2 补齐 2026-10-17] 旧 hash 链接兼容（**画布页侧**）。
 * 旧书签/旧链接形如 `/work/{wid}#{wid}/{cid}`（或旧形态 `#cid`）。对话页那条路由由
 * workspace.js 的 bootRestore 处理；**画布页不加载 workspace.js**，所以本入口必须自己
 * 做一次性重定向——否则从画布路径进来会「停在原地不跳」，旧链接静默失效。
 * 口径复用 navStore.select.get()（＝ state_layers 的 parseHash，两种形态都认），
 * 与 bootRestore 同一份解析，不另造第二套。
 * 放在 startNav() 之前：既然要跳走，就别先把导航/画布渲染一遍。 */
{
  const sel = navSystem.navStore.select.get();
  if (sel && sel.cid) {
    location.replace("/work/" + sel.wid + "/card/" + sel.cid);
  }
}

navSystem.startNav();

// 规则卡＝吸附右侧的常驻面板，画布页同样在场
loadRulePanel();
