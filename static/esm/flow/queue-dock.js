/* esm/flow/queue-dock.js —— 排队条（dsh `queue/QueueDock.tsx` 的移植件）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/queue/QueueDock.tsx
 *       ＋ QueueDock.module.css（样式原值见 style.css 的 .flow-queue-* 段）
 *
 * 移植口径：
 *   · 形态判据逐条对应原文 :33-50 ——
 *       queue = inbox.filter(placement==='queued')
 *       queue.length===0 ⇒ 不渲染（:46）
 *       interactionActive = queueMutable && (editing||busy)（:48）
 *       expanded = !collapsed || interactionActive（:49）
 *       listVisible = queue.length===1 || expanded（:50）
 *       collapsed 默认 true（:38）；队列空了自动收起（:42）
 *   · 三个动作（edit/remove/steer）走 updateQueue，失败 notify('error', …)（:52-67）
 *   · 编辑：Enter 保存（非 IME）、Escape 取消、空文本不保存（:69-76 / :110-120）
 *
 * 载体替换（非机制改动）：
 *   · dsh 的 Tooltip / Icon* 来自 ui-primitives；本机没有那层原子库 ⇒
 *     提示语改挂原生 title，图标改内联 SVG。
 *   · React 的 keyed 重渲染换成「按状态重绘 + 复用正在编辑的 <input>」：
 *     后者是 React 在编辑态下保持焦点与光标的等价手法，不改变任何可见行为。
 */

/** 排队条图标（内联 SVG，几何取自 ui-primitives 的 14/16 尺寸族）。 */
const ICON = {
  queue: '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="M2 4h10M2 7h10M2 10h6"/></svg>',
  chevronDown: '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 5.5L7 9l3.5-3.5"/></svg>',
  chevronUp: '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5L7 5l3.5 3.5"/></svg>',
  edit: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.5 2.5l3 3M3 13l.6-3.2 7-7 3 3-7 7L3 13z"/></svg>',
  trash: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8h5.8l.6-8"/></svg>',
  send: '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 12V2.5M3.5 6L7 2.5 10.5 6"/></svg>',
  check: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5l3 3 6-7"/></svg>',
  close: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
};

/** dsh 中文文案原值（locales.ts:152-162）。 */
const T = {
  count: (n) => n + " 条排队消息",
  edit: "编辑排队消息",
  editUnsupported: "包含非文本内容，暂不支持编辑",
  save: "保存排队消息",
  cancelEdit: "取消编辑",
  remove: "删除排队消息",
  steer: "插话发送",
  steerUnavailable: "仅运行中可插话发送",
  editFailed: "编辑失败：这条消息可能已经开始发送。",
  removeFailed: "删除失败：这条消息可能已经开始发送。",
  steerFailed: "插话发送失败，请重试。",
};

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

/**
 * 建排队条。
 * @param {object} opts
 * @param {HTMLElement} opts.mount   挂载点（输入卡上方的座位）
 * @param {object} opts.store        queue-store（getSnapshot/subscribe/updateQueue）
 * @param {() => boolean} opts.isMutable  queueMutable（dsh: subagent===null）
 * @param {() => boolean} opts.isRunning  running（决定 steer 键可用）
 * @param {(level:string,text:string)=>void} opts.notify
 */
export function createQueueDock({ mount, store, isMutable, isRunning, notify }) {
  const root = el("div", "flow-queue-dock");
  root.setAttribute("data-queue-dock", "");
  const panel = el("div", "flow-queue-panel");
  root.appendChild(panel);
  mount.appendChild(root);

  let editing = null;   // {id, text}
  let busy = null;      // itemId
  let collapsed = true; // dsh 默认收起（QueueDock.tsx:38）
  let editorEl = null;  // 复用中的编辑框（React 焦点保持的等价物）

  function render() {
    const queue = store.getSnapshot().filter((row) => row.placement === "queued");
    // 队列空了 ⇒ 收起（:42）；正在编辑的行消失 ⇒ 退出编辑（:43）
    if (queue.length === 0 && !collapsed) collapsed = true;
    if (editing !== null && (!isMutable() || !queue.some((row) => row.id === editing.id))) {
      editing = null;
      editorEl = null;
    }
    if (queue.length === 0) {
      root.style.display = "none";
      panel.innerHTML = "";
      editorEl = null;
      return;
    }
    root.style.display = "";

    const interactionActive = isMutable() && (editing !== null || busy !== null);
    const expanded = !collapsed || interactionActive;
    const listVisible = queue.length === 1 || expanded;

    panel.innerHTML = "";

    if (queue.length > 1) {
      const header = el("button", "flow-queue-header");
      header.type = "button";
      header.setAttribute("aria-expanded", expanded ? "true" : "false");
      header.disabled = interactionActive;
      header.innerHTML = '<span class="flow-queue-lead" aria-hidden>' + ICON.queue + "</span>"
        + '<span class="flow-queue-count">' + T.count(queue.length) + "</span>"
        + '<span class="flow-queue-chevron" aria-hidden>'
        + (expanded ? ICON.chevronDown : ICON.chevronUp) + "</span>";
      header.onclick = () => { collapsed = !collapsed; render(); };
      panel.appendChild(header);
    }

    const list = el("ul", "flow-queue-list");
    list.hidden = !listVisible;
    if (listVisible) {
      for (const row of queue) {
        list.appendChild(renderRow(row, queue.length));
      }
    }
    panel.appendChild(list);
  }

  function renderRow(row, total) {
    const li = el("li", "flow-queue-row");
    if (total === 1) {
      li.appendChild(el("span", "flow-queue-lead", ICON.queue));
    }
    if (editing !== null && editing.id === row.id) {
      if (editorEl === null) {
        editorEl = el("input", "flow-queue-editor");
        editorEl.type = "text";
        editorEl.setAttribute("aria-label", T.edit);
        editorEl.value = editing.text;
        editorEl.oninput = () => { editing = { id: row.id, text: editorEl.value }; refreshEditorButtons(); };
        editorEl.onkeydown = (ev) => {
          if (ev.key === "Escape") { editing = null; editorEl = null; render(); return; }
          if (ev.key === "Enter" && !ev.nativeEvent?.isComposing && !ev.isComposing) {
            ev.preventDefault();
            void saveEdit();
          }
        };
      }
      li.appendChild(editorEl);
    } else {
      const p = el("span", "flow-queue-preview");
      p.textContent = row.preview;
      li.appendChild(p);
    }

    if (isMutable()) {
      const actions = el("div", "flow-queue-actions");
      if (editing !== null && editing.id === row.id) {
        const save = actionBtn(T.save, ICON.check, () => { void saveEdit(); });
        save.disabled = busy !== null || String(editing.text).trim() === "";
        save.dataset.role = "save";
        const cancel = actionBtn(T.cancelEdit, ICON.close, () => { editing = null; editorEl = null; render(); });
        cancel.disabled = busy !== null;
        actions.appendChild(save);
        actions.appendChild(cancel);
      } else {
        const edit = actionBtn(T.edit, ICON.edit, () => {
          if (row.text !== null) { editing = { id: row.id, text: row.text }; editorEl = null; render(); }
        });
        // 禁用按钮不触发 hover ⇒ 不支持的原因走原生 title（原文 :158-160）
        if (row.text === null) edit.title = T.editUnsupported;
        edit.disabled = busy !== null || row.text === null;
        const remove = actionBtn(T.remove, ICON.trash, () => {
          void applyAction(row.id, { kind: "remove" }, T.removeFailed);
        });
        remove.disabled = busy !== null;
        const steer = actionBtn(T.steer, ICON.send, () => {
          void applyAction(row.id, { kind: "steer" }, T.steerFailed);
        });
        if (!isRunning()) steer.title = T.steerUnavailable;
        steer.disabled = busy !== null || !isRunning();
        actions.appendChild(edit);
        actions.appendChild(remove);
        actions.appendChild(steer);
      }
      li.appendChild(actions);
    }
    return li;
  }

  function actionBtn(label, icon, onclick) {
    const b = el("button", "flow-queue-action", icon);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.onclick = onclick;
    return b;
  }

  /** 编辑态里只更新保存键的可用性，不重绘（否则输入框失焦）。 */
  function refreshEditorButtons() {
    const save = panel.querySelector('[data-role="save"]');
    if (save) save.disabled = busy !== null || String(editing?.text ?? "").trim() === "";
  }

  async function applyAction(itemId, action, failure) {
    busy = itemId;
    render();
    try {
      await store.updateQueue(itemId, action);
      return true;
    } catch (e) {
      notify("error", failure);
      return false;
    } finally {
      busy = null;
      render();
    }
  }

  async function saveEdit() {
    if (editing === null || String(editing.text).trim() === "") return;
    const id = editing.id;
    const text = editing.text;
    const ok = await applyAction(id, { kind: "edit", content: [{ type: "text", text }] }, T.editFailed);
    if (ok) { editing = null; editorEl = null; render(); }
  }

  let unsub = store.subscribe(render);
  render();

  return {
    el: root,
    render,
    /** 运行态变化会影响 steer 键可用性 ⇒ 由 shell 通知重绘。 */
    refresh: render,
    /** [5702 · 2026-10-20] 换绑到另一张卡的队列（每卡一份前端权威队列）。
        切卡后旧卡的排队消息不再显示在新卡上方。 */
    setStore(next) {
      if (!next || next === store) return;
      try { unsub(); } catch (e) { /* noop */ }
      store = next;
      editing = null;
      editorEl = null;
      unsub = store.subscribe(render);
      render();
    },
    destroy() {
      unsub();
      if (root.parentNode) root.parentNode.removeChild(root);
    },
  };
}
