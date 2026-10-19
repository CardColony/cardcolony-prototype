/* esm/api.js —— fetch 封装（统一错误弹 toast，messages 不设超时）。迭代四 F2 唯一实现。
 * 原 static/api.js 的 IIFE 本体收编于此；[首页两态 2026-10-15] 该薄壳随旧列表页
 * 整体退役一并删除，本文件自此是 fetch 封装的唯一一份实现。
 */
export async function request(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  // messages / draft 不设超时（模型可能 30~120 秒），其他请求 15 秒
  if (!url.includes("/messages") && !url.includes("/draft")) {
    const ctrl = new AbortController();
    opts.signal = ctrl.signal;
    const t = setTimeout(() => ctrl.abort(), 15000);
    try {
      const resp = await fetch(url, opts);
      clearTimeout(t);
      return resp;
    } catch (e) {
      clearTimeout(t);
      throw e;
    }
  }
  return await fetch(url, opts);
}

export async function call(method, url, body) {
  let resp;
  try {
    resp = await request(method, url, body);
  } catch (e) {
    if (e.name === "AbortError") {
      toast("请求超时", "error");
    } else {
      toast("网络错误：" + e.message, "error");
    }
    throw e;
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : ("HTTP " + resp.status);
    toast(msg, "error");
    throw new Error(msg);
  }
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

export const API = {
  toast,
  call,
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
  getCanvas: (wid) => call("GET", `/api/works/${wid}/canvas`),
  patchPositions: (wid, positions) => call("PATCH", `/api/works/${wid}/canvas/positions`, { positions }),
  createCard: (wid, body) => call("POST", `/api/works/${wid}/cards`, body),
  getCard: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}`),
  patchCard: (wid, cid, updates, history_note) =>
    call("PATCH", `/api/works/${wid}/cards/${cid}`, { updates, history_note }),
  deleteCard: (wid, cid) => call("DELETE", `/api/works/${wid}/cards/${cid}`),
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
  getTimeline: (wid, cid, from) => call("GET",
    `/api/works/${wid}/cards/${cid}/timeline${(typeof from === "number" && from > 0) ? "?from=" + from : ""}`),
  // [更新现状按钮改造 2026-10-16] op 缺省不带到请求体 ⇒ 普通发送与改前逐字等价；
  // 传 op（如 "update_status"）⇒ 本轮走专用通道（后端认它注入专用任务 + 追加专用工具）。
  postMessage: (wid, cid, content, op) => call("POST",
    `/api/works/${wid}/cards/${cid}/messages`, op ? { content, op } : { content }),
  getProgress: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/progress`),
  // [迭代四 F3①] 停止按钮（U-7）
  postStop: (wid, cid) => postStop(wid, cid),
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
