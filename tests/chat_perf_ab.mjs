/* tests/chat_perf_ab.mjs —— **交错 A/B** 冷开基准（2026-11-20 性能批次一）
 *
 * 为什么不用「先跑 20 轮前、再跑 20 轮后」：
 * 实测两批之间的**批次漂移**（磁盘冷缓存、页缓存、机器负载）与被测效应同量级
 * （实测同一份代码连测两批，中位能差 ±15%）⇒ 单批前后比不可信。
 * ⇒ 改成**逐轮交错**：A、B、A、B……任何漂移对两边影响相同。
 *
 * 用法：node tests/chat_perf_ab.mjs <urlA> <urlB> <workId> <cardId> [轮数]
 *   A ＝ 改动后；B ＝ 改动前（两个实例同数据根、不同静态树）
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const URL_A = process.argv[2] || "http://127.0.0.1:5707";
const URL_B = process.argv[3] || "http://127.0.0.1:5708";
const WID = process.argv[4] || "work_073";
const CID = process.argv[5] || "card_007";
const ROUNDS = parseInt(process.argv[6] || "20", 10);
fs.mkdirSync("logs", { recursive: true });
const evidenceDir = fs.mkdtempSync(path.join("logs", "chat-perf-"));
/** 等首批对话行的步数（50ms/步）——重侧（整卡面 505KB）首屏更慢，给足。 */
const WAIT_STEPS = 1200;
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function killTree(child) {
  try {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)],
        { stdio: "ignore", timeout: 15000 });
      return;
    }
  } catch (e) { /* 默认 */ }
  try { child.kill("SIGKILL"); } catch (e) { /* 已死 */ }
}

/* ── 单轮：起浏览器 → 冷开 → 记首批对话行/长任务/请求 ────────────── */
const PROBE = `
(() => {
  performance.mark("card-perf-start");
  const P = { firstRowMs: null, longTasks: [] };
  window.__PERF__ = P;
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) P.longTasks.push(Math.round(e.duration)); })
      .observe({ entryTypes: ["longtask"] });
  } catch (e) {}
  /* [2026-11-21 修] 可读点＝**真实用户/助手正文**挂载（.flow-user-bubble 或
     .msg 有字）——旧判据「列里任一非骨架行有字」会被工具行/步骤行提前触发
     （它们比正文先到，但那不是用户要的「可读」）。
     [任务三·累计门槛] 再加**视口相交**：有字但挂在视口外的行不算可读
     （有界挂载下底部正文可能先挂进离屏窗）。 */
  const isRow = (el) => el && el.nodeType === 1 &&
    el.matches && el.matches(".flow-user-bubble, .msg") &&
    (el.textContent || "").trim().length > 0;
  const inView = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const scroller = document.querySelector(".flow-view, #chatArea")
        || document.scrollingElement || document.documentElement;
      const sr = scroller === document.documentElement
        ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect();
      return r.height > 0 && r.bottom > sr.top + 1 && r.top < sr.bottom - 1;
    } catch (e) { return true; }   // 量不出时退回旧判据（不误伤基线路径）
  };
  /* [2026-11-22 修] 相交判定改 IntersectionObserver 异步上报——旧实现把
     getBoundingClientRect 逐行同步查挂在 MutationObserver+30ms 轮询上，
     对增量渲染臂（DOM 变更频繁）不对称抽税：每次突变强制 layout，
     测出的 A 臂 P50 回退有相当部分来自测量器本身。判据口径不变：
     同选择器＋视口相交＋非空。只盯末尾 ≤3 个候选行（新卡视口恒在尾部）。 */
  let io = null;
  const markHit = () => {
    P.firstRowMs = performance.now();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      P.settledMs = performance.now();
      performance.mark("card-perf-first-paint");
    }));
    try { io && io.disconnect(); } catch (e) {}
  };
  const hit = () => {
    if (P.firstRowMs !== null) return;
    const col = document.querySelector(".flow-view-column");
    if (!col) return;
    const rows = col.querySelectorAll(".flow-user-bubble, .msg");
    for (let i = Math.max(0, rows.length - 3); i < rows.length; i++) {
      if (io) { try { io.observe(rows[i]); } catch (e) {} }
      if (rows[i].__ioSeen && isRow(rows[i])) { markHit(); return; }
    }
  };
  try {
    const scroller0 = document.querySelector(".flow-view, #chatArea");
    io = new IntersectionObserver((ents) => {
      for (const e of ents) { if (e.isIntersecting) e.target.__ioSeen = true; }
      hit();
    }, scroller0 ? { root: scroller0 } : undefined);
  } catch (e) { io = null; }
  const rowHit = () => {
    const col = document.querySelector(".flow-view-column");
    if (!col) return null;
    for (const el of col.querySelectorAll(".flow-user-bubble, .msg")) {
      if (isRow(el) && inView(el)) return el;
    }
    return null;
  };
  const check = () => {
    if (P.firstRowMs !== null) return;
    if (io) { hit(); return; }
    if (rowHit()) markHit();   // 无 IO 的老浏览器退回同步判据
  };
  const start = () => {
    try { new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true, characterData: true }); } catch (e) {}
    check(); setInterval(check, 30);
  };
  if (document.documentElement) start(); else document.addEventListener("readystatechange", start, { once: true });
})();
`;

async function oneRun(base, idx, { trace = false } = {}) {
  let tracingDone = false;
  const PORT = 9300 + Math.floor(Math.random() * 300);
  const profileDir = path.join(os.tmpdir(), "abperf_" + Date.now() + "_" + idx);
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
    } catch { /* wait */ }
    if (!target) await sleep(200);
  }
  if (!target) {
    killTree(child);
    return { failed: "Chrome 未起来（端口占用或残留进程挡住了启动）" };
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res) => { ws.onopen = res; });
  let id = 0; const pending = new Map(); const reqs = new Map();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === "Network.requestWillBeSent") {
      reqs.set(m.params.requestId, { url: m.params.request.url, size: 0 });
    } else if (m.method === "Network.loadingFinished") {
      const r = reqs.get(m.params.requestId);
      if (r) r.size = m.params.encodedDataLength;
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
  // ★ 轨迹模式：抓 Chrome 追踪（最长主线程任务归因用）
  const traceEvents = [];
  if (trace) {
    ws.addEventListener("message", (e) => {
      const m = JSON.parse(e.data);
      if (m.method === "Tracing.dataCollected") traceEvents.push(...m.params.value);
      if (m.method === "Tracing.tracingComplete") tracingDone = true;
    });
    await send("Tracing.start", {
      traceConfig: { includedCategories: ["devtools.timeline", "disabled-by-default-devtools.timeline", "blink.user_timing"] },
      transferMode: "ReportEvents",
    });
  }
  const navigation = await send("Page.navigate", { url: `${base}/work/${WID}/card/${CID}` });

  let perf = null;
  for (let i = 0; !navigation.result?.errorText && i < WAIT_STEPS; i++) {
    await sleep(50);
    const v = await ev("JSON.stringify(window.__PERF__||null)");
    if (v) {
      const o = JSON.parse(v);
      if (o && o.settledMs != null) { perf = o; break; }
    }
  }
  if (!perf) {
    ws.close(); killTree(child);
    try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { /* 临时目录 */ }
    return { failed: navigation.result?.errorText || "首批对话行等待超时（60 秒）" };
  }
  await sleep(500);
  let traceSummary = null;
  if (trace) {
    await send("Tracing.end");
    for (let i = 0; i < 100 && !tracingDone; i++) await sleep(100);
    traceSummary = tracingDone ? summariseTrace(traceEvents) : null;
    fs.writeFileSync(path.join(evidenceDir, "trace-" + idx + ".json"), JSON.stringify(traceEvents));
  }
  const list = [...reqs.values()];
  const api = list.filter((r) => r.url.includes("/api/"));
  const kb = api.reduce((a, r) => a + (r.size || 0), 0) / 1024;
  const apiCount = api.length;
  const urls = list.map((r) => r.url.replace(base, ""));
  ws.close(); killTree(child);
  try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { /* 临时目录 */ }
  return {
    firstRowMs: perf ? Math.round(perf.firstRowMs) : null,
    settledMs: perf ? Math.round(perf.settledMs) : null,
    maxLongTask: perf && perf.longTasks.length ? Math.max(...perf.longTasks) : 0,
    longTaskTotal: perf ? perf.longTasks.reduce((a, b) => a + b, 0) : 0,
    longTasks: perf ? perf.longTasks : [],
    apiCount, apiKB: Math.round(kb),
    // 整张卡面＝URL **恰好**到卡号（后面没有 ?projection=meta）。
    // ★ 别用 `/cards/[^/]+$/` —— ① 它会把 ?projection=meta 也算进去（假阳性）；
    //   ② 去掉查询串再比，又会把窄读算成整卡（假阴性）。用「精确等值」最省事。
    hasFullCard: urls.some((u) => u.endsWith("/cards/" + CID)),
    hasFullModelTable: urls.some((u) => u.includes("/model-overrides/") && !u.includes("/effective")),
    trace: traceSummary,
    urls,
  };
}

/** 从 Chrome 追踪里挑出**最长的主线程任务**，并给出它的归因（最贵的几个调用）。
 *  ★ 要**排除基准脚本自己**的 EvaluateScript（我注入的探针每 50ms 跑一次
 *    `Runtime.evaluate` ⇒ 它会把自己记成最长的任务，纯属测量自扰）。 */
function summariseTrace(events) {
  const end = events.find(e => e.name === "card-perf-first-paint");
  if (!end) return null;
  const start = events.find(e => e.name === "card-perf-start" && e.pid === end.pid && e.tid === end.tid);
  if (!start) return null;
  const pageEvents = events.filter(e => e.pid === end.pid && e.tid === end.tid &&
    e.ts >= start.ts && e.ts <= end.ts);
  const isHarness = (e) => {
    if (e.name !== "EvaluateScript") return false;
    const d = e.args && e.args.data;
    // scriptId 0 ＝ 页面里 addScriptToEvaluateOnNewDocument 注入的探针
    return !!(d && (d.scriptId === 0 || d.url === undefined));
  };
  const tasks = pageEvents.filter((e) => e.name === "RunTask" && e.ph === "X" && e.dur);
  if (!tasks.length) return null;
  tasks.sort((a, b) => b.dur - a.dur);
  const longest = tasks.find((t) => {
    const t0 = t.ts, t1 = t.ts + t.dur;
    return !pageEvents.some((e) => e.ts >= t0 && e.ts <= t1 && isHarness(e));
  }) || tasks[0];
  const t0 = longest.ts, t1 = longest.ts + longest.dur;
  const calls = pageEvents.filter((e) =>
    (e.name === "FunctionCall" || e.name === "EvaluateScript" || e.name === "TimerFire" ||
     e.name === "ParseHTML" || e.name === "Layout" || e.name === "UpdateLayoutTree" ||
     e.name === "Paint" || e.name === "ResourceSendRequest" ||
     e.name === "ResourceReceiveResponse" || e.name === "CommitLoad") &&
    e.ts >= t0 && e.ts <= t1 && e.dur && !isHarness(e));
  calls.sort((a, b) => b.dur - a.dur);
  return {
    longestTaskMs: Math.round(longest.dur / 1000),
    topCalls: calls.slice(0, 10).map((c) => ({
      name: c.name, ms: Math.round(c.dur / 1000),
      data: c.args ? JSON.stringify(c.args).slice(0, 150) : "",
    })),
  };
}

/* ── 交错跑（成对：两边都成功才计一轮）─────────────────────────── */
const TRACE_ONLY = process.argv.includes("--trace-only");
const A = [], B = [];
const failures = [];
const discardedRounds = [];
if (!TRACE_ONLY) {
  for (let round = 1; round <= ROUNDS; round++) {
    // 交替先后顺序。任一侧失败时整对重试，两个样本来自同一次尝试。
    const order = round % 2 ? ["A", "B"] : ["B", "A"];
    for (let attempt = 1; attempt <= 3; attempt++) {
      const pair = {};
      for (const arm of order) {
        const sample = await oneRun(arm === "A" ? URL_A : URL_B, [round, attempt, arm].join("-"));
        if (sample.failed) failures.push({ round, attempt, arm, reason: sample.failed });
        else pair[arm] = { round, attempt, ...sample };
      }
      if (pair.A && pair.B) {
        A.push(pair.A);
        B.push(pair.B);
        console.log("第 %d 轮  A=%d ms  B=%d ms", round, pair.A.settledMs, pair.B.settledMs);
        break;
      }
      if (attempt < 3) await sleep(1500);
      else {
        discardedRounds.push(round);
        console.log("第 %d 轮整对作废", round);
      }
    }
  }
}

for (const f of failures) {
  console.log("第 %d 轮第 %d 次 %s 臂失败：%s", f.round, f.attempt, f.arm, f.reason);
}

/* ── 各补一轮**带轨迹**的（最长主线程任务的归因）───────────────── */
console.log("\n=== 轨迹轮（各一轮，带 Chrome 追踪）===");
const traceA = await oneRun(URL_A, "A-after", { trace: true });
const traceB = await oneRun(URL_B, "B-before", { trace: true });
console.log("A（改动后）最长主线程任务：", JSON.stringify(traceA && traceA.trace));
console.log("B（改动前）最长主线程任务：", JSON.stringify(traceB && traceB.trace));
if (traceA && traceA.failed) console.log("  A 轨迹轮失败原因：", traceA.failed);
if (traceB && traceB.failed) console.log("  B 轨迹轮失败原因：", traceB.failed);

function stats(rows) {
  const f = rows.map((r) => r.settledMs).sort((x, y) => x - y);
  const median = (values) => values.length
    ? (values[Math.floor((values.length - 1) / 2)] + values[Math.floor(values.length / 2)]) / 2
    : null;
  const p95 = f.length ? f[Math.ceil(0.95 * f.length) - 1] : null;
  const trimmed = f.length > 6 ? f.slice(3, f.length - 3) : f;
  return {
    n: rows.length, median: median(f), p95, trimmedMedian: median(trimmed), min: f[0], max: f[f.length - 1],
    apiKB: rows.length ? Math.round(rows.reduce((a, r) => a + r.apiKB, 0) / rows.length) : 0,
    apiCount: rows.length ? Math.round(rows.reduce((a, r) => a + r.apiCount, 0) / rows.length) : 0,
    maxLongTask: rows.length ? Math.max(...rows.map((r) => r.maxLongTask)) : 0,
    longTaskTotal: rows.length ? Math.round(rows.reduce((a, r) => a + r.longTaskTotal, 0) / rows.length) : 0,
    fullCardRows: rows.filter((r) => r.hasFullCard).length,
    fullModelRows: rows.filter((r) => r.hasFullModelTable).length,
  };
}
const sa = stats(A), sb = stats(B);
console.log("\n=== 交错 A/B 汇总 ===");
console.log(JSON.stringify({ A_after: sa, B_before: sb }, null, 1));
if (A.length) {
  const medianChange = (sa.median - sb.median) / sb.median * 100;
  const p95Change = (sa.p95 - sb.p95) / sb.p95 * 100;
  console.log("P50 %d → %d ms (%s%%)", sb.median, sa.median, medianChange.toFixed(1));
  console.log("P95 %d → %d ms (%s%%)", sb.p95, sa.p95, p95Change.toFixed(1));
}
const resultPath = path.join(evidenceDir, "result.json");
fs.writeFileSync(resultPath, JSON.stringify({
  scenario: { URL_A, URL_B, WID, CID, rounds: TRACE_ONLY ? 0 : ROUNDS, metric: "settledMs" },
  A, B, sa, sb, failures, discardedRounds,
  traceA, traceB,
}, null, 1));
console.log("写出 " + resultPath);
process.exit(discardedRounds.length || !traceA.trace || !traceB.trace ? 1 : 0);
