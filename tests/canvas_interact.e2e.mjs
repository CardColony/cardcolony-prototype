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
async function mouse(type, x, y, { button = "left", clickCount = 1, modifiers = 0, buttons = 0 } = {}) {
  // ⚠ 拖拽时 mouseMoved 必须带 buttons:1（表示左键按住），否则浏览器认为是
  // 普通移动，不触发拖拽（实测：不带 buttons 时卡片 top 一点不动）。
  await send("Input.dispatchMouseEvent", { type, x, y, button, clickCount, modifiers, buttons });
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
  const btnBit = button === "left" ? 1 : button === "right" ? 2 : button === "middle" ? 4 : 0;
  await mouse("mouseMoved", x1, y1, { modifiers });
  await mouse("mousePressed", x1, y1, { button, modifiers });
  for (let i = 1; i <= steps; i++) {
    await mouse("mouseMoved", x1 + ((x2 - x1) * i) / steps, y1 + ((y2 - y1) * i) / steps,
                { button, modifiers, buttons: btnBit });
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
// 首屏只锚定根卡（100%），其余卡可能在视口外 ⇒ 先整幅适配
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
await sleep(600);
const selCount = async () => Number(await ev(`document.querySelectorAll('.card-node.is-selected').length`));
const zoomText = async () => String(await ev(`(document.getElementById('zoomDisplay')||{}).textContent||''`));
const worldTf = async () => String(await ev(`(document.getElementById('world')||{style:{}}).style.transform||''`));

const a = boxes[0], b = boxes[1];
const cx = (o) => o.x + o.w / 2, cy = (o) => o.y + o.h / 2;

// A1 选中（单击＝选中+右栏读卡；整幅适配后坐标已变，重新取）
// ⚠ CDP 的 modifiers 位掩码只影响 mousedown/mouseup，**不影响浏览器合成的 click
// 事件的 shiftKey**（实测：带 modifiers:8 点击，处理器的 e.shiftKey 仍为 false）。
// 所以 Shift+单击用 DOM 派发，直接测处理器的修饰键分支。
await ev(`document.querySelectorAll('.card-node')[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true }))`);
await sleep(300);
check("A1 Shift+单击卡＝选中 1 张", (await selCount()) === 1, await selCount());
check("A1b Shift+单击不跳页", (await ev("location.pathname")) === `/work/${WID}`, await ev("location.pathname"));

// A2 Shift 多选（先整幅适配：首屏锚在根卡，第二张卡可能不在视口内）
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
await sleep(360);
await ev(`document.querySelectorAll('.card-node')[1].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true }))`);
await sleep(300);
check("A2 Shift+单击＝多选 2 张", (await selCount()) === 2, await selCount());

// A3 点空白取消
// 动态找一个真正空白的点：不在任何卡片矩形内，且避开左栏目录/右栏/小地图
const blank = await J(`(() => {
  const rects = [...document.querySelectorAll('.card-node')].map(n => n.getBoundingClientRect());
  const vp = document.getElementById('viewport').getBoundingClientRect();
  for (let y = vp.bottom - 40; y > vp.top + 60; y -= 20) {
    for (let x = vp.left + 40; x < vp.right - 360; x += 20) {
      if (!rects.some(r => x > r.left - 8 && x < r.right + 8 && y > r.top - 8 && y < r.bottom + 8)) {
        return { x: Math.round(x), y: Math.round(y) };
      }
    }
  }
  return { x: Math.round(vp.left + 40), y: Math.round(vp.bottom - 40) };
})()`);
await click(blank.x, blank.y);
check("A3 点空白＝清空选择", (await selCount()) === 0, await selCount());

// A4 Ctrl+滚轮缩放（2026-10-18 滚轮默认改纵向滚动，缩放走 Ctrl/⌘+滚轮）
const z0 = await zoomText();
await wheel(cx(a), cy(a), -240, { modifiers: MOD.ctrl });
const z1 = await zoomText();
check("A4 Ctrl+滚轮＝放大", parseInt(z1) > parseInt(z0), `${z0} → ${z1}`);

// A4b 普通滚轮＝纵向滚动（阅读姿势）
const tf0 = await worldTf();
await wheel(cx(a), cy(a), -240);
const tf1 = await worldTf();
check("A4b 滚轮＝滚动（不缩放）", tf0 !== tf1 && (await zoomText()) === z1, `${tf0.slice(0,28)} → ${tf1.slice(0,28)}`);

// A5 中键平移
const t0 = await worldTf();
await drag(cx(a), cy(a), cx(a) + 120, cy(a) + 60, { button: "middle" });
const t1 = await worldTf();
check("A5 中键拖＝平移", t0 !== t1, `${t0} → ${t1}`);

// A6 Shift+拖空白＝框选（先整幅适配：A5 中键平移过，视野已变，卡片可能跑出框选范围）
await key("Escape");
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
await sleep(400);
// 起点用动态空白点（90% 下卡片位置变了，写死坐标可能压在卡上，框选就变成拖卡）
const startBlank = await J(`(() => {
  const rects = [...document.querySelectorAll('.card-node')].map(n => n.getBoundingClientRect());
  const vp = document.getElementById('viewport').getBoundingClientRect();
  for (let y = vp.top + 40; y < vp.bottom - 40; y += 24) {
    for (let x = vp.left + 40; x < vp.right - 60; x += 24) {
      if (!rects.some(r => x > r.left - 10 && x < r.right + 10 && y > r.top - 10 && y < r.bottom + 10)) {
        return { x: Math.round(x), y: Math.round(y) };
      }
    }
  }
  return null;
})()`);
if (startBlank) await drag(startBlank.x, startBlank.y, 1500, 940, { modifiers: MOD.shift });
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
  fs.writeFileSync("_shots/12_menu.png", Buffer.from(r.result.data, "base64"));
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
// 选**视口内**的两张卡（90% 下部分卡在视口外，拖它们会落空）
const pair = await J(`(() => {
  // 排除根卡：拖根卡＝平移画布（它是布局原点），不参与整组拖动
  const nav = (window.GetCanvasState && window.GetCanvasState().nav) || {};
  const rootId = nav.workCardId || nav.workRootId || '';
  const inView = [...document.querySelectorAll('.card-node')].filter(n => {
    if (n.dataset.id === rootId) return false;
    const r = n.getBoundingClientRect();
    return r.top > 60 && r.bottom < innerHeight - 20 && r.left > 0 && r.right < innerWidth;
  });
  if (inView.length < 2) return null;
  inView[0].dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));
  inView[1].dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));
  const r = inView[0].getBoundingClientRect();
  return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
})()`);
await sleep(300);
// [画布 2026-10-18] 拖拽＝只调纵向顺序（横向锁定在列上），故断言 top 变化
const before2 = await J(`[...document.querySelectorAll('.card-node.is-selected')].map(n => parseFloat(n.style.top))`);
const left2 = await J(`[...document.querySelectorAll('.card-node.is-selected')].map(n => parseFloat(n.style.left))`);
if (pair) await drag(pair.x, pair.y, pair.x, pair.y + 90);
const after2 = await J(`[...document.querySelectorAll('.card-node.is-selected')].map(n => parseFloat(n.style.top))`);
const leftAfter = await J(`[...document.querySelectorAll('.card-node.is-selected')].map(n => parseFloat(n.style.left))`);
const movedAll = before2.length >= 2 && before2.every((v, i) => Math.abs(after2[i] - v) > 30);
check("A8d 多选整组拖动（两张纵向同时位移）", movedAll, `${JSON.stringify(before2)} → ${JSON.stringify(after2)}`);
check("A8d2 整组拖动时横向锁定", left2.every((v, i) => Math.abs(leftAfter[i] - v) < 2), `${JSON.stringify(left2)} → ${JSON.stringify(leftAfter)}`);

// A8j 多选操作条（A8d 拖完仍是 2 张选中）
const barText = await ev(`(() => { const b = document.querySelector('.cv-selbar'); return b ? b.innerText.split(String.fromCharCode(10)).join(' ') : ''; })()`);
check("A8j 多选＝操作条出现并显示数量", barText.includes("已选"), barText);
await send("Page.captureScreenshot", { format: "png" }).then(async (r) => {
  const fs = await import("node:fs");
  fs.writeFileSync("_shots/24_selbar.png", Buffer.from(r.result.data, "base64"));
});

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
  fs.writeFileSync("_shots/17_palette.png", Buffer.from(r.result.data, "base64"));
});
await key("Enter");
await sleep(420);
const afterPal = await J(`({ sel: document.querySelectorAll('.card-node.is-selected').length, pal: !!document.querySelector('.cv-palette') })`);
check("A8g Enter＝定位并关闭面板", afterPal.sel === 1 && !afterPal.pal, JSON.stringify(afterPal));

// A8h/A8i 方向键在树中导航（先适配，保证根卡在视口内可点）
await key("Escape");
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
await sleep(320);
const rootBox = await J(`(() => { const n=document.querySelector('.card-node'); const r=n.getBoundingClientRect(); return { x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2) }; })()`);
await click(rootBox.x, rootBox.y, { modifiers: MOD.shift });
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

// A10 单击进对话（A9 平移过：先整幅适配，保证目标卡在视口内再取坐标）
await key("0", { code: "Digit0", modifiers: MOD.ctrl });
await sleep(300);
const before = await ev("location.pathname");
const b2 = await J(`(() => { const n = document.querySelectorAll('.card-node')[1]; const r = n.getBoundingClientRect(); return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
await click(b2.x, b2.y);
await sleep(300);
const afterClick = await ev("location.pathname");
check("A10a 单击＝选中不跳页", afterClick === `/work/${WID}`, afterClick);
await ev(`document.querySelectorAll('.card-node .cv-open')[1].click()`);
await sleep(1100);
const after = await ev("location.pathname");
check("A10b 点 ↗ ＝进入对话页", after.includes("/card/"), `${before} → ${after}`);

// A11 全程零 JS 异常
check("A11 零 JS 异常", bad.length === 0, JSON.stringify(bad.slice(0, 4)));

console.log(out.join("\n"));
console.log(ok ? "\nALL PASS" : "\nHAS FAIL");
ws.close();
child.kill();
process.exit(ok ? 0 : 1);
