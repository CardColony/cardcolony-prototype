/* tests/chat_resync_no_fullpull.test.mjs —— 「结算回流不许拉全量 timeline」网（[P24]）
 *
 * 跑法：node --test tests/chat_resync_no_fullpull.test.mjs
 *
 * ## 为什么有这张网
 *
 * 生产访问日志抓到（2026-10-26 00:45）：
 *   GET /api/works/work_027/cards/card_021/timeline   **20765ms**
 *   GET /api/works/work_027/cards/card_021/timeline   **32157ms**
 *   GET /api/works/work_027/cards/card_001/timeline?tail=400  13761ms（被拖慢的）
 * 同一时刻 nav/version 2740ms、rule 2737ms、unread 2965ms ⇒ 整个服务都在排队。
 *
 * 病根：这条**不带 tail** 的请求来自 `stream.js` 的 `resync()`（发消息后的结算回流）
 * —— 它**无条件先拉全量** timeline。大卡的全量是「每卡一份事件文件」
 * （实测 card_021：8277 条 / **109.5MB**）⇒ 生产 IO 忙时 20~32 秒
 * ⇒ 客户端 15 秒超时 ⇒ 弹「请求超时」⇒ 拉取被 abort ⇒ **内存态建不起来**
 * ⇒ 切回来还要重拉 ⇒ 用户观感「内存态被释放了」⇒ 整页卡住只能刷新。
 *
 * ## 网里钉的不变量
 *
 * | 编号 | 不变量 |
 * |---|---|
 * | R1 | `resync` 里的 timeline 拉取**必须带 tail 窗口**（不许全量） |
 * | R2 | `resync` 必须**先判 liveComplete 再决定拉不拉**（实时表已完整 ⇒ 零请求） |
 * | R3 | 实时表已完整时 `ok` 仍须为 true（不发请求不等于失败） |
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("../static/stream.js", import.meta.url), "utf8");

/** 取某个函数的函数体源码（大括号配平）。 */
function fnBody(src, name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) return null;
  const start = src.indexOf("{", i);
  let depth = 0;
  for (let k = start; k < src.length; k++) {
    if (src[k] === "{") depth++;
    else if (src[k] === "}") {
      depth--;
      if (depth === 0) return src.slice(start + 1, k);
    }
  }
  return null;
}

const RESYNC = fnBody(SRC, "resync");

test("R1：resync 的 timeline 拉取必须带 tail 窗口（不许拉全量）", () => {
  assert.ok(RESYNC, "stream.js 里应有 resync 函数");
  const pulls = RESYNC.match(/fetchTimeline\([^)]*\)/g) || [];
  assert.ok(pulls.length >= 1, "resync 里应有 timeline 拉取");
  for (const p of pulls) {
    assert.ok(/tail\s*:/.test(p),
      `resync 的 timeline 拉取必须带 tail 窗口（不许全量——大卡全量是 109.5MB，`
      + `生产实测 20~32 秒 ⇒ 15 秒超时 ⇒「请求超时」＋内存态建不起来）：${p}`);
  }
  assert.ok(!/fetchTimeline\(st,\s*wid,\s*cid\)\s*;/.test(RESYNC),
    "resync 里不许出现「不带任何窗口参数」的 fetchTimeline(st, wid, cid)");
});

test("R2：resync 必须先判 liveComplete 再决定拉不拉（实时表已完整 ⇒ 零请求）", () => {
  assert.ok(RESYNC, "stream.js 里应有 resync 函数");
  const judgeAt = RESYNC.indexOf("liveComplete");
  const pullAt = RESYNC.indexOf("fetchTimeline(");
  assert.ok(judgeAt >= 0, "resync 里应先判 liveComplete（实时表已完整就不需要拉落盘）");
  assert.ok(pullAt >= 0, "resync 里应有 timeline 拉取");
  assert.ok(judgeAt < pullAt,
    "liveComplete 的判断必须**在**拉取之前——原来是无条件先拉全量"
    + "（大卡 20~32 秒 ⇒ 客户端超时 ⇒ 内存态建不起来 ⇒ 看起来像被释放）");
  // 「实时已完整 ⇒ 不拉」这条分支必须真的存在（拉取在 if (!liveComplete) 里）
  assert.ok(/if\s*\(\s*!liveComplete\s*\)/.test(RESYNC),
    "resync 里应有 if (!liveComplete) 的守卫（实时表已完整 ⇒ 零请求）");
});

test("R3：实时表已完整时 ok 仍为 true（不发请求 ≠ 失败）", () => {
  assert.ok(RESYNC, "stream.js 里应有 resync 函数");
  assert.ok(/ok\s*:\s*items\s*!==\s*null\s*\|\|\s*liveComplete/.test(RESYNC),
    "resync 的 ok 必须是「拉到数据 **或** 实时表已完整」——否则不发请求会被判成失败");
});
