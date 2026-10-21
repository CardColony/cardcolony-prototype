/* esm/nav_render.js —— [左栏接线 步2] 行结构按定稿重写（样式定稿 v1+v2 全量落地）。
 *
 * 行骨架（卡行）：segs 线段层 ＋ 箭头(nav-chev, data-twist) ＋ 图标(nav-ico) ＋
 *                 标题(nav-ctitle) ＋ 行尾槽(nav-tail：时间/运行中转圈 二选一 ＋ ⋯)。
 * 定稿口径：
 * · 点名字＝打开这张卡（箭头只负责展开/收起——点击语义在步3 切换，本步保持既有委托）；
 * · 树线连在图标列上，每行只画自己高度内的线段（nav_geo.js 纯函数给坐标）；
 * · 项目根卡↔一级子卡不画线不缩进；再深每层微微推进（indentOf）；
 * · 每项目＝一张四圆角卡片（.nav-last 类画卡片底边，nav.css 实现）；
 * · 工具卡不进侧栏（card_type==="tool" 连同子树在投影层剥掉——快照层不过滤，
 *   cardInSnapshot 等消费方语义不变）；
 * · 灰只有一处含义（行尾时间＝次要信息）：标题/图标永不涂灰，「活跃」文字徽章退役
 *   （排序与时间已表达冷热；nodeActive 纯函数保留给步3 默认展开消费）；
 * · 行尾槽：运行中（/api/nav/running 灌入 temp.running）→ 转圈，时间让位；
 *   悬停 ⋯ 落在时间原位（CSS 同槽实现）。
 *
 * 保留不动的骨架：mount/unmount/patch 三原语、expandRows 投影、版本探测、
 * 默认折叠（当前卡路径）、SYSTEM_BUDGET 兜底、事件委托契约
 * （data-twist/data-ptwist/data-more/data-add/data-act="open"、.nav-card/.nav-phead、
 *   __navRow、dataset.cid/wid）——workspace.js 委托一行不改。
 *
 * [R-19 规则（唯一一处）] patchRowEl 开头检查 temp 交互集——hover 中 / loading 中的行
 * 本次不 patch；规则写在这里，不散落任何调用点。
 */
import { escapeHtml } from "./util.js?v=30";
import { notify } from "./api.js?v=46";
import { createRowModel } from "./row_model.js?v=31";
import { createVirtualList } from "./nav_virtual.js?v=31";
import { NAV_ROW_HEIGHT } from "./state_layers.js?v=32";
// [S3] 呈现族纯函数（R-33 徽章聚合／R-35 空态／R-21 快照存在性）
import { projectAggregate, isProjectEmpty, cardInSnapshot } from "./nav_badge.js?v=30";
// [左栏接线 步2] 行几何纯函数（微推进／图标列／树线段）
import { indentOf, rowSegs, childGuides } from "./nav_geo.js?v=30";

/* ---------- 图标（定稿：图标只表达"项目/卡"一件事；线经过处由 svg 底色咬断） ---------- */
const SVG_CHEV = '<svg viewBox="0 0 16 16" fill="none"><path class="under" d="M5.5 3.5 10 8l-4.5 4.5" stroke-linecap="round" stroke-linejoin="round"/><path class="top" d="M5.5 3.5 10 8l-4.5 4.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const SVG_FOLDER = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M1 3.5A1.5 1.5 0 0 1 2.5 2H6l1.7 1.5h5.8A1.5 1.5 0 0 1 15 5v7.5A1.5 1.5 0 0 1 13.5 14h-11A1.5 1.5 0 0 1 1 12.5z"/></svg>';
const SVG_DOC = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M3 2.5h7L12.5 5v8A1.5 1.5 0 0 1 11 14.5H3A1.5 1.5 0 0 1 1.5 13v-9A1.5 1.5 0 0 1 3 2.5z"/><path d="M10 2.5V5h2.5"/></svg>';
// [角色机制 5710] 角色卡图标：离树的观察者（人形），与作业卡区分。
const SVG_ROLE = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><circle cx="8" cy="5.2" r="2.4"/><path d="M3.2 13.6c.5-2.7 2.2-4 4.8-4s4.3 1.3 4.8 4"/></svg>';
const SVG_DOTS = '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="3.5" cy="8" r="1.3"/><circle cx="8" cy="8" r="1.3"/><circle cx="12.5" cy="8" r="1.3"/></svg>';
// [用户 2026-10-15 ①] 新建子卡从 ⋯ 菜单取出，单列一个 ＋ 入口（悬停时 ⋯ 左移一位、＋ 落原位）
const SVG_PLUS = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M8 3.6v8.8"/><path d="M3.6 8h8.8"/></svg>';

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
  // [B4/E2 既有口径] 活跃/分支活跃走 tooltip 三分列（行内不堆文字，定稿第 2 条）
  return (c.card_type === "role" ? "〔角色〕" : "")
    + escapeHtml(c.title)
    + (c.last_active_at ? `｜本卡活跃 ${fmtRel(c.last_active_at)}` : "｜无对话")
    + (c.branch_active_at ? `｜分支活跃 ${fmtRel(c.branch_active_at)}` : "");
}

/* [定稿] 搜索命中标黄：转义后只把命中的那一段包进 <mark>（大小写不敏感匹配）。
 * 先转义再包标签——顺序反了就是 XSS 口子（标题是用户数据）。 */
function highlight(title, q) {
  const s = String(title || "");
  const needle = String(q || "").trim().toLowerCase();
  const i = needle ? s.toLowerCase().indexOf(needle) : -1;
  if (i < 0) return escapeHtml(s);
  return escapeHtml(s.slice(0, i))
    + "<mark>" + escapeHtml(s.slice(i, i + needle.length)) + "</mark>"
    + escapeHtml(s.slice(i + needle.length));
}

/* [定稿 18:33] 工具卡不进左侧边栏：投影层剥掉（连同子树——子树因父行不出而不出）。
 * 快照层不过滤：cardInSnapshot（已删灰态）/cardsOf（零网络灌段）语义保持不变。 */
function isToolCard(c) { return c && c.card_type === "tool"; }

/* [R-19 规则唯一处] diff 应用前的交互集保护：悬停中（temp.hover）/加载中（temp.loading）
 * 的行本次不 patch。规则只在 shouldSkipPatch 一处定义，patchRowEl 开头调用，不散落调用点。 */
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

  /* [用户 2026-10-15 ①] 行尾槽＝时间/转圈（状态）＋ ⋯、＋（悬停操作）。
   * 悬停时 ⋯ 左移一个身位、＋ 落在 ⋯ 原来的位置（＝行尾/时间原位）——CSS 用绝对定位
   * 把两枚图标压在状态区上方（right:20px / right:0），所以悬停不改变行宽、不挤标题。
   * plusKind："child"＝在卡下建子卡（data-newchild）；"card"＝在项目下新建卡（data-add，
   * 复用既有委托分支 handleProjectAdd）。两种只差一个 data 属性，视觉完全一致。 */
  function makeTail(opts = {}) {
    const tail = document.createElement("div");
    tail.className = "nav-tail";
    const time = document.createElement("span");
    time.className = "nav-time";
    tail.appendChild(time);
    if (opts.plus) {
      const more = document.createElement("button");
      more.className = "nav-more";
      more.dataset.more = opts.cid || "";
      more.dataset.mwid = opts.wid || "";
      more.title = "更多";
      more.innerHTML = SVG_DOTS;
      tail.appendChild(more);
      tail.__more = more;
      const plus = document.createElement("button");
      plus.className = "nav-plus";
      if (opts.plus === "card") {
        plus.dataset.add = opts.wid || "";
        plus.title = "新建卡";
      } else {
        plus.dataset.newchild = opts.cid || "";
        plus.dataset.nwid = opts.wid || "";
        plus.title = "新建子卡";
      }
      plus.innerHTML = SVG_PLUS;
      tail.appendChild(plus);
      tail.__plus = plus;
    }
    tail.__time = time;
    return tail;
  }

  function buildProjectEl(row) {
    const p = row.card;
    const el = document.createElement("div");
    el.className = "nav-phead";
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.dataset.wid = p.work_id;
    el.dataset.root = p.work_card_id || "";
    el.title = p.title || "";
    const chev = document.createElement("span");
    chev.className = "nav-chev";
    chev.dataset.ptwist = p.work_id;
    chev.title = "展开/收起";
    chev.innerHTML = SVG_CHEV;
    el.__chev = chev;
    el.appendChild(chev);
    const ico = document.createElement("span");
    ico.className = "nav-ico proj";
    ico.innerHTML = SVG_FOLDER;
    el.appendChild(ico);
    const name = document.createElement("span");
    name.className = "nav-pname";
    el.__name = name;
    el.appendChild(name);
    // [用户 2026-10-15 ②] 根卡行与子卡行完全同构：「打开 →」「＋卡」两个文字按钮已撤，
    // 悬停出同一套 ⋯＋ 图标；点名字＝进它的对话（委托层处理，与子卡同一语义）。
    // ＋ ＝在它下面新建子卡（与子卡的 ＋ 语义一致——用户要的就是"和其他子卡一样的操作"）。
    // 无根卡的项目（实测 work_004/005/007 的 work_card_id 为空）没有"它的对话"可进，
    // 也没有可挂的父卡——＋ 退回复用既有的 handleProjectAdd（data-add）分支。
    const rootCid = p.work_card_id || "";
    const tail = makeTail(rootCid
      ? { plus: "child", cid: rootCid, wid: p.work_id }
      : { plus: "card", wid: p.work_id });
    el.__tail = tail;
    el.appendChild(tail);
    patchProjectContent(el, row);
    return el;
  }

  function buildCardEl(row) {
    const c = row.card;
    const el = document.createElement("div");
    el.className = "nav-card";
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.dataset.cid = c.card_id;
    el.dataset.wid = row.wid;
    el.dataset.depth = String(row.depth);
    // [定稿] 微推进：字基本还是齐的；segs 是 absolute inset:0，不受 padding 影响，
    // 其坐标由 nav_geo 给出（iconX 已含 indent），线与图标列严格对齐
    el.style.paddingLeft = indentOf(row.depth) + "px";
    const segs = document.createElement("div");
    segs.className = "nav-segs";
    el.__segs = segs;
    el.appendChild(segs);
    const chev = document.createElement("span");
    chev.className = "nav-chev";
    if ((c.child_count || 0) > 0) {
      chev.dataset.twist = c.card_id;
      chev.title = "展开/收起";
    }
    chev.innerHTML = SVG_CHEV;
    el.__chev = chev;
    el.appendChild(chev);
    const ico = document.createElement("span");
    ico.className = "nav-ico doc";
    ico.innerHTML = SVG_DOC;
    // [角色机制 5710] 角色卡离树：单独图标 + 行样式，一眼区分「干活的卡 / 盯梢的卡」
    if (c.card_type === "role") {
      el.classList.add("nav-role");
      ico.classList.add("role");
      ico.innerHTML = SVG_ROLE;
      const badge = document.createElement("span");
      badge.className = "nav-role-badge";
      badge.textContent = "角色";
      el.appendChild(ico);
      el.appendChild(badge);
    } else {
      el.appendChild(ico);
    }
    const title = document.createElement("span");
    title.className = "nav-ctitle";
    el.__title = title;
    el.appendChild(title);
    const tail = makeTail({ plus: "child", cid: c.card_id, wid: row.wid });
    el.__tail = tail;
    el.appendChild(tail);
    patchCardContent(el, row);
    return el;
  }

  /* ---------- 树线段（每行只画自己高度内的线段；patch 时整层重建，行其余不动） ---------- */

  function hasVisibleKids(row) {
    const kids = store.snapshot.getChildren(row.wid, row.card.card_id);
    if (!kids) return false;
    return kids.some(k => !isToolCard(k));
  }

  function renderSegs(container, segs) {
    while (container.children.length) container.children[0].remove();
    for (const s of segs) {
      const d = document.createElement("div");
      d.className = "nav-seg " + (s.k === "v" ? "nav-seg-v" : "nav-seg-h");
      if (s.k === "v") {
        d.style.left = s.x + "px";
        d.style.top = s.t + "px";
        d.style.height = s.h + "px";
      } else {
        d.style.left = s.x + "px";
        d.style.top = s.y + "px";
        d.style.width = s.w + "px";
      }
      container.appendChild(d);
    }
  }

  /* ---------- patch（R-15：只改变化字段；R-19 guard 在最前） ---------- */

  function patchRowEl(el, row) {
    // [R-19] 交互集守卫（规则定义见模块顶部 shouldSkipPatch，唯一处）
    if (shouldSkipPatch(store, row)) return false;
    if (row.kind === "project") patchProjectContent(el, row);
    else patchCardContent(el, row);
    const pr = row.props(store);
    el.classList.toggle("current", !!pr.selected);
    el.classList.toggle("nav-last", !!row.lastInProject);   // 四圆角卡片底边（定稿第 6 条）
    return true;
  }

  function patchProjectContent(el, row) {
    const p = row.card;
    const pr = row.props(store);
    el.__chev.classList.toggle("open", pr.expanded);
    // [定稿] 搜索镜片：项目名命中即标黄（未命中只是定位路径，不着色）
    el.__name.innerHTML = row.matched
      ? highlight(p.title || "", store.temp.getQuery ? store.temp.getQuery() : "")
      : escapeHtml(p.title || "");
    // [S3/R-33①] 项目行时间＝子树聚合（最新活跃机械 max）；快照 cards 空则回落项目字段
    const agg = projectAggregate(p);
    el.__tail.__time.textContent = fmtRel(agg.latest || p.last_active_at);
    el.title = p.title || "";
    // [角色机制 5710] 项目行警报红点：角色卡有未处理警报时，不用点进去也能看见。
    el.classList.toggle("has-alarm", Number(p.open_alarms || 0) > 0);
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
    // [定稿] 搜索态下「有没有子」看的是**剪枝后**还有没有可显示的命中子行（row.searchKids），
    // 不是真实 child_count——否则会出现「箭头说能展开、点开却是空的」这种骗人的行。
    // 镜片期间一律不出箭头：投影已把命中与路径全摊开，没有可收放的东西。
    const hasKids = row.searching ? false : ((c.child_count || 0) > 0);
    // 树线（定稿：连在图标列上，每行自己一小段）
    renderSegs(el.__segs, rowSegs({
      depth: row.depth, guides: row.guides, first: row.first, last: row.last,
      openKids: pr.expanded && (row.searching ? row.searchKids : hasVisibleKids(row)),
      H: row.rowHeight(),
    }));
    // 箭头：有子卡才可点（leaf 隐藏）；开合状态转 90°
    el.__chev.classList.toggle("leaf", !hasKids);
    el.__chev.classList.toggle("open", pr.expanded);
    // [定稿] 镜片期间摘掉 data-twist（事件委托认不到它＝点不动），退出搜索由本函数重挂
    if (row.searching) delete el.__chev.dataset.twist;
    else if (hasKids) el.__chev.dataset.twist = c.card_id;
    // [定稿] 搜索镜片：命中的那一行标黄；只是路径的行不着色（它负责定位，不负责提示命中）
    el.__title.innerHTML = row.matched
      ? highlight(c.title || "", store.temp.getQuery ? store.temp.getQuery() : "")
      : escapeHtml(c.title || "");
    el.title = cardTip(c);
    // [S3/R-21] 已删灰态权威投影：行在快照缺失（多标签删除/快照缝隙兜底）→ .deleted；
    // 快照恢复则自动褪灰（与 S2 零状态投影同哲学：类名随快照实时重导）
    el.classList.toggle("deleted", !cardInSnapshot(store.snapshot, row.wid, c.card_id));
    // [定稿] 行尾槽：运行中→转圈（时间让位）；否则→时间。⋯ 恒在（CSS 落时间原位）
    // [修·运行中假阳性 2026-10-13] 判据用 (wid, cid) 复合键——card_id 跨项目不唯一
    const running = store.temp.isRunning(row.wid, c.card_id);
    el.__tail.classList.toggle("has-run", running);
    el.__tail.classList.toggle("has-time", !running && !!c.last_active_at);
    el.__tail.__time.textContent = running ? "" : fmtRel(c.last_active_at);
    // [步2 收尾·补缺] 运行中＝行尾转圈。此前只挂了 has-run 类、**元素从未创建**——
    // nav.css 的 .nav-run/.nav-spinner 写在给不存在的元素上，运行中的卡毫无提示。
    // 插在 time 之前：运行时 time 空（让位）、转圈占其位、⋯ 在右（CSS has-run 分支）。
    let run = el.__tail.__run;
    if (running && !run) {
      run = document.createElement("span");
      run.className = "nav-run";
      run.innerHTML = '<span class="nav-spinner"></span>';
      el.__tail.insertBefore(run, el.__tail.__time);
      el.__tail.__run = run;
    } else if (!running && run) {
      run.remove();
      el.__tail.__run = null;
    }
    // [R-22] 三态就地：加载中＝行内转圈；失败＝行内「加载失败·点此重试」
    const state = el.__tail.__state;
    if (state) { state.remove(); el.__tail.__state = null; }
    if (pr.loading) {
      const s = document.createElement("span");
      s.className = "nav-state nav-loading";
      el.__tail.__state = s;
      el.__tail.appendChild(s);
    } else if (pr.error) {
      const b = document.createElement("button");
      b.className = "nav-state nav-err";
      b.textContent = "加载失败·点此重试";
      b.onclick = (e) => {
        e.stopPropagation();
        store.temp.error(c.card_id, null);
        async.loadChildren(row).then(() => render());
      };
      el.__tail.__state = b;
      el.__tail.appendChild(b);
    }
  }

  /* ---------- 行序列投影（快照+三层 → 行对象数组；含几何上下文） ---------- */

  /* [定稿] 搜索＝镜片：剪掉不相干的枝，只留「命中的卡 ＋ 它到根的路径」。
   * 为什么不平铺成结果列表：那样你就得学一套和平时完全不同的样子；剪枝树让你用同一双
   * 眼睛看所有时候。
   * 为什么一个字都不写进 prefs：镜片是「换一种看法」，不是「改动你摆好的样子」——
   * 清空查询即原样还原，靠的就是它从没写过任何状态（query 只活在 temp 内存）。
   * 命中路径行不标黄，只作定位；命中的那一行标黄。 */
  function searchRows(q) {
    const rows = [];
    const projects = (store.snapshot.get().projects) || [];
    for (const p of projects) {
      const wid = p.work_id;
      const all = (p.cards || []).filter(c => !isToolCard(c));
      const byId = new Map(all.map(c => [c.card_id, c]));
      const root = p.work_card_id || "";
      const projHit = (p.title || "").toLowerCase().includes(q);
      const hits = all.filter(c => (c.title || "").toLowerCase().includes(q));
      if (!projHit && !hits.length) continue;
      rows.push(createRowModel(p, store, {
        kind: "project", depth: 0, wid, searching: true, matched: projHit,
      }));
      if (!hits.length) continue;              // 只有项目名命中：显出项目行即可，不摊它的卡
      // 命中集 ＋ 各自到根的路径（want: cid → 是否命中）
      const want = new Map();
      for (const c of hits) {
        want.set(c.card_id, true);
        let cur = c;
        let guard = 0;
        while (cur && cur.parent && byId.has(cur.parent) && guard++ < 64) {
          cur = byId.get(cur.parent);
          if (!want.has(cur.card_id)) want.set(cur.card_id, false);
        }
      }
      const childrenOf = (pcid) => all.filter(c => {
        const pa = c.parent || "";
        return pcid === "" ? (pa === "" || pa === root) : pa === pcid;
      });
      const walk = (pcid, depth, guides) => {
        const kids = childrenOf(pcid).filter(c => want.has(c.card_id));
        kids.forEach((c, i) => {
          const first = i === 0;
          const last = i === kids.length - 1;
          const grand = childrenOf(c.card_id).filter(k => want.has(k.card_id));
          rows.push(createRowModel(c, store, {
            depth, wid, guides, first, last, searching: true,
            matched: want.get(c.card_id) === true, searchKids: grand.length > 0,
          }));
          walk(c.card_id, depth + 1, childGuides({ depth, last, guides }));
        });
      };
      const start = rows.length;
      walk("", 0, []);
      if (rows.length > start) rows[rows.length - 1].lastInProject = true;
    }
    return rows;
  }

  function expandRows() {
    const q = (store.temp.getQuery ? store.temp.getQuery() : "").trim().toLowerCase();
    if (q) return searchRows(q);
    const rows = [];
    const projects = (store.snapshot.get().projects) || [];
    for (const p of projects) {
      rows.push(createRowModel(p, store, { kind: "project", depth: 0, wid: p.work_id }));
      if (!store.prefs.getProjectOpen(p.work_id)) continue;
      const start = rows.length;
      pushSegment(rows, p.work_id, "", 0, new Set(), []);
      if (rows.length > start) rows[rows.length - 1].lastInProject = true;
    }
    // 每个项目的最后一行挂 .nav-last（卡片底边＋下圆角）——上面逐项目标记
    return rows;
  }

  function pushSegment(rows, wid, parentCid, depth, visited, guides) {
    const cards = store.snapshot.getChildren(wid, parentCid);
    if (!cards) return;   // 段未加载：父行由 props 投影出 loading 态
    const visible = cards.filter(c => !isToolCard(c));   // [定稿] 工具卡不进侧栏
    visible.forEach((c, i) => {
      if (visited.has(c.card_id)) return;   // 数据环防护
      visited.add(c.card_id);
      const first = i === 0;
      const last = i === visible.length - 1;
      const row = createRowModel(c, store, { depth, wid, guides, first, last });
      rows.push(row);
      if (store.prefs.getExpanded(wid, c.card_id)) {
        pushSegment(rows, wid, c.card_id, depth + 1, visited, childGuides({ depth, last, guides }));
      }
    });
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
    // [角色机制 5710] 角色卡离树，但排在根层末尾（作业卡在前、盯梢的在后）。
    seg.sort((a, b) => (a.card_type === "role" ? 1 : 0) - (b.card_type === "role" ? 1 : 0));
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

  /* ---------- [定稿] 最近活跃分支默认露出（用户 15:48 第 2、3 条） ----------
   * 判据：卡的分支活跃（branch_active_at）距今 < 48h —— 机械阈值，后端已送该字段，不新增字段。
   * 作用域：**只在「已打开的项目」内**（含当前项目，它上面被自动打开）。
   *   为什么不自动打开所有「有活跃」的项目：实测 13 个项目里 11 个的项目级活跃都在 48h 内
   *   （同一批时间戳，见 P0 数据污染），全开会把侧栏摊成一锅粥——默认展开的本意是
   *   「让你一眼看到正在干的」，不是「把一切都摊开」。冷项目仍由你自己点开。
   * 不覆盖 manual：用户亲手展开的，系统永不收回（同 applyDefaultFold 的既有判据）。
   */
  const RECENT_MS = 48 * 3600 * 1000;

  function isRecentBranch(c) {
    const t = c && c.branch_active_at ? Date.parse(c.branch_active_at) : NaN;
    return !isNaN(t) && (Date.now() - t) < RECENT_MS;
  }

  function expandRecentInProject(wid) {
    const visited = new Set();
    const walk = (parentCid) => {
      const kids = store.snapshot.cardsOf(wid, parentCid).filter(c => !isToolCard(c));
      for (const c of kids) {
        if (visited.has(c.card_id)) continue;   // 数据环防护
        visited.add(c.card_id);
        if (!isRecentBranch(c)) continue;
        if (store.prefs.getExpanded(wid, c.card_id) !== "manual") {
          store.prefs.setExpanded(wid, c.card_id, "system");
        }
        ensureSegmentFromSnapshot(wid, c.card_id);
        walk(c.card_id);
      }
    };
    walk("");
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
    // [定稿] 已打开的每个项目，各自展开其最近活跃分支
    for (const p of (store.snapshot.get().projects || [])) {
      const w = p.work_id;
      if (!store.prefs.getProjectOpen(w)) continue;
      if (!store.snapshot.getChildren(w, "")) snapshotRootsWithOrphans(w);
      expandRecentInProject(w);
    }
    for (const p of (store.snapshot.get().projects || [])) {
      if (store.prefs.getProjectOpen(p.work_id)) {
        enforceBudget(p.work_id, p.work_id === wid ? new Set(path.map(a => a.card_id)) : new Set());
      }
    }
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

  /* ---------- [左栏接线 步2] 运行中总览灌入（/api/nav/running → temp.running → 行尾转圈） ---------- */

  async function probeRunning() {
    if (!api.getNavRunning) return;
    let r;
    try { r = await api.getNavRunning(); } catch (e) { return; }   // 失败静默（下轮再来）
    const changed = store.temp.setRunningSet((r && r.running) || []);
    if (changed) render();   // 无变化不重渲（不打断交互）
  }

  /* ---------- 渲染入口 ---------- */

  function render() {
    const rows = expandRows();
    const noProjects = !((store.snapshot.get().projects || []).length);
    /* [对话界面商业化 2026-10-17] 搜索剪枝后一行不剩也是「空」：
       此前只判「有没有项目」，搜不到东西时整片树空白、零反馈——静默失败。
       两种空分开说：真没项目 vs 当前查询没命中。 */
    const q = String((document.getElementById("navSearchInput") || {}).value || "").trim();
    const noMatch = !noProjects && !!q && rows.length === 0;
    const empty = noProjects || noMatch;
    if (empty) {
      if (!emptyEl) {
        emptyEl = document.createElement("div");
        emptyEl.className = "nav-empty";
        /* [左栏新建项目 2026-10-16 用户令] 原此处的「＋ 新建项目」<a href="/"> 已删：
           它不建项目、只跳首页（已有项目时还会被 home.js 立刻弹回），是死件；
           左栏常驻的新建项目按钮由 work.html 的 #navNewProject 承担
           （与首页 #homeNewBtn 同一份 .home-new-project 实现），恒在、且真的建项目。
           此处只留空态文案。 */
        emptyEl.innerHTML = `还没有项目`;
        emptyEl.dataset.kind = "projects";
      }
      emptyEl.textContent = noMatch ? "没有匹配的卡" : "还没有项目";
      emptyEl.style.display = "";
      if (vl.spacer) vl.spacer.style.display = "none";
      return { count: 0 };
    }
    if (emptyEl) emptyEl.style.display = "none";
    if (vl.spacer) vl.spacer.style.display = "";
    return vl.reslice();
  }

  /* [切卡加速 2026-10-20] 左栏快照的会话级缓存。
     问题：画布 ⇄ 对话是**整页跳转**（location.assign），每跳一次 JS 世界重建、
     左栏要从零拉 /api/nav + /api/nav/version 再渲染——用户感受就是「切来切去」。
     做法：把上次的快照按版本号存进 sessionStorage，跳转后**先渲染缓存**（零网络），
     网络回来照常覆盖（版本没变时 render 是幂等的，DOM 复用，不闪）。 */
  const NAV_CACHE_KEY = "nav.snap.v1";

  function readNavCache() {
    try {
      const raw = window.sessionStorage.getItem(NAV_CACHE_KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      if (!o || !o.data || !Array.isArray(o.data.projects)) return null;
      return o;
    } catch (e) { return null; }
  }

  function writeNavCache(data, ver) {
    try {
      window.sessionStorage.setItem(NAV_CACHE_KEY,
        JSON.stringify({ data, ver: ver || null, at: Date.now() }));
    } catch (e) { /* 配额/隐私模式：缓存失败零影响 */ }
  }

  let cacheApplied = false;

  /* [切卡不再重拉导航 2026-10-21]
     病根：open() 每次都调 renderNav() ⇒ refresh() 无条件 `Promise.all([/api/nav,
     /api/nav/version])`。切一张卡就要多两次往返，而这两份数据**跟当前选中哪张卡
     毫无关系**——切卡只需要把已有快照按新选中重画（高亮 / 祖先展开 / 面包屑）。
     连点十张卡 = 二十个请求排队，用户看到的就是「切来切去很慢」。
     现在：`refresh({ network:false })` ＝ 只重画、零网络（快照缺失时自动退回拉取）；
     网络拉取只发生在首帧、版本探测、以及数据真的变了之后。 */
  async function refresh(opts) {
    const o = opts || {};
    const hasSnap = () => !!((store.snapshot.get().projects || []).length);
    // ① 首帧：有缓存就先出左栏，不等网络（跳转后左栏立刻有内容）
    if (!cacheApplied && appliedFor === "") {
      cacheApplied = true;
      const cached = readNavCache();
      if (cached) {
        try {
          store.snapshot.set(cached.data);
          if (cached.ver) store.snapshot.setVersion(cached.ver.version, cached.ver.by_work);
          applyDefaultFold();
          snapshotRootsWithOrphans(workId);
          render();
        } catch (e) { /* 缓存坏了大不了等网络，零阻断 */ }
      }
    }
    // ①.5 零网络重画（切卡 / 局部刷新）：快照在手就不必问后端
    if (o.network === false && hasSnap()) {
      applyDefaultFold();
      render();
      return;
    }
    // ② 正常拉取（权威数据，覆盖缓存）
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
        writeNavCache(data, v);
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
    // [定稿] 镜片期间不写状态：搜索是「换一种看法」，不是「改动你摆好的样子」。
    // 投影里一切都已摊开，没有可收放的东西——此时点箭头不改 prefs，退出搜索即原样。
    if (store.temp.getQuery && store.temp.getQuery()) return;
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
    // [定稿] 镜片期间不写状态（同 toggleProject）
    if (store.temp.getQuery && store.temp.getQuery()) return;
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

  // 虚拟列表：[定稿] 顶部文字提示行退役（好的交互不需要文字说明），header 不再挂
  const vl = createVirtualList(tree, expandRows, NAV_ROW_HEIGHT, {
    renderer: { build: buildRowEl, patch: patchRowEl },
  });

  return {
    refresh, render, probeVersion, probeRunning, toggleProject, toggleCard,
    expandRows, applyDefaultFold, enforceBudget,
    reslice: () => render(),
    vl,
  };
}
