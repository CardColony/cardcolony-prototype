/* stream.js - [信息流与快车道 Task5] 过程卡渲染器（甲线前端）。
   EventSource 管理＋对话区内嵌可折叠时间线：🧠轮次/🔧工具/💬三席/✚建卡/✓交付/✗错误。
   快车道秒回也过过程卡（fastlane_route→card_op→done，A102）。
   SSE 失败回调 onFail → 由 workspace.js 回退 createWaitingBubble 轮询（V6 降级）。
   事件纯展示层：只读渲染，不写任何存储、不影响 POST。 */
(function () {
  "use strict";

  let current = null;   // {wid, cid, es, card, body, fallbackUsed}

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function fmtData(data) {
    try { return JSON.stringify(data, null, 1); } catch (e) { return String(data); }
  }

  function ensureCard(wid, cid) {
    if (current && current.card && current.wid === wid && current.cid === cid) return current;
    stop();
    const card = document.createElement("div");
    card.className = "flow-card";
    card.innerHTML = `
      <div class="flow-head"><span class="flow-title">🧠 执行过程</span>
        <span class="flow-toggle" title="展开/折叠">▾</span></div>
      <div class="flow-body"></div>`;
    const chatArea = document.getElementById("chatArea");
    chatArea.appendChild(card);
    card.querySelector(".flow-head").onclick = () => card.classList.toggle("collapsed");
    current = { wid, cid, es: null, card, body: card.querySelector(".flow-body"), fallbackUsed: false, count: 0 };
    return current;
  }

  function addLine(st, html, detail, cls) {
    const line = document.createElement("div");
    line.className = "flow-line" + (cls ? " " + cls : "");
    line.innerHTML = html;
    if (detail) {
      const det = document.createElement("pre");
      det.className = "flow-detail";
      det.textContent = detail;
      line.appendChild(det);
      line.classList.add("has-detail");
      line.onclick = (e) => {
        if (e.target.tagName === "PRE") return;
        det.style.display = det.style.display === "block" ? "none" : "block";
      };
    }
    st.body.appendChild(line);
    st.body.scrollTop = st.body.scrollHeight;
    st.card.scrollTop = 1e9;
    return line;
  }

  function render(st, evt) {
    const d = evt.data || {};
    switch (evt.type) {
      case "run_start": st.body.innerHTML = ""; addLine(st, "🚀 开始处理"); break;
      case "context": addLine(st, `📐 装配上下文 <span class="flow-dim">${esc(d.bytes || 0)}B · ${(d.regions || []).length} 区</span>`); break;
      case "fastlane_route":
        addLine(st, d.mode === "fastlane"
          ? `⚡ 快车道 <span class="flow-dim">命中：${esc(d.matched_rule)}</span>`
          : `🧩 全链模式 <span class="flow-dim">未命中规则</span>`,
          fmtData(d));
        break;
      case "round_start":
        addLine(st, `🧠 第 ${esc(d.round)} 轮 <span class="flow-dim">${esc(d.phase || "")}</span>`);
        break;
      case "rescue": addLine(st, `🩹 文本抢救 ${esc(d.count)} 条`, fmtData(d)); break;
      case "tool_call": addLine(st, `🔧 ${esc(d.tool)} <span class="flow-dim">${esc(d.target)}</span>`, fmtData(d)); break;
      case "tool_result":
        addLine(st, `${d.ok ? "✓" : "✗"} ${esc(d.tool)} <span class="flow-dim">${esc(d.target)}</span>`, fmtData(d), d.ok ? "ok" : "fail");
        break;
      case "card_op": {
        const icon = { create_card: "✚", edit_card: "✎", delete_card: "🗑", read_card: "📖" }[d.op] || "🗂";
        addLine(st, `${icon} ${esc(d.op)} ${esc(d.target)} ${d.ok ? "✓" : "✗"}`, fmtData(d), d.ok ? "ok" : "fail");
        break;
      }
      case "seats_enter": addLine(st, `💬 三席进场 <span class="flow-dim">工具结果 ${esc(d.tool_results)} 条</span>`); break;
      case "seat_start": addLine(st, `💬 ${esc(d.model)} …`); break;
      case "seat_end":
        addLine(st, `${d.ok ? "✓" : "✗"} ${esc(d.model)} <span class="flow-dim">${d.ok ? ((d.chars || 0) + "字 · " + ((d.latency_ms || 0) / 1000).toFixed(1) + "s") : esc(d.error || "失败")}</span>`,
          d.ok ? null : fmtData(d), d.ok ? "ok" : "fail");
        break;
      case "deliver": addLine(st, `📦 交付拼接 <span class="flow-dim">${esc(d.branch)} · ${esc(d.bytes)}B</span>`); break;
      case "proposal":
        if (d.count > 0) addLine(st, `✚ 建卡 ${esc(d.count)} 张 <span class="flow-dim">${esc((d.cards || []).join("、"))}</span>`, fmtData(d));
        break;
      case "done":
        addLine(st, `✓ 完成 <span class="flow-dim">${esc(d.rounds)} 轮 · 模型 ${esc(d.model_calls)} 次</span>`, null, "ok");
        finish(st, true);
        break;
      case "error":
        addLine(st, `✗ 失败 <span class="flow-dim">${esc(d.error)}</span>`, fmtData(d), "fail");
        finish(st, false);
        break;
      default: break;
    }
  }

  function finish(st, ok) {
    // 结束自动折叠为一行摘要（1.2s 后；可点开回看）
    setTimeout(() => {
      st.card.classList.add("collapsed", ok ? "flow-done" : "flow-err");
      const t = st.card.querySelector(".flow-title");
      t.textContent = ok ? "🧠 执行过程 · ✓ 完成" : "🧠 执行过程 · ✗ 失败";
    }, 1200);
    stopES();
  }

  function stopES() {
    if (current && current.es) { try { current.es.close(); } catch (e) { /* noop */ } current.es = null; }
  }

  function stop() {
    stopES();
    current = null;   // 卡留在 DOM（POST settle 后由 workspace 控制去留）
  }

  /** 开始跟踪：EventSource(from=0)；onFail 在连不上时回调一次（降级保底）。 */
  function start(wid, cid, onFail) {
    const st = ensureCard(wid, cid);
    try {
      const es = new EventSource(`/api/works/${wid}/cards/${cid}/stream?from=0`);
      st.es = es;
      es.onmessage = (m) => {
        try { render(st, JSON.parse(m.data)); } catch (e) { /* 单帧坏不致命 */ }
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
    return st;
  }

  window.EventFlow = { start, stop };
})();
