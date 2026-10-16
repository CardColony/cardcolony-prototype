/* canvas_layout.js —— 布局与避让的 node 侧加载器（card_045 E2 · 双栖模块）。
 *
 * 唯一实现 = static/canvas.js 自执行闭包内的「布局引擎（纯函数）」区段
 * （card_045 E1 已把展开态与尺寸表参数化——该区段环境无关，浏览器与 node
 * 共用同一份源码，零复制）。两侧分工：
 *   浏览器侧：按原样加载 canvas.js（普通 <script>，window 挂载照旧，
 *             work.html 不改、加载序不动）——本文件不进页面；
 *   node 侧：require 本文件，得到对 canvas.js 布局段求值后的纯函数集，
 *             供 `node --test` 在无 window / 无 DOM 环境验证。
 * 求值环境按 E1 后的调用契约注入：state.expanded（展开态，兼容段内旧引用）、
 * measureSizes（尺寸表，兼容段内旧调用——E1 后 layoutTree 已参数化，
 * 注入恒空表仅为兜底旧路径）。
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const SRC_PATH = path.join(__dirname, "canvas.js");
const SEG_START = "  // ==================== 布局引擎（纯函数） ====================";
// 段终点＝布局引擎段的**下一个同级段分隔线**。
// 不写语义注释行（如「暴露纯函数供测试/外部调用」）作锚：那类注释会被无关改造
// 顺手改写——2026-10-11 迭代四 F2③（IIFE→ES module）改写了那行注释，本加载器
// 随即静默失效。段分隔线是结构性标记，改段时必动，动了这里会当场报错、不会静默。
const SEG_END = "  // ==================== 渲染 ====================";

function loadLayoutFunctions(source) {
  const i = source.indexOf(SEG_START);
  const j = source.indexOf(SEG_END);
  if (i < 0 || j < 0 || j <= i) {
    throw new Error("canvas.js 布局段锚未命中——源码结构变更，本加载器须跟改（card_045 E2）");
  }
  const segment = source.slice(i, j);
  const factory = new Function(`
    const window = {};                       // 段内挂载行兜底（node 侧假 window）
    const state = { expanded: new Set() };   // 段内旧引用兜底（E1 后 computeLayout 走参数）
    const nodeMap = new Map();               // measureSizes 旧路径兜底
    // 注：measureSizes 不再注入——它现在位于本段之内（[phase8.6-B] 实测节点尺寸）。
    // 若将来它被移出本段，这里会当场报错（标识符未定义），不会静默。
    ${segment}
    return { computeLayout, layoutTree, resolveOverlaps, separateOverlaps, computeVisibleSet, computeProjection, GAP_Y,
             // [画布重构 2026-10-15 · 阶段 2] 增量渲染的纯决策层——与上面同一段源码、
             // 同一个 factory，不另开模块（另开就等于有了第二份实现，早晚漂移）。
             computeSubtreeVisible, collectDescendants, planSubtreeUpdate, computeSubtreeLayout };
  `);
  return factory();
}

module.exports = { loadLayoutFunctions, SRC_PATH };
module.exports.default = loadLayoutFunctions(fs.readFileSync(SRC_PATH, "utf-8"));
