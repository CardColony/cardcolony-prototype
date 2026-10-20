/* esm/inspector.js —— 右侧「卡详情」抽屉的开合
 *
 * 为什么要单独一件：开合逻辑原先长在 workspace.js 的装配闭包里
 * （`const toggleInspector = ...; document.getElementById("inspTab").onclick = ...`）
 * ⇒ ① 导不出来，单测驱动不了；② 两个入口（顶栏「卡详情」按钮 ＋ 右缘竖条）
 * 各自绑一次 onclick，容易改一处忘一处 ⇒ **两处状态漂移**。
 *
 * 口径（逐字照搬原行为）：
 *   · 状态只有一个载体：#inspector 上的 class `open`（无 localStorage、无第二份变量）；
 *   · 两个入口**同一动作**（toggle，不是各自 set）⇒ 天然不会漂移；
 *   · 找不到面板/按钮就安静退场（不抛）——原代码对 #inspTab 是无守卫的，
 *     这里顺手补上（缺节点时旧代码会整页报错）。
 */
export function initInspectorToggle() {
  const inspector = document.getElementById("inspector");
  if (!inspector) return;
  const toggle = () => inspector.classList.toggle("open");
  for (const id of ["inspTab", "topInspBtn"]) {
    const btn = document.getElementById(id);
    if (btn) btn.onclick = toggle;
  }
}
