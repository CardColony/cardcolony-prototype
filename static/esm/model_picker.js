/* esm/model_picker.js —— 模型选择（v3，2026-10-19 第二次重做）
 * 设计裁定：不让用户理解「跟随/单独指定/全局/项目」这套继承机制——
 * 那是我内部覆盖层（card>work>global）的实现泄漏。
 * 产品只呈现一件事：**当前这个作用域用哪个模型**，要改就改。
 *
 * 形态：
 *   - 一个下拉。顶行是「用默认（当前 X）」；选中某个模型＝显式指定。
 *   - 默认继承被藏进「用默认」这一项：没单独选时，等于自动用项目/全局默认。
 *   - 没有 radio、没有来源三态、没有弹层。
 *
 * 作用域：
 *   - card：本卡模型（默认＝本项目/全局）
 *   - work：本项目默认模型（默认＝全局）
 */
export function initModelBadge({ mountId, workId, cardId = "" }) {
  const mount = document.getElementById(mountId);
  if (!mount || !workId) return;
  const scope = cardId ? "card" : "work";
  const state = { settings: null, override: null };

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
  // 当前「真正生效」的模型＝显式值，否则继承上层默认
  function effective() { const g = globalDefault(), w = workDefault(), c = cardId ? cardDefault() : ""; return cardId ? (c || w || g) : (w || g); }
  function chosen() { return cardId ? cardDefault() : workDefault(); } // 显式指定值；空＝用默认
  function inheritName() { // 用默认时实际会用到谁
    const g = globalDefault(), w = workDefault();
    const run = cardId ? (w || g) : g;
    return run ? display(run) : "（未配置）";
  }
  function display(key) { const m = (state.settings && state.settings.models && state.settings.models[key]) || {}; return m.model || key; }

  function modelOptions(selected) {
    const settings = state.settings || {};
    const provs = settings.providers || {};
    const models = settings.models || {};
    let html = "";
    for (const pid of Object.keys(provs)) {
      const keys = Object.keys(models).filter((k) => (models[k] || {}).provider === pid);
      if (!keys.length) continue;
      html += `<optgroup label="${esc(pid)}">`;
      for (const k of keys) html += `<option value="${esc(k)}"${k === selected ? " selected" : ""}>${esc(display(k))}</option>`;
      html += "</optgroup>";
    }
    const orphan = Object.keys(models).filter((k) => !(models[k] || {}).provider);
    if (orphan.length) {
      html += `<optgroup label="（未归类）">`;
      for (const k of orphan) html += `<option value="${esc(k)}"${k === selected ? " selected" : ""}>${esc(display(k))}</option>`;
      html += "</optgroup>";
    }
    return html;
  }

  function render() {
    const ch = chosen();
    const defOption = scope === "card"
      ? `默认（当前 ${esc(inheritName())}）`
      : `默认（全局当前 ${esc(inheritName())}）`;
    mount.innerHTML =
      `<span class="mp2-label">${scope === "card" ? "本卡模型" : "本项目默认"}</span>` +
      `<select class="mp2-select" data-mp2="pick" title="改动即保存">` +
        `<option value="" ${!ch ? "selected" : ""}>${esc(defOption)}</option>` +
        modelOptions(ch || "") +
      `</select>`;
  }

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

  mount.addEventListener("change", async (e) => {
    const s = e.target.closest("[data-mp2='pick']");
    if (!s) return;
    const val = s.value;
    const base = `/api/settings/model-overrides/${encodeURIComponent(workId)}`;
    const path = scope === "card" ? `${base}/card/${encodeURIComponent(cardId)}` : base;
    const r = val
      ? await api(path, { method: "PUT", body: JSON.stringify({ default_model: val }) })
      : await api(path, { method: "DELETE" });
    if (!r.ok) { toast("保存失败"); render(); return; }
    state.override = r.ok && r.data ? r.data : state.override;
    render();
    toast("已保存");
  });

  (async () => {
    try {
      const s = await api("/api/settings/models");
      state.settings = s.ok && s.data ? s.data : {};
      await reload();
    } catch (e) { /* 静默 */ }
  })();
}