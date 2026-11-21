/* esm/flow/chat-view.js —— 对话视图骨架（dsh `chat/ChatView.tsx` 的移植件）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/chat/ChatView.tsx
 *       ＋ ChatView.module.css（样式原值见 style.css 的 .flow-view-* 段）
 *
 * 移植口径（逐条对上原文行号）：
 *   · 滚动跟随（:264-299）：**离底 ≤24px 才跟**（FOLLOW_THRESHOLD=24）；
 *     只有「读者真实滚动」才改变跟随权——用 observed-top 台账判定
 *     （每次程序写 scrollTop 都同步记一笔，所以程序滚底永不夺权）。
 *   · 回底（:408-423）：非跟随态才出圆形回底钮，点击回到跟随态。
 *   · 锚点保位（:234-249 / :349-363）：上翻加载前记 (key, top)，前插落地后
 *     把同一行放回同一位置。
 *   · 运行条（:107-140）：整轮一条 "Deep diving..."，**15 秒后才显时钟**；
 *     锚点取本轮 turn/start 时间（没有则取挂载时刻）。
 *   · 节点表（:382-397）：扁平、按序，一切可见物＝一个节点座位。
 *
 * 载体替换（非机制改动）：React 的 useLayoutEffect/useRef 换成显式 render()
 * 与 renderRows() 的调用序；ChatNodeSeat 的「每行只订阅自己的 key」换成
 * 「renderRow(key) 注入 + refresh(key) 只重建那一行」。
 */

const FOLLOW_THRESHOLD = 24;

/* [性能批次二 · 探针] 首屏分段账：mount＝座位落位（ensureSeat+换元素+fragment
   插回），scroll＝滚动测量/校正（toBottom/恢复/滚动处理里的布局读写）。
   md 段在 stream.js 的渲染器包装上记。performance.now() 微秒级，埋点可忽略。 */
const _SEG = (typeof window !== "undefined")
  ? (window.__PERF_SEG = window.__PERF_SEG || { md: 0, mount: 0, scroll: 0 })
  : { md: 0, mount: 0, scroll: 0 };

/** 时长文案（dsh locales.ts:130-131 中文原值）。 */
function formatRunDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0
    ? minutes + "分" + String(seconds).padStart(2, "0") + "秒"
    : seconds + "秒";
}

function el(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

/**
 * 建对话视图。
 * @param {object} opts
 * @param {HTMLElement} opts.mount        挂载点（#chatArea）
 * @param {(key:string)=>HTMLElement[]} opts.renderRow  按节点键出元素（stream.js 注入）
 * @param {()=>void} [opts.onLoadOlder]   上翻加载（分页）
 * @param {()=>number|null} [opts.runningStart] 本轮开始时刻（ms）
 * @param {()=>boolean} [opts.isRunning]
 * @param {()=>boolean} [opts.hasMore]
 * @param {()=>boolean} [opts.isLoadingOlder]
 */
export function createChatView({ mount, renderRow, onLoadOlder, runningStart, isRunning, hasMore, isLoadingOlder }) {
  const root = el("div", "flow-view-root");
  const scroll = el("div", "flow-view-scroll");
  scroll.setAttribute("data-conversation-scroll", "");
  const column = el("div", "flow-view-column");
  column.setAttribute("data-chat-flow", "");
  const olderWrap = el("div", "flow-view-older");
  const olderBtn = el("button", "flow-view-older-btn");
  olderBtn.type = "button";
  olderBtn.textContent = "加载更早";
  olderWrap.appendChild(olderBtn);
  const statusEl = el("div", "flow-turn-status");
  statusEl.setAttribute("role", "status");
  statusEl.setAttribute("aria-live", "polite");
  const statusLabel = el("span", "flow-turn-status-label");
  statusLabel.textContent = "Deep diving...";
  statusEl.appendChild(statusLabel);
  const clockEl = el("span", "flow-turn-status-clock");
  clockEl.setAttribute("aria-hidden", "true");
  statusEl.appendChild(clockEl);
  const toBottomSlot = el("div", "flow-view-to-bottom-slot");
  const toBottomBtn = el("button", "flow-view-to-bottom");
  toBottomBtn.type = "button";
  toBottomBtn.setAttribute("aria-label", "回到底部");
  toBottomBtn.title = "回到底部";
  toBottomBtn.innerHTML = '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 5.5L7 9l3.5-3.5"/></svg><span class="flow-view-to-bottom-label"></span>';
  toBottomSlot.appendChild(toBottomBtn);

  /* [对话界面商业化 2026-10-17] 空态与首屏骨架：
     —— 空态：该卡一条记录都没有时给引导（标题 + 一句说明 + 可点建议），
        而不是一整块黑。建议点击＝把文本填进输入框（不代发，用户仍可改）。
     —— 骨架：首次拉 timeline 期间显示 3 行 shimmer，避免「白屏 → 突然出内容」。 */
  const emptyEl = el("div", "chat-empty");
  emptyEl.style.display = "none";
  const skeletonEl = el("div", "chat-skeleton");
  skeletonEl.innerHTML =
    '<div class="sk-bubble"></div>' +
    '<div class="sk-line w85"></div><div class="sk-line w70"></div><div class="sk-line w40"></div>';

  column.appendChild(olderWrap);
  column.appendChild(emptyEl);
  column.appendChild(skeletonEl);
  /* [有界挂载 · 性能批次二·任务二] 上下占位条：窗口外的节点不挂 DOM，
     其高度由「实测行高 + 估计行高」折算进占位条——滚动几何不变，
     DOM 峰值受挂载上限约束。 */
  const spacerT = el("div", "flow-view-spacer");
  const spacerB = el("div", "flow-view-spacer");
  column.appendChild(spacerT);
  column.appendChild(spacerB);
  /* [5715 · 网抓修] 运行条**先在 DOM 里**（不等到首次 render 才挂上去）：
     原来它只在 render() 末尾被 appendChild ⇒ 若 setRunning(true) 早于首次
     渲染（先发消息、后到时间线），运行条压根不在 DOM 里 ⇒ 「Deep diving...」
     静默消失，用户以为没跑起来。
     初态是隐藏（paintStatus 在下面统一处理）；render() 里那句 appendChild
     仍保留——它把运行条挪到末位（节点行之后），行为不变。 */
  column.appendChild(statusEl);
  scroll.appendChild(column);
  scroll.appendChild(toBottomSlot);
  root.appendChild(scroll);
  mount.appendChild(root);

  /** 节点键 → {el, rev}（渲染层唯一的复用表）。 */
  const seats = new Map();
  let order = [];
  let atBottom = true;
  let observedTop = 0;          // 程序写过 scrollTop 的台账
  let opened = false;
  let lastKey = null;
  let followSig = null;
  let anchor = null;            // {key, top}
  let firstKey = null;
  let statusTimer = null;
  let statusStart = null;
  let hasNew = false;      // 读者上翻期间有新节点到达
  let loading = true;           // 首屏骨架：首次 render 落地即收
  let stopping = false;         // 停止指令已发出、回合尚未收口
                                  // （[5715] 上移到状态区：构造期就要调
                                  //   paintStatus() 藏运行条，而 paintStatus 读它
                                  //   ——留在运行条段会撞 TDZ 报错）

  /* ── 有界挂载（2026-11-22 · 性能批次二·任务二）──────────────────
     挂载窗口＝order 的 [mntLo, mntHi) 子区间；窗口外节点不挂 DOM、
     不持监听（卸载时逐行释放），其滚动空间由 spacerT/spacerB 折算。
     行高模型：实测值进 hmap（行级 RO 持续校订），未测行按 EST_H 估；
     换挡时以「视口顶相交行」为锚，校正回同一行同一偏移。
     例外（不卸载）：① 无布局环境（jsdom/隐藏板，clientHeight=0）全挂——
       既兼容既有 DOM 单测，也避免游离画板被误裁；
       ② 焦点所在行（document.activeElement 在行内）——不打断输入/选择。 */
  const EST_H = 72;             // 未测行高估计（px）
  const OVERSCAN = 10;          // 视口外上下各多挂的行数
  const INIT_MOUNT = 30;        // 首开挂载行数（视口 + 余量）
  /* 对照旗标：``__FLOW_BOARD__.boundedMount === false`` ⇒ 全量挂载
     （性能批次二·任务二的 A/B 对照臂：同一源码、行为等价于改前全挂）。 */
  const BOUNDED = !(typeof window !== "undefined" && window.__FLOW_BOARD__
    && "boundedMount" in window.__FLOW_BOARD__
    && !window.__FLOW_BOARD__.boundedMount);
  let mntLo = 0, mntHi = 0;     // 挂载区间（order 下标，右开；0/0＝未开）
  const hmap = new Map();       // key → 实测行高
  let cum = null;               // 前缀和 cum[i]＝第 i 行顶（估值）
  let cumDirty = true;
  /** 卸载回调（宿主注入：清该键的渲染缓存/监听）。 */
  let onUnmount = null;

  function rowH(i) {
    const h = hmap.get(order[i]);
    return (h > 0) ? h : EST_H;
  }

  function ensureCum() {
    if (!cumDirty && cum && cum.length === order.length + 1) return;
    const c = new Array(order.length + 1);
    c[0] = 0;
    for (let i = 0; i < order.length; i++) c[i + 1] = c[i] + rowH(i);
    cum = c;
    cumDirty = false;
  }

  /** 视口内首个/末个可见行下标（按估值前缀和二分）。 */
  function _bisect(target) {
    ensureCum();
    let lo = 0, hi = order.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (cum[mid + 1] <= target) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  /** 目标挂载区间：视口覆盖行 ±OVERSCAN；无布局环境全挂。 */
  function desiredRange() {
    const len = order.length;
    if (!len || !BOUNDED) return [0, len];
    const ch = scroll.clientHeight;
    if (!ch || !scroll.scrollHeight) return [0, len];
    ensureCum();
    const vTop = scroll.scrollTop, vBot = vTop + ch;
    const visLo = _bisect(vTop);
    let visHi = visLo;
    while (visHi < len && cum[visHi] < vBot) visHi++;
    const lo = Math.max(0, visLo - OVERSCAN);
    const hi = Math.min(len, visHi + OVERSCAN);
    return [lo, Math.max(hi, lo + 1)];
  }

  /** 占位条刷新到当前高度模型。 */
  function paintSpacers() {
    ensureCum();
    spacerT.style.height = cum[mntLo] + "px";
    spacerB.style.height = Math.max(0, cum[order.length] - cum[mntHi]) + "px";
  }

  /** 视口顶相交的已挂行 → {key, off}（换挡/展开校正的阅读锚点）。 */
  function anchorRow() {
    const vTop = scroll.getBoundingClientRect().top;
    for (let i = mntLo; i < mntHi && i < order.length; i++) {
      const row = rowOf(order[i]);
      if (!row) continue;
      const r = row.getBoundingClientRect();
      if (r.bottom > vTop) return { key: order[i], off: r.top - vTop };
    }
    return null;
  }

  /** 卸载一行：移出 DOM、断行级 RO、放掉座位与宿主侧缓存。 */
  function unmountRow(key) {
    const seat = seats.get(key);
    if (!seat) return;
    const rr = _getRoRows();
    if (rr && rr.unobserve) rr.unobserve(seat.el);
    if (seat.el.parentNode) seat.el.parentNode.removeChild(seat.el);
    seats.delete(key);
    if (onUnmount) { try { onUnmount(key); } catch (e) { /* 零阻断 */ } }
  }

  /** 把挂载区间切成 [nLo, nHi)：卸载出窗行、挂载入窗行、校正锚点与占位。 */
  function _applyRange(nLo, nHi, revOf) {
    nLo = Math.max(0, Math.min(nLo, order.length));
    nHi = Math.max(nLo, Math.min(nHi, order.length));
    if (nLo === mntLo && nHi === mntHi) return;
    const inRange = new Set();
    for (let i = nLo; i < nHi; i++) inRange.add(order[i]);
    for (let i = mntLo; i < mntHi && i < order.length; i++) {
      const k = order[i];
      if (inRange.has(k)) continue;
      /* 焦点行不卸（选择/输入不被打断）——它留在挂载集里，区间账上记例外。 */
      const seat = seats.get(k);
      if (seat && seat.el.contains(document.activeElement)) { inRange.add(k); continue; }
      unmountRow(k);
    }
    for (const k of seats.keys()) {
      if (!inRange.has(k)) unmountRow(k);   // 兜底：区间外漏网的座位
    }
    for (let i = nLo; i < nHi; i++) {
      const key = order[i];
      const seat = ensureSeat(key);
      const rev = revOf ? revOf(key) : null;
      if (seat.rev !== null && rev !== null && seat.rev !== rev) {
        const fresh = renderRow(key);
        const neo = fresh.length === 1 ? fresh[0] : wrap(fresh);
        neo.dataset.chatAnchorKey = key;
        if (seat.el.parentNode) seat.el.parentNode.replaceChild(neo, seat.el);
        seat.el = neo;
        const rr = _getRoRows();
        if (rr) rr.observe(neo);
      }
      seat.rev = rev;
      const rr2 = _getRoRows();
      if (rr2) rr2.observe(seat.el);
    }
    mntLo = nLo; mntHi = nHi;
    /* 落位：窗口行按序排在 spacerT 之后、spacerB 之前（fragment 一次插回）。 */
    const els = [];
    for (let i = mntLo; i < mntHi; i++) {
      const seat = seats.get(order[i]);
      if (seat) els.push(seat.el);
    }
    const cur = [];
    for (const ch of column.children) {
      if (ch === olderWrap || ch === emptyEl || ch === skeletonEl
          || ch === statusEl || ch === spacerT || ch === spacerB) continue;
      cur.push(ch);
    }
    let same = cur.length === els.length;
    if (same) for (let i = 0; i < els.length; i++) if (cur[i] !== els[i]) { same = false; break; }
    if (!same) {
      const frag = document.createDocumentFragment();
      for (const e of els) frag.appendChild(e);
      column.insertBefore(frag, spacerB);
      column.appendChild(statusEl);
    }
    paintSpacers();
    _measureSoon();
  }

  /** 已挂行实测进 hmap（合并到帧末一次布局读）。 */
  let _measRaf = 0;
  function _measureSoon() {
    if (_measRaf) return;
    const run = () => {
      _measRaf = 0;
      const _t = performance.now();
      let changed = false;
      for (const [key, seat] of seats) {
        if (!seat.el.isConnected) continue;
        const h = seat.el.offsetHeight;
        if (h > 0 && hmap.get(key) !== h) { hmap.set(key, h); changed = true; }
      }
      if (changed) { cumDirty = true; paintSpacers(); }
      _SEG.scroll += performance.now() - _t;
    };
    _measRaf = (typeof requestAnimationFrame === "function")
      ? requestAnimationFrame(run) : setTimeout(run, 16);
  }

  /** 行级尺寸监听：行高变化（详情展开/流式增高/图片）校订 hmap＋占位；
      视口**上方**的行长高 ⇒ scrollTop 同步 +Δ 保住阅读锚点。
      惰性创建：列/视口 RO 必须先于它注册（既有测试以 observers[0] 指认列 RO）。 */
  let roRows = null;
  function _getRoRows() {
    if (roRows || typeof ResizeObserver === "undefined") return roRows;
    roRows = new ResizeObserver((ents) => {
        const _t = performance.now();
        let deltaAbove = 0;
        const vTop = scroll.getBoundingClientRect().top;
        for (const en of ents || []) {
          const key = en.target.dataset && en.target.dataset.chatAnchorKey;
          if (!key) continue;
          const nh = en.target.offsetHeight;
          const oh = hmap.get(key);
          if (!(nh > 0) || oh === nh) continue;
          hmap.set(key, nh);
          cumDirty = true;
          if (oh !== undefined) {
            const r = en.target.getBoundingClientRect();
            if (r.bottom < vTop) deltaAbove += nh - oh;
          }
        }
        paintSpacers();
        if (deltaAbove) {
          _cancelPin();
          scroll.scrollTop += deltaAbove;
          observedTop = scroll.scrollTop;
        }
        _SEG.scroll += performance.now() - _t;
      });
    return roRows;
  }

  /** 换挡：滚动/视口变化后重算挂载区间，锚点校正到同一行同一偏移。 */
  function rewindow() {
    if (!order.length || !scroll.isConnected) return;
    const ch = scroll.clientHeight;
    if (!ch || !scroll.scrollHeight) return;      // 无布局环境不动窗
    const [nLo, nHi] = desiredRange();
    if (nLo === mntLo && nHi === mntHi) return;
    const a = anchorRow();                        // 先记「同一行同一偏移」
    _applyRange(nLo, nHi, _revOf);
    if (a) {
      const row = rowOf(a.key);
      if (row) {
        _cancelPin();
        const vTop = scroll.getBoundingClientRect().top;
        const off = row.getBoundingClientRect().top - vTop;
        scroll.scrollTop += off - a.off;
        observedTop = scroll.scrollTop;
      }
    }
  }
  let _revOf = null;   // 最近一次 render 的 revOf（rewindow 换行要用）

  /** 空态内容（由宿主按卡类型注入；未注入则用中性默认）。 */
  function setEmpty(payload) {
    const p = payload || {};
    const title = p.title || "还没有对话";
    const desc = p.desc || "在下面输入框说点什么，这张卡的对话会从这里开始。";
    const hints = Array.isArray(p.hints) ? p.hints : [];
    emptyEl.innerHTML =
      '<div class="ce-mark">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4 5.5h16v11H9l-5 4V5.5Z"/><path d="M8.5 9.5h7M8.5 13h4.5"/></svg></div>' +
      "<h3></h3><p></p>";
    emptyEl.querySelector("h3").textContent = title;
    emptyEl.querySelector("p").textContent = desc;
    if (hints.length) {
      const wrap = el("div", "ce-hints");
      for (const h of hints) {
        const b = el("button", "ce-hint");
        b.type = "button";
        b.textContent = h.label || h.text || "";
        b.onclick = () => {
          try {
            window.dispatchEvent(new CustomEvent("ui:fill-input", {
              detail: { text: h.text || h.label || "" },
            }));
          } catch (e) { /* 零阻断 */ }
        };
        wrap.appendChild(b);
      }
      emptyEl.appendChild(wrap);
    }
  }

  function paintEmpty() {
    const isEmpty = order.length === 0;
    skeletonEl.style.display = loading ? "" : "none";
    emptyEl.style.display = (!loading && isEmpty) ? "" : "none";
  }

  /* [建项目流程 2026-10-17] 初始态与「已渲染且无更多」保持一致：
     分页键与回底键都按数据条件显隐，不能靠「还没刷过 ⇒ 用 CSS 默认值」。
     此前从未 replayTrace 的会话（草稿）会多出两个点了就 404 的死按钮。 */
  paintOlder();
  paintToBottom();
  paintEmpty();
  paintStatus();   // 初态隐藏运行条（它此刻已在 DOM 里，见上面的挂载说明）

  // ---- 回底 ----
  /* [渲染层 2026-10-21] 原来「写 scrollTop → 立刻读回 scrollTop」是写后读，
     必然强制一次同步布局（实测 178ms）。最大值就是 scrollHeight，写进去的
     就是这个值，不必读回来自证——台账直接记它。 */
  /* [2026-11-21 · 性能批次二] 程序钉底的**测量合并**：列高在同一帧可多次
     变化（字体/图片/思考体展开），每次 RO/toBottom 各读 scrollHeight ＝
     各一次强制布局（实测首屏 4 次 RO＋3 次 toBottom 叠在同帧）。改合并到
     rAF：同帧多次请求只量一次＋写一次，校正仍在绘制前落地（无可见跳帧）。
     间隙里读者滚动（scrollTop 偏离台账）⇒ 放弃本次写入、交给 handleScroll
     判读者权——程序钉底不抢用户滚动。 */
  let _pinRaf = 0;
  let _pinGen = 0;
  function _pinBottom() {
    if (_pinRaf) return;
    const gen = ++_pinGen;
    const fire = () => {
      _pinRaf = 0;
      if (gen !== _pinGen) return;              // 已被锚点/恢复作废 ⇒ 不出手
      const _t = performance.now();
      const stTop = scroll.scrollTop;
      if (Math.abs(stTop - observedTop) > 0.5) {   // 读者动过 ⇒ 不抢
        _SEG.scroll += performance.now() - _t;
        return;
      }
      const top = scroll.scrollHeight;
      if (stTop !== top) scroll.scrollTop = top;
      observedTop = scroll.scrollTop;
      _SEG.pins = (_SEG.pins || 0) + 1;
      _SEG.scroll += performance.now() - _t;
    };
    _pinRaf = (typeof requestAnimationFrame === "function")
      ? requestAnimationFrame(fire) : setTimeout(fire, 16);
  }

  /* 锚点保位/热切恢复写 scrollTop 前调用：作废已排队的钉底——
     延迟写绝不能踩掉这些显式位置（世代号作废，不用辨 rAF/timeout）。 */
  function _cancelPin() { _pinGen++; _pinRaf = 0; }

  function toBottom() {
    const _t0 = performance.now();
    _SEG.toBottoms = (_SEG.toBottoms || 0) + 1;
    anchor = null;
    atBottom = true;
    /* 有界挂载：回底前先把尾窗挂上（未挂的行没有 DOM 可滚到） */
    if (order.length && mntHi !== order.length) {
      _applyRange(Math.min(mntLo, Math.max(0, order.length - INIT_MOUNT)), order.length, _revOf);
    }
    paintToBottom();
    _pinBottom();
    _SEG.scroll += performance.now() - _t0;
  }

  /* ══════════════════════════════════════════════════════════════════════
     [滚动位置 · 视图独占 2026-11-20 · 性能批次一]

     改前的病根：**宿主读写的是错的元素**。
       · 真正的滚动容器是本视图的 ``.flow-view-scroll``（overflow-y:auto）；
       · 宿主（workspace.scrollChatBottom / stream.activate）读写的是
         ``#chatArea``（``.chat``，overflow:hidden）⇒ 它根本不能滚：
         「写 scrollTop」是 no-op，「读 scrollHeight」却**照样强制一次
         同步布局**（整棵画板的排版）。
     ⇒ 后果有两个：① 每批多一次强制布局（首屏路径上）；②
        「切回恢复滚动位置」**一直是 no-op**（存进去的是 #chatArea 的 0，
        取出来还是 0）——热切回时对话跳回顶部。
     ⇒ 现在：滚动位置**只由本视图持有**，宿主通过 get/restoreScrollTop 走。

     语义不变的部分：跟随阈值 24px、离底才出回底钮、前插保位、回底钮的
     「新消息」标记。
     ══════════════════════════════════════════════════════════════════════ */

  /** 当前滚动位置（宿主存切走时的位置用）。 */
  function getScrollTop() {
    return scroll.scrollTop || 0;
  }

  /** 切回时恢复滚动位置（画板重新挂回文档后调用）。
      位置可能已随内容变化而失效 ⇒ 越界就夹到合法范围；
      恢复后按「离底多远」重算跟随权。 */
  function restoreScrollTop(v) {
    const _t0 = performance.now();
    _cancelPin();   // 热切恢复是显式位置：作废可能排队的延迟钉底
    const want = Number(v) || 0;
    try { scroll.scrollTop = want; } catch (e) { /* 零阻断 */ }
    const st = scroll.scrollTop || 0;
    const floor = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
    observedTop = st;
    atBottom = (floor - st) <= FOLLOW_THRESHOLD + 1;
    anchor = null;
    paintToBottom();
    /* 有界挂载：位置落在窗口外 ⇒ 换挡到该位置（帧后量，免打断恢复链） */
    if (scroll.isConnected) {
      const run = () => rewindow();
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
      else setTimeout(run, 16);
    }
    _SEG.scroll += performance.now() - _t0;
  }

  function paintToBottom() {
    toBottomSlot.style.display = atBottom ? "none" : "";
    toBottomBtn.classList.toggle("has-new", !!hasNew);
    const lab = toBottomBtn.querySelector(".flow-view-to-bottom-label");
    if (lab) lab.textContent = hasNew ? "新消息" : "";
  }

  /** 读者真实滚动判据：scrollTop 偏离台账（程序写入恒等于台账）。
      [渲染层 2026-10-21] 两处根因修在这里：
       ① 每次 scroll 都读 scrollHeight/clientHeight（读）＋ 紧接着 paintToBottom
          写 style（写）⇒ 读—写—读交替，每一帧都在强制同步布局（实测 28ms/次且
          无节流，滚轮一动就是几十次）。
       ② rowOf 遍历 querySelectorAll 反查（O(n) DOM 查询）。
      现在：读全部提到最前（一次布局），写全部放在后面；整段用 rAF 合并
      （同一帧内的多个 scroll 事件只处理最后一次状态）；rowOf 直接查座位表。 */
  let _scrollRaf = 0;
  function onScroll() {
    if (_scrollRaf) return;
    _scrollRaf = (typeof requestAnimationFrame === "function")
      ? requestAnimationFrame(() => { _scrollRaf = 0; handleScroll(); })
      : setTimeout(() => { _scrollRaf = 0; handleScroll(); }, 16);
  }

  function handleScroll() {
    const _t0 = performance.now();
    // ── 读：一次取齐（此后到写之前不再读布局） ──
    const sh = scroll.scrollHeight;
    const ch = scroll.clientHeight;
    const stTop = scroll.scrollTop;
    const floor = Math.max(0, sh - ch);
    const movedByReader = Math.abs(stTop - Math.min(observedTop, floor)) > 0.5;
    const isAtBottom = movedByReader
      ? floor - stTop <= FOLLOW_THRESHOLD + 1
      : atBottom;
    if (!movedByReader && isAtBottom) { toBottom(); return; }
    const needAnchorRefresh = !isAtBottom && anchor !== null;
    const needLoadOlder = movedByReader && stTop <= 8
      && hasMore && hasMore() && !(isLoadingOlder && isLoadingOlder());
    // 锚点重定位：需要行位置（读），放在所有写之前
    let anchorTop = null;
    if (needAnchorRefresh) {
      const row = rowOf(anchor.key);
      if (row) anchorTop = flowTop(row);
    }
    // 前插锚点：也要读行位置 ⇒ 同样放在写之前（原实现在写之后读，多一次强制布局）
    if (needLoadOlder) captureAnchor();
    // ── 写：状态与样式 ──
    atBottom = isAtBottom;
    if (isAtBottom) hasNew = false;
    paintToBottom();
    if (isAtBottom) anchor = null;
    else if (needAnchorRefresh && anchorTop !== null) anchor = { key: anchor.key, top: anchorTop };
    observedTop = stTop;
    // 滚到顶自动往前插一页（本项目既有行为；锚点已在读段记好）
    if (needLoadOlder) {
      if (typeof onLoadOlder === "function") onLoadOlder();
    }
    rewindow();          // 有界挂载：视口外换挡（锚点内部校正）
    _SEG.scroll += performance.now() - _t0;
  }
  scroll.addEventListener("scroll", onScroll, { passive: true });

  /** 记当前首行的 (key, top) 作前插锚点。
      [有界挂载] order[0] 可能在窗口外（未挂）——锚取「视口顶相交的已挂行」。 */
  function captureAnchor() {
    const a = anchorRow();
    const ch = scroll.clientHeight || 0;
    /* [任务二收尾] 只有「真在视口内」的挂载行才算阅读锚：顶翻触发瞬间
       挂载窗往往还停在底部（首个 bottom>视口顶 的行其实在视口**下方**
       几公里）——拿它当锚会把视口扔到窗口中部。回退语义＝展示窗首行
       贴顶（前插后用户从新旧接缝继续上翻）。 */
    if (a && a.off <= ch) anchor = { key: a.key, top: a.off };
    else if (order.length) anchor = { key: order[0], top: 0 };
    else anchor = null;
  }

  /** 键 → 行元素。座位表就是 key→el 的权威映射，不必再回 DOM 里线性查找。 */
  function rowOf(key) {
    const seat = seats.get(key);
    return seat ? seat.el : null;
  }

  function flowTop(row) {
    return row.getBoundingClientRect().top - scroll.getBoundingClientRect().top;
  }

  /** 上翻：先记锚，再请求加载（原文 :349-363）。 */
  olderBtn.onclick = () => {
    captureAnchor();
    if (typeof onLoadOlder === "function") onLoadOlder();
  };
  toBottomBtn.onclick = () => {
    /* 用户主动点「回到底部」：平滑滚（有过程感）。
       程序性跟随仍用瞬时写 scrollTop——平滑会与 ResizeObserver 的钉底互相拉扯。 */
    anchor = null;
    const top = scroll.scrollHeight;
    try { scroll.scrollTo({ top, behavior: "smooth" }); }
    catch (e) { scroll.scrollTop = top; }
    observedTop = top;
    atBottom = true;
    hasNew = false;
    paintToBottom();
  };

  /** 渲染一行：rev 未变 ⇒ 复用；变了 ⇒ 原地换元素（不搬位置）。 */
  function ensureSeat(key) {
    let seat = seats.get(key);
    if (seat) return seat;
    const els = renderRow(key);
    seat = { el: els.length === 1 ? els[0] : wrap(els), rev: null };
    seat.el.dataset.chatAnchorKey = key;
    seats.set(key, seat);
    return seat;
  }

  function wrap(els) {
    const box = el("div", "flow-view-item");
    for (const e of els) box.appendChild(e);
    return box;
  }

  /**
   * 全量落位（扁平节点序）：只建缺的、只换变了的，其余原地复用。
   * @param {string[]} keys 可见节点键（窗口内的，按序）
   * @param {(key:string)=>({rev:number}|null)} revOf 取该节点当前内容版本
   */
  function render(keys, revOf) {
    const _t0 = performance.now();
    _SEG.renders = (_SEG.renders || 0) + 1;
    try {
    order = keys.slice();
    _revOf = revOf;
    cumDirty = true;
    const want = new Set(order);
    // 剪枝：不在本序里的座位移除（卸载＝放 DOM＋断监听＋通知宿主）
    for (const [key] of Array.from(seats)) {
      if (!want.has(key)) unmountRow(key);
    }
    /* 有界挂载：首开挂尾部一小窗（最新正文＋操作区）；已开按视口换挡。
       锚点行强制在窗（前插保位要先把它挂上才能量偏移）。 */
    let nLo, nHi;
    if (!opened && BOUNDED) {
      nLo = Math.max(0, order.length - INIT_MOUNT);
      nHi = order.length;
    } else if (!opened) {
      nLo = 0;
      nHi = order.length;
    } else {
      [nLo, nHi] = desiredRange();
      if (anchor !== null) {
        const ai = order.indexOf(anchor.key);
        if (ai >= 0) { nLo = Math.min(nLo, Math.max(0, ai - 2)); nHi = Math.max(nHi, ai + 3); }
      }
      if (atBottom) { nHi = order.length; nLo = Math.min(nLo, Math.max(0, nHi - OVERSCAN)); }
    }
    _applyRange(nLo, nHi, revOf);
    paintEmpty();

    const newFirst = order[0] ?? null;
    const newLast = order.length ? order[order.length - 1] : null;
    const sig = order.length + ":" + newFirst + ":" + newLast + ":" + (isRunning && isRunning() ? 1 : 0);

    if (!opened) {
      opened = true;
      toBottom();
      firstKey = newFirst;
      lastKey = newLast;
      followSig = sig;
      return;
    }
    // 前插（首键变了且锚还在）：把同一行放回同一位置（原文 :237-249）
    if (anchor !== null && newFirst !== firstKey) {
      const row = rowOf(anchor.key);
      _cancelPin();   // 延迟钉底不得踩掉锚点保位
      if (typeof window !== "undefined")
        window.__DBG_ANCHOR = { key: anchor.key, want: anchor.top,
          got: row ? flowTop(row) : null, ai: order.indexOf(anchor.key),
          mnt: [mntLo, mntHi] };
      if (row) scroll.scrollTop += flowTop(row) - anchor.top;
      observedTop = scroll.scrollTop;
      anchor = null;
      firstKey = newFirst;
      lastKey = newLast;
      followSig = sig;
      return;
    }
    firstKey = newFirst;
    const appendedUser = newLast !== lastKey && newLast !== null && isUserNode(newLast);
    const tipMoved = followSig !== sig;
    lastKey = newLast;
    followSig = sig;
    // 跟随新内容，但**不因为 atBottom 为真就每次 render 都重钉**
    if (appendedUser || (tipMoved && atBottom)) toBottom();
    else if (tipMoved && !atBottom) { hasNew = true; paintToBottom(); }
    } finally { _SEG.mount += performance.now() - _t0; }
  }

  /** 新到的用户节点必须可见（原文 :251-253）。 */
  let userKeyTest = () => false;
  function isUserNode(key) { return userKeyTest(key); }

  /** 只重建某一行的内容（实时帧增量；不搬任何位置）。 */
  function refresh(key, rev) {
    const seat = seats.get(key);
    if (!seat) return false;
    if (rev !== undefined && seat.rev === rev) return false;
    const fresh = renderRow(key);
    const neo = fresh.length === 1 ? fresh[0] : wrap(fresh);
    neo.dataset.chatAnchorKey = key;
    if (seat.el.parentNode) seat.el.parentNode.replaceChild(neo, seat.el);
    seat.el = neo;
    seat.rev = rev !== undefined ? rev : seat.rev;
    return true;
  }

  // ---- 分页控件 ----
  function paintOlder() {
    const more = !!(hasMore && hasMore());
    olderWrap.style.display = more ? "" : "none";
    olderBtn.disabled = !!(isLoadingOlder && isLoadingOlder());
    olderBtn.textContent = (isLoadingOlder && isLoadingOlder()) ? "载入历史…" : "加载更早";
  }

  // ---- 运行条（15s 后才显时钟）----
  function paintStatus() {
    const running = !!(isRunning && isRunning());
    if (!running && !stopping) {
      statusEl.style.display = "none";
      if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
      return;
    }
    /* 停止指令发出到回合真正收口之间有一段真实等待（中止缝在 step 边界）。
       这段不表达出来，用户会以为「点了没反应」→ 再点几次 → 以为坏了。 */
    statusLabel.textContent = stopping ? "正在停止…" : "Deep diving...";
    statusEl.classList.toggle("is-stopping", stopping);
    statusEl.style.display = "";
    const anchorMs = (typeof runningStart === "function" ? runningStart() : null) ?? statusStart ?? Date.now();
    const tick = () => {
      const elapsed = Math.max(0, Date.now() - anchorMs);
      const show = elapsed >= 15000;
      clockEl.style.display = show ? "" : "none";
      clockEl.textContent = show ? formatRunDuration(elapsed) : "";
    };
    tick();
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(tick, 1000);
  }

  // ---- 列高变化：跟随（ResizeObserver；原文 :331-341）----
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      if (atBottom) {
        _SEG.roScrolls = (_SEG.roScrolls || 0) + 1;
        _pinBottom();   // 测量合并：同帧多次列高变化只读一次 scrollHeight
      }
      rewindow();       // 列/视口尺寸变化 ⇒ 挂载区间换挡（内部自带不变早退）
    });
    ro.observe(column);
    ro.observe(statusEl);
    ro.observe(scroll);
  }

  return {
    el: root,
    scrollEl: scroll,
    columnEl: column,
    render,
    /* 数据落地信号：首屏骨架只在真正拿到数据后收起（render 本身不清 loading，
       否则「先渲染一帧空数组」会把骨架换成空态，再闪回内容）。 */
    markLoaded() { loading = false; paintEmpty(); },
    refresh,
    toBottom,
    /* [滚动位置 · 视图独占 2026-11-20] 宿主存取滚动位置走这两个口
       （宿主自己碰 #chatArea ——那不是滚动容器——是改前的病根）。 */
    getScrollTop,
    restoreScrollTop,
    paintOlder,
    paintStatus,
    setEmpty,
    /** 运行态变化：重画运行条与「新用户节点」判据。 */
    setRunning(bool) {
      if (bool) statusStart = Date.now();
      stopping = false;
      paintStatus();
    },
    /** 停止指令已发出、回合尚未收口：运行条改「正在停止…」。 */
    setStopping(bool) {
      stopping = !!bool;
      if (stopping) statusStart = statusStart || Date.now();
      paintStatus();
    },
    setUserKeyTest(fn) { userKeyTest = fn; },
    /** 行卸载回调（宿主：清该键的渲染缓存与监听）。 */
    setOnUnmount(fn) { onUnmount = fn; },
    /** 自检/测试：当前挂载区间与行数。 */
    _mounted() { return { lo: mntLo, hi: mntHi, count: seats.size, total: order.length }; },
    /** 切卡入场：整块淡入 160ms（硬切会让人分不清「换了卡」还是「内容跳了」）。 */
    playEnter() {
      root.classList.remove("view-enter");
      void root.offsetWidth;   // 强制重排，让动画能重放
      root.classList.add("view-enter");
    },
    /** 上翻落地后调用：清锚（加载失败也清，避免下次误用）。 */
    endOlder() { anchor = null; },
    isAtBottom() { return atBottom; },
    destroy() {
      if (statusTimer) clearInterval(statusTimer);
      if (root.parentNode) root.parentNode.removeChild(root);
    },
  };
}
