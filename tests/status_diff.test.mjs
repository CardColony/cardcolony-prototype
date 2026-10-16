/* tests/status_diff.test.mjs —— 「更新现状」对比窗的行级 diff 纯函数单测（node --test，零依赖）
 * 跑法：node --test tests/status_diff.test.mjs
 *
 * 手法＝沿用项目既有的「剪段求值」（见 static/canvas_layout.js）：唯一实现是
 * static/workspace.js 里带结构性段标记的纯函数段，本测试从源文件剪出该段在 node
 * 里求值 —— 零复制，浏览器与测试共用同一份源码。
 * 段分隔线是结构性标记：改段时必动，动了这里会当场报错、不会静默。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "static", "workspace.js");
const SEG_START = "    // ==================== 现状 diff（纯函数；node 侧剪段求值，见 tests/status_diff.test.mjs） ====================";
const SEG_END = "    // ==================== 现状 diff 段终 ====================";

function loadLineDiff() {
  const src = readFileSync(SRC, "utf-8");
  const i = src.indexOf(SEG_START);
  const j = src.indexOf(SEG_END);
  assert.ok(i >= 0, "找不到 diff 段起点标记（段标记被改？同步更新本测试）");
  assert.ok(j > i, "找不到 diff 段终点标记（段标记被改？同步更新本测试）");
  const segment = src.slice(i + SEG_START.length, j);
  // 段内只应含纯函数：无 DOM/window/document 引用
  assert.ok(!/\bdocument\b|\bwindow\b/.test(segment), "diff 段必须是纯函数（禁 DOM 引用）");
  // eslint-disable-next-line no-new-func
  const fn = new Function(segment + "\nreturn lineDiff;")();
  assert.equal(typeof fn, "function", "段内应导出 lineDiff");
  return fn;
}

const lineDiff = loadLineDiff();

const kinds = (rows) => rows.map((r) => r.k).join(",");
const texts = (rows, k) => rows.filter((r) => r.k === k).map((r) => r.text);

test("两侧相同 → 全 same（不产增删行）", () => {
  const rows = lineDiff("a\nb\nc", "a\nb\nc");
  assert.equal(kinds(rows), "same,same,same");
});

test("纯新增：原为空 → 新行全 add", () => {
  const rows = lineDiff("", "x\ny");
  assert.equal(kinds(rows), "add,add");
  assert.deepEqual(texts(rows, "add"), ["x", "y"]);
});

test("纯删除：新为空 → 旧行全 del", () => {
  const rows = lineDiff("x\ny", "");
  assert.equal(kinds(rows), "del,del");
  assert.deepEqual(texts(rows, "del"), ["x", "y"]);
});

test("中间改一行 → 上下文保留 same、改动处一 del 一 add", () => {
  const rows = lineDiff("a\nOLD\nc", "a\nNEW\nc");
  assert.equal(kinds(rows), "same,del,add,same");
  assert.deepEqual(texts(rows, "del"), ["OLD"]);
  assert.deepEqual(texts(rows, "add"), ["NEW"]);
});

test("追加到尾部 → 前缀全 same、尾部 add", () => {
  const rows = lineDiff("a\nb", "a\nb\nc");
  assert.equal(kinds(rows), "same,same,add");
});

test("整体覆盖（无公共行）→ 先全 del 再全 add", () => {
  const rows = lineDiff("a\nb", "x\ny");
  assert.equal(kinds(rows), "del,del,add,add");
});

test("LCS 取最长公共子序列（不把可保留的行误报成改动）", () => {
  // a b c d  →  a c d ：只应删 b
  const rows = lineDiff("a\nb\nc\nd", "a\nc\nd");
  assert.equal(kinds(rows), "same,del,same,same");
});

test("不丢字：same+add 覆盖新文本、same+del 覆盖旧文本（逐行、保序）", () => {
  const oldT = "头\n接口：GET /a\n依赖：m1\n尾";
  const newT = "头\n接口：GET /b\n依赖：m1\n依赖：m2\n尾";
  const rows = lineDiff(oldT, newT);
  const newSide = rows.filter((r) => r.k !== "del").map((r) => r.text).join("\n");
  const oldSide = rows.filter((r) => r.k !== "add").map((r) => r.text).join("\n");
  assert.equal(newSide, newT, "same+add 必须逐行还原新文本");
  assert.equal(oldSide, oldT, "same+del 必须逐行还原旧文本");
});

test("入参不被改动（纯函数）", () => {
  const a = "x\ny", b = "y\nz";
  const a0 = a, b0 = b;
  lineDiff(a, b);
  assert.equal(a, a0);
  assert.equal(b, b0);
});

test("空/undefined 输入不抛：空串＝零行（不产虚假空行）", () => {
  assert.equal(kinds(lineDiff(undefined, undefined)), "");   // 两侧都空 → 无行
  assert.equal(kinds(lineDiff(null, "x")), "add");
  assert.equal(kinds(lineDiff("x", null)), "del");
  assert.equal(kinds(lineDiff("  ", "  ")), "same");          // 空白行是真实一行
});
