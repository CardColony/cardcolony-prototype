/* tests/chat_perf.mjs —— 对话页（card.html）冷开/热切性能基准
 *
 * 用法：
 *   node tests/chat_perf.mjs <baseUrl> <workId> <cardId> [runs] [--json out.json]
 *
 * 口径（与施工方案「11 月 20 日初测」一致）：
 *   · 冷开 = **新浏览器上下文**（独立 user-data-dir ⇒ 缓存/会话存储全空）
 *     从导航起点到「首个对话行挂载后的第二个动画帧」。
 *   · 网络量 = 该次回包字节（loadingFinished 的 encodedDataLength）。
 *   · 长任务 = PerformanceObserver('longtask') 采到的主线程长任务。
 *
 * 只读：全程只发 GET 与页面导航，不点任何写按钮（不落盘、不改生产数据）。
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const RUNS = parseInt(process.argv[5] || "20", 10);
const JSON_OUT = (() => {
  const i = process.argv.indexOf("--json");
  return i > 0 ? process.argv[i + 1] : "";
})();
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const W = parseInt(process.env.PERF_W || "1440", 10);
const H = parseInt(process.env.PERF_H || "900", 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 页面内探针：首个对话行 + 长任务 + 导航时刻。
   在文档创建前注入 ⇒ 抢在模块脚本之前开始观察。 */
const PROBE = `
(() => {
  const P = { navStart: performance.now(), firstRowMs: null, rowsAtFirst: 0,
              longTasks: [], marks: {} };
  window.__PERF__ = P;
  try {
    const po = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        P.longTasks.push({ start: Math.round(e.startTime), dur: Math.round(e.duration) });
      }
    });
    po.observe({ entryTypes: ["longtask"] });
  } catch (e) { P.longTasksUnsupported = true; }
  const CHROME = new Set(["flow-view-older","chat-empty","chat-skeleton","flow-turn-status"]);
  const isRow = (el) => el && el.nodeType === 1 && el.classList &&
    !CHROME.has(el.className) && el.className !== "" &&
    !el.classList.contains("flow-view-older") && !el.classList.contains("chat-empty") &&
    !el.classList.contains("chat-skeleton") && !el.classList.contains("flow-turn-status");
  const check = () => {
    if (P.firstRowMs !== null) return;
    const col = document.querySelector(".flow-view-column");
    if (!col) return;
    for (const ch of col.children) {
      if (isRow(ch) && (ch.textContent || "").trim().length > 0) {
        P.firstRowMs = performance.now();
        P.rowsAtFirst = col.children.length;
        P.markFirst = ch.className;
        // 「挂载后的第二个动画帧」＝ 再等两帧，才算可读
        requestAnimationFrame(() => requestAnimationFrame(() => {
          P.settledMs = performance.now();
        }));
        return;
      }
    }
  };
  const start = () => {
    try {
      new MutationObserver(check).observe(document.documentElement,
        { childList: true, subtree: true, characterData: true });
    } catch (e) {}
    check();
    setInterval(check, 30);
  };
  if (document.documentElement) start();
  else document.addEventListener("readystatechange", start, { once: true });
})();
`;

/* ── CDP 客户端 ─────────────────────────────────────────────── */
async function openChrome(port, profileDir) {
  const child = spawn(CHROME, [
    "--remote-debugging-port=" + port, "--headless=new", "--disable-gpu", "--no-sandbox",
    "--hide-scrollbars", "--window-size=" + W + "," + H,
    "--disable-extensions", "--no-first-run", "--disable-background-timer-throttling",
    "--user-data-dir=" + profileDir, "about:blank",
  ], { stdio: "ignore" });
  let tr = null;
  for (let i = 0; i < 100 && !tr; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
      if (r.ok) tr = await r.json();
    } catch { /* wait */ }
    if (!tr) await sleep(200);
  }
  if (!tr) { child.kill(); throw new Error("Chrome 未起来"); }
  return { child, target: tr };
}

function cdp(ws) {
  let id = 0;
  const pending = new Map();
  const listeners = [];
  const send = (m, p = {}) => new Promise((res) => {
    const i = ++id;
    pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method: m, params: p }));
  });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    for (const fn of listeners) fn(m);
  };
  return { send, on: (fn) => listeners.push(fn) };
}

/** 跑一轮冷开。返回该轮的读数。 */
async function oneRun(idx) {
  const port = 9800 + Math.floor(Math.random() * 400);
  const profileDir = path.join(os.tmpdir(), "chatperf_" + Date.now() + "_" + idx);
  const { child, target } = await openChrome(port, profileDir);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res) => { ws.onopen = res; });
  const { send, on } = cdp(ws);

  const reqs = new Map();
  on((m) => {
    if (m.method === "Network.requestWillBeSent") {
      reqs.set(m.params.requestId, {
        url: m.params.request.url, method: m.params.request.method,
        t0: m.params.timestamp, size: 0, status: null,
      });
    } else if (m.method === "Network.responseReceived") {
      const r = reqs.get(m.params.requestId);
      if (r) { r.status = m.params.response.status; r.type = m.params.type; }
    } else if (m.method === "Network.loadingFinished") {
      const r = reqs.get(m.params.requestId);
      if (r) r.size = m.params.encodedDataLength;
    }
  });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");
  await send("Emulation.setDeviceMetricsOverride",
    { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send("Page.addScriptToEvaluateOnNewDocument", { source: PROBE });

  await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });

  // 等首批对话行 + 第二个动画帧（最多 30s）
  let perf = null;
  for (let i = 0; i < 600; i++) {
    await sleep(50);
    const r = await send("Runtime.evaluate", {
      expression: "JSON.stringify(window.__PERF__ ? {firstRowMs:window.__PERF__.firstRowMs, settledMs:window.__PERF__.settledMs, longTasks:window.__PERF__.longTasks, rowsAtFirst:window.__PERF__.rowsAtFirst, markFirst:window.__PERF__.markFirst} : null)",
      returnByValue: true,
    });
    const v = r.result && r.result.result ? r.result.result.value : null;
    if (v) {
      const o = JSON.parse(v);
      if (o && o.settledMs != null) { perf = o; break; }
    }
  }
  // 再等一会儿，让剩余请求落地（统计完整字节）
  await sleep(600);

  const dom = await send("Runtime.evaluate", {
    expression: "JSON.stringify({dom:document.querySelectorAll('*').length, rows:document.querySelectorAll('.flow-view-column > *').length, body:document.body.innerHTML.length})",
    returnByValue: true,
  });
  const domInfo = JSON.parse((dom.result && dom.result.result && dom.result.result.value) || "{}");

  const list = [...reqs.values()].map((r) => ({
    url: r.url.replace(BASE, ""), method: r.method, status: r.status,
    kb: Math.round((r.size || 0) / 1024 * 10) / 10,
  }));
  const apiOnly = list.filter((r) => r.url.startsWith("/api/"));
  const totalBytes = list.reduce((a, r) => a + (r.kb || 0), 0);
  const apiBytes = apiOnly.reduce((a, r) => a + (r.kb || 0), 0);

  ws.close();
  child.kill();
  try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { /* 临时目录 */ }

  return {
    run: idx,
    firstRowMs: perf ? Math.round(perf.firstRowMs) : null,
    settledMs: perf ? Math.round(perf.settledMs) : null,
    longTasks: perf ? perf.longTasks : [],
    maxLongTask: perf && perf.longTasks.length
      ? Math.max(...perf.longTasks.map((t) => t.dur)) : 0,
    longTaskTotal: perf ? perf.longTasks.reduce((a, t) => a + t.dur, 0) : 0,
    rowsAtFirst: perf ? perf.rowsAtFirst : null,
    reqCount: list.length,
    apiReqCount: apiOnly.length,
    totalKB: Math.round(totalBytes),
    apiKB: Math.round(apiBytes),
    dom: domInfo.dom || 0,
    rows: domInfo.rows || 0,
    htmlKB: Math.round((domInfo.body || 0) / 1024),
    requests: apiOnly,
  };
}

/* ── 跑 N 轮，出统计 ───────────────────────────────────────── */
function pct(arr, p) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const i = Math.min(s.length - 1, Math.max(0, Math.ceil(p / 100 * s.length) - 1));
  return s[i];
}

const out = [];
for (let i = 0; i < RUNS; i++) {
  const r = await oneRun(i);
  out.push(r);
  process.stdout.write(
    `run ${String(i + 1).padStart(2)}: first=${r.firstRowMs}ms settled=${r.settledMs}ms ` +
    `longTask(max=${r.maxLongTask}ms) apiReq=${r.apiReqCount} apiKB=${r.apiKB} dom=${r.dom}\n`);
}

const firsts = out.map((r) => r.firstRowMs).filter((x) => x != null);
const settles = out.map((r) => r.settledMs).filter((x) => x != null);
const maxes = out.map((r) => r.maxLongTask);
const summary = {
  base: BASE, work: WID, card: CID, runs: RUNS,
  firstRowMs: { p50: pct(firsts, 50), p95: pct(firsts, 95), min: pct(firsts, 0), max: pct(firsts, 100) },
  settledMs: { p50: pct(settles, 50), p95: pct(settles, 95) },
  maxLongTaskMs: { p50: pct(maxes, 50), p95: pct(maxes, 95), max: pct(maxes, 100) },
  apiReqCount: { p50: pct(out.map((r) => r.apiReqCount), 50), max: pct(out.map((r) => r.apiReqCount), 100) },
  apiKB: { p50: pct(out.map((r) => r.apiKB), 50), max: pct(out.map((r) => r.apiKB), 100) },
  dom: { p50: pct(out.map((r) => r.dom), 50), max: pct(out.map((r) => r.dom), 100) },
  rows: { p50: pct(out.map((r) => r.rows), 50), max: pct(out.map((r) => r.rows), 100) },
  failures: out.filter((r) => r.firstRowMs == null).length,
};
console.log("\n=== 汇总 ===");
console.log(JSON.stringify(summary, null, 1));

// 请求明细（取一轮代表）
const rep = out.find((r) => r.firstRowMs != null) || out[0];
if (rep) {
  console.log("\n=== 代表轮次的 /api 请求 ===");
  for (const r of rep.requests.sort((a, b) => b.kb - a.kb)) {
    console.log(`  ${String(r.kb).padStart(7)} KB  ${r.status}  ${r.url}`);
  }
}
if (JSON_OUT) {
  fs.writeFileSync(JSON_OUT, JSON.stringify({ summary, runs: out }, null, 1));
  console.log("\n写出明细：" + JSON_OUT);
}
process.exit(0);
