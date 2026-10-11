/* esm/api.js —— fetch 封装（统一错误弹 toast，messages 不设超时）。迭代四 F2 唯一实现。
 * 原 static/api.js 的 IIFE 本体收编于此；api.js 退为薄壳（index 页动态 import 兼容）。
 */
export async function request(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  // messages 不设超时（模型可能 30~120 秒），其他请求 15 秒
  if (!url.includes("/messages")) {
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
 * toast 保留为兼容包装（B-11 三处调用零改动；index 页薄壳同样拿到）。
 */
import { store } from "./store.js";

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
  setTimeout(() => el.remove(), ttl);
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
  getCanvas: (wid) => call("GET", `/api/works/${wid}/canvas`),
  patchPositions: (wid, positions) => call("PATCH", `/api/works/${wid}/canvas/positions`, { positions }),
  createCard: (wid, body) => call("POST", `/api/works/${wid}/cards`, body),
  getCard: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}`),
  patchCard: (wid, cid, updates, history_note) =>
    call("PATCH", `/api/works/${wid}/cards/${cid}`, { updates, history_note }),
  deleteCard: (wid, cid) => call("DELETE", `/api/works/${wid}/cards/${cid}`),
  getSession: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/session`),
  postMessage: (wid, cid, content) => call("POST", `/api/works/${wid}/cards/${cid}/messages`, { content }),
  getProgress: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/progress`),
  // [迭代四 F3①] 停止按钮（U-7）
  postStop: (wid, cid) => postStop(wid, cid),
  // [迭代四 F4] 卡间消息端点
  getCardMessages: (wid, limit = 200) =>
    call("GET", `/api/works/${wid}/card-messages?limit=${limit}`),
  getMessageTypes: () => call("GET", "/api/message-types"),
};

// toast 兼容包装（B-11 三处调用零改动；历史进 store.notifyLog）
export function toast(msg, kind) { return toastCompat(msg, kind); }
