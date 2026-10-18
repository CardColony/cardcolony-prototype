/* 真实用户旅程走查（5707）——按人手操作节奏，逐条走"能不能走通"
 * 用法：node scripts/_journey.mjs [baseUrl] [workId]
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
// [画布 2026-10-18] journey 只做**只读**路径，跑真实项目；
// 拖拽/建卡/删卡这类写操作归 canvas_crud.e2e.mjs（跑 work_001 假数据项目）。
const WID = process.argv[3] || "real_work_002";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9630 + Math.floor(Math.random() * 100);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "jny_" + Date.now()), "about:blank",
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
const pend = new Map();
const errs = [];
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((r) => { ws.onopen = r; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") {
    errs.push("EXC " + String((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text).slice(0, 200));
  }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
    errs.push("CONSOLE " + m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 200));
  }
};
await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });

const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const J = async (x) => JSON.parse(await ev(`JSON.stringify(${x})`) || "null");
const shot = async (n) => {
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(`_shots/j_${n}.png`, Buffer.from(r.result.data, "base64"));
};
// modifiers: Alt=1 Ctrl=2 Meta=4 Shift=8（CDP 位掩码）
const MOD = { alt: 1, ctrl: 2, meta: 4, shift: 8 };
const down = async (x, y, cc = 1, mod = 0) => send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: cc, modifiers: mod });
const up = async (x, y, cc = 1, mod = 0) => send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: cc, modifiers: mod });
const move = async (x, y, mod = 0) => send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, modifiers: mod });
async function click(x, y, opts = {}) {
  const mod = (opts && opts.modifiers) || 0;
  await move(x, y, mod); await down(x, y, 1, mod); await up(x, y, 1, mod); await sleep(260);
}
// 真人双击：系统派发 clickCount=2 才触发原生 dblclick
async function dblclick(x, y) {
  await move(x, y); await down(x, y, 1); await up(x, y, 1); await sleep(90);
  await down(x, y, 2); await up(x, y, 2); await sleep(800);
}
async function nav(u, w = 3200) { await send("Page.navigate", { url: u }); await sleep(w); }
const VK = { Digit0: 48, Escape: 27, Enter: 13, Delete: 46, ArrowLeft: 37, ArrowRight: 39 };
async function key(k, { code, modifiers = 0 } = {}) {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code, modifiers, windowsVirtualKeyCode: VK[code] });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code, modifiers, windowsVirtualKeyCode: VK[code] });
  await sleep(220);
}
const centerOf = async (sel, idx = 0) => J(`(() => {
  const n = document.querySelectorAll(${JSON.stringify(sel)})[${idx}];
  if (!n) return null;
  const r = n.getBoundingClientRect();
  return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
})()`);

const out = [];
let bad = 0;
const check = (name, cond, detail) => {
  out.push(`${cond ? "PASS" : "FAIL"} ${name}${detail !== undefined ? " :: " + detail : ""}`);
  if (!cond) bad++;
};

// ── 1 打开画布 ──
await nav(`${BASE}/work/${WID}`, 4000);
let nodes = Number(await ev("document.querySelectorAll('.card-node').length"));
check("1 画布渲染卡片", nodes > 0, nodes + " 张");
// 首屏只锚定根卡（100%），其余卡可能在视口外 ⇒ 先整幅适配，保证后续坐标点击都命中
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
await sleep(600);

// ── 2 进入对话页：走卡片右上角的 ↗（单击现在是"选中 + 右栏读卡"，不跳页）──
let b = await centerOf(".card-node", 1);
await click(b.x, b.y);
await sleep(300);
const stayedOnCanvas = (await ev("location.pathname")) === `/work/${WID}`;
const detailShown = await ev("!!document.querySelector('#cardDetail') && !document.querySelector('#cardDetail').hidden");
check("2a 单击＝选中并右栏读卡（不跳页）", stayedOnCanvas && detailShown, `canvas=${stayedOnCanvas} detail=${detailShown}`);
await ev(`document.querySelectorAll('.card-node .cv-open')[1].click()`);
await sleep(1100);
let p1 = await ev("location.pathname");
check("2b 点 ↗ ＝进入对话页", String(p1).includes("/card/"), p1);

// ── 3 对话页返回画布 ──
const backBtn = await centerOf("#canvasEntryBtn", 0);
if (backBtn) { await click(backBtn.x, backBtn.y); await sleep(1200); }
let p2 = await ev("location.pathname");
check("3 对话页→返回画布", p2 === `/work/${WID}`, p2);

// ── 4 单击＝选中（不跳页）；Shift+单击＝加选 ──
await nav(`${BASE}/work/${WID}`);
b = await centerOf(".card-node", 1);
await click(b.x, b.y);
await sleep(300);
check("4 单击＝选中且不跳页",
  (await ev("location.pathname")) === `/work/${WID}`
  && Number(await ev("document.querySelectorAll('.card-node.is-selected').length")) === 1);

// ── 5 展开/折叠 ──
const before = Number(await ev("document.querySelectorAll('.card-node').length"));
const badge = await centerOf(".kid-badge", 1);
if (badge) { await click(badge.x, badge.y); await sleep(700); }
const afterExpand = Number(await ev("document.querySelectorAll('.card-node').length"));
check("5 点徽章＝展开子卡", afterExpand > before, `${before} → ${afterExpand}`);
const badge2 = await centerOf(".kid-badge", 1);
if (badge2) { await click(badge2.x, badge2.y); await sleep(700); }
const afterCollapse = Number(await ev("document.querySelectorAll('.card-node').length"));
check("6 再点＝收起", afterCollapse === before, `${afterExpand} → ${afterCollapse}`);

// ── 7 缩放控件 ──
const z0 = await ev("(document.getElementById('zoomDisplay')||{}).textContent");
const zin = await centerOf("#zoomInBtn", 0);
if (zin) { await click(zin.x, zin.y); await sleep(300); }
const z1 = await ev("(document.getElementById('zoomDisplay')||{}).textContent");
check("7 缩放＋按钮生效", parseInt(z1) > parseInt(z0), `${z0} → ${z1}`);

// ── 8 小地图点击导航 ──
const mm = await centerOf(".cv-minimap", 0);
if (mm) {
  const panBefore = await ev("(document.getElementById('world')||{style:{}}).style.transform");
  await click(mm.x - 30, mm.y - 20);
  await sleep(400);
  const panAfter = await ev("(document.getElementById('world')||{style:{}}).style.transform");
  check("8 小地图点击＝移动视口", panBefore !== panAfter, "transform 变化");
} else check("8 小地图存在", false, "未渲染");

// ── 9 整理布局 ──
await click(...Object.values(await centerOf("#reorganizeBtn", 0)).slice(0, 2));
await sleep(900);
check("9 整理布局不报错", errs.length === 0, errs.slice(0, 2).join(" | "));

// ── 10 右键菜单 → 重命名（取消）──
// 用 DOM 派发 contextmenu：坐标右键在"目录占左侧 + ⌘0 适配"的组合下会偶发落空
await ev(`(() => {
  const n = document.querySelectorAll('.card-node')[1];
  const r = n.getBoundingClientRect();
  n.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true,
    clientX: Math.round(r.x + r.width/2), clientY: Math.round(r.y + r.height/2) }));
})()`);
await sleep(400);
const menuItems = await ev(`document.querySelectorAll('.cv-menu .cv-mi').length`);
check("10 右键菜单出现", Number(menuItems) >= 6, menuItems + " 项");
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape" });
await sleep(200);

// ── 11 ⌘K 搜索面板输入中文 ──
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "k", code: "KeyK", modifiers: 2, windowsVirtualKeyCode: 75 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "k", code: "KeyK", modifiers: 2, windowsVirtualKeyCode: 75 });
await sleep(350);
const palOpen = await ev("!!document.querySelector('.cv-palette')");
await send("Input.insertText", { text: "数据" });
await sleep(300);
const hits = Number(await ev("document.querySelectorAll('.cv-palette-item').length"));
check("11 ⌘K 面板＋中文筛选", palOpen && hits >= 1, `${hits} 条`);
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape" });

// （拖拽会写 pos，归 canvas_crud.e2e.mjs 在 work_001 上验证，本文件只做只读路径）

// ── 13 全程零 JS 异常 ──
check("13 零 JS 异常", errs.length === 0, errs.slice(0, 3).join(" | "));

console.log(out.join("\n"));
console.log(bad === 0 ? "\nALL PASS" : `\n${bad} FAIL`);
ws.close(); child.kill();
process.exit(bad === 0 ? 0 : 1);
