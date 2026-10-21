/* tests/chat_cardmsg_dom.test.mjs —— 卡间信在**对话流里怎么显示**（[5715]）
 *
 * 跑法：node --test tests/chat_cardmsg_dom.test.mjs
 *
 * 〔5715 · 补面〕卡间信的**存储面**早有 60+ 条测试（tests/test_card_msg.py），
 * 但**呈现面**（对话流里长什么样）此前**一条断言都没有**——它锁在 workspace.js
 * 的装配闭包里，导不出来。现已抽成 esm/flow/card-msg-node.js，本网把它的行为钉住：
 *
 *   · 头行合法 ⇒ 类型样式/中文名/**须回应**标记/「来源 → 去向」/正文**剥掉头行**；
 *   · 头行**格式异常** ⇒ 明确标「消息格式异常」＋ 正文**照示原文**（不丢话、不抛）；
 *   · 有 reply_to ⇒ 追加「回应 <id>」。
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";
import { createCardMsgRenderer } from "../static/esm/flow/card-msg-node.js";

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

/** 造一条头行合法的卡间信正文（协议头 + 正文）。 */
function cardMsgBody(type, from, id, text, replyTo = "") {
  return `[[card-msg id=${id} type=${type} from=${from} reply_to=${replyTo}]]\n${text}`;
}

function render(node) {
  setupDom();
  const r = createCardMsgRenderer({ getToLabel: () => "card_001" });
  return r(node);
}

/* ================================================================ 合法头行 */

test("卡间信（询问）：类型样式 ＋ 中文名 ＋ 须回应 ＋ 来源→去向，正文剥掉头行", { skip: SKIP }, () => {
  const el = render({
    from: "card_007",
    data: { content: cardMsgBody("msg_ask", "card_007", "m1", "帮我看看根卡的边界") },
  });
  assert.ok(el.classList.contains("cmsg"), "应是卡间信行");
  assert.ok(el.classList.contains("ask"), "询问类要有 ask 样式钩子");
  assert.equal(el.dataset.cm, "msg_ask", "要带 data-cm（前端据此分型）");
  const head = el.querySelector(".head");
  assert.match(head.textContent, /msg_ask/, "头部要带类型码");
  assert.match(head.textContent, /询问/, "头部要带中文名（用户看不懂 msg_ask）");
  assert.match(head.textContent, /须回应/, "询问类必须标「须回应」");
  assert.match(head.textContent, /card_007 → card_001/, "要看出处与去向");
  const txt = el.querySelector(".txt").textContent;
  assert.equal(txt.trim(), "帮我看看根卡的边界", "正文是话，不是那串协议头");
  assert.ok(!txt.includes("card-msg id="), "协议头不得露给用户");
});

test("卡间信（应答/通知）：不标「须回应」，样式钩子各归各位", { skip: SKIP }, () => {
  const ans = render({
    from: "card_007",
    data: { content: cardMsgBody("msg_answer", "card_007", "m2", "边界是三条") },
  });
  assert.ok(ans.classList.contains("answer"));
  assert.equal(ans.dataset.cm, "msg_answer");
  assert.ok(!ans.querySelector(".head").textContent.includes("须回应"), "应答不该标须回应");

  const note = render({
    from: "card_007",
    data: { content: cardMsgBody("msg_notify", "card_007", "m3", "进度同步") },
  });
  assert.ok(note.classList.contains("notify"));
  assert.equal(note.dataset.cm, "msg_notify");
  assert.match(note.querySelector(".head").textContent, /通知/);
});

test("卡间信带 reply_to：追加「回应 <id>」行", { skip: SKIP }, () => {
  const el = render({
    from: "card_007",
    data: { content: cardMsgBody("msg_answer", "card_007", "m4", "收到", "m1") },
  });
  const rt = el.querySelector(".replyto");
  assert.ok(rt, "带 reply_to 必须渲染回应行");
  assert.equal(rt.textContent, "回应 m1");
});

/* ================================================================ 格式异常 */

test("卡间信格式异常：明确标「消息格式异常」，正文**照示原文**（不丢话、不抛）", { skip: SKIP }, () => {
  const raw = "这不是协议头，但我还是要被看见。";
  const el = render({ from: "card_007", data: { content: raw } });
  assert.ok(el.classList.contains("bad"), "异常头要标 bad 样式钩子");
  assert.equal(el.dataset.cm, undefined, "异常头不该有 data-cm");
  assert.match(el.querySelector(".head").textContent, /消息格式异常/, "要明说格式异常");
  assert.equal(el.querySelector(".txt").textContent, raw, "原文照示（不吞内容）");
});

test("卡间信头行值域非法（如中文值）：同样降级为「格式异常」而不是崩", { skip: SKIP }, () => {
  const raw = "[[card-msg id=m1 type=msg_ask from=中文卡 reply_to=]]\n正文";
  const el = render({ from: "card_007", data: { content: raw } });
  assert.ok(el.classList.contains("bad"));
  assert.match(el.querySelector(".head").textContent, /消息格式异常/);
  assert.equal(el.querySelector(".txt").textContent, raw, "原文照示");
});

/* ================================================================ 注入安全 */

test("卡间信：来源/去向里的尖括号被转义（上游文本不得当 HTML 执行）", { skip: SKIP }, () => {
  const el = render({
    from: "<img src=x onerror=alert(1)>",
    data: { content: cardMsgBody("msg_notify", "card_007", "m5", "正文") },
  });
  assert.equal(el.querySelector("img"), null, "上游文本不得被当成标签渲染");
  assert.match(el.querySelector(".head").innerHTML, /&lt;img/, `应转义：${el.querySelector(".head").innerHTML}`);
});
