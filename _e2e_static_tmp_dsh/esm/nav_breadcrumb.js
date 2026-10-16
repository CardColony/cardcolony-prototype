/* esm/nav_breadcrumb.js —— [S3/T3] 定位族：面包屑（R-27）＋吸顶路径条（R-18）＋
 * 祖先链数据源（R-26「定位到当前卡」/ R-28「收起全部保留路径」共用）。
 *
 * buildBreadcrumb(snapshot, wid, cid)：当前卡祖先链段数组（项目→根卡→…→当前卡）；
 *   深度口径与 row_model 一致（根卡直系＝depth 0），depth < 3 返回 []（浅树不渲染，
 *   不占空间，V3-L1）。
 * ancestorCards(snapshot, wid, cid)：无深度门的卡对象链（含当前卡）——workspace.js
 *   一键收起/定位只收非路径分支的 pathIds 来源。
 * 吸顶（R-18）：同一 UI 两态——静止＝文档流槽位（navCrumbBox 恒定占位防跳动）；
 *   滚动（scrollTop>2）＝position:absolute 悬浮可视区顶部（.app-nav 本就 relative，
 *   槽位 top=box.offsetTop，零布局跳变）。不设第二套 UI。
 * headerH 消费（V3-L4，S2 预留口）：nav_virtual 暴露的 headerH()（.nav-hint 在流头部
 *   高度）用于「定位到当前卡」滚动数学：scrollTop = headerH + 行前缀和 top − 吸顶盖高。
 *
 * 段文本一律 textContent 写入（不拼 HTML），无需 escapeHtml。
 */

export const NAV_CRUMB_MIN_DEPTH = 3;   // 深树才显：当前卡 depth ≥ 3 才渲染面包屑
const STICKY_SCROLL_ON = 2;             // 吸顶阈值：树滚动超过 2px 即吸顶
const CHAIN_GUARD = 64;                 // 祖先链环防护（脏数据不死循环）

/* 祖先链（含当前卡）：[根层卡, …, 父卡, 当前卡]；cid 不在快照 → []（与 R-21 存在性同源） */
export function ancestorCards(snapshot, wid, cid) {
  const p = snapshot && snapshot.project ? snapshot.project(wid) : null;
  if (!p || !cid) return [];
  const byId = new Map((p.cards || []).map(c => [c.card_id, c]));
  const chain = [];
  let cur = byId.get(cid);
  let guard = 0;
  while (cur && guard < CHAIN_GUARD) {
    chain.unshift(cur);
    cur = cur.parent ? byId.get(cur.parent) : null;
    guard++;
  }
  return chain;
}

/* 面包屑段数组（V3-L1 单测直打）：[项目, 根卡, …, 当前卡]；depth < 3 → [] */
export function buildBreadcrumb(snapshot, wid, cid) {
  const p = snapshot && snapshot.project ? snapshot.project(wid) : null;
  if (!p) return [];
  return crumbSegments(snapshot, wid, cid, false);
}

function crumbSegments(snapshot, wid, cid, bypassDepth) {
  const p = snapshot && snapshot.project ? snapshot.project(wid) : null;
  if (!p) return [];
  const chain = ancestorCards(snapshot, wid, cid);
  if (!chain.length) return [];
  const depth = chain.length - 1;   // chain[0]＝根层卡（depth 0，row_model 同口径）
  if (!bypassDepth && depth < NAV_CRUMB_MIN_DEPTH) return [];
  const segs = [{ kind: "project", wid, cid: p.work_card_id || "", title: p.title || "" }];
  for (const c of chain) segs.push({ kind: "card", wid, cid: c.card_id, title: c.title || "" });
  return segs;
}

/* 面板装配：#navCrumbBox（恒占位槽）内 #navCrumb。onJump(wid,cid) 由 workspace.js 给；
 * vl＝createVirtualList 实例（headerH 消费点，供定位滚动数学取数）。 */
export function createNavBreadcrumb(deps) {
  const box = deps.box;
  const el = deps.el;
  const store = deps.store;
  const workId = deps.workId || "";
  const onJump = deps.onJump || (() => {});
  const getTree = deps.getTree || (() => null);
  const vl = deps.vl || null;

  /* 当前渲染目标：显式 override（R-21 灰态回父卡）＞选中层 hash＞无 */
  function currentTarget(overrideWid, overrideCid) {
    if (overrideCid) return { wid: overrideWid || workId, cid: overrideCid };
    const sel = store.select.get();
    if (sel && sel.cid) return sel;
    return null;
  }

  /* 重算面包屑。opts：{overrideWid, overrideCid, bypassDepth}（R-21 回父卡不受深度门限） */
  function render(opts = {}) {
    if (!el || !box) return;
    const t = currentTarget(opts.overrideWid, opts.overrideCid);
    const segs = t ? crumbSegments(store.snapshot, t.wid, t.cid, !!opts.bypassDepth) : [];
    if (!segs.length) {
      box.style.display = "none";
      el.innerHTML = "";
      setSticky(false);
      return;
    }
    el.innerHTML = "";
    segs.forEach((s, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "nav-crumb-sep";
        sep.textContent = "›";
        el.appendChild(sep);
      }
      const seg = document.createElement(s.cid ? "button" : "span");
      seg.className = "nav-crumb-seg" + (s.kind === "project" ? " proj" : "");
      seg.textContent = s.title || s.cid || "（未命名）";
      if (s.cid) {
        seg.dataset.wid = s.wid;
        seg.dataset.cid = s.cid;
        seg.onclick = () => onJump(s.wid, s.cid);
      }
      el.appendChild(seg);
    });
    // 槽位恒定：absolute 化吸顶时布局零跳动
    box.style.display = "";
    box.style.height = el.offsetHeight + "px";
  }

  /* 吸顶盖高＝槽位高（定位滚动数学用；hidden 时 0） */
  function stickyCoverPx() {
    return (box && box.style.display !== "none") ? (box.offsetHeight || 0) : 0;
  }

  function setSticky(on) {
    if (!el || !box || box.style.display === "none") return;
    const was = el.classList.contains("sticky");
    if (!!on === was) return;
    if (on) {
      el.style.top = box.offsetTop + "px";
      el.classList.add("sticky");
    } else {
      el.classList.remove("sticky");
      el.style.top = "";
    }
  }

  /* 树滚动联动（workspace.js 在 navTree scroll 上转发） */
  function onTreeScroll() {
    const tree = getTree();
    if (!tree) return;
    setSticky(tree.scrollTop > STICKY_SCROLL_ON);
  }

  /* [R-26] headerH 消费：在流头部（.nav-hint）高度——定位滚动数学的行顶偏移项 */
  function headerHeight() {
    return vl && typeof vl.headerH === "function" ? vl.headerH() : 0;
  }

  return { render, setSticky, stickyCoverPx, onTreeScroll, headerHeight, buildBreadcrumb };
}
