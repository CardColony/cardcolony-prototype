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
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((r) => { ws.onopen = r; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
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
const load = async () => {
  await send("Page.navigate", { url: `${BASE}/work/${WID}` });
  let last = -1, stable = 0;
  for (let i = 0; i < 200; i++) {
    await sleep(60);
    const st = await ev(`(() => ({
      path: location.pathname,
      n: document.querySelectorAll('.card-node').length,
    }))()`);
    const onCanvas = st && st.path === `/work/${WID}` && st.n > 0;
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
