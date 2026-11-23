/* tests/chat_req_defer_ab.mjs —— 任务三§2.2 成对干预：
 *   对照臂：冷开原样量 tlReq/queue/TTFB/可读点；
 *   干预臂：Fetch.requestPaused 把「可延后状态读」压住，到双帧可读点后放行。
 *   两臂同代码同数据同隔离 profile；报告 timeline 排队/TTFB/readable 差值。
 * 用法：node tests/chat_req_defer_ab.mjs <base> <work> <card> [轮数=4]
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const ROUNDS = parseInt(process.argv[5] || "4", 10);
const CHROME = process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync("logs", { recursive: true });
const outDir = fs.mkdtempSync(path.join("logs", "chat-defer-ab-"));

/* 可延后集合：开卡证据表里非首屏必需的状态读（URL 子串匹配） */
const DEFER_PATTERNS = [
  "card-messages/unread", "/api/nav/version", "model-overrides/",
  "review-status", "align-status",
];

let PORT0 = 9300 + Math.floor(Math.random() * 200);

async function oneRound(defer, roundIdx) {
  const PORT = PORT0 + roundIdx * 2;
  const profileDir = path.join(os.tmpdir(), `deferab_${Date.now()}_${roundIdx}`);
  const child = spawn(CHROME, [
    "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu",
    "--no-sandbox", "--window-size=1440,900", "--no-first-run",
    "--user-data-dir=" + profileDir, "about:blank"], { stdio: "ignore" });
  const killTree = () => {
    try { if (child.pid) spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore" }); } catch { } };
  try {
    let target = null;
    for (let i = 0; i < 100 && !target; i++) {
      try {
        const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
        if (r.ok) target = await r.json();
      } catch { }
      if (!target) await sleep(200);
    }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res) => { ws.onopen = res; });
    let mid = 0; const pend = new Map();
    const send = (method, params) => new Promise((res) => {
      const id = ++mid; pend.set(id, res);
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
    const reqs = new Map(); const held = []; const heldReqIds = [];
    let navBase = null;
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); return; }
      const p = m.params || {};
      if (m.method === "Fetch.requestPaused") {
        held.push(p.requestId); heldReqIds.push({ url: p.request.url, at: p.timestamp });
        return;   // 压住不放行
      }
      if (m.method === "Network.requestWillBeSent") {
        const u = p.request.url;
        if (navBase === null && u.includes(`/card/`)) navBase = p.timestamp;
        reqs.set(p.requestId, { url: u, sentAt: p.timestamp, type: p.type });
      }
      if (m.method === "Network.responseReceived" && reqs.has(p.requestId)) {
        const r = reqs.get(p.requestId);
        r.status = p.response.status; r.timing = p.response.timing;
        r.headAt = p.timestamp;
      }
      if (m.method === "Network.loadingFinished" && reqs.has(p.requestId)) {
        reqs.get(p.requestId).doneAt = p.timestamp;
      }
    };
    await send("Network.enable");
    await send("Page.enable");
    if (defer) {
      await send("Fetch.enable", { patterns: DEFER_PATTERNS.map((u) =>
        ({ urlPattern: `*${u}*`, requestStage: "Request" })) });
    }
    await send("Page.addScriptToEvaluateOnNewDocument", { source: `
      window.__M = { firstRow: null, readable: null };
      const isRow = (el) => el && el.matches &&
        el.matches(".flow-user-bubble, .msg") && (el.textContent || "").trim().length > 0;
      const chk = () => {
        const col = document.querySelector(".flow-view-column");
        if (!col || window.__M.firstRow) return;
        const row = col.querySelector(".flow-user-bubble, .msg");
        if (row && isRow(row)) {
          window.__M.firstRow = performance.now();
          requestAnimationFrame(() => requestAnimationFrame(
            () => { window.__M.readable = performance.now(); }));
        }
      };
      const boot = () => {
        if (!document.documentElement) return;
        try {
          new MutationObserver(chk).observe(document.documentElement,
            { childList: true, subtree: true, characterData: true });
        } catch (e) {}
        setInterval(chk, 30); chk();
      };
      if (document.documentElement) boot();
      else document.addEventListener("readystatechange", boot, { once: true });
      document.addEventListener("DOMContentLoaded", boot, { once: true });
    ` });
    await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });
    /* 等到可读点（最长 20s），再放行被压请求 */
    let marks = {};
    for (let i = 0; i < 100; i++) {
      await sleep(200);
      const v = await send("Runtime.evaluate", {
        expression: "JSON.stringify(window.__M)", returnByValue: true });
      try { marks = JSON.parse(v.result.value || "{}"); } catch { }
      if (marks.readable || i > 40) break;   // i>40 ≈ 8s 兜底放行
    }
    for (const id of held) await send("Fetch.continueRequest", { requestId: id });
    await sleep(2500);

    const tl = [...reqs.values()].find((r) => r.url.includes("timeline?tail"));
    const apiAt = [...reqs.values()].filter((r) => r.url.includes("/api/") && !r.url.includes("/events"));
    const rec = {
      defer, navBase,
      tlSent: tl && navBase != null ? Math.round((tl.sentAt - navBase) * 1000) : null,
      tlTtfb: tl && tl.headAt && navBase != null ? Math.round((tl.headAt - navBase) * 1000) : null,
      tlDone: tl && tl.doneAt && navBase != null ? Math.round((tl.doneAt - navBase) * 1000) : null,
      readable: marks.readable ? Math.round(marks.readable) : null,
      held: heldReqIds.map((h) => h.url.split("5707")[-1] || h.url),
      apiInFlightAtTlSent: tl ? apiAt.filter((r) => r.sentAt <= tl.sentAt && (!r.doneAt || r.doneAt > tl.sentAt)).length : null,
    };
    ws.close();
    return rec;
  } finally { killTree(); }
}

const rows = [];
for (let i = 0; i < ROUNDS; i++) {
  for (const defer of [false, true]) {
    const rec = await oneRound(defer, i * 2 + (defer ? 1 : 0));
    rows.push(rec);
    console.log(`r${i} ${defer ? "干预" : "对照"}: tlSent=${rec.tlSent} ttfb=${rec.tlTtfb} done=${rec.tlDone} readable=${rec.readable} inFlight@${rec.apiInFlightAtTlSent} held=${rec.held.length}`);
  }
}
fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify({ rows }, null, 1));
const med = (xs) => { const s = xs.filter((x) => x != null).sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; };
const A = rows.filter((r) => !r.defer), B = rows.filter((r) => r.defer);
console.log("\n=== 中位对比 ===");
for (const k of ["tlSent", "tlTtfb", "tlDone", "readable"]) {
  console.log(`${k}: 对照 ${med(A.map((r) => r[k]))} ms  →  干预 ${med(B.map((r) => r[k]))} ms   Δ=${med(B.map((r) => r[k])) - med(A.map((r) => r[k]))}`);
}
console.log("写出", path.join(outDir, "result.json"));
