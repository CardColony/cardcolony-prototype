/* 批三·屏外实时更新回归（两场景，CDP 真页面实测）
 *
 *   A. 屏外卡改名：__cvLive(updated) → 索引新标题 → __cvFocus 回窗
 *      ⇒ 挂出来的卡标题必须是新标题（修前：lastViewModel 旧引用⇒旧标题）。
 *   B. 已展开屏外父卡新增子卡：__cvLive(created) → 子卡进索引 →
 *      投影必须同步（visible+1 且有 layout）；__cvFocus 子卡 ⇒ 挂载+落视口。
 *
 * 用法：node tests/canvas_live_offscreen.mjs http://127.0.0.1:5707 work_073
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const PORT = 9640;
const prof = path.join(os.tmpdir(), "lv_" + Date.now());
const cp = spawn(CHROME, ["--remote-debugging-port=" + PORT, "--headless=new",
  "--disable-gpu", "--no-sandbox", "--window-size=1440,900",
  "--no-first-run", "--user-data-dir=" + prof, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let tr = null;
for (let i = 0; i < 60 && !tr; i++) {
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }); if (r.ok) tr = await r.json(); } catch {}
  if (!tr) await sleep(300);
}
if (!tr) { console.log("FAIL chrome"); process.exit(2); }
const ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
await new Promise(r => ws.onopen = r);
const send = (m, p = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => { const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true }); return r.result && r.result.result ? r.result.result.value : undefined; };
const J = async (x) => JSON.parse(await ev(`JSON.stringify(${x})`) || "null");
const PASS = [], FAIL = [];
const ck = (name, cond, info) => { (cond ? PASS : FAIL).push(name + " :: " + info); console.log((cond ? "PASS" : "FAIL") + " " + name + " :: " + info); };

const data = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
const parents = new Set();
for (const c of data.cards) if (c.parent) parents.add(c.parent);
const cardById = Object.fromEntries(data.cards.map(c => [c.card_id, c]));

await send("Page.navigate", { url: `${BASE}/work/${WID}` });
for (let i = 0; i < 100; i++) { if (await ev(`!!document.getElementById('viewport')`)) break; await sleep(100); }
await ev(`localStorage.setItem("projView:${WID}", ${JSON.stringify(JSON.stringify({ panX: 60, panY: 60, zoom: 1.0, expanded: [...parents], openCells: [] }))})`);
await send("Page.navigate", { url: `${BASE}/work/${WID}` });
for (let i = 0; i < 240; i++) {
  await sleep(100);
  const m = await J(`window.__cvMount ? __cvMount() : null`);
  if (m && m.visible >= 75 && m.mounted > 0) break;
}
await sleep(500);
const m0 = await J(`__cvMount()`);
const mountedIds = await J(`[...document.querySelectorAll('.card-node')].map(n=>n.dataset.id)`);
console.log(`base: visible=${m0.visible} mounted=${m0.mounted}`);

/* ── 场景 A：屏外卡改名，回窗必须挂新标题 ── */
const cidA = data.cards.find(c => c.parent && !mountedIds.includes(c.card_id) && c.card_type !== "function");
ck("A0 屏外候选", !!cidA, cidA && cidA.card_id);
const newTitle = "改名-屏外RT-" + Date.now() % 10000;
const updated = { ...cardById[cidA.card_id], title: newTitle };
await J(`__cvLive(${JSON.stringify(cidA.card_id)}, ${JSON.stringify({ kind: "updated", card: updated })})`);
await sleep(600);
await J(`__cvFocus(${JSON.stringify(cidA.card_id)})`);
await sleep(1200);
const nodeA = await J(`(() => {
  const n = document.querySelector('.card-node[data-id=${JSON.stringify(cidA.card_id)}]');
  if (!n) return { mounted: false };
  const r = n.getBoundingClientRect(), vp = document.getElementById('viewport').getBoundingClientRect();
  return { mounted: true,
    inView: r.left < vp.right && r.right > vp.left && r.top < vp.bottom && r.bottom > vp.top,
    text: ((n.querySelector('.tname') || {}).textContent || "").slice(0, 80) };
})()`);
ck("A1 回窗已挂载", nodeA.mounted === true, JSON.stringify(nodeA.mounted));
ck("A2 挂出新标题", nodeA.mounted && nodeA.text.includes(newTitle), (nodeA.text || "").slice(0, 60));
ck("A3 落进视口", nodeA.inView === true, JSON.stringify(nodeA.inView));

/* ── 场景 B：已展开屏外父卡新增子卡，子卡必须进投影 ── */
/* 找一个：在 expanded 集（canvas 数据里是父卡）且**未挂载**的父卡 */
const cidP = [...parents].find(p => !mountedIds.includes(p) && cardById[p]);
ck("B0 屏外展开父卡候选", !!cidP, String(cidP));
const cidNew = "card_rt9" + (Date.now() % 1000);
const childCard = {
  card_id: cidNew, work_id: WID, title: "屏外新子卡", card_type: "worker",
  parent: cidP, state: "completed", tags: "[]", links: "[]", relations: "[]",
  created_at: "2026-11-22T00:00:00+08:00", updated_at: "2026-11-22T00:00:00+08:00",
};
await J(`__cvLive(${JSON.stringify(cidNew)}, ${JSON.stringify({ kind: "created", card: childCard })})`);
await sleep(1200);
const m1 = await J(`__cvMount()`);
ck("B1 子卡进投影(visible+1)", m1.visible === m0.visible + 1, `visible ${m0.visible}→${m1.visible}`);
await J(`__cvFocus(${JSON.stringify(cidNew)})`);
await sleep(1500);
const nodeB = await J(`(() => {
  const n = document.querySelector('.card-node[data-id=${JSON.stringify(cidNew)}]');
  if (!n) return { mounted: false };
  const r = n.getBoundingClientRect(), vp = document.getElementById('viewport').getBoundingClientRect();
  return { mounted: true,
    inView: r.left < vp.right && r.right > vp.left && r.top < vp.bottom && r.bottom > vp.top };
})()`);
ck("B2 新子卡已挂载", nodeB.mounted === true, JSON.stringify(nodeB));
ck("B3 新子卡落视口", nodeB.inView === true, JSON.stringify(nodeB.inView));

/* ── 场景 C：零挂载（相机在空白区）时新增子卡，也要进投影 ──
   修前 reflowAll 以 !nodeMap.size 早退 ⇒ 子卡只进索引不进投影；
   修后以「投影已初始化」为闸，零挂载照常重算。 */
const blankPt = await J(`(() => {
  const vp = document.getElementById('viewport').getBoundingClientRect();
  for (const [fx, fy] of [[0.5,0.08],[0.08,0.5],[0.9,0.85],[0.15,0.9],[0.9,0.1],[0.5,0.5]]) {
    const x = vp.left + vp.width * fx, y = vp.top + vp.height * fy;
    const el = document.elementFromPoint(x, y);
    if (el && (el.id === 'viewport' || el.id === 'world' || el.id === 'edgesSvg'))
      return { x: Math.round(x), y: Math.round(y) };
  }
  return null;
})()`);
ck("C0 空白起点", !!blankPt, JSON.stringify(blankPt));
const mouse = (type, x, y, extra = {}) => send("Input.dispatchMouseEvent",
  { type, x, y, button: extra.button ?? "none", buttons: extra.buttons ?? 0,
    clickCount: extra.clickCount ?? 0, modifiers: extra.modifiers ?? 0,
    deltaX: extra.deltaX ?? 0, deltaY: extra.deltaY ?? 0 });
/* A/B 的焦点卡被 keepAlive 钉住（mounted=1 永不归零）——先点空白清选 */
await mouse("mousePressed", blankPt.x, blankPt.y, { button: "left", buttons: 1, clickCount: 1 });
await mouse("mouseReleased", blankPt.x, blankPt.y, { button: "left", buttons: 0, clickCount: 1 });
await sleep(300);
/* 同一方向拖空白把相机推进空白区，直到 mounted=0 */
for (let t = 0; t < 6; t++) {
  const m = await J(`__cvMount()`);
  if (m.mounted === 0) break;
  await mouse("mousePressed", blankPt.x, blankPt.y, { button: "left", buttons: 1, clickCount: 1 });
  for (let i = 1; i <= 20; i++) {
    await mouse("mouseMoved", blankPt.x - i * 60, blankPt.y - i * 45, { button: "left", buttons: 1 });
    await sleep(12);
  }
  await mouse("mouseReleased", blankPt.x - 1200, blankPt.y - 900, { button: "left", buttons: 0, clickCount: 1 });
  await sleep(400);
}
const mBlank = await J(`__cvMount()`);
ck("C1 零挂载到位", mBlank.mounted === 0, `mounted=${mBlank.mounted} visible=${mBlank.visible}`);
const cidC = "card_rtC" + (Date.now() % 1000);
const childC = { ...childCard, card_id: cidC, title: "零挂载新子卡" };
await J(`__cvLive(${JSON.stringify(cidC)}, ${JSON.stringify({ kind: "created", card: childC })})`);
await sleep(1200);
const mC = await J(`__cvMount()`);
ck("C2 零挂载下子卡进投影", mC.visible === mBlank.visible + 1 && mC.mounted === 0,
  `visible ${mBlank.visible}→${mC.visible} mounted=${mC.mounted}`);
await J(`__cvFocus(${JSON.stringify(cidC)})`);
await sleep(1500);
const nodeC = await J(`(() => {
  const n = document.querySelector('.card-node[data-id=${JSON.stringify(cidC)}]');
  if (!n) return { mounted: false };
  const r = n.getBoundingClientRect(), vp = document.getElementById('viewport').getBoundingClientRect();
  return { mounted: true,
    inView: r.left < vp.right && r.right > vp.left && r.top < vp.bottom && r.bottom > vp.top };
})()`);
ck("C3 回窗可见可定位", nodeC.mounted === true && nodeC.inView === true, JSON.stringify(nodeC));

console.log(`\n${FAIL.length ? "FAIL" : "ALL PASS"} ${PASS.length}/${PASS.length + FAIL.length}`);
ws.close(); cp.kill("SIGKILL");
try { fs_rm(); } catch {}
process.exit(FAIL.length ? 1 : 0);
function fs_rm() { }
