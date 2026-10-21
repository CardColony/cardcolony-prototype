/* esm/nav_system.js —— 左栏导航系统（状态容器 + 渲染 + 交互 + 通知面板）。
   [阶段1 A1.2 2026-10-17] 自 static/workspace.js 逐字搬运，零行为变更。
   装配：workspace.js 传入 workId / navTree / api / getCurrentCard / open / inspector。
   导出：navStore / navAsync / navRenderer / navFind / navBreadcrumb / renderNav /
        RenderNav / gotoCard / startNav。 */
import { escapeHtml } from "./util.js?v=30";
import { notify } from "./api.js?v=42";
import { createNavStateStore, storageGet, storageSet } from "./state_layers.js?v=32";
import { createNavAsync } from "./nav_async.js?v=30";
import { createNavRenderer } from "./nav_render.js?v=38";
import { createNavFind } from "./nav_find.js?v=31";
import { createNavBreadcrumb, ancestorCards } from "./nav_breadcrumb.js?v=30";
import { cardInSnapshot } from "./nav_badge.js?v=30";

/** canvas.js 建卡后同步刷新导航树用（静态具名导出；实现在 createNavSystem 里注入）。 */
let _renderNavImpl = () => {};
export const RenderNav = () => _renderNavImpl();

export function createNavSystem({ workId, navTree, api, getCurrentCard, open, inspector,
                                 onCardsChanged }) {
  const API = api;
  const cur = () => getCurrentCard();  // 搬运段原文的 currentCard（只读）
  /* [删卡刷新 2026-10-21] 卡数据变了要通知宿主：画布页传 reloadCanvas（就地重建），
     对话页不传（它靠 renderNav + 路径跳转）。原来删完只 renderNav，画布上那张卡
     还在原地——用户看着像"没删掉"。 */
  const cardsChanged = typeof onCardsChanged === "function" ? onCardsChanged : () => {};
  // [惰性新建 2026-10-17] 当前 work_id 改**惰性读**：草稿落库成功后 window.__WORK_ID__
  // 就地更新（画布/对话页写），本模块的跳转/建卡/通知口须随取随新——原 const 捕获在
  // 落库后失准（跳转会退回整页重载、通知口打草稿 id → 404）。INIT_WORK_ID 是创建
  // 状态容器/渲染器/查找器/面包屑时注入的一次性初值（它们的 workId 为局部查找用）。
  const WID = () => window.__WORK_ID__;
  const INIT_WORK_ID = workId;

  // ==================== [T17-F4] 左栏对话导航（读 /api/nav，服务端排序原样渲染） ====================
  // 层级口径（R-016/R-044，写死）：项目根卡＝第 0 层恒定；与画布 state.viewRoot 的
  // 相对深度是两套口径，互不同步、不做互相写入。
  // [S2] 原四个导航裸状态变量退役：
  // 状态收进三层容器（temp/prefs/select/snapshot，state_layers.js），渲染走
  // nav_render.js（mount/unmount/patch 三原语 + 虚拟滚动），展开态持久化进偏好层。
  const navStore = createNavStateStore({ workId: INIT_WORK_ID });
  const navAsync = createNavAsync(navStore, API);
  const navRenderer = createNavRenderer({
    tree: navTree,
    store: navStore,
    api: API,
    workId: INIT_WORK_ID,
    async: navAsync,
    getCardId: () => (cur() ? cur().meta.card_id : ""),
  });

  // [定稿 2026-10-12] 找通道：搜索＝镜片——输入即把树重投影成「命中＋其到根的路径」，
  // 命中标黄；清空即原样还原（query 只活在 temp 内存，从不写 prefs，所以无需回滚）。
  const navFind = createNavFind({
    input: document.getElementById("navSearchInput"),
    store: navStore,
    workId: INIT_WORK_ID,
    onJump: (wid, cid) => gotoCard(cid, wid, false),   // 复用 R-20 叶子跳转路径
    onQuery: (q) => {
      navStore.temp.setQuery(q);
      navRenderer.render();
    },
  });
  // [S3/T3] 定位族：面包屑+吸顶（同一 UI 两态）；vl.headerH 是 S2 预留的在流头部高度消费口
  const navBreadcrumb = createNavBreadcrumb({
    box: document.getElementById("navCrumbBox"),
    el: document.getElementById("navCrumb"),
    store: navStore,
    workId: INIT_WORK_ID,
    getTree: () => navTree,
    onJump: (wid, cid) => gotoCard(cid, wid, false),
    vl: navRenderer.vl,
  });

  /* opts 透传给渲染器：[切卡不再重拉导航 2026-10-21] 传 { network:false } ⇒ 只按
     已有快照重画（切卡/换选中走这条），不额外请求 /api/nav ＋ /api/nav/version。 */
  async function renderNav(opts) {
    await navRenderer.refresh(opts);
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
        const seg = all.filter(c => {
          const pa = c.parent || "";
          return pa === "" || pa === root || (pa && !ids.has(pa));
        });
        // [角色机制 5710] 角色卡排根层末尾（作业卡在前、盯梢的在后）。
        seg.sort((a, b) => (a.card_type === "role" ? 1 : 0) - (b.card_type === "role" ? 1 : 0));
        snap.setChildren(wid, "", seg);
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

  // [S2] ensureCurrentPath（祖先路径自动展开）职责移入 nav_render.js applyDefaultFold
  // （source="system" 记入偏好层）；renderNavFromCache / renderProject / renderCardRows
  // 的 innerHTML 整树拼装退役——渲染走 nav_render.js 行级 mount/unmount/patch。

  function gotoCard(cid, wid, focusChild) {
    if (!cid) return;
    if (wid && wid !== WID()) {
      /* [跨项目跳转 2026-10-21 · 修] 原来拼 `/work/{wid}#{cid}`（旧 hash 路由），
         hash 已退役（选中态在路径里）⇒ 靠 bootRestore / main.js 的旧链接兼容链
         多跳一次才落地，任何一环没跟上就是"点了没反应"。直接走页面身份路径。 */
      window.location.href = "/work/" + wid + "/card/" + cid;
      return;
    }
    open(cid).then(() => {
      if (focusChild && inspector) {
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
  // [定稿 2026-10-12] ⋯ 菜单＝卡片/项目行的次级操作入口。
  // [用户 2026-10-15 ①]「新建子卡」已从本菜单取出，改为行尾常驻的 ＋ 入口（悬停可见）——
  // 菜单里不再重复同一个动作。菜单只留：打开（次级入口）＋ 删除。
  // 删除的置灰判据不是新规矩，是把后端已有的结构性防护搬到眼前——card_store 本就
  // ① 拒绝删带子卡的卡（防孤儿）② 拒绝删项目根卡（card_id == work_card_id，403 语义），
  // 旧前端要点了才吃一个错误，这里提前置灰。
  function showNavMenu(cid, wid, anchor) {
    closeNavMenu();
    const isProject = !!anchor.closest(".nav-phead");   // 项目行（根卡）与卡行走同一口
    const p = navStore.snapshot.project(wid);
    const card = p && (p.cards || []).find(c => c.card_id === cid);
    const kids = card ? (card.child_count || 0) : 0;
    const delDisabled = isProject || kids > 0;
    // [用户 2026-10-15 ②] 根卡行的「打开 →」「＋卡」两钮已撤（改为与子卡相同的悬停 ⋯＋），
    // 该行 ⋯ 菜单**不加新条目**——用户要的是"取消这两个操作"，不是换个地方再放一个。
    // 项目行删除恒置灰：card_store 拒删根卡（card_id == work_card_id，403 语义），
    // 提前置灰＝把后端既有防护搬到眼前，不是新规矩。画布入口仍在顶栏面包屑首段（根卡名）。
    const items = [];
    if (!isProject && wid !== WID()) items.push(`<div class="nav-menu-item" data-act="detail">打开此卡</div>`);
    items.push(`<div class="nav-menu-item danger${delDisabled ? " disabled" : ""}" data-act="del">删除</div>`);
    navMenuEl = document.createElement("div");
    navMenuEl.className = "nav-menu";
    navMenuEl.innerHTML = items.join("");
    document.body.appendChild(navMenuEl);
    const r = anchor.getBoundingClientRect();
    navMenuEl.style.left = Math.max(8, Math.min(r.left, window.innerWidth - navMenuEl.offsetWidth - 8)) + "px";
    navMenuEl.style.top = Math.min(r.bottom + 4, window.innerHeight - navMenuEl.offsetHeight - 8) + "px";
    navMenuEl.querySelectorAll(".nav-menu-item").forEach(item => {
      item.onclick = (e) => {
        e.stopPropagation();
        if (item.classList.contains("disabled")) {
          /* [删卡可解释 2026-10-21] 原来静默 return ⇒ 用户点了完全没反应，
             以为按钮坏了。现在说清为什么不能删。 */
          closeNavMenu();
          if (isProject) notify("项目根卡不能删（它是这个项目的入口）", "warn");
          else notify("这张卡还有子卡，先删掉子卡", "warn");
          return;
        }
        const act = item.dataset.act;
        closeNavMenu();
        if (act === "del") { doDeleteCard(wid, cid, card); return; }
        gotoCard(cid, wid, act === "child");
      };
    });
    setTimeout(() => document.addEventListener("click", closeNavMenu, { once: true }), 0);
  }

  // [定稿] 删卡：确认 → DELETE 端点 → 重拉导航（快照/版本探测同一条既有链，零新机制）
  async function doDeleteCard(wid, cid, card) {
    const title = (card && card.title) || cid;
    if (!window.confirm(`删除「${title}」？`)) return;
    try {
      await API.deleteCard(wid, cid);
    } catch (e) {
      return;   // toast 已由 api.js 处理
    }
    /* [删卡后去哪 2026-10-21 · 修] 删的是当前卡 ⇒ 路径还指着已删的卡（原来只
       replaceState 去 hash，而 hash 早就没了 ⇒ 等于什么都没做），页面停在死地址上。
       现在直接回项目画布页。 */
    if (navStore.select.is(wid, cid)) {
      location.href = "/work/" + wid;
      return;
    }
    cardsChanged();          // 画布页：就地重建（否则那张卡还挂在画布上）
    await renderNav();
  }

  function handleProjectAdd(wid) {
    if (wid === WID()) {
      const btn = document.getElementById("newCardBtn");   // 画布页顶栏建卡对话框
      if (btn) btn.click();
      else location.assign("/work/" + WID());            // [阶段3] 对话页无建卡钮：回画布页建
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
    const curWid = (sel && sel.wid) || WID();
    const curCid = (sel && sel.cid) || (cur() ? cur().meta.card_id : "");
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
    const wid = (sel && sel.wid) || WID();
    const cid = (sel && sel.cid) || (cur() ? cur().meta.card_id : "");
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
      const parentCid = (cur() && cur().meta.card_id === cid)
        ? (cur().meta.parent || "") : "";
      if (parentCid) {
        navBreadcrumb.render({ overrideWid: wid, overrideCid: parentCid, bypassDepth: true });
      } else {
        navBreadcrumb.render();
      }
    }
  }

  // [定稿 2026-10-12] 卡行点击＝打开这张卡本身——**永远**，与有没有子卡无关。
  // 旧语义（有子卡＝展开/折叠、只有叶子才跳转）是「有子卡的卡永远点不进去」的根因，已退役：
  // 「展开/收起」只归箭头（data-twist，见下方委托），两个动作两个地方，行为不随数据变。
  function handleCardRowClick(rowEl) {
    const cid = rowEl.dataset.cid;
    const wid = rowEl.dataset.wid || WID();
    // [S3/R-21/V3-A4] 先查后动：snapshot.project(wid) 在场且快照含该卡（cardInSnapshot，
    // nav_badge.js 纯函数，内部走 snapshot.project + cards 全量比对）才动作
    if (!cardInSnapshot(navStore.snapshot, wid, cid)) {
      handleMissingCard(rowEl, wid, cid);
      return;
    }
    rowEl.classList.remove("deleted");   // 快照已恢复的自愈（灰态权威投影在 patchCardContent）
    // [定稿] 从搜索态点开一张卡 → 顺手退出镜片，回到你平时看的那棵树（新卡的路径会自动撑开）
    if (navStore.temp.getQuery && navStore.temp.getQuery()) navFind.close();
    gotoCard(cid, wid, false);
  }

  // 导航事件委托：一次绑定，免重挂。[S2] 读写走三层容器，展开/折叠/拉取走新模块。
  // [S3/R-20] 单击语义按行类型分叉（v3 L50-53；旧 B1「组头整行＝进组根卡对话」退役）：
  //   ▸/▾（data-twist/data-ptwist，24px 热区）＝只管展开收起（S2 热区不动）；
  //   项目行（.nav-phead）单击＝展开/折叠，URL 不变；
  //   有子卡卡行单击＝展开/折叠，URL 不变；
  //   叶子卡单击＝跳转（复合键写 URL＋页面切卡）；已删卡＝灰态不跳（R-21）；
  //   「打开 →」（data-act="open"）＝进项目画布（不动）。互不误触。
  // 导航事件委托：一次绑定，免重挂。[S2] 读写走三层容器，展开/折叠/拉取走新模块。
  // [用户 2026-10-15 ①②] 语义统一后的完整口径（项目行与卡行同构）：
  //   ▸/▾（data-twist / data-ptwist，24px 热区）＝**唯一**负责展开/收起；
  //   卡行名字单击＝进这张卡的对话（与有没有子卡无关，R-21 灰态卡除外）；
  //   项目行名字单击＝进该项目**根卡**的对话（用户 ② 原话「也包含点击根卡就进它的对话」）；
  //   ＋（data-newchild）＝给这张卡/根卡新建子卡；无根卡项目的 ＋（data-add）＝在本项目下新建卡；
  //   ⋯（data-more）＝次级操作菜单（删除；跨项目的卡另有「打开此卡」）。
  // 旧口径退役：「打开 →」（进画布）与「＋卡」两枚文字按钮已从 DOM 撤除（data-act="open" 分支随之删除，
  //   不留死接线）；项目行单击＝展开/折叠退位给 ▸ 箭头。
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
    // ＋ 新建子卡（用户 ①：从 ⋯ 菜单取出，单列入口）。复用既有链：gotoCard(focusChild=true)
    // ＝切到该卡对话 + 展开右栏详情 + 光标落进子卡标题框（原菜单「新建子卡」同一动作，未新造机制）。
    const pnew = e.target.closest("[data-newchild]");
    if (pnew) { gotoCard(pnew.dataset.newchild, pnew.dataset.nwid, true); return; }
    const padd = e.target.closest("[data-add]");
    if (padd) { handleProjectAdd(padd.dataset.add); return; }
    const phead = e.target.closest(".nav-phead");
    if (phead) {
      // [用户 2026-10-15 ②] 项目行名字单击＝进根卡对话（与子卡同构）。
      // 无根卡的项目（work_card_id 为空，实测 work_004/005/007）没有"它的对话"可进，
      // 回退＝展开/折叠（原行为），保证这一行点了始终有反应。
      const rootCid = phead.dataset.root || "";
      if (!rootCid) { navRenderer.toggleProject(phead.dataset.wid); return; }
      // 只在「对话态且已打开根卡」时不重复打开——画布态时 cur() 留着旧值，
      // 若按「当前卡==根卡」判会变成死点击（点了没反应），必须同时看视图态。
      // [阶段5 A5.1] 视图态改判页静态标记（body.page-chat），不再读 data-view。
      /* [当前卡单源 2026-10-21] 早退只问一个口：select.is(项目, 卡)。
         改前这里拼了三个条件（在对话页 且 当前卡存在 且 卡号相同），少比了项目号
         ——而所有项目的根卡都叫 card_001 ⇒ 点 B 项目被判成"已经在看"，毫无反应。 */
      if (navStore.select.is(phead.dataset.wid, rootCid)) return;
      gotoCard(rootCid, phead.dataset.wid, false);
      return;
    }
    const row = e.target.closest(".nav-card");
    if (row) handleCardRowClick(row);
  });

  // [S3/R-18] 树滚动 → 面包屑吸顶联动（同一 DOM 两态；scrollTop>2 挂 .sticky）
  navTree.addEventListener("scroll", () => { navBreadcrumb.onTreeScroll(); }, { passive: true });

  // [对齐样稿 2026-10-12] 搜索区只留搜索框后，「收起全部」「定位」两钮的接线一并移除。
  // 两个函数本体保留（collapseAllKeepPath / locateCurrentCard，见上方定义）——它们是
  // 「一键收起」「滚到当前卡」的唯一实现，删掉即丢能力；此处只摘 UI 入口，去留待定。
  // 已知连带：tests/s3/obs_nav_s3.js 是人肉观察脚本（非单测），其中两处 .click() 会取不到元素。

  // [S2/M3] 版本探测 / 运行总览周期 + 初始导航渲染（原 workspace.js :1785–1793）
  function startNav() {
  // [S2/M3] 版本探测 60s 周期：version 未变零开销（连 diff 都不跑）；变了才全量重拉
  // 并只 diff 变化 work 子树。相对时间重算随真实数据变更触发（旧「60s 整树重渲」退役）。
  setInterval(() => { navRenderer.probeVersion(); }, 60000);
  // [左栏接线 步2] 运行中总览 10s 周期：/api/nav/running 纯内存读、开销极小；
  // 一轮长活的起止比卡片数据变更快得多，故不与 60s 版本探测同频。
  // 集合无变化时 probeRunning 内部短路不重渲——状态变化不动用户视野。
  setInterval(() => { navRenderer.probeRunning(); }, 10000);
  // 初始导航渲染（画布态窄条悬停浮层也有树）
  renderNav().then(() => navRenderer.probeRunning());   // 快照就绪后再标「谁在跑」，避免空态闪烁
  }

  // ═══ [迭代四 F4①②③] 通知按钮＋面板：拉 K4 端点、渲染列表、点击跳卡、已读推进 ═══
  {
    const btn = document.getElementById("navNotifyBtn");
    const panel = document.getElementById("notifyPanel");
    const list = document.getElementById("npList");
    const count = document.getElementById("navNotifyCount");
    // [惰性新建 2026-10-17] 已读键改惰性函数：草稿落库后 work_id 变，键须随取随新。
    const readKey = () => `projMsgRead:${WID()}`;

    // [S2/V-S5] 裸本地存储访问收口：经 state_layers 的 storage 包装（行为等价）
    function readMap() {
      try { return JSON.parse(storageGet(null, readKey(), "{}") || "{}"); } catch (e) { return {}; }
    }
    function writeRead(map) {
      try {
        const entries = Object.entries(map).sort((a, b) => b[1] - a[1]).slice(0, 500);   // 上限 500 按 ts 淘汰
        storageSet(null, readKey(), JSON.stringify(Object.fromEntries(entries)));
      } catch (e) { /* 零阻断 */ }
    }

    async function refreshBadge() {
      // [D6] 草稿态：无真 work，不打 card-messages（否则 404）；落库后 WID() 变真，自然恢复。
      if (WID() === "draft") { count.textContent = ""; count.className = "nav-notify-count"; return; }
      try {
        const d = await API.getCardMessages(WID());
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
          open(i.to_card);   // 快捷跳收件卡（B4）
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
      if (WID() === "draft") { notify("项目尚未保存，暂无卡间消息", "warn"); return; }   // [D6] 草稿：不打 card-messages
      try {
        const d = await API.getCardMessages(WID());
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

  _renderNavImpl = renderNav;   // 静态导出 RenderNav 的实现注入
  return { navStore, navAsync, navRenderer, navFind, navBreadcrumb,
           renderNav, RenderNav: renderNav, gotoCard, startNav };
}
