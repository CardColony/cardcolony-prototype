/* tests/detail_batch.test.mjs —— 详情回取 64 上限分批（任务二 §四）
 *
 * 服务端 /timeline/event 单次上限 64 个 seq：fetchTimelineBatched 按 60/批
 * 并发合流，部分批失败保留已确认内容并把失败 seq 记入 failed（留 brief
 * 标记供单独重试）；全灭按原语义拒绝。
 *
 * 跑法：node --test tests/detail_batch.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import { fetchTimelineBatched } from "../static/stream.js";

/** 假详情读口：按 chunk 回 items/revoked；failSet 里的 seq 批整批抛错。 */
function fakeGet(failSet = new Set(), extra = {}) {
  const calls = [];
  const fn = (chunk) => {
    calls.push(chunk.slice());
    if (chunk.some((s) => failSet.has(s))) return Promise.reject(new Error("boom"));
    return Promise.resolve({
      items: chunk.map((s) => ({ seq: s, data: { text: "全文" + s } })),
      revoked: (extra.revoked || []).filter((s) => chunk.includes(s)),
    });
  };
  fn.calls = calls;
  return fn;
}

test("≤64 段：单请求直发（不分批）", async () => {
  const g = fakeGet();
  const r = await fetchTimelineBatched(g, [1, 2, 3]);
  assert.equal(g.calls.length, 1);
  assert.deepEqual(g.calls[0], [1, 2, 3]);
  assert.equal(r.items.length, 3);
});

test("65 段：拆 60+5 两批并发，items 全量合流", async () => {
  const g = fakeGet();
  const seqs = Array.from({ length: 65 }, (_, i) => i + 1);
  const r = await fetchTimelineBatched(g, seqs);
  assert.equal(g.calls.length, 2, "65 段应拆两批");
  assert.equal(g.calls[0].length, 60);
  assert.equal(g.calls[1].length, 5);
  assert.equal(r.items.length, 65);
  assert.deepEqual(r.failed, []);
});

test("65 段含撤销：revoked 跨批合流", async () => {
  const g = fakeGet(new Set(), { revoked: [3, 62] });
  const seqs = Array.from({ length: 65 }, (_, i) => i + 1);
  const r = await fetchTimelineBatched(g, seqs);
  assert.deepEqual(r.revoked.sort((a, b) => a - b), [3, 62]);
});

test("部分批失败：已确认内容保留、失败 seq 记 failed", async () => {
  // seq 61..65 所在批失败（61-65 在第二批）
  const g = fakeGet(new Set([63]));
  const seqs = Array.from({ length: 65 }, (_, i) => i + 1);
  const r = await fetchTimelineBatched(g, seqs);
  assert.equal(r.items.length, 60, "第一批 60 条保留");
  assert.deepEqual(r.failed, [61, 62, 63, 64, 65], "失败批的 seq 全部记账");
});

test("全灭：按原语义拒绝（可整体重试）", async () => {
  const g = fakeGet(new Set([1, 65]));
  const seqs = Array.from({ length: 65 }, (_, i) => i + 1);
  await assert.rejects(() => fetchTimelineBatched(g, seqs), /all batches failed/);
});

test("去重：重复 seq 不重复请求", async () => {
  const g = fakeGet();
  const r = await fetchTimelineBatched(g, [5, 5, 7, 7, 7]);
  assert.equal(g.calls.length, 1);
  assert.deepEqual(g.calls[0], [5, 7]);
  assert.equal(r.items.length, 2);
});
