import { coordinationApi as api, renderCardRegion } from "./coordination.js?v=6";
import { mountPlanBoard } from "./coordination_board.js?v=5";
import { initModelBadge } from "./model_picker.js?v=12";
import { API } from "./api.js?v=57";

function styleBoard() {
  const style = document.createElement("style");
  style.textContent = `
    .coord-pane { padding-top:8px; }
    .board-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }
    .board-rev { font-weight:600; }
    .board-notice { color:var(--accent, #4f8cff); font-size:12px; }
    .board-item { border:1px solid var(--line); border-radius:8px; margin-bottom:8px; }
    .board-item-head { display:flex; align-items:center; gap:8px; padding:8px 10px;
      flex-wrap:wrap; }
    .board-prio { font-variant-numeric:tabular-nums; font-weight:700; min-width:38px; }
    .board-status { font-size:12px; border:1px solid var(--line); border-radius:10px;
      padding:0 8px; }
    .board-status-doing { color:#d8a02a; border-color:#d8a02a; }
    .board-status-blocked { color:#d05a5a; border-color:#d05a5a; }
    .board-status-done { color:#4a9e5c; border-color:#4a9e5c; }
    .board-title { font-weight:600; }
    .board-owner { font-size:12px; color:var(--dim); }
    .board-owner-miss { color:#d8a02a; }
    .board-dep { font-size:12px; color:var(--dim); }
    .board-item-id { font-size:11px; color:var(--dim); margin-left:auto; }
    .board-actions { display:flex; gap:6px; }
    .board-item-body { padding:0 12px 8px; }
    .board-line { font-size:13px; margin:2px 0; }
    .board-dim { color:var(--dim); }
    .board-item-edit { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
      gap:8px; padding:10px; border-top:1px dashed var(--line); }
    .board-item-edit label { display:grid; gap:3px; font-size:12px; color:var(--dim); }
    .board-item-edit input, .board-item-edit select { color:var(--text);
      background:var(--panel); border:1px solid var(--line); border-radius:6px;
      padding:5px 7px; font:inherit; }
    .board-wide { grid-column:1/-1; }
    .board-edit-actions { grid-column:1/-1; display:flex; gap:8px; }
    .board-empty { color:var(--dim); padding:14px 0; }
    .board-done { margin-top:10px; }
    .board-conflict { display:flex; align-items:center; gap:10px; width:100%;
      border:1px solid #d05a5a; border-radius:8px; padding:8px 10px; margin-top:6px;
      color:#d05a5a; }
    .board-add-slot { margin-bottom:10px; border:1px dashed var(--line); border-radius:8px; }
  `;
  document.head.appendChild(style);
}

async function renderArchive(container, endpoint, work, agentPath) {
  const [record, legacy] = await Promise.all([api(`${endpoint}/runs`), api(`${work}/coordination`)]);
  if (!record.ok) throw new Error(record.body.error || "读取巡检记录失败");
  container.replaceChildren();
  for (const run of (record.body.runs || []).filter((row) => row.phase === "archived")) {
    const item = document.createElement("div");
    const link = document.createElement("a");
    link.href = `${agentPath}?run=${encodeURIComponent(run.run_id)}`;
    link.textContent = `${run.trigger === "inspection" ? "巡检" : "统筹核查"} · ${new Date(run.created_at).toLocaleString()}`;
    item.appendChild(link);
    container.appendChild(item);
  }
  if (!container.childElementCount) container.textContent = "暂无已归档子会话。";
  const prior = (legacy.body.plans || []).filter((plan) => plan.plan_card_id !== "coordination");
  for (const plan of prior) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `旧版计划 · ${plan.title || plan.plan_card_id}`;
    const body = document.createElement("div");
    details.append(summary, body);
    details.addEventListener("toggle", async () => {
      if (!details.open || body.textContent) return;
      const r = await api(`${work}/cards/${encodeURIComponent(plan.plan_card_id)}/coordination?history=1`);
      body.textContent = r.ok ? "已归档。\n" : (r.body.error || "读取旧版计划失败");
      if (r.ok) {
        const pre = document.createElement("pre");
        pre.style.whiteSpace = "pre-wrap";
        pre.textContent = JSON.stringify(r.body.plan || {}, null, 2);
        body.appendChild(pre);
      }
    });
    container.appendChild(details);
  }
}

const phaseLabel = {
  checking: "巡检中", waiting: "等待强模型", working: "强模型处理中", archived: "已归档",
};
const messageLabel = {
  decision: "统筹决定", inspection_conclusion: "弱模型结论",
  strong_step: "强模型处理记录", strong_conclusion: "强模型最终决定",
};

/** 会话原文投影到现有对话的事件结构。 */
function transcriptEvents(messages, runId) {
  const pending = new Map();
  return messages.map((message, index) => {
    const seq = Number.isInteger(message.seq) ? message.seq : index;
    const event = { seq, round: seq, run_id: runId || "",
                    ts: message.ts || message.timestamp, model: message.model };
    const content = message.content || "";
    if (message.actor === "inspection_call" || message.actor === "strong_tool_call") {
      let call = message.tool_calls?.[0];
      if (!call) {
        // 旧巡检记录只有正文。新记录沿用正式工具块的调用标识。
        try {
          const old = JSON.parse(content);
          call = { id: "stored:" + seq, function: { name: old.name, arguments: old.arguments } };
        } catch { /* 无法识别的旧记录仍显示正文。 */ }
      }
      const tool = call?.function?.name || call?.name;
      if (tool) {
        const id = call.id || "stored:" + seq;
        let args = call.function?.arguments ?? call.arguments;
        if (typeof args === "string") {
          try { args = JSON.parse(args); } catch { /* 原样保留参数。 */ }
        }
        pending.set(id, tool);
        return { ...event, type: "tool_call", data: { tool, arguments: args || {}, tool_call_id: id } };
      }
    }
    if (message.actor === "inspection_result" || message.actor === "strong_tool_result") {
      const oldTool = content.match(/^(?:查证结果 \[[^\]]+\] )?(coord_\w+)/)?.[1];
      const id = message.tool_call_id
        || [...pending].find(([, tool]) => oldTool ? tool === oldTool : tool !== "coord_wake_strong")?.[0];
      const tool = pending.get(id) || oldTool || "巡检查证";
      pending.delete(id);
      return { ...event, type: "tool_result", data: {
        tool, tool_call_id: id,
        ok: message.status ? message.status === "executed" : !/被拒|查证失败|未入队/.test(content.slice(0, 80)),
        result: { content },
      } };
    }
    return { ...event, type: message.actor === "human" ? "user_message" : "assistant_text",
             data: { content, behavior: messageLabel[message.actor],
                     coordination_run: message.actor === "decision" ? message.run_id : undefined } };
  });
}

export function mountCoordination({ wid, flow, setWritable }) {
  const work = "/api/works/" + encodeURIComponent(wid);
  const endpoint = work + "/coordination/agent";
  const chat = endpoint + "/chat";
  const agentPath = "/work/" + encodeURIComponent(wid) + "/coordination";
  const url = new URL(location.href);
  const runId = url.searchParams.get("run");
  const key = runId ? "coordination@" + runId : "coordination@coordination";
  const source = runId ? endpoint + "/runs/" + encodeURIComponent(runId) : chat;
  const crumb = document.getElementById("chatCrumb");
  const inspector = document.getElementById("inspector");
  const pane = document.getElementById("inspBody");
  const title = document.createElement("span");
  title.className = "cseg cur";
  title.textContent = runId ? "巡检子会话" : "项目统筹";
  crumb.replaceChildren();
  const toggle = document.getElementById("topInspBtn");

  if (runId) {
    const parent = document.createElement("a");
    parent.className = "cseg";
    parent.href = agentPath;
    parent.textContent = "项目统筹";
    const separator = document.createElement("span");
    separator.className = "csep";
    separator.textContent = "›";
    crumb.append(parent, separator);
    document.getElementById("composerSeat").style.display = "none";
    inspector.style.display = "none";
    toggle.hidden = true;
  } else {
    document.body.classList.add("coordination-chat");
    inspector.classList.remove("open");
    document.getElementById("flowInputLeft").style.display = "none";
    toggle.title = "统筹详情";
    toggle.setAttribute("aria-label", "统筹详情");
    const edge = document.getElementById("inspTab");
    edge.textContent = "ⓘ 统筹详情";
    edge.title = "展开或收起统筹详情";
  }
  crumb.appendChild(title);

  flow.activate(wid, key);
  const empty = { title: title.textContent,
                  desc: runId ? "巡检记录会显示在这里。" : "可以询问项目计划或进展。" };
  flow.setEmpty(wid, key, empty);
  let fetching = false;
  let initialized = false;
  let lastSeq = -1;
  let phase = "";
  async function refreshTranscript() {
    if (fetching) return;
    fetching = true;
    try {
      const r = await api(source);
      if (!r.ok) throw new Error(r.body.error || "读取会话失败");
      const messages = r.body.messages || [];
      const newest = messages.length ? (messages.at(-1).seq ?? messages.length - 1) : -1;
      if (newest !== lastSeq || !initialized) {
        flow.applyStoredTimeline(wid, key, transcriptEvents(messages, runId));
        lastSeq = newest;
      }
      if (!initialized) flow.setEmpty(wid, key, empty);
      initialized = true;
      if (runId && phase !== r.body.phase) {
        phase = r.body.phase;
        title.textContent = phaseLabel[phase] || "巡检子会话";
        const running = phase === "checking" || phase === "working";
        if (flow.sessionRunning(wid, key) !== running) {
          flow.setRunning(wid, key, running, { fromHost: true });
        }
      }
    } catch (error) {
      if (!initialized) {
        flow.setEmpty(wid, key, { title: "会话读取失败", desc: error.message });
        flow.markLoaded(wid, key);
      }
    } finally {
      fetching = false;
    }
  }

  if (runId) {
    refreshTranscript();
    const timer = setInterval(() => {
      if (!document.hidden && phase !== "archived") refreshTranscript();
    }, 3000);
    window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
    return { key, send: null };
  }

  const historyReady = Promise.all([flow.replayTrace(wid, key),
    API.getProgress(wid, "coordination", { session: key }).catch(() => null),
  ]).then(([, progress]) => {
    flow.start(wid, key, null, { replayTokens: !!progress?.active });
    if (progress?.active) flow.setRunning(wid, key, true);
  });
  window.addEventListener("pagehide", () => flow.stop(wid, key), { once: true });

  styleBoard();
  pane.innerHTML = `
    <nav class="session-tabs" aria-label="统筹详情">
      <button type="button" class="session-tab" data-tab="board">计划板</button>
      <button type="button" class="session-tab" data-tab="settings">运行设置</button>
      <button type="button" class="session-tab" data-tab="archive">巡检记录</button>
    </nav>
    <section class="coord-pane" data-pane="board" hidden></section>
    <section class="coord-pane" data-pane="settings" hidden>
      <div class="coord-status" role="status"></div>
      <div class="coord-page-plan"></div>
    </section>
    <section class="coord-pane coord-archives" data-pane="archive" hidden></section>`;

  const boardPane = pane.querySelector('[data-pane="board"]');
  const archivePane = pane.querySelector('[data-pane="archive"]');
  const planRegion = pane.querySelector(".coord-page-plan");
  const message = pane.querySelector(".coord-status");
  const binding = { assignment_id: "", expected_rev: 0 };
  const board = mountPlanBoard(boardPane, { endpoint, binding, wid });
  let appointment = {};
  let firstChoices = {};
  const tabs = pane.querySelectorAll("[data-tab]");
  const panels = pane.querySelectorAll("[data-pane]");
  const picker = initModelBadge({
    mountId: "modelBadgeMount", workId: wid, cardId: "coordination",
    selection: {
      title: "统筹模型",
      slots: [
        { id: "strong", label: "强模型", description: "用于与你对话和接手巡检。" },
        { id: "weak", label: "弱模型", description: "仅用于按计划巡检。" },
      ],
      current(role) {
        return { model: appointment.models?.[role] || firstChoices[role] || "",
                 label: appointment.model_labels?.[role] || "" };
      },
      save: saveModel,
    },
  });

  async function readPlan() {
    const r = await api(endpoint);
    if (!r.ok) throw new Error(r.body.error || "读取统筹设置失败");
    appointment = r.body;
    binding.assignment_id = appointment.assignment_id || "";
    binding.expected_rev = appointment.rev;
    if (binding.assignment_id) firstChoices = {};
    await historyReady;
    setWritable(appointment.state === "active");
    picker.refresh();
    return appointment;
  }

  async function saveModel(role, model) {
    const state = await readPlan();
    const configured = Boolean(state.assignment_id);
    const models = { ...(configured ? state.models : firstChoices), [role]: model };
    if (!configured && !(models.strong && models.weak)) {
      firstChoices = models;
      return role === "strong" ? "已选强模型，请继续选择弱模型" : "已选弱模型，请继续选择强模型";
    }
    if (configured && state.models[role] === model) return "模型已是当前选择";
    const r = await api(endpoint, { method: "POST", body: JSON.stringify({
      assignment_id: state.assignment_id, expected_rev: state.rev,
      op: configured ? "set_models" : "configure", models,
    }) });
    if (r.status === 409) await readPlan();
    if (!r.ok) throw new Error(r.body.error || "保存模型失败");
    await refreshSettings();
    return role === "strong" ? "强模型已保存" : "弱模型已保存";
  }

  async function refreshSettings() {
    await readPlan();
    message.textContent = binding.assignment_id ? "" : "请从输入框旁的模型选择器配置强模型和弱模型。";
    if (binding.assignment_id) {
      await renderCardRegion(planRegion, () => refreshSettings().catch(showSettingsError), endpoint);
    }
  }

  function showSettingsError(error) {
    message.textContent = error.message;
  }

  async function activate(next) {
    for (const button of tabs) button.classList.toggle("on", button.dataset.tab === next);
    for (const panel of panels) panel.hidden = panel.dataset.pane !== next;
    if (next === "board") {
      try { await board.load(); } catch (error) { boardPane.textContent = error.message; }
    } else if (next === "archive") {
      try { await renderArchive(archivePane, endpoint, work, agentPath); }
      catch (error) { archivePane.textContent = error.message; }
    } else {
      await refreshSettings().catch(showSettingsError);
    }
  }

  for (const button of tabs) button.onclick = () => {
    url.searchParams.set("tab", button.dataset.tab);
    history.replaceState(history.state, "", url);
    activate(button.dataset.tab);
  };

  new MutationObserver(() => {
    if (!inspector.classList.contains("open")) return;
    const selected = url.searchParams.get("tab");
    const known = ["board", "settings", "archive"].includes(selected);
    activate(known ? selected : (binding.assignment_id ? "board" : "settings"));
  }).observe(inspector, { attributes: true, attributeFilter: ["class"] });

  readPlan().then(() => {
    if (!binding.assignment_id) flow.setEmpty(wid, key, {
      title: "项目统筹", desc: "从输入框旁的模型选择器配置强模型和弱模型。",
    });
  }).catch(showSettingsError);

  return { key, async send(content) {
    const state = await readPlan();
    if (state.state !== "active") throw new Error("请先配置模型或恢复项目统筹。");
    const r = await api(chat, { method: "POST", body: JSON.stringify({ ...binding, content }) });
    if (r.ok || r.body.accepted) {
      await flow.resync(wid, key, { follow: true, runId: r.body.run_id });
    }
    readPlan().catch(showSettingsError);
    if (!r.ok) {
      const error = new Error(r.body.error || "发送失败");
      error.accepted = !!r.body.accepted;
      throw error;
    }
  } };
}
