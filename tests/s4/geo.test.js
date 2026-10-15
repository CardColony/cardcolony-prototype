/* tests/s4/geo.test.js —— [左栏接线 步2] 行几何纯函数验收（static/esm/nav_geo.js）。
 * 跑法：node --test tests/s4/geo.test.js
 *
 * 判据来源：原型自检那套几何断言（docs/exploration/左侧边栏-原型定稿-v2.md），
 * 搬成真单测——原型里的断言靠无头浏览器量坐标，这里直打纯函数，等价且更快。
 *
 * 六条：
 *   ① 一级子卡（depth 0）不缩进、不画父子连线（项目根卡↔一级子卡无线）
 *   ② 线全挂在图标列中心 → 结构上不可能压到别列的图标（原型"层级乱"的根因）
 *   ③ 每行只画自己高度内的线段（不越行）
 *   ④ 末子不出头线；展开的父卡出头线直达行底
 *   ⑤ 首子有从父列拐进来的横线，跨度恰一个推进档
 *   ⑥ guides 贯穿整行；childGuides 只把非末子的列留给后代
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  NAV_INDENT, NAV_CHEV_W, NAV_ICO_HALF, NAV_ICO_SVG_HALF, NAV_PAD,
  indentOf, iconX, rowSegs, childGuides,
} from "../../static/esm/nav_geo.js";

const H = 30;                              // 行高（与 NAV_ROW_HEIGHT 同值，对齐样稿）
const CY = Math.round(H / 2);              // 19：行中线
const OUT_T = CY + NAV_ICO_SVG_HALF;       // 26：出头线起点（图标 svg 下缘）

/* ---------- ① 一级子卡不缩进、无父子连线 ---------- */

test("G1a indentOf：depth 0 不缩进；每层只微微推进", () => {
  // [对齐样稿 2026-10-12] 基数＝卡片内边距 NAV_PAD（样稿 .wrap.card padding 5px＋边框 1px）
  assert.equal(indentOf(0), NAV_PAD, "一级子卡（depth 0）不缩进，只含卡片内边距");
  assert.equal(NAV_PAD, 6, "卡片内边距＝6px（对齐样稿）");
  assert.equal(indentOf(1), NAV_PAD + NAV_INDENT);
  assert.equal(indentOf(3), NAV_PAD + 3 * NAV_INDENT);
  assert.equal(NAV_INDENT, 10, "微推进＝10px（定稿：微微表示即可，不大缩进）");
});

test("G1b depth 0 行不画任何父子连线（项目根卡↔一级子卡无线）", () => {
  for (const first of [true, false]) {
    for (const last of [true, false]) {
      const segs = rowSegs({ depth: 0, guides: [], first, last, openKids: false, H });
      assert.deepEqual(segs, [], `depth 0 未展开时零线段（first=${first} last=${last}）`);
    }
  }
});

test("G1c depth 0 展开时只出自己的头线（连向二级），仍无缩进、无横线", () => {
  const segs = rowSegs({ depth: 0, guides: [], first: true, last: false, openKids: true, H });
  assert.equal(segs.length, 1, "只有一根头线");
  assert.deepEqual(segs[0], { k: "v", x: iconX(0), t: OUT_T, h: H - OUT_T });
  assert.equal(segs.some(s => s.k === "h"), false, "depth 0 无拐弯横线");
});

/* ---------- ② 线全挂在图标列中心（不压别列图标） ---------- */

test("G2a 任意深度/组合下，竖线 x 一律落在某列图标中心", () => {
  const base = NAV_PAD + NAV_CHEV_W + NAV_ICO_HALF;
  for (const depth of [0, 1, 2, 3, 5, 9]) {
    for (const guides of [[], [iconX(0)], [iconX(0), iconX(1)]]) {
      for (const first of [true, false]) {
        for (const last of [true, false]) {
          for (const openKids of [true, false]) {
            const segs = rowSegs({ depth, guides, first, last, openKids, H });
            for (const s of segs) {
              if (s.k !== "v") continue;
              assert.equal((s.x - base) % NAV_INDENT, 0,
                `竖线 x=${s.x} 未落在图标列中心（depth=${depth}）`);
              assert.ok(s.x >= base, `竖线 x=${s.x} 越出图标列起点 ${base}`);
            }
          }
        }
      }
    }
  }
});

test("G2b 竖线绝不落在非本列的图标区间内", () => {
  for (const depth of [0, 1, 2, 3, 5, 9]) {
    const guides = depth >= 2 ? [iconX(0)] : [];
    const segs = rowSegs({ depth, guides, first: true, last: false, openKids: true, H });
    for (const s of segs) {
      if (s.k !== "v") continue;
      for (let d = 0; d <= depth; d++) {
        const cx = iconX(d);
        if (s.x >= cx - NAV_ICO_HALF && s.x <= cx + NAV_ICO_HALF) {
          assert.equal(s.x, cx,
            `竖线 x=${s.x} 落在 depth=${d} 图标区间内但非其中心——压到图标`);
        }
      }
    }
  }
});

/* ---------- ③ 线段不越行 ---------- */

test("G3 线段一律在行高内；横线终点＝本行图标列中心", () => {
  for (const depth of [0, 1, 2, 5]) {
    for (const last of [true, false]) {
      for (const openKids of [true, false]) {
        const segs = rowSegs({ depth, guides: [iconX(0)], first: true, last, openKids, H });
        for (const s of segs) {
          if (s.k === "v") {
            assert.ok(s.t >= 0, `v 段起点越上 t=${s.t}`);
            assert.ok(s.t + s.h <= H, `v 段越下 t+h=${s.t + s.h} > ${H}`);
          } else {
            assert.ok(s.y >= 0 && s.y <= H, `h 段 y 越行 y=${s.y}`);
            assert.equal(s.x + s.w, iconX(depth), "h 段终点＝本行图标列中心");
          }
        }
      }
    }
  }
});

/* ---------- ④ 末子不出头线；展开的父卡出头线到行底 ---------- */

test("G4a 末子不出头线；非末子出", () => {
  const hasOut = (segs) => segs.some(s => s.k === "v" && s.t === OUT_T && s.h === H - OUT_T);
  const last = rowSegs({ depth: 2, guides: [], first: true, last: true, openKids: false, H });
  const nonLast = rowSegs({ depth: 2, guides: [], first: true, last: false, openKids: false, H });
  assert.equal(hasOut(last), false, "末子不出头线");
  assert.equal(hasOut(nonLast), true, "非末子出头线（连后续兄弟）");
});

test("G4b 展开的父卡出头线直达行底（与子行行顶相接）", () => {
  const segs = rowSegs({ depth: 1, guides: [], first: true, last: true, openKids: true, H });
  const out = segs.find(s => s.k === "v" && s.t === OUT_T);
  assert.ok(out, "展开即出头线");
  assert.equal(out.h, H - OUT_T, "出头线补到行底");
  assert.equal(out.x, iconX(1), "挂在本列图标中心");
});

/* ---------- ⑤ 首子拐弯横线 ---------- */

test("G5a 首子有从父列拐进来的横线，跨度恰一个推进档", () => {
  const segs = rowSegs({ depth: 3, guides: [], first: true, last: false, openKids: false, H });
  const h = segs.find(s => s.k === "h");
  assert.ok(h, "first 行有横线");
  assert.equal(h.x, iconX(2), "横线起于父列中心");
  assert.equal(h.w, iconX(3) - iconX(2), "跨度＝一个推进档");
  assert.equal(h.y, 0, "横线在行顶");
});

test("G5b 非首子无横线", () => {
  const segs = rowSegs({ depth: 3, guides: [], first: false, last: false, openKids: false, H });
  assert.equal(segs.some(s => s.k === "h"), false);
});

/* ---------- ⑥ guides 贯穿 + childGuides 继承 ---------- */

test("G6a guides 列的竖线贯穿整行高", () => {
  const gx = iconX(0);
  const segs = rowSegs({ depth: 4, guides: [gx], first: false, last: true, openKids: false, H });
  const g = segs.find(s => s.k === "v" && s.x === gx);
  assert.ok(g, "guides 列有线");
  assert.equal(g.t, 0);
  assert.equal(g.h, H, "贯穿整行");
});

test("G6b childGuides：非末子把本列留给后代；末子与 depth 0 不留", () => {
  assert.deepEqual(childGuides({ depth: 1, last: false, guides: [] }), [iconX(1)]);
  assert.deepEqual(childGuides({ depth: 1, last: true, guides: [] }), []);
  assert.deepEqual(childGuides({ depth: 0, last: false, guides: [] }), [],
    "一级子卡不留列（兄弟间不连线）");
  assert.deepEqual(childGuides({ depth: 2, last: false, guides: [iconX(0)] }),
    [iconX(0), iconX(2)], "继承既有 guides 并追加本列");
});

/* ---------- ⑦ 结构自洽：深度递增时线列也逐档右移 ---------- */

test("G7 iconX 随深度逐档右移，档距＝NAV_INDENT", () => {
  for (let d = 0; d < 8; d++) {
    assert.equal(iconX(d + 1) - iconX(d), NAV_INDENT, `depth ${d}→${d + 1} 档距`);
  }
});
