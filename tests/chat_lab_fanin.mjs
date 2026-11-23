/* tests/chat_lab_fanin.mjs —— 双 runner 扇入·断线重连·稳态 E2E（任务三 §4.1）
 * 拓扑：web:5730（已起）+ r0:5731 + r1:5732（本探针拉起，专用根 _datalab）
 * 场景：健康→杀 r1→r0 侧卡完成(清 A 留 B 待校准)→r1 恢复收敛→SSE 断线
 *       期 r0 状态变化→重连对账运行态→稳态请求量→pulse 覆盖。
 * 时轴（相对各 runner spawn）：
 *   card_049 立即开跑、~95s 完成（失联期「健康的 A 完成」演员）
 *   card_011 常驻 r1（失联 B 的待校准对象）
 *   card_057：SSE 阻断后重启 r0 注入 @25+55（断线期开跑+完成的演员）
 * 落盘 logs/chat-lab-fanin-<id>.json
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const WEB = process.argv[2] || "http://127.0.0.1:5730";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const CHROME = process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const ROOT = path.resolve(".");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const DATA = path.join(ROOT, "_datalab");
const RUNNER_URLS = "http://127.0.0.1:5731,http://127.0.0.1:5732";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync("logs", { recursive: true });
const T0 = Date.now();
const T = () => ((Date.now() - T0) / 1000).toFixed(1);
const log = (...a) => console.log(`[t=${T()}]`, ...a);
const evidence = { phases: [], requests: [] };

/* ── runner 生命周期 ─────────────────────────────── */
function spawnRunner(idx, port, fake) {
  const env = {
    ...process.env,
    ECO_5707_DATA: DATA, ECO_ROLE: "runner",
    ECO_RUNNER_INDEX: String(idx), ECO_RUNNER_COUNT: "2",
    ECO_RUNNER_URLS: RUNNER_URLS, ECO_5707_FAKE_RUNNING: fake,
    PYTHONIOENCODING: "utf-8",
  };
  const c = spawn(PY, [`scripts/run_canvas_5707.py`, String(port)],
    { cwd: ROOT, env, stdio: "ignore" });
  c._port = port;
  log(`spawn r${idx} :${port} pid=${c.pid} fake="${fake}"`);
  return c;
}
const killTree = (c) => {
  try { if (c && c.pid) spawnSync("taskkill", ["/T", "/F", "/PID", String(c.pid)], { stdio: "ignore" }); } catch { }
};

async function fanin() {
  try {
    const r = await fetch(`${WEB}/api/nav/running`, { signal: AbortSignal.timeout(15000) });
    if (!r.ok) return { http: r.status };
    return await r.json();
  } catch (e) { return { err: String(e).slice(0, 80) }; }
}
async function pulse() {
  try {
    const r = await fetch(`${WEB}/api/works/${WID}/pulse`, { signal: AbortSignal.timeout(20000) });
    return { http: r.status, body: r.ok ? await r.json() : null };
  } catch (e) { return { err: String(e).slice(0, 80) }; }
}

/* ── 先清旧 lab runner（若存在） ───────────────────── */
spawnSync("powershell", ["-NoProfile", "-Command",
  "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | " +
  "Where-Object { $_.CommandLine -match 'run_canvas_5707\\.py 573[12]' } | " +
  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], { stdio: "ignore" });
await sleep(1500);

const r0 = spawnRunner(0, 5731, "work_073:card_049@95");
const r1 = spawnRunner(1, 5732, "work_073:card_011");

/* ── 等扇入到齐（两片+两条目） ─────────────────────── */
let cur = null;
for (let i = 0; i < 60; i++) {
  await sleep(2000);
  cur = await fanin();
  const ids = (cur.running || []).map((x) => x.card_id).sort();
  if (cur.coverage?.missing?.length === 0 && ids.join() === "card_011,card_049") break;
}
log("fanin 到齐:", JSON.stringify(cur).slice(0, 220));
evidence.phases.push({ phase: "P0-complete-wire", t: +T(), fanin: cur });

/* ── 浏览器开卡 ────────────────────────────────────── */
const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "labfan_" + Date.now());
const chrome = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--window-size=1440,900", "--no-first-run",
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
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => { ws.onopen = res; });
let mid = 0;
const pend = new Map();
const send = (method, params) => new Promise((res, rej) => {
  const id = ++mid; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params: params || {} }));
});
const ev = (expr) => send("Runtime.evaluate",
  { expression: expr, returnByValue: true, awaitPromise: true })
  .then((r) => r.result && r.result.value);
const reqs = [];
let sseBlock = false;
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); p.res(m.result); return; }
  const p = m.params || {};
  if (m.method === "Network.requestWillBeSent" && p.request.url.includes("/api/")) {
    reqs.push({ t: +T(), url: p.request.url.replace(WEB, ""), type: p.type });
  }
  /* SSE 断线模拟：阻断期间把 /events 请求逐个 fail ⇒ EventSource 不断重试均失败 */
  if (m.method === "Fetch.requestPaused") {
    const u = p.request.url;
    if (sseBlock && u.includes("/events")) {
      send("Fetch.failRequest", { requestId: p.requestId, errorReason: "ConnectionFailed" });
    } else {
      send("Fetch.continueRequest", { requestId: p.requestId });
    }
  }
};
await send("Network.enable");
await send("Fetch.enable", { patterns: [{ urlPattern: "*/api/*" }] });
await send("Page.enable");
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  window.__READY=false;
  const chk=()=>{ const c=document.querySelector(".flow-view-column");
    if(!c) return;
    for(const el of c.querySelectorAll(".flow-user-bubble,.msg")){
      if((el.textContent||"").trim()){window.__READY=true;return;}}};
  const boot=()=>{new MutationObserver(chk).observe(document.documentElement,{childList:true,subtree:true});chk();setInterval(chk,200);};
  if(document.documentElement)boot();else addEventListener("DOMContentLoaded",boot,{once:true});
` });
await send("Page.navigate", { url: `${WEB}/work/${WID}/card/${CID}` });
let ready = false;
for (let i = 0; i < 600 && !ready; i++) { await sleep(50); ready = !!(await ev("window.__READY===true")); }
log("页面正文可读:", ready);
evidence.phases.push({ phase: "P1-readable", t: +T(), ready });

/* ── 画布页（第二标签）：观测 .cv-act-row.is-live[data-cid]——活动抽屉
   常驻 DOM，不受卡可见集/展开状态影响。遮蔽 document.hidden——
   pollPulse 被它闸住，后台标签不轮询会看不到状态。 */
let ev2 = async () => null;
try {
  const r2 = await fetch(`http://127.0.0.1:${PORT}/json/new?${WEB}/work/${WID}`,
    { method: "PUT" });
  const t2 = await r2.json();
  const ws2 = new WebSocket(t2.webSocketDebuggerUrl);
  await new Promise((res) => { ws2.onopen = res; });
  let mid2 = 0; const pend2 = new Map();
  ws2.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pend2.has(m.id)) { const p = pend2.get(m.id); pend2.delete(m.id); p.res(m.result); }
  };
  const send2 = (method, params) => new Promise((res, rej) => {
    const id = ++mid2; pend2.set(id, { res, rej });
    ws2.send(JSON.stringify({ id, method, params: params || {} }));
  });
  ev2 = (expr) => send2("Runtime.evaluate",
    { expression: expr, returnByValue: true, awaitPromise: true })
    .then((r) => r.result && r.result.value);
  await send2("Runtime.evaluate", { expression: `
    try{Object.defineProperty(document,"hidden",{get:()=>false});
        Object.defineProperty(document,"visibilityState",{get:()=>"visible"});}catch(e){}` });
} catch (e) { log("画布页开启失败:", String(e).slice(0, 80)); }
for (let i = 0; i < 60; i++) {
  await sleep(500);
  if (await ev2(`!!document.querySelector(".cv-act-row,#activityList")`)) break;
}
const cvRun = (cid) => ev2(
  `!!document.querySelector('.cv-act-row.is-live[data-cid="${cid}"]')`);
const cvMap = () => ev2(
  `JSON.stringify({m:window.__cvRunning?window.__cvRunning():null,`
  + `n:window.__cvPulseN||0,h:document.hidden,v:document.visibilityState,`
  + `lp:window.__cvLastPulse||null})`)
  .then((s) => { try { return JSON.parse(s); } catch { return { raw: s }; } });

const isRun = (cid) => ev(`(window.__navIsRunning?window.__navIsRunning(${JSON.stringify(WID)},${JSON.stringify(cid)}):null)`);
const snapState = async (tag) => {
  const s = {
    phase: tag, t: +T(),
    c049: await isRun("card_049"), c011: await isRun("card_011"),
    c057: await isRun("card_057"),
    cv049: await cvRun("card_049"), cv011: await cvRun("card_011"),
    cv057: await cvRun("card_057"),
  };
  s.cvMap = await cvMap();
  evidence.phases.push(s);
  log(`${tag}: c049=${s.c049} c011=${s.c011} c057=${s.c057}`
      + ` | cv049=${s.cv049} cv011=${s.cv011} cv057=${s.cv057}`
      + ` map=${JSON.stringify(s.cvMap).slice(0, 160)}`);
  return s;
};

/* 对账前提：画布须先见到**完整态**（prev 里留下 c011 属主标），
   否则「失联保留」无从生效——等画布两卡都亮再进 P1。 */
for (let i = 0; i < 60; i++) {
  await sleep(2000);
  if ((await cvRun("card_049")) && (await cvRun("card_011"))) break;
}
await snapState("P1-complete-ui");

/* ── 杀 r1（部分缺失） ────────────────────────────── */
killTree(r1); r1.dead = true;
log("r1 已杀");
await sleep(13000);                    // probeRunning 10s 周期 + 余量
const wireP2 = await fanin();
evidence.phases.push({ phase: "P2-wire-partial", t: +T(), fanin: wireP2 });
log("P2 线上:", JSON.stringify(wireP2).slice(0, 200));
await snapState("P2-partial-ui");
evidence.phases.push({ phase: "P2-pulse", t: +T(), pulse: await pulse() });

/* ── 等 card_049 到期完成（失联期 A 完成） ──────────── */
log("等 card_049 TTL 到期（r0 侧完成）…");
for (let i = 0; i < 50; i++) {
  await sleep(3000);
  const f = await fanin();
  if (!(f.running || []).some((x) => x.card_id === "card_049")) break;
}
await sleep(12000);                    // 下一轮 probeRunning
const wireP3 = await fanin();
evidence.phases.push({ phase: "P3-wire-A-done-B-missing", t: +T(), fanin: wireP3 });
log("P3 线上:", JSON.stringify(wireP3).slice(0, 200));
await snapState("P3-ui-A-done-B-missing");

/* P3b：partial 下再收敛一拍——「已应答属主名下完成⇒画布也清」
   与「失联属主保留」分开证（P3 快照可能撞在真值刚翻转的边界上） */
await sleep(9000);
await snapState("P3b-canvas-partial-converged");

/* ── r1 恢复 ───────────────────────────────────────── */
const r1b = spawnRunner(1, 5732, "work_073:card_011");
for (let i = 0; i < 40; i++) {
  await sleep(2000);
  const f = await fanin();
  if (f.coverage && f.coverage.missing.length === 0) break;
}
await sleep(12000);
const wireP4 = await fanin();
evidence.phases.push({ phase: "P4-wire-recovered", t: +T(), fanin: wireP4 });
log("P4 线上:", JSON.stringify(wireP4).slice(0, 200));
await snapState("P4-ui-recovered");
evidence.phases.push({ phase: "P4-pulse", t: +T(), pulse: await pulse() });

/* ── SSE 断线：重启 r0 注入 card_057@25+55（断线期变化的演员） ──
   断线窗口内：c057 开跑（probeRunning 仍见——它是真值网不依赖 SSE）
   → c057 完成（done 帧对浏览器丢失）→ 放行重连 → 补齐读对账。 */
log("SSE 阻断开始 + 重启 r0 注入 card_057…");
sseBlock = true;
const reqMark = reqs.length;
killTree(r0);
const r0b = spawnRunner(0, 5731, "work_073:card_057@25+55");
/* 等 c057 上线并被界面见到（断线期间 probeRunning 周期读仍工作） */
for (let i = 0; i < 60; i++) {
  await sleep(2000);
  if (await isRun("card_057") === true) break;
}
await snapState("P5-c057-visible-sse-dead");
/* 等 c057 完成（断线窗口内状态变化） */
for (let i = 0; i < 60; i++) {
  await sleep(2000);
  const fr = await fanin();
  if (!(fr.running || []).some((x) => x.card_id === "card_057")) break;
}
await sleep(3000);
const duringBlock = reqs.slice(reqMark).filter((r) => !r.url.includes("/events"));
evidence.phases.push({ phase: "P6-during-block-reqs", t: +T(),
  apiReqs: duringBlock.map((r) => ({ t: r.t, url: r.url })) });
log("断线窗口内非 events API 请求数:", duringBlock.length);

sseBlock = false;
log("SSE 放行（等重连补齐）");
await sleep(6000);
const catchup = reqs.slice(reqMark).filter((r) =>
  /nav\/version|nav\/running|unread|rule/.test(r.url));
evidence.phases.push({ phase: "P7-reconnect-catchup", t: +T(),
  catchupReqs: catchup.map((r) => ({ t: r.t, url: r.url })) });
log("重连补齐请求:", catchup.map((r) => r.url).join(" | "));
await sleep(8000);
await snapState("P7-ui-after-reconnect");
const readableAfter = !!(await ev("window.__READY===true"));
evidence.phases.push({ phase: "P7-readable", t: +T(), ready: readableAfter });

/* ── 稳态请求量（找 60s 干净窗口） ─────────────────── */
evidence.requests = reqs;
const f = `logs/chat-lab-fanin-${Date.now().toString(36)}.json`;
fs.writeFileSync(f, JSON.stringify(evidence, null, 1));
log("写出", f);

ws.close(); killTree(chrome); killTree(r0); killTree(r0b); killTree(r1b);
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch { }
console.log("done");
