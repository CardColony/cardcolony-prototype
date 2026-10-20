/* _audit.mjs —— 画布体检探针（CDP 无头 Chrome）
 *
 * 用途：把"挑不出毛病"变成可重复的量化检查。同一套探针在改动前后各跑一次，
 * 差异即回归。
 *
 * 用法：node tests/canvas_audit.mjs [baseUrl] [workId] [width] [height]
 * 输出：JSON（stdout）＋ 人类可读摘要（stderr）
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5711";
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
  "--user-data-dir=" + path.join(os.tmpdir(), "cdpaudit_" + Date.now()), "about:blank",
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
const netFails = [];
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((res) => { ws.onopen = res; });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled" && (m.params.type === "error" || m.params.type === "warning")) {
    consoleErrs.push(m.params.type + ": " + (m.params.args || []).map(a => a.value ?? a.description ?? "").join(" ").slice(0, 300));
  }
  if (m.method === "Runtime.exceptionThrown") {
    consoleErrs.push("exception: " + (m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || "").slice(0, 300));
  }
  if (m.method === "Network.loadingFailed") netFails.push(m.params.errorText + " " + (m.params.type || ""));
  if (m.method === "Network.responseReceived" && m.params.response.status >= 400) {
    netFails.push(m.params.response.status + " " + m.params.response.url);
  }
};
await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Log.enable");
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });

const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result && r.result.result ? r.result.result.value : undefined;
};

// ── 导航并等稳定 ──────────────────────────────────────────────
// 相位计时：注入一个观察器，量「导航 → 首张卡出现 → 卡片数稳定」。
// 只看总时长无法定位慢在哪一段（网络？布局？二次适配？）。
const t0 = Date.now();
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `
    window.__phase = { navStart: performance.now(), firstCard: 0, stableCount: 0 };
    const _install = () => {
      const mo = new MutationObserver(() => {
        const n = document.querySelectorAll('.card-node').length;
        if (n > 0 && !window.__phase.firstCard) window.__phase.firstCard = performance.now();
        if (n !== window.__phase.stableCount) { window.__phase.stableCount = n; window.__phase.lastChange = performance.now(); }
      });
      mo.observe(document.documentElement, { childList: true, subtree: true });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _install);
    else _install();
  `,
});
await send("Page.navigate", { url: `${BASE}/work/${WID}` });
let stableAt = 0, last = -1, stableFor = 0;
for (let i = 0; i < 240; i++) {
  await sleep(50);
  const n = Number(await ev(`document.querySelectorAll('.card-node').length`)) || 0;
  if (n === last && n > 0) { stableFor += 50; if (stableFor >= 400) { stableAt = Date.now() - t0; break; } }
  else { stableFor = 0; last = n; }
}
const phase = await ev(`(() => {
  const p = window.__phase || {};
  return {
    firstCardMs: p.firstCard ? Math.round(p.firstCard - p.navStart) : -1,
    lastChangeMs: p.lastChange ? Math.round(p.lastChange - p.navStart) : -1,
    navToStableMs: ${stableAt},
  };
})()`);

// ── 探针 ──────────────────────────────────────────────────────
const probe = await ev(`(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const cs = (el) => el ? getComputedStyle(el) : null;
  const world = q('#world');
  const nodes = qa('.card-node');
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  // 1) 画布是否可见（world 的行内 visibility 会不会永久 hidden）
  const worldVis = world ? world.style.visibility : '(no world)';

  // 2) 卡片是否真在视口内（有没有被挤出画布）
  const vp = q('#viewport');
  const vpr = vp ? vp.getBoundingClientRect() : { left: 0, top: 0, right: 0, bottom: 0 };
  let offscreen = 0, zeroPos = 0, noSize = 0;
  for (const n of nodes) {
    const r = n.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) { noSize++; continue; }
    if (r.bottom < vpr.top - 2 || r.top > vpr.bottom + 2 || r.right < vpr.left - 2 || r.left > vpr.right + 2) offscreen++;
    if (n.style.left === '0px' && n.style.top === '0px') zeroPos++;
  }

  // 3) 悬停浮层是否常驻可见（display 被 CSS 压成 flex 的历史坑）
  const tip = q('#cardTooltip');
  const tipCs = tip ? getComputedStyle(tip) : null;
  const tipRect = tip ? tip.getBoundingClientRect() : null;

  // 4) 空壳/不可见元素：卡片内是否为空（无标题）
  let emptyCards = 0;
  for (const n of nodes) {
    const t = n.querySelector('.tname');
    if (!t || !t.textContent.trim()) emptyCards++;
  }

  // 5) 溢出视口的固定/浮层元素
  //    ⚠ 只查**垂直**溢出：收起的抽屉/浮层是 translateX(100%) 移到屏幕外的，
  //    那是设计（点开才进来），横向"溢出"是正常态，不该报。
  const overTop = [];
  for (const el of qa('body *')) {
    const s = cs(el);
    if (!s || s.position !== 'fixed') continue;
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > innerHeight + 1) {
      overTop.push(el.id || el.className || el.tagName);
    }
  }

  // 6) 命中区小于 24px 的可点控件（屏幕坐标）
  const smallHits = [];
  for (const el of qa('button, .kid-badge, .cv-open, input, [role=button]')) {
    if (!el.offsetParent && el.offsetWidth === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (r.width < 24 || r.height < 24) {
      smallHits.push((el.id || el.className || el.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
    }
  }

  // 7) 顶栏按钮是否溢出
  const tb = q('.topbar');
  const tbRect = tb ? tb.getBoundingClientRect() : null;

  return {
    worldVisibility: worldVis,
    cardCount: nodes.length,
    offscreen, zeroPos, noSize, emptyCards,
    tooltip: tip ? {
      display: tipCs.display, visibility: tipCs.visibility, pointerEvents: tipCs.pointerEvents,
      active: tip.classList.contains('active'),
      rect: tipRect ? { x: Math.round(tipRect.x), y: Math.round(tipRect.y), w: Math.round(tipRect.width), h: Math.round(tipRect.height) } : null,
      textLen: (tip.textContent || '').trim().length,
    } : null,
    fixedOverflow: overTop,
    smallHits: smallHits.slice(0, 25),
    smallHitCount: smallHits.length,
    topbar: tbRect ? { w: Math.round(tbRect.width), h: Math.round(tbRect.height) } : null,
    zoom: q('#zoomDisplay') ? q('#zoomDisplay').textContent : '',
    domTotal: document.querySelectorAll('*').length,
    stateBox: !!q('#canvasStateBox'),
    rulePanelOpen: q('#rulePanel') ? q('#rulePanel').classList.contains('is-open') : false,
  };
})()`);

// ── 交互探测：单击一张卡后是否发生整页跳转（应否） ───────────
const navProbe = await ev(`(() => {
  const n = document.querySelector('.card-node[data-depth="1"]') || document.querySelector('.card-node');
  if (!n) return { skipped: true };
  const r = n.getBoundingClientRect();
  const before = location.href;
  n.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: r.left + 20, clientY: r.top + 12, view: window }));
  return { before, after: location.href, id: n.dataset.id };
})()`);
await sleep(600);
const navAfter = await ev(`location.href`);

// ── 展开/折叠：节点数是否变化、是否重排 ─────────────────────
const toggleProbe = await ev(`(() => {
  const before = document.querySelectorAll('.card-node').length;
  const badge = document.querySelector('.card-node .kid-badge');
  if (!badge) return { skipped: true, before };
  badge.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  return { before, badgeFound: true };
})()`);
await sleep(500);
const afterToggle = Number(await ev(`document.querySelectorAll('.card-node').length`));

const out = {
  work: WID, viewport: { W, H },
  firstPaintMs: stableAt,
  probe,
  clickNav: { ...navProbe, finalHref: navAfter },
  toggle: { ...toggleProbe, afterNodes: afterToggle },
  consoleErrors: consoleErrs.slice(0, 20),
  consoleErrorCount: consoleErrs.length,
  networkFailures: netFails.slice(0, 20),
  networkFailureCount: netFails.length,
};
console.log(JSON.stringify(out, null, 1));
ws.close();
child.kill();
process.exit(0);
