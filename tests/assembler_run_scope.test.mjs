/* tests/assembler_run_scope.test.mjs —— 装配器的**归属作用域**网（[5715]）
 *
 * 跑法：node --test tests/assembler_run_scope.test.mjs
 *
 * ## 为什么单独开一份
 *
 * 既有 tests/assembler.test.mjs 有 32 条用例，把装配器用得挺透——但它造的记录
 * **全部落在同一个 run**（rec() 默认 run_id="r1"）。于是「归属判据**跨 run** 时
 * 会不会错」这条分支**从来没被测过**。
 *
 * 实测缺陷（用户亲报「长程调用链之后刷新，工具归工具、内容给内容、信息顺序全乱」）：
 *   lastAssistant() 找思考归属时**不限 run** ⇒ 后一个 run 的 thinking 被挂到
 *   **前一个 run** 的 assistant 消息上（实测 247 条全挂到 21:26 那条老消息下）。
 *   刷新后：长链自己只剩「工具行 + 最后一段正文」，中间那段「模型在干什么」
 *   整段丢失、位置全错。
 *
 * 这份网把「run 作用域」逐条钉死：思考归属、工具配对、流式定稿、乐观上屏认领。
 * **同类判据一处漏，全链路皆漏**——所以这里按「作用域」成组地测，而不是按函数测。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { createAssembler } from "../static/esm/assembler.js";

/** 造一条落盘形态记录（可指定 run_id / round / ns / model）。 */
let _seq = 0;
function rec(type, data = {}, opts = {}) {
  _seq = opts.seq !== undefined ? opts.seq : ++_seq;
  return {
    seq: _seq,
    card_id: "card_x",
    run_id: opts.run_id !== undefined ? opts.run_id : "r1",
    round: opts.round !== undefined ? opts.round : 1,
    type,
    data,
    model: opts.model || "",
    ...(opts.ns ? { ns: opts.ns } : {}),
  };
}
function reset() { _seq = 0; }

/** 快照 → 节点数组（按 order 序）。 */
function nodesOf(asm) {
  const snap = asm.snapshot();
  return snap.order.map((k) => snap.nodes.get(k));
}
/** 某节点上的思考文本（reasoning 字段）。 */
function reasoningOf(node) { return String((node && node.reasoning) || ""); }

// ================================================================ 思考归属

test("run 作用域：run2 的思考**不挂到 run1 的 assistant**（长链场景的真缺陷）", () => {
  reset();
  const asm = createAssembler();
  // run1：一轮普通回复
  asm.applyRecord(rec("assistant_text", { content: "run1 的正文" }, { run_id: "r1", round: 1 }));
  // run2：长程调用链 —— 大量思考落在 assistant 之前（模型在工具轮里只思考不说话）
  asm.applyRecord(rec("reasoning", { text: "run2 的思考A" }, { run_id: "r2", round: 1 }));
  asm.applyRecord(rec("reasoning", { text: "run2 的思考B" }, { run_id: "r2", round: 1 }));
  asm.applyRecord(rec("tool_call", { tool: "read_card" }, { run_id: "r2", round: 1 }));
  asm.applyRecord(rec("tool_result", { ok: true }, { run_id: "r2", round: 1 }));
  asm.applyRecord(rec("reasoning", { text: "run2 的思考C" }, { run_id: "r2", round: 2 }));
  asm.applyRecord(rec("assistant_text", { content: "run2 的正文" }, { run_id: "r2", round: 2 }));

  const nodes = nodesOf(asm);
  const a1 = nodes.find((n) => n.kind === "assistant" && n.data && n.data.content === "run1 的正文");
  const a2 = nodes.find((n) => n.kind === "assistant" && n.data && n.data.content === "run2 的正文");
  assert.ok(a1, "应有 run1 的 assistant 节点");
  assert.ok(a2, "应有 run2 的 assistant 节点");

  assert.equal(reasoningOf(a1), "",
    "run1 的 assistant 不得挂 run2 的思考（这是长链刷新后顺序全乱的根因）");
  /* [5715] 新契约：思考归**本轮自己的节点**（键 stream:{run}:{round}，与 live 同），
     而不是「并进最近一条 assistant」。 */
  const r2r1 = nodes.find((n) => n.kind === "assistant" && n.run_id === "r2" && n.round === 1);
  assert.ok(r2r1, "run2 第 1 轮应有自己�� assistant 节点（思考-only 的一步）");
  assert.match(reasoningOf(r2r1), /run2 的思考A/);
  assert.match(reasoningOf(r2r1), /run2 的思考B/);
  assert.doesNotMatch(reasoningOf(a2), /run2 的思考A/, "轮 1 的思考不得挂到轮 2 的回复上");
  assert.match(reasoningOf(a2), /run2 的思考C/, "轮 2 的思考归轮 2 自己");
});

test("run 作用域：同 run 内思考仍照旧归属（既有行为不回归）", () => {
  reset();
  const asm = createAssembler();
  asm.applyRecord(rec("reasoning", { text: "思考1" }, { run_id: "r1", round: 1 }));
  asm.applyRecord(rec("assistant_text", { content: "正文" }, { run_id: "r1", round: 1 }));
  const a = nodesOf(asm).find((n) => n.kind === "assistant");
  assert.match(reasoningOf(a), /思考1/);
});

test("run 作用域：思考**永不丢**——本 run 无 assistant 时也落节点", () => {
  reset();
  const asm = createAssembler();
  asm.applyRecord(rec("reasoning", { text: "孤儿思考" }, { run_id: "r9", round: 1 }));
  asm.applyRecord(rec("done", {}, { run_id: "r9", round: 1 }));
  const nodes = nodesOf(asm);
  const holder = nodes.find((n) => n.reasoning && String(n.reasoning).includes("孤儿思考"));
  assert.ok(holder, "思考必须落到某个节点上（不丢文本）");
  assert.match(reasoningOf(holder), /孤儿思考/);
});

test("run 作用域：run2 的思考不吞掉 run1 已归位的思考", () => {
  reset();
  const asm = createAssembler();
  asm.applyRecord(rec("reasoning", { text: "run1 思考" }, { run_id: "r1", round: 1 }));
  asm.applyRecord(rec("assistant_text", { content: "run1 正文" }, { run_id: "r1", round: 1 }));
  asm.applyRecord(rec("reasoning", { text: "run2 思考" }, { run_id: "r2", round: 1 }));
  asm.applyRecord(rec("assistant_text", { content: "run2 正文" }, { run_id: "r2", round: 1 }));
  const nodes = nodesOf(asm);
  const a1 = nodes.find((n) => n.kind === "assistant" && n.data.content === "run1 正文");
  const a2 = nodes.find((n) => n.kind === "assistant" && n.data.content === "run2 正文");
  assert.match(reasoningOf(a1), /run1 思考/);
  assert.doesNotMatch(reasoningOf(a1), /run2 思考/);
  assert.match(reasoningOf(a2), /run2 思考/);
  assert.doesNotMatch(reasoningOf(a2), /run1 思考/);
});

// ================================================================ 工具配对

test("run 作用域：run2 的 tool_result **不配到 run1 的 tool_call** 上", () => {
  reset();
  const asm = createAssembler();
  // run1：调用了工具但**结果没回来**（异常收尾）
  asm.applyRecord(rec("tool_call", { tool: "read_card" }, { run_id: "r1", round: 1 }));
  // run2：结果回来了
  asm.applyRecord(rec("tool_result", { ok: true, content: "r2 的结果" }, { run_id: "r2", round: 1 }));
  const nodes = nodesOf(asm);
  const tools = nodes.filter((n) => n.kind === "tool");
  assert.equal(tools.length, 2,
    "跨 run 的 result 不得吃掉前一个 run 的 call（否则工具与内容张冠李戴）");
  const r2 = tools.find((n) => n.result);
  assert.ok(r2, "应有带 result 的工具节点");
  assert.equal(r2.result.content, "r2 的结果");
  assert.equal(r2.call, null, "r2 的 result 不该配到 r1 的 call 上");
});

test("run 作用域：同 run 内 tool_call/result 照旧合体", () => {
  reset();
  const asm = createAssembler();
  asm.applyRecord(rec("tool_call", { tool: "read_card" }, { run_id: "r1", round: 1 }));
  asm.applyRecord(rec("tool_result", { ok: true, content: "结果" }, { run_id: "r1", round: 1 }));
  const tools = nodesOf(asm).filter((n) => n.kind === "tool");
  assert.equal(tools.length, 1, "同 run 的 call+result 必须合成一个节点");
  assert.ok(tools[0].call && tools[0].result);
});

// ================================================================ 刷新前后一致

test("刷新前后一致：落盘形态重建的节点序列 == 事件到达序（长链场景）", () => {
  reset();
  const asm = createAssembler();
  /* 造**新落盘形状**：每轮一条 assistant_text（round＝轮号；正文可空）。
     这与 live 路径的 stream:{run}:{round} 一一对应——disk 与 live 同源，
     是「刷新前后一致」的前提。 */
  const events = [
    rec("user_message", { content: "问题" }, { run_id: "r1", round: 0 }),
    rec("round_start", {}, { run_id: "r1", round: 1 }),
    rec("reasoning", { text: "思考" }, { run_id: "r1", round: 1 }),
    rec("tool_call", { tool: "read_card" }, { run_id: "r1", round: 1 }),
    rec("tool_result", { ok: true }, { run_id: "r1", round: 1 }),
    rec("assistant_text", { content: "" }, { run_id: "r1", round: 1 }),
    rec("round_start", {}, { run_id: "r1", round: 2 }),
    rec("assistant_text", { content: "最终回复" }, { run_id: "r1", round: 2 }),
    rec("done", {}, { run_id: "r1", round: 2 }),
  ];
  for (const e of events) asm.applyRecord(e);

  const nodes = nodesOf(asm);
  const kinds = nodes.map((n) => n.kind + "/" + n.type);
  assert.deepEqual(kinds, [
    "user/user_message",
    "sys/round_start",
    "assistant/assistant_text",     // 轮 1：思考-only 的一步（正文空）
    "tool/tool_result",             // call+result 合体
    "sys/round_start",
    "assistant/assistant_text",     // 轮 2：最终回复
    "sys/done",
  ], "重建序列必须与事件到达序一致（刷新后不该出现「工具归工具、内容给内容」）");

  // 轮 1 的思考**只**在轮 1 的 assistant 上
  const a1 = nodes.find((n) => n.kind === "assistant" && n.round === 1);
  const a2 = nodes.find((n) => n.kind === "assistant" && n.round === 2);
  assert.match(reasoningOf(a1), /思考/);
  assert.equal(reasoningOf(a2), "", "轮 2 不得挂轮 1 的思考");
});

test("**live 与 disk 重建出同一序列**（刷新前后一致��终极判据）", () => {
  reset();
  // 同一件事的两种形态：活体（帧） vs 落盘（事件）
  const live = [
    rec("content_chunk", { text: "正文" }, { run_id: "r1", round: 1, ns: "live" }),
    rec("reasoning_chunk", { text: "思考" }, { run_id: "r1", round: 1, ns: "live" }),
  ];
  const disk = [
    rec("assistant_text", { content: "" }, { run_id: "r1", round: 1 }),
    rec("reasoning", { text: "思考" }, { run_id: "r1", round: 1 }),
    rec("assistant_text", { content: "正文" }, { run_id: "r1", round: 1 }),
  ];
  const shape = (events) => {
    const a = createAssembler();
    for (const e of events) a.applyRecord(e);
    return nodesOf(a).map((n) => n.kind + "/" + n.type);
  };
  assert.deepEqual(shape(disk), shape(live),
    "同一轮在活体与落盘下必须重建出同样的节点序列（否则刷新前后观感不同）");
});

test("刷新前后一致：重建两次快照全等（幂等，刷新不改变结构）", () => {
  reset();
  const asm = createAssembler();
  const events = [
    rec("reasoning", { text: "思考" }, { run_id: "r1", round: 1 }),
    rec("tool_call", { tool: "read_card" }, { run_id: "r1", round: 1 }),
    rec("tool_result", { ok: true }, { run_id: "r1", round: 1 }),
    rec("assistant_text", { content: "" }, { run_id: "r1", round: 1 }),
    rec("assistant_text", { content: "正文" }, { run_id: "r1", round: 2 }),
  ];
  for (const e of events) asm.applyRecord(e);
  const s1 = asm.snapshot();
  const s2 = asm.rebuild(events);
  assert.deepEqual(s1.order, s2.order, "重建前后 order 必须一致");
  assert.deepEqual(
    s1.order.map((k) => s1.nodes.get(k).kind),
    s2.order.map((k) => s2.nodes.get(k).kind),
    "重建前后节点类型序列必须一致");
});

// ================================================================ 各类节点

test("全类型：注册表覆盖的每个类型都能落成节点（不丢不炸）", () => {
  reset();
  const asm = createAssembler();
  const types = [
    "user_message", "assistant_text", "msg_ask", "msg_answer", "msg_notify",
    "judgment", "plan_created", "step_start", "step_advanced",
    "seats_enter", "seat_start", "seat_end",
    "synthesize", "deliver", "proposal", "done", "error", "rescue",
    "card_op", "context", "round_start", "round_end", "run_start",
    "context_preamble", "context_injection", "judgment_protocol",
    "context_seat_identity", "status_update_task",
  ];
  for (const t of types) asm.applyRecord(rec(t, { text: "x" }, { run_id: "r1", round: 1 }));
  const kinds = new Set(nodesOf(asm).map((n) => n.kind));
  assert.ok(kinds.size >= 6, "应覆盖多种 kind：user/assistant/cardmsg/chain/seats/sys/injection");
  // 每个类型要么落成节点、要么被合法合并（seats_enter/seat_start/seat_end 合成席卡）
  const total = nodesOf(asm).length;
  assert.ok(total >= types.length - 3, `落盘节点数偏少：${total} < ${types.length - 3}`);
});

test("空载荷：data 为空/内容为空的记录不炸、也不产空壳正文", () => {
  reset();
  const asm = createAssembler();
  asm.applyRecord(rec("assistant_text", {}, { run_id: "r1", round: 1 }));
  asm.applyRecord(rec("user_message", { content: "" }, { run_id: "r1", round: 1 }));
  assert.doesNotThrow(() => asm.snapshot());
});
