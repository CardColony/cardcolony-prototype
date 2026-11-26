// [2026-11-23 · 统筹计划板任务书] 「项目统筹 → 计划板」页签：
// 同一份 plan.items 的结构化编辑。读走 GET 面板（board 投影，只读不唤醒），
// 写走 POST op=board_apply（与强模型 coord_plan_board 同一版本保存入口）。
import { coordinationApi as api, esc } from "./coordination.js?v=6";

const STATUS_LABEL = { todo: "待办", doing: "进行中", blocked: "受阻", done: "完成" };

function itemEditor(it, isAdd) {
  const deps = (it.depends || []).join(",");
  const reqs = (it.requirement_refs || []).join(",");
  const evs = (it.evidence_refs || []).join(",");
  return `<form class="board-item-edit" data-item="${esc(it.item_id || "")}">
    <label>标题<input name="title" value="${esc(it.title || "")}" required></label>
    <label>优先级<input name="priority" type="number" min="1" step="1"
      value="${esc(String(it.priority || 100))}" required></label>
    <label>状态<select name="status">${Object.keys(STATUS_LABEL).map((s) =>
      `<option value="${s}"${it.status === s ? " selected" : ""}>${STATUS_LABEL[s]}</option>`).join("")}
    </select></label>
    <label>责任卡<input name="owner_card" value="${esc(it.owner_card || "")}"
      placeholder="空＝归属待核"></label>
    <label class="board-wide">下一结果<input name="next_result"
      value="${esc(it.next_result || "")}"></label>
    ${isAdd ? "" : `<label>依赖事项（逗号分隔 ID）<input name="depends"
      value="${esc(deps)}"></label>`}
    <label>需求引用（逗号分隔）<input name="requirement_refs" placeholder="需求卡号或已保存的版本坐标" value="${esc(reqs)}"></label>
    <label>证据坐标（逗号分隔）<input name="evidence_refs" placeholder="receipt:cmsg-… 或 event:…" value="${esc(evs)}"></label>
    <label class="board-wide">完成/受阻说明<input name="result_note"
      value="${esc(it.result_note || "")}" placeholder="标完成必填：写明完成依据"></label>
    <label>调整理由<input name="reason" value=""
      placeholder="本次变更理由（入历史）"></label>
    <div class="board-edit-actions">
      <button type="submit" class="btn primary">${isAdd ? "新增" : "保存"}</button>
      <button type="button" class="btn board-cancel">取消</button>
    </div>
  </form>`;
}

function itemRow(it, wid = "", requirements = {}, receipts = {}) {
  const owner = it.owner_card
    ? `<span class="board-owner">${esc(it.owner_card)}</span>`
    : '<span class="board-owner board-owner-miss">责任卡待核</span>';
  const dep = (it.depends || []).length
    ? `<span class="board-dep" title="依赖">⇐${esc(it.depends.join(","))}</span>` : "";
  return `<article class="board-item" data-item="${esc(it.item_id)}" data-status="${esc(it.status)}">
    <header class="board-item-head">
      <span class="board-prio">P${esc(String(it.priority))}</span>
      <span class="board-status board-status-${esc(it.status)}">${STATUS_LABEL[it.status] || it.status}</span>
      <span class="board-title">${esc(it.title)}</span>
      ${owner}${dep}
      <span class="board-item-id">${esc(it.item_id)}</span>
      <span class="board-actions">
        <button type="button" class="btn board-edit-btn">编辑</button>
        ${it.status === "done"
          ? '<button type="button" class="btn board-reopen-btn">重开</button>'
          : '<button type="button" class="btn board-done-btn">标完成</button>'}
      </span>
    </header>
    <div class="board-item-body">
      ${it.next_result ? `<div class="board-line">下一结果：${esc(it.next_result)}</div>` : ""}
      ${it.result_note ? `<div class="board-line">说明：${esc(it.result_note)}</div>` : ""}
      ${(it.requirement_refs || []).map((ref) => {
        const source = requirements[ref];
        return source ? `<details class="board-line"><summary>需求原文 · ${esc(ref)}</summary>
          <pre class="coord-plan">${esc(source.text)}</pre></details>`
          : `<div class="board-line board-dim">${esc(ref)}</div>`;
      }).join("")}
      ${(it.evidence_refs || []).map((ref) => {
        const receipt = receipts[ref];
        return receipt && wid ? `<div class="board-line"><a href="/work/${encodeURIComponent(wid)}/card/${encodeURIComponent(receipt)}?msg=${encodeURIComponent(ref.slice(8))}"
          target="_blank" rel="noopener">打开执行回执 · ${esc(ref.slice(8))}</a></div>`
          : `<div class="board-line board-dim">${esc(ref)}</div>`;
      }).join("")}
    </div>
    <div class="board-editor-slot" hidden></div>
  </article>`;
}

function csv(text) {
  return String(text || "").split(",").map((x) => x.trim()).filter(Boolean);
}

export function mountPlanBoard(mount, { endpoint, binding, onSaved, wid }) {
  let editing = null;   // 冲突时保留的用户编辑现场
  let lastBody = null;
  let awaitingCurrent = false;

  function board() { return (lastBody && lastBody.board) || { items: [], rev: lastBody?.rev || 0 }; }
  function rev() { return board().rev || 0; }

  async function load() {
    const r = await api(endpoint);
    if (!r.ok) throw new Error(r.body.error || "读取计划板失败");
    lastBody = r.body;
    awaitingCurrent = false;
    render();
  }

  function render(notice = "") {
    const items = board().items || [];
    const open = items.filter((it) => it.status !== "done");
    const done = items.filter((it) => it.status === "done");
    const receipts = Object.fromEntries((lastBody?.actions || []).flatMap((action) =>
      (action.execution_evidence || []).filter((ev) => ev.reply_msg_id && ev.reply_session_card)
        .map((ev) => [`receipt:${ev.reply_msg_id}`, ev.reply_session_card])));
    const renderItem = (it) => itemRow(it, wid, board().requirements || {}, receipts);
    mount.innerHTML = `
      <div class="board-head">
        <span class="board-rev">rev.${rev()}</span>
        <span>未完成 ${open.length}｜已完成 ${done.length}</span>
        <span class="board-notice">${esc(notice)}</span>
        <button type="button" class="btn board-refresh">刷新</button>
        <button type="button" class="btn board-add-btn">＋新增事项</button>
      </div>
      <div class="board-add-slot" hidden></div>
      <div class="board-open">${open.length
        ? open.map(renderItem).join("")
        : '<p class="board-empty">当前无未完成事项</p>'}</div>
      <details class="board-done"><summary>已完成（${done.length}）</summary>
        ${done.map(renderItem).join("") || '<p class="board-empty">尚无已完成事项</p>'}
      </details>`;
    bind();
    if (editing) restoreEditing();
  }

  async function apply(changes, reason) {
    if (awaitingCurrent) return null;
    const r = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({
        op: "board_apply", assignment_id: binding.assignment_id,
        expected_rev: rev(), changes, reason: reason || "面板编辑",
      }),
    });
    if (r.status === 409) {
      awaitingCurrent = true;
      const cur = r.body.current_rev;
      showConflict(`版本冲突：服务器已是 rev.${cur ?? "?"}`
        + "（你的编辑未保存——内容仍在表单里，可据当前版本重改后重试）",
        r.body.items);
      return null;
    }
    if (!r.ok) {
      mount.querySelector(".board-notice").textContent =
        `保存被拒：${r.body.error || "未知错误"}`;
      return null;
    }
    editing = null;
    binding.expected_rev = r.body.rev;
    await load();
    mount.querySelector(".board-notice").textContent = `已保存 rev.${r.body.rev}`;
    if (onSaved) onSaved(r.body);
    return r.body;
  }

  function showConflict(text, serverItems) {
    // 保留编辑现场（editing 已由调用方填好）；同时展示当前服务器版本
    const notice = mount.querySelector(".board-notice");
    if (notice) notice.textContent = text;
    const bar = document.createElement("div");
    bar.className = "board-conflict";
    bar.innerHTML = `<span>${esc(text)}</span>
      <button type="button" class="btn">载入当前版本</button>`;
    const reload = bar.querySelector("button");
    reload.onclick = async () => {
      reload.disabled = true;
      try {
        await load();
      } catch (error) {
        if (notice) notice.textContent = error.message;
        reload.disabled = false;
      }
    };
    for (const submit of mount.querySelectorAll(".board-item-edit button[type=submit]")) {
      submit.disabled = true;
    }
    mount.querySelector(".board-head").appendChild(bar);
    if (Array.isArray(serverItems)) {
      // 服务器版条目摆到已完成区之前的对照区
      const cur = document.createElement("details");
      cur.className = "board-server";
      cur.innerHTML = `<summary>服务器当前版本（${serverItems.length} 项）</summary>`
        + serverItems.map((it) => itemRow(it)).join("");
      cur.open = true;
      mount.insertBefore(cur, mount.querySelector(".board-done"));
    }
  }

  function openEditor(rowEl, it, preset) {
    const slot = rowEl.querySelector(".board-editor-slot");
    slot.hidden = false;
    slot.innerHTML = itemEditor({ ...it, ...preset }, false);
    const form = slot.querySelector("form");
    editing = { item_id: it.item_id, values: null };
    form.onsubmit = async (event) => {
      event.preventDefault();
      const f = form.elements;
      const change = { op: "update", item_id: it.item_id };
      const fields = {
        title: f.title.value.trim(), owner_card: f.owner_card.value.trim(),
        next_result: f.next_result.value.trim(),
        result_note: f.result_note.value.trim(),
        status: f.status.value,
        priority: parseInt(f.priority.value, 10),
        depends: csv(f.depends.value),
        requirement_refs: csv(f.requirement_refs.value),
        evidence_refs: csv(f.evidence_refs.value),
      };
      if (fields.status === "done" && !fields.result_note) {
        mount.querySelector(".board-notice").textContent =
          "标完成必须写明完成依据（完成/受阻说明）";
        return;
      }
      for (const [key, value] of Object.entries(fields)) {
        if (JSON.stringify(value) !== JSON.stringify(it[key])) change[key] = value;
      }
      editing.values = { fields: change, reason: f.reason.value };
      await apply([change], f.reason.value.trim());
    };
    slot.querySelector(".board-cancel").onclick = () => {
      slot.hidden = true; slot.innerHTML = ""; editing = null;
    };
  }

  function openAdd(draft = null) {
    const slot = mount.querySelector(".board-add-slot");
    slot.hidden = false;
    slot.innerHTML = itemEditor({
      title: "", priority: 100, status: "todo", owner_card: "",
      next_result: "", requirement_refs: [], evidence_refs: [],
      ...(draft?.add || {}),
    }, true);
    const form = slot.querySelector("form");
    if (draft?.reason) form.elements.reason.value = draft.reason;
    form.onsubmit = async (event) => {
      event.preventDefault();
      const f = form.elements;
      const change = {
        op: "add", title: f.title.value.trim(),
        priority: parseInt(f.priority.value, 10) || 100,
        owner_card: f.owner_card.value.trim(),
        next_result: f.next_result.value.trim(),
        status: f.status.value,
        requirement_refs: csv(f.requirement_refs.value),
        evidence_refs: csv(f.evidence_refs.value),
        result_note: f.result_note.value.trim(),
      };
      editing = { add: change, reason: f.reason.value };
      const rec = await apply([change], f.reason.value.trim());
      if (rec) slot.innerHTML = "";
    };
    slot.querySelector(".board-cancel").onclick = () => {
      slot.hidden = true; slot.innerHTML = ""; editing = null;
    };
  }

  function restoreEditing() {
    const draft = editing;
    if (!draft) return;
    if (draft.add) {
      openAdd(draft);
      return;
    }
    const row = mount.querySelector(`.board-item[data-item="${CSS.escape(draft.item_id)}"]`);
    const it = (board().items || []).find((entry) => entry.item_id === draft.item_id);
    if (!row || !it) return;
    openEditor(row, it, {});
    const f = row.querySelector("form").elements;
    if (draft.values) {
      for (const [k, v] of Object.entries(draft.values.fields)) {
        if (f[k] === undefined) continue;
        f[k].value = Array.isArray(v) ? v.join(",") : String(v);
      }
      f.reason.value = draft.values.reason || "";
      editing = draft;
    }
  }

  function bind() {
    mount.querySelector(".board-refresh").onclick = () => { editing = null; load(); };
    mount.querySelector(".board-add-btn").onclick = openAdd;
    for (const row of mount.querySelectorAll(".board-item")) {
      const iid = row.dataset.item;
      const it = (board().items || []).find((x) => x.item_id === iid);
      if (!it) continue;
      row.querySelector(".board-edit-btn").onclick = () => openEditor(row, it, {});
      const doneBtn = row.querySelector(".board-done-btn");
      if (doneBtn) {
        doneBtn.onclick = () => openEditor(row, it, { status: "done" });
      }
      const reopenBtn = row.querySelector(".board-reopen-btn");
      if (reopenBtn) {
        reopenBtn.onclick = () => apply(
          [{ op: "update", item_id: iid, status: "todo" }], "用户重开事项");
      }
    }
  }

  return { load, render };
}
