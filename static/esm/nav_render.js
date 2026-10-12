/* esm/nav_render.js —— [S2] 导航渲染换底：行 DOM createElement 构建 + mount/unmount/patch
 * 三原语 + diff 刷新（R-13/14/15/19）+ 默认折叠策略（R-23）+ 版本探测（R-22 配套）。
 *
 * 结构上无「重建整棵树」路径：行序列由快照+三层状态实时投影（expandRows），
 * 虚拟列表只对可视切片做 mount/unmount/patch；快照与 DOM 分离（R-15）。
 *
 * [R-19 规则（唯一一处）] patchRowEl 开头检查 temp 交互集——hover 中 / loading 中的行
 * 本次不 patch；规则写在这里，不散落任何调用点。
 */
import { escapeHtml } from "./util.js";
import { notify } from "./api.js";
import { createRowModel } from "./row_model.js";
import { createVirtualList } from "./nav_virtual.js";
import { NAV_ROW_HEIGHT } from "./state_layers.js";
// [S3/T4] 呈现族纯函数与常量（R-31 引导线／R-33 徽章聚合／R-35 空态／R-21 快照存在性）
import {
  projectAggregate, nodeActive, isProjectEmpty, cardInSnapshot,
  NAV_GUIDE_DEPTH, NAV_GUIDE_SPACING, NAV_GUIDE_STEP,
} from "./nav_badge.js";

function fmtRel(iso) {
  // 相对时间：刚刚／N 分钟前／N 小时前／N 天前／YYYY-MM-DD（与旧 workspace.js 逐字等价）
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

function cardTip(c) {
  // [B4/E2 既有口径] 行内时间戳退役，活跃/分支活跃走 tooltip 三分列
  return escapeHtml(c.title)
    + (c.last_active_at ? `｜本卡活跃 ${fmtRel(c.last_active_at)}` : "｜无对话")
    + (c.branch_active_at ? `｜分支活跃 ${fmtRel(c.branch_active_at)}` : "");
}

/* [R-19 规则唯一处] diff 应用前的交互集保护：悬停中（temp.hover）/加载中（temp.loading）
 * 的行本次不 patch。规则只在shouldSkipPatch 一处定义，patchRowEl 开头调用，不散落调用点。 */
export function shouldSkipPatch(store, row) {
  if (!row || row.kind === "project") return false;
  const cid = row.card.card_id;
  return store.temp.getHover() === cid || store.temp.isLoading(cid);
}

export function createNavRenderer(deps) {
  const tree = deps.tree;
  const store = deps.store;
  const api = deps.api;
  const workId = deps.workId;
  const async = deps.async;                       // createNavAsync 实例
  const getCardId = deps.getCardId || (() => null); // () => currentCard?.meta.card_id
  const onGoto = deps.onGoto || (() => {});         // (cid, wid) → gotoCard
  const onOpenProject = deps.onOpenProject || (() => {}); // (wid) → handleProjectAdd 外的画布跳转
  const onAddCard = deps.onAddCard || (() => {});   // (wid) → handleProjectAdd
  const SYSTEM_BUDGET = 100;                        // [R-23] 系统展开分支数量兜底

  let appliedFor = "";    // 当前卡祖先路径只自动应用一次（旧 ensureCurrentPath 语义）
  let emptyEl = null;

  /* ---------- 行 DOM 构建（R-13：createElement，非字符串拼接） ---------- */

  function buildRowEl(row) {
    if (row.kind === "project") return buildProjectEl(row);
    return buildCardEl(row);
  }

  function buildProjectEl(row) {
    const p = row.card;
    const el = document.createElement("div");
    el.className = "nav-phead";
    el.dataset.wid = p.work_id;
    el.dataset.root = p.work_card_id || "";
    el.title = p.title || "";
    const twist = document.createElement("span");
    twist.className = "nav-twist";
    twist.dataset.ptwist = p.work_id;
    twist.title = "展开/收起";
    el.appendChild(twist);
    const dot = document.createElement("span");
    dot.className = "dot root";
    el.appendChild(dot);
    const name = document.createElement("span");
    name.className = "nav-pname";
    el.appendChild(name);
    const count = document.createElement("span");
    count.className = "nav-pcount";
    el.appendChild(count);
    const open = document.createElement("button");
    open.className = "nav-popen";
    open.dataset.act = "open";
    open.dataset.wid = p.work_id;
    open.title = "打开该项目画布";
    open.textContent = "打开 →";
    el.appendChild(open);
    const time = document.createElement("span");
    time.className = "nav-ptime";
    el.appendChild(time);
    const add = document.createElement("button");
    add.className = "nav-padd";
    add.dataset.add = p.work_id;
    add.title = "＋ 新建卡";
    add.textContent = "＋卡";
    el.appendChild(add);
    patchProjectContent(el, row);
    return el;
  }

  function buildCardEl(row) {
    const c = row.card;
    const el = document.createElement("div");
    el.className = "nav-card";
    el.dataset.cid = c.card_id;
    el.dataset.wid = row.wid;
    // [S3/R-31] 缩进只到第 4 档：depth ≤ 3 普通缩进（+14px/档）；depth ≥ 4 缩进封顶，
    // 转引导线——repeating-linear-gradient 竖线背景（宽度=深度×档距，封顶在左沟宽度内）
    if (row.depth >= NAV_GUIDE_DEPTH) {
      el.classList.add("nav-guide");
      el.style.backgroundSize =
        Math.min(row.depth * NAV_GUIDE_SPACING, NAV_GUIDE_DEPTH * NAV_GUIDE_STEP) + "px 100%";
      el.style.paddingLeft = (10 + NAV_GUIDE_DEPTH * NAV_GUIDE_STEP) + "px";
    } else {
      el.style.paddingLeft = (10 + row.depth * NAV_GUIDE_STEP) + "px";   // §3.5：每层 +14px
    }
    const twist = document.createElement("span");
    twist.className = "nav-twist";
    if ((c.child_count || 0) > 0) {
      twist.dataset.twist = c.card_id;
      twist.title = "展开/收起";
    } else {
      twist.classList.add("none");   // 叶形（含空子卡节点，V-V4）
    }
    el.appendChild(twist);
    const dot = document.createElement("span");
    dot.className = "dot " + ((c.child_count || 0) > 0 ? "mid" : "leaf");
    el.appendChild(dot);
    const title = document.createElement("span");
    title.className = "nav-ctitle";
    el.appendChild(title);
    const more = document.createElement("button");
    more.className = "nav-more";
    more.dataset.more = c.card_id;
    more.dataset.mwid = row.wid;
    more.title = "建子卡 / 卡详情";
    more.textContent = "⋯";
    el.appendChild(more);
    patchCardContent(el, row);
    return el;
  }

  /* ---------- patch（R-15：只改变化字段；R-19 guard 在最前） ---------- */

  function patchRowEl(el, row) {
    // [R-19] 交互集守卫（规则定义见模块顶部 shouldSkipPatch，唯一处）
    if (shouldSkipPatch(store, row)) return false;
    if (row.kind === "project") patchProjectContent(el, row);
    else patchCardContent(el, row);
    const pr = row.props(store);
    el.classList.toggle("current", !!pr.selected);
    return true;
  }

  function patchProjectContent(el, row) {
    const p = row.card;
    const pr = row.props(store);
    el.querySelector(".nav-twist").textContent = pr.expanded ? "▾" : "▸";
    el.querySelector(".nav-pname").textContent = p.title || "";
    // [S3/R-33①] 项目行徽章＝子树聚合（最新活跃机械 max＋总卡数）；快照 cards 空则回落项目字段
    const agg = projectAggregate(p);
    el.querySelector(".nav-pcount").textContent =
      ((p.cards || []).length ? agg.count : (p.card_count || 0)) + "卡";
    el.querySelector(".nav-ptime").textContent = fmtRel(agg.latest || p.last_active_at);
    el.title = p.title || "";
    // [S3/R-35] 空项目态：展开且零卡 → 行内「空·去新建」（data-add 复用既有事件委托→建卡入口）
    const emptyBadge = el.querySelector(".nav-pempty");
    const needEmpty = pr.expanded && isProjectEmpty(store.snapshot, p.work_id);
    if (needEmpty && !emptyBadge) {
      const b = document.createElement("button");
      b.className = "nav-pempty";
      b.dataset.add = p.work_id;
      b.title = "该项目还没有卡片，点击新建";
      b.textContent = "空·去新建";
      el.appendChild(b);
    } else if (!needEmpty && emptyBadge) {
      emptyBadge.remove();
    }
  }

  function patchCardContent(el, row) {
    const c = row.card;
    const pr = row.props(store);
    const hasKids = (c.child_count || 0) > 0;
    const twist = el.querySelector(".nav-twist");
    twist.classList.toggle("none", !hasKids);
    if (hasKids) twist.textContent = pr.expanded ? "▾" : "▸";
    else twist.textContent = "";
    el.querySelector(".nav-ctitle").textContent = c.title || "";
    el.title = cardTip(c);
    // [S3/R-21] 已删灰态权威投影：行在快照缺失（多标签删除/快照缝隙兜底）→ .deleted；
    // 快照恢复则自动褪灰（与 S2 零状态投影同哲学：类名随快照实时重导）
    el.classList.toggle("deleted", !cardInSnapshot(store.snapshot, row.wid, c.card_id));
    // [S3/R-33②] 节点行活跃标记：branch_active_at 距今<48h（机械判定，纯函数在 nav_badge.js）
    const act = el.querySelector(".nav-badge-active");
    if (nodeActive(c)) {
      if (!act) {
        const s = document.createElement("span");
        s.className = "nav-badge-active";
        s.title = "分支 48 小时内活跃";
        s.textContent = "活跃";
        el.insertBefore(s, el.querySelector(".nav-more"));
      }
    } else if (act) {
      act.remove();
    }
    // [R-22] 三态就地：加载中＝行内转圈；失败＝行内「加载失败·点此重试」
    const tail = el.querySelector(".nav-state");
    if (tail) tail.remove();
    if (pr.loading) {
      const s = document.createElement("span");
      s.className = "nav-state nav-loading";
      el.appendChild(s);
    } else if (pr.error) {
      const b = document.createElement("button");
      b.className = "nav-state nav-err";
      b.textContent = "加载失败·点此重试";
      b.onclick = (e) => {
        e.stopPropagation();
        store.temp.error(c.card_id, null);
        async.loadChildren(row).then(() => render());
      };
      el.appendChild(b);
    }
  }

  /* ---------- 行序列投影（快照+三层 → 行对象数组） ---------- */

  function expandRows() {
    const rows = [];
    const projects = (store.snapshot.get().projects) || [];
    for (const p of projects) {
      rows.push(createRowModel(p, store, { kind: "project", depth: 0, wid: p.work_id }));
      if (!store.prefs.getProjectOpen(p.work_id)) continue;
      pushSegment(rows, p.work_id, "", 0, new Set());
    }
    return rows;
  }

  function pushSegment(rows, wid, parentCid, depth, visited) {
    const cards = store.snapshot.getChildren(wid, parentCid);
    if (!cards) return;   // 段未加载：父行由 props 投影出 loading 态
    for (const c of cards) {
      if (visited.has(c.card_id)) continue;   // 数据环防护
      visited.add(c.card_id);
      const row = createRowModel(c, store, { depth, wid });
      rows.push(row);
      if (store.prefs.getExpanded(wid, c.card_id)) {
        pushSegment(rows, wid, c.card_id, depth + 1, visited);
      }
    }
  }

  /* ---------- 快照灌段（系统展开/探测重灌共用，零网络） ---------- */

  function ensureSegmentFromSnapshot(wid, parentCid) {
    store.snapshot.setChildren(wid, parentCid, store.snapshot.cardsOf(wid, parentCid));
  }

  function snapshotRootsWithOrphans(wid) {
    const p = store.snapshot.project(wid);
    if (!p) return;
    const all = p.cards || [];
    const ids = new Set(all.map(c => c.card_id));
    const root = p.work_card_id || "";
    const seg = all.filter(c => {
      const pa = c.parent || "";
      return pa === "" || pa === root || (pa && !ids.has(pa));   // 孤儿契约：根层末尾，不丢弃
    });
    store.snapshot.setChildren(wid, "", seg);
  }

  /* ---------- [R-23] 默认折叠策略 + 数量兜底 ---------- */

  function currentCardPath(wid, cid) {
    const p = store.snapshot.project(wid);
    if (!p) return [];
    const byId = new Map((p.cards || []).map(c => [c.card_id, c]));
    const path = [];
    let cur = byId.get(cid);
    while (cur && cur.parent && byId.has(cur.parent)) {
      const pa = byId.get(cur.parent);
      path.unshift(pa);
      cur = pa;
    }
    return path;
  }

  function applyDefaultFold() {
    const cid = getCardId();
    if (!cid || appliedFor === cid || !store.snapshot.get().projects.length) return;
    const wid = workId;
    if (!store.prefs.getProjectOpen(wid)) store.prefs.setProjectOpen(wid, true);
    snapshotRootsWithOrphans(wid);
    const path = currentCardPath(wid, cid);
    for (const a of path) {
      // manual（用户手动展开）不动；无条目或 system → 系统展开
      const cur = store.prefs.getExpanded(wid, a.card_id);
      if (cur !== "manual") store.prefs.setExpanded(wid, a.card_id, "system");
      ensureSegmentFromSnapshot(wid, a.card_id);
    }
    enforceBudget(wid, new Set(path.map(a => a.card_id)));
    appliedFor = cid;
  }

  // 系统展开分支 > 100：自动收起最早的非路径 system 分支并提示，不打断用户
  function enforceBudget(wid, pathIds) {
    let warned = false;
    while (store.prefs.systemCount(wid) > SYSTEM_BUDGET) {
      const list = store.prefs.listExpanded(wid).filter(e => e.source === "system");
      const victim = list.find(e => !pathIds.has(e.cid));
      if (!victim) break;
      store.prefs.setExpanded(wid, victim.cid, null);
      store.snapshot.dropChildren(wid, victim.cid);
      store.temp.loaded(victim.cid, false);
      if (!warned) {
        warned = true;
        try { notify("导航展开分支过多，已自动收起最早展开的分支", "info"); } catch (e) { /* 测试环境零阻断 */ }
      }
    }
  }

  /* ---------- 版本探测（60s 周期，M3 替换旧整树重渲） ---------- */

  function diffWorks(oldByWork, newByWork) {
    const changed = [];
    const olds = oldByWork || {};
    for (const wid of Object.keys(newByWork || {})) {
      if (olds[wid] !== newByWork[wid]) changed.push(wid);
    }
    return changed;
  }

  async function probeVersion() {
    let v;
    try { v = await api.getNavVersion(); } catch (e) { return; }   // 探测失败静默跳过（不 toast）
    const old = store.snapshot.get();
    if (old.version && old.version === v.version) return;          // 未变：零开销，连 diff 都不跑
    let data;
    try { data = await api.getNav(); } catch (e) { return; }
    const changed = old.version ? diffWorks(old.by_work, v.by_work) : null;
    store.snapshot.set(data);
    store.snapshot.setVersion(v.version, v.by_work);
    if (changed) {
      // 只重灌变化 work 的已展开段（未变 work 的 DOM 不动）；数据源＝新快照，零网络
      for (const wid of changed) {
        const opened = store.prefs.listExpanded(wid).map(e => e.cid);
        if (store.prefs.getProjectOpen(wid)) ensureSegmentFromSnapshot(wid, "");
        for (const cid of opened) ensureSegmentFromSnapshot(wid, cid);
      }
    } else {
      // 首次（无基线 version）：全量灌当前项目根层，保证渲染有数据
      const p = store.snapshot.project(workId);
      if (p) snapshotRootsWithOrphans(workId);
    }
    applyDefaultFold();
    render();
  }

  /* ---------- 渲染入口 ---------- */

  function render() {
    const rows = expandRows();
    const empty = !((store.snapshot.get().projects || []).length);
    if (empty) {
      if (!emptyEl) {
        emptyEl = document.createElement("div");
        emptyEl.className = "nav-empty";
        emptyEl.innerHTML = `还没有项目<br>
          <a href="/" class="btn primary" style="margin-top:10px;display:inline-block;">＋ 新建项目</a>`;
      }
      emptyEl.style.display = "";
      if (vl.spacer) vl.spacer.style.display = "none";
      return { count: 0 };
    }
    if (emptyEl) emptyEl.style.display = "none";
    if (vl.spacer) vl.spacer.style.display = "";
    return vl.reslice();
  }

  async function refresh() {
    let data = null;
    try {
      const [d, v] = await Promise.all([
        api.getNav(),
        api.getNavVersion().catch(() => null),
      ]);
      data = d;
      if (data) {
        store.snapshot.set(data);
        if (v) store.snapshot.setVersion(v.version, v.by_work);
      }
    } catch (e) {
      return;   // toast 已由 api.js 处理；导航失败不影响画布与对话
    }
    if (!data) return;
    const first = appliedFor === "" ;
    applyDefaultFold();
    if (first) {   // 首帧：快照灌当前项目根层（零网络，V-V5）
      snapshotRootsWithOrphans(workId);
      const path = currentCardPath(workId, getCardId() || "");
      for (const a of path) ensureSegmentFromSnapshot(workId, a.card_id);
    }
    render();
  }

  /* ---------- 交互动作（事件委托层调用） ---------- */

  // 展开态存在但段数据缺失（折叠祖先级联丢段后重展、或快照未灌）→ 惰性补拉（R-22）
  function ensureExpandedSegments(wid) {
    for (const e of store.prefs.listExpanded(wid)) {
      if (!e.cid) continue;
      if (!store.prefs.getExpanded(wid, e.cid)) continue;
      if (store.snapshot.getChildren(wid, e.cid)) continue;
      if (store.temp.isLoading(e.cid) || store.temp.getError(e.cid)) continue;
      const stub = { wid, card: { card_id: e.cid, child_count: 1 } };
      async.loadChildren(stub).then(() => render());
    }
  }

  function toggleProject(wid) {
    const open = store.prefs.getProjectOpen(wid);
    store.prefs.setProjectOpen(wid, !open);
    if (!open) {
      // 展开才拉（R-22）：根卡直系走 /api/nav/children（parent=""）
      const p = store.snapshot.project(wid);
      const projRow = { wid, card: { card_id: "", child_count: (p && p.card_count) || 1 } };
      async.loadChildren(projRow).then(() => { ensureExpandedSegments(wid); render(); });
    }
    render();
  }

  function toggleCard(row) {
    const cid = row.card.card_id;
    const wid = row.wid;
    const open = store.prefs.getExpanded(wid, cid);
    if (open) {
      store.prefs.setExpanded(wid, cid, null);   // 折叠
      async.fold(row);                            // 折叠即丢（R-14）
      render();
    } else {
      store.prefs.setExpanded(wid, cid, "manual");
      async.loadChildren(row).then(() => { ensureExpandedSegments(wid); render(); });   // 展开才拉（R-22）
      render();
    }
  }

  // 虚拟列表（header＝提示行，序列外固定头，与旧行为等价地随滚动滚走）
  const hintEl = document.createElement("div");
  hintEl.className = "nav-hint";
  hintEl.textContent = "▸/▾ 或点行展开/折叠 · 叶子点行进对话 · 「打开 →」进项目画布";   // [S3/R-20] 语义随点击分叉更新
  const vl = createVirtualList(tree, expandRows, NAV_ROW_HEIGHT, {
    header: hintEl,
    renderer: { build: buildRowEl, patch: patchRowEl },
  });

  return {
    refresh, render, probeVersion, toggleProject, toggleCard,
    expandRows, applyDefaultFold, enforceBudget,
    reslice: () => render(),
    vl,
  };
}
