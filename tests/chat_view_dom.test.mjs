/* tests/chat_view_dom.test.mjs —— 对话视图 **DOM 行为网**（[5715]）
 *
 * 跑法：node --test tests/chat_view_dom.test.mjs
 *
 * 为什么要有这张网
 * ---------------
 * 之前对「对话界面」的所有断言都是**源码级**的（正则读 CSS/HTML，或读 JS 文本）。
 * 那种网钉不住「行为」：空态什么时候出现、骨架什么时候收起、节点落位对不对、
 * 回到底部按钮该什么时候冒出来——这些都只有真 DOM 跑起来才看得见。
 *
 * 2026-10-20 这一轮里，**这张网当场抓出三个真缺陷**（见下面每条的
 * 「[5715 · 网抓]」标记），也把「空态/骨架/回底/分页/运行条」这几条路径
 * 从「没人管」变成「有断言保护」。
 *
 * 依赖：jsdom（**仅测试用**；产品前端本身零依赖、不打包）。
 * 没装 jsdom 时本文件**整体跳过**（不误红）——`npm install` 后即可跑。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { createChatView } from "../static/esm/flow/chat-view.js";
import { createMessageItem } from "../static/esm/flow/message-item.js";
import {
  setupDom, makeScrollable, stubRects, JSDOM_AVAILABLE, SKIP_REASON,
} from "./dom_scaffold.mjs";

/* ---- jsdom 可选依赖：没装就整体跳过（不误红，也不假装跑过） ---- */
const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

/** 造一条节点行（renderRow 的替身）。 */
function rowFor(text) {
  const n = document.createElement("div");
  n.className = "flow-view-item";
  n.textContent = text;
  return n;
}

/** 装一个对话视图，返回视图与其常用零件。 */
function mountView(t, overrides = {}) {
  const env = setupDom();
  const rows = new Map();
  const view = createChatView({
    mount: env.mount,
    renderRow: (key) => {
      const el = rowFor(key);
      rows.set(key, el);
      return [el];
    },
    ...overrides,
  });
  // 运行条有个 setInterval（每秒刷时钟）——不销毁的话 node --test 永远不退出
  // （事件循环被挂住）。每条用例收尾必须 destroy。
  t.after(() => view.destroy());
  const scroll = view.scrollEl;
  const scrollState = makeScrollable(scroll);
  return { ...env, view, scroll, scrollState, rows };
}

/* ================================================================ 空态 / 骨架 */

/* [5715 · 网抓] 「首次拉 timeline 期间显示骨架、拿到数据后收起」这条路径，
   此前只有源码级断言（读 CSS 找 .chat-skeleton），没有任何行为保护。 */
test("首屏：骨架可见、空态隐藏；markLoaded 后（有数据）骨架收起、空态不出现", { skip: SKIP }, (t) => {
  const { view } = mountView(t);
  const skeleton = view.el.querySelector(".chat-skeleton");
  const empty = view.el.querySelector(".chat-empty");
  assert.ok(skeleton, "应有骨架节点");
  assert.ok(empty, "应有空态节点");
  assert.notEqual(skeleton.style.display, "none", "首屏骨架应可见");
  assert.equal(empty.style.display, "none", "还没拿到数据时不该出空态");

  view.render(["m1"], () => 1);
  view.markLoaded();
  assert.equal(skeleton.style.display, "none", "拿到数据后骨架必须收起");
  assert.equal(empty.style.display, "none", "有节点时不该出空态");
});

/* [5715 · 网抓] 空态与骨架**互斥**：一条记录都没有时给引导，不是给一块灰。 */
test("空态：markLoaded 后一条记录都没有 ⇒ 骨架收起、空态出现", { skip: SKIP }, (t) => {
  const { view } = mountView(t);
  view.render([], () => 1);
  view.markLoaded();
  const skeleton = view.el.querySelector(".chat-skeleton");
  const empty = view.el.querySelector(".chat-empty");
  assert.equal(skeleton.style.display, "none", "空态下骨架必须收起");
  assert.notEqual(empty.style.display, "none", "空态必须出现");
});

test("空态建议按钮：点了把文本填进输入框（派发 ui:fill-input，不代发）", { skip: SKIP }, (t) => {
  const { view, window } = mountView(t);
  view.setEmpty({ title: "还没有对话", desc: "说点什么", hints: [{ label: "先查这张卡", text: "查一下根卡" }] });
  const got = [];
  window.addEventListener("ui:fill-input", (e) => got.push(e.detail));
  const hint = view.el.querySelector(".ce-hint");
  assert.ok(hint, "应有建议按钮");
  hint.click();
  assert.equal(got.length, 1, "点建议必须派发一次填入事件");
  assert.equal(got[0].text, "查一下根卡", "填入的应是建议的正文");
});

/* ================================================================ 节点落位 */

/* [5715 · 网抓] 「工具归工具、内容给内容、信息顺序全乱」是用户亲报的现象。
   落位顺序必须严格按节点序，且骨架/空态恒在节点**之前**。 */
test("节点落位：按序排列，且骨架/空态恒在节点之前", { skip: SKIP }, (t) => {
  const { view } = mountView(t);
  view.render(["a", "b", "c"], () => 1);
  view.markLoaded();
  const column = view.columnEl;
  const order = [...column.children].map((n) => n.dataset.chatAnchorKey || n.className);
  const anchored = order.filter((x) => ["a", "b", "c"].includes(x));
  assert.deepEqual(anchored, ["a", "b", "c"], `节点顺序错了：${order.join(" > ")}`);
  const sk = order.indexOf("chat-skeleton");
  const first = order.indexOf("a");
  assert.ok(sk >= 0 && first >= 0 && sk < first, `骨架必须在节点之前：${order.join(" > ")}`);
});

test("节点剪枝：key 集合变小 ⇒ 旧节点从 DOM 里消失（不留残影）", { skip: SKIP }, (t) => {
  const { view } = mountView(t);
  view.render(["a", "b"], () => 1);
  view.render(["a"], () => 1);
  const keys = [...view.columnEl.children].map((n) => n.dataset.chatAnchorKey).filter(Boolean);
  assert.deepEqual(keys, ["a"], `旧节点没清干净：${keys.join(",")}`);
});

test("节点改版：rev 变了 ⇒ 原地换元素（不追加重复行）", { skip: SKIP }, (t) => {
  let rev = 1;
  const { view } = mountView(t);
  view.render(["a"], () => rev);
  rev = 2;
  view.render(["a"], () => rev);
  const keys = [...view.columnEl.children].map((n) => n.dataset.chatAnchorKey).filter(Boolean);
  assert.deepEqual(keys, ["a"], `同一 key 落了多行：${keys.join(",")}`);
});

/* ================================================================ 回到底部 */

/* [5715 · 网抓] 回底按钮的显隐是「跟随权」的对外表达：
   在底部 ⇒ 藏；读者上翻 ⇒ 冒出来。 */
test("回到底部：读者上翻后按钮出现，点它回到底部后按钮又藏起来", { skip: SKIP }, (t) => {
  const { view, scroll, scrollState } = mountView(t);
  view.render(["a"], () => 1);
  view.markLoaded();
  const slot = view.el.querySelector(".flow-view-to-bottom-slot");
  assert.equal(slot.style.display, "none", "在底部时回底按钮应藏起来");

  // 读者真上滚：改 scrollTop 再派 scroll
  scroll.scrollTop = 100;
  scroll.dispatchEvent(new window.Event("scroll"));
  assert.notEqual(slot.style.display, "none", "读者上翻后回底按钮必须出现");
  assert.equal(view.isAtBottom(), false, "上翻后不该还说自己在底部");

  view.el.querySelector(".flow-view-to-bottom").click();
  assert.equal(view.isAtBottom(), true, "点回底后必须回到跟随态");
  assert.equal(slot.style.display, "none", "回到底部后按钮该藏起来");
  assert.ok(scrollState.calls.length >= 1, "回底应当走一次滚动");
  assert.equal(scrollState.calls[0].behavior, "smooth", "回底应当是平滑滚动（要有过程感）");
});

/* [5715 · 网抓] 「读者上翻时来了新消息」必须**说一声**（按钮带"新消息"），
   否则用户以为界面死了。 */
test("新消息：非跟随态来了新内容 ⇒ 回底按钮标出「新消息」", { skip: SKIP }, (t) => {
  const { view, scroll } = mountView(t);
  view.render(["a"], () => 1);
  view.markLoaded();
  scroll.scrollTop = 100;
  scroll.dispatchEvent(new window.Event("scroll"));

  // 再来一条新节点（末键变化 ⇒ followSig 变）
  view.render(["a", "b"], () => 1);
  const btn = view.el.querySelector(".flow-view-to-bottom");
  assert.ok(btn.classList.contains("has-new"), "非跟随态来新内容必须标记 has-new");
  const label = btn.querySelector(".flow-view-to-bottom-label");
  assert.equal(label.textContent, "新消息", "回底按钮要说明有���消息");
});

/* ================================================================ 分页 */

/* [5715 · 网抓] 「加载更早」按钮是**死按钮**的历史包袱：
   没有更多时它不该占位；有更多时点它必须真的去加载。 */
test("分页：没有更多 ⇒ 「加载更早」隐藏；有更多 ⇒ 可见且点击触发加载", { skip: SKIP }, (t) => {
  let more = false;
  let loaded = 0;
  const { view } = mountView(t, {
    hasMore: () => more,
    onLoadOlder: () => { loaded += 1; },
  });
  const wrap = view.el.querySelector(".flow-view-older");
  assert.equal(wrap.style.display, "none", "没有更多时不该出「加载更早」");

  more = true;
  view.paintOlder();
  assert.notEqual(wrap.style.display, "none", "有更多时必须给出「加载更早」");
  view.el.querySelector(".flow-view-older-btn").click();
  assert.equal(loaded, 1, "点「加载更早」必须真的去加载");
});

/* ================================================================ 分页锚点 */

/* [5715 · 补网] 上翻加载后**锚点保位**：更早的一页插到前面后，
   原来那行必须回到同一位置（否则读者正看的内容被顶走）。 */
test("分页锚点保位：插入更早的行后，被锚定的那行回到同一位置", { skip: SKIP }, (t) => {
  let loaded = 0;
  const { view, scroll, rows } = mountView(t, {
    hasMore: () => true,
    onLoadOlder: () => { loaded += 1; },
  });
  view.render(["b", "c"], () => 1);
  view.markLoaded();
  // 造出可推演的布局（jsdom 无布局引擎）：b 在 100、c 在 150
  const rects = stubRects([rows.get("b"), rows.get("c")], { top: 100, height: 50 });

  // 读者先滚一段（scrollTop 与程序台账不同 ⇒ 判为「读者真滚动」）
  scroll.scrollTop = 200;
  scroll.dispatchEvent(new window.Event("scroll"));
  // 再滚到顶 ⇒ 触发上翻加载，并记下锚（首行 b 的位置）
  scroll.scrollTop = 0;
  scroll.dispatchEvent(new window.Event("scroll"));
  assert.equal(loaded, 1, "滚到顶必须触发上翻加载");

  // 更早的一页到了 ⇒ 布局整体下移（b 从 100 挪到 160，被顶下去 60px）
  rects.setTop(rows.get("b"), 160);
  view.render(["a", "b", "c"], () => 1);
  assert.equal(scroll.scrollTop, 60,
    "锚定的那行必须回到原位置（scrollTop 要补上被顶下去的 60px）");
});

/* ================================================================ 运行条 */

/* [5715 · 网抓] 运行条是「模型在跑」的唯一提示：
   跑 ⇒ 显示；停 ⇒ 说「正在停止…」；不跑 ⇒ 收掉。 */
test("运行条：运行中显示、停止中改文案、不跑时收掉", { skip: SKIP }, (t) => {
  let running = false;
  const { view } = mountView(t, { isRunning: () => running });
  const status = view.el.querySelector(".flow-turn-status");
  assert.ok(status, "运行条必须**构造时就在 DOM 里**（否则先发消息后到时间线时它会静默消失）");
  assert.equal(status.style.display, "none", "初态不该显示运行条");
  const label = status.querySelector(".flow-turn-status-label");
  assert.equal(status.getAttribute("role"), "status", "运行条要有 role=status（读屏会播报）");
  assert.equal(status.getAttribute("aria-live"), "polite", "运行条要 polite 播报");

  running = true;
  view.setRunning(true);
  assert.notEqual(status.style.display, "none", "跑起来必须显示运行条（不必先 render）");
  assert.equal(label.textContent, "Deep diving...");

  view.setStopping(true);
  assert.equal(label.textContent, "正在停止…", "停止中必须说清楚（否则用户以为没反应）");
  assert.ok(status.classList.contains("is-stopping"), "停止态要有独立样式钩子");

  running = false;
  view.setRunning(false);
  assert.equal(status.style.display, "none", "跑完必须收掉运行条");
});

/* [5715 · 网抓] 运行时钟**15 秒后才显**（免得刚开口就跳秒），
   到了才出现——这条此前完全没有断言。 */
test("运行条时钟：15 秒前不显，15 秒后才显并计时", { skip: SKIP }, (t) => {
  const realNow = Date.now;
  const startAt = realNow();
  let now = startAt;                 // 锚点固定（开跑那一刻），只推进「现在」
  Date.now = () => now;
  try {
    const { view } = mountView(t, { isRunning: () => true, runningStart: () => startAt });
    view.setRunning(true);
    const clock = view.el.querySelector(".flow-turn-status-clock");
    assert.equal(clock.style.display, "none", "刚开跑不该显时钟");
    now += 16000;
    view.paintStatus();
    assert.notEqual(clock.style.display, "none", "过了 15 秒该显时钟");
    assert.match(clock.textContent, /\d+分\d+秒|\d+秒/, `时钟文案不对：${clock.textContent}`);
  } finally {
    Date.now = realNow;
  }
});

/* [5715 · 网抓] 流式输出时列高会变：处于跟随态就得**继续钉在底部**，
   否则用户正看着新内容，它自己往上飘。 */
test("流式增高：处于跟随态时列高变化要把底部重新钉住", { skip: SKIP }, (t) => {
  const { view, scroll, observers } = mountView(t);
  view.render(["a"], () => 1);
  view.markLoaded();
  assert.equal(view.isAtBottom(), true, "初始应在底部");
  scroll.scrollTop = 0;      // 底部（scrollHeight 1000 / clientHeight 400 → 底部=600 起算的 0 位）
  const ro = observers[0];
  assert.ok(ro, "应挂了 ResizeObserver");
  ro.cb();
  assert.equal(scroll.scrollTop, scroll.scrollHeight, "跟随态下列高变化要重新钉底");
});

/* ================================================================ 用户气泡 */

/* [5715 · 网抓] 用户气泡是**右对齐**的，且正文默认要转义
   （不转义＝模型/上游塞进来的 HTML 直接执行）。 */
test("用户气泡：右对齐列 + 正文默认转义（不执行上游 HTML）", { skip: SKIP }, (t) => {
  const mi = createMessageItem({});
  const row = mi.renderUser({ data: { content: "<img src=x onerror=alert(1)>" } });
  assert.ok(row.classList.contains("flow-user-row"), "用户节点应是 flow-user-row");
  const bubble = row.querySelector(".flow-user-bubble");
  assert.ok(bubble, "应有气泡");
  assert.equal(bubble.querySelector("img"), null, "上游 HTML 不得被当成标签渲染");
  assert.match(bubble.innerHTML, /&lt;img/, `正文应被转义：${bubble.innerHTML}`);
});

test("用户气泡：注入格式化器时按它渲染（项目的 md 格式化走这条）", { skip: SKIP }, (t) => {
  const mi = createMessageItem({ renderUserHtml: (t) => `<p>${t}</p>` });
  const row = mi.renderUser({ data: { content: "查一下根卡" } });
  const p = row.querySelector(".flow-user-bubble p");
  assert.ok(p, "应按注入的格式化器渲染");
  assert.equal(p.textContent, "查一下根卡");
});

/* 未知面（JSON 行）：**不丢载荷**——原样 JSON 化给人看。 */
test("未知面：载荷不丢，JSON 化后给人看", { skip: SKIP }, (t) => {
  const mi = createMessageItem({});
  const row = mi.renderUnknown({ data: { a: 1, b: "x" } });
  const pre = row.querySelector("pre.flow-unknown-body");
  assert.ok(pre, "未知面应是 pre 块");
  assert.deepEqual(JSON.parse(pre.textContent), { a: 1, b: "x" });
});
