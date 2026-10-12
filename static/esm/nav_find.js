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
import { escapeHtml } from "./util.js";
import { storageGet, storageSet } from "./state_layers.js";

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

function relTime(ts) {
  const s = Math.max(0, Math.floor((Date.now() - (ts || 0)) / 1000));
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return Math.floor(s / 86400) + " 天前";
}

/* 面板装配：搜索框（work.html #navTop 内）＋独立结果层（host 内叠加层）。
 * onJump(wid,cid) 由 workspace.js 给（叶子跳转路径）；recent 可注入（测试/mock）。 */
export function createNavFind(deps) {
  const input = deps.input;
  const host = deps.host;
  const store = deps.store;
  const onJump = deps.onJump || (() => {});
  const recent = deps.recent || createRecent(null);
  const RESULT_CAP = 50;

  // 独立结果层：navTree 的兄弟叠加层（V3-F2 树 mutation 计 0 的前提）
  const layer = document.createElement("div");
  layer.className = "nav-find-layer";
  layer.style.display = "none";
  host.appendChild(layer);

  function renderPanel() {
    const q = input ? input.value : "";
    const projects = (store.snapshot.get().projects) || [];
    layer.innerHTML = "";
    if (String(q).trim()) {
      const hits = filterCards(q, projects);
      const head = document.createElement("div");
      head.className = "nav-find-head";
      head.textContent = hits.length ? "结果 " + hits.length : "无匹配卡片";
      layer.appendChild(head);
      for (const h of hits.slice(0, RESULT_CAP)) {
        const row = document.createElement("div");
        row.className = "nav-find-item";
        row.innerHTML =
          `<div class="nav-find-title">${escapeHtml(h.title)}</div>` +
          `<div class="nav-find-meta">${escapeHtml(h.project)}` +
          (h.path ? ` · ${escapeHtml(h.path)}` : "") + `</div>`;
        row.onclick = () => { close(); onJump(h.wid, h.cid); };
        layer.appendChild(row);
      }
    } else {
      const head = document.createElement("div");
      head.className = "nav-find-head";
      head.textContent = "最近访问";
      layer.appendChild(head);
      const list = recent.list();
      if (!list.length) {
        const empty = document.createElement("div");
        empty.className = "nav-find-meta nav-find-empty";
        empty.textContent = "（暂无最近访问）";
        layer.appendChild(empty);
      }
      for (const r of list) {
        const row = document.createElement("div");
        row.className = "nav-find-item";
        row.innerHTML =
          `<div class="nav-find-title">${escapeHtml(r.title || r.cid)}</div>` +
          `<div class="nav-find-meta">${escapeHtml(r.wid)} · ${relTime(r.ts)}</div>`;
        row.onclick = () => { close(); onJump(r.wid, r.cid); };
        layer.appendChild(row);
      }
    }
    layer.style.display = "";
    // 贴搜索行下缘（host=.app-nav 为 position:relative 外壳）
    const navTop = document.getElementById("navTop");
    if (navTop) layer.style.top = (navTop.offsetTop + navTop.offsetHeight + 4) + "px";
  }

  function close() {
    layer.style.display = "none";
    layer.innerHTML = "";
  }
  function isOpen() { return layer.style.display !== "none"; }

  if (input) {
    input.addEventListener("input", () => { renderPanel(); });
    input.addEventListener("focus", () => { renderPanel(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { close(); input.blur(); }
    });
    // 点击层外（含树上/页面上）收起结果层
    document.addEventListener("mousedown", (e) => {
      if (!isOpen()) return;
      if (layer.contains(e.target) || e.target === input) return;
      close();
    });
  }

  return {
    openPanel: renderPanel,
    close,
    isOpen,
    layer,
    filterCards,
    recordRecent: (wid, cid, title) => recent.record(wid, cid, title),
  };
}
