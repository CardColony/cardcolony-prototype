/* tests/board_resident_state.test.mjs —— 画板**常驻/释放**策略网（[2026-10-24 · 治「切页卡死」]）
 *
 * 跑法：node --test tests/board_resident_state.test.mjs
 *
 * ## 为什么有这张网
 *
 * 用户裁定：「内存不缺，不该按时间把内存态扔掉」。
 * 原来的释放策略是 **静默超时 10 分钟**（后改 60 分钟）＋ LRU 上限：
 * 切走一张卡、离开一会儿，它就被 sweep 释放 ⇒ 内存态没了 ⇒ 切回来又走
 * 「首次加载」（拉 timeline 几百 KB～26MB ＋ 重新装配 ＋ 重渲染）
 * ⇒ 用户体感的正是「切页卡死、得刷新才恢复」。
 *
 * 所以现在：**释放只剩一个理由 —— 内存封顶（residentMax）**。
 * 时间闸 idleReleaseMs = 0（关闭）。
 *
 * ## 网里钉的不变量
 *
 * | 编号 | 层 | 不变量 |
 * |---|---|---|
 * | A1 | 契约 | 默认 idleReleaseMs **必须是 0**（不许再出现任何正数默认） |
 * | A2 | 契约 | 时间闸必须被 `idleReleaseMs > 0` 守卫（配置为 0 时整段不执行） |
 * | A3 | 契约 | residentMax 仍是个**正数**（内存封顶不能被一起删掉——那才是无限增长） |
 * | A4 | 契约 | 同一模块的**所有** import 方必须用**同一个**版本戳（不一致 ⇒ 浏览器加载两份 module ⇒ 两份 sessions Map ⇒ 内存态分裂） |
 * | B1 | 行为 | 静默 **2 小时**后 sweep：**仍常驻**（旧策略 10/60 分钟会挂在这条） |
 * | B2 | 行为 | residentMax 仍是硬上限：超了按**最旧**丢，且只丢到上限 |
 * | B3 | 行为 | 守卫接得住：显式把 idleReleaseMs 配成正数时，时间闸**仍会生效**（逃生阀没被焊死） |
 * | B4 | 行为 | 铁律两道闸不变：当前在看的不释放、有任务在跑的不释放 |
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const STREAM_JS = readFileSync(path.join(ROOT, "static", "stream.js"), "utf8");

/* ================================================================ 源码工具 */

/** 去掉块注释（行注释保留：stream.js 里没有「行注释里带冒号数字」的写法，
 *  去掉反而可能误伤 URL 里的 `//`）。 */
function stripBlockComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** 取某个函数体的源码文本（按大括号配平）。 */
function funcBody(src, header) {
  const i = src.indexOf(header);
  if (i < 0) return null;
  const start = src.indexOf("{", i);
  if (start < 0) return null;
  let depth = 0;
  for (let k = start; k < src.length; k++) {
    const ch = src[k];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return src.slice(start + 1, k);
    }
  }
  return null;
}

/** 从「{ 之后」起数到配平的「}」的位置（相对 body 起点的下标）。 */
function blockSpan(body, fromIdx) {
  let depth = 0;
  for (let k = fromIdx; k < body.length; k++) {
    const ch = body[k];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return k;
    }
  }
  return -1;
}

/* ================================================================ A · 契约层（永远跑） */

test("契约 A1：默认 idleReleaseMs 必须是 0（不许再出现任何正数默认＝不许按时间释放）", () => {
  const src = stripBlockComments(STREAM_JS);
  const m = src.match(/idleReleaseMs\s*:\s*([^,\n}]+)/);
  assert.ok(m, "stream.js 的 BOARD_CFG 里应有 idleReleaseMs");
  assert.equal(m[1].trim(), "0",
    `idleReleaseMs 默认必须是 0（关闭时间释放）——当前是「${m[1].trim()}」`);
});

test("契约 A2：时间闸必须被 `idleReleaseMs > 0` 守卫（配 0 时整段不执行）", () => {
  const body = funcBody(stripBlockComments(STREAM_JS), "function sweepSessions(");
  assert.ok(body, "stream.js 里应有 sweepSessions");
  const guardAt = body.indexOf("BOARD_CFG.idleReleaseMs > 0");
  assert.ok(guardAt >= 0,
    "sweepSessions 里必须用 `idleReleaseMs > 0` 守卫时间闸——"
    + "否则 idleReleaseMs=0 时 `now - lastUsed >= 0` 恒真 ⇒ 每张卡都被秒释放");
  const cmpAt = body.indexOf(">= BOARD_CFG.idleReleaseMs");
  assert.ok(cmpAt >= 0, "sweepSessions 里应有静默超时比较式");
  // 比较式必须落在守卫块**内部**（而不是守卫块之外的另一处）
  const braceAt = body.indexOf("{", guardAt);
  const endAt = blockSpan(body, braceAt);
  assert.ok(endAt > cmpAt,
    "静默超时比较式必须在守卫块内（守卫块之外还留着比较式＝时间闸仍在生效）");
});

test("契约 A3：residentMax 仍是个正数（内存封顶不许被一起删掉）", () => {
  const src = stripBlockComments(STREAM_JS);
  const m = src.match(/residentMax\s*:\s*([^,\n}]+)/);
  assert.ok(m, "stream.js 的 BOARD_CFG 里应有 residentMax");
  const n = Number(m[1].trim());
  assert.ok(Number.isInteger(n) && n > 0,
    `residentMax 必须是正整数（内存封顶）——当前是「${m[1].trim()}」`);
});

test("契约 A4：同一模块的所有 import 方必须用同一个版本戳（否则浏览器加载两份 module ⇒ 两份 sessions Map）", () => {
  const roots = ["static", "templates"];
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = path.join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.(js|html)$/.test(name)) files.push(p);
    }
  };
  for (const r of roots) walk(path.join(ROOT, r));

  const groups = new Map();
  for (const f of files) {
    const src = readFileSync(f, "utf8");
    const re = /(?:from\s*|import\s*)["']([^"']+\?v=\d+)["']/g;
    let m;
    while ((m = re.exec(src))) {
      const spec = m[1];
      const q = spec.indexOf("?");
      const raw = spec.slice(0, q);
      if (!raw.startsWith(".")) continue;
      const abs = path.resolve(path.dirname(f), raw).replace(/\\/g, "/");
      const stamp = spec.slice(q + 1);
      if (!groups.has(abs)) groups.set(abs, new Set());
      groups.get(abs).add(stamp);
    }
  }
  const bad = [];
  for (const [mod, stamps] of groups) {
    if (stamps.size > 1) {
      bad.push(`${path.relative(ROOT, mod).replace(/\\/g, "/")} ⇒ ${[...stamps].join(" vs ")}`);
    }
  }
  assert.equal(bad.length, 0,
    `同一模块被不同版本戳引用 ⇒ 浏览器会加载两份 module 实例 ⇒ 两份 sessions Map ⇒ `
    + `内存态分裂（切回要重载、状态丢失）：\n  ${bad.join("\n  ")}`);
});

/* ================================================================ B · 行为层（jsdom） */

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

/** 起一个干净环境并把 stream.js 装进来。cacheKey 不同 ⇒ 独立的 module 实例。 */
async function loadFlow(cacheKey) {
  setupDom();
  const mod = await import(`../static/stream.js?probe=${cacheKey}`);
  return mod.EventFlow;
}

/** 把 Date.now 拨快 deltaMs，执行 fn，再拨回来。 */
function withClockShift(deltaMs, fn) {
  const real = Date.now;
  Date.now = () => real() + deltaMs;
  try { return fn(); } finally { Date.now = real; }
}

test("行为 B1：静默 2 小时后 sweep 仍常驻（旧策略 10/60 分钟会挂在这条）", { skip: SKIP }, async () => {
  const EventFlow = await loadFlow("b1");
  EventFlow.activate("w_x", "c_keep");
  assert.deepEqual(EventFlow.residentKeys(), ["w_x/c_keep"]);

  // 拨快 2 小时：老策略（60 分钟）下这张卡早该被释放了
  withClockShift(2 * 3600 * 1000, () => EventFlow.sweepSessions());

  assert.deepEqual(EventFlow.residentKeys(), ["w_x/c_keep"],
    "静默 2 小时后仍必须常驻——按时间释放会让切回变首次加载（用户体感的「切页卡死」）");
});

test("行为 B2：residentMax 仍是硬上限——超了按最旧丢，只丢到上限", { skip: SKIP }, async () => {
  const src = stripBlockComments(STREAM_JS);
  const R = Number(src.match(/residentMax\s*:\s*([^,\n}]+)/)[1].trim());
  const EventFlow = await loadFlow("b2");

  // 造 R+2 张卡，lastUsed 递增（保证 LRU 顺序确定）
  let t = Date.now();
  const realNow = Date.now;
  Date.now = () => t;
  try {
    for (let i = 0; i < R + 2; i++) {
      t += 1000;
      EventFlow.activate("w_lru", `c_${i}`);
    }
  } finally {
    Date.now = realNow;
  }
  assert.equal(EventFlow.residentKeys().length, R + 2, "造卡阶段不该释放");

  EventFlow.sweepSessions();
  const left = EventFlow.residentKeys();
  assert.equal(left.length, R, `常驻数必须被压回 residentMax=${R}（内存封顶）`);
  assert.ok(!left.includes("w_lru/c_0"),
    "最旧那张（c_0）该被丢（LRU）——内存封顶是唯一的释放理由");
  assert.ok(left.includes(`w_lru/c_${R + 1}`), "当前在看的那张永不释放");
});

test("行为 B3：守卫接得住——显式配成正数时时间闸仍生效（逃生阀没被焊死）", { skip: SKIP }, async () => {
  setupDom();
  // 现场可用 window.__FLOW_BOARD__ 临时开启时间闸（BOARD_CFG 在模块求值时读它）
  globalThis.window.__FLOW_BOARD__ = { idleReleaseMs: 1000 };
  let EventFlow;
  try {
    EventFlow = (await import("../static/stream.js?probe=b3")).EventFlow;
    // 造两张卡：c_2 是当前在看的那张（铁律：永不释放），
    // c_1 是切走的那张 —— 时间闸该只放它。
    EventFlow.activate("w_y", "c_1");
    EventFlow.activate("w_y", "c_2");
    withClockShift(60 * 1000, () => EventFlow.sweepSessions());
    assert.deepEqual(EventFlow.residentKeys(), ["w_y/c_2"],
      "idleReleaseMs 配成 1000 时，静默 60 秒的切走卡必须被释放（守卫不是焊死的死代码）；"
      + "当前在看的那张按铁律留着");
  } finally {
    delete globalThis.window.__FLOW_BOARD__;
  }
});

test("行为 B4：铁律两道闸不变——当前在看的不释放、有任务在跑的不释放", { skip: SKIP }, async () => {
  const EventFlow = await loadFlow("b4");
  EventFlow.activate("w_z", "c_active");
  EventFlow.activate("w_z", "c_other");

  // 让 c_other 处于「有任务在跑」：start() 会置 busy（无网络也能起，es 建不建无所谓）
  EventFlow.start("w_z", "c_other", () => {}, {});
  withClockShift(10 * 3600 * 1000, () => EventFlow.sweepSessions());

  const left = EventFlow.residentKeys();
  assert.ok(left.includes("w_z/c_active"), "当前在看的不许释放");
  assert.ok(left.includes("w_z/c_other"), "有任务在跑的不许释放");
});
