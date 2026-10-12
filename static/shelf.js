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

  function fmtRel(iso) {
    // [T17-F9] 最近活跃相对时间：与 workspace.js 同口径
    // （刚刚／N 分钟前／N 小时前／N 天前／YYYY-MM-DD）
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    if (s < 7 * 86400) return Math.floor(s / 86400) + " 天前";
    const d = new Date(iso);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
         + "-" + String(d.getDate()).padStart(2, "0");
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
        <div class="bmeta">创建于 ${fmtDate(w.created_at)}${w.last_active_at ? ` · 最近活跃 ${fmtRel(w.last_active_at)}` : ""}</div>
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
      const works = data.works || [];
      const g = document.getElementById("shelfGrid");
      if (!works.length && g) {
        g.innerHTML = `<div class="loading-screen">还没有剧本——点右上「＋ 新建剧本」开始。</div>`;   // [F3⑥] 空态
        return;
      }
      renderBooks(works);
    } catch (e) {
      // [F3⑥] 失败态＋重试（原静默 catch）
      const g = document.getElementById("shelfGrid");
      if (g) g.innerHTML = `<div class="loading-screen">加载失败 <button class="btn" onclick="location.reload()">重试</button></div>`;
    }
  }

  window.NewBookConfirm = confirmCreate;
  window.NewBookCancel = cancelCreate;
  // [2026-10-12 加载失败修复] 薄壳 api.js 以异步 import 挂 window.API（迭代四
  // F2①），顶层立即 load() 必然早于 API 就位 → ReferenceError 被 load 自己的
  // catch 吞成「加载失败」。该时序缺口自迭代四潜伏（薄壳注释假设"shelf.js 无
  // 顶层立即调用"被违反）；工作页经 esm/main.js 静态 import 无此病，故至今才
  // 爆发。改为就绪等待：API 上线即启动；同源模块正常毫秒级返回，100×100ms
  // 内不至即按真故障呈现失败态。
  (async function boot() {
    for (let i = 0; i < 100; i++) {
      if (window.API) { load(); return; }
      await new Promise(r => setTimeout(r, 100));
    }
    const g = document.getElementById("shelfGrid");
    if (g) g.innerHTML = `<div class="loading-screen">加载失败 <button class="btn" onclick="location.reload()">重试</button></div>`;
  })();

})();
