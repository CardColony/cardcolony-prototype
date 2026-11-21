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

/* ══════════════════════════════════════════════════════════════════════════
   S6–S8：2026-11-20 返工项 —— 共享条目的三个时序缺口
   （这三个都能独立复现，且都能**先红后绿**）
   ══════════════════════════════════════════════════════════════════════════ */

test("S6：最后一位消费者取消后**立即**（同步）重开同一 URL ⇒ 新请求不能接到已取消的条目", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  let hits = 0;
  globalThis.fetch = async (url, init) => {
    hits += 1;
    if (hits === 1) {
      // 第一条：等着被 abort（模拟服务端还没回）
      await new Promise((_, rej) => {
        init.signal.addEventListener("abort", () => {
          const e = new Error("aborted"); e.name = "AbortError"; rej(e);
        });
      });
    }
    return new Response(JSON.stringify({ ok: true }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
  const c1 = new AbortController();
  const p1 = call("GET", "/api/reopen", undefined, { signal: c1.signal });
  p1.catch(() => { });                 // 不让未接的 rejection 变成 unhandledrejection
  c1.abort();                         // 最后一位消费者退订
  /* ★ **同步**重开（不 await 任何东西）：原来条目只在 promise 的 finally 里摘表，
     而 finally 是**微任务** ⇒ 这一刻表里还挂着那条**已取消**的条目
     ⇒ 新请求会并上去 ⇒ 直接 AbortError（等于这次读没发生）。 */
  const p2 = call("GET", "/api/reopen");
  const v = await p2;
  assert.equal(hits, 2,
    "重开必须是**新的一条**请求（原来会并到已取消的条目上 ⇒ 直接 AbortError）");
  assert.deepEqual(v, { ok: true });
});

test("S7：响应头到达后取消 ⇒ **正文读取**也要被取消（不能只停消费者）", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  let bodyAborted = false;
  globalThis.fetch = async (url, init) => {
    // 头先到、正文还在流（首屏那条时序就是 1.3MB ⇒ 漏取消＝白拉一整条）
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"ok":'));
        init.signal.addEventListener("abort", () => {
          bodyAborted = true;
          try { controller.error(new Error("aborted")); } catch (e) { /* 已关 */ }
        });
      },
    });
    return new Response(stream, {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
  const c = new AbortController();
  const p = call("GET", "/api/bodycancel", undefined, { signal: c.signal });
  await new Promise((r) => setTimeout(r, 40));    // 等头到、正文开始读
  c.abort();
  await assert.rejects(p, (e) => e && e.name === "AbortError");
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(bodyAborted, true,
    "头到之后取消 ⇒ 底层正文读取必须一起停（原来头一到就解除了 abort 连线）");
});

test("S8：写**期间**发起的 GET 不能被提交后的重读复用 ⇒ 保存成功后必须读到新值", { skip: SKIP }, async () => {
  const { call } = await freshApi();
  let value = "old";
  let hits = 0;
  globalThis.fetch = async (url, init) => {
    hits += 1;
    const v = value;                       // 取**发起时**的值（服务端语义）
    if (init.method === "GET") {
      await new Promise((r) => setTimeout(r, 60));   // 保持它在途
      return new Response(JSON.stringify({ v }), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
    }
    // 写：在途 60ms（让「写期间」这个窗口真的存在）
    await new Promise((r) => setTimeout(r, 60));
    return new Response(JSON.stringify({ ok: true }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
  const putP = call("PUT", "/api/save", { v: "new" });   // 写在途
  await new Promise((r) => setTimeout(r, 15));          // 让写还没落定
  const duringP = call("GET", "/api/save");              // ★ **写期间**发起
  await putP;                                            // 写落定
  value = "new";
  const afterP = call("GET", "/api/save");               // 提交后重读
  const [during, after] = await Promise.all([duringP, afterP]);
  assert.equal(during.v, "old", "写期间发起的读拿旧值（正常）");
  assert.equal(after.v, "new",
    "提交后的重读必须拿到**新值**（原来纪元在写**之前**推进 ⇒ 写期间那条与重读同纪元 ⇒ 重读并上去 ⇒ 读到旧值）");
  assert.equal(hits, 3, "两条 GET 必须各走一次网络（不能合并）");
});
