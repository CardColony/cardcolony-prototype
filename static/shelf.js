/* shelf.js - 剧本架页 */
(function () {
  "use strict";

  const grid = document.getElementById("shelfGrid");

  function fmtDate(s) {
    if (!s) return "";
    const d = new Date(s);
    if (isNaN(d)) return s;
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function renderBooks(works) {
    grid.innerHTML = "";

    if (works.length === 0) {
      grid.innerHTML = `<div class="shelf-empty" style="grid-column:1/-1;">
        <h2>还没有任何剧本</h2>
        <p>一个剧本 = 一个独立的推演世界，自带画布、卡片树与对话。</p>
        <button class="btn primary" onclick="document.getElementById('newBookCard').click()">＋ 创建第一个剧本</button>
      </div>`;
      appendNewCard();
      return;
    }

    for (const w of works) {
      const el = document.createElement("div");
      el.className = "book";
      el.onclick = () => { window.location.href = "/work/" + w.work_id; };
      const ending = w.ending_derived ? `<span class="ending-flag">🏁</span>` : "";
      el.innerHTML = ending + `
        <div class="bname">${escapeHtml(w.title)}</div>
        <div class="bmeta">创建于 ${fmtDate(w.created_at)}</div>
        <div class="bstats">
          <span class="chip">卡片 ${w.card_count}</span>
        </div>
      `;
      grid.appendChild(el);
    }
    appendNewCard();
  }

  function appendNewCard() {
    const el = document.createElement("div");
    el.className = "book new";
    el.id = "newBookCard";
    el.innerHTML = `
      <div>
        <div class="plus">＋</div>
        新建剧本
      </div>
      <div class="inline-form">
        <input type="text" id="newBookInput" placeholder="剧本名" maxlength="60">
        <button class="btn primary" id="newBookConfirm">建</button>
        <button class="btn ghost" id="newBookCancel">×</button>
      </div>
    `;
    el.onclick = (e) => {
      if (el.classList.contains("form-mode")) return;
      el.classList.add("form-mode");
      el.querySelector(".inline-form").classList.add("active");
      el.querySelector(".plus").parentElement.style.display = "none";
      const input = el.querySelector("#newBookInput");
      input.focus();
      input.onkeydown = (ev) => {
        if (ev.key === "Enter") confirmCreate();
        if (ev.key === "Escape") cancelCreate();
      };
      el.querySelector("#newBookConfirm").onclick = confirmCreate;
      el.querySelector("#newBookCancel").onclick = cancelCreate;
    };
    grid.appendChild(el);
  }

  async function confirmCreate() {
    const input = document.getElementById("newBookInput");
    const title = input.value.trim();
    if (!title) { API.toast("请输入剧本名", "error"); return; }
    try {
      const w = await API.createWork(title);
      window.location.href = "/work/" + w.work_id;
    } catch (e) { /* toast 已由 api 处理 */ }
  }

  function cancelCreate() {
    const card = document.getElementById("newBookCard");
    card.classList.remove("form-mode");
    card.querySelector(".inline-form").classList.remove("active");
    card.querySelector(".plus").parentElement.style.display = "";
    card.querySelector("#newBookInput").value = "";
  }

  async function load() {
    try {
      const data = await API.listWorks();
      renderBooks(data.works || []);
    } catch (e) { /* toast 已处理 */ }
  }

  window.NewBookConfirm = confirmCreate;
  window.NewBookCancel = cancelCreate;
  load();

  /* ========== [phase9-C] 方法论库 ========== */
  const methoList = document.getElementById("methoList");
  let methoOpenId = null; // 当前展开的条目 id

  function fmtDateTime(s) {
    if (!s) return "";
    const d = new Date(s);
    if (isNaN(d)) return s;
    return fmtDate(s) + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function renderMethoList(entries) {
    if (!methoList) return;
    if (!entries.length) {
      methoList.innerHTML = `<div class="metho-empty">（暂无沉淀，与卡片对话中会自动积累）</div>`;
      return;
    }
    methoList.innerHTML = "";
    for (const e of entries) {
      const row = document.createElement("div");
      row.className = "metho-item";
      row.dataset.mid = e.id;
      const tags = (e.tags || []).map(t => `<span class="metho-tag">${escapeHtml(t)}</span>`).join("");
      row.innerHTML = `
        <div class="metho-row">
          <span class="metho-name">${escapeHtml(e.title)}</span>
          ${tags}
          <span class="spacer"></span>
          <span class="metho-src">${escapeHtml(e.source_work || "")}</span>
          <span class="metho-time">${fmtDateTime(e.updated_at)}</span>
        </div>
        <div class="metho-detail" style="display:none;"></div>
      `;
      row.querySelector(".metho-row").onclick = () => toggleMethoDetail(e.id, row);
      methoList.appendChild(row);
    }
  }

  async function toggleMethoDetail(mid, row) {
    const detail = row.querySelector(".metho-detail");
    if (methoOpenId === mid) {
      detail.style.display = "none";
      methoOpenId = null;
      return;
    }
    // 收起其他已展开条目
    methoList.querySelectorAll(".metho-detail").forEach(d => { d.style.display = "none"; });
    methoOpenId = mid;
    detail.style.display = "";
    detail.innerHTML = `<div class="loading-screen">加载中…</div>`;
    try {
      const entry = await API.getMethodology(mid);
      detail.innerHTML = `
        <textarea class="metho-edit"></textarea>
        <div class="metho-actions">
          <span class="metho-meta">来源：${escapeHtml(entry.source_work || "-")} / ${escapeHtml(entry.source_card || "-")}</span>
          <span class="spacer"></span>
          <button class="btn primary metho-save">保存</button>
        </div>
      `;
      const ta = detail.querySelector(".metho-edit");
      ta.value = entry.content || "";
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight + 4, 400) + "px";
      detail.querySelector(".metho-save").onclick = async () => {
        try {
          await API.patchMethodology(mid, { content: ta.value });
          API.toast("已保存", "");
          loadMetho(); // 就地刷新列表（更新时间等）
        } catch (err) { /* toast 已处理 */ }
      };
    } catch (err) {
      detail.innerHTML = `<div class="metho-empty">加载失败</div>`;
    }
  }

  async function loadMetho() {
    if (!methoList) return;
    try {
      const data = await API.listMethodology();
      renderMethoList(data.entries || []);
    } catch (e) { /* toast 已处理 */ }
  }

  loadMetho();
})();
