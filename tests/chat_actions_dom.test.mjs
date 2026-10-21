/* tests/chat_actions_dom.test.mjs —— 消息操作条与卡详情抽屉的 **DOM 行为网**（[5715]）
 *
 * 跑法：node --test tests/chat_actions_dom.test.mjs
 *
 * 〔5715 · 抽模块〕这两个面原先**长在渲染/装配闭包里**（stream.js 的用户消息渲染、
 * workspace.js 的助手消息渲染与卡详情开关）⇒ ① 导不出来，单测驱动不了；
 * ② 剪贴板回退逻辑在两处各写了一份。
 * ⇒ 先把逻辑**逐字**搬进可导入的模块（esm/flow/message-actions.js、esm/inspector.js），
 * 再用本网把行为钉住。搬动过程只改「谁持有这段代码」，不改行为。
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";
import { createCopyButton, createResendButton, copyText }
  from "../static/esm/flow/message-actions.js";
import { initInspectorToggle } from "../static/esm/inspector.js";

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

/* ================================================================ 复制按钮 */

test("复制按钮：把正文写进剪贴板，并给「已复制」反馈（成功后才给）", { skip: SKIP }, async () => {
  const { document, clipboardWrites } = setupDom();
  const btn = createCopyButton({ title: "复制", getText: () => "看这张卡" });
  document.body.appendChild(btn);

  btn.click();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(clipboardWrites, ["看这张卡"], "复制的必须是气泡正文");
  assert.ok(btn.classList.contains("is-ok"), "成功后要有成功态钩子");
});

test("复制按钮：正文为空时**不写剪贴板、不给反馈**（不假装复制过）", { skip: SKIP }, async () => {
  const { document, clipboardWrites } = setupDom();
  const btn = createCopyButton({ title: "复制", getText: () => "" });
  document.body.appendChild(btn);
  btn.click();
  await Promise.resolve();
  assert.deepEqual(clipboardWrites, [], "空正文不该写剪贴板");
  assert.ok(!btn.classList.contains("is-ok"), "没复制成就不该给「已复制」");
});

test("复制按钮：剪贴板 API 失败时**不给假反馈**（旧实现会谎报已复制）", { skip: SKIP }, async () => {
  const { document } = setupDom();
  // 让 clipboard.writeText 失败
  try {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("被拒")) },
      configurable: true, writable: true,
    });
  } catch (e) { /* ignore */ }
  const btn = createCopyButton({ title: "复制", getText: () => "正文" });
  document.body.appendChild(btn);
  btn.click();
  await Promise.resolve();
  await Promise.resolve();
  assert.ok(!btn.classList.contains("is-ok"), "复制失败不得显示「已复制」");
});

test("复制按钮：没有剪贴板 API 时走 execCommand 回退（老浏览器）", { skip: SKIP }, async () => {
  const { document } = setupDom();
  try {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: undefined, configurable: true, writable: true,
    });
  } catch (e) { /* ignore */ }
  let execCalled = 0;
  document.execCommand = () => { execCalled += 1; return true; };
  const btn = createCopyButton({ title: "复制", getText: () => "回退正文" });
  document.body.appendChild(btn);
  btn.click();
  await Promise.resolve();
  assert.equal(execCalled, 1, "必须走 execCommand 回退");
});

test("copyText：写入失败时返回 false（调用方据此决定要不要给反馈）", { skip: SKIP }, async () => {
  setupDom();
  try {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("被拒")) },
      configurable: true, writable: true,
    });
  } catch (e) { /* ignore */ }
  assert.equal(await copyText("x"), false, "失败必须如实回 false");
});

/* ================================================================ 重发按钮 */

test("重发按钮：点一下派发 ui:resend（内容＝气泡正文），宿主据此重发", { skip: SKIP }, () => {
  const { document, window } = setupDom();
  const got = [];
  window.addEventListener("ui:resend", (e) => got.push(e.detail));
  const btn = createResendButton({ getText: () => "刚才那句" });
  document.body.appendChild(btn);
  btn.click();
  assert.deepEqual(got, [{ content: "刚才那句" }], "重发必须把原文交给宿主");
  assert.equal(btn.title, "重新发送这条提问", "按钮要说明它做什么");
});

test("重发按钮：正文为空时不派发（没有可重发的东西）", { skip: SKIP }, () => {
  const { document, window } = setupDom();
  const got = [];
  window.addEventListener("ui:resend", (e) => got.push(e.detail));
  const btn = createResendButton({ getText: () => "" });
  document.body.appendChild(btn);
  btn.click();
  assert.deepEqual(got, [], "空正文不该派发重发");
});

/* ================================================================ 卡详情抽屉 */

test("卡详情抽屉：右缘竖条与顶栏按钮**共用一个状态**（一个开一个就开）", { skip: SKIP }, () => {
  const { document } = setupDom();
  const insp = document.createElement("div");
  insp.id = "inspector";
  const tab = document.createElement("button");
  tab.id = "inspTab";
  const top = document.createElement("button");
  top.id = "topInspBtn";
  document.body.append(insp, tab, top);
  initInspectorToggle();

  assert.ok(!insp.classList.contains("open"), "初始必须是关的");
  tab.click();
  assert.ok(insp.classList.contains("open"), "右缘竖条必须能开");
  top.click();
  assert.ok(!insp.classList.contains("open"), "顶栏按钮是同一动作 ⇒ 再点一下就关（不是两处各开各的）");
  top.click();
  assert.ok(insp.classList.contains("open"), "两个入口指向同一状态");
});

test("卡详情抽屉：缺节点时安静退场（不抛）", { skip: SKIP }, () => {
  const { document } = setupDom();
  assert.doesNotThrow(() => initInspectorToggle(), "缺 #inspector 时不得抛");
});
