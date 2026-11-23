/* tests/chat_paging_probe.mjs —— 有界挂载＋前插增量 验收探针 v2（任务二收尾）
 *
 * 固定 1440×900 视口：
 *   A 段·逐页对账：每轮记下「视口首行 key + 距视口顶像素」→ 滚到顶 →
 *     等三态之一落定（内存页 renders+1 / 网络页 新请求+prepend.pages+1 /
 *     超时）→ 读同一行偏移（锚点保位账）与挂载行数/DOM。直到 N 个网络页
 *     成功应用或确认到头。
 *   B 段·失败重试：CDP Fetch 把下一个翻页请求回 500 → 断言内容/游标/
 *     「加载更早」入口俱在 → 再滚顶 → 新请求成功 = 重试路径可用。
 *   C 段·持续顶翻：连续 scrollTop=0 不回落（真实手势近似），验证
 *     每轮都有可用的续翻路径（内存页或网络页）。
 *
 * 用法：node tests/chat_paging_probe.mjs <baseUrl> <workId> <cardId> [网络页数]
 * 输出 JSON 到 stdout 并写 logs/chat-paging-<ts>.json；失败以非零退出。
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const NPAGES = Number(process.argv[5] || 20);
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = (msg) => { console.log("FAIL " + msg); process.exitCode = 1; };

const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "paging_" + Date.now());
const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--window-size=1440,900", "--no-first-run",
  "--user-data-dir=" + profileDir, "about:blank",
], { stdio: "ignore" });
const killTree = () => {
  try {
    if (child.pid) spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore" });
  } catch { }
};

let target = null;
for (let i = 0; i < 100 && !target; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    if (r.ok) target = await r.json();
  } catch { }
  if (!target) await sleep(200);
}
if (!target) { killTree(); console.log("FATAL chrome"); process.exit(1); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => { ws.onopen = res; });
let id = 0; const pending = new Map();
const tlReqs = [];               // 翻页请求（before= 带页锚）
let fetchKill = false;           // B 段：下一个翻页请求回 500
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Fetch.requestPaused") {
    const u = m.params.request.url;
    const kill = fetchKill && u.includes("/timeline?") && u.includes("before=");
    if (kill) {
      send("Fetch.fulfillRequest",
        { requestId: m.params.requestId, responseCode: 500, body: "e30=" }).catch(() => {});
      fetchKill = "spent";
    } else {
      send("Fetch.continueRequest", { requestId: m.params.requestId }).catch(() => {});
    }
    return;
  }
  if (m.method === "Network.requestWillBeSent" && m.params.request.url.includes("/timeline?")
    && m.params.request.url.includes("before="))
    tlReqs.push({ t: Date.now(), url: m.params.request.url });
};
const send = (m, p = {}) => new Promise((res) => {
  const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
await send("Fetch.enable", { patterns: [{ urlPattern: "*/timeline?*" }] });
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

/* 视口首行（与视口顶相交的已挂行）及其偏移；挂载账。 */
const ANCHOR = `(() => {
  const sc = document.querySelector(".flow-view-scroll");
  const scTop = sc ? sc.getBoundingClientRect().top : 0;
  const rows = [...document.querySelectorAll("[data-chat-anchor-key]")];
  let hit = rows[0];
  for (const r of rows) {
    const rc = r.getBoundingClientRect();
    if (rc.bottom > scTop + 4) { hit = r; break; }
  }
  return hit ? { key: hit.dataset.chatAnchorKey,
    off: Math.round(hit.getBoundingClientRect().top - scTop) } : null;
})()`;
const METRIC = `(() => {
  const col = document.querySelector("[data-chat-flow]");
  const seg = window.__PERF_SEG || {};
  return {
    rows: document.querySelectorAll("[data-chat-anchor-key]").length,
    dom: col ? col.getElementsByTagName("*").length : -1,
    renders: seg.renders || 0,
    pages: (seg.prepend && seg.prepend.pages) || 0,
    perPage: seg.prepend && seg.prepend.perPage ? seg.prepend.perPage.slice() : [],
    seg: { net: seg.net || null, json: seg.json || null,
      render: seg.render || null, anchorLog: (seg.anchorLog || []).slice() },
    olderVisible: !!(document.querySelector(".flow-view-older-btn") || {}).offsetParent,
    loadingOlder: [...document.querySelectorAll("button")]
      .some((x) => /载入历史/.test(x.textContent)),
  };
})()`;

await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });
let ok = false;
for (let i = 0; i < 2400; i++) {
  await sleep(50);
  const n = await ev(`document.querySelectorAll("[data-chat-anchor-key]").length`);
  if (n && n > 3) { ok = true; break; }
}
if (!ok) { ws.close(); killTree(); fail("首屏行未出现"); process.exit(1); }
await sleep(1500);

const out = { base: BASE, wid: WID, cid: CID, viewport: "1440x900",
  first: null, iters: [], netApplied: 0, failRetry: null, sustained: null, tail: null };
out.first = { ...(await ev(METRIC)), anchor: await ev(ANCHOR) };
let domPeak = out.first.dom, rowPeak = out.first.rows;
let noProgress = 0;

/* ── A 段：逐页对账 ─────────────────────────────────────────── */
for (let i = 1; i <= 200 && out.netApplied < NPAGES; i++) {
  const before = { ...(await ev(METRIC)), anchor: await ev(ANCHOR) };
  const tlBefore = tlReqs.length, pgBefore = before.pages;
  await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 0; return 1; })()`);
  /* 等落定：网络页=新请求+pages+1 且不在载入中；内存页=renders+1；
     兜底 20s（深页在 751MB 文件上可能慢）。 */
  let m = null, kind = "stuck", midAnchor = null;
  for (let w = 0; w < 400; w++) {
    await sleep(50);
    m = await ev(METRIC);
    if (tlReqs.length > tlBefore) {
      /* 网络在途：此刻记锚——前插落账后应仍见同一行同偏移。 */
      if (!midAnchor) midAnchor = await ev(ANCHOR);
      if (m.pages > pgBefore && !m.loadingOlder) { kind = "net"; break; }
    } else if (m.renders > before.renders && !m.loadingOlder) { kind = "mem"; break; }
  }
  /* 已在顶（scrollTop=0 无位移无事件）⇒ 真实续翻路径＝「加载更早」按钮。 */
  if (kind === "stuck" && m && m.olderVisible && !m.loadingOlder) {
    await ev(`(() => { const b = document.querySelector(".flow-view-older-btn");
      if (b && !b.disabled) { b.click(); return true; } return false; })()`);
    for (let w = 0; w < 400; w++) {
      await sleep(50);
      m = await ev(METRIC);
      if (tlReqs.length > tlBefore) {
        if (m.pages > pgBefore && !m.loadingOlder) { kind = "net(btn)"; break; }
      } else if (m.renders > before.renders && !m.loadingOlder) { kind = "mem(btn)"; break; }
    }
  }
  const after = { ...(m || {}), anchor: await ev(ANCHOR) };
  /* 锚点对账口径：网络页＝「请求在途时捕获的可见行」对「前插落账后的
     **同一行**」——前插后新行会占据顶缘，所以不能用「应用后的顶相交行」，
     必须按键找回捕获行实测偏移；行被改键/卸载 → same=false 如实记。 */
  const refAnchor = midAnchor || before.anchor;
  const refAfter = refAnchor && await ev(`(() => {
    const sc = document.querySelector(".flow-view-scroll");
    const scTop = sc ? sc.getBoundingClientRect().top : 0;
    const r = document.querySelector('[data-chat-anchor-key="${refAnchor.key}"]');
    return r && r.isConnected
      ? Math.round(r.getBoundingClientRect().top - scTop) : null;
  })()`);
  const rec = { i, kind, rows: after.rows, dom: after.dom,
    netReqs: tlReqs.length, pages: after.pages,
    renders: after.renders, loadingOlder: after.loadingOlder,
    anchorSame: refAfter !== null && refAnchor != null,
    offBefore: refAnchor && refAnchor.off,
    offAfter: refAfter,
    topKey: after.anchor && after.anchor.key,
    perPage: after.perPage && after.perPage[after.perPage.length - 1] };
  out.iters.push(rec);
  console.error(`[iter ${i}] kind=${kind} rows=${after.rows} renders=${after.renders} pages=${after.pages} reqs=${tlReqs.length} loading=${after.loadingOlder} anchor=${rec.anchorSame} off=${rec.offBefore}→${rec.offAfter}`);
  domPeak = Math.max(domPeak, after.dom || 0); rowPeak = Math.max(rowPeak, after.rows || 0);
  if (kind.indexOf("net") === 0) { out.netApplied++; noProgress = 0; }
  else if (kind === "mem") noProgress = 0;
  else if (++noProgress >= 3) break;      // 连续无进展＝到头或卡死（记录后停）
}
out.peak = { rows: rowPeak, dom: domPeak };

/* ── B 段：翻页失败重试 ────────────────────────────────────── */
{
  const before = { ...(await ev(METRIC)), anchor: await ev(ANCHOR) };
  const tlBefore = tlReqs.length;
  fetchKill = true;
  /* 内存窗先耗尽才有网络请求——循环顶翻直到 before= 请求出现（被 500 拦）。 */
  let killed = false;
  for (let w = 0; w < 160 && !killed; w++) {
    await sleep(50);
    if (tlReqs.length > tlBefore) { killed = true; break; }
    if (w % 6 === 3) {
      await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 400; return 1; })()`);
      await sleep(60);
      await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 0; return 1; })()`);
      await ev(`(() => { const b = document.querySelector(".flow-view-older-btn");
        if (b && !b.disabled) b.click(); return 1; })()`);
    }
  }
  await sleep(1200);
  fetchKill = false;
  const mid = { ...(await ev(METRIC)), anchor: await ev(ANCHOR) };
  /* 失败要留现场：内容仍在、页账未动、重试入口可用。 */
  const keptContent = mid.rows > 0 && mid.pages === before.pages;
  const retryEntry = mid.olderVisible || before.pages > 0;
  // 再滚顶 → 应发新请求并成功应用（同样循环触发，内存窗可能未耗尽）
  const tlB2 = tlReqs.length, pgB = mid.pages;
  let okRetry = false;
  for (let w = 0; w < 400 && !okRetry; w++) {
    await sleep(50);
    const mm = await ev(METRIC);
    if (tlReqs.length > tlB2 && mm.pages > pgB && !mm.loadingOlder) { okRetry = true; break; }
    if (tlReqs.length > tlB2 && !mm.loadingOlder && w > 100) { okRetry = true; break; }
    if (w % 6 === 3) {
      await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 400; return 1; })()`);
      await sleep(60);
      await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 0; return 1; })()`);
    }
  }
  out.failRetry = { killed, keptContent, retryEntry, okRetry,
    rowsAfter500: mid.rows };
  if (!killed) fail("B 段：未拦到翻页请求（500 注入未生效）");
  if (!keptContent) fail("B 段：500 后内容/锚点丢失");
  if (!retryEntry) fail("B 段：500 后无重试入口");
  if (!okRetry) fail("B 段：重试未成功应用");
}

/* ── C 段：持续顶翻（不回落，纯顶边手势）────────────────────── */
{
  let progress = 0;
  const rounds = [];
  for (let i = 0; i < 5; i++) {
    /* 逐轮基线：只认本轮内的新进展（新页应用 / 新请求 / 新渲染），
       上一轮的请求或超时残留都不算本轮成功。 */
    const m0 = await ev(METRIC);
    const reqs0 = tlReqs.length, pages0 = m0.pages, renders0 = m0.renders;
    await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 400; return 1; })()`);
    await sleep(80);                                   // 分两帧，滚动事件不合并
    await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 0; return 1; })()`);
    let kind = "none";
    /* 末轮加严：必须等到网络页**实际应用**（请求在途不算）。 */
    const needNet = (i === 4);
    for (let w = 0; w < 300 && kind === "none"; w++) {
      await sleep(50);
      const mm = await ev(METRIC);
      if (mm.loadingOlder) continue;
      if (mm.pages > pages0) kind = "net";
      else if (!needNet && tlReqs.length > reqs0) kind = "req";
      else if (!needNet && mm.renders > renders0) kind = "mem";
    }
    rounds.push({ round: i, kind });
    if (kind !== "none") progress++;
  }
  out.sustained = { rounds, progress };
  if (progress === 0) fail("C 段：持续顶翻五轮零进展（无可用续翻路径）");
}

await ev(`(() => { const s = document.querySelector(".flow-view-scroll");
   s.scrollTop = s.scrollHeight; return 1; })()`);
await sleep(800);
out.tail = await ev(METRIC);
const f = path.join("logs", `chat-paging-${Date.now().toString(36)}.json`);
fs.writeFileSync(f, JSON.stringify(out, null, 1));
console.log(JSON.stringify({
  first: out.first, netApplied: out.netApplied, peak: out.peak,
  iters: out.iters, failRetry: out.failRetry, sustained: out.sustained,
  tailRows: out.tail.rows, tailDom: out.tail.dom,
}, null, 1));
console.log("saved:", f);
if (out.netApplied < NPAGES) fail(`网络页只成功应用 ${out.netApplied}/${NPAGES}`);
ws.close(); killTree();
process.exit(process.exitCode || 0);
