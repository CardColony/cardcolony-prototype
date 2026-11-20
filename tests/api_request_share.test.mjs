/* tests/api_request_share.test.mjs —— 网络层「在途 GET 合并」的网（2026-11-20 性能批次一）
 *
 * 跑法：node --test tests/api_request_share.test.mjs
 *
 * ## 为什么有这张网
 *
 * 冷开时同一份读口被打两次（实测：/rule 面板 + 审批探针各一次；/progress
 * 开卡 + 刷新恢复各一次）⇒ 白走网络、白占连接、白占服务端 IO。
 * 修法在 api.js：**同 URL 的并发 GET 合成一次**。
 *
 * ## 网里钉的不变量
 *
 * | 编号 | 不变量 |
 * |---|---|
 * | S1 | 同一 URL 的并发 GET ⇒ 只打一次网络，两个消费者拿到同一份数据 |
 * | S2 | 一个消费者退订（abort）⇒ **只摘它自己**；别人还要的那条请求继续跑完 |
 * | S3 | 所有消费者都退订 ⇒ 底层请求**真被取消**（浏览器连接被释放） |
 * | S4 | 写操作推进「纪元」⇒ 写前与写后的同一个 GET **不合并**（提交后重读必须拿到新值） |
 * | S5 | ``silent:true`` 的读口不弹错误 toast（周期探活不刷屏） |
 *
 * 依赖：jsdom（仅测试用）。api.js 的模块级状态是单例 ⇒ 每个用例用
 * ``?probe=N`` 拿**独立实例**，账是干净的。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

let _seq = 0;
/** 拿一份干净的 api.js 实例（模块级账是单例 ⇒ 必须换实例）。 */
async function freshApi() {
  setupDom();
  _seq += 1;
  return await import(`../static/esm/api.js?probe=${_seq}`);
}

/** 装一个可计数的 fetch：每次调用记录 url，返回给定的 JSON。 */
function stubFetch(body = { ok: true }, { delayMs = 20, onSignal = null } = {}) {
  const hits = [];
  globalThis.fetch = async (url, init) => {
    hits.push({ url: String(url), init });
    if (onSignal && init && init.signal) onSignal(init.signal);
    await new Promise((r) => setTimeout(r, delayMs));
    return new Response(JSON.stringify(body), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
  return hits;
}

test("S1：同一 URL 的并发 GET 只打一次网络，两个消费者拿到同一份数据", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  const hits = stubFetch({ ok: true, v: 7 });
  const [a, b] = await Promise.all([
    call("GET", "/api/works/w1/rule"),
    call("GET", "/api/works/w1/rule"),
  ]);
  assert.equal(hits.length, 1, `并发同 URL 必须合成一次（实际打了 ${hits.length} 次）`);
  assert.deepEqual(a, { ok: true, v: 7 });
  assert.deepEqual(b, { ok: true, v: 7 });
});

test("S1b：不同 URL 不合并（两个口各打各的）", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  const hits = stubFetch({ ok: true });
  await Promise.all([
    call("GET", "/api/works/w1/rule"),
    call("GET", "/api/works/w1/cards/c1/progress"),
  ]);
  assert.equal(hits.length, 2, "不同 URL 不能合并");
});

test("S2：一个消费者退订只摘它自己——别人还要的那条请求继续跑完", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  let aborted = false;
  const hits = stubFetch({ ok: true, v: 1 }, {
    delayMs: 40,
    onSignal: (sig) => sig.addEventListener("abort", () => { aborted = true; }),
  });
  const c1 = new AbortController();
  const p1 = call("GET", "/api/works/w1/progress", undefined, { signal: c1.signal });
  const p2 = call("GET", "/api/works/w1/progress");
  c1.abort();
  await assert.rejects(p1, (e) => e && e.name === "AbortError",
    "退订的消费者必须拿到 AbortError（它的晚回包不许落到界面上）");
  const v = await p2;
  assert.equal(hits.length, 1, "两个消费者只应打一次网络");
  assert.equal(aborted, false, "还有消费者要 ⇒ 底层请求不能被取消");
  assert.deepEqual(v, { ok: true, v: 1 }, "留下来的消费者必须拿到数据");
});

test("S3：所有消费者都退订 ⇒ 底层请求真被取消（连接被释放）", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  let aborted = false;
  stubFetch({ ok: true }, {
    delayMs: 60,
    onSignal: (sig) => sig.addEventListener("abort", () => { aborted = true; }),
  });
  const c1 = new AbortController();
  const c2 = new AbortController();
  const p1 = call("GET", "/api/works/w1/card", undefined, { signal: c1.signal });
  const p2 = call("GET", "/api/works/w1/card", undefined, { signal: c2.signal });
  c1.abort();
  c2.abort();
  await assert.rejects(p1, (e) => e && e.name === "AbortError");
  await assert.rejects(p2, (e) => e && e.name === "AbortError");
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(aborted, true, "没人要了 ⇒ 必须 abort 底层 fetch（否则白占浏览器那 6 条连接）");
});

test("S4：写操作推进纪元 ⇒ 写前与写后的同一个 GET 不合并", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  const hits = stubFetch({ ok: true }, { delayMs: 30 });
  const before = call("GET", "/api/works/w1/rule");     // 写在途
  await call("PUT", "/api/works/w1/rule", { x: 1 });    // 写 ⇒ 纪元推进
  const after = call("GET", "/api/works/w1/rule");      // 写后 ⇒ 不能拿旧的那条
  await before;
  await after;
  const gets = hits.filter((h) => h.url.startsWith("/api/works/w1/rule") && h.init.method === "GET");
  assert.equal(gets.length, 2,
    "提交后重读必须重新取（不能并到提交前那条在途请求上——那会拿到提交前的旧值）");
});

test("S5：silent 的读口不弹错误 toast", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  const box = document.createElement("div");
  box.id = "toastContainer";
  document.body.appendChild(box);
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "炸了" }), {
    status: 500, headers: { "Content-Type": "application/json" },
  });
  await assert.rejects(call("GET", "/api/probe", undefined, { silent: true }));
  assert.equal(box.querySelectorAll(".toast").length, 0,
    "silent 的读口不该弹 toast（周期探活会刷屏）");
  await assert.rejects(call("GET", "/api/probe2"));
  assert.equal(box.querySelectorAll(".toast").length, 1,
    "不 silent 的读口失败要照旧弹 toast");
});
