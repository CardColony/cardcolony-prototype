/* esm/main.js —— [分页 2026-10-16] 画布页唯一入口（/work/{wid}）。
 *
 * 画布与对话已拆成两个独立页面（用户令：「这不是客户端，是网页，网页不该这样」）：
 *   画布页 /work/{wid}          ← 本入口
 *   对话页 /work/{wid}/card/{cid} ← esm/main_chat.js
 *
 * 装配序：store → api → util → bus → nav_system → rule_panel → canvas。
 * canvas.js 不再 import workspace.js（对话模块不进画布页）：它对「打开卡对话 /
 * 刷新左栏」的调用走 pageBus，由本入口注入真实实现。URL hash 机制退役——
 * 旧链接 #wid/cid 由 redirectLegacyHash 在此一次性重定向到对话页。 */
import { store } from "./store.js";
import { API } from "./api.js";
import "./util.js";
import { pageBus } from "./page_bus.js";
import { createNavSystem, redirectLegacyHash } from "./nav_system.js";
import { initRulePanel } from "./rule_panel.js";
import "../canvas.js";

window.GetCanvasState = () => store.state.canvasState;
window.API = API;   // 调试便利（非功能依赖；与 api.js 薄壳同属兼容层）

const WORK_ID = window.__WORK_ID__;

// 左栏导航系统：画布页无选中态（selected 恒 null）；点卡＝真跳转到对话页 URL
const nav = createNavSystem({
  workId: WORK_ID,
  selected: () => null,
  onOpenCard: (wid, cid, focusChild) => {
    window.location.href = "/work/" + wid + "/card/" + cid + (focusChild ? "?focus=child" : "");
  },
});
pageBus.openCard = (wid, cid) => { window.location.href = "/work/" + wid + "/card/" + cid; };
pageBus.renderNav = () => { nav.renderNav(); };

// 规则卡（常驻吸附，不分页）；审批弹窗在对话页，画布页无回合收口、不绑定
initRulePanel({ workId: WORK_ID, withApproval: false });

// 旧链接兼容：#/wid/cid 或 #cid → 对话页（老书签不断链）
redirectLegacyHash(WORK_ID);
