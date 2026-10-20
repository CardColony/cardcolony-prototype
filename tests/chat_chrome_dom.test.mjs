/* tests/chat_chrome_dom.test.mjs —— 对话页**页面外壳**的 DOM 行为网（[5715]）
 *
 * 跑法：node --test tests/chat_chrome_dom.test.mjs
 *
 * 覆盖对话页上那些**不在聊天流里、但用户天天碰**的控件：
 * 主题切换 / 窄屏抽屉 / 过程折叠 / 搜索清除 / Esc 逐层关闭 / 代码块复制。
 *
 * 〔5715 · 补网〕上一轮把这几面列为缺口（只有源码级断言）。本网把它们变成
 * 行为断言，并当场复钉了 P6（通知面板按 Esc 后再也打不开）的修法。
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs（浏览器能力缺口都补在那儿）。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { readFileSync } from "node:fs";
import { setupDom, CHAT_HTML } from "./dom_scaffold.mjs";
import { initTheme, initProcToggle, initNavDrawer, initSearchClear, initShortcuts }
  from "../static/esm/theme.js";
import { bindCopyButtons } from "../static/esm/md.js";

const SKIP = false;   // jsdom 是硬依赖（package.json 已声明），没有它整批 skip


/* ================================================================ 主题切换 */

test("主题：默认深色；点一下切浅色，并落持久化 + 按钮态 + 广播事件", { skip: SKIP }, () => {
  const { document, window } = setupDom();
  initTheme();
  const html = document.documentElement;
  assert.equal(html.dataset.theme, "dark", "没选过就是深色");
  assert.equal(window.localStorage.getItem("ui.theme"), "dark", "首次也要落记忆（键与 head 内联脚本同键）");

  const btn = document.getElementById("themeToggleBtn");
  assert.equal(btn.getAttribute("aria-pressed"), "false", "深色时按钮不该按着");

  const seen = [];
  window.addEventListener("ui:theme", (e) => seen.push(e.detail.theme));
  btn.click();
  assert.equal(html.dataset.theme, "light", "点一下必须切到浅色");
  assert.equal(window.localStorage.getItem("ui.theme"), "light", "选择必须持久化");
  assert.equal(btn.getAttribute("aria-pressed"), "true", "浅色时按钮该按着");
  assert.equal(btn.title, "切换到深色", "按钮标题要说清「点了会去哪」");
  assert.deepEqual(seen, ["light"], "主题变化必须广播（其它件靠它跟着换）");
});

test("主题：唯一真值是 <html data-theme>（记忆由 head 内联脚本负责置位）", { skip: SKIP }, () => {
  const { document, window } = setupDom();
  // 首帧置位由 templates/card.html 的 head 内联脚本做（读 localStorage 写 dataset）；
  // theme.js 本身**只认 dataset**（它的契约注释写明「唯一真值 = dataset.theme」）。
  document.documentElement.dataset.theme = "light";
  window.localStorage.setItem("ui.theme", "light");
  initTheme();
  assert.equal(document.documentElement.dataset.theme, "light", "以 dataset 为准（首帧已定好）");
  assert.equal(document.getElementById("themeToggleBtn").getAttribute("aria-pressed"), "true",
    "按钮态要跟 dataset 一致");
  // 记忆键：theme.js 与 head 内联脚本必须同一个键（两处各写一套 ⇒ 记忆对不上）
  assert.equal(window.localStorage.getItem("ui.theme"), "light", "记忆键必须是 ui.theme");
});

/* ================================================================ 过程折叠 */

test("过程折叠：默认展开；点一下折叠并记忆；重进页面仍是折叠态", { skip: SKIP }, () => {
  const { document, window } = setupDom();
  initProcToggle();
  const body = document.body;
  const btn = document.getElementById("procToggleBtn");
  assert.ok(!body.classList.contains("proc-collapsed"), "默认必须展开（留痕是本产品底色）");
  assert.equal(btn.getAttribute("aria-pressed"), "true", "展开时按钮按着");
  assert.equal(btn.title, "收起过程细节", "标题要说清点了会做什么");

  btn.click();
  assert.ok(body.classList.contains("proc-collapsed"), "点一下必须折叠");
  assert.equal(btn.getAttribute("aria-pressed"), "false", "折叠时按钮不该按着");
  assert.equal(btn.title, "展开过程细节", "折叠后标题要反过来");
  assert.ok(btn.classList.contains("is-off"), "折叠态要有样式钩子");
  assert.equal(window.localStorage.getItem("ui.proc"), "0", "折叠态必须持久化");

  // 模拟刷新：同一份 localStorage 重新初始化
  const env2 = setupDom();
  env2.window.localStorage.setItem("ui.proc", "0");
  initProcToggle();
  assert.ok(env2.document.body.classList.contains("proc-collapsed"), "刷新后必须仍是折叠态");
  assert.equal(env2.document.getElementById("procToggleBtn").getAttribute("aria-pressed"), "false");
});

/* ================================================================ 窄屏抽屉 */

test("窄屏抽屉：按钮开合；点抽屉外关；点抽屉内不关；Esc 关", { skip: SKIP }, () => {
  const { document } = setupDom();
  initNavDrawer();
  const btn = document.getElementById("navDrawerBtn");
  const body = document.body;

  btn.click();
  assert.ok(body.classList.contains("nav-open"), "点按钮必须打开抽屉");

  // 点抽屉内 → 不关
  document.querySelector(".app-nav").click();
  assert.ok(body.classList.contains("nav-open"), "点抽屉内部不该关掉它");

  // 点抽屉外 → 关
  document.getElementById("chatArea").click();
  assert.ok(!body.classList.contains("nav-open"), "点抽屉外必须关");

  // Esc → 关
  btn.click();
  assert.ok(body.classList.contains("nav-open"));
  document.dispatchEvent(new document.defaultView.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.ok(!body.classList.contains("nav-open"), "Esc 必须能关抽屉");
});

/* ================================================================ 搜索清除 */

test("搜索清除：有输入才出现；点它清空并把 input 事件交回搜索逻辑", { skip: SKIP }, () => {
  const { document } = setupDom();
  const box = document.querySelector(".nav-search-box");
  const input = document.getElementById("navSearchInput");
  const clear = document.getElementById("navSearchClear");
  initSearchClear();
  assert.ok(!box.classList.contains("has-value"), "没输入不该占位");

  let fired = 0;
  input.addEventListener("input", () => { fired += 1; });
  input.value = "根卡";
  input.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
  assert.ok(box.classList.contains("has-value"), "有输入必须出现清除键");

  clear.click();
  assert.equal(input.value, "", "点清除必须清空输入");
  assert.ok(fired >= 1, "必须重发 input 事件（搜索逻辑靠它刷新）");
  assert.ok(!box.classList.contains("has-value"), "清空后清除键该收起来");
});

/* ================================================================ 过程折叠的 CSS 效果 */

/* [5715 · 补网] 「折叠态到底藏了哪些行」此前只有**源码级**断言（正则找 CSS 串）。
   〔实测坑〕jsdom 没有级联布局 ⇒ 「某元素会不会被 display:none 盖住」测不了。
   ⇒ 换一条**能测的路**：把**真规则**（含 :not() 与属性选择器）拿来，
   对**真 DOM 元素**做 `element.matches(selector)` —— 匹配＝该规则会命中它
   （即它会被那条 display:none 藏起来）。
   这不是「读源码找字符串」：选择器变了、类名改了，匹配结果就变。 */
test("过程折叠的 CSS 效果：折叠后只藏「装配类过程行」，工具行与轮次分隔留着", { skip: SKIP }, () => {
  const { document } = setupDom();
  const css = readFileSync(new URL("../static/ui.css", import.meta.url), "utf8");
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(([, sel, body]) => sel.includes("proc-collapsed") && /display\s*:\s*none/.test(body))
    // 注释会落进选择器那一段 ⇒ 先剥掉（否则 el.matches 报「不是合法选择器」）
    .map(([, sel]) => sel.replace(/\/\*[\s\S]*?\*\//g, "").trim())
    .filter((sel) => sel.length);
  assert.ok(rules.length, "ui.css 里应有折叠态的 display:none 规则");

  document.body.className = "page-chat proc-collapsed";
  const step = document.createElement("div");
  step.className = "step-row";                       // 装配类过程行 ⇒ 该藏
  const tool = document.createElement("div");
  tool.className = "step-row tool-row";              // 工具行 ⇒ 留着（它是「干了什么」）
  const round = document.createElement("div");
  round.className = "step-row";
  round.dataset.ev = "round_start";                  // 轮次分隔 ⇒ 留着
  document.body.append(step, tool, round);

  const hit = (el) => rules.some((sel) => el.matches(sel));
  assert.ok(hit(step), "折叠态必须藏装配类过程行（注入/判断协议这类脚注）");
  assert.ok(!hit(tool), "折叠态**要留**工具行（它是「它到底做了什么」的实质动作）");
  assert.ok(!hit(round), "折叠态**要留**轮次分隔");
});

/* ================================================================ Esc 逐层关闭 */

test("Esc 逐层关闭：弹窗 → 通知面板 → 抽屉（先关最上面那层）", { skip: SKIP }, () => {
  const { document } = setupDom();
  initShortcuts();
  const modal = document.createElement("div");
  modal.className = "draft-modal open";
  modal.innerHTML = '<button class="draft-modal-close">×</button>';
  document.body.appendChild(modal);
  // 弹窗的关闭按钮由 workspace 绑（真页面里它会摘掉 .open）。
  // 这里只绑一个「摘 class」的行为，用来观察 theme.js 的**关闭顺序**。
  modal.querySelector(".draft-modal-close").addEventListener("click", () => {
    modal.classList.remove("open");
  });
  const np = document.getElementById("notifyPanel");
  np.classList.add("open");
  document.body.classList.add("nav-open");

  const esc = () => document.dispatchEvent(
    new document.defaultView.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

  esc();
  assert.ok(!modal.classList.contains("open"), "第一下 Esc 关最上层（弹窗）");
  assert.ok(np.classList.contains("open"), "通知面板这层还没轮到");
  assert.ok(document.body.classList.contains("nav-open"), "抽屉这层还没轮到");

  esc();
  assert.ok(!np.classList.contains("open"), "第二下 Esc 关通知面板");
  assert.ok(document.body.classList.contains("nav-open"), "抽屉还没轮到");
  // jsdom 没有样式表 ⇒ 面板的 computed display 恒为 block。
  // 真页面上 `.notify-panel{display:none}` 会在摘 class 后立刻生效 ⇒ 这里补上
  // 那一层等价物，Esc 才会继续往下走（不然它会一直认为面板还开着）。
  np.style.display = "none";

  esc();
  assert.ok(!document.body.classList.contains("nav-open"), "第三下 Esc 关抽屉");
});

/* 〔5715 · 复钉 P6〕通知面板按 Esc 后**要能再打开**。
   原来的 bug：Esc 里除了摘 class 还写了内联 style.display="none"，
   而打开只加 class、不清内联样式 ⇒ 内联样式压过类规则 ⇒ 再也打不开。 */
test("通知面板：按 Esc 关掉后能再打开（内联 display 不得残留）", { skip: SKIP }, () => {
  const { document } = setupDom();
  initShortcuts();
  const np = document.getElementById("notifyPanel");
  // 模拟「打开」处理器的动作：加 class + 内联 display
  np.classList.add("open");
  np.style.display = "block";

  document.dispatchEvent(
    new document.defaultView.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.ok(!np.classList.contains("open"), "Esc 必须关掉面板");
  assert.notEqual(np.style.display, "none", "Esc 不得留下内联 display:none（那会让面板再也打不开）");

  // 再打开
  np.classList.add("open");
  np.style.display = "block";
  assert.ok(np.classList.contains("open"), "面板必须能再打开");
  assert.notEqual(np.style.display, "none", "再打开后不能是内联隐藏");
});

/* ================================================================ 代码块复制 */

test("代码块复制：点复制把代码原文写进剪贴板，并给出「已复制」反馈", { skip: SKIP }, async () => {
  const { document, window, clipboardWrites } = setupDom();
  const host = document.createElement("div");
  host.innerHTML =
    '<div class="code-block"><div class="code-head">' +
    '<button type="button" class="code-copy" data-copy="1"><span>复制</span></button></div>' +
    "<pre><code>const a = 1;</code></pre></div>";
  document.body.appendChild(host);
  bindCopyButtons(document);

  const btn = host.querySelector(".code-copy");
  btn.click();
  // clipboard.writeText 是 Promise ⇒ 等一拍
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(clipboardWrites, ["const a = 1;"], "复制内容必须是代码块的纯文本");
  assert.equal(btn.querySelector("span").textContent, "已复制", "必须有成功反馈（否则用户以为没点上）");
  assert.ok(btn.classList.contains("is-ok"), "成功态要有样式钩子");
});

/* 回退路径：没有 clipboard API 时走 execCommand —— 也要能成功反馈。
   〔实测坑〕jsdom 没有 execCommand，不 stub 的话回退路径里的异常被吞掉，
   「已复制」永不触发 ⇒ 测试会假绿。 */
test("代码块复制：没有剪贴板 API 时走 execCommand 回退，也要有成功反馈", { skip: SKIP }, () => {
  const { document, window } = setupDom();
  // 摘掉 clipboard（模拟老浏览器）
  try {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: undefined, configurable: true, writable: true,
    });
  } catch (e) { /* ignore */ }
  let execCalled = 0;
  document.execCommand = () => { execCalled += 1; return true; };

  const host = document.createElement("div");
  host.innerHTML =
    '<div class="code-block"><div class="code-head">' +
    '<button type="button" class="code-copy" data-copy="1"><span>复制</span></button></div>' +
    "<pre><code>x = 2;</code></pre></div>";
  document.body.appendChild(host);
  bindCopyButtons(document);
  host.querySelector(".code-copy").click();
  assert.equal(execCalled, 1, "没有剪贴板 API 时必须走 execCommand");
  assert.equal(host.querySelector(".code-copy span").textContent, "已复制", "回退路径也要有成功反馈");
});
