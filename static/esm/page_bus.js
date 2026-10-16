/* esm/page_bus.js —— [分页 2026-10-16] 画布核心与页面导航的解耦总线。
 *
 * canvas.js 是老模块（顶层执行、模块级调用 Workspace.open / RenderNav），不能改成工厂；
 * 画布/对话拆成独立页面后，canvas.js 不再 import workspace.js（对话模块不进画布页）。
 * 它对「打开卡对话」「刷新左栏」的调用改走本总线，由**画布页入口**（esm/main.js）
 * 注入真实实现：
 *   openCard(wid, cid) ＝ 跳转 /work/{wid}/card/{cid}（对话是独立页面）
 *   renderNav()        ＝ 画布页 nav_system 实例的刷新
 * 对话页不加载 canvas.js、不注入。默认实现为空操作——未注入时点卡无反应而非崩溃。 */
export const pageBus = {
  openCard(wid, cid) { /* 未注入：画布页入口负责注入 */ },
  renderNav() { /* 未注入 */ },
};
