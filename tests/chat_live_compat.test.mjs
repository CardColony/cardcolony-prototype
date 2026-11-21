/* tests/chat_live_compat.test.mjs —— **现网响应 × 新前端**的兼容复验
 * （2026-11-20 返工项 #4）
 *
 * 用法：node tests/chat_live_compat.test.mjs <baseUrl> <workId> <cardId>
 *
 * 为什么要有这条：
 *   现网 5700 跑的是**旧后端**（窄投影没有 body_keys、/effective 没有 global_model），
 *   而**新前端**已经读这两个字段 ⇒ 缺字段会影响：
 *     ① 起草按钮的文案（「需求确定」↔「定论确认」）；
 *     ② 模型面板的继承链显示（「默认（当前 X）」里的 X）。
 *   ⇒ 用例直接对**现网响应**验这两处，确保「前端不挑后端版本」。
 *
 * 全程只读：只开卡、只展开右栏、只开模型选择器，不点任何写按钮。
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5700";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9450 + Math.floor(Math.random() * 120);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(id, name, ok, detail) {
  results.push({ id, name, ok: !!ok, detail: String(detail == null ? "" : detail) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${id}  ${name}${detail ? "  —— " + detail : ""}`);
}
function killTree(child) {
  try {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore", timeout: 15000 });
      return;
    }
  } catch (e) { /* 默认 */ }
  try { child.kill("SIGKILL"); } catch (e) { /* 已死 */ }
}

/* ── 先看这个后端给了什么字段（事实，不是假设）──────────────── */
const cardResp = await (await fetch(`${BASE}/api/works/${WID}/cards/${CID}?projection=meta`)).json();
const effResp = await (await fetch(`${BASE}/api/settings/model-overrides/${WID}/effective?card_id=${CID}`)).json();
console.log(`卡片窄口：键=${Object.keys(cardResp).sort().join(",")}   有 body_keys=${"body_keys" in cardResp}`);
console.log(`模型窄口：键=${Object.keys(effResp).sort().join(",")}   有 global_model=${"global_model" in effResp}`);

/* ── 浏览器 ─────────────────────────────────────────────────── */
const profileDir = path.join(os.tmpdir(), "livecompat_" + Date.now());
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
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
const send = (m, p = {}) => new Promise((res) => {
  const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });

// 等首批对话行
let up = false;
for (let i = 0; i < 600; i++) {
  await sleep(100);
  const n = await ev("document.querySelectorAll('.flow-view-column > *').length");
  if (n > 3) { up = true; break; }
}
check("L0", "卡能打开（首批对话行上屏）", up);

/* ① 起草按钮文案：card_007 的 body 里有 requirements ⇒ 应显示「需求确定」 */
await sleep(2000);
const draftLabel = await ev(`(() => {
  const b = document.getElementById("draftBtn");
  return b ? b.textContent.trim() : "";
})()`);
check("L1", "起草按钮文案正确（第三块在 ⇒ 「需求确定」）",
  draftLabel === "需求确定", `实际文案="${draftLabel}"`);

/* ② 模型面板的继承链显示：开面板 ⇒ 「默认（当前 X）」里的 X 不能是「（未配置）」 */
const panelInfo = await ev(`(() => {
  const mount = document.getElementById("modelBadgeMount");
  if (!mount) return null;
  const btn = mount.querySelector('[data-mp="open"]');
  if (!btn) return null;
  btn.click();
  return true;
})()`);
await sleep(2500);
const panelText = await ev(`(() => {
  const p = document.querySelector(".model-panel");
  return p ? (p.innerText || "") : "";
})()`);
const inheritRow = String(panelText || "").split("\n").find((l) => l.includes("默认（当前")) || "";
check("L2", "模型面板的继承链显示正常（不是「（未配置）」）",
  !!inheritRow && !inheritRow.includes("（未配置）"), `该行="${inheritRow}"`);
check("L3", "模型面板列出了候选模型", /默认/.test(String(panelText || "")),
  `面板文本长度=${String(panelText || "").length}`);

ws.close(); killTree(child);
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { /* 临时目录 */ }
const bad = results.filter((r) => !r.ok);
console.log(`\n通过 ${results.length - bad.length}/${results.length}`);
process.exit(bad.length ? 1 : 0);
