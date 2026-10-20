/* tests/chat_think_stream_dom.test.mjs —— 思考块的**增量渲染**网（[5715] · 性能）
 *
 * 跑法：node --test tests/chat_think_stream_dom.test.mjs
 *
 * ## 为什么有这张网
 *
 * 直播时模型的思考是**逐帧**吐的（每帧一个 SSE reasoning_chunk），前端每帧让
 * 那一行重渲染，而**行里含累积全文** ⇒ 一次思考 N 帧 ⇒ 写入量 ∝ 1+2+…+N =
 * **O(N²)** ⇒ 长思考（实测上万帧）把主线程打满 —— 这是「对话页卡死」的
 * 另一半原因（另一半在落盘侧，已由 event_stream 的「按轮合并落一条」解决）。
 *
 * ## 网里钉的不变量
 *
 * | 编号 | 不变量 |
 * |---|---|
 * | T1 | N 帧思考 ⇒ **写入总量 ≈ 1 倍最终长度**（增量追加），不是 N/2 倍（平方级） |
 * | T2 | 最终 DOM 文本 == 累积全文（增量不能丢字） |
 * | T3 | 文本被**整体替换**（非前缀扩展）⇒ 整块重写（不硬套增量） |
 * | T4 | 增量更新**保留用户的折叠态**（点开的思考不该被下一帧弹回去） |
 * | T5 | prune 掉不在页面上的节点（缓存不留旧元素） |
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";
import { createThinkRenderer, createTextUpdater, thinkSummary } from "../static/esm/flow/think-stream.js";

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

/** 沿原型链找 textContent 的描述符（jsdom 里它在 Node.prototype 上，不在元素原型上）。 */
function textContentDesc(el) {
  let proto = el;
  while (proto) {
    const d = Object.getOwnPropertyDescriptor(proto, "textContent");
    if (d) return d;
    proto = Object.getPrototypeOf(proto);
  }
  return null;
}

/**
 * 给 .think-body 装上「写入计量」：整块重写（textContent 赋值）记全量，
 * 增量追加（appendChild 文本节点）记增量。
 */
function meter(body) {
  const stat = { fullWrites: 0, fullChars: 0, appendChars: 0, appends: 0 };
  const desc = textContentDesc(body);
  Object.defineProperty(body, "textContent", {
    configurable: true,
    get() { return desc.get.call(this); },
    set(v) {
      if (v !== "") { stat.fullWrites += 1; stat.fullChars += String(v).length; }
      desc.set.call(this, v);
    },
  });
  const origAppend = body.appendChild.bind(body);
  body.appendChild = (node) => {
    if (node && node.nodeType === 3) {
      stat.appends += 1;
      stat.appendChars += (node.data || "").length;
    }
    return origAppend(node);
  };
  return stat;
}

test("思考增量：N 帧 ⇒ 写入总量 ≈ 1 倍最终长度（不是平方级）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const r = createThinkRenderer();
  const FRAMES = 400;
  const CHUNK = 40;
  let text = "";
  let el = null;
  let stat = null;
  for (let i = 0; i < FRAMES; i++) {
    text += "思".repeat(CHUNK);           // 逐帧增长
    el = r.row("k1", text, { done: false });
    if (i === 0) stat = meter(el.querySelector(".think-body"));
  }
  const finalLen = text.length;
  const written = stat.fullChars + stat.appendChars;
  assert.equal(el.querySelector(".think-body").textContent, text, "增量不能丢字");
  assert.ok(stat.fullWrites <= 1, `整块重写应只发生 1 次，实为 ${stat.fullWrites} 次`);
  assert.ok(
    written <= finalLen * 1.2,
    `写入总量应≈最终长度（${finalLen}），实为 ${written} —— 平方级写入没修掉`,
  );
  assert.ok(stat.appends >= FRAMES - 1, `应逐帧追加，实为 ${stat.appends} 次追加`);
});

test("思考增量：最终文本与全文一致（逐帧拼接无丢字）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const r = createThinkRenderer();
  const parts = ["第一行\n", "第二行\n", "第三行"];
  let text = "";
  let el = null;
  for (const p of parts) {
    text += p;
    el = r.row("k2", text, { done: false });
  }
  assert.equal(el.querySelector(".think-body").textContent, text);
  assert.equal(el.querySelector(".think-sum").textContent, thinkSummary(text, true),
    "运行中摘要取最新一行");
});

test("思考增量：文本被整体替换 ⇒ 重建元素（不硬套增量）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const r = createThinkRenderer();
  const a = r.row("k3", "旧的一段思考", { done: false });
  const b = r.row("k3", "完全不同的新内容", { done: false });   // 不是前缀扩展
  assert.notEqual(a, b, "整体替换必须重建元素（否则新内容会追加在旧内容后面）");
  assert.equal(b.querySelector(".think-body").textContent, "完全不同的新内容",
    "替换后不能残留旧文本");
  assert.equal(a.querySelector(".think-body").textContent, "旧的一段思考",
    "旧元素不被污染");
});

test("思考增量：保留用户的折叠态（点开的思考不被下一帧弹回去）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const r = createThinkRenderer();
  const el = r.row("k4", "第一段", { done: false });
  el.querySelector(".think-head").click();          // 用户展开
  assert.ok(!el.classList.contains("collapsed"), "点开后应是展开态");
  const el2 = r.row("k4", "第一段第二段", { done: false });
  assert.ok(!el2.classList.contains("collapsed"), "增量更新不得重置折叠态");
  // 定稿态切换（done）也不该把折叠态吃掉
  const el3 = r.row("k4", "第一段第二段第三段", { done: true });
  assert.ok(el3.classList.contains("done"), "定稿后要有 done 态");
  assert.ok(!el3.classList.contains("collapsed"), "定稿也不得重置折叠态");
});

test("思考增量：prune 清掉不在页面上的节点（缓存不留旧元素）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const r = createThinkRenderer();
  r.row("k5", "甲", { done: false });
  r.row("k6", "乙", { done: false });
  assert.deepEqual(r.keys().sort(), ["k5", "k6"]);
  r.prune(new Set(["k5"]));
  assert.deepEqual(r.keys(), ["k5"], "prune 后只剩还挂着的节点");
});

test("思考增量：同一节点反复取 ⇒ 元素身份稳定（不重建 DOM）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const r = createThinkRenderer();
  const a = r.row("k7", "甲", { done: false });
  const b = r.row("k7", "甲乙", { done: false });
  assert.equal(a, b, "增量路径必须复用同一元素（否则 DOM 重建，收益归零）");
});

/* ---------------------------------------------------------------- 增量文本器
   （三席并排卡里的思考：``.tk`` 也是逐帧增长的整块文本） */

test("增量文本器：同元素 ⇒ 只追加增量（写入量 O(增量)）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const up = createTextUpdater();
  const el = document.createElement("span");
  document.body.appendChild(el);
  const stat = meter(el);
  let text = "";
  for (let i = 0; i < 200; i++) {
    text += "席".repeat(20);
    up.set("seat1", el, text);
  }
  assert.equal(el.textContent, text, "增量不能丢字");
  assert.ok(stat.fullWrites <= 1, `整块重写应只发生 1 次，实为 ${stat.fullWrites}`);
  assert.ok(stat.fullChars + stat.appendChars <= text.length * 1.2,
    `写入总量应≈最终长度（${text.length}），实为 ${stat.fullChars + stat.appendChars}`);
});

test("增量文本器：元素换了（重建）⇒ 整块重写，不追加到旧元素上", { skip: SKIP }, () => {
  const { document } = setupDom();
  const up = createTextUpdater();
  const el1 = document.createElement("span");
  up.set("seat1", el1, "旧");
  const el2 = document.createElement("span");
  up.set("seat1", el2, "全新内容");
  assert.equal(el2.textContent, "全新内容", "新元素上写的是新全文");
  assert.equal(el1.textContent, "旧", "旧元素不被污染");
});
