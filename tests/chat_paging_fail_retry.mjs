/* tests/chat_paging_fail_retry.mjs —— 翻页失败/重试专项探针（任务二收尾）
 *
 * 流程：首屏 → 上翻成功 1 个网络页（建立游标与入口）→ CDP 把下一个
 * before= 请求回 500 → 断言「内容/游标/重试入口俱在」→ 再上翻 →
 * 新请求成功应用 = 重试路径可用。迟到回包由 stream._epoch 世代闸拦截
 * （单测覆盖；本探针验证失败不破坏性落账）。
 *
 * 用法：node tests/chat_paging_fail_retry.mjs <baseUrl> <workId> <cardId>
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = (msg) => { console.log("FAIL " + msg); process.exitCode = 1; };

const PORT = 9600 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "failretry_" + Date.now());
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
const tlReqs = [];
let fetchKill = false;
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Fetch.requestPaused") {
    const u = m.params.request.url;
    if (fetchKill && u.includes("/timeline?") && u.includes("before=")) {
      send("Fetch.fulfillRequest",
        { requestId: m.params.requestId, responseCode: 500, body: "e30=" }).catch(() => {});
      fetchKill = "spent";
      console.error("[kill] 500 ->", u.slice(-60));
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

const METRIC = `(() => {
  const seg = window.__PERF_SEG || {};
  return {
    rows: document.querySelectorAll("[data-chat-anchor-key]").length,
    pages: (seg.prepend && seg.prepend.pages) || 0,
    olderVisible: !!(document.querySelector(".flow-view-older-btn") || {}).offsetParent,
    loadingOlder: [...document.querySelectorAll("button")]
      .some((x) => /载入历史/.test(x.textContent)),
  };
})()`;
const scrollTop = `document.querySelector(".flow-view-scroll").scrollTop = 0`;

await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });
let ok = false;
for (let i = 0; i < 2400; i++) {
  await sleep(50);
  const n = await ev(`document.querySelectorAll("[data-chat-anchor-key]").length`);
  if (n && n > 3) { ok = true; break; }
}
if (!ok) { ws.close(); killTree(); fail("首屏行未出现"); process.exit(1); }
await sleep(1200);

const out = { base: BASE, wid: WID, cid: CID, steps: [] };

/* 1) 先成功上翻一页（建立游标）。滚-等循环直到 pages>=1。 */
let m = null;
for (let t = 0; t < 40 && !(m && m.pages >= 1); t++) {
  await ev(`(() => { ${scrollTop}; return 1; })()`);
  await sleep(150);
  const cur = await ev(METRIC);
  if (cur && cur.olderVisible && !cur.loadingOlder) {
    await ev(`(() => { const b = document.querySelector(".flow-view-older-btn");
      if (b && !b.disabled) b.click(); return 1; })()`);
  }
  await sleep(700);
  m = await ev(METRIC);
}
const p1 = m && m.pages || 0;
out.steps.push({ step: "first-page", pages: p1, rows: m && m.rows });
if (p1 < 1) { ws.close(); killTree(); fail("首页前翻未成功"); process.exit(1); }
console.error("[1] first page ok rows=" + m.rows);

/* 2) 注入 500：下一次 before= 请求被杀。分两帧滚动防事件合并。 */
const reqBefore = tlReqs.length;
fetchKill = true;
await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 400; return 1; })()`);
await sleep(150);
await ev(`(() => { ${scrollTop}; return 1; })()`);
let killed = false;
for (let w = 0; w < 160 && !killed; w++) {
  await sleep(50);
  if (tlReqs.length > reqBefore) { killed = true; break; }
  /* 内存窗口先耗尽才有网络请求——每 300ms 重发一次 400→0 顶翻＋按钮兜底。 */
  if (w % 6 === 3) {
    await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 400; return 1; })()`);
    await sleep(60);
    await ev(`(() => { ${scrollTop}; return 1; })()`);
    await ev(`(() => { const b = document.querySelector(".flow-view-older-btn");
      if (b && !b.disabled) b.click(); return 1; })()`);
  }
}
await sleep(1500);
const afterKill = await ev(METRIC);
const kept = (afterKill.rows || 0) > 0 && afterKill.pages === p1;
out.steps.push({ step: "http500", killed, rowsAfter: afterKill.rows,
  pages: afterKill.pages, keptContent: kept,
  retryEntry: afterKill.olderVisible && !afterKill.loadingOlder });
console.error("[2] killed=" + killed + " rows=" + afterKill.rows +
  " olderVisible=" + afterKill.olderVisible + " loading=" + afterKill.loadingOlder);
if (!killed) fail("500 注入未命中请求");
if (!kept) fail("失败后内容/页账被清");
if (!(afterKill.olderVisible && !afterKill.loadingOlder)) fail("重试入口不可用");

/* 3) 重试：再上翻 ⇒ 新请求成功应用。 */
fetchKill = false;
const rb2 = tlReqs.length;
let retried = false;
for (let t = 0; t < 60 && !retried; t++) {
  await ev(`(() => { document.querySelector(".flow-view-scroll").scrollTop = 400; return 1; })()`);
  await sleep(150);
  await ev(`(() => { ${scrollTop}; return 1; })()`);
  await sleep(200);
  const cur = await ev(METRIC);
  if (tlReqs.length > rb2 && cur.pages > p1 && !cur.loadingOlder) retried = true;
  if (cur && cur.olderVisible && !cur.loadingOlder) {
    await ev(`(() => { const b = document.querySelector(".flow-view-older-btn");
      if (b && !b.disabled) b.click(); return 1; })()`);
  }
  await sleep(500);
}
out.steps.push({ step: "retry", ok: retried, pages: (await ev(METRIC)).pages });
console.error("[3] retry ok=" + retried);
if (!retried) fail("重试未成功应用新页");

console.log(JSON.stringify(out, null, 1));
console.log(process.exitCode ? "DONE(fail)" : "PASS 失败保留现场+重试续翻可用");
ws.close(); killTree();
process.exit(process.exitCode || 0);
