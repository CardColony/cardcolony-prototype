/* api.js - fetch 封装（统一错误弹 toast，messages 不设超时） */
(function () {
  "use strict";

  function toast(msg, kind) {
    const c = document.getElementById("toastContainer");
    if (!c) { alert(msg); return; }
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  async function request(method, url, body) {
    const opts = {
      method,
      headers: {},
    };
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

  async function call(method, url, body) {
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

  const API = {
    toast,
    call,
    // 剧本架
    listWorks: () => call("GET", "/api/works"),
    createWork: (title) => call("POST", "/api/works", { title }),
    // 画布
    getCanvas: (wid) => call("GET", `/api/works/${wid}/canvas`),
    patchPositions: (wid, positions) => call("PATCH", `/api/works/${wid}/canvas/positions`, { positions }),
    // 卡片
    createCard: (wid, body) => call("POST", `/api/works/${wid}/cards`, body),
    getCard: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}`),
    patchCard: (wid, cid, updates, history_note) =>
      call("PATCH", `/api/works/${wid}/cards/${cid}`, { updates, history_note }),
    // 会话
    getSession: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/session`),
    postMessage: (wid, cid, content) => call("POST", `/api/works/${wid}/cards/${cid}/messages`, { content }),
    // [实时反馈 2026-10-02] 调用进度查询（纯展示层）
    getProgress: (wid, cid) => call("GET", `/api/works/${wid}/cards/${cid}/progress`),
  };

  window.API = API;
})();
