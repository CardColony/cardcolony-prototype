/* esm/nav_find.js —— [S3/T2] 找通道：搜索独立层（R-24）＋最近访问（R-25）。
 *
 * R-24 搜索：query 存模块内存（temp 语义：刷新即丢，不动 S2 temp API 面）→
 *   filterCards 纯内存过滤 snapshot.projects[].cards[]（title 包含、大小写不敏感）→
 *   独立结果层叠加在 navTree 之上（不是就地过滤树，navTree 本体 DOM 全程零触碰）；
 *   关闭 → 结果层消失 → 树恢复搜索前展开态（树从未被改过，无需回滚）。
 *   结果行＝卡标题＋项目名＋父路径简述；点击＝跳转＋关搜索（复用 R-20 叶子跳转路径）。
 *   零网络：数据全部来自快照（V3-F3）。
 * R-25 最近访问：storageGet/storageSet 键 nav.recent.v1；{wid,cid,title,ts} 上限 10 条、
 *   同卡去重更新 ts、倒序；UI＝查找面板「最近」区，点击直达。
 */
import { storageGet, storageSet } from "./state_layers.js?v=27";

export const NAV_RECENT_KEY = "nav.recent.v1";
export const NAV_RECENT_MAX = 10;

/* 纯函数：内存过滤（V3-F1 单测直打）。命中项带 wid/cid/title/project/path。 */
export function filterCards(query, projects) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return [];
  const out = [];
  for (const p of (projects || [])) {
    const cards = (p && p.cards) || [];
    const byId = new Map(cards.map(c => [c.card_id, c]));
    for (const c of cards) {
      const title = c.title || "";
      if (!title.toLowerCase().includes(q)) continue;
      out.push({
        wid: p.work_id,
        cid: c.card_id,
        title,
        project: (p && p.title) || "",
        path: pathBrief(c, byId),
      });
    }
  }
  return out;
}

/* 父路径简述：祖先标题链（不含项目名），最多取最近两级、更深以 … 起头 */
function pathBrief(card, byId) {
  const parts = [];
  let cur = card && card.parent ? byId.get(card.parent) : null;
  let guard = 0;
  while (cur && guard < 32) {
    parts.unshift(cur.title || "");
    cur = cur.parent ? byId.get(cur.parent) : null;
    guard++;
  }
  if (parts.length > 2) return "…/" + parts.slice(-2).join("/");
  return parts.join("/");
}

/* [R-25] 最近访问（V3-F4 单测直打）：storage 注入（生产 localStorage，测试 mock） */
export function createRecent(storage) {
  function read() {
    try {
      const v = JSON.parse(storageGet(storage, NAV_RECENT_KEY, "[]") || "[]");
      return Array.isArray(v) ? v.filter(e => e && e.wid && e.cid) : [];
    } catch (e) { return []; }
  }
  function write(list) {
    storageSet(storage, NAV_RECENT_KEY, JSON.stringify(list.slice(0, NAV_RECENT_MAX)));
  }
  return {
    list: read,
    record(wid, cid, title) {
      if (!wid || !cid) return;
      const rest = read().filter(e => !(e.wid === wid && e.cid === cid));   // 同卡去重
      rest.unshift({ wid, cid, title: title || "", ts: Date.now() });        // 倒序：最新在前
      write(rest);
    },
    clear() { storageSet(storage, NAV_RECENT_KEY, "[]"); },
  };
}

/* 面板装配（[定稿 2026-10-12] 搜索＝镜片）：
 * 输入框的每一次输入 → onQuery(q)：由渲染层重投影成「剪枝树」（命中＋其到根的路径），
 *   命中标黄、清空即原样还原（镜片从不改任何状态，靠的是 query 只活在 temp 内存）。
 * Esc＝清空并还原；Enter＝打开第一条命中（复用 onJump 的既有跳转路径）。
 * 为什么不再用独立结果层：那样你得学一套和平时完全不同的样子；剪枝树让你用同一双眼睛。
 * recent（最近访问）保留为纯记录口（recordRecent），不再是搜索框的默认面板。 */
export function createNavFind(deps) {
  const input = deps.input;
  const store = deps.store;
  const onJump = deps.onJump || (() => {});
  const onQuery = deps.onQuery || (() => {});
  const recent = deps.recent || createRecent(null);

  function hits() {
    const q = input ? input.value : "";
    return filterCards(q, (store.snapshot.get().projects) || []);
  }

  if (input) {
    input.addEventListener("input", () => { onQuery(input.value); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        input.value = "";
        onQuery("");
        input.blur();
        return;
      }
      if (e.key === "Enter") {
        const list = hits();
        if (!list.length) return;
        const h = list[0];
        input.value = "";
        onQuery("");
        input.blur();
        onJump(h.wid, h.cid);
      }
    });
  }

  return {
    hits,
    // 兼容既有调用点（collapseAllKeepPath / locateCurrentCard 里的 navFind.close()）：
    // 镜片没有独立层可关，改为「清空查询并还原树」。
    close() {
      if (input && input.value) input.value = "";
      onQuery("");
    },
    isOpen() { return !!(input && input.value); },
    filterCards,
    recordRecent: (wid, cid, title) => recent.record(wid, cid, title),
  };
}
