/* esm/theme.js —— 深浅主题切换（对话页）
 *
 * 为什么单独一件：主题是**页面级**状态（<html data-theme>），与对话数据无关，
 * 不该塞进 workspace 的渲染闭包里；而且它必须在首帧前就生效（防闪白），
 * 首帧置位由 templates/card.html 的 head 内联脚本完成，本件只负责「切换 + 持久化」。
 *
 * 契约：
 *   · 唯一真值 = document.documentElement.dataset.theme ∈ {"dark","light"}
 *   · 持久化键 ui.theme（与 head 内联脚本同一键，两处不得各写一套）
 *   · 未点过按钮时跟随系统偏好（matchMedia），点过之后以记忆为准
 */
const KEY = "ui.theme";

function current() {
  const t = document.documentElement.dataset.theme;
  return t === "light" ? "light" : "dark";
}

function apply(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(KEY, theme); } catch (e) { /* 隐私模式：只切不记 */ }
  const btn = document.getElementById("themeToggleBtn");
  if (btn) {
    btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
    btn.title = theme === "light" ? "切换到深色" : "切换到浅色";
  }
  try { window.dispatchEvent(new CustomEvent("ui:theme", { detail: { theme } })); } catch (e) { /* 零阻断 */ }
}

export function initTheme() {
  const btn = document.getElementById("themeToggleBtn");
  apply(current());
  if (btn) btn.addEventListener("click", () => apply(current() === "dark" ? "light" : "dark"));
  // 系统偏好变化：仅在用户从未手动选过时跟随
  try {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", (e) => {
        let saved = null;
        try { saved = localStorage.getItem(KEY); } catch (err) { saved = null; }
        if (!saved) apply(e.matches ? "light" : "dark");
      });
    }
  } catch (e) { /* 老浏览器：不跟随，无副作用 */ }
}

/* ---------------------------------------------------------------------------
   过程细节开关
   ---------------------------------------------------------------------------
   折叠态只留「工具调用 + 轮次分隔」——这两样是「它到底做了什么」的实质动作；
   装配上下文、判断协议、交付拼接这类运行时注脚在折叠态不占位置。
   默认展开（留痕是本产品的底色，默认藏起来等于改了产品性格）。
   --------------------------------------------------------------------------- */
const PROC_KEY = "ui.proc";

function applyProc(collapsed) {
  document.body.classList.toggle("proc-collapsed", collapsed);
  const btn = document.getElementById("procToggleBtn");
  if (btn) {
    btn.setAttribute("aria-pressed", collapsed ? "false" : "true");
    btn.title = collapsed ? "展开过程细节" : "收起过程细节";
    btn.classList.toggle("is-off", collapsed);
  }
}

export function initProcToggle() {
  let saved = null;
  try { saved = localStorage.getItem(PROC_KEY); } catch (e) { saved = null; }
  applyProc(saved === "0");
  const btn = document.getElementById("procToggleBtn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = !document.body.classList.contains("proc-collapsed");
    applyProc(next);
    try { localStorage.setItem(PROC_KEY, next ? "0" : "1"); } catch (e) { /* 隐私模式 */ }
  });
}

/* ---------------------------------------------------------------------------
   窄屏导航抽屉（<900px 唯一入口）
   ---------------------------------------------------------------------------
   左栏在窄屏不再直接 display:none——那是「没有导航可用」，不是适配。
   抽屉由 #navDrawerBtn 开、点遮罩或按 Esc 关；桌面宽度下该按钮不渲染。
   --------------------------------------------------------------------------- */
export function initNavDrawer() {
  const btn = document.getElementById("navDrawerBtn");
  if (!btn) return;
  const close = () => document.body.classList.remove("nav-open");
  btn.addEventListener("click", () => {
    document.body.classList.toggle("nav-open");
  });
  // 点抽屉外的任意处关闭（不靠遮罩层：深色底上遮罩视觉无意义，还引入层级竞争）
  document.addEventListener("click", (e) => {
    if (!document.body.classList.contains("nav-open")) return;
    const nav = document.querySelector(".app-nav");
    if (nav && nav.contains(e.target)) return;
    if (e.target === btn || btn.contains(e.target)) return;
    close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

/* ---------------------------------------------------------------------------
   搜索框清除键：有输入才出现；点它清空并把 input 事件交回搜索逻辑
   （不直接调搜索函数——搜索由 nav_find 自己监听 input，重发一次事件即可同源）。
   --------------------------------------------------------------------------- */
export function initSearchClear() {
  const box = document.querySelector(".nav-search-box");
  const input = document.getElementById("navSearchInput");
  const clear = document.getElementById("navSearchClear");
  if (!box || !input || !clear) return;
  const sync = () => box.classList.toggle("has-value", !!input.value);
  input.addEventListener("input", sync);
  sync();
  clear.addEventListener("click", () => {
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    sync();
  });
}
