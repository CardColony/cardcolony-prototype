/* workspace.js - [T17-F4/F5] 卡片工作区：左栏对话导航 + 中区讨论区 + 右栏 Inspector
   （原「左35%资料 + 右65%讨论区」结构退役；编辑能力整体迁入右栏 Inspector） */
(function () {
  "use strict";

  const WORK_ID = window.__WORK_ID__;
  const ws = document.getElementById("workspace");
  const inspBody = document.getElementById("inspBody");
  const inspector = document.getElementById("inspector");
  const navTree = document.getElementById("navTree");
  const chatArea = document.getElementById("chatArea");
  const inputBox = document.getElementById("inputBox");
  const sendBtn = document.getElementById("sendBtn");

  let currentCard = null;     // 完整 card 数据 {meta, body}
  let lastSentContent = "";   // 用于失败重发（不重复追加用户消息）
  // [I2 主界面优化] 结论区只读优先：编辑态与草稿挂模块级——renderInspector 重渲
  // （如加删关系触发）时恢复编辑态，不丢未保存内容
  let conclEditing = false;
  let conclDraft = "";

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function formatMsgContent(text) {
    // 换行转 <br>，```代码块``` 转 <pre>
    const parts = text.split(/```/);
    let html = "";
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        // 代码块
        html += `<pre>${escapeHtml(parts[i])}</pre>`;
      } else {
        html += escapeHtml(parts[i]).replace(/\n/g, "<br>");
      }
    }
    return html;
  }

  // [T17-F6] 三态视图切换：data-view 驱动 #viewport/#workspace 显隐 + nav 宽窄
  function setAppView(view) {
    const main = document.getElementById("appMain");
    const nav = document.getElementById("appNav");
    if (main) main.dataset.view = view;
    if (nav) nav.classList.toggle("rail", view === "canvas");
  }
  window.SetAppView = setAppView;

  async function open(cardId) {
    try {
      const card = await API.getCard(WORK_ID, cardId);
      currentCard = card;
      await renderInspector();
      await renderChat();
      // [刷新恢复 2026-10-03] 卡号写进 URL hash：刷新后按 hash 自动重开这张卡。
      // 用 replaceState 不堆积浏览历史、不触发页面重载；URL 即状态，不引入前端存储。
      if (location.hash !== "#" + cardId) {
        history.replaceState(null, "", "#" + cardId);
      }
      // [T17] 视图切换：进入对话态（nav 260px、workspace 可见）；.active 旧机制退役
      setAppView("chat");
      document.getElementById("wsHeadTitle").textContent = card.meta.title || "";
      // 导航树重渲以应用当前卡高亮与祖先路径展开
      await renderNav();
      // [phase8.4.1] 面板可见后再锁定滚底（visibility 方案下排版恒有效，此处保底）
      scrollChatBottom();
      // [phase8.6-C] 同理：textarea auto-grow 必须在面板可见后执行
      autoGrowAll();
    } catch (e) { /* toast 已处理 */ }
  }
  window.Workspace = { open };

  // [T17-F4] renderLeft() 拆二之一：renderInspector()——原左栏 DOM 原样迁入右栏
  // inspBody（仅去掉 wsBackBtn：返回按钮上移到对话区头部，见 work.html .ws-head）。
  // 职责区/结论区/否决/关系/历史/建子卡/删卡 逻辑一字未动（R-021）。
  async function renderInspector() {
    const m = currentCard.meta;
    const b = currentCard.body;

    const relations = m.relations || [];
    // [phase8.6-E] 每条关系尾部 ✕ 删除按钮（data-idx 定位）
    const relationsHtml = relations.length === 0
      ? `<div class="rej-empty">（无关系）</div>`
      : relations.map((r, i) => `<div class="rel-item" data-target="${escapeHtml(r.target || "")}">
            <span class="rel-type">${escapeHtml(r.type || "")} →</span>
            <span>${escapeHtml(r.target || "")}</span>
            <button class="rel-del" data-idx="${i}" title="删除该关系">✕</button>
          </div>`).join("");

    // [phase8.6-E] 添加关系行：目标卡下拉（画布卡集，排除自身）+ 类型下拉
    const canvasCards = (window.GetCanvasState ? (window.GetCanvasState().cards || []) : [])
      .filter(c => c.card_id !== m.card_id);
    const targetOptions = canvasCards
      .map(c => `<option value="${escapeHtml(c.card_id)}">${escapeHtml(c.card_id)} · ${escapeHtml(c.title)}</option>`)
      .join("");
    const relAddHtml = `
      <div class="rel-add-row">
        <select id="relTargetSel"><option value="">＋ 添加关系：选目标卡</option>${targetOptions}</select>
        <select id="relTypeSel">
          <!-- [诸葛 2026-10-04 21:54] 与后端对齐：后端 MODEL_REL_TYPES 已收窄为「先后／关联」
               （T07 关闭了类型退役入口）。原下拉仍列「锚定／兑现」——用户能手动加模型被禁的关系。
               删这两项。 -->
          <option value="先后">先后</option>
          <option value="关联">关联</option>
        </select>
        <button class="btn" id="relAddBtn">确定</button>
      </div>`;

    const rejections = (b.rejections || "").trim();
    const rejHtml = rejections
      ? rejections.split("\n").filter(s => s.trim()).map(line => `<div class="rej-item">${escapeHtml(line)}</div>`).join("")
      : `<div class="rej-empty">（暂无否决）</div>`;

    const history = b.history || [];
    const historyCount = history.length;

    inspBody.innerHTML = `
      <div class="ws-title" id="wsTitle">${escapeHtml(m.title)}</div>
      <div style="margin:8px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span class="type-badge" data-type="${escapeHtml(m.card_type)}">${escapeHtml(m.card_type)}卡</span>
        <span style="font-size:11px;color:var(--dimmer);margin-left:auto;">${escapeHtml(m.card_id)} · ${escapeHtml(m.source)}</span>
      </div>

      <div class="blocktitle">职责区 · charter <span class="note">由上级卡对话创建 · 可编辑</span></div>
      <textarea class="ws-edit" id="charterEdit" placeholder="使命 / 方法 / 权能">${escapeHtml(b.charter || "")}</textarea>
      <div class="edit-row">
        <button class="btn" id="saveCharter">保存职责区</button>
        <span class="save-status" id="charterStatus"></span>
      </div>

      <div class="blocktitle">结论区</div>
      <div class="conclusion-display${(b.conclusion || "").trim() ? "" : " empty"}" id="conclusionView">${(b.conclusion || "").trim() ? escapeHtml(b.conclusion) : "（暂无结论）"}</div>
      <div class="edit-row" id="conclViewRow">
        <button class="btn" id="editConclusion">编辑结论</button>
        <span class="save-status" id="conclusionStatus"></span>
      </div>
      <textarea class="ws-edit" id="conclusionEdit" placeholder="本卡的结论（第一条建议是一句话摘要）" style="display:none">${escapeHtml(b.conclusion || "")}</textarea>
      <div class="edit-row" id="conclEditRow" style="display:none">
        <button class="btn" id="saveConclusion">保存结论</button>
        <button class="btn ghost" id="cancelConclusion">取消</button>
      </div>

      <div class="blocktitle">否决记录</div>
      ${rejHtml}

      <div class="blocktitle">关系</div>
      ${relationsHtml}
      ${relAddHtml}

      <div class="blocktitle">修改历史（${historyCount} 条）</div>
      <div class="history-block">
        <div class="history-toggle" id="historyToggle">▸ 展开/折叠</div>
        <div class="history-list" id="historyList">
          ${history.slice().reverse().map(h => `<div class="h-item">${escapeHtml(h)}</div>`).join("")}
        </div>
      </div>

      <div class="blocktitle">＋ 建子卡</div>
      <div class="add-child-row">
        <input type="text" id="childTitle" placeholder="子卡标题">
        <!-- D-06（T07）：只关新建入口、不删白名单 —— 四个废弃类型名不再提供；ALLOWED_CARD_TYPE 六值元组原样保留 -->
        <select id="childType">
          <option value="线索">线索</option>
        </select>
        <button class="btn" id="addChildBtn">建</button>
      </div>

      ${m.card_type !== "剧本" ? `
      <div class="blocktitle" style="color:#d05656;">危险操作</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:24px;flex-wrap:wrap;">
        <button class="btn" id="deleteCardBtn" style="color:#d05656;border-color:#d05656;">删除此卡</button>
        <span style="font-size:11px;color:var(--dimmer);">连同会话删除；有子卡时系统拒绝；剧本卡无此操作</span>
      </div>` : ""}
    `;

    bindInspectorEvents();
    // [I2] 恢复结论区编辑态（若有草稿）：重渲不丢未保存内容
    applyConclusionState();
    // [phase8.6-C] 面板已可见时（如保存后重渲染）立即撑高；首次 open 由 active 后统一触发
    autoGrowAll();
  }

  // [phase8.6-C] 文本区高度自适应：内容多高就多高，上限由 CSS max-height 兜底
  function autoGrowEl(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }
  function autoGrowAll() {
    autoGrowEl(document.getElementById("charterEdit"));
    autoGrowEl(document.getElementById("conclusionEdit"));
  }

  // [I2 主界面优化] 结论区显示/编辑两态切换（只读优先：默认只读、显式进编辑）
  function applyConclusionState() {
    const view = document.getElementById("conclusionView");
    const viewRow = document.getElementById("conclViewRow");
    const editEl = document.getElementById("conclusionEdit");
    const editRow = document.getElementById("conclEditRow");
    if (!view || !editEl || !viewRow || !editRow) return;
    if (conclEditing) {
      editEl.value = conclDraft;
      view.style.display = "none";
      viewRow.style.display = "none";
      editEl.style.display = "";
      editRow.style.display = "";
      autoGrowEl(editEl);
    } else {
      view.style.display = "";
      viewRow.style.display = "";
      editEl.style.display = "none";
      editRow.style.display = "none";
    }
  }

  // [T17-F5] bindLeftEvents() 拆二之一：bindInspectorEvents()——charter／conclusion 保存、
  // 关系增删、历史折叠、建子卡、删卡全部原样保留；wsBackBtn 处理上移到静态初始化
  // （返回画布＝纯显隐切换，不再走 ReloadCanvas，见 R-1/R-043）。
  function bindInspectorEvents() {
    // [phase8.6-C] 输入时实时撑高
    ["charterEdit", "conclusionEdit"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", () => autoGrowEl(el));
    });
    // [I2] 编辑态草稿实时入账：重渲恢复不丢
    const conclEditEl = document.getElementById("conclusionEdit");
    if (conclEditEl) conclEditEl.addEventListener("input", () => { conclDraft = conclEditEl.value; });

    // 标题编辑
    const titleEl = document.getElementById("wsTitle");
    titleEl.onclick = () => {
      const input = document.createElement("input");
      input.type = "text";
      input.className = "ws-title-edit";
      input.value = currentCard.meta.title;
      titleEl.replaceWith(input);
      input.focus();
      input.select();
      const save = async () => {
        const v = input.value.trim();
        if (!v) { API.toast("标题不能为空", "error"); return; }
        if (v === currentCard.meta.title) { input.replaceWith(titleEl); return; }
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { title: v }, "用户修改标题");
          currentCard = r;
          await renderInspector();
          await renderNav();
          document.getElementById("wsHeadTitle").textContent = currentCard.meta.title || "";
        } catch (e) { /* toast 已处理 */ }
      };
      input.onblur = save;
      input.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = currentCard.meta.title; input.blur(); }
      };
    };

    // charter 保存
    document.getElementById("saveCharter").onclick = async () => {
      const v = document.getElementById("charterEdit").value;
      const status = document.getElementById("charterStatus");
      status.textContent = "保存中…";
      try {
        const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { charter: v }, "用户编辑职责区");
        currentCard = r;
        status.textContent = "已保存";
        setTimeout(() => status.textContent = "", 2000);
      } catch (e) { status.textContent = "失败"; }
    };

    // conclusion 保存：[I2 主界面优化] 保存回只读态；再改须显式「编辑结论」
    document.getElementById("saveConclusion").onclick = async () => {
      const v = document.getElementById("conclusionEdit").value;
      const status = document.getElementById("conclusionStatus");
      status.textContent = "保存中…";
      try {
        const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { conclusion: v }, "用户编辑结论区");
        currentCard = r;
        conclEditing = false;
        conclDraft = "";
        const view = document.getElementById("conclusionView");
        const nv = (r.body.conclusion || "").trim();
        if (view) {
          view.textContent = nv ? r.body.conclusion : "（暂无结论）";
          view.classList.toggle("empty", !nv);
        }
        applyConclusionState();
        status.textContent = "已保存";
        setTimeout(() => status.textContent = "", 2000);
      } catch (e) { status.textContent = "失败"; }
    };

    // [I2] 显式进编辑 / 取消弃改
    const editConclBtn = document.getElementById("editConclusion");
    if (editConclBtn) editConclBtn.onclick = () => {
      conclEditing = true;
      conclDraft = currentCard.body.conclusion || "";
      applyConclusionState();
      document.getElementById("conclusionEdit").focus();
    };
    const cancelConclBtn = document.getElementById("cancelConclusion");
    if (cancelConclBtn) cancelConclBtn.onclick = () => {
      conclEditing = false;
      conclDraft = "";
      applyConclusionState();
    };

    // 关系项跳转
    inspBody.querySelectorAll(".rel-item").forEach(item => {
      item.onclick = () => {
        const target = item.dataset.target;
        if (target) open(target);
      };
    });

    // [phase8.6-E] 删除关系：PATCH 删除该条后的完整数组，成功后就地重渲染
    inspBody.querySelectorAll(".rel-del").forEach(btn => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const idx = parseInt(btn.dataset.idx, 10);
        const rels = (currentCard.meta.relations || []).slice();
        if (isNaN(idx) || idx < 0 || idx >= rels.length) return;
        const removed = rels.splice(idx, 1)[0];
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id,
            { relations: rels }, `用户删除关系：${removed.type || ""} → ${removed.target || ""}`);
          currentCard = r;
          await renderInspector();
        } catch (e2) { /* toast 已处理 */ }
      };
    });

    // [phase8.6-E] 添加关系：目标 + 类型 → PATCH 追加，成功后就地重渲染
    const relAddBtn = document.getElementById("relAddBtn");
    if (relAddBtn) {
      relAddBtn.onclick = async () => {
        const target = document.getElementById("relTargetSel").value;
        const type = document.getElementById("relTypeSel").value;
        if (!target) { API.toast("请选择目标卡", "error"); return; }
        const rels = (currentCard.meta.relations || []).slice();
        if (rels.some(r => r.target === target && r.type === type)) {
          API.toast("该关系已存在", "error"); return;
        }
        rels.push({ target, type });
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id,
            { relations: rels }, `用户添加关系：${type} → ${target}`);
          currentCard = r;
          await renderInspector();
        } catch (e2) { /* toast 已处理 */ }
      };
    }

    // 历史折叠
    document.getElementById("historyToggle").onclick = () => {
      const list = document.getElementById("historyList");
      list.classList.toggle("expanded");
    };

    // 建子卡
    document.getElementById("addChildBtn").onclick = async () => {
      const title = document.getElementById("childTitle").value.trim();
      if (!title) { API.toast("请输入子卡标题", "error"); return; }
      const ctype = document.getElementById("childType").value;
      try {
        await API.createCard(WORK_ID, {
          title, source: "用户直接创建",
          card_type: ctype, parent: currentCard.meta.card_id,
        });
        document.getElementById("childTitle").value = "";
        API.toast("已建子卡：" + title, "success");
        // [T17] 建卡即时刷新画布数据与导航树（视图不动；画布隐藏态仍可安全重渲）
        if (window.ReloadCanvas) window.ReloadCanvas();
        await renderNav();
      } catch (e) { /* toast 已处理 */ }
    };
    document.getElementById("childTitle").onkeydown = (e) => {
      if (e.key === "Enter") document.getElementById("addChildBtn").click();
    };

    // [用户删卡 2026-10-02] prompt 输入标题前三字放行；成功后返回画布并重拉
    const delBtn = document.getElementById("deleteCardBtn");
    if (delBtn) {
      delBtn.onclick = async () => {
        const title = currentCard.meta.title || "";
        const head = title.slice(0, 3);
        if (!head) { API.toast("标题为空，无法校验", "error"); return; }
        const input = window.prompt(
          `删除此卡不可撤销（连同会话；git 可回溯）。\n请输入标题前三个字放行：「${head}」`
        );
        if (input === null) return;
        if (input.trim() !== head) { API.toast("校验不符，未删除", "error"); return; }
        const cid = currentCard.meta.card_id;
        try {
          await API.deleteCard(WORK_ID, cid);
          API.toast("已删除：" + title, "success");
          if (window.ReloadCanvas) window.ReloadCanvas();
          setAppView("canvas");
          await renderNav();
        } catch (e) { /* toast 已处理（后端 error 文本透传） */ }
      };
    }
  }

  // ==================== [T17-F4] 左栏对话导航（读 /api/nav，服务端排序原样渲染） ====================
  // 层级口径（R-016/R-044，写死）：项目根卡＝第 0 层恒定；与画布 state.viewRoot 的
  // 相对深度是两套口径，互不同步、不做互相写入。展开态为本模块私有（R-3）。
  let navData = null;               // /api/nav 响应缓存（60s 重算相对时间时复用）
  let navProjectsOpen = new Set();  // 展开中的项目 work_id 集
  let navOpenCards = new Set();     // 展开中的卡节点集（默认空＝只展一级）
  let navPathAppliedFor = "";       // 当前卡祖先路径只自动展开一次（用户手动折叠不被顶回）

  function fmtRel(iso) {
    // 相对时间：刚刚／N 分钟前／N 小时前／N 天前／YYYY-MM-DD（方案 §3.5）
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

  async function renderNav() {
    try {
      navData = await API.getNav();
    } catch (e) {
      return;   // toast 已由 api.js 处理；导航失败不影响画布与对话
    }
    renderNavFromCache();
  }
  window.RenderNav = renderNav;   // canvas.js 建卡后同步刷新导航用

  function ensureCurrentPath() {
    // 当前卡所在路径自动展开＋其项目自动展开（每次换卡应用一次，不与用户手折对抗）
    const curId = currentCard ? currentCard.meta.card_id : "";
    if (!curId || navPathAppliedFor === curId || !navData) return;
    const proj = (navData.projects || []).find(p => (p.cards || []).some(c => c.card_id === curId));
    if (proj) {
      navProjectsOpen.add(proj.work_id);
      const byId = new Map((proj.cards || []).map(c => [c.card_id, c]));
      let cur = byId.get(curId);
      while (cur && cur.parent && byId.has(cur.parent)) {
        navOpenCards.add(cur.parent);
        cur = byId.get(cur.parent);
      }
    }
    navPathAppliedFor = curId;
  }

  function renderNavFromCache() {
    if (!navData) return;
    ensureCurrentPath();
    const projects = navData.projects || [];
    if (!projects.length) {
      navTree.innerHTML = `<div class="nav-empty">还没有项目<br>
        <a href="/" class="btn primary" style="margin-top:10px;display:inline-block;">＋ 新建项目</a></div>`;
      return;
    }
    const curId = currentCard ? currentCard.meta.card_id : "";
    // [B3 主界面优化] 不给色点配说明书（图例不引入）；提示行按 B1/B2/B5 新语义
    navTree.innerHTML = `<div class="nav-hint">▸/▾ 展开 · 点行进对话 · 「打开 →」进项目画布</div>`
      + projects.map(p => renderProject(p, curId)).join("");
  }

  function renderProject(p, curId) {
    const cards = p.cards || [];
    const byId = new Map(cards.map(c => [c.card_id, c]));
    const childrenMap = new Map();   // parent_card_id → 子卡数组（保持服务端降序）
    const orphans = [];              // parent 指向已删卡 → 根层末尾（契约，不丢弃）
    for (const c of cards) {
      const parent = c.parent || "";
      if (!parent || parent === p.work_card_id) {
        if (!childrenMap.has("")) childrenMap.set("", []);
        childrenMap.get("").push(c);
      } else if (byId.has(parent)) {
        if (!childrenMap.has(parent)) childrenMap.set(parent, []);
        childrenMap.get(parent).push(c);
      } else {
        orphans.push(c);
      }
    }
    const isOpen = navProjectsOpen.has(p.work_id);
    const isCur = p.work_id === WORK_ID;
    let rows = "";
    if (isOpen) {
      rows = renderCardRows(childrenMap.get("") || [], 0, p, childrenMap, curId);
      if (orphans.length) rows += renderCardRows(orphans, 0, p, childrenMap, curId);
    }
    return `<div class="nav-pgroup">
      <div class="nav-phead${isCur ? " current" : ""}" data-wid="${escapeHtml(p.work_id)}" data-root="${escapeHtml(p.work_card_id || "")}" title="${escapeHtml(p.title)}">
        <span class="nav-twist" data-ptwist="${escapeHtml(p.work_id)}" title="展开/收起">${isOpen ? "▾" : "▸"}</span>
        <span class="dot root"></span>
        <span class="nav-pname">${escapeHtml(p.title)}</span>
        <span class="nav-pcount">${p.card_count}卡</span>
        <button class="nav-popen" data-act="open" data-wid="${escapeHtml(p.work_id)}" title="打开该项目画布">打开 →</button>
        <span class="nav-ptime">${fmtRel(p.last_active_at)}</span>
        <button class="nav-padd" data-add="${escapeHtml(p.work_id)}" title="＋ 新建卡">＋卡</button>
      </div>
      ${rows}
    </div>`;
  }

  function renderCardRows(nodes, depth, p, childrenMap, curId) {
    let html = "";
    for (const c of nodes) {
      const hasKids = (c.child_count || 0) > 0;
      const open = navOpenCards.has(c.card_id);
      const isCur = c.card_id === curId;
      const pad = 10 + depth * 14;   // §3.5：每层 +14px
      // [B4 主界面优化] 行内时间戳（nav-ctime）退役——活跃/分支活跃走 tooltip 三分列；
      // [E2 前端消费] branch_active_at 入 tooltip（「分支活跃」标识的到车口）
      const tip = escapeHtml(c.title)
        + (c.last_active_at ? `｜本卡活跃 ${fmtRel(c.last_active_at)}` : "｜无对话")
        + (c.branch_active_at ? `｜分支活跃 ${fmtRel(c.branch_active_at)}` : "");
      html += `<div class="nav-card${isCur ? " current" : ""}" data-cid="${escapeHtml(c.card_id)}" data-wid="${escapeHtml(p.work_id)}" style="padding-left:${pad}px;" title="${tip}">
        <span class="nav-twist${hasKids ? "" : " none"}"${hasKids ? ` data-twist="${escapeHtml(c.card_id)}" title="展开/收起"` : ""}>${hasKids ? (open ? "▾" : "▸") : ""}</span>
        <span class="dot ${hasKids ? "mid" : "leaf"}"></span>
        <span class="nav-ctitle">${escapeHtml(c.title)}</span>
        <button class="nav-more" data-more="${escapeHtml(c.card_id)}" data-mwid="${escapeHtml(p.work_id)}" title="建子卡 / 卡详情">⋯</button>
      </div>`;
      if (hasKids && open) {
        html += renderCardRows(childrenMap.get(c.card_id) || [], depth + 1, p, childrenMap, curId);
      }
    }
    return html;
  }

  function gotoCard(cid, wid, focusChild) {
    if (!cid) return;
    if (wid && wid !== WORK_ID) {
      // 跨项目：整页跳转，目标 work 页走既有 restoreFromHash 链恢复（V8）
      window.location.href = "/work/" + wid + "#" + cid;
      return;
    }
    open(cid).then(() => {
      if (focusChild) {
        inspector.classList.add("open");
        const el = document.getElementById("childTitle");
        if (el) el.focus();
      }
    });
  }

  let navMenuEl = null;
  function closeNavMenu() {
    if (navMenuEl) { navMenuEl.remove(); navMenuEl = null; }
  }
  function showNavMenu(cid, wid, anchor) {
    closeNavMenu();
    navMenuEl = document.createElement("div");
    navMenuEl.className = "nav-menu";
    navMenuEl.innerHTML = (wid !== WORK_ID)
      ? `<div class="nav-menu-item" data-act="detail">ⓘ 打开此卡</div>`
      : `<div class="nav-menu-item" data-act="child">＋ 建子卡</div>
         <div class="nav-menu-item" data-act="detail">ⓘ 卡详情</div>`;
    document.body.appendChild(navMenuEl);
    const r = anchor.getBoundingClientRect();
    navMenuEl.style.left = Math.max(8, Math.min(r.left, window.innerWidth - navMenuEl.offsetWidth - 8)) + "px";
    navMenuEl.style.top = Math.min(r.bottom + 4, window.innerHeight - navMenuEl.offsetHeight - 8) + "px";
    navMenuEl.querySelectorAll(".nav-menu-item").forEach(item => {
      item.onclick = (e) => {
        e.stopPropagation();
        const act = item.dataset.act;
        closeNavMenu();
        gotoCard(cid, wid, act === "child");
      };
    });
    setTimeout(() => document.addEventListener("click", closeNavMenu, { once: true }), 0);
  }

  function handleProjectAdd(wid) {
    if (wid === WORK_ID) {
      const btn = document.getElementById("newCardBtn");   // 顶栏既有建卡对话框
      if (btn) btn.click();
    } else {
      window.location.href = "/work/" + wid;               // 跨项目：去该项目画布再建
    }
  }

  // 导航事件委托：一次绑定，重渲免重挂。
  // [B1/B2/B5 主界面优化] 三行为分离：▸/▾（data-twist/data-ptwist，24px 热区）＝只管
  // 展开收起；行（.nav-phead/.nav-card）＝进该卡对话（组头＝根卡对话，展开态保持）；
  // 「打开 →」（data-act="open"）＝进项目画布。互不误触。
  navTree.addEventListener("click", (e) => {
    const twist = e.target.closest("[data-twist]");
    if (twist) {
      const cid = twist.dataset.twist;
      if (navOpenCards.has(cid)) navOpenCards.delete(cid); else navOpenCards.add(cid);
      renderNavFromCache();
      return;
    }
    const ptwist = e.target.closest("[data-ptwist]");
    if (ptwist) {
      const wid = ptwist.dataset.ptwist;
      if (navProjectsOpen.has(wid)) navProjectsOpen.delete(wid); else navProjectsOpen.add(wid);
      renderNavFromCache();
      return;
    }
    const more = e.target.closest("[data-more]");
    if (more) { showNavMenu(more.dataset.more, more.dataset.mwid, more); return; }
    const padd = e.target.closest("[data-add]");
    if (padd) { handleProjectAdd(padd.dataset.add); return; }
    const popen = e.target.closest('[data-act="open"]');
    if (popen) { window.location.href = "/work/" + popen.dataset.wid; return; }
    const phead = e.target.closest(".nav-phead");
    if (phead) {
      // [B1] 组头整行点击＝进入该组根卡对话；展开态保持（折叠中则先展开）
      if (!navProjectsOpen.has(phead.dataset.wid)) {
        navProjectsOpen.add(phead.dataset.wid);
        renderNavFromCache();
      }
      gotoCard(phead.dataset.root, phead.dataset.wid, false);
      return;
    }
    const row = e.target.closest(".nav-card");
    if (row) gotoCard(row.dataset.cid, row.dataset.wid, false);
  });

  // ==================== 讨论区 ====================
  // [R3] 会话区渲染：首屏/换卡全量构建；同卡重渲染走增量（只 append 尾部差额），
  // 避免「打开大 session 时把全部历史 DOM 清掉重灌」的卡顿。
  // 判据（写死）：
  //   ① 换了卡（cardId 不同）或服务端消息数 < 本地已渲染数（消息被删）→ 全量重建；
  //   ② 服务端消息数 > 本地已渲染数 → 只 append 尾部差额；
  //   ③ 相等 → 不碰 DOM（既有元素引用保留，C-4 判据依赖此点）。
  // send() 的既有增量路径不动。
  let chatRenderedCard = "";
  let chatRenderedCount = 0;

  async function renderChat() {
    try {
      const cardId = currentCard.meta.card_id;
      const msgs = await API.getSession(WORK_ID, cardId);
      // [R3 修正·执行方2] 本地已渲染条数以 DOM 实数为准（.msg:not(.thinking)，与 send()
      // 的 baseline 同口径）。send() 绕过 renderChat 直接上屏用户/助手气泡——纯变量
      // 记账与 DOM 脱节，重开同卡时会把已上屏的消息再追加一遍（重复气泡）。
      // 判据不变：换卡或服务端变短（消息被删）→ 全量重建；
      // 服务端变长 → 只 append 尾部差额；相等 → 不碰 DOM（既有元素引用保留）。
      const localCount = (chatRenderedCard === cardId)
        ? chatArea.querySelectorAll(".msg:not(.thinking)").length
        : 0;
      const needFull = (chatRenderedCard !== cardId) || (msgs.length < localCount);
      if (needFull) {
        chatArea.innerHTML = "";
        chatRenderedCard = cardId;
        chatRenderedCount = 0;
      }
      const baseCount = needFull ? 0 : localCount;
      if (msgs.length > baseCount) {
        const delta = msgs.slice(baseCount);
        for (const m of delta) appendMessage(m.role, m.content, m.model);
        console.info("[R3] 增量追加 " + delta.length + " 条（本地 " + baseCount + " → 服务端 " + msgs.length + "）");
        chatRenderedCount = msgs.length;
      }
    } catch (e) { /* toast 已处理 */ }
    scrollChatBottom();
  }

  function appendMessage(role, content, model) {
    const el = document.createElement("div");
    el.className = "msg " + (role === "user" ? "user" : "assistant");
    const badge = (role === "assistant" && model) ? `<div class="model-badge">${escapeHtml(model)}</div>` : "";
    el.innerHTML = badge + formatMsgContent(content || "");
    chatArea.appendChild(el);
    // [C3 主界面优化] 记账与显示一致：send()/恢复路径与 renderChat 同走本函数，
    // 每次真实上屏同步计数（与 renderChat 的 DOM 实数判据同口径，杜绝增量路径脱节）
    chatRenderedCount += 1;
    scrollChatBottom();
    return el;
  }

  function scrollChatBottom() {
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  // ==================== 发送消息 ====================

  // [phase8.2] 等待反馈气泡：轮询后端真实进度（三席分析→统合→工具→落卡），收到响应/失败后 stop() 清除
  // [实时反馈 2026-10-02] 后端暴露 /progress 阶段态；展示层专用，不影响模型输入
  const STAGE_TEXT = {
    started: "已提交，进入链路",
    viewpoints: "三席分析中",
    synthesize: "统合推理中",
    tools: "工具循环",
    cards: "解析建卡",
    done: "完成",
    error: "调用失败",
    idle: "等待开始",
  };
  function createWaitingBubble(prefix, cardIdAtSend) {
    const el = document.createElement("div");
    el.className = "msg thinking";
    let seconds = 0;
    let stageLine = "";
    const render = () => {
      el.innerHTML = `<span class="spinner"></span>${escapeHtml(prefix)} ${escapeHtml(stageLine || "…")} · 已等待 ${seconds} 秒`;
    };
    render();
    chatArea.appendChild(el);
    scrollChatBottom();
    const timer = setInterval(() => { seconds += 1; render(); }, 1000);
    const poll = setInterval(async () => {
      const cid = cardIdAtSend || (currentCard ? currentCard.meta.card_id : "");
      if (!cid) return;
      try {
        const st = await API.getProgress(WORK_ID, cid);
        if (!st || st.stage === "idle") return;
        const label = STAGE_TEXT[st.stage] || st.stage;
        stageLine = st.detail ? `${label}｜${st.detail}` : label;
        render();
      } catch (e) { /* 进度轮询失败静默：不影响主等待 */ }
    }, 2000);
    return {
      stop() {
        clearInterval(timer);
        clearInterval(poll);
        el.remove();
      },
    };
  }

  // [刷新恢复实时状态 2026-10-03] 页面加载时重新挂接在途调用的等待气泡。
  // 问题：等待气泡的 timer/poll/el 全在 createWaitingBubble 的闭包里，刷新即随 JS 上下文销毁；
  //       后端 progress 状态（模块级内存、key=(work_id,card_id)）与端点都还在，但加载路径从不查它，
  //       所以刷新后「正在跑」的实时状态消失（历史消息由 restoreFromHash→open→renderChat 恢复）。
  // 修法：加载时查一次后端 progress——活跃态则复用 createWaitingBubble 挂回气泡与轮询；
  //       done/error/idle 及任何异常一律静默降级为现有行为，绝不抛错打断页面。
  // 硬约束：纯前端改动，静态文件热生效，零重启。
  // 活跃态白名单显式列出——done/error 是终止态，若当活跃处理会让已完成的卡永远转圈。
  const ACTIVE_STAGES = ["started", "viewpoints", "synthesize", "tools", "cards"];
  async function attachRunningState(cardId) {
    if (!cardId) return false;
    try {
      const st = await API.getProgress(WORK_ID, cardId);
      if (!st || !ACTIVE_STAGES.includes(st.stage)) return false;
      // 同一张卡上已有等待气泡时不重复挂（避免 hashchange 等路径重复触发）
      if (chatArea.querySelector(".msg.thinking")) return false;
      // [信息流 Task5] 刷新重放：过程卡 from=0 重建（V5；SSE 连不上回退等待气泡）
      if (window.EventFlow) {
        window.EventFlow.start(WORK_ID, cardId, () => {
          createWaitingBubble("思考中…（刷新前已启动）", cardId);
        });
      } else {
        createWaitingBubble("思考中…（刷新前已启动）", cardId);
      }
      return true;
    } catch (e) {
      return false;   // 查询失败静默：不影响页面其余功能
    }
  }

  async function send() {
    if (!currentCard) return;
    const content = inputBox.value.trim();
    if (!content) return;

    // 立即上屏用户气泡
    appendMessage("user", content);
    lastSentContent = content;
    inputBox.value = "";
    inputBox.style.height = "auto";

    // [hotfix] 自愈基线：当前已上屏消息数（含刚上屏的用户气泡）＝服务端追加用户消息后的条数
    const baseline = chatArea.querySelectorAll(".msg:not(.thinking)").length;
    const cardId = currentCard.meta.card_id;
    let settled = false;

    // [信息流 Task5] SSE 过程卡先行（from=0）；连不上回退等待气泡（V6 降级保底）
    let waiting = null;
    if (window.EventFlow) {
      window.EventFlow.start(WORK_ID, cardId, () => {
        waiting = createWaitingBubble("思考中…", cardId);
      });
    } else {
      waiting = createWaitingBubble("思考中…", cardId);
    }

    // [hotfix] 后端完成但连接断了 → 从会话记录恢复回复，杜绝无限转圈
    async function tryRecover() {
      if (settled) return false;
      try {
        const msgs = await API.getSession(WORK_ID, cardId);
        if (msgs.length >= baseline + 1 && msgs[msgs.length - 1].role === "assistant") {
          settled = true;
          if (waiting) waiting.stop();
          if (window.EventFlow) setTimeout(() => window.EventFlow.stop(), 2500);
          await renderChat();
          const note = document.createElement("div");
          note.className = "sys-tools";
          note.textContent = "⚠ 响应连接中断，已从会话记录恢复回复（若有新卡片，返回画布后可见）";
          chatArea.appendChild(note);
          scrollChatBottom();
          return true;
        }
      } catch (e2) { /* 静默，下轮再试 */ }
      return false;
    }
    const pollTimer = setInterval(tryRecover, 15000);

    sendBtn.disabled = true;

    try {
      const resp = await API.postMessage(WORK_ID, cardId, content);
      if (settled) return;   // 已由轮询恢复，丢弃迟到响应
      settled = true;
      if (waiting) waiting.stop();
      if (window.EventFlow) setTimeout(() => window.EventFlow.stop(), 2500);   // 让 done 帧送达后收流
      appendMessage("assistant", resp.reply, resp.model);

      // created_cards 绿条
      if (resp.created_cards && resp.created_cards.length > 0) {
        const card = document.createElement("div");
        card.className = "sys-newcards";
        const items = resp.created_cards.map(c =>
          `<span class="nc-item" data-cid="${escapeHtml(c.card_id)}">${escapeHtml(c.title)}</span>`
        ).join("");
        card.innerHTML = `<b>✚ 已创建 ${resp.created_cards.length} 张卡片</b>：${items}`;
        chatArea.appendChild(card);
        card.querySelectorAll(".nc-item").forEach(it => {
          it.onclick = () => open(it.dataset.cid);
        });
        // [T17] 对话中生的卡即时进画布与导航（不等「← 画布」）
        if (window.ReloadCanvas) window.ReloadCanvas();
        renderNav();
      }

      // tool_events 灰条
      if (resp.tool_events && resp.tool_events.length > 0) {
        const tool = document.createElement("div");
        tool.className = "sys-tools";
        const items = resp.tool_events.map(t =>
          `<span class="te-item ${t.ok ? "" : "fail"}">🔧 ${escapeHtml(t.tool)}(${escapeHtml(t.card_id)}) ${t.ok ? "✓" : "✗"}</span>`
        ).join("");
        tool.innerHTML = items;
        chatArea.appendChild(tool);
      }

      // [phase9-F] 观点元信息灰条（simple 多模型；有 viewpoints_meta 才显示）
      if (resp.viewpoints_meta && resp.viewpoints_meta.length > 0) {
        const vp = document.createElement("div");
        vp.className = "sys-tools";
        const items = resp.viewpoints_meta.map(v => {
          const secs = ((v.latency_ms || 0) / 1000).toFixed(1) + "s";
          const status = v.ok ? `✓ ${secs}` : `✗ ${escapeHtml(v.error || "失败").slice(0, 24)}`;
          return `<span class="te-item ${v.ok ? "" : "fail"}">${escapeHtml(v.model)} ${status}</span>`;
        }).join(" ｜ ");
        vp.innerHTML = `观点：${items}`;
        chatArea.appendChild(vp);
      }

      scrollChatBottom();
    } catch (e) {
      if (settled) return;
      // [hotfix] 报错前先探一次会话：后端可能已完成，只是响应没送回来
      if (await tryRecover()) return;
      if (waiting) waiting.stop();
      // 失败：红色系统条 + 重发按钮
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `请求失败：${escapeHtml(e.message || "")} <button class="retry-btn">重发</button>`;
      chatArea.appendChild(err);
      err.querySelector(".retry-btn").onclick = () => {
        err.remove();
        resend(content);
      };
      scrollChatBottom();
    } finally {
      clearInterval(pollTimer);
      sendBtn.disabled = false;
    }
  }

  // 重发：不重复追加用户消息，直接重调接口
  // [T10-3-1/3-2] 重发前取当前卡会话：最后一条是用户消息且其后无回复条 → 复用该条
  // pair_id 重发（后端据此复用、不重复落盘）；前端未持有 pair_id → 按新消息落盘，
  // 由后端生成新标识。判据与后端二次判定逐字同一句（3-4），后端为最终仲裁（3-5）。
  // pair_id 经本文件内独立 fetch 携带——api.js 的 postMessage 签名固定 {content}，
  // 而 api.js 不在本任务书五文件清单内（红线 1）。
  async function resend(content) {
    const cardId = currentCard ? currentCard.meta.card_id : "";
    let pairId = null;
    try {
      const msgs = await API.getSession(WORK_ID, cardId);
      const last = msgs[msgs.length - 1];
      if (last && last.role === "user" && last.pair_id) {
        pairId = last.pair_id;
        console.info("[T10] 重发复用原 pair_id：" + pairId);
      } else {
        console.info("[T10] 重发未持有 pair_id，按新消息落盘（后端生成新标识）");
      }
    } catch (e) { /* 取会话失败按未持有 pair_id 处理，不阻断重发 */ }
    // [信息流 Task5] 重发同样走过程卡（SSE 先行、失败回退）
    let waiting = null;
    if (window.EventFlow) {
      window.EventFlow.start(WORK_ID, cardId, () => {
        waiting = createWaitingBubble("重发中…", cardId);
      });
    } else {
      waiting = createWaitingBubble("重发中…", cardId);
    }
    sendBtn.disabled = true;
    try {
      const httpResp = await fetch(`/api/works/${WORK_ID}/cards/${cardId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pairId ? { content, pair_id: pairId } : { content }),
      });
      if (!httpResp.ok) {
        const errText = await httpResp.json().catch(() => ({}));
        throw new Error(errText.error || ("HTTP " + httpResp.status));
      }
      const resp = await httpResp.json();
      if (waiting) waiting.stop();
      if (window.EventFlow) setTimeout(() => window.EventFlow.stop(), 2500);
      appendMessage("assistant", resp.reply, resp.model);
      scrollChatBottom();
    } catch (e) {
      if (waiting) waiting.stop();
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `重发失败：${escapeHtml(e.message || "")} <button class="retry-btn">再试</button>`;
      chatArea.appendChild(err);
      err.querySelector(".retry-btn").onclick = () => { err.remove(); resend(content); };
      scrollChatBottom();
    } finally {
      sendBtn.disabled = false;
    }
  }

  sendBtn.onclick = send;
  inputBox.addEventListener("keydown", (e) => {
    // [C5 主界面优化] IME 守卫：中文输入法组词/选字回车（isComposing 或 keyCode 229）不发送
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      send();
    }
  });
  // 输入框自适应高度
  inputBox.addEventListener("input", () => {
    inputBox.style.height = "auto";
    inputBox.style.height = Math.min(inputBox.scrollHeight, 200) + "px";
  });

  // ==================== [刷新恢复 2026-10-03] 加载时按 URL hash 重开上次的卡 ====================
  // 问题：页面上看的那张卡刷新即丢——回读历史的能力（open→renderChat→GET /session）三层齐备，
  // 但加载时没有任何代码去调 open()，所以刷新后对话区空白。
  // 修法：卡号存 URL hash（open() 内写入），加载时读出来重开。不引入 localStorage 等前端存储。
  async function restoreFromHash() {
    const hash = (location.hash || "").replace(/^#/, "");
    if (!hash) return false;
    try {
      await open(hash);
      // [刷新恢复实时状态 2026-10-03] 历史回读完成后，查一次后端进度：若这张卡有调用在跑，
      // 把等待气泡与 2 秒轮询挂回去——刷新不再丢失「正在思考」的实时状态。
      await attachRunningState(hash);
      return true;
    } catch (e) {
      // 卡片不存在（已是旧链接）等情形：open() 内部已 toast，这里回退到根卡，
      // 并把失效的 hash 清掉，避免每次刷新都撞同一个失败。
      history.replaceState(null, "", location.pathname + location.search);
      return false;
    }
  }

  // 浏览器后退/前进时同步切卡（hash 变化即重开）
  window.addEventListener("hashchange", () => {
    const hash = (location.hash || "").replace(/^#/, "");
    if (hash && (!currentCard || currentCard.meta.card_id !== hash)) {
      open(hash);
    }
  });

  // ==================== [T17] 静态绑定与初始化 ====================
  // 返回画布：纯显隐切换（R-1/R-043 铁则：不得走 reloadCanvas——「刷新画布数据」与
  // 「收起对话层」是两件事；建卡/删卡等数据变更已在变更源头 ReloadCanvas 刷新）
  document.getElementById("wsBackBtn").onclick = () => setAppView("canvas");
  // Inspector 开关：对话区头部按钮与右缘竖条同款
  const toggleInspector = () => inspector.classList.toggle("open");
  document.getElementById("inspToggleBtn").onclick = toggleInspector;
  document.getElementById("inspTab").onclick = toggleInspector;
  // 窄条 ☰：钉住（或收起）悬停浮层全树
  const navRailBtn = document.getElementById("navRailBtn");
  if (navRailBtn) {
    navRailBtn.onclick = () => {
      const nav = document.getElementById("appNav");
      if (nav) nav.classList.toggle("pinned");
    };
  }
  // 相对时间 60s 重算（条款 7：会话内，不落盘）
  setInterval(() => { renderNav(); }, 60000);
  // 初始导航渲染（画布态窄条悬停浮层也有树）
  renderNav();

  restoreFromHash();

})();
