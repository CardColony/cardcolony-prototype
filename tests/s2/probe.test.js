/* tests/s2/probe.test.js —— [S2] V-V6 机制层：60s 版本探测（未变零开销 / 变了才全量重拉
 * 且只重灌变化 work）＋ V-V4 机制层：children 去重与折叠即丢。
 * 跑法：node --test tests/s2/probe.test.js
 * DOM 用最小 fake；真机网络观察归 obs 脚本（需用户确认项）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createNavStateStore } from "../../static/esm/state_layers.js";
import { createNavAsync } from "../../static/esm/nav_async.js";
import { createNavRenderer } from "../../static/esm/nav_render.js";

function fakeEl(tag = "div") {
  const el = {
    tagName: tag, children: [], dataset: {}, style: {},
    title: "", textContent: "", innerHTML: "", __navRow: null, __parent: null,
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, f) { f ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(ch) { ch.__parent = el; el.children.push(ch); return ch; },
    remove() {
      if (el.__parent) { el.__parent.children = el.__parent.children.filter(x => x !== el); el.__parent = null; }
    },
    addEventListener() {},
    querySelector(sel) {
      const cls = String(sel).replace(/^\./, "").split(/\s+/)[0];
      const walk = (node) => {
        for (const ch of node.children) {
          if (ch.classList._s.has(cls)) return ch;
          const r = walk(ch);
          if (r) return r;
        }
        return null;
      };
      return walk(el);
    },
  };
  Object.defineProperty(el, "className", {
    get: () => [...el.classList._s].join(" "),
    set: (v) => { el.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}
function fakeContainer() {
  const el = fakeEl();
  Object.defineProperty(el, "clientHeight", { get: () => 760 });
  el.scrollTop = 0;
  return el;
}

function card(over = {}) {
  return {
    card_id: "card_x", title: "T", parent: "", child_count: 0,
    last_active_at: "2026-10-11T10:00:00+08:00", branch_active_at: null,
    work_id: "work_001", state: "", ...over,
  };
}
const V1 = "a".repeat(8), V2 = "b".repeat(8);
const NAV1 = {
  projects: [
    { work_id: "work_001", title: "A", work_card_id: "card_a", card_count: 2, last_active_at: "2026-10-11T10:00:00+08:00",
      cards: [card({ card_id: "card_a", parent: "" }), card({ card_id: "card_b", parent: "card_a", child_count: 1 })] },
    { work_id: "work_002", title: "B", work_card_id: "card_c", card_count: 1, last_active_at: "2026-10-11T10:00:00+08:00",
      cards: [card({ card_id: "card_c", parent: "" })] },
  ],
};
const NAV2 = {
  projects: [
    { work_id: "work_001", title: "A2（标题已变）", work_card_id: "card_a", card_count: 2, last_active_at: "2026-10-11T11:00:00+08:00",
      cards: [card({ card_id: "card_a", parent: "" }), card({ card_id: "card_b", parent: "card_a", child_count: 1 })] },
    { work_id: "work_002", title: "B", work_card_id: "card_c", card_count: 1, last_active_at: "2026-10-11T10:00:00+08:00",
      cards: [card({ card_id: "card_c", parent: "" })] },
  ],
};

function setup() {
  globalThis.document = { createElement: (t) => fakeEl(t) };
  const store = createNavStateStore({ workId: "work_001" });
  let navCount = 0, verCount = 0, childrenCount = 0;
  const api = {
    getNav: async () => { navCount++; return JSON.parse(JSON.stringify(currentNav)); },
    getNavVersion: async () => { verCount++; return { version: currentVer, by_work: currentByWork }; },
    getNavChildren: async (wid, parent) => {
      childrenCount++;
      return { work_id: wid, parent, cards: [card({ card_id: "card_kid", parent, work_id: wid })] };
    },
  };
  let currentNav = NAV1;
  let currentVer = V1;
  let currentByWork = { work_001: "f1", work_002: "f1" };
  const async = createNavAsync(store, api);
  const renderer = createNavRenderer({
    tree: fakeContainer(), store, api, workId: "work_001", async,
    getCardId: () => null,
  });
  return {
    store, api, renderer, async,
    counts: { get nav() { return navCount; }, get ver() { return verCount; }, get children() { return childrenCount; } },
    setNav(nav, ver, byWork) { currentNav = nav; currentVer = ver; currentByWork = byWork; },
    cleanup() { delete globalThis.document; },
  };
}

test("V-V6a 首次 refresh：getNav+getNavVersion 各 1 次", async () => {
  const t = setup();
  try {
    await t.renderer.refresh();
    assert.equal(t.counts.nav, 1);
    assert.equal(t.counts.ver, 1);
    assert.equal(t.store.snapshot.get().version, V1);
  } finally { t.cleanup(); }
});

test("V-V6b version 未变：probe 零开销（连 diff 都不跑，getNav 不再调）", async () => {
  const t = setup();
  try {
    await t.renderer.refresh();
    await t.renderer.probeVersion();
    await t.renderer.probeVersion();
    assert.equal(t.counts.ver, 3, "每次 probe 只拉 version");
    assert.equal(t.counts.nav, 1, "version 未变→零全量重拉");
  } finally { t.cleanup(); }
});

test("V-V6c version 变：全量重拉 1 次＋只重灌变化 work（by_work 比对）", async () => {
  const t = setup();
  try {
    await t.renderer.refresh();
    // 变化前：两个 work 的根层段都有数据
    t.store.snapshot.setChildren("work_002", "", [card({ card_id: "card_c", parent: "", work_id: "work_002" })]);
    assert.ok(t.store.snapshot.getChildren("work_002", ""));
    // work_001 指纹变，work_002 不变
    t.setNav(NAV2, V2, { work_001: "f2", work_002: "f1" });
    await t.renderer.probeVersion();
    assert.equal(t.counts.nav, 2, "变了才全量重拉一次");
    assert.equal(t.store.snapshot.get().version, V2);
    // 变化 work：项目行数据来自新快照（标题已变）
    assert.equal(t.store.snapshot.project("work_001").title, "A2（标题已变）");
    // 未变 work 的段数据零触碰（DOM 不动的前提）
    assert.ok(t.store.snapshot.getChildren("work_002", ""), "未变 work 段保留");
  } finally { t.cleanup(); }
});

test("V-V6d 探测失败静默跳过（不抛不重拉）", async () => {
  const t = setup();
  try {
    await t.renderer.refresh();
    t.api.getNavVersion = async () => { throw new Error("net down"); };
    await t.renderer.probeVersion();   // 不抛
    assert.equal(t.counts.nav, 1);
  } finally { t.cleanup(); }
});

test("V-V4 机制：children 去重（pending 共用 Promise）＋折叠即丢重拉", async () => {
  const t = setup();
  try {
    await t.renderer.refresh();
    const row = {
      wid: "work_001", kind: "card", depth: 0,
      card: card({ card_id: "card_a", parent: "", child_count: 1 }),
    };
    const p1 = t.async.loadChildren(row);
    const p2 = t.async.loadChildren(row);   // 同节点重复触发
    await Promise.all([p1, p2]);
    assert.equal(t.counts.children, 1, "pending 去重：只发一次");
    assert.ok(t.store.temp.isLoaded("card_a"));
    t.async.fold(row);                       // 折叠即丢
    assert.equal(t.store.snapshot.getChildren("work_001", "card_a"), null);
    assert.equal(t.store.temp.isLoaded("card_a"), false);
    await t.async.loadChildren(row);         // 重展再拉
    assert.equal(t.counts.children, 2, "折叠后重展再发一次");
  } finally { t.cleanup(); }
});

test("V-V4 叶节点（child_count=0）：零请求直接 loaded", async () => {
  const t = setup();
  try {
    const row = { wid: "work_001", kind: "card", depth: 0, card: card({ card_id: "card_leaf", child_count: 0 }) };
    await t.async.loadChildren(row);
    assert.equal(t.counts.children, 0, "零网络");
    assert.ok(t.store.temp.isLoaded("card_leaf"));
  } finally { t.cleanup(); }
});
