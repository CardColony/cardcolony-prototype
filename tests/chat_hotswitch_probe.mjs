/* tests/chat_hotswitch_probe.mjs —— 热切读数（任务三 §4.1）
 * 同文档内：冷开 base 卡 → 等可读 → __openCard(目标卡) → 目标卡正文
 * 行挂载+视口相交+双帧 = 热切可读点。记请求清单/字节/耗时。
 * 用法：node tests/chat_hotswitch_probe.mjs <base> <work> <cardA> <cardB> [rounds]
 * 落盘：logs/chat-hotswitch-<id>.json
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CARD_A = process.argv[4] || "card_007";
const CARD_B = process.argv[5] || "card_011";
const ROUNDS = +process.argv[6] || 5;
const CHROME = process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync("logs", { recursive: true });

const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "hotsw_" + Date.now());
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
const ev = (expr) => send("Runtime.evaluate",
  { expression: expr, returnByValue: true, awaitPromise: true })
  .then((r) => r.result && r.result.value);

const reqs = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); p.res(m.result); return; }
  const p = m.params || {};
  if (m.method === "Network.requestWillBeSent") {
    reqs.set(p.requestId, { url: p.request.url, t: p.timestamp, type: p.type || "" });
  }
  if (m.method === "Network.loadingFinished" && reqs.has(p.requestId)) {
    const r = reqs.get(p.requestId); r.doneAt = p.timestamp; r.bytes = p.encodedDataLength;
  }
};

await send("Network.enable");
await send("Page.enable");
/* 可读判据注入（跨导航存活）：正文行非空 + 视口相交 + 双帧 ⇒ 记 readable。
   热切计时起点另用 __HS_MARK 记「发出 switch 的瞬间」。 */
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  window.__HS = { readable: null, readable2: null, want: null };
  const isRow = (el) => el && el.matches &&
    el.matches(".flow-user-bubble, .msg") && (el.textContent || "").trim().length > 0;
  const inView = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const sc = document.querySelector(".flow-view, #chatArea") || document.documentElement;
      const sr = sc === document.documentElement
        ? { top: 0, bottom: innerHeight } : sc.getBoundingClientRect();
      return r.height > 0 && r.bottom > sr.top + 1 && r.top < sr.bottom - 1;
    } catch (e) { return true; }
  };
  const chk = () => {
    if (window.__HS.readable != null) return;
    if (window.__HS.want && window.__CARD_ID__ !== window.__HS.want) return;
    const col = document.querySelector(".flow-view-column");
    if (!col) return;
    for (const el of col.querySelectorAll(".flow-user-bubble, .msg")) {
      if (isRow(el) && inView(el)) {
        window.__HS.readable = performance.now();
        requestAnimationFrame(() => requestAnimationFrame(() => {
          window.__HS.readable2 = performance.now();
        }));
        return;
      }
    }
  };
  const boot = () => {
    const mo = new MutationObserver(chk);
    mo.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    chk(); setInterval(chk, 30);
  };
  if (document.documentElement) boot();
  else addEventListener("DOMContentLoaded", boot, { once: true });
` });

await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CARD_A}` });
let cold = null;
for (let i = 0; i < 600; i++) {                 // 冷开等待 ≤30s
  await sleep(50);
  const v = await ev("JSON.stringify(window.__HS||{})");
  const o = v ? JSON.parse(v) : {};
  if (o.readable2 != null) { cold = o; break; }
}
if (!cold) { killTree(); console.log("FATAL 冷开未可读"); process.exit(1); }

const rounds = [];
for (let n = 0; n < ROUNDS; n++) {
  const target = n % 2 ? CARD_A : CARD_B;       // A↔B 交替
  await ev(`window.__HS.readable=null; window.__HS.readable2=null; window.__HS.want=${JSON.stringify(target)}; "ok"`);
  const tMark = Date.now();
  /* 热切走真实入口：__openCard(cid, wid) 是 workspace 暴露的开卡路径。 */
  await ev(`window.__HS.t0 = performance.now(); window.__openCard(${JSON.stringify(target)}, ${JSON.stringify(WID)}); "ok"`);
  let hit = null;
  for (let i = 0; i < 400; i++) {               // 热切等待 ≤20s
    await sleep(50);
    const v = await ev("JSON.stringify({r:window.__HS.readable, r2:window.__HS.readable2, t0:window.__HS.t0, card:(window.__CARD_ID__||document.body.dataset.card||null)})");
    const o = v ? JSON.parse(v) : {};
    if (o.r2 != null) { hit = o; break; }
  }
  rounds.push({
    round: n + 1, target,
    switchMs: hit ? Math.round(hit.r2 - hit.t0) : null,
    rowMs: hit ? Math.round(hit.r - hit.t0) : null,
    wallMs: Date.now() - tMark,
    ok: !!hit,
  });
  console.log(`第 ${n + 1} 轮 → ${target}: ${hit ? Math.round(hit.r2 - hit.t0) + " ms" : "超时"}`);
}

/* 归档 */
const arr = [...reqs.values()];
const api = arr.filter((r) => r.url.includes("/api/"));
const kb = api.reduce((a, r) => a + (r.bytes || 0), 0) / 1024;
const f = `logs/chat-hotswitch-${Date.now().toString(36)}.json`;
fs.writeFileSync(f, JSON.stringify({
  base: BASE, work: WID, cards: [CARD_A, CARD_B],
  coldReadableMs: Math.round(cold.readable2),
  rounds, apiCount: api.length, apiKB: Math.round(kb),
}, null, 1));
ws.close(); killTree();
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch { }
const ok = rounds.filter((r) => r.ok);
const ms = ok.map((r) => r.switchMs).sort((a, b) => a - b);
console.log(`热切 ${ok.length}/${ROUNDS} 成功 · p50=${ms[Math.floor(ms.length / 2)] || "-"} ms · api=${api.length}/${Math.round(kb)}KB`);
console.log("写出", f);
