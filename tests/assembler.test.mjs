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

test("reasoning 不带 model 且尚无 assistant → pending，assistant 到达时吸收", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "进行中的思考" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "还在想" }, { seq: 3 }));
  assert.equal(list(a).length, 1, "pending 期不成节点");
  a.applyRecord(rec("assistant_text", { content: "答" }, { seq: 4 }));
  const nodes = list(a);
  assert.equal(nodes.length, 2);
  assert.equal(nodes[1].reasoning, "进行中的思考还在想");
});

test("reasoning 永不丢：无 assistant 时落兜底节点", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "无人认领的思考" }, { seq: 2 }));
  a.applyRecord(rec("error", { error: "boom" }, { seq: 3 }));
  const orphan = list(a).find((n) => n.data && n.data.orphan);
  assert.ok(orphan, "思考以兜底节点落盘");
  assert.equal(orphan.data.text, "无人认领的思考");
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
