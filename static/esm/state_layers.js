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
// [左栏接线 步2] 行高契约唯一真相＝nav.css（样式已从 style.css 拆出）：
//   卡行 30 ＝ .nav-card height（对齐样稿 .row）
//   项目行 44 ＝ .nav-phead height（上间距 8 ＋ 内容 36，对齐样稿 .row.project 36＋8）
//   虚拟滚动前缀和必须与 CSS 同源，不一致＝行错位。
export const NAV_ROW_HEIGHT = 30;
export const NAV_PHEAD_HEIGHT = 44;

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
    running: new Set(),   // [左栏接线 步2] 运行中的卡（/api/nav/running 灌入，行尾转圈）
    query: "",            // [定稿] 搜索＝镜片：当前查询串（纯内存，绝不落盘、绝不改 prefs）
  };
  const temp = {
    hover(cid) { tempMap.hoverCid = cid || null; },
    getHover() { return tempMap.hoverCid; },
    // [左栏接线 步2] 「谁在跑」集合：判据在后端写点的 active 位，前端不反推阶段
    // [修·运行中假阳性 2026-10-13] 键＝`wid:cid` 复合键。为什么必须带 wid：
    //   card_id 在**跨项目**上不唯一（实测 card_002 横跨 8 个项目、全仓 186 张卡
    //   只有 43 个编号），只按 cid 判会把所有同名卡一起点亮——症状＝「根本不在
    //   运行中的卡片一直显示运行中」。复合键与 row_model.key() 同口径，防漂移。
    running(wid, cid, on) {
      on ? tempMap.running.add(wid + ":" + cid) : tempMap.running.delete(wid + ":" + cid);
    },
    isRunning(wid, cid) { return tempMap.running.has(wid + ":" + cid); },
    // 返回「集合是否真的变了」：调用方据此决定要不要重渲。
    // 为什么：总览是周期探测（10s），若每次都重渲，用户正 hover/展开时会被无谓 patch
    // 打断——违反「状态自己变，但绝不动你的视野」。无变化＝零动作。
    setRunningSet(list) {
      // 契约：/api/nav/running → {"running":[{work_id,card_id,stage,detail}]}（后端实测形态）。
      // 缺 work_id 或 card_id 的条目一律丢弃——**宁可漏标，不可误标**：
      // 少了 spinner 只是少个提示；多亮了 spinner 是「骗你说它在跑」，比不亮更坏。
      const next = new Set();
      for (const r of list || []) {
        if (!r || typeof r !== "object") continue;
        const w = r.work_id || "";
        const c = r.card_id || "";
        if (w && c) next.add(w + ":" + c);
      }
      const same = next.size === tempMap.running.size
        && [...next].every(k => tempMap.running.has(k));
      tempMap.running = next;
      return !same;
    },
    // [定稿] 搜索＝镜片：query 只活在内存。为什么放 temp 而不是 prefs——镜片是「换一种看法」，
    // 不是「改动你摆好的样子」：清空即回到原状，靠的就是它从没写进任何持久层。
    setQuery(q) { tempMap.query = String(q || ""); },
    getQuery() { return tempMap.query; },
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

  /* ---------- 选中层 ---------- */
  /* [5715 · 修] 承载载体从 hash 迁到**路径**。
   *
   * 病根（实测 5715）：2026-10-17「阶段2 A2.2」把页面身份搬进路径（/work/{wid}/card/{cid}），
   * 选中态却还留在 hash。于是 open() 里先 select.set() 写 hash、紧接着的 replaceState 写路径
   * **把 hash 抹掉** ⇒ select.get() 恒 null ⇒
   *   ① 侧栏从不高亮当前卡（row_model 的 selected 判据失效）；
   *   ② 「收起全部保留当前链」「定位到当前卡」全失准；
   *   ③ 画布面包屑读不到选中卡。
   * 三处读点（row_model / nav_system / nav_breadcrumb）全吃这一个通道，**一处根因、三处症状**。
   *
   * 修法：选中态走路径（与「URL 即页面身份」同源）；hash 只保留作**旧链接兼容**
   * （esm/main.js 与 workspace.js bootRestore 各认一次，见 select.parse）。 */
  function parsePathCard(pathname) {
    const m = /^\/work\/([^/]+)\/card\/([^/]+)/.exec(pathname || "");
    return m ? { wid: m[1], cid: m[2] } : null;
  }
  // 复合形态 "#wid/cid"；旧形态 "#cid" ＝ 回落当前页 WORK_ID（R-09 兼容旧链接）
  function parseHash(hash) {
    const h = (hash || "").replace(/^#/, "");
    if (!h) return null;
    const i = h.indexOf("/");
    if (i > 0) return { wid: h.slice(0, i), cid: h.slice(i + 1) };
    return { wid: workId, cid: h };
  }
  function serializePath(wid, cid) { return "/work/" + wid + "/card/" + cid; }
  function serializeHash(wid, cid) { return "#" + wid + "/" + cid; }   // 旧链接形态（仅供旧链接判定/测试）
  const select = {
    set(wid, cid) {
      if (!wid || !cid) return;
      // 写路径（＝页面身份）。不写 hash 的两个原因：
      //   ① 写 hash 会被随后的 replaceState 写路径抹掉（原病根）；
      //   ② 画布页读到 hash 会 location.replace 跳回对话页（esm/main.js 的旧链接重定向）。
      if (hist) hist.replaceState(null, "", serializePath(wid, cid));
    },
    get() {
      const p = loc ? parsePathCard(loc.pathname) : null;
      if (p) return p;
      return loc ? parseHash(loc.hash) : null;   // 旧链接兜底
    },
    parse: parseHash,          // 纯函数透出（**只认 hash**：旧链接判定的唯一入口）
    parsePath: parsePathCard,  // 纯函数透出（路径形态）
    serialize: serializePath,
    serializeHash,
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
