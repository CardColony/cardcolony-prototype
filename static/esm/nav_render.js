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
import { notify } from "./api.js?v=55";
import { createRowModel } from "./row_model.js?v=36";
import { createVirtualList } from "./nav_virtual.js?v=32";
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
// [编程卡区分 2026-10-23 · 用户令「现在分不清」] 编程卡图标：代码尖括号。
// 编程卡是**执行体**（干活的叶子），和项目卡（组织）、角色卡（盯梢）一眼要能分开。
const SVG_CODER = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 4.5 2 8l3.5 3.5"/><path d="M10.5 4.5 14 8l-3.5 3.5"/></svg>';
// [UI 卡 2026-11-02] 浏览器窗口图标（执行卡家族、与 coder 尖括号区分）
const SVG_UI = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M1.5 5.5h13"/><circle cx="4" cy="4" r="0.6" fill="currentColor"/><circle cx="6.2" cy="4" r="0.6" fill="currentColor"/></svg>';
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
  return (c.card_type === "role" ? "〔角色〕"
          : c.card_type === "coder" ? "〔编程〕"
          : c.card_type === "ui" ? "〔UI〕" : "")
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
    // [职能入口 2026-10-22] 职能不是卡（不进 cards 表），行结构另立。
    if (row.kind === "function") return buildFunctionEl(row);
    // [评审入口 2026-10-29] 评审卡也不是卡（离树、不进 cards 表）。
    if (row.kind === "review") return buildReviewEl(row);
    // [静默执行卡折叠 2026-11-04] 折叠头也不是卡（伪卡 id __quiet__*）。
    if (row.kind === "quietfold") return buildQuietfoldEl(row);
    return buildCardEl(row);
  }

  /* [职能入口 2026-10-22 · 用户令「新的 fn 角色卡，前端没有任何入口，应该像以前的
     角色卡一样挂在列表上」] 职能行：项目卡列表之后单独一段。
     · 不可展开（职能没有子卡）、无 ⋯ 菜单（它不是卡，不能删/改名）；
     · 点击＝打开职能档案面板（data-fn 交给 nav_system 委托）；
     · duty=archived ⇒ 整行灰掉并标「已撤岗」，一眼看出它还挂在名单上但不再盯。 */
  function buildFunctionEl(row) {
    const f = row.card;
    const el = document.createElement("div");
    el.className = "nav-frow";
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.dataset.fn = f.function_id || "";
    el.dataset.fwid = row.wid || "";
    el.title = (f.function_name || f.function_id || "") + "（职能 · 点击查看档案）";
    el.style.paddingLeft = indentOf(row.depth) + "px";
    const segs = document.createElement("div");
    segs.className = "nav-segs";
    el.appendChild(segs);
    const ico = document.createElement("span");
    ico.className = "nav-ico fn";
    ico.innerHTML = SVG_ROLE;
    el.appendChild(ico);
    const badge = document.createElement("span");
    badge.className = "nav-fn-badge";
    badge.textContent = "职能";
    el.appendChild(badge);
    const title = document.createElement("span");
    title.className = "nav-ctitle";
    title.textContent = f.function_name || f.function_id || "";
    el.appendChild(title);
    if (String(f.duty || "") === "archived") {
      el.classList.add("nav-fn-archived");
      const off = document.createElement("span");
      off.className = "nav-fn-off";
      off.textContent = "已撤岗";
      el.appendChild(off);
    }
    return el;
  }

  /* [用户 2026-10-15 ①] 行尾槽＝时间/转圈（状态）＋ ⋯、＋（悬停操作）。   * 悬停时 ⋯ 左移一个身位、＋ 落在 ⋯ 原来的位置（＝行尾/时间原位）——CSS 用绝对定位
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
    } else if (c.card_type === "coder") {
      // [编程卡区分 2026-10-23] 编程卡＝执行体（干活的叶子）：代码图标 + 橙色行 + 「编程」徽标。
      // 与项目卡（组织、蓝色文档图标）、角色卡（盯梢、金色人形）三色三分，扫一眼就分开。
      el.classList.add("nav-coder");
      ico.classList.add("coder");
      ico.innerHTML = SVG_CODER;
      const badge = document.createElement("span");
      badge.className = "nav-coder-badge";
      badge.textContent = "编程";
      el.appendChild(ico);
      el.appendChild(badge);
    } else if (c.card_type === "ui") {
      // [UI 卡 2026-11-02] UI 卡＝内循环执行卡（浏览器验界面）：
      // 青色浏览器图标 + 「UI」徽标，与编程卡（橙）/角色卡（金）分开。
      el.classList.add("nav-ui");
      ico.classList.add("ui");
      ico.innerHTML = SVG_UI;
      const badge = document.createElement("span");
      badge.className = "nav-ui-badge";
      badge.textContent = "UI";
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

  /* [评审入口 2026-10-29 · 用户令「左侧列表页临时出入口」]
     有**进行中批次**的评审卡在左栏占一行（收口即消失，不留常驻入口）。
     · 不可展开、无 ⋯ 菜单（它不是卡，不能删/改名）；
     · 点击＝打开评审卡对话页（data-review 交给 nav_system 委托）；
     · 行尾显示 `通过数/总数`——左栏一眼看出这场评审走到哪了。 */
  function buildReviewEl(row) {
    const r = row.card;
    const el = document.createElement("div");
    el.className = "nav-rvrow";
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.dataset.review = r.review_card_id || "";
    el.dataset.rwid = row.wid || "";
    el.title = `评审 · ${r.target_title || r.target || ""}（${r.passed || 0}/${r.total || 0}）· 点击进入`;
    el.style.paddingLeft = indentOf(row.depth) + "px";
    const segs = document.createElement("div");
    segs.className = "nav-segs";
    el.appendChild(segs);
    const ico = document.createElement("span");
    ico.className = "nav-ico rv";
    ico.innerHTML = SVG_DOC;
    el.appendChild(ico);
    const badge = document.createElement("span");
    badge.className = "nav-rv-badge";
    badge.textContent = "评审";
    el.appendChild(badge);
    const title = document.createElement("span");
    title.className = "nav-ctitle";
    title.textContent = r.target_title || r.target || "评审";
    el.appendChild(title);
    const cnt = document.createElement("span");
    cnt.className = "nav-rv-count";
    cnt.textContent = `${r.passed || 0}/${r.total || 0}`;
    el.appendChild(cnt);
    return el;
  }

  /* [静默执行卡折叠 2026-11-04 · 用户令「左侧列表，静默不动的编程卡、qa卡、
     产品经理卡，都折叠显示」] 折叠头行：挂在每个兄弟段的末尾，伪卡
     card_id="__quiet__<父id>"——展开态照常走 prefs.getExpanded/toggleCard
     （伪 id 在 prefs 里独立成键，不污染任何真卡）；
     child_count=0 ⇒ toggleCard 展开分支的 loadChildren 走「叶：零网络」；
     它不是卡没有对话页，整行点击在委托层被 data-qfold 拦成开合开关。 */
  function buildQuietfoldEl(row) {
    const el = document.createElement("div");
    el.className = "nav-card nav-qfold";
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.dataset.qfold = row.card.card_id;
    el.style.paddingLeft = indentOf(row.depth) + "px";
    const segs = document.createElement("div");
    segs.className = "nav-segs";
    el.__segs = segs;
    el.appendChild(segs);
    // [2026-11-04 · 用户令] 不要可展开箭头——但**箭头列 24px 要占位**
    // （leaf＝不可见但仍占宽）：删了图标会左偏 24px、和上下卡行图标对不齐，
    // 树线锚点也断。整行就是开关（data-qfold 委托成开合）。
    const chev = document.createElement("span");
    chev.className = "nav-chev leaf";
    el.appendChild(chev);
    const ico = document.createElement("span");
    ico.className = "nav-ico quiet";
    ico.innerHTML = SVG_DOC;
    el.appendChild(ico);
    // [2026-11-04 · 用户令] 样式二版：不要「静默」「静默执行卡」文字——
    // 灰度图标 + ··· N ··· 就够（信息降噪）；类型明细只留 tooltip。
    const title = document.createElement("span");
    title.className = "nav-ctitle nav-qfold-dots";
    el.__title = title;
    el.appendChild(title);
    patchQuietfoldContent(el, row);
    return el;
  }

  function patchQuietfoldContent(el, row) {
    const pr = row.props(store);
    renderSegs(el.__segs, rowSegs({
      depth: row.depth, guides: row.guides, first: row.first, last: row.last,
      openKids: pr.expanded, H: row.rowHeight(),
    }));
    const qs = row.card.__quiet || [];
    const cnt = { coder: 0, qa_card: 0, pm_card: 0 };
    for (const c of qs) if (cnt[c.card_type] != null) cnt[c.card_type]++;
    el.__title.textContent = `- ${qs.length} -`;
    const parts = [];
    if (cnt.coder) parts.push(`coder ${cnt.coder}`);
    if (cnt.qa_card) parts.push(`QA ${cnt.qa_card}`);
    if (cnt.pm_card) parts.push(`PM ${cnt.pm_card}`);
    el.title = `quiet cards: ${qs.length} (${parts.join(", ")}) - click to expand/collapse`;
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
    /* [职能行重叠 2026-10-22 · 真根因] 职能行没有 __chev / __tail / __segs 这套卡行
       结构，落到下面的 patchCardContent 会抛 TypeError；而 patch 是在
       nav_virtual.reslice() 的循环里逐行调的 ⇒ **一行抛异常，后面所有行的
       `el.style.top` 就不再被设置**，全部堆在旧位置 —— 表现就是「职能一多就叠在
       一起」。职能行内容不常变（名字/状态），直接跳过 patch，只同步两个类。 */
    if (row.kind === "function") {
      el.classList.toggle("current", false);
      el.classList.toggle("nav-last", !!row.lastInProject);
      return true;
    }
    /* [评审行 2026-10-29 · 同一个坑] 评审行是 buildReviewEl 自建的结构
       （__segs / __badge / __count），没有 __chev / __tail；落到下面的
       patchCardContent 会抛 TypeError，而 patch 在 reslice 循环里逐行调
       ⇒ 一行抛错，后面所有行的 top 就不再设置（表现：左栏只剩第一个项目、
       父卡行消失、评审少几条——全是同一个断点）。这里同职能行一样跳过 patch，
       只同步两个类。 */
    if (row.kind === "review") {
      el.classList.toggle("current", false);
      el.classList.toggle("nav-last", !!row.lastInProject);
      return true;
    }
    // [静默执行卡折叠 2026-11-04] 折叠头同评审/职能：自有结构（无 __tail），
    // 但计数/开合是活的 ⇒ 用自己的 patch 而不是跳过。
    if (row.kind === "quietfold") {
      patchQuietfoldContent(el, row);
      el.classList.toggle("current", false);
      el.classList.toggle("nav-last", !!row.lastInProject);
      return true;
    }
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
    // [静默执行卡折叠] 折叠组成员降级显示（用户令：置灰＋小一号）——
    // .nav-quiet 随投影实时挂/摘：卡一起跑出折叠组，类也跟着掉。
    el.classList.toggle("nav-quiet", !!row.quiet);
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
      // [评审入口 2026-10-29] 有进行中批次的评审卡——临时入口，收口即消失。
      // 排在卡列表之后、职能之前：它跟着"事"走，不是"人"。
      for (const r of (p.reviews || [])) {
        if (!r || !r.review_card_id) continue;
        rows.push(createRowModel(r, store, {
          kind: "review", depth: 0, wid: p.work_id,
        }));
      }
      // [职能入口 2026-10-22] 职能不进 cards 表 ⇒ 单独一段，排在全部卡之后。
      for (const f of (p.functions || [])) {
        if (!f || !f.function_id) continue;
        rows.push(createRowModel(f, store, {
          kind: "function", depth: 0, wid: p.work_id,
        }));
      }
      if (rows.length > start) rows[rows.length - 1].lastInProject = true;
    }
    // 每个项目的最后一行挂 .nav-last（卡片底边＋下圆角）——上面逐项目标记
    return rows;
  }

  // [静默执行卡折叠 2026-11-04 · 用户令] 这三类执行卡「不在跑」时折起来。
  const FOLD_QUIET_TYPES = { coder: "编程", qa_card: "QA", pm_card: "PM" };

  function pushSegment(rows, wid, parentCid, depth, visited, guides) {
    const cards = store.snapshot.getChildren(wid, parentCid);
    if (!cards) return;   // 段未加载：父行由 props 投影出 loading 态
    const visible = cards.filter(c => !isToolCard(c));   // [定稿] 工具卡不进侧栏
    /* [静默执行卡折叠] 分区：静默（不在 running 集）的 coder/qa_card/pm_card
       不进主流，折到段尾一行「静默执行卡 ×N」；在跑的、当前打开的保持原位。
       判据用 store.temp.isRunning（与行尾转圈同一份数据）⇒ 卡一起跑就自己
       出折，跑完回落——不靠用户手动归位。 */
    const curCid = (getCardId && getCardId()) || "";
    const normal = [], quiet = [];
    for (const c of visible) {
      if (FOLD_QUIET_TYPES[c.card_type] && c.card_id !== curCid
          && !store.temp.isRunning(wid, c.card_id)) quiet.push(c);
      else normal.push(c);
    }
    const pushOne = (c, i, arr, dep, gds, inQuiet) => {
      if (visited.has(c.card_id)) return;   // 数据环防护
      visited.add(c.card_id);
      // [静默执行卡折叠] normal 末位后面还挂着折叠行 ⇒ 它不是「最后可见
      // 兄弟」，last 必须 false——rowSegs 只在 !last 时画下行线，否则树线
      // 断在它中线、折叠行像悬空的（用户报「折叠行与上间距太宽」的根因）。
      const isLast = i === arr.length - 1 && (inQuiet || !quiet.length);
      const row = createRowModel(c, store, {
        depth: dep, wid, guides: gds,
        first: i === 0, last: isLast,
        // [静默执行卡折叠] 折叠组成员带 quiet 标记 → 置灰＋26px 小行
        // （用户令：静默的卡片置灰、缩小一号，层级清楚）。
        quiet: !!inQuiet,
      });
      rows.push(row);
      if (store.prefs.getExpanded(wid, c.card_id)) {
        pushSegment(rows, wid, c.card_id, dep + 1, visited,
                    childGuides({ depth: dep, last: isLast, guides: gds }));
      }
    };
    normal.forEach((c, i) => pushOne(c, i, normal, depth, guides));
    if (!quiet.length) return;
    // 折叠头：伪卡（不是真卡），展开态走 prefs 常规键位。
    const foldId = "__quiet__" + (parentCid || "");
    const pseudo = { card_id: foldId, child_count: 0, work_id: wid };
    pseudo.__quiet = quiet;   // patchQuietfoldContent 现算计数
    rows.push(createRowModel(pseudo, store, {
      kind: "quietfold", depth, wid, guides,
      // [用户令] 折叠行不画「行顶横肘」（first=false）——那条横线像上边框，
      // 视觉上把折叠行和上一行隔开=「间距宽」；它只靠竖线挂上，不留横边。
      first: false, last: true,
      // [用户令] 折叠行自身也缩到 26px——和成员行同高，与上方行的
      // 间距窄一档，视觉上是「一条分隔线」不是一张卡。
      quiet: true,
    }));
    if (store.prefs.getExpanded(wid, foldId)) {
      const qGuides = childGuides({ depth, last: true, guides });
      quiet.forEach((c, i) => pushOne(c, i, quiet, depth + 1, qGuides, true));
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
