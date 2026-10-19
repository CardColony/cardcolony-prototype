/* ============================================================================
 * 并行操作验收（accept_parallel.mjs）
 * ----------------------------------------------------------------------------
 * 验三条画布相对侧边栏列表的硬差异：
 *   ① 多选后能**一句话同时下达给 N 张卡**（列表里只能一张张点进去发）
 *   ② 下达后画布上出现「排队中」（串行执行下，只有 1 张在跑，其余没有进度记录）
 *   ③ 两张卡能**并排对比**（列表里只能来回点、靠记忆对照）
 *
 * 不真跑模型：注入 fetch 包一层，只拦 POST .../messages 并记录，返回 200。
 * 这样验的是前端链路（选择→操作条→弹窗→提交→画布状态），零模型调用。
 * ========================================================================== */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const BASE = "http://127.0.0.1:5707";
const WID = "real_work_002";
const PORT = 9972;
const CHROME = path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(CHROME, [
  "--remote-debugging-port=" + PORT, "--headless=new", "--disable-gpu",
  "--no-sandbox", "--hide-scrollbars", "--window-size=1600,1000",
  "--user-data-dir=" + path.join(os.tmpdir(), "par" + Date.now()), "about:blank",
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

const MOCK = `
(() => {
  const orig = window.fetch;
  window.__sent = [];
  window.fetch = async function (url, opt) {
    const u = String(url);
    if (opt && String(opt.method || "").toUpperCase() === "POST" && /\\/cards\\/[^/]+\\/messages$/.test(u)) {
      window.__sent.push({ url: u, body: opt.body });
      return new Response(JSON.stringify({ ok: true, accepted: true }), { status: 200, headers: { "Content-Type": "application/json" } });
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

// ── 选中两张可见卡 ────────────────────────────────────────────────────────
const picked = await ev(`(() => {
  const ns = [...document.querySelectorAll('.card-node')].filter(n => n.offsetParent);
  if (ns.length < 2) return [];
  ns[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
  ns[1].dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));
  return ns.slice(0, 2).map(n => n.dataset.id);
})()`);
await sleep(600);
check("选中两张卡", (picked || []).length === 2, JSON.stringify(picked));

const bar = await ev(`(() => {
  const b = document.querySelector('.cv-selbar');
  return b ? { text: b.textContent.replace(/\\s+/g, ' ').trim(), acts: [...b.querySelectorAll('.cv-selbar-btn')].map(x => x.dataset.act) } : null;
})()`);
check("操作条出现「下指令」", !!bar && (bar.acts || []).includes("command"), JSON.stringify(bar));
check("选 2 张时出现「对比」", !!bar && (bar.acts || []).includes("cmp"), JSON.stringify(bar?.acts));

const shotBar = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync("_shots/b2_selbar.png", Buffer.from(shotBar.result.data, "base64"));

// ── 批量下指令 ────────────────────────────────────────────────────────────
await ev(`document.querySelector('.cv-selbar-btn[data-act="command"]').click()`);
await sleep(400);
const dlg = await ev(`(() => {
  const m = document.querySelector('.cv-modal-mask');
  return m ? { title: m.querySelector('.cv-modal-title')?.textContent, hasInput: !!m.querySelector('.cv-modal-input') } : null;
})()`);
check("批量指令弹窗带输入框", !!dlg?.hasInput, JSON.stringify(dlg));

await ev(`(() => {
  const inp = document.querySelector('.cv-modal-input');
  inp.value = '请各自给出这一步的结论';
  document.querySelector('.cv-modal-mask [data-role="ok"]').click();
})()`);
await sleep(1200);

const sent = await ev(`(() => ({
  n: (window.__sent || []).length,
  urls: (window.__sent || []).map(x => x.url),
  body: (window.__sent || [])[0]?.body || ""
}))()`);
check("两条指令都发到了后端（各自一张卡）", sent?.n === 2, JSON.stringify(sent));
check("指令正文正确", /这一步的结论/.test(sent?.body || ""), sent?.body);

const queued = await ev(`(() => {
  const ns = [...document.querySelectorAll('.card-node[data-live="queued"]')];
  return { n: ns.length, texts: ns.map(n => (n.querySelector('.cv-msgline')?.textContent || '').trim()) };
})()`);
check("两张卡标成「排队中」", queued?.n === 2, JSON.stringify(queued));

const shotQ = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync("_shots/b2_queued.png", Buffer.from(shotQ.result.data, "base64"));

// ── 对比 ──────────────────────────────────────────────────────────────────
await ev(`document.querySelector('.cv-selbar-btn[data-act="cmp"]').click()`);
await sleep(2500);
const cmp = await ev(`(() => {
  const cols = [...document.querySelectorAll('.cmp-col')];
  return {
    n: cols.length,
    titles: cols.map(c => (c.querySelector('.cmp-title')?.textContent || '').trim()),
    bodyLens: cols.map(c => (c.querySelector('.cmp-body')?.textContent || '').trim().length)
  };
})()`);
check("对比弹窗两列并排", cmp?.n === 2, JSON.stringify(cmp));
check("两列都有正文区（空卡显示「还没有正文」）", (cmp?.bodyLens || []).every((n) => n > 3) && (cmp?.bodyLens || []).some((n) => n > 20), JSON.stringify(cmp?.bodyLens));

const shotCmp = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync("_shots/b2_compare.png", Buffer.from(shotCmp.result.data, "base64"));

const pass = results.filter((r) => r.ok).length;
console.log(`\n=== 并行操作：${pass}/${results.length} 通过 ===`);
ws.close();
child.kill();
process.exit(pass === results.length ? 0 : 1);
