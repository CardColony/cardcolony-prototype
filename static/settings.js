/* settings.js —— 模型设置页（模型设置 · 第二步）
 *
 * 沿用 shelf.js 的 IIFE 风格（无框架）。页面结构四块：顶栏 / 供应商 / 模型 / 角色绑定，
 * 角色行**由 GET 返回的 roles 动态渲染**（不写死——角色归模块侧，加角色＝加一条 JSON）。
 *
 * 三条约定：
 *   - 编辑期只改内存 DATA，不调后端；点「保存」才 PUT（保存即热生效）。
 *     拉取列表例外：走接口拿远端型号，但只往内存加卡片，未保存前可继续改。
 *   - 密钥永不回显：只显示「已配置 ····xxxx」，改密走 PUT /keys/{name}。
 *   - 400 → 顶部红条列**全部**错误，页面不关、表单不清。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // 页面上唯一的数据源；与后端 models.yaml 的六段一一对应
  let DATA = {
    providers: {}, models: {}, bindings: {},
    system: {}, simple: {}, retry_grading: {},
    roles: {}, key_status: {},
  };
  let META = {};
  let FETCH_CTX = null;   // 当前「拉取→勾选」上下文
  let RESTORE_LIST = [];

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

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, ok: res.ok, data: data };
  }

  // 型号 → 键名：anthropic/claude-haiku-5.5 → anthropic-claude-haiku-55
  // 规则：小写 → 去掉点 → 分隔符（/ _ : 空格）转 '-' → 合并连续 '-' → 去首尾 '-'
  function sanitizeKey(modelId) {
    let s = String(modelId || "").toLowerCase().replace(/\./g, "");
    s = s.replace(/[\s/:_]+/g, "-");
    s = s.replace(/[^a-z0-9-]+/g, "-");
    s = s.replace(/-+/g, "-").replace(/^-+|-+$/g, "");
    return s || "model";
  }

  // 冲突自动加后缀 -2 / -3
  function uniqueKey(base, exceptKey) {
    if (!DATA.models[base] || exceptKey === base) return base;
    let i = 2;
    while (DATA.models[base + "-" + i] && exceptKey !== base + "-" + i) i++;
    return base + "-" + i;
  }

  function modelOptions(selected) {
    const names = Object.keys(DATA.models);
    let html = '<option value="">（未绑定）</option>';
    for (const n of names) {
      const sel = n === selected ? " selected" : "";
      html += `<option value="${escapeHtml(n)}"${sel}>${escapeHtml(n)}</option>`;
    }
    return html;
  }

  // ------------------------------------------------------------ 读

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
    $("setMetaLine").textContent = META.path ? "真源：" + META.path : "";
    renderAll();
  }

  function renderAll() {
    renderProviders();
    renderModels();
    renderBindings();
    renderAdvanced();
  }

  // ------------------------------------------------------------ ① 供应商

  function keyStatusChip(name) {
    const st = DATA.key_status[name];
    if (!st || !st.configured) {
      return '<span class="set-key-state">未配置</span>';
    }
    return `<span class="set-key-state on">已配置 ····${escapeHtml(st.tail || "")}</span>`;
  }

  function renderProviders() {
    const box = $("providerList");
    const names = Object.keys(DATA.providers);
    if (!names.length) {
      box.innerHTML = '<div class="set-empty">还没有供应商，点下面「＋ 新增供应商」。</div>';
      return;
    }
    box.innerHTML = names.map((name) => {
      const p = DATA.providers[name] || {};
      return `
      <div class="set-card" data-provider="${escapeHtml(name)}">
        <div class="set-grid">
          <div class="set-field">
            <label>名称</label>
            <input class="set-input" data-f="name" value="${escapeHtml(name)}" />
          </div>
          <div class="set-field">
            <label>协议 protocol</label>
            <input class="set-input" data-f="protocol" value="${escapeHtml(p.protocol || "")}" placeholder="openai" />
          </div>
          <div class="set-field">
            <label>端点 base_url</label>
            <input class="set-input" data-f="base_url" value="${escapeHtml(p.base_url || "")}" placeholder="https://…/v1" />
          </div>
          <div class="set-field">
            <label>密钥变量 api_key_env</label>
            <input class="set-input" data-f="api_key_env" value="${escapeHtml(p.api_key_env || "")}" placeholder="XXX_API_KEY" />
          </div>
          <div class="set-field">
            <label>拉列表地址 models_url<span class="set-hint">（可选，缺省 端点/models）</span></label>
            <input class="set-input" data-f="models_url" value="${escapeHtml(p.models_url || "")}" />
          </div>
          <div class="set-field">
            <label>密钥状态</label>
            <div style="display:flex;align-items:center;gap:8px;">
              ${keyStatusChip(p.api_key_env || "")}
              <button class="btn ghost" data-act="key">改密钥</button>
            </div>
          </div>
          <div class="set-field wide">
            <label>说明 note</label>
            <input class="set-input" data-f="note" value="${escapeHtml(p.note || "")}" />
          </div>
        </div>
        <div class="set-actions">
          <button class="btn" data-act="fetch">拉取模型列表</button>
          <button class="btn ghost" data-act="test">测试连接</button>
          <button class="btn ghost" data-act="key">改密钥</button>
          <span class="spacer" style="flex:1"></span>
          <button class="btn danger" data-act="del">删除</button>
        </div>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------ ② 模型

  function renderModels() {
    const box = $("modelList");
    const names = Object.keys(DATA.models);
    if (!names.length) {
      box.innerHTML = '<div class="set-empty">还没有模型。可以「拉取模型列表」勾选，或手动新增。</div>';
      return;
    }
    const provOpts = Object.keys(DATA.providers)
      .map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
    box.innerHTML = names.map((name) => {
      const m = DATA.models[name] || {};
      const t = m.thinking;
      return `
      <div class="set-card" data-model="${escapeHtml(name)}">
        <div class="set-grid">
          <div class="set-field">
            <label>键名</label>
            <input class="set-input" data-f="key" value="${escapeHtml(name)}" readonly />
          </div>
          <div class="set-field">
            <label>显示名 label<span class="set-hint">（可选）</span></label>
            <input class="set-input" data-f="label" value="${escapeHtml(m.label || "")}" />
          </div>
          <div class="set-field">
            <label>供应商</label>
            <select class="set-select" data-f="provider">
              ${provOpts.replace(`value="${escapeHtml(m.provider || "")}"`,
                                 `value="${escapeHtml(m.provider || "")}" selected`)}
            </select>
          </div>
          <div class="set-field">
            <label>型号 model</label>
            <input class="set-input" data-f="model" value="${escapeHtml(m.model || "")}" placeholder="anthropic/claude-haiku-5.5" />
          </div>
          <div class="set-field">
            <label>温度 temperature</label>
            <input class="set-input" data-f="temperature" type="number" step="0.1" min="0" max="2"
                   value="${escapeHtml(m.temperature == null ? 0.7 : m.temperature)}" />
          </div>
          <div class="set-field">
            <label>思考 thinking</label>
            <select class="set-select" data-f="thinking">
              <option value="auto"${t === "auto" || t == null ? " selected" : ""}>auto（不传参数）</option>
              <option value="on"${t === "on" ? " selected" : ""}>on（开思考）</option>
              <option value="off"${t === "off" ? " selected" : ""}>off（关思考）</option>
            </select>
          </div>
          <div class="set-field wide">
            <label>说明 note</label>
            <input class="set-input" data-f="note" value="${escapeHtml(m.note || "")}" />
          </div>
        </div>
        <div class="set-actions">
          <button class="btn ghost" data-act="test">测试</button>
          <span class="spacer" style="flex:1"></span>
          <button class="btn danger" data-act="del">删除</button>
        </div>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------ ③ 角色绑定

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
        const chips = list.map((mn) =>
          `<span class="set-chip">${escapeHtml(mn)}<button data-rm="${escapeHtml(mn)}" title="移除">×</button></span>`
        ).join("");
        const addOpts = Object.keys(DATA.models)
          .filter((n) => list.indexOf(n) < 0)
          .map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
        return `
        <div class="set-bind-row" data-role="${escapeHtml(rid)}">
          <span class="set-bind-name">${escapeHtml(r.display || rid)}</span>
          <span class="set-bind-note">${escapeHtml(r.note || "")}</span>
          ${chips || '<span class="set-note">（未绑定）</span>'}
          <select class="set-select set-bind-select" data-add-bind>
            <option value="">＋ 添加…</option>${addOpts}
          </select>
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

  // ------------------------------------------------------------ ④ 高级

  function renderAdvanced() {
    $("advDefaultModel").innerHTML = modelOptions(DATA.system.default_model || "");
    $("advFallbackModel").innerHTML = modelOptions(DATA.system.fallback_model || "");
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
        fallback_model: $("advFallbackModel").value,
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
        toast("已保存并生效");
        await load();          // 重读：拿到热重载后的真实状态
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
        toast("保存被拒", true);
      }
    } catch (e) {
      banner("err", "保存请求失败：" + escapeHtml(String(e)));
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
    // 已存在于 models 的型号 → 置灰标「已添加」，不可重复勾
    const existing = new Set(Object.values(DATA.models).map((m) => m && m.model));
    const rows = FETCH_CTX.items.filter((it) => !kw || it.id.toLowerCase().indexOf(kw) >= 0);
    $("fetchCount").textContent = `共 ${FETCH_CTX.items.length} 个，显示 ${rows.length} 个`;
    $("fetchList").innerHTML = rows.length ? rows.map((it) => {
      const has = existing.has(it.id);
      const checked = FETCH_CTX.selected[it.id] ? " checked" : "";
      return `
      <label class="set-model-row${has ? " disabled" : ""}">
        <input type="checkbox" data-mid="${escapeHtml(it.id)}"${checked}${has ? " disabled" : ""} />
        <span class="set-model-id">${escapeHtml(it.id)}</span>
        <span class="set-model-owner">${escapeHtml(it.owned_by || "")}</span>
        ${has ? '<span class="set-model-owner">已添加</span>' : ""}
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
    for (const id of picked) {
      const base = sanitizeKey(id);
      const key = uniqueKey(base);
      DATA.models[key] = {
        provider: prov,
        model: id,
        temperature: 0.7,
        thinking: "auto",
        note: "",
      };
    }
    closeFetchModal();
    renderModels();
    renderBindings();
    renderAdvanced();
    toast(`已加入 ${picked.length} 个模型（未保存，点右上角保存生效）`);
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
      renderProviders();
      toast("密钥已保存并立即生效（不需重启）");
    } else {
      toast("密钥保存失败：" + ((r.data && r.data.error) || r.status), true);
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
        <span class="set-model-owner">${escapeHtml((b.size / 1024).toFixed(1))} KB · ${escapeHtml(b.mtime || "")}</span>
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

  // ------------------------------------------------------------ 事件绑定

  function bind() {
    $("setSaveBtn").addEventListener("click", save);
    $("setRestoreBtn").addEventListener("click", openRestore);
    $("setBackBtn").addEventListener("click", (e) => {
      e.preventDefault();
      if (history.length > 1) history.back(); else location.href = "/";
    });

    $("addProviderBtn").addEventListener("click", () => {
      let name = "new-provider", i = 1;
      while (DATA.providers[name]) { i++; name = "new-provider-" + i; }
      DATA.providers[name] = {
        protocol: "openai", base_url: "", api_key_env: "", note: "",
      };
      renderProviders();
      const cards = document.querySelectorAll("#providerList .set-card");
      const last = cards[cards.length - 1];
      if (last) { last.scrollIntoView({ behavior: "smooth", block: "center" }); }
    });

    $("addModelBtn").addEventListener("click", () => {
      const firstProv = Object.keys(DATA.providers)[0];
      if (!firstProv) { toast("先加一个供应商", true); return; }
      let key = "new-model", i = 1;
      while (DATA.models[key]) { i++; key = "new-model-" + i; }
      DATA.models[key] = {
        provider: firstProv, model: "", temperature: 0.7, thinking: "auto", note: "",
      };
      renderModels();
    });

    // —— 供应商卡：编辑 + 按钮 ——
    $("providerList").addEventListener("input", (e) => {
      const card = e.target.closest(".set-card");
      const field = e.target.dataset.f;
      if (!card || !field || field === "name") return;
      const name = card.dataset.provider;
      if (!DATA.providers[name]) return;
      if (field === "protocol" || field === "api_key_env") {
        DATA.providers[name][field] = e.target.value.trim();
      } else {
        DATA.providers[name][field] = e.target.value;
      }
    });

    // 改名：连带把模型里的引用改掉（否则改完名全部模型指向空供应商）
    $("providerList").addEventListener("change", (e) => {
      if (e.target.dataset.f !== "name") return;
      const card = e.target.closest(".set-card");
      const old = card.dataset.provider;
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
      Object.keys(DATA.models).forEach((mn) => {
        if (DATA.models[mn] && DATA.models[mn].provider === old) {
          DATA.models[mn].provider = next;
        }
      });
      renderProviders();
      renderModels();
      toast("已改名为 " + next + "，引用同步更新");
    });

    $("providerList").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const card = btn.closest(".set-card");
      const name = card.dataset.provider;
      const act = btn.dataset.act;
      if (act === "fetch") await doFetch(name);
      else if (act === "test") await testProvider(name);
      else if (act === "key") await changeKey(name);
      else if (act === "del") {
        const used = Object.keys(DATA.models)
          .filter((mn) => DATA.models[mn] && DATA.models[mn].provider === name);
        if (used.length &&
            !window.confirm(`有 ${used.length} 个模型挂在它下面（${used.join("、")}），仍要删除？`)) {
          return;
        }
        used.forEach((mn) => delete DATA.models[mn]);
        delete DATA.providers[name];
        renderAll();
        toast("已删除（未保存，点保存才落盘）");
      }
    });

    // —— 模型卡：编辑 + 按钮 ——
    $("modelList").addEventListener("input", (e) => {
      const card = e.target.closest(".set-card");
      const field = e.target.dataset.f;
      if (!card || !field || field === "key") return;
      const name = card.dataset.model;
      const m = DATA.models[name];
      if (!m) return;
      if (field === "temperature") m.temperature = parseFloat(e.target.value);
      else m[field] = e.target.value;
    });

    $("modelList").addEventListener("change", (e) => {
      const field = e.target.dataset.f;
      if (field !== "provider" && field !== "thinking") return;
      const card = e.target.closest(".set-card");
      const m = DATA.models[card.dataset.model];
      if (m) m[field] = e.target.value;
    });

    $("modelList").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const card = btn.closest(".set-card");
      const name = card.dataset.model;
      if (btn.dataset.act === "test") await testModel(name);
      else if (btn.dataset.act === "del") {
        // 从绑定里摘掉，免得留下悬空引用
        Object.keys(DATA.bindings).forEach((rid) => {
          const b = DATA.bindings[rid];
          if (Array.isArray(b)) DATA.bindings[rid] = b.filter((x) => x !== name);
          else if (b === name) delete DATA.bindings[rid];
        });
        delete DATA.models[name];
        renderAll();
        toast("已删除（未保存，点保存才落盘）");
      }
    });

    // —— 角色绑定 ——
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

    // —— 拉取弹层 ——
    $("fetchSearch").addEventListener("input", renderFetchList);
    $("fetchList").addEventListener("change", (e) => {
      const mid = e.target.dataset.mid;
      if (!mid || !FETCH_CTX) return;
      FETCH_CTX.selected[mid] = e.target.checked;
    });
    $("fetchAddBtn").addEventListener("click", addSelected);
    $("fetchCloseBtn").addEventListener("click", closeFetchModal);
    $("fetchCancelBtn").addEventListener("click", closeFetchModal);

    // —— 还原弹层 ——
    $("restoreCloseBtn").addEventListener("click", () => { $("restoreModal").style.display = "none"; });
    $("restoreCancelBtn").addEventListener("click", () => { $("restoreModal").style.display = "none"; });
    $("restoreList").addEventListener("dblclick", () => doRestore());

    // 弹层点遮罩关闭
    document.querySelectorAll(".set-modal").forEach((m) => {
      m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".set-modal").forEach((m) => { m.style.display = "none"; });
      }
      // Ctrl+S 保存
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        save();
      }
    });
  }

  bind();
  load();
})();