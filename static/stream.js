/* stream.js - [对话流渲染] 中间环节与逐字流**本身就是对话流里的一条条**。

   用户裁定（2026-10-08 12:52）：「气泡算哪门子的在对话流里，这他妈还不是独立区域？」
   —— 把全部过程装进**一个**容器（无论旁挂过程卡还是单条气泡）＝还是独立区域。
   故本条线**不存在任何容器**：

     · /stream 的每一个事件 → **各自**一条 `.step-row`，按到达顺序 append 进 chatArea
       （chatArea 的直接子元素；不包裹、不归拢、默认逐条可见）；
     · /tokens 的 content_chunk → 写进本轮那条「正在生成的**助手正文**」(.msg.assistant.live)；
       reasoning_chunk 作该条目内的弱化区。

   ★ 次序铁律（2026-10-08 13:3x，用户裁定「流式信息不能停在正式回复下面」）：
     **本轮的过程行永远在本轮的正式回复之前**。三处保证——
       1. 正在生成时：新环节行落下就把非空正文条目推到队尾（正文恒在最新行之下）；
       2. POST 结算：`workspace.appendMessage` **就地接管**正文条目为正式回复（行原地不动）；
       3. 刷新/换卡重放（`replayTrace`）：收流时把本轮过程行**整块移到该轮回复之前**。
     并且**每轮自成一组**（`beginGroup`）：新一条消息不会清掉、也不会删掉上一轮的行与回复。

   SSE 失败回调 onFail → 由 workspace.js 回退 createWaitingBubble 轮询（降级兜底；两态互斥）。
   事件纯展示层：只读渲染，不写存储、不影响 POST。
   日志（仅展示层、不参与判定、不设闸）：[flow-render] row / chunk / msg。 */
/* 迭代四 F2③：IIFE → ES module（module 作用域＝原闭包隔离）；esc 收编 esm/util.js。 */
import { API } from "./esm/api.js?v=11";
import { escapeHtml as esc } from "./esm/util.js?v=11";

let current = null;   // {wid,cid,es,esT,rows[],textEl,textBody,reasoning,reasoningText,fallbackUsed,lastSeq,lastTokenSeq}

  function fmtData(data) {
    try { return JSON.stringify(data, null, 1); } catch (e) { return String(data); }
  }

  function logInfo(msg) {
    try { if (window.console && console.info) console.info(msg); } catch (e) { /* 日志零阻断 */ }
  }

  function chatEl() {
    return document.getElementById("chatArea");
  }

  function scrollBottom() {
    const ca = chatEl();
    if (ca) ca.scrollTop = ca.scrollHeight;
  }

  function ensureRun(wid, cid) {
    if (current && current.wid === wid && current.cid === cid) return current;
    stop();
    current = {
      wid, cid, es: null, esT: null,
      rows: [], textEl: null, textBody: null,
      reasoning: null, reasoningText: null, seatThink: {}, fallbackUsed: false,
      lastSeq: 0, lastTokenSeq: 0,
    };
    return current;
  }

  /** 开新一组（新的一条用户消息＝新一轮）：
      把上一轮的过程行**封存**在对话流里（不再受本轮清理波及）、正文引用解绑。
      游标 lastSeq/lastTokenSeq 不清——它跨轮单调，用来跳过重放。 */
  function beginGroup(st) {
    st.rows = [];
    releaseText(st);
    st.fallbackUsed = false;
    st.toggle = null;
    st.seatThink = {};
  }

  /** 正式回复过后（收到 done/error）：给本组过程行加一个「收起/展开」开关。
      行仍是各自独立的条目，开关只是把本组临时隐起来——**不做容器、不归拢、不改次序**。
      默认展开（T2「折叠仅是用户可选减负、非默认隐藏」）。 */
  function addToggle(st) {
    const ca = chatEl();
    if (!ca || !st.rows.length || st.toggle) return;
    const group = st.rows.slice();          // 闭包持有本组行；之后 beginGroup 封存也不影响
    const t = document.createElement("div");
    t.className = "step-toggle";
    let open = true;
    const paint = () => { t.textContent = (open ? "▴ 收起过程（" : "▾ 展开过程（") + group.length + " 步）"; };
    paint();
    t.onclick = () => {
      open = !open;
      for (const r of group) r.style.display = open ? "" : "none";
      paint();
      scrollBottom();
    };
    ca.appendChild(t);
    st.rows.push(t);
    st.toggle = t;
    if (st.textEl && st.textBody && st.textBody.textContent) ca.appendChild(st.textEl);  // 正文仍压队尾
  }

  /* ---------- 中间环节：每条自己就是对话流里的一条（无容器） ---------- */

  function addRow(st, type, html, detail, cls) {
    const ca = chatEl();
    if (!ca) return null;
    const row = document.createElement("div");
    row.className = "step-row" + (cls ? " " + cls : "");
    row.innerHTML = html;
    if (detail) {
      const det = document.createElement("pre");
      det.className = "step-detail";
      det.textContent = detail;
      row.appendChild(det);
      row.classList.add("has-detail");
      row.onclick = (e) => {
        if (e.target.tagName === "PRE") return;
        det.style.display = det.style.display === "block" ? "none" : "block";
      };
    }
    ca.appendChild(row);
    st.rows.push(row);
    // 正文条目（非空）恒压在队尾：新环节行落下来时把它推到最下 ⇒ 行在上、回复在下
    if (st.textEl && st.textBody && st.textBody.textContent) ca.appendChild(st.textEl);
    logInfo("[flow-render] row(" + type + ", " + (cls === "fail" ? "false" : "true") + ")");
    scrollBottom();
    return row;
  }

  /** 只清本组的过程行（上一轮的行已被 beginGroup 封存，不受影响）。 */
  function clearRows(st) {
    for (const r of st.rows) { try { r.remove(); } catch (e) { /* noop */ } }
    st.rows = [];
  }

  /** 把本组过程行整块移到「本轮正式回复」之前（次序铁律第 3 条）。 */
  function anchorRows(st) {
    const ca = chatEl();
    if (!ca || !st.rows.length) return;
    const asst = ca.querySelectorAll(".msg.assistant");
    if (!asst.length) return;
    const anchor = asst[asst.length - 1];   // 本轮回复＝最后一条助手消息
    for (const r of st.rows) ca.insertBefore(r, anchor);
  }

  /* ---------- 逐字正文：助手消息本体（与用户消息同栏并列） ---------- */

  function textEntry(st) {
    if (st.textEl) return st;
    const ca = chatEl();
    if (!ca) return st;
    const el = document.createElement("div");
    el.className = "msg assistant live";
    el.innerHTML = '<span class="live-content"></span>';
    ca.appendChild(el);
    st.textEl = el;
    st.textBody = el.querySelector(".live-content");
    logInfo("[flow-render] msg created(assistant.live)");
    scrollBottom();
    return st;
  }

  function ensureReasoning(st) {
    if (st.reasoning) return st;
    textEntry(st);
    if (!st.textEl) return st;
    const r = document.createElement("div");
    r.className = "live-reasoning";
    r.innerHTML = '<span class="live-icon">🟡</span><span class="live-text"></span>';
    st.textEl.insertBefore(r, st.textBody);
    st.reasoning = r;
    st.reasoningText = r.querySelector(".live-text");
    return st;
  }

  function appendChunk(st, kind, text, seq, model) {
    if (!text) return;
    if (kind === "reasoning") {
      // [思考流支持层 2026-10-12] 带 model 的是**三席**思考（并发交织）→ 按席
      // 分桶的独立弱化行；不带 model 的是主模型思考 → 仍落正文条目内的弱化区。
      if (model) {
        const seatText = ensureSeatReasoning(st, model);
        if (seatText) {
          seatText.textContent += text;
          logInfo("[flow-render] seat-chunk(" + model + ", " + text.length + ")");
          scrollBottom();
          return;
        }
      }
      ensureReasoning(st).reasoningText.textContent += text;
    } else {
      textEntry(st);
      if (!st.textBody) return;
      st.textBody.textContent += text;
      const ca = chatEl();
      if (ca && st.textEl.parentNode === ca && ca.lastChild !== st.textEl) ca.appendChild(st.textEl);
    }
    logInfo("[flow-render] chunk(" + kind + ", " + text.length + ", " + (seq == null ? "-" : seq) + ")");
    scrollBottom();
  }

  /** 解绑正文引用。**只在还是 `.live` 时才动 DOM**——
      若它已被 workspace.appendMessage **就地接管**成正式回复（class 已变），绝不能删。 */
  function dropText(st) {
    if (st.textEl && st.textEl.classList && st.textEl.classList.contains("live")) {
      try { st.textEl.remove(); } catch (e) { /* noop */ }
    }
    releaseText(st);
  }

  function releaseText(st) {
    st.textEl = null; st.textBody = null; st.reasoning = null; st.reasoningText = null;
  }

  /** 轮界：正文条目只留「当前正在生成的那一轮」；并挪到队尾（最新在下）。 */
  function clearText(st) {
    if (!st.textEl) return;
    if (st.textBody) st.textBody.textContent = "";
    if (st.reasoningText) st.reasoningText.textContent = "";
    if (st.reasoning && !(st.reasoningText && st.reasoningText.textContent)) {
      try { st.reasoning.remove(); } catch (e) { /* noop */ }
      st.reasoning = null; st.reasoningText = null;
    }
    const ca = chatEl();
    if (ca && st.textEl.parentNode === ca && ca.lastChild !== st.textEl) ca.appendChild(st.textEl);
  }

  /** 三席思考块：**按模型分桶**的独立弱化行。
      三席是 asyncio.gather 并发的，思考增量必然交织——不分桶就是一团乱麻。
      刻意**不进**助手正文条目：三席观点既不是思考也不是助手回复正文，混进去
      会把审议过程当成正式回复的一部分显示。行入 st.rows：随本组一起收起/清理。 */
  function ensureSeatReasoning(st, model) {
    const ca = chatEl();
    if (!ca) return null;
    if (!st.seatThink) st.seatThink = {};
    if (st.seatThink[model]) return st.seatThink[model];
    const r = document.createElement("div");
    r.className = "live-reasoning seat-think";
    r.innerHTML = '<span class="live-icon">🧠</span><span class="live-model"></span><span class="live-text"></span>';
    r.querySelector(".live-model").textContent = model;
    ca.appendChild(r);
    st.rows.push(r);
    st.seatThink[model] = r.querySelector(".live-text");
    // 正文条目恒压队尾（次序铁律）：思考行落下时把正文推回最下
    if (st.textEl && st.textBody && st.textBody.textContent) ca.appendChild(st.textEl);
    scrollBottom();
    return st.seatThink[model];
  }

  function onTokenFrame(st, evt) {
    if (evt && typeof evt.seq === "number" && evt.seq > st.lastTokenSeq) st.lastTokenSeq = evt.seq;
    const type = (evt && evt.type) || "";
    const data = (evt && evt.data) || {};
    const text = data.text || "";
    const model = data.model || "";
    // 轮界标记帧（type 复用 run_start/round_start，data._boundary）。
    // [R-01] seq 空洞为正常态：按到达顺序处理，不因跳号中断/等待。
    if (type === "run_start" || type === "round_start") { clearText(st); return; }
    if (type === "content_chunk") appendChunk(st, "content", text, evt && evt.seq, model);
    else if (type === "reasoning_chunk") appendChunk(st, "reasoning", text, evt && evt.seq, model);
  }

  /* ---------- 事件流 → 每条一条（默认逐条可见） ---------- */

  function renderEvent(st, evt) {
    if (evt && typeof evt.seq === "number" && evt.seq > st.lastSeq) st.lastSeq = evt.seq;
    const d = evt.data || {};
    switch (evt.type) {
      case "run_start":
        // 本轮开场：清本组（若这是从 from=0 重放来的上一轮，会把上轮残留一并清掉）
        clearRows(st);
        dropText(st);
        addRow(st, evt.type, "🚀 开始处理");
        break;
      case "context": addRow(st, evt.type, `📐 装配上下文 <span class="step-dim">${esc(d.bytes || 0)}B · ${(d.regions || []).length} 区</span>`); break;
      case "triage_verdict":
        // [2026-10-11 两套机制拆除] 唯一判断行：机械预检命中（确定性动作）显
        // rule；模型判定显 verdict。旧「快车道/全链」渲染已整体退役。
        addRow(st, evt.type, `🧭 自判：${d.rule ? `确定性动作（${esc(d.rule)}）` : (d.verdict === "direct" ? "直答" : "拆解")} <span class="step-dim">${esc(d.reason || "")}</span>`, fmtData(d));
        break;
      case "plan_created":
        addRow(st, evt.type, `📋 拆解上板：${esc(d.name || "计划")}（${d.total || 0} 步）`, fmtData(d));
        break;
      case "step_advanced":
        addRow(st, evt.type, d.all_done
          ? `🏁 全部步骤完成，计划收口`
          : `✅ 步完成 → 下一步<span class="step-dim">${esc((d.prompt || "").slice(0, 60))}</span>`, fmtData(d));
        break;
      case "round_start":
        addRow(st, evt.type, `🧠 第 ${esc(d.round)} 轮 <span class="step-dim">${esc(d.phase || "")}</span>`);
        break;
      case "rescue": addRow(st, evt.type, `🩹 文本抢救 ${esc(d.count)} 条`, fmtData(d)); break;
      case "tool_call":
        st._toolT0 = performance.now();   // [F3③] 前端计时起点（不加后端字段）
        st._toolName = d.tool;
        addRow(st, evt.type, `🔧 ${esc(d.tool)} <span class="step-dim">${esc(d.target)}</span>`, fmtData(d));
        break;
      case "tool_result": {
        // [F3③] 工具条补「工具名 · 耗时 · 结果」——耗时前端计时（1.2s 形态）
        let dur = "";
        if (st._toolT0) {
          const ms = performance.now() - st._toolT0;
          dur = ` <span class="step-dim">${(ms / 1000).toFixed(1)}s</span>`;
          st._toolT0 = 0;
        }
        addRow(st, evt.type, `${d.ok ? "✓" : "✗"} ${esc(d.tool)}${dur} <span class="step-dim">${esc(d.target)}</span>`, fmtData(d), d.ok ? "ok" : "fail");
        break;
      }
        break;
      case "card_op": {
        const icon = { create_card: "✚", edit_card: "✎", delete_card: "🗑", read_card: "📖" }[d.op] || "🗂";
        addRow(st, evt.type, `${icon} ${esc(d.op)} ${esc(d.target)} ${d.ok ? "✓" : "✗"}`, fmtData(d), d.ok ? "ok" : "fail");
        break;
      }
      case "seats_enter": addRow(st, evt.type, `💬 三席进场 <span class="step-dim">工具结果 ${esc(d.tool_results)} 条</span>`); break;
      case "seat_start": addRow(st, evt.type, `💬 ${esc(d.model)} …`); break;
      case "seat_end":
        addRow(st, evt.type, `${d.ok ? "✓" : "✗"} ${esc(d.model)} <span class="step-dim">${d.ok ? ((d.chars || 0) + "字 · " + ((d.latency_ms || 0) / 1000).toFixed(1) + "s") : esc(d.error || "失败")}</span>`,
          d.ok ? null : fmtData(d), d.ok ? "ok" : "fail");
        break;
      case "deliver": addRow(st, evt.type, `📦 交付拼接 <span class="step-dim">${esc(d.branch)} · ${esc(d.bytes)}B</span>`); break;
      case "proposal":
        if (d.count > 0) addRow(st, evt.type, `✚ 建卡 ${esc(d.count)} 张 <span class="step-dim">${esc((d.cards || []).join("、"))}</span>`, fmtData(d));
        break;
      case "done":
        addRow(st, evt.type, `✓ 完成 <span class="step-dim">${esc(d.rounds)} 轮 · 模型 ${esc(d.model_calls)} 次</span>`, null, "ok");
        addToggle(st);   // 正式回复过后：过程可一键收起（默认展开，用户可选减负）
        finish();
        break;
      case "error":
        addRow(st, evt.type, `✗ 失败 <span class="step-dim">${esc(d.error)}</span>`, fmtData(d), "fail");
        addToggle(st);
        finish();
        break;
      default: break;
    }
  }

  function finish() {
    // 终止事件只在展示上收尾，**不关流**：from=0 重放里的上一轮 done 会误关刚连上的流。
    // 关流由 workspace 在 POST 结算后 stop()、或 replayTrace 的收流逻辑负责。
    scrollBottom();
  }

  function stopES() {
    if (current && current.es) { try { current.es.close(); } catch (e) { /* noop */ } current.es = null; }
    if (current && current.esT) { try { current.esT.close(); } catch (e) { /* noop */ } current.esT = null; }
  }

  function stop() {
    stopES();
    current = null;   // 已落的过程行与正文条目留在对话流里（POST 结算后由 workspace 接管正文条目）
  }

  /** 开始跟踪新一轮：两路 SSE；用 lastSeq/lastTokenSeq 跳过重放 ⇒ 不把上一轮的行重复渲染。
      onFail 在事件流连不上时回调一次（降级保底）。 */
  function start(wid, cid, onFail) {
    const st = ensureRun(wid, cid);
    stopES();
    beginGroup(st);
    try {
      const es = new EventSource(`/api/works/${wid}/cards/${cid}/stream?from=${st.lastSeq + 1}`);
      st.es = es;
      es.onmessage = (m) => {
        try { renderEvent(st, JSON.parse(m.data)); } catch (e) { /* 单帧坏不致命 */ }
      };
      es.onerror = () => {
        if (!st.fallbackUsed) {
          st.fallbackUsed = true;
          if (typeof onFail === "function") onFail();
        }
      };
    } catch (e) {
      if (!st.fallbackUsed && typeof onFail === "function") { st.fallbackUsed = true; onFail(); }
    }

    // 逐字流独立 SSE。刻意**不**接 onFail：连不上只少逐字显示。
    try {
      const esT = new EventSource(`/api/works/${wid}/cards/${cid}/tokens?from=${st.lastTokenSeq + 1}`);
      st.esT = esT;
      esT.onmessage = (m) => {
        try { onTokenFrame(st, JSON.parse(m.data)); } catch (e) { /* 单帧坏不致命 */ }
      };
      esT.onerror = () => { /* 静默 */ };
    } catch (e) { /* 同上 */ }
    return st;
  }

  /** 只重放**环节行**（不出正文）：把这张卡**上一条调用**的过程痕迹补回对话流。
      正文不重放——最终回复已由会话（renderChat）渲染，重放会多出一条重复气泡。
      事件只活在服务端内存 journal：同进程且该卡此后没再发过请求⇒重放得出；
      进程重启或又发过消息（run_start 清 journal）⇒ 自然没有了。
      收流时把行**整块移到该轮回复之前**（次序铁律）；收到终止事件即收流，空 journal 3s 兜底收流。 */
  function replayTrace(wid, cid) {
    const st = ensureRun(wid, cid);
    stopES();
    beginGroup(st);
    let closed = false;
    let es = null;
    const close = () => {
      if (closed) return;
      closed = true;
      // 已被后来的 start() 取代（页面刚打开就发消息）⇒ 这根流已不是本轮的，什么都别动：
      // 否则 3s 兜底计时器会把**新一轮刚连上的** /stream、/tokens 一起关掉（实测丢过整轮过程行）。
      if (es && st.es !== es) return;
      stopES();
      anchorRows(st);
    };
    try {
      es = new EventSource(`/api/works/${wid}/cards/${cid}/stream?from=0`);
      st.es = es;
      es.onmessage = (m) => {
        try {
          const evt = JSON.parse(m.data);
          renderEvent(st, evt);
          if (evt.type === "done" || evt.type === "error") close();
        } catch (e) { /* 单帧坏不致命 */ }
      };
      es.onerror = () => { close(); };   // 补历史连不上就静默收，绝不降级成等待气泡
      setTimeout(close, 3000);           // 展示层兜底：journal 为空时不悬着（非判定性、不设闸）
    } catch (e) { /* noop */ }
  }

export const EventFlow = { start, stop, replayTrace };
