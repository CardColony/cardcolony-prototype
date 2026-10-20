/* tests/dom_scaffold.mjs —— jsdom 脚手架（对话页 DOM 网共用）
 *
 * 为什么单独一件：jsdom 的坑集中在**环境**（没有布局、没有 localStorage、
 * 没有 matchMedia、没有 clipboard），每个网各写一份 stub 会漂。
 * 这里把「哪些浏览器能力在 jsdom 里缺、怎么补」集中一处，新网直接用。
 *
 * 已知缺口（照抄自实测，别重复踩）：
 *   · localStorage  ⇒ JSDOM 必须给 `url:` 选项，否则 opaque origin 报 SecurityError；
 *   · matchMedia    ⇒ jsdom 无 ⇒ 主题网要 stub；
 *   · clipboard     ⇒ jsdom 无 ⇒ 复制网要 stub（**且必须 stub execCommand**：
 *                     回退路径里 `document.execCommand("copy")` 抛错会被吞掉，
 *                     于是「已复制」的反馈永远不触发 ⇒ 测试假绿）；
 *   · 布局          ⇒ jsdom 无（getBoundingClientRect 恒 0、scrollHeight 恒 0）
 *                     ⇒ 滚动/锚点类断言要自己造尺寸（见 makeScrollable）。
 */

/* jsdom 是**可选**依赖：没装时对话页 DOM 网整体跳过（不误红，也不假装跑过）。
   用动态 import + 顶层 await：静态 import 在缺包时会在模块加载期直接炸。 */
let JSDOM = null;
try {
  ({ JSDOM } = await import("jsdom"));
} catch {
  JSDOM = null;
}
export const JSDOM_AVAILABLE = !!JSDOM;
/** 没装 jsdom 时的统一跳过理由。 */
export const SKIP_REASON = "jsdom 未安装（npm install 后可跑本文件）";

/** 对话页最小 DOM（够主题/抽屉/折叠/复制/快捷键用）。 */
export const CHAT_HTML = `<!doctype html><html><body>
  <div class="app-nav"><input id="navSearchInput" /><div class="nav-search-box"></div></div>
  <button id="navDrawerBtn">抽屉</button>
  <button id="themeToggleBtn">主题</button>
  <button id="procToggleBtn">过程</button>
  <button id="navSearchClear">清除</button>
  <div id="notifyPanel" class="notify-panel"></div>
  <div id="chatArea"></div>
</body></html>`;

/**
 * 建一个真 DOM 环境，把 window/document 挂到全局（被测模块按全局取用）。
 * @returns {{dom, window, document, observers, clipboardWrites}}
 */
export function setupDom({ html = CHAT_HTML, url = "http://localhost/" } = {}) {
  const dom = new JSDOM(html, { pretendToBeVisual: true, url });
  const { window } = dom;

  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.localStorage = window.localStorage;
  globalThis.CustomEvent = window.CustomEvent;
  globalThis.Event = window.Event;
  globalThis.HTMLElement = window.HTMLElement;
  globalThis.Element = window.Element;
  globalThis.Node = window.Node;
  globalThis.getComputedStyle = window.getComputedStyle.bind(window);

  // innerText：jsdom **没有实现**（产品代码取气泡正文用的就是它）
  // ⇒ 这里按 textContent 补一份近似（真实浏览器里 innerText 会忽略不可见文本，
  // 这层差异对「复制气泡正文」这类断言没有影响）。
  if (!("innerText" in window.HTMLElement.prototype)) {
    Object.defineProperty(window.HTMLElement.prototype, "innerText", {
      get() { return this.textContent; },
      set(v) { this.textContent = v; },
      configurable: true,
    });
  }

  // matchMedia：jsdom 无 ⇒ 主题网要能控制「系统偏好」
  const mq = {
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  };
  window.matchMedia = () => mq;

  // clipboard：jsdom 无 ⇒ 复制网要能接住写入内容
  const clipboardWrites = [];
  const clipboard = {
    writeText(text) {
      clipboardWrites.push(text);
      return Promise.resolve();
    },
  };
  try {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: clipboard, configurable: true, writable: true,
    });
  } catch (e) { /* 老 node：退化为 execCommand 路径 */ }
  globalThis.__clipboardWrites = clipboardWrites;

  // 假 ResizeObserver：把「列高变化」的回调暴露出来，好断言「底部不飘」
  const observers = [];
  globalThis.ResizeObserver = class {
    constructor(cb) { this.cb = cb; observers.push(this); }
    observe(el) { this.el = el; }
    disconnect() {}
  };

  return {
    dom,
    window,
    document: window.document,
    mount: window.document.getElementById("chatArea"),
    observers,
    clipboardWrites,
  };
}

/**
 * jsdom 没有布局引擎：scrollHeight/clientHeight 恒 0、scrollTop 写不进去。
 * 这里给滚动容器造出**可推演的尺寸**，好把「跟随 / 离底 / 回底 / 锚点」跑成真行为。
 */
export function makeScrollable(scroll, { height = 1000, viewport = 400 } = {}) {
  let top = 0;
  Object.defineProperty(scroll, "scrollHeight", { value: height, configurable: true });
  Object.defineProperty(scroll, "clientHeight", { value: viewport, configurable: true });
  Object.defineProperty(scroll, "scrollTop", {
    get: () => top,
    set: (v) => { top = Math.max(0, Math.min(Number(v) || 0, height)); },
    configurable: true,
  });
  // jsdom 未实现 scrollTo（会报 not implemented）——这里替它落成「写 scrollTop」
  // 并记录调用，好断言「点回到底部 ⇒ 平滑滚到底」。
  const calls = [];
  scroll.scrollTo = (opts) => {
    calls.push(opts);
    scroll.scrollTop = opts && typeof opts === "object" ? opts.top : opts;
  };
  return { calls, get top() { return top; } };
}

/**
 * 给元素造一个**可控**的 getBoundingClientRect（jsdom 恒返回 0）。
 * 用来模拟「前面插了一段 ⇒ 下面的行整体下移」这类布局变化。
 */
export function stubRects(nodes, { top = 0, height = 50 } = {}) {
  const map = new Map(nodes.map((n, i) => [n, top + i * height]));
  for (const [node, t] of map) {
    node.getBoundingClientRect = () => ({
      top: t, bottom: t + height, left: 0, right: 0, width: 0, height,
      x: 0, y: t, toJSON() {},
    });
  }
  return {
    map,
    /** 改某行的位置（模拟布局变化）。 */
    setTop(node, t) {
      map.set(node, t);
      node.getBoundingClientRect = () => ({
        top: t, bottom: t + height, left: 0, right: 0, width: 0, height,
        x: 0, y: t, toJSON() {},
      });
    },
  };
}
