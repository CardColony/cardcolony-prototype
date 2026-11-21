/* tests/chat_perf_trace.mjs —— 单侧**带轨迹**的冷开（最长主线程任务的归因用）
 *
 * 用法：node tests/chat_perf_trace.mjs <baseUrl> <workId> <cardId> <outTag>
 *
 * 为什么要单独一支：A/B 脚本里两侧都跑轨迹时，重侧（改动前：整卡面 505KB）
 * 的 Tracing 事件没被收到（文件写出来是空的）⇒ 归因拿不到。
 * 这支只跑**一侧**、并把轨迹事件完整落盘（logs/trace_<tag>.json），
 * 顺带把「最长主线程任务 + 它的归因」打印出来。
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5708";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const TAG = process.argv[5] || "solo";
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9200 + Math.floor(Math.random() * 90);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PROBE = `
(() => {
  const P = { firstRowMs: null, settledMs: null, longTasks: [] };
  window.__PERF__ = P;
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) P.longTasks.push(Math.round(e.duration)); })
      .observe({ entryTypes: ["longtask"] });
  } catch (e) {}
  const CHROME = new Set(["flow-view-older","chat-empty","chat-skeleton","flow-turn-status"]);
  const isRow = (el) => el && el.nodeType === 1 && el.classList && !CHROME.has(el.className) &&
    el.className !== "" && (el.textContent || "").trim().length > 0;
  const check = () => {
    if (P.firstRowMs !== null) return;
    const col = document.querySelector(".flow-view-column");
    if (!col) return;
    for (const ch of col.children) {
      if (isRow(ch)) {
        P.firstRowMs = performance.now();
        requestAnimationFrame(() => requestAnimationFrame(() => { P.settledMs = performance.now(); }));
        return;
      }
    }
  };
  const start = () => {
    try { new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true, characterData: true }); } catch (e) {}
    check(); setInterval(check, 30);
  };
  if (document.documentElement) start(); else document.addEventListener("readystatechange", start, { once: true });
})();
`;

const profileDir = path.join(os.tmpdir(), "trace_" + TAG + "_" + Date.now());
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
if (!target) { console.log("Chrome 未起来"); process.exit(1); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => { ws.onopen = res; });
let id = 0; const pending = new Map();
const events = [];
let tracingDone = false;
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Tracing.dataCollected") events.push(...m.params.value);
  if (m.method === "Tracing.tracingComplete") tracingDone = true;
};
const send = (m, p = {}) => new Promise((res) => {
  const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.addScriptToEvaluateOnNewDocument", { source: PROBE });
await send("Tracing.start", {
  traceConfig: { includedCategories: ["devtools.timeline", "disabled-by-default-devtools.timeline"] },
  transferMode: "ReportEvents",
});
await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });

let perf = null;
for (let i = 0; i < 1500; i++) {
  await sleep(50);
  const v = await ev("JSON.stringify(window.__PERF__||null)");
  if (v) { const o = JSON.parse(v); if (o && o.settledMs != null) { perf = o; break; } }
}
await send("Tracing.end");
for (let i = 0; i < 200 && !tracingDone; i++) await sleep(100);
await sleep(500);

fs.writeFileSync(`logs/trace_${TAG}.json`, JSON.stringify(events));
const isHarness = (x) => x.name === "EvaluateScript" && x.args && x.args.data
  && (x.args.data.scriptId === 0 || x.args.data.url === undefined);
const tasks = events.filter((e) => e.name === "RunTask" && e.ph === "X" && e.dur);
const pageTasks = tasks.filter((t) => !events.some((e) => e.ts >= t.ts && e.ts <= t.ts + t.dur && isHarness(e)));
pageTasks.sort((a, b) => b.dur - a.dur);
const out = {
  tag: TAG, base: BASE,
  firstRowMs: perf ? Math.round(perf.firstRowMs) : null,
  settledMs: perf ? Math.round(perf.settledMs) : null,
  longTasksFromObserver: perf ? perf.longTasks : [],
  traceEventCount: events.length,
  runTaskCount: tasks.length,
  pageTaskCount: pageTasks.length,
  longestPageTasks: pageTasks.slice(0, 5).map((t) => {
    const calls = events.filter((e) => e.ts >= t.ts && e.ts <= t.ts + t.dur && e.dur &&
      ["Layout", "UpdateLayoutTree", "ParseHTML", "FunctionCall", "Paint", "TimerFire",
        "ResourceReceiveResponse", "CommitLoad"].includes(e.name) && !isHarness(e));
    calls.sort((a, b) => b.dur - a.dur);
    return {
      ms: Math.round(t.dur / 1000),
      calls: calls.slice(0, 6).map((c) => ({
        name: c.name, ms: Math.round(c.dur / 1000),
        hint: c.args ? JSON.stringify(c.args).slice(0, 120) : "",
      })),
    };
  }),
};
fs.writeFileSync(`logs/trace_${TAG}_summary.json`, JSON.stringify(out, null, 1));
console.log(JSON.stringify(out, null, 1));
ws.close();
try { spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore" }); } catch (e) { }
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { }
process.exit(0);
