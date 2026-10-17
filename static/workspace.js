/* workspace.js - [T17-F4/F5] 卡片工作区：左栏对话导航 + 中区讨论区 + 右栏 Inspector
   （原「左35%资料 + 右65%讨论区」结构退役；编辑能力整体迁入右栏 Inspector） */
/* 迭代四 F2③：IIFE → ES module；escapeHtml/API/EventFlow 走模块 import。
 * [阶段4 A4.1 2026-10-17] 本文件**不再 import canvas.js**：reloadCanvas 三处已删、
 * 关系下拉与停止回落改读导航快照 ⇒ 对话页模块图与画布侧彻底解耦，双向依赖清零。
 * Workspace.open 仍导出（canvas.js 已改为整页跳转，不再消费；留作对外薄壳）。 */
import { API } from "./esm/api.js?v=35";
import { escapeHtml } from "./esm/util.js?v=30";
import { EventFlow } from "./stream.js?v=37";
import { renderMarkdown } from "./esm/md.js?v=30";
import { store } from "./esm/store.js?v=30";
import { notify, postStop } from "./esm/api.js?v=35";
import { parseCardHeader, stripCardHeader } from "./esm/cardmsg.js?v=30";
// [S2/S3] 导航体系整体抽入 esm/nav_system.js（[阶段1 A1.2]）；本文件只留面包屑用的 ancestorCards。
import { ancestorCards } from "./esm/nav_breadcrumb.js?v=30";
import { createNavSystem } from "./esm/nav_system.js?v=33";
import { loadRulePanel, getRuleCardId } from "./esm/rule_panel.js?v=31";
/* [对话流照抄 dsh 完全版 v3 2026-10-17] 输入系统整件来自 flow/（dsh 移植件）：
   machine＝输入状态机、submission-policy＝忙时 Enter 策略、
   input-bar＝输入卡、queue-store/queue-dock＝排队消息与排队条。 */
import { ComposerSubmissionPolicy } from "./esm/flow/submission-policy.js?v=1";
import { createInputBar } from "./esm/flow/input-bar.js?v=1";
import { createQueueStore } from "./esm/flow/queue-store.js?v=1";
import { createQueueDock } from "./esm/flow/queue-dock.js?v=1";

/* [惰性新建 2026-10-17] WORK_ID 由 const 改 let：草稿项目/草稿卡落库成功后就地换真 id
 * （不整页跳转，首条消息"只感知正常发送"），本模块所有闭包引用同一绑定，随取随新。
 * 同步写 window.__WORK_ID__：nav_system / rule_panel 惰性读，落库后不失准。 */
let WORK_ID = window.__WORK_ID__;
/* 草稿态：IS_DRAFT_PROJECT＝项目尚未落库（/work/draft 或画布页点＋）。
 * DRAFT_CARD＝刚建、正在后台落库的卡（**先出画面，数据后到**：绝不阻塞 UI）。
 * ★ 建卡的判定不变（任务书 D4：提交标题不等待落库）——落库是**后台立刻发起**，不等它返回。 */
let IS_DRAFT_PROJECT = (WORK_ID === "draft");
let DRAFT_CARD = null;
let _draftRootCardId = "";   // 草稿项目落库后拿到的根卡 id
let _draftBoardSeq = 0;      // 新卡画板键序号（连开两张不复用画板状态）
  const ws = document.getElementById("workspace");
  const inspBody = document.getElementById("inspBody");
  const inspector = document.getElementById("inspector");
  const navTree = document.getElementById("navTree");
  const chatArea = document.getElementById("chatArea");

  /* ═══ [对话流照抄 dsh 完全版 v3 2026-10-17] 输入系统整件交给 flow/ ═══
   * 退役（任务书 §三）：
   *   · `isRunning` 裸变量        → sessionState.running（输入条/运行条/主键共读一处）
   *   · `inputBox.disabled` 锁     → 删除。disabled 只按 dsh 的五条判据
   *                                  （removed/inert/!live/blocked/parentOffline），
   *                                  **运行中不在其中**（"Running input stays free"）
   *   · `setRunState` 改按钮类名   → 改由 input-bar 的 refreshPrimary 出两态
   *   · 15 秒自愈轮询              → 删除（状态机 ＋ SSE 收口；见 send()）
   *   · 「等待气泡 180 秒」旧兜底   → 删除（运行条 ＋ 停止键取代）
   */
  const sessionState = {
    running: false,
    removed: false,
    // live＝「会话面在场」（dsh: input/keyboard/actions 三面齐备）。本机＝有当前卡。
    // 用 getter 而不是快照字段：currentCard 在 open()/草稿建卡多处被替换，
    // 每次赋值都去同步一个布尔量是「靠下游记得做对」，用 getter 从根上没有这个问题。
    get live() { return !!currentCard; },
    blocked: undefined,
    parentOffline: false,
    subagent: null,
  };
  let inputBar = null;        // flow/input-bar 实例
  let queueStore = null;      // flow/queue-store（前端权威队列）
  let queueDock = null;       // flow/queue-dock 实例
  let submitPolicy = null;    // flow/submission-policy 实例

  // 运行态唯一入口：一处改，四处生效（会话事实 / 输入条 / 排队条 / 事件流运行条）
  function setRunState(running) {
    sessionState.running = !!running;
    if (inputBar) inputBar.refresh();
    if (queueDock) queueDock.refresh();
    if (currentCard) {
      const cid = currentCard.meta.card_id;
      try { EventFlow.setRunning(WORK_ID, cid, !!running); } catch (e) { /* 零阻断 */ }
    }
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

  // [阶段5 A5.1 2026-10-17] setAppView 机制退役：画布/对话已拆成两个独立页面，
  // 各自静态布局（模板 body 挂 page-canvas / page-chat），不再需要运行期切视图。

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
    if (cardId === "draft") return;   // [惰性新建] 草稿卡无真身：不走 getCard（否则 404）
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
      if (inputBar) inputBar.refresh();   // 会话面到场 ⇒ 输入卡解除 inert
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
      // [阶段5 A5.1] 视图切换机制退役：本页恒为对话态，无需 setAppView。
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

  // ==================== [惰性新建 2026-10-17] 落库序列 ====================
  // **先出画面，数据后到**：UI 绝不等待这里的 POST。
  // 草稿项目 → POST /api/works；新卡 → POST /cards（项目若也是草稿，先项目后卡）。
  // 带锁：并发触发复用同一 Promise（绝不两次 POST）。失败：调用方 toast、保持现场、可重试。
  let _persistPromise = null;
  async function ensurePersisted() {
    if (_persistPromise) return _persistPromise;                 // 落库中：排队复用
    if (!IS_DRAFT_PROJECT && !DRAFT_CARD) {
      return { wid: WORK_ID, cid: currentCard ? currentCard.meta.card_id : "" };
    }
    _persistPromise = (async () => {
      // ① 项目草稿（缺省标题"新项目"）
      if (IS_DRAFT_PROJECT) {
        const w = await API.createWork("新项目");
        WORK_ID = w.work_id;
        window.__WORK_ID__ = w.work_id;                          // nav/rule 惰性读，随取随新
        IS_DRAFT_PROJECT = false;
        _draftRootCardId = w.work_card_id || "";
        history.replaceState(null, "", "/work/" + WORK_ID);
        loadRulePanel().catch(() => {});
      }
      // ② 新卡
      let cid = currentCard ? currentCard.meta.card_id : "";
      if (DRAFT_CARD) {
        const parent = DRAFT_CARD.parent || _draftRootCardId || "";
        const r = await API.createCard(WORK_ID, {
          title: DRAFT_CARD.title, source: "用户直接创建",
          card_type: DRAFT_CARD.card_type || "", parent,
        });
        cid = (r && r.meta && r.meta.card_id) || "";
        DRAFT_CARD = null;
        if (currentCard) currentCard.meta.card_id = cid;
        history.replaceState(null, "", "/work/" + WORK_ID + "/card/" + cid);
        renderNav().catch(() => {});   // 卡进左栏
        renderInspector().catch(() => {});   // 右栏把临时 id 换成真 card_id
      }
      return { wid: WORK_ID, cid };
    })();
    try { return await _persistPromise; } finally { _persistPromise = null; }
  }
  // 供 rule_panel.js（跨模块）在草稿态保存规则区前调用：先把项目落库。
  window.__draftEnsurePersisted = () => ensurePersisted();

  /** 先出画面：立刻渲染这张新卡的对话（标题已在手），落库**后台立刻发起**、不等它返回。 */
  function enterNewCard({ title, parent, card_type }) {
    DRAFT_CARD = { title: ((title || "").trim() || "新卡"), parent: parent || "", card_type: card_type || "" };
    currentCard = {
      meta: {
        card_id: "draft", title: DRAFT_CARD.title, card_type: DRAFT_CARD.card_type || "topic",
        parent: DRAFT_CARD.parent, links: [], source: "用户直接创建", work_id: WORK_ID,
      },
      // 形状对齐真卡：mission/status_block/rulings 为字符串，history 为数组
      body: { mission: "", status_block: "", rulings: "", history: [] },
    };
    const boardKey = "new-" + (++_draftBoardSeq);
    try { EventFlow.activate(WORK_ID, boardKey); } catch (e) { /* 零阻断 */ }
    // [对话流照抄 dsh v3] board() 现在返回视图的**节点列**：不再清 innerHTML
    //（那会把视图骨架一起抹掉）。新 boardKey ⇒ 新会话 ⇒ 视图天然是空的。
    renderInspector().catch(() => {});
    const crumb = document.getElementById("chatCrumb");
    if (crumb) crumb.innerHTML = `<span class="cseg cur">${escapeHtml(DRAFT_CARD.title)}</span>`;
    if (inputBar) { inputBar.setDraft(""); inputBar.focus(); }
    scrollChatBottom();
    // ★ 画面已出，立刻后台落库（不 await）：用户零等待；成功则静默把 URL/ids 换成真身。
    ensurePersisted().catch((e) => {
      notify("建卡失败：" + String((e && e.message) || e) + "（可重试）", "error");
    });
  }

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
          await ensurePersisted();   // [D3] 改名即数据动作：草稿态先落库（项目/卡），再写真身
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
        await ensurePersisted();   // [D3] 保存使命即数据动作
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
        await ensurePersisted();   // [D3] 保存现状即数据动作
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
          await ensurePersisted();   // [D3] 改关系即数据动作
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
          await ensurePersisted();   // [D3] 加关系即数据动作
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
    // [惰性新建 2026-10-17] **先出画面**：提交标题立刻切到这张新卡的对话（标题已在手），
    // 落库后台立刻发起、不等它返回——用户零等待。失败则 toast，标题留在卡上可重试。
    document.getElementById("addChildBtn").onclick = () => {
      const title = document.getElementById("childTitle").value.trim();
      if (!title) { API.toast("请输入子卡标题", "error"); return; }
      document.getElementById("childTitle").value = "";
      // card_type 传空串 → 后端按树映射（底座收口 T5.c）
      enterNewCard({ title, parent: currentCard.meta.card_id, card_type: "" });
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

  /** 注入 stream.js：用户正文 HTML / assistant / cardmsg 三类内容构造。
      [对话流照抄 dsh v3] 用户气泡的**外壳**已归 flow/message-item（dsh 几何原值），
      本文件只提供正文 HTML（md/换行/代码块）。 */
  function registerMessageRenderers() {
    EventFlow.setMessageRenderers({
      userHtml: (text) => formatMsgContent(String(text == null ? "" : text)),
      assistant: renderAssistantNode,
      cardmsg: renderCardMsgNode,
    });
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

  /* [对话流照抄 dsh v3 · §三 退役清单] 原「等待气泡」整件已删除：
     createWaitingBubble（含 180 秒心跳提示、内联停止键、2 秒一次的阶段轮询）
     与 attachHeartbeat 一并退场——它们被 dsh 的「运行条 + 停止键」取代
     （ChatView.tsx:107-140 的 TurnStatus ＋ InputBar 的 primaryStops）。
     ★ 同时删除的还有 STAGE_TEXT 的使用点：它只服务于那个气泡。 */

  // [刷新恢复实时状态 2026-10-03] 页面加载时恢复「正在跑」的实时状态。
  // 问题：刷新即随 JS 上下文销毁实时视图；后端 progress 状态（模块级内存，
  //       key=(work_id,card_id)）与端点都还在，但加载路径从不查它。
  // 修法：加载时查一次后端 progress——活跃态 ⇒ 置运行态（主键转停止键）＋接上事件流；
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
      setRunState(true);
      // [对话流照抄 dsh v3] 刷新恢复只做两件事：置运行态（主键变停止键）＋ 接上事件流。
      // 等待气泡已退役——运行条由 chat-view 出（"Deep diving..."，15s 后显时钟）。
      if (EventFlow) EventFlow.start(WORK_ID, cardId, null);
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
    if (sessionState.running) return;   // 一轮在跑时不重入（运行中主键＝停止，见 doStop）
    const content = statusOp ? "更新现状" : String(inputBar ? inputBar.getDraft() : "").trim();
    if (!content) return;

    // [惰性新建 2026-10-17 · D3] 发送是数据动作：草稿态先落库（项目/卡），再走正常发送。
    // 失败：toast、保持草稿态、输入框**不清空**（现场保留）、可重试。
    if (IS_DRAFT_PROJECT || DRAFT_CARD) {
      try {
        await ensurePersisted();
      } catch (e) {
        notify("保存失败，未发送：" + String((e && e.message) || e), "error");
        return;
      }
    }

    if (!statusOp) lastSentContent = content;
    const cardId = currentCard.meta.card_id;

    /* [T1 乐观上屏] 点发送瞬间先落用户气泡——不等 POST、不等 timeline 回流。
       否则 run_start/context/judgment_protocol 会抢在用户提问之前出现（P1）。 */
    try { EventFlow.optimisticUser(WORK_ID, cardId, content); } catch (e) { /* 零阻断 */ }
    EventFlow.start(WORK_ID, cardId, null);
    // timeline 回流对账：真实 user_message 到达时按内容原地升级（补 seq），不出双气泡
    try { EventFlow.pullUserBubble(WORK_ID, cardId).catch(() => {}); } catch (e) { /* 零阻断 */ }

    /* [发送被接受] 清草稿并清撤销栈（machine.ts:544-553：已发内容不得被撤销复活）。
       专用回合（更新现状）不占输入框。 */
    if (!statusOp && inputBar) inputBar.commitSent();

    setRunState(true);   // 运行条起、主键转停止键、输入条照常可打字

    try {
      const resp = await API.postMessage(WORK_ID, cardId, content, op);
      setTimeout(() => EventFlow.stop(WORK_ID, cardId), 2500);   // 让 done 帧送达后收流
      // [C1 单源] 回复正文走 timeline 回流（与刷新同一条渲染路）
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
    } catch (e) {
      // [hotfix] 报错前先探一次会话：后端可能已完成，只是响应没送回来。
      // ★ 这不是「15 秒自愈轮询」（那条已按任务书 §三 删除）——只在**异常路径**探一次。
      if (await recoverIfDone(cardId)) return;
      // [发送失败] 草稿保住（不丢字）＋ 红条；未被认领的本地气泡撤下（不留幽灵消息）
      if (!statusOp && inputBar) {
        inputBar.setDraft(content);
        inputBar.failSend("请求失败：" + String((e && e.message) || e));
      }
      try { EventFlow.dropOptimisticUser(WORK_ID, cardId); } catch (e0) { /* 零阻断 */ }
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `请求失败：${escapeHtml(e.message || "")} <button class="retry-btn">重发</button>`;
      ensureMsgs(cardId).appendChild(err);
      err.querySelector(".retry-btn").onclick = () => {
        err.remove();
        resend(content);
      };
    } finally {
      setRunState(false);
      drainQueue();   // 轮结束：排队消息依序发出
    }
  }

  /** 异常路径的兜底探测：后端可能已完成、只是响应没送回来。
      在跑（且流没断）⇒ 什么都不做；否则按 timeline 结算一次。
      返回 true＝已结算（调用方不再报错）。 */
  async function recoverIfDone(cardId) {
    try {
      const p = await API.getProgress(WORK_ID, cardId);
      const alive = p && ((typeof p.active === "boolean")
        ? p.active
        : ACTIVE_STAGES.includes(p.stage));
      if (alive && !EventFlow.streamDead(WORK_ID, cardId)) return false;
    } catch (e0) { return false; }
    try {
      const r = await EventFlow.resync(WORK_ID, cardId);
      if (r && r.ok && r.done) {
        setTimeout(() => EventFlow.stop(WORK_ID, cardId), 2500);
        const note = document.createElement("div");
        note.className = "sys-tools";
        note.textContent = "⚠ 响应连接中断，已从事件流恢复回复（若有新卡片，返回画布后可见）";
        ensureMsgs(cardId).appendChild(note);
        if (window.__maybeOpenRuleEditModal) await window.__maybeOpenRuleEditModal(cardId);
        return true;
      }
    } catch (e2) { /* 静默：交给上层的错误条 */ }
    return false;
  }

  /* 轮结束自动出队：把队首交回发送链路（QueueDock 的「轮结束自动依序发出」）。
     一次只发一条——它自己收口时再调一次，天然串行。 */
  function drainQueue() {
    if (!queueStore || sessionState.running) return;
    if (!currentCard) return;
    const next = queueStore.dequeue();
    if (!next) return;
    if (inputBar) inputBar.setDraft(String(next.text || ""));
    send();
  }


  // 重发：不重复追加用户消息，直接重调接口
  // [T10-3-1/3-2] 重发前取当前卡会话：最后一条是用户消息且其后无回复条 → 复用该条
  // pair_id 重发（后端据此复用、不重复落盘）；前端未持有 pair_id → 按新消息落盘，
  // 由后端生成新标识。判据与后端二次判定逐字同一句（3-4），后端为最终仲裁（3-5）。
  // pair_id 经本文件内独立 fetch 携带——api.js 的 postMessage 签名固定 {content}，
  // 而 api.js 不在本任务书五文件清单内（红线 1）。
  async function resend(content) {
    if (sessionState.running) return;   // 同 send 的重入守卫
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
    // [对话流照抄 dsh v3] 重发同样走事件流；等待气泡已退役（运行条取代）
    if (EventFlow) EventFlow.start(WORK_ID, cardId, null);
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
      setTimeout(() => EventFlow.stop(WORK_ID, cardId), 2500);
      // [C1 单源] 同 send()：回复正文走 timeline 回流，不从响应体直接上屏。
      await EventFlow.resync(WORK_ID, cardId);
      if (window.__maybeOpenRuleEditModal) window.__maybeOpenRuleEditModal(cardId);
    } catch (e) {
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `重发失败：${escapeHtml(e.message || "")} <button class="retry-btn">再试</button>`;
      ensureMsgs(cardId).appendChild(err);
      err.querySelector(".retry-btn").onclick = () => { err.remove(); resend(content); };
    } finally {
      setRunState(false);
      drainQueue();
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

  /* ═══ [对话流照抄 dsh 完全版 v3] 输入系统装配 ═══
   * 顺序与 dsh 一致：QueueDock 在输入卡**上方**（QueueDock.module.css:19-25 的
   * 「tuck 3px under the input card」），输入卡在座位底部。
   * 提交去向（原文 default-sink 的 shell 侧）：
   *   · 不忙 ⇒ 直接发（send()）
   *   · 忙 ⇒ 入队（QueueDock 显示；轮结束由 drainQueue 依序发出）
   * ★ 本机后端没有「轮内投递窗口」⇒ steer（插话）无法真正注入正在跑的轮：
   *   per-row steer 实现为「提到队首，轮结束优先发出」，整队 steer 在运行中
   *   只提示不改状态。这是**适配层**的已知边界，不是对 dsh 语义的改写。 */
  function initComposer() {
    const seat = document.getElementById("composerSeat");
    if (!seat) return;
    queueStore = createQueueStore();
    submitPolicy = new ComposerSubmissionPolicy();
    queueDock = createQueueDock({
      mount: seat,
      store: queueStore,
      isMutable: () => !sessionState.subagent,
      isRunning: () => sessionState.running,
      notify: (level, text) => notify(text, level === "error" ? "error" : "info"),
    });
    const barMount = document.createElement("div");
    barMount.className = "composer-bar-mount";
    seat.appendChild(barMount);
    inputBar = createInputBar({
      mount: barMount,
      policy: submitPolicy,
      session: () => sessionState,
      hasQueued: () => !!queueStore && queueStore.queued().length > 0,
      notify,
      actions: {
        submit(draft, mode) {
          const text = String(draft == null ? "" : draft);
          if (sessionState.running) {
            queueStore.enqueue(text);
            if (inputBar) inputBar.commitSent();
            if (queueDock) queueDock.refresh();
            return;
          }
          send();
        },
        stop() { doStop(); },
        steerQueue() {
          if (!sessionState.running) { drainQueue(); return; }
          notify("本轮进行中，排队消息将在轮结束后依序发出", "info");
        },
        notify,
      },
    });
    inputBar.refresh();
    if (queueDock) queueDock.refresh();
  }

  initComposer();

  // ==================== [阶段2 A2.2 2026-10-17] 加载时按 URL 决定打开哪张卡 ====================
  // URL 即页面身份：/work/{wid} ＝ 画布页（停在画布态）；/work/{wid}/card/{cid} ＝ 该卡对话页。
  // 旧 hash 链接（#wid/cid）兼容：一次性重定向到新地址（hash 退役第一步）。
  async function bootRestore() {
    const pathCid = (window.__CARD_ID__ || "").trim();
    // [惰性新建 2026-10-17] 新卡对话：标题/父卡/类型由上一页经 sessionStorage **一次性携带**
    // （跳页清空内存；URL 只承担页面身份，不塞临时状态）。读出即删 ⇒ 刷新回到空白（从头再来）。
    if (pathCid === "draft") {
      let d = {};
      try {
        const raw = sessionStorage.getItem("newCard:" + WORK_ID);
        sessionStorage.removeItem("newCard:" + WORK_ID);
        if (raw) d = JSON.parse(raw) || {};
      } catch (e) { d = {}; }
      enterNewCard({ title: d.title || "", parent: d.parent || "", card_type: d.card_type || "" });
      return true;
    }
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
  // [左栏新建项目 2026-10-16 用户令] 搜索框右侧 ＋：新建项目。
  // [惰性新建 2026-10-17] 不再 POST /api/works 真落库——跳**草稿项目页** /work/draft
  // （本页是对话页、无画布，故走一次页面加载；画布页的同名钮由 canvas.js 就地 replaceState
  // 进草稿，零跳转）。点新建即出空画布、零落库；此后任何数据动作才真正创建项目。
  const navNewBtn = document.getElementById("navNewProject");
  if (navNewBtn) {
    navNewBtn.onclick = () => { location.assign("/work/draft"); };
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
        await ensurePersisted();   // [D3] 规则落盘即数据动作（草稿态先建项目）
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
