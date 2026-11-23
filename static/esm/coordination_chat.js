import {
  coordinationApi as api, esc, appointFormHtml, bindAppointForm, loadModels, renderCardRegion,
} from "./coordination.js?v=3";
import { mountPlanBoard } from "./coordination_board.js?v=2";

function stylePage() {
  const style = document.createElement("style");
  style.textContent = `
    .coord-page { position:absolute; inset:0; display:flex; flex-direction:column;
                  background:var(--panel); color:var(--text); overflow:hidden; }
    .coord-page-header { display:flex; align-items:center; flex-wrap:wrap; gap:12px;
                        padding:10px 18px; border-bottom:1px solid var(--line); }
    .coord-page-content { display:flex; flex-direction:column; flex:1; min-height:0;
                          max-width:960px; width:100%; margin:0 auto; }
    .coord-page-setup { padding:18px; overflow:auto; }
    .coord-page select, .coord-page textarea { color:var(--text); background:var(--panel);
      border:1px solid var(--line); border-radius:6px; padding:6px 8px; font:inherit; }
    .coord-page-settings { padding:10px 18px; max-height:44%; overflow:auto;
                           border-bottom:1px solid var(--line); }
    .coord-page-settings summary { cursor:pointer; }
    .coord-models { display:flex; align-items:end; flex-wrap:wrap; gap:10px; margin:8px 0; }
    .coord-models label { display:grid; gap:4px; min-width:140px; }
    .coord-chat-log { flex:1; min-height:0; overflow:auto; padding:18px; }
    .coord-chat-message { margin:0 0 18px; max-width:min(100%, 760px);
                          line-height:1.65; overflow-wrap:anywhere; }
    .coord-chat-message[data-actor="human"] { margin-left:auto; width:fit-content;
      padding:8px 12px; border-radius:8px; background:var(--panel-hover); }
    .coord-chat-message p { white-space:pre-wrap; margin:4px 0 0; }
    .coord-chat-message small, .coord-chat-status { color:var(--dim); }
    .coord-chat-form { display:flex; align-items:end; gap:8px; padding:10px 18px; }
    .coord-chat-form textarea { flex:1; min-width:0; resize:vertical; }
    .coord-chat-status { margin:0 18px 9px; min-height:1.25em; font-size:12px; }
    .coord-tabs { display:flex; gap:4px; padding:8px 18px 0; border-bottom:1px solid var(--line); }
    .coord-tabs button { border:0; background:none; color:var(--dim); font:inherit;
      padding:6px 14px; cursor:pointer; border-bottom:2px solid transparent; }
    .coord-tabs button.active { color:var(--text); border-bottom-color:var(--accent, #4f8cff); }
    .coord-pane { flex:1; min-height:0; display:flex; flex-direction:column; overflow:hidden; }
    .coord-pane[hidden] { display:none; }
    .coord-board-mount { flex:1; min-height:0; overflow:auto; padding:12px 18px; }
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

function showMessages(log, messages) {
  log.replaceChildren();
  for (const message of messages) {
    const row = document.createElement("article");
    row.className = "coord-chat-message";
    row.dataset.actor = message.actor;
    const label = document.createElement("small");
    label.textContent = message.actor === "human" ? "你" : "项目统筹";
    const content = document.createElement("p");
    content.textContent = message.content || "";
    row.append(label, content);
    log.appendChild(row);
  }
  if (!messages.length) log.textContent = "还没有对话。";
  log.scrollTop = log.scrollHeight;
}

export async function mountCoordinationPage(mount, wid) {
  stylePage();
  const work = `/api/works/${encodeURIComponent(wid)}`;
  const url = new URL(location.href);
  let selection = 0;

  async function load(preferred = url.searchParams.get("plan")) {
    const r = await api(`${work}/coordination`);
    if (!r.ok) throw new Error(r.body.error || "读取统筹计划失败");
    const plans = r.body.plans || [];
    mount.innerHTML = `<header class="coord-page-header">
      <label for="coordPagePlan">统筹计划</label>
      <select id="coordPagePlan">${plans.map((plan) =>
        `<option value="${esc(plan.plan_card_id)}">${esc(plan.title || plan.plan_card_id)}</option>`).join("")}
        <option value="">新任命统筹</option></select>
      </header><div class="coord-page-content"></div>`;
    const choose = mount.querySelector("#coordPagePlan");
    const defaultPlan = plans.find((p) => p.state === "active" || p.state === "paused") || plans[0];
    choose.value = plans.some((p) => p.plan_card_id === preferred) ? preferred : (defaultPlan?.plan_card_id || "");
    choose.onchange = () => showPlan(choose.value);
    await showPlan(choose.value);
  }

  async function showPlan(cid, draft = "", notice = "") {
    const serial = ++selection;
    const panel = mount.querySelector(".coord-page-content");
    panel.textContent = "读取中…";
    if (cid) url.searchParams.set("plan", cid);
    else url.searchParams.delete("plan");
    history.replaceState(history.state, "", url);
    try {
      if (!cid) {
        const r = await api(`${work}/canvas`);
        if (serial !== selection) return;
        if (!r.ok) throw new Error(r.body.error || "读取承载卡失败");
        const cards = (r.body.cards || []).filter((card) =>
          card.card_id !== r.body.work_card_id && !["project", "role"].includes(card.card_type)
          && !String(card.card_id).startsWith("fn_"));
        panel.innerHTML = `<div class="coord-page-setup">
          <label for="coordChatCarrier">承载计划的普通卡</label>
          <select id="coordChatCarrier"><option value="">请选择</option>
            ${cards.map((card) => `<option value="${esc(card.card_id)}">${esc(card.title || card.card_id)}</option>`).join("")}
          </select><div class="coord-chat-appoint"></div></div>`;
        const carrier = panel.querySelector("#coordChatCarrier");
        const form = panel.querySelector(".coord-chat-appoint");
        if (!cards.length) form.textContent = "请先建一张普通卡来承载计划。";
        carrier.onchange = () => {
          const selected = carrier.value;
          form.innerHTML = selected ? appointFormHtml(selected) : "";
          if (selected) bindAppointForm(form, wid, () => load(selected));
        };
        return;
      }

      const endpoint = `${work}/cards/${encodeURIComponent(cid)}/coordination`;
      const chatPath = `${endpoint}/chat`;
      const [plan, chat] = await Promise.all([api(endpoint), api(chatPath)]);
      if (serial !== selection) return;
      if (!plan.ok) throw new Error(plan.body.error || "读取统筹计划失败");
      if (!chat.ok) throw new Error(chat.body.error || "读取统筹对话失败");
      const appointment = plan.body;
      const active = appointment.state === "active";
      const configurable = active || appointment.state === "paused";
      panel.innerHTML = `
        <nav class="coord-tabs" role="tablist">
          <button type="button" data-tab="chat" class="active">对话</button>
          <button type="button" data-tab="board">计划板</button>
          <button type="button" data-tab="settings">模型设置</button>
        </nav>
        <section class="coord-pane" data-pane="chat">
          <div class="coord-chat-log" role="log" aria-label="统筹对话记录"></div>
          <form class="coord-chat-form">
            <textarea aria-label="给统筹发消息" rows="2" maxlength="12000"
              placeholder="${active ? "给统筹发消息" : "任命或恢复统筹后可发送消息"}" ${active ? "" : "disabled"}></textarea>
            <button class="btn primary" type="submit" ${active ? "" : "disabled"}>发送</button>
          </form>
          <div class="coord-chat-status" role="status"></div>
        </section>
        <section class="coord-pane" data-pane="board" hidden>
          <div class="coord-board-mount"></div>
        </section>
        <section class="coord-pane" data-pane="settings" hidden>
          <div class="coord-settings-body" style="overflow:auto;padding:14px 18px;">
            ${configurable ? `<form class="coord-models">
              <label>强模型<select name="strong" required></select></label>
              <label>弱模型<select name="weak" required></select></label>
              <button type="submit" class="btn" disabled>保存模型</button>
            </form><div class="coord-model-status" role="status"></div>` : ""}
            <div class="coord-page-plan" data-card-id="${esc(cid)}"></div>
          </div>
        </section>`;
      const log = panel.querySelector(".coord-chat-log");
      const form = panel.querySelector(".coord-chat-form");
      const input = form.querySelector("textarea");
      const send = form.querySelector("button");
      const status = panel.querySelector(".coord-chat-status");
      const planRegion = panel.querySelector(".coord-page-plan");
      const boardMount = panel.querySelector(".coord-board-mount");
      const tabs = panel.querySelectorAll(".coord-tabs button");
      const panes = panel.querySelectorAll(".coord-pane");
      const binding = { assignment_id: appointment.assignment_id, expected_rev: appointment.rev };
      const board = mountPlanBoard(boardMount, {
        endpoint, binding, wid,
        onSaved: (rec) => { binding.expected_rev = rec.rev; },
      });
      let boardLoaded = false;
      let settingsLoaded = false;
      const wantTab = url.searchParams.get("tab") || "";
      function activate(tabName) {
        for (const b of tabs) b.classList.toggle("active", b.dataset.tab === tabName);
        for (const p of panes) p.hidden = p.dataset.pane !== tabName;
        if (tabName === "board" && !boardLoaded) {
          boardLoaded = true;
          board.load().catch((e) => { boardMount.textContent = e.message; });
        } else if (tabName === "board") {
          board.load().catch(() => {});
        }
        if (tabName === "settings" && !settingsLoaded) {
          settingsLoaded = true;
          renderCardRegion(planRegion, () => showPlan(cid, input.value));
        }
      }
      for (const b of tabs) b.onclick = () => activate(b.dataset.tab);
      activate(["board", "settings"].includes(wantTab) ? wantTab : "chat");
      input.value = draft;
      status.textContent = notice;
      showMessages(log, chat.body.messages || []);
      form.onsubmit = async (event) => {
        event.preventDefault();
        const content = input.value.trim();
        if (!content || send.disabled) return;
        send.disabled = true;
        input.readOnly = true;
        status.textContent = "统筹正在回复…";
        try {
          const r = await api(chatPath, {
            method: "POST", body: JSON.stringify({ ...binding, content }),
          });
          if (serial !== selection) return;
          if (r.status === 409) {
            await showPlan(cid, input.value, r.body.error || "统筹状态已变化，请重试。");
            return;
          }
          if (!r.ok) throw new Error(r.body.error || "发送失败");
          input.value = "";
          const messages = await api(chatPath);
          if (!messages.ok) throw new Error("消息已发送，请刷新查看回复。");
          if (serial !== selection) return;
          showMessages(log, messages.body.messages || []);
          status.textContent = "";
          input.focus();
        } catch (error) {
          status.textContent = error.message;
        } finally {
          send.disabled = false;
          input.readOnly = false;
        }
      };
      input.onkeydown = (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          form.requestSubmit();
        }
      };
      const modelsForm = panel.querySelector(".coord-models");
      if (modelsForm) {
        const feedback = panel.querySelector(".coord-model-status");
        const save = modelsForm.querySelector("button");
        const fields = modelsForm.elements;
        loadModels().then((options) => {
          for (const key of ["strong", "weak"]) {
            const current = appointment.models?.[key] || "";
            for (const name of new Set([...options, current].filter(Boolean))) {
              fields[key].add(new Option(name, name, false, name === current));
            }
          }
          save.disabled = options.length === 0;
          if (save.disabled) feedback.textContent = "未取得可配置的模型。";
        }).catch(() => { feedback.textContent = "读取模型配置失败。"; });
        modelsForm.onsubmit = async (event) => {
          event.preventDefault();
          if (save.disabled) return;
          save.disabled = true;
          try {
            const models = { strong: fields.strong.value, weak: fields.weak.value };
            const r = await api(endpoint, {
              method: "POST", body: JSON.stringify({ ...binding, op: "set_models", models }),
            });
            if (serial !== selection) return;
            if (r.status === 409) {
              await showPlan(cid, input.value, r.body.error || "统筹状态已变化，请重试。");
            } else if (!r.ok) {
              throw new Error(r.body.error || "保存模型失败");
            } else {
              binding.expected_rev = r.body.rev;
              feedback.textContent = "强弱模型已保存。";
              await renderCardRegion(planRegion, () => showPlan(cid, input.value));
            }
          } catch (error) {
            feedback.textContent = error.message;
          } finally {
            save.disabled = false;
          }
        };
      }
    } catch (error) {
      if (serial === selection) panel.textContent = error.message;
    }
  }

  await load();
}
