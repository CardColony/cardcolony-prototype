/* esm/util.js —— 共享纯工具（card_045/F2 迭代四 · 唯一实现）。
 * 原 api/workspace/canvas/shelf 四处各自的 escapeHtml/esc 收编于此（F2②）。
 */
export function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
