/* esm/state_layers.js —— [S2/R-07/R-08/R-09] 导航状态三层容器 + 快照层。
 *
 * 四区唯一 API（任务书 T1）：
 *   temp     —— 临时层（纯内存，不持久）：hover / loading / error / loaded
 *   prefs    —— 偏好层（localStorage，KEY=nav.prefs.v1，读写带 JSON 容错）：展开态
 *   select   —— 选中层（URL hash，复合键 wid/cid；旧 cid-only hash 回落当前页 WORK_ID）
 *   snapshot —— 快照层（服务端数据投影）：/api/nav 响应 + version/by_work + 按需 children 段
 *
 * 依赖全部注入（storage / location / history / workId），生产由 workspace.js 传 WORK_ID，
 * 测试传 mock——模块本体不摸任何全局。
 */

export const NAV_PREFS_KEY = "nav.prefs.v1";
export const NAV_ROW_HEIGHT = 38;      // [S2/R-17] 卡行固定行高（与 style.css .nav-card 同源）
export const NAV_PHEAD_HEIGHT = 30;    // 项目行行高（与 style.css .nav-phead 同源，现状不动）

function defaultPrefs() {
  return { expanded: {}, projects: [], autoOrganize: true };
}

export function createNavStateStore(deps = {}) {
  const storage = deps.storage
    || (typeof localStorage !== "undefined" ? localStorage : null);
  const loc = deps.location
    || (typeof location !== "undefined" ? location : null);
  const hist = deps.history
    || (typeof history !== "undefined" ? history : null);
  const workId = deps.workId || "";

  /* ---------- 临时层（内存） ---------- */
  const tempMap = {
    hoverCid: null,
    loading: new Map(),   // cid → bool
    error: new Map(),     // cid → msg|null
    loaded: new Map(),    // cid → bool
  };
  const temp = {
    hover(cid) { tempMap.hoverCid = cid || null; },
    getHover() { return tempMap.hoverCid; },
    loading(cid, on) { on ? tempMap.loading.set(cid, true) : tempMap.loading.delete(cid); },
    isLoading(cid) { return tempMap.loading.get(cid) === true; },
    error(cid, msg) { msg ? tempMap.error.set(cid, msg) : tempMap.error.delete(cid); },
    getError(cid) { return tempMap.error.get(cid) || null; },
    loaded(cid, on) { tempMap.loaded.set(cid, on === true); },
    isLoaded(cid) { return tempMap.loaded.get(cid) === true; },
  };

  /* ---------- 偏好层（localStorage 持久化，坏 JSON 容错） ---------- */
  function readPrefs() {
    if (!storage) return defaultPrefs();
    try {
      const raw = storage.getItem(NAV_PREFS_KEY);
      if (!raw) return defaultPrefs();
      const v = JSON.parse(raw);
      if (!v || typeof v !== "object") return defaultPrefs();
      return {
        expanded: (v.expanded && typeof v.expanded === "object") ? v.expanded : {},
        projects: Array.isArray(v.projects) ? v.projects : [],
        autoOrganize: v.autoOrganize !== false,
      };
    } catch (e) {
      return defaultPrefs();   // 坏 JSON：返回默认值不抛错（R-08）
    }
  }
  function writePrefs(p) {
    if (!storage) return;
    try { storage.setItem(NAV_PREFS_KEY, JSON.stringify(p)); } catch (e) { /* 零阻断 */ }
  }
  const prefs = {
    getExpanded(wid, cid) {
      const w = readPrefs().expanded[wid];
      return (w && w[cid]) || null;      // null | "manual" | "system"
    },
    setExpanded(wid, cid, source) {
      const p = readPrefs();
      if (!p.expanded[wid]) p.expanded[wid] = {};
      if (source) p.expanded[wid][cid] = source;
      else delete p.expanded[wid][cid];   // source=null/undefined ＝ 折叠
      writePrefs(p);
    },
    // 展开条目快照（插入序）：[{cid, source}]——兜底 100 与祖先展开记账用
    listExpanded(wid) {
      const w = readPrefs().expanded[wid] || {};
      return Object.keys(w).map(cid => ({ cid, source: w[cid] }));
    },
    systemCount(wid) {
      return readPrefs().expanded[wid]
        ? Object.values(readPrefs().expanded[wid]).filter(s => s === "system").length
        : 0;
    },
    getProjectOpen(wid) {
      return readPrefs().projects.indexOf(wid) >= 0;
    },
    setProjectOpen(wid, open) {
      const p = readPrefs();
      const i = p.projects.indexOf(wid);
      if (open && i < 0) p.projects.push(wid);
      if (!open && i >= 0) p.projects.splice(i, 1);
      writePrefs(p);
    },
  };

  /* ---------- 选中层（URL hash 复合键） ---------- */
  // 复合形态 "#wid/cid"；旧形态 "#cid" ＝ 回落当前页 WORK_ID（R-09 兼容旧链接）
  function parseHash(hash) {
    const h = (hash || "").replace(/^#/, "");
    if (!h) return null;
    const i = h.indexOf("/");
    if (i > 0) return { wid: h.slice(0, i), cid: h.slice(i + 1) };
    return { wid: workId, cid: h };
  }
  function serializeHash(wid, cid) { return "#" + wid + "/" + cid; }
  const select = {
    set(wid, cid) {
      if (!wid || !cid) return;
      if (hist) hist.replaceState(null, "", serializeHash(wid, cid));
    },
    get() { return loc ? parseHash(loc.hash) : null; },
    parse: parseHash,      // 纯函数透出（restoreFromHash / 测试共用）
    serialize: serializeHash,
  };

  /* ---------- 快照层（服务端数据投影） ---------- */
  let snap = { version: null, by_work: {}, projects: [] };
  const childrenStore = {};   // wid → { parentCid: cards[] }（按需加载段；折叠即丢）
  const snapshot = {
    set(navResponse) {
      snap = {
        version: snap.version,
        by_work: snap.by_work,
        projects: (navResponse && navResponse.projects) || [],
      };
    },
    get() { return snap; },
    setVersion(version, by_work) {
      snap.version = version || null;
      snap.by_work = by_work || {};
    },
    project(wid) {
      return (snap.projects || []).find(p => p.work_id === wid) || null;
    },
    // /api/nav 全量 cards 里 parent 直系段（系统展开/祖先链的零网络数据源）
    cardsOf(wid, parent) {
      const p = this.project(wid);
      if (!p) return [];
      const root = p.work_card_id || "";
      return (p.cards || []).filter(c => {
        const pa = c.parent || "";
        return parent === "" ? (pa === "" || pa === root) : pa === parent;
      });
    },
    setChildren(wid, parent, cards) {
      if (!childrenStore[wid]) childrenStore[wid] = {};
      childrenStore[wid][parent] = cards || [];
    },
    getChildren(wid, parent) {
      return (childrenStore[wid] && childrenStore[wid][parent]) || null;
    },
    // 折叠即丢（R-14）：该段连同所有后代段级联丢弃
    dropChildren(wid, parent) {
      if (!childrenStore[wid]) return;
      const kids = childrenStore[wid][parent];
      delete childrenStore[wid][parent];
      if (Array.isArray(kids)) {
        for (const k of kids) this.dropChildren(wid, k.card_id);
      }
    },
  };

  return { temp, prefs, select, snapshot };
}

/* ---------- 裸 localStorage 通用包装（workspace.js 内禁裸读写，V-S5 静态闸） ---------- */
export function storageGet(storage, key, fallback) {
  try {
    const raw = (storage || (typeof localStorage !== "undefined" ? localStorage : null)).getItem(key);
    return raw === null ? fallback : raw;
  } catch (e) { return fallback; }
}
export function storageSet(storage, key, value) {
  try {
    (storage || (typeof localStorage !== "undefined" ? localStorage : null)).setItem(key, value);
  } catch (e) { /* 零阻断 */ }
}
