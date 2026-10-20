/* esm/flow/message-actions.js —— 消息操作条（复制 / 重发）
 *
 * 为什么要单独一件：这两个动作原先**长在渲染闭包里**
 * （stream.js 的用户消息渲染、workspace.js 的助手消息渲染）⇒ 它们既不能被
 * 单测驱动（导不出来），也**各写了一份剪贴板回退逻辑**（clipboard API →
 * execCommand 回退），改一处忘一处就会两处行为不一样。
 *
 * 抽出来后的口径：
 *   · 复制：clipboard.writeText 优先；没有该 API 时走 textarea + execCommand
 *     回退（隐私模式 / 老浏览器）；成功后按钮给一次反馈（is-ok + 文案/图标），
 *     1.4s 后复原。
 *   · 重发：**只对用户消息**有（重新生成只对最后一轮成立，放在历史消息上会
 *     生成到错误的轮次）⇒ 由调用方用 pending 标志决定要不要挂。
 *     动作以 window 自定义事件 `ui:resend` 交给宿主（stream 不持有发送闭包，
 *     避免反向依赖）。
 *
 * 载体替换（非机制改动）：逻辑逐字搬过来，只是从闭包里挪到可导入的模块。
 */

/** 剪贴板写入：clipboard 优先，execCommand 回退；一律返回 Promise。 */
export function copyText(text) {
  const value = String(text || "");
  if (!value) return Promise.resolve(false);
  if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(value).then(() => true, () => false);
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return Promise.resolve(!!ok);
  } catch (e) {
    return Promise.resolve(false);
  }
}

const ICON_COPY =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true">' +
  '<rect x="5.2" y="5.2" width="8" height="8" rx="1.6"/><path d="M10.8 3.2H3.6a.8.8 0 0 0-.8.8v7.2"/></svg>';

const ICON_OK =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M3.5 8.5l3 3 6-6.5"/></svg>';

const ICON_RESEND =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true">' +
  '<path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.7"/><path d="M13.4 2.6v3.2h-3.2"/></svg>';

/**
 * 复制按钮。
 * @param {object} opts
 * @param {() => string} opts.getText 取要复制的正文
 * @param {string} [opts.title]      按钮标题（默认「复制」）
 * @param {string} [opts.okTitle]    成功后的标题（默认沿用原标题，图标换成对勾）
 * @param {string} [opts.act]        data-act 标记（助手消息那条用 "copy"）
 * @param {(t:string)=>Promise<any>} [opts.copy] 注入的复制实现（默认用剪贴板）
 * @param {number} [opts.restoreMs]  反馈复原时长（默认 1400ms）
 * @returns {HTMLButtonElement}
 */
export function createCopyButton({
  getText, title = "复制", okTitle = "", copy = copyText, restoreMs = 1400, act = "",
} = {}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-act";
  if (act) btn.dataset.act = act;
  btn.title = title;
  btn.innerHTML = ICON_COPY;
  btn.onclick = () => {
    const text = typeof getText === "function" ? getText() : "";
    if (!text) return;
    Promise.resolve(copy(text)).then((ok) => {
      if (ok === false) return;          // 复制没成功 ⇒ 不给「已复制」的假反馈
      const old = btn.innerHTML;
      const oldTitle = btn.title;
      btn.classList.add("is-ok");
      btn.innerHTML = ICON_OK;
      if (okTitle) btn.title = okTitle;
      setTimeout(() => {
        btn.classList.remove("is-ok");
        btn.innerHTML = old;
        btn.title = oldTitle;
      }, restoreMs);
    });
  };
  return btn;
}

/**
 * 重发按钮：点一下派发 `ui:resend`（内容由 getText 给）。
 * @param {object} opts
 * @param {() => string} opts.getText
 * @param {string} [opts.title]
 * @returns {HTMLButtonElement}
 */
export function createResendButton({ getText, title = "重新发送这条提问" } = {}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-act";
  btn.title = title;
  btn.innerHTML = ICON_RESEND;
  btn.onclick = () => {
    const text = typeof getText === "function" ? getText() : "";
    if (!text) return;
    try {
      window.dispatchEvent(new CustomEvent("ui:resend", { detail: { content: text } }));
    } catch (e) { /* 零阻断 */ }
  };
  return btn;
}
