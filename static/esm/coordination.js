/* 项目统筹的状态、计划版本和发令回执面板。 */

let _styleInjected = false;
function injectStyle() {
  if (_styleInjected) return;
  _styleInjected = true;
  const st = document.createElement("style");
  st.id = "coordStyle";
  st.textContent = `
  .coord-block { margin: 10px 0 18px; }
  .coord-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .coord-state { font-size:11px; padding:1px 8px; border-radius:9px; border:1px solid var(--line, #555); }
  .coord-state[data-state="active"]   { color:#59c77e; border-color:#59c77e; }
  .coord-state[data-state="paused"]   { color:#d8a24a; border-color:#d8a24a; }
  .coord-state[data-state="revoked"]  { color:var(--dimmer, #888); }
  .coord-state[data-state="finished"] { color:var(--dimmer, #888); }
  .coord-state[data-state="none"]     { color:var(--dimmer, #888); }
  .coord-kv { font-size:11px; color:var(--dimmer, #888); margin-top:4px; line-height:1.7; }
  .coord-btns { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
  .coord-btns .btn { padding:2px 10px; font-size:12px; }
  .coord-plan { white-space:pre-wrap; font-size:12px; background:var(--panel-2, rgba(127,127,127,.08));
                border:1px solid var(--line, #444); border-radius:6px; padding:8px; max-height:200px; overflow:auto; }
  .coord-form { display:flex; flex-direction:column; gap:6px; margin-top:8px; }
  .coord-form label { font-size:11px; color:var(--dimmer, #888); display:block; margin-top:2px; }
  .coord-form input, .coord-form select, .coord-form textarea {
    width:100%; box-sizing:border-box; font-size:12px; padding:4px 6px;
    background:var(--panel, rgba(127,127,127,.06)); color:var(--fg, inherit);
    border:1px solid var(--line, #555); border-radius:5px; }
  .coord-form textarea { min-height:120px; font-family:var(--mono, monospace); }
  .coord-err { color:#e05d5d; font-size:11px; margin-top:4px; white-space:pre-wrap; }
  .coord-ok   { color:#59c77e; font-size:11px; margin-top:4px; }
  .coord-list-item { padding:8px 10px; border-bottom:1px solid var(--line, #333); cursor:pointer; }
  .coord-list-item:hover { background:var(--panel-2, rgba(127,127,127,.08)); }
  .coord-list-item .t { font-size:13px; }
  .coord-list-item .s { font-size:11px; color:var(--dimmer, #888); margin-top:2px; }
  .coord-empty { padding:16px 10px; font-size:12px; color:var(--dimmer, #888); line-height:1.8; }
  .coord-hist { font-size:11px; color:var(--dimmer, #888); }
  `;
  document.head.appendChild(st);
}

function _api(path, opts) {
  return fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" },
  }, opts || {})).then(async (r) => {
    let body = null;
    try { body = await r.json(); } catch (e) { body = {}; }
    return { status: r.status, ok: r.ok, body: body || {} };
  });
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return esc(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------- 卡详情区块 ---------- */

const STATE_LABEL = {
  active: "活跃", paused: "已暂停", revoked: "已撤回",
  finished: "已结束", none: "未配置",
};

export async function renderCardRegion(mount, onChange, endpoint) {
  if (!mount) return;
  const refresh = () => onChange ? onChange() : renderCardRegion(mount, null, endpoint);
  injectStyle();
  const wid = window.__WORK_ID__ || "";
  if (!wid || !endpoint) return;
  const r = await _api(`${endpoint}?history=1`);
  if (!r.ok) {
    mount.textContent = r.body.error || "统筹设置读取失败";
    return;
  }
  const p = r.body;
  if (p.state === "none") return;
  const inspectionStatus = {
    ready: "等待 Harness 条件命中",
    waiting_policy: "在统筹对话里请它制定巡检策略和唤醒条件",
    missing_models: "请重新选择强模型和弱模型",
    paused: "已暂停",
    harness_off: "Harness 已关闭",
  }[p.inspection_status];
  const g = p.grants || {};
  const obsTxt = ["work", "project"].includes((g.observe || {}).scope)
    ? "整个项目" : `卡集合 ${(g.observe || {}).ids || []}`;
  const insTxt = ["work_development_cards", "project_cards"].includes((g.instruct || {}).scope)
    ? "本项目全部卡" : `卡集合 ${(g.instruct || {}).ids || []}`;
  const st = p.state;
  const btns = [];
  if (st === "active") {
    btns.push(`<button class="btn" data-op="pause">暂停</button>`);
    btns.push(`<button class="btn" data-op="wake">点名唤醒</button>`);
  }
  if (st === "paused") {
    btns.push(`<button class="btn" data-op="resume">恢复</button>`);
  }
  const planJson = JSON.stringify(p.plan || {}, null, 2);
  const editablePlan = (plan) => Object.fromEntries(
    Object.entries(plan || {}).filter(([key]) => key !== "items"));
  const pendingLabels = { user_wake: "点名唤醒已排队", weak_wake: "等待强模型接手",
    unjudged_batch: "巡检未完成" };
  const pendingTxt = (p.pending || []).map((entry) => {
    const reason = entry.reason ? ` · ${esc(entry.reason)}` : "";
    return `· ${esc(pendingLabels[entry.kind] || "待处理事项")}（${fmtTs(entry.at)}）${reason}`;
  }).join("\n") || "（无待办）";
  const wakeLabels = { user_wake: "用户点名", weak_wake: "巡检提请" };
  const turnLabels = { parsed: "已完成", unparsed: "结果未通过", error: "失败", skipped: "已跳过" };
  const lastTurn = wakeLabels[p.last_turn?.trigger] ? `最近回合 ${esc(wakeLabels[p.last_turn.trigger])} · ${esc(turnLabels[p.last_turn.status] || p.last_turn.status)} · ${fmtTs(p.last_turn.at)}<br>` : "";
  // [历史唯一定位 2026-11-21 · 用户令] 版本史条目带修订记录主键
  // history_id——撤回后重新任命会出现两个 rev.0，按主键回读才能把
  // 前后两次的正文都打开。
  // [下一批 ③] 行动/专业答复/QA 回执只读视图（面板 GET 已带三组摘要）
  const actionsHtml = (p.actions || []).length
    ? (p.actions || []).map((a) => `<div class="coord-kv">
        ${esc(a.status)} · 事项 ${esc(a.item_id || "—")} · 目标 ${esc(a.target_card)}
        · rev.${Number(a.rev || 0)} · 验收判据 ${esc(a.acceptance || "（缺项）")}
        · 消息号 ${esc(a.msg_id || "—")}${a.woke ? " · 已唤醒" : ""}
        ${(a.execution_evidence || []).map((ev) => `<div class="coord-kv">
           执行证据 · 回执 ${esc(ev.reply_msg_id)}
           <a href="/work/${encodeURIComponent(wid)}/card/${encodeURIComponent(ev.reply_session_card || a.target_card)}?msg=${encodeURIComponent(ev.reply_msg_id)}"
              target="_blank" rel="noopener">打开原件（收件 ${esc(ev.reply_session_card || a.target_card)} · ${esc(ev.reply_msg_id)}）</a>
           <div>${esc(ev.content || "")}</div>
         </div>`).join("")}
      </div>`).join("")
    : `<div class="coord-kv">尚无行动</div>`;
  const questionsHtml = (p.questions || []).length
    ? (p.questions || []).map((q) => `<div class="coord-kv">
        ${esc(q.status)} · 职能 ${esc(q.function_id)} · 事项 ${esc(q.item_id || "—")}
        · 需求版本 ${esc(q.requirements_version || "—")}
        <div>${esc(q.answer || "（尚未取得答复）")}</div>
      </div>`).join("")
    : `<div class="coord-kv">尚无专业问答</div>`;
  const qaHtml = (p.qa || []).length
    ? (p.qa || []).map((q) => `<div class="coord-kv">
        QA ${esc(q.verdict || "未验证")} · 事项 ${esc(q.item_id || "—")}
        · 对象 ${esc(q.target_card)} · 编号 ${esc(q.qa_id)}
        <div>${esc(q.conclusion || "（未取得 QA 结论）")}</div>
        <pre>${esc(JSON.stringify(q.evidence_refs || [], null, 2))}</pre>
      </div>`).join("")
    : `<div class="coord-kv">尚无 QA 回执</div>`;
  const extrasHtml = `<details class="coord-kv"><summary>行动与收件</summary>${actionsHtml}</details>
    <details class="coord-kv"><summary>专业答复</summary>${questionsHtml}</details>
    <details class="coord-kv"><summary>QA 验收回执</summary>${qaHtml}</details>`;
  const histHtml = (p.history || []).map((h) =>
    `<a href="javascript:void(0)" data-hid="${Number(h.history_id)}" class="coord-hist-link">rev.${Number(h.rev)}</a>`
  ).join(" · ") || "—";
  mount.innerHTML = `
    <div class="blocktitle">运行设置</div>
    <div class="coord-block">
      <div class="coord-head">
        <span class="coord-state" data-state="${esc(st)}">${esc(STATE_LABEL[st] || st)}</span>
      </div>
      <div class="coord-kv">
        <span id="coordRevLabel">计划版本 rev.${Number(p.rev || 0)}</span><br>
        观察 ${esc(obsTxt)} ｜ 发令 ${esc(insTxt)}<br>
        强模型 ${esc((p.models || {}).strong || "—")} ｜ 弱模型 ${esc((p.models || {}).weak || "—")}<br>
        ${inspectionStatus ? `巡检：${inspectionStatus}<br>` : ""}
        ${lastTurn}
        待办：<span style="white-space:pre-wrap">${pendingTxt}</span>
      </div>
      ${extrasHtml}
      <div class="coord-btns">${btns.join("")}<button class="btn" id="coordEditPlan">编辑计划</button></div>
      <div class="cf-msg"></div>
      <div class="coord-plan" id="coordPlanView">${esc(planJson)}</div>
      <textarea class="coord-form" id="coordPlanEdit" style="display:none">${esc(JSON.stringify(editablePlan(p.plan), null, 2))}</textarea>
      <div class="coord-btns" id="coordPlanEditBtns" style="display:none">
        <button class="btn primary" id="coordPlanSave">保存计划</button>
        <button class="btn" id="coordPlanCancel">取消</button>
      </div>
      <div class="coord-hist" id="coordHist">版本史：${histHtml}</div>
      <div id="coordRevView"></div>
    </div>`;
  const msg = mount.querySelector(".cf-msg");
  // [读入版本闭环 2026-11-21 · 用户令] 可变的提交版本：409 时随最新正文
  // 一并更新，用户核对后**在同一面板**再次保存即可成功（不必刷新页面）。
  let planRev = Number(p.rev || 0);
  const revLabel = mount.querySelector("#coordRevLabel");
  const saveBtn = mount.querySelector("#coordPlanSave");
  const syncRev = (rv) => {
    planRev = Number(rv);
    if (revLabel) revLabel.textContent = `计划版本 rev.${planRev}`;
    if (saveBtn) saveBtn.textContent = `保存计划（rev.${planRev} → rev.${planRev + 1}）`;
  };
  // 状态操作（变更类控制请求带读入版本；过期 409 自动刷新面板拿最新状态）
  mount.querySelectorAll("[data-op]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      msg.textContent = ""; msg.className = "cf-msg";
      const payload = { op: btn.dataset.op, assignment_id: p.assignment_id };
      if (btn.dataset.op !== "wake") payload.expected_rev = planRev;
      const rr = await _api(endpoint,
        { method: "POST", body: JSON.stringify(payload) });
      if (!rr.ok) {
        if (rr.status === 409) {
          msg.textContent = (rr.body.error || "版本已过期") + "——面板已刷新到最新状态，请重试";
          msg.className = "coord-err";
          refresh();
          return;
        }
        msg.textContent = rr.body.error || `操作失败（${rr.status}）`;
        msg.className = "coord-err";
        return;
      }
      if (btn.dataset.op === "wake") {
        msg.textContent = "唤醒已排队";
        msg.className = "coord-ok";
        refresh();
        return;
      }
      refresh();
    });
  });
  // 计划编辑（提交读入版本；冲突返回最新版本＋最新版本号，原面板可再次保存）
  const view = mount.querySelector("#coordPlanView");
  const edit = mount.querySelector("#coordPlanEdit");
  const editBtns = mount.querySelector("#coordPlanEditBtns");
  mount.querySelector("#coordEditPlan").addEventListener("click", () => {
    view.style.display = "none"; edit.style.display = ""; editBtns.style.display = "";
  });
  mount.querySelector("#coordPlanCancel").addEventListener("click", () => {
    view.style.display = ""; edit.style.display = "none"; editBtns.style.display = "none";
  });
  mount.querySelector("#coordPlanSave").addEventListener("click", async () => {
    msg.textContent = ""; msg.className = "cf-msg";
    let plan;
    try { plan = JSON.parse(edit.value); }
    catch (e) { msg.textContent = `计划不是合法 JSON：${e.message}`; msg.className = "coord-err"; return; }
    const rr = await _api(endpoint,
      { method: "PATCH", body: JSON.stringify({ assignment_id: p.assignment_id, expected_rev: planRev, plan }) });
    if (rr.status === 409) {
      // 版本过期：按任务书要求返回最新版本并要求重读——同步填入最新正文
      // **和最新版本号**，用户核对后直接再点保存即按新版本提交。
      edit.value = JSON.stringify(editablePlan(rr.body.plan), null, 2);
      syncRev(rr.body.current_rev != null ? rr.body.current_rev : planRev);
      msg.textContent = (rr.body.error || "计划版本已过期")
        + "——已填入最新正文并更新提交版本，请核对后再次保存";
      msg.className = "coord-err";
      return;
    }
    if (!rr.ok) { msg.textContent = rr.body.error || `保存失败（${rr.status}）`; msg.className = "coord-err"; return; }
    refresh();
  });
  // [历史正文回读 2026-11-21 · 用户令] 版本史条目可点开该版本完整正文。
  bindHistoryLinks(mount, endpoint);
}
export { _api as coordinationApi, esc };

/* 版本史条目点击 → 按修订记录主键 history_id 回读该版完整正文
 * （跨任命 rev 撞号后仍能唯一定位每一条历史记录）。 */
function bindHistoryLinks(mount, endpoint) {
  mount.querySelectorAll(".coord-hist-link").forEach((a) => {
    a.addEventListener("click", async () => {
      const hid = Number(a.dataset.hid);
      const rr = await _api(`${endpoint}?history_id=${hid}`);
      const box = mount.querySelector("#coordRevView");
      if (!box) return;
      if (!rr.ok) { box.innerHTML = `<div class="coord-err">${esc(rr.body.error || "读取失败")}</div>`; return; }
      box.innerHTML = `
        <div class="coord-kv">rev.${Number(rr.body.rev)} · 保存于 ${fmtTs(rr.body.saved_at)} · ${esc(rr.body.saved_by || "")}
          <a href="javascript:void(0)" id="coordRevClose">收起</a></div>
        <div class="coord-plan">${esc(JSON.stringify(rr.body.plan || {}, null, 2))}</div>`;
      box.querySelector("#coordRevClose").addEventListener("click", () => { box.innerHTML = ""; });
    });
  });
}
