/* tests/assembler.test.mjs —— 装配器单测（node --test，零依赖）
 * 跑法：node --test tests/assembler.test.mjs
 *
 * [对话流照抄 dsh 完全版 v3 2026-10-17] 装配器改为 **dsh 的扁平节点表**：
 *     snapshot() = { order: string[], nodes: Map<key, node>, rev }
 * 因此本文件从「按轮分组」的断言改成「按到达序的扁平表」断言；语义用例
 * （工具合体 / 思考归属 / revoke / 去重 / 引用稳定 / 乐观上屏）逐条保留。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { createAssembler, REGISTRY } from "../static/esm/assembler.js";
import { rebuildReasoningText } from "../static/esm/flow/think-stream.js";

/** 造一条落盘形态记录（七键骨架：seq/card_id/run_id/round/type/data/model）。 */
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

/** 扁平表 → 节点数组（按 order 序）。 */
function list(a) {
  const s = a.snapshot();
  return s.order.map((k) => s.nodes.get(k));
}

/* ── 1. 扁平表：按到达序，跨轮零分组 ── */
test("多轮：全部节点在同一个 order 里，按到达序排列", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "第一问" }, { run_id: "A" }));
  a.applyRecord(rec("judgment", { verdict: "plan" }, { run_id: "A" }));
  a.applyRecord(rec("tool_call", { tool: "read_card" }, { run_id: "A" }));
  a.applyRecord(rec("assistant_text", { content: "第一答" }, { run_id: "A" }));
  a.applyRecord(rec("user_message", { content: "第二问" }, { run_id: "B" }));
  a.applyRecord(rec("judgment", { verdict: "direct" }, { run_id: "B" }));
  a.applyRecord(rec("assistant_text", { content: "第二答" }, { run_id: "B" }));

  const nodes = list(a);
  assert.equal(nodes.length, 7, "七个节点全在扁平表里（不再有轮容器）");
  assert.deepEqual(nodes.map((n) => n.type), [
    "user_message", "judgment", "tool_call", "assistant_text",
    "user_message", "judgment", "assistant_text",
  ], "顺序＝到达序（真实时序）");
  assert.equal(nodes[4].run_id, "B", "run_id 仍留在节点上（不分组，但字段不丢）");
});

test("迁移老卡形态（run_id 全空）同样按到达序扁平排列", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ seq: 1, round: 0, type: "user_message", data: { content: "q0" } });
  a.applyRecord({ seq: 2, round: 0, type: "assistant_text", data: { content: "a0" } });
  a.applyRecord({ seq: 3, round: 1, type: "user_message", data: { content: "q1" } });
  a.applyRecord({ seq: 4, round: 1, type: "assistant_text", data: { content: "a1" } });
  assert.deepEqual(list(a).map((n) => n.type),
    ["user_message", "assistant_text", "user_message", "assistant_text"]);
});

/* ── 2. tool call/result 合体 ── */
test("tool：call 与 result 合体成一条，位置取 call 的到达序", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }));
  a.applyRecord(rec("tool_call", { tool: "read_card", target: "card_001" }, { seq: 10 }));
  a.applyRecord(rec("judgment", { verdict: "direct" }, { seq: 11 }));
  a.applyRecord(rec("round_start", { round: 2 }, { seq: 12 }));
  a.applyRecord(rec("tool_result", { tool: "read_card", ok: true }, { seq: 13 }));

  const nodes = list(a);
  assert.equal(nodes.length, 4, "call/result 合体后共 4 条");
  assert.equal(nodes[1].type, "tool_result", "合成节点转为终态");
  assert.equal(nodes[1].seq, 10, "位置保留 call 的 seq（不挪位）");
  assert.equal(nodes[1].call.tool, "read_card");
  assert.equal(nodes[1].result.ok, true);
  assert.equal(nodes[1].closed, true);
});

test("tool：连着两次调用各自配对（最近未闭合优先）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_call", { tool: "A" }, { seq: 1 }));
  a.applyRecord(rec("tool_result", { tool: "A", ok: true }, { seq: 2 }));
  a.applyRecord(rec("tool_call", { tool: "B" }, { seq: 3 }));
  a.applyRecord(rec("tool_result", { tool: "B", ok: false }, { seq: 4 }));
  const nodes = list(a);
  assert.equal(nodes.length, 2, "两组调用各自合体");
  assert.equal(nodes[0].call.tool, "A");
  assert.equal(nodes[0].result.ok, true);
  assert.equal(nodes[1].call.tool, "B");
  assert.equal(nodes[1].result.ok, false);
});

test("tool：有 tool_call_id 时精确配对，不吃最近的", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_call", { tool: "A", tool_call_id: "id-A" }, { seq: 1 }));
  a.applyRecord(rec("tool_call", { tool: "B", tool_call_id: "id-B" }, { seq: 2 }));
  a.applyRecord(rec("tool_result", { tool: "B", ok: true, tool_call_id: "id-B" }, { seq: 3 }));
  const nodes = list(a);
  assert.equal(nodes.length, 2, "id-B 的 result 并回 B，A 仍未闭合");
  assert.equal(nodes[1].call.tool_call_id, "id-B");
  assert.equal(nodes[1].result.ok, true);
  assert.equal(nodes[0].closed, false);
});

test("tool：孤立 result 不丢（无配对则独立成终态节点）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_result", { tool: "orphan", ok: true }, { seq: 1 }));
  const nodes = list(a);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].kind, "tool");
  assert.equal(nodes[0].result.tool, "orphan");
});

/* ── 3. reasoning 归属 ── */
test("reasoning 带 model → 并进同 run 的 seats 席卡成员", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("seats_enter", { steps: 2 }, { seq: 1 }));
  a.applyRecord(rec("seat_start", { model: "m1" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "席位思考一" }, { seq: 3, model: "m1" }));
  a.applyRecord(rec("reasoning", { text: "席位思考二" }, { seq: 4, model: "m2" }));
  a.applyRecord(rec("seat_end", { model: "m1", ok: true, text: "观点" }, { seq: 5 }));
  const nodes = list(a);
  assert.equal(nodes.length, 1, "seats 全程一个节点");
  const seats = nodes[0];
  assert.equal(seats.entered, true);
  assert.equal(seats.members.m1.thinking, "席位思考一");
  assert.equal(seats.members.m1.end.text, "观点");
  assert.equal(seats.members.m2.thinking, "席位思考二");
});

test("reasoning 不带 model → 并进最近一条 assistant 的思考区（拼接）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("assistant_text", { content: "答" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "先想…" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "再想…" }, { seq: 3 }));
  const nodes = list(a);
  assert.equal(nodes.length, 1, "思考不成独立节点");
  assert.equal(nodes[0].reasoning, "先想…再想…", "两段拼进同一条 assistant");
});

/* [5715 · 契约变更] 思考的落盘归属改判据：
 *   原：并进「最近一条 assistant」（**不限 run、不限轮**）⇒ 长链 run 的思考挂到前一个
 *       run 的消息上、同 run 内轮 1 的思考挂到轮 2 的回复上（用户亲报：刷新后
 *       「工具归工具、内容给内容、信息顺序全乱」）。
 *   现：**每 (run, round) 一个 assistant 节点**（键与 live 路径的
 *       `stream:{run}:{round}` 相同）⇒ 思考归本轮自己的节点，位置＝思考到达处
 *       （工具之前），与活体逐点一致；该轮后续 assistant_text **定稿**同一节点。
 *   老数据（round 缺失/为 0）仍走 pending 兜底，不回归。
 */
test("reasoning 不带 model 且尚无 assistant → 建本轮节点（思考-only 的一步），assistant 到达时定稿它", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "进行中的思考" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "还在想" }, { seq: 3 }));
  const mid = list(a);
  assert.equal(mid.length, 2, "思考立即建本轮节点（不再等 assistant 才落位）");
  assert.equal(mid[1].kind, "assistant");
  assert.equal(mid[1].reasoning, "进行中的思考还在想");
  a.applyRecord(rec("assistant_text", { content: "答" }, { seq: 4 }));
  const nodes = list(a);
  assert.equal(nodes.length, 2, "assistant 到达是**定稿**同一节点，不新增");
  assert.equal(nodes[1].data.content, "答");
  assert.equal(nodes[1].reasoning, "进行中的思考还在想", "思考不被正文覆盖");
});

test("reasoning 永不丢：无 assistant 时也落节点（思考-only 的一步）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "无人认领的思考" }, { seq: 2 }));
  a.applyRecord(rec("error", { error: "boom" }, { seq: 3 }));
  const nodes = list(a);
  const holder = nodes.find((n) => n.reasoning && n.reasoning.includes("无人认领的思考"));
  assert.ok(holder, "思考必须落到某个节点上（不丢文本）");
  assert.equal(holder.reasoning, "无人认领的思考");
});

/* ── 4. revoke 软删 ── */
test("revoke：from_seq 之后的节点全剔，其后的新记录不受影响", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "旧问" }, { seq: 1, run_id: "A" }));
  a.applyRecord(rec("judgment", { verdict: "direct" }, { seq: 2, run_id: "A" }));
  a.applyRecord(rec("assistant_text", { content: "旧答" }, { seq: 3, run_id: "A" }));
  a.applyRecord({ seq: 4, type: "revoke", run_id: "A", round: 1, data: { from_seq: 2 } });
  let nodes = list(a);
  assert.equal(nodes.length, 1, "seq>=2 的三条被撤，只剩 seq=1");
  assert.equal(nodes[0].seq, 1);
  a.applyRecord(rec("user_message", { content: "新问" }, { seq: 5, run_id: "B" }));
  nodes = list(a);
  assert.equal(nodes.length, 2, "revoke 之后的新记录照常入表");
  assert.equal(nodes[1].data.content, "新问");
});

test("revoke 自身不成节点（控制位不进渲染面）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("revoke", { from_seq: 99 }, { seq: 2 }));
  assert.equal(list(a).length, 1);
  assert.ok(!list(a).some((n) => n.type === "revoke"));
});

/* ── 5. 兜底与健壮性 ── */
test("未知类型落 sys 兜底节点，不丢不炸", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("brand_new_event", { x: 1 }, { seq: 1 }));
  const nodes = list(a);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].kind, "sys");
  assert.equal(nodes[0].data.x, 1);
});

test("注册表覆盖任务书全部类型，缺省三类已补（card_message/triage_verdict/ping）", () => {
  for (const t of [
    "user_message", "assistant_text", "msg_ask", "msg_answer", "msg_notify",
    "tool_call", "tool_result", "judgment", "plan_created", "step_start", "step_advanced",
    "seats_enter", "seat_start", "seat_end", "synthesize", "deliver", "proposal",
    "done", "error", "rescue", "card_op", "context", "round_start", "round_end", "run_start",
    "context_preamble", "context_injection", "judgment_protocol", "context_seat_identity",
    "status_update_task", "reasoning", "card_message", "triage_verdict", "ping",
  ]) assert.ok(REGISTRY[t], "注册表缺 " + t);
});

test("重复 seq 二次 applyRecord 不重不炸", () => {
  reset();
  const a = createAssembler();
  const r = rec("user_message", { content: "q" }, { seq: 7 });
  a.applyRecord(r);
  a.applyRecord(r);
  assert.equal(list(a).length, 1, "同一条记录只入一次");
});

test("rebuild 幂等：同一 items 重建两次快照全等", () => {
  reset();
  const a = createAssembler();
  const items = [rec("user_message", { content: "q" }, { seq: 1 }),
                 rec("assistant_text", { content: "a" }, { seq: 2 })];
  a.rebuild(items);
  const first = a.snapshot().order.slice();
  a.rebuild(items);
  assert.deepEqual(a.snapshot().order, first);
});

test("rebuild 清空：先装过再 rebuild 空数组，快照为空", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.rebuild([]);
  assert.equal(a.snapshot().order.length, 0);
});

test("disk/live 两套 seq 空间互不吞帧", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ ns: "disk", seq: 3, type: "judgment", data: { verdict: "x" } });
  a.applyRecord({ ns: "live", seq: 3, type: "judgment", data: { verdict: "y" } });
  assert.equal(list(a).length, 2, "同名 seq 不同命名空间必须都留下");
});

test("畸形输入不炸：null / 无 type / data 非对象", () => {
  reset();
  const a = createAssembler();
  assert.doesNotThrow(() => {
    a.applyRecord(null);
    a.applyRecord({ seq: 1 });
    a.applyRecord({ seq: 2, type: "judgment", data: "裸字符串" });
  });
  const nodes = list(a);
  assert.equal(nodes.length, 1);
  assert.deepEqual(nodes[0].data, { value: "裸字符串" }, "非 dict 载荷按后端同语义装箱");
});

/* ── 6. 键与版本 ── */
test("节点 key：跨合体恒等（渲染层据此复用同一 DOM）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_call", { tool: "T", tool_call_id: "x" }, { seq: 5 }));
  const keyBefore = list(a)[0].key;
  a.applyRecord(rec("tool_result", { tool: "T", tool_call_id: "x", ok: true }, { seq: 6 }));
  assert.equal(list(a)[0].key, keyBefore, "合体不换 key");
});

test("节点 rev：内容变则递增；同一节点对象跨多次 apply 恒定", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("seats_enter", {}, { seq: 1 }));
  const node = list(a)[0];
  const rev0 = node.rev;
  a.applyRecord(rec("seat_start", { model: "m1" }, { seq: 2 }));
  assert.equal(list(a)[0], node, "同一节点对象（活引用）");
  assert.ok(node.rev > rev0, "内容变 ⇒ rev 递增");
});

test("快照引用稳定：结构未变 → 同一引用；结构变 → 换引用", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  const s1 = a.snapshot();
  assert.equal(a.snapshot(), s1, "结构未变必须返回同一快照对象");
  const order1 = s1.order;
  a.applyRecord(rec("assistant_text", { content: "a" }, { seq: 2 }));
  const s2 = a.snapshot();
  assert.notEqual(s2, s1, "结构变必须换快照");
  assert.notEqual(s2.order, order1, "结构变必须换 order 数组");
});

/* ── 7. 乐观上屏（扁平表版） ── */
test("乐观上屏：本地节点带 pending；真实 run 到达即被认领（补 run_id）", () => {
  const a = createAssembler();
  const local = a.applyRecord({
    ns: "local", seq: 0, local_key: "u1", type: "user_message",
    run_id: "", round: 0, data: { content: "我的问题" }, model: "",
  });
  assert.equal(local.pending, true, "本地节点必须带 pending");
  assert.equal(local.run_id, "", "认领前没有 run 锚");
  a.applyRecord({ ns: "live", seq: 1, run_id: "R9", type: "run_start", data: {} });
  assert.equal(local.run_id, "R9", "真实 run 到达 ⇒ 认领");
  assert.equal(local.claimed, true);
});

test("乐观上屏：真实 user_message 到达 → 原地升级，不出双节点", () => {
  const a = createAssembler();
  const local = a.applyRecord({
    ns: "local", seq: 0, local_key: "u1", type: "user_message",
    run_id: "", round: 0, data: { content: "我的问题" }, model: "",
  });
  const up = a.applyRecord(rec("user_message", { content: "我的问题" }, { seq: 42, run_id: "R9" }));
  assert.equal(up, local, "升级的是同一个节点对象");
  assert.equal(up.pending, false, "pending 清除");
  assert.equal(up.seq, 42, "补上落盘 seq");
  assert.equal(list(a).length, 1, "只有一个用户节点");
});

test("乐观上屏：内容不同则不误并", () => {
  const a = createAssembler();
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
    run_id: "", round: 0, data: { content: "本地这句" }, model: "" });
  a.applyRecord(rec("user_message", { content: "别的一句" }, { seq: 9 }));
  assert.equal(list(a).length, 2, "内容不同各自成节点");
});

test("dropLocal：发送失败撤下未被认领的本地节点", () => {
  const a = createAssembler();
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
    run_id: "", round: 0, data: { content: "幽灵" }, model: "" });
  assert.equal(a.dropLocal(), true);
  assert.equal(list(a).length, 0, "本地节点撤净");
});

test("dropLocal：已被认领的本地节点不误伤", () => {
  const a = createAssembler();
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
    run_id: "", round: 0, data: { content: "已认领" }, model: "" });
  a.applyRecord({ ns: "live", seq: 1, run_id: "R1", type: "run_start", data: {} });
  a.dropLocal();
  assert.ok(list(a).some((n) => n.ns === "local"), "认领后的节点属正式轮，不得撤");
});

test("兜底思考节点不与尾节点撞 key（渲染层按 key 复用 DOM）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 5 }));
  a.applyRecord(rec("reasoning", { text: "孤思考" }, { seq: 6 }));
  a.applyRecord(rec("error", { error: "x" }, { seq: 7 }));
  const keys = a.snapshot().order;
  assert.equal(new Set(keys).size, keys.length, "key 必须两两不同");
});

/* ── [5702 · C-1 2026-10-20] 逐字流进节点（dsh assistant/chunk 语义）──
   这三条锁的是「中间轮文字被吞」的根因：此前正文只写旁挂 DOM、
   round_start 一清就没；现在按 run:round 累积进节点、落盘就地定稿。 */

test("content_chunk：同 run+round 累积进同一个 assistant 节点", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "问" }, { run_id: "R", round: 1 }));
  a.applyRecord(rec("content_chunk", { text: "你" }, { run_id: "R", round: 1, ns: "live" }));
  const n2 = a.applyRecord(rec("content_chunk", { text: "好" }, { run_id: "R", round: 1, ns: "live" }));
  const nodes = list(a);
  assert.equal(nodes.length, 2, "两帧正文只出一个节点");
  assert.equal(nodes[1].key, "stream:R:1", "键＝run:round（稳定，不随 seq 变）");
  assert.equal(nodes[1].data.text, "你好", "正文按到达序累积");
  assert.ok(n2.rev > 1, "内容变化 rev 递增（渲染层据此换这一行）");
});

test("content_chunk：换轮开新节点，上一轮文字不清", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("content_chunk", { text: "第一轮的话" }, { run_id: "R", round: 1, ns: "live" }));
  a.applyRecord(rec("content_chunk", { text: "第二轮的话" }, { run_id: "R", round: 2, ns: "live" }));
  const nodes = list(a);
  assert.equal(nodes.length, 2, "两轮两个节点，互不覆盖");
  assert.equal(nodes[0].data.text, "第一轮的话", "第一轮文字仍在");
  assert.equal(nodes[1].data.text, "第二轮的话");
});

test("assistant_text：同 run+round 就地定稿，不新增重复节点", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("content_chunk", { text: "流式草稿" }, { run_id: "R", round: 1, ns: "live" }));
  const fin = a.applyRecord(rec("assistant_text", { content: "最终定稿" }, { run_id: "R", round: 1 }));
  const nodes = list(a);
  assert.equal(nodes.length, 1, "流式与落盘是同一个节点（不出双份）");
  assert.equal(nodes[0].streaming, false, "定稿后不再是 streaming");
  assert.equal(nodes[0].data.content, "最终定稿");
  assert.equal(nodes[0].data.text, "流式草稿", "流式原文保留作兜底");
  assert.equal(fin, nodes[0], "返回的就是那个节点（渲染层只换这一行）");
});

test("reasoning_chunk：主模型思考落本轮流式节点，不串到上一轮", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("content_chunk", { text: "第一轮" }, { run_id: "R", round: 1, ns: "live" }));
  a.applyRecord(rec("reasoning_chunk", { text: "第二轮的思考" }, { run_id: "R", round: 2, ns: "live" }));
  a.applyRecord(rec("content_chunk", { text: "第二轮" }, { run_id: "R", round: 2, ns: "live" }));
  const nodes = list(a);
  assert.equal(nodes.length, 2);
  assert.equal(nodes[0].reasoning, undefined, "第一轮不吸收第二轮的思考");
  assert.equal(nodes[1].reasoning, "第二轮的思考");
});

/* ══════════════ P19 · 轮次出现两次 ══════════════
 * 实测（work_025/card_054，2026-10-23）：wb_exec 委派路径自己发一条 round_start
 * （phase=wb_exec，带 executor/model），委派失败回落系统路径又发一条
 * （phase=main/tools）⇒ 同一轮两条 ⇒ 界面两行「第 N 轮」。
 * 装配器按 (run_id, round) 给 round_start 稳定键 ⇒ 同轮合流成一行。 */

test("round_start：同一轮的两条（委派＋回落）只成一个节点（轮次不再出现两次）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("round_start", { round: 1, phase: "wb_exec", executor: "wb-intl", model: "m1" },
    { run_id: "R", round: 1 }));
  a.applyRecord(rec("round_start", { round: 1, phase: "tools" }, { run_id: "R", round: 1 }));
  const nodes = list(a);
  const rounds = nodes.filter((n) => n.type === "round_start");
  assert.equal(rounds.length, 1, "同一轮只留一个轮次节点（界面一行「第 N 轮」）");
  assert.equal(rounds[0].data.phase, "tools", "后到的那条更新数据（回落路径＝实际执行方）");
  assert.equal(rounds[0].data.executor, "wb-intl", "先到那条的字段不丢");
});

test("round_start：不同轮 / 不同 run 各自成节点（合流不能跨轮跨 run）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("round_start", { round: 1, phase: "main" }, { run_id: "R", round: 1 }));
  a.applyRecord(rec("round_start", { round: 2, phase: "tools" }, { run_id: "R", round: 2 }));
  a.applyRecord(rec("round_start", { round: 1, phase: "main" }, { run_id: "R2", round: 1 }));
  const rounds = list(a).filter((n) => n.type === "round_start");
  assert.equal(rounds.length, 3, "不同轮、不同 run 都不能被合掉");
});

test("round_start：没有 run_id 的老数据不按轮合流（否则不同 run 的同轮号会被吞）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ seq: 1, round: 1, type: "round_start", data: { round: 1, phase: "main" } });
  a.applyRecord({ seq: 2, round: 1, type: "round_start", data: { round: 1, phase: "main" } });
  const rounds = list(a).filter((n) => n.type === "round_start");
  assert.equal(rounds.length, 2, "无 run_id ⇒ 不合流（宁可多一行，不可吞掉别处的一轮）");
});

test("round_start：live 与 disk 两条同轮合流（两套数据源也只一行）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("round_start", { round: 1, phase: "wb_exec" }, { run_id: "R", round: 1, ns: "live" }));
  a.applyRecord(rec("round_start", { round: 1, phase: "tools" }, { run_id: "R", round: 1, ns: "disk" }));
  const rounds = list(a).filter((n) => n.type === "round_start");
  assert.equal(rounds.length, 1, "同一轮的 live/disk 两份只出一个节点");
});



/* ── 8. 窄投影契约位传播（2026-11-21 · 性能批次二）────────────────────
   view=brief 的记录带 ``_brief``——装配器要把它传到节点上，渲染层据此
   在展开时按 seq 回取完整记录（timeline/event）。 */

test("_brief 标记传到节点（sys 类）", () => {
  reset();
  const a = createAssembler();
  const r = rec("card_block_edited",
    { block: "现状", by: "card_x", old: "…", new: "…" }, { seq: 5 });
  r._brief = true;
  a.applyRecord(r);
  const nodes = list(a);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0]._brief, true, "节点带契约位");
  assert.equal(nodes[0].seq, 5, "节点 seq 就是详情回取键");
});

test("tool_result 合体节点记 _resultSeq/_resultBrief", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_call", { tool: "read_card", arguments: { p: 1 } },
    { seq: 10 }));
  const res = rec("tool_result",
    { tool: "read_card", ok: true, result: { _brief: true, ok: true } },
    { seq: 11 });
  res._brief = true;
  a.applyRecord(res);
  const nodes = list(a);
  const tn = nodes.find((n) => n.kind === "tool");
  assert.ok(tn, "call+result 合成一条");
  assert.equal(tn._resultSeq, 11, "详情回取键＝result 记录的 seq");
  assert.equal(tn._resultBrief, true, "占位标记传到节点");
});

test("非 brief 的工具结果不挂详情键", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_call", { tool: "t" }, { seq: 1 }));
  a.applyRecord(rec("tool_result", { tool: "t", ok: true, result: { ok: true } },
    { seq: 2 }));
  const tn = list(a).find((n) => n.kind === "tool");
  assert.equal(tn._resultSeq, 2, "仍记 seq（无 brief 时也安全留键）");
  assert.equal(tn._resultBrief, false, "非 brief ⇒ false");
});

test("brief reasoning 贡献段挂 _reasoningSegs（按到达序记全部段）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("assistant_text", { content: "答" },
    { run_id: "R", round: 1, seq: 10 }));
  const r = rec("reasoning", { text: "思考预览…" },
    { run_id: "R", round: 1, seq: 11 });
  r._brief = true;
  a.applyRecord(r);
  const nodes = list(a);
  const an = nodes.find((n) => n.kind === "assistant");
  assert.ok(an.reasoning.includes("思考预览"), "思考仍并入节点");
  assert.ok(Array.isArray(an._reasoningSegs) && an._reasoningSegs.length === 1,
    "贡献段入 _reasoningSegs");
  assert.equal(an._reasoningSegs[0].seq, 11);
  assert.equal(an._reasoningSegs[0].text, "思考预览…");
  assert.equal(an._reasoningSegs[0].brief, true, "brief 标记供展开时回取");
});

test("非 brief reasoning 段也记序但 brief=false（不付详情往返）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("assistant_text", { content: "答" },
    { run_id: "R", round: 1, seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "完整思考" },
    { run_id: "R", round: 1, seq: 2 }));
  const an = list(a).find((n) => n.kind === "assistant");
  assert.ok(an._reasoningSegs.length === 1, "段仍记序（混排时重建要用）");
  assert.equal(an._reasoningSegs[0].brief, false, "非 brief 段不触发详情回取");
});

test("混排思考段保持到达序（相同预览不错位的数据前提）", () => {
  reset();
  const a = createAssembler();
  const brief = (seq) => { const r = rec("reasoning", { text: "相同预览" },
    { run_id: "R", round: 1, seq }); r._brief = true; return r; };
  a.applyRecord(brief(7));                      // seq=7 先到（预览与 seq=9 相同）
  a.applyRecord(rec("reasoning", { text: "中间全文段" },
    { run_id: "R", round: 1, seq: 8 }));
  a.applyRecord(brief(9));
  a.applyRecord(rec("assistant_text", { content: "答" },
    { run_id: "R", round: 1, seq: 10 }));
  const an = list(a).find((n) => n.kind === "assistant");
  assert.deepEqual(an._reasoningSegs.map((s) => s.seq), [7, 8, 9],
    "段序＝到达序：重建按此拼接，相同预览不串位");
  assert.deepEqual(an._reasoningSegs.map((s) => s.brief), [true, false, true]);
});

test("思考先于 assistant 到达：桶暂存的贡献段随归位传递", () => {
  reset();
  const a = createAssembler();
  // round=0 的老数据：reasoning 落 pending 桶（无 run/round 归属节点）
  const r = rec("reasoning", { text: "桶里预览…" },
    { run_id: "R", round: 0, seq: 5 });
  r._brief = true;
  a.applyRecord(r);
  // assistant 到达（同 run、round 缺失 ⇒ 同 run 最近一条归位）
  a.applyRecord(rec("assistant_text", { content: "答" },
    { run_id: "R", round: 0, seq: 6 }));
  const an = list(a).find((n) => n.kind === "assistant");
  assert.ok(an && an._reasoningSegs && an._reasoningSegs.length === 1,
    "桶里的贡献段随 assistant 归位传递");
  assert.equal(an._reasoningSegs[0].seq, 5);
  assert.equal(an._reasoningSegs[0].brief, true);
});

/* ── 思考详情重建（窄投影·修 2026-11-21）：按贡献段序拼接，
   撤销/缺段/相同预览各就其位 ─────────────────────────────────── */
test("rebuildReasoningText: brief 段换全文、非 brief 段原样、段序保持", () => {
  const segs = [
    { seq: 7, text: "预览A", brief: true },
    { seq: 8, text: "中间全文段", brief: false },
    { seq: 9, text: "预览B", brief: true },
  ];
  const items = [
    { seq: 7, data: { text: "全文A" } },
    { seq: 9, data: { text: "全文B" } },
  ];
  assert.equal(rebuildReasoningText(segs, items, []), "全文A中间全文段全文B");
});

test("rebuildReasoningText: 两段预览相同不拼错位（修字符串替换 bug）", () => {
  const segs = [
    { seq: 7, text: "相同预览", brief: true },
    { seq: 9, text: "相同预览", brief: true },
  ];
  const items = [
    { seq: 7, data: { text: "AAAA" } },
    { seq: 9, data: { text: "BBBB" } },
  ];
  // 旧实现 full.replace("相同预览","AAAA") 只换首个 ⇒ "AAAA相同预览" 再换错。
  // 段序拼接 ⇒ 各自就位。
  assert.equal(rebuildReasoningText(segs, items, []), "AAAABBBB");
});

test("rebuildReasoningText: 撤销段换标记，旧预览不冒充全文", () => {
  const segs = [
    { seq: 7, text: "预览A", brief: true },
    { seq: 8, text: "保留段", brief: false },
  ];
  assert.equal(rebuildReasoningText(segs, [], [7]),
    "〔该段思考已撤销〕保留段");
});

test("rebuildReasoningText: 缺段留预览（未命中也未列撤销）", () => {
  const segs = [
    { seq: 7, text: "预览A", brief: true },
    { seq: 8, text: "保留段", brief: false },
  ];
  assert.equal(rebuildReasoningText(segs, [], []), "预览A保留段");
});

/* ── 前插增量装配（性能批次二·任务二 2026-11-22）─────────────────
   多页装配结果与一次性装配逐项对账：可见顺序、工具归属、同轮思考、
   缺字段老记录；覆盖跨页配对、重复回包、ts/seq 乱序、跨页撤销。 */

function recTs(type, ts, data = {}, opts = {}) {
  const r = rec(type, data, opts);
  r.ts = ts;
  return r;
}

test("prepend：两页拼装 == 一次全量装配（顺序/内容/节点数一致）", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const older = [
    recTs("user_message", T(1), { content: "问1" }, { seq: 1 }),
    recTs("reasoning", T(2), { text: "思1" }, { seq: 2, round: 1 }),
    recTs("assistant_text", T(3), { content: "答1" }, { seq: 3, round: 1 }),
    recTs("tool_call", T(4), { name: "read_file", tool_call_id: "c1" }, { seq: 4 }),
    recTs("tool_result", T(5), { output: "结果1", tool_call_id: "c1" }, { seq: 5 }),
  ];
  const newer = [
    recTs("user_message", T(6), { content: "问2" }, { seq: 6 }),
    recTs("reasoning", T(7), { text: "思2" }, { seq: 7, round: 2 }),
    recTs("assistant_text", T(8), { content: "答2" }, { seq: 8, round: 2 }),
    recTs("done", T(9), {}, { seq: 9 }),
  ];
  const full = createAssembler();
  full.rebuild(older.concat(newer));
  const inc = createAssembler();
  inc.rebuild(newer);
  const res = inc.prepend(older);
  assert.equal(res.records, 5);
  assert.equal(res.added.length, 3,
    "5 条旧页记录出 3 个新节点（思考+答并入同轮节点，工具调用结果合体）");
  const fa = list(full), ia = list(inc);
  assert.deepEqual(ia.map((n) => n.key), fa.map((n) => n.key),
    "顺序：前插装配与全量重建的可见键序一致");
  for (let i = 0; i < fa.length; i++) {
    assert.deepEqual(
      JSON.parse(JSON.stringify(ia[i].data || {})),
      JSON.parse(JSON.stringify(fa[i].data || {})),
      "节点 " + i + " 内容一致");
  }
  const tool = ia.find((n) => n.kind === "tool");
  assert.ok(tool.call && tool.result && tool.closed, "跨页工具行合体");
});

test("prepend：调用在旧页结果在新页 ⇒ 孤儿结果行被调用归并", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([
    recTs("tool_result", T(5), { output: "R", tool_call_id: "c9" }, { seq: 5 }),
    recTs("assistant_text", T(6), { content: "尾" }, { seq: 6, round: 1 }),
  ]);
  const res = a.prepend([
    recTs("tool_call", T(4), { name: "read_file", tool_call_id: "c9" }, { seq: 4 }),
  ]);
  const tools = list(a).filter((n) => n.kind === "tool");
  assert.equal(tools.length, 1, "调用并入结果行，不出双行");
  assert.equal(tools[0].call.name, "read_file");
  assert.equal(tools[0].result.output, "R");
  assert.ok(tools[0].closed);
  /* [任务二收尾] 与全量重建同形态：节点换到**调用**的键与位置（重建时
     节点生在调用处），不再留在回执处。 */
  assert.equal(tools[0].seq, 4, "节点身份＝调用 seq");
  assert.equal(tools[0].ts, T(4));
  assert.deepEqual(list(a).map((n) => n.seq), [4, 6], "行序＝调用处，与重建一致");
  assert.equal(tools[0]._resultSeq, 5, "详情回取键不丢");
  assert.ok(res.added.includes(tools[0].key), "换键后按新行记 added（旧键随差集卸载）");
  // 与全量重建逐项一致
  const b = createAssembler();
  b.rebuild([
    recTs("tool_call", T(4), { name: "read_file", tool_call_id: "c9" }, { seq: 4 }),
    recTs("tool_result", T(5), { output: "R", tool_call_id: "c9" }, { seq: 5 }),
    recTs("assistant_text", T(6), { content: "尾" }, { seq: 6, round: 1 }),
  ]);
  assert.deepEqual(a.snapshot().order, b.snapshot().order, "键序 == 全量重建");
});

test("prepend：「调用→用户→回执」跨页行序/详情/节点数全对账", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  // 已装窗口：用户消息(seq5) + 孤儿回执(seq6)
  const a = createAssembler();
  const orph = recTs("tool_result", T(6), { output: "R", tool_call_id: "cX" }, { seq: 6 });
  orph._brief = true;
  a.rebuild([
    recTs("user_message", T(5), { content: "插话" }, { seq: 5 }),
    orph,
    recTs("assistant_text", T(7), { content: "尾" }, { seq: 7, round: 2 }),
  ]);
  a.prepend([
    recTs("tool_call", T(4), { name: "read_card", tool_call_id: "cX" },
      { seq: 4, round: 1, model: "m1" }),
  ]);
  const nodes = list(a);
  assert.equal(nodes.filter((n) => n.kind === "tool").length, 1);
  const tool = nodes[0];
  assert.equal(tool.kind, "tool", "工具行在序列最前（调用处）");
  assert.equal(tool.seq, 4);
  assert.equal(tool.round, 1, "节点字段取自调用记录");
  assert.equal(tool.model, "m1");
  assert.equal(tool._resultSeq, 6, "详情回取键随归并保留");
  assert.equal(tool._resultBrief, true, "回执 brief 位随归并保留");
  assert.deepEqual(nodes.map((n) => n.seq), [4, 5, 7]);
});

test("prepend：缺 call_id 的旧记录——调用认领 seq 紧随的首个孤儿结果", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([
    recTs("tool_result", T(5), { output: "R1" }, { seq: 5 }),
    recTs("tool_result", T(7), { output: "R2" }, { seq: 7 }),
  ]);
  a.prepend([
    recTs("tool_call", T(4), { name: "f1" }, { seq: 4 }),
    recTs("tool_call", T(6), { name: "f2" }, { seq: 6 }),
  ]);
  const tools = list(a).filter((n) => n.kind === "tool");
  assert.equal(tools.length, 2, "两对各自归并（首个未认领孤儿依次配对）");
  assert.equal(tools[0].call.name, "f1");
  assert.equal(tools[0].result.output, "R1");
  assert.equal(tools[1].call.name, "f2");
  assert.equal(tools[1].result.output, "R2");
  assert.deepEqual(tools.map((n) => n.seq), [4, 6], "均归位调用处");
  // 与全量重建逐项一致
  const b = createAssembler();
  b.rebuild([
    recTs("tool_call", T(4), { name: "f1" }, { seq: 4 }),
    recTs("tool_result", T(5), { output: "R1" }, { seq: 5 }),
    recTs("tool_call", T(6), { name: "f2" }, { seq: 6 }),
    recTs("tool_result", T(7), { output: "R2" }, { seq: 7 }),
  ]);
  assert.deepEqual(a.snapshot().order, b.snapshot().order, "键序 == 全量重建");
});

test("prepend：重复回包去重（页界重叠记录不二次装配）", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  const shared = recTs("assistant_text", T(5), { content: "界" }, { seq: 5, round: 1 });
  a.rebuild([shared, recTs("done", T(6), {}, { seq: 6 })]);
  const before = list(a).length;
  const res = a.prepend([
    recTs("user_message", T(4), { content: "旧问" }, { seq: 4 }),
    recTs("assistant_text", T(5), { content: "界" }, { seq: 5, round: 1 }),  // 重复
  ]);
  assert.equal(list(a).length, before + 1, "只新增 1 个节点");
  assert.equal(res.added.length, 1);
});

test("prepend：ts 乱序按 (ts,seq) 落位，不按到达序", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([recTs("done", T(9), {}, { seq: 9 })]);
  // 批内乱序：seq2 的 ts 比 seq1 早
  a.prepend([
    recTs("user_message", T(2), { content: "后到时戳早" }, { seq: 1 }),
    recTs("user_message", T(1), { content: "先到时戳晚" }, { seq: 2 }),
    recTs("user_message", T(3), { content: "三" }, { seq: 3 }),
  ]);
  const texts = list(a).map((n) => (n.data && n.data.content) || n.type);
  assert.deepEqual(texts.slice(0, 3), ["先到时戳晚", "后到时戳早", "三"],
    "可见序按 ts 而非到达序");
  assert.equal(texts[3], "done");
});

test("prepend：批内同 ts 保持批内到达序、整体先于已装同 ts 节点", () => {
  reset();
  const T = "2026-11-22T00:00:00.000Z";
  const a = createAssembler();
  a.rebuild([recTs("done", T, {}, { seq: 9 })]);
  a.prepend([
    recTs("user_message", T, { content: "一" }, { seq: 1 }),
    recTs("user_message", T, { content: "二" }, { seq: 2 }),
  ]);
  const texts = list(a).map((n) => (n.data && n.data.content) || n.type);
  assert.deepEqual(texts, ["一", "二", "done"]);
});

test("prepend：批内 revoke 只罩本批新建，不碰已装窗口", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([
    recTs("user_message", T(9), { content: "新问" }, { seq: 9 }),
    recTs("assistant_text", T(10), { content: "新答" }, { seq: 10, round: 1 }),
  ]);
  a.prepend([
    recTs("user_message", T(1), { content: "保留" }, { seq: 1 }),
    recTs("user_message", T(2), { content: "被撤1" }, { seq: 3 }),
    recTs("user_message", T(3), { content: "被撤2" }, { seq: 4 }),
    recTs("revoke", T(4), { from_seq: 3 }, { seq: 5 }),
  ]);
  const texts = list(a).map((n) => (n.data && n.data.content) || n.type);
  assert.deepEqual(texts, ["保留", "新问", "新答"],
    "撤销罩掉批内 seq>=3，已装窗口原样");
});

test("prepend：同轮思考前补——旧页段排在既有段之前且段序保持", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([
    recTs("reasoning", T(5), { text: "思B" }, { seq: 5, round: 1 }),
    recTs("assistant_text", T(6), { content: "答" }, { seq: 6, round: 1 }),
  ]);
  a.prepend([
    recTs("reasoning", T(1), { text: "思A1" }, { seq: 1, round: 1 }),
    recTs("reasoning", T(2), { text: "思A2" }, { seq: 2, round: 1 }),
  ]);
  const an = list(a).find((n) => n.kind === "assistant");
  assert.equal(an.reasoning, "思A1思A2思B", "旧页思考段序保持并先于既有");
  assert.deepEqual((an._reasoningSegs || []).map((s) => s.seq), [1, 2, 5]);
});

test("prepend：返回 added/touched 供渲染层只挂新行", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([recTs("done", T(9), {}, { seq: 9 })]);
  const res = a.prepend([
    recTs("round_start", T(7), { n: 1 }, { seq: 7, round: 1 }),
    recTs("round_start", T(8), { n: 2 }, { seq: 8, round: 2 }),
  ]);
  assert.equal(res.added.length, 2);
  assert.equal(res.touched.length, 0);
});

test("prepend：『调用A→调用B→回执B→回执A』跨页不互换（批尾统一配对）", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([                                            // 新页窗口：两条孤儿回执
    recTs("tool_result", T(5), { output: "RB" }, { seq: 5 }),
    recTs("tool_result", T(6), { output: "RA" }, { seq: 6 }),
  ]);
  a.prepend([                                            // 旧页：调用A、调用B
    recTs("tool_call", T(3), { name: "fA" }, { seq: 3 }),
    recTs("tool_call", T(4), { name: "fB" }, { seq: 4 }),
  ]);
  const tools = list(a).filter((n) => n.kind === "tool");
  assert.equal(tools.length, 2);
  /* 重建语义「回执绑最近前置未闭调用」：RB(T5) 绑 fB(T4)、RA(T6) 绑 fA(T3)——
     修复前 fA 到场即抢首个孤儿 RB ⇒ 互换。 */
  assert.equal(tools[0].call.name, "fA");
  assert.equal(tools[0].result.output, "RA");
  assert.equal(tools[1].call.name, "fB");
  assert.equal(tools[1].result.output, "RB");
  const b = createAssembler();                           // 与全量重建逐项一致
  b.rebuild([
    recTs("tool_call", T(3), { name: "fA" }, { seq: 3 }),
    recTs("tool_call", T(4), { name: "fB" }, { seq: 4 }),
    recTs("tool_result", T(5), { output: "RB" }, { seq: 5 }),
    recTs("tool_result", T(6), { output: "RA" }, { seq: 6 }),
  ]);
  assert.deepEqual(a.snapshot().order, b.snapshot().order, "键序 == 全量重建");
});

test("prepend：旧页回执不挂新页尚未闭合的调用（位次界）", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const a = createAssembler();
  a.rebuild([                                            // 新页：未闭合的调用B
    recTs("tool_call", T(9), { name: "fB" }, { seq: 9 }),
  ]);
  a.prepend([                                            // 旧页：调用A＋回执A
    recTs("tool_call", T(2), { name: "fA" }, { seq: 2 }),
    recTs("tool_result", T(3), { output: "RA" }, { seq: 3 }),
  ]);
  const tools = list(a).filter((n) => n.kind === "tool");
  assert.equal(tools.length, 2);
  assert.equal(tools[0].call.name, "fA");
  assert.equal(tools[0].result && tools[0].result.output, "RA",
    "回执A 归位到调用A——修复前会被新页的调用B 吸走");
  assert.equal(tools[1].call.name, "fB");
  assert.equal(tools[1].result, null, "fB 仍未闭合（没有更早的回执）");
  const b = createAssembler();
  b.rebuild([
    recTs("tool_call", T(2), { name: "fA" }, { seq: 2 }),
    recTs("tool_result", T(3), { output: "RA" }, { seq: 3 }),
    recTs("tool_call", T(9), { name: "fB" }, { seq: 9 }),
  ]);
  assert.deepEqual(a.snapshot().order, b.snapshot().order, "键序 == 全量重建");
});

/** 切点对账：四个事件按三种切法分「新页(rebuild) + 旧页(prepend)」，
    逐一切点与全量重建逐项对账行序、回执归属和详情 seq。 */
function assertSameAsRebuild(tools, refTools, tag) {
  assert.equal(tools.length, refTools.length, tag + "：节点数");
  tools.forEach((n, i) => {
    const r = refTools[i];
    assert.equal(n.call && n.call.name, r.call && r.call.name, `${tag}#${i} 调用名`);
    assert.equal(n.result && n.result.output, r.result && r.result.output,
      `${tag}#${i} 回执归属`);
    assert.equal(n._resultSeq, r._resultSeq, `${tag}#${i} 详情回取 seq`);
  });
}

test("prepend：『调用A→调用B→回执A→回执B』带 call_id——三切点均与重建一致", () => {
  reset();
  const T = (i) => new Date(1700000000000 + i * 1000).toISOString();
  const evs = [
    recTs("tool_call", T(3), { name: "fA", tool_call_id: "id-A" }, { seq: 3 }),
    recTs("tool_call", T(4), { name: "fB", tool_call_id: "id-B" }, { seq: 4 }),
    recTs("tool_result", T(5), { output: "RA", tool_call_id: "id-A" }, { seq: 5 }),
    recTs("tool_result", T(6), { output: "RB", tool_call_id: "id-B" }, { seq: 6 }),
  ];
  const ref = createAssembler();
  ref.rebuild(evs);
  const refTools = list(ref).filter((n) => n.kind === "tool");
  for (const cut of [1, 2, 3]) {
    const a = createAssembler();
    a.rebuild(evs.slice(cut));          // 新页先装（切点在调用之间 ⇒ 回执无调用在场）
    a.prepend(evs.slice(0, cut));       // 旧页补入
    assertSameAsRebuild(
      list(a).filter((n) => n.kind === "tool"), refTools, `cut=${cut}`);
    assert.deepEqual(a.snapshot().order, ref.snapshot().order,
      `cut=${cut} 键序 == 全量重建`);
  }
});

test("prepend：同时间戳无 id 调用/回执——三切点均与重建一致", () => {
  reset();
  const T = "2026-11-22T00:00:00.000Z";
  const evs = [
    recTs("tool_call", T, { name: "f1" }, { seq: 3 }),
    recTs("tool_call", T, { name: "f2" }, { seq: 4 }),
    recTs("tool_result", T, { output: "R1" }, { seq: 5 }),
    recTs("tool_result", T, { output: "R2" }, { seq: 6 }),
  ];
  const ref = createAssembler();
  ref.rebuild(evs);
  const refTools = list(ref).filter((n) => n.kind === "tool");
  for (const cut of [1, 2, 3]) {
    const a = createAssembler();
    a.rebuild(evs.slice(cut));
    a.prepend(evs.slice(0, cut));
    /* 无 id ⇒ 沿用全量重建配对语义：同 ts 切点错挂的回执要被前插调用抢回。 */
    assertSameAsRebuild(
      list(a).filter((n) => n.kind === "tool"), refTools, `cut=${cut}`);
    assert.deepEqual(a.snapshot().order, ref.snapshot().order,
      `cut=${cut} 同 ts 键序 == 全量重建`);
  }
});

function assertLegacyToolPairsAcrossAllPageBoundaries(laterResultTs) {
  reset();
  const callTs = "2026-11-22T00:00:00.000Z";
  /* 六事件：callA callB callC R1 R2 R3（无 call_id）。
     重建语义：Ri 绑「位次最优的前置未闭调用」⇒ A→R1、B→R2、C→R3。 */
  const evs = [
    recTs("tool_call", callTs, { name: "fA" }, { seq: 3 }),
    recTs("tool_call", callTs, { name: "fB" }, { seq: 4 }),
    recTs("tool_call", callTs, { name: "fC" }, { seq: 5 }),
    recTs("tool_result", callTs, { output: "R1" }, { seq: 6 }),
    recTs("tool_result", laterResultTs, { output: "R2" }, { seq: 7 }),
    recTs("tool_result", laterResultTs, { output: "R3" }, { seq: 8 }),
  ];
  const ref = createAssembler();
  ref.rebuild(evs);
  const refTools = list(ref).filter((n) => n.kind === "tool");
  /* 5 个事件间隙，每个可为页界 ⇒ 2^5-1=31 种 ≥2 页的连续分页组合。
     回放真实翻页序：rebuild(最后一页) 再逐页 prepend 更旧的页。 */
  for (let mask = 1; mask < 32; mask++) {
    const pages = [];
    let cur = [evs[0]];
    for (let g = 0; g < 5; g++) {
      if (mask & (1 << g)) { pages.push(cur); cur = []; }
      cur.push(evs[g + 1]);
    }
    pages.push(cur);
    const a = createAssembler();
    a.rebuild(pages[pages.length - 1]);
    for (let pi = pages.length - 2; pi >= 0; pi--) a.prepend(pages[pi]);
    assertSameAsRebuild(
      list(a).filter((n) => n.kind === "tool"), refTools, `mask=${mask}`);
    assert.deepEqual(a.snapshot().order, ref.snapshot().order,
      `mask=${mask} 键序 == 全量重建`);
  }
}

test("prepend：同 ts 无 id 三调用三回执——31 种分页组合全与重建一致", () => {
  assertLegacyToolPairsAcrossAllPageBoundaries("2026-11-22T00:00:00.000Z");
});

test("prepend：跨 ts 无 id 回执不重复认领待归并调用——31 种分页组合与重建一致", () => {
  assertLegacyToolPairsAcrossAllPageBoundaries("2026-11-22T00:00:01.000Z");
});

test("prepend：旧页包含完整的同 ts 无 id 工具批次，按记录序配对", () => {
  reset();
  const ts = "2026-11-22T00:00:00.000Z";
  const older = [
    recTs("tool_call", ts, { name: "fA" }, { seq: 1 }),
    recTs("tool_call", ts, { name: "fB" }, { seq: 2 }),
    recTs("tool_result", ts, { output: "R1" }, { seq: 3 }),
    recTs("tool_result", ts, { output: "R2" }, { seq: 4 }),
  ];
  const newer = [
    recTs("user_message", "2026-11-22T00:00:01.000Z", { content: "后续消息" }, { seq: 5 }),
  ];
  const ref = createAssembler();
  ref.rebuild([...older, ...newer]);
  const a = createAssembler();
  a.rebuild(newer);
  a.prepend(older);
  assertSameAsRebuild(list(a).filter((n) => n.kind === "tool"),
    list(ref).filter((n) => n.kind === "tool"), "旧页工具批次");
  assert.deepEqual(a.snapshot().order, ref.snapshot().order);
});
