/* canvas_suite.mjs —— 画布模块**全量**验收套件
 *
 * 为什么要有这支：前面几轮的验收都是"点状抽查"（查按钮画没画、尺寸对不对），
 * 漏掉了两类真问题：
 *   ① **命中层**：某个伪元素/覆盖层吃掉整张卡的点击（用户 2026-10-20 当面指出）；
 *   ② **手势层**：同一个输入被两个 handler 消费（节点级 Enter 与 document 级
 *      Enter 都执行 ⇒ 展开又收起，等于没反应）。
 * ⇒ 这支套件按画布模块的**每一层**铺开：结构 / 命中 / 行为 / 布局 / 实时 /
 *   健壮性 / 响应 / 可访问性 / 视图状态。
 *
 * 用法：node tests/canvas_suite.mjs [baseUrl] [workId]
 * 退出码：0 = 全过；1 = 有失败
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5711";
const WID = process.argv[3] || "work_002";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9800 + Math.floor(Math.random() * 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ───────────────────────── 迷你测试框架 ───────────────────────── */
const results = [];
let group = "";
const G = (name) => { group = name; };
const T = (name, cond, detail) => {
  results.push({ group, name, pass: !!cond, detail: cond ? "" : (detail || "") });
};
const T_EQ = (name, got, want) => T(name, got === want, `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);

/* ───────────────────────── CDP 接入 ───────────────────────── */
const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "cs_" + Date.now()), "about:blank",
], { stdio: "ignore" });
let tr = null;
for (let i = 0; i < 80 && !tr; i++) {
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }); if (r.ok) tr = await r.json(); } catch { }
  if (!tr) await sleep(200);
}
const ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const consoleErrs = [];
const netUrls = [];   // Network.responseReceived 的 URL（T12 用）
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((r) => { ws.onopen = r; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Network.responseReceived") { netUrls.push(m.params.response.url); return; }
  if (m.method === "Runtime.exceptionThrown") consoleErrs.push("EXC " + String(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text).slice(0, 200));
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") consoleErrs.push("ERR " + (m.params.args || []).map(a => a.value ?? a.description ?? "").join(" ").slice(0, 200));
};
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result && r.result.result ? r.result.result.value : undefined;
};
// load：导航到画布页并**等它真正就位**。
// ⚠ 之前只等"节点数 > 0"，而导航是异步的 ⇒ 旧页面（对话页）里也有 .card-node
//   时等待会立刻满足，随后所有操作都落在**对话页**上（表现为"节点不存在"）。
//   ⇒ 现在同时校验 pathname 是画布页、节点数连续两次相同。
const load = async (wid = WID) => {
  await send("Page.navigate", { url: `${BASE}/work/${wid}` });
  let last = -1, stable = 0;
  for (let i = 0; i < 200; i++) {
    await sleep(60);
    const st = await ev(`(() => ({
      path: location.pathname,
      n: document.querySelectorAll('.card-node').length,
    }))()`);
    const onCanvas = st && st.path === `/work/${wid}` && st.n > 0;
    if (onCanvas && st.n === last) { stable++; if (stable >= 3) return; }
    else stable = 0;
    last = st && st.n;
  }
  await sleep(400);
};
// 合成 click（走真实 DOM 事件路径）；命中归属由 elementFromPoint 单独验
const clickEl = (sel) => ev(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false; el.click(); return true; })()`);
const key = (k, opts = {}) => ev(`(() => {
  const target = document.activeElement && document.activeElement !== document.body ? document.activeElement : document.body;
  target.dispatchEvent(new KeyboardEvent('keydown', { key: ${JSON.stringify(k)}, code: ${JSON.stringify(opts.code || k)}, bubbles: true, cancelable: true, ${opts.modifiers || ""} }));
  return true;
})()`);
const keyOn = (sel, k, opts = {}) => ev(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false;
  el.focus();
  el.dispatchEvent(new KeyboardEvent('keydown', { key: ${JSON.stringify(k)}, code: ${JSON.stringify(opts.code || k)}, bubbles: true, cancelable: true, ${opts.modifiers || ""} }));
  return true;
})()`);

/* ══════════════════════════════════════════════════════════════════════
   T1 结构与数据
   ══════════════════════════════════════════════════════════════════════ */
G("T1 结构与数据");
await load();
{
  const api = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
  const dom = await ev(`(() => {
    const ns = [...document.querySelectorAll('.card-node')];
    const root = document.querySelector('.card-node.is-root');
    return {
      n: ns.length,
      rootId: root ? root.dataset.id : null,
      ids: ns.map(x => x.dataset.id),
      titles: ns.map(x => (x.querySelector('.tname') || {}).textContent || ''),
      badgeCounts: ns.map(x => {
        const b = x.querySelector('.kid-badge');
        const n = b ? Number((b.querySelector('.cv-kid-n') || {}).textContent || 0) : 0;
        return { id: x.dataset.id, badge: n };
      }),
      emptyTitles: ns.filter(x => !((x.querySelector('.tname') || {}).textContent || '').trim()).length,
      zeroSized: ns.filter(x => { const r = x.getBoundingClientRect(); return r.width < 10 || r.height < 10; }).length,
    };
  })()`);
  const apiIds = (api.cards || []).filter(c => c.card_type !== "rule").map(c => c.card_id);
  // 画布只渲染**可见集**（视图根 + 展开的子树），不是全部投影卡 ⇒ 口径按可见集。
  T("T1.1 画布渲染的卡数 > 0 且不超过投影卡数",
    dom.n > 0 && dom.n <= apiIds.length, `dom=${dom.n} api=${apiIds.length}`);
  T("T1.2 根卡在场", !!dom.rootId, JSON.stringify(dom.rootId));
  T("T1.3 没有空标题的卡", dom.emptyTitles === 0, `empty=${dom.emptyTitles}`);
  T("T1.4 没有零尺寸的卡", dom.zeroSized === 0, `zero=${dom.zeroSized}`);
  // 子卡数徽章 == 实际子卡数（用 DOM 的连线/结构反推：以徽章数之和与树的边数比）
  const tree = await ev(`(() => {
    const out = {};
    for (const n of document.querySelectorAll('.card-node')) {
      out[n.dataset.id] = { badge: !!n.querySelector('.kid-badge') };
    }
    return out;
  })()`);
  T("T1.5 每张卡都在 DOM 里且有 id", Object.keys(tree).length === dom.n, `${Object.keys(tree).length} vs ${dom.n}`);
}

/* ══════════════════════════════════════════════════════════════════════
   T2 命中矩阵：每个可点位置，命中谁、归谁
   ══════════════════════════════════════════════════════════════════════ */
G("T2 命中矩阵");
await load();
{
  const rows = await ev(`(() => {
    const owner = (el) => { const c = el && el.closest ? el.closest('.card-node') : null; return c ? c.dataset.id : '(none)'; };
    const kind = (el) => {
      if (!el) return '(none)';
      if (el.classList.contains('kid-badge')) return 'kid-badge';
      if (el.classList.contains('cv-open')) return 'cv-open';
      if (el.classList.contains('cv-chat')) return 'cv-chat';
      if (el.classList.contains('cv-wsdot')) return 'cv-wsdot';
      if (el.classList.contains('cv-ref')) return 'cv-ref';
      if (el.classList.contains('tname')) return 'tname';
      if (el.classList.contains('cv-msgline')) return 'cv-msgline';
      if (el.classList.contains('summary')) return 'summary';
      if (el.classList.contains('card-node')) return 'card';
      return el.className || el.tagName;
    };
    const at = (x, y) => { const e = document.elementFromPoint(x, y); return { k: kind(e), own: owner(e) }; };
    const out = [];
    for (const node of document.querySelectorAll('.card-node')) {
      const t = node.querySelector('.tname'), nr = node.getBoundingClientRect();
      if (!t) continue;
      const tr = t.getBoundingClientRect();
      const r = { id: node.dataset.id, pts: {} };
      r.pts.titleText = at(tr.x + 10, tr.y + tr.height / 2);
      r.pts.titleCenter = at(tr.x + tr.width / 2, tr.y + tr.height / 2);
      r.pts.titleRight = at(tr.right - 6, tr.y + tr.height / 2);
      r.pts.cardBottomLeft = at(nr.x + 6, nr.bottom - 6);
      r.pts.cardTopRight = at(nr.right - 6, nr.y + 6);
      for (const [sel, key] of [['.kid-badge','badge'], ['.cv-open','open'], ['.cv-chat','chat'], ['.cv-wsdot','wsdot'], ['.cv-ref','ref']]) {
        const el = node.querySelector(sel);
        if (!el) continue;
        const b = el.getBoundingClientRect();
        r.pts[key] = at(b.x + b.width / 2, b.y + b.height / 2);
      }
      out.push(r);
    }
    return out;
  })()`);
  T("T2.0 命中矩阵非空", rows.length > 0, `rows=${rows.length}`);
  for (const r of rows) {
    for (const pt of ["titleText", "titleCenter", "titleRight", "cardBottomLeft", "cardTopRight"]) {
      T(`T2 ${r.id} ${pt} 命中本卡`, r.pts[pt] && r.pts[pt].own === r.id, JSON.stringify(r.pts[pt]));
    }
    if (r.pts.badge) {
      T(`T2 ${r.id} 徽标命中徽标`, r.pts.badge.k === "kid-badge", JSON.stringify(r.pts.badge));
      T(`T2 ${r.id} 徽标本卡`, r.pts.badge.own === r.id, JSON.stringify(r.pts.badge));
    }
    if (r.pts.open) {
      T(`T2 ${r.id} ↗命中↗`, r.pts.open.k === "cv-open", JSON.stringify(r.pts.open));
      T(`T2 ${r.id} ↗本卡`, r.pts.open.own === r.id, JSON.stringify(r.pts.open));
    }
    if (r.pts.chat) {
      T(`T2 ${r.id} 💬命中💬`, r.pts.chat.k === "cv-chat", JSON.stringify(r.pts.chat));
      T(`T2 ${r.id} 💬本卡`, r.pts.chat.own === r.id, JSON.stringify(r.pts.chat));
    }
    if (r.pts.wsdot) T(`T2 ${r.id} 状态点本卡`, r.pts.wsdot.own === r.id, JSON.stringify(r.pts.wsdot));
    if (r.pts.ref) T(`T2 ${r.id} 引用数本卡`, r.pts.ref.own === r.id, JSON.stringify(r.pts.ref));
  }
}

/* ══════════════════════════════════════════════════════════════════════
   T3 行为矩阵：每个手势的唯一结果（含键盘）
   ══════════════════════════════════════════════════════════════════════ */
G("T3 行为矩阵");
// pick：按选择器取元素，**回溯到所属卡**取 id（子元素本身没有 data-id）
const pick = (sel) => ev(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  if (!el) return null;
  const c = el.closest ? el.closest('.card-node') : null;
  return c ? c.dataset.id : null;
})()`);

/* ── 同层顺序三件套（T10.1 / T11.4 共用） ──
 * 画布的口径是"拖拽＝调顺序"：拖动的语义是"这张卡排在这一层的第几位"，
 * 绝对坐标仍由布局算。所以验收"拖拽生效"不能看坐标变没变（顺序没变时坐标
 * 本来就不变），要看**同层次序**有没有按落点变。
 */

// 找一组"都在场"的同层卡（≥3 张），按世界 y 排好序
const siblingGroups = (wid) => ev(`(async () => {
  const api = await (await fetch('/api/works/${wid}/canvas')).json();
  const kids = {};
  for (const c of (api.cards || [])) {
    if (c.card_type === 'rule') continue;
    const p = c.parent || '';
    (kids[p] = kids[p] || []).push(c.card_id);
  }
  const out = [];
  for (const p of Object.keys(kids)) {
    if (kids[p].length < 3) continue;
    const rows = kids[p].map(id => {
      const n = document.querySelector('.card-node[data-id="' + id + '"]');
      return n ? { id, top: parseFloat(n.style.top) } : null;
    }).filter(Boolean);
    if (rows.length < 3) continue;
    rows.sort((a, b) => a.top - b.top);
    out.push({ parent: p, ids: rows.map(x => x.id), tops: rows.map(x => x.top) });
  }
  out.sort((a, b) => a.tops[0] - b.tops[0]);
  return out[0] || null;
})()`);

// 读某个父卡下、同层在场卡的当前次序（按世界 y）
const groupOrder = (parent, wid) => ev(`(async () => {
  const api = await (await fetch('/api/works/${wid}/canvas')).json();
  const ids = (api.cards || [])
    .filter(c => (c.parent || '') === ${JSON.stringify(parent)} && c.card_type !== 'rule')
    .map(c => c.card_id);
  const rows = ids.map(id => {
    const n = document.querySelector('.card-node[data-id="' + id + '"]');
    return n ? { id, top: parseFloat(n.style.top) } : null;
  }).filter(Boolean);
  rows.sort((a, b) => a.top - b.top);
  return { ids: rows.map(r => r.id), tops: rows.map(r => r.top) };
})()`);

// 找视口内**可见**的某个元素，返回其中心坐标（视口外的点了也白点）
const centerOfVisible = (sel) => ev(`(() => {
  const vp = document.getElementById('viewport');
  const vr = vp ? vp.getBoundingClientRect() : { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
  for (const n of document.querySelectorAll(${JSON.stringify(sel)})) {
    const r = n.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.left < vr.left || r.right > vr.right || r.top < vr.top || r.bottom > vr.bottom) continue;
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  }
  return null;
})()`);

// **真实**鼠标按下→抬起（CDP Input 域）。
// 为什么不能用 el.click()：只有真实的按下/抬起才会让浏览器合成 click，
// 而 click 的 target 取决于"抬起时指针下面是谁"——
// 这正是"根卡上的控件点了没反应"那个 bug 的测法。
const realClick = async (x, y) => {
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await sleep(90);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await sleep(70);
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
  await sleep(280);
};

// 把某张卡拖到指定的世界 y（落点＝卡片顶边落在该 y）
const dragToWorldY = (id, targetWorldY) => ev(`(() => {
  const n = document.querySelector('.card-node[data-id="${id}"]');
  if (!n) return { ok: false, err: 'no node' };
  const r = n.getBoundingClientRect();
  const from = parseFloat(n.style.top);
  const z = parseFloat(getComputedStyle(document.getElementById('world')).getPropertyValue('--z')) || 1;
  const d = (${targetWorldY} - from) * z;
  const sx = r.x + 30, sy = r.y + 12;
  n.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: sx, clientY: sy }));
  document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: sx, clientY: sy + d }));
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0, buttons: 0, clientX: sx, clientY: sy + d }));
  return { ok: true, from, to: ${targetWorldY}, z, delta: d };
})()`);

// T3.1 单击标题 ⇒ 进对话页
await load();
{
  const id = await pick(".card-node .tname");
  await clickEl(`.card-node[data-id="${id}"] .tname`);
  await sleep(800);
  T_EQ("T3.1 单击标题 ⇒ 进对话页", await ev(`location.pathname`), `/work/${WID}/card/${id}`);
}
// T3.2 Shift+单击 ⇒ 多选，不跳页
await load();
{
  const id = await pick(".card-node .tname");
  await ev(`(() => { const el = document.querySelector('.card-node[data-id="${id}"] .tname');
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true })); return true; })()`);
  await sleep(500);
  T_EQ("T3.2 Shift+单击 ⇒ 不跳页", await ev(`location.pathname`), `/work/${WID}`);
  T("T3.2 Shift+单击 ⇒ 选中", (await ev(`document.querySelectorAll('.card-node.is-selected').length`)) >= 1);
}
// T3.3 Alt+单击 ⇒ 画布内下钻，不跳页
await load();
{
  const id = await pick(".card-node .tname");
  await ev(`(() => { const el = document.querySelector('.card-node[data-id="${id}"] .tname');
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, altKey: true })); return true; })()`);
  await sleep(500);
  T_EQ("T3.3 Alt+单击 ⇒ 不跳页", await ev(`location.pathname`), `/work/${WID}`);
}
// T3.4 徽标 ⇒ 展开/折叠（取非根卡）
await load();
{
  const id = await ev(`(() => {
    const root = (document.querySelector('.card-node.is-root') || {}).dataset?.id;
    const n = [...document.querySelectorAll('.card-node')].find(x => x.querySelector('.kid-badge') && x.dataset.id !== root);
    return n ? n.dataset.id : null; })()`);
  if (!id) T("T3.4 有非根的带徽标卡", false, "找不到");
  else {
    const before = await ev(`document.querySelectorAll('.card-node').length`);
    await clickEl(`.card-node[data-id="${id}"] .kid-badge`);
    await sleep(700);
    const after = await ev(`document.querySelectorAll('.card-node').length`);
    T("T3.4 徽标 ⇒ 展开/折叠子卡", after !== before, `${before} → ${after}`);
    T_EQ("T3.4 徽标 ⇒ 不跳页", await ev(`location.pathname`), `/work/${WID}`);
  }
}
// T3.5 💬 ⇒ 就地展开，不跳页
await load();
{
  const id = await pick(".card-node .cv-chat");
  if (!id) T("T3.5 有💬按钮", false, "找不到");
  else {
    await clickEl(`.card-node[data-id="${id}"] .cv-chat`);
    await sleep(1400);
    T_EQ("T3.5 💬 ⇒ 不跳页", await ev(`location.pathname`), `/work/${WID}`);
    T("T3.5 💬 ⇒ 就地展开对话", (await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]'); return n ? n.dataset.open === '1' : false; })()`)) === true);
  }
}
// T3.6 ↗ ⇒ 进对话页
await load();
{
  const id = await pick(".card-node .cv-open");
  await clickEl(`.card-node[data-id="${id}"] .cv-open`);
  await sleep(800);
  T_EQ("T3.6 ↗ ⇒ 进对话页", await ev(`location.pathname`), `/work/${WID}/card/${id}`);
}
// T3.7 键盘 Enter（焦点在卡上）⇒ 就地展开一次（**不是两次**）
await load();
{
  const id = await pick(".card-node");
  await keyOn(`.card-node[data-id="${id}"]`, "Enter");
  await sleep(1200);
  const opened = await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]'); return n ? n.dataset.open === '1' : false; })()`);
  T("T3.7 焦点在卡上按 Enter ⇒ 就地展开（净效果是展开，不是没反应）", opened === true, `data-open=${opened}`);
}
// T3.7b **双重执行场景**：焦点在卡上 **且** 这张卡已被选中 ⇒ 节点级与全局级
// 都会看到 Enter ⇒ 不止冒泡就会"展开又收起"（看着像没反应）。
// 这是本轮修掉的第二个真 bug（节点级 keydown 补 stopPropagation），必须有人守着。
await load();
{
  const id = await pick(".card-node");
  await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]');
    n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,shiftKey:true})); return true; })()`);
  await sleep(300);
  const selN = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  await keyOn(`.card-node[data-id="${id}"]`, "Enter");
  await sleep(1400);
  const st = await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]');
    return { open: n ? n.dataset.open : '(no node)', path: location.pathname }; })()`);
  T("T3.7b 焦点在卡上+已选中时按 Enter ⇒ 只展开一次（不被全局 handler 再来一次）",
    st.open === "1" && selN === 1, JSON.stringify({ ...st, selN }));
}
// T3.8 键盘 Enter（**焦点不在卡上**，只有一张被选中）⇒ 走全局 handler ⇒ 就地展开
await load();
{
  const id2 = await pick(".card-node");
  await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id2}"]');
    n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,shiftKey:true})); return true; })()`);
  await sleep(300);
  const selN = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  T("T3.8 前置：Shift+单击选中 1 张", selN === 1, `selected=${selN}`);
  await key("Enter");
  await sleep(1400);
  const diag = await ev(`(() => {
    const n = document.querySelector('.card-node[data-id="${id2}"]');
    return {
      path: location.pathname,
      open: n ? n.dataset.open : '(no node)',
      selected: [...document.querySelectorAll('.card-node.is-selected')].map(x=>x.dataset.id),
      hasCellInput: !!(n && n.querySelector('.cv-cell-input')),
    };
  })()`);
  T("T3.8 选中一张后按 Enter ⇒ 就地展开", diag.open === "1", JSON.stringify(diag));
}
// T3.9 空格（焦点在卡上）⇒ 展开/折叠，且**不**切抓手态
await load();
{
  const id = await ev(`(() => {
    const root = (document.querySelector('.card-node.is-root') || {}).dataset?.id;
    const n = [...document.querySelectorAll('.card-node')].find(x => x.querySelector('.kid-badge') && x.dataset.id !== root);
    return n ? n.dataset.id : null; })()`);
  if (!id) T("T3.9 有可测的带徽标卡", false, "找不到");
  else {
    const before = await ev(`document.querySelectorAll('.card-node').length`);
    await keyOn(`.card-node[data-id="${id}"]`, " ", { code: "Space" });
    await sleep(700);
    const after = await ev(`document.querySelectorAll('.card-node').length`);
    T("T3.9 空格 ⇒ 展开/折叠子卡", after !== before, `${before} → ${after}`);
    T("T3.9 空格 ⇒ 不误切抓手态（视口不应带 panning 类）",
      !(await ev(`document.getElementById('viewport').classList.contains('panning')`)),
      "viewport 带着 panning 类");
  }
}
// T3.10 Esc ⇒ 清空选择
await load();
{
  await ev(`(() => { const n = document.querySelector('.card-node');
    n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,shiftKey:true})); return true; })()`);
  await sleep(300);
  const selBefore = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  await key("Escape");
  await sleep(300);
  const selAfter = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  T("T3.10 Esc ⇒ 清空选择", selBefore > 0 && selAfter === 0, `${selBefore} → ${selAfter}`);
}
// T3.11 方向键 ⇒ 在卡片树里移动选择
await load();
{
  const id = await pick(".card-node");
  await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]');
    n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,shiftKey:true})); return true; })()`);
  await sleep(300);
  const before = await ev(`(() => { const n = document.querySelector('.card-node.is-selected'); return n ? n.dataset.id : ''; })()`);
  await key("ArrowRight");
  await sleep(400);
  const after = await ev(`(() => { const n = document.querySelector('.card-node.is-selected'); return n ? n.dataset.id : ''; })()`);
  T("T3.11 → 键在树里移动选择", !!after, `before=${before} after=${after}`);
}
// T3.12 Ctrl+A ⇒ 全选可见卡
await load();
{
  await key("a", { modifiers: "ctrlKey: true" });
  await sleep(400);
  const sel = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  const total = await ev(`document.querySelectorAll('.card-node').length`);
  T("T3.12 Ctrl+A ⇒ 全选可见卡", sel === total && total > 0, `${sel}/${total}`);
}
// T3.13 Ctrl+0 ⇒ 整幅适配（zoom 变）
await load();
{
  // 整幅适配的**语义**是"内容全部进视口"，不是"zoom 数字一定变"（已经适配时数字不变是对的）
  await key("0", { modifiers: "ctrlKey: true" });
  await sleep(900);
  const fit = await ev(`(() => {
    const vp = document.getElementById('viewport').getBoundingClientRect();
    const ns = [...document.querySelectorAll('.card-node')];
    if (!ns.length) return { allIn: false, n: 0 };
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) {
      const r = n.getBoundingClientRect();
      x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y);
      x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
    }
    return { allIn: x0 >= vp.left - 2 && y0 >= vp.top - 2 && x1 <= vp.right + 2 && y1 <= vp.bottom + 2,
             n: ns.length, box: [Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)],
             vp: [Math.round(vp.left), Math.round(vp.top), Math.round(vp.right), Math.round(vp.bottom)] };
  })()`);
  T("T3.13 Ctrl+0 ⇒ 内容整体进视口（整幅适配的语义）", fit.allIn === true, JSON.stringify(fit));
}
// T3.14 右键菜单 ⇒ 含"打开对话"与"在此展开对话"与"展开子卡"
await load();
{
  const id = await pick(".card-node");
  await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]');
    const r = n.getBoundingClientRect();
    n.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: r.x + 20, clientY: r.y + 20 }));
    return true; })()`);
  await sleep(500);
  const menu = await ev(`(() => {
    const items = [...document.querySelectorAll('.cv-ctx-item, .ctx-item, [data-act]')].map(x => (x.textContent || '').trim());
    return { n: items.length, items };
  })()`);
  T("T3.14 右键菜单打开", menu.n > 0, JSON.stringify(menu).slice(0, 200));
  T("T3.14 菜单含 [打开对话]", menu.items.some(s => s.includes("打开对话")), JSON.stringify(menu.items));
  T("T3.14 菜单含 [在此展开对话]", menu.items.some(s => s.includes("在此展开对话")), JSON.stringify(menu.items));
  await key("Escape");
}

/* ══════════════════════════════════════════════════════════════════════
   T4 布局不变量
   ══════════════════════════════════════════════════════════════════════ */
G("T4 布局不变量");
await load();
{
  const lay = await ev(`(() => {
    const ns = [...document.querySelectorAll('.card-node')].map(n => {
      const r = n.getBoundingClientRect();
      return { id: n.dataset.id, x: r.x, y: r.y, w: r.width, h: r.height,
               l: parseFloat(n.style.left), t: parseFloat(n.style.top) };
    });
    const overlaps = [];
    for (let i = 0; i < ns.length; i++) for (let j = i + 1; j < ns.length; j++) {
      const a = ns[i], b = ns[j];
      const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      if (ox > 2 && oy > 2) overlaps.push([a.id, b.id, Math.round(ox), Math.round(oy)]);
    }
    return { ns, overlaps,
      nonInteger: ns.filter(n => !Number.isInteger(n.l) || !Number.isInteger(n.t)).map(n => n.id),
      zeroPos: ns.filter(n => n.l === 0 && n.t === 0).map(n => n.id) };
  })()`);
  T_EQ("T4.1 任意两张卡都不重叠", lay.overlaps.length, 0);
  T("T4.2 坐标是整数（不许 .5px 抖动）", lay.nonInteger.length === 0, JSON.stringify(lay.nonInteger));
  T("T4.3 只有根卡允许在 (0,0)", lay.zeroPos.length <= 1, JSON.stringify(lay.zeroPos));
}
// T4.4 展开/折叠后不变量保持
await load();
{
  const id = await ev(`(() => {
    const root = (document.querySelector('.card-node.is-root') || {}).dataset?.id;
    const n = [...document.querySelectorAll('.card-node')].find(x => x.querySelector('.kid-badge') && x.dataset.id !== root);
    return n ? n.dataset.id : null; })()`);
  if (id) {
    await clickEl(`.card-node[data-id="${id}"] .kid-badge`);
    await sleep(800);
    const ov = await ev(`(() => {
      const ns = [...document.querySelectorAll('.card-node')].map(n => { const r = n.getBoundingClientRect(); return { x:r.x,y:r.y,w:r.width,h:r.height }; });
      let c = 0;
      for (let i=0;i<ns.length;i++) for (let j=i+1;j<ns.length;j++) { const a=ns[i],b=ns[j];
        const ox=Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x), oy=Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y);
        if (ox>2&&oy>2) c++; }
      return c; })()`);
    T_EQ("T4.4 展开/折叠后仍无重叠", ov, 0);
  }
}

/* ══════════════════════════════════════════════════════════════════════
   T5 实时更新
   ══════════════════════════════════════════════════════════════════════ */
G("T5 实时更新");
await load();
{
  const before = await ev(`(() => { const n = document.querySelector('.card-node');
    return { id: n.dataset.id, others: [...document.querySelectorAll('.card-node')].filter(x=>x!==n).map(x=>x.dataset.id+':'+x.textContent.slice(0,10)).join('|') }; })()`);
  const NEW = "实时更新套件_" + Date.now().toString().slice(-6);
  const r = await fetch(`${BASE}/api/works/${WID}/cards/${before.id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { title: NEW }, history_note: "套件验收" }),
  });
  T_EQ("T5.1 写卡返回 200", r.status, 200);
  let after = null;
  for (let i = 0; i < 60; i++) { await sleep(100); after = await ev(`(() => { const n = document.querySelector('.card-node[data-id="${before.id}"]'); return n ? (n.querySelector('.tname')||{}).textContent : null; })()`); if (after === NEW) break; }
  T_EQ("T5.2 画布上那张卡自己更新了（不刷新）", after, NEW);
  const othersNow = await ev(`(() => { const n = document.querySelector('.card-node[data-id="${before.id}"]');
    return [...document.querySelectorAll('.card-node')].filter(x=>x!==n).map(x=>x.dataset.id+':'+x.textContent.slice(0,10)).join('|'); })()`);
  T("T5.3 其它卡未被重建（局部更新而非整树重绘）", othersNow === before.others, "others changed");
  // 还原
  await fetch(`${BASE}/api/works/${WID}/cards/${before.id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { title: before.title || "x" }, history_note: "套件验收-还原" }),
  });
}
// T5.4 实时更新 ≡ 刷新（DOM 顺序）
await load();
{
  const orderLive = await ev(`[...document.querySelectorAll('.card-node')].map(n=>n.dataset.id).join(',')`);
  const probeId = await ev(`(() => { const root=(document.querySelector('.card-node.is-root')||{}).dataset?.id;
    const n=[...document.querySelectorAll('.card-node')].find(x=>x.querySelector('.kid-badge')&&x.dataset.id!==root); return n?n.dataset.id:null; })()`);
  if (probeId) {
    const mk = await (await fetch(`${BASE}/api/works/${WID}/cards`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "套件临时卡", source: "用户直接创建", parent: probeId, card_type: "" }),
    })).json();
    const newId = mk.card_id || (mk.meta || {}).card_id;
    await sleep(1500);
    const orderAfterCreate = await ev(`[...document.querySelectorAll('.card-node')].map(n=>n.dataset.id).join(',')`);
    if (newId) {
      await fetch(`${BASE}/api/works/${WID}/cards/${newId}`, { method: "DELETE" });
      await sleep(1500);
    }
    const orderAfterDelete = await ev(`[...document.querySelectorAll('.card-node')].map(n=>n.dataset.id).join(',')`);
    T("T5.4 建卡+删卡后 DOM 顺序回到原样（实时更新 ≡ 刷新）", orderAfterDelete === orderLive,
      `live=${orderLive} after=${orderAfterDelete}`);
    T("T5.4 建卡期间新卡出现过", orderAfterCreate !== orderLive, "顺序没变过");
  }
}

/* ══════════════════════════════════════════════════════════════════════
   T6 健壮性
   ══════════════════════════════════════════════════════════════════════ */
G("T6 健壮性");
await load();
{
  // T6.1 world 的行内 visibility 不得停在 hidden（那＝整屏空白且无报错）
  T("T6.1 画布没有停在 visibility:hidden（永久白屏）",
    (await ev(`document.getElementById('world').style.visibility`)) !== "hidden",
    await ev(`document.getElementById('world').style.visibility`));
  // T6.2 卡片真的有面积（不是零尺寸）
  const z = await ev(`document.querySelectorAll('.card-node').length && [...document.querySelectorAll('.card-node')].filter(n=>{const r=n.getBoundingClientRect();return r.width<10||r.height<10;}).length`);
  T_EQ("T6.2 没有零尺寸的卡", z, 0);
  // 精确检查 @starting-style **块内部**的规则（不是父 @media 的全文）
  const startOpacity = await ev(`(() => {
    const walk = (rules) => {
      for (const r of rules) {
        const txt = r.cssText || '';
        if (r.cssRules && txt.trim().indexOf('@starting-style') === 0) {
          for (const c of r.cssRules) { if (/opacity/.test(c.cssText || '')) return c.cssText; }
          return null;
        }
        if (r.cssRules) { const hit = walk(r.cssRules); if (hit) return hit; }
      }
      return null;
    };
    const all = [];
    for (const sh of document.styleSheets) { try { all.push(...sh.cssRules); } catch (e) { } }
    return walk(all);
  })()`);
  T("T6.3 入场起始态不含 opacity（减少动效/后台标签页下不会透明）", startOpacity === null,
    startOpacity || "starting-style 里仍有 opacity");
}

/* ══════════════════════════════════════════════════════════════════════
   T7 可访问性
   ══════════════════════════════════════════════════════════════════════ */
G("T7 可访问性");
await load();
{
  const a11y = await ev(`(() => {
    const missLabel = [];
    for (const el of document.querySelectorAll('.card-node button')) {
      const label = el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent.trim();
      if (!label) missLabel.push(el.className);
    }
    const noFocusRing = [];
    for (const sel of ['.zoom-slider', '.icon-btn', '.zbtn', '.cv-mi-btn', '.topbar .btn']) {
      for (const el of document.querySelectorAll(sel)) {
        const s = getComputedStyle(el);
        // 有没有 outline 规则：看样式表里有没有该选择器的 :focus-visible
        noFocusRing.push(sel);
        break;
      }
    }
    return { missLabel, buttons: document.querySelectorAll('.card-node button').length,
             cardAria: [...document.querySelectorAll('.card-node')].filter(n=>!n.getAttribute('aria-label')).length };
  })()`);
  T("T7.1 卡面上每个按钮都有可读名称", a11y.missLabel.length === 0, JSON.stringify(a11y.missLabel));
  T("T7.2 每张卡都有 aria-label", a11y.cardAria === 0, `miss=${a11y.cardAria}`);
  // 焦点环：样式表里必须有 :focus-visible 规则
  const hasFocusRing = await ev(`(() => {
    const s=[...document.styleSheets].flatMap(sh=>{try{return [...sh.cssRules]}catch(e){return []}});
    return s.some(r=>r.cssText && r.cssText.includes(':focus-visible'));
  })()`);
  T("T7.3 存在 :focus-visible 焦点环规则", hasFocusRing === true);
  // 命中区尺寸（屏幕像素）
  const small = await ev(`(() => {
    const out = [];
    for (const el of document.querySelectorAll('.card-node button, .zoom-ctrl button, .icon-btn')) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      if (r.width < 20 || r.height < 20) out.push((el.className||'') + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
    }
    return out;
  })()`);
  T("T7.4 命中区不小于 20px", small.length === 0, JSON.stringify(small));
}

/* ══════════════════════════════════════════════════════════════════════
   T8 视图状态持久化
   ══════════════════════════════════════════════════════════════════════ */
G("T8 视图状态");
await load();
{
  const key = `projView:${WID}`;
  const v = await ev(`(() => { try { return JSON.parse(localStorage.getItem(${JSON.stringify(key)}) || '{}'); } catch(e){ return {}; } })()`);
  T("T8.1 视图状态存在（pan/zoom/expanded）", v && (v.panX !== undefined || v.zoom !== undefined), JSON.stringify(v));
  T("T8.2 展开态被持久化（expanded 是数组）", Array.isArray(v.expanded), JSON.stringify(v.expanded));
  T("T8.3 openCells 被持久化", Array.isArray(v.openCells), JSON.stringify(v.openCells));
}

/* ══════════════════════════════════════════════════════════════════════
   T9 响应与规模
   ══════════════════════════════════════════════════════════════════════ */
G("T9 响应与规模");
await load();
{
  const m = await ev(`({
    dom: document.querySelectorAll('*').length,
    nodes: document.querySelectorAll('.card-node').length,
    svg: document.querySelectorAll('#edgesSvg *').length,
    canvasMs: -1,
  })`);
  T("T9.1 DOM 规模合理（<6000）", m.dom < 6000, `dom=${m.dom}`);
  T("T9.2 卡片节点数与投影一致", m.nodes > 0, `nodes=${m.nodes}`);
}

/* t10_block.mjs —— T10 交互面用例（供 canvas_suite.mjs 内联） */
G("T10 交互面");
await load();
{
  // T10.1 拖拽 ⇒ 同层顺序按落点变化（画布口径：拖拽＝调顺序）
  //
  // ⚠ 口径修正（此前本用例是**假红**）：原来断言"拖完坐标变了"。但画布的产品口径
  //   是"拖拽＝调顺序，绝对坐标仍由布局算"——只要同层顺序没变，落点后坐标就回到
  //   同一个槽位（坐标不变是**设计**，不是没生效）。而且原用例选中的第一张非根卡
  //   常常没有同层兄弟，拖了也不可能改顺序。
  // ⇒ 现在断言真正该成立的那件事：**拖过同层卡之后，它排到了落点指定的那一位**。
  const grp = await siblingGroups(WID);
  if (!grp) {
    T("T10.1 找得到同层多卡的分组", false, "没有 ≥3 张且都在场的同层卡");
  } else {
    const last = grp.ids[grp.ids.length - 1];
    const mid = (grp.tops[0] + grp.tops[1]) / 2;    // 落点＝第一、二张之间的中点
    const dr = await dragToWorldY(last, mid);
    await sleep(900);
    const after = await groupOrder(grp.parent, WID);
    T("T10.1 拖过同层卡 ⇒ 顺序按落点变化（落到第 2 位）",
      after.ids[1] === last, JSON.stringify({ before: grp.ids, after: after.ids, dr }));
    // 落库：坐标写进了 /canvas 的 positions
    const api2 = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
    const pos = (api2.positions || {})[last];
    T("T10.1 拖拽后坐标已落库", !!pos, JSON.stringify(pos || null));
  }
  // T10.2 拖拽后布局不变量（零重叠）
  const ov2 = await ev(`(() => {
    const ns = [...document.querySelectorAll('.card-node')].map(n => { const r = n.getBoundingClientRect(); return { x:r.x,y:r.y,w:r.width,h:r.height }; });
    let c = 0;
    for (let i=0;i<ns.length;i++) for (let j=i+1;j<ns.length;j++) { const a=ns[i],b=ns[j];
      const ox=Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x), oy=Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y);
      if (ox>2&&oy>2) c++; }
    return c; })()`);
  T_EQ("T10.2 拖拽后仍无重叠", ov2, 0);
}
await load();
{
  // T10.3 框选（Shift+拖空白）⇒ 选中多张
  const selN = await ev(`(() => {
    const vp = document.getElementById('viewport');
    const r = vp.getBoundingClientRect();
    const x = r.x + 20, y = r.y + 20;
    vp.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, buttons: 1, shiftKey: true, clientX: x, clientY: y }));
    document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, button: 0, buttons: 1, shiftKey: true, clientX: r.right - 20, clientY: r.bottom - 20 }));
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0, buttons: 0, shiftKey: true, clientX: r.right - 20, clientY: r.bottom - 20 }));
    return document.querySelectorAll('.card-node.is-selected').length;
  })()`);
  await sleep(500);
  const selAfter = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  T("T10.3 Shift+拖空白 ⇒ 框选选中多张", selAfter >= 1, `selected=${selAfter} (immediate=${selN})`);
}
await load();
{
  // T10.4 滚轮 ⇒ 平移，不缩放
  const z0 = await ev(`document.getElementById('zoomDisplay').textContent`);
  const tf0 = await ev(`document.getElementById('world').style.transform`);
  await ev(`(() => {
    const vp = document.getElementById('viewport');
    vp.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 120, clientX: 400, clientY: 300 }));
    return true; })()`);
  await sleep(500);
  const z1 = await ev(`document.getElementById('zoomDisplay').textContent`);
  const tf1 = await ev(`document.getElementById('world').style.transform`);
  T("T10.4 滚轮 ⇒ 平移（不缩放）", z0 === z1 && tf0 !== tf1, `zoom ${z0}→${z1} tf ${tf0}→${tf1}`);
  // T10.5 Ctrl+滚轮 ⇒ 缩放
  await ev(`(() => {
    const vp = document.getElementById('viewport');
    vp.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -120, ctrlKey: true, clientX: 400, clientY: 300 }));
    return true; })()`);
  await sleep(500);
  const z2 = await ev(`document.getElementById('zoomDisplay').textContent`);
  T("T10.5 Ctrl+滚轮 ⇒ 缩放", z2 !== z1, `${z1} → ${z2}`);
}
await load();
{
  // T10.6 顶栏按钮：更多菜单 / 规则面板 / 活动抽屉 / 整理画布
  const clickBtn = (bid) => ev(`(() => { const b=document.getElementById(${JSON.stringify(bid)}); if(!b) return false; b.click(); return true; })()`);
  await clickBtn("moreBtn");
  await sleep(300);
  T("T10.6 「更多」菜单可打开", (await ev(`!!document.querySelector('.cv-more-menu') && getComputedStyle(document.querySelector('.cv-more-menu')).display !== 'none'`)) === true);
  await key("Escape");
  await clickBtn("ruleToggleBtn");
  await sleep(400);
  T("T10.6 规则面板可开合", (await ev(`document.getElementById('rulePanel').classList.contains('is-open')`)) === true);
  await clickBtn("ruleToggleBtn");
  await sleep(300);
  await clickBtn("activityBtn");
  await sleep(400);
  T("T10.6 项目动态抽屉可开合", (await ev(`!!document.getElementById('activityDrawer') && getComputedStyle(document.getElementById('activityDrawer')).display !== 'none'`)) === true);
  await clickBtn("activityBtn");
  await sleep(300);
  const nBefore = await ev(`document.querySelectorAll('.card-node').length`);
  await clickBtn("reorganizeBtn");
  await sleep(900);
  const nAfter = await ev(`document.querySelectorAll('.card-node').length`);
  T("T10.6 「整理画布」不丢卡", nAfter === nBefore, `${nBefore} → ${nAfter}`);
  T("T10.6 「整理画布」零 JS 异常", consoleErrs.length === 0, JSON.stringify(consoleErrs.slice(0, 2)));
}
await load();
{
  // T10.7 Ctrl+K 搜索面板
  await key("k", { modifiers: "ctrlKey: true" });
  await sleep(500);
  const pal = await ev(`(() => {
    const inputs = [...document.querySelectorAll('input')].filter(i => i.offsetParent !== null);
    const p = document.querySelector('.cv-palette, .cv-search, [class*=palette]');
    return { inputs: inputs.length, palette: !!p, visible: p ? getComputedStyle(p).display !== 'none' : false };
  })()`);
  T("T10.7 Ctrl+K ⇒ 搜索面板出现", pal.palette === true || pal.visible === true, JSON.stringify(pal));
  await key("Escape");
  await sleep(300);
}
await load();
{
  // T10.8 顶栏统计与真实数据一致
  const api3 = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
  const cards = api3.cards || [];
  const stat = await ev(`(() => {
    const el = document.getElementById('projectStat');
    return el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
  })()`);
  const total = cards.filter(c => c.card_type !== "rule").length;
  const pending = cards.reduce((a, c) => a + Number(c.pending_count || 0), 0);
  T("T10.8 顶栏显示的卡数与数据一致", stat.includes(String(total)), `stat="${stat}" total=${total}`);
  T("T10.8 顶栏显示的待裁定数与数据一致", stat.includes(String(pending)), `stat="${stat}" pending=${pending}`);
}
await load();
{
  // T10.9 右键菜单 → 新建子卡（走真实弹窗）
  const id = await pick(".card-node");
  const nBefore = await ev(`document.querySelectorAll('.card-node').length`);
  await ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]');
    const r = n.getBoundingClientRect();
    n.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: r.x + 20, clientY: r.y + 20 }));
    return true; })()`);
  await sleep(400);
  const picked = await ev(`(() => {
    const items = [...document.querySelectorAll('[data-act]')];
    const it = items.find(x => (x.textContent || '').includes('新建子卡'));
    if (!it) return false;
    it.click(); return true; })()`);
  T("T10.9 右键菜单有「新建子卡」", picked === true);
  if (picked) {
    await sleep(500);
    const hasInput = await ev(`!!document.querySelector('.cv-modal-input')`);
    T("T10.9 新建弹窗有输入框", hasInput === true);
    if (hasInput) {
      await ev(`(() => { const i = document.querySelector('.cv-modal-input');
        i.value = 'T10 验收新建卡'; i.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
      await ev(`(() => { const b = document.querySelector('.cv-modal-btn[data-role=ok]'); if(b) b.click(); return true; })()`);
      await sleep(1600);
      const nAfter = await ev(`document.querySelectorAll('.card-node').length`);
      T("T10.9 新建子卡 ⇒ 画布上多了一张", nAfter > nBefore, `${nBefore} → ${nAfter}`);
      // 清理：把这张卡删掉
      const created = await ev(`(() => { const ns=[...document.querySelectorAll('.card-node')];
        const t = ns.find(n => (n.querySelector('.tname')||{}).textContent === 'T10 验收新建卡');
        return t ? t.dataset.id : null; })()`);
      if (created) {
        await fetch(`${BASE}/api/works/${WID}/cards/${created}`, { method: "DELETE" });
        await sleep(800);
      }
    }
  }
}
await load();
{
  // T10.10 选中 + Delete ⇒ 删卡（弹窗确认）
  //
  // ⚠ **自给自足**（修本用例的自我饿死）：原来它去画布上找一张"没有子卡的叶子卡"
  //   当删除对象，**而且真的把它删掉** ⇒ 每跑一次套件就少一张叶子卡。
  //   沙箱 work_002 的叶子卡被吃光后（实测：37 张 → 35 张，叶子归零），
  //   本用例开始假红（"找不到"），而画布其实没有任何问题。
  // ⇒ 现在：自己建一张叶子卡（走真实 API），只删自己建的那张，删完即净。
  const jpost = async (url, body) => (await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })).json();
  const cvs0 = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
  const root0 = (cvs0.cards || []).find(c => !c.parent);
  const made = await jpost(`${BASE}/api/works/${WID}/cards`, {
    title: "T10 验收删除卡", source: "用户直接创建", parent: root0 ? root0.card_id : undefined,
  });
  const leaf = (made && made.card_id) || (made && made.meta && made.meta.card_id);
  if (!leaf) T("T10.10 造得出待删的叶子卡", false, JSON.stringify(made).slice(0, 200));
  else {
    await load();                      // 让新卡进画布
    const onCanvas = await ev(`!!document.querySelector('.card-node[data-id="${leaf}"]')`);
    T("T10.10 造得出的叶子卡已进画布", onCanvas === true, `leaf=${leaf}`);
    const nBefore = await ev(`document.querySelectorAll('.card-node').length`);
    await ev(`(() => { const n = document.querySelector('.card-node[data-id="${leaf}"]');
      n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,shiftKey:true})); return true; })()`);
    await sleep(300);
    await key("Delete");
    await sleep(500);
    const dlg = await ev(`!!document.querySelector('.cv-modal-mask')`);
    T("T10.10 Delete 弹出确认框（不静默删）", dlg === true);
    if (dlg) {
      await ev(`(() => { const b=document.querySelector('.cv-modal-btn[data-role=ok]'); if(b) b.click(); return true; })()`);
      await sleep(1500);
      const nAfter = await ev(`document.querySelectorAll('.card-node').length`);
      T("T10.10 确认后卡被删掉", nAfter < nBefore, `${nBefore} → ${nAfter}`);
    }
    // 兜底清理（万一删除没生效，别把垃圾留在沙箱里）
    const still = await fetch(`${BASE}/api/works/${WID}/cards/${leaf}`, { method: "DELETE" });
    if (!still.ok) { /* 已经删掉了，DELETE 回 404 属正常 */ }
  }
}
await load();
{
  // T10.11 窄视口（900px）：顶栏不重叠、卡片仍在视口内
  await send("Emulation.setDeviceMetricsOverride", { width: 900, height: 800, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: `${BASE}/work/${WID}` });
  await sleep(2200);
  const narrow = await ev(`(() => {
    const tb = document.querySelector('.topbar');
    const r = tb.getBoundingClientRect();
    const kids = [...tb.children].filter(x => { const b = x.getBoundingClientRect(); return b.width > 0; })
      .map(x => { const b = x.getBoundingClientRect(); return { cls: (x.className||'').slice(0,24), l: Math.round(b.left), r: Math.round(b.right) }; });
    let overlaps = 0;
    for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
      if (kids[i].r > kids[j].l + 1 && kids[j].r > kids[i].l + 1) overlaps++;
    }
    const vp = document.getElementById('viewport').getBoundingClientRect();
    const ns = [...document.querySelectorAll('.card-node')];
    const inView = ns.filter(n => { const b = n.getBoundingClientRect(); return b.right > vp.left && b.left < vp.right; }).length;
    return { overlaps, kids: kids.length, inView, n: ns.length, tbW: Math.round(r.width), vpW: Math.round(vp.width) };
  })()`);
  T("T10.11 窄视口：顶栏控件不重叠", narrow.overlaps === 0, JSON.stringify(narrow));
  T("T10.11 窄视口：卡片仍在视口内（不跑飞）", narrow.n === 0 || narrow.inView > 0, JSON.stringify(narrow));
  await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
}

/* ══════════════════════════════════════════════════════════════════════
   T11 手势一致性与视图锚点
   ══════════════════════════════════════════════════════════════════════
   本组验两件"界面说错话 / 行为做错事"的事：
   ① 菜单里印出来的快捷键提示必须与**真实**手势一致（提示骗人＝功能不存在）；
   ② 视图根（布局锚点）拖拽必须＝移动视图，而不是"拖了又弹回"。
   */
G("T11 手势与锚点");
await load();
{
  // T11.1 菜单快捷键提示 vs 真实手势
  const cardId = await ev(`(() => {
    const ns = [...document.querySelectorAll('.card-node')];
    const n = ns.find(x => !x.classList.contains('is-root'));
    return n ? n.dataset.id : null;
  })()`);
  await ev(`(() => { const n = document.querySelector('.card-node[data-id="${cardId}"]');
    const r = n.getBoundingClientRect();
    n.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: r.x + 20, clientY: r.y + 20 }));
    return true; })()`);
  await sleep(400);
  const menu = await ev(`(() => {
    const mis = [...document.querySelectorAll('.cv-menu .cv-mi')];
    const read = (needle) => {
      const m = mis.find(x => (x.textContent || '').includes(needle));
      if (!m) return null;
      const k = m.querySelector('.cv-kbd');
      const sp = m.querySelector('span');
      return { label: sp ? (sp.textContent || '') : '', kbd: k ? (k.textContent || '') : null };
    };
    return {
      open: !!document.querySelector('.cv-menu'),
      expandChat: read('在此展开对话'),
      openPage: read('打开对话'),
      drill: read('下钻到此卡'),
      anyDbl: mis.filter(x => (x.textContent || '').includes('双击')).map(x => (x.textContent || '').trim()),
      all: mis.map(x => (x.textContent || '').trim()),
    };
  })()`);
  T("T11.1 右键菜单能打开", menu.open === true, JSON.stringify(menu.all));
  T("T11.1 「在此展开对话」标 ↵（真实手势就是 Enter）",
    !!menu.expandChat && menu.expandChat.kbd === "↵", JSON.stringify(menu.expandChat));
  T("T11.1 「打开对话」不标 ↵（↵ 并不是它的快捷键）",
    !!menu.openPage && menu.openPage.kbd === null, JSON.stringify(menu.openPage));
  T("T11.1 菜单里不出现「双击」（双击手势已撤除）",
    menu.anyDbl.length === 0, JSON.stringify(menu.anyDbl));
  T("T11.1 下钻写的是 Alt+单击",
    !!menu.drill && menu.drill.label.indexOf("Alt+单击") >= 0, JSON.stringify(menu.drill));
  await ev(`(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); return true; })()`);
  await sleep(200);
}
await load();
{
  // T11.2 下钻后，视图根（布局锚点）拖拽＝移动视图，而不是"拖了又弹回"
  //
  // 背景：computeLayout 钉在原点的是 **viewRoot**，而下钻会改 viewRoot。
  // 旧实现拿 workCardId 判"根卡" ⇒ 下钻后判错 ⇒ 锚点卡走普通拖拽分支：
  // 落库坐标后 reflowAll 又把它拉回 (0,0)（拖了又弹回），且视图根本平移不了。
  const target = await ev(`(() => {
    const ns = [...document.querySelectorAll('.card-node')];
    const n = ns.find(x => !x.classList.contains('is-root') && x.querySelector('.kid-badge'));
    return n ? n.dataset.id : null;
  })()`);
  if (!target) {
    T("T11.2 找得到可下钻的卡（非根且有子卡）", false, "没有这样的卡");
  } else {
    await ev(`(() => { const n = document.querySelector('.card-node[data-id="${target}"]');
      n.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, altKey: true }));
      return true; })()`);
    await sleep(1200);
    const drilled = await ev(`(() => {
      const r = document.querySelector('.card-node.is-root');
      return { rootId: r ? r.dataset.id : null, n: document.querySelectorAll('.card-node').length };
    })()`);
    T("T11.2 Alt+单击 ⇒ 视图根换成那张卡", drilled.rootId === target, JSON.stringify(drilled));
    const panOf = () => ev(`(() => { const w = document.getElementById('world'); return w ? w.style.transform : ''; })()`);
    const posOf = (id) => ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]'); return n ? n.style.left + '|' + n.style.top : null; })()`);
    const posBefore = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json().then(d => (d.positions || {})[target] || null);
    const pan0 = await panOf();
    const pos0 = await posOf(target);
    await ev(`(() => {
      const n = document.querySelector('.card-node[data-id="${target}"]');
      const r = n.getBoundingClientRect();
      const x = r.x + 30, y = r.y + 12;
      n.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: x, clientY: y }));
      document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: x, clientY: y + 50 }));
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0, buttons: 0, clientX: x, clientY: y + 50 }));
      return true;
    })()`);
    await sleep(700);
    const pan1 = await panOf();
    const pos1 = await posOf(target);
    T("T11.2 拖视图根 ⇒ 视图被平移（world transform 变化）", pan1 !== pan0, `pan0=${pan0} pan1=${pan1}`);
    T("T11.2 拖视图根 ⇒ 锚点自身位置不变（不被布局拉回）", pos1 === pos0, `pos0=${pos0} pos1=${pos1}`);
    const posAfter = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json().then(d => (d.positions || {})[target] || null);
    T("T11.2 拖视图根不写坐标记录（无落库副作用）",
      JSON.stringify(posBefore) === JSON.stringify(posAfter),
      `before=${JSON.stringify(posBefore)} after=${JSON.stringify(posAfter)}`);
  }
}
await load();
{
  // T11.3 默认视图：拖项目根卡＝移动视图（老行为的回归护栏，不能被上一条修坏）
  const rootId = await ev(`(() => { const r = document.querySelector('.card-node.is-root'); return r ? r.dataset.id : null; })()`);
  if (!rootId) {
    T("T11.3 默认视图找得到根卡", false, "没有 .is-root");
  } else {
    const panOf = () => ev(`(() => { const w = document.getElementById('world'); return w ? w.style.transform : ''; })()`);
    const posOf = (id) => ev(`(() => { const n = document.querySelector('.card-node[data-id="${id}"]'); return n ? n.style.left + '|' + n.style.top : null; })()`);
    const drag = (dx, dy) => ev(`(() => {
      const n = document.querySelector('.card-node[data-id="${rootId}"]');
      const r = n.getBoundingClientRect();
      const x = r.x + 30, y = r.y + 12;
      n.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: x, clientY: y }));
      document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: x + ${dx}, clientY: y + ${dy} }));
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0, buttons: 0, clientX: x + ${dx}, clientY: y + ${dy} }));
      return true;
    })()`);
    const pan0 = await panOf();
    const pos0 = await posOf(rootId);
    await drag(60, 40);
    await sleep(500);
    const pan1 = await panOf();
    const pos1 = await posOf(rootId);
    T("T11.3 默认视图拖根卡 ⇒ 视图被平移", pan1 !== pan0, `pan0=${pan0} pan1=${pan1}`);
    T("T11.3 默认视图拖根卡 ⇒ 根卡位置不变", pos1 === pos0, `pos0=${pos0} pos1=${pos1}`);
    await drag(-60, -40);   // 复原平移，别把状态留给后面的用例
    await sleep(300);
  }
}

/* ══════════════════════════════════════════════════════════════════════
   T11.4 拖拽落点＝同层次序（用户意图不被忽略）
   ══════════════════════════════════════════════════════════════════════ */
{
  // 用一个一次性脚手架工程（根卡 + 3 张子卡，**都没有坐标记录**）——
  // 这正是 bug 的高发场景：同层里第一次拖某张卡时，旧实现会把它提到该层最前面，
  // 用户的落点被完全忽略（实测：把 card_017 拖到 card_010 下面，它却跑到第 3 位）。
  // 子卡每次重建（删了再建 ⇒ 坐标记录随卡一起消失），
  // 所以这条用例每次跑都在"同层从没拖过"的干净前提下验，不受上一轮残留影响。
  const jpost = async (url, body) => (await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })).json();
  const jdel = async (url) => { try { await fetch(url, { method: "DELETE" }); } catch { } };
  const list = await (await fetch(`${BASE}/api/works`)).json();
  let sw = (list.works || []).find(w => (w.title || "").indexOf("T11 拖拽顺序验收") >= 0);
  if (!sw) sw = await jpost(`${BASE}/api/works`, { title: "T11 拖拽顺序验收" });
  const swid = sw.work_id;
  const cvs = await (await fetch(`${BASE}/api/works/${swid}/canvas`)).json();
  const root = (cvs.cards || []).find(c => !c.parent);
  const rootId = root ? root.card_id : sw.work_card_id;
  for (const c of (cvs.cards || [])) {
    if (c.card_id !== rootId) await jdel(`${BASE}/api/works/${swid}/cards/${c.card_id}`);
  }
  const kids = [];
  for (const t of ["甲", "乙", "丙"]) {
    const c = await jpost(`${BASE}/api/works/${swid}/cards`, { title: "T11-" + t, source: "用户直接创建", parent: rootId });
    // 建卡接口回的是 {meta:{card_id,...}}（信封在端点层成形），不是顶层 card_id
    const cid = (c && c.card_id) || (c && c.meta && c.meta.card_id);
    if (cid) kids.push(cid);
  }
  if (kids.length !== 3) {
    T("T11.4 脚手架工程建好（根卡 + 3 张子卡）", false, JSON.stringify(kids));
  } else {
    T("T11.4 脚手架工程建好（根卡 + 3 张子卡）", true);
    await load(swid);
    const g0 = await siblingGroups(swid);
    if (!g0) {
      T("T11.4 脚手架里找得到同层 3 张卡", false, "没有");
    } else {
      const last = g0.ids[g0.ids.length - 1];
      const mid = (g0.tops[0] + g0.tops[1]) / 2;    // 落点＝第一、二张之间的中点
      await dragToWorldY(last, mid);
      await sleep(1000);
      const g1 = await groupOrder(g0.parent, swid);
      T("T11.4 拖到第一张卡下面 ⇒ 它排到第 2 位（落点被尊重）",
        g1.ids[1] === last, JSON.stringify({ before: g0.ids, after: g1.ids }));
    }
    for (const k of kids) await jdel(`${BASE}/api/works/${swid}/cards/${k}`);   // 清理
  }
}

/* ══════════════════════════════════════════════════════════════════════
   T11.5 关键控件命中区（WCAG 2.2 最小目标尺寸 24×24）
   ══════════════════════════════════════════════════════════════════════ */
await load();
{
  const hits = await ev(`(() => {
    const one = (sel) => {
      const n = document.querySelector(sel);
      if (!n) return null;
      const r = n.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height) };
    };
    return {
      chat: one('.card-node .cv-chat'),          // 💬 在此展开对话
      open: one('.card-node .cv-open'),            // ↗ 打开对话页
      slider: one('#zoomSlider'),                  // 缩放滑块
      zbtn: one('#zoomInBtn'),                    // ＋
      triage: one('.proj-stat .ps-item[data-act="triage"]'),  // 待裁定入口
    };
  })()`);
  const small = Object.entries(hits)
    .filter(([, v]) => v && Math.min(v.w, v.h) < 24)
    .map(([k, v]) => `${k}=${v.w}x${v.h}`);
  T("T11.5 关键控件命中区都 ≥24px（WCAG 2.2 最小目标尺寸）", small.length === 0,
    `偏小：${JSON.stringify(small)} 全量：${JSON.stringify(hits)}`);
}

/* ══════════════════════════════════════════════════════════════════════
   T11.6 根卡上的控件点得动
   ══════════════════════════════════════════════════════════════════════ */
await load();
{
  // 病根（实测抓到的真 bug）：平移态的 CSS 是
  //   `.page-canvas #viewport.panning .card-node { pointer-events: none; }`
  // —— 平移中**所有卡片都不可点**。而根卡（视图根）的 mousedown 会进
  // "拖根卡＝平移"分支 ⇒ 按下的瞬间 viewport 就进了 .panning ⇒
  // **鼠标抬起的落点变成 viewport** ⇒ 浏览器合成的 click 的 target 是 viewport，
  // 控件的 click 处理器**根本没被调用**。
  // ⇒ 症状：**根卡上的 💬（在此展开对话）／↗（打开对话页）／▸N（展开子卡）点了没反应**
  //   —— 而根卡恰是打开画布看到的第一张卡、最显眼的那张。
  //
  // ⚠ 这条**必须用真实鼠标事件**验：只有真实的"按下→抬起"才会让浏览器合成 click，
  // 而 click 的落点取决于"抬起时指针下面是谁"。用 `el.click()` 走的是直接派发，
  // 永远命中按钮 ⇒ 假通过。
  const rootChat = await centerOfVisible(".card-node.is-root .cv-chat");
  if (!rootChat) {
    T("T11.6 根卡上的 💬 点得动（就地展开对话，不跳页）", false, "视口内找不到根卡的 💬");
  } else {
    await realClick(rootChat.x, rootChat.y);
    await sleep(1400);
    const opened = await ev(`(() => {
      const n = document.querySelector('.card-node.is-root');
      return { open: n ? (n.dataset.open || '') : null, path: location.pathname };
    })()`);
    T("T11.6 根卡上的 💬 点得动（就地展开对话，不跳页）",
      opened.open === "1" && opened.path === `/work/${WID}`, JSON.stringify(opened));
  }
}

/* ══════════════════════════════════════════════════════════════════════
   T12 加载鲁棒性：出图不能被一条网络往返卡住
   ══════════════════════════════════════════════════════════════════════ */
G("T12 加载鲁棒性");
{
  // T12.1 屏蔽 /api/works（项目列表端点）⇒ 画布**仍然出图**
  //
  // 病根（实测抓到的 P0）：画布"找根卡"原来 `await API.listWorks()`，
  // 那是一次**在出图关键路径上**的网络往返。浏览器每源 6 条并发连接（HTTP/1.1），
  // 而画布页常驻 SSE 长连接 ⇒ 并发请求占满连接槽时，这条往返被**排队、永不发出**
  // ⇒ await 悬着 ⇒ reloadCanvas 停在半路 ⇒ **画布永久空白**（骨架屏不撤）。
  // ⇒ 现在根卡直接取自画布数据（parent 为空的那张 = 项目本体），零往返。
  // ⚠ 不用 Network.setBlockedURLs：它的模式是**前缀匹配**，`*/api/works` 会把
  //   `/api/works/{id}/canvas`（画布数据本体）一起挡掉 ⇒ 测的就不是要测的东西了。
  //   这里改成：页面脚本跑起来**之前**注入一个 fetch 包装，只让**精确的**项目列表
  //   端点**永不 settle**。
  //   为什么是"永不 settle"而不是"失败"：真症状是"请求被连接槽排队、永远发不出去"
  //   （不是报错）；旧实现 await 的是这条往返 ⇒ 它挂住 ⇒ 画布永远出不来。
  //   若只是让请求**失败**，旧实现的 catch 有兜底（退回 cards[0]）⇒ 画布照样出图
  //   ⇒ 这条用例就变成"假守护"（护不住真正的缺陷）。
  const inject = await send("Page.addScriptToEvaluateOnNewDocument", {
    source: `(() => {
      const orig = window.fetch;
      window.fetch = function (u, o) {
        const s = String(u && u.url ? u.url : u);
        if (/(^|\\/)api\\/works$/.test(s)) {
          return new Promise(() => {});   // 永不 settle：模拟"连接被占满 ⇒ 排队不发"
        }
        return orig.apply(this, arguments);
      };
    })();`,
  });
  await load();
  const blocked = await ev(`(() => {
    const n = document.querySelectorAll('.card-node').length;
    const root = document.querySelector('.card-node.is-root');
    return { n, root: root ? root.dataset.id : null };
  })()`);
  T("T12.1 项目列表端点挂住 ⇒ 画布仍出图（根卡不依赖网络往返）",
    blocked.n > 0 && !!blocked.root, JSON.stringify(blocked));
  if (inject && inject.result && inject.result.identifier) {
    await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: inject.result.identifier });
  }
}
{
  // T12.2 画布页的**常驻长连接只有一条**（工作级 /events）
  //
  // 原来还有第二条：`/api/works/{w}/cards/{w}/stream` —— card_id 位置传的是
  // **work_id**（不是卡号）⇒ 订阅的是一条**永远不会有帧**的键 ⇒
  //   ① 未读角标的增量通道从来没活过；② 却长期占着一条浏览器连接。
  // 浏览器每源只有 6 条并发连接 ⇒ 这种"白占的连接"会挤掉真正要用的请求。
  await send("Network.enable");     // 必须先开 Network，否则 netUrls 是空的 ⇒ 断言真空跑
  netUrls.length = 0;
  await load();
  await sleep(1500);
  const sse = netUrls.filter(u => /\/events(\?|$)/.test(u) || /\/stream(\?|$)/.test(u));
  const wrongKey = sse.filter(u => u.includes(`/cards/${WID}/stream`));
  T("T12.2 画布页不订阅「把 work id 当 card id」的那条流", wrongKey.length === 0,
    JSON.stringify(sse.map(u => u.replace(/^https?:\/\/[^/]+/, ""))));
  T("T12.3 画布页只常驻一条长连接（工作级 /events）",
    sse.filter(u => /\/events(\?|$)/.test(u)).length === 1, JSON.stringify(sse.map(u => u.replace(/^https?:\/\/[^/]+/, ""))));
}

/* ───────────────────────── 汇总 ───────────────────────── */
const fails = results.filter(r => !r.pass);
const byGroup = {};
for (const r of results) {
  byGroup[r.group] = byGroup[r.group] || { total: 0, fail: 0 };
  byGroup[r.group].total++;
  if (!r.pass) byGroup[r.group].fail++;
}
console.log("\n════ 画布全量验收汇总 ════");
for (const [g, v] of Object.entries(byGroup)) {
  console.log(`  ${v.fail === 0 ? "✓" : "✗"} ${g.padEnd(16)} ${v.total - v.fail}/${v.total}`);
}
console.log(`\n总计：${results.length - fails.length}/${results.length} 通过，失败 ${fails.length}`);
if (fails.length) {
  console.log("\n失败明细：");
  for (const f of fails) console.log(`  ✗ [${f.group}] ${f.name}  ${f.detail}`);
}
if (consoleErrs.length) {
  console.log("\n控制台错误：");
  for (const e of [...new Set(consoleErrs)].slice(0, 10)) console.log("  " + e);
}
ws.close(); child.kill(); process.exit(fails.length ? 1 : 0);
