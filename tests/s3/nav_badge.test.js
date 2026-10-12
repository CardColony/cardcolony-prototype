/* tests/s3/nav_badge.test.js —— [S3] V3-P2（R-33 徽章聚合纯函数）＋ V3-P4（R-35 空项目）
 * ＋ V3-P6（R-34 唯一高亮 selected 投影不回归）＋ R-31 引导线常量。
 * 跑法：node --test tests/s3/nav_badge.test.js
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  projectAggregate, nodeActive, isProjectEmpty, cardInSnapshot,
  NAV_GUIDE_DEPTH, NAV_GUIDE_SPACING, NAV_GUIDE_STEP,
} from "../../static/esm/nav_badge.js";
import { createNavStateStore } from "../../static/esm/state_layers.js";
import { createRowModel } from "../../static/esm/row_model.js";

/* ---------- V3-P2（R-33）：projectAggregate / nodeActive ---------- */

test("V3-P2a projectAggregate＝最新活跃机械 max＋总卡数", () => {
  const proj = {
    work_id: "work_p1", title: "假项目甲", last_active_at: "2026-10-01T08:00:00Z",
    cards: [
      { card_id: "c1", last_active_at: "2026-10-09T08:00:00Z", branch_active_at: null },
      { card_id: "c2", last_active_at: "2026-10-11T09:30:00Z", branch_active_at: "2026-10-10T10:00:00Z" },
      { card_id: "c3", last_active_at: null, branch_active_at: "2026-10-11T12:00:00Z" },
    ],
  };
  const agg = projectAggregate(proj);
  assert.equal(agg.latest, "2026-10-11T12:00:00Z", "机械取 max（含 branch_active_at）");
  assert.equal(agg.count, 3, "总卡数＝cards.length");
});

test("V3-P2b projectAggregate 全空 → latest 空串；无 cards → 0 卡", () => {
  assert.deepEqual(projectAggregate({ work_id: "w", cards: [] }), { latest: "", count: 0 });
  assert.deepEqual(projectAggregate(null), { latest: "", count: 0 });
  assert.deepEqual(projectAggregate({ work_id: "w" }), { latest: "", count: 0 });
});

test("V3-P2c nodeActive＝branch_active_at 距今<48h（机械阈值，无语义判断）", () => {
  const now = Date.parse("2026-10-11T12:00:00Z");
  assert.equal(nodeActive({ branch_active_at: "2026-10-11T11:00:00Z" }, now), true, "1h 前＝活跃");
  assert.equal(nodeActive({ branch_active_at: "2026-10-09T13:00:01Z" }, now), true, "46h59m59s 前＝活跃");
  assert.equal(nodeActive({ branch_active_at: "2026-10-09T12:00:00Z" }, now), false, "恰 48h＝不活跃");
  assert.equal(nodeActive({ branch_active_at: null }, now), false, "无分支活跃＝不活跃");
  assert.equal(nodeActive({ branch_active_at: "not-a-date" }, now), false, "脏值＝不活跃");
  assert.equal(nodeActive(null, now), false);
});

/* ---------- V3-P4（R-35）：isProjectEmpty ---------- */

test("V3-P4 isProjectEmpty→布尔（project 在场且 cards 空）", () => {
  const empty = { project: (w) => (w === "w1" ? { work_id: "w1", cards: [] } : null) };
  const full = { project: (w) => (w === "w1" ? { work_id: "w1", cards: [{ card_id: "c" }] } : null) };
  const none = { project: () => null };
  assert.equal(isProjectEmpty(empty, "w1"), true);
  assert.equal(isProjectEmpty(full, "w1"), false);
  assert.equal(isProjectEmpty(none, "w1"), false, "项目不存在≠空项目（是不存在）");
  assert.equal(isProjectEmpty(null, "w1"), false);
});

/* ---------- R-21：cardInSnapshot（点击先查后动的数据源） ---------- */

test("cardInSnapshot：快照含卡→true；缺失/项目缺失→false", () => {
  const snap = { project: (w) => (w === "w1" ? { work_id: "w1", cards: [{ card_id: "c1" }] } : null) };
  assert.equal(cardInSnapshot(snap, "w1", "c1"), true);
  assert.equal(cardInSnapshot(snap, "w1", "ghost"), false);
  assert.equal(cardInSnapshot(snap, "w2", "c1"), false);
});

/* ---------- V3-P6（R-34）：唯一高亮 selected 投影（S2 已测，S3 验收确认不回归） ---------- */

function makeStore(workId) {
  const storage = (() => { const m = new Map(); return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; })();
  const location = { hash: "" };
  const history = { replaceState(_s, _t, url) { location.hash = url || ""; } };
  return createNavStateStore({ storage, location, history, workId });
}

test("V3-P6a selected 来自 select 层：唯一卡命中，其余与项目外皆否", () => {
  const store = makeStore("work_p1");
  const cardA = { card_id: "ca", title: "A", parent: "", child_count: 0, work_id: "work_p1" };
  const cardB = { card_id: "cb", title: "B", parent: "", child_count: 0, work_id: "work_p1" };
  const rowA = createRowModel(cardA, store, { wid: "work_p1" });
  const rowB = createRowModel(cardB, store, { wid: "work_p1" });
  assert.equal(rowA.props(store).selected, false, "未选中");
  store.select.set("work_p1", "ca");
  assert.equal(rowA.props(store).selected, true, "选中层命中");
  assert.equal(rowB.props(store).selected, false, "同树他卡不亮（唯一高亮）");
  const rowOther = createRowModel(
    { card_id: "cx", title: "X", parent: "", child_count: 0, work_id: "work_p2" },
    store, { wid: "work_p2" },
  );
  assert.equal(rowOther.props(store).selected, false, "跨项目不亮");
});

test("V3-P6b 项目行 selected＝当前项目次强（wid 命中即亮）", () => {
  const store = makeStore("work_p1");
  store.select.set("work_p1", "ca");
  const proj = { work_id: "work_p1", title: "P1", work_card_id: "r", card_count: 1 };
  const projRow = createRowModel(proj, store, { kind: "project", depth: 0, wid: "work_p1" });
  assert.equal(projRow.props(store).selected, true, "当前项目次强");
});

/* ---------- R-31：引导线常量在场（V3-P1 静态闸的常量面） ---------- */

test("R-31 常量：缩进 4 档封顶、引导线档距、普通档距收编", () => {
  assert.equal(NAV_GUIDE_DEPTH, 4, "缩进只到第 4 档");
  assert.equal(NAV_GUIDE_SPACING, 4, "引导线档距");
  assert.equal(NAV_GUIDE_STEP, 14, "前四档普通缩进档距（与 S2 +14px 同源）");
});
