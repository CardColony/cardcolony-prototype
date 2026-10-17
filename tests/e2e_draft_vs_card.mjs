/* [建项目流程 2026-10-17 · V3 回归网] 草稿页 vs 真卡页：同一动作序列，不可区分。
 *
 * 这是**测试**，不是手工探针：以后这条链的回归由它守，不靠人跑脚本。
 *
 * 跑法（需要沙箱实例，未设环境变量则整组跳过）：
 *   ECO_E2E_BASE=http://127.0.0.1:5703 node --test tests/e2e_draft_vs_card.mjs
 *
 * 三轴：
 *   ① DOM 形态（气泡结构/类名）逐字一致
 *   ② 网络形态（方法+路径模式）除「草稿侧多一次 POST /api/works」外一致
 *   ③ 时序：把落库请求替换成即时返回后，两侧气泡上屏差 ≤100ms
 *      （不替换时草稿侧必然多等一次真实建项目往返——那是后端耗时，不是前端路径分叉；
 *        实测前端固定开销 4–12ms，见施工回报「边界①」）
 */
import { test } from "node:test";
import assert from "node:assert";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.env.ECO_E2E_BASE || "";
const CDP = Number(process.env.ECO_E2E_CDP_PORT || 0) || 9340 + Math.floor(Math.random() * 40);
const CHROME = join(process.env.LOCALAPPDATA || "", "ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const SKIP = BASE ? false : "未设 ECO_E2E_BASE（沙箱实例地址），跳过真机对照";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitJson(url, tries = 150) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
    } catch (e) { /* wait */ }
    await sleep(100);
  }
  throw new Error("CDP 未就绪: " + url);
}

class Sess {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []; this.hooks = new Map();
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) { const f = this.pending.get(m.id); this.pending.delete(m.id); f(m); }
      else if (m.method) {
        this.events.push([m.method, m.params || {}]);
        const h = this.hooks.get(m.method);
        if (h) h(m.params || {}).catch(() => {});
      }
    });
  }
  on(m, fn) { this.hooks.set(m, fn); }
  cmd(method, params = {}, timeout = 30000) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      const t = setTimeout(() => { this.pending.delete(id); rej(new Error("timeout " + method)); }, timeout);
      this.pending.set(id, (m) => {
        clearTimeout(t);
        if (m.error) rej(new Error(method + ": " + JSON.stringify(m.error))); else res(m.result || {});
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async js(expr) {
    const r = await this.cmd("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("JS: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
    return r.result.value;
  }
  clear() { this.events.length = 0; }
  posts() {
    return this.events.filter(([m]) => m === "Network.requestWillBeSent")
      .map(([, p]) => p.request || {}).filter((r) => r.method === "POST").map((r) => r.url);
  }
  async waitLoad(timeout = 20000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      const i = this.events.findIndex(([m]) => m === "Page.loadEventFired");
      if (i >= 0) { this.events.splice(0, i + 1); return true; }
      await sleep(50);
    }
    return false;
  }
  async nav(url, settle = 2000) {
    this.clear();
    await this.cmd("Page.navigate", { url });
    if (!(await this.waitLoad())) throw new Error("navigate 超时 " + url);
    await sleep(settle);
  }
}

const ARM = `(() => {
  const sel = '#chatArea .flow-user-row, #chatArea .msg.user';
  const shape = (n, d) => ({ t: n.tagName.toLowerCase(), c: [...n.classList].sort().join('.'),
    k: d > 0 ? [...n.children].map((c) => shape(c, d - 1)) : [] });
  window.__probe = { t0: 0, bubbleAt: null, shape: null };
  const check = () => {
    if (window.__probe.bubbleAt !== null) return;
    const el = document.querySelector(sel);
    if (el) {
      window.__probe.bubbleAt = performance.now() - window.__probe.t0;
      window.__probe.shape = shape(el, 3);
    }
  };
  new MutationObserver(check).observe(document.getElementById('chatArea'), { childList: true, subtree: true });
  return true;
})()`;

async function typeSend(s, text) {
  await s.js(`(() => { const ta=document.querySelector('.flow-input-ta'); if(!ta) return false; ta.focus(); return true; })()`);
  await s.cmd("Input.insertText", { text });
  await sleep(120);
  await s.js(ARM);
  await s.js(`(() => { const b=document.querySelector('.flow-input-primary');
    if(!b) return false; window.__probe.t0=performance.now(); b.click(); return true; })()`);
  const t0 = Date.now();
  while (Date.now() - t0 < 8000) {
    const v = await s.js("window.__probe ? window.__probe.bubbleAt : null");
    if (v !== null && v !== undefined) return { t: v, shape: await s.js("window.__probe.shape") };
    await sleep(60);
  }
  return { t: null, shape: null };
}

const norm = (u) => u.replace(/127\.0\.0\.1:\d+/, "{host}")
  .replace(/work_\d+/g, "{wid}").replace(/card_\d+/g, "{cid}").replace(/new-\d+/g, "{board}");

test("V3 草稿页与真卡页不可区分（DOM / 网络 / 时序三轴）", { skip: SKIP, timeout: 180000 }, async () => {
  const cr = await fetch(BASE + "/api/works", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "V3 回归对照" }),
  });
  assert.ok(cr.ok, "建对照项目失败");
  const w = await cr.json();
  const wid = w.work_id, root = w.work_card_id || "";
  assert.ok(root, "对照项目缺根卡");

  const profile = mkdtempSync(join(tmpdir(), "e2e_v3_"));
  const proc = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=${CDP}`, `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-gpu",
    "--disable-dev-shm-usage", "--remote-allow-origins=*", "--window-size=1440,900",
    "about:blank",
  ], { stdio: "ignore" });

  let ws;
  try {
    await waitJson(`http://127.0.0.1:${CDP}/json/version`);
    const tab = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: "PUT" })).json();
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", () => rej(new Error("ws error")), { once: true });
    });
    const s = new Sess(ws);
    await s.cmd("Page.enable");
    await s.cmd("Runtime.enable");
    await s.cmd("Network.enable");

    // 两侧统一拦截：/messages 立即 200（不真调模型）；/api/works 可切换为即时 201
    let mockCreate = false;
    s.on("Fetch.requestPaused", async (p) => {
      const u = p.request.url, isPost = p.request.method === "POST";
      if (mockCreate && isPost && u.endsWith("/api/works")) {
        await s.cmd("Fetch.fulfillRequest", {
          requestId: p.requestId, responseCode: 201,
          responseHeaders: [{ name: "Content-Type", value: "application/json" }],
          body: Buffer.from(JSON.stringify({ work_id: "work_999", work_card_id: "card_999" })).toString("base64"),
        });
        return;
      }
      if (isPost && u.endsWith("/messages")) {
        await s.cmd("Fetch.fulfillRequest", {
          requestId: p.requestId, responseCode: 200,
          responseHeaders: [{ name: "Content-Type", value: "application/json" }],
          body: Buffer.from(JSON.stringify({ ok: true, created_cards: [], tool_events: [] })).toString("base64"),
        });
        return;
      }
      await s.cmd("Fetch.continueRequest", { requestId: p.requestId });
    });
    await s.cmd("Fetch.enable", { patterns: [
      { urlPattern: "*/messages", requestStage: "Request" },
      { urlPattern: "*/api/works", requestStage: "Request" },
    ] });

    // 真卡页
    await s.nav(`${BASE}/work/${wid}/card/${root}`, 2400);
    await s.clear();
    const rCard = await typeSend(s, "对照消息");
    await sleep(1200);
    const postsCard = s.posts().map(norm);

    // 草稿页（真实落库）
    await s.nav(BASE + "/work/draft", 2200);
    await s.clear();
    const rDraft = await typeSend(s, "对照消息");
    await sleep(1200);
    const postsDraft = s.posts().map(norm);

    // 草稿页（落库即时返回）——隔离后端耗时
    mockCreate = true;
    await s.nav(BASE + "/work/draft", 2200);
    await s.clear();
    const rFast = await typeSend(s, "对照消息");
    await sleep(800);
    mockCreate = false;

    // ── 轴1 时序 ──
    assert.ok(rCard.t !== null, "真卡页气泡未上屏");
    assert.ok(rFast.t !== null, "草稿页（落库即时返回）气泡未上屏");
    const dt = Math.abs(rFast.t - rCard.t);
    assert.ok(dt <= 100,
      `时序轴：草稿 ${rFast.t.toFixed(0)}ms vs 真卡 ${rCard.t.toFixed(0)}ms，差 ${dt.toFixed(0)}ms > 100ms`);

    // ── 轴2 DOM 形态 ──
    assert.ok(rCard.shape, "真卡页未抓到气泡形态");
    assert.deepStrictEqual(rDraft.shape, rCard.shape,
      "DOM 轴：草稿页气泡结构与真卡页不一致\n真卡 " + JSON.stringify(rCard.shape) + "\n草稿 " + JSON.stringify(rDraft.shape));

    // ── 轴3 网络形态 ──
    const stripped = postsDraft.filter((u) => !u.endsWith("/api/works"));
    assert.deepStrictEqual(stripped, postsCard,
      "网络轴：除建项目外的请求形态不一致\n真卡 " + JSON.stringify(postsCard) + "\n草稿 " + JSON.stringify(stripped));
    assert.strictEqual(postsDraft.filter((u) => u.endsWith("/api/works")).length, 1,
      "网络轴：草稿侧建项目请求不是恰好 1 次");

    // 记录真实落库下的时序（不设阈值，供报告引用）
    if (rDraft.t !== null) {
      console.log(`[V3] 真实落库：草稿 ${rDraft.t.toFixed(0)}ms / 真卡 ${rCard.t.toFixed(0)}ms`
        + `；落库即时返回：草稿 ${rFast.t.toFixed(0)}ms（后端建项目耗时 = ${(rDraft.t - rFast.t).toFixed(0)}ms）`);
    }
  } finally {
    try { if (ws) ws.close(); } catch (e) { /* ignore */ }
    proc.kill();
    await sleep(300);
    try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  }
});
