/* _dom_order.mjs —— 画布 DOM 顺序探针（CDP）
 *
 * 问题：画布上 `.card-node` 的**文档顺序**＝用户看到的层叠顺序，也是
 * "第一张卡"（document.querySelector('.card-node')）的来源。
 * 远程建卡（自动模式/模型建卡）会触发增量渲染——如果增量路径把已有节点
 * 重新 append 到 DOM 末尾，文档顺序就变了（第一张卡不再是根卡）。
 *
 * 判据：
 *   ① 建卡前后的前 5 个 card-node 的 data-id 顺序；
 *   ② 根卡（work_card_id）是否仍在第一位；
 *   ③ 节点总数是否守恒（建一张 ⇒ +1）。
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:5711";
const WID = process.argv[3] || "work_001";
const CHROME = process.env.CHROME_PATH ||
  path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const PORT = 9800 + Math.floor(Math.random() * 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu", "--no-sandbox",
  "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "cdpdom_" + Date.now()), "about:blank",
], { stdio: "ignore" });
let tr = null;
for (let i = 0; i < 80 && !tr; i++) {
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" }); if (r.ok) tr = await r.json(); } catch { }
  if (!tr) await sleep(200);
}
const ws = new WebSocket(tr.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
await new Promise((r) => { ws.onopen = r; });
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};

await send("Page.navigate", { url: `${BASE}/work/${WID}` });
for (let i = 0; i < 120; i++) { await sleep(50); if (Number(await ev(`document.querySelectorAll('.card-node').length`)) > 0) break; }
await sleep(1200);

const snap = () => ev(`(() => {
  const ns = [...document.querySelectorAll('.card-node')];
  return { n: ns.length, first5: ns.slice(0,5).map(x => x.dataset.id), ids: ns.map(x => x.dataset.id) };
})()`);

const before = await snap();
// 建一张子卡（模拟远程建卡：模型/自动模式建的卡）
const res = await fetch(`${BASE}/api/works/${WID}/cards`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    title: "DOM顺序探针卡", source: "用户直接创建",
    parent: before.ids[0] || "", card_type: "",
  }),
});
const created = await res.json().catch(() => ({}));
await sleep(2500);
const after = await snap();

const rootId = before.ids[0];
const moved = before.first5.join(",") !== after.first5.join(",");
console.log(JSON.stringify({
  work: WID,
  createStatus: res.status,
  createdCardId: created.card_id || created.meta?.card_id || "(未取到)",
  before: { n: before.n, first5: before.first5 },
  after: { n: after.n, first5: after.first5 },
  rootStillFirst: after.first5[0] === rootId,
  domOrderChanged: moved,
  // 根卡是否被移到了后面（真问题：层叠顺序变了、"第一张卡"变了）
  rootIndexBefore: before.ids.indexOf(rootId),
  rootIndexAfter: after.ids.indexOf(rootId),
}, null, 1));
ws.close(); child.kill(); process.exit(0);
