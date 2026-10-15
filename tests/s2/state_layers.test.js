/* tests/s2/state_layers.test.js —— [S2] V-S1~V-S4：状态三层容器 + 行模型零状态投影。
 * 跑法：node --test tests/s2/state_layers.test.js
 * 依赖注入：storage/location/history 全 mock，模块本体不摸全局（jsdom 非必需）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createNavStateStore, NAV_PREFS_KEY, NAV_ROW_HEIGHT,
  storageGet, storageSet,
} from "../../static/esm/state_layers.js";
import { createRowModel } from "../../static/esm/row_model.js";

function mockStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    _m: m,
  };
}
const mockLocation = (hash) => ({ hash: hash || "" });
// replaceState 回写 location.hash（浏览器语义：URL 即状态；select.get 才能读到）
const mockHistory = () => ({
  calls: [],
  replaceState(_s, _t, url) { this.calls.push(url); this._loc.hash = url || ""; },
  _loc: null,
});

function makeStore(over = {}) {
  const storage = over.storage || mockStorage();
  const location = over.location || mockLocation();
  const history = over.history || mockHistory();
  history._loc = location;
  const store = createNavStateStore({
    storage, location, history, workId: over.workId || "work_001",
  });
  return { store, storage, location, history };
}

/* ---------- V-S1（R-07）：四区接口齐全；temp 不落盘；prefs 落盘；select 写 hash ---------- */

test("V-S1a 四区接口齐全（任务书 T1 方法集逐项在场）", () => {
  const { store } = makeStore();
  for (const fn of ["hover", "getHover", "loading", "isLoading", "error", "getError", "loaded", "isLoaded"]) {
    assert.equal(typeof store.temp[fn], "function", "temp." + fn);
  }
  for (const fn of ["getExpanded", "setExpanded", "getProjectOpen", "setProjectOpen", "listExpanded", "systemCount"]) {
    assert.equal(typeof store.prefs[fn], "function", "prefs." + fn);
  }
  for (const fn of ["set", "get", "parse", "serialize"]) {
    assert.equal(typeof store.select[fn], "function", "select." + fn);
  }
  for (const fn of ["set", "get", "setVersion", "project", "cardsOf", "setChildren", "getChildren", "dropChildren"]) {
    assert.equal(typeof store.snapshot[fn], "function", "snapshot." + fn);
  }
  assert.equal(NAV_ROW_HEIGHT, 30);   // [对齐样稿 2026-10-12] 卡行 38→30（＝nav.css .nav-card）
});

test("V-S1b 临时层 set 不落 localStorage（纯内存）", () => {
  const { store, storage } = makeStore();
  const before = JSON.stringify([...storage._m]);
  store.temp.hover("card_1");
  store.temp.loading("card_1", true);
  store.temp.error("card_1", "x");
  store.temp.loaded("card_1", true);
  assert.equal(JSON.stringify([...storage._m]), before, "storage 零写入");
  assert.equal(store.temp.getHover(), "card_1");
  assert.ok(store.temp.isLoading("card_1"));
  assert.equal(store.temp.getError("card_1"), "x");
  assert.ok(store.temp.isLoaded("card_1"));
});

test("V-S1c 偏好层 set 落 localStorage（KEY=nav.prefs.v1）", () => {
  const { store, storage } = makeStore();
  store.prefs.setExpanded("work_001", "card_9", "manual");
  assert.ok(storage._m.has(NAV_PREFS_KEY), "nav.prefs.v1 在 localStorage");
  const raw = JSON.parse(storage._m.get(NAV_PREFS_KEY));
  assert.equal(raw.expanded.work_001.card_9, "manual");
  store.prefs.setProjectOpen("work_002", true);
  assert.deepEqual(JSON.parse(storage._m.get(NAV_PREFS_KEY)).projects, ["work_002"]);
});

test("V-S1d 选中层 set 写 hash（复合形态）", () => {
  const { store, history } = makeStore();
  store.select.set("work_002", "card_3");
  assert.deepEqual(history.calls, ["#work_002/card_3"]);
});

/* ---------- V-S2（R-08）：持久化恢复 + 坏 JSON 容错 ---------- */

test("V-S2a setExpanded 落盘后重建 store（模拟刷新）展开态恢复", () => {
  const storage = mockStorage();
  const s1 = createNavStateStore({ storage, workId: "work_001" });
  s1.prefs.setExpanded("work_001", "card_5", "manual");
  s1.prefs.setExpanded("work_001", "card_7", "system");
  s1.prefs.setProjectOpen("work_001", true);
  const s2 = createNavStateStore({ storage, workId: "work_001" });   // 重建＝模拟刷新
  assert.equal(s2.prefs.getExpanded("work_001", "card_5"), "manual");
  assert.equal(s2.prefs.getExpanded("work_001", "card_7"), "system");
  assert.equal(s2.prefs.getProjectOpen("work_001"), true);
});

test("V-S2b 注入坏 JSON 返回默认值不抛错", () => {
  const storage = mockStorage();
  storage._m.set(NAV_PREFS_KEY, "{not valid json!!!");
  const { store } = makeStore({ storage });
  assert.equal(store.prefs.getExpanded("work_001", "card_1"), null);
  assert.equal(store.prefs.getProjectOpen("work_001"), false);
  assert.deepEqual(store.prefs.listExpanded("work_001"), []);
  store.prefs.setExpanded("work_001", "card_2", "manual");   // 容错后可正常写
  assert.equal(store.prefs.getExpanded("work_001", "card_2"), "manual");
});

test("V-S2c 折叠＝条目删除（source=null）", () => {
  const { store } = makeStore();
  store.prefs.setExpanded("work_001", "card_1", "manual");
  store.prefs.setExpanded("work_001", "card_1", null);
  assert.equal(store.prefs.getExpanded("work_001", "card_1"), null);
});

/* ---------- V-S3（R-09）：选中入 URL 复合键 ---------- */

test("V-S3a select.set→hash 复合形态；parse 读回 {wid,cid}", () => {
  const { store, history } = makeStore();
  store.select.set("work_004", "card_012");
  assert.equal(history.calls[0], "#work_004/card_012");
  const back = store.select.parse("#work_004/card_012");
  assert.deepEqual(back, { wid: "work_004", cid: "card_012" });
});

test("V-S3b hash 仅 cid→wid 回落当前页 WORK_ID（旧链接兼容）", () => {
  const { store } = makeStore({ workId: "work_009" });
  assert.deepEqual(store.select.parse("#card_045"), { wid: "work_009", cid: "card_045" });
  assert.deepEqual(store.select.parse(""), null);
});

test("V-S3c select.get 读当前 location.hash", () => {
  const { store, location } = makeStore({ workId: "work_001" });
  location.hash = "#work_001/card_077";
  assert.deepEqual(store.select.get(), { wid: "work_001", cid: "card_077" });
  location.hash = "#card_078";
  assert.deepEqual(store.select.get(), { wid: "work_001", cid: "card_078" });
});

/* ---------- V-S4（R-10）：行对象零状态投影 ---------- */

function makeCard(over = {}) {
  return {
    card_id: "card_010", title: "T", parent: "", child_count: 2,
    last_active_at: "2026-10-11T10:00:00+08:00", branch_active_at: null,
    work_id: "work_001", state: "", ...over,
  };
}

test("V-S4a props 六字段全来自三层（行对象自身零状态）", () => {
  const { store } = makeStore({ workId: "work_001" });
  const row = createRowModel(makeCard(), store, { depth: 1, wid: "work_001" });
  assert.deepEqual(row.props(store), {
    expanded: false, source: null, loading: false, error: null, loaded: false, selected: false,
  });
  store.prefs.setExpanded("work_001", "card_010", "manual");
  store.temp.loading("card_010", true);
  store.temp.loaded("card_010", true);
  store.temp.error("card_010", "boom");
  store.select.set("work_001", "card_010");
  const pr = row.props(store);
  assert.equal(pr.expanded, true);
  assert.equal(pr.source, "manual");
  assert.equal(pr.loading, true);
  assert.equal(pr.error, "boom");
  assert.equal(pr.loaded, true);
  assert.equal(pr.selected, true);
});

test("V-S4b Object.freeze 行对象后 props 仍正确（零状态铁证）", () => {
  const { store } = makeStore({ workId: "work_001" });
  const row = Object.freeze(createRowModel(makeCard(), store, { depth: 0, wid: "work_001" }));
  assert.equal(row.props(store).expanded, false);
  store.prefs.setExpanded("work_001", "card_010", "system");
  assert.equal(row.props(store).expanded, true);
  assert.equal(row.props(store).source, "system");
  store.prefs.setExpanded("work_001", "card_010", null);
  assert.equal(row.props(store).expanded, false);
  assert.throws(() => { row.depth = 9; }, TypeError);   // freeze 生效：不可变
});

test("V-S4c 项目行 props：expanded 走 getProjectOpen、selected 认 wid", () => {
  const { store } = makeStore({ workId: "work_001" });
  const proj = { work_id: "work_001", title: "P", card_count: 3, work_card_id: "card_001" };
  const row = createRowModel(proj, store, { kind: "project", depth: 0, wid: "work_001" });
  assert.equal(row.props(store).expanded, false);
  store.prefs.setProjectOpen("work_001", true);
  assert.equal(row.props(store).expanded, true);
  store.select.set("work_001", "card_001");
  assert.equal(row.props(store).selected, true);
});

/* ---------- 快照层 children 段（折叠即丢的级联语义） ---------- */

test("snapshot：setChildren/getChildren/dropChildren 级联丢弃", () => {
  const { store } = makeStore();
  store.snapshot.set({
    projects: [{ work_id: "work_001", title: "P", work_card_id: "card_001", cards: [] }],
  });
  store.snapshot.setChildren("work_001", "", [makeCard({ card_id: "a", child_count: 1 })]);
  store.snapshot.setChildren("work_001", "a", [makeCard({ card_id: "b", child_count: 1, parent: "a" })]);
  store.snapshot.setChildren("work_001", "b", [makeCard({ card_id: "c", parent: "b", child_count: 0 })]);
  assert.ok(store.snapshot.getChildren("work_001", "b"));
  store.snapshot.dropChildren("work_001", "a");   // 折叠 a：b、c 段级联丢
  assert.equal(store.snapshot.getChildren("work_001", "a"), null);
  assert.equal(store.snapshot.getChildren("work_001", "b"), null);
  assert.equal(store.snapshot.getChildren("work_001", "c"), null);
});

/* ---------- [修·运行中假阳性 2026-10-13] 运行中判据必须带 wid ----------
 * 事故：card_id 跨项目不唯一（实测 card_002 横跨 8 个项目；全仓 186 张卡只有 43 个
 * 编号）。旧实现只按 cid 存集合 → work_002 的 card_002 一跑，所有同名卡一起亮，
 * 症状＝「根本不在运行中的卡片一直显示运行中」。以下三条钉死这个口径。
 */

test("运行中：键含 wid，同 cid 跨项目互不串扰（假阳性回归）", () => {
  const { store } = makeStore();
  // 后端总览只报 work_002 的 card_002 在跑
  store.temp.setRunningSet([{ work_id: "work_002", card_id: "card_002" }]);
  assert.equal(store.temp.isRunning("work_002", "card_002"), true, "在跑的卡必须亮");
  // 同名卡在别的项目：绝不亮——这就是用户看到的那个假阳性
  assert.equal(store.temp.isRunning("work_001", "card_002"), false, "同名卡不得串扰");
  assert.equal(store.temp.isRunning("work_008", "card_002"), false, "同名卡不得串扰");
  // 同项目别的卡：不亮
  assert.equal(store.temp.isRunning("work_002", "card_003"), false, "异卡不得亮");
});

test("运行中：setRunningSet 短路判据（无变化 false／有变化 true）", () => {
  const { store } = makeStore();
  assert.equal(store.temp.setRunningSet([{ work_id: "w", card_id: "c1" }]), true, "首次＝有变化");
  assert.equal(store.temp.setRunningSet([{ work_id: "w", card_id: "c1" }]), false, "同集合＝短路");
  assert.equal(
    store.temp.setRunningSet([{ work_id: "w", card_id: "c1" }, { work_id: "w2", card_id: "c1" }]),
    true, "新增＝有变化",
  );
});

test("运行中：空列表清空（跑完即灭）＋缺字段条目丢弃（宁可漏标不可误标）", () => {
  const { store } = makeStore();
  store.temp.setRunningSet([{ work_id: "w", card_id: "c" }]);
  assert.equal(store.temp.isRunning("w", "c"), true);
  store.temp.setRunningSet([]);
  assert.equal(store.temp.isRunning("w", "c"), false, "跑完必须灭");
  // 缺 work_id / card_id 的一律不收（旧形态裸 cid 也无法归属到某项目，宁可漏标）
  assert.equal(store.temp.setRunningSet([{ card_id: "c9" }]), false, "缺 wid 不收（集合未变）");
  assert.equal(store.temp.setRunningSet(["c9"]), false, "裸字符串不收（集合未变）");
  assert.equal(store.temp.isRunning("", "c9"), false);
});

/* ---------- V-S5 配套：storage 包装行为等价 ---------- */

test("storageGet/storageSet 包装：行为与裸 localStorage 等价", () => {
  const s = mockStorage();
  assert.equal(storageGet(s, "k", "fb"), "fb");
  storageSet(s, "k", "v1");
  assert.equal(storageGet(s, "k", "fb"), "v1");
  assert.equal(storageGet(s, "missing", "fb"), "fb");
});
