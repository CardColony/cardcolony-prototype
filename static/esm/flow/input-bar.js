/* esm/flow/input-bar.js —— 输入卡（dsh `skeleton/InputBar.tsx` 的移植件）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/skeleton/InputBar.tsx
 *       ＋ InputBar.module.css（样式原值见 style.css 的 .flow-input-* 段）
 *
 * 移植口径（逐条对上原文行号）：
 *   · disabled 判据（:129-133）＝ removed || inert || !live || blocked || parentOffline
 *     ★「运行中」**不在其中**——注释原文 "Running input stays free"。
 *   · 主键两态（:544/:704/:779）＝ running && subagent===null ⇒ 发送键变停止键。
 *   · 运行中 Enter（:335-339）＝ resolveSubmitMode(running,…) ⇒ queue，这条路从不关闭。
 *   · 防连发（:323）＝ `if (e.repeat) return`（按住 Enter 不得机关枪）。
 *   · IME（:111-119 / :284-286）＝ composition 标记 + keyCode 229 双保险，收尾延后 10ms。
 *   · 忙时（adjudicating/submitting）只读（:704）＋ onChange 早退（:344）。
 *   · Shift+Enter 恒为换行，且**先于** IME 判据（:281-283）。
 *   · 撤销/重做归机器（:298-307），绝不让浏览器原生栈跑。
 *   · 复制/剪切展开占位符（:368-391）；粘贴是一次事务（:393-417）。
 *   · 草稿盒＝一个滚动口 + 两层文字（:689-731）：隐藏 mirror 定高、backdrop 出字、
 *     textarea 透明只留光标。滚轮在自己的边缘把增量交给外层会话滚动口（:254-268）。
 *
 * 载体替换（非机制改动）：
 *   · React 的 useInput/inputActions/keyboard 三面 ⇒ 本文件内持有 InputMachine 实例，
 *     把「效果执行」与「DOM 事件面」写成两个函数（run / 各 on*）。
 *   · Tooltip 组件 ⇒ 原生 title；IconPlusOutline16 ⇒ 内联 SVG。
 *   · 附件面（图片 rail / 拖放 / 灯箱 / 粘贴图片）本机不存在 ⇒ 整段不接（原文中
 *     addImages 缺省时那些分支本身就是空转）。
 *   · 命令菜单面不存在 ⇒ `+` 按钮**整件不渲染**（[2026-10-17 用户裁定] 死件纪律，
 *     不取原文 :741 的 disabled 分支——那会留一个永远点不动的假按钮）。
 *   · /命令 的 adjudicator 不存在 ⇒ adjudicate 效果一律按「未命中」回
 *     `adjudicated(attempt, undefined)`，机器据此走 default-sink（原文 :496-498）。
 */

import { InputMachine } from "./machine.js?v=1";
import { COMPOSER_SUBMIT_GESTURE } from "./composer-submission.js?v=1";

/** 文案（dsh locales.ts 中文原值；placeholder.default 用本项目自己的说法）。 */
const T = {
  defaultPlaceholder: "和这张卡讨论…",
  unavailable: "会话不可用",
  parentOffline: "父会话已离线，无法继续发送；仍可停止当前运行",
  steerQueue: "Cmd/Ctrl+Enter 插话发送全部排队消息",
  stop: "停止生成",
  send: "发送消息",
};

const SEND_PATH = "M8.3125 0.980183C8.66767 1.0531 8.97902 1.20418 9.2627 1.43233C9.48724 1.61297 9.73029 1.85793 9.97949 2.10714L14.707 6.83468L13.293 8.24874L9 3.95577V15.0417H7V3.95577L2.70703 8.24874L1.29297 6.83468L6.02051 2.10714C6.26971 1.85793 6.51277 1.61297 6.7373 1.43233C6.97662 1.23986 7.28445 1.04402 7.6875 0.980183C7.8973 0.947006 8.1031 0.95516 8.3125 0.980183Z";

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

/**
 * 建输入卡。
 * @param {object} opts
 * @param {HTMLElement} opts.mount  挂载点
 * @param {object} opts.actions
 *   submit(draft, mode)  普通发送（shell 按 mode 决定排队/注入/直接发）
 *   stop()               停止本轮
 *   steerQueue()         把整队排队消息插话进本轮
 *   onPhase(phase)       相位变化通知（shell 可据此判忙）
 * @param {object} opts.policy      ComposerSubmissionPolicy
 * @param {() => object} opts.session  会话事实 {running, removed, live, blocked, parentOffline, empty}
 * @param {() => boolean} opts.hasQueued  是否有排队中的消息（canSteerQueue 用）
 * @param {(level:string,text:string)=>void} [opts.notify]
 */
export function createInputBar({ mount, actions, policy, session, hasQueued, notify }) {
  const machine = new InputMachine({ now: () => Date.now() });

  // ---- DOM（一次建好，永不重建：原文 :248-253 的「一个生命周期」）----
  const root = el("div", "flow-input-root");
  const noticeEl = el("div", "flow-input-notice");
  noticeEl.setAttribute("role", "status");
  noticeEl.style.display = "none";

  const card = el("div", "flow-input-card");
  card.setAttribute("data-composer-card", "");

  const scroll = el("div", "flow-input-scroll");
  scroll.setAttribute("data-input-scroll", "");
  const grow = el("div", "flow-input-grow");
  const backdrop = el("div", "flow-input-backdrop");
  backdrop.setAttribute("aria-hidden", "true");
  backdrop.setAttribute("data-input-backdrop", "");
  const ta = el("textarea", "flow-input-ta");
  ta.rows = 2;
  ta.setAttribute("data-phase", "plain");
  const mirror = el("div", "flow-input-mirror");
  mirror.setAttribute("aria-hidden", "true");
  mirror.setAttribute("data-input-mirror", "");
  grow.appendChild(backdrop);
  grow.appendChild(ta);
  grow.appendChild(mirror);
  scroll.appendChild(grow);

  const row = el("div", "flow-input-row");
  const tools = el("div", "flow-input-tools");
  // [2026-10-17 用户裁定] 命令菜单面不存在 ⇒ `+` 按钮整件不渲染（死件纪律），
  // 不取原文 :741 的 disabled 分支——那会留一个永远点不动的假按钮。
  const modes = el("div", "flow-input-modes");
  tools.appendChild(modes);

  const trailing = el("div", "flow-input-trailing");
  const primary = el("button", "flow-input-primary");
  primary.type = "button";
  trailing.appendChild(primary);

  row.appendChild(tools);
  row.appendChild(trailing);
  card.appendChild(scroll);
  card.appendChild(row);
  root.appendChild(noticeEl);
  root.appendChild(card);
  mount.appendChild(root);

  // 项目自有的两个起草键从 card.html 迁进来（不属 dsh，位置对齐 leftItems）
  const leftSlot = document.getElementById("flowInputLeft");
  if (leftSlot) { leftSlot.parentNode.removeChild(leftSlot); modes.appendChild(leftSlot); }

  // ---- 状态 ----
  let composing = false;
  let notice = null;
  let lastPrimaryStops = null;

  function snap() {
    return session();
  }

  /** disabled（原文 :133）。运行中**不在**其中。 */
  function isDisabled() {
    const s = snap();
    return !!s.removed || !s.live || s.blocked !== undefined || !!s.parentOffline;
  }

  function machineBusy() {
    const p = machine.state.phase;
    return p === "adjudicating" || p === "submitting";
  }

  /** canSteerQueue（原文 :147-148）。 */
  function canSteerQueue() {
    const s = snap();
    return !isDisabled() && !machineBusy() && isEmpty() && !!s.running
      && !s.subagent && typeof hasQueued === "function" && hasQueued();
  }

  function isEmpty() {
    return machine.state.draft.trim() === "";
  }

  // ---- 值同步（React 受控值的等价物）----
  function syncValue() {
    const draft = machine.state.draft;
    if (ta.value !== draft) {
      const caret = ta.value.length === draft.length ? (ta.selectionStart ?? draft.length) : draft.length;
      ta.value = draft;
      try { ta.setSelectionRange(caret, caret); } catch (e) { /* 未聚焦时个别引擎抛错，忽略 */ }
    }
    backdrop.textContent = draft;
    mirror.textContent = draft + "\n";
    ta.setAttribute("data-phase", machine.state.phase);
    ta.readOnly = machineBusy();
    ta.disabled = isDisabled();
  }

  // ---- 效果执行（原文的 shell 角色）----
  function run(ev) {
    const effects = machine.dispatch(ev);
    for (const fx of effects) {
      if (fx.type === "notice") {
        showNotice(fx.level, fx.text);
      } else if (fx.type === "default-sink") {
        actions.submit(fx.draft, fx.mode);
      } else if (fx.type === "adjudicate") {
        // 无命令面 ⇒ 一律未命中，机器自行回落 default-sink（原文 :496-498）
        run({ type: "adjudicated", attempt: fx.attempt, outcome: undefined });
      } else if (fx.type === "begin-submit") {
        // 有命令面才走这里；本机不存在，按未命中兜底，绝不静默吞掉草稿
        showNotice("error", "命令不可用");
      }
    }
    syncValue();
    refreshPrimary();
    return effects;
  }

  function showNotice(level, text) {
    notice = { level, text };
    noticeEl.textContent = text;
    noticeEl.className = "flow-input-notice" + (level === "error" ? " is-error" : "");
    noticeEl.style.display = "";
  }

  function clearNotice() {
    notice = null;
    noticeEl.textContent = "";
    noticeEl.style.display = "none";
  }

  // ---- 主键两态（原文 :544-555）----
  function refreshPrimary() {
    const s = snap();
    const primaryStops = !!s.running && !s.subagent;
    const disabled = isDisabled();
    const label = primaryStops ? T.stop : T.send;
    if (lastPrimaryStops !== primaryStops) {
      lastPrimaryStops = primaryStops;
      primary.innerHTML = primaryStops
        ? '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="3" fill="currentColor"/></svg>'
        : '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="' + SEND_PATH + '" fill="currentColor"/></svg>';
      primary.setAttribute("aria-label", label);
      primary.title = label;
    }
    primary.disabled = primaryStops
      ? typeof actions.stop !== "function"
      : isEmpty() || disabled || machineBusy();
    // 占位符优先级（原文 :709-718）：不可用 > 整队插话提示 > 默认
    ta.placeholder = disabled
      ? (s.parentOffline ? T.parentOffline : T.unavailable)
      : (canSteerQueue() ? T.steerQueue : T.defaultPlaceholder);
  }

  function onPrimary() {
    const s = snap();
    if (s.running && !s.subagent) { actions.stop(); return; }
    if (!isEmpty() && !isDisabled() && !machineBusy()) {
      actions.submit(machine.state.draft, policy.resolve(!!s.running, COMPOSER_SUBMIT_GESTURE.ENTER, true));
    }
  }
  primary.onclick = onPrimary;

  // ---- 光标可见性（原文 :169-204）----
  function revealCaret(caret) {
    const mirrorEl = mirror;
    const text = mirrorEl.firstChild;
    if (!(text instanceof Text)) return;
    if (scroll.scrollHeight <= scroll.clientHeight) return;
    const at = Math.min(caret, text.data.length);
    const afterNewline = at > 0 && text.data[at - 1] === "\n";
    const range = document.createRange();
    range.setStart(text, afterNewline ? at - 1 : at);
    if (afterNewline) range.setEnd(text, at);
    else range.collapse(true);
    const line = afterNewline ? Number.parseFloat(getComputedStyle(mirrorEl).lineHeight) || 0 : 0;
    const rect = range.getBoundingClientRect();
    const box = scroll.getBoundingClientRect();
    if (rect.bottom + line > box.bottom) scroll.scrollTop += rect.bottom + line - box.bottom;
    else if (rect.top + line < box.top) scroll.scrollTop -= box.top - rect.top - line;
  }

  function revealSelectionFocus() {
    const caret = ta.selectionDirection === "backward" ? ta.selectionStart : ta.selectionEnd;
    revealCaret(caret ?? ta.value.length);
  }

  function restoreCaret(caret) {
    requestAnimationFrame(() => {
      try { ta.setSelectionRange(caret, caret); } catch (e) { /* 忽略 */ }
      revealCaret(caret);
    });
  }

  // ---- 滚轮链接（原文 :254-268）----
  scroll.addEventListener("wheel", (e) => {
    const host = scroll.closest("[data-conversation-scroll]");
    if (!(host instanceof HTMLElement) || e.deltaY === 0) return;
    const atTop = scroll.scrollTop <= 0;
    const atEnd = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 1;
    if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atEnd)) return;
    e.preventDefault();
    host.scrollTop += e.deltaY;
  }, { passive: false });

  // ---- 键盘面（原文 :270-340）----
  ta.addEventListener("keydown", (e) => {
    // Shift+Enter 恒为换行，先于 IME 判据（:281-283）
    if (e.key === "Enter" && e.shiftKey) return;
    const isComposing = composing || e.isComposing || e.keyCode === 229;
    if (e.key === "ArrowUp" || e.key === "ArrowDown") return;      // 无弹层可仲裁
    if (e.key === "Escape") return;                                 // 无浮层可关
    if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z" || e.key === "y")) {
      // 撤销/重做归机器：原生栈会复活已发送内容（:298-307）
      e.preventDefault();
      if (machineBusy() || isDisabled()) return;
      const redo = e.key === "y" || e.shiftKey;
      run({ type: redo ? "redo" : "undo" });
      return;
    }
    if (e.key === " ") {
      if (isComposing) return;
      return;                                                       // 无 claim token 可吞
    }
    if (e.key !== "Enter") return;
    if (isComposing) return;
    e.preventDefault();
    if (e.repeat) return;                                           // 防连发（:323）
    if (isDisabled() || machineBusy()) return;
    const accelerated = e.ctrlKey || e.metaKey;
    if (accelerated && canSteerQueue()) { actions.steerQueue(); return; }
    const gesture = accelerated ? COMPOSER_SUBMIT_GESTURE.ACCELERATED : COMPOSER_SUBMIT_GESTURE.ENTER;
    run({
      type: "enter",
      mode: policy.resolve(!!snap().running, gesture, true),
    });
  });

  ta.addEventListener("input", () => {
    if (isDisabled() || machineBusy()) { syncValue(); return; }
    run({ type: "draft-changed", draft: ta.value });
  });

  ta.addEventListener("compositionstart", () => { composing = true; });
  ta.addEventListener("compositionend", () => { setTimeout(() => { composing = false; }, 10); });
  ta.addEventListener("select", () => {
    if (machine.state.paste !== undefined) run({ type: "invalidate-paste" });
  });

  // 复制/剪切：把占位符展开成 owner 的剪贴板投影（原文 :368-391）
  ta.addEventListener("copy", (e) => onCopyOrCut(e, false));
  ta.addEventListener("cut", (e) => onCopyOrCut(e, true));

  function onCopyOrCut(e, cut) {
    const start = ta.selectionStart ?? 0;
    const end = ta.selectionEnd ?? start;
    if (start === end) return;
    const draft = machine.state.draft;
    const touched = machine.state.occurrences.filter((o) => o.offset >= start && o.offset < end);
    if (touched.length === 0 && !cut) return;   // 纯文本拷贝走原生
    e.preventDefault();
    let text = "";
    let cursor = start;
    for (const o of touched) {
      text += draft.slice(cursor, o.offset) + o.clipboardText;
      cursor = o.offset + 1;
    }
    text += draft.slice(cursor, end);
    e.clipboardData.setData("text/plain", text);
    if (cut && !machineBusy() && !isDisabled()) {
      run({ type: "draft-changed", draft: draft.slice(0, start) + draft.slice(end), editRange: { start, end, insertedLength: 0 } });
      restoreCaret(start);
    }
  }

  // 粘贴是一次事务（原文 :393-417）
  ta.addEventListener("paste", (e) => {
    if (machineBusy() || isDisabled()) return;
    const text = e.clipboardData.getData("text/plain");
    if (text === "") return;
    e.preventDefault();
    const sel = { start: ta.selectionStart ?? 0, end: ta.selectionEnd ?? ta.selectionStart ?? 0 };
    run({ type: "paste-begin", text, selection: sel });
    restoreCaret(sel.start + text.length);
  });

  // 按钮按下不抢焦点（原文 :531-534）
  function keepFocus(e) { e.preventDefault(); ta.focus({ preventScroll: true }); }
  primary.addEventListener("mousedown", keepFocus);

  // ---- 对外 ----
  const bar = {
    el: root,
    machine,
    /** 运行态/会话事实变化后刷新按钮与占位符。 */
    refresh() {
      syncValue();
      refreshPrimary();
    },
    /** 发送被接受：清草稿、清撤销栈（machine.ts:544-553）。 */
    commitSent() {
      run({ type: "send-committed" });
      clearNotice();
    },
    /** 发送失败：草稿保住 + 红条（machine.ts:501-507 的 notice 语义）。 */
    failSend(message) {
      showNotice("error", message);
    },
    /** 把文本放回草稿（失败恢复/草稿持久化恢复）。 */
    setDraft(text) {
      run({ type: "draft-changed", draft: String(text == null ? "" : text) });
    },
    getDraft() { return machine.state.draft; },
    focus() { try { ta.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ } },
    notice,
    ta,
    clearNotice,
    showNotice,
  };

  syncValue();
  refreshPrimary();
  return bar;
}
