/* tests/chat_lab_sse.mjs —— SSE 断线期状态变化·重连对账（任务三 §4.1 紧凑版）
 * web:5730（已起）+ r0:5731 + r1:5732。流程：
 *   开卡 → 断 SSE → 重启 r0 注 card_057@15+40（断线期内开跑+完成）
 *   → 断线中 probeRunning 真值网见到 c057 出现、见到其消失
 *   → 放行 → 重连补齐读 → 对账运行态（c057 清除、c011 保留、正文可读）。
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync("logs", { recursive: true });
const T0 = Date.now();
const T = () => ((Date.now() - T0) / 1000).toFixed(1);
const log = (...a) => console.log(`[t=${T()}]`, ...a);
const evidence = { phases: [], requests: [] };

const killTree = (c) => {
  try { if (c && c.pid) spawnSync("taskkill", ["/T", "/F", "/PID", String(c.pid)], { stdio: "ignore" }); } catch { }
};
function spawnRunner(idx, port, fake) {
  const env = {
    ...process.env, ECO_5707_DATA: DATA, ECO_ROLE: "runner",
    ECO_RUNNER_INDEX: String(idx), ECO_RUNNER_COUNT: "2",
    ECO_RUNNER_URLS: "http://127.0.0.1:5731,http://127.0.0.1:5732",
    ECO_5707_FAKE_RUNNING: fake, PYTHONIOENCODING: "utf-8",
  };
  const c = spawn(PY, ["scripts/run_canvas_5707.py", String(port)], { cwd: ROOT, env, stdio: "ignore" });
  log(`spawn r${idx} :${port} pid=${c.pid} fake="${fake}"`);
  return c;
}
async function fanin() {
  try {
    const r = await fetch(`${WEB}/api/nav/running`, { signal: AbortSignal.timeout(15000) });
    return r.ok ? await r.json() : { http: r.status };
  } catch (e) { return { err: String(e).slice(0, 80) }; }
}

/* 浏览器 */
const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "labsse_" + Date.now());
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
const send = (m, p) => new Promise((res, rej) => {
  const id = ++mid; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method: m, params: p || {} }));
});
const ev = (expr) => send("Runtime.evaluate",
  { expression: expr, returnByValue: true, awaitPromise: true })
  .then((r) => r.result && r.result.value);
const reqs = [];
let sseBlock = false, sseMsgCount = 0;
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); p.res(m.result); return; }
  const p = m.params || {};
  if (m.method === "Network.requestWillBeSent" && p.request.url.includes("/api/")) {
    reqs.push({ t: +T(), url: p.request.url.replace(WEB, "") });
  }
  if (m.method === "Network.eventSourceMessageReceived") sseMsgCount++;
  if (m.method === "Fetch.requestPaused") {
    if (sseBlock && p.request.url.includes("/events")) {
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
  const chk=()=>{const c=document.querySelector(".flow-view-column");if(!c)return;
    for(const el of c.querySelectorAll(".flow-user-bubble,.msg")){
      if((el.textContent||"").trim()){window.__READY=true;return;}}};
  const boot=()=>{new MutationObserver(chk).observe(document.documentElement,{childList:true,subtree:true});chk();setInterval(chk,200);};
  if(document.documentElement)boot();else addEventListener("DOMContentLoaded",boot,{once:true});
` });
await send("Page.navigate", { url: `${WEB}/work/${WID}/card/${CID}` });
const isRun = (cid) => ev(`(window.__navIsRunning?window.__navIsRunning(${JSON.stringify(WID)},${JSON.stringify(cid)}):null)`);
let ready = false;
for (let i = 0; i < 400 && !ready; i++) { await sleep(50); ready = !!(await ev("window.__READY===true")); }
log("页面正文可读:", ready, " c011=", await isRun("card_011"));
evidence.phases.push({ phase: "open", t: +T(), ready, c011: await isRun("card_011") });

/* 断 SSE + 重启 r0 注 c057@15+40 */
log("SSE 阻断 + 重启 r0…");
sseBlock = true;
const mark = reqs.length;
spawnSync("powershell", ["-NoProfile", "-Command",
  "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | " +
  "Where-Object { $_.CommandLine -match 'run_canvas_5707\\.py 5731' } | " +
  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], { stdio: "ignore" });
const r0 = spawnRunner(0, 5731, "work_073:card_057@15+40");
let seenUp = false, seenDown = false;
for (let i = 0; i < 90; i++) {
  await sleep(2000);
  if (!seenUp && await isRun("card_057") === true) {
    seenUp = true; log("断线中 probeRunning 见到 c057 出现 t=", T());
    evidence.phases.push({ phase: "c057-up-during-block", t: +T() });
  }
  if (seenUp && await isRun("card_057") === false) {
    seenDown = true; log("断线中 probeRunning 见到 c057 清除 t=", T());
    evidence.phases.push({ phase: "c057-down-during-block", t: +T() });
    break;
  }
}
const blockReqs = reqs.slice(mark).filter((r) => !r.url.includes("/events"));
evidence.phases.push({ phase: "during-block-api-reqs", t: +T(),
  count: blockReqs.length, urls: blockReqs.map((r) => r.url) });
log(`断线窗口非 events API 请求: ${blockReqs.length} 个`);

/* 放行 → 重连对账 */
sseBlock = false;
const sseBefore = sseMsgCount;
log("SSE 放行");
await sleep(9000);
const catchup = reqs.slice(mark).filter((r) =>
  /nav\/version|nav\/running|unread|rule|timeline|meta|progress/.test(r.url));
const st = {
  c049: await isRun("card_049"), c011: await isRun("card_011"),
  c057: await isRun("card_057"), ready: !!(await ev("window.__READY===true")),
  sseMsgsAfterReconnect: sseMsgCount - sseBefore,
};
evidence.phases.push({ phase: "after-reconnect", t: +T(), ...st,
  catchupReqs: catchup.map((r) => ({ t: r.t, url: r.url })) });
log("重连后: c049=", st.c049, " c011=", st.c011, " c057=", st.c057,
    " ready=", st.ready, " SSE帧+", st.sseMsgsAfterReconnect);
log("补齐读:", catchup.map((r) => r.url).join(" | "));
evidence.requests = reqs;
const f = `logs/chat-lab-sse-${Date.now().toString(36)}.json`;
fs.writeFileSync(f, JSON.stringify(evidence, null, 1));
log("写出", f);
ws.close(); killTree(chrome); killTree(r0);
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch { }
console.log("done");
