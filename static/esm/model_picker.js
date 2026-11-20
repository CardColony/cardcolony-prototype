/* esm/model_picker.js —— 模型选择器（v5，照 Claude.ai 抄）
 * 参照：Claude.ai 的模型选择器形态——
 *   输入框里一个「当前模型名 ▾」按钮；点开是上浮面板、分 provider 列型号、
 *   当前项带勾、顶部有「默认」项；选中即生效。
 * 作用域：
 *   - card ：本卡模型（默认＝继承项目/全局）
 *   - work ：本项目默认模型（默认＝继承全局）
 *
 * [窄投影 2026-11-20 · 性能批次一] 徽标的数据源改了两处：
 *   ① **生效模型**走 ``GET /api/settings/model-overrides/{wid}/effective?card_id=``
 *      ——只回**这一张卡**的生效模型键 ＋ 显示名。改前走
 *      ``GET /model-overrides/{wid}``：那个口回**整个项目每一行**
 *      （实测 work_073 975 行 ≈ 260KB），而徽标只要一张卡 ⇒
 *      每次切卡都白下一整张项目覆盖表。
 *   ② **模型目录**（/api/settings/models）**懒加载**：只有用户**打开选择面板**
 *      时才拉。徽标本身只要「当前模型的名字」，那个名字由上面那个窄口带回。
 */
import { API } from "./api.js?v=57";

export function initModelBadge({ mountId, workId, cardId = "" }) {
  const mount = document.getElementById(mountId);
  if (!mount || !workId) return;
  /* [切卡跟手 2026-10-21] 病根：cardId 原来是**页面加载时**的常量
     （window.__CARD_ID__），而切卡是前端路由（open(cid)），页面不刷新 ⇒
     ① 徽标一直显示「打开页面时那张卡」的模型：在卡 A 切了 k3，切到卡 B 还显示 k3；
     ② 在卡 B 上再切，PUT 的路径还是卡 A ⇒ 写错行。
     现在：当前卡可变，宿主切卡后派发 ui:card-changed，本件跟着换行重读。 */
  let curCard = cardId || "";
  /* [新建项目 2026-10-21 · 修] 作用域只看「有没有当前卡」：
     · 项目草稿（workId=draft, 无卡）⇒ work
     · 卡草稿（workId 真, curCard="draft"）⇒ **card**（改的是这张新卡，不是整个项目）
     原来把 "draft" 当空值排除，草稿卡选模型会被写成项目级 ⇒ 整个项目都变。 */
  const scopeOf = () => (curCard ? "card" : "work");
  /* 草稿态判定：与 rule_panel.js 的 isDraft 同口径（home.js 新建项目走 /work/draft）。 */
  const isDraft = () => workId === "draft" || curCard === "draft";
  const state = { open: false, settings: null, override: null, lastClickInside: false };
  let panel = null;
  /* [晚回包归属 2026-11-20] 切卡时把在途的窄读取消：旧卡的回包晚到，
     会把**上一张卡**的模型写进当前卡的徽标。 */
  let reqCtrl = null;

  /* [窄投影] 生效信息：{default_model, card_model, work_model, global_model,
     model_label, model_provider}。取不到就退到空（徽标显示「未配置」）。 */
  const eff = () => state.override || {};
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  /* 全局默认由 /effective 一并带回（省一次目录读）。 */
  function gd() { return String(eff().global_model || ""); }
  function wd() { return String(eff().work_model || ""); }
  function cd() { return String(eff().card_model || ""); }
  /* 生效值以**后端解析结果**为准（表里那一行就是物化后的运行时真值）；
     改前前端自己按 card||work||global 拼，拼的是展示口径不是运行时口径。 */
  function effective() { return String(eff().default_model || ""); }
  function inheritName() {
    const run = scopeOf() === "card" ? (wd() || gd()) : gd();
    return run ? display(run) : "（未配置）";
  }
  /* 显示名：窄口带回的 model_label 优先；否则查目录（面板已加载时）。 */
  function display(key) {
    if (key && key === effective() && eff().model_label) return String(eff().model_label);
    const m = (state.settings && state.settings.models && state.settings.models[key]) || {};
    return m.model || key;
  }
  function providers() { return Object.keys((state.settings && state.settings.providers) || {}); }
  function models() { return (state.settings && state.settings.models) || {}; }
  /* 当前选中的那一项（卡级＝这张卡自己的覆盖；没有＝继承 ⇒ 勾在「默认」行）。 */
  function chosen() { return scopeOf() === "card" ? cd() : wd(); }

  function pill() {
    const e = effective();
    /* [作用域标记 2026-10-21] 卡级**不显示**任何标记——卡片页本来就是「这张卡」，
       加「本卡」两个字是废话。只有「本项目」（会波及全部卡）才标出来。 */
    /* 只有「真项目级」才标——新建项目页整页就是新项目，标「本项目」是废话。 */
    const proj = scopeOf() !== "card" && workId !== "draft";
    const name = e ? (eff().model_label || display(e)) : "";
    return `<button type="button" class="model-pill" data-mp="open" title="${proj ? "本项目" : "本卡"}模型：${esc(name)}（点击切换）">
      ${proj ? '<span class="mp-pill-scope">本项目</span>' : ""}
      <span class="mp-pill-name">${name ? esc(name) : "未配置"}</span>
      <span class="mp-caret">▾</span>
    </button>`;
  }

  async function openPanel() {
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

    /* [懒加载 2026-11-20] 模型目录到这里才拉。徽标本身不需要它
       （生效模型的名字由 /effective 带回）⇒ 冷开不再下整份目录。 */
    if (!state.settings) {
      panel.innerHTML = `<div class="mp-grp"><div class="mp-grp-title">载入中…</div></div>`;
      document.body.appendChild(panel);
      state.open = true;
      try {
        const s = await API.call("GET", "/api/settings/models", undefined, { silent: true });
        state.settings = (s && s.models) ? s : {};
      } catch (e) {
        state.settings = {};
      }
      if (!panel) return;          // 载入期间用户关掉了面板
    }

    for (const prov of providers()) {
      const keys = Object.keys(models()).filter((k) => (models()[k] || {}).provider === prov);
      if (!keys.length) continue;
      rows += `<div class="mp-grp"><div class="mp-grp-title">${esc(prov)}</div>` +
        keys.map((k) => rButton(k, display(k), k === ch)).join("") + `</div>`;
    }
    rows += `<div class="mp-foot">选择即对本${scopeOf() === "card" ? "卡" : "项目"}生效；选「默认」则跟进全局/项目。</div>`;
    panel.innerHTML = rows;
    if (!panel.parentNode) document.body.appendChild(panel);
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
    /* [草稿态 2026-10-21] 项目/卡还没落盘，表还没生成 ⇒ 选择先记在本机，
       落库那一刻由 workspace.ensurePersisted 写进配置表（并通知本件刷新）。
       改前这里直接拒绝（"先保存项目再切模型"）——用户点了没反应，等于不能切。 */
    if (isDraft()) {
      const sc = workId === "draft" ? "work" : "card";
      try {
        sessionStorage.setItem("draftModel", JSON.stringify({ scope: sc, model: val || "" }));
      } catch (e) { /* 隐私模式：记不住也不阻断本次显示 */ }
      state.override = { cards: {}, work: val ? { default_model: val } : null };
      closePanel();
      render();
      toast(val ? "已选，项目保存后生效" : "已恢复默认");
      return;
    }
    const base = `/api/settings/model-overrides/${encodeURIComponent(workId)}`;
    const path = scopeOf() === "card" ? `${base}/card/${encodeURIComponent(curCard)}` : base;
    try {
      if (val) {
        await API.call("PUT", path, { default_model: val }, { silent: true });
      } else {
        await API.call("DELETE", path, undefined, { silent: true });
      }
    } catch (e) {
      toast("保存失败");
      closePanel();
      return;
    }
    await reload();
    closePanel();
    render();
    /* [切卡跟手 2026-10-21] 作用域不同，结果差得很远，提示必须说清改了几张：
       本卡＝只有这一张；本项目＝表里所有「策略填的」行（人手改过的不动）。 */
    if (scopeOf() === "work" && val) {
      toast("已保存：本项目的卡都换成了这个模型");
    } else {
      toast("已保存");
    }
  }

  function render() { mount.innerHTML = pill(); }

  /** 读「当前卡的生效模型」（窄投影）。切卡时取消上一条在途请求。 */
  async function reload() {
    if (isDraft()) { render(); return; }   // 草稿态不查库（GET 对 draft 必 404）
    if (reqCtrl) { try { reqCtrl.abort(); } catch (e) { /* 零阻断 */ } }
    const ctrl = new AbortController();
    reqCtrl = ctrl;
    const wantCard = curCard;
    try {
      const q = wantCard ? `?card_id=${encodeURIComponent(wantCard)}` : "";
      const data = await API.call(
        "GET", `/api/settings/model-overrides/${encodeURIComponent(workId)}${q}`,
        undefined, { signal: ctrl.signal, silent: true });
      /* [晚回包归属 2026-11-20] 回来的不是刚才要的那张卡 ⇒ 丢。
         （否则：卡 A 的回包晚到，把 A 的模型写进 B 的徽标。） */
      if (wantCard !== curCard) return;
      state.override = data || null;
    } catch (e) {
      if (ctrl.signal.aborted) return;      // 切卡取消的，不是错
    }
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

  /* [切卡跟手 2026-10-21] 宿主（workspace.open）切卡后派发：本件换行重读，
     徽标立刻变成**当前这张卡**的模型（不再停留在打开页面时那张）。 */
  window.addEventListener("ui:card-changed", (e) => {
    const cid = String((e && e.detail && e.detail.cardId) || "");
    if (cid === curCard) return;
    curCard = cid;
    closePanel();
    reload();
  });

  (async () => {
    try {
      /* [懒加载 2026-11-20] 这里**不再**拉整份模型目录 —— 徽标只要生效模型，
         那个由 /effective 带回（带显示名）。目录留到开面板时再拉。 */
      await reload();
      // 草稿态：把上次选过、还没落库的那个模型读回来（刷新不丢）
      if (isDraft()) {
        try {
          const pick = JSON.parse(sessionStorage.getItem("draftModel") || "null");
          if (pick && pick.model) {
            state.override = { cards: {}, work: { default_model: pick.model } };
            render();
          }
        } catch (e) { /* 坏值当没选过 */ }
      }
    } catch (e) { /* 静默 */ }
  })();
}
