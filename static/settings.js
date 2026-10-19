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
  const TEMPLATES = [
    { id: "nous",     name: "Nous",        sub: "claude-haiku-5.5 / deepseek-v4-pro", color: "#5c6bc0", protocol: "openai",   base_url: "https://inference-api.nousresearch.com/v1", api_key_env: "NOUS_API_KEY", models_url: "" },
    { id: "deepseek", name: "DeepSeek",    sub: "官方 deepseek 系列",                  color: "#4d6bfe", protocol: "deepseek", base_url: "", api_key_env: "DEEPSEEK_API_KEY", models_url: "https://api.deepseek.com/models" },
    { id: "zhipu",    name: "智谱 GLM",     sub: "glm-5.3 / glm-5.3-flash",           color: "#3859ff", protocol: "openai",   base_url: "https://open.bigmodel.cn/api/coding/paas/v4", api_key_env: "ZHIPU_CODING_API_KEY", models_url: "" },
    { id: "minimax",  name: "MiniMax",     sub: "MiniMax-M3",                          color: "#151515", protocol: "openai",   base_url: "https://api.minimaxi.com/v1", api_key_env: "MINIMAX_API_KEY", models_url: "" },
    { id: "aliyun",   name: "阿里云",       sub: "qwen3.8-max",                         color: "#ff6a00", protocol: "openai",   base_url: "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", api_key_env: "ALIYUN_TOKEN_PLAN_API_KEY", models_url: "" },
    { id: "opencode", name: "OpenCode",    sub: "deepseek-v4.1-flash",                 color: "#22d3ee", protocol: "openai",   base_url: "https://opencode.ai/zen/go/v1", api_key_env: "OPENCODE_API_KEY", models_url: "" },
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
          <input class="set-input" data-f="model" value="${escapeHtml(m.model || "")}" placeholder="anthropic/claude-haiku-5.5" />
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
    const v = window.prompt("输入 " + env + " 的值（明文不会回显、不会进日志）");
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

  function deleteProvider(name) {
    const used = modelsOf(name);
    if (used.length &&
        !window.confirm(`有 ${used.length} 个模型挂在它下面（${used.join("、")}），仍要删除？`)) {
      return false;
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
          if (deleteProvider(prov)) {
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
