/* tests/s2/virtual.test.js —— [S2] V-V1：虚拟滚动前缀和切片（mock 2000 行序列）＋
 * R-19 交互集守卫（shouldSkipPatch）＋ R-15 投影 diff 输入。
 * 跑法：node --test tests/s2/virtual.test.js
 * DOM 层用最小 fake（无 jsdom 依赖）；真机行为归 DOM 观察脚本（需用户确认项）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { sliceRange, createVirtualList } from "../../static/esm/nav_virtual.js";
import { NAV_ROW_HEIGHT, createNavStateStore } from "../../static/esm/state_layers.js";
import { shouldSkipPatch } from "../../static/esm/nav_render.js";

/* ---------- fake DOM（最小面） ---------- */

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
      if (el.__parent) {
        el.__parent.children = el.__parent.children.filter(x => x !== el);
        el.__parent = null;
      }
    },
    addEventListener() {},
  };
  Object.defineProperty(el, "className", {
    get: () => [...el.classList._s].join(" "),
    set: (v) => { el.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}
function fakeContainer(clientHeight = 760) {
  const el = fakeEl();
  let st = 0;
  const scrollHandlers = [];
  Object.defineProperty(el, "clientHeight", { get: () => clientHeight });
  Object.defineProperty(el, "scrollTop", {
    get: () => st,
    set: (v) => { st = v; scrollHandlers.forEach(h => h()); },
  });
  el.addEventListener = (_t, h) => scrollHandlers.push(h);
  return el;
}

/* ---------- V-V1（R-16）：前缀和切片 ---------- */

test("V-V1a mock 2000 行：可视区切片行数恒定（与 2000 无关）", () => {
  const N = 2000, H = NAV_ROW_HEIGHT, VIEW = 760;   // 760/38＝20 行可视（＋首尾部分可见行）
  const heights = new Array(N).fill(H);
  const offsets = heights.map(((_acc) => (h) => { const o = _acc.v; _acc.v += h; return o; })({ v: 0 }));
  // 首尾有裁剪（数据用尽/视口顶部），中部三点切片数必须严格相等（恒定铁证）
  const mid = [1000, 30000, 70000].map(top => {
    const { start, end } = sliceRange(offsets, heights, top, VIEW, 1);
    return end - start + 1;
  });
  assert.ok(new Set(mid).size === 1, "中部切片恒定: " + mid.join(","));
  assert.ok(mid[0] >= 20 && mid[0] <= Math.ceil(VIEW / H) + 3, "切片=可视行+缓冲: " + mid[0]);
  // 首点：从 0 切，覆盖顶行
  const head = sliceRange(offsets, heights, 0, VIEW, 1);
  assert.equal(head.start, 0);
  assert.ok(head.end >= Math.ceil(VIEW / H) - 1, "首屏覆盖 20 行: end=" + head.end);
  // 末点：视口贴底，覆盖末行（切片可小于可视行数——数据用尽是物理正确）
  const tail = sliceRange(offsets, heights, N * H - VIEW, VIEW, 1);
  assert.equal(tail.end, N - 1, "末行必须在切片内");
  // 对照组：同样视口，40 行序列与 2000 行序列在中部切片数完全一致（与总行数无关）
  const small = new Array(40).fill(H);
  const smallOff = small.map(((_acc) => (h) => { const o = _acc.v; _acc.v += h; return o; })({ v: 0 }));
  const sMid = sliceRange(smallOff, small, 380, VIEW, 1);   // 两序列同偏移且都在数据内部
  const lMid = sliceRange(offsets, heights, 380, VIEW, 1);
  assert.equal(sMid.end - sMid.start, lMid.end - lMid.start, "切片行数与总行数无关");
});

test("V-V1b 滚动到中部：前缀和切片索引正确（含混合行高 30/38）", () => {
  // 20 项目行(30px) + 1980 卡行(38px) 交替段：前 100 行＝项目/卡交替
  const N = 2000;
  const heights = Array.from({ length: N }, (_, i) => (i % 2 === 0 ? 30 : 38));
  const offsets = [];
  let acc = 0;
  for (const h of heights) { offsets.push(acc); acc += h; }
  const total = acc;
  // 定位到第 1000 行（offsets[1000]）
  const { start, end } = sliceRange(offsets, heights, offsets[1000], 380, 1);
  assert.ok(start <= 1000 && end >= 1000, `切片覆盖第 1000 行 [${start},${end}]`);
  // 该行 top 加视口高落在切片内
  assert.ok(offsets[start] <= offsets[1000] && offsets[end] + heights[end] >= offsets[1000] + 380);
  assert.equal(sliceRange(offsets, heights, 0, 380, 1).start, 0);
  // 总高＝前缀和末值
  const r = sliceRange(offsets, heights, 0, total, 1);
  assert.equal(r.total, total);
  assert.equal(r.end, N - 1, "视口高＝总高时切到末行");
});

test("V-V1c createVirtualList：DOM 行数恒定（滚动只换内容不涨数量）", () => {
  globalThis.document = { createElement: (t) => fakeEl(t) };
  try {
    const N = 2000;
    const rows = Array.from({ length: N }, (_, i) => ({
      card: { card_id: "c" + i }, kind: "card", depth: 0, wid: "work_001",
      rowHeight() { return NAV_ROW_HEIGHT; },
      key() { return "c:" + i; },
    }));
    const container = fakeContainer(760);
    const built = new Map();   // key → build 次数
    const vl = createVirtualList(container, () => rows, NAV_ROW_HEIGHT, {
      renderer: {
        build: (row) => { const el = fakeEl(); built.set(row.key(), (built.get(row.key()) || 0) + 1); return el; },
        patch: () => {},
      },
    });
    const after1 = vl.reslice();
    const n1 = vl.mountedCount();
    assert.ok(n1 >= 20 && n1 <= 23, "首切可视+缓冲：实际 " + n1);
    assert.equal(after1.total, N * NAV_ROW_HEIGHT);
    // 滚动到中部：卸载旧行、挂载新行，DOM 总数恒定
    container.scrollTop = 40000;
    const n2 = vl.mountedCount();
    assert.ok(n2 >= 20 && n2 <= 23, "滚动后仍恒定：实际 " + n2);
    // build 去重：可视区重复 reslice 不重建已有行
    container.scrollTop = 40038;
    const buildCountBefore = [...built.values()].reduce((a, b) => a + b, 0);
    container.scrollTop = 40076;
    const buildCountAfter = [...built.values()].reduce((a, b) => a + b, 0);
    assert.ok(buildCountAfter - buildCountBefore <= 2, "滚动 2 行只新建 ≤2 行");
    assert.ok(vl.mountedCount() <= 23);
  } finally {
    delete globalThis.document;
  }
});

/* ---------- R-19（V-R4 机制）：交互集守卫 ---------- */

test("R-19a temp.hover 集内行跳过 patch；取消后恢复", () => {
  const store = createNavStateStore({ workId: "work_001" });
  const row = { kind: "card", card: { card_id: "card_1" }, props: () => ({}) };
  store.temp.hover("card_1");
  assert.equal(shouldSkipPatch(store, row), true, "悬停中跳过");
  store.temp.hover(null);
  assert.equal(shouldSkipPatch(store, row), false, "取消悬停恢复");
});

test("R-19b temp.loading 集内行跳过 patch", () => {
  const store = createNavStateStore({ workId: "work_001" });
  const row = { kind: "card", card: { card_id: "card_2" } };
  store.temp.loading("card_2", true);
  assert.equal(shouldSkipPatch(store, row), true, "加载中跳过");
  store.temp.loading("card_2", false);
  assert.equal(shouldSkipPatch(store, row), false);
});

test("R-19c 项目行不受交互集守卫影响（hover 是卡行概念）", () => {
  const store = createNavStateStore({ workId: "work_001" });
  const prow = { kind: "project", card: { work_id: "work_001" } };
  store.temp.hover("work_001");
  assert.equal(shouldSkipPatch(store, prow), false);
});
