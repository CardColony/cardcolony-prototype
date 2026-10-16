/* esm/nav_geo.js —— [左栏接线 步2] 行几何纯函数：微推进／图标列／树线段。
 *
 * 定稿口径（docs/exploration/左侧边栏-原型定稿-v2.md）：
 * · 项目根卡 ↔ 一级子卡之间：不画线、不缩进（depth 0/1 推进 0）；
 * · 每深一层只微微推进（INDENT=10），字基本还是齐的；
 * · 线连在图标列上：每行只画自己高度内的线段——通贯长线会在微推进后
 *   横穿到浅层行的图标上（原型实测教训，别再犯）。
 *
 * 纯函数、零 DOM、零 store——tests/s4/geo.test.js 直打。
 * 坐标全部相对行内（行是 absolute 定位的等高条，段用 left/top/height 内联）。
 */

export const NAV_INDENT = 10;        // 每层微推进（定稿：微微表示即可，不大缩进）
export const NAV_CHEV_W = 24;        // 箭头判定区宽（定稿：判定区加大好点）
export const NAV_ICO_HALF = 9;       // 图标列半宽（.nav-ico 宽 18 ÷ 2）
export const NAV_ICO_SVG_HALF = 7;   // 图标 svg 半高（14 ÷ 2，出头线从 svg 下缘接出）
// [对齐样稿 2026-10-12] 卡片内边距：样稿 .wrap.card{padding:3px 5px 5px}＋1px 边框，
// 行内容距卡片边框 6px。行是扁平 absolute 序列、没有「容器」可承担 padding，故把这 6px
// 并进 indentOf（行内容整体内收），否则文字会顶在卡片边框上。
export const NAV_PAD = 6;

/* 一级子卡（depth 0）不缩进（定稿：项目根卡↔一级子卡不缩进）；再深每层 +INDENT 微微推进。
   基数 NAV_PAD＝卡片内边距（见上）。 */
export function indentOf(depth) {
  return NAV_PAD + depth * NAV_INDENT;
}

/* 本行图标列中心 x——树线全部挂在这条列上 */
export function iconX(depth) {
  return indentOf(depth) + NAV_CHEV_W + NAV_ICO_HALF;
}

/* 本行要画的线段（相对行内坐标）。
 * guides   祖先「非末子」列的贯穿线（父的后续兄弟靠它连起来，要穿过本行）
 * first    本行是可见兄弟中的第一个 → 从父列拐弯进来的横线（行顶）
 * last     本行是可见兄弟中的最后一个 → 本列不出头线
 * openKids 已展开且子段在场（下面真的渲染了子行）→ 本列出头线
 * depth 0（一级子卡）与根卡之间不画任何父子连线（定稿） */
export function rowSegs({ depth, guides, first, last, openKids, H }) {
  const segs = [];
  const cy = Math.round(H / 2);
  for (const gx of guides) segs.push({ k: "v", x: gx, t: 0, h: H });
  if (depth >= 1) {
    if (first) segs.push({ k: "h", x: iconX(depth - 1), y: 0, w: iconX(depth) - iconX(depth - 1) });
    segs.push({ k: "v", x: iconX(depth), t: 0, h: cy });
  }
  if (openKids || (depth >= 1 && !last)) {
    const t = cy + NAV_ICO_SVG_HALF;
    segs.push({ k: "v", x: iconX(depth), t, h: H - t });
  }
  return segs;
}

/* 子行继承的 guides：非末子把自己的图标列留给后代（兄弟连线要穿过它的子树） */
export function childGuides({ depth, last, guides }) {
  return (depth >= 1 && !last) ? guides.concat(iconX(depth)) : guides;
}
