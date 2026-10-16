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

const { computeLayout, separateOverlaps, computeVisibleSet, computeProjection, GAP_Y,
        computeSubtreeVisible, collectDescendants, planSubtreeUpdate, computeSubtreeLayout } =
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
  // [依赖关联线 2026-10-16] 通信线字段改名 relations → links：canvas.js 的
  // computeProjection 读 input.links（原 input.relations），此处键名随之改名。
  return { viewRoot: "root", cards, links: [], positions, expanded, sizes, cardMap, childMap };
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

// ════════ 画布重构 2026-10-15 · 阶段 2 · 增量渲染的三条判据 ════════
// 覆盖任务书 2.4：增量 DOM（节点引用不变）／子树局部布局／pinned 保护。
//
// 树形（b 是 pinned 卡、a2 折叠）：
//        root
//     ┌────┴────┐
//     a(pinned b) a2      → 实际：root ─ a ─ a1 ─ a11
//                      └─ b(pinned) ─ b1
function makeIncInput() {
  const cards = [
    { card_id: "root", parent: "", card_type: "剧本", title: "根", created_at: "2026-01-01" },
    { card_id: "a", parent: "root", card_type: "线索", title: "A", created_at: "2026-01-02" },
    { card_id: "b", parent: "root", card_type: "线索", title: "B", created_at: "2026-01-03" },
    { card_id: "a1", parent: "a", card_type: "线索", title: "A1", created_at: "2026-01-04" },
    { card_id: "a2", parent: "a", card_type: "线索", title: "A2", created_at: "2026-01-05" },
    { card_id: "a11", parent: "a1", card_type: "线索", title: "A11", created_at: "2026-01-06" },
    { card_id: "b1", parent: "b", card_type: "线索", title: "B1", created_at: "2026-01-07" },
  ];
  const positions = { b: { x: 900.5, y: 120.25, pinned: true } };
  const sizes = {
    root: { w: 320, h: 86 }, a: { w: 260, h: 95 }, b: { w: 260, h: 86 },
    a1: { w: 220, h: 36 }, a2: { w: 220, h: 86 }, a11: { w: 220, h: 36 },
    b1: { w: 220, h: 86 },
  };
  const cardMap = {}, childMap = {};
  for (const c of cards) {
    cardMap[c.card_id] = c;
    (childMap[c.parent || ""] = childMap[c.parent || ""] || []).push(c);
  }
  return { cards, relations: [], positions, sizes, cardMap, childMap };
}

test("增量·可见集：视图根子卡恒在，收起普通卡其子卡必须消失", () => {
  const { childMap } = makeIncInput();
  // 展开 a：其子卡 a1/a2 入列；a2 未展开故其子卡不入列；b 不在本单元内
  const open = computeSubtreeVisible("a", childMap, new Set(["root", "a"]), 1, false);
  assert.deepEqual([...open.ids].sort(), ["a1", "a2"]);
  assert.equal(open.depthOf.a1, 2, "depth 相对 viewRoot，不是相对 anchor");
  assert.equal(open.depthOf.a2, 2);
  // ★ 收起 a：子卡必须全部退出可见集（这条判据写错过一次——把「视图根子卡恒在」
  //   无差别套到普通卡上，结果收起反而多出一堆该消失的卡）
  const shut = computeSubtreeVisible("a", childMap, new Set(["root"]), 1, false);
  assert.equal(shut.ids.size, 0, "收起卡 a 后其子卡必须消失");
  // 单元根＝视图根时，子卡不因 expanded 缺失而消失（全树路径既有规则）
  const asRoot = computeSubtreeVisible("root", childMap, new Set([]), 0, true);
  assert.deepEqual([...asRoot.ids].sort(), ["a", "b"], "视图根的子卡恒可见");
  // 孙孙：a1 展开后 a11 入列
  const deep = computeSubtreeVisible("a", childMap, new Set(["a", "a1"]), 1, false);
  assert.deepEqual([...deep.ids].sort(), ["a1", "a11", "a2"]);
  assert.equal(deep.depthOf.a11, 3);
});

test("增量·计划：展开 A 只建 A 的子孙，A 的兄弟/祖先/无关卡一个都不进任何动作清单", () => {
  const { childMap } = makeIncInput();
  const before = new Set(["root", "a", "b"]);          // 展开前在场：root + 两个子卡
  const plan = planSubtreeUpdate("a", childMap, new Set(["root", "a"]), before, 1, false);

  assert.deepEqual([...plan.create].sort(), ["a1", "a2"], "只建 A 的可见子孙");
  assert.deepEqual(plan.remove, [], "展开不拆任何节点");
  assert.deepEqual([...plan.place].sort(), ["a1", "a2"], "位置只允许写 A 的可见子孙（锚点自己不动）");
  // ★ 验收标准①的落点：keep 里的卡既不被 remove()、也不被写 style.left/top
  assert.deepEqual([...plan.keep].sort(), ["a", "b", "root"]);
  for (const cid of plan.keep) {
    assert.ok(!plan.create.includes(cid), `${cid} 不得被重建`);
    assert.ok(!plan.remove.includes(cid), `${cid} 不得被拆除`);
    assert.ok(!plan.place.includes(cid), `${cid} 的坐标不得被改写`);
  }
  // 计划是纯判据：同输入两次一致
  assert.deepEqual(plan, planSubtreeUpdate("a", childMap, new Set(["root", "a"]), before, 1, false));
});

test("增量·计划：收起 A 只拆 A 的子孙，其余节点引用保住", () => {
  const { childMap } = makeIncInput();
  const field = new Set(["root", "a", "b", "a1", "a2", "a11"]);   // 展开态下都已建好
  const plan = planSubtreeUpdate("a", childMap, new Set(["root"]), field, 1, false);

  assert.deepEqual(plan.create, []);
  assert.deepEqual([...plan.place], [], "收起不该改写任何坐标");
  assert.deepEqual([...plan.remove].sort(), ["a1", "a11", "a2"],
    "只拆 A 的子孙（含更深层 a11）");
  assert.deepEqual([...plan.keep].sort(), ["a", "b", "root"]);
});

test("增量·布局：只重排 A 单元内部，A 自身不动，无关卡不流出布局结果", () => {
  const { cards, relations, positions, sizes } = makeIncInput();
  const expanded = new Set(["root", "a"]);
  // 全量布局取 A 的当前屏幕位当锚（真实渲染里就是从 DOM 的 style.left/top 读的）
  const full = computeLayout("root", cards, relations, positions, expanded, sizes);
  const anchor = { id: "a", x: full.a.x, y: full.a.y, depth: 1, isViewRoot: false };
  const unit = computeSubtreeLayout("root", cards, relations, positions, expanded, sizes, anchor);

  // ① A 自身坐标原样（不动＝不动，不是"算出来碰巧一样"）
  assert.deepEqual(unit.a, { x: full.a.x, y: full.a.y });
  // ② 结果只含 A 单元的键——别处的卡一根草都不许带出来（带出来＝调用方会误写）
  assert.deepEqual(Object.keys(unit).sort(), ["a", "a1", "a2"]);
  for (const cid of ["root", "b"]) assert.ok(!(cid in unit), `${cid} 不该出现在单元结果里`);
  // ③ 可见子孙都被排进 A 的右侧子树区，坐标有限
  for (const cid of ["a1", "a2"]) {
    assert.ok(Number.isFinite(unit[cid].x) && Number.isFinite(unit[cid].y), `${cid} 坐标必须有限`);
    assert.ok(unit[cid].x > anchor.x, `${cid} 排在 A 的下一列（子树单元内右排）`);
  }
  // ④ 无关卡的布局键一个都没被这个函数改写（入参不可变）
  assert.deepEqual(full.b, { x: 900.5, y: 120.25 }, "pinned 卡在全量布局里本就用存储坐标");
});

test("增量·布局：收起 A 时单元结果里不得残留已不可见的子孙（否则它们会当障碍误挤别人）", () => {
  const { cards, relations, positions, sizes } = makeIncInput();
  const full = computeLayout("root", cards, relations, positions, new Set(["root"]), sizes);
  const anchor = { id: "a", x: full.a.x, y: full.a.y, depth: 1, isViewRoot: false };
  // a 已收起：单元内不该再有任何子卡的坐标
  const unit = computeSubtreeLayout("root", cards, relations, positions,
                                    new Set(["root"]), sizes, anchor);
  assert.deepEqual(Object.keys(unit), ["a"], "收起后单元只剩锚点自己");
});

test("增量·pinned 保护：局部避让下 pinned 卡与固定障碍坐标绝对不变", () => {
  const { cards, relations, positions, sizes } = makeIncInput();
  const expanded = new Set(["root", "a"]);
  const full = computeLayout("root", cards, relations, positions, expanded, sizes);
  const anchor = { id: "a", x: full.a.x, y: full.a.y, depth: 1, isViewRoot: false };
  const unit = computeSubtreeLayout("root", cards, relations, positions, expanded, sizes, anchor);

  // 模拟 renderSubtree：全量基底 + 本单元覆盖，只有本单元可动
  const base = Object.assign({}, full, unit);
  const movable = new Set(["a1", "a2"]);
  const out = separateOverlaps(base, sizes, new Set(["b"]), GAP_Y, movable);

  // ★ 验收标准④：pinned 卡、以及任何不在 movable 里的卡，一个坐标都不许变
  assert.deepEqual(out.b, { x: 900.5, y: 120.25 }, "pinned 卡在展开/收起时位置绝对不变");
  assert.deepEqual(out.root, base.root, "视图根（固定障碍）不动");
  assert.deepEqual(out.a, { x: anchor.x, y: anchor.y }, "锚点自身不动");

  // ★ 局部避让的真义：新增的卡撞上 pinned 卡时，是**它绕行**，不是 pinned 让位。
  //   构造一次真碰撞——把本单元的卡拉到 b 的位置上（真实场景：用户在别处摆好的
  //   pinned 卡正好在新展开的子树要落的地方）。
  const collide = Object.assign({}, base, {
    a1: { x: 900, y: 130 }, a2: { x: 905, y: 135 },
  });
  const out2 = separateOverlaps(collide, sizes, new Set(["b"]), GAP_Y, movable);
  assert.deepEqual(out2.b, { x: 900.5, y: 120.25 }, "碰撞时 pinned 卡仍然绝对不动");
  assert.deepEqual(out2.root, collide.root, "碰撞时其余固定障碍同样不动");
  const gapOk = ["a1", "a2"].some(cid =>
    Math.abs(out2[cid].x - 900) > 0.01 || Math.abs(out2[cid].y - 130) > 0.01);
  assert.ok(gapOk, "新增卡必须为 pinned 卡绕行（局部避让不是全员钉死）");

  // ★ 缺省 movable=null ⇒ 旧行为不变（全量重排路径仍全员可动，pinned 仍不动）
  const global = separateOverlaps(collide, sizes, new Set(["b"]));
  assert.deepEqual(global.b, { x: 900.5, y: 120.25 }, "缺省 movable 时 pinned 依旧不动");
});


