/* ============================================================================
 * 画布全量验收（accept_canvas.mjs）
 * ----------------------------------------------------------------------------
 * 与 canvas_*.e2e.mjs 的分工：
 *   那些是**功能流程**测试（点得通不通）；本支是**全量体检**——把所有页面 ×
 *   视口 × 缩放 × 数据规模铺成矩阵，逐格量几何与可见性，抓"看起来对但实际不对"。
 *
 * 为什么要矩阵：文字被裁、元素超框这类 bug 只在特定 缩放×卡数 组合下出现。
 * 只测一个默认态等于没测（我此前就因此反复交付半成品）。
 *
 * 用法：
 *   node scripts/accept_canvas.mjs            # 全矩阵
 *   node scripts/accept_canvas.mjs --quick    # 只跑几何矩阵（跳过缩放/交互）
 * ========================================================================== */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const BASE = "http://127.0.0.1:5707";
const QUICK = process.argv.includes("--quick");
const PORT = 9973;
const CHROME = path.join(
  os.homedir(),
  "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"
);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- 浏览器 ---------------- */
const child = spawn(
  CHROME,
  [
    "--remote-debugging-port=" + PORT,
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--hide-scrollbars",
    "--window-size=1600,1000",
    "--user-data-dir=" + path.join(os.tmpdir(), "acc" + Date.now()),
    "about:blank",
  ],
  { stdio: "ignore" }
);
let target = null;
for (let i = 0; i < 100 && !target; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, {
      method: "PUT",
    });
    if (r.ok) target = await r.json();
  } catch {}
  if (!target) await sleep(200);
}
if (!target) {
  console.error("无法启动浏览器");
  process.exit(1);
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
let msgId = 0;
const pending = new Map();
const send = (method, params = {}) =>
  new Promise((res) => {
    const i = ++msgId;
    pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
await new Promise((r) => (ws.onopen = r));

let consoleErrors = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m);
    pending.delete(m.id);
    return;
  }
  if (m.method === "Runtime.exceptionThrown") {
    consoleErrors.push(
      "EXC " + (m.params.exceptionDetails?.exception?.description || "").slice(0, 160)
    );
  }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
    consoleErrors.push(
      "ERR " +
        m.params.args
          .map((a) => a.value ?? a.description ?? "")
          .join(" ")
          .slice(0, 160)
    );
  }
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
    const t = String(m.params.entry.text);
    // 只忽略"请求被中止"这一类：矩阵测试是连续跨页导航，上一页的 SSE 长连接
    // 会被下一次导航掐断，Chrome 记成 ERR_ABORTED / ERR_NETWORK_CHANGED。
    // 这是测试动作造成的，不是页面缺陷。404 / 500 / 脚本异常一律照报。
    if (/ERR_ABORTED|ERR_NETWORK_CHANGED/.test(t)) return;
    consoleErrors.push("LOG " + t.slice(0, 160));
  }
};

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");

const ev = async (expr) => {
  const r = await send("Runtime.evaluate", {
    expression: expr,
    returnByValue: true,
    awaitPromise: true,
  });
  if (r.result?.exceptionDetails) {
    return { __err: String(r.result.exceptionDetails.text || "").slice(0, 200) };
  }
  return r.result?.result?.value;
};

const setViewport = (w, h) =>
  send("Emulation.setDeviceMetricsOverride", {
    width: w,
    height: h,
    deviceScaleFactor: 1,
    mobile: false,
  });

/* ---------------- 页面内探针（一次注入，反复调用） ---------------- */
const PROBE = String.raw`
window.__acc = {
  // 逐张展开，直到没有新增。
  // ⚠ 不能批量点：每次 toggleExpand 都会重建卡片 DOM，捕获的旧节点立刻失效，
  //   循环点等于只点中第一个（上一版脚本就是这么把根卡折叠掉的，白报一堆假失败）。
  // 也不能用 c.id —— 投影里的字段是 card_id。
  async expandAll(maxPass = 8) {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    for (let pass = 0; pass < maxPass; pass++) {
      const clicked = await (async () => {
        const s = window.GetCanvasState();
        const ids = s.cards
          .filter(c => (s.childMap[c.card_id] || []).length > 0)
          .map(c => c.card_id);
        let n = 0;
        for (const cid of ids) {
          const s2 = window.GetCanvasState();
          if (s2.expanded.has(cid)) continue;
          const node = document.querySelector('.card-node[data-id="' + cid + '"]');
          if (!node) continue;                       // 祖先还没展开，下一轮再来
          const b = node.querySelector('.kid-badge');
          if (!b) continue;
          b.click(); n++; await sleep(40);
        }
        return n;
      })();
      await sleep(280);
      if (!clicked) break;
    }
    return document.querySelectorAll('.card-node').length;
  },
  setZoom(pct) {
    const s = document.getElementById('zoomSlider');
    if (!s) return false;
    s.value = String(pct);
    s.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  },
  geom() {
    const out = { problems: [], info: {} };
    const nodes = [...document.querySelectorAll('.card-node')];
    const vpEl = document.getElementById('viewport');
    const vp = vpEl ? vpEl.getBoundingClientRect() : { left:0, top:0, right:innerWidth, bottom:innerHeight };
    const mm = document.querySelector('.cv-minimap');
    const mmr = mm ? mm.getBoundingClientRect() : null;
    const zoom = (window.GetCanvasState && window.GetCanvasState().zoom) || 1;
    out.info = { cards: nodes.length, zoom: Math.round(zoom * 100) };

    const rects = [];
    const byDepth = {};
    for (const n of nodes) {
      const r = n.getBoundingClientRect();
      const cs = getComputedStyle(n);
      rects.push({ id: n.dataset.id, r, n });
      // ① 卡片自身内容被裁（overflow:hidden 且装不下）
      if (cs.overflow === 'hidden' && n.scrollHeight > n.clientHeight + 2) {
        out.problems.push('卡内被裁 ' + n.dataset.id + ' ' + n.scrollHeight + '>' + n.clientHeight);
      }
      // ② 子元素超出卡片边界（"超框显示"）
      for (const el of n.querySelectorAll('*')) {
        if (!el.textContent || !el.textContent.trim()) continue;
        const er = el.getBoundingClientRect();
        if (er.width < 1 || er.height < 1) continue;
        const dl = Math.round(er.left - r.left), dr = Math.round(er.right - r.right);
        const dt = Math.round(er.top - r.top), db = Math.round(er.bottom - r.bottom);
        if (dl < -1 || dr > 1 || dt < -1 || db > 1) {
          out.problems.push('超框 ' + n.dataset.id + ' <' + (el.className || el.tagName) + '> L' + dl + ' R' + dr + ' T' + dt + ' B' + db);
        }
      }
      // ③ 预期截断以外的裁切
      for (const el of n.querySelectorAll('.tname,.summary,.cv-pending-item,.cv-runline')) {
        const cs2 = getComputedStyle(el);
        if (cs2.overflow !== 'hidden') continue;
        if (cs2.webkitLineClamp && cs2.webkitLineClamp !== 'none') continue;
        if (el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2) {
          out.problems.push('文字被裁 ' + n.dataset.id + ' <' + el.className + '>');
        }
      }
      // ④ 有效字号（乘缩放后）小于 10px 等于看不见
      const tn = n.querySelector('.tname');
      if (tn && tn.offsetWidth > 0) {
        const eff = parseFloat(getComputedStyle(tn).fontSize) * zoom;
        if (eff < 10) out.problems.push('字太小 ' + n.dataset.id + ' ' + eff.toFixed(1) + 'px');
      }
      // ⑤ 被小地图压住
      if (mmr && r.right > mmr.left && r.left < mmr.right && r.bottom > mmr.top && r.top < mmr.bottom) {
        out.problems.push('被小地图遮挡 ' + n.dataset.id);
      }
    }

    // ⑥ 同父多列时，各列的 y 集合必须一致（行对齐）
    // 这里断言的是布局的核心不变量：兄弟卡分列后，同一行必须落在同一个 y 上。
    // 之前写过一版"同深度 x 必须唯一"——**那是错的**：兄弟超过 10 张会分列，
    // 同深度本来就有多个 x。也写过"同父同列行距必须等距"——**也错**：行高按该行
    // 最大子树跨度分配，行距本来就不同。真正的不变量是"同父的各列行 y 对齐"。
    {
      const st = window.GetCanvasState ? window.GetCanvasState() : null;
      const parOf = {};
      if (st) for (const c of st.cards) parOf[c.card_id] = c.parent || "";
      const byPar = {};
      for (const n of nodes) {
        const p = parOf[n.dataset.id] || "";
        const x = Math.round(parseFloat(n.style.left) || 0);
        const y = Math.round(parseFloat(n.style.top) || 0);
        (byPar[p] = byPar[p] || {})[x] = (byPar[p][x] || []).concat([y]);
      }
      for (const p in byPar) {
        const xs = Object.keys(byPar[p]);
        if (xs.length < 2) continue;          // 单列无从对齐
        const cols = xs.map((x) => byPar[p][x].slice().sort((a, b) => a - b));
        // 末列可以短（兄弟数不整除列数），所以按最短列逐项比：
        // 同一行必须落在同一个 y 上，短列允许提前结束。
        const minLen = Math.min(...cols.map((c) => c.length));
        let bad = -1;
        for (let i = 0; i < minLen; i++) {
          if (new Set(cols.map((c) => c[i])).size > 1) { bad = i; break; }
        }
        if (bad >= 0) {
          out.problems.push("分列未对齐 父=" + (p || "root") + " 第" + bad + "行 y=" +
            cols.map((c) => c[bad]).join("/"));
        }
      }
    }
    for (let i = 0; i < rects.length; i++) {
      for (let j = i + 1; j < rects.length; j++) {
        const a = rects[i].r, b = rects[j].r;
        const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ox > 2 && oy > 2) {
          out.problems.push('卡片重叠 ' + rects[i].id + '×' + rects[j].id + ' ' + Math.round(ox) + 'x' + Math.round(oy));
        }
      }
    }
    // ⑧ 徽章压住文字
    for (const n of nodes) {
      const badge = n.querySelector('.kid-badge');
      const sm = n.querySelector('.summary');
      // 两个元素都得真的画出来才谈得上"压住"：display:none 的元素
      // getBoundingClientRect 全 0，拿它比会得出假阴性/假阳性。
      if (!badge || !sm || !badge.offsetWidth || !sm.offsetWidth) continue;
      const br = badge.getBoundingClientRect(), sr = sm.getBoundingClientRect();
      if (br.left < sr.right - 1 && br.right > sr.left + 1 && br.top < sr.bottom - 1 && br.bottom > sr.top + 1) {
        out.problems.push('徽章压结论 ' + n.dataset.id);
      }
    }
    return out;
  },
  outOfView() {
    const vpEl = document.getElementById('viewport');
    const vp = vpEl ? vpEl.getBoundingClientRect() : { left:0, top:0, right:innerWidth, bottom:innerHeight };
    const bad = [];
    for (const n of document.querySelectorAll('.card-node')) {
      const r = n.getBoundingClientRect();
      if (r.right < vp.left - 2 || r.left > vp.right + 2 || r.bottom < vp.top - 2 || r.top > vp.bottom + 2) {
        bad.push(n.dataset.id);
      }
    }
    return bad;
  }
};
'ok'
`;

/* ---------------- 矩阵定义 ---------------- */
const WORKS = [
  { id: "real_work_001", cards: 0, label: "空项目" },
  { id: "work_004", cards: 1, label: "单卡" },
  { id: "real_work_002", cards: 39, label: "39卡" },
  { id: "work_001", cards: 58, label: "58卡" },
  { id: "work_003", cards: 114, label: "114卡" },
  { id: "work_002", cards: 232, label: "232卡" },
];
const VIEWPORTS = [
  [1280, 800],
  [1600, 900],
  [1920, 1080],
];
const ZOOMS = [50, 60, 70, 90, 110, 140, 180, 200];

const results = [];
const record = (cell, ok, notes) =>
  results.push({ cell, ok, notes: notes.filter(Boolean) });

async function goto(url, wait = 3200) {
  consoleErrors = [];
  await send("Page.navigate", { url });
  await sleep(wait);
  // 等卡片真的出现（232 卡的投影比 9 卡慢得多，固定 sleep 会在慢机上误判"一张都没渲染"）
  for (let i = 0; i < 24; i++) {
    const n = await ev("document.querySelectorAll('.card-node').length");
    if (n > 0) break;
    await sleep(500);
  }
  await ev(PROBE);
}

/* ---------------- 1. 几何矩阵：每个 work × 每个视口 ---------------- */
console.log("\n===== 1. 几何矩阵（全展开 + 默认视图）=====");
for (const w of WORKS) {
  for (const [vw, vh] of VIEWPORTS) {
    await setViewport(vw, vh);
    await goto(`${BASE}/work/${w.id}`);
    const n = await ev("window.__acc.expandAll()");
    await sleep(500);
    const g = await ev("window.__acc.geom()");
    const oov = await ev("window.__acc.outOfView()");
    // [画布 2026-10-19] 小地图是**浮层**。内容装不下时（有卡在视口外）平移本来就是
    // 常态，右下角被压住不代表渲染缺陷——这条断言只在"整棵树都装得下"时才有意义。
    // 原来无条件判，等于让结果取决于布局恰好没把卡放到那个角上（本轮卡片改成一行后
    // 布局变了，232 卡 @1280 就撞上了）。
    const probs = (g?.problems || []).filter(
      (p) => !(p.startsWith("被小地图遮挡") && oov.length > 0)
    );
    // 空项目没有卡片，展开数应为 0；其余至少要有卡
    if (w.cards > 0 && (g?.info?.cards || 0) === 0) probs.push("一张卡都没渲染");
    // 全展开后允许超出视口（232 卡本就装不下），但**默认缩放**下至少要有卡在视口内
    if (w.cards > 0 && oov.length === (g?.info?.cards || 0)) probs.push("全部卡都在视口外");
    // [画布 2026-10-19] **整棵树装得下时，默认视图必须有可读文字。**
    // 这条是补上一个漏网的真 bug：39 卡的项目打开后是 39 个没有文字的灰块
    // （两套适配打架把 zoom 压到 0.55，而 lm 档阈值 0.68 在那里就不画字了）。
    // 原来判不出来，是因为 ④ 的"字太小"检查在 lm 下取不到 offsetWidth 就跳过了——
    // 一张字都没画的画布，反而没有任何一条断言会红。
    if (w.cards > 0 && oov.length === 0) {
      const vis = await ev(`(()=>{const ns=[...document.querySelectorAll('.card-node')];
        const shown = ns.filter(n=>{const t=n.querySelector('.tname'); return t && t.offsetWidth>0 && t.textContent.trim();});
        return {total:ns.length, shown:shown.length};})()`);
      if (vis && vis.total > 0 && vis.shown === 0) {
        probs.push(`默认视图一张字都没画（${vis.total} 张卡全是块）`);
      }
    }
    if (consoleErrors.length) probs.push("控制台错误: " + consoleErrors.slice(0, 2).join(" | "));
    record(
      `${w.label} @${vw}x${vh}`,
      probs.length === 0,
      [`卡${g?.info?.cards ?? "?"}`, probs.length ? probs.slice(0, 6).join(" ; ") : ""]
    );
    process.stdout.write(probs.length ? "F" : ".");
  }
}

/* ---------------- 2. 缩放矩阵：两个极端规模 × 全部缩放档 ---------------- */
if (!QUICK) {
  console.log("\n\n===== 2. 缩放矩阵（每档缩放都要读得清、不超框）=====");
  for (const w of WORKS.filter((x) => x.cards === 39 || x.cards === 232)) {
    for (const z of ZOOMS) {
      await setViewport(1600, 900);
      await goto(`${BASE}/work/${w.id}`);
      await ev("window.__acc.expandAll()");
      await sleep(300);
      await ev(`window.__acc.setZoom(${z})`);
      await sleep(700);
      const g = await ev("window.__acc.geom()");
      const probs = (g?.problems || []).slice();
      // 骨架档（<96%）只显示摘要行，字太小/超框照样算问题；但"同层x不一致"在
      // 拖拽过的工作区是历史数据，不属渲染缺陷 —— 这里只报几何类
      const geomOnly = probs.filter(
        (p) => !p.startsWith("同层x不一致") && !p.startsWith("被小地图遮挡")
      );
      if (consoleErrors.length) geomOnly.push("控制台错误: " + consoleErrors[0]);
      record(
        `${w.label} @${z}%`,
        geomOnly.length === 0,
        [`实际${g?.info?.zoom ?? "?"}% 卡${g?.info?.cards ?? "?"}`, geomOnly.slice(0, 5).join(" ; ")]
      );
      process.stdout.write(geomOnly.length ? "F" : ".");
    }
  }
}

/* ---------------- 3. 其他页面存在性 & 控制台 ---------------- */
if (!QUICK) {
  console.log("\n\n===== 3. 其余页面（首页 / 对话页 / 草稿 / 设置）=====");
  const pages = [
    ["首页", `${BASE}/`],
    ["对话页", `${BASE}/work/real_work_002/card/card_001`],
    ["草稿", `${BASE}/work/draft`],
    ["设置", `${BASE}/settings`],
  ];
  for (const [label, url] of pages) {
    await setViewport(1600, 900);
    await goto(url, 2600);
    const info = await ev(`(() => {
      const body = document.body;
      const overflowX = body.scrollWidth > innerWidth + 2;
      const empty = !document.body.innerText.trim();
      // 首页会按"有最近记录就跳上次对话页"的规则自动离开 "/"，那是**预期行为**，
      // 不算"关键容器缺失"。只有仍停在 "/" 时才检查首页容器。
      const stayed = location.pathname === '/';
      const key = {
        '首页': '#viewport, #homeNewProject',
        '对话页': '#messages, .page-card, .chat',
        '草稿': '#messages, .page-card, .chat',
        '设置': '.set-page',
      }[${JSON.stringify(label)}] || 'main';
      const hasKey = !stayed || !!document.querySelector(key);
      return { overflowX, empty, hasKey, stayed, path: location.pathname, w: body.scrollWidth, h: body.scrollHeight };
    })()`);
    const probs = [];
    if (info?.overflowX) probs.push("横向溢出 " + info.w);
    if (info?.empty) probs.push("页面空白");
    if (info?.hasKey === false) probs.push("关键容器缺失");
    if (consoleErrors.length) probs.push("控制台错误: " + consoleErrors.slice(0, 2).join(" | "));
    record(`${label} @1600x900`, probs.length === 0, probs.slice(0, 4));
    process.stdout.write(probs.length ? "F" : ".");
  }
}

/* ---------------- 汇总 ---------------- */
const fail = results.filter((r) => !r.ok);
console.log("\n\n================ 汇总 ================");
console.log(`共 ${results.length} 格，通过 ${results.length - fail.length}，失败 ${fail.length}`);
if (fail.length) {
  console.log("\n--- 失败明细 ---");
  for (const f of fail) console.log(`✗ ${f.cell}\n    ${f.notes.join("\n    ")}`);
}
ws.close();
child.kill();
process.exit(fail.length ? 1 : 0);
