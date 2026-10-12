/* tests/s3/nav_find.test.js —— [S3] V3-F1（R-24 搜索内存过滤）＋ V3-F4（R-25 最近访问）。
 * 跑法：node --test tests/s3/nav_find.test.js
 * 全部纯函数＋mock storage，零网络、零 DOM 依赖（createRecent 注入 storage）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  filterCards, createRecent, NAV_RECENT_KEY, NAV_RECENT_MAX,
} from "../../static/esm/nav_find.js";

const projects = [
  {
    work_id: "work_f1", title: "假项目甲",
    cards: [
      { card_id: "card_f_root", title: "根卡甲", parent: "work_f1", child_count: 2 },
      { card_id: "card_f_a", title: "分支Alpha", parent: "card_f_root", child_count: 1 },
      { card_id: "card_f_a1", title: "叶子alpha一", parent: "card_f_a", child_count: 0 },
      { card_id: "card_f_b", title: "中文标题贝塔", parent: "card_f_root", child_count: 0 },
    ],
  },
  {
    work_id: "work_f2", title: "假项目乙",
    cards: [
      { card_id: "card_f2_x", title: "别项目Alpha卡", parent: "card_f2_root", child_count: 0 },
    ],
  },
];

/* ---------- V3-F1（R-24）：filterCards ---------- */

test("V3-F1a title 包含命中（大小写不敏感）", () => {
  const hits = filterCards("alpha", projects);
  const cids = hits.map(h => h.cid).sort();
  // 分支Alpha / 叶子alpha一 / 别项目Alpha卡 三张全中（大小写不敏感）
  assert.deepEqual(cids, ["card_f2_x", "card_f_a", "card_f_a1"].sort());
  const upper = filterCards("ALPHA", projects);
  assert.equal(upper.length, 3, "大写 query 同样命中");
});

test("V3-F1b 结果含项目名＋父路径", () => {
  const hits = filterCards("叶子alpha", projects);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].project, "假项目甲");
  // 父路径＝祖先标题链（card_f_a1 → card_f_a「分支Alpha」→ card_f_root「根卡甲」）
  assert.ok(hits[0].path.includes("根卡甲"), "父路径含根层卡标题");
  assert.ok(hits[0].path.includes("分支Alpha"), "父路径含直接父卡标题");
});

test("V3-F1c 无匹配返回空数组；空 query 返回空数组", () => {
  assert.deepEqual(filterCards("不存在的标题xyz", projects), []);
  assert.deepEqual(filterCards("", projects), []);
  assert.deepEqual(filterCards("   ", projects), []);
  assert.deepEqual(filterCards(null, projects), []);
});

test("V3-F1d 深祖先路径简述：超过两级以 … 起头取最近两级", () => {
  const deep = [{
    work_id: "work_f3", title: "假项目丙",
    cards: [
      { card_id: "d1", title: "甲层", parent: "root3" },
      { card_id: "d2", title: "乙层", parent: "d1" },
      { card_id: "d3", title: "丙层", parent: "d2" },
      { card_id: "d4", title: "丁层目标", parent: "d3" },
    ],
  }];
  const hits = filterCards("丁层目标", deep);
  assert.equal(hits.length, 1);
  assert.ok(hits[0].path.startsWith("…/"), "超两级以省略号起头");
  assert.ok(hits[0].path.includes("乙层/丙层"), "取最近两级");
});

/* ---------- V3-F4（R-25）：createRecent ---------- */

function mockStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    _m: m,
  };
}

test("V3-F4a record 上限 10 条、倒序（最新在前）", () => {
  const storage = mockStorage();
  const recent = createRecent(storage);
  for (let i = 1; i <= 12; i++) {
    recent.record("work_f1", "card_r" + i, "卡" + i);
  }
  const list = recent.list();
  assert.equal(list.length, NAV_RECENT_MAX, "上限 10 条");
  assert.equal(list[0].cid, "card_r12", "最新在前");
  assert.equal(list[9].cid, "card_r3", "最旧被挤出（r1/r2 淘汰）");
  // 倒序：ts 单调不增
  for (let i = 1; i < list.length; i++) {
    assert.ok(list[i - 1].ts >= list[i].ts, "倒序");
  }
});

test("V3-F4b 同卡去重更新 ts（移到最前，不新增条目）", () => {
  const storage = mockStorage();
  const recent = createRecent(storage);
  recent.record("work_f1", "card_x", "卡X");
  const firstTs = recent.list()[0].ts;
  recent.record("work_f1", "card_y", "卡Y");
  assert.equal(recent.list().length, 2);
  recent.record("work_f1", "card_x", "卡X");   // 再访问旧卡
  const list = recent.list();
  assert.equal(list.length, 2, "去重不新增");
  assert.equal(list[0].cid, "card_x", "旧卡移到最前");
  assert.ok(list[0].ts >= firstTs, "ts 更新");
  // 同卡不同 wid 视为不同条目
  recent.record("work_f2", "card_x", "卡X乙");
  assert.equal(recent.list().length, 3);
});

test("V3-F4c storage 键＝nav.recent.v1（storageGet/Set 规范入口）", () => {
  const storage = mockStorage();
  const recent = createRecent(storage);
  recent.record("work_f1", "card_z", "卡Z");
  assert.ok(storage._m.has(NAV_RECENT_KEY), "键 nav.recent.v1 在场");
  const raw = JSON.parse(storage._m.get(NAV_RECENT_KEY));
  assert.equal(raw.length, 1);
  assert.deepEqual(Object.keys(raw[0]).sort(), ["cid", "title", "ts", "wid"], "条目四字段");
});

test("V3-F4d 坏 JSON 容错：损坏存储返回空表不抛错", () => {
  const storage = mockStorage();
  storage._m.set(NAV_RECENT_KEY, "{not-json");
  const recent = createRecent(storage);
  assert.deepEqual(recent.list(), []);
  recent.record("work_f1", "card_w", "卡W");   // 自愈可写
  assert.equal(recent.list().length, 1);
});
