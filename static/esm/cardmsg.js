/* esm/cardmsg.js —— 卡间消息头行解析（迭代四 F4⑤）。
 * 与 Python 侧 src/api/card_msg.py **共享同一套测试向量**（任务书 T-5 跨组判据：
 * 两套实现解析同一字符串必须同结果——语法见《迭代四方案-卡间通信接口与底座》§4.2）。
 * 打点 [cardmsg]：渲染／解析失败。
 * 头行：[[card-msg id=<msg_id> type=<type_code> from=<from_card> reply_to=<msg_id|空>]]
 */
import { escapeHtml } from "./util.js?v=30";

const HEADER_RE = /^\s*\[\[card-msg\s+(.+?)\]\]\s*$/;
const KEY_RE = /^[a-z_]+$/;
const VALUE_RE = /^[A-Za-z0-9_\-:.]+$/;

export function parseCardHeader(content) {
  try {
    if (typeof content !== "string" || !content) return null;
    const firstLine = content.split("\n", 1)[0];
    const m = HEADER_RE.exec(firstLine);
    if (!m) return null;
    const fields = {};
    for (const token of m[1].split(/\s+/)) {
      const eq = token.indexOf("=");
      if (eq <= 0) return null;
      const k = token.slice(0, eq);
      const v = token.slice(eq + 1);
      if (!KEY_RE.test(k)) return null;
      if (v && !VALUE_RE.test(v)) return null;
      if (k in fields) return null;
      fields[k] = v;
    }
    for (const req of ["id", "type", "from"]) {
      if (!fields[req]) return null;
    }
    return { id: fields.id, type: fields.type, from: fields.from, reply_to: fields.reply_to || "" };
  } catch (e) {
    try { console.info("[cardmsg] parse fail"); } catch (_) { /* 零阻断 */ }
    return null;
  }
}

export function stripCardHeader(content) {
  try {
    if (typeof content !== "string" || !content) return "";
    const idx = content.indexOf("\n");
    const firstLine = idx < 0 ? content : content.slice(0, idx);
    if (HEADER_RE.test(firstLine)) return idx < 0 ? "" : content.slice(idx + 1);
    return content;
  } catch (e) {
    try { console.info("[cardmsg] strip fail"); } catch (_) { /* 零阻断 */ }
    return content || "";
  }
}

export { escapeHtml };
