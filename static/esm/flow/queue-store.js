/* esm/flow/queue-store.js —— 排队消息存储（dsh `queue/store.ts` 的**适配层**实现）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/queue/store.ts
 *       ＋ contract/queue.ts（QueueItemId / QueueAction / QueueRow 三个类型）
 *
 * ── 为什么这里不是逐行搬运 ──
 * dsh 的 queue/store.ts 是**纯投影**：队列的真相在 Host 会话快照里
 * （`session.getSnapshot().queue`），store 只把那一行数组暴露成
 * getSnapshot/subscribe 两个方法，自己不存东西。
 * 本项目的后端**没有队列**（POST 一次跑完整轮，中途没有可寻址的投递窗口），
 * 所以「队列的真相」只能落在前端：本文件就是那份前端权威存储。
 *
 * 对外形状与 dsh 完全一致（getSnapshot / subscribe ＋ updateQueue 三个动作），
 * 因此 QueueDock 与 InputBar 的消费代码可以逐行对应；差别只在**谁持有真相**。
 * 这条差异属任务书 §0 允许的「唯一适配层：数据接口」。
 *
 * 行形状（对齐 ConversationSnapshot['queue'][number]）：
 *   { id, placement: 'queued' | 'steering', preview, text }
 *   text 为 null 表示「含非文本内容、不可编辑」——本机只入队纯文本，恒不为 null，
 *   但字段与判据保留，QueueDock 的禁用分支因此与 dsh 同构。
 */

let _seq = 0;

/** 建一份前端权威队列。 */
export function createQueueStore() {
  /** @type {{id:string, placement:'queued'|'steering', preview:string, text:string|null}[]} */
  let rows = [];
  const listeners = new Set();

  function publish() {
    for (const fn of Array.from(listeners)) {
      try { fn(); } catch (e) { /* 订阅者异常不阻断其余 */ }
    }
  }

  /** 单行预览：首行、压掉连续空白（视觉由 .flow-queue-preview 的省略号收尾）。 */
  function previewOf(text) {
    return String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  }

  return {
    /** 快照引用在队列未变时保持稳定（dsh 的同一条约定）。 */
    getSnapshot() { return rows; },

    subscribe(fn) {
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    },

    /** 入队一条普通文本消息；返回行 id。 */
    enqueue(text) {
      _seq += 1;
      const id = "q" + _seq;
      const body = String(text == null ? "" : text);
      rows = rows.concat([{ id, placement: "queued", preview: previewOf(body), text: body }]);
      publish();
      return id;
    },

    /** 取队首并出队（轮结束自动发出时用）；空队列返回 null。 */
    dequeue() {
      if (rows.length === 0) return null;
      const head = rows[0];
      rows = rows.slice(1);
      publish();
      return head;
    },

    /** 取全部 queued 行（插话发送整队时用）。 */
    queued() {
      return rows.filter((r) => r.placement === "queued");
    },

    /**
     * 队列变更动词（对齐 SessionFace['updateQueue'] 的三种动作）。
     * @param {string} id
     * @param {{kind:'edit',content:{type:'text',text:string}[]}|{kind:'remove'}|{kind:'steer'}} action
     */
    async updateQueue(id, action) {
      const row = rows.find((r) => r.id === id);
      if (!row) throw new Error("queue row not found: " + id);
      if (action.kind === "edit") {
        const text = (action.content || [])
          .filter((b) => b && b.type === "text")
          .map((b) => b.text)
          .join("");
        rows = rows.map((r) => (r.id === id
          ? Object.assign({}, r, { text, preview: previewOf(text) })
          : r));
        publish();
        return;
      }
      if (action.kind === "remove") {
        rows = rows.filter((r) => r.id !== id);
        publish();
        return;
      }
      if (action.kind === "steer") {
        // [适配层边界] 本机后端没有「轮内投递窗口」，无法真正把消息注入正在跑的轮；
        // 等价可达的语义是「这条优先」⇒ 提到队首，轮结束先发它。
        const idx = rows.findIndex((r) => r.id === id);
        if (idx > 0) {
          const row = rows[idx];
          rows = [row].concat(rows.slice(0, idx), rows.slice(idx + 1));
          publish();
        }
        return;
      }
      throw new Error("unknown queue action: " + String(action.kind));
    },

    /** 清空（换卡/重置）。 */
    clear() {
      if (rows.length === 0) return;
      rows = [];
      publish();
    },
  };
}
