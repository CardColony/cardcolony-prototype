/* _perf.mjs —— 5707 画布性能基准（首屏时间 / 平移缩放 FPS / 拖拽 FPS / 节点规模）
 *
 * 用法：node scripts/_perf.mjs [baseUrl] [workId] [width] [height]
 * 输出：一行 JSON + 人类可读摘要
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_002";
const W = parseInt(process.argv[4] || "1600", 10);
const H = parseInt(process.argv[5] || "1000", 10);
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9800 + Math.floor(Math.random() * 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=" + W + "," + H,
  "--user-data-dir=" + path.join(os.tmpdir(), "cdpperf_" + Date.now()), "about:blank",
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
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((res) => { ws.onopen = res; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });

const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const J = async (x) => JSON.parse(await ev(`JSON.stringify(${x})`) || "null");

// ---- 1) 先把整棵树展开（写 localStorage 的视图存档，然后重载）----
const data = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
const parents = new Set();
for (const c of data.cards) if (c.parent) parents.add(c.parent);
console.log(`项目 ${WID}：${data.cards.length} 张卡，${data.links.length} 条通信线，需展开 ${parents.size} 张`);

await send("Page.navigate", { url: `${BASE}/work/${WID}` });
await sleep(1500);
await ev(`localStorage.setItem("projView:${WID}", ${JSON.stringify(JSON.stringify({
  panX: 40, panY: 40, zoom: 1, expanded: [...parents],
}))})`);

// ---- 2) 首屏：从 navigate 到 卡片数量稳定 ----
const t0 = Date.now();
await send("Page.navigate", { url: `${BASE}/work/${WID}` });
let stableAt = 0, lastCount = -1, stableFor = 0;
for (let i = 0; i < 200; i++) {
  await sleep(50);
  const n = Number(await ev(`document.querySelectorAll('.card-node').length`));
  if (n === lastCount && n > 0) { stableFor += 50; if (stableFor >= 300) { stableAt = Date.now() - t0; break; } }
  else { stableFor = 0; lastCount = n; }
}
const nodes = Number(await ev(`document.querySelectorAll('.card-node').length`));
const edges = Number(await ev(`document.querySelectorAll('#edgesSvg path').length`));
const domCount = Number(await ev(`document.querySelectorAll('*').length`));
const svgCount = Number(await ev(`document.querySelectorAll('#edgesSvg *').length`));

// ---- 3) FPS 采样：rAF 计数 ----
const fpsSample = async (label, ms = 1200) => {
  const v = await ev(`new Promise((res) => {
    let n = 0; const t0 = performance.now();
    function tick() { n++; if (performance.now() - t0 < ${ms}) requestAnimationFrame(tick); else res(n / ((performance.now() - t0) / 1000)); }
    requestAnimationFrame(tick);
  })`);
  return Math.round(v * 10) / 10;
};

const idleFps = await fpsSample("idle");

// 连续缩放（模拟滚轮）：每 16ms 一次 wheel，持续 1s
const zoomFps = await ev(`new Promise((res) => {
  const vp = document.getElementById('viewport');
  const r = vp.getBoundingClientRect();
  let n = 0, i = 0; const t0 = performance.now();
  const iv = setInterval(() => {
    i++;
    vp.dispatchEvent(new WheelEvent('wheel', { deltaY: (i % 2 ? -60 : 60), clientX: r.left + r.width/2, clientY: r.top + r.height/2, bubbles: true, cancelable: true }));
    if (performance.now() - t0 > 1000) {
      clearInterval(iv);
      res(n / ((performance.now() - t0) / 1000));
    }
  }, 16);
  function tick() { n++; if (performance.now() - t0 < 1000) requestAnimationFrame(tick); }
  requestAnimationFrame(tick);
})`);

// 连续平移：模拟 pointer 拖空白
const panFps = await ev(`new Promise((res) => {
  const vp = document.getElementById('viewport');
  const r = vp.getBoundingClientRect();
  let n = 0; const t0 = performance.now();
  vp.dispatchEvent(new MouseEvent('mousedown', { button: 0, clientX: r.left + 30, clientY: r.top + r.height - 30, bubbles: true }));
  let i = 0;
  const iv = setInterval(() => {
    i++;
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: r.left + 30 + (i % 200), clientY: r.top + r.height - 30, bubbles: true }));
    if (performance.now() - t0 > 1000) {
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      clearInterval(iv);
      res(n / ((performance.now() - t0) / 1000));
    }
  }, 16);
  function tick() { n++; if (performance.now() - t0 < 1000) requestAnimationFrame(tick); }
  requestAnimationFrame(tick);
})`);

// 拖一张卡（触发 reflowAll 全量重算）
const dragFps = await ev(`new Promise((res) => {
  const node = document.querySelectorAll('.card-node')[1];
  if (!node) return res(0);
  const r = node.getBoundingClientRect();
  let n = 0; const t0 = performance.now(); let i = 0;
  node.dispatchEvent(new MouseEvent('mousedown', { button: 0, clientX: r.left + 20, clientY: r.top + 20, bubbles: true }));
  const iv = setInterval(() => {
    i++;
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: r.left + 20 + (i % 120), clientY: r.top + 20 + (i % 80), bubbles: true }));
    if (performance.now() - t0 > 1000) {
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      clearInterval(iv);
      res(n / ((performance.now() - t0) / 1000));
    }
  }, 16);
  function tick() { n++; if (performance.now() - t0 < 1000) requestAnimationFrame(tick); }
  requestAnimationFrame(tick);
})`);

const out = {
  work: WID, totalCards: data.cards.length,
  renderedNodes: nodes, renderedEdges: edges,
  domElements: domCount, svgElements: svgCount,
  firstPaintMs: stableAt,
  fps: { idle: idleFps, zoom: Math.round(zoomFps * 10) / 10, pan: Math.round(panFps * 10) / 10, drag: Math.round(dragFps * 10) / 10 },
};
console.log(JSON.stringify(out, null, 1));
ws.close();
child.kill();
process.exit(0);
