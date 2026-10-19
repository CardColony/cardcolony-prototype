/* esm/model_picker.js —— 模型指示 + 就地切换
 * [2026-10-19 产品裁定] 模型不在后台设置表格里看，而在使用现场（卡片页/画布页顶栏）看和换。
 *
 * 支持两种作用域：
 *   - card：本卡「单独指定 / 跟随项目（含全局）」
 *   - work：本项目「单独指定 / 跟随全局」
 *
 * 数据源：
 *   GET /api/settings/models          （模型目录 + 全局默认）
 *   GET /api/settings/model-overrides/{wid}   （覆盖）
 *   PUT/DELETE /api/settings/model-overrides/{wid}[/card/{cid}]
 *
 * 语义（只显示结果，不教用户优先序）：
 *   - chip 永远展示「当前真正生效的模型」+ 来源标签；点开才能换。
 */
export function initModelBadge({ mountId, workId, cardId = "" }) {
  const mount = document.getElementById(mountId);
  if (!mount || !workId) return;
  const scope = cardId ? "card" : "work";
  const aid = `${scope}-${workId}-${cardId}`;
  const state = { open: false, settings: null, override: null };

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, ok: res.ok, data };
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function globalDefault() { return (state.settings && state.settings.system && state.settings.system.default_model) || ""; }
  function workDefault() { const w = (state.override && state.override.work) || {}; return w.default_model || ""; }
  function cardDefault() {
    if (!cardId) return "";
    const c = (state.override && state.override.cards && state.override.cards[cardId]) || {};
    return c.default_model || "";
  }
  function effective() {
    const g = globalDefault(), w = workDefault(), c = cardId ? cardDefault() : "";
    return cardId ? (c || w || g) : (w || g);
  }
  function srcLabel() {
    const hasC = !!(cardId && cardDefault());
    const hasW = !!workDefault();
    if (cardId) return hasC ? "本卡指定" : (hasW ? "项目默认" : "全局默认");
    return hasW ? "本项目指定" : "全局默认";
  }
  function modelDisplay(key) {
    const m = (state.settings && state.settings.models && state.settings.models[key]) || {};
    return m.model || key;
  }
  function modelOptions(selected) {
    const settings = state.settings || {};
    const provs = settings.providers || {};
    const models = settings.models || {};
    let html = `<option value="">（未选择）</option>`;
    for (const pid of Object.keys(provs)) {
      const keys = Object.keys(models).filter((k) => (models[k] || {}).provider === pid);
      if (!keys.length) continue;
      html += `<optgroup label="${esc(pid)}">`;
      for (const k of keys) {
        html += `<option value="${esc(k)}"${k === selected ? " selected" : ""}>${esc(modelDisplay(k))}</option>`;
      }
      html += "</optgroup>";
    }
    const orphan = Object.keys(models).filter((k) => !(models[k] || {}).provider);
    if (orphan.length) {
      html += `<optgroup label="（未归类）">`;
      for (const k of orphan) html += `<option value="${esc(k)}"${k === selected ? " selected" : ""}>${esc(modelDisplay(k))}</option>`;
      html += "</optgroup>";
    }
    return html;
  }

  function toast(msg) {
    const el = document.createElement("div");
    el.className = "model-badge-toast";
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2400);
  }

  function chip() {
    const eff = effective();
    const name = eff ? modelDisplay(eff) : "未配置模型";
    const src = eff ? srcLabel() : "";
    return `<button type="button" class="model-badge" data-mb="open" title="切换本${scope === "card" ? "卡" : "项目"}的模型">
      <span class="mb-name">${esc(name)}</span>
      ${src ? `<span class="mb-src">${esc(src)}</span>` : ""}
      <span class="mb-caret">▾</span>
    </button>`;
  }

  function followLabel() {
    const g = globalDefault(), w = workDefault();
    if (scope === "card") {
      const cur = w || g;
      return `跟随（当前 ${cur ? modelDisplay(cur) : "未配置"}）`;
    }
    return `跟随全局（当前 ${g ? modelDisplay(g) : "未配置"}）`;
  }

  function popover() {
    const hasC = !!cardDefault();
    const eff = effective();
    const sel = eff || globalDefault();
    const effHtml = eff ? `${esc(modelDisplay(eff))}（${esc(srcLabel())}）` : "未配置";
    return `<div class="model-pop" data-mb="pop">
      <div class="mp-title">${scope === "card" ? "本卡模型" : "本项目默认模型"}</div>
      <label class="mp-opt"><input type="radio" name="mbmode-${esc(aid)}" value="follow"${!hasC ? " checked" : ""}> <span>${esc(followLabel())}</span></label>
      <label class="mp-opt"><input type="radio" name="mbmode-${esc(aid)}" value="specify"${hasC ? " checked" : ""}> <span>单独指定</span></label>
      <div class="mp-select-wrap"${!hasC ? ' style="display:none"' : ""}><select class="mp-select" data-mb="select">${modelOptions(hasC ? cardDefault() : sel)}</select></div>
      <div class="mp-eff">当前：${effHtml}</div>
      <div class="mp-actions">
        <button type="button" class="btn ghost" data-mb="close">取消</button>
        <button type="button" class="btn primary" data-mb="save">保存</button>
      </div>
    </div>`;
  }

  function render() {
    mount.innerHTML = chip();
    if (state.open) mount.insertAdjacentHTML("beforeend", popover());
    bindPopEvents();
  }

  function onRadioChange() {
    const specify = mount.querySelector('input[name="mbmode-' + CSS.escape(aid) + '"][value="specify"]');
    const wrap = mount.querySelector(".mp-select-wrap");
    if (!specify || !wrap) return;
    wrap.style.display = specify.checked ? "" : "none";
  }

  function bindPopEvents() {
    const sel = mount.querySelector('input[name="mbmode-' + CSS.escape(aid) + '"]:checked');
    onRadioChange();
    void sel;
  }

  async function save() {
    const mode = (mount.querySelector('input[name="mbmode-' + CSS.escape(aid) + '"]:checked') || {}).value;
    const selEl = mount.querySelector(".mp-select");
    const sel = selEl ? selEl.value : "";
    const base = `/api/settings/model-overrides/${encodeURIComponent(workId)}`;
    const path = scope === "card" ? `${base}/card/${encodeURIComponent(cardId)}` : base;
    let r;
    if (mode === "specify" && sel) {
      r = await api(path, { method: "PUT", body: JSON.stringify({ default_model: sel }) });
    } else {
      r = await api(path, { method: "DELETE" });
    }
    if (!r.ok) { toast("保存失败"); return; }
    state.open = false;
    await reload();
    toast("已保存");
  }

  async function reload() {
    const o = await api(`/api/settings/model-overrides/${encodeURIComponent(workId)}`);
    state.override = o.ok && o.data ? o.data : state.override;
    render();
  }

  async function load() {
    try {
      const s = await api("/api/settings/models");
      state.settings = s.ok && s.data ? s.data : {};
      await reload();
    } catch (e) {
      mount.innerHTML = `<span class="model-badge model-badge-err">模型</span>`;
    }
  }

  mount.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-mb]");
    if (!t) {
      // 点外部关闭
      if (state.open && !e.target.closest(".model-pop")) { state.open = false; render(); }
      return;
    }
    const act = t.dataset.mb;
    if (act === "open") { state.open = !state.open; render(); return; }
    if (act === "close") { state.open = false; render(); return; }
    if (act === "save") { await save(); return; }
  });
  mount.addEventListener("change", (e) => {
    if (e.target.matches('input[name="mbmode-' + CSS.escape(aid) + '"]')) onRadioChange();
  });

  document.addEventListener("click", (e) => {
    if (state.open && !mount.contains(e.target)) { state.open = false; render(); }
  });

  load();
}