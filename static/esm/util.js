/* esm/util.js —— 共享纯工具（card_045/F2 迭代四 · 唯一实现）。
 * 原 api/workspace/canvas/旧列表页 四处各自的 escapeHtml/esc 收编于此（F2②）；
 * 旧列表页那一份随该页 2026-10-15 整体退役一并删除，现存实现为唯一一份。
 */
export function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
