/* stream.js - [对话流渲染] 中间环节与逐字流**本身就是对话流里的一条条**。

   用户裁定（2026-10-08 12:52）：「气泡算哪门子的在对话流里，这他妈还不是独立区域？」
   —— 把全部过程装进**一个**容器（无论旁挂过程卡还是单条气泡）＝还是独立区域。
   故本条线**不存在任何容器**：

     · /stream 的每一个事件 → **各自**一条 `.step-row`，按到达顺序 append 进 .msgs
       （统一内容列的直接子元素；不包裹、不归拢、默认逐条可见）；
     · /tokens 的 content_chunk → 写进本轮那条「正在生成的**助手正文**」(.msg.assistant.live)；
       reasoning_chunk 作该条目内的弱化区（主模型），或按席流入席卡（三席）。

   ★ 次序铁律（2026-10-08 13:3x，用户裁定「流式信息不能停在正式回复下面」）：
     **本轮的过程行永远在本轮的正式回复之前**。三处保证——
       1. 正在生成时：新环节行落下就把非空正文条目推到队尾（正文恒在最新行之下）；
       2. POST 结算：`workspace.appendMessage` **就地接管**正文条目为正式回复（行原地不动）；
       3. 刷新/换卡重放（`replayTrace`，源＝落盘事件流 `GET .../timeline`）：收尾把本轮过程行**整块移到该轮回复之前**。
     并且**每轮自成一组**（`beginGroup`）：新一条消息不会清掉、也不会删掉上一轮的行与回复。

   ★ [对话流全消息类型 2026-10-13] 行样式重做（按真实载荷，逐个 emit 调用点实读）：
     · 零图标——每行行首 emoji 全删（用户 00:16「去掉所有图标」）；
     · 语义标记（✓/✗）只落**行尾状态槽**，不做行首装饰（用户 08:39）；
     · 每行挂事件名徽标 `.ev` ＋ 说明 ＋ 真实字段名值 `.f`（不是照清单编的说明）；
     · 配色简化——除错误红外全灰阶（用户 00:07「不要五颜六色，简单一点」）；
     · 三席：`seat_start` 建卡（卡头＝模型名＋状态）、`reasoning_chunk(带 model)` 流入卡内
       单一滚动区、`seat_end` 输出正文入同一区＋状态转「已返回」，**不再单独成行**（用户 00:17）。

   SSE 失败回调 onFail → 由 workspace.js 回退 createWaitingBubble 轮询（降级兜底；两态互斥）。
   事件纯展示层：只读渲染，不写存储、不影响 POST。
   日志（仅展示层、不参与判定、不设闸）：[flow-render] row / chunk / msg。 */
/* 迭代四 F2③：IIFE → ES module（module 作用域＝原闭包隔离）；esc 收编 esm/util.js。 */
import { API } from "./esm/api.js?v=27";
import { escapeHtml as esc } from "./esm/util.js?v=27";
/* [边出边渲染 2026-10-14 · 用户令 ⑧] 流式期间也要渲染 md（原 textContent 累加＝
   全程看到 `**这样**`，用户判「发出来根本不支持 md，要过一会刷新才变」）。 */
import { renderMarkdown } from "./esm/md.js?v=27";

let current = null;   // {wid,cid,es,esT,rows[],textEl,textBody,reasoning,reasoningText,seatCards,seatsEl,fallbackUsed,lastSeq,lastTokenSeq}

  function fmtData(data) {
    try { return JSON.stringify(data, null, 1); } catch (e) { return String(data); }
  }

  function logInfo(msg) {
    try { if (window.console && console.info) console.info(msg); } catch (e) { /* 日志零阻断 */ }
  }

  function clip(s, n) {
    const t = String(s == null ? "" : s);
    return t.length > n ? t.slice(0, n) + "…" : t;
  }

  /** 真实字段名值（逐个 emit 调用点实读，不是照清单编的说明） */
  function fld(k, v) {
    if (v === undefined || v === null || v === "") return "";
    return ' <span class="f">· <span class="k">' + esc(k) + "</span>=" + esc(clip(v, 80)) + "</span>";
  }

  /** 行为类型（Ⅴ：config/behavior_registry.json）→ 中文显示名 */
  function btLabel(type) {
    return String(type) === "think" ? "思考" : "执行";
  }

  /** 卡间消息类型（Ⅰ：config/message_type_registry.json）→ 中文显示名 */
  const CARD_MSG_DISPLAY = { msg_ask: "询问", msg_answer: "应答", msg_notify: "通知" };

  function chatEl() {
    return document.getElementById("chatArea");
  }

  /* [对话区会话流样式 2026-10-12] 统一内容列：过程行/思考行/live 正文也进
     .msgs（与 workspace.ensureMsgs 同构，互不依赖）——整张卡片内所有条目
     共用一条列，谁都不出界。 */
  function msgsEl() {
    const ca = chatEl();
    if (!ca) return null;
    let m = ca.querySelector(":scope > .msgs");
    if (!m) {
      m = document.createElement("div");
      m.className = "msgs";
      ca.appendChild(m);
    }
    return m;
  }

  function scrollBottom() {
    const ca = chatEl();
    if (ca) ca.scrollTop = ca.scrollHeight;
  }

  /** 正文条目恒压队尾（次序铁律第 1 条）：新条目落下时把非空正文推回最下。 */
  function keepTextLast(st, box) {
    if (box && st.textEl && st.textBody && st.textBody.textContent &&
        st.textEl.parentNode === box && box.lastChild !== st.textEl) {
      box.appendChild(st.textEl);
    }
  }

  function ensureRun(wid, cid) {
    if (current && current.wid === wid && current.cid === cid) return current;
    stop();
    current = {
      wid, cid, es: null, esT: null,
      rows: [], textEl: null, textBody: null,
      reasoning: null, reasoningText: null, seatCards: {}, seatsEl: null,
      fallbackUsed: false, lastSeq: 0, lastTokenSeq: 0, stepIndex: 0,
      textRaw: "",      // [⑧] 流式累积原文（渲染用；textBody 里放的是渲染结果）
      _mdTimer: null,   // [⑧] 节流句柄
      _toolRow: null,   // [⑤] 当前工具行（结果到达时原地更新，不新增行）
    };
    return current;
  }

  /* [单一时序表 2026-10-14 · 用户令 ③] 消息渲染回填钩子。
     replayTrace 按 seq 顺序逐条渲染整条时序表时，消息类条目交回 workspace 渲染
     （stream.js 不复制 appendMessage 的接管/计数逻辑）。这是依赖注入——
     排序职责归 timeline，消息渲染职责仍归 workspace，两边各司其职。 */
  let _msgRenderer = null;
  function setMessageRenderer(fn) { _msgRenderer = fn; }

  /** 开新一组（新的一条用户消息＝新一轮）：
      把上一轮的过程行**封存**在对话流里（不再受本轮清理波及）、正文引用解绑。
      游标 lastSeq/lastTokenSeq 不清——它跨轮单调，用来跳过重放。 */
  function beginGroup(st) {
    st.rows = [];
    releaseText(st);
    st.fallbackUsed = false;
    st.toggle = null;
    // [⑦ 三席 2026-10-14 · 用户令] 清席卡时**必须清 DOM**——原来只清引用不清元素，
    // 刷新恢复再 start() 时会另建一个 .seats 容器，旧卡留在页面上、新卡建到别处，
    // 用户判「卡片内看不到三席的过程信息」（内容跑到另一个容器去了）。
    if (st.seatsEl) { try { st.seatsEl.remove(); } catch (e) { /* noop */ } }
    st.seatCards = {};
    st.seatsEl = null;
    st.stepIndex = 0;
    if (st._thinkTimer) { clearInterval(st._thinkTimer); st._thinkTimer = null; }   // [⑥]
    if (st.reasoning) { try { st.reasoning.remove(); } catch (e) { /* noop */ } }   // [⑥]
    st.reasoning = null; st.reasoningText = null;
    // [⑤] 上一轮未终结的工具行走秒定时器要掐掉（否则旧行继续走秒）
    if (st._toolRow) {
      const s = st._toolRow.querySelector(".sub .mk");
      if (s && s._timer) { clearInterval(s._timer); s._timer = null; }
    }
    st._toolRow = null;
    st._toolT0 = 0;
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
    const box = msgsEl();
    if (box) appendToBox(st, t);
    else if (ca) ca.appendChild(t);
    st.rows.push(t);
    st.toggle = t;
    keepTextLast(st, box);   // 正文仍压队尾
  }

  /* ---------- 中间环节：每条自己就是对话流里的一条（无容器） ---------- */

  /** [单一时序表 2026-10-14 · 用户令 ③] 归位插入：重放期间新行插到 `_insertAnchor`
      （＝下一条消息气泡）之前，而不是一律 append 到队尾。这样过程行落在**它所属那轮**
      的消息之间——原实现一律 append、收尾再整块搬到最后一轮回复前，导致"中间过程信息
      全塞到最后的文字里"（用户判「弄得乱七八糟，分不清正文从哪里开始」）。 */
  function appendToBox(st, el) {
    const box = msgsEl();
    if (!box) return;
    if (st._insertAnchor && st._insertAnchor.parentNode === box) {
      box.insertBefore(el, st._insertAnchor);
    } else {
      box.appendChild(el);
    }
  }

  /** 一条过程行：事件名徽标 ＋ 说明（含真实字段） ＋ 行尾状态槽。
      cls：ok/fail/err/void/content；detail：点开看的原始载荷。 */
  function addRow(st, type, txtHtml, subHtml, detail, cls) {
    const box = msgsEl();
    if (!box) return null;
    const row = document.createElement("div");
    row.className = "step-row" + (cls ? " " + cls : "");
    row.dataset.ev = type;
    row.innerHTML = '<span class="ev">' + esc(type) + "</span>" +
                    '<span class="txt">' + txtHtml + "</span>" +
                    (subHtml ? '<span class="sub">' + subHtml + "</span>" : "");
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
    appendToBox(st, row);
    st.rows.push(row);
    keepTextLast(st, box);
    logInfo("[flow-render] row(" + type + ", " + (cls === "fail" ? "false" : "true") + ")");
    scrollBottom();
    return row;
  }

  /** 缩进行（层级用 padding 表现，不加容器元素）。 */
  function addSub(st, type, txtHtml, subHtml, detail, cls, lv) {
    return addRow(st, type, txtHtml, subHtml, detail, (cls ? cls + " " : "") + (lv || "lv2"));
  }

  /** 计划步骤清单（plan_created 的真实 steps[] 载荷：id/prompt/criteria/deliverable/type）。 */
  function addPlanSteps(st, steps) {
    const box = msgsEl();
    if (!box || !steps.length) return;
    const wrap = document.createElement("div");
    wrap.className = "psteps";
    wrap.innerHTML = steps.map(s =>
      '<div class="pstep"><span class="bt">' + esc(btLabel(s.type)) + "</span>" +
      "<span>" + esc(s.id) + " · " + esc(clip(s.prompt, 70)) + "</span>" +
      '<span class="f">id=' + esc(s.id) + " criteria=" + esc(clip(s.criteria, 40)) + "</span></div>"
    ).join("");
    appendToBox(st, wrap);
    st.rows.push(wrap);
    keepTextLast(st, box);
  }

  /** 只清本组的过程行（上一轮的行已被 beginGroup 封存，不受影响）。 */
  function clearRows(st) {
    for (const r of st.rows) { try { r.remove(); } catch (e) { /* noop */ } }
    st.rows = [];
  }

  /** 把本组过程行整块移到「本轮正式回复」之前（次序铁律第 3 条）。
      [单一时序表 2026-10-14 · 用户令 ③] 实时流仍用本函数（本轮的行只属于本轮）；
      重放路径已改按 seq 归位插入（见 replayTrace），不再整块搬到最后一轮前面。 */
  function anchorRows(st) {
    const box = msgsEl();
    if (!box || !st.rows.length) return;
    const asst = box.querySelectorAll(".msg.assistant");
    if (!asst.length) return;
    const anchor = asst[asst.length - 1];   // 本轮回复＝最后一条助手消息
    for (const r of st.rows) box.insertBefore(r, anchor);
  }

  /* ---------- 逐字正文：助手消息本体（与用户消息同栏并列） ---------- */

  function textEntry(st) {
    if (st.textEl) return st;
    const box = msgsEl();
    if (!box) return st;
    const el = document.createElement("div");
    el.className = "msg assistant live";
    // [⑧] 流式期间就渲染 md（用户判「发出来根本不支持 md」）——容器按 .md 规范起，
    //     接管时 workspace.appendMessage 会用同样的 .md 结构替换（形态一致，不跳版）。
    el.innerHTML = '<div class="md live-content"></div>';
    box.appendChild(el);
    st.textEl = el;
    st.textBody = el.querySelector(".live-content");
    logInfo("[flow-render] msg created(assistant.live)");
    scrollBottom();
    return st;
  }

  /* [⑧ 边出边渲染 2026-10-14] 流式正文节流渲染：累积原文进 textRaw，最多每 100ms
     渲染一次（每字一渲染会反复重排，100ms 是人眼分辨不出的粒度），收尾 flush 终态。 */
  function scheduleMd(st) {
    if (st._mdTimer) return;
    st._mdTimer = setTimeout(() => {
      st._mdTimer = null;
      const el = st.textBody;
      if (!el) return;
      el.innerHTML = renderMarkdown(st.textRaw);
      keepTextLast(st, msgsEl());
      scrollBottom();
    }, 100);
  }

  function flushMd(st) {
    if (st._mdTimer) { clearTimeout(st._mdTimer); st._mdTimer = null; }
    if (st.textBody && st.textRaw) {
      st.textBody.innerHTML = renderMarkdown(st.textRaw);
      keepTextLast(st, msgsEl());
    }
  }

  /** 主模型 think（reasoning_chunk 不带 model）。
      [think 可见 2026-10-14 · 用户令 ⑥] 改为**可折叠思考块**（通用形态，不再是自己定义的
      隐形小字）：头行＝呼吸点＋「思考中」＋走秒（可点折叠／展开），正文＝灰色斜体流式追加；
      思考结束→头行转「已思考 Ns」。原来只有一行灰字、且在正文接管时被整块替换 ⇒ 用户
      判「think 的过程根本看不到」。 */
  function ensureReasoning(st) {
    if (st.reasoning) return st;
    textEntry(st);
    if (!st.textEl) return st;
    const r = document.createElement("div");
    r.className = "think";
    r.innerHTML =
      '<div class="think-head"><i class="dot"></i>' +
      '<span class="think-lab">思考中</span>' +
      '<span class="think-time">0.0s</span>' +
      '<span class="think-caret">▾</span></div>' +
      '<div class="think-body"></div>';
    st.textEl.insertBefore(r, st.textBody);
    st.reasoning = r;
    st.reasoningText = r.querySelector(".think-body");
    st._thinkT0 = performance.now();
    const timeEl = r.querySelector(".think-time");
    st._thinkTimer = setInterval(() => {
      if (!st._thinkT0) return;
      timeEl.textContent = ((performance.now() - st._thinkT0) / 1000).toFixed(1) + "s";
    }, 200);
    r.querySelector(".think-head").onclick = () => {
      const open = r.classList.toggle("collapsed");
      r.querySelector(".think-caret").textContent = open ? "▸" : "▾";
      scrollBottom();
    };
    return st;
  }

  /** think 收尾：停走秒、头行转「已思考 Ns」（正文保留，可展开回看）。 */
  function finishReasoning(st) {
    if (!st.reasoning) return;
    if (st._thinkTimer) { clearInterval(st._thinkTimer); st._thinkTimer = null; }
    if (st._thinkT0) {
      const s = ((performance.now() - st._thinkT0) / 1000).toFixed(1);
      st._thinkT0 = 0;
      const lab = st.reasoning.querySelector(".think-lab");
      const t = st.reasoning.querySelector(".think-time");
      if (lab) lab.textContent = "已思考";
      if (t) t.textContent = s + "s";
      st.reasoning.classList.remove("thinking");
    }
  }

  /* ---------- 三席并排卡片（用户 00:16 / 00:17 裁定） ----------
     真实依据：三席 asyncio.gather 并发（simple_service.py:276）⇒ 思考增量交织，按席分桶。
     时序：seats_enter 一行 → seat_start 建卡 → reasoning_chunk(带 model) 流入卡内
     → seat_end 输出正文入同一滚动区 + 状态转「已返回」（不单独成行）。 */

  function seatsBox(st) {
    const box = msgsEl();
    if (!box) return null;
    if (!st.seatsEl || !st.seatsEl.isConnected) {
      st.seatsEl = document.createElement("div");
      st.seatsEl.className = "seats";
      appendToBox(st, st.seatsEl);
      st.rows.push(st.seatsEl);
      keepTextLast(st, box);
    }
    return st.seatsEl;
  }

  function seatCard(st, model) {
    if (!st.seatCards) st.seatCards = {};
    if (st.seatCards[model]) return st.seatCards[model];
    const host = seatsBox(st);
    if (!host) return null;
    const card = document.createElement("div");
    card.className = "seat-card";
    card.dataset.seat = model;
    card.innerHTML =
      '<div class="sc-head"><span class="m"></span><span class="st">思考中</span></div>' +
      '<div class="sc-body"><span class="tk"></span></div>' +
      '<div class="sc-foot"><span class="ok"></span><span class="meta"></span></div>';
    card.querySelector(".sc-head .m").textContent = model;
    host.appendChild(card);
    st.seatCards[model] = {
      el: card,
      body: card.querySelector(".sc-body"),
      tk: card.querySelector(".tk"),
      st: card.querySelector(".sc-head .st"),
      footOk: card.querySelector(".sc-foot .ok"),
      footMeta: card.querySelector(".sc-foot .meta"),
      out: null,
    };
    logInfo("[flow-render] seat-card(" + model + ")");
    scrollBottom();
    return st.seatCards[model];
  }

  function seatScroll(c) {
    if (c && c.body) c.body.scrollTop = c.body.scrollHeight;   // 滚动往上：最新恒在视线里
  }

  function appendChunk(st, kind, text, seq, model) {
    if (!text) return;
    if (kind === "reasoning") {
      // 带 model 的是**三席**思考（并发交织）→ 流入各自席卡的单一滚动区；
      // 不带 model 的是主模型思考 → 仍落正文条目内的弱化区。
      if (model) {
        const c = seatCard(st, model);
        if (c && c.tk) {
          c.tk.textContent += text;
          seatScroll(c);
          logInfo("[flow-render] seat-chunk(" + model + ", " + text.length + ")");
          scrollBottom();
          return;
        }
      }
      ensureReasoning(st).reasoningText.textContent += text;
    } else {
      textEntry(st);
      if (!st.textBody) return;
      st.textRaw += text;                 // [⑧] 累积原文
      scheduleMd(st);                     // [⑧] 节流渲染（不是每字一次）
      keepTextLast(st, msgsEl());
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
    st.textRaw = "";                                   // [⑧] 原文累积随正文解绑一起清
    if (st._mdTimer) { clearTimeout(st._mdTimer); st._mdTimer = null; }
  }

  /** 轮界：正文条目只留「当前正在生成的那一轮」；并挪到队尾（最新在下）。 */
  function clearText(st) {
    if (!st.textEl) return;
    if (st.textBody) st.textBody.innerHTML = "";
    st.textRaw = "";                                   // [⑧] 轮界清空原文累积
    if (st._mdTimer) { clearTimeout(st._mdTimer); st._mdTimer = null; }
    if (st.reasoningText) st.reasoningText.textContent = "";
    if (st.reasoning && !(st.reasoningText && st.reasoningText.textContent)) {
      try { st.reasoning.remove(); } catch (e) { /* noop */ }
      st.reasoning = null; st.reasoningText = null;
    }
    keepTextLast(st, msgsEl());
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

  /** 每个事件的真实载荷（逐个 emit 调用点实读）：
      run_start{pair_id,chars} context{regions,bytes} judgment{verdict}
      plan_created{name,total,prompt,steps[]} step_start{step_id,index,total,prompt,criteria,deliverable}
      step_advanced{advanced,all_done,prompt,step_id,criteria_met,result,next_prompt,next_criteria}
      round_start{round,phase} tool_call{tool,target,step_id} tool_result{tool,target,ok,step_id}
      seats_enter{tool_results,steps} seat_start{model} seat_end{model,ok,latency_ms,chars,text}
      deliver{branch,bytes} proposal{count,cards[]} done{rounds,model_calls} error{error,rounds}
      rescue{count} card_message{msg_id,from,type}（不在 EVENT_TYPES，stream 原先无 case ⇒ 收到即丢） */
  /** [消息底座收尾 2026-10-14 · 件2] 老卡环节块降级显示的判定集。
      迁移来的老卡环节块 `data` 只有 `{content, behavior}`，缺 verdict/steps/
      criteria_met/result 等结构化字段——**不解析历史自由文本去补字段**
      （解析自由文本会卡死、且违背「不用 JSON 机械校验」裁定），直接降级成
      一条「历史记录（旧格式）」＋ content 原文。
      活跃事件这四类**从不带 content**（逐个 emit 调用点实读确认：judgment 带
      verdict；plan_created 带 name/total/prompt/steps；step_advanced 带
      step_id/criteria_met/result…；synthesize 无发送方）——故「data.content
      为非空串」＝老格式，判定最简、零误伤。 */
  const LEGACY_CONTENT_TYPES = { judgment: 1, plan_created: 1, step_advanced: 1, synthesize: 1 };

  function renderEvent(st, evt) {
    if (evt && typeof evt.seq === "number" && evt.seq > st.lastSeq) st.lastSeq = evt.seq;
    const d = evt.data || {};
    // [件2] 老卡环节块（迁移记录只有 content，无结构化字段）→ 降级显示原文，不占位。
    if (LEGACY_CONTENT_TYPES[evt.type] && typeof d.content === "string" && d.content) {
      const row = addRow(st, evt.type,
        "历史记录（旧格式）" + fld("behavior", d.behavior),
        null, d.content, "void");
      const det = row && row.querySelector(".step-detail");
      if (det) det.style.display = "block";   // 原文默认展开可见（降级＝照示原文）
      return;
    }
    switch (evt.type) {
      case "run_start":
        // 本轮开场：清本组。
        // [单一时序表 2026-10-14 · 用户令 ③] 重放期间**不清**——重放要逐条归位渲染
        // 整张时序表，清掉就把前面几轮的过程行一起毁了（原来只有最后一轮留下）。
        if (!st._replaying) { clearRows(st); dropText(st); }
        st.stepIndex = 0;
        addRow(st, evt.type, "一轮开始" + fld("pair_id", d.pair_id) + fld("chars", d.chars));
        break;
      case "context":
        addRow(st, evt.type,
          "装配上下文" + fld("regions", (d.regions || []).join("、")) + fld("bytes", d.bytes),
          (d.regions || []).length + " 区 · " + Math.round((d.bytes || 0) / 1024) + "KB");
        break;
      case "context_preamble":
        // [当前轮注入消息事件 2026-10-14] 固定总领（注入类事件，不是用户发言/
        // 助手回复——行样式由 type 标识，绝不渲染成聊天气泡）。
        addRow(st, evt.type,
          "上下文注入·固定总领" + fld("source_kind", d.source_kind) +
          fld("source_ref", typeof d.source_ref === "string" ? d.source_ref : ""),
          esc(d.source_kind || ""), fmtData(d), "lv3");
        break;
      case "context_injection": {
        // [当前轮注入消息事件 2026-10-14] 动态步骤注入（工具卡 / 思维链）。
        const ref = d.source_ref || {};
        const refTxt = (typeof ref === "string")
          ? ref
          : [ref.script_name || ref.plan_name || "", ref.step_id || ""]
              .filter(Boolean).join(" · ");
        addRow(st, evt.type,
          "上下文注入·当前步骤 " + esc(refTxt) +
          fld("source_kind", d.source_kind) + fld("step_id", d.step_id) +
          fld("index", d.index) + fld("total", d.total),
          esc(d.source_kind || ""), fmtData(d), "lv3");
        break;
      }
      case "judgment_protocol":
        // [判断协议改注入 2026-10-14] 判断协议注入（注入类事件，不是用户发言/
        // 助手回复——行样式由 type 标识）。只在轮首、且本卡无活板时出现一条。
        addRow(st, evt.type,
          "判断协议（轮首注入）" + fld("source_kind", d.source_kind),
          esc(d.source_kind || ""), fmtData(d), "lv3");
        break;
      case "context_seat_identity":
        // [三席身份注入 2026-10-14] 三席身份注入（注入类事件，不是用户发言/
        // 助手回复——行样式由 type 标识）。提交轮三席进场时一条，随三席调用下发。
        addRow(st, evt.type,
          "三席身份注入" + fld("source_kind", d.source_kind),
          esc(d.source_kind || ""), fmtData(d), "lv3");
        break;
      case "judgment":
        addRow(st, evt.type, "判断：" + (d.verdict === "direct" ? "直答" : "拆解上板") + fld("verdict", d.verdict));
        break;
      case "triage_verdict":
        // 已退役（无发送方）——保留渲染只为旧 journal 重放可读。
        addRow(st, evt.type, "旧判断事件（已退役）" + fld("verdict", d.verdict), "不会出现", fmtData(d), "void");
        break;
      case "plan_created": {
        const steps = Array.isArray(d.steps) ? d.steps : [];
        addRow(st, evt.type,
          "计划上板：<b>" + esc(d.name || "计划") + "</b>" + fld("total", d.total) + fld("prompt", d.prompt),
          (d.total || 0) + " 步", fmtData(d));
        addPlanSteps(st, steps);
        break;
      }
      case "step_start":
        st.stepIndex = Number(d.index) || (st.stepIndex + 1);
        addSub(st, evt.type,
          "步 " + esc(d.index || "") + "/" + esc(d.total || "") + ' <span class="bt">' + esc(btLabel(d.type)) + "</span> · " +
          esc(clip(d.prompt, 70)) + fld("step_id", d.step_id) + fld("criteria", d.criteria),
          null, fmtData(d), null, "lv2");
        break;
      case "step_advanced": {
        const done = d.all_done;
        const nxt = (st.stepIndex || 1) + 1;
        addSub(st, evt.type,
          (done ? "全部步骤完成，计划收口" : "步 " + esc(d.step_id || st.stepIndex || "") + " 完成") +
          fld("criteria_met", d.criteria_met) + fld("all_done", d.all_done) +
          fld("result", d.result) + fld("next_prompt", d.next_prompt),
          '<span class="mk ok">✓</span>' + (done ? "收口" : "→ 步 " + esc(nxt)),
          fmtData(d), done ? "ok" : null, "lv2");
        break;
      }
      case "round_start":
        addRow(st, evt.type, "第 " + esc(d.round || "") + " 轮" + fld("phase", d.phase), null, null, "lv3");
        break;
      case "rescue":
        addRow(st, evt.type, "文本抢救：截取可用片段续跑" + fld("count", d.count),
          esc(d.count || 0) + " 字符", fmtData(d));
        break;
      /* [调用中状态 2026-10-14 · 用户令 ⑤] 一条工具行两态原地更新——原先是两条独立行
         （tool_call 一行"调用"、tool_result 一行"返回"），中间那段"正在调用"看不见。
         现在：tool_call 落一行，行尾状态槽＝呼吸点＋走秒（显示效果）；
         tool_result 到达时**原地改成终态**（✓/✗ ＋ 耗时），不再新增行。
         配对依据：message_runner 内 tool_call → execute → tool_result 严格顺序成对
         （每个 tc 一次），故"上一行未终结的调用行"即本次结果的目标行。 */
      case "tool_call": {
        st._toolT0 = performance.now();
        st._toolName = d.tool;
        const row = addRow(st, evt.type,
          "<b>" + esc(d.tool) + "</b>" + fld("target", d.target) + fld("step_id", d.step_id),
          '<span class="mk run"><i class="dot"></i>调用中 0.0s</span>',
          fmtData(d), "running");
        st._toolRow = row;
        if (row) {
          const slot = row.querySelector(".sub .mk");
          if (slot) {
            slot._timer = setInterval(() => {
              if (!st._toolT0) return;
              const s = (performance.now() - st._toolT0) / 1000;
              slot.innerHTML = '<i class="dot"></i>调用中 ' + s.toFixed(1) + "s";
            }, 200);
          }
        }
        break;
      }
      case "tool_result": {
        let dur = "";
        if (st._toolT0) {
          const ms = performance.now() - st._toolT0;
          dur = (ms / 1000).toFixed(1) + "s";
          st._toolT0 = 0;
        }
        const row = st._toolRow;
        st._toolRow = null;
        const slotOf = (r) => r && r.querySelector(".sub .mk");
        if (row && row.isConnected && slotOf(row) && slotOf(row)._timer !== null) {
          const slot = slotOf(row);
          if (slot._timer) { clearInterval(slot._timer); slot._timer = null; }
          const txt = row.querySelector(".txt");
          if (txt) {
            txt.innerHTML = "<b>" + esc(d.tool) + "</b> 返回" +
              fld("ok", d.ok) + fld("step_id", d.step_id) + fld("target", d.target);
          }
          const sub = row.querySelector(".sub");
          if (sub) {
            sub.innerHTML = '<span class="mk ' + (d.ok ? "ok" : "no") + '">' +
              (d.ok ? "✓" : "✗") + "</span>" + dur +
              (d.step_id ? " #" + esc(d.step_id) : "");
          }
          const det = row.querySelector(".step-detail");
          if (det) det.textContent = fmtData(d);
          row.classList.remove("running");
          row.classList.add(d.ok ? "ok" : "fail");
          row.dataset.ev = "tool_result";
          logInfo("[flow-render] tool-row updated(" + (d.ok ? "ok" : "fail") + ", " + dur + ")");
        } else {
          // 无对应调用行（刷新重放/缺前帧）：单建一条终态行，不丢信息
          addRow(st, evt.type,
            "<b>" + esc(d.tool) + "</b> 返回" + dur +
            fld("ok", d.ok) + fld("step_id", d.step_id) + fld("target", d.target),
            '<span class="mk ' + (d.ok ? "ok" : "no") + '">' + (d.ok ? "✓" : "✗") + "</span>" +
            (d.step_id ? "#" + esc(d.step_id) : ""),
            fmtData(d), d.ok ? "ok" : "fail");
        }
        break;
      }
      case "seats_enter":
        addRow(st, evt.type, "三席进场" + fld("tool_results", d.tool_results) + fld("steps", d.steps),
          "并行质询", null, "lv3");
        break;
      case "seat_start":
        // 建卡：卡头＝模型名（+状态）；后续 reasoning_chunk 与 seat_end 都落这张卡。
        seatCard(st, d.model || "");
        break;
      case "seat_end": {
        const c = seatCard(st, d.model || "");
        if (c) {
          if (d.text) {
            const o = document.createElement("div");
            o.className = "ot";
            o.textContent = d.text;      // 观点正文全文（seat_end.text）
            c.body.appendChild(o);
            c.out = o;
          }
          c.st.textContent = (d.ok ? "✓ " : "✗ ") + "已返回";
          if (!d.ok) c.st.classList.add("fail");
          c.footOk.textContent = "ok=" + (d.ok ? "true" : "false");
          c.footMeta.textContent = d.ok
            ? ((d.latency_ms || 0) / 1000).toFixed(1) + "s · " + (d.chars || 0) + "字"
            : clip(d.error || "失败", 40);
          seatScroll(c);
        }
        break;
      }
      case "deliver":
        addRow(st, evt.type, "交付拼接" + fld("branch", d.branch) + fld("bytes", d.bytes),
          Math.round((d.bytes || 0) / 1024) + "KB");
        break;
      case "proposal":
        if (d.count > 0) {
          addRow(st, evt.type, "建卡 " + esc(d.count) + " 张" + fld("cards", (d.cards || []).join("、")),
            esc(d.count) + " 卡", fmtData(d));
        }
        break;
      case "card_message":
        // 卡间消息事件（Ⅰ 类）：真实载荷只有 msg_id/from/type——正文随会话渲染。
        addRow(st, evt.type,
          "卡间消息：<b>" + esc(CARD_MSG_DISPLAY[d.type] || d.type || "") + "</b>" +
          fld("msg_id", d.msg_id) + fld("from", d.from),
          esc(d.type || ""), fmtData(d));
        break;
      case "done":
        finishReasoning(st);   // [⑥] think 收尾：停走秒、头行转「已思考 Ns」
        flushMd(st);           // [⑧] 终态渲染一次（把节流期间的尾巴落定）
        addRow(st, evt.type, "完成" + fld("rounds", d.rounds) + fld("model_calls", d.model_calls),
          esc(d.rounds || 0) + " 轮 · " + esc(d.model_calls || 0) + " 调用", null, "ok");
        addToggle(st);   // 正式回复过后：过程可一键收起（默认展开，用户可选减负）
        finish();
        break;
      case "error":
        finishReasoning(st);   // [⑥]
        flushMd(st);           // [⑧]
        addRow(st, evt.type, "失败" + fld("error", d.error) + fld("rounds", d.rounds),
          '<span class="mk no">✗</span>失败', fmtData(d), "fail err");
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
    // [③ 单一时序表 2026-10-14] 实时流开始前清掉重放残留状态——重放结束后
    // _replaying/_insertAnchor 必须复位，否则实时帧会插到错误锚点、run_start 不清理。
    st._replaying = false;
    st._insertAnchor = null;
    try {
      // [消息底座收尾 2026-10-14] 无游标（lastSeq=0）＝从当前末帧起（from=0 即 tail，
      // 件4 语义）；有游标＝断点续传。刷新重建已改走 /timeline，不再依赖 SSE 重放。
      const sFrom = st.lastSeq > 0 ? st.lastSeq + 1 : 0;
      const es = new EventSource(`/api/works/${wid}/cards/${cid}/stream?from=${sFrom}`);
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
      const tFrom = st.lastTokenSeq > 0 ? st.lastTokenSeq + 1 : 0;
      const esT = new EventSource(`/api/works/${wid}/cards/${cid}/tokens?from=${tFrom}`);
      st.esT = esT;
      esT.onmessage = (m) => {
        try { onTokenFrame(st, JSON.parse(m.data)); } catch (e) { /* 单帧坏不致命 */ }
      };
      esT.onerror = () => { /* 静默 */ };
    } catch (e) { /* 同上 */ }
    return st;
  }

  /** [消息底座收尾 2026-10-14 · 件1] 把主模型思考（reasoning，model=""）挂进它所属
      那轮**正式回复气泡**内的弱化区——不新建任何条目（故不增 `.msg`、不动
      `renderChat` 的 `.msg:not(.live)` 计数判据）、不碰游标、不造空的 `.live` 气泡。
      目标气泡由调用方按「事件顺序计数」定位（见 replayTrace）。 */
  function attachReplayReasoning(bubble, text) {
    if (!bubble || !text) return;
    const r = document.createElement("div");
    r.className = "think done";
    r.innerHTML =
      '<div class="think-head"><span class="think-lab">已思考</span>' +
      '<span class="think-caret">▾</span></div>' +
      '<div class="think-body"></div>';
    r.querySelector(".think-body").textContent = text;
    r.querySelector(".think-head").onclick = () => {
      const open = r.classList.toggle("collapsed");
      r.querySelector(".think-caret").textContent = open ? "▸" : "▾";
    };
    const md = bubble.querySelector(".md");
    if (md) bubble.insertBefore(r, md);   // 思考块在正式正文之前
    else bubble.appendChild(r);
    logInfo("[flow-render] replay-reasoning(" + text.length + ")");
  }

  /** 刷新/换卡恢复：按**单一时序表**逐条归位重建对话流。
      [单一时序表 2026-10-14 · 用户令 ③] 源＝落盘事件流 `GET .../timeline`（唯一可回溯的
      时序真相源）。**不再**"渲染完再整块搬到最后一轮回复之前"——那会把历史上所有轮次的
      过程信息全塞到最后一轮（用户判「中间过程信息不在调用信息的间隔中，而是塞到最后的
      文字里，弄得乱七八糟，分不清正文从哪里开始」）。
      现在的纪律：时序表里每条记录都有 seq，**过程行插到"它后面第一条消息"之前**——
      于是每轮的过程行落在该轮提问与答复之间，顺序天然正确，不做任何事后搬移。

      正文不重放——消息气泡已由 renderChat 从会话渲染（两条口径实测同条数同序），
      重放只落过程行与思考块，避免重复气泡。

      ★ 游标纪律：`lastSeq`/`lastTokenSeq` 是**内存 journal 的 seq**，而 timeline
      记录带的是**落盘 events 的 seq**——两套序列起点不同、且消息块只进 events
      不进 journal，**数值不相等**。故重放期间先存后还原，绝不把 events 的 seq
      写进 journal 游标（否则随后 `start()` 拿错游标，会把过程行重复渲染）。 */
  async function replayTrace(wid, cid) {
    const st = ensureRun(wid, cid);
    stopES();
    beginGroup(st);
    const savedSeq = st.lastSeq, savedTokenSeq = st.lastTokenSeq;
    let items = [];
    try {
      const resp = await API.getTimeline(wid, cid);
      items = (resp && resp.items) || [];
    } catch (e) {
      return;   // 拉不到就静默收，绝不降级成等待气泡（旧行为同族）
    }
    // 消息类记录已由 renderChat 从会话渲染（实测与 timeline 同条数同序），重放只落过程。
    const MSG = { user_message: 1, assistant_text: 1,
                  msg_ask: 1, msg_answer: 1, msg_notify: 1 };
    const box = msgsEl();
    const msgEls = box
      ? Array.from(box.querySelectorAll(".msg.user, .msg.assistant:not(.live)"))
      : [];
    let cursor = 0;      // 已「走过」的消息条数＝下一条待插消息的下标
    st._replaying = true;
    try {
      for (const rec of items) {
        const t = rec && rec.type;
        if (!t) continue;
        if (MSG[t]) { cursor++; continue; }        // 消息已渲染，只推进游标
        st._insertAnchor = msgEls[cursor] || null; // ③ 归位锚点＝其后第一条消息
        if (t === "reasoning") {
          // 三席思考（带 model）流入各自席卡；主模型（model=""）挂进**它所属那轮**的
          // 正式回复气泡内的可折叠思考块（⑥）。
          // ★ model 位置：落盘 events 记录的 model 在**顶层**（event_log._normalize
          //   固定七键 seq/card_id/run_id/round/type/data/model），不是 data.model——
          //   与实时 SSE 帧（emit_token 把 model 并入 data）形态不同，此处以顶层为准。
          const d = rec.data || {};
          const model = rec.model || d.model || "";
          if (model) appendChunk(st, "reasoning", d.text || "", rec.seq, model);
          else {
            const target = nextAssistant(msgEls, cursor);
            attachReplayReasoning(target, d.text || "");
          }
          continue;
        }
        renderEvent(st, { seq: rec.seq, type: rec.type, data: rec.data || {} });
      }
    } finally {
      st._replaying = false;
      st._insertAnchor = null;
    }
    st.lastSeq = savedSeq;              // 还原 journal 游标（见函数头说明）
    st.lastTokenSeq = savedTokenSeq;
  }

  /** ③ 从游标处向后找第一条助手气泡（主模型思考挂这轮回复）。 */
  function nextAssistant(msgEls, cursor) {
    for (let i = cursor; i < msgEls.length; i++) {
      const e = msgEls[i];
      if (e && e.classList && e.classList.contains("assistant")) return e;
    }
    return null;
  }

export const EventFlow = { start, stop, replayTrace };
