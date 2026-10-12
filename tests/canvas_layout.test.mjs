// card_045 E2 第一批单元测试（node 24 自带 node --test，零安装）。
// 运行注意（任务书 E2 实测两条）：测试文件用 .mjs 扩展名；node --test 用绝对路径调用。
// 覆盖（任务书 5.1）：#1 布局与避让是纯函数（同输入两次一致、入参未改动）；
//   避让行为断言（重叠推开、pinned 不动、带 GAP 分离）。
//   #2（深合并，E6）属后端 Python——由 tests/test_card045_canvas_store.py 覆盖（pytest 档）。
//   #3（非法坐标兜底，E7）待 E7 兜底实现后随 E7 追加。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const { loadLayoutFunctions, SRC_PATH } = require(
  join(dirname(fileURLToPath(import.meta.url)), "..", "static", "canvas_layout.js"));
const { readFileSync } = await import("node:fs");

const { computeLayout, separateOverlaps, computeVisibleSet, computeProjection, GAP_Y } =
  loadLayoutFunctions(readFileSync(SRC_PATH, "utf-8"));

// ── 合成但非平凡的固定输入：三层树 + 钉住卡 + 相异尺寸 + 展开态 ──
function makeInput() {
  const cards = [
    { card_id: "root", parent: "", card_type: "剧本", title: "根", created_at: "2026-01-01" },
    { card_id: "a", parent: "root", card_type: "线索", title: "A", created_at: "2026-01-02" },
    { card_id: "b", parent: "root", card_type: "线索", title: "B", created_at: "2026-01-03" },
    { card_id: "a1", parent: "a", card_type: "线索", title: "A1", created_at: "2026-01-04" },
    { card_id: "a2", parent: "a", card_type: "线索", title: "A2", created_at: "2026-01-05" },
  ];
  const relations = [];
  const positions = { b: { x: 900.5, y: 120.25, pinned: true } };
  const expanded = new Set(["root", "a"]);
  const sizes = {
    root: { w: 320, h: 86 }, a: { w: 260, h: 95 }, b: { w: 260, h: 86 },
    a1: { w: 220, h: 36 }, a2: { w: 220, h: 86 },
  };
  return { cards, relations, positions, expanded, sizes };
}

test("布局纯函数：同输入两次结果逐键一致（含浮点）", () => {
  const { cards, relations, positions, expanded, sizes } = makeInput();
  const r1 = computeLayout("root", cards, relations, positions, expanded, sizes);
  const r2 = computeLayout("root", cards, relations, positions, expanded, sizes);
  assert.deepEqual(r1, r2);
  assert.equal(Object.keys(r1).length, 5); // 可见集 = root + a + b + a1 + a2
  // 钉住卡用存储坐标（浮点原样）
  assert.equal(r1.b.x, 900.5);
  assert.equal(r1.b.y, 120.25);
});

test("布局纯函数：入参未被改动", () => {
  const { cards, relations, positions, expanded, sizes } = makeInput();
  const snapCards = JSON.stringify(cards);
  const snapPos = JSON.stringify(positions);
  const snapSizes = JSON.stringify(sizes);
  const sizeBefore = expanded.size;
  computeLayout("root", cards, relations, positions, expanded, sizes);
  assert.equal(JSON.stringify(cards), snapCards);
  assert.equal(JSON.stringify(positions), snapPos);
  assert.equal(JSON.stringify(sizes), snapSizes);
  assert.equal(expanded.size, sizeBefore);
});

test("避让纯函数：相交卡被推开且满足 GAP，pinned 卡不动", () => {
  const layout = {
    p: { x: 0, y: 0 },      // pinned
    q: { x: 10, y: 10 },    // 与 p 重叠
    r: { x: 500, y: 0 },    // 独立
  };
  const sizes = { p: { w: 220, h: 86 }, q: { w: 220, h: 86 }, r: { w: 220, h: 86 } };
  const before = JSON.stringify(layout);
  const out = separateOverlaps(layout, sizes, new Set(["p"]));
  assert.equal(JSON.stringify(layout), before, "入参 layout 不得被改动");
  assert.equal(out.p.x, 0); assert.equal(out.p.y, 0), "pinned 卡永不被移动";
  const gapOk = out.q.y >= out.p.y + 86 + GAP_Y || out.q.x >= out.p.x + 220 + GAP_Y;
  assert.ok(gapOk, `q 应被推离 p 至少一个 GAP（实 q=${out.q.x},${out.q.y}）`);
  assert.deepEqual(out.r, { x: 500, y: 0 }, "不相交卡零改动");
});

test("避让纯函数：同输入两次一致", () => {
  const layout = { a: { x: 0, y: 0 }, b: { x: 5, y: 5 }, c: { x: 30, y: 30 } };
  const sizes = { a: { w: 220, h: 86 }, b: { w: 220, h: 86 }, c: { w: 220, h: 86 } };
  const o1 = separateOverlaps(layout, sizes, new Set());
  const o2 = separateOverlaps(layout, sizes, new Set());
  assert.deepEqual(o1, o2);
});

test("加载器：实现来自 canvas.js 唯一源（段锚存在、函数为真函数）", () => {
  assert.equal(typeof computeLayout, "function");
  assert.equal(typeof separateOverlaps, "function");
  assert.equal(typeof GAP_Y, "number");
});

// ════════ card_045 E7 · L5 兜底（5.1 #3）：非法坐标落到自动布局位置、不消失 ════════
test("L5 兜底：非法坐标的钉住记录落自动布局位置，卡片不消失", () => {
  const cards = [
    { card_id: "root", parent: "", card_type: "剧本", title: "根", created_at: "2026-01-01" },
    { card_id: "bad", parent: "root", card_type: "线索", title: "坏坐标", created_at: "2026-01-02" },
    { card_id: "ok", parent: "root", card_type: "线索", title: "好坐标", created_at: "2026-01-03" },
  ];
  const positions = {
    bad: { x: NaN, y: Infinity, pinned: true },   // 非法
    ok: { x: 500.5, y: 60.25, pinned: true },     // 合法
  };
  const expanded = new Set(["root"]);
  const sizes = { root: { w: 320, h: 86 }, bad: { w: 220, h: 86 }, ok: { w: 220, h: 86 } };
  const r = computeLayout("root", cards, relations_of(cards), positions, expanded, sizes);
  // 三张卡全部在结果里（不消失）
  for (const id of ["root", "bad", "ok"]) {
    assert.ok(r[id], `${id} 必须在布局结果中`);
    assert.ok(Number.isFinite(r[id].x) && Number.isFinite(r[id].y), `${id} 坐标必须有限`);
  }
  // 合法钉住用存储坐标；非法钉住落自动布局（非存储 NaN 值）
  assert.equal(r.ok.x, 500.5);
  assert.notEqual(r.bad.x, NaN);
});

function relations_of(cards) { return []; }

// ════════ 画布第三步 T1 · 投影纯函数（computeVisibleSet / computeProjection）════════
// 输入域最小集（任务书 T1 验收③）：≥3 层深度、≥2 张折叠中间卡、
// 1 张 parent 断裂（孤儿）卡、childMap 成环卡各一例。
function makeProjInput() {
  const cards = [
    { card_id: "root", parent: "", card_type: "剧本", title: "根", created_at: "2026-01-01" },
    { card_id: "a", parent: "root", card_type: "线索", title: "A", created_at: "2026-01-02" },
    { card_id: "b", parent: "root", card_type: "线索", title: "B", created_at: "2026-01-03" },
    { card_id: "a1", parent: "a", card_type: "线索", title: "A1", created_at: "2026-01-04" },
    { card_id: "a2", parent: "a", card_type: "线索", title: "A2折叠", created_at: "2026-01-05" },
    { card_id: "a11", parent: "a1", card_type: "线索", title: "A11三级", created_at: "2026-01-06" },
    { card_id: "a2x", parent: "a2", card_type: "线索", title: "A2X不可见", created_at: "2026-01-07" },
    { card_id: "b1", parent: "b", card_type: "线索", title: "B1不可见", created_at: "2026-01-08" },
    { card_id: "orphan", parent: "ghost", card_type: "线索", title: "孤儿", created_at: "2026-01-09" },
    { card_id: "x", parent: "y", card_type: "线索", title: "环X", created_at: "2026-01-10" },
    { card_id: "y", parent: "x", card_type: "线索", title: "环Y", created_at: "2026-01-11" },
  ];
  const positions = { b: { x: 900.5, y: 120.25, pinned: true } };
  const expanded = new Set(["root", "a", "a1"]);   // a2／b 为折叠中间卡
  const sizes = {
    root: { w: 320, h: 86 }, a: { w: 260, h: 95 }, b: { w: 260, h: 86 },
    a1: { w: 220, h: 36 }, a2: { w: 220, h: 86 }, a11: { w: 220, h: 86 },
  };
  const cardMap = {}, childMap = {};
  for (const c of cards) {
    cardMap[c.card_id] = c;
    const p = c.parent || "";
    (childMap[p] = childMap[p] || []).push(c);
  }
  return { viewRoot: "root", cards, relations: [], positions, expanded, sizes, cardMap, childMap };
}

test("投影·可见集：不动点规则（三层深度/折叠中间卡/孤儿/成环）", () => {
  const input = makeProjInput();
  const { visibleIds, depthOf } = computeVisibleSet("root", input.childMap, input.expanded);
  assert.deepEqual([...visibleIds].sort(), ["a", "a1", "a11", "a2", "b", "root"]);
  assert.equal(depthOf.root, 0);
  assert.equal(depthOf.a, 1);
  assert.equal(depthOf.a1, 2);
  assert.equal(depthOf.a11, 3);   // ≥3 层深度
  // 折叠中间卡的子卡不可见（a2x / b1）
  assert.ok(!visibleIds.has("a2x") && !visibleIds.has("b1"), "折叠中间卡的子卡不入可见集");
  // 孤儿（parent 断裂）与成环卡不可达即不可见；不动点收敛不悬挂（跑到这里即证）
  assert.ok(!visibleIds.has("orphan") && !visibleIds.has("x") && !visibleIds.has("y"),
    "孤儿/成环卡不入可见集");
});

test("投影·纯函数：computeProjection 同输入两次输出逐键相同（V2）", () => {
  const input = makeProjInput();
  const snap = JSON.stringify([input.cards, input.positions, input.sizes]);
  const sizeBefore = input.expanded.size;
  const p1 = computeProjection(input);
  const p2 = computeProjection(input);
  assert.deepEqual(p1, p2);
  assert.equal(JSON.stringify([input.cards, input.positions, input.sizes]), snap, "入参不得被改动");
  assert.equal(input.expanded.size, sizeBefore);
});

test("投影·链路：布局覆盖可见集、pinned 存储坐标、面包屑与连线几何", () => {
  const input = makeProjInput();
  const p = computeProjection(input);
  for (const cid of p.visibleIds) {
    assert.ok(p.layout[cid], `${cid} 必须有布局位`);
    assert.ok(Number.isFinite(p.layout[cid].x) && Number.isFinite(p.layout[cid].y));
  }
  assert.equal(p.layout.b.x, 900.5);       // pinned 卡用存储坐标
  assert.equal(p.sizes, input.sizes);      // 尺寸表透传（测量纪律在渲染入口）
  assert.deepEqual(p.breadcrumb.map(c => c.card_id), ["root"]);
  const p2 = computeProjection({ ...input, viewRoot: "a1" });
  assert.deepEqual(p2.breadcrumb.map(c => c.card_id), ["root", "a", "a1"]);
  // 父子折线段＝可见父子对（root-a, root-b, a-a1, a-a2, a1-a11＝5；折叠对不画）
  assert.equal(p.edges.elbowSegs.length, 5);
  assert.equal(p.edges.rootParentId, "");
  assert.ok(Number.isFinite(p.edges.bbox.w) && p.edges.bbox.w > 0);
});

test("加载器：投影纯函数经同一剪段通道可得（返回列表 5→7 符号）", () => {
  assert.equal(typeof computeVisibleSet, "function");
  assert.equal(typeof computeProjection, "function");
});
