/* settings.js —— 模型设置页（改版 v8 · 一张大卡片内左右分栏）
 *
 * 排版（照 zcode model-provider 界面骨架）：
 *   一张大卡片 —— 左：供应商列表（导航项，点选高亮，蓝色左指示条）；
 *   右：选中供应商的模型详细设置（头行 logo+名称+协议pill+密钥状态+「⋯」菜单 +
 *       连接字段 + 模型列表）。
 *   添加供应商 = 模板选择器（网格卡片），选模板预填端点/密钥变量，或自定义空。
 *
 * 约定：
 *   - 编辑期只改内存 DATA，点「保存」才 PUT（保存即热生效）。
 *   - 密钥永不回显：只显示「已配置 ····xxxx」，改密走 PUT /keys/{name}。
 *   - 400 → 顶部红条列全部错误，页面不关、表单不清。
 *   - CURRENT 记录当前选中供应商；删除后自动选中下一个。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  let DATA = {
    providers: {}, models: {}, bindings: {},
    system: {}, simple: {}, retry_grading: {},
    roles: {}, key_status: {},
  };
  let META = {};
  let FETCH_CTX = null;
  let RESTORE_LIST = [];
  let EDITING = new Set();        // 展开编辑的模型 key
  let CURRENT = null;             // 当前选中供应商

  // 内置供应商模板（对应 config/models.yaml 的 6 家 + 自定义空）
  //
  // 〔5715 · 模型名收口〕sub 里**不写具体型号**（原来写着各家的型号名）——型号换代后
  // 这段字会变成过期信息，而且它是**代码里写死的模型名**。型号信息由模型目录
  // （DATA.models，后端 /api/settings/models 下发）动态显示；这里只说协议与端点特征。
  const TEMPLATES = [
    { id: "nous",     name: "Nous",        sub: "OpenAI 兼容 · 需 API Key",         color: "#5c6bc0", protocol: "openai",   base_url: "https://inference-api.nousresearch.com/v1", api_key_env: "NOUS_API_KEY", models_url: "" },
    { id: "deepseek", name: "DeepSeek",    sub: "官方端点 · 需 API Key",              color: "#4d6bfe", protocol: "deepseek", base_url: "", api_key_env: "DEEPSEEK_API_KEY", models_url: "https://api.deepseek.com/models" },
    { id: "zhipu",    name: "智谱 GLM",     sub: "OpenAI 兼容 · 需 API Key",          color: "#3859ff", protocol: "openai",   base_url: "https://open.bigmodel.cn/api/coding/paas/v4", api_key_env: "ZHIPU_CODING_API_KEY", models_url: "" },
    { id: "minimax",  name: "MiniMax",     sub: "OpenAI 兼容 · 需 API Key",          color: "#151515", protocol: "openai",   base_url: "https://api.minimaxi.com/v1", api_key_env: "MINIMAX_API_KEY", models_url: "" },
    { id: "aliyun",   name: "阿里云",       sub: "OpenAI 兼容 · 支持 enable_search",  color: "#ff6a00", protocol: "openai",   base_url: "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", api_key_env: "ALIYUN_TOKEN_PLAN_API_KEY", models_url: "" },
    { id: "opencode", name: "OpenCode",    sub: "OpenAI 兼容 · 需会话头",            color: "#22d3ee", protocol: "openai",   base_url: "https://opencode.ai/zen/go/v1", api_key_env: "OPENCODE_API_KEY", models_url: "" },
  ];

  // ------------------------------------------------------------ 工具

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function toast(msg, isErr) {
    const box = $("toastContainer");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast" + (isErr ? " err" : "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 应用内弹窗（替代 window.prompt / window.confirm）
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 自查] 这个页面上还剩两处浏览器原生弹窗：改密钥用 prompt、
  // 删供应商用 confirm。它们不可换肤、会冻结主线程、长得像"网页在警告你"——
  // 而整个产品其余部分都是深色定制界面，只有这里露出浏览器原生 UI。
  // 改密钥尤其不能这么干：那是一个**密钥输入框**，原生 prompt 里既看不见已配置
  // 状态、也没法提示"不会进日志"，只有一个空白输入条。
  //
  // 为什么不做成和画布共用的模块：canvas.js 是 ES module、这个文件是普通脚本，
  // 共用要么把它也改成 module（牵动整个文件的全局导出），要么挂 window 全局。
  // 为一个页面上的两个对话框付这个代价不划算——这里给一份同语言的最小实现，
  // 样式全部走 style.css 的 token，视觉与画布那边一致。
  //
  // 返回：输入模式 → 字符串或 null；确认模式 → true 或 null。
  // ══════════════════════════════════════════════════════════════════════════
  const DLG_CSS = `
  .set-dlg-mask{position:fixed;inset:0;z-index:900;display:flex;align-items:center;
    justify-content:center;background:rgba(1,4,9,.66);animation:setDlgFade .12s ease}
  @keyframes setDlgFade{from{opacity:0}to{opacity:1}}
  .set-dlg{width:420px;max-width:calc(100vw - 48px);max-height:74vh;display:flex;
    flex-direction:column;padding:18px 18px 14px;background:var(--panel);
    border:1px solid var(--border);border-radius:12px;
    box-shadow:0 24px 64px rgba(0,0,0,.72)}
  .set-dlg-title{font-size:14px;font-weight:650;color:var(--text);margin-bottom:10px}
  .set-dlg-body{font-size:12.5px;line-height:1.75;color:var(--text-soft);
    overflow-y:auto;margin-bottom:16px;white-space:pre-wrap;word-break:break-word}
  .set-dlg-input{width:100%;padding:8px 10px;margin-bottom:16px;border-radius:7px;
    border:1px solid var(--border);background:var(--bg-deep);color:var(--text);
    font-size:13px;font-family:inherit;box-sizing:border-box}
  .set-dlg-input:focus{outline:none;border-color:#388bfd}
  .set-dlg-ops{display:flex;justify-content:flex-end;gap:8px}
  .set-dlg-btn{min-width:72px;height:32px;padding:0 14px;border-radius:7px;
    border:1px solid var(--border);background:transparent;color:var(--text-soft);
    font-size:12.5px;font-weight:600;cursor:pointer;font-family:inherit}
  .set-dlg-btn:hover{background:var(--panel-hover)}
  .set-dlg-btn.is-primary{border-color:rgba(56,139,253,.55);
    background:rgba(56,139,253,.16);color:#79b8ff}
  .set-dlg-btn.is-danger{border-color:rgba(248,81,73,.55);
    background:rgba(248,81,73,.14);color:#ff7b72}
  .set-dlg-btn:focus-visible{outline:2px solid #388bfd;outline-offset:2px}`;

  let _dlgCssInjected = false;

  function dialog({ title, body = "", input = false, value = "", placeholder = "",
                    okText = "确定", cancelText = "取消", danger = false }) {
    if (!_dlgCssInjected) {
      const st = document.createElement("style");
      st.textContent = DLG_CSS;
      document.head.appendChild(st);
      _dlgCssInjected = true;
    }
    return new Promise((resolve) => {
      const mask = document.createElement("div");
      mask.className = "set-dlg-mask";
      mask.innerHTML = `
        <div class="set-dlg" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
          <div class="set-dlg-title">${escapeHtml(title)}</div>
          ${body ? `<div class="set-dlg-body">${escapeHtml(body)}</div>` : ""}
          ${input ? `<input class="set-dlg-input" type="text" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" aria-label="${escapeHtml(title)}">` : ""}
          <div class="set-dlg-ops">
            <button class="set-dlg-btn" type="button" data-role="cancel">${escapeHtml(cancelText)}</button>
            <button class="set-dlg-btn ${danger ? "is-danger" : "is-primary"}" type="button" data-role="ok">${escapeHtml(okText)}</button>
          </div>
        </div>`;
      document.body.appendChild(mask);

      const inp = mask.querySelector(".set-dlg-input");
      const okBtn = mask.querySelector('[data-role="ok"]');
      let done = false;
      const close = (v) => {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey, true);
        mask.remove();
        resolve(v);
      };
      const submit = () => close(input ? (inp.value.trim() || null) : true);
      okBtn.onclick = submit;
      mask.querySelector('[data-role="cancel"]').onclick = () => close(null);
      mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(null); });

      // 捕获阶段拦截：弹窗开着时 Esc/Enter/Tab 不许漏到页面快捷键上；
      // Tab 圈在弹窗内（aria-modal 承诺了模态，就得真的模态）。
      const onKey = (e) => {
        if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(null); return; }
        if (e.key === "Enter") { e.stopPropagation(); e.preventDefault(); submit(); return; }
        if (e.key === "Tab") {
          const f = [...mask.querySelectorAll("button, input")]
            .filter((el) => el.offsetParent !== null);
          if (!f.length) return;
          e.preventDefault(); e.stopPropagation();
          const i = f.indexOf(document.activeElement);
          const n = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1)
                               : (i === f.length - 1 || i < 0 ? 0 : i + 1);
          f[n].focus();
        }
      };
      document.addEventListener("keydown", onKey, true);
      if (inp) { inp.focus(); inp.select(); } else okBtn.focus();
    });
  }

  function banner(kind, html) {
    const b = $("setBanner");
    b.className = "set-banner " + kind;
    b.innerHTML = html;
    b.style.display = "block";
    b.scrollIntoView({ block: "nearest" });
  }
  function clearBanner() {
    const b = $("setBanner");
    b.style.display = "none";
    b.innerHTML = "";
  }

  function setSaveStatus(msg, isErr) {
    const el = $("setSaveStatus");
    if (!el) return;
    el.textContent = msg;
    el.style.color = isErr ? "#f85149" : "#3fb950";
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, ok: res.ok, data: data };
  }

  function sanitizeKey(modelId) {
    let s = String(modelId || "").toLowerCase().replace(/\./g, "");
    s = s.replace(/[\s/:_]+/g, "-");
    s = s.replace(/[^a-z0-9-]+/g, "-");
    s = s.replace(/-+/g, "-").replace(/^-+|-+$/g, "");
    return s || "model";
  }

  function uniqueKey(base, exceptKey) {
    if (!DATA.models[base] || exceptKey === base) return base;
    let i = 2;
    while (DATA.models[base + "-" + i] && exceptKey !== base + "-" + i) i++;
    return base + "-" + i;
  }

  function modelOptions(selected, exclude, omitEmpty) {
    const providers = Object.keys(DATA.providers);
    let html = omitEmpty ? "" : '<option value="">（未绑定）</option>';
    for (const pid of providers) {
      const mns = Object.keys(DATA.models).filter(
        (n) => DATA.models[n].provider === pid && (!exclude || exclude.indexOf(n) < 0)
      );
      if (!mns.length) continue;
      html += `<optgroup label="${escapeHtml(pid)}">`;
      for (const n of mns) {
        const m = DATA.models[n] || {};
        const sel = n === selected ? " selected" : "";
        // 选项显示真实型号；key 只作 value（内部存储），不显示、不叠加
        html += `<option value="${escapeHtml(n)}"${sel}>${escapeHtml(m.model || n)}</option>`;
      }
      html += "</optgroup>";
    }
    // 兜底：孤儿模型（provider 指向已不存在的供应商）也要能选到，不能丢
    const orphan = Object.keys(DATA.models).filter(
      (n) => providers.indexOf(DATA.models[n].provider) < 0 && (!exclude || exclude.indexOf(n) < 0)
    );
    if (orphan.length) {
      html += `<optgroup label="（未归类）">`;
      for (const n of orphan) {
        const m = DATA.models[n] || {};
        const sel = n === selected ? " selected" : "";
        html += `<option value="${escapeHtml(n)}"${sel}>${escapeHtml(m.model || n)}</option>`;
      }
      html += "</optgroup>";
    }
    return html;
  }

  function keyStatusChip(env) {
    const st = env ? DATA.key_status[env] : null;
    if (!st || !st.configured) {
      return '<span class="set-key-state"><span class="set-status-dot"></span>未配置</span>';
    }
    return `<span class="set-key-state"><span class="set-status-dot on"></span>已配置 ····${escapeHtml(st.tail || "")}</span>`;
  }

  function modelsOf(provider) {
    return Object.keys(DATA.models).filter((mn) => {
      const m = DATA.models[mn];
      return m && m.provider === provider;
    });
  }

  function logoColor(name) {
    const t = TEMPLATES.find((x) => x.id === name || x.name.toLowerCase() === String(name).toLowerCase());
    return t ? t.color : "#3a3f47";
  }

  // ------------------------------------------------------------ 读

  // ------------------------------------------------------------ 按卡片类型设置默认模型（新建项目模板）

  const CTM = { cardTypes: [], types: {} };

  async function loadCardTypeModels() {
    const r = await api("/api/settings/card-type-models");
    if (!r.ok || !r.data) { return; }
    CTM.cardTypes = r.data.card_types || [];
    CTM.types = r.data.types || {};
    renderCardTypeModels();
  }

  function ctmModelOptions(selected) {
    const list = CTM.modelsList || [];
    let html = '<option value="">（默认 · 未指定）</option>';
    for (const m of list) {
      const sel = m === selected ? " selected" : "";
      html += `<option value="${escapeHtml(m)}"${sel}>${escapeHtml(m)}</option>`;
    }
    return html;
  }

  function renderCardTypeModels() {
    const box = $("ctmList");
    const hint = $("ctmHint");
    if (!box) return;
    // 模型目录来自全局配置（DATA.models 键）
    CTM.modelsList = Object.keys(DATA.models || {}).sort();
    if (hint) hint.textContent = `共 ${CTM.cardTypes.length} 种卡片类型 · 每次新建项目整体拷贝成该项目自己的配置表。`;
    box.innerHTML = `<div class="set-grid" style="grid-template-columns:1fr 1fr">` +
      CTM.cardTypes.map((ct) => `
        <div class="set-field">
          <label>${escapeHtml(ct)}</label>
          <select class="set-select" data-ctm="${escapeHtml(ct)}">${ctmModelOptions(CTM.types[ct] || "")}</select>
        </div>`).join("") +
      `</div>`;
  }

  async function saveCardTypeModels() {
    const types = {};
    document.querySelectorAll("[data-ctm]").forEach((sel) => {
      if (sel.value) types[sel.dataset.ctm] = sel.value;
    });
    const st = $("ctmStatus");
    const r = await api("/api/settings/card-type-models", { method: "PUT", body: JSON.stringify({ types }) });
    if (!r.ok) {
      const detail = r.data ? (r.data.errors || []).join("；") || r.data.error : "";
      if (st) { st.textContent = "保存失败：" + (detail || "未知错误"); st.style.color = "#f85149"; }
      return;
    }
    CTM.types = r.data.types || {};
    if (st) { st.textContent = "模板已保存（新建项目将使用它）"; st.style.color = "#3fb950"; }
    toast("已保存");
  }

  // ------------------------------------------------------------ 分层与兜底
  // [层内兜底 2026-10-21 · 用户令] 每层一个**有序**列表：第 1 个是首选，
  // 它失败就换第 2 个，依次接替。只在本层内兜底，绝不跨层。
  const TIER = {
    levels: ["depth_0", "depth_1", "depth_2", "default"],
    labels: {
      depth_0: "第 0 层 · 项目根卡",
      depth_1: "第 1 层",
      depth_2: "第 2 层",
      default: "兜底层 · 没有对应深度时",
    },
    tiers: {},
  };

  function tierLevels() {
    const known = TIER.levels.slice(0, -1);
    const extra = Object.keys(TIER.tiers).filter((k) => !TIER.levels.includes(k)).sort();
    return known.concat(extra, ["default"]);
  }

  function tierLabel(lv) {
    if (TIER.labels[lv]) return TIER.labels[lv];
    const m = /^depth_(\d+)$/.exec(lv);
    return m ? `第 ${m[1]} 层` : lv;
  }

  function tierOptions(pool) {
    const names = Object.keys(DATA.models || {}).sort();
    let html = '<option value="">＋ 添加模型</option>';
    for (const m of names) {
      if (pool.indexOf(m) >= 0) continue;
      html += `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`;
    }
    return html;
  }

  function renderTiers() {
    const box = $("tierList");
    if (!box) return;
    box.innerHTML = tierLevels().map((lv) => {
      const pool = TIER.tiers[lv] || [];
      const chips = pool.map((m, i) => `
        <span class="tier-chip">
          <span class="tier-idx">${i + 1}</span>
          <span class="tier-model" title="${escapeHtml(m)}">${escapeHtml(m)}</span>
          <button class="tier-btn" data-tier="up" data-lv="${escapeHtml(lv)}" data-i="${i}"${i === 0 ? " disabled" : ""} title="上移（更优先）">↑</button>
          <button class="tier-btn" data-tier="down" data-lv="${escapeHtml(lv)}" data-i="${i}"${i === pool.length - 1 ? " disabled" : ""} title="下移（更靠后兜底）">↓</button>
          <button class="tier-btn del" data-tier="del" data-lv="${escapeHtml(lv)}" data-i="${i}" title="移除">×</button>
        </span>`).join("");
      return `<div class="tier-row">
        <div class="tier-head">
          <span class="tier-name">${escapeHtml(tierLabel(lv))}</span>
          <span class="tier-count">${pool.length ? pool.length + " 个" : "空"}</span>
        </div>
        <div class="tier-pool">
          ${chips || '<span class="tier-empty">这一层还没有模型</span>'}
          <select class="tier-add" data-tier-add="${escapeHtml(lv)}">${tierOptions(pool)}</select>
        </div>
      </div>`;
    }).join("");
  }

  function onTierClick(e) {
    const btn = e.target.closest("[data-tier]");
    if (!btn) return;
    const lv = btn.dataset.lv;
    const i = Number(btn.dataset.i);
    const pool = (TIER.tiers[lv] || []).slice();
    if (!pool.length) return;
    const act = btn.dataset.tier;
    if (act === "up" && i > 0) {
      const t = pool[i - 1]; pool[i - 1] = pool[i]; pool[i] = t;
    } else if (act === "down" && i < pool.length - 1) {
      const t = pool[i + 1]; pool[i + 1] = pool[i]; pool[i] = t;
    } else if (act === "del") {
      pool.splice(i, 1);
    } else {
      return;
    }
    TIER.tiers[lv] = pool;
    renderTiers();
  }

  function onTierAdd(e) {
    const sel = e.target.closest("[data-tier-add]");
    if (!sel || !sel.value) return;
    const lv = sel.dataset.tierAdd;
    const pool = (TIER.tiers[lv] || []).slice();
    if (pool.indexOf(sel.value) < 0) pool.push(sel.value);
    TIER.tiers[lv] = pool;
    renderTiers();
  }

  async function saveTiers() {
    const st = $("tierStatus");
    const payload = {};
    for (const lv of tierLevels()) {
      const pool = TIER.tiers[lv] || [];
      if (pool.length) payload[lv] = pool;
    }
    const r = await api("/api/settings/tiers", {
      method: "PUT", body: JSON.stringify({ tiers: payload }),
    });
    if (!r.ok) {
      const detail = r.data ? ((r.data.errors || []).join("；") || r.data.error) : "";
      if (st) { st.textContent = "保存失败：" + (detail || "未知错误"); st.style.color = "#f85149"; }
      return;
    }
    TIER.tiers = r.data.tiers || payload;
    renderTiers();
    const changed = ((r.data.reseed || {}).changed) || 0;
    if (st) {
      st.textContent = changed
        ? `已保存；按新分层重算了 ${changed} 张卡的默认模型（人手改过的不动）`
        : "已保存（已有卡都在新分层下，无需重算）";
      st.style.color = "#3fb950";
    }
    toast("分层已保存");
  }

  async function load() {
    const r = await api("/api/settings/models");
    if (!r.ok || !r.data) {
      banner("err", "读取模型配置失败：" + escapeHtml((r.data && r.data.error) || r.status));
      return;
    }
    DATA.providers = r.data.providers || {};
    DATA.models = r.data.models || {};
    DATA.bindings = r.data.bindings || {};
    DATA.system = r.data.system || {};
    DATA.simple = r.data.simple || {};
    DATA.retry_grading = r.data.retry_grading || {};
    DATA.roles = r.data.roles || {};
    DATA.key_status = r.data.key_status || {};
    META = r.data.meta || {};
    // [分层与兜底] 归一成 {层: [模型…]}（配置里可能是单值）
    TIER.tiers = {};
    const rawTiers = r.data.tiers || {};
    for (const k of Object.keys(rawTiers)) {
      const v = rawTiers[k];
      const pool = (Array.isArray(v) ? v : (v ? [v] : [])).map(String).filter(Boolean);
      if (pool.length) TIER.tiers[k] = pool;
    }
    setSaveStatus("配置已加载", false);
    // 保持当前选中；失效则回退到第一个
    if (!CURRENT || !DATA.providers[CURRENT]) {
      const names = Object.keys(DATA.providers);
      CURRENT = names.length ? names[0] : null;
    }
    renderAll();
    loadCardTypeModels();
  }

  function renderAll() {
    renderNav();
    renderDetail();
    renderBindings();
    renderAdvanced();
    renderTiers();
  }

  // ------------------------------------------------------------ 左：供应商导航列表

  function renderNav() {
    const box = $("providerNav");
    const names = Object.keys(DATA.providers);
    $("providerCount").textContent = String(names.length);
    if (!names.length) {
      box.innerHTML = '<div class="set-empty">还没有供应商。<br>点左上角「＋」添加。</div>';
      return;
    }
    box.innerHTML = names.map((name) => {
      const p = DATA.providers[name] || {};
      const models = modelsOf(name);
      const ok = p.api_key_env && DATA.key_status[p.api_key_env] && DATA.key_status[p.api_key_env].configured;
      const active = name === CURRENT ? " active" : "";
      return `
      <button class="set-nav-item${active}" data-provider="${escapeHtml(name)}">
        <span class="set-nav-logo" style="background:${logoColor(name)}">${escapeHtml((name || "?").slice(0, 2))}</span>
        <span class="set-nav-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
        <span class="set-nav-dot${ok ? " on" : ""}" title="${ok ? "密钥已配置" : "密钥未配置"}"></span>
        <span class="set-nav-count">${models.length}</span>
      </button>`;
    }).join("");
  }

  // ------------------------------------------------------------ 右：选中供应商的模型详细设置

  function renderDetail() {
    const box = $("providerDetail");
    if (!CURRENT || !DATA.providers[CURRENT]) {
      box.innerHTML = '<div class="set-empty">选择左侧一个供应商，或点左上角「＋」添加。</div>';
      return;
    }
    const name = CURRENT;
    const p = DATA.providers[name] || {};
    const models = modelsOf(name);
    const dot = p.api_key_env && DATA.key_status[p.api_key_env] && DATA.key_status[p.api_key_env].configured ? " on" : "";
    box.innerHTML = `
    <div class="set-detail-head">
      <span class="set-provider-logo" style="background:${logoColor(name)}">${escapeHtml((name || "?").slice(0, 2))}</span>
      <span class="set-detail-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
      <span class="set-pill">${escapeHtml(p.protocol || "openai")}</span>
      <span class="set-key-state"><span class="set-status-dot${dot}"></span>${dot ? "已配置 ····" + escapeHtml((DATA.key_status[p.api_key_env] || {}).tail || "") : "未配置"}</span>
      <span class="spacer" style="flex:1"></span>
      <div class="set-menu-wrap">
        <button class="set-menu-btn" data-act="menu" title="更多">⋯</button>
        <div class="set-menu">
          <button data-act="test">⚡ 测试连接</button>
          <button data-act="fetch">⇣ 拉取模型列表</button>
          <button data-act="key">🔑 修改密钥</button>
          <div class="set-menu-sep"></div>
          <button class="danger" data-act="del">✕ 删除供应商</button>
        </div>
      </div>
    </div>
    <div class="set-detail-body">
      <div class="set-grid">
        <div class="set-field">
          <label>名称 name</label>
          <input class="set-input" data-f="name" value="${escapeHtml(name)}" />
        </div>
        <div class="set-field">
          <label>协议 protocol</label>
          <input class="set-input" data-f="protocol" value="${escapeHtml(p.protocol || "")}" placeholder="openai" />
        </div>
        <div class="set-field">
          <label>密钥变量 api_key_env</label>
          <input class="set-input" data-f="api_key_env" value="${escapeHtml(p.api_key_env || "")}" placeholder="XXX_API_KEY" />
        </div>
        <div class="set-field">
          <label>拉列表地址 models_url<span class="set-hint">（可选，缺省 端点/models）</span></label>
          <input class="set-input" data-f="models_url" value="${escapeHtml(p.models_url || "")}" />
        </div>
        <div class="set-field wide">
          <label>端点 base_url</label>
          <input class="set-input" data-f="base_url" value="${escapeHtml(p.base_url || "")}" placeholder="https://…/v1" />
        </div>
        <div class="set-field wide">
          <label>说明 note</label>
          <input class="set-input" data-f="note" value="${escapeHtml(p.note || "")}" />
        </div>
      </div>

      <div class="set-models-head">
        <span class="set-models-title">模型</span>
        <span class="set-count">${models.length}</span>
        <span class="spacer" style="flex:1"></span>
        <button class="btn ghost" data-act="add-model">＋ 添加模型</button>
      </div>
      <div class="set-models-box">
        ${models.length ? models.map(modelRow).join("") : '<div class="set-empty">该供应商下还没有模型。点「＋ 添加模型」或「⋯ → 拉取模型列表」。</div>'}
      </div>
    </div>`;
  }

  // [思考强度·从模型端获取 2026-10-13] 思考下拉选项按该模型录入时从端点拉回的
  // 能力声明渲染：端点给了档位（reasoning_efforts）→ 渲染档位；没给（端点不返回
  // 能力信息，如 MiniMax）→ 回落通用 auto/on/off。auto = 不传、跟随端点默认。
  function thinkingOptions(m) {
    const t = m.thinking == null ? "auto" : String(m.thinking);
    const efforts = Array.isArray(m.reasoning_efforts) && m.reasoning_efforts.length
      ? m.reasoning_efforts : null;
    if (!efforts) {
      return `
        <option value="auto"${t === "auto" ? " selected" : ""}>auto</option>
        <option value="on"${t === "on" ? " selected" : ""}>on</option>
        <option value="off"${t === "off" ? " selected" : ""}>off</option>`;
    }
    const mandatory = !!m.reasoning_mandatory;
    const def = m.default_effort ? escapeHtml(m.default_effort) : "";
    let html = `<option value="auto"${t === "auto" ? " selected" : ""}>auto（端点默认${def ? " " + def : ""}）</option>`;
    if (!mandatory) {
      html += `<option value="off"${t === "off" ? " selected" : ""}>off（关闭思考）</option>`;
    }
    for (const e of efforts) {
      html += `<option value="${escapeHtml(e)}"${t === e ? " selected" : ""}>${escapeHtml(e)}</option>`;
    }
    // 当前值不在端点声明里（异常/旧配置）→ 补一个占位项，避免选中态丢失
    if (t !== "auto" && t !== "off" && efforts.indexOf(t) < 0) {
      html += `<option value="${escapeHtml(t)}" selected>${escapeHtml(t)}（端点未声明）</option>`;
    }
    return html;
  }

  function modelRow(mn) {
    const m = DATA.models[mn] || {};
    const t = m.thinking;
    return `
    <div class="set-model-row${EDITING.has(mn) ? " editing" : ""}" data-model="${escapeHtml(mn)}">
      <span class="set-model-bullet"></span>
      <span class="set-model-id" title="${escapeHtml(mn)}">${escapeHtml(m.model || mn)}</span>
      <span class="set-model-param">
        思考
        <select class="set-select" data-f="thinking">
          ${thinkingOptions(m)}
        </select>
      </span>
      <span class="set-model-param">
        温度
        <input class="set-input" data-f="temperature" type="number" step="0.1" min="0" max="2"
               value="${escapeHtml(m.temperature == null ? 0.7 : m.temperature)}" />
      </span>
      <span class="set-model-acts">
        <button class="set-icon-btn" data-act="edit" title="编辑详情">✎</button>
        <button class="set-icon-btn" data-act="test-model" title="测试">⚡</button>
        <button class="set-icon-btn danger" data-act="del-model" title="删除">✕</button>
      </span>
    </div>
    <div class="set-model-edit">
      <div class="set-grid">
        <div class="set-field">
          <label>键名（只读）</label>
          <input class="set-input" value="${escapeHtml(mn)}" readonly />
        </div>
        <div class="set-field">
          <label>型号 model</label>
          <input class="set-input" data-f="model" value="${escapeHtml(m.model || "")}" placeholder="端点上的模型 id（点『刷新』从端点清单拉）" />
        </div>
        <div class="set-field">
          <label>供应商（只读，挂在父供应商下）</label>
          <input class="set-input" value="${escapeHtml(m.provider || "")}" readonly />
        </div>
        <div class="set-field wide">
          <label>说明 note</label>
          <input class="set-input" data-f="note" value="${escapeHtml(m.note || "")}" />
        </div>
      </div>
      <div class="set-actions" style="margin-top:10px;display:flex;justify-content:flex-end">
        <button class="btn ghost" data-act="done">完成</button>
      </div>
    </div>`;
  }

  // ------------------------------------------------------------ 模板选择器（添加供应商）

  function openTemplatePicker() {
    $("templateList").innerHTML = [
      ...TEMPLATES.map((t) => `
        <button class="set-template-card" data-tpl="${escapeHtml(t.id)}">
          <span class="set-template-logo" style="background:${t.color}">${escapeHtml(t.name.slice(0, 2))}</span>
          <span class="set-template-meta">
            ${escapeHtml(t.name)}
            <span class="set-template-sub">${escapeHtml(t.sub)}</span>
          </span>
          <span class="set-template-arrow">›</span>
        </button>`),
      `<button class="set-template-card" data-tpl="custom">
        <span class="set-template-logo" style="background:#3a3f47">＋</span>
        <span class="set-template-meta">
          自定义供应商
          <span class="set-template-sub">自己填名称 / 端点 / 密钥变量</span>
        </span>
        <span class="set-template-arrow">›</span>
      </button>`,
    ].join("");
    $("templateModal").style.display = "flex";
  }

  function createFromTemplate(tplId) {
    let base;
    if (tplId === "custom") {
      base = "custom";
      while (DATA.providers[base]) { base = "custom-" + Math.floor(Math.random() * 1000); }
      DATA.providers[base] = { protocol: "openai", base_url: "", api_key_env: "", models_url: "", note: "自定义供应商" };
    } else {
      const t = TEMPLATES.find((x) => x.id === tplId);
      if (!t) return;
      base = t.id;
      if (DATA.providers[base]) { toast("供应商 " + base + " 已存在", true); return; }
      DATA.providers[base] = {
        protocol: t.protocol, base_url: t.base_url, api_key_env: t.api_key_env,
        models_url: t.models_url, note: "模板 " + t.name,
      };
    }
    $("templateModal").style.display = "none";
    CURRENT = base;
    renderAll();
    toast("已添加供应商 " + base + "（未保存，点右上角保存生效）");
  }

  // ------------------------------------------------------------ 角色绑定

  function renderBindings() {
    const box = $("bindingList");
    const roles = DATA.roles || {};
    const roleIds = Object.keys(roles);
    $("roleHint").textContent = roleIds.length
      ? `（${roleIds.length} 个角色，由角色注册表动态给出）` : "（角色表为空）";

    if (!roleIds.length) {
      box.innerHTML = '<div class="set-empty">角色表读不到，检查 config/roles.json。</div>';
      return;
    }

    box.innerHTML = roleIds.map((rid) => {
      const r = roles[rid] || {};
      const cur = DATA.bindings[rid];
      if (r.multi) {
        const list = Array.isArray(cur) ? cur : (cur ? [cur] : []);
        const chips = list.map((mn) => {
          const m = DATA.models[mn] || {};
          const p = m.provider || "";
          const tip = m.model && m.model !== mn ? `${mn}（${p} · ${m.model}）` : mn;
          return `<span class="set-chip" title="${escapeHtml(tip)}">${escapeHtml(m.model || mn)}<button data-rm="${escapeHtml(mn)}" title="移除">×</button></span>`;
        }).join("");
        const addOpts = modelOptions("", list, true);
        return `
        <div class="set-bind-row" data-role="${escapeHtml(rid)}">
          <span class="set-bind-name">${escapeHtml(r.display || rid)}</span>
          <span class="set-bind-note">${escapeHtml(r.note || "")}</span>
          <span style="display:flex;gap:4px;flex-wrap:wrap;align-items:center">
            ${chips || '<span class="set-note">（未绑定）</span>'}
            <select class="set-select set-bind-select" data-add-bind>
              <option value="">＋ 添加…</option>${addOpts}
            </select>
          </span>
        </div>`;
      }
      return `
      <div class="set-bind-row" data-role="${escapeHtml(rid)}">
        <span class="set-bind-name">${escapeHtml(r.display || rid)}</span>
        <span class="set-bind-note">${escapeHtml(r.note || "")}</span>
        <select class="set-select set-bind-select" data-bind-single>
          ${modelOptions(typeof cur === "string" ? cur : "")}
        </select>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------ 高级

  function renderAdvanced() {
    $("advDefaultModel").innerHTML = modelOptions(DATA.system.default_model || "");
    $("advSimpleEnabled").checked = !!(DATA.simple && DATA.simple.enabled);
  }

  // ------------------------------------------------------------ 保存

  function collect() {
    return {
      providers: DATA.providers,
      models: DATA.models,
      bindings: DATA.bindings,
      system: {
        default_model: $("advDefaultModel").value,
      },
      simple: { enabled: $("advSimpleEnabled").checked },
      retry_grading: DATA.retry_grading,
    };
  }

  async function save() {
    clearBanner();
    const btn = $("setSaveBtn");
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
      const r = await api("/api/settings/models", {
        method: "PUT",
        body: JSON.stringify(collect()),
      });
      if (r.status === 200 && r.data && r.data.ok) {
        const n = (r.data.models || []).length;
        setSaveStatus("已保存并生效（" + n + " 个模型）", false);
        toast("已保存并生效");
        await load();
        let html = `已保存并生效（${n} 个模型）· 备份：${escapeHtml(r.data.backup || "无")}`;
        const warns = r.data.warnings || [];
        if (warns.length) {
          html += `<ul>${warns.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
        }
        banner("ok", html);
      } else {
        const errs = (r.data && (r.data.errors || r.data.error)) || ["保存失败"];
        const list = Array.isArray(errs) ? errs : [errs];
        banner("err",
          `保存被拒（${r.status}），文件未改动：` +
          `<ul>${list.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>`);
        setSaveStatus("保存被拒，文件未改动", true);
        toast("保存被拒", true);
      }
    } catch (e) {
      banner("err", "保存请求失败：" + escapeHtml(String(e)));
      setSaveStatus("保存请求失败", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "保存并生效";
    }
  }

  // ------------------------------------------------------------ 拉取 → 勾选

  function openFetchModal(providerName, items) {
    FETCH_CTX = { provider: providerName, items: items || [], selected: {} };
    $("fetchModalTitle").textContent = `选择模型（${providerName}）`;
    $("fetchError").textContent = "";
    $("fetchSearch").value = "";
    renderFetchList();
    $("fetchModal").style.display = "flex";
  }

  function closeFetchModal() {
    $("fetchModal").style.display = "none";
    FETCH_CTX = null;
  }

  function renderFetchList() {
    if (!FETCH_CTX) return;
    const kw = ($("fetchSearch").value || "").trim().toLowerCase();
    const existing = new Set(Object.values(DATA.models).map((m) => m && m.model));
    const rows = FETCH_CTX.items.filter((it) => !kw || it.id.toLowerCase().indexOf(kw) >= 0);
    $("fetchCount").textContent = `共 ${FETCH_CTX.items.length} 个，显示 ${rows.length} 个`;
    $("fetchList").innerHTML = rows.length ? rows.map((it) => {
      const has = existing.has(it.id);
      const checked = FETCH_CTX.selected[it.id] ? " checked" : "";
      return `
      <label class="set-model-row${has ? " has" : ""}">
        <input type="checkbox" data-mid="${escapeHtml(it.id)}"${checked} />
        <span class="set-model-id">${escapeHtml(it.id)}</span>
        <span class="set-model-sub">${escapeHtml(it.owned_by || "")}</span>
        ${has ? '<span class="set-model-sub">已添加（勾选可刷新能力）</span>' : ""}
      </label>`;
    }).join("") : '<div class="set-empty">没有匹配的型号。</div>';
  }

  async function doFetch(providerName) {
    const p = DATA.providers[providerName] || {};
    const r = await api("/api/settings/models/fetch", {
      method: "POST",
      body: JSON.stringify({
        provider: providerName,
        base_url: p.base_url || "",
        protocol: p.protocol || "",
        api_key_env: p.api_key_env || "",
        models_url: p.models_url || "",
      }),
    });
    if (r.status !== 200 || !r.data || !r.data.ok) {
      banner("err", "拉取模型列表失败：" +
        escapeHtml((r.data && (r.data.error || r.data.detail)) || r.status));
      return;
    }
    openFetchModal(providerName, r.data.models || []);
  }

  function addSelected() {
    if (!FETCH_CTX) return;
    const picked = Object.keys(FETCH_CTX.selected).filter((k) => FETCH_CTX.selected[k]);
    if (!picked.length) {
      $("fetchError").textContent = "还没勾选任何型号。";
      return;
    }
    const prov = FETCH_CTX.provider;
    let added = 0, refreshed = 0;
    for (const id of picked) {
      // [思考强度·从模型端获取 2026-10-13] 录入时把端点返回的能力声明一起存进
      // 模型配置——界面思考下拉按它渲染；端点没给（MiniMax 等）就不带，回落通用开关。
      // [已配置模型·能力刷新 2026-10-13] 已存在（按 model 字段匹配）的模型勾选 =
      // 用端点最新返回覆盖刷新它的能力字段，不再需要删掉重加。
      const it = (FETCH_CTX.items || []).find((x) => x.id === id) || {};
      const cap = {};
      if (Array.isArray(it.reasoning_efforts) && it.reasoning_efforts.length) {
        cap.reasoning_efforts = it.reasoning_efforts;
        if (it.default_effort) cap.default_effort = it.default_effort;
        if (it.reasoning_mandatory != null) cap.reasoning_mandatory = !!it.reasoning_mandatory;
      }
      const exKey = Object.keys(DATA.models).find(
        (k) => DATA.models[k] && DATA.models[k].model === id);
      if (exKey) {
        if (Object.keys(cap).length) Object.assign(DATA.models[exKey], cap);
        refreshed++;
        continue;
      }
      const base = sanitizeKey(id);
      const key = uniqueKey(base);
      const mdef = {
        provider: prov,
        model: id,
        temperature: 0.7,
        thinking: "auto",
        note: "",
        ...cap,
      };
      DATA.models[key] = mdef;
      added++;
    }
    closeFetchModal();
    renderDetail();
    renderBindings();
    toast(`已加入 ${added} 个、刷新 ${refreshed} 个模型能力（未保存，点保存生效）`);
  }

  // ------------------------------------------------------------ 测试 / 密钥

  async function testProvider(name) {
    const p = DATA.providers[name] || {};
    const r = await api("/api/settings/models/test", {
      method: "POST",
      body: JSON.stringify({
        kind: "provider", provider: name,
        base_url: p.base_url || "", protocol: p.protocol || "",
        api_key_env: p.api_key_env || "",
      }),
    });
    if (r.data && r.data.ok) toast(`连接正常：${r.data.reply}（${r.data.latency_ms}ms）`);
    else toast("测试失败：" + ((r.data && (r.data.error || r.data.detail)) || r.status), true);
  }

  async function testModel(name) {
    const r = await api("/api/settings/models/test", {
      method: "POST",
      body: JSON.stringify({ kind: "model", model_name: name }),
    });
    if (r.data && r.data.ok) toast(`模型可用：${r.data.reply}（${r.data.latency_ms}ms）`);
    else toast("测试失败：" + ((r.data && (r.data.error || r.data.detail)) || r.status), true);
  }

  async function changeKey(providerName) {
    const p = DATA.providers[providerName] || {};
    const env = p.api_key_env || "";
    if (!env) { toast("先填 api_key_env 再改密钥", true); return; }
    const v = await dialog({
      title: `设置 ${env}`,
      body: "密钥只写入服务端环境，明文不会回显、不会进日志。保存后立即生效，不需要重启。",
      input: true,
      placeholder: "粘贴密钥后回车",
      okText: "保存",
    });
    if (v === null) return;
    if (!v.trim()) { toast("值不能为空", true); return; }
    const r = await api("/api/settings/keys/" + encodeURIComponent(env), {
      method: "PUT", body: JSON.stringify({ value: v.trim() }),
    });
    if (r.status === 200 && r.data && r.data.ok) {
      DATA.key_status[env] = { configured: true, tail: r.data.tail };
      renderNav();
      renderDetail();
      toast("密钥已保存并立即生效（不需重启）");
    } else {
      toast("密钥保存失败：" + ((r.data && (r.data.error) || r.status)), true);
    }
  }

  // ------------------------------------------------------------ 备份还原

  async function openRestore() {
    const r = await api("/api/settings/models/backups");
    RESTORE_LIST = (r.data && r.data.backups) || [];
    $("restoreError").textContent = "";
    $("restoreList").innerHTML = RESTORE_LIST.length ? RESTORE_LIST.map((b) => `
      <label class="set-model-row">
        <input type="radio" name="rb" value="${escapeHtml(b.name)}" />
        <span class="set-model-id">${escapeHtml(b.name)}</span>
        <span class="set-model-sub">${escapeHtml((b.size / 1024).toFixed(1))} KB · ${escapeHtml(b.mtime || "")}</span>
      </label>`).join("") : '<div class="set-empty">还没有备份。保存一次就会自动生成。</div>';
    $("restoreModal").style.display = "flex";
  }

  async function doRestore() {
    const picked = document.querySelector('input[name="rb"]:checked');
    if (!picked) { $("restoreError").textContent = "先选一版备份。"; return; }
    $("restoreError").textContent = "还原中…";
    const r = await api("/api/settings/models/restore", {
      method: "POST", body: JSON.stringify({ name: picked.value }),
    });
    if (r.status === 200 && r.data && r.data.ok) {
      $("restoreModal").style.display = "none";
      await load();
      toast(`已还原到 ${r.data.restored} 并生效`);
    } else {
      $("restoreError").textContent =
        "还原失败：" + ((r.data && (r.data.error || (r.data.errors || []).join("；"))) || r.status);
    }
  }

  // ------------------------------------------------------------ 工具：供应商/模型增删

  // [引用清理 2026-10-14] 删除模型后，清理所有指向它的引用：
  // bindings（角色绑定）与 system.default_model（[2026-10-19 裁定] 无降级模型）。
  //
  // 铁律（用户 2026-10-14 明确要求）：
  //   模型删了 → 引用【置空】。绝不自动改指到别的模型。
  //   （曾误做「改指剩余第一个」，会把全部角色批量改成同一个模型——用户配置被改坏，
  //    2026-10-14 事故根因。用户原话："模型如果删了，那就变成空的就行，我他妈自己会改"）
  function fixRefsAfterDelete(removed) {
    const rm = removed || [];
    if (!rm.length) return;
    // 调用方必须先 delete DATA.models[mn] 再调本函数（生产两处调用点均满足）
    // ① bindings：数组过滤掉已删模型（删空即删除该键）；单值指向已删模型 → 删除该键
    Object.keys(DATA.bindings).forEach((rid) => {
      const b = DATA.bindings[rid];
      if (Array.isArray(b)) {
        const left = b.filter((x) => !rm.includes(x));
        if (left.length) DATA.bindings[rid] = left;
        else delete DATA.bindings[rid];
      } else if (rm.includes(b)) {
        delete DATA.bindings[rid];
      }
    });
    // ② system：指向已删模型的直接清空（不改指别的模型）；[2026-10-19 裁定] 无降级模型
    ["default_model"].forEach((f) => {
      if (DATA.system && rm.includes(DATA.system[f])) {
        DATA.system[f] = "";
      }
    });
  }

  async function deleteProvider(name) {
    const used = modelsOf(name);
    if (used.length) {
      // 把要连带删掉的模型**列出来**：原生 confirm 里那串名字挤成一行纯文本，
      // 而这正是"删错了会丢什么"的唯一依据，值得排版。
      const ok = await dialog({
        title: `删除供应商「${name}」`,
        body: `有 ${used.length} 个模型挂在它下面，会一并删除：\n\n${used.map((m) => "· " + m).join("\n")}`,
        danger: true,
        okText: "删除",
      });
      if (!ok) return false;
    }
    used.forEach((mn) => { delete DATA.models[mn]; EDITING.delete(mn); });
    delete DATA.providers[name];
    fixRefsAfterDelete(used);
    if (CURRENT === name) {
      const names = Object.keys(DATA.providers);
      CURRENT = names.length ? names[0] : null;
    }
    return true;
  }

  function toggleEdit(modelKey) {
    if (EDITING.has(modelKey)) EDITING.delete(modelKey);
    else EDITING.add(modelKey);
    renderDetail();
  }

  function closeMenu() {
    document.querySelectorAll(".set-menu.open").forEach((m) => m.classList.remove("open"));
  }

  // ------------------------------------------------------------ 事件绑定

  function bind() {
    $("setSaveBtn").addEventListener("click", save);
    $("setRestoreBtn").addEventListener("click", openRestore);
    $("setBackBtn").addEventListener("click", (e) => {
      e.preventDefault();
      if (history.length > 1) history.back(); else location.href = "/";
    });

    // 添加供应商 → 模板选择器
    $("addProviderBtn").addEventListener("click", openTemplatePicker);
    $("templateCloseBtn").addEventListener("click", () => { $("templateModal").style.display = "none"; });
    $("templateList").addEventListener("click", (e) => {
      const card = e.target.closest(".set-template-card");
      if (card) createFromTemplate(card.dataset.tpl);
    });

    // 左导航：点选供应商
    $("providerNav").addEventListener("click", (e) => {
      const item = e.target.closest(".set-nav-item");
      if (!item) return;
      const name = item.dataset.provider;
      if (!name || name === CURRENT) return;
      CURRENT = name;
      renderNav();
      renderDetail();
    });

    // 右详情：菜单 + 模型操作 + 字段编辑（事件委托）
    const detail = $("providerDetail");
    detail.addEventListener("click", async (e) => {
      const prov = CURRENT;

      // 「⋯」菜单开合
      const menuBtn = e.target.closest("[data-act='menu']");
      if (menuBtn) {
        const wrap = menuBtn.closest(".set-menu-wrap");
        const menu = wrap ? wrap.querySelector(".set-menu") : null;
        const open = menu ? menu.classList.toggle("open") : false;
        if (open) {
          document.querySelectorAll(".set-menu.open").forEach((m) => { if (m !== menu) m.classList.remove("open"); });
        }
        return;
      }

      // 菜单项
      const menuItem = e.target.closest(".set-menu button");
      if (menuItem && prov) {
        const act = menuItem.dataset.act;
        closeMenu();
        if (act === "test") { await testProvider(prov); return; }
        if (act === "fetch") { await doFetch(prov); return; }
        if (act === "key") { await changeKey(prov); return; }
        if (act === "del") {
          if (await deleteProvider(prov)) {
            renderNav();
            renderDetail();
            renderBindings();
            toast("已删除（未保存，点保存才落盘）");
          }
          return;
        }
        return;
      }

      // 按钮（模型区 + 添加模型）
      const btn = e.target.closest("button[data-act]");
      if (btn) {
        const act = btn.dataset.act;
        if (act === "add-model" && prov) {
          let key = "new-model", i = 1;
          while (DATA.models[key]) { i++; key = "new-model-" + i; }
          DATA.models[key] = {
            provider: prov, model: "", temperature: 0.7, thinking: "auto", note: "",
          };
          EDITING.add(key);
          renderDetail();
          const row = document.querySelector(`.set-model-row[data-model="${CSS.escape(key)}"]`);
          if (row) row.scrollIntoView({ block: "center" });
          toast("已加空模型，填型号后保存生效");
          return;
        }
        const modelRow = e.target.closest(".set-model-row");
        if (!modelRow) return;
        if (act === "edit" || act === "done") { toggleEdit(modelRow.dataset.model); return; }
        if (act === "test-model") { await testModel(modelRow.dataset.model); return; }
        if (act === "del-model") {
          const mk = modelRow.dataset.model;
          delete DATA.models[mk];
          EDITING.delete(mk);
          fixRefsAfterDelete([mk]);
          renderDetail();
          renderBindings();
          renderAdvanced();
          toast("已删除（未保存，点保存才落盘）");
          return;
        }
      }
    });

    // 字段编辑（input）
    detail.addEventListener("input", (e) => {
      const field = e.target.dataset.f;
      if (!field) return;
      const modelRow = e.target.closest(".set-model-row");
      if (modelRow) {
        const mn = modelRow.dataset.model;
        const m = DATA.models[mn];
        if (!m) return;
        if (field === "temperature") m.temperature = parseFloat(e.target.value);
        else m[field] = e.target.value;
        return;
      }
      if (!CURRENT || !DATA.providers[CURRENT]) return;
      if (field === "name") return; // 改名走 change
      DATA.providers[CURRENT][field] = e.target.value;
    });

    // 模型思考/温度 change + 供应商改名
    detail.addEventListener("change", (e) => {
      const field = e.target.dataset.f;
      const modelRow = e.target.closest(".set-model-row");
      if (modelRow && field) {
        const mn = modelRow.dataset.model;
        const m = DATA.models[mn];
        if (!m) return;
        if (field === "temperature") m.temperature = parseFloat(e.target.value);
        else m[field] = e.target.value;
        return;
      }
      if (field !== "name" || !CURRENT) return;
      const old = CURRENT;
      const next = e.target.value.trim();
      if (!next || next === old) { e.target.value = old; return; }
      if (DATA.providers[next]) {
        toast("供应商名已存在：" + next, true);
        e.target.value = old;
        return;
      }
      const val = DATA.providers[old];
      delete DATA.providers[old];
      DATA.providers[next] = val;
      CURRENT = next;
      Object.keys(DATA.models).forEach((mn) => {
        if (DATA.models[mn] && DATA.models[mn].provider === old) {
          DATA.models[mn].provider = next;
        }
      });
      renderNav();
      renderDetail();
      toast("已改名为 " + next + "，引用同步更新");
    });

    // 角色绑定
    $("bindingList").addEventListener("change", (e) => {
      const row = e.target.closest(".set-bind-row");
      if (!row) return;
      const rid = row.dataset.role;
      if (e.target.hasAttribute("data-bind-single")) {
        const v = e.target.value;
        if (v) DATA.bindings[rid] = v; else delete DATA.bindings[rid];
      } else if (e.target.hasAttribute("data-add-bind")) {
        const v = e.target.value;
        if (!v) return;
        const cur = DATA.bindings[rid];
        const list = Array.isArray(cur) ? cur.slice() : (cur ? [cur] : []);
        list.push(v);
        DATA.bindings[rid] = list;
        renderBindings();
      }
    });

    $("bindingList").addEventListener("click", (e) => {
      const rm = e.target.closest("button[data-rm]");
      if (!rm) return;
      const rid = e.target.closest(".set-bind-row").dataset.role;
      const cur = DATA.bindings[rid];
      if (Array.isArray(cur)) {
        const list = cur.filter((x) => x !== rm.dataset.rm);
        if (list.length) DATA.bindings[rid] = list; else delete DATA.bindings[rid];
      } else {
        delete DATA.bindings[rid];
      }
      renderBindings();
    });

    // 拉取弹层
    $("fetchSearch").addEventListener("input", renderFetchList);
    $("fetchList").addEventListener("change", (e) => {
      const mid = e.target.dataset.mid;
      if (!mid || !FETCH_CTX) return;
      FETCH_CTX.selected[mid] = e.target.checked;
    });
    $("fetchAddBtn").addEventListener("click", addSelected);
    $("fetchCloseBtn").addEventListener("click", closeFetchModal);
    $("fetchCancelBtn").addEventListener("click", closeFetchModal);

    // 还原弹层
    $("restoreCloseBtn").addEventListener("click", () => { $("restoreModal").style.display = "none"; });

    // [按卡片类型设置默认模型] 保存模板
    const ctmSave = $("ctmSaveBtn");
    if (ctmSave) ctmSave.addEventListener("click", saveCardTypeModels);

    // [分层与兜底] 排序/增删 + 保存
    const tierSave = $("tierSaveBtn");
    if (tierSave) tierSave.addEventListener("click", saveTiers);
    const tierBox = $("tierList");
    if (tierBox) {
      tierBox.addEventListener("click", onTierClick);
      tierBox.addEventListener("change", onTierAdd);
    }
    $("restoreCancelBtn").addEventListener("click", () => { $("restoreModal").style.display = "none"; });
    $("restoreList").addEventListener("dblclick", () => doRestore());

    // 点其它地方关「⋯」菜单 + 关弹层
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".set-menu-wrap")) closeMenu();
    });

    document.querySelectorAll(".set-modal").forEach((m) => {
      m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".set-modal").forEach((m) => { m.style.display = "none"; });
        closeMenu();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        save();
      }
    });
  }

  bind();
  load();
})();
