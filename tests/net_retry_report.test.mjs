/* tests/net_retry_report.test.mjs —— 「网络错误」机制网（[2026-10-25 · 治频繁网络错误]）
 *
 * 跑法：node --test tests/net_retry_report.test.mjs
 *
 * ## 背景（取证，不猜）
 *
 * 用户报：「整个页面频繁报网络错误」。前端唯一的「网络错误」出处是
 * `static/esm/api.js` 的 `call()`：`toast("网络错误：" + e.message)`。
 * Chrome 把它叫 **`TypeError: Failed to fetch`** ⇒ **连接层**失败。
 *
 * 实测根因（裸 socket，同一条连接隔 N 秒再发一次）：
 *   等 1s / 3s ⇒ 正常 200；等 6s / 8s ⇒ **WinError 10053 连接中止**。
 * ⇒ 服务端（uvicorn 默认 `timeout_keep_alive=5`）**空闲 5 秒就关掉 keep-alive 连接**，
 *   而浏览器把连接留在池里复用 ⇒ 下一次请求落在死连接上。
 * 而 api.js 原来**故意不重试非超时错误** ⇒ 瞬时竞态直接变成用户可见的「网络错误」。
 *
 * ## 网里钉的不变量
 *
 * | 编号 | 不变量 |
 * |---|---|
 * | N1 | **连接层失败重试一次**（GET）：第一次 TypeError、第二次成功 ⇒ 调用方拿到正常结果、**不弹 toast** |
 * | N2 | 重试也失败 ⇒ 仍然抛错**且**弹「网络错误」（不许把真失败吞掉） |
 * | N3 | 真失败要**上报到服务端**（`/api/client-errors`）——连接层失败服务端收不到请求 ⇒ 不主动上报就永远查不到 |
 * | N4 | **POST 绝不重试**（不幂等：重发＝重复落盘/重复发消息） |
 * | N5 | 超时仍重试（既有行为没被改坏） |
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

/** 假 fetch：按「前 N 次失败」的行为脚本化。 */
function installFetch({ failFirst = 0, alwaysFail = false, okStatus = 200 } = {}) {
  const calls = [];
  globalThis.fetch = async (input, init) => {
    const url = String((input && input.url) || input || "");
    const method = (init && init.method) || "GET";
    calls.push({ url, method });
    // 客户端错误上报那条永远放行（否则上报自己会失败，测的就不是被测物了）
    if (url.includes("/api/client-errors")) {
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    const shouldFail = alwaysFail || calls.length <= failFirst;
    if (shouldFail) {
      const e = new TypeError("Failed to fetch");
      throw e;
    }
    return { ok: true, status: okStatus, json: async () => ({ value: 42 }) };
  };
  return calls;
}

/**
 * 装一个**独立的** api.js 实例。
 * 为什么要独立实例：api.js 里有一个**可达性闩**（页面级状态——「后端不可达」要
 * 只提示一次），它是模块级变量 ⇒ 同一个实例被多个用例共用时，前一个用例把闩按下，
 * 后一个用例的「恢复」就会弹「服务已恢复」⇒ 用例之间互相污染。
 * ⇒ 每个用例用**不同的 cache key** ⇒ 各自一份模块实例（各自的闩）。
 */
async function loadApi(tag) {
  setupDom();
  const api = await import(`../static/esm/api.js?probe=neterr-${tag}`);
  const store = await import("../static/esm/store.js?v=30");
  return { api, store };
}

/** 清掉 toast 历史（store.notifyLog）。 */
function resetLog(store) {
  store.store.setState({ notifyLog: [] });
}

test("N1：连接层失败重试一次（GET）——成功就不该弹「网络错误」", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  const calls = installFetch({ failFirst: 1 });
  const data = await api.call("GET", "/api/probe");
  assert.deepEqual(data, { value: 42 });
  const hits = calls.filter((c) => c.url.includes("/api/probe"));
  assert.equal(hits.length, 2, "第一次 TypeError 之后应当重发一次");
  assert.equal(store.store.state.notifyLog.length, 0,
    "重试成功就不该有任何 toast（用户不该看到「网络错误」）");
});

test("N2：重试也失败 ⇒ 仍然抛错且弹「网络错误」（不许把真失败吞掉）", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  const calls = installFetch({ alwaysFail: true });
  await assert.rejects(() => api.call("GET", "/api/probe"));
  const hits = calls.filter((c) => c.url.includes("/api/probe"));
  assert.equal(hits.length, 2, "最多重试一次（不是无限重试）");
  const msgs = store.store.state.notifyLog.map((m) => m.msg);
  // [2026-10-25] 提示语改成「无法连接服务，正在重试…」（旧的是「网络错误：Failed to fetch」）
  // ⇒ **同一个故障只提示一次**，细节改落服务端日志（client_errors.jsonl）。
  // 判据是「**有提示**」而不是「提示长什么样」——不许把真失败吞掉。
  assert.ok(msgs.some((m) => /无法连接服务|网络错误|请求超时/.test(m)),
    `真失败必须提示用户（不许静默吞掉），实际：${JSON.stringify(msgs)}`);
});

test("N3：真失败要上报到服务端（连接层失败服务端收不到请求 ⇒ 不主动报就永远查不到）", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  const calls = installFetch({ alwaysFail: true });
  await assert.rejects(() => api.call("GET", "/api/probe"));
  const reports = calls.filter((c) => c.url.includes("/api/client-errors"));
  assert.ok(reports.length >= 1, "应上报客户端错误");
  for (const r of reports) assert.equal(r.method, "POST");
});

test("N3b：重试成功的**瞬时**失败也要上报（用户看不见的那些才是「频繁」的真相）", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  const calls = installFetch({ failFirst: 1 });
  const data = await api.call("GET", "/api/probe");
  assert.deepEqual(data, { value: 42 }, "重试应当成功");
  const reports = calls.filter((c) => c.url.includes("/api/client-errors"));
  assert.equal(reports.length, 1, "瞬时失败也要留痕（否则日志里看不到频率）");
  // 用户层面仍然零打扰
  assert.equal(store.store.state.notifyLog.length, 0, "瞬时失败不该弹 toast");
});

test("N4：POST 绝不重试（不幂等：重发＝重复落盘）", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  const calls = installFetch({ alwaysFail: true });
  await assert.rejects(() => api.call("POST", "/api/probe", { x: 1 }));
  const hits = calls.filter((c) => c.url.includes("/api/probe"));
  assert.equal(hits.length, 1, "POST 必须只发一次（原来 attempts=1 的约定不许被改坏）");
});

test("N5：超时仍重试（既有行为没被改坏）", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  let n = 0;
  globalThis.fetch = async (input, init) => {
    const url = String((input && input.url) || input || "");
    if (url.includes("/api/client-errors")) return { ok: true, status: 200, json: async () => ({}) };
    n++;
    if (n === 1) {
      // 真的挂住，直到 api.js **自己的**超时定时器把 signal abort 掉
      // （api.js 认「超时」靠的是它那个 timer，不是错误类型）
      return new Promise((_resolve, reject) => {
        const sig = init && init.signal;
        if (!sig) { reject(new Error("no signal passed")); return; }
        sig.addEventListener("abort", () => {
          const e = new Error("aborted by timeout");
          e.name = "AbortError";
          reject(e);
        });
      });
    }
    return { ok: true, status: 200, json: async () => ({ value: 7 }) };
  };
  const data = await api.call("GET", "/api/probe", undefined, { timeoutMs: 50 });
  assert.deepEqual(data, { value: 7 });
  assert.equal(n, 2, "超时后应重试一次");
  assert.equal(store.store.state.notifyLog.length, 0, "重试成功不该弹 toast");
});

/* ══════════════════════════════════════════════════════════════════════════
   可达性闩（2026-10-25 · 治「服务一关，网页就不停报网络错误」）

   病根实测：服务关掉后 120 秒内 47 个请求、30 个失败 ⇒ 旧逻辑**每个失败都弹一个
   toast** ⇒ 用户看到「每分钟两三个网络错误」。
   ⇒ 新逻辑：**同一个故障只报一次**（首次提示，之后静默），恢复时提示一次。
   ══════════════════════════════════════════════════════════════════════════ */

test("N6：服务不可达 ⇒ 只提示一次，后续失败一律静默（不刷屏）", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  installFetch({ alwaysFail: true });
  for (let i = 0; i < 5; i++) {
    await assert.rejects(() => api.call("GET", "/api/probe"));
  }
  const msgs = store.store.state.notifyLog.map((m) => m.msg);
  assert.equal(msgs.length, 1, `同一个故障只该提示一次，实得 ${JSON.stringify(msgs)}`);
  assert.match(msgs[0], /无法连接服务|请求超时/, msgs[0]);
});

test("N7：恢复 ⇒ 提示一次「服务已恢复」；之后再失败又只提示一次", { skip: SKIP }, async () => {
  const { api, store } = await loadApi("t" + Date.now() + Math.random().toString(36).slice(2, 8));
  resetLog(store);
  installFetch({ alwaysFail: true });
  await assert.rejects(() => api.call("GET", "/api/probe"));     // 不可达 ⇒ 提示一次
  // 恢复：换成成功的 fetch
  installFetch({});
  await api.call("GET", "/api/probe");                          // ⇒ 提示「服务已恢复」
  let msgs = store.store.state.notifyLog.map((m) => m.msg);
  assert.ok(msgs.some((m) => m.includes("服务已恢复")), JSON.stringify(msgs));
  // 恢复后再坏 ⇒ 又只提示一次（闩重新按下）
  const before = msgs.length;
  installFetch({ alwaysFail: true });
  await assert.rejects(() => api.call("GET", "/api/probe"));
  await assert.rejects(() => api.call("GET", "/api/probe"));
  msgs = store.store.state.notifyLog.map((m) => m.msg);
  assert.equal(msgs.length, before + 1, `再坏一次只该多提示一条：${JSON.stringify(msgs)}`);
});
