/* esm/api.js —— fetch 封装（统一错误弹 toast，messages 不设超时）。迭代四 F2 唯一实现。
 * 原 static/api.js 的 IIFE 本体收编于此；[首页两态 2026-10-15] 该薄壳随旧列表页
 * 整体退役一并删除，本文件自此是 fetch 封装的唯一一份实现。
 */
/* [网络层 2026-10-21] 三个根因在这里一次解决：
   ① 15s 硬超时 ＋ **零重试**：一次瞬时抖动（后端正被一打请求排队）就报「请求超时」，
      用户看到的是失败。GET 幂等 ⇒ 超时重试一次；POST 绝不重试（会重复落盘）。
   ② 请求**无法被调用方取消**：快速切卡时旧请求还在飞，回来还要抢连接、排队写 DOM。
      现在支持 `opts.signal`，调用方（open）切卡时 abort 上一个。
   ③ 超时与「调用方主动取消」都表现为 AbortError，原来一律弹「请求超时」——
      主动取消是正常流程，不该报错。按 signal 归属区分：外部 signal 已 abort ⇒ 静默抛。
   ④ `opts.silent`：周期探活类请求不弹 toast（失败由调用方静默降级）。 */

const DEFAULT_TIMEOUT_MS = 15000;

/** 连接层失败？（浏览器把它叫 `TypeError: Failed to fetch`）
 *
 *  [2026-10-25 · 治「页面频繁报网络错误」] 这类失败的**绝大多数**不是真断网，
 *  而是**复用了服务端已经关掉的 keep-alive 连接**：
 *  uvicorn 默认 `timeout_keep_alive=5`（空闲 5 秒即断），
 *  而浏览器把连接留在池里复用 ⇒ 下一次请求落在死连接上 ⇒ 直接失败。
 *  实测（裸 socket，同一条连接隔 N 秒再发一次）：
 *    等 1s / 3s ⇒ 正常 200；等 6s / 8s ⇒ WinError 10053 连接中止。
 *
 *  浏览器自己通常会重试这种失败，但**我们的封装故意不重试非超时错误**
 *  （原注释：「非超时（断网 / 连接被拒）：不重试」）⇒ 瞬时竞态
 *  直接变成用户可见的「网络错误」toast。
 *  ⇒ 连接层失败**允许重试一次**（GET 幂等；POST 仍绝不重试，见 attempts 计算）。
 */
function _isConnLevel(e) {
  if (!e) return false;
  if (e.name === "TypeError") return true;           // Chrome/Firefox: Failed to fetch
  const m = String(e.message || "");
  return /failed to fetch|networkerror|network error|econnreset|econnrefused|connection (reset|refused|closed|aborted)|socket hang up|terminated/i.test(m);
}

/** 客户端错误上报：连接层失败**服务端收不到请求** ⇒ 服务端日志天然看不见
 *  ⇒ 由客户端主动报一条（落 `logs/client_errors.jsonl`）。
 *  用 sendBeacon（fire-and-forget，不占连接、不等响应）；没有它就退回 keepalive fetch。
 *  上报失败一律吞掉——上报绝不能成为新的失败源。 */
function _reportClientError(where, url, e) {
  try {
    const body = JSON.stringify({
      where: where,
      url: String(url || ""),
      name: (e && e.name) || "",
      message: (e && e.message) || String(e || ""),
      href: (typeof location !== "undefined") ? String(location.href || "") : "",
      ua: (typeof navigator !== "undefined") ? String(navigator.userAgent || "") : "",
    });
    if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
      navigator.sendBeacon("/api/client-errors",
        new Blob([body], { type: "application/json" }));
      return;
    }
    fetch("/api/client-errors", {
      method: "POST", body,
      headers: { "Content-Type": "application/json" },
      keepalive: true,
    }).catch(() => { /* 上报失败零阻断 */ });
  } catch (e2) { /* 零阻断 */ }
}

/* ══════════════════════════════════════════════════════════════════════════
   服务可达性闩（2026-10-25 · 治「服务一关，网页就不停报网络错误」）

   病根：页面开着、服务被关掉 ⇒ 那些**周期轮询**（10s 运行总览、60s 版本探测、
   60s 角标）继续打 ⇒ 每次都失败 ⇒ 每次都弹一个「网络错误」⇒ 用户看到
   **每分钟两三个错**。实测：服务关掉 120 秒内 47 个请求、30 个失败。

   ⇒ 规矩：**同一个故障只报一次**。
     · 第一次连接层失败 ⇒ 提示一次「无法连接服务，正在重试…」；
     · 持续不可达 ⇒ **静默**（照旧上报到服务端日志，但不打扰用户）；
     · 恢复（任意一个请求成功）⇒ 提示一次「服务已恢复」。
   只管**连接层**失败（fetch 抛异常）；HTTP 4xx/5xx 走另一条路，不受这个闩影响。

   （真正的根治是「别轮询、改推送」——那是 nav_system/workspace 那层的活；
   这个闩保证即使还有残留轮询，也不会变成错误风暴。）
   ══════════════════════════════════════════════════════════════════════════ */

let _reachable = true;

/** 连接层失败 ⇒ 记一笔（首次弹「无法连接」，之后静默）。
 *  [2026-10-25] 提示语带**原因**，但**只带一次**：同一个故障不该刷屏。
 *  细节（Failed to fetch / ECONNRESET…）落在服务端日志（client_errors.jsonl），
 *  不往用户脸上堆。 */
function _noteUnreachable(e) {
  if (_reachable) {
    _reachable = false;
    const why = (e && e.name === "AbortError") ? "请求超时，正在重试…" : "无法连接服务，正在重试…";
    toast(why, "warn");
  }
}

/** 任意一个请求成功 ⇒ 恢复（首次弹「服务已恢复」）。 */
function _noteReachable() {
  if (!_reachable) {
    _reachable = true;
    toast("服务已恢复", "success");
  }
}

/** 当前是否可达（给「要不要退到兜底轮询」这类判断用）。 */
export function isReachable() { return _reachable; }

/** 外部信号：连接断了（长连接 SSE 断开）⇒ 记不可达（第一次提示一次）。
 *  ⚠ [P23 · 2026-10-25 修正] **对话页已不使用它**：SSE 断**不等于**后端不可达
 *  （代理抖动也会断），据此把闩按下会让兜底轮询一起停 ⇒ 左栏就此不更新，
 *  而服务端明明是通的。⇒ 「不可达」只由**实际请求失败**判定（call() 的失败分支）。
 *  这个口保留给「确实知道连不上」的场合（如明确的网络错误事件）。 */
export function markUnreachable() { _noteUnreachable(null); }

/** 外部信号：连接恢复了（长连接重连成功）⇒ 解闩（提示一次「服务已恢复」）。 */
export function markReachable() { _noteReachable(); }

/** 合成「外部取消 ＋ 内部超时」两路信号，返回 signal 与收尾函数。 */
function _composeSignal(outer, ms) {
  const ctrl = new AbortController();
  const state = { timedOut: false };
  const t = setTimeout(() => {
    state.timedOut = true;
    try { ctrl.abort(); } catch (e) { /* 零阻断 */ }
  }, ms);
  let onAbort = null;
  if (outer) {
    if (outer.aborted) {
      clearTimeout(t);
      try { ctrl.abort(); } catch (e) { /* 零阻断 */ }
    } else {
      onAbort = () => { try { ctrl.abort(); } catch (e) { /* 零阻断 */ } };
      outer.addEventListener("abort", onAbort, { once: true });
    }
  }
  const cleanupTimeout = () => { clearTimeout(t); };
  const cleanupAbort = () => {
    cleanupTimeout();
    if (outer && onAbort) outer.removeEventListener("abort", onAbort);
  };
  const cleanup = cleanupAbort;
  return { signal: ctrl.signal, state, cleanupTimeout, cleanupAbort, cleanup };
}

export async function request(method, url, body, opts) {
  const o = opts || {};
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  // messages / draft 不设超时（模型可能 30~120 秒），其他请求 15 秒
  if (url.includes("/messages") || url.includes("/draft")) {
    if (o.signal) init.signal = o.signal;
    return await fetch(url, init);
  }
  const ms = o.timeoutMs || DEFAULT_TIMEOUT_MS;
  const attempts = (o.retry === false || method !== "GET") ? 1 : 2;
  let lastErr = null;
  for (let i = 0; i < attempts; i++) {
    const { signal, state, cleanupTimeout, cleanupAbort } = _composeSignal(o.signal, ms);
    init.signal = signal;
    try {
      const _t = (typeof performance !== "undefined") ? performance.now() : 0;
      const resp = await fetch(url, init);
      /* [任务二·分账] 接口段：fetch 头到耗时（网络+服务端处理）。
         __PERF_SEG 存在才记——零成本常设路径。 */
      if (typeof window !== "undefined" && window.__PERF_SEG) {
        const s = (window.__PERF_SEG.net = window.__PERF_SEG.net
          || { ms: 0, reqs: 0, bytes: 0 });
        s.ms += performance.now() - _t; s.reqs++;
        const cl = resp.headers && resp.headers.get("content-length");
        if (cl) s.bytes += +cl || 0;
      }
      /* ★ 头到即解除 abort 连线 ⇒ **正文还在读的时候，取消已经传不下去了**
         （返工 2b）：消费者退了、底层正文却还在拉（首屏那条时序是 1.3MB）。
         ⇒ holdSignal：保留「外部取消 → 派生 signal」的连线，直到**正文读完**。
         头到之后不再计时（超时对读正文无意义）。 */
      cleanupTimeout();
      if (!o.holdSignal) cleanupAbort();
      // [2026-10-25] **通了** ⇒ 恢复（此前不可达则提示一次「服务已恢复」）。
      // 放在这一层：裸 request 的调用方（周期探测）也能把闩解开。
      _noteReachable();
      return resp;
    } catch (e) {
      cleanupAbort();
      lastErr = e;
      // 调用方主动取消：立刻退出（不重试、不报错）
      if (o.signal && o.signal.aborted) throw e;
      // [2026-10-25] **连接层**失败允许重试一次：多半是复用了服务端已关掉的
      // keep-alive 连接（uvicorn 空闲 5s 即断）⇒ 重发一次就好。
      // 其它非超时错误（真正的断网 / DNS 失败）仍不重试。
      if (!state.timedOut && !_isConnLevel(e)) throw e;
      // 连接层失败 ⇒ 记一笔，**哪怕重试成功也要留痕**：用户看不见的瞬时失败
      // 正是「频繁」的真相，日志里有了它才知道修没修好。
      // 只在**确实会重试**时记（where="retry"）；最终失败由 call() 记 where="call"。
      if (i < attempts - 1 && _isConnLevel(e)) _reportClientError("retry", url, e);
      if (i === attempts - 1) throw e;
    }
  }
  throw lastErr;
}

export async function call(method, url, body, opts) {
  const o = opts || {};
  // [在途合并 2026-11-20] GET 走**共享通道**：同一 URL 的并发请求合成一次。
  // 非 GET（写）永远不共享 —— 写的语义是「立刻生效」，不容合并。
  if (method === "GET" && !o.noShare) return _sharedGet(url, o);
  try {
    return await _callDirect(method, url, body, o);
  } finally {
    /* ★ 纪元在**写落定之后**推进，不是之前（2026-11-20 返工）。
       之前推进 ⇒「写期间发起的 GET」与「提交后的重读」拿到**同一个纪元**
       ⇒ 重读会并到写期间那条上 ⇒ **保存成功后仍然读到旧值**（实测复现）。
       写落定后推进 ⇒ 写期间那条（纪元 E）与提交后重读（纪元 E+1）**不合并** ✓
       （「提交后重读必须拿到写后的新值」这条语义由此守住）。
       ★ 写失败也推进：写可能部分落地，宁可作废在途读也不给旧值。 */
    _epoch++;
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   在途 GET 合并（2026-11-20 · 性能批次一）

   病根：同一次冷开里**同一个 GET 被打两次**——面板与审批探针各读一次
   /rule（两次都是整份规则正文）；开卡与刷新恢复各读一次 /progress。
   两次都实打实走网络、占连接、占服务端 IO。

   这里做的是「**同义在途**合并」：URL 完全相同 ⇒ 合成一次。
   ★ 边界三条：
     ① **消费者可各自退订**：一个消费者 abort（切卡）只摘掉它自己；
        别的消费者还要这条请求 ⇒ **底层请求继续跑**（不打断别人的）。
     ② **没人要了才真取消**：消费者退订到 0 ⇒ abort 底层 fetch，释放连接。
     ③ **晚回包只归属发起者**：每个消费者拿到的 promise 各自绑定
        自己的 signal ⇒ 退订者的 promise 立刻 reject（AbortError），
        它**绝不会**把过期数据写进当前卡。
   ★ 写操作会推进「纪元」：写前发起的 GET 与写后发起的 GET **不合并**
     （否则「提交后重读」会拿到提交前的旧值）。
   ══════════════════════════════════════════════════════════════════════════ */

/** 写纪元：任何非 GET 请求都会 +1（跨纪元的在途 GET 不合并）。 */
let _epoch = 0;

/** URL → 在途条目。条目：{epoch, ctrl, consumers:Set<{silent}>, promise} */
const _getInflight = new Map();

function _abortError() {
  const e = new Error("请求已取消");
  e.name = "AbortError";
  return e;
}

/** 取（或建）一条在途 GET，返回「本消费者自己的」promise。 */
function _sharedGet(url, o) {
  let entry = _getInflight.get(url);
  // 纪元不同（期间有写操作）⇒ 不复用旧条目，另起一条新的。
  if (entry && entry.epoch !== _epoch) entry = null;
  // ★ 条目已被作废（最后一个消费者退了）⇒ 也不能复用（返工 2a）。
  //   病根：原来条目只在 promise 的 finally 里摘表，而 finally 是**微任务** ⇒
  //   「最后一位消费者取消」之后**同步**重开同一 URL，会接到那条**已取消**的
  //   条目上 ⇒ 新请求直接 AbortError（等于这次读没发生）。
  if (entry && entry.dropped) entry = null;
  if (!entry) {
    entry = {
      url, epoch: _epoch,
      ctrl: new AbortController(),
      consumers: new Set(),
      promise: null,
      dropped: false,
    };
    entry.promise = _runSharedGet(entry, o).finally(() => {
      // 只有「我还是那条」才清表（期间可能已有新的同 URL 条目顶上）。
      if (_getInflight.get(url) === entry) _getInflight.delete(url);
    });
    // 无人接的 rejection 不该变成 unhandledrejection（消费者都退订的情况）。
    entry.promise.catch(() => { });
    _getInflight.set(url, entry);
  }
  return _attachConsumer(entry, o);
}

/** ★ 条目作废（返工 2a）：**立刻**从表里摘掉并取消底层请求。
    必须在「最后一个消费者退订」那一刻同步做，不能等 promise 的 finally
    （finally 是微任务 ⇒ 同步重开就会接到这条已取消的条目）。 */
function _dropEntry(entry) {
  if (entry.dropped) return;
  entry.dropped = true;
  if (_getInflight.get(entry.url) === entry) _getInflight.delete(entry.url);
  try { entry.ctrl.abort(); } catch (e) { /* 零阻断 */ }
}

/** 把一个消费者挂到共享条目上：退订只影响自己。 */
function _attachConsumer(entry, o) {
  const consumer = { silent: !!(o && o.silent) };
  entry.consumers.add(consumer);
  let settled = false;
  let detach = null;
  const cleanup = () => { if (detach) { detach(); detach = null; } };
  const p = new Promise((resolve, reject) => {
    entry.promise.then(
      (v) => { if (!settled) { settled = true; cleanup(); resolve(v); } },
      (e) => { if (!settled) { settled = true; cleanup(); reject(e); } },
    );
    const sig = o && o.signal;
    if (sig) {
      const onAbort = () => {
        if (settled) return;
        settled = true;
        entry.consumers.delete(consumer);
        cleanup();
        reject(_abortError());
        // 没人要了 ⇒ **立刻**作废这条条目（摘表 + 取消底层请求，释放浏览器连接）。
        // ★ 不能只 abort 底层而把条目留在表里（返工 2a）：同步重开会接到它。
        if (entry.consumers.size === 0) _dropEntry(entry);
      };
      detach = () => sig.removeEventListener("abort", onAbort);
      if (sig.aborted) { onAbort(); return; }
      sig.addEventListener("abort", onAbort, { once: true });
    }
  });
  // 消费者正常结束（拿到结果）⇒ 从集合里摘掉（条目自己会结束）。
  p.then(() => { entry.consumers.delete(consumer); },
         () => { entry.consumers.delete(consumer); });
  return p;
}

/** 共享通道的真正执行体：一次 fetch、一次解析、一次报错。 */
async function _runSharedGet(entry, o) {
  let resp;
  try {
    resp = await request("GET", entry.url, undefined, {
      signal: entry.ctrl.signal,
      timeoutMs: o.timeoutMs,
      retry: o.retry,
      /* ★ holdSignal（返工 2b）：保留「外部取消 → 派生 signal」的连线直到
         **正文读完**。否则头一到就解除连线 ⇒ 消费者这时退订，正文还在拉
         （首屏那条时序 1.3MB ⇒ 白拉一整条）。 */
      holdSignal: true,
    });
  } catch (e) {
    // 消费者全退订导致的 abort ⇒ 静默（不是「连不上」）。
    if (entry.ctrl.signal.aborted) throw e;
    _noteUnreachable(e);
    _reportClientError("call", entry.url, e);
    throw e;
  }
  let data = null;
  try {
    // 正文读取仍在 abort 连线之内 ⇒ 消费者此刻退订也能打断它。
    const _tj = (typeof performance !== "undefined") ? performance.now() : 0;
    data = await resp.json();
    /* [任务二·分账] JSON 解码段：body 读取+解析。 */
    if (typeof window !== "undefined" && window.__PERF_SEG) {
      const s = (window.__PERF_SEG.json = window.__PERF_SEG.json || { ms: 0, reqs: 0 });
      s.ms += performance.now() - _tj; s.reqs++;
    }
  } catch (e) {
    if (entry.ctrl.signal.aborted) throw _abortError();
    /* 非 JSON */
  }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : ("HTTP " + resp.status);
    // 有一条消费者要出声（silent=false）才弹 —— 免得周期探活刷屏。
    let anyLoud = false;
    for (const c of entry.consumers) { if (!c.silent) { anyLoud = true; break; } }
    if (anyLoud) toast(msg, "error");
    throw new Error(msg);
  }
  return data;
}

/* ══ [任务三·合源] 可读点闸：首屏正文落板双帧后才放行的后台状态读。
   解闩＝渲染侧首屏出正文行调 markReadable()；草稿页/空正文由兜底放行。
   旗标 ``window.__FLOW_BOARD__.deferStatus === false`` ⇒ 立即执行（对照）。 */
let _readableResolve = null;
const _readableGate = new Promise((r) => { _readableResolve = r; });
export function markReadable() {
  const r = _readableResolve;
  if (r) { _readableResolve = null; r(); }
}
if (typeof window !== "undefined") setTimeout(() => markReadable(), 8000);
/* [任务三·世代校验] 第二参 ``check``：执行前一刻的归属谓词——排队期间
   切了卡/换了项目/开了新世代 ⇒ 跳过整个 fn（请求不发、浮窗不重置）。
   谓词在**发请求前**判（不是响应回来后）——这正是它与函数内
   stale() 检查的差别：过期调用连副作用入口都进不了。 */
export function deferStatusRead(fn, check) {
  const run = () => (check && !check()) ? undefined : fn();
  const fb = (typeof window !== "undefined" && window.__FLOW_BOARD__) || {};
  if (fb.deferStatus === false) return Promise.resolve().then(run);
  return _readableGate.then(run);
}

/** 非 GET 的直连通道（语义与改前逐字一致）。 */
async function _callDirect(method, url, body, opts) {
  const o = opts || {};
  let resp;
  try {
    resp = await request(method, url, body, o);
  } catch (e) {
    // 主动取消：静默（调用方自己决定后续，界面不该为「用户点了别的卡」报错）
    if (o.signal && o.signal.aborted) throw e;
    // [2026-10-25] **可达性闩**：同一个故障只报一次。
    // 以前每个失败都弹一个「网络错误」⇒ 服务一关、周期轮询继续打 ⇒
    // 用户看到**每分钟两三个错**（实测：服务关掉 120 秒内 30 个失败请求）。
    // 现在：首次失败由 _noteUnreachable 弹一次「无法连接服务，正在重试…」，
    // 之后一律静默（照旧上报服务端日志）。**旧的那条「网络错误」提示撤掉**——
    // 它与闩的消息重复（会弹两次），且细节已在服务端日志里。
    _noteUnreachable(e);
    // [2026-10-25] 落盘留证：这类失败**没进到服务端** ⇒ 服务端日志看不见
    // ⇒ 排障只能靠猜。由客户端主动报上来（logs/client_errors.jsonl）。
    _reportClientError("call", url, e);
    throw e;
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : ("HTTP " + resp.status);
    if (!o.silent) toast(msg, "error");
    throw new Error(msg);
  }
  // [2026-10-25] 通了 ⇒ 恢复（已在 request() 成功那一层处理：那里才是「后端可达」的真相）
  return data;
}

/* [迭代四 F3①] notify：升级提示机制（替代 4 秒即逝的 toast 语义面）。
 * 历史 50 条存 store.notifyLog；同屏上限 3（超出挤掉最旧）；onClick 可点；
 * kind ∈ {info,success,warn,error}。打点 [notify]。
 * toast 保留为兼容包装（B-11 三处调用零改动）。
 */
import { store } from "./store.js?v=30";

const NOTIFY_MAX_HISTORY = 50;
const NOTIFY_MAX_ONSCREEN = 3;
let notifySeq = 0;

export function notify(msg, kind = "info", opts = {}) {
  const item = { id: `n${++notifySeq}`, msg, kind, ts: Date.now() };
  const log = store.state.notifyLog.slice(-(NOTIFY_MAX_HISTORY - 1));
  log.push(item);
  store.setState({ notifyLog: log });
  try { console.info("[notify]", kind, msg); } catch (e) { /* 零阻断 */ }

  const c = document.getElementById("toastContainer");
  if (!c) return item;
  // 同屏上限：挤掉最旧
  while (c.querySelectorAll(".toast.notify").length >= NOTIFY_MAX_ONSCREEN) {
    const oldest = c.querySelector(".toast.notify");
    if (oldest) oldest.remove(); else break;
  }
  const el = document.createElement("div");
  el.className = `toast notify ${kind}` + (opts.onClick ? " clickable" : "");
  el.textContent = msg;
  if (opts.onClick) {
    el.onclick = () => { opts.onClick(item); el.remove(); };
  }
  c.appendChild(el);
  const ttl = opts.ttl || 8000;   // 可点提示留 8s（替代 4s 即逝）
  let timer = setTimeout(() => el.remove(), ttl);
  /* 鼠标在上面时停表：提示是给人读的，读的时候被收掉＝白报。移开后重新计时。 */
  el.addEventListener("mouseenter", () => { clearTimeout(timer); timer = null; });
  el.addEventListener("mouseleave", () => {
    if (timer) return;
    timer = setTimeout(() => el.remove(), 3000);
  });
  return item;
}

// toast 兼容包装：既有调用零改动，内部走 notify 历史
export function toastCompat(msg, kind) {
  notify(msg, kind || "info");
}

export async function postStop(wid, cid, session) {
  const q = session ? `?session=${encodeURIComponent(session)}` : "";
  return call("POST", `/api/works/${wid}/cards/${cid}/stop${q}`);
}

/* [5715] 插话：把一条用户消息投递进**正在跑**的这一轮。
   返回 {applied:bool, reason:string}：applied=false ＝ 这一轮赶不上了
   （无在途回合）⇒ 调用方回退成排队，不是错误。 */
export async function postSteer(wid, cid, content) {
  return call("POST", `/api/works/${wid}/cards/${cid}/steer`, { content: String(content || "") });
}

export const API = {
  toast,
  call,
  // [推送优先 2026-10-25] 后端当前**可达**吗（可达性闩的只读口）。
  // 用途：服务不可达时，兜底轮询**也该停**——后端根本收不到，探测只是在制造错误。
  isReachable,
  // [推送优先 2026-10-25] 长连接（SSE）的断/恢复直接驱动可达性闩：
  // 轮询停了以后没有 fetch 会失败 ⇒ 闩不会自己按下 ⇒ 靠 SSE 的断线来按。
  markUnreachable,
  markReachable,
  listWorks: () => call("GET", "/api/works"),
  createWork: (title) => call("POST", "/api/works", { title }),
  getNav: () => call("GET", "/api/nav"),
  // [S2/T5] 导航按需子卡与版本探测（S1 后端既有端点；数据形态 2026-10-11 18:44 三通道实测）
  getNavChildren: (wid, parent) => call("GET", `/api/nav/children?work_id=${wid}&parent=${parent}`),
  getNavVersion: () => call("GET", "/api/nav/version"),
  // [左栏接线 步1 后端已落] 「谁在跑」总览：行尾运行中转圈的数据源（判据在后端 active 位）
  // 静默实现，不走 call()：本口是周期探测（10s），而 call() 在非 2xx 时无条件弹 toast——
  // 若服务尚未装载本口（旧版返回 404），就会每 10 秒弹一个错。失败一律返回 null，
  // 由调用方按「本轮拿不到运行中信息」处理（行尾不亮转圈，不打扰用户）。
  /* [任务三·合源] 走 call() 共享通道：probeRunning 周期探测与 stream 侧
     恢复校准在途合并成一次；silent 消费者不弹 toast（周期探测语义不变）。
     返回 {running, coverage?}——coverage.missing 非空＝部分 runner 缺失，
     调用方按「可确认的更新、未确认的保留」防误清运行态；null＝查询失败。 */
  getNavRunning: async () => {
    try {
      return await call("GET", "/api/nav/running", undefined, { silent: true });
    } catch (e) {
      return null;
    }
  },
  deferStatusRead,
  markReadable,
  // [画布 2026-10-19] 活态：谁在跑 + 最近发生了什么（一次到手）。
  // 周期探测（3s），静默实现：失败返回 null，不弹 toast——辅助信息拿不到时
  // 画布照常可用，只是少了"在跑"的呼吸条，不能因此每 3 秒打扰用户一次。
  getPulse: async (wid, limit = 20) => {
    try {
      const resp = await request("GET", `/api/works/${wid}/pulse?limit=${limit}`);
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) {
      return null;
    }
  },
  getCanvas: (wid) => call("GET", `/api/works/${wid}/canvas`),
  patchPositions: (wid, positions) => call("PATCH", `/api/works/${wid}/canvas/positions`, { positions }),
  createCard: (wid, body) => call("POST", `/api/works/${wid}/cards`, body),
  getCard: (wid, cid, opts) => call("GET", `/api/works/${wid}/cards/${cid}`, undefined, opts),
  // [窄投影 2026-11-20 · 性能批次一] 只取首屏要的 meta（**不带 body**）。
  // 实测 work_073/card_007：整张卡面 530KB 里 99.5% 是 body，meta 只有 2.7KB。
  // 正文是右栏「卡详情」的区域载荷 ⇒ 那个区域展开时再读全文（getCard）。
  getCardMeta: (wid, cid, opts) =>
    call("GET", `/api/works/${wid}/cards/${cid}?projection=meta`, undefined, opts),
  // [评审浮窗 2026-10-29] 这张卡的评审过程状态（有批次 ⇒ 右侧浮窗弹出）。
  getReviewStatus: (wid, cid, opts) =>
    call("GET", `/api/works/${wid}/cards/${cid}/review-status`, undefined, opts),
  // [对齐场浮窗 2026-11-11] 这张卡参与的对齐场状态（开着 ⇒ 右侧浮窗弹出）。
  getAlignStatus: (wid, cid, opts) =>
    call("GET", `/api/works/${wid}/cards/${cid}/align-status`, undefined, opts),
  // [wb_exec 2026-10-23] coder 卡的执行体选择（谁干活：WorkBuddy 还是系统自己的模型）
  // 读：当前设置 + 上次实际路由到的 + 候选项
  // 写：**只改 exec_backend**（不碰该卡原有的模型覆盖），下一回合生效
  getExecBackend: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/exec-backend`),
  setExecBackend: (wid, cid, backend) =>
    call("PUT", `/api/works/${wid}/cards/${cid}/exec-backend`, { backend }),
  // [wb_exec 2026-10-23 16:20] 执行体**总开关**（全局、运行时生效、不用重启）
  getWbExecSwitch: () => call("GET", "/api/wb-exec/switch"),
  setWbExecSwitch: (enabled) => call("PUT", "/api/wb-exec/switch", { enabled }),
  // [角色机制 5710] 角色卡：组阁/关注/巡检/判词/警报
  getRoles: (wid) => call("GET", `/api/works/${wid}/roles`),
  getRole: (wid, rid) => call("GET", `/api/works/${wid}/roles/${rid}`),
  createRole: (wid, body) => call("POST", `/api/works/${wid}/roles`, body),
  updateRole: (wid, rid, body) => call("PATCH", `/api/works/${wid}/roles/${rid}`, body),
  setRoleDuty: (wid, rid, state, note = "") =>
    call("POST", `/api/works/${wid}/roles/${rid}/duty`, { state, note }),
  issueDirective: (wid, rid, body) =>
    call("POST", `/api/works/${wid}/roles/${rid}/directives`, body),
  verifyDirective: (wid, rid, did, status, evidence = "") =>
    call("POST", `/api/works/${wid}/roles/${rid}/directives/${did}/verify`, { status, evidence }),
  checkRole: (wid, rid) => call("POST", `/api/works/${wid}/roles/${rid}/check`),
  watchRole: (wid, rid, card_ids, action = "add") =>
    call("POST", `/api/works/${wid}/roles/${rid}/watch`, { card_ids, action }),
  unwatchRole: (wid, rid, cid) =>
    call("DELETE", `/api/works/${wid}/roles/${rid}/watch/${cid}`),
  getAlarms: (wid, status = "open") =>
    call("GET", `/api/works/${wid}/alarms?status=${encodeURIComponent(status)}`),
  ackAlarm: (wid, aid) => call("POST", `/api/works/${wid}/alarms/${aid}/ack`),
  correctAlarm: (wid, aid) => call("POST", `/api/works/${wid}/alarms/${aid}/correct`),
  patchCard: (wid, cid, updates, history_note) =>
    call("PATCH", `/api/works/${wid}/cards/${cid}`, { updates, history_note }),
  // [删项目 2026-10-21] 整个项目目录移到 works/_trash/（可恢复）。
  deleteWork: (wid) => call("DELETE", `/api/works/${wid}`),
  // [级联删除 2026-10-21] cascade=true ⇒ 连同整棵子树一起删（每张进回收站）
  deleteCard: (wid, cid, cascade) =>
    call("DELETE", `/api/works/${wid}/cards/${cid}${cascade ? "?cascade=true" : ""}`),
  // [画布 2026-10-19] 撤销删除：从回收站按原 card_id 恢复（幂等）
  restoreCard: (wid, cid) => call("POST", `/api/works/${wid}/cards/${cid}/restore`),
  getTrash: (wid) => call("GET", `/api/works/${wid}/trash`),
  getSession: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/session`),
  // [消息底座收尾 2026-10-14] 恢复唯一入口：合并时序表（消息块＋过程事件＋思考），
  // 按 seq 升序、每条自带 type。刷新/换卡重建过程区走它（件4 退役 from=0 整卡重放后，
  // 这是唯一可回溯的时序真相源）。
  // [画板四机制 T5 2026-10-16] 开 `from` 口（增量拉取）：**缺省不带到参数 ⇒ 与改前逐字
  // 等价**（后端 `frm` 默认 0＝全量）；传 N 则只回 seq>=N 的记录（后端语义**含端点**，
  // routes.py `frm=Query(0, alias="from")` → `event_log.timeline(from_seq=frm)`）。
  // ★ 这里的 N 必须是**落盘 events seq**，绝不是内存 journal seq（两套计数空间）。
  /* [对话流分页 2026-10-20] opts.tail / opts.before 是**窗口参数**（后端同名 Query）：
       tail=N   → 只回最后 N 条（打开卡片用；长会话从上千条降到几百条）
       before=S → 只回 seq < S（往上翻历史用）
     不传 opts ＝ 全量（与改前逐字等价，其它调用点零影响）。 */
  getTimeline: (wid, cid, from, opts) => {
    const q = [];
    if (typeof from === "number" && from > 0) q.push("from=" + from);
    if (opts && opts.tail > 0) q.push("tail=" + opts.tail);
    if (opts && opts.before > 0) q.push("before=" + opts.before);
    // [席位会话] session=card@N ⇒ 取该会话的时序
    if (opts && opts.session) q.push("session=" + encodeURIComponent(opts.session));
    /* [窄投影 2026-11-21 · 性能批次二] opts.view="brief" ⇒ 折叠类事件的
       重字段回预览+_brief 标记（旧后端忽略此参，照常回全文 ⇒ 兼容）。 */
    if (opts && opts.view) q.push("view=" + encodeURIComponent(opts.view));
    // [取消 2026-11-20] opts 整个透传给 call()（此前只取了三个查询参数，
    // signal 被丢掉 ⇒ 切卡无法取消在途时序拉取）。
    return call("GET", `/api/works/${wid}/cards/${cid}/timeline${q.length ? "?" + q.join("&") : ""}`,
      undefined, opts);
  },
  /* [窄投影补详情 2026-11-21 · 性能批次二] 按 seq 取**完整**时序记录。
     brief 模式下展开折叠详情时按需回取原文；seqs 可以是单个数或数组。
     返回 {items:[...], revoked:[seq...]}；旧后端 404 ⇒ 调用方静默降级。 */
  getTimelineEvent: (wid, cid, seqs, opts) => {
    const list = Array.isArray(seqs) ? seqs.join(",") : String(seqs);
    const q = ["seqs=" + encodeURIComponent(list)];
    if (opts && opts.session) q.push("session=" + encodeURIComponent(opts.session));
    return call("GET", `/api/works/${wid}/cards/${cid}/timeline/event?${q.join("&")}`,
      undefined, Object.assign({ silent: true }, opts || {}));
  },
  // [更新现状按钮改造 2026-10-16] op 缺省不带到请求体 ⇒ 普通发送与改前逐字等价；
  // 传 op（如 "update_status"）⇒ 本轮走专用通道（后端认它注入专用任务 + 追加专用工具）。
  // [席位会话] opts.session=card@N ⇒ 本条消息进该会话（主会话缺省不带）。
  postMessage: (wid, cid, content, op, opts) => {
    const body = { content };
    if (op) body.op = op;
    if (opts && opts.session) body.session = opts.session;
    return call("POST", `/api/works/${wid}/cards/${cid}/messages`, body);
  },
  getProgress: (wid, cid, opts) => {
    const q = (opts && opts.session) ? `?session=${encodeURIComponent(opts.session)}` : "";
    return call("GET", `/api/works/${wid}/cards/${cid}/progress${q}`, undefined, opts);
  },
  // [席位会话 2026-11-18 · 一卡多会话] 会话登记处：主会话＋各职能会话。
  getSessions: (wid, cid, opts) =>
    call("GET", `/api/works/${wid}/cards/${cid}/sessions`, undefined, opts),
  // [窄投影 2026-11-20 · 性能批次一] 当前卡**生效的模型**（一张卡一个模型键）。
  // 徽标原来读的是 /model-overrides/{wid}——那个口回**整个项目每一行**
  // （work_073 975 行 ≈ 260KB），而徽标只要一张卡 ⇒ 改走本口。
  getEffectiveModel: (wid, cid, opts) => {
    const q = cid ? `?card_id=${encodeURIComponent(cid)}` : "";
    return call("GET", `/api/settings/model-overrides/${encodeURIComponent(wid)}${q}`, undefined, opts);
  },
  createSession: (wid, cid, persona, label) =>
    call("POST", `/api/works/${wid}/cards/${cid}/sessions`, label ? { persona, label } : { persona }),
  // 职能选择器数据源：本 work 的职能清单（function_id/function_name）。
  listFunctions: (wid) => call("GET", `/api/works/${wid}/functions`),
  // [迭代四 F3①] 停止按钮（U-7）。session=card@N ⇒ 只停该会话的回合（不置卡级 halt）。
  postStop: (wid, cid, session) => postStop(wid, cid, session),
  // [5715] 插话（投递进正在跑的这一轮）
  postSteer: (wid, cid, content) => postSteer(wid, cid, content),
  // [迭代四 F4] 卡间消息端点
  getCardMessages: (wid, limit = 200) =>
    call("GET", `/api/works/${wid}/card-messages?limit=${limit}`),
  getMessageTypes: () => call("GET", "/api/message-types"),
  // [卡片编辑机制改造 2026-10-12 · 块3] 起草/确认写入
  postDraft: (wid, cid, body) => call("POST", `/api/works/${wid}/cards/${cid}/draft`, body),
  postCommit: (wid, cid, body) => call("POST", `/api/works/${wid}/cards/${cid}/commit`, body),
};

// toast 兼容包装（B-11 三处调用零改动；历史进 store.notifyLog）
export function toast(msg, kind) { return toastCompat(msg, kind); }
