/* tests/exec_backend_dom.test.mjs —— coder 卡**执行体块**的渲染网（[wb_exec 2026-10-23]）
 *
 * 跑法：node --test tests/exec_backend_dom.test.mjs
 *
 * 覆盖三件用户天天要看的：
 *   ① 未开工 ⇒ 显示 auto（设置值）＋「未开工」提示
 *   ② 开工后 ⇒ 显示**实际路由到的**（友好标签，不是 executor id）
 *   ③ 随时能改 ⇒ 下拉里 auto / 系统自己的模型 / 每个 WorkBuddy 模型都在
 *
 * 纯函数渲染（零 DOM），故用普通 node --test（不引 jsdom）。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { buildExecBackendHtml, routedLabel }
  from "../static/esm/exec_backend.js";

const OPTIONS = [
  { value: "auto", label: "自动（按配比）" },
  { value: "system", label: "系统自己的模型" },
  { value: "wb-intl:deepseek-v4.1-flash", label: "WorkBuddy AI 国际版 · deepseek-v4.1-flash",
    executor: "wb-intl", model: "deepseek-v4.1-flash" },
  { value: "wb-cn:hy3", label: "WorkBuddy 国内版 · hy3",
    executor: "wb-cn", model: "hy3" },
];


/* ══════════════════════════════════════════════ 未开工 */

test("未开工：下拉选中 auto，状态行说「未开工」", () => {
  const html = buildExecBackendHtml({
    backend: "auto", routed: null, options: OPTIONS, enabled: true,
  });
  assert.match(html, /<option value="auto" selected>/, "auto 必须是被选中的那个");
  assert.match(html, /未开工/, "状态行要说明还没开工");
  assert.match(html, /开工后这里显示实际路由到的模型/, "要告诉人开工后会显示什么");
  assert.doesNotMatch(html, /本次路由/, "没开工就不该有「本次路由」");
});


/* ══════════════════════════════════════════════ 开工后 */

test("开工后：状态行显示实际路由到的（友好标签）", () => {
  const html = buildExecBackendHtml({
    backend: "auto", enabled: true, options: OPTIONS,
    routed: { executor: "wb-intl", model: "deepseek-v4.1-flash" },
  });
  assert.match(html, /本次路由 → WorkBuddy AI 国际版 · deepseek-v4\.1-flash/,
    "要显示版本名＋模型，不是 executor id");
  assert.doesNotMatch(html, /wb-intl ·/, "别把 executor id 甩给人看");
  // **设置与实际分开**：下拉仍显示你设的 auto
  assert.match(html, /<option value="auto" selected>/,
    "下拉显示的是「设置」，不是「实际」");
});

test("钉死某个模型：下拉选中它，状态行照旧显示实际路由到的", () => {
  const html = buildExecBackendHtml({
    backend: "wb-cn:hy3", enabled: true, options: OPTIONS,
    routed: { executor: "wb-cn", model: "hy3" },
  });
  assert.match(html, /<option value="wb-cn:hy3" selected>/, "钉死的那个必须选中");
  assert.match(html, /本次路由 → WorkBuddy 国内版 · hy3/, "状态行显示实际");
});

test("钉死的设置、但实际路由到了别的（fallback 换过）：两个都分得清", () => {
  const html = buildExecBackendHtml({
    backend: "wb-intl:deepseek-v4.1-flash", enabled: true, options: OPTIONS,
    routed: { executor: "wb-cn", model: "hy3" },
  });
  assert.match(html, /<option value="wb-intl:deepseek-v4\.1-flash" selected>/);
  assert.match(html, /本次路由 → WorkBuddy 国内版 · hy3/, "fallback 后的实际也要显示");
});


/* ══════════════════════════════════════════════ 随时可改 */

test("下拉里 auto / 系统自己的模型 / 每个 WorkBuddy 模型都在", () => {
  const html = buildExecBackendHtml({
    backend: "auto", routed: null, options: OPTIONS, enabled: true,
  });
  for (const v of ["auto", "system", "wb-intl:deepseek-v4.1-flash", "wb-cn:hy3"]) {
    assert.ok(html.includes(`value="${v}"`), `下拉里缺 ${v}`);
  }
  assert.match(html, /切换在下一回合生效/, "要说清什么时候生效");
});

test("切到「系统自己的模型」：下拉选中 system", () => {
  const html = buildExecBackendHtml({
    backend: "system", routed: { executor: "wb-cn", model: "hy3" },
    options: OPTIONS, enabled: true,
  });
  assert.match(html, /<option value="system" selected>/);
});


/* ══════════════════════════════════════════════ 边界 */

test("执行体未启用：提示「当前全部走系统自己的模型」", () => {
  const html = buildExecBackendHtml({
    backend: "auto", routed: null, options: OPTIONS, enabled: false,
  });
  assert.match(html, /执行体未启用/, "要说明为什么没生效");
  assert.match(html, /当前全部走系统自己的模型/);
});

test("routed 缺字段时不炸，也不显示「本次路由」", () => {
  for (const bad of [null, {}, { executor: "wb-cn" }, { model: "hy3" }]) {
    const html = buildExecBackendHtml({
      backend: "auto", routed: bad, options: OPTIONS, enabled: true,
    });
    assert.doesNotMatch(html, /本次路由/, `routed=${JSON.stringify(bad)} 不该显示本次路由`);
    assert.match(html, /未开工/, "缺字段时按未开工显示");
  }
});

test("options 为空也不炸", () => {
  const html = buildExecBackendHtml({ backend: "auto", routed: null, options: [] });
  assert.match(html, /<select id="execBackendSel"/);
});

test("委派回落系统模型：状态行**如实**显示「本轮实际走系统自己的模型（原因）」", () => {
  // 假信号防线：委派失败会静默回落系统路径，卡面不能还显示成"在 WorkBuddy 上跑"。
  const html = buildExecBackendHtml({
    backend: "auto", enabled: true, options: OPTIONS,
    routed: { executor: "", model: "", fallback: "system",
              reason: "网关不可用（wb-intl）: 找不到运行中网关" },
  });
  assert.match(html, /本轮实际/, "应显示「本轮实际」");
  assert.match(html, /系统自己的模型/, "应显示走的是系统自己的模型");
  assert.match(html, /找不到运行中网关/, "应带出原因");
  assert.doesNotMatch(html, /本次路由/, "回落时不该显示「本次路由」");
});

test("routedLabel：找不到对应候选用 executor · model 兜底", () => {
  const lb = routedLabel({
    options: [], routed: { executor: "wb-x", model: "m1" },
  });
  assert.equal(lb, "wb-x · m1");
});

test("总开关：开着 ⇒ 复选框勾上、下拉可用", () => {
  const html = buildExecBackendHtml(
    { backend: "auto", routed: null, options: OPTIONS, enabled: true },
    { enabled: true });
  assert.match(html, /id="execBackendSwitch"/);
  assert.match(html, /checked/);
  assert.doesNotMatch(html, /id="execBackendSel" class="exec-sel" disabled/);
});

test("总开关：关着 ⇒ 复选框不勾、下拉置灰、提示走系统模型", () => {
  const html = buildExecBackendHtml(
    { backend: "auto", routed: null, options: OPTIONS, enabled: true },
    { enabled: false });
  assert.match(html, /id="execBackendSwitch"/);
  assert.doesNotMatch(html, /id="execBackendSwitch" checked/);
  assert.match(html, /id="execBackendSel" class="exec-sel" disabled/);
  assert.match(html, /总开关已关闭/);
  assert.match(html, /走系统自己的模型/);
});

test("总开关：没给状态 ⇒ 退回配置态（不崩）", () => {
  const html = buildExecBackendHtml(
    { backend: "auto", routed: null, options: OPTIONS, enabled: true });
  assert.match(html, /id="execBackendSwitch"/);
});
