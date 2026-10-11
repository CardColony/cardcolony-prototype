/* api.js —— 薄壳（迭代四 F2①）：唯一实现已迁 static/esm/api.js（ESM）。
 * 本壳服务 index.html 页（剧本架，普通 script 加载、不转 ESM）：
 * 动态 import 后挂 window，shelf.js 事件回调内使用（异步就绪，无顶层立即调用）。
 * work 页（work.html）经 esm/main.js 静态 import，不走本壳。
 */
(async function () {
  const m = await import("/static/esm/api.js?v=11.0");
  window.API = m.API;
  window.toast = m.toast;
})();
