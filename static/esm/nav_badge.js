/* esm/nav_badge.js —— [S3/T4] 呈现族纯函数：徽章聚合（R-33）＋非正常态判定（R-35）＋
 * 已删卡快照存在性（R-21）＋引导线常量（R-31）。
 *
 * 全部纯函数、零 DOM、零 store 依赖（V3-P2/P4 单测直打；snapshot 参数按鸭子类型只调
 * snapshot.project(wid)——生产传 store.snapshot，测试传 mock 同形态）。
 */

/* [R-33①] 项目行子树聚合：最新活跃时间戳（机械取 max，不涉语义判断）＋总卡数。
 * 取材＝project.last_active_at 与全部卡的 last_active_at / branch_active_at；
 * 全空时 latest=""（渲染层回落项目自身字段）。 */
export function projectAggregate(project) {
  const cards = (project && project.cards) || [];
  let best = 0;
  let bestIso = "";
  const probe = (iso) => {
    if (!iso) return;
    const t = Date.parse(iso);
    if (!isNaN(t) && t > best) { best = t; bestIso = iso; }
  };
  probe(project && project.last_active_at);
  for (const c of cards) { probe(c.last_active_at); probe(c.branch_active_at); }
  return { latest: bestIso, count: cards.length };
}

/* [R-33②] 节点行活跃标记：branch_active_at 距今 < 48h（机械阈值，无语义判断） */
export function nodeActive(card, now) {
  const t = card && card.branch_active_at ? Date.parse(card.branch_active_at) : NaN;
  if (isNaN(t)) return false;
  return (now || Date.now()) - t < 48 * 3600 * 1000;
}

/* [R-35] 空项目判定：project 在场且 cards 为空（未展开的项目不显示空态，由渲染层并 prefs 判定） */
export function isProjectEmpty(snapshot, wid) {
  const p = snapshot && snapshot.project ? snapshot.project(wid) : null;
  return !!p && !(p.cards || []).length;
}

/* [R-21] 快照存在性：card 是否仍在服务端快照里（点击路径先查后动的唯一数据源；
 * 行由快照渲染而来，行在快照缺失＝快照缝隙/多标签删除的兜底判定） */
export function cardInSnapshot(snapshot, wid, cid) {
  const p = snapshot && snapshot.project ? snapshot.project(wid) : null;
  if (!p) return false;
  return (p.cards || []).some(c => c.card_id === cid);
}

/* [R-31] 引导线常量（nav_render buildCardEl 按 depth 消费）：
 * NAV_GUIDE_DEPTH —— 缩进只到第 4 档（depth 0-3 普通缩进），depth ≥ 4 转引导线；
 * NAV_GUIDE_SPACING —— 引导线竖线档距（px），深档压进左沟，标题不被缩进吃光；
 * NAV_GUIDE_STEP —— 前四档普通缩进档距（与 S2 buildCardEl 既有 +14px 同源，收编成常量）。 */
export const NAV_GUIDE_DEPTH = 4;
export const NAV_GUIDE_SPACING = 4;
export const NAV_GUIDE_STEP = 14;
