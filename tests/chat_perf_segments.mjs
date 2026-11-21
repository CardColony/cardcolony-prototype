/* tests/chat_perf_segments.mjs —— 首屏**分段**冷开基准（2026-11-21 性能批次二任务一①）
 *
 * 与 chat_perf_ab.mjs 的区别：那支只给 firstRow/settled/长任务/请求数，
 * 本支把首屏拆成可核对的分段——
 *   导航开始 → 入口模块就绪 → 时序请求发出 → 首字节(TTFB) → 正文下载完成
 *   → JSON 解码 → DOM 首行挂载 → 双帧可读
 * 分段的两侧可比口径（B=旧后端/旧前端也一样量得到）：
 *   · 网络段走 CDP Network 事件（requestWillBeSent/responseReceived/
 *     loadingFinished——请求排队与服务端执行、传输分离）；
 *   · 页内段走注入探针：包 window.fetch（timeline 请求打点 req/ttfb/json），
 *     行观察者给 DOM 挂载时刻（与 A/B 脚本同一判据）。
 *   「JSON 解码→DOM 挂载」之间的主线程工作（装配+渲染）由 Chrome 轨迹
 *   的函数级归因补足（traceEvents 落盘）。
 *
 * 用法：node tests/chat_perf_segments.mjs <baseUrl> <workId> <cardId> [轮数=3] [--trace]
 *   轮数缺省 3（分段测量侧重每段分布，不必 20 轮；要严测叠加 --trace 时轮轮带轨迹）。
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const ROUNDS = parseInt(process.argv[5] || "3", 10);
const TRACE = process.argv.includes("--trace");
fs.mkdirSync("logs", { recursive: true });
const evidenceDir = fs.mkdtempSync(path.join("logs", "chat-seg-"));
const WAIT_STEPS = 1200;   // 60s
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function killTree(child) {
  try {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore", timeout: 15000 });
      return;
    }
  } catch (e) { }
  try { child.kill("SIGKILL"); } catch (e) { }
}

/* 注入探针：包 fetch 给 timeline 打点（req=发出、ttfb=头到、json=解码完），
   行观察者记首条可读行挂载时刻 + 双帧后时刻。 */
const PROBE = `
(() => {
  performance.mark("card-perf-start");
  const P = { firstRowMs: null, settledMs: null, longTasks: [], marks: {} };
  window.__PERF__ = P;
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) P.longTasks.push(Math.round(e.duration)); })
      .observe({ entryTypes: ["longtask"] });
  } catch (e) {}
  /* —— fetch 包装：时序请求的 req/ttfb/json 三段 —— */
  try {
    const OF = window.fetch;
    window.fetch = function (...args) {
      const u = String((args[0] && args[0].url) || args[0] || "");
      const isTl = u.includes("/timeline");
      if (isTl) performance.mark("tl-req");
      const p = OF.apply(this, args);
      if (!isTl) return p;
      return p.then((resp) => {
        performance.mark("tl-ttfb");
        const oj = resp.json.bind(resp);
        resp.json = async () => { const r = await oj(); performance.mark("tl-json"); return r; };
        return resp;
      });
    };
  } catch (e) {}
  /* —— 首条可读对话行 ——
     [2026-11-21 修] 可读点＝**真实用户/助手正文**（.flow-user-bubble 或 .msg
     有字）——旧判据会被工具行/步骤行提前触发（它们先到但不是「可读」）。 */
  const isRow = (el) => el && el.nodeType === 1 &&
    el.matches && el.matches(".flow-user-bubble, .msg") &&
    (el.textContent || "").trim().length > 0;
  const check = () => {
    if (P.firstRowMs !== null) return;
    const col = document.querySelector(".flow-view-column");
    if (!col) return;
    const hit = col.querySelector(".flow-user-bubble, .msg");
    if (hit && isRow(hit)) {
      P.firstRowMs = performance.now();
      requestAnimationFrame(() => requestAnimationFrame(() => {
        P.settledMs = performance.now();
        performance.mark("card-perf-first-paint");
      }));
      return;
    }
  };
  const start = () => {
    try { new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true, characterData: true }); } catch (e) {}
    check(); setInterval(check, 30);
  };
  if (document.documentElement) start(); else document.addEventListener("readystatechange", start, { once: true });
})();
`;

async function oneRun(idx) {
  const PORT = 9300 + Math.floor(Math.random() * 300);
  const profileDir = path.join(os.tmpdir(), "segperf_" + Date.now() + "_" + idx);
  const child = spawn(CHROME, [
    "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
    "--hide-scrollbars", "--window-size=1440,900", "--no-first-run",
    "--user-data-dir=" + profileDir, "about:blank",
  ], { stdio: "ignore" });
  let target = null;
  for (let i = 0; i < 100 && !target; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
      if (r.ok) target = await r.json();
    } catch { }
    if (!target) await sleep(200);
  }
  if (!target) { killTree(child); return { failed: "Chrome 未起来" }; }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res) => { ws.onopen = res; });
  let id = 0; const pending = new Map(); const reqs = new Map(); const order = [];
  const traceEvents = []; let tracingDone = false;
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === "Network.requestWillBeSent") {
      reqs.set(m.params.requestId, {
        url: m.params.request.url, t0: m.params.timestamp, ttfb: null, done: null,
        size: 0, type: m.params.type,
      });
      order.push(m.params.requestId);
    } else if (m.method === "Network.responseReceived") {
      const r = reqs.get(m.params.requestId);
      if (r) r.ttfb = m.params.timestamp;
    } else if (m.method === "Network.loadingFinished") {
      const r = reqs.get(m.params.requestId);
      if (r) { r.done = m.params.timestamp; r.size = m.params.encodedDataLength; }
    } else if (m.method === "Tracing.dataCollected") {
      traceEvents.push(...m.params.value);
    } else if (m.method === "Tracing.tracingComplete") {
      tracingDone = true;
    }
  };
  const send = (m, p = {}) => new Promise((res) => {
    const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p }));
  });
  const ev = async (x) => {
    const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
  await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.addScriptToEvaluateOnNewDocument", { source: PROBE });
  if (TRACE) {
    await send("Tracing.start", {
      traceConfig: { includedCategories: ["devtools.timeline", "disabled-by-default-devtools.timeline", "blink.user_timing"] },
      transferMode: "ReportEvents",
    });
  }
  const tNavStart = Date.now();
  await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });

  let perf = null;
  for (let i = 0; i < WAIT_STEPS; i++) {
    await sleep(50);
    const v = await ev("JSON.stringify(window.__PERF__||null)");
    if (v) {
      const o = JSON.parse(v);
      if (o && o.settledMs != null) { perf = o; break; }
    }
  }
  if (!perf) {
    ws.close(); killTree(child);
    try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { }
    return { failed: "首批对话行等待超时" };
  }
  await sleep(400);
  // 页内 mark 表（tl-req/ttfb/json + navTiming 资源段）
  const markJson = await ev(`JSON.stringify({
    marks: performance.getEntriesByType("mark").map(m => ({ name: m.name, t: Math.round(m.startTime) })),
    nav: (() => { const n = performance.getEntriesByType("navigation")[0]; return n ? {
      fetchStart: Math.round(n.fetchStart), responseStart: Math.round(n.responseStart),
      responseEnd: Math.round(n.responseEnd), domContentLoaded: Math.round(n.domContentLoadedEventEnd),
      load: Math.round(n.loadEventEnd) } : null; })(),
    res: performance.getEntriesByType("resource")
      .filter(r => r.name.includes("/esm/") || r.name.includes(".js"))
      .map(r => ({ name: r.name.split("/").pop(), t0: Math.round(r.startTime), t1: Math.round(r.responseEnd) }))
      .sort((a, b) => b.t1 - a.t1).slice(0, 3),
    perfSeg: window.__PERF_SEG || null,
    /* [2026-11-21] 服务端四段账（queue/read/project/encode，Server-Timing 头） */
    tlServer: (() => {
      const r = performance.getEntriesByType("resource")
        .find(x => x.name.includes("/timeline?") || x.name.includes("/timeline&"));
      return r && r.serverTiming ? r.serverTiming.map(s => ({ name: s.name, ms: Math.round(s.duration * 10) / 10 })) : null;
    })()
  })`);
  let pageMarks = null;
  try { pageMarks = JSON.parse(markJson || "null"); } catch (e) { }
  if (TRACE) {
    await send("Tracing.end");
    for (let i = 0; i < 100 && !tracingDone; i++) await sleep(100);
    fs.writeFileSync(path.join(evidenceDir, `trace-${idx}.json`), JSON.stringify(traceEvents));
  }
  // —— 请求瀑布（API 全部记录；timeline 单列） ——
  const api = [...reqs.values()].filter(r => r.url.includes("/api/"));
  const tl = [...reqs.values()].find(r => r.url.includes("/timeline"));
  const apiKB = Math.round(api.reduce((a, r) => a + (r.size || 0), 0) / 1024);
  const waterfall = api.map(r => ({
    url: r.url.replace(BASE, "").slice(0, 120),
    t0: r.t0 ? Math.round((r.t0 - (tl ? tl.t0 : r.t0)) * 1000) : null,
    queueExec: (r.ttfb != null && r.t0 != null) ? Math.round((r.ttfb - r.t0) * 1000) : null,
    download: (r.done != null && r.ttfb != null) ? Math.round((r.done - r.ttfb) * 1000) : null,
    total: (r.done != null && r.t0 != null) ? Math.round((r.done - r.t0) * 1000) : null,
    kb: Math.round((r.size || 0) / 1024),
  }));
  ws.close(); killTree(child);
  try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { }
  const mark = (n) => {
    const m = (pageMarks && pageMarks.marks || []).find(x => x.name === n);
    return m ? m.t : null;
  };
  return {
    settledMs: perf.settledMs, firstRowMs: perf.firstRowMs,
    maxLongTask: perf.longTasks.length ? Math.max(...perf.longTasks) : 0,
    segments: {
      navToDocEnd: pageMarks && pageMarks.nav ? pageMarks.nav.responseEnd : null,
      moduleReady: pageMarks && pageMarks.res && pageMarks.res.length ? pageMarks.res[0].t1 : null,
      tlReq: mark("tl-req"), tlTtfb: mark("tl-ttfb"), tlJson: mark("tl-json"),
      firstRow: Math.round(perf.firstRowMs), settled: Math.round(perf.settledMs),
      tlQueueExec: tl && tl.ttfb != null ? Math.round((tl.ttfb - tl.t0) * 1000) : null,
      tlDownload: tl && tl.done != null ? Math.round((tl.done - tl.ttfb) * 1000) : null,
      tlKB: tl ? Math.round((tl.size || 0) / 1024) : null,
      tlServer: pageMarks && pageMarks.tlServer,
      js: pageMarks && pageMarks.perfSeg
        ? Object.fromEntries(Object.entries(pageMarks.perfSeg).map(([k, v]) =>
            [k, typeof v === "number" ? Math.round(v * 10) / 10 : v]))
        : null,
    },
    apiCount: api.length, apiKB, waterfall,
    wallMs: Date.now() - tNavStart,
  };
}

const rows = [];
for (let i = 1; i <= ROUNDS; i++) {
  const r = await oneRun("r" + i);
  if (r.failed) { console.log("第 %d 轮失败：%s", i, r.failed); continue; }
  rows.push(r);
  const s = r.segments;
  console.log("第 %d 轮 settled=%dms  导航文档=%s 模块就绪=%s tl-req=%s ttfb=%s json=%s firstRow=%s",
    i, r.settledMs, s.navToDocEnd, s.moduleReady, s.tlReq, s.tlTtfb, s.tlJson, s.firstRow);
}
const segStats = {};
for (const k of ["navToDocEnd", "moduleReady", "tlReq", "tlTtfb", "tlJson", "firstRow", "settled",
  "tlQueueExec", "tlDownload", "tlKB"]) {
  const vs = rows.map(r => r.segments[k]).filter(v => v != null).sort((a, b) => a - b);
  if (vs.length) segStats[k] = {
    p50: vs[Math.floor((vs.length - 1) / 2)],
    max: vs[vs.length - 1], n: vs.length,
  };
}
/* [2026-11-21] 服务端四段 + 前端三段：逐段名聚合（不改既有键表） */
for (const src of ["tlServer", "js"]) {
  const bucket = {};
  for (const r of rows) {
    const arr = Array.isArray(r.segments[src]) ? r.segments[src]
      : (r.segments[src] && typeof r.segments[src] === "object"
        ? Object.entries(r.segments[src]).map(([name, ms]) => ({ name, ms })) : []);
    for (const s of arr) {
      if (typeof s.ms === "number") { (bucket[s.name] = bucket[s.name] || []).push(s.ms); continue; }
      if (s.ms && typeof s.ms === "object")           // nodes 子表展开
        for (const [k2, v2] of Object.entries(s.ms))
          if (typeof v2 === "number") (bucket[`${s.name}.${k2}`] = bucket[`${s.name}.${k2}`] || []).push(v2);
    }
  }
  for (const [name, vs] of Object.entries(bucket)) {
    vs.sort((a, b) => a - b);
    segStats[`${src}.${name}`] = { p50: vs[Math.floor((vs.length - 1) / 2)], max: vs[vs.length - 1], n: vs.length };
  }
}
const out = {
  scenario: { BASE, WID, CID, rounds: ROUNDS },
  segStats,
  runs: rows.map(r => ({ settledMs: r.settledMs, segments: r.segments, apiCount: r.apiCount, apiKB: r.apiKB, waterfall: r.waterfall })),
};
fs.writeFileSync(path.join(evidenceDir, "segments.json"), JSON.stringify(out, null, 1));
console.log("\n=== 分段汇总 ===");
console.log(JSON.stringify(segStats, null, 1));
console.log("写出 " + path.join(evidenceDir, "segments.json"));
process.exit(rows.length ? 0 : 1);
