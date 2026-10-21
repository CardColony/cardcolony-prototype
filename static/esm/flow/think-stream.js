/* esm/flow/think-stream.js —— 思考块的**增量渲染**（直播路径的性能闸）
 *
 * ## 为什么要有这件
 *
 * 直播时模型的思考是**逐帧**吐的（每帧一个 SSE reasoning_chunk）。前端每一帧都会
 * 让那一行重渲染，而**行里含累积全文** ⇒ 一次思考 N 帧 ⇒ 写入量
 * ∝ 1+2+…+N = **O(N²)**。长思考（实测一次思考上万帧）会把主线程打满——
 * 这就是「对话页卡死」的另一半原因（另一半在落盘侧，已由 event_stream 的
 * 「按轮合并落一条」解决）。
 *
 * ## 口径
 *
 * · 思考文本在直播中**只增不改** ⇒ 新文本必是旧文本的**前缀扩展**；
 *   此时**只追加增量**（O(增量)），不重写整块。
 * · 文本被**整体替换**（换轮、换席、回放重建）⇒ 整块重写（此时一次，重写合理）。
 * · 增量更新**保留用户的折叠态**（点开的思考不该在下一帧被弹回去）。
 * · 元素按**节点键**缓存并复用 ⇒ DOM 节点身份稳定（不重建 ⇒ 也不丢事件绑定）。
 *
 * 这是**载体替换**：原 stream.js 里的 makeThink 逐字搬进来，只多了增量路径。
 */

/** 摘要：运行中取**最新一行**（跟着思考走），完成取**第一行**（一眼知主题）。 */
export function thinkSummary(text, running) {
  const lines = String(text == null ? "" : text).split("\n").map((s) => s.trim()).filter(Boolean);
  if (!lines.length) return "";
  return running ? lines[lines.length - 1] : lines[0];
}

const HEAD_HTML =
  '<div class="think-head">' +
    '<i class="think-ico"></i>' +
    '<span class="think-lab">Think</span>' +
    '<span class="think-sep">·</span>' +
    '<span class="think-sum"></span>' +
    '<span class="think-time"></span>' +
  "</div>" +
  '<div class="think-body"></div>';

/**
 * 通用**增量文本**器：给「承载一段只增不改的文本」的元素用。
 *
 * 用途：直播时**三席并排卡**里每席的思考（``.tk``）也是逐帧增长的整块文本，
 * 每次重写同样是 O(N²)。本器按 (键, 元素) 记住上次写到哪 ⇒ 新文本是前缀扩展时
 * **只追加增量**；元素换了（重建）或文本被替换 ⇒ 整块重写。
 *
 * @param {object} [opts]
 * @param {(key:string)=>boolean} [opts.isAlive] 元素是否还挂在页面上（默认都算活着）
 */
export function createTextUpdater({ isAlive } = {}) {
  const cache = new Map();   // key → {text, el}

  return {
    /**
     * 把 text 写进 el（增量优先）。
     * @param {string} key  稳定键（同一段文本每次用同一个键）
     * @param {HTMLElement} el 承载文本的元素
     * @param {string} text 新的完整文本
     */
    set(key, el, text) {
      const t = String(text == null ? "" : text);
      const prev = cache.get(key);
      if (prev && prev.el === el && t.indexOf(prev.text) === 0) {
        const delta = t.slice(prev.text.length);
        if (delta) el.appendChild(document.createTextNode(delta));
        cache.set(key, { text: t, el });
        return;
      }
      el.textContent = t;
      cache.set(key, { text: t, el });
    },

    /** 清掉不再挂着的（元素被换掉/节点消失）。 */
    prune(aliveKeys) {
      const alive = aliveKeys instanceof Set ? aliveKeys : new Set(aliveKeys || []);
      for (const k of Array.from(cache.keys())) {
        const e = cache.get(k).el;
        const ok = alive.size ? alive.has(k) : (isAlive ? isAlive(k) : true);
        if (!ok) cache.delete(k);
      }
    },

    keys() { return Array.from(cache.keys()); },
  };
}

/**
 * 造思考渲染器。
 * @param {object} [opts]
 * @param {(text:string, running:boolean)=>string} [opts.summaryOf] 摘要算法（默认 thinkSummary）
 */
export function createThinkRenderer({ summaryOf } = {}) {
  const summary = summaryOf || thinkSummary;
  /** 节点键 → {text, el} */
  const cache = new Map();

  function build(text, done) {
    const el = document.createElement("div");
    el.className = "think" + (done ? " done" : " thinking") + " collapsed";
    el.innerHTML = HEAD_HTML;
    el.querySelector(".think-sum").textContent = summary(text, !done);
    el.querySelector(".think-body").textContent = text || "";
    el.querySelector(".think-head").onclick = () => {
      el.classList.toggle("collapsed");
    };
    return el;
  }

  return {
    /**
     * 取（或建）思考行。
     * @param {string} key   节点键（缓存与复用都靠它）
     * @param {string} text  累积全文
     * @param {object} [opts]
     * @param {boolean} [opts.done] 是否已定稿
     */
    row(key, text, { done = true } = {}) {
      const t = String(text == null ? "" : text);
      const prev = cache.get(key);
      if (prev && prev.el && t.indexOf(prev.text) === 0) {
        // 增量：只追加新增部分（不重写整块 —— 那正是 O(N²) 的来源）
        const delta = t.slice(prev.text.length);
        if (delta) {
          const body = prev.el.querySelector(".think-body");
          body.appendChild(document.createTextNode(delta));
        }
        const sum = prev.el.querySelector(".think-sum");
        if (sum) sum.textContent = summary(t, !done);
        // 完成态变化要跟上，但**保留用户的折叠态**
        const collapsed = prev.el.classList.contains("collapsed") ? " collapsed" : "";
        prev.el.className = "think" + (done ? " done" : " thinking") + collapsed;
        cache.set(key, { text: t, el: prev.el });
        return prev.el;
      }
      // 整块重写（文本被替换／首次渲染）
      const el = build(t, done);
      cache.set(key, { text: t, el });
      return el;
    },

    /** 清掉不在页面上的节点（防缓存持有旧元素）。 */
    prune(aliveKeys) {
      const alive = aliveKeys instanceof Set ? aliveKeys : new Set(aliveKeys || []);
      for (const k of Array.from(cache.keys())) {
        if (!alive.has(k)) cache.delete(k);
      }
    },

    /** 当前缓存的节点键（自检用）。 */
    keys() { return Array.from(cache.keys()); },
  };
}
