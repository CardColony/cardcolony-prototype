/* esm/model_picker.js —— 模型选择器（v5，照 Claude.ai 抄）
 * 参照：Claude.ai 的模型选择器形态——
 *   输入框里一个「当前模型名 ▾」按钮；点开是上浮面板，分 provider 列型号、
 *   当前项带勾、顶部有「默认」项；选中即生效。
 * 作用域：
 *   - card ：本卡模型（默认＝继承项目/全局）
 *   - work ：本项目默认模型（默认＝继承全局）
 */
export function initModelBadge({ mountId, workId, cardId = "" }) {
  const mount = document.getElementById(mountId);
  if (!mount || !workId) return;
  const scope = cardId ? "card" : "work";
  const state = { open: false, settings: null, override: null, lastClickInside: false };
  let panel = null;

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
  function gd() { return (state.settings && state.settings.system && state.settings.system.default_model) || ""; }
  function wd() { const w = (state.override && state.override.work) || {}; return w.default_model || ""; }
  function cd() { if (!cardId) return ""; const c = (state.override && state.override.cards && state.override.cards[cardId]) || {}; return c.default_model || ""; }
  function chosen() { return cardId ? cd() : wd(); }
  function effective() { const g = gd(), w = wd(), c = cardId ? cd() : ""; return cardId ? (c || w || g) : (w || g); }
  function inheritName() { const g = gd(), w = wd(); const run = cardId ? (w || g) : g; return run ? display(run) : "（未配置）"; }
  function display(key) { const m = (state.settings && state.settings.models && state.settings.models[key]) || {}; return m.model || key; }
  function providers() { return Object.keys((state.settings && state.settings.providers) || {}); }
  function models() { return (state.settings && state.settings.models) || {}; }

  function pill() {
    const eff = effective();
    return `<button type="button" class="model-pill" data-mp="open" title="选择模型：${esc(display(eff || ""))}">
      <span class="mp-pill-name">${eff ? esc(display(eff)) : "未配置"}</span>
      <span class="mp-caret">▾</span>
    </button>`;
  }

  function openPanel() {
    closePanel();
    const pillar = mount.querySelector('[data-mp="open"]');
    const r = pillar ? pillar.getBoundingClientRect() : { right: mount.getBoundingClientRect().right, bottom: mount.getBoundingClientRect().top };
    panel = document.createElement("div");
    panel.className = "model-panel";
    const ch = chosen();
    const W = 300;
    panel.style.left = Math.max(8, r.right - W) + "px";
    panel.style.bottom = (window.innerHeight - r.top + 6) + "px";

    const rButton = (val, label, selected) =>
      `<button type="button" class="mp-row${selected ? " on" : ""}" data-mp-val="${esc(val)}">${esc(label)}${selected ? '<span class="mp-check">✓</span>' : ""}</button>`;

    let rows = `<div class="mp-grp"><div class="mp-grp-title">默认</div>` +
      rButton("", `默认（当前 ${esc(inheritName())}）`, !ch) + `</div>`;

    for (const prov of providers()) {
      const keys = Object.keys(models()).filter((k) => (models()[k] || {}).provider === prov);
      if (!keys.length) continue;
      rows += `<div class="mp-grp"><div class="mp-grp-title">${esc(prov)}</div>` +
        keys.map((k) => rButton(k, display(k), k === ch)).join("") + `</div>`;
    }
    rows += `<div class="mp-foot">选择即对本${scope === "card" ? "卡" : "项目"}生效；选「默认」则跟进全局/项目。</div>`;
    panel.innerHTML = rows;
    document.body.appendChild(panel);
    state.open = true;

    panel.addEventListener("click", (e) => {
      const row = e.target.closest("[data-mp-val]");
      if (!row) return;
      choose(row.dataset.mpVal);
    });
  }

  function closePanel() {
    state.open = false;
    if (panel) { panel.remove(); panel = null; }
  }

  async function choose(val) {
    const base = `/api/settings/model-overrides/${encodeURIComponent(workId)}`;
    const path = scope === "card" ? `${base}/card/${encodeURIComponent(cardId)}` : base;
    const r = val
      ? await api(path, { method: "PUT", body: JSON.stringify({ default_model: val }) })
      : await api(path, { method: "DELETE" });
    if (!r.ok) { toast("保存失败"); closePanel(); return; }
    state.override = r.ok && r.data ? r.data : state.override;
    closePanel();
    render();
    toast("已保存");
  }

  function render() { mount.innerHTML = pill(); }

  async function reload() {
    const o = await api(`/api/settings/model-overrides/${encodeURIComponent(workId)}`);
    state.override = o.ok && o.data ? o.data : state.override;
    render();
  }

  function toast(msg) {
    const el = document.createElement("div");
    el.className = "mp2-toast";
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2200);
  }

  mount.addEventListener("click", (e) => {
    state.lastClickInside = true;
    if (e.target.closest('[data-mp="open"]')) {
      if (state.open) closePanel(); else openPanel();
    }
  });

  document.addEventListener("click", (e) => {
    if (!state.open) return;
    // mount 内点击/panel 点击在冒泡中已被记录；re-render 会摘除旧按钮导致 contains 误判，
    // 故用 lastClickInside 标记（真机验收踩过这个坑）。
    if (state.lastClickInside) { state.lastClickInside = false; return; }
    if (panel && !panel.contains(e.target) && !mount.contains(e.target)) closePanel();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });

  (async () => {
    try {
      const s = await api("/api/settings/models");
      state.settings = s.ok && s.data ? s.data : {};
      await reload();
    } catch (e) { /* 静默 */ }
  })();
}