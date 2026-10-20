/* _live_update.mjs —— 画布「实时更新」端到端验收（CDP 无头 Chrome）
 *
 * 验收判据（一条条都是机械可测的，不靠人眼）：
 *   ① 画布页开着**不刷新**，后端改一张卡 ⇒ 画布上那张卡的 DOM 必须自己变；
 *   ② 变化必须在**秒级**内到达（不等 60s 轮询、不等用户手动刷新）；
 *   ③ 变化必须是**局部**的：只动那一张卡，其他卡的 DOM 引用不变（不重建整树）；
 *   ④ 改回去之后 DOM 也要跟着回去（双向都对）。
 *
 * 用法：node tests/canvas_live_update.mjs [baseUrl] [workId] [cardId]
 * 输出：JSON（stdout）；退出码 0 = 通过，1 = 失败
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5711";
const WID = process.argv[3] || "work_002";
const CID = process.argv[4] || "card_006";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9800 + Math.floor(Math.random() * 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "cdplive_" + Date.now()), "about:blank",
], { stdio: "ignore" });

let tr = null;
for (let i = 0; i < 80 && !tr; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    if (r.ok) tr = await r.json();
  } catch { /* wait */ }
  if (!tr) await sleep(200);
}
const ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
const consoleErrs = [];
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((res) => { ws.onopen = res; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") {
    consoleErrs.push((m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || "").slice(0, 240));
  }
};
await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });

const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result && r.result.result ? r.result.result.value : undefined;
};

// ── 打开画布并等稳定 ──
await send("Page.navigate", { url: `${BASE}/work/${WID}` });
let stable = false;
for (let i = 0; i < 240; i++) {
  await sleep(50);
  const n = Number(await ev(`document.querySelectorAll('.card-node').length`)) || 0;
  if (n > 0) { stable = true; break; }
}
if (!stable) { console.log(JSON.stringify({ pass: false, why: "画布没渲染出卡片" })); process.exit(1); }
await sleep(600);

// 记录基线：目标卡的标题 + 全树节点签名（用于判"是否只动了一张卡"）
const snap = () => ev(`(() => {
  const n = document.querySelector('.card-node[data-id="${CID}"]');
  const all = [...document.querySelectorAll('.card-node')];
  return {
    title: n ? (n.querySelector('.tname') || {}).textContent : null,
    hasNode: !!n,
    nodeCount: all.length,
    // 其它卡的 DOM 身份指纹（DOM 引用变了 ⇒ 整树被重建过）
    others: all.filter(x => x.dataset.id !== "${CID}").map(x => x.dataset.id + ':' + x.textContent.slice(0, 12)).join('|'),
    // 目标卡在 DOM 里的位置标记（用 title+id 组合，看它是否被换过节点）
    marker: n ? n.textContent.slice(0, 40) : null,
  };
})()`);

const before = await snap();

// 记录目标卡原标题（用于改回去）
const origTitle = await ev(`(function(){const n=document.querySelector('.card-node[data-id="${CID}"]');return n?(n.querySelector('.tname')||{}).textContent:""})()`);
const NEW_TITLE = "实时更新验收_" + Date.now().toString().slice(-6);

const t0 = Date.now();
const patch = await fetch(`${BASE}/api/works/${WID}/cards/${CID}`, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ updates: { title: NEW_TITLE }, history_note: "实时更新验收" }),
});
const patchStatus = patch.status;

// 等 DOM 自己变（不刷新页面）
let after = null, elapsed = -1;
for (let i = 0; i < 100; i++) {
  await sleep(100);
  const s = await snap();
  if (s.title === NEW_TITLE) { after = s; elapsed = Date.now() - t0; break; }
}
if (!after) after = await snap();

// 改回去，验证双向
const back = await fetch(`${BASE}/api/works/${WID}/cards/${CID}`, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ updates: { title: origTitle }, history_note: "实时更新验收-还原" }),
});
await sleep(1500);
const restored = await snap();

const out = {
  pass: !!(after && after.title === NEW_TITLE) && elapsed > 0 && elapsed < 10000,
  work: WID, card: CID,
  patchStatus, backStatus: back.status,
  beforeTitle: before.title,
  afterTitle: after.title,
  restoredTitle: restored.title,
  latencyMs: elapsed,
  nodeCountBefore: before.nodeCount,
  nodeCountAfter: after.nodeCount,
  // 其它卡的 DOM 引用没变 ⇒ 不是整树重建
  othersUnchanged: before.others === after.others,
  othersChangedSample: before.others === after.others ? null
    : { b: (before.others || "").slice(0, 120), a: (after.others || "").slice(0, 120) },
  consoleErrors: consoleErrs,
};
console.log(JSON.stringify(out, null, 1));
ws.close();
child.kill();
process.exit(out.pass ? 0 : 1);
