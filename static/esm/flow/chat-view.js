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
  statusEl.textContent = "Deep diving...";
  const clockEl = el("span", "flow-turn-status-clock");
  clockEl.setAttribute("aria-hidden", "true");
  statusEl.appendChild(clockEl);
  const toBottomSlot = el("div", "flow-view-to-bottom-slot");
  const toBottomBtn = el("button", "flow-view-to-bottom");
  toBottomBtn.type = "button";
  toBottomBtn.setAttribute("aria-label", "回到底部");
  toBottomBtn.title = "回到底部";
  toBottomBtn.innerHTML = '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 5.5L7 9l3.5-3.5"/></svg>';
  toBottomSlot.appendChild(toBottomBtn);

  column.appendChild(olderWrap);
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
  let liveSlot = null;

  /* [建项目流程 2026-10-17] 初始态与「已渲染且无更多」保持一致：
     分页键与回底键都按数据条件显隐，不能靠「还没刷过 ⇒ 用 CSS 默认值」。
     此前从未 replayTrace 的会话（草稿）会多出两个点了就 404 的死按钮。 */
  paintOlder();
  paintToBottom();

  /** 逐字正文槽（/tokens 直写区）：恒在运行条之前、全部节点座位之后。 */
  function ensureLiveSlot() {
    if (liveSlot === null) liveSlot = el("div", "flow-view-live-slot");
    column.appendChild(liveSlot);
    column.appendChild(statusEl);
    return liveSlot;
  }

  // ---- 回底 ----
  function toBottom() {
    anchor = null;
    scroll.scrollTop = scroll.scrollHeight;
    observedTop = scroll.scrollTop;
    atBottom = true;
    paintToBottom();
  }

  function paintToBottom() {
    toBottomSlot.style.display = atBottom ? "none" : "";
  }

  /** 读者真实滚动判据：scrollTop 偏离台账（程序写入恒等于台账）。 */
  function onScroll() {
    const floor = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
    const movedByReader = Math.abs(scroll.scrollTop - Math.min(observedTop, floor)) > 0.5;
    const isAtBottom = movedByReader
      ? floor - scroll.scrollTop <= FOLLOW_THRESHOLD + 1
      : atBottom;
    if (!movedByReader && isAtBottom) { toBottom(); return; }
    atBottom = isAtBottom;
    paintToBottom();
    if (isAtBottom) anchor = null;
    else if (anchor !== null) {
      const row = rowOf(anchor.key);
      if (row) anchor = { key: anchor.key, top: flowTop(row) };
    }
    observedTop = scroll.scrollTop;
    // 滚到顶自动往前插一页（本项目既有行为；锚点在插入前记好）
    if (movedByReader && scroll.scrollTop <= 8 && hasMore && hasMore() && !(isLoadingOlder && isLoadingOlder())) {
      captureAnchor();
      if (typeof onLoadOlder === "function") onLoadOlder();
    }
  }
  scroll.addEventListener("scroll", onScroll, { passive: true });

  /** 记当前首行的 (key, top) 作前插锚点。 */
  function captureAnchor() {
    const first = order[0];
    const row = first === undefined ? null : rowOf(first);
    if (row) anchor = { key: first, top: flowTop(row) };
  }

  function rowOf(key) {
    for (const row of column.querySelectorAll("[data-chat-anchor-key]")) {
      if (row.dataset.chatAnchorKey === key) return row;
    }
    return null;
  }

  function flowTop(row) {
    return row.getBoundingClientRect().top - scroll.getBoundingClientRect().top;
  }

  /** 上翻：先记锚，再请求加载（原文 :349-363）。 */
  olderBtn.onclick = () => {
    captureAnchor();
    if (typeof onLoadOlder === "function") onLoadOlder();
  };
  toBottomBtn.onclick = () => { toBottom(); };

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
    order = keys.slice();
    const want = new Set(order);
    // 剪枝：不在本序里的座位移除
    for (const [key, seat] of Array.from(seats)) {
      if (want.has(key)) continue;
      if (seat.el.parentNode) seat.el.parentNode.removeChild(seat.el);
      seats.delete(key);
    }
    // 逐键取/建/换
    const els = [];
    for (const key of order) {
      const seat = ensureSeat(key);
      const rev = revOf ? revOf(key) : null;
      if (seat.rev !== null && rev !== null && seat.rev !== rev) {
        const fresh = renderRow(key);
        const neo = fresh.length === 1 ? fresh[0] : wrap(fresh);
        neo.dataset.chatAnchorKey = key;
        if (seat.el.parentNode) seat.el.parentNode.replaceChild(neo, seat.el);
        seat.el = neo;
      }
      seat.rev = rev;
      els.push(seat.el);
    }
    // 落位：按序校正（已就位者 insertBefore 为 no-op ⇒ 零搬动）
    let prev = olderWrap;
    for (const e of els) {
      if (e.parentNode !== column || prev.nextSibling !== e) column.insertBefore(e, prev.nextSibling);
      prev = e;
    }
    // 逐字槽与运行条恒在末位之后（顺序：live 槽 → 运行条）
    if (liveSlot !== null) column.appendChild(liveSlot);
    column.appendChild(statusEl);

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
    if (!running) {
      statusEl.style.display = "none";
      if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
      return;
    }
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
        scroll.scrollTop = scroll.scrollHeight;
        observedTop = scroll.scrollTop;
      }
    });
    ro.observe(column);
    ro.observe(statusEl);
  }

  return {
    el: root,
    scrollEl: scroll,
    columnEl: column,
    render,
    refresh,
    ensureLiveSlot,
    toBottom,
    paintOlder,
    paintStatus,
    /** 运行态变化：重画运行条与「新用户节点」判据。 */
    setRunning(bool) {
      if (bool) statusStart = Date.now();
      paintStatus();
    },
    setUserKeyTest(fn) { userKeyTest = fn; },
    /** 上翻落地后调用：清锚（加载失败也清，避免下次误用）。 */
    endOlder() { anchor = null; },
    isAtBottom() { return atBottom; },
    destroy() {
      if (statusTimer) clearInterval(statusTimer);
      if (root.parentNode) root.parentNode.removeChild(root);
    },
  };
}
