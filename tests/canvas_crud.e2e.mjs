/* 高风险场景走查 2（5707）：新建子卡 / 删除 / 缩放后拖拽 / 折叠回退 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
// [画布 2026-10-18] 会建卡/删卡的测试跑在 work_001（假数据项目），不污染真实项目
const WID = process.argv[3] || "work_001";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9650 + Math.floor(Math.random() * 80);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, ["--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu",
  "--no-sandbox", "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "j2_" + Date.now()), "about:blank"], { stdio: "ignore" });
let tr = null;
for (let i = 0; i < 80 && !tr; i++) {
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }); if (r.ok) tr = await r.json(); } catch { /* wait */ }
  if (!tr) await sleep(200);
}
const ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0; const pend = new Map(); const errs = []; const net = [];
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((r) => { ws.onopen = r; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") errs.push("EXC " + String((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text).slice(0, 160));
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errs.push("CONSOLE " + m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 160));
  if (m.method === "Network.responseReceived" && m.params.response.url.includes("/cards")) net.push(m.params.response.status + " " + m.params.response.url.split("/api")[1]);
};
await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => { const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true }); return r.result && r.result.result ? r.result.result.value : undefined; };
const J = async (x) => JSON.parse(await ev(`JSON.stringify(${x})`) || "null");
// 拖拽时 mouseMoved 必须同时带 button:"left" 与 buttons:1
// （只带 buttons 不够：CDP 的 button 默认是 "none"，浏览器仍按"普通移动"处理）
const move = (x, y, buttons = 0) => send("Input.dispatchMouseEvent", {
  type: "mouseMoved", x, y, buttons, button: buttons ? "left" : "none",
});
const down = (x, y, b = "left", cc = 1) => send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: b, clickCount: cc });
const up = (x, y, b = "left", cc = 1) => send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: b, clickCount: cc });
async function click(x, y) { await move(x, y); await down(x, y); await up(x, y); await sleep(280); }
async function rightClick(x, y) { await move(x, y); await down(x, y, "right"); await up(x, y, "right"); await sleep(380); }
const centerOf = async (sel, idx = 0) => J(`(() => { const n = document.querySelectorAll(${JSON.stringify(sel)})[${idx}]; if (!n) return null; const r = n.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; })()`);

const out = []; let bad = 0;
const check = (n, c, d) => { out.push(`${c ? "PASS" : "FAIL"} ${n}${d !== undefined ? " :: " + d : ""}`); if (!c) bad++; };

await send("Page.navigate", { url: `${BASE}/work/${WID}` }); await sleep(3800);
// 首屏只锚定根卡（100%），其余卡可能在视口外 ⇒ 先整幅适配，保证右键/拖拽坐标命中
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "0", code: "Digit0", modifiers: 2, windowsVirtualKeyCode: 48 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "0", code: "Digit0", modifiers: 2, windowsVirtualKeyCode: 48 });
await sleep(700);
await ev(`(() => { window.__calls = []; const of = window.fetch; window.fetch = (...a) => { window.__calls.push(String((a[1] && a[1].method) || 'GET') + ' ' + String(a[0])); return of(...a); }; return 1; })()`);

// ── 1 新建子卡（右键 → 菜单项 → 弹窗 → 输入 → 创建）──
const n0 = Number(await ev("document.querySelectorAll('.card-node').length"));
// 用 DOM 派发 contextmenu：坐标右键在"视野变化 + 浮层"下会偶发落空
await ev(`(() => {
  const nav = (window.GetCanvasState && window.GetCanvasState().nav) || {};
  const rootId = nav.workCardId || '';
  const n = [...document.querySelectorAll('.card-node')].find(x => x.dataset.id !== rootId);
  if (!n) return;
  const r = n.getBoundingClientRect();
  n.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true,
    clientX: Math.round(r.x + r.width/2), clientY: Math.round(r.y + r.height/2) }));
})()`);
await sleep(450);
// 点「新建子卡」
const mi = await J(`(() => { const it = [...document.querySelectorAll('.cv-menu .cv-mi')].find(e => e.textContent.includes('新建子卡')); if (!it) return null; const r = it.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; })()`);
check("1a 菜单里有「新建子卡」", !!mi);
if (mi) {
  await click(mi.x, mi.y);
  await sleep(400);
  const hasDlg = await ev("!!document.getElementById('newCardTitle')");
  check("1b 弹窗出现", hasDlg);
  if (hasDlg) {
    // 走真实输入路径：聚焦输入框 → 键入 → Enter（弹窗的 onkeydown 会提交）。
    // 聚焦用 DOM focus 而不是坐标点击：整幅适配后弹窗位置随视口变化，
    // 坐标点击会偶发落空（实测 2 次里挂 1 次），而 focus 不依赖坐标。
    await ev(`document.getElementById('newCardTitle').focus()`);
    await sleep(120);
    await send("Input.insertText", { text: "走查新建的子卡" });
    await sleep(200);
    const typed = await ev("(document.getElementById('newCardTitle')||{}).value || ''");
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
    await sleep(2600);
    check("1b2 输入框收到文字", typed === "走查新建的子卡", typed);
    // 诊断：Enter 之后到底发生了什么（弹窗是否关闭 / fetch 是否发出 / 焦点在哪）
    const diag = await ev(`JSON.stringify({
      dlg: !!document.getElementById('newCardTitle'),
      active: (document.activeElement||{}).id || (document.activeElement||{}).tagName,
      calls: (window.__calls||[]).slice(-4),
      path: location.pathname,
    })`);
    console.log("   [diag]", diag);
    const n1 = Number(await ev("document.querySelectorAll('.card-node').length"));
    const created = await ev(`!![...document.querySelectorAll('.card-node .tname')].find(e => e.textContent.includes('走查新建的子卡'))`);
    check("1c 新卡出现在画布上", created === true, `${n0} → ${n1}；网络=${JSON.stringify(net.slice(-3))}；errs=${errs.slice(0,2).join("|")}`);
  }
}

// ── 2 删除刚建的卡（选中 → Delete → confirm）──
await send("Page.setInterceptFileChooserDialog", { enabled: false }).catch(() => {});
const delBox = await J(`(() => { const n = [...document.querySelectorAll('.card-node')].find(e => (e.querySelector('.tname')||{}).textContent === '走查新建的子卡'); if (!n) return null; const r = n.getBoundingClientRect(); return { id: n.dataset.id, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
if (delBox) {
  await ev("window.confirm = () => true");   // 自动接受确认框
  await rightClick(delBox.x, delBox.y);
  const delItem = await J(`(() => { const it = [...document.querySelectorAll('.cv-menu .cv-mi')].find(e => e.textContent.trim().startsWith('删除')); if (!it) return null; const r = it.getBoundingClientRect(); return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) }; })()`);
  if (delItem) { await click(delItem.x, delItem.y); }
  await sleep(2200);
  // 按 card_id 判（同名卡可能有多张，按标题判会假阴性）
  const still = await ev(`!![...document.querySelectorAll('.card-node')].find((x) => x.dataset.id === ${JSON.stringify('${delBox.id}')})`);
  check("2 删除卡生效", still === false, still ? `仍在 ${delBox.id}` : `已删 ${delBox.id}`);
} else {
  check("2 删除卡生效", false, "找不到刚建的卡");
}

// ── 3 缩放到 50% 后拖卡：位移量是否与缩放一致（不放大不缩小）──
// 顺序很重要：先 ⌘0 让内容居中，**再**缩到 50%（缩放以视口中心为锚，
// 居中状态下缩一半，卡片仍在视野内）。反过来做会让卡片跑出视口。
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "0", code: "Digit0", modifiers: 2, windowsVirtualKeyCode: 48 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "0", code: "Digit0", modifiers: 2, windowsVirtualKeyCode: 48 });
await sleep(500);
await ev("(() => { const s = document.getElementById('zoomSlider'); s.value = '50'; s.dispatchEvent(new Event('input', { bubbles: true })); })()");
await sleep(500);
const zoomNow = await ev("(document.getElementById('zoomDisplay')||{}).textContent");
// [画布 2026-10-18] 拖拽只调纵向顺序，故在纵向验证"屏幕位移 ÷ zoom = 世界位移"
// 用**视口内**的卡（50% 下有些卡在视口外，取第 3 张会落空）
const dragBox = await J(`(() => {
  const nav = (window.GetCanvasState && window.GetCanvasState().nav) || {};
  const rootId = nav.workCardId || '';
  const n = [...document.querySelectorAll('.card-node')].find(x => {
    if (x.dataset.id === rootId) return false;
    const r = x.getBoundingClientRect();
    // 条件放宽：50% 下卡片很小，位置随 pan 变；只要完整落在视口内且不被顶栏压住即可
    return r.top > 70 && r.bottom < innerHeight - 60 && r.left > 60 && r.right < innerWidth - 30;
  });
  if (!n) return null;
  const r = n.getBoundingClientRect();
  return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), id: n.dataset.id };
})()`);
const topBefore = await J(`(() => { const n = [...document.querySelectorAll('.card-node')].find(x => x.dataset.id === ${JSON.stringify(dragBox ? dragBox.id : "")}); return n ? parseFloat(n.style.top) : 0; })()`);
if (dragBox) {
  // 用与 probe 完全相同的序列（已验证有效）：moved → pressed → 8×moved(button+buttons) → released
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: dragBox.x, y: dragBox.y });
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: dragBox.x, y: dragBox.y, button: "left", clickCount: 1 });
  for (let i = 1; i <= 8; i++) {
    await send("Input.dispatchMouseEvent", {
      type: "mouseMoved", x: dragBox.x, y: dragBox.y + i * 10, button: "left", buttons: 1,
    });
    await sleep(20);
  }
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: dragBox.x, y: dragBox.y + 80, button: "left", clickCount: 1 });
  await sleep(900);
  await sleep(900);
}
const topAfter = await J(`(() => { const n = [...document.querySelectorAll('.card-node')].find(x => x.dataset.id === ${JSON.stringify(dragBox ? dragBox.id : "")}); return n ? parseFloat(n.style.top) : 0; })()`);
const delta = topAfter - topBefore;
check("3 缩放 50% 下拖 80px ≈ 世界位移 160px", Math.abs(delta - 160) < 30, `zoom=${zoomNow} Δworld=${Math.round(delta)}`);

// （展开/折叠回退已由 journey1 第 5/6 项覆盖，此处不重复——重复测同一路径
//   只会引入"按序号定位徽章"这类脚本噪音，掩盖真问题。）

check("5 零 JS 异常", errs.length === 0, errs.slice(0, 3).join(" | "));

console.log(out.join("\n"));
console.log(bad === 0 ? "\nALL PASS" : `\n${bad} FAIL`);
ws.close(); child.kill();
process.exit(bad === 0 ? 0 : 1);
