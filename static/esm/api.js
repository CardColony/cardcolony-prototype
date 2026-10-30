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
  const cleanup = () => {
    clearTimeout(t);
    if (outer && onAbort) outer.removeEventListener("abort", onAbort);
  };
  return { signal: ctrl.signal, state, cleanup };
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
    const { signal, state, cleanup } = _composeSignal(o.signal, ms);
    init.signal = signal;
    try {
      const resp = await fetch(url, init);
      cleanup();
      // [2026-10-25] **通了** ⇒ 恢复（此前不可达则提示一次「服务已恢复」）。
      // 放在这一层：裸 request 的调用方（周期探测）也能把闩解开。
      _noteReachable();
      return resp;
    } catch (e) {
      cleanup();
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
    toast(msg, "error");
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

export async function postStop(wid, cid) {
  return call("POST", `/api/works/${wid}/cards/${cid}/stop`);
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
  getNavRunning: async () => {
    try {
      const resp = await request("GET", "/api/nav/running");
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) {
      return null;
    }
  },
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
  // [评审浮窗 2026-10-29] 这张卡的评审过程状态（有批次 ⇒ 右侧浮窗弹出）。
  getReviewStatus: (wid, cid, opts) =>
    call("GET", `/api/works/${wid}/cards/${cid}/review-status`, undefined, opts),
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
    return call("GET", `/api/works/${wid}/cards/${cid}/timeline${q.length ? "?" + q.join("&") : ""}`);
  },
  // [更新现状按钮改造 2026-10-16] op 缺省不带到请求体 ⇒ 普通发送与改前逐字等价；
  // 传 op（如 "update_status"）⇒ 本轮走专用通道（后端认它注入专用任务 + 追加专用工具）。
  postMessage: (wid, cid, content, op) => call("POST",
    `/api/works/${wid}/cards/${cid}/messages`, op ? { content, op } : { content }),
  getProgress: (wid, cid, opts) => call("GET", `/api/works/${wid}/cards/${cid}/progress`, undefined, opts),
  // [迭代四 F3①] 停止按钮（U-7）
  postStop: (wid, cid) => postStop(wid, cid),
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
