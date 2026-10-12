/* tests/s3/nav_breadcrumb.test.js —— [S3] V3-L1（R-27 面包屑祖先链）＋ ancestorCards。
 * 跑法：node --test tests/s3/nav_breadcrumb.test.js
 * snapshot 以鸭子类型 mock（只要求 project(wid)），模块本体不摸全局。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildBreadcrumb, ancestorCards, NAV_CRUMB_MIN_DEPTH,
} from "../../static/esm/nav_breadcrumb.js";

/* 深树：root → L1(depth0) → L2(depth1) → L3(depth2) → L4(depth3) → L5(depth4) */
function mkSnapshot(withProject = true) {
  const cards = [
    { card_id: "card_l1", title: "假卡丙-L1", parent: "card_l_root", child_count: 1 },
    { card_id: "card_l2", title: "假卡丙-L2", parent: "card_l1", child_count: 1 },
    { card_id: "card_l3", title: "假卡丙-L3", parent: "card_l2", child_count: 1 },
    { card_id: "card_l4", title: "假卡丙-L4", parent: "card_l3", child_count: 1 },
    { card_id: "card_l5", title: "假卡丙-L5", parent: "card_l4", child_count: 0 },
  ];
  const proj = withProject
    ? { work_id: "work_m3", title: "假项目丙", work_card_id: "card_l_root", card_count: 5, cards }
    : null;
  return { project: (wid) => (wid === "work_m3" ? proj : null) };
}

/* ---------- V3-L1（R-27）：buildBreadcrumb ---------- */

test("V3-L1a 深树（depth≥3）→ 链数组：项目→根卡→…→当前卡", () => {
  const snap = mkSnapshot();
  const segs = buildBreadcrumb(snap, "work_m3", "card_l4");
  assert.equal(segs.length, 5);   // 项目 + L1..L4
  assert.deepEqual(
    segs.map(s => s.title),
    ["假项目丙", "假卡丙-L1", "假卡丙-L2", "假卡丙-L3", "假卡丙-L4"],
  );
  assert.equal(segs[0].kind, "project");
  assert.equal(segs[0].cid, "card_l_root", "项目段指向 work_card_id");
  for (let i = 1; i < segs.length; i++) assert.equal(segs[i].kind, "card");
  assert.equal(segs[4].cid, "card_l4", "末段＝当前卡");
});

test("V3-L1b depth<3 返回空（浅树不渲染）", () => {
  const snap = mkSnapshot();
  assert.deepEqual(buildBreadcrumb(snap, "work_m3", "card_l1"), [], "depth 0");
  assert.deepEqual(buildBreadcrumb(snap, "work_m3", "card_l2"), [], "depth 1");
  assert.deepEqual(buildBreadcrumb(snap, "work_m3", "card_l3"), [], "depth 2");
  assert.equal(buildBreadcrumb(snap, "work_m3", "card_l4").length, 5, "depth 3 恰好渲染");
  assert.equal(NAV_CRUMB_MIN_DEPTH, 3, "门限常量＝3");
});

test("V3-L1c 容错：cid 不在快照/项目缺失/wid 不对 → 空数组", () => {
  const snap = mkSnapshot();
  assert.deepEqual(buildBreadcrumb(snap, "work_m3", "card_ghost"), []);
  assert.deepEqual(buildBreadcrumb(snap, "work_m3", ""), []);
  assert.deepEqual(buildBreadcrumb(snap, "work_none", "card_l4"), []);
  assert.deepEqual(buildBreadcrumb(mkSnapshot(false), "work_m3", "card_l4"), []);
});

/* ---------- ancestorCards（R-26/R-28 共用数据源） ---------- */

test("ancestorCards：含当前卡的完整祖先链（无深度门）", () => {
  const snap = mkSnapshot();
  const chain = ancestorCards(snap, "work_m3", "card_l3");
  assert.deepEqual(chain.map(c => c.card_id), ["card_l1", "card_l2", "card_l3"]);
  // 浅卡也返回链（供一键收起/定位用，与 buildBreadcrumb 的深度门无关）
  assert.deepEqual(ancestorCards(snap, "work_m3", "card_l1").map(c => c.card_id), ["card_l1"]);
  assert.deepEqual(ancestorCards(snap, "work_m3", "card_ghost"), []);
  assert.deepEqual(ancestorCards(snap, "work_m3", ""), []);
});

test("ancestorCards：中间祖先缺失时止于最高可见祖先（脏数据不死循环）", () => {
  const snap = {
    project: () => ({
      work_id: "work_x", title: "断链项目", work_card_id: "r",
      cards: [
        { card_id: "a", title: "A", parent: "r" },
        { card_id: "c", title: "C", parent: "b" },   // b 不在快照（断链）
      ],
    }),
  };
  assert.deepEqual(ancestorCards(snap, "work_x", "c").map(c => c.card_id), ["c"]);
});
