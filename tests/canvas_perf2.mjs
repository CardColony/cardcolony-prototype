/* canvas_perf2.mjs —— 大画布交互帧正式探针（批三任务书口径）
 *
 * 与 canvas_perf.mjs（冒烟/规模核对）的区别：
 *   - CDP Input 真输入（mouseWheel/mousePressed/mouseMoved/mouseReleased）
 *   - 逐帧 rAF 间隔采样（首个有效输入→末帧视觉更新），P50/P95/最慢帧
 *   - PerformanceObserver 记 >50ms 长任务
 *   - 每轮起止变换/落点验证（手势未发生＝invalid 留痕，作废规则见施工卡§3）
 *   - 挂载卡/SVG 峰值随轮登记
 *
 * 用法：node tests/canvas_perf2.mjs <baseUrl> <workId> <tag> [w h]
 *   例：node tests/canvas_perf2.mjs http://127.0.0.1:5707 work_073 A
 * 输出：logs/canvas-perf-<tag>.json（单轮完整样本，配对由外层调度）
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const TAG = process.argv[4] || ("run" + Date.now());
const W = parseInt(process.argv[5], 10) || 1440;
const H = parseInt(process.argv[6], 10) || 900;
/* [impl11] 参数序为 <base> <wid> <tag> [w h]——把 w h 误传到 tag 位
   会得到 H=NaN ⇒ JSON null ⇒ setDeviceMetricsOverride 整包被拒、
   视口回落默认 ~756×~480（impl5-impl10 的 mounted=4 全由此而来）。 */
const CHROME = process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9600 + Math.floor(Math.random() * 200);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const T0 = Date.now();
const T = () => (Date.now() - T0) / 1000;
const log = (...a) => console.log(`[t=${T().toFixed(1)}]`, ...a);
fs.mkdirSync("logs", { recursive: true });

/* ---------- chrome + cdp ---------- */
const prof = path.join(os.tmpdir(), "cvp2_" + Date.now());
const cp = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu",
  "--no-sandbox", "--hide-scrollbars", "--window-size=" + W + "," + H,
  "--no-first-run", "--user-data-dir=" + prof, "about:blank",
], { stdio: "ignore" });
let tr = null;
for (let i = 0; i < 120 && !tr; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    if (r.ok) tr = await r.json();
  } catch { }
  if (!tr) await sleep(300);
}
if (!tr) { log("chrome never came up"); process.exit(2); }
let ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
const netLog = [];   /* [终轮] PATCH 取证：{method,url,postData,status} */
const _netPending = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  else if (m.method === "Network.requestWillBeSent") {
    const r = m.params && m.params.request;
    if (r && r.method === "PATCH")
      _netPending.set(m.params.requestId, { method: r.method, url: r.url, postData: r.postData || "" });
  } else if (m.method === "Network.responseReceived") {
    const p = _netPending.get(m.params.requestId);
    if (p) { p.status = m.params.response.status; netLog.push(p); _netPending.delete(m.params.requestId); }
  }
};
await new Promise((r) => { ws.onopen = r; });
const send = (m, p = {}) => new Promise((r) => {
  const i = ++id; pend.set(i, r);
  ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Emulation.setDeviceMetricsOverride",
  { width: W, height: H, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => {
  const r = await send("Runtime.evaluate",
    { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const J = async (x) => JSON.parse(await ev(`JSON.stringify(${x})`) || "null");
const chromeVer = (await (await fetch(
  `http://127.0.0.1:${PORT}/json/version`)).json()).Browser;

const mouse = (type, x, y, extra = {}) => send("Input.dispatchMouseEvent",
  { type, x, y, button: extra.button ?? "none", buttons: extra.buttons ?? 0,
    clickCount: extra.clickCount ?? 0, modifiers: extra.modifiers ?? 0,
    deltaX: extra.deltaX ?? 0, deltaY: extra.deltaY ?? 0 });

/* [impl7] 真实窗口尺寸收口：--window-size / setDeviceMetricsOverride 在新无头
   下都不保证生效（实测 innerW 停在默认 ~756）——经浏览器级 ws 走
   Target.getWindowForTarget + Browser.setWindowBounds 强制到 W×H。 */
try {
  /* [impl8] 已知：--window-size、Emulation、Browser.setWindowBounds 在新无头
     下都不保内尺寸。Target.createTarget 的 width/height 是唯一可靠入口——
     用它另起一个 W×H 页并切到它的 ws；原 about:blank 页关掉。 */
  const ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  const bws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((r) => { bws.onopen = r; });
  let bid = 0; const bpend = new Map();
  bws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && bpend.has(m.id)) { bpend.get(m.id)(m); bpend.delete(m.id); }
  };
  const bsend = (m, p = {}) => new Promise((r) => {
    const i = ++bid; bpend.set(i, r);
    bws.send(JSON.stringify({ id: i, method: m, params: p }));
  });
  const ct = await bsend("Target.createTarget",
    { url: `${BASE}/work/${WID}`, width: W + 16, height: H + 87, newWindow: true });
  const newId = ct.result && ct.result.targetId;
  log("createTarget =>", JSON.stringify(ct.result || ct.error));
  if (newId) {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const tr2 = list.find(t => t.id === newId && t.webSocketDebuggerUrl);
    if (tr2) {
      ws.close();
      ws = new WebSocket(tr2.webSocketDebuggerUrl);
      await new Promise((r) => { ws.onopen = r; });
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
        if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
        else if (m.method === "Network.requestWillBeSent") {
          const r = m.params && m.params.request;
          if (r && r.method === "PATCH")
            _netPending.set(m.params.requestId, { method: r.method, url: r.url, postData: r.postData || "" });
        } else if (m.method === "Network.responseReceived") {
          const p = _netPending.get(m.params.requestId);
          if (p) { p.status = m.params.response.status; netLog.push(p); _netPending.delete(m.params.requestId); }
        }
      };
      await send("Page.enable");
      await send("Runtime.enable");
      await send("Network.enable");
      await send("Emulation.setDeviceMetricsOverride",
        { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    }
    /* 关原空白页 */
    await fetch(`http://127.0.0.1:${PORT}/json/close/${tr.id}`);
  }
  bws.close();
} catch (e) { log("target-resize fallback:", e.message); }

/* ---------- 展开全集 + 固定相机 ---------- */
const data = await (await fetch(`${BASE}/api/works/${WID}/canvas`)).json();
const parents = new Set();
for (const c of data.cards) if (c.parent) parents.add(c.parent);
log(`work=${WID} cards=${data.cards.length} links=${data.links.length} expand=${parents.size}`);

await send("Page.navigate", { url: `${BASE}/work/${WID}` });
/* 等首导航真正落地（页面元素出现）再写 projView——慢机上 sleep(1200) 可能
   还没到 work 页，write 落进 about:blank 的 opaque origin，二次导航读不到
   ⇒ 展开丢失（impl4 实测：visible 停在 4）。 */
for (let i = 0; i < 120; i++) {
  const ok = await ev(`!!document.getElementById('viewport')`);
  if (ok) break;
  await sleep(100);
}
await ev(`localStorage.setItem("projView:${WID}", ${JSON.stringify(JSON.stringify({
  panX: 60, panY: 60, zoom: 1.0, expanded: [...parents], openCells: [],
}))})`);
await send("Page.navigate", { url: `${BASE}/work/${WID}` });
/* [impl6] --window-size 在新无头下不可靠（实测 innerW=756），仿真视口是
   唯一可信尺寸源；末次导航后重设并按 innerWidth 验证生效，否则坐标全错。 */
for (let i = 0; i < 40; i++) {
  await send("Emulation.setDeviceMetricsOverride",
    { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  const iw = Number(await ev(`innerWidth`));
  if (iw === W) break;
  if (i === 0 || i === 39) log(`emu retry#${i} iw=${iw} url=${await ev('location.href')}`);
  await sleep(150);
}
/* 等「逻辑展开到位 + 挂载数稳定」。
   [批三] 判据不能只数 DOM：虚拟化下存在「默认 4 张已挂、projView 展开
   尚未恢复」的平台期——基线全挂 DOM 没有这个平台，按 DOM 稳定会提前收口。
   先等 __cvMount().visible 到位（逻辑可见集），再等挂载数稳定。 */
const wantVisible = 1 + parents.size + data.cards.filter(c => !c.parent).length - 1;
for (let i = 0; i < 240; i++) {
  await sleep(50);
  const m = await J(`window.__cvMount ? window.__cvMount() : null`);
  const vis = m && m.visible >= 0 ? m.visible
    : Number(await ev(`document.querySelectorAll('.card-node').length`));
  if (vis >= data.cards.length) break;
}
let lastN = -1, stable = 0;
for (let i = 0; i < 240; i++) {
  await sleep(50);
  const n = Number(await ev(`document.querySelectorAll('.card-node').length`));
  if (n === lastN && n > 0) { stable += 50; if (stable >= 300) break; }
  else { stable = 0; lastN = n; }
}
const mountInfo0 = await J(`window.__cvMount ? window.__cvMount() : null`);
const scene0 = await J(`({
  innerW: innerWidth, innerH: innerHeight,
  vp: document.getElementById('viewport').getBoundingClientRect().toJSON(),
  body: document.body.className,
  panel: (document.getElementById('rulePanel')||{}).className,
  cardDetailOpen: !!(document.getElementById('cardDetail') && !document.getElementById('cardDetail').hidden),
  navOpen: document.querySelector('.nav-panel,.nav-drawer,#navPanel') ? true : false,
})`);
const mounted0 = Number(await ev(`document.querySelectorAll('.card-node').length`));
const svg0 = Number(await ev(`document.querySelectorAll('#edgesSvg *').length`));
const dom0 = Number(await ev(`document.querySelectorAll('*').length`));
log(`首屏稳定 mounted=${mounted0} svg=${svg0} dom=${dom0}` +
  (mountInfo0 ? ` visible=${mountInfo0.visible} edges=${mountInfo0.edgesMounted}/${mountInfo0.edgesCatalog} vw=${mountInfo0.vw} zoom=${mountInfo0.zoom}` : "") +
  (scene0 ? ` | vp=${Math.round(scene0.vp.x)},${Math.round(scene0.vp.y)},${Math.round(scene0.vp.width)}x${Math.round(scene0.vp.height)} innerW=${scene0.innerW} panel=${scene0.panel} detailOpen=${scene0.cardDetailOpen}` : ""));

/* ---------- 帧采样器 ---------- */
const Sampler = {
  start: () => ev(`(() => {
    window.__FR = { ts: [], lt: [], on: true };
    const loop = (t) => { if (window.__FR.on) { window.__FR.ts.push(t); requestAnimationFrame(loop); } };
    requestAnimationFrame(loop);
    try {
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__FR.lt.push(Math.round(e.duration));
      }).observe({ entryTypes: ["longtask"] });
    } catch (e) {}
    return true;
  })()`),
  stop: () => J(`(() => { window.__FR.on = false; return window.__FR; })()`),
};

const xform = () => ev(`(() => {
  const s = getComputedStyle(document.getElementById('world')).transform;
  return s;
})()`);
const counts = () => J(`({
  cards: document.querySelectorAll('.card-node').length,
  svg: document.querySelectorAll('#edgesSvg *').length,
})`);

/* ---------- 手势 ---------- */
const result = { tag: TAG, base: BASE, work: WID, chrome: chromeVer,
                 viewport: [W, H], cards: data.cards.length, links: data.links.length,
                 mounted0, svg0, dom0, scenes: [], invalid: [] };

async function scene(name, verify, run) {
  const before = await xform();
  await Sampler.start();
  const tS = T();
  const meta = await run();
  /* 手势尾部再留 ~8 帧收尾 */
  await sleep(140);
  const fr = (meta && meta.__fr) || (await Sampler.stop()) || { ts: [], lt: [] };
  if (meta) delete meta.__fr;   /* 采样数据归 intervals/longTasks，不进 meta */
  const after = await xform();
  const c = await counts();
  const gaps = (fr.ts || []).slice(1).map((t, i) => +(t - fr.ts[i]).toFixed(2))
    .filter((g) => g > 0 && g < 5000);
  const sorted = [...gaps].sort((a, b) => a - b);
  const q = (p) => sorted.length
    ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))] : null;
  const ok = verify ? verify(before, after, meta) : true;
  const rec = { name, durS: +(T() - tS).toFixed(2), frames: gaps.length,
    p50: q(0.5), p95: q(0.95), max: sorted[sorted.length - 1] || null,
    intervals: gaps,                    /* [终轮] 逐帧间隔原样入库（此前只存分位） */
    longTasks: fr.lt || [], mounted: c.cards, svg: c.svg,
    before, after, meta: meta || null, ok };
  result.scenes.push(rec);
  log(`${name}: frames=${gaps.length} p50=${q(0.5)} p95=${q(0.95)} max=${rec.max} lt=${(fr.lt || []).length} mounted=${c.cards} ${ok ? "OK" : "INVALID(" + (meta && meta.reason || "verify") + ")"}`);
  if (!ok) result.invalid.push({ scene: name, reason: (meta && meta.reason) || "end-state verify failed" });
  return rec;
}

const parseT = (s) => {
  const m = /matrix\(([^)]+)\)/.exec(s || "");
  if (!m) return null;
  const a = m[1].split(",").map(Number);
  return { zoom: a[0], panX: a[4], panY: a[5] };
};

/* S-zoom：中部锚 Ctrl+wheel——3进/3出往返 ×6 循环（deltaY=±20，每步 ×1.22，
   z 在 [1.0,1.83] 往返、每个事件都真变、不触 0.25/2.0 界）。
   [终轮修] 旧轨迹 36 次同向 deltaY=60，第 ~3 步就顶死 0.25 下限，
   之后大部分采样窗口里 zoom 根本没在变——等于在测静止画面。 */
await scene("zoom-center", (b, a, meta) =>
  meta && meta.zTrace && meta.zTrace.length >= 8
    && Math.max(...meta.zTrace) - Math.min(...meta.zTrace) > 0.3
    && meta.zTrace.every(z => z > 0.26 && z < 1.99),
async () => {
  const vr = await J(`document.getElementById('viewport').getBoundingClientRect().toJSON()`);
  const cx = Math.round(vr.x + vr.width / 2), cy = Math.round(vr.y + vr.height / 2);
  const zTrace = [];
  for (let i = 0; i < 36; i++) {
    /* (i/3)%2==0 进、==1 出：1.0→1.83→1.0→1.83… 单调段内每步都在变 */
    const dY = ((i / 3 | 0) % 2 === 0) ? -20 : 20;
    await mouse("mouseWheel", cx, cy,
      { modifiers: 2, deltaX: 0, deltaY: dY });
    if (i % 4 === 3) {
      const m = await J(`window.__cvMount ? __cvMount().zoom :
        (() => { const s = getComputedStyle(document.getElementById('world')).transform;
                 const mm = /matrix\\(([^,)]+)/.exec(s); return mm ? +mm[1] : null; })()`);
      if (m != null) zTrace.push(+m.toFixed(3));
    }
    await sleep(20);
  }
  /* 还原 100%：工具栏滑块是既有入口 */
  await ev(`(() => { const s = document.getElementById('zoomSlider');
    if (s) { s.value = '100'; s.dispatchEvent(new Event('input', { bubbles: true })); }
    return !!s; })()`);
  await sleep(300);
  return { steps: 36, zTrace };
});

/* S-pan：空白左键拖 ~420px 横扫 */
await scene("pan-blank", (b, a) => {
  const pb = parseT(b), pa = parseT(a);
  return pb && pa && Math.abs(pa.panX - pb.panX) > 4;
}, async () => {
  /* 找一块空白点（elementFromPoint 落在 viewport/world 上） */
  const pt = await J(`(() => {
    const vp = document.getElementById('viewport').getBoundingClientRect();
    for (const [fx, fy] of [[0.5,0.08],[0.08,0.5],[0.9,0.85],[0.15,0.9],[0.9,0.1]]) {
      const x = vp.left + vp.width * fx, y = vp.top + vp.height * fy;
      const el = document.elementFromPoint(x, y);
      if (el && (el.id === 'viewport' || el.id === 'world' || el.id === 'edgesSvg'))
        return { x: Math.round(x), y: Math.round(y) };
    }
    return null;
  })()`);
  if (!pt) return { reason: "no-blank-point" };
  const sx = pt.x, sy = pt.y;
  await mouse("mousePressed", sx, sy, { button: "left", buttons: 1, clickCount: 1 });
  for (let i = 1; i <= 60; i++) {
    await mouse("mouseMoved", sx + i * 7, sy + Math.round(Math.sin(i / 8) * 12),
      { button: "left", buttons: 1 });
    await sleep(16);
  }
  await mouse("mouseReleased", sx + 420, sy, { button: "left", buttons: 0, clickCount: 1 });
  return { moves: 60, at: pt };
});

/* S-drag-card：拖卡纵向 ~200px 后落空白回弹（不写服务端）。
   前置：pan 场景已把相机推走 ~420px——用真实输入反向平移复位（两臂同路径、
   树内零钩子；diag 实测不复位时全树卡心出屏 ⇒ elementFromPoint 全 null）。 */
{
  const pt2 = await J(`(() => {
    const vp = document.getElementById('viewport').getBoundingClientRect();
    for (const [fx, fy] of [[0.5,0.08],[0.08,0.5],[0.9,0.85],[0.15,0.9],[0.9,0.1]]) {
      const x = vp.left + vp.width * fx, y = vp.top + vp.height * fy;
      const el = document.elementFromPoint(x, y);
      if (el && (el.id === 'viewport' || el.id === 'world' || el.id === 'edgesSvg'))
        return { x: Math.round(x), y: Math.round(y) };
    }
    return null;
  })()`);
  if (pt2) {
    await mouse("mousePressed", pt2.x, pt2.y, { button: "left", buttons: 1, clickCount: 1 });
    for (let i = 1; i <= 30; i++) {
      await mouse("mouseMoved", pt2.x - i * 14, pt2.y, { button: "left", buttons: 1 });
      await sleep(10);
    }
    await mouse("mouseReleased", pt2.x - 420, pt2.y, { button: "left", buttons: 0, clickCount: 1 });
    await sleep(300);
  }
}
await scene("drag-card-revert", (b, a, meta) => meta && meta.dragged === true, async () => {
  const pt = await J(`(() => {
    const ns = document.querySelectorAll('.card-node');
    for (const n of ns) {
      if (!n.dataset.id || n.classList.contains('is-root')) continue;
      const r = n.getBoundingClientRect();
      if (r.width <= 30 || r.height <= 30) continue;
      /* 中心点必须是卡体本身（不是按钮/徽标/链接等控件——
         控件上的按下按既有规则不进拖拽分支） */
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const el = document.elementFromPoint(x, y);
      if (!el || !n.contains(el)) continue;
      if (el.closest('button, .kid-badge, input, textarea, select, a')) continue;
      return { x: Math.round(x), y: Math.round(y), id: n.dataset.id };
    }
    return null;
  })()`);
  if (!pt) return { reason: "no-card-found", dragged: false };
  await mouse("mousePressed", pt.x, pt.y, { button: "left", buttons: 1, clickCount: 1 });
  await sleep(40);
  let sawDragging = false;
  for (let i = 1; i <= 50; i++) {
    await mouse("mouseMoved", pt.x, pt.y + i * 4, { button: "left", buttons: 1 });
    if (i === 20) {
      sawDragging = !!(await ev(`!!document.querySelector('.card-node.dragging')`));
    }
    await sleep(16);
  }
  /* 落到空白（回弹，无写请求） */
  await mouse("mouseReleased", pt.x, pt.y + 200, { button: "left", buttons: 0, clickCount: 1 });
  return { card: pt.id, dragged: sawDragging, moves: 50 };
});

/* ═══ [终轮三补件] 换父两案 + 穿窗边命中 ═══════════════════════════════
   源/目标选卡在 node 侧按 data.cards 做祖先/后代关系核算（前端判据同族：
   自身/后代/已是子卡前端即拒，类型闸留给服务端）。 */

const _byId = {}; for (const c of data.cards) _byId[c.id || c.card_id] = c;
const _kids = new Set(); for (const c of data.cards) if (c.parent) _kids.add(c.parent);
const _anc = (id) => { const s = new Set(); let p = _byId[id] && _byId[id].parent;
  while (p) { s.add(p); p = _byId[p] && _byId[p].parent; } return s; };
/* 在页候选：已挂载 + 中心命中自身（不被别卡/浮层遮） + 不在控件上。
   needKids：只挑已挂 .kid-badge 的容器卡——已有子卡 ⇒ 类型必然可育
   （服务端既接受过它的子卡），不猜词表（work_073 是 rd_project）。 */
/* 挂载候选清单（node 侧配对用）：已挂载 + 中心命中自身 + 不在控件上。
   hasKids：.kid-badge 在 ⇒ 类型必然可育（不猜词表）。 */
const listMounted = () => J(`(() => {
  const out = [];
  for (const n of document.querySelectorAll('.card-node')) {
    const id = n.dataset.id;
    if (!id || n.classList.contains('is-root')) continue;
    const r = n.getBoundingClientRect();
    /* 缩放出全景后卡会缩到 ~100×40（z≈0.45）——高度阈放宽到 20 才不失真 */
    if (r.width <= 20 || r.height <= 20) continue;
    /* 多点探测：容器卡中心常压标题/徽标控件——在卡体上找一块
       「命中本卡且不在控件上」的干净点（源要按它起拖、目标要压它落点） */
    let pt = null;
    for (const [fx, fy] of [[.5,.5],[.3,.5],[.7,.5],[.5,.78],[.3,.78],[.7,.78],[.5,.25],[.85,.5]]) {
      const x = r.left + r.width * fx, y = r.top + r.height * fy;
      const el = document.elementFromPoint(x, y);
      if (!el || !n.contains(el)) continue;
      if (el.closest('button, .kid-badge, input, textarea, select, a')) continue;
      pt = { x: Math.round(x), y: Math.round(y) };
      break;
    }
    if (!pt) continue;
    out.push({ ...pt, id, type: n.dataset.type,
      hasKids: !!n.querySelector('.kid-badge') });
  }
  return out;
})()`);

const dragTo = async (from, to) => {
  const steps = 40;
  const obs = { sawDragging: false, dropOk: false, dropBad: false };
  await mouse("mousePressed", from.x, from.y, { button: "left", buttons: 1, clickCount: 1 });
  await sleep(40);
  for (let i = 1; i <= steps; i++) {
    await mouse("mouseMoved",
      from.x + Math.round((to.x - from.x) * i / steps),
      from.y + Math.round((to.y - from.y) * i / steps),
      { button: "left", buttons: 1 });
    if (i % 8 === 0) {
      const s = await J(`({
        dragging: !!document.querySelector('.card-node.dragging'),
        ok: !!document.querySelector('.card-node.drop-ok'),
        bad: !!document.querySelector('.card-node.drop-bad'),
      })`);
      if (s) { obs.sawDragging ||= s.dragging; obs.dropOk ||= s.ok; obs.dropBad ||= s.bad; }
    }
    await sleep(14);
  }
  /* 落点前的最后一刻再记一次高亮（松手即清） */
  const s = await J(`({
    dragging: !!document.querySelector('.card-node.dragging'),
    ok: !!document.querySelector('.card-node.drop-ok'),
    bad: !!document.querySelector('.card-node.drop-bad'),
  })`);
  if (s) { obs.sawDragging ||= s.dragging; obs.dropOk ||= s.ok; obs.dropBad ||= s.bad; }
  await mouse("mouseReleased", to.x, to.y, { button: "left", buttons: 0, clickCount: 1 });
  dragTo._obs = obs;
  return obs;
};
const cardParent = async (cid) => {
  const d = await (await fetch(`${BASE}/api/works/${WID}/cards/${cid}`)).json();
  const c = (d && d.meta) || d;      /* 单卡端点包 {body, meta} */
  return (c && c.parent) ?? null;
};
const patchParent = async (cid, parent) => {
  const r = await fetch(`${BASE}/api/works/${WID}/cards/${cid}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { parent },
      history_note: "canvas_perf2 复原拖拽换父" }) });
  return r.status;
};
const lastPatchFor = (cid) => {
  for (let i = netLog.length - 1; i >= 0; i--)
    if (netLog[i].url.includes(`/cards/${cid}`)) return netLog[i];
  return null;
};

/* ── 换父场景共享：真实输入平移 + 目标导向 seek + 配对 ─────────────── */
const panBy = async (dx, dy) => {
  const pt = await J(`(() => {
    const vp = document.getElementById('viewport').getBoundingClientRect();
    for (const [fx, fy] of [[0.5,0.08],[0.08,0.5],[0.9,0.85],[0.15,0.9],[0.9,0.1],[0.5,0.5]]) {
      const x = vp.left + vp.width * fx, y = vp.top + vp.height * fy;
      const el = document.elementFromPoint(x, y);
      if (el && (el.id === 'viewport' || el.id === 'world' || el.id === 'edgesSvg'))
        return { x: Math.round(x), y: Math.round(y) };
    }
    return null;
  })()`);
  if (!pt) return false;
  await mouse("mousePressed", pt.x, pt.y, { button: "left", buttons: 1, clickCount: 1 });
  const steps = 24;
  for (let i = 1; i <= steps; i++) {
    await mouse("mouseMoved", pt.x + Math.round(dx * i / steps),
      pt.y + Math.round(dy * i / steps), { button: "left", buttons: 1 });
    await sleep(12);
  }
  await mouse("mouseReleased", pt.x + dx, pt.y + dy, { button: "left", buttons: 0, clickCount: 1 });
  await sleep(350);
  return true;
};
const seekCard = async (cid) => {
  /* 新树 __cvFocus 直达；基线读 DOM rect（全挂载）按方向分段真实拖动 */
  if (await ev(`typeof window.__cvFocus === 'function'`)) {
    await ev(`window.__cvFocus(${JSON.stringify(cid)})`);
    await sleep(700);
    return true;
  }
  for (let i = 0; i < 8; i++) {
    const pos = await J(`(() => {
      const n = document.querySelector('.card-node[data-id=${JSON.stringify(cid)}]');
      if (!n) return null;
      const r = n.getBoundingClientRect();
      const vp = document.getElementById('viewport').getBoundingClientRect();
      return { cx: r.left + r.width / 2, cy: r.top + r.height / 2,
               vx: vp.left + vp.width / 2, vy: vp.top + vp.height / 2,
               vw: vp.width, vh: vp.height };
    })()`);
    if (!pos) return false;
    const dx = Math.round(pos.vx - pos.cx), dy = Math.round(pos.vy - pos.cy);
    if (Math.abs(dx) < pos.vw * 0.4 && Math.abs(dy) < pos.vh * 0.4) return true;
    await panBy(Math.max(-1200, Math.min(1200, dx)),
                Math.max(-700, Math.min(700, dy)));
  }
  return false;
};
const tryPair = (cands, wantTgt) => {
  for (const src of cands) {
    const srcAnc = _anc(src.id);
    for (const tgt of cands) {
      if (!wantTgt(tgt, src) || tgt.id === src.id) continue;
      if (srcAnc.has(tgt.id)) continue;
      if (_anc(tgt.id).has(src.id)) continue;
      if (_byId[src.id].parent === tgt.id) continue;
      return { src, tgt };
    }
  }
  return null;
};

/* S-reparent-ok：叶卡 → 合法容器卡（can_have_children），
   核 PATCH 请求体 + 服务端终态；随后 API 复原父子树并刷新页面归位。 */
await scene("reparent-ok", (b, a, meta) =>
  meta && meta.patchOk && meta.parentAfter === meta.target,
async () => {
  let cands = (await listMounted()) || [];
  let pair = tryPair(cands, t => t.hasKids);
  /* 默认窗口只罩住一条链（唯一容器=所有候选叶之父）⇒ 真实输入缩放出
     （Ctrl+滚轮视口中心，两臂同构），整树同框后必出合法配对。
     seekCard 留作兜底。 */
  for (let round = 0; !pair && round < 2; round++) {
    const vr = await J(`document.getElementById('viewport').getBoundingClientRect().toJSON()`);
    const cx = Math.round(vr.x + vr.width / 2), cy = Math.round(vr.y + vr.height / 2);
    for (let i = 0; i < 3; i++) {
      await mouse("mouseWheel", cx, cy, { modifiers: 2, deltaX: 0, deltaY: 80 });
      await sleep(80);
    }
    await sleep(450);
    cands = (await listMounted()) || [];
    pair = tryPair(cands, t => t.hasKids);
  }
  const root = data.cards.find(c => !c.parent);
  for (const alt of data.cards.filter(c => root && c.parent === root.card_id)) {
    if (pair) break;
    if (cands.some(m => m.id === alt.card_id || _anc(m.id).has(alt.card_id))) continue;
    if (!(await seekCard(alt.card_id))) continue;
    cands = (await listMounted()) || [];
    pair = tryPair(cands, t => t.hasKids);
  }
  if (!pair) {
    /* 失败也要把相机/页面复回 projView 初态，不污染后续场景；
       缩放出会经 scheduleSaveView 写 projView ⇒ 先覆写回规范态再导航 */
    await ev(`localStorage.setItem("projView:${WID}", ${JSON.stringify(JSON.stringify({
      panX: 60, panY: 60, zoom: 1.0, expanded: [...parents], openCells: [] }))})`);
    await send("Page.navigate", { url: `${BASE}/work/${WID}` });
    await sleep(1500);
    return { reason: "no-pair", candidates: cands.length,
      candIds: cands.map(c => c.id + (c.hasKids ? "+" : "")) };
  }
  const { src, tgt } = pair;
  const p0 = _byId[src.id].parent;
  const n0 = netLog.length;
  const dragObs = await dragTo(src, tgt);
  /* 等 PATCH 回包 + 前端重拉/重排稳定 */
  for (let i = 0; i < 30; i++) {
    if (lastPatchFor(src.id)) break;
    await sleep(150);
  }
  await sleep(1200);
  const pa = lastPatchFor(src.id);
  const parentAfter = await cardParent(src.id);
  const patchOk = !!pa && pa.status === 200
    && pa.postData.includes(`"parent"`) && pa.postData.includes(tgt.id);
  /* 复原：一张卡、一次回交——PATCH 回原父，再刷新页面回到权威树。
     导航会销毁 JS 世界 ⇒ 先收采样器（手势帧已采完），随 meta 交回 scene()；
     缩放出写过的 projView 一并覆写回规范态再导航。 */
  const restoreStatus = await patchParent(src.id, p0);
  const __fr = (await Sampler.stop()) || { ts: [], lt: [] };
  await ev(`localStorage.setItem("projView:${WID}", ${JSON.stringify(JSON.stringify({
    panX: 60, panY: 60, zoom: 1.0, expanded: [...parents], openCells: [] }))})`);
  await send("Page.navigate", { url: `${BASE}/work/${WID}` });
  await sleep(600);
  /* 基线臂没有 __cvMount：等「卡数两次读相同」才算摆位稳定，
     否则后续 listMounted 读到的是布局过渡态（实测仅 3 候选）。 */
  let lastN = -1;
  for (let i = 0; i < 80; i++) {
    const n = Number(await ev(`document.querySelectorAll('.card-node').length`));
    const mv = await J(`window.__cvMount ? __cvMount().visible : null`);
    if (n > 0 && n === lastN && (mv == null || mv >= wantVisible)) break;
    lastN = n;
    await sleep(200);
  }
  await sleep(300);
  return { src: src.id, target: tgt.id, targetType: tgt.type,
    origParent: p0, dragObs,
    patchReq: pa ? { url: pa.url.split("/").pop(), status: pa.status,
      hasParentInBody: pa.postData.includes('"parent"'), hitTarget: pa.postData.includes(tgt.id) } : null,
    patchOk, parentAfter, restoreStatus, netSeen: netLog.length - n0, __fr };
});

/* S-reparent-reject：叶卡 → worker（can_have_children=false），
   前端不查类型闸 ⇒ 服务端 400 拒收 ⇒ 回弹+提示，父子树不变。 */
await scene("reparent-reject", (b, a, meta) =>
  meta && meta.rejected && meta.parentAfter === meta.origParent,
async () => {
  let cands = (await listMounted()) || [];
  const wantW = t => t.type === "worker";
  let pair = tryPair(cands, wantW);
  /* 视口内无 worker：先真实输入缩放出同框整树，再兜底 seek 到任一 worker */
  for (let round = 0; !pair && round < 2; round++) {
    const vr = await J(`document.getElementById('viewport').getBoundingClientRect().toJSON()`);
    const cx = Math.round(vr.x + vr.width / 2), cy = Math.round(vr.y + vr.height / 2);
    for (let i = 0; i < 3; i++) {
      await mouse("mouseWheel", cx, cy, { modifiers: 2, deltaX: 0, deltaY: 80 });
      await sleep(80);
    }
    await sleep(450);
    cands = (await listMounted()) || [];
    pair = tryPair(cands, wantW);
  }
  if (!pair) {
    const w = data.cards.find(c => c.card_type === "worker"
      && !cands.some(m => m.id === c.card_id));
    if (w && await seekCard(w.card_id)) {
      cands = (await listMounted()) || [];
      pair = tryPair(cands, wantW);
    }
  }
  if (!pair) {
    await ev(`localStorage.setItem("projView:${WID}", ${JSON.stringify(JSON.stringify({
      panX: 60, panY: 60, zoom: 1.0, expanded: [...parents], openCells: [] }))})`);
    await send("Page.navigate", { url: `${BASE}/work/${WID}` });
    await sleep(1500);
    return { reason: "no-pair", candidates: cands.length,
      workers: cands.filter(c => c.type === "worker").length };
  }
  const { src, tgt } = pair;
  const p0 = _byId[src.id].parent;
  const toasts0 = Number(await ev(`document.querySelectorAll('#toastContainer .toast.notify').length`));
  await dragTo(src, tgt);
  let pa = null;
  for (let i = 0; i < 40; i++) {
    pa = lastPatchFor(src.id);
    if (pa && pa.status !== undefined) break;
    await sleep(150);
  }
  await sleep(600);
  const parentAfter = await cardParent(src.id);
  const toasts = await J(`[...document.querySelectorAll('#toastContainer .toast.notify')].map(t=>t.textContent)`);
  /* 缩放出残留复位：滑块回到 100%，edge-hit 在规范视口判定 */
  await ev(`(() => { const s = document.getElementById('zoomSlider');
    if (s) { s.value = '100'; s.dispatchEvent(new Event('input', { bubbles: true })); }
    return !!s; })()`);
  await sleep(400);
  return { src: src.id, target: tgt.id, origParent: p0,
    patchStatus: pa ? pa.status : null,
    rejected: !!pa && pa.status >= 400,
    parentAfter, toasts: (toasts || []).slice(toasts0),
    notifyShown: (toasts || []).length > toasts0 };
});

/* S-edge-hit：穿窗边实际命中——取「端点卡不在场但路径已挂」的边（目录裁窗
   语义：边挂不挂看路径 bbox 与视口相交，不看端点），用其屏幕矩形中心做
   elementFromPoint——边层 pointer-events:none，命中应**透传到下层画布内容**
   （卡/viewport），即边在视觉穿窗且不吞输入。基线臂全挂载无 dangling，
   退而取任一路径矩形压窗的边做同一断言。 */
await scene("edge-hit", (b, a, meta) => meta && meta.hit === true, async () => {
  /* 先平移一把把部分卡推出挂载窗——端点不在场的穿窗边（dangling）
     才有候选；基线臂全挂载自然 dangling=0，退任一起止明确的压窗边。 */
  await panBy(-700, -500);
  return await J(`(() => {
    const vp = document.getElementById('viewport').getBoundingClientRect();
    const inVp = (x, y) => x >= vp.left && x <= vp.right && y >= vp.top && y <= vp.bottom;
    const cands = [];
    for (const p of document.querySelectorAll("#edgesSvg path[data-edge='parent-child']")) {
      const d = p.dataset;
      /* 起止明确的边才作数：主干段（trunk seg）to="" 是父卡自身的
         出/入桩，不是「卡→卡」的边——终轮验收要求起止卡可点名。 */
      if (!d.from || !d.to) continue;
      const endpointMounted = (id) => !!document.querySelector(
        '.card-node[data-id="' + id + '"]');
      const dangling = !endpointMounted(d.from) || !endpointMounted(d.to);
      const r = p.getBoundingClientRect();
      if (r.width < 2 && r.height < 2) continue;
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (!inVp(cx, cy)) continue;
      cands.push({ from: d.from, to: d.to, dangling, cx: Math.round(cx), cy: Math.round(cy) });
    }
    const pick = cands.find(c => c.dangling) || cands[0];
    if (!pick) return { reason: "no-crossing-edge", count: cands.length,
      danglingCount: cands.filter(c => c.dangling).length };
    const el = document.elementFromPoint(pick.cx, pick.cy);
    const onEdge = el && (el.tagName === 'path' || el.tagName === 'svg' || el.id === 'edgesSvg');
    /* 命中=压窗点的顶层元素是画布内容（卡/viewport/world），不是边自身——
       边视觉穿窗存在 + 输入透传，两者一起才算"穿窗边命中正确"。 */
    const passThrough = el && !onEdge &&
      !!(el.closest('#viewport, .card-node, #world') || el.id === 'viewport' || el.id === 'world');
    return { edge: pick.from + "→" + pick.to, dangling: pick.dangling,
      candidates: cands.length, danglingCount: cands.filter(c => c.dangling).length,
      at: [pick.cx, pick.cy], hitElement: el ? (el.id || el.className || el.tagName) : null,
      hit: passThrough };
  })()`);
});

result.netLog = netLog;   /* PATCH 取证全量留档 */

/* 写出 */
const fp = `logs/canvas-perf-${TAG}.json`;
fs.writeFileSync(fp, JSON.stringify(result, null, 1));
log(`written ${fp} invalid=${result.invalid.length}`);
ws.close();
cp.kill("SIGKILL");
try { fs.rmSync(prof, { recursive: true, force: true }); } catch { }
process.exit(0);
