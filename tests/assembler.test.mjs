/* tests/assembler.test.mjs —— 装配器单测（node --test，零依赖）
 * 跑法：node --test tests/assembler.test.mjs
 * 用例覆盖任务书 §三件1③ 的六条，另加两条针对实测落盘形态的用例
 * （run_id 分组 / 迁移老卡 round 回落分组）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { createAssembler, REGISTRY, groupKeyOf } from "../static/esm/assembler.js";

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
  };
}
function reset() { _seq = 0; }

/* ── 1. 多轮 items → snapshot 分组正确、组内有序 ── */
test("多轮：按 run_id 分组，组内 seq 升序，组序按到达序", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "第一问" }, { run_id: "A" }));
  a.applyRecord(rec("judgment", { verdict: "plan" }, { run_id: "A" }));
  a.applyRecord(rec("tool_call", { tool: "read_card" }, { run_id: "A" }));
  a.applyRecord(rec("assistant_text", { content: "第一答" }, { run_id: "A" }));
  a.applyRecord(rec("user_message", { content: "第二问" }, { run_id: "B" }));
  a.applyRecord(rec("judgment", { verdict: "direct" }, { run_id: "B" }));
  a.applyRecord(rec("assistant_text", { content: "第二答" }, { run_id: "B" }));

  const snap = a.snapshot();
  assert.equal(snap.rounds.length, 2, "两个 run 分两组");
  assert.equal(snap.rounds[0].key, "run:A");
  assert.equal(snap.rounds[1].key, "run:B");
  assert.deepEqual(
    snap.rounds[0].nodes.map((n) => n.type),
    ["user_message", "judgment", "tool_call", "assistant_text"],
    "组内按到达序（seq 升序）"
  );
  assert.deepEqual(
    snap.rounds[1].nodes.map((n) => n.type),
    ["user_message", "judgment", "assistant_text"]
  );
  // 跨轮零混排：A 组不含 B 的任何节点
  const aSeqs = new Set(snap.rounds[0].nodes.map((n) => n.type));
  assert.equal(aSeqs.has("user_message"), true);
  assert.equal(snap.rounds[0].nodes.length, 4);
});

test("分组键：run_id 优先，缺失回落 round（迁移老卡形态）", () => {
  assert.equal(groupKeyOf({ run_id: "abc", round: 3 }), "run:abc");
  assert.equal(groupKeyOf({ run_id: "", round: 3 }), "r0:3");
  assert.equal(groupKeyOf({ run_id: "", round: 0 }), "r0:0");

  reset();
  const a = createAssembler();
  // 迁移老卡实测形态：run_id 全空，round＝轮次序号，一轮＝若干 user + 一条 assistant
  a.applyRecord({ seq: 1, round: 0, type: "user_message", data: { content: "q0" } });
  a.applyRecord({ seq: 2, round: 0, type: "assistant_text", data: { content: "a0" } });
  a.applyRecord({ seq: 3, round: 1, type: "user_message", data: { content: "q1" } });
  a.applyRecord({ seq: 4, round: 1, type: "assistant_text", data: { content: "a1" } });
  const snap = a.snapshot();
  assert.equal(snap.rounds.length, 2);
  assert.deepEqual(snap.rounds[0].nodes.map((n) => n.type), ["user_message", "assistant_text"]);
  assert.deepEqual(snap.rounds[1].nodes.map((n) => n.type), ["user_message", "assistant_text"]);
});

/* ── 2. tool call/result 合体（含跨事件间隔） ── */
test("tool：call 与 result 合体成一条，位置取 call 的 seq", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }));
  a.applyRecord(rec("tool_call", { tool: "read_card", target: "card_001" }, { seq: 10 }));
  // 跨事件间隔：中间插了几条别的记录
  a.applyRecord(rec("judgment", { verdict: "direct" }, { seq: 11 }));
  a.applyRecord(rec("round_start", { round: 2 }, { seq: 12 }));
  a.applyRecord(rec("tool_result", { tool: "read_card", ok: true }, { seq: 13 }));

  const nodes = a.snapshot().rounds[0].nodes;
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
  const nodes = a.snapshot().rounds[0].nodes;
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
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 2, "id-B 的 result 并回 B，A 仍未闭合");
  assert.equal(nodes[1].call.tool_call_id, "id-B");
  assert.equal(nodes[1].result.ok, true);
  assert.equal(nodes[0].closed, false);
});

test("tool：孤立 result 不丢（无配对则独立成终态节点）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("tool_result", { tool: "orphan", ok: true }, { seq: 1 }));
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].kind, "tool");
  assert.equal(nodes[0].result.tool, "orphan");
});

/* ── 3. reasoning 归属 ── */
test("reasoning 带 model → 并进 seats 席卡成员", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("seats_enter", { steps: 2 }, { seq: 1 }));
  a.applyRecord(rec("seat_start", { model: "m1" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "席位思考一" }, { seq: 3, model: "m1" }));
  a.applyRecord(rec("reasoning", { text: "席位思考二" }, { seq: 4, model: "m2" }));
  a.applyRecord(rec("seat_end", { model: "m1", ok: true, text: "观点" }, { seq: 5 }));
  const snap = a.snapshot();
  assert.equal(snap.rounds.length, 1);
  assert.equal(snap.rounds[0].nodes.length, 1, "seats 全程一个组节点");
  const seats = snap.rounds[0].nodes[0];
  assert.equal(seats.entered, true);
  assert.equal(seats.members.m1.thinking, "席位思考一");
  assert.equal(seats.members.m1.end.text, "观点");
  assert.equal(seats.members.m2.thinking, "席位思考二");
});

test("reasoning 不带 model → 并进同组 assistant 的思考区（拼接）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("assistant_text", { content: "答" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "先想…" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "再想…" }, { seq: 3 }));
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 1, "思考不成独立节点");
  assert.equal(nodes[0].reasoning, "先想…再想…", "两段拼进同一条 assistant");
});

test("reasoning 不带 model 且该轮尚无 assistant → pending，assistant 到达时吸收", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "进行中的思考" }, { seq: 2 }));
  a.applyRecord(rec("reasoning", { text: "还在想" }, { seq: 3 }));
  let nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 1, "pending 期不成节点");
  a.applyRecord(rec("assistant_text", { content: "答" }, { seq: 4 }));
  nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 2);
  assert.equal(nodes[1].reasoning, "进行中的思考还在想");
});

test("reasoning 永不丢：该轮无 assistant 时落兜底 sys 节点", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "无人认领的思考" }, { seq: 2 }));
  a.applyRecord(rec("error", { error: "boom" }, { seq: 3 }));
  const nodes = a.snapshot().rounds[0].nodes;
  const orphan = nodes.find((n) => n.data && n.data.orphan);
  assert.ok(orphan, "思考以兜底节点落盘");
  assert.equal(orphan.data.text, "无人认领的思考");
});

/* ── 4. revoke 软删 ── */
test("revoke：from_seq 之后的节点全剔，其后的新 run 不受影响", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "旧问" }, { seq: 1, run_id: "A" }));
  a.applyRecord(rec("judgment", { verdict: "direct" }, { seq: 2, run_id: "A" }));
  a.applyRecord(rec("assistant_text", { content: "旧答" }, { seq: 3, run_id: "A" }));
  a.applyRecord({ seq: 4, type: "revoke", run_id: "A", round: 1, data: { from_seq: 2 } });
  // 与后端 event_log.timeline 同语义：只撤 revoke 之前已累积的 seq>=F 段，
  // seq<from_seq 的留下。
  let snap = a.snapshot();
  assert.equal(snap.rounds.length, 1);
  assert.deepEqual(snap.rounds[0].nodes.map((n) => n.seq), [1], "seq>=2 全剔，seq=1 留");
  // 从该轮 user_message 起撤（＝后端编辑提问的实参）＝整轮没
  a.applyRecord({ seq: 5, type: "revoke", run_id: "A", round: 1, data: { from_seq: 1 } });
  assert.deepEqual(a.snapshot().rounds, [], "from_seq=1 时整轮作废");
  // revoke 之后的新 run（seq 更大）正常进表
  a.applyRecord(rec("user_message", { content: "新问" }, { seq: 6, run_id: "B" }));
  a.applyRecord(rec("assistant_text", { content: "新答" }, { seq: 7, run_id: "B" }));
  snap = a.snapshot();
  assert.equal(snap.rounds.length, 1);
  assert.equal(snap.rounds[0].key, "run:B");
});

test("revoke 自身不成节点（控制位不进渲染面）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord({ seq: 2, type: "revoke", data: { from_seq: 99 } });
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 1);
  assert.equal(nodes.some((n) => n.type === "revoke"), false);
});

/* ── 5. 未知类型 sys 兜底 ── */
test("未知类型落 sys 兜底节点，不丢不炸", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("future_x", { whatever: 1 }, { seq: 1 }));
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 2 }));
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 2);
  assert.equal(nodes[0].type, "future_x");
  assert.equal(nodes[0].kind, "sys");
  assert.equal(nodes[0].data.whatever, 1);
});

test("注册表覆盖任务书全部类型，缺省三类已补（card_message/triage_verdict/ping）", () => {
  for (const t of [
    "user_message", "assistant_text", "msg_ask", "msg_answer", "msg_notify",
    "tool_call", "tool_result", "judgment", "plan_created", "step_start", "step_advanced",
    "seats_enter", "seat_start", "seat_end", "synthesize", "deliver", "proposal",
    "done", "error", "rescue", "card_op", "context", "round_start", "round_end", "run_start",
    "context_preamble", "context_injection", "judgment_protocol", "context_seat_identity",
    "status_update_task",
    "reasoning", "card_message", "triage_verdict", "ping",
  ]) {
    assert.ok(REGISTRY[t], "注册表缺 " + t);
  }
});

/* ── 6. 重复 seq 幂等 ── */
test("重复 seq 二次 applyRecord 不重不炸", () => {
  reset();
  const a = createAssembler();
  const r1 = rec("user_message", { content: "q" }, { seq: 1 });
  a.applyRecord(r1);
  a.applyRecord({ ...r1 });
  a.applyRecord({ ...r1, data: { content: "改了也不认" } });
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 1, "同 seq 只进一次");
  assert.equal(nodes[0].data.content, "q");
});

test("rebuild 幂等：同一 items 重建两次快照全等", () => {
  reset();
  const items = [
    rec("user_message", { content: "q1" }, { seq: 1, run_id: "A" }),
    rec("tool_call", { tool: "T" }, { seq: 2, run_id: "A" }),
    rec("reasoning", { text: "想" }, { seq: 3, run_id: "A" }),
    rec("tool_result", { tool: "T", ok: true }, { seq: 4, run_id: "A" }),
    rec("assistant_text", { content: "a1" }, { seq: 5, run_id: "A" }),
    rec("user_message", { content: "q2" }, { seq: 6, run_id: "B" }),
    rec("assistant_text", { content: "a2" }, { seq: 7, run_id: "B" }),
  ];
  const a = createAssembler();
  const s1 = a.rebuild(items);
  const s2 = a.rebuild(items);
  const strip = (s) => JSON.parse(JSON.stringify(s));
  assert.deepEqual(strip(s1), strip(s2), "两次 rebuild 快照全等");
  assert.equal(s1.rounds.length, 2);
});

test("rebuild 清空：先装过再 rebuild 空数组，快照为空", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.rebuild([]);
  assert.deepEqual(a.snapshot().rounds, []);
});

/* ── 7. 两套 seq 空间隔离（实时 SSE seq 与落盘 seq 不互相吞帧） ── */
test("disk/live 两套 seq 空间互不吞帧", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ seq: 1, ns: "disk", run_id: "A", round: 1, type: "user_message", data: { content: "落盘问" } });
  // 实时帧 seq 也从 1 起（内存 journal 计数器独立）
  a.applyRecord({ seq: 1, ns: "live", run_id: "B", round: 0, type: "run_start", data: {} });
  a.applyRecord({ seq: 2, ns: "live", run_id: "B", round: 0, type: "context", data: {} });
  const snap = a.snapshot();
  assert.equal(snap.rounds.length, 2, "同名 seq 分属两个命名空间，都进表");
  assert.equal(snap.rounds[0].nodes.length, 1);
  assert.equal(snap.rounds[1].nodes.length, 2);
});

test("畸形输入不炸：null / 无 type / data 非对象", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(null);
  a.applyRecord({});
  a.applyRecord({ seq: 1, type: "tool_call" });               // 无 data
  a.applyRecord({ seq: 2, type: "done", data: "裸字符串" });   // data 非对象
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.length, 2);
  assert.deepEqual(nodes[1].data, { value: "裸字符串" }, "非对象 data 装箱不丢载荷");
  a.rebuild(undefined);                                        // 非数组入参：清空且不炸
  assert.deepEqual(a.snapshot().rounds, []);
});

/* ── 8. [对话流照抄 dsh 2026-10-16 · T2] 节点 key / rev / 快照引用稳定 ──
   增量装配的底座：渲染层按 key 复用 DOM、按 rev 判「内容变了没」；
   结构未变时快照必须返回**同一引用**（sameReferences）。 */
test("节点 key：跨合体恒等（渲染层据此复用同一 DOM）", () => {
  reset();
  const a = createAssembler();
  const call = a.applyRecord(rec("tool_call", { tool: "T" }, { seq: 5 }));
  const key = call.key;
  assert.ok(key, "节点必须带 key");
  const merged = a.applyRecord(rec("tool_result", { tool: "T", ok: true }, { seq: 6 }));
  assert.equal(merged, call, "result 合体到同一节点对象");
  assert.equal(merged.key, key, "合体不改 key（位置与身份都留 call 的）");
  assert.ok(merged.rev > call.rev - 1, "合体后 rev 递增（内容变了）");
});

test("节点 rev：内容变则递增；同一节点对象跨多次 apply 恒定", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  const asst = a.applyRecord(rec("assistant_text", { content: "答" }, { seq: 2 }));
  const r0 = asst.rev;
  a.applyRecord(rec("reasoning", { text: "想" }, { seq: 3 }));   // 吸收进该 assistant
  const again = a.snapshot().rounds[0].nodes.find((n) => n.kind === "assistant");
  assert.equal(again, asst, "吸收不改节点对象");
  assert.ok(again.rev > r0, "吸收思考后 rev 递增");
});

test("快照引用稳定：结构未变 → 同一引用；结构变 → 换引用", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  const s1 = a.snapshot();
  assert.equal(a.snapshot(), s1, "无变化时快照同引用（视图层零重排）");
  a.applyRecord(rec("assistant_text", { content: "a" }, { seq: 2 }));
  const s2 = a.snapshot();
  assert.notEqual(s2, s1, "结构变化后换引用");
  assert.equal(a.snapshot(), s2, "变化后再次取仍同引用");
  assert.equal(s2.rounds[0].nodes, a.snapshot().rounds[0].nodes, "节点数组是活引用");
});

/* ── 9. [T1 乐观上屏] 本地轮认领 + pending 原地升级 ── */
test("乐观上屏：本地轮自成一组，带 run_id 的真实记录到达即被认领改名", () => {
  reset();
  const a = createAssembler();
  const local = a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
                                data: { content: "刚发的" } });
  assert.equal(local.pending, true, "本地节点带 pending 标记");
  assert.equal(local.groupKey, "local:u1");
  assert.equal(a.snapshot().rounds.length, 1);
  // 真实过程帧到达（本轮第一条带 run_id 的记录）→ 认领
  const n2 = a.applyRecord(rec("run_start", { pair_id: "R" }, { seq: 9, run_id: "R" }));
  assert.equal(n2.groupKey, "run:R", "本地组改名入正式轮");
  const snap = a.snapshot();
  assert.equal(snap.rounds.length, 1, "认领不新增轮（同一组，只改名）");
  assert.equal(snap.rounds[0].key, "run:R");
  assert.equal(snap.rounds[0].nodes.length, 2);
  assert.equal(snap.rounds[0].nodes[0].kind, "user", "用户节点仍排本轮第一");
});

test("乐观上屏：真实 user_message 到达 → 原地升级，不出双节点/双气泡", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
                  data: { content: "  同样内容  " } });
  a.applyRecord(rec("run_start", { pair_id: "R" }, { seq: 9, run_id: "R" }));
  const upgraded = a.applyRecord(rec("user_message", { content: "同样内容" }, { seq: 10, run_id: "R" }));
  const nodes = a.snapshot().rounds[0].nodes;
  assert.equal(nodes.filter((n) => n.kind === "user").length, 1, "只有一个用户节点");
  assert.equal(upgraded.pending, false, "pending 标记已清");
  assert.equal(upgraded.seq, 10, "补上落盘 seq");
  assert.equal(nodes[0], upgraded, "仍是同一个节点对象（渲染层原地更新，零闪烁）");
});

test("乐观上屏：内容不同则不误并（各自成节点）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
                  data: { content: "本地这句" } });
  a.applyRecord(rec("run_start", { pair_id: "R" }, { seq: 9, run_id: "R" }));
  a.applyRecord(rec("user_message", { content: "落盘那句" }, { seq: 10, run_id: "R" }));
  assert.equal(a.snapshot().rounds[0].nodes.filter((n) => n.kind === "user").length, 2);
});

test("dropLocal：发送失败回退撤下未被认领的本地轮", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "历史" }, { seq: 1, run_id: "A" }));
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
                  data: { content: "失败的那条" } });
  assert.equal(a.snapshot().rounds.length, 2);
  assert.equal(a.dropLocal(), true);
  const snap = a.snapshot();
  assert.equal(snap.rounds.length, 1);
  assert.equal(snap.rounds[0].key, "run:A", "历史轮不受影响");
});

test("本地轮已被认领后 dropLocal 不误伤正式轮", () => {
  reset();
  const a = createAssembler();
  a.applyRecord({ ns: "local", seq: 0, local_key: "u1", type: "user_message",
                  data: { content: "已发出" } });
  a.applyRecord(rec("run_start", { pair_id: "R" }, { seq: 9, run_id: "R" }));
  assert.equal(a.dropLocal(), false, "已无本地轮可撤");
  assert.equal(a.snapshot().rounds[0].key, "run:R");
});

test("兜底思考节点不与尾节点撞 key（渲染层按 key 复用 DOM）", () => {
  reset();
  const a = createAssembler();
  a.applyRecord(rec("user_message", { content: "q" }, { seq: 1 }));
  a.applyRecord(rec("reasoning", { text: "无人认领" }, { seq: 2 }));
  a.applyRecord(rec("error", { error: "boom" }, { seq: 3 }));
  const nodes = a.snapshot().rounds[0].nodes;
  const keys = nodes.map((n) => n.key);
  assert.equal(new Set(keys).size, keys.length, "组内 key 唯一：" + keys.join(","));
  assert.ok(nodes.some((n) => n.data && n.data.orphan), "兜底思考仍在");
});
