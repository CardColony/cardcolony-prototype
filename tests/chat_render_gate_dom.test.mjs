/* tests/chat_render_gate_dom.test.mjs —— 重渲染节流闸的行为网（[5715] · 性能）
 *
 * 跑法：node --test tests/chat_render_gate_dom.test.mjs
 *
 * ## 为什么有这张网
 *
 * 直播时正文（content_chunk）逐帧到，行里含**累积全文**且正文要**重跑一遍
 * Markdown 解析** ⇒ 每帧全量重渲 ⇒ 一次长回复 N 帧就是 O(N²) 的写入与解析
 * ⇒ 主线程被吃满。（思考块已改增量追加，见 chat_think_stream_dom.test.mjs；
 * 正文是 Markdown，**不能追加**，只能节流。）
 *
 * ⇒ `static/esm/flow/render-gate.js`：同一节点两次重渲染**至少隔 minMs**；
 * **被节流掉的帧不丢**（调用方保留待刷、稍后重试）⇒ 最终文本一定落地。
 *
 * 依赖：jsdom（仅测试用）；脚手架见 tests/dom_scaffold.mjs。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { setupDom, JSDOM_AVAILABLE, SKIP_REASON } from "./dom_scaffold.mjs";
import { createRenderGate } from "../static/esm/flow/render-gate.js";

const SKIP = JSDOM_AVAILABLE ? false : SKIP_REASON;

test("节流闸：N 帧 ⇒ 放行次数 ≤ 时长/间隔（不是每帧都重渲）", { skip: SKIP }, () => {
  setupDom();
  let t = 0;
  const gate = createRenderGate({ minMs: 100, now: () => t });
  const FRAMES = 600;
  const STEP_MS = 16;                  // 60fps 的帧间隔
  let allowed = 0;
  for (let i = 0; i < FRAMES; i++) {
    t = i * STEP_MS;
    if (gate.allow("n1")) allowed += 1;
  }
  const elapsedMs = FRAMES * STEP_MS;   // 9.6s
  const cap = elapsedMs / 100 + 1;     // 100ms 间隔 ⇒ 最多 ~97 次
  assert.ok(allowed <= cap, `放行次数应≤时长/间隔（${cap}），实为 ${allowed}`);
  assert.ok(allowed >= 10, `也不能节流成几乎不渲染：${allowed}`);
  assert.ok(allowed < FRAMES / 4, `应明显少于每帧都渲（${FRAMES} 帧 ⇒ ${allowed} 次放行）`);
});

test("节流闸：间隔内的帧被挡下，间隔到了才放行", { skip: SKIP }, () => {
  setupDom();
  let t = 1000;
  const gate = createRenderGate({ minMs: 100, now: () => t });
  assert.equal(gate.allow("k"), true, "第一次应当放行");
  t += 50;
  assert.equal(gate.allow("k"), false, "间隔内应被挡下");
  t += 60;
  assert.equal(gate.allow("k"), true, "间隔到了应放行");
});

test("节流闸：不同节点互不影响", { skip: SKIP }, () => {
  setupDom();
  let t = 0;
  const gate = createRenderGate({ minMs: 100, now: () => t });
  assert.equal(gate.allow("a"), true);
  assert.equal(gate.allow("b"), true, "别的节点不该被 a 的节流挡住");
  t += 10;
  assert.equal(gate.allow("a"), false);
  assert.equal(gate.allow("b"), false);
});

test("节流闸：force 立即放行（终稿/结构变化必须马上出）", { skip: SKIP }, () => {
  setupDom();
  let t = 0;
  const gate = createRenderGate({ minMs: 100, now: () => t });
  assert.equal(gate.allow("k"), true);
  t += 5;
  assert.equal(gate.allow("k"), false);
  gate.force("k");
  assert.equal(gate.allow("k"), true, "force 后必须立刻能渲染");
});

test("节流闸：forget / reset 清掉时间戳", { skip: SKIP }, () => {
  setupDom();
  let t = 0;
  const gate = createRenderGate({ minMs: 100, now: () => t });
  gate.allow("k");
  t += 5;
  gate.forget("k");
  assert.equal(gate.allow("k"), true, "forget 后不该还记得上次渲染时间");
  gate.allow("k2");
  gate.reset();
  t += 5;
  assert.equal(gate.allow("k2"), true, "reset 后不该还记得上次渲染时间");
});
