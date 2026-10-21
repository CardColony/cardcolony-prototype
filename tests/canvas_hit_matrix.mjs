/* canvas_hit_matrix.mjs —— 卡面**命中矩阵 + 行为矩阵**验收（CDP）
 *
 * 这支脚本是 2026-10-20 那次回归的直接解药。
 *
 * 事故：为了扩大子卡徽章的命中区，给它加了 ::after{position:absolute;left:0;right:0}，
 * 而徽标是 position:static ⇒ 伪元素的包含块变成**整张卡** ⇒ 整张卡的点击都被徽标
 * 吃掉 ⇒ **有子卡的卡单击变成"展开/折叠子卡"，没有子卡的卡单击正常进对话**。
 *
 * 教训：改了卡面上的可点元素，验收就必须覆盖"每个位置的命中归属"与
 *      "每个手势的唯一结果"。只看"按钮画出来了/尺寸对不对"是不够的。
 *
 * 本脚本分两段：
 *   A. 命中矩阵：点上每个位置，elementFromPoint 必须给出**预期**的那个元素，
 *      而且必须属于**本卡**（不能被邻卡的元素抢走）。
 *   B. 行为矩阵：真点真看结果 ——
 *      · 点标题      ⇒ URL 变成 /work/{w}/card/{cid}（进对话，不是展开）
 *      · 点徽标      ⇒ 子卡可见数变化（展开/折叠）
 *      · 点 💬       ⇒ 卡片就地展开（data-open=1）且 URL **不变**
 *      · 点 ↗        ⇒ URL 变成 /work/{w}/card/{cid}
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5711";
const WID = process.argv[3] || "work_002";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9840 + Math.floor(Math.random() * 90);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "hitm_" + Date.now()), "about:blank",
], { stdio: "ignore" });

let tr = null;
for (let i = 0; i < 80 && !tr; i++) {
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }); if (r.ok) tr = await r.json(); } catch { }
  if (!tr) await sleep(200);
}
const ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((res) => { ws.onopen = res; });
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => (await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true })).result.result.value;

const fails = [];
const ok = (cond, label, detail) => { if (!cond) fails.push(label + (detail ? " :: " + detail : "")); return cond; };

async function loadPage() {
  await send("Page.navigate", { url: `${BASE}/work/${WID}` });
  for (let i = 0; i < 120; i++) { await sleep(50); if (Number(await ev(`document.querySelectorAll('.card-node').length`)) > 0) break; }
  await sleep(1000);
  // 兜底：可见集里若没有"带子卡的卡"（有些工程的默认视图就是这样，比如 300 卡那棵），
  // 行为矩阵就没有样本 ⇒ 那样报出来的"失败"是**前提不具备**，不是产品缺陷。
  // ⇒ 先"全部展开"把它变成有样本。
  const hasBadge = await ev(`!![...document.querySelectorAll('.card-node')].find(x => x.querySelector('.kid-badge'))`);
  if (!hasBadge) {
    await ev(`(() => { const b = document.getElementById('expandAllBtn'); if (b) b.click(); return 1; })()`);
    await sleep(1500);
  }
}

// ══════════════════════════════════════════════════════════════════════
// A. 命中矩阵
// ══════════════════════════════════════════════════════════════════════
await loadPage();
const hitRows = await ev(`(() => {
  const owner = (el) => { const c = el && el.closest ? el.closest('.card-node') : null; return c ? c.dataset.id : '(none)'; };
  const kind = (el) => {
    if (!el) return '(none)';
    if (el.classList.contains('kid-badge')) return 'kid-badge';
    if (el.classList.contains('cv-open')) return 'cv-open';
    if (el.classList.contains('cv-chat')) return 'cv-chat';
    if (el.classList.contains('tname')) return 'tname';
    if (el.classList.contains('card-node')) return 'card';
    return el.className || el.tagName;
  };
  const at = (x, y) => { const e = document.elementFromPoint(x, y); return { k: kind(e), own: owner(e) }; };
  const rows = [];
  for (const node of document.querySelectorAll('.card-node')) {
    const t = node.querySelector('.tname'), b = node.querySelector('.kid-badge');
    const o = node.querySelector('.cv-open'), c = node.querySelector('.cv-chat');
    if (!t) continue;
    const tr = t.getBoundingClientRect(), nr = node.getBoundingClientRect();
    const r = { id: node.dataset.id, pts: {} };
    // ⚠ "点标题文字"的探针必须落在**文字本身**上，不能用"标题盒左边缘 + 12"：
    //   根卡的标题是**居中**的（style.css .card-node.is-root{text-align:center}），
    //   标题盒比文字宽得多 ⇒ 盒左+12 落在**空白**上（甚至正好落在 💬 上）⇒ 假红。
    //   实测：work_002 的根卡因此误报"点标题命中的是 cv-chat"。
    //   真实文字矩形用 Range 量（selectNodeContents）。
    let tx = tr;
    try {
      const rg = document.createRange();
      rg.selectNodeContents(t);
      const rr = rg.getBoundingClientRect();
      if (rr.width > 2) tx = rr;
    } catch (e) { /* 退回盒矩形 */ }
    r.pts.titleText = at(tx.x + Math.min(6, tx.width / 2), tx.y + tx.height / 2);
    r.pts.titleCenter = at(tr.x + tr.width / 2, tr.y + tr.height / 2);
    r.pts.cardBlank = at(nr.x + 8, nr.y + nr.height - 4);
    if (b) { const br = b.getBoundingClientRect(); r.pts.badge = at(br.x + br.width / 2, br.y + br.height / 2); }
    if (o) { const or = o.getBoundingClientRect(); r.pts.open = at(or.x + or.width / 2, or.y + or.height / 2); }
    if (c) { const cr = c.getBoundingClientRect(); r.pts.chat = at(cr.x + cr.width / 2, cr.y + cr.height / 2); }
    rows.push(r);
  }
  return rows;
})()`);

for (const row of hitRows) {
  ok(row.pts.titleText.own === row.id, `[命中] ${row.id} 点标题文字应命中本卡`, JSON.stringify(row.pts.titleText));
  ok(row.pts.titleText.k === "tname" || row.pts.titleText.k === "card",
    `[命中] ${row.id} 点标题文字应命中标题/卡面`, JSON.stringify(row.pts.titleText));
  ok(row.pts.titleCenter.own === row.id, `[命中] ${row.id} 点标题盒中心应命中本卡`, JSON.stringify(row.pts.titleCenter));
  ok(row.pts.cardBlank.own === row.id, `[命中] ${row.id} 点卡面空白应命中本卡`, JSON.stringify(row.pts.cardBlank));
  if (row.pts.badge) {
    ok(row.pts.badge.k === "kid-badge", `[命中] ${row.id} 点徽标应命中徽标`, JSON.stringify(row.pts.badge));
    ok(row.pts.badge.own === row.id, `[命中] ${row.id} 徽标应属于本卡`, JSON.stringify(row.pts.badge));
  }
  if (row.pts.open) {
    ok(row.pts.open.k === "cv-open", `[命中] ${row.id} 点↗应命中↗`, JSON.stringify(row.pts.open));
    ok(row.pts.open.own === row.id, `[命中] ${row.id} ↗应属于本卡`, JSON.stringify(row.pts.open));
  }
  if (row.pts.chat) {
    ok(row.pts.chat.k === "cv-chat", `[命中] ${row.id} 点💬应命中💬`, JSON.stringify(row.pts.chat));
    ok(row.pts.chat.own === row.id, `[命中] ${row.id} 💬应属于本卡`, JSON.stringify(row.pts.chat));
  }
}

// ══════════════════════════════════════════════════════════════════════
// B. 行为矩阵（真点真看结果）
//
// ⚠ 派发方式的分工（踩过坑）：
//   · 命中矩阵用 elementFromPoint —— 问"那个位置上**是谁**"（含命中测试），
//     这一层能抓到"伪元素把整张卡的点击吃掉"这类问题；
//   · 行为矩阵用**合成 click**（element.click()）—— 直接触发该元素的处理器。
//     CDP 的 Input.dispatchMouseEvent 在本机对画布卡不可靠（press/release 会派发
//     click，但有时处理器中途被打断），会给出**假的失败**。
//   两层互补：命中矩阵保证"点到的是谁"，行为矩阵保证"它的行为是什么"。
// ══════════════════════════════════════════════════════════════════════
const clickSel = (sel) => ev(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  if (!el) return { ok: false, why: 'not found' };
  el.click();
  return { ok: true };
})()`);

// B1. 点**有子卡**的卡的标题 ⇒ 必须进对话页（不是展开/折叠）
await loadPage();
const target = await ev(`(() => {
  const n = [...document.querySelectorAll('.card-node')].find(x => x.querySelector('.kid-badge'));
  if (!n) return null;
  return { id: n.dataset.id };
})()`);
if (target) {
  await clickSel(`.card-node[data-id="${target.id}"] .tname`);
  await sleep(900);
  const href = await ev(`location.pathname`);
  ok(href === `/work/${WID}/card/${target.id}`,
    `[行为] 点有子卡的卡的标题应进对话页（不是展开/折叠）`, `href=${href} card=${target.id}`);
} else {
  fails.push("[行为] 找不到有子卡的卡，无法验证点标题");
}

// B2. 点徽标 ⇒ 子卡可见数变化（展开/折叠），且 URL 不变
// ⚠ 目标必须**非视图根**：computeVisibleSet 里视图根的子卡是**无条件可见**的
//   （"视图根 + 它的直接子卡"恒在可见集里，与 expanded 无关）⇒ 折叠根卡本来
//   就不改可见集，拿它当样本会得到"9 → 9"的假失败。
await loadPage();
const badgeTarget = await ev(`(() => {
  const rootId = (document.querySelector('.card-node.is-root') || {}).dataset?.id;
  const n = [...document.querySelectorAll('.card-node')].find(x =>
    x.querySelector('.kid-badge') && x.dataset.id !== rootId);
  if (!n) return null;
  return { id: n.dataset.id, before: document.querySelectorAll('.card-node').length };
})()`);
if (badgeTarget) {
  await clickSel(`.card-node[data-id="${badgeTarget.id}"] .kid-badge`);
  await sleep(900);
  const href = await ev(`location.pathname`);
  const after = await ev(`document.querySelectorAll('.card-node').length`);
  ok(href === `/work/${WID}`, `[行为] 点徽标不应跳走`, `href=${href}`);
  ok(after !== badgeTarget.before, `[行为] 点徽标应展开/折叠子卡`, `${badgeTarget.before} → ${after}`);
} else {
  fails.push("[行为] 找不到非根的有子卡徽标，无法验证展开/折叠");
}

// B3. 点 💬 ⇒ 卡片就地展开（data-open=1）且 URL 不变
await loadPage();
const chatTarget = await ev(`(() => {
  const n = [...document.querySelectorAll('.card-node')].find(x => x.querySelector('.cv-chat'));
  if (!n) return null;
  return { id: n.dataset.id };
})()`);
if (chatTarget) {
  await clickSel(`.card-node[data-id="${chatTarget.id}"] .cv-chat`);
  await sleep(1500);
  const href = await ev(`location.pathname`);
  const opened = await ev(`(() => { const n = document.querySelector('.card-node[data-id="${chatTarget.id}"]'); return n ? n.dataset.open === '1' : false; })()`);
  ok(href === `/work/${WID}`, `[行为] 点💬不应跳走（就地展开）`, `href=${href}`);
  ok(opened === true, `[行为] 点💬应就地展开这张卡的对话`, `data-open=${opened}`);
} else {
  fails.push("[行为] 找不到💬按钮，无法验证就地展开");
}

// B4. 点 ↗ ⇒ 进对话页
await loadPage();
const openTarget = await ev(`(() => {
  const n = [...document.querySelectorAll('.card-node')].find(x => x.querySelector('.cv-open'));
  if (!n) return null;
  return { id: n.dataset.id };
})()`);
if (openTarget) {
  await clickSel(`.card-node[data-id="${openTarget.id}"] .cv-open`);
  await sleep(900);
  const href = await ev(`location.pathname`);
  ok(href === `/work/${WID}/card/${openTarget.id}`, `[行为] 点↗应进对话页`, `href=${href}`);
} else {
  fails.push("[行为] 找不到↗按钮，无法验证");
}

// B5. Shift+单击 ⇒ 多选（不跳走）
await loadPage();
const multiTarget = await ev(`(() => {
  const n = document.querySelector('.card-node');
  if (!n) return null;
  return { id: n.dataset.id };
})()`);
if (multiTarget) {
  await ev(`(() => {
    const el = document.querySelector('.card-node[data-id="${multiTarget.id}"] .tname');
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true }));
    return true;
  })()`);
  await sleep(700);
  const href = await ev(`location.pathname`);
  const selN = await ev(`document.querySelectorAll('.card-node.is-selected').length`);
  ok(href === `/work/${WID}`, `[行为] Shift+单击不应跳走`, `href=${href}`);
  ok(selN >= 1, `[行为] Shift+单击应选中`, `selected=${selN}`);
} else {
  fails.push("[行为] 找不到卡，无法验证 Shift 多选");
}

const out = { work: WID, hitRows: hitRows.length, failCount: fails.length, fails };
console.log(JSON.stringify(out, null, 1));
ws.close(); child.kill(); process.exit(fails.length ? 1 : 0);
