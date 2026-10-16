/* workspace.js - [T17-F4/F5] 卡片工作区：左栏对话导航 + 中区讨论区 + 右栏 Inspector
   （原「左35%资料 + 右65%讨论区」结构退役；编辑能力整体迁入右栏 Inspector） */
/* 迭代四 F2③：IIFE → ES module；escapeHtml/API/EventFlow 走模块 import。
 * [阶段4 A4.1 2026-10-17] 本文件**不再 import canvas.js**：reloadCanvas 三处已删、
 * 关系下拉与停止回落改读导航快照 ⇒ 对话页模块图与画布侧彻底解耦，双向依赖清零。
 * Workspace.open 仍导出（canvas.js 已改为整页跳转，不再消费；留作对外薄壳）。 */
import { API } from "./esm/api.js?v=35";
import { escapeHtml } from "./esm/util.js?v=30";
import { EventFlow } from "./stream.js?v=36";
import { renderMarkdown } from "./esm/md.js?v=30";
import { store } from "./esm/store.js?v=30";
import { notify, postStop } from "./esm/api.js?v=35";
import { parseCardHeader, stripCardHeader } from "./esm/cardmsg.js?v=30";
// [S2/S3] 导航体系整体抽入 esm/nav_system.js（[阶段1 A1.2]）；本文件只留面包屑用的 ancestorCards。
import { ancestorCards } from "./esm/nav_breadcrumb.js?v=30";
import { createNavSystem } from "./esm/nav_system.js?v=31";
import { loadRulePanel, getRuleCardId } from "./esm/rule_panel.js?v=30";

const WORK_ID = window.__WORK_ID__;
  const ws = document.getElementById("workspace");
  const inspBody = document.getElementById("inspBody");
  const inspector = document.getElementById("inspector");
  const navTree = document.getElementById("navTree");
  const chatArea = document.getElementById("chatArea");
  const inputBox = document.getElementById("inputBox");
  // [顶栏整合 2026-10-13] #sendBtn（文字「发送」）与 #stopBtn（顶栏「■ 停止」）合并为
  // #runBtn 一个两态图标按钮（用户 2026-10-13 17:17 ③）。永远不禁用——运行中它要能点（＝停止）。
  const runBtn = document.getElementById("runBtn");
  let isRunning = false;   // 一轮在跑（防 Enter 二次发起；停止走运行按钮）

  // 运行按钮两态。running=true ⇒ 停止图标（点击＝停本轮）；false ⇒ 发送图标（点击＝发送）。
  function setRunState(running) {
    if (!runBtn) return;
    runBtn.dataset.state = running ? "running" : "idle";
    runBtn.className = "run " + (running ? "running" : "idle");
    runBtn.setAttribute("aria-label", running ? "停止" : "发送");
    runBtn.setAttribute("title", running ? "停止本轮生成" : "发送");
  }

  let currentCard = null;     // 完整 card 数据 {meta, body}
  let lastSentContent = "";   // 用于失败重发（不重复追加用户消息）
  // [I2 主界面优化] 结论区只读优先：编辑态与草稿挂模块级——renderInspector 重渲
  // （如加删关系触发）时恢复编辑态，不丢未保存内容
  let conclEditing = false;
  let conclDraft = "";

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

  // [顶栏整合 2026-10-13] 聊天态面包屑（吃 navStore.snapshot；替代已删的 #wsHeadTitle）。
  // 段序：首段＝项目名＝根卡（点它＝回画布，用户 17:17 ②「让【根卡】可点击…点击这个就是回画布」）
  //      → 中间段＝祖先卡（点它＝切到该卡对话）→ 末段＝当前卡（不可点）。
  // 根卡不再单列一段：实读 nav_breadcrumb.js:52 首段 cid 与 :53 根卡 cid 同为 work_card_id，
  // 且全 work 项目名与根卡名 10/10 同名——同时显示两遍是同一件东西。
  function renderChatCrumb(cardId) {
    const el = document.getElementById("chatCrumb");
    if (!el) return;
    el.innerHTML = "";
    const snap = navStore.snapshot;
    const p = (snap && snap.project) ? snap.project(WORK_ID) : null;
    const chain = (cardId && snap) ? ancestorCards(snap, WORK_ID, cardId) : [];
    if (!p || !chain.length) return;
    const mkSep = () => {
      const s = document.createElement("span");
      s.className = "csep";
      s.textContent = "›";
      return s;
    };
    const rootCid = p.work_card_id || (chain[0] && chain[0].card_id) || "";
    // 首段＝项目名（＝根卡）：点它回画布
    const root = document.createElement("button");
    root.className = "cseg root";
    root.textContent = (p.title || "").trim() || rootCid;
    root.title = "返回画布（项目名即根卡）";
    // [阶段3 A3.2 2026-10-17] 对话页无画布可切 ⇒ 整页跳转到画布页 URL。
    root.onclick = () => location.assign("/work/" + WORK_ID);
    el.appendChild(root);
    for (const c of chain) {
      if (c.card_id === rootCid) continue;   // 根卡＝首段，不重复
      el.appendChild(mkSep());
      if (c.card_id === cardId) {
        const cur = document.createElement("span");
        cur.className = "cseg cur";
        cur.setAttribute("aria-current", "page");
        cur.textContent = (c.title || "").trim() || c.card_id;
        el.appendChild(cur);
      } else {
        const b = document.createElement("button");
        b.className = "cseg mid";
        b.textContent = (c.title || "").trim() || c.card_id;
        b.title = "切到该卡对话";
        b.onclick = () => open(c.card_id);
        el.appendChild(b);
      }
    }
  }
  /* [F2③] 挂载改导出 */

  /* [切卡竞态 2026-10-14 · 用户令 ④] 底层修法：按「最终选择」收敛，不做临时补丁。
     问题根因：open() 全程无守卫，快速点两张卡＝两个 open 并发（各含 getCard →
     renderInspector → 事件流恢复等 await），谁后跑完谁赢——A 的迟到回复会写进
     已经清空、正在显示 B 的容器（用户判「上一张卡片的信息推进这张卡片里」）。
     修法：单调序号 _openSeq。每次 await 之后比对「我还是最后一次选择吗」，
     不是就立刻停手（一行 DOM 都不写）。这样 A 的异步链在自己这一层就断了，
     根本走不到写 DOM 那一步——不需要给每个写点打补丁。 */
  let _openSeq = 0;

  async function open(cardId) {
    const mySeq = ++_openSeq;
    const stale = () => mySeq !== _openSeq;
    // [画板四机制 T1/T4 2026-10-16] 点下去**同步翻页**：先把该卡的画板挂上
    // #chatArea，不等 getCard/inspector/timeline 任一 await——否则旧卡内容滞留，
    // 且快速连点时后到的 await 会把旧卡写进新卡（P4 串卡的界面侧表现）。
    try { EventFlow.activate(WORK_ID, cardId); } catch (e) { /* 零阻断 */ }
    try {
      const card = await API.getCard(WORK_ID, cardId);
      if (stale()) return;
      currentCard = card;
      await renderInspector();
      if (stale()) return;
      // [会话渲染单源化 2026-10-15 · C1] 刷新/换卡恢复走**唯一一条路**：
      // GET /timeline → 装配器 → 节点快照 → 渲染。原「先 renderChat 读 /session
      // 上屏消息、再 replayTrace 补过程行」两通道已合并成这一次调用。
      {
        await EventFlow.replayTrace(WORK_ID, cardId);
        if (stale()) return;
      }
      // [刷新恢复 2026-10-03] 卡号写进 URL hash：刷新后按 hash 自动重开这张卡。
      // 用 replaceState 不堆积浏览历史、不触发页面重载；URL 即状态，不引入前端存储。
      // [S2/R-09] 选中入 URL 走选中层复合键（#WORK_ID/cardId）。
      if (location.hash !== navStore.select.serialize(WORK_ID, cardId)) {
        navStore.select.set(WORK_ID, cardId);
      }
      // [阶段2 A2.2 2026-10-17] URL 即页面身份：选中卡同步进**路径**（取代 hash 承载）。
      // replaceState 不堆历史、不重载；旧 hash 链接由 bootRestore 一次性重定向到本形态。
      history.replaceState(null, "", "/work/" + WORK_ID + "/card/" + cardId);
      // [S3/R-25/V3-F5] 成功跳转（getCard 已取回）才记最近访问；失败/灰态不进此路径
      navFind.recordRecent(WORK_ID, cardId, card.meta.title);
      // [T17] 视图切换：进入对话态（nav 260px、workspace 可见）；.active 旧机制退役
      setAppView("chat");
      if (stale()) return;
      // 导航树重渲以应用当前卡高亮与祖先路径展开
      await renderNav();
      if (stale()) return;
      // [顶栏整合 2026-10-13] 卡标题改由顶栏聊天态面包屑承载（原 #wsHeadTitle 随 .ws-head 整条删除）。
      // 放在 await renderNav() 之后——面包屑吃 navStore.snapshot，快照到位才有祖先链。
      renderChatCrumb(cardId);
      // [phase8.4.1] 面板可见后再锁定滚底（visibility 方案下排版恒有效，此处保底）
      scrollChatBottom();
      // [phase8.6-C] 同理：textarea auto-grow 必须在面板可见后执行
      autoGrowAll();
    } catch (e) { /* toast 已处理 */ }
  }
  export const Workspace = { open };

  // [T17-F4] renderLeft() 拆二之一：renderInspector()——原左栏 DOM 原样迁入右栏
  // inspBody（仅去掉 wsBackBtn：返回按钮上移到对话区头部，见 work.html .ws-head）。
  // 职责区/结论区/否决/关系/历史/建子卡/删卡 逻辑一字未动（R-021）。
  async function renderInspector() {
    const m = currentCard.meta;
    const b = currentCard.body;

    const links = m.links || [];
    // [phase8.6-E] 每条关系尾部 ✕ 删除按钮（data-idx 定位）
    const linksHtml = links.length === 0
      ? `<div class="rej-empty">（无关系）</div>`
      : links.map((r, i) => `<div class="rel-item" data-target="${escapeHtml(r.target || "")}">
            <span class="rel-type">${escapeHtml(r.type || "")} →</span>
            <span>${escapeHtml(r.target || "")}</span>
            <button class="rel-del" data-idx="${i}" title="删除该关系">✕</button>
          </div>`).join("");

    // [phase8.6-E] 添加关系行：目标卡下拉（画布卡集，排除自身）+ 类型下拉
    // [阶段4 A4.1 2026-10-17] 目标卡数据源改读**导航快照**（state_layers 全树），
    // 不再吃画布状态（canvasState 只由画布页 canvas.js 写，对话页已无该模块）。
    const canvasCards = ((navStore.snapshot.get().projects || [])
      .flatMap(p => p.cards || []))
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

    // [卡片信息结构改造 S2-10] 键名随拆分器归一：rejections→rulings、charter→mission、
    // conclusion→status_block。存量卡不分叉——读侧已归一，新旧卡在此同一路径渲染。
    // [卡片编辑机制改造 2026-10-12 · 块3] 第三块按块集判据渲染：body 含 requirements
    // 键（rd_project 树）→ 需求文档；含 rulings 键（其他树）→ 定论。两者互斥（读侧过滤）。
    const thirdKey = ("requirements" in b) ? "requirements" : "rulings";
    const thirdLabel = thirdKey === "requirements" ? "需求文档 · requirements" : "定论 · rulings";
    const thirdEmpty = thirdKey === "requirements" ? "（暂无需求文档）" : "（暂无定论）";
    const thirdContent = (b[thirdKey] || "").trim();
    const thirdHtml = thirdContent
      ? thirdContent.split("\n").filter(s => s.trim()).map(line => `<div class="rej-item">${escapeHtml(line)}</div>`).join("")
      : `<div class="rej-empty">${thirdEmpty}</div>`;

    const history = b.history || [];
    const historyCount = history.length;

    inspBody.innerHTML = `
      <div class="ws-title" id="wsTitle">${escapeHtml(m.title)}</div>
      <div style="margin:8px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span class="type-badge" data-type="${escapeHtml(m.card_type)}">${escapeHtml(m.card_type)}卡</span>
        <span style="font-size:11px;color:var(--dimmer);margin-left:auto;">${escapeHtml(m.card_id)} · ${escapeHtml(m.source)}</span>
      </div>

      <div class="blocktitle">使命 · mission <span class="note">由上级卡对话创建 · 可编辑</span></div>
      <textarea class="ws-edit" id="charterEdit" placeholder="本卡作用/目标、授权来源">${escapeHtml(b.mission || "")}</textarea>
      <div class="edit-row">
        <button class="btn" id="saveCharter">保存使命</button>
        <span class="save-status" id="charterStatus"></span>
      </div>

      <div class="blocktitle">现状 · status_block</div>
      <div class="conclusion-display${(b.status_block || "").trim() ? "" : " empty"}" id="conclusionView">${(b.status_block || "").trim() ? escapeHtml(b.status_block) : "（暂无现状）"}</div>
      <div class="edit-row" id="conclViewRow">
        <button class="btn" id="editConclusion">编辑现状</button>
        <span class="save-status" id="conclusionStatus"></span>
      </div>
      <textarea class="ws-edit" id="conclusionEdit" placeholder="当前状况快照（做到哪、什么状态、卡在哪）" style="display:none">${escapeHtml(b.status_block || "")}</textarea>
      <div class="edit-row" id="conclEditRow" style="display:none">
        <button class="btn" id="saveConclusion">保存现状</button>
        <button class="btn ghost" id="cancelConclusion">取消</button>
      </div>

      <div class="blocktitle">${thirdLabel}</div>
      ${thirdHtml}

      <div class="blocktitle">关系</div>
      ${linksHtml}
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
        <!-- [底座收口 T5.c] 类型选择移除：card_type 传空串，由后端按本树
             project_class 映射（rd→sub_project / research→topic）——「线索」
             已退役，前端不再硬编码类型菜单（D-06 的下拉就此退役）。 -->
        <button class="btn" id="addChildBtn">建</button>
      </div>

      ${m.card_type !== "project" ? `
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
          renderChatCrumb(currentCard.meta.card_id);   // [顶栏整合] 标题改由顶栏面包屑承载
          notify("标题已保存", "success");   // [F3②] 标题保存反馈（原无任何反馈）
        } catch (e) { notify("标题保存失败", "error"); }
      };
      input.onblur = save;
      input.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = currentCard.meta.title; input.blur(); }
      };
    };

    // mission 保存（S2-10：patch 新键 mission；旧键 charter 仍被服务端白名单放行）
    document.getElementById("saveCharter").onclick = async () => {
      const v = document.getElementById("charterEdit").value;
      const status = document.getElementById("charterStatus");
      setSaveStatus(status, "busy", "保存中…");
      try {
        const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { mission: v }, "用户编辑使命区");
        currentCard = r;
        setSaveStatus(status, "ok", "已保存");
      } catch (e) { setSaveStatus(status, "fail", "保存失败"); }
    };

    // status_block 保存：[I2 主界面优化] 保存回只读态；再改须显式「编辑现状」
    document.getElementById("saveConclusion").onclick = async () => {
      const v = document.getElementById("conclusionEdit").value;
      const status = document.getElementById("conclusionStatus");
      setSaveStatus(status, "busy", "保存中…");
      try {
        const r = await API.patchCard(WORK_ID, currentCard.meta.card_id, { status_block: v }, "用户编辑现状区");
        currentCard = r;
        conclEditing = false;
        conclDraft = "";
        const view = document.getElementById("conclusionView");
        const nv = (r.body.status_block || "").trim();
        if (view) {
          view.textContent = nv ? r.body.status_block : "（暂无现状）";
          view.classList.toggle("empty", !nv);
        }
        applyConclusionState();
        setSaveStatus(status, "ok", "已保存");
      } catch (e) { setSaveStatus(status, "fail", "保存失败"); }
    };

    // [I2] 显式进编辑 / 取消弃改
    const editConclBtn = document.getElementById("editConclusion");
    if (editConclBtn) editConclBtn.onclick = () => {
      conclEditing = true;
      conclDraft = currentCard.body.status_block || "";
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
        const rels = (currentCard.meta.links || []).slice();
        if (isNaN(idx) || idx < 0 || idx >= rels.length) return;
        const removed = rels.splice(idx, 1)[0];
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id,
            { links: rels }, `用户删除关系：${removed.type || ""} → ${removed.target || ""}`);
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
        const rels = (currentCard.meta.links || []).slice();
        if (rels.some(r => r.target === target && r.type === type)) {
          API.toast("该关系已存在", "error"); return;
        }
        rels.push({ target, type });
        try {
          const r = await API.patchCard(WORK_ID, currentCard.meta.card_id,
            { links: rels }, `用户添加关系：${type} → ${target}`);
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
      // [底座收口 T5.c] card_type 传空串 → 后端按树映射
      try {
        await API.createCard(WORK_ID, {
          title, source: "用户直接创建",
          card_type: "", parent: currentCard.meta.card_id,
        });
        document.getElementById("childTitle").value = "";
        API.toast("已建子卡：" + title, "success");
        // [T17] 建卡即时刷新导航树（[阶段4 A4.1] 画布数据不再由本页刷新：画布页加载即拉新）
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
          // [阶段4 A4.1] 删的是当前卡 ⇒ 对话页已无内容可留，整页回画布页（原 setAppView("canvas")
          // 在纯对话页无画布可切）。
          location.assign("/work/" + WORK_ID);
          await renderNav();
        } catch (e) { /* toast 已处理（后端 error 文本透传） */ }
      };
    }
  }

  // ==================== [T17-F4] 左栏对话导航（读 /api/nav，服务端排序原样渲染） ====================
  // [阶段1 A1.2 2026-10-17] 导航系统（状态容器 + 渲染 + 交互 + 通知面板）整体抽入
  // esm/nav_system.js——逐字搬运、零行为变更；此处只做装配（注入 WORK_ID 与跳转回调）。
  const navSystem = createNavSystem({
    workId: WORK_ID,
    navTree,
    api: API,
    getCurrentCard: () => currentCard,
    open,
    inspector,
  });
  const { navStore, navFind, renderNav } = navSystem;

  export { setAppView };   // 原 window.SetAppView

  // ==================== 讨论区 ====================
  /* [会话渲染单源化 2026-10-15 · C1] /session 消息渲染通道**退役**：
     本文件不再调 API.getSession 取消息来渲染气泡。消息与过程行同属一条
     timeline，由 stream.js 的装配器按轮分组出快照、决定位置；本文件只提供
     「内容长什么样」的三个节点渲染器（注入 EventFlow，返回元素、不自行定位）。

     ★ 一处 C1 的必然后果（如实呈报，不自行补救）：timeline 的消息记录
       data 只有 {content}（session_facade.emit_message_event 唯一落点），
       **不带 model/behavior/end**。原 /session 通道提供的助手元信息行
       （model 徽标／behavior 环节块／end 结束原因）因此没有数据源，随本通道
       一并退役。三字段为空时 assistantMeta 不渲染（不报错、不留空壳）。
       要恢复只能动后端落盘形状（C3 本任务禁改），见施工回报偏差③。 */

  /** 注入 stream.js：user / assistant / cardmsg 三类消息节点的元素构造。 */
  function registerMessageRenderers() {
    EventFlow.setMessageRenderers({
      user: renderUserNode,
      assistant: renderAssistantNode,
      cardmsg: renderCardMsgNode,
    });
  }

  function renderUserNode(node) {
    const d = node.data || {};
    const el = document.createElement("div");
    // [T1] pending＝乐观上屏的本地节点（真实 user_message 到达即原地升级、类名消失）
    el.className = "msg user" + (node.pending ? " pending" : "");
    el.dataset.seq = node.seq;
    el.innerHTML = formatMsgContent(d.content == null ? "" : String(d.content));
    return el;
  }

  function renderAssistantNode(node) {
    const d = node.data || {};
    const el = document.createElement("div");
    el.className = "msg assistant";
    el.dataset.seq = node.seq;
    const badge = assistantMeta(d.model, d.behavior, d.end);
    el.innerHTML = badge +
      '<div class="md">' + renderMarkdown(d.content == null ? "" : String(d.content)) + "</div>";
    return el;
  }

  // [对话区会话流样式 2026-10-12] 统一内容列：卡片内所有消息/过程行都进同一条
  // .msgs 列（max-width min(70%, 48em)——用户 19:08/19:32 裁定），user 气泡靠右、
  // assistant 文档流靠左，同一右界：我的发言永不超出模型消息的边界（用户 21:39 裁定）。
  // 过程行仍是各自独立的 .step-row，不归拢、不包裹。
  // [画板四机制 T1 2026-10-16] 内容列不再是「全局唯一一份」：改取**该卡自己的**画板
  // 容器（stream.js 会话表持有）。cardId 决定写进哪张卡的画板——切走的卡系统行写
  // 自己那份，不会串进当前卡（原「chatArea 下唯一 .msgs」是 P4 串卡的根）。
  function ensureMsgs(cardId) {
    const cid = cardId || (currentCard && currentCard.meta.card_id) || "";
    return EventFlow.board(WORK_ID, cid);
  }

  /** [对话流全消息类型 2026-10-13] 助手消息元信息行（Ⅲ 对话块）：
      model 徽标 ＋ behavior 环节块徽标（Ⅳ）＋ end 结束原因（Ⅲ，END_VALUES：
      model_done / policy_stop / human_stop / error）。字段空则不渲染
      （timeline 通道下三字段恒空，见上方 C1 后果说明）。 */
  function assistantMeta(model, behavior, end) {
    const parts = [];
    if (model) parts.push(`<span class="model-badge">${escapeHtml(model)}</span>`);
    if (behavior) parts.push(`<span class="beh" data-beh="${escapeHtml(behavior)}">${escapeHtml(behavior)}</span>`);
    if (end) parts.push(`<span class="end${end === "error" ? " end-error" : ""}" data-end="${escapeHtml(end)}">end:${escapeHtml(end)}</span>`);
    return parts.length ? `<div class="meta">${parts.join("")}</div>` : "";
  }

  // [迭代四 F4⑥] 卡间消息渲染（Ⅰ 类 ×3）：.cmsg 结构＋头行剥离；解析失败显示
  // 「消息格式异常」标记＋原文全文照示（不静默丢弃，卡间册 §4.2 解析失败处置①）。
  // [对话流全消息类型 2026-10-13] 形态改 .cmsg（灰阶竖条＋类型标签＋方向行）。
  // 显示名与「须回应」镜像自 config/message_type_registry.json（后端 display_of 的同表；
  // 本路径无注册表端点，故前端持一份最小镜像，未知类型回落显示 code 本身＝fail-open）。
  const CARD_MSG_DISPLAY = { msg_ask: "询问", msg_answer: "应答", msg_notify: "通知" };
  const CARD_MSG_CLS = { msg_ask: "ask", msg_answer: "answer", msg_notify: "notify" };
  const CARD_MSG_MUST = { msg_ask: true };

  /** 卡间消息节点（注入 stream.js）：返回元素，**不自行 append**——
      位置由快照决定（C2）。role 形如 "card:card_007"（前 5 字符是来源前缀）。 */
  function renderCardMsgNode(node) {
    const d = node.data || {};
    const content = d.content == null ? "" : String(d.content);
    const hdr = parseCardHeader(content);
    const from = node.from || (node.data && node.data.from) || "";
    const to = (currentCard && currentCard.meta && currentCard.meta.card_id) || "本卡";
    const div = document.createElement("div");
    div.className = "msg cmsg" + (hdr ? (CARD_MSG_CLS[hdr.type] ? " " + CARD_MSG_CLS[hdr.type] : "") : " bad");
    if (hdr) div.dataset.cm = hdr.type;
    const bar = document.createElement("div");
    bar.className = "bar";
    const body = document.createElement("div");
    body.className = "body";
    const head = document.createElement("div");
    head.className = "head";
    const txt = document.createElement("div");
    txt.className = "txt";
    if (hdr) {
      const disp = CARD_MSG_DISPLAY[hdr.type] || hdr.type;
      head.innerHTML = `<span class="tag">${escapeHtml(hdr.type)} · ${escapeHtml(disp)}</span>` +
        (CARD_MSG_MUST[hdr.type] ? `<span class="must">须回应</span>` : "") +
        `<span class="dir">${escapeHtml(from)} → ${escapeHtml(to)}</span>`;
      txt.textContent = stripCardHeader(content);
    } else {
      head.innerHTML = `<span class="tag">消息格式异常</span><span class="dir">${escapeHtml(from)} → ${escapeHtml(to)}</span>`;
      txt.textContent = content;   // 原文全文照示
      try { console.info("[cardmsg] render bad header"); } catch (e) { /* 零阻断 */ }
    }
    body.appendChild(head);
    body.appendChild(txt);
    if (hdr && hdr.reply_to) {
      const rt = document.createElement("div");
      rt.className = "replyto";
      rt.textContent = "回应 " + hdr.reply_to;
      body.appendChild(rt);
    }
    div.appendChild(bar);
    div.appendChild(body);
    return div;
  }

  // [迭代四 F3②] 保存三态反馈（形态照 B-24 .edit-row .save-status 既有三态）
  function setSaveStatus(el, state, text) {
    if (!el) return;
    el.className = "save-status " + (state || "");
    el.textContent = text || "";
    if (state === "ok") setTimeout(() => { el.textContent = ""; el.className = "save-status"; }, 2500);
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
  // [迭代四 F3②] 等待气泡心跳：超 180 秒提示＋亮出停止按钮（提示不是中断——
  // /messages 保持不设超时，B-10 原样）。U-7 停止真生效（档案 end=human_stop）。
  function attachHeartbeat(bubble, cardId) {
    const timer = setTimeout(() => {
      if (!bubble.isConnected) return;
      const tip = document.createElement("div");
      tip.className = "sys-tools heartbeat-tip";
      tip.textContent = "⏱ 已等待超过 3 分钟——仍在生成。可继续等待或停止。";
      const stopBtn = document.createElement("button");
      stopBtn.className = "btn stop-inline";
      stopBtn.textContent = "停止";
      stopBtn.onclick = async () => {
        stopBtn.disabled = true;
        try {
          await postStop(WORK_ID, cardId);
          notify("已发送停止指令", "warn");
        } catch (e) { notify("停止指令失败", "error"); }
      };
      tip.appendChild(stopBtn);
      ensureMsgs(cardId).appendChild(tip);
      scrollChatBottom();
    }, 180000);
    const obs = new MutationObserver(() => {
      if (!bubble.isConnected) { clearTimeout(timer); obs.disconnect(); }
    });
    obs.observe(chatArea, { childList: true, subtree: false });
  }

  function createWaitingBubble(prefix, cardIdAtSend) {
    // [对话流渲染 T1] 两态互斥：等待气泡（.msg.thinking）只在事件流连不上时兜底，
    // 与生成中气泡（.msg.assistant.live）不并存——建等待气泡前先撤生成中气泡。
    chatArea.querySelectorAll(".msg.assistant.live").forEach(b => b.remove());
    const el = document.createElement("div");
    el.className = "msg thinking";
    let seconds = 0;
    let stageLine = "";
    const render = () => {
      el.innerHTML = `<span class="spinner"></span>${escapeHtml(prefix)} ${escapeHtml(stageLine || "…")} · 已等待 ${seconds} 秒`;
    };
    render();
    ensureMsgs(cardIdAtSend).appendChild(el);
    attachHeartbeat(el, cardIdAtSend);   // [F3②] 心跳＋停止按钮
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
  //       所以刷新后「正在跑」的实时状态消失（历史消息由 restoreFromHash→open→事件流恢复）。
  // 修法：加载时查一次后端 progress——活跃态则复用 createWaitingBubble 挂回气泡与轮询；
  //       done/error/idle 及任何异常一律静默降级为现有行为，绝不抛错打断页面。
  // 硬约束：纯前端改动，静态文件热生效，零重启。
  // 活跃态白名单显式列出——done/error 是终止态，若当活跃处理会让已完成的卡永远转圈。
  // [信息流重出 Task4] 活跃判定加固：超长请求不再被误判 stale。
// 此前只有阶段白名单——阶段不变的超长请求与「进程早已死掉但阶段没清」长得一模一样，
// 刷新后二者都可能被判成「还在跑」。后端现给出显式 active 位（progress.set_stage 写点声明），
// 有 active 就信它；无该字段（老响应）才退回阶段白名单，保证兼容。
// "streaming"：逐字流每帧心跳写入的阶段——正在吐字即仍在推进。
const ACTIVE_STAGES = ["started", "viewpoints", "synthesize", "tools", "cards", "streaming"];
  async function attachRunningState(cardId) {
    if (!cardId) return false;
    try {
      const st = await API.getProgress(WORK_ID, cardId);
      if (!st) return false;
      const alive = (typeof st.active === "boolean")
        ? st.active
        : ACTIVE_STAGES.includes(st.stage);
      if (!alive) return false;
      // [刷新后能暂停 2026-10-14 · 用户令 ②] 关键补丁：刷新型恢复了流、却没恢复**运行态**，
      // 于是卡还在跑、按钮却是「发送」，点下去发新消息而不是停——用户没法暂停。
      // 查到后端 active=true ⇒ 前端运行态同步置位：按钮转停止态、输入框锁住。
      isRunning = true;
      setRunState(true);
      if (inputBox) inputBox.disabled = true;
      // 同一张卡上已有等待气泡时不重复挂（避免 hashchange 等路径重复触发）
      if (chatArea.querySelector(".msg.thinking, .msg.assistant.live")) return false;
      // [信息流 Task5] 刷新重放：过程卡 from=0 重建（V5；SSE 连不上回退等待气泡）
      // [F2③] 原 `if (window.EventFlow)`——module 化后恒在，守卫保留结构
      if (EventFlow) {
        EventFlow.start(WORK_ID, cardId, () => {
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

  // [更新现状按钮改造 2026-10-16] `op` 缺省＝普通发送（逐字不变）；传 "update_status"
  // ⇒「更新现状」专用回合：本轮用户消息＝按钮名（可见、留痕，不另编话术），
  // 完整指令走后端轮首注入；收口时多扫一次 propose_status 开对比窗。
  async function send(op) {
    const statusOp = (op === "update_status");
    if (!currentCard) return;
    if (isRunning) return;   // [顶栏整合] 一轮在跑时不重入（运行中点按钮＝停止，见 doStop）
    const content = statusOp ? "更新现状" : inputBox.value.trim();
    if (!content) return;

    if (!statusOp) {   // 专用回合不占输入框：不清空、不记重发内容（见下方 resend 边界）
      lastSentContent = content;
      inputBox.value = "";
      inputBox.style.height = "auto";
    }

    const cardId = currentCard.meta.card_id;
    let settled = false;

    // [信息流 Task5] SSE 过程卡先行（from=0）；连不上回退等待气泡（V6 降级保底）
    let waiting = null;
    // [F2③] 原 `if (window.EventFlow)`——module 化后恒在，守卫保留结构
    if (EventFlow) {
      // [T1 乐观上屏 2026-10-16] 点发送瞬间先落用户气泡——**不等 POST、不等
      // timeline 回流**。此前用户气泡走 timeline 回流（POST 要跑到本轮结束才
      // 返回，过程事件却毫秒级到），于是 run_start/context/judgment_protocol
      // 抢在用户提问之前出现（P1）。现在本地轮先立，真实记录到达时原地升级。
      EventFlow.optimisticUser(WORK_ID, cardId, content);
      EventFlow.start(WORK_ID, cardId, () => {
        waiting = createWaitingBubble("思考中…", cardId);
      });
      // [T1 对账] timeline 回流保留：真实 user_message 到达时把 pending 节点
      // 按内容原地升级（补 seq），不产生双气泡。
      EventFlow.pullUserBubble(WORK_ID, cardId).catch(() => {});
    } else {
      const ub = document.createElement("div");
      ub.className = "msg user";
      ub.innerHTML = formatMsgContent(content);
      ensureMsgs(cardId).appendChild(ub);
      waiting = createWaitingBubble("思考中…", cardId);
    }

    // [hotfix] 后端完成但连接断了 → 从 timeline 恢复，杜绝无限转圈。
    // [T2 增量渲染 2026-10-16] 判据收紧：**先看后端是否仍在跑**——仍在跑就
    // 什么都不做。此前这里每 15s **无条件** resync（清列重建＋settled 冻结实时帧），
    // 正是「前面几条信息突然全部出现/历史搬位」（P2）与「从头到尾算一遍」（P3）
    // 的直接来源。现在只有「后端已不在跑（或事件流已断）」才回流结算。
    async function tryRecover() {
      if (settled) return false;
      try {
        const p = await API.getProgress(WORK_ID, cardId);
        const alive = p && ((typeof p.active === "boolean")
          ? p.active
          : ACTIVE_STAGES.includes(p.stage));
        if (alive && !EventFlow.streamDead(WORK_ID, cardId)) return false;   // 在跑：不重建
      } catch (e0) { return false; }   // 进度查不到＝不擅自重建（交给 POST 返回或断流兜底）
      try { console.info("[recover] tick"); } catch (e1) { /* 零阻断 */ }
      try {
        const r = await EventFlow.resync(WORK_ID, cardId);
        if (r && r.ok && r.done) {
          settled = true;
          if (waiting) waiting.stop();
          setTimeout(() => EventFlow.stop(WORK_ID, cardId), 2500);
          const note = document.createElement("div");
          note.className = "sys-tools";
          note.textContent = "⚠ 响应连接中断，已从事件流恢复回复（若有新卡片，返回画布后可见）";
          ensureMsgs(cardId).appendChild(note);
          scrollChatBottom();
          if (window.__maybeOpenRuleEditModal) await window.__maybeOpenRuleEditModal(cardId);
          if (statusOp && window.__maybeOpenStatusModal) window.__maybeOpenStatusModal(cardId);
          return true;
        }
      } catch (e2) { /* 静默，下轮再试 */ }
      return false;
    }
    let pollFails = 0;   // [F3②] 连续失败提示（继续轮询不放弃）
    const pollTimer = setInterval(async () => {
      try { await tryRecover(); pollFails = 0; }
      catch (e) {
        pollFails++;
        if (pollFails === 2) notify("进度查询连续失败，仍在重试…", "warn");
      }
    }, 15000);

    isRunning = true;
    setRunState(true);   // [顶栏整合] 运行按钮转停止态

    try {
      const resp = await API.postMessage(WORK_ID, cardId, content, op);
      if (settled) return;   // 已由轮询恢复，丢弃迟到响应
      settled = true;
      if (waiting) waiting.stop();
      setTimeout(() => EventFlow.stop(WORK_ID, cardId), 2500);   // 让 done 帧送达后收流
      // [C1 单源] 回复正文不再由 POST 响应直接上屏：此刻事件已全部落盘，
      // 走与刷新**同一条**渲染路（拉 timeline → 装配器 → 快照 → 渲染）。
      await EventFlow.resync(WORK_ID, cardId);
      // [更新现状按钮改造] 先等规则审批窗（同 DOM，防叠开），再按 op 开现状对比窗。
      if (window.__maybeOpenRuleEditModal) await window.__maybeOpenRuleEditModal(cardId);
      if (statusOp && window.__maybeOpenStatusModal) window.__maybeOpenStatusModal(cardId);

      // created_cards 绿条
      if (resp.created_cards && resp.created_cards.length > 0) {
        const card = document.createElement("div");
        card.className = "sys-newcards";
        const items = resp.created_cards.map(c =>
          `<span class="nc-item" data-cid="${escapeHtml(c.card_id)}">${escapeHtml(c.title)}</span>`
        ).join("");
        card.innerHTML = `<b>已创建 ${resp.created_cards.length} 张卡片</b>：${items}`;
        ensureMsgs(cardId).appendChild(card);
        card.querySelectorAll(".nc-item").forEach(it => {
          it.onclick = () => open(it.dataset.cid);
        });
        // [T17] 对话中生的卡即时进导航（[阶段4 A4.1] 画布侧由画布页加载即拉新）
        renderNav();
      }

      // tool_events 灰条
      if (resp.tool_events && resp.tool_events.length > 0) {
        const tool = document.createElement("div");
        tool.className = "sys-tools";
        const items = resp.tool_events.map(t =>
          `<span class="te-item ${t.ok ? "" : "fail"}">${escapeHtml(t.tool)}(${escapeHtml(t.card_id)}) ${t.ok ? "✓" : "✗"}</span>`
        ).join("");
        tool.innerHTML = items;
        ensureMsgs(cardId).appendChild(tool);
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
        ensureMsgs(cardId).appendChild(vp);
      }

      scrollChatBottom();
    } catch (e) {
      if (settled) return;
      // [hotfix] 报错前先探一次会话：后端可能已完成，只是响应没送回来
      if (await tryRecover()) return;
      if (waiting) waiting.stop();
      // [T1] 发送失败且未能恢复 ⇒ 撤下未被认领的本地气泡（不留幽灵消息）
      try { EventFlow.dropOptimisticUser(WORK_ID, cardId); } catch (e0) { /* 零阻断 */ }
      // 失败：红色系统条 + 重发按钮
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `请求失败：${escapeHtml(e.message || "")} <button class="retry-btn">重发</button>`;
      ensureMsgs(cardId).appendChild(err);
      err.querySelector(".retry-btn").onclick = () => {
        err.remove();
        resend(content);
      };
      scrollChatBottom();
    } finally {
      clearInterval(pollTimer);
      isRunning = false;
      setRunState(false);   // [顶栏整合] 运行按钮回发送态
      if (inputBox) inputBox.disabled = false;   // [②] 运行态解除时同步解锁输入框
    }
  }

  // 重发：不重复追加用户消息，直接重调接口
  // [T10-3-1/3-2] 重发前取当前卡会话：最后一条是用户消息且其后无回复条 → 复用该条
  // pair_id 重发（后端据此复用、不重复落盘）；前端未持有 pair_id → 按新消息落盘，
  // 由后端生成新标识。判据与后端二次判定逐字同一句（3-4），后端为最终仲裁（3-5）。
  // pair_id 经本文件内独立 fetch 携带——api.js 的 postMessage 签名固定 {content}，
  // 而 api.js 不在本任务书五文件清单内（红线 1）。
  async function resend(content) {
    if (isRunning) return;   // [顶栏整合] 同 send 的重入守卫
    const cardId = currentCard ? currentCard.meta.card_id : "";
    let pairId = null;
    try {
      // [C1 单源] pair_id 也从 timeline 取：落盘记录的 run_id **就是** pair_id
      // （session_facade.emit_message_event 的 run_id＝blk.pair_id），同一个字段，
      // 不必再读 /session。取最近一条 user_message 的 run_id 即上一轮回合对。
      const tl = await API.getTimeline(WORK_ID, cardId);
      const items = (tl && tl.items) || [];
      for (let i = items.length - 1; i >= 0; i--) {
        if (items[i] && items[i].type === "user_message" && String(items[i].run_id || "")) {
          pairId = String(items[i].run_id);
          break;
        }
      }
      if (pairId) {
        console.info("[T10] 重发复用原 pair_id：" + pairId);
      } else {
        console.info("[T10] 重发未持有 pair_id，按新消息落盘（后端生成新标识）");
      }
    } catch (e) { /* 取事件流失败按未持有 pair_id 处理，不阻断重发 */ }
    // [信息流 Task5] 重发同样走过程卡（SSE 先行、失败回退）
    let waiting = null;
    // [F2③] 原 `if (window.EventFlow)`——module 化后恒在，守卫保留结构
    if (EventFlow) {
      EventFlow.start(WORK_ID, cardId, () => {
        waiting = createWaitingBubble("重发中…", cardId);
      });
    } else {
      waiting = createWaitingBubble("重发中…", cardId);
    }
    isRunning = true;
    setRunState(true);
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
      await httpResp.json();
      if (waiting) waiting.stop();
      setTimeout(() => EventFlow.stop(WORK_ID, cardId), 2500);
      // [C1 单源] 同 send()：回复正文走 timeline 回流，不从响应体直接上屏。
      await EventFlow.resync(WORK_ID, cardId);
      if (window.__maybeOpenRuleEditModal) window.__maybeOpenRuleEditModal(cardId);
      scrollChatBottom();
    } catch (e) {
      if (waiting) waiting.stop();
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `重发失败：${escapeHtml(e.message || "")} <button class="retry-btn">再试</button>`;
      ensureMsgs(cardId).appendChild(err);
      err.querySelector(".retry-btn").onclick = () => { err.remove(); resend(content); };
      scrollChatBottom();
    } finally {
      isRunning = false;
      setRunState(false);
    }
  }

  // [顶栏整合 2026-10-13] 运行按钮两态（用户 17:17 ③：「把发送按钮，处理成【发送前】、
  // 【运行中】（运行中就是停止按钮）」）。同一元素同一位置，点一次的含义由当前态决定。
  async function doStop() {
    // [阶段4 A4.1] 回落数据源改读导航选中（不再依赖画布状态 window.GetCanvasState）。
    const cardId = currentCard ? currentCard.meta.card_id
      : ((navStore.select.get() || {}).cid || "");
    if (!cardId) { notify("没有正在查看的卡", "warn"); return; }
    try {
      await postStop(WORK_ID, cardId);
      notify("已发送停止指令", "warn");
    } catch (e) { notify("停止指令失败", "error"); }
  }

  if (runBtn) runBtn.onclick = () => { if (isRunning) doStop(); else send(); };
  inputBox.addEventListener("keydown", (e) => {
    // [C5 主界面优化] IME 守卫：中文输入法组词/选字回车（isComposing 或 keyCode 229）不发送
    if (e.isComposing || e.keyCode === 229) return;
    // [2026-10-13 用户令] 快捷键 Ctrl+Enter → Enter（Shift+Enter 保留换行）。
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  // 输入框自适应高度
  inputBox.addEventListener("input", () => {
    inputBox.style.height = "auto";
    inputBox.style.height = Math.min(inputBox.scrollHeight, 200) + "px";
  });

  // ==================== [阶段2 A2.2 2026-10-17] 加载时按 URL 决定打开哪张卡 ====================
  // URL 即页面身份：/work/{wid} ＝ 画布页（停在画布态）；/work/{wid}/card/{cid} ＝ 该卡对话页。
  // 旧 hash 链接（#wid/cid）兼容：一次性重定向到新地址（hash 退役第一步）。
  async function bootRestore() {
    const pathCid = (window.__CARD_ID__ || "").trim();
    const sel = navStore.select.get();   // 解析自旧 hash（无 hash 则 null）
    if (sel) {
      // 旧 hash 链接 → 新地址（wid 不同也一并收敛，原跨 work 分支并入此处）
      location.replace("/work/" + sel.wid + "/card/" + sel.cid);
      return true;
    }
    if (!pathCid) return false;          // 无卡号：停在画布态（现行为）
    try {
      await open(pathCid);
      // [刷新恢复实时状态 2026-10-03] 历史回读完成后查一次后端进度：有调用在跑就挂回等待气泡。
      await attachRunningState(pathCid);
      return true;
    } catch (e) {
      // 卡片不存在（旧链接）等：清掉路径里的卡号、回到画布页，避免每次刷新都撞同一个失败。
      history.replaceState(null, "", "/work/" + WORK_ID);
      return false;
    }
  }

  // 浏览器后退/前进时同步切卡（hash 变化即重开）
  window.addEventListener("hashchange", () => {
    const sel = navStore.select.get();
    if (!sel) return;
    if (sel.wid !== WORK_ID) {
      window.location.href = "/work/" + sel.wid + "#" + sel.cid;
      return;
    }
    if (!currentCard || currentCard.meta.card_id !== sel.cid) {
      open(sel.cid);
    }
  });

  // [会话渲染单源化 2026-10-15] 消息类节点的渲染器在此一次性注入 stream.js：
  // 内容格式化归本文件，放置归装配器快照（C2）。
  registerMessageRenderers();

  // ==================== [卡片编辑机制改造 2026-10-12 · 块3] 起草确认弹窗状态机 ====================
  // 点按钮 → draft(kind) → 草稿态（只读展示，暂存 base_hash）→ 三选一：
  //   〔确认〕→ commit(draft, base_hash)：成功 toast+关弹窗+刷新；hash 冲突红字回草稿态
  //   〔反馈〕→ 展开反馈框 → 重新 draft(kind, feedback, prev_draft)
  //   〔关闭〕→ 直接关，不发任何请求（草稿丢弃、卡 hash 不变）
  // 关键约束：关闭与反馈都不落卡；只有确认走 commit；draft 期间弹窗「加载中」灰字。
  {
    const modal = document.getElementById("draftModal");
    const overlay = document.getElementById("draftOverlay");
    const titleEl = document.getElementById("draftModalTitle");
    const draftArea = document.getElementById("draftArea");
    const feedbackBox = document.getElementById("draftFeedbackBox");
    const feedbackInput = document.getElementById("draftFeedbackInput");
    const errorEl = document.getElementById("draftModalError");
    const confirmBtn = document.getElementById("draftConfirmBtn");
    const feedbackBtn = document.getElementById("draftFeedbackBtn");
    const closeBtn = document.getElementById("draftCloseBtn");
    const modalX = document.getElementById("draftModalX");
    const draftBtn = document.getElementById("draftBtn");
    const statusDraftBtn = document.getElementById("statusDraftBtn");

    // 弹窗会话态（内存，不落盘）
    let dm = null;  // { kind, baseHash, draft, feedbackRounds, busy }

    function isRdCard() {
      return currentCard && currentCard.body && ("requirements" in currentCard.body);
    }

    // 按钮文案随块集判据切换（研发卡「需求确定」/ 其他卡「定论确认」）
    function updateDraftButtons() {
      if (!draftBtn) return;
      draftBtn.textContent = isRdCard() ? "需求确定" : "定论确认";
    }

    function setModalError(msg) {
      if (!errorEl) return;
      if (msg) { errorEl.textContent = msg; errorEl.style.display = "block"; }
      else { errorEl.textContent = ""; errorEl.style.display = "none"; }
    }

    function openDraft(kind) {
      if (!currentCard) { notify("请先打开一张卡", "warn"); return; }
      dm = { kind, baseHash: "", draft: "", feedbackRounds: 0, busy: false };
      titleEl.textContent = kind === "requirements"
        ? (isRdCard() ? "需求文档起草" : "定论起草")
        : "现状更新起草";
      draftArea.textContent = "起草中…";
      draftArea.classList.add("loading");
      feedbackBox.classList.remove("open");
      feedbackInput.value = "";
      setModalError("");
      confirmBtn.disabled = true;
      feedbackBtn.disabled = true;
      modal.classList.add("open");
      _requestDraft(kind, null, null);
    }

    async function _requestDraft(kind, feedback, prevDraft) {
      dm.busy = true;
      confirmBtn.disabled = true;
      feedbackBtn.disabled = true;
      draftArea.textContent = "起草中…";
      draftArea.classList.add("loading");
      setModalError("");
      try {
        const body = { kind };
        if (feedback) body.feedback = feedback;
        if (prevDraft != null) body.prev_draft = prevDraft;
        const r = await API.postDraft(WORK_ID, currentCard.meta.card_id, body);
        dm.draft = r.draft || "";
        dm.baseHash = r.base_hash || "";
        draftArea.textContent = dm.draft || "（空草稿）";
        draftArea.classList.remove("loading");
        confirmBtn.disabled = false;
        feedbackBtn.disabled = false;
      } catch (e) {
        // toast 已由 api.js 处理；弹窗内补错误态
        draftArea.textContent = "起草失败";
        draftArea.classList.remove("loading");
        setModalError("起草失败，请重试");
        confirmBtn.disabled = true;
        feedbackBtn.disabled = true;
      } finally {
        dm.busy = false;
      }
    }

    async function submitCommit() {
      if (!dm || dm.busy) return;
      dm.busy = true;
      confirmBtn.disabled = true;
      feedbackBtn.disabled = true;
      try {
        const r = await API.postCommit(WORK_ID, currentCard.meta.card_id, {
          kind: dm.kind,
          draft: dm.draft,
          base_hash: dm.baseHash,
          feedback_rounds: dm.feedbackRounds,
        });
        API.toast("已写入", "success");
        closeDraft();
        // 刷新 inspector（读侧过滤后的新 body）
        const fresh = await API.getCard(WORK_ID, currentCard.meta.card_id);
        currentCard = fresh;
        await renderInspector();
      } catch (e) {
        // hash 冲突等：弹窗内红字提示，回到草稿态（不关弹窗）
        const msg = (e && e.message) ? e.message : "写入失败";
        if (String(msg).indexOf("内容已被改") >= 0 || String(msg).indexOf("重新起草") >= 0) {
          setModalError("内容已被改，请重新起草");
        } else {
          setModalError(msg);
        }
        confirmBtn.disabled = false;
        feedbackBtn.disabled = false;
      } finally {
        dm.busy = false;
      }
    }

    async function submitFeedback() {
      if (!dm || dm.busy) return;
      const fb = (feedbackInput.value || "").trim();
      if (!fb) {
        feedbackBox.classList.add("open");
        feedbackInput.focus();
        return;
      }
      dm.feedbackRounds += 1;
      feedbackBox.classList.remove("open");
      feedbackInput.value = "";
      await _requestDraft(dm.kind, fb, dm.draft);
    }

    function closeDraft() {
      dm = null;
      modal.classList.remove("open");
      draftArea.textContent = "";
      feedbackBox.classList.remove("open");
      feedbackInput.value = "";
      setModalError("");
    }

    // 事件绑定
    if (draftBtn) draftBtn.onclick = () => openDraft("requirements");
    // [更新现状按钮改造 2026-10-16] 「更新现状」不再走 /draft 起草通道，改发起一次
    // 带 op 的专用回合（主流程模型 + 卡内全部工具 + 专用工具 propose_status）。
    // op 字符串与后端 card_tools.OP_UPDATE_STATUS 同值（唯一定义处是后端）。
    if (statusDraftBtn) statusDraftBtn.onclick = () => send("update_status");
    if (confirmBtn) confirmBtn.onclick = submitCommit;
    if (feedbackBtn) feedbackBtn.onclick = submitFeedback;
    if (closeBtn) closeBtn.onclick = closeDraft;
    if (modalX) modalX.onclick = closeDraft;
    if (overlay) overlay.onclick = closeDraft;  // 点遮罩＝关闭（无请求）

    // 按钮文案跟随开卡/重渲更新（renderInspector 后调用）
    const _origRenderInspector = renderInspector;
    renderInspector = async function () {
      await _origRenderInspector();
      updateDraftButtons();
    };

    // 初始化一次（无卡时保持缺省文案）
    updateDraftButtons();
  }

  // ==================== [更新现状按钮改造 2026-10-16] 现状对比窗状态机 ====================
  // 专用通道的收口：op 回合结束后扫本卡 timeline，找 propose_status 的**成功**
  // tool_result → 开单面板行级 diff（原现状 vs 提议；提议**可改**）：
  //   〔同意〕→ POST …/commit（复用既有「写入唯一入口」：base_hash 乐观锁 →
  //            整块替换 status_block → 影子 ## 迭代历史 记一行）
  //   〔关闭〕→ 关窗、不发任何请求、不落盘（提议留在对话里，可再点按钮重来）
  // 绑卡：窗口自带 cardId，提交用它 —— 防「期间切卡写到别的卡上」（同规则审批手法）。
  // DOM：独立 #statusModal（不挤 #draftModal，避免三套状态机争同一批 onclick）。
  {
    const modal = document.getElementById("statusModal");
    const overlay = document.getElementById("statusOverlay");
    const titleEl = document.getElementById("statusModalTitle");
    const diffArea = document.getElementById("statusDiffArea");
    const editArea = document.getElementById("statusEditArea");
    const errorEl = document.getElementById("statusModalError");
    const confirmBtn = document.getElementById("statusConfirmBtn");
    const closeBtn = document.getElementById("statusCloseBtn");
    const modalX = document.getElementById("statusModalX");
    const draftModalEl = document.getElementById("draftModal");

    let dmStatus = null;            // { cardId, baseHash, busy }
    const statusSeen = new Set();   // 已弹过的 propose_status tool_result seq（去重）

    function setErr(msg) {
      if (!errorEl) return;
      if (msg) { errorEl.textContent = msg; errorEl.style.display = "block"; }
      else { errorEl.textContent = ""; errorEl.style.display = "none"; }
    }

    // ==================== 现状 diff（纯函数；node 侧剪段求值，见 tests/status_diff.test.mjs） ====================
    // 段内**只放纯函数**（无 DOM、无闭包外引用）——测试从本文件剪出本段在 node 里求值。
    // 段分隔线是结构性标记：改段时必动，动了 tests/status_diff.test.mjs 会当场报错、不静默。
    /** 行级 diff（LCS 回溯）：文本量小，O(n·m) 足够。
        返回 [{k, text}]，k ∈ same/add/del —— 单面板逐行对照的数据源。 */
    function lineDiff(oldText, newText) {
      // 空串＝**零行**（不是一行空串）：否则「原现状为空」会多出一条虚假的 `- ` 行。
      const toLines = (t) => {
        const s = String(t == null ? "" : t);
        return s === "" ? [] : s.split("\n");
      };
      const a = toLines(oldText);
      const b = toLines(newText);
      const n = a.length, m = b.length;
      const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
      for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
          dp[i][j] = (a[i] === b[j]) ? dp[i + 1][j + 1] + 1
                                     : Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
      }
      const out = [];
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (a[i] === b[j]) { out.push({ k: "same", text: a[i] }); i++; j++; }
        else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ k: "del", text: a[i] }); i++; }
        else { out.push({ k: "add", text: b[j] }); j++; }
      }
      while (i < n) { out.push({ k: "del", text: a[i] }); i++; }
      while (j < m) { out.push({ k: "add", text: b[j] }); j++; }
      return out;
    }
    // ==================== 现状 diff 段终 ====================

    function renderDiff(oldText, newText) {
      const rows = lineDiff(oldText, newText);
      if (!rows.length) {
        diffArea.innerHTML = `<div class="sd-line sd-empty">（两侧都为空）</div>`;
        return;
      }
      const head = (!String(oldText || "").trim())
        ? `<div class="sd-line sd-empty">（原现状为空，以下为新增内容）</div>` : "";
      const body = rows.map(r => {
        const mark = r.k === "add" ? "+ " : (r.k === "del" ? "- " : "  ");
        return `<div class="sd-line sd-${r.k}">${escapeHtml(mark + r.text)}</div>`;
      }).join("");
      diffArea.innerHTML = head + body;
    }

    function openStatusModal(cardId, payload) {
      dmStatus = { cardId: cardId || "", baseHash: payload.base_hash || "", busy: false };
      titleEl.textContent = "更新现状 · 原/新对比";
      renderDiff(payload.old || "", payload.proposed || "");
      editArea.value = payload.proposed || "";
      setErr("");
      confirmBtn.disabled = false;
      modal.classList.add("open");
    }

    async function submitStatusCommit() {
      if (!dmStatus || dmStatus.busy) return;
      dmStatus.busy = true;
      confirmBtn.disabled = true;
      const cid = dmStatus.cardId;
      try {
        await API.postCommit(WORK_ID, cid, {
          kind: "status",
          draft: editArea.value,          // ← 用户可改后的文本（对比窗可编辑）
          base_hash: dmStatus.baseHash,
          feedback_rounds: 0,
        });
        API.toast("现状已写入", "success");
        closeStatusModal();
        // 右栏现状刷新（只在还停在本卡时刷；期间切卡则不打扰别的卡）
        if (currentCard && currentCard.meta.card_id === cid) {
          currentCard = await API.getCard(WORK_ID, cid);
          await renderInspector();
        }
      } catch (e) {
        const msg = String((e && e.message) || "写入失败");
        setErr((msg.indexOf("内容已被改") >= 0 || msg.indexOf("重新起草") >= 0)
          ? "现状已被改，请关窗后重新点「更新现状」"
          : ("写入失败：" + msg));
        confirmBtn.disabled = false;
      } finally {
        if (dmStatus) dmStatus.busy = false;
      }
    }

    function closeStatusModal() {
      dmStatus = null;
      modal.classList.remove("open");
      diffArea.innerHTML = "";
      editArea.value = "";
      setErr("");
    }

    if (confirmBtn) confirmBtn.onclick = () => { if (dmStatus) submitStatusCommit(); };
    if (closeBtn) closeBtn.onclick = closeStatusModal;
    if (modalX) modalX.onclick = closeStatusModal;
    if (overlay) overlay.onclick = closeStatusModal;   // 点遮罩＝关闭（无请求、不落盘）

    /** 回合收口扫 timeline：找 propose_status 的**成功** tool_result → 开对比窗。
        **只由带 op 的回合**（send / tryRecover 的 statusOp 分支）调用——普通回合不扫，
        故模型在普通对话里误调该工具也不会弹窗。 */
    async function maybeOpenStatusModal(cardId) {
      if (dmStatus || modal.classList.contains("open")) return;         // 本窗已在
      if (draftModalEl && draftModalEl.classList.contains("open")) return;  // 起草/规则窗在，不叠开
      try {
        const tl = await API.getTimeline(WORK_ID, cardId);
        const items = (tl && tl.items) || [];
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (!it || it.type !== "tool_result") continue;
          const d = it.data || {};
          if (d.tool !== "propose_status" || !d.ok) continue;
          const seq = it.seq || 0;
          if (statusSeen.has(seq)) continue;
          statusSeen.add(seq);
          const res = (d.result && d.result.data) || {};
          if (res.proposed !== undefined) openStatusModal(cardId, res);
          return;   // 每次收口最多开一个
        }
      } catch (e) { /* 零阻断 */ }
    }
    window.__maybeOpenStatusModal = maybeOpenStatusModal;
  }

  // ==================== [T17] 静态绑定与初始化 ====================
  // [顶栏整合 2026-10-13] 返回画布的入口由两处收敛为两处新归属：
  //   ① 面包屑首段（#chatCrumb 项目名，＝根卡）——renderChatCrumb 内绑 setAppView("canvas")
  //   ② 无独立「← 画布」按钮（用户 17:17 ②「不需要画布按钮…点击这个就是回画布」）
  // 原 #wsBackBtn/#inspToggleBtn 随 .ws-head 整条删除（用户 17:17 ④ 去「ⓘ 卡详情」顶部按钮），
  // 卡详情开关只留右缘竖条 #inspTab。
  const toggleInspector = () => inspector.classList.toggle("open");
  document.getElementById("inspTab").onclick = toggleInspector;
  // [画布入口 2026-10-16 用户令] 顶部栏最左 icon → 返回本项目画布（根卡/子卡聊天态常显）
  const canvasEntryBtn = document.getElementById("canvasEntryBtn");
  // [阶段3 A3.2 2026-10-17] 对话页无画布可切 ⇒ 回画布＝整页跳转到画布页 URL。
  if (canvasEntryBtn) canvasEntryBtn.onclick = () => location.assign("/work/" + WORK_ID);
  // 窄条 ☰：钉住（或收起）悬停浮层全树
  const navRailBtn = document.getElementById("navRailBtn");
  if (navRailBtn) {
    navRailBtn.onclick = () => {
      const nav = document.getElementById("appNav");
      if (nav) nav.classList.toggle("pinned");
    };
  }
  // [左栏新建项目 2026-10-16 用户令] 搜索框右侧 ＋：新建项目（复用首页既有链路，
  // 端点 POST /api/works 零改动）。语义照抄 esm/home.js createProject：creating 锁防连点、
  // 失败 toast 由 api.js 的 call() 弹（此处不重复处理）。
  const navNewBtn = document.getElementById("navNewProject");
  if (navNewBtn) {
    let creatingProject = false;
    navNewBtn.onclick = async () => {
      if (creatingProject) return;
      creatingProject = true;
      navNewBtn.disabled = true;
      try {
        const w = await API.createWork("新项目");
        location.href = "/work/" + w.work_id + "#";
      } catch (e) {
        /* 失败原因已由 api.js 的 call() 弹 toast */
        creatingProject = false;
        navNewBtn.disabled = false;
      }
    };
  }
  navSystem.startNav();   // [阶段1 A1.2] 导航探测周期与首渲（原 nav 初始化段，已入 nav_system）

  // ═══ [顶栏整合 2026-10-13] 原顶栏 #stopBtn（「■ 停止」）已并入输入条 #runBtn 两态（doStop 定义见上）═══
  // 用户 17:17 ③：「把发送按钮，处理成【发送前】、【运行中】（运行中就是停止按钮）」——停止不再是独立按钮。


  // ═══ [规则卡 2026-10-15 · 2026-10-16 二次归位] 规则卡＝吸附屏幕右侧的常驻卡片 ═══
  // （用户明令：不是画布节点、不是可展开边栏——就是一张固定吸附右侧、始终完整
  // 显示的卡）。正文按 ## {区名} 分六区（project/sub_project/tool/coder/topic/
  // rule＝card_type code），存 mission 列。直改即存：textarea blur → PATCH
  // /rule/sections/{section}（带 base_hash，不符→409 提示刷新）。
  // 审批：仅根卡可调 draft_rule_edit 提议（工具自身不落盘）；回合后扫 timeline 的
  // tool_result（data.tool=="draft_rule_edit"）→ 开弹窗，「同意」→ POST /rule/commit 落盘。
  {
    const ruleSeen = new Set();   // 已弹过审批的 draft_rule_edit 去重（tool_result 的 seq）

    // 常驻卡：页面加载即渲染（不分画布态/聊天态，始终吸附右侧）
    loadRulePanel();

    // [2026-10-16 归位] rule 卡＝普通卡：导航/画布点它走正常 gotoCard（对话+inspector
    // 分区编辑），原「点规则卡开右栏面板」的拦截已随面板形态删除。

    // ─── 根卡提议审批弹窗：扫 timeline 的 draft_rule_edit 成功 tool_result ───
    // 复用既有 draftModal 的 DOM，走独立 dmRule 状态机（与块3起草弹窗互不干扰）。
    const dmRuleModal = document.getElementById("draftModal");
    const dmRuleOverlay = document.getElementById("draftOverlay");
    const dmRuleTitle = document.getElementById("draftModalTitle");
    const dmRuleArea = document.getElementById("draftArea");
    const dmRuleFbBox = document.getElementById("draftFeedbackBox");
    const dmRuleFbInput = document.getElementById("draftFeedbackInput");
    const dmRuleErr = document.getElementById("draftModalError");
    const dmRuleConfirm = document.getElementById("draftConfirmBtn");
    const dmRuleFeedback = document.getElementById("draftFeedbackBtn");
    const dmRuleClose = document.getElementById("draftCloseBtn");
    const dmRuleX = document.getElementById("draftModalX");
    let dmRule = null;   // { section, old, draft, baseHash, ruleCardId, busy }

    function setRuleModalError(msg) {
      if (!dmRuleErr) return;
      if (msg) { dmRuleErr.textContent = msg; dmRuleErr.style.display = "block"; }
      else { dmRuleErr.textContent = ""; dmRuleErr.style.display = "none"; }
    }

    function openRuleEditModal(payload) {
      dmRule = {
        section: payload.section || "",
        old: payload.old || "",
        draft: payload.draft || "",
        baseHash: payload.base_hash || "",
        ruleCardId: payload.rule_card_id || getRuleCardId(),
        busy: false,
      };
      dmRuleTitle.textContent = `规则修改审批 · ${dmRule.section}`;
      dmRuleArea.innerHTML =
        `<div class="rule-old-block"><div class="rule-sec-name">当前内容</div><pre>${escapeHtml(dmRule.old)}</pre></div>` +
        `<div class="rule-new-block"><div class="rule-sec-name">提议修改为</div><pre>${escapeHtml(dmRule.draft)}</pre></div>`;
      dmRuleArea.classList.remove("loading");
      dmRuleFbBox.classList.remove("open");
      dmRuleFbInput.value = "";
      setRuleModalError("");
      dmRuleConfirm.disabled = false;
      dmRuleFeedback.disabled = true;   // 审批无「反馈重起草」，只留 同意/拒绝
      dmRuleFeedback.style.display = "none";
      dmRuleModal.classList.add("open");
    }

    async function submitRuleCommit() {
      if (!dmRule || dmRule.busy) return;
      dmRule.busy = true;
      dmRuleConfirm.disabled = true;
      try {
        await API.call("POST", `/api/works/${WORK_ID}/rule/commit`, {
          section: dmRule.section,
          content: dmRule.draft,
          base_hash: dmRule.baseHash,
        });
        API.toast("规则已写入", "success");
        closeRuleModal();
        loadRulePanel();   // [2026-10-16] 吸附卡常驻，落盘后即时重渲分区
      } catch (e) {
        const msg = String(e.message || "");
        setRuleModalError(msg.indexOf("hash 不符") >= 0 ? "内容已被改，请重新提议" : ("写入失败：" + msg));
        dmRuleConfirm.disabled = false;
      } finally {
        dmRule.busy = false;
      }
    }

    function closeRuleModal() {
      dmRule = null;
      dmRuleModal.classList.remove("open");
      dmRuleArea.textContent = "";
      dmRuleFbBox.classList.remove("open");
      setRuleModalError("");
      dmRuleFeedback.style.display = "";
    }

    if (dmRuleConfirm) dmRuleConfirm.onclick = () => { if (dmRule) submitRuleCommit(); };
    if (dmRuleClose) dmRuleClose.onclick = closeRuleModal;
    if (dmRuleX) dmRuleX.onclick = closeRuleModal;
    if (dmRuleOverlay) dmRuleOverlay.onclick = closeRuleModal;

    // 回合收口（send/resend/tryRecover 的 resync 之后）扫 timeline 开审批弹窗
    async function maybeOpenRuleEditModal(cardId) {
      if (dmRule) return;   // 已有审批弹窗在，不叠开
      try {
        const tl = await API.getTimeline(WORK_ID, cardId);
        const items = (tl && tl.items) || [];
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (!it || it.type !== "tool_result") continue;
          const d = it.data || {};
          if (d.tool !== "draft_rule_edit" || !d.ok) continue;
          const seq = it.seq || 0;
          if (ruleSeen.has(seq)) continue;
          ruleSeen.add(seq);
          const res = (d.result && d.result.data) || {};
          if (res.section !== undefined) openRuleEditModal(res);
          return;   // 每次收口最多开一个
        }
      } catch (e) { /* 零阻断 */ }
    }
    // 暴露给 send/resend/tryRecover 在 resync 后调用
    window.__maybeOpenRuleEditModal = maybeOpenRuleEditModal;
  }

  bootRestore();
