/* tests/chat_req_capture.mjs —— 开卡请求来源取证（任务三 §2.3）
 * 一次冷开（隔离 profile），Network 域记每个请求的 URL/发起栈/
 * queued→stalled→TTFB→done 时序 + EventSource 长连接；另注入
 * fetch 包装记 call-stack 归属（stream/workspace/nav/api）。
 * 用法：node tests/chat_req_capture.mjs <base> <work> <card> [可读判定ms=0]
 * 落盘：logs/chat-req-<id>.json
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const CHROME = process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync("logs", { recursive: true });

const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "reqcap_" + Date.now());
const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--window-size=1440,900", "--no-first-run",
  "--user-data-dir=" + profileDir, "about:blank",
], { stdio: "ignore" });
const killTree = () => {
  try { if (child.pid) spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore" }); } catch { }
};

let target = null;
for (let i = 0; i < 100 && !target; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    if (r.ok) target = await r.json();
  } catch { }
  if (!target) await sleep(200);
}
if (!target) { killTree(); console.log("FATAL chrome"); process.exit(1); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => { ws.onopen = res; });
let mid = 0;
const pend = new Map();
const send = (method, params) => new Promise((res, rej) => {
  const id = ++mid;
  pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params: params || {} }));
});

const reqs = new Map();   // requestId -> record
const evLog = [];         // 读点后的简表也保留
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); p.res(m.result); return; }
  const p = m.params || {};
  if (m.method === "Network.requestWillBeSent") {
    const it = p.initiator || {};
    reqs.set(p.requestId, {
      url: p.request.url, method: p.request.method, type: p.type || "",
      t: p.wallTime ? undefined : undefined, sentAt: p.timestamp,
      stack: it.stack ? it.stack.callFrames.map((f) =>
        `${f.url.split("/").pop()}:${f.lineNumber} ${f.functionName}`).join(" <- ")
        : (it.type || "") + (it.url ? ` (${it.url.split("/").pop()}:${it.lineNumber})` : ""),
      initType: it.type || "",
      docUrl: p.documentURL || "",
    });
  }
  if (m.method === "Network.responseReceived" && reqs.has(p.requestId)) {
    const r = reqs.get(p.requestId);
    r.status = p.response.status;
    r.timing = p.response.timing || null;
    r.mimeType = p.response.mimeType;
    r.protocol = p.response.protocol;
  }
  if (m.method === "Network.loadingFinished" && reqs.has(p.requestId)) {
    const r = reqs.get(p.requestId);
    r.doneAt = p.timestamp;
    r.bytes = p.encodedDataLength;
  }
  if (m.method === "Network.loadingFailed" && reqs.has(p.requestId)) {
    const r = reqs.get(p.requestId);
    r.failed = p.errorText;
  }
  if (m.method === "Network.eventSourceMessageReceived" && reqs.has(p.requestId)) {
    const r = reqs.get(p.requestId); r.sse = (r.sse || 0) + 1;
  }
};

await send("Network.enable");
await send("Page.enable");
/* 可读点探针：首条正文行挂载时刻 + 两帧后 */
await send("Runtime.evaluate", { expression: `
  window.__REQ_MARKS = { firstRow: null, readable: null };
  const mo = new MutationObserver(() => {
    const row = document.querySelector(".frow .fbody:not(:empty), .flow-row .row-body:not(:empty)");
    if (row && !window.__REQ_MARKS.firstRow) {
      window.__REQ_MARKS.firstRow = performance.now();
      requestAnimationFrame(() => requestAnimationFrame(
        () => { window.__REQ_MARKS.readable = performance.now(); }));
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
` });
const t0 = Date.now();
await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });
await sleep(12000);

const marks = await send("Runtime.evaluate", {
  expression: "JSON.stringify(window.__REQ_MARKS)", returnByValue: true });
const out = JSON.parse(marks.result.value || "{}");

/* 归档：wallTime 换成自导航 ms（sentAt 是 CDP monotonic；用最早请求当 t0） */
const arr = [...reqs.values()];
const base = Math.min(...arr.map((r) => r.sentAt || 1e12));
for (const r of arr) {
  r.tMs = r.sentAt ? Math.round((r.sentAt - base) * 1000) : null;
  r.doneMs = r.doneAt ? Math.round((r.doneAt - base) * 1000) : null;
  if (r.timing) {
    r.queued = r.timing.requestTime ? Math.round((r.timing.requestTime * 1000 - base * 1) * 1) : null;
    r.stalledMs = r.timing.stalled;
    r.waitTtfb = r.timing.waiting;
    r.contentDl = r.timing.receiveHeadersEnd != null ? Math.round(r.timing.receiveHeadersEnd) : null;
    r.dlMs = r.timing.receiveHeadersEnd;
  }
}
arr.sort((a, b) => a.tMs - b.tMs);
const f = `logs/chat-req-${Date.now().toString(36)}.json`;
fs.writeFileSync(f, JSON.stringify({
  base: BASE, card: `${WID}/${CID}`, navAt: t0, marks: out, requests: arr }, null, 1));
console.log("readable:", JSON.stringify(out));
for (const r of arr) {
  console.log(`${String(r.tMs).padStart(6)} +${String(r.doneMs - r.tMs || 0).padStart(5)} ` +
    `${(r.type || "-").slice(0, 10).padEnd(10)} ${String(r.status || "-").padStart(3)} ` +
    `stalled=${String(Math.round(r.stalledMs || 0)).padStart(4)} wait=${String(Math.round(r.waitTtfb || 0)).padStart(5)} ` +
    `${r.sse ? "sse:" + r.sse + " " : ""}${r.url.replace(BASE, "").slice(0, 80)}`);
}
ws.close(); killTree();
console.log("写出", f);
