/* workspace.js - [T17-F4/F5] 卡片工作区：左栏对话导航 + 中区讨论区 + 右栏 Inspector
   （原「左35%资料 + 右65%讨论区」结构退役；编辑能力整体迁入右栏 Inspector） */
/* 迭代四 F2③：IIFE → ES module；escapeHtml/API/EventFlow/ReloadCanvas 走模块 import。
 * 循环依赖（canvas.js import 本文件的 Workspace/RenderNav）由 ESM 活绑定＋函数声明
 * 提升保证：本文件顶层不调用任何 canvas 导出。 */
import { API } from "./esm/api.js?v=11";
import { escapeHtml } from "./esm/util.js?v=11";
import { EventFlow } from "./stream.js?v=12";
import { reloadCanvas } from "./canvas.js?v=11";
import { store } from "./esm/store.js?v=11";
import { notify, postStop } from "./esm/api.js?v=11";
import { parseCardHeader, stripCardHeader } from "./esm/cardmsg.js?v=11";
// [S2] 导航渲染体系：状态三层容器 + 行模型 + 渲染换底 + 虚拟滚动 + 异步加载
import {
  createNavStateStore, storageGet, storageSet,
} from "./esm/state_layers.js?v=11";
import { createNavAsync } from "./esm/nav_async.js?v=11";
import { createNavRenderer } from "./esm/nav_render.js?v=11";
// [S3] 交互与呈现族：找通道（R-24/25）、面包屑+吸顶+祖先链（R-27/18/26/28）、快照存在性（R-21）
import { createNavFind } from "./esm/nav_find.js?v=11";
import { createNavBreadcrumb, ancestorCards } from "./esm/nav_breadcrumb.js?v=11";
import { cardInSnapshot } from "./esm/nav_badge.js?v=11";

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
  /* [F2③] 挂载改导出 */

  async function open(cardId) {
    try {
      const card = await API.getCard(WORK_ID, cardId);
      currentCard = card;
      await renderInspector();
      await renderChat();
      // [对话流渲染] 把这张卡**上一条调用**的中间环节痕迹补回对话流（刷新/换卡都生效）。
      // 事件只活在服务端内存 journal（不落盘、不进 session/prior/模型输入）：
      //   同进程且该卡此后没再发过请求 ⇒ 重放得出；进程重启或又发过消息 ⇒ 自然没有了。
      // 只重放环节行、不重放正文（正文已由 renderChat 从会话渲染，免得出现重复气泡）。
      {
        EventFlow.replayTrace(WORK_ID, cardId);
      }
      // [刷新恢复 2026-10-03] 卡号写进 URL hash：刷新后按 hash 自动重开这张卡。
      // 用 replaceState 不堆积浏览历史、不触发页面重载；URL 即状态，不引入前端存储。
      // [S2/R-09] 选中入 URL 走选中层复合键（#WORK_ID/cardId）。
      if (location.hash !== navStore.select.serialize(WORK_ID, cardId)) {
        navStore.select.set(WORK_ID, cardId);
      }
      // [S3/R-25/V3-F5] 成功跳转（getCard 已取回）才记最近访问；失败/灰态不进此路径
      navFind.recordRecent(WORK_ID, cardId, card.meta.title);
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
  export const Workspace = { open };

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
    const canvasCards = ((store.state.canvasState && store.state.canvasState.cards) || [])
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
          document.getElementById("wsHeadTitle").textContent = currentCard.meta.title || "";
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
      // [底座收口 T5.c] card_type 传空串 → 后端按树映射
      try {
        await API.createCard(WORK_ID, {
          title, source: "用户直接创建",
          card_type: "", parent: currentCard.meta.card_id,
        });
        document.getElementById("childTitle").value = "";
        API.toast("已建子卡：" + title, "success");
        // [T17] 建卡即时刷新画布数据与导航树（视图不动；画布隐藏态仍可安全重渲）
        reloadCanvas();
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
          reloadCanvas();
          setAppView("canvas");
          await renderNav();
        } catch (e) { /* toast 已处理（后端 error 文本透传） */ }
      };
    }
  }

  // ==================== [T17-F4] 左栏对话导航（读 /api/nav，服务端排序原样渲染） ====================
  // 层级口径（R-016/R-044，写死）：项目根卡＝第 0 层恒定；与画布 state.viewRoot 的
  // 相对深度是两套口径，互不同步、不做互相写入。
  // [S2] 原四个导航裸状态变量退役：
  // 状态收进三层容器（temp/prefs/select/snapshot，state_layers.js），渲染走
  // nav_render.js（mount/unmount/patch 三原语 + 虚拟滚动），展开态持久化进偏好层。
  const navStore = createNavStateStore({ workId: WORK_ID });
  const navAsync = createNavAsync(navStore, API);
  const navRenderer = createNavRenderer({
    tree: navTree,
    store: navStore,
    api: API,
    workId: WORK_ID,
    async: navAsync,
    getCardId: () => (currentCard ? currentCard.meta.card_id : ""),
  });

  // [S3/T2] 找通道：搜索独立层（结果层叠加在 appNav 上，navTree 本体零触碰）
  const navFind = createNavFind({
    host: document.getElementById("appNav"),
    input: document.getElementById("navSearchInput"),
    store: navStore,
    workId: WORK_ID,
    onJump: (wid, cid) => gotoCard(cid, wid, false),   // 复用 R-20 叶子跳转路径
  });
  // [S3/T3] 定位族：面包屑+吸顶（同一 UI 两态）；vl.headerH 是 S2 预留的在流头部高度消费口
  const navBreadcrumb = createNavBreadcrumb({
    box: document.getElementById("navCrumbBox"),
    el: document.getElementById("navCrumb"),
    store: navStore,
    workId: WORK_ID,
    getTree: () => navTree,
    onJump: (wid, cid) => gotoCard(cid, wid, false),
    vl: navRenderer.vl,
  });

  async function renderNav() {
    await navRenderer.refresh();
    // [S3/R-29] 展开态还原：S2 refresh 只灌当前 work 的段——其余「prefs 记着开着」的
    // 项目与分支 reload/切回后段缺失（▾ 却无行）。从全量快照零网络补灌后再重切。
    if (restoreOpenSegments()) navRenderer.render();
    // [S3/R-27] 换卡/数据刷新后面包屑随当前选中重算（浅树自隐，深树重渲段文本）
    navBreadcrumb.render();
  }

  // [S3/R-29] 开着的项目根段＋已展开分支段，凡缺失一律从快照灌（孤儿契约与
  // nav_render.snapshotRootsWithOrphans 同口径；零网络，全量 cards 已在快照里）。
  function restoreOpenSegments() {
    const snap = navStore.snapshot;
    let dirty = false;
    for (const p of (snap.get().projects || [])) {
      const wid = p.work_id;
      if (!navStore.prefs.getProjectOpen(wid)) continue;
      if (!snap.getChildren(wid, "")) {
        const all = p.cards || [];
        const ids = new Set(all.map(c => c.card_id));
        const root = p.work_card_id || "";
        snap.setChildren(wid, "", all.filter(c => {
          const pa = c.parent || "";
          return pa === "" || pa === root || (pa && !ids.has(pa));
        }));
        dirty = true;
      }
      for (const e of navStore.prefs.listExpanded(wid)) {
        if (!snap.getChildren(wid, e.cid)) {
          snap.setChildren(wid, e.cid, snap.cardsOf(wid, e.cid));
          dirty = true;
        }
      }
    }
    return dirty;
  }
  export const RenderNav = renderNav;   // canvas.js 建卡后同步刷新导航用
  export { setAppView };   // 原 window.SetAppView

  // [S2] ensureCurrentPath（祖先路径自动展开）职责移入 nav_render.js applyDefaultFold
  // （source="system" 记入偏好层）；renderNavFromCache / renderProject / renderCardRows
  // 的 innerHTML 整树拼装退役——渲染走 nav_render.js 行级 mount/unmount/patch。

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

  // ==================== [S3/T1] 交互层（R-20/21/28/29） ====================

  // [S3/R-28] 收起非路径分支的批量内核：非 keepOpenWid 的项目行全收；已展开分支凡不在
  // pathIds 一律折叠。逐条走 prefs 常规 API（S3 只批量调用，不改 API 面）——路径条目的
  // source 标记原样保留（S4「手动展开绝不收」判据数据不破坏）。
  function collapseNonPathProjects(keepOpenWid, pathIds) {
    for (const p of (navStore.snapshot.get().projects || [])) {
      const wid = p.work_id;
      if (wid !== keepOpenWid) navStore.prefs.setProjectOpen(wid, false);
      for (const e of navStore.prefs.listExpanded(wid)) {
        if (pathIds.has(e.cid)) continue;
        navStore.prefs.setExpanded(wid, e.cid, null);        // 折叠（与手动折叠同一条常规路径）
        navAsync.fold({ wid, card: { card_id: e.cid } });    // 折叠即丢（R-14 既有语义）
      }
    }
  }

  // [S3/R-28] 一键收起全部：只保留当前卡祖先链（v4 硬禁第一项：当前卡祖先链绝不收）。
  // 折叠记忆自动存（prefs 天然持久化）。
  function collapseAllKeepPath() {
    const sel = navStore.select.get();
    const curWid = (sel && sel.wid) || WORK_ID;
    const curCid = (sel && sel.cid) || (currentCard ? currentCard.meta.card_id : "");
    const pathIds = new Set(ancestorCards(navStore.snapshot, curWid, curCid).map(c => c.card_id));
    if (curCid && !navStore.prefs.getProjectOpen(curWid)) {
      navStore.prefs.setProjectOpen(curWid, true);   // 当前卡所在项目行必须保持展开
    }
    collapseNonPathProjects(curCid ? curWid : null, pathIds);
    navFind.close();
    navRenderer.render();
    navBreadcrumb.render();
  }

  // [S3/R-26] 定位到当前卡：收起非路径分支＋祖先链强制展开（段数据从快照灌，零网络）＋
  // 滚到当前卡可视区（scrollTop = headerH + 行前缀和 top − 吸顶盖高）。
  function locateCurrentCard() {
    const sel = navStore.select.get();
    const wid = (sel && sel.wid) || WORK_ID;
    const cid = (sel && sel.cid) || (currentCard ? currentCard.meta.card_id : "");
    if (!cid) return;
    const snap = navStore.snapshot;
    const chain = ancestorCards(snap, wid, cid);
    if (!chain.length) return;
    const pathIds = new Set(chain.map(c => c.card_id));
    if (!navStore.prefs.getProjectOpen(wid)) navStore.prefs.setProjectOpen(wid, true);
    for (const a of chain) {
      // manual（用户手动展开）不动——applyDefaultFold 同判据；无条目或 system → 系统展开
      if (navStore.prefs.getExpanded(wid, a.card_id) !== "manual") {
        navStore.prefs.setExpanded(wid, a.card_id, "system");
      }
      snap.setChildren(wid, a.card_id, snap.cardsOf(wid, a.card_id));   // 祖先段（零网络）
    }
    if (!snap.getChildren(wid, "")) {
      // 根层段缺失才灌（孤儿契约与 nav_render.snapshotRootsWithOrphans 同口径）
      const p = snap.project(wid);
      const all = (p && p.cards) || [];
      const ids = new Set(all.map(c => c.card_id));
      const root = (p && p.work_card_id) || "";
      snap.setChildren(wid, "", all.filter(c => {
        const pa = c.parent || "";
        return pa === "" || pa === root || (pa && !ids.has(pa));
      }));
    }
    collapseNonPathProjects(wid, pathIds);
    navFind.close();
    navRenderer.render();
    navBreadcrumb.render();
    // 滚动定位：行前缀和 top（expandRows 导出）＋在流头部高度（vl.headerH，S2 预留口）−吸顶盖高
    const rows = navRenderer.expandRows();
    const idx = rows.findIndex(r => r.kind === "card" && r.wid === wid && r.card.card_id === cid);
    if (idx >= 0) {
      let off = 0;
      for (let i = 0; i < idx; i++) off += rows[i].rowHeight();
      const header = navBreadcrumb.headerHeight();
      const cover = navBreadcrumb.stickyCoverPx();
      navTree.scrollTop = Math.max(0, header + off - cover);
      navRenderer.render();   // 显式 reslice（容器 scroll 被动监听亦触发，双保险）
    }
  }

  // [S3/R-21] 灰态兜底：点击的卡不在快照（多标签删除/快照缝隙）→ 置灰不跳；
  // 该卡恰为当前选中 → 清选中（hash 摘除，restoreFromHash 清失效 hash 同款）＋面包屑回父卡。
  function handleMissingCard(rowEl, wid, cid) {
    rowEl.classList.add("deleted");
    const sel = navStore.select.get();
    if (sel && sel.wid === wid && sel.cid === cid) {
      history.replaceState(null, "", location.pathname + location.search);
      const parentCid = (currentCard && currentCard.meta.card_id === cid)
        ? (currentCard.meta.parent || "") : "";
      if (parentCid) {
        navBreadcrumb.render({ overrideWid: wid, overrideCid: parentCid, bypassDepth: true });
      } else {
        navBreadcrumb.render();
      }
    }
  }

  // [S3/R-20②③] 卡行点击判型：先查后动（R-21）→ 有子卡＝展开/折叠（URL 不变）；
  // 叶子＝跳转（open() → select.set 复合键 ＋页面切卡）。
  function handleCardRowClick(rowEl) {
    const cid = rowEl.dataset.cid;
    const wid = rowEl.dataset.wid || WORK_ID;
    // [S3/R-21/V3-A4] 先查后动：snapshot.project(wid) 在场且快照含该卡（cardInSnapshot，
    // nav_badge.js 纯函数，内部走 snapshot.project + cards 全量比对）才动作
    if (!cardInSnapshot(navStore.snapshot, wid, cid)) {
      handleMissingCard(rowEl, wid, cid);
      return;
    }
    rowEl.classList.remove("deleted");   // 快照已恢复的自愈（灰态权威投影在 patchCardContent）
    const rowObj = rowEl.__navRow;
    const hasKids = rowObj
      ? ((rowObj.card.child_count || 0) > 0)
      : (parseInt(rowEl.dataset.childCount || "0", 10) > 0);
    if (hasKids) {
      if (rowObj) navRenderer.toggleCard(rowObj);
      return;
    }
    gotoCard(cid, wid, false);
  }

  // 导航事件委托：一次绑定，免重挂。[S2] 读写走三层容器，展开/折叠/拉取走新模块。
  // [S3/R-20] 单击语义按行类型分叉（v3 L50-53；旧 B1「组头整行＝进组根卡对话」退役）：
  //   ▸/▾（data-twist/data-ptwist，24px 热区）＝只管展开收起（S2 热区不动）；
  //   项目行（.nav-phead）单击＝展开/折叠，URL 不变；
  //   有子卡卡行单击＝展开/折叠，URL 不变；
  //   叶子卡单击＝跳转（复合键写 URL＋页面切卡）；已删卡＝灰态不跳（R-21）；
  //   「打开 →」（data-act="open"）＝进项目画布（不动）。互不误触。
  navTree.addEventListener("click", (e) => {
    const twist = e.target.closest("[data-twist]");
    if (twist) {
      const rowEl = twist.closest(".nav-card");
      if (rowEl && rowEl.__navRow) navRenderer.toggleCard(rowEl.__navRow);
      return;
    }
    const ptwist = e.target.closest("[data-ptwist]");
    if (ptwist) {
      navRenderer.toggleProject(ptwist.dataset.ptwist);
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
      // [S3/R-20①] 项目行单击＝展开/折叠（URL 不变）
      navRenderer.toggleProject(phead.dataset.wid);
      return;
    }
    const row = e.target.closest(".nav-card");
    if (row) handleCardRowClick(row);
  });

  // [S3/R-18] 树滚动 → 面包屑吸顶联动（同一 DOM 两态；scrollTop>2 挂 .sticky）
  navTree.addEventListener("scroll", () => { navBreadcrumb.onTreeScroll(); }, { passive: true });

  // [S3/R-28/R-26] 左栏顶部两钮（work.html #navTop 容器内）
  {
    const collapseBtn = document.getElementById("navCollapseAll");
    if (collapseBtn) collapseBtn.onclick = collapseAllKeepPath;
    const locateBtn = document.getElementById("navLocate");
    if (locateBtn) locateBtn.onclick = locateCurrentCard;
  }

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
      // [R3 修正·执行方2] 本地已渲染条数以 DOM 实数为准（.msg:not(.thinking):not(.live)，与 send()
      // 的 baseline 同口径）。send() 绕过 renderChat 直接上屏用户/助手气泡——纯变量
      // 记账与 DOM 脱节，重开同卡时会把已上屏的消息再追加一遍（重复气泡）。
      // 判据不变：换卡或服务端变短（消息被删）→ 全量重建；
      // 服务端变长 → 只 append 尾部差额；相等 → 不碰 DOM（既有元素引用保留）。
      const localCount = (chatRenderedCard === cardId)
        ? chatArea.querySelectorAll(".msg:not(.thinking):not(.live)").length
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
    // [迭代四 F4⑥] 卡间消息分流：role 以 card: 开头＝其他卡发来的消息——
    // 必须先于下方 live 接管分支（否则吞掉正在生成的助手气泡）。
    if (role && String(role).startsWith("card:")) return appendCardMessage(role, content);
    const badge = (role === "assistant" && model) ? `<div class="model-badge">${escapeHtml(model)}</div>` : "";
    // [对话流渲染] 权威助手消息上屏时**就地接管**那条正在生成的正文条目
    // （`.msg.assistant.live`）——.live 不计入基数（见 localCount/baseline），
    // 接管后它就成为该条助手消息本体；过程行 `.step-row` 各自独立留在对话流里，不动。
    if (role !== "user") {
      const live = chatArea.querySelector(".msg.assistant.live");
      if (live) {
        live.className = "msg assistant";
        live.innerHTML = badge + formatMsgContent(content || "");
        chatRenderedCount += 1;
        scrollChatBottom();
        return live;
      }
    }
    const el = document.createElement("div");
    el.className = "msg " + (role === "user" ? "user" : "assistant");
    el.innerHTML = badge + formatMsgContent(content || "");
    chatArea.appendChild(el);
    // [C3 主界面优化] 记账与显示一致：send()/恢复路径与 renderChat 同走本函数，
    // 每次真实上屏同步计数（与 renderChat 的 DOM 实数判据同口径，杜绝增量路径脱节）
    chatRenderedCount += 1;
    scrollChatBottom();
    return el;
  }

  // [迭代四 F4⑥] 卡间消息渲染：.msg.card 结构＋头行剥离；解析失败显示
  // 「消息格式异常」标记＋原文全文照示（不静默丢弃，卡间册 §4.2 解析失败处置①）。
  function appendCardMessage(role, content) {
    const hdr = parseCardHeader(content);
    const div = document.createElement("div");
    div.className = "msg card";
    const meta = document.createElement("div");
    meta.className = "cmsg-meta";
    const body = document.createElement("div");
    body.className = "cmsg-body";
    if (hdr) {
      meta.innerHTML = `<span class="cmsg-type">${escapeHtml(hdr.type)}</span>` +
        ` <span class="step-dim">来自 ${escapeHtml(role.slice(5))}</span>` +
        (hdr.reply_to ? ` <span class="step-dim">回应 ${escapeHtml(hdr.reply_to)}</span>` : "");
      body.textContent = stripCardHeader(content);
    } else {
      meta.innerHTML = `<span class="cmsg-type bad">消息格式异常</span>`;
      body.textContent = content;   // 原文全文照示
      try { console.info("[cardmsg] render bad header"); } catch (e) { /* 零阻断 */ }
    }
    div.appendChild(meta);
    div.appendChild(body);
    chatArea.appendChild(div);
    scrollChatBottom();
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
      chatArea.appendChild(tip);
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
    chatArea.appendChild(el);
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
  //       所以刷新后「正在跑」的实时状态消失（历史消息由 restoreFromHash→open→renderChat 恢复）。
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

  async function send() {
    if (!currentCard) return;
    const content = inputBox.value.trim();
    if (!content) return;

    // 立即上屏用户气泡
    appendMessage("user", content);
    lastSentContent = content;
    inputBox.value = "";
    inputBox.style.height = "auto";

    // [hotfix] 自愈基线：当前已上屏消息数（含刚上屏的用户气泡）＝服务端追加用户消息后的条数。
    // [对话流渲染 T5] 排除 .thinking 与 .assistant.live——生成中/等待气泡不计入基数。
    const baseline = chatArea.querySelectorAll(".msg:not(.thinking):not(.live)").length;
    const cardId = currentCard.meta.card_id;
    let settled = false;

    // [信息流 Task5] SSE 过程卡先行（from=0）；连不上回退等待气泡（V6 降级保底）
    let waiting = null;
    // [F2③] 原 `if (window.EventFlow)`——module 化后恒在，守卫保留结构
    if (EventFlow) {
      EventFlow.start(WORK_ID, cardId, () => {
        waiting = createWaitingBubble("思考中…", cardId);
      });
    } else {
      waiting = createWaitingBubble("思考中…", cardId);
    }

    // [hotfix] 后端完成但连接断了 → 从会话记录恢复回复，杜绝无限转圈
    async function tryRecover() {
      try { console.info("[recover] tick"); } catch (e0) { /* 零阻断 */ }
      if (settled) return false;
      try {
        const msgs = await API.getSession(WORK_ID, cardId);
        if (msgs.length >= baseline + 1 && msgs[msgs.length - 1].role === "assistant") {
          settled = true;
          if (waiting) waiting.stop();
          setTimeout(() => EventFlow.stop(), 2500);
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
    let pollFails = 0;   // [F3②] 连续失败提示（继续轮询不放弃）
    const pollTimer = setInterval(async () => {
      try { await tryRecover(); pollFails = 0; }
      catch (e) {
        pollFails++;
        if (pollFails === 2) notify("进度查询连续失败，仍在重试…", "warn");
      }
    }, 15000);

    sendBtn.disabled = true;

    try {
      const resp = await API.postMessage(WORK_ID, cardId, content);
      if (settled) return;   // 已由轮询恢复，丢弃迟到响应
      settled = true;
      if (waiting) waiting.stop();
      setTimeout(() => EventFlow.stop(), 2500);   // 让 done 帧送达后收流
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
        reloadCanvas();
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
    // [F2③] 原 `if (window.EventFlow)`——module 化后恒在，守卫保留结构
    if (EventFlow) {
      EventFlow.start(WORK_ID, cardId, () => {
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
      setTimeout(() => EventFlow.stop(), 2500);
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
  // 修法：卡号存 URL hash（open() 内写入），加载时读出来重开。不引入浏览器本地持久存储等前端存储。
  async function restoreFromHash() {
    // [S2/R-09] 选中层复合键解析：#wid/cid ＝ 指向该 work 的卡；旧 #cid ＝ 回落当前页 WORK_ID
    const sel = navStore.select.get();
    if (!sel) return false;
    if (sel.wid !== WORK_ID) {
      window.location.href = "/work/" + sel.wid + "#" + sel.cid;
      return true;
    }
    try {
      await open(sel.cid);
      // [刷新恢复实时状态 2026-10-03] 历史回读完成后，查一次后端进度：若这张卡有调用在跑，
      // 把等待气泡与 2 秒轮询挂回去——刷新不再丢失「正在思考」的实时状态。
      await attachRunningState(sel.cid);
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
    if (statusDraftBtn) statusDraftBtn.onclick = () => openDraft("status");
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
  // [S2/M3] 版本探测 60s 周期：version 未变零开销（连 diff 都不跑）；变了才全量重拉
  // 并只 diff 变化 work 子树。相对时间重算随真实数据变更触发（旧「60s 整树重渲」退役）。
  setInterval(() => { navRenderer.probeVersion(); }, 60000);
  // 初始导航渲染（画布态窄条悬停浮层也有树）
  renderNav();

  // ═══ [迭代四 F3⑤] 停止按钮（顶栏两态可见）：U-7 停止真生效（档案 end=human_stop）═══
  {
    const stopBtn = document.getElementById("stopBtn");
    if (stopBtn) stopBtn.onclick = async () => {
      const cardId = currentCard ? currentCard.meta.card_id : (window.GetCanvasState && (() => { const s = window.GetCanvasState(); return s && s._lastOpenedCard; })());
      if (!cardId) { notify("没有正在查看的卡", "warn"); return; }
      stopBtn.disabled = true;
      try {
        await postStop(WORK_ID, cardId);
        notify("已发送停止指令", "warn");
      } catch (e) { notify("停止指令失败", "error"); }
      stopBtn.disabled = false;
    };
  }

  // ═══ [迭代四 F4①②③] 通知按钮＋面板：拉 K4 端点、渲染列表、点击跳卡、已读推进 ═══
  {
    const btn = document.getElementById("navNotifyBtn");
    const panel = document.getElementById("notifyPanel");
    const list = document.getElementById("npList");
    const count = document.getElementById("navNotifyCount");
    const READ_KEY = `projMsgRead:${WORK_ID}`;

    // [S2/V-S5] 裸本地存储访问收口：经 state_layers 的 storage 包装（行为等价）
    function readMap() {
      try { return JSON.parse(storageGet(null, READ_KEY, "{}") || "{}"); } catch (e) { return {}; }
    }
    function writeRead(map) {
      try {
        const entries = Object.entries(map).sort((a, b) => b[1] - a[1]).slice(0, 500);   // 上限 500 按 ts 淘汰
        storageSet(null, READ_KEY, JSON.stringify(Object.fromEntries(entries)));
      } catch (e) { /* 零阻断 */ }
    }

    async function refreshBadge() {
      try {
        const d = await API.getCardMessages(WORK_ID);
        const unread = d.unread_by_card || {};
        const n = Object.keys(unread).length;
        count.textContent = n > 99 ? "99+" : n;
        count.className = "nav-notify-count" + (n > 0 ? " on" : "");
      } catch (e) { /* 展示层零阻断 */ }
    }

    function renderPanel(items) {
      const read = readMap();
      const bad = items.filter(i => !i.format_ok);
      const good = items.filter(i => i.format_ok);
      const mk = (i, isBad) => {
        const unreadKey = `${i.to_card}:${i.id}`;
        const btn = document.createElement("button");
        btn.className = "np-item" + (isBad ? " bad" : "");
        const ts = (i.ts || "").slice(0, 16).replace("T", " ");
        btn.innerHTML = `<div>${escapeHtml(ts)} · ${escapeHtml(i.from_title || i.from_card)} → ${escapeHtml(i.to_card)}` +
          ` · <span class="cmsg-type${isBad ? " bad" : ""}">${escapeHtml(isBad ? "格式异常" : i.type_display)}</span></div>` +
          `<div class="np-meta">${escapeHtml((i.summary || "").slice(0, 60))}</div>` +
          (read[unreadKey] ? "" : `<div class="np-meta">（未读）</div>`);
        btn.onclick = () => {
          read[unreadKey] = Date.now();
          writeRead(read);
          panel.classList.remove("open");
          Workspace.open(i.to_card);   // 快捷跳收件卡（B4）
          setTimeout(async () => {
            const el = document.querySelector(`[data-seq="${i.seq}"]`);
            if (el) el.scrollIntoView({ block: "center" });
          }, 600);
        };
        return btn;
      };
      list.innerHTML = "";
      if (!items.length) { list.innerHTML = `<div class="np-item">（暂无卡间消息）</div>`; return; }
      for (const i of good) list.appendChild(mk(i, false));
      if (bad.length) {
        const head = document.createElement("div");
        head.className = "np-meta";
        head.style.padding = "8px 12px";
        head.textContent = `格式异常 ${bad.length} 条`;
        list.appendChild(head);
        for (const i of bad) list.appendChild(mk(i, true));
      }
    }

    if (btn) btn.onclick = async () => {
      if (panel.classList.contains("open")) { panel.classList.remove("open"); return; }
      try {
        const d = await API.getCardMessages(WORK_ID);
        renderPanel(d.items || []);
        panel.classList.add("open");
        refreshBadge();
      } catch (e) { notify("通知拉取失败", "error"); }
    };
    const npClose = document.getElementById("npClose");
    if (npClose) npClose.onclick = () => panel.classList.remove("open");

    refreshBadge();
    setInterval(refreshBadge, 60000);
  }

  restoreFromHash();
