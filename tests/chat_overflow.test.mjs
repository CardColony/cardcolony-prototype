/* tests/chat_overflow.test.mjs —— 对话页「文字超框」的**布局**网（[5715 · P18]）
 *
 * 跑法：node --test tests/chat_overflow.test.mjs
 *
 * 〔5715 · P18〕线上症状（取证于 5700 正在跑的 work_025/card_054）：
 *   工具调用那一行里，**工具名槽**被上游塞进了整条命令
 *   （`cd "/d/AI_station/trading-workspace/cardlo_p1_治理机制" && python …`），
 *   而 ui.css 给这一槽写了 `flex: none`（＝永不收缩）⇒ 该槽按 max-content 宽
 *   1953px ⇒ 768px 的消息列 scrollWidth 冲到 1994px ⇒
 *   **文字横着漏出框外**、整条对话出现横向滚动条。
 *
 * 两层网：
 *   A（永远跑）：CSS **契约**层 —— 钉住「工具名槽可收缩 ＋ 省略号」这条规则本身
 *                （改 CSS 的人一改就红）。
 *   B（行为级）：真实 Chrome 里量**布局** —— 超长工具名不得让消息列溢出。
 *                没装 Chrome 就 skip 并写明原因（不误红，也不假装跑过）。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { readFileSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

const ROOT = new URL("..", import.meta.url);
const UI_CSS = readFileSync(new URL("static/ui.css", ROOT), "utf8");
const STYLE_CSS = readFileSync(new URL("static/style.css", ROOT), "utf8");
const CARD_HTML = readFileSync(new URL("templates/card.html", ROOT), "utf8");

/* ================================================================ A · CSS 契约 */

/** 从 CSS 文本里取某选择器的声明体（取**最后**一条同选择器规则＝层叠结果）。
 *  注释先剥掉——否则注释里写「原来是 flex:none」会被当成真声明。 */
function decls(css, selector) {
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(css))) {
    const sel = m[1].replace(/\/\*[\s\S]*?\*\//g, "").trim();
    if (sel === selector) out.push(m[2].replace(/\/\*[\s\S]*?\*\//g, ""));
  }
  return out.join("\n");
}

test("契约：工具名槽不许再是 flex:none（那是「永不收缩」＝撑破消息列的根因）", () => {
  const body = decls(UI_CSS, "body.page-chat .step-row.tool-row .txt b");
  assert.ok(body, "ui.css 里应有 .tool-row .txt b 规则");
  assert.ok(!/flex\s*:\s*none/.test(body),
    `工具名槽不能是 flex:none（永不收缩）——那正是 1953px 撑破列的根因：${body}`);
  assert.match(body, /flex\s*:\s*0\s+1\s+auto/, "应当可收缩：flex: 0 1 auto");
  assert.match(body, /min-width\s*:\s*0/, "必须有 min-width:0（否则自动最小尺寸＝min-content）");
  assert.match(body, /text-overflow\s*:\s*ellipsis/, "长内容要用省略号截断");
  assert.match(body, /white-space\s*:\s*nowrap/, "工具名槽是单行");
});

test("契约：同一行的其它槽位也得能收缩（min-width:0），否则各自把行撑宽", () => {
  for (const sel of [
    "body.page-chat .step-row.tool-row .txt",
    "body.page-chat .step-row.tool-row .targs",
    "body.page-chat .step-row.tool-row .tool-sum",
  ]) {
    const body = decls(UI_CSS, sel);
    assert.ok(body, `ui.css 里应有 ${sel} 规则`);
    assert.match(body, /min-width\s*:\s*0/, `${sel} 必须有 min-width:0`);
  }
});

test("契约：改了 ui.css 就要升版本戳（浏览器吃旧缓存 ⇒ 改了一版等于没改）", () => {
  const m = CARD_HTML.match(/ui\.css\?v=(\d+)/);
  assert.ok(m, "card.html 应引 ui.css?v=N");
  assert.equal((CARD_HTML.match(/ui\.css\?v=\d+/g) || []).length, 1,
    "每个模块只能有一个 URL（不能同一文件引两次）");
  assert.ok(Number(m[1]) >= 9, `ui.css 改过了，戳必须升（当前 v=${m[1]}，应 ≥9）`);
});

/* ================================================================ B · 行为级（真浏览器） */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findChrome() {
  const cands = [
    path.join(process.env.ProgramFiles || "C:\\Program Files",
      "Google", "Chrome", "Application", "chrome.exe"),
    path.join(process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
      "Google", "Chrome", "Application", "chrome.exe"),
    path.join(process.env.LOCALAPPDATA || "", "Google", "Chrome", "Application", "chrome.exe"),
    path.join(process.env.ProgramFiles || "C:\\Program Files",
      "Microsoft", "Edge", "Application", "msedge.exe"),
    path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
  ];
  return cands.find((p) => p && existsSync(p)) || null;
}

/** 复刻 stream.js 的工具行结构（stream.js:557-561），把超长命令塞进「工具名」槽。 */
const TOOL_ROW_HTML = (long) => `
<div class="step-row tool-row running">
  <span class="ev">tool_call</span>
  <span class="txt"><i class="tool-ico"></i><b>${long}</b><span class="targs">command=${long}</span></span>
  <span class="sub"><span class="mk run">调用中</span></span>
</div>`;

const LONG = '`cd "/d/AI_station/trading-workspace/cardlo_p1_治理机制" && python P1-4_自身一致性验收.py --negative; echo "RC=$?"`';

test("行为：超长工具名不得撑破消息列（不得出现横向溢出／横向滚动条）",
  { skip: findChrome() ? false : "本机没有 Chrome/Edge（行为级网跑不了，CSS 契约网仍在守）" },
  async () => {
    const chrome = findChrome();
    const port = 9800 + Math.floor(Math.random() * 90);
    const child = spawn(chrome, [
      `--remote-debugging-port=${port}`, "--headless=new", "--disable-gpu", "--no-sandbox",
      "--hide-scrollbars", "--window-size=1440,900",
      `--user-data-dir=${path.join(os.tmpdir(), "ovf" + Date.now())}`, "about:blank",
    ], { stdio: "ignore" });
    let target = null;
    try {
      for (let i = 0; i < 100 && !target; i++) {
        try {
          const r = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
          if (r.ok) target = await r.json();
        } catch { /* 还没起来 */ }
        if (!target) await sleep(200);
      }
      assert.ok(target, "Chrome 起不来（CDP 无响应）");

      const ws = new WebSocket(target.webSocketDebuggerUrl);
      let id = 0;
      const pend = new Map();
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
        if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
      };
      await new Promise((r) => { ws.onopen = r; });
      const send = (method, params = {}) => new Promise((res) => {
        const i = ++id; pend.set(i, res);
        ws.send(JSON.stringify({ id: i, method, params }));
      });
      await send("Page.enable");
      await send("Runtime.enable");
      await send("Emulation.setDeviceMetricsOverride",
        { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

      // 真页面：body.page-chat + 真 CSS（层叠顺序与 card.html 一致：style → ui）
      const css = [STYLE_CSS, UI_CSS].join("\n");
      const html = `<!doctype html><html><head><style>${css}</style></head>
<body class="page-chat">
<div id="chatArea" class="chat">
  <div class="msgs"><div class="flow-view-root"><div class="flow-view-scroll">
    <div class="flow-view-column">
      ${TOOL_ROW_HTML(LONG)}
      ${TOOL_ROW_HTML(LONG)}
      <div class="step-row tool-row ok has-detail">
        <span class="ev">tool_result</span>
        <span class="txt"><i class="tool-ico"></i><b>Bash</b><span class="targs">command=ls</span></span>
        <span class="sub"></span>
        <div class="tool-sum">status=completed round=1</div>
      </div>
      <div class="step-row tool-row open ok has-detail">
        <span class="ev">tool_result</span>
        <span class="txt"><i class="tool-ico"></i><b>Bash</b><span class="targs">command=cat</span></span>
        <span class="sub"></span>
        <div class="tool-body">${LONG}
${LONG}</div>
      </div>
    </div>
  </div></div></div>
</div></body></html>`;
      // 注入（不依赖 setDocumentContent：它在新版 headless 上偶发不生效）
      const inj = await send("Runtime.evaluate", {
        returnByValue: true,
        expression: `(() => { document.open(); document.write(${JSON.stringify(html)}); document.close(); return 1; })()`,
      });
      assert.ok(inj.result && inj.result.result && inj.result.result.value === 1,
        `页面注入失败: ${JSON.stringify(inj).slice(0, 400)}`);
      await sleep(600);

      const r = await send("Runtime.evaluate", {
        returnByValue: true,
        expression: `(() => {
          const col = document.querySelector(".flow-view-column");
          const b = document.querySelector(".step-row.tool-row .txt b");
          const sum = document.querySelector(".tool-sum");
          if (!col || !b || !sum) return JSON.stringify({ error: "缺元素", col: !!col, b: !!b, sum: !!sum });
          const doc = document.documentElement;
          const rect = (e) => { const x = e.getBoundingClientRect(); return { w: Math.round(x.width), right: Math.round(x.right) }; };
          const colR = col.getBoundingClientRect();
          const offenders = [];
          col.querySelectorAll("*").forEach((e) => {
            const r = e.getBoundingClientRect();
            if (r.width && r.right > colR.right + 1) {
              offenders.push({
                sel: e.tagName + "." + String(e.className || "").trim().split(/\\s+/).join("."),
                w: Math.round(r.width), right: Math.round(r.right), overBy: Math.round(r.right - colR.right),
                ml: getComputedStyle(e).marginLeft,
              });
            }
          });
          return JSON.stringify({
            colScrollW: col.scrollWidth, colClientW: col.clientWidth,
            colOverflow: col.scrollWidth - col.clientWidth,
            docOverflow: doc.scrollWidth - doc.clientWidth,
            bRect: rect(b), bScrollW: b.scrollWidth,
            sumRect: rect(sum), sumScrollW: sum.scrollWidth,
            colRect: rect(col),
            offenders: offenders.slice(0, 8),
          });
        })()`,
      });
      const raw = r.result && r.result.result && r.result.result.value;
      assert.ok(raw, `量布局失败: ${JSON.stringify(r).slice(0, 500)}`);
      const m = JSON.parse(raw);
      assert.ok(!m.error, `量布局失败: ${m.error}`);
      assert.ok(m.colOverflow <= 1,
        `消息列不得横向溢出（实测溢出 ${m.colOverflow}px：scrollW=${m.colScrollW} clientW=${m.colClientW}；越界元素=${JSON.stringify(m.offenders)}）`);
      assert.ok(m.docOverflow <= 1,
        `整页不得出现横向滚动条（实测 ${m.docOverflow}px）`);
      assert.ok(m.bRect.w <= m.colRect.w + 1,
        `工具名槽宽度 ${m.bRect.w} 不得超过消息列 ${m.colRect.w}`);
      assert.ok(m.sumRect.w <= m.colRect.w + 1,
        `结果摘要宽度 ${m.sumRect.w} 不得超过消息列 ${m.colRect.w}`);
      ws.close();
    } finally {
      child.kill();
    }
  });
