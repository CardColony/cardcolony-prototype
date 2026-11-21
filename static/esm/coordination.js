/* 项目统筹（coordination，2026-11-21 任务一）——前端面板。
 *
 * 两条入口共用同一份数据与同一个任命表单（任务书口径）：
 *  · 画布页 work.html：顶栏「项目统筹」按钮 → 计划列表抽屉（#coordDrawer）。
 *    首次使用（无计划）在抽屉里选一张普通卡直接出任命表单；
 *    已有计划点计划名跳那张卡的对话页（卡详情里是同一份面板）。
 *  · 对话页 card.html：右侧卡详情（workspace.js renderInspector）的
 *    #coordRegion 容器 → renderCardRegion()。
 *
 * 本模块只做机械事实的呈现与提交：GET 面板/列表不唤醒模型；
 * appoint/pause/resume/revoke/finish/wake/set_mode/set_grant/PATCH 全部走
 * /api/works/{wid}/cards/{cid}/coordination 正式入口。
 * 打开列表、刷新、浏览历史均不产生模型调用（后端只读路径保证）。
 */

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
  .coord-overdue { color:#e05d5d; font-size:11px; }
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

/* datetime-local 值 → 带时区 ISO（浏览器本地时区）。 */
function localToIso(v) {
  if (!v) return "";
  const d = new Date(v);
  return isNaN(d.getTime()) ? "" : d.toISOString();
}

async function loadFunctions(wid) {
  const r = await _api(`/api/works/${wid}/functions`);
  return (r.body.functions || []).filter((f) =>
    String(f.function_id || "").startsWith("fn_"));
}

async function loadModels() {
  const r = await _api("/api/settings/models");
  return Object.keys((r.body.models || {})).sort();
}

/* ---------- 任命表单（列表抽屉与卡详情共用同一份） ---------- */

function appointFormHtml(cardId) {
  return `
  <div class="coord-form" data-card="${esc(cardId)}">
    <div><label>承载卡</label><input class="cf-card" value="${esc(cardId)}" readonly></div>
    <div><label>负责职能（第一期＝非根职能卡）</label><select class="cf-fn"><option value="">加载中…</option></select></div>
    <div><label>用户目标（这期计划要达成什么）</label><input class="cf-objective" placeholder="按现行需求推进底座重构，先核无卡 agent 接入"></div>
    <div><label>观察范围</label><select class="cf-obs">
      <option value="work">整个 work（含后来加入的卡）</option>
      <option value="cards">显式卡集合</option></select></div>
    <div class="cf-obs-ids" style="display:none"><label>观察卡号（逗号分隔）</label><input class="cf-obs-cards" placeholder="card_017,card_049"></div>
    <div><label>发令范围</label><select class="cf-ins">
      <option value="cards">显式目标卡集合</option>
      <option value="work_development_cards">本 work 全部开发卡</option></select></div>
    <div class="cf-ins-ids"><label>发令目标卡号（逗号分隔）</label><input class="cf-ins-cards" placeholder="card_demo_protocol"></div>
    <div style="display:flex;gap:8px">
      <div style="flex:1"><label>强模型（统筹判断）</label><select class="cf-strong"><option value="">加载中…</option></select></div>
      <div style="flex:1"><label>弱模型（观察判断）</label><select class="cf-weak"><option value="">加载中…</option></select></div>
    </div>
    <div style="display:flex;gap:8px">
      <div style="flex:1"><label>运行模式（默认旁观）</label><select class="cf-mode">
        <option value="observe">旁观 observe</option>
        <option value="live">实发 live</option></select></div>
      <div style="flex:1"><label>首次最迟回看时间</label><input class="cf-review" type="datetime-local"></div>
    </div>
    <div class="coord-btns"><button class="btn primary cf-submit">任命统筹</button></div>
    <div class="cf-msg"></div>
  </div>`;
}

function bindAppointForm(container, wid, onDone) {
  const form = container.querySelector(".coord-form");
  if (!form) return;
  const cardId = form.dataset.card || "";
  const msg = form.querySelector(".cf-msg");
  // 职能/模型候选
  loadFunctions(wid).then((fns) => {
    const sel = form.querySelector(".cf-fn");
    sel.innerHTML = fns.length
      ? fns.map((f) => `<option value="${esc(f.function_id)}">${esc(f.function_name)}（${esc(f.function_id)}）</option>`).join("")
      : `<option value="">（本 work 没有非根职能——先建职能）</option>`;
  });
  loadModels().then((names) => {
    for (const [cls, dft] of [[".cf-strong", names[0] || ""], [".cf-weak", names[1] || names[0] || ""]]) {
      const sel = form.querySelector(cls);
      sel.innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
      if (dft) sel.value = dft;
    }
  });
  form.querySelector(".cf-obs").addEventListener("change", (e) => {
    form.querySelector(".cf-obs-ids").style.display =
      e.target.value === "cards" ? "" : "none";
  });
  form.querySelector(".cf-ins").addEventListener("change", (e) => {
    form.querySelector(".cf-ins-ids").style.display =
      e.target.value === "cards" ? "" : "none";
  });
  form.querySelector(".cf-submit").addEventListener("click", async () => {
    msg.textContent = ""; msg.className = "cf-msg";
    const obsSel = form.querySelector(".cf-obs").value;
    const insSel = form.querySelector(".cf-ins").value;
    const idsOf = (raw) => String(raw || "").split(/[,\s，]+/).map((s) => s.trim()).filter(Boolean);
    const payload = {
      op: "appoint",
      function_id: form.querySelector(".cf-fn").value,
      objective: form.querySelector(".cf-objective").value.trim(),
      observe: obsSel === "work"
        ? { scope: "work" }
        : { scope: "cards", ids: idsOf(form.querySelector(".cf-obs-cards").value) },
      instruct: insSel === "work_development_cards"
        ? { scope: "work_development_cards" }
        : { scope: "cards", ids: idsOf(form.querySelector(".cf-ins-cards").value) },
      models: {
        strong: form.querySelector(".cf-strong").value,
        weak: form.querySelector(".cf-weak").value,
      },
      mode: form.querySelector(".cf-mode").value,
      next_review_at: localToIso(form.querySelector(".cf-review").value),
    };
    const r = await _api(`/api/works/${wid}/cards/${cardId}/coordination`,
      { method: "POST", body: JSON.stringify(payload) });
    if (!r.ok) {
      msg.textContent = r.body.error || `任命失败（HTTP ${r.status}）`;
      msg.className = "coord-err";
      return;
    }
    msg.textContent = "已任命。统筹首次醒来会拟定计划与唤醒标准。";
    msg.className = "coord-ok";
    if (onDone) setTimeout(onDone, 600);
  });
}

/* ---------- 卡详情区块 ---------- */

const STATE_LABEL = {
  active: "活跃", paused: "已暂停", revoked: "已撤回",
  finished: "已结束", none: "未任命",
};

export async function renderCardRegion(mount) {
  if (!mount) return;
  injectStyle();
  const wid = window.__WORK_ID__ || "";
  const cid = mount.dataset.cardId || "";
  if (!wid || !cid || cid === "draft") return;
  const r = await _api(`/api/works/${wid}/cards/${cid}/coordination?history=1`);
  if (!r.ok) { mount.innerHTML = `<div class="coord-err">面板读取失败：${esc(r.body.error || r.status)}</div>`; return; }
  const p = r.body;
  if (p.state === "none") {
    mount.innerHTML = `
      <div class="blocktitle">统筹计划 <span class="note">把「项目统筹」差事任命给一张职能卡</span></div>
      <div class="coord-empty">这张卡还没有任命统筹。填写下面表单后，强模型按计划持续判偏、协调与发令；弱模型按它写的标准替它盯变化。</div>
      ${appointFormHtml(cid)}`;
    bindAppointForm(mount, wid, () => renderCardRegion(mount));
    return;
  }
  // 有任命
  const g = p.grants || {};
  const obsTxt = (g.observe || {}).scope === "work"
    ? "整个 work" : `卡集合 ${(g.observe || {}).ids || []}`;
  const insTxt = (g.instruct || {}).scope === "work_development_cards"
    ? "全部开发卡" : `卡集合 ${(g.instruct || {}).ids || []}`;
  const st = p.state;
  const btns = [];
  if (st === "active") {
    btns.push(`<button class="btn" data-op="pause">暂停</button>`);
    btns.push(`<button class="btn" data-op="wake">点名唤醒</button>`);
    btns.push(`<button class="btn" data-op="set_mode" data-mode="${p.mode === "live" ? "observe" : "live"}">切为${p.mode === "live" ? "旁观" : "实发"}</button>`);
  }
  if (st === "paused") {
    btns.push(`<button class="btn" data-op="resume">恢复</button>`);
  }
  if (st === "active" || st === "paused") {
    btns.push(`<button class="btn" data-op="revoke">撤回</button>`);
    btns.push(`<button class="btn" data-op="finish">结束</button>`);
  }
  const planJson = JSON.stringify(p.plan || {}, null, 2);
  const pendingTxt = (p.pending || []).length
    ? (p.pending || []).map((x) => x.kind === "user_wake"
        ? `· 点名唤醒已排队（${fmtTs(x.at)}）`
        : `· ${esc(x.kind)} @ ${fmtTs(x.at)}`).join("\n")
    : "（无待办）";
  const review = p.last_review || {};
  const wakeLabels = { appoint: "首次任命", user_wake: "用户点名", direct_node: "节点达成",
    weak_wake: "观察提请", review_due: "到期回看" };
  const turnLabels = { parsed: "已完成", unparsed: "结果未通过", error: "失败", skipped: "已跳过" };
  const lastTurn = p.last_turn?.trigger ? `最近回合 ${esc(wakeLabels[p.last_turn.trigger] || p.last_turn.trigger)} · ${esc(turnLabels[p.last_turn.status] || p.last_turn.status)} · ${fmtTs(p.last_turn.at)}<br>` : "";
  const lastReview = review.completed_at ? `<details class="coord-kv">
    <summary>上次核查 ${fmtTs(review.completed_at)}</summary>
    <div>${esc(review.conclusion || "")}</div>
    ${(review.open_items || []).map((item) => `<div>待核查 · ${esc(item)}</div>`).join("")}
    <pre>${esc(JSON.stringify(review.evidence || [], null, 2))}</pre>
  </details>` : "";
  // [历史唯一定位 2026-11-21 · 用户令] 版本史条目带修订记录主键
  // history_id——撤回后重新任命会出现两个 rev.0，按主键回读才能把
  // 前后两次的正文都打开。
  const histHtml = (p.history || []).map((h) =>
    `<a href="javascript:void(0)" data-hid="${Number(h.history_id)}" class="coord-hist-link" title="任命 ${esc(h.assignment_id || "")}">rev.${Number(h.rev)}</a>`
  ).join(" · ") || "—";
  // [重新任命 2026-11-21 · 用户令] 撤回/结束后同一张卡可以直接重新任命
  // （新任命标识；历史版本仍可回看）。
  if (st === "revoked" || st === "finished") {
    mount.innerHTML = `
      <div class="blocktitle">统筹计划 <span class="note">负责人 ${esc(p.function_id)} · 计划载体＝本卡</span></div>
      <div class="coord-block">
        <div class="coord-head">
          <span class="coord-state" data-state="${esc(st)}">${esc(STATE_LABEL[st] || st)}</span>
          <span class="coord-kv">上次任命 ${esc(p.assignment_id)} · 终止于 rev.${Number(p.rev || 0)}</span>
        </div>
        <div class="coord-hist" id="coordHist">版本史：${histHtml}</div>
        <div id="coordRevView"></div>
        <div class="coord-empty">上次任命已${st === "revoked" ? "撤回" : "结束"}。可重新任命（产生新任命标识；历史版本仍可回看）。</div>
        ${appointFormHtml(cid)}
      </div>`;
    bindAppointForm(mount, wid, () => renderCardRegion(mount));
    bindHistoryLinks(mount, wid, cid);
    return;
  }
  mount.innerHTML = `
    <div class="blocktitle">统筹计划 <span class="note">负责人 ${esc(p.function_id)} · 计划载体＝本卡</span></div>
    <div class="coord-block">
      <div class="coord-head">
        <span class="coord-state" data-state="${esc(st)}">${esc(STATE_LABEL[st] || st)}</span>
        <span class="coord-state">${esc(p.mode === "live" ? "实发" : "旁观")}</span>
        <span class="coord-overdue">${p.overdue ? "⚠ 回看已到期欠办" : ""}</span>
      </div>
      <div class="coord-kv">
        任命 ${esc(p.assignment_id)} · <span id="coordRevLabel">计划版本 rev.${Number(p.rev || 0)}</span> · 到期 ${fmtTs(p.next_review_at)}<br>
        观察 ${esc(obsTxt)} ｜ 发令 ${esc(insTxt)}<br>
        强模型 ${esc((p.models || {}).strong || "—")} ｜ 弱模型 ${esc((p.models || {}).weak || "—")}<br>
        ${lastTurn}
        待办：<span style="white-space:pre-wrap">${pendingTxt}</span>
      </div>
      ${lastReview}
      <div class="coord-btns">${btns.join("")}<button class="btn" id="coordEditPlan">编辑计划</button></div>
      <label>提前回看 <input type="datetime-local" id="coordEarlierAt"></label>
      <button class="btn" data-op="set_review_at">保存回看时间</button>
      <div class="cf-msg"></div>
      <div class="coord-plan" id="coordPlanView">${esc(planJson)}</div>
      <textarea class="coord-form" id="coordPlanEdit" style="display:none">${esc(planJson)}</textarea>
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
      if (btn.dataset.mode) payload.mode = btn.dataset.mode;
      if (btn.dataset.op === "set_review_at") {
        payload.next_review_at = localToIso(mount.querySelector("#coordEarlierAt").value);
        if (!payload.next_review_at) {
          msg.textContent = "请选择回看时间";
          return;
        }
      }
      if (btn.dataset.op !== "wake") payload.expected_rev = planRev;
      const rr = await _api(`/api/works/${wid}/cards/${cid}/coordination`,
        { method: "POST", body: JSON.stringify(payload) });
      if (!rr.ok) {
        if (rr.status === 409) {
          msg.textContent = (rr.body.error || "版本已过期") + "——面板已刷新到最新状态，请重试";
          msg.className = "coord-err";
          renderCardRegion(mount);
          return;
        }
        msg.textContent = rr.body.error || `操作失败（${rr.status}）`;
        msg.className = "coord-err";
        return;
      }
      if (btn.dataset.op === "wake") {
        msg.textContent = "唤醒已排队";
        msg.className = "coord-ok";
        renderCardRegion(mount);
        return;
      }
      renderCardRegion(mount);
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
    const rr = await _api(`/api/works/${wid}/cards/${cid}/coordination`,
      { method: "PATCH", body: JSON.stringify({ assignment_id: p.assignment_id, expected_rev: planRev, plan }) });
    if (rr.status === 409) {
      // 版本过期：按任务书要求返回最新版本并要求重读——同步填入最新正文
      // **和最新版本号**，用户核对后直接再点保存即按新版本提交。
      edit.value = JSON.stringify(rr.body.plan || {}, null, 2);
      syncRev(rr.body.current_rev != null ? rr.body.current_rev : planRev);
      msg.textContent = (rr.body.error || "计划版本已过期")
        + "——已填入最新正文并更新提交版本，请核对后再次保存";
      msg.className = "coord-err";
      return;
    }
    if (!rr.ok) { msg.textContent = rr.body.error || `保存失败（${rr.status}）`; msg.className = "coord-err"; return; }
    renderCardRegion(mount);
  });
  // [历史正文回读 2026-11-21 · 用户令] 版本史条目可点开该版本完整正文。
  bindHistoryLinks(mount, wid, cid);
}

/* 版本史条目点击 → 按修订记录主键 history_id 回读该版完整正文
 * （跨任命 rev 撞号后仍能唯一定位每一条历史记录）。 */
function bindHistoryLinks(mount, wid, cid) {
  mount.querySelectorAll(".coord-hist-link").forEach((a) => {
    a.addEventListener("click", async () => {
      const hid = Number(a.dataset.hid);
      const rr = await _api(`/api/works/${wid}/cards/${cid}/coordination?history_id=${hid}`);
      const box = mount.querySelector("#coordRevView");
      if (!box) return;
      if (!rr.ok) { box.innerHTML = `<div class="coord-err">${esc(rr.body.error || "读取失败")}</div>`; return; }
      box.innerHTML = `
        <div class="coord-kv">rev.${Number(rr.body.rev)}（任命 ${esc(rr.body.assignment_id || "")}）· 保存于 ${fmtTs(rr.body.saved_at)} · ${esc(rr.body.saved_by || "")}
          <a href="javascript:void(0)" id="coordRevClose">收起</a></div>
        <div class="coord-plan">${esc(JSON.stringify(rr.body.plan || {}, null, 2))}</div>`;
      box.querySelector("#coordRevClose").addEventListener("click", () => { box.innerHTML = ""; });
    });
  });
}

/* ---------- 画布页：顶栏按钮 + 计划列表抽屉 ---------- */

export function mountCanvasCoordination() {
  injectStyle();
  const wid = window.__WORK_ID__ || "";
  const btn = document.getElementById("coordBtn");
  const drawer = document.getElementById("coordDrawer");
  const list = document.getElementById("coordList");
  if (!btn || !drawer || !list || !wid) return;

  const setOpen = (on) => {
    drawer.classList.toggle("is-open", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.classList.toggle("is-on", on);
    if (on) renderList();
  };
  btn.addEventListener("click", () =>
    setOpen(!drawer.classList.contains("is-open")));
  const closeBtn = document.getElementById("coordClose");
  if (closeBtn) closeBtn.addEventListener("click", () => setOpen(false));

  async function renderList() {
    const r = await _api(`/api/works/${wid}/coordination`);
    const plans = (r.body.plans || []).filter((x) =>
      x.state === "active" || x.state === "paused"
      || x.state === "revoked" || x.state === "finished");
    if (!plans.length) {
      list.innerHTML = `
        <div class="coord-empty">
          本 work 还没有任命统筹。<br>
          第一步：选一张承载计划的普通卡 ——
          <select id="coordPickCard" style="width:100%;margin-top:6px;font-size:12px;padding:4px 6px;background:var(--panel,rgba(127,127,127,.06));color:var(--fg,inherit);border:1px solid var(--line,#555);border-radius:5px;">
            <option value="">加载中…</option></select>
        </div>
        <div id="coordDrawerForm"></div>`;
      const cr = await _api(`/api/works/${wid}/canvas`);
      const sel = list.querySelector("#coordPickCard");
      const cards = (cr.body.cards || [])
        .filter((c) => String(c.card_id || "") !== String(cr.body.work_card_id || ""))
        .map((c) => ({ id: c.card_id, title: c.title }));
      sel.innerHTML = cards.length
        ? `<option value="">＋ 任命统筹：选承载卡…</option>` +
          cards.map((c) => `<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.title)}</option>`).join("")
        : `<option value="">（本 work 还没有普通卡）</option>`;
      sel.addEventListener("change", () => {
        const formMount = list.querySelector("#coordDrawerForm");
        if (!sel.value) { formMount.innerHTML = ""; return; }
        formMount.innerHTML = appointFormHtml(sel.value);
        bindAppointForm(list, wid, () => renderList());
      });
      return;
    }
    list.innerHTML = plans.map((x) => `
      <div class="coord-list-item" data-card="${esc(x.plan_card_id)}">
        <div class="t">${esc(x.plan_card_id)} <span class="coord-state" data-state="${esc(x.state)}">${esc(STATE_LABEL[x.state] || x.state)}</span></div>
        <div class="s">负责人 ${esc(x.function_id)} · ${esc(x.mode === "live" ? "实发" : "旁观")} · rev.${Number(x.rev || 0)} · 到期 ${fmtTs(x.next_review_at)}${x.overdue ? ' <span class="coord-overdue">⚠ 欠办</span>' : ""}</div>
      </div>`).join("");
    list.querySelectorAll(".coord-list-item").forEach((item) => {
      item.addEventListener("click", () => {
        window.location.href = `/work/${wid}/card/${item.dataset.card}`;
      });
    });
  }
}
