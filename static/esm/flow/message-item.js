/* esm/flow/message-item.js —— 用户气泡与轮内失败行（dsh `chat/MessageItem.tsx` 的移植件）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/chat/MessageItem.tsx
 *       ＋ MessageItem.module.css（气泡几何原值见 style.css 的 .flow-user-* 段）
 *
 * 移植口径：
 *   · 用户气泡（:179-205）：右对齐列（bubble + actions 间隙 6），气泡
 *     max-width: min(525px, 82%)、22px 圆角、10px/16px 内距、16px/24px 字号行高
 *     ——数值全部取 MessageItem.module.css:4-29 原值。
 *   · 未知面（:296-307）：JSON 行。
 *   ★ dsh 的 TurnErrorItem（:117-131，输入框外的整轮失败行）**本机不移植**：
 *     任务书 §四b 第 7 条明确要求「运行中 error 按**过程行形态**＋红色态显示」，
 *     不是 dsh 的 turn-error 行。照 dsh 再画一个失败行会与 §四b 冲突。
 *
 * 载体替换：dsh 的 MessageText / JsonBlock / StateDot / ImageGallery 来自原子库，
 *   本机没有 ⇒ 文本走注入的格式化器（本项目的 md/卡间信头剥离仍在 workspace），
 *   JSON 行用 <pre>，状态点用 8px 圆点。图片面本机不存在 ⇒ 不接。
 */

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

/**
 * @param {object} opts
 * @param {(text:string)=>string} opts.renderUserHtml 用户正文 → HTML（项目自有格式化）
 * @param {(payload:any)=>string} [opts.renderJson]  未知面 JSON → HTML
 */
export function createMessageItem({ renderUserHtml, renderJson }) {
  /** 用户节点：右侧气泡（原文 :238-258）。 */
  function renderUser(node) {
    const data = node.data || {};
    const text = String(data.content == null ? "" : data.content);
    const row = el("div", "flow-user-row");
    const stack = el("div", "flow-user-stack");
    const bubble = el("div", "flow-user-bubble");
    bubble.innerHTML = renderUserHtml ? renderUserHtml(text) : escHtml(text);
    stack.appendChild(bubble);
    row.appendChild(stack);
    return row;
  }

  /** 未知面（原文 :296-307）：不丢载荷。 */
  function renderUnknown(node) {
    const data = node.data || {};
    const row = el("div", "flow-unknown-row");
    const pre = el("pre", "flow-unknown-body");
    pre.textContent = renderJson ? renderJson(data) : safeJson(data);
    row.appendChild(pre);
    return row;
  }

  return { renderUser, renderUnknown };
}

function safeJson(v) {
  try { return JSON.stringify(v, null, 1); } catch (e) { return String(v); }
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
