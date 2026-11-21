/* tests/chat_switch_regression.mjs —— 对话页**切卡行为**回归（2026-11-20 性能批次一）
 *
 * 用法：node tests/chat_switch_regression.mjs <baseUrl> <workId> <cardId>
 *
 * 覆盖方案「第一批 · 交付判据」里的行为项：
 *   K1  A→B→A 快切：内容不串卡（切回来还是自己那张卡的对话）
 *   K2  热切回**不重拉**完整时序（切回不重新打 /timeline）
 *   K3  热切回**保持滚动位置**（改前这条实际是 no-op：宿主读写的是
 *       #chatArea 那个 overflow:hidden 的画框，不是真正的滚动容器）
 *   K4  取卡失败**回滚**身份（打开失效的旧链接 ⇒ 地址栏回到项目，不留在打不开的卡上）
 *   K5  当前卡徽标**不再下载全项目模型覆盖表**（只下 /effective 窄口）
 *
 * 全程只读：只点导航里的卡、只读滚动位置，不点任何写按钮。
 */
import { spawn, spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "work_073";
const CID = process.argv[4] || "card_007";
const CHROME = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9700 + Math.floor(Math.random() * 200);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(id, name, ok, detail) {
  results.push({ id, name, ok: !!ok, detail: String(detail == null ? "" : detail) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${id}  ${name}${detail ? "  —— " + detail : ""}`);
}

/** 杀浏览器整棵树（Windows 上 child.kill() 只杀父进程 ⇒ 攒孤儿）。 */
function killTree(child) {
  try {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)],
        { stdio: "ignore", timeout: 15000 });
      return;
    }
  } catch (e) { /* 落到默认 */ }
  try { child.kill("SIGKILL"); } catch (e) { /* 已死 */ }
}

const profileDir = path.join(os.tmpdir(), "chatswitch_" + Date.now());
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
const reqs = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Network.requestWillBeSent") reqs.push({ url: m.params.request.url });
};
const send = (m, p = {}) => new Promise((res) => {
  const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params: p }));
});
const ev = async (x) => {
  const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise: true });
  return r.result && r.result.result ? r.result.result.value : undefined;
};
await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

async function waitFirstRow(timeoutMs = 90000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const n = await ev("document.querySelectorAll('.flow-view-column > *').length");
    if (n > 3) return true;
    await sleep(120);
  }
  return false;
}
/** 等左栏把卡行画出来（虚拟列表，渲染比对话晚一点）。 */
async function waitNavRows(timeoutMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const n = await ev("document.querySelectorAll('.nav-card[data-cid]').length");
    if (n > 1) return n;
    await sleep(150);
  }
  return 0;
}
const columnText = () => ev(`(() => { const c = document.querySelector('.flow-view-column'); return c ? c.textContent : ""; })()`);
const clickNavCard = (cid) => ev(`(() => {
  const rows = [...document.querySelectorAll('.nav-card')];
  const hit = rows.find(r => r.dataset && r.dataset.cid === ${JSON.stringify(cid)});
  if (!hit) return "no-row";
  hit.click();
  return "clicked";
})()`);
const timelineReqs = (cid) => reqs.filter((r) => r.url.includes(`/cards/${cid}/timeline`));

/* ══════════════ 开卡 ══════════════ */
await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });
const ok0 = await waitFirstRow();
check("K0", "冷开：首批对话行上屏", ok0, ok0 ? "" : "超时");
/* 等渲染落定再取基准文本（首批行挂载 ≠ 整屏渲染完；此时取会只拿到前缀）。 */
await sleep(2500);
const textA = await columnText();

/* K5：徽标的数据源 */
const modelReqs = reqs.filter((r) => r.url.includes("/api/settings/model-overrides/"));
const hasFullTable = modelReqs.some((r) => !r.url.includes("/effective"));
const hasNarrow = modelReqs.some((r) => r.url.includes("/effective"));
check("K5", "徽标不再下载全项目模型覆盖表", !hasFullTable && hasNarrow,
  `窄口=${hasNarrow} 全表=${hasFullTable} 请求=${modelReqs.length}`);

/* ══════════════ K1/K2/K3：切走再切回 ══════════════ */
const navRows = await waitNavRows();
const otherCid = await ev(`(() => {
  const rows = [...document.querySelectorAll('.nav-card[data-cid]')];
  return rows.map(r => r.dataset.cid).find(c => c && c !== ${JSON.stringify(CID)}) || "";
})()`);
if (!otherCid) {
  check("K1", "A→B→A 快切：内容不串卡", false, `左栏只找到 ${navRows} 张卡行`);
  check("K2", "热切回不重拉完整时序", false, "没有第二张卡可比");
  check("K3", "热切回保持滚动位置", false, "没有第二张卡可比");
} else {
  const tlBefore = timelineReqs(CID).length;
  await clickNavCard(otherCid);
  await sleep(3000);
  const textB = await columnText();

  await clickNavCard(CID);
  await sleep(3000);
  const textAfter = await columnText();
  const tlAfter = timelineReqs(CID).length;

  /* K1：不串卡 —— 切回来后的内容应与切走前一致，且**不是**对方卡的 */
  const sameAsBefore = !!(textA && textAfter && textA.slice(0, 800) === textAfter.slice(0, 800));
  const notOther = !(textB && textAfter && textB.slice(0, 400) === textAfter.slice(0, 400));
  check("K1", "A→B→A 快切：内容不串卡", sameAsBefore && notOther,
    `与切走前一致=${sameAsBefore} 不等于对方=${notOther}（对方卡=${otherCid}）`);

  /* K2：热切回不重拉完整时序（只数本卡的） */
  check("K2", "热切回不重拉完整时序", tlAfter === tlBefore,
    `本卡 /timeline 请求数 ${tlBefore} → ${tlAfter}`);

  /* K3：热切回保持滚动位置 */
  await ev(`(() => { const s = document.querySelector('.flow-view-scroll'); s.scrollTop = Math.max(0, s.scrollHeight - s.clientHeight - 300); return s.scrollTop; })()`);
  await sleep(500);
  const posBefore = await ev(`(() => { const s = document.querySelector('.flow-view-scroll'); return s ? s.scrollTop : -1; })()`);
  await clickNavCard(otherCid);
  await sleep(2500);
  await clickNavCard(CID);
  await sleep(2500);
  const posAfter = await ev(`(() => { const s = document.querySelector('.flow-view-scroll'); return s ? s.scrollTop : -1; })()`);
  const kept = posBefore > 0 && posAfter > 0 && Math.abs(posAfter - posBefore) < 60;
  check("K3", "热切回保持滚动位置", kept, `切走前 scrollTop=${posBefore} 切回后=${posAfter}`);
}

/* ══════════════ K4：取卡失败回滚（真路径：打开失效的旧链接）══ */
await send("Page.navigate", { url: `${BASE}/work/${WID}/card/card___nope__` });
await sleep(6000);
const pathAfter = await ev("location.pathname");
check("K4", "取卡失败回滚身份（地址栏不留在打不开的卡上）",
  !String(pathAfter).includes("card___nope__"),
  `打开失效卡后的路径 = ${pathAfter}`);

/* ══════════════ 收尾 ══════════════ */
ws.close();
killTree(child);
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { /* 临时目录 */ }

const bad = results.filter((r) => !r.ok);
console.log(`\n通过 ${results.length - bad.length}/${results.length}`);
process.exit(bad.length ? 1 : 0);
