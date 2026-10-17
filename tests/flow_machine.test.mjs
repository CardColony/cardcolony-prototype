/* tests/flow_machine.test.mjs —— 输入状态机与提交策略单测（node --test，零依赖）
 * 跑法：node --test tests/flow_machine.test.mjs
 *
 * 对应任务书《对话流照抄 dsh 完全版 v3》§五「输入」组的行为判据，
 * 逐条对上 dsh `input/machine.ts` / `input/submission-policy.ts` 的原文分支。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { InputMachine, PLACEHOLDER, projectClipboard } from "../static/esm/flow/machine.js";
import { ComposerSubmissionPolicy } from "../static/esm/flow/submission-policy.js";

function type(m, text) {
  // 模拟逐字输入：一次一个字符，走 draft-changed（无 editRange ⇒ 机器自己 diff）。
  for (const ch of text) {
    const cur = m.state.draft;
    m.dispatch({ type: "draft-changed", draft: cur + ch });
  }
}

/* ── 1. 相位集与初始态 ── */
test("初始相位是 plain，草稿为空", () => {
  const m = new InputMachine();
  assert.equal(m.state.phase, "plain");
  assert.equal(m.state.draft, "");
  assert.equal(m.state.draftRev, 0);
});

/* ── 2. 空草稿 Enter 无效果（machine.ts:469） ── */
test("空草稿 / 纯空白 Enter 不产生任何效果", () => {
  const m = new InputMachine();
  assert.deepEqual(m.dispatch({ type: "enter", mode: "queue" }), []);
  m.dispatch({ type: "draft-changed", draft: "   \n " });
  assert.deepEqual(m.dispatch({ type: "enter", mode: "queue" }), []);
});

/* ── 3. 普通 Enter → default-sink（带 mode），相位不变 ── */
test("普通 Enter 走 default-sink，草稿与相位都不动（由 shell 决定排队/发送）", () => {
  const m = new InputMachine();
  type(m, "你好");
  const fx = m.dispatch({ type: "enter", mode: "queue" });
  assert.equal(fx.length, 1);
  assert.equal(fx[0].type, "default-sink");
  assert.equal(fx[0].draft, "你好");
  assert.equal(fx[0].mode, "queue");
  assert.equal(m.state.phase, "plain", "普通发送不加锁相位（原文 :476）");
  assert.equal(m.state.draft, "你好", "default-sink 不清草稿");
});

/* ── 4. 任何相位都可编辑（onDraftChanged 无相位检查） ── */
test("adjudicating 相位仍可编辑草稿（machine.ts:240 无相位检查）", () => {
  const m = new InputMachine();
  type(m, "/goal x");
  const fx = m.dispatch({ type: "enter", mode: "queue" });
  assert.equal(fx[0].type, "adjudicate");
  assert.equal(m.state.phase, "adjudicating");
  m.dispatch({ type: "draft-changed", draft: "/goal xy" });
  assert.equal(m.state.draft, "/goal xy");
});

/* ── 5. 忙时（submitting）Enter 被丢弃：不吞草稿、不报错 ── */
test("submitting 相位丢弃本次 Enter，草稿原样保留（machine.ts:461）", () => {
  const m = new InputMachine();
  type(m, "/goal x");
  m.dispatch({ type: "enter", mode: "queue" });
  const attempt = m.inflight.attempt;
  m.dispatch({ type: "adjudicated", attempt, outcome: { claim: { token: "/goal " } } });
  assert.equal(m.state.phase, "submitting");
  const before = m.state.draft;
  assert.deepEqual(m.dispatch({ type: "enter", mode: "queue" }), [], "忙时 Enter 必须被丢弃");
  assert.equal(m.state.draft, before, "不得吞草稿");
});

/* ── 6. 发送成功清撤销栈：undo 不能把已发内容复活 ── */
test("send-committed 清空草稿并清撤销栈（machine.ts:518-519）", () => {
  const m = new InputMachine();
  type(m, "要发的话");
  assert.equal(m.state.draft, "要发的话");
  m.dispatch({ type: "send-committed" });
  assert.equal(m.state.draft, "");
  const rev = m.state.draftRev;
  m.dispatch({ type: "undo" });
  assert.equal(m.state.draft, "", "撤销不得复活已发送内容");
  assert.equal(m.state.draftRev, rev, "撤销无可用单元 ⇒ 修订号不变");
});

/* ── 7. 撤销 / 重做 ── */
test("撤销回到上一事务，重做再前进（逐字输入合并为一个事务）", () => {
  const m = new InputMachine({ now: () => 0 });
  type(m, "abc");
  assert.equal(m.state.draft, "abc");
  m.dispatch({ type: "undo" });
  assert.equal(m.state.draft, "", "连续单字输入合并成一次撤销");
  m.dispatch({ type: "redo" });
  assert.equal(m.state.draft, "abc");
});

/* ── 8. 提交失败：草稿保住（machine.ts:527-539） ── */
test("命令提交失败：草稿保住，只出一条 error notice", () => {
  const m = new InputMachine();
  type(m, "/goal 做点事");
  m.dispatch({ type: "enter", mode: "queue" });
  const attempt = m.inflight.attempt;
  m.dispatch({ type: "adjudicated", attempt, outcome: { claim: { token: "/goal " } } });
  const fx = m.dispatch({ type: "submit-settled", attempt, ok: false, message: "炸了" });
  assert.equal(fx.length, 1);
  assert.equal(fx[0].type, "notice");
  assert.equal(fx[0].level, "error");
  assert.equal(m.state.draft, "/goal 做点事", "失败必须保住草稿（不丢字）");
});

/* ── 9. 粘贴事务 + 剪贴板投影 ── */
test("粘贴是一次事务；占位符在剪贴板投影里展开为原文", () => {
  const m = new InputMachine({ now: () => 0 });
  m.dispatch({ type: "paste-begin", text: "hello", selection: { start: 0, end: 0 } });
  assert.equal(m.state.draft, "hello");
  assert.equal(projectClipboard(m.state), "hello");
  m.dispatch({ type: "undo" });
  assert.equal(m.state.draft, "", "一次撤销回到粘贴前");
});

/* ── 10. 提交策略 resolve 的四条分支（submission-policy.ts:48-57） ── */
test("resolve：不忙/不支持 steer ⇒ queue；忙 + 普通 Enter ⇒ 偏好；加速键 ⇒ 反向", () => {
  const p = new ComposerSubmissionPolicy();
  assert.equal(p.resolve(false, "enter", true), "queue");
  assert.equal(p.resolve(true, "enter", false), "queue");
  assert.equal(p.resolve(true, "enter", true), "queue", "默认偏好＝queue");
  assert.equal(p.resolve(true, "accelerated", true), "steer", "加速键走另一条");
  p.setBusyEnter("steer");
  assert.equal(p.resolve(true, "enter", true), "steer");
  assert.equal(p.resolve(true, "accelerated", true), "queue");
});

/* ── 11. release：在飞 attempt 被 abort，相位归 plain ── */
test("release 中止在飞 attempt 并把相位收回 plain", () => {
  const m = new InputMachine();
  type(m, "/goal x");
  m.dispatch({ type: "enter", mode: "queue" });
  const signal = m.inflight.attempt.signal;
  assert.equal(signal.aborted, false);
  m.dispatch({ type: "release" });
  assert.equal(signal.aborted, true);
  assert.equal(m.state.phase, "plain");
});

/* ── 12. 占位符常量与 dsh 一致 ── */
test("PLACEHOLDER 是 U+FFFC（对象替换字符）", () => {
  assert.equal(PLACEHOLDER, "\uFFFC");
  assert.equal(PLACEHOLDER.length, 1);
});
