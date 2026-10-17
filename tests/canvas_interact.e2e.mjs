/* _interact.mjs —— 5707 画布交互验收（真鼠标键盘事件，走 CDP Input 域）
 *
 * 用法：node scripts/_interact.mjs [baseUrl] [workId]
 * 输出：逐条 PASS/FAIL + 末尾汇总；任一 FAIL ⇒ 退出码 1
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_001";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9500 + Math.floor(Math.random() * 300);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const bad = [];
function onEvent(m) {
  if (m.method === "Runtime.exceptionThrown") {
    const d = m.params.exceptionDetails;
    bad.push("EXC " + ((d.exception && d.exception.description) || d.text));
  }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
    bad.push("CONSOLE " + m.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
  }
}

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "cdpint_" + Date.now()), "about:blank",
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
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else onEvent(m);
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: `${BASE}/work/${WID}` });
await sleep(3200);

const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const J = async (x) => JSON.parse(await ev(`JSON.stringify(${x})`) || "null");

// ---- 输入原语 ----
const MOD = { alt: 1, ctrl: 2, meta: 4, shift: 8 };
async function mouse(type, x, y, { button = "left", clickCount = 1, modifiers = 0 } = {}) {
  await send("Input.dispatchMouseEvent", { type, x, y, button, clickCount, modifiers });
}
async function click(x, y, { modifiers = 0 } = {}) {
  await mouse("mouseMoved", x, y, { modifiers });
  await mouse("mousePressed", x, y, { modifiers });
  await mouse("mouseReleased", x, y, { modifiers });
  await sleep(260);   // 越过 200ms 双击窗口
}
async function dblclick(x, y, { modifiers = 0 } = {}) {
  await mouse("mouseMoved", x, y, { modifiers });
  await mouse("mousePressed", x, y, { clickCount: 1, modifiers });
  await mouse("mouseReleased", x, y, { clickCount: 1, modifiers });
  await sleep(40);
  await mouse("mousePressed", x, y, { clickCount: 2, modifiers });
  await mouse("mouseReleased", x, y, { clickCount: 2, modifiers });
  await sleep(600);
}
async function wheel(x, y, deltaY, { modifiers = 0 } = {}) {
  await send("Input.dispatchMouseEvent", {
    type: "mouseWheel", x, y, deltaX: 0, deltaY, modifiers,
  });
  await sleep(120);
}
async function drag(x1, y1, x2, y2, { button = "left", modifiers = 0, steps = 8 } = {}) {
  await mouse("mouseMoved", x1, y1, { modifiers });
  await mouse("mousePressed", x1, y1, { button, modifiers });
  for (let i = 1; i <= steps; i++) {
    await mouse("mouseMoved", x1 + ((x2 - x1) * i) / steps, y1 + ((y2 - y1) * i) / steps, { button, modifiers });
    await sleep(12);
  }
  await mouse("mouseReleased", x2, y2, { button, modifiers });
  await sleep(220);
}
async function rightClick(x, y) {
  await mouse("mouseMoved", x, y);
  await mouse("mousePressed", x, y, { button: "right" });
  await mouse("mouseReleased", x, y, { button: "right" });
  await sleep(320);
}
async function key(k, { code, modifiers = 0 } = {}) {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code: code || k, modifiers, windowsVirtualKeyCode: 0 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: code || k, modifiers, windowsVirtualKeyCode: 0 });
  await sleep(180);
}

const out = [];
let ok = true;
const check = (name, cond, detail) => {
  out.push(`${cond ? "PASS" : "FAIL"} ${name}${detail !== undefined ? " :: " + detail : ""}`);
  ok = ok && cond;
};

// ---- 取卡片屏幕框 ----
const boxes = await J(`[...document.querySelectorAll('.card-node')].map(n => {
  const r = n.getBoundingClientRect();
  return { id: n.dataset.id, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
})`);
check("E0 画布已渲染卡片", boxes.length >= 3, `${boxes.length} 张`);
const selCount = async () => Number(await ev(`document.querySelectorAll('.card-node.is-selected').length`));
const zoomText = async () => String(await ev(`(document.getElementById('zoomDisplay')||{}).textContent||''`));
const worldTf = async () => String(await ev(`(document.getElementById('world')||{style:{}}).style.transform||''`));

const a = boxes[0], b = boxes[1];
const cx = (o) => o.x + o.w / 2, cy = (o) => o.y + o.h / 2;

// A1 单击选中
await click(cx(a), cy(a));
check("A1 单击卡＝选中 1 张", (await selCount()) === 1, await selCount());
check("A1b 单击不再跳页", (await ev("location.pathname")) === `/work/${WID}`, await ev("location.pathname"));

// A2 Shift 多选
await click(cx(b), cy(b), { modifiers: MOD.shift });
check("A2 Shift+单击＝多选 2 张", (await selCount()) === 2, await selCount());

// A3 点空白取消
await click(80, 900);
check("A3 点空白＝清空选择", (await selCount()) === 0, await selCount());

// A4 滚轮缩放
const z0 = await zoomText();
await wheel(cx(a), cy(a), -240);
const z1 = await zoomText();
check("A4 滚轮向上＝放大", parseInt(z1) > parseInt(z0), `${z0} → ${z1}`);

// A5 中键平移
const t0 = await worldTf();
await drag(cx(a), cy(a), cx(a) + 120, cy(a) + 60, { button: "middle" });
const t1 = await worldTf();
check("A5 中键拖＝平移", t0 !== t1, `${t0} → ${t1}`);

// A6 Shift+拖空白＝框选
await key("Escape");
await drag(200, 120, 1400, 900, { modifiers: MOD.shift });
const marqSel = await selCount();
check("A6 Shift+拖空白＝框选选中多张", marqSel >= 2, marqSel);

// A7 ⌘0 整幅适配
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
const z2 = await zoomText();
check("A7 Ctrl+0＝整幅适配（回到合理档）", parseInt(z2) > 0, z2);

// A8 Esc 清空
await key("Escape");
check("A8 Esc＝清空选择", (await selCount()) === 0, await selCount());

// A8b 右键菜单
const bx = await J(`(() => { const n = document.querySelectorAll('.card-node')[0]; const r = n.getBoundingClientRect(); return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
await rightClick(bx.x, bx.y);
const menuInfo = await J(`(() => { const m = document.querySelector('.cv-menu'); return m ? { items: m.querySelectorAll('.cv-mi').length, text: m.innerText.split(String.fromCharCode(10)).join('|') } : null; })()`);
check("A8b 右键卡＝菜单出现且项数≥6", !!menuInfo && menuInfo.items >= 6, menuInfo ? menuInfo.items + " 项" : "无菜单");
await send("Page.captureScreenshot", { format: "png" }).then(async (r) => {
  const fs = await import("node:fs");
  fs.writeFileSync("../_shots/12_menu.png", Buffer.from(r.result.data, "base64"));
});
await key("Escape");
check("A8c Esc＝关闭菜单", (await ev(`document.querySelectorAll('.cv-menu').length`)) === 0);

// A8d 多选整组拖动（前面的缩放/平移已改变屏幕坐标，重新取）
const boxes2 = await J(`[...document.querySelectorAll('.card-node')].map(n => {
  const r = n.getBoundingClientRect();
  return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
})`);
// 用两张**非根卡**（根卡是布局锚点，拖它＝移动画布，不参与整组拖动）
const p1 = boxes2[1], p2 = boxes2[2];
await click(p1.x, p1.y);
await click(p2.x, p2.y, { modifiers: MOD.shift });
const before2 = await J(`[...document.querySelectorAll('.card-node.is-selected')].map(n => parseFloat(n.style.left))`);
await drag(p1.x, p1.y, p1.x + 90, p1.y + 50);
const after2 = await J(`[...document.querySelectorAll('.card-node.is-selected')].map(n => parseFloat(n.style.left))`);
const movedAll = before2.length >= 2 && before2.every((v, i) => Math.abs(after2[i] - v) > 40);
check("A8d 多选整组拖动（两张同时位移）", movedAll, `${JSON.stringify(before2)} → ${JSON.stringify(after2)}`);

// A8e ⌘K 搜索定位
await key("k", { code: "KeyK", modifiers: MOD.ctrl });
await sleep(320);
check("A8e Ctrl+K＝搜索面板出现", (await ev(`!!document.querySelector('.cv-palette')`)));
await send("Input.insertText", { text: "架构" });
await sleep(300);
const hitCount = Number(await ev(`document.querySelectorAll('.cv-palette-item').length`));
check("A8f 输入即筛（命中≥1）", hitCount >= 1, hitCount + " 条");
await send("Page.captureScreenshot", { format: "png" }).then(async (r) => {
  const fs = await import("node:fs");
  fs.writeFileSync("../_shots/17_palette.png", Buffer.from(r.result.data, "base64"));
});
await key("Enter");
await sleep(420);
const afterPal = await J(`({ sel: document.querySelectorAll('.card-node.is-selected').length, pal: !!document.querySelector('.cv-palette') })`);
check("A8g Enter＝定位并关闭面板", afterPal.sel === 1 && !afterPal.pal, JSON.stringify(afterPal));

// A8h/A8i 方向键在树中导航
await key("Escape");
const rootBox = await J(`(() => { const n=document.querySelector('.card-node'); const r=n.getBoundingClientRect(); return { x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2) }; })()`);
await click(rootBox.x, rootBox.y);
const navFrom = await ev(`(() => { const n=document.querySelector('.card-node.is-selected'); return n ? n.dataset.id : ''; })()`);
await key("ArrowRight", { code: "ArrowRight" });
const navRight = await ev(`(() => { const n=document.querySelector('.card-node.is-selected'); return n ? n.dataset.id : ''; })()`);
check("A8h →＝下钻到首个子卡", !!navRight && navRight !== navFrom, `${navFrom} → ${navRight}`);
await key("ArrowLeft", { code: "ArrowLeft" });
const navLeft = await ev(`(() => { const n=document.querySelector('.card-node.is-selected'); return n ? n.dataset.id : ''; })()`);
check("A8i ←＝回到父卡", navLeft === navFrom, `${navRight} → ${navLeft}`);

// A9 空格+拖＝平移（即使起点压在卡上）
const t2 = await worldTf();
await send("Input.dispatchKeyEvent", { type: "keyDown", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
await drag(cx(a), cy(a), cx(a) + 80, cy(a) + 40);
await send("Input.dispatchKeyEvent", { type: "keyUp", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
const t3 = await worldTf();
check("A9 空格+拖＝平移", t2 !== t3, `${t2} → ${t3}`);

// A10 双击进对话（A9 平移过，重新取坐标）
const before = await ev("location.pathname");
const b2 = await J(`(() => { const n = document.querySelectorAll('.card-node')[1]; const r = n.getBoundingClientRect(); return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
await dblclick(b2.x, b2.y);
const after = await ev("location.pathname");
check("A10 双击＝进入对话页", after.includes("/card/"), `${before} → ${after}`);

// A11 全程零 JS 异常
check("A11 零 JS 异常", bad.length === 0, JSON.stringify(bad.slice(0, 4)));

console.log(out.join("\n"));
console.log(ok ? "\nALL PASS" : "\nHAS FAIL");
ws.close();
child.kill();
process.exit(ok ? 0 : 1);
