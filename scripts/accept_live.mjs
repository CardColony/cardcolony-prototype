/* ============================================================================
 * 实时活态验收（accept_live.mjs）
 * ----------------------------------------------------------------------------
 * 验的是**真链路**：3 秒轮询 /pulse → applyLiveStates → 卡面 DOM → 顶栏数字
 * → 活动抽屉。不靠"手工给节点加 class 看样式"（那样验不到轮询与恢复）。
 *
 * 手段：Page.addScriptToEvaluateOnNewDocument 在页面脚本之前包一层 fetch，
 * 只拦 /pulse 返回假数据；其余请求照常走真后端。这样：
 *   · 运行态链路整条被驱动（不需要真的调一次模型）
 *   · 活动抽屉用真实 card_history（假数据里带 recent）
 *   · 撤掉假数据后能验"跑完恢复成最后一条对话"
 *
 * 用法：node scripts/accept_live.mjs
 * ========================================================================== */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const BASE = "http://127.0.0.1:5707";
const WID = "real_work_002";
const LIVE_CID = "card_006";          // 这张卡在投影里一定存在（UI交互）
const PORT = 9971;
const CHROME = path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu",
  "--no-sandbox", "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "live" + Date.now()), "about:blank",
], { stdio: "ignore" });

let target = null;
for (let i = 0; i < 100 && !target; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    if (r.ok) target = await r.json();
  } catch {}
  if (!target) await sleep(200);
}
if (!target) { console.error("浏览器起不来"); process.exit(1); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
let msgId = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((res) => {
  const i = ++msgId;
  pending.set(i, res);
  ws.send(JSON.stringify({ id: i, method, params }));
});
await new Promise((r) => (ws.onopen = r));
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result?.result?.value;
};

// ── 注入：假 pulse（只拦这一个口）──────────────────────────────────────────
const MOCK = `
(() => {
  const orig = window.fetch;
  window.__liveMock = true;
  window.__liveRunning = ${JSON.stringify([{ card_id: LIVE_CID, stage: "tools", detail: "read_file message_runner.py", ts: Date.now() / 1000 }])};
  window.fetch = async function (url, opt) {
    const u = String(url);
    if (u.includes("/pulse")) {
      const running = window.__liveMock ? window.__liveRunning : [];
      return new Response(JSON.stringify({
        work_id: "${WID}",
        running,
        recent: [
          { card_id: "${LIVE_CID}", title: "UI交互", ts: new Date().toISOString(), detail: "由「UI交互」对话编辑正文" },
          { card_id: "card_002", title: "上下文记忆系统", ts: new Date(Date.now() - 3600e3).toISOString(), detail: "卡片创建" },
          { card_id: "card_005", title: "影子卡机制", ts: new Date(Date.now() - 86400e3).toISOString(), detail: "由「影子卡」对话编辑正文" }
        ],
        ts: Date.now() / 1000
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return orig.apply(this, arguments);
  };
})();
`;
await send("Page.addScriptToEvaluateOnNewDocument", { source: MOCK });

const results = [];
const check = (name, ok, note = "") => {
  results.push({ name, ok: !!ok, note });
  console.log(`${ok ? "  ✓" : "  ✗"} ${name}${note ? "  — " + note : ""}`);
};

await send("Page.navigate", { url: `${BASE}/work/${WID}` });
await sleep(6000);

// ① 运行态落到卡面
const live = await ev(`(() => {
  const n = document.querySelector('.card-node[data-id="${LIVE_CID}"]');
  if (!n) return { found: false };
  const line = n.querySelector('.cv-msgline');
  return {
    found: true,
    live: n.dataset.live || "",
    lineWho: line ? (line.dataset.who || "") : "",
    lineText: line ? line.textContent.trim() : "",
    borderColor: getComputedStyle(n).borderTopColor
  };
})()`);
check("运行中卡面标记 data-live", live?.live === "running", JSON.stringify(live));
check("卡面那一行换成「正在做什么」", live?.lineWho === "live" && /调用工具/.test(live?.lineText || ""), live?.lineText);

// ② 顶栏数字
const stat = await ev(`(() => {
  const el = document.querySelector('#projectStat [data-act="running"]');
  return el ? el.textContent.replace(/\\s+/g, " ").trim() : "";
})()`);
check("顶栏出现「1 运行中」", /1\s*运行中/.test(stat), stat);

// ③ 活动抽屉
await ev(`document.getElementById("activityBtn").click()`);
await sleep(400);
const drawer = await ev(`(() => {
  const d = document.getElementById("activityDrawer");
  const rows = [...document.querySelectorAll("#activityList .cv-act-row")];
  return {
    open: d.classList.contains("is-open"),
    rows: rows.length,
    liveRows: rows.filter(r => r.classList.contains("is-live")).length,
    first: rows[0] ? rows[0].textContent.replace(/\\s+/g, " ").trim().slice(0, 40) : "",
    dot: !!document.querySelector("#activityBtn .cv-act-dot")
  };
})()`);
check("动态抽屉能打开", drawer?.open === true);
check("抽屉里有运行中区块 + 活动条目", (drawer?.rows || 0) >= 3 && (drawer?.liveRows || 0) === 1, JSON.stringify(drawer));

const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync("_shots/b1_live.png", Buffer.from(shot.result.data, "base64"));

// ④ 点活动条目 → 选中那张卡
await ev(`(() => { const r = document.querySelectorAll("#activityList .cv-act-row")[1]; if (r) r.click(); })()`);
await sleep(500);
const afterClick = await ev(`(() => {
  const sel = document.querySelector('.card-node.is-selected');
  return { selected: sel ? sel.dataset.id : "", drawerOpen: document.getElementById("activityDrawer").classList.contains("is-open") };
})()`);
check("点活动条目会选中对应卡", !!afterClick?.selected, JSON.stringify(afterClick));

// ⑤ 关掉假运行态 → 卡面恢复
await ev(`window.__liveMock = false`);
await sleep(4000);
const restored = await ev(`(() => {
  const n = document.querySelector('.card-node[data-id="${LIVE_CID}"]');
  const line = n ? n.querySelector('.cv-msgline') : null;
  const statEl = document.querySelector('#projectStat [data-act="running"]');
  return {
    live: n ? (n.dataset.live || "") : "node-missing",
    lineWho: line ? (line.dataset.who || "") : "",
    stat: statEl ? "still-there" : "gone"
  };
})()`);
check("跑完：data-live 撤掉", restored?.live === "", JSON.stringify(restored));
check("跑完：那一行恢复成对话", restored?.lineWho !== "live", restored?.lineWho);
check("跑完：顶栏运行中消失", restored?.stat === "gone", restored?.stat);

const shot2 = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync("_shots/b1_after.png", Buffer.from(shot2.result.data, "base64"));

const pass = results.filter((r) => r.ok).length;
console.log(`\n=== 实时活态：${pass}/${results.length} 通过 ===`);
ws.close();
child.kill();
process.exit(pass === results.length ? 0 : 1);
