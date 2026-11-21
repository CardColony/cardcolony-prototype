/* tests/chat_brief_check.mjs —— 窄投影（view=brief）浏览器交互验证
 * (2026-11-21 性能批次二任务一②)
 *
 * 验证三件事：
 *   ① 时序请求带 view=brief、回包条目带 _brief 契约位；
 *   ② 折叠态下首屏照常出图（工具行/思考行摘要可见）；
 *   ③ 展开触发 timeline/event 按需回取——tool-body/think-body/step-detail
 *      各自拿到完整内容；失败与撤销有可辨显示。
 *
 * 用法：node tests/chat_brief_check.mjs <baseUrl> <workId> <cardId>
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function killTree(child) {
  try {
    if (process.platform === "win32" && child.pid) {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)], { stdio: "ignore", timeout: 15000 });
      return;
    }
  } catch (e) { }
  try { child.kill("SIGKILL"); } catch (e) { }
}

const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = path.join(os.tmpdir(), "briefchk_" + Date.now());
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
  } catch { }
  if (!target) await sleep(200);
}
if (!target) { killTree(child); console.log("FATAL: Chrome 未起来"); process.exit(1); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => { ws.onopen = res; });
let id = 0; const pending = new Map();
const tlUrls = []; const detailUrls = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Network.requestWillBeSent") {
    const u = m.params.request.url;
    if (u.includes("/timeline/event")) detailUrls.push(u);
    else if (u.includes("/timeline")) tlUrls.push(u);
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
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: `${BASE}/work/${WID}/card/${CID}` });
// 服务端行索引冷建一次（首个 timeline_events 调用会建 750MB 文件的行索引，
// 几秒级——与页面加载并行预热，真实用户首次展开也会吃到这个成本，见回报）
const _warmIdx = fetch(
  `${BASE}/api/works/${WID}/cards/${CID}/timeline/event?seqs=1&session=${CID}`
).then(() => true).catch(() => false);

// 等首批行挂载（内容行——骨架/空态不算）
let ok = false;
for (let i = 0; i < 1200; i++) {
  await sleep(50);
  const n = await ev(`document.querySelectorAll(".step-row,.msg,.flow-user-row,.tool-row,.think,.seats").length`);
  if (n && n > 3) { ok = true; break; }
}
if (!ok) { console.log("FAIL: 首屏行未出现"); ws.close(); killTree(child); process.exit(1); }

const briefReq = tlUrls.find(u => u.includes("view=brief"));
console.log("① 时序请求:", briefReq ? "带 view=brief ✓" : "❌ 未见 view=brief →", tlUrls[0] || "(无)");

const scan = await ev(`JSON.stringify({
  toolRows: document.querySelectorAll(".tool-row").length,
  thinks: document.querySelectorAll(".think").length,
  stepRows: document.querySelectorAll(".step-row").length,
})`);
console.log("② 折叠态出图:", scan);
await _warmIdx;   // 行索引冷建完成后再进交互段（防首个懒取排队几秒）

// ③a 展开第一条工具行
const toolBefore = await ev(`(() => {
  const r = document.querySelector(".tool-row");
  if (!r) return null;
  r.click();
  return r.querySelector(".tool-body").textContent.slice(0, 60);
})()`);
await sleep(1800);
const toolAfter = await ev(`(() => {
  const r = document.querySelector(".tool-row");
  const b = r && r.querySelector(".tool-body");
  return b ? b.textContent.slice(0, 200) : null;
})()`);
console.log("③a 工具行展开: before=", JSON.stringify(toolBefore), "→ after=", JSON.stringify((toolAfter || "").slice(0, 80)));

// ③b 展开思考行
const thinkBefore = await ev(`(() => {
  const t = document.querySelector(".think .think-head");
  if (!t) return null;
  const body = document.querySelector(".think .think-body");
  const n0 = body ? body.textContent.length : -1;
  t.click();
  return n0;
})()`);
await sleep(1800);
const thinkAfter = await ev(`(() => {
  const body = document.querySelector(".think .think-body");
  return body ? body.textContent.length : -1;
})()`);
console.log("③b 思考行展开: body", thinkBefore, "→", thinkAfter, "字");

// ③c 折叠详情行（step-detail 懒取）
const detCheck = await ev(`(() => {
  const rows = [...document.querySelectorAll(".step-row.has-detail")];
  for (const r of rows) {
    const det = r.querySelector(".step-detail");
    if (det && !det._fullLoaded) { r.click(); return r.dataset.ev || true; }
  }
  return null;
})()`);
await sleep(1800);
const detAfter = await ev(`(() => {
  const rows = [...document.querySelectorAll(".step-row.has-detail")];
  for (const r of rows) {
    const det = r.querySelector(".step-detail");
    if (det && det._fullLoaded) return det.textContent.slice(0, 120);
  }
  return null;
})()`);
console.log("③c step-detail 懒取: type=", detCheck, "→", JSON.stringify((detAfter || "").slice(0, 90)));

// ── ④a 撤销段显示（Fetch 拦截构造 revoked 回包，不动数据）──────────
await send("Fetch.enable", { patterns: [{ urlPattern: "*/timeline/event*" }] });
let pausedOnce = null;
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Fetch.requestPaused") {
    const { requestId, request } = m.params;
    const mode = pausedOnce; pausedOnce = null;
    if (mode === "revoke") {
      // 把请求里的 seqs 全部回成 revoked——前端应显示「已撤销」而非旧预览
      const seqs = (new URL(request.url).searchParams.get("seqs") || "").split(",").filter(Boolean).map(Number);
      send("Fetch.fulfillRequest", { requestId, responseCode: 200,
        body: Buffer.from(JSON.stringify({ ok: true, items: [], revoked: seqs })).toString("base64") });
    } else if (mode === "fail") {
      send("Fetch.fulfillRequest", { requestId, responseCode: 500, body: Buffer.from("{}").toString("base64") });
    } else {
      send("Fetch.continueRequest", { requestId });   // 无模式也必放行，防悬挂
    }
  }
});

// 找一条带懒取的思考行（_briefLoad 存在）——先设拦截模式再点击
const think2 = await ev(`(() => {
  const els = [...document.querySelectorAll(".think")];
  for (const el of els) if (el._briefLoad && !el._fullLoaded) return true;
  return false;
})()`);
if (think2) {
  pausedOnce = "revoke";
  await ev(`(() => {
    const els = [...document.querySelectorAll(".think")];
    for (const el of els) if (el._briefLoad && !el._fullLoaded) {
      el._probeRevoked = true;                    // 打标：④a 只读这一行
      el.querySelector(".think-head").click(); return true;
    }
    return false;
  })()`);
  await sleep(1500);
  const revTxt = await ev(`(() => {
    const els = [...document.querySelectorAll(".think")];
    for (const el of els) if (el._probeRevoked)
      return el.querySelector(".think-body").textContent.slice(0, 60);
    return null;
  })()`);
  console.log("④a 撤销段显示:", JSON.stringify(revTxt),
    revTxt && revTxt.includes("已撤销") ? "✓" : "❌");
} else {
  console.log("④a 撤销段显示: 无可展开思考行（全已加载）— 跳过");
}

// ── ④b 详情请求失败 → 再展开重试成功 ─────────────────────────────
const think3 = await ev(`(() => {
  const els = [...document.querySelectorAll(".think")];
  for (const el of els) if (el._briefLoad && !el._fullLoaded) return true;
  return false;
})()`);
if (think3) {
  pausedOnce = "fail";       // 先设模式再点击（防 requestPaused 先于模式到达）
  await ev(`(() => {
    const els = [...document.querySelectorAll(".think")];
    for (const el of els) if (el._briefLoad && !el._fullLoaded) {
      el.querySelector(".think-head").click(); return true;
    }
    return false;
  })()`);
  await sleep(1200);
  const failTxt = await ev(`(() => {
    const els = [...document.querySelectorAll(".think")];
    for (const el of els) {
      const b = el.querySelector(".think-body");
      if (b && b.textContent.includes("失败")) return b.textContent.slice(0, 30);
    }
    return null;
  })()`);
  // 重试：折叠再展开（_fullLoaded 已被复位 ⇒ 再取一次，走真实后端）
  const retryTxt = await ev(`(() => {
    const els = [...document.querySelectorAll(".think")];
    for (const el of els) {
      const b = el.querySelector(".think-body");
      if (b && b.textContent.includes("失败")) {
        el.querySelector(".think-head").click();
        el.querySelector(".think-head").click();
        return true;
      }
    }
    return false;
  })()`);
  await sleep(1800);
  const okTxt = retryTxt && await ev(`(() => {
    const els = [...document.querySelectorAll(".think")];
    for (const el of els) if (el._fullLoaded) {
      const b = el.querySelector(".think-body");
      if (b && b.textContent.length > 50) return b.textContent.length;
    }
    return null;
  })()`);
  console.log("④b 失败重试:", JSON.stringify(failTxt), "→ 重试后", okTxt, "字",
    failTxt && okTxt ? "✓" : "❌");
} else {
  console.log("④b 失败重试: 无可展开思考行 — 跳过");
}
await send("Fetch.disable");

// ── ④c 详情请求归属发起会话（session 参数与 timeline 请求一致）─────
const tlSession = (() => {
  const u = tlUrls.find(x => x.includes("session="));
  return u ? new URL(u).searchParams.get("session") : null;
})();
const detSessions = detailUrls.map(u => new URL(u).searchParams.get("session"));
const sessOk = !detSessions.length || detSessions.every(s => s === tlSession);
console.log("④c 详情会话归属: timeline session=", tlSession,
  "detail=", JSON.stringify(detSessions), sessOk ? "✓" : "❌");

console.log("④ 详情请求数:", detailUrls.length, detailUrls.slice(0, 4).map(u => u.split("?")[1]));
ws.close(); killTree(child);
try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch (e) { }
process.exit(0);
