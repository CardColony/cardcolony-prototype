/* workspace.js - [T17-F4/F5] 卡片工作区：左栏对话导航 + 中区讨论区 + 右栏 Inspector
   （原「左35%资料 + 右65%讨论区」结构退役；编辑能力整体迁入右栏 Inspector） */
/* 迭代四 F2③：IIFE → ES module；escapeHtml/API/EventFlow 走模块 import。
 * [阶段4 A4.1 2026-10-17] 本文件**不再 import canvas.js**：reloadCanvas 三处已删、
 * 关系下拉与停止回落改读导航快照 ⇒ 对话页模块图与画布侧彻底解耦，双向依赖清零。
 * Workspace.open 仍导出（canvas.js 已改为整页跳转，不再消费；留作对外薄壳）。 */
import { API } from "./esm/api.js?v=42";
import { escapeHtml } from "./esm/util.js?v=30";
import { EventFlow } from "./stream.js?v=52";
import { renderMarkdown, bindCopyButtons } from "./esm/md.js?v=31";
import { store } from "./esm/store.js?v=30";
import { notify, postStop } from "./esm/api.js?v=42";
// [5715 · 抽模块] 卡间信的解析/剥头行与呈现都搬进 esm/flow/card-msg-node.js
//   （原先在装配闭包里 ⇒ 不可单测）；此处不再需要 cardmsg.js 的解析件。
// [S2/S3] 导航体系整体抽入 esm/nav_system.js（[阶段1 A1.2]）；本文件只留面包屑用的 ancestorCards。
import { ancestorCards } from "./esm/nav_breadcrumb.js?v=30";
import { createNavSystem } from "./esm/nav_system.js?v=41";
import { loadRulePanel, getRuleCardId } from "./esm/rule_panel.js?v=33";
/* [对话流照抄 dsh 完全版 v3 2026-10-17] 输入系统整件来自 flow/（dsh 移植件）：
   machine＝输入状态机、submission-policy＝忙时 Enter 策略、
   input-bar＝输入卡、queue-store/queue-dock＝排队消息与排队条。 */
import { ComposerSubmissionPolicy } from "./esm/flow/submission-policy.js?v=1";
import { InputMachine } from "./esm/flow/machine.js?v=1";
import { createInputBar } from "./esm/flow/input-bar.js?v=5";
import { createQueueStore } from "./esm/flow/queue-store.js?v=2";
import { createQueueDock } from "./esm/flow/queue-dock.js?v=3";
// [5715 · 抽模块] 消息操作条（复制/重发）与卡详情抽屉开合：原生长在渲染/装配闭包里
// ⇒ 导不出来、单测驱动不了；抽出后行为逐字不变，但可被 DOM 行为网驱动。
import { createCopyButton } from "./esm/flow/message-actions.js?v=1";
import { initInspectorToggle } from "./esm/inspector.js?v=1";
// [5715 · 抽模块] 卡间信在对话流里的呈现（原先在装配闭包里 ⇒ 不可单测）。
import { createCardMsgRenderer } from "./esm/flow/card-msg-node.js?v=1";

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

  /* ═══ [运行态单一真相源 2026-10-21] ═══
     运行态的**唯一真相源是事件流**（stream.js 的 st.busy，由 run_start / done /
     error / turn_end 帧推进）。本文件只持有它的**投影** sessionState.running
     （输入条 / 排队条 / 主键共读这一处）。
       · 宿主主动发起（发送、停止、切卡）：setRunState → 投影 + 回写 stream（fromHost）。
       · 事件流推进：onRunChange → 只更新投影，**不回写**（单向，不成环）。
     改前只有 workspace→stream 一个方向，stream 收口时没人告诉宿主 ⇒
     「对话结束页面还停在运行中，必须刷新」。 */

  /** 只更新本文件的投影（输入条 / 排队条 / 主键三处随之刷新）。 */
  function applyRunState(running, cardId) {
    sessionState.running = !!running;
    if (inputBar) inputBar.refresh();
    if (queueDock) queueDock.refresh();
  }

  // 运行态唯一入口：一处改，四处生效（会话投影 / 输入条 / 排队条 / 事件流运行条）
  // [5702] 可显式给 cardId：切卡恢复运行态时不能写到「上一张卡」的运行条上。
  function setRunState(running, cardId) {
    applyRunState(running, cardId);
    const cid = cardId || (currentCard ? currentCard.meta.card_id : "");
    if (cid) {
      try { EventFlow.setRunning(WORK_ID, cid, !!running, { fromHost: true }); } catch (e) { /* 零阻断 */ }
    }
  }

  /* 事件流推进运行态 → 只落到投影。卡对不上（事件属于别的卡）就忽略：
     那张卡自己的投影在切回去时由 switchComposer 按 sessionRunning 恢复。 */
  try {
    EventFlow.onRunChange((wid, cid, running) => {
      if (wid !== WORK_ID) return;
      // [当前卡单源 2026-10-21] 与左栏早退、高亮、面包屑用同一个判断口
      if (!navStore.select.is(WORK_ID, cid)) return;
      applyRunState(running, cid);
    });
  } catch (e) { /* 旧版 stream 无此口：零阻断 */ }

  /* ═══ [5702 · 2026-10-20] 输入会话按卡隔离 ═══
     每张卡一个 InputMachine ＋ 一份前端权威队列；DOM 仍是单实例（dsh「一次建好」），
     切卡只**换绑**。此前是「全页一个 machine/queue」⇒ 切卡时草稿、排队消息、
     运行态全部跟着走（用户报的「输入框文字被带走」）。 */
  const composerSessions = new Map();   // cardId -> { machine, queue }
  let activeComposer = null;            // { key, session }

  function composerOf(cardId) {
    const key = String(cardId || "draft");
    let s = composerSessions.get(key);
    if (!s) {
      s = { machine: new InputMachine({ now: () => Date.now() }), queue: createQueueStore() };
      composerSessions.set(key, s);
    }
    return s;
  }

  /** 切到某卡的输入会话：换绑 machine ＋ 换绑队列 ＋ 按该卡真实 busy 恢复运行态。 */
  function switchComposer(cardId) {
    if (!inputBar) return;
    const key = String(cardId || "draft");
    if (activeComposer && activeComposer.key === key) return;
    const session = composerOf(key);
    activeComposer = { key, session };
    queueStore = session.queue;                    // 既有路径（enqueue/hasQueued/drain）随之切换
    inputBar.bindSession(session.machine);
    if (queueDock) queueDock.setStore(session.queue);
    let busy = false;
    try { busy = EventFlow.sessionRunning(WORK_ID, key); } catch (e) { /* 零阻断 */ }
    setRunState(busy, key);
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
    if (!p || !chain.length) {
      /* [建项目流程 2026-10-17] 快照未就绪（草稿刚落库、导航刷新还在途）：退回当前卡标题。
         不留空面包屑——标题此刻一定在手，用户刚改完更不该看到空白。 */
      const t = (currentCard && currentCard.meta) ? String(currentCard.meta.title || "").trim() : "";
      if (t) el.innerHTML = `<span class="cseg cur">${escapeHtml(t)}</span>`;
      return;
    }
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
    /* [阶段3 A3.2 2026-10-17] 对话页无画布可切 ⇒ 整页跳转到画布页 URL。
       [5715 · 修] 与顶栏「返回项目画布」按钮**行为统一**：同样带 ?sel=当前卡，
       回去后画布直接选中它（原来只有图标带、这里不带 ⇒ 两个入口行为不一致）。 */
    root.onclick = () => {
      const sel = cardId ? "?sel=" + encodeURIComponent(cardId) : "";
      location.assign("/work/" + WORK_ID + sel);
    };
    el.appendChild(root);

    /* [对话界面商业化 2026-10-17] 深层卡的面包屑折叠：
       5 层以上（工具卡挂在子项目卡下很常见）会把顶栏挤满、把当前卡名挤到看不见。
       保留「第一层 + 最后两层」，中间折成一个可悬停查看全路径的省略号。 */
    const mids = chain.filter((c) => c.card_id !== rootCid);
    let shown = mids;
    if (mids.length > 3) {
      const dots = document.createElement("span");
      dots.className = "cseg more";
      dots.textContent = "…";
      dots.title = mids.slice(0, mids.length - 2).map((c) => (c.title || c.card_id).trim()).join(" › ");
      el.appendChild(mkSep());
      el.appendChild(dots);
      shown = mids.slice(-2);
    }
    for (const c of shown) {
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
  /* [切卡取消在途请求 2026-10-21] 光有序号守卫只能「不写 DOM」——旧请求还在飞，
     仍占着浏览器那 6 条连接，于是新卡的 getCard 排在它后面等，用户看到的就是
     「切卡转圈、然后超时」。序号变了就把上一个请求**真的取消**（不是等它跑完）。 */
  let _openCtrl = null;

  /** [建项目流程 2026-10-17] 显示板绑定：把某卡的画板挂到 #chatArea。
      从 open() 抽出——草稿落库后要**先换绑显示板、再走原发送路径**，
      两处必须是同一份实现，否则「写的板 ≠ 看的板」会再次分裂（病根②）。 */
  function bindBoard(wid, cid) {
    try { EventFlow.activate(wid, cid); } catch (e) { /* 零阻断 */ }
  }

  async function open(cardId) {
    if (cardId === "draft") return;   // [惰性新建] 草稿卡无真身：不走 getCard（否则 404）
    const mySeq = ++_openSeq;
    const stale = () => mySeq !== _openSeq;
    // [切卡取消在途请求 2026-10-21] 先掐掉上一张卡还没回来的 getCard
    if (_openCtrl) { try { _openCtrl.abort(); } catch (e) { /* 零阻断 */ } }
    const ctrl = new AbortController();
    _openCtrl = ctrl;
    // [画板四机制 T1/T4 2026-10-16] 点下去**同步翻页**：先把该卡的画板挂上
    // #chatArea，不等 getCard/inspector/timeline 任一 await——否则旧卡内容滞留，
    // 且快速连点时后到的 await 会把旧卡写进新卡（P4 串卡的界面侧表现）。
    bindBoard(WORK_ID, cardId);
    switchComposer(cardId);   // [5702] 输入会话同步换绑：点下去草稿立刻按卡切换
    /* [当前卡单源 2026-10-21] 身份在「点下去」这一刻就写进路径——它是 select.is()
       的唯一来源。原来拖到 open 末尾才写，中途任何判断（早退/高亮/徽标）看到的
       都还是上一张卡。 */
    try { navStore.select.set(WORK_ID, cardId); } catch (e) { /* 零阻断 */ }
    try {
      const card = await API.getCard(WORK_ID, cardId, { signal: ctrl.signal });
      if (stale()) return;
      currentCard = card;
      /* [切卡跟手 2026-10-21] 模型徽标（顶栏那颗）原来绑的是**页面加载时**的卡号
         （window.__CARD_ID__），切卡不刷新 ⇒ 在卡 A 切了 k3，切到卡 B 还显示 k3，
         而且在卡 B 上再切会写进卡 A。这里把「当前卡变了」广播出去，徽标换行重读。 */
      try {
        window.dispatchEvent(new CustomEvent("ui:card-changed", { detail: { cardId } }));
      } catch (e) { /* 零阻断 */ }
      // [角色机制 5710] 角色卡的主界面就是右栏台账（关注/判词/警报）；
      // 进角色页自动展开 Inspector，否则用户只看到聊天，看不到它在盯什么。
      if (card.meta && card.meta.card_type === "role") {
        inspector.classList.add("open");
      }
      if (inputBar) inputBar.refresh();   // 会话面到场 ⇒ 输入卡解除 inert
      applyEmptyState(card);
      // [会话渲染单源化 2026-10-15 · C1] 刷新/换卡恢复走**唯一一条路**：
      // GET /timeline → 装配器 → 节点快照 → 渲染。原「先 renderChat 读 /session
      // 上屏消息、再 replayTrace 补过程行」两通道已合并成这一次调用。
      //
      // [首屏顺序 2026-10-20] **先出对话，再填右栏**（原来是反的）。
      // 原因：renderInspector 对角色卡要额外 await /api/.../role（档案+六节+判断簿+
      // 警报簿+职责书），它压在 replayTrace 之前 ⇒ 用户点卡后先等右栏、才看到对话。
      // 对话区是主区域，右栏是附属；把主区域的 await 提到前面，点卡立刻出内容。
      // 仍然是串行（不是并行）——两处都写 DOM，并行会在快速切卡时互相写错卡。
      {
        await EventFlow.replayTrace(WORK_ID, cardId);
        if (stale()) return;
      }
      await renderInspector();
      if (stale()) return;
      /* [5702 · 治「切到在跑的卡不再更新」] replayTrace 内部会 stopES 关流；
         恢复完再按后端真实运行态接流——此前只有页面加载（bootRestore）会接，
         页内切卡后只能刷新才动。 */
      attachRunningState(cardId).catch(() => {});
      /* [5715 · 修] 选中态写**路径**（页面身份即选中态）。
       * 原写法：先 select.set() 写 hash、再 replaceState 写路径 ⇒ hash 被后写者抹掉
       * ⇒ select.get() 恒 null ⇒ **侧栏从不高亮当前卡**、面包屑/定位/收起全部全失准。
       * hash 已退役承载选中（2026-10-17 A2.2），旧 hash 链接由 bootRestore 一次性重定向。 */
      // [当前卡单源] 身份已在函数开头写入路径（点下去即成立），此处不再重复写。
      // [S3/R-25/V3-F5] 成功跳转（getCard 已取回）才记最近访问；失败/灰态不进此路径
      navFind.recordRecent(WORK_ID, cardId, card.meta.title);
      // [阶段5 A5.1] 视图切换机制退役：本页恒为对话态，无需 setAppView。
      if (stale()) return;
      // 导航树重渲以应用当前卡高亮与祖先路径展开
      /* [切卡不再重拉导航 2026-10-21] 传 {network:false}：切卡只需要按已有快照重画
         （高亮/祖先展开/面包屑），与 /api/nav、/api/nav/version 无关。此前每切一张卡
         都白拉这两个接口 ⇒ 连点十张卡二十个请求排队，切卡从 14ms 涨到 655ms。 */
      await renderNav({ network: false });
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
  /** @param {string} [titleHint] 草稿项目的名字（改名链传用户刚起的名；
      发送链不传 ⇒ 缺省「新项目」）。项目与根卡共用这一个名字。 */
  async function ensurePersisted(titleHint) {
    if (_persistPromise) return _persistPromise;                 // 落库中：排队复用
    if (!IS_DRAFT_PROJECT && !DRAFT_CARD) {
      return { wid: WORK_ID, cid: currentCard ? currentCard.meta.card_id : "" };
    }
    _persistPromise = (async () => {
      // ① 项目草稿（缺省标题"新项目"）
      if (IS_DRAFT_PROJECT) {
        const w = await API.createWork(String(titleHint || "").trim() || "新项目");
        WORK_ID = w.work_id;
        window.__WORK_ID__ = w.work_id;                          // nav/rule 惰性读，随取随新
        IS_DRAFT_PROJECT = false;
        _draftRootCardId = w.work_card_id || "";
        syncCanvasEntry();   // [5715] 落库后才有画布可回 ⇒ 放出「返回项目画布」入口
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
      } else if (cid === "draft" && _draftRootCardId) {
        /* [建项目流程 2026-10-17] 项目草稿：根卡是**随项目一起建好**的（work_card_id），
           不是再 POST 一张。把画面身份从临时键 "draft" 换成真根卡 id——
           否则 send() 会拿 "draft" 去写板/发消息（写进用户看不见的键）。
           落库结果从这里返回，调用方据此换绑显示板，不从旧 DOM 状态猜。 */
        cid = _draftRootCardId;
      }
      if (currentCard && cid) currentCard.meta.card_id = cid;
      /* 地址更新一律 replaceState（静默换址，零整页加载、不堆历史）。
         有真卡 ⇒ 对话页地址；只有项目 ⇒ 画布页地址（兜底，正常不会走到）。 */
      if (cid) {
        history.replaceState(null, "", "/work/" + WORK_ID + "/card/" + cid);
        /* ★ [建项目流程 2026-10-17] 显示板就在**身份变更这一刻**换绑，不放发送里：
           建卡路径的落库常在用户开始打字之前就完成了（页面一进就后台发起），
           等发送时 DRAFT_CARD 已是 null，三步分支根本不进——板会一直停在 new-N，
           气泡写进游离板，假界面原样复现。换绑跟着「id 变了」走，不跟「谁触发」走。 */
        bindBoard(WORK_ID, cid);
        switchComposer(cid);                // [5702] 草稿卡落库：输入会话跟着换到真 id
        renderNav().catch(() => {});        // 新项目/新卡进左栏
        renderInspector().catch(() => {});  // 右栏把临时 id 换成真 card_id
        /* [草稿模型 2026-10-21] 草稿态在徽标上选的模型，落库这一刻写进项目配置表：
           scope=work ⇒ 项目级（表里所有卡，此时只有根卡）；scope=card ⇒ 只写这张新卡。
           写前先清暂存，写完通知徽标换行重读（它此时还停在 "draft" 那套显示上）。 */
        try {
          const pick = JSON.parse(sessionStorage.getItem("draftModel") || "null");
          if (pick && pick.model) {
            sessionStorage.removeItem("draftModel");
            const url = pick.scope === "card"
              ? `/api/settings/model-overrides/${WORK_ID}/card/${cid}`
              : `/api/settings/model-overrides/${WORK_ID}`;
            fetch(url, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ default_model: pick.model }),
            }).then(() => {
              try {
                window.dispatchEvent(new CustomEvent("ui:card-changed", { detail: { cardId: cid } }));
              } catch (e) { /* 零阻断 */ }
            }).catch(() => { /* 写不进不影响建项目本身 */ });
          }
        } catch (e) { /* 暂存坏了当没选过 */ }
      } else {
        history.replaceState(null, "", "/work/" + WORK_ID);
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
    /* [5715 · 修] 草稿卡原本零空态 ⇒ 整屏空白。补上（按卡类型给文案，视图键＝画板键）。
       顺带收首屏骨架：草稿路径不走 replayTrace ⇒ markLoaded 从不被调 ⇒ 骨架永远不收。 */
    applyEmptyState(currentCard, boardKey);
    try { EventFlow.markLoaded(WORK_ID, boardKey); } catch (e) { /* 零阻断 */ }
    switchComposer("draft");   // [5702] 新卡用独立输入会话（随后 setDraft("") 清空）
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

  /** [建项目流程 2026-10-17] 项目草稿页（/work/draft）：落地即这张画面——
      标题「新项目」、空消息区、输入框已聚焦。**零网络、零 POST**：
      草稿项目没有库可查（拉了就是 404），也不该在「用户什么都没干」时就在盘上留空壳。
      第一条数据动作（发消息 / 改名 / 规则保存）才经 ensurePersisted() 真建项目＋根卡。 */
  function enterDraftProjectPage() {
    currentCard = {
      meta: {
        card_id: "draft", title: "新项目", card_type: "project",
        parent: "", links: [], source: "用户直接创建", work_id: WORK_ID,
      },
      // 形状对齐真卡（同 enterNewCard）：mission/status_block/rulings 字符串、history 数组
      body: { mission: "", status_block: "", rulings: "", history: [] },
    };
    try { EventFlow.activate(WORK_ID, "draft"); } catch (e) { /* 零阻断 */ }
    /* [5715 · 修] 新建项目页原本零空态 ⇒ 整屏空白（新用户第一屏什么都没有）。
       补一张项目落地引导：说清"从这儿开始"，并给三个可直接点的起始问法。 */
    try {
      EventFlow.setEmpty(WORK_ID, "draft", {
        title: "新项目",
        desc: DRAFT_PROJECT_COPY.desc,
        hints: DRAFT_PROJECT_COPY.hints.map((h) => ({ label: h, text: h })),
      });
    } catch (e) { /* 零阻断 */ }
    /* [5715 · 修] 收首屏骨架：草稿路径不走 replayTrace ⇒ markLoaded 从不被调 ⇒ 骨架永远不收。 */
    try { EventFlow.markLoaded(WORK_ID, "draft"); } catch (e) { /* 零阻断 */ }
    switchComposer("draft");   // [5702] 草稿项目页独立输入会话
    renderInspector().catch(() => {});
    const crumb = document.getElementById("chatCrumb");
    if (crumb) crumb.innerHTML = `<span class="cseg cur">新项目</span>`;
    if (inputBar) { inputBar.setDraft(""); inputBar.focus(); }
    scrollChatBottom();
  }

  // [T17-F4] renderLeft() 拆二之一：renderInspector()——原左栏 DOM 原样迁入右栏
  // inspBody（仅去掉 wsBackBtn：返回按钮上移到对话区头部，见 work.html .ws-head）。
  // 职责区/结论区/否决/关系/历史/建子卡/删卡 逻辑一字未动（R-021）。
  // [自动模式 2026-10-17 · 回灌 5700 块0.5] 目标态中文标签（机器态→人话）。
  // 与 goal 区（上下文 152 区）的机器态原值分工：那里给模型读、这里给人读。
  function goalStateLabel(state) {
    const s = String(state || "").trim();
    if (s === "active") return "推进中";
    if (s === "paused") return "已暂停";
    if (s === "blocked") return "卡住";
    if (s === "complete") return "已完成";
    return "未设定";
  }

  // [自动模式 2026-10-17 · 回灌 5700 块0.5] 底边栏 自动/手动 按钮的状态刷新。
  // 与右栏卡详情里的 #autoToggle 同源：都读 currentCard.meta.auto_mode。
  // U1：任何卡（根卡/普卡）都能开、随时可切——开关，不是建卡定死。
  function renderAutoToggle() {
    const btn = document.getElementById("autoToggleBtn");
    if (!btn || !currentCard) return;
    const isAuto = Number(currentCard.meta.auto_mode || 0) === 1;
    btn.textContent = isAuto ? "自动模式" : "手动模式";
    btn.classList.toggle("on", isAuto);
    btn.title = isAuto
      ? "本卡处于自动模式（权限全开、自动续跑）——点击切回手动"
      : "本卡处于手动模式——点击开启自动模式（权限全开、自动续跑）";
  }

  // 切本卡自动模式：右栏按钮与底边栏按钮**共用这一处逻辑**。
  // 调 PATCH 落 auto_mode；后端在 auto_mode 变化时把该卡 perms 重算为全开／
  // 按类型还原（权限随之变，不需要前端另发请求——块0.2 的开关动作）。
  async function toggleAutoMode(btn) {
    if (!currentCard) return;
    const cid = currentCard.meta.card_id;
    const next = Number(currentCard.meta.auto_mode || 0) === 1 ? 0 : 1;
    if (btn) btn.disabled = true;
    try {
      const r = await API.patchCard(
        WORK_ID, cid, { auto_mode: next },
        next ? "用户开启自动模式" : "用户关闭自动模式");
      currentCard = r;
      notify(next
        ? "已开启自动模式：权限全开，模型将自动续跑"
        : "已关闭自动模式：权限按卡类型还原", "warn");
      await renderInspector();
      renderAutoToggle();
    } catch (e) {
      notify("切换自动模式失败", "error");
      if (btn) btn.disabled = false;
    }
  }

  // [6702 移植 P8] 人工恢复自动推进。后端 POST /auto/resume 会落
  // goal_state=active ＋ 清 goal_await_children / goal_await_wakes，然后 kick。
  // 与 toggleAutoMode 分开：那个是开关（改权限），这个是「状态复位」（不改权限）。
  async function resumeAuto(btn) {
    if (!currentCard) return;
    const cid = currentCard.meta.card_id;
    if (btn) btn.disabled = true;
    try {
      const r = await API.call(
        "POST", `/api/works/${WORK_ID}/cards/${cid}/auto/resume`);
      currentCard = await API.getCard(WORK_ID, cid);
      notify(r && r.resumed ? "已恢复自动推进" : "该卡已在推进中", "warn");
      await renderInspector();
      renderAutoToggle();
    } catch (e) {
      notify("恢复失败", "error");
      if (btn) btn.disabled = false;
    }
  }

  // [角色机制 5710] 角色卡右栏：关注对象 / 判断台账 / 警报 / 立即巡检。
  // 角色卡不是作业卡：不显示建子卡与危险操作；它的「使命」就是根卡写的职责书。
  function fmtRoleTs(ts) {
    const s = String(ts || "");
    const m = s.match(/T(\d{2}:\d{2})/);
    return m ? m[1] : s.slice(0, 16);
  }

  function roleDutyLabel(state) {
    return ({ active: "值班中", suspended: "已停职", archived: "已撤岗" })[state] || state;
  }

  function buildRoleHtml(info, m) {
    if (!info) {
      return `<div class="role-panel"><div class="rej-empty">角色信息读取失败（稍后重试）</div></div>`;
    }
    const prof = info.profile || {};
    const jurisdiction = info.jurisdiction || prof.jurisdiction || {};
    const watch = info.watch || prof.watch || {};
    const watched = watch.cards || [];
    const signals = watch.signals || [];
    const sc = info.speak_contract || prof.speak_contract || {};
    const esc = info.escalation || prof.escalation || {};
    const duty = info.duty || prof.duty || {};
    const charter = info.charter || prof.charter || {};
    const verdicts = (info.verdicts || []).slice(0, 10);
    const alarms = (info.alarms || []).slice(0, 10);
    const directives = (info.directives || []).slice(0, 10);
    const openDirs = (info.open_directives || []).length;
    const dirStatus = { issued: "已下发", complied: "达标", rejected: "未达标", rework: "已返工", overdue: "已超时", cancelled: "撤销" };
    const pending = (info.pending_sources || []).length;
    const dutyState = duty.state || "active";
    const canRun = dutyState === "active";
    const watchIds = new Set(watched.map(w => w.card_id));
    const candidates = ((navStore.snapshot.get().projects || []).flatMap(p => p.cards || []))
      .filter(c => c.card_id !== m.card_id && c.card_type !== "role" && !watchIds.has(c.card_id));
    const watchHtml = watched.length
      ? watched.map(w => `<span class="role-w" title="${escapeHtml(w.title)}">
            <b>${escapeHtml(w.card_id)}</b>${escapeHtml(w.title)}
            <button class="role-unwatch" data-cid="${escapeHtml(w.card_id)}" title="解除关注">✕</button>
          </span>`).join("")
      : `<span class="rej-empty">（还未关注任何工作卡）</span>`;
    const options = candidates
      .map(c => `<option value="${escapeHtml(c.card_id)}">${escapeHtml(c.card_id)} · ${escapeHtml(c.title)}</option>`)
      .join("");
    const signalOpts = ["turn_completed", "status_changed", "decision_appended", "turn_error"]
      .map(s => `<option value="${s}" ${signals.includes(s) ? "selected" : ""}>${s}</option>`).join("");
    const verdictHtml = verdicts.length
      ? verdicts.map(v => `<div class="role-v">
            <span class="role-vd ${v.decision === "speak" ? "speak" : "silent"}">${v.decision === "speak" ? "发言" : "沉默"}</span>
            <span class="role-vr">${escapeHtml(v.reason || "")}</span>
            <span class="role-vt">${escapeHtml(fmtRoleTs(v.ts))}</span>
            ${v.evidence && v.evidence.length ? `<div class="role-ve">证据：${v.evidence.map(escapeHtml).join("；")}</div>` : ""}
            ${v.superseded_by ? `<div class="role-ve">已被 ${escapeHtml(v.superseded_by)} 取代</div>` : ""}
          </div>`).join("")
      : `<div class="rej-empty">（还没有巡检记录）</div>`;
    const alarmHtml = alarms.length
      ? alarms.map(a => `<div class="role-alarm ${escapeHtml(a.severity || "warn")}">
            <div class="role-alarm-head"><b>${escapeHtml(a.title || "")}</b>
              <span>${escapeHtml(fmtRoleTs(a.ts))} · ${escapeHtml(a.status || "open")}</span>
              ${a.status === "open" ? `<button class="role-ack" data-id="${escapeHtml(a.id || "")}">已知</button>` : ""}
              ${a.status !== "corrected" ? `<button class="role-correct" data-id="${escapeHtml(a.id || "")}">更正</button>` : ""}
            </div>
            ${a.question ? `<div class="role-alarm-q">要你决定：${escapeHtml(a.question)}</div>` : ""}
            ${(a.options || []).length ? `<div class="role-ve">选项：${a.options.map(escapeHtml).join(" / ")}</div>` : ""}
            ${a.recommendation ? `<div class="role-ve">推荐：${escapeHtml(a.recommendation)}</div>` : ""}
            ${a.default_action ? `<div class="role-ve">你不回复，我按：${escapeHtml(a.default_action)}${a.deadline ? `（${escapeHtml(a.deadline)} 生效）` : ""}</div>` : ""}
            <div class="role-alarm-detail">${escapeHtml(a.detail || "")}</div>
            ${a.corrected_by ? `<div class="role-ve">已更正 → ${escapeHtml(a.corrected_by)}</div>` : ""}
          </div>`).join("")
      : `<div class="rej-empty">（没有警报）</div>`;
    const history = (charter.history || []).length;
    return `
      <div class="role-panel">
        <div class="role-head">
          <span class="role-badge">角色卡 · 离树观察者</span>
          <span class="role-duty ${escapeHtml(dutyState)}">${escapeHtml(roleDutyLabel(dutyState))}</span>
          <span class="role-pending">${pending ? pending + " 源待巡检" : "已同步"}</span>
        </div>
        <div class="role-actions">
          <button class="btn" id="roleCheckBtn" ${canRun ? "" : "disabled"}>立即巡检</button>
          <button class="btn ghost" id="roleEditBtn">编辑档案</button>
          ${dutyState === "active"
            ? `<button class="btn ghost" id="roleSuspendBtn">停职</button>`
            : dutyState === "suspended"
              ? `<button class="btn ghost" id="roleResumeBtn">复职</button>`
              : ""}
          ${dutyState !== "archived" ? `<button class="btn danger" id="roleArchiveBtn">撤岗</button>` : ""}
        </div>
        <div class="role-sub">最后巡检：${escapeHtml(duty.last_wake_at || "（从未）")}　·　职责书 v${escapeHtml(charter.version || 1)}（${history} 个版本）</div>

        <div class="role-sec">
          <div class="role-sec-title">管辖 · jurisdiction <span class="note">排他域 ${escapeHtml(jurisdiction.exclusive_key || "")}</span></div>
          <div class="role-domain">${escapeHtml(jurisdiction.domain || "（未设置）")}</div>
          <div class="role-statement">${escapeHtml(jurisdiction.statement || "（未写职责说明）")}</div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">关注 · watch <span class="note">${watched.length} 张工作卡 · 信号 ${signals.length}</span></div>
          <div class="role-watch-list">${watchHtml}</div>
          <div class="role-watch-add">
            <select id="roleWatchSel"><option value="">＋ 关注一张工作卡</option>${options}</select>
            <button class="btn ghost" id="roleWatchAdd">关注</button>
          </div>
          <div class="role-chips">${signals.map(s => `<span class="role-chip">${escapeHtml(s)}</span>`).join("") || "（未声明信号）"}</div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">发言契约 · speak contract</div>
          <div class="role-kv">建议：${escapeHtml((sc.advice_when || []).join("、") || "（未写）")}</div>
          <div class="role-kv">警报：${escapeHtml((sc.alarm_when || []).join("、") || "（未写）")}</div>
          <div class="role-kv">默认沉默 ${sc.silence_default === false ? "否" : "是"} · 单次最多 ${escapeHtml(sc.max_messages_per_wake || 2)} 条</div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">升级线 · escalation</div>
          <div class="role-kv">建议 → ${escapeHtml(esc.advice_to || "source_card")}</div>
          <div class="role-kv">警报 → ${escapeHtml(esc.alarm_to || "human")}　·　严重 → ${escapeHtml(esc.critical_to || "root_and_human")}</div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">判断簿 · verdicts <span class="note">发言与沉默都落账</span></div>
          <div class="role-verdicts">${verdictHtml}</div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">指令簿 · directives <span class="note">${openDirs} 条待验收</span></div>
          <div class="role-directives">
            ${directives.length ? directives.map(d => `<div class="role-dir ${escapeHtml(d.status || "issued")}">
              <div class="role-dir-head"><b>→ ${escapeHtml(d.target_card_id)}</b>
                <span class="role-dir-status">${escapeHtml(dirStatus[d.status] || d.status || "已下发")}</span>
                <span class="role-dir-ts">${escapeHtml(fmtRoleTs(d.created_at))}</span></div>
              <div class="role-dir-body">${escapeHtml(d.instruction || "")}</div>
              <div class="role-ve">验收：${escapeHtml(d.acceptance || "（未写）")}</div>
              ${d.evidence ? `<div class="role-ve">证据：${escapeHtml(d.evidence)}</div>` : ""}
              ${(d.status === "issued" || d.status === "rejected" || d.status === "overdue") ? `<div class="role-dir-actions">
                <button class="role-dir-verify" data-id="${escapeHtml(d.id)}" data-status="complied">达标</button>
                <button class="role-dir-verify" data-id="${escapeHtml(d.id)}" data-status="rejected">打回</button>
              </div>` : ""}
            </div>`).join("") : `<div class="rej-empty">（还没有指令）</div>`}
          </div>
          <div class="role-dir-new">
            <select id="roleDirectiveTarget" class="role-input">
              <option value="">选择已关注的工作卡</option>
              ${watched.map(w => `<option value="${escapeHtml(w.card_id)}">${escapeHtml(w.card_id)} · ${escapeHtml(w.title)}</option>`).join("")}
            </select>
            <input type="text" id="roleDirectiveInstruction" class="role-input" placeholder="指令：要它做什么（明确、可执行）">
            <input type="text" id="roleDirectiveAcceptance" class="role-input" placeholder="验收判据：怎么算做到">
            <input type="text" id="roleDirectiveDue" class="role-input" placeholder="期望完成时间（可选，ISO 时间）">
            <button class="btn" id="roleDirectiveIssue">下指令</button>
          </div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">警报簿 · alarms <span class="note">${alarms.filter(a => a.status === "open").length} 条未处理</span></div>
          <div class="role-alarms">${alarmHtml}</div>
        </div>

        <div class="role-sec">
          <div class="role-sec-title">职责书 · charter <span class="note">v${escapeHtml(charter.version || 1)} · ${escapeHtml(charter.ratified_by || "")}</span></div>
          <div class="role-charter">${escapeHtml(charter.text || "（空）")}</div>
        </div>

        <div class="role-edit" id="roleEditPanel" style="display:none">
          <div class="role-sec-title">编辑档案</div>
          <input type="text" id="roleJurDomain" class="role-input" placeholder="责任域排他键，如 timeline_contract" value="${escapeHtml(jurisdiction.domain || "")}">
          <input type="text" id="roleJurStatement" class="role-input" placeholder="一句话职责说明" value="${escapeHtml(jurisdiction.statement || "")}">
          <div class="role-kv">关注信号</div>
          <select id="roleSignalSel" class="role-input" multiple size="4">${signalOpts}</select>
          <div class="role-kv">发言契约</div>
          <input type="text" id="roleAdvice" class="role-input" placeholder="建议条件，逗号分隔" value="${escapeHtml((sc.advice_when || []).join(","))}">
          <input type="text" id="roleAlarm" class="role-input" placeholder="警报条件，逗号分隔" value="${escapeHtml((sc.alarm_when || []).join(","))}">
          <div class="role-create-row">
            <input type="number" id="roleMaxMsg" class="role-input" min="1" max="9" placeholder="单次最多几条" value="${escapeHtml(sc.max_messages_per_wake || 2)}">
            <label class="role-kv"><input type="checkbox" id="roleSilence" ${sc.silence_default === false ? "" : "checked"}> 默认沉默</label>
          </div>
          <div class="role-kv">升级线</div>
          <input type="text" id="roleEscAdvice" class="role-input" placeholder="建议发给谁" value="${escapeHtml(esc.advice_to || "source_card")}">
          <input type="text" id="roleEscAlarm" class="role-input" placeholder="警报发给谁" value="${escapeHtml(esc.alarm_to || "human")}">
          <input type="text" id="roleEscCritical" class="role-input" placeholder="严重风险发给谁" value="${escapeHtml(esc.critical_to || "root_and_human")}">
          <div class="role-kv">职责书（保存后生成新版本）</div>
          <textarea id="roleCharterEdit" class="role-input" rows="5">${escapeHtml(charter.text || "")}</textarea>
          <div class="role-actions"><button class="btn" id="roleSaveProfile">保存档案</button>
          <button class="btn ghost" id="roleCancelProfile">取消</button></div>
        </div>
      </div>`;
  }

  async function renderInspector() {
    const m = currentCard.meta;
    const b = currentCard.body;
    const isRoleCard = m.card_type === "role";
    let roleInfo = null;
    if (isRoleCard && m.card_id && m.card_id !== "draft") {
      try { roleInfo = await API.getRole(WORK_ID, m.card_id); } catch (e) { roleInfo = null; }
    }

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

    // [自动模式 2026-10-17 · 回灌 5700 块0.5] 卡详情 auto 行：开 ⇒ 徽标＋目标态＋
    // 轮次＋目标文本＋关闭钮；关 ⇒ 开启钮＋提示。非 auto 卡的渲染与改前逐字一致
    // （这块只在 auto_mode=1 时出现）。goalStateLabel 把机器态翻成中文标签。
    const isAutoCard = Number(m.auto_mode || 0) === 1;
    // [6702 移植 P8] 停在 paused/blocked 的卡给一条人工恢复入口：
    // 死锁兜底与空转兜底都会把卡停在 paused，而没有这条按钮就只能手改数据库。
    const canResume = isAutoCard
      && ["paused", "blocked"].includes(String(m.goal_state || "").trim());
    const autoRowHtml = isRoleCard ? "" : (isAutoCard
      ? `<div class="auto-row">
          <span class="auto-badge">自动模式</span>
          <span class="auto-meta">${escapeHtml(goalStateLabel(m.goal_state))} · 已跑 ${Number(m.goal_rounds || 0)} 轮</span>
          ${canResume ? `<button class="btn ghost auto-resume" id="autoResume" title="重新拉起：清等待位与活锁计数，立刻续跑">恢复自动推进</button>` : ""}
          <button class="btn ghost auto-toggle" id="autoToggle" title="关闭后：停自动继续、按类型还原权限">关闭自动</button>
        </div>
        <div class="auto-goal"><span class="auto-goal-k">目标</span>${escapeHtml((m.goal || "").trim() || "（尚未设定——本卡收到的首条消息即成为目标）")}</div>`
      : `<div class="auto-row">
          <button class="btn ghost auto-toggle" id="autoToggle" title="开了就权限全开、自动续跑，直到模型声明完成">开启自动模式</button>
          <span class="auto-hint">开了就权限全开，模型自己一轮接一轮跑到声明完成</span>
        </div>`);

    inspBody.innerHTML = `
      <div class="ws-title" id="wsTitle">${escapeHtml(m.title)}</div>
      <div style="margin:8px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span class="type-badge" data-type="${escapeHtml(m.card_type)}">${escapeHtml(m.card_type)}卡</span>
        <span style="font-size:11px;color:var(--dimmer);margin-left:auto;">${escapeHtml(m.card_id)} · ${escapeHtml(m.source)}</span>
      </div>
      ${autoRowHtml}
      ${isRoleCard ? buildRoleHtml(roleInfo, m) : ""}
      ${m.card_type === "project" ? `
      <div class="blocktitle">角色（参谋线）<span class="note">离树观察者 · 组阁权在根卡</span></div>
      <div class="role-create">
        <input type="text" id="roleTitle" placeholder="角色名，如「一致性观察员」">
        <div class="role-create-row">
          <input type="text" id="roleDomain" placeholder="责任域排他键，如 timeline_contract">
          <input type="text" id="roleStatement" placeholder="一句话职责说明">
        </div>
        <textarea id="roleCharter" rows="3" placeholder="职责书：盯什么、什么情况必须说话、什么情况直接报你"></textarea>
        <button class="btn" id="roleCreateBtn">组阁</button>
      </div>` : ""}

      <div id="genericBlocks" style="${isRoleCard ? "display:none" : ""}">
      <div class="blocktitle">使命 · mission <span class="note">${isRoleCard ? "根卡组阁时写入 · 可编辑" : "由上级卡对话创建 · 可编辑"}</span></div>
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
      </div>

      <div id="roleRelations" ${isRoleCard ? 'style="display:none"' : ""}>
      <div class="blocktitle">关系</div>
      ${linksHtml}
      ${relAddHtml}
      </div>

      <div class="blocktitle">修改历史（${historyCount} 条）</div>
      <div class="history-block">
        <div class="history-toggle" id="historyToggle">▸ 展开/折叠</div>
        <div class="history-list" id="historyList">
          ${history.slice().reverse().map(h => `<div class="h-item">${escapeHtml(h)}</div>`).join("")}
        </div>
      </div>

      ${!isRoleCard ? `<div class="blocktitle">＋ 建子卡</div>
      <div class="add-child-row">
        <input type="text" id="childTitle" placeholder="子卡标题">
        <!-- [底座收口 T5.c] 类型选择移除：card_type 传空串，由后端按本树
             project_class 映射（rd→sub_project / research→topic）——「线索」
             已退役，前端不再硬编码类型菜单（D-06 的下拉就此退役）。 -->
        <button class="btn" id="addChildBtn">建</button>
      </div>` : ""}

      ${m.card_type !== "project" && !isRoleCard ? `
      <div class="blocktitle" style="color:#d05656;">危险操作</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:24px;flex-wrap:wrap;">
        <button class="btn" id="deleteCardBtn" style="color:#d05656;border-color:#d05656;">删除此卡</button>
        <span style="font-size:11px;color:var(--dimmer);">连同会话删除；有子卡时系统拒绝；剧本卡无此操作</span>
      </div>` : ""}
    `;

    bindInspectorEvents();
    bindRolePanel();
    // [自动模式 2026-10-17 · 回灌 5700 块0.5] 每次重渲同步底边栏开关状态
    // （开/关 auto 后 renderInspector 会走到这里——一步到位，不用调用方惦记）。
    renderAutoToggle();
    // [I2] 恢复结论区编辑态（若有草稿）：重渲不丢未保存内容
    applyConclusionState();
    // [phase8.6-C] 面板已可见时（如保存后重渲染）立即撑高；首次 open 由 active 后统一触发
    autoGrowAll();
  }

  /* [phase8.6-C] 文本区高度自适应：内容多高就多高，上限由 CSS max-height 兜底。
     [渲染层 2026-10-21] 原来每个元素各来一遍「写 height:auto → 读 scrollHeight →
     写 height」，而它被 renderInspector / 每次 input 事件随时调用 ⇒ 读写交替，每次
     读都强制一次同步布局（实测单次 190ms，输入时肉眼可见卡顿）。
     改成**帧内批处理**：同一帧里先全部写 auto、再统一读、再统一写 ⇒ 一轮只有一次
     强制布局；并被 rAF 合并（连续输入不会叠加成 N 次布局）。 */
  const _autoGrowPending = new Set();
  let _autoGrowRaf = 0;
  function autoGrowEl(el) {
    if (!el) return;
    _autoGrowPending.add(el);
    if (_autoGrowRaf) return;
    const run = () => {
      _autoGrowRaf = 0;
      const els = Array.from(_autoGrowPending);
      _autoGrowPending.clear();
      for (const e of els) e.style.height = "auto";               // 批写
      const hs = els.map((e) => e.scrollHeight);                  // 批读（一次布局）
      els.forEach((e, i) => { e.style.height = hs[i] + "px"; });  // 批写
    };
    _autoGrowRaf = (typeof requestAnimationFrame === "function")
      ? requestAnimationFrame(run)
      : setTimeout(run, 0);
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
    // [自动模式 2026-10-17 · 回灌 5700 块0.5] 自动模式开关（U2：任何卡、随时可切）。
    // 右栏卡详情里的 #autoToggle 与底边栏 #autoToggleBtn 共用 toggleAutoMode。
    const autoBtn = document.getElementById("autoToggle");
    if (autoBtn) autoBtn.onclick = () => toggleAutoMode(autoBtn);
    // [6702 移植 P8] 人工恢复：清等待位与活锁计数后立刻续跑。
    const resumeBtn = document.getElementById("autoResume");
    if (resumeBtn) resumeBtn.onclick = () => resumeAuto(resumeBtn);
    const autoBtnBottom = document.getElementById("autoToggleBtn");
    if (autoBtnBottom) autoBtnBottom.onclick = () => toggleAutoMode(autoBtnBottom);

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
          await ensurePersisted(v);   // [D3] 改名即数据动作：草稿态先落库——**用用户起的名字**建项目，再写真身
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

    // 建子卡（角色卡不显示该区 ⇒ 元素不存在，必须空守卫）
    // [惰性新建 2026-10-17] **先出画面**：提交标题立刻切到这张新卡的对话（标题已在手），
    // 落库后台立刻发起、不等它返回——用户零等待。失败则 toast，标题留在卡上可重试。
    const addChildBtn = document.getElementById("addChildBtn");
    if (addChildBtn) {
      addChildBtn.onclick = () => {
        const title = document.getElementById("childTitle").value.trim();
        if (!title) { API.toast("请输入子卡标题", "error"); return; }
        document.getElementById("childTitle").value = "";
        // card_type 传空串 → 后端按树映射（底座收口 T5.c）
        enterNewCard({ title, parent: currentCard.meta.card_id, card_type: "" });
      };
    }
    const childTitleEl = document.getElementById("childTitle");
    if (childTitleEl) {
      childTitleEl.onkeydown = (e) => {
        if (e.key === "Enter") document.getElementById("addChildBtn").click();
      };
    }

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

  // ==================== [角色机制 5710] 角色面板交互 ====================
  function bindRolePanel() {
    const refresh = () => { if (currentCard) renderInspector().catch(() => {}); };
    const checkBtn = document.getElementById("roleCheckBtn");
    if (checkBtn) {
      checkBtn.onclick = async () => {
        if (!currentCard) return;
        const cid = currentCard.meta.card_id;
        checkBtn.disabled = true;
        try {
          const r = await API.checkRole(WORK_ID, cid);
          notify(r.started ? "巡检已启动，回合结束后台账自动刷新" : "没有新同步，未起回合",
                 r.started ? "success" : "info");
          for (let i = 1; i <= 10; i += 1) {
            setTimeout(() => { if (currentCard && currentCard.meta.card_id === cid) refresh(); }, i * 1200);
          }
        } catch (e) {
          checkBtn.disabled = false;
        }
      };
    }
    const addBtn = document.getElementById("roleWatchAdd");
    if (addBtn) {
      addBtn.onclick = async () => {
        if (!currentCard) return;
        const sel = document.getElementById("roleWatchSel");
        const cid = sel ? sel.value : "";
        if (!cid) { notify("先选一张工作卡", "warn"); return; }
        try {
          await API.watchRole(WORK_ID, currentCard.meta.card_id, [cid], "add");
          notify("已建立关注连线", "success");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    }
    const editBtn = document.getElementById("roleEditBtn");
    const editPanel = document.getElementById("roleEditPanel");
    if (editBtn && editPanel) {
      editBtn.onclick = () => {
        editPanel.style.display = editPanel.style.display === "none" ? "block" : "none";
      };
    }
    const cancelEdit = document.getElementById("roleCancelProfile");
    if (cancelEdit && editPanel) {
      cancelEdit.onclick = () => { editPanel.style.display = "none"; };
    }
    const saveProfile = document.getElementById("roleSaveProfile");
    if (saveProfile) {
      saveProfile.onclick = async () => {
        if (!currentCard) return;
        const domain = (document.getElementById("roleJurDomain") || {}).value || "";
        const statement = (document.getElementById("roleJurStatement") || {}).value || "";
        const signalSel = document.getElementById("roleSignalSel");
        const signals = signalSel ? Array.from(signalSel.selectedOptions).map(o => o.value) : [];
        const charter = (document.getElementById("roleCharterEdit") || {}).value || "";
        const splitCsv = (id) => ((document.getElementById(id) || {}).value || "")
          .split(/[,，]/).map(x => x.trim()).filter(Boolean);
        const advice = splitCsv("roleAdvice");
        const alarmWhen = splitCsv("roleAlarm");
        const maxMsg = parseInt((document.getElementById("roleMaxMsg") || {}).value || "2", 10) || 2;
        const silence = !!(document.getElementById("roleSilence") || {}).checked;
        const escAdvice = (document.getElementById("roleEscAdvice") || {}).value || "source_card";
        const escAlarm = (document.getElementById("roleEscAlarm") || {}).value || "human";
        const escCritical = (document.getElementById("roleEscCritical") || {}).value || "root_and_human";
        try {
          await API.updateRole(WORK_ID, currentCard.meta.card_id, {
            jurisdiction: { domain: domain.trim(), statement: statement.trim() },
            signals, charter,
            speak_contract: {
              advice_when: advice.length ? advice : ["gap_or_risk"],
              alarm_when: alarmWhen.length ? alarmWhen : ["mutually_exclusive_fact"],
              max_messages_per_wake: Math.max(1, Math.min(9, maxMsg)),
              silence_default: silence,
            },
            escalation: {
              advice_to: escAdvice.trim() || "source_card",
              alarm_to: escAlarm.trim() || "human",
              critical_to: escCritical.trim() || "root_and_human",
            },
          });
          notify("角色档案已更新", "success");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    }
    const suspendBtn = document.getElementById("roleSuspendBtn");
    if (suspendBtn) {
      suspendBtn.onclick = async () => {
        if (!currentCard) return;
        try {
          await API.setRoleDuty(WORK_ID, currentCard.meta.card_id, "suspended");
          notify("角色已停职：不再同步、不再巡检", "info");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    }
    const resumeBtn = document.getElementById("roleResumeBtn");
    if (resumeBtn) {
      resumeBtn.onclick = async () => {
        if (!currentCard) return;
        try {
          await API.setRoleDuty(WORK_ID, currentCard.meta.card_id, "active");
          notify("角色已复职", "success");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    }
    const archiveBtn = document.getElementById("roleArchiveBtn");
    if (archiveBtn) {
      archiveBtn.onclick = async () => {
        if (!currentCard) return;
        const title = currentCard.meta.title || currentCard.meta.card_id;
        if (!window.confirm(`撤岗《${title}》？档案与判断/警报台账保留，只解除关注连线。`)) return;
        try {
          await API.setRoleDuty(WORK_ID, currentCard.meta.card_id, "archived");
          notify("角色已撤岗（台账保留）", "info");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    }
    // 根卡组阁：标题 + 管辖域 + 职责书 → 建一张离树角色卡
    const createBtn = document.getElementById("roleCreateBtn");
    if (createBtn) {
      createBtn.onclick = async () => {
        if (!currentCard) return;
        const title = (document.getElementById("roleTitle") || {}).value || "";
        const charter = (document.getElementById("roleCharter") || {}).value || "";
        const domain = (document.getElementById("roleDomain") || {}).value || "";
        const statement = (document.getElementById("roleStatement") || {}).value || "";
        if (!title.trim() || !charter.trim()) {
          notify("角色名与职责书都要写", "warn");
          return;
        }
        createBtn.disabled = true;
        try {
          const r = await API.createRole(WORK_ID, {
            title: title.trim(), charter: charter.trim(),
            jurisdiction: { domain: domain.trim() || title.trim(), statement: statement.trim() },
            watch_card_ids: [],
          });
          notify("角色卡已创建：" + (r.role_card_id || ""), "success");
          await renderNav();
          await open(r.role_card_id);
        } catch (e) {
          createBtn.disabled = false;
        }
      };
    }
    const dirIssue = document.getElementById("roleDirectiveIssue");
    if (dirIssue) {
      dirIssue.onclick = async () => {
        if (!currentCard) return;
        const target = (document.getElementById("roleDirectiveTarget") || {}).value || "";
        const instruction = (document.getElementById("roleDirectiveInstruction") || {}).value || "";
        const acceptance = (document.getElementById("roleDirectiveAcceptance") || {}).value || "";
        const due = (document.getElementById("roleDirectiveDue") || {}).value || "";
        if (!target || !instruction.trim() || !acceptance.trim()) {
          notify("目标、指令、验收判据都要写", "warn");
          return;
        }
        dirIssue.disabled = true;
        try {
          const r = await API.issueDirective(WORK_ID, currentCard.meta.card_id, {
            target_card_id: target, instruction: instruction.trim(),
            acceptance: acceptance.trim(), due_at: due.trim(),
          });
          notify("指令已下发：" + (r.directive_id || ""), "success");
          await renderInspector();
        } catch (e) {
          dirIssue.disabled = false;
        }
      };
    }
    document.querySelectorAll(".role-dir-verify").forEach((btn) => {
      btn.onclick = async () => {
        if (!currentCard) return;
        const did = btn.dataset.id || "";
        const status = btn.dataset.status || "";
        let evidence = "";
        if (status === "rejected") {
          evidence = window.prompt("打回：差在哪？") || "";
          if (!evidence.trim()) return;
        }
        try {
          await API.verifyDirective(WORK_ID, currentCard.meta.card_id, did, status, evidence);
          notify(status === "complied" ? "已验收达标" : "已打回", status === "complied" ? "success" : "info");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    });
    document.querySelectorAll(".role-unwatch").forEach((btn) => {
      btn.onclick = async () => {
        if (!currentCard) return;
        const cid = btn.dataset.cid || "";
        try {
          await API.unwatchRole(WORK_ID, currentCard.meta.card_id, cid);
          notify("已解除关注", "info");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    });
    document.querySelectorAll(".role-ack").forEach((btn) => {
      btn.onclick = async () => {
        if (!currentCard) return;
        try {
          await API.ackAlarm(WORK_ID, btn.dataset.id || "");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    });
    document.querySelectorAll(".role-correct").forEach((btn) => {
      btn.onclick = async () => {
        if (!currentCard) return;
        try {
          await API.correctAlarm(WORK_ID, btn.dataset.id || "");
          notify("警报已更正", "info");
          await renderInspector();
        } catch (e) { /* toast 已处理 */ }
      };
    });
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
    /* [对话界面商业化 2026-10-17] 模型名取自**节点顶层**。
       [5715 · 纠偏] 原注释写「落盘记录的 model 在顶层（data 里没有）」——**说反了**：
       落盘记录的模型名在 **data.model**（实测 timeline：assistant_text 事件
       `data.model = "deepseek-v4-pro"`），顶层没有 model 键；是装配器的
       `modelOf(rec, data)`（rec.model 优先、再 data.model）把它**算**到节点顶层的
       `node.model` 上。所以这里取 `node.model` 就是取 data.model，
       后面的 `d.model` 只是冗余兜底。
       ⚠ 照着原注释去改「顶层落盘形状」会改错地方——**真正的数据源是 data.model**。
       另注：2026-10-04/05 的老记录**当时**就没传 model（调用方未传），
       故老消息只有时间、没有模型徽标；后端 `emit_message_event` 早已支持 model
       （会话渲染单源化·偏差③补 2026-10-15），新消息正常带。 */
    const badge = assistantMeta(node.model || d.model, d.behavior, d.end, node.ts);
    /* [5702 · C-1] 流式节点正文在 data.text（累积），落盘节点在 data.content；
       定稿后两者并存（content 优先）。对应 dsh assistant-step 的 blocks 语义。 */
    const bodyText = (d.content != null && String(d.content) !== "")
      ? String(d.content)
      : String(d.text || "");
    el.innerHTML = badge +
      /* [5702 · C-1] 只有思考、没有正文的轮（工具调用前的 think-only step）
         不渲染空正文块——否则每轮多一个空气泡。dsh 的 step 视图同理。 */
      (bodyText ? '<div class="md">' + renderMarkdown(bodyText) + "</div>" : "");

    /* [对话界面商业化 2026-10-17] 消息级操作条：hover 浮出（CSS 控显隐），
       动作只做「复制全文」——重新生成只对最后一轮成立（resend 复用最后一条
       user_message 的 pair_id），放在历史消息上会生成到错误的轮次，故不放。
       时间来自事件 ts（落盘/实时同源），无 ts 的旧记录不显示时间、不补假值。 */
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    // [5715 · 抽模块] 复制按钮搬进 esm/flow/message-actions.js（复制实现仍用本文件
    // 自己的 copyText ⇒ 行为逐字不变；只是按钮的构造与反馈可被单测驱动了）。
    const copyBtn = createCopyButton({
      title: "复制这条回复",
      okTitle: "已复制",
      act: "copy",
      getText: () => {
        const body = el.querySelector(".md");
        return body ? body.innerText : "";
      },
      copy: (text) => copyText(text),
    });
    actions.appendChild(copyBtn);
    el.appendChild(actions);
    return el;
  }

  /* [对话界面商业化 2026-10-17] 空态内容按卡类型给：
     同一张空卡，对不同类型说不同的话；建议项是**真实可用的提问**，
     点一下只填进输入框（不代发）——用户仍可改写，不制造「我替你决定了」的越权感。 */
  const EMPTY_COPY = {
    project: {
      desc: "这是整个项目的总架构卡。说说项目要达成什么、边界在哪——后续拆解都从这里长出来。",
      hints: ["这个项目要解决什么？", "理一遍边界与不做什么", "把已有结论整理成现状"],
    },
    sub_project: {
      desc: "这是一张工作单元卡。可以直接派活，也可以先对齐它负责的范围与交付物。",
      hints: ["这张卡负责什么？", "现在卡在哪一步？", "把已有进展整理成现状"],
    },
    topic: {
      desc: "这是一个课题卡。可以先给问题、给材料，或让它先调研再收敛。",
      hints: ["这个课题要回答什么？", "列出需要查证的方向", "把已有材料整理成结论"],
    },
    tool: {
      desc: "这是一张工具卡，按剧本分步执行。可以直接说明这次要它做什么。",
      hints: ["这次要处理什么、产出什么？", "按剧本从头走一遍"],
    },
    coder: {
      desc: "这是一张编程卡，承接一个开发任务，自报完工即终态。",
      hints: ["要改什么、验收标准是什么？", "先读代码再给方案"],
    },
    rule: {
      desc: "这是一张规则卡，正文按卡类型分区注入上下文。模型侧只读，改动走根卡提议＋审批。",
      hints: ["现在各类型的规则分别写了什么？"],
    },
  };

  /* [5715 · 修] 新建项目页（/work/draft）的空态文案。
     原状：零消息 ⇒ 整屏空白（只有输入框），新用户第一眼看到的是"什么都没有"。
     这张卡是项目根卡的落地面，说的应该是"从这儿开始"，不是内部术语。 */
  const DRAFT_PROJECT_COPY = {
    desc: "先说清楚这个项目要达成什么、边界在哪。系统会据此拆出卡片树，每一步的结论都沉淀成可回溯的卡片。",
    hints: ["这个项目要解决什么问题？", "拆成哪几个子项目并行推进", "先把已有材料整理成现状"],
  };

  function applyEmptyState(card, viewKey) {
    const meta = (card && card.meta) || {};
    const type = String(meta.card_type || "");
    const title = String(meta.title || "").trim();
    const copy = EMPTY_COPY[type] || {
      desc: "在下面输入框说点什么，这张卡的对话会从这里开始。",
      hints: ["先介绍这张卡的情况", "把已有结论整理成现状"],
    };
    /* [5715 · 修] 草稿态的视图键与 meta.card_id 不同（草稿卡用 "new-N" 画板键），
       允许显式传视图键，否则空态会写到别人的画板上。 */
    EventFlow.setEmpty(WORK_ID, String(viewKey || meta.card_id || ""), {
      title: title ? title : "还没有对话",
      desc: copy.desc,
      hints: copy.hints.map((h) => ({ label: h, text: h })),
    });
  }

  /** 剪贴板：优先异步 API，失败回退 execCommand（http/旧浏览器兜底）。 */  function copyText(text) {
    const fallback = () => new Promise((resolve) => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* 静默 */ }
      document.body.removeChild(ta);
      resolve();
    });
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(fallback);
    }
    return fallback();
  }

  /** 时间文案：只给 HH:MM。
      日期由消息流里的「日期分隔线」统一承担（同一天只在首个节点出现一次），
      每条消息再带一遍日期就是重复信息——实测一屏里会出现三个「10月4日」。 */
  function fmtMsgTime(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
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
  function assistantMeta(model, behavior, end, ts) {
    const parts = [];
    if (model) parts.push(`<span class="model-badge">${escapeHtml(model)}</span>`);
    if (behavior) parts.push(`<span class="beh" data-beh="${escapeHtml(behavior)}">${escapeHtml(behavior)}</span>`);
    /* 结束原因只在「非正常结束」时显示：model_done 是绝大多数回合的默认值，
       每条都挂一个 end:model_done 等于给正常状态贴标签（纯噪音）。
       人工停止 / 判不了交人 / 出错，才是有信息量的三种。 */
    if (end && end !== "model_done") {
      const zh = end === "error" ? "出错" : end === "human_stop" ? "已停止" : end === "policy_stop" ? "转人工" : String(end);
      parts.push(`<span class="end${end === "error" ? " end-error" : ""}" data-end="${escapeHtml(end)}">${escapeHtml(zh)}</span>`);
    }
    // [对话界面商业化 2026-10-17] 时间放最后、最弱：它是辅助信息，不该抢标题。
    const t = fmtMsgTime(ts);
    if (t) parts.push(`<span class="mtime" title="${escapeHtml(String(ts))}">${escapeHtml(t)}</span>`);
    return parts.length ? `<div class="meta">${parts.join("")}</div>` : "";
  }

  // [迭代四 F4⑥] 卡间消息渲染（Ⅰ 类 ×3）：.cmsg 结构＋头行剥离；解析失败显示
  // 「消息格式异常」标记＋原文全文照示（不静默丢弃，卡间册 §4.2 解析失败处置①）。
  // [对话流全消息类型 2026-10-13] 形态改 .cmsg（灰阶竖条＋类型标签＋方向行）。
  // 显示名与「须回应」镜像自 config/message_type_registry.json（后端 display_of 的同表；
  // 本路径无注册表端点，故前端持一份最小镜像，未知类型回落显示 code 本身＝fail-open）。
  const CARD_MSG_DISPLAY = { msg_ask: "询问", msg_answer: "应答", msg_notify: "通知" };

  /** 卡间消息节点（注入 stream.js）：返回元素，**不自行 append**——
      位置由快照决定（C2）。role 形如 "card:card_007"（前 5 字符是来源前缀）。
      [5715 · 抽模块] 渲染搬进 esm/flow/card-msg-node.js（原先在装配闭包里 ⇒
      导不出来、这条产品面没有断言保护）；这里只留「去向标签」的取法。 */
  const renderCardMsgNode = createCardMsgRenderer({
    getToLabel: () => (currentCard && currentCard.meta && currentCard.meta.card_id) || "本卡",
  });

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
      if (EventFlow) EventFlow.start(WORK_ID, cardId, null, { replayTokens: true });
      return true;
    } catch (e) {
      return false;   // 查询失败静默：不影响页面其余功能
    }
  }

  /* ═══ [断流兜底 2026-10-21] ═══
     运行态的权威来源是事件流（见 setRunState 上方注释）。但事件流本身会断：
     代理超时、网络抖动、后端重启——这时 done/turn_end 帧收不到，前端就永远停在
     「运行中」，只能刷新。这是**离线恢复**，不是给状态机打补丁：
       · 只在「前端认为在跑」且「事件流已断」时才探活（流健康时零额外请求）；
       · 后端 /progress 的 active 位是权威判据；
       · 连续两次判「不在跑」才收口——自动续轮两轮之间有毫秒级空窗，一次不足为凭。 */
  let _idleProbeHits = 0;
  let _idleProbeInFlight = false;
  setInterval(async () => {
    if (!sessionState.running) { _idleProbeHits = 0; return; }
    const cid = currentCard ? currentCard.meta.card_id : "";
    if (!cid) return;
    try { if (!EventFlow.streamDead(WORK_ID, cid)) { _idleProbeHits = 0; return; } }
    catch (e) { /* 查不到断流状态就照常探一次 */ }
    if (_idleProbeInFlight) return;
    _idleProbeInFlight = true;
    let st = null;
    try { st = await API.getProgress(WORK_ID, cid, { silent: true }); }
    catch (e) { st = null; }
    finally { _idleProbeInFlight = false; }
    if (!st) { _idleProbeHits = 0; return; }
    const alive = (typeof st.active === "boolean")
      ? st.active
      : ACTIVE_STAGES.includes(st.stage);
    if (alive) { _idleProbeHits = 0; return; }
    _idleProbeHits += 1;
    if (_idleProbeHits < 2) return;
    _idleProbeHits = 0;
    if (!sessionState.running) return;
    if (!currentCard || currentCard.meta.card_id !== cid) return;
    setRunState(false, cid);
    try { EventFlow.stop(WORK_ID, cid); } catch (e) { /* 零阻断 */ }
  }, 3000);

  // [更新现状按钮改造 2026-10-16] `op` 缺省＝普通发送（逐字不变）；传 "update_status"
  // ⇒「更新现状」专用回合：本轮用户消息＝按钮名（可见、留痕，不另编话术），
  // 完整指令走后端轮首注入；收口时多扫一次 propose_status 开对比窗。
  async function send(op) {
    const statusOp = (op === "update_status");
    if (!currentCard) return;
    if (sessionState.running) return;   // 一轮在跑时不重入（运行中主键＝停止，见 doStop）
    /* [5702] 捕获**发起这次发送**的输入会话：切卡后迟到的 commitSent/drain
       不能作用到新卡（否则会清掉新卡草稿、或替新卡发队列）。 */
    const sendingMachine = inputBar ? inputBar.machine : null;
    const sendingQueue = queueStore;
    const content = statusOp ? "更新现状" : String(inputBar ? inputBar.getDraft() : "").trim();
    if (!content) return;

    /* [建项目流程 2026-10-17] 草稿首条消息：
         ① 落库（ensurePersisted：建项目/卡；带锁、失败拒绝，不半建）
         ② 显示板换绑到真键——在 ensurePersisted 内、身份变更那一刻完成
            （见该函数「★ 显示板就在身份变更这一刻换绑」注释：放这里会漏掉
            「落库先于发送完成」的建卡时序，板停在 new-N，气泡写进游离板）
         ③ 下面就是原有发送路径，一字不动
       失败：toast、保持草稿态、输入框**不清空**（现场保留）、可重试。 */
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
    if (!statusOp && inputBar) inputBar.commitSent(sendingMachine);

    // 本轮回复基线：异常恢复判据＝「resync 后是否多出一条 assistant」，不是「有没有 assistant」
    const asstBefore = (() => {
      try { return EventFlow.assistantSeq(WORK_ID, cardId); } catch (e) { return 0; }
    })();

    setRunState(true);   // 运行条起、主键转停止键、输入条照常可打字

    try {
      const resp = await API.postMessage(WORK_ID, cardId, content, op);
      // [对话界面商业化 2026-10-17] 流与整轮同寿：POST 是同步的（等整轮结束），
      // 2.5s 就收流＝把中间过程全丢在自己手里，只剩事后一次性全量上屏。
      // 改为 resync 对账之后再收（见下方）。
      // [C1 单源] 回复正文走 timeline 回流（与刷新同一条渲染路）
      await EventFlow.resync(WORK_ID, cardId);
      // 对账后再收流：resync 置 settled，此后的残帧不入装配。留 1.5s 让 done 帧到齐。
      setTimeout(() => EventFlow.stop(WORK_ID, cardId), 1500);
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
      if (await recoverIfDone(cardId, asstBefore)) return;
      // [发送失败] 草稿保住（不丢字）＋ 红条；未被认领的本地气泡撤下（不留幽灵消息）
      if (!statusOp && inputBar) {
        inputBar.setDraft(content);
        /* 输入框级提示只说「状态＋草稿还在」，具体原因与重发入口交给消息区的错误条——
           两处都写一遍原因＝同一个错误报两次，用户只会觉得吵。 */
        inputBar.failSend("发送失败，内容已保留");
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
      /* [运行态单一真相源 2026-10-21] 收口**以事件流为准**：POST 同步返回后，
         自动续轮/卡间调起可能已经在跑（run_start 帧先到、setRunState(true)）。
         这里若无脑置 false，就会把刚开始的下一轮运行态抹掉——正是「自动模式
         页面状态乱跳」的来源。stillBusy 读的就是真相源，只有它确实不在跑才收。 */
      let stillBusy = false;
      try { stillBusy = EventFlow.sessionRunning(WORK_ID, cardId); } catch (e) { /* 零阻断 */ }
      if (!stillBusy) setRunState(false, cardId);
      /* [5702] 只给「还在看的这张卡」出队：切走了就不替别的卡发消息
         （队列留在那张卡自己的 store 里，切回时仍在）。 */
      if (!currentCard || currentCard.meta.card_id === cardId) drainQueue(sendingQueue);
    }
  }

  /** 异常路径的兜底探测：后端可能已完成、只是响应没送回来。
      在跑（且流没断）⇒ 什么都不做；否则按 timeline 结算一次。
      返回 true＝已结算（调用方不再报错）。 */
  async function recoverIfDone(cardId, asstBefore) {
    try {
      const p = await API.getProgress(WORK_ID, cardId);
      const alive = p && ((typeof p.active === "boolean")
        ? p.active
        : ACTIVE_STAGES.includes(p.stage));
      if (alive && !EventFlow.streamDead(WORK_ID, cardId)) return false;
    } catch (e0) { return false; }
    try {
      const r = await EventFlow.resync(WORK_ID, cardId);
      // 只有「本轮确实多了一条 assistant」才算后端已完成；否则一律按发送失败处理
      // （保住草稿、出错误条、撤乐观气泡）——历史卡不会再把失败吞成成功。
      const grew = (() => {
        try { return EventFlow.assistantSeq(WORK_ID, cardId) > Number(asstBefore || 0); }
        catch (e) { return false; }
      })();
      if (r && r.ok && r.done && grew) {
        setTimeout(() => EventFlow.stop(WORK_ID, cardId), 1500);
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
  function drainQueue(qs) {
    const q = qs || queueStore;
    if (!q || sessionState.running) return;
    if (!currentCard) return;
    const next = q.dequeue();
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
      // [对话界面商业化 2026-10-17] 同 send()：流跟整轮同寿（POST 同步返回），
      // 2.5s 就收流＝中间过程全丢，只剩事后一次性全量上屏。对账之后再收。
      await EventFlow.resync(WORK_ID, cardId);
      setTimeout(() => EventFlow.stop(WORK_ID, cardId), 1500);
      if (window.__maybeOpenRuleEditModal) window.__maybeOpenRuleEditModal(cardId);
    } catch (e) {
      const err = document.createElement("div");
      err.className = "sys-error";
      err.innerHTML = `重发失败：${escapeHtml(e.message || "")} <button class="retry-btn">再试</button>`;
      ensureMsgs(cardId).appendChild(err);
      err.querySelector(".retry-btn").onclick = () => { err.remove(); resend(content); };
    } finally {
      // [运行态单一真相源 2026-10-21] 同 send()：以事件流真相为准，别抹掉已起跑的下一轮
      let stillBusy = false;
      try { stillBusy = EventFlow.sessionRunning(WORK_ID, cardId); } catch (e) { /* 零阻断 */ }
      if (!stillBusy) setRunState(false, cardId);
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
      /* 先置过渡态再发请求：中止缝在 step 边界，从「点下」到「真正停住」
         有一段真实等待，这段必须表达出来（否则用户会重复点、以为坏了）。 */
      try { EventFlow.setStopping(WORK_ID, cardId, true); } catch (e0) { /* 零阻断 */ }
      await postStop(WORK_ID, cardId);
      notify("正在停止…", "info");
      // 兜底：8 秒仍未收口则撤掉过渡态（避免卡在「正在停止」）
      setTimeout(() => {
        try { EventFlow.setStopping(WORK_ID, cardId, false); } catch (e0) { /* 零阻断 */ }
      }, 8000);
    } catch (e) {
      try { EventFlow.setStopping(WORK_ID, cardId, false); } catch (e0) { /* 零阻断 */ }
      notify("停止指令失败", "error");
    }
  }

  /* ═══ [对话流照抄 dsh 完全版 v3] 输入系统装配 ═══
   * 顺序与 dsh 一致：QueueDock 在输入卡**上方**（QueueDock.module.css:19-25 的
   * 「tuck 3px under the input card」），输入卡在座位底部。
   * 提交去向（原文 default-sink 的 shell 侧）：
   *   · 不忙 ⇒ 直接发（send()）
   *   · 忙 ⇒ 入队（QueueDock 显示；轮结束由 drainQueue 依序发出）
   *   · 忙且 steer ⇒ **真插话**（[5715] 后端已开轮内投递窗口，见 steerToRunning）
   */

  /* [5715] 插话投递：把消息送进**正在跑**的这一轮。
   *   投递成功 ⇒ 消息已经在轮里了（从队列摘掉）；投递不上（这一轮刚收口）
   *   ⇒ 退回队列，轮结束后照常发出——**不丢消息，也不假装成功**。 */
  function steerToRunning(text, queuedRows) {
    const cid = (currentCard && currentCard.meta && currentCard.meta.card_id) || "";
    if (!cid) { notify("当前没有打开的卡", "warn"); return; }
    const fallback = () => {
      if (!queuedRows) queueStore.enqueue(text);
      if (inputBar) inputBar.commitSent(inputBar.machine);
      if (queueDock) queueDock.refresh();
    };
    API.postSteer(WORK_ID, cid, text).then((res) => {
      if (res && res.applied) {
        if (queuedRows) {
          queuedRows.forEach((r) => { queueStore.updateQueue(r.id, { kind: "remove" }); });
        } else if (inputBar) {
          inputBar.commitSent(inputBar.machine);
        }
        if (queueDock) queueDock.refresh();
        notify("已插话：这一轮模型就能看到", "success");
      } else {
        fallback();
        notify("这一轮刚结束，已改为排队发送", "info");
      }
    }).catch(() => {
      fallback();
      notify("插话投递失败，已改为排队发送", "warn");
    });
  }

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
      /* [5715] 队列行上的「插话发送」＝真投递（后端轮内投递窗口）。
         投递成功后由 steerToRunning 把这条从队列摘掉。 */
      onSteer: (row) => {
        const text = String((row && row.text) || "");
        if (!text) { notify("这条消息没有内容", "warn"); return; }
        steerToRunning(text, [row]);
      },
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
            /* [5715] 真插话：mode==="steer" 时把这条**投递进正在跑的这一轮**
               （后端 steer 通道：下一次模型调用即可见），而不是干等整轮收口。
               此前这里只有入队，UI 上的「插话」二字是假的。 */
            if (mode === "steer") { steerToRunning(text); return; }
            queueStore.enqueue(text);
            // [5702] 入队也走「发起会话」：同步路径下就是当前绑定的 machine
            if (inputBar) inputBar.commitSent(inputBar.machine);
            if (queueDock) queueDock.refresh();
            return;
          }
          send();
        },
        stop() { doStop(); },
        steerQueue() {
          if (!sessionState.running) { drainQueue(queueStore); return; }
          /* [5715] 整队插话：把排队里的消息**一次性**插进正在跑的这一轮。
             原来这里只弹一句「轮结束后依序发出」——用户等的是 1–8 分钟。 */
          const items = queueStore.queued();
          if (!items.length) { notify("没有排队中的消息", "info"); return; }
          steerToRunning(items.map((i) => String(i.text || "")).join("\n\n"), items);
        },
        notify,
      },
    });
    inputBar.refresh();
    if (queueDock) queueDock.refresh();
    // [5702] 建好后先绑一张（bootRestore 会按真实卡再换绑）
    switchComposer(currentCard ? currentCard.meta.card_id : "draft");
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
      // [建项目流程 2026-10-17] 两种草稿共用 card_id="draft"，靠项目身份区分：
      //   · WORK_ID==="draft"        ⇒ 项目草稿（/work/draft）：新建项目，根卡随项目一起落库；
      //   · WORK_ID===真 work_id     ⇒ 建卡草稿（/work/{wid}/card/draft）：新卡落库。
      // 两者都不能走 getCard（draft 无库 ⇒ 404），但画面与后续发送走同一条路。
      if (IS_DRAFT_PROJECT) { enterDraftProjectPage(); return true; }
      let d = {};
      try {
        const raw = sessionStorage.getItem("newCard:" + WORK_ID);
        sessionStorage.removeItem("newCard:" + WORK_ID);
        if (raw) d = JSON.parse(raw) || {};
      } catch (e) { d = {}; }
      enterNewCard({ title: d.title || "", parent: d.parent || "", card_type: d.card_type || "" });
      return true;
    }
    /* [5715 · 修] 旧 hash 链接兼容：**只认 hash**。
     * 不能用 select.get()——它现在优先读路径，对话页会返回「当前页自己」，
     * 于是 location.replace 把自己换掉 ⇒ 无限重定向。 */
    const legacy = navStore.select.parse(location.hash);
    if (legacy && legacy.cid) {
      // 旧 hash 链接 → 新地址（wid 不同也一并收敛，原跨 work 分支并入此处）
      location.replace("/work/" + legacy.wid + "/card/" + legacy.cid);
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

  /* [5715 · 修] 浏览器前进/后退时同步切卡。
   * 监听对象由 hashchange 改 popstate——选中态已迁到路径，hash 不再变化。
   * 读路径（parsePath），不用 select.get()：后退到「无卡」的路径时 get() 会回退读 hash。 */
  window.addEventListener("popstate", () => {
    const sel = navStore.select.parsePath(location.pathname);
    if (!sel || !sel.cid) return;
    if (sel.wid !== WORK_ID) {
      window.location.href = "/work/" + sel.wid + "/card/" + sel.cid;
      return;
    }
    if (!currentCard || currentCard.meta.card_id !== sel.cid) {
      open(sel.cid);
    }
  });

  // [会话渲染单源化 2026-10-15] 消息类节点的渲染器在此一次性注入 stream.js：
  // 内容格式化归本文件，放置归装配器快照（C2）。
  registerMessageRenderers();

  /* [对话界面商业化 2026-10-17] 代码块复制：一次 document 级委托（不随消息重建重绑）。
     md.js 只输出带 data-copy 的按钮，事件处理集中在此处。 */
  bindCopyButtons(document);

  /* [对话界面商业化 2026-10-17] 用户消息「重新发送」的宿主侧落地：
     stream.js 不持有发送闭包，只发一条 ui:resend 事件；本文件收到后走既有
     resend(content) 全流程（pair_id 复用、事件流、错误行、排队 drain 全不变）。
     运行中忽略——与 resend 自身的重入守卫同口径，避免双发。 */
  window.addEventListener("ui:resend", (e) => {
    const content = e && e.detail && e.detail.content;
    if (!content) return;
    if (sessionState.running) return;
    resend(String(content));
  });

  /* [对话界面商业化 2026-10-17] 空态建议项 → 填入输入框（不代发）。
     走 input-bar 的 setDraft（同一份 machine 状态），再聚焦——不直接改 DOM 值，
     否则机器里的 draft 与界面上看到的字会分叉。 */
  window.addEventListener("ui:fill-input", (e) => {
    const text = e && e.detail && e.detail.text;
    if (!text || !inputBar) return;
    inputBar.setDraft(String(text));
    inputBar.focus();
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
  // [5715 · 抽模块] 卡详情开合搬进 esm/inspector.js（原先在装配闭包里 ⇒ 导不出来、
  // 两个入口各绑一次容易漂移；新件里两个入口共用同一个 toggle 动作）。
  initInspectorToggle();
  // [画布入口 2026-10-16 用户令] 顶栏 → 返回本项目画布（根卡/子卡聊天态常显）
  const canvasEntryBtn = document.getElementById("canvasEntryBtn");
  // [阶段3 A3.2 2026-10-17] 对话页无画布可切 ⇒ 回画布＝整页跳转到画布页 URL。
  // [画布 2026-10-18] 返回画布时带上当前卡：回去之后画布直接选中它，
  // 用户不用在一屏 5 张卡里重新找"我刚才在哪"。
  /* [5715 · 修] 草稿态隐藏：/work/draft 时项目还没落库，没有画布可回，
     按钮点了只会跳回本页（无效入口）。落库后由 ensurePersisted 放出来。 */
  function syncCanvasEntry() {
    if (!canvasEntryBtn) return;
    const usable = !!WORK_ID && WORK_ID !== "draft";
    canvasEntryBtn.style.display = usable ? "inline-flex" : "none";
  }
  syncCanvasEntry();
  if (canvasEntryBtn) canvasEntryBtn.onclick = () => {
    const cid = (window.__CARD_ID__ || "").trim();
    location.assign("/work/" + WORK_ID + (cid ? "?sel=" + encodeURIComponent(cid) : ""));
  };
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
