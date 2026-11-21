/* tests/chat_work_isolation.test.mjs —— 跨 work 隔离与失败回滚的回归
 * （2026-11-20 返工项 #1 / #3）
 *
 * 用法：node tests/chat_work_isolation.test.mjs <baseUrl> <workA> <cardA> <workB> <cardB>
 *
 * 覆盖两条：
 *   W1 **右栏不串到另一个 work**：A 的卡详情（正文）延迟返回，期间切到 B 的
 *      **同号卡**（卡号只在项目内唯一）⇒ A 的正文不得盖到 B 的当前卡上。
 *      ⇒ 详情请求按 (work_id, card_id) 隔离；写入 currentCard 与渲染右栏前
 *         都要核对当前选择。
 *   W2 **开卡失败整套回滚**：跨 work 取卡失败（404）⇒ 不只是地址栏，
 *      WORK_ID / 对话画板 / 输入框都要退回上一次成功打开的卡，
 *      并且在原卡上**能继续输入、发送请求发到原卡**。
 *
 * 全程只读：W2 的发送请求用 CDP Fetch 拦下来**直接失败**（不落库）。
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WORK_A = process.argv[3] || "work_073";
const CARD_A = process.argv[4] || "card_007";
const WORK_B = process.argv[5] || "work_001";
const CARD_B = process.argv[6] || "card_007";
const INTERMEDIATE_CARD = process.argv[7] || "card_049";
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9600 + Math.floor(Math.random() * 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(id, name, ok, detail) {
  results.push({ id, name, ok: !!ok, detail: String(detail == null ? "" : detail) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${id}  ${name}${detail ? "  —— " + detail : ""}`);
}
function killTree(child) {
  try {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)],
        { stdio: "ignore", timeout: 15000 });
      return;
    }
  } catch (e) { /* 默认 */ }
  try { child.kill("SIGKILL"); } catch (e) { /* 已死 */ }
}

/* ── CDP ─────────────────────────────────────────────────── */
const profileDir = path.join(os.tmpdir(), "workiso_" + Date.now());
const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1440,900", "--no-first-run",
  "--user-data-dir=" + profileDir, "about:blank",
], { stdio: "ignore" });
let target = null;
for (let i = 0; i < 100 && !target; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    if (r.ok) target = await r.json();
  } catch { /* wait */ }
  if (!target) await sleep(200);
}
if (!target) { killTree(child); throw new Error("Chrome 未起来"); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => { ws.onopen = res; });
let id = 0; const pending = new Map();
/** 被扣住的请求：{url, requestId} —— 放行时 continueRequest。 */
const held = [];
/** 被拦下/放行过的所有 /api 请求（诊断用）。 */
const seenUrls = [];
/** 被拦下的写请求（不落库）：记录 URL。 */
const blockedWrites = [];
let holdDetail = false;
let holdIntermediate = false;

ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Fetch.requestPaused") {
    const p = m.params;
    const url = p.request.url;
    seenUrls.push(url.replace(BASE, ""));
    const isDetailA = holdDetail &&
      url.endsWith(`/api/works/${WORK_A}/cards/${CARD_A}`);   // ★ 精确等值：别把 /timeline 也扣住
    const isIntermediateTimeline = holdIntermediate &&
      new URL(url).pathname === `/api/works/${WORK_A}/cards/${INTERMEDIATE_CARD}/timeline`;
    if (isDetailA || isIntermediateTimeline) {
      held.push({ id: p.requestId, url });
      return;                                   // 扣住：等切到 B 之后再放行
    }
    if (p.request.method === "POST" && url.includes("/messages")) {
      blockedWrites.push(url);                  // 记下来但**不放行**（不写库）
      send("Fetch.failRequest", { requestId: p.requestId, errorReason: "Aborted" });
      return;
    }
    send("Fetch.continueRequest", { requestId: p.requestId });
  }
};
const send = (m, p = {}) => new Promise((res) => {
  const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  window.__probeWorkEvents = new Set();
  window.addEventListener("ui:card-changed", event => {
    window.__probeOpenedCard = event.detail.cardId;
  });
  window.EventSource = class extends window.EventSource {
    constructor(url, options) {
      super(url, options);
      if (/^\\/api\\/works\\/[^/]+\\/events$/.test(new URL(url, location.href).pathname)) {
        window.__probeWorkEvents.add(this);
      }
    }
    close() {
      window.__probeWorkEvents.delete(this);
      super.close();
    }
  };
` });
await send("Fetch.enable", { patterns: [{ urlPattern: "*/api/*" }, { urlPattern: "*/api/nav*" }] });
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

/* 右栏正文在**textarea 里**（可编辑区）⇒ innerText 读不到 ⇒ 指纹要连
   textarea 的 value 一起算。 */
const FINGERPRINT = `(() => {
  const b = document.getElementById("inspBody");
  if (!b) return "";
  const ta = [...b.querySelectorAll("textarea")].map(t => t.value || "").join("\\n");
  return (b.innerText || "") + "\\n" + ta;
})()`;

const api = async (path) => {
  const r = await fetch(`${BASE}${path}`);
  return await r.json();
};
async function waitFor(cond, timeoutMs = 30000, step = 200) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await ev(cond)) return true;
    await sleep(step);
  }
  return false;
}

/* ══════════════ 取两份卡的正文当「指纹」 ══════════════ */
const aFull = await api(`/api/works/${WORK_A}/cards/${CARD_A}`);
const bFull = await api(`/api/works/${WORK_B}/cards/${CARD_B}`);
const aMark = String((aFull.body && (aFull.body.mission || aFull.body.requirements || aFull.body.status_block)) || "").trim().slice(0, 40);
const bMark = String((bFull.body && (bFull.body.mission || bFull.body.requirements || bFull.body.status_block)) || "").trim().slice(0, 40);
console.log(`A=${WORK_A}/${CARD_A} 指纹="${aMark}"\nB=${WORK_B}/${CARD_B} 指纹="${bMark}"`);
if (!aMark || !bMark || aMark === bMark) {
  console.log("（两张卡的正文指纹不可区分 ⇒ 本用例无法判定，见报告）");
}

/* ══════════════ W1：右栏不串到另一个 work ══════════════ */
holdDetail = true;
await send("Page.navigate", { url: `${BASE}/work/${WORK_A}/card/${CARD_A}` });
await waitFor("document.querySelectorAll('.flow-view-column > *').length > 3", 60000);
// 展开右栏 ⇒ 触发「详情整卡」读口（A 的这条要被扣住）
await ev("(() => { const t = document.getElementById('inspTab'); if (t) t.click(); return true; })()");
await sleep(1500);
const heldCount = held.length;
const inspCls = await ev("document.getElementById('inspector').className");
console.log(`已扣住 A 的详情请求：${heldCount} 条；inspector class="${inspCls}"`);
console.log("  期间见到的 /api 请求：" + seenUrls.slice(0, 14).join("\n    "));

// 切到 B 的**同号卡**
await ev(`window.__openCard(${JSON.stringify(CARD_B)}, ${JSON.stringify(WORK_B)})`);
await waitFor(`window.__WORK_ID__ === ${JSON.stringify(WORK_B)}`, 30000);
await sleep(2500);                       // 让 B 的右栏也渲染完

// 放行 A 的迟到详情 ⇒ 它必须**进不了** B 的当前卡
holdDetail = false;
for (const h of held.splice(0)) {
  await send("Fetch.continueRequest", { requestId: h.id });
}
await sleep(2500);

const state = await ev(`JSON.stringify({
  wid: window.__WORK_ID__,
  cid: window.__CARD_ID__,
  insp: (document.getElementById("inspBody") || {}).innerText || "",
  inspAll: (() => { const b = document.getElementById("inspBody"); if (!b) return ""; return (b.innerText||"") + "\\n" + [...b.querySelectorAll("textarea")].map(t=>t.value||"").join("\\n"); })(),
})`);
const st = JSON.parse(state || "{}");
const insp = String(st.inspAll || st.insp || "");
const hasA = aMark ? insp.includes(aMark) : false;
const hasB = bMark ? insp.includes(bMark) : false;
check("W1a", "跨 work 切卡后 WORK_ID 停在 B", st.wid === WORK_B, `WORK_ID=${st.wid}`);
check("W1b", "右栏**不含**另一个 work 的正文", !hasA,
  `右栏含 A 的指纹=${hasA}（含 B 的指纹=${hasB}）`);
/* ★ 反向断言：右栏**必须**显示 B 的正文 —— 否则「不含 A」是空断言
   （右栏空着也算通过）。 */
check("W1c", "右栏显示的是 **B** 的正文（断言非空）", hasB,
  `含 B 的指纹=${hasB}（右栏长度 ${insp.length}）`);

/* ══════════════ W2：跨 work 取卡失败 ⇒ 整套回滚 ══════════════ */
// 先回到 A 的卡（成功打开一次 ⇒ 成为「上一次成功打开的卡」）
await ev(`window.__openCard(${JSON.stringify(CARD_A)}, ${JSON.stringify(WORK_A)})`);
await waitFor(`window.__WORK_ID__ === ${JSON.stringify(WORK_A)}`, 30000);
await sleep(2000);
const beforeW2 = await ev(`JSON.stringify({
  wid: window.__WORK_ID__,
  cid: window.__CARD_ID__,
  insp: (document.getElementById("inspBody") || {}).innerText || "",
  inspAll: (() => { const b = document.getElementById("inspBody"); if (!b) return ""; return (b.innerText||"") + "\\n" + [...b.querySelectorAll("textarea")].map(t=>t.value||"").join("\\n"); })(),
})`);

// 跨 work 取一张**不存在的卡**（404）
await ev(`window.__openCard("card___nope__", ${JSON.stringify(WORK_B)})`);
await sleep(4000);

const afterW2 = await ev(`JSON.stringify({
  wid: window.__WORK_ID__,
  cid: window.__CARD_ID__,
  path: location.pathname,
  insp: (document.getElementById("inspBody") || {}).innerText || "",
  inspAll: (() => { const b = document.getElementById("inspBody"); if (!b) return ""; return (b.innerText||"") + "\\n" + [...b.querySelectorAll("textarea")].map(t=>t.value||"").join("\\n"); })(),
  rows: document.querySelectorAll(".flow-view-column > *").length,
  composerDisabled: (() => { const t = document.querySelector(".flow-input-ta, textarea"); return t ? !!t.disabled : null; })(),
})`);
const a2 = JSON.parse(afterW2 || "{}");
const b2 = JSON.parse(beforeW2 || "{}");
check("W2a", "失败后 WORK_ID 退回上一次成功打开的项目", a2.wid === WORK_A,
  `WORK_ID=${a2.wid}（期望 ${WORK_A}）`);
check("W2b", "失败后当前卡退回上一次成功打开的卡", a2.cid === CARD_A,
  `__CARD_ID__=${a2.cid}（期望 ${CARD_A}）`);
check("W2c", "失败后地址栏也退回", String(a2.path || "").includes(`${WORK_A}/card/${CARD_A}`),
  `path=${a2.path}`);
check("W2d", "失败后右栏仍是原卡的内容", b2.inspAll ? String(a2.inspAll || "").includes(String(b2.inspAll).trim().slice(0, 30)) : false,
  `右栏首 30 字一致`);

/* W2e：原卡上**能继续输入、发送请求发到原卡**（发送被 CDP 拦下，不落库） */
await ev(`(() => {
  const t = document.querySelector(".flow-input-ta, textarea");
  if (!t) return false;
  t.focus();
  t.value = "回归探针";
  t.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
})()`);
await sleep(400);
const canType = await ev(`(() => {
  const t = document.querySelector(".flow-input-ta, textarea");
  return t ? (!t.disabled && t.value === "回归探针") : false;
})()`);
check("W2e", "失败后原卡的输入框可用（能继续输入）", !!canType, `输入框可写=${canType}`);

blockedWrites.length = 0;
await ev(`(() => {
  const t = document.querySelector(".flow-input-ta, textarea");
  if (!t) return false;
  t.focus();
  t.value = "回归探针";
  t.dispatchEvent(new Event("input", { bubbles: true }));
  const btn = document.querySelector(".flow-input-primary");
  if (btn) btn.click();
  return true;
})()`);
await sleep(2500);
const sentTo = blockedWrites.map((u) => u.replace(BASE, ""));
const sentOk = sentTo.length > 0 && sentTo.every((u) => u.includes(`/works/${WORK_A}/cards/${CARD_A}`));
const sendDiag = await ev(`JSON.stringify({
  disabled: (() => { const b = document.querySelector(".flow-input-primary"); return b ? b.disabled : null; })(),
  val: (() => { const t = document.querySelector(".flow-input-ta, textarea"); return t ? t.value : null; })(),
  taDisabled: (() => { const t = document.querySelector(".flow-input-ta, textarea"); return t ? t.disabled : null; })(),
})`);
check("W2f", "失败后发送请求发到**原卡**（不是失败目标）", sentOk,
  `拦下的写请求：${sentTo.join(" | ") || "（没发出）"}  诊断=${sendDiag}`);

// W3: B 的元数据已经成为 currentCard，时序仍在途。C 失败后查看实际发送目标。
await send("Page.navigate", { url: `${BASE}/work/${WORK_A}/card/${CARD_A}` });
const originalReady = await waitFor(`window.__probeOpenedCard === ${JSON.stringify(CARD_A)} &&
  document.querySelector('.flow-view-column > [data-chat-anchor-key]') &&
  document.querySelector('.flow-input-ta:not(:disabled)')`, 30000);
// 快照能先把行画出来。等 open 完成，才让 A 成为已确认的回滚目标。
if (originalReady) await ev(`window.__openCard(${JSON.stringify(CARD_A)}, ${JSON.stringify(WORK_A)})`);
holdIntermediate = true;
await ev(`(() => {
  window.__probeIntermediate = window.__openCard(${JSON.stringify(INTERMEDIATE_CARD)}, ${JSON.stringify(WORK_A)});
  return true;
})()`);
const intermediateReady = await waitFor(`window.__probeOpenedCard === ${JSON.stringify(INTERMEDIATE_CARD)}`, 15000);
check("W3a", "中间卡元数据已发布，时序仍扣在浏览器里", originalReady && intermediateReady && held.length > 0,
  `原卡=${originalReady} 中间卡=${intermediateReady} 扣住=${held.length}`);
await ev(`window.__openCard("card___nope__", ${JSON.stringify(WORK_B)})`);
holdIntermediate = false;
for (const h of held.splice(0)) await send("Fetch.continueRequest", { requestId: h.id });
await ev("window.__probeIntermediate");
const actualSubscriptions = await ev(`[...window.__probeWorkEvents].map(es => new URL(es.url).pathname)`);
const actualPage = await ev("location.pathname");
check("W3b", "失败回滚后地址与工作级 SSE 都属于原卡的 work",
  actualPage === `/work/${WORK_A}/card/${CARD_A}` &&
    JSON.stringify(actualSubscriptions) === JSON.stringify([`/api/works/${WORK_A}/events`]),
  `地址=${actualPage} SSE=${JSON.stringify(actualSubscriptions)}`);
blockedWrites.length = 0;
await ev(`(() => {
  const input = document.querySelector(".flow-input-ta");
  if (!input) return false;
  input.focus();
  input.value = "三次切卡回归探针";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
})()`);
const sendReady = await waitFor(`(() => {
  const button = document.querySelector(".flow-input-primary");
  return button && !button.disabled && button.querySelector("svg path");
})()`, 10000);
if (sendReady) await ev(`document.querySelector(".flow-input-primary").click()`);
for (let i = 0; i < 80 && sendReady && !blockedWrites.length; i++) await sleep(100);
const actualTargets = blockedWrites.map(url => new URL(url).pathname);
const sendState = await ev(`(() => {
  const input = document.querySelector(".flow-input-ta");
  const button = document.querySelector(".flow-input-primary");
  return { label: button?.getAttribute("aria-label"), disabled: button?.disabled, value: input?.value };
})()`);
check("W3c", "A→B→C 失败后发送目标仍为 A",
  actualTargets.length > 0 && actualTargets.every(url => url === `/api/works/${WORK_A}/cards/${CARD_A}/messages`),
  `就绪=${sendReady} 路径=${actualTargets.join(" | ")} 状态=${JSON.stringify(sendState)}`);

/* ══════════════ 收尾 ══════════════ */
ws.close();
killTree(child);
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { /* 临时目录 */ }
const bad = results.filter((r) => !r.ok);
console.log(`\n通过 ${results.length - bad.length}/${results.length}`);
process.exit(bad.length ? 1 : 0);
