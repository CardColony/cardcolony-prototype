/* esm/nav_system.js —— [分页 2026-10-16] 左栏导航系统（画布页 / 对话页共用）。
 *
 * 来源：原 workspace.js 的导航段整体抽出。画布与对话拆成两个独立页面（用户 2026-10-16 令：
 * 「这不是客户端，是网页，网页不该这样」）后，左栏是唯一两页都在的交互系统——
 * 它不该跟着任何一页走，故独立成本模块，两页各自实例化。
 *
 * 页面差异全部经 deps 注入（本模块不感知页面形态）：
 *   workId      当前项目号
 *   api         API 对象（缺省用 esm/api.js 的 API）
 *   selected()  选中层投影源：() => ({wid, cid}) | null。
 *               对话页＝当前打开的卡；画布页＝null（画布无选中态）。
 *   onOpenCard(wid, cid, focusChild)  点卡进对话：画布页＝跳转 /work/{wid}/card/{cid}；
 *               对话页＝同项目就地 open、跨项目跳转。
 *   onAddCard(wid)   无根卡项目的 ＋ 入口：两页同语义＝去该项目画布建卡（建卡对话框在画布页）。
 *   onDeletedCurrent()  对话页删掉「当前打开的卡」后回画布（画布页不传）。
 *
 * 选中层改造：state_layers 的 select 读侧走 location.hash（#wid/cid 复合键）。分页后
 * 选中态不再住在 hash（页面路径即身份），故本模块给 store 注入**虚拟 location**——
 * hash getter 恒返回 selected() 的复合键形态。row_model / 面包屑的选中投影零改动，
 * s2/s3 测试口径不动。 */
import { API, notify } from "./api.js?v=35";
import { escapeHtml } from "./util.js?v=30";
import {
  createNavStateStore, storageGet, storageSet,
} from "./state_layers.js?v=30";
import { createNavAsync } from "./nav_async.js?v=30";
import { createNavRenderer } from "./nav_render.js?v=30";
import { createNavFind } from "./nav_find.js?v=30";
import { createNavBreadcrumb, ancestorCards } from "./nav_breadcrumb.js?v=30";
import { cardInSnapshot } from "./nav_badge.js?v=30";

export function createNavSystem(deps) {
  const WORK_ID = deps.workId || "";
  const api = deps.api || API;
  const selected = deps.selected || (() => null);
  const onOpenCard = deps.onOpenCard || (() => {});
  const onAddCard = deps.onAddCard || ((wid) => { window.location.href = "/work/" + wid; });
  const tree = document.getElementById("navTree");

  // 虚拟 location/history：select 层读写不再碰真实 URL（路径即页面身份，hash 机制退役）
  const virtualLoc = {
    get hash() {
      const s = selected();
      return s ? "#" + s.wid + "/" + s.cid : "";
    },
  };
  const virtualHist = { replaceState() {} };

  // ==================== [T17-F4] 左栏对话导航（读 /api/nav，服务端排序原样渲染） ====================
  // 层级口径（R-016/R-044，写死）：项目根卡＝第 0 层恒定。
  const navStore = createNavStateStore({
    location: virtualLoc,
    history: virtualHist,
    workId: WORK_ID,
  });
  const navAsync = createNavAsync(navStore, api);
  const navRenderer = createNavRenderer({
    tree,
    store: navStore,
    api,
    workId: WORK_ID,
    async: navAsync,
    getCardId: () => {
      const s = selected();
      return (s && s.wid === WORK_ID) ? s.cid : "";
    },
  });

  // [定稿 2026-10-12] 找通道：搜索＝镜片——输入即把树重投影成「命中＋其到根的路径」，
  // 命中标黄；清空即原样还原（query 只活在 temp 内存，从不写 prefs，所以无需回滚）。
  const navFind = createNavFind({
    input: document.getElementById("navSearchInput"),
    store: navStore,
    workId: WORK_ID,
    onJump: (wid, cid) => onOpenCard(wid, cid, false),   // R-20 叶子跳转
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
    workId: WORK_ID,
    getTree: () => tree,
    onJump: (wid, cid) => onOpenCard(wid, cid, false),
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

  // ─────────── 次级操作（⋯ 菜单 / 删除） ───────────

  let navMenuEl = null;
  function closeNavMenu() {
    if (navMenuEl) { navMenuEl.remove(); navMenuEl = null; }
  }
  // [定稿 2026-10-12] ⋯ 菜单＝卡片/项目行的次级操作入口。
  // [用户 2026-10-15 ①]「新建子卡」已从本菜单取出，改为行尾常驻的 ＋ 入口。
  // 菜单只留：打开（跨项目的卡）＋ 删除。删除置灰判据＝后端既有防护搬到眼前
  // （card_store 拒删带子卡的卡、拒删根卡），不是新规矩。
  function showNavMenu(cid, wid, anchor) {
    closeNavMenu();
    const isProject = !!anchor.closest(".nav-phead");   // 项目行（根卡）与卡行走同一口
    const p = navStore.snapshot.project(wid);
    const card = p && (p.cards || []).find(c => c.card_id === cid);
    const kids = card ? (card.child_count || 0) : 0;
    const delDisabled = isProject || kids > 0;
    const items = [];
    if (!isProject && wid !== WORK_ID) items.push(`<div class="nav-menu-item" data-act="detail">打开此卡</div>`);
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
        if (item.classList.contains("disabled")) return;   // 带子卡的卡：删除不可点
        const act = item.dataset.act;
        closeNavMenu();
        if (act === "del") { doDeleteCard(wid, cid, card); return; }
        if (act === "detail") { onOpenCard(wid, cid, false); return; }
      };
    });
    setTimeout(() => document.addEventListener("click", closeNavMenu, { once: true }), 0);
  }

  // [定稿] 删卡：确认 → DELETE 端点 → 重拉导航（快照/版本探测同一条既有链，零新机制）
  async function doDeleteCard(wid, cid, card) {
    const title = (card && card.title) || cid;
    if (!window.confirm(`删除「${title}」？`)) return;
    try {
      await api.deleteCard(wid, cid);
    } catch (e) {
      return;   // toast 已由 api.js 处理
    }
    // [分页 2026-10-16] 对话页删的是「当前打开的卡」→ 本页 URL 已指向死卡，回画布。
    const sel = selected();
    if (deps.onDeletedCurrent && sel && sel.wid === wid && sel.cid === cid) {
      deps.onDeletedCurrent();
      return;
    }
    await renderNav();
  }

  // ＋（无根卡项目）＝去该项目画布建卡（两页同语义：建卡对话框在画布页顶栏）
  function handleProjectAdd(wid) {
    onAddCard(wid);
  }

  // ─────────── 收起/定位（无 UI 入口，实现保留） ───────────

  // [S3/R-28] 收起非路径分支的批量内核：非 keepOpenWid 的项目行全收；已展开分支凡不在
  // pathIds 一律折叠。逐条走 prefs 常规 API——路径条目的 source 标记原样保留。
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
  function collapseAllKeepPath() {
    const sel = selected();
    const curWid = (sel && sel.wid) || WORK_ID;
    const curCid = (sel && sel.cid) || "";
    const pathIds = new Set(ancestorCards(navStore.snapshot, curWid, curCid).map(c => c.card_id));
    if (curCid && !navStore.prefs.getProjectOpen(curWid)) {
      navStore.prefs.setProjectOpen(curWid, true);   // 当前卡所在项目行必须保持展开
    }
    collapseNonPathProjects(curCid ? curWid : null, pathIds);
    navFind.close();
    navRenderer.render();
    navBreadcrumb.render();
  }

  // [S3/R-26] 定位到当前卡：收起非路径分支＋祖先链强制展开＋滚到当前卡可视区。
  function locateCurrentCard() {
    const sel = selected();
    const wid = (sel && sel.wid) || WORK_ID;
    const cid = (sel && sel.cid) || "";
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
    // 滚动定位：行前缀和 top（expandRows 导出）＋在流头部高度（vl.headerH）−吸顶盖高
    const rows = navRenderer.expandRows();
    const idx = rows.findIndex(r => r.kind === "card" && r.wid === wid && r.card.card_id === cid);
    if (idx >= 0) {
      let off = 0;
      for (let i = 0; i < idx; i++) off += rows[i].rowHeight();
      const header = navBreadcrumb.headerHeight();
      const cover = navBreadcrumb.stickyCoverPx();
      tree.scrollTop = Math.max(0, header + off - cover);
      navRenderer.render();   // 显式 reslice（容器 scroll 被动监听亦触发，双保险）
    }
  }

  // ─────────── 行点击 ───────────

  // [S3/R-21] 灰态兜底：点击的卡不在快照（多标签删除/快照缝隙）→ 置灰不跳。
  // [分页 2026-10-16] 简化：选中卡消失时不再改写 URL（路径由页面拥有）；面包屑按现状重渲。
  function handleMissingCard(rowEl) {
    rowEl.classList.add("deleted");
    navBreadcrumb.render();
  }

  // [定稿 2026-10-12] 卡行点击＝打开这张卡本身——**永远**，与有没有子卡无关。
  // 「展开/收起」只归箭头（data-twist），两个动作两个地方，行为不随数据变。
  function handleCardRowClick(rowEl) {
    const cid = rowEl.dataset.cid;
    const wid = rowEl.dataset.wid || WORK_ID;
    if (!cardInSnapshot(navStore.snapshot, wid, cid)) {
      handleMissingCard(rowEl);
      return;
    }
    rowEl.classList.remove("deleted");   // 快照已恢复的自愈
    // 从搜索态点开一张卡 → 顺手退出镜片，回到平时看的那棵树
    if (navStore.temp.getQuery && navStore.temp.getQuery()) navFind.close();
    onOpenCard(wid, cid, false);
  }

  // 导航事件委托：一次绑定，免重挂。[用户 2026-10-15 ①②] 语义统一后的完整口径：
  //   ▸/▾（data-twist / data-ptwist，24px 热区）＝**唯一**负责展开/收起；
  //   卡行名字单击＝进这张卡的对话（与有没有子卡无关，灰态卡除外）；
  //   项目行名字单击＝进该项目**根卡**的对话（无根卡项目回退＝展开/折叠，保证点了有反应）；
  //   ＋（data-newchild）＝给这张卡/根卡新建子卡；无根卡项目的 ＋（data-add）＝去该项目画布建卡；
  //   ⋯（data-more）＝次级操作菜单（删除；跨项目的卡另有「打开此卡」）。
  tree.addEventListener("click", (e) => {
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
    // ＋ 新建子卡：onOpenCard(wid, cid, focusChild=true)——对话页＝开卡+光标落子卡标题框；
    // 画布页＝跳 /work/{wid}/card/{cid}?focus=child
    const pnew = e.target.closest("[data-newchild]");
    if (pnew) { onOpenCard(pnew.dataset.nwid, pnew.dataset.newchild, true); return; }
    const padd = e.target.closest("[data-add]");
    if (padd) { handleProjectAdd(padd.dataset.add); return; }
    const phead = e.target.closest(".nav-phead");
    if (phead) {
      // 项目行名字单击＝进根卡对话（与子卡同构）。无根卡的项目（work_card_id 为空）
      // 没有"它的对话"可进，回退＝展开/折叠（原行为）。
      const rootCid = phead.dataset.root || "";
      if (!rootCid) { navRenderer.toggleProject(phead.dataset.wid); return; }
      onOpenCard(phead.dataset.wid, rootCid, false);
      return;
    }
    const row = e.target.closest(".nav-card");
    if (row) handleCardRowClick(row);
  });

  // [S3/R-18] 树滚动 → 面包屑吸顶联动（scrollTop>2 挂 .sticky）
  tree.addEventListener("scroll", () => { navBreadcrumb.onTreeScroll(); }, { passive: true });

  // ─────────── 静态绑定 ───────────

  // 窄条 ☰：钉住（或收起）悬停浮层全树（画布页；对话页左栏常开，按钮同样无害可用）
  const navRailBtn = document.getElementById("navRailBtn");
  if (navRailBtn) {
    navRailBtn.onclick = () => {
      const nav = document.getElementById("appNav");
      if (nav) nav.classList.toggle("pinned");
    };
  }
  // [S2/M3] 版本探测 60s 周期：version 未变零开销；变了才全量重拉并只 diff 变化 work 子树。
  setInterval(() => { navRenderer.probeVersion(); }, 60000);
  // [左栏接线 步2] 运行中总览 10s 周期：纯内存读、开销极小；集合无变化内部短路不重渲。
  setInterval(() => { navRenderer.probeRunning(); }, 10000);
  // 初始导航渲染（画布态窄条悬停浮层也有树）；快照就绪后再标「谁在跑」，避免空态闪烁
  renderNav().then(() => navRenderer.probeRunning());

  // ─────────── [迭代四 F4①②③] 通知按钮＋面板 ───────────
  {
    const btn = document.getElementById("navNotifyBtn");
    const panel = document.getElementById("notifyPanel");
    const list = document.getElementById("npList");
    const count = document.getElementById("navNotifyCount");
    const READ_KEY = `projMsgRead:${WORK_ID}`;

    function readMap() {
      try { return JSON.parse(storageGet(null, READ_KEY, "{}") || "{}") || {}; } catch (e) { return {}; }
    }
    function writeRead(map) {
      try {
        const entries = Object.entries(map).sort((a, b) => b[1] - a[1]).slice(0, 500);   // 上限 500 按 ts 淘汰
        storageSet(null, READ_KEY, JSON.stringify(Object.fromEntries(entries)));
      } catch (e) { /* 零阻断 */ }
    }

    async function refreshBadge() {
      try {
        const d = await api.getCardMessages(WORK_ID);
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
        const btnIt = document.createElement("button");
        btnIt.className = "np-item" + (isBad ? " bad" : "");
        const ts = (i.ts || "").slice(0, 16).replace("T", " ");
        btnIt.innerHTML = `<div>${escapeHtml(ts)} · ${escapeHtml(i.from_title || i.from_card)} → ${escapeHtml(i.to_card)}` +
          ` · <span class="cmsg-type${isBad ? " bad" : ""}">${escapeHtml(isBad ? "格式异常" : i.type_display)}</span></div>` +
          `<div class="np-meta">${escapeHtml((i.summary || "").slice(0, 60))}</div>` +
          (read[unreadKey] ? "" : `<div class="np-meta">（未读）</div>`);
        btnIt.onclick = () => {
          read[unreadKey] = Date.now();
          writeRead(read);
          panel.classList.remove("open");
          onOpenCard(WORK_ID, i.to_card, false);   // 快捷跳收件卡（B4）——分页后经 onOpenCard
          setTimeout(() => {
            const el = document.querySelector(`[data-seq="${i.seq}"]`);
            if (el) el.scrollIntoView({ block: "center" });
          }, 600);
        };
        return btnIt;
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
        const d = await api.getCardMessages(WORK_ID);
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

  return {
    renderNav,
    store: navStore,
    find: navFind,
    breadcrumb: navBreadcrumb,
    renderer: navRenderer,
    collapseAllKeepPath,   // 无 UI 入口，实现保留（去留待定，见 workspace.js 旧注）
    locateCurrentCard,
  };
}

/* 旧链接兼容：#/wid/cid 或 #cid → /work/{wid}/card/{cid}。
 * 仅画布页入口调用一次（老书签/旧跳转不断链）；对话页无 hash 语义。 */
export function redirectLegacyHash(workId) {
  const h = (location.hash || "").replace(/^#/, "");
  if (!h) return;
  const i = h.indexOf("/");
  const wid = i > 0 ? h.slice(0, i) : workId;
  const cid = i > 0 ? h.slice(i + 1) : h;
  if (wid && cid && /^card_/.test(cid)) {
    location.replace("/work/" + wid + "/card/" + cid);
  }
}
