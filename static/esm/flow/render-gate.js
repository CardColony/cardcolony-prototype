/* esm/flow/render-gate.js —— 同一节点的**重渲染最小间隔**闸（直播路径的性能闸）
 *
 * ## 为什么要有这件
 *
 * 直播时模型逐帧吐字（每帧一个 SSE content_chunk）。前端每帧都会让**那一行**
 * 重渲染，而 assistant 行里含**累积全文**，正文还要**重跑一遍 Markdown 解析**
 * ⇒ 一次长回复 N 帧 ⇒ 写入量与解析量 ∝ 1+2+…+N = **O(N²)** ⇒ 主线程被吃满
 * （思考块已由 think-stream.js 改成增量追加；**正文是 Markdown**，不能追加，
 * 只能节流）。
 *
 * ## 口径
 *
 * · 同一节点两次重渲染**至少隔 `minMs`**（默认 100ms ≈ 10fps）——
 *   文本本来是人读的，10fps 足够顺，代价降到 1/6。
 * · **最后一次一定落地**：被节流掉的帧不丢，而是**留下待刷**（由调用方稍后重试）。
 * · 终稿/结构变化等「必须马上出」的场景走 `force()`（立即放行）。
 * · 节点从页面上消失 ⇒ `forget()`（不留时间戳）。
 *
 * 这是**载体替换**：节流逻辑从 stream.js 的渲染闭包里搬出来，好单测。
 */

/**
 * @param {object} [opts]
 * @param {number} [opts.minMs] 最小间隔（毫秒）
 * @param {() => number} [opts.now] 取当前时刻（测试可注入）
 */
export function createRenderGate({ minMs = 100, now = () => Date.now() } = {}) {
  const last = new Map();

  return {
    /** 该不该**现在**渲染这个 key（不该 ⇒ 调用方保留待刷、稍后重试）。 */
    allow(key) {
      const t = now();
      if (!last.has(key)) {
        // **第一次必放行**（首帧不能被挡——否则这一节点的首屏就没了）
        last.set(key, t);
        return true;
      }
      const l = last.get(key);
      if (t - l < minMs) return false;
      last.set(key, t);
      return true;
    },

    /** 立即放行（终稿、结构变化等必须马上出的场景）。 */
    force(key) {
      last.set(key, now() - minMs);
    },

    /** 节点不在页面上了 ⇒ 别留时间戳。 */
    forget(key) { last.delete(key); },

    /** 清空（换卡/重建时）。 */
    reset() { last.clear(); },
  };
}
