/* esm/flow/card-msg-node.js —— 卡间信在对话流里的**呈现**
 *
 * 为什么要单独一件：这段渲染原先长在 workspace.js 的装配闭包里
 * ⇒ ① 导不出来，单测驱动不了；② 「卡间信在对话流里长什么样」这条产品面
 * **完全没有断言保护**（装配器认得 `msg_ask/msg_answer/msg_notify`，
 * 但渲染成什么样、格式异常时怎么降级，全靠肉眼）。
 *
 * 形态（逐字照搬原实现）：
 *   · 头行合法 ⇒ `.msg.cmsg.<type>`（ask/answer/notify）＋ `data-cm=<type>`；
 *     头部一行＝「类型 · 中文名」＋（msg_ask 追加「须回应」）＋「来源 → 去向」；
 *     正文＝**剥掉头行**的剩余全文（用户看到的是话，不是那串协议头）。
 *   · 头行**格式异常** ⇒ `.msg.cmsg.bad` ＋ 头部显示「消息格式异常」，
 *     正文**照示原文**（不吞内容、不抛错）——宁可难看，不可丢话。
 *   · 有 `reply_to` ⇒ 追加一行「回应 <id>」。
 */
import { parseCardHeader, stripCardHeader, escapeHtml } from "../cardmsg.js?v=30";

const CARD_MSG_DISPLAY = { msg_ask: "询问", msg_answer: "应答", msg_notify: "通知" };
const CARD_MSG_CLS = { msg_ask: "ask", msg_answer: "answer", msg_notify: "notify" };
const CARD_MSG_MUST = { msg_ask: true };

/**
 * 造渲染器。
 * @param {object} opts
 * @param {() => string} [opts.getToLabel] 当前卡 id（显示成「来源 → 去向」的去向）
 * @returns {(node: object) => HTMLElement}
 */
export function createCardMsgRenderer({ getToLabel } = {}) {
  return function renderCardMsgNode(node) {
    const d = node.data || {};
    const content = d.content == null ? "" : String(d.content);
    const hdr = parseCardHeader(content);
    const from = node.from || (node.data && node.data.from) || "";
    const to = (typeof getToLabel === "function" && getToLabel()) || "本卡";

    const div = document.createElement("div");
    div.className = "msg cmsg" + (hdr ? (CARD_MSG_CLS[hdr.type] ? " " + CARD_MSG_CLS[hdr.type] : "") : " bad");
    if (hdr) div.dataset.cm = hdr.type;

    const bar = document.createElement("div");
    bar.className = "bar";
    const body = document.createElement("div");
    body.className = "body";
    const head = document.createElement("div");
    head.className = "head";
    const txt = document.createElement("div");
    txt.className = "txt";

    if (hdr) {
      const disp = CARD_MSG_DISPLAY[hdr.type] || hdr.type;
      head.innerHTML = `<span class="tag">${escapeHtml(hdr.type)} · ${escapeHtml(disp)}</span>` +
        (CARD_MSG_MUST[hdr.type] ? `<span class="must">须回应</span>` : "") +
        `<span class="dir">${escapeHtml(from)} → ${escapeHtml(to)}</span>`;
      txt.textContent = stripCardHeader(content);
    } else {
      head.innerHTML = `<span class="tag">消息格式异常</span><span class="dir">${escapeHtml(from)} → ${escapeHtml(to)}</span>`;
      txt.textContent = content;   // 原文全文照示
      try { console.info("[cardmsg] render bad header"); } catch (e) { /* 零阻断 */ }
    }
    body.appendChild(head);
    body.appendChild(txt);

    if (hdr && hdr.reply_to) {
      const rt = document.createElement("div");
      rt.className = "replyto";
      rt.textContent = "回应 " + hdr.reply_to;
      body.appendChild(rt);
    }

    div.appendChild(bar);
    div.appendChild(body);
    return div;
  };
}
